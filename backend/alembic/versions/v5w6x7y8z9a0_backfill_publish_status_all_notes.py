"""backfill notes.publish_status as the single publish-state field

2026-09-23 上机验收修正（设计 13 §32.3，用户口径原文）：

> 「笔记内容库中笔记状态筛选条件新增发布失败状态，后端数据库表**应是一个字段**，
> 发布状态字段（**待发布，已发布，发布失败**），发布失败并记录失败原因」

## 语义变更（取代 §31.3.1 的「仅本地生成笔记有意义」）

`publish_status` 从「只有 `local_generated` 笔记有值、其余恒 `''`」升为
**所有笔记的发布状态**：`pending` / `publishing` / `published` / `failed`。
`published`（布尔）降级为它的**投影**，写入统一经
`models/note.py::set_note_publish_status()`。

## 回填规则（幂等；纯数据、不改结构）

| 条件 | 结果 |
|---|---|
| `publish_status` 空 且 `published = 1` | `published` |
| `publish_status` 空 且 `published = 0` | `pending` |
| `publish_status = 'publishing'`（发布中断的僵尸态） | `failed` + 原因「发布中断（发布过程中进程退出）」（**不清空**已有原因） |
| 其余（本地生成笔记的 pending/published/failed） | **原样保留** —— 那是链路写下的真值，不重算 |

⚠️ 回填依据是 `published` 列（§19.12 的不变量：同步链路落库 ⇒ True），**不是猜**。
判据与 `published` 的历史语义严格同源，故回填后 `publish_status = 'published'`
的行集合 ≡ 原 `published = 1` 的行集合。

## 可逆

`downgrade()` 把**非本地生成笔记**的状态清回 `''`（旧口径形态）；`published` 列
一路上不受影响，因此降级不丢事实。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "v5w6x7y8z9a0"
down_revision = "u4v5w6x7y8z9"
branch_labels = None
depends_on = None

_TABLE = "notes"
#: 发布中断的僵尸态原因（迁移里直接写死：它描述的是**当时**发生的事，不是平台原文）。
_INTERRUPTED_REASON = "发布中断（发布过程中进程退出）"


def upgrade() -> None:
    conn = op.get_bind()
    # 1) 已发布过的（同步链路落库）→ published
    conn.execute(
        sa.text(
            f"UPDATE {_TABLE} SET publish_status = 'published' "
            "WHERE (publish_status IS NULL OR publish_status = '') AND published = 1"
        )
    )
    # 2) 其余（采集 / 本地保存，未发布）→ pending
    conn.execute(
        sa.text(
            f"UPDATE {_TABLE} SET publish_status = 'pending' "
            "WHERE (publish_status IS NULL OR publish_status = '') "
            "AND (published = 0 OR published IS NULL)"
        )
    )
    # 3) 卡在「发布中」的僵尸行 → failed（进程重启后它不会再自己往前走）
    conn.execute(
        sa.text(
            f"UPDATE {_TABLE} SET publish_status = 'failed', "
            "publish_error = CASE WHEN publish_error IS NULL OR publish_error = '' "
            f"THEN '{_INTERRUPTED_REASON}' ELSE publish_error END "
            "WHERE publish_status = 'publishing'"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    # 回到「只有本地生成笔记有状态」的旧形态；published 列不动 ⇒ 不丢事实。
    conn.execute(
        sa.text(
            f"UPDATE {_TABLE} SET publish_status = '' "
            "WHERE note_type IS NULL OR note_type <> 'local_generated'"
        )
    )
