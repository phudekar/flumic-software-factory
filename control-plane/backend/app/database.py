"""Persistence schema. Production schema changes run through Alembic."""

import time
import uuid
from sqlalchemy import String, Text, Float, JSON, ForeignKey, ForeignKeyConstraint, UniqueConstraint, create_engine, event
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
        ForeignKeyConstraint(["project_id", "id", "revision_id"],
                             ["repository_revisions.project_id", "repository_revisions.repository_id", "repository_revisions.id"],
                             name="fk_repository_head", use_alter=True),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    alias: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(20), default="github")
    host: Mapped[str] = mapped_column(String(240), default="github.com")
    owner: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(100))
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    credential_id: Mapped[str | None] = mapped_column(String(36))
    revision_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ProjectOwned:
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), default=DEFAULT_PROJECT_ID, server_default=DEFAULT_PROJECT_ID, index=True)


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
    profile: Mapped[str] = mapped_column(String(100))
    revision_id: Mapped[str] = mapped_column(String(36))
    config_id: Mapped[str | None] = mapped_column(String(36))
    target_id: Mapped[str] = mapped_column(String(80), default="local-docker")
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
        ForeignKeyConstraint(["project_id", "repository_id"],
                             ["repository_connections.project_id", "repository_connections.id"],
                             name="fk_execution_events_repository"),
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
    __table_args__ = (UniqueConstraint("project_id", "key", name="uq_workflow_control_project_key"),)
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
        ForeignKeyConstraint(["project_id", "repository_id"],
                             ["repository_connections.project_id", "repository_connections.id"],
                             name="fk_repository_revision_owner"),
        UniqueConstraint("project_id", "repository_id", "id", name="uq_repository_revision_identity"),
        ForeignKeyConstraint(["project_id", "repository_id", "parent_id"],
                             ["repository_revisions.project_id", "repository_revisions.repository_id", "repository_revisions.id"],
                             name="fk_repository_revision_parent"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    repository_id: Mapped[str] = mapped_column(String(36), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(36))
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
