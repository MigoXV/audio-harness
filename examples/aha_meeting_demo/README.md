# AHA 会议转写 JavaScript Demo

这个示例以一个浏览器页面演示完整的会议音频链路：上传 WAV、以约 10× 实时速度发送 PCM、观察音频发送/VAD/说话人分离进度，并以说话人甬道图查看局部分离、跨批次聚类和分角色转写结果。说话人视图是页面主视图，原生正式转写放在下方的辅助标签页中。

浏览器不会保存 Realtime 密钥。它只连接本 Demo 的 Node 服务；Node 服务持有密钥并连接 Audio Harness 的 `intent=transcription&x_aha=v1` WebSocket。

## 前置条件

Audio Harness 中必须存在一个公开且已激活的 Audio Workflow Route，并且该 Workflow 已配置：

- `primary_transcript`
- `semantic_vad`：接在物理 VAD 后，且它的 `speech_stopped` 必须绑定为正式的 `speech_stopped` 输出
- `diarization_local`
- `speaker_transcript`
- `diarization_cluster`

为显示“物理停顿、语义 VAD 仍在判定”的状态，还应将语义 VAD 的 `turn_progress` 绑定为可选输出 `turn_progress`。主物理 VAD 的进度由 Harness 自动产生，但其 `progress_interval_seconds` 不能设为 `0`。缺少说话人输出不会造成连接失败，不过页面将无法收到作为整场完成标志的最终聚类快照；缺少语义 VAD 时则无法得到语义断句。Demo 不生成会议纪要，也不采集麦克风。

本地开发需要先在仓库根目录生成证书并启动服务：

```bash
bash scripts/generate_realtime_tls.sh
poetry run audio-harness serve
```

## 安装与启动

```bash
cd examples/aha_meeting_demo
pnpm install
cp .env.example .env
pnpm dev
```

将 `.env` 中的 `REALTIME_MODEL` 改为实际的公开 Route 名称，然后打开 `https://127.0.0.1:8092`。本地使用自签名证书时，浏览器需要信任 `model-bin/realtime-tls/cert.pem`；不要通过关闭 TLS 校验绕过该要求。

`REALTIME_BASE_URL` 是 Audio Harness 的 HTTP(S) 基地址，例如 `https://127.0.0.1:8000/v1`。`REALTIME_CA_CERT_FILE` 只在上游使用自签名证书时需要设置。Demo 的地址、端口和 TLS 文件可通过 `MEETING_DEMO_*` 环境变量调整。

`pnpm dev` 与 `pnpm start` 均会以 Node 的 `--use-system-ca` 启动，因此也会信任已安装到操作系统根证书库的 CA；对于未安装到系统库的开发自签名证书，仍应设置 `REALTIME_CA_CERT_FILE`。

启动时终端会输出实际连接的上游 WebSocket URL（不含密钥）。它由 `REALTIME_BASE_URL` 转换协议并追加固定路径和查询参数：

```text
https://127.0.0.1:8000/v1
→ wss://127.0.0.1:8000/v1/realtime?intent=transcription&x_aha=v1
```

## 使用方式与时序

选择一个 WAV 文件后点击“开始会议”。浏览器用 Web Audio 的离线渲染能力下混并高质量重采样为 **16 kHz、单声道、PCM S16LE**，避免手写线性插值造成的下采样混叠。每块包含 100 ms 音频，正常时每 10 ms 向 Demo 后端发送一次，即约 **10× 实时速度**；若 WebSocket 待发送数据超过 128 KiB，Demo 会临时降速，避免网络或上游队列积压影响识别质量。这不是一次性上传，能保持流式事件行为。

Node 后端的上游时序固定如下：

```text
连接 /v1/realtime?intent=transcription&x_aha=v1
← session.created
→ session.update(model, 16 kHz PCM, server_vad)
← session.updated
→ input_audio_buffer.append *
→ input_audio_buffer.append * (2 秒尾部静音)
→ input_audio_buffer.commit
```

后端只在收到 `session.created` 后发送配置，并且只有收到 `session.updated` 后才接受浏览器音频。协议层请求兼容的 `server_vad`；语义断句仍由 Workflow 的 `semantic_vad` 节点实现。Workflow 的物理 VAD 可先报告 `x_aha.vad.physical_speech_stopped`，页面显示“仍在等待本轮语义结束”；直到语义 VAD 给出正式边界，才收到原生 `input_audio_buffer.speech_stopped` 并开始正式转写。停止发送或文件结束时，后端先补送 2 秒静音，让语义 VAD 有机会完成最后一段音频，再发送 `input_audio_buffer.commit` 排空整个 Workflow。最终聚类快照随后把说话人分离进度推进到 100%。

## 页面展示规则

“会议处理进度”在同一条轨道中叠放三层：底层为浏览器发送 PCM 的进度，中层为 `x_aha.vad.progress` 报告的主 VAD 处理进度，顶层为说话人分离进度。说话人分离层按已完成本地批次的 `batch_audio_end_ms` 前进，但只有最终 `x_aha.diarization.cluster.completed` 到达时才显示 100%。如果终止事件是 `x_aha.diarization.cluster.failed`，页面结束等待并用失败色显示顶层，不把失败伪装成成功。尾部补送的 2 秒静音只用于排空处理，不扩大页面时间轴。

甬道图以原 WAV 时长为横轴，片段位置来自 `audio_start_ms/audio_end_ms`：

- 本地说话人分离事件到达后，片段立即按 `local_speaker_id` 进入本地甬道，不等待聚类。
- 收到更新的全量聚类快照后，`speaker_id >= 0` 的片段迁移到对应全局说话人甬道；同一全局说话人跨批次合并并保持稳定颜色。
- `speaker_id=-1` 表示聚类认为该片段是噪声。页面仍保留原 `local_speaker_id` 甬道、片段和分角色转写，并增加“噪声”标记。
- 分离片段之间可能产生没有 `local_speaker_id` 的补空白/兜底 ASR 片段；它们不代表说话人，不进入甬道图或“全部说话人转写”。
- 最终聚类完成后，少数有本地说话人但没有 assignment 的片段显示为“未归类”，不会继续显示“待聚类”。
- 分角色 ASR 会跳过短于说话人分离节点 `min_asr_segment_seconds`（默认 0.5 秒）的片段。它们可在处理中短暂显示“识别中”，但会议处理结束后会从甬道和全部说话人转写中隐藏，避免没有文本的碎片干扰阅读。
- 点击甬道名称或片段会选中该说话人，下方“说话人转写”按时间列出它的分角色转写；“全部说话人转写”按时间顺序汇总所有甬道的分角色转写，并合并相邻的同一说话人片段。

原生正式转写与分角色片段转写的粒度不同，Demo 不会合并或去重两种结果。新的聚类 revision 可能重新分配已有片段，当前选中项会跟随所选片段迁移到新的全局甬道。

## 验证

```bash
pnpm lint
pnpm test
```

测试覆盖 PCM 转换和分块、正式/临时转写覆盖、三层进度终态、局部分离和分角色文本覆盖、聚类全量替换、噪声片段保留、甬道迁移与选择，以及模拟上游 `session.created → session.update → session.updated` 生命周期。
