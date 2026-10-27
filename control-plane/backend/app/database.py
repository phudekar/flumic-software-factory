"""Persistence schema. Production schema changes run through Alembic."""

import time
import uuid
from sqlalchemy import (
    String,
    Text,
    Float,
    JSON,
    ForeignKey,
    ForeignKeyConstraint,
    UniqueConstraint,
    CheckConstraint,
    false,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def identity():
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


DEFAULT_PROJECT_ID = "00000000-0000-0000-0000-000000000001"


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    defaults: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class RepositoryConnection(Base):
    __tablename__ = "repository_connections"
    __table_args__ = (
        UniqueConstraint("project_id", "alias", name="uq_repository_project_alias"),
        UniqueConstraint("project_id", "id", name="uq_repository_project_identity"),
        ForeignKeyConstraint(
            ["project_id", "id", "revision_id"],
            [
                "repository_revisions.project_id",
                "repository_revisions.repository_id",
                "repository_revisions.id",
            ],
            name="fk_repository_head",
            use_alter=True,
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    alias: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(20), default="github")
    host: Mapped[str] = mapped_column(String(240), default="github.com")
    owner: Mapped[str] = mapped_column(String(1024))
    name: Mapped[str] = mapped_column(String(100))
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    credential_id: Mapped[str | None] = mapped_column(String(36))
    revision_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ProjectOwned:
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id"),
        default=DEFAULT_PROJECT_ID,
        server_default=DEFAULT_PROJECT_ID,
        index=True,
    )


class Revision(ProjectOwned, Base):
    __tablename__ = "config_revisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    scope: Mapped[str] = mapped_column(String(32), default="factory")
    parent_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    summary: Mapped[str] = mapped_column(String(240))
    document: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))


class Worker(Base):
    __tablename__ = "worker_instances"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    protocol: Mapped[str] = mapped_column(
        String(20), default="legacy", server_default="legacy"
    )
    pool_id: Mapped[str | None] = mapped_column(ForeignKey("worker_pools.id"))
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    registered_at: Mapped[float | None] = mapped_column(Float)
    last_heartbeat_at: Mapped[float | None] = mapped_column(Float)
    profile: Mapped[str] = mapped_column(String(100))
    revision_id: Mapped[str] = mapped_column(String(36))
    config_id: Mapped[str | None] = mapped_column(String(36))
    target_id: Mapped[str] = mapped_column(String(80), default="local-docker")
    target_snapshot: Mapped[dict] = mapped_column(
        JSON, default=dict, server_default="{}"
    )
    credential_hash: Mapped[str] = mapped_column(String(64))
    desired_state: Mapped[str] = mapped_column(String(32), default="created")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Operation(Base):
    __tablename__ = "lifecycle_operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    worker_id: Mapped[str] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    key: Mapped[str] = mapped_column(String(160), unique=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    error: Mapped[str | None] = mapped_column(String(240))


class Event(ProjectOwned, Base):
    __tablename__ = "execution_events"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "execution_id", "call_id", name="uq_action_model_call"
        ),
        ForeignKeyConstraint(
            ["project_id", "repository_id"],
            ["repository_connections.project_id", "repository_connections.id"],
            name="fk_execution_events_repository",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    worker_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str | None] = mapped_column(String(120), index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(120), index=True)
    stage: Mapped[str | None] = mapped_column(String(100))
    occurred_at: Mapped[float] = mapped_column(Float)
    received_at: Mapped[float] = mapped_column(Float, default=time.time)
    payload: Mapped[dict] = mapped_column(JSON)

    # NULL attribution preserves historical/legacy records without guessing.
    assignment_id: Mapped[str | None] = mapped_column(String(36), index=True)
    execution_id: Mapped[str | None] = mapped_column(String(120), index=True)
    definition_id: Mapped[str | None] = mapped_column(String(36), index=True)
    workflow_revision_id: Mapped[str | None] = mapped_column(String(36), index=True)
    executor: Mapped[str | None] = mapped_column(String(100), index=True)
    job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    call_id: Mapped[str | None] = mapped_column(String(180))
    ingest_digest: Mapped[str | None] = mapped_column(String(64))

    # NULL means historical repository attribution is unknown, never guessed.
    repository_id: Mapped[str | None] = mapped_column(String(36))


