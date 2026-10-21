import {
  ArrowUpRight,
  ChevronRight,
  Command,
  GitBranch,
  Layers3,
  Plus,
  Server,
  Workflow,
} from "lucide-react";
import { Link } from "react-router-dom";
import type { Run, Usage, Workers } from "../api";
import {
  count,
  Empty,
  Failure,
  Metric,
  money,
  PageTitle,
  RunTable,
  Section,
  Status,
} from "../components/ui";
import { useData } from "../hooks";

export function Overview() {
  const workers = useData<Workers>("workers", "/workers");
  const runs = useData<{ items: Run[]; warnings: string[] }>("runs", "/runs");
  const usage = useData<Usage>("usage", "/usage");
  const w = workers.data;
  const running =
    w?.managed.filter((x) => x.runtime?.status === "running").length ?? 0;
  return (
    <>
      <PageTitle
        eyebrow="YOUR OPERATIONS, AT A GLANCE"
        title="Factory overview"
        description="Follow your workers, workflows, and AI usage in one place."
        action={
          <Link className="primary" to="/workers">
            <Plus size={15} />
            Manage workers
          </Link>
        }
      />
      <div className="metrics">
        <Metric
          label="Managed workers"
          value={workers.isPending ? "…" : String(w?.managed.length ?? 0)}
          detail={`${running} running · ${w?.discovered.length ?? 0} discovered`}
          icon={<Server size={17} />}
        />
        <Metric
          label="Recent workflow runs"
          value={runs.isPending ? "…" : String(runs.data?.items.length ?? 0)}
          detail="Latest page from Hatchet"
          icon={<Workflow size={17} />}
        />
        <Metric
          label="Recorded tokens"
          value={count(
            usage.data?.input_tokens == null &&
              usage.data?.output_tokens == null
              ? null
              : (usage.data?.input_tokens ?? 0) +
                  (usage.data?.output_tokens ?? 0),
          )}
          detail={`${count(usage.data?.calls)} model accounting events`}
          icon={<Command size={17} />}
        />
        <Metric
          label="Estimated spend"
          value={money(usage.data?.estimated_usd)}
          detail={`${usage.data?.unpriced_calls ?? 0} calls without a price estimate`}
          icon={<Layers3 size={17} />}
        />
      </div>
      {w?.warnings.map((x) => (
        <div className="notice" key={x}>
          {x}
        </div>
      ))}
      {runs.data?.warnings.map((x) => (
        <div className="notice" key={x}>
          {x}
        </div>
      ))}
      <Failure error={workers.error || usage.error} />
      <Section
        title="Recent workflow runs"
        aside={
          <Link className="text-link" to="/runs">
            View all runs <ArrowUpRight size={14} />
          </Link>
        }
      >
        <RunTable items={runs.data?.items ?? []} loading={runs.isPending} />
      </Section>
      <div className="two-column">
        <Section
          title="Worker fleet"
          aside={
            <Link className="text-link" to="/workers">
              Manage <ArrowUpRight size={14} />
            </Link>
          }
        >
          {!w?.managed.length ? (
            <Empty
              title="No managed workers yet"
              detail="Create a stopped worker from an approved profile. Starting it is a separate action."
            />
          ) : (
            <div className="fleet">
              {w.managed.slice(0, 5).map((x) => (
                <div key={x.id}>
                  <span className="resource-icon">
                    <Server size={17} />
                  </span>
                  <div>
                    <strong>{x.name}</strong>
                    <small>
                      {x.profile} · {x.target_id}
                    </small>
                  </div>
                  <Status value={x.runtime?.status ?? "unknown"} />
                </div>
              ))}
            </div>
          )}
        </Section>
        <Section title="Built for traceability">
          <div className="explainer">
            <span className="resource-icon">
              <GitBranch />
            </span>
            <h3>Every stage has a story.</h3>
            <p>
              Move from a workflow to its attempts, stage logs, model calls, and
              exact configuration revision.
            </p>
            <div>
              <span className="tag">Workflow</span>
              <ChevronRight size={13} />
              <span className="tag">Stage</span>
              <ChevronRight size={13} />
              <span className="tag">Model call</span>
            </div>
            <small>
              Conversation history begins with newly instrumented workers. Older
              exchanges cannot be reconstructed.
            </small>
          </div>
        </Section>
      </div>
    </>
  );
}
