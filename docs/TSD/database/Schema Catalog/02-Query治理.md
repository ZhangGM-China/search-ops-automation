# 字段目录：Query 治理

| 字段 | 日期 |
| --- | --- |
| 创建时间 | 2026-07-23 |
| 最后更新时间 | 2026-09-24 |

> 本目录只记录新 Query 治理数据库目标结构，来源为 `src/query_governance/models.py` 与保留的共享模型。不代表任一环境的真实库表结构。旧 `top_query_*`、预算、评分和 Ops Team 表按数据库重构计划删除，不迁移历史数据。

## 当前表

| 表 | 用途 | 关键字段与约束 |
|---|---|---|
| `query_governance_selection_batch` | 品牌一批 Query 的提取与筛选结果 | `batch_id`、`brand`、`data_date`、`config_json`；`batch_id + brand` 唯一 |
| `query_governance_candidate` | 候选 Query 的规则命中和排序证据 | `selection_batch_id`、`query_key`、`query_text`、`matched_rules_json`、`metrics_json`、`selected`；批次内 Query 唯一 |
| `query_metric_snapshot` | Query 按日真实行为指标快照，供详情抽屉和趋势读取 | `brand + query_text + data_date` 唯一；PV、UV、点击 UV、ATC UV、购买 UV、CTR、CVR；由候选日按配置历史/后续 N 日窗口幂等写入 |
| `query_governance_task` | 单个 Query 的治理任务和 S0 | `batch_id`、`brand`、`data_date`、`query_text`、`status`、`s0_snapshot`、错误和时间字段 |
| `query_governance_step_status` | 固定节点状态和结果 | `task_id`、`node_key`、`status`、`result_json`、错误、耗时；任务与节点唯一 |
| `query_governance_context_snapshot` | 批次共享事实和品牌上下文 | `batch_id`、`brand`、`context_type`、`payload`；批次与上下文类型唯一；MySQL/MariaDB 表及 `payload` 使用 `utf8mb4` / `utf8mb4_unicode_ci`，支持 XHS emoji |
| `query_governance_agent_event` | 模型/Agent 调用、Token 和异常 | `task_id`、`agent_name`、`node_key`、模型、Token、耗时和错误字段 |
| `query_governance_action_plan` | 节点动作、幂等键、实际执行来源和执行结果 | `task_id`、`node_key`、`action_type`、`idempotency_key`、标准 `status`、`executor_type`、`executor_user_id`、`executed_at`、`failure_stage`、请求/响应 JSON |
| `query_governance_workflow_run` | 批次运行和 Checkpoint | `batch_id`、`status`、`current_node`、`checkpoint_payload`、`checkpoint_version`、心跳和租约字段 |
| `query_governance_async_task` | KG Agent 异步任务 | `task_id`、`trigger_source`、`idempotency_key`、`status`、证据/方案 JSON、异常字段 |
| `query_governance_manual_relevance_check` | 外部调用方提交的手工相关性检测任务 | `check_id` 主键；`brand + idempotency_key` 唯一；请求/结果 JSON、状态和异常字段；不关联治理任务或动作表 |
| `query_governance_experiment` | A/B 实验生命周期 | `brand`、`experiment_name`、`status`、计划日期和创建人 |
| `query_governance_experiment_sample` | 实验固定样本与分组 | `experiment_id`、`query_text`、`group_type`、样本指标；实验与 Query 唯一 |
| `query_governance_experiment_daily_metric` | 实验每日观测和治理结果 | `experiment_id`、`query_text`、`data_date`、分组、指标、治理任务、动作、Token 和耗时 |
| `query_governance_relevance_record` | 商品与 Query 的永久相关性通过记录 | `brand`、`query_text`、`product_id`、`relevance`、模型和检测类型；三者唯一 |
| `query_governance_relevance_guidance` | 品牌与 Query 当前相关性规则 | `brand`、`query_text`、`rules_text`、`status`、维护人和时间；品牌与 Query 唯一 |

## 保留的共享表

用户、断码追踪、No Result、智能助理、动作审计和节点模型配置等共享表按数据库重构范围保留或迁移；其字段目录分别见其他 Schema Catalog 页面。
