"""Compatibility projection over the new engineering-owned fact service.

The legacy Top Query context still imports this module while the deterministic
workflow is being rolled out. External facts now come only from
``src.query_governance.facts``; this module no longer owns provider processes.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.query_governance.facts import (
    DEFAULT_WEATHER_CITIES,
    WorldFactsService,
)


WORLD_KNOWLEDGE_SNAPSHOT_SCHEMA_VERSION = 2


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _normalize_fingerprint_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_fingerprint_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_fingerprint_value(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("fingerprint values must be finite")
        if value.is_integer():
            return int(value)
    return value


@dataclass(frozen=True)
class WorldKnowledgeBatchSnapshot:
    snapshot_id: str
    collected_at: datetime.datetime
    schema_version: int
    brand: str
    data_date: datetime.date
    knowledge_date: datetime.date
    scope_id: str
    config_fingerprint: str
    _effective_config_json: str = field(repr=False)
    _shared_facts_json: str = field(repr=False)
    _source_call_counts_json: str = field(repr=False)
    _source_status_json: str = field(repr=False)

    def effective_config(self) -> Dict[str, Any]:
        return json.loads(self._effective_config_json)

    def shared_facts(self) -> Dict[str, Any]:
        return json.loads(self._shared_facts_json)

    def source_call_counts(self) -> Dict[str, int]:
        return json.loads(self._source_call_counts_json)

    def source_status(self) -> Dict[str, str]:
        return json.loads(self._source_status_json)

    def provenance(self) -> Dict[str, Any]:
        return {
            "snapshotId": self.snapshot_id,
            "collectedAt": self.collected_at.isoformat(),
            "schemaVersion": self.schema_version,
            "scopeId": self.scope_id,
            "brand": self.brand,
            "dataDate": self.data_date.isoformat(),
            "knowledgeDate": self.knowledge_date.isoformat(),
            "configFingerprint": self.config_fingerprint,
            "sourceCallCounts": self.source_call_counts(),
            "sourceStatus": self.source_status(),
        }


class WorldKnowledgeService:
    """Keep the legacy context shape while sourcing facts from project code."""

    def __init__(self, facts: Optional[WorldFactsService] = None):
        self.facts = facts or WorldFactsService()

    def collect(
        self,
        brand: str,
        query: str,
        today: datetime.date,
        products: List[Dict[str, Any]],
        config: Dict[str, Any],
        snapshot: Optional[WorldKnowledgeBatchSnapshot] = None,
    ) -> Dict[str, Any]:
        config = config or {}
        if snapshot is not None:
            if brand != snapshot.brand:
                raise ValueError(
                    f"snapshot brand mismatch: expected {snapshot.brand}, got {brand}"
                )
            config = snapshot.effective_config()
            shared = snapshot.shared_facts()
        else:
            shared = self._shared_facts(today, config)
        season = shared["season"]
        weather = shared["weather"]
        festival = shared["festival"]
        active_social_trends = (
            shared["active_social_trends"]
            if "active_social_trends" in shared
            else self._active_social_trends(config, today)
        )
        social_trend = self._social_trend_for_query(active_social_trends, query)
        matched_factors = self._matched_factors(
            query, products, season, weather, festival, social_trend, config
        )
        suggested_tags = self._suggested_tags(
            matched_factors, social_trend, config
        )
        keyword, filters = self._keyword_and_filters_for_query(query, config)
        result = {
            "season": season,
            "weather": weather,
            "temperature": shared.get("temperature"),
            "festival": festival,
            "warnings": shared.get("warnings") or [],
            "social_trend": social_trend,
            "configured_factors": self._configured_factors(config),
            "matched_factors": matched_factors,
            "suggested_tags": suggested_tags,
            "tag_bucket_target": {
                "keyword": keyword,
                "filters": filters,
                "default_after_bucket": config.get("default_after_bucket") or "top",
            },
            "tag_bucket_config": self._tag_bucket_config(
                keyword, filters, suggested_tags, config
            ),
        }
        if snapshot is not None:
            result["provenance"] = snapshot.provenance()
        return result

    def prepare_batch_snapshot(
        self,
        brand: str,
        data_date: datetime.date,
        knowledge_date: datetime.date,
        config: Dict[str, Any],
        scope_id: str,
    ) -> WorldKnowledgeBatchSnapshot:
        config_json = _canonical_json(config or {})
        effective_config = json.loads(config_json)
        shared = self._shared_facts(knowledge_date, effective_config)
        schema_version = WORLD_KNOWLEDGE_SNAPSHOT_SCHEMA_VERSION
        fingerprint = _canonical_json(
            {
                "config": _normalize_fingerprint_value(effective_config),
                "schemaVersion": schema_version,
            }
        )
        weather_available = shared["weather"].get("status") == "available"
        festival_available = shared["festival"].get("status") == "available"
        configured_weather_cities = (
            effective_config["weather_cities"]
            if "weather_cities" in effective_config
            and effective_config["weather_cities"] is not None
            else DEFAULT_WEATHER_CITIES
        )
        return WorldKnowledgeBatchSnapshot(
            snapshot_id=str(uuid.uuid4()),
            collected_at=datetime.datetime.now(datetime.timezone.utc),
            schema_version=schema_version,
            brand=brand,
            data_date=data_date,
            knowledge_date=knowledge_date,
            scope_id=scope_id,
            config_fingerprint=hashlib.sha256(fingerprint.encode()).hexdigest(),
            _effective_config_json=config_json,
            _shared_facts_json=_canonical_json(shared),
            _source_call_counts_json=_canonical_json(
                {
                    "weather": len(configured_weather_cities),
                    "festival": 1,
                }
            ),
            _source_status_json=_canonical_json(
                {
                    "weather": "available" if weather_available else "unavailable",
                    "festival": "available" if festival_available else "unavailable",
                }
            ),
        )

    def _shared_facts(
        self, value: datetime.date, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        weather_cities = (
            config["weather_cities"]
            if "weather_cities" in config and config["weather_cities"] is not None
            else DEFAULT_WEATHER_CITIES
        )
        facts = self.facts.collect(value, weather_cities)
        festival = facts.get("festival") or {}
        if festival.get("status") == "available":
            legacy_festival = {
                "status": "available",
                "items": [
                    {
                        key: item
                        for key, item in festival.items()
                        if key != "status"
                    }
                ],
            }
        else:
            legacy_festival = {
                "status": "unavailable",
                "error_code": festival.get("error_code"),
                "error_message": festival.get("error_message"),
            }
        return {
            "season": facts["season"],
            "weather": facts["weather"],
            "temperature": facts.get("temperature"),
            "festival": legacy_festival,
            "warnings": facts.get("warnings") or [],
            "active_social_trends": self._active_social_trends(config, value),
        }

    @staticmethod
    def _configured_factors(config: Dict[str, Any]) -> List[str]:
        configured = ["festival"]
        weather_cities = (
            config["weather_cities"]
            if "weather_cities" in config and config["weather_cities"] is not None
            else DEFAULT_WEATHER_CITIES
        )
        if weather_cities:
            configured.insert(0, "weather")
        if any(
            isinstance(item, dict) and item.get("enabled") is not False
            for item in config.get("social_trends") or []
        ):
            configured.append("social_trend")
        return configured

    def _active_social_trends(
        self, config: Dict[str, Any], today: datetime.date
    ) -> List[Dict[str, Any]]:
        return [
            item
            for item in config.get("social_trends") or []
            if isinstance(item, dict)
            and item.get("enabled") is not False
            and self._date_active(item, today)
        ]

    def _social_trend_for_query(
        self, active: List[Dict[str, Any]], query: str
    ) -> Dict[str, Any]:
        matched = [
            item
            for item in active
            if self._matches_any(
                query, item.get("query_patterns") or item.get("keywords") or []
            )
        ]
        return {"status": "matched" if matched else "unavailable", "items": matched}

    def _matched_factors(
        self,
        query: str,
        products: List[Dict[str, Any]],
        season: str,
        weather: Dict[str, Any],
        festival: Dict[str, Any],
        social_trend: Dict[str, Any],
        config: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        factors = []
        product_text = json.dumps(products, ensure_ascii=False)
        for rule in config.get("tag_rules") or []:
            patterns = rule.get("query_keywords") or []
            tags = rule.get("tags") or []
            if not self._matches_any(query, patterns) and not self._matches_any(
                product_text, [*patterns, *tags]
            ):
                continue
            factor = rule.get("factor")
            if factor == season:
                factors.append({"type": "season", "value": season})
            elif factor == "rain" and self._weather_has_rain(weather):
                factors.append({"type": "weather", "value": "rain"})
            elif factor == "festival" and self._festival_has_signal(festival):
                factors.append({"type": "festival", "value": festival.get("items")})
            elif factor == "social_trend" and social_trend.get("status") == "matched":
                factors.append({"type": "social_trend", "value": social_trend.get("items")})
        if social_trend.get("status") == "matched" and not any(
            item["type"] == "social_trend" for item in factors
        ):
            factors.append({"type": "social_trend", "value": social_trend.get("items")})
        return factors

    @staticmethod
    def _suggested_tags(
        matched: List[Dict[str, Any]],
        social_trend: Dict[str, Any],
        config: Dict[str, Any],
    ) -> List[str]:
        keys = {str(item.get("type") or "") for item in matched}
        keys.update(str(item.get("value") or "") for item in matched)
        tags: List[str] = []
        for rule in config.get("tag_rules") or []:
            factor = rule.get("factor")
            if factor in keys or (factor == "rain" and "weather" in keys):
                tags.extend(str(tag).strip() for tag in rule.get("tags") or [])
        for item in social_trend.get("items") or []:
            tags.extend(str(tag).strip() for tag in item.get("tags") or [])
        return list(dict.fromkeys(tag for tag in tags if tag))

    @classmethod
    def _keyword_and_filters_for_query(
        cls, query: str, config: Dict[str, Any]
    ) -> tuple[str, List[Dict[str, str]]]:
        query_text = str(query or "").strip()
        rules = {"女士": "WOMAN", "男士": "MAN", "儿童": "KID", "家居": "HOME"}
        configured = (config.get("tag_filter_rules") or {}).get("gender")
        if isinstance(configured, dict):
            rules.update({str(key): str(value) for key, value in configured.items()})
        parts = query_text.split()
        if parts and parts[-1] in rules:
            keyword = " ".join(parts[:-1]).strip() or query_text
            return keyword, [{"field": "gender", "value": rules[parts[-1]]}]
        for token, value in rules.items():
            if token in query_text:
                return query_text, [{"field": "gender", "value": value}]
        filters = config.get("default_filters")
        return query_text, [item for item in filters or [] if isinstance(item, dict)]

    @staticmethod
    def _tag_bucket_config(
        keyword: str,
        filters: List[Dict[str, str]],
        tags: List[str],
        config: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not tags:
            return None
        return {
            "keyword": keyword,
            "filters": filters,
            "configs": [
                {
                    "tags": ",".join(tags),
                    "afterBucket": config.get("default_after_bucket") or "top",
                }
            ],
        }

    @staticmethod
    def _matches_any(text: str, patterns: List[Any]) -> bool:
        lowered = str(text).lower()
        return any(
            str(pattern).strip()
            and str(pattern).strip().lower() in lowered
            for pattern in patterns
        )

    @staticmethod
    def _date_active(item: Dict[str, Any], today: datetime.date) -> bool:
        start = item.get("start_date")
        end = item.get("end_date")
        return not (
            (start and today < datetime.date.fromisoformat(str(start)))
            or (end and today > datetime.date.fromisoformat(str(end)))
        )

    @staticmethod
    def _weather_has_rain(weather: Dict[str, Any]) -> bool:
        return any(
            item.get("weather_state") == "rain"
            for item in weather.get("cities") or []
            if isinstance(item, dict)
        )

    @staticmethod
    def _festival_has_signal(festival: Dict[str, Any]) -> bool:
        for item in festival.get("items") or []:
            if not isinstance(item, dict):
                continue
            if item.get("is_holiday") is True or item.get("solar_term"):
                return True
            if str(item.get("type") or "") not in {"", "normal", "workday"}:
                return True
        return False
