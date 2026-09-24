from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


class AutoTask(Base):
    """自动运营任务（关键词 → 搜索 → AI 改写 → 发布管线）的**业务配置**。

    ⚠️ 2026-09-24（设计 15 §4.3）：`schedule_type` / `schedule_time` /
    `schedule_days` / `schedule_interval_hours` 四列**已删除** —— 调度语义全部
    迁往 `scheduled_jobs` 行（本表经 `schedule_job_id` 1:1 关联）。同一语义
    不留两份实现；被删四列的存量值由迁移 `y8z9a0b1c2d3` 换算为 job 行的
    cron/interval 触发器（旧 API 的 daily/weekly/interval 三档由前端 cron
    预设一键等价，见 `lib/cron-builder.ts` 的 `CRON_PRESETS`）。

    `last_run_at` / `total_published` 与 job 行的运行态计数**语义不同**
    （前者=发布计数，后者=执行计数），各自保留。
    """

    __tablename__ = "auto_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    keywords: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    pc_account_id: Mapped[int] = mapped_column(ForeignKey("platform_accounts.id"))
    creator_account_id: Mapped[int] = mapped_column(ForeignKey("platform_accounts.id"))
    ai_instruction: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="active")
    # 1:1 关联的调度行（设计 15 §7.1）；manual-only 的老任务也建行，只是 status=paused。
    schedule_job_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("scheduled_jobs.id"), nullable=True, index=True
    )
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    total_published: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
