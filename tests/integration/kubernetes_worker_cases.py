"""Real scheduler/pull failures must remain infrastructure observations, not health."""

from dataclasses import replace, asdict
import json
import os
from pathlib import Path
import tempfile
import time
import uuid


def run_worker_failure_cases(case):
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from project_release_fixture import ProjectReleaseFixture
    from app.database import Worker, Operation, RuntimeResourceBinding
    from app.main import create_app
    from app.reconciler import reconcile_once
    from app.runtime_router import RuntimeRouter

    case.assertTrue(
        os.environ.get("CONTROL_TEST_DATABASE_URL", "").startswith("postgresql")
    )
    root = Path(__file__).resolve().parents[2]
    worker_ns = next(n for n in case.config.namespace_uids if "-worker-" in n)
    job_ns = next(n for n in case.config.namespace_uids if "-action-" in n)
    records = []
    for scenario, expected in [
        ("unschedulable", "unschedulable"),
        ("invalid-image", "image-pull-backoff"),
    ]:
        private = Path(
            tempfile.mkdtemp(
                prefix="worker-cases-",
                dir=root / ".superpowers/sdd/2026-10-27-kubernetes-runtime",
            )
        )
        target = json.loads(
            (root / "deploy/kubernetes/target.example.json").read_text()
        )["targets"][0]
        image = (
            os.environ["FLUMIC_KUBERNETES_HELPER_IMAGE"]
            if scenario == "unschedulable"
            else "localhost:5117/flumic-worker@sha256:" + "f" * 64
        )
        target.update(
            {
                "auth": {
                    "mode": "kubeconfig",
                    "kubeconfig": os.environ.get(
                        "FLUMIC_KUBERNETES_CONTROLLER_KUBECONFIG",
                        case.config.kubeconfig,
                    ),
                    "context": case.config.context,
                },
                "worker_namespace": worker_ns,
                "worker_namespace_uid": case.config.namespace_uids[worker_ns],
                "job_namespace": job_ns,
                "job_namespace_uid": case.config.namespace_uids[job_ns],
                "images": {
                    "simple": image,
                    "developer": image,
                    "helper": os.environ["FLUMIC_KUBERNETES_HELPER_IMAGE"],
                },
                "node_selector": {"flumic.io/fixture-node": "no-such-node"}
                if scenario == "unschedulable"
                else {},
            }
        )

        def configure(settings):
            path = private / "targets.json"
            path.write_text(json.dumps({"schema_version": 1, "targets": [target]}))
            path.chmod(0o600)
            return replace(settings, runtime_targets_file=path)

        f = ProjectReleaseFixture(
            settings_transform=configure, worker_target_id=target["id"]
        )
        recovery = asdict(f.settings)
        recovery["database_url"] = f.case.test_engine.url.render_as_string(
            hide_password=False
        )
        path = private / "recovery.json"
        path.write_text(json.dumps(recovery, default=str))
        path.chmod(0o600)
        router = RuntimeRouter(f.settings, f.config, f.sessions)
        with f.sessions.begin() as db:
            for worker in db.scalars(select(Worker)):
                worker.registered_at = None
                worker.capabilities = {}

        def settle(worker_id):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                reconcile_once(f.sessions, router, f.settings)
                with f.sessions() as db:
                    operations = list(
                        db.scalars(
                            select(Operation).where(Operation.worker_id == worker_id)
                        )
                    )
                    failed = [value for value in operations if value.status == "failed"]
                    case.assertFalse(failed, "Worker failure fixture lifecycle failed")
                    if operations and all(
                        value.status == "completed" for value in operations
                    ):
                        return
                time.sleep(0.3)
            raise AssertionError("Worker failure fixture lifecycle did not settle")

        def action(worker_id, verb):
            f.call(
                "/api/v1/workers/" + worker_id + "/actions",
                {"action": verb},
                202,
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )
            settle(worker_id)

        cleaned = False
        try:
            worker_id = f.workers["simple"]["id"]
            settle(worker_id)
            action(worker_id, "start")
            app = create_app(f.settings, f.sessions, router, f.case.workflow)
            with TestClient(app) as browser:
                case.assertEqual(
                    browser.post(
                        "/api/v1/auth/login", json={"password": "test-admin-password"}
                    ).status_code,
                    200,
                )
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    response = browser.get("/api/v1/workers")
                    case.assertEqual(response.status_code, 200)
                    worker = next(
                        value
                        for value in response.json()["managed"]
                        if value["id"] == worker_id
                    )
                    if worker.get("runtime", {}).get("status") == expected:
                        break
                    time.sleep(0.5)
                else:
                    raise AssertionError(
                        "Expected native infrastructure failure was not observed"
                    )
                case.assertIsNone(worker["engine"])
                case.assertNotEqual(worker["runtime"]["status"], "running")
                case.assertNotIn("target_snapshot", worker)
                case.assertNotIn("credential_hash", worker)
                records.append(
                    {
                        "scenario": scenario,
                        "runtime_status": worker["runtime"]["status"],
                        "registered": False,
                        "worker_id": worker_id,
                    }
                )
        finally:
            for role in ["simple", "developer"]:
                action(f.workers[role]["id"], "remove")
            with f.sessions() as db:
                pvcs = list(
                    db.scalars(
                        select(RuntimeResourceBinding).where(
                            RuntimeResourceBinding.kind == "PersistentVolumeClaim"
                        )
                    )
                )
            for binding in pvcs:
                value = case.fixture.client.get(
                    "PersistentVolumeClaim", worker_ns, binding.name
                )
                case.assertEqual(value["metadata"]["uid"], binding.uid)
                case.assertFalse(value["metadata"].get("ownerReferences"))
                case.fixture.guard()
                case.fixture.client.delete(
                    "PersistentVolumeClaim",
                    worker_ns,
                    binding.name,
                    uid=binding.uid,
                    resource_version=value["metadata"]["resourceVersion"],
                )
            cleaned = True
            f.close()
            import shutil

            shutil.rmtree(private)
        case.assertTrue(cleaned)
    (root / "output/kubernetes-evidence/qualification-worker-failures.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fixture_id": case.config.fixture_id,
                "cluster_uid": case.config.cluster_uid,
                "cases": records,
            },
            indent=2,
        )
        + "\n"
    )
