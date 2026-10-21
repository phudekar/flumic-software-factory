# Control-plane error visibility audit

The audit found 10 retained failed-attempt events, all missing `error_message`.
Recent worker runtime logs included `AgentError` (including file size rejection)
and `OperationBusy` (another execution owning the same work item). Model
completion can succeed even though post-model patch validation then fails.

| Failure path | Previous gap | Implemented handling |
| --- | --- | --- |
| GitHub reads, API errors and repository validation | Failed stage status without exception text | Failed attempt includes redacted type/message and correlated ERROR log |
| CLI launch, exit, timeout and patch validation | SDK/runtime traceback carried the useful message | Same stage failure capture; received file names/sizes logged before validation |
| Model API request | Generic model failure discarded exception classification | Factory error preserves provider exception class and HTTP status without response body |
| Failed model accounting event | Type only | Bounded redacted factory error message included |
| Token budgets and unknown tokenizer | Validation failure happened before a model call | Stage validation error captured without fabricating a model call |
| Work-item lock conflicts and database errors | Only runtime error type was visible | Known factory/database failure message captured |
| Repository mismatch | Validation ran before observer entry | Validation moved inside observed operation |
| Workflow enqueue | Observer closed before the actual submission | Submission remains inside the dispatch observer |
| Durable discussion/PR checks | Polling repository calls had no operation observer | Individual polls observed; human wait intervals do not count as active work |
| Engine failures, including older worker versions | Workflow adapter discarded Hatchet `errorMessage` | Redacted latest task error exposed and rendered; never assigned to a specific historical retry |
| UI log view | Failed-attempt events omitted from logs | Failed attempts rendered at ERROR level; parsed/raw modes retain the same stage filters |

Messages are redacted before spooling and again on ingestion. Factory-controlled
messages, plain validation errors and SQLite errors are bounded to 4,096
characters. Unexpected SDK exception bodies are intentionally omitted; their
exception type remains visible. Model request failures expose safe classification
and status only. No prompt, provider response body or full traceback is added to
normal stage error metadata.

Bootstrap, configuration and startup script failures occur before a task/stage
exists. Inspect the worker's runtime logs (parsed or raw) for those failures.
If the control plane is unavailable, worker history delivery may be delayed or
incomplete. Startup hooks intentionally fail closed when profile resolution is
unavailable. Existing historical events without error text remain incomplete;
latest Hatchet task errors and runtime logs provide a separate diagnostic source.

Regression coverage checks oversized UTF-8 files, custom worker limits, file-name
logging without file contents, redacted stage/model failures, dispatch failure
capture, API ingestion, Hatchet task errors, model HTTP classification, and raw/
parsed UI behavior.
