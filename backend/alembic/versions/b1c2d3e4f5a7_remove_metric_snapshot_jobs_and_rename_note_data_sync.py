"""remove metric snapshot jobs and rename note_data_sync (2026-10-13 用户口径)

2026-10-13 用户口径（原文）：

> 把商品洞察和内容洞察中的指标相关内容逻辑移除，包括店铺商品指标快照采集、
> 笔记指标快照采集定时任务移除，后续作为专项单独排期
> 离线笔记数据同步定时任务改为数据中心笔记指标同步

本迁移做**两件事**（同一轮口径，一次落地）：

1. **移除两个指标快照采集任务**：`system.note_metrics_collect`（笔记指标快照采集）/
   `system.shop_metrics_collect`（店铺商品指标快照采集）—— 整块指标快照/趋势子系统
   退场（端点、服务函数、注册表 JobSpec 均已删）。**表结构与历史数据保留**
   （`note_metric_snapshots` / `shop_metric_snapshots` 与 ORM 模型都不动），便于后续专项复用。
2. **改名** `system.note_data_sync`：中文名「离线笔记数据同步」→「数据中心笔记指标同步」
   （**键不变**，只改显示名；该任务是「落 `note_datacenter_stats` 供数据中心页展示 +
   无流量清理判定」，原名易被误解为一期「同步笔记」）。

⚠️ **为什么必须是迁移、不能只删注册表**：`scheduled_jobs` 行是**数据**，
删掉注册表项只会让它变成 `unknown job_key` 的僵尸行 —— 任务列表**照显**
（`GET /jobs` 按 `user_id` 查全表，不 join 注册表），点「执行一次」才报
`JOB_KEY_UNSUPPORTED`。必须在库里删掉（同 `p1q2r3s4t5u6` / `a7b8c9d0e1f3` 的处置）。

⚠️ **还要清 `apscheduler_jobs`**：两条是 cron 档（旧默认 `0 5 * * * *` / `0 35 * * * *`）
⇒ 历史库里必然有注册项，需一并删除，否则调度器会继续按旧 cron 跑已删注册表项空转。

⚠️ **改名要连历史日志一起改**：`scheduled_job_logs.job_name` 是**执行时快照**，
任务中心按它显示。只改任务行不改日志 ⇒ 同一个任务的历史名不一致（与
`c2d3e4f5a6b7` 改名迁移同一纪律）。**键未变**，故 `job_key` 不动、无需收敛重复行。

⚠️ **`downgrade()` 对删除项刻意不重建数据行**：被删的是**内置初始化数据**（内容恒定），
降级回到的代码版本注册表里有它们 ⇒ 启动/注册时自动补回来（R24 纪律，同
`a7b8c9d0e1f3`）。改名项则改回旧中文名（纯数据改写）。

Revision ID: b1c2d3e4f5a7
Revises: a7b8c9d0e1f3
Create Date: 2026-10-13
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect, text

revision: str = "b1c2d3e4f5a7"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 退场的任务键（**只此一处**；注册表里的 JobSpec 已随同删除）。
RETIRED_JOB_KEYS = (
    "system.note_metrics_collect",
    "system.shop_metrics_collect",
)

#: 改名任务（键不变，只改显示名）。
RENAME_JOB_KEY = "system.note_data_sync"
RENAME_OLD_NAME = "离线笔记数据同步"
RENAME_NEW_NAME = "数据中心笔记指标同步"

#: APScheduler 持久化 job id 前缀（与 `scheduler_service.JOB_ID_PREFIX` 同值）。
#: ⚠️ 刻意**不 import** 那个模块：迁移必须能在「当时任意版本的代码」上跑通。
APSCHEDULER_JOB_PREFIX = "job_"


def _table_names() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _table_names()

    # ① 取退场行的 id（先查后删：调度注册项 key 要用它们拼）
    retired_ids: list[int] = []
    if "scheduled_jobs" in tables:
        retired_ids = [
            row[0]
            for row in bind.execute(
                text(
                    "SELECT id FROM scheduled_jobs WHERE job_key IN "
                    "(:k0, :k1)"
                ),
                {"k0": RETIRED_JOB_KEYS[0], "k1": RETIRED_JOB_KEYS[1]},
            ).all()
        ]

    # ② 清调度器注册项
    if retired_ids and "apscheduler_jobs" in tables:
        params = {
            f"p{index}": f"{APSCHEDULER_JOB_PREFIX}{job_id}"
            for index, job_id in enumerate(retired_ids)
        }
        placeholders = ", ".join(f":{name}" for name in params)
        bind.execute(
            text(f"DELETE FROM apscheduler_jobs WHERE id IN ({placeholders})"), params
        )

    # ③ 删任务行（幂等：无行时 DELETE 影响 0 行）
    if "scheduled_jobs" in tables:
        bind.execute(
            text("DELETE FROM scheduled_jobs WHERE job_key IN (:k0, :k1)"),
            {"k0": RETIRED_JOB_KEYS[0], "k1": RETIRED_JOB_KEYS[1]},
        )

    # ④ 改名（键不变 ⇒ 只改名称列）
    if "scheduled_jobs" in tables:
        bind.execute(
            text("UPDATE scheduled_jobs SET name = :new WHERE job_key = :key"),
            {"new": RENAME_NEW_NAME, "key": RENAME_JOB_KEY},
        )
    if "scheduled_job_logs" in tables:
        bind.execute(
            text("UPDATE scheduled_job_logs SET job_name = :new WHERE job_key = :key"),
            {"new": RENAME_NEW_NAME, "key": RENAME_JOB_KEY},
        )


def downgrade() -> None:
    """删除项**不重建数据行**（见模块 docstring，由代码侧注册表播种恢复）；改名回旧值。"""
    bind = op.get_bind()
    tables = _table_names()
    if "scheduled_jobs" in tables:
        bind.execute(
            text("UPDATE scheduled_jobs SET name = :old WHERE job_key = :key"),
            {"old": RENAME_OLD_NAME, "key": RENAME_JOB_KEY},
        )
    if "scheduled_job_logs" in tables:
        bind.execute(
            text("UPDATE scheduled_job_logs SET job_name = :old WHERE job_key = :key"),
            {"old": RENAME_OLD_NAME, "key": RENAME_JOB_KEY},
        )
