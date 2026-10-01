"""persist pinned and actual job execution identity"""

from alembic import op
import sqlalchemy as sa


revision = '0008_job_execution_identity'
down_revision = '0007_job_outbox'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'job_records',
        sa.Column('execution_identity', sa.JSON(), nullable=True),
    )
    op.add_column(
        'job_records',
        sa.Column('routing', sa.JSON(), nullable=True),
    )
    op.add_column(
        'job_records',
        sa.Column('execution', sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column('job_records', 'execution')
    op.drop_column('job_records', 'routing')
    op.drop_column('job_records', 'execution_identity')
