import hashlib
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, sleep
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml

from src.external_service_policy import ServiceRetryDeferredError
from src.job_execution import (
    ExecutionLimits,
    JobExecutionCancelled,
    build_tool_executor_from_env,
)
from src.plugin_container import ContainerToolExecutor
from src.plugin_sandbox_server import SandboxRuntime, create_server
from src.storage_workspace import S3ObjectReference

TOKEN = 'test-plugin-sandbox-token-0000000000000000'


class PluginContainerTests(unittest.TestCase):
    def limits(self):
        return ExecutionLimits(
            timeout_seconds=5,
            memory_limit_mb=512,
            cpu_time_seconds=2,
            max_result_bytes=1024 * 1024,
            poll_interval_seconds=0.01,
            terminate_grace_seconds=1,
        )

    def test_container_executor_transports_context_and_result(self):
        calls = []

        def transport(path, payload, timeout):
            calls.append((path, payload, timeout))
            return {'ok': True, 'result': {'status': 'ok', 'value': 7}}

        executor = ContainerToolExecutor(
            'http://plugin-sandbox:8081',
            TOKEN,
            limits=self.limits(),
            transport=transport,
        )
        try:
            result = executor.execute('demo_run', {'value': 7})
        finally:
            executor.shutdown()
        self.assertEqual(result['value'], 7)
        self.assertEqual(calls[0][0], '/v1/execute')
        self.assertEqual(calls[0][1]['tool'], 'demo_run')
        self.assertEqual(calls[0][1]['limits']['memory_limit_mb'], 512)

    def test_container_executor_propagates_cancellation(self):
        executing = Event()
        released = Event()

        def transport(path, _payload, _timeout):
            if path == '/v1/execute':
                executing.set()
                released.wait(2)
                return {'ok': False, 'error': 'cancelled'}
            released.set()
            return {'ok': True}

        executor = ContainerToolExecutor(
            'http://plugin-sandbox:8081',
            TOKEN,
            limits=self.limits(),
            transport=transport,
        )
        try:
            with self.assertRaises(JobExecutionCancelled):
                executor.execute(
                    'demo_run',
                    {},
                    cancelled=lambda: executing.is_set(),
                )
        finally:
            released.set()
            executor.shutdown()

    def test_container_executor_materializes_input_for_sandbox_and_cleans_workspace(self):
        content = b'gene,value\nTP53,12\n'
        digest = hashlib.sha256(content).hexdigest()
        observed = {}

        class Client:
            def head_object(self, **_request):
                return {
                    'VersionId': 'version-1',
                    'ContentLength': len(content),
                    'Metadata': {'sha256': digest},
                }

            def download_file(self, _bucket, _key, filename, ExtraArgs=None):
                Path(filename).write_bytes(content)

        def transport(_path, payload, _timeout):
            input_path = Path(payload['arguments']['input_path'])
            observed['path'] = input_path
            observed['content'] = input_path.read_bytes()
            return {'ok': True, 'result': {'status': 'ok'}}

        reference = S3ObjectReference(
            'bio-test',
            'research/file/expression.csv',
            'version-1',
            digest,
            len(content),
        ).serialize()
        with tempfile.TemporaryDirectory(prefix='container_inputs_') as raw:
            executor = ContainerToolExecutor(
                'http://plugin-sandbox:8081',
                TOKEN,
                limits=self.limits(),
                transport=transport,
                input_workspace_root=raw,
                storage_client=Client(),
            )
            try:
                with patch.dict(os.environ, {'S3_BUCKET': 'bio-test', 'S3_PREFIX': 'research'}):
                    executor.execute('demo_run', {'input_path': reference})
            finally:
                executor.shutdown()
            self.assertEqual(list(Path(raw).iterdir()), [])
        self.assertEqual(observed['content'], content)
        self.assertFalse(observed['path'].exists())

    def test_environment_selects_container_executor(self):
        values = {
            'JOB_EXECUTION_MODE': 'container',
            'PLUGIN_SANDBOX_URL': 'http://plugin-sandbox:8081',
            'PLUGIN_SANDBOX_TOKEN': TOKEN,
        }
        with patch.dict(os.environ, values, clear=False):
            executor = build_tool_executor_from_env()
        try:
            self.assertEqual(executor.mode, 'container')
        finally:
            executor.shutdown()

    def test_container_executor_transfers_only_contract_paths_and_publishes_output(self):
        with tempfile.TemporaryDirectory(prefix='container_transfer_') as raw:
            root = Path(raw)
            inputs = root / 'inputs'
            artifacts = root / 'artifacts'
            exchange = root / 'exchange'
            inputs.mkdir()
            artifacts.mkdir()
            source = inputs / 'sample.csv'
            source.write_text('gene,value\nTP53,12\n', encoding='utf-8')
            target = artifacts / 'result.csv'

            def transport(_path, payload, _timeout):
                staged_input = Path(payload['arguments']['input_path'])
                staged_output = Path(payload['arguments']['output_path'])
                self.assertTrue(staged_input.is_relative_to(exchange))
                self.assertTrue(staged_output.is_relative_to(exchange))
                self.assertNotIn(str(artifacts), str(payload['arguments']))
                staged_output.write_text(
                    staged_input.read_text(encoding='utf-8'),
                    encoding='utf-8',
                )
                return {
                    'ok': True,
                    'result': {'output_path': str(staged_output)},
                }

            executor = ContainerToolExecutor(
                'http://plugin-sandbox:8081',
                TOKEN,
                limits=self.limits(),
                transport=transport,
                input_workspace_root=exchange,
                input_roots=(inputs,),
                artifact_root=artifacts,
            )
            try:
                with patch(
                    'src.plugin_container._tool_filesystem_contract',
                    return_value=(
                        {'input_path'},
                        {'output_path': 'file'},
                        {'output_path'},
                    ),
                ):
                    result = executor.execute('demo_run', {
                        'input_path': str(source),
                        'output_path': str(target),
                    })
            finally:
                executor.shutdown()
            self.assertEqual(result['output_path'], str(target.resolve()))
            self.assertEqual(
                target.read_text(encoding='utf-8'),
                source.read_text(encoding='utf-8'),
            )
            self.assertEqual(list(exchange.iterdir()), [])

    def test_environment_reads_sandbox_token_from_secret_file(self):
        with tempfile.TemporaryDirectory() as raw:
            secret = Path(raw) / 'sandbox-token'
            secret.write_text(TOKEN, encoding='utf-8')
            values = {
                'JOB_EXECUTION_MODE': 'container',
                'PLUGIN_SANDBOX_URL': 'http://plugin-sandbox:8081',
                'PLUGIN_SANDBOX_TOKEN': '',
                'PLUGIN_SANDBOX_TOKEN_FILE': str(secret),
            }
            with patch.dict(os.environ, values, clear=False):
                executor = build_tool_executor_from_env()
        try:
            self.assertEqual(executor.token, TOKEN)
        finally:
            executor.shutdown()

    def test_sandbox_runtime_executes_and_cleans_active_request(self):
        executor = Mock()
        executor.execute.return_value = {'status': 'ok'}
        runtime = SandboxRuntime(
            executor_factory=lambda: executor,
            max_concurrency=1,
        )
        result = runtime.execute({
            'request_id': 'a' * 32,
            'tool': 'demo_run',
            'arguments': {'value': 1},
        })
        self.assertEqual(result, {'ok': True, 'result': {'status': 'ok'}})
        self.assertEqual(runtime.active_count, 0)
        executor.execute.assert_called_once()
        executor.shutdown.assert_called_once()

    def test_sandbox_runs_four_lightweight_requests_together(self):
        started = [Event() for _ in range(4)]
        release = Event()
        start_lock = Lock()
        next_start = [0]

        def make_executor():
            executor = Mock()

            def execute(_tool, _arguments, **_kwargs):
                with start_lock:
                    index = next_start[0]
                    next_start[0] += 1
                started[index].set()
                self.assertTrue(release.wait(2))
                return {'status': 'ok'}

            executor.execute.side_effect = execute
            return executor

        with patch.dict(os.environ, {
            'PLUGIN_SANDBOX_LIGHT_MEMORY_LIMIT_MB': '1024',
            'PLUGIN_SANDBOX_MEMORY_BUDGET_MB': '4096',
        }):
            runtime = SandboxRuntime(executor_factory=make_executor, max_concurrency=4)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(runtime.execute, {
                'request_id': chr(ord('a') + index) * 32,
                'tool': 'omics_inspect_toolchain',
                'arguments': {},
            }) for index in range(4)]
            try:
                for event in started:
                    self.assertTrue(event.wait(2))
                self.assertEqual(runtime.active_count, 4)
            finally:
                release.set()
            for future in futures:
                self.assertTrue(future.result(timeout=2)['ok'])
        self.assertEqual(runtime.active_count, 0)

    def test_sandbox_reserves_capacity_for_waiting_heavy_request(self):
        light_started = Event()
        heavy_started = Event()
        second_light_started = Event()
        release_light = Event()
        release_heavy = Event()

        def make_executor():
            executor = Mock()

            def execute(tool, _arguments, **_kwargs):
                if tool == 'omics_run_analysis':
                    heavy_started.set()
                    self.assertTrue(release_heavy.wait(3))
                elif not light_started.is_set():
                    light_started.set()
                    self.assertTrue(release_light.wait(3))
                else:
                    second_light_started.set()
                return {'status': 'ok'}

            executor.execute.side_effect = execute
            return executor

        runtime = SandboxRuntime(executor_factory=make_executor, max_concurrency=2)
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(runtime.execute, {
                'request_id': 'a' * 32,
                'tool': 'omics_inspect_toolchain',
                'arguments': {},
            })
            self.assertTrue(light_started.wait(2))
            heavy = pool.submit(runtime.execute, {
                'request_id': 'b' * 32,
                'tool': 'omics_run_analysis',
                'arguments': {},
            })
            deadline = monotonic() + 2
            while monotonic() < deadline:
                with runtime._capacity:
                    if runtime._waiting_exclusive:
                        break
                sleep(0.01)
            else:
                self.fail('heavy request did not enter the admission queue')
            second = pool.submit(runtime.execute, {
                'request_id': 'c' * 32,
                'tool': 'omics_inspect_toolchain',
                'arguments': {},
            })
            try:
                self.assertFalse(heavy_started.is_set())
                self.assertFalse(second_light_started.is_set())
                release_light.set()
                self.assertTrue(heavy_started.wait(2))
                self.assertFalse(second_light_started.is_set())
            finally:
                release_light.set()
                release_heavy.set()
            self.assertTrue(first.result(timeout=2)['ok'])
            self.assertTrue(heavy.result(timeout=2)['ok'])
            self.assertTrue(second.result(timeout=2)['ok'])
        self.assertTrue(second_light_started.is_set())
        self.assertEqual(runtime.active_count, 0)

    def test_sandbox_can_cancel_waiting_request(self):
        started = Event()
        release = Event()
        executor = Mock()

        def execute(_tool, _arguments, **_kwargs):
            started.set()
            self.assertTrue(release.wait(3))
            return {'status': 'ok'}

        executor.execute.side_effect = execute
        runtime = SandboxRuntime(executor_factory=lambda: executor, max_concurrency=1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(runtime.execute, {
                'request_id': 'a' * 32,
                'tool': 'omics_inspect_toolchain',
                'arguments': {},
            })
            self.assertTrue(started.wait(2))
            waiting = pool.submit(runtime.execute, {
                'request_id': 'b' * 32,
                'tool': 'omics_run_analysis',
                'arguments': {},
            })
            deadline = monotonic() + 2
            while monotonic() < deadline:
                if runtime.cancel('b' * 32):
                    break
                sleep(0.01)
            else:
                self.fail('waiting request was not registered for cancellation')
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                waiting.result(timeout=2)
            release.set()
            self.assertTrue(first.result(timeout=2)['ok'])
        self.assertEqual(runtime.active_count, 0)

    def test_large_knowledge_index_stays_exclusive(self):
        with tempfile.TemporaryDirectory() as raw:
            index = Path(raw) / 'large.json'
            with index.open('wb') as handle:
                handle.truncate(8 * 1024 * 1024 + 1)
            self.assertFalse(SandboxRuntime._lightweight(
                'knowledge_search', {'index_path': str(index)}
            ))
            self.assertTrue(SandboxRuntime._lightweight(
                'knowledge_search', {'index_path': str(Path(__file__))}
            ))

    def test_sandbox_memory_budget_caps_parallel_admission(self):
        with patch.dict(os.environ, {
            'PLUGIN_SANDBOX_LIGHT_MEMORY_LIMIT_MB': '2048',
            'PLUGIN_SANDBOX_MEMORY_BUDGET_MB': '4096',
        }):
            runtime = SandboxRuntime(max_concurrency=8)
        self.assertEqual(runtime.max_concurrency, 2)

    def test_sandbox_limits_memory_of_lightweight_children(self):
        with patch.dict(os.environ, {
            'JOB_MEMORY_LIMIT_MB': '4096',
            'PLUGIN_SANDBOX_LIGHT_MEMORY_LIMIT_MB': '2048',
        }):
            with patch('src.plugin_sandbox_server.ProcessToolExecutor') as factory:
                factory.return_value.execute.return_value = {'status': 'ok'}
                runtime = SandboxRuntime(max_concurrency=2)
                runtime.execute({
                    'request_id': 'a' * 32,
                    'tool': 'omics_inspect_toolchain',
                    'arguments': {},
                })
                light_limit = factory.call_args.args[0].memory_limit_mb
                runtime.execute({
                    'request_id': 'b' * 32,
                    'tool': 'omics_run_analysis',
                    'arguments': {},
                })
                heavy_limit = factory.call_args.args[0].memory_limit_mb
        self.assertEqual(light_limit, 2048)
        self.assertEqual(heavy_limit, 4096)

    def test_sandbox_server_requires_strong_shared_token(self):
        with self.assertRaisesRegex(ValueError, 'at least 32'):
            create_server('127.0.0.1', 0, 'short')

    def test_sandbox_server_hides_internal_execution_errors(self):
        executor = Mock()
        executor.execute.side_effect = RuntimeError(
            'token=secret-value /srv/private/input.fastq'
        )
        runtime = SandboxRuntime(executor_factory=lambda: executor)
        server = create_server('127.0.0.1', 0, TOKEN, runtime=runtime)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        payload = json.dumps({
            'request_id': 'a' * 32,
            'tool': 'research_catalog',
            'arguments': {},
        }).encode('utf-8')
        request = Request(
            f'http://127.0.0.1:{server.server_port}/v1/execute',
            data=payload,
            headers={
                'Authorization': f'Bearer {TOKEN}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        try:
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=5)
            response = json.loads(raised.exception.read().decode('utf-8'))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(raised.exception.code, 500)
        self.assertEqual(response, {
            'ok': False,
            'error_code': 'sandbox_execution_failed',
            'error': 'plugin execution failed',
        })
        self.assertNotIn('secret-value', str(response))
        self.assertNotIn('/srv/private', str(response))

    def test_sandbox_preserves_deferred_retry_metadata(self):
        worker = Mock()
        worker.execute.side_effect = ServiceRetryDeferredError('uniprot', 45, 429)
        with tempfile.TemporaryDirectory(prefix='deferred_sandbox_') as raw:
            server = create_server(
                '127.0.0.1', 0, TOKEN,
                runtime=SandboxRuntime(
                    executor_factory=lambda: worker,
                    workspace_root=raw,
                ),
            )
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            executor = ContainerToolExecutor(
                f'http://127.0.0.1:{server.server_port}',
                TOKEN,
                limits=self.limits(),
                input_workspace_root=raw,
            )
            try:
                with self.assertRaises(ServiceRetryDeferredError) as raised:
                    executor.execute('demo_run', {})
            finally:
                executor.shutdown()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
        self.assertEqual(raised.exception.service, 'uniprot')
        self.assertEqual(raised.exception.retry_after_seconds, 45)
        self.assertEqual(raised.exception.status_code, 429)

    def test_secure_compose_has_container_and_scanner_boundaries(self):
        root = Path(__file__).resolve().parent.parent
        compose = yaml.safe_load(
            (root / 'docker-compose.secure.yml').read_text(encoding='utf-8')
        )
        services = compose['services']
        sandbox = services['plugin-sandbox']
        self.assertTrue(sandbox['read_only'])
        self.assertEqual(sandbox['cap_drop'], ['ALL'])
        self.assertIn('no-new-privileges:true', sandbox['security_opt'])
        self.assertEqual(sandbox['networks'], ['plugin_control'])
        self.assertNotIn('ports', sandbox)
        self.assertEqual(
            services['worker']['environment']['JOB_EXECUTION_MODE'],
            'container',
        )
        self.assertEqual(
            services['worker']['environment']['JOB_INPUT_WORKSPACE_ROOT'],
            '/run/bioagent/plugin-exchange',
        )
        self.assertIn(':-4}', services['worker']['environment']['WORKER_MAX_CONCURRENCY'])
        self.assertIn(':-4}', services['worker']['environment']['PLUGIN_SANDBOX_CLIENT_CONCURRENCY'])
        self.assertIn(':-4}', sandbox['environment']['PLUGIN_SANDBOX_MAX_CONCURRENCY'])
        self.assertIn(':-1024}', sandbox['environment']['PLUGIN_SANDBOX_LIGHT_MEMORY_LIMIT_MB'])
        self.assertIn(':-4096}', sandbox['environment']['PLUGIN_SANDBOX_MEMORY_BUDGET_MB'])
        self.assertNotIn('./output:/app/output:rw', sandbox['volumes'])
        self.assertIn(
            'plugin_exchange:/run/bioagent/plugin-exchange:rw',
            sandbox['volumes'],
        )
        self.assertNotIn('ports', services['clamav'])
        self.assertTrue(compose['networks']['malware_scan']['internal'])
        self.assertTrue(compose['networks']['plugin_control']['internal'])


if __name__ == '__main__':
    unittest.main()
