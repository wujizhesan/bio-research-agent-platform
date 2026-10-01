"""scope worker database access to claimed jobs"""

from alembic import op
import sqlalchemy as sa


revision = '0015_worker_job_capabilities'
down_revision = '0014_project_control_plane_rls'
branch_labels = None
depends_on = None


LEGACY_API_READ_ACCESS = """(
    current_setting('bioagent.is_admin', true) = 'true'
    OR EXISTS (
        SELECT 1 FROM project_members member
        WHERE member.project_id = {table}.project_id
          AND member.subject = current_setting('bioagent.subject', true)
    )
)"""

LEGACY_API_WRITE_ACCESS = """(
    current_setting('bioagent.is_admin', true) = 'true'
    OR EXISTS (
        SELECT 1 FROM project_members member
        WHERE member.project_id = {table}.project_id
          AND member.subject = current_setting('bioagent.subject', true)
          AND member.role IN ('owner', 'editor')
    )
)"""

LEGACY_API_CHILD_READ_ACCESS = """(
    current_setting('bioagent.is_admin', true) = 'true'
    OR EXISTS (
        SELECT 1 FROM job_records job
        JOIN project_members member ON member.project_id = job.project_id
        WHERE job.job_id = {table}.job_id
          AND member.subject = current_setting('bioagent.subject', true)
    )
)"""

LEGACY_API_CHILD_WRITE_ACCESS = """(
    current_setting('bioagent.is_admin', true) = 'true'
    OR EXISTS (
        SELECT 1 FROM job_records job
        JOIN project_members member ON member.project_id = job.project_id
        WHERE job.job_id = {table}.job_id
          AND member.subject = current_setting('bioagent.subject', true)
          AND member.role IN ('owner', 'editor')
    )
)"""

WORKER_ACCESS = "bioagent_worker_can_access_job(job_id)"
API_READ_ACCESS = "bioagent_api_can_access_project({table}.project_id, false)"
API_WRITE_ACCESS = "bioagent_api_can_access_project({table}.project_id, true)"
API_CHILD_READ_ACCESS = "bioagent_api_can_access_job({table}.job_id, false)"
API_CHILD_WRITE_ACCESS = "bioagent_api_can_access_job({table}.job_id, true)"


API_PROJECT_ACCESS_FUNCTION = r"""
CREATE FUNCTION bioagent_api_can_access_project(
    target_project_id varchar,
    require_write boolean
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT
    pg_has_role(session_user, 'bioagent_api', 'member')
    AND (
        current_setting('bioagent.is_admin', true) = 'true'
        OR EXISTS (
            SELECT 1
            FROM public.project_members AS member
            WHERE member.project_id = target_project_id
              AND member.subject = current_setting('bioagent.subject', true)
              AND (
                  NOT require_write
                  OR member.role IN ('owner', 'editor')
              )
        )
    )
$$
"""


API_JOB_ACCESS_FUNCTION = r"""
CREATE FUNCTION bioagent_api_can_access_job(
    target_job_id varchar,
    require_write boolean
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT EXISTS (
    SELECT 1
    FROM public.job_records AS job
    WHERE job.job_id = target_job_id
      AND public.bioagent_api_can_access_project(
          job.project_id,
          require_write
      )
)
$$
"""


REGISTER_CAPABILITY_FUNCTION = r"""
CREATE FUNCTION bioagent_register_worker_capability(
    target_job_id varchar,
    supplied_capability varchar
)
RETURNS boolean
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
WITH allowed_job AS (
    SELECT job.job_id
    FROM public.job_records AS job
    WHERE job.job_id = target_job_id
      AND length(COALESCE(supplied_capability, '')) >= 32
      AND (
          session_user = current_user
          OR current_setting('bioagent.is_admin', true) = 'true'
          OR public.bioagent_current_project_role(job.project_id)
              IN ('owner', 'editor')
      )
), upserted AS (
    INSERT INTO public.job_worker_capabilities (
        job_id, capability, created_at, updated_at
    )
    SELECT
        allowed_job.job_id,
        supplied_capability,
        statement_timestamp()::text,
        statement_timestamp()::text
    FROM allowed_job
    ON CONFLICT (job_id) DO UPDATE
    SET updated_at = EXCLUDED.updated_at
    WHERE public.job_worker_capabilities.capability = EXCLUDED.capability
    RETURNING true
)
SELECT COALESCE((SELECT true FROM upserted LIMIT 1), false)
$$
"""


