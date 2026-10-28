"""No-network qualification guards; writes require independently pinned authority."""

import copy
import unittest
from unittest.mock import Mock


class KubernetesHarnessSafetyTests(unittest.TestCase):
    def values(self):
        return {
            "FLUMIC_LIVE_KUBERNETES": "1",
            "FLUMIC_KUBERNETES_ALLOW_WRITES": "1",
            "FLUMIC_KUBERNETES_KUBECONFIG": "/private/test-config",
            "FLUMIC_KUBERNETES_CONTEXT": "k3d-flumic-issue17",
            "FLUMIC_KUBERNETES_SERVER": "https://127.0.0.1:6551",
            "FLUMIC_KUBERNETES_CLUSTER_UID": "00000000-0000-0000-0000-000000000001",
            "FLUMIC_KUBERNETES_WORKER_NAMESPACE": "flumic-i17-worker-test",
            "FLUMIC_KUBERNETES_WORKER_NAMESPACE_UID": "00000000-0000-0000-0000-000000000002",
            "FLUMIC_KUBERNETES_JOB_NAMESPACE": "flumic-i17-action-test",
            "FLUMIC_KUBERNETES_JOB_NAMESPACE_UID": "00000000-0000-0000-0000-000000000003",
            "FLUMIC_KUBERNETES_FIXTURE_ID": "issue17-test",
        }

    def fixture(self):
        from integration.kubernetes_live_fixture import (
            LiveKubernetesConfig,
            KubernetesLiveFixture,
        )

        config = LiveKubernetesConfig.from_env(self.values())
        client = Mock()
        client.identity.return_value = {
            "context": config.context,
            "server": config.server,
            "cluster_uid": config.cluster_uid,
        }
        client.namespace.side_effect = lambda name: {
            "metadata": {
                "name": name,
                "uid": config.namespace_uids[name],
                "labels": {"flumic.io/qualification": config.fixture_id},
            }
        }
        return KubernetesLiveFixture(config, client), client

    def resource(self, **metadata):
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "fixture-proof",
                "namespace": "flumic-i17-action-test",
                "uid": "resource-uid",
                "resourceVersion": "12",
                "labels": {"flumic.io/qualification": "issue17-test"},
                **metadata,
            },
            "data": {"proof": "safe"},
        }

    def test_opt_in_is_all_or_nothing_and_scope_is_fixed(self):
        from integration.kubernetes_live_fixture import LiveKubernetesConfig

        with self.assertRaises(unittest.SkipTest):
            LiveKubernetesConfig.from_env({})
        for key in self.values():
            if key == "FLUMIC_LIVE_KUBERNETES":
                continue
            values = self.values()
            values.pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                LiveKubernetesConfig.from_env(values)
        for key, value in [
            ("FLUMIC_KUBERNETES_CONTEXT", "production"),
            ("FLUMIC_KUBERNETES_KUBECONFIG", "relative"),
            ("FLUMIC_KUBERNETES_SERVER", "https://remote.example"),
            ("FLUMIC_KUBERNETES_WORKER_NAMESPACE", "default"),
            ("FLUMIC_KUBERNETES_FIXTURE_ID", "*"),
            ("FLUMIC_KUBERNETES_CLUSTER_UID", "not-a-uid"),
        ]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                LiveKubernetesConfig.from_env({**self.values(), key: value})

    def test_wrong_cluster_context_and_namespace_never_write(self):
        for field in ["context", "server", "cluster_uid"]:
            fixture, client = self.fixture()
            client.identity.return_value[field] = "wrong"
            with self.subTest(field=field), self.assertRaises(ValueError):
                fixture.create(self.resource())
            client.create.assert_not_called()
        for changed in [{"uid": "wrong"}, {"labels": {}}]:
            fixture, client = self.fixture()
            original = client.namespace.side_effect

            def namespace(name):
                value = original(name)
                value["metadata"].update(changed)
                return value

            client.namespace.side_effect = namespace
            with self.assertRaises(ValueError):
                fixture.create(self.resource())
            client.create.assert_not_called()

    def test_dry_run_resume_cleanup_is_uid_owner_spec_and_version_guarded(self):
        from integration.kubernetes_live_fixture import KubernetesLiveFixture

        fixture, client = self.fixture()
        resource = self.resource()
        client.create.return_value = resource
        fixture.create(resource)
        resumed = KubernetesLiveFixture(
            fixture.config, client, inventory=fixture.checkpoint()
        )
        client.get.return_value = resource
        resumed.cleanup()
        client.delete.assert_called_once_with(
            "ConfigMap",
            "flumic-i17-action-test",
            "fixture-proof",
            uid="resource-uid",
            resource_version="12",
        )
        self.assertEqual(resumed.checkpoint(), [])
        for change in ["uid", "resourceVersion", "labels", "ownerReferences", "data"]:
            fixture, client = self.fixture()
            client.create.return_value = resource
            fixture.create(resource)
            changed = copy.deepcopy(resource)
            if change == "data":
                changed[change] = {"proof": "changed"}
            else:
                changed["metadata"][change] = {
                    "labels": {"other": "owner"},
                    "ownerReferences": [{"uid": "foreign"}],
                }.get(change, "changed")
            client.get.return_value = changed
            with self.subTest(change=change), self.assertRaises(ValueError):
                fixture.cleanup()
            client.delete.assert_not_called()
            self.assertEqual(len(fixture.checkpoint()), 1)

    def test_pvc_secret_owner_changes_and_namespace_replacement_refuse_cleanup(self):
        for kind in ["PersistentVolumeClaim", "Secret"]:
            fixture, client = self.fixture()
            resource = self.resource()
            resource["kind"] = kind
            client.create.return_value = resource
            fixture.create(resource)
            changed = copy.deepcopy(resource)
            changed["metadata"]["ownerReferences"] = [{"uid": "foreign"}]
            client.get.return_value = changed
            with self.assertRaises(ValueError):
                fixture.cleanup()
            client.delete.assert_not_called()
        fixture, client = self.fixture()
        resource = self.resource()
        client.create.return_value = resource
        fixture.create(resource)
        client.namespace.side_effect = lambda name: {"metadata": {"uid": "replaced"}}
        with self.assertRaises(ValueError):
            fixture.cleanup()
        client.delete.assert_not_called()

    def test_unbounded_or_host_privileged_pods_are_rejected(self):
        fixture, client = self.fixture()
        pod = self.resource()
        pod["kind"] = "Pod"
        pod.pop("data")
        pod["spec"] = {"containers": [{"name": "probe", "image": "fixture:approved"}]}
        with self.assertRaises(ValueError):
            fixture.create(pod)
        pod["spec"]["containers"][0]["resources"] = {
            "limits": {"cpu": "100m", "memory": "32Mi"}
        }
        pod["spec"]["hostNetwork"] = True
        with self.assertRaises(ValueError):
            fixture.create(pod)
        pod["spec"].pop("hostNetwork")
        pod["spec"]["volumes"] = [{"name": "host", "hostPath": {"path": "/"}}]
        with self.assertRaises(ValueError):
            fixture.create(pod)
        client.create.assert_not_called()

    def test_expected_scheduler_binding_is_allowed_but_redirection_is_not(self):
        fixture, client = self.fixture()
        resource = self.resource()
        resource["kind"] = "Pod"
        resource.pop("data")
        resource["spec"] = {
            "nodeSelector": {"kubernetes.io/hostname": "owned-node"},
            "containers": [
                {
                    "name": "probe",
                    "image": "fixture",
                    "resources": {"limits": {"cpu": "100m", "memory": "32Mi"}},
                }
            ],
        }
        client.create.return_value = resource
        fixture.create(resource)
        scheduled = copy.deepcopy(resource)
        scheduled["spec"]["nodeName"] = "owned-node"
        scheduled["metadata"]["resourceVersion"] = "13"
        client.get.return_value = scheduled
        fixture.observe(
            "Pod", scheduled["metadata"]["namespace"], scheduled["metadata"]["name"]
        )
        scheduled["spec"]["nodeName"] = "foreign-node"
        with self.assertRaises(ValueError):
            fixture.observe(
                "Pod", scheduled["metadata"]["namespace"], scheduled["metadata"]["name"]
            )
        client.delete.assert_not_called()

    def test_private_checkpoint_resumes_after_process_interruption(self):
        import json
        import tempfile
        from pathlib import Path
        from integration.kubernetes_live_fixture import KubernetesLiveFixture

        original, client = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.json"
            fixture = KubernetesLiveFixture(
                original.config, client, checkpoint_path=path
            )
            client.create.return_value = self.resource()
            fixture.create(self.resource())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(json.loads(path.read_text())), 1)
            resumed = KubernetesLiveFixture(
                original.config, client, checkpoint_path=path
            )
            client.get.return_value = self.resource()
            resumed.cleanup()
            self.assertEqual(json.loads(path.read_text()), [])

    def test_concurrent_live_processes_are_rejected_before_writes(self):
        import tempfile
        from pathlib import Path
        from integration.kubernetes_live_fixture import acquire_fixture_lock

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scope.lock"
            first = acquire_fixture_lock(path)
            try:
                with self.assertRaises(ValueError):
                    acquire_fixture_lock(path)
            finally:
                first.close()
            acquire_fixture_lock(path).close()

    def test_malformed_checkpoint_names_cannot_become_kubectl_flags_or_paths(self):
        from integration.kubernetes_live_fixture import KubernetesLiveFixture

        original, client = self.fixture()
        valid = original.record(self.resource())
        for field, value in [
            ("name", "--raw=/api/v1/secrets"),
            ("name", "../../nodes"),
            ("uid", "foreign/uid"),
            ("resource_version", "not-a-version"),
            ("fingerprint", "unverified"),
        ]:
            entry = {**valid, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                KubernetesLiveFixture(original.config, client, inventory=[entry])
        client.get.assert_not_called()
        client.delete.assert_not_called()

    def test_cluster_bootstrap_requires_a_fresh_owned_local_cluster(self):
        from integration.kubernetes_cluster_setup import validate_bootstrap_identity

        values = {
            "context": "k3d-flumic-issue17",
            "server": "https://127.0.0.1:6551",
            "expected_uid": "00000000-0000-0000-0000-000000000001",
            "observed_uid": "00000000-0000-0000-0000-000000000001",
            "node_labels": {
                name: {"k3d.cluster": "flumic-issue17"}
                for name in [
                    "k3d-flumic-issue17-server-0",
                    "k3d-flumic-issue17-agent-0",
                ]
            },
            "namespaces": ["default", "kube-system", "kube-public", "kube-node-lease"],
        }
        validate_bootstrap_identity(**values)
        for field, value in [
            ("context", "production"),
            ("server", "https://remote.example"),
            ("observed_uid", "00000000-0000-0000-0000-000000000099"),
            ("node_labels", {"user-node": {"k3d.cluster": "other"}}),
            ("namespaces", values["namespaces"] + ["user-workload"]),
        ]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_bootstrap_identity(**{**values, field: value})

    def test_ci_registry_config_generator_produces_a_real_mirror_mapping(self):
        import os
        from pathlib import Path
        import shlex
        import subprocess
        import sys
        import tempfile
        import yaml

        root = Path(__file__).resolve().parents[2]
        workflow = yaml.safe_load((root / ".github/workflows/tests.yml").read_text())
        job = workflow["jobs"]["kubernetes-contracts"]
        step = next(
            step
            for step in job["steps"]
            if step.get("name")
            == "Create only the owned registry, cluster and fixture database"
        )
        line = next(
            line for line in step["run"].splitlines() if line.startswith("python -c ")
        )
        program = shlex.split(line)[2]
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(
                [sys.executable, "-c", program],
                env={**os.environ, "RUNNER_TEMP": directory},
                check=True,
                capture_output=True,
            )
            actual = yaml.safe_load(
                (Path(directory) / "flumic-registry.yaml").read_text()
            )
        self.assertEqual(
            actual,
            {
                "mirrors": {
                    "localhost:5117": {
                        "endpoint": ["http://k3d-flumic-issue17-registry:5000"]
                    }
                }
            },
        )

    def test_public_evidence_rejects_credentials_unknown_files_and_private_payloads(
        self,
    ):
        import json
        import tempfile
        from pathlib import Path
        from integration.kubernetes_evidence import validate_public_evidence

        valid = {
            "schema_version": 1,
            "fixture_id": "issue17-test",
            "cluster_uid": "00000000-0000-0000-0000-000000000001",
            "projects": 2,
            "jobs": 2,
            "input_tokens_per_project": 4,
            "output_tokens_per_project": 6,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "qualification-project-jobs.json"
            path.write_text(json.dumps(valid))
            validate_public_evidence(root)
            other = root / "qualification-rbac.json"
            other.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "fixture_id": "issue17-other",
                        "cluster_uid": valid["cluster_uid"],
                        "controller_checks": [],
                    }
                )
            )
            with self.assertRaises(ValueError):
                validate_public_evidence(root)
            other.unlink()
            for change in [
                {"auth": {"token": "private-token-value"}},
                {
                    "resources": [
                        {"kind": "Secret", "credentials": "private-token-value"}
                    ]
                },
                {"fixture_components": ["private-token-value"]},
            ]:
                path.write_text(json.dumps({**valid, **change}))
                with self.assertRaises(ValueError):
                    validate_public_evidence(
                        root, forbidden_values=["private-token-value"]
                    )
            path.unlink()
            (root / "qualification-debug.json").write_text(json.dumps(valid))
            with self.assertRaises(ValueError):
                validate_public_evidence(root)
            (root / "qualification-debug.json").unlink()
            path.write_text('{"schema_version":1,"schema_version":2}')
            with self.assertRaises(ValueError):
                validate_public_evidence(root)

    def test_budget_and_scope_rejected_before_effects(self):
        fixture, client = self.fixture()
        for kind, ns in [
            ("Namespace", ""),
            ("Pod", "default"),
            ("ClusterRole", "flumic-i17-action-test"),
        ]:
            resource = self.resource(namespace=ns)
            resource["kind"] = kind
            with self.assertRaises(ValueError):
                fixture.create(resource)
        unowned = self.resource(labels={})
        with self.assertRaises(ValueError):
            fixture.create(unowned)
        huge = self.resource()
        huge["data"] = {"value": "x" * 1048577}
        with self.assertRaises(ValueError):
            fixture.create(huge)
        client.create.assert_not_called()
        client.create.side_effect = lambda value: value
        for index in range(64):
            fixture.create(self.resource(name="proof-" + str(index)))
        with self.assertRaises(ValueError):
            fixture.create(self.resource(name="overflow"))
        self.assertEqual(client.create.call_count, 64)
