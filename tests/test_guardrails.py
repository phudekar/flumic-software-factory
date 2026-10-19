import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from guardrails import (
    MAX_CONTEXT_TOKENS,
    MAX_COMPLETION_TOKENS,
    SensitiveContentBlocked,
    assert_safe_text,
    count_request_tokens,
    find_sensitive_categories,
    is_allowed_source_path,
    is_safe_repository_path,
    validate_source_inventory,
)


class GuardrailTests(unittest.TestCase):
    def test_blocks_common_secret_patterns_without_echoing_match(self):
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"

        with self.assertRaises(SensitiveContentBlocked) as raised:
            assert_safe_text(f"example = {secret}")

        self.assertIn("openai-key", raised.exception.categories)
        self.assertNotIn(secret, str(raised.exception))

    def test_allows_ordinary_issue_and_source_text(self):
        assert_safe_text(
            "Add validation for issue processing.",
            "def process_issue(number: int) -> bool:\n    return number > 0\n",
        )

    def test_source_allowlist_excludes_secrets_config_and_generated_content(self):
        allowed = [
            "src/worker.py",
            "web/src/agent.ts",
            "docs/architecture.md",
            "examples/sample.txt",
            "public/index.html",
            "src/generated-types.d.ts",
            "README",
            "assets/icon.svg",
            "Cargo.toml",
            "backend/Cargo.toml",
            "package.json",
            "frontend/package.json",
            "go.mod",
            "pom.xml",
            "pyproject.toml",
            "requirements.txt",
            "vite.config.ts",
            "frontend/vite.config.ts",
            "vite.config.mjs",
        ]
        denied = [
            ".env",
            "appsettings.json",
            ".github/workflows/deploy.yml",
            "src/credentials.py",
            "node_modules/pkg/index.js",
            "dist/bundle.js",
            "../outside.py",
            "/absolute/path.py",
            "Dockerfile",
            "deploy/manifest.json",
            "infra/main.tf",
            "package-lock.json",
            "Cargo.lock",
            "src/config.toml",
            "config/vite.config.ts",
            "frontend/secret-vite.config.ts",
            "Jenkinsfile",
            "service.yaml",
        ]

        for path in allowed:
            with self.subTest(path=path):
                self.assertTrue(is_allowed_source_path(path))
        for path in denied:
            with self.subTest(path=path):
                self.assertFalse(is_allowed_source_path(path))

    def test_inventory_is_deduplicated_and_filtered(self):
        paths = validate_source_inventory(
            ["src/app.py", "src/app.py", ".env", ".github/workflows/test.py"]
        )

        self.assertEqual(paths, ["src/app.py"])

    def test_repository_path_validation_still_confines_output_to_workspace(self):
        for path in (
            ".github/workflows/deploy.yml",
            "Cargo.lock",
            "src/config.toml",
            "Dockerfile",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_safe_repository_path(path))

        for path in (
            "",
            "../outside.py",
            "/absolute/path.py",
            "src\\outside.py",
            "C:/outside.py",
            ".git/config",
            "src/.git/hooks/pre-commit",
        ):
            with self.subTest(path=path):
                self.assertFalse(is_safe_repository_path(path))

    def test_credential_and_configuration_files_are_not_allowed(self):
        for path in ("src/credentials.py", "src/settings.py", "app/config.py"):
            with self.subTest(path=path):
                self.assertFalse(is_allowed_source_path(path))

    def test_finding_categories_do_not_include_sensitive_values(self):
        categories = find_sensitive_categories(
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
        )

        self.assertEqual(categories, ["authorization-header"])

    def test_token_limit_includes_reserved_completion_tokens(self):
        tokens = count_request_tokens(
            [{"role": "user", "content": "Implement input validation."}]
        )

        self.assertEqual(MAX_COMPLETION_TOKENS, 16_000)
        self.assertEqual(MAX_CONTEXT_TOKENS, 32_000)
        self.assertLess(tokens + MAX_COMPLETION_TOKENS, MAX_CONTEXT_TOKENS)
        with self.assertRaisesRegex(ValueError, "32,000-token"):
            count_request_tokens(
                [{"role": "user", "content": "word " * MAX_CONTEXT_TOKENS}]
            )


if __name__ == "__main__":
    unittest.main()
