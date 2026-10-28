"""字典（`dict_types` + `dict_items` 合并为一张自引用父子表，L0 项目骨架基础表）。

2026-10-28 用户口径「dict_types 和 dict_items 合并一张表，父子，懒加载，项目骨架基础表」
⇒ 两张表合并为 `dict_rows`，`row_type` 分 `type` / `item` 两型：

| 层 | `row_type` | 回答的问题 |
|---|---|---|
| **类型** | `type` | 这组枚举叫什么、前端 `useDict(code)` 用哪个键 |
| **项** | `item` | 这个枚举有哪几个选项、显示成什么颜色 |

## 单表继承（不是别名）

`DictType` / `DictItem` 是 `DictRow` 的两个**子类**（`polymorphic_on=row_type`）。
好处是 `select(DictType)` 自动带 `WHERE row_type='type'` —— 现有几十处查询
**不用改**，也不会因为漏加条件而把类型行和项行混读。

⚠️ 判据是 `row_type`，不是 `parent_id IS NULL`：后者只是「类型行没有父」的巧合，
任何孤儿项行都会被当成类型读出来。

## 字典是**镜像**，不是权威

`note.PUBLISH_STATUSES` / `scheduled_job.JOB_STATUSES` 等 Python 常量仍是代码的
唯一真相源；字典只服务**前端下拉**。漂移由守卫 `test_dict_mirrors_python_constants`
报红，**不做双向同步**（那是两份真相）。

## 不并入 `system_settings`

那是「每用户一份的 KV」（26 人 × 9 键 = 209 行），而字典是**全局共享、带层级**的
枚举 —— 混进去就是「每用户各存一份同样的枚举」。

## `type_code` 是冗余列

免 join 的高频读（`GET /dict/items?type_code=…` 每次渲染下拉都走）。写入时由
`parent_id` 推导，守卫断言两者一致 —— 冗余列只在「有东西保证它一致」时才不是隐患。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

DICT_STATUS_NORMAL = "normal"
DICT_STATUS_DISABLED = "disabled"
DICT_STATUSES: tuple[str, ...] = (DICT_STATUS_NORMAL, DICT_STATUS_DISABLED)

#: 行类型（**父子唯一判据**）。
ROW_TYPE = "type"
ROW_ITEM = "item"

#: antd `Tag` 的颜色档（`list_class` 的取值白名单）。
#: 设计 63 §2.3：由 5 档状态色放宽到 **antd 全量预设**（5 状态色 + 13 预设色
#: + processing/error）—— 前端既有枚举颜色用的是 green/orange/gold/cyan/purple
#: 这类预设色，不放宽字典就表达不了真实颜色（迁移 `f8a9b0c1d2e3`）。
LIST_CLASS_DEFAULT = "default"
LIST_CLASS_SUCCESS = "success"
LIST_CLASS_WARNING = "warning"
LIST_CLASS_DANGER = "danger"
LIST_CLASS_INFO = "info"
LIST_CLASS_PROCESSING = "processing"
LIST_CLASS_ERROR = "error"
LIST_CLASSES: tuple[str, ...] = (
    LIST_CLASS_DEFAULT,
    LIST_CLASS_SUCCESS,
    LIST_CLASS_WARNING,
    LIST_CLASS_DANGER,
    LIST_CLASS_INFO,
    LIST_CLASS_PROCESSING,
    LIST_CLASS_ERROR,
    # antd 预设色板（https://ant.design/docs/spec/colors-cn）
    "red",
    "volcano",
    "orange",
    "gold",
    "yellow",
    "lime",
    "green",
    "cyan",
    "blue",
    "geekblue",
    "purple",
    "magenta",
    "pink",
)
#: 与 CHECK / API `Field(pattern=...)` 共用一份正则（DEF-044）。
LIST_CLASS_PATTERN = "^(%s)$" % "|".join(LIST_CLASSES)

#: 全部列都在基类声明（单表继承下子类加列也会落到同一张表，
#: 分开写反而容易漏 NOT NULL 默认值）。
class DictRow(Base):
    """字典行：一行是「类型」或它下面的一项。"""

    __tablename__ = "sys_dicts"
    __table_args__ = (
        #: 类型行 `code` 唯一、项行按 (父, value) 唯一 —— 见迁移里的**部分唯一索引**
        #: （普通唯一索引会被「项行 code 为空串」撞车）。
        Index("ix_dict_rows_row_type", "row_type"),
        Index("ix_dict_rows_code", "code"),
        #: 项行按 `type_code` 取整组（每次渲染下拉都走这条）。
        Index("ix_dict_rows_type_code", "type_code"),
        Index("ix_dict_rows_parent", "parent_id"),
        CheckConstraint("status IN ('normal', 'disabled')", name="ck_dict_rows_status"),
        CheckConstraint("row_type IN ('type', 'item')", name="ck_dict_rows_row_type"),
        CheckConstraint(
            "list_class IN ('default', 'success', 'warning', 'danger', 'info', "
            "'processing', 'error', 'red', 'volcano', 'orange', 'gold', 'yellow', "
            "'lime', 'green', 'cyan', 'blue', 'geekblue', 'purple', 'magenta', 'pink')",
            name="ck_dict_rows_list_class",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: **父子唯一判据**：`type` = 类型行，`item` = 字典项行。
    row_type: Mapped[str] = mapped_column(String(8), nullable=False)
    #: 项行所属类型（`type` 行恒 NULL）。删类型连带删项。
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sys_dicts.id", ondelete="CASCADE"), nullable=True
    )

    # ---- 类型行字段（项行留空串）----
    #: 类型展示名（如「用户状态」）。
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: **类型编码**（唯一，如 `sys_user_status`）—— 前端 `useDict(code)` 的键。
    code: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    # ---- 项行字段（类型行留空串）----
    #: **冗余**类型编码（免 join；写入时由 `parent_id` 推导，守卫断言一致）。
    type_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: 项显示名。
    label: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: 项值（**永远是字符串**：`"1"` 与 `1` 混用迟早出错）。
    value: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    # ---- 两型共有 ----
    #: antd Tag 颜色档，取值见 `LIST_CLASSES`。
    list_class: Mapped[str] = mapped_column(
        String(16), default=LIST_CLASS_DEFAULT, nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: `normal` / `disabled`。停用 ⇒ 前端下拉为空（并清缓存）。
    status: Mapped[str] = mapped_column(
        String(16), default=DICT_STATUS_NORMAL, nullable=False
    )
    remark: Mapped[str] = mapped_column(String(255), default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=shanghai_now, onupdate=shanghai_now, nullable=False
    )

    #: 父子**懒加载**（用户口径「查询懒加载」）：查类型不自动拉项。
    children: Mapped[list["DictRow"]] = relationship(
        "DictRow",
        back_populates="parent",
        lazy="select",
        foreign_keys=[parent_id],
    )
    parent: Mapped[Optional["DictRow"]] = relationship(
        "DictRow",
        back_populates="children",
        remote_side=[id],
        lazy="select",
        foreign_keys=[parent_id],
    )

    __mapper_args__ = {"polymorphic_on": row_type, "polymorphic_identity": "row"}


class DictType(DictRow):
    """字典类型（如「用户状态」= `sys_user_status`）。**全局共享**。

    单表继承 ⇒ `select(DictType)` 自动带 `WHERE row_type='type'`。
    """

    __mapper_args__ = {"polymorphic_identity": ROW_TYPE}


class DictItem(DictRow):
    """字典项。`value` **永远是字符串**（不做 int）。

    `type_id` 是 `parent_id` 的 synonym —— 现有 `DictItem.type_id` 读写不用改。
    """

    #: 原 `dict_items.type_id` → 合并后的 `parent_id`（同一列的两个名字）。
    type_id = synonym("parent_id")

    __mapper_args__ = {"polymorphic_identity": ROW_ITEM}
