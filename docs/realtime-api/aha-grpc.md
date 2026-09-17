# aha-grpc v1

aha-grpc 是当前 OpenAI Realtime 兼容协议和 AHA v1 的 gRPC 双向流接口。WS 与 gRPC 使用同一个 `RealtimeService` 和 `RealtimeConnection`，共享模型解析、Session 更新、音频缓冲与重采样、Workflow、Conversation、Voice Agent、工具及后台交互。

## 启动和连接

`poetry run audio-harness serve` 强制启动 HTTP/WS/WebRTC 与 aha-grpc；没有 gRPC 启停开关。

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `--grpc-host` / `AHA_GRPC_HOST` | `0.0.0.0` | TLS 监听地址 |
| `--grpc-port` / `AHA_GRPC_PORT` | `50051` | TLS 端口；0 自动分配 |
| `--tls-cert-file` / `HTTP_TLS_CERT_FILE` | `model-bin/realtime-tls/cert.pem` | 与 HTTP 共用证书 |
| `--tls-key-file` / `HTTP_TLS_KEY_FILE` | `model-bin/realtime-tls/key.pem` | 与 HTTP 共用私钥 |
| `--max-message-bytes` / `MAX_MESSAGE_BYTES` | `4194304` | 包含 Proto 开销的消息大小上限 |

证书错误或端口绑定失败会使整体启动失败。客户端使用 TLS 并验证证书及主机名，不提供明文回退。身份认证继续由部署网关负责。

Proto 位于 [`aha_realtime.proto`](../../src/audio_harness/protos/aha_grpc/v1/aha_realtime.proto)：

```proto
package aha.grpc.v1;
service AhaRealtime {
  rpc Connect(stream ClientEvent) returns (stream ServerEvent);
}
```

RPC 路径为 `/aha.grpc.v1.AhaRealtime/Connect`。每个 RPC 是独立会话，初始事件为 `session.created`。

| Metadata | 语义 |
| --- | --- |
| `intent=transcription` | 动态转写，首次有效 session.update 指定已发布工作流 Slug |
| `model=<slug>` | 已发布语音智能体 Slug |
| `diagnostics=true/false` | 可选，默认 false |
| `x_aha=v1` | 可省略，默认启用 v1；其他值不支持 |

`intent` 和 `model` 必须且只能指定一个。重复的业务 metadata 被拒绝。这里的 model 是当前资源 Slug，不是已移除的独立 Route 配置。

## 事件、字段和音频

协议覆盖当前事件联合类型：14 类客户端事件、77 类服务端事件，包含 3 类 AHA 客户端事件、31 类 AHA 服务端事件。兼容基线为项目锁定的 OpenAI SDK 2.50.0 及本地 AHA 模型；定义某个事件不代表运行时增加了原服务没有的业务能力。实际能力沿用 [OpenAI 兼容子集](openai-realtime.md) 和 [AHA 扩展](aha-v1.md)，另包含代码中已有的 `x_aha.agent_backend.*` 审批、问答及任务事件。

- Envelope 使用 `oneof event`；字段名是点分事件名用下划线连接，如 `input_audio_buffer_append`。具体消息中的 `type` 保留原始点分名称，且必须与 oneof 分支匹配。
- 业务对象为完整强类型 Proto。字段名及逻辑结构保持原样；辅助 Value/List/Map 消息用于表达联合类型和字段存在性。
- 非可空标量使用 `optional`，缺失不会自动变成 0、false 或空字符串。可空字段的包装消息使用 `oneof kind`，`null` 分支表示显式 null，整个字段缺失表示未传。
- 集合包装中的 `values` 承载数组或映射：包装不存在是未传，包装存在且 values 为空是显式空集合。`tools:[]` 和 `turn_detection:null` 可正常清除配置。
- 原本允许任意 JSON 的字段使用类型化 `JsonValue`。其整数分支是 int64，不经过浮点转换；开放对象额外字段通过 `additional_properties` 保留，禁止覆盖已声明字段。
- `input_audio_buffer.append.audio`、`response.output_audio.delta.delta` 和嵌套 content 的 audio 都是原始音频 bytes，不包含 WAV 头，也不做 Base64。文本 delta 仍为字符串。
- 输入支持 16/24 kHz PCM S16LE mono，输出事件音频为 24 kHz。内部统一处理 PCM，JSON/Base64 编解码仅在 WS/WebRTC 事件适配器执行。
- gRPC 与 WS 一样不具备服务端播放缓冲，因此 `output_audio_buffer.clear` 仍返回现有能力错误；客户端使用 response.cancel 并清理自己的播放队列。

