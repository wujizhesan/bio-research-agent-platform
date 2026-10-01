"""enforce tenant row-level security"""

from alembic import op
import sqlalchemy as sa


revision = '0013_tenant_rls'
down_revision = '0012_tenant_ownership'
branch_labels = None
depends_on = None


READ_ACCESS = """(
    pg_has_role(current_user, 'bioagent_worker', 'member')
    OR current_setting('bioagent.is_admin', true) = 'true'
    OR EXISTS (
        SELECT 1 FROM project_members member
        WHERE member.project_id = {table}.project_id
          AND member.subject = current_setting('bioagent.subject', true)
    )
)"""

WRITE_ACCESS = """(
    pg_has_role(current_user, 'bioagent_worker', 'member')
    OR current_setting('bioagent.is_admin', true) = 'true'
    OR EXISTS (
        SELECT 1 FROM project_members member
        WHERE member.project_id = {table}.project_id
          AND member.subject = current_setting('bioagent.subject', true)
          AND member.role IN ('owner', 'editor')
    )
)"""

JOB_CHILD_READ_ACCESS = """(
    pg_has_role(current_user, 'bioagent_worker', 'member')
    OR current_setting('bioagent.is_admin', true) = 'true'
    OR EXISTS (
        SELECT 1 FROM job_records job
        JOIN project_members member ON member.project_id = job.project_id
        WHERE job.job_id = {table}.job_id
          AND member.subject = current_setting('bioagent.subject', true)
    )
)"""

JOB_CHILD_WRITE_ACCESS = """(
    pg_has_role(current_user, 'bioagent_worker', 'member')
    OR current_setting('bioagent.is_admin', true) = 'true'
    OR EXISTS (
        SELECT 1 FROM job_records job
        JOIN project_members member ON member.project_id = job.project_id
        WHERE job.job_id = {table}.job_id
          AND member.subject = current_setting('bioagent.subject', true)
          AND member.role IN ('owner', 'editor')
    )
)"""


