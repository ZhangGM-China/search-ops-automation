from pathlib import Path

from tests.manual_scripts import benchmark_relevance as benchmark


def test_parse_results_does_not_call_unreviewed_non_badcases_false_positives():
    items = [
        {
            "item_id": "bad-item",
            "query": "衬衫 女士",
            "product_id": "P1",
            "expected_is_relevant": False,
            "bad_case_id": "bad-01",
            "image_available": True,
            "product_payload": {"productName": "衬衫"},
        },
        {
            "item_id": "unreviewed-item",
            "query": "裤子 女士",
            "product_id": "P2",
            "expected_is_relevant": None,
            "bad_case_id": None,
            "image_available": True,
            "product_payload": {"productName": "裙裤"},
        },
    ]
    raw = '{"results":[{"query":"衬衫 女士","relevant":[0],"image_checked":[1],"reasons":{"1":"图片错配"}},{"query":"裤子 女士","relevant":[0],"image_checked":[1],"reasons":{"1":"商品边界待确认"}}]}'

    quality = benchmark._parse_results(raw, items)

    assert quality["bad_case_recall"] == 1.0
    assert quality["bad_case_not_detected_details"] == []
    assert quality["reviewed_normal_product_count"] == 0
    assert quality["reviewed_normal_false_positive_count"] == 0
    assert quality["unreviewed_product_count"] == 1
    assert quality["unreviewed_predicted_false_count"] == 1
    assert quality["unreviewed_predicted_false_items"] == [{
        "item_id": "unreviewed-item",
        "query": "裤子 女士",
        "product_id": "P2",
        "product_name": "裙裤",
        "reason": "商品边界待确认",
    }]
    assert quality["normal_false_positive_count"] is None


def test_load_fixture_normalizes_legacy_non_badcase_placeholder_labels(tmp_path):
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    (fixture_dir / "dataset.json").write_text(
        '{"products":[{"product_id":"P1","image_url":"https://example.test/p1.jpg","expected_is_relevant":true,"bad_case_id":null},{"product_id":"P2","image_url":"https://example.test/p2.jpg","expected_is_relevant":false,"bad_case_id":"bad-01"}]}',
        encoding="utf-8",
    )

    items = benchmark._load_fixture(fixture_dir)

    assert items[0]["expected_is_relevant"] is None
    assert items[0]["gold_label_source"] == "unreviewed"
    assert items[1]["expected_is_relevant"] is False


def test_load_benchmark_environment_falls_back_to_main_checkout_env(monkeypatch, tmp_path):
    worktree = tmp_path / "aiops-230"
    main_checkout = tmp_path / "aiops"
    worktree.mkdir()
    main_checkout.mkdir()
    (main_checkout / ".env").write_text("RD_LLM_API_KEY=from-main\n", encoding="utf-8")

    loaded: list[tuple[Path, bool]] = []

    def fake_load_dotenv(path, override=False):
        loaded.append((Path(path), override))
        return True

    monkeypatch.setattr(benchmark, "load_dotenv", fake_load_dotenv)
    monkeypatch.setattr(
        benchmark.subprocess,
        "check_output",
        lambda *args, **kwargs: str(main_checkout / ".git") + "\n",
    )

    benchmark._load_benchmark_environment(worktree)

    assert loaded == [(main_checkout / ".env", False)]


def test_load_benchmark_environment_prefers_worktree_env(monkeypatch, tmp_path):
    worktree = tmp_path / "aiops-230"
    main_checkout = tmp_path / "aiops"
    worktree.mkdir()
    main_checkout.mkdir()
    (worktree / ".env").write_text("RD_LLM_API_KEY=from-worktree\n", encoding="utf-8")
    (main_checkout / ".env").write_text("RD_LLM_API_KEY=from-main\n", encoding="utf-8")

    loaded: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        benchmark,
        "load_dotenv",
        lambda path, override=False: loaded.append((Path(path), override)) or True,
    )
    monkeypatch.setattr(
        benchmark.subprocess,
        "check_output",
        lambda *args, **kwargs: str(main_checkout / ".git") + "\n",
    )

    benchmark._load_benchmark_environment(worktree)

    assert loaded == [(worktree / ".env", False)]


def test_load_benchmark_environment_is_safe_when_git_metadata_is_unavailable(monkeypatch, tmp_path):
    worktree = tmp_path / "aiops-230"
    worktree.mkdir()
    monkeypatch.setattr(
        benchmark.subprocess,
        "check_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("git unavailable")),
    )

    assert benchmark._load_benchmark_environment(worktree) == []
