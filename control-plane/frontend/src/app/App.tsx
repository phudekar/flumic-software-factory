import flumicIcon from "../assets/flumic.svg";
import { useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Box,
  ChevronRight,
  Code2,
  FileSliders,
  GitBranch,
  Layers3,
  LoaderCircle,
  LogOut,
  MessageSquareText,
  RefreshCw,
  Server,
  Workflow,
} from "lucide-react";
import { useEffect, useState } from "react";
import { Link, NavLink, Route, Routes } from "react-router-dom";
import { api, setCsrf } from "../api";
import { Empty } from "../components/ui";
import { Login } from "../features/auth";
import { Configuration } from "../features/configuration";
import { History } from "../features/history";
import { Overview } from "../features/overview";
import { RunPage, RunsPage } from "../features/runs";
import { UsagePage } from "../features/usage";
import { WorkflowsPage } from "../features/workflows";
import { WorkerPage, WorkersPage } from "../features/workers";

export function App() {
  const queryClient = useQueryClient();
  const [signedIn, setSignedIn] = useState(false);
  const [checking, setChecking] = useState(true);
  useEffect(() => {
    const out = () => setSignedIn(false);
    window.addEventListener("factory:unauthorized", out);
    api<{ csrf: string }>("/auth/session")
      .then((r) => {
        setCsrf(r.csrf);
        setSignedIn(true);
      })
      .catch(() => {})
      .finally(() => setChecking(false));
    return () => window.removeEventListener("factory:unauthorized", out);
  }, []);
  if (checking)
    return (
      <div className="boot">
        <LoaderCircle className="spin" /> Connecting to your factory
      </div>
    );
  if (!signedIn)
    return (
      <Login
        onLogin={() => {
          queryClient.clear();
          setSignedIn(true);
        }}
      />
    );
  return (
    <div className="shell">
      <aside className="sidebar">
        <Link className="brand" to="/">
          <span className="brand-icon">
            <img src={flumicIcon} alt="" aria-hidden="true" />
          </span>
          <div>
            Flumic AI<span>CONTROL PLANE</span>
          </div>
        </Link>
        <div className="workspace">
          <span className="workspace-symbol">FA</span>
          <div>
            Flumic AI<small>Local workspace</small>
          </div>
          <ChevronRight size={14} />
        </div>
        <span className="nav-label">WORKSPACE</span>
        <nav>
          {[
            ["/", "Overview", Activity],
            ["/workers", "Workers", Server],
            ["/workflows", "Workflows", Workflow],
            ["/runs", "Workflow runs", Workflow],
            ["/history", "Model history", MessageSquareText],
            ["/usage", "Usage & cost", Layers3],
            ["/config", "Configuration", FileSliders],
            ["/prompts", "Worker prompts", Code2],
          ].map(([path, label, Icon]) => {
            const Glyph = Icon as typeof Activity;
            return (
              <NavLink key={String(path)} to={String(path)} end={path === "/"}>
                <Glyph size={17} />
                {String(label)}
              </NavLink>
            );
          })}
        </nav>
        <div className="sidebar-bottom">
          <div className="target-note">
            <span className="target-dot" />
            <div>
              Docker target<small>local-docker</small>
            </div>
            <Box size={16} />
          </div>
          <p>
            Runtime adapters keep your factory ready for future Kubernetes
            targets.
          </p>
          <button
            className="user-button"
            onClick={async () => {
              await api("/auth/logout", {});
              setSignedIn(false);
              queryClient.clear();
            }}
          >
            <span className="avatar">A</span>
            <div>
              Administrator<small>Local session</small>
            </div>
            <LogOut size={16} />
          </button>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <span>
            Workspace <ChevronRight size={12} />{" "}
            <strong>Flumic AI</strong>
          </span>
          <div>
            <span className="environment">
              <GitBranch size={12} /> development
            </span>
            <button
              className="icon-button"
              title="Refresh all data"
              aria-label="Refresh all data"
              onClick={() => queryClient.invalidateQueries()}
            >
              <RefreshCw size={15} />
            </button>
          </div>
        </header>
        <div className="content">
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/workers" element={<WorkersPage />} />
            <Route path="/workers/:id" element={<WorkerPage />} />
            <Route path="/workflows" element={<WorkflowsPage />} />
            <Route path="/runs" element={<RunsPage />} />
            <Route path="/runs/:id" element={<RunPage />} />
            <Route path="/history" element={<History />} />
            <Route path="/usage" element={<UsagePage />} />
            <Route path="/config" element={<Configuration />} />
            <Route path="/prompts" element={<Configuration prompts />} />
            <Route
              path="*"
              element={
                <Empty
                  title="Page not found"
                  detail="Choose a view from the navigation."
                />
              }
            />
          </Routes>
        </div>
      </main>
    </div>
  );
}
