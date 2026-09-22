"""Dispatch durable PostgreSQL outbox jobs into Redis queues."""

import argparse
import asyncio
import signal
from threading import Event
from time import monotonic, sleep

try:
    from .observability import configure_logging, log_event
    from .settings import PlatformSettings
except ImportError:
    from observability import configure_logging, log_event
    from settings import PlatformSettings


def _health_check(settings, asyncpg_module=None, redis_module=None):
    if asyncpg_module is None:
        import asyncpg as asyncpg_module
    if redis_module is None:
        import redis as redis_module

    async def check_database():
        connection = await asyncpg_module.connect(
            settings.database_url.replace(
                'postgresql+asyncpg://', 'postgresql://', 1
            ),
            timeout=settings.readiness_timeout_seconds,
        )
        try:
            await connection.fetchval('SELECT 1')
        finally:
            await connection.close()

    asyncio.run(check_database())
    client = redis_module.Redis.from_url(
        settings.redis_url,
        socket_timeout=settings.redis_socket_timeout,
        socket_connect_timeout=settings.redis_socket_timeout,
    )
    try:
        if not client.ping():
            raise RuntimeError('Redis is unavailable')
    finally:
        client.close()


def main(argv=None):
    settings = PlatformSettings.from_env().validate('dispatcher')
    parser = argparse.ArgumentParser(description='Dispatch durable research jobs')
    parser.add_argument('--redis-url', default=settings.redis_url)
    parser.add_argument('--namespace', default=settings.redis_namespace)
    parser.add_argument('--interval', type=float, default=2.0)
    parser.add_argument('--batch-size', type=int, default=1000)
    parser.add_argument('--lease-seconds', type=int, default=30)
    parser.add_argument('--reconcile-seconds', type=int, default=30)
    parser.add_argument('--claim-ticket-ttl-seconds', type=int, default=900)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    stop_event = Event()

    def request_stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    configure_logging('bio-research-agent-dispatcher')
    if args.check:
        _health_check(settings)
        return 0
    try:
        from .job_state_store import DatabaseDispatchSource
        from .redis_job_manager import RedisJobManager
    except ImportError:
        from job_state_store import DatabaseDispatchSource
        from redis_job_manager import RedisJobManager
    source = DatabaseDispatchSource(settings=settings)
    manager = RedisJobManager(
        redis_url=args.redis_url,
        namespace=args.namespace,
        settings=settings,
    )
    interval = max(float(args.interval), 0.1)
    batch_size = min(max(int(args.batch_size), 1), 10000)
    lease_seconds = max(int(args.lease_seconds), 1)
    reconcile_seconds = max(int(args.reconcile_seconds), 1)
    ticket_ttl_seconds = max(
        int(args.claim_ticket_ttl_seconds),
        lease_seconds,
    )
    try:
        log_event('dispatcher.started', namespace=args.namespace)
        next_dispatch = 0.0
        while not stop_event.is_set():
            now = monotonic()
            if now >= next_dispatch:
                claimed = source.claim_dispatchable(
                    limit=batch_size,
                    lease_seconds=lease_seconds,
                    claim_ticket_ttl_seconds=ticket_ttl_seconds,
                )
                outcomes = []
                rebuilt_count = 0
                for durable in claimed:
                    job_id = str(durable.get('job_id') or '')
                    generation = int(
                        durable.get('_dispatch_generation') or 0
                    )
                    try:
                        rebuilt = manager.rebuild_durable_queue(
                            limit=1,
                            loader=lambda limit, record=durable: [record],
                        )
                    except Exception as exc:
                        outcomes.append({
                            'job_id': job_id,
                            'generation': generation,
                            'succeeded': False,
                            'error': str(exc),
                        })
                        log_event(
                            'dispatcher.job_failed',
                            job_id=job_id,
                            error=str(exc),
                        )
                    else:
                        rebuilt_count += len(rebuilt)
                        outcomes.append({
                            'job_id': job_id,
                            'generation': generation,
                            'succeeded': True,
                        })
                if outcomes:
                    source.complete_claims(
                        outcomes,
                        reconcile_seconds=reconcile_seconds,
                        failure_delay_seconds=max(int(interval), 1),
                    )
                if rebuilt_count:
                    log_event('dispatcher.jobs_rebuilt', count=rebuilt_count)
                next_dispatch = now + interval
            sleep(min(interval, 0.5))
    except KeyboardInterrupt:
        return 0
    finally:
        manager.shutdown()
        log_event('dispatcher.stopped', namespace=args.namespace)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
