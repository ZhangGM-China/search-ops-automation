# Query 治理双 Agent 实施方案

## 路由边界

- 召回数量为 0、低于阈值或相对前一日下降超过阈值：只投递 `recall_anomaly`，由 KG Agent 处理。
- 相关性检测得到错误商品：只投递 `relevance_root_cause`，由相关性治理 Agent 处理。检测节点只保存错误商品证据，不拉黑、不生成执行动作；相关性 Agent 自己决定 `individual`、`kg_issue` 或 `uncertain`。它不会调用 KG Agent，也不读取 KG Agent 方案。
- 两个任务共用异步任务、动作审计、受控执行和回读基础设施，但使用不同的 Agent 名称、Prompt、Trace 节点和持久化 `node_key`。

## Query 输入

工程代码先执行 `transform_governance_payload_query`。Atlas 的 `薄开衫 女士` 传给模型和查询接口的值只有 `薄开衫`；gender 不进入 Prompt、segments 或 KG 动作。

## KG Agent

System Prompt 约束：只读查询 `query_tag`、`query_relationship`、`search_products`；最多 12 次工具调用；工具结果返回后继续分析；不执行写接口；最终输出 `decision` 和可执行 `actions`。

User Prompt 最小格式：

```json
{"query":"薄开衫","recall":{"result_count":20,"previous_result_count":35,"drop_ratio":0.429}}
```

动作参数必须满足真实接口：标签动作包含 `name/viewTag/sceneId/dimensions/isEnglish/isRoot/segmentingAgain/isCompose/weight`，合成词再包含 `nameAnd/nameNot`；关系动作包含 `sceneId/relationships/tagId/tagName/dimensionNames/responseTagId/responseTagName`，更新或删除包含 `id`。`isEnglish` 不按 ASCII 自动推断，默认业务值为 `0`。

## 相关性治理 Agent

它接收同一 Query 的全部错误商品，并先通过只读 `product_detail` 获取完整商品资料（名称、类目、性别、材质、属性、当前标签等），必要时查询 KG 标签、关系和真实召回。每次运行最多 16 次工具调用。

最终格式：

```json
{"groups":[{"groupId":"kg:relation-too-wide","rootCause":"kg_issue","causeKey":"relation-too-wide","productIds":["p-1"],"confidence":0.92,"actions":[{"actionType":"delete_relationship","params":{"id":"rel-1"}}]}]}
```

`individual` 生成 `product_blacklist` 动作，参数包含 `product_ids`、`product_names`、`product_images`、`reasons`，由现有拉黑网关按品牌配置自动执行或等待人工执行，并回读状态。`uncertain` 不产生写动作，进入人工复核。`kg_issue` 的完整动作直接进入受控 KG 执行器。

## Langfuse

主 Query Trace 下分别建立 `query-governance.kg-agent` 和 `query-governance.relevance-agent` 两个 `agent` observation。每个模型调用记录 `generation`，每个只读接口记录 `tool`；输入、输出、Prompt 版本、模型、Token、耗时、错误、最终方案和执行回读均使用脱敏后的数据。异步任务持久化 Trace context，Worker 恢复到对应 Agent 节点。
