from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from factory.application.telemetry import model_call
from factory.adapters.cli_usage import record_codex_usage

from cli_agent import (
    _ensure_codex_authenticated,
    _sanitize_cli_stderr,
    selected_codex_model,
)

AGENT_HOME = Path("/home/agent")
AGENT_UID = 10001
AGENT_GID = 10001
TIMEOUT_SECONDS = 60
EXPECTED_RESPONSE = "CODEX_HEALTH_OK"


def check_codex_connection(model=None) -> str | None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return "OPENAI_API_KEY is not set"

    codex_home = AGENT_HOME / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    config_path = codex_home / "config.toml"
    config_path.write_text(
        f'model = "{model or selected_codex_model()}"\n',
        encoding="utf-8",
    )
    os.chown(codex_home, AGENT_UID, AGENT_GID)
    os.chown(config_path, AGENT_UID, AGENT_GID)

    output_file = tempfile.NamedTemporaryFile(
        mode="w",
        prefix="codex-healthcheck-",
        dir="/tmp",
        delete=False,
    )
    output_path = Path(output_file.name)
    output_file.close()
    output_path.chmod(0o666)
    workspace = AGENT_HOME / "workspaces"
    workspace.mkdir(parents=True, exist_ok=True)
    os.chown(workspace, AGENT_UID, AGENT_GID)

    env = {
        "HOME": str(AGENT_HOME),
        "CODEX_HOME": str(codex_home),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "TMPDIR": "/tmp",
        "OPENAI_API_KEY": api_key,
    }
    if login_error := _ensure_codex_authenticated(env):
        return login_error

    command = [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--output-last-message",
        str(output_path),
        "-",
    ]
    prompt = f"Reply with exactly {EXPECTED_RESPONSE}. Do not use tools."

    try:
        with model_call("openai", selected_codex_model(), agent="codex-healthcheck"):
            result = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=workspace,
                env=env,
                timeout=TIMEOUT_SECONDS,
                check=False,
                user=AGENT_UID,
                group=AGENT_GID,
                extra_groups=(),
            )
            record_codex_usage(result.stdout)
        if result.returncode != 0:
            details = _sanitize_cli_stderr(result.stderr, prompt=prompt)
            details = re.sub(
                r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+",
                "Bearer [REDACTED]",
                details,
            )
            details = re.sub(r"\bsk-[A-Za-z0-9_-]+\b", "[REDACTED]", details)
            message = f"Codex API health check failed (exit code {result.returncode})"
            return f"{message}: {details}" if details else message
        response = output_path.read_text(encoding="utf-8").strip()
        if not response:
            return "Codex API health check returned an empty response"
        return None
    except subprocess.TimeoutExpired:
        return f"Codex API health check timed out after {TIMEOUT_SECONDS} seconds"
    except OSError as error:
        return f"Codex API health check could not run: {error.__class__.__name__}"
    finally:
        output_path.unlink(missing_ok=True)


def main() -> int:
    from factory.settings import Settings
    from factory.bootstrap import load_factory
    from factory.adapters.telemetry import configure_logging

    os.environ["FACTORY_TELEMETRY_PROCESS_ROLE"] = "healthcheck"
    settings = Settings.load()
    if settings.developer_agent != "codex" or settings.coding_factory:
        return 0
    configure_logging(settings)
    observer = load_factory(settings.observer_factory)(settings)
    try:
        with observer.observe("codex_healthcheck", {"healthcheck": True}):
            error = check_codex_connection(settings.codex_model)
        flush = getattr(observer, "flush", None)
        if flush:
            flush()
    finally:
        shutdown = getattr(observer, "shutdown", None)
        if shutdown:
            shutdown()
    if error:
        print(error)
        return 1
    print("Codex API health check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
