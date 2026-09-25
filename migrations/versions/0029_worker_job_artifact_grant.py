"""Allow workers to persist the job artifact manifest."""

from alembic import op
import sqlalchemy as sa


revision = '0029_worker_job_artifact_grant'
down_revision = '0028_pure_job_delayed_retry'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text(
        'GRANT UPDATE (artifacts) ON job_records TO bioagent_worker'
    ))


def downgrade():
    op.execute(sa.text(
        'REVOKE UPDATE (artifacts) ON job_records FROM bioagent_worker'
    ))
