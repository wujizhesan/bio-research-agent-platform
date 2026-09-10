import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from src.auth import Principal
from src.domain_registry import active_tool_specs, REGISTRY, run_tool
from src.job_execution import InlineToolExecutor
from src.job_manager import JobManager
from src.job_subprocess import main as subprocess_main
from src.observability import current_context
from src.run_context import (
    bind_run_actor,
    bind_run_context,
    build_run_context,
    current_run_context,
    derive_run_context,
    RunContext,
)


class RunContextTests(unittest.TestCase):
    def setUp(self):
        self.spec = next(
            spec for spec in active_tool_specs()
            if spec['name'] == 'research_catalog'
        )

    def test_context_is_immutable_and_round_trips(self):
        context = build_run_context(
            'research_catalog',
            {'seed': 17},
            spec=self.spec,
            resources={'cpu_cores': 2},
        )
        with self.assertRaises(TypeError):
            context.resources['cpu_cores'] = 4
        restored = RunContext.from_dict(json.loads(json.dumps(context.as_dict())))
        self.assertEqual(restored.as_dict(), context.as_dict())
        self.assertEqual(restored.random_seeds['seed'], 17)

    def test_context_hashes_inputs_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory(prefix='run_context_') as raw:
            source = Path(raw) / 'input.txt'
            source.write_text('research-data', encoding='utf-8')
            principal = Principal('scientist-1', ('researcher',), 'jwt')
            spec = dict(self.spec)
            spec['permissions'] = {
                'filesystem': {'read': ['input_file'], 'write': []},
                'network': [],
                'subprocess': [],
                'environment': [],
            }
            with bind_run_actor(principal):
                context = build_run_context(
                    'research_catalog',
                    {
                        'input_file': str(source),
                        'api_token': 'do-not-persist',
                        'random_seed': 42,
                    },
                    spec=spec,
                )
        payload = context.as_dict()
        self.assertEqual(payload['actor']['sub'], 'scientist-1')
        self.assertNotIn('do-not-persist', json.dumps(payload))
        self.assertEqual(payload['configuration']['argument_types']['api_token'], 'str')
        self.assertEqual(payload['input_hashes']['input_file']['size_bytes'], 13)
        self.assertEqual(payload['random_seeds']['random_seed'], 42)

    def test_binding_synchronizes_observability(self):
        context = build_run_context('research_catalog', {}, spec=self.spec, job_id='job-1')
        with bind_run_context(context):
            self.assertIs(current_run_context(), context)
            observable = current_context()
            self.assertEqual(observable['run_id'], context.run_id)
            self.assertEqual(observable['trace_id'], context.trace_id)
            self.assertEqual(observable['job_id'], 'job-1')
        self.assertIsNone(current_run_context())

    def test_derived_context_preserves_run_identity(self):
        parent = build_run_context('workflow:test', {}, spec={'domain': 'workflow'})
        child = derive_run_context(parent, 'research_catalog', {}, spec=self.spec)
        self.assertEqual(child.run_id, parent.run_id)
        self.assertEqual(child.trace_id, parent.trace_id)
        self.assertEqual(child.tool, 'research_catalog')
        self.assertEqual(child.domain, 'research')

    def test_plugin_can_read_current_context(self):
        observed = []
        tool = REGISTRY.domains['research'].tools['catalog']
        original = tool['function']

        def wrapped(**arguments):
            observed.append(current_run_context(as_dict=True))
            return original(**arguments)

        with patch.dict(tool, {'function': wrapped}):
            result = run_tool('research_catalog', {})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(observed[0]['tool'], 'research_catalog')
        self.assertEqual(observed[0]['domain'], 'research')

    def test_subprocess_entrypoint_rebinds_context(self):
        with tempfile.TemporaryDirectory(prefix='run_context_child_') as raw:
            request_path = Path(raw) / 'request.json'
            response_path = Path(raw) / 'response.json'
            context = build_run_context(
                'research_catalog', {}, spec=self.spec, job_id='child-job'
            )
            request_path.write_text(json.dumps({
                'tool': 'research_catalog',
                'arguments': {},
                'limits': {},
                'run_context': context.as_dict(),
            }), encoding='utf-8')
            observed = []

            def execute(_tool, _arguments):
                observed.append(current_run_context(as_dict=True))
                return {'status': 'ok'}

            with patch('src.domain_registry.run_tool', side_effect=execute):
                exit_code = subprocess_main([str(request_path), str(response_path)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(observed[0]['job_id'], 'child-job')
        self.assertEqual(observed[0]['run_id'], context.run_id)

    def test_job_context_persists_and_retry_records_lineage(self):
        with tempfile.TemporaryDirectory(prefix='run_context_jobs_') as raw:
            store = Path(raw) / 'jobs.sqlite3'
            manager = JobManager(
                max_workers=1,
                store_path=store,
                tool_executor=InlineToolExecutor(
                    lambda _tool, _arguments: {'status': 'ok'}
                ),
            )
            try:
                principal = Principal('scientist-2', ('researcher',), 'jwt')
                with bind_run_actor(principal):
                    submitted = manager.submit('research_catalog', {})
                for _ in range(100):
                    original = manager.get(submitted['job_id'])
                    if original['status'] == 'completed':
                        break
                    time.sleep(0.01)
                retried = manager.retry(original['job_id'])
                self.assertEqual(original['run_context']['actor']['sub'], 'scientist-2')
                self.assertEqual(retried['run_context']['retry_of'], original['job_id'])
                self.assertEqual(
                    retried['run_context']['parent_run_id'],
                    original['run_context']['run_id'],
                )
                self.assertNotEqual(
                    retried['run_context']['run_id'],
                    original['run_context']['run_id'],
                )
            finally:
                manager.shutdown()
            restored = JobManager(max_workers=1, store_path=store)
            try:
                record = restored.get(submitted['job_id'])
                self.assertEqual(record['run_context'], original['run_context'])
            finally:
                restored.shutdown()


if __name__ == '__main__':
    unittest.main()
