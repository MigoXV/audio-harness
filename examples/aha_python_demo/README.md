# aha Python SDK 示例

使用独立 `aha-python` SDK 连接已有语音智能体，发送一条内存文本并消费文字和 TTS 音频事件。
示例不读写音频文件，不自动批准工具操作。

先按 [SDK 安装说明](../../docs/realtime-api/python-sdk.md) 安装 wheel，再在服务端配置并发布语音智能体：

```bash
AHA_BASE_URL=http://localhost:8000 AHA_MODEL=voice-agent poetry run python examples/aha_python_demo/main.py
```

`AHA_MODEL` 为已发布智能体的 Slug；如果部署网关要求 Bearer 凭证，通过 `AHA_API_KEY` 提供。
程序打印文字和收到的 PCM 字节数，应用可将 PCM 接入自己的播放器。
收到 `response.done` 后显示响应状态并退出；整体等待期限为 60 秒，超时清理连接。
若工作流需要人工审批，应用需显式处理审批事件；这个最小示例不会替用户作决定。
