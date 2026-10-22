"""Cross-cutting operation boundary; engine retries stay in orchestration adapters."""

from contextlib import nullcontext, contextmanager, ExitStack
import logging
import time
from factory.domain.models import OperationBusy
from functools import wraps
from factory.application.telemetry import incoming_trace
from factory.configuration import task_context


@contextmanager
def claim_with_wait(state, resource, stage, task):
    # Keep acquisition bounded and leave execution time for the stage body.
    wait = min(task.lock_wait_seconds, max(0, task.timeout_seconds - 1))
    deadline = time.monotonic() + wait
    warned = False
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(state.claim(resource, stage))
                break
            except OperationBusy:
                remaining = deadline - time.monotonic()
                if not warned:
                    logging.getLogger("issue-worker.operations").info(
                        "Waiting for work-item owner: resource=%s stage=%s",
                        resource,
                        stage,
                    )
                    warned = True
                if remaining <= 0:
                    raise
                time.sleep(min(1.0, remaining))
        if warned:
            logging.getLogger("issue-worker.operations").info(
                "Work-item claim acquired after waiting"
            )
        yield


def operation(stage: str, *, exclusive: bool = False):
    def decorate(method):
        @wraps(method)
        def execute(self, input, *args, **kwargs):
            resource = f"{input.repository}:issue:{input.issue_number}"
            attributes = {
                "repository": input.repository,
                "factory_run_id": input.factory_run_id,
                "issue_number": input.issue_number,
                "pull_number": getattr(input, "pull_number", None),
                "operation_id": f"v1:{resource}:{stage}",
            }
            task = self.task_settings(stage)
            claim = (
                claim_with_wait(self.state, resource, stage, task)
                if exclusive
                else nullcontext()
            )
            with (
                task_context(task),
                incoming_trace(input.trace_context),
                self.observer.observe(stage, attributes),
                claim,
            ):
                self.validate_repository(input.repository)
                result = method(self, input, *args, **kwargs)
                if exclusive:
                    self.state.put(
                        f"progress:{resource}:{stage}", result.model_dump(mode="json")
                    )
                return result

        return execute

    return decorate
