"""Opt-in deterministic local Docker job and broker restart, no provider calls.

RUN_DOCKER_JOB_CONTRACT=1 DOCKER_JOB_CONTRACT_IMAGE=repo@sha256:... python -m unittest discover -s tests/integration -p test_docker_job_contract.py -v
Use a local digest-pinned image containing Python, e.g. the control-plane image.
Only uniquely named flumic-job-* containers belonging to this fixture are touched.
"""

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(
    os.environ.get("RUN_DOCKER_JOB_CONTRACT") == "1", "opt-in Docker job contract"
)
class DockerJobContract(unittest.TestCase):
    def test_job_and_broker_restart_reattach_then_capture_cleanup(self):
        sys.path[:0] = [
            str(ROOT / "worker"),
            str(ROOT / "control-plane/backend"),
            str(ROOT / "control-plane/backend/tests"),
        ]
        from test_docker_jobs import JobBrokerTests
        from app.job_broker import JobBroker
        from app.adapters.docker_jobs import DockerJobRuntime
        from app.job_contracts import RuntimeJobRef
        from app.database import RuntimeJob
        from app.project_context import decrypt

        image = os.environ["DOCKER_JOB_CONTRACT_IMAGE"]
        case = JobBrokerTests("test_same_key_durable_reattach_and_digest_conflict")
        case.setUp()
        self.addCleanup(case.tearDown)
        case.arrange(
            image=image,
            command=[
                "python",
                "-c",
                "import json,time; time.sleep(2); v=json.load(open('/flumic/input.json')); assert v['hello']=='world'; s=json.load(open('/flumic/credentials.json')); assert set(s)=={'openai'}; print('deterministic job'); json.dump({'output':{'ok':True}},open('/flumic/output/result.json','w'))",
            ],
        )
        (ROOT / "output").mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(prefix="job-contract-", dir=ROOT / "output")
        self.addCleanup(tmp.cleanup)
        runtime = DockerJobRuntime(
            Path(tmp.name), Path(tmp.name), "flumic-job-contract"
        )
        case.broker = JobBroker(
            case.sessions, case.settings, clock=lambda: case.now, root=Path(tmp.name)
        )
        receipt = case.submit()
        with case.sessions() as db:
            document = decrypt(
                db.get(RuntimeJob, receipt.job_id).launch_ciphertext, case.settings
            )
            ref = RuntimeJobRef(
                **{k: document["launch"][k] for k in RuntimeJobRef.model_fields}
            )

        def cleanup():
            runtime.terminate(ref, grace_seconds=1)
            runtime.cleanup(ref)

        self.addCleanup(cleanup)
        case.broker.reconcile_once(runtime)
        first = runtime.client.containers.get("flumic-job-" + receipt.job_id).id
        # New broker process state, same persisted database/filesystem/container.
        case.broker = JobBroker(
            case.sessions, case.settings, clock=lambda: case.now, root=Path(tmp.name)
        )
        end = time.monotonic() + 40
        while time.monotonic() < end:
            case.broker.reconcile_once(runtime)
            current = case.submit()
            if current.state in {"completed", "failed", "cancelled", "timed-out"}:
                break
            time.sleep(0.2)
        self.assertEqual(current.state, "completed", current)
        self.assertEqual(current.result["output"], {"ok": True})
        self.assertIn("deterministic job", current.result["stdout"])
        self.assertEqual(
            runtime.client.containers.get("flumic-job-" + receipt.job_id).id, first
        )
        case.broker.reconcile_once(runtime)
        self.assertEqual(runtime.inspect(ref).state, "missing")
        self.assertEqual(case.submit(), current)
