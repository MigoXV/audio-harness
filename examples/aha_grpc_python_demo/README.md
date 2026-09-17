# aha-grpc Python 示例

使用项目的 Poetry 环境运行。服务端先发布工作流或语音智能体，再启动 `poetry run audio-harness serve`；aha-grpc 随服务强制启动，默认端口为 50051，使用现有 HTTP TLS 证书。

## 转写 WAV

```bash
poetry run python examples/aha_grpc_python_demo/main.py \
  --target localhost:50051 \
  --ca-cert model-bin/realtime-tls/cert.pem \
  --model meeting-transcribe \
  --audio input.wav
```

输入必须为单声道 PCM S16LE WAV，采样率为 16 kHz 或 24 kHz。示例读取 WAV 帧后发送原始 PCM bytes，发送和接收并行执行。`finish_input()` 半关闭请求流，服务端自动提交尾部音频，客户端继续读取最终事件。

## 语音智能体

```bash
poetry run python examples/aha_grpc_python_demo/main.py \
  --mode agent --model voice-agent \
  --text "你好，请介绍一下你自己。" \
  --output response.wav
```

响应音频保存为 24 kHz 单声道 WAV。此示例在发送文本后即结束输入，适合无需客户端工具或审批的单轮响应；需要交互时保持请求流开启，在收到工具、审批或问答事件后继续调用 `send()`。

`--ca-cert` 应提供可信 CA 证书；本地自签名部署可直接使用服务端证书。`--target` 的主机名必须匹配证书 SAN，示例不会关闭证书校验。RPC 默认 deadline 为 300 秒，可用 `--timeout` 调整。

协议及强类型 stub 的用法见 [aha-grpc 协议](../../docs/realtime-api/aha-grpc.md)。
