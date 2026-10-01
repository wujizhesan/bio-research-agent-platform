import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from urllib.request import Request, urlopen

from scripts.alert_webhook_sink import create_handler
from scripts.verify_monitoring_stack import (
    SMOKE_ALERT,
    active_targets,
    contains_smoke_alert,
    verify,
)


class MonitoringSmokeTests(unittest.TestCase):
    def test_target_health_and_delivery_parser(self):
        payload = {
            'data': {
                'activeTargets': [
                    {'labels': {'job': 'bioagent-api'}, 'health': 'up'},
                    {'labels': {'job': 'bioagent-worker'}, 'health': 'down'},
                ]
            }
        }
        self.assertEqual(active_targets(payload), {
            'bioagent-api': 'up',
            'bioagent-worker': 'down',
        })
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'alerts.jsonl'
            path.write_text(json.dumps({
                'payload': {
                    'alerts': [{'labels': {'alertname': SMOKE_ALERT}}],
                }
            }) + '\n', encoding='utf-8')
            self.assertTrue(contains_smoke_alert(path))

    def test_verifier_requires_targets_and_webhook_delivery(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            delivery = root / 'alerts.jsonl'

            def requester(url, method='GET', payload=None):
                if url.endswith('/api/v1/targets'):
                    return {'data': {'activeTargets': [
                        {'labels': {'job': 'bioagent-api'}, 'health': 'up'},
                        {'labels': {'job': 'bioagent-worker'}, 'health': 'up'},
                    ]}}
                self.assertEqual(method, 'POST')
                delivery.write_text(json.dumps({
                    'payload': {'alerts': payload},
                }) + '\n', encoding='utf-8')
                return {}

            result = verify(
                'http://prometheus',
                'http://alertmanager',
                delivery,
                root / 'evidence',
                requester=requester,
                sleep_fn=lambda _seconds: None,
            )
            self.assertEqual(result['status'], 'ok')
            self.assertTrue((root / 'evidence/prometheus-targets.json').is_file())

    def test_webhook_sink_accepts_only_expected_routes(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / 'alerts.jsonl'
            from http.server import ThreadingHTTPServer
            server = ThreadingHTTPServer(
                ('127.0.0.1', 0),
                create_handler(target),
            )
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                with urlopen(f'{base}/health', timeout=5) as response:
                    self.assertEqual(response.status, 200)
                body = json.dumps({'alerts': [
                    {'labels': {'alertname': SMOKE_ALERT}},
                ]}).encode('utf-8')
                request = Request(
                    f'{base}/alerts',
                    data=body,
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                self.assertTrue(contains_smoke_alert(target))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
