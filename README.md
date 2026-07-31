# Audio Harness

Audio Harness 是面向实时音频智能的可编排基础设施。当前版本以有向无环图
（DAG）描述语音处理链：控制面用 SQLite 管理引擎资产、处理链工作副本和
不可变激活修订；数据面按已激活执行计划，为每个会话启动带有界边队列的异步
Graph Runner。服务同时提供 gRPC，以及兼容 OpenAI Realtime 转写事件的
WebSocket 和 WebRTC 接口。

## 架构

```text
                  HTTP/HTTPS/WSS :8000
             ┌───────────────────────────┐
             │ DAG 工作副本 / 引擎资产  │
             │ 校验 / 激活修订 / Web UI │
             └────────────┬──────────────┘
                          │ SQLite
                          ▼
                 NetworkX 编译与校验
                          │
                          ▼
                   不可变执行计划
                          │
音频客户端 ─ gRPC :50051 ─┐
Realtime WS ─── WSS :8000 ─┼─ 会话 Graph Runner ─ 有界边队列 ─ 会话输出
WebRTC ──── HTTPS/RTP :8000┘          │
                         音频输入 → 物理 VAD → 可选语义 VAD → ASR
                                      └────────事件──────────→ 输出
```

主业务管线只依赖 `inferencers/contracts.py` 中的异步能力接口。各引擎的
gRPC 请求构造、响应解析、错误映射和通道生命周期封装在
`inferencers/grpc/asr.py` 与 `inferencers/grpc/vad.py`，不会泄漏到工作流
逻辑中，后续可以增加其他推理协议。

NetworkX 只参与控制面的结构校验、拓扑排序和执行计划编译，不进入音频热路径。
节点端口带有载荷类型；输出可以扇出，多路输入只允许进入显式声明为聚合端口的
节点。当前内置 `audio_input`、`physical_vad`、`semantic_vad`、`asr` 和
`session_output` 五类节点，节点注册表保留了继续扩展新能力的边界。

每个新会话获取同一份不可变处理链修订、引擎快照和全局运行设置。激活新修订
后，后续会话使用新执行计划，活动会话继续使用开始时的计划。gRPC、Realtime
WebSocket 和 WebRTC 统一执行同一张图；各协议拥有独立会话容量，但共享全局
ASR 并发限制。

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

升级前应停止旧进程并备份数据库：

```bash
mkdir -p data-bin/sqlite/before-serving-refactor
mv data-bin/sqlite/audio-harness.db* \
  data-bin/sqlite/before-serving-refactor/
```

服务启动时会自动执行 Alembic。升级迁移会把旧角色绑定转换成默认 DAG，把
旧物理 VAD 参数迁入对应节点；旧绑定不完整时只创建工作副本，不会伪造一个
可运行修订。默认入口：

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

`src/web` 是同源 React 配置工作台。默认入口是处理链编排：查看当前工作副本
和运行修订、编辑节点与类型兼容的连线、配置节点参数和引擎，并依次完成保存、
校验和激活。完整引擎 CRUD 位于独立“引擎库”，运行设置只管理全局传输、队列
和容量边界。开发模式：

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
| `GET` | `/api/v1/node-types` | 查询节点类型、端口和配置 Schema |
| `GET` | `/api/v1/pipeline` | 查询处理链工作副本和当前激活修订 |
| `PUT` | `/api/v1/pipeline/draft` | 携带工作副本 `revision` 保存整张图 |
| `POST` | `/api/v1/pipeline/validate` | 编译并校验当前工作副本 |
| `POST` | `/api/v1/pipeline/activate` | 准备引擎并原子激活不可变修订 |
| `GET` | `/api/v1/pipeline/active` | 查询当前激活修订 |
| `GET` | `/api/v1/pipeline/revisions/{revision}` | 查询指定历史修订 |
| `GET` | `/api/v1/workflow-settings` | 读取完整工作流设置快照 |
| `PUT` | `/api/v1/workflow-settings` | 携带 `revision` 原子更新设置 |
| `GET` | `/api/v1/capacity-settings` | 读取协议与共享容量快照 |
| `PUT` | `/api/v1/capacity-settings` | 携带 `revision` 原子更新容量 |

保存工作副本不会改变数据面。校验会检查无环、端口类型、必需输入、非法隐式
汇合、输入/输出可达性、节点配置、失败策略和引擎接口兼容性。激活阶段还会
等待所有引用的 gRPC 引擎 ready，并再次确认资产修订未变化；任何失败都会保留
当前运行修订。没有激活处理链时，`/health/ready` 返回未就绪。

工作流策略由 SQLite 单例记录保存，默认值包括：

| 策略 | 默认值 |
| --- | ---: |
| 输入重分块 | 640 字节 |
| 输入 / 片段 / 结果队列 | 32 / 8 / 32 |
| 默认语言 | `zh-CN` |

最短/最长片段、环形缓冲和语音前滚属于 `physical_vad` 节点配置，不再是
全局工作流设置。容量设置由独立 SQLite 单例记录保存：gRPC、Realtime
WebSocket 和 WebRTC
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

`model` 是兼容协议中的会话标识；实际推理由当前激活处理链引用的本地引擎
执行。图中存在语义 VAD 节点时，Realtime 也会执行该节点。

自定义诊断事件默认关闭。连接 URL 增加 `diagnostics=true` 后，会额外发送
`audio_harness.vad_progress`；以后新增的 `audio_harness.*` 诊断事件也统一受
此开关控制：

```text
wss://127.0.0.1:8000/v1/realtime?model=gpt-4o-transcribe&diagnostics=true
```

## OpenAI Realtime WebRTC

WebRTC 使用官方 calls 形态。应用后端以 Bearer Token 提交 multipart 表单，
其中 `sdp` 是完整 offer，`session` 是可选的 transcription session JSON：

```text
POST /v1/realtime/calls
POST /v1/realtime/calls/{call_id}/hangup
```

创建 call 时同样可通过 `/v1/realtime/calls?diagnostics=true` 开启诊断事件；
默认不会在 `oai-events` DataChannel 中发送自定义诊断事件。

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

请求音频进入当前激活处理链。默认图由物理 VAD 形成完整片段，再交给可选的
语义 VAD 和 ASR；环形缓冲与语音前滚参数由 `physical_vad` 节点配置。示例：

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
