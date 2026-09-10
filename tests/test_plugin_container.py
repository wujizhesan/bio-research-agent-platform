import os
from pathlib import Path
from threading import Event
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

from src.job_execution import (
    ExecutionLimits,
    JobExecutionCancelled,
    build_tool_executor_from_env,
)
from src.plugin_container import ContainerToolExecutor
from src.plugin_sandbox_server import SandboxRuntime, create_server


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

    def test_sandbox_server_requires_strong_shared_token(self):
        with self.assertRaisesRegex(ValueError, 'at least 32'):
            create_server('127.0.0.1', 0, 'short')

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
        self.assertNotIn('ports', services['clamav'])
        self.assertTrue(compose['networks']['malware_scan']['internal'])
        self.assertTrue(compose['networks']['plugin_control']['internal'])


if __name__ == '__main__':
    unittest.main()
