# AIOPS TKE 生产发布指南

本指南定义 AIOPS 迁移到腾讯云 TKE 后的唯一生产发布流程。所有发布动作只能通过 `scripts/release/release.py` 执行，不得绕过入口直接运行 `kubectl apply`、手工替换镜像或修改本地发布状态。

真实密码、Token、数据库连接串和私钥不得进入 Git、Plane、日志、命令历史或发布状态。运行时敏感值仅通过 `Secret/aiops-runtime-secrets` 注入，发布脚本只回读 Secret 名称和键名。

Query 治理商品图片下载支持可选的专用代理出口。需要使用代理时，在
`Secret/aiops-runtime-secrets` 中注入 `QUERY_GOVERNANCE_IMAGE_PROXY_URL`，值为
`http://` 或 `https://` 代理 URL；Backend、Scheduler 和手工相关性任务会将其只用于商品图片下载。
当前 Backend、Scheduler 的 NetworkPolicy 允许 DNS 和 TCP 443 出站，生产代理需在 443 端口可达；未配置时保持直接访问。
代理 URL 可能包含凭据，只能通过 Secret 注入，不能写入配置文件、镜像、日志或命令行。

## 1. 固定目标

- TKE：`cls-demo0000`
- kube context：`cls-demo0000-000000000000-context-default`
- Namespace：`aiops-demo`
- IngressClass：`qcloud`
- TCR：`registry.example.test/aiops/backend`、`registry.example.test/aiops/frontend`
- 域名：`ops.example.test`
- HTTPS：由现有外部代理终止，TKE 只提供 HTTP 源站
- 数据库：继续使用现有生产 MySQL，不创建或迁移数据库实例

生产发布仅由 TKE Provider 承载。旧主机 SSH/helper/Compose 发布链已退役，仓库不再提供旧主机发布、接收、Compose 或启动入口。

## 2. 发布状态与硬门禁

TKE 主链固定为：

```text
tke-candidate
  -> tke-prepare
  -> tke-rehearse-pull
  -> [tke-init-runtime-data，仅普通 CFS 的 /runtime 尚未初始化或权限验收失败时]
  -> tke-rehearse-mysql
  -> tke-rehearse-deploy（内含 standby 验收并写入候选证明）
  -> [tke-rehearse-accept，可选的只读复验]
  -> tke-activate（内含 active 验收并写入活动槽状态）
  -> tke-accept（激活后的只读复核）
  -> tke-rollback (仅失败或人工回退时)
```

`tke-preview` 是可选的本地预览，不属于发布证据链：它只输出清单和阶段计划，不访问云侧、不写状态。

如果本地发布状态目录丢失，但集群仍有稳定公共 Service，禁止把空状态当作首次发布并默认选择 `green`。应在干净的独立发布 worktree 中，使用只读的 `tke-adopt-active` 从**已记录的旧状态目录**恢复本地活动槽证明；命令会回读稳定 Service、Ingress、活动槽验收和 Pod 镜像身份，只写新的本地状态文件，不 apply 或删除 Kubernetes 资源：

```bash
python3 scripts/release/release.py --state-dir "$STATE_DIR" --format json tke-adopt-active \
  --source-state-dir "$SOURCE_STATE_DIR" \
  --target-state-dir "$STATE_DIR" \
  --release-sha "$ACTIVE_RELEASE_SHA" \
  --kube-context "$KUBE_CONTEXT" \
  --execute \
  --confirm-release-sha "$ACTIVE_RELEASE_SHA"
```

若不存在可验证的旧活动状态，必须先停止发布并人工核对客户入口与回退边界；不得手工编辑 `tke-release-state.json`，也不得直接用 `kubectl apply` 替代状态恢复。

如果不存在任何可验证的旧状态目录，但 TKE 集群仍由本发布链管理并且公共入口可读，可使用
`tke-adopt-live` 做受控恢复。该命令只读回稳定 Service、Ingress、活动槽 ConfigMap、三份
Deployment 和活动验收，再在本地计算 manifest hash 并写入状态；不会 apply、删除或修改生产资源。
它要求 `--release-sha` 与活动槽 ConfigMap 完全一致，不能用来猜测或覆盖活动版本：

