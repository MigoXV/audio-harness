# Agent Backends

Agent Backend 是与推理引擎平级的远端智能体运行资源。它保存连接信息和健康状态，但不保存 DSH 使用的模型密钥；DSH 应在自己的进程或容器中配置模型凭据。

首版支持 `kind=dsh`、`protocol=dsh_web_v1`。`target` 是 HTTP(S) base URL，WebSocket 地址由服务自动派生。需要经过鉴权代理时，用 `headers_env` 把请求头名称映射到 Audio Harness 进程的环境变量名称，API 不会读取或返回这些变量的值。

## 接口

<!-- API: GET /api/v1/agent-backends -->
<!-- API: POST /api/v1/agent-backends -->
<!-- API: GET /api/v1/agent-backends/{backend_id} -->
<!-- API: PATCH /api/v1/agent-backends/{backend_id} -->
<!-- API: DELETE /api/v1/agent-backends/{backend_id} -->
<!-- API: POST /api/v1/agent-backends/{backend_id}/validate -->
<!-- API: GET /api/v1/agent-backends/{backend_id}/capabilities -->
<!-- API: GET /api/v1/agent-backends/{backend_id}/usage -->

| 接口 | 用途 |
|---|---|
| `GET/POST /api/v1/agent-backends` | 分页查询或创建资源 |
| `GET/PATCH/DELETE /api/v1/agent-backends/{backend_id}` | 读取、更新或删除资源 |
| `POST /api/v1/agent-backends/{backend_id}/validate` | 执行一次 HTTP/WS 连通性探测并持久化状态 |
| `GET /api/v1/agent-backends/{backend_id}/capabilities` | 读取 DSH host、provider、model、preset 和插件清单 |
| `GET /api/v1/agent-backends/{backend_id}/usage` | 查询语音智能体引用，删除和停用前使用 |

运行字段变化会增加资源 `revision`。已发布语音智能体使用编译时固化的 revision，不会被后续编辑静默替换。

## 远端部署边界

Audio Harness 不提供 DSH 镜像或 Compose。生产部署应让 DSH 独立持有模型密钥，
经私网或鉴权代理同时暴露 HTTP JSON-RPC、`events.mux` 和 `events.host` WebSocket。
Audio Harness 与 DSH 位于不同容器时不使用 stdio；DSH 自己需要的 stdio MCP 在 DSH
容器内部启动。`headers_env` 只解决 Audio Harness 到代理的请求头注入，不会把
Responses Engine 的数据库密钥传给 DSH。
