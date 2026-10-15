import { useCallback, useEffect, useRef } from "react";

export function isCompactWorkbench(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(max-width: 1100px)").matches;
}

export function useInspectorFocus(selectedId: string | null) {
  const previousSelection = useRef<string | null>(null);
  useEffect(() => {
    if (selectedId) {
      previousSelection.current = selectedId;
      document.querySelector<HTMLElement>(".audio-inspector .icon-button")?.focus();
    } else if (previousSelection.current) {
      document.querySelectorAll<HTMLElement>(".react-flow__node").forEach((node) => {
        if (node.dataset.id === previousSelection.current) node.focus();
      });
      previousSelection.current = null;
    }
  }, [selectedId]);
  return useCallback(() => {
    document.querySelector<HTMLElement>(".workbench-sidebar > header button")?.focus();
    previousSelection.current = null;
  }, []);
}
