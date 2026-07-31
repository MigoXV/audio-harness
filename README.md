# Audio Harness

Audio Harness 是面向实时音频智能的可编排基础设施。当前第一版聚焦 ASR 与
VAD：控制面用 SQLite 管理引擎数字资产、角色绑定和工作流策略；数据面用
异步有界队列处理持续音频流，在物理 VAD 确认完整语音片段后，把整个片段交给
ASR。服务同时提供 gRPC，以及兼容 OpenAI Realtime 转写事件的 WebSocket
和 WebRTC 接口。

## 架构

```text
                 HTTP/HTTPS/WSS :8000
                ┌───────────────────────┐
                │ 引擎 CRUD / 角色绑定 │
                │ 策略 / 容量 / Web UI │
                └──────────┬────────────┘
                           │ SQLite
                           ▼
音频客户端 ── gRPC :50051 ─┐
                           ├─ 会话快照 ─ 环形缓冲 ─ VAD ─ 完整片段 ─ ASR
Realtime WS ───── WSS :8000─┤                   │
WebRTC ───── HTTPS/RTP :8000┘                   │
                                               └─ 可选语义 VAD（仅 gRPC）
```

主业务管线只依赖 `inferencers/contracts.py` 中的异步能力接口。各引擎的
gRPC 请求构造、响应解析、错误映射和通道生命周期封装在
`inferencers/grpc/asr.py` 与 `inferencers/grpc/vad.py`，不会泄漏到工作流
逻辑中，后续可以增加其他推理协议。

每个新会话会获取不可变的引擎路由和工作流设置快照。控制面修改绑定或策略后，
新会话立即使用新配置；活动会话继续使用开始时的配置。gRPC 与 Realtime 各自
拥有独立的会话容量，但共享全局 ASR 并发限制；降低容量不会取消已经运行的
任务。

对外协议统一位于 `audio_harness.serving`：`serving/grpc` 只处理 protobuf
与 50051 生命周期，`serving/http` 组装 8000 上的 REST、Web 与 WebSocket，
`serving/realtime` 只处理 OpenAI 事件，`serving/webrtc` 负责 SDP、ICE、
RTP 音轨和 DataChannel。三种入口都调用协议无关的
`TranscriptionSession`；下游模型连接继续隔离在 `inferencers/grpc`。

## 安装与启动

要求 Python 3.10、Poetry、Node.js 20+、pnpm 和 OpenSSL。

```bash
poetry env use python3.10
poetry install
pnpm --dir src/web install
pnpm --dir src/web build
bash scripts/generate_realtime_tls.sh
cp .env.example .env
```

编辑 `.env`，至少把 `AUDIO_HARNESS_REALTIME_API_KEY` 换成本地客户端使用的
随机密钥。主程序不加载 `.env` 文件，启动前需要把它导入进程环境：

```bash
set -a
. ./.env
set +a
poetry run audio-harness serve
```

当前原型不兼容旧的设置表。首次切换到本版本前，停止旧进程并备份旧数据库：

```bash
mkdir -p data-bin/sqlite/before-serving-refactor
mv data-bin/sqlite/audio-harness.db* \
  data-bin/sqlite/before-serving-refactor/
```

服务启动时会自动从头创建 SQLite 并执行 Alembic 初始化。默认入口：

- gRPC 数据面：`0.0.0.0:50051`，第一版保持明文；
- REST、Web 工作台与 Realtime：`https://0.0.0.0:8000`；
- OpenAPI：`https://127.0.0.1:8000/docs`；
- Realtime WS：`wss://127.0.0.1:8000/v1/realtime?model=gpt-4o-transcribe`；
- Realtime WebRTC：`POST https://127.0.0.1:8000/v1/realtime/calls`；
- SQLite：`data-bin/sqlite/audio-harness.db`。

开发证书和私钥生成到已忽略的 `model-bin/realtime-tls`，不会提交到 Git。
生成脚本遇到已有文件会拒绝覆盖。生产环境应换成受信任证书，并把未鉴权的
REST 控制面放在可信网络或带访问控制的反向代理后。

## 环境变量

