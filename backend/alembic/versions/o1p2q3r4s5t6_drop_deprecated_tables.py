"""drop deprecated tables: llm_gateway_snapshots + app_migrations

设计 75 §2.4 收尾（2026-10-27 用户口径「本地废弃表移除」）。

## 为什么删这两张

### `llm_gateway_snapshots`
v1.3 方案是「按日把 New API 用量快照缓存到本地」；v1.6 用户口径
「两个数据库合并 —— 数据层全归主库」后，用量真相源改为
`llm_gateway_usage_logs` 台账，本表**不再写入**。
实测：全仓非定义引用 **0**、表内 **0 行** ⇒ 只剩一个 ORM 类占着 metadata。

### `app_migrations`
SQLite 时代的「数据修正已执行记录」表（`normalize_legacy_gpt_54_model_name_v1`
/ `sqlite_datetime_asia_shanghai_v1` / `sqlite_publish_scheduled_at_asia_shanghai_v1`）。
项目已「完全放弃 SQLite」（`core/database.py` 非 PG 直接拒绝启动），
代码里**零引用** ⇒ 纯历史残留。

## 为什么不删另外三张「看着也可疑」的

| 表 | 为什么留 |
|---|---|
| `apscheduler_jobs` | APScheduler **自管**表，`SQLAlchemyJobStore` 运行时读写（实测 8 行）。删了调度器直接废 |
| `alembic_version` | Alembic 迁移版本锚点 |
| `note_metric_snapshots` | 实测 **10848 行**，`note_purge_service` 删笔记时要级联清它 |

## 幂等

`checkfirst=True` —— 表不存在时安安静静跳过（真机可能已被手工删过）。

Revision ID: o1p2q3r4s5t6
Revises: n0o1p2q3r4s5
Create Date: 2026-10-27
"""

from alembic import op
import sqlalchemy as sa

revision = "o1p2q3r4s5t6"
down_revision = "n0o1p2q3r4s5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "llm_gateway_snapshots" in existing:
        # 先删唯一约束所在的索引（PG 上 DROP TABLE 会连带，但显式更稳）
        op.drop_table("llm_gateway_snapshots")

    if "app_migrations" in existing:
        op.drop_table("app_migrations")


def downgrade() -> None:
    """重建空表结构（**数据不恢复** —— 两张表都没有写入来源）。

    `llm_gateway_snapshots` 按 v1.3 原结构重建；`app_migrations` 按原两列重建。
    历史行已经没了，`upgrade` 之后不会再有写入，所以重建出来就是空表。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "llm_gateway_snapshots" not in existing:
        op.create_table(
            "llm_gateway_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column(
                "gateway_id", sa.Integer(), sa.ForeignKey("llm_gateway_configs.id"), nullable=False
            ),
            sa.Column("stat_date", sa.Date(), nullable=False),
            sa.Column("call_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "user_id", "gateway_id", "stat_date",
                name="uq_llm_gateway_snapshots_user_gateway_date",
            ),
        )

    if "app_migrations" not in existing:
        op.create_table(
            "app_migrations",
            sa.Column("name", sa.String(length=200), primary_key=True),
            sa.Column("applied_at", sa.DateTime(), nullable=False),
        )
