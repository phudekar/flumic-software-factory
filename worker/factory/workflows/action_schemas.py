"""Fixed action metadata/argument schemas. Runtime implementations live in actions.py."""

from dataclasses import dataclass
from typing import Annotated, get_args, get_origin
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from factory.domain import models
from factory.workflows.references import is_reference, contains_reference


# Only actions whose entire implementation has no external mutations may
# acknowledge an exception as terminal without operator reconciliation.
READ_ONLY_FAILURE_ACTIONS = frozenset({"issue.load@1"})


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class IssueCommand(Arguments):
    issue_number: int = Field(gt=0)


class PullCommand(IssueCommand):
    pull_number: int = Field(gt=0)
    pull_url: str = Field(min_length=1, max_length=2048)
    branch: str = Field(min_length=1, max_length=240)


class IssueValue(models.IssueState):
    model_config = ConfigDict(extra="forbid", strict=True)


class PlanValue(models.IssuePlanState):
    model_config = ConfigDict(extra="forbid", strict=True)


class TriageValue(models.TriageState):
    model_config = ConfigDict(extra="forbid", strict=True)


class PublishValue(models.PublishState):
    model_config = ConfigDict(extra="forbid", strict=True)


class EventValue(models.EventResult):
    model_config = ConfigDict(extra="forbid", strict=True)


class LoadArguments(Arguments):
    command: IssueCommand


class PlanArguments(LoadArguments):
    state: IssueValue


class TriageArguments(PlanArguments):
    plan: PlanValue


class ImplementArguments(LoadArguments):
    triage: TriageValue


class ReviewArguments(LoadArguments):
    publish: PublishValue


class RecordArguments(LoadArguments):
    result: EventValue


class PullArguments(Arguments):
    command: PullCommand


@dataclass(frozen=True)
class ActionDescriptor:
    id: str
    role: str | None
    arguments: type[BaseModel] | None
    output: type[BaseModel] | None


CATALOG = {
    item.id: item
    for item in (
        ActionDescriptor("core.echo@1", None, None, None),
        ActionDescriptor("issue.load@1", "simple", LoadArguments, models.IssueState),
        ActionDescriptor(
            "issue.plan@1", "simple", PlanArguments, models.IssuePlanState
        ),
        ActionDescriptor(
            "issue.triage@1", "simple", TriageArguments, models.TriageState
        ),
        ActionDescriptor(
            "change.implement@1", "developer", ImplementArguments, models.PublishState
        ),
        ActionDescriptor(
            "change.review@1", "simple", ReviewArguments, models.EventResult
        ),
        ActionDescriptor(
            "workflow.record@1", "simple", RecordArguments, models.EventResult
        ),
        ActionDescriptor(
            "pull.feedback@1", "developer", PullArguments, models.EventResult
        ),
        ActionDescriptor("pull.verify@1", "simple", PullArguments, models.EventResult),
    )
}


def _known_value(annotation, value):
    if is_reference(value):
        return
    if not contains_reference(value):
        TypeAdapter(annotation).validate_python(value, strict=True)
        return
    base = (
        get_args(annotation)[0] if get_origin(annotation) is Annotated else annotation
    )
    if isinstance(base, type) and issubclass(base, BaseModel):
        _known_fields(base, value)
    elif get_origin(base) is list:
        if not isinstance(value, list):
            raise ValueError("Builtin list argument must be a list or a sole reference")
        if get_origin(annotation) is Annotated:
            for bound in get_args(annotation)[1:]:
                minimum = getattr(bound, "min_length", None)
                maximum = getattr(bound, "max_length", None)
                if (minimum is not None and len(value) < minimum) or (
                    maximum is not None and len(value) > maximum
                ):
                    raise ValueError("Builtin list argument violates its size bounds")
        for item in value:
            _known_value(get_args(base)[0], item)
    else:
        # A nested reference cannot change an enclosing object/list into a scalar.
        TypeAdapter(annotation).validate_python(value, strict=True)


def _known_fields(model, value):
    if is_reference(value):
        return
    if not isinstance(value, dict):
        raise ValueError("Builtin arguments must be objects")
    if set(value) - set(model.model_fields):
        raise ValueError("Unknown or reserved builtin argument")
    for key, field in model.model_fields.items():
        if key not in value:
            if field.is_required():
                raise ValueError("Required builtin argument is missing")
            continue
        item = value[key]
        annotation = (
            Annotated[field.annotation, *field.metadata]
            if field.metadata
            else field.annotation
        )
        _known_value(annotation, item)
    if not contains_reference(value):
        model.model_validate(value, strict=True)


def validate_arguments(action, inputs, *, dynamic=False):
    descriptor = CATALOG.get(action)
    if descriptor is None:
        raise ValueError("Unknown action version")
    if descriptor.arguments is None:
        return inputs
    if dynamic:
        _known_fields(descriptor.arguments, inputs)
    else:
        descriptor.arguments.model_validate(inputs, strict=True)
    return inputs
