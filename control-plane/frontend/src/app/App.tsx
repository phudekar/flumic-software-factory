import { ProjectNavigation } from "../components/project-navigation";
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
import { Link, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { api, setCsrf } from "../api";
import {
  ProjectPages,
  ProjectsPage,
  ProjectSelector,
} from "../features/projects";
import { Empty } from "../components/ui";
import { Login } from "../features/auth";
import { Configuration } from "../features/configuration";
import { History } from "../features/history";
import { Overview } from "../features/overview";
import { RunPage, RunsPage } from "../features/runs";
import { UsagePage } from "../features/usage";
import { WorkflowsPage } from "../features/workflows";
import { WorkerPage, WorkersPage } from "../features/workers";

import { RuntimeTargetsPage } from "../features/runtime-targets";

export function App() {
  const queryClient = useQueryClient();
  const location = useLocation();
  const selectedProject = /^\/projects\/([^/]+)(?:\/|$)/.exec(
    location.pathname,
  )?.[1];
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
        <Link
          className="brand"
          to={selectedProject ? `/projects/${selectedProject}/overview` : "/"}
        >
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
        <ProjectSelector />
        {selectedProject ? (
          <>
            <Link className="workspace-return" to="/projects">
              ← All projects
            </Link>
            <ProjectNavigation project={selectedProject} />
          </>
        ) : (
          <>
            <nav aria-label="Projects">
              <NavLink to="/projects">
                <Box size={17} />
                Projects
              </NavLink>
            </nav>
            <span className="nav-label">LEGACY VIEWS & INFRASTRUCTURE</span>
            <nav aria-label="Default legacy project">
              {[
                ["/", "Overview", Activity],
                ["/workers", "Workers", Server],
                ["/runtime-targets", "Runtime targets", Box],
                ["/workflows", "Workflows", Workflow],
                ["/runs", "Workflow runs", Workflow],
                ["/history", "Model history", MessageSquareText],
                ["/usage", "Usage & cost", Layers3],
                ["/config", "Configuration", FileSliders],
                ["/prompts", "Worker prompts", Code2],
              ].map(([path, label, Icon]) => {
                const Glyph = Icon as typeof Activity;
                return (
                  <NavLink
                    key={String(path)}
                    to={String(path)}
                    end={path === "/"}
                  >
                    <Glyph size={17} />
                    {String(label)}
                  </NavLink>
                );
              })}
            </nav>
          </>
        )}
        <div className="sidebar-bottom">
          {!selectedProject && (
            <>
              <div className="target-note">
                <span className="target-dot" />
                <div>
                  Runtime targets<small>Operator configured</small>
                </div>
                <Box size={16} />
              </div>
              <p>
                Shared workers run on configured Docker and Kubernetes targets.
              </p>
            </>
          )}
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
            <strong>
              {selectedProject ? "Project workspace" : "Flumic AI"}
            </strong>
          </span>
          <div>
            <span className="environment">
              <GitBranch size={12} /> development
            </span>
            <button
              className="icon-button"
              title={
                selectedProject ? "Refresh project data" : "Refresh all data"
              }
              aria-label={
                selectedProject ? "Refresh project data" : "Refresh all data"
              }
              onClick={() =>
                queryClient.invalidateQueries(
                  selectedProject
                    ? { queryKey: ["project", selectedProject] }
                    : undefined,
                )
              }
            >
              <RefreshCw size={15} />
            </button>
          </div>
        </header>
        <div className="content">
          <Routes>
            <Route path="/projects" element={<ProjectsPage />} />
            <Route path="/projects/:projectId/*" element={<ProjectPages />} />
            <Route path="/" element={<Overview />} />
            <Route path="/runtime-targets" element={<RuntimeTargetsPage />} />
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
