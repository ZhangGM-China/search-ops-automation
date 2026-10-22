import json
from types import SimpleNamespace

import pytest

from tests.manual_scripts import benchmark_preference_generation as benchmark


def _query(name: str, count: int = 2) -> dict:
    return {
        "query": name,
        "brand": "atlas",
        "ranking_eligible": True,
        "shared_context": {"season": "夏季"},
        "brand_context": {"summary": "轻盈剪裁"},
        "products": [
            {
                "product_id": f"{name}-{index}",
                "click_rank": index,
                "purchase_rank": None,
                "current_rank": index + 5,
                "product_name": f"{name} 商品 {index}",
                "category": "连衣裙",
                "color": "黑色",
                "material": "棉",
            }
            for index in range(1, count + 1)
        ],
    }


def test_pack_queries_never_splits_a_query_and_respects_limit():
    queries = [_query("q1", 3), _query("q2", 2), _query("q3", 4)]

    batches = benchmark.pack_queries(queries, batch_query_limit=2, token_limit=10_000)

    assert [[item["query"] for item in batch] for batch in batches] == [["q1", "q2"], ["q3"]]
    assert sum(len(item["products"]) for batch in batches for item in batch) == 9


def test_pack_queries_uses_token_limit_without_splitting_query():
    queries = [_query("q1", 2), _query("q2", 2)]
    estimated = [benchmark.estimate_query_tokens(item) for item in queries]
    batches = benchmark.pack_queries(
        queries,
        batch_query_limit=10,
        token_limit=estimated[0] + estimated[1] - 1,
    )

    assert [[item["query"] for item in batch] for batch in batches] == [["q1"], ["q2"]]


def test_pack_queries_rejects_non_positive_limits():
    with pytest.raises(ValueError, match="positive"):
        benchmark.pack_queries([], batch_query_limit=0, token_limit=100)


def test_fixture_validation_rejects_empty_query_products_and_duplicates(tmp_path):
    for payload, message in (
        ({"queries": [{"query": "", "products": [{}]}]}, "fixture_query_is_empty"),
        ({"queries": [{"query": "q", "products": []}]}, "fixture_products_missing"),
        ({"queries": [{"query": "q", "products": [{}]}]}, "fixture_product_id_missing"),
        ({"queries": [_query("q"), _query("q")]}, "duplicate_fixture_query"),
    ):
        fixture = tmp_path / f"{len(message)}.json"
        fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            benchmark.load_fixture(fixture, brand="atlas")


def test_validate_output_rejects_missing_duplicate_and_unknown_queries():
    expected = [_query("q1"), _query("q2")]
    payload = {
        "results": [
            {"query": "q1", "should_reorder": True, "preference_description": "偏好轻盈材质", "reason": None},
            {"query": "q1", "should_reorder": True, "preference_description": "重复", "reason": None},
        ]
    }

    result = benchmark.validate_output(payload, expected)

    assert result.valid is False
    assert "duplicate_query" in result.errors
    assert "missing_query" in result.errors


def test_validate_output_accepts_compact_valid_payload():
    expected = [_query("q1"), _query("q2")]
    payload = {
        "results": [
            {"query": "q1", "should_reorder": True, "preference_description": "偏好轻盈材质", "reason": None},
            {"query": "q2", "should_reorder": False, "preference_description": None, "reason": "证据不足"},
        ]
    }

    result = benchmark.validate_output(payload, expected)

    assert result.valid is True
    assert result.errors == []


def test_validate_output_accepts_brand_level_strategies_and_rejects_missing_query():
    expected = [_query("q1"), _query("q2")]
    valid = benchmark.validate_output(
        {"brand": "atlas", "strategies": {"q1": "偏好轻盈材质", "q2": "偏好宽松版型"}},
        expected,
    )
    assert valid.valid is True
    assert valid.results == [
        {"query": "q1", "strategy": "偏好轻盈材质"},
        {"query": "q2", "strategy": "偏好宽松版型"},
    ]
    invalid = benchmark.validate_output({"brand": "atlas", "strategies": {"q1": "偏好"}}, expected)
    assert invalid.valid is False
    assert "missing_query" in invalid.errors