```bash
python3 scripts/release/release.py --state-dir "$STATE_DIR" --format json tke-adopt-live \
  --target-state-dir "$STATE_DIR" \
  --release-sha "$ACTIVE_RELEASE_SHA" \
  --kube-context "$KUBE_CONTEXT" \
  --execute \
  --confirm-release-sha "$ACTIVE_RELEASE_SHA"
```

发布采用同一 Namespace 内的 `blue` / `green` 双槽。候选永远部署到本地 release state 所记录活动槽的另一槽（首次为 `green`）。每个槽拥有以下对象：

| 对象 | 命名 |
| --- | --- |
| 工作负载与 PDB | `backend-{slot}`、`frontend-{slot}`、`scheduler-{slot}` |
| 槽内 Service | `api-{slot}`、`frontend-{slot}` |
| 发布身份 | `aiops-release-info-{slot}` |

候选不渲染稳定 `api`/`frontend` Service 或 Ingress，Scheduler 必须为 `standby`。激活时才重新渲染候选槽为 `active`，并将稳定 Service 与 Ingress 指向该槽；成功后才在本地状态中记录新的活动槽和不可变镜像身份。

`tke-preview`、`tke-prepare`、`tke-rehearse-accept` 和 `tke-accept` 为只读命令。所有实际写入阶段（candidate、临时演练、CFS 初始化、候选部署、激活、回滚和失败候选清理）均要求 `--execute` 与精确的 `--confirm-release-sha`。以下任一项缺失时失败关闭：

- `tke-candidate`、`tke-rehearse-pull`、`tke-rehearse-mysql`、`tke-rehearse-deploy`、`tke-activate`、`tke-accept` 和 `tke-rollback` 不在干净、独立的 Git worktree 中执行，或当前 `HEAD` 不等于 release SHA。
- `tke-init-runtime-data`、`tke-rehearse-accept` 与 `tke-cleanup-candidate` 不在干净、独立的发布工具 worktree 中执行。它们可以在候选 SHA 之后的工具提交上运行，但分别只消费已记录候选、已记录候选证明或精确指定的候选 SHA。
- kube context 未精确绑定 `cls-demo0000`。
- `ConfigMap/aiops-runtime-config` 缺失或 `config.yaml` 无法解析。
- `Secret/aiops-runtime-secrets` 缺少 `AUTO_DB_URI_PROD`、`SECRET_KEY`、`LANGFUSE_PUBLIC_KEY` 或 `LANGFUSE_SECRET_KEY` 键名。
- `PersistentVolumeClaim/aiops-runtime-data` 不是 AIOPS 专用、`Bound`、`ReadWriteMany`。
- Backend/Frontend 镜像不是由同一 release SHA 构建，或 TCR digest 与候选状态不一致。
- TKE cluster pull 证明缺失或漂移。
- 同一 SHA、digest、context 的 MySQL 只读连接演练证明缺失或漂移。
- 激活前没有同一 SHA、digest、context 的 standby 候选验收证明。
- Deployment、Pod imageID、健康探针、Service Endpoint、Frontend 路由或 Scheduler mode 验收失败。

`aiops-runtime-data` 不能用 CBS `ReadWriteOnce` 或单副本降级替代。Backend 双副本与 Scheduler 共享 `/app/data`，必须使用 CFS 或等价 RWX 存储；两个挂载该目录的 Pod 均以 UID/GID `10001` 运行，并固定声明 `fsGroup: 10001` 与 `fsGroupChangePolicy: OnRootMismatch`，否则运行态目录不可写时必须阻断候选验收。

## 3. 发布前准备

### 3.1 快速预检与阶段耗时

在运行完整测试或构建镜像前，先执行只读快速预检。它只检查本地发布物料、前端锁定依赖、Docker 命令和目标 TKE 运行时前置条件，不读取 Secret 值、不写 Kubernetes 资源：

