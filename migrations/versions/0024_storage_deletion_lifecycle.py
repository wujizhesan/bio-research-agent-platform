"""add explicit storage deletion lifecycle"""

from alembic import op
import sqlalchemy as sa


revision = '0024_storage_deletion_lifecycle'
down_revision = '0023_legacy_artifact_backfill'
branch_labels = None
depends_on = None


MAINTENANCE = "pg_has_role(session_user, 'bioagent_maintenance', 'member')"
API_OWNER = (
    "pg_has_role(session_user, 'bioagent_api', 'member') AND ("
    "bioagent_context_is_admin() OR "
    "bioagent_current_project_role(project_id) = 'owner')"
)


def _add_columns(table):
    op.add_column(
        table,
        sa.Column('delete_request_id', sa.String(length=128), nullable=True),
    )
    op.add_column(
        table,
        sa.Column('delete_requested_by', sa.String(length=200), nullable=True),
    )
    op.add_column(
        table,
        sa.Column('delete_requested_at', sa.String(length=64), nullable=True),
    )
    op.add_column(
        table,
        sa.Column('deleted_at', sa.String(length=64), nullable=True),
    )
    op.create_index(
        f'ix_{table}_delete_request_id',
        table,
        ['delete_request_id'],
    )


def upgrade():
    _add_columns('file_records')
    _add_columns('job_artifacts')
    op.execute(sa.text(
        'CREATE POLICY job_artifacts_api_delete_request ON job_artifacts '
        'FOR UPDATE USING (' + API_OWNER + ') WITH CHECK (' + API_OWNER + ' AND '
        "delete_request_id IS NOT NULL AND status IN ('delete_requested', "
        "'deleting', 'delete_failed', 'retained', 'deleted'))"
    ))
    op.execute(sa.text(
        'CREATE POLICY file_records_delete_owner_guard ON file_records AS '
        'RESTRICTIVE FOR UPDATE USING (delete_request_id IS NULL OR '
        + MAINTENANCE + ' OR ' + API_OWNER + ') WITH CHECK ('
        'delete_request_id IS NULL OR ' + MAINTENANCE + ' OR ' + API_OWNER + ')'
    ))
    op.execute(sa.text(
        'GRANT UPDATE (status, delete_request_id, delete_requested_by, '
        'delete_requested_at, deleted_at, revision, updated_at, last_error, '
        'retention_until) ON job_artifacts TO bioagent_api'
    ))
    op.execute(sa.text(
        'GRANT UPDATE (deleted_at) ON file_records, job_artifacts '
        'TO bioagent_maintenance'
    ))
    op.execute(sa.text(
        'GRANT EXECUTE ON FUNCTION bioagent_context_is_admin(), '
        'bioagent_current_project_role(varchar) '
        'TO bioagent_maintenance, bioagent_worker'
    ))


def _drop_columns(table):
    op.drop_index(f'ix_{table}_delete_request_id', table_name=table)
    op.drop_column(table, 'deleted_at')
    op.drop_column(table, 'delete_requested_at')
    op.drop_column(table, 'delete_requested_by')
    op.drop_column(table, 'delete_request_id')


def downgrade():
    op.execute(sa.text(
        'REVOKE EXECUTE ON FUNCTION bioagent_context_is_admin(), '
        'bioagent_current_project_role(varchar) '
        'FROM bioagent_maintenance, bioagent_worker'
    ))
    op.execute(sa.text(
        'REVOKE UPDATE (deleted_at) ON file_records, job_artifacts '
        'FROM bioagent_maintenance'
    ))
    op.execute(sa.text(
        'REVOKE UPDATE (status, delete_request_id, delete_requested_by, '
        'delete_requested_at, deleted_at, revision, updated_at, last_error, '
        'retention_until) ON job_artifacts FROM bioagent_api'
    ))
    op.execute(sa.text(
        'DROP POLICY IF EXISTS file_records_delete_owner_guard ON file_records'
    ))
    op.execute(sa.text(
        'DROP POLICY IF EXISTS job_artifacts_api_delete_request ON job_artifacts'
    ))
    _drop_columns('job_artifacts')
    _drop_columns('file_records')
