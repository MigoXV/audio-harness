# AHA Realtime 扩展协议 v1

AHA v1 为 OpenAI Realtime 事件流追加 Audio Harness 特有的中间转写、语义 VAD、人物检索和说话人事件。它不替换、删除或改写任何原生事件。

## 协商与版本

- WebSocket：`/v1/realtime?...&x_aha=v1`。
- WebRTC：`POST /v1/realtime/calls?...&x_aha=v1`。
- 未传 `x_aha` 时服务端绝不发送 `x_aha.*`、`audio_harness.*` 或其他私有事件。
- 只支持精确值 `v1`。WebSocket 未升级前以 `4400` 拒绝；WebRTC 返回 `400 unsupported_x_aha_version`。
- 服务端使用项目内建的 `AhaRealtimeServerEvent` 联合类型校验标准事件与全部 `x_aha.*` 事件；客户端若只使用官方 OpenAI SDK，仍应先按原始 JSON 的 `type` 分派私有事件。
- 新增可选字段不视为破坏性变更；客户端必须忽略未知字段。本项目当前直接演进 v1，严格校验客户端必须与服务端同步升级。

所有事件都带服务端唯一 `event_id`。带 `item_id` 的事件与同一轮 OpenAI speech/transcription item 对齐；除 `x_aha.vad.progress.processed_audio_ms` 外，毫秒时间均为会话输入音频的绝对时间。

## 事件总表

| 事件 | 产生条件 | 失败影响 |
| --- | --- | --- |
| `x_aha.vad.progress` | 主物理 VAD 正在处理或已正常排空 | 观测事件，无失败变体 |
| `x_aha.vad.physical_speech_started/stopped` | 主物理 VAD 检出即时边界 | 观测事件，无失败变体 |
| `x_aha.vad.turn_completed` | 工作流公共语义轮次完成 | 关联物理段与标准 item |
| `x_aha.input_audio_transcription.interim` | Workflow 有 interim 出口、自动 VAD、AHA 已协商 | 可跳过，不影响正式转写 |
| `x_aha.input_audio_transcription.interim_cleared` | 正式转写开始或预览被替代 | 客户端清空临时文本 |
| `x_aha.vector.search.completed/failed` | Workflow 有 vector_search 出口 | fail-open，不中断 ASR |
| `x_aha.speaker_filter.matched/rejected/failed` | Workflow 有 speaker_filter 出口 | matched 才进入 ASR；其余静默拒绝 |
| `x_aha.diarization.local.delta/completed/failed` | Workflow 有本地 diarization 出口 | 失败时原始 VAD 音频直通 ASR |
| `x_aha.input_audio_transcription.speaker.delta/completed/failed` | ASR 消费 diarization segment | 单片段失败，不替代标准聚合转写 |
| `x_aha.diarization.cluster.completed/failed` | Workflow 有 speaker clustering 出口 | 保留上一成功 revision，后续重连重放 |
| `x_aha.agent_backend.session/turn.*` | Voice Agent 已绑定并实际调用 Agent Backend | 展示远端 session 与任务状态 |
| `x_aha.agent_backend.text.delta/tool.*` | DSH 输出文本进度或工具事件 | 观测事件，字段有长度上限 |
| `x_aha.agent_backend.approval.*` | DSH 请求或完成审批 | 超时默认拒绝，界面/语音先到者生效 |
| `x_aha.agent_backend.question.*` | DSH 请求或完成结构化提问 | 超时按 cancelled 回传 DSH |
| `x_aha.agent_backend.failed/cancelled` | 任务失败、超时或显式取消 | 只结束后台任务，不关闭 Realtime 连接 |

## Agent Backend 事件与控制

Agent Backend 事件只在 Voice Agent 绑定 backend 且客户端协商 AHA v1 后发送。服务不会
透传 DSH reasoning；文本 delta、工具参数、工具结果和最终文本都有服务端长度上限。
典型执行顺序为 `session` → `turn.started` → 零到多个 `text.delta/tool.*` →
`turn.completed`，之后 Responses 通过标准事件输出最终总结与 TTS。

审批请求包含 `request_id`、`approval_id`、`tool_name` 和可选 `reason`。客户端回答：

```json
{"type":"x_aha.agent_backend.approval.respond","request_id":"req_xxx","approval_id":"approval_xxx","approved":true}
```

结构化问题包含 `questions`，每项有 `id/question/options/multiSelect`。回答中的
`selected` 是选项 label 数组，`custom` 可以为空：

```json
{"type":"x_aha.agent_backend.question.respond","request_id":"req_xxx","answers":[{"id":"scope","selected":["当前目录"],"custom":null}]}
```

界面与下一句语音通过同一原子 pending 状态竞争，先到者生效；迟到回答会收到
`.resolved` 且 `resolution=already_processed`。审批超时返回 rejected，问题超时返回
cancelled。显式取消使用 `{"type":"x_aha.agent_backend.cancel"}`；标准
`response.cancel` 只取消当前 Responses/TTS 输出，不会隐式取消 DSH。

## 物理 VAD 状态

Harness 从稳定 `speech_started` 出口自动确定唯一的主物理 VAD，并根据下游单调采样游标生成进度；不会透传 VAD 引擎的原始事件、频率或额外字段。普通事件按物理 VAD 节点的 `progress_interval_seconds` 使用墙钟限频，默认最多 5 Hz；设为 `0` 时关闭。预览 VAD 不会重复报告。

