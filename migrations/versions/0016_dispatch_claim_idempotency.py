"""separate dispatch authority and persist idempotency claims"""

from alembic import op
import sqlalchemy as sa


revision = '0016_dispatch_claim_idempotency'
down_revision = '0015_worker_job_capabilities'
branch_labels = None
depends_on = None


CLAIM_FUNCTION = r"""
CREATE FUNCTION bioagent_claim_worker_job(
    target_job_id varchar,
    supplied_capability varchar,
    supplied_worker_id varchar
)
RETURNS jsonb
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
WITH updated AS (
    UPDATE public.job_worker_capabilities
    SET claimed_worker_id = supplied_worker_id,
        attempt = COALESCE(attempt, 0) + 1,
        fencing_token = (COALESCE(attempt, 0) + 1)::text,
        updated_at = statement_timestamp()::text
    WHERE job_id = target_job_id
      AND capability = supplied_capability
      AND pg_has_role(session_user, 'bioagent_worker', 'member')
      AND supplied_worker_id IS NOT NULL
      AND supplied_worker_id <> ''
      AND COALESCE(attempt, 0) < 2147483647
    RETURNING job_id, claimed_worker_id, fencing_token, attempt
)
SELECT jsonb_build_object(
    'job_id', job_id,
    'worker_id', claimed_worker_id,
    'fencing_token', fencing_token,
    'attempt', attempt
)
FROM updated
$$
"""


VALIDATE_CLAIM_FUNCTION = r"""
CREATE OR REPLACE FUNCTION bioagent_bind_worker_claim(
    target_job_id varchar,
    supplied_capability varchar,
    supplied_worker_id varchar,
    supplied_fencing_token varchar,
    supplied_attempt integer
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT
    pg_has_role(session_user, 'bioagent_worker', 'member')
    AND EXISTS (
        SELECT 1
        FROM public.job_worker_capabilities AS claim
        WHERE claim.job_id = target_job_id
          AND claim.capability = supplied_capability
          AND claim.claimed_worker_id = supplied_worker_id
          AND claim.fencing_token = supplied_fencing_token
          AND claim.attempt = supplied_attempt
    )
$$
"""


LEGACY_BIND_CLAIM_FUNCTION = r"""
CREATE OR REPLACE FUNCTION bioagent_bind_worker_claim(
    target_job_id varchar,
    supplied_capability varchar,
    supplied_worker_id varchar,
    supplied_fencing_token varchar,
    supplied_attempt integer
)
RETURNS boolean
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
WITH updated AS (
    UPDATE public.job_worker_capabilities
    SET claimed_worker_id = supplied_worker_id,
        fencing_token = supplied_fencing_token,
        attempt = supplied_attempt,
        updated_at = statement_timestamp()::text
    WHERE job_id = target_job_id
      AND capability = supplied_capability
      AND pg_has_role(session_user, 'bioagent_worker', 'member')
      AND supplied_worker_id IS NOT NULL
      AND supplied_worker_id <> ''
      AND supplied_fencing_token IS NOT NULL
      AND supplied_fencing_token <> ''
      AND supplied_attempt >= 1
      AND (
          attempt IS NULL
          OR supplied_attempt > attempt
          OR (
              supplied_attempt = attempt
              AND claimed_worker_id = supplied_worker_id
              AND fencing_token = supplied_fencing_token
          )
      )
    RETURNING true
)
SELECT COALESCE((SELECT true FROM updated LIMIT 1), false)
$$
"""


DISPATCHABLE_FUNCTION = r"""
CREATE OR REPLACE FUNCTION bioagent_list_worker_dispatchable_jobs(requested_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT
    outbox.payload::jsonb || jsonb_build_object(
        'job_id', job.job_id,
        'project_id', job.project_id,
        'tool', job.tool,
        'status', job.status,
        'created_at', job.created_at,
        '_attempts', job.attempts,
        '_cancel_requested', job.cancel_requested,
        'resources', job.resources,
        'priority', job.priority,
        'run_context', job.run_context
    )
FROM public.job_outbox AS outbox
JOIN public.job_records AS job ON job.job_id = outbox.job_id
WHERE (
    pg_has_role(session_user, 'bioagent_dispatcher', 'member')
    OR session_user = current_user
)
AND job.status NOT IN ('completed', 'failed', 'cancelled', 'indeterminate')
ORDER BY outbox.created_at
LIMIT LEAST(GREATEST(requested_limit, 1), 10000)
$$
"""


