"""add job artifact lifecycle records"""

from alembic import op
import sqlalchemy as sa


revision = '0020_job_artifact_lifecycle'
down_revision = '0019_job_artifacts_manifest'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'job_artifacts',
        sa.Column('publication_id', sa.String(length=64), primary_key=True),
        sa.Column('artifact_id', sa.String(length=32), nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=False),
        sa.Column('project_id', sa.String(length=64), nullable=False),
        sa.Column('execution_key', sa.String(length=64), nullable=False),
        sa.Column('fencing_token', sa.String(length=128), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('parameter', sa.String(length=128), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('storage_backend', sa.String(length=32), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('content_type', sa.String(length=255), nullable=True),
        sa.Column('size_bytes', sa.BigInteger(), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('path', sa.Text(), nullable=True),
        sa.Column('storage_key', sa.Text(), nullable=True),
        sa.Column('version_id', sa.String(length=1024), nullable=True),
        sa.Column('reference', sa.Text(), nullable=True),
        sa.Column('retention_until', sa.String(length=64), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.String(length=64), nullable=False),
        sa.Column('updated_at', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['job_records.job_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.project_id'], ondelete='RESTRICT'),
    )
    op.create_index('ix_job_artifacts_artifact_id', 'job_artifacts', ['artifact_id'])
    op.create_index('ix_job_artifacts_job_id', 'job_artifacts', ['job_id'])
    op.create_index('ix_job_artifacts_project_id', 'job_artifacts', ['project_id'])
    op.create_index('ix_job_artifacts_execution_key', 'job_artifacts', ['execution_key'])
    op.create_index('ix_job_artifacts_status', 'job_artifacts', ['status'])
    op.create_index('ix_job_artifacts_created_at', 'job_artifacts', ['created_at'])
    op.create_index('ix_job_artifacts_updated_at', 'job_artifacts', ['updated_at'])
    op.execute(sa.text('ALTER TABLE job_artifacts ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text('ALTER TABLE job_artifacts FORCE ROW LEVEL SECURITY'))
    op.execute(sa.text(
        'CREATE POLICY job_artifacts_tenant_read ON job_artifacts FOR SELECT USING ('
        'bioagent_api_can_access_job(job_id, false) '
        'OR bioagent_worker_can_access_job(job_id))'
    ))
    op.execute(sa.text(
        'CREATE POLICY job_artifacts_worker_write ON job_artifacts FOR ALL USING ('
        'bioagent_worker_can_access_job(job_id)) WITH CHECK ('
        'bioagent_worker_can_access_job(job_id))'
    ))
    op.execute(sa.text('GRANT SELECT ON job_artifacts TO bioagent_api'))
    op.execute(sa.text(
        'GRANT SELECT, INSERT, UPDATE ON job_artifacts TO bioagent_worker'
    ))


def downgrade():
    op.execute(sa.text(
        'DROP POLICY IF EXISTS job_artifacts_worker_write ON job_artifacts'
    ))
    op.execute(sa.text(
        'DROP POLICY IF EXISTS job_artifacts_tenant_read ON job_artifacts'
    ))
    op.drop_table('job_artifacts')
