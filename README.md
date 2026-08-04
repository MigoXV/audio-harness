# Audio Harness

Audio Harness 是面向实时音频智能的可编排基础设施。第二代控制面使用受约束的
阶段式工作流描述语音处理链，用户不再直接编辑节点坐标和任意连线；发布时才把
阶段定义编译成内部有向无环图（DAG）与不可变执行计划。数据面为每个会话启动
带有界边队列的异步 Graph Runner。服务同时提供 gRPC，以及兼容 OpenAI Realtime 的 WebSocket
和 WebRTC 接口。Realtime 既可路由到转写工作流，也可路由到由 Responses API
和双向流式 TTS 组成的语音 Agent 工作流；LLM 与 TTS 不单独对外暴露。

## 架构

```text
                  HTTP/HTTPS/WSS :8000
             ┌───────────────────────────┐
             │ 阶段草稿 / 引擎资产      │
             │ 自动保存 / 发布 / Web UI │
             └────────────┬──────────────┘
                          │ SQLite
                          ▼
                 NetworkX 编译与校验
                          │
                          ▼
                   不可变执行计划
                          │
音频客户端 ─ gRPC :50051 ─┐
Realtime WS ─── WSS :8000 ─┼─ 模型路由 ─ 工作流快照 ─ 会话输出
WebRTC ──── HTTPS/RTP :8000┘                    │
        音频输入 → 物理 VAD → 可选语义 VAD → ASR ─┬─ 转写输出
                                                    └─ Responses SSE
                                                        │ 文本 delta
                                                        ▼
                                                  双向流式 TTS
```

主业务管线只依赖 `inferencers/contracts.py` 中的异步能力接口。各引擎的
gRPC 请求构造、响应解析、错误映射和通道生命周期封装在
`inferencers/grpc/asr.py`、`inferencers/grpc/vad.py`、
`inferencers/grpc/tts.py` 与 `inferencers/http/responses.py`，不会泄漏到
传输协议层。LLM 只实现 Responses API；TTS 只实现 `DuplexSynthesize`。

NetworkX 只参与服务端的结构校验、拓扑排序和执行计划编译，不进入音频热路径。
节点端口带有载荷类型；输出可以扇出，多路输入只允许进入显式声明为聚合端口的
节点。当前内置 `audio_input`、`physical_vad`、`semantic_vad`、`asr` 和
`responses_agent`、`duplex_tts` 和 `session_output` 七类节点。

每个新会话获取同一份不可变处理链修订、引擎快照和全局运行设置。发布新修订
后，后续会话使用新执行计划，活动会话继续使用开始时的计划。每个工作流使用
不可变 slug 作为默认公开模型名，并可独立自动保存、校验和发布。gRPC 没有模型选择字段，因此
始终解析内部保留模型名 `grpc-default`；该路由不能从 Realtime 使用。各协议
拥有独立会话容量，并共享 ASR、Responses 和 TTS 三类并发保护。

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

按部署需要编辑 `.env`。主程序不加载 `.env` 文件，启动前需要把它导入进程环境：

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

第二代控制面有意不原地迁移旧业务数据。首次切换前必须停止旧进程，并人工移走
SQLite 主文件及同名 `-wal`、`-shm` 文件；服务检测到第二代之前的 Alembic
版本会拒绝启动，不会自动删除或覆盖数据。移走旧库后，服务会执行 Alembic 并
创建阶段式默认转写工作流。默认入口：

- gRPC 数据面：`0.0.0.0:50051`，第一版保持明文；
- REST、Web 工作台与 Realtime：`https://0.0.0.0:8000`；
- OpenAPI：`https://127.0.0.1:8000/docs`；
- Realtime WS：`wss://127.0.0.1:8000/v1/realtime?model=default-transcription`；
- Realtime WebRTC：`POST https://127.0.0.1:8000/v1/realtime/calls`；
- SQLite：`data-bin/sqlite/audio-harness.db`。

