from __future__ import annotations

import json

from scripts.image_sync.atlas_browser_sync import build_identity, build_storage_key, load_rows


def test_load_rows_deduplicates_product_ids_and_keeps_stable_order(tmp_path) -> None:
    source = tmp_path / "atlas.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps({"product_id": "P2", "source_url": "https://static.atlas.example.test/p2.jpg"}),
                json.dumps({"product_id": "P1", "source_url": "https://static.atlas.example.test/p1.jpg"}),
                json.dumps({"product_id": "P1", "source_url": "https://static.atlas.example.test/p1-new.jpg"}),
                json.dumps({"product_id": "", "source_url": "https://static.atlas.example.test/ignored.jpg"}),
            ]
        ),
        encoding="utf-8",
    )

    rows = load_rows(source)

    assert [row["product_id"] for row in rows] == ["P1", "P2"]
    assert rows[0]["source_url"] == "https://static.atlas.example.test/p1.jpg"


def test_identity_and_storage_key_are_deterministic() -> None:
    row = {"product_id": "P1", "source_url": "https://static.atlas.example.test/p1.jpg"}

    assert build_identity(row) == "atlas|P1|https://static.atlas.example.test/p1.jpg"
    assert build_storage_key("query-governance/images/prod/v1", row["source_url"]).startswith(
        "query-governance/images/prod/v1/"
    )