## Python 使用

跨语言客户端可直接使用 Proto 生成 stub。Python 也提供无 JSON 序列化的字典与 Proto 转换器，以及轻量异步客户端：

```python
import grpc
from pathlib import Path
from audio_harness.serving.aha_grpc.client import AhaGrpcClient

credentials = grpc.ssl_channel_credentials(Path("cert.pem").read_bytes())
async with grpc.aio.secure_channel("localhost:50051", credentials) as channel:
    client = AhaGrpcClient(channel)
    async with client.connect(intent="transcription", timeout=300) as connection:
        # events() 必须与 send() 并行消费；每个连接只使用一个接收协程。
        await connection.send({
            "type": "session.update",
            "session": {"type": "transcription", "audio": {"input": {
                "transcription": {"model": "meeting-transcribe"},
                "format": {"type": "audio/pcm", "rate": 16000},
                "turn_detection": None,
            }}},
        })
```

以上是配置片段；完整并行收发、WAV 转写和 Agent 示例见 [Python 示例](../../examples/aha_grpc_python_demo/README.md)。`send()` 也接受直接构造的 `ClientEvent`：

```python
from audio_harness.protos.aha_grpc.v1 import aha_realtime_pb2 as pb

message = pb.ClientEvent()
message.input_audio_buffer_append.type = "input_audio_buffer.append"
message.input_audio_buffer_append.audio = b"\x00\x00"
await connection.send(message)
```

## 提交、收尾与错误

客户端正常 `done_writing()` / `finish_input()` 时，服务端刷新重采样尾块并提交尚未提交的音频，排空最终转写、AHA 最终事件和已启动的 Agent 响应后结束 RPC。已经提交的音频不会重复提交。输入结束后仍需读取输出，直至 RPC EOF。

若任务需要新的客户端工具结果、审批或问答，半关闭后会以 `client_input_closed` 收尾，不能继续等待不存在的输入。需要交互的客户端应保持请求流开启。

取消、deadline、断连不会隐式提交剩余音频。服务停止后会等待连接清理完成，再关闭共享推理资源。输出队列复用 Workflow 的 `result_queue_capacity`，慢消费者通过背压限制内存使用。gRPC 和 WS 共用 Realtime 会话容量及下游推理并发限制。

| 情况 | 结果 |
| --- | --- |
| 连接参数非法 | INVALID_ARGUMENT |
| 模型不存在 | NOT_FOUND |
| 模型尚不可用 | FAILED_PRECONDITION |
| 容量或消息上限 | RESOURCE_EXHAUSTED |
| RPC deadline / 取消 | DEADLINE_EXCEEDED / CANCELLED |
| 无法继续的内部失败 | INTERNAL |
| 已连接后的可恢复业务错误 | 原有 error 事件，连接继续可用 |

Proto 字段校验失败、非法事件分支、奇数字节 PCM 等业务输入错误保留原错误关联信息。跨并行工作流分支不增加全局时序承诺；继续使用 item ID、revision 和 sequence 关联事件。

## 协议维护

```bash
# 生成完整 Proto、字段编号表、codec 元数据及 Python stub
bash scripts/run_grpcio_tools.sh

# 校验当前模型与签入协议完全一致，不写文件
poetry run python scripts/generate_aha_grpc.py --check

poetry run pytest -q tests/test_aha_grpc.py
```

`field_numbers.json` 是追加式编号表，不删除或复用已有编号；已移除字段在 Proto 中保留 reserved。协议及生成文件应一起提交。运行服务无需安装编译器；生成工具按项目约定使用与 grpcio 相同版本的 grpcio-tools。
