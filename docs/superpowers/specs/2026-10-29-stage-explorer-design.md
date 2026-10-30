# Inspect Run stage explorer

Approved direction: option 1 and the interactive stage-explorer preview, accepted on 2026-10-29.

## Intent

Replace the long, nested Inspect Run page with a stage list and one selected-stage workspace. Keep the existing dark theme, project navigation and execution behavior. A person should find a stage's state, evidence, logs, model calls and pinned configuration without opening several nested sections.

## Approaches

- Stage explorer: selected because stage context stays visible while switching evidence views.
- Run dashboard tabs: simpler top-level separation, but repeated switching loses stage context.
- Chronological timeline: useful for run-wide activity; less effective for parallel stages/retries. Retain this as a separate run activity view.

## Design

- Top-level views: Stage explorer, Overview, Run activity, All model calls.
- Stage explorer: persistent stage selector at left, horizontally scrollable on narrow screens. Display exact recorded attempt states; do not interpret display order as dependency edges.
- Select one stage, defaulting to the first declared stage. Preserve explicit stage/execution URL parameters and browser navigation. Unknown or mismatched stage/attempt selections show an error without exposing unrelated evidence.
- Stage detail: Result, Logs, Model calls, Configuration. Default to Result; retain the chosen tab across stage selection and reset the attempt to the selected stage's latest generation. Attempt selector enables older retries.
- Result: canonical attempt state, captured start/finish/duration where available, worker/job links, and readable retained activity evidence. Structured stage output is not exposed by the current run API; explicitly state this and distinguish model responses from action results.
- Logs: searchable retained logs for this stage and attempt, directly visible rather than nested under another expander. Individual raw event details remain optional.
- Model calls: existing capture-aware viewer, with stage/attempt accounting and model filters nearby. Preserve separate estimated/reported accounting and missing-capture notices.
- Configuration: pinned stage configuration plus raw JSON as one optional disclosure. Keep run identifiers and revision navigation in Overview.
- Overview: canonical run status, dispatch, created timestamp, follow-ups, identifiers and revision link. Cancellation remains available from the header and retains current receipt/polling behavior.
- Run activity and All model calls: run-wide scope, regardless of selected stage/attempt. Keep worker/provider/model filters scoped to evidence rather than global unrelated controls.
- All dates use the existing locale/timezone formatter. Missing retained data remains unknown, not zero or successful.
- Accessible labelled navigation/buttons, visible selection/focus and readable empty/loading/error states. No new libraries or backend contracts.

## Scope

Frontend only. Do not run workflows, change credentials/configuration, restart workers or migrate the database. Rebuild local frontend static assets for verification and update affected guide screenshots. Keep existing public run/stage links usable, including hash links to activity/models.
