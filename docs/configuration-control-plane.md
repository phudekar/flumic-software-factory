# Control-plane and runtime environment configuration

This reference covers the server, deployment environment, and worker credentials. For YAML worker settings, inheritance, image settings and task settings, see [worker configuration](configuration-workers.md); for workflow inputs and controls, see [workflow configuration](configuration-workflows.md). Values below come from `AppSettings.load`, the Compose manifests and the actual worker bootstrap path, rather than an assumed environment-to-setting convention.

## Configuration layers and when changes apply

`./scripts/start-control-plane.sh` changes to the repository root and runs Compose with `--env-file .env --env-file .env.control-plane`. The second file takes precedence over the first; exported shell values can override Compose interpolation. Neither file is automatically a complete container environment: only entries listed under a service's `environment` are delivered to it. Do not put runtime settings in `.env` expecting them to override YAML.

The server loads `AppSettings` at process startup. Apply server environment changes by recreating **both** `api` and `reconciler`; restarting an existing container preserves its original environment. The reconciler also creates managed worker containers from its environment. Changing its credentials does not update existing worker container environments: stop and reconfigure/recreate those runtimes to deliver new credentials. Merely starting an existing worker does refresh its linked saved profile through bootstrap, but does not refresh its environment.

Standalone workers use `Settings.load`, which reads the selected YAML file/profile. Managed workers with a nonempty `FACTORY_CONTROL_ENDPOINT` instead fetch their saved profile on each process start. `Settings.from_env` is a compatibility constructor, not the production worker configuration path. Its legacy `GITHUB_REPOSITORY`, `WORKER_TYPE`, `ORCHESTRATOR`, `HATCHET_WORKER_NAME`, `SCAN_CRON`, `MAX_PATCH_ITERATIONS`, `PATCH_PATH_POLICY`, `FACTORY_STATE_PATH`, `LOG_LEVEL`, `LOG_FORMAT`, `PLANNER_FACTORY`, `REVIEWER_FACTORY`, `CODING_AGENT_FACTORY`, `OBSERVER_FACTORY`, `STATE_FACTORY`, and `DEVELOPER_AGENT` mappings do not make those variables live overrides of YAML. Direct helper fallbacks for `OPENAI_MODEL`, `CODEX_MODEL` and `DEVELOPER_AGENT` likewise do not replace the explicit settings passed by normal worker adapters.

## Every AppSettings field

“Required” means direct `AppSettings.load()` raises if the environment variable is absent; an empty string is not a useful substitute. Constructor defaults and deployed Compose values are shown separately where they differ. All examples use placeholders.

