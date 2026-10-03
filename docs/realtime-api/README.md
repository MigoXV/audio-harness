# Realtime API 接入

Audio Harness 在同一套 OpenAI Realtime 事件模型上提供 WebSocket、WebRTC 与 [aha-grpc](aha-grpc.md)。WebSocket 适合服务端、脚本和调试；WebRTC 适合浏览器/移动端，音频走 RTP、事件走 DataChannel。

OpenAI 官方资料：[WebSocket](https://developers.openai.com/api/docs/guides/realtime-websocket)、[WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc)、[Conversation](https://developers.openai.com/api/docs/guides/realtime-conversations)、[VAD](https://developers.openai.com/api/docs/guides/realtime-vad)、[客户端事件](https://platform.openai.com/docs/api-reference/realtime-client-events)、[服务端事件](https://platform.openai.com/docs/api-reference/realtime-server-events)。这些链接定义上游完整能力；本项目只承诺[已实现事件子集](openai-realtime.md)。

## 连接模式

WebSocket endpoint：`GET ws://localhost:8000/v1/realtime`。必须且只能选择一种模式：

| 模式 | Query | 前置条件 | 首个事件 |
| --- | --- | --- | --- |
| 动态纯转写 | `intent=transcription` | 后续 `session.update` 指定一个指向已激活 Audio Workflow 的 public Route model | `session.created` transcription；未配置 model 前不能发音频 |
| Voice Agent | `model=<public-agent-route>` | Route 存在、public、目标为已激活 Voice Agent | `session.created` realtime |

可选 query：

- `x_aha=v1`：启用 [AHA 扩展](aha-v1.md)；缺省只发 OpenAI 原生事件。
- `diagnostics=true`：打开服务端诊断行为；不应作为业务协议依赖。

示例：

```text
ws://localhost:8000/v1/realtime?intent=transcription&x_aha=v1
ws://localhost:8000/v1/realtime?model=meeting-agent
```

当前应用本身不校验 OpenAI API Key 或 `Authorization`；生产环境认证由部署网关提供。不要把 OpenAI 官方服务的鉴权规则误套到本地 endpoint。

## 建连拒绝

| close code | 原因 |
| --- | --- |
| `4400` | intent/model 缺失或同时提供、intent 非 transcription、AHA 版本不支持、model 不是 Agent Route |
| `4404` | model Route 不存在或不可见 |
| `4409` | Route 目标尚未激活 |
| `4429` | Realtime 会话容量已满 |

建连成功后，单个业务事件失败通常返回 `error`，连接保持可用；客户端应记录 `error.code` 并根据 `error.event_id` 关联请求。

## 最小动态转写

```json
{"type":"session.update","event_id":"cfg-1","session":{"type":"transcription","audio":{"input":{"format":{"type":"audio/pcm","rate":24000},"transcription":{"model":"meeting-transcribe","language":"zh-CN"},"turn_detection":{"type":"server_vad"}}}}}
```

收到 `session.updated` 后循环发送：

```json
{"type":"input_audio_buffer.append","audio":"<base64-pcm-s16le>"}
```

输入 PCM 也可在 Session 中声明 `rate:16000`；未声明时继续使用标准的 24 kHz。客户端发送的实际 PCM 采样率必须与 Session 配置一致。

自动 VAD 会产生 speech started/stopped、commit、conversation item 和 transcription 事件。若 `turn_detection:null`，客户端需发送 `input_audio_buffer.commit`。

完整 Python 示例见 [`examples/openai_realtime_python_demo`](../../examples/openai_realtime_python_demo/)。Voice Agent 示例见 [`examples/openai_realtime_voice_demo`](../../examples/openai_realtime_voice_demo/)。

## 进一步阅读

- [Python SDK 构建、安装与调用](python-sdk.md)
- [aha-grpc TLS 双向流与强类型协议](aha-grpc.md)
- [OpenAI 原生兼容子集](openai-realtime.md)
- [AHA v1 事件](aha-v1.md)
- [WebRTC 信令与媒体](webrtc.md)
- [典型事件时序](event-flows.md)
- [Realtime 错误](../reference/errors.md)
