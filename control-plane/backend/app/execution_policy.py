"""Versioned project policy; current direct behavior is never changed implicitly."""
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from factory.domain.execution import ExecutionMode, ExecutionPolicy, ExecutionPin, ExecutionPolicyError
from app.database import Project, ExecutionPolicyHead, ExecutionPolicyRevision, WorkflowRun, Audit
from app.projects import project_parameter, request_scope

IMPLICIT_DIRECT_REVISION = 'direct-v1'


def resolve_execution_pin(policy: ExecutionPolicy, requested: ExecutionMode | None,
                          parent_minimum: ExecutionMode | None) -> ExecutionPin:
    if requested not in (None, 'direct', 'protected') or parent_minimum not in (None, 'direct', 'protected'):
        raise ExecutionPolicyError('Unknown execution mode')
    requires_protected = policy.minimum_mode == 'protected' or parent_minimum == 'protected'
    mode = requested or ('protected' if requires_protected else policy.default_mode)
    if requires_protected and mode != 'protected':
        raise ExecutionPolicyError('Protected execution is required by project or parent policy')
    return ExecutionPin(mode=mode, policy_revision_id=policy.revision_id)


def read_execution_policy(db, scope) -> ExecutionPolicy:
    head = db.get(ExecutionPolicyHead, scope.project_id)
    if head is None:
        # A stable, immutable default for pre-policy and newly created projects.
        return ExecutionPolicy(revision_id=IMPLICIT_DIRECT_REVISION)
    revision = db.get(ExecutionPolicyRevision, head.revision_id)
    if revision is None or revision.project_id != scope.project_id:
        raise HTTPException(503, 'Project execution policy is unavailable')
    return ExecutionPolicy(revision_id=revision.id, default_mode=revision.default_mode,
                           minimum_mode=revision.minimum_mode)


def update_execution_policy(db, scope, expected_revision: str, changes: dict) -> ExecutionPolicy:
    # Lock the project to serialize first-write head creation on PostgreSQL.
    project = db.scalar(select(Project).where(Project.id == scope.project_id).with_for_update())
    if project is None:
        raise HTTPException(404, 'Project not found')
    current = read_execution_policy(db, scope)
    if current.revision_id != expected_revision:
        raise HTTPException(409, 'Execution policy changed; reload before saving')
    desired = ExecutionPolicy(revision_id=current.revision_id, **changes)
    if (desired.default_mode, desired.minimum_mode) == (current.default_mode, current.minimum_mode):
        return current
    revision = ExecutionPolicyRevision(project_id=scope.project_id,
        default_mode=desired.default_mode, minimum_mode=desired.minimum_mode)
    db.add(revision)
    db.flush()
    if current.revision_id == IMPLICIT_DIRECT_REVISION:
        try:
            with db.begin_nested():
                db.add(ExecutionPolicyHead(project_id=scope.project_id, revision_id=revision.id))
                db.flush()
        except IntegrityError:
            raise HTTPException(409, 'Execution policy changed; reload before saving') from None
    else:
        result = db.execute(update(ExecutionPolicyHead).where(
            ExecutionPolicyHead.project_id == scope.project_id,
            ExecutionPolicyHead.revision_id == current.revision_id,
        ).values(revision_id=revision.id))
        if result.rowcount != 1:
            raise HTTPException(409, 'Execution policy changed; reload before saving')
    db.add(Audit(action='execution-policy.update', resource=f'{scope.project_id}/{revision.id}'))
    return ExecutionPolicy(revision_id=revision.id, default_mode=revision.default_mode,
                           minimum_mode=revision.minimum_mode)


def require_direct_admission(db, scope) -> None:
    # A1 only persists policy. Until qualified admission exists, a protected
    # default must block new work rather than silently use the old direct path.
    project = db.scalar(select(Project).where(Project.id == scope.project_id).with_for_update())
    if project is None:
        raise HTTPException(404, 'Project not found')
    policy = read_execution_policy(db, scope)
    if resolve_execution_pin(policy, None, None).mode == 'protected':
        raise HTTPException(409, 'Protected execution is not available on a qualified runtime yet')


class PolicyEdit(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: str = Field(min_length=1, max_length=120)
    default_mode: ExecutionMode
    minimum_mode: ExecutionMode

    @model_validator(mode='after')
    def validate_strength(self):
        ExecutionPolicy(revision_id=self.expected_revision, default_mode=self.default_mode,
                        minimum_mode=self.minimum_mode)
        return self


def admitted_direct_count(db, scope):
    # Until A2 adds effective mode pins, every admitted run is direct. A2 must
    # replace this with pinned-mode filtering, never current project defaults.
    from sqlalchemy import func
    return db.scalar(select(func.count()).select_from(WorkflowRun).where(
        WorkflowRun.project_id == scope.project_id,
        WorkflowRun.state.not_in(('completed', 'failed', 'cancelled')),
    ))


def install(app, sessions):
    # These routes deliberately live under the existing admin-session/CSRF
    # middleware. Worker tokens and future project-member roles are not admin.
    @app.get('/api/v1/projects/{project_id}/execution-policy', dependencies=[Depends(project_parameter)])
    def read(request: Request, project_id: str):
        with sessions() as db:
            scope = request_scope(request, db)
            return {**read_execution_policy(db, scope).model_dump(),
                    'admitted_direct_runs': admitted_direct_count(db, scope),
                    'protected_execution_available': False}

    @app.put('/api/v1/projects/{project_id}/execution-policy', dependencies=[Depends(project_parameter)])
    def edit(request: Request, project_id: str, body: PolicyEdit):
        with sessions.begin() as db:
            scope = request_scope(request, db)
            policy = update_execution_policy(db, scope, body.expected_revision,
                                             body.model_dump(exclude={'expected_revision'}))
            return {**policy.model_dump(), 'admitted_direct_runs': admitted_direct_count(db, scope),
                    'protected_execution_available': False}
