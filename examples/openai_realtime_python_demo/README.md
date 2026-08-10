# OpenAI Realtime Python WAV Demo

这个 Demo 只做一件事：读取 WAV，转换为 24 kHz 单声道 PCM16，然后按
100 ms 大小分块、无延迟连续发送到 OpenAI Realtime API，并逐行打印最终转写。

它直接使用主项目已有的 OpenAI Python SDK，不依赖 `python-dotenv`。先在仓库
根目录准备并导出环境变量：

```bash
cp examples/openai_realtime_python_demo/.env.example \
  examples/openai_realtime_python_demo/.env
# 编辑 .env，将 OPENAI_API_KEY 改成服务端配置的密钥
set -a
. examples/openai_realtime_python_demo/.env
set +a
```

然后运行：

```bash
poetry run python examples/openai_realtime_python_demo/main.py \
  data-bin/audio.wav --seconds 30
```

删除 `--seconds 30` 就会发送整个 WAV。连接官方服务时，将
`OPENAI_BASE_URL` 改为 `https://api.openai.com/v1`，并清空
`OPENAI_CA_CERT_FILE`。

## 启用 x_aha 扩展

连接 Audio Harness 本地 Realtime 接口时，可以增加 `--x-aha`：

```bash
poetry run python examples/openai_realtime_python_demo/main.py \
  data-bin/audio.wav --seconds 30 --x-aha
```

该参数会在 OpenAI Python SDK 建连时传入：

```python
extra_query={"intent": "transcription", "x_aha": "v1"}
```

不启用扩展时仍会传入 `intent=transcription`。模型不放在 WebSocket URL 中，而由随后
发送的 `session.update.audio.input.transcription.model` 指定。

当物理 VAD 已检测到一段语音结束、但语义 VAD 判断用户还没有说完时，
服务端会发送以下扩展事件：

```json
{
  "type": "x_aha.vad.physical_speech_stopped",
  "event_id": "event_xxx",
  "item_id": "item_xxx",
  "audio_start_ms": 1200,
  "audio_end_ms": 2380,
  "turn_completed": false
}
```

后续语义轮次真正完成时仍发送标准
`input_audio_buffer.speech_stopped`。不使用 `--x-aha` 时，服务端只发送
OpenAI Realtime 原生事件，因此原有客户端不需要修改。
