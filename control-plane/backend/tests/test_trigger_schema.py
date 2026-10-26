"""Trigger storage fences references to their owning project and binding."""

import unittest
from sqlalchemy import create_engine, inspect
from app import database as models


class TriggerSchemaTests(unittest.TestCase):
    def test_trigger_tables_have_scoped_foreign_keys_and_no_plaintext_payload(self):
        engine = create_engine("sqlite://")
        try:
            models.Base.metadata.create_all(engine)
            schema = inspect(engine)
            expected = {
                "repository_triggers": {
                    "repository_connections",
                    "workflow_definitions",
                },
                "repository_trigger_tokens": {
                    "repository_triggers",
                    "repository_trigger_tokens",
                },
                "repository_trigger_receipts": {
                    "repository_triggers",
                    "workflow_runs",
                    "workflow_definition_revisions",
                    "repository_revisions",
                },
                "repository_trigger_observations": {
                    "repository_triggers",
                    "workflow_runs",
                },
            }
            for table, targets in expected.items():
                self.assertIn(table, schema.get_table_names())
                foreign = schema.get_foreign_keys(table)
                for target in targets:
                    self.assertTrue(
                        any(
                            f["referred_table"] == target
                            and "project_id" in f["constrained_columns"]
                            for f in foreign
                        ),
                        (table, target),
                    )
            columns = {
                c["name"] for c in schema.get_columns("repository_trigger_receipts")
            }
            self.assertTrue(
                {
                    "payload_digest",
                    "workflow_revision_id",
                    "repository_revision_id",
                    "run_id",
                }
                <= columns
            )
            self.assertFalse({"payload", "body", "token", "webhook_token"} & columns)
            token_columns = {
                c["name"] for c in schema.get_columns("repository_trigger_tokens")
            }
            self.assertIn("ciphertext", token_columns)
            self.assertNotIn("token", token_columns)
        finally:
            engine.dispose()

    def test_additive_migration_preserves_old_project_and_matches_models(self):
        import os
        import tempfile
        import uuid
        from pathlib import Path
        from unittest.mock import patch
        from sqlalchemy import text
        from sqlalchemy.engine import make_url
        from alembic.config import Config
        from alembic import command
        from alembic.migration import MigrationContext
        from alembic.autogenerate import compare_metadata

        with tempfile.TemporaryDirectory() as temporary:
            url = os.environ.get(
                "CONTROL_MIGRATION_TEST_URL", "sqlite:///" + temporary + "/trigger.db"
            )
            admin = None
            if url.startswith("postgresql"):
                admin = create_engine(url)
                schema = "trigger_" + uuid.uuid4().hex
                with admin.begin() as connection:
                    connection.execute(text('CREATE SCHEMA "' + schema + '"'))
                url = (
                    make_url(url)
                    .update_query_dict({"options": "-csearch_path=" + schema})
                    .render_as_string(hide_password=False)
                )
            engine = create_engine(url)
            try:
                cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
                with patch.dict(os.environ, {"CONTROL_DATABASE_URL": url}):
                    command.upgrade(cfg, "e201f1000010")
                    with engine.begin() as connection:
                        connection.execute(
                            text(
                                "INSERT INTO projects (id, slug, name, defaults, created_at) VALUES ('historic', 'historic', 'Historic', '{}', 1)"
                            )
                        )
                    command.upgrade(cfg, "head")
                    with engine.connect() as connection:
                        self.assertEqual(
                            connection.execute(
                                text("SELECT name FROM projects WHERE id='historic'")
                            ).scalar_one(),
                            "Historic",
                        )

                        # SQLite VARCHAR bounds are advisory; migration 0010 intentionally
                        # preserves its referenced table rather than rebuilding it.
                        def compare_type(
                            context,
                            inspected_column,
                            metadata_column,
                            inspected_type,
                            metadata_type,
                        ):
                            if (
                                context.dialect.name == "sqlite"
                                and inspected_column.table.name
                                == "repository_connections"
                                and inspected_column.name == "owner"
                            ):
                                return False
                            return None

                        changes = compare_metadata(
                            MigrationContext.configure(
                                connection, opts={"compare_type": compare_type}
                            ),
                            models.Base.metadata,
                        )
                        self.assertEqual(changes, [])
                    command.downgrade(cfg, "e201f1000010")
                    self.assertNotIn(
                        "repository_triggers", inspect(engine).get_table_names()
                    )
                    command.upgrade(cfg, "head")
            finally:
                engine.dispose()
                if admin:
                    with admin.begin() as connection:
                        connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
                    admin.dispose()
