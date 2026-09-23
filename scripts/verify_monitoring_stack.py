import argparse
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen


SMOKE_ALERT = 'BioAgentMonitoringSmoke'


def request_json(url, method='GET', payload=None):
    body = None
    headers = {'Accept': 'application/json'}
    if payload is not None:
        body = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=10) as response:
        data = response.read()
    return json.loads(data.decode('utf-8')) if data else {}


def active_targets(payload):
    targets = payload.get('data', {}).get('activeTargets', [])
    return {
        str(item.get('labels', {}).get('job')): str(item.get('health'))
        for item in targets
    }


def contains_smoke_alert(path):
    target = Path(path)
    if not target.is_file():
        return False
    for line in target.read_text(encoding='utf-8').splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        alerts = record.get('payload', {}).get('alerts', [])
        if any(
            item.get('labels', {}).get('alertname') == SMOKE_ALERT
            for item in alerts
        ):
            return True
    return False


def verify(
    prometheus_url,
    alertmanager_url,
    delivery_file,
    evidence_directory,
    timeout_seconds=60,
    requester=None,
    sleep_fn=time.sleep,
):
    requester = requester or request_json
    deadline = time.monotonic() + max(float(timeout_seconds), 1)
    targets_payload = {}
    while time.monotonic() < deadline:
        targets_payload = requester(
            f'{prometheus_url.rstrip("/")}/api/v1/targets'
        )
        targets = active_targets(targets_payload)
        if targets.get('bioagent-api') == 'up' and targets.get('bioagent-worker') == 'up':
            break
        sleep_fn(1)
    else:
        raise RuntimeError('Prometheus did not scrape API and Worker successfully')

    evidence = Path(evidence_directory)
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / 'prometheus-targets.json').write_text(
        json.dumps(targets_payload, ensure_ascii=True, indent=2, sort_keys=True),
        encoding='utf-8',
    )
    requester(
        f'{alertmanager_url.rstrip("/")}/api/v2/alerts',
        method='POST',
        payload=[{
            'labels': {
                'alertname': SMOKE_ALERT,
                'severity': 'info',
            },
            'annotations': {
                'summary': 'Production monitoring delivery smoke test',
            },
        }],
    )
    while time.monotonic() < deadline:
        if contains_smoke_alert(delivery_file):
            return {
                'status': 'ok',
                'targets': active_targets(targets_payload),
                'alert': SMOKE_ALERT,
            }
        sleep_fn(1)
    raise RuntimeError('Alertmanager webhook delivery was not observed')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--prometheus-url', default='http://127.0.0.1:9090')
    parser.add_argument('--alertmanager-url', default='http://127.0.0.1:9093')
    parser.add_argument('--delivery-file', required=True)
    parser.add_argument('--evidence-directory', required=True)
    parser.add_argument('--timeout-seconds', type=float, default=60)
    args = parser.parse_args(argv)
    print(json.dumps(verify(
        args.prometheus_url,
        args.alertmanager_url,
        args.delivery_file,
        args.evidence_directory,
        timeout_seconds=args.timeout_seconds,
    ), ensure_ascii=True, sort_keys=True))


if __name__ == '__main__':
    main()
