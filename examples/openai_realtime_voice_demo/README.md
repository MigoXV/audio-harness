# Realtime 服务端语音对话 Demo

这个 Demo 使用 Audio Harness 带 `model` 查询参数的普通 OpenAI Realtime
WebSocket 入口进行语音对话，
但浏览器不会直接调用 Realtime API，也不会持有 API Key 或组装任何
`session.update`、`input_audio_buffer.append`、`response.create` 事件。

浏览器和 Demo 后端之间只有一条受限协议：

- 浏览器向后端发送 24 kHz、单声道 PCM S16LE 二进制帧；
- 浏览器只能发送 `stop`、`clear`、`disconnect` 三种 JSON 控制消息；
- Node 后端持有 `REALTIME_API_KEY`，创建并维护上游 Realtime WebSocket；
- 后端负责 `session.update`、音频事件封装、服务端事件解析和打断状态；
- 上游语音 PCM 由后端以二进制转发给浏览器播放，界面只接收整理后的高层状态。

因此，这个示例适合验证上一层业务服务如何托管完整语音智能体会话，而不是把
Realtime 协议和密钥下放给浏览器。

语音智能体入口支持显式协商 `x_aha=v1`，但该 Demo 刻意不传入扩展参数，
用于验证默认 OpenAI Realtime 原生协议保持兼容。Demo 也不依赖已删除的
`audio_harness.tts_first_audio` 或任何其他 `audio_harness.*` 事件；首个可播放音频块直接来自
`response.output_audio.delta`。

## 前置条件

先在 Audio Harness 控制面创建并激活语音智能体，确保其语音工作流、
Responses 和 TTS 依赖都已就绪，再为它绑定一个公开 Realtime Route。
`REALTIME_MODEL` 填这个公开路由名，不能填内部资源 ID，也不能指向纯转写工作流。

在仓库根目录准备证书并启动服务：

```bash
bash scripts/generate_realtime_tls.sh
poetry run audio-harness serve
```

## 安装与启动

```bash
cd examples/openai_realtime_voice_demo
pnpm install
cp .env.example .env
```

编辑 `.env`：

```dotenv
REALTIME_API_KEY=local-demo-key
REALTIME_BASE_URL=https://127.0.0.1:8000/v1
REALTIME_MODEL=my-voice-agent
REALTIME_CA_CERT_FILE=model-bin/realtime-tls/cert.pem
REALTIME_VOICE=wise
REALTIME_INSTRUCTIONS=你是一个自然、简洁、有帮助的中文语音助手。
VOICE_DEMO_TLS_CERT_FILE=model-bin/realtime-tls/cert.pem
VOICE_DEMO_TLS_KEY_FILE=model-bin/realtime-tls/key.pem
VOICE_DEMO_HOST=0.0.0.0
VOICE_DEMO_PORT=8091
```

Demo 后端要求 `REALTIME_API_KEY` 非空，并会将其作为 Bearer Token 发送。
直连本地 Audio Harness 时可使用示例中的占位值；如果数据面前还有鉴权网关，
则改为网关要求的服务端密钥。

启动：

```bash
pnpm dev
```

打开 `https://127.0.0.1:8091`。点击“开始对话”并允许麦克风权限即可持续交谈；
用户重新开口时，Audio Harness 会取消旧 Responses/TTS 流，Demo 同时清空浏览器
里尚未播放的音频。

## 验证

```bash
pnpm lint
pnpm test
pnpm build
```

实现遵循 OpenAI Realtime WebSocket 的音频事件生命周期：服务端发送
`session.update` 和 `input_audio_buffer.append`，并消费
`response.output_audio.delta` 及音频转写事件。协议参考：
[Realtime WebSocket](https://developers.openai.com/api/docs/guides/realtime-websocket) 和
[Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations#handling-audio-with-websockets)。
