import copy
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "worker"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "control-plane/backend"))

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select, create_engine, text
from sqlalchemy.engine import make_url
from app.database import (
    Base,
    database,
    Worker,
    Event,
    Conversation,
    Operation,
)
from app.settings import AppSettings
from app.main import create_app
from app.reconciler import worker_token, reconcile_once
from app.services import Configurations
from app.security import redact


ROOT = Path(os.environ.get("FACTORY_ROOT", str(Path(__file__).resolve().parents[3])))


class ControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = AppSettings(
            database_url="sqlite://",
            admin_password="test-admin-password",
            encryption_key=Fernet.generate_key().decode(),
            root=ROOT,
            snapshot_root=Path(self.temporary.name),
            host_root=ROOT,
            frontend=Path(self.temporary.name) / "no-frontend",
        )
        test_url = os.environ.get("CONTROL_TEST_DATABASE_URL", "sqlite://")
        self.pg_engine = None
        if test_url.startswith("postgresql"):
            self.schema = "test_cp_" + uuid.uuid4().hex
            self.pg_engine = create_engine(test_url)
            with self.pg_engine.begin() as connection:
                connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
            url = make_url(test_url).update_query_dict(
                {"options": "-csearch_path=" + self.schema}
            )
            test_url = url.render_as_string(hide_password=False)
        engine, self.sessions = database(test_url)
        self.test_engine = engine
        Base.metadata.create_all(engine)
        self.runtime = Mock()
        self.runtime.list.return_value = []
        self.workflow = Mock()
        self.workflow.workers.return_value = []
        self.workflow.runs.return_value = []
        self.app = create_app(self.settings, self.sessions, self.runtime, self.workflow)
        self.client = TestClient(self.app)
        self.client.__enter__()
        login = self.client.post(
            "/api/v1/auth/login", json={"password": "test-admin-password"}
        )
        self.csrf = login.json()["csrf"]
        self.client.headers.update(
            {"X-CSRF-Token": self.csrf, "Idempotency-Key": str(uuid.uuid4())}
        )
        self.revision = self.client.get("/api/v1/config/revisions").json()[0]

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temporary.cleanup()
        self.test_engine.dispose()
        if self.pg_engine:
            with self.pg_engine.begin() as connection:
                connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))
            self.pg_engine.dispose()

    def create_worker(self, name="factory-test"):
        response = self.client.post(
            "/api/v1/workers",
            json={
                "name": name,
                "profile": "simple",
                "revision_id": self.revision["id"],
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        with self.sessions() as db:
            return db.get(Worker, response.json()["worker_id"])

    def ingest(self, worker, events):
        token = worker_token(self.settings, worker.id)
        return self.client.post(
            "/api/v1/ingest",
            json=events,
            headers={"X-Worker-Id": worker.id, "Authorization": "Bearer " + token},
        )

    def test_authentication_csrf_and_origin(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/v1/workers").status_code, 401)
        result = self.client.post("/api/v1/auth/login", json={"password": "wrong"})
        self.assertEqual(result.status_code, 401)
        self.client.post("/api/v1/auth/login", json={"password": "test-admin-password"})
        self.assertEqual(
            self.client.post(
                "/api/v1/auth/logout", json={}, headers={"X-CSRF-Token": "wrong"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/auth/logout",
                json={},
                headers={"Origin": "https://evil.example"},
            ).status_code,
            403,
        )

    def test_create_is_stopped_and_idempotent(self):
        worker = self.create_worker()
        self.assertEqual(worker.desired_state, "created")
        duplicate = self.client.post(
            "/api/v1/workers",
            json={
                "name": worker.name,
                "profile": "simple",
                "revision_id": self.revision["id"],
            },
        )
        self.assertEqual(duplicate.status_code, 202)
        self.runtime.start.assert_not_called()
        self.assertTrue(reconcile_once(self.sessions, self.runtime, self.settings))
        self.runtime.create.assert_called_once()
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(Operation)).status, "completed")

    def test_worker_credential_never_in_list_and_external_cannot_be_controlled(self):
        worker = self.create_worker()
        data = self.client.get("/api/v1/workers").json()
        self.assertNotIn(worker.credential_hash, str(data))
        self.assertNotIn(worker_token(self.settings, worker.id), str(data))
        response = self.client.post(
            "/api/v1/workers/not-owned/actions", json={"action": "stop"}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            self.client.post(
                "/api/v1/workers/" + worker.id + "/actions", json={"action": "drain"}
            ).status_code,
            422,
        )

    def test_configuration_application_requires_a_stopped_worker(self):
        worker = self.create_worker()
        reconcile_once(self.sessions, self.runtime, self.settings)
        self.runtime.list.return_value = [
            {"instance_id": worker.id, "status": "running"}
        ]
        response = self.client.post(
            "/api/v1/workers/" + worker.id + "/actions",
            json={"action": "configure", "revision_id": self.revision["id"]},
        )
        self.assertEqual(response.status_code, 409)
        self.runtime.list.return_value = [
            {"instance_id": worker.id, "status": "exited"}
        ]
        response = self.client.post(
            "/api/v1/workers/" + worker.id + "/actions",
            json={"action": "configure", "revision_id": self.revision["id"]},
        )
        self.assertEqual(response.status_code, 202, response.text)
        reconcile_once(self.sessions, self.runtime, self.settings)
        self.runtime.configure.assert_called_once()
        self.runtime.start.assert_not_called()

    def test_only_known_targets_and_profiles_can_be_created(self):
        data = {
            "name": "factory-invalid",
            "profile": "missing",
            "revision_id": self.revision["id"],
        }
        self.assertEqual(
            self.client.post("/api/v1/workers", json=data).status_code, 422
        )
        data.update(profile="simple", target_id="untrusted-cluster")
        self.assertEqual(
            self.client.post("/api/v1/workers", json=data).status_code, 422
        )

    def test_config_conflict_and_snapshot_provenance(self):
        revision = self.client.get(
            "/api/v1/config/revisions/" + self.revision["id"]
        ).json()
        document = revision["document"]
        document["tasks"]["plan_issue"]["additional_system_prompt"] = (
            "Prefer concrete acceptance criteria."
        )
        body = {
            "document": document,
            "parent_id": revision["id"],
            "summary": "Prompt update",
        }
        saved = self.client.post("/api/v1/config/revisions", json=body)
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(
            self.client.post("/api/v1/config/revisions", json=body).status_code, 409
        )
        self.assertIn(
            "Prefer concrete",
            saved.json()["document"]["tasks"]["plan_issue"]["additional_system_prompt"],
        )
        snapshot = Configurations(ROOT).snapshot(
            document, "simple", "unique-test-worker", saved.json()["id"]
        )
        self.assertEqual(snapshot["defaults"]["worker_name"], "unique-test-worker")
        self.assertEqual(snapshot["defaults"]["control_revision"], saved.json()["id"])

    def test_unsafe_configuration_is_rejected(self):
        revision = self.client.get(
            "/api/v1/config/revisions/" + self.revision["id"]
        ).json()
        document = copy.deepcopy(revision["document"])
        document["defaults"]["planner_factory"] = "untrusted:execute"
        self.assertEqual(
            self.client.post(
                "/api/v1/config/validate", json={"document": document}
            ).status_code,
            422,
        )
        document = copy.deepcopy(revision["document"])
        document["defaults"]["OPENAI_API_KEY"] = "placeholder"
        self.assertEqual(
            self.client.post(
                "/api/v1/config/validate", json={"document": document}
            ).status_code,
            422,
        )

    def test_ingestion_deduplicates_redacts_encrypts_and_scopes(self):
        worker = self.create_worker()
        identifier = str(uuid.uuid4())
        events = [
            {
                "id": identifier,
                "kind": "model.completed",
                "occurred_at": time.time(),
                "run_id": "factory-run",
                "attempt_id": "attempt-1",
                "stage": "triage_issue",
                "payload": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "estimated_usd": 0.001,
                    "conversation": {
                        "request": "authorization: Bearer credentialvalue1234567890",
                        "response": "ordinary response",
                    },
                },
            }
        ]
        self.assertEqual(self.ingest(worker, events).status_code, 202)
        self.assertEqual(self.ingest(worker, events).status_code, 202)
        with self.sessions() as db:
            self.assertEqual(len(db.scalars(select(Event)).all()), 1)
            content = db.get(Conversation, identifier)
            self.assertNotIn("ordinary response", content.ciphertext)
        result = self.client.get("/api/v1/model-calls/" + identifier + "/conversation")
        self.assertEqual(result.status_code, 200)
        self.assertNotIn("credentialvalue1234567890", result.text)
        self.assertIn("ordinary response", result.text)
        self.assertEqual(
            self.client.post(
                "/api/v1/ingest",
                json=events,
                headers={"X-Worker-Id": worker.id, "Authorization": "Bearer wrong"},
            ).status_code,
            401,
        )
        usage = self.client.get("/api/v1/usage").json()
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["estimated_usd"], 0.001)
        self.assertNotIn(
            "conversation", self.client.get("/api/v1/events").json()[0]["payload"]
        )

    def test_worker_capture_flows_to_encrypted_history(self):
        from factory.adapters.control import ControlObserver
        from factory.adapters.telemetry import LoggingObserver
        from factory.application.telemetry import (
            model_call,
            record_conversation,
            ModelUsage,
        )
        from factory.settings import Settings

        worker = self.create_worker()
        publisher = Mock()
        recorded = []

        def emit(kind, payload, metadata):
            identifier = str(uuid.uuid4())
            event = {
                "id": identifier,
                "kind": kind,
                "occurred_at": time.time(),
                "run_id": metadata.get("factory_run_id"),
                "attempt_id": metadata.get("attempt_id"),
                "stage": metadata.get("stage"),
                "payload": payload,
            }
            response = self.ingest(worker, [event])
            self.assertEqual(response.status_code, 202, response.text)
            if kind == "model.completed":
                recorded.append(identifier)

        publisher.emit.side_effect = emit
        observer = ControlObserver(
            LoggingObserver(),
            Settings(control_revision=self.revision["id"]),
            publisher=publisher,
        )
        try:
            with observer.observe(
                "triage_issue", {"factory_run_id": "synthetic-integration-run"}
            ):
                with model_call("openai", "gpt-5-nano") as call:
                    record_conversation(
                        request=[
                            {
                                "role": "system",
                                "content": "Synthetic instructions. No real provider call.",
                            }
                        ]
                    )
                    call.record(
                        ModelUsage(
                            input_tokens=100, output_tokens=10, cached_input_tokens=0
                        )
                    )
                    record_conversation(response="Synthetic completion")
        finally:
            observer.shutdown()
        events = self.client.get(
            "/api/v1/events?run_id=synthetic-integration-run"
        ).json()
        self.assertTrue(any(event["kind"] == "attempt.completed" for event in events))
        conversation = self.client.get(
            "/api/v1/model-calls/" + recorded[0] + "/conversation"
        ).json()
        self.assertEqual(conversation["response"], "Synthetic completion")
        usage = self.client.get("/api/v1/usage").json()
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 10)

    def test_unknown_costs_and_parent_aggregates_are_not_added(self):
        worker = self.create_worker()
        events = [
            {
                "id": str(uuid.uuid4()),
                "kind": "model.completed",
                "occurred_at": time.time(),
                "payload": data,
            }
            for data in [
                {"accounting_leaf": True, "input_tokens": 10},
                {"accounting_leaf": False, "input_tokens": 900, "estimated_usd": 10},
            ]
        ]
        self.ingest(worker, events)
        usage = self.client.get("/api/v1/usage").json()
        self.assertEqual(usage["calls"], 1)
        self.assertEqual(usage["input_tokens"], 10)
        self.assertIsNone(usage["estimated_usd"])
        self.assertEqual(usage["unpriced_calls"], 1)

    def test_safe_integration_failures_and_missing_conversations(self):
        self.workflow.runs.side_effect = RuntimeError(
            "private credential must not leak"
        )
        response = self.client.get("/api/v1/runs")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["warnings"])
        self.assertNotIn("private credential", response.text)
        self.assertEqual(
            self.client.get("/api/v1/model-calls/missing/conversation").status_code, 404
        )

    @unittest.skipUnless(
        os.environ.get("CONTROL_TEST_DATABASE_URL"), "PostgreSQL integration only"
    )
    def test_postgres_concurrent_config_updates_conflict(self):
        from concurrent.futures import ThreadPoolExecutor

        revision = self.client.get(
            "/api/v1/config/revisions/" + self.revision["id"]
        ).json()
        body = {
            "document": revision["document"],
            "parent_id": revision["id"],
            "summary": "Concurrent change",
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: (
                        self.client.post(
                            "/api/v1/config/revisions", json=body
                        ).status_code
                    ),
                    range(2),
                )
            )
        self.assertEqual(sorted(results), [201, 409])

    @unittest.skipUnless(
        os.environ.get("CONTROL_TEST_DATABASE_URL"), "PostgreSQL integration only"
    )
    def test_postgres_concurrent_ingestion_is_idempotent(self):
        from concurrent.futures import ThreadPoolExecutor

        worker = self.create_worker()
        events = [
            {
                "id": str(uuid.uuid4()),
                "kind": "model.completed",
                "occurred_at": time.time(),
                "payload": {"input_tokens": 10, "output_tokens": 2},
            }
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(
                pool.map(lambda _: self.ingest(worker, events).status_code, range(2))
            )
        self.assertEqual(statuses, [202, 202])
        with self.sessions() as db:
            self.assertEqual(len(db.scalars(select(Event)).all()), 1)

    def saved_profile(self):
        return self.client.get("/api/v1/worker-profiles").json()[0]

    def save_profile(self, profile, **changes):
        body = {
            key: profile[key]
            for key in ("name", "profile", "factory_revision_id", "document")
        }
        body.update(parent_id=profile["id"], **changes)
        return self.client.post("/api/v1/worker-profiles", json=body)

    def profile_worker(self, profile, name):
        self.client.headers["Idempotency-Key"] = str(uuid.uuid4())
        response = self.client.post(
            "/api/v1/workers",
            json={"name": name, "worker_profile_id": profile["family_id"]},
        )
        self.assertEqual(response.status_code, 202, response.text)
        with self.sessions() as db:
            return db.get(Worker, response.json()["worker_id"])

    def test_shared_profile_updates_all_linked_workers_on_start(self):
        profile = self.saved_profile()
        workers = [
            self.profile_worker(profile, "linked-one"),
            self.profile_worker(profile, "linked-two"),
        ]
        for _ in workers:
            reconcile_once(self.sessions, self.runtime, self.settings)
        document = copy.deepcopy(profile["document"])
        document["settings"].update(
            openai_model="test-model", startup_script="echo prerequisites"
        )
        document["tasks"]["plan_issue"]["additional_system_prompt"] = (
            "Profile-local guidance"
        )
        saved = self.save_profile(profile, document=document)
        self.assertEqual(saved.status_code, 201, saved.text)
        with self.sessions() as db:
            self.assertEqual(
                db.get(Worker, workers[0].id).revision_id, workers[0].revision_id
            )
        for worker in workers:
            self.client.headers["Idempotency-Key"] = str(uuid.uuid4())
            response = self.client.post(
                "/api/v1/workers/" + worker.id + "/actions", json={"action": "start"}
            )
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["action"], "configure_start")
            reconcile_once(self.sessions, self.runtime, self.settings)
            with self.sessions() as db:
                current = db.get(Worker, worker.id)
                self.assertEqual(current.config_id, saved.json()["id"])
                from app.database import Revision

                snapshot = db.get(Revision, current.revision_id)
                self.assertEqual(
                    snapshot.document["defaults"]["openai_model"], "test-model"
                )
                self.assertEqual(
                    snapshot.document["defaults"]["startup_script"],
                    "echo prerequisites",
                )
        self.assertEqual(self.runtime.configure.call_count, 2)
        self.assertEqual(self.runtime.action.call_count, 2)
        # Compiled worker snapshots never become factory revisions.
        self.assertEqual(len(self.client.get("/api/v1/config/revisions").json()), 1)
        self.assertEqual(self.save_profile(profile).status_code, 409)

    def test_profile_clone_is_independent_and_scope_is_enforced(self):
        profile = self.saved_profile()
        body = {
            key: profile[key]
            for key in ("name", "profile", "factory_revision_id", "document")
        }
        body.update(name="Independent clone", cloned_from=profile["id"])
        result = self.client.post("/api/v1/worker-profiles", json=body)
        self.assertEqual(result.status_code, 201, result.text)
        self.assertNotEqual(result.json()["family_id"], profile["family_id"])
        self.assertEqual(result.json()["cloned_from"], profile["id"])
        bad = copy.deepcopy(profile["document"])
        bad["settings"]["scan_cron"] = "* * * * *"
        self.assertEqual(self.save_profile(profile, document=bad).status_code, 422)
        bad = copy.deepcopy(profile["document"])
        bad["tasks"]["plan_issue"]["retries"] = 9
        self.assertEqual(self.save_profile(profile, document=bad).status_code, 422)
        factory = self.client.get("/api/v1/factory-config").json()
        for value in factory["document"]["profiles"].values():
            self.assertNotIn("openai_model", value["settings"])
            self.assertNotIn("startup_script", value["settings"])
            self.assertNotIn("additional_system_prompt", value["tasks"]["plan_issue"])
        response = self.client.post(
            "/api/v1/factory-config",
            json={"document": factory["document"], "parent_id": factory["id"]},
        )
        self.assertEqual(response.status_code, 201, response.text)

    def test_worker_authenticated_bootstrap_loads_latest_profile_on_process_restart(
        self,
    ):
        profile = self.saved_profile()
        worker = self.profile_worker(profile, "bootstrap-worker")
        doc = copy.deepcopy(profile["document"])
        doc["settings"]["log_level"] = "DEBUG"
        saved = self.save_profile(profile, document=doc).json()
        self.assertEqual(self.client.get("/api/v1/worker-bootstrap").status_code, 401)
        self.client.cookies.clear()
        response = self.client.get(
            "/api/v1/worker-bootstrap",
            headers={
                "X-Worker-Id": worker.id,
                "Authorization": "Bearer " + worker_token(self.settings, worker.id),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["defaults"]["log_level"], "DEBUG")
        self.assertEqual(response.json()["defaults"]["worker_name"], worker.name)
        with self.sessions() as db:
            self.assertEqual(db.get(Worker, worker.id).config_id, saved["id"])

    def test_execution_filters_apply_before_usage_and_page_limits(self):
        worker = self.create_worker()
        events = [
            {
                "id": str(uuid.uuid4()),
                "kind": "model.completed",
                "occurred_at": time.time() + i,
                "run_id": "logical-run",
                "stage": "plan_issue",
                "attempt_id": "attempt-" + str(i),
                "payload": {
                    "engine_workflow_run_id": "engine-run",
                    "engine_task_run_id": "task-" + str(i),
                    "input_tokens": 10 + i,
                    "output_tokens": 1,
                    "estimated_usd": 0.01,
                },
            }
            for i in range(3)
        ]
        self.assertEqual(self.ingest(worker, events).status_code, 202)
        query = (
            "?run_id=engine-run&worker_id="
            + worker.id
            + "&engine_task_id=task-0&attempt_id=attempt-0"
        )
        data = self.client.get("/api/v1/events" + query + "&limit=1").json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["attempt_id"], "attempt-0")
        usage = self.client.get("/api/v1/usage" + query).json()
        self.assertEqual(usage["calls"], 1)
        self.assertEqual(usage["input_tokens"], 10)
        self.assertEqual(
            self.client.get("/api/v1/usage" + query + "&stage=wrong").json()["calls"], 0
        )
        self.assertEqual(self.client.get("/api/v1/events?worker_id=other").json(), [])

    @unittest.skipUnless(
        os.environ.get("CONTROL_TEST_DATABASE_URL"), "PostgreSQL integration only"
    )
    def test_postgres_concurrent_profile_updates_conflict(self):
        from concurrent.futures import ThreadPoolExecutor

        profile = self.saved_profile()
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(
                pool.map(lambda _: self.save_profile(profile).status_code, range(2))
            )
        self.assertEqual(sorted(statuses), [201, 409])

    def test_import_preserves_existing_worker_snapshot_and_links_shared_profile(self):
        from app.worker_configs import bootstrap

        first = self.create_worker("legacy-one")
        self.client.headers["Idempotency-Key"] = str(uuid.uuid4())
        second = self.create_worker("legacy-two")
        with self.sessions.begin() as db:
            for worker in (first, second):
                db.get(Worker, worker.id).config_id = None
        with self.sessions.begin() as db:
            bootstrap(db, Configurations(ROOT))
        with self.sessions() as db:
            one, two = db.get(Worker, first.id), db.get(Worker, second.id)
            self.assertEqual(one.config_id, two.config_id)
            self.assertEqual(one.revision_id, first.revision_id)
            self.assertEqual(two.revision_id, second.revision_id)
        self.runtime.configure.assert_not_called()

    def test_redaction_and_utf8_bounds(self):
        self.assertNotIn(
            "abcdefghijklmnopqrstuvwxyz123456",
            redact("sk-abcdefghijklmnopqrstuvwxyz123456"),
        )
        self.assertIn("[TRUNCATED]", redact("界" * 50000))


if __name__ == "__main__":
    unittest.main()
