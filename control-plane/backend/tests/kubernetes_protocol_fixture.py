"""Stateful Kubernetes wire fixture; never contacts a cluster or executes images."""

import copy
import io
from types import SimpleNamespace


class ApiFailure(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__("private-server-secret must not escape an adapter")


class KubeProtocol:
    def __init__(self, configuration):
        self.configuration = configuration
        self.objects = {}
        self.calls = []
        self.serial = 0
        self.lose_create = None
        self.lose_patch = None
        self.lose_delete = None
        self.enforce_fence = True
        self.server_defaults = False
        self.namespace_uids = {
            configuration["worker_namespace"]: configuration["worker_namespace_uid"],
            configuration["job_namespace"]: configuration["job_namespace_uid"],
        }
        self.namespace_fences = dict(self.namespace_uids)
        self.log_bytes = b"worker log\n"
        self.clients = {
            group: SimpleNamespace() for group in ("core", "apps", "batch", "network")
        }
        resources = {
            "core": ["pod", "secret", "config_map", "persistent_volume_claim"],
            "apps": ["deployment", "replica_set"],
            "batch": ["job"],
            "network": ["network_policy"],
        }
        for group, names in resources.items():
            for name in names:
                for operation in ("read", "list", "create", "patch", "delete"):
                    method = operation + "_namespaced_" + name
                    setattr(
                        self.clients[group], method, self.handle(group, name, operation)
                    )
        self.clients["core"].read_namespace = self.namespace
        self.clients["core"].read_namespaced_pod_log = self.logs

    def namespace(self, name, **kwargs):
        self.calls.append(("read_namespace", name))
        if name not in self.namespace_uids:
            raise ApiFailure(404)
        return {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": name,
                "uid": self.namespace_uids[name],
                "annotations": {
                    "flumic.io/namespace-uid": self.namespace_fences.get(name)
                },
            },
        }

    def logs(self, name, namespace, **kwargs):
        self.calls.append(("logs", namespace, name, kwargs))
        raw = self.log_bytes
        if kwargs.get("tail_lines") is not None:
            count = kwargs["tail_lines"]
            raw = b"".join(raw.splitlines(keepends=True)[-count:]) if count else b""
        return io.BytesIO(raw[: kwargs.get("limit_bytes", len(raw))])

    def handle(self, group, kind, operation):
        def request(namespace, name=None, body=None, **kwargs):
            self.calls.append((operation, kind, namespace, name))
            if operation == "list":
                items = [
                    copy.deepcopy(v)
                    for (g, k, n, _), v in self.objects.items()
                    if (g, k, n) == (group, kind, namespace)
                ]
                selector = kwargs.get("label_selector", "")
                for part in filter(None, selector.split(",")):
                    key, _, value = part.partition("=")
                    items = [
                        item
                        for item in items
                        if item["metadata"].get("labels", {}).get(key) == value
                    ]
                return {"items": items, "metadata": {"resourceVersion": "1"}}
            if operation == "create":
                name = body["metadata"]["name"]
                if self.enforce_fence and body["metadata"].get("annotations", {}).get(
                    "flumic.io/namespace-uid"
                ) != self.namespace_fences.get(namespace):
                    raise ApiFailure(422)
                if kwargs.get("dry_run") == "All":
                    return copy.deepcopy(body)
            key = (group, kind, namespace, name)
            if operation == "create":
                if key in self.objects:
                    raise ApiFailure(409)
                self.serial += 1
                value = copy.deepcopy(body)
                value["metadata"].update(
                    uid="uid-" + str(self.serial),
                    resourceVersion=str(self.serial),
                    generation=1,
                    namespace=namespace,
                )
                if kind == "deployment":
                    value["status"] = {
                        "observedGeneration": 1,
                        "replicas": value["spec"]["replicas"],
                        "readyReplicas": 0,
                    }
                if self.server_defaults and kind == "deployment":
                    pod = value["spec"]["template"]["spec"]
                    pod["securityContext"] = {}
                    pod["restartPolicy"] = "Always"
                    for volume in pod["volumes"]:
                        if "configMap" in volume:
                            volume["configMap"]["defaultMode"] = 420
                if self.server_defaults and kind == "persistent_volume_claim":
                    value["spec"].update(
                        volumeMode="Filesystem", volumeName="pvc-fixture-volume"
                    )
                    value["spec"]["resources"]["requests"]["storage"] = "0.25Gi"
                    value["metadata"]["annotations"][
                        "pv.kubernetes.io/bind-completed"
                    ] = "yes"
                    value["metadata"]["finalizers"] = ["kubernetes.io/pvc-protection"]
                self.objects[key] = value
                if self.lose_create == kind:
                    self.lose_create = None
                    raise ApiFailure(503)
                return copy.deepcopy(value)
            if key not in self.objects:
                raise ApiFailure(404)
            if operation == "read":
                return copy.deepcopy(self.objects[key])
            if operation == "delete":
                conditions = (body or {}).get("preconditions", {})
                if conditions.get("uid") != self.objects[key]["metadata"]["uid"]:
                    raise ApiFailure(409)
                if (
                    conditions.get("resourceVersion") is not None
                    and conditions["resourceVersion"]
                    != self.objects[key]["metadata"]["resourceVersion"]
                ):
                    raise ApiFailure(409)
                del self.objects[key]
                if self.lose_delete == kind:
                    self.lose_delete = None
                    raise ApiFailure(503)
                return {"status": "Success"}
            if operation == "patch":
                assert isinstance(body, list), (
                    "Mutations must use resourceVersion/UID-tested JSON Patch"
                )
                tests = {p["path"]: p["value"] for p in body if p["op"] == "test"}
                actual = self.objects[key]
                for path in ("/metadata/uid", "/metadata/resourceVersion"):
                    if tests.get(path) != actual["metadata"][path.rsplit("/", 1)[1]]:
                        raise ApiFailure(409)
                for patch in body:
                    if patch["op"] == "test":
                        continue
                    parts = [
                        p.replace("~1", "/").replace("~0", "~")
                        for p in patch["path"].strip("/").split("/")
                    ]
                    current = actual
                    for p in parts[:-1]:
                        current = current.setdefault(p, {})
                    current[parts[-1]] = copy.deepcopy(patch["value"])
                self.serial += 1
                actual["metadata"]["resourceVersion"] = str(self.serial)
                actual["metadata"]["generation"] += 1
                if kind == "deployment":
                    actual["status"].update(
                        observedGeneration=actual["metadata"]["generation"],
                        replicas=actual["spec"]["replicas"],
                    )
                if self.lose_patch == kind:
                    self.lose_patch = None
                    raise ApiFailure(503)
                return copy.deepcopy(actual)
            raise AssertionError("Unsupported fixture operation")

        return request
