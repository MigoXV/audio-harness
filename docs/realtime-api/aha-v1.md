# AHA Realtime 扩展协议 v1

AHA v1 为 OpenAI Realtime 事件流追加 Audio Harness 特有的中间转写、语义 VAD、人物检索和说话人事件。它不替换、删除或改写任何原生事件。

## 协商与版本

- WebSocket：`/v1/realtime?...&x_aha=v1`。
- WebRTC：`POST /v1/realtime/calls?...&x_aha=v1`。
- 未传 `x_aha` 时服务端绝不发送 `x_aha.*`、`audio_harness.*` 或其他私有事件。
- 只支持精确值 `v1`。WebSocket 未升级前以 `4400` 拒绝；WebRTC 返回 `400 unsupported_x_aha_version`。
- 客户端应先按原始 JSON 的 `type` 分派 `x_aha.*`，再把其他事件交给 OpenAI SDK 类型解析。
- 新增可选字段不视为破坏性变更；客户端必须忽略未知字段。事件名、既有字段含义或覆盖规则变化需要新版本。

所有事件都带服务端唯一 `event_id`。带 `item_id` 的事件与同一轮 OpenAI speech/transcription item 对齐；毫秒时间均为会话输入音频的绝对时间。

## 事件总表

| 事件 | 产生条件 | 失败影响 |
| --- | --- | --- |
| `x_aha.vad.physical_speech_stopped` | 物理 VAD 停止，但语义 VAD 判定轮次未结束 | 观测事件，无失败变体 |
| `x_aha.input_audio_transcription.interim` | Workflow 有 interim 出口、自动 VAD、AHA 已协商 | 可跳过，不影响正式转写 |
| `x_aha.input_audio_transcription.interim_cleared` | 正式转写开始或预览被替代 | 客户端清空临时文本 |
| `x_aha.vector.search.completed/failed` | Workflow 有 vector_search 出口 | fail-open，不中断 ASR |
| `x_aha.diarization.local.delta/completed/failed` | Workflow 有本地 diarization 出口 | 失败时原始 VAD 音频直通 ASR |
| `x_aha.input_audio_transcription.speaker.delta/completed/failed` | ASR 消费 diarization segment | 单片段失败，不替代标准聚合转写 |
| `x_aha.diarization.cluster.completed/failed` | Workflow 有 speaker clustering 出口 | 保留上一成功 revision，后续重连重放 |

## 物理 VAD 进度

```json
{
  "type":"x_aha.vad.physical_speech_stopped",
  "event_id":"event_xxx",
  "item_id":"item_xxx",
  "audio_start_ms":1200,
  "audio_end_ms":2380,
  "turn_completed":false
}
```

`turn_completed` 在 v1 固定为 false。它表示用户可能短暂停顿，不能提交最终 UI turn、不能触发 Agent 回复；最终仍以原生 `input_audio_buffer.speech_stopped` 和 transcription completed 为准。

## 中间转写

`x_aha.input_audio_transcription.interim` 字段：

| 字段 | 语义 |
| --- | --- |
| `item_id/content_index` | 当前 OpenAI item；content index 固定 0 |
| `revision` | 同 item 内从 1 单调递增 |
| `transcript` | 当前累计快照，不是 delta |
| `audio_start_ms/audio_end_ms` | 当前快照覆盖范围 |

```json
{
  "type":"x_aha.input_audio_transcription.interim",
  "event_id":"event_xxx",
  "item_id":"item_xxx",
  "content_index":0,
  "revision":2,
  "transcript":"这是当前累计的临时文本",
  "audio_start_ms":120,
  "audio_end_ms":2120
}
```

客户端按 `(item_id, revision)` 覆盖，不拼接 transcript。正式 `conversation.item.input_audio_transcription.delta` 前发送：

```json
{
  "type":"x_aha.input_audio_transcription.interim_cleared",
  "event_id":"event_xxx",
  "item_id":"item_xxx",
  "reason":"primary_transcript_started"
}
```

`reason` 当前可见 `primary_transcript_started` 或 `superseded`。收到 cleared 立即删除预览。手动 commit 不运行 interim 分支；容量繁忙或预览 ASR 失败时可完全没有 interim，客户端不得等待它才显示正式结果。

## 人物向量检索

共同字段：`item_id`、`audio_start_ms/audio_end_ms`、`collection_name`、`vector_name`。成功事件：

