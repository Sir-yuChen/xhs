from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

#: 配置行归属（设计 64 §1.2，2026-10-25 三级化，取代原「所有行必归属用户」口径）：
#: 同一张表三个归属级 ——
#: * **系统级**：`user_id IS NULL ∧ workspace IS NULL`（每 key 至多一行，admin 专属管理）；
#: * **用户级**：`user_id` 非空（原 `(user_id, key)` 语义不变，个人覆盖）；
#: * **工作区级**：`workspace` 非空（String32，同 `menus.workspace` 词汇表，工作区参数）。
#: 三条 partial unique 分别钉住三个级内的唯一性；生效优先级（设计 64 §1.3）：
#: 工作区级 > 用户级 > 系统级 > YAML/env > BUILTIN_DEFAULTS。
#: ⚠️ 原 UniqueConstraint("user_id","key") 由迁移 `b0c1d2e3f4a5` 换成 partial unique
#: （user_id 转 nullable 后全表约束会挡住系统级行）。

#: 值类型白名单。`value` 列一律存 **JSON 文本**，类型信息靠这一列解码 ——
#: 不靠「猜」（`"60"` 是字符串还是数字，猜错就是静默错值）。
VALUE_TYPE_INT = "int"
VALUE_TYPE_FLOAT = "float"
VALUE_TYPE_BOOL = "bool"
VALUE_TYPE_STR = "str"
VALUE_TYPE_JSON = "json"
VALUE_TYPES: tuple[str, ...] = (
    VALUE_TYPE_INT,
    VALUE_TYPE_FLOAT,
    VALUE_TYPE_BOOL,
    VALUE_TYPE_STR,
    VALUE_TYPE_JSON,
)

#: 分组（UI 分组 + 筛选）。刻意**不在这里限制**用户新增行的分组 ——
#: 白名单只管**种子**的合法性，自定义行允许任意短字符串（否则「增」就名存实亡）。
CATEGORY_SYNC = "sync"
CATEGORY_SCHEDULER = "scheduler"
CATEGORY_PUBLISH = "publish"
CATEGORY_AI = "ai"
CATEGORY_FEATURE = "feature"
#: 笔记生成/配图相关（如 `xhs_note_image_total_limit`）。
CATEGORY_NOTE = "note"


class SystemSetting(Base):
    """运行时配置行（设计 10 §7 → 设计 64 §1 三级化）。

    与 `Settings`（`core/config.py`）的分工**不重叠**：

    * `Settings` = **启动期**配置（YAML / env），语义是「进程生命周期内不变」，带 `lru_cache`；
    * 本表 = **运行期**配置，改完**立即生效**（不重启），按 系统级/用户级/工作区级 三层归属。

    判据（设计 10 §3.3）：**含密钥 / 影响能否启动 / 部署环境差异 ⇒ 留在 git**；
    **业务策略参数 / 需热改 / 需审计 ⇒ 进本表**。
    """

    __tablename__ = "system_settings"
    __table_args__ = (
        # 三个级各一条 partial unique（DEF-044：同一语义只留一份约束表达）。
        Index(
            "uq_system_settings_system_key",
            "key",
            unique=True,
            sqlite_where=text("user_id IS NULL AND workspace IS NULL"),
            postgresql_where=text("user_id IS NULL AND workspace IS NULL"),
        ),
        Index(
            "uq_system_settings_user_key",
            "user_id",
            "key",
            unique=True,
            sqlite_where=text("user_id IS NOT NULL"),
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index(
            "uq_system_settings_workspace_key",
            "workspace",
            "key",
            unique=True,
            sqlite_where=text("workspace IS NOT NULL"),
            postgresql_where=text("workspace IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 用户级归属；系统级/工作区级行为 NULL。
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )
    #: 工作区级归属（`menus.workspace` 词汇表：xhs/douyin/…/system）；其余级为 NULL。
    workspace: Mapped[Optional[str]] = mapped_column(
        String(32), index=True, nullable=True
    )
    key: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    #: JSON 文本（不是 JSON 列）：SQLAlchemy 的 JSON 列在 SQLite 上做原地改**不标脏**
    #: （本项目踩过），而配置行的读写路径本来就是「整串替换」⇒ 用 Text 反而少一个坑。
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    value_type: Mapped[str] = mapped_column(String(16), default=VALUE_TYPE_STR, nullable=False)
    category: Mapped[str] = mapped_column(String(32), default=CATEGORY_FEATURE, nullable=False)
    label: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 需重启才生效（UI 加显式标记）。⚠️ 与 `is_live`（**派生**，见 `settings_service`）
    #: 不是一回事：这个说「要重启」，那个说「有没有读点」。
    requires_restart: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 危险开关（UI 二次确认）
    is_dangerous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 谁改的（审计；种子行 / 手工改库可能为空 ⇒ nullable，不造假）
    updated_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now, nullable=False)
