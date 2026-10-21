"""Docker-specific host paths, resource labels, and credential delivery."""

import os
import docker
from docker.errors import NotFound

LABEL = "software-factory.control-plane"


class DockerRuntime:
    def __init__(self, settings, config_service, client=None):
        self.settings, self.config_service = settings, config_service
        self.client = client or docker.from_env(timeout=10)

    def list(self):
        result = []
        for container in self.client.containers.list(all=True):
            labels = container.labels or {}
            managed = labels.get(LABEL) == "factory"
            discovered = labels.get(
                "com.docker.compose.project"
            ) == self.settings.project and labels.get("com.docker.compose.service") in {
                "worker",
                "developer-worker",
            }
            if not managed and not discovered:
                continue
            result.append(
                {
                    "id": container.id,
                    "instance_id": labels.get("factory.instance"),
                    "name": container.name,
                    "status": container.status,
                    "image": container.attrs["Config"]["Image"],
                    "managed": managed,
                    "profile_refresh_supported": labels.get(
                        "software-factory.control-protocol"
                    )
                    == "2",
                    "profile": labels.get("factory.profile"),
                    "revision_id": labels.get("factory.revision"),
                    "target_id": "local-docker",
                }
            )
        return result

    def container(self, worker_id):
        items = self.client.containers.list(
            all=True,
            filters={"label": [f"{LABEL}=factory", f"factory.instance={worker_id}"]},
        )
        if len(items) != 1:
            raise ValueError("Managed worker runtime was not found")
        return items[0]

    def create(self, worker, document, token):
        try:
            return self.container(worker.id).id
        except ValueError:
            pass
        self.config_service.validate(document)
        snapshot = self.config_service.snapshot(
            document, worker.profile, worker.name, worker.revision_id
        )
        directory = self.settings.snapshot_root / worker.id / worker.revision_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "snapshot.yaml"
        import yaml

        encoded = yaml.safe_dump(snapshot, sort_keys=False)
        if path.exists():
            if path.read_text() != encoded:
                raise ValueError("Configuration snapshots are immutable")
        else:
            path.write_text(encoded)
            path.chmod(0o444)
        role = snapshot["defaults"]["worker_type"]
        image = (
            "hatchet-test-developer-worker:latest"
            if role == "developer"
            else "hatchet-test-worker:latest"
        )
        approved_image = self.client.images.get(image)  # Never pull arbitrary images.
        if (approved_image.attrs.get("Config", {}).get("Labels") or {}).get(
            "software-factory.control-protocol"
        ) != "2":
            raise ValueError(
                "Rebuild worker images before creating instrumented workers"
            )
        credentials = {
            key: os.environ[key]
            for key in [
                "HATCHET_CLIENT_TOKEN",
                "GITHUB_TOKEN",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
            ]
            if os.environ.get(key)
        }
        if role == "developer" and os.environ.get("DEVELOPER_OPENAI_API_KEY"):
            credentials["OPENAI_API_KEY"] = os.environ["DEVELOPER_OPENAI_API_KEY"]
        if not credentials.get("HATCHET_CLIENT_TOKEN") or not credentials.get(
            "GITHUB_TOKEN"
        ):
            raise ValueError(
                "Configure Hatchet and GitHub credentials on the control-plane server"
            )
        host = self.settings.host_root
        container = self.client.containers.create(
            image,
            name=worker.name,
            environment={
                **credentials,
                "WORKER_CONFIG_FILE": "/config/snapshot.yaml",
                "WORKER_PROFILE": worker.profile,
                "FACTORY_CONTROL_ENDPOINT": self.settings.worker_endpoint,
                "FACTORY_CONTROL_TOKEN": token,
                "FACTORY_CONTROL_WORKER_ID": worker.id,
            },
            labels={
                LABEL: "factory",
                "factory.instance": worker.id,
                "factory.profile": worker.profile,
                "factory.revision": worker.revision_id,
            },
            volumes={
                str(
                    host
                    / "data/control-plane/snapshots"
                    / worker.id
                    / worker.revision_id
                ): {
                    "bind": "/config",
                    "mode": "ro",
                },
                str(host / "data"): {"bind": "/data", "mode": "rw"},
                str(host / "observability/model-pricing.json"): {
                    "bind": "/observability/model-pricing.json",
                    "mode": "ro",
                },
            },
            network="worker-network",
            mem_limit="2g",
            nano_cpus=2_000_000_000,
            restart_policy={"Name": "unless-stopped"},
            detach=True,
        )
        try:
            self.client.networks.get("factory-signoz-network").connect(container)
        except NotFound:
            pass
        return container.id

    def configure(self, worker, document, token):
        try:
            current = self.container(worker.id)
        except ValueError:
            current = None
        if current is not None:
            if current.status == "running":
                raise ValueError("Stop the worker before changing its configuration")
            current.remove()
        return self.create(worker, document, token)

    def action(self, worker_id, action):
        container = self.container(worker_id)
        if action == "start":
            container.start()
        elif action == "stop":
            container.stop(timeout=30)
        elif action == "remove":
            if container.status == "running":
                raise ValueError("Stop the worker before removing its runtime")
            container.remove()
        else:
            raise ValueError("Unsupported runtime action")

    def logs(self, runtime_id, limit=200):
        container = self.client.containers.get(runtime_id)
        labels = container.labels or {}
        managed = labels.get(LABEL) == "factory"
        discovered = labels.get(
            "com.docker.compose.project"
        ) == self.settings.project and labels.get("com.docker.compose.service") in {
            "worker",
            "developer-worker",
        }
        if not (managed or discovered):
            raise ValueError("Container is outside this factory")
        return container.logs(tail=min(limit, 500), timestamps=True).decode(
            "utf-8", errors="replace"
        )[-65536:]
