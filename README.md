# Audio Harness

Audio Harness 是面向实时音频智能的可编排基础设施。当前第一版聚焦 ASR 与
VAD：服务接收持续 PCM 音频流，使用物理 VAD 划分语音段，可选地使用语义
VAD 判断说话轮次，最后把完成的轮次交给 ASR，并通过兼容 `UxSpeech` 的
gRPC 双向流返回结果。

## 架构

```text
UxSpeech grpc.aio 服务
        │
        ▼
协议无关的异步 ASR+VAD 工作流
        ├── UxVoiceActivityDetector.StreamingDetect（物理 VAD）
        ├── UxVoiceActivityDetector.Detect（可选语义 VAD）
        └── UxSpeech.StreamingRecognize（ASR）
```

所有下游引擎通道在服务启动时创建并复用。每个会话通过有界异步队列连接
输入、VAD、分段、ASR 和结果输出；同一会话严格保序，跨会话 ASR 并发由
全局容量控制。核心工作流不依赖 protobuf，后续可以在其上增加 OpenAI
Realtime、WebSocket 或电话网络适配器。

## 协议行为

- 对外接口：`ux_speech.UxSpeech/StreamingRecognize`。
- 首包必须携带 `StreamingRecognitionConfig`，后续包只能携带
  `audio_content`。
- 第一版只支持单声道 PCM S16LE；未指定采样率时默认使用 16 kHz。
- VAD 确认片段后才启动 ASR。无论请求中的 `interim_results` 值为何，
  下游 ASR 的临时和最终结果都会透传。
- 语义 VAD 判定轮次未完成时，返回空文本、`is_final=true`、
  `turn_completed=false`；后续轮次完成时合并音频再转写。
- 语义 VAD 不可用时记录告警并放行，物理 VAD 或 ASR 不可用时终止 RPC。
- 当前 `WordInfo` 表示整个语音段，不是真实词级时间戳。

## 配置

复制环境变量示例并填写三个引擎地址：

```bash
cp .env.example .env
set -a
source .env
set +a
```

默认配置位于 `configs/asr-vad.yaml`。`${NAME}` 会在读取 YAML 前按当前环境
变量展开。`semantic_vad` 是可选能力；不需要时从 YAML 中删除整个
`semantic_vad` 节点。

主要策略默认值：

- 输入重分块：6400 字节；
- 最短语音段：0.5 秒；
- 最长语音段：60 秒；
- 最大活动会话：64；
- 最大并发 ASR：8。

## 安装与启动

```bash
poetry env use python3.10
poetry install
poetry run audio-harness serve --config configs/asr-vad.yaml
```

监听地址也可以通过命令行或环境变量覆盖：

```bash
poetry run audio-harness serve \
  --config configs/asr-vad.yaml \
  --host 0.0.0.0 \
  --port 50051
```

使用单声道 16-bit PCM WAV 做一次流式调用：

```bash
poetry run python examples/grpc_client.py \
  path/to/audio.wav \
  --target 127.0.0.1:50051
```

## 生成 protobuf

协议文件位于 `src/audio_harness/protos`。生成脚本会检查 `grpcio` 版本，
安装完全相同版本的 `grpcio-tools`，并生成 Python、gRPC 与类型声明：

```bash
bash scripts/run_grpcio_tools.sh
```

## 验证

```bash
poetry check
poetry run pytest
poetry run python -m audio_harness.commands.app --help
```

测试使用进程内假引擎，不需要连接真实 ASR/VAD 服务。
