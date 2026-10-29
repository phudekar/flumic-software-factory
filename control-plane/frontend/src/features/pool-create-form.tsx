import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useSetupForm } from "../hooks";
import {
  useProjectData,
  useProjectId,
  useScopedWrite,
} from "../project-context";
import { Failure, Section } from "../components/ui";

type Role = "simple" | "developer";
interface Catalog {
  actions: { id: string; role: Role | null }[];
}
interface Configuration {
  document: {
    profiles: Partial<
      Record<Role, { settings?: { hatchet_tls_strategy?: string } }>
    >;
  };
}

export function PoolCreateForm({
  onCancel,
  onCreated,
}: {
  onCancel: () => void;
  onCreated: (name: string) => void;
}) {
  const catalog = useProjectData<Catalog>("/workflow-catalog");
  const configuration = useProjectData<Configuration>("/factory-config");
  const project = useProjectId(),
    client = useQueryClient();
  const { alive } = useScopedWrite();
  const formRef = useSetupForm(true);
  const [name, setName] = useState(""),
    [role, setRole] = useState<Role>("simple");
  const [actions, setActions] = useState<string[]>([]);
  const [tls, setTls] = useState("");
  const [normal, setNormal] = useState(2),
    [durable, setDurable] = useState(16);
  const [executors, setExecutors] = useState("builtin@1");
  const [pending, setPending] = useState(false),
    [error, setError] = useState("");
  const available = (catalog.data?.actions ?? []).filter(
    (a) => a.role === role || a.role === null,
  );
  useEffect(() => {
    setActions(
      (catalog.data?.actions ?? [])
        .filter((a) => a.role === role || a.role === null)
        .map((a) => a.id),
    );
  }, [catalog.data, role]);
  useEffect(() => {
    setTls(
      configuration.data?.document?.profiles?.[role]?.settings
        ?.hatchet_tls_strategy ?? "",
    );
  }, [configuration.data, role]);
  return (
    <Section title="Create shared worker pool">
      <Failure error={error || catalog.error || configuration.error} />
      <form
        ref={formRef}
        className="editor-body"
        onSubmit={async (e) => {
          e.preventDefault();
          setPending(true);
          setError("");
          try {
            await api("/worker-pools", {
              name: name.trim(),
              role,
              normal_slots: normal,
              durable_slots: durable,
              actions,
              executors: executors
                .split(",")
                .map((v) => v.trim())
                .filter(Boolean),
              hatchet_tls_strategy: tls,
            });
            await client.invalidateQueries({
              queryKey: ["project", project, "/worker-pools"],
            });
            await client.invalidateQueries({ queryKey: ["worker-pools"] });
            if (alive.current) onCreated(name.trim());
          } catch (e) {
            if (alive.current) setError((e as Error).message);
          } finally {
            if (alive.current) setPending(false);
          }
        }}
      >
        <p>
          Pools are shared workspace infrastructure. Creating one does not
          authorize a project or start workers.
        </p>
        <fieldset className="pool-form-fields" disabled={pending}>
          <label>
            Pool name
            <input
              required
              pattern="[a-z][a-z0-9-]{0,99}"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="walkthrough-simple"
            />
          </label>
          <label>
            Worker role
            <select
              value={role}
              onChange={(e) => setRole(e.target.value as Role)}
            >
              <option value="simple">Simple — planning and review</option>
              <option value="developer">Developer — code changes</option>
            </select>
          </label>
          <p>
            Available built-in actions for this role are selected initially. The
            worker image must support every advertised version.
          </p>
          <fieldset className="setup-advanced">
            <legend>Action versions</legend>
            {catalog.isPending && <p>Loading actions…</p>}
            {available.map((a) => (
              <label className="checkbox-label" key={a.id}>
                <input
                  type="checkbox"
                  checked={actions.includes(a.id)}
                  onChange={(e) =>
                    setActions(
                      e.target.checked
                        ? [...actions, a.id]
                        : actions.filter((v) => v !== a.id),
                    )
                  }
                />
                {a.id}
              </label>
            ))}
          </fieldset>
          <label>
            Normal slots per worker
            <input
              type="number"
              required
              min={1}
              max={8}
              value={normal}
              onChange={(e) => setNormal(Number(e.target.value))}
            />
          </label>
          <label>
            Durable slots per worker
            <input
              type="number"
              required
              min={1}
              max={16}
              value={durable}
              onChange={(e) => setDurable(Number(e.target.value))}
            />
          </label>
          <p>
            Slots bound concurrent work per worker, rather than the number of
            workers in the pool.
          </p>
          <label>
            Hatchet transport
            <select
              required
              value={tls}
              onChange={(e) => setTls(e.target.value)}
            >
              <option value="">Choose the deployment transport</option>
              <option value="tls">TLS</option>
              <option value="mtls">Mutual TLS</option>
              <option value="none">No TLS — local deployment only</option>
            </select>
          </label>
          <p>
            Prefilled from the saved role profile when available. It must match
            your Hatchet deployment.
          </p>
          <details>
            <summary>Advanced: executor versions</summary>
            <label>
              Executor versions
              <input
                required
                value={executors}
                onChange={(e) => setExecutors(e.target.value)}
              />
            </label>
            <p>
              Comma-separated installed versions, such as builtin@1 or docker@1.
              Advertising an executor does not install it.
            </p>
          </details>
          <button
            className="primary"
            disabled={
              pending || catalog.isPending || !!catalog.error || !actions.length
            }
          >
            Create pool
          </button>
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
        </fieldset>
      </form>
    </Section>
  );
}
