"""Project-owned publication, immutable revisions and launch pinning."""

import copy
import json
import unittest
import test_api as fixtures
from app import database as models
from app.projects import ProjectScope


class WorkflowDefinitionTests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown

    def setup_graph(self, slug="alpha"):
        project = self.client.post(
            "/api/v1/projects", json={"slug": slug, "name": slug}
        ).json()["id"]
        base = f"/api/v1/projects/{project}"
        repos = {
            alias: self.client.post(
                base + "/repositories",
                json={
                    "alias": alias,
                    "owner": slug,
                    "name": alias,
                    "overrides": {"openai_model": "model-before"},
                },
            ).json()
            for alias in ("backend", "frontend")
        }
        pool = self.client.post(
            "/api/v1/worker-pools",
            json={
                "name": slug + "-pool",
                "role": "simple",
                "executors": ["builtin@1", "docker@1"],
                "actions": ["core.echo@1", "issue.load@1", "docker.run@1"],
            },
        ).json()
        self.client.put(base + "/worker-pools/" + pool["id"], json={"enabled": True})
        document = {
            "schema_version": 1,
            "name": "check",
            "repositories": ["backend", "frontend"],
            "default_repository": "backend",
            "stages": [
                {
                    "id": "echo",
                    "executor": "builtin",
                    "action": "core.echo@1",
                    "role": "simple",
                    "pool_id": pool["id"],
                    "inputs": {"message": "original"},
                },
                {
                    "id": "check",
                    "executor": "docker",
                    "role": "simple",
                    "pool_id": pool["id"],
                    "repository": "frontend",
                    "after": ["echo"],
                    "image": "example/check@sha256:" + "a" * 64,
                    "command": ["python", "/app/check.py"],
                    "inputs": {"upstream": {"$ref": "stages.echo.output"}},
                },
            ],
        }
        return project, base, repos, pool, document

    def publish(self, base, document):
        response = self.client.post(
            base + "/workflow-definitions", json={"document": document}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_publish_is_encrypted_versioned_scoped_and_conflict_checked(self):
        project, base, repos, pool, document = self.setup_graph()
        first = self.publish(base, document)
        other, otherbase, _, _, otherdoc = self.setup_graph("beta")
        foreign = self.publish(otherbase, otherdoc)
        path = base + "/workflow-definitions/" + first["id"]
        changed = copy.deepcopy(document)
        changed["stages"][0]["inputs"]["message"] = "new-value"
        updated = self.client.put(
            path, json={"parent_id": first["revision_id"], "document": changed}
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertNotEqual(first["revision_id"], updated.json()["revision_id"])
        self.assertEqual(
            self.client.put(
                path, json={"parent_id": first["revision_id"], "document": document}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.put(
                path, json={"parent_id": foreign["revision_id"], "document": document}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                otherbase + "/workflow-definitions/" + first["id"]
            ).status_code,
            404,
        )
        revision_path = base + "/workflow-definition-revisions/" + first["revision_id"]
        self.assertEqual(
            self.client.get(revision_path).json()["document"]["stages"][0]["inputs"][
                "message"
            ],
            "original",
        )
        self.assertEqual(
            self.client.get(
                otherbase + "/workflow-definition-revisions/" + first["revision_id"]
            ).status_code,
            404,
        )
        with self.sessions() as db:
            revision = db.get(models.WorkflowRevision, first["revision_id"])
            self.assertNotIn("original", revision.graph_ciphertext)
        listing = self.client.get(base + "/workflow-definitions").text
        self.assertNotIn("original", listing)
        self.assertNotIn("graph_ciphertext", listing)

    def test_validation_rejects_unknown_alias_unauthorized_pool_foreign_secret_and_bridge(
        self,
    ):
        project, base, repos, pool, document = self.setup_graph()
        other, otherbase, _, otherpool, _ = self.setup_graph("beta")
        foreign_key = self.client.post(
            otherbase + "/secrets",
            json={
                "name": "model",
                "purpose": "openai",
                "value": "foreign-secret-value",
            },
        ).json()["id"]
        cases = []
        alias = copy.deepcopy(document)
        alias["repositories"].append("missing")
        cases.append(alias)
        badpool = copy.deepcopy(document)
        badpool["stages"][0]["pool_id"] = otherpool["id"]
        cases.append(badpool)
        secret = copy.deepcopy(document)
        secret["stages"][0]["secrets"] = {"openai": foreign_key}
        cases.append(secret)
        bridge = copy.deepcopy(document)
        bridge["stages"][1]["network"] = "bridge"
        cases.append(bridge)
        for invalid in cases:
            response = self.client.post(
                base + "/workflow-definitions/validate", json={"document": invalid}
            )
            self.assertIn(response.status_code, (404, 409, 422), response.text)
            self.assertNotIn("foreign-secret-value", response.text)
        valid = self.client.post(
            base + "/workflow-definitions/validate", json={"document": document}
        )
        self.assertEqual(valid.status_code, 200, valid.text)
        self.assertEqual(self.client.get(base + "/workflow-definitions").json(), [])

    def test_launch_pin_retains_definition_repository_config_and_secret_versions(self):
        from app.workflow_definitions import pin_definition, read_definition_pin

        project, base, repos, pool, document = self.setup_graph()
        key = self.client.post(
            base + "/secrets",
            json={"name": "model", "purpose": "openai", "value": "pinned-provider-key"},
        ).json()["id"]
        document["stages"][0]["secrets"] = {"openai": key}
        published = self.publish(base, document)
        with self.sessions.begin() as db:
            pin = pin_definition(
                db,
                ProjectScope(project),
                published["revision_id"],
                fixtures.Configurations(self.settings.root),
                self.settings,
            )
            first = read_definition_pin(
                db, ProjectScope(project), pin.id, self.settings
            )
        self.client.put(
            base + "/repositories/" + repos["backend"]["id"],
            json={
                "alias": "backend",
                "owner": "changed",
                "name": "repo",
                "parent_id": repos["backend"]["revision_id"],
                "overrides": {"openai_model": "model-after"},
            },
        )
        self.client.post(
            base + "/secrets",
            json={
                "name": "model",
                "purpose": "openai",
                "value": "rotated-provider-key",
                "parent_id": key,
            },
        )
        changed = copy.deepcopy(document)
        changed["stages"][0]["inputs"]["message"] = "later-definition"
        self.client.put(
            base + "/workflow-definitions/" + published["id"],
            json={"parent_id": published["revision_id"], "document": changed},
        )
        with self.sessions() as db:
            pinned = read_definition_pin(
                db, ProjectScope(project), pin.id, self.settings
            )
            self.assertEqual(first, pinned)
            self.assertEqual(pinned["workflow_revision_id"], published["revision_id"])
            self.assertEqual(
                pinned["repositories"]["backend"]["revision_id"],
                repos["backend"]["revision_id"],
            )
            settings = json.loads(pinned["stages"]["echo"]["settings_json"])
            self.assertEqual(settings["repository"], "alpha/backend")
            self.assertEqual(settings["openai_model"], "model-before")
            self.assertEqual(
                pinned["stages"]["echo"]["secret_versions"], {"openai": key}
            )
            self.assertNotIn("pinned-provider-key", json.dumps(pinned))
            self.assertNotIn(
                "original", db.get(models.WorkflowLaunchSnapshot, pin.id).ciphertext
            )

    @unittest.skipUnless(
        __import__("os")
        .environ.get("CONTROL_TEST_DATABASE_URL", "")
        .startswith("postgresql"),
        "PostgreSQL publish locking",
    )
    def test_concurrent_publishes_have_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor

        _, base, _, _, document = self.setup_graph()
        published = self.publish(base, document)

        def update(value):
            changed = copy.deepcopy(document)
            changed["stages"][0]["inputs"]["message"] = value
            return self.client.put(
                base + "/workflow-definitions/" + published["id"],
                json={"parent_id": published["revision_id"], "document": changed},
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as threads:
            codes = list(threads.map(update, ["one", "two"]))
        self.assertEqual(sorted(codes), [200, 409])

    def test_migrated_constraints_and_populated_rollback_preserve_existing_projects(
        self,
    ):
        import os
        import tempfile
        import uuid
        from unittest.mock import patch
        from sqlalchemy import create_engine, text, inspect
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import IntegrityError
        from alembic.config import Config
        from alembic import command

        with tempfile.TemporaryDirectory() as tmp:
            url = os.environ.get(
                "CONTROL_MIGRATION_TEST_URL", "sqlite:///" + tmp + "/definitions.db"
            )
            admin = None
            if url.startswith("postgresql"):
                admin = create_engine(url)
                schema = "definition_" + uuid.uuid4().hex
                with admin.begin() as connection:
                    connection.execute(text('CREATE SCHEMA "' + schema + '"'))
                url = (
                    make_url(url)
                    .update_query_dict({"options": "-csearch_path=" + schema})
                    .render_as_string(hide_password=False)
                )
            engine, sessions = models.database(url)
            try:
                cfg = Config(str(fixtures.ROOT / "control-plane/backend/alembic.ini"))
                with patch.dict(os.environ, {"CONTROL_DATABASE_URL": url}):
                    command.upgrade(cfg, "head")
                    with sessions.begin() as db:
                        db.add_all(
                            [
                                models.Project(id="alpha", slug="alpha", name="Alpha"),
                                models.Project(id="beta", slug="beta", name="Beta"),
                            ]
                        )
                        db.flush()
                        db.add_all(
                            [
                                models.WorkflowDefinitionRecord(
                                    id="a", project_id="alpha", name="a"
                                ),
                                models.WorkflowDefinitionRecord(
                                    id="b", project_id="beta", name="b"
                                ),
                            ]
                        )
                        db.flush()
                        db.add_all(
                            [
                                models.WorkflowRevision(
                                    id="a1",
                                    project_id="alpha",
                                    definition_id="a",
                                    digest="a" * 64,
                                    graph_ciphertext="encrypted",
                                ),
                                models.WorkflowRevision(
                                    id="b1",
                                    project_id="beta",
                                    definition_id="b",
                                    digest="b" * 64,
                                    graph_ciphertext="encrypted",
                                ),
                            ]
                        )
                        db.flush()
                        db.add(
                            models.WorkflowRevision(
                                id="a2",
                                project_id="alpha",
                                definition_id="a",
                                parent_id="a1",
                                digest="a" * 64,
                                graph_ciphertext="encrypted",
                            )
                        )
                        db.flush()
                        db.get(models.WorkflowDefinitionRecord, "a").revision_id = "a2"
                        db.get(models.WorkflowDefinitionRecord, "b").revision_id = "b1"
                        db.add(
                            models.WorkflowLaunchSnapshot(
                                project_id="alpha",
                                workflow_revision_id="a1",
                                ciphertext="encrypted",
                            )
                        )
                    with self.assertRaises(IntegrityError), sessions.begin() as db:
                        db.get(models.WorkflowDefinitionRecord, "a").revision_id = "b1"
                    with self.assertRaises(IntegrityError), sessions.begin() as db:
                        db.add(
                            models.WorkflowRevision(
                                project_id="alpha",
                                definition_id="a",
                                parent_id="b1",
                                digest="a" * 64,
                                graph_ciphertext="encrypted",
                            )
                        )
                    with self.assertRaises(IntegrityError), sessions.begin() as db:
                        db.add(
                            models.WorkflowLaunchSnapshot(
                                project_id="beta",
                                workflow_revision_id="a1",
                                ciphertext="encrypted",
                            )
                        )
                    command.downgrade(cfg, "e201f1000004")
                    self.assertNotIn(
                        "workflow_definitions", inspect(engine).get_table_names()
                    )
                    with sessions() as db:
                        self.assertEqual(db.get(models.Project, "alpha").name, "Alpha")
                    command.upgrade(cfg, "head")
                    with sessions() as db:
                        self.assertEqual(
                            db.query(models.WorkflowDefinitionRecord).count(), 0
                        )
            finally:
                engine.dispose()
                if admin:
                    with admin.begin() as connection:
                        connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
                    admin.dispose()

    def test_scoped_catalog_exposes_versioned_schemas_and_unavailable_future_executors(
        self,
    ):
        project, base, *_ = self.setup_graph()
        response = self.client.get(base + "/workflow-catalog")
        self.assertEqual(response.status_code, 200, response.text)
        catalog = response.json()
        actions = {a["id"]: a for a in catalog["actions"]}
        self.assertIn("command", actions["issue.load@1"]["input_schema"]["properties"])
        self.assertIn("schema_version", catalog["definition_schema"]["properties"])
        self.assertEqual(
            {e["id"] for e in catalog["executors"] if e["available"]},
            {"builtin", "docker"},
        )
        self.assertFalse(
            next(e for e in catalog["executors"] if e["id"] == "kubernetes")[
                "available"
            ]
        )
        self.assertEqual(
            self.client.get("/api/v1/projects/missing/workflow-catalog").status_code,
            404,
        )


class SQLitePublicationRaceTests(unittest.TestCase):
    setup_graph = WorkflowDefinitionTests.setup_graph
    publish = WorkflowDefinitionTests.publish

    def setUp(self):
        import os
        import tempfile
        from unittest.mock import patch

        self.sqlite_dir = tempfile.TemporaryDirectory()
        with patch.dict(
            os.environ,
            {
                "CONTROL_TEST_DATABASE_URL": "sqlite:///"
                + self.sqlite_dir.name
                + "/race.db"
            },
        ):
            fixtures.ControlPlaneTests.setUp(self)

    def tearDown(self):
        fixtures.ControlPlaneTests.tearDown(self)
        self.sqlite_dir.cleanup()

    def test_same_parent_concurrent_edits_have_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from unittest.mock import patch
        from sqlalchemy import select
        from app import workflow_definitions as service

        _, base, _, _, document = self.setup_graph()
        published = self.publish(base, document)
        ready = Barrier(2)
        prepare = service.prepare_definition

        def synchronized(*args, **kwargs):
            result = prepare(*args, **kwargs)
            ready.wait(timeout=10)
            return result

        def update(message):
            changed = copy.deepcopy(document)
            changed["stages"][0]["inputs"]["message"] = message
            return self.client.put(
                base + "/workflow-definitions/" + published["id"],
                json={"parent_id": published["revision_id"], "document": changed},
            ).status_code

        with patch.object(service, "prepare_definition", synchronized):
            with ThreadPoolExecutor(max_workers=2) as threads:
                codes = list(threads.map(update, ["one", "two"]))
        self.assertEqual(sorted(codes), [200, 409])
        with self.sessions() as db:
            revisions = list(
                db.scalars(
                    select(models.WorkflowRevision).where(
                        models.WorkflowRevision.definition_id == published["id"]
                    )
                )
            )
            self.assertEqual(len(revisions), 2)
