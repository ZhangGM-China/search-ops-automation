"""Shared world and brand context for the deterministic Query workflow.

World facts are collected once per data date and stored under a stable global
key when collection succeeds. Failed collections remain run-local so the same
data date can be retried. Brand context is collected once per batch and brand.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Protocol

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from src.config import get_brand_business_config

from .facts import (
    DEFAULT_WEATHER_CITIES,
    normalize_temperature,
    normalize_weather,
    season_for_date,
)
from .models import QueryGovernanceContextSnapshot, QueryGovernanceTask


logger = logging.getLogger(__name__)


GLOBAL_CONTEXT_BRAND = "__global__"
GLOBAL_CONTEXT_TYPE = "global_facts"
BRAND_CONTEXT_TYPE = "brand_context"


class SharedContextStore(Protocol):
    def get_global_facts(self, data_date: dt.date) -> Mapping[str, Any] | None:
        ...

    def save_global_facts_if_absent(
        self, data_date: dt.date, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        ...

    def get_brand_context(
        self, batch_id: str, brand: str
    ) -> Mapping[str, Any] | None:
        ...

    def save_brand_context_if_absent(
        self, batch_id: str, brand: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        ...

    def read_for_query(
        self, batch_id: str, brand: str, query: str
    ) -> Mapping[str, Any] | None:
        ...


class BrandContextSource(Protocol):
    def collect(self, brand: str, data_date: dt.date) -> Mapping[str, Any]:
        ...


class InMemorySharedContextStore:
    """Small deterministic store used by unit tests and local adapters."""

    def __init__(self) -> None:
        self._global: dict[str, dict[str, Any]] = {}
        self._brands: dict[tuple[str, str], dict[str, Any]] = {}
        self._batch_dates: dict[str, dt.date] = {}

    def bind_batch_date(self, batch_id: str, data_date: dt.date) -> None:
        self._batch_dates[str(batch_id)] = data_date

    def get_global_facts(self, data_date: dt.date) -> Mapping[str, Any] | None:
        value = self._global.get(data_date.isoformat())
        if not _is_valid_global_facts(value):
            return None
        return copy.deepcopy(value)

    def save_global_facts_if_absent(
        self, data_date: dt.date, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        key = data_date.isoformat()
        existing = self._global.get(key)
        if not _is_valid_global_facts(existing):
            self._global[key] = copy.deepcopy(dict(payload))
        return copy.deepcopy(self._global[key])

    def get_brand_context(
        self, batch_id: str, brand: str
    ) -> Mapping[str, Any] | None:
        value = self._brands.get((str(batch_id), str(brand)))
        return copy.deepcopy(value) if value is not None else None

    def save_brand_context_if_absent(
        self, batch_id: str, brand: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        key = (str(batch_id), str(brand))
        self._brands.setdefault(key, copy.deepcopy(dict(payload)))
        return copy.deepcopy(self._brands[key])

    def read_for_query(
        self, batch_id: str, brand: str, _query: str
    ) -> Mapping[str, Any] | None:
        data_date = self._batch_dates.get(str(batch_id))
        brand_context = self.get_brand_context(batch_id, brand)
        if brand_context is None or data_date is None:
            return None
        global_facts = self.get_global_facts(data_date)
        return (
            _combine_context(global_facts, brand_context)
            if global_facts is not None
            else None
        )


class SqlAlchemySharedContextStore:
    """Persistence adapter for the new context snapshot table."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        snapshot_model: type[Any] = QueryGovernanceContextSnapshot,
        task_model: type[Any] = QueryGovernanceTask,
    ) -> None:
        self._session_factory = session_factory
        self._snapshot_model = snapshot_model
        self._task_model = task_model

    def get_global_facts(self, data_date: dt.date) -> Mapping[str, Any] | None:
        return self._get_snapshot(
            self._global_batch_id(data_date), GLOBAL_CONTEXT_BRAND, GLOBAL_CONTEXT_TYPE
        )

    def save_global_facts_if_absent(
        self, data_date: dt.date, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self._save_global_facts(data_date, payload)

    def get_brand_context(
        self, batch_id: str, brand: str
    ) -> Mapping[str, Any] | None:
        return self._get_snapshot(str(batch_id), brand, BRAND_CONTEXT_TYPE)

    def save_brand_context_if_absent(
        self, batch_id: str, brand: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self._save_if_absent(
            str(batch_id), brand, BRAND_CONTEXT_TYPE, payload
        )

    def read_for_query(
        self, batch_id: str, brand: str, query: str
    ) -> Mapping[str, Any] | None:
        with self._session_factory() as session:
            task = session.execute(
                select(self._task_model).where(
                    self._task_model.batch_id == str(batch_id),
                    self._task_model.brand == brand,
                    self._task_model.query_text == query,
                )
            ).scalar_one_or_none()
            if task is None:
                return None
            global_facts = self._read_snapshot(
                session,
                self._global_batch_id(task.data_date),
                GLOBAL_CONTEXT_BRAND,
                GLOBAL_CONTEXT_TYPE,
            )
            brand_context = self._read_snapshot(
                session, str(batch_id), brand, BRAND_CONTEXT_TYPE
            )
        if not _is_valid_global_facts(global_facts) or brand_context is None:
            return None
        return _combine_context(global_facts, brand_context)

    def _get_snapshot(
        self, batch_id: str, brand: str, context_type: str
    ) -> Mapping[str, Any] | None:
        with self._session_factory() as session:
            return self._read_snapshot(session, batch_id, brand, context_type)

    def _save_if_absent(
        self,
        batch_id: str,
        brand: str,
        context_type: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        encoded = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
        with self._session_factory() as session:
            existing = session.execute(
                select(self._snapshot_model).where(
                    self._snapshot_model.batch_id == batch_id,
                    self._snapshot_model.brand == brand,
                    self._snapshot_model.context_type == context_type,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    self._snapshot_model(
                        batch_id=batch_id,
                        brand=brand,
                        context_type=context_type,
                        payload=encoded,
                    )
                )
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
            return self._read_snapshot(
                session, batch_id, brand, context_type
            ) or dict(payload)

    def _save_global_facts(
        self, data_date: dt.date, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        batch_id = self._global_batch_id(data_date)
        encoded = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
        with self._session_factory() as session:
            existing = session.execute(
                select(self._snapshot_model).where(
                    self._snapshot_model.batch_id == batch_id,
                    self._snapshot_model.brand == GLOBAL_CONTEXT_BRAND,
                    self._snapshot_model.context_type == GLOBAL_CONTEXT_TYPE,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    self._snapshot_model(
                        batch_id=batch_id,
                        brand=GLOBAL_CONTEXT_BRAND,
                        context_type=GLOBAL_CONTEXT_TYPE,
                        payload=encoded,
                    )
                )
            else:
                current = self._read_snapshot(
                    session, batch_id, GLOBAL_CONTEXT_BRAND, GLOBAL_CONTEXT_TYPE
                )
                if _is_valid_global_facts(current):
                    return current
                # A legacy failed snapshot is not deleted; it is replaced by
                # the first successful snapshot for this data date.
                existing.payload = encoded
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
            return self._read_snapshot(
                session, batch_id, GLOBAL_CONTEXT_BRAND, GLOBAL_CONTEXT_TYPE
            ) or dict(payload)

    def _read_snapshot(
        self, session: Any, batch_id: str, brand: str, context_type: str
    ) -> Mapping[str, Any] | None:
        row = session.execute(
            select(self._snapshot_model).where(
                self._snapshot_model.batch_id == batch_id,
                self._snapshot_model.brand == brand,
                self._snapshot_model.context_type == context_type,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        try:
            value = json.loads(row.payload)
        except (TypeError, ValueError) as exc:
            raise ValueError("stored shared context is invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("stored shared context must be an object")
        if context_type == GLOBAL_CONTEXT_TYPE and not _is_valid_global_facts(value):
            return None
        return value

    @staticmethod
    def _global_batch_id(data_date: dt.date) -> str:
        return f"shared-facts:{data_date.isoformat()}"


class ConfigBrandContextSource:
    """Read only the brand information and active social trends from config."""

    def __init__(
        self,
        config_provider: Callable[[str], Mapping[str, Any]] = get_brand_business_config,
    ) -> None:
        self._config_provider = config_provider

    def collect(self, brand: str, data_date: dt.date) -> Mapping[str, Any]:
        business_config = self._config_provider(brand) or {}
        world_config = business_config.get("top_query_world_knowledge_config") or {}
        if not isinstance(world_config, Mapping):
            world_config = {}
        brand_info = world_config.get("brand_info")
        if not isinstance(brand_info, Mapping):
            brand_info = {"name": brand}
        social_trends = [
            dict(item)
            for item in world_config.get("social_trends") or []
            if isinstance(item, Mapping) and _active_on(item, data_date)
        ]
        return {
            "brand_info": dict(brand_info),
            "social_trends": social_trends,
            "warnings": [],
        }


class SqlXhsBrandContextSource:
    """Compose static brand configuration with read-only XHS note snapshots."""

    _BRAND_NAMES = {
        "atlas": "ATLAS", "harbor": "Harbor", "summit": "Summit",
        "riviera": "Riviera", "meadow": "Meadow", "lumen": "Lumen",
    }

    def __init__(self, engine: Any, *, base_source: BrandContextSource | None = None, table_name: str = "industry_knowledge_base.xhs_note_snapshots") -> None:
        self.engine = engine
        self.base_source = base_source or ConfigBrandContextSource()
        self.table_name = table_name

    def collect(self, brand: str, data_date: dt.date) -> Mapping[str, Any]:
        base = dict(self.base_source.collect(brand, data_date) or {})
        sql = text(f"SELECT note_title AS title, note_description AS description FROM {self.table_name} WHERE LOWER(brand_name) = LOWER(:brand) AND crawled_date = :data_date ORDER BY overall_score DESC, engagement_count DESC")
        params = {"brand": self._BRAND_NAMES.get(str(brand).strip().lower(), str(brand).strip()), "data_date": data_date.isoformat()}
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(sql, params).mappings().all()
        except Exception as exc:
            # Local/test databases may expose the same table without a schema.
            if "." not in self.table_name:
                rows = []
            else:
                try:
                    fallback = text("SELECT note_title AS title, note_description AS description FROM xhs_note_snapshots WHERE LOWER(brand_name) = LOWER(:brand) AND crawled_date = :data_date ORDER BY overall_score DESC, engagement_count DESC")
                    with self.engine.connect() as connection:
                        rows = connection.execute(fallback, params).mappings().all()
                except Exception:
                    logger.warning("xhs social trends unavailable brand=%s data_date=%s error_type=%s", brand, data_date.isoformat(), type(exc).__name__)
                    rows = []
        base["social_trends"] = [
            {"title": str(row.get("title") or "").strip(), "description": str(row.get("description") or "").strip()}
            for row in rows
            if str(row.get("title") or "").strip() or str(row.get("description") or "").strip()
        ]
        return base


class SharedContextService:
    """Collect and persist the context shared by reorder for one batch."""

    def __init__(
        self,
        *,
        facts: Any,
        brands: BrandContextSource | None = None,
        store: SharedContextStore,
        weather_cities: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.facts = facts
        self.brands = brands or ConfigBrandContextSource()
        self.store = store
        self.weather_cities = list(weather_cities) if weather_cities is not None else None

    def prepare(
        self,
        *,
        batch_id: str,
        data_date: dt.date,
        brands: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        if not str(batch_id).strip():
            raise ValueError("batch_id is required for shared context")
        if not isinstance(data_date, dt.date):
            raise TypeError("data_date must be a date")
        names = sorted({str(brand) for brand in brands if str(brand).strip()})
        if not names:
            return {}

        global_facts = self.store.get_global_facts(data_date)
        if global_facts is None:
            weather_cities = (
                self.weather_cities
                if self.weather_cities is not None
                else DEFAULT_WEATHER_CITIES
            )
            try:
                collected = self.facts.collect(data_date, weather_cities)
            except Exception as exc:
                logger.warning(
                    "shared world facts collection failed data_date=%s error_type=%s",
                    data_date.isoformat(),
                    type(exc).__name__,
                )
                collected = _unavailable_facts(data_date)
            if not isinstance(collected, Mapping):
                logger.warning(
                    "shared world facts collection returned invalid payload data_date=%s",
                    data_date.isoformat(),
                )
                collected = _unavailable_facts(data_date, invalid=True)
            normalized = _normalize_global_facts(collected, data_date)
            if normalized["external_facts_available"]:
                global_facts = self.store.save_global_facts_if_absent(
                    data_date, normalized
                )
            else:
                logger.warning(
                    "shared world facts unavailable data_date=%s; retry on next run",
                    data_date.isoformat(),
                )
                # Keep the empty facts only for this run.  A failed collection
                # must not become the valid snapshot for this data date, so a
                # later run gets another chance to read the shared service.
                global_facts = normalized

        bind_date = getattr(self.store, "bind_batch_date", None)
        if callable(bind_date):
            bind_date(batch_id, data_date)

        result: dict[str, dict[str, Any]] = {}
        for brand in names:
            brand_context = self.store.get_brand_context(batch_id, brand)
            if brand_context is None:
                try:
                    collected_brand = self.brands.collect(brand, data_date)
                except Exception:
                    collected_brand = {
                        "brand_info": {"name": brand},
                        "social_trends": [],
                        "warnings": [
                            {
                                "source": "brand_context",
                                "error_code": "FACT_BRAND_CONTEXT_UNAVAILABLE",
                                "error_message": "品牌上下文读取失败",
                                "brand": brand,
                            }
                        ],
                    }
                brand_context = self.store.save_brand_context_if_absent(
                    batch_id, brand, _normalize_brand_context(collected_brand, brand)
                )
            result[brand] = _combine_context(global_facts, brand_context)
        return result


def _normalize_brand_context(value: Any, brand: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {
            "brand_info": {"name": brand},
            "social_trends": [],
            "warnings": [
                {
                    "source": "brand_context",
                    "error_code": "FACT_BRAND_CONTEXT_INVALID",
                    "error_message": "品牌上下文格式无效",
                    "brand": brand,
                }
            ],
        }
    brand_info = value.get("brand_info")
    if not isinstance(brand_info, Mapping):
        brand_info = {"name": brand}
    trends = value.get("social_trends")
    if not isinstance(trends, Sequence) or isinstance(trends, (str, bytes)):
        trends = []
    warnings = value.get("warnings")
    if not isinstance(warnings, Sequence) or isinstance(warnings, (str, bytes)):
        warnings = []
    return {
        "brand_info": dict(brand_info),
        "social_trends": [dict(item) for item in trends if isinstance(item, Mapping)],
        "warnings": [dict(item) for item in warnings if isinstance(item, Mapping)],
    }


def _combine_context(
    global_facts: Mapping[str, Any], brand_context: Mapping[str, Any]
) -> dict[str, Any]:
    warnings = [
        dict(item)
        for item in list(global_facts.get("warnings") or [])
        + list(brand_context.get("warnings") or [])
        if isinstance(item, Mapping)
    ]
    return {
        "global_facts": copy.deepcopy(dict(global_facts)),
        "brand_context": copy.deepcopy(dict(brand_context)),
        "warnings": warnings,
        "reorder_available": global_facts.get("external_facts_available") is not False,
    }


def _is_valid_global_facts(value: Any) -> bool:
    """Return whether a persisted global snapshot is safe to reuse."""

    return isinstance(value, Mapping) and value.get("external_facts_available") is True


def _unavailable_facts(data_date: dt.date, *, invalid: bool = False) -> dict[str, Any]:
    code = "FACTS_INVALID" if invalid else "FACTS_UNAVAILABLE"
    return {
        "date": data_date.isoformat(),
        "season": None,
        "weather": {"status": "error", "cities": []},
        "temperature": None,
        "festival": {"status": "error"},
        "warnings": [
            {
                "source": "facts",
                "error_code": code,
                "error_message": "外部事实读取失败",
            }
        ],
        "external_facts_available": False,
    }


def _normalize_global_facts(value: Mapping[str, Any], data_date: dt.date) -> dict[str, Any]:
    weather_value = value.get("weather")
    if not isinstance(weather_value, Mapping):
        weather_value = {}
    states: list[str] = []
    weather_cities: list[dict[str, Any]] = []
    for item in weather_value.get("cities") or []:
        if not isinstance(item, Mapping):
            continue
        state = str(item.get("weather_state") or "").strip().lower()
        if not state:
            state = normalize_weather(item.get("weather"))
        if state and state not in states:
            states.append(state)
        weather_cities.append({
            "city": item.get("city"),
            "weather": item.get("weather") or state,
            "weather_state": state,
            "temperature": item.get("temperature") or item.get("temperature_level"),
        })
    weather_status = str(weather_value.get("status") or "").strip().lower()
    if weather_status not in {"available", "error"}:
        weather_status = "available" if states else "error"

    festival_value = value.get("festival")
    if not isinstance(festival_value, Mapping):
        festival_value = {"status": "error"}
    festival = {
        key: festival_value[key]
        for key in (
            "status",
            "date",
            "name",
            "type",
            "is_holiday",
            "is_work_day",
            "note",
            "solar_term",
        )
        if key in festival_value
    }
    if festival.get("status") not in {"available", "error"}:
        festival["status"] = "available" if festival else "error"

    temperature = value.get("temperature")
    temperature = (
        str(temperature).strip().lower()
        if isinstance(temperature, str)
        and str(temperature).strip().lower() in {"high", "normal", "cold"}
        else normalize_temperature(temperature)
    )
    raw_warnings = value.get("warnings")
    warnings = []
    if isinstance(raw_warnings, Sequence) and not isinstance(raw_warnings, (str, bytes)):
        for item in raw_warnings:
            if not isinstance(item, Mapping):
                continue
            warning = {
                key: item[key]
                for key in ("source", "error_code", "error_message", "city")
                if key in item
            }
            if warning:
                warnings.append(warning)
    return {
        "date": str(value.get("date") or data_date.isoformat()),
        "season": str(value.get("season") or season_for_date(data_date)),
        "weather": {"status": weather_status, "states": states},
        "weather_cities": weather_cities,
        "temperature": temperature,
        "festival": festival,
        "warnings": warnings,
        "external_facts_available": (
            weather_status == "available"
            or festival.get("status") == "available"
        ),
    }


def _active_on(item: Mapping[str, Any], data_date: dt.date) -> bool:
    if item.get("enabled") is False:
        return False
    start = _parse_date(item.get("start_date"))
    end = _parse_date(item.get("end_date"))
    return (start is None or data_date >= start) and (end is None or data_date <= end)


def _parse_date(value: Any) -> dt.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


__all__ = [
    "BRAND_CONTEXT_TYPE",
    "BrandContextSource",
    "ConfigBrandContextSource",
    "GLOBAL_CONTEXT_BRAND",
    "GLOBAL_CONTEXT_TYPE",
    "InMemorySharedContextStore",
    "SharedContextService",
    "SharedContextStore",
    "SqlAlchemySharedContextStore",
    "SqlXhsBrandContextSource",
]
