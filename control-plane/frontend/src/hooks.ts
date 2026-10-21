import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
export function useData<T>(key: string, path: string) {
  return useQuery<T>({ queryKey: [key, path], queryFn: () => api<T>(path) });
}