```bash
python3 scripts/release/release.py --state-dir "$STATE_DIR" --format json tke-preflight \
  --release-sha "$RELEASE_SHA" \
  --kube-context "$KUBE_CONTEXT"
```

预检失败应先修复，不进入完整测试、镜像构建或 TCR 推送。发布入口会在同一 `STATE_DIR` 记录每个阶段的开始时间、结束时间、耗时和失败分类，文件为 `tke-stage-timings.json`；只包含阶段元数据，不包含配置值、Secret、Token 或业务 payload。

### 3.2 可选链路编排

为减少人工逐阶段复制命令，可以使用现有阶段的编排入口。默认只执行到无流量 standby 验收，不激活客户流量：

```bash
python3 scripts/release/release.py --state-dir "$STATE_DIR" --format json tke-pipeline \
  --release-sha "$RELEASE_SHA" \
  --schema-release 'zha33' \
  --kube-context "$KUBE_CONTEXT" \
  --through standby \
  --execute \
  --confirm-release-sha "$RELEASE_SHA"
```

只有明确指定 `--through activate` 才会继续执行 `tke-activate` 和 `tke-accept`。编排入口不改变任何阶段的精确 SHA、独立 worktree、临时 Pod 清理、MySQL 只读、standby 和激活门禁；任一阶段失败立即停止，不自动重试、不跳过阶段。

所有实际写入阶段以及 `tke-rehearse-accept`、`tke-accept` 都在独立 worktree 中执行。`tke-preview`、`tke-prepare` 可以只读查看，但不得以脏工作区的结果作为发布证据。为本次发布准备权限为 `0700` 的绝对状态目录，并在整条链路中复用同一目录：

```bash
RELEASE_SHA='<40-char-release-sha>'
KUBE_CONTEXT='cls-demo0000-000000000000-context-default'
STATE_DIR='<absolute-private-state-dir>'
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
```

`STATE_DIR` 保存候选、cluster pull、MySQL、standby 候选和活动槽的受限状态文件。不得在阶段之间更换、删除或复制其中内容；命令会拒绝不安全的目录、软链接或状态文件权限。

### 临时 Kubernetes API 访问配置

当前集群已有公网 API CLB，但控制台自定义域名尚未配置公网 DNS。不要修改 `/etc/hosts`、系统 DNS 或长期 kubeconfig；将经授权的 kubeconfig 复制到 owner-only 临时文件后，只覆盖本次发布的 server 与 TLS 校验名称：

```bash
KUBECONFIG='<absolute-owner-only-temporary-kubeconfig>'
kubectl --kubeconfig "$KUBECONFIG" config set-cluster cls-demo0000 \
  --server='https://load-balancer.example.test:443' \
  --tls-server-name='cluster.example.test'
export KUBECONFIG
kubectl --context "$KUBE_CONTEXT" --request-timeout=15s get --raw=/version
```

该 CLB 仅承载 Kubernetes 控制面访问，不是客户业务入口，不等同于后续应用 Ingress/CLB。临时 kubeconfig 不得提交、上传、打印或长期保留。

执行仓库与本地工具检查。`kubeconform` 是本地静态审阅工具，不是 `release.py` 的云侧准入证据：

```bash
git status --short
git rev-parse HEAD
docker version
kubectl --context "$KUBE_CONTEXT" version
kubectl kustomize deploy/kubernetes/base > /tmp/aiops-rendered.yaml
kubeconform -strict -summary -ignore-missing-schemas /tmp/aiops-rendered.yaml
```

静态校验不证明 TCR、TKE、MySQL 或客户入口可用，真实证据只能由后续阶段回读。

前端依赖必须在本次干净发布 worktree 内作为独立准备动作安装；发布 Gate 不会隐式安装或通过 `npx` 下载工具：

```bash
(cd frontend && npm ci --prefer-offline --no-audit --no-fund)
```

