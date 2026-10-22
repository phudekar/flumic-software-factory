---
title: "Investigate runs, stages and logs"
---

## Find the failing execution

Open **Workflow runs** and choose a run. Stage cards show engine status and recorded attempts. Select a stage and use **View logs** to preserve stage/task context; worker detail links can preserve worker and retry identity too.

Inspect `error_type` and the bounded redacted `error_message` on failed attempts and model calls. Older records may not have a captured message. Startup errors are available in worker runtime logs rather than stage records.

## Keep the scope explicit

Stage filtering scopes event history, recorded model calls and cost using engine task identity. Attempt filtering further isolates a retry. Engine task logs lack retry identity and are hidden when an individual attempt is selected. URL-encoded filters survive reloads and shared links.

Event lists show bounded pages (up to 500 entries); a full page can mean more history exists. Worker attempt history also uses a bounded event page.

## Parsed and raw

Parsed mode extracts message/severity/stage metadata from structured JSON and preserves plain traceback text. Raw shows the same redacted records in their original representation. Raw does not decrypt conversation bodies or bypass redaction. The selected mode is kept in the URL.

## Model history

![Model history listing models, stages, token usage, estimated costs and completion status](/flumic-software-factory/images/control-plane-model-history.png)

The list exposes accounting and execution metadata without opening retained request or response content.

The model-history page lists retained captured calls. Open a call to inspect separate redacted input and result blocks; content reads are audited. Capture starts with instrumented workers and follows profile policy. API capture includes assembled system messages. CLI capture contains submitted guidance and final output, not a complete internal conversation.

[Traceability and accounting](/flumic-software-factory/observability/stage-accounting/) · [Error coverage audit](/flumic-software-factory/handbook/telemetry-error-audit/)

## Example: attempt-scoped planning logs

![Logs for a selected planning attempt with parsed severity, stage and worker identity](/flumic-software-factory/images/control-plane-stage-logs.png)

This actual local run completed its planning attempt. The attempt-scoped view correlates worker logs and model-call metadata without opening conversation content. Scroll position and filters are part of the shown view.