LEGACY_DISPATCHABLE_FUNCTION = r"""
CREATE OR REPLACE FUNCTION bioagent_list_worker_dispatchable_jobs(requested_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT
    outbox.payload::jsonb || jsonb_build_object(
        'job_id', job.job_id,
        'project_id', job.project_id,
        'tool', job.tool,
        'status', job.status,
        'created_at', job.created_at,
        '_attempts', job.attempts,
        '_cancel_requested', job.cancel_requested,
        'resources', job.resources,
        'priority', job.priority,
        'run_context', job.run_context
    )
FROM public.job_outbox AS outbox
JOIN public.job_records AS job ON job.job_id = outbox.job_id
WHERE (
    pg_has_role(session_user, 'bioagent_worker', 'member')
    OR session_user = current_user
)
AND job.status NOT IN ('completed', 'failed', 'cancelled', 'indeterminate')
ORDER BY outbox.created_at
LIMIT LEAST(GREATEST(requested_limit, 1), 10000)
$$
"""


def upgrade():
    op.create_table(
        'job_idempotency',
        sa.Column('idempotency_key', sa.String(length=80), nullable=False),
        sa.Column('subject', sa.String(length=200), nullable=False),
        sa.Column('project_id', sa.String(length=64), nullable=False),
        sa.Column('payload_hash', sa.String(length=64), nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['job_records.job_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.project_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('idempotency_key'),
        sa.UniqueConstraint('job_id'),
    )
    op.create_index('ix_job_idempotency_subject', 'job_idempotency', ['subject'])
    op.create_index('ix_job_idempotency_project_id', 'job_idempotency', ['project_id'])
    op.create_index('ix_job_idempotency_created_at', 'job_idempotency', ['created_at'])
    op.execute(sa.text('ALTER TABLE job_idempotency ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text('ALTER TABLE job_idempotency FORCE ROW LEVEL SECURITY'))
    op.execute(sa.text(
        'CREATE POLICY job_idempotency_tenant_access ON job_idempotency FOR ALL '
        'USING (subject = current_setting(\'bioagent.subject\', true) '
        'AND bioagent_api_can_access_project(project_id, false)) '
        'WITH CHECK (subject = current_setting(\'bioagent.subject\', true) '
        'AND bioagent_api_can_access_project(project_id, true))'
    ))
    op.execute(sa.text('REVOKE ALL PRIVILEGES ON job_idempotency FROM PUBLIC'))
    op.execute(sa.text(
        'GRANT SELECT, INSERT ON job_idempotency TO bioagent_api'
    ))

    op.execute(sa.text(CLAIM_FUNCTION))
    op.execute(sa.text(VALIDATE_CLAIM_FUNCTION))
    op.execute(sa.text(DISPATCHABLE_FUNCTION))
    op.execute(sa.text(
        'REVOKE ALL ON FUNCTION '
        'bioagent_claim_worker_job(varchar, varchar, varchar) FROM PUBLIC'
    ))
    op.execute(sa.text(
        'GRANT EXECUTE ON FUNCTION '
        'bioagent_claim_worker_job(varchar, varchar, varchar) TO bioagent_worker'
    ))
    op.execute(sa.text(
        'REVOKE EXECUTE ON FUNCTION '
        'bioagent_list_worker_dispatchable_jobs(integer) FROM bioagent_worker'
    ))
    op.execute(sa.text('GRANT USAGE ON SCHEMA public TO bioagent_dispatcher'))
    op.execute(sa.text(
        'GRANT EXECUTE ON FUNCTION '
        'bioagent_list_worker_dispatchable_jobs(integer) TO bioagent_dispatcher'
    ))


def downgrade():
    op.execute(sa.text(
        'REVOKE EXECUTE ON FUNCTION '
        'bioagent_list_worker_dispatchable_jobs(integer) FROM bioagent_dispatcher'
    ))
    op.execute(sa.text(
        'GRANT EXECUTE ON FUNCTION '
        'bioagent_list_worker_dispatchable_jobs(integer) TO bioagent_worker'
    ))
    op.execute(sa.text(
        'DROP FUNCTION IF EXISTS '
        'bioagent_claim_worker_job(varchar, varchar, varchar)'
    ))
    op.execute(sa.text(LEGACY_BIND_CLAIM_FUNCTION))
    op.execute(sa.text(LEGACY_DISPATCHABLE_FUNCTION))
    op.drop_index('ix_job_idempotency_created_at', table_name='job_idempotency')
    op.drop_index('ix_job_idempotency_project_id', table_name='job_idempotency')
    op.drop_index('ix_job_idempotency_subject', table_name='job_idempotency')
    op.drop_table('job_idempotency')
