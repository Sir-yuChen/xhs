"""服务商层删除 default_model 死字段（设计 17 §13.2.10，v2.8）

2026-10-02 用户口径（原文）：**「不指定默认模型，该字段移除」**。

## 为什么它是死字段

`llm_gateway_configs.default_model` 自 **v2.3** 起已无任何读点：

- 业务点映射（`llm_business_providers.model_name`）**必须显式选模型**，
  `resolve_business_provider` **刻意不回落** `default_model`（回落 = 用户配错时
  「看着正常、实际跑另一个模型」的静默错误，DEF-048 同型事故形态）；
- v2.7（§13.2.9）类型/平台下沉到模型层后，模型真相源 = `llm_gateway_models` 模型行；
- 唯一残留读点是「测试连通性」端点，v2.8 已改用**模型行的第一条 text 模型**。

⇒ 保留列 = 「UI 看不到但库里有」的僵尸字段，后来者会误以为它生效。
v2.7 迁移已把它能提供的信息全部**回填**进模型层，本迁移只需**安全删列**。

## 动作

| 动作 | 对象 | 说明 |
|---|---|---|
| **补漏** | `llm_gateway_models` | 若某服务商的 `default_model` 尚未成为模型行 ⇒ 补一条，**数据不丢** |
| **删列** | `llm_gateway_configs.default_model` | 死字段下线 |

## 补漏规则（用户 2026-10-02 选择「删列 + 用迁移补齐模型行」）

`default_model` 非空、且该 `(user_id, provider_key, default_model)` **尚无模型行**时：

- `model_type` = **该服务商既有模型行的类型**（多数情况只有一种，取其众数；取不到 ⇒ `text`）；
- `platform` = **该服务商既有模型行的平台**（同上；取不到 ⇒ `xhs`）；
- 理由：v2.7 迁移已按同一规则回填过一次；这里是**二次兜底**（防 v2.7 之后
  新建的服务商又填了 `default_model` 但没建模型行）。**不臆造类型** ——
  取已有行是对该用户已表态的尊重，`text` 是 §13.2.1a 定的安全默认。

## ⚠️ 为什么「取既有模型行」而不是「取服务商旧 model_type」

服务商层的 `model_type` / `platform` 两列**已被 v2.7 迁移删除**（`e5f6a7b8c9d0`），
本迁移读不到它们。故只能从模型层反查 —— 这也正好符合「模型真相源 = 模型行」的既定口径。

## 幂等（本项目硬纪律，设计 03 §9.5.1）

SQLite DDL 不在事务里。故：补漏用 `INSERT ... WHERE NOT EXISTS`（重跑不产生重复行）、
删列判 `PRAGMA table_info`。**可整体重跑。**

## 可逆

`downgrade()` 建回列（`String(128)` NOT NULL default `''`）⇒
从模型层**取每个服务商的第一条模型行**回填名 ⇒ 服务商无模型行时留空串
（v1.9 曾要求必填，但降级态由旧代码自行校验，**不替它编造模型名**）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None

_TABLE_MODELS = "llm_gateway_models"
_TABLE_CONFIGS = "llm_gateway_configs"


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
        {"n": table},
    ).fetchone()
    return row is not None


def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)


def upgrade() -> None:
    conn = op.get_bind()

    # ------------------- ① 补漏：default_model 尚未成模型行 ⇒ 补一条 #
    # ⚠️ 必须在删列之前执行（删了就读不到了）。
    # 类型/平台取「该服务商既有模型行」的众数（子查询 LIMIT 1 按出现次数降序）——
    # 取不到 ⇒ text / xhs（§13.2.1a 安全默认）。**不猜**。
    if _column_exists(conn, _TABLE_CONFIGS, "default_model") and _table_exists(
        conn, _TABLE_MODELS
    ):
        conn.execute(
            sa.text(
                f"""
                INSERT INTO {_TABLE_MODELS}
                    (user_id, provider_key, model_name, model_type, platform,
                     enabled, remark, created_at, updated_at)
                SELECT
                    c.user_id,
                    c.provider_key,
                    c.default_model,
                    COALESCE(
                        (
                            SELECT m.model_type FROM {_TABLE_MODELS} AS m
                            WHERE m.user_id = c.user_id
                              AND m.provider_key = c.provider_key
                            GROUP BY m.model_type
                            ORDER BY COUNT(*) DESC, m.model_type ASC
                            LIMIT 1
                        ),
                        'text'
                    ),
                    COALESCE(
                        (
                            SELECT m.platform FROM {_TABLE_MODELS} AS m
                            WHERE m.user_id = c.user_id
                              AND m.provider_key = c.provider_key
                            GROUP BY m.platform
                            ORDER BY COUNT(*) DESC, m.platform ASC
                            LIMIT 1
                        ),
                        'xhs'
                    ),
                    1, '', datetime('now'), datetime('now')
                FROM {_TABLE_CONFIGS} AS c
                WHERE c.default_model IS NOT NULL
                  AND TRIM(c.default_model) <> ''
                  AND c.provider_key <> ''
                  AND NOT EXISTS (
                      SELECT 1 FROM {_TABLE_MODELS} AS m
                      WHERE m.user_id = c.user_id
                        AND m.provider_key = c.provider_key
                        AND m.model_name = c.default_model
                  )
                """
            )
        )

    # ------------------------------------ ② 删死列 #
    if _column_exists(conn, _TABLE_CONFIGS, "default_model"):
        with op.batch_alter_table(_TABLE_CONFIGS, schema=None) as batch_op:
            batch_op.drop_column("default_model")


def downgrade() -> None:
    conn = op.get_bind()

    # ------------------------------------ ① 建回列 #
    if not _column_exists(conn, _TABLE_CONFIGS, "default_model"):
        with op.batch_alter_table(_TABLE_CONFIGS, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "default_model",
                    sa.String(length=128),
                    nullable=False,
                    server_default="",
                )
            )

    # ------------------------- ② 从模型层取首条模型行回填 #
    # 取不到 ⇒ 留空串（**不替旧代码编造模型名**；v1.9 的必填校验由旧代码自行兜）。
    if _table_exists(conn, _TABLE_MODELS):
        conn.execute(
            sa.text(
                f"""
                UPDATE {_TABLE_CONFIGS}
                SET default_model = COALESCE(
                        (
                            SELECT m.model_name FROM {_TABLE_MODELS} AS m
                            WHERE m.user_id = {_TABLE_CONFIGS}.user_id
                              AND m.provider_key = {_TABLE_CONFIGS}.provider_key
                            ORDER BY m.id
                            LIMIT 1
                        ),
                        ''
                    )
                """
            )
        )
