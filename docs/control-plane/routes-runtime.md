# Realtime Route、运行设置与健康状态

## Realtime Route

Route 将客户端可见 `model` 映射到已激活的 Audio Workflow 或 Voice Agent。

| 方法和路径 | 请求 | 成功响应 |
| --- | --- | --- |
| `GET /api/v1/realtime-routes` | 无 | 全部 Route |
| `GET /api/v1/realtime-routes/{model}` | model | Route |
| `PUT /api/v1/realtime-routes/{model}` | `RealtimeRouteUpsert` | 创建或更新 Route |
| `DELETE /api/v1/realtime-routes/{model}` | 无 | `204` |

<!-- API: GET /api/v1/realtime-routes -->
<!-- API: GET /api/v1/realtime-routes/{model} -->
<!-- API: PUT /api/v1/realtime-routes/{model} -->
<!-- API: DELETE /api/v1/realtime-routes/{model} -->

```bash
curl -sS -X PUT http://localhost:8000/api/v1/realtime-routes/meeting-agent \
  -H 'Content-Type: application/json' \
  -d '{
    "model":"meeting-agent",
    "target_type":"voice_agent",
    "target_id":"agent_xxx",
    "visibility":"public",
    "replace_existing":false
  }'
```

路径 model 必须等于 body model，格式为小写字母/数字开头，后续可含 `._-`，最长 100。`target_type` 为 `audio_workflow` 或 `voice_agent`；`visibility` 为 `public` 或 `internal`。Realtime 数据面只能使用 public 且目标已激活的 Route。

已存在 model 指向不同目标时，默认返回 `409 realtime_route_conflict`，`details` 给出当前目标。用户确认后重发 `replace_existing=true`。更新 Route 不迁移已建立会话。

## Workflow 运行设置

| 方法和路径 | 请求/响应 |
| --- | --- |
| `GET /api/v1/settings/workflow` | 当前队列、语言设置和 revision |
| `PUT /api/v1/settings/workflow` | 完整运行配置和 `revision` |

<!-- API: GET /api/v1/settings/workflow -->
<!-- API: PUT /api/v1/settings/workflow -->

运行配置字段：`min_chunk_bytes`（偶数且至少 2，默认 640）、`default_language_code`、`audio_queue_capacity`、`segment_queue_capacity`、`result_queue_capacity`，三个队列容量均必须大于 0。

## 容量设置与计数器

| 方法和路径 | 请求/响应 |
| --- | --- |
| `GET /api/v1/settings/capacity` | 当前容量和 revision |
| `PUT /api/v1/settings/capacity` | 完整 `CapacitySettingsUpdate` |
| `GET /api/v1/runtime/status` | 每个 limiter 的 active/capacity/rejected 和进程 started_at |

<!-- API: GET /api/v1/settings/capacity -->
<!-- API: PUT /api/v1/settings/capacity -->
<!-- API: GET /api/v1/runtime/status -->

会话上限分别为 `realtime.max_sessions`、`webrtc.max_sessions`，默认均为 64。推理上限为 `max_asr_concurrency=8`、`max_embedding_concurrency=8`、`max_qdrant_concurrency=8`、`max_responses_concurrency=8`、`max_tts_concurrency=1`；所有值必须大于 0。

达到会话或即时推理容量时返回 HTTP `429` 或 Realtime `error`。`rejected` 是进程内累计计数，重启后清零。

## 总览和探针

| 方法和路径 | 状态语义 |
| --- | --- |
| `GET /api/v1/runtime/overview` | 始终 `200`；返回 `ready/needs_configuration/unavailable` 和可操作 issues |
| `GET /api/v1/health/live` | 进程存活，`200` |
| `GET /api/v1/health/ready` | 数据库与控制面可用时 `200`，不可用时 `503` |

<!-- API: GET /api/v1/runtime/overview -->
<!-- API: GET /api/v1/health/live -->
<!-- API: GET /api/v1/health/ready -->

根路径 `/health/live` 和 `/health/ready` 也存在，但为了不重复，未放进 OpenAPI。Kubernetes liveness 使用 live；readiness 使用 ready；管理控制台使用 overview 展示未配置的 Realtime 路由。

`RuntimeIssueRead` 包含稳定 `code`、`severity=warning|error`、`scope=control_plane|realtime`、消息和可选 resource ID。自动化应根据 code/state 决策，不解析中文 message。
