"""Durable operations; adapter calls happen outside the database claim transaction."""

import hmac
import time
import threading
from contextlib import contextmanager
from sqlalchemy import select, delete
from app.database import (
    Operation,
    Worker,
    Revision,
    Conversation,
    Session,
    Event,
    Audit,
)


def worker_token(settings, worker_id):
    return hmac.new(
        settings.encryption_key.encode(), worker_id.encode(), "sha256"
    ).hexdigest()


@contextmanager
def operation_heartbeat(sessions, identifier, interval=20):
    """Keep long image builds from being reclaimed as abandoned operations."""
    stopped = threading.Event()

    def renew():
        while not stopped.wait(interval):
            try:
                with sessions.begin() as db:
                    operation = db.get(Operation, identifier)
                    if operation is None or operation.status != "running":
                        return
                    operation.updated_at = time.time()
            except Exception:
                # A later tick retries; the durable stale lease still provides
                # crash recovery when the process itself disappears.
                import logging

                logging.getLogger(__name__).exception("Lifecycle lease renewal failed")

    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=5)


def reconcile_once(sessions, runtime, settings):
    with sessions.begin() as db:
        # Resume an interrupted operation after a bounded lease. Docker create is idempotent.
        stale = db.scalars(
            select(Operation).where(
                Operation.status == "running", Operation.updated_at < time.time() - 120
            )
        ).all()
        for row in stale:
            row.status = "pending"
        operation = db.scalar(
            select(Operation)
            .where(Operation.status == "pending")
            .order_by(Operation.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if operation is None:
            return False
        operation.status = "running"
        operation.updated_at = time.time()
        identifier = operation.id
        worker = db.get(Worker, operation.worker_id)
        if worker.protocol == 'shared-v1':
            from app.worker_pools import pool_record, shared_profile
            document = shared_profile(worker, pool_record(db, worker.pool_id))
        else:
            document = db.get(Revision, worker.revision_id).document
        action = operation.action
        token = worker_token(settings, worker.id)
    try:
        with operation_heartbeat(sessions, identifier):
            if action in {"restart", "configure_start"}:
                if action == "restart":
                    runtime.action(worker.id, "stop")
                runtime.configure(worker, document, token)
                runtime.action(worker.id, "start")
            elif action == "configure":
                runtime.configure(worker, document, token)
            elif action == "create":
                runtime.create(worker, document, token)
            else:
                runtime.action(worker.id, action)
        status, error = "completed", None
    except Exception as exc:
        status = "failed"
        error = (
            str(exc)
            if isinstance(exc, ValueError)
            else f"{type(exc).__name__}: runtime unavailable; check server integration"
        )
    with sessions.begin() as db:
        operation = db.get(Operation, identifier)
        operation.status, operation.error, operation.updated_at = (
            status,
            error,
            time.time(),
        )
        if status == "completed":
            worker = db.get(Worker, operation.worker_id)
            worker.desired_state = {
                "create": "created",
                "configure": "created",
                "start": "running",
                "restart": "running",
                "configure_start": "running",
                "stop": "stopped",
                "remove": "removed",
            }[action]
    return True


def main():
    from app.settings import AppSettings
    from app.database import database
    from app.services import Configurations
    from app.adapters.docker import DockerRuntime

    settings = AppSettings.load()
    _, sessions = database(settings.database_url)
    runtime = DockerRuntime(settings, Configurations(settings.root))
    while True:
        reconcile_once(sessions, runtime, settings)
        with sessions.begin() as db:
            db.execute(
                delete(Conversation).where(Conversation.expires_at < time.time())
            )
            db.execute(
                delete(Event).where(Event.received_at < time.time() - 90 * 86400)
            )
            db.execute(
                delete(Audit).where(Audit.created_at < time.time() - 180 * 86400)
            )
            db.execute(delete(Session).where(Session.expires_at < time.time()))
        time.sleep(2)


if __name__ == "__main__":
    main()
