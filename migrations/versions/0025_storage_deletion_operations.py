"""add deletion retry scheduling and durable audit events"""

from alembic import op
import sqlalchemy as sa


revision = '0025_storage_deletion_operations'
down_revision = '0024_storage_deletion_lifecycle'
branch_labels = None
depends_on = None


MAINTENANCE = "pg_has_role(session_user, 'bioagent_maintenance', 'member')"
API_OWNER = (
    "pg_has_role(session_user, 'bioagent_api', 'member') AND ("
    "bioagent_context_is_admin() OR "
    "bioagent_current_project_role(project_id) = 'owner')"
)


def _add_retry_columns(table):
    op.add_column(
        table,
        sa.Column(
            'delete_attempts',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('0'),
        ),
    )
    op.add_column(
        table,
        sa.Column('delete_next_attempt_at', sa.String(length=64), nullable=True),
    )
    op.create_index(
        f'ix_{table}_delete_next_attempt_at',
        table,
        ['delete_next_attempt_at'],
    )


def upgrade():
    _add_retry_columns('file_records')
    _add_retry_columns('job_artifacts')
    op.create_table(
        'storage_deletion_events',
        sa.Column('event_id', sa.String(length=32), primary_key=True),
        sa.Column('sequence', sa.BigInteger(), nullable=False),
        sa.Column('resource_type', sa.String(length=32), nullable=False),
        sa.Column('resource_id', sa.String(length=64), nullable=False),
        sa.Column('project_id', sa.String(length=64), nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=True),
        sa.Column('request_id', sa.String(length=128), nullable=False),
        sa.Column('actor', sa.String(length=200), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('previous_hash', sa.String(length=64), nullable=False),
        sa.Column('event_hash', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.String(length=64), nullable=False),
        sa.UniqueConstraint('event_hash'),
        sa.UniqueConstraint(
            'project_id',
            'sequence',
            name='uq_storage_deletion_events_project_sequence',
        ),
    )
    for column in (
        'resource_type', 'resource_id', 'project_id', 'job_id', 'request_id',
        'status', 'created_at',
    ):
        op.create_index(
            f'ix_storage_deletion_events_{column}',
            'storage_deletion_events',
            [column],
        )
    op.execute(sa.text(
        'ALTER TABLE storage_deletion_events ENABLE ROW LEVEL SECURITY'
    ))
    op.execute(sa.text(
        'ALTER TABLE storage_deletion_events FORCE ROW LEVEL SECURITY'
    ))
    op.execute(sa.text(
        'CREATE POLICY storage_deletion_events_api_read ON '
        'storage_deletion_events FOR SELECT USING (' + API_OWNER + ')'
    ))
    op.execute(sa.text(
        'CREATE POLICY storage_deletion_events_api_insert ON '
        'storage_deletion_events FOR INSERT WITH CHECK (' + API_OWNER + ')'
    ))
    op.execute(sa.text(
        'CREATE POLICY storage_deletion_events_maintenance ON '
        'storage_deletion_events FOR SELECT USING (' + MAINTENANCE + ')'
    ))
    op.execute(sa.text(
        'CREATE POLICY storage_deletion_events_maintenance_insert ON '
        'storage_deletion_events FOR INSERT WITH CHECK (' + MAINTENANCE + ')'
    ))
    op.execute(sa.text(
        'ALTER POLICY job_artifacts_api_delete_request ON job_artifacts '
        'USING (' + API_OWNER + ') WITH CHECK (' + API_OWNER + ' AND '
        "delete_request_id IS NOT NULL AND status IN ('delete_requested', "
        "'deleting', 'delete_failed', 'delete_dead_letter', 'retained', "
        "'deleted'))"
    ))
    op.execute(sa.text(
        'GRANT SELECT, INSERT ON storage_deletion_events '
        'TO bioagent_api, bioagent_maintenance'
    ))
    op.execute(sa.text(
        'GRANT UPDATE (delete_attempts, delete_next_attempt_at) '
        'ON file_records, job_artifacts TO bioagent_api, bioagent_maintenance'
    ))


def _drop_retry_columns(table):
    op.drop_index(f'ix_{table}_delete_next_attempt_at', table_name=table)
    op.drop_column(table, 'delete_next_attempt_at')
    op.drop_column(table, 'delete_attempts')


def downgrade():
    op.execute(sa.text(
        'REVOKE UPDATE (delete_attempts, delete_next_attempt_at) '
        'ON file_records, job_artifacts FROM bioagent_api, bioagent_maintenance'
    ))
    op.execute(sa.text(
        'ALTER POLICY job_artifacts_api_delete_request ON job_artifacts '
        'USING (' + API_OWNER + ') WITH CHECK (' + API_OWNER + ' AND '
        "delete_request_id IS NOT NULL AND status IN ('delete_requested', "
        "'deleting', 'delete_failed', 'retained', 'deleted'))"
    ))
    op.drop_table('storage_deletion_events')
    _drop_retry_columns('job_artifacts')
    _drop_retry_columns('file_records')
