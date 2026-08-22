"""Prometheus metrics shared by the API service."""
from prometheus_client import Counter, Gauge, Histogram


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
    'Latest observed job status, represented as one for the current status.',
    ['tool', 'status'],
)
