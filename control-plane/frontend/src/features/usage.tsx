import { Code2, Command, Layers3, Terminal } from "lucide-react";
import type { Usage } from "../api";
import {
  count,
  Failure,
  Metric,
  money,
  PageTitle,
  Section,
} from "../components/ui";
import { useProjectData } from "../project-context";
import { useData } from "../hooks";

export function UsagePage() {
  const data = useData<Usage>("usage", "/usage");
  return <UsageSummary usage={data.data} error={data.error} />;
}
export function ProjectUsagePage() {
  const data = useProjectData<Usage>("/usage");
  return <UsageSummary usage={data.data} error={data.error} project />;
}
function UsageSummary({
  usage,
  error,
  project = false,
}: {
  usage?: Usage;
  error: unknown;
  project?: boolean;
}) {
  return (
    <>
      <PageTitle
        eyebrow="ACCOUNTING"
        title="Usage & cost"
        description={
          project
            ? "Token consumption and known costs attributed to this project."
            : "Understand token consumption and known costs across recorded model calls."
        }
      />
      <Failure error={error} />
      <div className="metrics">
        <Metric
          label="Input tokens"
          value={count(usage?.input_tokens)}
          detail="Includes cache reads"
          icon={<Command size={17} />}
        />
        <Metric
          label="Output tokens"
          value={count(usage?.output_tokens)}
          detail="Includes reasoning tokens"
          icon={<Code2 size={17} />}
        />
        <Metric
          label="Estimated cost"
          value={money(usage?.estimated_usd)}
          detail="Known Standard API price subtotal"
          icon={<Layers3 size={17} />}
        />
        <Metric
          label="CLI-reported cost"
          value={money(usage?.reported_usd)}
          detail="Separate accounting source"
          icon={<Terminal size={17} />}
        />
      </div>
      <Section title="Accounting coverage">
        <div className="coverage">
          <div>
            <span>Model accounting events</span>
            <strong>{count(usage?.calls)}</strong>
          </div>
          <div>
            <span>Calls with unknown estimated cost</span>
            <strong>{count(usage?.unpriced_calls)}</strong>
          </div>
          <div>
            <span>Calls with missing usage</span>
            <strong>{count(usage?.missing_usage_calls)}</strong>
          </div>
          <div>
            <span>Query coverage</span>
            <strong>
              {usage?.truncated
                ? "Limited to 10,000 recent events"
                : "All retained events up to 10,000"}
            </strong>
          </div>
        </div>
      </Section>
      <div className="notice">
        Estimated and CLI-reported amounts can describe the same call. They are
        not added together. Unknown values are not zero. Long-context tiers,
        unreported cache writes, tool fees, and invoices can differ.
      </div>
    </>
  );
}

export function ProjectAccounting({ query }: { query: string }) {
  const data = useProjectData<Usage>("/usage?" + query),
    value = data.data;
  return (
    <Section title="Project AI accounting">
      <div className="editor-body">
        <Failure error={data.error} />
        <div className="project-accounting-metrics">
          <div>
            <span>Input tokens</span>
            <strong>
              {value?.input_tokens == null
                ? "Unknown"
                : count(value.input_tokens)}{" "}
            </strong>
          </div>
          <div>
            <span>Output tokens</span>
            <strong>
              {value?.output_tokens == null
                ? "Unknown"
                : count(value.output_tokens)}
            </strong>
          </div>
          <div>
            <span>Estimated cost</span>
            <strong>{money(value?.estimated_usd)}</strong>
          </div>
          <div>
            <span>Reported cost</span>
            <strong>{money(value?.reported_usd)}</strong>
          </div>
        </div>
        <p>
          {count(value?.calls)} accounting calls ·{" "}
          {count(value?.unpriced_calls)} unpriced ·{" "}
          {count(value?.missing_usage_calls)} missing usage.{" "}
          {value?.truncated
            ? "Limited to 10,000 retained events."
            : "Retained events only, up to 10,000."}
        </p>
        <small>
          Known subtotals only. Estimated and reported costs are separate and
          are never added. CPU/memory do not imply token usage or AI cost.
        </small>
      </div>
    </Section>
  );
}
