"""protect project membership control plane with row-level security"""

from alembic import op
import sqlalchemy as sa


revision = '0014_project_control_plane_rls'
down_revision = '0013_tenant_rls'
branch_labels = None
depends_on = None


PROJECT_ROLE_FUNCTION = """
CREATE FUNCTION bioagent_current_project_role(target_project_id varchar)
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS 'SELECT CASE
    WHEN project.owner_subject = current_setting(''bioagent.subject'', true)
        THEN ''owner''
    ELSE member.role
END
FROM public.projects AS project
LEFT JOIN public.project_members AS member
  ON member.project_id = project.project_id
 AND member.subject = current_setting(''bioagent.subject'', true)
WHERE project.project_id = target_project_id
LIMIT 1'
"""


PROJECT_OWNER_FUNCTION = """
CREATE FUNCTION bioagent_project_owner_subject(target_project_id varchar)
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS 'SELECT project.owner_subject
FROM public.projects AS project
WHERE project.project_id = target_project_id
LIMIT 1'
"""


WORKER_ACCESS = "pg_has_role(current_user, 'bioagent_worker', 'member')"
ADMIN_ACCESS = "current_setting('bioagent.is_admin', true) = 'true'"
MEMBER_ACCESS = "bioagent_current_project_role(project_id) IS NOT NULL"
OWNER_ACCESS = "bioagent_current_project_role(project_id) = 'owner'"
OWNER_INVARIANT = """(
    (subject = bioagent_project_owner_subject(project_id) AND role = 'owner')
    OR
    (subject <> bioagent_project_owner_subject(project_id) AND role <> 'owner')
)"""


def upgrade():
    op.execute(sa.text(PROJECT_OWNER_FUNCTION))
    op.execute(sa.text(PROJECT_ROLE_FUNCTION))
    op.execute(sa.text(
        "REVOKE ALL ON FUNCTION bioagent_project_owner_subject(varchar) FROM PUBLIC"
    ))
    op.execute(sa.text(
        "REVOKE ALL ON FUNCTION bioagent_current_project_role(varchar) FROM PUBLIC"
    ))
    op.execute(sa.text(
        "GRANT EXECUTE ON FUNCTION bioagent_project_owner_subject(varchar) "
        "TO bioagent_api, bioagent_worker"
    ))
    op.execute(sa.text(
        "GRANT EXECUTE ON FUNCTION bioagent_current_project_role(varchar) "
        "TO bioagent_api, bioagent_worker"
    ))
    op.execute(sa.text(
        "GRANT DELETE ON project_members TO bioagent_api"
    ))

    op.execute(sa.text("ALTER TABLE projects ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(
        "CREATE POLICY projects_tenant_read ON projects FOR SELECT USING ("
        + WORKER_ACCESS + " OR " + ADMIN_ACCESS + " OR " + MEMBER_ACCESS + ")"
    ))
    op.execute(sa.text(
        "CREATE POLICY projects_tenant_insert ON projects FOR INSERT WITH CHECK ("
        + ADMIN_ACCESS
        + " OR owner_subject = current_setting('bioagent.subject', true))"
    ))
    op.execute(sa.text(
        "CREATE POLICY projects_tenant_update ON projects FOR UPDATE USING ("
        + ADMIN_ACCESS + " OR " + OWNER_ACCESS + ") WITH CHECK ("
        + ADMIN_ACCESS
        + " OR owner_subject = current_setting('bioagent.subject', true))"
    ))

    op.execute(sa.text("ALTER TABLE project_members ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(
        "CREATE POLICY project_members_tenant_read ON project_members FOR SELECT USING ("
        + WORKER_ACCESS + " OR " + ADMIN_ACCESS + " OR " + MEMBER_ACCESS + ")"
    ))
    op.execute(sa.text(
        "CREATE POLICY project_members_tenant_insert ON project_members FOR INSERT WITH CHECK (("
        + ADMIN_ACCESS + " OR " + OWNER_ACCESS + ") AND "
        + OWNER_INVARIANT + ")"
    ))
    op.execute(sa.text(
        "CREATE POLICY project_members_tenant_update ON project_members FOR UPDATE USING ("
        + ADMIN_ACCESS + " OR " + OWNER_ACCESS + ") "
        "WITH CHECK ((" + ADMIN_ACCESS + " OR " + OWNER_ACCESS + ") "
        "AND " + OWNER_INVARIANT + ")"
    ))
    op.execute(sa.text(
        "CREATE POLICY project_members_tenant_delete ON project_members FOR DELETE USING (("
        + ADMIN_ACCESS + " OR " + OWNER_ACCESS + ") AND "
        "subject <> bioagent_project_owner_subject(project_id))"
    ))


def downgrade():
    for policy in (
        'project_members_tenant_delete',
        'project_members_tenant_update',
        'project_members_tenant_insert',
        'project_members_tenant_read',
    ):
        op.execute(sa.text(f'DROP POLICY IF EXISTS {policy} ON project_members'))
    op.execute(sa.text('ALTER TABLE project_members DISABLE ROW LEVEL SECURITY'))
    for policy in (
        'projects_tenant_update',
        'projects_tenant_insert',
        'projects_tenant_read',
    ):
        op.execute(sa.text(f'DROP POLICY IF EXISTS {policy} ON projects'))
    op.execute(sa.text('ALTER TABLE projects DISABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(
        'DROP FUNCTION IF EXISTS bioagent_current_project_role(varchar)'
    ))
    op.execute(sa.text(
        'DROP FUNCTION IF EXISTS bioagent_project_owner_subject(varchar)'
    ))