在真实执行 `tke-candidate` 前，需在同一干净 worktree 独立完成质量 Gate：

```bash
ENV=test bash scripts/ci/test_fast.sh
ENV=test bash scripts/ci/test_release_gate.sh
git diff --check
```

`test_fast.sh` 用于快速反馈，`test_release_gate.sh` 是完整本地发布质量证据；两者都必须以成功退出结束。它们不会被 `tke-candidate` 自动调用，也不会写入候选状态。除构建、推送和回读候选镜像外，`tke-candidate` 只校验候选工作区、构建上下文和镜像身份；缺失前端依赖时应先完成上述安装，不得把安装塞进 Gate。

## 4. 创建不可变候选

不带 `--execute` 时只生成候选计划；带精确确认后才构建 `linux/amd64` 镜像、校验 OCI 身份、获取短期 TCR 登录能力、推送 SHA tag、回读 registry digest，并在本地再次按 digest 拉取：

```bash
python3 scripts/release/release.py --state-dir "$STATE_DIR" tke-candidate \
  --release-sha "$RELEASE_SHA" \
  --schema-release 'zha33'

python3 scripts/release/release.py --state-dir "$STATE_DIR" tke-candidate \
  --release-sha "$RELEASE_SHA" \
  --schema-release 'zha33' \
  --execute \
  --confirm-release-sha "$RELEASE_SHA"
```

候选状态仅记录 release SHA、source tree SHA、`artifactManifestSha256`、Backend/Frontend digest 和 schema release；不记录 Registry 密码或 Token。字段名 `artifactManifestSha256` 沿用现有实现，但当前值是由 `releaseSha`、`sourceTreeSha` 和 `schemaRelease` 计算的候选身份哈希，并非完整制品文件的摘要，也不代表已执行数据库迁移。TCR 短期 Token 只在 owner-only 临时 `DOCKER_CONFIG` 中使用，命令结束时删除该目录，不写入用户默认 Docker 配置。API 与 Scheduler 使用同一个 Backend digest，Frontend 使用对应 Frontend digest。

本地按 digest 拉取不等于 TKE 已能拉取镜像；只有后续 `tke-rehearse-pull` 的临时 Pod 回读才产生 cluster pull 证明。

## 5. 只读准备检查

`tke-prepare` 不创建资源、不读取 Secret 值，只核对目标身份、ConfigMap、Secret 键名和 RWX PVC。实际发布权限和清单合法性由后续 `server-side dry-run` 与 `apply` 的真实结果验证，不提前枚举 RBAC：

```bash
python3 scripts/release/release.py --state-dir "$STATE_DIR" --format json tke-prepare \
  --release-sha "$RELEASE_SHA" \
  --kube-context "$KUBE_CONTEXT"
```

必须保存脱敏结果。它通过后仍不等于应用可运行，只表示部署前置资源满足声明契约。

## 6. TKE 镜像拉取演练

`tke-rehearse-pull` 创建 Backend/Frontend 临时 Pod，验证目标集群能拉取候选 digest，回读 imageID 后清理临时 Pod：

```bash
python3 scripts/release/release.py --state-dir "$STATE_DIR" tke-rehearse-pull \
  --release-sha "$RELEASE_SHA" \
  --kube-context "$KUBE_CONTEXT" \
  --execute \
  --confirm-release-sha "$RELEASE_SHA"
```

清理失败视为阶段失败，不能保留未登记的临时 Pod 后继续。

### 6.1 普通 CFS 运行目录初始化

当普通 CFS NFS 的根目录并非 `10001:10001`、`0770` 时，`tke-init-runtime-data` 使用已通过 cluster pull 的同一 Backend digest 创建一次性 `Job/aiops-cfs-runtime-init`。它只有一个 root init container：仅创建或校验 `/app/data/runtime`，设置 `10001:10001` 与 `0770`；随后由 UID/GID `10001` 的无特权 container 创建并删除随机文件进行写入自检。该 Job 不挂载 ConfigMap 或 Secret，不访问 MySQL，不创建 Service、Ingress、DNS/CLB 或客户流量：

