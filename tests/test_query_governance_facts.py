import datetime as dt
import io
import json
from urllib.error import URLError

import pytest

from src.query_governance.facts import (
    HolidayFactProvider,
    WeatherFactProvider,
    WorldFactsService,
)
from src.query_governance.workflow import QueryGovernanceWorkflow


class FakeResponse:
    def __init__(self, payload: bytes, *, status: int = 200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, _limit=-1):
        return self.payload


def test_weather_provider_reads_and_normalizes_china_weather_response():
    payload = (
        b'var dataSK = {"nameen":"shanghai","cityname":"\\u4e0a\\u6d77",'
        b'"city":"101020100","temp":"29","wd":"\\u4e1c\\u5317\\u98ce",'
        b'"ws":"1\\u7ea7","sd":"69%","aqi":"28","weather":"\\u5c0f\\u96e8"}'
    )
    calls = []

    def opener(request, timeout=None):
        calls.append((request.full_url, timeout, dict(request.headers)))
        return FakeResponse(payload)

    result = WeatherFactProvider(urlopen=opener, retries=0).fetch(
        {"city": "上海", "city_code": 101020100}, dt.date(2026, 8, 28)
    )

    assert result["city"] == "上海"
    assert result["temperature"] == 29
    assert result["temperature_level"] == "high"
    assert result["weather"] == "小雨"
    assert result["weather_state"] == "rain"
    assert calls[0][0].endswith("/101020100.html")


def test_weather_provider_retries_then_returns_structured_error():
    attempts = []

    def opener(_request, data=None, timeout=None):
        assert data is None
        attempts.append(1)
        raise URLError("weather unavailable")

    result = WeatherFactProvider(urlopen=opener, retries=2).fetch(
        {"city": "上海", "city_code": 101020100}, dt.date(2026, 8, 28)
    )

    assert len(attempts) == 3
    assert result["status"] == "error"
    assert result["error_code"] == "FACT_WEATHER_UNAVAILABLE"


def test_fact_providers_pass_timeout_as_keyword_to_stdlib_urlopen():
    weather_payload = (
        b'var dataSK = {"cityname":"\\u4e0a\\u6d77","city":"101020100",'
        b'"temp":"29","weather":"\\u6674"}'
    )
    holiday_payload = json.dumps({"days": []}).encode()

    def opener(request, data=None, timeout=None):
        # urllib.request.urlopen(request, data=None, timeout=...) treats the
        # second positional argument as request data.  A numeric timeout in
        # that position raises TypeError in the real stdlib implementation.
        if data is not None:
            raise TypeError("message_body should be bytes-like")
        assert timeout == 10
        payload = weather_payload if "weather.com.cn" in request.full_url else holiday_payload
        return FakeResponse(payload)

    weather = WeatherFactProvider(urlopen=opener, retries=0).fetch(
        {"city": "上海", "city_code": 101020100}, dt.date(2026, 9, 2)
    )
    holiday = HolidayFactProvider(
        urlopen=opener,
        primary_url_template="https://cdn.example/{year}.json",
        backup_url_template="https://backup.example/{year}.json",
        retries=0,
    ).fetch(dt.date(2026, 9, 2))

    assert weather["status"] == "available"
    assert holiday["status"] == "available"


def test_holiday_provider_caches_year_and_normalizes_weekend():
    payload = json.dumps(
        {
            "days": [
                {
                    "date": "2026-10-01",
                    "name": "国庆节",
                    "isOffDay": True,
                    "note": "国庆",
                }
            ]
        },
        ensure_ascii=False,
    ).encode()
    calls = []

    def opener(request, timeout=None):
        calls.append((request.full_url, timeout))
        return FakeResponse(payload)

    provider = HolidayFactProvider(
        urlopen=opener,
        primary_url_template="https://primary.example/{year}.json",
        backup_url_template="https://backup.example/{year}.json",
        retries=0,
    )
    holiday = provider.fetch(dt.date(2026, 10, 1))
    weekend = provider.fetch(dt.date(2026, 10, 3))

    assert holiday["name"] == "国庆节"
    assert holiday["is_holiday"] is True
    assert holiday["type"] == "holiday"
    assert weekend["type"] == "normal"
    assert weekend["is_holiday"] is True
    assert len(calls) == 1


