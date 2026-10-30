import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
export function useData<T>(key: string, path: string) {
  return useQuery<T>({ queryKey: [key, path], queryFn: () => api<T>(path) });
}

// Bring an explicitly opened setup form into view and restore the trigger on Cancel.
export function useSetupForm(open: boolean, draftKey = "new") {
  const ref = useRef<HTMLFormElement>(null);
  useEffect(() => {
    if (!open || !ref.current) return;
    const trigger = document.activeElement;
    ref.current.scrollIntoView?.({ block: "start", behavior: "smooth" });
    ref.current
      .querySelector<HTMLElement>("input, select, textarea")
      ?.focus({ preventScroll: true });
    return () => {
      if (trigger instanceof HTMLElement && trigger.isConnected)
        trigger.focus({ preventScroll: true });
    };
  }, [open, draftKey]);
  return ref;
}
