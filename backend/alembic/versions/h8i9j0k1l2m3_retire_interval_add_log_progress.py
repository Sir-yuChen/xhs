"""retire interval trigger + add log progress (design 15 §3.1 / §4.1 / §4.2, R13/R14)

Revision ID: h8i9j0k1l2m3
Revises: f6g7h8i9j0k1
Create Date: 2026-09-24

2026-09-24 用户口径（两项合入本迁移，同批次减少迁移数量）：

**R14 · 任务类型收敛为三档，interval 退役**
- `trigger_type` 取值从 `at` / `cron` / `interval` 收敛为 **`manual` / `cron` / `at`**；
- 「间隔周期」并入 Cron 周期（如「每 6 小时」写成 6 小时步长式 cron）；
- `scheduled_jobs.interval_seconds` 列**彻底删除**（同 `auto_tasks` 删调度列手法，
  SQLite batch 模式）。老 `interval` 行按下表**自动换算**成等价 cron：

  | 旧 interval_seconds | 等价 cron         | 说明                  |
  |---------------------|-------------------|-----------------------|
  | 60                  | `0 * * * * *`     | 每分钟（秒位 0）      |
  | 300                 | `0 */5 * * * *`   | 每 5 分钟             |
  | 1800                | `0 */30 * * * *`  | 每 30 分钟            |
  | 3600                | `0 0 * * * *`     | 每小时整点            |
  | 7200                | `0 0 */2 * * *`   | 每 2 小时             |
  | 21600               | `0 0 */6 * * *`   | 每 6 小时             |
  | 86400               | `0 0 0 * * *`     | 每天 0 点             |
  | 其它                | 按分钟取整回退     | 见 `interval_to_cron()` |

  换算函数 `interval_to_cron()` 与运行期同源（迁移内联一份，迁移要可重放、
  不 import 应用代码 —— 与 `y8z9a0b1c2d3` 的换算先例一致）。

**R13 · 日志进度列**
- `scheduled_job_logs.progress`（Integer 0~100，默认 0）：任务执行进度百分比，
  供日志表「任务进度 + 耗时或异常」合并列渲染进度条。存量日志补 0
  （0 = 「未上报进度」的合法表达，非猜想值）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h8i9j0k1l2m3"
down_revision: Union[str, None] = "f6g7h8i9j0k1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def interval_to_cron(seconds: int) -> str:
    """interval 秒数 → 6 位 cron（秒 分 时 日 月 周）。

    与注册表默认种子同口径；无法整除时保守回退。**单一换算实现**：
    测试从本模块 import 它（`test_scheduled_jobs.py::_interval_to_cron`），
    不重抄一份 —— 两处口径漂移即缺陷。
    """
    if seconds <= 0:
        return "0 0 * * * *"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        if hours <= 1:
            return "0 0 * * * *"
        if hours <= 23:
            return f"0 0 */{hours} * * *"
        return "0 0 0 * * *"
    minutes = seconds // 60
    if seconds % 60 == 0 and 1 <= minutes <= 59:
        # 分钟粒度：每 N 分钟（1 分钟 ⇒ 每分钟）
        return f"0 */{minutes} * * * *" if minutes > 1 else "0 * * * * *"
    # 非整分/整时：向上取整小时
    hours = -(-seconds // 3600) or 1
    if hours <= 1:
        return "0 0 * * * *"
    if hours <= 23:
        return f"0 0 */{hours} * * *"
    return "0 0 0 * * *"


def upgrade() -> None:
    # ---- R13：日志进度列 ----
    with op.batch_alter_table("scheduled_job_logs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("progress", sa.Integer(), nullable=False, server_default="0")
        )

    # ---- R14：interval 行换算为等价 cron，然后删列 ----
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, interval_seconds FROM scheduled_jobs "
            "WHERE trigger_type = 'interval'"
        )
    ).fetchall()
    for row in rows:
        seconds = int(row[1] or 0)
        conn.execute(
            sa.text(
                "UPDATE scheduled_jobs SET trigger_type = 'cron', cron_expression = :cron "
                "WHERE id = :id"
            ),
            {"cron": interval_to_cron(seconds), "id": row[0]},
        )

    with op.batch_alter_table("scheduled_jobs", schema=None) as batch_op:
        batch_op.drop_column("interval_seconds")


def downgrade() -> None:
    with op.batch_alter_table("scheduled_jobs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("interval_seconds", sa.Integer(), nullable=False, server_default="3600")
        )
    # cron → interval 不逆向换算（cron 表达力是 interval 的超集，信息不可逆丢；
    # 与 y8z9a0b1c2d3 的降级口径一致：只恢复结构，不还原语义）。

    with op.batch_alter_table("scheduled_job_logs", schema=None) as batch_op:
        batch_op.drop_column("progress")
