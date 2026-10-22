# API 路由全量清单

本清单由 `src/api/app.py` 实际装配的路由模块 AST 提取，当前 **108 operations**。`exposure` 是发布分类：`external-read` 为外部只读 allowlist，`external-compute` 为不产生外部业务写入的外部计算接口，`internal` 为受控管理端，`deprecated` 为待退役兼容入口。`来源` 标记路由装饰器的文件和行号；`src/api/app.py` 只负责装配。

| # | method/path | 处理函数 | exposure | 认证/品牌边界 | 主要副作用 | 来源 |
| ---: | --- | --- | --- | --- | --- | --- |
| 1 | GET `/api/health/live` | `liveness_check` | internal | 无 Bearer；无品牌 | 进程存活探针 | `src/api/routers/health.py:L26` |
| 2 | GET `/api/health/startup` | `startup_check` | internal | 无 Bearer；无品牌 | 启动状态探针 | `src/api/routers/health.py:L34` |
| 3 | GET `/api/health/ready` | `readiness_check` | internal | 无 Bearer；无品牌 | 就绪状态探针 | `src/api/routers/health.py:L43` |
| 4 | GET `/api/health` | `health_check` | external-read | 无 Bearer；无品牌 | 健康/DB 探针读取 | `src/api/routers/health.py:L64` |
| 5 | POST `/api/auth/login` | `login` | internal | 匿名；无品牌 | 签发 JWT | `src/api/application/auth.py:L15` |
| 6 | GET `/api/auth/me` | `get_me` | internal | Bearer；当前用户 | 读取身份 | `src/api/application/auth.py:L40` |
| 7 | POST `/api/auth/logout` | `logout` | internal | Bearer；当前用户 | 撤销当前用户全部会话 | `src/api/application/auth.py:L78` |
| 8 | POST `/api/auth/change-password` | `change_password` | internal | Bearer；当前用户 | 修改密码 | `src/api/application/auth.py:L83` |
| 9 | GET `/api/admin/users` | `list_users` | internal | Bearer；角色/可管理品牌 | 读取账号 | `src/api/application/admin.py:L29` |
| 10 | POST `/api/admin/users` | `create_user` | internal | Bearer；管理员/品牌范围 | 创建账号 | `src/api/application/admin.py:L55` |
| 11 | PUT `/api/admin/users/{user_id}` | `update_user` | internal | Bearer；管理员/品牌范围 | 更新账号权限 | `src/api/application/admin.py:L145` |
| 12 | POST `/api/admin/users/{user_id}/reset-password` | `reset_password` | internal | Bearer；管理员/品牌范围 | 重置密码 | `src/api/application/admin.py:L231` |
| 13 | POST `/api/admin/users/{user_id}/revoke-sessions` | `revoke_user_sessions` | internal | Bearer；管理员/品牌范围 | 撤销用户会话 | `src/api/application/admin.py:L290` |
| 14 | DELETE `/api/admin/users/{user_id}` | `delete_user` | internal | Bearer；管理员/品牌范围 | 删除账号 | `src/api/application/admin.py:L338` |
| 15 | GET `/api/admin/assistant/access-config` | `get_assistant_access_config` | internal | 历史兼容接口 | 读取旧绑定配置；账号页不再调用 | `src/api/application/admin.py:L412` |
| 16 | POST `/api/admin/assistant/access-config` | `update_assistant_access_config` | internal | 历史兼容接口 | 维护旧绑定配置；不改变运行时账号品牌范围 | `src/api/application/admin.py:L451` |
| 17 | GET `/api/admin/node-model-config` | `get_node_model_config` | internal | Bearer；配置权限/品牌 | 读取节点模型配置 | `src/api/application/admin.py:L902` |
| 18 | POST `/api/admin/node-model-config/drafts` | `create_node_model_config_draft_api` | internal | Bearer；配置权限/品牌 | 创建节点模型草稿 | `src/api/application/admin.py:L922` |
| 19 | POST `/api/admin/node-model-config/versions/{version}/publish` | `publish_node_model_config_api` | internal | Bearer；配置权限/品牌 | 发布节点模型配置 | `src/api/application/admin.py:L978` |
| 20 | POST `/api/admin/node-model-config/rollback` | `rollback_node_model_config_api` | internal | Bearer；配置权限/品牌 | 回滚节点模型配置 | `src/api/application/admin.py:L1032` |
| 21 | GET `/api/admin/config` | `get_dashboard_config` | internal | Bearer；管理员/品牌范围 | 读取系统配置 | `src/api/application/admin.py:L1091` |
| 22 | GET `/api/admin/page-config` | `get_page_config` | internal | Bearer；当前用户 | 读取全局页面展示配置 | `src/api/application/admin.py:L1125` |
| 23 | POST `/api/admin/page-config` | `update_page_config` | internal | Bearer；平台配置权限 | 更新全局页面展示配置并按版本回读 | `src/api/application/admin.py:L1132` |
| 24 | POST `/api/admin/config` | `update_dashboard_config` | internal | Bearer；配置权限/品牌 | 写配置并 reload | `src/api/application/admin.py:L1185` |
| 25 | GET `/api/dashboard/top-query/brands/{brand}/kg-tag-details` | `get_query_governance_kg_tag_details` | internal | Bearer；路径品牌 | 只读查询知识图谱标签属性 | `src/api/application/query_governance.py:L143` |
| 26 | GET `/api/dashboard/top-query/brands/{brand}/kg-relationship-details` | `get_query_governance_kg_relationship_details` | internal | Bearer；路径品牌 | 只读查询知识图谱关系属性 | `src/api/application/query_governance.py:L169` |
| 27 | POST `/api/query-governance/relevance-checks` | `submit_manual_relevance_check` | external-compute | Bearer；请求品牌 | 创建只读商品详情/模型检测任务状态 | `src/api/application/query_governance.py:L210` |
| 28 | GET `/api/query-governance/relevance-checks/latest` | `get_latest_manual_relevance_check` | external-compute | Bearer；查询品牌 | 读取 Query 最近一次检测结果 | `src/api/application/query_governance.py:L246` |
| 29 | GET `/api/query-governance/relevance-checks/{check_id}` | `get_manual_relevance_check` | external-compute | Bearer；任务所属品牌 | 读取检测结果 | `src/api/application/query_governance.py:L264` |
| 30 | GET `/api/dashboard/top-query/overview` | `get_query_governance_overview` | external-read | Bearer；仅 `brandPermissions` | 读指标/任务 | `src/api/application/query_governance.py:L281` |
| 31 | GET `/api/dashboard/top-query/brands/{brand}` | `get_query_governance_brand_detail` | internal | Bearer；路径品牌 | 读聚合详情 | `src/api/application/query_governance.py:L309` |
| 32 | GET `/api/dashboard/top-query/brands/{brand}/summary` | `get_query_governance_brand_summary` | external-read | Bearer；路径品牌 | 读摘要 | `src/api/application/query_governance.py:L319` |
| 33 | GET `/api/dashboard/top-query/brands/{brand}/queries` | `get_query_governance_brand_queries` | external-read | Bearer；路径品牌 | 读 Query 列表 | `src/api/application/query_governance.py:L349` |
| 34 | GET `/api/dashboard/top-query/queries/{task_id}/workspace` | `get_query_governance_query_workspace` | external-read | Bearer；任务所属品牌 | 读详情工作区（详情/趋势/事件聚合） | `src/api/application/query_governance.py:L401` |
| 35 | GET `/api/dashboard/top-query/brands/{brand}/trend` | `get_query_governance_brand_trend` | external-read | Bearer；路径品牌 | 读趋势 | `src/api/application/query_governance.py:L428` |
| 36 | GET `/api/dashboard/top-query/brands/{brand}/metric-trend` | `get_query_governance_brand_metric_trend` | external-read | Bearer；路径品牌 | 读品牌运营指标逐日趋势快照 | `src/api/application/query_governance.py:L449` |
| 37 | GET `/api/dashboard/top-query/tasks` | `get_query_governance_tasks` | external-read | Bearer；brand 参数/授权范围 | 读任务列表 | `src/api/application/query_governance.py:L471` |
| 38 | GET `/api/dashboard/top-query/queries/{task_id}` | `get_query_governance_detail` | external-read | Bearer；任务所属品牌 | 读详情 | `src/api/application/query_governance.py:L481` |
| 39 | POST `/api/query-governance/actions/{action_id}/execute` | `execute_query_governance_kg_action` | internal | Bearer；治理权限/动作所属品牌 | 执行已确认的知识图谱治理动作并写回执行事实 | `src/api/application/query_governance.py:L497` |
| 40 | POST `/api/query-governance/kg-plans/{plan_id}/execute` | `execute_query_governance_kg_plan` | internal | Bearer；治理权限/动作所属品牌 | 通过兼容入口执行已确认的知识图谱治理动作并写回执行事实 | `src/api/application/query_governance.py:L555` |
| 41 | GET `/api/dashboard/top-query/queries/{task_id}/trend` | `get_query_governance_query_trend` | external-read | Bearer；任务所属品牌 | 读趋势 | `src/api/application/query_governance.py:L568` |
| 42 | GET `/api/dashboard/top-query/queries/{task_id}/current-result-count` | `get_query_governance_live_result` | external-read | Bearer；任务所属品牌 | 外部搜索只读/短缓存 | `src/api/application/query_governance.py:L588` |
| 43 | GET `/api/dashboard/top-query/brands/{brand}/products/{product_id}` | `get_query_governance_product_detail` | external-read | Bearer；路径品牌/可选任务品牌 | 外部商品读取/短缓存 | `src/api/application/query_governance.py:L635` |
| 44 | GET `/api/dashboard/top-query/brands/{brand}/relevance-guidance` | `get_relevance_guidance` | internal | Bearer；路径品牌 | 读取相关性纠正规则 | `src/api/application/query_governance.py:L739` |
| 45 | PUT `/api/dashboard/top-query/brands/{brand}/relevance-guidance` | `save_relevance_guidance` | internal | Bearer；路径品牌/治理配置权限 | 保存相关性纠正规则 | `src/api/application/query_governance.py:L755` |
| 46 | POST `/api/dashboard/top-query/brands/{brand}/relevance-guidance/append` | `append_relevance_guidance_rule` | internal | Bearer；路径品牌/执行治理动作权限 | 原子追加商品纠正规则并按序去重；写入前过滤 MySQL 三字节 `utf8` 不支持的补充平面字符 | `src/api/application/query_governance.py:L774` |
| 47 | POST `/api/dashboard/top-query/brands/{brand}/relevance-guidance/deactivate` | `deactivate_relevance_guidance` | internal | Bearer；路径品牌/治理配置权限 | 停用相关性纠正规则 | `src/api/application/query_governance.py:L793` |
| 48 | POST `/api/dashboard/top-query/brands/{brand}/relevance-guidance/organize` | `preview_relevance_guidance_organization` | internal | Bearer；路径品牌/治理配置权限 | 预览 AI 整理后的相关性纠正规则 | `src/api/application/query_governance.py:L813` |
| 49 | POST `/api/dashboard/top-query/brands/{brand}/relevance-guidance/confirm` | `confirm_relevance_guidance_organization` | internal | Bearer；路径品牌/治理配置权限 | 确认并保存 AI 整理后的相关性纠正规则 | `src/api/application/query_governance.py:L831` |
| 50 | GET `/api/dashboard/top-query/queries/{task_id}/events` | `get_query_governance_events` | internal | Bearer；任务所属品牌 | 读 Agent 事件 | `src/api/application/query_governance.py:L850` |
| 51 | GET `/api/dashboard/top-query/experiments/brands/{brand}` | `get_query_governance_experiment_history` | external-read | Bearer；路径品牌 | 读实验列表 | `src/api/application/query_governance.py:L864` |
| 52 | GET `/api/dashboard/top-query/experiments/brands/{brand}/sample-pool` | `get_query_governance_experiment_sample_pool` | external-read | Bearer；路径品牌 | 读候选池 | `src/api/application/query_governance.py:L885` |
| 53 | POST `/api/dashboard/top-query/experiments/brands/{brand}/draft` | `create_query_governance_experiment_draft` | internal | Bearer；路径品牌/配置权限 | 创建实验草稿 | `src/api/application/query_governance.py:L896` |
| 54 | POST `/api/dashboard/top-query/experiments/{experiment_id}/schedule` | `schedule_query_governance_experiment` | internal | Bearer；实验品牌/配置权限 | 迁移为计划状态 | `src/api/application/query_governance.py:L942` |
| 55 | POST `/api/dashboard/top-query/experiments/{experiment_id}/end` | `end_query_governance_experiment` | internal | Bearer；实验品牌/配置权限 | 结束实验 | `src/api/application/query_governance.py:L950` |
| 56 | POST `/api/dashboard/top-query/experiments/{experiment_id}/cancel` | `cancel_query_governance_experiment` | internal | Bearer；实验品牌/配置权限 | 取消实验 | `src/api/application/query_governance.py:L958` |
| 57 | GET `/api/dashboard/top-query/experiments/{experiment_id}` | `get_query_governance_experiment_detail` | external-read | Bearer；实验所属品牌 | 读实验详情 | `src/api/application/query_governance.py:L966` |
| 58 | GET `/api/dashboard/top-query/experiments/{experiment_id}/report` | `get_query_governance_experiment_report` | external-read | Bearer；实验所属品牌 | 读报告/快照 token | `src/api/application/query_governance.py:L982` |
| 59 | GET `/api/dashboard/top-query/brands/{brand}/snapshot/validate` | `validate_top_query_snapshot` | external-read | Bearer；路径品牌 | 读快照校验 | `src/api/application/query_governance_legacy.py:L467` |
| 60 | POST `/api/dashboard/top-query/brands/{brand}/snapshot/rebuild` | `rebuild_top_query_snapshot` | internal | Bearer；路径品牌/治理权限 | 重建快照 | `src/api/application/query_governance_legacy.py:L485` |
| 61 | GET `/api/dashboard/top-query/experiments/{experiment_id}/export` | `export_top_query_experiment` | external-read | Bearer；实验所属品牌 | 生成下载响应 | `src/api/application/query_governance_legacy.py:L783` |
| 62 | POST `/api/dashboard/top-query/experiments/{experiment_id}/export` | `export_top_query_experiment_snapshot` | internal | Bearer；实验所属品牌 | 读取指定快照下载 | `src/api/application/query_governance_legacy.py:L815` |
| 63 | POST `/api/dashboard/top-query/actions/{action_id}/approve` | `approve_top_query_action` | internal | Bearer；动作品牌/治理权限 | 更新审批状态 | `src/api/application/query_governance_legacy.py:L1299` |
| 64 | POST `/api/dashboard/top-query/actions/{action_id}/execute` | `execute_top_query_action` | internal | Bearer；动作品牌/治理权限 | 触发治理执行 | `src/api/application/query_governance_legacy.py:L1309` |
| 65 | POST `/api/dashboard/top-query/actions/{action_id}/reject` | `reject_top_query_action` | internal | Bearer；动作品牌/治理权限 | 更新拒绝状态 | `src/api/application/query_governance_legacy.py:L1323` |
| 66 | POST `/api/dashboard/top-query/tasks/{task_id}/execute-action` | `execute_top_query_task_manual_actions` | internal | Bearer；任务品牌/治理权限 | 批量治理执行 | `src/api/application/query_governance_legacy.py:L1333` |
| 67 | GET `/api/dashboard/top-query/tasks/{task_id}/execute-action-preview` | `preview_top_query_task_manual_actions` | internal | Bearer；任务品牌/治理权限 | 读执行预览 | `src/api/application/query_governance_legacy.py:L1385` |
| 68 | POST `/api/dashboard/top-query/actions/{action_id}/rollback` | `rollback_top_query_action` | internal | Bearer；动作品牌/治理权限 | 回滚治理动作 | `src/api/application/query_governance_legacy.py:L1398` |
| 69 | POST `/api/dashboard/top-query/tasks/{task_id}/mark-processed` | `mark_top_query_task_processed` | internal | Bearer；任务品牌/治理权限 | 标记任务 | `src/api/application/query_governance_legacy.py:L1588` |
| 70 | GET `/api/dashboard/no-result-governance` | `list_no_result_governance` | external-read | Bearer；brand 参数/授权范围 | 读 No-Result 列表 | `src/api/application/no_result.py:L23` |
| 71 | POST `/api/dashboard/no-result-governance/batches/{batch_id}/rollback` | `rollback_no_result_batch` | internal | Bearer；批次所属品牌/治理权限 | 回滚批次 | `src/api/application/no_result.py:L55` |
| 72 | POST `/api/dashboard/no-result-governance/queries/{query_id}/rollback` | `rollback_no_result_query` | internal | Bearer；Query 所属品牌/治理权限 | 回滚 Query | `src/api/application/no_result.py:L68` |
| 73 | POST `/api/dashboard/no-result-governance/brands/{brand}/dates/{date}/rollback` | `rollback_no_result_brand_date` | internal | Bearer；路径品牌/治理权限 | 回滚品牌日期 | `src/api/application/no_result.py:L81` |
| 74 | GET `/api/dashboard/no-result-governance/queries/{query_id}/execution` | `get_no_result_query_execution` | external-read | Bearer；Query 所属品牌 | 读执行状态 | `src/api/application/no_result.py:L228` |
| 75 | POST `/api/dashboard/no-result-governance/queries/{query_id}/execution/start` | `start_no_result_query_execution` | internal | Bearer；Query 品牌/治理权限 | 启动执行 | `src/api/application/no_result.py:L239` |
| 76 | POST `/api/dashboard/no-result-governance/queries/{query_id}/execution/pause` | `pause_no_result_query_execution` | internal | Bearer；Query 品牌/治理权限 | 暂停执行 | `src/api/application/no_result.py:L254` |
| 77 | POST `/api/dashboard/no-result-governance/queries/{query_id}/execution/resume` | `resume_no_result_query_execution` | internal | Bearer；Query 品牌/治理权限 | 恢复执行 | `src/api/application/no_result.py:L268` |
| 78 | POST `/api/dashboard/no-result-governance/actions/{action_id}/approve` | `approve_no_result_action` | internal | Bearer；动作品牌/治理权限 | 审批动作 | `src/api/application/no_result.py:L1589` |
| 79 | POST `/api/dashboard/no-result-governance/actions/{action_id}/execute` | `execute_no_result_pending_action` | internal | Bearer；动作品牌/治理权限 | 执行治理动作 | `src/api/application/no_result.py:L1603` |
| 80 | POST `/api/dashboard/no-result-governance/actions/{action_id}/reject` | `reject_no_result_action` | internal | Bearer；动作品牌/治理权限 | 拒绝动作 | `src/api/application/no_result.py:L1617` |
| 81 | GET `/api/dashboard/assistant-context` | `get_assistant_context` | internal | Bearer；助理绑定/品牌范围 | 读助理上下文 | `src/api/application/assistant.py:L488` |
| 82 | GET `/api/assistant/runtime-inspector` | `get_assistant_runtime_inspector` | internal | Bearer；当前用户账号 | 查看 PI Agent 运行时、权限、工作区、工具与事件 | `src/api/application/assistant.py:L644` |
| 83 | GET `/api/assistant/sessions` | `list_assistant_sessions` | internal | Bearer；当前用户 | 读会话 | `src/api/application/assistant.py:L668` |
| 84 | GET `/api/assistant/workspace` | `get_assistant_workspace` | internal | Bearer；当前用户工作区根目录 | 读取授权项目工作区目录 | `src/api/application/assistant.py:L686` |
| 85 | GET `/api/assistant/workspace/file` | `preview_assistant_workspace_file` | internal | Bearer；当前用户工作区路径 | 读取授权项目工作区文件预览 | `src/api/application/assistant.py:L699` |
| 86 | POST `/api/assistant/workspace/file` | `upload_assistant_workspace_file` | internal | Bearer；当前用户工作区路径 | 上传文件到授权项目工作区 | `src/api/application/assistant.py:L712` |
| 87 | POST `/api/assistant/terminal/sessions` | `create_assistant_terminal_session` | internal | Bearer；当前用户工作区 | 创建账号范围内终端会话 | `src/api/application/assistant.py:L727` |
| 88 | POST `/api/assistant/terminal/sessions/{terminal_session_id}/execute` | `execute_assistant_terminal` | internal | Bearer；终端会话及工作区 | 执行受控终端命令并返回输出 | `src/api/application/assistant.py:L735` |
| 89 | GET `/api/assistant/terminal/sessions/{terminal_session_id}` | `get_assistant_terminal_session` | internal | Bearer；终端会话及工作区 | 查询终端增量输出与状态 | `src/api/application/assistant.py:L745` |
| 90 | POST `/api/assistant/terminal/sessions/{terminal_session_id}/stop` | `stop_assistant_terminal` | internal | Bearer；终端会话及工作区 | 停止终端会话 | `src/api/application/assistant.py:L753` |
| 91 | POST `/api/assistant/sessions/{session_meta_id}/branches` | `branch_assistant_session` | internal | Bearer；当前用户会话 | 从历史消息创建会话分支 | `src/api/application/assistant.py:L761` |
| 92 | POST `/api/internal/assistant/tools/{tool_name}` | `execute_internal_assistant_tool` | internal | X-PI-Internal-Token；按 userId 重解析账号权限 | 调用账号范围内的 PI AIOPS 业务工具；业务写操作不在此接口执行 | `src/api/application/assistant.py:L785` |
| 93 | PATCH `/api/assistant/sessions/{session_meta_id}` | `update_assistant_session` | internal | Bearer；当前用户会话 | 更新会话元数据 | `src/api/application/assistant.py:L824` |
| 94 | DELETE `/api/assistant/sessions/{session_meta_id}` | `delete_assistant_session` | internal | Bearer；当前用户会话 | 删除会话 | `src/api/application/assistant.py:L850` |
| 95 | POST `/api/assistant/actions/prepare` | `prepare_assistant_action` | internal | Bearer；助理品牌范围 | 创建待确认动作 | `src/api/application/assistant.py:L1370` |
| 96 | POST `/api/assistant/actions/{action_request_id}/confirm` | `confirm_assistant_action` | internal | Bearer；动作品牌范围 | claim/执行确认动作 | `src/api/application/assistant.py:L1422` |
| 97 | POST `/api/assistant/actions/{action_request_id}/cancel` | `cancel_assistant_action` | internal | Bearer；动作品牌范围 | claim/取消动作 | `src/api/application/assistant.py:L1538` |
| 98 | POST `/api/assistant/chat` | `assistant_chat` | internal | Bearer；助理绑定/单品牌 | 模型调用并持久化会话 | `src/api/application/assistant.py:L1700` |
| 99 | POST `/api/assistant/chat/stream` | `assistant_chat_stream` | internal | Bearer；助理绑定/单品牌 | SSE 并持久化会话 | `src/api/application/assistant.py:L1762` |
| 100 | GET `/api/dashboard/blacklist-logs` | `list_blacklist_logs` | internal | Bearer；brand 参数/授权范围 | 读拉黑日志 | `src/api/application/dashboard.py:L27` |
| 101 | GET `/api/dashboard/demoted-logs` | `list_demoted_logs` | internal | Bearer；brand 参数/授权范围 | 读降桶日志 | `src/api/application/dashboard.py:L103` |
| 102 | GET `/api/dashboard/stock-tasks` | `list_stock_tasks` | internal | Bearer；brand 参数/授权范围 | 读断码任务 | `src/api/application/dashboard.py:L152` |
| 103 | POST `/api/dashboard/stock-tasks/{task_id}/toggle-status` | `toggle_stock_task_status` | internal | Bearer；任务品牌/治理权限 | 更新追踪状态 | `src/api/application/dashboard.py:L190` |
| 104 | POST `/api/dashboard/actions/cancel-blacklist` | `cancel_blacklist` | internal | Bearer；品牌/治理权限 | 调用撤销拉黑；`reason` 仅审计，`correctionRule` 才写相关性规则 | `src/api/application/dashboard.py:L652` |
| 105 | POST `/api/dashboard/actions/blacklist` | `blacklist_product` | internal | Bearer；品牌/治理权限 | 调用单商品拉黑并执行权威回读 | `src/api/application/dashboard.py:L683` |
| 106 | POST `/api/dashboard/actions/cancel-blacklist/batch` | `cancel_blacklist_batch` | internal | Bearer；品牌/治理权限 | 批量调用撤销拉黑并逐项返回结果 | `src/api/application/dashboard.py:L708` |
| 107 | POST `/api/dashboard/actions/blacklist/batch` | `blacklist_batch` | internal | Bearer；品牌/治理权限 | 批量调用拉黑并逐项返回结果 | `src/api/application/dashboard.py:L747` |
| 108 | POST `/api/dashboard/actions/cancel-demotion` | `cancel_demotion` | internal | Bearer；品牌/治理权限 | 调用撤销降桶 | `src/api/application/dashboard.py:L780` |

## 覆盖口径

本表逐行对应实际装配路由的 AST 提取；`method/path` 作为唯一键。运行 `venv/bin/python scripts/ci/generate_api_inventory.py` 可重新生成。未列入本表的前端历史 URL 不构成后端 operation。
