import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.evidence_providers import UniProtEvidenceProvider, _write_cache
from src.external_service_policy import (
    CircuitOpenError,
    ExternalServicePolicy,
    ExternalServicePolicyConfig,
    ServiceQuotaError,
    reset_service_policies,
)


class Response:
    def __init__(self, status_code=200, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')


class ExternalServicePolicyTests(unittest.TestCase):
    def test_retries_retry_after_then_succeeds(self):
        calls = []
        sleeps = []
        policy = ExternalServicePolicy(
            'test-retry',
            ExternalServicePolicyConfig(max_attempts=2, max_delay_seconds=10),
            sleep=sleeps.append,
            random_value=lambda: 0,
        )

        def operation():
            calls.append(True)
            if len(calls) == 1:
                return Response(503, {'Retry-After': '2'})
            return Response(200)

        result = policy.call(operation)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [2])

    def test_opens_circuit_after_failure_threshold(self):
        policy = ExternalServicePolicy(
            'test-circuit',
            ExternalServicePolicyConfig(max_attempts=1, circuit_failures=2),
        )
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                policy.call(lambda: (_ for _ in ()).throw(RuntimeError('offline')))
        with self.assertRaises(CircuitOpenError):
            policy.call(lambda: Response())

    def test_enforces_request_quota(self):
        policy = ExternalServicePolicy(
            'test-quota',
            ExternalServicePolicyConfig(max_attempts=1, requests_per_minute=1),
            clock=lambda: 10,
        )
        policy.call(lambda: Response())
        with self.assertRaises(ServiceQuotaError):
            policy.call(lambda: Response())

    def test_evidence_provider_uses_recent_stale_cache_on_outage(self):
        with tempfile.TemporaryDirectory(prefix='stale_evidence_') as raw:
            provider = UniProtEvidenceProvider(cache_dir=raw)
            path = provider._cache_path('TP53')
            payload = {'results': [{'primaryAccession': 'P04637'}]}
            with patch.dict('os.environ', {
                'EVIDENCE_CACHE_MODE': 'ttl',
                'EVIDENCE_CACHE_TTL_SECONDS': '60',
                'EVIDENCE_CACHE_STALE_IF_ERROR_SECONDS': '3600',
                'EXTERNAL_HTTP_MAX_ATTEMPTS': '1',
            }, clear=False):
                _write_cache(
                    path,
                    payload,
                    provider='uniprot',
                    request={'gene_id': 'TP53'},
                )
                document = json.loads(path.read_text(encoding='utf-8'))
                document['_cache']['expires_at'] = (
                    datetime.now(timezone.utc) - timedelta(seconds=60)
                ).isoformat()
                path.write_text(json.dumps(document), encoding='utf-8')
                reset_service_policies()
                with patch(
                    'src.evidence_providers.requests.get',
                    side_effect=RuntimeError('offline'),
                ):
                    self.assertEqual(provider._request('TP53'), payload)


if __name__ == '__main__':
    unittest.main()