| Field / environment input | Load default and deployed value | Effect and example |
| --- | --- | --- |
| `database_url` / `CONTROL_DATABASE_URL` | Required. Compose constructs `postgresql+psycopg://factory_control:${CONTROL_DB_PASSWORD}@postgres:5432/factory_control`. | SQLAlchemy connection for revisions, profiles, worker desired state, operations, sessions, events, conversations and audit history. Example direct process value: `postgresql+psycopg://factory_control:<url-encoded-password>@db:5432/factory_control`. A shell `CONTROL_DATABASE_URL` does **not** override the hard-coded Compose environment expression; use an override manifest for an external database. |
| `admin_password` / `CONTROL_ADMIN_PASSWORD` | Required; script generates a random URL-safe value on first use. | Sole administrator login password, hashed in memory when the API starts. Example: `<generated-admin-password>`. Updating the file requires API recreation; existing database-backed sessions are not automatically revoked by a password change. Keep credentials out of committed documentation and YAML profiles. |
| `encryption_key` / `CONTROL_ENCRYPTION_KEY` | Required; script generates URL-safe base64 for 32 random bytes. | Must be a valid Fernet key or API startup fails. Encrypts stored captured conversations and is also the HMAC secret used to derive per-worker bearer tokens. Example: `<generated-Fernet-key>`, not an arbitrary password. Preserve it with the database; see backup notes below. |
| `root` / `FACTORY_ROOT` | `.` resolved to an absolute path; Compose sets `/factory`. | Repository root used for bootstrap configuration/profile imports, worker image build sources, and derived control-plane data paths. Example direct process value: `/srv/software-factory`. A host path is not appropriate here when the process runs inside `/factory`. Compose fixes this value. |
| `snapshot_root` / no direct environment input | Always `root / "data/control-plane/snapshots"`. | Server-side immutable per-worker/per-revision `snapshot.yaml` files. Build status/log records are in sibling `data/control-plane/builds`. Example with Compose: `/factory/data/control-plane/snapshots`. Change `FACTORY_ROOT` or mounts, not a nonexistent `CONTROL_SNAPSHOT_ROOT`. |
| `host_root` / `FACTORY_HOST_ROOT` | `Path(root)` when unset; script exports the repository's host `$PWD`. | Docker bind mounts must use paths visible to the Docker daemon, while API file writes use `root`. Example: `/Users/example/workspace/hatchet-test`. A wrong value mounts missing/wrong snapshots, `/data` or pricing data into newly created workers. Compose requires this input. Unlike `root`, this path is not explicitly resolved by the loader. |
| `frontend` / `CONTROL_FRONTEND` | `control-plane/frontend/dist`, relative to process working directory; Docker image sets `/app/frontend`. | If the directory exists, serves frontend assets and SPA index. Example local override: `/srv/ui/dist`. Missing output leaves the API without the UI routes. Compose does not forward a host `CONTROL_FRONTEND`; image ENV supplies the deployed value. Frontend changes need a rebuilt server image. |
| `public_origin` / `CONTROL_PUBLIC_ORIGIN` | `http://localhost:3400`; Compose interpolates the same default. | Exact allowed `Origin` for unsafe HTTP methods when an Origin header is present, except ingestion. Example: `https://factory.example.com` (scheme, host, optional port; no trailing slash). Wrong origin produces 403 for browser mutations, including login. It does not change listen ports, enable CORS or configure TLS. |
| `worker_endpoint` / `CONTROL_WORKER_ENDPOINT` | `http://factory-control-api:8000`; Compose fixes this same value. | Base URL injected as `FACTORY_CONTROL_ENDPOINT` into **new** managed containers. Used for saved-profile bootstrap and event ingestion. Example alternative network endpoint: `http://control-api:8000`. It must be reachable from worker containers, not just from the browser; a host localhost URL refers to the worker itself. Override Compose explicitly to change it. |
| `compose_project_name` / no loader environment input | Dataclass default `hatchet-test`. | Discovers preexisting Compose containers whose project label matches and whose service is `worker` or `developer-worker`; also scopes access to their logs. Example programmatic override: `AppSettings(..., compose_project_name="my-factory")`. No `CONTROL_PROJECT` or `FACTORY_PROJECT` variable is read. Managed containers use separate factory labels. |
| `cookie_secure` / `CONTROL_COOKIE_SECURE` | `False`; loader enables only exact lowercase string `true`. Compose does not forward it. | Sets the login cookie's Secure flag. Example direct-process value: `true` for an HTTPS deployment. Setting it in the host env file alone has no effect with the checked-in Compose manifest: add it to API environment via an override. `True`, `1` and `yes` remain false. |

### `CONTROL_JOB_BRIDGE`

Configures optional `job_bridge` (default `None`): the fixed, pre-created dedicated Docker network used by internal one-time job stages declaring `network: bridge`. Example: `CONTROL_JOB_BRIDGE=flumic-action-network`. Reserved `host`, `bridge` and `none` names are rejected. Jobs default to no network; this is not a host/metadata egress firewall. The base Compose manifest forwards it to the API and reconciler. Recreate both after changing it: publication policy and broker execution must agree. A graph may use bridge mode only when this fixed deployment network is configured. See the [job contract](docker-action-jobs.md).

### `CONTROL_DATABASE_URL`

Configures `database_url`; required for direct server startup. Under Compose the URL is assembled from `CONTROL_DB_PASSWORD`, so use a service environment override for a different database.

### `CONTROL_ADMIN_PASSWORD`

Configures `admin_password`; the startup script generates the initial secret. Recreate the API after changing it; existing sessions remain database-backed.

### `CONTROL_ENCRYPTION_KEY`

Configures `encryption_key`; preserve the generated Fernet key with database backups. Both server processes must use the same key for encryption and managed-token derivation.

### `FACTORY_ROOT`