class Conversation(ProjectOwned, Base):
    __tablename__ = "conversation_content"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[float] = mapped_column(Float, index=True)


class Session(Base):
    __tablename__ = "operator_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf: Mapped[str] = mapped_column(String(80))
    expires_at: Mapped[float] = mapped_column(Float, index=True)


class Audit(Base):
    __tablename__ = "operator_audit"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    action: Mapped[str] = mapped_column(String(80))
    resource: Mapped[str] = mapped_column(String(160))


def database(url):
    from sqlalchemy.pool import StaticPool

    kwargs = (
        {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
        if url == "sqlite://"
        else {}
    )
    engine = create_engine(url, pool_pre_ping=True, **kwargs)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def enforce_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    return engine, sessionmaker(engine, expire_on_commit=False)


class WorkerConfiguration(ProjectOwned, Base):
    __tablename__ = "worker_configurations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    family_id: Mapped[str] = mapped_column(String(36), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(36))
    cloned_from: Mapped[str | None] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(100))
    profile: Mapped[str] = mapped_column(String(100))
    factory_revision_id: Mapped[str] = mapped_column(String(36))
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class WorkflowControlOperation(ProjectOwned, Base):
    __tablename__ = "workflow_control_operations"
    __table_args__ = (
        UniqueConstraint("project_id", "key", name="uq_workflow_control_project_key"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    key: Mapped[str] = mapped_column(String(160))
    request_digest: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[str] = mapped_column(String(36), index=True)
    workflow_name: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(String(240))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class RepositoryRevision(ProjectOwned, Base):
    """Append-only connection configuration; runs pin this ID rather than the head."""

    __tablename__ = "repository_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "repository_id"],
            ["repository_connections.project_id", "repository_connections.id"],
            name="fk_repository_revision_owner",
        ),
        UniqueConstraint(
            "project_id", "repository_id", "id", name="uq_repository_revision_identity"
        ),
        ForeignKeyConstraint(
            ["project_id", "repository_id", "parent_id"],
            [
                "repository_revisions.project_id",
                "repository_revisions.repository_id",
                "repository_revisions.id",
            ],
            name="fk_repository_revision_parent",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    repository_id: Mapped[str] = mapped_column(String(36), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(36))
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class SecretVersion(ProjectOwned, Base):
    __tablename__ = "secret_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_secret_project_identity"),
        ForeignKeyConstraint(
            ["project_id", "repository_id"],
            ["repository_connections.project_id", "repository_connections.id"],
            name="fk_secret_repository",
        ),
        ForeignKeyConstraint(
            ["project_id", "parent_id"],
            ["secret_versions.project_id", "secret_versions.id"],
            name="fk_secret_parent",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    repository_id: Mapped[str | None] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(100))
    purpose: Mapped[str] = mapped_column(String(20))
    parent_id: Mapped[str | None] = mapped_column(String(36))
    ciphertext: Mapped[str] = mapped_column(Text)
    revoked_at: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ActionContextSnapshot(ProjectOwned, Base):
    __tablename__ = "action_context_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "execution_id", name="uq_action_context_execution"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    execution_id: Mapped[str] = mapped_column(String(120))
    ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class WorkerPool(Base):
    __tablename__ = "worker_pools"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    profile: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ProjectPool(Base):
    __tablename__ = "project_worker_pools"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    pool_id: Mapped[str] = mapped_column(
        ForeignKey("worker_pools.id"), primary_key=True
    )


class Assignment(ProjectOwned, Base):
    __tablename__ = "execution_assignments"
    __table_args__ = (
        UniqueConstraint("project_id", "execution_id", name="uq_assignment_execution"),
        UniqueConstraint("project_id", "id", name="uq_assignment_owner"),
        ForeignKeyConstraint(
            ["project_id", "repository_id"],
            ["repository_connections.project_id", "repository_connections.id"],
            name="fk_assignment_repository",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    repository_id: Mapped[str] = mapped_column(String(36))
    pool_id: Mapped[str] = mapped_column(ForeignKey("worker_pools.id"))
    run_id: Mapped[str] = mapped_column(String(120))
    stage_id: Mapped[str] = mapped_column(String(100))
    execution_id: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20))
    executor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(120))
    worker_id: Mapped[str | None] = mapped_column(ForeignKey("worker_instances.id"))
    status: Mapped[str] = mapped_column(String(20), default="ready")
    claim_hash: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[float | None] = mapped_column(Float)
    context_id: Mapped[str | None] = mapped_column(
        ForeignKey("action_context_snapshots.id")
    )
    input_ciphertext: Mapped[str] = mapped_column(Text)
    result_ciphertext: Mapped[str | None] = mapped_column(Text)
    result_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class WorkflowDefinitionRecord(ProjectOwned, Base):
    __tablename__ = "workflow_definitions"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_workflow_definition_name"),
        UniqueConstraint("project_id", "id", name="uq_workflow_definition_identity"),
        ForeignKeyConstraint(
            ["project_id", "id", "revision_id"],
            [
                "workflow_definition_revisions.project_id",
                "workflow_definition_revisions.definition_id",
                "workflow_definition_revisions.id",
            ],
            name="fk_workflow_definition_head",
            use_alter=True,
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    name: Mapped[str] = mapped_column(String(100))
    revision_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class WorkflowRevision(ProjectOwned, Base):
    __tablename__ = "workflow_definition_revisions"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_workflow_revision_identity"),
        UniqueConstraint(
            "project_id", "definition_id", "id", name="uq_workflow_revision_family"
        ),
        ForeignKeyConstraint(
            ["project_id", "definition_id"],
            ["workflow_definitions.project_id", "workflow_definitions.id"],
            name="fk_workflow_revision_definition",
        ),
        ForeignKeyConstraint(
            ["project_id", "definition_id", "parent_id"],
            [
                "workflow_definition_revisions.project_id",
                "workflow_definition_revisions.definition_id",
                "workflow_definition_revisions.id",
            ],
            name="fk_workflow_revision_parent",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    definition_id: Mapped[str] = mapped_column(String(36), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(36))
    digest: Mapped[str] = mapped_column(String(64))
    graph_ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class WorkflowLaunchSnapshot(ProjectOwned, Base):
    __tablename__ = "workflow_launch_snapshots"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_workflow_launch_owner"),
        ForeignKeyConstraint(
            ["project_id", "workflow_revision_id"],
            [
                "workflow_definition_revisions.project_id",
                "workflow_definition_revisions.id",
            ],
            name="fk_workflow_launch_revision",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    workflow_revision_id: Mapped[str] = mapped_column(String(36))
    ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class WorkflowAdmission(Base):
    __tablename__ = "workflow_admission"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    last_project: Mapped[str] = mapped_column(String(36), default="")
    tick: Mapped[int] = mapped_column(default=0)


class WorkflowRun(ProjectOwned, Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_workflow_run_owner"),
        UniqueConstraint("project_id", "request_key", name="uq_workflow_run_request"),
        ForeignKeyConstraint(
            ["project_id", "snapshot_id"],
            ["workflow_launch_snapshots.project_id", "workflow_launch_snapshots.id"],
            name="fk_run_snapshot",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    snapshot_id: Mapped[str] = mapped_column(String(36))
    sequence: Mapped[int] = mapped_column()
    request_key: Mapped[str] = mapped_column(String(120))
    request_digest: Mapped[str] = mapped_column(String(64))
    input_ciphertext: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default="queued")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class StageAttempt(ProjectOwned, Base):
    __tablename__ = "workflow_stage_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "run_id"],
            ["workflow_runs.project_id", "workflow_runs.id"],
            name="fk_attempt_run",
        ),
        UniqueConstraint(
            "project_id", "run_id", "stage_id", "generation", name="uq_stage_generation"
        ),
        UniqueConstraint("project_id", "id", name="uq_attempt_owner"),
        ForeignKeyConstraint(
            ["project_id", "assignment_id"],
            ["execution_assignments.project_id", "execution_assignments.id"],
            name="fk_attempt_assignment",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    stage_id: Mapped[str] = mapped_column(String(100))
    generation: Mapped[int] = mapped_column(default=1)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    assignment_id: Mapped[str | None] = mapped_column(String(36))
    spec_digest: Mapped[str | None] = mapped_column(String(64))
    input_digest: Mapped[str | None] = mapped_column(String(64))
    origins_ciphertext: Mapped[str | None] = mapped_column(Text)
    result_digest: Mapped[str | None] = mapped_column(String(64))
    result_ciphertext: Mapped[str | None] = mapped_column(Text)
    available_at: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ProjectBusinessState(ProjectOwned, Base):
    __tablename__ = "project_business_state"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "repository_id"],
            ["repository_connections.project_id", "repository_connections.id"],
            name="fk_business_state_repository",
        ),
    )
    repository_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)


