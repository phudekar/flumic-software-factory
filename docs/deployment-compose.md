# Local deployment stacks and lifecycle commands

Flumic keeps the application, optional LLM gateway, monitoring and host metrics in
separate Compose projects. Start only what you need. Shared worker pools are
managed through the Control Pane; they are not another static Compose fleet.

## What belongs to each stack

| Stack | Manifest | Contents | Configuration |
| --- | --- | --- | --- |
| Control Pane | `compose.control-plane.yaml` | API and bundled UI, lifecycle reconciler, dynamic runtime, one-time migration job | `.env.control-plane`, optionally `.env`, exported shell values |
| Default local database | `compose.control-db.yaml`, merged into the control stack | PostgreSQL; retains `factory-control_control-db` or your existing `CONTROL_DB_DATA_PATH` | `CONTROL_DB_PASSWORD` in `.env.control-plane` |
| Optional gateway | `compose.litellm.yaml` | LiteLLM and its own PostgreSQL | `.env.litellm`; models and provider credentials in LiteLLM's UI/database |
| Monitoring | `observability/pours/deployment/compose.yaml` | SigNoz, its PostgreSQL, ClickHouse/Keeper, ingestion, storage migration jobs, optional dashboard provisioning | `.env.observability`, optionally `.env` |
| Host metrics | `compose.resource-metrics.yaml` | Resource collector with Docker socket and host data access | `observability/resource-metrics.yaml` |

Shared workers are provisioned through Control Pane worker pools. The retired
static Compose fleet and telemetry overlays have been removed.

API, reconciler and dynamic runtime share database contracts and release versions,
so they stay in the core project. SigNoz stays with its storage dependencies.
Hatchet keeps its existing independent installation/profile; these commands do
not install another engine.

## First installation and upgrades

Prepare Docker, Hatchet and private configuration as described in the
[user walkthrough](user-walkthrough.md). For a new install, build worker images
explicitly before creating shared workers:

```sh
just images-build
just control-up api reconciler dynamic-runtime
```

`images-build` produces `hatchet-test-worker:latest` and
`hatchet-test-developer-worker:latest`, even when your checkout has another name.
It does not start containers or require model/engine credentials. Existing worker
containers keep their code until you deliberately recreate/restart them through
the worker lifecycle controls. Rebuilding an image does not update a container.

Control startup initializes `.env.control-plane` once with private permissions,
ensures `worker-network` exists and starts the selected control services. It does
**not** build worker images. `--build` still builds the Control Pane image.

For upgrades, back up the database and `.env.control-plane` encryption key first.
Keep the deployment canonical host root stable: saved Docker target pins include
`FACTORY_HOST_ROOT`. Running the launcher from a different worktree changes the
root it exports and can make existing worker target pins unavailable.
The one-time `migrate` service runs `alembic upgrade head` using the application
image. Local PostgreSQL must be healthy before migrations run; API, reconciler and
dynamic runtime wait for successful migration completion. API startup itself runs
only Uvicorn. A migration failure blocks application startup rather than starting
against an incompatible schema.

Inspect failure output with `just control-logs migrate`. After fixing the cause,
rerun `just control-up api reconciler dynamic-runtime`. Do not bypass dependencies
with `--no-deps` when applying an upgrade. A Compose restart does not run migrations
or adopt changed environment values; use the startup command for upgrades.

## Local or external Control PostgreSQL

The default is **local**. `scripts/control-compose.sh` combines the core manifest
with the local database overlay. The Compose project remains `factory-control`;
the existing volume name and optional `CONTROL_DB_DATA_PATH` are preserved.

For an external database, put a SQLAlchemy/psycopg URL in your private
`.env.control-plane` file, for example:

```dotenv
CONTROL_DATABASE_URL='postgresql+psycopg://factory_control:<url-encoded-password>@db.example.internal:5432/factory_control?sslmode=require'
```

Then select external mode in your **shell**, for all lifecycle commands:

```sh
export CONTROL_DATABASE_MODE=external
just control-up api reconciler dynamic-runtime
just control-logs api
# When intentionally stopping the control services:
just control-down
```

`CONTROL_DATABASE_MODE` is a shell selector, not a setting read from an env file.
External mode omits bundled PostgreSQL and validates the resolved URL before
running Compose operations. The hostname must be reachable from the containers;
`localhost` points at the container itself. Local mode uses the local database
URL even if an external URL is saved, preventing an accidental connection switch.

Changing modes does not copy business data. Plan a database restore/cutover,
preserve the original encryption key, and stop the old control deployment using
its old mode before switching. No command automatically removes the old volume.

