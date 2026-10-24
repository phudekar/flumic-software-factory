"""Restricted Docker I/O for broker-owned one-time jobs, never shared workers."""

import json
import re
import os
from pathlib import Path
import stat
import shutil
import docker
from docker.errors import NotFound, APIError
from app.job_contracts import RuntimeJobRef, RuntimeJobState, JobCapture
from factory.workflows.references import json_bytes

MAX_RESULT = 1048576
MAX_LOG = 262144


class DockerJobRuntime:
    def __init__(self, root, host_root, deployment, *, client=None, bridge=None):
        self.root, self.host_root = Path(root).absolute(), Path(host_root).absolute()
        if bridge is not None and (
            bridge in {"host", "none", "bridge"}
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", bridge)
        ):
            raise ValueError("Job bridge must be a fixed dedicated Docker network")
        self.deployment, self.bridge = deployment, bridge
        self.client = client or docker.from_env(timeout=10)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def reference(launch):
        return RuntimeJobRef(
            **{key: getattr(launch, key) for key in RuntimeJobRef.model_fields}
        )

    def labels(self, ref):
        return {
            "flumic.job." + key: value
            for key, value in {
                "deployment": self.deployment,
                "id": ref.job_id,
                "project": ref.project_id,
                "execution": ref.execution_id,
                "spec": ref.spec_digest,
            }.items()
        }

    def _container(self, ref):
        try:
            container = self.client.containers.get("flumic-job-" + ref.job_id)
        except NotFound:
            return None
        container.reload()
        config = container.attrs["Config"]
        if config["Image"] != ref.image or any(
            config.get("Labels", {}).get(k) != v for k, v in self.labels(ref).items()
        ):
            raise ValueError("Job container identity does not match durable authority")
        return container

    def _directory(self, ref):
        ref = RuntimeJobRef.model_validate(ref)
        directory = self.root / ref.job_id
        if directory.is_symlink():
            raise ValueError("Invalid broker job directory")
        return directory

    @staticmethod
    def _file(path, value):
        encoded = json_bytes(value)
        try:
            fd = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444
            )
        except FileExistsError:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                if stream.read(len(encoded) + 1) != encoded:
                    raise ValueError("Job input files are immutable")
            return
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())

    def ensure(self, launch):
        ref = self.reference(launch)
        if launch.network == "bridge" and not self.bridge:
            raise ValueError("Dedicated job bridge is not configured")
        existing = self._container(ref)
        if existing is not None:
            if existing.attrs["State"]["Status"] == "created":
                existing.start()
            return ref
        directory = self._directory(ref)
        directory.mkdir(mode=0o700, exist_ok=True)
        output = directory / "output"
        output.mkdir(mode=0o777, exist_ok=True)
        if output.is_symlink():
            raise ValueError("Invalid broker output directory")
        output.chmod(0o777)  # Writable by fixed UID10001; parent stays broker-private.
        self._file(directory / "input.json", launch.inputs)
        self._file(directory / "credentials.json", launch.credentials)
        host = self.host_root / ref.job_id
        options = dict(
            name="flumic-job-" + ref.job_id,
            entrypoint=launch.command[0],
            command=launch.command[1:],
            labels=self.labels(ref),
            environment={},
            user="10001:10001",
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            network_mode=self.bridge if launch.network == "bridge" else "none",
            nano_cpus=int(launch.limits.cpu * 1000000000),
            mem_limit=launch.limits.memory_mib * 1048576,
            pids_limit=launch.limits.pids,
            restart_policy={"Name": "no"},
            auto_remove=False,
            tmpfs={
                "/tmp": f"rw,noexec,nosuid,nodev,size={launch.limits.tmpfs_mib}m,mode=1777"
            },
            volumes={
                str(host / "input.json"): {"bind": "/flumic/input.json", "mode": "ro"},
                str(host / "credentials.json"): {
                    "bind": "/flumic/credentials.json",
                    "mode": "ro",
                },
                str(host / "output"): {"bind": "/flumic/output", "mode": "rw"},
            },
            log_config=docker.types.LogConfig(
                type="json-file", config={"max-size": "1m", "max-file": "2"}
            ),
        )
        try:
            existing = self.client.containers.create(launch.image, **options)
        except APIError as error:
            if error.status_code != 409:
                raise
            existing = self._container(ref)
            if existing is None:
                raise ValueError("Conflicting container disappeared") from None
        # A racing reconciler may already have started/completed the same container.
        if existing.attrs.get("State", {}).get("Status", "created") == "created":
            try:
                existing.start()
            except APIError:
                existing = self._container(ref)
                if existing is None or existing.attrs["State"]["Status"] == "created":
                    raise
        return ref

    def inspect(self, ref):
        container = self._container(ref)
        if container is None:
            return RuntimeJobState(state="missing")
        state = container.attrs["State"]
        return RuntimeJobState(
            state=state["Status"],
            exit_code=state.get("ExitCode"),
            oom=state.get("OOMKilled", False),
        )

    @staticmethod
    def _logs(container, stderr):
        stream = container.logs(
            stdout=not stderr, stderr=stderr, stream=True, follow=False
        )
        content = bytearray()
        try:
            for chunk in stream:
                content.extend(chunk[: MAX_LOG + 1 - len(content)])
                if len(content) > MAX_LOG:
                    break
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
        return bytes(content[:MAX_LOG]).decode("utf-8", errors="replace").encode()[
            :MAX_LOG
        ].decode("utf-8", errors="ignore"), len(content) > MAX_LOG

    def _result(self, ref):
        directory = self._directory(ref) / "output"
        try:
            directory_fd = os.open(
                directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                fd = os.open(
                    "result.json",
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
            finally:
                os.close(directory_fd)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size > MAX_RESULT
                ):
                    raise ValueError("Invalid or oversized result file")
                encoded = stream.read(MAX_RESULT + 1)
            if len(encoded) > MAX_RESULT:
                raise ValueError("Job result exceeds 1 MiB")
            value = json.loads(encoded)
            if (
                not isinstance(value, dict)
                or set(value) - {"output", "usage"}
                or not isinstance(value.get("output"), dict)
            ):
                raise ValueError("Invalid job result envelope")
            json_bytes(value)
            capture = JobCapture.model_validate(value)
        except (OSError, ValueError, RecursionError):
            raise ValueError(
                "Missing, malformed, unsafe or oversized job result"
            ) from None
        return capture

    def collect(self, ref):
        container = self._container(ref)
        if container is None:
            raise ValueError("Job container is missing")
        state = container.attrs["State"]
        if state["Status"] not in {"exited", "dead"}:
            raise ValueError("Job is not terminal")
        error = None
        if state.get("OOMKilled") or state.get("ExitCode") != 0:
            error = "Job exited nonzero or was killed for memory usage"
        try:
            capture = self._result(ref) if error is None else JobCapture(output={})
        except ValueError:
            capture = JobCapture(output={})
            error = "Missing, malformed, unsafe or oversized job result"
        stdout, stdout_truncated = self._logs(container, False)
        stderr, stderr_truncated = self._logs(container, True)
        return capture.model_copy(
            update=dict(
                error=error,
                exit_code=state.get("ExitCode"),
                oom=state.get("OOMKilled", False),
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )
        )

    def terminate(self, ref, grace_seconds=5):
        container = self._container(ref)
        if container is None:
            return
        if container.attrs["State"]["Status"] in {"exited", "dead"}:
            return
        if container.attrs["State"]["Status"] == "created":
            container.remove()
            return
        try:
            container.stop(timeout=min(max(grace_seconds, 0), 10))
        finally:
            container = self._container(ref)
            if container and container.attrs["State"]["Status"] not in {
                "exited",
                "dead",
            }:
                container.kill()

    def cleanup(self, ref):
        container = self._container(ref)
        if container is not None:
            if container.attrs["State"]["Status"] not in {"exited", "dead", "created"}:
                raise ValueError("Cannot clean up a live job")
            container.remove()
        directory = self._directory(ref)
        if directory.exists():
            if not shutil.rmtree.avoids_symlink_attacks:
                raise RuntimeError(
                    "Job cleanup requires fd-based safe directory removal"
                )
            shutil.rmtree(directory)
