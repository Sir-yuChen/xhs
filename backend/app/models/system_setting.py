from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

#: 配置行归属用户（DEF-066 同口径）：**所有行必归属用户**，不存在全局共享行。
#: ⚠️ 设计 10 §3.1 的 `scope`（`global` / `user`）列**整条移除** —— 它表达的是同一件
#: 事（归属），多一列就是同一语义的第二份表达（DEF-044）；且 `scope=global` 的
#: 「谁都能改」语义在单机多用户下没有收益，只有越权面。
#: 唯一键是 `(user_id, key)`，不是 `key` 单列。

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
#: ⚠️ 该分组 2026-10 追加图片上限时**只改了种子、忘了补白名单**，
#: 导致 `test_settings_initial_data_fields_are_within_whitelists` 长期红。
CATEGORY_NOTE = "note"


class SystemSetting(Base):
    """运行时配置行（设计 10 §7，2026-09-27）。

    与 `Settings`（`core/config.py`）的分工**不重叠**：

    * `Settings` = **启动期**配置（YAML / env），语义是「进程生命周期内不变」，带 `lru_cache`；
    * 本表 = **运行期**配置，改完**立即生效**（不重启），按用户隔离。

    判据（设计 10 §3.3）：**含密钥 / 影响能否启动 / 部署环境差异 ⇒ 留在 git**；
    **业务策略参数 / 需热改 / 需审计 ⇒ 进本表**。
    """

    __tablename__ = "system_settings"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_system_settings_user_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
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
