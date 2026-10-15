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
| `automatic_gain_control` | `audio: pcm_chunk` | `audio: pcm_chunk` | 无直接 Engine | attack、decay、soft-knee、初始电平和末级补偿；固定七段曲线 |
| `streaming_denoise` | `audio: pcm_chunk` | `audio: vad_audio_chunk` | `ux_denoise` | `denoised_weight` 可空，默认关闭混合；默认 `fail_open` |
| `physical_vad` | `audio: pcm_chunk` 或 `detection_audio: vad_audio_chunk`，两者互斥 | `segments: audio_segment`、`speech_chunks: speech_audio_chunk`、`speech_started`、`speech_stopped` | `ux_vad` | 分段时长、缓冲、pre-roll、语音块时长／尾部保留、进度间隔、FireRed 后处理 |
| `semantic_vad` | `segments: audio_segment` | `segments`、`speech_stopped`、`turn_progress` | `ux_vad` | `max_buffer_seconds`，默认 `fail_open` |
| `asr` | `segments: audio_segment`，也接受 `diarization_segment` | `transcripts`、`speaker_transcripts` | `ux_speech` | `remove_punctuation` |
| `audio_embedding` | `segments` | `embeddings` | `engine_feature` | 算法、维度、归一化 |
| `vector_search` | `embeddings` | `results` | 无直接 Engine | Collection、Named Vector、top-k、阈值、filter |
| `speaker_filter` | `segments`、`results` | 通过的 `segments`、`decisions`、`speech_started/stopped` | 无直接 Engine | Speaker ID 白名单、独立门控分数；未匹配和故障均 fail-closed |
| `speaker_diarization` | `segments` | diarization `segments`、`local_results` | `ux_speaker_diarization` | 批次最短/最长秒数、送入 ASR 的最短分段秒数（`min_asr_segment_seconds`，默认 0.5）、可空的 `postprocessing`；默认使用 `audio_harness.diarization.vad_coverage` v1，显式 `null` 保留旧行为 |
| `streaming_speaker_diarization` | `audio: pcm_chunk` 和 `segments: audio_segment` 或 `speech_audio_chunk`，两个输入均必需 | diarization `segments`、`local_results`、`cluster_results` | `ux_speaker_diarization_duplex` | 模型输入缓冲 320／640／1040 ms、待处理音频上限、累计片段上限 |
| `speaker_clustering` | diarization `segments` | `results` | `engine_feature` | embedding/聚类算法、参数、最大向量数 |

客户端不应把本表硬编码为动态插件目录；编辑器应调用 `/audio-node-types` 读取当前端口和 `config_schema`。

`physical_vad.audio` 是主音频入口，VAD 判定、切片及后续节点都使用同一份 PCM；
`physical_vad.detection_audio` 是仅检测入口，只接受私有 `vad_audio_chunk`。同步降噪
响应中的处理音频用于 VAD 判定，响应中的原音用于 VAD 切片，因此其他引擎不会看到
纯降噪或加权波形。两个输入必须且只能连接一个。
`denoised_weight=w` 时检测音频为 `(1-w) × 原音 + w × 降噪音频`；字段为 `null`
时直接使用纯降噪音频。

`physical_vad.speech_chunks` 是图内专用的有效语音块出口，只能连接
`streaming_speaker_diarization.segments`，不能直接连接普通 ASR、语义 VAD 或绑定工作流出口。
它与完整的 `segments` 出口使用相同的原始 PCM 和物理段身份，增量块不会触发新的
`speech_started/stopped`。默认 `speech_chunk_seconds=1`、
`speech_chunk_holdback_seconds=1`，仅在该出口有消费者时启用增量处理。
保留时长用于等待可能回溯的停顿边界，应覆盖引擎实际的回溯范围；边界回溯到已输出
音频范围时明确失败，不能撤回已经送入 ASR 的音频。结束时排空全部尾部，
没有剩余 PCM 时仍发出一次轮次结束标记。语音块时长与保留时长之和不能超过音频缓冲时长。

