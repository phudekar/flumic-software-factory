# Projects and dynamic workflows roadmap

This is planned work. GitHub issues are the source of truth for current status; an issue is closed only after its acceptance criteria and verification evidence pass.

- [Delivery epic](https://github.com/phudekar/flumic-software-factory/issues/1)
- [Implementation milestone](https://github.com/phudekar/flumic-software-factory/milestone/1)

Each project supports multiple repositories. Projects own workflows, configuration, credentials and history; shared worker pools provide execution capacity. The first runtime supports built-in actions and one-time Docker jobs.

## Delivery sequence

| Issue | Deliverable | Depends on |
| --- | --- | --- |
| [#2](https://github.com/phudekar/flumic-software-factory/issues/2) | Introduce projects and multiple repository connections with a compatibility migration | None |
| [#3](https://github.com/phudekar/flumic-software-factory/issues/3) | Scope configuration and operator APIs by project and repository | [#2](https://github.com/phudekar/flumic-software-factory/issues/2) |
| [#4](https://github.com/phudekar/flumic-software-factory/issues/4) | Add immutable per-action context and project-scoped credential injection | [#2](https://github.com/phudekar/flumic-software-factory/issues/2), [#3](https://github.com/phudekar/flumic-software-factory/issues/3) |
| [#5](https://github.com/phudekar/flumic-software-factory/issues/5) | Introduce shared worker pools, capabilities and scoped stage assignments | [#3](https://github.com/phudekar/flumic-software-factory/issues/3), [#4](https://github.com/phudekar/flumic-software-factory/issues/4) |
| [#6](https://github.com/phudekar/flumic-software-factory/issues/6) | Define and publish immutable data-driven workflow revisions | [#3](https://github.com/phudekar/flumic-software-factory/issues/3) |
| [#7](https://github.com/phudekar/flumic-software-factory/issues/7) | Persist workflow runs, stage attempts, claims and fair admission limits | [#2](https://github.com/phudekar/flumic-software-factory/issues/2), [#4](https://github.com/phudekar/flumic-software-factory/issues/4), [#6](https://github.com/phudekar/flumic-software-factory/issues/6), [#5](https://github.com/phudekar/flumic-software-factory/issues/5) |
| [#8](https://github.com/phudekar/flumic-software-factory/issues/8) | Implement a durable data-driven coordinator and verify Hatchet replay contracts | [#6](https://github.com/phudekar/flumic-software-factory/issues/6), [#7](https://github.com/phudekar/flumic-software-factory/issues/7), [#5](https://github.com/phudekar/flumic-software-factory/issues/5) |
| [#9](https://github.com/phudekar/flumic-software-factory/issues/9) | Expose built-in actions and project-aware follow-up workflow dispatch | [#4](https://github.com/phudekar/flumic-software-factory/issues/4), [#6](https://github.com/phudekar/flumic-software-factory/issues/6), [#7](https://github.com/phudekar/flumic-software-factory/issues/7), [#8](https://github.com/phudekar/flumic-software-factory/issues/8) |
| [#10](https://github.com/phudekar/flumic-software-factory/issues/10) | Add a durable Docker broker for one-time custom action jobs | [#4](https://github.com/phudekar/flumic-software-factory/issues/4), [#5](https://github.com/phudekar/flumic-software-factory/issues/5), [#6](https://github.com/phudekar/flumic-software-factory/issues/6), [#7](https://github.com/phudekar/flumic-software-factory/issues/7) |
| [#11](https://github.com/phudekar/flumic-software-factory/issues/11) | Add project workflow/run/job APIs with revision pinning and cancellation | [#8](https://github.com/phudekar/flumic-software-factory/issues/8), [#9](https://github.com/phudekar/flumic-software-factory/issues/9), [#10](https://github.com/phudekar/flumic-software-factory/issues/10) |
| [#12](https://github.com/phudekar/flumic-software-factory/issues/12) | Attribute execution history, traces and token/cost usage to projects and repositories | [#7](https://github.com/phudekar/flumic-software-factory/issues/7), [#9](https://github.com/phudekar/flumic-software-factory/issues/9), [#10](https://github.com/phudekar/flumic-software-factory/issues/10), [#11](https://github.com/phudekar/flumic-software-factory/issues/11) |
| [#13](https://github.com/phudekar/flumic-software-factory/issues/13) | Add project selection, repositories, scoped configuration and shared pool views | [#3](https://github.com/phudekar/flumic-software-factory/issues/3), [#5](https://github.com/phudekar/flumic-software-factory/issues/5), [#4](https://github.com/phudekar/flumic-software-factory/issues/4) |
| [#14](https://github.com/phudekar/flumic-software-factory/issues/14) | Add workflow definition editing, validation and per-stage execution views | [#13](https://github.com/phudekar/flumic-software-factory/issues/13), [#11](https://github.com/phudekar/flumic-software-factory/issues/11), [#12](https://github.com/phudekar/flumic-software-factory/issues/12) |
| [#15](https://github.com/phudekar/flumic-software-factory/issues/15) | Verify multi-project mixed workflows, recovery and legacy migration; publish the operator guide | [#14](https://github.com/phudekar/flumic-software-factory/issues/14), [#12](https://github.com/phudekar/flumic-software-factory/issues/12), [#10](https://github.com/phudekar/flumic-software-factory/issues/10), [#9](https://github.com/phudekar/flumic-software-factory/issues/9) |

## Separate roadmap items

- [#16: Roadmap: support GitLab repository workflows and merge-request operations](https://github.com/phudekar/flumic-software-factory/issues/16)
- [#17: Roadmap: add Kubernetes worker orchestration and one-time Job execution](https://github.com/phudekar/flumic-software-factory/issues/17)
- [#18: Roadmap: add function/remote-job executors under the action contract](https://github.com/phudekar/flumic-software-factory/issues/18)
- [#19: Add a noncommercial source-available license and clear reuse documentation](https://github.com/phudekar/flumic-software-factory/issues/19)

## Working agreements

- Mark an issue in progress when implementation begins.
- Close as completed only with a linked commit/PR and validation evidence; update the epic checklist.
- Preserve current workflows and running workers during the compatibility migration.
- Keep GitLab, Kubernetes and function executors labeled as planned until implemented and verified.
