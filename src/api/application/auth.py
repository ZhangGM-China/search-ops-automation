"""Authentication application handlers."""

from __future__ import annotations

from fastapi import Body, HTTPException, Depends
from typing import Dict
from src.persistence import SessionLocal, User
from fastapi import APIRouter
from ..dependencies import *  # noqa: F401,F403

router = APIRouter()

# --- 认证接口 ---

@router.post("/api/auth/login")
def login(payload: Dict = Body(...)):
    username = payload.get("username")
    password = payload.get("password")

    db = SessionLocal()
    user = db.query(User).filter(User.username == username, User.is_active == 1).first()
    db.close()

    if not user or not verify_password(password, user.password_hash):
        _log_auth_event(
            "login_failed",
            outcome="rejected",
            user_id=int(user.id or 0) if user else None,
        )
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    access_token = create_access_token(data={"sub": user.username, "auth_ver": _user_authorization_version(user)})
    _log_auth_event("login", outcome="accepted", user_id=int(user.id or 0))
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": _serialize_user(user),
    }

@router.get("/api/auth/me")
def get_me(user: User = Depends(get_current_user)):
    return _serialize_user(user)


def _revoke_all_user_sessions(user_id: int, *, event: str) -> Dict[str, str]:
    for _ in range(3):
        db = SessionLocal()
        try:
            db_user = db.get(User, user_id)
            if not db_user:
                raise HTTPException(status_code=404, detail="User not found")
            previous = db_user.updated_at
            filters = [User.id == user_id]
            if previous is None:
                filters.append(User.updated_at.is_(None))
            else:
                filters.append(User.updated_at == previous)
            next_version = _next_user_authorization_version(previous)
            updated_rows = db.query(User).filter(*filters).update(
                {User.updated_at: next_version},
                synchronize_session=False,
            )
            if updated_rows != 1:
                db.rollback()
                continue
            db.commit()
            _log_auth_event(event, outcome="accepted", user_id=int(db_user.id or 0))
            return {"msg": "All sessions revoked"}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    _log_auth_event(event, outcome="rejected", user_id=user_id)
    raise HTTPException(status_code=409, detail="Session revocation conflict")


@router.post("/api/auth/logout")
def logout(user: User = Depends(get_current_user)):
    return _revoke_all_user_sessions(int(user.id or 0), event="logout")


@router.post("/api/auth/change-password")
def change_password(payload: Dict = Body(...), user: User = Depends(get_current_user)):
    old_password = payload.get("oldPassword")
    new_password = payload.get("newPassword")

    if not verify_password(old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="旧密码错误")

    db = SessionLocal()
    db_user = db.get(User, user.id)
    db_user.password_hash = get_password_hash(new_password)
    db.commit()
    db.close()
    return {"msg": "密码修改成功"}

# --- 账号管理接口 (Admin Only) ---

__all__ = tuple(
    name for name in globals()
    if not name.startswith("__") and name != "router"
)
