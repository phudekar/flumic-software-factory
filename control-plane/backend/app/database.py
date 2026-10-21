"""Persistence schema. Production schema changes run through Alembic."""

import time
import uuid
from sqlalchemy import String, Text, Float, JSON, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def identity():
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Revision(Base):
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


class Event(Base):
    __tablename__ = "execution_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    worker_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str | None] = mapped_column(String(120), index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(120), index=True)
    stage: Mapped[str | None] = mapped_column(String(100))
    occurred_at: Mapped[float] = mapped_column(Float)
    received_at: Mapped[float] = mapped_column(Float, default=time.time)
    payload: Mapped[dict] = mapped_column(JSON)


class Conversation(Base):
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
    return engine, sessionmaker(engine, expire_on_commit=False)


class WorkerConfiguration(Base):
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


class WorkflowControlOperation(Base):
    __tablename__ = "workflow_control_operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identity)
    key: Mapped[str] = mapped_column(String(160), unique=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[str] = mapped_column(String(36), index=True)
    workflow_name: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(String(240))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
