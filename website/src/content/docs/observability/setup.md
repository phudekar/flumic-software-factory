---
title: "Set up SigNoz and resource monitoring"
---

## Monitoring stack

```sh
./scripts/start-observability.sh
```

Open `http://localhost:3301`, then **Dashboards → Software Factory**. This installation keeps the existing dashboard title **Software Factory — Workers and AI Usage** and telemetry service identifiers after the Flumic AI rename. Bootstrap provisions the checked-in dashboard and reuses its stable name. Existing accounts need private SigNoz credentials in `.env.observability`. A fresh bootstrap can generate an account, saving credentials under `data/observability-bootstrap/login.json` without printing them.

This starts monitoring only. Configure `FACTORY_SHARED_TELEMETRY_ENDPOINT` in the
Control Pane environment for shared-worker infrastructure metrics and the OTEL
observer in project configuration for action telemetry. Provision workers from
pools; no additional static worker fleet is required. See the
[full monitoring reference](/flumic-software-factory/handbook/observability/).

## Worker and stage dashboard

![Actual SigNoz worker and stage dashboard with active tasks, heartbeats, attempts and failures](/flumic-software-factory/images/signoz-workers-stages.png)

This existing installation is shown over **Last 1 day**, not a fresh-install baseline. Gaps and absent series do not establish a healthy idle worker. The dashboard name and `software-factory` telemetry filters remain stable after the product rename.

## Inspect telemetry

The default dashboard filters service `software-factory` and environment `development`. Use trace attributes `factory.factory_run_id`, `factory.issue_number`, `factory.stage` and model names to find an execution. A stale heartbeat is missing/stale telemetry, not proof of a healthy idle worker.

Export is batched and best effort; outages can lose data. OTLP exports accounting and identifiers, not prompts/completions. Control-plane conversation capture is a separate policy-controlled path.

## Container resources

```sh
./scripts/start-resource-metrics.sh
```

A separate collector reads Docker CPU, memory, network and I/O, and discovers new containers. It observes all running containers on the host, including unrelated workloads. Docker socket access is privileged even when mounted read-only. Disk capacity series describe filesystems, not ownership of the full disk by each worker.

![Actual SigNoz CPU, memory, disk I/O and network panels for Docker containers](/flumic-software-factory/images/signoz-container-resources.png)

Resource charts include shared services and can include other Docker workloads on the host. CPU above 100% can represent multiple cores. Read [container metrics and disk-capacity semantics](/flumic-software-factory/handbook/observability/#infrastructure-resources) before comparing series.

## Validate without paid calls

```sh
.venv/bin/python scripts/telemetry-smoke.py
```

Synthetic events use `telemetry-smoke` and `factory.synthetic=true`. They do not populate the default development filter. See the [complete observability guide](/flumic-software-factory/handbook/observability/) for credential bootstrap, pricing, panel semantics, shutdown and reproducible upgrades.

## Independent lifecycle and dashboard updates

First setup attempts dashboard provisioning separately; later starts preserve local edits. Use `just dashboards-provision` to reconcile the checked-in JSON explicitly. Provisioning failures warn without failing the monitoring startup. Start without host resource collection with `FLUMIC_RESOURCE_METRICS=0 just observability-up`. See [local deployment stacks](/flumic-software-factory/handbook/deployment-compose/) for logs, shutdown and recovery commands.
