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


def _get_owned_setting(db: Session, current_user: User, key: str) -> SystemSetting:
    row = settings_service.find_setting(db, current_user.id, key)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="配置项不存在")
    return row


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


@router.get("")
def list_settings(
    category: Optional[str] = Query(default=None, description="按分组筛选；留空 = 全部"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """本用户的配置列表（含 `is_live` 生效性标记）。"""
    rows = settings_service.list_settings(db, current_user.id, category=category)
    return {
        "items": [settings_service.serialize_setting(row, db=db) for row in rows],
        "total": len(rows),
        "live_keys": settings_service.live_keys(),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_setting(
    payload: SettingCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """新增配置项。键已存在 ⇒ 409（不静默改成「更新」—— 那会让用户以为新建成功了）。"""
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
    return settings_service.serialize_setting(row, db=db)


@router.get("/{key}")
def get_setting(
    key: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """单条配置（别人的键 ⇒ 404）。"""
    row = _get_owned_setting(db, current_user, key)
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
    row = _get_owned_setting(db, current_user, key)
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
    return settings_service.serialize_setting(row, db=db)


@router.delete("/{key}")
def delete_setting(
    key: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除配置行 ⇒ 该键**回落到默认**（settings / 内置兜底）。

    响应带 `effective_value`：删除的语义是「恢复默认」，用户必须看得见恢复成了什么。
    """
    row = _get_owned_setting(db, current_user, key)
    outcome = settings_service.delete_setting(db, row)
    db.commit()
    _wake_runtime_consumers(outcome["key"])
    return {
        "key": outcome["key"],
        "status": "deleted",
        "effective_value": outcome["effective_value"],
        "is_live": settings_service.is_live(outcome["key"]),
    }

# --------------------------------------------------------------------------- #
# 应用到所有用户（设计 48 §M8 定稿方案）
# --------------------------------------------------------------------------- #
class ApplyToAllRequest(BaseModel):
    """把当前用户的某个键值复制到**所有用户**。"""

    model_config = ConfigDict(extra="forbid")

    keys: list[str] = Field(default_factory=list, max_length=64)


@router.post("/apply-to-all")
def apply_to_all(
    payload: ApplyToAllRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """把当前用户名下的配置值复制给所有用户（**仅 admin**）。

    ## 为什么是「复制一份行」而不是「加一个 scope 列」

    `system_settings` 的归属已经由 `user_id NOT NULL` 表达；再加 `scope` 会产生
    `scope='global'` 且 `user_id` 有值这种自相矛盾的行（设计 48 §M8 的三条判据）。
    全站基线用**随包种子**（`config/initial_data/system_settings.json`）表达，
    而「一键把这个值推广给所有人」是一次**显式**的管理动作 —— 落成每用户一行，
    语义清楚且有审计痕迹（哪一行是谁改的、什么时候改的）。

    ## 为什么不做「清掉别人的行」

    只**覆盖**已存在的键、**补建**缺失的行，不删除任何人的其他配置
    —— 管理员的职责是「统一这个值」，不是「清空别人的配置」。

    ## 与「种子只补缺不覆盖」的区别

    种子纪律管的是**升级**（用户改过的不能被系统悄悄打回）；
    这里是**管理员主动点按钮**，明确要求覆盖。两者不冲突：
    一个是系统行为（保守），一个是人的决策（显式）。
    """
    from backend.app.core.roles import ROLE_ADMIN, normalize_role
    from backend.app.models import SystemSetting
    from sqlalchemy import select

    if normalize_role(current_user.role) != ROLE_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "SETTING_APPLY_ALL_FORBIDDEN",
                "message": "只有超级管理员能把配置应用到所有用户",
            },
        )

    #: 取当前用户在这些键上的**有效行**（没有行就用内置默认 —— 那样推广的
    #: 是「我实际生效的值」，而不是「我没有配」）
    updated = created = 0
    details: list[dict[str, Any]] = []

    for key in payload.keys:
        source = db.execute(
            select(SystemSetting).where(
                SystemSetting.user_id == current_user.id, SystemSetting.key == key
            )
        ).scalar_one_or_none()
        value = (
            source.value
            if source is not None
            else settings_service.effective_value(db, current_user.id, key)
        )

        users = db.execute(select(User.id).where(User.deleted_at.is_(None))).scalars().all()
        per_key_updated = per_key_created = 0
        for uid in users:
            if uid == current_user.id:
                continue
            row = db.execute(
                select(SystemSetting).where(
                    SystemSetting.user_id == uid, SystemSetting.key == key
                )
            ).scalar_one_or_none()
            if row is None:
                if source is not None:
                    db.add(
                        SystemSetting(
                            user_id=uid,
                            key=key,
                            value=value,
                            value_type=source.value_type,
                            category=source.category,
                            label=source.label,
                            description=source.description,
                            requires_restart=source.requires_restart,
                            is_dangerous=source.is_dangerous,
                        )
                    )
                    per_key_created += 1
                #: 源也没有行 ⇒ 推广的是「内置默认」，那种情况**不必建行**
                #: （不建 = 该用户自然读到同一个默认值，少一堆等价行）
            else:
                row.value = value
                if source is not None:
                    row.value_type = source.value_type
                per_key_updated += 1

        updated += per_key_updated
        created += per_key_created
        details.append(
            {
                "key": key,
                "value": value,
                "updated": per_key_updated,
                "created": per_key_created,
            }
        )

    db.commit()
    settings_service.invalidate()
    return {
        "ok": True,
        "updated": updated,
        "created": created,
        "details": details,
        "note": "只覆盖 / 补建指定键，不动其他配置，也不删除任何行",
    }

