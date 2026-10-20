"""generation_records 增加 media_json（这一版实际用了哪些图/视频）

用户口径 2026-10-18：

> 「配图没有展示，分析原因，是否未生成，生成异常还是其它原因」
> 「支持用户对生成后的结果部分位置进行重复生成，也允许用户对生成图视频等
>   选择性保留或重新生成补充等」

## 根因

`product_image` 媒体来源是**直接引用台账商品图直链**（设计 17 §13.4 一期口径），
它**不走 `AigcMediaAsset`** —— 那张表是 AI 生成图的溯源表。
于是「图确实用了、笔记也带图了」，但 `generation_records` 里**没有任何字段记录用了哪些图**
⇒ 版本详情抽屉无从展示，用户以为「配图没生成」。

## 改了什么

`generation_records` 新增 `media_json`（TEXT，默认 `"[]"`），存这一版落库时的
**最终媒体清单**：`[{url, local_path, sort_order, asset_type}]`。

* 存**最终**清单而不是入参 plan：段级失败会让实际张数少于计划；
* 与媒体来源无关：商品图 / 素材 / AI 生成 / 视频都记同一种形状。

## 为什么用 batch mode

SQLite 不支持 `ALTER TABLE ... ADD COLUMN` 携带复杂默认值/约束；
batch 会建影子表再搬迁。表达式默认值用 `server_default=sa.text("'[]'")` 写死，
让存量行回填成空数组（而不是 NULL —— 前端按数组解析，NULL 会炸）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e7f8a9b0c1d2"
down_revision = "c1d2e3f4a5b7"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    if "media_json" in _columns("generation_records"):
        return
    with op.batch_alter_table("generation_records") as batch:
        batch.add_column(
            sa.Column(
                "media_json",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )


def downgrade() -> None:
    if "media_json" not in _columns("generation_records"):
        return
    with op.batch_alter_table("generation_records") as batch:
        batch.drop_column("media_json")
