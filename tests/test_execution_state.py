"""Shared action state uses explicit invocation identity; legacy storage is intact."""

import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))


class ExecutionStateTests(unittest.TestCase):
    def test_scoped_adapter_passes_context_and_retains_claim_until_terminal_receipt(
        self,
    ):
        from factory.adapters.state import ExecutionStateStore
        from factory.workflows.contracts import ActionContext

        context = ActionContext(
            project_id="p",
            repository_id="repo",
            repository_revision_id="r1",
            config_revision_id="c1",
            run_id="run",
            stage_id="s",
            execution_id="e",
            attempt=1,
            assignment_id="a",
            namespace="p/repo",
            settings_json='{"repository":"Owner/Repo"}',
        )
        transport = Mock()
        transport.get_state.return_value = {"status": "saved"}
        state = ExecutionStateStore(context, transport)
        self.assertEqual(state.get("plan:1"), {"status": "saved"})
        state.put("plan:1", {"message": "value"})
        state.delete("plan:1")
        with state.claim("Owner/Repo:issue:12", "implement"):
            pass
        transport.get_state.assert_called_once_with(context, "plan:1")
        transport.put_state.assert_called_once_with(
            context, "plan:1", {"message": "value"}
        )
        transport.delete_state.assert_called_once_with(context, "plan:1")
        transport.claim_resource.assert_called_once_with(
            context, "github.com/owner/repo/issue/12"
        )
        self.assertFalse(transport.release_resource.called)
        with self.assertRaises(ValueError):
            with state.claim("Other/Repo:issue:12", "implement"):
                pass
        self.assertEqual(transport.claim_resource.call_count, 1)

    def test_legacy_sqlite_keys_remain_separate_from_shared_adapter(self):
        from factory.adapters.state import SQLiteStateStore, ExecutionStateStore
        from factory.workflows.contracts import ActionContext

        with tempfile.TemporaryDirectory() as tmp:
            legacy = SQLiteStateStore(str(Path(tmp) / "legacy.db"))
            legacy.put("plan:1", {"legacy": True})
            context = ActionContext(
                project_id="p",
                repository_id="repo",
                repository_revision_id="r1",
                config_revision_id="c1",
                run_id="run",
                stage_id="s",
                execution_id="e",
                attempt=1,
                assignment_id="a",
                namespace="p/repo",
                settings_json='{"repository":"owner/repo"}',
            )
            transport = Mock()
            transport.get_state.return_value = None
            shared = ExecutionStateStore(context, transport)
            self.assertIsNone(shared.get("plan:1"))
            self.assertEqual(legacy.get("plan:1"), {"legacy": True})