def test_validate_output_catches_field_shape_errors_and_json_parser_variants():
    expected = [_query("q1")]
    invalid = benchmark.validate_output(
        {"results": [{"query": "unknown", "should_reorder": "yes", "preference_description": None, "reason": 3}]},
        expected,
    )
    assert invalid.valid is False
    assert "unknown_query" in invalid.errors
    assert "should_reorder_not_bool:unknown" in invalid.errors
    assert benchmark._parse_json("```json\n{\"results\": []}\n```") == {"results": []}
    assert benchmark._parse_json("not json") is None
    assert benchmark.validate_output(None, expected).errors == ["results_not_list"]


def test_usage_reads_common_langchain_metadata():
    response = SimpleNamespace(usage_metadata={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150})

    assert benchmark.extract_usage(response) == {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "reported": True,
    }


def test_usage_falls_back_to_response_metadata_and_derives_total():
    response = SimpleNamespace(
        usage_metadata=None,
        response_metadata={"token_usage": {"prompt_tokens": 12, "completion_tokens": 3}},
    )

    assert benchmark.extract_usage(response) == {
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 15,
        "reported": True,
    }


def test_build_messages_has_one_top_level_schema_and_separate_query_groups():
    messages = benchmark.build_messages([_query("q1"), _query("q2")])

    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    payload = json.loads(messages[1]["content"])
    assert set(payload) == {"brand", "scene", "brand_context", "queries"}
    assert list(payload["queries"]) == ["q1", "q2"]
    assert isinstance(payload["queries"]["q1"], list)
    assert payload["brand_context"]["social_trends"] == []


def test_build_messages_normalizes_scene_enums_to_chinese():
    item = _query("q1")
    item["shared_context"] = {
        "date": "2026-09-07",
        "season": "autumn",
        "weather": {"cities": [{"city": "上海", "weather_state": "clear", "temperature_level": "normal"}]},
        "festival": {"date": "2026-09-07", "type": "normal", "is_holiday": False},
    }
    payload = json.loads(benchmark.build_messages([item])[1]["content"])
    assert payload["scene"] == {
        "date": "2026-09-07",
        "season": "秋季",
        "weather": [{"city": "上海", "weather": "晴", "temperature": "常温"}],
        "holiday": {"date": "2026-09-07", "name": "", "type": "工作日"},
    }


def test_build_messages_rejects_empty_query_batch():
    with pytest.raises(ValueError, match="cannot_build_empty_batch"):
        benchmark.build_messages([])


def test_build_messages_passes_social_posts_once_at_brand_level_and_null_when_missing():
    first = _query("q1")
    first["brand_context"]["social_posts"] = [
        {"note_id": "n1", "note_title": "早秋穿搭", "note_description": "轻盈叠穿", "comments": None}
    ]
    second = _query("q2")
    second["brand_context"]["social_posts"] = first["brand_context"]["social_posts"]

    messages = benchmark.build_messages([first, second])
    payload = json.loads(messages[1]["content"])

    assert payload["brand_context"]["social_trends"] == [{"title": "早秋穿搭", "description": "轻盈叠穿"}]
    assert all("brand_context" not in group for group in payload["queries"].values())

    missing = _query("q3")
    missing_messages = benchmark.build_messages([missing])
    assert json.loads(missing_messages[1]["content"])["brand_context"]["social_trends"] == []


