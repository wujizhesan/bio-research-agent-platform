"""Async relational persistence for the service layer."""
from pathlib import Path
import os

from sqlalchemy import Boolean, Float, Integer, JSON, String, Text, inspect, or_, select, text
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TERMINAL_STATUSES = {'completed', 'failed', 'cancelled'}


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
    }


def _coalesced_values(records):
    values = {}
    for record in records:
        values[record['job_id']] = _row_values(record)
    return list(values.values())


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
    for field in ('started_at', 'finished_at', 'result', 'error', 'retry_of'):
        value = getattr(row, field)
        if value is not None:
            output[field] = value
    output['attempts'] = row.attempts
    if row.cancel_requested:
        output['cancel_requested'] = True
    return output


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
        }
        boolean_default = 'FALSE' if connection.dialect.name == 'postgresql' else '0'
        missing['cancel_requested'] = f'BOOLEAN NOT NULL DEFAULT {boolean_default}'
        for name, definition in missing.items():
            if name not in columns:
                connection.execute(text(f'ALTER TABLE job_records ADD COLUMN {name} {definition}'))

    async def ping(self):
        async with self.engine.connect() as connection:
            await connection.execute(text('SELECT 1'))

    async def upsert_job(self, record):
        await self.upsert_jobs([record])

    async def upsert_jobs(self, records):
        values_list = _coalesced_values(records)
        if not values_list:
            return
        if not self.url.startswith('postgresql'):
            for values in values_list:
                await self._upsert_sqlite_values(values)
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
            await session.commit()

    async def _upsert_sqlite_values(self, values):
        async with self.sessions() as session:
            row = await session.get(JobRow, values['job_id'])
            if row is None:
                try:
                    session.add(JobRow(**values))
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
            await session.commit()

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

    async def close(self):
        await self.engine.dispose()
