import { useEffect, useMemo, useRef, useState, type DragEvent, type ReactNode } from "react";

import type { AudioHarnessApi, EngineAsset, VadAudioPreview, VectorInfrastructure, VectorPoint, VectorSpace, VectorSpeakerMatch } from "../api/types";
import { errorMessage } from "../api/client";
import "../components/vectorWorkspace.css";

type Tab = "register" | "search" | "query" | "records";
type EmbeddingConfig = { engine_id: string; algorithm: string; normalize: boolean };
type VadPreviewState = { file: File; durationMs: number };
type VadPostprocessing = { processor: "firered.stream_vad"; version: 1; parameters: Record<string, number> };

const featureTemplateVectors = '[{"name":"eres2netv2","size":192,"distance":"cosine"}]';
const defaultVadPostprocessing: VadPostprocessing = { processor: "firered.stream_vad", version: 1, parameters: { smooth_window_size: 5, speech_threshold: 0.5, pad_start_frame: 5, min_speech_frame: 8, max_speech_frame: 2000, min_silence_frame: 20 } };

function currentLocalTime(): string {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function formatDuration(durationMs: number): string {
  const seconds = Math.max(0, Math.floor(durationMs / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function previewFile(value: VadAudioPreview): File {
  const binary = atob(value.audio_base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return new File([bytes], "vad-preview.wav", { type: value.content_type });
}

function AudioPlayer({ blob, label }: { blob: Blob | null; label: string }) {
  const ref = useRef<HTMLAudioElement>(null);
  useEffect(() => {
    const player = ref.current;
    if (!blob || !player) return;
    const url = URL.createObjectURL(blob);
    player.src = url;
    return () => {
      player.removeAttribute("src");
      URL.revokeObjectURL(url);
    };
  }, [blob]);
  return blob ? <audio ref={ref} aria-label={label} controls preload="metadata" /> : null;
}

function AudioInput({ file, onChange, onRecordingDeviceName, vadControls }: {
  file: File | null;
  onChange: (file: File) => void;
  onRecordingDeviceName: (name: string) => void;
  vadControls: ReactNode;
}) {
  const [source, setSource] = useState<"upload" | "record">("upload");
  const [recording, setRecording] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [recordedDurationMs, setRecordedDurationMs] = useState<number | null>(null);
  const [recordingError, setRecordingError] = useState("");
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const startedAtRef = useRef(0);
  const elapsedRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const mountedRef = useRef(true);

  const clearTimer = () => {
    if (timerRef.current !== null) clearInterval(timerRef.current);
    timerRef.current = null;
  };
  const stopStream = () => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  };
  const chooseFile = (next: File) => {
    if (next.type && !next.type.startsWith("audio/")) {
      setRecordingError("请选择音频文件。");
      return;
    }
    setRecordingError("");
    setRecordedDurationMs(null);
    setSource("upload");
    onChange(next);
  };
  const start = async () => {
    setRecordingError("");
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setRecordingError("当前浏览器不支持麦克风录音。");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (!mountedRef.current) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      const deviceName = stream.getAudioTracks()[0]?.label.trim();
      if (deviceName) onRecordingDeviceName(deviceName);
      streamRef.current = stream;
      const next = new MediaRecorder(stream);
      recorderRef.current = next;
      chunksRef.current = [];
      elapsedRef.current = 0;
      startedAtRef.current = performance.now();
      next.ondataavailable = (event) => {
        if (event.data.size) chunksRef.current.push(event.data);
      };
      next.onstop = () => {
        clearTimer();
        stopStream();
        recorderRef.current = null;
        if (!mountedRef.current) return;
        const duration = Math.max(
          elapsedRef.current,
          performance.now() - startedAtRef.current,
        );
        const mimeType = next.mimeType || "audio/webm";
        const blob = new Blob(chunksRef.current, { type: mimeType });
        chunksRef.current = [];
        setRecording(false);
        if (!blob.size) {
          setRecordingError("没有录到有效音频，请重试。");
          return;
        }
        setRecordedDurationMs(duration);
        setSource("record");
        onChange(new File([blob], `recording-${Date.now()}.webm`, { type: mimeType }));
      };
      next.start();
      setElapsedMs(0);
      setRecording(true);
      timerRef.current = setInterval(() => {
        const duration = performance.now() - startedAtRef.current;
        elapsedRef.current = duration;
        setElapsedMs(duration);
      }, 250);
    } catch (reason) {
      clearTimer();
      stopStream();
      recorderRef.current = null;
      setRecording(false);
      setRecordingError(
        reason instanceof DOMException && reason.name === "NotAllowedError"
          ? "麦克风权限被拒绝，请在浏览器设置中允许访问。"
          : errorMessage(reason, "无法访问麦克风，请检查浏览器权限。"),
      );
    }
  };
  const stopRecording = () => {
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") recorder.stop();
  };
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      clearTimer();
      const recorder = recorderRef.current;
      if (recorder && recorder.state !== "inactive") {
        recorder.onstop = null;
        recorder.stop();
      }
      stopStream();
    };
  }, []);
  const drop = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    setDragging(false);
    if (!recording && event.dataTransfer.files[0]) chooseFile(event.dataTransfer.files[0]);
  };
  return <section className="vector-audio-input" aria-label="输入音频">
    <nav className="vector-audio-source-tabs" aria-label="音频来源"><span>音频来源</span>
      <button type="button" className={source === "upload" ? "active" : ""} disabled={recording && source !== "upload"} onClick={() => setSource("upload")}>上传音频</button>
      <button type="button" className={source === "record" ? "active" : ""} disabled={recording && source !== "record"} onClick={() => setSource("record")}>现场录音</button>
    </nav>
    <div className="vector-audio-source-frame">
      {source === "upload" ? <label className={`vector-audio-dropzone ${dragging ? "dragging" : ""}`} onDragEnter={(event) => { event.preventDefault(); if (!recording) setDragging(true); }} onDragOver={(event) => event.preventDefault()} onDragLeave={() => setDragging(false)} onDrop={drop}>
        <input type="file" accept="audio/*" disabled={recording} onChange={(event) => { const next = event.target.files?.[0]; if (next) chooseFile(next); event.currentTarget.value = ""; }} />
        <div className="vector-audio-source-copy"><span><strong>{file ? "更换音频" : "拖放音频到这里"}</strong><small>{file ? "点击选择其他文件" : "或点击选择音频文件"}</small></span></div>
        <span className="secondary-button">选择文件</span>
      </label> : <div className="vector-audio-recorder">
        <div className="vector-audio-source-copy"><span className="vector-audio-icon" aria-hidden="true">●</span><span><strong>使用浏览器麦克风录音</strong><small>录音结束后可试听，并可继续切换到上传音频。</small></span></div>
        <div className="vector-recorder-controls">
          <button type="button" className={recording ? "secondary-button recording" : "secondary-button"} onClick={() => recording ? stopRecording() : void start()}>
            {recording ? "停止录音" : "开始录音"}
          </button>
          <span className={recording ? "recording-time active" : "recording-time"}><i aria-hidden="true" />{formatDuration(recording ? elapsedMs : recordedDurationMs ?? 0)}</span>
        </div>
      </div>}
      {file && <div className="vector-audio-selected"><span><strong>原始音频 · {file.name}</strong><small>{(file.size / 1024).toFixed(1)} KB{recordedDurationMs !== null ? ` · 录音 ${formatDuration(recordedDurationMs)}` : ""}</small></span><AudioPlayer blob={file} label="原始音频" /></div>}
      {vadControls}
    </div>
    {recordingError && <div className="inline-error" role="alert">{recordingError}</div>}
  </section>;
}

