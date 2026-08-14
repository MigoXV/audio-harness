# Audio Harness

Audio Harness 是一个面向实时语音推理的异步编排服务。它把推理引擎、语音工作流、语音智能体配置和协议接入拆成独立边界，同时保留 gRPC、OpenAI Realtime WebSocket 与 WebRTC 数据面。

当前版本采用全新的数据库基线，不兼容旧版 Pipeline、Stage Workflow 或历史业务数据。

## 核心设计

系统围绕四个明确对象组织：

1. `EngineAsset` 描述一个可连接的推理引擎；发布时会生成不可变 `EngineSnapshot`。
2. `AudioWorkflow` 只描述 PCM、VAD、语音片段和 ASR 转写之间的类型化 DAG。
3. `VoiceAgentProfile` 在已发布的语音工作流之上绑定 Responses、TTS、工具和对话策略。
4. `RealtimeRoute` 把外部 `model` 名称指向一个已发布的语音工作流或语音智能体。

```text
HTTP 控制面
    │
    ├── EngineCatalog ───────────────┐
    ├── AudioWorkflowService         │
    ├── VoiceAgentService            ├── InferencerPool
    └── RealtimeRouteService         │       │
                                      │       └── 不可变引擎快照租约
gRPC / Realtime / WebRTC 数据面 ─────┘
```

协议层不读取数据库、不解析业务图，也不负责创建推理客户端。进程组合根在启动时装配服务；会话只持有活动工作流快照、容量租约和有界队列。

## 类型化语音 DAG

第一版内置三个静态节点类型：

| 节点 | 输入 | 输出 | 引擎 API |
| --- | --- | --- | --- |
| Physical VAD | `pcm_chunk` | `audio_segment`、`speech_started`、`speech_stopped` | `ux_vad` |
| Semantic VAD | `audio_segment` | `audio_segment`、`speech_stopped`、`transcription` | `ux_vad` |
| ASR | `audio_segment` | `transcription` | `ux_speech` |

节点类型不是动态插件。编译器会统一检查：

- 节点和端口是否存在；
- 源端口与目标端口类型是否一致；
- 单输入端口是否发生隐式 fan-in；
- 是否存在环、不可达节点或断开的输出路径；
- 引擎是否存在、启用且 API 能力匹配；
- `primary_transcript`、`speech_started`、`speech_stopped` 三个稳定出口是否完整且类型正确；
- 可选的 `interim_transcript` 是否连接到 `transcription` 端口。

物理 VAD 节点可以按工作流配置 FireRed `firered.stream_vad` v1 后处理参数，
包括概率平滑、判定阈值、起点前补以及语音/静音帧数限制。未启用自定义配置时，
流式首包不会发送 `postprocessing` 字段，继续使用 VAD 服务端默认值；参数修改随
工作流重新发布后生效，不会热更新已经运行的会话。旧 VAD 服务可以忽略新增字段。

工作流编辑器另提供“物理双路 VAD + ASR”和“物理双路 + 语义 VAD + ASR”模板。
两者都使用独立的敏感物理 VAD 与 ASR 生成 `interim_transcript` Realtime 预览结果；
后一个模板的正式分支为“物理 VAD → 语义 VAD → ASR”，由语义 VAD 输出正式的
`speech_stopped` 和 `turn_progress`。`primary_transcript` 始终是正式结果，预览分支的
VAD 带有一组偏敏感的初始参数，但所有参数和引擎绑定都可继续编辑；现有默认工作流
和已发布修订不会被自动迁移。

ASR 节点可以独立开启“移除转写标点”，在节点输出进入工作流出口前删除中英文及
其他语言的 Unicode 标点，同时保留原有空白、符号和 emoji。该选项默认关闭；两个
双路模板新建的预览 ASR 默认开启，正式 ASR 默认关闭。

工作流的语义文档与画布 `layout` 分开保存。发布时重新编译草稿、准备全部引擎，然后原子写入不可变 revision；活动会话不会被后续草稿或引擎资产修改热改。

## 目录结构

