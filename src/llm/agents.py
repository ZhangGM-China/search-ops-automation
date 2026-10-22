import os
import logging
import json
import time
from uuid import uuid4
from typing import Optional, Dict, Any, List
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv

from .node_config import NodeConfigSnapshot, ResolvedNodeConfig, get_snapshot_node_config
from .rd_client import DEFAULT_RD_LLM_MODEL, FallbackChatModel, build_rd_chat_model
from src.query_governance.langfuse import QueryGovernanceObservability
from src.query_governance.models import QuerySpec

load_dotenv()

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _agent_timeout(primary_env: str, default: float) -> float:
    return _env_float(primary_env, default)


def _build_snapshot_rd_llm(
    node_key: str,
    model_config_snapshot: NodeConfigSnapshot,
    *,
    temperature: float,
    timeout: Optional[float] = None,
) -> Optional[Any]:
    config = get_snapshot_node_config(
        model_config_snapshot,
        node_key,
        required_channel="rd_llm",
    )
    model_kwargs = {
        "default_model": config.primary_model,
        "temperature": temperature,
        "max_retries": 0,
    }
    if timeout is not None:
        model_kwargs["timeout"] = timeout
    primary = build_rd_chat_model(**model_kwargs)
    if primary is None:
        return None
    fallback = None
    if config.fallback_model:
        fallback_kwargs = dict(model_kwargs)
        fallback_kwargs["default_model"] = config.fallback_model
        fallback = build_rd_chat_model(**fallback_kwargs)
    return FallbackChatModel(primary=primary, fallback=fallback)


class _SnapshotModelAuditMixin:
    model_config: Optional[ResolvedNodeConfig]
    selected_model_name: Optional[str]
    selected_model_role: Optional[str]
    llm: Any
    langfuse_config: Optional[Dict[str, Any]] = None

    def _invoke(self, messages: List[Dict[str, Any]]) -> Any:
        self.selected_model_name = None
        self.selected_model_role = None
        invoke_kwargs = {"config": self.langfuse_config} if self.langfuse_config else {}
        if self.model_config is None:
            return self.llm.invoke(messages, **invoke_kwargs)
        if not isinstance(self.llm, FallbackChatModel):
            raise RuntimeError("snapshot RD-LLM is unavailable")
        try:
            response = self.llm.invoke_primary(messages, **invoke_kwargs)
        except Exception:
            if self.llm.fallback is None or not self.model_config.fallback_model:
                raise
            response = self.llm.invoke_fallback(messages, **invoke_kwargs)
            self.selected_model_name = self.model_config.fallback_model
            self.selected_model_role = "fallback"
            return response
        self.selected_model_name = self.model_config.primary_model
        self.selected_model_role = "primary"
        return response

    def _model_audit_fields(self) -> Dict[str, Any]:
        if self.model_config is None:
            return {}
        if self.selected_model_role == "primary":
            config_source = self.model_config.primary_source
        elif self.selected_model_role == "fallback":
            config_source = self.model_config.fallback_source
        else:
            config_source = None
        return {
            "nodeKey": self.model_config.node_key,
            "channel": self.model_config.channel,
            "modelName": self.selected_model_name,
            "modelRole": self.selected_model_role,
            "configSource": config_source,
        }

    def _operation_log_response(self) -> Any:
        model_audit = self._model_audit_fields()
        if not model_audit:
            return None
        return json.dumps({"modelAudit": model_audit}, ensure_ascii=False)

    def _apply_model_audit_log_policy(self, log_data: Dict[str, Any]) -> None:
        if self.model_config is None:
            return
        log_data["request_payload_policy"] = "none"
        log_data["response_payload_policy"] = "full"
        log_data["response"] = self._operation_log_response()

# --- 环境上下文工具 (处理内网代理问题) ---

class EnvContext:
    """上下文管理器：临时清理代理环境变量，确保内网调用直连"""
    def __init__(self, env_vars: List[str]):
        self.env_vars = env_vars
        self.old_env = {}

    def __enter__(self):
        self.old_env = {v: os.environ.get(v) for v in self.env_vars}
        for v in self.env_vars: os.environ.pop(v, None)
        os.environ['no_proxy'] = '*' # 强制直连
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for v, val in self.old_env.items():
            if val is not None: os.environ[v] = val
        os.environ.pop('no_proxy', None)

