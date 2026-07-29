# Audio Harness

Audio Harness 是面向实时音频智能的可编排基础设施。当前第一版聚焦 ASR 与
VAD：`grpc.aio` 数据面接收持续 PCM 音频流，物理 VAD 划分语音段，可选语义
VAD 判断说话轮次，最后调用 ASR，并通过兼容 `UxSpeech` 的双向流返回结果。

服务同时提供异步 REST 控制面，使用 SQLAlchemy 和 SQLite 管理 ASR/VAD
引擎数字资产及运行时角色绑定。

## 架构

```text
REST 控制面（FastAPI）             gRPC 数据面（grpc.aio）
        │                                  │
        ▼                                  ▼
SQLAlchemy AsyncSession              每个音频流获取路由租约
        │                                  │
        ├── 引擎资产 CRUD                   ├── 物理 VAD
        └── ASR/VAD 角色绑定                ├── 可选语义 VAD
                    │                       └── ASR
                    └──── EngineRouter ──────┘
                                  │
                         InferencerFactory
                                  │
                 ┌────────────────┴────────────────┐
                 ▼                                 ▼
       inferencers/grpc/asr.py           inferencers/grpc/vad.py
```

ASR、物理 VAD 和语义 VAD 的角色绑定保存在 SQLite 中。每个新音频流在开始
时获取一份不可变路由快照；REST 切换绑定或更新引擎地址后，新流使用新引擎，
已经开始的流继续使用旧引擎。旧 gRPC 通道会在最后一个租约释放后关闭。

每个会话仍通过有界异步队列连接输入、VAD、分段、ASR 和结果输出；同一会话
严格保序，跨会话 ASR 并发由全局容量控制。

`pipelines`、领域模型和 `EngineRouter` 只依赖 `inferencers/contracts.py`
定义的异步能力接口，不导入 gRPC 或 protobuf。`UxSpeech` 与 `UxVAD` 的请求
构造、响应解析、错误映射和通道生命周期分别封装在对应的 gRPC inferencer
中；服务装配层通过工厂选择实现，为后续增加其他通讯协议保留边界。

## 安装与启动

```bash
poetry env use python3.10
poetry install
pnpm --dir src/web install
pnpm --dir src/web build
cp .env.example .env
poetry run audio-harness serve --config configs/asr-vad.yaml
```

`serve` 会在监听端口前自动执行 Alembic 升级。默认地址：

- gRPC 数据面：`0.0.0.0:50051`
- REST 管理面：`0.0.0.0:8000`
- Web 配置工作台：`http://127.0.0.1:8000/`
- SQLite：`./data/audio-harness.db`

相对数据库路径基于进程工作目录解析。监听地址可通过 YAML、命令行或环境变量
覆盖：

```bash
poetry run audio-harness serve \
  --config configs/asr-vad.yaml \
  --host 0.0.0.0 \
  --port 50051 \
  --management-host 0.0.0.0 \
  --management-port 8000
```

> REST 管理面第一版没有鉴权，并默认监听所有网卡。只能部署在可信内网，或放在
> 带鉴权和访问控制的反向代理之后。

## Web 配置工作台

`src/web` 提供基于 React、TypeScript 和 Vite 的单页配置工作台，用于管理
ASR/VAD 引擎资产、三个运行时角色绑定及服务健康状态。生产构建产物由
FastAPI 同源暴露，因此浏览器请求不需要额外配置 CORS。

本地开发时先启动后端，再启动 Vite：

```bash
poetry run audio-harness serve --config configs/asr-vad.yaml
pnpm --dir src/web dev
```

开发页面位于 `http://127.0.0.1:5173/`，Vite 会把 `/api` 和 `/health`
代理到 `http://127.0.0.1:8000`。部署或直接通过管理端口访问前，执行：

```bash
pnpm --dir src/web build
```

如果尚未构建前端，`GET /` 会返回 503 和构建提示，但 REST API、健康检查及
OpenAPI 文档仍可正常使用。工作台会异步探活后再切换角色绑定；探活失败时保留
原绑定，并展示服务端返回的错误。

## 初始引擎资产

首次建库会预置知识库中记录的 11 个资产：

- 2 个 Qwen ASR；
- 3 个对外暴露 `UxSpeech` 的 ASR+VAD sidecar；
- 3 个现用 FireRedVAD；
- 3 个已停用 VAD。

