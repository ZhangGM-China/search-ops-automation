# Text-as-JSON Contracts

## 定义与权威边界

本表只列出源码可确认以 JSON 文本读写的字段。其物理类型仍为 TEXT 或 MEDIUMTEXT，而不是数据库原生 JSON；对象结构、版本、长度、兼容和脱敏由应用层负责。除 candidate_tags、batch_ids 已确认数组语义外，其余对象结构未在 ORM 中固定时均为待业务确认。

[字段目录](01-字段目录.md)关联的各领域目录与本表同步使用 Text-as-JSON 标记；未列入本表的 TEXT/MEDIUMTEXT 字段不得仅因类型为文本而假定可 JSON 解析。

| 表 | 字段 | 已确认语义 | 来源 |
|---|---|---|---|
| daily_abnormal_query | tracking_data | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| operation_log | request_payload | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| operation_log | response_payload | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| task_run_session | summary_data | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| no_result_governance_batch | summary_data | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| no_result_query | raw_payload | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| no_result_query | candidate_tags | 候选标签 JSON 数组 | src/persistence/models.py 及写入/读取方 |
| no_result_governance_action | request_payload | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| no_result_governance_action | response_payload | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| no_result_governance_action | before_snapshot | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| no_result_governance_action | rollback_payload | 回滚参数 JSON | src/persistence/models.py 及写入/读取方 |
| no_result_agent_event | payload | 事件结构化 JSON 载荷 | src/persistence/models.py 及写入/读取方 |
| no_result_retag_task | batch_ids | 待重新打标批次 ID 的 JSON 数组 | src/persistence/models.py 及写入/读取方 |
| no_result_retag_task | response_payload | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| brand_daily_metric | raw_payload | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| top_query_task | raw_payload | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| top_query_step_status | evidence | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| top_query_step_status | before_snapshot | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| top_query_step_status | after_snapshot | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| top_query_context_snapshot | payload | 事件结构化 JSON 载荷 | src/persistence/models.py 及写入/读取方 |
| query_governance_context_snapshot | payload | 批次共享事实和品牌上下文 JSON 载荷；MySQL/MariaDB 使用 `utf8mb4` / `utf8mb4_unicode_ci` 以支持 XHS emoji | src/query_governance/models.py 及写入/读取方 |
| top_query_agent_event | input_payload | Agent/工具输入 JSON | src/persistence/models.py 及写入/读取方 |
| top_query_agent_event | output_payload | Agent/工具输出 JSON | src/persistence/models.py 及写入/读取方 |
| top_query_action_plan | request_payload | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| top_query_action_plan | response_payload | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| top_query_action_plan | before_snapshot | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| top_query_action_plan | after_snapshot | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| top_query_workflow_run | checkpoint_payload | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| top_query_budget_ledger | detail_payload | 预算流水明细 JSON | src/persistence/models.py 及写入/读取方 |
| ops_team_run | summary_json | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| ops_team_message | content_json | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| ops_expert_ranking | ranking_json | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| ops_expert_ranking | evidence_json | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| ops_agent_prompt_version | model_config_json | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| ops_agent_decision_audit | input_json | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| ops_agent_decision_audit | output_json | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| ops_agent_decision_audit | evidence_json | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| ops_agent_decision_audit | tool_activity_json | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| ops_intelligence_snapshot | source_json | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| ops_intelligence_snapshot | content_json | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| ops_visual_review_cache | review_json | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| agent_experience_entry | evidence_json | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| top_query_score | dimension_scores | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| top_query_score | evaluation_detail | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| top_query_score | before_snapshot | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| top_query_score | after_snapshot | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| top_query_experiment_daily_metric | raw_payload | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| users | page_permissions | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| users | brand_permissions | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| users | action_permissions | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| assistant_agent_binding | available_models_json | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| assistant_session_meta | message_log | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| assistant_session_meta | proposed_actions_snapshot | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| assistant_action_request | payload_json | 结构化 JSON 文本；对象结构待业务确认 | src/persistence/models.py 及写入/读取方 |
| action_intent | payload_json | 非敏感请求保存脱敏有界 canonical 事实；敏感请求保存版本化 scrypt envelope（脱敏摘要、随机 salt、固定参数和 verifier），不保存原始 secret | src/governance/action_gateway.py、src/persistence/models.py |
| action_attempt | response_summary | 脱敏审计摘要；字符数和 UTF-8 bytes 均不超过 4096 | src/governance/action_gateway.py、src/persistence/models.py |
| action_fence_evidence | evidence_payload | 脱敏 fence 证据摘要；fact_hash 指向截断前完整脱敏 canonical fact | src/governance/action_gateway.py、src/persistence/models.py |
| action_readback | observed_payload | 脱敏审计摘要；fact_hash 指向截断前完整脱敏 canonical fact | src/governance/action_gateway.py、src/persistence/models.py |
| action_terminal_fact | result_json | 脱敏审计摘要；字符数和 UTF-8 bytes 均不超过 4096 | src/governance/action_gateway.py、src/persistence/models.py |
| action_terminal_fact | replay_result_json | 完整脱敏 canonical 业务响应；MySQL/MariaDB 使用 MEDIUMTEXT | src/governance/action_gateway.py、src/persistence/models.py |

## 非 JSON 文本

例如 error_message、reason、content、last_message_preview、product_id 和 final_strategy 并非在所有路径都以 JSON 读写，不能归为 Text-as-JSON。final_strategy 在现有调用中可保存可读策略文本或 JSON，属于 Text-or-JSON 兼容字段，消费方必须按来源处理。

operation_log 的请求/响应载荷可能含敏感信息：成功记录仅保留允许的读回动作并有字符上限，错误诊断也有上限；具体以 src/persistence/models.py 的脱敏与保留策略为准。
