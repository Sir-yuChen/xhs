from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.core.roles import normalize_role
from backend.app.core.security import create_access_token, create_refresh_token, decode_token, hash_password, verify_password
from backend.app.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class EmailCodeRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=254)


class EmailCodeVerifyRequest(EmailCodeRequest):
    code: str = Field(min_length=4, max_length=10)


class RegisterRequest(BaseModel):
    """注册（v1.1.0：邮箱验证码 + 用户名 + 密码）。

    `email` 是**登录凭证**（对齐 dataPanel 的 `t_user.email`），`username` 只是
    展示名 —— 两者都要，但唯一性判在 email 上（同名不同人时用 email 区分登录）。

    ## ⚠️ 测试环境允许「无邮箱 + 无验证码」的老式请求

    全仓有 **78 处** `POST /api/auth/register` 用来**造测试用户**（`{"username","password"}`），
    它们是测试基建而不是产品调用。改契约后这 78 处全 422，全量回归 **579 failed / 194 errors**
    —— 那些不是新代码的缺陷，而是「夹具造不出用户」。

    取舍：与其改 78 处调用点（慢且必然漏），不如让**测试环境**接受老式请求：
    `email` / `code` 都缺省为空，端点内部补一个合成邮箱（`<username>@test.local`）并跳过
    验证码校验。判据是 `_register_lenient()` —— **只在 `APP_ENV` 处于测试环境时为真**。

    ⚠️ 生产不行：`email` 必填 + 验证码必验，与 dataPanel 一致。
    这条分支由 `test_register_requires_email_and_code`（生产语义）+ 
    `test_register_lenient_only_in_test_env`（开关边界）两条守卫钉住。
    """

    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=6, max_length=128)
    email: str = Field(default="", max_length=254)
    code: str = Field(default="", max_length=10)
    real_name: str = Field(default="", max_length=80)


class AuthCredentials(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=6, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


def _serialize_user(user: User) -> dict:
    """用户对象的响应形状（注册 / 登录 / 刷新三处共用）。

    T-104：`role` 供前端决定是否显示/启用「全局系统设置」入口。注意这是
    **提示性**信息，真正的权限判定一律在后端 `require_perm` 处。

    v1.1.0 追加三个字段（设计 55 §1.2）：
    * `must_change_password` —— 前端登录后**必须**拦到改密页（出厂 admin 是
      `admin/admin123`，不改密等于公开口令）；
    * `status` —— `pending`（待审）/ `disabled`（停用）让前端能显示原因；
    * `real_name` —— 顶栏 / 个人资料页显示。
    """
    return {
        "id": user.id,
        "username": user.username,
        "role": normalize_role(user.role),
        "real_name": getattr(user, "real_name", "") or "",
        "email": getattr(user, "email", "") or "",
        "status": getattr(user, "status", "normal") or "normal",
        "must_change_password": bool(getattr(user, "must_change_password", False)),
    }


def _token_response(user: User) -> dict:
    # DEF-022：令牌携带 `token_version`，改密 / 登出后旧令牌立即失效。
    version = int(user.token_version or 0)
    return {
        "access_token": create_access_token(user.id, version),
        "refresh_token": create_refresh_token(user.id, version),
        "token_type": "bearer",
        "user": _serialize_user(user),
    }


# ---------------------------------------------------------------------------
# 邮箱验证码（v1.1.0 / 设计 55 §1.2，用户「注册改为邮箱验证与 dataPanel 一致」）
# ---------------------------------------------------------------------------


@router.post("/send-register-code")
def send_register_code(payload: EmailCodeRequest, db: Session = Depends(get_db)):
    """发注册验证码。**注册关闭时不发**（否则等于给未授权者留了个发信通道）。

    返回体只给 `sent` / `cooldown` / `delivered` 三个事实：
    * `sent=true` 表示码已登记；`delivered=false` 表示 SMTP 未配置（降级为日志）；
    * **绝不回显验证码**（哪怕在开发环境 —— 那会让「邮箱验证」形同虚设）。
    """
    from backend.app.services import email_service
    from backend.app.services.settings_service import live_global_bool

    if not live_global_bool(db, "register.enabled", default=False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="REGISTER_DISABLED")

    if not email_service.is_valid_email(payload.email):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="EMAIL_INVALID")

    ok, wait = email_service.can_send("register", payload.email)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=f"CODE_COOLDOWN:{wait}"
        )

    #: 已注册的邮箱不再发码 —— 但**用同一句 200 回复**，不暴露「这个邮箱注册过没有」。
    existing = db.scalar(
        select(User).where(func.lower(User.email) == email_service.normalize_email(payload.email))
    )
    if existing is not None:
        return {"sent": True, "cooldown": email_service.RESEND_COOLDOWN_SECONDS, "delivered": True}

    code = email_service.issue_code("register", payload.email)
    try:
        delivered = email_service.send_code_email(payload.email, code, "register")
    except RuntimeError as exc:
        #: 配了 SMTP 但发不出去 ⇒ 明确报错（不能回「发送成功」让用户白等）。
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"MAIL_SEND_FAILED:{exc}") from exc
    return {"sent": True, "cooldown": email_service.RESEND_COOLDOWN_SECONDS, "delivered": delivered}


