# GitLab.com support — issue #16

Status: design and implementation plan approved by the user; implementation underway. GitLab.com first is the
user's requirement. Reusing the shared repository layer is the user's selected
approach. Other choices below are proposed implementation decisions.

## Outcome and boundaries

A project can contain GitHub and GitLab.com repository connections and run the
same supported builtin workflow operations on shared workers. GitLab issues lead
to draft merge requests, review feedback can produce further commits, and merged
changes are checked before business completion. Immutable configuration, scoped
credentials, history/accounting, external-resource claims and recovery apply to
both hosts. Existing GitHub connections, graph revisions, static workflow IDs,
claims, receipts and state continue to work without migration-driven restarts.

GitLab Self-Managed/Dedicated, OAuth installation, automatic merging, fork-based
publication, paid-tier-only project management features and broader RBAC are not
part of this issue. Providers are explicitly composed installed adapters; a host
string cannot load Python code or choose an arbitrary API endpoint.

## Approaches considered

1. Extend the shared repository boundary with a GitLab adapter and small explicit
   provider capabilities. Selected by the user. More initial compatibility work,
   but shared workflow/recovery logic and one configuration model.
2. Separate GitLab action package. Fewer initial changes to GitHub composition,
   but duplicates business behavior and splits workflow compatibility. Rejected.

## Repository identity and configuration

- Support exactly `github` / `github.com` and `gitlab` / `gitlab.com`. Preserve
  existing defaults. Reject other provider/host combinations, schemes, ports,
  userinfo and arbitrary outbound hosts.
- GitLab namespace may contain nested groups; the final component is the project
  name. Validate path segments, percent encoding and traversal explicitly.
- GitLab connections resolve and pin the numeric GitLab project ID through a
  credential-authenticated read before they become executable. Store ID, path,
  provider and host in the immutable repository revision. A rename is explicit
  revision management; IDs prevent accidentally switching to a reused path.
- Canonical GitHub identities retain their current byte representation. GitLab
  claims use the stable host/project ID and distinguish issue IID from MR IID:
  GitLab issue #7 and MR !7 must not share a work-item identity. Repository-level
  exclusion still prevents legacy/dynamic concurrent mutations where required.
- UI/API permit selecting GitLab.com, namespace/project and a GitLab credential;
  show resolved identity and validation failures. Never require a GitHub grant
  for GitLab. Mixed-provider aliases remain project-owned and stage targeting is
  pinned at launch. Provider selection is not a workflow/stage behavior override.

## Credentials and execution context

Add a `gitlab` secret purpose, encrypted/versioned/repository-scoped like GitHub.
Resolve the correct source-host purpose from the pinned repository revision.
Model grants remain independent. Revocation, old-version replay/redaction and
cross-project denial keep the existing rules. No global GitLab environment token
is used by managed project actions. HTTP tokens remain in headers, never URLs,
logs, emitted context or return values. Reject redirects outside the allowed host;
GitLab archive/file download paths obey the same restriction and size limits.

Add explicit provider/host/project identity to authoritative action context and
repository construction. Compatibility defaults interpret old GitHub pins as
GitHub; never infer provider from a user-supplied output URL. Generic Docker jobs
receive only explicitly declared grants; source-host credentials are not implied
by executor type.

## Adapter and shared business behavior

Keep `FactoryService` responsible for business policy. Implement a bounded,
paginated GitLab REST adapter using the existing HTTP/dependency conventions.
Normalize wire responses into the application's required issue/change contracts;
GitLab IDs/IIDs, states and URLs remain explicit at the adapter boundary.
Provider-specific business capabilities must not be implemented as fake GitHub
endpoints or undocumented no-ops.

Required adapter coverage (REST for queries/metadata, dedicated Git transport for atomic branch publication):

- Issues, labels, notes, source inventory/files, revision resolution and bounded
  archives, including nested namespace encoding and pagination.
- Draft MRs, branch commits, changed-file/diff retrieval, human discussion feedback,
  merge/closed/draft state and terminal pipeline/commit-check verification.
- Deterministic issue/change/comment/commit markers to reconcile lost responses.
- Planning subtask links use ordinary related issues plus explicit factory parent
  markers, not paid-tier hierarchy assumptions. The UI/docs call these related
  plan tasks; they do not claim native GitHub sub-issue semantics.

Retain old public GitHub routes/legacy workflow names and input shapes. Introduce
provider-neutral internal names where useful without renaming historical data.
Existing action @1 definitions retain GitHub behavior for existing pins; new
GitLab pins use the explicit provider context. Bump an action version if a public
input/output contract actually changes; do not silently reinterpret old data.

## Publication, feedback and checks

Before publication, compare the generated base commit with the current expected
branch. Validate all proposed paths and the complete patch. GitLab's batch commit API has per-file guards but does not establish an atomic
whole-branch expected-head update. Proposed builder decision: publish through a
small dedicated Git HTTPS transport using an explicit expected-old-OID ref lease.
Create the commit locally with the pinned expected head as parent, verify that
parent/ancestry, then push with an exact `--force-with-lease=<ref>:<expected-oid>`
condition (empty expected OID for creation). Never use unconditional force or allow
an ancestry rewrite: the lease provides compare-and-swap, not permission to drop
human commits. This rejects any head change, including unrelated edits; recovery
must re-read/replan instead of silently rebasing generated output. Use an isolated
per-action workspace, fixed HTTPS gitlab.com remote, bounded operations and a
private askpass/token channel rather than putting tokens in remote URLs or argv.
Use deterministic content/base markers. Reconcile commit success followed by a lost response or MR creation
failure by inspecting the expected branch content/commit marker and existing MR;
marker presence alone is insufficient proof of the complete expected mutation.
Unknown/conflicting effects fail closed and preserve recovery evidence.

