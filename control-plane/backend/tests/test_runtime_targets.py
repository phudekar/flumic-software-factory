"""Runtime pins must not redirect effects or silently weaken stage constraints."""

import copy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from app.settings import AppSettings
from app.job_contracts import JobLaunch, RuntimeJobRef, JobCapture
from factory.workflows.definitions import DockerStage, BuiltinStage

IMAGE = "registry.example/worker@sha256:" + "a" * 64


def target():
    return {
        "id": "test-kubernetes",
        "name": "Test Kubernetes",
        "kind": "kubernetes",
        "deployment_id": "test-deployment",
        "auth": {
            "mode": "kubeconfig",
            "kubeconfig": "/private/test-kubeconfig",
            "context": "test-context",
        },
        "worker_namespace": "flumic-workers",
        "worker_namespace_uid": "11111111-1111-4111-8111-111111111111",
        "job_namespace": "flumic-actions",
        "job_namespace_uid": "22222222-2222-4222-8222-222222222222",
        "images": {"simple": IMAGE, "developer": IMAGE, "helper": IMAGE},
        "storage_class": "local-path",
        "worker_endpoint": "http://control.test:8000",
        "network_qualified": True,
        "network_evidence": "fixture-network-v1",
        "network_policy_names": {"none": "flumic-deny", "bridge": "flumic-egress"},
        "pid_qualified": True,
        "pid_evidence": "fixture-pids-v1",
        "pid_limit": 128,
    }


def stage(**extra):
    return DockerStage.model_validate(
        {
            "id": "check",
            "executor": "docker",
            "role": "simple",
            "pool_id": "pool",
            "image": IMAGE,
            "command": ["/bin/check"],
            **extra,
        }
    )


def old_launch():
    return dict(
        job_id="a" * 64,
        project_id="p",
        execution_id="e",
        spec_digest="b" * 64,
        image=IMAGE,
        command=["/bin/check"],
        inputs={},
    )


class RuntimeTargetsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.settings = AppSettings(
            database_url="sqlite://",
            admin_password="fixture",
            encryption_key="fixture",
            root=root,
            host_root=root,
            snapshot_root=root / "snapshots",
            frontend=root / "frontend",
        )

    def registry(self, document=None):
        self.assertIsNotNone(
            importlib.util.find_spec("app.runtime_targets"),
            "Runtime target authority is not implemented",
        )
        from app.runtime_targets import TargetRegistry

        settings = self.settings
        if document is not None:
            path = self.settings.root / "targets.json"
            path.write_text(json.dumps(document))
            settings = replace(settings, runtime_targets_file=path)
        return TargetRegistry(settings)

    def test_default_docker_and_unknown_target_are_explicit(self):
        registry = self.registry()
        snapshot = registry.snapshot("local-docker")
        self.assertEqual((snapshot.id, snapshot.kind), ("local-docker", "docker"))
        self.assertEqual(len(snapshot.revision), 64)
        self.assertEqual([v["id"] for v in registry.public_targets()], ["local-docker"])
        with self.assertRaises(ValueError):
            registry.snapshot("ambient-context")
        registry.require_stage(snapshot, stage())

    def test_target_revision_covers_namespace_and_context_authority(self):
        doc = {"schema_version": 1, "targets": [target()]}
        original = self.registry(doc).snapshot("test-kubernetes")
        self.assertEqual(
            original, self.registry(copy.deepcopy(doc)).snapshot("test-kubernetes")
        )
        for key in ("worker_namespace_uid", "job_namespace_uid"):
            changed = copy.deepcopy(doc)
            changed["targets"][0][key] = "33333333-3333-4333-8333-333333333333"
            self.assertNotEqual(
                original.revision, self.registry(changed).snapshot(original.id).revision
            )
        changed = copy.deepcopy(doc)
        changed["targets"][0]["auth"]["context"] = "different-cluster"
        self.assertNotEqual(
            original.revision, self.registry(changed).snapshot(original.id).revision
        )

    def test_public_views_hide_auth_and_snapshot_mutation_does_not_change_registry(
        self,
    ):
        registry = self.registry({"schema_version": 1, "targets": [target()]})
        first = registry.snapshot("test-kubernetes")
        first.configuration["job_namespace"] = "hijacked"
        fresh = registry.snapshot("test-kubernetes")
        self.assertEqual(fresh.configuration["job_namespace"], "flumic-actions")
        public = registry.public_targets()[1]
        wire = json.dumps(public)
        for private in ("/private/", "kubeconfig", "test-context", "auth", "token"):
            self.assertNotIn(private, wire)
        self.assertEqual(public["capabilities"]["log_stream"], "combined")
        self.assertTrue(public["capabilities"]["scratch_executable"])
        self.assertEqual(public["availability"], "configured")
        self.assertIn("Executable scratch", public["limitations"])
        self.assertFalse(public["capabilities"]["legacy"])
        with self.assertRaises(ValueError):
            registry.require_stage(first, stage())

    def test_schema_rejects_ambiguous_clusters_and_unapproved_credentials(self):
        variants = []
        for key, value in (
            ("id", "local-docker"),
            ("worker_namespace", "Bad/namespace"),
            ("job_namespace", "flumic-workers"),
            ("storage_class", ""),
            ("pid_limit", True),
            ("worker_endpoint", "http://fixture-secret@control.test:8000"),
        ):
            item = target()
            item[key] = value
            variants.append(item)
        for auth in (
            {"mode": "kubeconfig", "kubeconfig": "/private/test"},
            {"mode": "kubeconfig", "context": "test"},
            {"mode": "ambient"},
            {"mode": "incluster", "context": "test"},
            {"mode": "incluster", "token": "fixture-secret"},
        ):
            item = target()
            item["auth"] = auth
            variants.append(item)
        item = target()
        item["images"]["simple"] = "worker:latest"
        variants.append(item)
        item = target()
        item["insecure_skip_tls_verify"] = True
        variants.append(item)
        for item in variants:
            with self.subTest(item=item):
                with self.assertRaises(ValueError) as raised:
                    self.registry({"schema_version": 1, "targets": [item]})
                self.assertNotIn("fixture-secret", str(raised.exception))
        for doc in (
            {"schema_version": 2, "targets": []},
            {"schema_version": 1, "targets": [target(), target()]},
            {"schema_version": 1, "targets": [], "token": "fixture-secret"},
        ):
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                self.registry(doc)

    def test_stage_admission_requires_qualified_network_and_exact_pid_ceiling(self):
        for updates in (
            {"network_qualified": False},
            {"network_evidence": ""},
            {"pid_qualified": False},
            {"pid_evidence": ""},
        ):
            item = {**target(), **updates}
            registry = self.registry({"schema_version": 1, "targets": [item]})
            snap = registry.snapshot(item["id"])
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                registry.require_stage(snap, stage())
            registry.require_stage(
                snap,
                BuiltinStage(
                    id="echo",
                    executor="builtin",
                    action="core.echo@1",
                    role="simple",
                    pool_id="pool",
                ),
            )
        registry = self.registry({"schema_version": 1, "targets": [target()]})
        snap = registry.snapshot("test-kubernetes")
        registry.require_stage(snap, stage(network="bridge"))
        for pids in (64, 256):
            with self.subTest(pids=pids), self.assertRaises(ValueError):
                registry.require_stage(snap, stage(limits={"pids": pids}))
        with self.assertRaises(ValueError):
            registry.require_stage(snap, stage(), protocol="legacy")
        item = target()
        item["network_policy_names"].pop("bridge")
        registry = self.registry({"schema_version": 1, "targets": [item]})
        with self.assertRaises(ValueError):
            registry.require_stage(
                registry.snapshot(item["id"]), stage(network="bridge")
            )

    def test_removed_or_edited_target_pin_is_not_readmitted(self):
        first = self.registry({"schema_version": 1, "targets": [target()]})
        snap = first.snapshot("test-kubernetes")
        changed = target()
        changed["deployment_id"] = "other-deployment"
        with self.assertRaises(ValueError):
            self.registry({"schema_version": 1, "targets": [changed]}).require_stage(
                snap, stage()
            )
        with self.assertRaises(ValueError):
            self.registry().require_stage(snap, stage())

    def test_old_job_wire_remains_accepted_and_kubernetes_requires_valid_pin_deadline(
        self,
    ):
        launch = JobLaunch(**old_launch())
        self.assertEqual(getattr(launch, "target_id", None), "local-docker")
        self.assertNotIn("target_snapshot", launch.model_dump())
        self.assertEqual(getattr(JobCapture(output={}), "log_stream", None), "split")
        self.assertNotIn("log_stream", JobCapture(output={}).model_dump())
        registry = self.registry({"schema_version": 1, "targets": [target()]})
        snap = registry.snapshot("test-kubernetes")
        supplied = {
            **old_launch(),
            "target_id": snap.id,
            "target_revision": snap.revision,
            "target_snapshot": snap.model_dump(),
            "deadline": 1234.0,
        }
        launch = JobLaunch(**supplied)
        ref = RuntimeJobRef(
            **{k: getattr(launch, k) for k in RuntimeJobRef.model_fields}
        )
        self.assertEqual(
            ref.target_snapshot["configuration"]["job_namespace"], "flumic-actions"
        )
        for updates in (
            {"deadline": None},
            {"deadline": float("inf")},
            {"deadline": 0.0},
            {"target_snapshot": {}},
            {"target_revision": "f" * 64},
            {"target_id": "other"},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                JobLaunch(**{**supplied, **updates})
        changed = copy.deepcopy(supplied)
        changed["target_snapshot"]["configuration"]["job_namespace"] = "hijacked"
        with self.assertRaises(ValueError):
            JobLaunch(**changed)

    def test_combined_log_metadata_is_authority_not_secret_redacted_content(self):
        from app.job_contracts import sanitize_capture

        result, failed = sanitize_capture(
            JobCapture(output={"message": "combined"}, log_stream="combined"),
            ["combined", "log_stream"],
        )
        self.assertFalse(failed)
        self.assertEqual(result["log_stream"], "combined")
        self.assertEqual(result["output"], {"message": "[REDACTED]"})

    def test_boolean_schema_version_is_not_an_integer_protocol_version(self):
        self.registry()
        with self.assertRaises(ValueError):
            self.registry({"schema_version": True, "targets": []})

    def test_settings_loads_only_explicit_target_file_not_ambient_kubeconfig(self):
        import os
        from unittest.mock import patch

        self.registry()
        path = self.settings.root / "explicit-targets.json"
        path.write_text(json.dumps({"schema_version": 1, "targets": [target()]}))
        environment = {
            "CONTROL_DATABASE_URL": "sqlite://",
            "CONTROL_ADMIN_PASSWORD": "fixture",
            "CONTROL_ENCRYPTION_KEY": "fixture",
            "FACTORY_ROOT": str(self.settings.root),
            "KUBECONFIG": "/private/ambient-cluster",
        }
        from app.runtime_targets import TargetRegistry

        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(
                len(TargetRegistry(AppSettings.load()).public_targets()), 1
            )
            os.environ["CONTROL_RUNTIME_TARGETS_FILE"] = str(path)
            loaded = AppSettings.load()
            self.assertTrue(loaded.runtime_targets_file.samefile(path))
            self.assertEqual(len(TargetRegistry(loaded).public_targets()), 2)

    def test_oversized_and_duplicate_key_target_files_are_not_silently_read(self):
        self.registry()
        path = self.settings.root / "raw.json"
        from app.runtime_targets import TargetRegistry

        for raw in (
            '{"schema_version":1,"schema_version":2,"targets":[]}',
            " " * 1048577,
        ):
            path.write_text(raw)
            with self.subTest(raw=raw[:80]), self.assertRaises(ValueError):
                TargetRegistry(replace(self.settings, runtime_targets_file=path))
