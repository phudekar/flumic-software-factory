import { createContext, useContext, useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";

const ProjectContext = createContext<string | null>(null);
export function ProjectProvider({
  id,
  children,
}: {
  id: string;
  children: React.ReactNode;
}) {
  const client = useQueryClient();
  useEffect(
    () => () => {
      void client.cancelQueries({ queryKey: ["project", id] });
    },
    [client, id],
  );
  return (
    <ProjectContext.Provider value={id}>{children}</ProjectContext.Provider>
  );
}
export function useProjectId() {
  const id = useContext(ProjectContext);
  if (!id) throw new Error("Choose a project first.");
  return id;
}
export function projectPath(id: string, path: string) {
  return `/projects/${encodeURIComponent(id)}${path}`;
}
export function useProjectData<T>(path: string, enabled = true) {
  const id = useProjectId();
  return useQuery<T>({
    queryKey: ["project", id, path],
    queryFn: ({ signal }) =>
      api<T>(projectPath(id, path), undefined, { signal }),
    enabled,
  });
}

// A component may finish an already accepted write after navigation. Only its
// captured project's cache is invalidated; no late result repopulates a new form.
export function useScopedWrite() {
  const id = useProjectId(),
    client = useQueryClient();
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  return {
    alive,
    write: async <T,>(path: string, body: unknown, method?: "PUT") => {
      const result = await api<T>(projectPath(id, path), body, { method });
      await client.invalidateQueries({ queryKey: ["project", id] });
      return result;
    },
  };
}
