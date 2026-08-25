"""add project workspaces and resource ownership"""
from alembic import op
import sqlalchemy as sa


revision = '0003_project_workspaces'
down_revision = '0002_job_execution_state'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'projects',
        sa.Column('project_id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('owner_subject', sa.String(length=200), nullable=False),
        sa.Column('created_at', sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint('project_id'),
    )
    op.create_index('ix_projects_owner_subject', 'projects', ['owner_subject'])
    op.create_index('ix_projects_created_at', 'projects', ['created_at'])

    op.create_table(
        'project_members',
        sa.Column('project_id', sa.String(length=64), nullable=False),
        sa.Column('subject', sa.String(length=200), nullable=False),
        sa.Column('role', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.project_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('project_id', 'subject'),
    )

    op.create_table(
        'job_projects',
        sa.Column('job_id', sa.String(length=64), nullable=False),
        sa.Column('project_id', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['job_records.job_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.project_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('job_id'),
    )
    op.create_index('ix_job_projects_project_id', 'job_projects', ['project_id'])

    op.create_table(
        'file_projects',
        sa.Column('file_id', sa.String(length=64), nullable=False),
        sa.Column('project_id', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.project_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('file_id'),
    )
    op.create_index('ix_file_projects_project_id', 'file_projects', ['project_id'])


def downgrade():
    op.drop_index('ix_file_projects_project_id', table_name='file_projects')
    op.drop_table('file_projects')
    op.drop_index('ix_job_projects_project_id', table_name='job_projects')
    op.drop_table('job_projects')
    op.drop_table('project_members')
    op.drop_index('ix_projects_created_at', table_name='projects')
    op.drop_index('ix_projects_owner_subject', table_name='projects')
    op.drop_table('projects')
