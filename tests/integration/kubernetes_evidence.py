"""Validate the exact public summary allowlist before publishing CI artifacts."""

import json
import os
from pathlib import Path
import re

COMMON = {"schema_version", "fixture_id", "cluster_uid"}
FIELDS = {
    "qualification-prerequisites.json": {
        "probe_image",
        "observations",
        "cleanup_inventory",
        "limitations",
        "versions",
    },
    "qualification-project-jobs.json": {
        "projects",
        "jobs",
        "native_network_modes",
        "usage_calls_per_project",
        "input_tokens_per_project",
        "output_tokens_per_project",
        "reported_usd_per_project",
        "resources",
        "real_components",
        "fixture_components",
    },
    "qualification-rbac.json": {"controller_checks", "template"},
    "qualification-worker-pods.json": {
        "workers",
        "image",
        "private_pvc_retention",
        "restart_identity_preserved",
        "private_history_replay_deduplicated",
        "infrastructure_reconfiguration",
    },
    "qualification-worker-failures.json": {"cases"},
    "qualification-job-faults.json": {
        "cases",
        "one_root_uid_per_create",
        "controller_restart_recovered",
        "duplicate_acceptance_idempotent",
        "initial_adapter_outage_recovered",
        "source_host_writes",
        "paid_model_calls",
    },
    "qualification-environment.json": {
        "versions",
        "images",
        "platforms",
        "source_commit",
        "existing_workloads_restarted",
    },
}
NESTED = {
    "node",
    "pid_probe_spawned",
    "fork_rejected",
    "parent_pod_pid_ceiling",
    "mode",
    "pod_pid_ceiling",
    "approved_egress",
    "dns_denied",
    "external_denied",
    "kind",
    "uid",
    "role",
    "worker_id",
    "deployment_uid",
    "pod_uid",
    "pvc_uid",
    "registered",
    "hatchet_heartbeat_observed",
    "verb",
    "resource",
    "subresource",
    "namespace",
    "allowed",
    "scenario",
    "runtime_status",
    "case",
    "job_id",
    "state",
    "capture_attempts",
    "cleanup_pending",
    "python",
    "kubernetes_sdk",
    "k3d",
    "k3s",
    "kernel",
    "architecture",
    "container_runtime",
    "postgresql",
    "docker",
    "helper_multiarch",
    "helper",
    "worker",
    "control",
    "probe",
    "os",
}
FORBIDDEN = {
    "auth",
    "authorization",
    "token",
    "lease_token",
    "control_token",
    "hatchet_client_token",
    "encryption_key",
    "database_url",
    "kubeconfig",
    "credentials",
    "grants",
    "input",
    "inputs",
    "stdout",
    "stderr",
    "logs",
    "spool",
    "headers",
    "client-key-data",
}


def pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("Duplicate public evidence key")
        result[key] = value
    return result


def validate_public_evidence(directory, *, forbidden_values=(), expected_scope=None):
    directory = Path(directory)
    paths = sorted(directory.glob("qualification-*.json"))
    if not paths or len(paths) > len(FIELDS):
        raise ValueError("Missing or unexpected public evidence inventory")
    secrets = [
        value
        for value in forbidden_values
        if isinstance(value, str) and len(value) >= 8
    ]

    def inspect(value):
        if isinstance(value, dict):
            if any(key.lower() in FORBIDDEN or key not in NESTED for key in value):
                raise ValueError("Private or unknown nested evidence field")
            for item in value.values():
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)
        elif isinstance(value, str):
            if any(secret in value for secret in secrets) or value.startswith(
                ("/Users/", "/home/", "/root/", "Bearer ")
            ):
                raise ValueError("Private credential or host path in public evidence")

    scopes = set()
    for path in paths:
        if path.name not in FIELDS or path.is_symlink() or path.stat().st_size > 262144:
            raise ValueError("Unapproved public evidence file")
        value = json.loads(
            path.read_text(),
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("Nonfinite public evidence")
            ),
        )
        if (
            not isinstance(value, dict)
            or not COMMON <= value.keys()
            or not value.keys() <= COMMON | FIELDS[path.name]
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or not re.fullmatch(r"issue17-[a-z0-9-]{1,32}", str(value["fixture_id"]))
            or not re.fullmatch(
                r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}",
                str(value["cluster_uid"]),
            )
        ):
            raise ValueError("Invalid public evidence schema")
        scope = (value["fixture_id"], value["cluster_uid"])
        scopes.add(scope)
        if len(scopes) > 1 or (expected_scope is not None and scope != expected_scope):
            raise ValueError("Public evidence mixes different pinned fixture scopes")
        for item in value.values():
            inspect(item)
        if "cleanup_inventory" in value and value["cleanup_inventory"] != []:
            raise ValueError("Unresolved private cleanup inventory cannot be published")
    return len(paths)


def main():
    import sys

    secrets = [
        os.environ.get(key, "")
        for key in (
            "HATCHET_CLIENT_TOKEN",
            "GITHUB_TOKEN",
            "GITLAB_TOKEN",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "CONTROL_ENCRYPTION_KEY",
            "CONTROL_ADMIN_PASSWORD",
        )
    ]
    controller = os.environ.get("FLUMIC_KUBERNETES_CONTROLLER_KUBECONFIG")
    if controller:
        private = json.loads(Path(controller).read_text())
        secrets.extend(
            user.get("user", {}).get("token", "") for user in private.get("users", [])
        )
    count = validate_public_evidence(
        sys.argv[1] if len(sys.argv) > 1 else "output/kubernetes-evidence",
        forbidden_values=secrets,
        expected_scope=(
            os.environ["FLUMIC_KUBERNETES_FIXTURE_ID"],
            os.environ["FLUMIC_KUBERNETES_CLUSTER_UID"],
        )
        if os.environ.get("FLUMIC_KUBERNETES_FIXTURE_ID")
        and os.environ.get("FLUMIC_KUBERNETES_CLUSTER_UID")
        else None,
    )
    print("Validated " + str(count) + " allowlisted public Kubernetes summaries")


if __name__ == "__main__":
    main()