YAML 只保存 SQLite URL，监听与服务参数全部通过命令行或环境变量设置。命令行
优先于环境变量，环境变量优先于默认值。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `AUDIO_HARNESS_CONFIG` | 无 | 静态 YAML 路径 |
| `AUDIO_HARNESS_GRPC_HOST` | `0.0.0.0` | gRPC 监听地址 |
| `AUDIO_HARNESS_GRPC_PORT` | `50051` | gRPC 监听端口 |
| `AUDIO_HARNESS_MAX_MESSAGE_BYTES` | `4194304` | gRPC/Realtime 单消息上限 |
| `AUDIO_HARNESS_GRPC_SHUTDOWN_GRACE_SECONDS` | `5` | gRPC 关闭宽限 |
| `AUDIO_HARNESS_HTTP_HOST` | `0.0.0.0` | HTTP/HTTPS/WSS 监听地址 |
| `AUDIO_HARNESS_HTTP_PORT` | `8000` | HTTP/HTTPS/WSS 监听端口 |
| `AUDIO_HARNESS_HTTP_SHUTDOWN_GRACE_SECONDS` | `5` | HTTP/WS 关闭宽限 |
| `AUDIO_HARNESS_HTTP_TLS_CERT_FILE` | `model-bin/realtime-tls/cert.pem` | TLS 证书 |
| `AUDIO_HARNESS_HTTP_TLS_KEY_FILE` | `model-bin/realtime-tls/key.pem` | TLS 私钥 |
| `AUDIO_HARNESS_REALTIME_API_KEY` | 无 | 本地 WSS Bearer Token |
| `AUDIO_HARNESS_WEBRTC_ICE_SERVERS_JSON` | `[]` | STUN/TURN 服务器数组 |
| `AUDIO_HARNESS_WEBRTC_NEGOTIATION_TIMEOUT_SECONDS` | `10` | SDP 协商超时 |
| `AUDIO_HARNESS_WEBRTC_DISCONNECTED_GRACE_SECONDS` | `10` | 断线等待宽限 |
| `AUDIO_HARNESS_LOG_LEVEL` | `INFO` | 日志级别 |

## 控制面

`src/web` 是同源 React 配置工作台。它用于管理 ASR/VAD 引擎、设置
`asr`、`vad`、`semantic_vad` 三个运行时角色，以及分别修改工作流与容量
设置。开发模式：

```bash
pnpm --dir src/web dev
```

Vite 页面位于 `http://127.0.0.1:5173`，会把 API 请求代理到管理端口。生产
访问前必须执行 `pnpm --dir src/web build`。

