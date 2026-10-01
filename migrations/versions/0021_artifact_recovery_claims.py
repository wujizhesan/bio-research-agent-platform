"""add leased artifact recovery claims"""

from alembic import op
import sqlalchemy as sa


revision = '0021_artifact_recovery_claims'
down_revision = '0020_job_artifact_lifecycle'
branch_labels = None
depends_on = None


MAINTENANCE_ACCESS = (
    "pg_has_role(session_user, 'bioagent_maintenance', 'member')"
)
JOB_ARTIFACTS_MAINTENANCE_POLICY = (
    'CREATE POLICY job_artifacts_maintenance ON job_artifacts FOR ALL USING ('
    + MAINTENANCE_ACCESS + ') WITH CHECK (' + MAINTENANCE_ACCESS + ')'
)
EXECUTION_RESULTS_MAINTENANCE_POLICY = (
    'CREATE POLICY job_execution_results_maintenance_read ON '
    'job_execution_results FOR SELECT USING (' + MAINTENANCE_ACCESS + ')'
)
JOB_RECORDS_MAINTENANCE_POLICY = (
    'CREATE POLICY job_records_maintenance_read ON job_records '
    'FOR SELECT USING (' + MAINTENANCE_ACCESS + ')'
)


def upgrade():
    op.add_column(
        'job_artifacts',
        sa.Column('recovery_token', sa.String(length=128), nullable=True),
    )
    op.add_column(
        'job_artifacts',
        sa.Column('recovery_lease_until', sa.String(length=64), nullable=True),
    )
    op.add_column(
        'job_artifacts',
        sa.Column(
            'revision',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('0'),
        ),
    )
    op.create_index(
        'ix_job_artifacts_recovery_scan',
        'job_artifacts',
        ['status', 'updated_at'],
        postgresql_where=sa.text(
            "status IN ('reserved', 'uploaded', 'orphaned', "
            "'retained', 'reclaiming')"
        ),
        sqlite_where=sa.text(
            "status IN ('reserved', 'uploaded', 'orphaned', "
            "'retained', 'reclaiming')"
        ),
    )
    op.execute(sa.text(JOB_ARTIFACTS_MAINTENANCE_POLICY))
    op.execute(sa.text(EXECUTION_RESULTS_MAINTENANCE_POLICY))
    op.execute(sa.text(JOB_RECORDS_MAINTENANCE_POLICY))
    op.execute(sa.text(
        'GRANT USAGE ON SCHEMA public TO bioagent_maintenance'
    ))
    op.execute(sa.text(
        'GRANT SELECT ON job_records, job_execution_results, job_artifacts '
        'TO bioagent_maintenance'
    ))
    op.execute(sa.text(
        'GRANT UPDATE (status, recovery_token, recovery_lease_until, revision, '
        'retention_until, last_error, updated_at) ON job_artifacts '
        'TO bioagent_maintenance'
    ))
    op.execute(sa.text(
        'GRANT EXECUTE ON FUNCTION bioagent_api_can_access_project('
        'varchar, boolean), bioagent_api_can_access_job(varchar, boolean), '
        'bioagent_worker_can_access_job(varchar) TO bioagent_maintenance'
    ))


def downgrade():
    op.execute(sa.text(
        'REVOKE EXECUTE ON FUNCTION bioagent_api_can_access_project('
        'varchar, boolean), bioagent_api_can_access_job(varchar, boolean), '
        'bioagent_worker_can_access_job(varchar) FROM bioagent_maintenance'
    ))
    op.execute(sa.text(
        'REVOKE ALL ON job_artifacts, job_execution_results, job_records '
        'FROM bioagent_maintenance'
    ))
    op.execute(sa.text(
        'DROP POLICY IF EXISTS job_records_maintenance_read ON job_records'
    ))
    op.execute(sa.text(
        'DROP POLICY IF EXISTS job_execution_results_maintenance_read '
        'ON job_execution_results'
    ))
    op.execute(sa.text(
        'DROP POLICY IF EXISTS job_artifacts_maintenance ON job_artifacts'
    ))
    op.drop_index('ix_job_artifacts_recovery_scan', table_name='job_artifacts')
    op.drop_column('job_artifacts', 'revision')
    op.drop_column('job_artifacts', 'recovery_lease_until')
    op.drop_column('job_artifacts', 'recovery_token')
