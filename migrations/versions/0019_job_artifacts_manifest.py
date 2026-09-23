"""add durable job artifact manifests"""

from alembic import op
import sqlalchemy as sa


revision = '0019_job_artifacts_manifest'
down_revision = '0018_identity_context_sessions'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'job_records',
        sa.Column('artifacts', sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column('job_records', 'artifacts')
