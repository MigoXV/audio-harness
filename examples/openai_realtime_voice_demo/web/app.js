const SAMPLE_RATE = 24_000;
const CHUNK_SAMPLES = 2_400;

const conversation = document.querySelector("#conversation");
const empty = document.querySelector("#empty");
const toggle = document.querySelector("#toggle");
const clear = document.querySelector("#clear");
const status = document.querySelector("#status");
const detail = document.querySelector("#detail");
const model = document.querySelector("#model");
const target = document.querySelector("#target");
const connectionDot = document.querySelector("#connectionDot");

let socket = null;
let readyPromise = null;
let microphone = null;
let playbackContext = null;
let playbackAt = 0;
let playbackSources = new Set();
let assistantMessage = null;

function setStatus(title, description) {
  status.textContent = title;
  detail.textContent = description;
}

function addMessage(role, text = "", streaming = false) {
  empty.hidden = true;
  const item = document.createElement("article");
  item.className = `message ${role}${streaming ? " streaming" : ""}`;
  const label = document.createElement("small");
  label.textContent = role === "user" ? "YOU" : "AGENT";
  const content = document.createElement("p");
  content.textContent = text;
  item.append(label, content);
  conversation.append(item);
  conversation.scrollTop = conversation.scrollHeight;
  return item;
}

function stopPlayback() {
  for (const source of playbackSources) {
    try { source.stop(); } catch { /* source already ended */ }
  }
  playbackSources.clear();
  playbackAt = playbackContext?.currentTime || 0;
}

async function playPcm(buffer) {
  playbackContext ||= new AudioContext({ sampleRate: SAMPLE_RATE });
  await playbackContext.resume();
  const view = new DataView(buffer);
  const samples = new Float32Array(Math.floor(view.byteLength / 2));
  for (let index = 0; index < samples.length; index += 1) {
    samples[index] = view.getInt16(index * 2, true) / 32_768;
  }
  const audio = playbackContext.createBuffer(1, samples.length, SAMPLE_RATE);
  audio.copyToChannel(samples, 0);
  const source = playbackContext.createBufferSource();
  source.buffer = audio;
  source.connect(playbackContext.destination);
  playbackAt = Math.max(playbackAt, playbackContext.currentTime + 0.025);
  source.start(playbackAt);
  playbackAt += audio.duration;
  playbackSources.add(source);
  source.onended = () => playbackSources.delete(source);
}

function handleServerMessage(message) {
  switch (message.type) {
    case "ready":
      connectionDot.classList.add("ready");
      setStatus("Realtime 已就绪", "服务端正在管理完整语音会话");
      break;
    case "speech_started":
      stopPlayback();
      assistantMessage?.classList.remove("streaming");
      assistantMessage = null;
      setStatus("正在聆听", "检测到用户开始说话，已打断旧响应");
      break;
    case "speech_stopped":
      setStatus("正在思考", "语音片段已提交");
      break;
    case "user_transcript":
      if (message.text) addMessage("user", message.text);
      break;
    case "response_started":
      assistantMessage = addMessage("assistant", "", true);
      setStatus("正在回答", "文字与语音正在流式返回");
      break;
    case "assistant_delta": {
      assistantMessage ||= addMessage("assistant", "", true);
      const content = assistantMessage.querySelector("p");
      content.textContent += message.delta || "";
      conversation.scrollTop = conversation.scrollHeight;
      break;
    }
    case "assistant_done": {
      assistantMessage ||= addMessage("assistant", message.text || "");
      const content = assistantMessage.querySelector("p");
      if (!content.textContent && message.text) content.textContent = message.text;
      assistantMessage.classList.remove("streaming");
      assistantMessage = null;
      break;
    }
    case "response_done":
      setStatus(
        message.status === "completed" ? "可以继续说话" : "响应已结束",
        `状态：${message.status}`,
      );
      break;
    case "error":
      setStatus("Realtime 错误", message.message || "未知错误");
      break;
  }
}

