"""allow time-limited overlap of signed tenant context keys"""

from alembic import op
import sqlalchemy as sa


revision = '0027_tenant_key_rotation'
down_revision = '0026_durable_audit_events'
branch_labels = None
depends_on = None


CONTEXT_VALID_FUNCTION = r"""
CREATE OR REPLACE FUNCTION bioagent_signed_context_valid()
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
      AND (context_key.valid_until IS NULL
           OR context_key.valid_until > EXTRACT(EPOCH FROM clock_timestamp()))
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


def upgrade():
    op.add_column('tenant_context_keys', sa.Column('valid_until', sa.Float(), nullable=True))
    op.execute(sa.text(CONTEXT_VALID_FUNCTION))


def downgrade():
    old_function = CONTEXT_VALID_FUNCTION.replace(
        "      AND (context_key.valid_until IS NULL\n"
        "           OR context_key.valid_until > EXTRACT(EPOCH FROM clock_timestamp()))\n",
        '',
    )
    op.execute(sa.text(old_function))
    op.drop_column('tenant_context_keys', 'valid_until')
