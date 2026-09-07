import json
import logging
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from io import StringIO

from prometheus_client import generate_latest

from src.job_execution import InlineToolExecutor
from src.job_manager import JobManager
from src.observability import (
    JsonLogFormatter,
    bind_context,
    configure_logging,
    current_context,
    log_event,
    trace_id,
)
from src.workflow_runner import run_workflow


def _specs():
    return [{
        'name': 'demo_run',
        'domain': 'demo',
        'description': 'run',
        'parameters': {
            'type': 'object',
            'properties': {},
            'additionalProperties': False,
        },
        'returns': {},
        'resources': {},
        'plugin_version': '1.0.0',
        'plugin_api_version': 1,
        'plugin_contract_digest': 'contract-v1',
        'function': lambda: None,
    }]


class ObservabilityTests(unittest.TestCase):
    def test_context_is_nested_and_traceparent_is_supported(self):
        parent = '00-0123456789abcdef0123456789abcdef-0123456789abcdef-01'
        self.assertEqual(
            trace_id(traceparent=parent),
            '0123456789abcdef0123456789abcdef',
        )
        self.assertEqual(current_context(), {})
        with bind_context(trace_id='trace-1', request_id='request-1'):
            self.assertEqual(current_context()['trace_id'], 'trace-1')
            with bind_context(job_id='job-1'):
                self.assertEqual(current_context()['job_id'], 'job-1')
            self.assertNotIn('job_id', current_context())
        self.assertEqual(current_context(), {})

    def test_json_logs_include_context_and_redact_sensitive_fields(self):
        record = logging.LogRecord(
            'bio_agent.test', logging.INFO, __file__, 1,
            'tool.execution.completed', (), None,
        )
        record.event = 'tool.execution.completed'
        record.observability_fields = {
            'status': 'success',
            'api_key': 'secret-value',
            'nested': {'access_token': 'token-value'},
        }
        with bind_context(trace_id='trace-1', job_id='job-1'):
            payload = json.loads(JsonLogFormatter('test-service').format(record))
        self.assertEqual(payload['trace_id'], 'trace-1')
        self.assertEqual(payload['job_id'], 'job-1')
        self.assertEqual(payload['api_key'], '[REDACTED]')
        self.assertEqual(payload['nested']['access_token'], '[REDACTED]')

    def test_logger_uses_current_stderr_stream(self):
        logger = logging.getLogger('bio_agent')
        previous_handlers = list(logger.handlers)
        logger.handlers.clear()
        first = StringIO()
        second = StringIO()
        try:
            with patch('sys.stderr', first):
                configure_logging('test-service')
                log_event('first.event')
            first.close()
            with patch('sys.stderr', second):
                log_event('second.event')
            self.assertIn('second.event', second.getvalue())
        finally:
            logger.handlers.clear()
            logger.handlers.extend(previous_handlers)

    def test_job_context_propagates_to_worker_and_persists(self):
        observed = []

        def execute(_tool, _arguments):
            observed.append(current_context())
            return {'status': 'ok'}

        with tempfile.TemporaryDirectory(prefix='observability_job_') as raw:
            store = Path(raw) / 'jobs.sqlite3'
            manager = JobManager(
                max_workers=1,
                store_path=store,
                tool_executor=InlineToolExecutor(execute),
            )
            try:
                with bind_context(trace_id='trace-job', request_id='request-job'):
                    submitted = manager.submit('research_catalog', {})
                for _ in range(200):
                    completed = manager.get(submitted['job_id'])
                    if completed['status'] == 'completed':
                        break
                    time.sleep(0.01)
            finally:
                manager.shutdown()
            restored = JobManager(
                max_workers=1,
                store_path=store,
                tool_executor=InlineToolExecutor(execute),
            )
            try:
                persisted = restored.get(submitted['job_id'])
            finally:
                restored.shutdown()
        self.assertEqual(observed[0]['trace_id'], 'trace-job')
        self.assertEqual(observed[0]['request_id'], 'request-job')
        self.assertEqual(observed[0]['job_id'], submitted['job_id'])
        self.assertEqual(persisted['trace_id'], 'trace-job')
        self.assertEqual(persisted['request_id'], 'request-job')

    def test_workflow_manifest_and_metrics_share_trace_context(self):
        workflow = {'name': 'observable', 'steps': [{
            'id': 'one', 'tool': 'demo_run', 'args': {},
        }]}
        with bind_context(trace_id='trace-workflow', job_id='job-workflow'), patch(
            'src.workflow_runner.active_tool_specs', return_value=_specs()
        ), patch('src.workflow_runner.run_tool', return_value={'status': 'ok'}):
            manifest = run_workflow(workflow)
        self.assertEqual(manifest['observability']['trace_id'], 'trace-workflow')
        self.assertEqual(manifest['observability']['job_id'], 'job-workflow')
        metrics = generate_latest().decode('utf-8')
        self.assertIn('bio_agent_workflow_runs_total', metrics)
        self.assertIn('bio_agent_job_executions_total', metrics)


if __name__ == '__main__':
    unittest.main()
