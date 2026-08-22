import { splitPcmChunks, wavToPcm } from "./audio.js";
import {
  applyEvent,
  buildSpeakerLanes,
  buildSpeakerTranscript,
  colorIndex,
  createState,
  getProgress,
  resolveSelectedLane,
} from "./state.js";

const elements = Object.fromEntries([
  "file", "start", "stop", "status", "meta", "upstream", "error", "progress-state",
  "send-progress", "vad-progress", "diarization-progress", "send-value", "vad-value",
  "diarization-value", "send-detail", "vad-detail", "diarization-detail", "timeline-axis",
  "timeline", "timeline-summary", "speaker-tab", "all-tab", "speaker-transcript", "all-transcript",
].map((id) => [id, document.querySelector(`#${id}`)]));

const SEND_INTERVAL_MS = 10; // 每块仍为 100 ms 音频，因此为约 10× 实时速度。
const MAX_BUFFERED_AUDIO_BYTES = 128_000;
const PCM_BYTES_PER_SECOND = 32_000;
const SPEAKER_COLORS = ["#2367d1", "#bc4b51", "#17806d", "#8b5bb5", "#c56a16", "#3e7c17", "#b63f84", "#347f9f"];

let socket;
let cancelled = false;
let sessionFinished = false;
let pcm;
let sentBytes = 0;
let totalAudioMs = 0;
let selection = null;
let activeTab = "speaker";
let state = createState();

fetch("/api/config")
  .then((response) => {
    if (!response.ok) throw new Error("无法读取 Demo 配置");
    return response.json();
  })
  .then((config) => { elements.upstream.textContent = config.upstream_url; })
  .catch((reason) => {
    elements.upstream.textContent = "不可用";
    elements.error.textContent = reason.message;
  });