def test_social_sql_reads_all_raw_xhs_posts_without_comments_or_limit():
    assert "xhs_note_snapshots" in benchmark.SOCIAL_NOTES_SQL
    assert "xhs_post_comments" not in benchmark.SOCIAL_NOTES_SQL
    assert "LIMIT" not in benchmark.SOCIAL_NOTES_SQL.upper()
    assert "note_title" in benchmark.SOCIAL_NOTES_SQL
    assert "note_description" in benchmark.SOCIAL_NOTES_SQL
    assert "note_id" not in benchmark.SOCIAL_NOTES_SQL
    assert "note_url" not in benchmark.SOCIAL_NOTES_SQL

    class FakeConnection:
        calls = 0

        def execute(self, statement, params=None):
            self.calls += 1
            sql = str(statement)
            assert "xhs_note_snapshots" in sql
            assert "xhs_post_comments" not in sql
            return [
                {"title": "早秋穿搭", "description": "轻盈叠穿"},
                {"title": "蕾丝细节", "description": "浪漫风格"},
                {"title": "通勤风格", "description": "简约剪裁"},
            ]

    class FakeEngine:
        def connect(self):
            class Context:
                def __enter__(self):
                    return FakeConnection()

                def __exit__(self, *_args):
                    return False

            return Context()

    posts = benchmark.fetch_social_posts(FakeEngine(), brand="atlas", snapshot_date="2026-08-30")

    assert posts[0]["title"] == "早秋穿搭"
    assert len(posts) == 3
    assert all("comments" not in post for post in posts)


def test_social_posts_are_null_when_no_hot_posts():
    class EmptyConnection:
        def execute(self, *_args, **_kwargs):
            return []

    class EmptyEngine:
        def connect(self):
            class Context:
                def __enter__(self):
                    return EmptyConnection()

                def __exit__(self, *_args):
                    return False

            return Context()

    assert benchmark.fetch_social_posts(EmptyEngine(), brand="atlas", snapshot_date="2026-08-30") is None