进程启动不会等待 ASR、VAD、Responses、TTS 引擎就绪，因此单个推理服务故障
不会阻断控制面、HTTP、Realtime 或 gRPC 监听。启动后后台健康监视器每 60 秒
检查到期资产；连续失败按 60、120、240、300 秒退避，并持久化最近成功时间、
延迟和失败次数。`/health/ready` 只检查数据库以及 `grpc-default` 是否指向已发布
工作流，不会临时连接推理引擎。发布会重新探测草稿引用的全部引擎，任何失败都
保留当前运行修订。

开发证书和私钥生成到已忽略的 `model-bin/realtime-tls`，不会提交到 Git。
生成脚本遇到已有文件会拒绝覆盖。生产环境应换成受信任证书，并把未鉴权的
REST 控制面放在可信网络或带访问控制的反向代理后。

## 环境变量

监听与服务参数全部通过命令行或环境变量设置。命令行优先于环境变量，环境变量
优先于默认值。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data-bin/sqlite/audio-harness.db` | SQLite 数据库 URL |
| `VOICE_STORAGE_DIR` | `./data-bin/voices` | 参考音色 WAV 存储目录 |
| `GRPC_HOST` | `0.0.0.0` | gRPC 监听地址 |
| `GRPC_PORT` | `50051` | gRPC 监听端口 |
| `MAX_MESSAGE_BYTES` | `4194304` | gRPC/Realtime 单消息上限 |
| `HTTP_HOST` | `0.0.0.0` | HTTP/HTTPS/WSS 监听地址 |
| `HTTP_PORT` | `8000` | HTTP/HTTPS/WSS 监听端口 |
| `HTTP_TLS_CERT_FILE` | `model-bin/realtime-tls/cert.pem` | TLS 证书 |
| `HTTP_TLS_KEY_FILE` | `model-bin/realtime-tls/key.pem` | TLS 私钥 |
| `WEBRTC_ICE_SERVERS_JSON` | `[]` | STUN/TURN 服务器数组 |
| `WEBRTC_NEGOTIATION_TIMEOUT_SECONDS` | `10` | SDP 协商超时 |
| `WEBRTC_DISCONNECTED_GRACE_SECONDS` | `10` | 断线等待宽限 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 控制面

`src/web` 是面向桌面端的同源 React 工作台，信息架构分为“工作流”“推理引擎”
和“运行状态”。工作流详情按概览、构建、测试、发布、历史组织；构建页只展示
真实可执行的阶段拓扑，修改后约 800 ms 自动保存，发布是唯一改变数据面的动作。
开发模式：

```bash
pnpm --dir src/web dev
```

Vite 页面位于 `http://127.0.0.1:5173`，会把 API 请求代理到管理端口。生产
访问前必须执行 `pnpm --dir src/web build`。

### 构建全双工双模态 Agent

完整链路为“麦克风 → 物理 VAD → 可选语义 VAD → ASR → Responses → 双工 TTS”。启用语义 VAD 后，物理 VAD 只负责即时开口、候选边界和打断 TTS；只有语义 VAD 确认轮次结束才会提交音频并开始转写。语义 VAD 默认最多缓存 60 秒：缓存与新候选片段超过上限时先硬提交已有缓存；单个超长候选片段会按上限连续硬切提交。
ASR 转写和模型文本作为 DataChannel/WebSocket 事件持续返回，TTS 音频同时通过
WebRTC 远端音轨或 WSS PCM delta 播放；用户重新开口会取消当前 Responses/TTS，
实现打断式全双工对话。

1. 在“推理引擎”中分别登记 VAD、ASR、Responses 和 TTS。Responses 的 API Key
   直接保存在 SQLite 并可在控制台回显；当前部署假设处于可信网络，界面会持续
   显示明文风险提示。TURN 和 MCP 请求头仍只来自环境或会话。
2. 在“工作流”中新建“语音 Agent”，slug 会成为不可变的公开模型名。
3. 在“构建”中绑定各阶段引擎，填写 Responses 模型、系统指令、工具/MCP，以及
   TTS 允许音色和默认音色。可按需启用语义 VAD。
