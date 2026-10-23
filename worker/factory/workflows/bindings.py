"""Bounded JSON resolution with repository provenance and no expression engine."""

from dataclasses import dataclass
import json
from factory.workflows.references import (
    is_reference,
    parse_reference,
    json_bytes,
    CONTEXT_FIELDS,
)
from factory.workflows.definitions import BuiltinStage
from factory.workflows.action_schemas import validate_arguments
from factory.workflows.contracts import ActionContext


@dataclass(frozen=True)
class ParentOutput:
    project_id: str
    repository_id: str
    output: dict

    def __post_init__(self):
        if not isinstance(self.output, dict) or len(json_bytes(self.output)) > 1048576:
            raise ValueError("Parent output must be a JSON object of at most 1 MiB")


@dataclass(frozen=True)
class BindingOrigin:
    path: tuple[str | int, ...]
    stage_id: str
    project_id: str
    repository_id: str


@dataclass(frozen=True)
class ResolvedInputs:
    data: dict
    origins: tuple[BindingOrigin, ...]


def public_context(context):
    if isinstance(context, ActionContext):
        values = {
            key: getattr(context, key) for key in CONTEXT_FIELDS if key != "repository"
        }
        values["repository"] = context.settings.repository
        return values
    if not isinstance(context, dict):
        raise ValueError(
            "Expected immutable action context or reserved context mapping"
        )
    return {key: value for key, value in context.items() if key in CONTEXT_FIELDS}


class Resolver:
    def __init__(self, run_input, parents, context, stage=None):
        if not isinstance(run_input, dict) or len(json_bytes(run_input)) > 65536:
            raise ValueError("Run input must be a JSON object of at most 64 KiB")
        self.input = run_input
        self.parents = parents
        self.context = public_context(context)
        self.stage = stage
        self.remaining = 1048576
        self.origins = []

    def consume(self, size):
        self.remaining -= size
        if self.remaining < 0:
            raise ValueError("Resolved action input exceeds 1 MiB")

    def lookup(self, parts, path):
        if parts[0] == "input":
            value = self.input
            keys = parts[1:]
        elif parts[0] == "context":
            value = self.context
            keys = parts[1:]
        else:
            stage_id = parts[1]
            if self.stage and stage_id not in self.stage.after:
                raise ValueError("Producer must be a declared direct parent")
            parent = self.parents.get(stage_id)
            if not isinstance(
                parent, ParentOutput
            ) or parent.project_id != self.context.get("project_id"):
                raise ValueError("Parent output is unavailable in this project")
            value = parent.output
            keys = parts[3:]
            self.origins.append(
                BindingOrigin(path, stage_id, parent.project_id, parent.repository_id)
            )
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                raise ValueError("Binding mapping key is unavailable")
            value = value[key]
        return value

    def resolve(self, value, path=(), depth=0):
        if depth > 64:
            raise ValueError("JSON binding nesting exceeds 64 levels")
        if is_reference(value):
            picked = self.lookup(parse_reference(value["$ref"]), path)
            encoded = json_bytes(picked)
            # Charge before cloning, so repeated large references cannot allocate
            # an unbounded intermediate object before the final size check.
            self.consume(len(encoded))
            return json.loads(encoded)
        if isinstance(value, dict):
            self.consume(2 + max(0, len(value) - 1))
            result = {}
            for key, item in value.items():
                self.consume(len(json_bytes(key)) + 1)
                result[key] = self.resolve(item, (*path, key), depth + 1)
            return result
        if isinstance(value, list):
            self.consume(2 + max(0, len(value) - 1))
            return [
                self.resolve(item, (*path, index), depth + 1)
                for index, item in enumerate(value)
            ]
        self.consume(len(json_bytes(value)))
        return value


def resolve_bindings(stage, run_input, parent_results, context):
    resolver = Resolver(run_input, parent_results, context, stage)
    if resolver.context.get("stage_id", stage.id) != stage.id:
        raise ValueError("Action context belongs to another stage")
    value = resolver.resolve(stage.inputs)
    if not isinstance(value, dict):
        raise ValueError("Action inputs must resolve to an object")
    if isinstance(stage, BuiltinStage):
        validate_arguments(stage.action, value)
        if stage.action != "core.echo@1" and any(
            origin.repository_id != resolver.context.get("repository_id")
            for origin in resolver.origins
        ):
            raise ValueError(
                "Repository-affine builtin input has another repository origin"
            )
    return ResolvedInputs(value, tuple(resolver.origins))


def condition_matches(condition, run_input, parent_results, context):
    if condition is None:
        return True
    resolver = Resolver(run_input, parent_results, context)
    value = resolver.lookup(parse_reference(condition.ref), ())
    if isinstance(value, (dict, list)):
        raise ValueError("Condition references must resolve to JSON scalars")
    if isinstance(value, bool) or isinstance(condition.equals, bool):
        return type(value) is type(condition.equals) and value == condition.equals
    return value == condition.equals
