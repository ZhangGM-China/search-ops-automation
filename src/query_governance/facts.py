"""Engineering-owned world fact providers for Query governance."""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any
from urllib.request import Request, urlopen


DEFAULT_WEATHER_CITIES: tuple[dict[str, Any], ...] = (
    {"city": "上海", "city_code": 101020100},
    {"city": "杭州", "city_code": 101210101},
    {"city": "南京", "city_code": 101190101},
    {"city": "苏州", "city_code": 101190401},
)
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_SOLAR_TERMS = (
    "小寒", "大寒", "立春", "雨水", "惊蛰", "春分",
    "清明", "谷雨", "立夏", "小满", "芒种", "夏至",
    "小暑", "大暑", "立秋", "处暑", "白露", "秋分",
    "寒露", "霜降", "立冬", "小雪", "大雪", "冬至",
)
_SOLAR_TERM_MINUTES = (
    0, 21208, 42467, 63836, 85337, 107014,
    128867, 150921, 173149, 195551, 218072, 240693,
    263343, 285989, 308563, 331033, 353350, 375494,
    397447, 419210, 440795, 462224, 483532, 504758,
)


def season_for_date(value: dt.date) -> str:
    if value.month in {3, 4, 5}:
        return "spring"
    if value.month in {6, 7, 8}:
        return "summer"
    if value.month in {9, 10, 11}:
        return "autumn"
    return "winter"


def normalize_weather(value: Any) -> str:
    text = str(value or "").strip().lower()
    rules = (
        (("雷", "storm", "thunder"), "storm"),
        (("雪", "snow"), "snow"),
        (("雨", "rain", "shower"), "rain"),
        (("霾", "haze"), "haze"),
        (("雾", "fog"), "fog"),
        (("阴", "云", "cloud", "overcast"), "cloudy"),
        (("晴", "sun", "clear"), "clear"),
    )
    for tokens, normalized in rules:
        if any(token in text for token in tokens):
            return normalized
    return "other"


def normalize_temperature(value: Any) -> str | None:
    try:
        temperature = float(value)
    except (TypeError, ValueError):
        return None
    if temperature >= 28:
        return "high"
    if temperature <= 10:
        return "cold"
    return "normal"


def solar_term_for_date(value: dt.date) -> str | None:
    if value.year < 1901 or value.year > 2099:
        return None
    base = dt.datetime(1900, 1, 6, 2, 5, tzinfo=dt.timezone.utc)
    for index, name in enumerate(_SOLAR_TERMS):
        instant = base + dt.timedelta(
            milliseconds=(
                31556925974.7 * (value.year - 1900)
                + _SOLAR_TERM_MINUTES[index] * 60_000
            )
        )
        if instant.astimezone(dt.timezone(dt.timedelta(hours=8))).date() == value:
            return name
    return None