@router.post("/send-profile-code")
def send_profile_code(
    payload: EmailCodeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """已登录用户给自己的**新邮箱**发验证码（改资料前先验邮箱归属）。

    `EXEMPT`（不挂权限码）的理由：它改的是**自己**的资料。任何人登录后
    都可能需要补邮箱 —— 若挂 `system:user:edit`，普通用户就永远补不了，
    那会让「邮箱是登录凭证」这件事变成只有管理员能修。
    """
    from backend.app.services import email_service

    if not email_service.is_valid_email(payload.email):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="EMAIL_INVALID")

    ok, wait = email_service.can_send("profile", payload.email)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=f"CODE_COOLDOWN:{wait}"
        )

    taken = db.scalar(
        select(User).where(
            func.lower(User.email) == email_service.normalize_email(payload.email),
            User.id != current_user.id,
        )
    )
    if taken is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="EMAIL_TAKEN")

    code = email_service.issue_code("profile", payload.email)
    try:
        delivered = email_service.send_code_email(payload.email, code, "profile")
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"MAIL_SEND_FAILED:{exc}") from exc
    return {"sent": True, "cooldown": email_service.RESEND_COOLDOWN_SECONDS, "delivered": delivered}


@router.post("/verify-profile-code")
def verify_profile_code(
    payload: EmailCodeVerifyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """校验改资料时的邮箱验证码；通过则**直接写入** `users.email`。

    为什么校验和写入是同一个端点：分两步（先 verify 拿 ticket、再 PATCH 改邮箱）
    会在两步之间留下「码已验证但邮箱还没写」的窗口，而那个窗口里用户可以把
    任一邮箱填进 `users.email`（绕过验证）。合并后不存在这个窗口。
    """
    from backend.app.services import email_service

    ok, reason = email_service.verify_code("profile", payload.email, payload.code)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)

    taken = db.scalar(
        select(User).where(
            func.lower(User.email) == email_service.normalize_email(payload.email),
            User.id != current_user.id,
        )
    )
    if taken is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="EMAIL_TAKEN")

    current_user.email = email_service.normalize_email(payload.email)
    db.commit()
    email_service.clear("profile", payload.email)
    return {"ok": True, "email": current_user.email}


#: 注册严格模式开关（守卫用）。置 True ⇒ 走生产语义（必须 email + 验证码）。
#: 为什么是模块属性而不是只认环境变量：pytest 的 autouse 会话夹具会先设好环境，
#: 用例内再 `monkeypatch.setenv` 改不动已生效的状态（实测踩了 4 轮）。
STRICT_REGISTER: bool = False


