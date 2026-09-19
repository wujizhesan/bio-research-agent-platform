"""Run the Redis-backed research job worker."""

import argparse
import signal
from threading import Event

try:
    from .job_execution import build_tool_executor_from_env
    from .job_state_store import DatabaseStateWriter
    from .redis_job_manager import RedisJobManager
    from .resource_scheduling import ResourceCapacity
    from .observability import configure_logging, log_event
    from .worker_health import WorkerHealthState, WorkerHttpServer
    from .settings import PlatformSettings
except ImportError:
    from job_execution import build_tool_executor_from_env
    from job_state_store import DatabaseStateWriter
    from redis_job_manager import RedisJobManager
    from resource_scheduling import ResourceCapacity
    from observability import configure_logging, log_event
    from worker_health import WorkerHealthState, WorkerHttpServer
    from settings import PlatformSettings


def main(argv=None):
    settings = PlatformSettings.from_env().validate('worker')
    parser = argparse.ArgumentParser(description='Run the Bio Research Agent Redis worker')
    parser.add_argument('--redis-url', default=settings.redis_url)
    parser.add_argument('--namespace', default=settings.redis_namespace)
    parser.add_argument('--metrics-host', default=settings.worker_metrics_host)
    parser.add_argument('--metrics-port', type=int, default=settings.worker_metrics_port)
    args = parser.parse_args(argv)
    drain_timeout = settings.worker_drain_timeout_seconds
    stop_event = Event()

    def request_stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    configure_logging('bio-research-agent-worker')
    state_writer = DatabaseStateWriter(settings=settings)
    manager = None
    health_server = None
    try:
        manager = RedisJobManager(
            redis_url=args.redis_url,
            namespace=args.namespace,
            state_store=state_writer,
            tool_executor=build_tool_executor_from_env(),
            resource_capacity=ResourceCapacity.from_env(),
            enforce_capacity=True,
            settings=settings,
        )
        health_state = WorkerHealthState(manager, state_writer, settings=settings)
        manager.health_state = health_state
        if args.metrics_port > 0:
            health_server = WorkerHttpServer(
                args.metrics_host,
                args.metrics_port,
                health_state,
            ).start()
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
        if health_server is not None:
            health_server.close()
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
