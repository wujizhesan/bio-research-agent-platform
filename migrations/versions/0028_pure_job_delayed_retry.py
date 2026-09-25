"""persist pure-job retry delays and fence deferred worker attempts"""

from alembic import op
import sqlalchemy as sa


revision = '0028_pure_job_delayed_retry'
down_revision = '0027_tenant_key_rotation'
branch_labels = None
depends_on = None


DEFER_PURE_JOB_FUNCTION = r"""
CREATE FUNCTION bioagent_defer_pure_job(
    target_job_id varchar,
    supplied_capability varchar,
    supplied_worker_id varchar,
    supplied_fencing_token varchar,
    supplied_attempt integer,
    supplied_delay_seconds double precision,
    supplied_max_attempts integer,
    supplied_record_revision integer
)
RETURNS jsonb
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
WITH clock_value AS (
    SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision
        + supplied_delay_seconds AS retry_epoch
), eligible AS (
    SELECT claim.job_id
    FROM public.job_worker_capabilities AS claim
    JOIN public.job_records AS job ON job.job_id = claim.job_id
    JOIN public.job_outbox AS outbox ON outbox.job_id = job.job_id
    WHERE claim.job_id = target_job_id
      AND pg_has_role(session_user, 'bioagent_worker', 'member')
      AND supplied_delay_seconds > 0
      AND supplied_delay_seconds <= 604800
      AND supplied_attempt >= 1
      AND supplied_attempt < supplied_max_attempts
      AND claim.capability = supplied_capability
      AND claim.claimed_worker_id = supplied_worker_id
      AND claim.fencing_token = supplied_fencing_token
      AND claim.attempt = supplied_attempt
      AND job.status = 'running'
      AND job.worker_id = supplied_worker_id
      AND NOT job.cancel_requested
      AND outbox.payload ->> 'execution_semantics' = 'pure'
    FOR UPDATE OF claim, job, outbox
), event_revision AS (
    SELECT eligible.job_id,
           GREATEST(
               COALESCE(MAX(events.revision), 0),
               COALESCE(supplied_record_revision, 0)
           ) + 1 AS revision
    FROM eligible
    LEFT JOIN public.job_events AS events ON events.job_id = eligible.job_id
    GROUP BY eligible.job_id
), updated_execution AS (
    UPDATE public.job_execution_results AS execution
    SET status = 'deferred', updated_at = statement_timestamp()::text
    FROM eligible
    WHERE execution.execution_key = supplied_capability
      AND execution.job_id = eligible.job_id
      AND execution.fencing_token = supplied_fencing_token
      AND execution.status = 'running'
    RETURNING execution.job_id
), updated_job AS (
    UPDATE public.job_records AS job
    SET status = 'queued',
        started_at = NULL,
        worker_id = NULL,
        lease_until = NULL,
        attempts = supplied_attempt
    FROM eligible
    WHERE job.job_id = eligible.job_id
    RETURNING job.job_id
), updated_outbox AS (
    UPDATE public.job_outbox AS outbox
    SET next_attempt_at = retry_epoch,
        dispatch_owner = NULL,
        dispatch_lease_until = NULL,
        dispatch_generation = COALESCE(outbox.dispatch_generation, 0) + 1,
        payload = (outbox.payload::jsonb || jsonb_build_object(
            '_retry_not_before', retry_epoch,
            '_deferred_attempt', supplied_attempt,
            '_revision', event_revision.revision,
            'scheduling', jsonb_build_object(
                'status', 'waiting_for_external_service',
                'retry_at', retry_epoch
            )
        ))::json
    FROM eligible, clock_value, event_revision
    WHERE outbox.job_id = eligible.job_id
      AND event_revision.job_id = eligible.job_id
    RETURNING outbox.job_id
), updated_claim AS (
    UPDATE public.job_worker_capabilities AS claim
    SET claimed_worker_id = NULL,
        fencing_token = NULL,
        claim_ticket_sha256 = NULL,
        claim_ticket_expires_at = NULL,
        claim_ticket_redeemed_at = NULL,
        updated_at = statement_timestamp()::text
    FROM updated_job, updated_outbox
    WHERE claim.job_id = updated_job.job_id
      AND claim.job_id = updated_outbox.job_id
    RETURNING claim.job_id
), inserted_event AS (
    INSERT INTO public.job_events (
        job_id, revision, event_id, status, payload, created_at, terminal
    )
    SELECT updated_claim.job_id,
           event_revision.revision,
           event_revision.revision::text || '-0',
           'queued',
           jsonb_strip_nulls(jsonb_build_object(
               'job_id', job.job_id,
               'tool', job.tool,
               'status', 'queued',
               'created_at', job.created_at,
               'attempts', supplied_attempt,
               'resources', job.resources,
               'priority', job.priority,
               'project_id', job.project_id,
               'revision', event_revision.revision,
               'trace_id', job.trace_id,
               'request_id', job.request_id,
               'run_context', job.run_context,
               'execution_identity', job.execution_identity,
               'routing', job.routing,
               'execution', job.execution,
               'scheduling', jsonb_build_object(
                   'status', 'waiting_for_external_service',
                   'retry_at', clock_value.retry_epoch
               )
           ))::json,
           statement_timestamp()::text,
           false
    FROM updated_claim
    JOIN event_revision ON event_revision.job_id = updated_claim.job_id
    JOIN public.job_records AS job ON job.job_id = updated_claim.job_id
    CROSS JOIN clock_value
    RETURNING job_id, revision
)
SELECT jsonb_build_object(
    'job_id', inserted_event.job_id,
    'retry_at', clock_value.retry_epoch,
    'revision', inserted_event.revision
)
FROM inserted_event, clock_value
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
    JOIN public.job_outbox AS outbox ON outbox.job_id = job.job_id
    CROSS JOIN clock_value
    WHERE claim.job_id = target_job_id
      AND claim.capability = supplied_capability
      AND claim.claim_ticket_sha256 = supplied_ticket_sha256
      AND claim.claim_ticket_redeemed_at IS NULL
      AND claim.claim_ticket_expires_at > clock_value.epoch
      AND COALESCE(claim.attempt, 0) < 2147483647
      AND (
          outbox.next_attempt_at IS NULL
          OR outbox.next_attempt_at <= clock_value.epoch
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
    op.execute(sa.text(DEFER_PURE_JOB_FUNCTION))
    op.execute(sa.text(CLAIM_WORKER_JOB_FUNCTION))
    op.execute(sa.text(
        'REVOKE ALL ON FUNCTION bioagent_defer_pure_job('
        'varchar, varchar, varchar, varchar, integer, double precision, integer, integer) '
        'FROM PUBLIC'
    ))
    op.execute(sa.text(
        'GRANT EXECUTE ON FUNCTION bioagent_defer_pure_job('
        'varchar, varchar, varchar, varchar, integer, double precision, integer, integer) '
        'TO bioagent_worker'
    ))


def downgrade():
    op.execute(sa.text(
        'DROP FUNCTION IF EXISTS bioagent_defer_pure_job('
        'varchar, varchar, varchar, varchar, integer, double precision, integer, integer)'
    ))
    from importlib import import_module
    previous = import_module(
        'migrations.versions.0017_dispatch_leases_claim_tickets'
    )
    op.execute(sa.text(previous.CLAIM_WORKER_JOB_FUNCTION))
