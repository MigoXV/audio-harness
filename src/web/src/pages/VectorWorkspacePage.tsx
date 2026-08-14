import { useEffect, useMemo, useState } from "react";

import type { AudioHarnessApi, EngineAsset, VectorInfrastructure, VectorMatch, VectorPoint, VectorSpace } from "../api/types";
import { errorMessage } from "../api/client";

type Tab = "register" | "search" | "query" | "records";
type EmbeddingConfig = { engine_id: string; algorithm: string; normalize: boolean };

const featureTemplateVectors = '[{"name":"eres2netv2","size":192,"distance":"cosine"}]';

function currentLocalTime(): string {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function AudioInput({ file, onChange }: { file: File | null; onChange: (file: File) => void }) {
  const [recording, setRecording] = useState(false);
  const [recorder, setRecorder] = useState<MediaRecorder | null>(null);
  const start = async () => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const chunks: Blob[] = [];
    const next = new MediaRecorder(stream);
    next.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
    next.onstop = () => {
      stream.getTracks().forEach((track) => track.stop());
      onChange(new File([new Blob(chunks, { type: next.mimeType })], `recording-${Date.now()}.webm`, { type: next.mimeType }));
      setRecording(false);
    };
    next.start(); setRecorder(next); setRecording(true);
  };
  return <div className="vector-audio-input"><label><span>音频文件</span><input type="file" accept="audio/*" onChange={(event) => { const next = event.target.files?.[0]; if (next) onChange(next); }} /></label><button type="button" className="secondary-button" onClick={() => recording ? recorder?.stop() : void start()}>{recording ? "停止录音" : "浏览器录音"}</button>{file && <small>{file.name} · {(file.size / 1024).toFixed(1)} KB</small>}</div>;
}

