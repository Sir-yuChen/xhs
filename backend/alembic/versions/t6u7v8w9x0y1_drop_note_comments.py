"""drop note_comments（方案 B，2026-10-28 用户拍板「整条退场」）

## 本迁移 DROP 1 张表

| 表 | 原用途 | 为什么删 |
|---|---|---|
| `note_comments` | 平台笔记评论的**本地镜像**，供「评论洞察 / 有评论筛选 / 已存评论计数」 | **写入口从未打通**，实测 0 行 |

## 为什么是「删表」而不是「补写入口」

表的 3 个读点都是真 UI（不是死代码）：

1. 内容库「有评论」勾选框 → `GET /api/notes?has_comments=`
2. 概览卡「已存评论」→ `GET /api/xhs/analytics/overview` 的 `comment_count`
3. 内容洞察「评论关键词 / 高赞评论」→ `GET /api/xhs/analytics/comment-insights`

但**两条写入口都是断的**：

* `POST /notes/batch-save` 要求 `fetch_comments=true`，而唯一前端调用方
  `saveXhsNotesToLibrary` 的入参只有 `{account_id, notes}` —— 永远 false；
* 采集链路 `/xhs/crawl/data` 的 `fetch_comments` **抓得到但不落库**：
  评论只塞进 SSE 回给前端看，`_save_normalized_notes` 不写 `NoteComment`。

⇒ 三个读点全部恒为空。用户口径：「方案 B —— 整条退场」。

## ⚠️ 评论功能本身没有删除

「我的笔记」/「笔记发现」页的评论区**一直是直查平台**：

    POST /api/xhs/pc/notes/comments → adapter.get_note_comments()
        → normalize_comment_payload() → 直接返回前端（**不落库**）

本迁移只删「本地镜像」这条从未打通的支线，不动实时评论链路
（`pc.py` 的 `note_comments` 端点、`NoteCommentList` 组件、`NoteComment` 前端类型
全部保留 —— 它们服务的是平台实时数据，不是这张表）。

## 表已空

真机实测 `count(*) = 0`（2026-10-28）⇒ DROP 不丢任何用户数据，
`downgrade` 只重建空表结构。这也意味着**不需要数据备份**；
但为了可回滚，`downgrade` 仍按原 DDL 重建（含 2 个索引 + 1 个外键）。

Revision ID: t6u7v8w9x0y1
Revises: r4s5t6u7v8w9
Create Date: 2026-10-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "t6u7v8w9x0y1"
down_revision = "r4s5t6u7v8w9"
branch_labels = None
depends_on = None

_TABLE = "note_comments"


def upgrade() -> None:
    """DROP 表（幂等：存量库可能已被手工清过）。

    ⚠️ PG 上 `DROP TABLE` 会连带删掉它的索引与外键，不需要逐条 `drop_index` /
    `drop_constraint` —— 先删索引反而会因为「依赖对象已被删」报错。
    """
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if _TABLE in existing:
        op.drop_table(_TABLE)


def downgrade() -> None:
    """按原 DDL 重建空表（**数据不恢复** —— 它本来就是 0 行）。

    列与索引逐条对齐 `n1o2p3q4r5s6`/`c3d4e5f6a7b8` 时代的最终形态（含
    2026-10-12 ADR-021 加的 `platform_account_id`）。
    """
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if _TABLE in existing:
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("note_id", sa.Integer(), sa.ForeignKey("notes.id"), nullable=False),
        sa.Column("comment_id", sa.String(128), nullable=False),
        sa.Column("user_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("like_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parent_comment_id", sa.String(128), nullable=True),
        sa.Column("created_at_remote", sa.String(64), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        #: ⚠️ **刻意不加外键**（ADR-021）：账号删了，历史评论仍要能回答
        #: 「当时属于哪个账号」。同 `notes.platform_account_id` 的口径。
        sa.Column("platform_account_id", sa.Integer(), nullable=True),
    )
    op.create_index("ix_note_comments_note_id", _TABLE, ["note_id"])
    op.create_index("ix_note_comments_comment_id", _TABLE, ["comment_id"])
