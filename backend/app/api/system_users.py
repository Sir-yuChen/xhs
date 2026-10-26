"""系统用户管理端点（设计 55 §2 / 设计 49 模块一）。

| 方法 | 路径 | 权限码 |
|---|---|---|
| `GET`    | `/api/system/users` | `system:user:view` |
| `POST`   | `/api/system/users` | `system:user:create` |
| `GET`    | `/api/system/users/{user_id}` | `system:user:view` |
| `PUT`    | `/api/system/users/{user_id}` | `system:user:edit` |
| `DELETE` | `/api/system/users/{user_id}` | `system:user:delete`（**软删**） |
| `POST`   | `/api/system/users/{user_id}/reset-password` | `system:user:resetPwd` |
| `POST`   | `/api/system/users/{user_id}/force-logout` | `system:user:edit` |
| `POST`   | `/api/system/users/{user_id}/approve` | `system:user:approve` |
| `GET`    | `/api/system/users/{user_id}/online` | `system:user:view` |

## 四条贯穿全文件的判据

### ① 删除是**软删**（`users.deleted_at`）

物理删会让 49 张按 `user_id` 关联的业务表变成孤儿（或级联删掉用户全部数据 ——
对一个「只想撤销某个人访问权」的管理动作来说，那是灾难性的副作用）。
软删后 `get_current_user` 直接拒旧令牌，效果等价于「立即踢出」，
但数据都还在，恢复只需把 `deleted_at` 置空。

### ② 手机号列表**必脱敏**，详情才给全

列表接口一次返回 20 个用户，脱敏是防「截图外发」这类最小必要原则。
详情接口是管理员主动点开一个人的档案，给全字段。

### ③ 停用 / 删除自己 —— 拒绝

否则管理员点一下就把自己锁在门外（admin 的权限虽由代码派生，
但 `status='disabled'` 在 `get_current_user` 里就拦住了，与权限码无关）。

### ④ 改 `role` / 改 `status` / 重置密码 ⇒ `token_version += 1`

让旧令牌立刻失效。三者的共同点是「这个人的权限边界变了」，
而 JWT 是无状态的 —— 不改版本号，用户手上的旧令牌会继续按旧权限干活。

⚠️ 与「改 `real_name` / `email`」区别对待：那些不影响权限，不该把人踢下线。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.core.perms import ROLE_ADMIN, normalize_role

#: 种子创建的超级管理员账号名（唯一）。
#: 2026-10-26 用户口径：「超级管理员只能有一个，系统种子数据 admin 账号」。
#:
#: ⚠️ **从种子常量派生**，不在这里写字面量 —— 两处各写一份，改种子时必然漂移
#: （改一处忘另一处 ⇒ 「建号被拒，但界面上说该用户名合法」这种自相矛盾）。
from backend.app.services.menu_seed_service import ADMIN_SEED  # noqa: E402

SEED_SUPER_ADMIN_USERNAME: str = str(ADMIN_SEED["username"])
from backend.app.core.perm_deps import require_perm
from backend.app.core.security import hash_password
from backend.app.core.time import shanghai_now
from backend.app.models.role import Role
from backend.app.models.user import User
from backend.app.core import permission_cache, presence, rbac_service
from backend.app.schemas.system import (
    SystemUserListOut,
    SystemUserOnlineOut,
    SystemUserOut,
)

router = APIRouter(prefix="/system/users", tags=["system"])

#: 列表接口的手机号脱敏：保留前 3 位 + 后 4 位
def _mask_phone(phone: str | None) -> str:
    digits = (phone or "").strip()
    if len(digits) < 7:
        return "" if not digits else "*" * len(digits)
    return f"{digits[:3]}{'*' * (len(digits) - 7)}{digits[-4:]}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _serialize(
    user: User,
    *,
    role_names: dict[str, str],
    online_ids: set[int] | None,
    full: bool,
) -> dict:
    """序列化用户。`full=True` 时给完整手机号（详情接口），否则脱敏。"""
    role = normalize_role(user.role)
    return {
        "id": user.id,
        "username": user.username,
        "real_name": getattr(user, "real_name", "") or "",
        "email": getattr(user, "email", "") or "",
        # 脱敏判据：列表一律脱敏，详情给全（设计 55 §2.1 派生字段表）
        "phone": (getattr(user, "phone", "") or "")
        if full
        else _mask_phone(getattr(user, "phone", "")),
        "role": role,
        # 角色名 join 不到 ⇒ 回落 code 原值（不造假，硬约束 C5）
        "role_name": role_names.get(role, role),
        "status": getattr(user, "status", "normal") or "normal",
        # online 是**近似值**：判据 = 「5 分钟内有没有请求」（见 presence）
        "online": (user.id in online_ids) if online_ids is not None else presence.is_online(user.id),
        "must_change_password": bool(getattr(user, "must_change_password", False)),
        "last_login_at": _iso(getattr(user, "last_login_at", None)),
        "last_login_ip": getattr(user, "last_login_ip", "") or "",
        "created_at": _iso(getattr(user, "created_at", None)),
        "deleted_at": _iso(getattr(user, "deleted_at", None)),
    }


def _role_name_map(db: Session) -> dict[str, str]:
    return {r.code: r.name for r in db.scalars(select(Role)).all()}


def _get_or_404(db: Session, user_id: int, *, include_deleted: bool = False) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, detail={"code": "USER_NOT_FOUND", "message": "用户不存在"})
    if not include_deleted and getattr(user, "deleted_at", None) is not None:
        raise HTTPException(404, detail={"code": "USER_NOT_FOUND", "message": "用户不存在"})
    return user


def _assert_not_self(actor: User, target: User, action: str) -> None:
    """不许对自己做「停用 / 删除 / 强制下线」—— 会把管理员自己锁在门外。"""
    if actor.id == target.id:
        raise HTTPException(
            400,
            detail={
                "code": "USER_SELF_OPERATION_FORBIDDEN",
                "message": f"不能{action}自己的账号",
            },
        )


def _assert_role_exists(db: Session, role_code: str) -> str:
    role = rbac_service.role_by_code(db, role_code)
    if role is None or role.status != "normal":
        raise HTTPException(
            400,
            detail={"code": "ROLE_NOT_FOUND", "message": f"角色 {role_code} 不存在或已停用"},
        )
    return role.code


def _assert_single_super_admin(db: Session, role_code: str, *, target: User | None = None) -> None:
    """**超级管理员只允许一个**，且必须是种子创建的 `admin` 账号。

    2026-10-26 用户口径：「超级管理员只能有一个，系统种子数据 admin 账号」。

    ## 为什么要在建/改两条路都拦

    `_delete_user_core` 早就有「不能删唯一管理员」，但那只防**减**不防**增**：
    建一个 `role=admin` 的新用户、或把别人改成 admin，都会出现两个超级管理员。
    多出来的那个不受任何约束（删不掉、也说不清谁是正式的那一个）。

    ## 判据

    * 想把人设成 admin ⇒ 必须**就是**种子账号（`username == "admin"`）；
    * 种子账号本身**不许被改成别的角色**（否则系统没有超级管理员了）。

    角色归属是**代码事实**（见 `roles.py`：admin 权限由 `ALL_PERMS` 派生），
    不是可配置项，所以这里直接按用户名钉，不走配置。
    """
    if role_code != ROLE_ADMIN:
        #: 不涉及 admin 角色 ⇒ 顺带防止「把种子账号降级」
        if target is not None and normalize_role(target.role) == ROLE_ADMIN:
            raise HTTPException(
                400,
                detail={
                    "code": "SUPER_ADMIN_NOT_DEMOTABLE",
                    "message": f"超级管理员账号「{target.username}」不能改成其他角色",
                },
            )
        return

    if target is None:
        #: 新建 ⇒ 只有种子账号名可以是 admin
        raise HTTPException(
            400,
            detail={
                "code": "SUPER_ADMIN_SINGLETON",
                "message": "系统只允许一个超级管理员（种子账号 admin），不能新建其他超级管理员",
            },
        )

    if target.username != SEED_SUPER_ADMIN_USERNAME:
        raise HTTPException(
            400,
            detail={
                "code": "SUPER_ADMIN_SINGLETON",
                "message": f"超级管理员只能是种子账号「{SEED_SUPER_ADMIN_USERNAME}」，"
                f"不能把「{target.username}」提升为超级管理员",
            },
        )


def _bump_token_version(db: Session, user: User) -> None:
    """让该用户的旧令牌立即失效（改角色 / 改状态 / 重置密码时调用）。"""
    user.token_version = int(user.token_version or 0) + 1
    permission_cache.clear(user_id=user.id)
    presence.forget(user.id)


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #
USERNAME_PATTERN = r"^[A-Za-z0-9_]+$"


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=80, pattern=USERNAME_PATTERN)
    password: str = Field(min_length=6, max_length=128)
    real_name: str = Field(default="", max_length=64)
    email: str = Field(default="", max_length=255)
    phone: str = Field(default="", max_length=32)
    role: str = Field(default="user", max_length=32)
    status: str = Field(default="normal", pattern="^(normal|disabled)$")


class UserUpdate(BaseModel):
    """部分更新。`None` 表示「不改这个字段」（与「改成空串」区分）。"""

    model_config = ConfigDict(extra="forbid")

    real_name: Optional[str] = Field(default=None, max_length=64)
    email: Optional[str] = Field(default=None, max_length=255)
    phone: Optional[str] = Field(default=None, max_length=32)
    role: Optional[str] = Field(default=None, max_length=32)
    status: Optional[str] = Field(default=None, pattern="^(normal|pending|disabled)$")


class BatchUserIdsRequest(BaseModel):
    """批量操作的目标 id 列表。"""

    user_ids: list[int] = Field(default_factory=list, max_length=200)


class BatchResetPasswordRequest(BaseModel):
    """批量重置密码（同一新密码）。"""

    user_ids: list[int] = Field(default_factory=list, max_length=200)
    password: str = Field(min_length=6, max_length=128)
    must_change: bool = True


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=6, max_length=128)
    #: 是否要求下次登录改密（管理员重置后默认要求，与 admin 种子同口径）
    must_change: bool = True


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #
@router.get("", response_model=SystemUserListOut)
def list_users(
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:user:view")),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
    username: Annotated[Optional[str], Query(max_length=80)] = None,
    email: Annotated[Optional[str], Query(max_length=255)] = None,
    status_filter: Annotated[Optional[str], Query(alias="status")] = None,
    role: Annotated[Optional[str], Query(max_length=32)] = None,
    include_deleted: bool = False,
) -> dict:
    """用户列表（分页 + 筛选）。手机号**脱敏**。"""
    stmt = select(User)
    if not include_deleted:
        stmt = stmt.where(User.deleted_at.is_(None))
    if username:
        stmt = stmt.where(func.lower(User.username).like(f"%{username.strip().lower()}%"))
    if email:
        stmt = stmt.where(func.lower(func.coalesce(User.email, "")).like(f"%{email.strip().lower()}%"))
    if status_filter:
        stmt = stmt.where(User.status == status_filter)
    if role:
        stmt = stmt.where(User.role == role)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(User.id).offset((page - 1) * page_size).limit(page_size)
    ).all()

    role_names = _role_name_map(db)
    online_ids = presence.online_user_ids()
    return {
        "items": [
            _serialize(u, role_names=role_names, online_ids=online_ids, full=False)
            for u in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:user:create")),
) -> dict:
    """新建用户。用户名唯一（含已软删的行 —— 用户名被历史上占用过就不可复用）。"""
    exists = db.execute(
        select(User.id).where(func.lower(User.username) == payload.username.lower())
    ).first()
    if exists is not None:
        raise HTTPException(
            409, detail={"code": "USERNAME_TAKEN", "message": "用户名已存在"}
        )
    role_code = _assert_role_exists(db, payload.role)
    #: 建号即限制：不允许新建第二个超级管理员（唯一的是种子 admin）
    _assert_single_super_admin(db, role_code, target=None)

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=role_code,
        created_at=shanghai_now(),
    )
    # 这些列由迁移 a1b2c3d4e5f9 加出；用 setattr 而不是构造参数，
    # 让「最小夹具」缺列时也能跑（老测试库）
    for field, value in (
        ("real_name", payload.real_name),
        ("email", payload.email),
        ("phone", payload.phone),
        ("status", payload.status),
        ("must_change_password", False),
        ("token_version", 0),
    ):
        if hasattr(user, field):
            setattr(user, field, value)

    db.add(user)
    db.commit()
    db.refresh(user)
    return _serialize(
        user, role_names=_role_name_map(db), online_ids=None, full=True
    )


# --------------------------------------------------------------------------- #
# 批量操作（2026-10-25 用户口径「新增用户管理批量相关操作」）
#
# 判据：**逐条调单条实现**，不另写规则（见 `user_batch_service` docstring）。
# 回执四态 ok / skipped / failed / not_found，逐条说清，不合并成「已处理 N 个」。
# 权限码各按钮独立（红线：一按钮一码）。
# --------------------------------------------------------------------------- #
@router.post("/batch/delete")
def batch_delete_users(
    payload: BatchUserIdsRequest,
    *,
    db: Session = Depends(get_db),
    actor: User = Depends(require_perm("system:user:batchDelete")),
) -> dict:
    """批量软删。单条失败不中断整批，逐条回执。"""
    from backend.app.services.user_batch_service import run_batch  # noqa: PLC0415

    return run_batch(
        db, actor, payload.user_ids, action=_delete_user_core, action_label="删除"
    )


@router.post("/batch/reset-password")
def batch_reset_password(
    payload: BatchResetPasswordRequest,
    *,
    db: Session = Depends(get_db),
    actor: User = Depends(require_perm("system:user:batchResetPassword")),
) -> dict:
    """批量重置为**同一个**新密码（逐一 hash，不共享同一哈希）。"""
    from backend.app.services.user_batch_service import run_batch  # noqa: PLC0415

    def _reset(s: Session, act: User, uid: int) -> None:
        user = _get_or_404(s, uid)
        user.password_hash = hash_password(payload.password)
        if hasattr(user, "must_change_password"):
            user.must_change_password = payload.must_change
        _bump_token_version(s, user)
        s.commit()

    return run_batch(
        db, actor, payload.user_ids, action=_reset, action_label="重置密码"
    )


@router.post("/batch/force-logout")
def batch_force_logout(
    payload: BatchUserIdsRequest,
    *,
    db: Session = Depends(get_db),
    actor: User = Depends(require_perm("system:user:batchForceLogout")),
) -> dict:
    """批量强制下线（只 bump token_version，不动密码）。"""
    from backend.app.services.user_batch_service import run_batch  # noqa: PLC0415

    def _kick(s: Session, act: User, uid: int) -> None:
        user = _get_or_404(s, uid)
        _bump_token_version(s, user)
        s.commit()

    return run_batch(
        db, actor, payload.user_ids, action=_kick, action_label="强制下线"
    )


@router.post("/batch/approve")
def batch_approve_users(
    payload: BatchUserIdsRequest,
    *,
    db: Session = Depends(get_db),
    actor: User = Depends(require_perm("system:user:batchApprove")),
) -> dict:
    """批量通过审核（待审 → 正常）。"""
    from backend.app.services.user_batch_service import run_batch  # noqa: PLC0415

    def _approve(s: Session, act: User, uid: int) -> None:
        user = _get_or_404(s, uid)
        if getattr(user, "status", "") != "pending":
            raise HTTPException(
                400, detail={"code": "NOT_PENDING", "message": "该用户不是待审核状态"}
            )
        user.status = "normal"
        s.commit()

    return run_batch(
        db, actor, payload.user_ids, action=_approve, action_label="审核"
    )


@router.get("/{user_id}", response_model=SystemUserOut)
def get_user(
    user_id: int,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:user:view")),
    include_deleted: bool = False,
) -> dict:
    """用户详情（手机号给全）。"""
    user = _get_or_404(db, user_id, include_deleted=include_deleted)
    return _serialize(
        user, role_names=_role_name_map(db), online_ids=None, full=True
    )


@router.put("/{user_id}")
def update_user(
    user_id: int,
    payload: UserUpdate,
    *,
    db: Session = Depends(get_db),
    actor: User = Depends(require_perm("system:user:edit")),
) -> dict:
    """部分更新。改 role / status ⇒ 抬 `token_version`（旧令牌立即失效）。"""
    user = _get_or_404(db, user_id)
    bump = False

    if payload.role is not None and payload.role != user.role:
        new_role = _assert_role_exists(db, payload.role)
        #: 改角色即限制：只有种子 admin 能是超级管理员，且它不能被降级
        _assert_single_super_admin(db, new_role, target=user)
        user.role = new_role
        bump = True

    if payload.status is not None and payload.status != user.status:
        if payload.status != "normal":
            _assert_not_self(actor, user, "停用")
        user.status = payload.status
        bump = True

    # 这几项不影响权限 ⇒ 不踢人
    for field in ("real_name", "email", "phone"):
        value = getattr(payload, field)
        if value is not None and hasattr(user, field):
            setattr(user, field, value)

    if bump:
        _bump_token_version(db, user)

    db.commit()
    db.refresh(user)
    return _serialize(
        user, role_names=_role_name_map(db), online_ids=None, full=True
    )


def _delete_user_core(db: Session, actor: User, user_id: int) -> User:
    """删除单个用户的**全部规则**（单条端点与批量端点共用同一实现）。

    抽出来的理由：批量若另写一遍「不能删自己 / 不能删最后一个管理员 / 软删释放唯一键」，
    迟早与单条漂移 —— 而「批量删了唯一管理员」是不可恢复的事故。
    """
    user = _get_or_404(db, user_id)
    _assert_not_self(actor, user, "删除")

    if normalize_role(user.role) == ROLE_ADMIN:
        remaining = db.scalar(
            select(func.count()).select_from(User).where(
                User.role == "admin", User.deleted_at.is_(None), User.id != user.id
            )
        )
        if not remaining:
            raise HTTPException(
                400,
                detail={"code": "LAST_ADMIN_PROTECTED", "message": "不能删除唯一的超级管理员"},
            )

    user.deleted_at = shanghai_now()
    _bump_token_version(db, user)
    stamp = f"deleted_{user.id}_"
    if user.email:
        user.email = (stamp + user.email)[:254]
    user.username = (stamp + user.username)[:80]
    db.commit()
    return user


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    *,
    db: Session = Depends(get_db),
    actor: User = Depends(require_perm("system:user:delete")),
) -> dict:
    """**软删**（`deleted_at`）。业务数据不动，旧令牌立即失效。

    规则全部在 `_delete_user_core`（批量端点共用同一份）。
    """
    user = _delete_user_core(db, actor, user_id)
    return {"ok": True, "id": user.id, "deleted": True}



    user = _get_or_404(db, user_id)
    _assert_not_self(actor, user, "强制下线")
    _bump_token_version(db, user)
    db.commit()
    return {"ok": True, "id": user.id, "token_version": user.token_version}


@router.post("/{user_id}/reset-password")
def reset_password(
    user_id: int,
    payload: ResetPasswordRequest,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:user:resetPassword")),
) -> dict:
    """管理员重置某用户密码。默认要求下次登录改密。"""
    user = _get_or_404(db, user_id)
    user.password_hash = hash_password(payload.password)
    if hasattr(user, "must_change_password"):
        user.must_change_password = payload.must_change
    #: 密码变了 ⇒ 旧令牌必须失效（否则被盗令牌仍有 30 分钟有效期）
    _bump_token_version(db, user)
    db.commit()
    return {"ok": True, "id": user.id, "must_change_password": payload.must_change}


@router.post("/{user_id}/force-logout")
def force_logout(
    user_id: int,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:user:forceLogout")),
) -> dict:
    """强制下线：bump `token_version`，该用户所有令牌立即失效。

    ⚠️ 必须在 `/{user_id}/approve` 之前注册？不需要 —— FastAPI 按**路径模板**
    匹配，`/batch/*` 与 `/{user_id}/*` 不会互相遮蔽（段数不同）。
    但**同段数的字面量要排在变量前**才能命中（`/batch/delete` vs `/{user_id}` 是
    两段 vs 一段，无冲突）。
    """
    user = _get_or_404(db, user_id)
    _bump_token_version(db, user)
    db.commit()
    return {"ok": True, "id": user.id, "force_logged_out": True}


@router.post("/{user_id}/approve")
def approve_user(
    user_id: int,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:user:approve")),
) -> dict:
    """审批通过注册申请：`pending` → `normal`。

    只对 `pending` 生效（对 `normal` 调它 = 无操作成功，幂等；
    对 `disabled` 调它要报错 —— 那是在绕过「停用」这个明确的管理动作）。
    """
    user = _get_or_404(db, user_id)
    current = getattr(user, "status", "normal") or "normal"
    if current == "disabled":
        raise HTTPException(
            400,
            detail={
                "code": "USER_DISABLED_CANNOT_APPROVE",
                "message": "已停用的账号不能直接审批，请先启用",
            },
        )
    if current == "pending":
        user.status = "normal"
        _bump_token_version(db, user)
        db.commit()
        db.refresh(user)
    return _serialize(
        user, role_names=_role_name_map(db), online_ids=None, full=True
    )


@router.get("/{user_id}/online", response_model=SystemUserOnlineOut)
def get_user_online(
    user_id: int,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:user:view")),
) -> dict:
    """单用户在线状态（**近似值**：5 分钟内是否请求过）。"""
    _get_or_404(db, user_id)
    return {
        "user_id": user_id,
        "online": presence.is_online(user_id),
        "window_seconds": presence.ONLINE_WINDOW_SECONDS,
        "note": "判据为「窗口内是否有请求」，不是浏览器是否开着",
    }
