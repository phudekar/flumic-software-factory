import unittest
import test_api as fixtures
from app import database as models


class ProjectSecretTests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown

    def test_secrets_are_encrypted_scoped_versioned_and_revocable(self):
        a = self.client.post(
            "/api/v1/projects", json={"slug": "alpha", "name": "Alpha"}
        ).json()["id"]
        b = self.client.post(
            "/api/v1/projects", json={"slug": "beta", "name": "Beta"}
        ).json()["id"]
        path = f"/api/v1/projects/{a}/secrets"
        first = self.client.post(
            path,
            json={"name": "repo", "purpose": "github", "value": "alpha-private-token"},
        )
        self.assertEqual(first.status_code, 201, first.text)
        identifier = first.json()["id"]
        second = self.client.post(
            path,
            json={
                "name": "repo",
                "purpose": "github",
                "value": "rotated-private-token",
                "parent_id": identifier,
            },
        )
        self.assertEqual(second.status_code, 201, second.text)
        self.assertNotIn("private-token", self.client.get(path).text)
        with self.sessions() as db:
            record = db.get(models.SecretVersion, identifier)
            self.assertNotIn("alpha-private-token", record.ciphertext)
            from app.project_context import resolve_secret
            from app.projects import ProjectScope

            self.assertEqual(
                resolve_secret(
                    db, ProjectScope(a), identifier, "github", self.settings
                ),
                "alpha-private-token",
            )
            from fastapi import HTTPException

            with self.assertRaises(HTTPException):
                resolve_secret(db, ProjectScope(b), identifier, "github", self.settings)
        self.assertEqual(
            self.client.post(
                f"/api/v1/projects/{b}/secrets/{identifier}/revoke"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(path + "/" + identifier + "/revoke").status_code, 200
        )
        with self.sessions() as db:
            with self.assertRaises(HTTPException):
                resolve_secret(db, ProjectScope(a), identifier, "github", self.settings)

    def test_repository_revision_does_not_change_after_edit(self):
        a = self.client.post(
            "/api/v1/projects", json={"slug": "alpha", "name": "Alpha"}
        ).json()["id"]
        path = f"/api/v1/projects/{a}/repositories"
        first = self.client.post(
            path, json={"alias": "backend", "owner": "a", "name": "one"}
        ).json()
        self.assertIn("revision_id", first)
        updated = self.client.put(
            path + "/" + first["id"],
            json={
                "alias": "backend",
                "owner": "a",
                "name": "two",
                "parent_id": first["revision_id"],
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertNotEqual(first["revision_id"], updated.json()["revision_id"])
        with self.sessions() as db:
            self.assertEqual(
                db.get(models.RepositoryRevision, first["revision_id"]).document[
                    "name"
                ],
                "one",
            )
        stale = self.client.put(
            path + "/" + first["id"],
            json={
                "alias": "backend",
                "owner": "a",
                "name": "three",
                "parent_id": first["revision_id"],
            },
        )
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_pinned_context_keeps_settings_and_grants_after_rotation_and_edits(self):
        from app.project_context import pin_context, resolve_pinned_context
        from app.projects import ProjectScope
        from fastapi import HTTPException

        project = self.client.post(
            "/api/v1/projects", json={"slug": "alpha", "name": "Alpha"}
        ).json()["id"]
        other = self.client.post(
            "/api/v1/projects", json={"slug": "beta", "name": "Beta"}
        ).json()["id"]
        base = f"/api/v1/projects/{project}"
        key = self.client.post(
            base + "/secrets",
            json={"name": "git", "purpose": "github", "value": "pinned-private-key"},
        ).json()["id"]
        repo = self.client.post(
            base + "/repositories",
            json={
                "alias": "backend",
                "owner": "alpha",
                "name": "one",
                "credential_id": key,
            },
        ).json()
        self.assertIn("revision_id", repo)
        with self.sessions.begin() as db:
            pin = pin_context(
                db,
                ProjectScope(project),
                fixtures.Configurations(self.settings.root),
                self.settings,
                repository="backend",
                workflow_revision_id="workflow-v1",
                run_id="run",
                stage_id="build",
                execution_id="execution",
                attempt=1,
                assignment_id="assignment",
                purposes=["github"],
            )
        self.client.post(
            base + "/secrets",
            json={
                "name": "git",
                "purpose": "github",
                "value": "new-private-key",
                "parent_id": key,
            },
        )
        self.client.put(
            base + "/repositories/" + repo["id"],
            json={
                "alias": "backend",
                "owner": "alpha",
                "name": "two",
                "parent_id": repo["revision_id"],
            },
        )
        with self.sessions() as db:
            context = resolve_pinned_context(
                db,
                ProjectScope(project),
                pin.id,
                self.settings,
                assignment_id="assignment",
            )
            self.assertEqual(context.settings.repository, "alpha/one")
            self.assertEqual(context.credential("github"), "pinned-private-key")
            self.assertEqual(context.workflow_revision_id, "workflow-v1")
            self.assertNotIn("pinned-private-key", repr(context))
            self.assertNotIn("pinned-private-key", context.model_dump_json())
            self.assertNotIn(
                "alpha/one", db.get(models.ActionContextSnapshot, pin.id).ciphertext
            )
            with self.assertRaises(HTTPException):
                resolve_pinned_context(
                    db,
                    ProjectScope(other),
                    pin.id,
                    self.settings,
                    assignment_id="assignment",
                )
            with self.assertRaises(HTTPException):
                resolve_pinned_context(
                    db,
                    ProjectScope(project),
                    pin.id,
                    self.settings,
                    assignment_id="wrong",
                )
        self.client.post(base + "/secrets/" + key + "/revoke")
        with self.sessions() as db:
            with self.assertRaises(HTTPException):
                resolve_pinned_context(
                    db,
                    ProjectScope(project),
                    pin.id,
                    self.settings,
                    assignment_id="assignment",
                )

    def test_repository_secret_grant_cannot_cross_repository_or_purpose(self):
        project = self.client.post(
            "/api/v1/projects", json={"slug": "alpha", "name": "Alpha"}
        ).json()["id"]
        base = f"/api/v1/projects/{project}"
        repo = self.client.post(
            base + "/repositories", json={"alias": "one", "owner": "a", "name": "one"}
        ).json()
        key = self.client.post(
            base + "/secrets",
            json={
                "name": "git",
                "purpose": "github",
                "value": "private-repo-key",
                "repository_id": repo["id"],
            },
        )
        self.assertEqual(key.status_code, 201, key.text)
        response = self.client.post(
            base + "/repositories",
            json={
                "alias": "two",
                "owner": "a",
                "name": "two",
                "credential_id": key.json()["id"],
            },
        )
        self.assertEqual(response.status_code, 404, response.text)
        wrong = self.client.post(
            base + "/secrets",
            json={"name": "model", "purpose": "openai", "value": "private-model-key"},
        ).json()
        response = self.client.put(
            base + "/repositories/" + repo["id"],
            json={
                "alias": "one",
                "owner": "a",
                "name": "one",
                "parent_id": repo["revision_id"],
                "credential_id": wrong["id"],
            },
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_pinning_rejects_missing_excessive_foreign_and_wrong_purpose_grants(self):
        from app.project_context import pin_context
        from app.projects import ProjectScope
        from fastapi import HTTPException

        project = self.client.post(
            "/api/v1/projects", json={"slug": "alpha", "name": "Alpha"}
        ).json()["id"]
        other = self.client.post(
            "/api/v1/projects", json={"slug": "beta", "name": "Beta"}
        ).json()["id"]
        base = f"/api/v1/projects/{project}"
        self.client.post(
            base + "/repositories", json={"alias": "backend", "owner": "a", "name": "b"}
        )
        own = self.client.post(
            base + "/secrets",
            json={"name": "git", "purpose": "github", "value": "own-private-key"},
        ).json()["id"]
        foreign = self.client.post(
            f"/api/v1/projects/{other}/secrets",
            json={"name": "model", "purpose": "openai", "value": "foreign-private-key"},
        ).json()["id"]
        for purposes, grants in [
            (["openai"], {}),
            ([], {"openai": own}),
            (["openai"], {"openai": own}),
            (["openai"], {"openai": foreign}),
        ]:
            with (
                self.subTest(purposes=purposes, grants=grants),
                self.sessions.begin() as db,
            ):
                with self.assertRaises(HTTPException):
                    pin_context(
                        db,
                        ProjectScope(project),
                        fixtures.Configurations(self.settings.root),
                        self.settings,
                        repository="backend",
                        workflow_revision_id="wf",
                        run_id="run",
                        stage_id="s",
                        execution_id="e",
                        attempt=1,
                        assignment_id="assignment",
                        purposes=purposes,
                        grants=grants,
                    )
        wrong = self.client.post(
            base + "/secrets",
            json={
                "name": "git",
                "purpose": "github",
                "value": "do-not-echo",
                "parent_id": foreign,
            },
        )
        self.assertEqual(wrong.status_code, 404)
        self.assertNotIn("do-not-echo", wrong.text)
