"""Compatibility and project ownership contracts; no runtime side effects."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from alembic import command
from alembic.config import Config
from sqlalchemy import text, select, inspect
from sqlalchemy.exc import IntegrityError
from app import database as models
from app.settings import AppSettings
from app.services import Configurations
from app.worker_configs import bootstrap

ROOT = Path(__file__).resolve().parents[3]
DEFAULT = "00000000-0000-0000-0000-000000000001"


class ProjectTests(unittest.TestCase):
    def test_project_has_multiple_repository_aliases(self):
        self.assertTrue(hasattr(models, "RepositoryConnection"), "project repositories missing")
        engine, sessions = models.database("sqlite://")
        self.addCleanup(engine.dispose)
        models.Base.metadata.create_all(engine)
        with sessions.begin() as db:
            db.add(models.Project(id=DEFAULT, slug="default", name="Default"))
            db.flush()
            for alias, name in [("backend", "api"), ("frontend", "web")]:
                db.add(models.RepositoryConnection(project_id=DEFAULT, alias=alias,
                    provider="github", host="github.com", owner="example", name=name))
        with sessions() as db:
            self.assertEqual([r.alias for r in db.scalars(select(models.RepositoryConnection).order_by(models.RepositoryConnection.alias))], ["backend", "frontend"])
        with self.assertRaises(IntegrityError), sessions.begin() as db:
            db.add(models.RepositoryConnection(project_id=DEFAULT, alias="backend",
                provider="github", host="github.com", owner="other", name="repo"))

    def test_domain_project_does_not_change_compose_discovery(self):
        self.assertIn("compose_project_name", AppSettings.__dataclass_fields__)
        from app.adapters.docker import DockerRuntime
        settings = AppSettings("sqlite://", "pw", "key", ROOT, ROOT, ROOT, ROOT,
                               compose_project_name="hatchet-test")
        container = Mock(id="existing", name="worker", status="running",
                         labels={"com.docker.compose.project":"hatchet-test", "com.docker.compose.service":"worker"},
                         attrs={"Config":{"Image":"factory-worker"}})
        client = Mock()
        client.containers.list.return_value = [container]
        self.assertEqual(DockerRuntime(settings, Mock(), client).list()[0]["id"], "existing")

    def test_bootstrap_is_project_scoped(self):
        self.assertTrue(hasattr(models, "Project"), "project bootstrap missing")
        engine, sessions = models.database("sqlite://")
        self.addCleanup(engine.dispose)
        models.Base.metadata.create_all(engine)
        config = Configurations(ROOT)
        for identity, slug in [(DEFAULT, "default"), ("00000000-0000-0000-0000-000000000002", "second")]:
            with sessions.begin() as db:
                db.add(models.Project(id=identity, slug=slug, name=slug))
                db.flush()
                db.add(models.Revision(project_id=identity, summary="seed", document=config.approved, digest=config.hash(config.approved)))
            with sessions.begin() as db:
                bootstrap(db, config, project_id=identity)
        with sessions() as db:
            configs = db.scalars(select(models.WorkerConfiguration)).all()
            self.assertEqual({c.project_id for c in configs}, {DEFAULT, "00000000-0000-0000-0000-000000000002"})
            self.assertEqual(len(configs), 4)

    def test_upgrade_preserves_populated_legacy_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = os.environ.get("CONTROL_MIGRATION_TEST_URL", "sqlite:///" + tmp + "/migration.db")
            cfg = Config(str(ROOT / "control-plane/backend/alembic.ini"))
            with patch.dict(os.environ, {"CONTROL_DATABASE_URL": url}):
                command.upgrade(cfg, "c82a1d6e40f3")
                engine, _ = models.database(url)
                self.addCleanup(engine.dispose)
                document = {"defaults":{"repository":"phudekar/software-factory-test"}}
                with engine.begin() as connection:
                    connection.execute(text("INSERT INTO config_revisions (id,scope,parent_id,created_at,summary,document,digest) VALUES ('old','factory',NULL,1,'legacy',:doc,'unchanged')"), {"doc":json.dumps(document)})
                    connection.execute(text("INSERT INTO worker_instances (id,name,profile,revision_id,config_id,target_id,credential_hash,desired_state,created_at) VALUES ('worker','existing','simple','old',NULL,'local-docker','hash','running',1)"))
                    connection.execute(text("INSERT INTO execution_events (id,kind,worker_id,occurred_at,received_at,payload) VALUES ('event','heartbeat','worker',1,1,'{}')"))
                command.upgrade(cfg, "e201f1000001")
                with engine.connect() as connection:
                    self.assertIn("project_id", [column["name"] for column in inspect(connection).get_columns("config_revisions")])
                    revision = connection.execute(text("SELECT id,document,digest,project_id FROM config_revisions")).one()
                    self.assertEqual((revision.id, (json.loads(revision.document) if isinstance(revision.document, str) else revision.document), revision.digest, revision.project_id), ("old", document, "unchanged", DEFAULT))
                    self.assertEqual(connection.execute(text("SELECT revision_id,desired_state FROM worker_instances")).one(), ("old", "running"))
                    self.assertEqual(connection.execute(text("SELECT project_id,repository_id FROM execution_events")).one(), (DEFAULT,None))
                    self.assertEqual(connection.execute(text("SELECT COUNT(*) FROM repository_connections")).scalar(), 0)