知识库中的镜像、场景、状态依据和观测时间会一并保存。已停用实例不能绑定。
首次启动不会自动绑定任何引擎，避免根据历史审计记录直接发送业务流量。

此时 `/health/live` 返回 200，`/health/ready` 返回 503；完成 `asr` 和 `vad`
绑定后数据面才进入 ready 状态。

## REST 管理接口

OpenAPI 文档位于 `http://127.0.0.1:8000/docs`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/v1/engines` | 创建引擎资产 |
| `GET` | `/api/v1/engines` | 分页、过滤和搜索资产 |
| `GET` | `/api/v1/engines/{id}` | 查询资产 |
| `PATCH` | `/api/v1/engines/{id}` | 更新资产 |
| `DELETE` | `/api/v1/engines/{id}` | 删除未绑定资产 |
| `GET` | `/api/v1/engine-bindings` | 查询三个角色的绑定 |
| `PUT` | `/api/v1/engine-bindings/{role}` | 探活并原子切换绑定 |
| `DELETE` | `/api/v1/engine-bindings/{role}` | 解除绑定 |

列表接口支持 `limit`、`offset`、`kind`、`api`、`enabled` 和 `q` 参数。

查询可绑定的 ASR 和 VAD：

```bash
curl 'http://127.0.0.1:8000/api/v1/engines?api=ux_speech&enabled=true'
curl 'http://127.0.0.1:8000/api/v1/engines?api=ux_vad&enabled=true'
```

使用查询结果中的资产 ID 完成绑定：

```bash
curl -X PUT 'http://127.0.0.1:8000/api/v1/engine-bindings/asr' \
  -H 'Content-Type: application/json' \
  -d '{"engine_id":"<ASR_ENGINE_ID>"}'

curl -X PUT 'http://127.0.0.1:8000/api/v1/engine-bindings/vad' \
  -H 'Content-Type: application/json' \
  -d '{"engine_id":"<VAD_ENGINE_ID>"}'
```

绑定前会按资产的连接超时异步等待 gRPC 通道 ready。探活失败返回 503，旧绑定
不变；接口不兼容、资产停用、删除已绑定资产等冲突返回 409。`asr` 角色要求
`ux_speech`，`vad` 和 `semantic_vad` 要求 `ux_vad`。

## gRPC 协议行为

- 对外接口：`ux_speech.UxSpeech/StreamingRecognize`。
- 首包必须携带 `StreamingRecognitionConfig`，后续包只能携带
  `audio_content`。
- 第一版只支持单声道 PCM S16LE；未指定采样率时默认使用 16 kHz。
- VAD 确认片段后才启动 ASR。下游 ASR 的临时和最终结果始终透传。
- 语义 VAD 未绑定时直接使用物理 VAD 片段；运行中不可用时记录告警并放行。
- 物理 VAD 或 ASR 不可用时终止当前 RPC。
- 未完成必需角色绑定时，新 RPC 返回 `FAILED_PRECONDITION`。
- 当前 `WordInfo` 表示整个语音段，不是真实词级时间戳。

使用单声道 16-bit PCM WAV 做一次流式调用：

```bash
poetry run python examples/grpc_client.py \
  path/to/audio.wav \
  --target 127.0.0.1:50051
```

## 配置与容量

默认配置位于 `configs/asr-vad.yaml`，包括数据库、工作流、gRPC 数据面和 REST
管理面。主要策略默认值：

- 输入重分块：6400 字节；
- 每路流式音频环形缓冲：120 秒；
- VAD 语音起点前保留：0.3 秒；
- 最短语音段：0.5 秒；
- 最长语音段：60 秒；
- 最大活动会话：64；
- 最大并发 ASR：8。

## 生成 protobuf

协议文件位于 `src/audio_harness/protos`。生成脚本会匹配 `grpcio-tools` 与当前
`grpcio` 版本，并生成 Python、gRPC 与类型声明：

```bash
bash scripts/run_grpcio_tools.sh
```

## 验证

```bash
poetry check
poetry run pytest
poetry run python -m audio_harness.commands.app --help
pnpm --dir src/web lint
pnpm --dir src/web test
pnpm --dir src/web build
pnpm --dir src/web test:server
```

测试使用临时 SQLite 和进程内假 gRPC 引擎，不需要连接知识库中的真实服务。
