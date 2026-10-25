"""task center two-level refactor (design 69)

任务中心两级架构（2026-10-25 用户口径）：
  「任务中心分为系统级别和工作区域级别，工作区域为每个区域中私有」
  「暂时不允许用户创建任务」「不在保存用户级的任务中心，收为公共」

## 做什么

1. `scheduled_jobs`：**按 `job_key` 去重**（403 行 → 每个 job_key 一行），
   删掉 `(job_key, user_id)` 唯一约束，建 `job_key` 全表唯一；
   留下的行 `user_id` 置 NULL（它是"最后修改人"，内置种子行没人改过），
   `workspace` 按新口径重算（`system` / `xhs`）。
2. `scheduled_job_logs`：**+1 列 `executed_for_user_id`**（这次执行跑谁的数据），
   存量行回填 = 原 `user_id`（那本来就是"为谁跑的"）。

## 为什么删 (job_key, user_id) 唯一约束

那是 DEF-066「每用户各持一份」的载体。留着它，`job_key` 就不可能全表唯一
（同一 key 在不同 user 下各一份是合法的）。

## 不去重日志

`scheduled_job_logs` 是**执行历史**，一行就是一次真实执行 —— 历史不能删。
去重只针对"定义行"。

Revision ID: a9b8c7d6e5f4
Revises: f1a2b3c4d5e6
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a9b8c7d6e5f4"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None

#: 系统级任务（设计 69 §2.1）：与接哪个平台无关，只操作本系统自己的资源。
#: 与 `job_registry.SYSTEM_WORKSPACE_JOBS` 同源 —— 写在这里是因为迁移不能
#: import 应用代码（应用代码会随版本变，迁移必须钉死在当时的判据上）。
SYSTEM_JOB_KEYS = (
    "system.queue_record_cleanup",
    "system.package_staging_cleanup",
    "system.picture_space_cleanup",
)


def _index_exists(inspector: sa.Inspector, table: str, name: str) -> bool:
    return any(ix["name"] == name for ix in inspector.get_indexes(table))


def _has_column(inspector: sa.Inspector, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspector.get_columns(table)}


def _drop_unique_if_exists(inspector: sa.Inspector, table: str, name: str) -> None:
    """删唯一**索引或约束**（原名可能由 create_all 建成索引、由旧迁移建成约束）。"""
    if _index_exists(inspector, table, name):
        op.execute(f"DROP INDEX IF EXISTS {name}")
    for con in inspector.get_unique_constraints(table):
        if con["name"] == name:
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ── ① 日志表：+1 列（先做，回填不依赖去重结果） ────────────────────────
    if not _has_column(inspector, "scheduled_job_logs", "executed_for_user_id"):
        op.add_column(
            "scheduled_job_logs",
            sa.Column("executed_for_user_id", sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            "fk_scheduled_job_logs_executed_for_user",
            "scheduled_job_logs",
            "users",
            ["executed_for_user_id"],
            ["id"],
        )
        op.create_index(
            "ix_scheduled_job_logs_executed_for_user_id",
            "scheduled_job_logs",
            ["executed_for_user_id"],
        )
    #: 存量行：原 `user_id` 就是"为谁跑的" ⇒ 直接搬过去（不造值，它是真实快照）。
    op.execute(
        "UPDATE scheduled_job_logs SET executed_for_user_id = user_id"
        " WHERE executed_for_user_id IS NULL"
    )

    # ── ② 定义表：去重到「每个 job_key 一行」 ──────────────────────────────
    #: 保留规则：**最小的 id**（最早那条）。理由：它承载着最早的 `is_builtin`
    #: 与参数默认值；后来的行是 DEF-066 克隆出来的副本。
    #: 用窗口函数一次搞定（PG 原生支持；本项目已完全放弃 SQLite）。
    op.execute(
        """
        DELETE FROM scheduled_jobs
        WHERE id NOT IN (
            SELECT MIN(id) FROM scheduled_jobs GROUP BY job_key
        )
        """
    )
    #: 留下来的行：`user_id` 不再是归属 ⇒ 置 NULL（= 内置行，无人改过）。
    op.execute("UPDATE scheduled_jobs SET user_id = NULL")

    #: `workspace` 按新口径重算（系统级 = 与平台无关的三个清理任务）。
    op.execute(
        "UPDATE scheduled_jobs SET workspace = 'xhs'"
        " WHERE job_key NOT IN ("
        + ",".join(f"'{k}'" for k in SYSTEM_JOB_KEYS)
        + ")"
    )
    op.execute(
        "UPDATE scheduled_jobs SET workspace = 'system'"
        " WHERE job_key IN ("
        + ",".join(f"'{k}'" for k in SYSTEM_JOB_KEYS)
        + ")"
    )

    # ── ③ 约束：换成 job_key 全表唯一 ────────────────────────────────────
    inspector = sa.inspect(bind)
    _drop_unique_if_exists(inspector, "scheduled_jobs", "uq_scheduled_jobs_key_user")
    for con in sa.inspect(bind).get_unique_constraints("scheduled_jobs"):
        cols = tuple(con.get("column_names") or ())
        if cols == ("job_key", "user_id"):
            op.execute(f"ALTER TABLE scheduled_jobs DROP CONSTRAINT IF EXISTS {con['name']}")
    if not _index_exists(sa.inspect(bind), "scheduled_jobs", "uq_scheduled_jobs_job_key"):
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_scheduled_jobs_job_key"
            " ON scheduled_jobs (job_key)"
        )


def downgrade() -> None:
    """回滚到「每用户一份」的形态（只恢复结构，**不重建被删的副本行**）。"""
    op.execute("DROP INDEX IF EXISTS uq_scheduled_jobs_job_key")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_scheduled_jobs_key_user"
        " ON scheduled_jobs (job_key, user_id)"
    )
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _index_exists(inspector, "scheduled_job_logs", "ix_scheduled_job_logs_executed_for_user_id"):
        op.drop_index(
            "ix_scheduled_job_logs_executed_for_user_id", table_name="scheduled_job_logs"
        )
    if _has_column(inspector, "scheduled_job_logs", "executed_for_user_id"):
        op.drop_constraint(
            "fk_scheduled_job_logs_executed_for_user", "scheduled_job_logs", type_="foreignkey"
        )
        op.drop_column("scheduled_job_logs", "executed_for_user_id")
