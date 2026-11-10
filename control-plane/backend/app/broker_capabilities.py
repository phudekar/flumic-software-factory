"""Trusted broker ledger. Callers own the transaction and commit before dispatch."""
from dataclasses import dataclass
from hashlib import sha256
import json
import re
import secrets
import time
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import case, func, select, update

from app.database import Assignment, BrokerCapability, BrokerReservation, StageAttempt, Worker, WorkflowRun
from app.execution_admission import read_execution_pin
from app.projects import ProjectScope
from app.workflow_definitions import read_definition_pin
from app.worker_pools import assignment_deadline, eligibility
from factory.domain.broker import CapabilityScope, MAX_ALLOWANCE, SessionLimits


class CapabilityDenied(Exception):
    """Authorization or allowance denied without exposing bearer or request data."""


@dataclass(frozen=True)
class Reservation:
    id: str
    scope: CapabilityScope
    request_id: str
    request_digest: str
    operation: str
    input_tokens: int
    output_tokens: int
    state: str


def _lock(db):
    from app.workflow_state import transition_lock

    transition_lock(db)


def _hash(bearer):
    return sha256(bearer.encode("ascii")).hexdigest()


def _active_scope(db, scope: CapabilityScope, settings):
    assignment = db.get(Assignment, scope.assignment_id, populate_existing=True)
    if (
        assignment is None or assignment.status != "running" or assignment.broker_revoked
        or assignment.worker_id is None or assignment.started_at is None
        or assignment.expires_at is None
    ):
        raise CapabilityDenied("Assignment is inactive")
    attempt = db.get(StageAttempt, assignment.execution_id, populate_existing=True)
    run = db.get(WorkflowRun, assignment.run_id, populate_existing=True)
    if (
        attempt is None or run is None or attempt.state != "running"
        or run.state != "running" or run.execution_mode != "protected"
        or attempt.assignment_id != assignment.id or attempt.stage_id != assignment.stage_id
        or attempt.run_id != run.id
        or not (attempt.project_id == run.project_id == assignment.project_id)
    ):
        raise CapabilityDenied("Assignment is outside an active protected attempt")
    worker = db.get(Worker, assignment.worker_id, populate_existing=True, with_for_update=True)
    if worker is None:
        raise CapabilityDenied("Assignment worker is inactive")
    try:
        eligibility(db, worker, assignment, settings)
    except HTTPException:
        raise CapabilityDenied("Assignment worker is inactive") from None
    launch = read_definition_pin(db, ProjectScope(run.project_id), run.snapshot_id, settings)
    if read_execution_pin(launch).mode != "protected":
        raise CapabilityDenied("Launch is outside an active protected attempt")
    deadline = assignment_deadline(db, assignment, settings)
    expiry = min(assignment.expires_at, deadline) if deadline is not None else assignment.expires_at
    now = time.time()
    if not now < expiry:
        raise CapabilityDenied("Assignment authority has expired")
    if (scope.project_id, scope.run_id, scope.stage_id, scope.attempt_id) != (
        assignment.project_id, run.id, attempt.stage_id, attempt.id
    ):
        raise CapabilityDenied("Capability scope does not match the assignment")
    return now, expiry


def issue_capability(db, scope: CapabilityScope, limits: SessionLimits, *, settings) -> str:
    """Trusted supervisor only. Rotate the bearer without replacing its allowance."""
    if scope.audience.startswith("mcp:") and any(
        operation.startswith("model.") for operation in scope.operations
    ):
        raise CapabilityDenied("MCP model delegation requires shared model allowance accounting")
    _lock(db)
    now, lease_expiry = _active_scope(db, scope, settings)
    capability = db.scalar(select(BrokerCapability).where(
        BrokerCapability.assignment_id == scope.assignment_id,
        BrokerCapability.audience == scope.audience,
    ).execution_options(populate_existing=True))
    policy = limits.model_dump()
    identity = scope.model_dump(mode="json")
    bearer = secrets.token_urlsafe(32)
    if capability is None:
        capability = BrokerCapability(project_id=scope.project_id, assignment_id=scope.assignment_id,
                                      audience=scope.audience, limits=policy, scope=identity)
        db.add(capability)
    elif capability.revoked or capability.limits != policy or capability.scope != identity:
        raise CapabilityDenied("Capability was revoked or its allowance changed")
    capability.bearer_hash = _hash(bearer)
    capability.expires_at = min(now + limits.ttl_seconds, lease_expiry)
    db.flush()
    return bearer


def _authenticate(db, bearer, audience, settings):
    if type(bearer) is not str or len(bearer) != 43 or not bearer.isascii():
        raise CapabilityDenied("Invalid broker capability")
    lookup = (
        BrokerCapability.bearer_hash == _hash(bearer),
        BrokerCapability.audience == audience,
        BrokerCapability.revoked.is_(False),
    )
    if db.scalar(select(BrokerCapability.id).where(*lookup)) is None:
        raise CapabilityDenied("Invalid broker capability")
    _lock(db)
    capability = db.scalar(select(BrokerCapability).where(*lookup).execution_options(populate_existing=True))
    if capability is None:
        raise CapabilityDenied("Invalid broker capability")
    scope = CapabilityScope.model_validate_json(
        # JSON round-trip preserves the strict tuple contract.
        json.dumps(capability.scope)
    )
    now, _ = _active_scope(db, scope, settings)
    if not now < capability.expires_at:
        raise CapabilityDenied("Broker capability has expired")
    return capability, scope


