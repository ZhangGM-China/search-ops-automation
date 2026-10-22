# 06 Query 治理接口

| 字段 | 日期 |
| --- | --- |
| 创建时间 | 2026-07-23 |
| 最后更新时间 | 2026-09-22 |

> 本页详述 Query 治理的确定性读取接口，并索引 A/B 实验和受控写入路由。外部 GET 均需 Bearer；手工相关性检测的 POST/GET 也需 Bearer，并按任务品牌校验。HTTP 路径中的 `/top-query/` 为历史兼容命名，保持不变。

## 1. 接口索引

| 方法 | 路径 | 鉴权 | 用途 |
| --- | --- | --- | --- |
| GET | `/api/dashboard/top-query/overview` | Bearer；授权品牌 | 品牌总览 |
| GET | `/api/dashboard/top-query/brands/{brand}/summary` | Bearer；路径品牌 | 品牌摘要 |
| GET | `/api/dashboard/top-query/brands/{brand}/queries` | Bearer；路径品牌 | Query 列表 |
| GET | `/api/dashboard/top-query/brands/{brand}/trend` | Bearer；路径品牌 | 品牌趋势 |
| GET | `/api/dashboard/top-query/brands/{brand}/metric-trend` | Bearer；路径品牌 | 品牌运营指标逐日趋势（运营数据抽屉指标折线图） |
| GET | `/api/dashboard/top-query/tasks` | Bearer；授权品牌 | 任务列表 |
| GET | `/api/dashboard/top-query/queries/{task_id}` | Bearer；任务品牌 | Query 详情 |
| GET | `/api/dashboard/top-query/queries/{task_id}/workspace` | Bearer；任务品牌 | Query 详情工作区（详情、趋势、事件一次读取） |
| GET | `/api/dashboard/top-query/queries/{task_id}/trend` | Bearer；任务品牌 | Query 趋势 |
| GET | `/api/dashboard/top-query/queries/{task_id}/current-result-count` | Bearer；任务品牌 | 当前结果数 |
| GET | `/api/dashboard/top-query/brands/{brand}/products/{product_id}` | Bearer；品牌/任务范围 | 商品详情 |
| GET | `/api/dashboard/top-query/brands/{brand}/snapshot/validate` | Bearer；路径品牌 | 快照校验 |
| GET | `/api/dashboard/top-query/brands/{brand}/relevance-guidance` | Bearer；路径品牌 | 读取相关性纠正规则；响应始终带 `reasonHistory`（取消拉黑理由审计记录，只读，不参与规则内容），即使该 `brand + query` 还没有规则记录 |
| PUT | `/api/dashboard/top-query/brands/{brand}/relevance-guidance` | Bearer；路径品牌/治理配置权限 | 保存相关性纠正规则 |
| POST | `/api/dashboard/top-query/brands/{brand}/relevance-guidance/append` | Bearer；路径品牌/执行治理动作权限 | 原子追加商品纠正规则，按 1、2、3 顺序编号并去重；写入前过滤 MySQL 三字节 `utf8` 不支持的补充平面字符 |
| POST | `/api/dashboard/top-query/brands/{brand}/relevance-guidance/deactivate` | Bearer；路径品牌/治理配置权限 | 停用相关性纠正规则 |
| POST | `/api/dashboard/top-query/brands/{brand}/relevance-guidance/organize` | Bearer；路径品牌/治理配置权限 | 预览 AI 整理后的相关性纠正规则 |
| POST | `/api/dashboard/top-query/brands/{brand}/relevance-guidance/confirm` | Bearer；路径品牌/治理配置权限 | 确认并保存 AI 整理后的相关性纠正规则 |
| POST | `/api/query-governance/relevance-checks` | Bearer；请求品牌 | 提交手工相关性检测任务 |
| GET | `/api/query-governance/relevance-checks/latest` | Bearer；查询品牌 | 读取该 Query 最近一次手工相关性检测任务 |
| GET | `/api/query-governance/relevance-checks/{check_id}` | Bearer；任务所属品牌 | 读取手工相关性检测任务 |
| POST | `/api/query-governance/actions/{action_id}/execute` | Bearer；治理权限/动作所属品牌 | 执行已确认的知识图谱治理动作并写回执行事实 |
| POST | `/api/query-governance/kg-plans/{plan_id}/execute` | Bearer；治理权限/动作所属品牌 | 兼容入口；复用同一知识图谱治理执行、审计和回读链路 |