export function VectorWorkspacePage({ api, refreshVersion }: { api: AudioHarnessApi; refreshVersion: number }) {
  const [spaces, setSpaces] = useState<VectorSpace[]>([]);
  const [engines, setEngines] = useState<EngineAsset[]>([]);
  const [infrastructure, setInfrastructure] = useState<VectorInfrastructure | null>(null);
  const [selectedCollection, setSelectedCollection] = useState("");
  const [tab, setTab] = useState<Tab>("register");
  const [file, setFile] = useState<File | null>(null);
  const [engineId, setEngineId] = useState("");
  const [algorithm, setAlgorithm] = useState("eres2netv2");
  const [vectorName, setVectorName] = useState("");
  const [selectedVectors, setSelectedVectors] = useState<string[]>([]);
  const [embeddingConfigs, setEmbeddingConfigs] = useState<Record<string, EmbeddingConfig>>({});
  const [payload, setPayload] = useState("{}");
  const [speakerId, setSpeakerId] = useState("");
  const [channelId, setChannelId] = useState("");
  const [capturedAt, setCapturedAt] = useState(currentLocalTime);
  const [qualityScore, setQualityScore] = useState("0");
  const [clusterId, setClusterId] = useState("-1");
  const [filter, setFilter] = useState("{}");
  const [rows, setRows] = useState<(VectorPoint | VectorMatch)[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [create, setCreate] = useState({ template: "voiceprint", collection: "voiceprints", vectors: featureTemplateVectors });
  const selected = useMemo(() => spaces.find((item) => item.collection_name === selectedCollection) ?? spaces[0], [spaces, selectedCollection]);

  const load = async () => {
    try {
      const [nextSpaces, nextEngines, nextInfrastructure] = await Promise.all([api.listVectorSpaces(), api.listEngines({ kind: "embedding" as never, enabled: true }), api.getVectorInfrastructure()]);
      setSpaces(nextSpaces); setEngines(nextEngines.items);
      setInfrastructure(nextInfrastructure);
      if (!selectedCollection && nextSpaces[0]) setSelectedCollection(nextSpaces[0].collection_name);
      if (!engineId && nextEngines.items[0]) setEngineId(nextEngines.items[0].id);
    } catch (reason) { setError(errorMessage(reason)); }
  };
  useEffect(() => { void load(); }, [refreshVersion]);
  useEffect(() => {
    if (!selected) return;
    if (!selected.vectors.some((item) => item.name === vectorName)) setVectorName(selected.vectors[0]?.name ?? "");
    setSelectedVectors(selected.vectors.map((item) => item.name));
    setEmbeddingConfigs(Object.fromEntries(selected.vectors.map((item) => [item.name, { engine_id: engineId || engines[0]?.id || "", algorithm: item.name, normalize: true }])));
  }, [selected?.collection_name, engines.length]);

  const createSpace = async () => {
    setError("");
    try {
      const next = await api.createVectorSpace({ collection_name: create.collection, vectors: JSON.parse(create.vectors), template: create.template === "voiceprint" ? "voiceprint" : null });
      setCreating(false); await load(); setSelectedCollection(next.collection_name);
    } catch (reason) { setError(errorMessage(reason)); }
  };
  const requireSearchContext = () => {
    if (!selected || !file || !engineId || !vectorName) throw new Error("请选择空间、音频、Named Vector 和 embedding 引擎");
    JSON.parse(payload); JSON.parse(filter);
  };
  const register = async () => {
    setError(""); setMessage("");
    try {
      if (!selected || !file || !selectedVectors.length) throw new Error("请选择 Collection、音频和至少一个 Named Vector");
      const configs = selectedVectors.map((name) => ({ vector_name: name, ...embeddingConfigs[name] }));
      if (configs.some((item) => !item.engine_id || !item.algorithm)) throw new Error("请为每个 Named Vector 配置引擎和算法");
      JSON.parse(payload); const form = new FormData(); form.set("audio", file); form.set("payload_json", payload); form.set("embeddings_json", JSON.stringify(configs));
      if (selected.template === "voiceprint") {
        if (!speakerId.trim() || !channelId.trim()) throw new Error("Speaker ID 和 Channel ID 必填");
        const capturedMillis = new Date(capturedAt).getTime();
        if (!Number.isFinite(capturedMillis)) throw new Error("采集时间无效");
        form.set("speaker_id", speakerId.trim()); form.set("channel_id", channelId.trim());
        form.set("captured_at", String(capturedMillis)); form.set("quality_score", qualityScore); form.set("cluster_id", clusterId);
      }
      const result = await api.registerVectorAudio(selected!.collection_name, form); setMessage(`已登记 Point ${result.point_id}`);
    } catch (reason) { setError(errorMessage(reason)); }
  };
  const search = async () => {
    setError("");
    try {
      requireSearchContext(); const form = new FormData(); form.set("audio", file!); form.set("vector_name", vectorName); form.set("engine_id", engineId); form.set("algorithm", algorithm); form.set("normalize", "true"); form.set("top_k", "10"); form.set("filter_json", filter);
      setRows((await api.searchVectorAudio(selected!.collection_name, form)).matches);
    } catch (reason) { setError(errorMessage(reason)); }
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
  const updatePayload = async (row: VectorPoint | VectorMatch) => {
    if (!selected) return;
    const next = window.prompt("替换 Point payload（JSON object）", JSON.stringify(row.payload, null, 2));
    if (next === null) return;
    try { await api.replaceVectorPayload(selected.collection_name, row.point_id, JSON.parse(next)); await scroll(); }
    catch (reason) { setError(errorMessage(reason)); }
  };

  return <main className="workspace-page vector-workspace"><section className="page-heading"><div><p className="eyebrow">VECTOR RETRIEVAL</p><h1>向量检索</h1><p>以音频生成向量，在 Qdrant Named Vector 空间中登记、检索与维护数据。</p>{infrastructure && <small className={infrastructure.ready ? "status-ok" : "status-warning"}>Qdrant {infrastructure.ready ? "已就绪" : infrastructure.configured ? "不可用" : "未配置"} · gRPC {infrastructure.grpc_target ?? "—"}{infrastructure.http_url && <> · <a href={infrastructure.http_url} target="_blank" rel="noreferrer">Dashboard</a></>}</small>}</div><button className="primary-button" onClick={() => setCreating(true)}>创建 Collection</button></section>
    {error && <div className="alert alert-error">{error}</div>}{message && <div className="alert alert-success">{message}</div>}
    <section className="vector-layout"><aside className="panel vector-space-list"><h2>Qdrant Collections</h2>{spaces.map((space) => <button className={space.collection_name === selected?.collection_name ? "active" : ""} key={space.collection_name} onClick={() => setSelectedCollection(space.collection_name)}><strong>{space.collection_name}</strong><small>{space.vectors.length} 个 Named Vector</small></button>)}{!spaces.length && <p className="empty-copy">Qdrant 中尚无 Collection。</p>}</aside>
      <section className="panel vector-main">{selected ? <><header className="vector-space-header"><div><p className="eyebrow">{selected.template === "voiceprint" ? "语音特征向量提取" : "QDRANT"}</p><h2>{selected.collection_name}</h2><small>{selected.vectors.map((item) => `${item.name} ${item.size}D`).join(" · ") || "当前 Collection 不是 Named Vector 结构"}</small></div><div className="panel-actions"><button className="text-button danger" onClick={() => void removeSpace()}>删除 Collection</button></div></header><nav className="filter-tabs">{([['register','登记'],['search','相似搜索'],['query','条件查询'],['records','记录']] as [Tab,string][]).map(([key,label]) => <button className={tab === key ? "active" : ""} onClick={() => setTab(key)} key={key}>{label}</button>)}</nav>
        {(tab === "register" || tab === "search") && <div className="vector-form"><AudioInput file={file} onChange={setFile} />{tab === "register" ? <div className="vector-enrollment-list"><span>本次登记的向量与临时推理配置</span>{selected.vectors.map((vector) => { const config = embeddingConfigs[vector.name] ?? { engine_id: engineId, algorithm: vector.name, normalize: true }; const checked = selectedVectors.includes(vector.name); return <div className="vector-enrollment-row" key={vector.name}><label><input type="checkbox" checked={checked} onChange={(event) => setSelectedVectors(event.target.checked ? [...selectedVectors, vector.name] : selectedVectors.filter((name) => name !== vector.name))} /><strong>{vector.name}</strong><small>{vector.size}D</small></label><select disabled={!checked} value={config.engine_id} onChange={(event) => setEmbeddingConfigs({ ...embeddingConfigs, [vector.name]: { ...config, engine_id: event.target.value } })}>{engines.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select><input disabled={!checked} value={config.algorithm} onChange={(event) => setEmbeddingConfigs({ ...embeddingConfigs, [vector.name]: { ...config, algorithm: event.target.value } })} /><label><input disabled={!checked} type="checkbox" checked={config.normalize} onChange={(event) => setEmbeddingConfigs({ ...embeddingConfigs, [vector.name]: { ...config, normalize: event.target.checked } })} />归一化</label></div>; })}</div> : <div className="form-grid two-columns"><label><span>Named Vector</span><select value={vectorName} onChange={(event) => setVectorName(event.target.value)}>{selected.vectors.map((item) => <option key={item.name}>{item.name}</option>)}</select></label><label><span>音频向量引擎</span><select value={engineId} onChange={(event) => setEngineId(event.target.value)}>{engines.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><label><span>算法</span><input value={algorithm} onChange={(event) => setAlgorithm(event.target.value)} /></label></div>}{tab === "register" ? <>{selected.template === "voiceprint" && <div className="form-grid two-columns feature-metadata"><label><span>Speaker ID</span><input maxLength={128} value={speakerId} onChange={(event) => setSpeakerId(event.target.value)} placeholder="speaker-001" /></label><label><span>Channel ID</span><input maxLength={128} value={channelId} onChange={(event) => setChannelId(event.target.value)} placeholder="microphone-001" /></label><label><span>采集时间</span><input type="datetime-local" value={capturedAt} onChange={(event) => setCapturedAt(event.target.value)} /></label><label><span>Quality Score</span><input type="number" min="0" max="1" step="0.01" value={qualityScore} onChange={(event) => setQualityScore(event.target.value)} /></label><label><span>Cluster ID</span><input type="number" step="1" value={clusterId} onChange={(event) => setClusterId(event.target.value)} /><small>-1 表示尚未分配</small></label></div>}<label><span>{selected.template === "voiceprint" ? "扩展 payload JSON" : "Point payload JSON"}</span><textarea className="json-editor" value={payload} onChange={(event) => setPayload(event.target.value)} /></label></> : <label><span>Qdrant Filter JSON</span><textarea className="json-editor" value={filter} onChange={(event) => setFilter(event.target.value)} /></label>}<button className="primary-button" onClick={() => void (tab === "register" ? register() : search())}>{tab === "register" ? "生成并登记" : "执行相似搜索"}</button></div>}
        {(tab === "query" || tab === "records") && <div className="vector-form"><label><span>Qdrant Filter JSON</span><textarea className="json-editor" value={filter} onChange={(event) => setFilter(event.target.value)} /></label><button className="primary-button" onClick={() => void scroll()}>查询记录</button></div>}
        {rows.length > 0 && <div className="vector-results">{rows.map((row) => <article key={row.point_id}><div><strong>{row.point_id}</strong>{"score" in row && <b>{row.score.toFixed(4)}</b>}</div><pre>{JSON.stringify(row.payload, null, 2)}</pre><div className="panel-actions"><button className="text-button" onClick={() => void updatePayload(row)}>编辑 payload</button><button className="text-button danger" onClick={() => { if (selected && confirm(`删除 Point ${row.point_id}？`)) void api.deleteVectorPoint(selected.collection_name, row.point_id).then(scroll); }}>删除</button></div></article>)}</div>}</> : <div className="empty-state">请选择一个 Qdrant Collection。</div>}</section></section>
    {creating && <div className="vector-modal-backdrop"><section className="confirm-dialog vector-create-dialog"><h2>创建 Qdrant Collection</h2><div className="form-grid"><label><span>模板</span><select value={create.template} onChange={(event) => setCreate(event.target.value === "voiceprint" ? { template: "voiceprint", collection: "voiceprints", vectors: featureTemplateVectors } : { ...create, template: "custom" })}><option value="voiceprint">语音特征向量提取</option><option value="custom">自定义</option></select></label>{create.template === "voiceprint" && <div className="template-note"><strong>语音特征向量提取</strong><span>eres2netv2 · 192D · cosine</span><small>自动建立 speaker_id、channel_id、captured_at、quality_score、cluster_id 索引。</small></div>}<label><span>Collection</span><input value={create.collection} onChange={(event) => setCreate({ ...create, collection: event.target.value })} /></label><label><span>Named Vectors JSON</span><textarea className="json-editor" value={create.vectors} onChange={(event) => setCreate({ ...create, vectors: event.target.value })} /></label></div><div className="panel-actions"><button className="secondary-button" onClick={() => setCreating(false)}>取消</button><button className="primary-button" onClick={() => void createSpace()}>创建</button></div></section></div>}
  </main>;
}
