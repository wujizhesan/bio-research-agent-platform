"""persist manual job resolution"""

from alembic import op
import sqlalchemy as sa


revision = '0011_job_resolution'
down_revision = '0010_job_execution_results'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('job_records', sa.Column('resolution', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('job_records', 'resolution')
