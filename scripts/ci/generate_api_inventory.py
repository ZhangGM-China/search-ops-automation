#!/usr/bin/env python3
"""Regenerate the checked-in API inventory from FastAPI route decorators."""

from __future__ import annotations

import argparse
import ast
import re
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_APP_PATH = PROJECT_ROOT / "src/api/app.py"
DEFAULT_INVENTORY_PATH = PROJECT_ROOT / "docs/TSD/api/internal-api-inventory.md"
ROW_PATTERN = re.compile(
    r"^\| \d+ \| ([A-Z]+) `([^`]+)` \| `([^`]+)` \| ([^|]+) \| ([^|]+) \| ([^|]+) \| [^|]+ \|$"
)
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class Route:
    source_path: Path
    line: int
    method: str
    path: str
    handler: str


@dataclass(frozen=True)
class Metadata:
    exposure: str
    boundary: str
    effect: str


ADDED_ROUTE_METADATA = {
    ("GET", "/api/health/live"): Metadata("internal", "无 Bearer；无品牌", "进程存活探针"),
    ("GET", "/api/health/startup"): Metadata("internal", "无 Bearer；无品牌", "启动状态探针"),
    ("GET", "/api/health/ready"): Metadata("internal", "无 Bearer；无品牌", "就绪状态探针"),
    ("POST", "/api/auth/logout"): Metadata("internal", "Bearer；当前用户", "撤销当前用户全部会话"),
    ("GET", "/api/dashboard/top-query/brands/{brand}"): Metadata("internal", "Bearer；路径品牌", "读聚合详情"),
    ("POST", "/api/admin/users/{user_id}/revoke-sessions"): Metadata("internal", "Bearer；管理员/品牌范围", "撤销用户会话"),
    ("GET", "/api/admin/page-config"): Metadata("internal", "Bearer；当前用户", "读取全局页面展示配置"),
    ("POST", "/api/admin/page-config"): Metadata("internal", "Bearer；平台配置权限", "更新全局页面展示配置并按版本回读"),
    ("POST", "/api/query-governance/relevance-checks"): Metadata("external-compute", "Bearer；请求品牌", "创建只读商品详情/模型检测任务状态"),
    ("GET", "/api/dashboard/top-query/brands/{brand}/metric-trend"): Metadata("external-read", "Bearer；路径品牌", "读品牌运营指标逐日趋势快照"),
    ("GET", "/api/query-governance/relevance-checks/{check_id}"): Metadata("external-compute", "Bearer；任务所属品牌", "读取检测结果"),
    ("POST", "/api/query-governance/actions/{action_id}/execute"): Metadata("internal", "Bearer；治理权限/动作所属品牌", "执行已确认的知识图谱治理动作并写回执行事实"),
    ("POST", "/api/query-governance/kg-plans/{plan_id}/execute"): Metadata("internal", "Bearer；治理权限/动作所属品牌", "通过兼容入口执行已确认的知识图谱治理动作并写回执行事实"),
    ("GET", "/api/admin/node-model-config"): Metadata("internal", "Bearer；配置权限/品牌", "读取节点模型配置"),
    ("POST", "/api/admin/node-model-config/drafts"): Metadata("internal", "Bearer；配置权限/品牌", "创建节点模型草稿"),
    ("POST", "/api/admin/node-model-config/versions/{version}/publish"): Metadata("internal", "Bearer；配置权限/品牌", "发布节点模型配置"),
    ("POST", "/api/admin/node-model-config/rollback"): Metadata("internal", "Bearer；配置权限/品牌", "回滚节点模型配置"),
    ("GET", "/api/dashboard/top-query/brands/{brand}/relevance-guidance"): Metadata("internal", "Bearer；路径品牌", "读取相关性纠正规则"),
    ("PUT", "/api/dashboard/top-query/brands/{brand}/relevance-guidance"): Metadata("internal", "Bearer；路径品牌/治理配置权限", "保存相关性纠正规则"),
    ("POST", "/api/dashboard/top-query/brands/{brand}/relevance-guidance/append"): Metadata("internal", "Bearer；路径品牌/执行治理动作权限", "原子追加商品纠正规则并按序去重；写入前过滤 MySQL 三字节 `utf8` 不支持的补充平面字符"),
    ("POST", "/api/dashboard/top-query/brands/{brand}/relevance-guidance/deactivate"): Metadata("internal", "Bearer；路径品牌/治理配置权限", "停用相关性纠正规则"),
    ("POST", "/api/dashboard/top-query/brands/{brand}/relevance-guidance/organize"): Metadata("internal", "Bearer；路径品牌/治理配置权限", "预览 AI 整理后的相关性纠正规则"),
    ("POST", "/api/dashboard/top-query/brands/{brand}/relevance-guidance/confirm"): Metadata("internal", "Bearer；路径品牌/治理配置权限", "确认并保存 AI 整理后的相关性纠正规则"),
    ("POST", "/api/dashboard/actions/cancel-blacklist/batch"): Metadata("internal", "Bearer；品牌/治理权限", "批量调用撤销拉黑并逐项返回结果"),
    ("POST", "/api/dashboard/actions/blacklist"): Metadata("internal", "Bearer；品牌/治理权限", "调用单商品拉黑并执行权威回读"),
    ("POST", "/api/dashboard/actions/blacklist/batch"): Metadata("internal", "Bearer；品牌/治理权限", "批量调用拉黑并逐项返回结果"),
    ("POST", "/api/internal/assistant/tools/{tool_name}"): Metadata("internal", "X-PI-Internal-Token；按 userId 重解析账号权限", "调用账号范围内的 PI AIOPS 业务工具；业务写操作不在此接口执行"),
    ("GET", "/api/assistant/workspace"): Metadata("internal", "Bearer；当前用户工作区根目录", "读取授权项目工作区目录"),
    ("GET", "/api/assistant/workspace/file"): Metadata("internal", "Bearer；当前用户工作区路径", "读取授权项目工作区文件预览"),
    ("POST", "/api/assistant/workspace/file"): Metadata("internal", "Bearer；当前用户工作区路径", "上传文件到授权项目工作区"),
    ("POST", "/api/assistant/terminal/sessions"): Metadata("internal", "Bearer；当前用户工作区", "创建账号范围内终端会话"),
    ("POST", "/api/assistant/terminal/sessions/{terminal_session_id}/execute"): Metadata("internal", "Bearer；终端会话及工作区", "执行受控终端命令并返回输出"),
    ("POST", "/api/assistant/terminal/sessions/{terminal_session_id}/stop"): Metadata("internal", "Bearer；终端会话及工作区", "停止终端会话"),
    ("POST", "/api/assistant/sessions/{session_meta_id}/branches"): Metadata("internal", "Bearer；当前用户会话", "从历史消息创建会话分支"),
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-path",
        type=Path,
        action="append",
        help="Explicit route implementation source; repeat for multiple files.",
    )
    parser.add_argument("--inventory-path", type=Path, default=DEFAULT_INVENTORY_PATH)
    parser.add_argument(
        "--metadata-path",
        type=Path,
        default=DEFAULT_INVENTORY_PATH,
        help="Existing inventory from which route classification metadata is read.",
    )
    return parser.parse_args()


