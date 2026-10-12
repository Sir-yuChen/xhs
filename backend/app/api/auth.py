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
    # DEF-022：令牌携带 `token_version`，改密 / 登出后旧令牌立即失效。
    version = int(user.token_version or 0)
    return {
        "access_token": create_access_token(user.id, version),
        "refresh_token": create_refresh_token(user.id, version),
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

    # 设计 17 §13.5.4（v2.24）：内置提示词模板 / 模块同样**按用户各持一份** ⇒ 注册即播种。
    # ⚠️ 少了这一步，新用户第一次打开「生成 AI 商品图」仍然**选不到任何模板**
    # （启动播种只覆盖「启动时已存在的用户」）—— 那正是本轮要修的缺口。
    try:
        from backend.app.api.llm_prompts import seed_builtin_prompt_templates

        seed_builtin_prompt_templates(db, user_id=user.id)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - 注册不被子系统问题拖死
        db.rollback()
        logger.warning("register: seed prompt templates for user %s failed: %s", user.id, exc)

    # 2026-10-13 用户口径：**国内常见 6 个服务商**作为种子数据（用户登录后只需配 KEY
    # 并点「测试连接」即可用）。同样**按用户各持一份** ⇒ 注册即播种；种子**不含 KEY**，
    # 存量用户不主动补种（避免替用户建一堆没配 KEY 的服务商，同 §4.5.1 纪律）。
    try:
        from backend.app.services.llm_gateway_seed_service import (
            seed_default_business_map_for_user,
            seed_gateway_providers_for_user,
        )

        seed_gateway_providers_for_user(db, user.id)
        # 小红书平台的默认业务点映射（文本→DeepSeek、图片→通义千问）；
        # 幂等（已有映射不碰），且只在服务商种子存在时才写（缺一即跳过）。
        seed_default_business_map_for_user(db, user.id, platform="xhs")
        db.commit()
    except Exception as exc:  # noqa: BLE001 - 注册不被子系统问题拖死
        db.rollback()
        logger.warning("register: seed gateway providers for user %s failed: %s", user.id, exc)

    return _token_response(user)


#: 登录失败限流（DEF-030 / T-21）：连续失败 N 次锁定 M 秒。
#: ⚠️ **只按用户名单键**（不按 IP）—— 本应用是桌面单机、无反向代理，
#: 按 IP 会把「同一台机器上的正常用户」一起锁掉；按用户名锁才对准攻击面
#: （猜某个账号的密码）。代价是攻击者可以枚举**不存在的**用户名（那不会触发锁定），
#: 但它拿不到任何信息（存在与不存在都回同一句 401）。
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 300.0

#: `{username的小写: deque(失败时间戳)}` —— 进程级内存态。
#: ⚠️ 重启即清空：本机制防的是**在线爆破**（几分钟内几百次），不是持久封禁；
#: 要持久化就得加表，而桌面单机场景不值得为它加一张表。
_login_failures: dict[str, list[float]] = {}


def _login_locked_for(username: str, now: float) -> float:
    """返回还需等待的秒数（0 = 未锁定）。"""
    key = username.strip().lower()
    stamps = _login_failures.get(key)
    if not stamps:
        return 0.0
    # 只用窗口内的失败计数（窗口外的旧失败不算）
    recent = [t for t in stamps if now - t < LOGIN_LOCKOUT_SECONDS]
    _login_failures[key] = recent
    if len(recent) >= LOGIN_MAX_FAILURES:
        return max(0.0, LOGIN_LOCKOUT_SECONDS - (now - recent[-1]))
    return 0.0


def _record_login_failure(username: str, now: float) -> None:
    _login_failures.setdefault(username.strip().lower(), []).append(now)


def _clear_login_failures(username: str) -> None:
    _login_failures.pop(username.strip().lower(), None)


@router.post("/login")
def login(credentials: AuthCredentials, db: Session = Depends(get_db)):
    import time as _time

    username = credentials.username.strip()
    now = _time.monotonic()
    # DEF-030：连续失败达阈值 ⇒ 429（带 Retry-After，让调用方知道等多久）。
    wait = _login_locked_for(username, now)
    if wait > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"登录失败次数过多，请 {int(wait) + 1} 秒后再试。",
            headers={"Retry-After": str(int(wait) + 1)},
        )
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not verify_password(credentials.password, user.password_hash):
        _record_login_failure(username, now)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    # 成功 ⇒ 清掉该用户名的失败计数（不让「之前失败过」影响下次）
    _clear_login_failures(username)
    return _token_response(user)


@router.post("/refresh")
def refresh_token(payload: RefreshRequest, db: Session = Depends(get_db)):
    decoded = decode_token(payload.refresh_token)
    if decoded.get("token_type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    user = db.get(User, decoded["user_id"])
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    # DEF-022：版本不匹配 ⇒ 该 refresh token 已被撤销（改密 / 登出 / 强制下线）。
    if int(decoded.get("ver") or 0) != int(user.token_version or 0):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked",
        )
    return {
        "access_token": create_access_token(user.id, int(user.token_version or 0)),
        "token_type": "bearer",
    }


@router.post("/logout")
def logout(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """登出：**真的撤销**已签发的 refresh token（DEF-022）。

    ⚠️ 实现 = 把 `users.token_version` +1 —— 无状态令牌没有「删掉某一个」的手段，
    能做的只有「让这一代全部失效」。因此登出会同时失效该用户**所有设备**的 refresh
    token（access token 最长 30 分钟后自然过期）。这是**刻意的取舍**：
    宁可多失效几个，也不要留一个「点了登出却还能续期」的洞。
    """
    current_user.token_version = int(current_user.token_version or 0) + 1
    db.add(current_user)
    db.commit()
    return {"status": "ok", "revoked": True}


@router.post("/revoke-all-sessions")
def revoke_all_sessions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """强制下线**所有**会话（含当前这条）—— 改密后应当调用。

    与 `/logout` 是同一套机制（都靠 `token_version`），分开命名是为了让「改密后
    该干什么」这件事在 API 面上有明确答案：调它，而不是靠用户自己记得重新登录。
    """
    current_user.token_version = int(current_user.token_version or 0) + 1
    db.add(current_user)
    db.commit()
    return {"status": "ok", "revoked": True}


@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return _serialize_user(current_user)