主要 REST 接口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/v1/engines` | 创建 ASR/VAD 引擎资产 |
| `GET` | `/api/v1/engines` | 分页、过滤和搜索资产 |
| `GET/PATCH/DELETE` | `/api/v1/engines/{id}` | 查询、更新、删除资产 |
| `GET` | `/api/v1/engine-bindings` | 查询运行时角色绑定 |
| `PUT/DELETE` | `/api/v1/engine-bindings/{role}` | 探活切换或解除绑定 |
| `GET` | `/api/v1/workflow-settings` | 读取完整工作流设置快照 |
| `PUT` | `/api/v1/workflow-settings` | 携带 `revision` 原子更新设置 |
| `GET` | `/api/v1/capacity-settings` | 读取协议与共享容量快照 |
| `PUT` | `/api/v1/capacity-settings` | 携带 `revision` 原子更新容量 |

绑定前会异步等待目标 gRPC 通道 ready。失败时返回 503 并保留旧绑定；停用、
接口类型不匹配和删除已绑定资产等冲突返回 409。首次启动不会擅自绑定知识库
中的历史引擎；必须至少绑定 `asr` 和 `vad`，`/health/ready` 才会就绪。

工作流策略由 SQLite 单例记录保存，默认值包括：

| 策略 | 默认值 |
| --- | ---: |
| 输入重分块 | 6400 字节 |
| 最短 / 最长语音片段 | 0.5 / 60 秒 |
| 每会话音频环形缓冲 | 120 秒 |
| VAD 语音前滚 | 0.3 秒 |
| 输入 / 片段 / 结果队列 | 32 / 8 / 32 |
| 默认语言 | `zh-CN` |

容量设置由独立 SQLite 单例记录保存：gRPC、Realtime WebSocket 和 WebRTC
默认各允许 64 个活动会话，所有协议共享 8 个 ASR 并发。两类设置的完整 PUT
都使用各自的乐观锁；旧 `revision` 会返回 409，客户端应重新读取后再提交。

## OpenAI Realtime WSS

客户端使用 `Authorization: Bearer <AUDIO_HARNESS_REALTIME_API_KEY>` 连接：

```text
wss://127.0.0.1:8000/v1/realtime?model=gpt-4o-transcribe
```

第一版只支持 transcription session、24 kHz 单声道 PCM S16LE 和
`server_vad`。输入会连续重采样为引擎使用的 16 kHz，不能发送手动
`input_audio_buffer.commit`。主程序使用 OpenAI Python SDK 的官方事件模型
完成校验和序列化。

支持的客户端事件：

- `session.update`；
- `input_audio_buffer.append`；
- `input_audio_buffer.clear`。

主要服务端事件：

- `session.created`、`session.updated`；
- `input_audio_buffer.speech_started`、`speech_stopped`、`committed`；
- `conversation.item.created`；
- `conversation.item.input_audio_transcription.delta`、`completed`、`failed`；
- `error`。

`model` 是兼容协议中的会话标识；实际推理由控制面当前绑定的本地 ASR/VAD
引擎执行。语义 VAD 不参与 Realtime 第一版的 `server_vad` 管线。

## OpenAI Realtime WebRTC

WebRTC 使用官方 calls 形态。应用后端以 Bearer Token 提交 multipart 表单，
其中 `sdp` 是完整 offer，`session` 是可选的 transcription session JSON：

```text
POST /v1/realtime/calls
POST /v1/realtime/calls/{call_id}/hangup
```

浏览器音频通过 RTP 发送，服务端连续重采样为 16 kHz PCM 后进入同一套
VAD + ASR 工作流；协议事件通过名为 `oai-events` 的 DataChannel 返回。第一版
仅接收一条音频输入轨道，同时会协商一条持续静音的服务端音频输出轨道，为以后
追加 TTS 留出兼容方向。

同机与同一局域网通常可以不配置 ICE server。跨 NAT 部署时通过
`AUDIO_HARNESS_WEBRTC_ICE_SERVERS_JSON` 配置外部 STUN/TURN，例如：

```json
[{"urls":"stun:stun.example.com:3478"},{"urls":"turn:turn.example.com:3478","username":"user","credential":"secret"}]
```

WebRTC 会话额度与 gRPC、Realtime WebSocket 分离，但仍共享全局 ASR 并发。

## Realtime Demo 与官方/本地对照

独立 Demo 位于 `examples/openai_realtime_demo`。它的 TypeScript/Node 后端
保存 API Key，浏览器只收发 OpenAI TypeScript SDK 定义的事件类型。界面支持
麦克风、WAV 文件以及 `data-bin/audio.wav` 的前 30 秒。

```bash
cd examples/openai_realtime_demo
pnpm install
cp .env.example .env
pnpm dev
```

详细的官方配置和本地 CA 信任方式见
[Demo 文档](examples/openai_realtime_demo/README.md)。

更精简的 WebRTC 麦克风 Demo 位于 `examples/openai_webrtc_demo`。API Key
保留在 Vite 开发服务器侧，浏览器只建立 PeerConnection：

```bash
cd examples/openai_webrtc_demo
cp .env.example .env
pnpm install
pnpm dev
```

## gRPC

对外接口为 `ux_speech.UxSpeech/StreamingRecognize`。首包只能携带
`StreamingRecognitionConfig`，后续包只能携带 `audio_content`。第一版支持
单声道 PCM S16LE，未指定采样率时默认 16 kHz。

VAD 判定完整片段后只发起一次 ASR 请求，避免底层引擎收到多条
`audio_content`。120 秒可轮转环形缓冲和 0.3 秒语音前滚用于保留 VAD 起点
之前的音频。示例：

```bash
poetry run python examples/grpc_client.py \
  data-bin/audio.wav \
  --target 127.0.0.1:50051
```

## 生成协议代码

```bash
bash scripts/run_grpcio_tools.sh
```

脚本使用与当前 `grpcio` 匹配的 `grpcio-tools`，生成 protobuf、gRPC 和类型
声明。

## 验证

```bash
poetry check
poetry run pytest
poetry run python -m audio_harness.commands.app --help
pnpm --dir src/web lint
pnpm --dir src/web test
pnpm --dir src/web build
pnpm --dir examples/openai_realtime_demo lint
pnpm --dir examples/openai_realtime_demo test
pnpm --dir examples/openai_realtime_demo build
pnpm --dir examples/openai_webrtc_demo lint
pnpm --dir examples/openai_webrtc_demo build
```

自动化测试使用临时 SQLite 与进程内假引擎，不会连接知识库中的真实服务。
