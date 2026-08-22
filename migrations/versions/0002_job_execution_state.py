"""add job execution state columns"""
from alembic import op
import sqlalchemy as sa


revision = '0002_job_execution_state'
down_revision = '0001_create_job_records'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('job_records', sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('job_records', sa.Column('cancel_requested', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('job_records', sa.Column('worker_id', sa.String(length=128), nullable=True))
    op.add_column('job_records', sa.Column('lease_until', sa.Float(), nullable=True))


def downgrade():
    op.drop_column('job_records', 'lease_until')
    op.drop_column('job_records', 'worker_id')
    op.drop_column('job_records', 'cancel_requested')
    op.drop_column('job_records', 'attempts')
