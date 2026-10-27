"""Bounded, namespace-pinned Kubernetes I/O with no ambient client authority."""

import copy
from contextlib import contextmanager
import json
import re
import time
import uuid
from urllib.parse import urlsplit

from app.runtime_targets import TargetSnapshot

NAMESPACE_FENCE = "flumic.io/namespace-uid"
MAX_RESPONSE = 8 * 1048576
MAX_LOG = 262144
_ALLOWED = {
    "core": {"pod", "secret", "config_map", "persistent_volume_claim"},
    "apps": {"deployment", "replica_set"},
    "batch": {"job"},
    "network": {"network_policy"},
}


class KubernetesError(RuntimeError):
    def __init__(self, status=None):
        self.status = status if type(status) is int else None
        super().__init__("Kubernetes request failed; inspect scoped runtime metadata")


class KubernetesApi:
    def __init__(self, target, *, clients=None, effect_guard=None):
        self.target = TargetSnapshot.model_validate(target.model_dump())
        if self.target.kind != "kubernetes":
            raise ValueError("Kubernetes target required")
        self.effect_guard = effect_guard
        self.configuration = None
        self.api_client = None
        if clients is not None:
            self.clients = clients
            return
        try:
            from kubernetes import client, config

            configuration = client.Configuration()
            auth = self.target.configuration["auth"]
            if auth["mode"] == "kubeconfig":
                config.load_kube_config(
                    config_file=auth["kubeconfig"],
                    context=auth["context"],
                    client_configuration=configuration,
                    persist_config=False,
                )
            else:
                config.load_incluster_config(client_configuration=configuration)
            endpoint = urlsplit(configuration.host)
            if (
                not configuration.verify_ssl
                or endpoint.scheme != "https"
                or endpoint.username is not None
                or endpoint.password is not None
            ):
                raise ValueError("Verified Kubernetes TLS is required")
            configuration.debug = False
            self.configuration = configuration
            self.api_client = client.ApiClient(configuration)
            self.clients = {
                "core": client.CoreV1Api(self.api_client),
                "apps": client.AppsV1Api(self.api_client),
                "batch": client.BatchV1Api(self.api_client),
                "network": client.NetworkingV1Api(self.api_client),
            }
        except Exception:
            raise ValueError(
                "Explicit Kubernetes client configuration is unavailable"
            ) from None

    def _raw(self, group, method, **kwargs):
        response = None
        try:
            response = getattr(self.clients[group], method)(
                **kwargs, _request_timeout=(3, 10), _preload_content=False
            )
            if isinstance(response, (dict, list)):
                if len(json.dumps(response, allow_nan=False).encode()) > MAX_RESPONSE:
                    raise ValueError()
                return copy.deepcopy(response)
            data = bytearray()
            deadline = time.monotonic() + 10
            while len(data) <= MAX_RESPONSE:
                if time.monotonic() >= deadline:
                    raise ValueError()
                chunk = response.read(min(16384, MAX_RESPONSE + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > MAX_RESPONSE:
                raise ValueError()
            return json.loads(data)
        except KubernetesError:
            raise
        except Exception as error:
            raise KubernetesError(getattr(error, "status", None)) from None
        finally:
            if response is not None and not isinstance(response, (dict, list)):
                response.close()

    def require_namespace(self, kind):
        if kind not in {"worker", "job"}:
            raise ValueError("Unknown runtime namespace kind")
        config = self.target.configuration
        namespace = config[kind + "_namespace"]
        value = self._raw("core", "read_namespace", name=namespace)
        if value.get("metadata", {}).get("uid") != config[kind + "_namespace_uid"]:
            raise ValueError("Kubernetes namespace identity changed")
        return namespace

    def check_effect(self):
        if self.effect_guard is None:
            raise ValueError("Kubernetes client has no mutation authority")
        self.effect_guard()

    def _admission_fence(self, kind):
        namespace = self.require_namespace(kind)
        expected = self.target.configuration[kind + "_namespace_uid"]
        metadata = self._raw("core", "read_namespace", name=namespace).get(
            "metadata", {}
        )
        if metadata.get("annotations", {}).get(NAMESPACE_FENCE) != expected:
            raise ValueError("Kubernetes namespace admission bootstrap is unavailable")
        name = "flumic-fence-probe-" + uuid.uuid4().hex[:16]
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "annotations": {NAMESPACE_FENCE: expected},
            },
            "data": {},
        }
        self._raw(
            "core",
            "create_namespaced_config_map",
            namespace=namespace,
            body=body,
            dry_run="All",
        )
        body["metadata"]["annotations"][NAMESPACE_FENCE] = "invalid-" + uuid.uuid4().hex
        try:
            self._raw(
                "core",
                "create_namespaced_config_map",
                namespace=namespace,
                body=body,
                dry_run="All",
            )
        except KubernetesError as error:
            if error.status not in {400, 403, 422}:
                raise ValueError(
                    "Kubernetes namespace admission fence is unavailable"
                ) from None
        else:
            raise ValueError("Kubernetes namespace admission fence is not enforced")
        self.require_namespace(kind)

    def call(self, group, method, *, namespace_kind, **kwargs):
        if "namespace" in kwargs or any(k.startswith("_") for k in kwargs):
            raise ValueError(
                "Runtime namespace and transport bounds cannot be overridden"
            )
        match = re.fullmatch(r"(read|list|create|patch|delete)_namespaced_(.+)", method)
        if not match or match[2] not in _ALLOWED.get(group, set()):
            raise ValueError("Unsupported Kubernetes runtime operation")
        mutation = match[1] in {"create", "patch", "delete"}
        if group == "apps" and match[2] == "replica_set" and mutation:
            raise ValueError("ReplicaSets are observed, not managed directly")
        if mutation:
            self.check_effect()
        namespace = self.require_namespace(namespace_kind)
        kwargs = copy.deepcopy(kwargs)
        if match[1] == "patch":
            body = kwargs.get("body")
            tests = (
                {
                    item.get("path"): item.get("value")
                    for item in body or []
                    if isinstance(item, dict) and item.get("op") == "test"
                }
                if isinstance(body, list)
                else {}
            )
            if not all(
                isinstance(tests.get(path), str) and tests[path]
                for path in ("/metadata/uid", "/metadata/resourceVersion")
            ):
                raise ValueError(
                    "Kubernetes patch requires UID and resourceVersion tests"
                )
        if match[1] == "delete":
            body = kwargs.get("body", {})
            uid = (
                body.get("preconditions", {}).get("uid")
                if isinstance(body, dict)
                else None
            )
            if not isinstance(uid, str) or not uid:
                raise ValueError("Kubernetes delete requires a UID precondition")
        if match[1] == "create":
            body = kwargs.get("body", {})
            meta = body.setdefault("metadata", {})
            if meta.get("namespace", namespace) != namespace:
                raise ValueError("Resource namespace mismatch")
            meta["namespace"] = namespace
            annotations = meta.setdefault("annotations", {})
            expected = self.target.configuration[namespace_kind + "_namespace_uid"]
            if annotations.get(NAMESPACE_FENCE, expected) != expected:
                raise ValueError("Resource namespace pin mismatch")
            annotations[NAMESPACE_FENCE] = expected
        if match[1] in {"create", "patch"}:
            self._admission_fence(namespace_kind)
        if mutation:
            self.check_effect()
        value = self._raw(group, method, namespace=namespace, **kwargs)
        self.require_namespace(namespace_kind)
        if isinstance(value, dict) and value.get("metadata", {}).get("continue"):
            raise KubernetesError()
        return value

    def pod_logs(self, name, *, namespace_kind, container, expected_uid, limit=MAX_LOG):
        if not isinstance(name, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9.-]{0,252}", name
        ):
            raise ValueError("Invalid Pod log identity")
        limit = max(0, min(int(limit), MAX_LOG))
        namespace = self.require_namespace(namespace_kind)

        def require_pod():
            pod = self._raw(
                "core", "read_namespaced_pod", name=name, namespace=namespace
            )
            if not expected_uid or pod.get("metadata", {}).get("uid") != expected_uid:
                raise ValueError("Kubernetes log Pod identity changed")

        require_pod()
        response = None
        try:
            response = self.clients["core"].read_namespaced_pod_log(
                name=name,
                namespace=namespace,
                container=container,
                limit_bytes=limit + 1,
                _request_timeout=(3, 10),
                _preload_content=False,
            )
            raw = response.read(limit + 1)
            require_pod()
            self.require_namespace(namespace_kind)
            return raw[:limit].decode("utf-8", errors="replace").encode()[
                :limit
            ].decode("utf-8", errors="ignore"), len(raw) > limit
        except ValueError:
            raise
        except Exception as error:
            raise KubernetesError(getattr(error, "status", None)) from None
        finally:
            if response is not None:
                response.close()

    def close(self):
        if self.api_client is not None:
            self.api_client.close()

    def collector_exec(self, name, *, expected_uid, mode):
        """Only fixed trusted collector commands, on a separately owned stream."""
        from kubernetes.stream import stream
        from app.adapters.kubernetes_job_manifests import HELPER_COMMAND
        from factory.workflows.references import json_bytes

        if (
            mode not in {"capture", "release"}
            or not isinstance(name, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", name)
        ):
            raise ValueError("Unsupported collector invocation")
        namespace = self.require_namespace("job")

        def require_pod():
            pod = self._raw(
                "core", "read_namespaced_pod", name=name, namespace=namespace
            )
            if not expected_uid or pod.get("metadata", {}).get("uid") != expected_uid:
                raise ValueError("Collector Pod identity changed")

        require_pod()
        self.check_effect()
        response = None
        output, diagnostic = bytearray(), bytearray()
        try:
            with self.exec_client() as client:
                response = stream(
                    client.connect_get_namespaced_pod_exec,
                    name=name,
                    namespace=namespace,
                    container="collector",
                    command=HELPER_COMMAND + [mode],
                    stdin=False,
                    stdout=True,
                    stderr=True,
                    tty=False,
                    _preload_content=False,
                    _request_timeout=(3, 10),
                )
                deadline = time.monotonic() + 15
                while response.is_open():
                    if time.monotonic() >= deadline:
                        raise ValueError("Collector exec exceeded bounded duration")
                    response.update(timeout=1)
                    for channel, destination in (
                        ("stdout", output),
                        ("stderr", diagnostic),
                    ):
                        if getattr(response, "peek_" + channel)(timeout=0):
                            destination.extend(
                                getattr(response, "read_" + channel)(timeout=0).encode()
                            )
                    if len(output) + len(diagnostic) > 1048576:
                        raise ValueError("Collector exec exceeded response bounds")
                if response.returncode != 0:
                    raise ValueError("Collector command did not complete successfully")
            require_pod()
            self.require_namespace("job")
            self.check_effect()
            if mode == "release":
                if output:
                    raise ValueError("Collector release returned unexpected output")
                return None

            def pairs(items):
                value = {}
                for key, item in items:
                    if key in value:
                        raise ValueError("Duplicate collector JSON field")
                    value[key] = item
                return value

            def nonfinite(_):
                raise ValueError("Nonfinite collector JSON")

            try:
                value = json.loads(
                    output, object_pairs_hook=pairs, parse_constant=nonfinite
                )
                if not isinstance(value, dict) or len(json_bytes(value)) > 1048576:
                    raise ValueError()
            except (ValueError, TypeError, RecursionError):
                raise ValueError("Collector returned invalid bounded JSON") from None
            return value
        except (ValueError, KubernetesError):
            raise
        except Exception as error:
            raise KubernetesError(getattr(error, "status", None)) from None
        finally:
            if response is not None:
                response.close()

    @contextmanager
    def exec_client(self):
        """Streaming changes transport; never lend it the REST client's pool."""
        from kubernetes import client

        if self.configuration is None:
            raise ValueError("Explicit exec transport is unavailable")
        transport = client.ApiClient(copy.deepcopy(self.configuration))
        try:
            yield client.CoreV1Api(transport)
        finally:
            transport.close()