def upgrade():
    op.execute(sa.text(
        "GRANT USAGE ON SCHEMA public TO bioagent_api, bioagent_worker"
    ))
    op.execute(sa.text(
        "GRANT SELECT, INSERT, UPDATE ON projects, project_members "
        "TO bioagent_api"
    ))
    op.execute(sa.text(
        "GRANT SELECT ON projects, project_members TO bioagent_worker"
    ))
    op.execute(sa.text(
        "GRANT SELECT, INSERT, UPDATE ON job_records, job_projects, "
        "file_records, file_projects TO bioagent_api, bioagent_worker"
    ))
    op.execute(sa.text(
        "GRANT SELECT, INSERT ON job_events TO bioagent_api, bioagent_worker"
    ))
    op.execute(sa.text(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON job_outbox "
        "TO bioagent_api, bioagent_worker"
    ))
    op.execute(sa.text(
        "GRANT SELECT, INSERT, UPDATE ON job_execution_results "
        "TO bioagent_api, bioagent_worker"
    ))

    op.execute(sa.text("ALTER TABLE job_records ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE job_records FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(
        "CREATE POLICY job_records_tenant_read ON job_records FOR SELECT USING "
        + READ_ACCESS.format(table='job_records')
    ))
    op.execute(sa.text(
        "CREATE POLICY job_records_tenant_write ON job_records FOR ALL USING "
        + WRITE_ACCESS.format(table='job_records')
        + " WITH CHECK " + WRITE_ACCESS.format(table='job_records')
    ))

    op.execute(sa.text("ALTER TABLE file_records ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE file_records FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(
        "CREATE POLICY file_records_tenant_read ON file_records FOR SELECT USING "
        + READ_ACCESS.format(table='file_records')
    ))
    op.execute(sa.text(
        "CREATE POLICY file_records_tenant_write ON file_records FOR ALL USING "
        + WRITE_ACCESS.format(table='file_records')
        + " WITH CHECK " + WRITE_ACCESS.format(table='file_records')
    ))

    op.execute(sa.text("ALTER TABLE job_projects ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE job_projects FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(
        "CREATE POLICY job_projects_tenant_read ON job_projects FOR SELECT USING "
        + READ_ACCESS.format(table='job_projects')
    ))
    op.execute(sa.text(
        "CREATE POLICY job_projects_tenant_write ON job_projects FOR ALL USING "
        + WRITE_ACCESS.format(table='job_projects')
        + " WITH CHECK " + WRITE_ACCESS.format(table='job_projects')
    ))

    op.execute(sa.text("ALTER TABLE file_projects ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE file_projects FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(
        "CREATE POLICY file_projects_tenant_read ON file_projects FOR SELECT USING "
        + READ_ACCESS.format(table='file_projects')
    ))
    op.execute(sa.text(
        "CREATE POLICY file_projects_tenant_write ON file_projects FOR ALL USING "
        + WRITE_ACCESS.format(table='file_projects')
        + " WITH CHECK " + WRITE_ACCESS.format(table='file_projects')
    ))

    op.execute(sa.text("ALTER TABLE job_events ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE job_events FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(
        "CREATE POLICY job_events_tenant_read ON job_events FOR SELECT USING "
        + JOB_CHILD_READ_ACCESS.format(table='job_events')
    ))
    op.execute(sa.text(
        "CREATE POLICY job_events_tenant_write ON job_events FOR ALL USING "
        + JOB_CHILD_WRITE_ACCESS.format(table='job_events')
        + " WITH CHECK " + JOB_CHILD_WRITE_ACCESS.format(table='job_events')
    ))

    op.execute(sa.text("ALTER TABLE job_outbox ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE job_outbox FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(
        "CREATE POLICY job_outbox_tenant_read ON job_outbox FOR SELECT USING "
        + JOB_CHILD_READ_ACCESS.format(table='job_outbox')
    ))
    op.execute(sa.text(
        "CREATE POLICY job_outbox_tenant_write ON job_outbox FOR ALL USING "
        + JOB_CHILD_WRITE_ACCESS.format(table='job_outbox')
        + " WITH CHECK " + JOB_CHILD_WRITE_ACCESS.format(table='job_outbox')
    ))

    op.execute(sa.text(
        "ALTER TABLE job_execution_results ENABLE ROW LEVEL SECURITY"
    ))
    op.execute(sa.text(
        "ALTER TABLE job_execution_results FORCE ROW LEVEL SECURITY"
    ))
    op.execute(sa.text(
        "CREATE POLICY job_execution_results_tenant_read ON "
        "job_execution_results FOR SELECT USING "
        + JOB_CHILD_READ_ACCESS.format(table='job_execution_results')
    ))
    op.execute(sa.text(
        "CREATE POLICY job_execution_results_tenant_write ON "
        "job_execution_results FOR ALL USING "
        + JOB_CHILD_WRITE_ACCESS.format(table='job_execution_results')
        + " WITH CHECK "
        + JOB_CHILD_WRITE_ACCESS.format(table='job_execution_results')
    ))


def downgrade():
    for table, policies in (
        ('job_execution_results', ('job_execution_results_tenant_read', 'job_execution_results_tenant_write')),
        ('job_outbox', ('job_outbox_tenant_read', 'job_outbox_tenant_write')),
        ('job_events', ('job_events_tenant_read', 'job_events_tenant_write')),
        ('file_projects', ('file_projects_tenant_read', 'file_projects_tenant_write')),
        ('job_projects', ('job_projects_tenant_read', 'job_projects_tenant_write')),
        ('file_records', ('file_records_tenant_read', 'file_records_tenant_write')),
        ('job_records', ('job_records_tenant_read', 'job_records_tenant_write')),
    ):
        for policy in policies:
            op.execute(sa.text(f'DROP POLICY IF EXISTS {policy} ON {table}'))
        op.execute(sa.text(f'ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'ALTER TABLE {table} DISABLE ROW LEVEL SECURITY'))