BIND_CLAIM_FUNCTION = r"""
CREATE FUNCTION bioagent_bind_worker_claim(
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


ACCESS_FUNCTION = r"""
CREATE FUNCTION bioagent_worker_can_access_job(target_job_id varchar)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT
    pg_has_role(session_user, 'bioagent_worker', 'member')
    AND target_job_id = current_setting('bioagent.worker_job_id', true)
    AND EXISTS (
        SELECT 1
        FROM public.job_worker_capabilities AS claim
        WHERE claim.job_id = target_job_id
          AND claim.capability = current_setting(
              'bioagent.worker_capability', true
          )
          AND claim.claimed_worker_id = current_setting(
              'bioagent.worker_id', true
          )
          AND claim.fencing_token = current_setting(
              'bioagent.worker_fencing_token', true
          )
          AND claim.attempt::text = current_setting(
              'bioagent.worker_attempt', true
          )
    )
$$
"""


DISPATCHABLE_FUNCTION = r"""
CREATE FUNCTION bioagent_list_worker_dispatchable_jobs(requested_limit integer)
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


def _replace_policy(table, name, command, using, check=None):
    op.execute(sa.text(f'DROP POLICY IF EXISTS {name} ON {table}'))
    sql = f'CREATE POLICY {name} ON {table} FOR {command}'
    if using is not None:
        sql += f' USING ({using})'
    if check is not None:
        sql += f' WITH CHECK ({check})'
    op.execute(sa.text(sql))


