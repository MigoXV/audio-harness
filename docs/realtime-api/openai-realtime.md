# OpenAI Realtime 兼容子集

本页是 Audio Harness 的实际兼容契约，不是 OpenAI Realtime 完整 API 的镜像。所有非 `x_aha.*` 服务端事件在发送前都通过当前 `openai` Python SDK 的 `RealtimeServerEvent` 联合类型校验。

## 能力矩阵

| 能力 | OpenAI 定义 | Audio Harness |
| --- | --- | --- |
| WebSocket、WebRTC | 是 | 是 |
| transcription session | 是 | 是 |
| realtime conversation/response | 是 | 仅 Voice Agent Route |
| server VAD、manual commit | 是 | 是；语义边界由发布的 Workflow 决定 |
| function calling | 是 | 是；服务工具和 MCP 工具 |
| conversation item CRUD | 是 | 仅 Voice Agent |
| SIP、webhook、server-side call control | 是 | 不支持 |
| image input、原生 OpenAI hosted tools | 上游可能支持 | 当前不支持/不承诺 |
| AHA diarization/vector/interim | 否 | 仅显式 `x_aha=v1` |

## 支持的客户端事件

| 事件 | 转写 | Agent | 约束 |
| --- | --- | --- | --- |
| `session.update` | 是 | 是 | 只更新已实现字段；非法更新不污染当前配置 |
| `input_audio_buffer.append` | WebSocket | WebSocket | WebRTC 必须使用 RTP 音轨，DataChannel append 返回 `unsupported_audio_event` |
| `input_audio_buffer.clear` | 是 | 是 | 清空输入和重采样器，解锁输入格式/model 修改 |
| `input_audio_buffer.commit` | 是 | 是 | 空 buffer 返回 `input_audio_buffer_commit_empty` |
| `response.create` | 否 | 是 | 可覆盖本轮 response 参数 |
| `response.cancel` | 否 | 是 | 可带活动 `response_id` |
| `conversation.item.create` | 否 | 是 | 支持 message、function output、MCP approval response 等 SDK item |
| `conversation.item.retrieve/delete/truncate` | 否 | 是 | truncate 仅 assistant audio content，`content_index=0` |
| `output_audio_buffer.clear` | 否 | 仅 WebRTC Agent | 清空尚未播放输出并返回 cleared |

其他能被 SDK 解析但未实现的客户端事件返回 `unsupported_event`。

## Session 配置

### 动态转写

`intent=transcription` 连接初始 session 没有 transcription model。第一次有效 `session.update` 必须满足：

- `session.type="transcription"`。
- `audio.input.transcription.model` 非空，且能解析为 public、已激活 Audio Workflow Route。
- 输入格式只支持 `{"type":"audio/pcm","rate":24000}`。
- language 读取 `transcription.language`；若为空，兼容取 `languages[0]`，再回退 Workflow Settings 默认语言。
- `turn_detection` 支持 `server_vad` 或 `null`。服务会将已发布 Workflow 的 physical VAD pre-roll 反映为 prefix padding，并对纯转写设置 `create_response=false`。

发送音频后，修改转写 model/输入契约会得到 `session_update_after_audio`。先 clear，或在手动模式 commit，再更新。切换 model 会关闭旧 session 并建立新 Workflow 快照。

### Voice Agent

Agent session 支持 instructions、tools、voice、输入/输出音频格式和 turn detection 的当前实现字段。model 由 URL Route 固定，不能通过 session.update 切换。首次输出音频后 voice 被锁定；响应参数只对本轮有效。

## 音频缓冲与 VAD

WebSocket `append.audio` 是 Base64 PCM S16LE 24 kHz mono。服务校验 Base64、偶数字节和 message 大小，并在线重采样为 16 kHz、按 `min_chunk_bytes` 投递 Workflow。

自动 VAD 时典型服务事件：

1. `input_audio_buffer.speech_started`
2. `input_audio_buffer.speech_stopped`
3. `input_audio_buffer.committed`
4. `conversation.item.added`
5. `conversation.item.done`
6. `conversation.item.input_audio_transcription.delta`
7. `conversation.item.input_audio_transcription.completed` 或 `.failed`

手动模式不发送 started/stopped。commit 将当前全部音频作为一个 item；clear 返回 `input_audio_buffer.cleared`。

## 服务端事件

| 分类 | 已实现事件 |
| --- | --- |
| Session | `session.created`、`session.updated` |
| Input audio | `input_audio_buffer.cleared/committed/speech_started/speech_stopped` |
| Transcription | `conversation.item.input_audio_transcription.delta/completed/failed` |
| Conversation | `conversation.item.added/done/retrieved/deleted/truncated` |
| Response | `response.created/done` |
| Output item | `response.output_item.added/done`、`response.content_part.added/done` |
| Text/audio | `response.output_text.delta/done`、`response.output_audio.delta/done`、`response.output_audio_transcript.delta/done` |
| Function | `response.function_call_arguments.done`；function call/output 也通过 item 事件呈现 |
| Audio buffer | `output_audio_buffer.cleared` |
| Error | `error` |

`response.done` 是 response 的最终状态快照，可能为 completed、cancelled 或 failed。消费端不能仅以 audio/text done 判断整轮完成。

## Conversation CRUD

- `conversation.item.create` 可用 `previous_item_id` 插入；不存在的前项、重复 ID、非法角色/内容返回带 `param` 的 error。
- retrieve 返回 `conversation.item.retrieved`；delete 返回 `.deleted` 并从后续 Responses history 移除。
- truncate 同步“实际已经播放的 assistant audio”与服务端上下文，并删除未听到部分的 transcript 语义。
- 历史投影受 Agent `history_max_items` 限制；CRUD 操作与工具输出都会更新投影。

## Response、工具与打断

server VAD 检测新一轮语音时，若 Agent `interrupt_response=true`，活动 response 以 `turn_detected` 取消。客户端 `response.cancel` 使用 `client_cancelled`。WebRTC 还可发送 `output_audio_buffer.clear` 清理本地待播输出；WebSocket 语音输出通过 Base64 `response.output_audio.delta` 传输。

工具调用顺序为 output item added → arguments done → item done → output item done。服务工具可自动执行；MCP 工具根据 approval policy 产生 approval request item，客户端用 `conversation.item.create` 提交 approval response。工具轮数、并行数和超时由 Agent Definition 限制。

## 错误与恢复

```json
{
  "type":"error",
  "event_id":"event_xxx",
  "error":{
    "type":"invalid_request_error",
    "code":"invalid_event",
    "message":"Realtime 事件格式不合法",
    "event_id":"client-event-id",
    "param":"type"
  }
}
```

大多数事件错误可恢复，服务不会主动断开。客户端应修正事件后继续；Workflow、Responses 或 TTS 失败可能同时产生细分 failed/done 事件和通用 error。关闭连接会取消任务、结束 Workflow、释放 Engine/会话容量。

