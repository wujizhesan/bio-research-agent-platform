"""Durable append-only audit events for security-sensitive API actions."""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock
from uuid import uuid4

try:
    from .database import database_principal_scope
    from .observability import REQUEST_ID, TRACE_ID, log_event, sanitize
except ImportError:
    from database import database_principal_scope
    from observability import REQUEST_ID, TRACE_ID, log_event, sanitize


class AuditLogger:
    def __init__(self, path, database=None):
        self.path = Path(path)
        self.database = database
        self._lock = Lock()

    def _append_file(self, event):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open('a', encoding='utf-8', newline='\n') as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + '\n')
                handle.flush()

    async def record(
        self,
        principal,
        action,
        resource_type,
        resource_id=None,
        metadata=None,
    ):
        event = {
            'event_id': uuid4().hex,
            'at': datetime.now(timezone.utc).isoformat(),
            'request_id': REQUEST_ID.get(),
            'trace_id': TRACE_ID.get(),
            'actor': principal.subject if principal else 'anonymous',
            'roles': list(principal.roles) if principal else [],
            'action': action,
            'resource_type': resource_type,
            'resource_id': resource_id,
            'metadata': sanitize(metadata or {}),
        }
        if self.database is not None:
            with database_principal_scope(principal):
                await self.database.append_audit_event(event)
            try:
                await asyncio.to_thread(self._append_file, event)
            except OSError as exc:
                log_event(
                    'audit.compatibility_copy_failed',
                    error_type=type(exc).__name__,
                )
        else:
            await asyncio.to_thread(self._append_file, event)
        return event
