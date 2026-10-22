import hashlib
import json
import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

from src.integrations.business_http import (
    BusinessCircuitOpen,
    BusinessRequestGovernor,
    CircuitRegistry,
)


class KnowledgeGraphClient:
    """知识图谱接口客户端，负责 action 到真实 API 的映射。"""

    RELATION_SUBORDINATE = "902227092548485120"
    RELATION_SYNONYM = "902227138815852544"
    RELATION_MUTEX = "902227674009042944"

    FIXED_SCENE_IDS = {
        "summit": "1258129681140809728",
        "meadow": "1445369145549258752",
    }

    ACTION_ENDPOINTS = {
        "add_tag": "/admin/tag/addTag",
        "update_tag": "/admin/tag/updateTag",
        "blacklist_tag": "/admin/tag/updateTagInfo",
        "unblacklist_tag": "/admin/tag/updateTagInfo",
        "add_relationship": "/admin/tagRelationship/addRelationship",
        "update_relationship": "/admin/tagRelationship/update",
        "delete_relationship": "/admin/tagRelationship/delete",
        "query_tag": "/admin/tag/query",
        "query_relationship": "/admin/tagRelationship/query",
        "query_dimension": "/admin/dimension/query",
    }

    def __init__(
        self,
        http_client: Optional[Any] = None,
        token: Optional[str] = None,
        base_url: Optional[str] = None,
        request_governor: Optional[BusinessRequestGovernor] = None,
        circuit_registry: Optional[CircuitRegistry] = None,
        environment: Optional[str] = None,
    ):
        from src.config import GLOBAL_SAFETY_CONFIG, get_env

        self.http_client = http_client or requests
        self.token = token or os.getenv("KG_API_TOKEN", "")
        self.base_url = (base_url or os.getenv("KG_BASE_URL") or "https://kg.example.test").rstrip("/")
        self.request_governor = request_governor or BusinessRequestGovernor()
        self.circuit_registry = circuit_registry or CircuitRegistry(
            failure_threshold=int(GLOBAL_SAFETY_CONFIG.get("circuit_breaker_threshold", 10)),
            cooldown_seconds=float(GLOBAL_SAFETY_CONFIG.get("circuit_breaker_cooldown_seconds", 60)),
        )
        self.environment = environment or get_env()
        self.max_retries = int(GLOBAL_SAFETY_CONFIG.get("max_retry_count", 3))
        self._scene_cache: Dict[str, str] = {}

    def snapshot(self, action: Dict[str, Any]) -> Dict[str, Any]:
        action_type = self._action_type(action)
        brand = action.get("brand") or action.get("target", {}).get("brand")
        if action_type in {"add_tag", "update_tag", "blacklist_tag", "unblacklist_tag"}:
            params = self._params(action)
            query_params = {
                "sceneId": params.get("sceneId") or self.get_scene_id(brand),
                "nameAcc": params.get("name") or "",
                "pageNum": 1,
                "pageSize": 30,
            }
            if params.get("id"):
                query_params["id"] = params.get("id")
            return self._post("/admin/tag/query", query_params)
        if action_type in {"add_relationship", "update_relationship", "delete_relationship"}:
            params = self._params(action)
            query_params = {
                "sceneId": params.get("sceneId") or self.get_scene_id(brand),
                "pageNum": 1,
                "pageSize": 30,
            }
            if params.get("id"):
                query_params["id"] = params.get("id")
            if params.get("tagName"):
                query_params["tagNameAcc"] = params.get("tagName")
            if params.get("responseTagName"):
                query_params["resTagName"] = params.get("responseTagName")
            if params.get("relationships"):
                query_params["relationshipId"] = params.get("relationships")
            return self._post("/admin/tagRelationship/query", query_params)
        return {"target": action.get("target") or {}, "snapshotSource": "not_required"}

    def execute(
        self,
        action: Dict[str, Any],
        external_request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        action_type = self._action_type(action)
        endpoint = self.ACTION_ENDPOINTS.get(action_type)
        if not endpoint:
            return {"code": -1, "message": f"unsupported knowledge graph action: {action_type}"}

        params = self._params(action)
        brand = action.get("brand") or action.get("target", {}).get("brand") or params.get("brand")
        if endpoint not in {"/admin/tagRelationship/delete"} and not params.get("sceneId"):
            params["sceneId"] = self.get_scene_id(brand)
        if action_type == "add_tag":
            params = self._normalize_add_tag_params(params)
        if action_type == "blacklist_tag":
            params["isBlack"] = 1
        if action_type == "unblacklist_tag":
            params["isBlack"] = 0
        return self._post(endpoint, params, external_request_id=external_request_id)

    def build_rollback(
        self,
        action: Dict[str, Any],
        snapshot: Dict[str, Any],
        response: Dict[str, Any],
    ) -> Dict[str, Any]:
        action_type = self._action_type(action)
        params = self._params(action)
        brand = action.get("brand") or params.get("brand")

        if action_type == "add_relationship":
            relationship_id = self._response_id(response) or params.get("id") or params.get("relationshipId")
            return {
                "brand": brand,
                "actionType": "delete_relationship",
                "params": {"id": relationship_id} if relationship_id else {
                    "requiresLookup": True,
                    **params,
                },
            }
        if action_type == "delete_relationship":
            relationship = self._first_snapshot_item(snapshot)
            return {
                "brand": brand,
                "actionType": "add_relationship",
                "params": self._relationship_to_add_params(relationship, params),
            }
        if action_type == "update_relationship":
            relationship = self._first_snapshot_item(snapshot)
            rollback_params = self._relationship_to_update_params(relationship, params)
            return {"brand": brand, "actionType": "update_relationship", "params": rollback_params}
        if action_type == "add_tag":
            tag_id = self._response_tag_id(response, params)
            return {
                "brand": brand,
                "actionType": "blacklist_tag",
                "params": tag_id,
            }
        if action_type in {"update_tag", "blacklist_tag", "unblacklist_tag"}:
            tag = self._first_snapshot_item(snapshot)
            return {"brand": brand, "actionType": "update_tag", "params": self._tag_to_update_params(tag, params)}

        return {
            "brand": brand,
            "actionType": f"rollback_{action_type}",
            "params": params,
            "snapshot": snapshot,
            "response": response,
        }

    def detect_conflict(self, action: Dict[str, Any], before_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        return {"hasConflict": False}

    def get_scene_id(self, brand: Optional[str]) -> str:
        brand_key = str(brand or "").lower()
        if brand_key in self.FIXED_SCENE_IDS:
            return self.FIXED_SCENE_IDS[brand_key]
        if brand_key in self._scene_cache:
            return self._scene_cache[brand_key]
        if not brand_key:
            return ""

        from src.integrations.operational_service import OperationalService
        op_service = OperationalService()
        url = f"{op_service._get_kg_config_base_url(brand_key)}/admin-api/config/new/word/get/cfg_kg_key"
        request_key = op_service._business_request_key(brand_key, url)
        permit = self.circuit_registry.admit(request_key)
        if permit is None:
            raise BusinessCircuitOpen(f"business circuit open: {request_key}")
        try:
            response = self.request_governor.request_with_rate_limit_retry(
                key=request_key,
                method="GET",
                url=url,
                request_func=lambda method, target_url, **request_kwargs: self.http_client.get(
                    target_url,
                    **request_kwargs,
                ),
                max_attempts=self.max_retries,
                request_timeout=15.0,
                headers={**op_service._get_headers(brand_key), "pragma": "no-cache"},
                timeout=15,
            )
        except Exception:
            self.circuit_registry.record_failure(request_key, permit)
            raise
        try:
            payload = response.json()
            data = payload.get("data") or "{}"
            parsed = json.loads(data) if isinstance(data, str) else data
            scene_id = str(parsed.get("sceneId") or "")
        except Exception:
            self._record_circuit_outcome(
                request_key,
                response,
                permit,
                malformed_success=True,
            )
            raise
        self._record_circuit_outcome(request_key, response, permit)
        self._scene_cache[brand_key] = scene_id
        return scene_id

    def _post(
        self,
        endpoint: str,
        params: Dict[str, Any],
        external_request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        headers = self._headers()
        if external_request_id:
            headers["Idempotency-Key"] = external_request_id
            headers["X-Request-ID"] = external_request_id
        url = f"{self.base_url}{endpoint}"
        token_hash = hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:16]
        request_key = f"{self.environment}|{urlparse(url).netloc.lower()}|kg:{token_hash}"
        permit = self.circuit_registry.admit(request_key)
        if permit is None:
            raise BusinessCircuitOpen(f"business circuit open: {request_key}")
        try:
            response = self.request_governor.request_with_rate_limit_retry(
                key=request_key,
                method="POST",
                url=url,
                request_func=lambda method, target_url, **request_kwargs: self.http_client.post(
                    target_url,
                    **request_kwargs,
                ),
                max_attempts=self.max_retries,
                request_timeout=15.0,
                json={"params": params},
                headers=headers,
                timeout=15,
            )
        except Exception:
            self.circuit_registry.record_failure(request_key, permit)
            raise
        try:
            payload = response.json()
        except Exception:
            self._record_circuit_outcome(
                request_key,
                response,
                permit,
                malformed_success=True,
            )
            return {"code": -1, "message": getattr(response, "text", "")}
        if not isinstance(payload, dict):
            self._record_circuit_outcome(
                request_key,
                response,
                permit,
                malformed_success=True,
            )
            return {"code": -1, "message": getattr(response, "text", "")}
        self._record_circuit_outcome(request_key, response, permit)
        return payload

    def _record_circuit_outcome(
        self,
        request_key: str,
        response: Any,
        permit: Any,
        *,
        malformed_success: bool = False,
    ) -> None:
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code >= 500 or (status_code == 200 and malformed_success):
            self.circuit_registry.record_failure(request_key, permit)
        elif status_code == 429:
            self.circuit_registry.record_rate_limited(request_key, permit)
        else:
            self.circuit_registry.record_success(request_key, permit)

    def _headers(self) -> Dict[str, str]:
        return {
            "authorization": f"Bearer {self.token}",
            "content-type": "application/json;charset=UTF-8",
        }

    @staticmethod
    def _action_type(action: Dict[str, Any]) -> str:
        return str(action.get("actionType") or action.get("action_type") or "")

    @staticmethod
    def _params(action: Dict[str, Any]) -> Dict[str, Any]:
        params = action.get("params") or action.get("target") or {}
        return dict(params)

    @staticmethod
    def _normalize_add_tag_params(params: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(params)
        tag_name = normalized.get("name") or normalized.get("tagName")
        if tag_name:
            normalized["name"] = tag_name
            normalized.setdefault("viewTag", tag_name)
        dimension = normalized.get("dimensions") or normalized.get("dimensionId")
        if dimension:
            normalized["dimensions"] = dimension
        if "isEnglish" not in normalized:
            normalized["isEnglish"] = 0
        normalized.setdefault("isRoot", "1")
        normalized.setdefault("segmentingAgain", "1")
        normalized.setdefault("isCompose", "0")
        normalized.setdefault("weight", 200)
        normalized.setdefault("nameAnd", [])
        normalized.setdefault("nameNot", [])
        normalized.setdefault("nameOr", "")
        normalized.setdefault("vector", "")
        normalized.setdefault("luansheng", "")
        normalized.setdefault("englishName", "")
        normalized.setdefault("japaneseName", "")
        normalized.setdefault("yueName", "")
        normalized.setdefault("pinyin", "")
        normalized.setdefault("privateIds", [])
        normalized.setdefault("englishHom", "")
        normalized.setdefault("japaneseHom", "")
        normalized.setdefault("yueHom", "")
        normalized.setdefault("homophonic", "")
        normalized.setdefault("privateId", "")
        normalized.setdefault("privateName", "")
        normalized.setdefault("extendType", "")
        normalized.setdefault("extendContent", "")
        for planner_key in ("tagName", "dimensionId", "dimensionName", "tagDimension"):
            normalized.pop(planner_key, None)
        return normalized

    @staticmethod
    def _first_snapshot_item(snapshot: Dict[str, Any]) -> Dict[str, Any]:
        data = snapshot.get("data")
        if isinstance(data, list) and data:
            return dict(data[0])
        if isinstance(data, dict):
            return dict(data)
        return {}

    @staticmethod
    def _response_id(response: Dict[str, Any]) -> Optional[str]:
        data = response.get("data")
        if isinstance(data, dict):
            return str(data.get("id") or data.get("key") or data.get("_key") or "") or None
        if isinstance(data, str) and data and data != "标签关系插入成功":
            return data
        return None

    @classmethod
    def _response_tag_id(cls, response: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
        data = response.get("data")
        properties = data.get("properties") if isinstance(data, dict) else {}
        data_key = data.get("key") if isinstance(data, dict) else None
        data_id = data.get("id") if isinstance(data, dict) else None
        return {
            "id": properties.get("_key") or data_key or cls._strip_kg_tag_prefix(data_id) or fallback.get("id"),
            "sceneId": properties.get("scene_id") or fallback.get("sceneId"),
            "tagId": properties.get("tag_id") or fallback.get("tagId"),
            "isBlack": 1,
        }

    @classmethod
    def _relationship_to_add_params(cls, relationship: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
        relationships = relationship.get("relationships") or [fallback.get("relationships")]
        tag_dimensions = relationship.get("tagDimensions") or [fallback.get("dimensionNames", "")]
        return {
            "sceneId": relationship.get("sceneId") or fallback.get("sceneId"),
            "relationships": relationships[0] if isinstance(relationships, list) else relationships,
            "privateIds": relationship.get("privateId") or fallback.get("privateIds") or [],
            "tagId": cls._strip_kg_tag_prefix(relationship.get("parent")) or fallback.get("tagId"),
            "tagName": relationship.get("tagName") or fallback.get("tagName"),
            "dimensionNames": tag_dimensions[0] if isinstance(tag_dimensions, list) and tag_dimensions else fallback.get("dimensionNames", ""),
            "responseTagId": cls._strip_kg_tag_prefix(relationship.get("child")) or fallback.get("responseTagId"),
            "responseTagName": relationship.get("responseTagName") or fallback.get("responseTagName"),
            "privateId": "",
            "privateName": "",
        }

    @classmethod
    def _relationship_to_update_params(cls, relationship: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
        params = cls._relationship_to_add_params(relationship, fallback)
        params["id"] = relationship.get("id") or fallback.get("id")
        return params

    @staticmethod
    def _tag_to_update_params(tag: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
        if not tag:
            return fallback
        dimensions = tag.get("dimensions") or []
        return {
            "id": tag.get("id") or fallback.get("id"),
            "isEnglish": tag.get("isEnglish", fallback.get("isEnglish", 0)),
            "name": tag.get("tagName") or fallback.get("name"),
            "isCompose": str(tag.get("isCompose", fallback.get("isCompose", "0"))),
            "isRoot": str(tag.get("isRoot", fallback.get("isRoot", "1"))),
            "segmentingAgain": str(tag.get("segmentingAgain", fallback.get("segmentingAgain", "1"))),
            "homophonic": tag.get("homophonic") or fallback.get("homophonic", ""),
            "luansheng": tag.get("luansheng") or fallback.get("luansheng", ""),
            "sceneId": tag.get("sceneId") or fallback.get("sceneId"),
            "weight": tag.get("weight", fallback.get("weight", 200)),
            "dimensions": dimensions[0] if isinstance(dimensions, list) and dimensions else fallback.get("dimensions"),
            "vector": tag.get("vector", fallback.get("vector", 0)),
            "nameOr": tag.get("nameOr") or fallback.get("nameOr", ""),
            "nameNot": tag.get("nameNot") or fallback.get("nameNot", []),
            "nameAnd": tag.get("nameAnd") or fallback.get("nameAnd", []),
            "viewTag": tag.get("viewTag") or fallback.get("viewTag") or tag.get("tagName"),
            "privateIds": tag.get("privateId") or fallback.get("privateIds", []),
            "privateId": "",
            "privateName": "",
            "extendType": tag.get("extendType") or fallback.get("extendType", ""),
            "extendContent": tag.get("extendContent") or fallback.get("extendContent", ""),
        }

    @staticmethod
    def _strip_kg_tag_prefix(value: Optional[str]) -> Optional[str]:
        if not value:
            return value
        return str(value).replace("kg_tag/", "")


class NullKnowledgeGraphClient:
    """默认知识图谱客户端，仅用于本地测试和 dry-run 留痕。"""

    def snapshot(self, action: Dict[str, Any]) -> Dict[str, Any]:
        return {"target": action.get("target") or {}, "snapshotSource": "dry_run"}

    def execute(
        self,
        action: Dict[str, Any],
        external_request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {"code": 1, "message": "dry_run_success"}

    def build_rollback(
        self,
        action: Dict[str, Any],
        snapshot: Dict[str, Any],
        response: Dict[str, Any],
    ) -> Dict[str, Any]:
        action_type = action.get("actionType") or action.get("action_type") or "unknown"
        return {
            "actionType": f"rollback_{action_type}",
            "target": action.get("target") or {},
            "snapshot": snapshot,
            "response": response,
        }

    def detect_conflict(self, action: Dict[str, Any], before_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        return {"hasConflict": False}


class DryRunKnowledgeGraphClient:
    """dry-run 客户端：查询走真实只读客户端，写入动作在客户端层继续阻断。"""

    QUERY_ACTIONS = {"query_tag", "query_relationship", "query_dimension"}

    def __init__(self, query_client: Optional[Any] = None, fallback_client: Optional[Any] = None):
        self.query_client = query_client or KnowledgeGraphClient()
        self.fallback_client = fallback_client or NullKnowledgeGraphClient()

    def snapshot(self, action: Dict[str, Any]) -> Dict[str, Any]:
        return self.fallback_client.snapshot(action)

    def execute(
        self,
        action: Dict[str, Any],
        external_request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        action_type = action.get("actionType") or action.get("action_type")
        if action_type in self.QUERY_ACTIONS:
            if external_request_id:
                return self.query_client.execute(action, external_request_id=external_request_id)
            return self.query_client.execute(action)
        return {"code": 0, "message": "dry_run_write_blocked"}

    def build_rollback(
        self,
        action: Dict[str, Any],
        snapshot: Dict[str, Any],
        response: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self.fallback_client.build_rollback(action, snapshot, response)

    def detect_conflict(self, action: Dict[str, Any], before_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        if hasattr(self.query_client, "detect_conflict"):
            return self.query_client.detect_conflict(action, before_snapshot)
        return self.fallback_client.detect_conflict(action, before_snapshot)