function formatTime(milliseconds) {
  const seconds = Math.floor((milliseconds || 0) / 1_000);
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function formatPercent(value) {
  return `${Math.floor(value)}%`;
}

function setProgress(element, value) {
  element.style.width = `${value}%`;
  element.setAttribute("aria-valuenow", String(Math.round(value)));
}

function renderProgress() {
  const progress = getProgress(state, { sentBytes, totalBytes: pcm?.length || 0, totalAudioMs });
  setProgress(elements["send-progress"], progress.send);
  setProgress(elements["vad-progress"], progress.vad);
  setProgress(elements["diarization-progress"], progress.diarization);
  elements["send-value"].textContent = formatPercent(progress.send);
  elements["vad-value"].textContent = formatPercent(progress.vad);
  elements["diarization-value"].textContent = formatPercent(progress.diarization);

  elements["send-detail"].textContent = cancelled
    ? `已停止于 ${formatTime(sentBytes / PCM_BYTES_PER_SECOND * 1_000)}`
    : progress.send >= 100 ? "发送完成" : sentBytes > 0 ? "正在发送" : "等待开始";
  elements["vad-detail"].textContent = state.vad.final
    ? "处理完成"
    : state.vad.processedAudioMs > 0 ? `已处理至 ${formatTime(state.vad.processedAudioMs)}` : "等待音频";
  elements["diarization-detail"].textContent = state.processing.failed
    ? "聚类失败"
    : state.cluster.final ? "分离及聚类完成" : progress.completedBatchAudioMs > 0
      ? `已分离至 ${formatTime(progress.completedBatchAudioMs)}` : "等待片段";

  const topLayer = elements["diarization-progress"];
  topLayer.classList.toggle("failed", state.processing.failed);
  if (state.processing.complete) {
    elements["progress-state"].textContent = state.processing.failed ? "处理结束 · 聚类失败" : "处理完成";
    elements["progress-state"].className = `state-chip ${state.processing.failed ? "failure" : "success"}`;
  } else if (sentBytes > 0 || state.vad.processedAudioMs > 0) {
    elements["progress-state"].textContent = "处理中";
    elements["progress-state"].className = "state-chip working";
  } else {
    elements["progress-state"].textContent = pcm ? "准备就绪" : "等待音频";
    elements["progress-state"].className = "state-chip";
  }
}

function renderAxis() {
  elements["timeline-axis"].replaceChildren();
  if (!(totalAudioMs > 0)) return;
  for (const ratio of [0, 0.25, 0.5, 0.75, 1]) {
    const marker = document.createElement("span");
    marker.style.left = `${ratio * 100}%`;
    marker.textContent = formatTime(totalAudioMs * ratio);
    elements["timeline-axis"].append(marker);
  }
}

function laneColor(lane) {
  return SPEAKER_COLORS[colorIndex(lane.colorKey, SPEAKER_COLORS.length)];
}

function selectLane(lane, segmentId = lane.segments[0]?.segmentId) {
  selection = { laneKey: lane.key, segmentId };
  render();
}

function renderTimeline(lanes, selectedLane) {
  elements.timeline.replaceChildren();
  const visibleSegments = lanes.reduce((count, lane) => count + lane.segments.length, 0);
  elements["timeline-summary"].textContent = lanes.length
    ? `${lanes.length} 条甬道 · ${visibleSegments} 个说话人片段`
    : state.processing.complete ? "没有可展示的说话人转写" : "等待说话人分离结果";
  if (!lanes.length) {
    elements.timeline.className = "timeline-empty";
    elements.timeline.textContent = state.processing.complete ? "没有可展示的说话人转写。" : "等待说话人分离结果。";
    return;
  }
  elements.timeline.className = "timeline";
  for (const lane of lanes) {
    const row = document.createElement("article");
    row.className = `lane ${lane.clustered ? "clustered" : "local"}${selectedLane?.key === lane.key ? " selected" : ""}`;
    const color = laneColor(lane);
    row.style.setProperty("--lane-color", color);

    const label = document.createElement("button");
    label.className = "lane-label";
    label.type = "button";
    label.addEventListener("click", () => selectLane(lane));
    const dot = document.createElement("span");
    dot.className = "speaker-dot";
    const name = document.createElement("strong");
    name.textContent = lane.label;
    label.append(dot, name);
    if (lane.unassigned) {
      const badge = document.createElement("small");
      badge.className = "unassigned-badge";
      badge.textContent = "未归类";
      label.append(badge);
    } else if (!lane.clustered) {
      const badge = document.createElement("small");
      badge.className = "local-badge";
      badge.textContent = "待聚类";
      label.append(badge);
    }

    const track = document.createElement("div");
    track.className = "lane-track";
    for (const item of lane.segments) {
      const block = document.createElement("button");
      block.type = "button";
      block.className = "segment";
      const start = Math.max(0, item.audio_start_ms || 0);
      const end = Math.max(start, item.audio_end_ms || start);
      block.style.left = `${totalAudioMs > 0 ? Math.min(100, start / totalAudioMs * 100) : 0}%`;
      block.style.width = `${totalAudioMs > 0 ? Math.min(100, Math.max(0, (end - start) / totalAudioMs * 100)) : 0}%`;
      block.title = `${lane.label} · ${formatTime(start)}–${formatTime(end)}${item.speakerText ? ` · ${item.speakerText}` : ""}`;
      block.setAttribute("aria-label", block.title);
      block.addEventListener("click", () => selectLane(lane, item.segmentId));
      track.append(block);
    }
    row.append(label, track);
    elements.timeline.append(row);
  }
}

function transcriptRow(item) {
  const row = document.createElement("article");
  row.className = "transcript-row";
  const time = document.createElement("time");
  time.textContent = `${formatTime(item.audio_start_ms)} – ${formatTime(item.audio_end_ms)}`;
  const text = document.createElement("p");
  text.textContent = item.speakerError?.message || item.speakerText || "识别中…";
  if (item.speakerError) text.className = "failed-text";
  row.append(time, text);
  return row;
}

function renderTranscripts(selectedLane) {
  elements["speaker-transcript"].replaceChildren();
  if (!selectedLane) {
    elements["speaker-transcript"].textContent = state.processing.complete
      ? "没有可展示的说话人转写。"
      : "请先在甬道图中选择说话人。";
  } else {
    const heading = document.createElement("div");
    heading.className = "selected-speaker";
    heading.style.setProperty("--lane-color", laneColor(selectedLane));
    const title = document.createElement("strong");
    title.textContent = selectedLane.label;
    const count = document.createElement("span");
    const result = selectedLane.unassigned ? " · 最终聚类未分配" : "";
    count.textContent = `${selectedLane.segments.length} 个片段${result}`;
    heading.append(title, count);
    elements["speaker-transcript"].append(heading, ...selectedLane.segments.map(transcriptRow));
  }

  const entries = buildSpeakerTranscript(state);
  elements["all-transcript"].replaceChildren();
  if (!entries.length) {
    elements["all-transcript"].textContent = state.processing.complete
      ? "没有可展示的说话人转写。"
      : "等待说话人转写。";
  } else {
    for (const entry of entries) {
      const row = document.createElement("article");
      row.className = "transcript-row speaker-transcript-row";
      const meta = document.createElement("div");
      meta.className = "speaker-entry-meta";
      meta.style.setProperty("--lane-color", laneColor(entry));
      const speaker = document.createElement("strong");
      speaker.textContent = entry.label;
      const time = document.createElement("time");
      time.textContent = `${formatTime(entry.audioStartMs)} – ${formatTime(entry.audioEndMs)}`;
      meta.append(speaker, time);
      const text = document.createElement("p");
      if (entry.error) {
        text.textContent = entry.error.message;
      } else if (entry.text) {
        text.textContent = entry.text;
      } else {
        text.textContent = "识别中…";
      }
      if (entry.error) text.className = "failed-text";
      row.append(meta, text);
      elements["all-transcript"].append(row);
    }
  }
}

function renderTabs() {
  const speakerActive = activeTab === "speaker";
  elements["speaker-tab"].classList.toggle("active", speakerActive);
  elements["all-tab"].classList.toggle("active", !speakerActive);
  elements["speaker-tab"].setAttribute("aria-selected", String(speakerActive));
  elements["all-tab"].setAttribute("aria-selected", String(!speakerActive));
  elements["speaker-transcript"].hidden = !speakerActive;
  elements["all-transcript"].hidden = speakerActive;
}

function render() {
  renderProgress();
  renderAxis();
  const lanes = buildSpeakerLanes(state);
  const selectedLane = resolveSelectedLane(lanes, selection);
  selection = selectedLane ? { laneKey: selectedLane.key, segmentId: selection?.segmentId || selectedLane.segments[0]?.segmentId } : null;
  renderTimeline(lanes, selectedLane);
  renderTranscripts(selectedLane);
  renderTabs();
}

function wait(milliseconds) { return new Promise((resolve) => window.setTimeout(resolve, milliseconds)); }

async function waitForSocketCapacity() {
  while (socket?.readyState === WebSocket.OPEN && socket.bufferedAmount > MAX_BUFFERED_AUDIO_BYTES) {
    elements.status.textContent = "网络或上游处理较慢，正在降低推流速度。";
    await wait(SEND_INTERVAL_MS);
  }
}

async function streamAudio() {
  for (const chunk of splitPcmChunks(pcm)) {
    if (cancelled || socket?.readyState !== WebSocket.OPEN) break;
    await waitForSocketCapacity();
    if (cancelled || socket?.readyState !== WebSocket.OPEN) break;
    socket.send(chunk);
    sentBytes += chunk.length;
    renderProgress();
    await wait(SEND_INTERVAL_MS);
  }
  if (!cancelled && socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "stop" }));
    elements.status.textContent = "音频已发送，正在等待 VAD、说话人分离和最终聚类。";
  }
  elements.stop.disabled = true;
}

