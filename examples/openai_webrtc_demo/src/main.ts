import type { RealtimeServerEvent } from "openai/resources/realtime/realtime";

import "./styles.css";

const connectButton = document.querySelector<HTMLButtonElement>("#connect")!;
const disconnectButton =
  document.querySelector<HTMLButtonElement>("#disconnect")!;
const statusLine = document.querySelector<HTMLElement>("#status")!;
const transcript = document.querySelector<HTMLElement>("#lines")!;
const eventsView = document.querySelector<HTMLElement>("#events")!;
const eventCount = document.querySelector<HTMLElement>("#event-count")!;
const remoteAudio = document.querySelector<HTMLAudioElement>("#remote-audio")!;

let peer: RTCPeerConnection | undefined;
let microphone: MediaStream | undefined;
let callLocation: string | undefined;
let events = 0;
const transcripts = new Map<string, string>();

interface PublicConfig {
  ice_servers: RTCIceServer[];
}

function setStatus(message: string): void {
  statusLine.textContent = message;
}

function renderTranscripts(): void {
  transcript.replaceChildren(
    ...Array.from(transcripts.values()).map((text) => {
      const line = document.createElement("p");
      line.textContent = text;
      return line;
    }),
  );
}

function handleEvent(event: RealtimeServerEvent): void {
  events += 1;
  eventCount.textContent = String(events);
  eventsView.textContent =
    `${new Date().toLocaleTimeString()}  ${event.type}\n` +
    eventsView.textContent;

  if (event.type === "conversation.item.input_audio_transcription.delta") {
    transcripts.set(
      event.item_id,
      `${transcripts.get(event.item_id) ?? ""}${event.delta ?? ""}`,
    );
    renderTranscripts();
  } else if (
    event.type === "conversation.item.input_audio_transcription.completed"
  ) {
    transcripts.set(event.item_id, event.transcript);
    renderTranscripts();
    setStatus("转写完成，继续监听");
  } else if (event.type === "input_audio_buffer.speech_started") {
    setStatus("检测到语音");
  } else if (event.type === "input_audio_buffer.speech_stopped") {
    setStatus("话音结束，正在识别");
  } else if (event.type === "error") {
    setStatus(`错误：${event.error.message}`);
  }
}

async function waitForIceGathering(connection: RTCPeerConnection) {
  if (connection.iceGatheringState === "complete") {
    return;
  }
  await new Promise<void>((resolve) => {
    const changed = () => {
      if (connection.iceGatheringState === "complete") {
        connection.removeEventListener("icegatheringstatechange", changed);
        resolve();
      }
    };
    connection.addEventListener("icegatheringstatechange", changed);
  });
}

async function connect(): Promise<void> {
  connectButton.disabled = true;
  setStatus("正在申请麦克风");
  try {
    const configResponse = await fetch("/api/config");
    if (!configResponse.ok) {
      throw new Error("无法读取 WebRTC Demo 配置");
    }
    const config = (await configResponse.json()) as PublicConfig;
    microphone = await navigator.mediaDevices.getUserMedia({ audio: true });
    peer = new RTCPeerConnection({ iceServers: config.ice_servers });
    const channel = peer.createDataChannel("oai-events");

    for (const track of microphone.getAudioTracks()) {
      peer.addTrack(track, microphone);
    }
    peer.addEventListener("track", (event) => {
      remoteAudio.srcObject = event.streams[0] ?? new MediaStream([event.track]);
    });
    peer.addEventListener("connectionstatechange", () => {
      if (peer?.connectionState === "connected") {
        setStatus("已连接，等待语音");
      } else if (
        peer?.connectionState === "failed" ||
        peer?.connectionState === "disconnected"
      ) {
        setStatus(`连接状态：${peer.connectionState}`);
      }
    });
    channel.addEventListener("message", (message) => {
      handleEvent(JSON.parse(String(message.data)) as RealtimeServerEvent);
    });

    setStatus("正在生成 WebRTC offer");
    await peer.setLocalDescription(await peer.createOffer());
    await waitForIceGathering(peer);
    if (peer.localDescription === null) {
      throw new Error("浏览器没有生成 SDP offer");
    }

    setStatus("正在协商");
    const response = await fetch("/api/realtime/calls", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sdp: peer.localDescription.sdp }),
    });
    const payload = (await response.json()) as {
      sdp?: string;
      location?: string;
      error?: string;
    };
    if (!response.ok || !payload.sdp) {
      throw new Error(payload.error ?? `协商失败（${response.status}）`);
    }
    callLocation = payload.location;
    await peer.setRemoteDescription({ type: "answer", sdp: payload.sdp });
    disconnectButton.disabled = false;
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "连接失败");
    await disconnect();
  }
}

async function disconnect(): Promise<void> {
  const location = callLocation;
  callLocation = undefined;
  microphone?.getTracks().forEach((track) => track.stop());
  microphone = undefined;
  peer?.close();
  peer = undefined;
  if (location) {
    await fetch("/api/realtime/hangup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ location }),
    }).catch(() => undefined);
  }
  connectButton.disabled = false;
  disconnectButton.disabled = true;
  setStatus("已断开");
}

connectButton.addEventListener("click", () => void connect());
disconnectButton.addEventListener("click", () => void disconnect());
window.addEventListener("beforeunload", () => {
  microphone?.getTracks().forEach((track) => track.stop());
  peer?.close();
});
