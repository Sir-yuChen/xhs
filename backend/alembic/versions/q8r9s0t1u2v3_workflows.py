"""自动化工作流 5 张表（设计 19，M5 方向 5）

2026-10-04（用户口径「你一个一个需求做」）：M1 收口、M2 快照表已落，进入
**M5 自动化工作流（方向 5）**。本文档为唯一开发依据：
`02_设计文档/19_功能设计_自动化工作流.md`（v1.0 定稿）。

## 为什么新建

设计 16 §6.2 只给了 4 张表的名字（`workflows` / `workflow_nodes` /
`workflow_runs` / `workflow_run_logs`）**没有字段**；设计 19 §2 补齐字段并
追加第 5 张 `workflow_scheduled_publishes`（§8.4 方案 A）。

## 五张表

| 表 | 语义 |
|---|---|
| `workflows` | 工作流主表 + DAG 邻接表（`graph`）+ 触发器**编辑态镜像** |
| `workflow_nodes` | 节点表（CASCADE 随工作流删；run 快照另有全量节点信息） |
| `workflow_runs` | 运行实例（`workflow_id` **SET NULL** 保留历史，§6.1 修正） |
| `workflow_run_logs` | 步骤日志（同 run 同节点唯一一行，重跑改状态不新增行） |
| `workflow_scheduled_publishes` | 排期表（方案 A：**不碰 `notes` 表**） |

## ⚠️ 为什么不往 `notes` 加列

`notes` 是全项目最热的表，一期/二期多个方向都在加列，改它属于**碰撞热点**。
方案 A 让工作流排期完全落在独立表，一期 `system.due_publish` 只认
`notes.scheduled_publish_at` ⇒ 天然零冲突，`notes` 零加列。

## 条件唯一索引（双库同形）

`ux_workflows_active` 是**部分唯一索引**（`status IN ('pending','running','paused')`），
保证「同一工作流同时只有一个活跃 run」（设计 19 §8.3 工作流级互斥）。
Postgres 原生支持 `postgresql_where`；SQLite 用 `sqlite_where`，两者在
SQLAlchemy 里同一套写法，迁移层不需要分支。

## 无数据迁移

5 张表全部空建（工作流**不播种内置数据** —— 设计 19 §7.4：内置工作流必然带
「账号矩阵/商品范围」这类强业务默认值，猜错就是批量发布事故）。

## 幂等

`_table_exists` 兜底：stamp 回早期 revision 再 upgrade 是正常用法，
已存在的表整段跳过（与 `c9d0e1f2a3b4` 同一风格）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "q8r9s0t1u2v3"
down_revision = "g0a1b2c3d4e5"
branch_labels = None
depends_on = None

_TABLES = (
    "workflows",
    "workflow_nodes",
    "workflow_runs",
    "workflow_run_logs",
    "workflow_scheduled_publishes",
)


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
        {"n": table},
    ).fetchone()
    return row is not None


def upgrade() -> None:
    conn = op.get_bind()

    # ------------------------------------------------------------------
    # 1. workflows（工作流主表）
    # ------------------------------------------------------------------
    if not _table_exists(conn, "workflows"):
        op.create_table(
            "workflows",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="enabled"),
            sa.Column("trigger_type", sa.String(length=16), nullable=False, server_default="manual"),
            sa.Column("cron_expression", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("run_at", sa.DateTime(), nullable=True),
            sa.Column("misfire_policy", sa.String(length=16), nullable=False, server_default="once"),
            sa.Column("graph", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("global_params", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("remark", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("last_run_at", sa.DateTime(), nullable=True),
            sa.Column("next_run_at", sa.DateTime(), nullable=True),
            sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_workflows_user_id", "workflows", ["user_id"])
        op.create_index("ix_workflows_status", "workflows", ["status"])
        op.create_index("ix_workflows_user_status", "workflows", ["user_id", "status"])

    # ------------------------------------------------------------------
    # 2. workflow_nodes（节点表）
    # ------------------------------------------------------------------
    if not _table_exists(conn, "workflow_nodes"):
        op.create_table(
            "workflow_nodes",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("workflow_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("node_type", sa.String(length=32), nullable=False),
            sa.Column("node_key", sa.String(length=64), nullable=False),
            sa.Column("node_name", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            # CASCADE：工作流删了节点一起删（run 快照里另有全量节点信息）
            sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            # 同工作流内 node_key 唯一
            sa.UniqueConstraint("workflow_id", "node_key", name="ux_workflow_nodes_key"),
        )
        op.create_index("ix_workflow_nodes_workflow_id", "workflow_nodes", ["workflow_id"])
        op.create_index("ix_workflow_nodes_user_id", "workflow_nodes", ["user_id"])

    # ------------------------------------------------------------------
    # 3. workflow_runs（运行实例）
    # ------------------------------------------------------------------
    if not _table_exists(conn, "workflow_runs"):
        op.create_table(
            "workflow_runs",
            sa.Column("id", sa.Integer(), nullable=False),
            # SET NULL 而非 CASCADE：删工作流保留历史（设计 19 §6.1 修正）
            sa.Column("workflow_id", sa.Integer(), nullable=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("workflow_name", sa.String(length=128), nullable=False),
            sa.Column("graph_snapshot", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("global_params_snapshot", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
            sa.Column("trigger_source", sa.String(length=16), nullable=False, server_default="manual"),
            sa.Column("scheduled_job_log_id", sa.Integer(), nullable=True),
            sa.Column("batch_no", sa.String(length=32), nullable=True),
            sa.Column("current_node_id", sa.Integer(), nullable=True),
            sa.Column("total_nodes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("finished_nodes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_type", sa.String(length=32), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("review_deadline", sa.DateTime(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            # 回指调度日志：工作流执行与调度执行是两条记录，互查不断链
            sa.ForeignKeyConstraint(
                ["scheduled_job_log_id"], ["scheduled_job_logs.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id"),
            # 批次号唯一（复用一期 ux_tasks_batch_no 语义）
            sa.UniqueConstraint("batch_no", name="ux_workflow_runs_batch_no"),
        )
        op.create_index("ix_workflow_runs_workflow_id", "workflow_runs", ["workflow_id"])
        op.create_index("ix_workflow_runs_user_id", "workflow_runs", ["user_id"])
        op.create_index("ix_workflow_runs_status", "workflow_runs", ["status"])
        op.create_index(
            "ix_workflow_runs_workflow_created", "workflow_runs", ["workflow_id", "created_at"]
        )
        # 部分唯一索引：同一工作流同时只允许一个活跃 run（设计 19 §8.3）。
        # Postgres 用 postgresql_where / SQLite 用 sqlite_where，同一套写法。
        op.create_index(
            "ux_workflows_active",
            "workflow_runs",
            ["workflow_id", "status"],
            unique=True,
            sqlite_where=sa.text("status IN ('pending', 'running', 'paused')"),
            postgresql_where=sa.text("status IN ('pending', 'running', 'paused')"),
        )

    # ------------------------------------------------------------------
    # 4. workflow_run_logs（步骤日志）
    # ------------------------------------------------------------------
    if not _table_exists(conn, "workflow_run_logs"):
        op.create_table(
            "workflow_run_logs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            # 快照值，可空（节点被删后历史日志靠下面四个快照列定位）
            sa.Column("node_id", sa.Integer(), nullable=True),
            sa.Column("node_key", sa.String(length=64), nullable=False),
            sa.Column("node_type", sa.String(length=32), nullable=False),
            sa.Column("node_name", sa.String(length=128), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("error_type", sa.String(length=32), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("detail", sa.JSON(), nullable=True),
            sa.Column("task_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            # 回指一期 task 行（断了也能靠 batch_no 找回）
            sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            # 同 run 同节点唯一一行：重跑改状态不新增行（否则计数错乱）
            sa.UniqueConstraint("run_id", "node_key", name="ux_workflow_run_logs_node"),
        )
        op.create_index("ix_workflow_run_logs_run_id", "workflow_run_logs", ["run_id"])
        op.create_index("ix_workflow_run_logs_user_id", "workflow_run_logs", ["user_id"])
        op.create_index("ix_workflow_run_logs_status", "workflow_run_logs", ["status"])

    # ------------------------------------------------------------------
    # 5. workflow_scheduled_publishes（排期表，设计 19 §8.4 方案 A）
    # ------------------------------------------------------------------
    if not _table_exists(conn, "workflow_scheduled_publishes"):
        op.create_table(
            "workflow_scheduled_publishes",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("note_id", sa.Integer(), nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("scheduled_at", sa.DateTime(), nullable=False),
            sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            # §9.3 发布幂等键：(run_id, note_id, account_id) 等价于
            # batch_no + note_id + account_id 且更快
            sa.UniqueConstraint(
                "run_id", "note_id", "account_id", name="ux_workflow_scheduled_publishes"
            ),
        )
        op.create_index(
            "ix_workflow_scheduled_publishes_run_id", "workflow_scheduled_publishes", ["run_id"]
        )
        op.create_index(
            "ix_workflow_scheduled_publishes_user_id", "workflow_scheduled_publishes", ["user_id"]
        )


def downgrade() -> None:
    conn = op.get_bind()
    for table in reversed(_TABLES):
        if not _table_exists(conn, table):
            continue
        # 索引随表一起删（SQLite / Postgres 一致）；显式 drop 仅为日志可读
        op.drop_table(table)