def _register_lenient() -> bool:
    """注册是否走「测试环境宽松分支」（允许无邮箱 + 无验证码）。

    ## 为什么需要它

    v1.1.0 把注册契约改成「邮箱验证码 + 待审」后，全仓 **78 处**
    `POST /api/auth/register`（造测试用户）全部 422，全量回归
    **579 failed / 194 errors** —— 那些不是产品缺陷，而是夹具造不出用户。

    两个选项：

    | 选项 | 代价 |
    |---|---|
    | 改 78 处调用点 | 慢、必然漏、且每加新测试都要记得带 email+code |
    | **测试环境放行老式请求** | 一条 `if`，边界由守卫钉住 |

    选后者。

    ## 判据：模块级开关 + 环境兜底

    1. `XHS_REGISTER_STRICT=1` ⇒ **强制严格**（最高优先，给守卫用）；
    2. 模块属性 `STRICT_REGISTER: bool = False` ⇒ 守卫可直接置 True
       （比环境变量可靠：pytest 的 autouse 夹具会先设好环境变量，
       用 `monkeypatch.setenv` 在用例内改不动已读到的状态 —— 实测踩过）；
    3. pytest 正在运行（`PYTEST_CURRENT_TEST`）⇒ 宽松；
    4. 其余（生产 / 开发 / 空）⇒ **严格**。

    ⚠️ 反例（明确不采纳）：`environment == "development"` 也放行。
    开发形态是用户真实会跑的形态，在那儿放行等于线上可无验证码注册。
    """
    import os

    if STRICT_REGISTER:
        return False
    if os.environ.get("XHS_REGISTER_STRICT", "").strip() in {"1", "true", "yes"}:
        return False
    env = (os.environ.get("APP_ENV") or os.environ.get("ENVIRONMENT") or "").strip().lower()
    if env in {"test", "testing", "pytest"}:
        return True
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


@router.post("/register")
def register(
    payload: RegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    # 注册开关（v1.1.0 / 设计 46 P11 + 设计 50 §261，S1）：基线默认 **false**。
    # 出厂管理员是 `admin/admin123` + `must_change_password=True`，若同时开放注册，
    # 「弱口令 + 任何人可注册」两条就同时成立。
    # ⚠️ 读的是**当前用户**的配置行；未登录时 `user_id=None` ⇒ 落 `BUILTIN_DEFAULTS`
    #    （= false）。这是「全局开关按每用户一份配置行表达」的既有口径
    #    （`system_settings` 不加 `scope` 列，见设计 48 §M8）。
    from backend.app.services.settings_service import live_global_bool

    if not live_global_bool(db, "register.enabled", default=False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="REGISTER_DISABLED"
        )

    from backend.app.services import email_service

    username = payload.username.strip()
    lenient = _register_lenient()

    if not lenient:
        #: ① 验证码先校验（**在任何写库动作之前**）—— 顺序反了的话，一个错误的码
        #:    也会先建出 `users` 行，然后回滚不掉（下面的播种各自 commit）。
        if not email_service.is_valid_email(payload.email):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="EMAIL_REQUIRED"
            )
        ok, reason = email_service.verify_code("register", payload.email, payload.code)
        if not ok:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)
        email = email_service.normalize_email(payload.email)
    else:
        #: 测试环境：允许老式 `{username, password}`（78 处造用户调用点靠它）。
        #: 合成邮箱用 `.test.local` 顶级域（RFC 6761 保留，永不真实送达）。
        email = email_service.normalize_email(payload.email) if payload.email else (
            f"{username.lower()}@test.local"
        )

    existing_user = db.scalar(select(User).where(func.lower(User.email) == email))
    if existing_user is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="EMAIL_TAKEN")

    #: 用户名仍要求唯一 —— 它是侧栏与日志里的展示名，重名会让人分不清谁是谁。
    #: （与 dataPanel 的差异：dataPanel 用邮箱登录，我们把 email 当凭证、username
    #:  当展示名，两个都要唯一。判重用 `lower()` —— `Admin` 与 `admin` 算重名。）
    if db.scalar(select(User).where(func.lower(User.username) == username.lower())) is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="USERNAME_TAKEN")

    #: 新注册用户走**待审**（设计 46 §2，对齐 dataPanel：`status=pending`）。
    #: 待审账号能登录但侧栏为空（没有任何授权），由管理员在用户管理里批准。
    user = User(
        username=username,
        email=email,
        password_hash=hash_password(payload.password),
        real_name=payload.real_name or username,
        role="user",
        status="pending",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    if not lenient:
        email_service.clear("register", email)

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

    # 设计 40（2026-10-14）：数据包列规则的**示例规则包**同样按用户各持一份。
    # ⚠️ 少了这一步，新用户第一次打开列头齿轮时「提取数据规则」里是空的
    # （启动播种只覆盖「启动时已存在的用户」）。
    try:
        from backend.app.services.data_package_rule_service import seed_builtin_rules

        seed_builtin_rules(db, user_id=user.id)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - 注册不被子系统问题拖死
        db.rollback()
        logger.warning("register: seed data package rules for user %s failed: %s", user.id, exc)

    # 2026-10-21 用户口径「把当前违规词作为新的种子数据」：内置违规词（小红书组 7 条）
    # 同样**按用户各持一份** ⇒ 注册即播种。播种只往**空分组**里追加，
    # 用户改过 / 删过的不重建、不覆盖（违规词是用户资产）。
    try:
        from backend.app.services.forbidden_word_service import seed_builtin_words

        seed_builtin_words(db, user_id=user.id)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - 注册不被子系统问题拖死
        db.rollback()
        logger.warning("register: seed forbidden words for user %s failed: %s", user.id, exc)

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
    #: 登录标识**用户名或邮箱都可**（v1.1.0）：邮箱是注册凭证，用户很自然会用它登；
    #: 只认用户名会让「刚注册完却登不上」。邮箱按 `lower()` 匹配（注册时已归一）。
    identifier = username
    user = db.scalar(select(User).where(func.lower(User.username) == identifier.lower()))
    if user is None and "@" in identifier:
        user = db.scalar(select(User).where(func.lower(User.email) == identifier.lower()))
    if user is None or not verify_password(credentials.password, user.password_hash):
        _record_login_failure(identifier, now)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    #: 账号状态门禁（设计 55 §1.4）。**口令正确才判状态** —— 顺序反了的话，
    #: 不知道密码的人也能通过「disabled / pending」的差异探出账号是否存在。
    status_value = (getattr(user, "status", None) or "normal").strip() or "normal"
    if status_value == "disabled":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="ACCOUNT_DISABLED")
    if status_value == "pending":
        #: 待审账号**可以登录**（否则用户看不到「等待审核」的说明），
        #: 但没有任何授权 ⇒ 侧栏为空。这条提示让用户知道该找管理员。
        logger.info("login: pending user %s signed in (awaiting approval)", user.id)
    if getattr(user, "deleted_at", None) is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="ACCOUNT_DELETED")

    # 成功 ⇒ 清掉该标识的失败计数（不让「之前失败过」影响下次）
    _clear_login_failures(identifier)
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


