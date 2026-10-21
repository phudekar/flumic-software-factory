"""Optional bounded redacted history publisher. Independent of OpenTelemetry."""

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import asdict
from pathlib import Path
from functools import wraps
import inspect
import json
import logging
import os
import re
import threading
import time
import urllib.request
import uuid

from factory.application.telemetry import current_recorder, use_recorder
from factory.adapters.telemetry import current_metadata
from factory.adapters.pricing import Pricing
from factory.domain.safety import find_sensitive_categories

_engine = ContextVar("factory_engine_context", default={})
_parent_call = ContextVar("factory_history_parent_call", default=None)


def engine_context(function):
    def values(ctx):
        result = {}
        for name in ("workflow_run_id", "task_run_id"):
            try:
                value = getattr(ctx, name, None)
                value = value() if callable(value) else value
                if isinstance(value, str):
                    result["engine_" + name] = value
            except Exception:
                pass
        return result

    if inspect.iscoroutinefunction(function):

        @wraps(function)
        async def execute(input, ctx):
            token = _engine.set(values(ctx))
            try:
                return await function(input, ctx)
            finally:
                _engine.reset(token)
    else:

        @wraps(function)
        def execute(input, ctx):
            token = _engine.set(values(ctx))
            try:
                return function(input, ctx)
            finally:
                _engine.reset(token)

    return execute


def redact(value):
    if isinstance(value, dict):
        return {str(k): redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    for key, secret in os.environ.items():
        if (
            any(part in key for part in ("TOKEN", "PASSWORD", "SECRET", "API_KEY"))
            and len(secret) >= 8
        ):
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        value,
        flags=re.S,
    )
    value = re.sub(
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}", "Bearer [REDACTED]", value
    )
    value = "\n".join(
        "[REDACTED sensitive line]" if find_sensitive_categories(line) else line
        for line in value.splitlines()
    )
    raw = value.encode()
    return raw[:65536].decode(errors="ignore") + (
        "\n[TRUNCATED]" if len(raw) > 65536 else ""
    )


class Publisher:
    def __init__(self, settings):
        self.endpoint = (
            os.environ["FACTORY_CONTROL_ENDPOINT"].rstrip("/") + "/api/v1/ingest"
        )
        self.token = os.environ.get("FACTORY_CONTROL_TOKEN", "")
        self.worker_id = os.environ.get("FACTORY_CONTROL_WORKER_ID", "")
        self.root = Path(settings.state_path).parent / "control-spool" / self.worker_id
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.revision = settings.control_revision
        self.last_heartbeat = 0
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def emit(self, kind, payload, metadata):
        try:
            identifier = str(uuid.uuid4())
            event = {
                "id": identifier,
                "kind": kind,
                "occurred_at": time.time(),
                "run_id": metadata.get("factory_run_id"),
                "attempt_id": metadata.get("attempt_id"),
                "stage": metadata.get("stage"),
                "payload": redact({**_engine.get(), **payload}),
            }
            encoded = json.dumps(event)
            if len(encoded.encode()) > 500000:
                event["payload"].pop("conversation", None)
                event["payload"]["capture_status"] = "size_limit"
                encoded = json.dumps(event)
            path = self.root / (identifier + ".json")
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(encoded)
            # Bounded already-redacted disk spool; never retain unredacted content.
            files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime)
            for old in files[:-500]:
                old.unlink(missing_ok=True)
        except Exception:
            logging.getLogger("issue-worker.control").warning(
                "Control history event could not be queued"
            )

    def run(self):
        while not self.closed.wait(2):
            if time.time() - self.last_heartbeat > 15:
                self.emit("heartbeat", {"config_revision": self.revision}, {})
                self.last_heartbeat = time.time()
            for path in list(self.root.glob("*.json"))[:25]:
                try:
                    payload = "[" + path.read_text() + "]"
                    request = urllib.request.Request(
                        self.endpoint,
                        data=payload.encode(),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": "Bearer " + self.token,
                            "X-Worker-Id": self.worker_id,
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=3) as response:
                        if response.status == 202:
                            path.unlink(missing_ok=True)
                except Exception:
                    break

    def shutdown(self):
        self.closed.set()
        self.thread.join(timeout=4)


