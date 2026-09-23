"""Shared resilience controls for synchronous external service calls."""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import random
from threading import BoundedSemaphore, Lock
import time

from prometheus_client import Counter, Gauge

try:
    from .settings import PlatformSettings
except ImportError:
    from settings import PlatformSettings


EXTERNAL_REQUESTS = Counter(
    'bio_agent_external_service_requests_total',
    'External service calls by outcome.',
    ['service', 'outcome'],
)
EXTERNAL_RETRIES = Counter(
    'bio_agent_external_service_retries_total',
    'External service retry attempts.',
    ['service', 'reason'],
)
EXTERNAL_RATE_LIMITED = Counter(
    'bio_agent_external_service_rate_limited_total',
    'External service calls delayed or rejected by local quotas.',
    ['service', 'outcome'],
)
EXTERNAL_IN_FLIGHT = Gauge(
    'bio_agent_external_service_in_flight',
    'External service calls currently in flight.',
    ['service'],
)
EXTERNAL_CIRCUIT_OPEN = Gauge(
    'bio_agent_external_service_circuit_open',
    'Whether an external service circuit is open.',
    ['service'],
)
EXTERNAL_STALE_CACHE = Counter(
    'bio_agent_external_service_stale_cache_total',
    'External service failures served from a verified stale cache.',
    ['service'],
)


class CircuitOpenError(RuntimeError):
    pass


class ServiceQuotaError(RuntimeError):
    pass


class ServiceRetryDeferredError(RuntimeError):
    def __init__(self, service, retry_after_seconds, status_code):
        self.service = str(service)
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code
        super().__init__(
            f'external service requested a {retry_after_seconds:.3f}s retry delay: {service}'
        )

    def as_payload(self):
        return {
            'error_code': 'external_retry_deferred',
            'service': self.service,
            'retry_after_seconds': self.retry_after_seconds,
            'status_code': self.status_code,
        }


def retry_deferred_from_payload(payload):
    if not isinstance(payload, dict) or payload.get('error_code') != 'external_retry_deferred':
        return None
    try:
        delay = float(payload['retry_after_seconds'])
        status = payload.get('status_code')
        status = int(status) if status is not None else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(delay) or delay <= 0:
        return None
    if status is not None and not 100 <= status <= 599:
        return None
    service = str(payload.get('service') or '').strip()
    if not service or len(service) > 128:
        return None
    return ServiceRetryDeferredError(service, delay, status)


@dataclass(frozen=True)
class ExternalServicePolicyConfig:
    max_attempts: int = 3
    base_delay_seconds: float = .25
    max_delay_seconds: float = 10
    circuit_failures: int = 5
    circuit_reset_seconds: float = 60
    max_concurrency: int = 4
    requests_per_minute: int = 60
    acquire_timeout_seconds: float = 30

    @classmethod
    def from_settings(cls, settings):
        return cls(
            max_attempts=settings.external_http_max_attempts,
            base_delay_seconds=settings.external_http_base_delay_seconds,
            max_delay_seconds=settings.external_http_max_delay_seconds,
            circuit_failures=settings.external_http_circuit_failures,
            circuit_reset_seconds=settings.external_http_circuit_reset_seconds,
            max_concurrency=settings.external_http_max_concurrency,
            requests_per_minute=settings.external_http_requests_per_minute,
            acquire_timeout_seconds=settings.external_http_acquire_timeout_seconds,
        )


