from __future__ import annotations

import logging
import re
from typing import Iterable


MAX_SOURCE_PATHS = 500
MAX_SOURCE_FILE_BYTES = 32_768
logger = logging.getLogger("issue-worker.guardrails")

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    (
        "credential-assignment",
        re.compile(
            r"(?i)['\"]?\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd|secret)"
            r"\b['\"]?\s*[:=]\s*['\"][^'\"]{8,}['\"]"
        ),
    ),
    (
        "authorization-header",
        re.compile(r"(?i)\bauthorization\s*:\s*(?:bearer|basic)\s+\S{12,}"),
    ),
    (
        "high-entropy-assignment",
        re.compile(
            r"(?i)\b(?:token|secret|key)\s*[:=]\s*['\"]([A-Za-z0-9+/=_-]{32,})['\"]"
        ),
    ),
)

_DENIED_COMPONENTS = {
    ".git",
    ".github",
    ".vscode",
    ".idea",
    "node_modules",
    "vendor",
    ".circleci",
    ".buildkite",
    "dist",
    "build",
    "target",
    "coverage",
    "__pycache__",
    ".venv",
    "venv",
    "data",
    "secrets",
    "secret",
    "credentials",
    "credential",
    "config",
    "configs",
    "deploy",
    "deployment",
    "deployments",
    "infra",
    "infrastructure",
    "terraform",
    "helm",
    "charts",
    "kubernetes",
    "k8s",
}
_DENIED_NAMES = {
    ".env",
    ".env.example",
    "id_rsa",
    "id_ed25519",
    "credentials",
    "secrets",
    "package-lock.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "setup.py",
    "setup.cfg",
    "go.sum",
    "makefile",
    "jenkinsfile",
    "dockerfile",
}
_ALLOWED_DEPENDENCY_MANIFESTS = {
    "cargo.toml",
    "composer.json",
    "gemfile",
    "go.mod",
    "package.json",
    "package.jsonc",
    "pom.xml",
    "pipfile",
    "pyproject.toml",
    "requirements-dev.txt",
    "requirements.in",
    "requirements.txt",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
}
_ALLOWED_TOOL_CONFIGS = {
    "vite.config.js",
    "vite.config.ts",
    "vite.config.mjs",
    "vite.config.mts",
}
_DENIED_EXTENSIONS = {
    ".7z",
    ".a",
    ".avi",
    ".bin",
    ".class",
    ".crt",
    ".cer",
    ".der",
    ".dll",
    ".dmg",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".key",
    ".keystore",
    ".lock",
    ".mp3",
    ".mp4",
    ".o",
    ".pdf",
    ".pem",
    ".p12",
    ".pfx",
    ".png",
    ".so",
    ".sqlite",
    ".sqlite3",
    ".tf",
    ".tfvars",
    ".hcl",
    ".bicep",
    ".tar",
    ".tgz",
    ".wasm",
    ".webp",
    ".zip",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".properties",
}


class SensitiveContentBlocked(ValueError):
    def __init__(self, categories: list[str]) -> None:
        self.categories = categories
        super().__init__("Sensitive content blocked by outbound-data policy")


def find_sensitive_categories(*values: str) -> list[str]:
    categories: set[str] = set()
    for value in values:
        for category, pattern in _SECRET_PATTERNS:
            if pattern.search(value):
                categories.add(category)
    return sorted(categories)


def assert_safe_text(*values: str) -> None:
    categories = find_sensitive_categories(*values)
    if categories:
        logger.warning("Blocked outbound content; finding categories: %s", categories)
        raise SensitiveContentBlocked(categories)


def is_safe_repository_path(path: str) -> bool:
    if not path or path.startswith(("/", "\\")) or "\\" in path or "\0" in path:
        return False
    parts = path.split("/")
    if (
        any(part in {"", ".", ".."} for part in parts)
        or parts[0].endswith(":")
        or any(part.lower() == ".git" for part in parts)
    ):
        return False
    return True


def is_allowed_source_path(path: str) -> bool:
    if not is_safe_repository_path(path):
        return False
    parts = path.split("/")
    if any(part.lower() in _DENIED_COMPONENTS for part in parts):
        return False
    filename = parts[-1].lower()
    if filename in _ALLOWED_DEPENDENCY_MANIFESTS or filename in _ALLOWED_TOOL_CONFIGS:
        return True
    if filename in _DENIED_NAMES or filename.endswith((".lock", ".pem", ".key")):
        return False
    if filename.startswith(("dockerfile", "compose.")):
        return False
    if filename.startswith("requirements") and filename.endswith((".txt", ".in")):
        return False
    if filename.endswith(tuple(_DENIED_EXTENSIONS)):
        return False
    if any(
        marker in filename
        for marker in (
            "secret",
            "credential",
            "password",
            "passwd",
            "token",
            "config",
            "setting",
        )
    ):
        return False
    if filename.startswith(".") or filename.endswith(".min.js"):
        return False
    return True


def validate_source_inventory(paths: Iterable[str]) -> list[str]:
    safe = sorted({path for path in paths if is_allowed_source_path(path)})
    if len(safe) > MAX_SOURCE_PATHS:
        return safe[:MAX_SOURCE_PATHS]
    return safe
