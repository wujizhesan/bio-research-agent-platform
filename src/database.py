"""Async relational persistence for the service layer."""
from pathlib import Path
import os

from sqlalchemy import Boolean, Float, Integer, JSON, String, Text, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
        self.engine = create_async_engine(self.url, pool_pre_ping=True)
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
        values = _row_values(record)
        async with self.sessions() as session:
            row = await session.get(JobRow, values['job_id'])
            if row is None:
                session.add(JobRow(**values))
            else:
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