def test_world_facts_service_collects_season_weather_temperature_and_festival():
    class StaticWeather:
        def fetch(self, city, _date):
            return {
                "status": "available",
                "city": city["city"],
                "temperature": 29,
                "temperature_level": "high",
                "weather": "晴",
                "weather_state": "clear",
            }

    class StaticHoliday:
        def fetch(self, _date):
            return {"status": "available", "type": "normal", "is_holiday": False}

    facts = WorldFactsService(
        weather_provider=StaticWeather(), holiday_provider=StaticHoliday()
    ).collect(
        dt.date(2026, 8, 28), [{"city": "上海", "city_code": 101020100}]
    )

    assert facts["season"] == "summer"
    assert facts["temperature"] == "high"
    assert facts["weather"]["cities"][0]["weather_state"] == "clear"
    assert facts["festival"]["type"] == "normal"


def test_world_facts_service_caches_success_for_same_date_and_cities():
    calls = {"weather": 0, "holiday": 0}

    class Weather:
        def fetch(self, city, _date):
            calls["weather"] += 1
            return {
                "status": "available",
                "city": city["city"],
                "temperature_level": "normal",
                "weather_state": "clear",
            }

    class Holiday:
        def fetch(self, _date):
            calls["holiday"] += 1
            return {"status": "available", "type": "normal"}

    service = WorldFactsService(
        weather_provider=Weather(), holiday_provider=Holiday()
    )
    cities = [{"city": "上海", "city_code": 101020100}]

    first = service.collect(dt.date(2026, 8, 28), cities)
    first["weather"]["cities"].clear()
    second = service.collect(dt.date(2026, 8, 28), cities)

    assert calls == {"weather": 1, "holiday": 1}
    assert len(second["weather"]["cities"]) == 1


def test_world_facts_service_turns_provider_exceptions_into_structured_warnings():
    class BrokenWeather:
        def fetch(self, _city, _date):
            raise RuntimeError("weather secret detail")

    class BrokenHoliday:
        def fetch(self, _date):
            raise RuntimeError("holiday secret detail")

    facts = WorldFactsService(
        weather_provider=BrokenWeather(), holiday_provider=BrokenHoliday()
    ).collect(
        dt.date(2026, 8, 28), [{"city": "上海", "city_code": 101020100}]
    )

    assert facts["external_facts_available"] is False
    assert facts["weather"] == {"status": "error", "cities": []}
    assert facts["festival"]["error_code"] == "FACT_HOLIDAY_UNAVAILABLE"
    assert [warning["error_code"] for warning in facts["warnings"]] == [
        "FACT_WEATHER_UNAVAILABLE",
        "FACT_HOLIDAY_UNAVAILABLE",
    ]
    assert "secret detail" not in json.dumps(facts, ensure_ascii=False)


def test_query_workflow_can_call_world_facts_service_without_legacy_mcp_client():
    class Facts:
        def collect(self, date, weather_cities):
            return {"date": date.isoformat(), "cities": list(weather_cities)}

    class Search:
        def mixed(self, _brand, _query):
            return []

    class Store:
        def get_s0(self, _brand, _query):
            return None

        def save_s0_if_absent(self, _brand, _query, snapshot):
            return snapshot

        def save_step(self, *_args):
            return None

    class Runner:
        def run(self, _spec, _context):
            from src.query_governance.models import NodeResult

            return NodeResult.executed()

    class Actions:
        def execute_and_verify(self, _spec, _action, _context):
            from src.query_governance.models import NodeResult

            return NodeResult.executed()

    workflow = QueryGovernanceWorkflow(
        search=Search(),
        store=Store(),
        runners={"recall": Runner(), "reorder": Runner(), "relevance": Runner()},
        action_gateway=Actions(),
        facts=Facts(),
    )

    result = workflow.collect_facts(
        dt.date(2026, 8, 28), [{"city": "上海", "city_code": 101020100}]
    )
    assert result["date"] == "2026-08-28"


def test_query_governance_code_has_no_mcp_world_knowledge_client_or_config():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sources = [
        root / "src" / "query_governance",
        root / "src" / "config.py",
        root / "config.yaml",
        root / "requirements.txt",
        root / "requirements.runtime.in",
        root / "Dockerfile",
    ]
    text = "\n".join(
        path.read_text(encoding="utf-8")
        if path.is_file()
        else "\n".join(p.read_text(encoding="utf-8") for p in path.rglob("*.py"))
        for path in sources
    )
    assert "weather_mcp" not in text
    assert "festival_mcp" not in text
    assert "china-festival-mcp" not in text
    assert "McpStdioClient" not in text