class CapturedCall:
    def __init__(self, delegate, observer, provider, model, agent, iteration):
        self.delegate, self.observer = delegate, observer
        self.payload = {
            "provider": provider,
            "model": model,
            "agent": agent,
            "iteration": iteration,
            "accounting_leaf": True,
            "call_id": str(uuid.uuid4()),
            "config_revision": observer.settings.control_revision,
            "pricing_scope": "Standard list-price estimate; unreported cache writes and tier multipliers may be missing.",
        }
        self.messages = {}

    def record(self, usage):
        if self.delegate:
            self.delegate.record(usage)
        self.payload.update(asdict(usage))
        estimate = self.observer.pricing.estimate(
            self.payload["provider"], self.payload["model"], usage
        )
        if self.payload["model"] == "gpt-6-luna" and (usage.input_tokens or 0) > 272000:
            estimate = None
            self.payload["pricing_scope"] = (
                "Unknown tier: CLI aggregate or long-context usage"
            )
        self.payload["estimated_usd"] = estimate
        self.payload["pricing_rates"] = self.observer.pricing.models.get(
            self.payload["provider"] + "/" + (self.payload["model"] or "")
        )

    def conversation(self, *, request=None, response=None, coverage="api_exchange"):
        if self.observer.settings.conversation_capture != "redacted":
            return
        self.messages["coverage"] = coverage
        if request is not None:
            self.messages["request"] = redact(request)
        if response is not None:
            self.messages["response"] = redact(response)


class Recorder:
    def __init__(self, observer, delegate):
        self.observer, self.delegate = observer, delegate

    def capture_trace(self):
        return self.delegate.capture_trace() if self.delegate else {}

    @contextmanager
    def model_call(self, provider, model, *, agent="api", iteration=None):
        context = (
            self.delegate.model_call(provider, model, agent=agent, iteration=iteration)
            if self.delegate
            else nullcontext()
        )
        started = time.monotonic()
        with context as underlying:
            call = CapturedCall(
                underlying, self.observer, provider, model, agent, iteration
            )
            parent = _parent_call.get()
            if parent is not None:
                parent.payload["accounting_leaf"] = False
                call.payload["parent_call_id"] = parent.payload["call_id"]
            parent_token = _parent_call.set(call)
            try:
                yield call
                call.payload["outcome"] = "completed"
            except BaseException as error:
                call.payload.update(outcome="failed", error_type=type(error).__name__)
                raise
            finally:
                _parent_call.reset(parent_token)
                call.payload["duration_seconds"] = time.monotonic() - started
                if call.messages:
                    call.payload["conversation"] = call.messages
                if self.observer.settings.conversation_capture != "disabled":
                    self.observer.publisher.emit(
                        "model.completed", call.payload, current_metadata()
                    )


class ControlLogHandler(logging.Handler):
    def __init__(self, publisher):
        super().__init__()
        self.publisher = publisher

    def emit(self, record):
        metadata = current_metadata()
        if (
            metadata.get("attempt_id")
            and record.name.startswith("issue-worker")
            and record.name != "issue-worker.control"
        ):
            self.publisher.emit(
                "log",
                {"message": redact(record.getMessage()), "level": record.levelname},
                metadata,
            )


class ControlObserver:
    def __init__(self, delegate, settings, publisher=None):
        self.delegate, self.settings = delegate, settings
        self.publisher = publisher or Publisher(settings)
        self.pricing = Pricing(settings.pricing_file or None)
        self.handler = ControlLogHandler(self.publisher)
        logging.getLogger().addHandler(self.handler)

    @contextmanager
    def observe(self, stage, attributes):
        with self.delegate.observe(stage, attributes):
            metadata = current_metadata()
            self.publisher.emit(
                "attempt.started",
                {"config_revision": self.settings.control_revision, **attributes},
                metadata,
            )
            started = time.monotonic()
            outcome = "completed"
            try:
                with use_recorder(Recorder(self, current_recorder())):
                    yield
            except BaseException:
                outcome = "failed"
                raise
            finally:
                self.publisher.emit(
                    "attempt.completed",
                    {
                        "outcome": outcome,
                        "duration_seconds": time.monotonic() - started,
                    },
                    metadata,
                )

    def shutdown(self):
        logging.getLogger().removeHandler(self.handler)
        self.publisher.shutdown()
        shutdown = getattr(self.delegate, "shutdown", None)
        if shutdown:
            shutdown()
