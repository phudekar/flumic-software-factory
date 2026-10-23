"""Published graph validation and non-executable JSON bindings."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import unittest


def echo(identifier="echo", **values):
    return {
        "id": identifier,
        "executor": "builtin",
        "action": "core.echo@1",
        "role": "simple",
        "pool_id": "pool",
        "inputs": {},
        **values,
    }


def graph(stages=None):
    return {
        "schema_version": 1,
        "name": "example",
        "repositories": ["backend", "frontend"],
        "default_repository": "backend",
        "stages": stages or [echo()],
    }


class DefinitionTests(unittest.TestCase):
    def parse(self, document):
        from factory.workflows.definitions import WorkflowDefinition

        return WorkflowDefinition.model_validate(document)

    def test_mixed_multi_repository_graph_roundtrips_without_executing_code(self):
        document = graph(
            [
                echo(
                    "load",
                    action="issue.load@1",
                    inputs={
                        "command": {"issue_number": {"$ref": "input.issue_number"}}
                    },
                ),
                {
                    "id": "check",
                    "executor": "docker",
                    "role": "simple",
                    "pool_id": "pool",
                    "repository": "frontend",
                    "after": ["load"],
                    "image": "ghcr.io/example/checker@sha256:" + "0" * 64,
                    "command": ["python", "/app/check.py"],
                    "inputs": {"issue": {"$ref": "stages.load.output"}},
                },
            ]
        )
        definition = self.parse(document)
        clone = self.parse(definition.model_dump(mode="json"))
        self.assertEqual(clone.stages[1].repository, "frontend")
        self.assertEqual(clone.stages[1].limits.memory_mib, 512)
        self.assertEqual(clone.stages[1].network, "none")
        self.assertEqual(clone.stages[1].timeout_seconds, 300)

    def test_graph_rejects_missing_duplicate_cyclic_and_undeclared_parents(self):
        cases = [
            [echo("a", after=["missing"])],
            [echo("a"), echo("a")],
            [echo("a", after=["b"]), echo("b", after=["a"])],
            [echo("a"), echo("b", inputs={"value": {"$ref": "stages.a.output"}})],
            [echo("a", after=["a"])],
        ]
        for stages in cases:
            with self.subTest(stages=stages), self.assertRaises(ValueError):
                self.parse(graph(stages))

    def test_unknown_versions_unsafe_options_foreign_alias_and_reserved_commands_fail(
        self,
    ):
        cases = [
            echo(action="issue.load@99"),
            echo(repository="foreign"),
            echo(
                action="issue.load@1",
                inputs={"command": {"issue_number": 1, "repository": "foreign/repo"}},
            ),
            echo(action="issue.load@1", inputs={"command": {"issue_number": -1}}),
            echo(
                action="change.implement@1",
                inputs={
                    "command": {"issue_number": 1},
                    "triage": {"$ref": "input.triage"},
                },
            ),
            {
                "id": "job",
                "executor": "docker",
                "role": "simple",
                "pool_id": "pool",
                "image": "python:latest",
                "command": ["python"],
            },
            {
                "id": "job",
                "executor": "docker",
                "role": "simple",
                "pool_id": "pool",
                "image": "python@sha256:" + "a" * 64,
                "command": ["python"],
                "volumes": {"/": "/host"},
            },
        ]
        for stage in cases:
            with self.subTest(stage=stage), self.assertRaises(ValueError):
                self.parse(graph([stage]))

    def test_document_and_stage_bounds_are_enforced(self):
        self.parse(graph([echo("s-" + str(i)) for i in range(64)]))
        for value in [
            graph([echo("s-" + str(i)) for i in range(65)]),
            graph([echo(inputs={"large": "x" * 262144})]),
            graph([echo(overrides={"repository": "a/b"})]),
        ]:
            with self.assertRaises(ValueError):
                self.parse(value)
        empty = graph()
        empty["stages"] = []
        with self.assertRaises(ValueError):
            self.parse(empty)

    def test_static_repository_affinity_rejects_backend_publish_into_frontend_review(
        self,
    ):
        definition = graph(
            [
                echo(
                    "implement",
                    action="change.implement@1",
                    role="developer",
                    inputs={
                        "command": {"issue_number": 1},
                        "triage": {"$ref": "input.triage"},
                    },
                ),
                echo(
                    "review",
                    action="change.review@1",
                    repository="frontend",
                    after=["implement"],
                    inputs={
                        "command": {"issue_number": 1},
                        "publish": {"$ref": "stages.implement.output"},
                    },
                ),
            ]
        )
        with self.assertRaises(ValueError):
            self.parse(definition)

    def test_conditions_are_limited_and_producer_must_be_direct_parent(self):
        self.parse(
            graph(
                [
                    echo("a"),
                    echo(
                        "b",
                        after=["a"],
                        when={"ref": "stages.a.output.ready", "equals": True},
                    ),
                ]
            )
        )
        for when in [
            {"ref": "input.ready", "equals": {}},
            {"ref": "input.ready", "equals": True, "eval": "danger()"},
            {"ref": "stages.a.output.ready", "equals": True},
        ]:
            with self.assertRaises(ValueError):
                self.parse(graph([echo("b", when=when)]))


class BindingTests(unittest.TestCase):
    def resolve(self, stage, input=None, parents=None, context=None):
        from factory.workflows.bindings import resolve_bindings
        from factory.workflows.definitions import WorkflowDefinition

        definition = WorkflowDefinition.model_validate(graph([echo("parent"), stage]))
        return resolve_bindings(
            definition.stages[1],
            input or {},
            parents or {},
            context
            or {
                "project_id": "project",
                "repository_id": "backend-id",
                "repository": "a/backend",
                "run_id": "run",
                "stage_id": "child",
                "execution_id": "execution",
                "attempt": 1,
            },
        )

    def test_only_sole_ref_objects_resolve_and_literals_are_copied(self):
        literal = {"$ref": "input.missing", "label": "literal"}
        original = {"nested": {"key": ["a", True, None]}}
        result = self.resolve(
            echo(
                "child",
                inputs={
                    "value": {"$ref": "input.nested"},
                    "literal": literal,
                    "repo": {"$ref": "context.repository"},
                },
            ),
            input=original,
        )
        self.assertEqual(
            result.data,
            {
                "value": {"key": ["a", True, None]},
                "literal": literal,
                "repo": "a/backend",
            },
        )
        result.data["value"]["key"].append("changed")
        self.assertEqual(original, {"nested": {"key": ["a", True, None]}})

    def test_traversal_attributes_indexing_and_missing_values_fail(self):
        for ref in [
            "input.__class__",
            "input.a[0]",
            "input.a()",
            "input..a",
            "context.credentials",
            "stages.parent.output.__dict__",
        ]:
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                self.resolve(
                    echo("child", after=["parent"], inputs={"value": {"$ref": ref}})
                )
        with self.assertRaises(ValueError):
            self.resolve(
                echo("child", inputs={"value": {"$ref": "input.a.0"}}),
                input={"a": ["not-a-mapping"]},
            )
        with self.assertRaises(ValueError):
            self.resolve(echo("child", inputs={"value": {"$ref": "input.missing"}}))

    def test_runtime_provenance_cannot_feed_another_repository_to_builtin(self):
        from factory.workflows.bindings import ParentOutput

        stage = echo(
            "child",
            action="change.review@1",
            after=["parent"],
            inputs={
                "command": {"issue_number": 1},
                "publish": {"$ref": "stages.parent.output"},
            },
        )
        parent = ParentOutput(
            project_id="project",
            repository_id="other-repo",
            output={"status": "opened", "issue_number": 1, "pull_number": 5},
        )
        with self.assertRaises(ValueError):
            self.resolve(stage, parents={"parent": parent})
        stage["action"] = "core.echo@1"
        resolved = self.resolve(stage, parents={"parent": parent})
        self.assertEqual(resolved.origins[0].repository_id, "other-repo")
        self.assertEqual(resolved.data["publish"]["pull_number"], 5)

    def test_dynamic_arguments_and_resolved_size_are_validated_at_runtime(self):
        with self.assertRaises(ValueError):
            self.resolve(
                echo(
                    "child",
                    action="issue.load@1",
                    inputs={"command": {"issue_number": {"$ref": "input.number"}}},
                ),
                input={"number": "not-an-integer"},
            )
        with self.assertRaises(ValueError):
            self.resolve(echo("child"), input={"large": "a" * 65536})
        from factory.workflows.bindings import ParentOutput

        parent = ParentOutput(
            project_id="project",
            repository_id="backend-id",
            output={"large": "a" * 600000},
        )
        with self.assertRaises(ValueError):
            self.resolve(
                echo(
                    "child",
                    after=["parent"],
                    inputs={
                        "one": {"$ref": "stages.parent.output"},
                        "two": {"$ref": "stages.parent.output"},
                    },
                ),
                parents={"parent": parent},
            )

    def test_json_condition_equality_does_not_equate_boolean_and_number(self):
        from factory.workflows.bindings import condition_matches
        from factory.workflows.definitions import Condition

        self.assertFalse(
            condition_matches(
                Condition(ref="input.value", equals=True), {"value": 1}, {}, {}
            )
        )
        self.assertTrue(
            condition_matches(
                Condition(ref="input.value", equals=1), {"value": 1.0}, {}, {}
            )
        )


class DefinitionEdgeTests(unittest.TestCase):
    def test_normalized_published_document_also_obeys_256k_bound(self):
        import json
        from factory.workflows.definitions import WorkflowDefinition

        document = graph([echo(inputs={"padding": "x" * 261800})])
        self.assertLess(
            len(json.dumps(document, separators=(",", ":")).encode()), 262144
        )
        with self.assertRaises(ValueError):
            WorkflowDefinition.model_validate(document)

    def test_builtin_whole_input_reference_keeps_repository_affinity(self):
        from factory.workflows.definitions import WorkflowDefinition

        document = graph(
            [
                echo("parent"),
                echo(
                    "review",
                    action="change.review@1",
                    repository="frontend",
                    after=["parent"],
                    inputs={"$ref": "stages.parent.output"},
                ),
            ]
        )
        with self.assertRaises(ValueError):
            WorkflowDefinition.model_validate(document)

    def test_docker_argv_allows_empty_argument_but_not_empty_executable_or_nul(self):
        from factory.workflows.definitions import WorkflowDefinition

        stage = {
            "id": "job",
            "executor": "docker",
            "role": "simple",
            "pool_id": "pool",
            "image": "example/job@sha256:" + "a" * 64,
            "command": ["echo", ""],
        }
        WorkflowDefinition.model_validate(graph([stage]))
        for command in [[""], ["echo", "bad\x00value"]]:
            with self.assertRaises(ValueError):
                WorkflowDefinition.model_validate(
                    graph([{**stage, "command": command}])
                )

    def test_non_json_mapping_keys_and_nonfinite_conditions_are_rejected(self):
        from factory.workflows.definitions import WorkflowDefinition, Condition

        with self.assertRaises(ValueError):
            WorkflowDefinition.model_validate(graph([echo(inputs={1: "value"})]))
        with self.assertRaises(ValueError):
            Condition(ref="input.number", equals=float("nan"))

    def test_literal_type_errors_cannot_hide_next_to_dynamic_values(self):
        from factory.workflows.definitions import WorkflowDefinition

        bad_states = [
            {
                "issue_number": 1,
                "state": "open",
                "labels": [1, {"$ref": "input.label"}],
            },
            {
                "issue_number": {"nested": {"$ref": "input.number"}},
                "state": "open",
                "labels": [],
            },
        ]
        for state in bad_states:
            with self.assertRaises(ValueError):
                WorkflowDefinition.model_validate(
                    graph(
                        [
                            echo(
                                action="issue.plan@1",
                                inputs={"command": {"issue_number": 1}, "state": state},
                            )
                        ]
                    )
                )