class WeatherFactProvider:
    """Query China Weather Network directly by city code."""

    def __init__(
        self,
        *,
        urlopen: Callable[..., Any] = urlopen,
        timeout_seconds: float = 10,
        retries: int = 2,
        retry_delay_seconds: float = 0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._urlopen = urlopen
        self.timeout_seconds = timeout_seconds
        self.retries = max(0, retries)
        self.retry_delay_seconds = max(0, retry_delay_seconds)
        self._sleeper = sleeper

    def fetch(self, city: Mapping[str, Any], _date: dt.date) -> dict[str, Any]:
        city_name = str(city.get("city") or "").strip()
        city_code = str(city.get("city_code") or "").strip()
        if not city_name or not city_code:
            return self._error(city_name, "FACT_WEATHER_CITY_INVALID")
        request = Request(
            f"http://d1.weather.com.cn/sk_2d/{city_code}.html",
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; AIOPS/1.0)",
                "Host": "d1.weather.com.cn",
                "Referer": "http://www.weather.com.cn/",
            },
        )
        for attempt in range(self.retries + 1):
            try:
                with self._urlopen(request, timeout=self.timeout_seconds) as response:
                    raw = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise ValueError("weather response is too large")
                payload = self._decode_payload(raw)
                temperature = self._number_or_text(payload.get("temp"))
                weather = self._decode_text(payload.get("weather"))
                return {
                    "status": "available",
                    "city": self._decode_text(payload.get("cityname")) or city_name,
                    "city_code": str(payload.get("city") or city_code),
                    "weather": weather,
                    "weather_state": normalize_weather(weather),
                    "temperature": temperature,
                    "temperature_level": normalize_temperature(temperature),
                }
            except Exception:
                if attempt < self.retries and self.retry_delay_seconds:
                    self._sleeper(self.retry_delay_seconds)
        return self._error(city_name, "FACT_WEATHER_UNAVAILABLE")

    @staticmethod
    def _decode_payload(raw: bytes) -> dict[str, Any]:
        content = raw.decode("latin1")
        normalized = content.encode("latin1").decode("unicode_escape", errors="ignore")
        match = re.search(r"({.*})", normalized, re.DOTALL)
        if not match:
            raise ValueError("weather response does not contain JSON")
        value = json.loads(match.group(1))
        if not isinstance(value, dict):
            raise ValueError("weather response is invalid")
        return value

    @staticmethod
    def _decode_text(value: Any) -> str:
        text = str(value or "")
        try:
            return text.encode("latin1").decode("utf-8")
        except UnicodeError:
            return text

    @staticmethod
    def _number_or_text(value: Any) -> Any:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return text
        return int(number) if number.is_integer() else number

    @staticmethod
    def _error(city: str, code: str) -> dict[str, Any]:
        return {
            "status": "error",
            "city": city,
            "error_code": code,
            "error_message": "天气事实读取失败",
        }


