# Audio Harness

Audio Harness 是一个面向实时语音推理的异步编排服务。它把推理引擎、语音工作流、语音智能体配置和协议接入拆成独立边界，通过 OpenAI Realtime WebSocket 与 WebRTC 提供数据面。

当前版本采用全新的数据库基线，不兼容旧版 Pipeline、Stage Workflow 或历史业务数据。

## 核心设计

系统围绕五个明确对象组织：

1. `EngineAsset` 描述一个可连接的推理引擎；发布时会生成不可变 `EngineSnapshot`。
2. `AudioWorkflow` 只描述 PCM、VAD、语音片段和 ASR 转写之间的类型化 DAG。
3. `VoiceAgentProfile` 在已发布的语音工作流之上绑定 Responses、TTS、工具和对话策略。
4. `AgentBackendAsset` 描述一个远端智能体运行服务；首个适配器是 DSH Web v1。
5. `RealtimeRoute` 把外部 `model` 名称指向一个已发布的语音工作流或语音智能体。

```text
HTTP 控制面
    │
    ├── EngineCatalog ───────────────┐
    ├── AudioWorkflowService         │
    ├── VoiceAgentService            ├── InferencerPool
    ├── AgentBackendCatalog          │       │
    └── RealtimeRouteService         │       │
                                      │       └── 不可变引擎快照租约
Realtime / WebRTC 数据面 ───────────┘
```

协议层不读取数据库、不解析业务图，也不负责创建推理客户端。进程组合根在启动时装配服务；会话只持有活动工作流快照、容量租约和有界队列。

## 类型化语音 DAG

内置节点类型包括：

| 节点 | 输入 | 输出 | 引擎 API |
| --- | --- | --- | --- |
| 自动增益控制 | `pcm_chunk` | `pcm_chunk` | 无（本地实时处理） |
| Streaming Denoise | `pcm_chunk` | `vad_audio_chunk`（仅物理 VAD） | `ux_denoise` |
| Physical VAD | `audio: pcm_chunk` 或 `detection_audio: vad_audio_chunk`（互斥） | `audio_segment`、`speech_started`、`speech_stopped` | `ux_vad` |
| Semantic VAD | `audio_segment` | `audio_segment`、`speech_stopped`、`transcription` | `ux_vad` |
| ASR | `audio_segment` | `transcription` | `ux_speech` |
| Speaker Diarization | `audio_segment` | `audio_segment`、`diarization_local_result` | `ux_speaker_diarization` |
| Audio Embedding | `audio_segment` | `embedding` | `engine_feature` |
| Vector Search | `embedding` | `vector_search_result` | 无 |
| Speaker Filter | `audio_segment`、`vector_search_result` | `audio_segment`、`speaker_filter_result`、VAD 事件 | 无 |

节点类型不是动态插件。编译器会统一检查：

- 节点和端口是否存在；
- 源端口与目标端口类型是否一致；
- 单输入端口是否发生隐式 fan-in；
- 是否存在环、不可达节点或断开的输出路径；
- 引擎是否存在、启用且 API 能力匹配；
- `primary_transcript`、`speech_started`、`speech_stopped` 三个稳定出口是否完整且类型正确；
- 可选的 `interim_transcript`、`vector_search` 和 `diarization_local` 是否连接到匹配端口。

在线降噪节点调用 `SynchronizedStreamingDenoise`，服务会逐采样点返回等长的原音和
降噪音频。节点默认把纯降噪音频交给物理 VAD；可选的 `denoised_weight` 使用
`(1-w) × 原音 + w × 降噪音频` 混合，未设置时关闭混合。物理 VAD 只用处理后的音频
判断边界，切片缓存和所有 ASR、分离、向量化下游始终使用原音。降噪专用端口不能
绑定命名出口或连接其他节点；失败策略默认 `fail_open`，从未确认采样点旁路原音。

