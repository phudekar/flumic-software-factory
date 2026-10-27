import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { parseDocument, stringify } from "yaml";
import { api, ApiError } from "./api";
import type { FactoryEvent } from "./api";
import { projectPath, useProjectId } from "./project-context";
export interface GraphStage {
  id: string;
  executor: string;
  action?: string;
  role: string;
  pool_id: string;
  after?: string[];
  inputs?: Record<string, unknown>;
  when?: unknown;
  repository?: string;
  image?: string;
  command?: string[];
  timeout_seconds?: number;
  retries?: number;
}
export interface Graph {
  schema_version: number;
  name: string;
  repositories: string[];
  default_repository: string;
  stages: GraphStage[];
  [key: string]: unknown;
}
export interface Definition {
  id: string;
  name: string;
  revision_id: string;
}
export interface DefinitionRevision {
  id: string;
  definition_id: string;
  parent_id: string | null;
  digest: string;
  created_at: number;
  document: Graph;
}
export interface WorkflowCatalog {
  definition_schema: unknown;
  actions: {
    id: string;
    role: string | null;
    input_schema: unknown;
    output_schema: unknown;
  }[];
  executors: { id: string; available: boolean; version?: string }[];
}
export interface PinnedStage extends GraphStage {
  repository_id: string;
  repository_alias: string;
  repository_revision_id: string;
  config_revision_id: string;
  settings: Record<string, unknown>;
}
export interface Execution {
  id: string;
  stage_id: string;
  generation: number;
  state: string;
  assignment_id: string | null;
  worker_id: string | null;
  job_id: string | null;
}
export interface ProjectRun {
  id: string;
  project_id: string;
  name: string;
  definition_id: string;
  revision_id: string;
  state: string;
  snapshot_id: string;
  created_at: number;
  dispatch: { state: string; engine_id: string | null; error: string | null };
  default_repository?: string;
  factory_revision_id?: string;
  repositories?: Record<string, { id: string; revision_id: string }>;
  stages?: PinnedStage[];
  attempts?: Execution[];
  followups?: {
    id: string;
    kind: string;
    state: string;
    result_run_id: string | null;
    error: string | null;
    dispatch?: { engine_id: string | null; state: string };
  }[];
}
export interface ProjectJob {
  target_id?: string;
  runtime?: {
    kind: string;
    namespace?: string;
    job_uid?: string;
    pod_uids?: string[];
    phase?: string;
    cleanup_pending?: boolean;
    log_stream?: string;
  };
  job_id: string;
  execution_id: string;
  state: string;
  deadline: number;
  cancel_requested: boolean;
  error: string | null;
  result: null | {
    exit_code?: number | null;
    error?: string | null;
    oom?: boolean;
    stdout?: string;
    stderr?: string;
    stdout_truncated?: boolean;
    stderr_truncated?: boolean;
    output?: unknown;
    usage?: unknown[];
  };
}
export function documentObject(source: string): Record<string, unknown> {
  if (new TextEncoder().encode(source).length > 262144)
    throw new Error("Document exceeds 256 KiB.");
  const parsed = parseDocument(source, { uniqueKeys: true });
  if (parsed.errors.length) throw new Error(parsed.errors[0].message);
  const value = parsed.toJS({ maxAliasCount: 0 });
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("Enter a YAML or JSON object.");
  return value;
}
export function graphSource(graph: unknown, format = "yaml") {
  return format === "json" ? JSON.stringify(graph, null, 2) : stringify(graph);
}
export function diagnostic(error: unknown): string {
  if (
    error instanceof ApiError &&
    error.detail &&
    typeof error.detail === "object"
  ) {
    const detail = error.detail as {
      message?: string;
      fields?: { path: (string | number)[]; type: string }[];
    };
    if (detail.fields)
      return [
        detail.message,
        ...detail.fields.map(
          (field) => field.path.join(".") + ": " + field.type,
        ),
      ]
        .filter(Boolean)
        .join("\n");
    if (Array.isArray(error.detail))
      return error.detail
        .map(
          (field) =>
            String(field.msg ?? "Invalid value") +
            " at " +
            (field.loc ?? []).join("."),
        )
        .join("\n");
  }
  return error instanceof Error ? error.message : String(error);
}
export function useWorkflowWrite() {
  const project = useProjectId(),
    client = useQueryClient(),
    alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  return {
    alive,
    write: async <T>(
      path: string,
      body: unknown,
      options?: { method?: "PUT"; idempotencyKey?: string },
    ) => {
      const result = await api<T>(projectPath(project, path), body, options);
      await client.invalidateQueries({ queryKey: ["project", project] });
      return result;
    },
  };
}

// Event accounting names intentionally differ from aggregate Usage response keys.
export interface ModelAccountingPayload extends Record<string, unknown> {
  provider?: string | null;
  model?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  estimated_usd?: number | null;
  reported_cost_usd?: number | null;
  accounting_leaf?: boolean;
  conversation_available?: boolean;
}
export interface ModelAccountingEvent extends FactoryEvent {
  kind: "model.completed";
  payload: ModelAccountingPayload;
}
