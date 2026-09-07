"""add resource-aware scheduling fields"""

from alembic import op
import sqlalchemy as sa


revision = '0004_job_resources'
down_revision = '0003_project_workspaces'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'job_records',
        sa.Column('resources', sa.JSON(), nullable=False, server_default='{}'),
    )
    op.add_column(
        'job_records',
        sa.Column('priority', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade():
    op.drop_column('job_records', 'priority')
    op.drop_column('job_records', 'resources')
