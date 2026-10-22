"""Evaluate workspace ignore rules with Git, without modifying the workspace."""

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import tempfile

from factory.domain.models import ModelError


@contextmanager
def git_ignore_rules(workspace: Path):
    # Ignore host/global Git configuration and any inherited repository/index.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    with tempfile.TemporaryDirectory(prefix="factory-ignore-") as directory:
        try:
            subprocess.run(
                ["git", "init", "--bare", "--quiet", "--template=", directory],
                env=env,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            raise ModelError(
                "Git is required to evaluate workspace .gitignore rules"
            ) from None

        def ignored(paths):
            if not paths:
                return set()
            try:
                result = subprocess.run(
                    [
                        "git",
                        "--git-dir=" + directory,
                        "--work-tree=" + str(workspace.resolve()),
                        "-c",
                        "core.bare=false",
                        "-c",
                        "core.excludesFile=" + os.devnull,
                        "check-ignore",
                        "--no-index",
                        "-z",
                        "--stdin",
                    ],
                    cwd=workspace,
                    env=env,
                    input=b"".join(os.fsencode(path) + b"\0" for path in paths),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                raise ModelError(
                    "Could not evaluate workspace .gitignore rules"
                ) from None
            if result.returncode not in (0, 1):
                raise ModelError("Could not evaluate workspace .gitignore rules")
            return {os.fsdecode(path) for path in result.stdout.split(b"\0") if path}

        yield ignored
