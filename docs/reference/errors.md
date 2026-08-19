# 错误索引

## HTTP 错误包络

```json
{"error":{"code":"not_found","message":"资源不存在","details":null}}
```

| HTTP | code | 主要来源 | 是否建议原请求重试 |
| --- | --- | --- | --- |
| `400` | `invalid_request` | 路径/body 不一致、JSON object、confirmation、业务参数 | 否，修正请求 |
| `400` | WebRTC 专用 code | SDP/session/encoding/offer/AHA 版本 | 否 |
| `404` | `not_found` | Engine、Workflow/revision、Agent、Route、Voice、Call、Vector 资源不存在 | 否；先重新发现资源 |
| `409` | `revision_conflict` | 乐观锁、资源/向量冲突 | 重新读取合并后重试 |
| `409` | `realtime_route_conflict` | model 已映射其他目标 | 用户确认后 `replace_existing=true` |
| `409` | Engine capability code | 模型发现/能力不支持 | 修复 Engine 配置 |
| `412` | `precondition_failed` | 未发布、依赖/设置不满足、发布准备失败 | 修复前置条件 |
| `422` | `request_validation_failed` | FastAPI/Pydantic 字段、枚举、范围或未知字段 | 否 |
| `422` | `audio_graph_invalid` | 工作流编译失败 | 按 `details[]` 修图 |
| `429` | `capacity_exceeded` | 会话或推理容量满 | 带抖动退避 |
| `502` | Engine discovery code | `/models` 上游失败 | 有限重试并告警 |
| `503` | `qdrant_unavailable` | Qdrant 数据面不可用 | 有限重试 |
| `503` | health response | readiness 未满足 | 修复配置/依赖 |
| `504` | Engine discovery timeout code | `/models` 超时 | 有限重试 |
| `500` | `service_error` | Catalog/Repository 未分类错误 | 告警，谨慎重试 |

校验错误 `details` 是 Pydantic error 数组；图错误是 `code/message/node_id/edge_id/port` 数组；Route 冲突 details 是当前 `model/target_type/target_id/visibility`。

## WebSocket close code

| code | reason 示例 | 阶段 |
| --- | --- | --- |
| `4400` | `Specify exactly one of intent or model`、`Unsupported realtime intent`、`Unsupported x_aha version`、目标不是 Agent | HTTP upgrade/路由解析前后 |
| `4404` | `Model not found` | Route 不存在/不可解析 |
| `4409` | `Model workflow not active` | 目标未发布 |
| `4429` | `Session capacity exceeded` | accept 后预留容量失败 |

close 前没有统一 JSON body，客户端必须记录 close code/reason。建连成功后的业务错误使用 Realtime `error`，通常不关闭连接。

## Realtime `error`

结构遵循 OpenAI Realtime：

```json
{
  "type":"error",
  "event_id":"event_server",
  "error":{
    "type":"invalid_request_error",
    "code":"session_not_configured",
    "message":"发送音频前必须先通过 session.update 配置转写模型",
    "event_id":"event_client",
    "param":"type"
  }
}
```

常见 code：

| code | 处理 |
| --- | --- |
| `invalid_event` | 修复 UTF-8/JSON/SDK event Schema |
| `message_too_large` | 缩小音频 chunk 或事件 |
| `session_not_configured` | 先发送有效 session.update model |
| `session_update_after_audio` | clear 或 commit 后再改输入契约 |
| `unsupported_event/unsupported_audio_event` | 改用已支持事件；WebRTC 音频走 RTP |
| `input_audio_buffer_commit_empty` | 先 append/RTP 音频 |
| `invalid_value/invalid_response` | 修正字段值或 response override |
| `response_cancel_not_active` | 刷新活动 response ID，不盲重试 |
| `workflow_failed/transcription_failed/audio_track_failed` | 记录诊断，检查 Workflow/Engine |
| `capacity_exceeded` | 退避或扩容 |

`error.event_id` 是触发错误的客户端 event ID，可能为空；顶层 `event_id` 是服务端 error 事件 ID。

## 下游 gRPC Status

Audio Harness 将下游 `DEADLINE_EXCEEDED` 归类为 deadline；其他 `AioRpcError` 归类为 unavailable。引擎实现方仍应精确返回 `INVALID_ARGUMENT`、`FAILED_PRECONDITION`、`RESOURCE_EXHAUSTED` 等，便于日志定位和未来细化映射。

## 重试原则

- 可重试：容量、deadline、unavailable、502/503/504，使用指数退避、随机抖动和总时限。
- 条件重试：revision conflict 先重新读取并合并；Route conflict 需要明确用户确认。
- 不可直接重试：校验、图编译、未发布、unsupported event、认证/权限（由网关返回）。
- 创建/发布等非幂等动作重试前先查询资源状态；不要因为网络超时重复创建 slug。
