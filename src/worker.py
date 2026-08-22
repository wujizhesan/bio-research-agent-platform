"""Run the Redis-backed research job worker."""

import argparse
import os

try:
    from .redis_job_manager import RedisJobManager
except ImportError:
    from redis_job_manager import RedisJobManager


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the Bio Research Agent Redis worker')
    parser.add_argument('--redis-url', default=os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0'))
    parser.add_argument('--namespace', default=os.environ.get('REDIS_NAMESPACE', 'bioagent'))
    args = parser.parse_args(argv)
    manager = RedisJobManager(redis_url=args.redis_url, namespace=args.namespace)
    try:
        manager.run_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        manager.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
