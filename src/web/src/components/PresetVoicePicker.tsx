import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { errorMessage } from "../api/client";
import type { AudioHarnessApi, EngineAsset } from "../api/types";

interface Props {
  api: AudioHarnessApi;
  engine: EngineAsset | undefined;
  onAdd: (voice: string) => void;
}

export function PresetVoicePicker({ api, engine, onAdd }: Props) {
  const [value, setValue] = useState("");
  const voices = useQuery({
    queryKey: ["engine-preset-voices", engine?.id, engine?.revision],
    queryFn: ({ signal }) => api.listEnginePresetVoices(engine!.id, signal),
    enabled: Boolean(engine),
    retry: false,
    staleTime: 60_000,
  });
  const available = useMemo(() => (voices.data?.items ?? []).filter(
    (item) => item.available_on_instance && item.supports_duplex,
  ), [voices.data]);
  const matches = useMemo(() => {
    const prefix = value.trim().toLocaleLowerCase();
    return prefix ? available.filter((item) => item.name.toLocaleLowerCase().startsWith(prefix)) : available;
  }, [available, value]);
  const add = (next = value) => {
    const trimmed = next.trim();
    if (!trimmed) return;
    onAdd(trimmed);
    setValue("");
  };
  const status = !engine ? "先选择 TTS 引擎。"
    : voices.isFetching ? "正在读取可用 Duplex 音色…"
      : voices.isError ? `${errorMessage(voices.error, "音色目录读取失败。")} 仍可手工输入。`
        : voices.data && available.length === 0 ? "当前实例没有可用的 Duplex 音色；仍可手工输入。"
          : voices.data ? `已读取 ${available.length} 个可用 Duplex 音色。` : "";

  return <label><span>允许音色</span><div className="tag-editor preset-voice-picker"><div className="preset-voice-input"><input aria-label="输入或搜索音色" disabled={!engine} value={value} onChange={(event) => setValue(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); add(); } }} placeholder={engine ? "输入音色名称以按前缀筛选" : "先选择 TTS 引擎"} />{engine && matches.length > 0 && <ul role="listbox" aria-label="可用 Duplex 音色">{matches.map((item) => <li key={item.voice_id} role="option" aria-selected={false}><button type="button" onClick={() => add(item.name)}><span>{item.name}</span>{item.language && <small>{item.language}</small>}</button></li>)}</ul>}</div><button type="button" className="secondary-button" disabled={!engine || !value.trim()} onClick={() => add()}>添加</button></div><small role="status" aria-live="polite">{status}</small></label>;
}
