# 下游推理引擎接入

本目录面向实现或部署 Audio Harness 下游引擎的同事。Engine Asset 通过 `protocol + api` 选择适配器，`kind` 主要用于管理和筛选。Audio Harness 只作为这些 gRPC 服务的客户端，不对外监听 gRPC 端口。

## 支持矩阵

| `api` | `protocol` | 实现契约 | 主要调用方 |
| --- | --- | --- | --- |
| `ux_denoise` | `grpc` | `ux_denoise_proto.UxDenoise` | Streaming Denoise 节点 |
| `ux_speech` | `grpc` | `ux_speech.UxSpeech` | ASR 节点 |
| `ux_vad` | `grpc` | `ux_vad.UxVoiceActivityDetector` | Physical/Semantic VAD |
| `engine_feature` | `grpc` | `pb.Engine` 的 Feature/StreamingClustering | Embedding/Clustering 节点 |
| `ux_speaker_diarization` | `grpc` | `ux_sd.UxSpeakerDiarization` | Diarization 节点 |
| `ux_speaker_diarization_duplex` | `grpc` | `ux_sd.UxSpeakerDiarization/DuplexStreamingDetect`，Nemotron-3、16 kHz | 流式分离合并节点 |
| `dummy_diarize` | `dummy` | 内置：整批标记为 `speaker=0` | Diarization 节点 |
| `qwen_tts_duplex` | `grpc` | `qwen_tts.service.v1.TtsService` | Voice Agent TTS |
| `responses` | `https` | OpenAI-compatible Models + Responses | Voice Agent LLM |

其他组合在 inferencer factory 阶段返回“不支持的引擎协议/API”。

## 地址、连接与超时

- gRPC `target` 使用 `host:port`，当前创建 insecure channel，并设置 send/receive `max_message_bytes`。
- `connect_timeout_seconds` 只约束 channel ready/模型列表连接，必须大于 0。
- `request_timeout_seconds` 可空；ASR/VAD/Diarization/Embedding 使用它作为 RPC deadline。Duplex TTS 整轮不设置普通 request deadline，取消由上层 response 生命周期传播。
- Responses target 使用 HTTP(S) base URL；详见 [Responses](responses.md)。
- `dummy_diarize` 固定使用 `dummy://diarize`，不建立网络连接。
- gRPC 错误 `DEADLINE_EXCEEDED` 映射为 Engine deadline，其他 AioRpcError 映射为 unavailable。

## 探测

gRPC 引擎 `validate` 当前等待 channel ready，不主动发送业务 RPC。实现方应提供可建立 HTTP/2 的 endpoint，但不能把探测成功理解为模型加载完成。Responses 探测调用 `/models`。

## 凭据

gRPC 适配器当前不注入 metadata 凭据，应由 mTLS sidecar/网络层保护。Responses API Key 解析优先级：Engine `api_key` → `credential_env` 指定的进程环境变量；都缺失时配置无效。

## 实现检查清单

1. 使用仓库中的原始 Proto 生成服务端 stub，不依据本文手写 message。
2. 严格实现首包/后续包顺序、PCM 格式、采样率、byte order 和 response sequence。
3. 流取消后立即停止计算并释放模型资源。
4. 对非法输入返回合适的 gRPC Status，不要以成功空响应吞错。
5. 先通过 Engine validate，再用对应 Workflow 节点完成真实 RPC 联调。

详细线协议见 [gRPC 推理协议](grpc-protocols.md)。