function finishSession() {
  sessionFinished = true;
  elements.stop.disabled = true;
  elements.start.disabled = !pcm;
  elements.file.disabled = false;
  if (state.processing.failed) {
    const message = state.processing.error?.message || "说话人聚类失败";
    elements.status.textContent = `会议处理已结束，但最终聚类失败：${message}`;
    elements.error.textContent = message;
  } else {
    elements.status.textContent = "会议处理完成。";
  }
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "disconnect" }));
}

function connect() {
  cancelled = false;
  sessionFinished = false;
  sentBytes = 0;
  selection = null;
  elements.error.textContent = "";
  elements.start.disabled = true;
  elements.file.disabled = true;
  state = createState();
  render();
  socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  elements.status.textContent = "正在创建转写会话…";
  socket.onmessage = ({ data }) => {
    const event = JSON.parse(data);
    if (event.type === "demo.ready") {
      elements.status.textContent = "会话已配置，正在以约 10× 实时速度发送 WAV。";
      elements.stop.disabled = false;
      streamAudio();
      return;
    }
    if (event.type === "x_aha.vad.physical_speech_stopped") {
      elements.status.textContent = "检测到物理停顿，语义 VAD 仍在确认本轮边界。";
    }
    if (event.type === "input_audio_buffer.speech_stopped") {
      elements.status.textContent = "语义轮次已结束，正在生成转写并更新说话人。";
    }
    if (event.type === "error") {
      const detail = event.error?.code ? `（${event.error.code}）` : "";
      elements.error.textContent = `${event.error?.message || "Realtime 出错"}${detail}`;
      console.error("Realtime error", event);
    }
    applyEvent(state, event);
    render();
    if (state.processing.complete && !sessionFinished) finishSession();
  };
  socket.onerror = () => {
    elements.error.textContent = "无法连接 Demo 服务。";
    elements.start.disabled = !pcm;
    elements.file.disabled = false;
  };
  socket.onclose = () => {
    if (!sessionFinished) elements.status.textContent = "连接已关闭，未收到最终聚类结果。";
    elements.stop.disabled = true;
    elements.start.disabled = !pcm;
    elements.file.disabled = false;
  };
}

elements.file.addEventListener("change", async () => {
  const [file] = elements.file.files;
  if (!file) return;
  try {
    elements.start.disabled = true;
    elements.error.textContent = "";
    elements.status.textContent = "正在解码 WAV…";
    pcm = await wavToPcm(file);
    totalAudioMs = pcm.length / PCM_BYTES_PER_SECOND * 1_000;
    sentBytes = 0;
    cancelled = false;
    selection = null;
    state = createState();
    elements.meta.textContent = `${file.name}，${(totalAudioMs / 1_000).toFixed(1)} 秒，已转换为 16 kHz 单声道 PCM。`;
    elements.status.textContent = "可以开始会议。";
    elements.start.disabled = false;
    render();
  } catch {
    pcm = undefined;
    totalAudioMs = 0;
    elements.error.textContent = "无法解码该 WAV 文件。";
    elements.status.textContent = "请选择有效 WAV 文件。";
    render();
  }
});

elements.start.addEventListener("click", connect);
elements.stop.addEventListener("click", () => {
  cancelled = true;
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "stop" }));
  elements.status.textContent = "已停止发送，正在处理已经发送的音频。";
  elements.stop.disabled = true;
  renderProgress();
});
elements["speaker-tab"].addEventListener("click", () => { activeTab = "speaker"; renderTabs(); });
elements["all-tab"].addEventListener("click", () => { activeTab = "all"; renderTabs(); });

render();