通用请求、响应和错误见[03 通用协议、错误与流式传输](03-通用协议与错误契约.md)；内部写接口的执行契约见[04 内部写入与执行契约](04-内部写入与执行契约.md)。

## 2. 手工相关性检测

### `POST /api/query-governance/relevance-checks`

请求只包含品牌、Query 和 SKU：

```json
{
  "brand": "atlas",
  "queries": [
    {"query": "白色连衣裙 女士", "products": ["SKU001", "SKU002"]}
  ]
}
```

同时传 `Idempotency-Key` 请求头。服务端按 SKU 调用品牌商品详情接口，取得名称、图片、品类、描述等模型判断所需字段，再复用现有图文相关性 Prompt、图片准备、模型运行时和结果解析；不会读取历史相关性结论，也不会创建拉黑、KG 或治理动作。Query 和 SKU 数量不设接口限制，内部按配置分片。成功返回 `202`：`{"checkId":"relchk_xxx","status":"queued"}`。

```bash
curl --fail-with-body \
  'https://ops.example.test/api/query-governance/relevance-checks' \
  -H 'Authorization: Bearer <issued-jwt>' \
  -H 'Idempotency-Key: manual-check-20260917-001' \
  -H 'Content-Type: application/json' \
  --data '{"brand":"atlas","queries":[{"query":"白色连衣裙 女士","products":["SKU001","SKU002"]}]}'
```

### `GET /api/query-governance/relevance-checks/latest`

按授权品牌和 Query 读取最近一次手工相关性检测任务。没有历史任务时返回 `404`；存在排队或执行中的任务时返回当前状态，页面可继续通过任务 ID 轮询。

请求参数：`brand`、`query`。

### `GET /api/query-governance/relevance-checks/{check_id}`

返回：

```json
{
  "checkId": "relchk_xxx",
  "brand": "atlas",
  "status": "completed",
  "queries": [
    {
      "query": "白色连衣裙 女士",
      "status": "completed",
      "products": [
        {"sku": "SKU001", "status": "completed", "isRelevant": true, "reason": null}
      ]
    }
  ]
}
```

`queries[]` 严格只包含 `query`、`status`、`products`；商品结果不返回 `detectionTypes`。该接口只读取内部检测任务状态和结果。

```bash
curl --fail-with-body \
  'https://ops.example.test/api/query-governance/relevance-checks/relchk_xxx' \
  -H 'Authorization: Bearer <issued-jwt>'
```

### GET `/api/dashboard/top-query/brands/{brand}/kg-tag-details`

受当前用户品牌权限保护的标签只读接口。精确匹配 `name` 或外部记录的 `tagName`，仅返回 `name/viewTag/isRoot/dimensions/weight/isEnglish/segmentingAgain/isCompose/nameAnd/nameNot`。属性缺失返回 null，不补默认值；404 表示目标不存在，502 表示外部读取异常或业务失败。

### GET `/api/dashboard/top-query/brands/{brand}/kg-relationship-details`

受当前用户品牌权限保护，通过 KG 客户端的只读 `snapshot` 按关系 ID 查询，并精确匹配结果。仅返回 `tagName/responseTagName/relationships`，用于只包含 ID 的更新或删除步骤补齐展示文案。该接口不执行删除动作，也不接受写操作；404/502 分别表示不存在与读取失败。

### `POST /api/query-governance/actions/{action_id}/execute`

受治理权限保护的 KG 原因组执行入口。请求体使用 `groupId` 指定待执行的原因组；服务端校验动作计划所属品牌、动作参数和步骤依赖，通过统一 KG 执行器逐步执行，写入后权威回读，并将执行状态、执行人、错误阶段、幂等键和回滚所需前置事实写回原治理方案。该入口只执行已生成的结构化方案，不接受自然语言脚本。

```json
{
  "groupId": "kg-group-1"
}
```

`POST /api/query-governance/kg-plans/{plan_id}/execute` 是历史客户端兼容别名，`plan_id` 按同一动作计划 ID 解析，并复用上述权限、执行、审计与权威回读链路；它不是第二套 KG 执行实现。

## 3. 品牌总览与 Query

### 3.1 `GET /api/dashboard/top-query/overview`

