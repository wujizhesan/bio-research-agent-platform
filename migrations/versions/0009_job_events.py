"""persist replayable job events"""

from alembic import op
import sqlalchemy as sa


revision = '0009_job_events'
down_revision = '0008_job_execution_identity'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'job_events',
        sa.Column('job_id', sa.String(length=64), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('event_id', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.String(length=64), nullable=False),
        sa.Column('terminal', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(['job_id'], ['job_records.job_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('job_id', 'revision'),
    )
    op.create_index('ix_job_events_event_id', 'job_events', ['event_id'])
    op.create_index('ix_job_events_status', 'job_events', ['status'])
    op.create_index('ix_job_events_created_at', 'job_events', ['created_at'])


def downgrade():
    op.drop_index('ix_job_events_created_at', table_name='job_events')
    op.drop_index('ix_job_events_status', table_name='job_events')
    op.drop_index('ix_job_events_event_id', table_name='job_events')
    op.drop_table('job_events')
