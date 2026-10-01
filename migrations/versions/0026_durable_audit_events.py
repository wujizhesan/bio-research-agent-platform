"""add durable append-only security audit events"""

from alembic import op
import sqlalchemy as sa


revision = '0026_durable_audit_events'
down_revision = '0025_storage_deletion_operations'
branch_labels = None
depends_on = None


API_INSERT = (
    "pg_has_role(session_user, 'bioagent_api', 'member') AND "
    "bioagent_signed_context_valid() AND actor = bioagent_context_subject()"
)
MAINTENANCE = "pg_has_role(session_user, 'bioagent_maintenance', 'member')"


def upgrade():
    op.create_table(
        'audit_events',
        sa.Column('event_id', sa.String(length=32), primary_key=True),
        sa.Column('at', sa.String(length=64), nullable=False),
        sa.Column('request_id', sa.String(length=128), nullable=True),
        sa.Column('trace_id', sa.String(length=128), nullable=True),
        sa.Column('actor', sa.String(length=200), nullable=False),
        sa.Column('roles', sa.JSON(), nullable=False),
        sa.Column('action', sa.String(length=128), nullable=False),
        sa.Column('resource_type', sa.String(length=64), nullable=False),
        sa.Column('resource_id', sa.String(length=256), nullable=True),
        sa.Column('metadata', sa.JSON(), nullable=False),
        sa.Column('event_hash', sa.String(length=64), nullable=False, unique=True),
    )
    for column in (
        'at', 'request_id', 'trace_id', 'actor', 'action', 'resource_type',
        'resource_id',
    ):
        op.create_index(f'ix_audit_events_{column}', 'audit_events', [column])
    op.execute(sa.text('ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text('ALTER TABLE audit_events FORCE ROW LEVEL SECURITY'))
    op.execute(sa.text(
        'CREATE POLICY audit_events_api_insert ON audit_events '
        'FOR INSERT WITH CHECK (' + API_INSERT + ')'
    ))
    op.execute(sa.text(
        'CREATE POLICY audit_events_maintenance_read ON audit_events '
        'FOR SELECT USING (' + MAINTENANCE + ')'
    ))
    op.execute(sa.text('REVOKE ALL ON audit_events FROM PUBLIC'))
    op.execute(sa.text('GRANT INSERT ON audit_events TO bioagent_api'))
    op.execute(sa.text('GRANT SELECT ON audit_events TO bioagent_maintenance'))


def downgrade():
    op.drop_table('audit_events')
