"""scheduled task center (design 15): scheduled_jobs + scheduled_job_logs

Revision ID: y8z9a0b1c2d3
Revises: x7y8z9a0b1c2
Create Date: 2026-09-24

定时任务中心重构（设计 15，贴近若依）：调度语义收敛到 `scheduled_jobs` 行
（cron/interval + 错过策略 + 并发开关 + 暂停/恢复），每次执行一行
`scheduled_job_logs`。

本迁移做四件事（顺序不能反）：

1. 建 `scheduled_jobs` / `scheduled_job_logs` 两表；
2. `auto_tasks` 加 `schedule_job_id` 列；
3. **存量换算**：每个 auto_tasks 行生成 1:1 job 行
   - `manual`                      → cron `0 0 9 * * *` + **paused**（惰性默认）
   - `daily` + `HH:MM`             → cron `0 MM HH * * *`
   - `weekly` + `days` + `HH:MM`   → cron `0 MM HH * * * D1,D2`
     ⚠️ 旧 `schedule_days` 是 **isoweekday（1=周一…7=周日）**，APScheduler
     `day_of_week` 数字是 **0=周一…6=周日** ⇒ 换算规则 `(d - 1) % 7`（7→6、1→0）。
   - `interval` + N 小时           → interval 触发器，`interval_seconds=N*3600`
   - 状态：`active→active`，`paused/completed→paused`
   - `job_key="auto_ops.keyword_pipeline"`、`params={"auto_task_id": id}`、
     `user_id` 继承、`is_builtin=0`
4. 回填 `schedule_job_id`，删 5 个旧列（4 个 schedule_* + `next_run_at`）；
   ~~播种 7 个系统 job 行（`auto_tasks_runner` 不播种——60s 轮询取消，
   AutoTask 各自持触发器）~~ —— **⚠️ 播种段已被 R24 删除**（2026-09-24）：
   种子改由**随安装包分发的应用初始化数据** `config/initial_data/scheduled_jobs.json`
   在应用启动时铺设（设计 15 §4.5）。见下方第 5 步处的详细理由。

⚠️ 迁移**不读环境变量**（可重放、可审计）：~~种子值按当前 config 默认值写死；
   运行期 `ensure_builtin_jobs()` 只对缺失行补种（读 config），两条路径的
   默认值必须一致（测试用夹具钉住）。~~
   **被 R24 取代**：播种段删除后，「迁移写死值 vs 运行期读 config」的**双路径
   一致性要求随之作废** —— 运行期成为唯一路径，值来自初始化数据文件。
   `_SYSTEM_SEEDS` 保留在文件顶部仅作**审计对照**，不再产生任何行。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "y8z9a0b1c2d3"
down_revision: Union[str, None] = "x7y8z9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 与 models/scheduled_job.py 的常量保持一致（迁移自包含，不 import 业务代码）
_JOB_GROUP_DEFAULT = "DEFAULT"
_JOB_GROUP_SYSTEM = "SYSTEM"
_MISFIRE_ONCE = "once"
_CONCURRENT_FORBID = "forbid"

# 系统种子行（job_key → (name, interval_seconds, status)）
# 默认值 = 2026-09-24 config.py 现值；启停的语义从「config 开关」搬进 job 行
# status（设计 15 §4.4 语义收缩：config 键此后只影响**全新安装**的种子）。
#
# ⚠️ **本表已不再被 upgrade() 使用**（R24，2026-09-24）：播种段已删，种子改由
#    `config/initial_data/scheduled_jobs.json` 在应用启动时铺设（设计 15 §4.5）。
#    保留仅为**审计对照** —— 它是当时的播种口径（interval 形态，后被 `h8i9j0k1l2m3`
#    换算成等价 cron）。测试用它对照历史值，**不要**据此恢复播种。
_SYSTEM_SEEDS: list[tuple[str, str, int, str]] = [
    ("system.due_publish", "定时发布补跑", 60, "active"),
    ("system.monitoring_refresh", "监控目标刷新", 60, "active"),
    ("system.cookie_health_check", "账号 Cookie 巡检", 7200, "active"),
    ("system.qianfan_health_check", "千帆登录态巡检", 21600, "active"),
    ("system.no_traffic_cleanup", "无流量笔记清理", 21600, "active"),
    ("system.note_sync", "账号笔记同步", 21600, "paused"),
    ("system.shop_goods_sync", "店铺商品同步", 21600, "paused"),
]


def _shanghai_now() -> datetime:
    """与 `core/time.py::shanghai_now()` 同口径（迁移自包含，不 import 业务代码）。"""
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)


def _parse_hhmm(value: Union[str, None]) -> tuple[int, int]:
    text = (value or "09:00").strip()
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return 9, 0
    return hour, minute


def _isoweekday_to_cron_dow(days_text: Union[str, None]) -> list[int]:
    """旧 `schedule_days`（isoweekday 1=周一…7=周日）→ cron `day_of_week`（0=周一…6=周日）。"""
    result: list[int] = []
    for chunk in (days_text or "").split(","):
        chunk = chunk.strip()
        if not chunk.isdigit():
            continue
        value = int(chunk)
        if 1 <= value <= 7:
            result.append((value - 1) % 7)
    return sorted(set(result))


def _legacy_to_trigger(schedule_type: Union[str, None], schedule_time, schedule_days, interval_hours) -> tuple[str, str, int, str]:
    """旧四档调度 → (trigger_type, cron_expression, interval_seconds, status)。

    坏值（解析不出时间 / weekly 无有效星期）退化为「每天 09:00 + paused」——
    与旧代码「算不出 next_run 就永不自动执行」的行为对齐（不猜、不静默跑）。
    """
    kind = (schedule_type or "manual").strip()
    if kind == "daily":
        hour, minute = _parse_hhmm(schedule_time)
        return "cron", f"0 {minute} {hour} * * *", 3600, "active"
    if kind == "weekly":
        hour, minute = _parse_hhmm(schedule_time)
        days = _isoweekday_to_cron_dow(schedule_days)
        if days:
            return "cron", f"0 {minute} {hour} * * * {','.join(str(d) for d in days)}", 3600, "active"
        return "cron", f"0 {minute} {hour} * * *", 3600, "paused"
    if kind == "interval":
        try:
            hours = int(interval_hours or 24)
        except (TypeError, ValueError):
            hours = 24
        return "interval", "", max(1, hours) * 3600, "active"
    # manual 与一切未知值：惰性默认，等用户在任务中心显式启用
    return "cron", "0 0 9 * * *", 3600, "paused"


def upgrade() -> None:
    # 1. 两张新表 ----------------------------------------------------------- #
    op.create_table(
        "scheduled_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("job_group", sa.String(length=32), nullable=False, server_default="DEFAULT"),
        sa.Column("job_key", sa.String(length=128), nullable=False),
        sa.Column("trigger_type", sa.String(length=16), nullable=False, server_default="cron"),
        sa.Column("cron_expression", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("interval_seconds", sa.Integer(), nullable=False, server_default="3600"),
        sa.Column("run_at", sa.DateTime(), nullable=True),
        sa.Column("misfire_policy", sa.String(length=16), nullable=False, server_default="once"),
        sa.Column("concurrent", sa.String(length=16), nullable=False, server_default="forbid"),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("remark", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("backoff_until", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scheduled_jobs_user_id", "scheduled_jobs", ["user_id"])
    op.create_index("ix_scheduled_jobs_job_key", "scheduled_jobs", ["job_key"])
    op.create_index("ix_scheduled_jobs_job_group", "scheduled_jobs", ["job_group"])
    op.create_index("ix_scheduled_jobs_status", "scheduled_jobs", ["status"])

    op.create_table(
        "scheduled_job_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("job_name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("job_key", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("job_group", sa.String(length=32), nullable=False, server_default="DEFAULT"),
        sa.Column("trigger_type", sa.String(length=16), nullable=False, server_default="scheduled"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("exception_info", sa.Text(), nullable=False, server_default=""),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["scheduled_jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scheduled_job_logs_job_id", "scheduled_job_logs", ["job_id"])
    op.create_index("ix_scheduled_job_logs_status", "scheduled_job_logs", ["status"])
    op.create_index("ix_scheduled_job_logs_created_at", "scheduled_job_logs", ["created_at"])

    bind = op.get_bind()

    # 2. auto_tasks 加关联列 ------------------------------------------------- #
    with op.batch_alter_table("auto_tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("schedule_job_id", sa.Integer(), nullable=True))
    op.create_index("ix_auto_tasks_schedule_job_id", "auto_tasks", ["schedule_job_id"])

    # 3. 存量 auto_tasks → job 行 -------------------------------------------- #
    # 用反射拿列（迁移要能在「四列已在/不在」两种形态下都跑通：全新库由
    # create_all 建表时已无这四列 —— models 已删，stamp 路径不执行本迁移）。
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("auto_tasks")}
    legacy_columns = {
        "schedule_type",
        "schedule_time",
        "schedule_days",
        "schedule_interval_hours",
    }
    if legacy_columns.issubset(columns):
        rows = bind.execute(
            sa.text(
                "SELECT id, user_id, name, status, schedule_type, schedule_time, "
                "schedule_days, schedule_interval_hours FROM auto_tasks"
            )
        ).mappings()
        now = _shanghai_now()
        for row in rows:
            trigger_type, cron_expression, interval_seconds, job_status = _legacy_to_trigger(
                row["schedule_type"],
                row["schedule_time"],
                row["schedule_days"],
                row["schedule_interval_hours"],
            )
            # 任务本身 active 才让调度行 active；旧任务 paused/completed ⇒ paused
            if (row["status"] or "active") != "active":
                job_status = "paused"
            params_json = json.dumps({"auto_task_id": row["id"]}, ensure_ascii=False)
            bind.execute(
                sa.text(
                    "INSERT INTO scheduled_jobs "
                    "(user_id, name, job_group, job_key, trigger_type, cron_expression, "
                    " interval_seconds, misfire_policy, concurrent, params, status, remark, "
                    " is_builtin, created_at) "
                    "VALUES (:user_id, :name, :job_group, :job_key, :trigger_type, :cron_expression, "
                    " :interval_seconds, :misfire_policy, :concurrent, :params, :status, :remark, "
                    " :is_builtin, :created_at) RETURNING id"
                ),
                {
                    "user_id": row["user_id"],
                    "name": f"自动运营 · {row['name']}",
                    "job_group": _JOB_GROUP_DEFAULT,
                    "job_key": "auto_ops.keyword_pipeline",
                    "trigger_type": trigger_type,
                    "cron_expression": cron_expression,
                    "interval_seconds": interval_seconds,
                    "misfire_policy": _MISFIRE_ONCE,
                    "concurrent": _CONCURRENT_FORBID,
                    "params": params_json,
                    "status": job_status,
                    "remark": "由 auto_tasks 调度字段迁移生成（设计 15 §4.4）",
                    "is_builtin": False,
                    "created_at": now,
                },
            ).scalar()
            #: v1.1.0 PG 化（2026-10-24）：id 直接取自 INSERT 的 `RETURNING id`。
            #: 原 `_last_insert_id()` 按方言取 `last_insert_rowid()`（SQLite）/
            #: `LAST_INSERT_ID()`（MySQL）—— 两个方言都随 SQLite 退役退场了，
            #: PG 的等价物就是 RETURNING（连接级会话状态在池化下也不可靠）。
            bind.execute(
                sa.text("UPDATE auto_tasks SET schedule_job_id = :job_id WHERE id = :task_id"),
                {"job_id": job_id, "task_id": row["id"]},
            )

    # 4. 删 5 个旧调度列 ------------------------------------------------------ #
    # `next_run_at` 一并删：下次运行时间改由 job 行维护（`scheduled_jobs.next_run_at`，
    # 展示优先取调度器活值）——留着就是同一语义两份存储。
    drop_columns = [
        "schedule_interval_hours",
        "schedule_days",
        "schedule_time",
        "schedule_type",
        "next_run_at",
    ]
    existing_after = {column["name"] for column in sa.inspect(bind).get_columns("auto_tasks")}
    droppable = [name for name in drop_columns if name in existing_after]
    if droppable:
        with op.batch_alter_table("auto_tasks", schema=None) as batch_op:
            for name in droppable:
                batch_op.drop_column(name)

    # 5. ~~播种 7 个系统 job 行~~ —— **R24（2026-09-24）本段已删除**。
    #
    #    用户口径：「将当前的定时任务配置数据定义为**应用初始化数据**，随应用的安装
    #    自动附有」⇒ 种子的唯一真相源改为**随安装包分发**的
    #    `config/initial_data/scheduled_jobs.json`（设计 15 §4.5），由应用启动时
    #    `ensure_builtin_jobs()` 据其铺设。
    #
    #    留下来的实际后果**有害**，不只是"冗余"：
    #      ① 本段播种的是**已退役**的 interval 形态 + 旧启停口径（R14 之后 `interval`
    #         已不在 `TRIGGER_TYPES` 内，只因 `h8i9j0k1l2m3` 的换算才变成 cron）；
    #      ② 运行期 `ensure_builtin_jobs()` 按 `job_key + is_builtin` **判重** ⇒ 会
    #         **跳过**这些行 ⇒ 新装库永远拿不到初始化数据里的值，**修都修不掉**
    #         （这是本段必须删、而不是"留着也无妨"的原因）。
    #
    #    原播种口径 `_SYSTEM_SEEDS` **常量保留在文件顶部**：它是当时的定义，供审计与
    #    测试对照（项目规范：结论被推翻要标注并保留原句，不静默删除）。


def downgrade() -> None:
    # 数据不还原（调度行 → 旧列的逆向换算会丢 cron 的通用性）：
    # 只把 auto_tasks 的旧列加回来（默认值）并断开关联，删除两张新表。
    with op.batch_alter_table("auto_tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("schedule_type", sa.String(length=32), nullable=False, server_default="manual")
        )
        batch_op.add_column(
            sa.Column("schedule_time", sa.String(length=32), nullable=False, server_default="09:00")
        )
        batch_op.add_column(
            sa.Column("schedule_days", sa.String(length=64), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("schedule_interval_hours", sa.Integer(), nullable=False, server_default="24")
        )
        batch_op.add_column(sa.Column("next_run_at", sa.DateTime(), nullable=True))
    op.drop_index("ix_auto_tasks_schedule_job_id", "auto_tasks")
    with op.batch_alter_table("auto_tasks", schema=None) as batch_op:
        batch_op.drop_column("schedule_job_id")
    op.drop_index("ix_scheduled_job_logs_created_at", "scheduled_job_logs")
    op.drop_index("ix_scheduled_job_logs_status", "scheduled_job_logs")
    op.drop_index("ix_scheduled_job_logs_job_id", "scheduled_job_logs")
    op.drop_table("scheduled_job_logs")
    op.drop_index("ix_scheduled_jobs_status", "scheduled_jobs")
    op.drop_index("ix_scheduled_jobs_job_group", "scheduled_jobs")
    op.drop_index("ix_scheduled_jobs_job_key", "scheduled_jobs")
    op.drop_index("ix_scheduled_jobs_user_id", "scheduled_jobs")
    op.drop_table("scheduled_jobs")
