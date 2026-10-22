from pathlib import Path

from src.persistence import session


def test_repository_root_is_database_path_base():
    repository_root = Path(__file__).resolve().parents[1]
    assert session.BASE_DIR == repository_root
    assert session.DEFAULT_TEST_DB_PATH == (
        repository_root / "data" / "local_db" / "search_ops_automation_test.db"
    )


def test_relative_sqlite_uri_resolves_under_repository_root():
    resolved = session._ensure_sqlite_parent_dir(
        "sqlite:///data/local_db/search_ops_automation_test.db"
    )
    expected = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "local_db"
        / "search_ops_automation_test.db"
    )
    assert resolved == f"sqlite:///{expected}"
