# AI 会议占位聚类部署验证

2026-10-16 重启 audio-harness.service，加载用户更新后的提交 1a51780。
服务保持 active。通过控制 API 在既有 ai-meeting 工作流修订 3 上增加：

- 引擎 dummy-clustering：kind=embedding，protocol=dummy，api=dummy_clustering，target=dummy://clustering。
- 节点 speaker-clustering，从 speaker-diarization.segments 接收数据。
- 输出 diarization_cluster，绑定 speaker-clustering.results。

工作流验证无诊断，已发布修订 4。现有 VAD、ASR、AGC、说话人分离配置保持不变。
占位聚类统一返回 speaker_id=0，不代表真实说话人身份识别。

用 5.13 秒真实音频经 TLS gRPC 与 WebSocket 分别验证，两者均收到正常转写、
聚类全量快照和最终事件 x_aha.diarization.cluster.completed：
final=true，processed_audio_ms=5130，assignments 中 speaker_id=0。
最终事件到达时间分别约 0.905 秒、0.819 秒（含连接，10 倍速上传）。

active-before.json / draft-before.json 保存修改前配置，active-after.json 保存已发布配置。
引擎和工作流验证及两个协议的原始事件均保存在同目录。
没有修改业务源码；配置通过管理 API 持久化。
