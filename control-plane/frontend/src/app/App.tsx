import { ProjectNavigation } from "../components/project-navigation";
import flumicIcon from "../assets/flumic.svg";
import { useQueryClient } from "@tanstack/react-query";
import {
  Box,
  ChevronRight,
  GitBranch,
  LoaderCircle,
  LogOut,
  RefreshCw,
  Server,
} from "lucide-react";
import { useEffect, useState } from "react";
import {
  Link,
  NavLink,
  Navigate,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { api, setCsrf } from "../api";
import {
  ProjectPages,
  ProjectsPage,
  ProjectSelector,
} from "../features/projects";
import { Empty } from "../components/ui";
import { Login } from "../features/auth";
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
            <span className="nav-label">INFRASTRUCTURE</span>
            <nav aria-label="Infrastructure">
              {[
                ["/workers", "Shared workers", Server],
                ["/runtime-targets", "Runtime targets", Box],
              ].map(([path, label, Icon]) => {
                const Glyph = Icon as typeof Box;
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
            <Route path="/" element={<Navigate to="/projects" replace />} />
            <Route path="/runtime-targets" element={<RuntimeTargetsPage />} />
            <Route path="/workers" element={<WorkersPage />} />
            <Route path="/workers/:id" element={<WorkerPage />} />
            {[
              "/workflows",
              "/runs",
              "/runs/:id",
              "/history",
              "/usage",
              "/config",
              "/prompts",
            ].map((path) => (
              <Route
                key={path}
                path={path}
                element={
                  <>
                    <Empty
                      title="Choose a project"
                      detail="Workflows, runs, model history and configuration now belong to a project. Select your project to continue."
                    />
                    <Link to="/projects">Open projects</Link>
                  </>
                }
              />
            ))}
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
