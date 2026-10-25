let csrf = "";
export function setCsrf(value: string) {
  csrf = value;
}
export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
  }
}
export async function api<T>(
  path: string,
  body?: unknown,
  options?: {
    idempotencyKey?: string;
    signal?: AbortSignal;
    method?: "POST" | "PUT";
  },
): Promise<T> {
  const response = await fetch("/api/v1" + path, {
    method: body === undefined ? "GET" : (options?.method ?? "POST"),
    signal: options?.signal,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrf,
      "Idempotency-Key": options?.idempotencyKey ?? crypto.randomUUID(),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401) {
    window.dispatchEvent(new Event("factory:unauthorized"));
    throw new Error("Please sign in.");
  }
  if (!response.ok) {
    const error = await response
      .json()
      .catch(() => ({ detail: "The service is unavailable." }));
    throw new ApiError(
      typeof error.detail === "string"
        ? error.detail
        : "Please check the submitted values.",
      response.status,
    );
  }
  return response.json();
}
export interface Runtime {
  profile_refresh_supported?: boolean;
  protocol_current?: boolean;
  id: string;
  instance_id: string | null;
  name: string;
  status: string;
  image: string;
  managed: boolean;
  profile: string | null;
  revision_id: string | null;
}
export interface Operation {
  id: string;
  action: string;
  status: string;
  error: string | null;
}
export interface WorkerBuild {
  status: string;
  image: string | null;
  content?: string;
  updated_at?: number;
}
export interface Worker {
  build?: WorkerBuild;
  config_id: string | null;
  worker_profile_id: string | null;
  worker_profile_name: string | null;
  profile_update_pending: boolean;
  target_id: string;
  id: string;
  name: string;
  profile: string;
  revision_id: string;
  desired_state: string;
  runtime: Runtime | null;
  engine: {
    id: string;
    status: string;
    paused: boolean;
    last_heartbeat: string;
  } | null;
  operations: Operation[];
}
export interface Workers {
  managed: Worker[];
  discovered: Runtime[];
  warnings: string[];
  observed_at: number;
}
export interface Run {
  id: string;
  name: string;
  status: string;
  workflow: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}
export interface Usage {
  calls: number;
  input_tokens: number | null;
  output_tokens: number | null;
  estimated_usd: number | null;
  reported_usd: number | null;
  unpriced_calls: number;
  missing_usage_calls: number;
  truncated: boolean;
  note: string;
}
export interface FactoryEvent {
  id: string;
  kind: string;
  worker_id: string;
  run_id: string | null;
  attempt_id: string | null;
  stage: string | null;
  occurred_at: number;
  payload: Record<string, unknown>;
}
export interface Task {
  additional_system_prompt: string;
  model: string | null;
  retries: number;
  timeout_seconds: number;
  worker_type: string;
}
export interface Profile {
  worker_type: string;
  model: string;
  coding_agent: string;
  tasks: Record<string, Task>;
}
export interface Revision {
  id: string;
  parent_id: string | null;
  created_at: number;
  summary: string;
  digest: string;
  document: Config;
  profiles: Record<string, Profile>;
}
export interface Config {
  version: number;
  defaults: Record<string, unknown>;
  workers: Record<string, Record<string, unknown>>;
  tasks: Record<string, Record<string, unknown>>;
  task_defaults?: Record<string, unknown>;
  task_templates?: Record<string, Record<string, unknown>>;
}

export interface WorkerConfig {
  id: string;
  family_id: string;
  parent_id: string | null;
  cloned_from: string | null;
  name: string;
  profile: string;
  factory_revision_id: string;
  document: WorkerDocument;
  created_at: number;
}
export interface WorkerDocument {
  settings: Record<string, unknown>;
  tasks: Record<
    string,
    { model?: string | null; additional_system_prompt?: string }
  >;
}
