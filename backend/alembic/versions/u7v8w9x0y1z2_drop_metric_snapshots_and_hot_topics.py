"""drop note_metric_snapshots + hot_topics

设计 76 §3.1 / §3.2 处置（2026-10-28）。两张表都是「写入链路已死、数据无人读」的空壳。

| 表 | 原用途 | 为什么删 |
|---|---|---|
| `note_metric_snapshots` | 笔记指标日快照（做趋势线） | 0 行。写入链路 2026-10-13 整块移除（设计 39），唯一残留引用是 `note_purge_service` 里那行空 delete |
| `hot_topics` | 平台按商品推荐的热度词快照 | 33 行但只进不出。4 条 `platform-hot-topics` 端点前端零调用；唯一「像在读它」的 `GET /analytics/hot-topics` 读的是 `note_topics` + `notes.raw_json` |

⚠️ 热度词功能本身没删：`product_note_gen_service._build_aigc_context()` 仍在同一次
`aigc-publish/attributes` 响应里现取现用（喂给生成的 `context.topic`），只是不落库。

Revision ID: u7v8w9x0y1z2
Revises: t6u7v8w9x0y1
"""

from alembic import op
import sqlalchemy as sa

revision = "u7v8w9x0y1z2"
down_revision = "t6u7v8w9x0y1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    #: `hot_topics` 先删 —— 它带 users / platform_accounts 两条外键。
    if "hot_topics" in existing:
        op.drop_table("hot_topics")

    #: `note_metric_snapshots` 的 note_pk FK → notes.id，无 CASCADE，DROP 表本身不受影响。
    if "note_metric_snapshots" in existing:
        op.drop_table("note_metric_snapshots")


def downgrade() -> None:
    """重建空表结构（历史行不恢复 —— 两张表都没有写入来源）。"""
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "note_metric_snapshots" not in existing:
        op.create_table(
            "note_metric_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("note_pk", sa.Integer(), sa.ForeignKey("notes.id"), nullable=False),
            sa.Column("stat_date", sa.String(length=10), nullable=False),
            sa.Column("likes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("collects", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("comments", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("shares", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("views", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("engagement", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("note_pk", "stat_date", name="uq_note_metric_note_date"),
        )
        op.create_index("ix_note_metric_snapshots_user_id", "note_metric_snapshots", ["user_id"])
        op.create_index("ix_note_metric_snapshots_note_pk", "note_metric_snapshots", ["note_pk"])
        op.create_index(
            "ix_note_metric_snapshots_stat_date", "note_metric_snapshots", ["stat_date"]
        )

    if "hot_topics" not in existing:
        op.create_table(
            "hot_topics",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("item_id", sa.String(length=64), nullable=False),
            sa.Column("item_title", sa.String(length=512), nullable=False, server_default=""),
            sa.Column("topic", sa.String(length=128), nullable=False),
            sa.Column("topic_type", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("hot_value", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "platform_account_id",
                sa.Integer(),
                sa.ForeignKey("platform_accounts.id"),
                nullable=True,
            ),
            sa.Column("stat_date", sa.String(length=10), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "user_id", "item_id", "topic", "stat_date", name="uq_hot_topic_item_topic_date"
            ),
        )
        for col in ("user_id", "item_id", "topic", "stat_date", "platform_account_id"):
            op.create_index("ix_hot_topics_%s" % col, "hot_topics", [col])
