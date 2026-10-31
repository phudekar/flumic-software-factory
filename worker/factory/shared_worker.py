"""Infrastructure-only shared worker. Legacy repository workflows stay separate."""

import json
import threading
from contextlib import contextmanager
import os
import uuid
import urllib.request
import urllib.error
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from factory.workflows.contracts import ActionResult, StageReceipt
from factory.workflows.action_schemas import CATALOG, READ_ONLY_FAILURE_ACTIONS

RENEW_SECONDS = 20

INSTALLED_ACTIONS = (*CATALOG, "docker.run@1")
INSTALLED_EXECUTORS = ("builtin@1", "docker@1")


class SharedRequestError(RuntimeError):
    def __init__(self, status=None):
        self.status = status
        super().__init__(
            "Shared worker request failed; inspect assignment metadata on the control plane"
        )


class SharedSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    protocol: Literal["shared-v1"] = "shared-v1"
    pool_id: str
    worker_id: str
    worker_name: str
    role: Literal["simple", "developer"]
    normal_slots: int = Field(default=2, ge=1, le=8)
    durable_slots: int = Field(default=16, ge=1, le=16)
    executors: list[str] = Field(default_factory=lambda: ["builtin@1"])
    actions: list[str] = Field(default_factory=lambda: ["core.echo@1"])
    hatchet_tls_strategy: Literal["tls", "none", "mtls"] = "tls"
    log_level: str = "INFO"
    log_format: str = "json"


class SharedControl:
    def __init__(self, settings, env):
        self.settings = settings
        self.endpoint = env["FACTORY_CONTROL_ENDPOINT"].rstrip("/")
        self.headers = {
            "X-Worker-Id": settings.worker_id,
            "Authorization": "Bearer " + env["FACTORY_CONTROL_TOKEN"],
            "Content-Type": "application/json",
        }
        from factory.adapters.otel import WorkerTelemetry

        self.infrastructure = WorkerTelemetry(
            settings,
            endpoint=env.get("FACTORY_SHARED_TELEMETRY_ENDPOINT", ""),
            environment=env.get("FACTORY_SHARED_DEPLOYMENT_ENVIRONMENT", "development"),
        )
        self.history = None
        if env.get("FACTORY_SHARED_HISTORY_ROOT"):
            from pathlib import Path
            from factory.adapters.shared_history import SharedPublisher

            self.history = SharedPublisher(
                self, Path(env["FACTORY_SHARED_HISTORY_ROOT"])
            )

    def request(self, path, body=None):
        request = urllib.request.Request(
            self.endpoint + path,
            data=json.dumps(body, allow_nan=False).encode()
            if body is not None
            else None,
            headers=self.headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read(2 * 1048576 + 1)
            if len(raw) > 2 * 1048576:
                raise ValueError("Worker response exceeds size limit")
            return json.loads(raw)
        except urllib.error.HTTPError as error:
            raise SharedRequestError(error.code) from None
        except Exception:
            raise SharedRequestError() from None

    @contextmanager
    def observation(self, context, proof):
        from factory.bootstrap import load_factory
        from factory.application.telemetry import action_scope, incoming_trace

        from dataclasses import replace

        settings = replace(context.settings, worker_name=self.settings.worker_name)
        with (
            self.infrastructure.track(context.execution_id),
            action_scope(context),
            incoming_trace(context.trace_context),
        ):
            delegate = load_factory(settings.observer_factory)(settings)
            attach = getattr(delegate, "attach_infrastructure", None)
            if attach:
                attach(self.infrastructure)
            if getattr(delegate, "log_handler", None) is not None:
                delegate.log_handler.assignment_id = context.assignment_id
            observer = delegate
            if self.history is not None:
                from factory.adapters.control import ControlObserver

                observer = ControlObserver(
                    delegate,
                    settings,
                    publisher=self.history.bound(context, proof),
                    assignment_id=context.assignment_id,
                )
            try:
                yield observer
            finally:
                shutdown = getattr(observer, "shutdown", None)
                if shutdown:
                    shutdown()

    def shutdown(self):
        try:
            if self.history:
                self.history.shutdown()
        finally:
            self.infrastructure.shutdown()

    def register(self):
        import shutil

        settings = self.settings
        actions = sorted(
            action
            for action in set(INSTALLED_ACTIONS) & set(settings.actions)
            if action == "docker.run@1" or CATALOG[action].role in {None, settings.role}
        )
        executors = sorted(set(INSTALLED_EXECUTORS) & set(settings.executors))
        if not actions or not executors:
            raise ValueError("Pool has no locally installed action/executor")
        return self.request(
            "/api/v1/shared-worker/register",
            {
                "protocol": settings.protocol,
                "providers": ["github", "gitlab"]
                if shutil.which("git")
                else ["github"],
                "pool_id": settings.pool_id,
                "role": settings.role,
                "normal_slots": settings.normal_slots,
                "durable_slots": settings.durable_slots,
                "actions": actions,
                "executors": executors,
            },
        )

    @contextmanager
    def lease(self, identifier, proof):
        stopped = threading.Event()
        lost = threading.Event()

        def renew():
            while not stopped.wait(RENEW_SECONDS):
                try:
                    self.request(
                        "/api/v1/shared-worker/assignments/" + identifier + "/renew",
                        {"lease_token": proof},
                    )
                except Exception:
                    lost.set()
                    return

        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=20)
        if lost.is_set() or thread.is_alive():
            raise RuntimeError(
                "Assignment lease could not be renewed; reconcile before retry"
            )

    def execute(self, invocation):
        with self.infrastructure.track(invocation.execution_id):
            return self._execute(invocation)

    def _execute(self, invocation):
        claim = self.request(
            "/api/v1/shared-worker/claim",
            {**invocation.model_dump(), "claim_key": str(uuid.uuid4())},
        )
        if claim.get("reused"):
            return StageReceipt(execution_id=invocation.execution_id, state="waiting")
        if (
            claim["action"] not in INSTALLED_ACTIONS
            or claim["action"] not in self.settings.actions
        ):
            raise ValueError("Action is not installed in shared worker")
        identifier, proof = claim["assignment_id"], claim["lease_token"]
        with self.lease(identifier, proof):
            failed = False
            if claim.get("executor") == "docker@1":
                self.request(
                    "/api/v1/shared-worker/assignments/" + identifier + "/jobs",
                    {"lease_token": proof, "spec_digest": claim["spec_digest"]},
                )
                # Durable acceptance is not terminal proof. The trusted controller
                # settles the stage from the broker receipt even if this worker dies.
                return StageReceipt(
                    execution_id=invocation.execution_id, state="waiting"
                )
            if claim["action"] == "core.echo@1":
                result = ActionResult(output=claim["input"])
            else:
                from factory.workflows.contracts import ActionContext
                from factory.workflows.bindings import ResolvedInputs, BindingOrigin
                from factory.bootstrap import run_builtin

                delivered = self.request(
                    "/api/v1/shared-worker/assignments/" + identifier + "/context",
                    {"lease_token": proof},
                )
                context = ActionContext.model_validate_json(
                    json.dumps(delivered["context"])
                ).with_credentials(delivered["credentials"])
                if (
                    context.project_id,
                    context.run_id,
                    context.execution_id,
                    context.assignment_id,
                    context.settings.worker_type,
                ) != (
                    invocation.project_id,
                    invocation.run_id,
                    invocation.execution_id,
                    identifier,
                    self.settings.role,
                ):
                    raise ValueError("Delivered context does not match invocation")
                if context.worker_id and context.worker_id != self.settings.worker_id:
                    raise ValueError("Delivered context does not match assigned worker")
                origins = tuple(
                    BindingOrigin(**{**value, "path": tuple(value["path"])})
                    for value in delivered["origins"]
                )
                with self.observation(context, proof) as observer:
                    try:
                        result = run_builtin(
                            claim["action"],
                            context,
                            ResolvedInputs(claim["input"], origins),
                            BoundTransport(self, identifier, proof),
                            observer=observer,
                        )
                    except Exception:
                        if claim["action"] not in READ_ONLY_FAILURE_ACTIONS:
                            raise
                        # Fixed policy proves no external mutation.
                        failed = True
                        result = ActionResult(
                            output={"error": "Read-only action failed"}
                        )
            receipt = self.request(
                "/api/v1/shared-worker/assignments/" + identifier + "/complete",
                {
                    "lease_token": proof,
                    "result": result.model_dump(mode="json"),
                    **({"failed": True} if failed else {}),
                },
            )
            return StageReceipt.model_validate(receipt)


