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

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

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
    """一条违规词（按登录用户隔离 —— ADR-021）。"""

    __tablename__ = "forbidden_words"
    __table_args__ = (
        #: ⚠️ 「不可重复」的**唯一实现**：`(user_id, group_name, word)` 复合唯一。
        #:
        #: 只索引 `word` 会让不同用户互相冲突（词库是各人各的）；
        #: 不含分组则「同词进两个分组」会被拒 —— 那是正常需求，不该拦。
        Index("ux_forbidden_words_user_group_word", "user_id", "group_name", "word", unique=True),
        Index("ix_forbidden_words_user_enabled", "user_id", "enabled"),
        Index("ix_forbidden_words_user_group", "user_id", "group_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    #: 词本身。**存用户原文**（不存归一化值）—— 存归一化会让用户看不懂
    #: 自己填了什么。匹配时再归一化（见 `forbidden_word_service.normalize`）。
    word: Mapped[str] = mapped_column(String(120))
    match_mode: Mapped[int] = mapped_column(Integer, default=MATCH_MODE_EXACT)
    #: 停用/启用。**软开关**：停用不删行 —— 曾经命中过哪些图要能追溯，
    #: 删了就查不到了（而「这个词以前拦下过什么」正是运营要问的）。
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    remark: Mapped[str] = mapped_column(String(255), default="")
    #: 分组名（用户自建，如「极限词」「医疗词」）。空串 = 未分组。
    #:
    #: ⚠️ 与「词」不同：**同名词可以出现在不同分组**（「最佳」在极限词和广告词
    #: 里各有一条是有意义的）。所以唯一键改成 `(user_id, group_name, word)`。
    group_name: Mapped[str] = mapped_column(String(MAX_GROUP_LEN), default=DEFAULT_GROUP)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)


class ForbiddenWordGroupRow(Base):
    """分组本体（**让「组」成为独立实体**，用户口径 2026-10-08：「组新增启用停用功能」）。

    ## 为什么不靠 `forbidden_words.group_name` 去重推出分组列表

    词行上只能表达「这个组有词」，表达不了「这个组被停用了」——
    停用后词还在（不该删，那是用户的词），但整组不参与预检匹配。
    所以分组需要自己的行。

    ## 语义

    - **组被停用 ⇒ 组内所有词都不参与匹配**（不必逐条停用）；词自己的
      `enabled` 仍然独立生效（两个开关是**与**关系）。
    - 组名唯一：`(user_id, name)`。
    - 删除组不在这里做（用户口径只要求启停）；词删光了组行留着也无害。
    """

    __tablename__ = "forbidden_word_groups"
    __table_args__ = (
        Index("ux_forbidden_word_groups_user_name", "user_id", "name", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    #: 分组名（空串 = 未分组；未分组也允许有独立开关）。
    name: Mapped[str] = mapped_column(String(MAX_GROUP_LEN), default=DEFAULT_GROUP)
    #: 整组启用/停用。停用 ⇒ 组内词全部不参与匹配。
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)


__all__ = [
    "DEFAULT_GROUP",
    "ForbiddenWord",
    "ForbiddenWordGroupRow",
    "MAX_WORDS_PER_GROUP",
    "MATCH_MODES",
    "MATCH_MODE_CONTAINS",
    "MATCH_MODE_EXACT",
    "MAX_GROUP_LEN",
]