Configures `root`; Compose fixes `/factory`. This is the process-visible repository path used for imports, image build inputs and snapshot storage.

### `FACTORY_HOST_ROOT`

Configures `host_root`; the startup script exports the host repository directory. Docker must be able to bind mount paths beneath it.

### `CONTROL_FRONTEND`

Configures `frontend`; the server image supplies `/app/frontend`. Direct runs default to a relative frontend build directory.

### `CONTROL_PUBLIC_ORIGIN`

Configures `public_origin`; match the browser's exact origin to allow mutations. It does not change network exposure.

### `CONTROL_WORKER_ENDPOINT`

Configures `worker_endpoint`; Compose fixes the internal API address used by newly created workers. This must resolve inside the worker network.

### `CONTROL_COOKIE_SECURE`

Configures `cookie_secure`; only `true` enables it. Add an explicit container environment override for HTTPS deployments because the base Compose manifest does not forward it.

### `snapshot_root`

Derived from `root`, with no independent environment variable. Snapshot mounts and build records must persist on the shared host control-plane data directory.

### `compose_project_name`

Fixed loader default `hatchet-test`, with no environment mapping. This selects discovery of preexisting standalone Compose workers by project label; it does not rename the `factory-control` Compose deployment. The deprecated constructor alias, `project`, remains supported as `project="my-factory"`. Domain projects and repository connections do not change this infrastructure setting.

The browser always calls relative `/api/v1` with same-origin credentials; there is no `VITE_API_URL` setting in the frontend. Its development Vite proxy sends `/api` to `http://127.0.0.1:8000`. Production should serve the UI and API under the same origin. Login uses an HttpOnly, SameSite=Strict `factory_session` cookie with an eight-hour lifetime; authenticated mutations also require the session CSRF token. The cookie Secure flag and `CONTROL_PUBLIC_ORIGIN` solve different parts of HTTPS deployment and must be set consistently.

## Compose and startup script knobs

| Input or fixed deployment choice | Default/example | Operational effect |
| --- | --- | --- |
| `CONTROL_DB_PASSWORD` | Generated at first startup; example `<generated-db-password>`. Required by Compose. | Sets PostgreSQL `POSTGRES_PASSWORD` and the API/reconciler URL password. It is Compose interpolation, **not** an `AppSettings` field. PostgreSQL initializes credentials only for a fresh data directory; changing this variable alone on an existing volume does not change the database user's actual password. Coordinate a real DB password update with server recreation. |
| `CONTROL_DB_DATA_PATH` | `control-db`; example `/srv/factory-postgres`. | Selects the Postgres volume or host bind mount at `/var/lib/postgresql/data`. The default is a Compose-managed named volume (normally project-prefixed). Use an absolute existing host path for an intentional bind mount. Changing it selects a different database directory and may initialize an empty database; it does not migrate data. |
| `.env.control-plane` existence | Script creates it only if missing, mode `0600`. | Generates only admin password, DB password and encryption key. An existing partial file is not repaired or rotated. Back it up securely; rerunning the script preserves its values. `.env.example` documents GitHub/model credentials for `.env`, not these generated server secrets. |
| Script `"$@"` arguments | Example `./scripts/start-control-plane.sh --force-recreate`. | Passed to the control Compose `up --build -d` invocation, after initial worker image builds. Service selection and Compose options apply there. This script does not restart the existing standalone worker services. |
| Hatchet development profile | Fixed name `development`; used only if `hatchet` is on `PATH`. | Script evaluates `hatchet profile env --name development`; failure aborts. Without CLI, supply the token through the environment/files. Control Compose allows an empty token, so API startup can succeed while engine actions or managed-worker creation fail. |
| Host root and network | Script exports `$PWD`; creates `worker-network` if missing. | Connects managed workers, API and reconciler on a shared Docker network. Fixed API container name `factory-control-api` supplies the internal DNS name. Existing standalone `compose.yaml` uses the same worker network. |
| Exposed port | Fixed `127.0.0.1:3400:8000`. | Browser access is host-local at `http://localhost:3400`. Changing `CONTROL_PUBLIC_ORIGIN` does not publish a new port or expose it remotely; edit/override the port and configure a proxy for other access. The startup message is also fixed to localhost. |
| Server image and storage mounts | `software-factory-control:local`; repository read-only at `/factory`, `./data/control-plane` writable there; Docker socket mounted. | API startup runs Alembic migrations before Uvicorn; reconciler waits for API health. Both need shared snapshots/build progress. Docker socket access enables worker lifecycle/image operations on the daemon. File permissions and Docker daemon host paths must match this topology. |

