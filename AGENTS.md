# AGENTS.md

## 项目概览

智能搜索运营系统，覆盖 Query 治理、No Result 的发现、诊断、治理、验证与效果追踪，并提供运营工作台和智能助理。当前 Query 治理代码位于 `src/query_governance/`；旧 `src/top_query/` 仅作历史参考，不在其中新增当前主线代码。

- Backend API：FastAPI，入口 `src/api.py`
- Scheduler：APScheduler，入口 `main.py`
- Frontend：React + TypeScript + Vite，入口 `frontend/src/App.tsx`
- Persistence：测试环境 SQLite，生产环境 MySQL
- External Systems：生产数据源数据库只读；搜索运营和知识图谱接口支持受控读写

核心代码位于 `src/`，详细架构以 `docs/TSD.md` 为准。

## 核心约束

- 禁止引入 LangChain、LangGraph 以外的 LLM 应用框架。
- 确定性路由、重试、计算和格式转换使用代码实现，仅在需要语义判断时调用模型。
- 不主动重构需求范围外的文件，不新增需求未要求的抽象层。
- 未经确认不得新增依赖。
- 变量、函数使用英文命名；表名使用下划线命名。
- 只在业务意图不明显时添加中文注释。

### Python 执行环境

- 测试、离线校验和项目 Python 脚本统一通过 `bash scripts/ci/run_python_offline.sh ...` 执行；该入口会优先使用当前项目或主目录共享 `venv`。
- 不要直接使用系统 `python3` 运行项目测试，否则可能因独立 Worktree 未安装依赖而出现误报。

## 安全边界

- 默认使用 `ENV=test`；切换 `ENV=prod` 必须获得显式授权。
- 除了 `docs/` 文件夹以外，其他被 Git 跟踪的文件不得保存真实密钥、Token、密码或数据库凭据。
- 不得修改密钥，除非获得显式授权，否则不执行数据库迁移、生产写入或生产发布。
- 不得绕过现有服务层、权限检查、动作审计、回读验证和回滚链路直接调用外部写接口。
- 未经授权不得删除文件、目录、worktree、分支或 Git 历史，也不得执行 `git push`、`git rebase`、`git reset --hard`。

## 工作区规则

- 项目主目录固定检出 `master`，本地常驻服务只能从主目录启动。
- 代码修改必须在独立分支和独立 worktree 中完成。
- 修改前检查 Git 状态；遇到来源不明的未提交改动、分支占用或合并冲突时停止并询问。
- 只读检查不受主目录未提交改动影响，但不得修改或覆盖这些改动。
- 生产发布和发布验证必须使用干净的独立 worktree。
- 生产发布唯一入口为 `python3 scripts/release/release.py`；按 `docs/production_deployment_guide.md` 分阶段执行，不得绕过入口直接运行旧脚本。
- 验证通过后，在项目主目录将变更本地合并至 `master`，并确认主目录、`master` 与本地服务代码一致。
- 主分支稳定后，主动清理已合并分支和临时 worktree。


# Documents
- PRD（产品需求文档）见 `docs/PRD.md` ，涉及到需求调整请先阅读产品需求文档。
- 接口文档见 `docs/API.md` ，文档内包含需要和外部业务系统对接的API接口、数据库、大模型接口/密钥等信息。
- 前端规范见 `docs/FESpec.md` ，设计规范见 `docs/DESIGN.md` ,进行前端页面开发时请先阅读这两个文档。
- 技术实施方案见 `docs/TSD.md` , 文档内包含该项目的技术架构、数据库设计、接口设计等信息.
- 生产发布规范 `docs/production_deployment_guide.md` ,项目需要发生产时请先阅读此文档。
- 开始任务时只检索与当前任务相关的 Memory。只有产生可复用的新坑点或长期决策时才更新。只读审查、普通问答、无新发现的任务不更新。
- PRD、TSD请随着项目需求、技术实现的变化而及时更新,保证文档的实时性、有效性;API.md、DESIGN.md非必要不更新.

# Rules
- 确定性转换、路由、重试由代码完成；仅在需要语义判断时调用模型。
- 新旧实现冲突时，以当前代码和最新验证证据为准，不混合互斥模式。