```bash
python3 scripts/release/release.py --state-dir "$STATE_DIR" tke-init-runtime-data \
  --release-sha "$RELEASE_SHA" \
  --kube-context "$KUBE_CONTEXT" \
  --execute \
  --confirm-release-sha "$RELEASE_SHA"
```

无论成功或失败，发布入口都会删除该 Job。若 CFS root-squash 阻断 `chown` 或 `chmod`，该阶段会以 `ownership` 或 `mode` 失败关闭；不得用放宽 Pod 权限、改为 root 运行应用或绕过 RWX 验收替代，必须由 CFS 管理侧修复目录权限后重试。

## 7. 数据库边界

TKE 发布主链不执行 schema migration、TXSQL schema 诊断或数据修复。现有生产 MySQL 保持不变；若未来需要 DDL，另行制定数据库变更方案并单独授权，不将它混入应用发布。

## 8. MySQL 无客户流量演练

`tke-rehearse-mysql` 只在已通过 cluster pull 的同一 Backend digest 上创建一个临时 Backend Pod。该 Pod 只按键引用 `AUTO_DB_URI_PROD`，在只读 session 内执行 `SELECT 1`；不会执行数据库迁移、业务写入、Service、Ingress 或持久工作负载，并且无论成功或失败都会删除临时 Pod：

```bash
python3 scripts/release/release.py --state-dir "$STATE_DIR" tke-rehearse-mysql \
  --release-sha "$RELEASE_SHA" \
  --kube-context "$KUBE_CONTEXT" \
  --execute \
  --confirm-release-sha "$RELEASE_SHA"
```

该证明与 release SHA、Backend digest、kube context 绑定。缺失、漂移、临时 Pod 清理失败或只读查询失败时，禁止进入 standby 候选部署。

## 9. 无客户流量候选演练

`tke-rehearse-deploy` 只能消费已通过 MySQL 演练的同一不可变 digest。它会再次执行运行时物料只读检查，并在当前非活动槽渲染工作负载，执行 server-side dry-run、apply、rollout 与 standby 验收后才写入候选证明。候选渲染不包含稳定 Service 或 Ingress，Frontend 的 `API_UPSTREAM` 指向同槽 `api-{slot}`，Scheduler 强制为 `standby`，因此不会注册生产调度任务，也不会接收客户流量。

在创建候选工作负载前，入口会以该 release 的 `config.yaml` 作为**字段结构模板**同步共享的 `ConfigMap/aiops-runtime-config`：共同字段保留生产现有值；新增字段写入候选配置的默认值；候选已退役字段从生产配置删除；mapping、sequence 或标量类型变化一律失败关闭，不能猜测转换或覆盖生产值。同步仅通过 stdin 的 server-side apply 提交 `config.yaml`，随后回读精确配置；命令输出只记录字段路径、计数和无值的结构哈希，不记录任何配置值。无需也不得在控制台手工复制本地测试配置到生产。

如果字段结构本已一致，入口不写 ConfigMap。若类型冲突或 ConfigMap 回读不一致，会在任何候选工作负载 apply 前停止；这时修复明确的配置迁移规则后重新发布。命令输出只包含字段路径、计数和无值的结构哈希，不输出配置值：

```bash
python3 scripts/release/release.py --state-dir "$STATE_DIR" tke-rehearse-deploy \
  --release-sha "$RELEASE_SHA" \
  --kube-context "$KUBE_CONTEXT" \
  --execute \
  --confirm-release-sha "$RELEASE_SHA"
```

阶段验收至少包括：

- Backend、Frontend、Scheduler rollout 完成，Pod 使用记录中的 registry digest。
- `/api/health/live`、`/api/health/startup`、`/api/health/ready` 符合契约。
- Frontend `/release-info.json` 返回同一 release SHA。
- Frontend 通过 `/api` 正常反代 Backend。
- 同一 release 的 MySQL 临时演练证明包含网络、认证与只读查询结果。
- Scheduler 回读为 `standby`，没有有效生产调度执行。