def upgrade():
    op.create_table(
        'job_worker_capabilities',
        sa.Column('job_id', sa.String(length=64), nullable=False),
        sa.Column('capability', sa.String(length=128), nullable=False),
        sa.Column('claimed_worker_id', sa.String(length=128), nullable=True),
        sa.Column('fencing_token', sa.String(length=128), nullable=True),
        sa.Column('attempt', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.String(length=64), nullable=False),
        sa.Column('updated_at', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ['job_id'], ['job_records.job_id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('job_id'),
        sa.UniqueConstraint('capability'),
    )
    op.execute(sa.text(
        "INSERT INTO job_worker_capabilities "
        "(job_id, capability, created_at, updated_at) "
        "SELECT job_id, payload ->> '_execution_key', created_at, created_at "
        "FROM job_outbox "
        "WHERE length(COALESCE(payload ->> '_execution_key', '')) >= 32 "
        "ON CONFLICT (job_id) DO NOTHING"
    ))

    op.execute(sa.text(REGISTER_CAPABILITY_FUNCTION))
    op.execute(sa.text(BIND_CLAIM_FUNCTION))
    op.execute(sa.text(ACCESS_FUNCTION))
    op.execute(sa.text(DISPATCHABLE_FUNCTION))
    op.execute(sa.text(API_PROJECT_ACCESS_FUNCTION))
    op.execute(sa.text(API_JOB_ACCESS_FUNCTION))
    op.execute(sa.text(
        'REVOKE ALL ON FUNCTION '
        'bioagent_register_worker_capability(varchar, varchar) FROM PUBLIC'
    ))
    op.execute(sa.text(
        'REVOKE ALL ON FUNCTION '
        'bioagent_bind_worker_claim(varchar, varchar, varchar, varchar, integer) '
        'FROM PUBLIC'
    ))
    op.execute(sa.text(
        'REVOKE ALL ON FUNCTION bioagent_worker_can_access_job(varchar) '
        'FROM PUBLIC'
    ))
    op.execute(sa.text(
        'REVOKE ALL ON FUNCTION bioagent_list_worker_dispatchable_jobs(integer) '
        'FROM PUBLIC'
    ))
    op.execute(sa.text(
        'REVOKE ALL ON FUNCTION '
        'bioagent_api_can_access_project(varchar, boolean), '
        'bioagent_api_can_access_job(varchar, boolean) FROM PUBLIC'
    ))
    op.execute(sa.text(
        'GRANT EXECUTE ON FUNCTION '
        'bioagent_register_worker_capability(varchar, varchar) TO bioagent_api'
    ))
    op.execute(sa.text(
        'GRANT EXECUTE ON FUNCTION bioagent_worker_can_access_job(varchar) '
        'TO bioagent_api'
    ))
    op.execute(sa.text(
        'GRANT EXECUTE ON FUNCTION '
        'bioagent_api_can_access_project(varchar, boolean), '
        'bioagent_api_can_access_job(varchar, boolean) '
        'TO bioagent_api, bioagent_worker'
    ))
    op.execute(sa.text(
        'GRANT EXECUTE ON FUNCTION '
        'bioagent_bind_worker_claim(varchar, varchar, varchar, varchar, integer), '
        'bioagent_worker_can_access_job(varchar), '
        'bioagent_list_worker_dispatchable_jobs(integer) TO bioagent_worker'
    ))

    op.execute(sa.text(
        'REVOKE ALL PRIVILEGES ON projects, project_members, job_records, '
        'job_projects, file_records, file_projects, job_events, job_outbox, '
        'job_execution_results, job_worker_capabilities FROM bioagent_worker'
    ))
    op.execute(sa.text('GRANT SELECT ON job_records TO bioagent_worker'))
    op.execute(sa.text(
        'GRANT UPDATE (status, started_at, finished_at, result, error, attempts, '
        'cancel_requested, worker_id, lease_until, execution) '
        'ON job_records TO bioagent_worker'
    ))
    op.execute(sa.text('GRANT SELECT, INSERT ON job_events TO bioagent_worker'))
    op.execute(sa.text('GRANT SELECT, DELETE ON job_outbox TO bioagent_worker'))
    op.execute(sa.text(
        'GRANT SELECT, INSERT, UPDATE ON job_execution_results TO bioagent_worker'
    ))

    api_read = API_READ_ACCESS.format(table='job_records')
    api_write = API_WRITE_ACCESS.format(table='job_records')
    _replace_policy(
        'job_records', 'job_records_tenant_read', 'SELECT',
        f'{api_read} OR {WORKER_ACCESS}',
    )
    _replace_policy(
        'job_records', 'job_records_tenant_write', 'ALL',
        f'{api_write} OR {WORKER_ACCESS}',
        f'{api_write} OR {WORKER_ACCESS}',
    )
    for table in ('file_records', 'job_projects', 'file_projects'):
        _replace_policy(
            table, f'{table}_tenant_read', 'SELECT',
            API_READ_ACCESS.format(table=table),
        )
        _replace_policy(
            table, f'{table}_tenant_write', 'ALL',
            API_WRITE_ACCESS.format(table=table),
            API_WRITE_ACCESS.format(table=table),
        )
    for table in ('job_events', 'job_outbox', 'job_execution_results'):
        api_child_read = API_CHILD_READ_ACCESS.format(table=table)
        api_child_write = API_CHILD_WRITE_ACCESS.format(table=table)
        _replace_policy(
            table, f'{table}_tenant_read', 'SELECT',
            f'{api_child_read} OR {WORKER_ACCESS}',
        )
        _replace_policy(
            table, f'{table}_tenant_write', 'ALL',
            f'{api_child_write} OR {WORKER_ACCESS}',
            f'{api_child_write} OR {WORKER_ACCESS}',
        )

    op.execute(sa.text(
        'ALTER POLICY projects_tenant_read ON projects USING ('
        "current_setting('bioagent.is_admin', true) = 'true' "
        'OR bioagent_current_project_role(project_id) IS NOT NULL)'
    ))
    op.execute(sa.text(
        'ALTER POLICY project_members_tenant_read ON project_members USING ('
        "current_setting('bioagent.is_admin', true) = 'true' "
        'OR bioagent_current_project_role(project_id) IS NOT NULL)'
    ))
    op.execute(sa.text(
        'REVOKE EXECUTE ON FUNCTION bioagent_project_owner_subject(varchar), '
        'bioagent_current_project_role(varchar) FROM bioagent_worker'
    ))


def downgrade():
    op.execute(sa.text(
        'GRANT EXECUTE ON FUNCTION bioagent_project_owner_subject(varchar), '
        'bioagent_current_project_role(varchar) TO bioagent_worker'
    ))
    op.execute(sa.text('GRANT SELECT ON projects, project_members TO bioagent_worker'))
    op.execute(sa.text(
        'GRANT SELECT, INSERT, UPDATE ON job_records, job_projects, '
        'file_records, file_projects TO bioagent_worker'
    ))
    op.execute(sa.text('GRANT SELECT, INSERT ON job_events TO bioagent_worker'))
    op.execute(sa.text(
        'GRANT SELECT, INSERT, UPDATE, DELETE ON job_outbox TO bioagent_worker'
    ))
    op.execute(sa.text(
        'GRANT SELECT, INSERT, UPDATE ON job_execution_results TO bioagent_worker'
    ))

    worker = "pg_has_role(current_user, 'bioagent_worker', 'member')"
    api_read = LEGACY_API_READ_ACCESS.format(table='job_records')
    api_write = LEGACY_API_WRITE_ACCESS.format(table='job_records')
    _replace_policy(
        'job_records', 'job_records_tenant_read', 'SELECT',
        f'{worker} OR {api_read}',
    )
    _replace_policy(
        'job_records', 'job_records_tenant_write', 'ALL',
        f'{worker} OR {api_write}', f'{worker} OR {api_write}',
    )
    for table in ('file_records', 'job_projects', 'file_projects'):
        read = LEGACY_API_READ_ACCESS.format(table=table)
        write = LEGACY_API_WRITE_ACCESS.format(table=table)
        _replace_policy(
            table, f'{table}_tenant_read', 'SELECT', f'{worker} OR {read}'
        )
        _replace_policy(
            table, f'{table}_tenant_write', 'ALL',
            f'{worker} OR {write}', f'{worker} OR {write}',
        )
    for table in ('job_events', 'job_outbox', 'job_execution_results'):
        read = LEGACY_API_CHILD_READ_ACCESS.format(table=table)
        write = LEGACY_API_CHILD_WRITE_ACCESS.format(table=table)
        _replace_policy(
            table, f'{table}_tenant_read', 'SELECT', f'{worker} OR {read}'
        )
        _replace_policy(
            table, f'{table}_tenant_write', 'ALL',
            f'{worker} OR {write}', f'{worker} OR {write}',
        )

    op.execute(sa.text('DROP POLICY IF EXISTS projects_tenant_read ON projects'))
    op.execute(sa.text(
        'CREATE POLICY projects_tenant_read ON projects FOR SELECT USING ('
        f"{worker} OR current_setting('bioagent.is_admin', true) = 'true' "
        'OR bioagent_current_project_role(project_id) IS NOT NULL)'
    ))
    op.execute(sa.text(
        'DROP POLICY IF EXISTS project_members_tenant_read ON project_members'
    ))
    op.execute(sa.text(
        'CREATE POLICY project_members_tenant_read ON project_members FOR SELECT USING ('
        f"{worker} OR current_setting('bioagent.is_admin', true) = 'true' "
        'OR bioagent_current_project_role(project_id) IS NOT NULL)'
    ))
    for signature in (
        'bioagent_api_can_access_job(varchar, boolean)',
        'bioagent_api_can_access_project(varchar, boolean)',
        'bioagent_list_worker_dispatchable_jobs(integer)',
        'bioagent_worker_can_access_job(varchar)',
        'bioagent_bind_worker_claim(varchar, varchar, varchar, varchar, integer)',
        'bioagent_register_worker_capability(varchar, varchar)',
    ):
        op.execute(sa.text(f'DROP FUNCTION IF EXISTS {signature}'))
    op.drop_table('job_worker_capabilities')