def _existing_metadata(path: Path) -> dict[tuple[str, str], Metadata]:
    metadata = dict(ADDED_ROUTE_METADATA)
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ROW_PATTERN.match(line)
        if not match:
            continue
        method, route_path, _handler, exposure, boundary, effect = match.groups()
        key = (method, route_path)
        if key not in ADDED_ROUTE_METADATA:
            metadata[key] = Metadata(exposure.strip(), boundary.strip(), effect.strip())
    return metadata


def _is_router_decorator(decorator: ast.expr) -> bool:
    if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
        return False
    return (
        isinstance(decorator.func.value, ast.Name)
        and decorator.func.value.id == "router"
        and decorator.func.attr.upper() in HTTP_METHODS
    )


def _router_implementation_paths(router_path: Path) -> list[Path]:
    """Follow router composition modules to their decorator sources."""
    tree = ast.parse(router_path.read_text(encoding="utf-8"))
    if any(
        _is_router_decorator(decorator)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
    ):
        return [router_path]

    candidates: list[Path] = []
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not any(name.name == "router" for name in node.names):
            continue
        candidate = PROJECT_ROOT / "src/api" / Path(*node.module.split("."))
        candidate = candidate.with_suffix(".py")
        if candidate.is_file():
            candidates.append(candidate)

    if candidates:
        return candidates

    raise RuntimeError(f"unable to locate route decorators for {router_path}")