def _llm_metrics_payload(
    *,
    response: Any = None,
    timeout_seconds: float | None = None,
    request_text: str = "",
    system_prompt: str = "",
    error: Exception | None = None,
) -> Dict[str, Any]:
    response_text = str(getattr(response, "content", response) or "") if response is not None else ""
    return {
        "timeoutSeconds": timeout_seconds,
        "requestChars": len(request_text) + len(system_prompt),
        "responseChars": len(response_text),
        "errorType": type(error).__name__ if error else None,
    }

# --- 核心诊断 Agent ---


class NoResultQueryIntentAgent(_SnapshotModelAuditMixin):
    """无结果 Query 二筛 Agent，判断是否属于购物搜索意图。"""

    NON_SHOPPING_KEYWORDS = ("积分", "兑换", "抽奖", "优惠券", "门店", "回收", "租赁", "二手")

    def __init__(
        self,
        model_config_snapshot: Optional[NodeConfigSnapshot] = None,
        langfuse_config: Optional[Dict[str, Any]] = None,
    ):
        self.langfuse_config = langfuse_config
        self.model_config: Optional[ResolvedNodeConfig] = None
        if model_config_snapshot is None:
            self.llm = build_rd_chat_model(
                default_model=DEFAULT_RD_LLM_MODEL,
                temperature=0.1,
                max_retries=0,
            )
        else:
            self.model_config = get_snapshot_node_config(
                model_config_snapshot,
                "no_result.intent",
                required_channel="rd_llm",
            )
            self.llm = _build_snapshot_rd_llm(
                "no_result.intent",
                model_config_snapshot,
                temperature=0.1,
            )
        self.selected_model_name = None
        self.selected_model_role = None
        self.system_prompt = """
你是电商搜索无结果 Query 判断 Agent。判断 Query 是否具备购物搜索意图。
只输出合法 JSON：
{
  "decision": "process 或 ignore",
  "reason": "20字以内原因",
  "confidence": 0到1的小数
}
非购物意图包括积分、兑换、抽奖、门店、优惠券、租赁、二手回收等。
"""

    def classify(self, query: str, brand: str) -> Dict[str, Any]:
        from src.persistence import save_operation_log
        start_time = time.time()
        request_text = f"品牌: {brand}\nQuery: {query}"
        log_data = {
            "brand": brand,
            "module": "NoResultQueryIntentAgent",
            "action": "No_Result_Query_Intent",
            "request": request_text,
        }
        if self.model_config is None:
            log_data["query"] = query
        try:
            with EnvContext(['http_proxy', 'https_proxy', 'all_proxy']):
                response = self._invoke([
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": request_text},
                ])
            result = self._normalize_intent_result(_extract_json_object(response.content))
            log_data.update({"response": response.content, "status": "success", "duration": time.time() - start_time})
            self._apply_model_audit_log_policy(log_data)
            save_operation_log(log_data)
            return result
        except Exception as e:
            fallback = self._rule_based_classify(query)
            log_data.update({"status": "failed", "duration": time.time() - start_time, "error": str(e), "response": json.dumps(fallback, ensure_ascii=False)})
            self._apply_model_audit_log_policy(log_data)
            save_operation_log(log_data)
            return fallback

    def _rule_based_classify(self, query: str) -> Dict[str, Any]:
        if any(keyword in query for keyword in self.NON_SHOPPING_KEYWORDS):
            return {"decision": "ignore", "reason": "非购物搜索意图", "confidence": 0.8}
        return {"decision": "process", "reason": "具备购物搜索意图", "confidence": 0.6}

    def _normalize_intent_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        decision = str(result.get("decision") or "process").lower()
        if decision not in {"process", "ignore"}:
            decision = "process"
        reason = str(result.get("reason") or ("非购物搜索意图" if decision == "ignore" else "具备购物搜索意图"))[:40]
        try:
            confidence = float(result.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        return {"decision": decision, "reason": reason, "confidence": max(0.0, min(confidence, 1.0))}


class NoResultDiagnosisAgent(_SnapshotModelAuditMixin):
    """无结果词归因分析与任务路由 Agent。"""

    def __init__(
        self,
        model_config_snapshot: Optional[NodeConfigSnapshot] = None,
        langfuse_config: Optional[Dict[str, Any]] = None,
    ):
        self.langfuse_config = langfuse_config
        timeout = _env_float("NO_RESULT_AGENT_TIMEOUT_SECONDS", 100.0)
        self.model_config: Optional[ResolvedNodeConfig] = None
        if model_config_snapshot is None:
            self.llm = build_rd_chat_model(
                timeout=timeout,
                default_model=DEFAULT_RD_LLM_MODEL,
                temperature=0.2,
                max_retries=0,
            )
        else:
            self.model_config = get_snapshot_node_config(
                model_config_snapshot,
                "no_result.diagnosis",
                required_channel="rd_llm",
            )
            self.llm = _build_snapshot_rd_llm(
                "no_result.diagnosis",
                model_config_snapshot,
                timeout=timeout,
                temperature=0.2,
            )
        self.selected_model_name = None
        self.selected_model_role = None
        self.system_prompt = """
你是无结果词归因分析与任务路由 Agent。你不能调用外部 API，只能输出结构化 JSON。
输出类型：
1. 请求查询：
{
  "outputType": "query_request",
  "reason": "原因",
  "confidence": 0到1,
  "queryIntents": [{"type":"query_tag|query_relationship|query_dimension","params":{}}]
}
2. 输出策略：
{
  "outputType": "strategy",
  "reason": "原因",
  "confidence": 0到1,
  "actions": [
    {
      "actionType": "add_tag|update_tag|blacklist_tag|unblacklist_tag|add_relationship|update_relationship|delete_relationship",
      "targetInterface": "接口路径",
      "riskLevel": "low|medium|high",
      "reason": "动作原因",
      "params": {}
    }
  ]
}
遵守约束：同义关系仅非根标签到根标签；从属关系仅根标签到根标签；删改关系为高风险。
"""

    def diagnose(self, query: str, candidate_tags: List[str], graph_context: Dict[str, Any]) -> Dict[str, Any]:
        from src.persistence import save_operation_log
        start_time = time.time()
        request_payload = {
            "query": query,
            "candidateTags": candidate_tags,
            "graphContext": graph_context,
        }
        request_text = json.dumps(request_payload, ensure_ascii=False)
        log_data = {
            "module": "NoResultDiagnosisAgent",
            "action": "No_Result_Diagnosis",
            "request": request_text,
        }
        if self.model_config is None:
            log_data["query"] = query
        try:
            with EnvContext(['http_proxy', 'https_proxy', 'all_proxy']):
                response = self._invoke([
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": request_text},
                ])
            result = self._normalize_diagnosis_result(_extract_json_object(response.content))
            llm_metrics = _llm_metrics_payload(
                response=response,
                timeout_seconds=_env_float("NO_RESULT_AGENT_TIMEOUT_SECONDS", 100.0),
                request_text=request_text,
                system_prompt=self.system_prompt,
            )
            result["_llm_metrics"] = llm_metrics
            log_data.update({
                "response": json.dumps({"content": response.content, "llmMetrics": llm_metrics}, ensure_ascii=False),
                "status": "success",
                "duration": time.time() - start_time,
            })
            self._apply_model_audit_log_policy(log_data)
            save_operation_log(log_data)
            return result
        except Exception as e:
            fallback = {
                "outputType": "manual_required",
                "reason": f"诊断失败: {str(e)}"[:80],
                "confidence": 0.0,
                "actions": [],
                "queryIntents": [],
            }
            llm_metrics = _llm_metrics_payload(
                timeout_seconds=_env_float("NO_RESULT_AGENT_TIMEOUT_SECONDS", 100.0),
                request_text=request_text,
                system_prompt=self.system_prompt,
                error=e,
            )
            fallback["_llm_metrics"] = llm_metrics
            log_data.update({
                "status": "failed",
                "duration": time.time() - start_time,
                "error": str(e),
                "response": json.dumps({"content": json.dumps(fallback, ensure_ascii=False), "llmMetrics": llm_metrics}, ensure_ascii=False),
            })
            self._apply_model_audit_log_policy(log_data)
            save_operation_log(log_data)
            return fallback

    def _normalize_diagnosis_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        output_type = result.get("outputType") or result.get("output_type") or "manual_required"
        if output_type not in {"query_request", "strategy", "manual_required", "ignore"}:
            output_type = "manual_required"
        actions = result.get("actions") if isinstance(result.get("actions"), list) else []
        normalized_actions = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            risk = str(action.get("riskLevel") or action.get("risk_level") or "medium").lower()
            if risk not in {"low", "medium", "high"}:
                risk = "medium"
            normalized_actions.append({
                "actionType": action.get("actionType") or action.get("action_type"),
                "targetInterface": action.get("targetInterface") or action.get("target_interface"),
                "riskLevel": risk,
                "reason": action.get("reason") or "",
                "params": action.get("params") or action.get("target") or {},
            })
        query_intents = result.get("queryIntents") or result.get("query_intents") or []
        return {
            "outputType": output_type,
            "reason": str(result.get("reason") or "")[:120],
            "confidence": float(result.get("confidence", 0.0) or 0.0),
            "queryIntents": query_intents if isinstance(query_intents, list) else [],
            "actions": normalized_actions,
        }


def _extract_json_object(content: Any) -> Dict[str, Any]:
    text = str(content or "").strip()
    if text.startswith("```json"):
        text = text[7:].strip()
    if text.startswith("```"):
        text = text[3:].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    try:
        loaded = json.loads(text)
        return loaded if isinstance(loaded, dict) else {}
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            loaded = json.loads(text[start:end + 1])
            return loaded if isinstance(loaded, dict) else {}
        raise

# --- 报告生成 Agent (已按原始提示词 100% 还原) ---

class ReportAgent:
    def __init__(
        self,
        langfuse_config: Optional[Dict[str, Any]] = None,
        observability: Optional[QueryGovernanceObservability] = None,
    ):
        self.observability = observability or QueryGovernanceObservability.from_environment()
        if langfuse_config is None:
            trace_id = f"report-{uuid4().hex}"
            langfuse_config = self.observability.start_batch(
                trace_id,
                [QuerySpec(trace_id, "system", "daily-report")],
                metadata={"operation": "report.daily"},
                trace_name="report",
            ).query_config(trace_id, "report.daily")
        self.langfuse_config = langfuse_config
        base_url = os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:3000/v1")
        api_key = os.getenv("LOCAL_LLM_API_KEY")
        self.llm = ChatOpenAI(model="qwen2.5:14b", openai_api_key=api_key, base_url=base_url, temperature=0.1, max_retries=0)
        self.system_prompt = """
# Role
你是一个资深的数据产品经理与自动化运维专家。你擅长从海量系统治理日志中提取核心业务指标，并能以极高的专业度、严谨的逻辑撰写面向管理层和运营团队的“自动化运维日报”。

# Task
接收系统传入的 5 个快消品品牌的自动化治理 JSON 数据，计算核心转化率，并生成一份固定格式、适合在“飞书群机器人”中推送的 text 运维简报。

# Input Data Format
输入为包含 5 个品牌数据的 JSON 对象/数组，每个品牌的数据结构如下：
- `total_err_queries`: 今日发现的数据异常 Query 总数
- `auto_processed_queries`: 成功执行了自动化诊断的 Query 数量
- `blacklisted_products`: 累计拉黑商品总数
- `demoted_products`: 累计降低分层商品总数
- `recovered_products`: 累计恢复分层商品总数
- `unhealthy_queries_after_retry`: 经过多次复查后依然不符合预期的 Query 列表 (Array)

# Workflow
1. 全局数据汇总 (Data Aggregation)：
   遍历所有品牌的数据，计算出全局的：异常Query总数、自动化处理总数、拉黑总数、降维总数、恢复总数，以及“依然不健康的Query”总数。
2. 核心率值计算 (Metrics Calculation)：
   - 总体自动化处理率 = (自动化处理总数 / 异常Query总数) * 100%，保留1位小数。
3. 撰写高管摘要 (Executive Summary)：
   基于汇总数据和率值，生成一段 50-80 字的专业运维总结。必须客观陈述系统的运转健康度与治理成效。
4. 长列表截断处理 (List Truncation)：
   对于每个品牌的 `unhealthy_queries_after_retry` 列表，统计其真实长度（即该品牌遗留不健康Query的总数）。如果列表长度超过 10 个，在输出时仅展示前 10 个，并折叠提示剩余数量。
5. 输出渲染 (Rendering)：
   严格按照下方 `Output Format` 定义的模板生成最终排版，确保在飞书 PC 端和移动端均具备良好的阅读体验。

# Constraints
1. **绝对忠于数据**：只能基于提供的 JSON 数据进行计算和汇总，严禁捏造或脑补任何数据（禁止幻觉）。若输入数据为 0，则如实展示。
2. **专业汇报语调**：必须保持严格专业的业务汇报口吻。**严禁使用任何口语化的比喻、俏皮话或不恰当的拟人化修辞**（例如：禁止使用“给系统洗了个澡”、“打了个补丁”等表述），请使用标准的系统运维和运营术语。
3. **列表展示上限**：任何品牌名下的不健康 Query 列表，最多展示 10 条。

# Output Format (飞书 Markdown Template)
请严格使用以下 text 模板进行输出（括号及内部说明为需要替换的变量）：
···
一、各品牌自动化运维数据：
1. ATLAS
- 异常 Query 总数量：{total_err_queries} 个
- 商品拉黑数量：{blacklisted_products} 个
- 商品排序降低数量：{demoted_products} 个
- 商品排序恢复数量：{recovered_products} 个
- 自动化处理的商品总数量：{auto_processed_queries} 个
- 二次复查后仍然异常的 Query 数量：{unhealthy_queries_count} 个
- 二次复查后仍然异常的 Query （展示10个）：[queryq1, query2, ..., query10]（共 {unhealthy_queries_count} 个，剩余 {unhealthy_queries_count - 10} 个未展示）

2. Meadow
- 异常 Query 总数量：{total_err_queries} 个
- 商品拉黑数量：{blacklisted_products} 个
- 商品排序降低数量：{demoted_products} 个
- 商品排序恢复数量：{recovered_products} 个
- 自动化处理的商品总数量：{auto_processed_queries} 个
- 二次复查后仍然异常的 Query 数量：{unhealthy_queries_count} 个
- 二次复查后仍然异常的 Query （展示10个）：[queryq1, query2, ..., query10]（共 {unhealthy_queries_count} 个，剩余 {unhealthy_queries_count - 10} 个未展示）

3. Harbor
- 异常 Query 总数量：{total_err_queries} 个
- 商品拉黑数量：{blacklisted_products} 个
- 商品排序降低数量：{demoted_products} 个
- 商品排序恢复数量：{recovered_products} 个
- 自动化处理的商品总数量：{auto_processed_queries} 个
- 二次复查后仍然异常的 Query 数量：{unhealthy_queries_count} 个
- 二次复查后仍然异常的 Query （展示10个）：[queryq1, query2, ..., query10]（共 {unhealthy_queries_count} 个，剩余 {unhealthy_queries_count - 10} 个未展示）

4. Summit
- 异常 Query 总数量：{total_err_queries} 个
- 商品拉黑数量：{blacklisted_products} 个
- 商品排序降低数量：{demoted_products} 个
- 商品排序恢复数量：{recovered_products} 个
- 自动化处理的商品总数量：{auto_processed_queries} 个
- 二次复查后仍然异常的 Query 数量：{unhealthy_queries_count} 个
- 二次复查后仍然异常的 Query （展示10个）：[queryq1, query2, ..., query10]（共 {unhealthy_queries_count} 个，剩余 {unhealthy_queries_count - 10} 个未展示）

5. Riviera
- 异常 Query 总数量：{total_err_queries} 个
- 商品拉黑数量：{blacklisted_products} 个
- 商品排序降低数量：{demoted_products} 个
- 商品排序恢复数量：{recovered_products} 个
- 自动化处理的商品总数量：{auto_processed_queries} 个
- 二次复查后仍然异常的 Query 数量：{unhealthy_queries_count} 个
- 二次复查后仍然异常的 Query （展示10个）：[queryq1, query2, ..., query10]（共 {unhealthy_queries_count} 个，剩余 {unhealthy_queries_count - 10} 个未展示）

----------------

二、数据总结
今日系统共监测到各品牌数据异常 Query {total_sum} 条，成功自动化处理 {processed_sum} 条，整体自动化处理率为 {rate}%。系统共拉黑异常商品 {black_sum} 个，降级 {demote_sum} 个，恢复 {recover_sum} 个。目前仍有 {unhealthy_sum} 条 Query 需人工进一步介入。

----------------

3、运维汇总
[本次运维的总结和注意事项]
···
"""

    def generate(self, stats: Any) -> str:
        """根据 5 品牌 JSON 数据生成符合飞书模板的 Markdown 报告"""
        try:
            # 输入数据转为 JSON 字符串供模型阅读
            data_str = json.dumps(stats, ensure_ascii=False, indent=2)
            
            with EnvContext(['http_proxy', 'https_proxy', 'all_proxy']):
                invoke_kwargs = {"config": self.langfuse_config} if self.langfuse_config else {}
                response = self.llm.invoke([
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": f"请处理以下数据并生成报告：\n{data_str}"}
                ], **invoke_kwargs)
                return response.content.strip()
        except Exception as e:
            logging.error(f"ReportAgent generate failed: {e}")
            return "自动化运维报告生成失败，请检查 Agent 逻辑或网络。"