function connect() {
  if (socket?.readyState === WebSocket.OPEN && readyPromise) return readyPromise;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(`${protocol}//${location.host}/voice`);
  socket.binaryType = "arraybuffer";
  setStatus("正在连接", "由 Demo 后端连接 Realtime API");
  readyPromise = new Promise((resolve, reject) => {
    socket.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        void playPcm(event.data);
        return;
      }
      const message = JSON.parse(String(event.data));
      handleServerMessage(message);
      if (message.type === "ready") resolve();
      if (message.type === "error") reject(new Error(message.message));
    };
    socket.onerror = () => reject(new Error("Demo WebSocket 连接失败"));
    socket.onclose = () => {
      socket = null;
      readyPromise = null;
      connectionDot.classList.remove("ready");
      setStatus("连接已关闭", "再次开始即可重连");
    };
  });
  return readyPromise;
}

function resample(input, inputRate, state) {
  const merged = new Float32Array(state.tail.length + input.length);
  merged.set(state.tail);
  merged.set(input, state.tail.length);
  const values = [];
  const step = inputRate / SAMPLE_RATE;
  while (state.position + 1 < merged.length) {
    const left = Math.floor(state.position);
    const fraction = state.position - left;
    values.push(merged[left] * (1 - fraction) + merged[left + 1] * fraction);
    state.position += step;
  }
  const consumed = Math.floor(state.position);
  state.tail = merged.slice(consumed);
  state.position -= consumed;
  return values;
}

function pcmBytes(samples) {
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  samples.forEach((sample, index) => {
    const value = Math.max(-1, Math.min(1, sample));
    view.setInt16(index * 2, value < 0 ? value * 32768 : value * 32767, true);
  });
  return buffer;
}

async function startMicrophone() {
  await connect();
  playbackContext ||= new AudioContext({ sampleRate: SAMPLE_RATE });
  await playbackContext.resume();
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });
  const context = new AudioContext();
  const source = context.createMediaStreamSource(stream);
  const processor = context.createScriptProcessor(4096, 1, 1);
  const silent = context.createGain();
  silent.gain.value = 0;
  const state = { tail: new Float32Array(0), position: 0 };
  let pending = [];
  processor.onaudioprocess = (event) => {
    pending.push(...resample(
      event.inputBuffer.getChannelData(0),
      context.sampleRate,
      state,
    ));
    while (pending.length >= CHUNK_SAMPLES) {
      const chunk = pending.splice(0, CHUNK_SAMPLES);
      if (socket?.readyState === WebSocket.OPEN) socket.send(pcmBytes(chunk));
    }
  };
  source.connect(processor);
  processor.connect(silent);
  silent.connect(context.destination);
  microphone = {
    stop: async () => {
      processor.onaudioprocess = null;
      if (pending.length && socket?.readyState === WebSocket.OPEN) {
        socket.send(pcmBytes(pending));
      }
      source.disconnect();
      processor.disconnect();
      silent.disconnect();
      stream.getTracks().forEach((track) => track.stop());
      await context.close();
    },
  };
}

async function toggleConversation() {
  toggle.disabled = true;
  try {
    if (microphone) {
      await microphone.stop();
      microphone = null;
      socket?.send(JSON.stringify({ type: "stop" }));
      toggle.textContent = "继续对话";
      toggle.classList.remove("active");
      setStatus("麦克风已暂停", "等待最后一个回答完成");
    } else {
      await startMicrophone();
      toggle.textContent = "暂停收听";
      toggle.classList.add("active");
      setStatus("正在聆听", "可以直接开始说话");
    }
  } catch (error) {
    setStatus("无法开始", error instanceof Error ? error.message : String(error));
  } finally {
    toggle.disabled = false;
  }
}

toggle.addEventListener("click", () => void toggleConversation());
clear.addEventListener("click", () => {
  stopPlayback();
  conversation.querySelectorAll(".message").forEach((item) => item.remove());
  empty.hidden = false;
  assistantMessage = null;
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "clear" }));
  }
});

fetch("/api/config")
  .then((response) => response.json())
  .then((config) => {
    target.textContent = config.base_url;
    model.textContent = `${config.model} · ${config.voice}`;
  })
  .catch(() => setStatus("配置读取失败", "请检查 Demo 后端"));

window.addEventListener("beforeunload", () => {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "disconnect" }));
  }
});
