"""add unified job run context"""

from alembic import op
import sqlalchemy as sa


revision = '0006_job_run_context'
down_revision = '0005_job_observability'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'job_records',
        sa.Column('run_context', sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column('job_records', 'run_context')
