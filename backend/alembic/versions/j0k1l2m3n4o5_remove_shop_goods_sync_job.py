"""remove shop_goods_sync scheduled job（设计 75 §2.4 / §4.5，去本地化实时查询 S2）

商品列表 / 详情改**实时查千帆**（`POST /xhs/qianfan/items` / `/item-detail`）后，
「整店商品定时落进本地 `shop_products` 台账」不再有任何数据消费者 ⇒
定时任务 `system.shop_goods_sync`（店铺商品同步）退场。

本迁移处置的是**数据行**，代码侧同批已改（三项）：
`job_registry` 摘除、两个端点改 410 墓碑、
`config/initial_data/scheduled_jobs.json` 删掉该条种子。

⚠️ **为什么必须是迁移、不能只删注册表**（与 `p1q2r3s4t5u6` 同一个教训）：
`scheduled_jobs` 行是**数据**，删掉注册表项只会让它变成 `unknown job_key` 的
僵尸行 —— `GET /api/schedule/jobs` 按行查、不 join 注册表 ⇒ **列表照显**，
点「执行一次」才报 `JOB_KEY_UNSUPPORTED`。必须在库里删掉。

⚠️ **还要清 `apscheduler_jobs`**：该行多半是 cron 活跃档 ⇒ 调度器启动时会为它
注册一个 `job_<id>`。行删了但注册项留着，`sync_jobs_from_db` 的 prune 会顺带清掉，
但**让迁移自己清干净**更可靠 —— 不依赖「应用启动过一次」才生效。

⚠️ `downgrade()` **刻意不重建数据行**：被删的是**内置初始化数据**（内容恒定、
没有用户价值），回退到旧代码版本时注册表里有这一项，启动 `ensure_builtin_jobs`
会自己补回来。写死 INSERT 反而给 `config/initial_data/scheduled_jobs.json`
造出第二份真相源（R24：种子权威在随包数据文件）。

⚠️ **不动 `shop_products` 表、不动 `shop_goods_sync_service.py`**：本卡（S2）
只切读路径与摘入口；表 DROP 与残留引用清理归 **S5**（设计 75 §4.5 第 4 步），
那里要**先反查外键**（该表有 4 张引用表，硬约束 6 的教训）。

Revision ID: j0k1l2m3n4o5
Revises: e8f9a0b1c2d3
Create Date: 2026-10-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect, text

revision: str = "j0k1l2m3n4o5"
down_revision: Union[str, Sequence[str], None] = "e8f9a0b1c2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 退场的任务键（**只此一处**；常量本身仍留在 `job_registry` 供文档/守卫引用）。
RETIRED_JOB_KEY = "system.shop_goods_sync"

#: APScheduler 持久化 job id 前缀（与 `scheduler_service.JOB_ID_PREFIX` 同值）。
#: ⚠️ 刻意**不 import** 那个模块：迁移必须能在「当时任意版本的代码」上跑通。
APSCHEDULER_JOB_PREFIX = "job_"


def _table_names() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _table_names()

    #: ① 先取行 id —— 调度器注册项的 key 是 `job_<scheduled_jobs.id>` 拼出来的。
    retired_ids: list[int] = []
    if "scheduled_jobs" in tables:
        retired_ids = [
            row[0]
            for row in bind.execute(
                text("SELECT id FROM scheduled_jobs WHERE job_key = :key"),
                {"key": RETIRED_JOB_KEY},
            ).all()
        ]

    #: ② 清调度器注册项
    if retired_ids and "apscheduler_jobs" in tables:
        params = {
            f"p{index}": f"{APSCHEDULER_JOB_PREFIX}{job_id}"
            for index, job_id in enumerate(retired_ids)
        }
        placeholders = ", ".join(f":{name}" for name in params)
        bind.execute(text(f"DELETE FROM apscheduler_jobs WHERE id IN ({placeholders})"), params)

    #: ③ 删任务行（含历史日志快照？**不删** —— 日志是执行历史，按设计保留）
    if retired_ids:
        bind.execute(
            text("DELETE FROM scheduled_jobs WHERE job_key = :key"),
            {"key": RETIRED_JOB_KEY},
        )


def downgrade() -> None:
    """**不重建数据行**（见模块 docstring）。

    回退到旧代码版本时，注册表里有 `system.shop_goods_sync`，启动
    `ensure_builtin_jobs` 会按当时的种子把它补回来。
    """
    return
