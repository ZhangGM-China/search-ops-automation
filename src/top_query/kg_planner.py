from typing import Any, Dict, List, Optional, Tuple

from src.integrations.knowledge_graph import KnowledgeGraphClient


class TopQueryKnowledgeGraphPlanner:
    """Top Query 场景的知识图谱写入方案生成器。

    这里只生成可审计的 action payload，不执行写接口。所有图谱写入仍由
    TopQueryGovernanceService 在人工确认后统一执行。
    """

    RELATION_SUBORDINATE = KnowledgeGraphClient.RELATION_SUBORDINATE

    def __init__(self, kg_client: Optional[Any] = None):
        self.kg_client = kg_client or KnowledgeGraphClient()

    def plan(self, intent: Dict[str, Any], context: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        brand = str(context.get("brand") or intent.get("brand") or "").strip()
        query = str(intent.get("query") or context.get("query") or "").strip()
        intent_type = str(intent.get("intent_type") or "").strip()
        if intent_type not in {"expand_recall", "world_knowledge_missing_tag"}:
            return [], {
                "queryToolsCalled": 0,
                "missingRequiredFields": [],
                "skippedReasons": ["unsupported_legacy_intent"],
                "graphContext": {
                    "tags": [],
                    "relationshipCandidates": [],
                    "dimensions": [],
                },
            }
        planner_context = {**context, "kg_intent": intent}
        candidates = self._candidate_tags(query, planner_context)
        graph_context = self._fetch_graph_context(brand, candidates)
        diagnostics = {
            "queryToolsCalled": len(candidates) * 2 + 1,
            "missingRequiredFields": [],
            "skippedReasons": [],
            "graphContext": self._summarize_graph_context(graph_context),
        }

        if intent_type == "expand_recall":
            actions = self._plan_recall_action(brand, query, intent, graph_context, diagnostics)
        else:
            diagnostics["worldKnowledgeIntent"] = True
            actions = self._plan_world_knowledge_actions(brand, query, intent, graph_context, diagnostics)

        return actions, diagnostics

    def _plan_world_knowledge_actions(
        self,
        brand: str,
        query: str,
        intent: Dict[str, Any],
        graph_context: Dict[str, Any],
        diagnostics: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        follow_up = intent.get("follow_up_tag_bucket_config")
        dimension = self._first_dimension(graph_context)
        actions: List[Dict[str, Any]] = []

        for tag in intent.get("missing_tags") or []:
            tag_name = str(tag).strip()
            if not tag_name:
                continue
            if self._tag_item(graph_context, tag_name):
                diagnostics["skippedReasons"].append(f"tag_already_exists:{tag_name}")
                continue
            if not dimension.get("id"):
                diagnostics["missingRequiredFields"].append("dimensions")
                continue
            actions.append({
                "actionType": "add_tag",
                "targetInterface": "/admin/tag/addTag",
                "riskLevel": "medium",
                "reason": f"世界知识建议提升 {query} 的 {tag_name} 标签，但知识图谱缺少该标签，需先创建",
                "params": {
                    "name": tag_name,
                    "viewTag": tag_name,
                    "dimensions": dimension["id"],
                },
                "evidence": {
                    "rootCause": intent.get("reason"),
                    "openFactorContext": intent.get("open_factor_context") or {},
                    "graphContext": diagnostics["graphContext"],
                },
                "expectedImpact": "补齐世界知识推荐标签，为后续当前 Query 标签权重调整提供可匹配标签",
                "negativeImpact": "新增 KG 标签会影响其他依赖该标签的 Query，需人工确认",
                "rollbackSuggestion": "如标签引入异常，可按执行返回 rollback payload 拉黑新增标签",
                "followUpTagBucketConfig": follow_up,
            })

        for relation in intent.get("missing_relations") or []:
            source = str(relation.get("source") or relation.get("tagName") or "").strip()
            target = str(relation.get("target") or relation.get("responseTagName") or "").strip()
            if not source or not target:
                diagnostics["missingRequiredFields"].append("tagName")
                continue
            source_tag = self._tag_item(graph_context, source)
            target_tag = self._tag_item(graph_context, target)
            dimension_names = self._relationship_dimension_names({}, source_tag or target_tag)
            actions.append({
                "actionType": "add_relationship",
                "targetInterface": "/admin/tagRelationship/addRelationship",
                "riskLevel": "high" if not self._tag_id(source_tag) or not self._tag_id(target_tag) else "medium",
                "reason": f"世界知识建议提升 {query} 的 {source} 标签，需要补齐 {source} 与 {target} 的知识图谱关系",
                "params": {
                    "relationships": self.RELATION_SUBORDINATE,
                    "tagId": self._tag_id(source_tag),
                    "tagName": source,
                    "dimensionNames": dimension_names,
                    "responseTagId": self._tag_id(target_tag),
                    "responseTagName": target,
                    "privateIds": [],
                    "privateId": "",
                    "privateName": "",
                    "dependsOnTagName": source if not self._tag_id(source_tag) else "",
                },
                "evidence": {
                    "rootCause": intent.get("reason"),
                    "openFactorContext": intent.get("open_factor_context") or {},
                    "graphContext": diagnostics["graphContext"],
                },
                "expectedImpact": "补齐标签关系后再执行当前 Query 标签权重调整，提升目标标签商品排序",
                "negativeImpact": "标签关系会影响多个 Query，需人工确认执行顺序",
                "rollbackSuggestion": "如召回异常，可删除本次新增标签关系",
                "followUpTagBucketConfig": follow_up,
            })

        if not actions:
            diagnostics["missingRequiredFields"].append("worldKnowledgeKgActions")
        return actions

    def _fetch_graph_context(self, brand: str, candidates: List[str]) -> Dict[str, Any]:
        context = {"tags": [], "relationships": [], "dimensions": []}
        for candidate in candidates:
            context["tags"].append({
                "candidate": candidate,
                "result": self.kg_client.execute({
                    "brand": brand,
                    "actionType": "query_tag",
                    "params": {"name": candidate, "nameAcc": candidate, "pageNum": 1, "pageSize": 30},
                }),
            })
            context["relationships"].append({
                "candidate": candidate,
                "result": self.kg_client.execute({
                    "brand": brand,
                    "actionType": "query_relationship",
                    "params": {"tagName": candidate, "pageNum": 1, "pageSize": 30},
                }),
            })
        context["dimensions"].append(self.kg_client.execute({
            "brand": brand,
            "actionType": "query_dimension",
            "params": {"pageNum": 1, "pageSize": 100},
        }))
        return context

    def _plan_recall_action(
        self,
        brand: str,
        query: str,
        intent: Dict[str, Any],
        graph_context: Dict[str, Any],
        diagnostics: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if not query:
            diagnostics["missingRequiredFields"].append("name")
            return []
        if self._tag_item(graph_context, query):
            diagnostics["skippedReasons"].append("tag_already_exists")
            return []
        dimension = self._first_dimension(graph_context)
        if not dimension.get("id"):
            diagnostics["missingRequiredFields"].append("dimensions")
            return []
        return [{
            "actionType": "add_tag",
            "targetInterface": "/admin/tag/addTag",
            "riskLevel": "low",
            "reason": f"{query} 召回不足，知识图谱中缺少可匹配的 Query 标签",
            "params": {
                "name": query,
                "viewTag": query,
                "dimensions": dimension["id"],
            },
            "evidence": {
                "rootCause": intent.get("reason"),
                "graphContext": diagnostics["graphContext"],
            },
            "expectedImpact": "补足 Query 标签召回，让相关商品具备进入候选集的基础条件",
            "negativeImpact": "新增标签可能扩大召回范围，需要执行后观察相关性",
            "rollbackSuggestion": "如召回异常，可按执行返回的 rollback payload 拉黑新增标签",
        }]


    def _candidate_tags(self, query: str, context: Dict[str, Any]) -> List[str]:
        candidates = [item for item in query.split() if item.strip()] or ([query] if query else [])
        intent = context.get("kg_intent") if isinstance(context.get("kg_intent"), dict) else {}
        for item in intent.get("missing_tags") or []:
            candidates.append(str(item))
        for relation in intent.get("missing_relations") or []:
            if isinstance(relation, dict):
                candidates.extend([str(relation.get("source") or ""), str(relation.get("target") or "")])
        for product in context.get("products") or []:
            for key in ("tag_name", "tagName"):
                value = product.get(key)
                if isinstance(value, str):
                    candidates.extend(part.strip() for part in value.replace("，", ",").split(",") if part.strip())
        return list(dict.fromkeys(candidates))


    def _summarize_graph_context(self, context: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "tagCandidates": [
                {"candidate": item.get("candidate"), "resultCount": self._result_count(item.get("result"))}
                for item in context.get("tags", [])
            ],
            "relationshipCandidates": [
                {"candidate": item.get("candidate"), "resultCount": self._result_count(item.get("result"))}
                for item in context.get("relationships", [])
            ],
            "dimensionResultCount": sum(self._result_count(item) for item in context.get("dimensions", [])),
        }

    def _tag_item(self, context: Dict[str, Any], candidate: str) -> Dict[str, Any]:
        for item in context.get("tags", []):
            if item.get("candidate") != candidate:
                continue
            rows = self._items(item.get("result"))
            return rows[0] if rows else {}
        return {}

    def _first_dimension(self, context: Dict[str, Any]) -> Dict[str, Any]:
        for result in context.get("dimensions", []):
            rows = self._items(result)
            if rows:
                row = rows[0]
                return {
                    "id": row.get("id") or row.get("_key") or row.get("key") or row.get("dimensionId"),
                    "name": row.get("name") or row.get("dimensionName"),
                }
        return {}

    @staticmethod
    def _relationship_dimension_names(relationship: Dict[str, Any], source_tag: Dict[str, Any]) -> str:
        value = relationship.get("dimensionNames") or relationship.get("dimension_names")
        if isinstance(value, list):
            return str(value[0] if value else "")
        if value:
            return str(value)
        dimensions = source_tag.get("dimensionNames") or source_tag.get("dimension_names") or source_tag.get("dimensions")
        if isinstance(dimensions, list):
            return str(dimensions[0] if dimensions else "")
        return str(dimensions or "")

    @staticmethod
    def _tag_id(tag: Dict[str, Any]) -> str:
        return str(
            tag.get("id")
            or tag.get("_key")
            or tag.get("key")
            or tag.get("tagId")
            or tag.get("tag_id")
            or ""
        ).replace("kg_tag/", "").strip()

    @classmethod
    def _result_count(cls, result: Any) -> int:
        return len(cls._items(result))

    @staticmethod
    def _items(result: Any) -> List[Dict[str, Any]]:
        if not isinstance(result, dict):
            return []
        data = result.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            rows = data.get("rows") or data.get("list")
            if isinstance(rows, list):
                return [item for item in rows if isinstance(item, dict)]
            return [data]
        return []
