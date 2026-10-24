"""llm_business_providers 增加 kind（一个业务点可按类型配多个模型）

用户口径 2026-10-18：

> 「一个业务点支持用户配置**多个类型**模型，**每个类型只能配置一个**，比如，
>   LLM生成笔记是一个业务点，该业务点逻辑功能需要使用大模型生成文本，也需要大模型
>   生成视频或图片，所以该业务点必须配置一个文本模型，一个图片模型，一个视频模型，
>   用户可以手动添加不同类型的模型，但是**相同类型的模型不允许重复添加**，
>   同一个类型的模型支持切换」

## 改了什么

| 项 | 改造前 | 改造后 |
|---|---|---|
| 唯一键 | `(user_id, platform, business_key)` | `(user_id, platform, business_key, kind)` |
| 一个业务点 | **只能一行** | **每类型一行**（text / image / video 各一） |
| 新增 `kind` 列 | — | `String(16)`，默认 `text` |

## 存量回填（**不许填错**）

存量行没有 `kind` 概念，按**该校验业务点自身的 kind** 回填：

* `generate_note` → `text`（它原本就是文本业务点）
* `generate_image` → `image`
* `generate_video` → `video`

🔴 **不能一律填 `text`**：那会让所有图像 / 视频业务点的老配置在解析时匹配不上
（`resolve_business_provider` 按 `kind` 取行）⇒ 升级后全部变成「未配置」，
是最不可接受的回归。

⚠️ 业务点 kind 的真相源是 `services/business_provider_keys.BUSINESS_KEYS`；
本迁移**不在迁移里抄一份映射表**（抄了就会随代码漂移），而是 import 常量取值。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f9a0b1c2d3e4"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    #: ⚠️ **幂等分两段**：列已存在 ⇒ 只跳过 ，**回填与唯一键仍要跑**
    #: （首次执行可能在回填前失败，此时列已加好但全为 NULL；
    #:  直接  会让 286 行永远没有 kind ⇒ 解析全部「未配置」）。
    has_kind = "kind" in _columns("llm_business_providers")
    if not has_kind:
        with op.batch_alter_table("llm_business_providers") as batch:
            batch.add_column(sa.Column("kind", sa.String(16), nullable=True))

    #: **按业务点自身 kind 回填**（真相源 = 代码常量，不在迁移里抄映射）
    from backend.app.core.business_provider_keys import BUSINESS_KEYS

    conn = op.get_bind()
    for spec in BUSINESS_KEYS:
        conn.execute(
            sa.text(
                "UPDATE llm_business_providers SET kind = :kind "
                "WHERE business_key = :key AND (kind IS NULL OR kind = '')"
            ),
            {"kind": spec.kind, "key": spec.key},
        )
    #: 兜底：仍为空的（业务点已从常量里删除的孤儿行）一律 text ——
    #: 这些行本来也解析不到（业务点不存在 ⇒ 未配置），填什么都不会误伤。
    conn.execute(
        sa.text(
            "UPDATE llm_business_providers SET kind = 'text' "
            "WHERE kind IS NULL OR kind = ''"
        )
    )

    #: 改成 NOT NULL + 换唯一键（SQLite 不支持直接改约束 ⇒ batch 重建表）。
    #: ⚠️ 这里唯一键在库里是**唯一索引**（`CREATE UNIQUE INDEX`），不是表级
    #: CONSTRAINT —— `drop_constraint` 会报 `No such constraint`。先 DROP INDEX 再建。
    conn.execute(
        sa.text("DROP INDEX IF EXISTS uq_llm_business_providers_user_platform_business_key")
    )
    with op.batch_alter_table("llm_business_providers") as batch:
        batch.alter_column(
            "kind", existing_type=sa.String(16), nullable=False, server_default="text"
        )
    conn.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_llm_business_providers_user_platform_business_key_kind "
            "ON llm_business_providers (user_id, platform, business_key, kind)"
        )
    )


def downgrade() -> None:
    if "kind" not in _columns("llm_business_providers"):
        return
    #: 回退要注意：多行（同业务点不同类型）在旧唯一键下会冲突。
    #: 只保留「业务点自身 kind」那一行，其余删除 —— 这是有损回退，
    #: 但旧结构**物理上**无法表达多行，删是最小代价（并在日志里说明）。
    from backend.app.core.business_provider_keys import BUSINESS_KEYS

    conn = op.get_bind()
    own = {spec.key: spec.kind for spec in BUSINESS_KEYS}
    rows = conn.execute(
        sa.text("SELECT id, business_key, kind FROM llm_business_providers")
    ).fetchall()
    keep: set[int] = set()
    for row_id, business_key, kind in rows:
        if own.get(business_key) == kind:
            keep.add(int(row_id))
    for row_id, business_key, kind in rows:
        if int(row_id) not in keep:
            conn.execute(
                sa.text("DELETE FROM llm_business_providers WHERE id = :id"),
                {"id": int(row_id)},
            )

    conn.execute(
        sa.text(
            "DROP INDEX IF EXISTS uq_llm_business_providers_user_platform_business_key_kind"
        )
    )
    with op.batch_alter_table("llm_business_providers") as batch:
        batch.drop_column("kind")
    conn.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_llm_business_providers_user_platform_business_key "
            "ON llm_business_providers (user_id, platform, business_key)"
        )
    )
