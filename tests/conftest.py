import os
import pytest
from sqlalchemy.orm import close_all_sessions

from network_guard import install_test_network_guard

os.environ["ENV"] = "test"
install_test_network_guard()

from src.persistence import Base, SessionLocal, reset_engine_for_testing

_sqlite_unclosed_warnings = []

# This will run before any other code in the tests directory
def pytest_configure(config):
    config.addinivalue_line(
        "filterwarnings",
        "error:.*unclosed database.*:pytest.PytestUnraisableExceptionWarning",
    )
    os.environ["AUTO_DB_URI"] = "sqlite:///:memory:"
    os.environ["ENV"] = "test"
    os.environ["RD_RDS_USER"] = "mock"
    os.environ["RD_RDS_PASS"] = "mock"
    os.environ["RD_RDS_HOST"] = "localhost"
    os.environ["RD_RDS_PORT"] = "3306"
    os.environ["RD_RDS_DB"] = "mock"


def pytest_warning_recorded(warning_message, when, nodeid, location):
    if (
        warning_message.category is pytest.PytestUnraisableExceptionWarning
        and "unclosed database" in str(warning_message.message).lower()
    ):
        _sqlite_unclosed_warnings.append((nodeid, str(warning_message.message)))


def pytest_sessionfinish(session, exitstatus):
    if _sqlite_unclosed_warnings and exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED

@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    # 强制重置 engine 为内存数据库
    test_engine = reset_engine_for_testing()
    Base.metadata.create_all(bind=test_engine)
    yield test_engine
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


@pytest.fixture(autouse=True)
def ensure_runtime_test_schema(setup_test_db):
    """Recreate the full shared schema after tests swap the global test engine."""
    bind = SessionLocal.kw.get("bind")
    if bind is not None:
        Base.metadata.create_all(bind=bind)
    yield


@pytest.fixture(autouse=True)
def reset_runtime_state_between_tests(tmp_path, monkeypatch):
    from src.integrations.operational_service import SummitService, OperationalService

    monkeypatch.setenv("BUSINESS_HTTP_STATE_DIR", str(tmp_path / "business_http"))
    OperationalService.clear_brand_config_cache()
    SummitService._shared_token = None
    yield
    try:
        close_all_sessions()
    finally:
        OperationalService.clear_brand_config_cache()
        SummitService._shared_token = None
