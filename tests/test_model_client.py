import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from model_client import (
    IssueWorkPlan,
    ModelClient,
    ModelError,
    _parse_max_patch_iterations,
)


class ModelClientTests(unittest.TestCase):
    def setUp(self):
        self.client = ModelClient.__new__(ModelClient)

    def test_patch_iteration_setting_accepts_values_within_configured_bounds(self):
        self.assertEqual(_parse_max_patch_iterations("1"), 1)
        self.assertEqual(_parse_max_patch_iterations("10"), 10)
        self.assertEqual(_parse_max_patch_iterations("20"), 20)

    def test_patch_iteration_setting_rejects_invalid_values(self):
        for value in ("0", "21", "many"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "MAX_PATCH_ITERATIONS"):
                    _parse_max_patch_iterations(value)

    def test_empty_completion_logs_response_metadata_without_content(self):
        sensitive_prompt = "private issue and review details"
        sensitive_refusal = "private refusal details"
        response = SimpleNamespace(
            id="chatcmpl-test",
            model="gpt-5-nano",
            choices=[
                SimpleNamespace(
                    finish_reason="length",
                    message=SimpleNamespace(content=None, refusal=sensitive_refusal),
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=123,
                completion_tokens=2048,
                total_tokens=2171,
            ),
        )
        self.client.model = "gpt-5-nano"
        self.client.client = Mock()
        self.client.client.chat.completions.create.return_value = response

        with self.assertLogs("issue-worker.model", level="WARNING") as captured:
            with self.assertRaisesRegex(ModelError, "empty response"):
                self.client._complete([{"role": "user", "content": sensitive_prompt}])

        log_output = "\n".join(captured.output)
        self.assertIn("response_id=chatcmpl-test", log_output)
        self.assertIn("finish_reason=length", log_output)
        self.assertIn("refusal_present=True", log_output)
        self.assertIn("completion_tokens=2048", log_output)
        self.assertNotIn(sensitive_prompt, log_output)
        self.assertNotIn(sensitive_refusal, log_output)

    def test_empty_completion_without_choices_raises_model_error(self):
        self.client.model = "gpt-5-nano"
        self.client.client = Mock()
        self.client.client.chat.completions.create.return_value = SimpleNamespace(
            id="chatcmpl-empty",
            model="gpt-5-nano",
            choices=[],
            usage=None,
        )

        with self.assertLogs("issue-worker.model", level="WARNING"):
            with self.assertRaisesRegex(ModelError, "empty response"):
                self.client._complete(
                    [{"role": "user", "content": "private review details"}]
                )

    def test_triage_selects_only_paths_from_the_allowlisted_inventory(self):
        self.client._complete = lambda messages: json.dumps(
            {
                "needs_discussion": False,
                "questions": [],
                "source_paths": ["src/worker.py"],
            }
        )

        result = self.client.triage(
            title="Validate worker inputs",
            body="Reject invalid IDs.",
            comments=[],
            source_paths=["src/worker.py", "src/github.py"],
        )

        self.assertEqual(result.source_paths, ["src/worker.py"])

    def test_plan_issue_creates_independent_subtasks_with_acceptance_criteria(self):
        captured_messages = []

        def complete(messages):
            captured_messages.extend(messages)
            return json.dumps(
                {
                    "tasks": [
                        {
                            "title": "Add task input",
                            "objective": "Provide a form for entering a task.",
                            "acceptance_criteria": [
                                "The form accepts a non-empty title.",
                                "Submitting adds the task to the list.",
                            ],
                        }
                    ]
                }
            )

        self.client._complete = complete
        plan = self.client.plan_issue(
            title="Build a task app",
            body="Users can create and complete tasks.",
            comments=["Keep the initial scope unauthenticated."],
            source_paths=["frontend/src/App.js"],
        )

        self.assertIsInstance(plan, IssueWorkPlan)
        self.assertEqual(len(plan.tasks), 1)
        prompt = json.loads(captured_messages[1]["content"])
        self.assertEqual(
            prompt["issue_requirements"],
            "Users can create and complete tasks.",
        )
        self.assertIn("independently and in parallel", captured_messages[0]["content"])

    def test_triage_discards_paths_not_in_the_approved_inventory(self):
        self.client._complete = lambda messages: json.dumps(
            {
                "needs_discussion": False,
                "questions": [],
                "source_paths": [".env", "src/worker.py"],
            }
        )

        result = self.client.triage(
            title="Issue",
            body="Do something.",
            comments=[],
            source_paths=["src/worker.py"],
        )

        self.assertEqual(result.source_paths, ["src/worker.py"])

    def test_triage_returns_no_paths_when_all_model_selections_are_unapproved(self):
        self.client._complete = lambda messages: json.dumps(
            {
                "needs_discussion": False,
                "questions": [],
                "source_paths": [".env"],
            }
        )

        result = self.client.triage(
            title="Issue",
            body="Do something.",
            comments=[],
            source_paths=["src/worker.py"],
        )

        self.assertEqual(result.source_paths, [])

    def test_resolved_discussion_prevents_another_clarification_cycle(self):
        captured_messages = []

        def complete(messages):
            captured_messages.extend(messages)
            return json.dumps(
                {
                    "needs_discussion": True,
                    "questions": ["Please clarify the expected behavior."],
                    "source_paths": ["src/worker.py"],
                }
            )

        self.client._complete = complete

        comment = "The expected behavior is described here."
        result = self.client.triage(
            title="Issue",
            body="Implement the requested behavior.",
            comments=[comment],
            source_paths=["src/worker.py"],
            discussion_resolved=True,
        )

        self.assertFalse(result.needs_discussion)
        self.assertEqual(result.questions, [])
        self.assertEqual(result.source_paths, ["src/worker.py"])
        self.assertIn(comment, captured_messages[1]["content"])
        self.assertIn(
            "Do not request further clarification", captured_messages[0]["content"]
        )

    def test_patch_rejects_changes_outside_the_selected_files(self):
        self.client._complete = lambda messages: json.dumps(
            {
                "pull_title": "Fix issue",
                "pull_body": "Closes the reported issue.",
                "is_complete": True,
                "progress": "Implemented the reported fix.",
                "changes": [{"path": "src/other.py", "content": "print('new')\n"}],
            }
        )

        with self.assertRaisesRegex(ModelError, "not provided to the model"):
            self.client.generate_patch(
                title="Issue",
                body="Do something.",
                comments=[],
                files={"src/worker.py": "print('before')\n"},
                existing_paths={"src/worker.py", "src/other.py"},
            )

    def test_patch_can_create_a_new_safe_text_file(self):
        self.client._complete = lambda messages: json.dumps(
            {
                "pull_title": "Add documentation",
                "pull_body": "Add issue-specific documentation.",
                "is_complete": True,
                "progress": "Added the requested guide.",
                "changes": [{"path": "docs/new-guide.md", "content": "# New guide\n"}],
            }
        )

        result = self.client.generate_patch(
            title="Add documentation",
            body="Create a guide.",
            comments=[],
            files={},
            existing_paths={"README.md", "src/worker.py"},
        )

        self.assertEqual(result.changes[0].path, "docs/new-guide.md")

    def test_patch_generation_iterates_with_prior_changes_until_complete(self):
        captured_messages = []
        responses = iter(
            [
                {
                    "pull_title": "Build the requested feature",
                    "pull_body": "Implements the requested feature.",
                    "is_complete": False,
                    "progress": "Added the main component; styling remains.",
                    "changes": [
                        {
                            "path": "src/App.js",
                            "content": "export default function App() {}\n",
                        }
                    ],
                },
                {
                    "pull_title": "Build the requested feature",
                    "pull_body": "Implements the requested feature.",
                    "is_complete": True,
                    "progress": "Added the component and its styling.",
                    "changes": [
                        {
                            "path": "src/App.css",
                            "content": ".app { display: flex; }\n",
                        }
                    ],
                },
            ]
        )

        def complete(messages):
            captured_messages.append(messages)
            return json.dumps(next(responses))

        self.client.model = "gpt-5-nano"
        self.client._complete = complete
        result = self.client.generate_patch(
            title="Build a feature",
            body="Create a component and style it.",
            comments=[],
            files={"src/App.js": "export default function App() { return null; }\n"},
            existing_paths={"src/App.js"},
        )

        self.assertTrue(result.is_complete)
        self.assertEqual(
            {change.path: change.content for change in result.changes},
            {
                "src/App.js": "export default function App() {}\n",
                "src/App.css": ".app { display: flex; }\n",
            },
        )
        self.assertEqual(len(captured_messages), 2)
        second_round = json.loads(captured_messages[1][1]["content"])
        self.assertEqual(second_round["round"], 2)
        self.assertEqual(
            second_round["previous_progress"],
            "Added the main component; styling remains.",
        )
        self.assertEqual(
            second_round["current_working_files"]["src/App.js"],
            "export default function App() {}\n",
        )

    def test_patch_generation_can_accumulate_changes_across_rounds(self):
        responses = iter(
            [
                {
                    "pull_title": "Add requested documents",
                    "pull_body": "Adds the requested documents.",
                    "is_complete": False,
                    "progress": "Added the first five documents.",
                    "changes": [
                        {
                            "path": f"docs/guide-{index}.md",
                            "content": f"Guide {index}\n",
                        }
                        for index in range(5)
                    ],
                },
                {
                    "pull_title": "Add requested documents",
                    "pull_body": "Adds the requested documents.",
                    "is_complete": True,
                    "progress": "Added all six documents.",
                    "changes": [{"path": "docs/guide-5.md", "content": "Guide 5\n"}],
                },
            ]
        )
        self.client.model = "gpt-5-nano"
        self.client._complete = lambda messages: json.dumps(next(responses))

        result = self.client.generate_patch(
            title="Add documents",
            body="Add six guides.",
            comments=[],
            files={},
            existing_paths=set(),
        )

        self.assertEqual(len(result.changes), 6)

    def test_patch_generation_rejects_incomplete_round_without_progress(self):
        self.client.model = "gpt-5-nano"
        self.client._complete = lambda messages: json.dumps(
            {
                "pull_title": "Build a feature",
                "pull_body": "Implements the requested feature.",
                "is_complete": False,
                "progress": "Nothing done yet.",
                "changes": [],
            }
        )

        with self.assertRaisesRegex(ModelError, "made no progress"):
            self.client.generate_patch(
                title="Build a feature",
                body="Create a component.",
                comments=[],
                files={},
                existing_paths=set(),
            )

    def test_patch_generation_fails_when_round_limit_is_reached(self):
        self.client.model = "gpt-5-nano"
        round_number = 0

        def incomplete_round(messages):
            nonlocal round_number
            round_number += 1
            return json.dumps(
                {
                    "pull_title": "Build a feature",
                    "pull_body": "Implements the requested feature.",
                    "is_complete": False,
                    "progress": f"Completed round {round_number}.",
                    "changes": [
                        {
                            "path": "src/App.js",
                            "content": f"export const round = {round_number};\n",
                        }
                    ],
                }
            )

        self.client._complete = incomplete_round

        with patch("model_client.MAX_PATCH_ITERATIONS", 2):
            with self.assertRaisesRegex(ModelError, "did not complete within 2 rounds"):
                self.client.generate_patch(
                    title="Build a feature",
                    body="Complete the implementation.",
                    comments=[],
                    files={"src/App.js": "export const round = 0;\n"},
                    existing_paths={"src/App.js"},
                )

        self.assertEqual(round_number, 2)

    def test_patch_includes_pull_request_feedback_as_review_context(self):
        captured_messages = []
        feedback = "Comment 42 (inline review) on src/worker.py: Check this error path."

        def complete(messages):
            captured_messages.extend(messages)
            return json.dumps(
                {
                    "pull_title": "Address review feedback",
                    "pull_body": "Handle the reported error path.",
                    "is_complete": True,
                    "progress": "Handled the reported error path.",
                    "changes": [
                        {
                            "path": "src/worker.py",
                            "content": "raise RuntimeError('handled')\n",
                        }
                    ],
                }
            )

        self.client._complete = complete
        self.client.generate_patch(
            title="Issue",
            body="Implement the behavior.",
            comments=[],
            files={"src/worker.py": "pass\n"},
            existing_paths={"src/worker.py"},
            review_feedback=[feedback],
        )

        prompt = json.loads(captured_messages[1]["content"])
        self.assertEqual(prompt["pull_request_review_feedback"], [feedback])
        self.assertIn(
            "Address actionable code-change requests", captured_messages[0]["content"]
        )

    def test_pull_request_review_uses_requirements_and_related_current_source(self):
        captured_messages = []

        def complete(messages):
            captured_messages.extend(messages)
            return json.dumps(
                {
                    "findings": [
                        {
                            "kind": "suggestion",
                            "severity": "low",
                            "path": "src/tasks.py",
                            "line": 12,
                            "description": "Add a focused test for this branch.",
                        }
                    ]
                }
            )

        self.client._complete = complete

        review = self.client.review_pull_request(
            issue_title="Complete tasks",
            issue_body="Tasks must be closed after approval.",
            issue_comments=["This also covers tasks reopened by a user."],
            pull_title="Handle task state",
            pull_body="Implements the requested state transition.",
            diff=[
                {
                    "path": "src/worker.py",
                    "patch": "@@ -10,1 +10,2 @@\n+return pending",
                    "status": "modified",
                }
            ],
            current_files={
                "src/worker.py": "def process():\n    return pending\n",
                "src/tasks.py": "def close_task(task):\n    task.close()\n",
            },
        )

        self.assertEqual(len(review.findings), 1)
        self.assertEqual(review.findings[0].kind, "suggestion")
        self.assertEqual(review.findings[0].path, "src/tasks.py")
        prompt = json.loads(captured_messages[1]["content"])
        self.assertEqual(
            prompt["issue_requirements"],
            "Tasks must be closed after approval.",
        )
        self.assertEqual(
            prompt["issue_comments"],
            ["This also covers tasks reopened by a user."],
        )
        self.assertEqual(
            prompt["current_source_files"]["src/tasks.py"],
            "def close_task(task):\n    task.close()\n",
        )

    def test_pull_request_review_rejects_findings_outside_the_diff(self):
        self.client._complete = lambda messages: json.dumps(
            {
                "findings": [
                    {
                        "kind": "bug",
                        "severity": "medium",
                        "path": "README.md",
                        "line": 1,
                        "description": "Documentation is incomplete.",
                    }
                ]
            }
        )

        with self.assertRaisesRegex(ModelError, "outside the review context"):
            self.client.review_pull_request(
                issue_title="Issue",
                issue_body="Requirements",
                issue_comments=[],
                pull_title="PR",
                pull_body="Body",
                diff=[
                    {
                        "path": "src/worker.py",
                        "patch": "@@ diff",
                        "status": "modified",
                    }
                ],
                current_files={"src/worker.py": "current file contents"},
            )

    def test_patch_rejects_creating_a_path_that_already_exists_unselected(self):
        self.client._complete = lambda messages: json.dumps(
            {
                "pull_title": "Modify an unselected file",
                "pull_body": "Changes an existing file.",
                "is_complete": True,
                "progress": "Updated the README.",
                "changes": [{"path": "README.md", "content": "replacement\n"}],
            }
        )

        with self.assertRaisesRegex(ModelError, "existing file not provided"):
            self.client.generate_patch(
                title="Issue",
                body="Do something.",
                comments=[],
                files={"src/worker.py": "print('before')\n"},
                existing_paths={"src/worker.py", "README.md"},
            )

    def test_patch_cannot_create_a_protected_file(self):
        self.client._complete = lambda messages: json.dumps(
            {
                "pull_title": "Add config",
                "pull_body": "Adds configuration.",
                "is_complete": True,
                "progress": "Added configuration.",
                "changes": [{"path": "new-config.json", "content": "{}\n"}],
            }
        )

        with self.assertRaisesRegex(ModelError, "protected or unsupported path"):
            self.client.generate_patch(
                title="Issue",
                body="Do something.",
                comments=[],
                files={},
                existing_paths=set(),
            )

    def test_request_messages_do_not_include_api_key_from_environment(self):
        api_key = "test-api-key-that-must-not-enter-prompt"
        response_body = json.dumps(
            {
                "needs_discussion": False,
                "questions": [],
                "source_paths": ["src/worker.py"],
            }
        )
        fake_openai_client = Mock()
        fake_openai_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=response_body))]
        )
        with patch.dict("os.environ", {"OPENAI_API_KEY": api_key}):
            with patch(
                "model_client.OpenAI", return_value=fake_openai_client
            ) as openai_constructor:
                client = ModelClient()
                client.triage(
                    title="Validate input",
                    body="Reject invalid input.",
                    comments=[],
                    source_paths=["src/worker.py"],
                )

        sent_messages = fake_openai_client.chat.completions.create.call_args.kwargs[
            "messages"
        ]
        self.assertNotIn(api_key, json.dumps(sent_messages))
        self.assertEqual(openai_constructor.call_args.kwargs["api_key"], api_key)


if __name__ == "__main__":
    unittest.main()