自动增益控制节点是无前瞻的本地透明 PCM 节点。默认采用固定七段 compand 曲线、
`attack=0`、`decay=2.5` 秒、`soft-knee=6`、初始检测电平 `-12 dBFS` 和末级
`+4.5 dB` 补偿。节点状态跨音频块保存且不改变采样位置；处理后的音频会供 VAD、
ASR、说话人分离等全部下游使用。节点通过 PyAV 在进程内持有连续的 FFmpeg
`libavfilter` filter graph，直接运行 `compand,volume`；不会为每个音频块启动子进程。
`delay=0` 时输入块立即产生等长 PCM，并保持 FFmpeg 的线性振幅包络和软拐点行为。

物理 VAD 显式区分两个互斥输入：`audio` 是全链路主音频，检测、缓存和后续切片均
使用它；`detection_audio` 接收同步双路音频，仅把处理音用于 VAD 检测，后续切片仍
使用其中的主音频。降噪节点应连接 `detection_audio`，AGC 等透明 PCM 节点应连接
`audio`。旧版“降噪输出连接 VAD.audio”的工作流在读取时自动归一化到新端口。

物理 VAD 节点可以按工作流配置 FireRed `firered.stream_vad` v1 后处理参数，
包括概率平滑、判定阈值、起点前补以及语音/静音帧数限制。未启用自定义配置时，
流式首包不会发送 `postprocessing` 字段，继续使用 VAD 服务端默认值；参数修改随
工作流重新发布后生效，不会热更新已经运行的会话。旧 VAD 服务可以忽略新增字段。
主物理 VAD 还可以用 `progress_interval_seconds` 控制 AHA v1 处理进度，默认按墙钟
最多 5 Hz，设为 `0` 时关闭；正常 commit 排空会发送覆盖完整输入（含尾部静音）的
最终进度，并在全部聚类完成后发送最终全量聚类快照。

工作流编辑器另提供“物理双路 VAD + ASR”和“物理双路 + 语义 VAD + ASR”模板。
两者都使用独立的敏感物理 VAD 与 ASR 生成 `interim_transcript` Realtime 预览结果；
后一个模板的正式分支为“物理 VAD → 语义 VAD → ASR”，由语义 VAD 输出正式的
`speech_stopped` 和 `turn_progress`。`primary_transcript` 始终是正式结果，预览分支的
VAD 带有一组偏敏感的初始参数，但所有参数和引擎绑定都可继续编辑；现有默认工作流
和已发布修订不会被自动迁移。

“会议系统完全体”模板建立在双路物理与语义 VAD 模板之上，并把正式语义 VAD 输出的
音频片段并联到“语音特征向量提取 → 人物搜索”，增加 `vector_search` 命名出口；
正式语义 VAD 同时直连主 ASR，并经“说话人分离 → 说话人 ASR”产生分角色转写，
增加 `diarization_local` 与 `speaker_transcript` 命名出口。主转写不等待说话人分离，
原低延迟 interim ASR 分支也继续并行工作。
模板优先选择带语音特征向量提取模板标记的 Qdrant Collection，并据其首个 Named
Vector 初始化算法和维度；没有可用空间时使用 `voiceprints`、`eres2netv2`、192 维
作为待确认配置。再次应用模板会保留已经选择的提取引擎、Collection 和检索参数。

说话人分离节点按有效语音时长聚合 VAD 块，默认累计 60 秒后处理，单次最多 120 秒。
`min_batch_seconds=0` 时每个 VAD 块立即处理；旧缓存加新块超过最大长度时先提交旧缓存，
新块不会丢弃；单个超长块按最大长度硬切。会话结束时不足最小时长的尾块也会提交。
不同 VAD 块之间不补静音，服务返回的相对时间会根据拼接映射还原为会话绝对时间。
节点默认启用 `audio_harness.diarization.vad_coverage` v1 后处理：填充有效语音头尾和
片段间空洞，并在同一语义轮次内跨 VAD 静音块合并相邻同说话人片段。VAD 块边界
本身不会触发提交；首个有效的异说话人片段会使上一片段稳定并立即送入说话人 ASR，
语义轮次边界和 RPC 结束也会刷新待定片段。引擎时间戳允许小范围重叠；不同说话人
重叠时以后一个片段的起点切分。把节点 `postprocessing` 显式设为 `null` 可恢复原始
时间戳增量和 RPC 结束后统一送入 ASR 的兼容行为。

