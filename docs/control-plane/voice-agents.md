# 语音智能体控制面

Voice Agent 在已发布 Audio Workflow 上绑定 OpenAI-compatible Responses、双工 TTS、音色和工具策略。它同样采用 draft → validate → immutable revision → Realtime Route 的显式生命周期。

## Agent 接口

| 方法和路径 | 请求/语义 | 成功响应 |
| --- | --- | --- |
| `GET /api/v1/voice-agents` | 列表 | `VoiceAgentRead[]` |
| `POST /api/v1/voice-agents` | 元数据和 `VoiceAgentDefinition` | `201 VoiceAgentRead` |
| `GET /api/v1/voice-agents/{profile_id}` | 摘要 | `VoiceAgentRead` |
| `PATCH /api/v1/voice-agents/{profile_id}` | `name/description` | 更新摘要 |
| `DELETE /api/v1/voice-agents/{profile_id}` | 删除 | `204`；被 Route 引用时冲突 |
| `GET .../{profile_id}/draft` | 草稿 | `VoiceAgentDraftRead` |
| `PUT .../{profile_id}/draft` | `expected_revision + definition` | 新草稿 revision |
| `POST .../{profile_id}/validate` | 校验依赖 | `valid + diagnostics` |
| `POST .../{profile_id}/publish` | `expected_revision` | Agent/Workflow/Engine 固定 revision |
| `GET .../{profile_id}/active` | 活动版本 | `VoiceAgentRevisionRead` |
| `GET .../{profile_id}/revisions` | 历史 | revision 数组 |
| `POST .../{profile_id}/revisions/{revision}/restore` | `expected_revision` | 历史复制为草稿 |

<!-- API: GET /api/v1/voice-agents -->
<!-- API: POST /api/v1/voice-agents -->
<!-- API: GET /api/v1/voice-agents/{profile_id} -->
<!-- API: PATCH /api/v1/voice-agents/{profile_id} -->
<!-- API: DELETE /api/v1/voice-agents/{profile_id} -->
<!-- API: GET /api/v1/voice-agents/{profile_id}/draft -->
<!-- API: PUT /api/v1/voice-agents/{profile_id}/draft -->
<!-- API: POST /api/v1/voice-agents/{profile_id}/validate -->
<!-- API: POST /api/v1/voice-agents/{profile_id}/publish -->
<!-- API: GET /api/v1/voice-agents/{profile_id}/active -->
<!-- API: GET /api/v1/voice-agents/{profile_id}/revisions -->
<!-- API: POST /api/v1/voice-agents/{profile_id}/revisions/{revision}/restore -->

## Definition

```json
{
  "schema_version": 1,
  "audio_workflow_id": "wf_xxx",
  "responses_engine_id": "eng_responses",
  "responses": {
    "model": "voice-model",
    "thinking_enabled": false,
    "instructions": "请简洁回答。",
    "max_output_tokens": 1024,
    "max_tool_rounds": 8,
    "max_parallel_tools": 4,
    "tool_timeout_seconds": 30,
    "service_tools": [],
    "mcp_servers": [],
    "history_max_items": 128
  },
  "tts_engine_id": "eng_tts",
  "tts": {
    "default_voice": "voice_xxx",
    "allowed_voices": ["voice_xxx"],
    "language": "Auto",
    "decoder_chunk_size": 0
  }
}
```

约束：Responses Engine 已绑定时 `model` 不能为空；TTS Engine 已绑定时 `allowed_voices` 非空且包含 `default_voice`。发布要求所引用 Workflow 已激活，Responses/TTS Engine 存在、启用且 API 匹配，引用 Voice Asset 存在。

## Responses、历史和工具

- `thinking_enabled` 控制运行时是否请求思考能力；具体支持由下游模型决定。
- `max_output_tokens` 为 `1..32768`；`max_tool_rounds` 为 `1..32`；`max_parallel_tools` 为 `1..16`；工具超时为 `(0,300]` 秒。
- `history_max_items` 为 `8..4096`，限制传给 Responses 的会话历史项。
- `service_tools` 引用进程启动前注入 `ToolRegistry` 的名称。未注册工具会在运行时失败；本仓库默认不内置业务工具。
- MCP 工具公开名为 `mcp__<server_label>__<remote_name>`，连接先尝试 Streamable HTTP，失败后回退 SSE。

MCP 配置：

```json
{
  "server_label": "knowledge",
  "server_url": "https://mcp.example.test/mcp",
  "require_approval": "always",
  "allowed_tools": ["search"],
  "headers_env": {"Authorization": "KNOWLEDGE_MCP_AUTH"}
}
```

`headers_env` 的 value 是环境变量名，不是密钥本身。`require_approval` 支持 `always`、`never` 或带 always/never tool rule 的对象。

## 音色资源

| 方法和路径 | 请求 | 成功响应 |
| --- | --- | --- |
| `GET /api/v1/voices` | 无 | `VoiceAssetRead[]` |
| `POST /api/v1/voices` | multipart `name`、可选 `ref_text`、`file` | `201 VoiceAssetRead` |
| `DELETE /api/v1/voices/{voice_id}` | 无 | `204` |

<!-- API: GET /api/v1/voices -->
<!-- API: POST /api/v1/voices -->
<!-- API: DELETE /api/v1/voices/{voice_id} -->

```bash
curl -sS http://localhost:8000/api/v1/voices \
  -F 'name=客服音色' \
  -F 'ref_text=您好，请问有什么可以帮助您？' \
  -F 'file=@reference.wav;type=audio/wav'
```

返回字段包括 `id/name/ref_text/sample_rate/duration_seconds/created_at`。删除被 Agent draft/active revision 使用的音色前应先解除引用；运行中的会话持有已解析的音色快照。

## 发布结果

`VoiceAgentRevisionRead` 固定 Agent `revision`、完整 definition、`audio_workflow_id/revision`，并分别记录 Responses 与 TTS Engine revision。发布不会自动创建 Realtime Route；必须在[接入与运行](routes-runtime.md)中显式配置公开 model。

