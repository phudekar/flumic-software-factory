"""Echo-only live worker. All file effects are isolated to the test directory."""

import json
import os
import sys
import time
from pathlib import Path
from datetime import timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker"))
from hatchet_sdk import Hatchet
from hatchet_sdk.runnables.eviction import EvictionPolicy
from factory.adapters.orchestration.dynamic import register_coordinator, register_action
from factory.adapters.orchestration import dynamic
from factory.workflows.coordinator import coordinate as real_coordinate
from factory.workflows.contracts import (
    RunAdvance,
    ActionInvocation,
    ActionRoute,
    StageReceipt,
)


def register(client, root, namespace):
    async def observed_coordinate(command, *args, **kwargs):
        with (root / command.run_id / "entries.jsonl").open("a") as out:
            out.write(json.dumps({"pid": os.getpid(), "time": time.time()}) + "\n")
        return await real_coordinate(command, *args, **kwargs)

    dynamic.coordinate = observed_coordinate

    def advance(command):
        folder = root / command.run_id
        if (folder / "cancel").exists():
            return RunAdvance(state="cancelled")
        for index in range(3):
            if not (folder / f"done-{index}").exists():
                retry = (folder / f"failed-{index}").exists()
                if retry and not (folder / "retry").exists():
                    return RunAdvance(state="failed")
                key = (
                    f"{command.run_id}-retry-{index}"
                    if retry
                    else f"{command.run_id}-{index}"
                )
                return RunAdvance(
                    state="running",
                    ready=[
                        ActionInvocation(
                            project_id=command.project_id,
                            run_id=command.run_id,
                            execution_id=key,
                        )
                    ],
                    routes={
                        key: ActionRoute(
                            stage_id=f"echo-{index}",
                            attempt=2 if retry else 1,
                            pool_id=namespace,
                            role="simple",
                        )
                    },
                )
        return RunAdvance(state="completed")

    def echo(command):
        folder = root / command.run_id
        index = command.execution_id.rsplit("-", 1)[-1]
        with (folder / "effects.jsonl").open("a") as out:
            out.write(
                json.dumps(
                    {
                        "execution": command.execution_id,
                        "pid": os.getpid(),
                        "start": time.time(),
                    }
                )
                + "\n"
            )
        (folder / f"start-{index}").touch()
        if index == "1":
            time.sleep(8)
        if index == "1" and (
            (folder / "fail").exists()
            or ((folder / "retry").exists() and "-retry-" not in command.execution_id)
        ):
            (folder / f"failed-{index}").touch()
            raise RuntimeError("Deterministic echo failure")
        (folder / f"done-{index}").write_text(str(time.time()))
        return StageReceipt(execution_id=command.execution_id, state="completed")

    tasks = register_coordinator(
        client,
        advance,
        namespace=namespace,
        poll_seconds=1,
        eviction_policy=EvictionPolicy(
            ttl=timedelta(seconds=1), allow_capacity_eviction=True, priority=0
        ),
    )
    action = register_action(client, namespace, "simple", echo)
    return tasks, action


if __name__ == "__main__":
    root = Path(sys.argv[1])
    namespace = sys.argv[2]
    client = Hatchet()
    tasks, action = register(client, root, namespace)
    client.worker(
        namespace + "-control",
        slots=2,
        durable_slots=1,
        labels={"factory_protocol": "shared-v1", "factory_role": "control"},
        workflows=[tasks.coordinator, tasks.advance],
    ).start() if sys.argv[3] == "control" else client.worker(
        namespace + "-action",
        slots=1,
        durable_slots=1,
        labels={
            "factory_protocol": "shared-v1",
            "factory_pool": namespace
            if sys.argv[3] != "decoy"
            else namespace + "-wrong",
            "factory_role": "simple",
        },
        workflows=[action],
    ).start()
