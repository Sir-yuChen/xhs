from __future__ import annotations

import logging

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.core.roles import normalize_role
from backend.app.core.security import create_access_token, create_refresh_token, decode_token, hash_password, verify_password
from backend.app.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class AuthCredentials(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=6, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


def _serialize_user(user: User) -> dict:
    """用户对象的响应形状（注册 / 登录 / 刷新三处共用）。

    T-104：`role` 供前端决定是否显示/启用「全局系统设置」入口。注意这是
    **提示性**信息，真正的权限判定一律在后端 `require_admin` 处。
    """
    return {"id": user.id, "username": user.username, "role": normalize_role(user.role)}


def _token_response(user: User) -> dict:
    return {
        "access_token": create_access_token(user.id),
        "refresh_token": create_refresh_token(user.id),
        "token_type": "bearer",
        "user": _serialize_user(user),
    }


@router.post("/register")
def register(
    credentials: AuthCredentials,
    request: Request,
    db: Session = Depends(get_db),
):
    username = credentials.username.strip()
    existing_user = db.scalar(select(User).where(User.username == username))
    if existing_user is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username already exists")

    user = User(username=username, password_hash=hash_password(credentials.password))
    db.add(user)
    db.commit()
    db.refresh(user)

    # DEF-066 / 设计 15 §4.5：内置任务行**按用户各持一份** ⇒ 新用户注册即播种
    # （启动播种只覆盖「启动时已存在的用户」）。注册完打开任务中心就有这 7 条。
    # 播种失败**可容忍但不静默**：记 warning，用户仍能在任务中心手动新建。
    from backend.app.services.scheduler_service import (
        seed_builtin_jobs_for_user,
        sync_job_registration,
    )

    try:
        created = seed_builtin_jobs_for_user(db, user.id)
    except Exception as exc:  # noqa: BLE001 - 注册不被子系统问题拖死
        logger.warning("register: seed builtin jobs for user %s failed: %s", user.id, exc)
    else:
        scheduler = getattr(request.app.state, "scheduler", None)
        if scheduler is not None and scheduler.running:
            for job in created:
                sync_job_registration(scheduler, job)

    # 设计 10 §7.3（2026-09-27）：运行时配置行同样**按用户各持一份** ⇒ 注册即播种，
    # 新用户打开设置页就看得见基线（启动播种只覆盖「启动时已存在的用户」）。
    try:
        from backend.app.services.settings_service import seed_settings_for_user

        seed_settings_for_user(db, user.id)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - 注册不被子系统问题拖死
        db.rollback()
        logger.warning("register: seed runtime settings for user %s failed: %s", user.id, exc)

    return _token_response(user)


@router.post("/login")
def login(credentials: AuthCredentials, db: Session = Depends(get_db)):
    username = credentials.username.strip()
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not verify_password(credentials.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    return _token_response(user)


@router.post("/refresh")
def refresh_token(payload: RefreshRequest, db: Session = Depends(get_db)):
    decoded = decode_token(payload.refresh_token)
    if decoded.get("token_type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    user = db.get(User, decoded["user_id"])
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return {"access_token": create_access_token(user.id), "token_type": "bearer"}


@router.post("/logout")
def logout():
    return {"status": "ok"}


@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return _serialize_user(current_user)