上述项目是 `tke-rehearse-deploy` 的自动验收范围。日志、指标、审计、告警以及 PDB/驱逐故障注入属于单独的运行验收，不得误写为本命令已经验证。全部通过后，发布脚本才写入与 SHA、digest、context、manifest hash 和槽位绑定的 standby 候选证明。

### 9.1 standby 候选只读复验

`tke-rehearse-deploy` 已在写入候选证明前完成一次强制 standby 验收。`tke-rehearse-accept` 用于激活前的额外只读回读，不 apply、不写入状态，也不能替代候选部署：

```bash
python3 scripts/release/release.py --state-dir "$STATE_DIR" --format json tke-rehearse-accept \
  --release-sha "$RELEASE_SHA" \
  --kube-context "$KUBE_CONTEXT"
```

## 10. 激活与验收

`tke-activate` 只有在 cluster pull、MySQL 临时演练和 standby 候选证明均匹配时才允许执行；`tke-rehearse-accept` 是推荐的额外回读，不是另一份激活前置证明。激活会再次读取运行时物料，先将上一活动槽 Scheduler 缩为 `0`，再将候选槽重新渲染为 `active`，把稳定 `api`/`frontend` Service 和 Ingress 指向候选槽，完成 rollout 和 active 验收后才记录新的活动槽；若此前已有活动槽，同时把它保留为不可变回退目标。

```bash
python3 scripts/release/release.py --state-dir "$STATE_DIR" tke-activate \
  --release-sha "$RELEASE_SHA" \
  --kube-context "$KUBE_CONTEXT" \
  --execute \
  --confirm-release-sha "$RELEASE_SHA"

python3 scripts/release/release.py --state-dir "$STATE_DIR" --format json tke-accept \
  --release-sha "$RELEASE_SHA" \
  --kube-context "$KUBE_CONTEXT"
```

`tke-activate` 是唯一的正式激活命令；不提供兼容别名。
`tke-activate` 内部已经完成一次 active 验收；后续 `tke-accept` 是独立的只读回读，不会 apply 或写入活动槽状态。

若激活在停止上一槽 Scheduler 后失败，代码会尝试恢复上一槽 Scheduler。该恢复不等于完整应用或客户入口回滚；一旦稳定 Service、Ingress 或外部映射可能已切换，必须按第 11 节经过明确授权执行回滚并重新验收，不能假定脚本已自动恢复全部入口。

激活会修改 TKE 内的稳定 Service 与 Ingress，但不会直接修改 DNS、外部代理或证书。只有在外部映射尚未指向 TKE，或已取得客户流量切换授权时，才可执行；不得仅因命令本身不改 DNS 就推断不会影响客户流量。取得 CLB IP 后，只向域名同事提供：

- CLB 公网 IP
- HTTP 端口 `80`
- Host `ops.example.test`
- Frontend 路径 `/release-info.json`
- API 健康路径 `/api/health/ready`

客户流量切换必须单独确认。映射完成后再从外部网络验证 HTTPS、登录、关键页面、API 和 Scheduler 业务结果。

### 10.1 失败候选清理

候选部署或验收失败后的固定操作顺序是：不要直接重试；先确认失败发生在 server-side apply 后且候选资源仍存在，再仅清理明确指定的非活动槽，随后重新执行 `tke-rehearse-deploy`。清理仍需精确 SHA 确认。若失败发生在 apply 前，修复阻断原因后直接重试，不执行清理。这里是发布操作约定，脚本不会自动替你删除资源或强制阻断重试。

当 `tke-rehearse-deploy` 已计算出 standby 槽但未写入候选证明时，入口会打印同一 `state-dir`、namespace、kube context、release SHA 和槽位组成的精确清理命令；该输出只提供人工执行选项，绝不自动删除资源。命令形如：

