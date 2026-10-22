"""Single-host durable state. Shared multi-host deployments need another adapter."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path


from factory.domain.models import OperationBusy  # compatibility re-export


class SQLiteStateStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_directory = self.path.parent / (self.path.name + ".locks")
        self.lock_directory.mkdir(exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    attempt_id TEXT PRIMARY KEY, resource TEXT NOT NULL,
                    operation TEXT NOT NULL, status TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    finished_at TEXT, error_type TEXT
                );
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key):
        with self._connect() as db:
            row = db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        encoded = json.dumps(value, ensure_ascii=True)
        with self._connect() as db:
            db.execute(
                "INSERT INTO state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, encoded),
            )

    def delete(self, key):
        with self._connect() as db:
            db.execute("DELETE FROM state WHERE key=?", (key,))

    @contextmanager
    def claim(self, resource, operation):
        # Kernel locks release on process exit, including container termination.
        # Hold no database transaction across network/agent operations.
        lock_name = hashlib.sha256(resource.encode()).hexdigest()
        with (self.lock_directory / lock_name).open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise OperationBusy("Another execution owns this work item") from None
            attempt = str(uuid.uuid4())
            try:
                with self._connect() as db:
                    db.execute(
                        "UPDATE operations SET status='interrupted', finished_at=CURRENT_TIMESTAMP WHERE resource=? AND status='running'",
                        (resource,),
                    )
                    db.execute(
                        "INSERT INTO operations(attempt_id, resource, operation, status) VALUES (?, ?, ?, 'running')",
                        (attempt, resource, operation),
                    )
                try:
                    yield
                except BaseException as error:
                    with self._connect() as db:
                        db.execute(
                            "UPDATE operations SET status='failed', finished_at=CURRENT_TIMESTAMP, error_type=? WHERE attempt_id=?",
                            (type(error).__name__, attempt),
                        )
                    raise
                else:
                    with self._connect() as db:
                        db.execute(
                            "UPDATE operations SET status='completed', finished_at=CURRENT_TIMESTAMP WHERE attempt_id=?",
                            (attempt,),
                        )
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class MemoryStateStore:
    """For tests and embedded use; no persistence across restarts."""

    def __init__(self):
        self.values = {}
        self.active = set()
        self.mutex = threading.Lock()

    def get(self, key):
        value = self.values.get(key)
        return json.loads(json.dumps(value)) if value is not None else None

    def put(self, key, value):
        self.values[key] = json.loads(json.dumps(value))

    def delete(self, key):
        self.values.pop(key, None)

    @contextmanager
    def claim(self, resource, operation):
        with self.mutex:
            if resource in self.active:
                raise OperationBusy("Another execution owns this work item")
            self.active.add(resource)
        try:
            yield
        finally:
            with self.mutex:
                self.active.remove(resource)


def sqlite_state(settings):
    return SQLiteStateStore(settings.state_path)
