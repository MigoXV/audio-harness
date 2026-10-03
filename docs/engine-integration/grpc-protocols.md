# 下游 gRPC 推理协议

Proto 文件是结构定义唯一来源。本页规定 Audio Harness 实际调用的方法、包顺序和额外语义。

## Denoise：`ux_denoise_proto.UxDenoise`

Proto：[`denoise/ux_denoise.proto`](../../src/audio_harness/protos/denoise/ux_denoise.proto)。调用双向流
`SynchronizedStreamingDenoise`：

1. 首包为 `DenoiseConfig`，输入输出均为 LINEAR16 且采样率相同。
2. 后续包发送非空、偶数字节的原始 PCM；响应可以跨请求边界重新分块。
3. 每个响应的 `audio_in` 与 `audio_out` 必须逐采样点对齐、字节数相同；所有响应拼接后的 `audio_in` 必须与完整请求原音一致。
4. Harness 可按节点权重混合两路音频供物理 VAD 检测，但 VAD 输出的下游片段只取 `audio_in` 原音。

服务提前结束、两路长度不一致或原音回显不一致均视为失败；节点默认从尚未确认的原音位置旁路。

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
- segment 起点必须在整个响应流中全局非递减，范围必须合法；相邻 segment 允许少量重叠。默认后处理会在同一语义轮次内跨 VAD 静音块合并相同 speaker；VAD 块边界不触发提交，首个有效的异 speaker segment、语义轮次边界或正常 EOF 才使待定片段稳定。稳定片段作为 AHA local delta 立即交给下游说话人 ASR，正常 EOF 再发送 completed。显式关闭节点后处理时，每个原始响应仍直接对应一个 local delta，正常 EOF 后再统一交给 ASR。

### 双向流分离：`DuplexStreamingDetect`

引擎资产 API 为 `ux_speaker_diarization_duplex`，与旧的一元请求路径显式区分。当前服务仅支持 Nemotron-3、16 kHz 单声道 LINEAR16。

1. 首包为 `DuplexStreamingConfig`，包含音频配置和 `input_buffer_latency_ms`；Harness 提供 320（默认）、640、1040 三档。
2. 后续请求只有非空、偶数字节的 `audio_content`，发送完整连续 PCM，包含静音、无 WAV 文件头。分包无需对齐模型推理块。
3. 响应包含 `results` 与 `process_time`，静音响应可以没有 results。后者是排他的确定水位，不是计算耗时；不能倒退或超过已上传的音频范围。
4. 每条结果含 RPC 内唯一 `segment_id`、固定 speaker `1..8` 及起止 Duration。边界的 `seconds=-1,nanos=0` 表示本次未更新；客户端按 ID 合并，不能覆盖已有边界。两边均为 `-1` 表示持续，非负终点表示永久闭合。
5. 闭合可以乱序到达。新增起点及闭合边界不得修订旧水位之前的判定。Harness 先合并边界，再按轮次的 `TimelineSpan` 映射；不把原始响应直接送入要求全局起点有序的旧后处理器。
6. 上传结束使用 half-close；客户端继续接收尾部响应。正常结束要求所有片段闭合、最终水位等于输入样本数。空输入由 Harness 直接完成，不发送只有配置的空业务 RPC。

工作流节点同时消费连续 `audio` 和 VAD／语义 `segments`。水位覆盖候选轮次后，裁剪该轮次内的确定片段，按起点及模型片段 ID 排序后执行原有单说话人覆盖后处理；开放长片段在该轮次内也已有确定范围。模型 ID 与对外分段 ID 分别维护。

节点同时产生原有局部分离事件及全量聚类快照，speaker ID 映射为 `model_speaker - 1`，不调用 Feature/StreamingClustering。取消清理当前 RPC；失败停止本次身份推断，排空完整音频输入，剩余轮次以未知身份继续 ASR，并发送聚类失败事件保留上一成功 revision，不透明重连。

`request_timeout_seconds` 默认可空；非空时是整个 RPC 的 deadline。待处理音频与时间轴历史默认 120 秒，模型累计片段和输出片段默认各最多 10,000；超限也走上述失败开放路径。健康探测只检查 channel ready，业务能力需要实际流式调用验证。

## TTS：`qwen_tts.service.v1.TtsService`

Proto：[`tts/tts.proto`](../../src/audio_harness/protos/tts/tts.proto)。Agent 使用 `DuplexSynthesize(stream) returns (stream)`：

1. 首包 `config`，包含 voice、language 和 `decoder_chunk_size=0..1024`。
2. voice 二选一：非空 `preset_voice_id`；或 `ReferenceVoice`（非空偶数字节 PCM S16LE、正 sample rate、可空 ref_text）。
3. 后续每包只含非空 `text_chunk`，顺序与 Responses 文本流一致。
4. response `AudioChunk.pcm_s16le` 必须非空、偶数字节；sample rate 为正且整轮固定；`chunk_index` 必须从 0 连续递增。
5. 正常流至少返回一个 chunk；用户打断/response cancel 时 Harness 取消 RPC。

WebSocket 输出会重采样到 24 kHz 并编码到 `response.output_audio.delta`；WebRTC 输出重采样到 RTP track 所需的 48 kHz。引擎不需要感知传输类型。

### 预置音色目录：`qwen_tts.service.v1.PresetVoiceService`

Proto：[`tts/preset_voice.proto`](../../src/audio_harness/protos/tts/preset_voice.proto)。调用一元 `ListPresetVoices`，返回服务模式和完整预置音色目录。每项包含稳定 `voice_id`、实际请求使用的 `name`、语言，以及 `supports_synthesize`、`supports_duplex` 和 `available_on_instance` 能力标记。

Audio Harness 的语音智能体只把同时满足 `available_on_instance=true` 与 `supports_duplex=true` 的项目作为自动候选；控制面仍会返回完整目录，且手工填写音色继续可用以兼容旧服务或临时音色。

## 通用失败要求

- 参数错误：`INVALID_ARGUMENT`。
- 模型未加载/配置缺失：`FAILED_PRECONDITION`。
- 暂时不可用：`UNAVAILABLE`。
- 超时：`DEADLINE_EXCEEDED`。
- 服务端取消后应停止产生 response；不要继续持有 GPU/流状态。
