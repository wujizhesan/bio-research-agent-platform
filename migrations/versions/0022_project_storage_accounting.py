"""add project storage accounting and file upload lifecycle"""

from alembic import op
import sqlalchemy as sa


revision = '0022_project_storage_accounting'
down_revision = '0021_artifact_recovery_claims'
branch_labels = None
depends_on = None


API_READ = "bioagent_api_can_access_project(project_id, false)"
API_WRITE = "bioagent_api_can_access_project(project_id, true)"
WORKER_PROJECT = (
    "EXISTS (SELECT 1 FROM job_records AS job "
    "WHERE job.job_id = current_setting('bioagent.worker_job_id', true) "
    "AND job.project_id = project_storage_usage.project_id "
    "AND bioagent_worker_can_access_job(job.job_id))"
)
WORKER_RESERVATION = (
    "job_id IS NOT NULL AND bioagent_worker_can_access_job(job_id)"
)
MAINTENANCE = "pg_has_role(session_user, 'bioagent_maintenance', 'member')"


def upgrade():
    op.add_column(
        'file_records',
        sa.Column('storage_reservation_id', sa.String(length=64), nullable=True),
    )
    op.add_column(
        'file_records',
        sa.Column('recovery_token', sa.String(length=128), nullable=True),
    )
    op.add_column(
        'file_records',
        sa.Column('recovery_lease_until', sa.String(length=64), nullable=True),
    )
    op.add_column(
        'file_records',
        sa.Column('retention_until', sa.String(length=64), nullable=True),
    )
    op.add_column(
        'file_records',
        sa.Column(
            'revision',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('0'),
        ),
    )
    op.add_column(
        'job_artifacts',
        sa.Column('storage_reservation_id', sa.String(length=64), nullable=True),
    )
    op.create_table(
        'project_storage_usage',
        sa.Column('project_id', sa.String(length=64), primary_key=True),
        sa.Column('quota_bytes', sa.BigInteger(), nullable=False),
        sa.Column('used_bytes', sa.BigInteger(), nullable=False),
        sa.Column('reserved_bytes', sa.BigInteger(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.String(length=64), nullable=False),
        sa.Column('updated_at', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ['project_id'], ['projects.project_id'], ondelete='CASCADE'
        ),
    )
    op.create_table(
        'storage_reservations',
        sa.Column('reservation_id', sa.String(length=64), primary_key=True),
        sa.Column('project_id', sa.String(length=64), nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=True),
        sa.Column('resource_kind', sa.String(length=32), nullable=False),
        sa.Column('resource_id', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('reserved_bytes', sa.BigInteger(), nullable=False),
        sa.Column('actual_bytes', sa.BigInteger(), nullable=True),
        sa.Column('expires_at', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.String(length=64), nullable=False),
        sa.Column('updated_at', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ['project_id'], ['projects.project_id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['job_id'], ['job_records.job_id'], ondelete='CASCADE'
        ),
    )
    op.create_index(
        'uq_storage_reservations_resource',
        'storage_reservations',
        ['resource_kind', 'resource_id'],
        unique=True,
    )
    op.create_index(
        'ix_storage_reservations_project_id',
        'storage_reservations',
        ['project_id'],
    )
    op.create_index(
        'ix_storage_reservations_job_id',
        'storage_reservations',
        ['job_id'],
    )
    op.create_index(
        'ix_storage_reservations_resource_id',
        'storage_reservations',
        ['resource_id'],
    )
    op.create_index(
        'ix_storage_reservations_status',
        'storage_reservations',
        ['status'],
    )
    op.create_index(
        'ix_storage_reservations_expires_at',
        'storage_reservations',
        ['expires_at'],
    )
    op.create_index(
        'ix_file_records_storage_reservation_id',
        'file_records',
        ['storage_reservation_id'],
    )
    op.create_index(
        'ix_file_records_recovery_scan',
        'file_records',
        ['status', 'updated_at'],
    )
    op.create_index(
        'ix_job_artifacts_storage_reservation_id',
        'job_artifacts',
        ['storage_reservation_id'],
    )
    op.execute(sa.text(
        "INSERT INTO storage_reservations (reservation_id, project_id, job_id, "
        "resource_kind, resource_id, status, reserved_bytes, actual_bytes, "
        "expires_at, created_at, updated_at) SELECT file_id, project_id, NULL, "
        "'file', file_id, CASE WHEN size_bytes IS NULL THEN 'reserved' ELSE "
        "'committed' END, CASE WHEN size_bytes IS NULL THEN 1 ELSE size_bytes END, "
        "size_bytes, updated_at, created_at, updated_at FROM file_records"
    ))
    op.execute(sa.text(
        "UPDATE file_records SET storage_reservation_id = file_id"
    ))
    op.execute(sa.text(
        "INSERT INTO storage_reservations (reservation_id, project_id, job_id, "
        "resource_kind, resource_id, status, reserved_bytes, actual_bytes, "
        "expires_at, created_at, updated_at) SELECT publication_id, project_id, "
        "job_id, 'artifact', publication_id, CASE WHEN size_bytes IS NULL THEN "
        "'reserved' ELSE 'committed' END, CASE WHEN size_bytes IS NULL THEN 1 "
        "ELSE size_bytes END, size_bytes, updated_at, created_at, updated_at "
        "FROM job_artifacts"
    ))
    op.execute(sa.text(
        "UPDATE job_artifacts SET storage_reservation_id = publication_id"
    ))
    op.execute(sa.text(
        "INSERT INTO project_storage_usage (project_id, quota_bytes, used_bytes, "
        "reserved_bytes, revision, created_at, updated_at) SELECT project_id, "
        "GREATEST(10737418240, SUM(CASE WHEN status = 'committed' THEN "
        "COALESCE(actual_bytes, 0) ELSE 0 END) + SUM(CASE WHEN status = "
        "'reserved' THEN reserved_bytes ELSE 0 END)), SUM(CASE WHEN status = "
        "'committed' THEN COALESCE(actual_bytes, 0) ELSE 0 END), SUM(CASE WHEN "
        "status = 'reserved' THEN reserved_bytes ELSE 0 END), 0, MIN(created_at), "
        "MAX(updated_at) FROM storage_reservations GROUP BY project_id"
    ))
    op.execute(sa.text(
        'ALTER TABLE project_storage_usage ENABLE ROW LEVEL SECURITY'
    ))
    op.execute(sa.text(
        'ALTER TABLE project_storage_usage FORCE ROW LEVEL SECURITY'
    ))
    op.execute(sa.text(
        'ALTER TABLE storage_reservations ENABLE ROW LEVEL SECURITY'
    ))
    op.execute(sa.text(
        'ALTER TABLE storage_reservations FORCE ROW LEVEL SECURITY'
    ))
    op.execute(sa.text(
        'CREATE POLICY project_storage_usage_api_read ON project_storage_usage '
        'FOR SELECT USING (' + API_READ + ')'
    ))
    op.execute(sa.text(
        'CREATE POLICY project_storage_usage_api_write ON project_storage_usage '
        'FOR ALL USING (' + API_WRITE + ') WITH CHECK (' + API_WRITE + ')'
    ))
    op.execute(sa.text(
        'CREATE POLICY project_storage_usage_worker ON project_storage_usage '
        'FOR ALL USING (' + WORKER_PROJECT + ') WITH CHECK (' + WORKER_PROJECT + ')'
    ))
    op.execute(sa.text(
        'CREATE POLICY project_storage_usage_maintenance ON project_storage_usage '
        'FOR ALL USING (' + MAINTENANCE + ') WITH CHECK (' + MAINTENANCE + ')'
    ))
    op.execute(sa.text(
        'CREATE POLICY storage_reservations_api ON storage_reservations FOR ALL '
        'USING (' + API_WRITE + ') WITH CHECK (' + API_WRITE + ')'
    ))
    op.execute(sa.text(
        'CREATE POLICY storage_reservations_worker ON storage_reservations FOR ALL '
        'USING (' + WORKER_RESERVATION + ') WITH CHECK (' + WORKER_RESERVATION + ')'
    ))
    op.execute(sa.text(
        'CREATE POLICY storage_reservations_maintenance ON storage_reservations '
        'FOR ALL USING (' + MAINTENANCE + ') WITH CHECK (' + MAINTENANCE + ')'
    ))
    op.execute(sa.text(
        'CREATE POLICY file_records_maintenance ON file_records FOR ALL USING ('
        + MAINTENANCE + ') WITH CHECK (' + MAINTENANCE + ')'
    ))
    op.execute(sa.text(
        'GRANT SELECT, INSERT, UPDATE ON project_storage_usage, '
        'storage_reservations TO bioagent_api, bioagent_worker'
    ))
    op.execute(sa.text(
        'GRANT SELECT, INSERT, UPDATE ON project_storage_usage, '
        'storage_reservations TO bioagent_maintenance'
    ))
    op.execute(sa.text(
        'GRANT SELECT ON file_records TO bioagent_maintenance'
    ))
    op.execute(sa.text(
        'GRANT UPDATE (status, last_error, recovery_token, recovery_lease_until, '
        'retention_until, revision, updated_at) ON file_records '
        'TO bioagent_maintenance'
    ))


def downgrade():
    op.execute(sa.text(
        'DROP POLICY IF EXISTS file_records_maintenance ON file_records'
    ))
    op.execute(sa.text(
        'DROP POLICY IF EXISTS storage_reservations_maintenance '
        'ON storage_reservations'
    ))
    op.execute(sa.text(
        'DROP POLICY IF EXISTS storage_reservations_worker ON storage_reservations'
    ))
    op.execute(sa.text(
        'DROP POLICY IF EXISTS storage_reservations_api ON storage_reservations'
    ))
    op.execute(sa.text(
        'DROP POLICY IF EXISTS project_storage_usage_maintenance '
        'ON project_storage_usage'
    ))
    op.execute(sa.text(
        'DROP POLICY IF EXISTS project_storage_usage_worker '
        'ON project_storage_usage'
    ))
    op.execute(sa.text(
        'DROP POLICY IF EXISTS project_storage_usage_api_write '
        'ON project_storage_usage'
    ))
    op.execute(sa.text(
        'DROP POLICY IF EXISTS project_storage_usage_api_read '
        'ON project_storage_usage'
    ))
    op.drop_table('storage_reservations')
    op.drop_table('project_storage_usage')
    op.drop_index(
        'ix_job_artifacts_storage_reservation_id',
        table_name='job_artifacts',
    )
    op.drop_column('job_artifacts', 'storage_reservation_id')
    op.drop_index('ix_file_records_recovery_scan', table_name='file_records')
    op.drop_index(
        'ix_file_records_storage_reservation_id',
        table_name='file_records',
    )
    op.drop_column('file_records', 'revision')
    op.drop_column('file_records', 'retention_until')
    op.drop_column('file_records', 'recovery_lease_until')
    op.drop_column('file_records', 'recovery_token')
    op.drop_column('file_records', 'storage_reservation_id')