说话人聚类节点默认启用
`audio_harness.speaker_clustering.complete_link_merge` v1 后处理，用于减少同一个人被
拆成多个全局 speaker ID。每次上游成功返回全量聚类快照后，节点按原始 speaker ID
汇总并按时间拼接片段，每人最多取前 20 秒 16 kHz 单声道 PCM，重新提取归一化声纹，
并维护会话级余弦相似度矩阵缓存。相似度达到高阈值（默认 `0.70`）时尝试合并，同时
要求合并组内任意两人都不低于低阈值（默认 `0.50`），避免链式相似导致误合并。
后处理只重映射输出快照，不修改流式 HDBSCAN 的内部标签；设为 `null` 可关闭。默认
`fail_open` 下后处理失败会保留当前原始聚类快照，`fail_pipeline` 则沿用节点失败策略。

ASR 节点可以独立开启“移除转写标点”，在节点输出进入工作流出口前删除中英文及
其他语言的 Unicode 标点，同时保留原有空白、符号和 emoji。该选项默认关闭；两个
双路模板新建的预览 ASR 默认开启，正式 ASR 默认关闭。

工作流的语义文档与画布 `layout` 分开保存。发布时重新编译草稿、准备全部引擎，然后原子写入不可变 revision；活动会话不会被后续草稿或引擎资产修改热改。

## 目录结构

```text
src/audio_harness/
├── api/                # FastAPI 应用、URL、路由与异常映射
├── errors.py           # 跨能力共享的应用异常
├── schemas/            # 跨协议的请求与响应数据契约
├── configs/            # 按用途拆分的进程配置
├── realtime_routes/    # Realtime 模型路由控制面
├── workflows/          # AudioWorkflow 文档、编译器、运行时、仓储与发布服务
├── voice_agents/       # 语音智能体配置、仓储与发布服务
├── agent_backends/     # 通用智能体后端协议与 DSH Web 适配器
├── engines/            # 引擎资产目录与 InferencerPool
├── inferencers/        # 按能力分组的推理客户端适配器
├── media/              # PCM 等媒体数据类型
├── sessions/           # 协议无关的内部执行会话
│   ├── audio_workflow/ # 工作流会话、容量预留与 Inferencer 租约
│   ├── physical_vad/   # 物理 VAD 流式边界会话
│   ├── semantic_vad/   # 语义 VAD 轮次聚合会话
│   ├── diarization/    # 说话人分离批处理与结果映射会话
│   └── speaker_clustering/ # 跨批次说话人聚类会话
├── serving/            # 进程容量、状态与协议服务
│   ├── realtime/       # OpenAI Realtime WebSocket 适配器
│   └── webrtc/         # WebRTC 适配器
└── database/           # SQLAlchemy 模型与单一 Alembic 基线

src/web/
├── openapi.json        # 后端导出的接口契约
└── src/
    ├── api/generated.ts # openapi-typescript 生成类型
    ├── components/AudioGraphEditor.tsx
    └── components/DeploymentPanel.tsx # 工作流与语音智能体共用的显式接入面板
```

## 数据库策略

数据库只支持 `sqlite+aiosqlite`。当前 Alembic head 为 `0006_agent_backends`。

启动命令只接受：

- 不存在或空的数据库；
- 已处于迁移链中可升级的 revision 或当前 head 的数据库。

发现旧 revision 时会直接拒绝启动。请先停止服务，人工移走 SQLite 主文件及对应的 `-wal`、`-shm` 文件，再启动服务创建干净数据库。系统不会隐式删除或转换旧业务数据。

## 安装与启动

```bash
poetry install
cd src/web
pnpm install
pnpm build
cd ../..
poetry run audio-harness serve
```

