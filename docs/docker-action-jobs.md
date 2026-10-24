# One-time Docker actions

The dynamic runtime has a durable Docker job broker separate from persistent worker management. A shared worker never receives the Docker socket. The Control Plane reconciler launches jobs from an owned, admitted assignment and its pinned workflow revision.

**Delivery status:** the broker integrates with assignment-authenticated HTTP submission, worker polling and broker-confirmed graph completion through the [project runtime API](project-runtime-api.md). The public UI does not launch these jobs yet. Kubernetes Jobs and remote/function executors remain planned.

## Define an action

Publish a dynamic graph with a `docker` stage. Use an actual image digest available on the Docker target; the broker does not pull or build images during execution.

```yaml
schema_version: 1
name: validate-change
repositories: [backend]
default_repository: backend
stages:
  - id: validate
    executor: docker
    role: simple
    pool_id: <shared-pool-id>
    image: registry.example.com/team/check@sha256:<64-hex-digest>
    command: [python, /app/check.py]
    inputs:
      change: {$ref: input.change}
    timeout_seconds: 300
    retries: 0
    network: none
    limits:
      cpu: 1.0
      memory_mib: 512
      pids: 128
      tmpfs_mib: 64
```

The placeholder digest above must be replaced before validation. `command` is an explicit executable/argument array; it overrides the image entrypoint. Shell interpretation happens only when an operator deliberately selects a shell executable. Link another stage with `after: [validate]` and use `{$ref: stages.validate.output}` to consume its whole JSON output. Normal graph ownership, provenance and dependency rules still apply.

| Field | Default / accepted values | Execution effect |
| --- | --- | --- |
| `image` | Required `repository@sha256:<64 lowercase hex>` | Pins the exact image reference; tags are rejected. Pre-pull it on the target. |
| `command` | Required 1–128 strings, each at most 4,096 characters | Executable followed by arguments, with no inherited image entrypoint. NUL bytes are rejected. |
| `inputs` | JSON object, resolved input at most 1 MiB | Written to read-only `/flumic/input.json`; stdout is not an input/result protocol. |
| `timeout_seconds` | 300; 1–3,600 | Absolute broker deadline starts at accepted submission and survives retries/restarts; it is not reset by polling. |
| `retries` | 0; 0–3 | A confirmed terminal stage failure may produce a new logical attempt under graph policy. An uncertain job is never automatically duplicated. |
| `limits.cpu` | 1.0; 0.1–8.0 | Docker CPU quota. |
| `limits.memory_mib` | 512; 64–4,096 | Container memory limit; an OOM termination fails the job. |
| `limits.pids` | 128; 16–512 | Maximum container processes. |
| `limits.tmpfs_mib` | 64; 16–256 | `/tmp` scratch mount size, with `noexec,nosuid,nodev`. |
| `network` | `none`; optionally `bridge` | Default disables job networking. Bridge requires a fixed dedicated deployment network; it is not a host/metadata egress firewall. |
| `secrets` | Empty; explicit `github`, `openai`, `anthropic` version IDs | Only purposes explicitly declared by the graph/stage are delivered. A Docker stage does not inherit a repository credential merely because the connection has one. |

## Program contract

Read `/flumic/input.json`, optionally use the new read-only `/flumic/context.json` for canonical execution IDs/trace context, and read explicitly granted `/flumic/credentials.json`, then write `/flumic/output/result.json` and exit zero. Credentials are a purpose-to-value JSON object, not environment variables. No worker control token or global provider environment is inherited from the Control Plane.

```python
import json
from pathlib import Path

request = json.loads(Path('/flumic/input.json').read_text())
result = {'output': {'valid': bool(request['change'])}, 'usage': []}
Path('/flumic/output/result.json').write_text(json.dumps(result))
```

The envelope contains an `output` JSON object and optional `usage` array (at most 256 records). Input and the entire result file are each limited to 1 MiB. Nonfinite JSON, missing/malformed files, symlinks, nonregular files and oversized results fail explicitly. Usage values are supplied by the program; they are not inferred from container CPU/memory. Accounting ingestion and reporting belong to the project telemetry milestone.

