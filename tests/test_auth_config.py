import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
import json
import os
import unittest.mock

# --- Consistent Test Environment Setup ---
from src.persistence import database
from src.api import app

TEST_ADMIN_PASSWORD = database.TEST_DEFAULT_ADMIN_PASSWORD

# Create a clean engine for this test file
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

@pytest.fixture(autouse=True)
def setup_db():
    previous_session_options = dict(database.SessionLocal.kw)
    database.SessionLocal.configure(bind=engine, expire_on_commit=False)
    try:
        with unittest.mock.patch.dict(
            os.environ,
            {"INITIAL_ADMIN_PASSWORD": TEST_ADMIN_PASSWORD},
            clear=False,
        ), unittest.mock.patch("src.persistence.engine", engine):
            from src.persistence import Base, init_admin_user

            Base.metadata.create_all(bind=engine)
            init_admin_user()
            yield
            database.close_all_sessions()
            Base.metadata.drop_all(bind=engine)
    finally:
        database.SessionLocal.configure(**previous_session_options)

client = TestClient(app)

@pytest.fixture(scope="module", autouse=True)
def cleanup_module_resources():
    yield
    client.close()
    engine.dispose()

def test_login_success():
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": TEST_ADMIN_PASSWORD}
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["user"]["username"] == "admin"
    assert "/no-result-governance" in data["user"]["pagePermissions"]
    assert "/task-board" not in data["user"]["pagePermissions"]
    assert "/ops-dashboard" in data["user"]["pagePermissions"]
    assert "/smart-assistant" in data["user"]["pagePermissions"]

def test_init_admin_user_merges_missing_default_permissions():
    from src.persistence import SessionLocal, User, init_admin_user

    db = SessionLocal()
    admin = db.query(User).filter(User.username == "admin").one()
    admin.page_permissions = json.dumps(["/", "/custom-local-page"])
    admin.brand_permissions = json.dumps(["atlas"])
    db.commit()
    db.close()

    init_admin_user()

    db = SessionLocal()
    admin = db.query(User).filter(User.username == "admin").one()
    pages = json.loads(admin.page_permissions)
    brands = json.loads(admin.brand_permissions)
    db.close()

    assert "/custom-local-page" in pages
    assert "/no-result-governance" in pages
    assert "/task-board" not in pages
    assert "/ops-dashboard" in pages
    assert "/smart-assistant" in pages
    assert "harbor" in brands
    assert "riviera" in brands


def test_test_admin_password_is_an_explicit_non_secret_placeholder():
    assert TEST_ADMIN_PASSWORD == "test-admin-password"


def test_init_admin_user_restores_fixed_test_password():
    from passlib.context import CryptContext
    from src.persistence import SessionLocal, User, init_admin_user

    password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    db = SessionLocal()
    admin = db.query(User).filter(User.username == "admin").one()
    admin.password_hash = password_context.hash("unexpected-imported-password")
    db.commit()
    db.close()

    with unittest.mock.patch.dict(
        os.environ,
        {"ENV": "test", "INITIAL_ADMIN_PASSWORD": "placeholder-must-be-ignored"},
        clear=False,
    ):
        init_admin_user()

    db = SessionLocal()
    admin = db.query(User).filter(User.username == "admin").one()
    assert password_context.verify(TEST_ADMIN_PASSWORD, admin.password_hash)
    db.close()

def test_login_failure():
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "wrongpassword"}
    )
    assert response.status_code == 400

def test_prod_requires_secret_key():
    from src.api import get_secret_key
    with unittest.mock.patch.dict(os.environ, {"ENV": "prod"}, clear=False):
        with unittest.mock.patch.dict(os.environ, {"SECRET_KEY": ""}, clear=False):
            os.environ.pop("SECRET_KEY", None)
            with pytest.raises(RuntimeError):
                get_secret_key()

def test_prod_requires_initial_admin_password():
    from src.persistence import get_initial_admin_password
    with unittest.mock.patch.dict(os.environ, {"ENV": "prod"}, clear=False):
        os.environ.pop("INITIAL_ADMIN_PASSWORD", None)
        os.environ.pop("ADMIN_INITIAL_PASSWORD", None)
        with pytest.raises(ValueError):
            get_initial_admin_password()

def test_get_me():
    login_resp = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": TEST_ADMIN_PASSWORD}
    )
    token = login_resp.json()["access_token"]
    
    response = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json()["username"] == "admin"

def test_admin_list_users():
    login_resp = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": TEST_ADMIN_PASSWORD}
    )
    token = login_resp.json()["access_token"]
    
    response = client.get(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert len(response.json()) >= 1

def test_create_user_and_permission_check():
    login_resp = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": TEST_ADMIN_PASSWORD}
    )
    token = login_resp.json()["access_token"]
    
    new_user_payload = {
        "username": "operator1",
        "pagePermissions": ["/blacklist"],
        "brandPermissions": ["atlas"]
    }
    create_resp = client.post(
        "/api/admin/users",
        json=new_user_payload,
        headers={"Authorization": f"Bearer {token}"}
    )
    assert create_resp.status_code == 200
    temporary_password = create_resp.json()["temporaryPassword"]
    
    sub_login_resp = client.post(
        "/api/auth/login",
        json={"username": "operator1", "password": temporary_password}
    )
    sub_token = sub_login_resp.json()["access_token"]
    
    resp_blacklist = client.get(
        "/api/dashboard/blacklist-logs",
        headers={"Authorization": f"Bearer {sub_token}"}
    )
    assert resp_blacklist.status_code == 200
