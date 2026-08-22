"""Run the Redis-backed research job worker."""

import argparse
import os

try:
    from .job_state_store import DatabaseStateWriter
    from .redis_job_manager import RedisJobManager
except ImportError:
    from job_state_store import DatabaseStateWriter
    from redis_job_manager import RedisJobManager


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the Bio Research Agent Redis worker')
    parser.add_argument('--redis-url', default=os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0'))
    parser.add_argument('--namespace', default=os.environ.get('REDIS_NAMESPACE', 'bioagent'))
    parser.add_argument('--metrics-host', default=os.environ.get('WORKER_METRICS_HOST', '0.0.0.0'))
    parser.add_argument('--metrics-port', type=int, default=int(os.environ.get('WORKER_METRICS_PORT', '9000')))
    args = parser.parse_args(argv)
    if args.metrics_port > 0:
        from prometheus_client import start_http_server
        start_http_server(args.metrics_port, addr=args.metrics_host)
    state_writer = DatabaseStateWriter()
    manager = None
    try:
        manager = RedisJobManager(redis_url=args.redis_url, namespace=args.namespace, state_store=state_writer)
        manager.run_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        if manager is not None:
            manager.shutdown()
        state_writer.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