常用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data-bin/sqlite/audio-harness.db` | SQLite 数据库 |
| `HTTP_HOST` / `HTTP_PORT` | `0.0.0.0` / `8000` | HTTP、WSS 与 WebRTC 信令地址 |
| `VOICE_STORAGE_DIR` | `./data-bin/voices` | 参考音频目录 |
| `RECEIVED_AUDIO_CACHE_DIR` | `./data-bin/cache` | 接收音频缓存根目录 |
| `QDRANT_GRPC_TARGET` | 未配置 | Qdrant 正式数据面，例如 `192.168.0.222:16334` |
| `QDRANT_HTTP_URL` | 未配置 | Qdrant Dashboard 与健康检查，例如 `http://192.168.0.222:16333` |

## DSH Agent Backend

Responses 仍是 Voice Agent 的必选组件，直接处理对话历史、现有工具/MCP 和 TTS；DSH
不会注册成 Responses tool。Voice Agent v2 可以选择一个 DSH Agent Backend，Responses
以严格结构化决策决定直接回复、委派、steer、取消、回答审批或回答结构化问题。DSH
完成后，结果会进入一次独立、禁用工具的 Responses 总结调用，再流式送入 TTS。

本仓库本次不交付 DSH 镜像或 Docker Compose。生产环境应让 DSH 作为独立容器运行，
并通过私有网络或带 TLS、鉴权和访问控制的代理暴露同一个 HTTP/WS endpoint。
Audio Harness 的 `target` 指向该 endpoint；如果代理要求请求头，可用 `headers_env`
把请求头名称映射到 Audio Harness 容器中的环境变量。SQLite 只保存环境变量名，不保存
真实远端凭据。DSH 调用模型所需的 API Key 必须由 DSH 容器自行持有，不能复用或依赖
Audio Harness 数据库中的 Responses 密钥。

开发时可以在同一容器临时启动 DSH，先验证 HTTP JSON-RPC 与两个 WebSocket 事件流，
但这不是生产拓扑。每条 Realtime/WebRTC 连接按需创建并复用一个 DSH session；连接
断开会取消运行任务，但 DSH session 和服务端日志保留。`response.cancel` 只取消当前
Responses/TTS 输出，取消 DSH 必须显式发送 `x_aha.agent_backend.cancel`。

## 音频向量检索

Qdrant 是 Collection、Named Vector、Point 和 payload 的唯一事实来源；Audio Harness
不在 SQLite 中复制向量空间注册表。系统支持通用 Collection、voiceprint 模板、音频
登记、VAD 预处理、按人物分组搜索，以及工作流内的 Embedding → Vector Search 分支。
人物搜索后还可以连接“说话人过滤”节点：它同时接收原始语音片段和检索结果，
只有白名单人物达到门控分数时才将音频送入 ASR；未匹配、低分和检索失败均静默拒绝。
完整字段、curl 示例和 `x_aha.vector.*` 事件分别见[向量检索接口](docs/control-plane/vectors.md)
与 [AHA v1 协议](docs/realtime-api/aha-v1.md)。

控制台按语音工作流、语音智能体、Agent Backends、推理引擎和接入运行等任务域组织。首次启动后的推荐顺序：

1. 在“推理引擎”中注册 VAD 与 ASR 引擎，并完成连通性校验。
2. 打开默认语音工作流，在 DAG 画布绑定节点引擎、连线和命名出口。
3. 校验并发布语音工作流，再在“接入”页显式创建 Realtime model。
4. 如需 DSH 委派，先在“Agent Backends”注册远端服务并完成能力探测。
5. 创建语音智能体空白草稿，在固定拓扑画布中配置语音工作流、Responses、TTS 和可选 Agent Backend。该画布用于表达与配置当前运行关系，暂不提供节点增删或连线编排。
6. 发布语音智能体后显式创建公开 Realtime Route，再进入“测试”页发起连接。

发布和接入是两个独立动作。发布只生成不可变活动版本，不会自动创建外部入口；Realtime model 冲突时接口返回 `409 realtime_route_conflict`，只有用户确认后才允许替换原目标。测试页会先读取路由并检查其可见性，避免把未部署资源误报为 WebSocket 故障。

