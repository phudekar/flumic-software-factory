"""Cross-cutting operation boundary; engine retries stay in orchestration adapters."""

from contextlib import nullcontext
from functools import wraps
from factory.application.telemetry import incoming_trace
from factory.configuration import task_context


def operation(stage: str, *, exclusive: bool = False):
    def decorate(method):
        @wraps(method)
        def execute(self, input, *args, **kwargs):
            self.validate_repository(input.repository)
            resource = f"{input.repository}:issue:{input.issue_number}"
            attributes = {
                "repository": input.repository,
                "factory_run_id": input.factory_run_id,
                "issue_number": input.issue_number,
                "pull_number": getattr(input, "pull_number", None),
                "operation_id": f"v1:{resource}:{stage}",
            }
            claim = self.state.claim(resource, stage) if exclusive else nullcontext()
            with (
                task_context(self.task_settings(stage)),
                incoming_trace(input.trace_context),
                self.observer.observe(stage, attributes),
                claim,
            ):
                result = method(self, input, *args, **kwargs)
                if exclusive:
                    self.state.put(
                        f"progress:{resource}:{stage}", result.model_dump(mode="json")
                    )
                return result

        return execute

    return decorate