class ExternalResourceClaim(Base):
    __tablename__ = "external_resource_claims"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "execution_id"],
            ["workflow_stage_attempts.project_id", "workflow_stage_attempts.id"],
            name="fk_resource_execution",
        ),
    )
    resource: Mapped[str] = mapped_column(String(512), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    execution_id: Mapped[str] = mapped_column(String(36))
    expires_at: Mapped[float] = mapped_column(Float)


class RepositoryExecutionMode(Base):
    __tablename__ = "repository_execution_modes"
    repository: Mapped[str] = mapped_column(String(300), primary_key=True)
    mode: Mapped[str] = mapped_column(String(20))


class RuntimeJob(ProjectOwned, Base):
    __tablename__ = "runtime_jobs"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_runtime_job_project_identity"),
        UniqueConstraint("project_id", "execution_id", name="uq_runtime_job_execution"),
        ForeignKeyConstraint(
            ["project_id", "execution_id"],
            ["workflow_stage_attempts.project_id", "workflow_stage_attempts.id"],
            name="fk_runtime_job_execution",
        ),
        ForeignKeyConstraint(
            ["project_id", "assignment_id"],
            ["execution_assignments.project_id", "execution_assignments.id"],
            name="fk_runtime_job_assignment",
        ),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str] = mapped_column(String(36))
    assignment_id: Mapped[str] = mapped_column(String(36))
    spec_digest: Mapped[str] = mapped_column(String(64))
    launch_ciphertext: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    launch_started: Mapped[bool] = mapped_column(default=False)
    runtime_observed: Mapped[bool] = mapped_column(default=False)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    deadline: Mapped[float] = mapped_column(Float)
    result_ciphertext: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    next_cleanup_at: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str | None] = mapped_column(String(120))
    target_id: Mapped[str] = mapped_column(
        String(80), default="local-docker", server_default="local-docker"
    )
    target_revision: Mapped[str] = mapped_column(
        String(64), default="", server_default=""
    )
    target_snapshot: Mapped[dict] = mapped_column(
        JSON, default=dict, server_default="{}"
    )
    runtime_phase: Mapped[str] = mapped_column(
        String(24), default="intent", server_default="intent"
    )
    cleanup_pending: Mapped[bool] = mapped_column(default=False, server_default=false())


