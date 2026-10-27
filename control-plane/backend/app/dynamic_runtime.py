"""Trusted control worker and dispatch pump; shared action workers never get DB keys."""

import logging
import threading
from sqlalchemy import select, desc
from app.database import database, Revision, DEFAULT_PROJECT_ID
from app.settings import AppSettings
from app.services import Configurations
from app.workflow_runs import DynamicRuntime
from app.adapters.hatchet import HatchetEngine
from factory.adapters.orchestration.hatchet import create_client
from factory.adapters.orchestration.dynamic import (
    register_coordinator,
    register_project_waits,
)


def build_runtime_worker(client, runtime, settings):
    tasks = register_coordinator(
        client, runtime.advance, namespace=settings.runtime_namespace
    )
    waits = register_project_waits(
        client,
        runtime.check_wait,
        runtime.resume_wait,
        namespace=settings.runtime_namespace + "-waits",
    )
    return client.worker(
        settings.runtime_namespace + "-control",
        slots=2,
        durable_slots=16,
        labels={"factory_protocol": "shared-v1", "factory_role": "control"},
        workflows=[
            tasks.coordinator,
            tasks.advance,
            waits.wait,
            waits.check,
            waits.resume,
        ],
    )


def pump(runtime, engine, stopped):
    while not stopped.is_set():
        for operation in (
            runtime.reconcile_background,
            runtime.reconcile_followups,
            lambda: runtime.reconcile_outbox(engine),
        ):
            try:
                operation()
            except Exception:
                logging.getLogger(__name__).error(
                    "Dynamic runtime reconciliation unavailable; durable state retained"
                )
        stopped.wait(2)


def main():
    settings = AppSettings.load()
    _, sessions = database(settings.database_url)
    config = Configurations(settings.root)
    with sessions() as db:
        revision = db.scalar(
            select(Revision)
            .where(
                Revision.project_id == DEFAULT_PROJECT_ID, Revision.scope == "factory"
            )
            .order_by(desc(Revision.created_at))
            .limit(1)
        )
        if revision is None:
            raise RuntimeError(
                "Start the API and initialize the saved factory configuration first"
            )
        transport_settings = config.resolve(revision.document, "simple")
    client = create_client(transport_settings)
    runtime = DynamicRuntime(sessions, settings, config)
    engine = HatchetEngine(
        transport_settings, client, runtime_namespace=settings.runtime_namespace
    )
    stopped = threading.Event()
    thread = threading.Thread(target=pump, args=(runtime, engine, stopped), daemon=True)
    thread.start()
    from app.trigger_runtime import TriggerRuntime

    triggers = TriggerRuntime(sessions, settings, config)
    trigger_thread = threading.Thread(
        target=triggers.poll, args=(stopped,), daemon=True
    )
    trigger_thread.start()
    try:
        build_runtime_worker(client, runtime, settings).start()
    finally:
        stopped.set()
        thread.join(timeout=5)
        trigger_thread.join(timeout=5)


if __name__ == "__main__":
    main()
