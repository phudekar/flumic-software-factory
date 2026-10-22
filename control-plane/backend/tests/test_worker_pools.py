"""Pool eligibility and worker-authenticated assignment boundaries."""

import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
import os
import test_api as fixtures
from app.reconciler import worker_token
from app.projects import ProjectScope
from app import database as models


class WorkerPoolTests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown
    create_worker = fixtures.ControlPlaneTests.create_worker

    def project(self, slug):
        identity = self.client.post(
            "/api/v1/projects", json={"slug": slug, "name": slug}
        ).json()["id"]
        repo = self.client.post(
            f"/api/v1/projects/{identity}/repositories",
            json={"alias": "repo", "owner": slug, "name": "repo"},
        ).json()["id"]
        return identity, repo

    def pool(self, name="shared"):
        response = self.client.post(
            "/api/v1/worker-pools", json={"name": name, "role": "simple"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def worker(self, pool, name="shared-one"):
        response = self.client.post(
            f"/api/v1/worker-pools/{pool['id']}/workers", json={"name": name}
        )
        self.assertEqual(response.status_code, 201, response.text)
        identity = response.json()["id"]
        headers = {
            "X-Worker-Id": identity,
            "Authorization": "Bearer " + worker_token(self.settings, identity),
        }
        response = self.client.post(
            "/api/v1/shared-worker/register",
            headers=headers,
            json={
                "protocol": "shared-v1",
                "pool_id": pool["id"],
                "role": "simple",
                "executors": ["builtin@1"],
                "actions": ["core.echo@1"],
                "normal_slots": 2,
                "durable_slots": 16,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return identity, headers

    def assignment(self, project, repo, pool, execution=None):
        from app.worker_pools import authorize_execution

        with self.sessions.begin() as db:
            row = authorize_execution(
                db,
                ProjectScope(project),
                self.settings,
                repository_id=repo,
                pool_id=pool["id"],
                run_id="run-" + project,
                stage_id="echo",
                execution_id=execution or str(uuid.uuid4()),
                role="simple",
                executor="builtin@1",
                action="core.echo@1",
                input={"message": project},
            )
            return row

    def claim(self, row, headers):
        return self.client.post(
            "/api/v1/shared-worker/claim",
            headers=headers,
            json={
                "project_id": row.project_id,
                "run_id": row.run_id,
                "execution_id": row.execution_id,
                "claim_key": str(uuid.uuid4()),
            },
        )

    def test_one_worker_claims_two_projects_and_completes_scoped_results(self):
        pool = self.pool()
        worker, headers = self.worker(pool)
        for slug in ("alpha", "beta"):
            project, repo = self.project(slug)
            self.assertEqual(
                self.client.put(
                    f"/api/v1/projects/{project}/worker-pools/{pool['id']}",
                    json={"enabled": True},
                ).status_code,
                200,
            )
            row = self.assignment(project, repo, pool)
            response = self.claim(row, headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["input"], {"message": project})
            lease = {"lease_token": response.json()["lease_token"]}
            path = f"/api/v1/shared-worker/assignments/{row.id}"
            self.assertEqual(
                self.client.post(
                    path + "/renew", json=lease, headers=headers
                ).status_code,
                200,
            )
            complete = {**lease, "result": {"output": {"echo": project}, "usage": []}}
            response = self.client.post(
                path + "/complete", json=complete, headers=headers
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                self.client.post(
                    path + "/complete", json=complete, headers=headers
                ).status_code,
                200,
            )
            complete["result"]["output"]["echo"] = "changed"
            self.assertEqual(
                self.client.post(
                    path + "/complete", json=complete, headers=headers
                ).status_code,
                409,
            )
            with self.sessions() as db:
                saved = db.get(models.Assignment, row.id)
                self.assertNotIn(project, saved.result_ciphertext)

    def test_legacy_bad_auth_wrong_pool_and_stale_lease_are_rejected(self):
        project, repo = self.project("alpha")
        pool = self.pool()
        self.client.put(
            f"/api/v1/projects/{project}/worker-pools/{pool['id']}",
            json={"enabled": True},
        )
        _, headers = self.worker(pool)
        row = self.assignment(project, repo, pool)
        self.assertEqual(
            self.claim(
                row,
                {
                    "X-Worker-Id": headers["X-Worker-Id"],
                    "Authorization": "Bearer wrong",
                },
            ).status_code,
            401,
        )
        legacy = self.create_worker()
        legacy_headers = {
            "X-Worker-Id": legacy.id,
            "Authorization": "Bearer " + worker_token(self.settings, legacy.id),
        }
        self.assertEqual(self.claim(row, legacy_headers).status_code, 409)
        self.assertEqual(
            self.claim(
                row, self.worker(self.pool("other"), "other-worker")[1]
            ).status_code,
            409,
        )
        claimed = self.claim(row, headers).json()
        path = f"/api/v1/shared-worker/assignments/{row.id}/renew"
        self.assertEqual(
            self.client.post(
                path, json={"lease_token": "wrong"}, headers=headers
            ).status_code,
            409,
        )
        with self.sessions.begin() as db:
            db.get(models.Assignment, row.id).expires_at = 0
        self.assertEqual(
            self.client.post(
                path, json={"lease_token": claimed["lease_token"]}, headers=headers
            ).status_code,
            409,
        )

    def test_membership_role_action_and_protocol_fail_closed(self):
        project, repo = self.project("alpha")
        pool = self.pool()
        identity, headers = self.worker(pool)
        from app.worker_pools import authorize_execution
        from fastapi import HTTPException

        with self.sessions.begin() as db, self.assertRaises(HTTPException):
            authorize_execution(
                db,
                ProjectScope(project),
                self.settings,
                repository_id=repo,
                pool_id=pool["id"],
                run_id="run",
                stage_id="s",
                execution_id="e",
                role="simple",
                executor="builtin@1",
                action="core.echo@1",
                input={},
            )
        self.client.put(
            f"/api/v1/projects/{project}/worker-pools/{pool['id']}",
            json={"enabled": True},
        )
        row = self.assignment(project, repo, pool)
        for field, value in [
            ("protocol", "legacy"),
            ("role", "developer"),
            ("actions", ["core.echo@2"]),
            ("executors", ["builtin@99"]),
            ("normal_slots", 999),
        ]:
            data = {
                "protocol": "shared-v1",
                "pool_id": pool["id"],
                "role": "simple",
                "executors": ["builtin@1"],
                "actions": ["core.echo@1"],
                "normal_slots": 2,
                "durable_slots": 16,
            }
            data[field] = value
            response = self.client.post(
                "/api/v1/shared-worker/register", headers=headers, json=data
            )
            self.assertIn(response.status_code, (409, 422), response.text)
        with self.sessions.begin() as db:
            db.get(models.Worker, identity).capabilities = {
                "executors": ["builtin@1"],
                "actions": [],
            }
        self.assertEqual(self.claim(row, headers).status_code, 409)

    @unittest.skipUnless(
        os.environ.get("CONTROL_TEST_DATABASE_URL", "").startswith("postgresql"),
        "PostgreSQL claim locking",
    )
    def test_concurrent_claims_have_one_owner(self):
        pool = self.pool()
        project, repo = self.project("alpha")
        self.client.put(
            f"/api/v1/projects/{project}/worker-pools/{pool['id']}",
            json={"enabled": True},
        )
        headers = [self.worker(pool, "worker-" + str(index))[1] for index in range(2)]
        row = self.assignment(project, repo, pool)
        with ThreadPoolExecutor(max_workers=2) as threads:
            codes = list(threads.map(lambda h: self.claim(row, h).status_code, headers))
        self.assertEqual(sorted(codes), [200, 409])

    def test_context_delivery_requires_own_live_assignment_and_membership(self):
        from app.project_context import pin_context

        project, repo = self.project("alpha")
        pool = self.pool()
        identity, headers = self.worker(pool)
        self.client.put(
            f"/api/v1/projects/{project}/worker-pools/{pool['id']}",
            json={"enabled": True},
        )
        row = self.assignment(project, repo, pool)
        key = self.client.post(
            f"/api/v1/projects/{project}/secrets",
            json={"name": "model", "purpose": "openai", "value": "private-model-value"},
        ).json()["id"]
        with self.sessions.begin() as db:
            pin = pin_context(
                db,
                ProjectScope(project),
                fixtures.Configurations(self.settings.root),
                self.settings,
                repository="repo",
                workflow_revision_id="wf",
                run_id=row.run_id,
                stage_id=row.stage_id,
                execution_id=row.execution_id,
                attempt=1,
                assignment_id=row.id,
                purposes=["openai"],
                grants={"openai": key},
            )
            db.get(models.Assignment, row.id).context_id = pin.id
        claimed = self.claim(row, headers).json()
        lease = {"lease_token": claimed["lease_token"]}
        path = f"/api/v1/shared-worker/assignments/{row.id}/context"
        response = self.client.post(path, json=lease, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["credentials"], {"openai": "private-model-value"}
        )
        _, other = self.worker(pool, "other-worker")
        self.assertEqual(
            self.client.post(path, json=lease, headers=other).status_code, 404
        )
        self.assertEqual(self.client.post(path, json=lease).status_code, 401)
        self.client.put(
            f"/api/v1/projects/{project}/worker-pools/{pool['id']}",
            json={"enabled": False},
        )
        self.assertEqual(
            self.client.post(path, json=lease, headers=headers).status_code, 409
        )

    def test_shared_workers_cannot_use_legacy_configuration_or_ingest(self):
        pool = self.pool()
        identity, headers = self.worker(pool)
        response = self.client.post(
            f"/api/v1/workers/{identity}/actions",
            json={"action": "configure", "revision_id": self.revision["id"]},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(
            self.client.post("/api/v1/ingest", json=[], headers=headers).status_code,
            409,
        )

    def test_pool_bootstrap_and_reconciler_are_infrastructure_only(self):
        from app.reconciler import reconcile_once

        pool = self.pool()
        identity, headers = self.worker(pool)
        response = self.client.get("/api/v1/worker-bootstrap", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("repository", response.text)
        self.assertNotIn("tasks", response.text)
        self.assertEqual(response.json()["pool_id"], pool["id"])
        self.assertTrue(reconcile_once(self.sessions, self.runtime, self.settings))
        worker, document, token = self.runtime.create.call_args.args
        self.assertEqual(document["protocol"], "shared-v1")
        self.assertEqual(worker.pool_id, pool["id"])
        self.assertNotIn("repository", str(document))

    def test_worker_slots_are_bounded_and_expired_effects_are_not_reclaimed(self):
        pool = self.pool()
        project, repo = self.project("alpha")
        _, headers = self.worker(pool)
        self.client.put(
            f"/api/v1/projects/{project}/worker-pools/{pool['id']}",
            json={"enabled": True},
        )
        rows = [self.assignment(project, repo, pool) for _ in range(3)]
        first = self.claim(rows[0], headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.claim(rows[1], headers).status_code, 200)
        self.assertEqual(self.claim(rows[2], headers).status_code, 409)
        with self.sessions.begin() as db:
            db.get(models.Assignment, rows[1].id).expires_at = 0
        self.assertEqual(self.claim(rows[2], headers).status_code, 409)
        self.assertEqual(
            self.client.post(
                f"/api/v1/shared-worker/assignments/{rows[0].id}/complete",
                headers=headers,
                json={
                    "lease_token": first.json()["lease_token"],
                    "result": {"output": {}},
                },
            ).status_code,
            200,
        )
        self.assertEqual(self.claim(rows[2], headers).status_code, 200)

    def test_echo_worker_executes_two_project_assignments_through_real_api(self):
        from factory.shared_worker import SharedControl, SharedSettings
        from factory.workflows.contracts import ActionInvocation
        from unittest.mock import patch

        pool = self.pool()
        identity, headers = self.worker(pool)
        worker = SharedControl(
            SharedSettings(
                pool_id=pool["id"],
                worker_id=identity,
                worker_name="shared-one",
                role="simple",
            ),
            {
                "FACTORY_CONTROL_ENDPOINT": "http://testserver",
                "FACTORY_CONTROL_TOKEN": worker_token(self.settings, identity),
            },
        )

        def request(path, body=None):
            response = self.client.post(path, json=body, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        with patch.object(worker, "request", side_effect=request):
            for slug in ("alpha", "beta"):
                project, repo = self.project(slug)
                self.client.put(
                    f"/api/v1/projects/{project}/worker-pools/{pool['id']}",
                    json={"enabled": True},
                )
                row = self.assignment(project, repo, pool)
                receipt = worker.execute(
                    ActionInvocation(
                        project_id=project,
                        run_id=row.run_id,
                        execution_id=row.execution_id,
                    )
                )
                self.assertEqual(receipt.state, "completed")
                self.assertNotIn(project, receipt.model_dump_json())