class RuntimeLease(Base):
    __tablename__ = "runtime_leases"
    resource_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    owner: Mapped[str | None] = mapped_column(String(36))
    epoch: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[float] = mapped_column(Float, default=0)


class RuntimeResourceBinding(Base):
    __tablename__ = "runtime_resource_bindings"
    __table_args__ = (
        UniqueConstraint(
            "target_id",
            "target_revision",
            "kind",
            "logical_id",
            name="uq_runtime_binding_logical",
        ),
        UniqueConstraint(
            "target_id",
            "target_revision",
            "namespace",
            "kind",
            "uid",
            name="uq_runtime_binding_remote",
        ),
        ForeignKeyConstraint(
            ["project_id", "job_id"],
            ["runtime_jobs.project_id", "runtime_jobs.id"],
            name="fk_runtime_binding_job",
        ),
        CheckConstraint(
            "(worker_id IS NOT NULL AND job_id IS NULL AND project_id IS NULL) OR (worker_id IS NULL AND job_id IS NOT NULL AND project_id IS NOT NULL)",
            name="ck_runtime_binding_owner",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    target_id: Mapped[str] = mapped_column(String(80))
    target_revision: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(40))
    logical_id: Mapped[str] = mapped_column(String(160))
    namespace: Mapped[str] = mapped_column(String(63))
    name: Mapped[str] = mapped_column(String(253))
    uid: Mapped[str] = mapped_column(String(128))
    owner_uid: Mapped[str | None] = mapped_column(String(128))
    spec_digest: Mapped[str] = mapped_column(String(64))
    pending_spec_digest: Mapped[str | None] = mapped_column(String(64))
    phase: Mapped[str] = mapped_column(String(24), default="prepared")
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"))
    worker_id: Mapped[str | None] = mapped_column(ForeignKey("worker_instances.id"))
    job_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class DynamicDispatch(ProjectOwned, Base):
    __tablename__ = "dynamic_dispatches"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "kind", "subject_id", name="uq_dynamic_dispatch_subject"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    kind: Mapped[str] = mapped_column(String(20))
    subject_id: Mapped[str] = mapped_column(String(36))
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    engine_id: Mapped[str | None] = mapped_column(String(120))
    available_at: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str | None] = mapped_column(String(120))


