"""lease dispatcher outbox rows and require one-time worker claim tickets"""

from alembic import op
import sqlalchemy as sa


revision = '0017_dispatch_claim_tickets'
down_revision = '0016_dispatch_claim_idempotency'
branch_labels = None
depends_on = None


CLAIM_DISPATCH_BATCH_FUNCTION = r"""
CREATE FUNCTION bioagent_claim_dispatch_batch(
    requested_limit integer,
    supplied_dispatcher_id varchar,
    supplied_lease_seconds integer
)
RETURNS SETOF jsonb
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
WITH clock_value AS (
    SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision AS epoch
), candidates AS (
    SELECT outbox.job_id
    FROM public.job_outbox AS outbox
    JOIN public.job_records AS job ON job.job_id = outbox.job_id
    CROSS JOIN clock_value
    WHERE pg_has_role(session_user, 'bioagent_dispatcher', 'member')
      AND supplied_dispatcher_id IS NOT NULL
      AND supplied_dispatcher_id <> ''
      AND job.status NOT IN ('completed', 'failed', 'cancelled', 'indeterminate')
      AND (
          outbox.dispatch_lease_until IS NULL
          OR outbox.dispatch_lease_until <= clock_value.epoch
      )
      AND (
          outbox.next_attempt_at IS NULL
          OR outbox.next_attempt_at <= clock_value.epoch
      )
    ORDER BY
        COALESCE(outbox.next_attempt_at, 0),
        outbox.created_at,
        outbox.job_id
    FOR UPDATE OF outbox SKIP LOCKED
    LIMIT LEAST(GREATEST(requested_limit, 1), 10000)
), claimed AS (
    UPDATE public.job_outbox AS outbox
    SET dispatch_owner = supplied_dispatcher_id,
        dispatch_lease_until = clock_value.epoch
            + GREATEST(supplied_lease_seconds, 1),
        dispatch_generation = COALESCE(outbox.dispatch_generation, 0) + 1
    FROM candidates, clock_value
    WHERE outbox.job_id = candidates.job_id
    RETURNING outbox.*
)
SELECT
    claimed.payload::jsonb || jsonb_build_object(
        'job_id', job.job_id,
        'project_id', job.project_id,
        'tool', job.tool,
        'status', job.status,
        'created_at', job.created_at,
        '_attempts', job.attempts,
        '_cancel_requested', job.cancel_requested,
        'resources', job.resources,
        'priority', job.priority,
        'run_context', job.run_context,
        '_dispatch_generation', claimed.dispatch_generation
    )
FROM claimed
JOIN public.job_records AS job ON job.job_id = claimed.job_id
ORDER BY claimed.created_at, claimed.job_id
$$
"""


ISSUE_CLAIM_TICKET_FUNCTION = r"""
CREATE FUNCTION bioagent_issue_worker_claim_ticket(
    target_job_id varchar,
    supplied_dispatcher_id varchar,
    supplied_dispatch_generation integer,
    supplied_ticket_sha256 varchar,
    supplied_ttl_seconds integer
)
RETURNS boolean
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
WITH clock_value AS (
    SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision AS epoch
), updated AS (
    UPDATE public.job_worker_capabilities AS claim
    SET claim_ticket_sha256 = supplied_ticket_sha256,
        claim_ticket_expires_at = clock_value.epoch
            + GREATEST(supplied_ttl_seconds, 1),
        claim_ticket_redeemed_at = NULL,
        updated_at = statement_timestamp()::text
    FROM public.job_outbox AS outbox, clock_value
    WHERE claim.job_id = target_job_id
      AND outbox.job_id = claim.job_id
      AND pg_has_role(session_user, 'bioagent_dispatcher', 'member')
      AND outbox.dispatch_owner = supplied_dispatcher_id
      AND outbox.dispatch_generation = supplied_dispatch_generation
      AND outbox.dispatch_lease_until > clock_value.epoch
      AND supplied_ticket_sha256 ~ '^[0-9a-f]{64}$'
      AND supplied_ttl_seconds > 0
    RETURNING true
)
SELECT COALESCE((SELECT true FROM updated LIMIT 1), false)
$$
"""


