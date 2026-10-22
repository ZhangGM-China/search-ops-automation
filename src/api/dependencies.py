"""Authentication and permission dependencies for API handlers."""

from __future__ import annotations

import os
import hashlib
import hmac
import time
from fastapi import HTTPException, Depends, Request, status
from fastapi.security import OAuth2PasswordBearer
from typing import List, Optional, Dict, Any
import datetime
import json
import secrets
import jwt
from passlib.context import CryptContext
from src.persistence import SessionLocal, User, PLATFORM_ADMIN_ROLE, BRAND_ADMIN_ROLE, BRAND_MEMBER_ROLE, ACTION_MANAGE_BRAND_USERS, DEFAULT_PLATFORM_ADMIN_ACTION_PERMISSIONS, DEFAULT_BRAND_ADMIN_ACTION_PERMISSIONS, DEFAULT_BRAND_MEMBER_ACTION_PERMISSIONS
from src.integrations.operational_service import OperationalService
from src.config import BRAND_CONFIGS, get_env
from src.persistence.schema import DEFAULT_ADMIN_PAGE_PERMISSIONS
import logging
logger = logging.getLogger(__name__)

def get_secret_key() -> str:
    secret_key = os.getenv("SECRET_KEY")
    if secret_key:
        return secret_key
    if get_env() == "prod":
        raise RuntimeError("生产环境必须配置 SECRET_KEY")
    return "search_ops_test_secret_key"

SECRET_KEY = get_secret_key()
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 # 1天

service = OperationalService()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")

# --- 认证工具函数 ---

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def generate_temporary_password() -> str:
    return secrets.token_urlsafe(12)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def _user_authorization_version(user: User) -> str:
    updated_at = user.updated_at.isoformat(timespec="microseconds") if user.updated_at else ""
    security_state = [
        "authorization-version-v1",
        str(user.username or ""),
        str(user.password_hash or ""),
        bool(user.is_active),
        bool(user.is_admin),
        str(user.role or ""),
        sorted(str(item) for item in _json_list(user.page_permissions)),
        sorted(str(item) for item in _json_list(user.brand_permissions)),
        sorted(str(item) for item in _json_list(user.action_permissions)),
        updated_at,
    ]
    serialized = json.dumps(security_state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hmac.new(
        SECRET_KEY.encode("utf-8"),
        serialized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _next_user_authorization_version(previous: Optional[datetime.datetime]) -> datetime.datetime:
    now = datetime.datetime.now()
    if previous is not None:
        return max(now, previous + datetime.timedelta(seconds=1))
    return now


def _log_auth_event(event: str, *, outcome: str, user_id: Optional[int] = None) -> None:
    log = logger.warning if outcome == "rejected" else logger.info
    log(
        "authentication event=%s outcome=%s userId=%s",
        str(event or "unknown"),
        outcome,
        user_id,
    )

def get_current_user(request: Request, token: str = Depends(oauth2_scheme)):
    started = time.perf_counter()
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            username: str = payload.get("sub")
            if username is None:
                _log_auth_event("subject_missing", outcome="rejected")
                raise credentials_exception
        except jwt.PyJWTError:
            _log_auth_event("token_invalid", outcome="rejected")
            raise credentials_exception

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == username).first()
            if user is None:
                _log_auth_event("user_missing", outcome="rejected")
                raise credentials_exception
            if not bool(user.is_active):
                _log_auth_event("account_inactive", outcome="rejected", user_id=int(user.id or 0))
                raise credentials_exception
            token_version = payload.get("auth_ver")
            if not isinstance(token_version, str) or not token_version:
                _log_auth_event("authorization_version_missing", outcome="rejected", user_id=int(user.id or 0))
                raise credentials_exception
            current_version = _user_authorization_version(user)
            if not hmac.compare_digest(token_version, current_version):
                _log_auth_event("authorization_version_changed", outcome="rejected", user_id=int(user.id or 0))
                raise credentials_exception
            return user
        finally:
            db.close()
    finally:
        if request is not None:
            request.state.auth_duration_ms = (time.perf_counter() - started) * 1000

def check_page_permission(page: str):
    async def permission_dependency(user: User = Depends(get_current_user)):
        permissions = _json_list(user.page_permissions)
        if not _is_platform_admin(user) and page not in permissions:
            raise HTTPException(status_code=403, detail="Permission denied for this page")
        return user
    return permission_dependency


def _json_list(raw_value: Any) -> List[Any]:
    if isinstance(raw_value, list):
        return raw_value
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _json_object(raw_value: Any) -> Dict[str, Any]:
    if isinstance(raw_value, dict):
        return raw_value
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _user_role(user: User) -> str:
    role = str(getattr(user, "role", "") or "").strip()
    if role:
        return role
    if bool(getattr(user, "is_admin", 0)):
        return PLATFORM_ADMIN_ROLE
    return BRAND_MEMBER_ROLE


def _default_action_permissions_for_role(role: str) -> List[str]:
    if role == PLATFORM_ADMIN_ROLE:
        return list(DEFAULT_PLATFORM_ADMIN_ACTION_PERMISSIONS)
    if role == BRAND_ADMIN_ROLE:
        return list(DEFAULT_BRAND_ADMIN_ACTION_PERMISSIONS)
    return list(DEFAULT_BRAND_MEMBER_ACTION_PERMISSIONS)


def _user_action_permissions(user: User) -> List[str]:
    if _is_platform_admin(user):
        return list(DEFAULT_PLATFORM_ADMIN_ACTION_PERMISSIONS)
    permissions = [str(item) for item in _json_list(getattr(user, "action_permissions", None)) if str(item).strip()]
    if permissions:
        return _normalize_action_permissions(permissions, _user_role(user))
    return _default_action_permissions_for_role(_user_role(user))


def _has_action_permission(user: User, action: str) -> bool:
    if _user_role(user) == PLATFORM_ADMIN_ROLE:
        return True
    return action in _user_action_permissions(user)


def _ensure_action_permission(user: User, action: str, detail: str = "Permission denied for this action") -> None:
    if not _has_action_permission(user, action):
        raise HTTPException(status_code=403, detail=detail)


def _is_platform_admin(user: User) -> bool:
    return _user_role(user) == PLATFORM_ADMIN_ROLE


def _is_brand_admin(user: User) -> bool:
    return _user_role(user) == BRAND_ADMIN_ROLE


def _serialize_user(user: User) -> Dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "isAdmin": _is_platform_admin(user),
        "role": _user_role(user),
        "pagePermissions": list(dict.fromkeys(DEFAULT_ADMIN_PAGE_PERMISSIONS + _json_list(user.page_permissions))) if _is_platform_admin(user) else _json_list(user.page_permissions),
        "brandPermissions": _user_brand_permissions(user),
        "actionPermissions": _user_action_permissions(user),
        "isActive": bool(user.is_active),
    }


