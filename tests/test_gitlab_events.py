"""Typed GitLab signals preserve source identity and distinguish IID namespaces."""

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))


class GitLabEventTests(unittest.TestCase):
    def payload(self, kind="issue"):
        return {
            "object_kind": kind,
            "project": {"id": 123, "path_with_namespace": "group/sub/repo"},
            "object_attributes": {"id": 900, "iid": 7, "action": "open"},
        }

    def normalize(self, payload, **kwargs):
        from factory.adapters.gitlab_events import GitLabWebhook, normalize_webhook

        return normalize_webhook(GitLabWebhook.model_validate(payload), **kwargs)

    def test_issue_uses_iid_and_bound_numeric_project(self):
        event = self.normalize(self.payload(), delivery_id="delivery-1")
        self.assertEqual(
            (event.event_id, event.project_id, event.repository, event.issue_number),
            ("delivery-1", 123, "group/sub/repo", 7),
        )
        self.assertIsNone(event.pull_number)
        self.assertEqual(event.as_factory_event().kind, "work-item-ready")
        event.assert_repository("group/sub/repo", 123)
        with self.assertRaises(ValueError):
            event.assert_repository("group/sub/repo", 124)
        with self.assertRaises(ValueError):
            event.assert_repository("other/repo", 123)

    def test_mr_and_note_use_merge_request_iid_not_global_noteable_id(self):
        mr = self.normalize(self.payload("merge_request"))
        self.assertEqual((mr.pull_number, mr.issue_number), (7, None))
        note = self.payload("note")
        note["object_attributes"] = {
            "id": 905,
            "noteable_id": 900,
            "noteable_type": "MergeRequest",
            "note": "Human feedback",
            "system": False,
        }
        note["merge_request"] = {"id": 900, "iid": 7}
        event = self.normalize(note)
        self.assertEqual((event.pull_number, event.note_id), (7, 905))
        self.assertEqual(event.as_factory_event().kind, "change-feedback-received")
        note["object_attributes"]["system"] = True
        self.assertEqual(self.normalize(note).as_factory_event().kind, "ignored")

    def test_duplicate_body_digest_is_stable_and_changed_body_conflicts(self):
        original = self.payload()
        first = self.normalize(original)
        self.assertEqual(
            first.event_id, self.normalize(copy.deepcopy(original)).event_id
        )
        changed = copy.deepcopy(original)
        changed["object_attributes"]["description"] = "updated"
        second = self.normalize(changed)
        self.assertNotEqual(first.payload_digest, second.payload_digest)
        self.assertEqual(
            self.normalize(original, delivery_id="same").event_id,
            self.normalize(changed, delivery_id="same").event_id,
        )

    def test_unknown_event_and_issue_note_are_ignored(self):
        self.assertEqual(
            self.normalize(self.payload("pipeline")).as_factory_event().kind, "ignored"
        )
        note = self.payload("note")
        note["object_attributes"].update(noteable_type="Issue", noteable_id=900)
        self.assertEqual(self.normalize(note).as_factory_event().kind, "ignored")

    def test_boolean_ids_invalid_paths_and_header_injection_are_rejected(self):
        for values in (
            {"id": True, "path_with_namespace": "group/repo"},
            {"id": 123, "path_with_namespace": "group/../repo"},
        ):
            payload = self.payload()
            payload["project"] = values
            with self.assertRaises(ValueError):
                self.normalize(payload)
        payload = self.payload()
        payload["object_attributes"]["iid"] = True
        with self.assertRaises(ValueError):
            self.normalize(payload)
        with self.assertRaises(ValueError):
            self.normalize(self.payload(), delivery_id="bad\nheader")

    def test_gitlab_sdk_registration_is_qualified_without_changing_github(self):
        import base64
        import json
        import warnings
        from unittest.mock import Mock, patch
        from hatchet_sdk import Hatchet
        from hatchet_sdk.config import ClientConfig
        from factory.adapters.orchestration.hatchet import HatchetDispatch, build_worker
        from factory.settings import Settings

        claims = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "sub": "00000000-0000-0000-0000-000000000001",
                        "server_url": "http://localhost:8080",
                        "grpc_broadcast_address": "localhost:7070",
                    }
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
        config = ClientConfig(token="eyJhbGciOiJub25lIn0." + claims + ".test")

        def definitions(settings):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                engine = Hatchet(config=config, client=Mock(config=config, debug=False))
            with patch.object(engine, "worker") as register:
                build_worker(engine, Mock(), settings, HatchetDispatch())
            return [
                workflow.to_proto()
                for workflow in register.call_args.kwargs["workflows"]
            ]

        github = definitions(Settings())
        self.assertEqual(len(github), 11)
        self.assertEqual(github[0].name, "software-factory-issue-agent")
        self.assertEqual(
            github[0].idempotency.expression,
            "input.repository + ':issue:' + string(input.issue_number)",
        )
        gitlab = definitions(
            Settings(
                repository_provider="gitlab",
                repository="g/sub/r",
                gitlab_project_id=123,
            )
        )
        self.assertEqual(gitlab[0].name, "gitlab-123-software-factory-issue-agent")
        self.assertIn("gitlab.com/project/123", gitlab[0].idempotency.expression)
        serialized = "\n".join(str(value) for value in gitlab)
        self.assertNotIn("github:issues:opened", serialized)
        self.assertNotIn("github:issue_comment:created", serialized)
        self.assertIn("gitlab:123:issue", serialized)
