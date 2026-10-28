"""Real filesystem transport checks; no cluster, images or credentials needed."""

import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app.job_contracts import JobLaunch
from app.runtime_targets import KubernetesTarget, TargetSnapshot, digest
from factory.workflows.references import json_bytes
from test_runtime_targets import target, old_launch

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "control-plane/kubernetes-helper/flumic_job_helper.py"


def snapshot():
    document = KubernetesTarget.model_validate(target()).model_dump(mode="json")
    return TargetSnapshot(
        id=document["id"],
        kind="kubernetes",
        configuration=document,
        revision=digest(document),
    )


def launch(**extra):
    pin = snapshot()
    return JobLaunch(
        **(
            old_launch()
            | dict(
                target_id=pin.id,
                target_revision=pin.revision,
                target_snapshot=pin.model_dump(),
                deadline=12345678900.0,
            )
            | extra
        )
    )


class KubernetesPayloadTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(
            HELPER.is_file(), "Trusted Kubernetes helper is not implemented"
        )
        self.assertIsNotNone(
            importlib.util.find_spec("app.adapters.kubernetes_job_manifests"),
            "Kubernetes payload manifests are not implemented",
        )
        spec = importlib.util.spec_from_file_location(
            "flumic_job_helper_fixture", HELPER
        )
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("projection", "stage", "output", "control"):
            (self.root / name).mkdir()
        self.addCleanup(patch.stopall)
        patch.object(self.helper, "PROJECTION", self.root / "projection").start()
        patch.object(self.helper, "STAGING", self.root / "stage").start()
        patch.object(self.helper, "OUTPUT", self.root / "output").start()
        patch.object(self.helper, "CONTROL", self.root / "control").start()

    def project(self, value):
        from app.adapters.kubernetes_job_manifests import payload_resources

        resources = payload_resources(value, "owned-job-uid", snapshot())
        for item in resources:
            if item["kind"] != "ConfigMap":
                continue
            for key, data in item.get("data", {}).items():
                (self.root / "projection" / key).write_text(data)
            for key, data in item.get("binaryData", {}).items():
                (self.root / "projection" / key).write_bytes(
                    base64.b64decode(data, validate=True)
                )
        return resources

    def test_full_mebibyte_and_unicode_roundtrip_with_separate_credentials(self):
        for value in (
            {"value": "x" * (1048576 - len(json_bytes({"value": ""})))},
            {"value": "λ🙂" * 90000},
        ):
            with self.subTest(size=len(json_bytes(value))):
                for path in (self.root / "stage").iterdir():
                    path.unlink()
                resources = self.project(
                    launch(inputs=value, credentials={"gitlab": "fixture-grant"})
                )
                self.helper.initialize()
                self.assertEqual(
                    json.loads((self.root / "stage/input.json").read_text()), value
                )
                self.assertFalse((self.root / "stage/context.json").exists())
                self.assertFalse((self.root / "stage/credentials.json").exists())
                self.assertNotIn(
                    "fixture-grant",
                    json.dumps([r for r in resources if r["kind"] == "ConfigMap"]),
                )
                self.assertTrue(
                    all(
                        len(base64.b64decode(v)) <= 196608
                        for r in resources
                        for v in r.get("binaryData", {}).values()
                    )
                )
        with self.assertRaises(ValueError):
            launch(inputs={"value": "x" * (1048577 - len(json_bytes({"value": ""})))})

    def test_context_limit_and_optional_context_transport(self):
        value = {"value": "x" * (16384 - len(json_bytes({"value": ""})))}
        self.project(launch(context=value))
        self.helper.initialize()
        self.assertEqual(
            json.loads((self.root / "stage/context.json").read_text()), value
        )
        with self.assertRaises(ValueError):
            launch(context={"value": "x" * (16385 - len(json_bytes({"value": ""})))})

    def test_chunk_proofs_reject_corruption_missing_order_count_and_oversize(self):
        for mutation in (
            "bytes",
            "missing",
            "index",
            "count",
            "size",
            "boolean-version",
            "float-length",
        ):
            with self.subTest(mutation=mutation):
                for path in (self.root / "projection").iterdir():
                    path.unlink()
                self.project(launch(inputs={"value": "x" * 250000}))
                manifest_path = self.root / "projection/manifest.json"
                manifest = json.loads(manifest_path.read_text())
                if mutation == "bytes":
                    (self.root / "projection/chunk-0").write_bytes(b"bad")
                if mutation == "missing":
                    (self.root / "projection/chunk-0").unlink()
                if mutation == "index":
                    manifest["chunks"][0]["index"] = 1
                if mutation == "count":
                    manifest["count"] = 9
                if mutation == "size":
                    (self.root / "projection/chunk-0").write_bytes(b"x" * 196609)
                if mutation == "boolean-version":
                    manifest["version"] = True
                if mutation == "float-length":
                    manifest["chunks"][0]["length"] = float(
                        manifest["chunks"][0]["length"]
                    )
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaises(ValueError):
                    self.helper.initialize()

    def test_repeatable_bounded_capture_does_not_release_collector(self):
        output = {"output": {"answer": "λ🙂"}, "usage": [{"tokens": 1}] * 256}
        (self.root / "output/result.json").write_bytes(json_bytes(output))
        self.assertEqual(self.helper.capture(), output)
        self.assertEqual(self.helper.capture(), output)
        self.assertFalse((self.root / "control/released").exists())
        self.helper.release()
        self.helper.release()
        self.assertTrue((self.root / "control/released").is_file())

    def test_result_size_schema_and_nonfinite_values_fail_closed(self):
        path = self.root / "output/result.json"
        envelope = {"output": {"value": ""}, "usage": []}
        envelope["output"]["value"] = "x" * (1048576 - len(json_bytes(envelope)))
        path.write_bytes(json_bytes(envelope))
        self.assertEqual(len(json_bytes(envelope)), 1048576)
        self.assertEqual(self.helper.capture()["output"], envelope["output"])
        cases = [
            b"x" * 1048577,
            b'{"output":{"value":NaN}}',
            b'{"output":[]}',
            b'{"output":{},"usage":' + json.dumps([{}] * 257).encode() + b"}",
            b'{"output":{},"output":{"bad":true}}',
            b"not-json",
        ]
        for data in cases:
            with self.subTest(size=len(data)):
                path.write_bytes(data)
                with self.assertRaises(ValueError):
                    self.helper.capture()

    def test_result_special_files_and_link_attacks_do_not_block_or_escape(self):
        path = self.root / "output/result.json"
        outside = self.root / "outside.json"
        outside.write_text('{"output":{"private":true}}')
        for kind in ("symlink", "hardlink", "fifo", "directory"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    path.symlink_to(outside)
                if kind == "hardlink":
                    os.link(outside, path)
                if kind == "fifo":
                    os.mkfifo(path)
                if kind == "directory":
                    path.mkdir()
                with self.assertRaises(ValueError):
                    self.helper.capture()
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()

    def test_cli_rejects_paths_argv_and_arbitrary_modes(self):
        for args in (
            ["capture", "/etc/passwd"],
            ["release", "/tmp/released"],
            ["shell"],
            ["-c", "print(1)"],
        ):
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(HELPER), *args],
                capture_output=True,
                timeout=5,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(b"Traceback", result.stderr)

    def test_manifest_has_suspended_restricted_action_and_private_helper_mounts(self):
        from app.adapters.kubernetes_job_manifests import (
            job_manifest,
            payload_resources,
        )

        value = launch(
            command=["/custom/bin/action", "--check"],
            context={"execution": "fixture"},
            credentials={"openai": "fixture-grant"},
        )
        job = job_manifest(value, snapshot())
        spec = job["spec"]
        self.assertTrue(spec["suspend"])
        self.assertEqual(spec["backoffLimit"], 0)
        self.assertNotIn("ttlSecondsAfterFinished", spec)
        pod = spec["template"]["spec"]
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertFalse(pod.get("shareProcessNamespace", False))
        action, collector = pod["containers"]
        self.assertEqual(action["name"], "action")
        self.assertEqual(action["command"], value.command)
        self.assertEqual(action["securityContext"]["runAsUser"], 10001)
        self.assertTrue(action["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(action["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(action["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertFalse(action.get("envFrom"))
        self.assertFalse(any("hostPath" in v for v in pod["volumes"]))
        self.assertNotIn("control", {v["name"] for v in action["volumeMounts"]})
        self.assertEqual(
            {v["name"] for v in collector["volumeMounts"]}, {"output", "control"}
        )
        self.assertNotIn(
            "credentials", {v["name"] for v in pod["initContainers"][0]["volumeMounts"]}
        )
        self.assertEqual(
            collector["image"], snapshot().configuration["images"]["helper"]
        )
        self.assertEqual(collector["command"][-1], "serve")
        resources = payload_resources(value, "owned-job-uid", snapshot())
        self.assertTrue(all(r["immutable"] for r in resources))
        self.assertTrue(
            all(
                r["metadata"]["ownerReferences"][0]["uid"] == "owned-job-uid"
                for r in resources
            )
        )

    def test_fingerprint_ignores_server_defaults_but_rejects_injected_authority(self):
        from app.adapters.kubernetes_job_manifests import (
            job_manifest,
            manifest_fingerprint,
            payload_resources,
        )

        expected = job_manifest(launch(), snapshot())
        actual = copy.deepcopy(expected)
        actual["metadata"].update(
            uid="job-uid", resourceVersion="1", creationTimestamp="2026-10-27T00:00:00Z"
        )
        actual["status"] = {"active": 1}
        actual["spec"].update(
            parallelism=1,
            completions=1,
            completionMode="NonIndexed",
            manualSelector=False,
        )
        actual["spec"]["template"]["spec"].update(
            dnsPolicy="ClusterFirst", schedulerName="default-scheduler"
        )
        self.assertEqual(manifest_fingerprint(expected), manifest_fingerprint(actual))
        actual["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
            {"secretRef": {"name": "foreign"}}
        ]
        self.assertNotEqual(
            manifest_fingerprint(expected), manifest_fingerprint(actual)
        )
        resource = payload_resources(launch(), "owned-job-uid", snapshot())[0]
        foreign = copy.deepcopy(resource)
        foreign["metadata"]["ownerReferences"][0]["uid"] = "foreign"
        self.assertNotEqual(
            manifest_fingerprint(resource), manifest_fingerprint(foreign)
        )

    def test_helper_rejects_deep_and_normalized_oversize_results(self):
        path = self.root / "output/result.json"
        deep = {}
        for _ in range(70):
            deep = {"next": deep}
        path.write_bytes(json.dumps({"output": deep}).encode())
        with self.assertRaises(ValueError):
            self.helper.capture()
        value = {"output": {"value": ""}}
        value["output"]["value"] = "x" * (1048576 - len(json_bytes(value)))
        path.write_bytes(json_bytes(value))
        with self.assertRaises(ValueError):
            self.helper.capture()

    def test_fingerprint_validates_job_controller_defaults_and_quantities(self):
        from app.adapters.kubernetes_job_manifests import (
            job_manifest,
            manifest_fingerprint,
        )

        expected = job_manifest(launch(), snapshot())
        actual = copy.deepcopy(expected)
        actual["metadata"]["uid"] = "job-uid"
        name = actual["metadata"]["name"]
        auto = {
            "batch.kubernetes.io/controller-uid": "job-uid",
            "controller-uid": "job-uid",
            "batch.kubernetes.io/job-name": name,
            "job-name": name,
        }
        actual["spec"]["selector"] = {
            "matchLabels": {"batch.kubernetes.io/controller-uid": "job-uid"}
        }
        actual["spec"]["template"]["metadata"]["labels"].update(auto)
        actual["spec"]["template"]["metadata"]["creationTimestamp"] = None
        pod = actual["spec"]["template"]["spec"]
        pod.pop("nodeSelector")
        pod.pop("tolerations")
        pod.pop("imagePullSecrets")
        pod["containers"][0].pop("env")
        for container in pod["containers"] + pod["initContainers"]:
            container["imagePullPolicy"] = "IfNotPresent"
            container["terminationMessagePolicy"] = "File"
            container["terminationMessagePath"] = "/dev/termination-log"
            for limits in container["resources"].values():
                if limits["cpu"] == "1.0":
                    limits["cpu"] = "1"
        self.assertEqual(manifest_fingerprint(expected), manifest_fingerprint(actual))
        actual["spec"]["template"]["metadata"]["labels"]["controller-uid"] = "foreign"
        with self.assertRaises(ValueError):
            manifest_fingerprint(actual)

    def test_collector_stays_alive_after_capture_until_explicit_release(self):
        import threading
        import time

        thread = threading.Thread(target=self.helper.serve, daemon=True)
        thread.start()
        try:
            (self.root / "output/result.json").write_text(
                '{"output":{"ok":true},"usage":[]}'
            )
            self.helper.capture()
            time.sleep(0.03)
            self.assertTrue(thread.is_alive())
        finally:
            self.helper.release()
            thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
