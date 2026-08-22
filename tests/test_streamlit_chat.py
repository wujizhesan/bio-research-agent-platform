import json
import time
import unittest
from pathlib import Path

from src.api_server import route_request
from src.streamlit_chat import handle_command


class StreamlitChatTests(unittest.TestCase):
    def test_submit_and_job_commands_use_async_api(self):
        command = handle_command(
            '/submit literature_search {"gene_ids":["GeneA"],"provider":"local","evidence_csv":"examples/rnaseq/evidence.csv"}',
            'all',
            Path.cwd(),
        )
        self.assertEqual(command['traces'][0]['result']['status'], 'accepted')
        job_id = command['traces'][0]['result']['job']['job_id']
        current = None
        for _ in range(100):
            current = route_request('GET', '/jobs/' + job_id)[1]['job']
            if current['status'] in {'completed', 'failed'}:
                break
            time.sleep(0.01)
        self.assertEqual(current['status'], 'completed')

        detail = handle_command('/job ' + job_id, 'all', Path.cwd())
        parsed = json.loads(detail['answer'])
        self.assertEqual(parsed['job']['job_id'], job_id)

        retry = handle_command('/retry ' + job_id, 'all', Path.cwd())
        retry_payload = json.loads(retry['answer'])
        self.assertEqual(retry_payload['status'], 'accepted')


if __name__ == '__main__':
    unittest.main()
