"""add signed tenant context and revocable authentication sessions"""

from alembic import op
import sqlalchemy as sa


revision = '0018_identity_context_sessions'
down_revision = '0017_dispatch_claim_tickets'
branch_labels = None
depends_on = None


CONTEXT_VALID_FUNCTION = r"""
CREATE FUNCTION bioagent_signed_context_valid()
RETURNS boolean
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT EXISTS (
    SELECT 1
    FROM public.tenant_context_keys AS context_key
    WHERE context_key.key_id = current_setting('bioagent.context_key_id', true)
      AND context_key.active
      AND current_setting('bioagent.context_signature', true) ~ '^[0-9a-f]{64}$'
      AND current_setting('bioagent.context_nonce', true) ~ '^[A-Za-z0-9_-]{16,128}$'
      AND current_setting('bioagent.context_expires_at', true) ~ '^[0-9]{1,12}$'
      AND current_setting('bioagent.context_is_admin', true) IN ('0', '1')
      AND current_setting('bioagent.context_expires_at', true)::bigint
          >= EXTRACT(EPOCH FROM clock_timestamp())::bigint
      AND current_setting('bioagent.context_expires_at', true)::bigint
          <= EXTRACT(EPOCH FROM clock_timestamp())::bigint + 300
      AND encode(
          public.hmac(
              convert_to(
                  encode(
                      sha256(convert_to(
                          current_setting('bioagent.context_subject', true),
                          'UTF8'
                      )),
                      'hex'
                  ) || ':' || current_setting('bioagent.context_is_admin', true)
                    || ':' || current_setting('bioagent.context_expires_at', true)
                    || ':' || current_setting('bioagent.context_nonce', true)
                    || ':' || pg_backend_pid()::text,
                  'UTF8'
              ),
              context_key.key_digest,
              'sha256'
          ),
          'hex'
      ) = current_setting('bioagent.context_signature', true)
)
$$
"""


CONTEXT_SUBJECT_FUNCTION = r"""
CREATE FUNCTION bioagent_context_subject()
RETURNS text
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT CASE
    WHEN public.bioagent_signed_context_valid()
        THEN current_setting('bioagent.context_subject', true)
    WHEN NOT COALESCE((
        SELECT enforce_signed FROM public.tenant_context_control WHERE singleton
    ), true)
        THEN current_setting('bioagent.subject', true)
    ELSE ''
END
$$
"""


CONTEXT_ADMIN_FUNCTION = r"""
CREATE FUNCTION bioagent_context_is_admin()
RETURNS boolean
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT CASE
    WHEN public.bioagent_signed_context_valid()
        THEN current_setting('bioagent.context_is_admin', true) = '1'
    WHEN NOT COALESCE((
        SELECT enforce_signed FROM public.tenant_context_control WHERE singleton
    ), true)
        THEN current_setting('bioagent.is_admin', true) = 'true'
    ELSE false
END
$$
"""


CONTEXT_ENFORCED_FUNCTION = r"""
CREATE FUNCTION bioagent_signed_context_enforced()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT COALESCE((
    SELECT enforce_signed FROM public.tenant_context_control WHERE singleton
), false)
$$
"""


API_PROJECT_ACCESS_FUNCTION = r"""
CREATE OR REPLACE FUNCTION bioagent_api_can_access_project(
    target_project_id varchar,
    require_write boolean
)
RETURNS boolean
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT
    pg_has_role(session_user, 'bioagent_api', 'member')
    AND (
        public.bioagent_context_is_admin()
        OR EXISTS (
            SELECT 1
            FROM public.project_members AS member
            WHERE member.project_id = target_project_id
              AND member.subject = public.bioagent_context_subject()
              AND (
                  NOT require_write
                  OR member.role IN ('owner', 'editor')
              )
        )
    )
$$
"""


CURRENT_PROJECT_ROLE_FUNCTION = r"""
CREATE OR REPLACE FUNCTION bioagent_current_project_role(target_project_id varchar)
RETURNS text
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT CASE
    WHEN project.owner_subject = public.bioagent_context_subject()
        THEN 'owner'
    ELSE member.role
END
FROM public.projects AS project
LEFT JOIN public.project_members AS member
  ON member.project_id = project.project_id
 AND member.subject = public.bioagent_context_subject()
WHERE project.project_id = target_project_id
LIMIT 1
$$
"""


