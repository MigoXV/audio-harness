# Realtime 转写兼容修复与回归

验证日期：2026-11-05。分支：`fix/realtime-transcription-closure`。服务端为本仓库；`/workspace/apps/aha-flow` 为 Qt 客户端，本次没有修改客户端、合并分支或部署线上服务。

## 协议依据

依据当前官方[客户端事件参考](https://developers.openai.com/api/reference/resources/realtime/client-events)及[服务端事件参考](https://developers.openai.com/api/reference/resources/realtime/server-events)：非空提交触发转写，空缓冲提交报错；提交项与转写完成或失败通过 `item_id` 关联；错误中的 `event_id` 关联客户端请求。阈值范围为 0 到 1。这里的 2 秒静音验收预算是工程目标，不是官方时限。

## 调用链与根因

WebSocket `/v1/realtime` → `serving/realtime/connection.py` 解码 → `core.py` 会话选模、容量租约、流式重采样 → `session.py` 配置、追加和提交 → `sessions/audio_workflow/session.py` 音频输入队列与工作流输出 → 已发布 DAG 的 VAD、ASR 节点 → `session.py` 关联语音项并发送事件 → 连接输出队列 → WebSocket。

默认 VAD 的 commit 原来仅调用 `_finish_workflow()`，没有检测到语音就没有 SpeechStopped、提交项及 ASR 输出，结束后直接返回。AHA 的进度结束与标准转写终态没有关联。关闭 VAD 的路径先创建提交项，再执行工作流，因此同样静音有提交确认和空转写。

其他问题包括：SDK 的模型类型不强制阈值范围；部分未知字段作为扩展属性被接受；会话响应重建时丢弃降噪请求；SDK 的通用 `exclude_none=True` 序列化省略关闭 VAD 的 null；异常路径只处理首个待完成项且没有移除；收尾没有服务端超时限制。

## 修改位置及行为

- `serving/realtime/connection.py`：在 SDK 解码前校验转写输入，避免类型错误先被 SDK 拦截而丢失具体参数路径。
- `serving/realtime/core.py`：配置错误传递具体参数路径。
- `serving/realtime/session.py`：非空静音在 VAD 工作流耗尽后返回标准 `error`，代码为 `input_audio_buffer_commit_no_speech`，关联请求 event_id。真正空缓冲及音频已全部自动提交的重复请求返回 `input_audio_buffer_commit_empty`。不创建虚假转写项。
- 同文件：已接受项目在 ASR 异常、收尾超时、缺少最终结果时逐项发送 `conversation.item.input_audio_transcription.failed`；移除待完成项，防止重复终态。手动模式只在未检测到语音且工作流正常结束时允许空转写；检测到语音却没有最终结果会失败。
- 同文件：更新先校验再赋值；部分更新保留已有格式、模型和 VAD 设置；`session.updated` 保留 `turn_detection: null`。缓冲非空时禁止切换 VAD 模式。
- `configs/workflow.py`：增加 `realtime_finish_timeout_seconds`，默认 120 秒，限制提交/断连时工作流收尾等待；超时明确失败，不把未完成语音写为空文本。部署方应按负载调整此配置。

### 配置能力边界

当前工作流快照固定。源码检查确认按会话提供的 threshold、prefix_padding_ms、silence_duration_ms、eagerness 没有到运行节点的覆盖绑定，本次对这些可选覆盖返回带完整路径的错误，移除即可继续。按会话切换 semantic_vad 同样明确拒绝；这不表示已发布工作流没有语义 VAD 算法。默认 VAD 使用工作流已有图和参数。

会话级 noise_reduction 非 null 明确拒绝，不能据此断言底层图不存在降噪节点。输入配置中的未知字段明确拒绝。该策略是本服务的严格兼容子集，未调用官方付费服务验证所有未知字段的实际处理方式。其他未涉及的配置字段不在本次完整兼容声明范围内。

合法 VAD 覆盖参数的拒绝属于兼容能力收窄，避免成功回显掩盖没有运行绑定；不是通过真实音频实验确认参数算法无效。语义算法效果、eagerness 和不同合法数值的音频效果仍待独立验证。

## 真实服务对照

线上旧版：`https://192.168.0.222:10000`。本地修改版：`https://127.0.0.1:10080`，gRPC `127.0.0.1:50081`，独立 SQLite 备份 `/tmp/realtime-validation.db`，连接实际推理引擎，模型 `default-audio`。没有改写线上数据库。采样为内存生成的单声道 PCM16LE，24 kHz，10 个 100 ms 包，按实时节奏追加。下表耗时从发送 commit 起算，均为单次观测。

| 模式 | AHA | 线上旧版 | 本地最终版 |
| --- | --- | --- | --- |
| 默认 VAD | 关闭 | 6 秒无标准结果 | 24 ms 标准 error，关联 probe-commit |
| 默认 VAD | 开启 | 6 秒无标准结果 | 21 ms 标准 error，关联 probe-commit |
| VAD=null | 关闭 | committed 3 ms；completed 188 ms | committed 11 ms；completed 172 ms |
| VAD=null | 开启 | committed 3 ms；completed 183 ms | committed 5 ms；completed 197 ms |

手动模式两次本地结果均为空文本且与 committed 使用同一 item_id。AHA 扩展不参与终态判断。

另用 ffmpeg/flite 在内存生成英文语音，接实际引擎测试默认 VAD，完整输入和约 65% 处停止输入、分别开关 AHA。四次均返回 committed 和同 item_id 的 completed，无错误；提交后约 126–200 ms。完整输入识别为 “The quick brown fox jumps over the lazy dog. This is a speech transcription test.”；中途停止返回已输入部分。该项验证使用了本次收尾修复版本；最后增加的可选配置拒绝及手动缺失结果保护不影响其路径。合成语音不代替真人录音与麦克风验收。

最终本地进程另已验证 threshold=2.0、noise_reduction、合法但无绑定的 threshold=0.65 均按对应路径拒绝并关联请求；移除参数、关闭 VAD、再次仅更新语言均成功且保持 VAD=null。

## 自动化验证

```bash
poetry run pytest tests/test_diarization.py tests/test_realtime_service.py tests/test_realtime_session_events.py tests/test_realtime_protocol.py -q
```

最终结果：166 passed。覆盖默认/关闭 VAD、AHA 开关、空缓冲、极短音频、1 秒静音、模拟正常语音、自动提交后手动提交、连续轮次、提交后立即追加下一轮、ASR 失败、缺失最终结果、内部超时、断连清理、参数错误及修正重试。正常语音及故障注入测试使用假推理器；不得将其当作模型效果实测。

完整测试集在最后追加的四个流水轮次测试之前运行：524 passed，9 failed，23 errors。对照原始提交 e0370bc 的隔离工作树：499 passed，11 failed，23 errors。最终完整运行的 9 个失败名称均在原始失败列表内，主要涉及已移除的 realtime_routes、旧管理 API/网页测试和 OpenAPI/文档快照；23 个错误集中于旧控制面测试初始化。没有修复这些无关问题，不能宣称全仓测试通过。原始和早期运行还出现 TestClient 退出时取消异常；新增测试显式等待服务端断连清理，未修改客户端超时。

## 回归与启动

对指定服务执行四种静音组合（仅向该服务发送测试会话）：

```bash
poetry run python scripts/check_realtime_transcription.py https://127.0.0.1:10080 --insecure
poetry run python scripts/check_realtime_transcription.py https://192.168.0.222:10000 --insecure
```

`--insecure` 仅用于该开发环境自签名证书。脚本观察窗为 6 秒，只用于记录结果，未改变产品客户端的收尾行为。输出标准事件及提交后的毫秒数，不保存音频。

本次隔离启动命令（数据库已用 SQLite backup API 从本地配置备份；不要将本机数据库或密钥加入 Git）：

```bash
DATABASE_URL=sqlite+aiosqlite:////tmp/realtime-validation.db \
poetry run audio-harness --http-host 127.0.0.1 --http-port 10080 \
  --grpc-host 127.0.0.1 --grpc-port 50081
```

当前 Typer 单命令入口直接接受参数，不加 `serve` 子命令。TLS 使用项目已有本机证书，不在本次提交范围。

## 待验收

线上服务尚未部署，默认 VAD 的线上问题仍可复现。尚未验证真人多说话人语音、真实尾字完整性、不同模型能力、长会话和并发负载、语义 VAD 实际断句效果、降噪音频效果，以及 WebRTC/gRPC 与本次 WebSocket 行为的全面一致性。关闭 VAD 的现有实现仍通过工作流图处理音频，并非完全绕过图内 VAD；对低音量、极短真实语音的模型效果需要进一步验收。
