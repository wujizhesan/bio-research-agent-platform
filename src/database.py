"""Async relational persistence for the service layer."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import hmac
import json
import math
from pathlib import Path
import os
import re
from secrets import token_urlsafe
from time import time
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, Index, Integer, JSON, LargeBinary, String, Text, UniqueConstraint, and_, delete, event, func, inspect, or_, select, text
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TERMINAL_STATUSES = {'completed', 'failed', 'cancelled', 'indeterminate'}
SYSTEM_PROJECT_ID = 'system-legacy'
DATABASE_SUBJECT = ContextVar('bioagent_database_subject', default='')
DATABASE_IS_ADMIN = ContextVar('bioagent_database_is_admin', default=False)
DATABASE_WORKER_JOB_ID = ContextVar('bioagent_worker_job_id', default='')
DATABASE_WORKER_CAPABILITY = ContextVar('bioagent_worker_capability', default='')
DATABASE_WORKER_ID = ContextVar('bioagent_worker_id', default='')
DATABASE_WORKER_FENCING_TOKEN = ContextVar(
    'bioagent_worker_fencing_token', default=''
)
DATABASE_WORKER_ATTEMPT = ContextVar('bioagent_worker_attempt', default=0)


@lru_cache(maxsize=8)
def _tenant_context_file_secret(path, expected_digest):
    try:
        with open(path, 'rb') as handle:
            value = handle.read(65537)
    except OSError as exc:
        raise ValueError('unable to read RLS_CONTEXT_SIGNING_KEY_FILE') from exc
    if len(value) > 65536:
        raise ValueError('RLS_CONTEXT_SIGNING_KEY_FILE exceeds 65536 bytes')
    if expected_digest:
        if re.fullmatch(r'[0-9a-f]{64}', expected_digest) is None or not hmac.compare_digest(
            hashlib.sha256(value).hexdigest(), expected_digest,
        ):
            raise ValueError('RLS_CONTEXT_SIGNING_KEY_FILE checksum mismatch')
    return value.decode('utf-8').strip()


def _tenant_context_secret():
    direct = os.environ.get('RLS_CONTEXT_SIGNING_KEY', '').strip()
    file_name = os.environ.get('RLS_CONTEXT_SIGNING_KEY_FILE', '').strip()
    if direct and file_name:
        raise ValueError(
            'configure only one of RLS_CONTEXT_SIGNING_KEY or '
            'RLS_CONTEXT_SIGNING_KEY_FILE'
        )
    if direct:
        return direct
    if not file_name:
        return ''
    return _tenant_context_file_secret(
        os.path.abspath(file_name),
        os.environ.get('RLS_CONTEXT_SIGNING_KEY_SHA256', '').strip(),
    )


def _signed_tenant_context(subject, is_admin, backend_pid, ttl_seconds=60):
    secret = _tenant_context_secret()
    if not secret:
        return None
    if len(secret) < 32:
        raise ValueError('RLS_CONTEXT_SIGNING_KEY must be at least 32 characters')
    expires_at = int(time()) + max(min(int(ttl_seconds), 300), 10)
    nonce = token_urlsafe(24)
    admin_flag = '1' if is_admin else '0'
    subject_hash = hashlib.sha256(str(subject).encode('utf-8')).hexdigest()
    message = (
        f'{subject_hash}:{admin_flag}:{expires_at}:{nonce}:{int(backend_pid)}'
    )
    derived_key = hashlib.sha256(secret.encode('utf-8')).digest()
    signature = hmac.new(
        derived_key,
        message.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return {
        'key_id': os.environ.get('RLS_CONTEXT_KEY_ID', 'primary').strip()
        or 'primary',
        'subject': str(subject),
        'is_admin': admin_flag,
        'expires_at': str(expires_at),
        'nonce': nonce,
        'signature': signature,
    }


def set_database_principal(principal=None):
    DATABASE_SUBJECT.set(str(getattr(principal, 'subject', '') or ''))
    roles = tuple(getattr(principal, 'roles', ()) or ())
    DATABASE_IS_ADMIN.set('admin' in roles)


@contextmanager
def database_principal_scope(principal=None):
    subject_token = DATABASE_SUBJECT.set(
        str(getattr(principal, 'subject', '') or '')
    )
    roles = tuple(getattr(principal, 'roles', ()) or ())
    admin_token = DATABASE_IS_ADMIN.set('admin' in roles)
    try:
        yield
    finally:
        DATABASE_IS_ADMIN.reset(admin_token)
        DATABASE_SUBJECT.reset(subject_token)


@contextmanager
def database_worker_scope(job_id, capability, worker_id, fencing_token, attempt):
    values = (
        (DATABASE_WORKER_JOB_ID, str(job_id or '')),
        (DATABASE_WORKER_CAPABILITY, str(capability or '')),
        (DATABASE_WORKER_ID, str(worker_id or '')),
        (DATABASE_WORKER_FENCING_TOKEN, str(fencing_token or '')),
        (DATABASE_WORKER_ATTEMPT, int(attempt or 0)),
    )
    tokens = [(variable, variable.set(value)) for variable, value in values]
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


class TenantSession(Session):
    pass


@event.listens_for(TenantSession, 'after_begin')
def _apply_tenant_context(_session, _transaction, connection):
    if connection.dialect.name != 'postgresql':
        return
    subject = DATABASE_SUBJECT.get()
    is_admin = DATABASE_IS_ADMIN.get()
    connection.execute(
        text("SELECT set_config('bioagent.subject', :subject, true)"),
        {'subject': subject},
    )
    connection.execute(
        text("SELECT set_config('bioagent.is_admin', :is_admin, true)"),
        {'is_admin': 'true' if is_admin else 'false'},
    )
    backend_pid = connection.execute(text('SELECT pg_backend_pid()')).scalar_one()
    signed = _signed_tenant_context(subject, is_admin, backend_pid)
    for name, value in (
        ('bioagent.context_key_id', (signed or {}).get('key_id', '')),
        ('bioagent.context_subject', (signed or {}).get('subject', '')),
        ('bioagent.context_is_admin', (signed or {}).get('is_admin', '')),
        ('bioagent.context_expires_at', (signed or {}).get('expires_at', '')),
        ('bioagent.context_nonce', (signed or {}).get('nonce', '')),
        ('bioagent.context_signature', (signed or {}).get('signature', '')),
    ):
        connection.execute(
            text('SELECT set_config(:name, :value, true)'),
            {'name': name, 'value': value},
        )
    for name, value in (
        ('bioagent.worker_job_id', DATABASE_WORKER_JOB_ID.get()),
        ('bioagent.worker_capability', DATABASE_WORKER_CAPABILITY.get()),
        ('bioagent.worker_id', DATABASE_WORKER_ID.get()),
        ('bioagent.worker_fencing_token', DATABASE_WORKER_FENCING_TOKEN.get()),
        ('bioagent.worker_attempt', DATABASE_WORKER_ATTEMPT.get()),
    ):
        connection.execute(
            text('SELECT set_config(:name, :value, true)'),
            {'name': name, 'value': str(value)},
        )


def normalize_database_url(value=None):
    url = value or os.environ.get('DATABASE_URL') or 'sqlite+aiosqlite:///./output/bio-agent.db'
    if url.startswith('postgres://'):
        return url.replace('postgres://', 'postgresql+asyncpg://', 1)
    if url.startswith('postgresql://'):
        return url.replace('postgresql://', 'postgresql+asyncpg://', 1)
    if url.startswith('sqlite:///') and not url.startswith('sqlite+aiosqlite:///'):
        return url.replace('sqlite:///', 'sqlite+aiosqlite:///', 1)
    return url


class Base(DeclarativeBase):
    pass


class JobRow(Base):
    __tablename__ = 'job_records'

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey('projects.project_id', ondelete='RESTRICT'),
        nullable=False,
        index=True,
        default=SYSTEM_PROJECT_ID,
        server_default=SYSTEM_PROJECT_ID,
    )
    tool: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    started_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    arguments: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    artifacts: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_of: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    resources: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    run_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    execution_identity: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    routing: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    execution: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resolution: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ProjectRow(Base):
    __tablename__ = 'projects'

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_subject: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class ProjectMemberRow(Base):
    __tablename__ = 'project_members'

    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey('projects.project_id', ondelete='CASCADE'), primary_key=True,
    )
    subject: Mapped[str] = mapped_column(String(200), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default='viewer')
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class JobProjectRow(Base):
    __tablename__ = 'job_projects'

    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey('job_records.job_id', ondelete='CASCADE'), primary_key=True,
    )
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey('projects.project_id', ondelete='CASCADE'), nullable=False, index=True,
    )
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class FileProjectRow(Base):
    __tablename__ = 'file_projects'

    file_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey('projects.project_id', ondelete='CASCADE'), nullable=False, index=True,
    )
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class FileRecordRow(Base):
    __tablename__ = 'file_records'
    __table_args__ = (
        Index(
            'ix_file_records_recovery_scan',
            'status',
            'updated_at',
        ),
    )

    file_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey('projects.project_id', ondelete='RESTRICT'),
        nullable=False,
        index=True,
    )
    filename: Mapped[str | None] = mapped_column(String(180), nullable=True)
    storage_backend: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    version_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_reservation_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
    )
    recovery_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recovery_lease_until: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retention_until: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delete_request_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True,
    )
    delete_requested_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    delete_requested_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deleted_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delete_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delete_next_attempt_at: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class ProjectStorageUsageRow(Base):
    __tablename__ = 'project_storage_usage'

    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey('projects.project_id', ondelete='CASCADE'), primary_key=True,
    )
    quota_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class StorageReservationRow(Base):
    __tablename__ = 'storage_reservations'
    __table_args__ = (
        Index(
            'uq_storage_reservations_resource',
            'resource_kind',
            'resource_id',
            unique=True,
        ),
    )

    reservation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey('projects.project_id', ondelete='CASCADE'), nullable=False, index=True,
    )
    job_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey('job_records.job_id', ondelete='CASCADE'), nullable=True, index=True,
    )
    resource_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class JobOutboxRow(Base):
    __tablename__ = 'job_outbox'
    __table_args__ = (
        Index(
            'ix_job_outbox_dispatch_schedule',
            'next_attempt_at',
            'created_at',
        ),
    )

    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey('job_records.job_id', ondelete='CASCADE'), primary_key=True,
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    dispatched_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dispatch_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dispatch_lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_attempt_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    dispatch_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class JobEventRow(Base):
    __tablename__ = 'job_events'

    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey('job_records.job_id', ondelete='CASCADE'), primary_key=True,
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class JobExecutionResultRow(Base):
    __tablename__ = 'job_execution_results'

    execution_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey('job_records.job_id', ondelete='CASCADE'), nullable=False, index=True,
    )
    fencing_token: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_semantics: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class JobArtifactRow(Base):
    __tablename__ = 'job_artifacts'
    __table_args__ = (
        Index(
            'ix_job_artifacts_recovery_scan',
            'status',
            'updated_at',
            postgresql_where=text(
                "status IN ('reserved', 'uploaded', 'orphaned', 'retained', "
                "'reclaiming')"
            ),
            sqlite_where=text(
                "status IN ('reserved', 'uploaded', 'orphaned', 'retained', "
                "'reclaiming')"
            ),
        ),
    )

    publication_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey('job_records.job_id', ondelete='CASCADE'), nullable=False, index=True,
    )
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey('projects.project_id', ondelete='RESTRICT'), nullable=False, index=True,
    )
    execution_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    fencing_token: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    parameter: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    storage_backend: Mapped[str] = mapped_column(String(32), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    version_id: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    retention_until: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delete_request_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True,
    )
    delete_requested_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    delete_requested_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deleted_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delete_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delete_next_attempt_at: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_reservation_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
    )
    recovery_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recovery_lease_until: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class StorageDeletionEventRow(Base):
    __tablename__ = 'storage_deletion_events'
    __table_args__ = (
        UniqueConstraint(
            'project_id',
            'sequence',
            name='uq_storage_deletion_events_project_sequence',
        ),
    )

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class AuditEventRow(Base):
    __tablename__ = 'audit_events'

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    at: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    roles: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    metadata_json: Mapped[dict] = mapped_column('metadata', JSON, nullable=False, default=dict)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)


class JobWorkerCapabilityRow(Base):
    __tablename__ = 'job_worker_capabilities'

    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey('job_records.job_id', ondelete='CASCADE'), primary_key=True,
    )
    capability: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    claimed_worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fencing_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claim_ticket_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_ticket_expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    claim_ticket_redeemed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class JobIdempotencyRow(Base):
    __tablename__ = 'job_idempotency'

    idempotency_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    subject: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey('projects.project_id', ondelete='CASCADE'), nullable=False, index=True,
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey('job_records.job_id', ondelete='CASCADE'), nullable=False, unique=True,
    )
    created_at: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class AuthSubjectRow(Base):
    __tablename__ = 'auth_subjects'

    subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class TenantContextKeyRow(Base):
    __tablename__ = 'tenant_context_keys'

    key_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key_digest: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    valid_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class TenantContextControlRow(Base):
    __tablename__ = 'tenant_context_control'

    singleton: Mapped[bool] = mapped_column(Boolean, primary_key=True)
    enforce_signed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class AuthSessionRow(Base):
    __tablename__ = 'auth_sessions'

    jti: Mapped[str] = mapped_column(String(80), primary_key=True)
    subject: Mapped[str] = mapped_column(
        String(255),
        ForeignKey('auth_subjects.subject', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    token_version: Mapped[int] = mapped_column(Integer, nullable=False)
    roles_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[float] = mapped_column(Float, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


def _row_values(record):
    return {
        'job_id': record['job_id'],
        'project_id': str(record.get('project_id') or SYSTEM_PROJECT_ID),
        'tool': record['tool'],
        'status': record['status'],
        'created_at': record['created_at'],
        'started_at': record.get('started_at'),
        'finished_at': record.get('finished_at'),
        'arguments': record.get('_arguments', {}),
        'result': record.get('result'),
        'artifacts': record.get('artifacts'),
        'error': record.get('error'),
        'retry_of': record.get('retry_of'),
        'attempts': int(record.get('_attempts', 0)),
        'cancel_requested': bool(record.get('_cancel_requested')),
        'worker_id': record.get('_worker_id'),
        'lease_until': record.get('_lease_until'),
        'resources': record.get('resources', {}),
        'priority': int(record.get('priority', 0)),
        'trace_id': record.get('trace_id'),
        'request_id': record.get('request_id'),
        'run_context': record.get('run_context'),
        'execution_identity': record.get('execution_identity'),
        'routing': record.get('routing'),
        'execution': record.get('execution'),
        'resolution': record.get('resolution'),
    }


def _coalesced_values(records):
    values = {}
    for record in records:
        values[record['job_id']] = _row_values(record)
    return list(values.values())


def _event_values(record):
    revision = int(record.get('_revision', record.get('revision', 0)) or 0)
    values = _row_values(record)
    payload = {
        'job_id': values['job_id'],
        'tool': values['tool'],
        'status': values['status'],
        'created_at': values['created_at'],
        'attempts': values['attempts'],
        'resources': values['resources'],
        'priority': values['priority'],
        'project_id': values['project_id'],
        'revision': revision,
    }
    for key in (
        'started_at', 'finished_at', 'result', 'artifacts', 'error', 'retry_of',
        'trace_id', 'request_id', 'run_context', 'execution_identity',
        'routing', 'execution', 'resolution', 'scheduling',
    ):
        value = record.get(key) if key == 'scheduling' else values.get(key)
        if value is not None:
            payload[key] = value
    if values['cancel_requested']:
        payload['cancel_requested'] = True
    return {
        'job_id': values['job_id'],
        'revision': revision,
        'event_id': str(record.get('_event_id') or f'{revision}-0'),
        'status': values['status'],
        'payload': payload,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'terminal': values['status'] in TERMINAL_STATUSES,
    }


def _configured_int(name, default, minimum):
    try:
        return max(int(os.environ.get(name, default)), minimum)
    except (TypeError, ValueError):
        return default


def _configured_float(name, default, minimum):
    try:
        return max(float(os.environ.get(name, default)), minimum)
    except (TypeError, ValueError):
        return default


def _public_row(row):
    output = {
        'job_id': row.job_id,
        'project_id': row.project_id,
        'tool': row.tool,
        'status': row.status,
        'created_at': row.created_at,
    }
    for field in (
        'started_at', 'finished_at', 'result', 'artifacts', 'error', 'retry_of',
        'trace_id', 'request_id', 'run_context', 'execution_identity',
        'routing', 'execution', 'resolution',
    ):
        value = getattr(row, field)
        if value is not None:
            output[field] = value
    output['attempts'] = row.attempts
    output['resources'] = row.resources or {}
    output['priority'] = row.priority
    if row.cancel_requested:
        output['cancel_requested'] = True
    return output


def _public_artifact_row(row):
    output = {
        'publication_id': row.publication_id,
        'artifact_id': row.artifact_id,
        'job_id': row.job_id,
        'project_id': row.project_id,
        'execution_key': row.execution_key,
        'fencing_token': row.fencing_token,
        'attempt': row.attempt,
        'parameter': row.parameter,
        'kind': row.kind,
        'status': row.status,
        'storage_backend': row.storage_backend,
        'filename': row.filename,
        'created_at': row.created_at,
        'updated_at': row.updated_at,
    }
    for field in (
        'content_type', 'size_bytes', 'sha256', 'path', 'storage_key',
        'version_id', 'reference', 'retention_until', 'last_error',
        'recovery_token', 'recovery_lease_until', 'storage_reservation_id',
        'delete_request_id', 'delete_requested_by', 'delete_requested_at',
        'deleted_at', 'delete_next_attempt_at',
    ):
        value = getattr(row, field)
        if value is not None:
            output[field] = value
    output['revision'] = row.revision
    output['delete_attempts'] = row.delete_attempts
    return output


def _public_storage_deletion_event(row):
    return {
        'event_id': row.event_id,
        'sequence': row.sequence,
        'resource_type': row.resource_type,
        'resource_id': row.resource_id,
        'project_id': row.project_id,
        'job_id': row.job_id,
        'request_id': row.request_id,
        'actor': row.actor,
        'status': row.status,
        'attempt': row.attempt,
        'error': row.error,
        'previous_hash': row.previous_hash,
        'event_hash': row.event_hash,
        'created_at': row.created_at,
    }


def _public_audit_event(row):
    return {
        'event_id': row.event_id,
        'at': row.at,
        'request_id': row.request_id,
        'trace_id': row.trace_id,
        'actor': row.actor,
        'roles': list(row.roles or []),
        'action': row.action,
        'resource_type': row.resource_type,
        'resource_id': row.resource_id,
        'metadata': dict(row.metadata_json or {}),
        'event_hash': row.event_hash,
    }


def _dispatch_record(outbox, job):
    record = dict(outbox.payload or {})
    record.update({
        'job_id': job.job_id,
        'project_id': job.project_id,
        'tool': job.tool,
        'status': job.status,
        'created_at': job.created_at,
        '_attempts': job.attempts,
        '_cancel_requested': job.cancel_requested,
        'resources': job.resources or {},
        'priority': job.priority,
        '_dispatch_generation': int(outbox.dispatch_generation or 0),
    })
    if job.run_context is not None:
        record['run_context'] = job.run_context
    return record


def _public_project(row):
    return {
        'project_id': row.project_id,
        'name': row.name,
        'description': row.description,
        'owner_subject': row.owner_subject,
        'created_at': row.created_at,
    }


class Database:
    def __init__(self, url=None):
        self.url = normalize_database_url(url)
        if self.url.startswith('sqlite'):
            (PROJECT_ROOT / 'output').mkdir(parents=True, exist_ok=True)
        engine_options = {'pool_pre_ping': True}
        if self.url.startswith('postgresql'):
            engine_options.update({
                'pool_size': _configured_int('DB_POOL_SIZE', 10, 1),
                'max_overflow': _configured_int('DB_MAX_OVERFLOW', 10, 0),
                'pool_timeout': _configured_float('DB_POOL_TIMEOUT', 30.0, 1.0),
            })
        self.engine = create_async_engine(self.url, **engine_options)
        self.sessions = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            sync_session_class=TenantSession,
            expire_on_commit=False,
        )

    async def init_schema(self):
        auto_create = os.environ.get('AUTO_CREATE_SCHEMA', 'true').lower() in {'1', 'true', 'yes'}
        if auto_create:
            async with self.engine.begin() as connection:
                await connection.run_sync(self._create_and_upgrade_schema)

    @staticmethod
    def _create_and_upgrade_schema(connection):
        Base.metadata.create_all(connection)
        if connection.dialect.name == 'postgresql':
            connection.execute(text(
                "INSERT INTO projects "
                "(project_id, name, description, owner_subject, created_at) VALUES "
                "('system-legacy', 'Legacy system resources', "
                "'Resources created before mandatory tenant ownership', 'system', "
                "'1970-01-01T00:00:00+00:00') ON CONFLICT (project_id) DO NOTHING"
            ))
        else:
            connection.execute(text(
                "INSERT OR IGNORE INTO projects "
                "(project_id, name, description, owner_subject, created_at) VALUES "
                "('system-legacy', 'Legacy system resources', "
                "'Resources created before mandatory tenant ownership', 'system', "
                "'1970-01-01T00:00:00+00:00')"
            ))
        columns = {item['name'] for item in inspect(connection).get_columns('job_records')}
        missing = {
            'attempts': 'INTEGER NOT NULL DEFAULT 0',
            'worker_id': 'VARCHAR(128)',
            'lease_until': 'FLOAT',
            'resources': "JSON NOT NULL DEFAULT '{}'",
            'priority': 'INTEGER NOT NULL DEFAULT 0',
            'trace_id': 'VARCHAR(128)',
            'request_id': 'VARCHAR(128)',
            'run_context': 'JSON',
            'execution_identity': 'JSON',
            'routing': 'JSON',
            'execution': 'JSON',
            'resolution': 'JSON',
            'artifacts': 'JSON',
            'project_id': "VARCHAR(64) NOT NULL DEFAULT 'system-legacy'",
        }
        boolean_default = 'FALSE' if connection.dialect.name == 'postgresql' else '0'
        missing['cancel_requested'] = f'BOOLEAN NOT NULL DEFAULT {boolean_default}'
        for name, definition in missing.items():
            if name not in columns:
                connection.execute(text(f'ALTER TABLE job_records ADD COLUMN {name} {definition}'))
        connection.execute(text(
            "UPDATE job_records SET project_id = COALESCE("
            "(SELECT job_projects.project_id FROM job_projects "
            "WHERE job_projects.job_id = job_records.job_id), 'system-legacy') "
            "WHERE project_id IS NULL"
        ))
        connection.execute(text(
            "INSERT INTO file_records "
            "(file_id, project_id, storage_backend, status, created_at, updated_at) "
            "SELECT file_projects.file_id, file_projects.project_id, "
            "'unknown', 'active', file_projects.created_at, file_projects.created_at "
            "FROM file_projects LEFT JOIN file_records "
            "ON file_records.file_id = file_projects.file_id "
            "WHERE file_records.file_id IS NULL"
        ))
        indexes = {
            item['name'] for item in inspect(connection).get_indexes('job_records')
        }
        if 'ix_job_records_trace_id' not in indexes:
            connection.execute(text(
                'CREATE INDEX ix_job_records_trace_id ON job_records (trace_id)'
            ))
        if 'ix_job_records_project_id' not in indexes:
            connection.execute(text(
                'CREATE INDEX ix_job_records_project_id ON job_records (project_id)'
            ))
        outbox_columns = {
            item['name'] for item in inspect(connection).get_columns('job_outbox')
        }
        outbox_missing = {
            'dispatch_owner': 'VARCHAR(128)',
            'dispatch_lease_until': 'FLOAT',
            'next_attempt_at': 'FLOAT',
            'dispatch_generation': 'INTEGER NOT NULL DEFAULT 0',
        }
        for name, definition in outbox_missing.items():
            if name not in outbox_columns:
                connection.execute(text(
                    f'ALTER TABLE job_outbox ADD COLUMN {name} {definition}'
                ))
        outbox_indexes = {
            item['name'] for item in inspect(connection).get_indexes('job_outbox')
        }
        if 'ix_job_outbox_dispatch_schedule' not in outbox_indexes:
            connection.execute(text(
                'CREATE INDEX ix_job_outbox_dispatch_schedule '
                'ON job_outbox (next_attempt_at, created_at)'
            ))
        capability_columns = {
            item['name']
            for item in inspect(connection).get_columns('job_worker_capabilities')
        }
        capability_missing = {
            'claim_ticket_sha256': 'VARCHAR(64)',
            'claim_ticket_expires_at': 'FLOAT',
            'claim_ticket_redeemed_at': 'FLOAT',
        }
        for name, definition in capability_missing.items():
            if name not in capability_columns:
                connection.execute(text(
                    'ALTER TABLE job_worker_capabilities '
                    f'ADD COLUMN {name} {definition}'
                ))
        for table in ('file_records', 'job_artifacts'):
            lifecycle_columns = {
                item['name'] for item in inspect(connection).get_columns(table)
            }
            lifecycle_missing = {
                'delete_request_id': 'VARCHAR(128)',
                'delete_requested_by': 'VARCHAR(200)',
                'delete_requested_at': 'VARCHAR(64)',
                'deleted_at': 'VARCHAR(64)',
                'delete_attempts': 'INTEGER NOT NULL DEFAULT 0',
                'delete_next_attempt_at': 'VARCHAR(64)',
            }
            for name, definition in lifecycle_missing.items():
                if name not in lifecycle_columns:
                    connection.execute(text(
                        f'ALTER TABLE {table} ADD COLUMN {name} {definition}'
                    ))
            lifecycle_indexes = {
                item['name'] for item in inspect(connection).get_indexes(table)
            }
            index_name = f'ix_{table}_delete_request_id'
            if index_name not in lifecycle_indexes:
                connection.execute(text(
                    f'CREATE INDEX {index_name} ON {table} (delete_request_id)'
                ))
            retry_index_name = f'ix_{table}_delete_next_attempt_at'
            if retry_index_name not in lifecycle_indexes:
                connection.execute(text(
                    f'CREATE INDEX {retry_index_name} ON {table} '
                    '(delete_next_attempt_at)'
                ))

    async def ping(self):
        async with self.sessions() as session:
            await session.execute(text('SELECT 1'))
            if (
                self.url.startswith('postgresql')
                and os.environ.get('APP_ENV', '').strip().lower()
                in {'production', 'prod'}
                and os.environ.get('DATABASE_ROLE', '').strip().lower() == 'api'
            ):
                state = (await session.execute(text(
                    'SELECT bioagent_signed_context_enforced(), '
                    'bioagent_signed_context_valid()'
                ))).one()
                if not bool(state[0]) or not bool(state[1]):
                    raise RuntimeError('signed tenant context is not enforced')

    async def create_auth_session(self, subject, roles_sha256, ttl_seconds=3600):
        selected_subject = str(subject or '')
        selected_roles = str(roles_sha256 or '')
        ttl = max(min(int(ttl_seconds), 86400), 60)
        jti = token_urlsafe(32)
        async with self.sessions() as session:
            if self.url.startswith('postgresql'):
                created = await session.scalar(
                    text(
                        'SELECT bioagent_create_auth_session('
                        ':jti, :subject, :roles_sha256, :ttl)'
                    ),
                    {
                        'jti': jti,
                        'subject': selected_subject,
                        'roles_sha256': selected_roles,
                        'ttl': ttl,
                    },
                )
                await session.commit()
                if not created:
                    raise PermissionError('authentication subject is disabled')
                return dict(created)
            now = time()
            state = await session.get(AuthSubjectRow, selected_subject)
            if state is None:
                state = AuthSubjectRow(
                    subject=selected_subject,
                    token_version=0,
                    disabled=False,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                session.add(state)
                await session.flush()
            if state.disabled:
                raise PermissionError('authentication subject is disabled')
            row = AuthSessionRow(
                jti=jti,
                subject=selected_subject,
                token_version=int(state.token_version),
                roles_sha256=selected_roles,
                issued_at=now,
                expires_at=now + ttl,
                revoked_at=None,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            session.add(row)
            await session.commit()
            return {
                'jti': row.jti,
                'subject': row.subject,
                'token_version': row.token_version,
                'issued_at': int(row.issued_at),
                'expires_at': int(row.expires_at),
            }

    async def validate_auth_session(
        self,
        jti,
        subject,
        token_version,
        roles_sha256,
    ):
        async with self.sessions() as session:
            if self.url.startswith('postgresql'):
                return bool(await session.scalar(
                    text(
                        'SELECT bioagent_validate_auth_session('
                        ':jti, :subject, :token_version, :roles_sha256)'
                    ),
                    {
                        'jti': str(jti or ''),
                        'subject': str(subject or ''),
                        'token_version': int(token_version or 0),
                        'roles_sha256': str(roles_sha256 or ''),
                    },
                ))
            row = await session.get(AuthSessionRow, str(jti or ''))
            if row is None:
                return False
            state = await session.get(AuthSubjectRow, row.subject)
            return bool(
                state is not None
                and row.subject == str(subject or '')
                and row.token_version == int(token_version or 0)
                and row.roles_sha256 == str(roles_sha256 or '')
                and row.revoked_at is None
                and row.expires_at > time()
                and not state.disabled
                and state.token_version == row.token_version
            )

    async def revoke_auth_session(self, jti):
        async with self.sessions() as session:
            if self.url.startswith('postgresql'):
                revoked = await session.scalar(
                    text('SELECT bioagent_revoke_auth_session(:jti)'),
                    {'jti': str(jti or '')},
                )
                await session.commit()
                return bool(revoked)
            row = await session.get(AuthSessionRow, str(jti or ''))
            if row is None or row.revoked_at is not None:
                return False
            if (
                row.subject != DATABASE_SUBJECT.get()
                and not DATABASE_IS_ADMIN.get()
            ):
                return False
            row.revoked_at = time()
            await session.commit()
            return True

    async def revoke_subject_sessions(self, subject, disabled=False):
        if not DATABASE_IS_ADMIN.get():
            return False
        selected_subject = str(subject or '')
        async with self.sessions() as session:
            if self.url.startswith('postgresql'):
                revoked = await session.scalar(
                    text(
                        'SELECT bioagent_revoke_subject_sessions('
                        ':subject, :disabled)'
                    ),
                    {
                        'subject': selected_subject,
                        'disabled': bool(disabled),
                    },
                )
                await session.commit()
                return bool(revoked)
            state = await session.get(AuthSubjectRow, selected_subject)
            if state is None:
                return False
            state.token_version = int(state.token_version) + 1
            state.disabled = bool(disabled)
            state.updated_at = datetime.now(timezone.utc).isoformat()
            rows = (await session.execute(
                select(AuthSessionRow).where(
                    AuthSessionRow.subject == selected_subject,
                    AuthSessionRow.revoked_at.is_(None),
                )
            )).scalars().all()
            revoked_at = time()
            for row in rows:
                row.revoked_at = revoked_at
            await session.commit()
            return True

    async def upsert_job(self, record):
        await self.upsert_jobs([record])

    async def _persist_job_events(self, session, records):
        values_by_key = {}
        for record in records:
            values = _event_values(record)
            values_by_key[(values['job_id'], values['revision'])] = values
        if not values_by_key:
            return
        values_list = list(values_by_key.values())
        if self.url.startswith('postgresql'):
            statement = postgres_insert(JobEventRow).values(values_list)
            await session.execute(statement.on_conflict_do_nothing(
                index_elements=[JobEventRow.job_id, JobEventRow.revision],
            ))
            return
        for values in values_list:
            existing = await session.get(JobEventRow, {
                'job_id': values['job_id'],
                'revision': values['revision'],
            })
            if existing is None:
                session.add(JobEventRow(**values))

    async def _bind_job_project(
        self,
        session,
        job_id,
        project_id,
        created_at,
    ):
        project = await session.get(ProjectRow, str(project_id))
        if project is None:
            raise ValueError(f'project not found: {project_id}')
        job = await session.get(JobRow, str(job_id))
        if job is None:
            raise ValueError(f'job not found: {job_id}')
        ownership = await session.get(JobProjectRow, str(job_id))
        if job.project_id in {None, SYSTEM_PROJECT_ID} and ownership is None:
            job.project_id = str(project_id)
        elif job.project_id != str(project_id):
            raise ValueError('job already belongs to another project')
        if ownership is None:
            session.add(JobProjectRow(
                job_id=str(job_id),
                project_id=str(project_id),
                created_at=str(created_at),
            ))
        elif ownership.project_id != str(project_id):
            raise ValueError('job already belongs to another project')

    async def stage_job(
        self,
        record,
        project_id=None,
        ownership_created_at=None,
        idempotency_subject=None,
        idempotency_key=None,
        idempotency_payload_hash=None,
    ):
        values = _row_values(record)
        if project_id is not None:
            values['project_id'] = str(project_id)
        payload = dict(record)
        payload['project_id'] = values['project_id']
        if idempotency_key:
            existing = await self.get_idempotent_job(
                idempotency_key,
                idempotency_subject,
                values['project_id'],
                idempotency_payload_hash,
            )
            if existing is not None:
                return existing
        async with self.sessions() as session:
            row = await session.get(JobRow, values['job_id'])
            if row is None:
                row = JobRow(**values)
                session.add(row)
            elif row.status not in TERMINAL_STATUSES or values['status'] in TERMINAL_STATUSES:
                for key, value in values.items():
                    if key != 'project_id':
                        setattr(row, key, value)
            outbox = await session.get(JobOutboxRow, values['job_id'])
            if values['status'] in TERMINAL_STATUSES:
                if outbox is not None:
                    await session.delete(outbox)
            elif outbox is None:
                outbox = JobOutboxRow(
                    job_id=values['job_id'],
                    payload=payload,
                    created_at=values['created_at'],
                )
                session.add(outbox)
            else:
                outbox.payload = payload
                outbox.last_error = None
            if project_id is not None:
                await self._bind_job_project(
                    session,
                    values['job_id'],
                    project_id,
                    ownership_created_at or values['created_at'],
                )
            await self._persist_job_events(session, [payload])
            capability = str(record.get('_execution_key') or '')
            if capability:
                await session.flush()
                if self.url.startswith('postgresql'):
                    registered = await session.scalar(
                        text(
                            'SELECT bioagent_register_worker_capability('
                            ':job_id, :capability)'
                        ),
                        {
                            'job_id': values['job_id'],
                            'capability': capability,
                        },
                    )
                    if not registered:
                        raise PermissionError('worker capability registration denied')
                else:
                    worker_capability = await session.get(
                        JobWorkerCapabilityRow,
                        values['job_id'],
                    )
                    if worker_capability is None:
                        session.add(JobWorkerCapabilityRow(
                            job_id=values['job_id'],
                            capability=capability,
                            created_at=values['created_at'],
                            updated_at=values['created_at'],
                        ))
                    elif worker_capability.capability != capability:
                        raise PermissionError('worker capability registration denied')
            if idempotency_key:
                session.add(JobIdempotencyRow(
                    idempotency_key=str(idempotency_key),
                    subject=str(idempotency_subject or ''),
                    project_id=values['project_id'],
                    payload_hash=str(idempotency_payload_hash or ''),
                    job_id=values['job_id'],
                    created_at=ownership_created_at or values['created_at'],
                ))
            if outbox is not None and values['status'] not in TERMINAL_STATUSES:
                outbox.payload = {
                    **payload,
                    '_outbox_staged_at': datetime.now(timezone.utc).isoformat(),
                }
                if (
                    self.url.startswith('postgresql')
                    and (
                        outbox.next_attempt_at is None
                        or outbox.next_attempt_at <= time()
                    )
                ):
                    await session.execute(text(
                        "SELECT pg_notify('bioagent_dispatch_outbox', '')"
                    ))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if not idempotency_key:
                    raise
                existing = await self.get_idempotent_job(
                    idempotency_key,
                    idempotency_subject,
                    values['project_id'],
                    idempotency_payload_hash,
                )
                if existing is None:
                    raise
                return existing
        return {'job_id': values['job_id'], 'deduplicated': False}

    async def get_idempotent_job(
        self,
        idempotency_key,
        subject,
        project_id,
        payload_hash,
    ):
        if not idempotency_key:
            return None
        async with self.sessions() as session:
            row = await session.get(JobIdempotencyRow, str(idempotency_key))
            if row is None:
                return None
            if row.subject != str(subject or '') or row.project_id != str(project_id):
                raise ValueError('idempotency key scope does not match the original request')
            if row.payload_hash != str(payload_hash or ''):
                raise ValueError('idempotency key already used with different job payload')
            return {'job_id': row.job_id, 'deduplicated': True}

    async def upsert_job_with_project(
        self,
        record,
        project_id,
        ownership_created_at=None,
    ):
        values = _row_values(record)
        values['project_id'] = str(project_id)
        async with self.sessions() as session:
            row = await session.get(JobRow, values['job_id'])
            if row is None:
                row = JobRow(**values)
                session.add(row)
            elif row.status not in TERMINAL_STATUSES or values['status'] in TERMINAL_STATUSES:
                if row.project_id != str(project_id):
                    raise ValueError('job already belongs to another project')
                for key, value in values.items():
                    if key != 'project_id':
                        setattr(row, key, value)
            await self._bind_job_project(
                session,
                values['job_id'],
                project_id,
                ownership_created_at or values['created_at'],
            )
            await self._persist_job_events(
                session,
                [{**record, 'project_id': str(project_id)}],
            )
            if values['status'] in TERMINAL_STATUSES:
                await session.execute(
                    delete(JobOutboxRow).where(
                        JobOutboxRow.job_id == values['job_id']
                    )
                )
            await session.commit()

    async def mark_job_dispatched(self, job_id, dispatched_at):
        async with self.sessions() as session:
            row = await session.get(JobOutboxRow, str(job_id))
            if row is None:
                return False
            row.dispatched_at = str(dispatched_at)
            row.dispatch_attempts = int(row.dispatch_attempts or 0) + 1
            row.last_error = None
            await session.commit()
            return True

    async def mark_job_dispatch_failed(self, job_id, error):
        async with self.sessions() as session:
            row = await session.get(JobOutboxRow, str(job_id))
            if row is None:
                return False
            row.dispatch_attempts = int(row.dispatch_attempts or 0) + 1
            row.last_error = str(error)[:2000]
            await session.commit()
            return True

    async def list_dispatchable_jobs(self, limit=1000):
        size = min(max(int(limit), 1), 10000)
        statement = (
            select(JobOutboxRow, JobRow)
            .join(JobRow, JobRow.job_id == JobOutboxRow.job_id)
            .where(
                ~JobRow.status.in_(TERMINAL_STATUSES),
                or_(
                    JobOutboxRow.next_attempt_at.is_(None),
                    JobOutboxRow.next_attempt_at <= time(),
                ),
            )
            .order_by(JobOutboxRow.created_at)
            .limit(size)
        )
        async with self.sessions() as session:
            rows = (await session.execute(statement)).all()
            return [_dispatch_record(outbox, job) for outbox, job in rows]

    async def list_worker_dispatchable_jobs(self, limit=1000):
        size = min(max(int(limit), 1), 10000)
        if not self.url.startswith('postgresql'):
            return await self.list_dispatchable_jobs(size)
        async with self.sessions() as session:
            rows = await session.execute(
                text(
                    'SELECT bioagent_list_worker_dispatchable_jobs(:limit)'
                ),
                {'limit': size},
            )
            return [dict(row[0] or {}) for row in rows]

    async def list_dispatcher_jobs(self, limit=1000):
        size = min(max(int(limit), 1), 10000)
        if not self.url.startswith('postgresql'):
            return await self.list_dispatchable_jobs(size)
        async with self.sessions() as session:
            rows = await session.execute(
                text('SELECT bioagent_list_worker_dispatchable_jobs(:limit)'),
                {'limit': size},
            )
            return [dict(row[0] or {}) for row in rows]

    async def claim_dispatch_batch(
        self,
        dispatcher_id,
        limit=1000,
        lease_seconds=30,
        claim_ticket_ttl_seconds=900,
    ):
        size = min(max(int(limit), 1), 10000)
        lease_seconds = max(int(lease_seconds), 1)
        ticket_ttl = max(int(claim_ticket_ttl_seconds), lease_seconds)
        selected_dispatcher = str(dispatcher_id or '')
        if not selected_dispatcher:
            raise ValueError('dispatcher_id is required')
        async with self.sessions() as session:
            if self.url.startswith('postgresql'):
                rows = await session.execute(
                    text(
                        'SELECT bioagent_claim_dispatch_batch('
                        ':limit, :dispatcher_id, :lease_seconds)'
                    ),
                    {
                        'limit': size,
                        'dispatcher_id': selected_dispatcher,
                        'lease_seconds': lease_seconds,
                    },
                )
                records = [dict(row[0] or {}) for row in rows]
                for record in records:
                    ticket = token_urlsafe(32)
                    issued = await session.scalar(
                        text(
                            'SELECT bioagent_issue_worker_claim_ticket('
                            ':job_id, :dispatcher_id, :generation, '
                            ':ticket_sha256, :ttl_seconds)'
                        ),
                        {
                            'job_id': str(record['job_id']),
                            'dispatcher_id': selected_dispatcher,
                            'generation': int(record['_dispatch_generation']),
                            'ticket_sha256': hashlib.sha256(
                                ticket.encode('utf-8')
                            ).hexdigest(),
                            'ttl_seconds': ticket_ttl,
                        },
                    )
                    if not issued:
                        raise PermissionError('dispatcher claim ticket issuance denied')
                    record['_claim_ticket'] = ticket
                await session.commit()
                claimed_at = datetime.now(timezone.utc).isoformat()
                for record in records:
                    record['_dispatch_claimed_at'] = claimed_at
                return records

            current_time = time()
            statement = (
                select(JobOutboxRow, JobRow)
                .join(JobRow, JobRow.job_id == JobOutboxRow.job_id)
                .where(
                    ~JobRow.status.in_(TERMINAL_STATUSES),
                    or_(
                        JobOutboxRow.dispatch_lease_until.is_(None),
                        JobOutboxRow.dispatch_lease_until <= current_time,
                    ),
                    or_(
                        JobOutboxRow.next_attempt_at.is_(None),
                        JobOutboxRow.next_attempt_at <= current_time,
                    ),
                )
                .order_by(
                    JobOutboxRow.next_attempt_at.asc().nullsfirst(),
                    JobOutboxRow.created_at,
                    JobOutboxRow.job_id,
                )
                .limit(size)
                .with_for_update(skip_locked=True)
            )
            rows = (await session.execute(statement)).all()
            records = []
            for outbox, job in rows:
                outbox.dispatch_owner = selected_dispatcher
                outbox.dispatch_lease_until = current_time + lease_seconds
                outbox.dispatch_generation = int(outbox.dispatch_generation or 0) + 1
                capability = await session.get(JobWorkerCapabilityRow, job.job_id)
                if capability is None:
                    raise RuntimeError('dispatchable job has no worker capability')
                ticket = token_urlsafe(32)
                capability.claim_ticket_sha256 = hashlib.sha256(
                    ticket.encode('utf-8')
                ).hexdigest()
                capability.claim_ticket_expires_at = current_time + ticket_ttl
                capability.claim_ticket_redeemed_at = None
                capability.updated_at = datetime.now(timezone.utc).isoformat()
                record = _dispatch_record(outbox, job)
                record['_claim_ticket'] = ticket
                records.append(record)
            await session.commit()
            claimed_at = datetime.now(timezone.utc).isoformat()
            for record in records:
                record['_dispatch_claimed_at'] = claimed_at
            return records

    async def complete_dispatch_claims(
        self,
        dispatcher_id,
        outcomes,
        reconcile_seconds=30,
        failure_delay_seconds=2,
    ):
        selected_dispatcher = str(dispatcher_id or '')
        if not selected_dispatcher:
            raise ValueError('dispatcher_id is required')
        completed = []
        async with self.sessions() as session:
            for outcome in outcomes:
                job_id = str(outcome['job_id'])
                generation = int(outcome['generation'])
                succeeded = bool(outcome.get('succeeded'))
                delay = (
                    max(float(reconcile_seconds), 0)
                    if succeeded
                    else max(float(failure_delay_seconds), 0)
                )
                error = str(outcome.get('error') or '')[:2000]
                if self.url.startswith('postgresql'):
                    accepted = await session.scalar(
                        text(
                            'SELECT bioagent_complete_dispatch_claim('
                            ':job_id, :dispatcher_id, :generation, '
                            ':succeeded, :error, :delay)'
                        ),
                        {
                            'job_id': job_id,
                            'dispatcher_id': selected_dispatcher,
                            'generation': generation,
                            'succeeded': succeeded,
                            'error': error,
                            'delay': delay,
                        },
                    )
                else:
                    row = await session.get(JobOutboxRow, job_id)
                    accepted = bool(
                        row is not None
                        and row.dispatch_owner == selected_dispatcher
                        and int(row.dispatch_generation or 0) == generation
                    )
                    if accepted:
                        row.dispatch_owner = None
                        row.dispatch_lease_until = None
                        row.next_attempt_at = time() + delay
                        row.dispatch_attempts = int(row.dispatch_attempts or 0) + 1
                        if succeeded:
                            row.dispatched_at = datetime.now(timezone.utc).isoformat()
                            row.last_error = None
                        else:
                            row.last_error = error or 'dispatcher failure'
                if accepted:
                    completed.append(job_id)
            await session.commit()
        return completed

    async def claim_worker_job(
        self,
        job_id,
        capability,
        worker_id,
        claim_ticket,
        lease_seconds,
    ):
        if not self.url.startswith('postgresql'):
            current_time = time()
            ticket_hash = hashlib.sha256(
                str(claim_ticket or '').encode('utf-8')
            ).hexdigest()
            async with self.sessions() as session:
                capability_row = await session.get(
                    JobWorkerCapabilityRow,
                    str(job_id),
                    with_for_update=True,
                )
                job = await session.get(JobRow, str(job_id), with_for_update=True)
                outbox = await session.get(JobOutboxRow, str(job_id))
                valid = bool(
                    capability_row is not None
                    and job is not None
                    and outbox is not None
                    and float(
                        (outbox.payload or {}).get('_retry_not_before') or 0
                    ) <= current_time
                    and not job.cancel_requested
                    and capability_row.capability == str(capability)
                    and capability_row.claim_ticket_sha256 == ticket_hash
                    and capability_row.claim_ticket_redeemed_at is None
                    and float(capability_row.claim_ticket_expires_at or 0) > current_time
                    and (
                        job.status == 'queued'
                        or (
                            job.status == 'running'
                            and (
                                job.lease_until is None
                                or float(job.lease_until) <= current_time
                            )
                        )
                    )
                )
                if not valid:
                    return None
                attempt = int(capability_row.attempt or 0) + 1
                capability_row.claimed_worker_id = str(worker_id)
                capability_row.attempt = attempt
                capability_row.fencing_token = str(attempt)
                capability_row.claim_ticket_redeemed_at = current_time
                capability_row.updated_at = datetime.now(timezone.utc).isoformat()
                job.status = 'running'
                job.worker_id = str(worker_id)
                job.lease_until = current_time + max(int(lease_seconds), 1)
                await session.commit()
                return {
                    'job_id': str(job_id),
                    'worker_id': str(worker_id),
                    'attempt': attempt,
                    'fencing_token': str(attempt),
                }
        ticket_hash = hashlib.sha256(
            str(claim_ticket or '').encode('utf-8')
        ).hexdigest()
        async with self.sessions() as session:
            claim = await session.scalar(
                text(
                    'SELECT bioagent_claim_worker_job('
                    ':job_id, :capability, :worker_id, '
                    ':ticket_sha256, :lease_seconds)'
                ),
                {
                    'job_id': str(job_id),
                    'capability': str(capability),
                    'worker_id': str(worker_id),
                    'ticket_sha256': ticket_hash,
                    'lease_seconds': max(int(lease_seconds), 1),
                },
            )
            await session.commit()
            return dict(claim) if claim else None

    async def defer_pure_job(
        self,
        execution_key,
        job_id,
        fencing_token,
        attempt,
        worker_id,
        delay_seconds,
        max_attempts,
        record_revision=0,
    ):
        delay = float(delay_seconds)
        if not math.isfinite(delay) or not 0 < delay <= 604800:
            raise ValueError('external retry delay must be within seven days')
        attempt = int(attempt)
        max_attempts = int(max_attempts)
        if attempt < 1 or max_attempts < 2:
            return None
        async with self.sessions() as session:
            if self.url.startswith('postgresql'):
                deferred = await session.scalar(
                    text(
                        'SELECT bioagent_defer_pure_job('
                        ':job_id, :execution_key, :worker_id, :fencing_token, '
                        ':attempt, :delay_seconds, :max_attempts, :record_revision)'
                    ),
                    {
                        'job_id': str(job_id),
                        'execution_key': str(execution_key),
                        'worker_id': str(worker_id),
                        'fencing_token': str(fencing_token),
                        'attempt': attempt,
                        'delay_seconds': delay,
                        'max_attempts': max_attempts,
                        'record_revision': max(int(record_revision), 0),
                    },
                )
                await session.commit()
                return dict(deferred) if deferred else None

            claim = await session.get(
                JobWorkerCapabilityRow, str(job_id), with_for_update=True
            )
            job = await session.get(JobRow, str(job_id), with_for_update=True)
            outbox = await session.get(JobOutboxRow, str(job_id), with_for_update=True)
            if not all((claim, job, outbox)):
                return None
            if (
                job.status != 'running'
                or job.cancel_requested
                or job.worker_id != str(worker_id)
                or str(outbox.payload.get('execution_semantics')) != 'pure'
                or claim.capability != str(execution_key)
                or claim.claimed_worker_id != str(worker_id)
                or claim.fencing_token != str(fencing_token)
                or int(claim.attempt or 0) != attempt
                or attempt >= max_attempts
            ):
                return None
            retry_at = time() + delay
            revision = await session.scalar(
                select(func.max(JobEventRow.revision)).where(
                    JobEventRow.job_id == str(job_id)
                )
            )
            revision = max(int(revision or 0), int(record_revision or 0)) + 1
            job.status = 'queued'
            job.started_at = None
            job.worker_id = None
            job.lease_until = None
            job.attempts = attempt
            outbox.next_attempt_at = retry_at
            outbox.dispatch_owner = None
            outbox.dispatch_lease_until = None
            outbox.dispatch_generation = int(outbox.dispatch_generation or 0) + 1
            outbox.payload = {
                **outbox.payload,
                '_retry_not_before': retry_at,
                '_deferred_attempt': attempt,
                '_revision': revision,
                'scheduling': {
                    'status': 'waiting_for_external_service',
                    'retry_at': retry_at,
                },
            }
            claim.claimed_worker_id = None
            claim.fencing_token = None
            claim.claim_ticket_sha256 = None
            claim.claim_ticket_expires_at = None
            claim.claim_ticket_redeemed_at = None
            claim.updated_at = datetime.now(timezone.utc).isoformat()
            execution = await session.get(
                JobExecutionResultRow, str(execution_key), with_for_update=True
            )
            if (
                execution is not None
                and execution.job_id == str(job_id)
                and execution.fencing_token == str(fencing_token)
                and execution.status == 'running'
            ):
                execution.status = 'deferred'
                execution.updated_at = claim.updated_at
            event_record = _public_row(job)
            event_record['scheduling'] = outbox.payload['scheduling']
            event_record['_revision'] = revision
            await self._persist_job_events(session, [event_record])
            await session.commit()
            return {'job_id': str(job_id), 'retry_at': retry_at, 'revision': revision}

    async def upsert_worker_job(
        self,
        record,
        capability,
        worker_id,
        fencing_token,
        attempt,
    ):
        if not self.url.startswith('postgresql'):
            values = _row_values(record)
            async with self.sessions() as session:
                claim = await session.get(
                    JobWorkerCapabilityRow,
                    values['job_id'],
                    with_for_update=True,
                )
                row = await session.get(
                    JobRow, values['job_id'], with_for_update=True
                )
                if (
                    claim is None
                    or row is None
                    or claim.capability != str(capability)
                    or claim.claimed_worker_id != str(worker_id)
                    or claim.fencing_token != str(fencing_token)
                    or int(claim.attempt or 0) != int(attempt)
                ):
                    raise PermissionError('worker claim is invalid or stale')
                if row.status in TERMINAL_STATUSES and values['status'] not in TERMINAL_STATUSES:
                    return
                for key in (
                    'status', 'started_at', 'finished_at', 'result', 'artifacts',
                    'error', 'attempts', 'worker_id',
                    'lease_until', 'execution',
                ):
                    setattr(row, key, values[key])
                row.cancel_requested = bool(
                    row.cancel_requested or values['cancel_requested']
                )
                await self._persist_job_events(
                    session,
                    [{**record, 'project_id': row.project_id}],
                )
                if values['status'] in TERMINAL_STATUSES:
                    await session.execute(delete(JobOutboxRow).where(
                        JobOutboxRow.job_id == values['job_id']
                    ))
                await session.commit()
            return
        values = _row_values(record)
        async with self.sessions() as session:
            claimed = await session.scalar(
                text(
                    'SELECT bioagent_bind_worker_claim('
                    ':job_id, :capability, :worker_id, :fencing_token, :attempt)'
                ),
                {
                    'job_id': values['job_id'],
                    'capability': str(capability),
                    'worker_id': str(worker_id),
                    'fencing_token': str(fencing_token),
                    'attempt': int(attempt),
                },
            )
            if not claimed:
                raise PermissionError('worker claim is invalid or stale')
            row = await session.get(JobRow, values['job_id'], with_for_update=True)
            if row is None:
                raise RuntimeError('worker job claim does not reference a durable job')
            if row.status in TERMINAL_STATUSES and values['status'] not in TERMINAL_STATUSES:
                await session.rollback()
                return
            for key in (
                'status', 'started_at', 'finished_at', 'result', 'artifacts', 'error',
                'attempts', 'worker_id', 'lease_until',
                'execution',
            ):
                setattr(row, key, values[key])
            row.cancel_requested = bool(
                row.cancel_requested or values['cancel_requested']
            )
            await self._persist_job_events(
                session,
                [{**record, 'project_id': row.project_id}],
            )
            if values['status'] in TERMINAL_STATUSES:
                await session.execute(
                    delete(JobOutboxRow).where(
                        JobOutboxRow.job_id == values['job_id']
                    )
                )
            await session.commit()

    async def upsert_jobs(self, records):
        records = [dict(record) for record in records]
        if not records:
            return
        if not self.url.startswith('postgresql'):
            for record in records:
                await self._upsert_sqlite_values(record, _row_values(record))
            return
        async with self.sessions() as session:
            missing_project_ids = [
                record['job_id']
                for record in records
                if not record.get('project_id')
            ]
            if missing_project_ids:
                existing_projects = dict((await session.execute(
                    select(JobRow.job_id, JobRow.project_id).where(
                        JobRow.job_id.in_(missing_project_ids)
                    )
                )).all())
                records = [
                    {
                        **record,
                        'project_id': existing_projects.get(record['job_id']),
                    }
                    if not record.get('project_id') else record
                    for record in records
                ]
            values_list = _coalesced_values(records)
            statement = postgres_insert(JobRow).values(values_list)
            updates = {
                key: getattr(statement.excluded, key)
                for key in values_list[0]
                if key not in {'job_id', 'project_id'}
            }
            statement = statement.on_conflict_do_update(
                index_elements=[JobRow.job_id],
                set_=updates,
                where=or_(
                    ~JobRow.status.in_(TERMINAL_STATUSES),
                    statement.excluded.status.in_(TERMINAL_STATUSES),
                ),
            )
            await session.execute(statement)
            await self._persist_job_events(session, records)
            terminal_ids = [
                values['job_id']
                for values in values_list
                if values['status'] in TERMINAL_STATUSES
            ]
            if terminal_ids:
                await session.execute(
                    delete(JobOutboxRow).where(JobOutboxRow.job_id.in_(terminal_ids))
                )
            await session.commit()

    async def _upsert_sqlite_values(self, record, values):
        async with self.sessions() as session:
            row = await session.get(JobRow, values['job_id'])
            if row is None:
                try:
                    session.add(JobRow(**values))
                    await self._persist_job_events(session, [record])
                    await session.commit()
                    return
                except IntegrityError:
                    await session.rollback()
                    row = await session.get(JobRow, values['job_id'])
                    if row is None:
                        raise
            if row.status in TERMINAL_STATUSES and values['status'] not in TERMINAL_STATUSES:
                return
            for key, value in values.items():
                if key != 'project_id':
                    setattr(row, key, value)
            await self._persist_job_events(
                session,
                [{**record, 'project_id': row.project_id}],
            )
            if values['status'] in TERMINAL_STATUSES:
                await session.execute(
                    delete(JobOutboxRow).where(JobOutboxRow.job_id == values['job_id'])
                )
            await session.commit()

    async def list_job_events(self, job_id, after_event_id='0-0', limit=100):
        size = min(max(int(limit), 1), 1000)
        selected_job_id = str(job_id)
        cursor = str(after_event_id or '0-0')
        replay_gap = False
        async with self.sessions() as session:
            if cursor == '0-0':
                statement = (
                    select(JobEventRow)
                    .where(JobEventRow.job_id == selected_job_id)
                    .order_by(JobEventRow.revision)
                    .limit(size)
                )
                rows = (await session.execute(statement)).scalars().all()
            elif cursor.startswith('r-') and cursor[2:].isdigit():
                cursor_revision = int(cursor[2:])
                if cursor_revision > 2147483647:
                    rows = []
                else:
                    statement = (
                        select(JobEventRow)
                        .where(
                            JobEventRow.job_id == selected_job_id,
                            JobEventRow.revision > cursor_revision,
                        )
                        .order_by(JobEventRow.revision)
                        .limit(size)
                    )
                    rows = (await session.execute(statement)).scalars().all()
                if not rows:
                    latest_revision = await session.scalar(
                        select(func.max(JobEventRow.revision)).where(
                            JobEventRow.job_id == selected_job_id
                        )
                    )
                    if latest_revision is not None and cursor_revision > latest_revision:
                        replay_gap = True
                        statement = (
                            select(JobEventRow)
                            .where(JobEventRow.job_id == selected_job_id)
                            .order_by(JobEventRow.revision.desc())
                            .limit(1)
                        )
                        rows = (await session.execute(statement)).scalars().all()
            else:
                cursor_revision = await session.scalar(
                    select(JobEventRow.revision).where(
                        JobEventRow.job_id == selected_job_id,
                        JobEventRow.event_id == cursor,
                    )
                )
                if cursor_revision is None:
                    replay_gap = True
                    statement = (
                        select(JobEventRow)
                        .where(JobEventRow.job_id == selected_job_id)
                        .order_by(JobEventRow.revision.desc())
                        .limit(1)
                    )
                else:
                    statement = (
                        select(JobEventRow)
                        .where(
                            JobEventRow.job_id == selected_job_id,
                            JobEventRow.revision > cursor_revision,
                        )
                        .order_by(JobEventRow.revision)
                        .limit(size)
                    )
                rows = (await session.execute(statement)).scalars().all()
        return [
            {
                'event_id': row.event_id,
                'revision': row.revision,
                'status': row.status,
                'created_at': row.created_at,
                'terminal': row.terminal,
                'job': dict(row.payload or {}),
                **({'replay_gap': True} if replay_gap else {}),
            }
            for row in rows
        ]

    async def get_execution_result(self, execution_key):
        async with self.sessions() as session:
            row = await session.get(JobExecutionResultRow, str(execution_key))
            if row is None:
                return None
            return {
                'execution_key': row.execution_key,
                'job_id': row.job_id,
                'fencing_token': row.fencing_token,
                'attempt': row.attempt,
                'execution_semantics': row.execution_semantics,
                'status': row.status,
                'result': row.result,
                'result_sha256': row.result_sha256,
                'created_at': row.created_at,
                'updated_at': row.updated_at,
            }

    async def begin_execution_attempt(
        self,
        execution_key,
        job_id,
        fencing_token,
        attempt,
        semantics='pure',
    ):
        now = datetime.now(timezone.utc).isoformat()
        async with self.sessions() as session:
            row = await session.get(
                JobExecutionResultRow,
                str(execution_key),
                with_for_update=True,
            )
            if row is None:
                row = JobExecutionResultRow(
                    execution_key=str(execution_key),
                    job_id=str(job_id),
                    fencing_token=str(fencing_token),
                    attempt=int(attempt),
                    execution_semantics=str(semantics),
                    status='running',
                    result=None,
                    result_sha256=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            elif row.job_id != str(job_id):
                raise RuntimeError('execution key belongs to another job')
            elif row.execution_semantics != str(semantics):
                raise RuntimeError('execution semantics changed for an existing attempt')
            elif row.status != 'completed' and int(attempt) > row.attempt:
                if row.execution_semantics == 'side_effecting':
                    row.status = 'indeterminate'
                    row.updated_at = now
                else:
                    row.fencing_token = str(fencing_token)
                    row.attempt = int(attempt)
                    row.status = 'running'
                    row.result = None
                    row.result_sha256 = None
                    row.updated_at = now
            elif row.status != 'completed' and row.fencing_token != str(fencing_token):
                raise RuntimeError('execution attempt is stale')
            await session.commit()
        return await self.get_execution_result(execution_key)

    async def store_execution_result(self, execution_key, job_id, result, fencing_token=None):
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            default=str,
        ).encode('utf-8')
        values = {
            'execution_key': str(execution_key),
            'job_id': str(job_id),
            'fencing_token': str(fencing_token),
            'result': result,
            'result_sha256': hashlib.sha256(encoded).hexdigest(),
        }
        async with self.sessions() as session:
            row = await session.get(
                JobExecutionResultRow,
                values['execution_key'],
                with_for_update=True,
            )
            if row is None:
                raise RuntimeError('execution attempt was not started')
            if row.job_id != str(job_id):
                raise RuntimeError('execution key belongs to another job')
            if row.status == 'completed':
                await session.commit()
                return await self.get_execution_result(values['execution_key'])
            if row.status != 'running':
                raise RuntimeError(
                    f'execution result cannot be committed from status {row.status}'
                )
            if row.fencing_token != values['fencing_token']:
                raise RuntimeError('execution result fencing token is stale')
            row.status = 'completed'
            row.result = values['result']
            row.result_sha256 = values['result_sha256']
            row.updated_at = datetime.now(timezone.utc).isoformat()
            await session.commit()
        return await self.get_execution_result(values['execution_key'])

    async def store_execution_result_with_artifacts(
        self,
        execution_key,
        job_id,
        result,
        publication_ids,
        fencing_token=None,
    ):
        publication_ids = sorted({str(value) for value in publication_ids})
        if not publication_ids:
            return await self.store_execution_result(
                execution_key,
                job_id,
                result,
                fencing_token=fencing_token,
            )
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            default=str,
        ).encode('utf-8')
        values = {
            'execution_key': str(execution_key),
            'job_id': str(job_id),
            'fencing_token': str(fencing_token),
            'result': result,
            'result_sha256': hashlib.sha256(encoded).hexdigest(),
        }
        async with self.sessions() as session:
            artifacts = (await session.execute(
                select(JobArtifactRow).where(
                    JobArtifactRow.publication_id.in_(publication_ids)
                ).order_by(
                    JobArtifactRow.updated_at,
                    JobArtifactRow.publication_id,
                ).with_for_update()
            )).scalars().all()
            if len(artifacts) != len(publication_ids):
                raise RuntimeError('artifact publication was not reserved')
            row = await session.get(
                JobExecutionResultRow,
                values['execution_key'],
                with_for_update=True,
            )
            if row is None:
                raise RuntimeError('execution attempt was not started')
            if row.job_id != values['job_id']:
                raise RuntimeError('execution key belongs to another job')
            if row.status == 'completed':
                await session.commit()
                return await self.get_execution_result(values['execution_key'])
            if row.status != 'running':
                raise RuntimeError(
                    f'execution result cannot be committed from status {row.status}'
                )
            if row.fencing_token != values['fencing_token']:
                raise RuntimeError('execution result fencing token is stale')
            for artifact in artifacts:
                if (
                    artifact.job_id != values['job_id']
                    or artifact.execution_key != values['execution_key']
                    or artifact.fencing_token != values['fencing_token']
                    or artifact.attempt != row.attempt
                ):
                    raise RuntimeError('artifact publication execution identity mismatch')
                if artifact.status not in {'uploaded', 'committed'}:
                    raise RuntimeError(
                        f'artifact cannot be committed from status {artifact.status}'
                    )
            now = datetime.now(timezone.utc).isoformat()
            row.status = 'completed'
            row.result = values['result']
            row.result_sha256 = values['result_sha256']
            row.updated_at = now
            for artifact in artifacts:
                if artifact.status != 'committed':
                    artifact.status = 'committed'
                    artifact.last_error = None
                    artifact.recovery_token = None
                    artifact.recovery_lease_until = None
                    artifact.revision += 1
                    artifact.updated_at = now
            await session.commit()
        return await self.get_execution_result(values['execution_key'])

    async def _reserve_storage(
        self,
        session,
        *,
        reservation_id,
        project_id,
        job_id,
        resource_kind,
        resource_id,
        reserved_bytes,
        quota_bytes,
        expires_at,
        now,
    ):
        reserved_bytes = max(int(reserved_bytes), 1)
        quota_bytes = max(int(quota_bytes), 1)
        if self.url.startswith('postgresql'):
            await session.execute(
                postgres_insert(ProjectStorageUsageRow).values(
                    project_id=str(project_id),
                    quota_bytes=quota_bytes,
                    used_bytes=0,
                    reserved_bytes=0,
                    revision=0,
                    created_at=now,
                    updated_at=now,
                ).on_conflict_do_nothing(
                    index_elements=[ProjectStorageUsageRow.project_id],
                )
            )
        else:
            usage = await session.get(ProjectStorageUsageRow, str(project_id))
            if usage is None:
                session.add(ProjectStorageUsageRow(
                    project_id=str(project_id),
                    quota_bytes=quota_bytes,
                    used_bytes=0,
                    reserved_bytes=0,
                    revision=0,
                    created_at=now,
                    updated_at=now,
                ))
                await session.flush()
        usage = await session.get(
            ProjectStorageUsageRow,
            str(project_id),
            with_for_update=True,
        )
        if usage is None:
            raise RuntimeError('project storage usage could not be initialized')
        reservation = await session.get(
            StorageReservationRow,
            str(reservation_id),
            with_for_update=True,
        )
        if reservation is not None:
            identity = (
                reservation.project_id,
                reservation.job_id,
                reservation.resource_kind,
                reservation.resource_id,
            )
            expected = (
                str(project_id),
                str(job_id) if job_id is not None else None,
                str(resource_kind),
                str(resource_id),
            )
            if identity != expected or reservation.reserved_bytes != reserved_bytes:
                raise RuntimeError('storage reservation identity mismatch')
            return reservation
        effective_quota = min(int(usage.quota_bytes), quota_bytes)
        if int(usage.used_bytes) + int(usage.reserved_bytes) + reserved_bytes > effective_quota:
            raise ValueError('project storage quota exceeded')
        usage.quota_bytes = effective_quota
        usage.reserved_bytes += reserved_bytes
        usage.revision += 1
        usage.updated_at = now
        reservation = StorageReservationRow(
            reservation_id=str(reservation_id),
            project_id=str(project_id),
            job_id=str(job_id) if job_id is not None else None,
            resource_kind=str(resource_kind),
            resource_id=str(resource_id),
            status='reserved',
            reserved_bytes=reserved_bytes,
            actual_bytes=None,
            expires_at=str(expires_at),
            created_at=now,
            updated_at=now,
        )
        session.add(reservation)
        return reservation

    async def _commit_storage(self, session, reservation_id, actual_bytes, now):
        reservation = await session.get(
            StorageReservationRow,
            str(reservation_id),
            with_for_update=True,
        )
        if reservation is None:
            raise RuntimeError('storage reservation not found')
        actual_bytes = max(int(actual_bytes), 0)
        if reservation.status == 'committed':
            if reservation.actual_bytes != actual_bytes:
                raise RuntimeError('committed storage size changed')
            return reservation
        if reservation.status != 'reserved':
            raise RuntimeError(
                f'storage cannot be committed from status {reservation.status}'
            )
        usage = await session.get(
            ProjectStorageUsageRow,
            reservation.project_id,
            with_for_update=True,
        )
        if usage is None:
            raise RuntimeError('project storage usage not found')
        next_reserved = max(
            int(usage.reserved_bytes) - int(reservation.reserved_bytes),
            0,
        )
        if int(usage.used_bytes) + next_reserved + actual_bytes > int(usage.quota_bytes):
            raise ValueError('project storage quota exceeded')
        usage.reserved_bytes = next_reserved
        usage.used_bytes += actual_bytes
        usage.revision += 1
        usage.updated_at = now
        reservation.status = 'committed'
        reservation.actual_bytes = actual_bytes
        reservation.updated_at = now
        return reservation

    async def _release_storage(self, session, reservation_id, now):
        if not reservation_id:
            return None
        reservation = await session.get(
            StorageReservationRow,
            str(reservation_id),
            with_for_update=True,
        )
        if reservation is None or reservation.status == 'released':
            return reservation
        usage = await session.get(
            ProjectStorageUsageRow,
            reservation.project_id,
            with_for_update=True,
        )
        if usage is None:
            raise RuntimeError('project storage usage not found')
        if reservation.status == 'reserved':
            usage.reserved_bytes = max(
                int(usage.reserved_bytes) - int(reservation.reserved_bytes),
                0,
            )
        elif reservation.status == 'committed':
            usage.used_bytes = max(
                int(usage.used_bytes) - int(reservation.actual_bytes or 0),
                0,
            )
        else:
            raise RuntimeError(
                f'storage cannot be released from status {reservation.status}'
            )
        usage.revision += 1
        usage.updated_at = now
        reservation.status = 'released'
        reservation.updated_at = now
        return reservation

    async def _append_storage_deletion_event(
        self,
        session,
        *,
        resource_type,
        resource_id,
        project_id,
        job_id,
        request_id,
        actor,
        status,
        attempt,
        error,
        created_at,
    ):
        usage = await session.get(
            ProjectStorageUsageRow,
            str(project_id),
            with_for_update=True,
        )
        if usage is None:
            raise RuntimeError('project storage usage not found')
        latest = (
            await session.execute(
                select(StorageDeletionEventRow)
                .where(StorageDeletionEventRow.project_id == str(project_id))
                .order_by(StorageDeletionEventRow.sequence.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        previous_hash = latest.event_hash if latest is not None else '0' * 64
        sequence = int(latest.sequence if latest is not None else 0) + 1
        event_id = uuid4().hex
        payload = {
            'event_id': event_id,
            'sequence': sequence,
            'resource_type': str(resource_type),
            'resource_id': str(resource_id),
            'project_id': str(project_id),
            'job_id': str(job_id) if job_id is not None else None,
            'request_id': str(request_id),
            'actor': str(actor),
            'status': str(status),
            'attempt': int(attempt),
            'error': str(error or '')[:2048] or None,
            'previous_hash': previous_hash,
            'created_at': str(created_at),
        }
        event_hash = hashlib.sha256(json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')).hexdigest()
        row = StorageDeletionEventRow(
            **payload,
            event_hash=event_hash,
        )
        session.add(row)
        return row

    async def list_storage_deletion_events(
        self,
        resource_type,
        resource_id,
        limit=100,
    ):
        statement = select(StorageDeletionEventRow).where(
            StorageDeletionEventRow.resource_type == str(resource_type),
            StorageDeletionEventRow.resource_id == str(resource_id),
        ).order_by(
            StorageDeletionEventRow.sequence,
        ).limit(min(max(int(limit), 1), 1000))
        async with self.sessions() as session:
            rows = (await session.execute(statement)).scalars().all()
            return [_public_storage_deletion_event(row) for row in rows]

    async def append_audit_event(self, event):
        payload = {
            'event_id': str(event['event_id']),
            'at': str(event['at']),
            'request_id': str(event['request_id']) if event.get('request_id') else None,
            'trace_id': str(event['trace_id']) if event.get('trace_id') else None,
            'actor': str(event['actor']),
            'roles': [str(role) for role in event.get('roles') or []],
            'action': str(event['action']),
            'resource_type': str(event['resource_type']),
            'resource_id': (
                str(event['resource_id']) if event.get('resource_id') is not None else None
            ),
            'metadata': dict(event.get('metadata') or {}),
        }
        event_hash = hashlib.sha256(json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')).hexdigest()
        row = AuditEventRow(
            event_id=payload['event_id'],
            at=payload['at'],
            request_id=payload['request_id'],
            trace_id=payload['trace_id'],
            actor=payload['actor'],
            roles=payload['roles'],
            action=payload['action'],
            resource_type=payload['resource_type'],
            resource_id=payload['resource_id'],
            metadata_json=payload['metadata'],
            event_hash=event_hash,
        )
        async with self.sessions() as session:
            session.add(row)
            await session.commit()
            return _public_audit_event(row)

    async def list_audit_events(self, *, action=None, actor=None, limit=100):
        statement = select(AuditEventRow)
        if action is not None:
            statement = statement.where(AuditEventRow.action == str(action))
        if actor is not None:
            statement = statement.where(AuditEventRow.actor == str(actor))
        statement = statement.order_by(AuditEventRow.at.desc()).limit(
            min(max(int(limit), 1), 1000)
        )
        async with self.sessions() as session:
            rows = (await session.execute(statement)).scalars().all()
            return [_public_audit_event(row) for row in rows]

    async def storage_deletion_metrics(self):
        statuses = (
            'delete_requested',
            'deleting',
            'delete_failed',
            'delete_dead_letter',
            'retained',
        )
        now = datetime.now(timezone.utc)
        metrics = []
        async with self.sessions() as session:
            for resource_type, model in (
                ('file', FileRecordRow),
                ('job_artifact', JobArtifactRow),
            ):
                rows = (await session.execute(
                    select(
                        model.status,
                        func.count(),
                        func.min(model.delete_requested_at),
                    )
                    .where(
                        model.delete_request_id.is_not(None),
                        model.status.in_(statuses),
                    )
                    .group_by(model.status)
                )).all()
                by_status = {row[0]: row for row in rows}
                for deletion_status in statuses:
                    row = by_status.get(deletion_status)
                    oldest_age = 0.0
                    if row is not None and row[2]:
                        try:
                            requested_at = datetime.fromisoformat(
                                str(row[2]).replace('Z', '+00:00')
                            )
                            oldest_age = max(
                                (now - requested_at.astimezone(timezone.utc)).total_seconds(),
                                0.0,
                            )
                        except ValueError:
                            oldest_age = 0.0
                    metrics.append({
                        'resource_type': resource_type,
                        'status': deletion_status,
                        'count': int(row[1]) if row is not None else 0,
                        'oldest_age_seconds': oldest_age,
                    })
            event_rows = (await session.execute(
                select(
                    StorageDeletionEventRow.resource_type,
                    StorageDeletionEventRow.status,
                    func.count(),
                )
                .where(StorageDeletionEventRow.status.in_((
                    'retry_requested', 'delete_dead_letter',
                )))
                .group_by(
                    StorageDeletionEventRow.resource_type,
                    StorageDeletionEventRow.status,
                )
            )).all()
        return {
            'backlog': metrics,
            'events': [
                {
                    'resource_type': row[0],
                    'status': row[1],
                    'count': int(row[2]),
                }
                for row in event_rows
            ],
        }

    async def get_project_storage_usage(self, project_id):
        async with self.sessions() as session:
            row = await session.get(ProjectStorageUsageRow, str(project_id))
            if row is None:
                return None
            return {
                'project_id': row.project_id,
                'quota_bytes': row.quota_bytes,
                'used_bytes': row.used_bytes,
                'reserved_bytes': row.reserved_bytes,
                'revision': row.revision,
                'created_at': row.created_at,
                'updated_at': row.updated_at,
            }

    async def register_legacy_job_artifact(self, values, quota_bytes=10 * 1024 ** 3):
        values = dict(values)
        now = datetime.now(timezone.utc).isoformat()
        publication_id = str(values['publication_id'])
        project_id = str(values['project_id'])
        job_id = str(values['job_id'])
        size_bytes = max(int(values['size_bytes']), 0)
        reservation_id = str(
            values.get('storage_reservation_id') or publication_id
        )
        async with self.sessions() as session:
            row = await session.get(
                JobArtifactRow,
                publication_id,
                with_for_update=True,
            )
            job = await session.get(JobRow, job_id)
            if job is None:
                raise RuntimeError('legacy artifact job was not found')
            if job.project_id != project_id:
                raise RuntimeError('legacy artifact project does not match its job')
            if row is not None and row.status == 'committed':
                expected = (
                    job_id,
                    project_id,
                    str(values['artifact_id']),
                    str(values['storage_backend']),
                    size_bytes,
                    str(values['sha256']),
                )
                current = (
                    row.job_id,
                    row.project_id,
                    row.artifact_id,
                    row.storage_backend,
                    row.size_bytes,
                    row.sha256,
                )
                if current != expected:
                    raise RuntimeError(
                        'legacy artifact publication identity mismatch'
                    )
                await session.commit()
                return _public_artifact_row(row)
            if row is not None and row.status != 'quarantined':
                raise RuntimeError(
                    f'legacy artifact cannot be registered from status {row.status}'
                )
            if self.url.startswith('postgresql'):
                await session.execute(
                    postgres_insert(ProjectStorageUsageRow).values(
                        project_id=project_id,
                        quota_bytes=max(int(quota_bytes), 1),
                        used_bytes=0,
                        reserved_bytes=0,
                        revision=0,
                        created_at=now,
                        updated_at=now,
                    ).on_conflict_do_nothing(
                        index_elements=[ProjectStorageUsageRow.project_id],
                    )
                )
            elif await session.get(ProjectStorageUsageRow, project_id) is None:
                session.add(ProjectStorageUsageRow(
                    project_id=project_id,
                    quota_bytes=max(int(quota_bytes), 1),
                    used_bytes=0,
                    reserved_bytes=0,
                    revision=0,
                    created_at=now,
                    updated_at=now,
                ))
                await session.flush()
            usage = await session.get(
                ProjectStorageUsageRow,
                project_id,
                with_for_update=True,
            )
            if usage is None:
                raise RuntimeError('project storage usage could not be initialized')
            reservation = await session.get(
                StorageReservationRow,
                reservation_id,
                with_for_update=True,
            )
            if reservation is None:
                reservation = StorageReservationRow(
                    reservation_id=reservation_id,
                    project_id=project_id,
                    job_id=job_id,
                    resource_kind='artifact',
                    resource_id=publication_id,
                    status='committed',
                    reserved_bytes=max(size_bytes, 1),
                    actual_bytes=size_bytes,
                    expires_at=now,
                    created_at=now,
                    updated_at=now,
                )
                session.add(reservation)
                usage.used_bytes += size_bytes
                usage.revision += 1
                usage.updated_at = now
            else:
                identity = (
                    reservation.project_id,
                    reservation.job_id,
                    reservation.resource_kind,
                    reservation.resource_id,
                    reservation.status,
                    reservation.actual_bytes,
                )
                expected = (
                    project_id,
                    job_id,
                    'artifact',
                    publication_id,
                    'committed',
                    size_bytes,
                )
                if identity != expected:
                    raise RuntimeError(
                        'legacy artifact storage reservation mismatch'
                    )
            artifact_values = {
                'artifact_id': str(values['artifact_id']),
                'job_id': job_id,
                'project_id': project_id,
                'execution_key': str(values['execution_key']),
                'fencing_token': str(values['fencing_token']),
                'attempt': int(values['attempt']),
                'parameter': str(values['parameter']),
                'kind': str(values['kind']),
                'status': 'committed',
                'storage_backend': str(values['storage_backend']),
                'filename': str(values['filename']),
                'content_type': values.get('content_type'),
                'size_bytes': size_bytes,
                'sha256': str(values['sha256']),
                'path': values.get('path'),
                'storage_key': values.get('storage_key'),
                'version_id': values.get('version_id'),
                'reference': values.get('reference'),
                'retention_until': values.get('retention_until'),
                'last_error': None,
                'storage_reservation_id': reservation_id,
                'recovery_token': None,
                'recovery_lease_until': None,
                'updated_at': now,
            }
            if row is None:
                row = JobArtifactRow(
                    publication_id=publication_id,
                    revision=0,
                    created_at=str(values.get('created_at') or now),
                    **artifact_values,
                )
                session.add(row)
            else:
                for key, value in artifact_values.items():
                    setattr(row, key, value)
                row.revision += 1
            await session.commit()
            return _public_artifact_row(row)

    async def quarantine_legacy_job_artifact(self, values, error):
        values = dict(values)
        now = datetime.now(timezone.utc).isoformat()
        publication_id = str(values['publication_id'])
        job_id = str(values['job_id'])
        project_id = str(values['project_id'])
        async with self.sessions() as session:
            row = await session.get(
                JobArtifactRow,
                publication_id,
                with_for_update=True,
            )
            job = await session.get(JobRow, job_id)
            if job is None or job.project_id != project_id:
                raise RuntimeError('legacy artifact job identity mismatch')
            if row is not None and row.status == 'committed':
                await session.commit()
                return _public_artifact_row(row)
            if row is not None and row.status != 'quarantined':
                raise RuntimeError(
                    f'legacy artifact cannot be quarantined from status {row.status}'
                )
            artifact_values = {
                'artifact_id': str(values['artifact_id']),
                'job_id': job_id,
                'project_id': project_id,
                'execution_key': str(values['execution_key']),
                'fencing_token': str(values['fencing_token']),
                'attempt': int(values['attempt']),
                'parameter': str(values['parameter']),
                'kind': str(values['kind']),
                'status': 'quarantined',
                'storage_backend': str(values['storage_backend']),
                'filename': str(values['filename']),
                'content_type': values.get('content_type'),
                'size_bytes': values.get('size_bytes'),
                'sha256': values.get('sha256'),
                'path': values.get('path'),
                'storage_key': values.get('storage_key'),
                'version_id': values.get('version_id'),
                'reference': values.get('reference'),
                'retention_until': None,
                'last_error': str(error)[:2048],
                'storage_reservation_id': None,
                'recovery_token': None,
                'recovery_lease_until': None,
                'updated_at': now,
            }
            if row is None:
                row = JobArtifactRow(
                    publication_id=publication_id,
                    revision=0,
                    created_at=str(values.get('created_at') or now),
                    **artifact_values,
                )
                session.add(row)
            else:
                for key, value in artifact_values.items():
                    setattr(row, key, value)
                row.revision += 1
            await session.commit()
            return _public_artifact_row(row)

    async def reserve_job_artifacts(self, records):
        records = [dict(item) for item in records]
        if not records:
            return []
        now = datetime.now(timezone.utc).isoformat()
        async with self.sessions() as session:
            for values in records:
                publication_id = str(values['publication_id'])
                row = await session.get(
                    JobArtifactRow,
                    publication_id,
                    with_for_update=True,
                )
                identity = {
                    'artifact_id': str(values['artifact_id']),
                    'job_id': str(values['job_id']),
                    'project_id': str(values['project_id']),
                    'execution_key': str(values['execution_key']),
                    'fencing_token': str(values['fencing_token']),
                    'attempt': int(values['attempt']),
                    'parameter': str(values['parameter']),
                    'kind': str(values['kind']),
                    'storage_backend': str(values['storage_backend']),
                    'filename': str(values['filename']),
                }
                if row is None:
                    reservation_id = str(
                        values.get('storage_reservation_id') or publication_id
                    )
                    await self._reserve_storage(
                        session,
                        reservation_id=reservation_id,
                        project_id=identity['project_id'],
                        job_id=identity['job_id'],
                        resource_kind='artifact',
                        resource_id=publication_id,
                        reserved_bytes=values.get('reserved_bytes') or 1,
                        quota_bytes=values.get('quota_bytes') or 10 * 1024 ** 3,
                        expires_at=values.get('reservation_expires_at') or now,
                        now=now,
                    )
                    session.add(JobArtifactRow(
                        publication_id=publication_id,
                        status='reserved',
                        content_type=None,
                        size_bytes=None,
                        sha256=None,
                        path=values.get('path'),
                        storage_key=values.get('storage_key'),
                        version_id=None,
                        reference=None,
                        retention_until=None,
                        last_error=None,
                        storage_reservation_id=reservation_id,
                        recovery_token=None,
                        recovery_lease_until=None,
                        revision=0,
                        created_at=now,
                        updated_at=now,
                        **identity,
                    ))
                    continue
                for key, expected in identity.items():
                    if getattr(row, key) != expected:
                        raise RuntimeError(
                            'artifact publication id belongs to another artifact'
                        )
            await session.commit()
        return await self.list_job_artifacts(records[0]['job_id'])

    async def mark_job_artifacts_uploaded(self, records):
        records = [dict(item) for item in records]
        if not records:
            return []
        now = datetime.now(timezone.utc).isoformat()
        job_id = None
        async with self.sessions() as session:
            for values in records:
                row = await session.get(
                    JobArtifactRow,
                    str(values['publication_id']),
                    with_for_update=True,
                )
                if row is None:
                    raise RuntimeError('artifact publication was not reserved')
                job_id = row.job_id
                if row.status == 'committed':
                    continue
                if row.status not in {'reserved', 'uploaded', 'orphaned'}:
                    raise RuntimeError(
                        f'artifact cannot be uploaded from status {row.status}'
                    )
                row.status = 'uploaded'
                row.content_type = str(values.get('content_type') or '') or None
                row.size_bytes = int(values['size_bytes'])
                row.sha256 = str(values['sha256'])
                row.path = values.get('path')
                row.storage_key = values.get('storage_key')
                row.version_id = values.get('version_id')
                row.reference = values.get('reference')
                row.retention_until = None
                row.last_error = None
                row.recovery_token = None
                row.recovery_lease_until = None
                row.revision += 1
                row.updated_at = now
                await self._commit_storage(
                    session,
                    row.storage_reservation_id,
                    row.size_bytes,
                    now,
                )
            await session.commit()
        return await self.list_job_artifacts(job_id)

    async def commit_job_artifacts(self, publication_ids):
        publication_ids = [str(value) for value in publication_ids]
        if not publication_ids:
            return []
        now = datetime.now(timezone.utc).isoformat()
        job_id = None
        async with self.sessions() as session:
            for publication_id in publication_ids:
                row = await session.get(
                    JobArtifactRow,
                    publication_id,
                    with_for_update=True,
                )
                if row is None:
                    raise RuntimeError('artifact publication was not reserved')
                job_id = row.job_id
                if row.status == 'committed':
                    continue
                if row.status != 'uploaded':
                    raise RuntimeError(
                        f'artifact cannot be committed from status {row.status}'
                    )
                row.status = 'committed'
                row.last_error = None
                row.recovery_token = None
                row.recovery_lease_until = None
                row.revision += 1
                row.updated_at = now
            await session.commit()
        return await self.list_job_artifacts(job_id)

    async def orphan_job_artifacts(self, publication_ids, error=None):
        publication_ids = [str(value) for value in publication_ids]
        if not publication_ids:
            return []
        now = datetime.now(timezone.utc).isoformat()
        job_id = None
        async with self.sessions() as session:
            for publication_id in publication_ids:
                row = await session.get(
                    JobArtifactRow,
                    publication_id,
                    with_for_update=True,
                )
                if row is None:
                    continue
                job_id = row.job_id
                if row.status in {'committed', 'deleted', 'reclaiming'}:
                    continue
                row.status = 'orphaned'
                row.last_error = str(error or '')[:2048] or None
                row.recovery_token = None
                row.recovery_lease_until = None
                row.revision += 1
                row.updated_at = now
            await session.commit()
        return await self.list_job_artifacts(job_id) if job_id else []

    async def request_job_artifact_deletion(
        self,
        job_id,
        artifact_id,
        requested_by,
        request_id,
        requested_at,
    ):
        selected_request_id = str(request_id or '').strip()
        if not selected_request_id or len(selected_request_id) > 128:
            raise ValueError('delete request id must contain at most 128 characters')
        actor = str(requested_by or '').strip()
        if not actor or len(actor) > 200:
            raise ValueError('delete requester is invalid')
        statement = select(JobArtifactRow).where(
            JobArtifactRow.job_id == str(job_id),
            JobArtifactRow.artifact_id == str(artifact_id),
        ).order_by(JobArtifactRow.updated_at.desc()).limit(1).with_for_update()
        async with self.sessions() as session:
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                raise ValueError('artifact publication not found')
            if row.status == 'committed':
                row.status = 'delete_requested'
                row.delete_request_id = selected_request_id
                row.delete_requested_by = actor
                row.delete_requested_at = str(requested_at)
                row.deleted_at = None
                row.last_error = None
                row.retention_until = None
                row.delete_attempts = 0
                row.delete_next_attempt_at = str(requested_at)
                row.revision += 1
                row.updated_at = str(requested_at)
                await self._append_storage_deletion_event(
                    session,
                    resource_type='job_artifact',
                    resource_id=row.publication_id,
                    project_id=row.project_id,
                    job_id=row.job_id,
                    request_id=selected_request_id,
                    actor=actor,
                    status='delete_requested',
                    attempt=0,
                    error=None,
                    created_at=str(requested_at),
                )
            elif row.status not in {
                'delete_requested', 'deleting', 'delete_failed', 'retained',
                'delete_dead_letter', 'deleted',
            }:
                raise RuntimeError(
                    f'artifact cannot be deleted from status {row.status}'
                )
            await session.commit()
            return _public_artifact_row(row)

    async def retry_job_artifact_deletion(
        self,
        job_id,
        artifact_id,
        requested_by,
        request_id,
        requested_at,
    ):
        selected_request_id = str(request_id or '').strip()
        actor = str(requested_by or '').strip()
        if not selected_request_id or len(selected_request_id) > 128:
            raise ValueError('delete request id must contain at most 128 characters')
        if not actor or len(actor) > 200:
            raise ValueError('delete requester is invalid')
        statement = select(JobArtifactRow).where(
            JobArtifactRow.job_id == str(job_id),
            JobArtifactRow.artifact_id == str(artifact_id),
        ).order_by(JobArtifactRow.updated_at.desc()).limit(1).with_for_update()
        async with self.sessions() as session:
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                raise ValueError('artifact publication not found')
            if row.delete_request_id == selected_request_id:
                output = _public_artifact_row(row)
                output['_deduplicated'] = True
                return output
            if row.status not in {'delete_failed', 'delete_dead_letter'}:
                raise RuntimeError(
                    f'artifact deletion cannot retry from status {row.status}'
                )
            row.status = 'delete_requested'
            row.delete_request_id = selected_request_id
            row.delete_requested_by = actor
            row.delete_requested_at = str(requested_at)
            row.delete_attempts = 0
            row.delete_next_attempt_at = str(requested_at)
            row.last_error = None
            row.recovery_token = None
            row.recovery_lease_until = None
            row.revision += 1
            row.updated_at = str(requested_at)
            await self._append_storage_deletion_event(
                session,
                resource_type='job_artifact',
                resource_id=row.publication_id,
                project_id=row.project_id,
                job_id=row.job_id,
                request_id=selected_request_id,
                actor=actor,
                status='retry_requested',
                attempt=0,
                error=None,
                created_at=str(requested_at),
            )
            await session.commit()
            output = _public_artifact_row(row)
            output['_deduplicated'] = False
            return output

    async def claim_recoverable_job_artifacts(
        self,
        recovery_token,
        updated_before,
        lease_seconds=300,
        limit=100,
    ):
        token = str(recovery_token)
        if not token:
            raise ValueError('artifact recovery token is required')
        now_value = datetime.now(timezone.utc)
        now = now_value.isoformat()
        lease_until = (
            now_value + timedelta(seconds=max(int(lease_seconds), 1))
        ).isoformat()
        statement = (
            select(JobArtifactRow)
            .where(
                JobArtifactRow.status.in_((
                    'reserved', 'uploaded', 'orphaned', 'retained', 'reclaiming',
                    'delete_requested', 'deleting', 'delete_failed',
                )),
                or_(
                    and_(
                        JobArtifactRow.status.in_(('reclaiming', 'deleting')),
                        or_(
                            JobArtifactRow.recovery_lease_until.is_(None),
                            JobArtifactRow.recovery_lease_until < now,
                        ),
                    ),
                    JobArtifactRow.status.in_((
                        'delete_requested',
                    )),
                    and_(
                        JobArtifactRow.status == 'delete_failed',
                        or_(
                            JobArtifactRow.delete_next_attempt_at.is_(None),
                            JobArtifactRow.delete_next_attempt_at <= now,
                        ),
                    ),
                    and_(
                        JobArtifactRow.status.not_in((
                            'reclaiming', 'deleting', 'delete_requested',
                            'delete_failed',
                        )),
                        JobArtifactRow.updated_at < str(updated_before),
                    ),
                ),
            )
            .order_by(JobArtifactRow.updated_at, JobArtifactRow.publication_id)
            .limit(min(max(int(limit), 1) * 4, 4000))
            .with_for_update(skip_locked=True)
        )
        claimed = []
        async with self.sessions() as session:
            rows = (await session.execute(statement)).scalars().all()
            for row in rows:
                if len(claimed) >= min(max(int(limit), 1), 1000):
                    break
                if (
                    row.status in {'reclaiming', 'deleting'}
                    and row.recovery_lease_until
                    and row.recovery_lease_until >= now
                ):
                    continue
                if (
                    row.status == 'retained'
                    and row.retention_until
                    and row.retention_until > now
                ):
                    continue
                if row.status in {'reserved', 'uploaded'}:
                    execution = await session.get(
                        JobExecutionResultRow,
                        row.execution_key,
                    )
                    job = await session.get(JobRow, row.job_id)
                    execution_finished = (
                        execution is not None and execution.status == 'completed'
                    )
                    job_abandoned = (
                        job is None
                        or job.status in {'failed', 'cancelled', 'indeterminate'}
                    )
                    if not execution_finished and not job_abandoned:
                        continue
                row.status = 'deleting' if row.delete_request_id else 'reclaiming'
                if row.delete_request_id:
                    row.delete_attempts = int(row.delete_attempts or 0) + 1
                    row.delete_next_attempt_at = None
                row.recovery_token = token
                row.recovery_lease_until = lease_until
                row.revision += 1
                row.updated_at = now
                claimed.append(row)
            await session.commit()
            return [_public_artifact_row(row) for row in claimed]

    async def finish_job_artifact_recovery(
        self,
        publication_id,
        recovery_token,
        status,
        error=None,
        retention_until=None,
        next_attempt_at=None,
    ):
        allowed = {
            'committed', 'orphaned', 'retained', 'delete_failed',
            'delete_dead_letter', 'deleted',
        }
        if status not in allowed:
            raise ValueError('invalid artifact recovery status')
        async with self.sessions() as session:
            row = await session.get(
                JobArtifactRow,
                str(publication_id),
                with_for_update=True,
            )
            if row is None:
                raise ValueError('artifact publication not found')
            claimed_status = row.status
            if claimed_status not in {'reclaiming', 'deleting'}:
                raise RuntimeError(
                    f'artifact is not claimed for recovery: {row.status}'
                )
            if row.recovery_token != str(recovery_token):
                raise RuntimeError('artifact recovery token is stale')
            if claimed_status == 'deleting' and status in {'committed', 'orphaned'}:
                raise RuntimeError('explicit artifact deletion cannot be reverted')
            if claimed_status == 'reclaiming' and status in {
                'delete_failed', 'delete_dead_letter',
            }:
                raise RuntimeError('orphan recovery cannot enter delete_failed')
            now = datetime.now(timezone.utc).isoformat()
            if status == 'delete_failed':
                maximum_attempts = _configured_int(
                    'ARTIFACT_DELETE_MAX_ATTEMPTS',
                    8,
                    1,
                )
                if int(row.delete_attempts or 0) >= maximum_attempts:
                    status = 'delete_dead_letter'
                    next_attempt_at = None
                elif next_attempt_at is None:
                    base_seconds = _configured_int(
                        'ARTIFACT_DELETE_RETRY_BASE_SECONDS',
                        60,
                        1,
                    )
                    maximum_seconds = _configured_int(
                        'ARTIFACT_DELETE_RETRY_MAX_SECONDS',
                        21600,
                        base_seconds,
                    )
                    delay = min(
                        base_seconds * (2 ** max(int(row.delete_attempts or 1) - 1, 0)),
                        maximum_seconds,
                    )
                    next_attempt_at = (
                        datetime.now(timezone.utc) + timedelta(seconds=delay)
                    ).isoformat()
            if status == 'deleted':
                await self._release_storage(
                    session,
                    row.storage_reservation_id,
                    now,
                )
                row.deleted_at = now
            row.status = status
            row.last_error = str(error or '')[:2048] or None
            row.retention_until = retention_until
            row.delete_next_attempt_at = (
                str(next_attempt_at) if next_attempt_at is not None else None
            )
            row.recovery_token = None
            row.recovery_lease_until = None
            row.revision += 1
            row.updated_at = now
            if row.delete_request_id:
                await self._append_storage_deletion_event(
                    session,
                    resource_type='job_artifact',
                    resource_id=row.publication_id,
                    project_id=row.project_id,
                    job_id=row.job_id,
                    request_id=row.delete_request_id,
                    actor='system:artifact-recovery',
                    status=status,
                    attempt=row.delete_attempts,
                    error=row.last_error,
                    created_at=now,
                )
            await session.commit()
            return _public_artifact_row(row)

    async def list_job_artifacts(self, job_id, statuses=None):
        statement = select(JobArtifactRow).where(
            JobArtifactRow.job_id == str(job_id)
        )
        if statuses:
            statement = statement.where(
                JobArtifactRow.status.in_(tuple(str(item) for item in statuses))
            )
        statement = statement.order_by(JobArtifactRow.created_at)
        async with self.sessions() as session:
            rows = (await session.execute(statement)).scalars().all()
            return [_public_artifact_row(row) for row in rows]

    async def get_job_artifact(self, job_id, artifact_id, statuses=None):
        statement = select(JobArtifactRow).where(
            JobArtifactRow.job_id == str(job_id),
            JobArtifactRow.artifact_id == str(artifact_id),
        )
        if statuses:
            statement = statement.where(
                JobArtifactRow.status.in_(tuple(str(item) for item in statuses))
            )
        statement = statement.order_by(JobArtifactRow.updated_at.desc()).limit(1)
        async with self.sessions() as session:
            row = (await session.execute(statement)).scalar_one_or_none()
            return _public_artifact_row(row) if row else None

    async def list_job_artifacts_for_jobs(self, job_ids, statuses=None):
        selected_job_ids = tuple(dict.fromkeys(
            str(job_id) for job_id in job_ids if job_id
        ))
        if not selected_job_ids:
            return {}
        statement = select(JobArtifactRow).where(
            JobArtifactRow.job_id.in_(selected_job_ids)
        )
        if statuses:
            statement = statement.where(
                JobArtifactRow.status.in_(tuple(str(item) for item in statuses))
            )
        statement = statement.order_by(
            JobArtifactRow.job_id,
            JobArtifactRow.created_at,
        )
        async with self.sessions() as session:
            rows = (await session.execute(statement)).scalars().all()
        grouped = {job_id: [] for job_id in selected_job_ids}
        for row in rows:
            grouped.setdefault(row.job_id, []).append(_public_artifact_row(row))
        return grouped

    async def list_recoverable_job_artifacts(self, updated_before, limit=100):
        now = datetime.now(timezone.utc).isoformat()
        statement = select(JobArtifactRow).where(
            JobArtifactRow.status.in_((
                'reserved', 'uploaded', 'orphaned', 'retained', 'reclaiming',
                'delete_requested', 'deleting', 'delete_failed',
            )),
            or_(
                JobArtifactRow.status == 'delete_requested',
                and_(
                    JobArtifactRow.status == 'delete_failed',
                    or_(
                        JobArtifactRow.delete_next_attempt_at.is_(None),
                        JobArtifactRow.delete_next_attempt_at <= now,
                    ),
                ),
                and_(
                    JobArtifactRow.status != 'delete_failed',
                    JobArtifactRow.updated_at < str(updated_before),
                ),
            ),
        ).order_by(JobArtifactRow.updated_at).limit(min(max(int(limit), 1), 1000))
        async with self.sessions() as session:
            rows = (await session.execute(statement)).scalars().all()
            return [_public_artifact_row(row) for row in rows]

    async def get_job(self, job_id):
        async with self.sessions() as session:
            row = await session.get(JobRow, str(job_id))
            if row is None:
                return None
            record = _public_row(row)
            if row.status == 'queued':
                outbox = await session.get(JobOutboxRow, str(job_id))
                if outbox is not None and outbox.next_attempt_at is not None:
                    scheduling = (outbox.payload or {}).get('scheduling')
                    if (
                        isinstance(scheduling, dict)
                        and scheduling.get('status') == 'waiting_for_external_service'
                        and outbox.next_attempt_at > time()
                    ):
                        record['scheduling'] = scheduling
            return record

    async def request_job_cancel(
        self, job_id, require_deferred=False, known_waiting=False
    ):
        selected_job_id = str(job_id)
        async with self.sessions() as session:
            row = await session.get(JobRow, selected_job_id, with_for_update=True)
            if row is None:
                return None
            outbox = await session.get(
                JobOutboxRow, selected_job_id, with_for_update=True
            )
            scheduling = (
                (outbox.payload or {}).get('scheduling')
                if outbox is not None else None
            )
            durable_waiting = bool(
                outbox is not None
                and row.status == 'queued'
                and outbox.next_attempt_at is not None
                and isinstance(scheduling, dict)
                and scheduling.get('status') == 'waiting_for_external_service'
            )
            if require_deferred and not durable_waiting:
                return None
            if row.status in TERMINAL_STATUSES:
                return _public_row(row)
            row.cancel_requested = True
            if durable_waiting or known_waiting:
                row.status = 'cancelled'
                row.finished_at = datetime.now(timezone.utc).isoformat()
                row.error = 'job cancelled by user'
            record = _public_row(row)
            record['_cancel_requested'] = True
            record['_attempts'] = row.attempts
            revision = await session.scalar(
                select(func.max(JobEventRow.revision)).where(
                    JobEventRow.job_id == selected_job_id
                )
            )
            record['_revision'] = int(revision or 0) + 1
            await self._persist_job_events(session, [record])
            if row.status == 'cancelled' and outbox is not None:
                await session.delete(outbox)
            await session.commit()
            return _public_row(row)

    async def cancel_deferred_job(self, job_id):
        return await self.request_job_cancel(job_id, require_deferred=True)

    async def list_jobs(self, limit=20):
        size = min(max(int(limit), 1), 100)
        statement = select(JobRow).order_by(JobRow.created_at.desc()).limit(size)
        async with self.sessions() as session:
            rows = (await session.execute(statement)).scalars().all()
            return [_public_row(row) for row in rows]

    async def list_jobs_with_artifact_manifests(
        self,
        after_job_id=None,
        limit=100,
    ):
        statement = select(JobRow).where(JobRow.artifacts.is_not(None))
        if after_job_id:
            statement = statement.where(JobRow.job_id > str(after_job_id))
        statement = statement.order_by(JobRow.job_id).limit(
            min(max(int(limit), 1), 1000)
        )
        async with self.sessions() as session:
            rows = (await session.execute(statement)).scalars().all()
            return [_public_row(row) for row in rows]

    async def list_jobs_for_principal(
        self,
        subject,
        *,
        is_admin=False,
        project_id=None,
        limit=20,
    ):
        size = min(max(int(limit), 1), 100)
        statement = select(JobRow)
        if not is_admin:
            statement = statement.join(
                ProjectMemberRow,
                ProjectMemberRow.project_id == JobRow.project_id,
            ).where(ProjectMemberRow.subject == str(subject))
        if project_id is not None:
            statement = statement.where(JobRow.project_id == str(project_id))
        statement = statement.order_by(JobRow.created_at.desc()).limit(size)
        async with self.sessions() as session:
            if (
                self.url.startswith('postgresql')
                and DATABASE_SUBJECT.get() != str(subject)
            ):
                raise PermissionError('database principal does not match subject')
            rows = (await session.execute(statement)).scalars().all()
            return [_public_row(row) for row in rows]

    async def create_project(self, project_id, name, description, owner_subject, created_at):
        async with self.sessions() as session:
            session.add(ProjectRow(
                project_id=project_id,
                name=name,
                description=description,
                owner_subject=owner_subject,
                created_at=created_at,
            ))
            await session.flush()
            session.add(ProjectMemberRow(
                project_id=project_id,
                subject=owner_subject,
                role='owner',
                created_at=created_at,
            ))
            await session.commit()
            row = await session.get(ProjectRow, project_id)
            return _public_project(row)

    async def get_project(self, project_id):
        async with self.sessions() as session:
            row = await session.get(ProjectRow, str(project_id))
            return _public_project(row) if row else None

    async def list_projects(self, subject, limit=20):
        size = min(max(int(limit), 1), 100)
        statement = (
            select(ProjectRow)
            .join(ProjectMemberRow, ProjectMemberRow.project_id == ProjectRow.project_id)
            .where(ProjectMemberRow.subject == str(subject))
            .order_by(ProjectRow.created_at.desc())
            .limit(size)
        )
        async with self.sessions() as session:
            rows = (await session.execute(statement)).scalars().all()
            return [_public_project(row) for row in rows]

    async def get_project_member(self, project_id, subject):
        async with self.sessions() as session:
            row = await session.get(ProjectMemberRow, {
                'project_id': str(project_id),
                'subject': str(subject),
            })
            if row is None:
                return None
            return {
                'project_id': row.project_id,
                'subject': row.subject,
                'role': row.role,
                'created_at': row.created_at,
            }

    async def list_project_members(self, project_id):
        statement = (
            select(ProjectMemberRow)
            .where(ProjectMemberRow.project_id == str(project_id))
            .order_by(ProjectMemberRow.created_at)
        )
        async with self.sessions() as session:
            rows = (await session.execute(statement)).scalars().all()
            return [
                {
                    'project_id': row.project_id,
                    'subject': row.subject,
                    'role': row.role,
                    'created_at': row.created_at,
                }
                for row in rows
            ]

    async def upsert_project_member(self, project_id, subject, role, created_at):
        async with self.sessions() as session:
            row = await session.get(ProjectMemberRow, {
                'project_id': str(project_id),
                'subject': str(subject),
            })
            if row is None:
                row = ProjectMemberRow(
                    project_id=str(project_id),
                    subject=str(subject),
                    role=role,
                    created_at=created_at,
                )
                session.add(row)
            else:
                row.role = role
            await session.commit()
            return {
                'project_id': row.project_id,
                'subject': row.subject,
                'role': row.role,
                'created_at': row.created_at,
            }

    async def delete_project_member(self, project_id, subject):
        async with self.sessions() as session:
            project = await session.get(ProjectRow, str(project_id))
            if project is None:
                return False
            if project.owner_subject == str(subject):
                raise ValueError('project owner membership cannot be removed')
            result = await session.execute(
                delete(ProjectMemberRow).where(
                    ProjectMemberRow.project_id == str(project_id),
                    ProjectMemberRow.subject == str(subject),
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def assign_job_project(self, job_id, project_id, created_at):
        async with self.sessions() as session:
            job = await session.get(JobRow, str(job_id))
            if job is None:
                raise ValueError(f'job not found: {job_id}')
            await self._bind_job_project(
                session,
                job_id,
                project_id,
                created_at,
            )
            await session.commit()

    async def get_job_project(self, job_id):
        async with self.sessions() as session:
            row = await session.get(JobRow, str(job_id))
            return row.project_id if row else None

    async def begin_file_upload(
        self,
        file_id,
        project_id,
        filename,
        storage_backend,
        storage_key,
        reserved_bytes,
        quota_bytes,
        created_at,
        reservation_ttl_seconds=3600,
    ):
        now = str(created_at)
        expires_at = (
            datetime.fromisoformat(now.replace('Z', '+00:00'))
            + timedelta(seconds=max(int(reservation_ttl_seconds), 60))
        ).isoformat()
        reservation_id = str(file_id)
        async with self.sessions() as session:
            project = await session.get(ProjectRow, str(project_id))
            if project is None:
                raise ValueError(f'project not found: {project_id}')
            await self._reserve_storage(
                session,
                reservation_id=reservation_id,
                project_id=project_id,
                job_id=None,
                resource_kind='file',
                resource_id=file_id,
                reserved_bytes=reserved_bytes,
                quota_bytes=quota_bytes,
                expires_at=expires_at,
                now=now,
            )
            ownership = await session.get(FileProjectRow, str(file_id))
            if ownership is None:
                session.add(FileProjectRow(
                    file_id=str(file_id),
                    project_id=str(project_id),
                    created_at=now,
                ))
            elif ownership.project_id != str(project_id):
                raise ValueError('file already belongs to another project')
            row = await session.get(FileRecordRow, str(file_id), with_for_update=True)
            if row is None:
                row = FileRecordRow(
                    file_id=str(file_id),
                    project_id=str(project_id),
                    filename=str(filename),
                    storage_backend=str(storage_backend),
                    storage_key=str(storage_key) if storage_key else None,
                    version_id=None,
                    sha256=None,
                    size_bytes=None,
                    status='pending',
                    last_error=None,
                    storage_reservation_id=reservation_id,
                    recovery_token=None,
                    recovery_lease_until=None,
                    retention_until=None,
                    revision=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            elif row.project_id != str(project_id):
                raise ValueError('file already belongs to another project')
            await session.commit()
        return await self.get_file_record(file_id)

    async def mark_file_uploading(self, file_id, updated_at):
        async with self.sessions() as session:
            row = await session.get(FileRecordRow, str(file_id), with_for_update=True)
            if row is None:
                raise ValueError('file upload not found')
            if row.status not in {'pending', 'uploading'}:
                raise RuntimeError(f'file cannot upload from status {row.status}')
            row.status = 'uploading'
            row.revision += 1
            row.updated_at = str(updated_at)
            await session.commit()
        return await self.get_file_record(file_id)

    async def activate_file_upload(self, file_id, stored, updated_at):
        now = str(updated_at)
        async with self.sessions() as session:
            row = await session.get(FileRecordRow, str(file_id), with_for_update=True)
            if row is None:
                raise ValueError('file upload not found')
            if row.status == 'active':
                await session.commit()
                return await self.get_file_record(file_id)
            if row.status not in {'pending', 'uploading'}:
                raise RuntimeError(f'file cannot activate from status {row.status}')
            await self._commit_storage(
                session,
                row.storage_reservation_id,
                int(stored['size_bytes']),
                now,
            )
            row.filename = str(stored['filename'])
            row.storage_key = stored.get('storage_key') or row.storage_key
            row.version_id = stored.get('version_id')
            row.sha256 = str(stored['sha256'])
            row.size_bytes = int(stored['size_bytes'])
            row.status = 'active'
            row.last_error = None
            row.revision += 1
            row.updated_at = now
            await session.commit()
        return await self.get_file_record(file_id)

    async def fail_file_upload(self, file_id, error, updated_at):
        async with self.sessions() as session:
            row = await session.get(FileRecordRow, str(file_id), with_for_update=True)
            if row is None:
                return None
            if row.status in {'active', 'deleted'}:
                return await self.get_file_record(file_id)
            row.status = 'orphaned'
            row.last_error = str(error or '')[:2048] or None
            row.revision += 1
            row.updated_at = str(updated_at)
            await session.commit()
        return await self.get_file_record(file_id)

    async def discard_failed_file_upload(self, file_id, error, updated_at):
        now = str(updated_at)
        async with self.sessions() as session:
            row = await session.get(FileRecordRow, str(file_id), with_for_update=True)
            if row is None:
                return None
            if row.status == 'active':
                raise RuntimeError('active file upload cannot be discarded')
            if row.status != 'deleted':
                await self._release_storage(
                    session,
                    row.storage_reservation_id,
                    now,
                )
                row.status = 'deleted'
                row.last_error = str(error or '')[:2048] or None
                row.recovery_token = None
                row.recovery_lease_until = None
                row.retention_until = None
                row.revision += 1
                row.updated_at = now
            await session.commit()
        return await self.get_file_record(file_id)

    async def request_file_deletion(
        self,
        file_id,
        requested_by,
        request_id,
        requested_at,
    ):
        selected_request_id = str(request_id or '').strip()
        if not selected_request_id or len(selected_request_id) > 128:
            raise ValueError('delete request id must contain at most 128 characters')
        actor = str(requested_by or '').strip()
        if not actor or len(actor) > 200:
            raise ValueError('delete requester is invalid')
        async with self.sessions() as session:
            row = await session.get(FileRecordRow, str(file_id), with_for_update=True)
            if row is None:
                raise ValueError('file upload not found')
            if row.status == 'active':
                row.status = 'delete_requested'
                row.delete_request_id = selected_request_id
                row.delete_requested_by = actor
                row.delete_requested_at = str(requested_at)
                row.deleted_at = None
                row.last_error = None
                row.retention_until = None
                row.delete_attempts = 0
                row.delete_next_attempt_at = str(requested_at)
                row.revision += 1
                row.updated_at = str(requested_at)
                await self._append_storage_deletion_event(
                    session,
                    resource_type='file',
                    resource_id=row.file_id,
                    project_id=row.project_id,
                    job_id=None,
                    request_id=selected_request_id,
                    actor=actor,
                    status='delete_requested',
                    attempt=0,
                    error=None,
                    created_at=str(requested_at),
                )
            elif row.status not in {
                'delete_requested', 'deleting', 'delete_failed', 'retained',
                'delete_dead_letter', 'deleted',
            }:
                raise RuntimeError(
                    f'file cannot be deleted from status {row.status}'
                )
            await session.commit()
            return self._file_record_dict(row)

    async def retry_file_deletion(
        self,
        file_id,
        requested_by,
        request_id,
        requested_at,
    ):
        selected_request_id = str(request_id or '').strip()
        actor = str(requested_by or '').strip()
        if not selected_request_id or len(selected_request_id) > 128:
            raise ValueError('delete request id must contain at most 128 characters')
        if not actor or len(actor) > 200:
            raise ValueError('delete requester is invalid')
        async with self.sessions() as session:
            row = await session.get(FileRecordRow, str(file_id), with_for_update=True)
            if row is None:
                raise ValueError('file upload not found')
            if row.delete_request_id == selected_request_id:
                output = self._file_record_dict(row)
                output['_deduplicated'] = True
                return output
            if row.status not in {'delete_failed', 'delete_dead_letter'}:
                raise RuntimeError(
                    f'file deletion cannot retry from status {row.status}'
                )
            row.status = 'delete_requested'
            row.delete_request_id = selected_request_id
            row.delete_requested_by = actor
            row.delete_requested_at = str(requested_at)
            row.delete_attempts = 0
            row.delete_next_attempt_at = str(requested_at)
            row.last_error = None
            row.recovery_token = None
            row.recovery_lease_until = None
            row.revision += 1
            row.updated_at = str(requested_at)
            await self._append_storage_deletion_event(
                session,
                resource_type='file',
                resource_id=row.file_id,
                project_id=row.project_id,
                job_id=None,
                request_id=selected_request_id,
                actor=actor,
                status='retry_requested',
                attempt=0,
                error=None,
                created_at=str(requested_at),
            )
            await session.commit()
            output = self._file_record_dict(row)
            output['_deduplicated'] = False
            return output

    async def list_recoverable_files(self, updated_before, limit=100):
        now = datetime.now(timezone.utc).isoformat()
        statement = select(FileRecordRow).where(
            FileRecordRow.status.in_((
                'pending', 'uploading', 'orphaned', 'retained', 'reclaiming',
                'delete_requested', 'deleting', 'delete_failed',
            )),
            or_(
                FileRecordRow.status == 'delete_requested',
                and_(
                    FileRecordRow.status == 'delete_failed',
                    or_(
                        FileRecordRow.delete_next_attempt_at.is_(None),
                        FileRecordRow.delete_next_attempt_at <= now,
                    ),
                ),
                and_(
                    FileRecordRow.status != 'delete_failed',
                    FileRecordRow.updated_at < str(updated_before),
                ),
            ),
        ).order_by(
            FileRecordRow.updated_at,
            FileRecordRow.file_id,
        ).limit(min(max(int(limit), 1), 1000))
        async with self.sessions() as session:
            rows = (await session.execute(statement)).scalars().all()
            return [self._file_record_dict(row) for row in rows]

    async def claim_recoverable_files(
        self,
        recovery_token,
        updated_before,
        lease_seconds=300,
        limit=100,
    ):
        token = str(recovery_token)
        if not token:
            raise ValueError('file recovery token is required')
        now_value = datetime.now(timezone.utc)
        now = now_value.isoformat()
        lease_until = (
            now_value + timedelta(seconds=max(int(lease_seconds), 1))
        ).isoformat()
        statement = select(FileRecordRow).where(
            FileRecordRow.status.in_((
                'pending', 'uploading', 'orphaned', 'retained', 'reclaiming',
                'delete_requested', 'deleting', 'delete_failed',
            )),
            or_(
                and_(
                    FileRecordRow.status.in_(('reclaiming', 'deleting')),
                    or_(
                        FileRecordRow.recovery_lease_until.is_(None),
                        FileRecordRow.recovery_lease_until < now,
                    ),
                ),
                FileRecordRow.status == 'delete_requested',
                and_(
                    FileRecordRow.status == 'delete_failed',
                    or_(
                        FileRecordRow.delete_next_attempt_at.is_(None),
                        FileRecordRow.delete_next_attempt_at <= now,
                    ),
                ),
                and_(
                    FileRecordRow.status.not_in((
                        'reclaiming', 'deleting', 'delete_requested',
                        'delete_failed',
                    )),
                    FileRecordRow.updated_at < str(updated_before),
                ),
            ),
        ).order_by(
            FileRecordRow.updated_at,
            FileRecordRow.file_id,
        ).limit(min(max(int(limit), 1), 1000)).with_for_update(skip_locked=True)
        claimed = []
        async with self.sessions() as session:
            rows = (await session.execute(statement)).scalars().all()
            for row in rows:
                if (
                    row.status in {'reclaiming', 'deleting'}
                    and row.recovery_lease_until
                    and row.recovery_lease_until >= now
                ):
                    continue
                if (
                    row.status == 'retained'
                    and row.retention_until
                    and row.retention_until > now
                ):
                    continue
                row.status = 'deleting' if row.delete_request_id else 'reclaiming'
                if row.delete_request_id:
                    row.delete_attempts = int(row.delete_attempts or 0) + 1
                    row.delete_next_attempt_at = None
                row.recovery_token = token
                row.recovery_lease_until = lease_until
                row.revision += 1
                row.updated_at = now
                claimed.append(row)
            await session.commit()
            return [self._file_record_dict(row) for row in claimed]

    async def finish_file_recovery(
        self,
        file_id,
        recovery_token,
        status,
        error=None,
        retention_until=None,
        next_attempt_at=None,
    ):
        if status not in {
            'orphaned', 'retained', 'delete_failed', 'delete_dead_letter',
            'deleted',
        }:
            raise ValueError('invalid file recovery status')
        async with self.sessions() as session:
            row = await session.get(FileRecordRow, str(file_id), with_for_update=True)
            if row is None:
                raise ValueError('file upload not found')
            claimed_status = row.status
            if claimed_status not in {'reclaiming', 'deleting'}:
                raise RuntimeError(f'file is not claimed for recovery: {row.status}')
            if row.recovery_token != str(recovery_token):
                raise RuntimeError('file recovery token is stale')
            if claimed_status == 'deleting' and status == 'orphaned':
                raise RuntimeError('explicit file deletion cannot become orphaned')
            if claimed_status == 'reclaiming' and status in {
                'delete_failed', 'delete_dead_letter',
            }:
                raise RuntimeError('orphan recovery cannot enter delete_failed')
            now = datetime.now(timezone.utc).isoformat()
            if status == 'delete_failed':
                maximum_attempts = _configured_int(
                    'ARTIFACT_DELETE_MAX_ATTEMPTS',
                    8,
                    1,
                )
                if int(row.delete_attempts or 0) >= maximum_attempts:
                    status = 'delete_dead_letter'
                    next_attempt_at = None
                elif next_attempt_at is None:
                    base_seconds = _configured_int(
                        'ARTIFACT_DELETE_RETRY_BASE_SECONDS',
                        60,
                        1,
                    )
                    maximum_seconds = _configured_int(
                        'ARTIFACT_DELETE_RETRY_MAX_SECONDS',
                        21600,
                        base_seconds,
                    )
                    delay = min(
                        base_seconds * (2 ** max(int(row.delete_attempts or 1) - 1, 0)),
                        maximum_seconds,
                    )
                    next_attempt_at = (
                        datetime.now(timezone.utc) + timedelta(seconds=delay)
                    ).isoformat()
            if status == 'deleted':
                await self._release_storage(
                    session,
                    row.storage_reservation_id,
                    now,
                )
                row.deleted_at = now
            row.status = status
            row.last_error = str(error or '')[:2048] or None
            row.retention_until = retention_until
            row.delete_next_attempt_at = (
                str(next_attempt_at) if next_attempt_at is not None else None
            )
            row.recovery_token = None
            row.recovery_lease_until = None
            row.revision += 1
            row.updated_at = now
            if row.delete_request_id:
                await self._append_storage_deletion_event(
                    session,
                    resource_type='file',
                    resource_id=row.file_id,
                    project_id=row.project_id,
                    job_id=None,
                    request_id=row.delete_request_id,
                    actor='system:artifact-recovery',
                    status=status,
                    attempt=row.delete_attempts,
                    error=row.last_error,
                    created_at=now,
                )
            await session.commit()
            return self._file_record_dict(row)

    @staticmethod
    def _file_record_dict(row):
        return {
            'file_id': row.file_id,
            'project_id': row.project_id,
            'filename': row.filename,
            'storage_backend': row.storage_backend,
            'storage_key': row.storage_key,
            'version_id': row.version_id,
            'sha256': row.sha256,
            'size_bytes': row.size_bytes,
            'status': row.status,
            'last_error': row.last_error,
            'storage_reservation_id': row.storage_reservation_id,
            'recovery_token': row.recovery_token,
            'recovery_lease_until': row.recovery_lease_until,
            'retention_until': row.retention_until,
            'delete_request_id': row.delete_request_id,
            'delete_requested_by': row.delete_requested_by,
            'delete_requested_at': row.delete_requested_at,
            'deleted_at': row.deleted_at,
            'delete_attempts': row.delete_attempts,
            'delete_next_attempt_at': row.delete_next_attempt_at,
            'revision': row.revision,
            'created_at': row.created_at,
            'updated_at': row.updated_at,
        }

    async def assign_file_project(
        self,
        file_id,
        project_id,
        created_at,
        *,
        filename=None,
        storage_backend='unknown',
        storage_key=None,
        version_id=None,
        sha256=None,
        size_bytes=None,
        status='active',
        last_error=None,
    ):
        async with self.sessions() as session:
            project = await session.get(ProjectRow, str(project_id))
            if project is None:
                raise ValueError(f'project not found: {project_id}')
            row = await session.get(FileProjectRow, str(file_id))
            if row is None:
                session.add(FileProjectRow(
                    file_id=str(file_id),
                    project_id=str(project_id),
                    created_at=created_at,
                ))
            elif row.project_id != str(project_id):
                raise ValueError('file already belongs to another project')
            file_record = await session.get(FileRecordRow, str(file_id))
            if file_record is None:
                file_record = FileRecordRow(
                    file_id=str(file_id),
                    project_id=str(project_id),
                    filename=str(filename) if filename is not None else None,
                    storage_backend=str(storage_backend),
                    storage_key=str(storage_key) if storage_key is not None else None,
                    version_id=str(version_id) if version_id is not None else None,
                    sha256=str(sha256) if sha256 is not None else None,
                    size_bytes=int(size_bytes) if size_bytes is not None else None,
                    status=str(status),
                    last_error=str(last_error)[:2000] if last_error else None,
                    created_at=str(created_at),
                    updated_at=str(created_at),
                )
                session.add(file_record)
            elif file_record.project_id != str(project_id):
                raise ValueError('file already belongs to another project')
            else:
                file_record.filename = str(filename) if filename is not None else file_record.filename
                file_record.storage_backend = str(storage_backend)
                file_record.storage_key = str(storage_key) if storage_key is not None else file_record.storage_key
                file_record.version_id = str(version_id) if version_id is not None else file_record.version_id
                file_record.sha256 = str(sha256) if sha256 is not None else file_record.sha256
                file_record.size_bytes = int(size_bytes) if size_bytes is not None else file_record.size_bytes
                file_record.status = str(status)
                file_record.last_error = str(last_error)[:2000] if last_error else None
                file_record.updated_at = str(created_at)
            await session.commit()

    async def get_file_project(self, file_id):
        async with self.sessions() as session:
            row = await session.get(FileRecordRow, str(file_id))
            if row is not None:
                return row.project_id
            row = await session.get(FileProjectRow, str(file_id))
            return row.project_id if row else None

    async def get_file_record(self, file_id):
        async with self.sessions() as session:
            row = await session.get(FileRecordRow, str(file_id))
            if row is None:
                return None
            return self._file_record_dict(row)

    async def close(self):
        await self.engine.dispose()
