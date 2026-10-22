---
title: "Security, storage and recovery"
---

## Trusted local operator

The API/reconciler and resource collector have Docker socket access. Treat this as privileged host access. The control plane supports a local administrator, not Internet-facing multi-user operation. The Pages website is public static documentation and does not expose the local API.

## Data sent to models

Issue context and selected source content reach the selected provider. Coding CLIs receive a repository archive, the selected provider key, network access and shared mounts. Best-effort secret scanning/redaction does not prove ordinary source text is safe to disclose. Review high-impact changes, especially CI/infrastructure.

## Separate capture paths

Standard telemetry excludes prompts, completions and tool transcripts. Control-plane capture can retain bounded redacted prompts/results under `redacted`, `metadata_only` or `disabled` policy. Content is encrypted, reads are audited and retention applies. Missing history cannot be reconstructed; CLI captures are not full internal conversations.

## Backups and recovery

Back up control PostgreSQL together with the encryption key in `.env.control-plane`. Losing the key makes stored conversations unreadable; casual rotation also affects worker ingestion credentials. Factory execution SQLite/locks and prepared changes live under the shared `data/` directory and need their own consistent backup procedure.

Stop writers before filesystem-level copies; use database-aware backups when available. Do not delete volumes to troubleshoot a running installation. For host PostgreSQL storage, follow the [documented migration](/flumic-software-factory/handbook/control-plane/#host-database-storage), preserve the old volume until verified, and retain the PostgreSQL major version.

## Upgrades

Build changed worker code/dependencies into new images, then replace runtimes intentionally. Coordinate shared factory revisions across profiles and workers. SigNoz has pinned/generated deployment assets; follow its lifecycle guide when regenerating. There is no automated safe rolling drain.

[Detailed security boundary](/flumic-software-factory/handbook/issue-lifecycle/#security-boundary-and-limitations) · [Architecture](/flumic-software-factory/handbook/architecture/)