```text
src/audio_harness/
├── workflows/          # AudioWorkflow 文档、编译器、运行时、仓储与发布服务
├── voice_agents/       # 语音智能体配置与 Realtime Route
├── engines/            # InferencerPool
├── inferencers/        # 各协议推理客户端适配器
├── sessions/           # 协议无关会话生命周期与容量租约
├── serving/
│   ├── grpc/           # gRPC 数据面适配器
│   ├── realtime/       # OpenAI Realtime WebSocket 适配器
│   ├── webrtc/         # WebRTC 适配器
│   └── http/routers/   # 按资源拆分的 HTTP 控制面
└── database/           # SQLAlchemy 模型与单一 Alembic 基线

src/web/
├── openapi.json        # 后端导出的接口契约
└── src/
    ├── api/generated.ts # openapi-typescript 生成类型
    ├── components/AudioGraphEditor.tsx
    └── components/DeploymentPanel.tsx # 工作流与语音智能体共用的显式接入面板
```

## 数据库策略

数据库只支持 `sqlite+aiosqlite`。当前 Alembic head 为 `0001_initial`。

启动命令只接受：

- 不存在或空的数据库；
- 已处于 `0001_initial` 的当前数据库。

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
| `GRPC_HOST` / `GRPC_PORT` | `0.0.0.0` / `50051` | gRPC 监听地址 |
| `HTTP_HOST` / `HTTP_PORT` | `0.0.0.0` / `8000` | HTTP、WSS 与 WebRTC 信令地址 |
| `VOICE_STORAGE_DIR` | `./data-bin/voices` | 参考音频目录 |

控制台按“语音工作流、语音智能体、推理引擎、接入与运行”四个任务域组织。首次启动后的推荐顺序：

1. 在“推理引擎”中注册 VAD 与 ASR 引擎，并完成连通性校验。
2. 打开默认语音工作流，在 DAG 画布绑定节点引擎、连线和命名出口。
3. 校验并发布语音工作流，再在“接入”页显式绑定 gRPC 默认工作流或 Realtime model。
4. 如需对话能力，通过四步向导创建语音智能体，依次选择语音工作流、Responses 引擎和 TTS 引擎。
5. 发布语音智能体后显式创建公开 Realtime Route，再进入“测试”页发起连接。

发布和接入是两个独立动作。发布只生成不可变活动版本，不会自动创建外部入口；Realtime model 冲突时接口返回 `409 realtime_route_conflict`，只有用户确认后才允许替换原目标。测试页会先读取路由并检查其可见性，避免把未部署资源误报为 WebSocket 故障。

`GET /api/v1/runtime/overview` 始终返回 `200`，用于控制台展示“待配置”和可操作诊断；`/health/ready` 仍是部署探针，在 gRPC 默认工作流未就绪时返回 `503`。因此浏览器界面不再轮询 503 探针来判断管理服务是否在线。

## 控制面接口

