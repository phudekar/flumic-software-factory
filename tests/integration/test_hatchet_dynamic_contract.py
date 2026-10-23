"""Opt-in real engine contracts: FLUMIC_LIVE_HATCHET=1, test-only profile/token.

This creates uniquely named echo workflows/workers in the selected tenant. No
model, GitHub, Docker or project credentials are used. Keep logs as engine evidence.
"""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


@unittest.skipUnless(
    os.environ.get("FLUMIC_LIVE_HATCHET") == "1", "Opt-in live Hatchet echo contract"
)
class LiveHatchetContract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from hatchet_sdk import Hatchet
        from hatchet_dynamic_fixture import register

        self.temp = tempfile.TemporaryDirectory(prefix="flumic-contract-")
        self.root = Path(self.temp.name)
        self.namespace = "flumic-contract-" + uuid.uuid4().hex[:12]
        self.client = Hatchet()
        self.tasks, _ = register(self.client, self.root, self.namespace)
        self.processes = []
        self.logs = []
        self.decoy = self.worker("decoy")
        self.control = self.worker("control")
        self.action = self.worker("action")
        await asyncio.sleep(5)

    def worker(self, role):
        log = (self.root / f"{role}-{len(self.logs)}.log").open("w")
        self.logs.append(log)
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("hatchet_dynamic_fixture.py")),
                str(self.root),
                self.namespace,
                role,
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.processes.append(process)
        return process

    async def asyncTearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                await asyncio.to_thread(process.wait, 10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)
        for log in self.logs:
            log.close()
        destination = os.environ.get("FLUMIC_LIVE_EVIDENCE_DIR")
        if destination:
            import shutil

            shutil.copytree(
                self.root, Path(destination) / self.namespace, dirs_exist_ok=True
            )
        self.temp.cleanup()

    async def wait_file(self, path, timeout=90):
        deadline = time.monotonic() + timeout
        while not path.exists():
            if time.monotonic() > deadline:
                self.fail("Live engine did not reach " + path.name)
            await asyncio.sleep(0.2)

    async def launch(self, run, *, markers=()):
        from factory.workflows.contracts import RunInvocation

        folder = self.root / run
        folder.mkdir()
        for marker in markers:
            (folder / marker).touch()
        ref = await self.tasks.coordinator.aio_run(
            RunInvocation(project_id="echo-only", run_id=run), wait_for_result=False
        )
        return folder, ref

    async def test_parent_restart_reattaches_completed_child(self):
        folder, ref = await self.launch("restart")
        await self.wait_file(folder / "start-1")
        self.control.kill()
        await asyncio.to_thread(self.control.wait)
        self.control = self.worker("control")
        result = await asyncio.wait_for(ref.aio_result(), 180)
        from factory.workflows.contracts import RunAdvance

        self.assertEqual(RunAdvance.model_validate(result).state, "completed")
        effects = [
            json.loads(line)["execution"]
            for line in (folder / "effects.jsonl").read_text().splitlines()
        ]
        self.assertEqual(effects.count("restart-0"), 1)
        self.assertEqual(effects.count("restart-1"), 1)
        self.assertEqual(effects.count("restart-2"), 1)

    async def test_failure_settles_without_starting_descendant(self):
        folder, ref = await self.launch("failure", markers=("fail",))
        result = await asyncio.wait_for(ref.aio_result(), 120)
        from factory.workflows.contracts import RunAdvance

        self.assertEqual(RunAdvance.model_validate(result).state, "failed")
        self.assertFalse((folder / "start-2").exists())

    async def test_cooperative_cancel_settles_active_child(self):
        folder, ref = await self.launch("cancel")
        await self.wait_file(folder / "start-1")
        (folder / "cancel").touch()
        result = await asyncio.wait_for(ref.aio_result(), 120)
        from factory.workflows.contracts import RunAdvance

        self.assertEqual(RunAdvance.model_validate(result).state, "cancelled")
        self.assertTrue((folder / "done-1").exists())
        self.assertFalse((folder / "start-2").exists())

    async def test_capacity_eviction_pool_affinity_and_single_action_slot(self):
        first, one = await self.launch("capacity-one")
        await self.wait_file(first / "start-1")
        second, two = await self.launch("capacity-two")
        results = await asyncio.wait_for(
            asyncio.gather(one.aio_result(), two.aio_result()), 180
        )
        from factory.workflows.contracts import RunAdvance

        self.assertTrue(
            all(
                RunAdvance.model_validate(result).state == "completed"
                for result in results
            )
        )
        # Same process reentry is eviction replay, independently of process death.
        entries = [
            json.loads(line)
            for line in (first / "entries.jsonl").read_text().splitlines()
        ]
        self.assertGreaterEqual(len(entries), 2, "No real parent eviction observed")
        self.assertEqual(len({entry["pid"] for entry in entries}), 1)
        intervals = []
        for folder in (first, second):
            effects = [
                json.loads(line)
                for line in (folder / "effects.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(effects), 3)
            self.assertEqual({item["pid"] for item in effects}, {self.action.pid})
            for item in effects:
                index = item["execution"].rsplit("-", 1)[-1]
                intervals.append(
                    (item["start"], float((folder / f"done-{index}").read_text()))
                )
        intervals.sort()
        self.assertTrue(
            all(
                previous[1] <= following[0]
                for previous, following in zip(intervals, intervals[1:])
            ),
            "Normal slot over-admitted actions",
        )

    async def test_known_failure_uses_new_attempt_child_identity(self):
        folder, ref = await self.launch("retry", markers=("retry",))
        result = await asyncio.wait_for(ref.aio_result(), 120)
        from factory.workflows.contracts import RunAdvance

        self.assertEqual(RunAdvance.model_validate(result).state, "completed")
        executions = [
            json.loads(line)["execution"]
            for line in (folder / "effects.jsonl").read_text().splitlines()
        ]
        self.assertEqual(executions, ["retry-0", "retry-1", "retry-retry-1", "retry-2"])

    async def test_engine_parent_cancel_does_not_prove_sync_child_stopped(self):
        folder, ref = await self.launch("engine-cancel")
        await self.wait_file(folder / "start-1")
        (folder / "cancel").touch()
        await self.client.runs.aio_cancel(ref.workflow_run_id)
        await self.wait_file(folder / "done-1")
        deadline = time.monotonic() + 30
        while True:
            status = await self.client.runs.aio_get_status(ref.workflow_run_id)
            if str(status.value) == "CANCELLED":
                break
            if time.monotonic() > deadline:
                self.fail("Engine parent cancellation did not settle")
            await asyncio.sleep(0.2)
        self.assertFalse((folder / "start-2").exists())

    async def test_transient_control_failure_recovers_without_new_execution(self):
        folder, ref = await self.launch("control-retry", markers=("control-flaky",))
        result = await asyncio.wait_for(ref.aio_result(), 120)
        from factory.workflows.contracts import RunAdvance

        self.assertEqual(RunAdvance.model_validate(result).state, "completed")
        executions = [
            json.loads(line)["execution"]
            for line in (folder / "effects.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            executions, ["control-retry-0", "control-retry-1", "control-retry-2"]
        )
        frontiers = [
            json.loads(line)["execution"]
            for line in (folder / "frontiers.jsonl").read_text().splitlines()
        ]
        self.assertEqual(frontiers, ["control-retry-1", "control-retry-1"])
