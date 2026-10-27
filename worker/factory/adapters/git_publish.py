"""Guarded descendant-only Git publication in a private, checkout-free repository."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

from factory.domain.models import RepositoryError, StaleRevisionError
from factory.domain.safety import (
    assert_safe_text,
    is_safe_repository_path,
    MAX_SOURCE_FILE_BYTES,
)

_OID = re.compile(r"[0-9a-f]{40}")


class GitPublisher:
    def __init__(self, *, timeout_seconds=60):
        self.timeout_seconds = timeout_seconds

    def _validate_remote(self, remote):
        url = urlsplit(remote)
        if (
            url.scheme != "https"
            or url.netloc != "gitlab.com"
            or url.query
            or url.fragment
            or not re.fullmatch(r"(?:/[A-Za-z0-9_.-]+){2,21}\.git", url.path)
            or any(part in {".", ".."} for part in url.path.split("/"))
        ):
            raise ValueError("Expected a fixed GitLab.com HTTPS repository remote")

    def _run(self, args, *, cwd, env, input=None):
        try:
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(
                    [
                        "git",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "-c",
                        "credential.helper=",
                        "-c",
                        "http.followRedirects=false",
                        "-c",
                        "protocol.allow=never",
                        "-c",
                        "protocol.https.allow=always",
                        "-c",
                        "protocol.file.allow="
                        + env.get("FLUMIC_LOCAL_PROTOCOL", "never"),
                        *args,
                    ],
                    cwd=cwd,
                    env=env,
                    input=input,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=self.timeout_seconds,
                    check=False,
                )
                # Disk-backed capture avoids retaining an unbounded remote
                # diagnostic stream in worker memory. Never expose stderr.
                if result.returncode:
                    raise RepositoryError(
                        "Git operation failed; inspect repository state"
                    )
                output.seek(0, os.SEEK_END)
                if output.tell() > 8 * 1024 * 1024:
                    raise RepositoryError("Git operation response exceeded limit")
                output.seek(0)
                return output.read().decode("utf-8").strip()
        except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
            raise RepositoryError("Git operation unavailable or timed out") from None

    def publish(
        self,
        *,
        remote,
        branch,
        expected_head,
        changes,
        marker,
        credentials,
        base_commit=None,
    ):
        self._validate_remote(remote)
        base = base_commit or expected_head
        if not _OID.fullmatch(base or "") or (
            expected_head and not _OID.fullmatch(expected_head)
        ):
            raise ValueError("Expected a full pinned commit OID")
        if base_commit and expected_head and base_commit != expected_head:
            raise ValueError("Publication parent must be the expected head")
        if not isinstance(branch, str) or len(branch) > 200 or branch.startswith("-"):
            raise ValueError("Invalid publication branch")
        if (
            not isinstance(marker, str)
            or not marker
            or len(marker) > 256
            or any(ord(c) < 32 for c in marker)
        ):
            raise ValueError("Invalid publication marker")
        if not changes or len(changes) > 100:
            raise ValueError("Expected one to one hundred file changes")
        paths = set()
        for change in changes:
            if (
                not is_safe_repository_path(change.path)
                or any(ord(c) < 32 or ord(c) == 127 for c in change.path)
                or change.path in paths
                or len(change.content.encode()) > MAX_SOURCE_FILE_BYTES
            ):
                raise ValueError("Invalid or oversized publication patch")
            paths.add(change.path)
            assert_safe_text(change.content)
        token = credentials.get("gitlab")
        if (
            set(credentials) != {"gitlab"}
            or not isinstance(token, str)
            or not token
            or "\n" in token
            or "\r" in token
        ):
            raise ValueError("Explicit GitLab credential required")
        ref = "refs/heads/" + branch
        with tempfile.TemporaryDirectory(prefix="flumic-publish-") as directory:
            root = Path(directory)
            root.chmod(0o700)
            secret = root / "credential"
            secret.write_text(token)
            secret.chmod(0o600)
            askpass = root / "askpass"
            askpass.write_text(
                '#!/bin/sh\ncase "$1" in *sername*) printf "%s\\n" oauth2 ;; *) cat "$FLUMIC_GIT_CREDENTIAL_FILE" ;; esac\n'
            )
            askpass.chmod(0o700)
            env = {
                "PATH": os.environ.get("PATH", os.defpath),
                "HOME": directory,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": str(askpass),
                "FLUMIC_GIT_CREDENTIAL_FILE": str(secret),
                "GIT_AUTHOR_NAME": "Flumic AI",
                "GIT_AUTHOR_EMAIL": "flumic@example.invalid",
                "GIT_COMMITTER_NAME": "Flumic AI",
                "GIT_COMMITTER_EMAIL": "flumic@example.invalid",
                "LC_ALL": "C",
                "GIT_LFS_SKIP_SMUDGE": "1",
            }
            # Local remote access exists solely in read-only-test subclasses;
            # production remote validation above never permits this protocol.
            if not remote.startswith("https://"):
                env["FLUMIC_LOCAL_PROTOCOL"] = "always"
            repository = root / "repository.git"

            def run(args, input=None):
                return self._run(args, cwd=repository, env=env, input=input)

            self._run(["init", "--bare", str(repository)], cwd=root, env=env)
            run(["check-ref-format", ref])
            run(["fetch", "--no-tags", "--depth=1", "--filter=blob:none", remote, base])
            run(["read-tree", base])
            for change in changes:
                entries = run(
                    ["ls-files", "--stage", "-z", "--", ":(literal)" + change.path]
                )
                mode = "100644"
                for entry in entries.split("\0"):
                    if entry and entry.split("\t", 1)[1] == change.path:
                        mode = entry.split(" ", 1)[0]
                if mode not in {"100644", "100755"}:
                    raise RepositoryError("Publication can only edit regular files")
                oid = run(
                    ["hash-object", "-w", "--stdin"], input=change.content.encode()
                )
                # No checkout: repository symlinks, attributes and executable
                # filters cannot cause local writes or code execution.
                run(
                    [
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        f"{mode},{oid},{change.path}",
                    ]
                )
            tree = run(["write-tree"])
            current = run(["ls-remote", "--refs", remote, ref])
            current = current.split()[0] if current else ""
            message = "Flumic publication\n\n" + marker

            def recovered(head):
                if not head or not _OID.fullmatch(head):
                    return False
                run(["fetch", "--no-tags", "--depth=1", remote, head])
                actual_tree = run(["show", "-s", "--format=%T", head])
                actual_parents = (
                    run(["cat-file", "-p", head]).split("\n\n", 1)[0].splitlines()
                )
                parents = [
                    line.removeprefix("parent ")
                    for line in actual_parents
                    if line.startswith("parent ")
                ]
                actual_message = run(["show", "-s", "--format=%B", head])
                return (
                    actual_tree == tree
                    and parents == [base]
                    and actual_message == message
                )

            if current != expected_head:
                if recovered(current):
                    return current
                raise StaleRevisionError(
                    "Repository head differs from pinned publication base"
                )
            commit = run(
                ["commit-tree", tree, "-p", base], input=(message + "\n").encode()
            )
            try:
                run(
                    [
                        "push",
                        "--porcelain",
                        f"--force-with-lease={ref}:{expected_head}",
                        remote,
                        f"{commit}:{ref}",
                    ]
                )
            except RepositoryError:
                observed = run(["ls-remote", "--refs", remote, ref])
                observed = observed.split()[0] if observed else ""
                if recovered(observed):
                    return observed
                if observed != expected_head:
                    raise StaleRevisionError(
                        "Repository changed during publication"
                    ) from None
                raise RepositoryError(
                    "Publication not confirmed; retry only with original pins"
                ) from None
            return commit
