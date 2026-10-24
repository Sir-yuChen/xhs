"""按前端入口拆分业务点 key（旧 key 的配置搬到新 key）

用户口径 2026-10-18：

> 「模型配置**以前端入口为父对象**，比如当前的 LLM 生成笔记功能应配置三种类型的模型，
>   触发源移除」
>
> 「所有业务功能**不在共用配置**，改为**各个功能单独配置**」

## 拆了什么

改造前是「一个 key 服务多个入口」（`generate_image` 同时被笔记工坊的「生成配图」和
「草稿-生成配图」用；`generate_note` 同时被工坊草稿与商品列表用）—— 改一处影响两处。

改造后**每个入口一组独立 key**（见 `business_provider_keys.LEGACY_KEY_ALIASES`）：

| 旧 key | 新 key（每个入口一份） |
|---|---|
| `generate_note` | `llm_note_text`、`workshop_draft_text` |
| `generate_image` | `llm_note_image`、`workshop_image`、`workshop_image_gen` |
| `generate_video` | `llm_note_video` |
| `rewrite_note` | `workshop_rewrite_text`、`auto_ops_text` |
| `generate_title` | `workshop_title_text` |
| `describe_image` | `workshop_image_understand` |
| `polish_product_title` | `product_title_polish` |
| `generate_selling_point` | `product_selling_point` |
| `generate_product_detail_image` | `product_detail_image` |

## 搬迁语义（**不丢配置**）

对每个旧行，按别名表**复制**成 N 条新行（`(user, platform, business_key, kind)` 唯一），
`provider_key` / `model_name` / `enabled` 原样带过去 —— 用户升级后各入口**开箱即用**，
之后可以各改各的。

⚠️ **旧行不删**：它是回退依据（downgrade 要搬回来）。旧 key 已不在
`BUSINESS_KEYS` 里 ⇒ 解析不到（`get_business_key` 返回 None），对业务无影响；
它们只作为历史快照留着。想彻底清掉可另跑清理（本迁移不做破坏性删除）。

⚠️ 新 key 的 `kind` 取**新业务点自己的 kind**（不是旧行的 kind）—— 两者在本次拆分里
完全相同（文本→文本、图片→图片、视频→视频），但代码上以新定义为准，避免将来
某入口改了能力类型时迁移把行填错。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b5c6d7e8f9a0"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in inspector.get_table_names()


def upgrade() -> None:
    if not _table_exists("llm_business_providers"):
        return

    from backend.app.core.business_provider_keys import (
        LEGACY_KEY_ALIASES,
        get_business_key,
    )

    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, user_id, platform, business_key, provider_key, model_name, "
            "enabled, kind FROM llm_business_providers"
        )
    ).fetchall()

    inserted = 0
    for row in rows:
        old_key = str(row[3] or "")
        targets = LEGACY_KEY_ALIASES.get(old_key)
        if not targets:
            continue
        for new_key in targets:
            if new_key == old_key:
                continue  #: 同名沿用（无入口 / 仅 API 的 key）⇒ 不用搬
            spec = get_business_key(new_key)
            if spec is None:
                continue
            #: 已有同键行 ⇒ 跳过（幂等：重复跑升级不会重复插）
            exists = conn.execute(
                sa.text(
                    "SELECT 1 FROM llm_business_providers WHERE user_id = :u "
                    "AND platform = :p AND business_key = :b AND kind = :k"
                ),
                {"u": row[1], "p": row[2], "b": new_key, "k": spec.kind},
            ).first()
            if exists:
                continue
            conn.execute(
                sa.text(
                    "INSERT INTO llm_business_providers "
                    "(user_id, platform, business_key, kind, provider_key, model_name, "
                    " enabled, created_at, updated_at) "
                    "VALUES (:u, :p, :b, :k, :pk, :mn, :en, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "u": row[1],
                    "p": row[2],
                    "b": new_key,
                    "k": spec.kind,
                    "pk": row[4] or "",
                    "mn": row[5] or "",
                    "en": bool(row[6]),
                },
            )
            inserted += 1

    print(f"[split business keys] copied {inserted} rows")


def downgrade() -> None:
    if not _table_exists("llm_business_providers"):
        return
    #: **有损回退**：新 key 行删掉，旧 key 行原样还在（升级时没删它们）⇒ 回到改造前。
    from backend.app.core.business_provider_keys import LEGACY_KEY_ALIASES

    new_keys = set()
    for targets in LEGACY_KEY_ALIASES.values():
        new_keys.update(targets)

    conn = op.get_bind()
    for key in sorted(new_keys):
        conn.execute(
            sa.text("DELETE FROM llm_business_providers WHERE business_key = :b"),
            {"b": key},
        )
