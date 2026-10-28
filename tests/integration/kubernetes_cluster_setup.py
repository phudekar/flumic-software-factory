"""Operator bootstrap for a NEW dedicated k3d qualification cluster, never teardown.

Requires both live/write opt-ins, --allow-bootstrap and an independently read
kube-system UID. Refuses any cluster with existing application namespaces.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from integration.kubernetes_live_fixture import UUID, MARKER

NODES = {"k3d-flumic-issue17-server-0", "k3d-flumic-issue17-agent-0"}
SYSTEM_NAMESPACES = {"default", "kube-system", "kube-public", "kube-node-lease"}


def validate_bootstrap_identity(
    *, context, server, expected_uid, observed_uid, node_labels, namespaces
):
    if (
        context != "k3d-flumic-issue17"
        or server != "https://127.0.0.1:6551"
        or not UUID.fullmatch(expected_uid)
        or observed_uid != expected_uid
        or set(node_labels) != NODES
        or any(
            labels.get("k3d.cluster") != "flumic-issue17"
            for labels in node_labels.values()
        )
        or set(namespaces) != SYSTEM_NAMESPACES
    ):
        raise ValueError(
            "Bootstrap requires the pinned, fresh, owned local k3d cluster"
        )


def run(args, body=None, *, allow_failure=False):
    result = subprocess.run(
        args,
        input=None if body is None else json.dumps(body),
        capture_output=True,
        text=True,
        timeout=40,
    )
    if (result.returncode and not allow_failure) or len(
        result.stdout.encode()
    ) > 8 * 1048576:
        raise ValueError("Scoped fixture bootstrap request failed")
    return result


def data(args, body=None):
    return json.loads(run(args, body).stdout)


def private_json(path, value):
    with open(
        path, "w", opener=lambda filename, flags: os.open(filename, flags, 0o600)
    ) as out:
        json.dump(value, out, allow_nan=False)
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-bootstrap", action="store_true")
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--docker-context", required=True)
    parser.add_argument("--expected-cluster-uid", required=True)
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--github-env")
    args = parser.parse_args()
    if (
        not args.allow_bootstrap
        or os.environ.get("FLUMIC_LIVE_KUBERNETES") != "1"
        or os.environ.get("FLUMIC_KUBERNETES_ALLOW_WRITES") != "1"
        or not Path(args.kubeconfig).is_absolute()
        or not Path(args.state_dir).is_absolute()
        or not re.fullmatch(r"issue17-[a-z0-9-]{1,24}", args.fixture_id)
        or any(
            any(ord(c) < 32 for c in value)
            for value in [
                args.kubeconfig,
                args.state_dir,
                args.context,
                args.docker_context,
            ]
        )
    ):
        raise ValueError(
            "Explicit pinned local bootstrap scope and write approval required"
        )
    kube = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        args.context,
        "--request-timeout=15s",
    ]
    docker = ["docker", "--context", args.docker_context]
    selected = data(kube + ["config", "view", "--minify", "-o", "json"])
    cluster_uid = data(kube + ["get", "namespace", "kube-system", "-o", "json"])[
        "metadata"
    ]["uid"]
    nodes = data(kube + ["get", "nodes", "-o", "json"])["items"]
    if {node["metadata"]["name"] for node in nodes} != NODES:
        raise ValueError("Unexpected API node inventory")
    labels = {
        name: data(docker + ["inspect", name])[0]["Config"]["Labels"] for name in NODES
    }
    namespaces = data(kube + ["get", "namespaces", "-o", "json"])["items"]
    validate_bootstrap_identity(
        context=selected.get("current-context"),
        server=selected["clusters"][0]["cluster"]["server"],
        expected_uid=args.expected_cluster_uid,
        observed_uid=cluster_uid,
        node_labels=labels,
        namespaces=[value["metadata"]["name"] for value in namespaces],
    )
    state = Path(args.state_dir)
    state.mkdir(mode=0o700, parents=True, exist_ok=False)
    suffix = args.fixture_id.removeprefix("issue17-")
    worker = "flumic-i17-worker-" + suffix
    actions = "flumic-i17-action-" + suffix
    pins = {}
    for name in [worker, actions]:
        value = data(
            kube + ["create", "-f", "-", "-o", "json"],
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": name,
                    "labels": {
                        MARKER: args.fixture_id,
                        "flumic.io/namespace-fence": "enforced",
                    },
                },
            },
        )
        pins[name] = value["metadata"]["uid"]
        private_json(state / "namespace-inventory.json", pins)
    root = Path(__file__).resolve().parents[2]
    run(
        [
            sys.executable,
            str(root / "deploy/kubernetes/bootstrap-fence.py"),
            "--kubeconfig",
            args.kubeconfig,
            "--context",
            args.context,
            worker,
            actions,
        ]
    )
    run(kube + ["apply", "-f", str(root / "deploy/kubernetes/admission-policy.yaml")])
    # Wait for CEL type checking, rather than assuming successful apply compiled it.
    import time

    for _ in range(20):
        policy = data(
            kube
            + [
                "get",
                "validatingadmissionpolicy",
                "flumic-namespace-fence",
                "-o",
                "json",
            ]
        )
        status = policy.get("status", {})
        if status.get("observedGeneration") == policy["metadata"]["generation"]:
            if status.get("typeChecking", {}).get("expressionWarnings"):
                raise ValueError("Namespace fence policy did not compile")
            break
        time.sleep(0.5)
    else:
        raise ValueError("Namespace fence compilation was not observed")
    controller = "flumic-i17-" + suffix + "-controller"
    reader = "flumic-i17-" + suffix + "-namespace-reader"
    mapping = {
        "flumic-workers": worker,
        "flumic-actions": actions,
        "flumic-control": worker,
        "flumic-controller": controller,
        "flumic-namespace-reader": reader,
    }

    def replace_values(value):
        if isinstance(value, str):
            return mapping.get(value, value)
        if isinstance(value, list):
            return [replace_values(item) for item in value]
        if isinstance(value, dict):
            return {key: replace_values(item) for key, item in value.items()}
        return value

    roles = replace_values(
        json.loads((root / "deploy/kubernetes/rbac.yaml").read_text().split("\n", 1)[1])
    )
    roles["items"].insert(
        0,
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": controller, "namespace": worker},
        },
    )
    for item in roles["items"]:
        item["metadata"].setdefault("labels", {})[MARKER] = args.fixture_id
    run(kube + ["apply", "-f", "-"], roles)
    token = run(
        kube + ["create", "token", controller, "-n", worker, "--duration=2h"]
    ).stdout.strip()
    admin = data(kube + ["config", "view", "--raw", "--minify", "-o", "json"])
    ca = admin["clusters"][0]["cluster"].get("certificate-authority-data")
    if not ca:
        raise ValueError("Dedicated kubeconfig must embed its cluster CA")
    controller_path = state / "controller-kubeconfig.json"
    private_json(
        controller_path,
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": "fixture",
                    "cluster": {
                        "server": "https://127.0.0.1:6551",
                        "certificate-authority-data": ca,
                    },
                }
            ],
            "users": [{"name": "controller", "user": {"token": token}}],
            "contexts": [
                {
                    "name": args.context,
                    "context": {"cluster": "fixture", "user": "controller"},
                }
            ],
            "current-context": args.context,
        },
    )
    limited = [
        "kubectl",
        "--kubeconfig",
        str(controller_path),
        "--context",
        args.context,
    ]
    checks = []
    for verb, resource, namespace, subresource, allowed in [
        ("get", "namespaces/" + worker, None, None, True),
        ("get", "namespaces/" + actions, None, None, True),
        ("create", "jobs", actions, None, True),
        ("create", "pods", actions, "exec", True),
        ("get", "pods", actions, "exec", True),
        ("create", "deployments", worker, None, True),
        ("get", "pods", worker, "log", True),
        ("get", "nodes", None, None, False),
        ("get", "nodes", None, "proxy", False),
        ("list", "namespaces", None, None, False),
        ("patch", "namespaces/" + worker, None, None, False),
        ("create", "pods", actions, None, False),
        ("get", "clusterroles", None, None, False),
    ]:
        command = (
            limited
            + ["auth", "can-i", verb, resource]
            + (["--subresource=" + subresource] if subresource else [])
            + (["-n", namespace] if namespace else [])
        )
        result = run(command, allow_failure=True)
        if (result.stdout.strip() == "yes") != allowed:
            raise ValueError("Controller RBAC qualification failed")
        checks.append(
            {
                "verb": verb,
                "resource": resource,
                "subresource": subresource,
                "namespace": namespace,
                "allowed": allowed,
            }
        )
    public = root / "output/kubernetes-evidence"
    public.mkdir(parents=True, exist_ok=True)
    (public / "qualification-rbac.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fixture_id": args.fixture_id,
                "cluster_uid": cluster_uid,
                "controller_checks": checks,
                "template": "deploy/kubernetes/rbac.yaml",
            },
            indent=2,
        )
        + "\n"
    )
    env = {
        "FLUMIC_LIVE_KUBERNETES": "1",
        "FLUMIC_KUBERNETES_ALLOW_WRITES": "1",
        "FLUMIC_KUBERNETES_KUBECONFIG": args.kubeconfig,
        "FLUMIC_KUBERNETES_CONTEXT": args.context,
        "FLUMIC_KUBERNETES_DOCKER_CONTEXT": args.docker_context,
        "FLUMIC_KUBERNETES_SERVER": "https://127.0.0.1:6551",
        "FLUMIC_KUBERNETES_CLUSTER_UID": cluster_uid,
        "FLUMIC_KUBERNETES_WORKER_NAMESPACE": worker,
        "FLUMIC_KUBERNETES_WORKER_NAMESPACE_UID": pins[worker],
        "FLUMIC_KUBERNETES_JOB_NAMESPACE": actions,
        "FLUMIC_KUBERNETES_JOB_NAMESPACE_UID": pins[actions],
        "FLUMIC_KUBERNETES_FIXTURE_ID": args.fixture_id,
        "FLUMIC_KUBERNETES_CHECKPOINT": str(state / "inventory.json"),
        "FLUMIC_KUBERNETES_CONTROLLER_KUBECONFIG": str(controller_path),
    }
    private_json(state / "environment.json", env)
    if args.github_env:
        with open(args.github_env, "a") as out:
            for key, value in env.items():
                out.write(key + "=" + value + "\n")
    print(
        "Dedicated namespaces, admission fence and restricted controller prepared; credentials remain private"
    )


if __name__ == "__main__":
    main()
