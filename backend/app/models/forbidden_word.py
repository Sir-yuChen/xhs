"""违规词库（设计 25 §2；用户口径 2026-10-08）。

## 这个表是「用户自己维护的词库」，不是平台词库的镜像

平台有 `check_forbidden_keyword`（**校验器**：拿文本问平台有没有违规），
但它不支持增删改查、没有「停用/启用」语义、也不返回词表 —— 用户要的
「新增 / 修改 / 停用 / 启用」只能落在本地表上。

## 「不可重复」由**数据库**保证，不是应用层 if

`(user_id, word)` 复合唯一索引。应用层「先查后插」会被并发写绕过
（两个请求同时查到「不存在」→ 都插 → 一个撞 IntegrityError），
而撞唯一约束会让整个事务回滚（DEF-088 同型：空值落行撞唯一约束整批崩）。
⇒ 应用层**照旧先查**（为了给出人话错误码），但**最终由数据库兜底**，
并把 IntegrityError 翻译成 409 而不是 500。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

#: 匹配模式：1 = 精确（整段相等）、2 = 包含（子串命中）。
#:
#: ⚠️ **默认 1（精确）** —— 用户口径 2026-10-08：「违规词的匹配方式默认**精确**」。
#: （原先默认包含，已被本条取代；包含仍可选，只是不再是默认值。）
MATCH_MODE_EXACT = 1
MATCH_MODE_CONTAINS = 2
MATCH_MODES: tuple[int, ...] = (MATCH_MODE_EXACT, MATCH_MODE_CONTAINS)

#: 未分组（用户没选分组时的归属）。
#:
#: ⚠️ 用户口径 2026-10-08：「违规词新增**分组**逻辑，点击预检支持选择指定
#: 违规词分组，支持多组，**默认全部分组**」。
#: 空串 = 未分组（不是 NULL）—— 省掉一层 None 分支，且「未分组」本身是合法状态。
DEFAULT_GROUP = ""
#: 分组名长度上限。
MAX_GROUP_LEN = 60

#: ⚠️ **每个分组最多 500 条**（用户口径 2026-10-08：「每组违规词限制最多 500 条」）。
#:
#: 为什么要有上限：分组是给预检挑选用的，一个组塞几千条会让预检的
#: 「按分组收窄」失去意义（选一组等于选全部），且词库匹配是逐词扫描。
MAX_WORDS_PER_GROUP = 500


class ForbiddenWord(Base):
    """违规词库 —— 一张自引用父子表（2026-10-28 合并）。

    `parent_id IS NULL` = **分组**（组的启停挂在这一行）；
    `parent_id` 非空 = **组内的一条词**。

    合并前是 `forbidden_word_groups` + `forbidden_words` 两张表。合并的收益是
    「组」有了唯一真相源：旧形态下词的 `group_name` 是字符串、分组行另存一份，
    词表里有 `满组`/`小红书` 两个组名而分组表 0 行 —— 两边可以不一致。

    **唯一性**：`(user_id, parent_id, word)` —— 同组内词不重复，**不同组可同名**
    （「最佳」在极限词组和广告词组各一条是正常需求）。
    未分组也算一个组（`parent_id` 指向「未分组」那一行）。

    **生效 = 组 enabled AND 词 enabled**（组停 ⇒ 组内词全部不参与匹配）。
    停用只置标记不删行：曾经命中过哪些图要能追溯。
    """

    __tablename__ = "xhs_forbidden_words"
    __table_args__ = (
        #: 同用户下**组名不重名**（父行）。
        Index(
            "uq_xhs_fw_group_name",
            "user_id",
            "name",
            unique=True,
            postgresql_where=text("parent_id IS NULL"),
        ),
        #: **同组内词不重复；不同组可同名**（子行）。
        Index(
            "uq_xhs_fw_group_word",
            "parent_id",
            "word",
            unique=True,
            postgresql_where=text("parent_id IS NOT NULL"),
        ),
        Index("ix_xhs_fw_user_enabled", "user_id", "enabled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: `NULL` = 分组行；非空 = 该组下的一条词。
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("xhs_forbidden_words.id", ondelete="CASCADE"), index=True, nullable=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_users.id"), index=True, nullable=False)
    #: **每一行都存组名**（空串 = 未分组）：父行是分组的定义，子行是所属组。
    #: 子行冗余这一份是为了让「按组过滤」不必 join 父表（旧查询直接可用）。
    name: Mapped[str] = mapped_column(String(MAX_GROUP_LEN), default=DEFAULT_GROUP, nullable=False, index=True)
    #: 子行 = 词本身（存用户原文，不存归一化值）；父行留空。
    word: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    #: 1 = 精确（整段相等）、2 = 包含（子串命中）。
    match_mode: Mapped[int] = mapped_column(Integer, default=MATCH_MODE_EXACT, nullable=False)
    #: 停用/启用（父行管整组，子行管单条）。
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    remark: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=shanghai_now, onupdate=shanghai_now, nullable=False
    )

    #: ---- 自引用父子关系（**懒加载**：访问 .children / .parent 时才发 SQL）----
    children: Mapped[list["ForbiddenWord"]] = relationship(
        back_populates="parent",
        foreign_keys=[parent_id],
        lazy="select",
    )
    parent: Mapped[Optional["ForbiddenWord"]] = relationship(
        back_populates="children",
        foreign_keys=[parent_id],
        remote_side=[id],
        lazy="select",
    )

    #: ---- 旧字段名别名（合并前的 `group_name`）----
    #: 服务层与前端按旧名读写，映射到父行的 `name`，避免改 20 多处调用点。
    @property
    def group_name(self) -> str:
        return self.name

    @group_name.setter
    def group_name(self, value: str) -> None:
        self.name = value


#: 同表内的行类型别名（父子共用一张表，读起来更清楚）。
ForbiddenWordRow = ForbiddenWord


__all__ = [
    "DEFAULT_GROUP",
    "ForbiddenWord",
    "MAX_WORDS_PER_GROUP",
    "MATCH_MODES",
    "MATCH_MODE_CONTAINS",
    "MATCH_MODE_EXACT",
    "MAX_GROUP_LEN",
]