```json
{
  "type":"x_aha.vad.progress",
  "event_id":"event_xxx",
  "processed_audio_ms":82400
}
```

正常排空时绕过限频并且只发送一次终值：

```json
{
  "type":"x_aha.vad.progress",
  "event_id":"event_xxx",
  "processed_audio_ms":187765,
  "final":true
}
```

`processed_audio_ms` 是当前一次 commit 实际消费的完整 PCM 时长，包含尾部静音，不是连接累计值。普通事件省略 `final`，等同于 false。clear、异常取消、未协商 AHA v1 或节点配置为 `0` 时不会发送正常终值。

主物理 VAD 的开始和结束边界会立即成对发送，不等待语义 VAD：

```json
{
  "type":"x_aha.vad.physical_speech_started",
  "event_id":"event_xxx",
  "vad_turn_id":"vturn_xxx",
  "physical_segment_id":"pseg_xxx",
  "audio_start_ms":1200
}
```

```json
{
  "type":"x_aha.vad.physical_speech_stopped",
  "event_id":"event_xxx",
  "vad_turn_id":"vturn_xxx",
  "physical_segment_id":"pseg_xxx",
  "item_id":"item_xxx",
  "audio_start_ms":1200,
  "audio_end_ms":2380
}
```

同一个物理段的 started/stopped 共享 `physical_segment_id`；同一工作流轮次内的多个物理段共享 `vad_turn_id`。`item_id` 在 OpenAI item 尚未创建时省略。

工作流公共 `speech_stopped` 到达时，先发送聚合关系，再发送原生 stopped：

```json
{
  "type":"x_aha.vad.turn_completed",
  "event_id":"event_xxx",
  "vad_turn_id":"vturn_xxx",
  "item_id":"item_xxx",
  "audio_start_ms":1200,
  "audio_end_ms":8920,
  "physical_segment_ids":["pseg_a","pseg_b"]
}
```

`audio_start_ms/audio_end_ms` 是语义轮次首尾范围；真正送入下游的有效物理区间由 `physical_segment_ids` 对应的 stopped 事件给出。客户端不得把两个物理段之间的空洞解释为持续语音。

连续长语音达到物理 VAD 最大分段限制后，Harness 会在切点生成上一物理段的
stopped 和下一物理段的 started；两段使用不同 `physical_segment_id`，后续音频进入
新的 `vad_turn_id` 和标准 Realtime item。这样即使底层引擎保持持续 speech 状态，
每个对外物理段仍具有完整且无歧义的边界。

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

共同字段：`audio_start_ms/audio_end_ms`、`collection_name`、`vector_name`。若检索时已经创建 OpenAI turn，事件还会携带 `item_id`；声纹过滤工作流可能先完成检索、后决定是否创建 turn，此时 `item_id` 省略，但检索事件仍会立即发送。成功事件：

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

## 说话人过滤

过滤判定按音频区间关联，不创建独立 conversation item。匹配事件包含
`speaker_id` 和 `score`；拒绝事件的 `reason` 为 `no_match`、
`speaker_not_allowed` 或 `score_too_low`；失败事件使用标准 `error.code/message`。
事件不会返回 embedding、完整候选列表或 Qdrant payload。

## 局部说话人分离

每次积累批次有稳定 `batch_id`。`delta` 表示下游说话人分离 gRPC 流的当前快照，`completed` 表示批次最终完整结果；客户端可以用 completed 整体替代此前同 batch delta。

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

客户端只接受比当前 revision 新的 completed，并整体覆盖 assignments，绝不能按增量合并。`speaker_id>=0` 表示已经归档到全局聚类；`speaker_id<0` 表示暂未成功归档，不同负数仍代表不同的临时说话人。同一 `local_speaker_id` 在会话内复用同一个负数，后续数据足够时可在新 revision 中转为非负 ID。失败事件包含 `revision/replaces_revision/error`，不含 full snapshot；收到失败后保留上一成功快照。

commit 完成全部 Workflow 输出后，会再发送一个更高 revision 的最终全量快照。即使没有说话人，`assignments` 也会是空数组；其 `processed_audio_ms` 与最终 VAD 进度相同并包含尾部静音。

```json
{
  "type":"x_aha.diarization.cluster.completed",
  "event_id":"event_xxx",
  "revision":6,
  "replaces_revision":5,
  "full_snapshot":true,
  "final":true,
  "processed_audio_ms":187765,
  "assignments":[]
}
```

如果最近一次聚类或 Workflow 失败，终止事件为 `x_aha.diarization.cluster.failed`，带 `final:true`、`processed_audio_ms` 和 `error`，不会伪造成功快照。最终 VAD 进度先于最终聚类快照产生；其他并行分支仍不承诺全局事件顺序。

引擎流失败后，节点会在下一批次重连并重放历史 embedding，因此后续成功 revision 仍可能重新分配所有历史 speaker ID。

## 客户端状态建议

```text
item_id -> { interim_revision, interim_text, final_transcript }
batch_id -> { latest_sequence, local_segments }
cluster -> { revision, assignments_by_segment_id }
vector item_id -> groups
```

不同分支并行执行，跨分支事件没有全局顺序保证；只能依赖各自的 ID、revision/sequence 和原生 item 生命周期。
