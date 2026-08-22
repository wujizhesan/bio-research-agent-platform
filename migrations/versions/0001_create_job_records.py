"""create job records table"""
from alembic import op
import sqlalchemy as sa


revision = '0001_create_job_records'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'job_records',
        sa.Column('job_id', sa.String(length=64), nullable=False),
        sa.Column('tool', sa.String(length=200), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.String(length=64), nullable=False),
        sa.Column('started_at', sa.String(length=64), nullable=True),
        sa.Column('finished_at', sa.String(length=64), nullable=True),
        sa.Column('arguments', sa.JSON(), nullable=False),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('retry_of', sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint('job_id'),
    )
    op.create_index('ix_job_records_tool', 'job_records', ['tool'])
    op.create_index('ix_job_records_status', 'job_records', ['status'])
    op.create_index('ix_job_records_created_at', 'job_records', ['created_at'])
    op.create_index('ix_job_records_retry_of', 'job_records', ['retry_of'])


def downgrade():
    op.drop_index('ix_job_records_retry_of', table_name='job_records')
    op.drop_index('ix_job_records_created_at', table_name='job_records')
    op.drop_index('ix_job_records_status', table_name='job_records')
    op.drop_index('ix_job_records_tool', table_name='job_records')
    op.drop_table('job_records')
