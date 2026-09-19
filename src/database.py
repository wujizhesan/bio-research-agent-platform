"""Async relational persistence for the service layer."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os

from sqlalchemy import Boolean, Float, ForeignKey, Integer, JSON, String, Text, delete, inspect, or_, select, text
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TERMINAL_STATUSES = {'completed', 'failed', 'cancelled', 'indeterminate'}


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
    tool: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    started_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    arguments: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
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


class JobOutboxRow(Base):
    __tablename__ = 'job_outbox'

    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey('job_records.job_id', ondelete='CASCADE'), primary_key=True,
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    dispatched_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


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


def _row_values(record):
    return {
        'job_id': record['job_id'],
        'tool': record['tool'],
        'status': record['status'],
        'created_at': record['created_at'],
        'started_at': record.get('started_at'),
        'finished_at': record.get('finished_at'),
        'arguments': record.get('_arguments', {}),
        'result': record.get('result'),
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
        'revision': revision,
    }
    for key in (
        'started_at', 'finished_at', 'result', 'error', 'retry_of',
        'trace_id', 'request_id', 'run_context', 'execution_identity',
        'routing', 'execution', 'resolution',
    ):
        if values.get(key) is not None:
            payload[key] = values[key]
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
        'tool': row.tool,
        'status': row.status,
        'created_at': row.created_at,
    }
    for field in (
        'started_at', 'finished_at', 'result', 'error', 'retry_of',
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
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def init_schema(self):
        auto_create = os.environ.get('AUTO_CREATE_SCHEMA', 'true').lower() in {'1', 'true', 'yes'}
        if auto_create:
            async with self.engine.begin() as connection:
                await connection.run_sync(self._create_and_upgrade_schema)

    @staticmethod
    def _create_and_upgrade_schema(connection):
        Base.metadata.create_all(connection)
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
        }
        boolean_default = 'FALSE' if connection.dialect.name == 'postgresql' else '0'
        missing['cancel_requested'] = f'BOOLEAN NOT NULL DEFAULT {boolean_default}'
        for name, definition in missing.items():
            if name not in columns:
                connection.execute(text(f'ALTER TABLE job_records ADD COLUMN {name} {definition}'))
        indexes = {
            item['name'] for item in inspect(connection).get_indexes('job_records')
        }
        if 'ix_job_records_trace_id' not in indexes:
            connection.execute(text(
                'CREATE INDEX ix_job_records_trace_id ON job_records (trace_id)'
            ))

    async def ping(self):
        async with self.engine.connect() as connection:
            await connection.execute(text('SELECT 1'))

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

    async def stage_job(self, record):
        values = _row_values(record)
        payload = dict(record)
        async with self.sessions() as session:
            row = await session.get(JobRow, values['job_id'])
            if row is None:
                row = JobRow(**values)
                session.add(row)
            elif row.status not in TERMINAL_STATUSES or values['status'] in TERMINAL_STATUSES:
                for key, value in values.items():
                    setattr(row, key, value)
            outbox = await session.get(JobOutboxRow, values['job_id'])
            if values['status'] in TERMINAL_STATUSES:
                if outbox is not None:
                    await session.delete(outbox)
            elif outbox is None:
                session.add(JobOutboxRow(
                    job_id=values['job_id'],
                    payload=payload,
                    created_at=values['created_at'],
                ))
            else:
                outbox.payload = payload
                outbox.last_error = None
            await self._persist_job_events(session, [record])
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
            .where(~JobRow.status.in_(TERMINAL_STATUSES))
            .order_by(JobOutboxRow.created_at)
            .limit(size)
        )
        async with self.sessions() as session:
            rows = (await session.execute(statement)).all()
            records = []
            for outbox, job in rows:
                record = dict(outbox.payload or {})
                record.update({
                    'job_id': job.job_id,
                    'tool': job.tool,
                    'status': job.status,
                    'created_at': job.created_at,
                    '_attempts': job.attempts,
                    '_cancel_requested': job.cancel_requested,
                    'resources': job.resources or {},
                    'priority': job.priority,
                })
                if job.run_context is not None:
                    record['run_context'] = job.run_context
                records.append(record)
            return records

    async def upsert_jobs(self, records):
        records = list(records)
        values_list = _coalesced_values(records)
        if not values_list:
            return
        if not self.url.startswith('postgresql'):
            for record in records:
                await self._upsert_sqlite_values(record, _row_values(record))
            return
        async with self.sessions() as session:
            statement = postgres_insert(JobRow).values(values_list)
            updates = {
                key: getattr(statement.excluded, key)
                for key in values_list[0]
                if key != 'job_id'
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
                setattr(row, key, value)
            await self._persist_job_events(session, [record])
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
                        .where(
                            JobEventRow.job_id == selected_job_id,
                            JobEventRow.terminal.is_(True),
                        )
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

    async def get_job(self, job_id):
        async with self.sessions() as session:
            row = await session.get(JobRow, str(job_id))
            return _public_row(row) if row else None

    async def list_jobs(self, limit=20):
        size = min(max(int(limit), 1), 100)
        statement = select(JobRow).order_by(JobRow.created_at.desc()).limit(size)
        async with self.sessions() as session:
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

    async def assign_job_project(self, job_id, project_id, created_at):
        async with self.sessions() as session:
            row = await session.get(JobProjectRow, str(job_id))
            if row is None:
                session.add(JobProjectRow(
                    job_id=str(job_id),
                    project_id=str(project_id),
                    created_at=created_at,
                ))
            else:
                row.project_id = str(project_id)
            await session.commit()

    async def get_job_project(self, job_id):
        async with self.sessions() as session:
            row = await session.get(JobProjectRow, str(job_id))
            return row.project_id if row else None

    async def assign_file_project(self, file_id, project_id, created_at):
        async with self.sessions() as session:
            row = await session.get(FileProjectRow, str(file_id))
            if row is None:
                session.add(FileProjectRow(
                    file_id=str(file_id),
                    project_id=str(project_id),
                    created_at=created_at,
                ))
            else:
                row.project_id = str(project_id)
            await session.commit()

    async def get_file_project(self, file_id):
        async with self.sessions() as session:
            row = await session.get(FileProjectRow, str(file_id))
            return row.project_id if row else None

    async def close(self):
        await self.engine.dispose()
