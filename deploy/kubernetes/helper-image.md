# Approved images and helper protocol

Build the fixed helper from the repository root:

```sh
docker buildx build --platform linux/amd64,linux/arm64 \
  -f control-plane/kubernetes-helper/Dockerfile \
  -t registry.example.com/flumic/helper:reviewed \
  --push control-plane/kubernetes-helper
```

Resolve the published manifest digest and replace the tag with `@sha256:...` in the target registry. The helper image must contain `/opt/flumic/flumic_job_helper.py` and `python`; the runtime invokes a fixed `python -I -B` command. Build/pin simple and developer workers from the repository's reviewed worker Dockerfiles, verify their `shared-v1` handshake and published platform list, and ensure each eligible node architecture is covered. Mutable tags are rejected. For k3d use an owned registry or import an image built for the node architecture before referencing its locally available digest. A multiarch index digest and its per-platform digest are different: pin the manifest actually available to the cluster.

The init helper validates bounded input chunks, index/count/length and SHA-256 proofs before writing `/flumic/input.json` and optional `/flumic/context.json`. Credentials are a separate action-only Secret mounted at `/flumic/credentials.json`. Actions may use any approved language/image and write `/flumic/output/result.json` containing `{ "output": {"ok": true}, "usage": [] }`. AI usage must follow the documented action result contract; CPU and memory are never inferred as AI usage.

The collector receives only read-only output and a private control volume. It has no credentials, Kubernetes token or shared PID namespace. Fixed capture/release commands use bounded exec, reject symlinks/hardlinks/FIFOs and malformed or oversized JSON, and retain the collector until capture is durable. Action output is limited to 1 MiB; context to 16 KiB. Helper memory defaults: init 64 MiB, collector 96 MiB, CPU 100m each. Account for this overhead in scheduling alongside action limits and memory-backed scratch volumes. Kubernetes scratch is executable; it does not reproduce Docker's `/tmp noexec` behavior.

Logs from Kubernetes are a **combined action stream**. Capture can be unavailable after a hard deadline, eviction or collector loss. The broker does not invent empty successful output or zero usage when capture is missing. No exactly-once guarantee applies to external side effects performed before failure or cancellation.
