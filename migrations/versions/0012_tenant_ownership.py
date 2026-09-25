"""expand job and file tenant ownership storage"""

from alembic import op
import sqlalchemy as sa


revision = '0012_tenant_ownership'
down_revision = '0011_job_resolution'
branch_labels = None
depends_on = None

def upgrade():
    op.execute(sa.text(
        "INSERT INTO projects "
        "(project_id, name, description, owner_subject, created_at) "
        "SELECT 'system-legacy', 'Legacy system resources', "
        "'Resources created before mandatory tenant ownership', 'system', "
        "'1970-01-01T00:00:00+00:00' WHERE NOT EXISTS "
        "(SELECT 1 FROM projects WHERE project_id = 'system-legacy')"
    ))
    op.add_column(
        'job_records',
        sa.Column(
            'project_id',
            sa.String(length=64),
            sa.ForeignKey('projects.project_id', ondelete='RESTRICT'),
            nullable=False,
            server_default='system-legacy',
        ),
    )
    op.create_index('ix_job_records_project_id', 'job_records', ['project_id'])

    op.create_table(
        'file_records',
        sa.Column('file_id', sa.String(length=64), nullable=False),
        sa.Column('project_id', sa.String(length=64), nullable=False),
        sa.Column('filename', sa.String(length=180), nullable=True),
        sa.Column('storage_backend', sa.String(length=32), nullable=False),
        sa.Column('storage_key', sa.Text(), nullable=True),
        sa.Column('version_id', sa.String(length=256), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('size_bytes', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.String(length=64), nullable=False),
        sa.Column('updated_at', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ['project_id'],
            ['projects.project_id'],
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('file_id'),
    )
    op.create_index('ix_file_records_project_id', 'file_records', ['project_id'])
    op.create_index('ix_file_records_status', 'file_records', ['status'])

    op.execute(sa.text(
        "UPDATE job_records SET project_id = "
        "(SELECT job_projects.project_id FROM job_projects "
        "WHERE job_projects.job_id = job_records.job_id) "
        "WHERE EXISTS (SELECT 1 FROM job_projects "
        "WHERE job_projects.job_id = job_records.job_id)"
    ))
    op.execute(sa.text(
        "INSERT INTO file_records "
        "(file_id, project_id, storage_backend, status, created_at, updated_at) "
        "SELECT file_projects.file_id, file_projects.project_id, "
        "'unknown', 'active', file_projects.created_at, file_projects.created_at "
        "FROM file_projects LEFT JOIN file_records "
        "ON file_records.file_id = file_projects.file_id "
        "WHERE file_records.file_id IS NULL"
    ))


def downgrade():
    op.drop_index('ix_file_records_status', table_name='file_records')
    op.drop_index('ix_file_records_project_id', table_name='file_records')
    op.drop_table('file_records')
    op.drop_index('ix_job_records_project_id', table_name='job_records')
    op.drop_column('job_records', 'project_id')
