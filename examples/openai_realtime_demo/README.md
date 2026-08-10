# OpenAI Realtime 流式转写 Demo

这个 Demo 用同一套界面验证 OpenAI 官方 Realtime API 与 Audio Harness 的本地
兼容接口。浏览器只连接 TypeScript/Node Demo 后端，`OPENAI_API_KEY` 始终
保留在 Node 进程中，不会写入前端产物或返回给浏览器。后端使用 OpenAI
TypeScript SDK 的 `RealtimeClientEvent` 与 `RealtimeServerEvent` 类型约束
协议边界。

界面支持麦克风、任意 WAV 文件，以及仓库中 `data-bin/audio.wav` 的前 30 秒。
音频统一转换成 24 kHz、单声道 PCM S16LE，每 100 ms 发送一次
`input_audio_buffer.append`。停止输入时会继续发送 3 秒静音，让
`server_vad` 完成最后一个语音片段；第一版不会发送手动 `commit`。

每条转写仍会拆分显示“端到端首字”、“VAD 端点确认时间”、ASR 和 TPS。浏览器以
10 ms 音频帧的能量估算本地话音终点；“VAD 端点确认时间”是从这个近似终点到收到
服务端 `input_audio_buffer.speech_stopped` 的耗时。若本地未检测到有效话音，会回退到
服务端音频终点。TPS 按最终字符数除以 VAD 确认到整句完成的 ASR 阶段耗时计算。
协议事件面板默认折叠，需要排查原始事件时再从页面右侧展开。

## 安装与启动

进入 Demo 目录后，安装依赖并准备配置：

```bash
cd examples/openai_realtime_demo
pnpm install
cp .env.example .env
```

编辑 `.env` 后，一个命令同时启动 TypeScript 后端和 Vite 前端：

```bash
pnpm dev
```

Vite 默认监听 `0.0.0.0:5174`，后端监听 `0.0.0.0:8090`。本机页面位于
`https://127.0.0.1:5174`，
Vite 会把 `/api` 和 `/ws` 代理到 Demo 后端。按一次 `Ctrl+C` 即可一起停止
前后端。

生产方式运行时，先执行 `pnpm build`，再执行 `pnpm start`；此时后端会直接
托管构建后的页面，入口为 `https://127.0.0.1:8090`。

前端、Demo 后端与本地 Audio Harness 默认复用
`model-bin/realtime-tls/cert.pem` 和 `key.pem`。首次使用前先生成证书：

```bash
bash scripts/generate_realtime_tls.sh
```

从另一台 PC 通过 SSH 端口转发访问时，应把 `cert.pem` 安装为受信任证书，
然后使用 `https://127.0.0.1:5174`；不要关闭 TLS 校验。证书包含
`localhost` 与 `127.0.0.1` 的 SAN。

## 连接官方 API

编辑 Demo 的 `.env`：

```dotenv
OPENAI_API_KEY=你的官方密钥
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-transcribe
OPENAI_CA_CERT_FILE=
REALTIME_DEMO_TLS_CERT_FILE=model-bin/realtime-tls/cert.pem
REALTIME_DEMO_TLS_KEY_FILE=model-bin/realtime-tls/key.pem
```

专用转写 WebSocket 使用 `intent=transcription` 建立连接，ASR 模型只放在后续
`session.update` 中。Demo 对官方服务和 Audio Harness 使用完全相同的握手，不再提供
`model` 查询参数兼容模式。

## 连接本地 Audio Harness

先在仓库根目录生成开发证书并启动主程序：

```bash
bash scripts/generate_realtime_tls.sh
cp .env.example .env
set -a
. ./.env
set +a
poetry run audio-harness serve
```

然后把 Demo 的 `.env` 改为：

```dotenv
OPENAI_API_KEY=与REALTIME_API_KEY相同的值
OPENAI_BASE_URL=https://127.0.0.1:8000/v1
OPENAI_MODEL=gpt-4o-transcribe
OPENAI_CA_CERT_FILE=model-bin/realtime-tls/cert.pem
REALTIME_DEMO_TLS_CERT_FILE=model-bin/realtime-tls/cert.pem
REALTIME_DEMO_TLS_KEY_FILE=model-bin/realtime-tls/key.pem
```

重启 Demo 后生效。连接目标在 Demo 进程启动时固定，界面不会动态切换目标。
自签名证书通过 `OPENAI_CA_CERT_FILE` 加入信任链，Demo 不会关闭 TLS 校验。本地服务
同样通过 `intent=transcription` 建连，再从 `session.update` 读取并解析模型路由。
