import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from src import domain_registry
from src.evidence_providers import UniProtEvidenceProvider, _write_cache
from src.external_service_policy import (
    CircuitOpenError,
    ExternalServicePolicy,
    ExternalServicePolicyConfig,
    ServiceQuotaError,
    ServiceRetryDeferredError,
    retry_deferred_from_payload,
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

    def test_retry_after_above_budget_defers_without_early_retry(self):
        now = [100.0]
        calls = []
        sleeps = []
        policy = ExternalServicePolicy(
            'test-deferred-retry',
            ExternalServicePolicyConfig(
                max_attempts=3,
                max_delay_seconds=10,
                circuit_failures=5,
            ),
            clock=lambda: now[0],
            sleep=sleeps.append,
        )

        def rate_limited():
            calls.append(True)
            return Response(429, {'Retry-After': '120'})

        with self.assertRaises(ServiceRetryDeferredError) as raised:
            policy.call(rate_limited)
        self.assertEqual(raised.exception.retry_after_seconds, 120)
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])
        with self.assertRaises(CircuitOpenError):
            policy.call(lambda: Response())
        now[0] = 221.0
        self.assertEqual(policy.call(lambda: Response()).status_code, 200)

    def test_deferred_retry_payload_requires_valid_delay_and_service(self):
        error = ServiceRetryDeferredError('uniprot', 120, 429)
        reconstructed = retry_deferred_from_payload(error.as_payload())
        self.assertEqual(reconstructed.service, 'uniprot')
        self.assertEqual(reconstructed.retry_after_seconds, 120)
        self.assertEqual(reconstructed.status_code, 429)
        self.assertIsNone(retry_deferred_from_payload({
            **error.as_payload(), 'retry_after_seconds': 'inf',
        }))
        self.assertIsNone(retry_deferred_from_payload({
            **error.as_payload(), 'service': '',
        }))

    def test_plugin_boundary_preserves_deferred_retry(self):
        domain, local_name, spec = domain_registry.REGISTRY.resolve('literature_search')

        def deferred(**_arguments):
            raise ServiceRetryDeferredError('uniprot', 120, 429)

        with patch.object(
            domain_registry.REGISTRY,
            'resolve',
            return_value=(domain, local_name, {**spec, 'function': deferred}),
        ):
            with self.assertRaises(ServiceRetryDeferredError):
                domain_registry.run_tool('literature_search', {'gene_ids': ['TP53']})

    def test_retry_after_from_exception_response_is_respected(self):
        policy = ExternalServicePolicy(
            'test-exception-retry-after',
            ExternalServicePolicyConfig(max_attempts=3, max_delay_seconds=5),
        )
        calls = []

        def rate_limited():
            calls.append(True)
            error = RuntimeError('rate limited')
            error.headers = {}
            error.response = Response(429, {'Retry-After': '30'})
            raise error

        with self.assertRaises(ServiceRetryDeferredError) as raised:
            policy.call(rate_limited)
        self.assertEqual(raised.exception.retry_after_seconds, 30)
        self.assertEqual(len(calls), 1)

    def test_final_attempt_preserves_short_retry_after(self):
        now = [100.0]
        policy = ExternalServicePolicy(
            'test-final-retry-after',
            ExternalServicePolicyConfig(
                max_attempts=1,
                max_delay_seconds=10,
                circuit_failures=5,
            ),
            clock=lambda: now[0],
        )
        with self.assertRaises(RuntimeError):
            policy.call(lambda: Response(429, {'Retry-After': '2'}))
        with self.assertRaises(CircuitOpenError):
            policy.call(lambda: Response())
        now[0] = 103.0
        self.assertEqual(policy.call(lambda: Response()).status_code, 200)

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

    def test_half_open_allows_only_one_probe(self):
        now = [100.0]
        policy = ExternalServicePolicy(
            'test-half-open',
            ExternalServicePolicyConfig(
                max_attempts=1,
                circuit_failures=1,
                circuit_reset_seconds=5,
                max_concurrency=2,
            ),
            clock=lambda: now[0],
        )
        with self.assertRaises(RuntimeError):
            policy.call(lambda: Response(503))
        now[0] = 106.0
        entered = Event()
        release = Event()
        results = []

        def probe():
            entered.set()
            release.wait(5)
            return Response()

        thread = Thread(target=lambda: results.append(policy.call(probe)))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(CircuitOpenError):
                policy.call(lambda: Response())
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].status_code, 200)
        self.assertEqual(policy.call(lambda: Response()).status_code, 200)

    def test_failed_probe_reopens_and_client_error_closes_circuit(self):
        now = [100.0]
        policy = ExternalServicePolicy(
            'test-probe-failure',
            ExternalServicePolicyConfig(
                max_attempts=1,
                circuit_failures=1,
                circuit_reset_seconds=5,
            ),
            clock=lambda: now[0],
        )
        with self.assertRaises(RuntimeError):
            policy.call(lambda: Response(503))
        now[0] = 106.0
        with self.assertRaises(RuntimeError):
            policy.call(lambda: Response(503))
        with self.assertRaises(CircuitOpenError):
            policy.call(lambda: Response())
        now[0] = 112.0
        with self.assertRaises(RuntimeError):
            policy.call(lambda: Response(404))
        self.assertEqual(policy.call(lambda: Response()).status_code, 200)

    def test_probe_slot_is_released_when_local_quota_rejects(self):
        now = [100.0]
        policy = ExternalServicePolicy(
            'test-probe-quota',
            ExternalServicePolicyConfig(
                max_attempts=1,
                circuit_failures=1,
                circuit_reset_seconds=5,
                requests_per_minute=1,
            ),
            clock=lambda: now[0],
        )
        with self.assertRaises(RuntimeError):
            policy.call(lambda: Response(503))
        now[0] = 106.0
        with self.assertRaises(ServiceQuotaError):
            policy.call(lambda: Response())
        now[0] = 161.0
        self.assertEqual(policy.call(lambda: Response()).status_code, 200)

    def test_client_errors_do_not_retry_or_open_circuit(self):
        policy = ExternalServicePolicy(
            'test-client-error',
            ExternalServicePolicyConfig(max_attempts=3, circuit_failures=2),
        )
        calls = []

        def not_found():
            calls.append(True)
            return Response(404)

        for _ in range(3):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 404'):
                policy.call(not_found)
        self.assertEqual(len(calls), 3)
        self.assertEqual(policy.call(lambda: Response()).status_code, 200)

    def test_exception_response_status_prevents_client_error_retry(self):
        policy = ExternalServicePolicy(
            'test-exception-response',
            ExternalServicePolicyConfig(max_attempts=3, circuit_failures=1),
        )
        calls = []

        def bad_request():
            calls.append(True)
            error = RuntimeError('bad request')
            error.response = Response(400)
            raise error

        with self.assertRaisesRegex(RuntimeError, 'bad request'):
            policy.call(bad_request)
        self.assertEqual(len(calls), 1)
        self.assertEqual(policy.call(lambda: Response()).status_code, 200)

    def test_jitter_does_not_exceed_max_delay(self):
        policy = ExternalServicePolicy(
            'test-delay-cap',
            ExternalServicePolicyConfig(base_delay_seconds=10, max_delay_seconds=5),
            random_value=lambda: 1,
        )
        self.assertEqual(policy._delay(2), 5)

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
