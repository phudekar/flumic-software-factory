"""Real bare-Git races test publication without touching a remote service."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from factory.domain.models import FileChange, StaleRevisionError


def git(directory, *args):
    return (
        subprocess.check_output(
            ["git", "-C", str(directory), *args],
            stderr=subprocess.DEVNULL,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_AUTHOR_NAME": "Fixture",
                "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                "GIT_COMMITTER_NAME": "Fixture",
                "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            },
        )
        .decode()
        .strip()
    )


class GitPublicationTests(unittest.TestCase):
    def setUp(self):
        from factory.adapters.git_publish import GitPublisher

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.remote = root / "remote.git"
        git(root, "init", "--bare", str(self.remote))
        self.work = root / "work"
        git(root, "init", str(self.work))
        (self.work / "main.py").write_text("base\n")
        git(self.work, "add", "main.py")
        git(self.work, "commit", "-m", "base")
        self.base = git(self.work, "rev-parse", "HEAD")
        git(self.work, "remote", "add", "origin", str(self.remote))
        git(self.work, "push", "origin", "HEAD:refs/heads/factory")

        class LocalPublisher(GitPublisher):
            def _validate_remote(inner, remote):
                if remote != str(self.remote):
                    raise ValueError("Only owned fixture remote allowed")

        self.publisher = LocalPublisher()
        self.kwargs = dict(
            remote=str(self.remote),
            branch="factory",
            expected_head=self.base,
            changes=[FileChange(path="main.py", content="generated\n")],
            marker="factory-patch:fixture",
            credentials={"gitlab": "fixture-secret"},
        )

    def test_publish_descendant_and_replay_full_tree(self):
        result = self.publisher.publish(**self.kwargs)
        self.assertEqual(git(self.remote, "rev-parse", "factory"), result)
        self.assertEqual(git(self.remote, "rev-parse", result + "^"), self.base)
        self.assertEqual(git(self.remote, "show", result + ":main.py"), "generated")
        self.assertEqual(self.publisher.publish(**self.kwargs), result)

    def race_before_push(self, mutate):
        original = self.publisher._run

        def racing(args, **kwargs):
            if args and args[0] == "push":
                mutate()
            return original(args, **kwargs)

        self.publisher._run = racing

    def test_exact_head_lease_rejects_human_advance(self):
        (self.work / "human.py").write_text("human\n")
        git(self.work, "add", "human.py")
        git(self.work, "commit", "-m", "human")
        human = git(self.work, "rev-parse", "HEAD")
        self.race_before_push(
            lambda: git(self.work, "push", "origin", "HEAD:refs/heads/factory")
        )
        with self.assertRaises(StaleRevisionError):
            self.publisher.publish(**self.kwargs)
        self.assertEqual(git(self.remote, "rev-parse", "factory"), human)

    def test_exact_head_lease_rejects_human_reset(self):
        (self.work / "main.py").write_text("next\n")
        git(self.work, "add", "main.py")
        git(self.work, "commit", "-m", "next")
        head = git(self.work, "rev-parse", "HEAD")
        git(self.work, "push", "origin", "HEAD:refs/heads/factory")
        self.kwargs["expected_head"] = head
        self.race_before_push(
            lambda: git(self.remote, "update-ref", "refs/heads/factory", self.base)
        )
        with self.assertRaises(StaleRevisionError):
            self.publisher.publish(**self.kwargs)
        self.assertEqual(git(self.remote, "rev-parse", "factory"), self.base)

    def test_create_branch_race_preserves_other_commit(self):
        self.kwargs.update(branch="new-branch", expected_head="", base_commit=self.base)
        self.race_before_push(
            lambda: git(self.remote, "update-ref", "refs/heads/new-branch", self.base)
        )
        with self.assertRaises(StaleRevisionError):
            self.publisher.publish(**self.kwargs)
        self.assertEqual(git(self.remote, "rev-parse", "new-branch"), self.base)

    def test_lost_push_response_recovers_only_full_expected_tree(self):
        result = self.publisher.publish(**self.kwargs)
        git(self.work, "fetch", "origin", "factory")
        git(self.work, "checkout", "-B", "attack", "FETCH_HEAD")
        (self.work / "main.py").write_text("different\n")
        git(self.work, "add", "main.py")
        git(self.work, "commit", "-m", "factory-patch:fixture")
        git(self.work, "push", "origin", "HEAD:refs/heads/factory")
        with self.assertRaises(StaleRevisionError):
            self.publisher.publish(**self.kwargs)
        self.assertNotEqual(git(self.remote, "rev-parse", "factory"), result)

    def test_credentials_not_in_argv_remote_or_errors(self):
        original = self.publisher._run
        observed = []

        def record(args, **kwargs):
            observed.append((args, kwargs.get("env", {})))
            return original(args, **kwargs)

        self.publisher._run = record
        self.publisher.publish(**self.kwargs)
        self.assertTrue(observed)
        self.assertNotIn("fixture-secret", str(observed))
        from factory.adapters.git_publish import GitPublisher

        for remote in (
            "https://token@gitlab.com/g/p.git",
            "https://evil.com/g/p.git",
            "file:///tmp/repo",
            "https://gitlab.com:443/g/p.git",
        ):
            with self.subTest(remote=remote), self.assertRaises(ValueError):
                GitPublisher().publish(**{**self.kwargs, "remote": remote})

    def test_lost_successful_push_reply_uses_existing_commit(self):
        from factory.domain.models import RepositoryError

        original = self.publisher._run
        pushes = []

        def lose_reply(args, **kwargs):
            value = original(args, **kwargs)
            if args and args[0] == "push":
                pushes.append(value)
                raise RepositoryError("reply lost")
            return value

        self.publisher._run = lose_reply
        result = self.publisher.publish(**self.kwargs)
        self.assertEqual(git(self.remote, "rev-parse", "factory"), result)
        self.assertEqual(len(pushes), 1)

    def test_new_branch_creation_preserves_pinned_parent(self):
        result = self.publisher.publish(
            **{
                **self.kwargs,
                "branch": "new",
                "expected_head": "",
                "base_commit": self.base,
            }
        )
        self.assertEqual(git(self.remote, "rev-parse", "new^"), self.base)
        self.assertEqual(git(self.remote, "rev-parse", "new"), result)
