# Audio Harness 接口文档

本目录是 Audio Harness 的对接规范入口。文档描述当前仓库已经实现并由测试覆盖的行为；代码、Pydantic Schema、OpenAPI 和 Proto 是最终事实来源。示例默认服务地址为 `http://localhost:8000`、WebSocket 地址为 `ws://localhost:8000`。

## 系统边界

```text
业务客户端
  ├─ HTTP /api/v1/* ───────────── 控制面 CRUD、发布、路由和运行状态
  ├─ WebSocket /v1/realtime ───── 实时转写或语音智能体
  ├─ WebRTC /v1/realtime/calls ─ 浏览器/移动端实时音频
                         │
                  Audio Harness
                         │
  ├─ ux_speech / ux_vad / ux_sd / Engine Feature & Clustering
  ├─ qwen_tts.service.v1.TtsService
  ├─ OpenAI-compatible Models + Responses
  └─ Qdrant gRPC
```

控制面负责可变配置；发布动作生成不可变 revision。数据面在会话建立时取得活动快照，后续草稿或引擎资产修改不会热更新正在运行的会话。

## 调用 Audio Harness

1. 先阅读[通用 API 约定](api-conventions.md)。
2. 注册并验证[推理引擎](control-plane/engines.md)。
3. 创建、校验并发布[语音工作流](control-plane/workflows.md)。
4. 如需对话，创建并发布[语音智能体](control-plane/voice-agents.md)。
5. 配置 [Realtime Route 与运行参数](control-plane/routes-runtime.md)。
6. 选择 [Realtime WebSocket](realtime-api/README.md)或 [WebRTC](realtime-api/webrtc.md)接入。
7. 如需人物检索，阅读[向量检索接口](control-plane/vectors.md)；如需扩展观测事件，阅读 [AHA v1](realtime-api/aha-v1.md)。

## 实现下游推理引擎

- [引擎接入总览](engine-integration/README.md)：能力映射、连接、超时、探测和凭据。
- [gRPC 推理协议](engine-integration/grpc-protocols.md)：ASR、VAD、Embedding、聚类、Diarization 和 TTS。
- [Responses 兼容接口](engine-integration/responses.md)：模型发现和 Responses 流。

## 快速索引

| 主题 | 文档 |
| --- | --- |
| HTTP 错误、WebSocket close code、Realtime error、下游 gRPC Status | [错误索引](reference/errors.md) |
| 全部 HTTP 路径和 Schema | [OpenAPI JSON](reference/openapi.json) |
| OpenAI 原生事件兼容子集 | [Realtime 事件规范](realtime-api/openai-realtime.md) |
| 典型事件顺序 | [Realtime 时序](realtime-api/event-flows.md) |
| 原始 Proto | [`src/audio_harness/protos`](../src/audio_harness/protos/) |
| 可运行客户端 | [`examples`](../examples/) |

## 契约维护

修改 FastAPI 路由或 Schema 后运行：

```bash
poetry run python scripts/export_openapi.py
poetry run pytest -q tests/test_docs_contract.py
```

默认导出会同时更新 `src/web/openapi.json` 与 `docs/reference/openapi.json`。文档测试会校验两份契约、内部链接、HTTP operation 覆盖以及 Engine API 枚举覆盖。
