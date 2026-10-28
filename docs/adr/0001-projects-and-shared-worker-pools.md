# Projects own execution; worker pools share capacity

Status: accepted. Recorded 2026-10-28 from the implemented architecture and [PR #36](https://github.com/phudekar/flumic-software-factory/pull/36).

A project can contain multiple repositories and needs independent workflows, configuration, grants and history. Giving every project its own Control Plane or embedding project credentials in a long-lived worker would couple capacity to project ownership.

Projects own repository connections, immutable workflow/configuration snapshots, execution grants and history. Role-compatible shared worker pools serve authorized projects; assignments authenticate the worker and pin the project context. Before a claim changes state, the frozen container stage must be supported by the selected runtime target, including its network policy. An incapable Docker worker cannot reserve native-only bridge work ahead of a capable Kubernetes worker.

**Consequences:** capacity can scale independently of project count, while saved context and history remain scoped. Saving new configuration does not rewrite an admitted execution or hot-patch running processes. Project scoping is an application boundary for a trusted administrator; it is not multi-user authorization or a hostile-tenant sandbox.

See [project runtime API](../project-runtime-api.md), [claim validation](../../control-plane/backend/app/worker_pools.py) and [runtime authority](0003-postgresql-runtime-authority.md).
