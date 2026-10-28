"""merge the 3 orphan revision heads into the main chain

Revision ID: f1a2b3c4d5e7
Revises: a5b6c7d8e9f1, c1d2e3f4a5b7, g0a1b2c3d4e5, f4a5b6c7d8e9

2026-10-28 修：版本图里有 **3 个孤立 head**，与主链完全不相交 ⇒ 任何
`alembic upgrade head` 都报 `Cycle is detected in revisions (...)`（alembic 把
「多条根不相交的链」误判成环），导致所有迁移都无法执行。

三条孤立链的来历（都是历史遗留，与本次合表无关）：

| 孤立 head | 内容 | 状态 |
|---|---|---|
| `c1d2e3f4a5b7` | note_generation 版本历史 | 真库未落地（表不存在） |
| `g0a1b2c3d4e5` | product_groups（商品分组） | 真库未落地（表已删） |
| `f4a5b6c7d8e9` | add_note_category | 早已被设计 35 退役 |

本迁移只做**版本图收口**：不执行任何 DDL —— 三张表在真库里本就不存在
（实测 `information_schema` 零命中），它们代表的是被放弃的分支。把它们声明为
主链的祖先，`upgrade head` 即恢复唯一确定性。
"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = "f1a2b3c4d5e7"
down_revision: Union[str, Sequence[str], None] = (
    "a5b6c7d8e9f1",
    "c1d2e3f4a5b7",
    "g0a1b2c3d4e5",
    "f4a5b6c7d8e9",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """版本图收口，无 DDL。"""


def downgrade() -> None:
    """反向无 DDL（拆回多 head 只会让 upgrade 再次失败，不提供）。"""
