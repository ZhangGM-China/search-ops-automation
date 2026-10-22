# Query 看板响应优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Query rail 伪分页并接入 Redis 共享缓存，降低多 Pod 生产看板响应延迟。

**Architecture:** 读取服务对 `filter=all` 使用 SQL count 和 limit/offset，只加载当前页关联数据。API 层通过 Redis JSON 缓存跨 Pod 复用热点读取，Redis 故障时安全回退；请求和健康检查增加低敏耗时观测，前端保留取消过期请求的边界。

**Tech Stack:** FastAPI, SQLAlchemy, Redis Python client, React, TypeScript, pytest, Vitest。

**Spec:** `docs/query-response-optimization/design.md`

## Global Constraints

- 默认 `ENV=test`；不执行生产写入、发布或数据库迁移。
- Python 测试和脚本使用 `bash scripts/ci/run_python_offline.sh ...`。
- 不引入 LangChain、LangGraph 以外的 LLM 应用框架。
- Redis 连接由环境配置提供，代码必须在未配置或不可用时回退。
- 表名使用下划线命名，变量和函数使用英文命名。

### Task 1: 真正的 Query rail SQL 分页

**Files:**
- Modify: `src/query_governance/read_api.py`
- Test: `tests/test_query_governance_read_api.py`

- [x] 写失败测试：`filter=all` 只使用当前页任务及其步骤，不调用全量任务读取路径。
- [x] 运行该测试确认因现有实现全量读取而失败。
- [x] 增加任务 count 查询和当前页任务查询；`filter=all` 只加载当前页步骤和指标，保持返回字段与 total/hasNext 语义不变。
- [x] 对兼容回退路径按 task id 小批次读取步骤，避免生产 MySQL 大 IN 列表读取 `result_json` 的慢计划。
- [x] 运行目标测试并确认通过。
- [x] 运行现有 Query read API 测试并提交。

### Task 2: Redis 共享缓存适配器

**Files:**
- Modify: `requirements.txt`
- Create: `src/api/redis_cache.py`
- Modify: `src/api/shared.py`
- Test: `tests/test_redis_cache.py`
- Modify: `tests/test_query_governance_api_contract.py`

- [x] 写失败测试：配置 Redis 时 get/set 使用 JSON；未配置或异常时返回 miss 且不抛出业务异常。
- [x] 运行测试确认失败。
- [x] 增加 RedisCache 和统一 key 编码；API 读取先 Redis，再进程内缓存；写入 Redis 失败回退本地缓存。
- [x] 为 overview 和 brand queries 接入可配置 TTL、前缀和版本。
- [x] 为 current-result-count 接入共享缓存，并将同步外部调用移出 async 事件循环。
- [x] 运行缓存和 API contract 测试并提交。

### Task 2.1: Candidate lookup index and summary reliability

**Files:**
- Modify: `src/query_governance/models.py`
- Modify: `src/migrations/release_schema.py`
- Modify: `src/query_governance/scheduler.py`
- Test: `tests/test_prod_migration.py`, `tests/test_aiops_201_production_composition.py`

- [x] 为候选指标查询增加 `(batch_id, brand, query_text)` 复合索引，并纳入生产发布入口。
- [x] 部分 Query 失败时仍生成 overview 摘要，保留错误统计而不是跳过物化。

### Task 3: API 观测与健康检查隔离

**Files:**
- Modify: `src/api/lifecycle.py`
- Modify: `src/api/routers/health.py`
- Modify: `src/api.py` 或现有 middleware 文件
- Test: `tests/test_health.py`、现有 API middleware 测试

- [x] 复核现有契约：live 不触发数据库检查，ready 保留有界数据库检查。
- [x] 复用现有 `Server-Timing` 和健康检查隔离实现，本阶段不重复改动探针阈值。
- [x] 运行健康检查与 API contract 测试。

### Task 4: 前端请求边界

**Files:**
- Modify: `frontend/src/features/query-governance/pages/BrandDetail/hooks/useBrandDetailWorkspace.ts`
- Test: `frontend/src/features/query-governance/pages/BrandDetail/hooks/useBrandDetailWorkspace.test.tsx`

- [ ] 写失败测试：路由或页码变化会取消旧列表和详情请求，且不提交旧响应。
- [ ] 运行 Vitest 目标测试确认失败。
- [x] 现有 Hook 已通过 AbortController 在路由、页码和 Query 切换时取消过期请求；保持列表请求与详情请求解耦。
- [x] 运行目标 Vitest 和相关前端测试。

### Task 5: 配置、文档与验证

**Files:**
- Modify: `config.example.yaml` 或环境配置文档
- Modify: `deploy/kubernetes/base/configmap.yaml`（仅非敏感配置键）
- Modify: `docs/TSD.md`
- Test: 发布静态校验和相关测试

- [x] 增加 Redis URL、key prefix、connect/read/write timeout 和 TTL 的非敏感配置说明。
- [x] 明确 Redis Secret 只能通过运行时 Secret 注入。
- [x] 运行后端目标测试、前端目标测试、静态配置校验。
- [x] 汇总生产只读验收命令和回滚方式，不执行生产发布。

## 发布前置事项

- 新版本生成的批次摘要会包含 `stats`，从而启用 `filter=all` 的 SQL 真分页。
- 历史摘要若没有 `stats` 会继续走旧的全量兼容路径，避免统计口径改变；调度器每 5 分钟在最近 7 天窗口内幂等回填已完成但缺失的摘要，再观察接口耗时。
- `AIOPS_REDIS_URL` 必须通过运行时 Secret 注入；本阶段不创建 Redis 实例、不执行生产写入。
