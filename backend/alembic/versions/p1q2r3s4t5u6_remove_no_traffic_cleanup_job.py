"""remove no-traffic cleanup scheduled job

用户口径（2026-10-07）：

* 「无流量笔记清理任务不应在任务列表展示」
* 「无流量笔记清理任务表移除该配置」

清理**本身没有退场** —— `/users/no-traffic/cleanup` 端点、候选池、两张日志表
（`no_traffic_cleanup_logs` / `no_traffic_cleanup_items`）与「我的笔记」页的
「清理无流量笔记」按钮全部保留。退场的只是**它在定时任务中心的存在**：
注册表项（否则「调用目标」下拉里还能选到它）+ 已播种的任务行。

⚠️ **为什么必须是迁移、不能只删注册表**：`scheduled_jobs` 行是**数据**，
删掉注册表项只会让它变成 `unknown job_key` 的僵尸行 —— 任务列表**照显**
（`GET /jobs` 按 `user_id` 查全表，不 join 注册表），点「执行一次」才报
`JOB_KEY_UNSUPPORTED`。必须在库里删掉。

⚠️ **还要清 `apscheduler_jobs`**：该行是 manual 档 ⇒ 本就没被注册过，但历史库里
可能留有早期「cron 兜底」装配留下的注册项（迁移 `y8z9a0b1c2d3` 之前跑过的库）。
残留项会被 `sync_jobs_from_db` 的 prune 顺带清掉（`active_ids` 不含它），
但**让迁移自己清干净**更可靠 —— 不依赖调度器启动才生效。

⚠️ **备注文案统一**（用户口径「备注全部改为：系统内置任务」）：存量行的长文案
（「随安装附有；调度属性可调」/「代码默认种子，未在应用初始化数据中声明…」等）
一律改写成 `系统内置任务`。判据 = `remark` 以种子文案前缀 `系统内置任务` 开头
且不等于目标值 ⇒ **用户自建的备注不会被误伤**（用户行不会以此开头，同
`i0j1k2l3m4n5` 的判据口径）。

⚠️ **`downgrade()` 刻意不重建数据行**：被删的行是**内置初始化数据**（内容恒定、
没有用户价值），而「重建」只能靠 `ensure_builtin_jobs` 按**当时的注册表**播种 ——
本迁移的降级回到的代码版本注册表里**有**这一项，故启动/注册时会自己补回来。
写死 INSERT 反而会引入一份「迁移里的种子」与 `config/initial_data/` 两份真相源
（R24 明确把种子权威迁到了随包数据文件）。备注改写**不还原**（旧长文案没有
回退价值，如实写明）。

Revision ID: p1q2r3s4t5u6
Revises: e6f7a8b9c0d1
Create Date: 2026-10-07
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect, text

revision: str = "p1q2r3s4t5u6"
down_revision: Union[str, Sequence[str], None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 退场的任务键（**只此一处**；注册表里的常量已随同删除）。
RETIRED_JOB_KEY = "system.no_traffic_cleanup"

#: APScheduler 持久化 job id 前缀（与 `scheduler_service.JOB_ID_PREFIX` 同值）。
#: ⚠️ 刻意**不 import** 那个模块：迁移必须能在「当时任意版本的代码」上跑通，
#: 依赖应用模块会把迁移与代码版本绑死。
APSCHEDULER_JOB_PREFIX = "job_"

#: 种子备注前缀 + 目标文案（用户口径「备注全部改为：系统内置任务」）。
SEED_REMARK_PREFIX = "系统内置任务"
NEW_REMARK = "系统内置任务"


def _table_names() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _table_names()

    # ① 取退场行的 id（先查后删：注册项 key 要用它们拼）
    retired_ids: list[int] = []
    if "scheduled_jobs" in tables:
        retired_ids = [
            row[0]
            for row in bind.execute(
                text("SELECT id FROM scheduled_jobs WHERE job_key = :key"),
                {"key": RETIRED_JOB_KEY},
            ).all()
        ]

    # ② 清调度器注册项（先于任务行删除，顺序无关但先清更省事）
    if retired_ids and "apscheduler_jobs" in tables:
        params = {f"p{index}": f"{APSCHEDULER_JOB_PREFIX}{job_id}" for index, job_id in enumerate(retired_ids)}
        placeholders = ", ".join(f":{name}" for name in params)
        bind.execute(text(f"DELETE FROM apscheduler_jobs WHERE id IN ({placeholders})"), params)

    # ③ 删任务行
    if retired_ids:
        bind.execute(
            text("DELETE FROM scheduled_jobs WHERE job_key = :key"),
            {"key": RETIRED_JOB_KEY},
        )

    # ④ 备注文案统一（判据：种子前缀开头且不等于目标值 ⇒ 用户自建备注不受影响）
    if "scheduled_jobs" in tables:
        bind.execute(
            text(
                "UPDATE scheduled_jobs SET remark = :new "
                "WHERE remark LIKE :prefix AND remark != :new"
            ),
            {"new": NEW_REMARK, "prefix": f"{SEED_REMARK_PREFIX}%"},
        )


def downgrade() -> None:
    """**只回滚备注**（见模块 docstring：数据行由代码侧播种恢复，不在此重建）。

    备注改写刻意不还原 —— 旧长文案只是「来路说明」，回退它没有任何价值，
    而「记下原值再写回」会让本迁移多存一份与 `config/initial_data/` 重复的文案表。
    """
    return