def _status_code(value):
    code = getattr(value, 'status_code', None)
    if code is None:
        code = getattr(value, 'code', None)
    if code is None:
        getter = getattr(value, 'getcode', None)
        code = getter() if getter else None
    if code is None:
        response = getattr(value, 'response', None)
        code = getattr(response, 'status_code', None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _headers(value):
    headers = getattr(value, 'headers', None)
    if not headers:
        headers = getattr(getattr(value, 'response', None), 'headers', None)
    return headers or {}


def _retry_after_seconds(headers, now):
    getter = getattr(headers, 'get', None)
    raw = getter('Retry-After') if getter else None
    if raw is None:
        return None
    try:
        seconds = float(raw)
        return max(seconds, 0) if math.isfinite(seconds) else None
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(str(raw))
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(target.timestamp() - now, 0)
        except (TypeError, ValueError, OverflowError):
            return None


class ExternalServicePolicy:
    RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        service,
        config=None,
        *,
        clock=time.time,
        sleep=time.sleep,
        random_value=random.random,
    ):
        self.service = str(service)
        self.config = config or ExternalServicePolicyConfig()
        self._clock = clock
        self._sleep = sleep
        self._random = random_value
        self._semaphore = BoundedSemaphore(self.config.max_concurrency)
        self._lock = Lock()
        self._requests = deque()
        self._failures = 0
        self._open_until = 0.0
        self._probe_in_flight = False

    def _check_circuit(self):
        now = self._clock()
        with self._lock:
            if self._open_until > now or self._probe_in_flight:
                EXTERNAL_CIRCUIT_OPEN.labels(self.service).set(1)
                EXTERNAL_REQUESTS.labels(self.service, 'circuit_open').inc()
                raise CircuitOpenError(
                    f'external service circuit is open: {self.service}'
                )
            if self._open_until:
                self._probe_in_flight = True
                return True
            return False

    def _reserve_request(self):
        now = self._clock()
        with self._lock:
            while self._requests and self._requests[0] <= now - 60:
                self._requests.popleft()
            if len(self._requests) >= self.config.requests_per_minute:
                delay = max(self._requests[0] + 60 - now, 0)
                EXTERNAL_RATE_LIMITED.labels(self.service, 'rejected').inc()
                raise ServiceQuotaError(
                    f'external service quota exceeded for {self.service}; retry in {delay:.3f}s'
                )
            self._requests.append(now)

    def _success(self):
        with self._lock:
            self._failures = 0
            self._open_until = 0.0
        EXTERNAL_CIRCUIT_OPEN.labels(self.service).set(0)
        EXTERNAL_REQUESTS.labels(self.service, 'success').inc()

    def _failure(self, *, count_for_circuit=True, was_probe=False, retry_after=None):
        with self._lock:
            if count_for_circuit:
                self._failures += 1
                now = self._clock()
                if was_probe or self._failures >= self.config.circuit_failures:
                    self._open_until = now + self.config.circuit_reset_seconds
                if retry_after is not None:
                    self._open_until = max(self._open_until, now + retry_after)
                if self._open_until > now:
                    EXTERNAL_CIRCUIT_OPEN.labels(self.service).set(1)
            elif was_probe:
                self._failures = 0
                self._open_until = 0.0
                EXTERNAL_CIRCUIT_OPEN.labels(self.service).set(0)
        EXTERNAL_REQUESTS.labels(self.service, 'failure').inc()

    def _delay(self, attempt, retry_after=None):
        if retry_after is not None:
            return retry_after
        exponential = self.config.base_delay_seconds * (2 ** max(attempt - 1, 0))
        return min(exponential * (.5 + self._random()), self.config.max_delay_seconds)

    def call(self, operation):
        acquired = self._semaphore.acquire(timeout=self.config.acquire_timeout_seconds)
        if not acquired:
            EXTERNAL_RATE_LIMITED.labels(self.service, 'concurrency_timeout').inc()
            raise ServiceQuotaError(
                f'external service concurrency limit exceeded: {self.service}'
            )
        EXTERNAL_IN_FLIGHT.labels(self.service).inc()
        probe = False
        try:
            probe = self._check_circuit()
            last_error = None
            count_for_circuit = True
            retry_after = None
            max_attempts = 1 if probe else self.config.max_attempts
            for attempt in range(1, max_attempts + 1):
                self._reserve_request()
                retry_after = None
                observed_status = None
                try:
                    result = operation()
                    status = observed_status = _status_code(result)
                    if status in self.RETRYABLE_STATUS:
                        retry_after = _retry_after_seconds(_headers(result), self._clock())
                        error = RuntimeError(f'external service returned HTTP {status}')
                        setattr(error, 'status_code', status)
                        raise error
                    if status is not None and status >= 400:
                        raiser = getattr(result, 'raise_for_status', None)
                        if raiser:
                            raiser()
                        raise RuntimeError(f'external service returned HTTP {status}')
                    self._success()
                    return result
                except Exception as exc:
                    last_error = exc
                    status = observed_status if observed_status is not None else _status_code(exc)
                    if retry_after is None:
                        retry_after = _retry_after_seconds(_headers(exc), self._clock())
                    retryable = status is None or status in self.RETRYABLE_STATUS
                    count_for_circuit = retryable
                    if retryable and retry_after is not None and retry_after > self.config.max_delay_seconds:
                        last_error = ServiceRetryDeferredError(
                            self.service, retry_after, status
                        )
                        break
                    if not retryable or attempt >= max_attempts:
                        break
                    EXTERNAL_RETRIES.labels(
                        self.service,
                        f'http_{status}' if status else type(exc).__name__,
                    ).inc()
                    self._sleep(self._delay(attempt, retry_after))
            self._failure(
                count_for_circuit=count_for_circuit,
                was_probe=probe,
                retry_after=retry_after if count_for_circuit else None,
            )
            raise last_error
        finally:
            if probe:
                with self._lock:
                    self._probe_in_flight = False
            EXTERNAL_IN_FLIGHT.labels(self.service).dec()
            self._semaphore.release()


_POLICIES = {}
_POLICIES_LOCK = Lock()


def service_policy(service):
    name = str(service)
    settings = PlatformSettings.from_env()
    config = ExternalServicePolicyConfig.from_settings(settings)
    key = (name, config)
    with _POLICIES_LOCK:
        policy = _POLICIES.get(key)
        if policy is None:
            policy = ExternalServicePolicy(name, config)
            _POLICIES[key] = policy
        return policy


def resilient_call(service, operation):
    return service_policy(service).call(operation)


def record_stale_cache(service):
    EXTERNAL_STALE_CACHE.labels(str(service)).inc()


def reset_service_policies():
    with _POLICIES_LOCK:
        _POLICIES.clear()
