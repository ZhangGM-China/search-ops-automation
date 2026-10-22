from src.runtime.release_info import scheduler_config_fingerprint


def test_scheduler_config_fingerprint_accepts_release_manifest_sha():
    release_info = {
        "releaseSha": "a" * 40,
        "schemaRelease": "20261014",
        "artifactManifestSha256": "b" * 64,
    }
    assert scheduler_config_fingerprint(release_info) == "b" * 64


def test_scheduler_config_fingerprint_falls_back_for_dev_identity():
    """本地 dev 的 "unversioned" 不是 sha256，必须回退以通过运行记录 CHECK 约束。"""

    release_info = {
        "releaseSha": "dev",
        "schemaRelease": "unversioned",
        "artifactManifestSha256": "unversioned",
    }
    assert scheduler_config_fingerprint(release_info) is None
    assert scheduler_config_fingerprint({}) is None
    assert scheduler_config_fingerprint({"artifactManifestSha256": None}) is None
    # 大写或长度不符同样拒绝，避免弱化 CHECK 约束
    assert scheduler_config_fingerprint({"artifactManifestSha256": "B" * 64}) is None
    assert scheduler_config_fingerprint({"artifactManifestSha256": "b" * 63}) is None