Stdout and stderr are captured separately, retaining at most 256 KiB each with truncation flags. Failure logs are retained too. Docker's own log rotation is bounded separately to two 1 MiB files. Resolved grant values are redacted before encrypted result persistence, with protocol keys/failure status preserved. Log limits are reapplied after redaction and truncation flags reflect any additional clipping. A redacted output/usage envelope exceeding 1 MiB fails explicitly with a bounded diagnostic. Operators must still avoid logging sensitive derived values.

## Durability and cancellation

A project/run/stage/logical-attempt has one durable job identity. Resubmission with the same digest returns its receipt; changing the digest conflicts. A new logical attempt gets a new identity. Deterministic container names and deployment/project/execution/spec labels permit recovery after create/start/exit or an interrupted result commit. Every attach, inspection, stop and cleanup verifies the pinned image and labels.

The broker commits launch intent before contacting Docker. A container whose launch response was lost is inspected and reattached. If the launch outcome is uncertain and no container can be observed, the receipt stays `unknown`; it is not relaunched or reported cancelled merely because an RPC failed. Keep its durable records and investigate the Docker target. If the delayed container appears, reconciliation resumes and applies any persisted stop intent. An observed container that later disappears is also never automatically recreated.

Cancellation and deadlines are broker-owned and work after a worker/coordinator exits. Reconciliation requests stop with a five-second grace, escalates to kill if necessary, and verifies terminality before recording a terminal receipt. Cancellation wins if persisted before the result's terminal commit. Deadlines are checked again at that commit. Completed remote effects are not undone.

The reconciler runs the job loop independently of persistent-worker image builds. Each two-second tick reserves a budget of up to eight active jobs plus one historical cleanup; completed tombstones cannot consume active launch/stop service capacity. Enforcement includes polling, queue and Docker response latency, so this is not a hard real-time deadline. It persists the terminal output/status before a later cleanup tick removes the container and its job directory. Durable terminal records remain as identity tombstones, with periodic cleanup reconciliation.

## Deployment and isolation

Broker instances for a local Docker target must share the same private job directory and stable deployment identity. POSIX file locks serialize job I/O without holding database transactions across Docker calls; use a local filesystem supporting `flock`. This is a single-target local Docker implementation, not distributed Kubernetes orchestration. Mount paths are generated from deployment-owned roots; users cannot submit host paths or Docker options.

Jobs run as UID/GID `10001:10001`, with a read-only root, all Linux capabilities dropped, `no-new-privileges`, restart policy `no`, no automatic container removal and broker-generated input/credential/output mounts only. The broker parent directory is private; mounted input/credential files are read-only and accessible to the fixed container UID. The output mount is writable by that UID. The trusted broker/reconciler alone has Docker access.

`CONTROL_JOB_BRIDGE` can name a pre-created dedicated network for trusted networked actions. Reserved Docker `host`, `bridge` and `none` names are rejected. Graph publication accepts bridge stages only when this fixed deployment policy is configured.

The writable output bind mount has **no hard host disk quota** in v1. Result retention is bounded, but a program can write unrelated files before it exits. Monitor free disk space, use a dedicated filesystem/quota where needed, and verify cleanup. Protect and back up the database/encryption key; keep broker data and locks available during reconciliation. Do not delete active jobs or reuse their durable identities.

Images are trusted operator selections. Docker restrictions are not a hostile-code sandbox or protection against exfiltration of intentionally granted secrets. Hostile-image isolation and network egress controls require additional infrastructure.

## Verify locally

Fake Docker/database tests cover ownership, identity conflicts, replay, deadline/cancel races, result validation and failure capture. Run the opt-in contract with an already available Python image pinned by digest:

```sh
RUN_DOCKER_JOB_CONTRACT=1 \
DOCKER_JOB_CONTRACT_IMAGE='your-image@sha256:<digest>' \
PYTHONPATH=worker:control-plane/backend \
python -m unittest discover -s tests/integration -p test_docker_job_contract.py -v
```

It launches a uniquely named deterministic JSON job, reconstructs the broker, verifies the container identity/result and cleans up only its own job. It performs no GitHub or model calls.

Declared usage is attributed and deduplicated through [project accounting](project-observability.md#custom-docker-actions); container resources and missing reports do not imply zero AI spend.
