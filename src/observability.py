"""Unified metrics, trace context and structured logging for the platform."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import os
import re
import sys
from time import perf_counter
from uuid import uuid4

from prometheus_client import Counter, Gauge, Histogram


REQUEST_ID = ContextVar('bio_agent_request_id', default=None)
TRACE_ID = ContextVar('bio_agent_trace_id', default=None)
JOB_ID = ContextVar('bio_agent_job_id', default=None)
RUN_ID = ContextVar('bio_agent_run_id', default=None)
STEP_ID = ContextVar('bio_agent_step_id', default=None)
TOOL_NAME = ContextVar('bio_agent_tool_name', default=None)
PLUGIN_DOMAIN = ContextVar('bio_agent_plugin_domain', default=None)
_REQUEST_ID_PATTERN = re.compile(r'^[A-Za-z0-9._:-]{1,128}$')
_TRACEPARENT_PATTERN = re.compile(
    r'^[\da-fA-F]{2}-([\da-fA-F]{32})-[\da-fA-F]{16}-[\da-fA-F]{2}$'
)
_SENSITIVE_KEY = re.compile(
    r'(authorization|cookie|password|passwd|secret|token|api[_-]?key|private[_-]?key)',
    re.IGNORECASE,
)
_CONTEXT = {
    'request_id': REQUEST_ID,
    'trace_id': TRACE_ID,
    'job_id': JOB_ID,
    'run_id': RUN_ID,
    'step_id': STEP_ID,
    'tool': TOOL_NAME,
    'plugin': PLUGIN_DOMAIN,
}


def request_id(value=None):
    candidate = value.strip() if isinstance(value, str) else ''
    return candidate if _REQUEST_ID_PATTERN.fullmatch(candidate) else uuid4().hex


def trace_id(value=None, traceparent=None):
    parent = traceparent.strip() if isinstance(traceparent, str) else ''
    match = _TRACEPARENT_PATTERN.fullmatch(parent)
    if match and match.group(1) != '0' * 32:
        return match.group(1).lower()
    candidate = value.strip() if isinstance(value, str) else ''
    return candidate if _REQUEST_ID_PATTERN.fullmatch(candidate) else uuid4().hex


def current_context():
    return {
        name: variable.get()
        for name, variable in _CONTEXT.items()
        if variable.get() is not None
    }


@contextmanager
def bind_context(**values):
    tokens = []
    for name, value in values.items():
        variable = _CONTEXT.get(name)
        if variable is not None and value is not None:
            tokens.append((variable, variable.set(str(value)[:128])))
    try:
        yield current_context()
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def _max_field_length():
    try:
        return max(int(os.environ.get('OBSERVABILITY_MAX_FIELD_LENGTH', '512')), 64)
    except (TypeError, ValueError):
        return 512


def sanitize(value, key=None, depth=0):
    if key is not None and _SENSITIVE_KEY.search(str(key)):
        return '[REDACTED]'
    if depth >= 6:
        return '[TRUNCATED]'
    if isinstance(value, dict):
        return {
            str(item_key): sanitize(item, item_key, depth + 1)
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item, key, depth + 1) for item in list(value)[:50]]
    if isinstance(value, str):
        limit = _max_field_length()
        return value if len(value) <= limit else value[:limit] + '...'
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize(str(value), key, depth + 1)


class JsonLogFormatter(logging.Formatter):
    def __init__(self, service='bio-agent'):
        super().__init__()
        self.service = service

    def format(self, record):
        payload = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'level': record.levelname.lower(),
            'service': self.service,
            'logger': record.name,
            'event': getattr(record, 'event', record.getMessage()),
            **current_context(),
        }
        fields = getattr(record, 'observability_fields', None)
        if fields:
            payload.update(sanitize(fields))
        if record.exc_info:
            payload['exception_type'] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(',', ':'), default=str)


class DynamicStderrHandler(logging.StreamHandler):
    def emit(self, record):
        self.stream = sys.stderr
        super().emit(record)


def configure_logging(service='bio-agent'):
    logger = logging.getLogger('bio_agent')
    level = logging.getLevelNamesMapping().get(
        os.environ.get('LOG_LEVEL', 'INFO').strip().upper(),
        logging.INFO,
    )
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = DynamicStderrHandler()
        logger.addHandler(handler)
    formatter = (
        JsonLogFormatter(service)
        if os.environ.get('LOG_FORMAT', 'json').strip().lower() == 'json'
        else logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')
    )
    for handler in logger.handlers:
        handler.setFormatter(formatter)
    return logger


def log_event(event, level=logging.INFO, **fields):
    logger = logging.getLogger('bio_agent')
    if not logger.handlers:
        logger = configure_logging()
    logger.log(
        level,
        event,
        extra={
            'event': str(event),
            'observability_fields': sanitize(fields),
        },
    )


@contextmanager
def observed(event, **fields):
    started = perf_counter()
    log_event(event + '.started', **fields)
    try:
        yield
    except Exception as exc:
        log_event(
            event + '.failed',
            level=logging.ERROR,
            duration_seconds=perf_counter() - started,
            error_type=type(exc).__name__,
            **fields,
        )
        raise
    else:
        log_event(
            event + '.completed',
            duration_seconds=perf_counter() - started,
            **fields,
        )


HTTP_REQUESTS = Counter(
    'bio_agent_http_requests_total',
    'Total HTTP requests handled by the research Agent API.',
    ['method', 'path', 'status'],
)
HTTP_LATENCY = Histogram(
    'bio_agent_http_request_duration_seconds',
    'HTTP request latency in seconds.',
    ['method', 'path'],
)
HTTP_ACTIVE = Gauge(
    'bio_agent_http_active_requests',
    'HTTP requests currently being handled.',
    ['method'],
)
JOB_SUBMISSIONS = Counter(
    'bio_agent_job_submissions_total',
    'Jobs submitted to the research Agent API.',
    ['tool'],
)
JOB_STATUS = Gauge(
    'bio_agent_job_status',
    'Latest observed job status, represented by one for the current status.',
    ['tool', 'status'],
)
JOB_TRANSITIONS = Counter(
    'bio_agent_job_transitions_total',
    'Job lifecycle transitions across all execution backends.',
    ['backend', 'tool', 'status'],
)
JOB_EXECUTIONS = Counter(
    'bio_agent_job_executions_total',
    'Completed job executions across all execution backends.',
    ['backend', 'tool', 'status'],
)
JOB_DURATION = Histogram(
    'bio_agent_job_duration_seconds',
    'Job execution duration across all execution backends.',
    ['backend', 'tool'],
)
JOB_QUEUE_DURATION = Histogram(
    'bio_agent_job_queue_duration_seconds',
    'Time jobs spend queued before execution.',
    ['backend', 'tool'],
)
JOB_ACTIVE = Gauge(
    'bio_agent_job_active',
    'Jobs currently executing.',
    ['backend', 'tool'],
)
TOOL_EXECUTIONS = Counter(
    'bio_agent_tool_executions_total',
    'Scientific tool calls by plugin domain and outcome.',
    ['domain', 'tool', 'status'],
)
TOOL_DURATION = Histogram(
    'bio_agent_tool_duration_seconds',
    'Scientific tool execution duration.',
    ['domain', 'tool'],
)
TOOL_ACTIVE = Gauge(
    'bio_agent_tool_active',
    'Scientific tool calls currently executing.',
    ['domain', 'tool'],
)
WORKFLOW_RUNS = Counter(
    'bio_agent_workflow_runs_total',
    'Workflow runs by outcome and mode.',
    ['status', 'dry_run'],
)
WORKFLOW_DURATION = Histogram(
    'bio_agent_workflow_duration_seconds',
    'Workflow run duration.',
    ['dry_run'],
)
WORKFLOW_ACTIVE = Gauge(
    'bio_agent_workflow_active',
    'Workflows currently executing.',
)
WORKFLOW_STEPS = Counter(
    'bio_agent_workflow_steps_total',
    'Workflow step executions by tool and outcome.',
    ['tool', 'status'],
)
WORKFLOW_STEP_DURATION = Histogram(
    'bio_agent_workflow_step_duration_seconds',
    'Workflow step execution duration.',
    ['tool'],
)
PLUGIN_HEALTH = Gauge(
    'bio_agent_plugin_health',
    'Latest plugin health status where one is healthy and zero is unhealthy.',
    ['domain'],
)
PLUGIN_SECURITY_DECISIONS = Counter(
    'bio_agent_plugin_security_decisions_total',
    'Plugin security policy decisions by capability and outcome.',
    ['domain', 'capability', 'decision'],
)
REDIS_QUEUE_DEPTH = Gauge(
    'bio_agent_redis_queue_depth',
    'Current Redis job queue depth.',
    ['namespace'],
)
REDIS_PROCESSING_DEPTH = Gauge(
    'bio_agent_redis_processing_depth',
    'Current Redis processing list depth.',
    ['namespace'],
)
REDIS_DEAD_LETTER_DEPTH = Gauge(
    'bio_agent_redis_dead_letter_depth',
    'Current Redis dead-letter queue depth.',
    ['namespace'],
)
REDIS_DEAD_LETTERS = Counter(
    'bio_agent_redis_dead_letters_total',
    'Jobs moved to the Redis dead-letter queue.',
    ['namespace', 'reason'],
)
REDIS_JOB_EXECUTIONS = Counter(
    'bio_agent_redis_job_executions_total',
    'Redis worker job executions by final status.',
    ['tool', 'status'],
)
REDIS_JOB_DURATION = Histogram(
    'bio_agent_redis_job_duration_seconds',
    'Redis worker job execution duration in seconds.',
    ['tool'],
)
REDIS_JOB_RETRIES = Counter(
    'bio_agent_redis_job_retries_total',
    'Redis worker job retry attempts.',
    ['tool'],
)
REDIS_RESULT_CACHE = Counter(
    'bio_agent_redis_result_cache_total',
    'Redis worker execution result cache outcomes.',
    ['tool', 'outcome'],
)
REDIS_WORKER_ACTIVE = Gauge(
    'bio_agent_redis_worker_active',
    'Number of jobs currently executing in this Redis worker process.',
    ['namespace'],
)
FILE_OPERATIONS = Counter(
    'bio_agent_file_operations_total',
    'File storage operations by backend and outcome.',
    ['backend', 'operation', 'outcome'],
)
FILE_UPLOAD_BYTES = Counter(
    'bio_agent_file_upload_bytes_total',
    'Total bytes accepted by the file storage layer.',
    ['backend'],
)