```bash
python3 scripts/release/release.py --state-dir "$STATE_DIR" tke-cleanup-candidate \
  --release-sha "$RELEASE_SHA" \
  --kube-context "$KUBE_CONTEXT" \
  --slot green \
  --execute \
  --confirm-release-sha "$RELEASE_SHA"
```

清理前会回读活动槽状态、稳定 Service 和 `Ingress/aiops` 的后端，拒绝活动槽或仍被客户入口指向的槽。它只删除该槽的 Deployment、槽位 Service、PDB 和槽位 release ConfigMap，绝不删除 Namespace、CFS/PVC、Runtime ConfigMap/Secret、RBAC、NetworkPolicy、稳定 Service、Ingress 或 TCR 镜像；因此也不能用它撤销候选部署前的运行时 ConfigMap 字段同步。`--legacy-single-slot` 只用于已停止使用的历史单槽候选，不能替代 `--slot`。

## 11. 回滚

`tke-rollback` 不接受手工镜像或 tag，只能使用本地状态中记录的前一个不可变 release。命令中的 `RELEASE_SHA` 必须是当前已记录的活动 release，而不是希望恢复到的旧 release；入口据此定位并授权回退目标。首次 TKE 激活尚无前一个 TKE release 时，命令会失败关闭，此时只能按已批准的客户入口回退方案切回旧主机。回滚同样执行 server-side dry-run、rollout 和 active 验收：

```bash
python3 scripts/release/release.py --state-dir "$STATE_DIR" tke-rollback \
  --release-sha "$RELEASE_SHA" \
  --kube-context "$KUBE_CONTEXT" \
  --execute \
  --confirm-release-sha "$RELEASE_SHA"
```

应用回滚不等于数据库 schema 或业务数据回滚。本发布链不执行数据库变更；若未来单独执行 DDL，必须在该变更方案中明确备份、恢复和回滚边界。

客户入口异常时，先使用 `tke-rollback` 恢复到已记录的前一个 TKE 不可变 release；外部映射必须继续指向 TKE Ingress/CLB，不得回指已退役旧主机。

## 12. 完成标准

只有以下两类证据都具备，才可声明完成生产切流：

TKE 应用发布证据：

- 同一 release SHA、source tree SHA、候选身份哈希（`artifactManifestSha256`）与 schema release。
- Backend/Frontend 不可变 registry digest 与 TKE Pod imageID 一致。
- cluster pull、必要时的 CFS 初始化、MySQL 临时演练、standby 候选、active release、rollback 验收证据，以及活动槽状态。
- Runtime ConfigMap、Secret 键名与 RWX PVC 回读通过。
- TKE Pod 到生产 MySQL 的只读链路通过。
- Ingress/CLB、日志、指标、审计、告警和外部 HTTPS 回源通过。
- TKE 回滚目标、维护窗口、外部入口映射和最终切流确认已登记。
- 以上 cluster pull、CFS 初始化、MySQL、standby、active、rollback 证据均绑定同一候选身份；非首次 TKE 激活时，本地活动槽状态必须存在前一个不可变 release 作为回滚目标。

客户入口切换证据（仅在单独授权后收集）：

- 外部映射、HTTPS、登录、关键页面、关键 API 和 Scheduler 业务结果从外部网络回读通过。
- 日志、指标、审计和告警已按本次候选观察；这些不是 `tke-rehearse-deploy` 或 `tke-accept` 自动证明的内容。
- TKE 回滚目标、维护窗口、外部入口映射和最终切流确认已登记；旧主机已退役，不作为客户入口回退目标。

局部测试通过、Pod Running、Deployment rollout 完成或内部 HTTP 200 都不能单独替代完整生产准入。

## 独立 Atlas 图片 Worker 发布

主应用蓝绿发布之外，使用同一入口的 `tke-image-worker` 子命令。限定目标
context `cls-demo0000-000000000000-context-default`、namespace `aiops-demo`。
该子命令不会发布 Backend/Frontend/Scheduler 或改变客户流量。