export function VectorWorkspacePage({ api, refreshVersion }: { api: AudioHarnessApi; refreshVersion: number }) {
  const [spaces, setSpaces] = useState<VectorSpace[]>([]);
  const [engines, setEngines] = useState<EngineAsset[]>([]);
  const [vadEngines, setVadEngines] = useState<EngineAsset[]>([]);
  const [infrastructure, setInfrastructure] = useState<VectorInfrastructure | null>(null);
  const [selectedCollection, setSelectedCollection] = useState("");
  const [tab, setTab] = useState<Tab>("register");
  const [file, setFile] = useState<File | null>(null);
  const [engineId, setEngineId] = useState("");
  const [vadEngineId, setVadEngineId] = useState("");
  const [vadPreview, setVadPreview] = useState<VadPreviewState | null>(null);
  const [previewingVad, setPreviewingVad] = useState(false);
  const [vadPostprocessing, setVadPostprocessing] = useState<VadPostprocessing>(defaultVadPostprocessing);
  const previewRevisionRef = useRef(0);
  const vadSelectionTouchedRef = useRef(false);
  const [algorithm, setAlgorithm] = useState("eres2netv2");
  const [vectorName, setVectorName] = useState("");
  const [selectedVectors, setSelectedVectors] = useState<string[]>([]);
  const [embeddingConfigs, setEmbeddingConfigs] = useState<Record<string, EmbeddingConfig>>({});
  const [payload, setPayload] = useState("{}");
  const [speakerId, setSpeakerId] = useState("");
  const [channelId, setChannelId] = useState("");
  const [capturedAt, setCapturedAt] = useState(currentLocalTime);
  const [qualityScore, setQualityScore] = useState("0");
  const [filter, setFilter] = useState("{}");
  const [rows, setRows] = useState<(VectorPoint | VectorSpeakerMatch)[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [create, setCreate] = useState({ template: "voiceprint", collection: "voiceprints", vectors: featureTemplateVectors });
  const selected = useMemo(() => spaces.find((item) => item.collection_name === selectedCollection) ?? spaces[0], [spaces, selectedCollection]);

  const load = async () => {
    try {
      const [nextSpaces, nextEngines, nextVadEngines, nextInfrastructure] = await Promise.all([api.listVectorSpaces(), api.listEngines({ kind: "embedding" as never, enabled: true }), api.listEngines({ kind: "vad" as never, enabled: true }), api.getVectorInfrastructure()]);
      const embeddingItems = nextEngines.items.filter((item) => item.kind === "embedding" && item.enabled);
      const vadItems = nextVadEngines.items.filter((item) => item.kind === "vad" && item.enabled);
      setSpaces(nextSpaces); setEngines(embeddingItems); setVadEngines(vadItems);
      setInfrastructure(nextInfrastructure);
      if (!selectedCollection && nextSpaces[0]) setSelectedCollection(nextSpaces[0].collection_name);
      setEngineId((current) => embeddingItems.some((item) => item.id === current) ? current : embeddingItems[0]?.id ?? "");
      setVadEngineId((current) => vadSelectionTouchedRef.current && (current === "" || vadItems.some((item) => item.id === current)) ? current : vadItems[0]?.id ?? "");
    } catch (reason) { setError(errorMessage(reason)); }
  };
  // refreshVersion 是外部刷新信号，effect 负责把远端状态同步到本页。
  // eslint-disable-next-line react-hooks/set-state-in-effect, react-hooks/exhaustive-deps
  useEffect(() => { void load(); }, [refreshVersion]);
  useEffect(() => {
    if (!selected) return;
    // Collection 切换后需要原子重置依赖它的登记表单状态。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (!selected.vectors.some((item) => item.name === vectorName)) setVectorName(selected.vectors[0]?.name ?? "");
    setSelectedVectors(selected.vectors.map((item) => item.name));
    setEmbeddingConfigs(Object.fromEntries(selected.vectors.map((item) => [item.name, { engine_id: engineId || engines[0]?.id || "", algorithm: item.name, normalize: true }])));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.collection_name, engines.length]);
  const createSpace = async () => {
    setError("");
    try {
      const next = await api.createVectorSpace({ collection_name: create.collection, vectors: JSON.parse(create.vectors), template: create.template === "voiceprint" ? "voiceprint" : null });
      setCreating(false); await load(); setSelectedCollection(next.collection_name);
    } catch (reason) { setError(errorMessage(reason)); }
  };
  const selectAudio = (next: File) => {
    previewRevisionRef.current += 1;
    setFile(next);
    setVadPreview(null);
    setPreviewingVad(false);
    setRows([]);
    if (vadEngineId) void previewVad(next, vadEngineId, vadPostprocessing);
  };
  const selectVadEngine = (next: string) => {
    vadSelectionTouchedRef.current = true;
    previewRevisionRef.current += 1;
    setVadEngineId(next);
    setVadPreview(null);
    setPreviewingVad(false);
    setRows([]);
    if (next && file) void previewVad(file, next, vadPostprocessing);
  };
  const requireSearchContext = (searchAudio: File | null) => {
    if (!selected || !searchAudio || !engineId || !vectorName) throw new Error("请选择空间、音频、Named Vector 和 embedding 引擎");
    if (selected.template !== "voiceprint") throw new Error("人物搜索仅支持 voiceprint Collection");
    JSON.parse(filter);
  };
  const register = async () => {
    setError(""); setMessage("");
    try {
      if (vadEngineId && !vadPreview) throw new Error("请等待 VAD 音频处理完成");
      const registrationAudio = vadEngineId ? vadPreview?.file ?? null : file;
      if (!selected || !registrationAudio || !selectedVectors.length) throw new Error("请选择 Collection、音频和至少一个 Named Vector");
      const configs = selectedVectors.map((name) => ({ vector_name: name, ...embeddingConfigs[name] }));
      if (configs.some((item) => !item.engine_id || !item.algorithm)) throw new Error("请为每个 Named Vector 配置引擎和算法");
      JSON.parse(payload); const form = new FormData(); form.set("audio", registrationAudio); form.set("payload_json", payload); form.set("embeddings_json", JSON.stringify(configs));
      if (selected.template === "voiceprint") {
        if (!speakerId.trim() || !channelId.trim()) throw new Error("Speaker ID 和 Channel ID 必填");
        const capturedMillis = new Date(capturedAt).getTime();
        if (!Number.isFinite(capturedMillis)) throw new Error("采集时间无效");
        form.set("speaker_id", speakerId.trim()); form.set("channel_id", channelId.trim());
        form.set("captured_at", String(capturedMillis)); form.set("quality_score", qualityScore);
      }
      const result = await api.registerVectorAudio(selected!.collection_name, form); setMessage(`已登记 Point ${result.point_id}`);
    } catch (reason) { setError(errorMessage(reason)); }
  };
  const search = async () => {
    setError("");
    try {
      if (vadEngineId && !vadPreview) throw new Error("请等待 VAD 音频处理完成");
      const searchAudio = vadEngineId ? vadPreview?.file ?? null : file;
      requireSearchContext(searchAudio); const form = new FormData(); form.set("audio", searchAudio!); form.set("vector_name", vectorName); form.set("engine_id", engineId); form.set("algorithm", algorithm); form.set("normalize", "true"); form.set("top_k", "10"); form.set("filter_json", filter);
      setRows((await api.searchVectorAudio(selected!.collection_name, form)).groups);
    } catch (reason) { setError(errorMessage(reason)); }
  };
  const previewVad = async (sourceAudio: File, engine: string, postprocessing: VadPostprocessing | null) => {
    setError("");
    setMessage("");
    if (!selected) {
      setError("请选择音频和 VAD 引擎");
      return;
    }
    const revision = previewRevisionRef.current + 1;
    previewRevisionRef.current = revision;
    setPreviewingVad(true);
    setVadPreview(null);
      setRows([]);
    try {
      const form = new FormData();
      form.set("audio", sourceAudio);
      form.set("vad_engine_id", engine);
      if (postprocessing) form.set("vad_postprocessing_json", JSON.stringify(postprocessing));
      const result = await api.previewVectorSearchVad(selected.collection_name, form);
      if (previewRevisionRef.current !== revision) return;
      setVadPreview({ file: previewFile(result), durationMs: result.duration_ms });
    } catch (reason) {
      if (previewRevisionRef.current === revision) setError(errorMessage(reason));
    } finally {
      if (previewRevisionRef.current === revision) setPreviewingVad(false);
    }
  };
  const scroll = async () => {
    setError("");
    try { if (!selected) return; setRows((await api.scrollVectorPoints(selected.collection_name, { filter: JSON.parse(filter), limit: 100, with_vectors: false })).items); }
    catch (reason) { setError(errorMessage(reason)); }
  };
  const removeSpace = async () => {
    if (!selected) return;
    const confirmation = window.prompt(`输入 ${selected.collection_name} 以确认删除 Qdrant Collection`);
    if (confirmation === null) return;
    try {
      await api.destroyVectorSpace(selected.collection_name, confirmation);
      setSelectedCollection(""); setRows([]); await load();
    } catch (reason) { setError(errorMessage(reason)); }
  };
  const updatePayload = async (row: VectorPoint) => {
    if (!selected) return;
    const next = window.prompt("替换 Point payload（JSON object）", JSON.stringify(row.payload, null, 2));
    if (next === null) return;
    try { await api.replaceVectorPayload(selected.collection_name, row.point_id, JSON.parse(next)); await scroll(); }
    catch (reason) { setError(errorMessage(reason)); }
  };
  const searchDisabled = previewingVad
    || !file
    || !engineId
    || !vectorName
    || (Boolean(vadEngineId) && !vadPreview);
  const registerDisabled = previewingVad
    || !file
    || !selectedVectors.length
    || (Boolean(vadEngineId) && !vadPreview);
  const vadControls = <section className="vector-vad-settings">
    <label><span>VAD 预处理</span><select value={vadEngineId} onChange={(event) => selectVadEngine(event.target.value)}><option value="">不使用 VAD</option>{vadEngines.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
    {vadEngineId && <details className="vector-vad-postprocessing"><summary>后处理参数</summary><div className="form-grid two-columns">{[["smooth_window_size", "平滑窗口帧数", 1, 1], ["speech_threshold", "语音阈值", 0, 0.01], ["pad_start_frame", "起点前补帧数", 1, 1], ["min_speech_frame", "最短语音帧数", 1, 1], ["max_speech_frame", "最长语音帧数", 1, 1], ["min_silence_frame", "最短静音帧数", 1, 1]].map(([name, label, min, step]) => <label key={name}><span>{label}</span><input type="number" min={min} step={step} value={vadPostprocessing.parameters[String(name)]} onChange={(event) => { const next = { ...vadPostprocessing, parameters: { ...vadPostprocessing.parameters, [String(name)]: Number(event.target.value) } }; setVadPostprocessing(next); if (file) void previewVad(file, vadEngineId, next); }} /></label>)}</div></details>}
    {vadEngineId && previewingVad && <small className="vector-vad-processing">正在按 VAD 裁剪音频…</small>}
    {vadPreview && <div className="vector-vad-audio"><span><strong>VAD 裁剪后音频</strong><small>{formatDuration(vadPreview.durationMs)} · {tab === "register" ? "登记" : "搜索"}将使用此音频</small></span><AudioPlayer blob={vadPreview.file} label="VAD 裁剪后音频" /></div>}
  </section>;

  return <main className="workspace-page vector-workspace">
    <section className="page-heading">
      <div>
        <h1>向量检索</h1>
        <p>以音频生成向量，在 Qdrant Named Vector 空间中登记、检索与维护数据。</p>
        {infrastructure && <small className={infrastructure.ready ? "status-ok" : "status-warning"}>Qdrant {infrastructure.ready ? "已就绪" : infrastructure.configured ? "不可用" : "未配置"} · gRPC {infrastructure.grpc_target ?? "—"}{infrastructure.http_url && <> · <a href={`${infrastructure.http_url.replace(/\/$/, "")}/dashboard`} target="_blank" rel="noreferrer">Dashboard</a></>}</small>}
      </div>
    </section>
    {error && <div className="alert alert-error">{error}</div>}
    {message && <div className="alert alert-success">{message}</div>}
    <section className="vector-layout">
      <aside className="panel vector-space-list">
        <h2>Qdrant Collections</h2>
        <button className="secondary-button vector-create-trigger" onClick={() => setCreating(true)}>创建 Collection</button>
        {spaces.map((space) => <button className={space.collection_name === selected?.collection_name ? "active" : ""} key={space.collection_name} onClick={() => {
          previewRevisionRef.current += 1;
          setSelectedCollection(space.collection_name);
          setVadPreview(null);
          setPreviewingVad(false);
          setRows([]);
          if (tab === "search" && space.template !== "voiceprint") setTab("register");
        }}><strong>{space.collection_name}</strong><small>{space.vectors.length} 个 Named Vector</small></button>)}
        {!spaces.length && <p className="empty-copy">Qdrant 中尚无 Collection。</p>}
      </aside>
      <section className="panel vector-main">
        {selected ? <>
          <header className="vector-space-header">
            <div><h2>{selected.collection_name}</h2><small>{selected.vectors.map((item) => `${item.name} ${item.size}D`).join(" · ") || "当前 Collection 不是 Named Vector 结构"}</small></div>
            <div className="panel-actions"><button className="text-button danger" onClick={() => void removeSpace()}>删除 Collection</button></div>
          </header>
          <nav className="filter-tabs">{([['register','登记'],['search','人物搜索'],['query','条件查询'],['records','记录']] as [Tab,string][]).map(([key,label]) => <button className={tab === key ? "active" : ""} disabled={key === "search" && selected.template !== "voiceprint"} onClick={() => { setTab(key); setRows([]); }} key={key}>{label}</button>)}</nav>
          {(tab === "register" || tab === "search") && <div className="vector-form">
            <AudioInput file={file} onChange={selectAudio} onRecordingDeviceName={(name) => setChannelId((current) => current.trim() || name)} vadControls={vadControls} />
            {tab === "register" ? <div className="vector-enrollment-list"><table className="vector-config-table" aria-label="本次登记的向量与推理配置"><thead><tr><th scope="col">启用</th><th scope="col">向量名称与维度</th><th scope="col">推理服务</th><th scope="col">模型</th><th scope="col">归一化</th></tr></thead><tbody>
              {selected.vectors.map((vector) => {
                const config = embeddingConfigs[vector.name] ?? { engine_id: engineId, algorithm: vector.name, normalize: true };
                const checked = selectedVectors.includes(vector.name);
                return <tr key={vector.name}>
                  <td><input aria-label={`启用向量 ${vector.name}`} type="checkbox" checked={checked} onChange={(event) => setSelectedVectors(event.target.checked ? [...selectedVectors, vector.name] : selectedVectors.filter((name) => name !== vector.name))} /></td>
                  <th scope="row"><strong>{vector.name}</strong><small>{vector.size}D</small></th>
                  <td><label className="vector-select-cell"><span className="visually-hidden">{vector.name} 推理服务</span><select disabled={!checked} value={config.engine_id} onChange={(event) => setEmbeddingConfigs({ ...embeddingConfigs, [vector.name]: { ...config, engine_id: event.target.value } })}>{engines.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label></td>
                  <td><input aria-label={`${vector.name} 模型`} disabled={!checked} value={config.algorithm} onChange={(event) => setEmbeddingConfigs({ ...embeddingConfigs, [vector.name]: { ...config, algorithm: event.target.value } })} /></td>
                  <td><input aria-label={`${vector.name} 归一化`} disabled={!checked} type="checkbox" checked={config.normalize} onChange={(event) => setEmbeddingConfigs({ ...embeddingConfigs, [vector.name]: { ...config, normalize: event.target.checked } })} /></td>
                </tr>;
              })}
            </tbody></table></div> : <>
              <div className="form-grid two-columns">
                <label><span>Named Vector</span><select value={vectorName} onChange={(event) => setVectorName(event.target.value)}>{selected.vectors.map((item) => <option key={item.name}>{item.name}</option>)}</select></label>
                <label><span>音频向量引擎</span><select value={engineId} onChange={(event) => setEngineId(event.target.value)}>{engines.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
                <label><span>算法</span><input value={algorithm} onChange={(event) => setAlgorithm(event.target.value)} /></label>
              </div>
            </>}
            {tab === "register" ? <>
              {selected.template === "voiceprint" && <fieldset className="form-grid two-columns feature-metadata"><legend>登记信息</legend><label><span>说话人 ID</span><input aria-label="Speaker ID" maxLength={128} value={speakerId} onChange={(event) => setSpeakerId(event.target.value)} placeholder="speaker-001" /></label><label><span>通道 ID</span><input aria-label="Channel ID" maxLength={128} value={channelId} onChange={(event) => setChannelId(event.target.value)} /><small>现场录音时自动填充麦克风名称</small></label><label><span>采集时间</span><input type="datetime-local" value={capturedAt} onChange={(event) => setCapturedAt(event.target.value)} /></label><label><span>质量分数</span><input aria-label="Quality Score" type="number" min="0" max="1" step="0.01" value={qualityScore} onChange={(event) => setQualityScore(event.target.value)} /></label></fieldset>}
              <label><span>{selected.template === "voiceprint" ? "扩展数据 · JSON" : "Point payload JSON"}</span><textarea className="json-editor" value={payload} onChange={(event) => setPayload(event.target.value)} /></label>
            </> : <label><span>Qdrant Filter JSON</span><textarea className="json-editor" value={filter} onChange={(event) => setFilter(event.target.value)} /></label>}
            <div className="vector-form-actions"><button className="primary-button" disabled={tab === "register" ? registerDisabled : searchDisabled} onClick={() => void (tab === "register" ? register() : search())}>{tab === "register" ? "生成并登记" : "执行人物搜索"}</button></div>
          </div>}
          {(tab === "query" || tab === "records") && <div className="vector-form"><label><span>Qdrant Filter JSON</span><textarea className="json-editor" value={filter} onChange={(event) => setFilter(event.target.value)} /></label><button className="primary-button" onClick={() => void scroll()}>查询记录</button></div>}
          {rows.length > 0 && <div className="vector-results">{rows.map((row, index) => "speaker_id" in row ? <article className="vector-speaker-result" key={`${row.speaker_id}-${index}`}><div><strong>{row.speaker_id}</strong><b>分数 {row.score.toFixed(4)}</b></div></article> : <article key={row.point_id}><div><strong>{row.point_id}</strong></div><pre>{JSON.stringify(row.payload, null, 2)}</pre><div className="panel-actions"><button className="text-button" onClick={() => void updatePayload(row)}>编辑 payload</button><button className="text-button danger" onClick={() => { if (window.confirm(`删除 Point ${row.point_id}？`)) void api.deleteVectorPoint(selected.collection_name, row.point_id).then(scroll); }}>删除</button></div></article>)}</div>}
        </> : <div className="empty-state">请选择一个 Qdrant Collection。</div>}
      </section>
    </section>
    {creating && <div className="vector-modal-backdrop"><section className="confirm-dialog vector-create-dialog"><h2>创建 Qdrant Collection</h2><div className="form-grid"><label><span>模板</span><select value={create.template} onChange={(event) => setCreate(event.target.value === "voiceprint" ? { template: "voiceprint", collection: "voiceprints", vectors: featureTemplateVectors } : { ...create, template: "custom" })}><option value="voiceprint">语音特征向量提取</option><option value="custom">自定义</option></select></label>{create.template === "voiceprint" && <div className="template-note"><strong>语音特征向量提取</strong><span>eres2netv2 · 192D · cosine</span><small>自动建立 speaker_id、channel_id、captured_at、quality_score 索引。</small></div>}<label><span>Collection</span><input value={create.collection} onChange={(event) => setCreate({ ...create, collection: event.target.value })} /></label><label><span>Named Vectors JSON</span><textarea className="json-editor" value={create.vectors} onChange={(event) => setCreate({ ...create, vectors: event.target.value })} /></label></div><div className="panel-actions"><button className="secondary-button" onClick={() => setCreating(false)}>取消</button><button className="primary-button" onClick={() => void createSpace()}>创建</button></div></section></div>}
  </main>;
}
