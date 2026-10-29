import { useState } from "react";
import { Link } from "react-router-dom";
import type { ProjectRun } from "../workflow-api";
import { projectPath, useProjectId } from "../project-context";
import { Status } from "./ui";
import {
  humanWorkflowName,
  localTimestamp,
  relativeTimestamp,
} from "./project-format";

export function ProjectRunTable({
  runs,
  compact = false,
}: {
  runs: ProjectRun[];
  compact?: boolean;
}) {
  const project = useProjectId();
  const [search, setSearch] = useState("");
  const [state, setState] = useState("");
  const query = search.trim().toLocaleLowerCase();
  const visible = runs.filter(
    (run) =>
      (compact || !state || run.state === state) &&
      (compact ||
        [run.id, run.name, humanWorkflowName(run.name)].some((value) =>
          value.toLocaleLowerCase().includes(query),
        )),
  );
  if (!runs.length)
    return (
      <div className="empty">
        <h3>No project runs yet.</h3>
        <p>
          Choose a saved workflow and finish repository and worker setup before
          launching.
        </p>
      </div>
    );
  return (
    <>
      {!compact && (
        <div className="project-run-toolbar">
          <label>
            Search runs
            <input
              type="search"
              value={search}
              placeholder="Workflow name or run ID"
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <label>
            Run status
            <select
              value={state}
              onChange={(event) => setState(event.target.value)}
            >
              <option value="">All statuses</option>
              {[...new Set(runs.map((run) => run.state))]
                .sort()
                .map((status) => (
                  <option key={status} value={status}>
                    {status.replaceAll("_", " ")}
                  </option>
                ))}
            </select>
          </label>
          <span>
            {visible.length} of {runs.length} runs
          </span>
        </div>
      )}
      {visible.length ? (
        <div className="table-scroll">
          <table className="project-runs-table">
            <thead>
              <tr>
                <th>Workflow</th>
                <th>Status</th>
                <th>Dispatch</th>
                <th>Created</th>
                <th>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {visible.map((run) => (
                <tr key={run.id}>
                  <td>
                    <strong>{humanWorkflowName(run.name)}</strong>
                    <small title={run.id}>
                      {run.id.slice(0, 12)}
                      {run.id.length > 12 ? "…" : ""}
                    </small>
                  </td>
                  <td>
                    <Status value={run.state} />
                  </td>
                  <td>
                    <Status value={run.dispatch.state} />
                  </td>
                  <td>
                    <time title={localTimestamp(run.created_at)}>
                      {relativeTimestamp(run.created_at)}
                    </time>
                    <small>{localTimestamp(run.created_at)}</small>
                  </td>
                  <td>
                    <Link
                      aria-label={"Inspect run " + run.id}
                      to={projectPath(
                        project,
                        "/runs/" + encodeURIComponent(run.id),
                      )}
                    >
                      Inspect run →
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="editor-body" role="status">
          No runs match these filters.
        </p>
      )}
    </>
  );
}
