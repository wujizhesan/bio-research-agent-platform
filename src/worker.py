"""Run the Redis-backed research job worker."""

import argparse
import os
import signal
from threading import Event

try:
    from .job_execution import build_tool_executor_from_env
    from .job_state_store import DatabaseStateWriter
    from .redis_job_manager import RedisJobManager
    from .resource_scheduling import ResourceCapacity
    from .observability import configure_logging, log_event
except ImportError:
    from job_execution import build_tool_executor_from_env
    from job_state_store import DatabaseStateWriter
    from redis_job_manager import RedisJobManager
    from resource_scheduling import ResourceCapacity
    from observability import configure_logging, log_event


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the Bio Research Agent Redis worker')
    parser.add_argument('--redis-url', default=os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0'))
    parser.add_argument('--namespace', default=os.environ.get('REDIS_NAMESPACE', 'bioagent'))
    parser.add_argument('--metrics-host', default=os.environ.get('WORKER_METRICS_HOST', '0.0.0.0'))
    parser.add_argument('--metrics-port', type=int, default=int(os.environ.get('WORKER_METRICS_PORT', '9000')))
    args = parser.parse_args(argv)
    try:
        drain_timeout = max(float(os.environ.get('WORKER_DRAIN_TIMEOUT_SECONDS', '120')), 0.0)
    except ValueError:
        drain_timeout = 120.0
    stop_event = Event()

    def request_stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    configure_logging('bio-research-agent-worker')
    if args.metrics_port > 0:
        from prometheus_client import start_http_server
        start_http_server(args.metrics_port, addr=args.metrics_host)
    state_writer = DatabaseStateWriter()
    manager = None
    try:
        manager = RedisJobManager(
            redis_url=args.redis_url,
            namespace=args.namespace,
            state_store=state_writer,
            tool_executor=build_tool_executor_from_env(),
            resource_capacity=ResourceCapacity.from_env(),
            enforce_capacity=True,
        )
        log_event(
            'worker.started',
            worker_id=manager.worker_id,
            namespace=args.namespace,
            metrics_port=args.metrics_port,
            max_concurrency=manager.max_concurrency,
            max_attempts=manager.max_attempts,
        )
        drained = manager.run_forever(
            stop_event=stop_event,
            drain_timeout_seconds=drain_timeout,
        )
        if not drained:
            log_event(
                'worker.drain_timeout',
                worker_id=manager.worker_id,
                drain_timeout_seconds=drain_timeout,
            )
    except KeyboardInterrupt:
        return 0
    finally:
        if manager is not None:
            log_event(
                'worker.stopped',
                worker_id=manager.worker_id,
                namespace=args.namespace,
                drained=locals().get('drained'),
            )
            manager.shutdown()
        state_writer.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
