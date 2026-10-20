"""add note_form for image / video notes

设计 41 §15（2026-10-16）：小红书笔记在平台侧**只有两大类且互斥** ——
「图文笔记」（1–18 张图）与「视频笔记」（1 个视频 + 独立封面）。平台官方契约
（`xhs.postNote` 的 `mediaInfo`）：`image_resources` / `video_resources`
**只能二选一，多传触发校验失败**；我们自己的 SDK 也是两套 payload
（`get_post_note_image_data` 的 `type="normal"` vs
`get_post_note_video_data` 的 `type="video"`）。

⚠️ **与既有两列正交，不许顺手改它们**（沿用设计 33 §4 的纪律）：
* `note_type`  = 落库来源（`synced` / `local` / `local_generated`）
* `note_category`（已退役，见设计 35）
* **`note_form`（本列）** = 平台形态（`image` / `video`）

存量行一律回填 `image`：本项目历史上只成功发布过图文笔记（视频发布链路长期
未接通，`NOTE_VIDEO_UNSUPPORTED`），且平台图文占绝大多数 —— 回填 `image` 是
**与事实一致**的默认，不是猜测。

Revision ID: b3e4f5a6c7d8
Revises: a1d2p3r4u5l6
"""

from alembic import op
import sqlalchemy as sa

revision = "b3e4f5a6c7d8"
down_revision = "a1d2p3r4u5l6"
branch_labels = None
depends_on = None

#: 默认值 = 图文（存量行与未指明的写入都按图文，见模块 docstring）。
DEFAULT_FORM = "image"


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}


def upgrade() -> None:
    if "note_form" not in _columns("notes"):
        op.add_column(
            "notes",
            sa.Column("note_form", sa.String(16), nullable=False, server_default=DEFAULT_FORM),
        )
        #: 列表按形态筛选（内容库「图文 / 视频」）。
        op.create_index("ix_notes_form", "notes", ["note_form"])


def downgrade() -> None:
    if "note_form" in _columns("notes"):
        op.drop_index("ix_notes_form", table_name="notes")
        op.drop_column("notes", "note_form")
