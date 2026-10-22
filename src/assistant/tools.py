import json
import re
import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func

from src.persistence import (
    BrandDailyMetric,
    DailyAbnormalQuery,
    SessionLocal,
    TopQueryAgentEvent,
    TopQueryTask,
)
from src.integrations.operational_service import OperationalService


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class AssistantToolService:
    def __init__(self, operational_service: Optional[OperationalService] = None):
        self.operational_service = operational_service or OperationalService()

    def maybe_handle_message(
        self,
        message: str,
        *,
        allowed_brands: List[str],
        context: Dict[str, Any],
        selected_brand: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        normalized = (message or "").strip()
        if not normalized:
            return None
        selected_query = self._selected_query_from_context(context, normalized)

        if any(keyword in normalized.lower() for keyword in ["query详情", "top query详情", "topquery详情", "query detail"]):
            tool_result = self._read_top_query_detail(normalized, allowed_brands, selected_brand, context, selected_query)
            if tool_result:
                return tool_result

        if any(keyword in normalized.lower() for keyword in ["query事件", "agent事件", "执行事件", "query event"]):
            tool_result = self._read_top_query_events(normalized, allowed_brands, selected_brand, context, selected_query)
            if tool_result:
                return tool_result

        if any(keyword in normalized for keyword in ["品牌指标", "指标趋势", "搜索UV", "CTR", "CVR"]):
            tool_result = self._read_brand_metrics(normalized, allowed_brands, selected_brand, context)
            if tool_result:
                return tool_result

        if "异常日志" in normalized:
            tool_result = self._read_abnormal_query_logs(normalized, allowed_brands, selected_brand, context)
            if tool_result:
                return tool_result

        if any(keyword in normalized for keyword in ["今日", "待处理", "自动化", "任务概览", "运营数据"]):
            return self._build_today_summary(context)

        if "断码" in normalized:
            return self._build_inventory_summary(context, selected_query)

        if "top query" in normalized.lower() or "topquery" in normalized.lower():
            return self._build_top_query_summary(context, selected_query)

        if any(keyword in normalized for keyword in ["搜索", "查商品"]):
            tool_result = self._search_products(normalized, allowed_brands, selected_brand)
            if tool_result:
                return tool_result

        if "商品详情" in normalized or "product" in normalized.lower():
            tool_result = self._product_detail(normalized, allowed_brands, selected_brand)
            if tool_result:
                return tool_result

        return None

    def available_tools(self) -> List[Dict[str, Any]]:
        return [
            {"key": "read_today_context", "title": "读取今日自动化数据", "mode": "read"},
            {"key": "read_top_query_summary", "title": "读取 Top Query 摘要", "mode": "read"},
            {"key": "read_brand_metrics", "title": "读取品牌指标", "mode": "read"},
            {"key": "read_top_query_detail", "title": "读取 Top Query 详情", "mode": "read"},
            {"key": "read_top_query_events", "title": "读取 Query 事件日志", "mode": "read"},
            {"key": "read_abnormal_query_logs", "title": "读取异常 Query 日志", "mode": "read"},
            {"key": "read_inventory_monitoring", "title": "读取断码监测", "mode": "read"},
            {"key": "search_products", "title": "查询业务搜索结果", "mode": "read"},
            {"key": "get_product_detail", "title": "查询商品详情", "mode": "read"},
            {"key": "prepare_action", "title": "准备待确认业务动作", "mode": "confirm"},
        ]

    def execute_tool(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]],
        *,
        allowed_brands: List[str],
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute one allow-listed PI business tool with account scope applied.

        The PI runtime calls this method through the protected FastAPI endpoint;
        keeping dispatch here means database/API access remains in the existing
        service layer instead of being reimplemented in Node.
        """
        name = str(tool_name or "").strip()
        args = arguments if isinstance(arguments, dict) else {}
        brands = [str(item).strip() for item in (allowed_brands or []) if str(item).strip()]
        requested_brand = str(args.get("brand") or "").strip() or None
        if requested_brand and requested_brand not in brands:
            return {
                "message": f"品牌 {requested_brand} 不在当前账号授权范围内。",
                "toolCalls": [{"name": name, "arguments": json.dumps(args, ensure_ascii=False)}],
                "toolResults": [{"toolName": name, "summary": "账号权限拦截。"}],
                "proposedActions": [],
                "status": "failed",
            }

        if name == "read_today_context":
            return self._build_today_summary(context)
        if name == "read_top_query_summary":
            return self._build_top_query_summary(context, str(args.get("query") or "").strip() or None)
        if name == "read_inventory_monitoring":
            return self._build_inventory_summary(context, str(args.get("query") or "").strip() or None)
        if name == "read_brand_metrics":
            return self._read_brand_metrics(
                f"品牌指标 {requested_brand or ''}", brands, requested_brand, context,
            ) or self._tool_not_found(name)
        if name == "read_top_query_detail":
            query = str(args.get("query") or "").strip()
            return self._read_top_query_detail(
                f"query详情 {query}", brands, requested_brand, context, query or None,
            ) or self._tool_not_found(name)
        if name == "read_top_query_events":
            query = str(args.get("query") or "").strip()
            return self._read_top_query_events(
                f"query事件 {query}", brands, requested_brand, context, query or None,
            ) or self._tool_not_found(name)
        if name == "read_abnormal_query_logs":
            return self._read_abnormal_query_logs(
                f"异常日志 {requested_brand or ''}", brands, requested_brand, context,
            ) or self._tool_not_found(name)
        if name == "search_products":
            query = str(args.get("query") or "").strip()
            return self._search_products(f"搜索 {query}", brands, requested_brand) or self._tool_not_found(name)
        if name == "get_product_detail":
            product_id = str(args.get("productId") or "").strip()
            return self._product_detail(f"商品详情 {product_id}", brands, requested_brand) or self._tool_not_found(name)
        if name == "prepare_action":
            return {
                "message": "业务写操作必须通过 AIOPS 的 prepare/confirm/cancel 接口完成。",
                "toolCalls": [{"name": name, "arguments": json.dumps(args, ensure_ascii=False)}],
                "toolResults": [{"toolName": name, "summary": "已阻止直接写入，等待确认链路。"}],
                "proposedActions": [],
                "status": "failed",
            }
        return self._tool_not_found(name)

    @staticmethod
    def _tool_not_found(name: str) -> Dict[str, Any]:
        return {
            "message": f"不支持的智能运营工具：{name or '未命名'}。",
            "toolCalls": [{"name": name, "arguments": "{}"}],
            "toolResults": [{"toolName": name, "summary": "工具不存在。"}],
            "proposedActions": [],
            "status": "failed",
        }

    def _resolve_brand(self, message: str, allowed_brands: List[str], selected_brand: Optional[str]) -> Optional[str]:
        if selected_brand:
            return selected_brand
        normalized = (message or "").lower()
        alias_map = {
            "atlas": "atlas",
            "meadow": "meadow",
            "Meadow": "meadow",
            "harbor": "harbor",
            "h&m": "harbor",
            "summit": "summit",
            "Summit": "summit",
            "riviera lauren": "riviera",
            "riviera": "riviera",
            "Riviera": "riviera",
            "lumen": "lumen",
            "Lumen": "lumen",
        }
        for alias, brand in alias_map.items():
            if alias in normalized and brand in allowed_brands:
                return brand
        return allowed_brands[0] if len(allowed_brands) == 1 else None

    def _context_date(self, context: Dict[str, Any]) -> Optional[datetime.date]:
        raw_date = context.get("date")
        if not raw_date:
            return None
        try:
            return datetime.datetime.strptime(str(raw_date), "%Y-%m-%d").date()
        except ValueError:
            return None

    def _extract_query_fragment(self, message: str, keyword_patterns: List[str], brand: Optional[str]) -> str:
        quoted = re.search(r"[「\"](.+?)[」\"]", message)
        if quoted:
            return quoted.group(1).strip()
        fragment = message
        for pattern in keyword_patterns:
            fragment = re.split(pattern, fragment, maxsplit=1, flags=re.IGNORECASE)[-1]
        fragment = fragment.strip(" ：:，,。")
        if brand and fragment.lower().startswith(brand.lower()):
            fragment = fragment[len(brand):].strip(" ：:，,。")
        return fragment

    def _selected_query_from_context(self, context: Dict[str, Any], message: str) -> Optional[str]:
        page_context = context.get("pageContext") if isinstance(context, dict) else None
        if isinstance(page_context, dict):
            selected_query = str(page_context.get("selectedQuery") or "").strip()
            if selected_query:
                return selected_query
        contextual_match = re.search(r"当前\s*Query[:：]\s*([^\n；;]+)", message)
        if contextual_match:
            selected_query = contextual_match.group(1).strip()
            if selected_query:
                return selected_query
        return None

    def _build_today_summary(self, context: Dict[str, Any]) -> Dict[str, Any]:
        summary = context.get("summary") or {}
        queries = context.get("queries") or []
        top_queries = queries[:3]
        lines = [
            f"今日已读取 {summary.get('taskCount', 0)} 条任务，待处理 {summary.get('pendingCount', 0)} 条，治理动作 {summary.get('actionCount', 0)} 条，断码监测 {summary.get('monitoringCount', 0)} 条。"
        ]
        if top_queries:
            lines.append("重点 Query：")
            for item in top_queries:
                lines.append(
                    f"- {item.get('brand', '')} / {item.get('query', '')} / 状态 {item.get('status', 'unknown')} / CTR {round(_safe_float(item.get('ctr')) * 100, 2)}% / CVR {round(_safe_float(item.get('cvr')) * 100, 2)}%"
                )
        return {
            "message": "\n".join(lines),
            "toolCalls": [{"name": "read_today_context", "arguments": "{}"}],
            "toolResults": [{"toolName": "read_today_context", "summary": lines[0]}],
            "proposedActions": [],
            "status": "ok",
        }

    def _read_brand_metrics(
        self,
        message: str,
        allowed_brands: List[str],
        selected_brand: Optional[str],
        context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        brand = self._resolve_brand(message, allowed_brands, selected_brand)
        if not brand:
            return {
                "message": "当前可访问多个品牌，请先指定品牌后再查品牌指标。",
                "toolCalls": [{"name": "read_brand_metrics", "arguments": "{}"}],
                "toolResults": [{"toolName": "read_brand_metrics", "summary": "查询被拒绝：缺少明确品牌。"}],
                "proposedActions": [],
                "status": "ok",
            }
        target_date = self._context_date(context)
        db = SessionLocal()
        try:
            query = db.query(BrandDailyMetric).filter(BrandDailyMetric.brand == brand)
            if target_date is not None:
                query = query.filter(BrandDailyMetric.data_date == target_date)
            row = query.order_by(BrandDailyMetric.data_date.desc(), BrandDailyMetric.updated_at.desc()).first()
        finally:
            db.close()
        if not row:
            summary = f"没有查到 {brand} 的品牌指标。"
            return {
                "message": summary,
                "toolCalls": [{"name": "read_brand_metrics", "arguments": f'{{"brand":"{brand}"}}'}],
                "toolResults": [{"toolName": "read_brand_metrics", "summary": summary}],
                "proposedActions": [],
                "status": "ok",
            }
        lines = [
            f"{brand} 品牌指标（{row.data_date}）：",
            f"- 搜索UV：{row.brand_uv or 0}",
            f"- 搜索PV：{row.brand_pv or 0}",
            f"- CTR：{round((_safe_float(row.ctr)) * 100, 2)}%",
            f"- CVR：{round((_safe_float(row.cvr)) * 100, 2)}%",
            f"- Top Query 数：{row.top_query_count or 0}",
        ]
        return {
            "message": "\n".join(lines),
            "toolCalls": [{"name": "read_brand_metrics", "arguments": f'{{"brand":"{brand}","date":"{row.data_date}"}}'}],
            "toolResults": [{"toolName": "read_brand_metrics", "summary": f"已读取 {brand} 在 {row.data_date} 的品牌指标。"}],
            "proposedActions": [],
            "status": "ok",
        }

    def _read_top_query_detail(
        self,
        message: str,
        allowed_brands: List[str],
        selected_brand: Optional[str],
        context: Dict[str, Any],
        selected_query: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        brand = self._resolve_brand(message, allowed_brands, selected_brand)
        if not brand:
            return None
        query_text = self._extract_query_fragment(message, [r"query详情", r"top query详情", r"topquery详情", r"query detail"], brand)
        if query_text in {"这个top词", "这个Top词", "这个 query", "这个 Query", "这个"}:
            query_text = ""
        if not query_text:
            query_text = selected_query or ""
        if not query_text:
            return None
        target_date = self._context_date(context)
        db = SessionLocal()
        try:
            query = db.query(TopQueryTask).filter(
                TopQueryTask.brand == brand,
                TopQueryTask.query_text.contains(query_text),
            )
            if target_date is not None:
                query = query.filter(TopQueryTask.data_date == target_date)
            row = query.order_by(TopQueryTask.data_date.desc(), TopQueryTask.updated_at.desc()).first()
        finally:
            db.close()
        if not row:
            summary = f"没有查到 {brand} / {query_text} 的 Top Query 详情。"
            return {
                "message": summary,
                "toolCalls": [{"name": "read_top_query_detail", "arguments": f'{{"brand":"{brand}","query":"{query_text}"}}'}],
                "toolResults": [{"toolName": "read_top_query_detail", "summary": summary}],
                "proposedActions": [],
                "status": "ok",
            }
        lines = [
            f"{brand} / {row.query_text} 的 Top Query 详情：",
            f"- 状态 {row.status}",
            f"- 排名 {row.query_rank or 0}",
            f"- 搜索UV {row.search_uv or 0} / PV {row.search_pv or 0}",
            f"- CTR {round((_safe_float(row.ctr)) * 100, 2)}% / CVR {round((_safe_float(row.cvr)) * 100, 2)}%",
            f"- 结果数 {row.result_count or 0}",
        ]
        return {
            "message": "\n".join(lines),
            "toolCalls": [{"name": "read_top_query_detail", "arguments": f'{{"brand":"{brand}","query":"{row.query_text}","taskId":{row.id}}}'}],
            "toolResults": [{"toolName": "read_top_query_detail", "summary": f"已读取任务 {row.id} 的 Top Query 详情。"}],
            "proposedActions": [],
            "status": "ok",
        }

    def _read_top_query_events(
        self,
        message: str,
        allowed_brands: List[str],
        selected_brand: Optional[str],
        context: Dict[str, Any],
        selected_query: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        brand = self._resolve_brand(message, allowed_brands, selected_brand)
        if not brand:
            return None
        query_text = self._extract_query_fragment(message, [r"query事件", r"agent事件", r"执行事件", r"query event"], brand)
        if query_text in {"这个top词", "这个Top词", "这个 query", "这个 Query", "这个"}:
            query_text = ""
        if not query_text:
            query_text = selected_query or ""
        if not query_text:
            return None
        target_date = self._context_date(context)
        db = SessionLocal()
        try:
            query = db.query(TopQueryTask).filter(
                TopQueryTask.brand == brand,
                TopQueryTask.query_text.contains(query_text),
            )
            if target_date is not None:
                query = query.filter(TopQueryTask.data_date == target_date)
            task = query.order_by(TopQueryTask.data_date.desc(), TopQueryTask.updated_at.desc()).first()
            if not task:
                events: List[TopQueryAgentEvent] = []
            else:
                events = db.query(TopQueryAgentEvent).filter(
                    TopQueryAgentEvent.task_id == task.id
                ).order_by(TopQueryAgentEvent.id.desc()).limit(5).all()
        finally:
            db.close()
        if not task:
            summary = f"没有查到 {brand} / {query_text} 的 Query 事件。"
            return {
                "message": summary,
                "toolCalls": [{"name": "read_top_query_events", "arguments": f'{{"brand":"{brand}","query":"{query_text}"}}'}],
                "toolResults": [{"toolName": "read_top_query_events", "summary": summary}],
                "proposedActions": [],
                "status": "ok",
            }
        lines = [f"{brand} / {task.query_text} 的最近 Query 事件："]
        for event in events:
            lines.append(f"- {event.agent_name} / {event.stage} / {event.summary}")
        if len(lines) == 1:
            lines.append("- 当前没有已记录的 Agent 事件。")
        return {
            "message": "\n".join(lines),
            "toolCalls": [{"name": "read_top_query_events", "arguments": f'{{"brand":"{brand}","query":"{task.query_text}","taskId":{task.id}}}'}],
            "toolResults": [{"toolName": "read_top_query_events", "summary": f"已读取任务 {task.id} 的 {len(events)} 条事件。"}],
            "proposedActions": [],
            "status": "ok",
        }

    def _read_abnormal_query_logs(
        self,
        message: str,
        allowed_brands: List[str],
        selected_brand: Optional[str],
        context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        brand = self._resolve_brand(message, allowed_brands, selected_brand)
        target_date = self._context_date(context)
        db = SessionLocal()
        try:
            query = db.query(DailyAbnormalQuery).filter(DailyAbnormalQuery.brand.in_(allowed_brands))
            if brand:
                query = query.filter(DailyAbnormalQuery.brand == brand)
            if target_date is not None:
                query = query.filter(func.date(DailyAbnormalQuery.data_date) == target_date)
            rows = query.order_by(DailyAbnormalQuery.updated_at.desc(), DailyAbnormalQuery.created_at.desc()).limit(5).all()
        finally:
            db.close()
        lines = ["最近异常 Query 日志："]
        for row in rows:
            lines.append(
                f"- {row.brand} / {row.query_text} / 类型 {row.anomaly_type or ''} / 原因 {row.reason_analysis or row.remark or '待补充'}"
            )
        if len(lines) == 1:
            lines.append("当前没有可用的异常 Query 日志。")
        summary = f"已读取 {len(rows)} 条异常 Query 日志。"
        return {
            "message": "\n".join(lines),
            "toolCalls": [{"name": "read_abnormal_query_logs", "arguments": "{}" if not brand else f'{{"brand":"{brand}"}}'}],
            "toolResults": [{"toolName": "read_abnormal_query_logs", "summary": summary}],
            "proposedActions": [],
            "status": "ok",
        }

    def _build_inventory_summary(self, context: Dict[str, Any], selected_query: Optional[str] = None) -> Dict[str, Any]:
        inventory_rows = context.get("inventoryMonitoring") or []
        if selected_query:
            selected_rows = [row for row in inventory_rows if str(row.get("query") or "").strip() == selected_query]
            if selected_rows:
                inventory_rows = selected_rows
        if not inventory_rows:
            summary = "今日没有可用的断码监测数据。"
            return {
                "message": summary,
                "toolCalls": [{"name": "read_inventory_monitoring", "arguments": "{}"}],
                "toolResults": [{"toolName": "read_inventory_monitoring", "summary": summary}],
                "proposedActions": [],
                "status": "ok",
            }
        lines = ["今日断码监测摘要："]
        for row in inventory_rows[:5]:
            lines.append(
                f"- {row.get('brand', '')} / {row.get('query', '')} / 商品 {row.get('productId', '')} / 状态 {row.get('status', '')} / 断码率 {row.get('lastBrokenRate', 0)}%"
            )
        lines.append("说明：status=monitoring 通常表示系统已经对高断码商品执行了正确的降桶/降权治理，当前处于恢复观察阶段；这本身不是新的运营风险。")
        return {
            "message": "\n".join(lines),
            "toolCalls": [{"name": "read_inventory_monitoring", "arguments": "{}"}],
            "toolResults": [{"toolName": "read_inventory_monitoring", "summary": f"已读取 {len(inventory_rows)} 条断码监测记录。"}],
            "proposedActions": [],
            "status": "ok",
        }

    def _build_top_query_summary(self, context: Dict[str, Any], selected_query: Optional[str] = None) -> Dict[str, Any]:
        query_rows = context.get("queries") or []
        if selected_query:
            selected_rows = [row for row in query_rows if str(row.get("query") or "").strip() == selected_query]
            if selected_rows:
                query_rows = selected_rows
        lines = ["Top Query 摘要："]
        for row in query_rows[:5]:
            lines.append(
                f"- {row.get('brand', '')} / {row.get('query', '')} / 类型 {row.get('anomalyType', '')} / 状态 {row.get('status', '')}"
            )
        if len(lines) == 1:
            lines.append("当前没有可用的 Top Query 数据。")
        return {
            "message": "\n".join(lines),
            "toolCalls": [{"name": "read_top_query_summary", "arguments": "{}"}],
            "toolResults": [{"toolName": "read_top_query_summary", "summary": f"已读取 {len(query_rows)} 条 Query 上下文。"}],
            "proposedActions": [],
            "status": "ok",
        }

    def _search_products(self, message: str, allowed_brands: List[str], selected_brand: Optional[str]) -> Optional[Dict[str, Any]]:
        brand = selected_brand or (allowed_brands[0] if len(allowed_brands) == 1 else None)
        if not brand:
            return {
                "message": "当前可访问多个品牌，请先指定品牌后再查商品。",
                "toolCalls": [{"name": "search_products", "arguments": "{}"}],
                "toolResults": [{"toolName": "search_products", "summary": "查询被拒绝：缺少明确品牌。"}],
                "proposedActions": [],
                "status": "ok",
            }
        query = re.split(r"搜索|查商品", message, maxsplit=1)[-1].strip(" ：:，,。")
        if not query:
            return None
        rows = self.operational_service.search_products(brand, query, page_size=5)
        lines = [f"{brand} 搜索「{query}」返回 {len(rows)} 个样本："]
        for row in rows[:5]:
            lines.append(
                f"- {row.get('productId') or row.get('skuId') or ''} / {row.get('productName') or ''} / {row.get('price') or 0}"
            )
        return {
            "message": "\n".join(lines),
            "toolCalls": [{"name": "search_products", "arguments": f'{{"brand":"{brand}","query":"{query}"}}'}],
            "toolResults": [{"toolName": "search_products", "summary": f"已返回 {len(rows)} 个搜索商品样本。"}],
            "proposedActions": [],
            "status": "ok",
        }

    def _product_detail(self, message: str, allowed_brands: List[str], selected_brand: Optional[str]) -> Optional[Dict[str, Any]]:
        brand = selected_brand or (allowed_brands[0] if len(allowed_brands) == 1 else None)
        if not brand:
            return None
        match = re.search(r"([A-Za-z0-9_-]{4,})", message)
        if not match:
            return None
        product_id = match.group(1)
        rows = self.operational_service.get_product_details(brand, product_id)
        if not rows:
            summary = f"没有查到商品 {product_id} 的详情。"
            return {
                "message": summary,
                "toolCalls": [{"name": "get_product_detail", "arguments": f'{{"brand":"{brand}","productId":"{product_id}"}}'}],
                "toolResults": [{"toolName": "get_product_detail", "summary": summary}],
                "proposedActions": [],
                "status": "ok",
            }
        detail = rows[0]
        message_lines = [
            f"商品 {product_id} 详情：",
            f"- 名称：{detail.get('productName') or detail.get('name') or ''}",
            f"- 价格：{detail.get('price') or detail.get('discountPrice') or 0}",
            f"- 类目：{detail.get('mainCategory') or detail.get('category') or ''}",
        ]
        return {
            "message": "\n".join(message_lines),
            "toolCalls": [{"name": "get_product_detail", "arguments": f'{{"brand":"{brand}","productId":"{product_id}"}}'}],
            "toolResults": [{"toolName": "get_product_detail", "summary": f"已读取商品 {product_id} 的详情。"}],
            "proposedActions": [],
            "status": "ok",
        }