class BoundTransport:
    """Only proof and operation data cross HTTP; context labels never grant scope."""

    def __init__(self, control, identifier, proof):
        self.control, self.identifier, self.proof = control, identifier, proof

    def call(self, path, **body):
        return self.control.request(
            "/api/v1/shared-worker/assignments/" + self.identifier + "/" + path,
            {"lease_token": self.proof, **body},
        )

    def get_state(self, context, key):
        return self.call("state", operation="get", key=key)["value"]

    def put_state(self, context, key, value):
        self.call("state", operation="put", key=key, value=value)

    def delete_state(self, context, key):
        self.call("state", operation="delete", key=key)

    def claim_resource(self, context, resource):
        try:
            self.call("resource", resource=resource)
        except SharedRequestError as error:
            if error.status == 409:
                from factory.domain.models import OperationBusy

                raise OperationBusy(
                    "Work item is held or lease is unavailable"
                ) from None
            raise

    def submit_followup(self, context, request):
        value = self.call("followups", request=request.model_dump(mode="json"))
        if value.get("accepted") is not True:
            raise ValueError("Follow-up was not durably accepted")
        return True


def build_shared_worker(hatchet, settings, control):
    labels = {
        "factory_protocol": "shared-v1",
        "factory_pool": settings.pool_id,
        "factory_role": settings.role,
    }

    from factory.adapters.orchestration.dynamic import register_action

    action = register_action(hatchet, settings.pool_id, settings.role, control.execute)

    return hatchet.worker(
        settings.worker_name,
        slots=settings.normal_slots,
        durable_slots=settings.durable_slots,
        labels=labels,
        workflows=[action],
    )


def start_shared(env=None):
    env = dict(os.environ if env is None else env)
    env.setdefault("FACTORY_SHARED_HISTORY_ROOT", "/data/control-spool")
    worker_id = env.get("FACTORY_CONTROL_WORKER_ID", "")
    request = urllib.request.Request(
        env["FACTORY_CONTROL_ENDPOINT"].rstrip("/") + "/api/v1/worker-bootstrap",
        headers={
            "X-Worker-Id": worker_id,
            "Authorization": "Bearer " + env.get("FACTORY_CONTROL_TOKEN", ""),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError("Pool profile exceeds size limit")
        settings = SharedSettings.model_validate_json(raw)
        if settings.worker_id != worker_id or settings.pool_id != env.get(
            "FACTORY_WORKER_POOL_ID"
        ):
            raise ValueError("Worker identity does not match pool profile")
    except Exception:
        raise RuntimeError(
            "Could not load shared infrastructure profile; retry startup"
        ) from None
    from factory.adapters.telemetry import configure_logging
    from factory.adapters.hatchet_client import create_client

    configure_logging(settings)
    control = SharedControl(settings, env)
    try:
        control.register()
        return build_shared_worker(create_client(settings), settings, control).start()
    finally:
        control.shutdown()
