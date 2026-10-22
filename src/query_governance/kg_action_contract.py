"""Validate model proposals against the writable KG API contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


ACTION_ENDPOINTS = {
    "add_tag": "/admin/tag/addTag",
    "update_tag": "/admin/tag/updateTag",
    "blacklist_tag": "/admin/tag/updateTagInfo",
    "add_relationship": "/admin/tagRelationship/addRelationship",
    "update_relationship": "/admin/tagRelationship/update",
    "delete_relationship": "/admin/tagRelationship/delete",
}
RELATION_TYPES = {"902227092548485120", "902227138815852544"}


def normalize_kg_action(value: Mapping[str, Any]) -> dict[str, Any]:
    action_type = str(value.get("actionType") or "").strip()
    if action_type not in ACTION_ENDPOINTS:
        raise ValueError(f"unsupported KG action: {action_type}")
    params = value.get("params")
    if not isinstance(params, Mapping):
        raise ValueError(f"{action_type} requires params")
    params = dict(params)
    for field in ("dimensions", "dimensionNames"):
        current = params.get(field)
        if isinstance(current, list):
            if len(current) != 1 or not current[0]:
                raise ValueError(f"{field} must contain exactly one dimension ID")
            params[field] = current[0]
    required: tuple[str, ...]
    if action_type == "add_tag":
        required = ("name", "viewTag", "sceneId", "dimensions", "isEnglish", "isRoot", "segmentingAgain", "isCompose", "weight")
    elif action_type == "update_tag":
        required = ("id", "name", "viewTag", "sceneId", "dimensions", "isEnglish", "isRoot", "segmentingAgain", "isCompose", "weight")
    elif action_type == "blacklist_tag":
        required = ("id", "sceneId", "tagId", "isBlack")
    elif action_type == "delete_relationship":
        required = ("id",)
    else:
        required = ("sceneId", "relationships", "tagId", "tagName", "dimensionNames", "responseTagId", "responseTagName")
        if action_type == "update_relationship":
            required += ("id",)
    missing = [key for key in required if key not in params or params[key] is None or params[key] == ""]
    if missing:
        raise ValueError(f"{action_type} missing API fields: {', '.join(missing)}")
    if action_type == "blacklist_tag":
        expected = 1 if action_type == "blacklist_tag" else 0
        if params["isBlack"] not in (expected, str(expected)):
            raise ValueError(f"{action_type} requires isBlack={expected}")
    elif action_type in {"add_tag", "update_tag"}:
        if params["isEnglish"] not in (0, 1):
            raise ValueError("isEnglish must be 0 or 1")
        if str(params["isRoot"]) not in {"0", "1"} or str(params["segmentingAgain"]) not in {"0", "1"}:
            raise ValueError("tag flags must be 0 or 1")
        if str(params["isCompose"]) not in {"0", "1"}:
            raise ValueError("isCompose must be 0 or 1")
        if not isinstance(params["weight"], int) or isinstance(params["weight"], bool):
            raise ValueError("weight must be an integer")
        if str(params["isCompose"]) == "1":
            if not isinstance(params.get("nameAnd"), list) or not params["nameAnd"]:
                raise ValueError("composed tag requires nameAnd")
            if not isinstance(params.get("nameNot"), list):
                raise ValueError("composed tag requires nameNot")
        params.setdefault("nameAnd", [])
        params.setdefault("nameNot", [])
    elif action_type in {"add_relationship", "update_relationship"}:
        if str(params["relationships"]) not in RELATION_TYPES:
            raise ValueError("relationship must be synonym or subordinate")
        if str(params["tagId"]) == str(params["responseTagId"]):
            raise ValueError("relationship endpoints must differ")
    result = {"actionType": action_type, "targetInterface": ACTION_ENDPOINTS[action_type], "params": params}
    for key in ("reason", "dependsOn", "rollbackPlan", "riskLevel", "title"):
        if key in value:
            result[key] = value[key]
    return result


def normalize_kg_actions(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        raise ValueError("actions must be an array")
    if any(not isinstance(item, Mapping) for item in values):
        raise ValueError("each KG action must be an object")
    return [normalize_kg_action(item) for item in values]
