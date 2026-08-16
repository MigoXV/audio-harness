# gRPC 流式 ASR 数据面

外部 gRPC 数据面实现 `ux_speech.UxSpeech/StreamingRecognize` 双向流。原始协议见 [`ux_speech.proto`](../../src/audio_harness/protos/asr/ux_speech.proto)，可运行客户端见 [`examples/grpc_client.py`](../../examples/grpc_client.py)。

## 前置条件

- `PUT /api/v1/settings/grpc-workflow` 已绑定一个 active Audio Workflow。
- Workflow 提供稳定 `primary_transcript`、`speech_started`、`speech_stopped` 出口并能准备全部引擎。
- 客户端连接默认 `localhost:50051`；当前 server channel 未配置 TLS/认证，生产环境应使用受信网络或 sidecar/proxy。

## 请求流

首包必须且只能携带 `streaming_config`：

```text
StreamingRecognizeRequest {
  streaming_config: {
    config: {
      encoding: LINEAR16
      sample_rate_hertz: 16000
      language_code: "zh-CN"
      enable_automatic_punctuation: true
      hotwords: ["Audio Harness"]
      hotword_bias: 5.0
    }
    interim_results: true
  }
}
```

- encoding 只接受未指定或 `LINEAR16`；实际负载必须为裸 PCM S16LE。
- `sample_rate_hertz=0` 回退 16000；有效值必须大于 0。
- language 为空或 `auto` 时回退 Workflow Settings 的 `default_language_code`。
- `interim_results` 字段由 Proto 保留；当前服务会按 Workflow/ASR 实际结果返回临时与最终结果。

之后每包只能携带 `audio_content`。单包不能超过 `max_message_bytes`；服务按 `min_chunk_bytes` 聚合投递。客户端半关闭请求流表示输入结束，尾部偶数字节会提交，奇数字节返回 `INVALID_ARGUMENT`。

## 响应流

每个内部 ASR result 转换为一个 `StreamingRecognizeResponse.results[0]`：

| 字段 | 当前语义 |
| --- | --- |
| `alternative.transcript` | 当前文本 |
| `alternative.words[0]` | 当前实现将整个 transcript 作为一个 WordInfo，时间为 segment 绝对起止 |
| `alternative.metadata` | 下游 ASR metadata 原样字符串映射 |
| `alternative.language.code` | 有语言时填充 |
| `alternative.turn_completed` | 语义轮次是否完成 |
| `is_final` | 当前结果是否最终 |

客户端必须遍历 repeated `results`，不能长期假设只有一项；`speaker`、`speaker_changed` 等字段存在于共享 Proto，但 Audio Harness 主 gRPC 数据面当前不填充它们。

## 时序

```text
Client                                  Audio Harness
  | -- streaming_config ------------------> |
  | -- audio_content ---------------------> |
  | -- audio_content ---------------------> | -> Workflow VAD/ASR
  | <---- response(is_final=false) -------- |
  | <---- response(is_final=true) --------- |
  | -- half-close ------------------------> |
  | <---- remaining results / stream EOF -- |
```

客户端取消会传播到 Workflow 并释放容量。服务关闭或引擎异常结束流，客户端应根据 gRPC Status 决定是否重试，不能仅凭 EOF 判定成功。

## Status 映射

| gRPC Status | 场景 |
| --- | --- |
| `INVALID_ARGUMENT` | 空流、首包不是 config、后续重复 config、编码/采样率/消息大小/PCM 对齐错误 |
| `RESOURCE_EXHAUSTED` | gRPC session 或推理容量已满 |
| `DEADLINE_EXCEEDED` | 下游引擎 deadline |
| `UNAVAILABLE` | 下游引擎连接/RPC 不可用 |
| `FAILED_PRECONDITION` | 默认 Workflow 未激活或 Engine 配置无效 |
| `INTERNAL` | 未分类服务错误 |

建议只对 `UNAVAILABLE/DEADLINE_EXCEEDED/RESOURCE_EXHAUSTED` 做带抖动退避的有限重试；`INVALID_ARGUMENT/FAILED_PRECONDITION` 需要修复配置。

