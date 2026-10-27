"""Durable operations; adapter calls happen outside the database claim transaction."""

import hmac
import time
import threading
from contextlib import contextmanager
from sqlalchemy import select, delete, exists, and_, or_
from sqlalchemy.orm import aliased
from app.ports import RuntimePending
from app.adapters.kubernetes_api import KubernetesError
from app.runtime_claims import (
    claim_runtime,
    require_runtime_claim,
    release_runtime_claim,
    runtime_heartbeat,
)
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
def operation_heartbeat(sessions, identifier, interval=20, *, claim=None):
    """Keep long image builds from being reclaimed as abandoned operations."""
    stopped = threading.Event()

    def renew():
        while not stopped.wait(interval):
            try:
                with sessions.begin() as db:
                    if claim is not None:
                        require_runtime_claim(sessions, claim, time.time(), db=db)
                    operation = db.get(Operation, identifier)
                    if operation is None or operation.status != "running":
                        return
                    operation.updated_at = time.time()
            except ValueError:
                return
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
        earlier = aliased(Operation)
        operation = db.scalar(
            select(Operation)
            .where(
                Operation.status == "pending",
                ~exists(
                    select(earlier.id).where(
                        earlier.worker_id == Operation.worker_id,
                        earlier.status.in_(("pending", "running")),
                        or_(
                            earlier.created_at < Operation.created_at,
                            and_(
                                earlier.created_at == Operation.created_at,
                                earlier.id < Operation.id,
                            ),
                        ),
                    )
                ),
            )
            .order_by(Operation.updated_at, Operation.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if operation is None:
            return False
        operation.status = "running"
        operation.updated_at = time.time()
        identifier = operation.id
        worker = db.get(Worker, operation.worker_id)
        if worker.protocol == "shared-v1":
            from app.worker_pools import pool_record, shared_profile

            document = shared_profile(worker, pool_record(db, worker.pool_id))
        else:
            document = db.get(Revision, worker.revision_id).document
        action = operation.action
        token = worker_token(settings, worker.id)
    claim = claim_runtime(sessions, "worker:" + worker.id, time.time())
    if claim is None:
        with sessions.begin() as db:
            operation = db.get(Operation, identifier)
            if operation.status == "running":
                operation.status, operation.updated_at = "pending", time.time()
        return False

    def invoke(method, *args):
        require_runtime_claim(sessions, claim, time.time())
        return getattr(selected, method)(*args)

    try:
        with (
            operation_heartbeat(sessions, identifier, claim=claim),
            runtime_heartbeat(sessions, claim),
        ):
            selected = (
                runtime.for_worker(worker, claim)
                if callable(getattr(type(runtime), "for_worker", None))
                else runtime
            )
            if worker.protocol == "legacy" and action in {
                "create",
                "configure",
                "configure_start",
                "start",
                "restart",
            }:
                from app.workflow_state import legacy_guard
                from app.services import Configurations

                with sessions() as db:
                    legacy_guard(
                        db, Configurations(settings.root), document, worker.profile
                    )
            if action in {"restart", "configure_start"}:
                if action == "restart":
                    invoke("action", worker.id, "stop")
                invoke("configure", worker, document, token)
                invoke("action", worker.id, "start")
            elif action == "configure":
                invoke("configure", worker, document, token)
            elif action == "create":
                invoke("create", worker, document, token)
            else:
                invoke("action", worker.id, action)
        status, error = "completed", None
    except RuntimePending:
        status, error = "pending", None
    except KubernetesError as exc:
        if exc.status is None or exc.status in {408, 409, 429} or exc.status >= 500:
            status, error = "pending", None
        else:
            status, error = "failed", "Kubernetes lifecycle request was refused"
    except Exception as exc:
        status = "failed"
        error = (
            str(exc)
            if isinstance(exc, ValueError)
            else f"{type(exc).__name__}: runtime unavailable; check server integration"
        )
    try:
        with sessions.begin() as db:
            require_runtime_claim(sessions, claim, time.time(), db=db)
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
    except ValueError:
        # Expired owners leave recovery to a fresh claimant.
        return False
    finally:
        release_runtime_claim(sessions, claim)
    return True


def reconcile_jobs(sessions, runtime, settings, stopped):
    """Independent loop: worker image builds never block action-job deadlines."""
    from app.job_broker import JobBroker

    broker = JobBroker(sessions, settings)
    while not stopped.is_set():
        try:
            broker.reconcile_once(runtime)
        except Exception:
            import logging

            logging.getLogger(__name__).error("Job reconciliation tick unavailable")
        stopped.wait(2)


def main():
    from app.settings import AppSettings
    from app.database import database
    from app.services import Configurations
    from app.adapters.docker import DockerRuntime

    settings = AppSettings.load()
    _, sessions = database(settings.database_url)
    runtime = DockerRuntime(settings, Configurations(settings.root))
    from app.adapters.docker_jobs import DockerJobRuntime
    import hashlib

    root = settings.snapshot_root / "jobs"
    host = settings.host_root / root.relative_to(settings.root)
    deployment = hashlib.sha256(
        f"{settings.compose_project_name}:{settings.host_root}".encode()
    ).hexdigest()[:24]
    jobs = DockerJobRuntime(root, host, deployment, bridge=settings.job_bridge)
    stopped = threading.Event()
    threading.Thread(
        target=reconcile_jobs, args=(sessions, jobs, settings, stopped), daemon=True
    ).start()
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
