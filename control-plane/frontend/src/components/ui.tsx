import { localTimestamp } from "./project-format";
import { ChevronRight, Layers3, Workflow } from "lucide-react";
import React from "react";
import { Link } from "react-router-dom";
import type { Run } from "../api";

export const count = (n: number | null | undefined) =>
  n == null
    ? "—"
    : new Intl.NumberFormat("en-US", {
        notation: "compact",
        maximumFractionDigits: 1,
      }).format(n);

export const money = (n: number | null | undefined) =>
  n == null
    ? "Unknown"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        minimumFractionDigits: 4,
      }).format(n);

export const date = (n: string | number | null | undefined) =>
  localTimestamp(n);

export function Status({ value }: { value: string }) {
  return (
    <span className={"status " + value.toLowerCase()}>
      <i />
      {value.replaceAll("_", " ").toLowerCase()}
    </span>
  );
}

export function Empty({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="empty">
      <Layers3 size={26} />
      <h3>{title}</h3>
      <p>{detail}</p>
    </div>
  );
}

export function Failure({ error }: { error: unknown }) {
  return error ? (
    <div role="alert" className="notice error">
      {error instanceof Error ? error.message : String(error)}
    </div>
  ) : null;
}

export function PageTitle({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="page-title">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action}
    </div>
  );
}

export function Section({
  title,
  aside,
  children,
}: {
  title: string;
  aside?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function Metric({
  label,
  value,
  detail,
  icon,
}: {
  label: string;
  value: string;
  detail: string;
  icon: React.ReactNode;
}) {
  return (
    <div className="metric">
      <div>
        {label}
        <span>{icon}</span>
      </div>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

export function RunTable({
  items,
  loading = false,
}: {
  items: Run[];
  loading?: boolean;
}) {
  return !items.length ? (
    <Empty
      title={loading ? "Loading workflows…" : "No workflow runs to show"}
      detail={
        loading
          ? "Reading execution state from Hatchet."
          : "Run history will appear when Hatchet is connected and work has been executed."
      }
    />
  ) : (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Run</th>
            <th>Status</th>
            <th>Workflow</th>
            <th>Created</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {items.map((r) => (
            <tr key={r.id}>
              <td>
                <Link className="row-link" to={"/runs/" + r.id}>
                  <span className="resource-icon">
                    <Workflow size={15} />
                  </span>
                  {r.name || r.id.slice(0, 16)}
                </Link>
              </td>
              <td>
                <Status value={r.status ?? "unknown"} />
              </td>
              <td className="muted">{r.workflow || "—"}</td>
              <td className="muted mono">{date(r.created_at)}</td>
              <td>
                <Link title="Open run" to={"/runs/" + r.id}>
                  <ChevronRight size={16} />
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
