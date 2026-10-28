# Use bounded action transport and explicit isolation limits

Status: accepted. Recorded 2026-10-28 from the implemented architecture and [PR #36](https://github.com/phudekar/flumic-software-factory/pull/36).

Portable container actions need inputs, scoped credentials, output capture and accounting without receiving controller authority. Docker and Kubernetes expose different isolation and logging primitives, so identical stage syntax cannot promise identical operating-system behavior.

Kubernetes uses bounded immutable ConfigMap payloads, an action-only credential Secret and fixed trusted init/collector helper operations. Worker/action Pods do not receive controller credentials or automounted API tokens. There is no Docker socket, hostPath or shared PID namespace in actions. Operators qualify enforcing network policies and effective Pod PID limits on their nodes. Result authority is separate from logs; a log outage can mark logs incomplete without discarding a valid result. Stage events and usage feed project views; optional OTLP export connects telemetry to SigNoz.

**Consequences:** Kubernetes scratch is executable, logs are combined, and helper/storage overhead is additional to action limits. NetworkPolicy retains Kubernetes' own-node traffic exception. Hard deadlines, collector failure or permanent storage loss can discard uncaptured output. Costs/tokens are reported or estimated only where instrumentation supplies them; unknown values remain unknown. Qualification covers the dedicated two-node k3d/K3s environment, not every distribution, provider or hostile administrator.

See [helper protocol](../../deploy/kubernetes/helper-image.md), [qualification and limits](../kubernetes.md) and [project observability](../project-observability.md).