4. 等待顶部保存状态变为“已保存”，进入“发布”检查缺项和引擎健康，然后执行
   一次原子发布。发布失败不会替换当前运行版本。
5. 在“测试”中选择 WSS 或 WebRTC 并授权麦克风；测试不会持久化录音、转写或模型输出。

主要 REST 接口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/v1/engines` | 创建 ASR、VAD、Responses 或 TTS 引擎资产 |
| `GET` | `/api/v1/engines` | 分页、过滤和搜索资产 |
| `POST` | `/api/v1/engines/validate` | 手动探测全部已启用引擎 |
| `GET/PATCH/DELETE` | `/api/v1/engines/{id}` | 查询、更新、删除资产 |
| `POST` | `/api/v1/engines/{id}/probe` | 立即探测单个引擎并保存健康状态 |
| `GET` | `/api/v1/engines/{id}/usage` | 查询删除前的工作流引用 |
| `GET/POST` | `/api/v1/workflows` | 列出或创建工作流 |
| `GET/PATCH/DELETE` | `/api/v1/workflows/{id}` | 管理工作流元数据 |
| `GET/PUT` | `/api/v1/workflows/{id}/draft` | 读取或乐观锁保存阶段草稿 |
| `GET` | `/api/v1/workflows/{id}/status` | 查询缺项、阶段健康和发布状态 |
| `POST` | `/api/v1/workflows/{id}/validate` | 校验指定工作流 |
| `POST` | `/api/v1/workflows/{id}/publish` | 探测引擎并原子发布草稿 |
| `GET` | `/api/v1/workflows/{id}/revisions` | 查询不可变发布历史 |
| `POST` | `/api/v1/workflows/{id}/revisions/{revision}/restore` | 把历史版本恢复为新草稿 |
| `GET` | `/api/v1/model-routes` | 列出公开模型名与内部 gRPC 路由 |
| `GET/POST/DELETE` | `/api/v1/voices` | 管理参考 WAV 音色资产 |
| `GET` | `/api/v1/runtime/status` | 查询当前并发、容量和累计拒绝数 |
| `GET` | `/api/v1/workflow-settings` | 读取完整工作流设置快照 |
| `PUT` | `/api/v1/workflow-settings` | 携带 `revision` 原子更新设置 |
| `GET` | `/api/v1/capacity-settings` | 读取协议与共享容量快照 |
| `PUT` | `/api/v1/capacity-settings` | 携带 `revision` 原子更新容量 |

保存草稿不会改变数据面。服务端根据工作流种类生成固定边界与合法连线，并检查
阶段必填项、节点配置、失败策略和引擎接口兼容性。发布会等待全部引用引擎就绪，
并再次确认资产修订未变化；任何失败都会保留当前运行修订。没有已发布转写工作流
时，`/health/ready` 返回未就绪。

gRPC 引擎健康检查执行 channel-ready 探测，Responses 引擎使用资产中的 API Key
请求轻量模型列表。状态分为 unknown、checking、healthy、unhealthy、stale 和
disabled；探测结果不会改变工作流修订，也不会影响 `/health/ready`。

工作流策略由 SQLite 单例记录保存，默认值包括：

| 策略 | 默认值 |
| --- | ---: |
| 输入重分块 | 640 字节 |
| 输入 / 片段 / 结果队列 | 32 / 8 / 32 |
| 默认语言 | `zh-CN` |

最短/最长片段、环形缓冲和语音前滚属于 `physical_vad` 节点配置，不再是
全局工作流设置。容量设置由独立 SQLite 单例记录保存：gRPC、Realtime
WebSocket 和 WebRTC
默认各允许 64 个活动会话，所有协议共享 8 个 ASR、8 个 Responses 和 1 个
TTS 并发。Responses/TTS 容量耗尽时立即返回失败，不排队等待。两类设置的完整
PUT 都使用各自的乐观锁；旧 `revision` 会返回 409，客户端应重新读取后再提交。

全新数据库会创建 `default-transcription` 阶段草稿，并把同名公开模型和内部
`grpc-default` 路由到该工作流。语音 Agent 必须在控制面显式创建、绑定引擎并
发布；公开模型名在创建时由 slug 确定，不允许在发布后悄然漂移。

语音 Agent 的 Responses 阶段提供“启用思考”开关，默认关闭。关闭时会向
Responses API 传入 `reasoning.effort: "none"`；开启时传入 `"high"`。该设置随
工作流发布版本固化，修改草稿后需重新发布才会影响运行中的会话。

## OpenAI Realtime WSS

服务部署在可信内网，不要求客户端鉴权：

```text
wss://127.0.0.1:8000/v1/realtime?model=default-transcription
```

模型名先解析为一个已激活的公开工作流。转写工作流使用 transcription
session；语音 Agent 使用 realtime session。输入统一为 24 kHz 单声道 PCM
S16LE 和 `server_vad`，再连续重采样为识别链使用的 16 kHz。当前不支持手动
`input_audio_buffer.commit`。

支持的客户端事件：

- `session.update`；
- `input_audio_buffer.append`；
- `input_audio_buffer.clear`；
- Agent 工作流额外支持 `response.cancel`、`response.create`，以及通过
  `conversation.item.create` 回填 `function_call_output` 和 MCP 审批。

主要服务端事件：

- `session.created`、`session.updated`；
- `input_audio_buffer.speech_started`、`speech_stopped`、`committed`；
- `conversation.item.created`；
- `conversation.item.input_audio_transcription.delta`、`completed`、`failed`；
- Agent 的 `response.created`、`response.output_text.*`、
  `response.output_audio*`、工具调用事件和 `response.done`；
- `error`。

Agent 的最终转写会立即进入 Responses API SSE。每个
`response.output_text.delta` 都直接写入同一条 TTS `DuplexSynthesize` 请求流，
请求流只在本轮 Responses 完成后结束。WSS 用 base64
`response.output_audio.delta` 返回 24 kHz PCM；WebRTC 直接写远端音轨，不在
DataChannel 重复发送音频负载。用户重新开口时会提升会话 epoch、取消当前
Responses/TTS、清空尚未播放的 WebRTC 音频，并丢弃旧 epoch 的迟到结果。

Responses 引擎资产把 API base URL 与 API Key 以明文保存在 SQLite，控制台读取
资产时也会完整返回；应只在可信网络使用，并通过文件权限、磁盘加密和数据库备份
策略保护密钥。Agent 模型不再提供隐式默认值，发布前必须在 Responses 阶段明确
填写。请求强制 `stream=true`、`store=false`，完整会话历史在本地按完整轮次截断。函数工具分为三类：客户端临时函数由客户端
执行并回填结果；通过 `AudioHarnessHost.tool_registry` 注册的服务函数在本进程
执行；远程 MCP 由服务端使用 Streamable HTTP 连接，失败时兼容旧 SSE。客户端
提交的 MCP Authorization/headers 只保存在会话内存中，缺省
`require_approval` 为 `always`。

DeepSeek thinking 当前不接受 `required` 或指定函数的 `tool_choice` 时，运行时会
转换为 `auto` 加强约束提示，并在没有产生工具调用时最多重试一次。工具循环最多
8 轮，默认最多并行 4 个调用，每个调用超时 30 秒。

## OpenAI Realtime WebRTC

WebRTC 使用官方 calls 形态。应用后端以 Bearer Token 提交 multipart 表单，
其中 `sdp` 是完整 offer，`session` 是可选的 transcription 或 realtime
session JSON；语音 Agent 可通过 `?model=<公开模型名>` 选路由：

```text
POST /v1/realtime/calls
POST /v1/realtime/calls/{call_id}/hangup
```

浏览器音频通过 RTP 发送，服务端连续重采样为 16 kHz PCM 后进入同一套
VAD + ASR 工作流；协议事件通过名为 `oai-events` 的 DataChannel 返回。当前仅
接收一条音频输入轨道，并协商一条服务端音频输出轨道。Agent 的 TTS PCM 会被
重采样为 48 kHz、按 20 ms 帧写入该轨道；没有语音时轨道持续发送静音。

同机与同一局域网通常可以不配置 ICE server。跨 NAT 部署时通过
`WEBRTC_ICE_SERVERS_JSON` 配置外部 STUN/TURN，例如：

```json
[{"urls":"stun:stun.example.com:3478"},{"urls":"turn:turn.example.com:3478","username":"user","credential":"secret"}]
```

WebRTC 会话额度与 gRPC、Realtime WebSocket 分离，但仍共享全局 ASR、
Responses 与 TTS 并发。

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

完整语音 Agent 对话 Demo 位于 `examples/openai_realtime_voice_demo`。浏览器
只向 Demo 后端发送二进制 PCM 并播放返回的 PCM；API Key、模型路由、
`session.update`、音频 append 和 Realtime 服务端事件解析全部由 Node 后端负责：

```bash
cd examples/openai_realtime_voice_demo
cp .env.example .env
pnpm install
pnpm dev
```

详细配置见
[服务端语音对话 Demo 文档](examples/openai_realtime_voice_demo/README.md)。

## gRPC

对外接口为 `ux_speech.UxSpeech/StreamingRecognize`。首包只能携带
`StreamingRecognitionConfig`，后续包只能携带 `audio_content`。第一版支持
单声道 PCM S16LE，未指定采样率时默认 16 kHz。

请求音频固定进入 `grpc-default` 指向的已激活转写工作流，客户端不能通过 gRPC
请求字段切换模型。默认图由物理 VAD 形成完整片段，再交给可选的
语义 VAD 和 ASR；环形缓冲与语音前滚参数由 `physical_vad` 节点配置。示例：

```bash
poetry run python examples/grpc_client.py \
  data-bin/audio.wav \
  --target 127.0.0.1:50051
