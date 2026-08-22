import { useEffect, useState } from "react";

import type { AudioHarnessApi } from "../../api/types";
import { errorMessage } from "../../api/client";
export type VoiceprintInterfaceStatus = "unbound" | "loading" | "available" | "unavailable" | "error";

export interface VoiceAgentWorkflowInterface {
  status: VoiceprintInterfaceStatus;
  error: string | null;
}

interface LoadedInterface extends VoiceAgentWorkflowInterface { workflowId: string }

export function useVoiceAgentWorkflowInterface(api: AudioHarnessApi, workflowId: string): VoiceAgentWorkflowInterface {
  const [loaded, setLoaded] = useState<LoadedInterface>({ workflowId: "", status: "unbound", error: null });

  useEffect(() => {
    if (!workflowId) return;
    const controller = new AbortController();
    void api.getActiveAudioWorkflow(workflowId, controller.signal).then((revision) => {
      const available = revision.document.outputs.some((output) => output.name === "speaker_filter");
      setLoaded({ workflowId, status: available ? "available" : "unavailable", error: null });
    }).catch((reason) => {
      if (!controller.signal.aborted) {
        setLoaded({ workflowId, status: "error", error: errorMessage(reason, "语音工作流接口读取失败。") });
      }
    });
    return () => controller.abort();
  }, [api, workflowId]);

  if (!workflowId) return { status: "unbound", error: null };
  if (loaded.workflowId !== workflowId) return { status: "loading", error: null };
  return loaded;
}
