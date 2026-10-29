# Flumic AI walkthrough: from a new project to monitoring

This walkthrough is for a technical developer using the **project workspace** in
the Control Pane. Follow the numbered steps in order. The current application uses
a trusted administrator account; named-user roles and LLM gateway integration are
separate roadmap work, not prerequisites here.

You will reach two milestones:

| Milestone | What you prove | Effects |
| --- | --- | --- |
| A: first workflow | Project setup, shared capacity, stage dependencies and run inspection | Two deterministic echo stages; no model calls or source-host writes |
| B: first AI change | Issue → planning → implementation → review, with feedback/verification follow-ups | Real repository writes and billable provider calls after explicit launch |

Then you will inspect stage logs, model history, usage and optional SigNoz traces.
This is a guide to perform yourself, not a report that these live scenarios have
been executed. Screenshots show an existing local Default project as examples;
your new project initially has no runs or accounting records.

## Before you start

Prepare an initialized **disposable GitHub repository** with a default branch and
source/tests you understand. Use your own `OWNER/REPO`; do not copy the repository
name visible in screenshots. GitLab.com is also supported, but follow the
[GitLab-specific setup](gitlab.md) for credentials, numeric identity and CI policy.
This walkthrough uses GitHub to keep one consistent path.

You need Git, Python 3, Docker, the `docker-compose` command used by the launchers,
a reachable Hatchet instance and its `development` CLI profile. For milestone B,
you also need a repository-scoped GitHub token and provider credentials for your
selected planner/reviewer/coding backend. Use the exact prerequisites in the
[README quick start](../README.md#1-prepare-your-environment).

Install `just` (for example, `brew install just` on macOS). Run `just` at the
repository root to list shared shortcuts. Their underlying scripts remain
available for environments without `just`; see the [command table](../README.md#command-shortcuts).

From the repository root, prepare your private environment and start the control
services using the quick-start instructions, then:

```sh
just control-up api reconciler dynamic-runtime
```

**Checkpoint:** `http://localhost:3400` opens. API, PostgreSQL, reconciler and the
dynamic runtime must be available; a functioning UI alone does not establish that
Hatchet handlers or shared workers are registered. This launcher builds images
but does not restart existing managed workers. Keep all control services on the
same runtime namespace and deployment configuration.

**If blocked:** inspect control-service startup logs and Hatchet connectivity
before creating more workers. Do not start a duplicate legacy worker fleet to
try to make a project run progress.

## 1. Sign in and open Projects

1. Open `http://localhost:3400`.
2. Sign in with `CONTROL_ADMIN_PASSWORD` from the private `.env.control-plane`.
3. Open **Projects** in the workspace sidebar.

The password and encryption key are private deployment credentials. Do not put
them into workflow YAML, issue text, screenshots or source control.

![Projects list and Add project action](images/control-plane-projects-guided.png)

**Checkpoint:** you see existing projects and **Add project**, not an always-open
creation form. If the UI differs, confirm that the frontend and backend were
built from compatible current source and refresh the page.

## 2. Create your project

1. Click **Add project**.
2. Enter **Project name**: `Walkthrough`.
3. Expand **Advanced: project key** if needed. Use key `walkthrough`, or a unique
   key if it already exists. The key is generated from the name.
4. Click **Create project**.
5. Confirm **Walkthrough** is selected in the Project selector.

**Checkpoint:** the sidebar contains this project's Overview, Workflows, Workflow
runs, Model history, Usage & cost and setup pages. **All projects** returns to
workspace infrastructure/legacy navigation. The URL contains the project UUID,
not the human-readable key. Copy that UUID for the API steps below.

![Project workspace and project-scoped overview](images/control-plane-project-overview-guided.png)

The screenshot's populated Default project is illustrative; your overview should
start empty. If creation reports a duplicate key, choose a different key rather
than assuming an existing project is yours to change. Cancel discards the draft.

## 3. Save a source credential, or defer it for the echo test

**Milestone A can skip this step.** Echo does not contact GitHub. Milestone B must
complete it before saving a connection that will be used for repository actions.

1. In the selected project, open **Credentials → Add credential**.
2. Enter **Credential name**: `walkthrough-github`.
3. Set **Purpose** to `github` and **Credential repository** to **Whole project**.
   This lets you reference the credential while creating the connection next.
   It still belongs only to this project; use a provider token restricted to the
   disposable repository.
4. Paste the GitHub token into **Secret value**.
5. Click **Create credential**.

For the GitHub workflow, the token needs Contents, Issues and Pull requests
read/write, plus Checks and Commit statuses read access. Account/repository policy
may impose additional approval requirements; grant only the required repository.

![Credential names and scope without secret values](images/control-plane-credentials-guided.png)

**Checkpoint:** a credential card appears; the secret value is not shown again.
Under **Advanced details**, its credential reference ID is available. Saving a
credential does not prove provider access. Cancel clears an unsaved secret.

## 4. Connect the repository

1. Open **Repositories → Add repository**.
2. Set **Source provider** to **GitHub.com**.
3. Enter **Repository alias**: `backend`.
4. Enter your actual **GitHub owner** and **Repository name**, separately.
5. Select `walkthrough-github` as the credential. For milestone A only, **No
   credential** is permitted; add one and edit/save this connection before B.
6. Leave **Advanced: behavior overrides** unchanged for now.
7. Click **Save repository**.

![Repository alias, provider and assigned credential](images/control-plane-repositories-guided.png)

**Checkpoint:** the card shows your actual `OWNER/REPO` and alias `backend`.
Expand **Advanced details** and copy the **Connection ID** for later. This is not
its revision ID. Workflow definitions use `backend`, while handover/follow-up
bindings use the connection UUID. Credential assignment does not verify access.

**If blocked:** check provider, owner/name and the credential's purpose/scope.
For GitLab, saving alone is insufficient: validate its saved numeric identity
using the provider-specific instructions. Do not rename the alias accidentally
while editing a credential or behavior override.

## 5. Review behavior settings before adding capacity

1. Open the project's **Configuration** page.
2. Review the existing configuration rather than replacing it with an empty JSON
   document. It includes worker behavior, tasks, labels and model settings.
3. For milestone A, leave it unchanged. Echo does not require provider models.
4. Before B, check the `simple` and `developer` profiles' models, coding backend,
   prompts, timeouts and output limits against your accounts and repository.

**Checkpoint:** know whether the developer profile uses direct API, Codex or
Claude. API/Codex require the appropriate OpenAI credential; Claude requires an
Anthropic credential. Planning/review use their configured adapters independently.
Do not paste a key into configuration JSON.

New runs resolve and pin saved settings. Saving does not hot-patch an existing
run or install code into a worker. Use the [configuration ownership map](configuration-control-plane.md)
and [prompt/adoption guide](prompt-quality.md) when making deliberate changes.

## 6. Review shared pool settings — UI

Open **Walkthrough → Shared worker pools**. A pool declares the role, action
versions and concurrency supported by its workers. A worker is a running process;
a project authorization grants access to a pool. These are three separate actions.

1. Review any existing pools before adding a duplicate. Pool names are unique
   across the workspace, and pools can serve several projects.
2. In **Configuration**, check the saved role profile's Hatchet transport setting.
   The Add pool form prefills it when available. Use the setting matching your
   deployment; do not disable TLS to resolve a connection failure.
3. Plan to use `simple` for echo/planning/review and `developer` for code changes.
   Each worker image must support the action/executor versions advertised by its
   pool. Selecting a capability does not install it.

**Checkpoint:** creating a pool will neither start workers nor authorize this
project. No Python or API session is needed for pool setup.

## 7. Create the simple worker pool — UI

1. In **Shared worker pools**, click **Add pool**.
2. Set **Pool name** to `walkthrough-simple` (or a unique name).
3. Select **Worker role → Simple — planning and review**.
4. Leave the role's built-in **Action versions** selected. Confirm `core.echo@1`,
   `issue.load@1`, `issue.plan@1`, `issue.triage@1`, `change.review@1`,
   `workflow.record@1` and `pull.verify@1` are included.
5. Leave **Normal slots per worker** at `2` and **Durable slots per worker** at
   `16`. These are per-worker concurrency limits, not worker counts.
6. Confirm **Hatchet transport** matches your saved deployment settings. Under
   **Advanced: executor versions**, keep `builtin@1` for this walkthrough.
7. Click **Create pool**. **Cancel** discards the form without creating anything.
8. On its new card, expand **Advanced pool details** and copy the **Pool ID** for
   the workflow YAML in step 10. Record the ID, rather than the pool name.

**Checkpoint:** the new pool appears with the expected role, actions, transport
and slots. It is not yet project-authorized and has no workers. If reusing an
existing pool instead, check all of these settings before authorizing it; a
matching name alone does not prove compatibility. A duplicate-name error leaves
the form open so you can choose a unique name. See the
[simple pool API example](examples/walkthrough/simple-pool.json) for the equivalent
request shape; it is reference material, not an additional setup step.

![Guided Add pool form with role-filtered action choices](images/control-plane-pool-create-guided.png)

![Pool concurrency, transport and explicit Create pool controls](images/control-plane-pool-settings-guided.png)

These show an unsubmitted example form in the existing Default project. They do
not indicate that a walkthrough pool was created or a worker started.

## 8. Authorize your project to use the pool — UI

1. In the Control Pane, select **Walkthrough → Shared worker pools**.
2. Find `walkthrough-simple` (refresh if necessary).
3. Click **Authorize project**, unless it already says **Project authorized**.
4. Check required action `core.echo@1` and executor `builtin@1`.

**Checkpoint:** the project is authorized. **No workers in this pool** is expected
until the next step. Eligible registered slots are a capability view, not proof
of free runtime capacity.

## 9. Create and start a shared worker — workspace UI

1. Click **All projects** to leave the project sidebar.
2. Open global **Workers → Add shared worker**.
3. Enter **Worker name**: `walkthrough-simple-1` (or a unique name).
4. Choose **Worker pool**: `walkthrough-simple`.
5. Choose the operator-approved Docker **Deployment target**, usually
   `local-docker`. Kubernetes requires its separate qualification guide.
6. Click **Create shared worker**. Wait for creation to finish.
7. On the new worker row, choose **Start worker**. Wait for its lifecycle
   operation and startup to finish.
8. Return to **Projects → Walkthrough → Shared worker pools**.

Use **Add shared worker**, not **Create worker**: the latter creates a legacy
profile-based worker and will not supply this project's action assignments.

**Checkpoint:** the shared worker has registered the required action/executor
versions, reports a recent heartbeat, and its runtime is running. A container can
be running without Hatchet registration; a stale registration does not prove a
live process. If the target or registration is unavailable, inspect startup logs,
Hatchet transport and image compatibility. Do not proceed on a missing-capability
warning. Missing target APIs can indicate mismatched frontend/backend versions.

## 10. Publish the deterministic two-stage workflow — UI

1. Return to **Walkthrough → Workflows → New workflow**.
2. Open [the echo workflow example](examples/walkthrough/echo-workflow.yaml).
3. Copy its complete YAML into **Workflow source** in the advanced editor.
4. Replace **both** `REPLACE_WITH_SIMPLE_POOL_ID` values with the ID copied in
   step 7. Keep repository alias `backend` and workflow name `onboarding-check`.
5. Click **Validate definition**. Correct any reported error before publishing.
6. Click **Publish revision**.
7. Open **View or run this saved revision**; inspect its stage summary.

![Readable workflow cards with View, Edit and Run actions](images/control-plane-workflows-guided.png)

**Checkpoint:** validation reports two stages. `receive` has no dependencies;
`confirm` depends on `receive` and reads its `output.message`. `after` controls
ordering; `$ref` supplies data. Publishing creates an immutable saved revision;
it does not run the workflow. If `onboarding-check` already exists, edit that
workflow or choose a unique name instead of creating a duplicate.

![Stage summary and direct prerequisites](images/control-plane-workflow-stages-guided.png)

The screenshot shows a different installed six-stage recipe; your echo workflow
has exactly the two stages above. Inspect its aliases, pool IDs and dependencies,
not the screenshot's revision UUIDs.

## 11. Launch the saved echo revision — UI

1. From the `onboarding-check` card, click **Run**, or use its saved revision page.
2. Under **Run saved revision**, select `backend` in **Default repository**.
3. Set **Run input JSON** to the complete object below:

```json
{"message":"Hello from Flumic AI"}
```

4. Keep **Follow-up bindings JSON** as `{}` for this echo-only workflow.
5. Click **Run saved revision** once. The UI opens the new project run.

**Checkpoint:** you have a project run ID and a queued/dispatch receipt. This is
acceptance, not completion. Repeated submission from a newly opened form can
create another invocation; inspect the existing receipt before retrying after a
network error. Echo needs no source credential, legacy ownership handover or
model call. Do not infer that this qualifies repository mutations in B.

## 12. Verify milestone A and inspect an attempt — UI

1. Open **Workflow runs** and select this run if you navigated away.
2. Wait for canonical run/stage progress; refresh the project's data if needed.
3. Inspect `receive`, then `confirm`, under **Pinned dependency graph**.
4. Open **Inspect attempt 1** for a stage. Inspect its worker/assignment,
   **Project AI accounting**, **Bounded execution logs** and **Project model calls**.

**Checkpoint:** both stages and the run become `completed`. The data binding was
validated and the second stage consumed the first stage's message. The run view
primarily exposes state and pinned metadata; do not expect a full raw builtin
output viewer on every stage. Echo can have no captured model calls/logs and
requires no provider spending. Missing telemetry by itself is never proof of
success: canonical terminal state is the checkpoint.

![A populated installation's stage/attempt investigation view](images/control-plane-stage-attempts.png)

If a run remains queued, check `dynamic-runtime`, pool authorization, compatible
shared workers and dispatch error. If a stage fails, use its attempt and pinned
configuration to diagnose it; do not delete claims or force it to completed.

## 13. Prepare milestone B: provider credentials and developer capacity

Continue only when you intend to make real model requests and repository writes.
Use your disposable repository. Keep existing production/legacy work separate.

1. Complete steps 3–4 if you deferred the GitHub credential: **Credentials → Add
   credential**, then **Repositories → Edit connection**, select it and save.
2. Add an `openai` purpose credential named `walkthrough-openai` through the UI.
   Planning/review and API/Codex coding use their configured provider grants.
3. If the developer profile uses Claude, add an `anthropic` purpose credential as
   well. Copy credential reference IDs under Advanced; never use the raw keys in
   the next API request.
4. Check configured model IDs, backend availability, prompts and the account's
   limits in **Configuration**. Use account-supported models; identifiers in
   checked-in examples are not guarantees of your provider access.
5. In **Shared worker pools → Add pool**, name the pool
   `walkthrough-developer` and choose **Developer — code changes**. Keep its
   built-in action selections, `2` normal/`16` durable slots and `builtin@1`
   executor; confirm the prefilled **Hatchet transport** matches your deployment.
   Click **Create pool**, then copy its **Pool ID** from **Advanced pool details**.
   Reuse an existing compatible pool only after checking its capabilities.

6. Verify role `developer`, executor `builtin@1`, and actions
   `change.implement@1`/`pull.feedback@1`. Resolve any reused-pool mismatch.
7. In **Walkthrough → Shared worker pools**, authorize `walkthrough-developer`.
8. In workspace **Workers → Add shared worker**, create
   `walkthrough-developer-1` in that pool on the approved target, then start it.
9. Confirm registration/capabilities and runtime state as in step 9.

**Checkpoint:** both roles have authorized, compatible shared capacity. Merely
creating a developer pool does not install coding tools, credentials or models.
The saved project profile determines which installed coding backend is used.

## 14. Install issue, feedback and verification templates — API

Builtin-template installation still requires the API; pool creation above uses
the UI. Open the authenticated session below only when starting milestone B.

Open a terminal at the cloned repository root and run `python3`. Paste this
Python block into the interactive interpreter (not your shell). Finish the
function definition with a blank line if your interpreter waits for one:

```python
import getpass
import http.cookiejar
import json
import uuid
import urllib.request
from pathlib import Path

control = "http://localhost:3400"
csrf = ""
jar = http.cookiejar.CookieJar()
client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def api(method, path, body=None):
    headers = {
        "Content-Type": "application/json",
        "Origin": control,
        "X-CSRF-Token": csrf,
        "Idempotency-Key": str(uuid.uuid4()),
    }
    request = urllib.request.Request(
        control + "/api/v1" + path,
        data=None if body is None else json.dumps(body).encode(),
        headers=headers,
        method=method,
    )
    with client.open(request, timeout=60) as response:
        return json.load(response)

```

Run this login line **on its own**. Enter the password when prompted and wait for
the Python prompt to return before pasting the next block:

```python
csrf = api("POST", "/auth/login", {"password": getpass.getpass("Admin password: ")})["csrf"]
```

Then set the identifiers:

```python
PROJECT = "PASTE_PROJECT_UUID_FROM_STEP_2"
REPOSITORY = "PASTE_CONNECTION_UUID_FROM_STEP_4"
simple_pool = {"id": "PASTE_SIMPLE_POOL_ID_FROM_STEP_7"}
developer_pool = {"id": "PASTE_DEVELOPER_POOL_ID_FROM_STEP_13"}
prefix = "/projects/" + PROJECT
print(api("GET", prefix)["name"])
```

**Checkpoint:** the final line prints `Walkthrough`. Stop if it names another
project. All later Python blocks use **this same interpreter session** so its
cookies, CSRF value and IDs remain available. A 401 requires a fresh login; a 403
requires checking the configured public origin/session, not disabling protection.
No password is written to a file or echoed by the prompt.

### Identifier checklist

| Name used below | Where it comes from | Do not substitute |
| --- | --- | --- |
| `PROJECT` | UUID in selected project URL | Project key/name |
| `REPOSITORY` | Repository card → Advanced details → Connection ID | Alias `backend` or repository revision |
| Seed request `repository` | Alias `backend` entered in step 4 | Connection UUID |
| `simple_pool["id"]` / `developer_pool["id"]` | Pool card → Advanced pool details | Pool name |
| `OPENAI_SECRET` / `ANTHROPIC_SECRET` | Credentials → Advanced details | Raw key or credential name |
| `revisions["issue"]`, etc. | Template-installation response in step 14 | Definition family ID |
| Run ID | Run page URL / run acceptance receipt | Hatchet engine ID |


In the same Python session, set `OPENAI_SECRET` to your saved credential reference
from step 13. Add an Anthropic reference only when the selected backend needs it:

```python
OPENAI_SECRET = "PASTE_OPENAI_CREDENTIAL_REFERENCE"
model_grants = {"openai": OPENAI_SECRET}
# For Claude coding, also set the saved reference:
# model_grants["anthropic"] = "PASTE_ANTHROPIC_CREDENTIAL_REFERENCE"
revisions = api("POST", prefix + "/workflow-definitions/seed-builtins", {
    "repository": "backend",
    "simple_pool": simple_pool["id"],
    "developer_pool": developer_pool["id"],
    "secrets": model_grants,
})
print(revisions)
```

**Checkpoint:** the result has `issue`, `feedback` and `verify` revision IDs.
Refresh **Workflows** and inspect each recipe with **View**. Template installation
is create-only and does not overwrite previous operator edits. If recipes already
exist with different grants/pools, inspect their saved source instead of assuming
this request updated them. Publish deliberate revisions and use their actual IDs.
GitHub grants come from the repository connection, not `model_grants`.
The template endpoint takes the repository **alias** `backend`; keep using the
connection UUID `REPOSITORY` only for handover and follow-up binding keys.

## 15. Hand repository ownership to project execution — API

Every non-echo builtin needs dynamic repository mode, even on a newly connected
repository. Before calling this endpoint:

1. Pause legacy trigger sources for this repository.
2. Let active work settle and inspect pending/uncertain effects and commands.
3. Explicitly stop its relevant legacy workers through workspace **Workers**.
4. For a fresh disposable repository with no legacy owner, confirm that fact;
   do not stop unrelated workers or assume a new connection means no old owner.

Then, in the Python session:

```python
handover = api("POST", prefix + "/repositories/" + REPOSITORY + "/dynamic-mode", {})
print(handover)
```

**Checkpoint:** the request succeeds. A `409` means the conflict/claim guard
refused the handover. Investigate it using [rollout and recovery](project-runtime-operations.md#roll-out-from-a-current-checkout);
never bypass the guard or manually delete claims. This does not copy historical
SQLite business state. Legacy start/restart for a handed-over repository is fenced.

## 16. Create a small issue and launch the issue recipe — UI

1. In your disposable GitHub repository, create a small, verifiable issue grounded
   in its existing source/tests. State current behavior, desired behavior and
   concrete acceptance criteria. Avoid a broad refactor for the first run.
2. Copy its actual issue number. The `42` below is only an example: replace it.
3. In **Walkthrough → Workflows**, choose **Run** on **Issue to pull request**.
4. Confirm default repository `backend`.
5. Enter **Run input JSON**, using your issue number:

```json
{"issue_number":42}
```

6. Prepare the follow-up binding object in Python so you use connection and
   revision IDs consistently:

```python
bindings = {REPOSITORY: {
    "issue": revisions["issue"],
    "feedback": revisions["feedback"],
    "verify": revisions["verify"],
}}
print(json.dumps(bindings, indent=2))
```

7. Copy that output into **Follow-up bindings JSON**. Do not use `{}` for this
   builtin workflow, repository alias in place of the connection ID, or
   definition IDs in place of revision IDs.
8. Recheck repository, input, grants and capacity, then explicitly click
   **Run saved revision**. This can incur provider charges and write to GitHub.

**Checkpoint:** a project run is accepted, then stages progress. `load` → `plan`
→ `triage` → `implement` → `review` → `record` is the installed recipe; parent
planning may create sub-issues and follow-up runs. A completed control run is not
proof that all child work was reviewed and merged.

If clarification is needed, answer the issue questions and use the configured
ready label. For PRs, review actual diffs and CI, request changes as appropriate,
and approve/merge through the repository's normal rules. Feedback and merge
verification can involve durable waits and separate follow-up runs; follow the
**Follow-up workflows** links and receipts. Do not launch an identical second run
just because human review is still pending. This guide uses explicit manual
launch, not automatic webhook/trigger setup.

## 17. Monitor execution, model exchanges and spend — project UI

Start from the project's sidebar so you keep the correct scope:

| Screen / action | What to check | How to interpret it |
| --- | --- | --- |
| Overview | Connections, definitions, recent project runs, known estimated spend | Counts are bounded views; published definitions do not prove live capacity |
| Workflow runs → run | Canonical state, dispatch, pinned revision and stage attempts | Accepted/queued is not completed; Hatchet receipt IDs are not project run IDs |
| Inspect attempt | Assigned worker, execution generation, logs and model accounting | Logical graph stage `plan` may execute operation `plan_issue` |
| History filters on the run | Selected stage/execution/worker identity | Keep filters for this run; clear intentionally to broaden the view |
| Model history | Captured exchanges and metadata for this project | Content is policy-controlled and expires; unavailable content is not an empty model response |
| Usage & cost | Input/output tokens, known estimated and separately reported spend, gaps | Missing usage/price is unknown, not zero; estimates are not provider invoices |

A default project may contain attributed legacy accounting even if it has no
new dynamic runs. A newly created project starts with its own records. Do not add
gateway/CLI/provider observations together as independent charges for the same call.

Capture settings are `redacted`, `metadata_only` or `disabled`. They affect what
history is retained; disabled model capture also removes its usage history.
CLI captures do not contain every internal exchange. See [project history](project-observability.md)
for exact coverage, correlations and retention.

## 18. Add optional SigNoz monitoring — deployment step

Use the existing monitoring stack rather than starting another worker fleet:

```sh
just observability-up
```

1. Open `http://localhost:3301` and sign in using the private bootstrap/account
   instructions in the [observability guide](observability.md#start-and-open-the-dashboard).
2. Open **Dashboards → Software Factory — Workers and AI Usage**. The retained
   dashboard/service names still use `software-factory`.
3. Ensure the project action's effective `observer_factory` is
   `factory.adapters.otel:otel_observer`, with a worker-reachable OTLP/HTTP endpoint.
   Preserve the rest of the saved configuration and check for profile overrides;
   do not replace the complete JSON document just to change observation.
4. Follow [project telemetry configuration](project-observability.md) for shared
   worker infrastructure export (`FACTORY_SHARED_TELEMETRY_ENDPOINT`) and the
   separate action observer. Deployment transport edits need deliberate service/
   worker recreation; admitted actions retain their existing configuration pins.
5. Generate a **new explicitly authorized** run after telemetry is configured.
   Export does not retroactively create traces for earlier executions.
6. In SigNoz, choose the run's time range and filter service `software-factory`,
   environment and attributes such as `factory.project_id`, `factory.run_id`,
   `factory.stage_id` and `factory.execution_id` from the inspected run/attempt.

![SigNoz worker and stage monitoring example](images/signoz-workers-stages.png)

![SigNoz reported model usage and estimated cost example](images/signoz-model-usage-cost.png)

**Checkpoint:** new instrumented operations have correlated telemetry when capture,
transport and retention permit. Dashboard gaps do not prove a healthy idle worker.
Container CPU/memory and token/cost are different measurements. The resource
collector can include unrelated host containers; do not interpret its disk metric
as storage uniquely owned by each worker. See [monitoring setup](observability.md)
for collector access and [accounting coverage](project-observability.md).

Do not run `start-observed-workers.sh` alongside this shared fleet merely to get
charts: it launches real legacy workers and can duplicate processing.

## 19. Change configuration, recover safely and finish

- **Change behavior:** save a project/repository/workflow revision, then launch a
  new run. Active runs keep their pins. A code/image update requires an explicit
  compatible worker lifecycle action; a settings save does not install code.
- **Cancel:** open the run and choose its cancellation action. Intent can remain
  `cancelling` while an external effect is uncertain. Source cancellation does
  not recursively cancel already-launched follow-up runs.
- **Diagnose:** inspect attempt state, dispatch receipts, worker capability and
  recorded errors before retrying. Use [recovery procedures](project-runtime-operations.md)
  for uncertain actions; never force terminal state from absence of logs.
- **Stop this test capacity:** if no longer needed, use workspace Workers to stop
  only the specifically created walkthrough workers. Do not stop shared capacity
  that other projects use. Keep data/backups and credentials according to policy.
- **End the helper session:** in the same Python interpreter, call
  `api("POST", "/auth/logout", {})`, then `exit()`. Sign out of the browser when
  done. Session variables are not a substitute for a private secret manager.

### Troubleshooting checkpoints

| Symptom | Check before proceeding |
| --- | --- |
| Project/sidebar looks different | Matching current frontend/backend versions, selected project and browser refresh |
| No Add pool control | Update to the frontend version containing guided pool creation; do not mistake Authorize project for Create pool |
| No capacity after creating a worker | Correct **shared** worker, authorized pool, loaded action/executor versions, runtime and Hatchet registration |
| Echo validation fails | Both pool-ID replacements, declared alias `backend`, exact `$ref` and direct dependency |
| Echo run stays queued | Dynamic control runtime registered, dispatch receipt and available compatible worker |
| Builtin launch returns 409 | Dynamic-mode handover, current revision, legacy ownership or an uncertain action; inspect the specific error |
| Builtin launch lacks bindings/grants | Connection UUID → all three workflow revision IDs; source grant on connection; correct provider purpose |
| UI shows old settings | Pinned run revision vs newly saved settings; compatible adoption/startup is separate |
| No model exchange/cost | Echo has none; for AI work inspect capture policy, retention, delivery and price/usage gaps |
| No SigNoz trace | Observer selection, reachable endpoint, network/time filters and new operation after configuration |

You have completed the walkthrough when you can explain which project owns a
connection, which pool supplies an action, which saved revision a run used, why a
stage is waiting/failed/completed, and where its reported usage and unknowns appear.