COMPLETE_DISPATCH_FUNCTION = r"""
CREATE FUNCTION bioagent_complete_dispatch_claim(
    target_job_id varchar,
    supplied_dispatcher_id varchar,
    supplied_dispatch_generation integer,
    supplied_succeeded boolean,
    supplied_error text,
    supplied_retry_delay_seconds double precision
)
RETURNS boolean
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
WITH clock_value AS (
    SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision AS epoch
), updated AS (
    UPDATE public.job_outbox AS outbox
    SET dispatch_owner = NULL,
        dispatch_lease_until = NULL,
        next_attempt_at = clock_value.epoch
            + GREATEST(supplied_retry_delay_seconds, 0),
        dispatched_at = CASE
            WHEN supplied_succeeded THEN statement_timestamp()::text
            ELSE outbox.dispatched_at
        END,
        dispatch_attempts = COALESCE(outbox.dispatch_attempts, 0) + 1,
        last_error = CASE
            WHEN supplied_succeeded THEN NULL
            ELSE LEFT(COALESCE(supplied_error, 'dispatcher failure'), 2000)
        END
    FROM clock_value
    WHERE outbox.job_id = target_job_id
      AND pg_has_role(session_user, 'bioagent_dispatcher', 'member')
      AND outbox.dispatch_owner = supplied_dispatcher_id
      AND outbox.dispatch_generation = supplied_dispatch_generation
    RETURNING true
)
SELECT COALESCE((SELECT true FROM updated LIMIT 1), false)
$$
"""


CLAIM_WORKER_JOB_FUNCTION = r"""
CREATE OR REPLACE FUNCTION bioagent_claim_worker_job(
    target_job_id varchar,
    supplied_capability varchar,
    supplied_worker_id varchar,
    supplied_ticket_sha256 varchar,
    supplied_lease_seconds integer
)
RETURNS jsonb
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
WITH clock_value AS (
    SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision AS epoch
), eligible AS (
    SELECT claim.job_id
    FROM public.job_worker_capabilities AS claim
    JOIN public.job_records AS job ON job.job_id = claim.job_id
    CROSS JOIN clock_value
    WHERE claim.job_id = target_job_id
      AND claim.capability = supplied_capability
      AND claim.claim_ticket_sha256 = supplied_ticket_sha256
      AND claim.claim_ticket_redeemed_at IS NULL
      AND claim.claim_ticket_expires_at > clock_value.epoch
      AND COALESCE(claim.attempt, 0) < 2147483647
      AND pg_has_role(session_user, 'bioagent_worker', 'member')
      AND supplied_worker_id IS NOT NULL
      AND supplied_worker_id <> ''
      AND (
          job.status = 'queued'
          OR (
              job.status = 'running'
              AND (
                  job.lease_until IS NULL
                  OR job.lease_until <= clock_value.epoch
              )
          )
      )
    FOR UPDATE OF claim, job
), updated_claim AS (
    UPDATE public.job_worker_capabilities AS claim
    SET claimed_worker_id = supplied_worker_id,
        attempt = COALESCE(claim.attempt, 0) + 1,
        fencing_token = (COALESCE(claim.attempt, 0) + 1)::text,
        claim_ticket_redeemed_at = clock_value.epoch,
        updated_at = statement_timestamp()::text
    FROM eligible, clock_value
    WHERE claim.job_id = eligible.job_id
    RETURNING
        claim.job_id,
        claim.claimed_worker_id,
        claim.fencing_token,
        claim.attempt
), updated_job AS (
    UPDATE public.job_records AS job
    SET status = 'running',
        worker_id = supplied_worker_id,
        lease_until = clock_value.epoch
            + GREATEST(supplied_lease_seconds, 1)
    FROM updated_claim, clock_value
    WHERE job.job_id = updated_claim.job_id
    RETURNING job.job_id
)
SELECT jsonb_build_object(
    'job_id', updated_claim.job_id,
    'worker_id', updated_claim.claimed_worker_id,
    'fencing_token', updated_claim.fencing_token,
    'attempt', updated_claim.attempt
)
FROM updated_claim
JOIN updated_job ON updated_job.job_id = updated_claim.job_id
$$
"""


DISABLE_LEGACY_CLAIM_FUNCTION = r"""
CREATE OR REPLACE FUNCTION bioagent_claim_worker_job(
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
SELECT NULL::jsonb
$$
"""