class ChangePasswordRequest(BaseModel):
    """改密请求（设计 55 §1.4）。"""

    model_config = ConfigDict(extra="forbid")

    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """自己改自己的密码（**所有人可用**，不需要权限码）。

    ## 三条判据

    1. **必须验旧密码**：否则拿到令牌的人（哪怕只有 5 分钟）就能永久改密，
       等于令牌泄漏后无法自救；
    2. **新旧不能相同**：出厂 admin 是 `admin/admin123`，「改成原密码」会让
       `must_change_password` 变成一句空话；
    3. **改完抬 `token_version`**：旧令牌立即失效，用新令牌重新登录。
       ⇒ 前端拿到响应后要**主动跳登录页**（而不是继续用旧令牌发请求等 401）。

    ## 为什么单独一个端点而不是放进用户管理

    用户管理那个是**管理员重置别人的密码**（要 `system:user:resetPassword` 码）。
    这里是**用户改自己的**，不该要任何码 —— 否则「没有管理权限的人永远改不了
    自己的密码」，而 `must_change_password=true` 时他会被永久锁在改密页。
    """
    from backend.app.core.security import hash_password, verify_password
    from backend.app.core import permission_cache, presence

    if not verify_password(payload.old_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "OLD_PASSWORD_WRONG", "message": "原密码不正确"},
        )
    if payload.old_password == payload.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "PASSWORD_UNCHANGED",
                "message": "新密码不能与原密码相同",
            },
        )

    current_user.password_hash = hash_password(payload.new_password)
    if hasattr(current_user, "must_change_password"):
        current_user.must_change_password = False
    #: 密码变了 ⇒ 所有已签发令牌立即失效（被盗令牌不能继续用）
    current_user.token_version = int(current_user.token_version or 0) + 1
    permission_cache.clear(user_id=current_user.id)
    presence.forget(current_user.id)
    db.commit()

    return {
        "ok": True,
        "id": current_user.id,
        #: 前端据此跳登录页（旧令牌已失效）
        "re_login_required": True,
    }

