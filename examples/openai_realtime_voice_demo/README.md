# Realtime 服务端语音对话 Demo

这个 Demo 完整使用 Audio Harness 的 OpenAI Realtime 兼容入口进行语音对话，
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

## 前置条件

先在 Audio Harness 控制面创建并激活语音智能体，为它绑定公开模型名。
该工作流需要包含 VAD、ASR、Responses 和双向流式 TTS 节点；TTS 可使用
`192.168.0.222:50005` 和预置音色 `wise`。

在仓库根目录准备证书并启动服务：

```bash
bash scripts/generate_realtime_tls.sh
set -a
. ./.env
set +a
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
REALTIME_API_KEY=与主服务 REALTIME_API_KEY 相同的值
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

`REALTIME_MODEL` 必须是控制面中绑定到已激活语音智能体的公开模型名，
不能使用内部保留的 `grpc-default`。

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
[Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations#handling-audio-with-websockets)。