def _registered_route_source_paths() -> list[Path]:
    """Resolve the router modules actually mounted by ``src/api/app.py``."""
    tree = ast.parse(DEFAULT_APP_PATH.read_text(encoding="utf-8"))
    router_imports: dict[str, Path] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith("routers."):
            continue
        module_path = PROJECT_ROOT / "src/api" / Path(*node.module.split("."))
        module_path = module_path.with_suffix(".py")
        for name in node.names:
            if name.name == "router":
                router_imports[name.asname or name.name] = module_path

    mounted_aliases: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not isinstance(node.iter, (ast.Tuple, ast.List)):
            continue
        if not any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "include_router"
            for child in ast.walk(node)
        ):
            continue
        mounted_aliases = [item.id for item in node.iter.elts if isinstance(item, ast.Name)]
        if mounted_aliases:
            break

    if not mounted_aliases:
        raise RuntimeError(f"unable to find mounted routers in {DEFAULT_APP_PATH}")

    sources: list[tuple[int, Path]] = []
    for position, alias in enumerate(mounted_aliases):
        try:
            router_path = router_imports[alias]
        except KeyError as exc:
            raise RuntimeError(f"missing router import for mounted alias: {alias}") from exc
        sources.extend(
            (position, source_path)
            for source_path in _router_implementation_paths(router_path)
        )

    # Keep probes at the top, then retain the application's registration order.
    sources.sort(key=lambda item: (item[1].name != "health.py", item[0]))
    return [path for _, path in sources]


def _routes(source_paths: list[Path]) -> list[Route]:
    routes: list[Route] = []
    seen: set[tuple[str, str]] = set()
    for source_path in source_paths:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not _is_router_decorator(decorator) or not decorator.args:
                    continue
                path = ast.literal_eval(decorator.args[0])
                if not isinstance(path, str):
                    raise RuntimeError(f"route path must be a string literal: {source_path}:L{decorator.lineno}")
                key = (decorator.func.attr.upper(), path)
                if key in seen:
                    continue
                seen.add(key)
                routes.append(
                    Route(
                        source_path=source_path,
                        line=decorator.lineno,
                        method=decorator.func.attr.upper(),
                        path=path,
                        handler=node.name,
                    )
                )
    return routes


def _render(routes: list[Route], metadata: dict[tuple[str, str], Metadata]) -> str:
    missing = [(route.method, route.path) for route in routes if (route.method, route.path) not in metadata]
    if missing:
        raise RuntimeError(f"missing inventory metadata: {missing}")
    lines = [
        "# API 路由全量清单",
        "",
        f"本清单由 `src/api/app.py` 实际装配的路由模块 AST 提取，当前 **{len(routes)} operations**。`exposure` 是发布分类：`external-read` 为外部只读 allowlist，`external-compute` 为不产生外部业务写入的外部计算接口，`internal` 为受控管理端，`deprecated` 为待退役兼容入口。`来源` 标记路由装饰器的文件和行号；`src/api/app.py` 只负责装配。",
        "",
        "| # | method/path | 处理函数 | exposure | 认证/品牌边界 | 主要副作用 | 来源 |",
        "| ---: | --- | --- | --- | --- | --- | --- |",
    ]
    for index, route in enumerate(routes, start=1):
        item = metadata[(route.method, route.path)]
        try:
            source_label = route.source_path.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            source_label = str(route.source_path)
        lines.append(
            f"| {index} | {route.method} `{route.path}` | `{route.handler}` | {item.exposure} | {item.boundary} | {item.effect} | `{source_label}:L{route.line}` |"
        )
    lines.extend([
        "",
        "## 覆盖口径",
        "",
        "本表逐行对应实际装配路由的 AST 提取；`method/path` 作为唯一键。运行 `venv/bin/python scripts/ci/generate_api_inventory.py` 可重新生成。未列入本表的前端历史 URL 不构成后端 operation。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    args = _parse_args()
    source_paths = args.source_path or _registered_route_source_paths()
    source_paths = [path if path.is_absolute() else PROJECT_ROOT / path for path in source_paths]
    inventory_path = args.inventory_path if args.inventory_path.is_absolute() else PROJECT_ROOT / args.inventory_path
    metadata_path = args.metadata_path if args.metadata_path.is_absolute() else PROJECT_ROOT / args.metadata_path
    inventory_path.write_text(_render(_routes(source_paths), _existing_metadata(metadata_path)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
