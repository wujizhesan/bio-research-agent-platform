"""Queue phase durations for the first execution attempt of a durable job."""

from datetime import datetime


_PHASES = (
    ('submission', 'created_at', '_outbox_staged_at'),
    ('outbox_wait', '_outbox_staged_at', '_dispatch_claimed_at'),
    ('dispatch', '_dispatch_claimed_at', '_redis_enqueued_at'),
    ('worker_wait', '_redis_enqueued_at', 'started_at'),
)


def queue_phase_seconds(record):
    try:
        if int(record.get('_attempts') or 0) > 1:
            return None
        values = {
            key: datetime.fromisoformat(str(record[key]))
            for _, start, end in _PHASES
            for key in (start, end)
        }
        if any(value.utcoffset() is None for value in values.values()):
            return None
        durations = {
            phase: (values[end] - values[start]).total_seconds()
            for phase, start, end in _PHASES
        }
    except (KeyError, TypeError, ValueError):
        return None
    return durations if all(value >= 0 for value in durations.values()) else None