Prefer the lifecycle wrapper over invoking the core YAML alone: the wrapper
selects the overlay, env files and host path consistently. The root `.env` is
optional; `.env.control-plane` overrides it, and exported variables have Compose's
normal precedence. Environment files are not evaluated as shell scripts.

## Optional local LiteLLM gateway

```sh
just gateway-up
just gateway-logs
```

First startup creates an ignored `.env.litellm` with mode 0600 containing a random
Postgres password, admin master key and separate encryption salt. Existing files
are preserved. Neither logs nor startup output print credentials. Open
[LiteLLM Admin UI](http://localhost:4000/ui); use `admin` and the
`LITELLM_MASTER_KEY` from your private file.

Under **Models + Endpoints**, add your provider/model and credentials. Assign a
stable alias such as `engineering-small`. Use restricted **virtual keys** for
applications, with permitted models and budgets; keep the master key for gateway
administration. Provider credentials, models, keys and spend records persist in
the gateway's own database.

| Caller | Gateway address |
| --- | --- |
| Host application | `http://localhost:4000/v1` |
| Container attached to `worker-network` | `http://litellm:4000/v1` |

The UI/API port is bound to loopback. Gateway PostgreSQL is not published and only
joins the gateway's private network. LiteLLM also joins `worker-network` so future
Flumic adapters can reach it. Containers on that network still need authorization.

The default image is pinned to `ghcr.io/berriai/litellm:v1.106.6`; set `LITELLM_IMAGE`
in `.env.litellm` to a reviewed release or digest when deliberately upgrading.
Preserve `LITELLM_SALT_KEY` together with the gateway database: changing it makes
stored provider credentials unreadable. Back up this database separately from
Control PostgreSQL. See [official deployment guidance](https://docs.litellm.ai/docs/proxy/deploy)
and [model management](https://docs.litellm.ai/docs/proxy/model_management).

**This starts the gateway only.** Flumic project routing, scoped gateway credentials,
model alias resolution, CLI integration and accounting remain work in
[the integration plan](roadmap/2026-10-30-litellm-integration-plan.md). No startup
command changes model routes or sends a test inference. LiteLLM routes requests to
providers; starting it does not install a local model server.

## Monitoring and dashboard provisioning

```sh
just observability-up
# Reapply the checked-in dashboard deliberately:
just dashboards-provision
```

Default monitoring startup excludes the profiles-gated `dashboard-bootstrap`
service. On first setup it separately attempts provisioning; a successful attempt
writes `data/observability-bootstrap/.provisioned`. Later starts preserve local
dashboard edits. A failed attempt warns without failing the already-started
monitoring stack and is retried on the next startup. After correcting credentials,
`just dashboards-provision` retries explicitly and propagates failure to its caller.
It reconciles the existing dashboard rather than duplicating it.

If monitoring storage was reset/restored, provision explicitly again; the local
marker does not inspect the database. Retain bootstrap state and credentials
with your monitoring backups. See [observability](observability.md) for account
bootstrap and dashboard semantics.

Host resource collection remains separate. For monitoring without it:

```sh
FLUMIC_RESOURCE_METRICS=0 just observability-up
# Enable the collector later:
just metrics-up
```

This flag controls whether startup starts the collector; it does not stop one
already running. Use `just metrics-down` to stop it. Resource metrics need the
SigNoz network, so start monitoring before starting this collector. Stopping
SigNoz does not stop this independently owned collector; stop metrics first when
shutting down both.

## Lifecycle shortcuts

| Stack | Start | Follow logs | Stop and remove containers |
| --- | --- | --- | --- |
| Control | `just control-up` | `just control-logs` | `just control-down` |
| Gateway | `just gateway-up` | `just gateway-logs` | `just gateway-down` |
| Monitoring | `just observability-up` | `just observability-logs` | `just observability-down` |
| Resource collector | `just metrics-up` | `just metrics-logs` | `just metrics-down` |

Down commands retain database volumes by default. Control down does not stop
managed shared workers or delete repositories. There is no combined destructive
shutdown or automatic pruning. Shared networks must remain available while their
consumers run. For partial control shutdown, use
`./scripts/control-compose.sh stop api` rather than `down api`.

Without `just`, run the corresponding scripts; logs/down use
`scripts/control-compose.sh`, `scripts/gateway-compose.sh` and
`scripts/observability-compose.sh` followed by the Compose command. The resource
collector uses `scripts/compose-command.sh -f compose.resource-metrics.yaml`.
Use `just --dry-run gateway-up` to inspect a shortcut without starting services.
