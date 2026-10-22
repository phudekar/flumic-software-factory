---
title: "Trace each stage, token and cost"
---

## Follow the evidence

```text
GitHub issue → factory workflow run → engine task / stage
  → stage attempt → worker + actual configuration revision
  → recorded model exchange → provider usage + known cost estimate
```

Open a run, select a stage, then inspect attempts, logs, model calls and usage. The selected stage uses engine task IDs to scope records; selecting an attempt further narrows to one retry. Worker detail links retain their context. This makes it possible to investigate which stage failed, which settings were loaded and which reported calls contributed to its token/cost subtotal.

## Reading tokens correctly

Input, output, cached input, reasoning and cache-creation tokens depend on provider reporting. Cached input is included in normalized input, and reasoning is a subset of output. Do not add these subsets to input/output again. Codex reports completed-turn totals; Claude can report per-model accounting. CLI totals do not reconstruct every internal tool/model invocation.

Missing usage remains **unknown**. A stage that has no model call is different from a model call with missing accounting. Failed calls may incur provider costs that cannot be recovered from a usable response.

## Reading cost correctly

Estimated USD uses reported usage and a configured price. Reported client USD is displayed separately. Do not add calculated and client estimates for the same invocation. Null/unpriced data does not imply free execution. The UI subtotal is bounded to retained accounting events and marks missing usage, unpriced calls and query truncation.

Example, for illustration only: a planning attempt may report 8,000 input and 900 output tokens but no matching price. Its token subtotal is known and its USD estimate is unknown. A coding retry adds another attempt; filtering the second attempt should not include the first attempt's recorded usage.

## Where to investigate

| View | Question |
| --- | --- |
| Run → stage → attempt | What happened on this try? |
| Stage logs / model exchanges | Which error or recorded request/result explains it? |
| Usage & cost | Which retained records contribute to known totals? |
| Worker detail | Which instance and loaded configuration participated? |
| SigNoz traces | How do instrumented spans and logs correlate? |
| SigNoz resource metrics | Was container CPU/memory/I/O unusual? |

[Detailed accounting and price formula](/software-factory/handbook/observability/#cost-estimates) · [API usage filters](/software-factory/api/contracts/#usage)

## See it in the control plane

![Run detail with stage and attempt selectors and accounting for the current selection](/software-factory/images/control-plane-stage-attempts.png)

This completed planning attempt has two recorded model calls, with input/output token totals and a known estimated cost for the selection. Reported client USD remains unknown. Stage and attempt selectors keep accounting scoped to the same execution context.

![Global usage page with separate token totals, known estimated cost, unpriced calls and coverage](/software-factory/images/control-plane-usage.png)

The global Usage page summarizes retained accounting records. It complements the run detail's stage/attempt scope; values shown here come from an existing installation and are not expected first-run totals.

## Model usage and cost in SigNoz

![SigNoz one-day token, accounting-event and estimated-cost panels with stage duration](/software-factory/images/signoz-model-usage-cost.png)

This capture shows actual recorded model activity and known estimated spend from a local installation. The empty CLI-reported panel indicates unavailable client estimates, not free usage. Use SigNoz for rates and correlated telemetry; use the run detail's stage/attempt filters for the corresponding retained application accounting records.

[Set up these dashboards](/software-factory/observability/setup/) · [Pricing and token normalization](/software-factory/handbook/observability/#cost-estimates)
