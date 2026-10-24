"""Compatibility and project ownership contracts; no runtime side effects."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from alembic import command
from alembic.config import Config
from sqlalchemy import text, select, inspect, create_engine, MetaData, event, Table
from sqlalchemy.engine import make_url
import uuid
from sqlalchemy.exc import IntegrityError
from app import database as models
from app.settings import AppSettings
from app.services import Configurations
from app.worker_configs import bootstrap

ROOT = Path(__file__).resolve().parents[3]
DEFAULT = "00000000-0000-0000-0000-000000000001"


def create_schema(engine):
    # PostgreSQL emits ALTER constraints for the repository/revision cycle.
    # SQLAlchemy's AddConstraint marks its source metadata to suppress inline
    # DDL; do not reuse that mutable DDL state in later SQLite fixtures.
    metadata = MetaData()
    for table in models.Base.metadata.tables.values():
        table.to_metadata(metadata)
    metadata.create_all(engine)


class ProjectTests(unittest.TestCase):
    def test_repository_revisions_are_immutable_references_with_scoped_ownership(self):
        self.assertTrue(hasattr(models, "RepositoryRevision"), "repository revision schema missing")
        engine, sessions = models.database("sqlite://")
        self.addCleanup(engine.dispose)
        create_schema(engine)
        with sessions.begin() as db:
            db.add_all([models.Project(id=DEFAULT, slug="default", name="Default"),
                        models.Project(id="other", slug="other", name="Other")])
            db.flush()
            repository = models.RepositoryConnection(id="repo", project_id=DEFAULT, alias="backend", owner="example", name="one")
            db.add(repository)
            db.flush()
            db.add(models.RepositoryRevision(id="rev1", project_id=DEFAULT, repository_id="repo", document={"name":"one"}))
            db.flush()
            repository.revision_id = "rev1"
        with sessions.begin() as db:
            db.add(models.RepositoryRevision(id="rev2", project_id=DEFAULT, repository_id="repo", parent_id="rev1", document={"name":"two"}))
            db.flush()
            db.get(models.RepositoryConnection,"repo").revision_id="rev2"
        with sessions() as db:
            self.assertEqual(db.get(models.RepositoryRevision,"rev1").document, {"name":"one"})
            self.assertEqual(db.get(models.RepositoryConnection,"repo").revision_id,"rev2")
        with self.assertRaises(IntegrityError), sessions.begin() as db:
            db.add(models.RepositoryRevision(id="foreign",project_id="other",repository_id="repo",document={}))

    def test_repository_heads_reject_other_repository_and_unknown_revisions(self):
        engine, sessions = models.database("sqlite://")
        self.addCleanup(engine.dispose)
        create_schema(engine)
        with sessions.begin() as db:
            db.add_all([models.Project(id=DEFAULT,slug="default",name="Default"),models.Project(id="other",slug="other",name="Other")])
            db.flush()
            for identity, project in [("a",DEFAULT),("b",DEFAULT),("c","other")]:
                db.add(models.RepositoryConnection(id=identity,project_id=project,alias=identity,owner="example",name=identity))
            db.flush()
            for identity, project in [("a",DEFAULT),("b",DEFAULT),("c","other")]:
                db.add(models.RepositoryRevision(id="rev-"+identity,project_id=project,repository_id=identity,document={}))
            db.flush()
            db.get(models.RepositoryConnection,"a").revision_id="rev-a"
        for invalid in ("rev-b","rev-c","nonexistent"):
            with self.subTest(invalid=invalid), self.assertRaises(IntegrityError), sessions.begin() as db:
                db.get(models.RepositoryConnection,"a").revision_id=invalid
        with self.assertRaises(IntegrityError), sessions.begin() as db:
            db.add(models.RepositoryRevision(id="bad-parent",project_id=DEFAULT,repository_id="a",parent_id="rev-b",document={}))

    def test_event_repository_must_belong_to_its_project(self):
        engine,sessions=models.database("sqlite://")
        self.addCleanup(engine.dispose)
        create_schema(engine)
        with sessions.begin() as db:
            db.add_all([models.Project(id=DEFAULT,slug="default",name="Default"),models.Project(id="other",slug="other",name="Other")])
            db.flush()
            db.add(models.RepositoryConnection(id="repo",project_id="other",alias="repo",owner="example",name="repo"))
        with self.assertRaises(IntegrityError), sessions.begin() as db:
            db.add(models.Event(id="foreign",project_id=DEFAULT,repository_id="repo",kind="heartbeat",worker_id="worker",occurred_at=1,payload={}))

    def test_project_has_multiple_repository_aliases(self):
        engine, sessions = models.database("sqlite://")
        self.addCleanup(engine.dispose)
        create_schema(engine)
        with sessions.begin() as db:
            db.add_all([models.Project(id=DEFAULT, slug="default", name="Default"),
                        models.Project(id="other", slug="other", name="Other")])
            db.flush()
            for project in (DEFAULT,"other"):
                for alias, name in [("backend", "api"), ("frontend", "web")]:
                    db.add(models.RepositoryConnection(project_id=project, alias=alias,
                        provider="github", host="github.com", owner="example", name=name))
        with sessions() as db:
            for project in (DEFAULT,"other"):
                aliases = db.scalars(select(models.RepositoryConnection.alias).where(
                    models.RepositoryConnection.project_id == project).order_by(models.RepositoryConnection.alias)).all()
                self.assertEqual(aliases, ["backend", "frontend"])
        with self.assertRaises(IntegrityError), sessions.begin() as db:
            db.add(models.RepositoryConnection(project_id=DEFAULT, alias="backend",
                provider="github", host="github.com", owner="other", name="repo"))

    def test_old_constructor_arguments_preserve_compose_and_cookie_settings(self):
        settings = AppSettings("sqlite://", "pw", "key", ROOT, ROOT, ROOT, ROOT,
                               "http://localhost", "http://worker", "custom-compose", True)
        self.assertEqual(settings.compose_project_name,"custom-compose")
        self.assertIs(settings.cookie_secure,True)
        legacy = AppSettings("sqlite://", "pw", "key", ROOT, ROOT, ROOT, ROOT,
                             project="custom-compose")
        self.assertEqual(legacy.compose_project_name,"custom-compose")

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
        create_schema(engine)
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
            if url.startswith("postgresql"):
                admin = create_engine(url)
                schema = "migration_" + uuid.uuid4().hex
                with admin.begin() as connection:
                    connection.execute(text('CREATE SCHEMA "' + schema + '"'))
                def cleanup():
                    with admin.begin() as connection:
                        connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
                    admin.dispose()
                self.addCleanup(cleanup)
                url = make_url(url).update_query_dict({"options":"-csearch_path="+schema}).render_as_string(hide_password=False)
            cfg = Config(str(ROOT / "control-plane/backend/alembic.ini"))
            with patch.dict(os.environ, {"CONTROL_DATABASE_URL": url}):
                command.upgrade(cfg, "c82a1d6e40f3")
                engine, sessions = models.database(url)
                self.addCleanup(engine.dispose)
                document = {"defaults":{"repository":"phudekar/software-factory-test"}}
                with engine.begin() as connection:
                    connection.execute(text("INSERT INTO config_revisions (id,scope,parent_id,created_at,summary,document,digest) VALUES ('old','factory',NULL,1,'legacy',:doc,'unchanged')"), {"doc":json.dumps(document)})
                    connection.execute(text("INSERT INTO worker_instances (id,name,profile,revision_id,config_id,target_id,credential_hash,desired_state,created_at) VALUES ('worker','existing','simple','old',NULL,'local-docker','hash','running',1)"))
                    connection.execute(text("INSERT INTO worker_configurations (id,family_id,parent_id,cloned_from,name,profile,factory_revision_id,document,created_at) VALUES ('profile','family',NULL,NULL,'existing','simple','old','{}',1)"))
                    connection.execute(text("INSERT INTO conversation_content (id,ciphertext,expires_at) VALUES ('conversation','encrypted-content',9999999999)"))
                    connection.execute(text("INSERT INTO workflow_control_operations (id,key,request_digest,workflow_id,workflow_name,action,status,result,created_at,updated_at) VALUES ('control','key','digest','workflow','legacy','trigger','completed','{}',1,1)"))
                    connection.execute(text("INSERT INTO execution_events (id,kind,worker_id,occurred_at,received_at,payload) VALUES ('event','heartbeat','worker',1,1,'{}')"))
                command.upgrade(cfg, "e201f1000001")
                command.upgrade(cfg, "e201f1000001")
                with engine.connect() as connection:
                    self.assertIn("project_id", [column["name"] for column in inspect(connection).get_columns("config_revisions")])
                    revision = connection.execute(text("SELECT id,document,digest,project_id FROM config_revisions")).one()
                    self.assertEqual((revision.id, (json.loads(revision.document) if isinstance(revision.document, str) else revision.document), revision.digest, revision.project_id), ("old", document, "unchanged", DEFAULT))
                    self.assertEqual(connection.execute(text("SELECT revision_id,desired_state FROM worker_instances")).one(), ("old", "running"))
                    self.assertEqual(connection.execute(text("SELECT project_id,repository_id FROM execution_events")).one(), (DEFAULT,None))
                    self.assertEqual(connection.execute(text("SELECT COUNT(*) FROM repository_connections")).scalar(), 0)
                    self.assertEqual(connection.execute(text("SELECT id,family_id,factory_revision_id,project_id FROM worker_configurations")).one(), ("profile","family","old",DEFAULT))
                    self.assertEqual(connection.execute(text("SELECT id,ciphertext,project_id FROM conversation_content")).one(), ("conversation","encrypted-content",DEFAULT))
                    self.assertEqual(connection.execute(text("SELECT id,request_digest,status,project_id FROM workflow_control_operations")).one(), ("control","digest","completed",DEFAULT))
                # Exercise the migrated constraints too; metadata.create_all is
                # not evidence that Alembic installed the same ownership rules.
                with sessions.begin() as db:
                    db.add(models.Project(id="other",slug="other",name="Other"))
                    db.flush()
                    db.add(models.RepositoryConnection(id="a",project_id=DEFAULT,alias="a",owner="example",name="a"))
                    db.add(models.RepositoryConnection(id="b",project_id="other",alias="b",owner="example",name="b"))
                    db.flush()
                    db.add(models.RepositoryRevision(id="rev-a",project_id=DEFAULT,repository_id="a",document={}))
                    db.add(models.RepositoryRevision(id="rev-b",project_id="other",repository_id="b",document={}))
                    db.flush()
                    db.get(models.RepositoryConnection,"a").revision_id="rev-a"
                with self.assertRaises(IntegrityError), sessions.begin() as db:
                    db.get(models.RepositoryConnection,"a").revision_id="rev-b"
                # This deliberately exercises revision 1, not today's ORM shape.
                historical_events = Table("execution_events", MetaData(), autoload_with=engine)
                with self.assertRaises(IntegrityError), sessions.begin() as db:
                    db.execute(historical_events.insert().values(
                        id="foreign", project_id=DEFAULT, repository_id="b",
                        kind="heartbeat", worker_id="worker", occurred_at=1,
                        received_at=1, payload={},
                    ))

                command.upgrade(cfg,"head")
                with sessions.begin() as db:
                    db.add(models.WorkflowControlOperation(project_id="other",key="key",request_digest="other-digest",workflow_id="workflow",workflow_name="other",action="trigger",status="completed"))
                with sessions() as db:
                    old = db.get(models.WorkflowControlOperation,"control")
                    self.assertEqual((old.key,old.request_digest,old.status),("key","digest","completed"))
                    self.assertEqual(len(db.scalars(select(models.WorkflowControlOperation).where(models.WorkflowControlOperation.key=="key")).all()),2)

    def test_migrations_release_pooled_database_connections(self):
        closed=[]
        def tracked_engine(*args, **kwargs):
            engine=create_engine(*args, **kwargs)
            event.listen(engine,"close",lambda *_:closed.append(True))
            return engine
        with tempfile.TemporaryDirectory() as tmp:
            cfg=Config(str(ROOT / "control-plane/backend/alembic.ini"))
            with patch.dict(os.environ,{"CONTROL_DATABASE_URL":"sqlite:///"+tmp+"/connections.db"}), patch("sqlalchemy.create_engine",side_effect=tracked_engine):
                command.upgrade(cfg,"head")
            self.assertTrue(closed,"Alembic left its DBAPI connection pooled after completing")

    def test_fresh_install_creates_default_project_and_revision_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(str(ROOT / "control-plane/backend/alembic.ini"))
            url="sqlite:///"+tmp+"/fresh.db"
            with patch.dict(os.environ, {"CONTROL_DATABASE_URL":url}):
                command.upgrade(cfg,"head")
            engine,_=models.database(url)
            self.addCleanup(engine.dispose)
            with engine.connect() as connection:
                self.assertEqual(connection.execute(text("SELECT id,slug FROM projects")).one(),(DEFAULT,"default"))
                self.assertIn("repository_revisions",inspect(connection).get_table_names())
