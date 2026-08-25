"""Prometheus metrics shared by the API service."""
from contextvars import ContextVar
import re
from uuid import uuid4

from prometheus_client import Counter, Gauge, Histogram


REQUEST_ID = ContextVar('bio_agent_request_id', default=None)
_REQUEST_ID_PATTERN = re.compile(r'^[A-Za-z0-9._:-]{1,128}$')


def request_id(value=None):
    candidate = value.strip() if isinstance(value, str) else ''
    return candidate if _REQUEST_ID_PATTERN.fullmatch(candidate) else uuid4().hex


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
