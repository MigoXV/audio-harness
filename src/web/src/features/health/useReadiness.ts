import { useQuery } from "@tanstack/react-query";

import type { AudioHarnessApi } from "../../api/types";

export function useReadiness(api: AudioHarnessApi) {
  return useQuery({
    queryKey: ["health", "readiness"],
    queryFn: () => api.getReadiness(),
    refetchInterval: () => (document.visibilityState === "visible" ? 15_000 : false),
  });
}
