"""add job observability context"""

from alembic import op
import sqlalchemy as sa


revision = '0005_job_observability'
down_revision = '0004_job_resources'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'job_records',
        sa.Column('trace_id', sa.String(length=128), nullable=True),
    )
    op.add_column(
        'job_records',
        sa.Column('request_id', sa.String(length=128), nullable=True),
    )
    op.create_index(
        'ix_job_records_trace_id',
        'job_records',
        ['trace_id'],
        unique=False,
    )


def downgrade():
    op.drop_index('ix_job_records_trace_id', table_name='job_records')
    op.drop_column('job_records', 'request_id')
    op.drop_column('job_records', 'trace_id')
