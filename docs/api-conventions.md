# 通用 API 约定

## 地址与传输

| 接口 | 默认地址 | 编码 |
| --- | --- | --- |
| 控制面 HTTP | `http://localhost:8000/api/v1` | UTF-8 JSON；音频/音色接口使用 `multipart/form-data` |
| Realtime WebSocket | `ws://localhost:8000/v1/realtime` | UTF-8 JSON 文本消息，音频放在 Base64 字段中 |
| WebRTC 信令 | `POST http://localhost:8000/v1/realtime/calls` | `multipart/form-data`，响应为 `application/sdp` |
| gRPC 数据面 | `localhost:50051` | Protobuf + HTTP/2 |

服务支持在 HTTP 监听器上配置证书；生产环境应使用 HTTPS/WSS。当前应用没有认证中间件，不校验 `Authorization`。部署方必须在受信网络或 API Gateway 后提供 TLS、身份认证、访问控制、审计和请求限流。该说明只描述 Audio Harness 本身，不代表下游 Responses 引擎不需要 API Key。

## JSON 与字段规则

- 控制面请求模型统一使用 `extra="forbid"`：未知字段返回 `422 request_validation_failed`。
- 日期时间使用 ISO 8601/RFC 3339 字符串；客户端不应依赖本地时区展示。
- 资源 `id` 由服务生成，示例中的 `eng_xxx`、`wf_xxx`、`agent_xxx` 仅为占位符。
- `slug` 使用小写 kebab-case；工作流节点 ID 可使用字母、数字、下划线和连字符。
- 响应中未明确声明可省略的字段，客户端都应按 Schema 处理；机器契约见 [OpenAPI](reference/openapi.json)。
- JSON 示例只展示调用所需字段；完整默认值和枚举以 OpenAPI Schema 为准。

## HTTP 成功状态

| 状态 | 语义 |
| --- | --- |
| `200 OK` | 查询、更新、动作或 hangup 成功 |
| `201 Created` | Engine、Workflow、Agent、Vector Space、Voice、Point 或 WebRTC call 创建成功 |
| `204 No Content` | 删除、销毁或 payload 替换成功，不返回 JSON body |

## 统一错误包络

除 WebSocket 建连拒绝、WebRTC SDP 成功响应和 gRPC 外，应用错误使用：

```json
{
  "error": {
    "code": "revision_conflict",
    "message": "草稿 revision 已变化",
    "details": null
  }
}
```

`details` 可以是 `null`、校验错误数组、图诊断数组或冲突资源。通用映射见[错误索引](reference/errors.md)。

## 分页与查询

当前只有 Engine 列表使用 offset 分页：

```http
GET /api/v1/engines?limit=100&offset=0&kind=asr&enabled=true&q=speech
```

`limit` 为 `1..500`，`offset >= 0`。响应同时返回 `items`、`total`、`limit`、`offset`。Vector Point 使用 Qdrant 的不透明 `next_offset` 游标，客户端只能原样传回，不能自行计算。

## 乐观锁与不可变发布

Workflow、Voice Agent、Workflow Settings 和 Capacity Settings 的更新请求携带当前 revision：

```json
{"expected_revision": 3, "document": {"schema_version": 1, "nodes": [], "edges": [], "outputs": []}}
```

Settings 使用字段名 `revision`，资源草稿和发布使用 `expected_revision`。revision 不匹配返回 `409 revision_conflict`；客户端应重新读取、合并后重试，不能盲目递增。

发布会重新校验并固定依赖的 Engine revision。活动会话继续使用建立时的工作流/Agent/Engine 快照；恢复历史 revision 只会复制为新草稿，不会直接切换活动版本。

## 音频约定

- Realtime WebSocket 输入：Base64 编码、单声道 PCM S16LE，协商格式为 `audio/pcm`、`24000 Hz`；服务在线重采样到内部 `16000 Hz`。
- gRPC ASR 和引擎适配器：PCM S16LE，字节数必须为偶数；具体采样率由各协议配置字段给出。
- HTTP 音频上传允许常见容器，服务端解码后统一到内部格式；空音频、无法解码音频或不完整样本返回错误。
- 单条协议消息受 `max_message_bytes` 限制，默认 `4 MiB`。

## 兼容性原则

- HTTP API 版本前缀为 `/api/v1`；AHA Realtime 扩展通过 `x_aha=v1` 单独协商。
- OpenAI Realtime 兼容表示“实现了本文列出的事件子集”，不表示代理 OpenAI 云端全部能力。
- 未协商 AHA 时只发送通过当前 OpenAI SDK `RealtimeServerEvent` 校验的原生事件。
- 下游 Proto 文件是引擎线协议的唯一结构定义；文档解释调用顺序和额外语义，不复制生成代码。