```json
{
  "type":"x_aha.vector.search.completed",
  "event_id":"event_xxx",
  "item_id":"item_xxx",
  "audio_start_ms":1000,
  "audio_end_ms":4200,
  "collection_name":"voiceprints",
  "vector_name":"eres2netv2",
  "groups":[{
    "speaker_id":"speaker-001",
    "score":0.95,
    "observation":{"point_id":"point_xxx","payload":{"speaker_id":"speaker-001"}}
  }],
  "latency_ms":32.145
}
```

Workflow 的 `include_embedding=true` 时额外返回 `embedding:number[]`，可能很大。`groups` 按人分组，每人只带最高分代表观测。失败事件不带 groups/latency，改带：

```json
{"type":"x_aha.vector.search.failed","event_id":"event_xxx","item_id":"item_xxx","audio_start_ms":1000,"audio_end_ms":4200,"collection_name":"voiceprints","vector_name":"eres2netv2","error":{"code":"qdrant_unavailable","message":"人物搜索失败"}}
```

## 局部说话人分离

每次积累批次有稳定 `batch_id`。`delta` 表示该 gRPC 流当前快照，`completed` 表示批次最终完整结果；客户端可以用 completed 整体替代此前同 batch delta。

```json
{
  "type":"x_aha.diarization.local.completed",
  "event_id":"event_xxx",
  "batch_id":"batch_xxx",
  "batch_audio_start_ms":1000,
  "batch_audio_end_ms":65000,
  "speech_duration_ms":60000,
  "sequence":3,
  "segments":[
    {"segment_id":"dseg_a","audio_start_ms":1000,"audio_end_ms":5200,"speaker":1,"local_speaker_id":"batch_xxx:1"},
    {"segment_id":"dseg_b","audio_start_ms":7200,"audio_end_ms":11800,"speaker":2,"local_speaker_id":"batch_xxx:2"}
  ],
  "latency_ms":972.84
}
```

- `batch_audio_*` 是批次在会话中的跨度，`speech_duration_ms` 是去除 VAD 间隙后的有效音频。
- `speaker` 只在 batch 内有效；跨 batch 必须使用带命名空间的 `local_speaker_id`。
- `segment_id` 在会话及后续 cluster revision 中稳定。
- `sequence` 仅在引擎提供时出现，用于同 batch delta 排序。
- `.failed` 保留共同字段/当前 segments，并增加 `error.code/message`。失败不终止主 ASR。

## 分角色 ASR

```json
{
  "type":"x_aha.input_audio_transcription.speaker.completed",
  "event_id":"event_xxx",
  "item_id":"item_xxx",
  "item_source_id":"source_xxx",
  "segment_id":"dseg_a",
  "batch_id":"batch_xxx",
  "local_speaker_id":"batch_xxx:1",
  "speaker":1,
  "audio_start_ms":1000,
  "audio_end_ms":5200,
  "language":"zh-CN",
  "transcript":"大家好"
}
```

`.delta` 使用 `delta` 字段，`.completed` 使用 `transcript`，`.failed` 使用 `error.code/message`。`language` 可省略。该流按 diarization segment 输出；原生 OpenAI transcription 仍按来源 VAD turn 聚合，两者粒度不同，不应互相去重。

## 全局说话人聚类

聚类没有 delta。成功是全量快照：

```json
{
  "type":"x_aha.diarization.cluster.completed",
  "event_id":"event_xxx",
  "revision":4,
  "replaces_revision":3,
  "full_snapshot":true,
  "assignments":[
    {"segment_id":"dseg_a","local_speaker_id":"batch_xxx:1","speaker_id":0},
    {"segment_id":"dseg_b","local_speaker_id":"batch_yyy:1","speaker_id":0}
  ]
}
```

客户端只接受比当前 revision 新的 completed，并整体覆盖 assignments，绝不能按增量合并。`speaker_id=-1` 表示数据不足以形成稳定聚类。失败事件包含 `revision/replaces_revision/error`，不含 full snapshot；收到失败后保留上一成功快照。

引擎流失败后，节点会在下一批次重连并重放历史 embedding，因此后续成功 revision 仍可能重新分配所有历史 speaker ID。

## 客户端状态建议

```text
item_id -> { interim_revision, interim_text, final_transcript }
batch_id -> { latest_sequence, local_segments }
cluster -> { revision, assignments_by_segment_id }
vector item_id -> groups
```

不同分支并行执行，跨分支事件没有全局顺序保证；只能依赖各自的 ID、revision/sequence 和原生 item 生命周期。