`GET /api/v1/runtime/overview` 始终返回 `200`，用于控制台展示“待配置”和可操作诊断；`/health/ready` 是部署探针，只检查数据库与控制面是否可用。缺少公开 Realtime 路由不会阻止部署就绪。

## 接口文档

完整对接规范已经拆分到 [`docs/`](docs/README.md)，以避免 README 与接口契约形成两套事实来源：

- [通用 API 约定](docs/api-conventions.md)
- [控制面：引擎、向量、工作流、Agent 与运行设置](docs/README.md#调用-audio-harness)
- [OpenAI Realtime 兼容子集](docs/realtime-api/openai-realtime.md)
- [AHA Realtime 扩展协议 v1](docs/realtime-api/aha-v1.md)
- [WebRTC 接入](docs/realtime-api/webrtc.md)
- [下游推理引擎协议](docs/engine-integration/README.md)
- [机器可读 OpenAPI](docs/reference/openapi.json)

## 接收音频缓存

“接入与运行”的高级设置可以为新建的 Realtime WSS 和 WebRTC 会话开启
接收音频缓存。缓存记录服务端重采样后实际送入工作流的 16 kHz、单声道、
PCM S16LE 音频；默认每 5 秒提交一个后台写入块，每 300 秒发布一个 WAV 文件，
会话关闭时也会发布不足一个完整周期的尾文件。

缓存根目录由 `RECEIVED_AUDIO_CACHE_DIR` 环境变量配置，默认是
`./data-bin/cache`。文件写入该目录下的
`<UTC 日期>/<首段音频接收时间>__<短会话 ID>/`，例如
`2026-08-20/20260820T081308.665Z__sess-a9044150/`。时间表示服务端首次
接收并处理该缓存 PCM 的时刻，而非落盘时刻；目录内的每个 WAV 文件也使用其
首段音频的接收时间和序号命名。`metadata.json` 保存完整会话 ID、缓存开始时间、
音频格式与当时生效的分片设置，以便与日志反查。若同一毫秒的目录名冲突，会追加
`__02`、`__03` 等递增序号。当前文件使用 `.wav.part` 后缀，正常收口后原子改名为
`.wav`；异常退出遗留的临时文件原样保留。缓存队列拥塞或磁盘写入失败只会停用当前
会话的后续缓存，不会中断实时处理。设置更新只影响新会话，已建立的会话继续使用
创建时的配置快照。

主程序不会删除任何历史缓存，也不提供缓存浏览、下载或保留期清理 API。
既有缓存目录不会被迁移或改写。
生产环境应把该目录放在独立缓存卷中，并由具有独立、受限权限的运维设施负责
容量监控和生命周期管理。

## OpenAPI 与前端类型

前端不手写后端 DTO，`docs/reference/openapi.json` 也由同一脚本生成。修改接口后执行：

```bash
poetry run python scripts/export_openapi.py
cd src/web
pnpm generate:api
```

默认导出会同时更新前端与文档契约；`--output <path>` 可只导出单个文件。
`pnpm build` 会自动重新生成 `src/api/generated.ts`，从而在后端字段变化时让 TypeScript 构建直接失败。

## 验证

```bash
poetry run pytest -q tests

cd src/web
pnpm lint
pnpm test
pnpm build
```

Python 测试覆盖编译诊断、typed DAG 流式执行、取消传播、发布快照、乐观锁、语音智能体组合、数据库迁移、下游 gRPC 推理适配器以及 Realtime、WebRTC 协议行为。前端测试覆盖 API 客户端、主要页面和端口型 DAG 编辑器入口。

需要重新校准 OpenAI Realtime 实网行为时，可运行：

```bash
poetry run python scripts/capture_openai_realtime.py --mode all
```

脚本只静态读取进程环境或项目 `.env` 中的 `OPENAI_API_KEY`，不会执行配置文件；
测试音频由 ffmpeg/flite 现场合成。原始 JSONL 仅写入 `/tmp` 或 Git 已忽略目录，
归一化 fixture 会去除标识、文本和音频内容，并用当前 OpenAI SDK 校验每个服务端事件。
