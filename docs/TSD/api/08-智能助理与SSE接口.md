# 08 智能助理与 SSE 接口

| 字段 | 日期 |
| --- | --- |
| 创建时间 | 2026-07-23 |
| 最后更新时间 | 2026-08-18 |

> 本页接口全部为 `internal`，不在首期外部 allowlist。会话、页面上下文、品牌绑定、提议动作和工具结果均属于用户或品牌敏感数据。

## 1. 接口索引

| 方法 | 路径 | 请求 | 响应 | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/dashboard/assistant-context` | `date?`,`brand?` | JSON 上下文 | 按品牌权限读取。 |
| GET | `/api/assistant/sessions` | 无 | `{items:[...]}` | 当前用户会话。 |
| GET | `/api/assistant/runtime-inspector` | 无 | 运行时、权限、工作区、工具与事件 | 当前账号可见的 PI Agent 运行时检查信息。 |
| GET | `/api/assistant/workspace` | `path?` | 工作区目录和文件条目 | 当前用户授权项目工作区内的目录浏览。 |
| GET | `/api/assistant/workspace/file` | `path` | 文件预览 | 当前用户授权项目工作区内的 UTF-8 文本预览；二进制文件不返回内容。 |
| POST | `/api/assistant/workspace/file` | multipart `file` + `path` | 上传结果 | 当前用户授权项目工作区内上传，单文件上限 10 MB。 |
| POST | `/api/assistant/terminal/sessions` | `cwd` | 终端会话 | 当前用户授权项目目录内创建终端会话。 |
| POST | `/api/assistant/terminal/sessions/{terminal_session_id}/execute` | `command` | 输出、退出码 | 服务端校验工作目录后执行命令。 |
| GET | `/api/assistant/terminal/sessions/{terminal_session_id}` | 无 | 增量输出、状态、退出码 | 查询当前用户终端会话。 |
| POST | `/api/assistant/terminal/sessions/{terminal_session_id}/stop` | 无 | 停止状态 | 回收当前终端会话。 |
| POST | `/api/assistant/sessions/{session_meta_id}/branches` | `messageId` | 新会话 | 从指定消息创建独立会话分支。 |
| PATCH | `/api/assistant/sessions/{session_meta_id}` | `title?`,`pinned?` | 会话元数据 | 仅当前用户会话。 |
| DELETE | `/api/assistant/sessions/{session_meta_id}` | 无 | 删除状态 | 仅当前用户会话。 |
| POST | `/api/assistant/actions/prepare` | `sessionId`,`action` | 待确认动作 | 固化授权快照。 |
| POST | `/api/assistant/actions/{action_request_id}/confirm` | 无 body | 执行结果 | 并发 claim；可能返回 409。 |
| POST | `/api/assistant/actions/{action_request_id}/cancel` | 无 body | 取消结果 | 仅改变动作状态。 |
| POST | `/api/assistant/chat` | `message` 等 | JSON 会话结果 | 会持久化会话和提议动作。 |
| POST | `/api/assistant/chat/stream` | 同 chat | `text/event-stream` | SSE 流式响应。 |
| POST | `/api/internal/assistant/tools/{tool_name}` | `userId`,`arguments`,`context?`；`X-PI-Internal-Token` | 结构化业务工具结果 | 仅供项目内 PI SDK runtime 调用；服务端按 `userId` 重解析账号权限，业务写操作不得从此接口直接执行。 |

## 2. 助理普通接口

### 2.1 `GET /api/dashboard/assistant-context`

#### 请求

- Query：`date?`、`brand?`。
- 多品牌用户必须明确指定 `brand`；品牌不在授权范围返回 `403`。

#### 成功响应

返回助理上下文；完整 schema 当前未固化，调用方应忽略未知字段。

### 2.2 会话接口

| 方法 | 路径 | 请求 | 成功响应 | 错误 |
| --- | --- | --- | --- | --- |
| GET | `/api/assistant/sessions` | 无 | `{items:[...]}` | 当前用户范围外不返回。 |
| PATCH | `/api/assistant/sessions/{session_meta_id}` | `title?`,`pinned?` | 更新后的元数据 | `404` 不存在，`403` 越权。 |
| DELETE | `/api/assistant/sessions/{session_meta_id}` | 无 | `status=deleted,sessionId` | `404` 不存在，`403` 越权。 |

所有会话接口需 Bearer，并只操作当前用户会话。

### 2.3 动作准备与确认

| 方法 | 路径 | 请求/状态 | 成功响应 | 错误与重试 |
| --- | --- | --- | --- | --- |
| POST | `/api/assistant/actions/prepare` | `sessionId`,`action`；服务端回读真实对象归属 | 待确认动作 | 权限或对象不匹配返回 4xx。 |
| POST | `/api/assistant/actions/{action_request_id}/confirm` | 仅 path `action_request_id`；服务端重新校验权限、归属、状态和有效期 | 执行或既有终态 | 并发 claim 可返回 `409`；重复确认不得重复外写。 |
| POST | `/api/assistant/actions/{action_request_id}/cancel` | 仅 path `action_request_id` | 取消结果 | 已终态按既有状态返回。 |

`prepare` 不信任客户端提交的 `actionId`、`taskId`、品牌或 Query；`confirm` 任一授权快照漂移、动作过期或对象状态改变时 fail closed。动作确认的状态机、幂等身份、并发 claim 和未知结果见[04 内部写入与执行契约](04-内部写入与执行契约.md)。

### 2.4 `POST /api/assistant/chat`

#### 请求

必填 `message`；可选 `sessionId?`,`date?`,`brand?`,`agentBindingId?`,`pageContext?`,`retryOfMessageId?`。多品牌用户不选 `brand` 返回 `400`。

#### 成功响应

返回 JSON 会话结果，并可能持久化消息和提议动作；“对话”不是幂等读取。

#### 错误与重试

空 `message` 返回 `400`；无权品牌返回 `403`。只有用户明确重试时才使用 `retryOfMessageId`，不得自动重放 POST。

#### 调用示例

```bash
curl --fail-with-body -X POST \
  'https://ops.example.test/api/assistant/chat' \
  -H 'Authorization: Bearer <issued-jwt>' \
  -H 'Content-Type: application/json' \
  -d '{"message":"<question>","brand":"atlas"}'
```

## 3. SSE 业务边界

`POST /api/assistant/chat/stream` 使用通用 SSE 协议；事件帧、事件类型、连接错误和断线处理统一见[03 通用协议、错误与流式传输](03-通用协议与错误契约.md)。本页只补充助理语义：`done` 事件会携带 `sessionId`、`proposedActions`、`toolResults` 和运行时摘要，服务端随后持久化会话；流式对话不是幂等读取，断线不得自动重放。

## 4. 非外部边界

助理流接口均需受控用户、品牌和动作权限，不能因为返回 200 或使用 GET 上下文就被纳入外部 allowlist。
