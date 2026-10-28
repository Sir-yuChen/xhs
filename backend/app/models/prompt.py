from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

#: 模板任务类型白名单（设计 17 §3.2 / §7.2）。
#: 与 §7.2 `POST /llm/prompt-templates` 的 `LLM_TASK_TYPE_UNSUPPORTED` 判据同源 ——
#: 只此一份，禁在 API 层另写字面量副本（DEF-044「同一语义不留两份实现」）。
TASK_TYPE_TEXT = "text"
TASK_TYPE_IMAGE = "image"
TASK_TYPE_VIDEO = "video"
TASK_TYPE_COVER = "cover"
PROMPT_TASK_TYPES = (TASK_TYPE_TEXT, TASK_TYPE_IMAGE, TASK_TYPE_VIDEO, TASK_TYPE_COVER)

#: 提示词模块类别（自由文本，此处只给出建议集，不做白名单校验）。
PROMPT_MODULE_CATEGORIES = ("style", "tone", "audience", "structure", "taboo")


class PromptModule(Base):
    """公共提示词模块（设计 17 §3.1，T2）。

    语义：可复用的 prompt 片段，按类别组织，可组合拼接到任意生成
    （16 文档 §2.4-A 的「公共模块」层）。

    **唯一约束 `(user_id, name)`**：user 内模块名唯一（DEF-066 归属纪律 +
    「不造第二份真相源」）。删除被模板引用 ⇒ API 层 400 `PROMPT_MODULE_IN_USE`
    （**不级联删引用**，避免静默改坏模板）。
    """

    __tablename__ = "xhs_prompt_modules"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_prompt_modules_user_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 归属（DEF-066 纪律：资源行必属当前用户）。
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_users.id"), index=True)
    #: 模块名（user 内唯一展示名）。
    name: Mapped[str] = mapped_column(String(128))
    #: 类别（如 `style`/`tone`/`audience`）。
    category: Mapped[str] = mapped_column(String(64), index=True)
    #: Prompt 片段正文。
    content: Mapped[str] = mapped_column(Text)
    #: 拼接顺序（同组合内先后）。
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    #: 停用即不参与渲染。
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=shanghai_now, onupdate=shanghai_now
    )


class PromptTemplate(Base):
    """提示词模板（设计 17 §3.2，T2）。

    语义：按任务类型的完整 prompt 模板，引用多个公共模块。

    **`module_ids` 是 JSON 列表**（顺序即拼接序）—— 与 §5.1 渲染规则同源：
    `最终 Prompt = 基础指令 + Σ(模块按 sort_order) + 模板骨架 + 商品白名单字段 + 素材引用`。

    **`version` 只升不降**：仅当 `prompt_skeleton` 或 `module_ids` 变更时 +1
    （改名 / 改 is_default 不涨版本，那不是「骨架版本」）。
    """

    __tablename__ = "xhs_prompt_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    #: `text`/`image`/`video`/`cover`（白名单 `PROMPT_TASK_TYPES`）。
    task_type: Mapped[str] = mapped_column(String(32), index=True)
    #: 模板骨架（含 `{商品字段}` / `{模块}` 插槽，见 §5.1 白名单）。
    prompt_skeleton: Mapped[str] = mapped_column(Text)
    #: 引用的 `prompt_modules.id` 列表（顺序即拼接序）。
    module_ids: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    #: 按 (user_id, task_type) 唯一默认（写时清旧）。
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    #: 每次改骨架 / 模块引用 +1（历史可追溯，只升不降）。
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=shanghai_now, onupdate=shanghai_now
    )
