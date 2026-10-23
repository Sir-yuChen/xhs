"""字典（`dict_types` + `dict_items`，两层固定，不合并）—— 设计 47 §2.4 / 47 §2.5。

## 为什么两张表

设计 48 §3.2 裁定**不合并**：一个类型下有 N 项是**固定两层**（不是任意深度的树），
合成一张就要靠 `parent_id` 区分「类型行」与「项行」—— 那是 `menus` 的形状，
用在这里只会让每条查询都要多带一个类型判别条件。

也不能并进 `system_settings`：那是「每用户一份的 KV」（实测 26 人 × 9 键 = 209 行），
而字典是**全局共享、带层级**的枚举 —— 混进去就是「每用户各存一份同样的枚举」。

## 字典是**镜像**，不是权威

`note.PUBLISH_STATUSES` / `scheduled_job.JOB_STATUSES` 等 Python 常量仍是代码的
唯一真相源；字典只服务**前端下拉**（P17 保证前端不再写死数组）。漂移由守卫
`test_dict_mirrors_python_constants` 报红，**不做双向同步**（那是两份真相）。

## `dict_items.type_code` 是冗余列

免 join 的高频读（`GET /dict/items?type_code=…` 每次渲染下拉都走）。写入时由
`type_id` 推导，守卫断言两者一致 —— 冗余列只在「有东西保证它一致」时才不是隐患。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

DICT_STATUS_NORMAL = "normal"
DICT_STATUS_DISABLED = "disabled"
DICT_STATUSES: tuple[str, ...] = (DICT_STATUS_NORMAL, DICT_STATUS_DISABLED)

#: antd `Tag` 的颜色档（`list_class` 的取值白名单）
LIST_CLASS_DEFAULT = "default"
LIST_CLASS_SUCCESS = "success"
LIST_CLASS_WARNING = "warning"
LIST_CLASS_DANGER = "danger"
LIST_CLASS_INFO = "info"
LIST_CLASSES: tuple[str, ...] = (
    LIST_CLASS_DEFAULT,
    LIST_CLASS_SUCCESS,
    LIST_CLASS_WARNING,
    LIST_CLASS_DANGER,
    LIST_CLASS_INFO,
)


class DictType(Base):
    """字典类型（如「用户状态」= `sys_user_status`）。**全局共享**。"""

    __tablename__ = "dict_types"
    __table_args__ = (
        CheckConstraint(
            "status IN ('normal', 'disabled')", name="ck_dict_types_status"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: **唯一**，如 `sys_user_status`（前端 `useDict(code)` 的键）
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    remark: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    #: 停用 ⇒ 前端下拉为空（并清缓存）
    status: Mapped[str] = mapped_column(
        String(16), default=DICT_STATUS_NORMAL, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=shanghai_now, onupdate=shanghai_now, nullable=False
    )


class DictItem(Base):
    """字典项。`value` **永远是字符串**（不做 int：`"1"` 与 `1` 混用迟早出错）。"""

    __tablename__ = "dict_items"
    __table_args__ = (
        UniqueConstraint("type_id", "value", name="uq_dict_items_type_value"),
        #: 按 `type_code` 取整组（每次渲染下拉都走这条）
        Index("ix_dict_items_code", "type_code"),
        CheckConstraint(
            "status IN ('normal', 'disabled')", name="ck_dict_items_status"
        ),
        CheckConstraint(
            "list_class IN ('default', 'success', 'warning', 'danger', 'info')",
            name="ck_dict_items_list_class",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type_id: Mapped[int] = mapped_column(
        ForeignKey("dict_types.id", ondelete="CASCADE"), nullable=False
    )
    #: **冗余**（免 join）。写入时由 `type_id` 推导；守卫断言与 `dict_types.code` 一致。
    type_code: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(String(64), nullable=False)
    list_class: Mapped[str] = mapped_column(
        String(16), default=LIST_CLASS_DEFAULT, nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=DICT_STATUS_NORMAL, nullable=False
    )
    remark: Mapped[str] = mapped_column(String(255), default="", nullable=False)