def _user_brand_permissions(user: User) -> List[str]:
    if _is_platform_admin(user):
        return list(BRAND_CONFIGS)
    return [str(item) for item in _json_list(user.brand_permissions) if str(item).strip()]


def _normalize_brand_permissions(payload_brands: Any, allowed_brands: Optional[List[str]] = None) -> List[str]:
    brands = [str(item).strip() for item in _json_list(payload_brands) if str(item).strip()]
    if allowed_brands is None:
        return brands
    allowed = set(allowed_brands)
    return [brand for brand in brands if brand in allowed]


def _allowed_action_permissions_for_role(role: str) -> List[str]:
    if role == PLATFORM_ADMIN_ROLE:
        return DEFAULT_PLATFORM_ADMIN_ACTION_PERMISSIONS
    return DEFAULT_BRAND_ADMIN_ACTION_PERMISSIONS


def _normalize_action_permissions(payload_permissions: Any, role: str) -> List[str]:
    allowed_actions = set(_allowed_action_permissions_for_role(role))
    return [
        permission
        for permission in (str(item).strip() for item in _json_list(payload_permissions))
        if permission and permission in allowed_actions
    ]


def _can_manage_user(manager: User, target: User) -> bool:
    if _is_platform_admin(manager):
        return True
    if not _is_brand_admin(manager) or not _has_action_permission(manager, ACTION_MANAGE_BRAND_USERS):
        return False
    if _user_role(target) != BRAND_MEMBER_ROLE:
        return False
    manager_brands = set(_user_brand_permissions(manager))
    target_brands = set(_user_brand_permissions(target))
    return bool(target_brands) and target_brands.issubset(manager_brands)

__all__ = tuple(
    name for name in globals()
    if not name.startswith("__") and name != "router"
)