流式分离持续接收含静音的完整音频。有效语音块仅进入缓存，不会直接成为 ASR 请求。
开放的说话人片段通过 `diarization_local.delta` 增量更新，重复更新保持相同的
`batch_id` 和 `segment_id`；只有模型边界闭合且对应 PCM 收齐后才发布
`diarization_local.completed` 并将完整片段送入 ASR。正常 EOF 排空模型闭合后的尾部，
超过缓存限制或推理失败时显式失败开放，不静默丢弃有效语音。
同一物理段的分角色转写共享 `item_source_id`，下游 ASR 的 `transcripts` 在该段完成后
合并输出一次；`min_asr_segment_seconds` 对闭合的完整片段生效。流式会议模板的独立
主转写分支仍由语义 VAD 划分轮次。旧 `segments` 输入继续按原行为处理。
迁移已有工作流时，先更新并重启服务，通过 `GET /audio-node-types` 确认新增出口，
再读取当前 draft，将流式分离 `segments` 入边改为主物理 VAD 的 `speech_chunks`，
使用 `expected_revision` 保存，校验后发布。完整 `audio` 输入仍需与 VAD 同源，包含静音。

`automatic_gain_control` 是无前瞻、跨块保持包络状态的本地 `PCM → PCM` 节点，输出
连接 `physical_vad.audio` 后，VAD 与所有后续节点都会使用 AGC 波形。默认参数为
`attack_seconds=0`、`decay_seconds=2.5`、`soft_knee_db=6`、
`initial_level_db=-12`、`output_gain_db=4.5`，points 固定为
`-80/-80|-55/-42|-40/-28|-30/-18|-20/-13|-10/-9|0/-6`。每条会话通过 PyAV
持有独立的 FFmpeg `libavfilter` graph，直接执行 `compand,volume`，因此 detector、
时间常数和 soft-knee 均沿用 FFmpeg 实现；`delay=0` 不引入额外采样缓存。

## 稳定命名出口

每个可发布工作流必须提供并匹配类型：

- `primary_transcript`：正式 `transcription`。
- `speech_started`：物理 VAD 起点。
- `speech_stopped`：正式轮次终点，可来自物理或语义 VAD。

可选出口：`interim_transcript`、`turn_progress`、`vector_search`、`speaker_filter`、`diarization_local`、`speaker_transcript`、`diarization_cluster`。它们分别驱动 AHA 扩展；不存在或未协商 `x_aha=v1` 时不会产生对应扩展事件。

`speaker_filter` 必须把同一个片段出口同时接到自身 `segments` 和上游
`audio_embedding → vector_search` 分支。主转写经过该节点时，
`speech_started`、`speech_stopped` 和 `speaker_filter` 命名出口都必须连接过滤节点，
保证被拒绝的语音不会创建 Realtime 用户项。门控使用节点自己的 `min_score`，
不依赖检索节点的 `score_threshold`。若过滤前使用语义 VAD，还应把语义 VAD 的
`turn_progress` 暴露为同名出口，以便 AHA v1 客户端观察物理停顿而不提前创建用户项。

物理 VAD 的 `progress_interval_seconds` 控制 AHA v1 进度的墙钟限频，默认 `0.2` 秒，`0` 表示关闭，必须是有限非负数。Harness 自动选择 `speech_started` 出口绑定的物理 VAD 作为唯一进度源，因此旧工作流无需增加进度出口或重新发布，双 VAD 工作流的预览分支也不会重复发送。

## 编译校验

校验检查节点/端口存在、类型兼容、单输入 fan-in、环、不可达节点、断开输出、Engine 存在且启用、API 能力、稳定出口及节点配置。业务编译失败通常由 validate 接口以 `200 valid=false` 返回诊断；在发布等需要异常的路径中返回 `422 audio_graph_invalid`。

`GraphDiagnosticRead` 提供 `code/message/node_id/edge_id/port`。客户端应使用 `code` 做定位，`message` 用于展示，不解析中文文本。

## 发布和恢复

- 发布按 `expected_revision` 重新读取并编译草稿、准备全部引擎，再原子保存 revision。
- `engine_revisions` 以 node ID 为 key，值为发布时 Engine revision。
- active 读取在未发布时返回 `412 precondition_failed`。
- restore 不修改 active；它占用一个新的 draft revision，因此仍需 validate 和 publish。