#### 请求

- Query：`date?`，ISO `YYYY-MM-DD`；这是页面监测日期。服务端按 `date - 1 天` 读取业务数据，未传时以服务端当天为监测日期并读取前一天业务数据；响应中的 `dataDate` 为实际业务数据日期。

#### 成功响应

返回 `{ "brands": BrandOverview[] }`。`brands[]` 稳定字段为 `brand,batchId,dataDate,status,queryCount,nodeStatusCounts,durationSeconds,startedAt,completedAt`。`durationSeconds` 表示当前治理执行尝试的有效墙钟耗时；中断恢复之间的等待时间不计入。`status` 只使用 `pending,running,executed,skipped,error`；`nodeStatusCounts` 按这五种状态计数。

### 3.2 `GET /api/dashboard/top-query/brands/{brand}/summary`

#### 请求

- Path：`brand`，必填且必须在授权范围。
- Query：`date?`，ISO 日期，表示监测日期；服务端按前一天业务数据日期读取当前 Query 治理主链，不接受历史 `querySource` 参数。

#### 成功响应

返回 `{brand,batch,summary}`。`batch` 为当前批次，无批次时为 `null`；`summary` 包含 `queryCount,queryStatusCounts,nodeStatusCounts`。

### 3.3 `GET /api/dashboard/top-query/brands/{brand}/queries`

#### 请求

- Path：`brand`，必填且必须在授权范围。
- Query：`date?`、`page?`（默认 `1`）、`pageSize?`（默认 `30`，最大 `100`）、`filter?`。`date` 为 ISO 日期，表示监测日期；服务端按前一天业务数据日期读取。`filter` 支持 `all`、`waiting_human_approval`、`fixed`、`normal`、`still_problematic`、`recall`、`result_drop`、`relevance`、`open_factor`、`inventory`。

#### 成功响应

返回 `{brand,dataDate,queries,total,page,pageSize,hasNext,filter,stats}`。服务端在数据库排序后分页，`queries[]` 只返回当前页的轻量 Query 卡片和步骤状态；详情节点、趋势、事件通过 `GET /api/dashboard/top-query/queries/{task_id}/workspace` 按需读取。`total` 和 `stats` 始终针对当前筛选条件对应的全量 Query，不是当前页数量。

`hasNext` 表示当前页之后是否仍有数据。

### 3.4 `GET /api/dashboard/top-query/brands/{brand}/trend`

#### 请求

- Path：`brand`，必填且必须在授权范围。
- Query：`date?`，ISO 日期，表示监测日期；服务端按前一天业务数据日期读取趋势。

#### 成功响应

返回 `{brand,dataDate,trend}`；`trend[]` 的统计维度为扩展区，无数据时可为空数组。Query 详情的 `trend[]` 从 `query_metric_snapshot` 按品牌和 Query 返回全部已持久化日期，字段包括 `pv,uv,clickUv,addToCartUv,purchaseUv,ctr,atc,cvr`；缺失源数据的日期不写入快照。详情响应同时返回 `historyDays`，供前端按品牌批次配置计算前 N 日加权基线。

### 3.5 `GET /api/dashboard/top-query/brands/{brand}/metric-trend`

#### 请求

- Path：`brand`，必填且必须在授权范围。
- Query：`startDate?`、`endDate?`，ISO 日期，表示监测日期；服务端换算为前一天业务数据日期。缺省为最近 7 天，服务端把窗口上限收敛到 180 天。

#### 成功响应

返回 `{brand,startDate,endDate,days}`；`days[]` 按日期升序逐日返回 `{date,batchId,metrics}`，窗口内每一天都有条目。`metrics` 是当天 `query_governance_batch_summary` 物化摘要投影出的运营指标：计数类为绝对值，比例类同时给出分子分母（`query_count/pass_count/pass_rate/no_recall_count/no_recall_rate/detected_product_count/irrelevant_product_count/relevance_accuracy/token_usage/duration_seconds/model_call_count` 等）。当天没有摘要，或摘要的 `qualityMetricsVersion` 落后于当前口径时，`metrics` 为 `null`，表示图上留空而不是伪造 0。前端按时间粒度（按天/按周/按月/按年）分桶后，计数类求和、比例类用分子分母重算。

### 3.6 `GET /api/dashboard/top-query/tasks`

#### 请求

