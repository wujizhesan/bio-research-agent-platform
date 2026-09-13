import importlib.util
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_expand_contract_migrations.py"
SPEC = importlib.util.spec_from_file_location("check_expand_contract_migrations", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MigrationCompatibilityTests(unittest.TestCase):
    def write_migration(self, directory, source):
        path = Path(directory) / "0001_test.py"
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return path

    def test_current_migrations_are_expand_contract_compatible(self):
        self.assertEqual(MODULE.check_directory(ROOT / "migrations" / "versions"), [])

    def test_allows_non_null_column_with_server_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_migration(
                directory,
                """
                def upgrade():
                    op.add_column(
                        "jobs",
                        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
                    )

                def downgrade():
                    op.drop_column("jobs", "attempts")
                """,
            )
            self.assertEqual(MODULE.check_migration(path), [])

    def test_rejects_destructive_upgrade_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_migration(
                directory,
                """
                def upgrade():
                    op.drop_column("jobs", "legacy")

                def downgrade():
                    op.add_column("jobs", sa.Column("legacy", sa.String()))
                """,
            )
            violations = MODULE.check_migration(path)
            self.assertEqual(len(violations), 1)
            self.assertIn("op.drop_column", violations[0])

    def test_rejects_required_column_without_server_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_migration(
                directory,
                """
                def upgrade():
                    op.add_column("jobs", sa.Column("owner", sa.String(), nullable=False))

                def downgrade():
                    pass
                """,
            )
            violations = MODULE.check_migration(path)
            self.assertEqual(len(violations), 1)
            self.assertIn("require server_default", violations[0])


if __name__ == "__main__":
    unittest.main()
