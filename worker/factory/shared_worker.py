"""Infrastructure-only shared worker. Legacy repository workflows stay separate."""

import json
import threading
from contextlib import contextmanager
import os
import uuid
import urllib.request
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from factory.workflows.contracts import ActionResult, StageReceipt

RENEW_SECONDS = 20

INSTALLED_ACTIONS = ("core.echo@1",)
INSTALLED_EXECUTORS = ("builtin@1",)


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
        except Exception:
            raise RuntimeError(
                "Shared worker request failed; inspect assignment metadata on the control plane"
            ) from None

    def register(self):
        settings = self.settings
        actions = sorted(set(INSTALLED_ACTIONS) & set(settings.actions))
        executors = sorted(set(INSTALLED_EXECUTORS) & set(settings.executors))
        if not actions or not executors:
            raise ValueError("Pool has no locally installed action/executor")
        return self.request(
            "/api/v1/shared-worker/register",
            {
                "protocol": settings.protocol,
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
        claim = self.request(
            "/api/v1/shared-worker/claim",
            {**invocation.model_dump(), "claim_key": str(uuid.uuid4())},
        )
        if claim.get("reused"):
            return StageReceipt(execution_id=invocation.execution_id, state="waiting")
        if claim["action"] != "core.echo@1":
            raise ValueError("Action is not installed in shared worker")
        # Deterministic smoke action only. Catalog execution/context heartbeat
        # integration arrives with #7/#9; no project clients are created here.
        with self.lease(claim["assignment_id"], claim["lease_token"]):
            result = ActionResult(output=claim["input"])
        receipt = self.request(
            "/api/v1/shared-worker/assignments/" + claim["assignment_id"] + "/complete",
            {
                "lease_token": claim["lease_token"],
                "result": result.model_dump(mode="json"),
            },
        )
        return StageReceipt.model_validate(receipt)


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
    env = os.environ if env is None else env
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
    from factory.adapters.orchestration.hatchet import create_client

    configure_logging(settings)
    control = SharedControl(settings, env)
    control.register()
    return build_shared_worker(create_client(settings), settings, control).start()