- Query：`brand?`、`status?`。
- `brand` 不传时仅返回 token 授权品牌；传入无权品牌返回 `403`。
- 当前无 `page/pageSize`，返回完整集合。

#### 成功响应

返回 `{items,total}`；`items[]` 与 `brands/{brand}/queries` 的 Query 稳定字段相同。

## 4. 任务、实时结果与商品

### 4.1 `GET /api/dashboard/top-query/queries/{task_id}`

#### 请求

- Path：`task_id`，整数；按任务所属品牌校验。

#### 成功响应

稳定顶层为 `query,nodes,s0,actions,governancePlans,recall,inventoryExecution,slots,productDisplayLimit`。`inventoryExecution` 是独立断码任务的失败执行事实；失败时返回 `{available:false,source:"independent_job",status:"failed",errorCode,errorMessage}`，由 `action_intent` 和 `action_terminal_fact` 只读投影得到，不以缺少 `inventory_recovery_task` 推断成功。`slots.inventory` 同步返回 `available:false,source:"independent_job",status:"failed"`。`productDisplayLimit` 是当前品牌系统配置 `top_query_check_product_limit` 的有效值，表示详情页商品列表最多展示的前 N 个商品；实时结果接口使用同一配置请求 Mixed，并在响应中返回本次实际采用的 `productDisplayLimit`，前端据此统一限制实时召回和治理前快照两列，避免详情与实时请求命中不同实例时使用不一致的配置值。`s0` 是本次治理前快照；`actions` 是底层动作事实；`governancePlans` 是详情页可执行的治理方案投影，按 KG Agent、AI 重排序、拉黑商品、降桶商品、恢复降桶商品的固定顺序返回。一个 Query 在相关性复查轮次中产生的多条拉黑动作，在 `governancePlans` 中合成一条“错误商品”方案，商品按 ID 去重并保留逐件原因与状态；`actions` 仍逐条保留执行事实。每项包含原因、状态、执行模式、步骤、证据、商品和断码率指标；只有真实动作 ID 可进入执行链路，自动拉黑或降桶在可安全取消时返回 `cancelable=true`。旧 `timeline` 字段不再返回。

`recall` 提供稳定展示载荷：`currentResultCount,previousResultCount,comparisonStatus,conclusion,kgStrategy`。结论来自召回节点执行时持久化的事实，不被实时 Mixed 覆盖；`previousResultCount` 只表示前一日同品牌同 Query 结果。`kgStrategy` 无方案时为 `null`；有方案时包含逐条 `items` 和 `tags[]`，其中标签状态为 `exists` 或 `pending_create`，已有标签的 `resultCount` 通过 Mixed 查询，查询失败时为 `null`。

`slots` 为 KG 异步任务、共享事实、偏好、相关性、独立断码、模型配置和 A/B Test 的后续数据接入位。不再返回旧 `plannerPlan,timeline,stepStatuses,inventoryBrokenRateThreshold,teamTrace`。

### 4.2 `GET /api/dashboard/top-query/queries/{task_id}/workspace`

品牌详情页首屏使用的聚合读取接口，一次返回 Query 详情、持久化趋势和 Agent 事件，避免前端分别请求详情、趋势和事件并重复读取同一任务。实时 Mixed 结果不包含在该响应中，由前端在详情渲染后异步请求 `current-result-count`。

### 4.3 `GET /api/dashboard/top-query/queries/{task_id}/trend`

#### 请求

- Path：`task_id`，整数；按任务所属品牌校验。

#### 成功响应

返回 `{taskId,brand,queryText,trend}`。`trend[]` 含 `date,uv,clickUv,addToCartUv,purchaseUv,ctr,atc,cvr`；`addToCartUv/atc` 可为 `null`。

### 4.4 `GET /api/dashboard/top-query/queries/{task_id}/current-result-count`

#### 请求

- Path：`task_id`，整数；按任务所属品牌校验。

#### 成功响应

返回 `{taskId,status,resultCount,resultCountKnown,products,productDisplayLimit,errorCode,errorMessage}`。`productDisplayLimit` 是本次实时 Mixed 请求采用的品牌配置 `top_query_check_product_limit`，详情页用它与详情响应中的同名字段统一限制两列商品展示。结果使用按品牌、Query 和展示条数隔离的短 TTL 共享缓存；缓存未命中时读取实时 Mixed，搜索不可用时返回 `status:error,resultCount:null,resultCountKnown:false,products:[]` 及明确错误，不能把它当成零结果。

