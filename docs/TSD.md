# TSD 总纲

| 字段 | 日期 |
| --- | --- |
| 创建时间 | 2026-07-14 |
| 最后更新时间 | 2026-10-15 |

> 本文档为技术方案设计（TSD）导航页。目标是让开发者和 Agent 都能快速理解 AI OPS 项目的技术实现。

2026-10-07：图片缩略解码与共享并发预算见[Query 治理](TSD/features/01-Query治理.md)；有界 checkpoint 恢复、leader 失效排空和进程健康探针见[调度与中间件](TSD/06-调度与中间件.md)。代码验证与生产发布、历史补跑分别验收。

2026-10-15：主治理品牌锁改为有界等待（默认 1800s，env 可调），避免被断码巡检占锁时品牌当天静默缺失；异常跳过原因以 `skippedBrands` 写入 job 终态摘要。见[调度与中间件](TSD/06-调度与中间件.md)。

2026-10-15：候选提取 SQL 的历史指标聚合压平为基表单层 GROUP BY，规避 MySQL 8 磁盘临时表对 ci 派生表二段聚合的伪 1062（`selection.py` docstring 记录约束与口径影响）。

## 1. 文档目的

TSD 主要回答这些问题：

- 系统整体由哪些运行面和模块组成
- 业务主链路如何流转
- 数据落在哪些表
- 后端暴露了哪些关键接口
- 调度器、缓存、外部依赖如何配合
- 出现问题时应该优先看哪里

约束：

- 所有内容以当前代码实现和现有文档事实为准
- 没有独立实现的能力明确标注“当前无独立实现”
- 不虚构未来态，不补不存在的中间件或错误码体系

## 2. 文档索引

- [01 整体技术架构](TSD/01-整体技术架构.md)
- [02 业务流程与状态流转](TSD/02-业务流程与状态流转.md)
- [03 数据存储设计](TSD/03-数据存储设计.md)
- [04 接口设计](TSD/04-接口设计.md)
- [05 功能模块设计索引](TSD/05-功能模块技术设计.md)
- [06 调度与中间件](TSD/06-调度与中间件.md)
- [07 运行、恢复与发布边界](TSD/07-兼容性灰度与运维.md)
- [08 联调测试与规范](TSD/08-联调测试与规范.md)

## 3. 文档说明

### 01 整体技术架构

- 文件：[整体技术架构](TSD/01-整体技术架构.md)
- 说明：描述 API、调度器、前端、本地治理库、外部搜索系统、知识图谱、PI Agent、生产只读源之间的关系。

### 02 业务流程与状态流转

- 文件：[业务流程与状态流转](TSD/02-业务流程与状态流转.md)
- 说明：描述 Query 治理、无结果词治理、智能助理三条主流程，以及核心状态机和动作执行模式决策。

### 03 数据存储设计

- 文件：[数据存储设计](TSD/03-数据存储设计.md)
- 说明：整理当前真实表结构、索引、环境差异，以及 Query 治理 / No Result / 助理 / 权限相关的数据边界。

### 04 接口设计

- 文件：[接口设计](TSD/04-接口设计.md)
- 说明：按接口域整理认证、配置、Query 治理、No Result、智能助理、运营日志等关键 API 契约。

### 05 功能模块设计索引

- 文件：[功能模块设计索引](TSD/05-功能模块技术设计.md)
- 说明：定位领域模块和 Query 治理扩展模块，再进入对应模块技术设计。

### 06 调度与中间件

- 文件：[调度与中间件](TSD/06-调度与中间件.md)
- 说明：说明 APScheduler 任务、中间件事实、缓存用法和当前未引入的组件。

### 07 运行、恢复与发布边界

- 文件：[运行、恢复与发布边界](TSD/07-兼容性灰度与运维.md)
- 说明：说明环境、受控业务动作、补跑恢复、健康排查、回滚与受控发布之间的边界。

### 08 联调测试与规范

- 文件：[联调测试与规范](TSD/08-联调测试与规范.md)
- 说明：说明联调对象、测试要点、日志/审计规范，以及当前可用的责任人/排期信息边界。

## 4. 与其他文档的关系

