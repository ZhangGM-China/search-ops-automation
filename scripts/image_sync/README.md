# Atlas 浏览器图片 Worker

这是一个独立的 Atlas 图片获取 Worker。它不进入 AIOPS Backend、Scheduler 的请求链路，也不负责商品查询。

## 职责

```text
浏览器 Worker
  → 以 Chromium 访问 static.atlas.example.test
  → 每个 Worker 串行请求
  → 请求起始间隔 1.5～2 秒
  → 返回图片字节

同步脚本
  → 调用 Worker
  → 使用项目现有 Pillow 规则转换图片
  → 上传 COS
  → 写入 query_governance_image_asset Mapping
```

Worker 只接受 `https://static.atlas.example.test`，并且远程绑定地址必须配置 Bearer Token。

## 本地验证

```bash
node scripts/image_sync/atlas_browser_worker.mjs \
  --chrome-path '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  --token '<local-token>'
```

同步脚本默认是 dry-run，只下载并转换，不写 COS 和生产 Mapping：

```bash
python scripts/image_sync/atlas_browser_sync.py \
  --source-file /path/to/atlas.on_shelf.jsonl \
  --bridge-url http://127.0.0.1:8790 \
  --bridge-token '<local-token>' \
  --limit 10
```

明确执行生产写入时必须同时满足：

```bash
ENV=prod python scripts/image_sync/atlas_browser_sync.py \
  --source-file /path/to/atlas.on_shelf.jsonl \
  --bridge-url http://127.0.0.1:8790 \
  --bridge-token '<local-token>' \
  --execute
```

### 本机一次性初始同步

如果生产出口被 Atlas 拒绝，但本机 Chrome 可以访问图片，不要把本机当成生产长期代理。初始迁移可以在本机一次性运行，Worker 使用本机 headed Chrome，同步脚本仍只负责 COS 和 Mapping：

```bash
QG_BROWSER_PROBE_URL='https://static.atlas.example.test/...jpg' \
ENV=prod ./scripts/image_sync/run_local_atlas_bootstrap.sh \
  /path/to/atlas.on_shelf.jsonl \
  --execute
```

不带 `--execute` 时仍是 dry-run。脚本只启动独立 Worker，退出后会清理 Worker，不会进入 Backend、Scheduler 或 `master` 主链路。

## 独立部署

`Dockerfile` 只构建浏览器 Worker，不修改 AIOPS 主镜像。生产部署需要提供一个能稳定访问 Atlas 的网络出口：

- Worker 直接运行在已能访问 Atlas 的固定出口主机；或
- 使用 `QG_BROWSER_PROXY_SERVER` / `--proxy-server` 指向可达的专用代理。

如果 Worker 运行在 TKE，TKE 出口本身仍被 Atlas 拒绝时，单纯部署 Chromium 不会消除 403。

`GET /health` 只表示进程存活；配置 `QG_BROWSER_PROBE_URL` 后，`GET /probe` 才会真实请求一张 Atlas 图片并作为部署前出口验收。`/probe` 返回 403 时不要开始全量同步，应先切换到本机一次性初始同步或提供稳定专用出口。

远程运行时至少注入：

```text
QG_BROWSER_BIND_HOST=0.0.0.0
QG_BROWSER_BRIDGE_TOKEN=<secret>
QG_BROWSER_CHROME_PATH=/usr/bin/chromium
```

Token 只能通过 Secret 注入，不写入镜像、代码、日志或命令历史。

## 验收顺序

1. 用 10 个上架商品 URL 做 canary，确认 Worker 返回 `200 image/*`。
2. 确认同一 Worker 没有并发请求，并观察请求间隔至少 1.5 秒。
3. 确认 COS `put_object` 后 `head_object` 成功。
4. 确认生产 Mapping 的 `brand/product_id/source_url/storage_key/status=ready` 回读一致。
5. 才开始断点续跑全量同步。
