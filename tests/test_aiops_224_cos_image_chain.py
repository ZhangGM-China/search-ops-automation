from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_local_cos_template_targets_verified_test_bucket_and_public_base_url():
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "QUERY_GOVERNANCE_COS_REGION=ap-shanghai" in env_example
    assert "QUERY_GOVERNANCE_COS_BUCKET=searchops-ops-data-example" in env_example
    assert (
        "QUERY_GOVERNANCE_COS_PUBLIC_BASE_URL="
        "https://searchops-ops-data-example.cos.ap-shanghai.myqcloud.com"
    ) in env_example
    assert "QUERY_GOVERNANCE_COS_KEY_PREFIX=query-governance/images/test/v1" in env_example