主要资源路径如下：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET/POST` | `/api/v1/engines` | 引擎列表与创建 |
| `POST` | `/api/v1/engines/{id}/validate` | 单引擎真实探测 |
| `GET` | `/api/v1/audio-node-types` | 节点、端口与配置 Schema |
| `GET/POST` | `/api/v1/audio-workflows` | 语音工作流列表与创建 |
| `GET/PUT` | `/api/v1/audio-workflows/{id}/draft` | 乐观锁读取和保存草稿 |
| `POST` | `/api/v1/audio-workflows/{id}/validate` | 编译校验草稿 |
| `POST` | `/api/v1/audio-workflows/{id}/publish` | 原子发布不可变 revision |
| `GET` | `/api/v1/audio-workflows/{id}/revisions` | 发布历史 |
| `GET/POST` | `/api/v1/voice-agents` | 语音智能体配置 |
| `POST` | `/api/v1/voice-agents/{id}/validate` | 校验语音智能体草稿及其依赖 |
| `GET` | `/api/v1/realtime-routes/{model}` | 读取并预检单个 Realtime 路由 |
| `PUT` | `/api/v1/realtime-routes/{model}` | 显式创建或确认替换 Realtime 路由 |
| `GET` | `/api/v1/runtime/overview` | 控制面与协议接入状态总览 |
| `GET/PUT` | `/api/v1/settings/workflow` | 流式队列设置 |
| `GET/PUT` | `/api/v1/settings/capacity` | 会话与推理并发设置 |

数据面路径保持为：

- gRPC `UxSpeech.StreamingRecognize`
- 纯转写 WebSocket `/v1/realtime?intent=transcription`；转写模型在后续
  `session.update.audio.input.transcription.model` 中指定，可显式协商 `x_aha=v1`
- 语音智能体 WebSocket `/v1/realtime?model=<public-active-route>`；`model`
  必须是指向已激活语音智能体的公开 Realtime Route，也可显式协商
  `x_aha=v1`
- WebRTC `POST /v1/realtime/calls?model=...`

WebSocket 建连时必须在 `intent=transcription` 和 `model=...` 中二选一；
缺失、并用或传入未发布的路由都会在建连阶段被拒绝。

### x_aha Realtime 扩展

默认情况下，WebSocket 与 WebRTC 只发送 OpenAI Realtime 原生事件。
纯转写和语音智能体会话都可以在建连时显式传入 `x_aha=v1`，启用版本化的
`x_aha.*` 扩展事件。不支持的扩展版本会被拒绝。扩展只追加观测信息，
不会替换、删减或改写 OpenAI Realtime 原生事件；未协商扩展时，服务端不会
发送 `x_aha.*`、`audio_harness.*` 或其他私有事件。

当物理 VAD 已结束、语义 VAD 仍认为用户轮次未完成时，服务端发送：

```json
{
  "type": "x_aha.vad.physical_speech_stopped",
  "event_id": "event_xxx",
  "item_id": "item_xxx",
  "audio_start_ms": 1200,
  "audio_end_ms": 2380,
  "turn_completed": false
}
```

当工作流声明了 `interim_transcript` 出口时，协商扩展的自动 VAD 会话还会按轮次发送
累计中间转写快照：

```json
{
  "type": "x_aha.input_audio_transcription.interim",
  "event_id": "event_xxx",
  "item_id": "item_xxx",
  "content_index": 0,
  "revision": 2,
  "transcript": "这是当前累计的临时文本",
  "audio_start_ms": 120,
  "audio_end_ms": 2120
}
```

客户端应按相同 `item_id` 下单调递增的 `revision` 覆盖旧文本。正式转写开始前服务端
会先发送清除事件，随后继续发送标准 OpenAI Realtime delta/completed：

```json
{
  "type": "x_aha.input_audio_transcription.interim_cleared",
  "event_id": "event_xxx",
  "item_id": "item_xxx",
  "reason": "primary_transcript_started"
}
```

中间结果只用于界面预览，不提交 conversation item、不触发语音智能体回复，也不替代
最终标准事件。未协商 `x_aha=v1` 或使用手动 commit 时不会执行预览分支；ASR 容量
繁忙时可能跳过预览块。预览分支失败不会中断正式转写，gRPC 服务仍只执行正式出口。

上述扩展事件的 `item_id` 与本轮标准 `input_audio_buffer.speech_started` 及最终的
`input_audio_buffer.speech_stopped` 相同。Python SDK 客户端可使用
`extra_query={"intent": "transcription", "x_aha": "v1"}` 协商转写扩展，
或使用 `extra_query={"model": "<route>", "x_aha": "v1"}` 协商语音智能体
扩展。启用后应按原始 JSON 分派 `x_aha.*`，再使用 SDK 类型处理原生事件。
完整转写示例见 `examples/openai_realtime_python_demo`；语音智能体 Demo 刻意不
协商扩展，用于验证默认原生协议兼容性。

## OpenAPI 与前端类型

前端不手写后端 DTO。修改接口后执行：

```bash
poetry run audio-harness export-openapi --output src/web/openapi.json
cd src/web
pnpm generate:api
```

`pnpm build` 会自动重新生成 `src/api/generated.ts`，从而在后端字段变化时让 TypeScript 构建直接失败。

## 验证

```bash
poetry run pytest -q

cd src/web
pnpm lint
pnpm test
pnpm build
```

Python 测试覆盖编译诊断、typed DAG 流式执行、取消传播、发布快照、乐观锁、语音智能体组合、数据库基线以及 gRPC、Realtime、WebRTC 协议行为。前端测试覆盖 API 客户端、主要页面和端口型 DAG 编辑器入口。

需要重新校准 OpenAI Realtime 实网行为时，可运行：

```bash
poetry run python scripts/capture_openai_realtime.py --mode all
```

脚本只静态读取进程环境或项目 `.env` 中的 `OPENAI_API_KEY`，不会执行配置文件；
测试音频由 ffmpeg/flite 现场合成。原始 JSONL 仅写入 `/tmp` 或 Git 已忽略目录，
归一化 fixture 会去除标识、文本和音频内容，并用当前 OpenAI SDK 校验每个服务端事件。
