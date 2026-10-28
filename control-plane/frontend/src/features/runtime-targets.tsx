import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { useData } from "../hooks";
import { Failure, PageTitle, Section } from "../components/ui";

interface Target {
  id: string;
  name: string;
  runtime: string;
  availability: string;
  capabilities: {
    create: boolean;
    shared: boolean;
    legacy: boolean;
    container_actions: boolean;
    log_stream: string;
  };
  limitations: string[];
}
// Allowlist before caching: operator credentials and kubeconfig paths never enter browser state.
async function targets(): Promise<Target[]> {
  const rows = await api<Record<string, unknown>[]>("/targets");
  return rows.map((row) => {
    const caps = (row.capabilities ?? {}) as Record<string, unknown>;
    return {
      id: String(row.id),
      name: String(row.name),
      runtime: String(row.runtime),
      availability:
        typeof row.availability === "string" ? row.availability : "configured",
      capabilities: {
        create: caps.create === true,
        shared: caps.shared === true,
        legacy: caps.legacy === true,
        container_actions: caps.container_actions === true,
        log_stream:
          typeof caps.log_stream === "string" ? caps.log_stream : "split",
      },
      limitations: Array.isArray(row.limitations)
        ? row.limitations.filter((v): v is string => typeof v === "string")
        : [],
    };
  });
}
function useTargets() {
  return useQuery({ queryKey: ["runtime-targets"], queryFn: targets });
}
export function RuntimeTargetsPage() {
  const query = useTargets();
  return (
    <>
      <PageTitle
        eyebrow="EXECUTION INFRASTRUCTURE"
        title="Runtime targets"
        description="Operator-configured deployment destinations. Configured does not imply connectivity or worker registration."
      />
      <Failure error={query.error} />
      {query.data?.map((target) => (
        <Section key={target.id} title={target.name}>
          <div className="editor-body">
            <p>
              {target.id} · {target.runtime} · {target.availability}
            </p>
            <p>
              Shared workers:{" "}
              {target.capabilities.shared ? "supported" : "unavailable"} ·
              Container actions:{" "}
              {target.capabilities.container_actions
                ? "qualified"
                : "unavailable"}
            </p>
            {target.limitations.map((value) => (
              <p key={value}>{value}</p>
            ))}
          </div>
        </Section>
      ))}
    </>
  );
}
export function SharedWorkerForm({ close }: { close: () => void }) {
  const query = useTargets();
  const pools = useData<{ id: string; name: string }[]>(
    "worker-pools",
    "/worker-pools",
  );
  const client = useQueryClient();
  const [targetId, setTargetId] = useState(""),
    [poolId, setPoolId] = useState(""),
    [name, setName] = useState("");
  const [pending, setPending] = useState(false),
    [error, setError] = useState("");
  const selected = query.data?.find(
    (t) => t.id === (targetId || query.data?.[0]?.id),
  );
  const pool = poolId || pools.data?.[0]?.id || "";
  const available =
    !!selected &&
    selected.availability !== "unavailable" &&
    selected.capabilities.create &&
    selected.capabilities.shared;
  return (
    <div className="modal-backdrop">
      <form
        className="modal panel"
        onSubmit={async (event) => {
          event.preventDefault();
          if (!available || !pool) return;
          setPending(true);
          try {
            await api(
              "/worker-pools/" + encodeURIComponent(pool) + "/workers",
              { name, target_id: selected.id },
            );
            await client.invalidateQueries({ queryKey: ["workers"] });
            close();
          } catch (e) {
            setError((e as Error).message);
          } finally {
            setPending(false);
          }
        }}
      >
        <div className="panel-heading">
          <h2>Add shared worker</h2>
          <button
            type="button"
            aria-label="Close shared worker form"
            onClick={close}
          >
            Close
          </button>
        </div>
        <Failure error={query.error || pools.error || error} />
        <label>
          Worker name
          <input
            required
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label>
          Worker pool
          <select
            value={pool}
            onChange={(event) => setPoolId(event.target.value)}
          >
            {pools.data?.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Deployment target
          <select
            value={selected?.id ?? ""}
            onChange={(event) => setTargetId(event.target.value)}
          >
            {query.data?.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name} ({t.runtime})
              </option>
            ))}
          </select>
        </label>
        {selected && !available && (
          <p>This target is unavailable for shared worker creation.</p>
        )}
        {selected && !selected.capabilities.container_actions && (
          <p>
            Built-in stages are available; container actions require operator
            qualification.
          </p>
        )}
        <p>
          Workers are created stopped. Runtime readiness and Hatchet
          registration are observed separately.
        </p>
        <button className="primary" disabled={!available || !pool || pending}>
          Create shared worker
        </button>
      </form>
    </div>
  );
}