def _valid_tokens(count):
    return type(count) is int and 0 <= count <= MAX_ALLOWANCE


def _receipt(row, scope):
    return Reservation(row.id, scope, row.request_id, row.request_digest, row.operation,
                       row.input_tokens, row.output_tokens, row.state)


def reserve_request(db, bearer: str, audience: str, request_id: str, input_tokens: int,
                    output_limit: int, *, operation: str, request_digest: str, settings) -> Reservation:
    """Reserve using a trusted digest of the canonical request and its route."""
    if (
        type(request_id) is not str or not 0 < len(request_id) <= 120 or not request_id.isprintable()
        or not isinstance(request_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", request_digest)
        or not _valid_tokens(input_tokens) or not _valid_tokens(output_limit)
    ):
        raise CapabilityDenied("Invalid broker request allowance")
    capability, scope = _authenticate(db, bearer, audience, settings)
    if operation not in scope.operations:
        raise CapabilityDenied("Operation is outside the capability")
    within_capability = BrokerReservation.capability_id == capability.id
    duplicate = db.scalar(select(BrokerReservation).where(
        within_capability, BrokerReservation.request_id == request_id,
    ).execution_options(populate_existing=True))
    if duplicate is not None:
        if (duplicate.operation, duplicate.request_digest, duplicate.input_tokens, duplicate.output_tokens) != (
            operation, request_digest, input_tokens, output_limit
        ):
            raise CapabilityDenied("Logical request already has another payload or allowance")
        return _receipt(duplicate, scope)
    active = BrokerReservation.state.in_(("reserved", "dispatched", "uncertain"))
    completed = BrokerReservation.state == "completed"
    count, pending, used_input, used_output = db.execute(select(
        func.count(),
        func.coalesce(func.sum(case((active, 1), else_=0)), 0),
        func.coalesce(func.sum(case(
            (active, BrokerReservation.input_tokens),
            (completed, BrokerReservation.actual_usage["input_tokens"].as_integer()),
            else_=0,
        )), 0),
        func.coalesce(func.sum(case(
            (active, BrokerReservation.output_tokens),
            (completed, BrokerReservation.actual_usage["output_tokens"].as_integer()),
            else_=0,
        )), 0),
    ).where(within_capability)).one()
    limits = capability.limits
    if (
        count >= limits["request_count"] or pending >= limits["concurrent_requests"]
        or used_input + input_tokens > limits["input_tokens"]
        or used_output + output_limit > limits["output_tokens"]
    ):
        raise CapabilityDenied("Broker allowance exhausted")
    row = BrokerReservation(capability_id=capability.id, request_id=request_id,
                            request_digest=request_digest, operation=operation,
                            input_tokens=input_tokens, output_tokens=output_limit)
    db.add(row)
    db.flush()
    return _receipt(row, scope)


def mark_dispatched(db, bearer: str, audience: str, reservation_id: str, *, settings) -> bool:
    """Send only after a True receipt commits. A lost sender must reconcile."""
    capability, _ = _authenticate(db, bearer, audience, settings)
    row = db.get(BrokerReservation, reservation_id, populate_existing=True)
    if row is None or row.capability_id != capability.id:
        raise CapabilityDenied("Reservation is outside the capability")
    if row.state != "reserved":
        return False
    row.state = "dispatched"
    db.flush()
    return True


def settle_request(
    db, reservation_id: str, actual_usage: dict | None,
    outcome: Literal["completed", "rejected", "uncertain"],
) -> None:
    """Trusted settlement, independent of the original capability's lifetime."""
    if outcome == "completed":
        if (
            not isinstance(actual_usage, dict) or set(actual_usage) != {"input_tokens", "output_tokens"}
            or not all(_valid_tokens(value) for value in actual_usage.values())
        ):
            raise CapabilityDenied("Completed requests require actual token usage")
    elif outcome not in {"rejected", "uncertain"} or actual_usage is not None:
        raise CapabilityDenied("Invalid settlement")
    _lock(db)
    row = db.get(BrokerReservation, reservation_id, populate_existing=True)
    if row is None:
        raise CapabilityDenied("Unknown reservation")
    if (row.state, row.actual_usage) == (outcome, actual_usage):
        return
    if row.state not in {"dispatched", "uncertain"} and (row.state, outcome) != ("reserved", "rejected"):
        raise CapabilityDenied("Reservation cannot transition to that outcome")
    row.state = outcome
    row.actual_usage = actual_usage
    db.flush()


def revoke_assignment(db, assignment_id: str) -> None:
    _lock(db)
    db.execute(update(Assignment).where(Assignment.id == assignment_id).values(broker_revoked=True))
    _revoke(db, BrokerCapability.assignment_id == assignment_id)


def revoke_run(db, project_id: str, run_id: str) -> None:
    """Called inside the existing workflow transition fence."""
    _revoke(db, BrokerCapability.assignment_id.in_(
        select(Assignment.id).where(Assignment.project_id == project_id, Assignment.run_id == run_id)
    ))


def _revoke(db, condition):
    db.execute(update(BrokerCapability).where(condition).values(revoked=True))