Human MR discussions/notes feed review loops with stable IDs and cursors; exclude
factory/system echoes. A changed source head invalidates stale feedback/output
assumptions. Closed-unmerged is distinct from merged. Verify checks against the
actual merged/result commit, with explicit handling of pending, failed, cancelled,
skipped, manual, absent and inaccessible pipelines. Proposed builder decision:
pin a GitLab check policy with required pipeline sources (default `push`) and
optional required external status names. For each required source, require the
latest pipeline for the exact resulting SHA and target branch, plus its linked
child/downstream pipelines, to succeed. Every required external context must have
a successful latest status for that SHA; any observed failing external status
also blocks completion. Require at least one successful non-optional job; skipped
pipelines, unresolved blocking manual jobs, missing required sources/statuses,
pagination truncation and inaccessible downstream evidence are not success.
Additional pipeline sources are only included when explicitly configured; a green
MR-source pipeline for a different SHA never substitutes for merged-commit checks.
Persist the selected check policy with the run settings; changing it cannot
retroactively clear an active run. Unknown or absent evidence must not become
success. Shared worker retry/lease rules cannot promise exactly
once external effects.

## Events, polling and trigger ownership

Normalize GitLab issue, MR and note events into source-host-neutral events. Bind
remote numeric project identity to the configured repository; never trust payload
paths to select another project's context. Duplicate/reordered events prompt
fresh authoritative reads rather than replaying stale payload business state.

Project execution needs an explicit saved trigger binding: project/repository,
event kind, target workflow definition, enabled state and readiness policy. It
resolves the current saved workflow revision when accepting an event and stores
that immutable launch pin with a durable deduplication receipt. Follow-up workflow
bindings remain the existing pinned project runtime mechanism. Polling and event
acceptance share the same eligibility/deduplication path. Merely receiving an MR
notification cannot select an arbitrary workflow or run another project's graph.

Add a project/repository-qualified GitLab webhook endpoint with a separately
versioned webhook token, constant-time authentication, bounded payload validation
and redacted errors. The webhook token is never an action grant. Verify claimed
numeric project identity against the binding. Keep authenticated delivery IDs or
canonical content digests for duplicate detection. Do not add an unauthenticated
public forwarder to Hatchet. Polling provides correctness/recovery when ingress
is unavailable; operators explicitly enable bindings and configure their public
webhook endpoint. Preserve existing GitHub Hatchet webhook integration.

For legacy GitLab workers, explicit provider settings select adapter/event
registration and independent names; existing GitHub settings and workflows keep
their defaults. No existing worker is restarted automatically. Dynamic takeover
checks compare provider-qualified external identities, including legacy owners.

## Validation and release gate

1. Shared source-host contract tests plus GitHub regression fixtures; realistic
   GitLab wire responses, pagination, subgroup paths, issue/MR IID collisions,
   state mappings, missing capabilities, unauthorized grants and bad redirects.
2. Deterministic GitLab HTTP fixture for the full service/action path with fake AI:
   issue → plan → draft MR → feedback commit → human-merge observation → checks.
   Inject lost mutation responses, stale edits, duplicate events, revoked grants,
   missing/failed pipelines and mismatched repository identities.
3. PostgreSQL project isolation/migration/claim/trigger receipt tests, existing
   mixed-runtime release tests, frontend project/configuration/history tests and
   docs build. Verify GitHub historical identities and old revisions unchanged.
4. Opt-in real GitLab.com contract against an explicitly designated disposable
   test project, using deterministic changes and fake AI. It must exercise actual
   issue/MR/commit/check semantics and clean only its own labeled test resources.
   Read-only tests cannot establish publication/recovery. No live writes or
   credential collection occur until the test project and mutation scope are
   explicitly authorized. Never ask for a secret pasted into chat.
5. Record evidence in issue #16 and the PR. Keep GitLab labeled planned until the
   end-to-end real-host gate is verified. Missing infrastructure may leave a PR
   reviewable, but cannot justify closing #16 as fully supported.

Document GitLab.com token scopes/permissions and rotation, namespace/repository
setup, trigger bindings/webhook authentication, workflow examples, related-task
semantics, MR/check recovery and limitations. Update current/planned marketing
only after verified behavior. Existing workers remain untouched throughout.

## Implementation sequencing and builder decisions

Sequence: compatibility contracts/identity → adapter/recovery → pinned credential
composition → project UI/API/trigger ingestion → end-to-end qualification/docs.
Use the repository's Python/React/PostgreSQL/Hatchet stack. The builder chooses
module/file splits, exact field names/migration IDs and HTTP transport details,
provided compatibility/security invariants above remain testable. One issue/PR
may contain several focused commits. Use test-first implementation, one fresh
read-only final review and exact-head CI before merge as in prior issues.

## Primary references

- https://docs.gitlab.com/api/merge_requests/
- https://docs.gitlab.com/api/commits/
- https://docs.gitlab.com/api/discussions/
- https://docs.gitlab.com/api/issue_links/
- https://docs.gitlab.com/user/project/integrations/webhook_events/

## Builder check and proposed calls

The fresh builder check identified branch-head atomicity and CI selection as the
two material gaps. The proposed decisions above require an exact-head descendant
commit publication transport, and pinned required sources/statuses with fail-closed
evidence. These are author proposals for approval, not user choices already made.
Costs: a controlled Git executable becomes a worker-image dependency; GitLab
repositories need an explicit qualifying pipeline, and strict evidence checks can
leave a run waiting for operator correction rather than marking it complete.

## Approval

Approved by the user (“sounds good”). No implementation code has been changed.

Additional references: https://git-scm.com/docs/git-push ; https://docs.gitlab.com/api/pipelines/
