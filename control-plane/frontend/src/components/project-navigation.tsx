import {
  Activity,
  Workflow,
  MessageSquareText,
  Layers3,
  GitBranch,
  KeyRound,
  Settings2,
  Users,
  Radio,
} from "lucide-react";
import { NavLink } from "react-router-dom";
import { projectPath } from "../project-context";
export function ProjectNavigation({ project }: { project: string }) {
  const groups = [
    {
      label: "PROJECT WORKSPACE",
      links: [
        ["overview", "Overview", Activity],
        ["workflows", "Workflows", Workflow],
        ["runs", "Workflow runs", Workflow],
        ["history", "Model history", MessageSquareText],
        ["usage", "Usage & cost", Layers3],
      ],
    },
    {
      label: "PROJECT SETUP",
      links: [
        ["repositories", "Repositories", GitBranch],
        ["credentials", "Credentials", KeyRound],
        ["triggers", "Repository triggers", Radio],
        ["config", "Configuration", Settings2],
        ["pools", "Shared worker pools", Users],
      ],
    },
  ] as const;
  return (
    <nav aria-label="Project navigation">
      {groups.map((group) => (
        <div key={group.label}>
          <span className="nav-label">{group.label}</span>
          {group.links.map(([path, label, Icon]) => (
            <NavLink key={path} to={projectPath(project, "/" + path)}>
              <Icon size={17} />
              {label}
            </NavLink>
          ))}
        </div>
      ))}
    </nav>
  );
}