在干净独立 worktree 完成以下专项质量检查（无前端改动时不运行前端 Gate）：

```bash
ENV=test bash scripts/ci/run_python_offline.sh -m pytest -q \
  tests/test_image_worker_release.py tests/test_atlas_browser_sync.py \
  tests/test_atlas_browser_bridge_client.py
node --test scripts/image_sync/test_atlas_browser_worker.mjs
git diff --check
```

使用 owner-only 状态目录、同一 release SHA 与临时 kubeconfig。先执行
`--phase preview`，然后执行 `--phase build`。只有明确授权新建专用访问
Token 时执行 `--phase provision-secret`；该阶段只创建不存在的 Secret，
不修改业务 Secret，不在日志或参数中提供 Token。变更阶段均要求
`--execute --confirm-release-sha "$RELEASE_SHA"`。

`--phase deploy --probe-url "$ATLAS_IMAGE_URL"` 消费已记录的不可变 Worker
候选镜像，先 server-side dry-run，保存前驱 Worker 清单，再部署专属
Deployment、Service、NetworkPolicy。不会创建公开 Ingress。

`--phase verify --urls-file "$URLS_FILE"` 检查 SHA、Pod imageID、readiness、
无认证 health 与拒绝未认证 fetch，并在真实 Worker Pod 串行请求至少十张
不同图片，要求全部 `200 image/*` 且非空。`--phase rollback` 只恢复前驱
Worker 清单；首次安装只缩容到零，保留 Secret，不删除主应用资源。

这些验证不等于 COS 和业务验收。线上 COS 同步消费者及其上传、HEAD、Mapping
回读还未由本子命令部署，完整图片链路必须另行验收。

## 图片 COS 小批验收

独立 Worker 的源站验证通过后，可通过 `tke-image-canary` 验证实时上架商品的
COS 与 Mapping 闭环。工具 SHA 与已部署 Worker SHA 分别显式绑定，不修改旧
Worker 状态。`provision-secret` 仅从 owner-only 本地环境文件读取已有生产 COS
配置，创建不存在的 `qg-image-assets`；绝不覆盖已有凭据，也不输出配置值。
`deploy` 只创建独立 Job / NetworkPolicy，读取并核对指定的当前活动 Backend
不可变镜像；失败不自动重跑。十个商品的 HEAD、公开 COS 取图、Mapping
回读及缓存复用全通过才写 accepted。该验收不等于新主应用版本已激活。

## PI 错误商品治理运行时

Backend/Scheduler Pod 内各运行一个独立 `pi-runtime` 容器，使用同一 Backend
不可变镜像，只监听 `127.0.0.1:8124`，随蓝绿槽一起发布和回滚。仅开放 KG 任务，
不启用交互助理会话；PI 不注入数据库/业务接口凭据，不挂载共享业务数据目录。
镜像包含锁定的 PI npm 依赖和 Node；镜像构建检查真实导入，Pod 探针检查运行时及
必要配置，发布验收核对 PI 容器 readiness 和镜像身份。业务验收另外执行真实
`RelevanceGovernanceAgent → PI → MCP → Schema 校验`，不能用健康接口替代。

首次部署前，使用 owner-only (0600) 环境文件提供 `KG_AGENT_MCP_TOKEN`：

```bash
python3 scripts/release/release.py --state-dir "$STATE_DIR" tke-pi-provision \
  --release-sha "$RELEASE_SHA" --kube-context "$KUBE_CONTEXT" \
  --env-file /absolute/private/pi.env --execute --confirm-release-sha "$RELEASE_SHA"
```

该入口仅给 `aiops-runtime-secrets` 增加缺失键，内部 Token 随机生成，已有键不覆盖。
通过 resourceVersion 前置条件避免并发覆盖；值只经 stdin 传输，不进入参数或状态文件。
标准 preflight 要求这两个键存在。正常发布仍执行完整 TKE 主链，不绕过质量或候选门禁。
PI 连接失败、超时、认证失败和输出格式错误分别记录独立脱敏错误码。
