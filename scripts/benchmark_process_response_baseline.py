import json

from src.job_execution import JobExecutionError

BASELINE_SOURCE_COMMIT = '081b9b25c2e68ea55357dc62eb16e2a480053957'


def legacy_read_process_response(self, response_path):
    if response_path.stat().st_size > self.limits.max_result_bytes:
        raise JobExecutionError(f'job result exceeded {self.limits.max_result_bytes} byte limit')
    try:
        payload = json.loads(response_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise JobExecutionError('isolated worker returned an invalid response') from exc
    return payload
