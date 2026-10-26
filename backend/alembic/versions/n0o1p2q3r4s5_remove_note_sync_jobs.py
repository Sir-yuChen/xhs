"""remove note_sync / goods_note_sync scheduled jobs（设计 75 §4.5，去本地化实时查询 S4）

内容库改**读平台实时**后，「把平台笔记翻页落进本地 `notes` 表」不再有任何数据消费者 ⇒
两个定时任务退场：

* `system.note_sync`（账号笔记同步，creator 源）
* `system.goods_note_sync`（账号商品笔记同步，千帆 `goods-note/list` 源）

代码侧同批已改：`job_registry` 摘除两个 `JobSpec`、`scheduler_service` 删两个执行函数、
`config/initial_data/scheduled_jobs.json` 11 → 9 行、`accounts.py` / `notes.py`
共 7 条端点改 410 墓碑、5 个权限码 + 菜单行删除。

⚠️ **`system.note_data_sync` 必须保留** —— 那是「数据中心笔记指标同步」
（butterfly → `note_datacenter_stats`），与「笔记本体」是两件事。删错了**不报错**，
几周后才发现指标没数据。`_KEEP_JOB_KEYS` 里显式防一手（本迁移只按
`RETIRED_JOB_KEYS` 删，但把保留清单写出来，防后人「顺手扩大到全部 note_* 」）。

⚠️ **为什么必须是迁移、不能只删注册表**（与 `j0k1l2m3n4o5` / `p1q2r3s4t5u6` 同一个教训）：
`scheduled_jobs` 行是**数据**，删掉注册表项只会让它变成 `unknown job_key` 的僵尸行 ——
`GET /api/schedule/jobs` 按行查、不 join 注册表 ⇒ **列表照显**，点「执行一次」才报错。

⚠️ **还要清 `apscheduler_jobs`**：这两行多是 cron 活跃档 ⇒ 调度器启动时会各注册一个
`job_<id>`。行删了但注册项留着，`sync_jobs_from_db` 的 prune 会顺带清掉，但**让迁移
自己清干净**更可靠 —— 不依赖「应用启动过一次」才生效。

⚠️ `downgrade()` **刻意不重建数据行**：被删的是**内置初始化数据**（内容恒定、没有用户
价值）。回退到旧代码版本时注册表里有这两项，启动 `ensure_builtin_jobs` 会自己补回来。
写死 INSERT 反而给 `config/initial_data/scheduled_jobs.json` 造出第二份真相源
（种子权威在随包数据文件）。

⚠️ **不动 `notes` 表、不删任何笔记行**：本卡只摘「同步入口 + 任务 + 码 + 菜单行」。
「`notes` 表只留本地生成的笔记」是**读路径**的结果（内容库不再从这张表取平台笔记），
存量 `synced` 行的物理清理归后续（需先反查 5 张引用表，硬约束 6 的教训）。

Revision ID: n0o1p2q3r4s5
Revises: n8o9p0q1r2s3
Create Date: 2026-10-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect, text

revision: str = "n0o1p2q3r4s5"
down_revision: Union[str, Sequence[str], None] = "n8o9p0q1r2s3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 退场的任务键（**只此一处**；常量本身仍留在 `job_registry` 供迁移与守卫引用）。
RETIRED_JOB_KEYS = (
    "system.note_sync",
    "system.goods_note_sync",
)

#: **必须保留**的任务键（写出来是为了让「顺手删全部 note_* 」在 review 时一眼可见）。
KEEP_JOB_KEYS = ("system.note_data_sync",)

#: APScheduler 持久化 job id 前缀（与 `scheduler_service.JOB_ID_PREFIX` 同值）。
#: ⚠️ 刻意**不 import** 那个模块：迁移必须能在「当时任意版本的代码」上跑通。
APSCHEDULER_JOB_PREFIX = "job_"


def _table_names() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _table_names()

    for keep in KEEP_JOB_KEYS:
        assert keep not in RETIRED_JOB_KEYS, f"{keep} 被误列入退场清单"

    #: ① 先取行 id —— 调度器注册项的 key 是 `job_<scheduled_jobs.id>` 拼出来的。
    retired_ids: list[int] = []
    if "scheduled_jobs" in tables:
        params = {f"k{i}": key for i, key in enumerate(RETIRED_JOB_KEYS)}
        placeholders = ", ".join(f":{name}" for name in params)
        retired_ids = [
            row[0]
            for row in bind.execute(
                text(f"SELECT id FROM scheduled_jobs WHERE job_key IN ({placeholders})"),
                params,
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

    #: ③ 删任务行（**日志不删** —— `scheduled_job_logs` 是执行历史，按设计保留）
    if "scheduled_jobs" in tables:
        params = {f"k{i}": key for i, key in enumerate(RETIRED_JOB_KEYS)}
        placeholders = ", ".join(f":{name}" for name in params)
        bind.execute(
            text(f"DELETE FROM scheduled_jobs WHERE job_key IN ({placeholders})"), params
        )


def downgrade() -> None:
    """**不重建数据行**（见模块 docstring）。

    回退到旧代码版本时，注册表里有这两个任务，启动 `ensure_builtin_jobs` 会按
    当时的种子把它们补回来。
    """
    return
