# Realtime 典型事件时序

图中只列关键事件；错误、零个或多个 delta、工具调用等可插入其中。

## 自动 VAD 纯转写

```text
Client                                      Server
  | ----- WebSocket intent=transcription ----> |
  | <---------------- session.created --------- |
  | ----- session.update(model, server_vad) --> |
  | <---------------- session.updated ---------- |
  | ----- input_audio_buffer.append * --------> |
  | <------ input_audio_buffer.speech_started -- |
  | <------ input_audio_buffer.speech_stopped -- |
  | <------ input_audio_buffer.committed ------- |
  | <------ conversation.item.added/done ------- |
  | <------ transcription.delta * -------------- |
  | <------ transcription.completed ------------ |
```

协商 AHA v1 后，主物理 VAD 会即时产生带 `physical_segment_id` 的
`x_aha.vad.physical_speech_started/stopped`。同一语义轮次内的物理段共享
`vad_turn_id`；轮次完成时依次发送 `x_aha.vad.turn_completed` 和原生 speech stopped。

## 手动提交

```text
session.update(turn_detection=null)
input_audio_buffer.append *
input_audio_buffer.commit
  <- input_audio_buffer.committed
  <- conversation.item.added/done
  <- x_aha.vad.progress *
  <- transcription.delta *
  <- transcription.completed
  <- x_aha.vad.progress(final=true, full input duration)
  <- x_aha.diarization.cluster.completed(final=true, full snapshot)
```

空 buffer commit 返回 error，连接仍可使用。`input_audio_buffer.clear` 会丢弃未提交音频并返回 cleared。手动模式不发送 speech started/stopped，也不执行 AHA interim 分支。

最终 VAD 进度只表示主物理 VAD 已正常消费并排空完整 PCM，包含尾部静音。最终聚类快照要等重采样器、VAD、局部分离和聚类均完成后才发送；没有说话人时 assignments 为空。聚类或 Workflow 失败时最后一项改为 `x_aha.diarization.cluster.failed(final=true)`。

## AHA interim 到正式结果

```text
speech_started(item_A)
x_aha.input_audio_transcription.interim(item_A, revision=1, snapshot)
x_aha.input_audio_transcription.interim(item_A, revision=2, snapshot)
speech_stopped(item_A)
committed(item_A)
conversation item added/done(item_A)
x_aha.input_audio_transcription.interim_cleared(item_A)
transcription.delta(item_A) *
transcription.completed(item_A)
```

UI 维护两个区域：临时快照按 revision 覆盖；cleared 后只展示正式 delta/completed。

## Voice Agent 自动回复

```text
User audio -> speech/transcription completed(item_user)
  <- response.created(response_1)
  <- response.output_item.added(item_assistant)
  <- conversation.item.added(item_assistant)
  <- response.content_part.added
  <- response.output_audio_transcript.delta *
  <- response.output_audio.delta *          # WebSocket；WebRTC 音频走 track
  <- response.output_audio_transcript.done
  <- response.output_audio.done              # WebSocket 生命周期仍完成
  <- response.content_part.done
  <- conversation.item.done
  <- response.output_item.done
  <- response.done(response_1)
```

新 speech started 可打断 response，最终 `response.done.status=cancelled`。客户端显式 `response.cancel` 也会取消；WebRTC 可再发 `output_audio_buffer.clear` 清理待播音频。

## 工具调用

```text
response.created
response.output_item.added(function_call)
conversation.item.added(function_call)
response.function_call_arguments.done
conversation.item.done(function_call)
response.output_item.done(function_call)
  [服务工具自动执行，或客户端提交 function_call_output]
conversation.item.added/done(function_call_output)
  [在 max_tool_rounds 内继续 Responses]
response.done
```

MCP 需要审批时先发送 `mcp_approval_request` item，客户端以 `conversation.item.create` 携带 `mcp_approval_response`。拒绝、超时或工具失败会生成工具输出/response failure，不应由客户端无限重试。

## Diarization、聚类和人物检索

```text
VAD audio segment
  ├─ speaker_diarization
  │    ├─ x_aha.diarization.local.delta *
  │    ├─ x_aha.diarization.local.completed
  │    ├─ per-speaker ASR -> x_aha...speaker.delta/completed
  │    └─ speaker_clustering -> x_aha.diarization.cluster.completed(snapshot)
  ├─ primary ASR -> OpenAI transcription events
  └─ audio_embedding -> vector_search -> x_aha.vector.search.completed
```

三条分支并行，没有固定先后。主 ASR 的完成不代表人物检索/聚类一定完成；客户端按 item/batch/revision 分别维护状态，并允许 fail-open 分支缺失。

commit 的尾部排空是例外：主物理 VAD 的 `x_aha.vad.progress(final=true)` 一定先于最终聚类完成/失败快照；其他并行分支之间仍没有固定先后。