```

## Responses 与双向流式 TTS

这两项是 Realtime 语音 Agent 的内部节点，不提供 `/v1/responses`、
`/v1/audio/speech` 或独立模型列表接口。可以在 Web 引擎库注册，也可以通过
控制面 API 创建。DeepSeek 示例：

```bash
curl -k https://127.0.0.1:8000/api/v1/engines \
  -H 'content-type: application/json' \
  -d '{"slug":"deepseek-responses","name":"DeepSeek Responses","kind":"llm","api":"responses","protocol":"https","target":"https://api.deepseek.com","api_key":"<YOUR_API_KEY>"}'
```

当前可用 TTS 地址和预置音色：

```bash
curl -k https://127.0.0.1:8000/api/v1/engines \
  -H 'content-type: application/json' \
  -d '{"slug":"qwen-tts","name":"Qwen 双流 TTS","kind":"tts","api":"qwen_tts_duplex","protocol":"grpc","target":"192.168.0.222:50005"}'
```

`duplex_tts` 阶段不再假设默认音色，发布前必须提供至少一个允许音色并从中选择
默认音色；`decoder_chunk_size` 默认是 `0`。如需克隆音色，通过 `/api/v1/voices` 上传参考 WAV，
再把返回的音色 ID 加入节点 `allowed_voices`；音频文件默认保存到
`data-bin/voices`，数据库只保存受管路径和元数据。双向 TTS 的一轮合成是长连接流，
不会套用引擎的“请求超时”作为整条流的 deadline；用户打断会立即取消该流。

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
pnpm --dir examples/openai_realtime_voice_demo lint
pnpm --dir examples/openai_realtime_voice_demo test
pnpm --dir examples/openai_realtime_voice_demo build
pnpm --dir examples/openai_webrtc_demo lint
pnpm --dir examples/openai_webrtc_demo build
```

自动化测试使用临时 SQLite 与进程内假引擎，不会连接知识库中的真实服务。
