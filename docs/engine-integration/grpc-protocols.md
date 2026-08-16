# 下游 gRPC 推理协议

Proto 文件是结构定义唯一来源。本页规定 Audio Harness 实际调用的方法、包顺序和额外语义。

## ASR：`ux_speech.UxSpeech`

Proto：[`asr/ux_speech.proto`](../../src/audio_harness/protos/asr/ux_speech.proto)。调用 `StreamingRecognize(stream) returns (stream)`：

1. 首包 `streaming_config`，encoding=`LINEAR16`，真实 sample rate/language/punctuation/hotwords，`interim_results=true`。
2. 第二包（有音频时）发送完整 segment 的 `audio_content`，随后半关闭。
3. 服务可返回多个 response/result；Audio Harness逐项转换 transcript、is_final 和 metadata。

当前节点按 VAD segment 为每次 ASR 调用，所以上游 `start_sample/end_sample/turn_completed` 由 Workflow 保留，不依赖 ASR 返回时间。实现方应至少返回 transcript 和可靠 `is_final`；空流或永不 final 会阻塞正式 item 完成。

## VAD：`ux_vad.UxVoiceActivityDetector`

Proto：[`vad/ux_vad.proto`](../../src/audio_harness/protos/vad/ux_vad.proto)。

### Physical VAD

调用双向 `StreamingDetect`：首包 `config`，后续多包 `audio_content`。config 为 LINEAR16、实际 sample rate；可选 `postprocessing` 只在首包发送：

```text
processor = "firered.stream_vad"
version = 1
parameters = Struct{
  smooth_window_size, speech_threshold, pad_start_frame,
  min_speech_frame, max_speech_frame, min_silence_frame
}
```

未设置 postprocessing 时字段完全省略，引擎使用自身默认值。每个响应给 `processed_time` 和零个或多个 boundary；负数 duration 被解释为尚无该侧边界。boundary 时间相对该流开头，必须单调且不能越过 processed time。

### Semantic VAD

调用一元 `Detect`，request 为完整 segment PCM。只要任一 result 的 `end_time >= 0` 就表示当前轮次完成；没有有效 end 表示继续缓冲。语义 VAD 默认 `fail_open`，RPC 失败时 Workflow 允许原 segment 继续。

## Embedding 与聚类：`pb.Engine`

Proto：[`embedding/engine.proto`](../../src/audio_harness/protos/embedding/engine.proto)。

### `Feature`

Audio Harness 发送 `FeatRequest{vadAlg:"", sidAlg:<algorithm>, pcm:<PCM S16LE>}`。`FeatReply.feat` 必须为 little-endian float32 连续数组，字节长度精确等于 `expected_dimension * 4`，所有值有限。`normalize=true` 时 Harness 做 L2 归一化，并拒绝零范数。

`EngineInfo/Compare/Clustering` 当前不用于普通 embedding 节点；不要依赖 Harness 调用它们完成健康探测。

### `StreamingClustering`

1. 首包必须为 `config`：包含非空历史 vectors、algorithm 和 Struct parameters。
2. 引擎首个响应 `sequence=0`，`labels` 是全部历史向量的当前标签。
3. 后续 request 为 `batch`，sequence 从 1 连续递增且 vectors 非空。
4. 每个 response sequence 必须等于 request，labels 是全部历史向量的全量标签，不是新增批次的局部标签。

sequence 不匹配、提前 EOF 或 timeout 被视为引擎失败。Workflow 会保留上一 cluster snapshot，并在下一触发点重连、重放历史 vectors。

## Speaker Diarization：`ux_sd.UxSpeakerDiarization`

Proto：[`diarization/ux_speaker_diarization.proto`](../../src/audio_harness/protos/diarization/ux_speaker_diarization.proto)。调用一元请求/服务端流 `StreamingDetect(DetectRequest)`：

- request audio 必须为非空 PCM S16LE，config 为 LINEAR16 和真实 sample rate。
- 每个 response 是当前到达的一组 segment；start/end 是相对拼接批次的 Duration，speaker 为批次内无符号编号。
- Harness 会把去除静音后拼接时间映射回会话绝对时间。实现方不能假设输入 PCM 中保留了原始 VAD 间隙。
- segment 必须有合法顺序和范围；批次可产生多次流响应，对应 AHA local delta，正常 EOF 对应 completed。

## TTS：`qwen_tts.service.v1.TtsService`

Proto：[`tts/tts.proto`](../../src/audio_harness/protos/tts/tts.proto)。Agent 使用 `DuplexSynthesize(stream) returns (stream)`：

1. 首包 `config`，包含 voice、language 和 `decoder_chunk_size=0..1024`。
2. voice 二选一：非空 `preset_voice_id`；或 `ReferenceVoice`（非空偶数字节 PCM S16LE、正 sample rate、可空 ref_text）。
3. 后续每包只含非空 `text_chunk`，顺序与 Responses 文本流一致。
4. response `AudioChunk.pcm_s16le` 必须非空、偶数字节；sample rate 为正且整轮固定；`chunk_index` 必须从 0 连续递增。
5. 正常流至少返回一个 chunk；用户打断/response cancel 时 Harness 取消 RPC。

WebSocket 输出会重采样到 24 kHz 并编码到 `response.output_audio.delta`；WebRTC 输出重采样到 RTP track 所需的 48 kHz。引擎不需要感知传输类型。

## 通用失败要求

- 参数错误：`INVALID_ARGUMENT`。
- 模型未加载/配置缺失：`FAILED_PRECONDITION`。
- 暂时不可用：`UNAVAILABLE`。
- 超时：`DEADLINE_EXCEEDED`。
- 服务端取消后应停止产生 response；不要继续持有 GPU/流状态。

