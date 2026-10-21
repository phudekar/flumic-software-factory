"""Assemble managed worker images from a small explicit, credential-free context."""

import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import time
import uuid

from docker.errors import ImageNotFound, APIError
from app.security import redact

PROTOCOL = "3"


class BuildStore:
    """Bounded, atomic progress records shared by API and reconciler."""

    def __init__(self, root):
        self.root = Path(root)

    def path(self, worker_id):
        if not worker_id or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
            for c in worker_id
        ):
            raise ValueError("Invalid worker identifier")
        return self.root / (worker_id + ".json")

    def read(self, worker_id):
        try:
            return json.loads(self.path(worker_id).read_text())
        except FileNotFoundError:
            return {"status": "not_requested", "content": "", "image": None}

    def write(self, worker_id, status, content, image=None):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(worker_id)
        data = {
            "status": status,
            "content": redact(content)[-65536:],
            "image": image,
            "updated_at": time.time(),
        }
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(data))
            temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class WorkerImageBuilder:
    def __init__(self, root, client, store):
        self.root, self.client, self.store = Path(root), client, store

    def context_files(self, settings):
        files = {}
        worker = self.root / "worker"
        names = [
            "requirements.txt",
            "bootstrap.sh",
            "worker.py",
            "guardrails.py",
            "model_client.py",
            "github_client.py",
            "cli_agent.py",
            "codex_healthcheck.py",
        ]
        for name in names:
            path = worker / name
            if path.is_symlink():
                raise ValueError("Worker build inputs must not be symbolic links")
            files["worker/" + name] = path.read_bytes()
        for path in sorted((worker / "factory").rglob("*.py")):
            if path.is_symlink() or not path.resolve().is_relative_to(worker.resolve()):
                raise ValueError("Worker build inputs must stay inside worker source")
            files[str(path.relative_to(self.root))] = path.read_bytes()
        for name in ("install-system-dependencies.sh", "install-coding-tools.sh"):
            files["image/" + name] = (worker / "image" / name).read_bytes()
        template = (worker / "image/Dockerfile.template").read_text()
        files["Dockerfile"] = template.replace(
            "__BASE_IMAGE__", settings.base_image
        ).encode()
        files["image/system-packages.txt"] = (
            "\n".join(sorted(set(settings.system_packages))) + "\n"
        ).encode()
        files["image/setup.sh"] = (settings.image_setup_script + "\n").encode()
        tool = settings.developer_agent
        if settings.coding_factory:
            tool = "api"  # Custom adapters supply dependencies via profile setup.
        files["image/coding-tool.txt"] = tool.encode()
        files["image/codex-version.txt"] = settings.codex_version.encode()
        files["image/claude-version.txt"] = settings.claude_version.encode()
        return files

    def prepare(self, settings, worker_id):
        content = "Resolving base image " + settings.base_image + "\n"
        image = None
        self.store.write(worker_id, "resolving", content)
        try:
            # Pull resolves mutable tags before computing the cache key. Credentials
            # for worker integrations are never supplied to Docker build or pull.
            base = self.client.images.pull(settings.base_image)
            files = self.context_files(settings)
            digest = hashlib.sha256(base.id.encode())
            for name, value in sorted(files.items()):
                digest.update(name.encode() + b"\0" + hashlib.sha256(value).digest())
            image = "software-factory-worker:" + digest.hexdigest()
            # Build FROM the resolved immutable content, even if its tag moves.
            files["Dockerfile"] = files["Dockerfile"].replace(
                settings.base_image.encode(), base.id.encode(), 1
            )
            try:
                cached = self.client.images.get(image)
                self.verify(cached)
            except ImageNotFound:
                pass
            else:
                self.store.write(
                    worker_id, "cached", content + "Using cached worker image\n", image
                )
                return image
            context = io.BytesIO()
            with tarfile.open(fileobj=context, mode="w") as archive:
                for name, value in sorted(files.items()):
                    info = tarfile.TarInfo(name)
                    info.size, info.mode, info.mtime = len(value), 0o644, 0
                    archive.addfile(info, io.BytesIO(value))
            context.seek(0)
            self.store.write(worker_id, "building", content, image)
            for item in self.client.api.build(
                fileobj=context,
                custom_context=True,
                tag=image,
                rm=True,
                forcerm=True,
                decode=True,
                pull=False,
                use_config_proxy=False,
            ):
                message = (
                    item.get("stream") or item.get("error") or item.get("status") or ""
                )
                if message:
                    content = (
                        content
                        + str(message)
                        + ("\n" if not str(message).endswith("\n") else "")
                    )[-65536:]
                    self.store.write(worker_id, "building", content, image)
                if item.get("error") or item.get("errorDetail"):
                    raise ValueError("Docker build failed")
            self.verify(self.client.images.get(image))
            self.store.write(
                worker_id, "ready", content + "Worker image ready\n", image
            )
            return image
        except Exception as exc:
            # Keep integration exception bodies out of operation errors; detailed
            # Docker stream output is redacted in the bounded build log instead.
            content = (
                content
                + "Image build failed: "
                + type(exc).__name__
                + (": " + str(exc.explanation) if isinstance(exc, APIError) else "")
                + "\n"
            )[-65536:]
            self.store.write(worker_id, "failed", content, image)
            raise ValueError(
                "Worker image build failed; see worker build logs"
            ) from None

    @staticmethod
    def verify(image):
        labels = image.attrs.get("Config", {}).get("Labels") or {}
        if labels.get("software-factory.control-protocol") != PROTOCOL:
            raise ValueError("Worker image has an incompatible control protocol")
