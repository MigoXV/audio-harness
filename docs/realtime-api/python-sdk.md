# Python SDK 接入

SDK 仓库位于 `/workspace/libs/aha-python`，发行包名为 `aha-python`，导入名为 `aha`。
首版通过已有 `/v1/realtime` 和 `x_aha=v1` 提供同步／异步客户端，服务端接口与业务语义保持原样。

## 构建与安装

先在 SDK 仓库通过测试并构建，再在服务端的 Poetry 环境安装 wheel：

```bash
poetry -C /workspace/libs/aha-python run pytest
poetry -C /workspace/libs/aha-python build
poetry run python -m pip install /workspace/libs/aha-python/dist/aha_python-0.1.0-py3-none-any.whl
poetry run python -c "import aha; print(aha.__version__, aha.__file__)"
```

其他机器可复制该 wheel 后安装，运行时不依赖 SDK 源码目录。
同版本 wheel 重新构建后需用 `--force-reinstall --no-deps` 更新代码（仅在依赖已满足时使用）。
本阶段不添加 Poetry 路径依赖、不修改服务端锁文件。新建环境或执行 `poetry sync` 后需重新安装 wheel。
以后 SDK 发布到 PyPI，再将 `aha-python` 加入服务端 Poetry 依赖并更新锁文件。

## 客户端能力

```python
from aha import AudioHarness, AsyncAudioHarness, AudioFormat

client = AsyncAudioHarness(base_url="http://localhost:8000")
# 完整用法应使用 async with client，并并行发送音频和消费结果。
connection = client.transcriptions.connect(
    model="meeting-transcribe",
    audio=AudioFormat.pcm16(sample_rate=16000),
)
# 语音智能体使用 client.realtime.connect(model="voice-agent")。
```

`model` 是服务端已发布资源的 Slug。转写连接自动配置 `intent=transcription` 和初始
`session.update`；语音智能体通过 query model 选择。`connect()` 等待配置成功后返回，初始事件仍交付用户。
默认开启 `x_aha=v1`，可用 `extensions=False` 关闭。

- `send_audio()` 接受内存 PCM bytes 或分块来源，自动分块和 Base64 编码；异步客户端也接受异步来源。
- `events()` 提供单消费者强类型事件流，包含晚到的分角色转写、聚类和后台 Agent 事件。
- `session.update`、`input_audio_buffer.commit/clear`、`response.create/cancel`、会话条目操作、工具输出、MCP／后台 Agent 审批和问答均映射现有事件。
- TTS 事件 `response.output_audio.delta` 的 `delta` 解码为 PCM bytes，响应和条目标识保留。
- 同步 `AudioHarness` 使用 `with` 和普通方法；异步 `AsyncAudioHarness` 使用 `async with` 和 `await`。

可运行的文本／TTS 示例见 [aha_python_demo](../../examples/aha_python_demo/README.md)。
SDK 仓库 README 包含持续 PCM、并行消费、审批、TLS、代理和预算配置说明。

## 结束与失败

当前 WS 没有统一的 `stream.drained/session.closed` 事件。SDK 不提供假定所有分支完成的 `finish()`，
`close()` 和上下文退出只负责关闭连接。输入耗尽不自动 commit 或关闭；主转写完成、静默超时都不能证明晚到结果已经齐全。

服务端 `error` 仍是可恢复业务事件，SDK 不自动断连或重试该操作。
断线抛 `ConnectionLost`，慢消费者超过本地预算抛 `ConsumerBackpressure`；SDK 不重连、不重放音频或业务动作。
SDK 不管理扬声器，WS 播放缓冲由应用清理；取消生成使用 `response.cancel`，已播放内容同步沿用 conversation truncate。

单连接保持一路输入；SDK 不新增 Files、Jobs、Artifacts 或文件转写。原有服务端音色上传、声纹管理、录音缓存、WebRTC 和 gRPC 功能均保留。

## 服务端共享契约

服务端直接从 `aha.contracts.protocol` 导入协议模型与校验器，从
`aha.contracts.binary` 导入二进制事件契约，从 `aha.contracts.json_codec`
导入 JSON/Base64 编解码。旧 `audio_harness.serving.realtime.protocol`、
`contracts`、`json_codec` 转发模块已删除，不再提供旧导入路径。
服务端不会导入 SDK 客户端实现；工作流、推理调度、会话状态机和现有 gRPC 生成物没有迁移。

契约验证命令：

```bash
poetry run pytest -q tests/test_aha_sdk.py tests/test_realtime_protocol.py tests/test_aha_grpc.py
poetry run python scripts/generate_aha_grpc.py --check
```

集成测试从已安装 SDK 经真实 WS 路由发送 16/24 kHz PCM，并验证同步／异步转写、TTS、可恢复错误及资源释放。
测试使用现有假推理引擎，验证协议与编排行为，不代表真实模型精度或性能基准。
