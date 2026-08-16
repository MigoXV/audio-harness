# 向量检索控制面

Qdrant 是 Collection、Named Vector、Point 和 payload 的唯一事实来源；Audio Harness 不在 SQLite 中复制 Vector Space 注册表。向量接口只有在配置 `QDRANT_GRPC_TARGET` 时挂载，`QDRANT_HTTP_URL` 用于 Dashboard/健康展示。

## 基础设施与 Collection

| 方法和路径 | 请求 | 成功响应 |
| --- | --- | --- |
| `GET /api/v1/vector-infrastructure` | 无 | `configured`、`ready`、gRPC target、HTTP URL |
| `GET /api/v1/vector-spaces` | 无 | Qdrant Collection 列表 |
| `POST /api/v1/vector-spaces` | `VectorSpaceCreate` | `201 VectorSpaceRead` |
| `GET /api/v1/vector-spaces/{collection_name}` | 名称 | `VectorSpaceRead` |
| `POST /api/v1/vector-spaces/{collection_name}/destroy` | `{"confirmation":"<collection_name>"}` | `204` |

<!-- API: GET /api/v1/vector-infrastructure -->
<!-- API: GET /api/v1/vector-spaces -->
<!-- API: POST /api/v1/vector-spaces -->
<!-- API: GET /api/v1/vector-spaces/{collection_name} -->
<!-- API: POST /api/v1/vector-spaces/{collection_name}/destroy -->

Named Vector：`name` 匹配 `[A-Za-z0-9_-]+`，`size` 为 `1..65536`，`distance` 为 `cosine/euclid/dot/manhattan`。名称在同一 Collection 中不能重复。

```bash
curl -sS http://localhost:8000/api/v1/vector-spaces \
  -H 'Content-Type: application/json' \
  -d '{
    "collection_name": "voiceprints",
    "template": "voiceprint",
    "vectors": [{"name":"eres2netv2","size":192,"distance":"cosine"}]
  }'
```

`voiceprint` 模板会建立并通过索引识别以下 payload：`speaker_id` keyword、`channel_id` keyword、`captured_at` integer（Unix ms）、`quality_score` float。人物搜索只允许该模板；普通 Collection 仍支持 Point 和 payload 管理。

销毁要求 confirmation 与 Collection 名完全一致。不存在返回 `404`，重名/状态冲突返回 `409 revision_conflict`，Qdrant 不可用返回 `503 qdrant_unavailable`。

## 音频登记

`POST /api/v1/vector-spaces/{collection_name}/points/audio` 使用 multipart：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `audio` | 是 | 可解码音频文件 |
| `embeddings_json` | 是 | JSON 数组，每项给出 `vector_name`、`engine_id`、`algorithm`、`normalize` 等嵌入配置 |
| `payload_json` | 否 | JSON object，默认 `{}` |
| `point_id` | 否 | 不传则生成 |
| `speaker_id/channel_id` | voiceprint 必需 | 非空且最长 128 |
| `captured_at` | 否 | Unix ms，缺省为服务端时间 |
| `quality_score` | 否 | `0..1`，默认 `0` |

<!-- API: POST /api/v1/vector-spaces/{collection_name}/points/audio -->

```bash
curl -sS http://localhost:8000/api/v1/vector-spaces/voiceprints/points/audio \
  -F 'audio=@sample.wav;type=audio/wav' \
  -F 'embeddings_json=[{"vector_name":"eres2netv2","engine_id":"eng_xxx","algorithm":"eres2netv2","normalize":true}]' \
  -F 'speaker_id=speaker-001' \
  -F 'channel_id=microphone-1' \
  -F 'payload_json={"department":"support"}'
```

返回 `201 {"point_id":"..."}`。输入会统一为 16 kHz、单声道 PCM S16LE；引擎返回的小端 float32 必须与 Named Vector 维度一致。

## VAD 预处理与人物搜索

`POST .../search/audio/vad-preview` 在正式登记/搜索前用指定 VAD 合并所有语音区间，返回 Base64 WAV 和 `duration_ms`。`vad_postprocessing_json` 可传 FireRed v1 参数。

<!-- API: POST /api/v1/vector-spaces/{collection_name}/search/audio/vad-preview -->

`POST .../search/audio` multipart 字段：`audio`、`vector_name`、`engine_id`、`algorithm` 必填；`normalize=true`、`top_k=10`、`score_threshold` 可空、`filter_json={}`。`top_k` 为候选人数而非观测数，范围 `1..100`。

<!-- API: POST /api/v1/vector-spaces/{collection_name}/search/audio -->

```bash
curl -sS http://localhost:8000/api/v1/vector-spaces/voiceprints/search/audio \
  -F 'audio=@query.wav;type=audio/wav' \
  -F 'vector_name=eres2netv2' \
  -F 'engine_id=eng_xxx' \
  -F 'algorithm=eres2netv2' \
  -F 'top_k=5' \
  -F 'filter_json={"must":[{"key":"department","match":{"value":"support"}}]}'
```

响应包含查询 `embedding` 和按 `speaker_id` 分组的 `groups`。每组 `score` 是该人最高相似度观测的分数，`observation` 含代表 Point ID 和 payload。

## Point 管理

| 方法和路径 | 请求 | 响应 |
| --- | --- | --- |
| `POST .../points/scroll` | `filter`、`limit=1..200`、`offset`、`with_vectors` | `items`、不透明 `next_offset` |
| `PUT .../points/{point_id}/payload` | `{"payload":{...}}` | `204`，整体替换 payload |
| `DELETE .../points/{point_id}` | 无 | `204` |

<!-- API: POST /api/v1/vector-spaces/{collection_name}/points/scroll -->
<!-- API: PUT /api/v1/vector-spaces/{collection_name}/points/{point_id}/payload -->
<!-- API: DELETE /api/v1/vector-spaces/{collection_name}/points/{point_id} -->

`offset` 由 Qdrant 返回，客户端必须原样保存。`with_vectors=false` 是默认值，避免列表响应携带大向量。payload 替换可能影响 voiceprint 模板识别字段，调用方应保留必填业务字段。

