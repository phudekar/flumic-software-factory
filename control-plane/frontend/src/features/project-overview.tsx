import { Link } from "react-router-dom";
import { GitBranch, Workflow, Layers3 } from "lucide-react";
import type { Usage } from "../api";
import type {
  Definition,
  ProjectRun,
  ModelAccountingEvent,
} from "../workflow-api";
import { Failure, Metric, money, PageTitle, Section } from "../components/ui";
import { projectPath, useProjectData, useProjectId } from "../project-context";
import { ProjectCalls } from "./project-runs";
import { ProjectRunTable } from "../components/project-run-table";

export function ProjectOverview() {
  const project = useProjectId();
  const repositories = useProjectData<{ id: string }[]>("/repositories");
  const workflows = useProjectData<Definition[]>("/workflow-definitions");
  const runs = useProjectData<ProjectRun[]>("/workflow-runs?limit=50");
  const usage = useProjectData<Usage>("/usage");
  return (
    <>
      <PageTitle
        eyebrow="PROJECT WORKSPACE"
        title="Project overview"
        description="Repositories, published workflows, recent project executions and recorded AI cost for this project."
        action={
          <Link className="primary" to={projectPath(project, "/workflows")}>
            Browse workflows
          </Link>
        }
      />
      <Failure
        error={
          repositories.error || workflows.error || runs.error || usage.error
        }
      />
      <div className="metrics">
        <Metric
          label="Repositories"
          value={
            repositories.isPending
              ? "…"
              : repositories.data
                ? String(repositories.data.length)
                : "—"
          }
          detail="Connections in this project"
          icon={<GitBranch size={17} />}
        />
        <Metric
          label="Published workflows"
          value={
            workflows.isPending
              ? "…"
              : workflows.data
                ? String(workflows.data.length)
                : "—"
          }
          detail="Saved definitions, not live capacity"
          icon={<Workflow size={17} />}
        />
        <Metric
          label="Recent project runs"
          value={
            runs.isPending ? "…" : runs.data ? String(runs.data.length) : "—"
          }
          detail="Latest 50 project workflow runs"
          icon={<Workflow size={17} />}
        />
        <Metric
          label="Estimated spend"
          value={money(usage.data?.estimated_usd)}
          detail="Known estimated subtotal; missing prices stay unknown"
          icon={<Layers3 size={17} />}
        />
      </div>
      <div
        className="project-next-actions"
        aria-label="Project setup and monitoring"
      >
        <Link to={projectPath(project, "/repositories")}>
          <GitBranch size={20} />
          <strong>Connect your code</strong>
          <span>Manage target repositories and their execution settings.</span>
        </Link>
        <Link to={projectPath(project, "/workflows")}>
          <Workflow size={20} />
          <strong>Choose a workflow</strong>
          <span>
            Understand stages, inspect a saved version and launch a run.
          </span>
        </Link>
        <Link to={projectPath(project, "/history")}>
          <Layers3 size={20} />
          <strong>Inspect AI activity</strong>
          <span>Read captured model exchanges alongside tokens and costs.</span>
        </Link>
      </div>
      <Section
        title="Recent project runs"
        aside={<Link to={projectPath(project, "/runs")}>View all runs</Link>}
      >
        <div className="editor-body">
          {runs.isPending ? (
            <p>Loading project runs…</p>
          ) : runs.data?.length ? (
            <ProjectRunTable runs={runs.data.slice(0, 8)} compact />
          ) : !runs.error ? (
            <p>
              No project runs yet. Publish a workflow and complete worker setup
              before launching.
            </p>
          ) : null}
        </div>
      </Section>
      <p>
        Shared infrastructure is managed from the workspace.{" "}
        <Link to={projectPath(project, "/pools")}>Project pool membership</Link>{" "}
        controls authorized capacity; it does not prove that workers are
        available.
      </p>
    </>
  );
}

export function ProjectHistory() {
  const events = useProjectData<ModelAccountingEvent[]>(
    "/events?kind=model.completed",
  );
  return (
    <>
      <PageTitle
        eyebrow="PROJECT OBSERVABILITY"
        title="Model history"
        description="Retained model exchanges and accounting records attributed to this project."
      />
      <Failure error={events.error} />
      {events.isPending && <p role="status">Loading project model history…</p>}
      {events.data && <ProjectCalls events={events.data} />}
    </>
  );
}
