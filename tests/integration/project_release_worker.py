"""Test-only worker entry points; fixed fake GitHub/model integrations only."""

import json
import os
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "worker"),
    str(ROOT / "control-plane/backend"),
    str(ROOT / "control-plane/backend/tests"),
]


def main():
    from hatchet_sdk import Hatchet
    from app.settings import AppSettings
    from app.database import database
    from app.services import Configurations
    from app.workflow_runs import DynamicRuntime
    from app.dynamic_runtime import build_runtime_worker, pump
    from app.adapters.hatchet import HatchetEngine
    from factory.shared_worker import SharedSettings, start_shared

    role, config_path = sys.argv[1:]
    document = json.loads(Path(config_path).read_text())
    client = Hatchet()
    if role == "control":
        for field in ("root", "snapshot_root", "host_root", "frontend"):
            document["settings"][field] = Path(document["settings"][field])
        settings = AppSettings(**document["settings"])
        _, sessions = database(document["database_url"])
        config = Configurations(settings.root)
        runtime = DynamicRuntime(sessions, settings, config)
        from app.job_broker import JobBroker

        runtime.broker = JobBroker(sessions, settings, root=Path(document["job_root"]))
        transport = config.resolve(config.approved, "simple")
        engine = HatchetEngine(
            transport, client, runtime_namespace=settings.runtime_namespace
        )
        stopped = threading.Event()
        thread = threading.Thread(
            target=pump, args=(runtime, engine, stopped), daemon=True
        )
        thread.start()
        try:
            build_runtime_worker(client, runtime, settings).start()
        finally:
            stopped.set()
            thread.join(timeout=5)
    else:
        assert not any(
            os.environ.get(key)
            for key in (
                "GITHUB_TOKEN",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "CONTROL_ENCRYPTION_KEY",
                "CONTROL_DATABASE_URL",
                "CONTROL_TEST_DATABASE_URL",
                "CONTROL_MIGRATION_TEST_URL",
            )
        )
        assert set(document) == {"settings", "endpoint", "history_root"}
        import factory.bootstrap as bootstrap
        import factory.adapters.control as control_module
        from factory.adapters.pricing import Pricing
        from project_release_fixture import fake_builtin

        bootstrap.run_builtin = fake_builtin
        control_module.Pricing = lambda path: (
            Pricing()
        )  # Synthetic models have no real pricing.
        settings = SharedSettings(**document["settings"])
        start_shared(
            {
                **os.environ,
                "FACTORY_CONTROL_ENDPOINT": document["endpoint"],
                "FACTORY_CONTROL_WORKER_ID": settings.worker_id,
                "FACTORY_WORKER_POOL_ID": settings.pool_id,
                "FACTORY_SHARED_HISTORY_ROOT": document["history_root"],
            }
        )


if __name__ == "__main__":
    main()
