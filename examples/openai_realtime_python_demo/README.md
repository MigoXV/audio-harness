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
