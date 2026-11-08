"""Resolve immutable launch intent separately from live policy and qualification."""
import hashlib
import json
import math
from fastapi import HTTPException
from sqlalchemy import select
from factory.domain.execution import ExecutionMode, ExecutionPin, ExecutionPolicyError, TargetQualification
from app.database import Project
from app.execution_policy import read_execution_policy, resolve_execution_pin

PROTECTED_CAPABILITIES = frozenset({
    'protected-supervisor-v1', 'sandbox-isolation-v1', 'brokered-model-v1',
    'brokered-repository-v1', 'restricted-egress-v1', 'tool-policy-v1',
})


def read_execution_pin(launch: dict) -> ExecutionPin:
    if not isinstance(launch, dict):
        raise HTTPException(409, 'Pinned execution policy is invalid')
    if 'execution_pin' not in launch:
        return ExecutionPin(mode='direct', policy_revision_id='direct-v1')
    try:
        return ExecutionPin.model_validate(launch['execution_pin'])
    except ValueError:
        raise HTTPException(409, 'Pinned execution policy is invalid') from None


def qualification_digest(target: TargetQualification) -> str:
    value = target.model_dump(mode='json')
    value['capabilities'] = sorted(target.capabilities)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def require_target_qualification(pin: ExecutionPin, target: TargetQualification, now: float) -> None:
    if pin.mode == 'direct':
        return
    try:
        valid = (
            math.isfinite(now) and math.isfinite(target.expires_at) and target.expires_at > now
            and pin.target_qualification_id == target.id
            and pin.target_qualification_digest == qualification_digest(target)
            and PROTECTED_CAPABILITIES <= target.capabilities
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise HTTPException(409, 'Protected target qualification is missing, stale or changed')


def admit_execution(db, scope, launch: dict, requested_mode: ExecutionMode | None = None,
                    parent_pin: ExecutionPin | None = None) -> ExecutionPin:
    project = db.scalar(select(Project).where(Project.id == scope.project_id).with_for_update())
    if project is None:
        raise HTTPException(404, 'Project not found')
    policy = read_execution_policy(db, scope)
    previous = read_execution_pin(launch) if 'execution_pin' in launch and parent_pin is None else None
    if previous:
        if requested_mode is not None and requested_mode != previous.mode:
            raise HTTPException(409, 'A replay cannot change its pinned execution mode')
        requested_mode = previous.mode
    try:
        resolved = resolve_execution_pin(policy, requested_mode, parent_pin.mode if parent_pin else None)
    except ExecutionPolicyError as error:
        raise HTTPException(409, str(error)) from None
    if resolved.mode == 'protected':
        # No installed protected supervisor/sandbox/broker composition exists yet.
        # Attestation validation alone must never advertise executable support.
        raise HTTPException(409, 'Protected execution is not available on a qualified runtime yet')
    if previous:
        # A new logical replay keeps route/target intent but records the policy
        # that authorized this new admission. The original snapshot is untouched.
        return previous.model_copy(update={'policy_revision_id': policy.revision_id})
    return resolved


def require_dispatch_mode(pin: ExecutionPin) -> None:
    if pin.mode != 'direct':
        raise HTTPException(409, 'Protected execution requires an installed qualified supervisor')
