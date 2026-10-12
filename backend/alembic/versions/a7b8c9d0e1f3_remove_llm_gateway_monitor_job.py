"""remove llm gateway monitor scheduled job

用户口径（2026-10-13）：

* 「大模型的预警**移除 LLM 网关预警任务**，实现根据配置的规则**自动触发**，并站内消息提醒。」

预警**本身没有退场** —— `llm_gateway_alerts` 表、`/llm/gateway-alerts` 三件 CRUD 端点、
`evaluate_alert` 判定逻辑、站内通知链路**全部保留**。退场的只是**它在定时任务中心的存在**：
注册表项（`job_registry.py` 已删，否则「调用目标」下拉里还能选到它）+ 已播种的任务行。

**为什么**：预警改由「台账写入即**异步评估**」（`services/llm_gateway_alert_trigger.py`）
触发 —— 延迟从「≤10 分钟轮询」降到「秒级」，且不再需要常驻调度任务。

⚠️ **为什么必须是迁移、不能只删注册表**：`scheduled_jobs` 行是**数据**，
删掉注册表项只会让它变成 `unknown job_key` 的僵尸行 —— 任务列表**照显**
（`GET /jobs` 按 `user_id` 查全表，不 join 注册表），点「执行一次」才报
`JOB_KEY_UNSUPPORTED`。必须在库里删掉（同 `p1q2r3s4t5u6` 的处置）。

⚠️ **还要清 `apscheduler_jobs`**：该行是 cron 档（旧默认 `0 */10 * * * *`）⇒
历史库里必然有注册项，需一并删除，否则调度器会继续按旧 cron 跑
（跑的还是已删注册表项 ⇒ `JOB_KEY_UNSUPPORTED` 空转）。让迁移自己清干净更可靠 ——
不依赖调度器启动才生效。

⚠️ **`downgrade()` 刻意不重建数据行**：被删的行是**内置初始化数据**（内容恒定），
降级回到的代码版本注册表里**有**这一项 ⇒ 启动/注册时会自动补回来，写死 INSERT
反而引入「迁移里的种子」与代码注册表两份真相源（R24 纪律，同 `p1q2r3s4t5u6`）。

Revision ID: a7b8c9d0e1f3
Revises: c0ffee000002
Create Date: 2026-10-13
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect, text

revision: str = "a7b8c9d0e1f3"
down_revision: Union[str, Sequence[str], None] = "c0ffee000002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 退场的任务键（**只此一处**；注册表里的 JobSpec 已随同删除）。
RETIRED_JOB_KEY = "system.llm_gateway_monitor"

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
                text("SELECT id FROM scheduled_jobs WHERE job_key = :key"),
                {"key": RETIRED_JOB_KEY},
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
            text("DELETE FROM scheduled_jobs WHERE job_key = :key"),
            {"key": RETIRED_JOB_KEY},
        )


def downgrade() -> None:
    """**不重建数据行**（见模块 docstring：由代码侧注册表播种恢复）。"""
    return