Example override for HTTPS behind a same-origin proxy (proxy provisioning and TLS are separate):

```yaml
# compose.control-plane.https.yaml
services:
  api:
    environment:
      CONTROL_COOKIE_SECURE: "true"
  reconciler:
    environment:
      CONTROL_COOKIE_SECURE: "true"
```

Set `CONTROL_PUBLIC_ORIGIN=https://factory.example.com` in the deployment environment, then include this override with the base control-plane manifest when recreating services. The startup script only selects the checked-in manifest, so a custom manifest requires an explicit Compose command using the same env files and exported `FACTORY_HOST_ROOT`.

## Credentials and worker bootstrap variables

These are real runtime inputs, separate from YAML `Settings`. Credentials are not included in the managed image build context, Docker build/pull arguments, or profile snapshots. They are delivered as container environment variables; access to Docker/container inspection therefore remains sensitive.

| Variable | Default / example | Consumer and effect |
| --- | --- | --- |
| `HATCHET_CLIENT_TOKEN` | No usable credential default; `<hatchet-tenant-token>`. | Hatchet SDK authenticates to the configured engine. Standalone Compose requires a nonempty value. Control Compose accepts empty but managed runtime creation requires it, alongside GitHub. Server engine adapter uses it too. TLS strategy is set in YAML; engine endpoint discovery is delegated to the Hatchet SDK/token configuration. The control-plane container does not automatically forward arbitrary Hatchet endpoint environment variables. |
| `GITHUB_TOKEN` | No usable default; `<fine-grained-github-token>`. | GitHub API/repository access; required by standalone Compose and managed runtime creation. Must authorize the configured repository and requested operations. Changing repository YAML does not change token permissions. |
| `OPENAI_API_KEY` | Empty in Compose if absent; `<openai-api-key>`. | Default OpenAI credentials for built-in API planner/reviewer/coder and Codex paths that use OpenAI. Validation occurs in the selected adapter; custom adapters need not require this credential. Managed creation itself does not require an OpenAI key. |
| `DEVELOPER_OPENAI_API_KEY` | Empty; `<separate-developer-openai-key>`. | Deployment-only override: developer-role managed containers receive it as `OPENAI_API_KEY` when nonempty. Standalone developer Compose uses the same fallback to `OPENAI_API_KEY`. The worker does not receive a separate key under this name; simple workers retain the main key. |
| `ANTHROPIC_API_KEY` | Empty; `<anthropic-api-key>`. | Required by built-in Claude coding when selected; passed to all managed roles if present, and to the standalone developer service. Does not switch the selected coding adapter. |
| `WORKER_CONFIG_FILE` | `worker-config.yaml` for standalone loader; `/config/worker-config.yaml` in standalone Compose, `/config/snapshot.yaml` in managed containers. | Selects standalone YAML file. Managed bootstrap retrieves current saved configuration from the API instead; the mounted immutable snapshot is not an outage fallback. Example local run: `/srv/factory/worker-config.yaml`. |
| `WORKER_PROFILE` | `simple` in loader; standalone developer service uses `developer`; managed value is its selected profile. | Selects the named entry in `workers` when resolving standalone YAML or downloaded managed configuration. Example: `developer`. Unknown/invalid names fail configuration validation. Container recreation is needed to change its environment selector. |
| `FACTORY_CONTROL_ENDPOINT` | Absent for standalone; injected from server `worker_endpoint` for managed workers. | A nonempty value switches startup to authenticated remote bootstrap and wraps the configured observer with control-plane event reporting. Example `http://factory-control-api:8000`. An unavailable API makes managed startup fail; restart policy can retry later rather than running stale configuration. |
| `FACTORY_CONTROL_TOKEN` | Empty fallback in HTTP clients; generated/injected for managed workers. | Bearer authentication for `/api/v1/worker-bootstrap` and `/api/v1/ingest`. Derived from server encryption key plus worker ID; DB stores its digest. Example `<server-issued-worker-token>`. Do not manually copy an administrator password here. Missing/wrong token prevents bootstrap/ingestion. |
| `FACTORY_CONTROL_WORKER_ID` | Empty fallback; injected worker UUID. | `X-Worker-Id` identifies the managed worker for bootstrap/ingestion and scopes bearer validation. Example `11111111-2222-4333-8444-555555555555`. It must match the server-issued token and existing worker record. |
| `FACTORY_TELEMETRY_PROCESS_ROLE` | `worker`; Codex healthcheck sets `healthcheck`. | Internal telemetry classification and suppression of worker-only runtime metrics in healthcheck processes. This is process metadata, not a YAML override or control-plane setting; normal deployments leave it unset. |

