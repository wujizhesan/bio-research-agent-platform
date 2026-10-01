"""persist completed execution results"""

from alembic import op
import sqlalchemy as sa


revision = '0010_job_execution_results'
down_revision = '0009_job_events'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'job_execution_results',
        sa.Column('execution_key', sa.String(length=64), nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=False),
        sa.Column('fencing_token', sa.String(length=128), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('execution_semantics', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('result_sha256', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.String(length=64), nullable=False),
        sa.Column('updated_at', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['job_records.job_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('execution_key'),
    )
    op.create_index(
        'ix_job_execution_results_job_id',
        'job_execution_results',
        ['job_id'],
    )
    op.create_index(
        'ix_job_execution_results_created_at',
        'job_execution_results',
        ['created_at'],
    )
    op.create_index(
        'ix_job_execution_results_status',
        'job_execution_results',
        ['status'],
    )


def downgrade():
    op.drop_index(
        'ix_job_execution_results_status',
        table_name='job_execution_results',
    )
    op.drop_index(
        'ix_job_execution_results_created_at',
        table_name='job_execution_results',
    )
    op.drop_index(
        'ix_job_execution_results_job_id',
        table_name='job_execution_results',
    )
    op.drop_table('job_execution_results')
