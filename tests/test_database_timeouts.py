from unittest.mock import MagicMock, patch

from src.persistence import database


def test_automation_mysql_engine_has_bounded_socket_timeouts_by_default(monkeypatch):
    monkeypatch.setenv("AUTO_DB_URI_PROD", "mysql+pymysql://user:pass@db.example/automation")
    monkeypatch.delenv("AUTO_DB_CONNECT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AUTO_DB_READ_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AUTO_DB_WRITE_TIMEOUT_SECONDS", raising=False)
    created_engine = MagicMock()

    with patch("src.config.get_env", return_value="prod"), patch(
        "src.persistence.session.create_engine",
        return_value=created_engine,
    ) as create_engine:
        assert database.get_engine() is created_engine

    assert create_engine.call_args.kwargs["connect_args"] == {
        "connect_timeout": 10,
        "read_timeout": 60,
        "write_timeout": 60,
    }


def test_automation_mysql_engine_accepts_positive_timeout_overrides(monkeypatch):
    monkeypatch.setenv("AUTO_DB_URI_PROD", "mysql+pymysql://user:pass@db.example/automation")
    monkeypatch.setenv("AUTO_DB_CONNECT_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("AUTO_DB_READ_TIMEOUT_SECONDS", "31")
    monkeypatch.setenv("AUTO_DB_WRITE_TIMEOUT_SECONDS", "32")

    with patch("src.config.get_env", return_value="prod"), patch(
        "src.persistence.session.create_engine",
        return_value=MagicMock(),
    ) as create_engine:
        database.get_engine()

    assert create_engine.call_args.kwargs["connect_args"] == {
        "connect_timeout": 7,
        "read_timeout": 31,
        "write_timeout": 32,
    }