- 产品目标与功能边界：看 [PRD 总纲](PRD.md)
- 模块级技术设计：先看[功能模块设计索引](TSD/05-功能模块技术设计.md)，再进入对应 `features/` 页面。
- PRD 验收项到 TSD、API 和验证证据的反向映射：看 [联调测试与规范 ](TSD/08-联调测试与规范.md#8-prd-验收追踪矩阵)
- 本系统内部接口与治理库契约：看 [接口设计](TSD/04-接口设计.md) 和 [数据存储设计](TSD/03-数据存储设计.md)
- 本系统依赖的外部接口、第三方服务和生产只读数据源：看 [API 总纲](API.md)
- 生产发布、迁移、回滚和发布后验收：看 [生产发布指南](production_deployment_guide.md)
- 历史背景和已归档材料位于项目外归档；验收状态和限制仍以[联调测试与规范](TSD/08-联调测试与规范.md)为准。

冲突裁决顺序：同一层级选择更新、且有当前代码或验证证据支撑的口径；不得把互相矛盾的新旧模式混合实现。

## 5. 维护原则

- 结构优先于细节堆叠
- 先总览，再展开技术实现
- 变更代码后，优先更新最受影响的 TSD 子文档
- 若实现不存在，不补想象方案

## 6. 任务 Change Map

| 变更类型 | 必读文档 | 首要事实源 | 必查验证 |
| --- | --- | --- | --- |
| Query 治理流程/状态 | [02 流程](TSD/02-业务流程与状态流转.md)、[Query 治理](TSD/features/01-Query治理.md)、[08 联调](TSD/08-联调测试与规范.md) | `src/query_governance/workflow.py`、`read_api.py` | workflow/read API 定向测试 |
| 动作、幂等、外部写 | [04 接口](TSD/04-接口设计.md)、[07 运行边界](TSD/07-兼容性灰度与运维.md) | `src/governance/write_entry_registry.py`、`executors.py`、`services.py`、`policy.py` | ActionPlan、回读、回滚与脱敏 |
| Ops Team / 排序 | [Ops Team 与排序协作](TSD/features/05-Ops-Team与排序协作.md)、[02 流程](TSD/02-业务流程与状态流转.md)、[04 接口](TSD/04-接口设计.md) | `ops_team.py`、`ops_ranking.py`、QueryDetail | Ops Team API/页面/基线指纹 |
| A/B 实验 | [品牌 A/B 实验](TSD/features/04-品牌A-B实验.md)、[06 调度](TSD/06-调度与中间件.md)、[08 联调](TSD/08-联调测试与规范.md) | `experiment_service.py`、`experiment_analytics.py` | 40 样本、污染、质量和导出 |
| 调度/恢复 | [06 调度](TSD/06-调度与中间件.md)、[07 运行边界](TSD/07-兼容性灰度与运维.md)、[08 联调](TSD/08-联调测试与规范.md) | `src/scheduler/runner.py`、workflow run/lease | `start_scheduler()` 任务覆盖与重启恢复 |
| 生产发布 | [07 运行边界](TSD/07-兼容性灰度与运维.md)、[08 联调](TSD/08-联调测试与规范.md)、[生产发布指南](production_deployment_guide.md) | `scripts/release/release.py` | Release Gate、迁移、release identity、真实页面 |

## Query 看板读路径性能约束

Query rail 的 `filter=all` 读取必须使用批次摘要提供统计信息，并通过任务表的总数查询与 PV 排序的 `LIMIT/OFFSET` 读取当前页；不得先加载整个批次的任务、步骤或候选指标后再在 Python 中切页。筛选路径可以读取必要的步骤证据，但应通过观测持续收敛到投影查询。

Query 看板的热点只读接口支持 Redis JSON 共享缓存。运行时通过 `AIOPS_REDIS_URL` 注入连接地址，`AIOPS_REDIS_KEY_PREFIX`、`AIOPS_REDIS_CONNECT_TIMEOUT_SECONDS` 和 `AIOPS_REDIS_SOCKET_TIMEOUT_SECONDS` 为非敏感配置。Redis 连接失败时必须 fail-open，回退进程内短缓存和数据库读取；缓存 key 必须包含接口版本、品牌、日期、页码、页大小和筛选条件。

`current-result-count` 的同步数据库和外部 Mixed 调用必须运行在线程池可调度的同步路由中，并复用按品牌、Query 和展示条数隔离的短 TTL 共享缓存，不能阻塞单 Worker 事件循环。

Query 看板请求使用同步的 `get_current_user` 认证依赖。依赖内部使用同步 SQLAlchemy Session 查询用户和授权版本；FastAPI 会将同步依赖调度到线程池，避免同步数据库访问阻塞事件循环。`/api/dashboard/top-query/queries/{task_id}/workspace` 的 `includeTrend` 默认值为 `true` 以保持既有响应；传入 `includeTrend=false` 时必须下推到 `QueryGovernanceReadService.query_workspace(..., include_trend=False)`，直接跳过 `_query_metric_trend` 数据库读取并返回空 `trend` 数组。

Nginx 为每个 API 请求生成 `X-Request-ID` 并传给应用；应用只接受 32 位十六进制 id，直连请求无有效 id 时自行生成。应用在 `Server-Timing` 返回应用、认证和 workspace 服务分段耗时。仅当请求耗时达到 1000ms 或发生未处理异常时记录低敏日志；日志只包含 request id、HTTP method、路径和耗时，不记录 token、查询参数、请求体或异常详情。Nginx API access log 同样使用不含 query string 的 `$uri`，并记录 upstream 各阶段耗时与自身生成的 request id，以便未处理 500 也能跨层关联。API location 隐藏上游的同名响应头，由 Nginx 使用 `add_header ... always` 统一回传自身 request id，因此上游成功响应不会重复，5xx 也保留关联 ID。所有 API 启动入口均使用 Uvicorn `--no-access-log`，由 Nginx 负责 API access log，避免 Uvicorn 默认记录完整 query string。

## 独立品牌图片采集运行面

主应用通过独立的 `allow-main-image-worker-egress` 策略访问 Worker，不更改历史 Backend/Scheduler 出站策略的字段所有权。主发布渲染会保留 Worker 的 `aiops-image-sync` 标签选择器，移除 Kustomize 自动附加的主应用管理标签，避免独立 Worker 被主应用标签排除。

Atlas 图片采集 Worker 位于 `scripts/image_sync/`，独立于 Backend/Scheduler，
通过内网 Service 接收带 Bearer Token 的取图请求。使用完整 Chromium 的
`ozone-headless` 离屏模式；仅允许 Atlas 静态图域名，同品牌串行且间隔
1.5～2 秒。代理可选，是否可用以实际部署 Pod 的源站验证为准。

独立发布通过唯一入口 `scripts/release/release.py tke-image-worker`，绑定干净
独立 worktree、精确 SHA、linux/amd64 和不可变 TCR digest。分别执行构建、
专用 Secret 创建、三资源部署、至少十张真实图片验证与回滚；新建 Secret
须单独授权，不覆盖已有密钥。首次部署回滚仅将 Worker 副本数降为零。
详见 `scripts/image_sync/online/README.md` 与生产发布指南。

当前 Worker 发布不自动部署 COS 同步消费者，不修改主应用流量；源站验证、
COS 上传/HEAD、商品 Mapping 回读和相关性流程验收是不同关口。

### 图片完整性与错图防线

上游搜索索引的 `mainImage` 可能被批量写错（2026-09-29~10-13 Harbor 事故），
本服务无法阻止写入，防线分三层：

1. **判图守卫**（`src/query_governance/relevance.py`）：同一图片 URL 被
   ≥2 个商品使用时不下载、不送模型，降级为纯文本判断；候选携带
   `IMAGE_URL_SHARED` 告警，治理计划把自动拉黑降级为人工复核
   （`src/query_governance/image_integrity.py`、`relevance_remediation.py`）。
2. **显示优先级**（`src/query_governance/read_api.py` `_plan_products()`）：
   计划 `product_images` 是生成计划时经资产链路解析的图，统一覆盖快照
   回带的历史图字段，避免已修复的正确图被快照旧错图压过。
3. **每日巡检**（Scheduler job `query_governance_image_integrity_job`，
   20:30 cron）：只读扫描近 3 天任务快照的共享图分组、缺图比例与资产表
   自洽性，命中阈值即经 `src/runtime/notifier.py` 推送飞书告警。
   深度巡检（抽样与商品目录对图）用 `scripts/ops/audit_harbor_image_integrity.py`
   手工执行；存量修复用 `scripts/ops/repair_harbor_polluted_images.py`。

### 图片采集业务接线与小批验收

业务进程不嵌入 Chromium。`RemoteImageFetcher` 对 `static.atlas.example.test` 使用
`QG_BROWSER_BRIDGE_URL` / `QG_BROWSER_BRIDGE_TOKEN` 指向独立 Worker；其他品牌
保留原下载器。Worker 错误不回退 Atlas 直连，避免重复请求。自动、批处理和
人工相关性检测使用相同工厂，仍先复用商品 Mapping 和 COS 对象。

COS 配置由专用 `Secret/qg-image-assets` 注入，Token 引用现有 Worker Secret，
两端 NetworkPolicy 仅允许内网 8790。主应用镜像发布与 Worker 独立发布分开执行。
`tke-image-canary` 使用当前线上不可变 Backend 镜像运行独立 Job：最多读取
五页实时上架商品 List，验证十个商品的 COS 上传或复用、HEAD、公开图片
读取、正式 Mapping 回读及第二次零源站访问；不会启动全量同步或激活主应用。
