from dataclasses import dataclass, field
from pathlib import Path
import os


@dataclass(frozen=True)
class AppSettings:
    database_url: str
    admin_password: str
    encryption_key: str
    root: Path
    snapshot_root: Path
    host_root: Path
    frontend: Path
    public_origin: str = "http://localhost:3400"
    worker_endpoint: str = "http://factory-control-api:8000"
    compose_project_name: str = "hatchet-test"
    # Deprecated constructor alias for integrations using the old Compose name.
    project: str | None = field(default=None, kw_only=True)
    cookie_secure: bool = False
    job_bridge: str | None = field(default=None, kw_only=True)

    def __post_init__(self):
        import re

        if self.job_bridge is not None and (
            self.job_bridge in {"host", "none", "bridge"}
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", self.job_bridge)
        ):
            raise ValueError("Job bridge must be a fixed dedicated Docker network")
        if self.project is not None:
            object.__setattr__(self, "compose_project_name", self.project)

    @classmethod
    def load(cls):
        root = Path(os.environ.get("FACTORY_ROOT", ".")).resolve()
        return cls(
            database_url=os.environ["CONTROL_DATABASE_URL"],
            admin_password=os.environ["CONTROL_ADMIN_PASSWORD"],
            encryption_key=os.environ["CONTROL_ENCRYPTION_KEY"],
            root=root,
            host_root=Path(os.environ.get("FACTORY_HOST_ROOT", str(root))),
            snapshot_root=root / "data/control-plane/snapshots",
            frontend=Path(
                os.environ.get("CONTROL_FRONTEND", "control-plane/frontend/dist")
            ),
            public_origin=os.environ.get(
                "CONTROL_PUBLIC_ORIGIN", "http://localhost:3400"
            ),
            worker_endpoint=os.environ.get(
                "CONTROL_WORKER_ENDPOINT", "http://factory-control-api:8000"
            ),
            job_bridge=os.environ.get("CONTROL_JOB_BRIDGE") or None,
            cookie_secure=os.environ.get("CONTROL_COOKIE_SECURE", "false") == "true",
        )
