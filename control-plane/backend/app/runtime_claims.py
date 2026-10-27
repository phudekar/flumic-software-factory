"""Short-transaction runtime leases shared by independent controller processes."""

from contextlib import contextmanager
from dataclasses import dataclass
import math
import re
import threading
import time
import uuid

from sqlalchemy import update
from app.database import RuntimeLease

LEASE_SECONDS = 60
RENEW_SECONDS = 15
_KEY = re.compile(r"^(?:job:[a-f0-9]{64}|worker:[a-f0-9-]{36})$")


@dataclass(frozen=True)
class RuntimeClaim:
    resource_key: str
    owner: str
    epoch: int
    expires_at: float


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Invalid runtime lease clock")


def claim_runtime(sessions, resource_key, now):
    _time(now)
    if not isinstance(resource_key, str) or not _KEY.fullmatch(resource_key):
        raise ValueError("Invalid runtime resource key")
    owner = str(uuid.uuid4())
    with sessions.begin() as db:
        if db.bind.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif db.bind.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            raise RuntimeError("Runtime claims require PostgreSQL or SQLite")
        statement = insert(RuntimeLease).values(
            resource_key=resource_key,
            owner=owner,
            epoch=1,
            expires_at=now + LEASE_SECONDS,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[RuntimeLease.resource_key],
            set_={
                "owner": owner,
                "epoch": RuntimeLease.epoch + 1,
                "expires_at": now + LEASE_SECONDS,
            },
            where=RuntimeLease.expires_at <= now,
        ).returning(RuntimeLease.epoch)
        epoch = db.scalar(statement)
    return (
        RuntimeClaim(resource_key, owner, epoch, now + LEASE_SECONDS)
        if epoch is not None
        else None
    )


def require_runtime_claim(sessions, claim, now, *, db=None):
    _time(now)
    if db is None:
        with sessions.begin() as connection:
            require_runtime_claim(sessions, claim, now, db=connection)
        return
    # A conditional no-op UPDATE holds the authority row through the dependent
    # canonical write on PostgreSQL and SQLite (where FOR UPDATE is ignored).
    result = db.execute(
        update(RuntimeLease)
        .where(
            RuntimeLease.resource_key == claim.resource_key,
            RuntimeLease.owner == claim.owner,
            RuntimeLease.epoch == claim.epoch,
            RuntimeLease.expires_at > now,
        )
        .values(expires_at=RuntimeLease.expires_at)
    )
    if result.rowcount != 1:
        raise ValueError("Runtime reconciliation claim is no longer active")


def renew_runtime_claim(sessions, claim, now):
    _time(now)
    with sessions.begin() as db:
        result = db.execute(
            update(RuntimeLease)
            .where(
                RuntimeLease.resource_key == claim.resource_key,
                RuntimeLease.owner == claim.owner,
                RuntimeLease.epoch == claim.epoch,
                RuntimeLease.expires_at > now,
            )
            .values(expires_at=now + LEASE_SECONDS)
        )
        return result.rowcount == 1


def release_runtime_claim(sessions, claim):
    with sessions.begin() as db:
        db.execute(
            update(RuntimeLease)
            .where(
                RuntimeLease.resource_key == claim.resource_key,
                RuntimeLease.owner == claim.owner,
                RuntimeLease.epoch == claim.epoch,
            )
            .values(owner=None, expires_at=0)
        )


@contextmanager
def runtime_heartbeat(sessions, claim, *, clock=time.time):
    stopped = threading.Event()

    def renew():
        while not stopped.wait(RENEW_SECONDS):
            try:
                if not renew_runtime_claim(sessions, claim, clock()):
                    return
            except Exception:
                # Failure does not prolong a lease. Every effect and commit must
                # separately recheck the fence; expired owners cannot resurrect.
                return

    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=5)