def test_load_fixture_separates_ineligible_queries(tmp_path):
    fixture = tmp_path / "dataset.json"
    fixture.write_text(
        json.dumps({
            "fixture_version": 1,
            "brand": "atlas",
            "evidence_source": "behavior_sql_snapshot",
            "queries": [
                _query("q1"),
                {**_query("q2"), "ranking_eligible": False},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    metadata, eligible = benchmark.load_fixture(fixture, brand="atlas")

    assert metadata["eligible_query_count"] == 1
    assert metadata["skipped_query_count"] == 1
    assert [item["query"] for item in eligible] == ["q1"]


def test_run_batch_collects_usage_and_quality_without_external_writes(monkeypatch):
    expected = [_query("q1")]

    class FakeModel:
        def invoke(self, _messages):
            return SimpleNamespace(
                content=json.dumps({
                    "results": [{
                        "query": "q1",
                        "should_reorder": True,
                        "preference_description": "偏好高点击的轻盈材质",
                        "reason": None,
                    }]
                }, ensure_ascii=False),
                usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            )

    monkeypatch.setattr(benchmark, "_build_llm", lambda *_args, **_kwargs: FakeModel())

    result = benchmark.run_batch(
        expected,
        model_name="test-model",
        timeout=10,
        max_completion_tokens=100,
        stream=False,
        batch_index=1,
        langfuse=None,
    )

    assert result["total_tokens"] == 120
    assert result["quality"]["valid"] is True
    assert result["error"] is None


def test_stream_invocation_collects_chunks():
    class Chunk:
        def __init__(self, content, usage_metadata=None):
            self.content = content
            self.usage_metadata = usage_metadata

        def __add__(self, other):
            return Chunk(self.content + other.content, other.usage_metadata or self.usage_metadata)

    class FakeStreamModel:
        def stream(self, _messages, stream_usage=True):
            yield Chunk('{"results":')
            yield Chunk('[]}', {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6})

    response, raw, chunks = benchmark._invoke(FakeStreamModel(), [], stream=True)

    assert chunks == 2
    assert raw == '{"results":[]}'
    assert response.content == '{"results":[]}'


def test_stream_invocation_rejects_empty_stream():
    class EmptyStreamModel:
        def stream(self, _messages, stream_usage=True):
            return iter(())

    with pytest.raises(RuntimeError, match="empty_stream_response"):
        benchmark._invoke(EmptyStreamModel(), [], stream=True)


def test_run_batch_records_raw_social_posts_in_langfuse_generation(monkeypatch):
    query = _query("q1")
    query["brand_context"]["social_posts"] = [{"note_id": "n1", "note_title": "早秋穿搭", "comments": None}]

    class FakeModel:
        def invoke(self, _messages):
            return SimpleNamespace(
                content=json.dumps({"results": [{
                    "query": "q1", "should_reorder": False,
                    "preference_description": None, "reason": "证据不足",
                }]}, ensure_ascii=False),
                usage_metadata={"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
            )

    class FakeObservation:
        def __init__(self, observation_id):
            self.id = observation_id
            self.updates = []
            self.ended = False

        def update(self, **kwargs):
            self.updates.append(kwargs)

        def end(self):
            self.ended = True

    class FakeLangfuse:
        def __init__(self):
            self.generations = []
            self.flushed = False

        def create_trace_id(self, seed):
            return "trace-1"

        def start_observation(self, **kwargs):
            observation = FakeObservation("root" if kwargs["as_type"] == "chain" else "generation")
            if kwargs["as_type"] == "generation":
                self.generations.append((observation, kwargs))
            return observation

        def flush(self):
            self.flushed = True

        def get_trace_url(self, trace_id):
            return f"http://langfuse/traces/{trace_id}"

    langfuse = FakeLangfuse()
    monkeypatch.setattr(benchmark, "_build_llm", lambda *_args, **_kwargs: FakeModel())

    result = benchmark.run_batch(
        [query], model_name="test-model", timeout=10, max_completion_tokens=100,
        stream=False, batch_index=1, langfuse=langfuse,
    )

    generation_input = langfuse.generations[0][1]["input"]
    assert isinstance(generation_input, list)
    assert generation_input[0] == {"role": "system", "content": benchmark.SYSTEM_PROMPT}
    assert generation_input[1]["role"] == "user"
    user_payload = json.loads(generation_input[1]["content"])
    assert user_payload["brand_context"]["social_trends"][0]["title"] == "早秋穿搭"
    assert "note_id" not in user_payload["brand_context"]["social_trends"][0]
    assert langfuse.flushed is True
    assert result["langfuse_trace_url"].endswith("trace-1")


def test_run_aggregates_batches_and_writes_result(tmp_path, monkeypatch):
    fixture = tmp_path / "dataset.json"
    fixture.write_text(json.dumps({"brand": "atlas", "queries": [_query("q1"), _query("q2")]}, ensure_ascii=False), encoding="utf-8")

    class FakeModel:
        def invoke(self, messages):
            payload = json.loads(messages[1]["content"])
            return SimpleNamespace(
                content=json.dumps({
                    "brand": payload["brand"],
                    "strategies": {query: "证据不足" for query in payload["queries"]},
                }, ensure_ascii=False),
                usage_metadata={"input_tokens": 20, "output_tokens": 4, "total_tokens": 24},
            )

    monkeypatch.setattr(benchmark, "_build_llm", lambda *_args, **_kwargs: FakeModel())
    output_path = tmp_path / "result.json"
    args = SimpleNamespace(
        fixture=str(fixture), brand="atlas", query_limit=None, batch_query_limit=1,
        token_limit=100_000, concurrency=1, model="test-model", timeout=10,
        max_completion_tokens=100, stream=False, no_langfuse=True, output=str(output_path),
    )

    result = benchmark.run(args)

    assert result["metadata"]["batch_count"] == 2
    assert result["summary"]["total_tokens"] == 48
    assert json.loads(output_path.read_text(encoding="utf-8"))["summary"]["valid_batch_count"] == 2


def test_build_llm_reports_missing_key(monkeypatch):
    monkeypatch.setattr("src.llm.rd_client.build_rd_chat_model", lambda **_kwargs: None)

    with pytest.raises(RuntimeError, match="not_configured"):
        benchmark._build_llm("test-model", 10, 100)


def test_langfuse_is_optional_without_credentials(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setattr(benchmark.subprocess, "check_output", lambda *_args, **_kwargs: "")

    assert benchmark._langfuse_client() is None
