"""运行时配置中心 API（设计 10 §7.5，2026-09-27）。

五个端点 = 一套完整 CRUD：列表 / 新增 / 单条 / 改 / 删。

**归属与越权**：所有读写都按 `(current_user.id, key)` 收口 —— 别的用户的键一律 **404**
（不泄露「这个键存在」），与全项目「资源双归属校验」口径一致（设计 10 §7.1 D3）。

**删 = 回落**：DELETE 不是「把配置弄没」，而是「回到默认」（settings / 内置兜底）⇒
响应里回 `effective_value`，让页面**立刻**显示恢复成了什么值，不用再发一次 GET。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models import SystemSetting, User
from backend.app.models.system_setting import VALUE_TYPES
from backend.app.services import business_log_service
from backend.app.schemas.settings import (
    EffectiveSettingsResponse,
    SettingDetailResponse,
    SettingListResponse,
)
from backend.app.services import settings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingCreateRequest(BaseModel):
    """新增一行配置。

    ⚠️ `extra="forbid"`：未知字段直接 422，不静默丢弃 —— 旧进程（`extra="ignore"`）
    会把新参数悄悄吃掉，表现为「前端传了、后端没收到、页面还不报错」。
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=settings_service.KEY_MAX_LENGTH)
    value: Any = None
    value_type: Optional[str] = Field(default=None, description=f"留空按值推断；合法值：{', '.join(VALUE_TYPES)}")
    category: str = Field(default="feature", max_length=32)
    label: str = Field(default="", max_length=128)
    description: str = Field(default="")
    requires_restart: bool = False
    is_dangerous: bool = False
    #: 归属级（设计 64 §1.5）：user=本人覆盖（缺省）/ system=全局（admin）/
    #: workspace=工作区（需带 workspace）。写权限在端点内收口。
    scope: str = Field(default="user", pattern="^(user|system|workspace)$")
    workspace: Optional[str] = Field(default=None, max_length=32)


class SettingUpdateRequest(BaseModel):
    """改一行配置。**只改传进来的字段**（`None` = 不动）。"""

    model_config = ConfigDict(extra="forbid")

    value: Any = None
    value_type: Optional[str] = None
    category: Optional[str] = Field(default=None, max_length=32)
    label: Optional[str] = Field(default=None, max_length=128)
    description: Optional[str] = None
    requires_restart: Optional[bool] = None
    is_dangerous: Optional[bool] = None
    scope: str = Field(default="user", pattern="^(user|system|workspace)$")
    workspace: Optional[str] = Field(default=None, max_length=32)


def _get_owned_setting(db: Session, current_user: User, key: str) -> SystemSetting:
    row = settings_service.find_setting(db, current_user.id, key)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="配置项不存在")
    return row


def _resolve_scope(
    db: Session,
    current_user: User,
    scope: str,
    workspace: Optional[str],
) -> tuple[str, Optional[str]]:
    """解析并**权限收口**归属级（设计 64 §1.5）。

    * `user`（缺省）：本人，任何登录用户；
    * `system`：需要 `system:setting:edit`（系统级是全站基线）；
    * `workspace`：需要 `<workspace>:settings:edit`（工作区参数归该工作区管）。

    ## 2026-10-25 用户口径：「系统管理模块也是依靠菜单权限授权，并不是仅管理员」

    原实现一律硬判 `role == admin` —— 与「靠菜单权限授权」矛盾：
    管理员给某角色勾了「配置管理」菜单与 `system:setting:view`，那个人进得去页面，
    但**读写全 403**，页面成了空壳。

    ⇒ 判据改为权限码。管理员把 `system:setting:edit` 勾给谁，谁就能改系统级配置
    —— 这正是「授权」的含义；不想给就不勾，而不是代码里写死身份。
    """
    scope = str(scope or "user").strip()
    if scope == "user":
        return "user", None

    from backend.app.core.perms import has_perm  # noqa: PLC0415

    clean_ws = str(workspace or "").strip()
    if scope == "workspace":
        if not clean_ws:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="工作区级配置必须带 workspace 参数",
            )
        #: 工作区级配置的写权限属于**该工作区**（`xhs:settings:edit`）。
        #: 兼容旧口径：系统级配置编辑权限也能管工作区级（跨工作区运维）。
        if not (
            has_perm(current_user, f"{clean_ws}:settings:edit", db=db)
            or has_perm(current_user, "system:setting:edit", db=db)
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "SETTING_SCOPE_FORBIDDEN",
                    "message": "缺少该工作区的配置编辑权限",
                },
            )
        return "workspace", clean_ws

    if not has_perm(current_user, "system:setting:edit", db=db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "SETTING_SCOPE_FORBIDDEN",
                "message": "缺少「配置管理」编辑权限",
            },
        )
    return "system", None


