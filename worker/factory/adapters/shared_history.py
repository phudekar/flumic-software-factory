"""Bounded private spool: authority is captured before the action context ends."""

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from factory.adapters.control import redact

MAX_EVENT = 500000


def event_payload(payload):
    # Protocol keys and locally generated correlation IDs are not source content.
    return {
        key: value if key in {"call_id", "parent_call_id"} else redact(value)
        for key, value in payload.items()
    }


class BoundPublisher:
    def __init__(self, publisher, context, proof):
        self.publisher = publisher
        self.assignment_id, self.proof = context.assignment_id, proof
        self.attribution = {
            key: getattr(context, key)
            for key in (
                "project_id",
                "repository_id",
                "workflow_revision_id",
                "run_id",
                "stage_id",
                "execution_id",
                "assignment_id",
                "attempt",
            )
        }
        if context.worker_id:
            self.attribution["worker_id"] = context.worker_id
        self.attribution.update(
            definition_id=getattr(context, "definition_id", ""),
            executor=getattr(context, "executor", "builtin@1"),
            job_id=getattr(context, "job_id", None),
        )

    def emit(self, kind, payload, metadata):
        self.publisher.enqueue(self, kind, payload)

    def shutdown(self):
        # The worker owns the shared spool and sender, not an individual action.
        pass


class SharedPublisher:
    def __init__(self, control, root, *, start=True):
        self.control = control
        self.root = Path(root) / control.settings.worker_id
        if self.root.is_symlink():
            raise ValueError("History spool must be a private directory")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_uid != os.getuid():
            os.chown(self.root, os.getuid(), os.getgid())
        self.root.chmod(0o700)
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.cursor = ""
        if start:
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def bound(self, context, proof):
        return BoundPublisher(self, context, proof)

    def enqueue(self, bound, kind, payload):
        try:
            event = {
                "id": str(uuid.uuid4()),
                "kind": kind,
                "occurred_at": time.time(),
                "run_id": bound.attribution["run_id"],
                "attempt_id": bound.attribution["execution_id"],
                "stage": bound.attribution["stage_id"],
                "attribution": dict(bound.attribution),
                "payload": event_payload(payload),
            }
            document = {"lease_token": bound.proof, "events": [event]}
            encoded = json.dumps(document, allow_nan=False)
            if len(encoded.encode()) > MAX_EVENT:
                event["payload"].pop("conversation", None)
                event["payload"]["capture_status"] = "size_limit"
                encoded = json.dumps(document, allow_nan=False)
            if len(encoded.encode()) > MAX_EVENT:
                raise ValueError("History event exceeds spool budget")
            with self.lock:
                files = sorted(
                    self.root.glob("*.json"), key=lambda p: p.stat().st_mtime
                )
                for old in files[:-499]:
                    old.unlink(missing_ok=True)
                path = self.root / (bound.assignment_id + "--" + event["id"] + ".json")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
        except Exception:
            logging.getLogger("issue-worker.control").warning(
                "Action history could not be queued"
            )

    def flush_once(self):
        files = sorted(self.root.glob("*.json"))
        start = next(
            (index for index, path in enumerate(files) if path.name > self.cursor), 0
        )
        for path in (files[start:] + files[:start])[:25]:
            if self.closed.is_set():
                break
            self.cursor = path.name
            try:
                if path.stat().st_size > MAX_EVENT:
                    raise ValueError("Invalid queued history size")
                document = json.loads(path.read_text())
                assignment = path.name.split("--", 1)[0]
                response = self.control.request(
                    "/api/v1/shared-worker/assignments/" + assignment + "/events",
                    document,
                )
                if response.get("accepted") == len(document["events"]):
                    path.unlink(missing_ok=True)
            except Exception:
                # Do not let one temporarily unavailable project starve others.
                continue

    def run(self):
        heartbeat = 0
        while not self.closed.wait(2):
            if time.time() - heartbeat >= 15:
                try:
                    self.control.request("/api/v1/shared-worker/heartbeat", {})
                    heartbeat = time.time()
                except Exception:
                    pass
            self.flush_once()

    def shutdown(self):
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=20)