LEGACY_API_PROJECT_ACCESS_FUNCTION = r"""
CREATE OR REPLACE FUNCTION bioagent_api_can_access_project(
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


LEGACY_CURRENT_PROJECT_ROLE_FUNCTION = r"""
CREATE OR REPLACE FUNCTION bioagent_current_project_role(target_project_id varchar)
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT CASE
    WHEN project.owner_subject = current_setting('bioagent.subject', true)
        THEN 'owner'
    ELSE member.role
END
FROM public.projects AS project
LEFT JOIN public.project_members AS member
  ON member.project_id = project.project_id
 AND member.subject = current_setting('bioagent.subject', true)
WHERE project.project_id = target_project_id
LIMIT 1
$$
"""


CREATE_AUTH_SESSION_FUNCTION = r"""
CREATE FUNCTION bioagent_create_auth_session(
    supplied_jti varchar,
    supplied_subject varchar,
    supplied_roles_sha256 varchar,
    supplied_ttl_seconds integer
)
RETURNS jsonb
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
WITH clock_value AS (
    SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision AS epoch
), subject_state AS (
    INSERT INTO public.auth_subjects (
        subject, token_version, disabled, updated_at
    )
    SELECT supplied_subject, 0, false, statement_timestamp()::text
    WHERE pg_has_role(session_user, 'bioagent_api', 'member')
      AND supplied_jti ~ '^[A-Za-z0-9_-]{20,80}$'
      AND supplied_subject <> ''
      AND supplied_roles_sha256 ~ '^[0-9a-f]{64}$'
      AND supplied_ttl_seconds BETWEEN 60 AND 86400
    ON CONFLICT (subject) DO UPDATE SET subject = EXCLUDED.subject
    RETURNING subject, token_version, disabled
), created AS (
    INSERT INTO public.auth_sessions (
        jti, subject, token_version, roles_sha256,
        issued_at, expires_at, revoked_at, created_at
    )
    SELECT
        supplied_jti,
        subject_state.subject,
        subject_state.token_version,
        supplied_roles_sha256,
        clock_value.epoch,
        clock_value.epoch + supplied_ttl_seconds,
        NULL,
        statement_timestamp()::text
    FROM subject_state, clock_value
    WHERE NOT subject_state.disabled
    ON CONFLICT (jti) DO NOTHING
    RETURNING jti, subject, token_version, issued_at, expires_at
)
SELECT jsonb_build_object(
    'jti', created.jti,
    'subject', created.subject,
    'token_version', created.token_version,
    'issued_at', floor(created.issued_at)::bigint,
    'expires_at', floor(created.expires_at)::bigint
)
FROM created
$$
"""


VALIDATE_AUTH_SESSION_FUNCTION = r"""
CREATE FUNCTION bioagent_validate_auth_session(
    supplied_jti varchar,
    supplied_subject varchar,
    supplied_token_version integer,
    supplied_roles_sha256 varchar
)
RETURNS boolean
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT EXISTS (
    SELECT 1
    FROM public.auth_sessions AS auth_session
    JOIN public.auth_subjects AS auth_subject
      ON auth_subject.subject = auth_session.subject
    WHERE pg_has_role(session_user, 'bioagent_api', 'member')
      AND auth_session.jti = supplied_jti
      AND auth_session.subject = supplied_subject
      AND auth_session.token_version = supplied_token_version
      AND auth_session.roles_sha256 = supplied_roles_sha256
      AND auth_session.revoked_at IS NULL
      AND auth_session.expires_at > EXTRACT(EPOCH FROM clock_timestamp())
      AND NOT auth_subject.disabled
      AND auth_subject.token_version = supplied_token_version
)
$$
"""


REVOKE_AUTH_SESSION_FUNCTION = r"""
CREATE FUNCTION bioagent_revoke_auth_session(supplied_jti varchar)
RETURNS boolean
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
WITH revoked AS (
    UPDATE public.auth_sessions AS auth_session
    SET revoked_at = EXTRACT(EPOCH FROM clock_timestamp())
    WHERE pg_has_role(session_user, 'bioagent_api', 'member')
      AND auth_session.jti = supplied_jti
      AND auth_session.revoked_at IS NULL
      AND (
          auth_session.subject = public.bioagent_context_subject()
          OR public.bioagent_context_is_admin()
      )
    RETURNING true
)
SELECT COALESCE((SELECT true FROM revoked LIMIT 1), false)
$$
"""


REVOKE_SUBJECT_FUNCTION = r"""
CREATE FUNCTION bioagent_revoke_subject_sessions(
    supplied_subject varchar,
    supplied_disabled boolean
)
RETURNS boolean
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
WITH changed_subject AS (
    UPDATE public.auth_subjects AS auth_subject
    SET token_version = auth_subject.token_version + 1,
        disabled = supplied_disabled,
        updated_at = statement_timestamp()::text
    WHERE pg_has_role(session_user, 'bioagent_api', 'member')
      AND public.bioagent_context_is_admin()
      AND auth_subject.subject = supplied_subject
    RETURNING auth_subject.subject
), revoked AS (
    UPDATE public.auth_sessions AS auth_session
    SET revoked_at = COALESCE(
        auth_session.revoked_at,
        EXTRACT(EPOCH FROM clock_timestamp())
    )
    FROM changed_subject
    WHERE auth_session.subject = changed_subject.subject
    RETURNING true
)
SELECT EXISTS (SELECT 1 FROM changed_subject)
$$
"""


def upgrade():
    op.execute(sa.text('CREATE EXTENSION IF NOT EXISTS pgcrypto'))
    op.create_table(
        'tenant_context_keys',
        sa.Column('key_id', sa.String(length=64), primary_key=True),
        sa.Column('key_digest', sa.LargeBinary(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.String(length=64), nullable=False),
    )
    op.create_table(
        'tenant_context_control',
        sa.Column('singleton', sa.Boolean(), primary_key=True),
        sa.Column('enforce_signed', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('updated_at', sa.String(length=64), nullable=False),
    )
    op.create_table(
        'auth_subjects',
        sa.Column('subject', sa.String(length=255), primary_key=True),
        sa.Column('token_version', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('disabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('updated_at', sa.String(length=64), nullable=False),
    )
    op.create_table(
        'auth_sessions',
        sa.Column('jti', sa.String(length=80), primary_key=True),
        sa.Column('subject', sa.String(length=255), nullable=False),
        sa.Column('token_version', sa.Integer(), nullable=False),
        sa.Column('roles_sha256', sa.String(length=64), nullable=False),
        sa.Column('issued_at', sa.Float(), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('revoked_at', sa.Float(), nullable=True),
        sa.Column('created_at', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['subject'], ['auth_subjects.subject'], ondelete='CASCADE'),
    )
    op.create_index('ix_auth_sessions_subject', 'auth_sessions', ['subject'])
    op.create_index('ix_auth_sessions_expires_at', 'auth_sessions', ['expires_at'])
    op.execute(sa.text(
        "INSERT INTO tenant_context_control (singleton, enforce_signed, updated_at) "
        "VALUES (true, false, statement_timestamp()::text)"
    ))
    op.execute(sa.text(CONTEXT_VALID_FUNCTION))
    op.execute(sa.text(CONTEXT_SUBJECT_FUNCTION))
    op.execute(sa.text(CONTEXT_ADMIN_FUNCTION))
    op.execute(sa.text(CONTEXT_ENFORCED_FUNCTION))
    op.execute(sa.text(API_PROJECT_ACCESS_FUNCTION))
    op.execute(sa.text(CURRENT_PROJECT_ROLE_FUNCTION))
    op.execute(sa.text(CREATE_AUTH_SESSION_FUNCTION))
    op.execute(sa.text(VALIDATE_AUTH_SESSION_FUNCTION))
    op.execute(sa.text(REVOKE_AUTH_SESSION_FUNCTION))
    op.execute(sa.text(REVOKE_SUBJECT_FUNCTION))
    op.execute(sa.text(
        'REVOKE ALL ON tenant_context_keys, tenant_context_control, '
        'auth_subjects, auth_sessions FROM PUBLIC, bioagent_api, '
        'bioagent_worker, bioagent_dispatcher'
    ))
    op.execute(sa.text(
        'REVOKE ALL ON FUNCTION bioagent_signed_context_valid(), '
        'bioagent_signed_context_enforced(), bioagent_context_subject(), '
        'bioagent_context_is_admin(), '
        'bioagent_create_auth_session(varchar, varchar, varchar, integer), '
        'bioagent_validate_auth_session(varchar, varchar, integer, varchar), '
        'bioagent_revoke_auth_session(varchar), '
        'bioagent_revoke_subject_sessions(varchar, boolean) FROM PUBLIC'
    ))
    op.execute(sa.text(
        'GRANT EXECUTE ON FUNCTION bioagent_signed_context_valid(), '
        'bioagent_signed_context_enforced(), bioagent_context_subject(), '
        'bioagent_context_is_admin(), '
        'bioagent_create_auth_session(varchar, varchar, varchar, integer), '
        'bioagent_validate_auth_session(varchar, varchar, integer, varchar), '
        'bioagent_revoke_auth_session(varchar), '
        'bioagent_revoke_subject_sessions(varchar, boolean) TO bioagent_api'
    ))
    op.execute(sa.text(
        'ALTER POLICY projects_tenant_read ON projects USING ('
        'bioagent_context_is_admin() '
        'OR bioagent_current_project_role(project_id) IS NOT NULL)'
    ))
    op.execute(sa.text(
        'ALTER POLICY projects_tenant_insert ON projects WITH CHECK ('
        'bioagent_context_is_admin() '
        'OR owner_subject = bioagent_context_subject())'
    ))
    op.execute(sa.text(
        'ALTER POLICY projects_tenant_update ON projects USING ('
        'bioagent_context_is_admin() '
        "OR bioagent_current_project_role(project_id) = 'owner') "
        'WITH CHECK (bioagent_context_is_admin() '
        'OR owner_subject = bioagent_context_subject())'
    ))
    op.execute(sa.text(
        'ALTER POLICY project_members_tenant_read ON project_members USING ('
        'bioagent_context_is_admin() '
        'OR bioagent_current_project_role(project_id) IS NOT NULL)'
    ))
    op.execute(sa.text(
        'ALTER POLICY project_members_tenant_insert ON project_members WITH CHECK (('
        'bioagent_context_is_admin() '
        "OR bioagent_current_project_role(project_id) = 'owner') "
        'AND ((subject = bioagent_project_owner_subject(project_id) '
        "AND role = 'owner') OR (subject <> bioagent_project_owner_subject(project_id) "
        "AND role <> 'owner')))"
    ))
    op.execute(sa.text(
        'ALTER POLICY project_members_tenant_update ON project_members USING ('
        'bioagent_context_is_admin() '
        "OR bioagent_current_project_role(project_id) = 'owner') "
        'WITH CHECK ((bioagent_context_is_admin() '
        "OR bioagent_current_project_role(project_id) = 'owner') "
        'AND ((subject = bioagent_project_owner_subject(project_id) '
        "AND role = 'owner') OR (subject <> bioagent_project_owner_subject(project_id) "
        "AND role <> 'owner')))"
    ))
    op.execute(sa.text(
        'ALTER POLICY project_members_tenant_delete ON project_members USING (('
        'bioagent_context_is_admin() '
        "OR bioagent_current_project_role(project_id) = 'owner') "
        'AND subject <> bioagent_project_owner_subject(project_id))'
    ))
    op.execute(sa.text(
        'ALTER POLICY job_idempotency_tenant_access ON job_idempotency USING ('
        'subject = bioagent_context_subject() '
        'AND bioagent_api_can_access_project(project_id, false)) '
        'WITH CHECK (subject = bioagent_context_subject() '
        'AND bioagent_api_can_access_project(project_id, true))'
    ))


def downgrade():
    op.execute(sa.text(
        'ALTER POLICY job_idempotency_tenant_access ON job_idempotency USING ('
        "subject = current_setting('bioagent.subject', true) "
        'AND bioagent_api_can_access_project(project_id, false)) '
        "WITH CHECK (subject = current_setting('bioagent.subject', true) "
        'AND bioagent_api_can_access_project(project_id, true))'
    ))
    op.execute(sa.text(LEGACY_API_PROJECT_ACCESS_FUNCTION))
    op.execute(sa.text(LEGACY_CURRENT_PROJECT_ROLE_FUNCTION))
    op.execute(sa.text(
        'ALTER POLICY projects_tenant_read ON projects USING ('
        "current_setting('bioagent.is_admin', true) = 'true' "
        'OR bioagent_current_project_role(project_id) IS NOT NULL)'
    ))
    op.execute(sa.text(
        'ALTER POLICY projects_tenant_insert ON projects WITH CHECK ('
        "current_setting('bioagent.is_admin', true) = 'true' "
        "OR owner_subject = current_setting('bioagent.subject', true))"
    ))
    op.execute(sa.text(
        'ALTER POLICY projects_tenant_update ON projects USING ('
        "current_setting('bioagent.is_admin', true) = 'true' "
        "OR bioagent_current_project_role(project_id) = 'owner') "
        "WITH CHECK (current_setting('bioagent.is_admin', true) = 'true' "
        "OR owner_subject = current_setting('bioagent.subject', true))"
    ))
    op.execute(sa.text(
        'ALTER POLICY project_members_tenant_read ON project_members USING ('
        "current_setting('bioagent.is_admin', true) = 'true' "
        'OR bioagent_current_project_role(project_id) IS NOT NULL)'
    ))
    op.execute(sa.text(
        'ALTER POLICY project_members_tenant_insert ON project_members WITH CHECK (('
        "current_setting('bioagent.is_admin', true) = 'true' "
        "OR bioagent_current_project_role(project_id) = 'owner') "
        'AND ((subject = bioagent_project_owner_subject(project_id) '
        "AND role = 'owner') OR (subject <> bioagent_project_owner_subject(project_id) "
        "AND role <> 'owner')))"
    ))
    op.execute(sa.text(
        'ALTER POLICY project_members_tenant_update ON project_members USING ('
        "current_setting('bioagent.is_admin', true) = 'true' "
        "OR bioagent_current_project_role(project_id) = 'owner') "
        "WITH CHECK ((current_setting('bioagent.is_admin', true) = 'true' "
        "OR bioagent_current_project_role(project_id) = 'owner') "
        'AND ((subject = bioagent_project_owner_subject(project_id) '
        "AND role = 'owner') OR (subject <> bioagent_project_owner_subject(project_id) "
        "AND role <> 'owner')))"
    ))
    op.execute(sa.text(
        'ALTER POLICY project_members_tenant_delete ON project_members USING (('
        "current_setting('bioagent.is_admin', true) = 'true' "
        "OR bioagent_current_project_role(project_id) = 'owner') "
        'AND subject <> bioagent_project_owner_subject(project_id))'
    ))
    for signature in (
        'bioagent_revoke_subject_sessions(varchar, boolean)',
        'bioagent_revoke_auth_session(varchar)',
        'bioagent_validate_auth_session(varchar, varchar, integer, varchar)',
        'bioagent_create_auth_session(varchar, varchar, varchar, integer)',
        'bioagent_context_is_admin()',
        'bioagent_context_subject()',
        'bioagent_signed_context_enforced()',
        'bioagent_signed_context_valid()',
    ):
        op.execute(sa.text(f'DROP FUNCTION IF EXISTS {signature}'))
    op.drop_index('ix_auth_sessions_expires_at', table_name='auth_sessions')
    op.drop_index('ix_auth_sessions_subject', table_name='auth_sessions')
    op.drop_table('auth_sessions')
    op.drop_table('auth_subjects')
    op.drop_table('tenant_context_control')
    op.drop_table('tenant_context_keys')
