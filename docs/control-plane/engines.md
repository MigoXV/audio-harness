# 推理引擎控制面

Engine Asset 描述一个可连接的推理服务。Workflow/Agent 发布时会记录 Engine Snapshot（`id`、`revision`、目标、协议、超时、凭据引用和 details），保证活动会话不受后续资产编辑影响。

## 类型枚举

| `kind` | `api` | `protocol` | 用途 |
| --- | --- | --- | --- |
| `asr` | `ux_speech` | `grpc` | ASR |
| `vad` | `ux_vad` | `grpc` | 物理或语义 VAD |
| `llm` | `responses` | `https` | OpenAI-compatible Responses |
| `tts` | `qwen_tts_duplex` | `grpc` | 双工 TTS |
| `embedding` | `engine_feature` | `grpc` | 音频向量和说话人聚类 |
| `diarization` | `ux_speaker_diarization` | `grpc` | 说话人分离 |

`kind` 是管理分类，真正决定适配器和工作流能力的是 `api`。创建时应使用匹配组合；发布校验会拒绝能力不匹配的绑定。

## Engine Schema

| 字段 | 约束/语义 |
| --- | --- |
| `slug` | 1..100，唯一，小写 kebab-case |
| `name` | 1..200 |
| `target` | gRPC 为 `host:port`；Responses 为 HTTP(S) base URL 或以 `/responses` 结尾的 URL |
| `connect_timeout_seconds` | `> 0`，默认 `10` |
| `request_timeout_seconds` | 可空；设置时必须 `> 0` |
| `credential_env` | 环境变量名；仅 Responses 适配器读取 |
| `api_key` | 可直接保存凭据；优先于 `credential_env`，响应当前也会返回该字段，调用方必须保护结果 |
| `enabled` | 禁用后不能被新发布版本选用 |
| `status` | `unknown/checking/healthy/unhealthy/stale/disabled/observed_running/confirmed_running/unavailable/inactive` |
| `details` | 适配器扩展 JSON，不承诺跨 `api` 通用字段 |
| `revision` | 每次更新递增，发布快照固定该值 |

## 接口

| 方法和路径 | 请求 | 成功响应 | 主要错误 |
| --- | --- | --- | --- |
| `GET /api/v1/engines` | `limit/offset/kind/api/enabled/q` | `200 EngineList` | `422` 参数错误 |
| `POST /api/v1/engines` | `EngineCreate` | `201 EngineRead` | `409` slug 冲突 |
| `GET /api/v1/engines/{engine_id}` | 路径 ID | `200 EngineRead` | `404` |
| `PATCH /api/v1/engines/{engine_id}` | `EnginePatch`，只更新出现字段 | `200 EngineRead` | `404/409/422` |
| `DELETE /api/v1/engines/{engine_id}` | 无 | `204` | `404/409` 被引用 |
| `GET /api/v1/engines/{engine_id}/models` | 无 | `200 EngineModelListRead` | `404/409/502/504` |
| `POST /api/v1/engines/{engine_id}/validate` | 无 | `200 EngineValidationRead` | `404`；探测结果也可在 body 中表达失败 |
| `POST /api/v1/engines/validate` | 无 | 所有 enabled Engine 的结果 | 服务级错误 |
| `GET /api/v1/engines/{engine_id}/usage` | 无 | 引用字符串数组 | `404` |

<!-- API: GET /api/v1/engines -->
<!-- API: POST /api/v1/engines -->
<!-- API: GET /api/v1/engines/{engine_id} -->
<!-- API: PATCH /api/v1/engines/{engine_id} -->
<!-- API: DELETE /api/v1/engines/{engine_id} -->
<!-- API: GET /api/v1/engines/{engine_id}/models -->
<!-- API: POST /api/v1/engines/{engine_id}/validate -->
<!-- API: POST /api/v1/engines/validate -->
<!-- API: GET /api/v1/engines/{engine_id}/usage -->

## 创建示例

```bash
curl -sS http://localhost:8000/api/v1/engines \
  -H 'Content-Type: application/json' \
  -d '{
    "slug": "meeting-asr",
    "name": "Meeting ASR",
    "kind": "asr",
    "api": "ux_speech",
    "protocol": "grpc",
    "target": "asr.internal:50051",
    "connect_timeout_seconds": 10,
    "request_timeout_seconds": 60,
    "enabled": true
  }'
```

Responses 引擎示例：

```json
{
  "slug": "responses-main",
  "name": "Responses Main",
  "kind": "llm",
  "api": "responses",
  "protocol": "https",
  "target": "https://llm.example.test/v1",
  "credential_env": "RESPONSES_API_KEY",
  "enabled": true
}
```

## 探测与模型发现

- gRPC 引擎的基础 `probe` 等待 channel ready；它证明目标可连接，不保证每个业务 RPC 的模型可用。
- Responses 探测调用 `GET /models`；`/models` 超时映射为 `504 engine_model_discovery_timeout`，上游失败映射为 `502`。
- `GET .../models` 只对支持模型发现的能力有意义；能力不支持时返回 `409` 和具体错误码。
- `EngineValidationResultRead.outcome`、`message`、`latency_ms` 应用于界面诊断，不应替代 HTTP 层错误判断。

## 更新、引用与删除

删除前调用 usage 接口。`references` 可能包含 Workflow/Agent 草稿或活动 revision 引用；存在引用时服务拒绝删除。更新 Engine 只产生新 revision，不会修改已经发布到 Workflow/Agent 中的快照。若要让新配置生效，必须重新校验并发布依赖资源。