def _wake_runtime_consumers(key: str) -> None:
    """配置改了 ⇒ 让**读点方**立刻对齐（设计 10 §7.4 的「写后立即生效」后半段）。

    缓存已经在 `settings_service` 的写路径里失效了；这里补的是**结构型**读点：
    同步 worker 池的**线程条数**是按配置起的，配置从 1 改成 3 时，
    光失效缓存不会凭空多出两条线程 —— 得显式让它对齐一次。

    ⚠️ 失败只记 warning：配置**已经落库**了（用户的操作成功），唤醒失败只是「晚一点
    由看门狗对齐」，不该把一个已成功的写操作报成失败。
    """
    if key != "xhs_sync_account_parallelism":
        return
    try:
        from backend.app.services.note_sync_service import ensure_sync_worker

        ensure_sync_worker()
    except Exception:  # noqa: BLE001 - 唤醒失败不影响配置已生效这件事
        logger.warning("settings: 配置 %s 已保存，但唤醒同步 worker 池失败", key, exc_info=True)


@router.get("", response_model=SettingListResponse)
def list_settings(
    category: Optional[str] = Query(default=None, description="按分组筛选；留空 = 全部"),
    scope: str = Query(default="user", description="user=本人 / system=全局（admin）/ workspace=工作区（admin，需 workspace）"),
    workspace: Optional[str] = Query(default=None, max_length=32),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """配置列表（设计 64 §1.5：scope 决定读哪一级）。"""
    resolved_scope, clean_ws = _resolve_scope(db, current_user, scope, workspace)
    rows = settings_service.list_scoped_settings(
        db,
        scope=resolved_scope,
        user_id=current_user.id,
        workspace=clean_ws,
        category=category,
    )
    return {
        "scope": resolved_scope,
        "workspace": clean_ws,
        "items": [settings_service.serialize_setting(row, db=db) for row in rows],
        "total": len(rows),
        "live_keys": settings_service.live_keys(),
    }


@router.get("/effective", response_model=EffectiveSettingsResponse)
def effective_settings(
    workspace: Optional[str] = Query(default=None, max_length=32),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """三级级联合并视图（设计 64 §1.3）：工作区级 > 用户级 > 系统级 > 内置默认。

    登录即可（每页面的展示依赖）；`scope_hit` 告诉前端"这一格生效的是哪级"。
    ⚠️ 路由必须注册在 `/{key}` 之前，否则 `effective` 会被当成 key。
    """
    merged = settings_service.effective_settings_map(db, current_user.id, workspace)
    return {
        "workspace": workspace,
        "items": [dict(key=key, **row) for key, row in sorted(merged.items())],
        "total": len(merged),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_setting(
    payload: SettingCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """新增配置项。键已存在 ⇒ 409（不静默改成「更新」—— 那会让用户以为新建成功了）。"""
    resolved_scope, clean_ws = _resolve_scope(db, current_user, payload.scope, payload.workspace)
    try:
        row = settings_service.create_setting(
            db,
            user_id=current_user.id,
            key=payload.key,
            value=payload.value,
            value_type=payload.value_type,
            category=payload.category,
            label=payload.label,
            description=payload.description,
            requires_restart=payload.requires_restart,
            is_dangerous=payload.is_dangerous,
            scope=resolved_scope,
            workspace=clean_ws,
        )
    except ValueError as exc:
        message = str(exc)
        if "已存在" in message:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message) from exc
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message
        ) from exc
    db.commit()
    db.refresh(row)
    _wake_runtime_consumers(row.key)
    business_log_service.record(
        "settings.create",
        action="create",
        actor_id=current_user.id,
        actor_name=current_user.username,
        target_type="setting",
        target_id=row.key,
        summary=f"新增{resolved_scope}级配置 {row.key} = {row.value}",
        detail={"scope": resolved_scope, "workspace": clean_ws, "value_type": row.value_type},
        db=db,
    )
    return settings_service.serialize_setting(row, db=db)


@router.get("/{key}", response_model=SettingDetailResponse)
def get_setting(
    key: str,
    scope: str = Query(default="user"),
    workspace: Optional[str] = Query(default=None, max_length=32),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """单条配置（设计 64：scope 决定读哪级；越级/别人的键 ⇒ 404）。"""
    resolved_scope, clean_ws = _resolve_scope(db, current_user, scope, workspace)
    row = settings_service.find_scoped(
        db, scope=resolved_scope, key=key, user_id=current_user.id, workspace=clean_ws
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="配置项不存在")
    return settings_service.serialize_setting(row, db=db)


@router.put("/{key}")
def update_setting(
    key: str,
    payload: SettingUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """改配置值（改完**立即生效**，无需重启）。

    `value` 用「是否显式出现在请求体里」判定要不要改 —— 只改 `label` 时不许把值冲成
    `None`（`None` 是合法配置值，不能拿它当「没传」）。
    """
    resolved_scope, clean_ws = _resolve_scope(db, current_user, payload.scope, payload.workspace)
    row = settings_service.find_scoped(
        db, scope=resolved_scope, key=key, user_id=current_user.id, workspace=clean_ws
    )
    if row is None:
        #: ⚠️ **内置键在该级还没有行**时不能 404。
        #:
        #: 列表接口会给「归属本级但还没行」的内置键**补一条虚拟默认行**下发
        #:（`list_scoped_settings` 的 `_default_row`），用户看得到、点得到编辑 ——
        #: 但库里没有对应行，改值就会 404「配置项不存在」：**能看能点不能存**。
        #: 实测（2026-10-25）：工作区级 8 个键的行迁到 `workspace='xhs'` 后，
        #: 未配置过的工作区（如未来的 douyin）必然落到这条路径。
        #:
        #: 判据：只有**归属本级的已知键**才允许「首次落库」；未知键仍 404 ——
        #: 否则任何人都能用 PUT 凭空造出任意配置键。
        if settings_service.scope_of(key) != resolved_scope:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="配置项不存在")
        row = settings_service.ensure_row(
            db,
            key=key,
            scope=resolved_scope,
            user_id=current_user.id,
            workspace=clean_ws,
        )
    fields = payload.model_dump(exclude_unset=True)
    try:
        settings_service.update_setting(
            db,
            row,
            value=fields["value"] if "value" in fields else settings_service.UNSET,
            value_type=fields.get("value_type"),
            category=fields.get("category"),
            label=fields.get("label"),
            description=fields.get("description"),
            requires_restart=fields.get("requires_restart"),
            is_dangerous=fields.get("is_dangerous"),
            updated_by=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    db.commit()
    db.refresh(row)
    _wake_runtime_consumers(row.key)
    business_log_service.record(
        "settings.update",
        action="update",
        actor_id=current_user.id,
        actor_name=current_user.username,
        target_type="setting",
        target_id=row.key,
        summary=f"修改{resolved_scope}级配置 {row.key} = {row.value}",
        detail={"scope": resolved_scope, "workspace": clean_ws},
        db=db,
    )
    return settings_service.serialize_setting(row, db=db)


@router.delete("/{key}")
def delete_setting(
    key: str,
    payload: SettingUpdateRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除配置行 ⇒ 该键**回落到下一级默认**（设计 64：工作区删→回落用户/系统级）。

    响应带 `effective_value`：删除的语义是「恢复默认」，用户必须看得见恢复成了什么。
    """
    scope_in = (payload.scope if payload is not None else "user") or "user"
    ws_in = payload.workspace if payload is not None else None
    resolved_scope, clean_ws = _resolve_scope(db, current_user, scope_in, ws_in)
    row = settings_service.find_scoped(
        db, scope=resolved_scope, key=key, user_id=current_user.id, workspace=clean_ws
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="配置项不存在")
    outcome = settings_service.delete_setting(db, row)
    db.commit()
    _wake_runtime_consumers(outcome["key"])
    business_log_service.record(
        "settings.delete",
        action="delete",
        actor_id=current_user.id,
        actor_name=current_user.username,
        target_type="setting",
        target_id=key,
        summary=f"删除{resolved_scope}级配置 {key}（回落 {outcome.get('effective_value')}）",
        detail={"scope": resolved_scope, "workspace": clean_ws},
        db=db,
    )
    return {
        "key": outcome["key"],
        "status": "deleted",
        "effective_value": outcome["effective_value"],
        "is_live": settings_service.is_live(outcome["key"]),
    }

# --------------------------------------------------------------------------- #
# apply-to-all 退场（设计 64 §1.5）：系统级配置取代「复制给所有人」的补丁语义 ——
# 管理员在系统管理改一次系统级行即全站生效，不需要把 26 份副本互相覆盖。
# 显式 410 墓碑（不静默 404）：旧前端还在调用时能看到明确的迁移指引。
# --------------------------------------------------------------------------- #
@router.post("/apply-to-all", include_in_schema=False)
def apply_to_all_gone() -> dict:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "code": "SETTING_APPLY_ALL_RETIRED",
            "message": "「应用到所有用户」已退场：请改在系统管理 → 配置管理维护系统级配置（对全站直接生效）",
        },
    )