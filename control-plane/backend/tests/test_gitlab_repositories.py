"""Project-owned GitLab identity resolves into immutable revisions before execution."""

import unittest
from unittest.mock import patch
import test_api as fixtures
from app import database as models
from factory.domain.repositories import RepositoryIdentity


class GitLabRepositoriesTests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown

    def project(self, slug="gitlab-project"):
        result = self.client.post("/api/v1/projects", json={"slug": slug, "name": slug})
        self.assertEqual(result.status_code, 201, result.text)
        return "/api/v1/projects/" + result.json()["id"]

    def create(self, base):
        body = {
            "alias": "backend",
            "provider": "gitlab",
            "host": "gitlab.com",
            "owner": "group/sub",
            "name": "repo",
        }
        response = self.client.post(base + "/repositories", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json(), body

    def credential(self, base, repository, purpose="gitlab"):
        response = self.client.post(
            base + "/secrets",
            json={
                "name": "source-token",
                "purpose": purpose,
                "repository_id": repository["id"],
                "value": "fixture-private-token",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def prepare(self):
        base = self.project()
        repo, body = self.create(base)
        body["credential_id"] = self.credential(base, repo)
        response = self.client.put(
            base + "/repositories/" + repo["id"],
            json={**body, "parent_id": repo["revision_id"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return base, response.json(), body

    def remote(self, owner="group/sub", name="repo", project_id=123):
        return RepositoryIdentity(
            provider="gitlab",
            host="gitlab.com",
            owner=owner,
            name=name,
            project_id=project_id,
        )

    def test_unresolved_connection_is_owned_but_not_executable(self):
        base = self.project()
        repo, _ = self.create(base)
        self.assertFalse(repo["validated"])
        self.assertIsNone(repo["identity"])
        response = self.client.post(
            base + "/config/effective", json={"repository": "backend"}
        )
        self.assertEqual(response.status_code, 409, response.text)
        with self.sessions() as db:
            row = db.get(models.RepositoryConnection, repo["id"])
            self.assertEqual(row.project_id, base.rsplit("/", 1)[-1])

    def test_validate_appends_owned_identity_revision_and_preserves_parent(self):
        base, repo, _ = self.prepare()
        parent = repo["revision_id"]
        with patch(
            "app.repository_validation.resolve_gitlab_project",
            return_value=self.remote(),
        ) as resolve:
            response = self.client.post(
                base + "/repositories/" + repo["id"] + "/validate",
                json={"parent_id": parent},
            )
        self.assertEqual(response.status_code, 200, response.text)
        validated = response.json()
        self.assertNotEqual(validated["revision_id"], parent)
        self.assertEqual(validated["identity"]["project_id"], 123)
        self.assertTrue(validated["validated"])
        self.assertNotIn("fixture-private-token", response.text)
        with self.sessions() as db:
            previous = db.get(models.RepositoryRevision, parent)
            self.assertNotIn("identity", previous.document)
            current = db.get(models.RepositoryRevision, validated["revision_id"])
            self.assertEqual(current.parent_id, parent)
            self.assertEqual(current.project_id, repo["project_id"])
        resolve.assert_called_once_with("group/sub", "repo", "fixture-private-token")
        preview = self.client.post(
            base + "/config/effective", json={"repository": "backend"}
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["settings"]["repository_provider"], "gitlab")
        self.assertEqual(preview.json()["settings"]["gitlab_project_id"], 123)

    def test_stale_and_foreign_validation_does_not_resolve_secret(self):
        base, repo, _ = self.prepare()
        other = self.project("other")
        with patch(
            "app.repository_validation.resolve_gitlab_project",
            return_value=self.remote(),
        ) as resolve:
            path = base + "/repositories/" + repo["id"] + "/validate"
            first = self.client.post(path, json={"parent_id": repo["revision_id"]})
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(
                self.client.post(
                    path, json={"parent_id": repo["revision_id"]}
                ).status_code,
                409,
            )
            self.assertEqual(
                self.client.post(
                    other + "/repositories/" + repo["id"] + "/validate",
                    json={"parent_id": repo["revision_id"]},
                ).status_code,
                404,
            )
        self.assertEqual(resolve.call_count, 1)

    def test_reused_path_cannot_switch_remote_numeric_identity(self):
        base, repo, body = self.prepare()
        path = base + "/repositories/" + repo["id"]
        with patch(
            "app.repository_validation.resolve_gitlab_project",
            return_value=self.remote(),
        ):
            validated = self.client.post(
                path + "/validate", json={"parent_id": repo["revision_id"]}
            ).json()
        edited = self.client.put(
            path,
            json={**body, "owner": "new/group", "parent_id": validated["revision_id"]},
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertFalse(edited.json()["validated"])
        with patch(
            "app.repository_validation.resolve_gitlab_project",
            return_value=self.remote(owner="new/group", project_id=999),
        ):
            rejected = self.client.post(
                path + "/validate", json={"parent_id": edited.json()["revision_id"]}
            )
        self.assertEqual(rejected.status_code, 409, rejected.text)
        with patch(
            "app.repository_validation.resolve_gitlab_project",
            return_value=self.remote(owner="new/group"),
        ):
            accepted = self.client.post(
                path + "/validate", json={"parent_id": edited.json()["revision_id"]}
            )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["identity"]["project_id"], 123)

    def test_gitlab_requires_its_own_credential_purpose(self):
        base = self.project()
        repo, body = self.create(base)
        github = self.credential(base, repo, purpose="github")
        response = self.client.put(
            base + "/repositories/" + repo["id"],
            json={**body, "credential_id": github, "parent_id": repo["revision_id"]},
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_unresolved_behavior_values_still_validate(self):
        base = self.project()
        body = {
            "alias": "backend",
            "provider": "gitlab",
            "host": "gitlab.com",
            "owner": "group/sub",
            "name": "repo",
            "overrides": {"api_timeout_seconds": -1},
        }
        rejected = self.client.post(base + "/repositories", json=body)
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertEqual(self.client.get(base + "/repositories").json(), [])

    def test_old_github_worker_snapshot_omits_new_provider_defaults(self):
        from app.services import Configurations

        with self.sessions() as db:
            document = db.get(models.Revision, self.revision["id"]).document
        snapshot = Configurations(fixtures.ROOT).snapshot(
            document, "simple", "worker", "revision"
        )
        for key in (
            "repository_provider",
            "repository_host",
            "gitlab_project_id",
            "repository_check_policy_json",
        ):
            self.assertNotIn(key, snapshot["defaults"])

    def test_validation_rechecks_parent_after_remote_read(self):
        base, repo, _ = self.prepare()
        with self.sessions() as db:
            original = dict(
                db.get(models.RepositoryRevision, repo["revision_id"]).document
            )

        def change_head(*args):
            with self.sessions.begin() as db:
                revision = models.RepositoryRevision(
                    project_id=repo["project_id"],
                    repository_id=repo["id"],
                    parent_id=repo["revision_id"],
                    document=original,
                )
                db.add(revision)
                db.flush()
                db.get(
                    models.RepositoryConnection, repo["id"]
                ).revision_id = revision.id
            return self.remote()

        with patch(
            "app.repository_validation.resolve_gitlab_project", side_effect=change_head
        ):
            response = self.client.post(
                base + "/repositories/" + repo["id"] + "/validate",
                json={"parent_id": repo["revision_id"]},
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(
            self.client.get(base + "/repositories/" + repo["id"]).json()["validated"]
        )

    def test_validation_rechecks_grant_revocation_after_remote_read(self):
        import time

        base, repo, body = self.prepare()

        def revoke(*args):
            with self.sessions.begin() as db:
                db.get(
                    models.SecretVersion, body["credential_id"]
                ).revoked_at = time.time()
            return self.remote()

        with patch(
            "app.repository_validation.resolve_gitlab_project", side_effect=revoke
        ):
            response = self.client.post(
                base + "/repositories/" + repo["id"] + "/validate",
                json={"parent_id": repo["revision_id"]},
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(
            self.client.get(base + "/repositories/" + repo["id"]).json()["validated"]
        )

    def test_nested_namespace_storage_exceeds_old_owner_column_limit(self):
        self.assertEqual(
            models.RepositoryConnection.__table__.c.owner.type.length, 1024
        )
        base = self.project()
        response = self.client.post(
            base + "/repositories",
            json={
                "alias": "long",
                "provider": "gitlab",
                "host": "gitlab.com",
                "owner": "group/" + "x" * 100,
                "name": "repo",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)

    def test_pinned_gitlab_identity_and_credential_survive_connection_edits(self):
        import json
        from app.project_context import pin_context, resolve_pinned_context
        from app.projects import ProjectScope

        base, repo, body = self.prepare()
        scope = ProjectScope(repo["project_id"])
        path = base + "/repositories/" + repo["id"]
        with patch(
            "app.repository_validation.resolve_gitlab_project",
            return_value=self.remote(),
        ):
            validated = self.client.post(
                path + "/validate", json={"parent_id": repo["revision_id"]}
            ).json()
        with self.sessions.begin() as db:
            pin = pin_context(
                db,
                scope,
                fixtures.Configurations(self.settings.root),
                self.settings,
                repository="backend",
                workflow_revision_id="wf",
                run_id="run",
                stage_id="load",
                execution_id="load-v1",
                attempt=1,
                assignment_id="assignment",
                purposes=["gitlab"],
            )
            identifier = pin.id
        rotated = self.client.post(
            base + "/secrets",
            json={
                "name": "source-token",
                "purpose": "gitlab",
                "repository_id": repo["id"],
                "value": "rotated-private-token",
                "parent_id": body["credential_id"],
            },
        )
        self.assertEqual(rotated.status_code, 201, rotated.text)
        edit = self.client.put(
            path,
            json={
                **body,
                "credential_id": rotated.json()["id"],
                "owner": "new/group",
                "parent_id": validated["revision_id"],
            },
        )
        self.assertEqual(edit.status_code, 200, edit.text)
        with self.sessions() as db:
            context = resolve_pinned_context(
                db, scope, identifier, self.settings, assignment_id="assignment"
            )
            self.assertEqual(context.repository_identity.project_id, 123)
            self.assertEqual(context.settings.repository, "group/sub/repo")
            self.assertEqual(
                dict(context.credentials), {"gitlab": "fixture-private-token"}
            )
            self.assertNotIn("fixture-private-token", context.model_dump_json())
            self.assertEqual(
                json.loads(context.settings_json)["repository_provider"], "gitlab"
            )

    def test_old_github_pinned_settings_wire_keeps_original_fields(self):
        import json
        from app.project_context import pin_context, decrypt
        from app.projects import ProjectScope

        base = self.project()
        project = base.rsplit("/", 1)[-1]
        token = self.client.post(
            base + "/secrets",
            json={"name": "github-token", "purpose": "github", "value": "fixture-gh"},
        ).json()["id"]
        self.client.post(
            base + "/repositories",
            json={
                "alias": "legacy",
                "owner": "owner",
                "name": "repo",
                "credential_id": token,
            },
        )
        with self.sessions.begin() as db:
            pin = pin_context(
                db,
                ProjectScope(project),
                fixtures.Configurations(self.settings.root),
                self.settings,
                repository="legacy",
                workflow_revision_id="wf",
                run_id="run",
                stage_id="load",
                execution_id="legacy-load",
                attempt=1,
                assignment_id="assignment",
                purposes=["github"],
            )
            context = decrypt(pin.ciphertext, self.settings)["context"]
            values = json.loads(context["settings_json"])
        self.assertNotIn("repository_identity_json", context)
        for key in (
            "repository_provider",
            "repository_host",
            "gitlab_project_id",
            "repository_check_policy_json",
        ):
            self.assertNotIn(key, values)

    def test_gitlab_run_waits_for_provider_capability_and_pins_context(self):
        from app.workflow_state import WorkflowState
        from app.projects import ProjectScope
        from app.reconciler import worker_token
        from app.project_context import decrypt
        from sqlalchemy import select

        base, repo, _ = self.prepare()
        with patch(
            "app.repository_validation.resolve_gitlab_project",
            return_value=self.remote(),
        ):
            validated = self.client.post(
                base + "/repositories/" + repo["id"] + "/validate",
                json={"parent_id": repo["revision_id"]},
            ).json()
        pool = self.client.post(
            "/api/v1/worker-pools",
            json={
                "name": "gitlab-pool",
                "role": "simple",
                "executors": ["builtin@1"],
                "actions": ["core.echo@1"],
            },
        ).json()
        self.client.put(base + "/worker-pools/" + pool["id"], json={"enabled": True})
        graph = {
            "schema_version": 1,
            "name": "gitlab-check",
            "repositories": ["backend"],
            "default_repository": "backend",
            "stages": [
                {
                    "id": "echo",
                    "executor": "builtin",
                    "action": "core.echo@1",
                    "role": "simple",
                    "pool_id": pool["id"],
                    "inputs": {"message": "scoped"},
                }
            ],
        }
        published = self.client.post(
            base + "/workflow-definitions", json={"document": graph}
        )
        self.assertEqual(published.status_code, 201, published.text)
        worker = self.client.post(
            "/api/v1/worker-pools/" + pool["id"] + "/workers",
            json={"name": "old-provider-worker"},
        ).json()
        headers = {
            "X-Worker-Id": worker["id"],
            "Authorization": "Bearer " + worker_token(self.settings, worker["id"]),
        }
        registration = {
            "protocol": "shared-v1",
            "pool_id": pool["id"],
            "role": "simple",
            "executors": ["builtin@1"],
            "actions": ["core.echo@1"],
            "normal_slots": 2,
            "durable_slots": 16,
        }
        registered = self.client.post(
            "/api/v1/shared-worker/register", headers=headers, json=registration
        )
        self.assertEqual(registered.status_code, 200, registered.text)
        store = WorkflowState(
            self.sessions,
            self.settings,
            fixtures.Configurations(self.settings.root),
            clock=lambda: 1000,
        )
        scope = ProjectScope(repo["project_id"])
        run = store.start_run(
            scope, published.json()["revision_id"], {}, key="gitlab-one"
        )
        store.advance_run(scope, run.id)
        with self.sessions() as db:
            self.assertEqual(
                list(
                    db.scalars(
                        select(models.Assignment).where(
                            models.Assignment.run_id == run.id
                        )
                    )
                ),
                [],
            )
        registered = self.client.post(
            "/api/v1/shared-worker/register",
            headers=headers,
            json={**registration, "providers": ["github", "gitlab"]},
        )
        self.assertEqual(registered.status_code, 200, registered.text)
        store.advance_run(scope, run.id)
        with self.sessions() as db:
            assignment = db.scalar(
                select(models.Assignment).where(models.Assignment.run_id == run.id)
            )
            self.assertIsNotNone(assignment)
            snapshot = db.scalar(
                select(models.ActionContextSnapshot).where(
                    models.ActionContextSnapshot.execution_id == assignment.execution_id
                )
            )
            data = decrypt(snapshot.ciphertext, self.settings)
            from factory.workflows.contracts import ActionContext

            context = ActionContext.model_validate_json(
                __import__("json").dumps(data["context"])
            )
            self.assertEqual(context.repository_identity.project_id, 123)
            self.assertEqual(context.repository_revision_id, validated["revision_id"])
            self.assertEqual(data["grants"], {})
            identifier = assignment.execution_id
        store.claim_resource(scope, identifier, "gitlab.com/project/123/issue/7")
        store.claim_resource(
            scope, identifier, "gitlab.com/project/123/merge-request/7"
        )
        with self.sessions() as db:
            from sqlalchemy import func

            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(models.ExternalResourceClaim)
                    .where(models.ExternalResourceClaim.execution_id == identifier)
                ),
                2,
            )
        # A re-registration that drops provider support cannot claim already
        # admitted work, even though action/executor versions still match.
        self.client.post(
            "/api/v1/shared-worker/register", headers=headers, json=registration
        )
        rejected = self.client.post(
            "/api/v1/shared-worker/claim",
            headers=headers,
            json={
                "project_id": scope.project_id,
                "run_id": run.id,
                "execution_id": identifier,
                "claim_key": "stable-provider-test-key",
            },
        )
        self.assertEqual(rejected.status_code, 409, rejected.text)

    def test_explicit_docker_gitlab_grant_is_supported_and_private(self):
        from app.job_contracts import JobLaunch

        job = JobLaunch(
            job_id="a" * 64,
            project_id="p",
            execution_id="execution",
            spec_digest="b" * 64,
            image="example/check@sha256:" + "c" * 64,
            command=["python", "check.py"],
            inputs={},
            credentials={"gitlab": "private-docker-source"},
        )
        self.assertEqual(job.credentials, {"gitlab": "private-docker-source"})
        self.assertNotIn("private-docker-source", job.model_dump_json())

    def test_postgres_namespace_migration_preserves_legacy_and_refuses_truncation(self):
        import os
        import uuid
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import create_engine, text, inspect
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import DataError

        url = os.environ.get("CONTROL_MIGRATION_TEST_URL", "")
        if not url.startswith("postgresql"):
            self.skipTest("requires dedicated PostgreSQL migration database")
        schema = "gitlab_namespace_" + uuid.uuid4().hex
        admin = create_engine(url)
        with admin.begin() as db:
            db.execute(text('CREATE SCHEMA "' + schema + '"'))
        scoped = (
            make_url(url)
            .update_query_dict({"options": "-csearch_path=" + schema})
            .render_as_string(hide_password=False)
        )
        engine = create_engine(scoped)
        config = Config(str(fixtures.ROOT / "control-plane/backend/alembic.ini"))
        try:
            with patch.dict(os.environ, {"CONTROL_DATABASE_URL": scoped}):
                command.upgrade(config, "e201f1000009")
                self.assertEqual(
                    next(
                        column["type"].length
                        for column in inspect(engine).get_columns(
                            "repository_connections"
                        )
                        if column["name"] == "owner"
                    ),
                    100,
                )

                def insert(identifier, owner, provider="github"):
                    with engine.begin() as db:
                        db.execute(
                            models.RepositoryConnection.__table__.insert().values(
                                id=identifier,
                                project_id=models.DEFAULT_PROJECT_ID,
                                alias=identifier,
                                provider=provider,
                                host=provider + ".com",
                                owner=owner,
                                name="repo",
                                overrides={},
                            )
                        )

                insert("legacy", "old-owner")
                long_owner = "group/" + "x" * 100
                with self.assertRaises(DataError):
                    insert("new-gitlab", long_owner, "gitlab")
                command.upgrade(config, "head")
                insert("new-gitlab", long_owner, "gitlab")
                with self.assertRaisesRegex(RuntimeError, "no data will be truncated"):
                    command.downgrade(config, "e201f1000009")
                with engine.begin() as db:
                    self.assertEqual(
                        db.execute(
                            text(
                                "SELECT owner FROM repository_connections WHERE id='new-gitlab'"
                            )
                        ).scalar_one(),
                        long_owner,
                    )
                    self.assertEqual(
                        db.execute(
                            text(
                                "SELECT owner FROM repository_connections WHERE id='legacy'"
                            )
                        ).scalar_one(),
                        "old-owner",
                    )
                    db.execute(
                        text("DELETE FROM repository_connections WHERE id='new-gitlab'")
                    )
                command.downgrade(config, "e201f1000009")
                command.upgrade(config, "head")
        finally:
            engine.dispose()
            with admin.begin() as db:
                db.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
            admin.dispose()

    def test_gitlab_takeover_fences_matching_legacy_owner_and_pending_operations(self):
        import copy
        from app.workflow_state import WorkflowState
        from app.projects import ProjectScope
        from fastapi import HTTPException
        from sqlalchemy import select

        base, repo, _ = self.prepare()
        with patch(
            "app.repository_validation.resolve_gitlab_project",
            return_value=self.remote(),
        ):
            self.client.post(
                base + "/repositories/" + repo["id"] + "/validate",
                json={"parent_id": repo["revision_id"]},
            )
        with self.sessions() as db:
            document = copy.deepcopy(
                db.get(models.Revision, self.revision["id"]).document
            )
        document["defaults"].update(
            repository="group/sub/repo",
            repository_provider="gitlab",
            repository_host="gitlab.com",
            gitlab_project_id=123,
        )
        config = fixtures.Configurations(self.settings.root)
        with self.sessions.begin() as db:
            revision = models.Revision(
                project_id=models.DEFAULT_PROJECT_ID,
                scope="factory",
                summary="Fixture GitLab legacy",
                document=document,
                digest=config.hash(document),
            )
            db.add(revision)
            db.flush()
            legacy_revision = revision.id
        worker = fixtures.ControlPlaneTests.create_worker(self, "gitlab-legacy-fixture")
        with self.sessions.begin() as db:
            record = db.get(models.Worker, worker.id)
            record.revision_id = legacy_revision
            record.desired_state = "running"
        store = WorkflowState(self.sessions, self.settings, config)
        scope = ProjectScope(repo["project_id"])
        with self.assertRaises(HTTPException) as live:
            store.enable_dynamic_repository(scope, repo["id"])
        self.assertEqual(live.exception.status_code, 409)
        with self.sessions.begin() as db:
            db.get(models.Worker, worker.id).desired_state = "stopped"
        with self.assertRaises(HTTPException):
            store.enable_dynamic_repository(scope, repo["id"])
        with self.sessions.begin() as db:
            for operation in db.scalars(
                select(models.Operation).where(models.Operation.worker_id == worker.id)
            ):
                operation.status = "completed"
        store.enable_dynamic_repository(scope, repo["id"])
        with self.sessions() as db:
            self.assertEqual(
                db.get(models.RepositoryExecutionMode, "gitlab.com/project/123").mode,
                "dynamic",
            )
            self.assertEqual(db.get(models.Worker, worker.id).desired_state, "stopped")

    def test_behavior_only_edit_keeps_verified_source_identity(self):
        base, repo, body = self.prepare()
        path = base + "/repositories/" + repo["id"]
        with patch(
            "app.repository_validation.resolve_gitlab_project",
            return_value=self.remote(),
        ):
            validated = self.client.post(
                path + "/validate", json={"parent_id": repo["revision_id"]}
            ).json()
        changed = self.client.put(
            path,
            json={
                **body,
                "parent_id": validated["revision_id"],
                "overrides": {"openai_model": "updated-model"},
            },
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertTrue(changed.json()["validated"])
        self.assertEqual(changed.json()["identity"]["project_id"], 123)
        preview = self.client.post(
            base + "/config/effective", json={"repository": "backend"}
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["settings"]["openai_model"], "updated-model")
