# 语音工作流控制面

Audio Workflow 是类型化、无环的音频处理图。资源有可编辑 draft 和不可变 active revision；发布与对外接入是两个独立动作。

## 生命周期接口

| 方法和路径 | 请求/语义 | 成功响应 |
| --- | --- | --- |
| `GET /api/v1/audio-node-types` | 节点目录、端口和 JSON Schema | `AudioNodeTypeRead[]` |
| `GET /api/v1/audio-workflows` | 列表 | `AudioWorkflowRead[]` |
| `POST /api/v1/audio-workflows` | `slug/name/description/document?` | `201 AudioWorkflowRead` |
| `GET /api/v1/audio-workflows/{workflow_id}` | 摘要 | `AudioWorkflowRead` |
| `PATCH /api/v1/audio-workflows/{workflow_id}` | 仅 `name/description` | 更新后的摘要 |
| `DELETE /api/v1/audio-workflows/{workflow_id}` | 删除资源 | `204`；被引用时冲突 |
| `GET .../{workflow_id}/draft` | 当前草稿和 revision | `AudioWorkflowDraftRead` |
| `PUT .../{workflow_id}/draft` | `expected_revision + document` | 新草稿 revision |
| `POST .../{workflow_id}/validate` | 编译草稿，不发布 | `valid/diagnostics/topological_order` |
| `POST .../{workflow_id}/publish` | `expected_revision` | 不可变 revision 与 Engine revision 映射 |
| `GET .../{workflow_id}/active` | 当前活动版本 | `AudioWorkflowRevisionRead` |
| `GET .../{workflow_id}/revisions` | 发布历史 | revision 数组 |
| `POST .../{workflow_id}/revisions/{revision}/restore` | `expected_revision` | 将历史文档复制为新草稿 |

<!-- API: GET /api/v1/audio-node-types -->
<!-- API: GET /api/v1/audio-workflows -->
<!-- API: POST /api/v1/audio-workflows -->
<!-- API: GET /api/v1/audio-workflows/{workflow_id} -->
<!-- API: PATCH /api/v1/audio-workflows/{workflow_id} -->
<!-- API: DELETE /api/v1/audio-workflows/{workflow_id} -->
<!-- API: GET /api/v1/audio-workflows/{workflow_id}/draft -->
<!-- API: PUT /api/v1/audio-workflows/{workflow_id}/draft -->
<!-- API: POST /api/v1/audio-workflows/{workflow_id}/validate -->
<!-- API: POST /api/v1/audio-workflows/{workflow_id}/publish -->
<!-- API: GET /api/v1/audio-workflows/{workflow_id}/active -->
<!-- API: GET /api/v1/audio-workflows/{workflow_id}/revisions -->
<!-- API: POST /api/v1/audio-workflows/{workflow_id}/revisions/{revision}/restore -->

创建、保存、校验、发布示例：

```bash
WORKFLOW_ID=$(curl -sS http://localhost:8000/api/v1/audio-workflows \
  -H 'Content-Type: application/json' \
  -d '{"slug":"meeting","name":"Meeting"}' | jq -r .id)

curl -sS -X POST "http://localhost:8000/api/v1/audio-workflows/$WORKFLOW_ID/validate"
curl -sS -X POST "http://localhost:8000/api/v1/audio-workflows/$WORKFLOW_ID/publish" \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":1}'
```

新建资源若未传 `document`，会获得默认的 Physical VAD → ASR 图，但其中 Engine 仍需绑定。

## `AudioWorkflowDocument`

```json
{
  "schema_version": 1,
  "nodes": [],
  "edges": [],
  "outputs": [],
  "layout": {}
}
```

- `nodes` 是带 `kind` 判别字段的节点联合。
- `edges[].source` 可以是图输入 `{kind:"input",name:"audio"}` 或节点输出；target 必须是节点输入。
- `outputs` 将内部端口绑定为稳定的业务出口。
- `layout` 只保存节点坐标，不参与执行语义；key 必须引用存在的节点。
- Node ID、Edge ID、Output name 各自在文档内唯一；未知字段被拒绝。

## 节点和端口

| kind | 输入 | 输出 | Engine API | 关键配置 |
| --- | --- | --- | --- | --- |
| `physical_vad` | `audio: pcm_chunk` | `segments: audio_segment`、`speech_started`、`speech_stopped` | `ux_vad` | 分段时长、缓冲、pre-roll、进度间隔、FireRed 后处理 |
| `semantic_vad` | `segments: audio_segment` | `segments`、`speech_stopped`、`turn_progress` | `ux_vad` | `max_buffer_seconds`，默认 `fail_open` |
| `asr` | `segments: audio_segment`，也接受 `diarization_segment` | `transcripts`、`speaker_transcripts` | `ux_speech` | `remove_punctuation` |
| `audio_embedding` | `segments` | `embeddings` | `engine_feature` | 算法、维度、归一化 |
| `vector_search` | `embeddings` | `results` | 无直接 Engine | Collection、Named Vector、top-k、阈值、filter |
| `speaker_diarization` | `segments` | diarization `segments`、`local_results` | `ux_speaker_diarization` | 批次最短/最长秒数、送入 ASR 的最短分段秒数（`min_asr_segment_seconds`，默认 0.5） |
| `speaker_clustering` | diarization `segments` | `results` | `engine_feature` | embedding/聚类算法、参数、最大向量数 |

客户端不应把本表硬编码为动态插件目录；编辑器应调用 `/audio-node-types` 读取当前端口和 `config_schema`。

## 稳定命名出口

每个可发布工作流必须提供并匹配类型：

- `primary_transcript`：正式 `transcription`。
- `speech_started`：物理 VAD 起点。
- `speech_stopped`：正式轮次终点，可来自物理或语义 VAD。

可选出口：`interim_transcript`、`turn_progress`、`vector_search`、`diarization_local`、`speaker_transcript`、`diarization_cluster`。它们分别驱动 AHA 扩展；不存在或未协商 `x_aha=v1` 时不会产生对应扩展事件。

物理 VAD 的 `progress_interval_seconds` 控制 AHA v1 进度的墙钟限频，默认 `0.2` 秒，`0` 表示关闭，必须是有限非负数。Harness 自动选择 `speech_started` 出口绑定的物理 VAD 作为唯一进度源，因此旧工作流无需增加进度出口或重新发布，双 VAD 工作流的预览分支也不会重复发送。

## 编译校验

校验检查节点/端口存在、类型兼容、单输入 fan-in、环、不可达节点、断开输出、Engine 存在且启用、API 能力、稳定出口及节点配置。业务编译失败通常由 validate 接口以 `200 valid=false` 返回诊断；在发布等需要异常的路径中返回 `422 audio_graph_invalid`。

`GraphDiagnosticRead` 提供 `code/message/node_id/edge_id/port`。客户端应使用 `code` 做定位，`message` 用于展示，不解析中文文本。

## 发布和恢复

- 发布按 `expected_revision` 重新读取并编译草稿、准备全部引擎，再原子保存 revision。
- `engine_revisions` 以 node ID 为 key，值为发布时 Engine revision。
- active 读取在未发布时返回 `412 precondition_failed`。
- restore 不修改 active；它占用一个新的 draft revision，因此仍需 validate 和 publish。
