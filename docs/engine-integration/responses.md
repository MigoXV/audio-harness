# OpenAI-compatible Responses 引擎

`responses` Engine 使用官方 OpenAI Python SDK 访问兼容 HTTP 服务。兼容服务至少要实现模型发现和流式 Responses；Audio Harness 不代理 OpenAI 云端账号、计费或模型列表。

## Base URL 与凭据

- Engine `target` 去除尾部 `/`；若以 `/responses` 结尾，会先移除该段再作为 SDK `base_url`。
- 推荐 target 形如 `https://llm.example.test/v1`，由 SDK 调用 `GET /models` 和 `POST /responses`。
- API Key 优先读取 Engine `api_key`，否则读取 `credential_env` 指定的环境变量。缺失时抛出配置错误，Agent 不能发布/运行。
- 不要把 key 写进文档、日志或客户端配置。当前 EngineRead 会返回 `api_key` 字段，控制面必须置于可信网络并限制读取权限。

## 模型发现

Engine validate 和 `GET /api/v1/engines/{id}/models` 调用 SDK `models.list()`。兼容响应至少提供 `data[].id`；Harness 去空白、去重、排序。连接阶段使用 `connect_timeout_seconds`：超时映射为 504，其他失败映射为 502。

## Responses 请求

每次 Agent 推理最终调用 `responses.create`，Harness 强制：

```json
{"stream":true,"store":false}
```

其余参数来自 Agent session/definition 和 conversation projection，包括 `model`、instructions、input、tools、最大输出 token 及并行工具策略。兼容端必须返回 SDK 可解析的 Responses streaming events，至少支持文本增量、完成状态和 function call 参数；具体上游字段以项目锁定的 `openai>=2.50,<3` SDK 类型为准。

`request_timeout_seconds` 为空时 SDK timeout 默认 180 秒。Responses concurrency 由 `max_responses_concurrency` 控制；用户打断、客户端 cancel 或连接关闭会取消消费任务。

## 工具与 MCP

- 进程内 service tool 会被转换为严格 function definition。
- MCP server 工具先由 Harness 发现，再以 `mcp__label__name` function 暴露给 Responses 引擎；兼容模型只需要产生普通 function call。
- tool output 会作为 `function_call_output` 投回后续 Responses input。
- approval、工具轮数、并发与 timeout 在 Harness 执行，Responses 服务不应自行假设无限工具循环。

## 兼容性验证

1. 创建 `protocol=https, api=responses` Engine。
2. 调用 Engine models/validate，确认 `/models`。
3. 创建最小 Agent，关闭工具，验证文本流。
4. 绑定 TTS，验证文本增量能持续驱动 duplex TTS。
5. 最后加入单个 service tool，再验证并行工具和 MCP approval。

若兼容服务只实现 Chat Completions 或非 OpenAI 格式 SSE，不能注册为 `responses` Engine。

