"""allow maintenance role to register validated historical artifacts"""

from alembic import op
import sqlalchemy as sa


revision = '0023_legacy_artifact_backfill'
down_revision = '0022_project_storage_accounting'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text(
        'GRANT INSERT ON job_artifacts TO bioagent_maintenance'
    ))
    op.execute(sa.text(
        'GRANT UPDATE (parameter, kind, status, storage_backend, filename, '
        'content_type, size_bytes, sha256, path, storage_key, version_id, '
        'reference, retention_until, last_error, storage_reservation_id, '
        'recovery_token, recovery_lease_until, revision, updated_at) '
        'ON job_artifacts TO bioagent_maintenance'
    ))


def downgrade():
    op.execute(sa.text(
        'REVOKE INSERT, UPDATE ON job_artifacts FROM bioagent_maintenance'
    ))
    op.execute(sa.text(
        'GRANT UPDATE (status, recovery_token, recovery_lease_until, revision, '
        'retention_until, last_error, updated_at) ON job_artifacts '
        'TO bioagent_maintenance'
    ))
