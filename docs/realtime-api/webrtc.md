# WebRTC 接入

WebRTC 使用 HTTP SDP 信令、单条输入音轨、单条服务端输出音轨和名为 `oai-events` 的 DataChannel。浏览器音频由 WebRTC 协商编码，服务端解码、重采样为内部 16 kHz PCM；客户端不要再通过 DataChannel 发送 `input_audio_buffer.append`。

## 创建 Call

`POST /v1/realtime/calls` 使用 `multipart/form-data`：

<!-- API: POST /v1/realtime/calls -->

| 字段/query | 必填 | 说明 |
| --- | --- | --- |
| form `sdp` | 是 | UTF-8 offer SDP，可作为文本或文件 part |
| form `session` | 否 | UTF-8 JSON；`type=realtime` 按 Realtime Session 解析，否则按 Transcription Session |
| query `model` | 条件 | public active Route；未传时从 session model/转写 model 推导，最终缺省 `gpt-4o-transcribe` |
| query `x_aha` | 否 | 只接受 `v1` |
| query `diagnostics` | 否 | boolean，默认 false |

成功返回 `201 application/sdp`，body 为 answer SDP，响应头 `Location: /v1/realtime/calls/{call_id}`。

```bash
curl -sS -X POST 'http://localhost:8000/v1/realtime/calls?model=meeting-agent&x_aha=v1' \
  -F 'sdp=@offer.sdp;type=application/sdp' \
  -F 'session={"type":"realtime"};type=application/json' \
  -D headers.txt -o answer.sdp
```

Offer 必须包含：

- 恰好一条向服务端发送的 audio track；第一版拒绝零条或多条。
- SCTP/DataChannel 协商，客户端预先创建 label 为 `oai-events` 的 DataChannel。
- SDP 和 session 分别不能超过 `max_message_bytes`，默认 4 MiB。

## 浏览器骨架

```js
const pc = new RTCPeerConnection();
const events = pc.createDataChannel("oai-events");
const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
pc.addTrack(stream.getAudioTracks()[0], stream);

events.onmessage = ({ data }) => {
  const event = JSON.parse(data);
  console.log(event.type, event);
};
pc.ontrack = ({ streams }) => {
  document.querySelector("audio").srcObject = streams[0];
};

await pc.setLocalDescription(await pc.createOffer());
await waitForIceGatheringComplete(pc);
const form = new FormData();
form.set("sdp", pc.localDescription.sdp);
form.set("session", JSON.stringify({ type: "realtime" }));
const response = await fetch("/v1/realtime/calls?model=meeting-agent", {
  method: "POST",
  body: form,
});
if (!response.ok) throw new Error(await response.text());
await pc.setRemoteDescription({ type: "answer", sdp: await response.text() });
```

`waitForIceGatheringComplete` 需要等待 `iceGatheringState === "complete"`。完整示例见 [`examples/openai_webrtc_demo`](../../examples/openai_webrtc_demo/)。

## DataChannel

- label 必须为 `oai-events`，只接受 UTF-8 JSON 文本；binary message 返回 `invalid_event`。
- DataChannel 打开后服务发送 `session.created`，随后接受与 [WebSocket 相同的客户端事件子集](openai-realtime.md)。
- 建连表单和 DataChannel 的 Session 音频输入格式接受 16/24 kHz 并原样回显；RTP 音轨仍依据媒体帧自身采样率转换到内部 16 kHz，不依赖该声明解释 RTP 数据。
- `input_audio_buffer.append` 在 WebRTC 返回 `unsupported_audio_event`；RTP 音轨是唯一输入。
- Agent 音频通过服务端 output track 播放，不发送 `response.output_audio.delta`；仍发送 transcript 和 response lifecycle 事件。
- `output_audio_buffer.clear` 只在 WebRTC Agent 有效，用于 barge-in 后丢弃待播音频。

## 挂断

`POST /v1/realtime/calls/{call_id}/hangup` 返回 `200`；不存在返回 `404 not_found`。PeerConnection failed/closed 会自动清理；disconnected 超过 `WEBRTC_DISCONNECTED_GRACE_SECONDS` 后清理。

<!-- API: POST /v1/realtime/calls/{call_id}/hangup -->

## ICE/TURN

服务端 `WEBRTC_ICE_SERVERS_JSON` 配置 STUN/TURN，URL 只允许 `stun:`、`turn:`、`turns:`，可带 username/credential。浏览器端也必须配置与网络拓扑匹配的 ICE server。凭据不通过 API 返回。

## 信令错误

| HTTP | code | 场景 |
| --- | --- | --- |
| `400` | `invalid_sdp/invalid_session/invalid_encoding/invalid_webrtc_offer` | multipart、JSON、UTF-8、音轨或 DataChannel 不合法 |
| `400` | `sdp_too_large/session_too_large/unsupported_x_aha_version` | 限制或扩展版本错误 |
| `408` | `negotiation_timeout` | set remote/create answer/set local 超时 |
| `429` | `capacity_exceeded` | WebRTC/Workflow 会话容量已满 |
| `404/412` | 通用错误 | model Route 不存在或目标未激活 |
