"""add durable job dispatch outbox"""

from alembic import op
import sqlalchemy as sa


revision = '0007_job_outbox'
down_revision = '0006_job_run_context'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'job_outbox',
        sa.Column('job_id', sa.String(length=64), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.String(length=64), nullable=False),
        sa.Column('dispatched_at', sa.String(length=64), nullable=True),
        sa.Column('dispatch_attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['job_id'], ['job_records.job_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('job_id'),
    )
    op.create_index('ix_job_outbox_created_at', 'job_outbox', ['created_at'])


def downgrade():
    op.drop_index('ix_job_outbox_created_at', table_name='job_outbox')
    op.drop_table('job_outbox')