### 4.4 `GET /api/dashboard/top-query/brands/{brand}/products/{product_id}`

#### 请求

- Path：`brand`、`product_id` 必填；Query：`taskId`，可选整数。
- 任务必须属于同一品牌，否则返回 `400`。

#### 成功响应

返回 `{product,detailMeta}`；`detailMeta` 含 `detailSource,capturedAt,isSnapshot,taskId`。实时商品详情中的非空字段覆盖 `S0` 快照，读取失败时保留快照。未指定任务时可以独立读取实时详情。

通用业务品牌的商品列表接口不包含 `description` 时，使用其返回的记录 `id` 调用已有的 `showTag` 只读接口；仅在返回 ID 一致且描述为非空字符串时补齐描述，不覆盖价格、库存、标签等字段。已有描述、缺少记录 ID 或Summit品牌不触发补读。补读失败或返回无效描述时保留原详情及快照兜底；商品弹窗沿用既有描述展示位置。

若详情仍缺少描述且请求关联任务，则按任务所属品牌和 Query 调用同环境 Mixed（最多 100 件，单次尝试，沿用实时召回超时）。优先匹配完整商品 ID，再兼容逗号/分号组合 ID；无实时详情时补齐匹配商品字段，已有详情时仅补非空描述。召回失败或未匹配时保留原详情与 S0，不切换环境或扩大权限。未关联任务的请求不进行 Query 召回兜底。

#### 错误与缓存

`taskId` 缺失或 S0 中不存在该商品时返回 `404`。

### 4.5 `GET /api/dashboard/top-query/brands/{brand}/snapshot/validate`

#### 请求

- Path：`brand`，必填且必须在授权范围。
- Query：`date?`，ISO 日期；`limit?`，整数，用于限制比对范围。

#### 成功响应

至少包含 `brand,monitoringDate`；其余差异对象属于扩展区，结果缓存约 300 秒。

## 5. 调用示例

```bash
curl --fail-with-body \
  'https://ops.example.test/api/dashboard/top-query/brands/atlas/queries?date=2026-07-23' \
  -H 'Authorization: Bearer <issued-jwt>'
```

## 6. 内部接口索引

| 方法 | 路径 | 用途 | 主要边界 |
| --- | --- | --- | --- |
| GET | `/api/dashboard/top-query/brands/{brand}` | 读取品牌聚合详情 | 管理端兼容读取，不在外部 allowlist。 |
| POST | `/api/dashboard/top-query/brands/{brand}/snapshot/rebuild` | 重建指定品牌快照 | 需治理权限；可能创建任务。 |
| GET | `/api/dashboard/top-query/queries/{task_id}/events` | 读取 Agent 事件 | 按任务所属品牌校验。 |
| POST | `/api/dashboard/top-query/actions/{action_id}/approve` | 审批治理动作 | 需治理权限和动作品牌权限。 |
| POST | `/api/dashboard/top-query/actions/{action_id}/execute` | 执行治理动作 | 可能外写；执行前置、回读和未知结果见 04。 |
| POST | `/api/dashboard/top-query/actions/{action_id}/reject` | 拒绝治理动作 | 需治理权限。 |
| GET | `/api/dashboard/top-query/tasks/{task_id}/execute-action-preview` | 读取批量执行预览 | 不执行写入，但不在外部 allowlist。 |
| POST | `/api/dashboard/top-query/tasks/{task_id}/execute-action` | 批量执行任务动作 | 需治理权限；可能外写。 |
| POST | `/api/dashboard/top-query/actions/{action_id}/rollback` | 回滚治理动作 | 需治理权限和权威回读。 |
| POST | `/api/dashboard/top-query/tasks/{task_id}/mark-processed` | 标记任务已处理 | 只修改内部任务状态。 |

实验写入接口归入[09 A/B 实验与导出接口](09-A-B实验与导出接口.md)。Ops Team 接口已退役，仅在[10 Ops-Team 与专家排序接口（历史参考）](10-Ops-Team与专家排序接口.md)保留说明。所有内部写接口的请求字段、状态前置、幂等身份、权威回读和失败恢复见 04；本页只保留 Top Query 的业务语义和路由索引。
