import { useQuery } from "@tanstack/react-query";

import type { AudioHarnessApi } from "../../api/types";

export function useRuntimeOverview(api: AudioHarnessApi) {
  return useQuery({
    queryKey: ["runtime", "overview"],
    queryFn: () => api.getRuntimeOverview(),
    refetchInterval: () => (document.visibilityState === "visible" ? 15_000 : false),
  });
}