class ProjectFollowup(ProjectOwned, Base):
    __tablename__ = "project_followups"
    __table_args__ = (
        UniqueConstraint("project_id", "request_key", name="uq_project_followup_key"),
        ForeignKeyConstraint(
            ["project_id", "source_execution_id"],
            ["workflow_stage_attempts.project_id", "workflow_stage_attempts.id"],
            name="fk_followup_source",
        ),
        ForeignKeyConstraint(
            ["project_id", "source_run_id"],
            ["workflow_runs.project_id", "workflow_runs.id"],
            name="fk_followup_source_run",
        ),
        ForeignKeyConstraint(
            ["project_id", "result_run_id"],
            ["workflow_runs.project_id", "workflow_runs.id"],
            name="fk_followup_result_run",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    source_execution_id: Mapped[str] = mapped_column(String(36))
    source_run_id: Mapped[str] = mapped_column(String(36))
    request_key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(20))
    ciphertext: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    result_run_id: Mapped[str | None] = mapped_column(String(36))
    error: Mapped[str | None] = mapped_column(String(120))


class RepositoryTrigger(ProjectOwned, Base):
    __tablename__ = "repository_triggers"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_trigger_owner"),
        ForeignKeyConstraint(
            ["project_id", "repository_id"],
            ["repository_connections.project_id", "repository_connections.id"],
            name="fk_trigger_repository",
        ),
        ForeignKeyConstraint(
            ["project_id", "definition_id"],
            ["workflow_definitions.project_id", "workflow_definitions.id"],
            name="fk_trigger_definition",
        ),
        ForeignKeyConstraint(
            ["project_id", "id", "current_token_id"],
            [
                "repository_trigger_tokens.project_id",
                "repository_trigger_tokens.binding_id",
                "repository_trigger_tokens.id",
            ],
            name="fk_trigger_token_head",
            use_alter=True,
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    repository_id: Mapped[str] = mapped_column(String(36))
    event_kind: Mapped[str] = mapped_column(String(24))
    definition_id: Mapped[str] = mapped_column(String(36))
    revision_id: Mapped[str] = mapped_column(String(36), default=identity)
    configuration_ciphertext: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
    current_token_id: Mapped[str | None] = mapped_column(String(36))
    poll_cursor: Mapped[int] = mapped_column(default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[float | None] = mapped_column(Float)
    next_poll_at: Mapped[float] = mapped_column(Float, default=0)
    last_error: Mapped[str | None] = mapped_column(String(240))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class TriggerToken(ProjectOwned, Base):
    __tablename__ = "repository_trigger_tokens"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "binding_id", "id", name="uq_trigger_token_owner"
        ),
        ForeignKeyConstraint(
            ["project_id", "binding_id"],
            ["repository_triggers.project_id", "repository_triggers.id"],
            name="fk_trigger_token_binding",
        ),
        ForeignKeyConstraint(
            ["project_id", "binding_id", "parent_id"],
            [
                "repository_trigger_tokens.project_id",
                "repository_trigger_tokens.binding_id",
                "repository_trigger_tokens.id",
            ],
            name="fk_trigger_token_parent",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    binding_id: Mapped[str] = mapped_column(String(36))
    parent_id: Mapped[str | None] = mapped_column(String(36))
    ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    revoked_at: Mapped[float | None] = mapped_column(Float)


class TriggerReceipt(ProjectOwned, Base):
    __tablename__ = "repository_trigger_receipts"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "binding_id", "event_id", name="uq_trigger_delivery"
        ),
        ForeignKeyConstraint(
            ["project_id", "binding_id"],
            ["repository_triggers.project_id", "repository_triggers.id"],
            name="fk_trigger_receipt_binding",
        ),
        ForeignKeyConstraint(
            ["project_id", "run_id"],
            ["workflow_runs.project_id", "workflow_runs.id"],
            name="fk_trigger_receipt_run",
        ),
        ForeignKeyConstraint(
            ["project_id", "workflow_revision_id"],
            [
                "workflow_definition_revisions.project_id",
                "workflow_definition_revisions.id",
            ],
            name="fk_trigger_receipt_workflow",
        ),
        ForeignKeyConstraint(
            ["project_id", "repository_id", "repository_revision_id"],
            [
                "repository_revisions.project_id",
                "repository_revisions.repository_id",
                "repository_revisions.id",
            ],
            name="fk_trigger_receipt_repository",
        ),
        ForeignKeyConstraint(
            ["project_id", "binding_id", "auth_token_version"],
            [
                "repository_trigger_tokens.project_id",
                "repository_trigger_tokens.binding_id",
                "repository_trigger_tokens.id",
            ],
            name="fk_trigger_receipt_token",
        ),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    binding_id: Mapped[str] = mapped_column(String(36))
    event_id: Mapped[str] = mapped_column(String(200))
    payload_digest: Mapped[str] = mapped_column(String(64))
    config_revision_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20))
    source_key: Mapped[str | None] = mapped_column(String(120))
    run_id: Mapped[str | None] = mapped_column(String(36))
    workflow_revision_id: Mapped[str | None] = mapped_column(String(36))
    repository_revision_id: Mapped[str | None] = mapped_column(String(36))
    repository_id: Mapped[str | None] = mapped_column(String(36))
    auth_token_version: Mapped[str | None] = mapped_column(String(36))
    received_at: Mapped[float] = mapped_column(Float, default=time.time)


class TriggerObservation(ProjectOwned, Base):
    __tablename__ = "repository_trigger_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "binding_id"],
            ["repository_triggers.project_id", "repository_triggers.id"],
            name="fk_trigger_observation_binding",
        ),
        ForeignKeyConstraint(
            ["project_id", "run_id"],
            ["workflow_runs.project_id", "workflow_runs.id"],
            name="fk_trigger_observation_run",
        ),
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    binding_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(36))
    eligible: Mapped[bool] = mapped_column(default=False)
    last_stamp: Mapped[str] = mapped_column(String(120), default="")
    dispatched_stamp: Mapped[str] = mapped_column(String(120), default="")
    epoch: Mapped[int] = mapped_column(default=0)
    last_dispatched_epoch: Mapped[int] = mapped_column(default=0)
    remote_iid: Mapped[int] = mapped_column()
    last_checked: Mapped[float] = mapped_column(Float, default=time.time)
