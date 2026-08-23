"""Append-only JSON audit events for security-sensitive API actions."""

from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock
from uuid import uuid4

try:
    from .observability import REQUEST_ID
except ImportError:
    from observability import REQUEST_ID


class AuditLogger:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = Lock()

    def record(self, principal, action, resource_type, resource_id=None, metadata=None):
        event = {
            'event_id': uuid4().hex,
            'at': datetime.now(timezone.utc).isoformat(),
            'request_id': REQUEST_ID.get(),
            'actor': principal.subject if principal else 'anonymous',
            'roles': list(principal.roles) if principal else [],
            'action': action,
            'resource_type': resource_type,
            'resource_id': resource_id,
            'metadata': metadata or {},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open('a', encoding='utf-8', newline='\n') as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + '\n')
                handle.flush()
        return event
