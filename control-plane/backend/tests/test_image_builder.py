"""Image assembly tests: cache semantics, context isolation and build failures."""

from dataclasses import replace
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from docker.errors import ImageNotFound
from app.adapters.image_builder import WorkerImageBuilder, BuildStore
from factory.settings import Settings

ROOT = Path(__file__).resolve().parents[3]


class ImageBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = Mock()
        self.store = BuildStore(Path(self.temp.name))
        self.builder = WorkerImageBuilder(ROOT, self.client, self.store)
        self.settings = Settings(base_image="python:3.12-slim", system_packages=["git"])
        self.base = Mock(
            id="sha256:base", attrs={"RepoDigests": ["python@sha256:base"]}
        )
        self.built = Mock(
            id="sha256:built",
            attrs={"Config": {"Labels": {"software-factory.control-protocol": "3"}}},
        )
        self.client.images.pull.return_value = self.base

    def test_context_excludes_credentials_config_and_unrelated_files(self):
        files = self.builder.context_files(self.settings)
        self.assertIn("worker/worker.py", files)
        self.assertIn("worker/requirements.txt", files)
        self.assertIn("Dockerfile", files)
        self.assertFalse(
            any(
                ".env" in p or p.startswith(("config/", "data/", "tests/"))
                for p in files
            )
        )
        self.assertNotIn("worker-config.yaml", files)
        self.assertNotIn("FACTORY_CONTROL_TOKEN", files["Dockerfile"].decode())

    def test_cached_image_skips_build_and_runtime_edits_preserve_cache(self):
        self.client.images.get.return_value = self.built
        first = self.builder.prepare(self.settings, "worker-1")
        self.settings = replace(
            self.settings,
            startup_script="echo runtime only",
            openai_model="another-model",
        )
        second = self.builder.prepare(self.settings, "worker-2")
        self.assertEqual(first, second)
        self.client.api.build.assert_not_called()
        self.assertEqual(self.store.read("worker-2")["status"], "cached")
        self.settings = replace(self.settings, image_setup_script="echo new-build")
        self.assertNotEqual(first, self.builder.prepare(self.settings, "worker-3"))

    def test_build_stream_is_recorded_and_only_allowlisted_context_sent(self):
        self.client.images.get.side_effect = [ImageNotFound("missing"), self.built]
        self.client.api.build.return_value = iter(
            [{"stream": "Installing dependencies\n"}, {"stream": "Done\n"}]
        )
        image = self.builder.prepare(self.settings, "worker-1")
        kwargs = self.client.api.build.call_args.kwargs
        self.assertEqual(kwargs["tag"], image)
        self.assertTrue(kwargs["custom_context"])
        self.assertNotIn("buildargs", kwargs)
        with tarfile.open(fileobj=io.BytesIO(kwargs["fileobj"].getvalue())) as archive:
            self.assertIn("worker/worker.py", archive.getnames())
            self.assertNotIn(".env", archive.getnames())
        state = self.store.read("worker-1")
        self.assertEqual(state["status"], "ready")
        self.assertIn("Installing dependencies", state["content"])

    def test_build_error_is_bounded_and_does_not_report_success(self):
        self.client.images.get.side_effect = ImageNotFound("missing")
        self.client.api.build.return_value = iter(
            [{"stream": "x" * 100000}, {"error": "Unsupported base distribution"}]
        )
        with self.assertRaisesRegex(ValueError, "build failed"):
            self.builder.prepare(self.settings, "worker-1")
        state = self.store.read("worker-1")
        self.assertEqual(state["status"], "failed")
        self.assertLessEqual(len(state["content"]), 65536)
        self.assertIn("Unsupported base distribution", state["content"])

    def test_base_digest_changes_invalidate_cached_image(self):
        self.client.images.get.return_value = self.built
        first = self.builder.prepare(self.settings, "worker-1")
        self.client.images.pull.return_value = Mock(id="sha256:changed")
        self.assertNotEqual(first, self.builder.prepare(self.settings, "worker-2"))

    def test_log_store_redacts_and_keeps_a_tail(self):
        from unittest.mock import patch

        with patch.dict("os.environ", {"OPENAI_API_KEY": "fixture-secret-12345"}):
            self.store.write("worker-1", "building", "fixture-secret-12345")
        self.assertNotIn("fixture-secret-12345", self.store.read("worker-1")["content"])

    def test_tool_installation_follows_coding_adapter_on_simple_workers(self):
        settings = replace(self.settings, worker_type="simple", developer_agent="codex")
        self.assertEqual(
            self.builder.context_files(settings)["image/coding-tool.txt"], b"codex"
        )

    def test_invalid_build_inputs_are_rejected(self):
        for fields in [
            {"base_image": "ubuntu\nRUN echo injected"},
            {"system_packages": ["--allow-unauthenticated"]},
            {"system_packages": ["git; echo injected"]},
            {"codex_version": "latest"},
            {"image_setup_script": "x" * 32769},
        ]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                Settings(**fields)
