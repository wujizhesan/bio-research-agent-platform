"""Allow a dispatched worker claim while the outbox awaits reconciliation."""

from alembic import op
import sqlalchemy as sa


revision = '0030_worker_claim_after_dispatch'
down_revision = '0029_worker_job_artifact_grant'
branch_labels = None
depends_on = None


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
    JOIN public.job_outbox AS outbox ON outbox.job_id = job.job_id
    CROSS JOIN clock_value
    WHERE claim.job_id = target_job_id
      AND claim.capability = supplied_capability
      AND claim.claim_ticket_sha256 = supplied_ticket_sha256
      AND claim.claim_ticket_redeemed_at IS NULL
      AND claim.claim_ticket_expires_at > clock_value.epoch
      AND COALESCE(claim.attempt, 0) < 2147483647
      AND (
          outbox.payload ->> '_retry_not_before' IS NULL
          OR (outbox.payload ->> '_retry_not_before')::double precision
              <= clock_value.epoch
      )
      AND NOT job.cancel_requested
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
    FOR UPDATE OF claim, job, outbox
), updated_claim AS (
    UPDATE public.job_worker_capabilities AS claim
    SET claimed_worker_id = supplied_worker_id,
        attempt = COALESCE(claim.attempt, 0) + 1,
        fencing_token = (COALESCE(claim.attempt, 0) + 1)::text,
        claim_ticket_redeemed_at = clock_value.epoch,
        updated_at = statement_timestamp()::text
    FROM eligible, clock_value
    WHERE claim.job_id = eligible.job_id
    RETURNING claim.job_id, claim.claimed_worker_id,
        claim.fencing_token, claim.attempt
), updated_job AS (
    UPDATE public.job_records AS job
    SET status = 'running',
        worker_id = supplied_worker_id,
        lease_until = clock_value.epoch + GREATEST(supplied_lease_seconds, 1)
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


def upgrade():
    op.execute(sa.text(CLAIM_WORKER_JOB_FUNCTION))


def downgrade():
    from importlib import import_module
    previous = import_module(
        'migrations.versions.0028_pure_job_delayed_retry'
    )
    op.execute(sa.text(previous.CLAIM_WORKER_JOB_FUNCTION))