`./scripts/start-workers.sh` and `./scripts/start-observed-workers.sh` always require the Hatchet CLI development profile and a nonempty exported token, unlike the control-plane script's optional CLI lookup. They pass arguments to Compose `up --build`; the observed variant also includes `compose.observability.yaml`. See the worker and observability references for those settings.

## Dynamic control workflow namespace

`runtime_namespace` is configured by `CONTROL_RUNTIME_NAMESPACE` (default `flumic-dynamic-v1`). It is a lowercase slug of 1–80 characters beginning with a letter. For example, `flumic-staging-v1` isolates control handler names in a shared Hatchet tenant. The API outbox and `dynamic-runtime` service must use the same value; recreate both after changing it. This does not select a project, repository, worker pool or business configuration. Existing queued engine workflow names are not migrated automatically. See [runtime activation](project-runtime-api.md).

## Saved profile metadata and runtime consequences

The control plane bootstraps factory configuration and named profiles from repository files when the corresponding database records are absent; editing those files is not a replacement for editing existing saved database profiles. A saved worker profile record has `name` (1–100 characters; display name), `profile` (1–100 characters; existing factory worker profile), `factory_revision_id` (the shared factory revision to combine with it), and `document` (validated worker-local settings and task model/prompt overrides). `parent_id` links a new version and `cloned_from` records cloning provenance; neither is a runtime tuning value. Unknown top-level API fields are rejected. See [worker configuration](configuration-workers.md) for the local/shared split and each YAML value.

When compiling a worker snapshot, the server fixes `worker_name` to the managed worker's name and `control_revision` to its snapshot revision ID. These are derived identity/correlation metadata, not separate environment knobs. The snapshot contains a single selected worker profile with resolved defaults and tasks. Managed Docker creation uses a read-only snapshot mount, shared writable `/data`, read-only model-pricing file, fixed `worker-network`, 2 GiB memory, two CPUs and `unless-stopped`; none is an `AppSettings` environment override. It optionally joins `factory-signoz-network` if that network exists. Default images are `hatchet-test-worker:latest` or `hatchet-test-developer-worker:latest`; a configured `base_image` triggers the custom image builder. Images must advertise control protocol `3` before creation.

Stopping and starting a managed process fetches its linked saved profile again, including after Docker restarts. Applying changes that require a new container/image follows the control-plane configuration/reconciliation path; reconfiguration rejects a running worker until it is stopped. Image dependencies, installed coding tools and container credentials cannot be refreshed merely by changing the profile payload read into an already-running process.

## Durable data, keys and recovery

Back up the PostgreSQL data, `.env.control-plane` encryption key and durable `data/control-plane` files together. The default named DB volume survives normal Compose down/up, but volume deletion discards saved state; `CONTROL_DB_DATA_PATH` must keep pointing to the intended durable directory/volume. Snapshots and build progress use the host `data/control-plane` mount, separate from PostgreSQL. Worker `/data` also holds the configured worker state/checkout paths and needs its own persistence plan.

Fernet protects captured conversations, which are stored with a seven-day expiration timestamp; normal event payloads are redacted separately. Losing/changing the key prevents decryption of existing ciphertext and changes newly derived worker tokens. Existing runtime tokens/DB hashes may still agree with each other until a runtime is reconfigured; replacing the key is not a complete token rotation procedure. There is no automatic re-encryption/rotation workflow here. Preserve the original key for recovery and coordinate any deliberate migration across API, reconciler, saved credentials and managed runtimes.
