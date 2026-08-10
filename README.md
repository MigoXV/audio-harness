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
- `primary_transcript`、`speech_started`、`speech_stopped` 三个稳定出口是否完整且类型正确。

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
  `session.update.audio.input.transcription.model` 中指定
- WebRTC `POST /v1/realtime/calls`

### x_aha Realtime 扩展

默认情况下，WebSocket 与 WebRTC 只发送 OpenAI Realtime 原生事件。专用客户端可在
建连查询参数中传入 `x_aha=v1`，启用版本化的 `x_aha.*` 扩展事件。WebSocket 不再接受
`model` 查询参数，只接受 `intent=transcription`；不支持的 intent 或扩展版本会在建连
阶段被拒绝。

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

该 `item_id` 与本轮标准 `input_audio_buffer.speech_started` 及最终的
`input_audio_buffer.speech_stopped` 相同。Python SDK 客户端可使用
`extra_query={"x_aha": "v1"}` 协商扩展；完整示例见
`examples/openai_realtime_python_demo`。

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
