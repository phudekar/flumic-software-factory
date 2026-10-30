import type { Graph } from "../workflow-api";

export function workflowPresentation(graph: Graph): {
  kind: "issue" | "feedback" | "verify" | "custom";
  title: string;
  description: string;
} {
  const actions = graph.stages.map((stage) => stage.action).join(",");
  if (
    graph.name.startsWith("factory-issue-") &&
    actions ===
      "issue.load@1,issue.plan@1,issue.triage@1,change.implement@1,change.review@1,workflow.record@1"
  )
    return {
      kind: "issue",
      title: "Issue to pull request",
      description:
        "Load an issue, plan and implement the change, then review and record the result.",
    };
  if (
    graph.name.startsWith("factory-feedback-") &&
    actions === "pull.feedback@1"
  )
    return {
      kind: "feedback",
      title: "Address pull request feedback",
      description:
        "Apply actionable review feedback to an existing pull request.",
    };
  if (graph.name.startsWith("factory-verify-") && actions === "pull.verify@1")
    return {
      kind: "verify",
      title: "Verify a merged pull request",
      description: "Check the merged change and record its outcome.",
    };
  return {
    kind: "custom",
    title: graph.name,
    description: "A custom workflow defined for this project.",
  };
}

export function StageSummary({ graph }: { graph: Graph }) {
  return (
    <section className="setup-stages" aria-labelledby="workflow-stages-title">
      <h2 id="workflow-stages-title">Stages</h2>
      <p>
        Dependencies determine execution order. Independent stages may run in
        parallel.
      </p>
      <ul className="stage-summary-list">
        {graph.stages.map((stage) => (
          <li key={stage.id} className="setup-card">
            <div className="setup-card-heading">
              <h3>{stage.id}</h3>
              <span className="setup-badge">{stage.role} worker</span>
            </div>
            <p>
              {stage.action ?? `${stage.executor} action`} · Repository:{" "}
              {stage.repository ?? graph.default_repository}
            </p>
            <p>
              {stage.after?.length
                ? `After: ${stage.after.join(", ")}`
                : "No stage dependencies"}
            </p>
            {stage.when != null && (
              <p>Runs when its configured condition matches.</p>
            )}
            <details>
              <summary>Advanced stage settings</summary>
              <pre>{JSON.stringify(stage, null, 2)}</pre>
            </details>
          </li>
        ))}
      </ul>
    </section>
  );
}
