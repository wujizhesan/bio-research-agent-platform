import json

BASELINE_SOURCE_COMMIT = '1b54aa858b1b6a06fc1fe4a2f0759d03405df4e4'


def legacy_write_process_response(payload, request, response_path, exit_code):
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    max_result_bytes = int(request.get('limits', {}).get('max_result_bytes') or 0)
    if max_result_bytes and len(encoded.encode('utf-8')) > max_result_bytes:
        encoded = json.dumps({'ok': False, 'error': f'job result exceeded {max_result_bytes} byte limit'})
        exit_code = 1
    response_path.write_text(encoded, encoding='utf-8')
    return exit_code