LEGACY_CLAIM_WORKER_JOB_FUNCTION = r"""
CREATE OR REPLACE FUNCTION bioagent_claim_worker_job(
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


def upgrade():
    op.add_column(
        'job_outbox',
        sa.Column('dispatch_owner', sa.String(length=128), nullable=True),
    )
    op.add_column(
        'job_outbox',
        sa.Column('dispatch_lease_until', sa.Float(), nullable=True),
    )
    op.add_column(
        'job_outbox',
        sa.Column('next_attempt_at', sa.Float(), nullable=True),
    )
    op.add_column(
        'job_outbox',
        sa.Column(
            'dispatch_generation',
            sa.Integer(),
            nullable=False,
            server_default='0',
        ),
    )
    op.create_index(
        'ix_job_outbox_dispatch_schedule',
        'job_outbox',
        ['next_attempt_at', 'created_at'],
    )
    op.add_column(
        'job_worker_capabilities',
        sa.Column('claim_ticket_sha256', sa.String(length=64), nullable=True),
    )
    op.add_column(
        'job_worker_capabilities',
        sa.Column('claim_ticket_expires_at', sa.Float(), nullable=True),
    )
    op.add_column(
        'job_worker_capabilities',
        sa.Column('claim_ticket_redeemed_at', sa.Float(), nullable=True),
    )

    op.execute(sa.text(DISABLE_LEGACY_CLAIM_FUNCTION))
    op.execute(sa.text(CLAIM_DISPATCH_BATCH_FUNCTION))
    op.execute(sa.text(ISSUE_CLAIM_TICKET_FUNCTION))
    op.execute(sa.text(COMPLETE_DISPATCH_FUNCTION))
    op.execute(sa.text(CLAIM_WORKER_JOB_FUNCTION))

    op.execute(sa.text(
        'REVOKE ALL ON FUNCTION '
        'bioagent_claim_dispatch_batch(integer, varchar, integer), '
        'bioagent_issue_worker_claim_ticket(varchar, varchar, integer, varchar, integer), '
        'bioagent_complete_dispatch_claim(varchar, varchar, integer, boolean, text, double precision), '
        'bioagent_claim_worker_job(varchar, varchar, varchar, varchar, integer) '
        'FROM PUBLIC'
    ))
    op.execute(sa.text(
        'GRANT EXECUTE ON FUNCTION '
        'bioagent_claim_dispatch_batch(integer, varchar, integer), '
        'bioagent_issue_worker_claim_ticket(varchar, varchar, integer, varchar, integer), '
        'bioagent_complete_dispatch_claim(varchar, varchar, integer, boolean, text, double precision) '
        'TO bioagent_dispatcher'
    ))
    op.execute(sa.text(
        'GRANT EXECUTE ON FUNCTION '
        'bioagent_claim_worker_job(varchar, varchar, varchar, varchar, integer) '
        'TO bioagent_worker'
    ))


def downgrade():
    op.execute(sa.text(
        'DROP FUNCTION IF EXISTS '
        'bioagent_claim_worker_job(varchar, varchar, varchar, varchar, integer)'
    ))
    op.execute(sa.text(
        'DROP FUNCTION IF EXISTS '
        'bioagent_complete_dispatch_claim('
        'varchar, varchar, integer, boolean, text, double precision)'
    ))
    op.execute(sa.text(
        'DROP FUNCTION IF EXISTS '
        'bioagent_issue_worker_claim_ticket('
        'varchar, varchar, integer, varchar, integer)'
    ))
    op.execute(sa.text(
        'DROP FUNCTION IF EXISTS '
        'bioagent_claim_dispatch_batch(integer, varchar, integer)'
    ))
    op.execute(sa.text(LEGACY_CLAIM_WORKER_JOB_FUNCTION))
    op.execute(sa.text(
        'REVOKE ALL ON FUNCTION '
        'bioagent_claim_worker_job(varchar, varchar, varchar) FROM PUBLIC'
    ))
    op.execute(sa.text(
        'GRANT EXECUTE ON FUNCTION '
        'bioagent_claim_worker_job(varchar, varchar, varchar) TO bioagent_worker'
    ))

    op.drop_column('job_worker_capabilities', 'claim_ticket_redeemed_at')
    op.drop_column('job_worker_capabilities', 'claim_ticket_expires_at')
    op.drop_column('job_worker_capabilities', 'claim_ticket_sha256')
    op.drop_index('ix_job_outbox_dispatch_schedule', table_name='job_outbox')
    op.drop_column('job_outbox', 'dispatch_generation')
    op.drop_column('job_outbox', 'next_attempt_at')
    op.drop_column('job_outbox', 'dispatch_lease_until')
    op.drop_column('job_outbox', 'dispatch_owner')