class HolidayFactProvider:
    """Query public annual Chinese holiday data through direct HTTP."""

    def __init__(
        self,
        *,
        urlopen: Callable[..., Any] = urlopen,
        primary_url_template: str = (
            "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json"
        ),
        backup_url_template: str = (
            "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json"
        ),
        timeout_seconds: float = 10,
        retries: int = 2,
        retry_delay_seconds: float = 0,
        cache_ttl_seconds: float = 7 * 24 * 60 * 60,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._urlopen = urlopen
        self.url_templates = (primary_url_template, backup_url_template)
        self.timeout_seconds = timeout_seconds
        self.retries = max(0, retries)
        self.retry_delay_seconds = max(0, retry_delay_seconds)
        self.cache_ttl_seconds = max(0, cache_ttl_seconds)
        self._sleeper = sleeper
        self._clock = clock
        self._cache: dict[int, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def fetch(self, value: dt.date) -> dict[str, Any]:
        try:
            year_data = self._year_data(value.year)
        except Exception:
            return {
                "status": "error",
                "date": value.isoformat(),
                "error_code": "FACT_HOLIDAY_UNAVAILABLE",
                "error_message": "节日事实读取失败",
            }
        special = next(
            (
                item
                for item in year_data.get("days") or []
                if isinstance(item, dict) and item.get("date") == value.isoformat()
            ),
            None,
        )
        solar_term = solar_term_for_date(value)
        if special is not None:
            is_holiday = special.get("isOffDay") is True
            result = {
                "status": "available",
                "date": value.isoformat(),
                "name": str(special.get("name") or "").strip(),
                "type": "holiday" if is_holiday else "workday",
                "is_holiday": is_holiday,
                "is_work_day": not is_holiday,
                "note": str(special.get("note") or "").strip(),
            }
        else:
            is_weekend = value.weekday() >= 5
            result = {
                "status": "available",
                "date": value.isoformat(),
                "name": "",
                "type": "normal",
                "is_holiday": is_weekend,
                "is_work_day": not is_weekend,
                "note": "周末" if is_weekend else "工作日",
            }
        if solar_term:
            result["solar_term"] = solar_term
        return result

    def _year_data(self, year: int) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            cached = self._cache.get(year)
            if cached and now - cached[0] < self.cache_ttl_seconds:
                return deepcopy(cached[1])
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            for template in self.url_templates:
                try:
                    request = Request(
                        template.format(year=year),
                        headers={"User-Agent": "AIOPS/1.0"},
                    )
                    with self._urlopen(request, timeout=self.timeout_seconds) as response:
                        raw = response.read(_MAX_RESPONSE_BYTES + 1)
                    if len(raw) > _MAX_RESPONSE_BYTES:
                        raise ValueError("holiday response is too large")
                    value = json.loads(raw.decode("utf-8"))
                    if not isinstance(value, dict) or not isinstance(value.get("days"), list):
                        raise ValueError("holiday response is invalid")
                    with self._lock:
                        self._cache[year] = (now, deepcopy(value))
                    return value
                except Exception as exc:
                    last_error = exc
            if attempt < self.retries and self.retry_delay_seconds:
                self._sleeper(self.retry_delay_seconds)
        raise RuntimeError("holiday source unavailable") from last_error


class WorldFactsService:
    """Collect normalized facts behind a stable Query-governance interface."""

    def __init__(
        self,
        *,
        weather_provider: Any | None = None,
        holiday_provider: Any | None = None,
        cache_ttl_seconds: float = 60 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.weather_provider = weather_provider or WeatherFactProvider()
        self.holiday_provider = holiday_provider or HolidayFactProvider()
        self.cache_ttl_seconds = max(0, cache_ttl_seconds)
        self._clock = clock
        self._cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def collect(
        self,
        value: dt.date,
        weather_cities: Sequence[Mapping[str, Any]] = DEFAULT_WEATHER_CITIES,
    ) -> dict[str, Any]:
        cities = tuple(
            (str(city.get("city") or ""), str(city.get("city_code") or ""))
            for city in weather_cities
        )
        key = (value.isoformat(), cities)
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < self.cache_ttl_seconds:
                return deepcopy(cached[1])

        weather_items = []
        for city in weather_cities:
            try:
                item = self.weather_provider.fetch(city, value)
            except Exception:
                item = WeatherFactProvider._error(
                    str(city.get("city") or "") if isinstance(city, Mapping) else "",
                    "FACT_WEATHER_UNAVAILABLE",
                )
            if not isinstance(item, Mapping):
                item = WeatherFactProvider._error(
                    str(city.get("city") or "") if isinstance(city, Mapping) else "",
                    "FACT_WEATHER_INVALID",
                )
            weather_items.append(dict(item))
        available_weather = [
            item for item in weather_items if item.get("status") == "available"
        ]
        warnings = [
            {
                "source": "weather",
                "error_code": item.get("error_code") or "FACT_WEATHER_UNAVAILABLE",
                "error_message": item.get("error_message") or "天气事实读取失败",
                "city": item.get("city"),
            }
            for item in weather_items
            if item.get("status") != "available"
        ]
        try:
            holiday = self.holiday_provider.fetch(value)
        except Exception:
            holiday = {
                "status": "error",
                "date": value.isoformat(),
                "error_code": "FACT_HOLIDAY_UNAVAILABLE",
                "error_message": "节日事实读取失败",
            }
        if not isinstance(holiday, Mapping):
            holiday = {
                "status": "error",
                "date": value.isoformat(),
                "error_code": "FACT_HOLIDAY_INVALID",
                "error_message": "节日事实读取失败",
            }
        holiday = dict(holiday)
        if holiday.get("status") != "available":
            warnings.append(
                {
                    "source": "festival",
                    "error_code": holiday.get("error_code") or "FACT_HOLIDAY_UNAVAILABLE",
                    "error_message": holiday.get("error_message") or "节日事实读取失败",
                }
            )
        levels = [
            item["temperature_level"]
            for item in available_weather
            if item.get("temperature_level")
        ]
        result = {
            "date": value.isoformat(),
            "season": season_for_date(value),
            "weather": {
                "status": "available" if available_weather else "error",
                "cities": available_weather,
            },
            "temperature": Counter(levels).most_common(1)[0][0] if levels else None,
            "festival": holiday,
            "warnings": warnings,
            "external_facts_available": bool(
                available_weather or holiday.get("status") == "available"
            ),
        }
        with self._lock:
            self._cache[key] = (now, deepcopy(result))
        return result
