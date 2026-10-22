# 07 No-Result 接口

| 字段 | 日期 |
| --- | --- |
| 创建时间 | 2026-07-23 |
| 最后更新时间 | 2026-08-18 |

> 本页详述 2 个 `external-read` GET，并索引 9 个内部执行与回滚接口。外部 GET 均需 Bearer，并按服务端 `brandPermissions` 判定数据范围。

## 1. 接口索引

| 方法 | 路径 | 鉴权 | 用途 |
| --- | --- | --- | --- |
| GET | `/api/dashboard/no-result-governance` | Bearer；`brand` 受授权范围约束 | No-Result 列表 |
| GET | `/api/dashboard/no-result-governance/queries/{query_id}/execution` | Bearer；Query 所属品牌 | 执行状态读取 |

通用请求、响应和错误见[03 通用协议、错误与流式传输](03-通用协议与错误契约.md)；内部写接口的执行契约见[04 内部写入与执行契约](04-内部写入与执行契约.md)。

## 2. `GET /api/dashboard/no-result-governance`

### 请求参数

| 参数 | 位置 | 类型 | 必填 | 规则 |
| --- | --- | --- | --- | --- |
| `date` | query | string | 否 | `YYYY-MM-DD`；按业务日筛选。 |
| `brand` | query | string | 否 | 仅能传 token 授权品牌；无权品牌当前返回 `200 {total:0,items:[]}`，不能把空结果当成授权成功证明。 |
| `query_text` | query | string | 否 | 搜索词包含匹配。 |
| `status` | query | string | 否 | 精确状态过滤；枚举未由 DTO 固化。 |
| `page` | query | integer | 否 | 默认 1，最小 1。 |
| `limit` | query | integer | 否 | 默认 20，范围 1–100。 |

### 成功响应

返回 `{total,items}`；`total` 是过滤后的总数，`items` 是当前页，没有 `page/pageSize` 回显。

`items[]` 稳定字段：

```text
id, batchId, dataDate, brand, query, status,
aiDecision, aiReason, finalStrategy,
beforeResultCount, afterResultCount, searchPv, searchUv,
executionStatus, actions, agentTimeline, createdAt, processedAt
```

- `dataDate` 为 `YYYY-MM-DD`；`createdAt/processedAt` 是服务端本地时间字符串，缺失时为空字符串。
- `searchUv、aiDecision、aiReason、finalStrategy、processedAt` 可为 `null` 或空字符串。
- `executionStatus` 可能为 `idle/running/paused/completed/blocked`，仅读取状态，不授予执行权限。
- `actions[]` 和 `agentTimeline[]` 的 payload 内容属于治理扩展区。

### 错误与重试

400 表示参数或服务前置条件无效；403 表示品牌越权；404 表示对象不存在。读请求按[03](03-通用协议与错误契约.md)的有限重试规则处理。

### 调用示例

```bash
curl --fail-with-body \
  'https://ops.example.test/api/dashboard/no-result-governance?brand=atlas&date=2026-07-23&page=1&limit=20' \
  -H 'Authorization: Bearer <issued-jwt>'
```

## 3. `GET /api/dashboard/no-result-governance/queries/{query_id}/execution`

### 请求参数

| 参数 | 位置 | 类型 | 必填 | 规则 |
| --- | --- | --- | --- | --- |
| `query_id` | path | integer | 是 | 服务端先按 Query 所属品牌校验。 |

### 成功响应

返回 `{queryId,status,executedActions,pendingActions,actions}`。`actions[]` 含 `id,status,errorMessage,updatedAt`；`updatedAt` 缺失时为空字符串。

`status` 可能为 `idle/running/paused/completed/blocked`，由执行状态推导。

### 错误与重试

- `400`：服务层无法读取执行进度。
- `403`：无品牌权限。
- `404`：Query 不存在。
- 读取失败按 GET 有限退避，不能调用对应写接口。

## 4. 内部接口索引

| 方法 | 路径 | 用途 | 主要边界 |
| --- | --- | --- | --- |
| POST | `/api/dashboard/no-result-governance/batches/{batch_id}/rollback` | 回滚批次 | 需治理权限；可能写知识图谱。 |
| POST | `/api/dashboard/no-result-governance/queries/{query_id}/rollback` | 回滚 Query | 按 Query 品牌校验；可能写知识图谱。 |
| POST | `/api/dashboard/no-result-governance/brands/{brand}/dates/{date}/rollback` | 回滚品牌业务日 | `date` 为 `YYYY-MM-DD`；需品牌与治理权限。 |
| POST | `/api/dashboard/no-result-governance/queries/{query_id}/execution/start` | 启动 Query 执行 | body 可含 `reason`；可能外写。 |
| POST | `/api/dashboard/no-result-governance/queries/{query_id}/execution/pause` | 暂停 Query 执行 | body 可含 `reason`；不代表已回滚既有动作。 |
| POST | `/api/dashboard/no-result-governance/queries/{query_id}/execution/resume` | 恢复 Query 执行 | body 可含 `reason`；可能外写。 |
| POST | `/api/dashboard/no-result-governance/actions/{action_id}/approve` | 审批动作 | 需动作所属品牌和治理权限。 |
| POST | `/api/dashboard/no-result-governance/actions/{action_id}/execute` | 执行待处理动作 | 可能外写；未知结果不得盲目重放。 |
| POST | `/api/dashboard/no-result-governance/actions/{action_id}/reject` | 拒绝动作 | body 必须含非空 `reason`。 |

以上均为 `internal` 写操作；每条接口的请求字段、状态前置、幂等身份、权威回读和未知结果处理见[04 内部写入与执行契约](04-内部写入与执行契约.md)。外部调用方不得因本页提供进度读取而调用这些写接口。
