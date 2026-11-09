# ADR 0010: Separate model inference from stage behavior

- Status: Accepted for the transport slice
- Date: 2026-11-09
- Related: [ADR 0009](0009-execution-mode-authority.md), [gateway delivery plan](../roadmap/2026-10-30-litellm-integration-plan.md)

## Decision

Stage clients own prompts, task-specific result validation, and conversation
capture. Model transports own the wire protocol, token assessment, credential
headers, and translation into usage and completion records. The existing
OpenAI adapter keeps its task-level model selection, token counting behavior,
JSON-object requests, and SDK retry settings.

A gateway transport consumes an operator-owned connection revision and a
separately resolved inference credential. The connection fixes an HTTPS base
URL, timeout and explicit alias-to-tokenizer map. The adapter rejects redirects,
unrecognized aliases, and unsupported request shapes. A gateway error or
uncertain network outcome returns to the caller without automatic retry or
Direct fallback.

Keep local context estimates distinct from usage reported after inference. The
initial gateway fixture qualifies one JSON-object Chat Completions route. Local
tokenization provides a context preflight estimate. Durable authorization and
allowance enforcement, route binding at admission, CLI protocols, and protected
runtime enablement have separate release gates.

## Consequences

New protocol adapters can share the same stage logic. Operators must qualify
each routing and tokenizer pairing before activation. Changing the external
destination behind an alias can invalidate earlier qualification even when the
Flumic connection revision has stayed the same.
