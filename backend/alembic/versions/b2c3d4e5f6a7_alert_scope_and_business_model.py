"""预警规则三级作用域 + 业务入口强制选模型（设计 17 §4.5 / §13.2，v2.3）

2026-10-02 用户口径两条：
1. 「预警规则挂可以控制到具体模型维度，也可以服务商维度或全网关」
2. 「其它工作区域业务模块根据具体配置的业务入口随时切换不同的服务商和服务商模型
   （两者都应是必须选择，不指定具体的模型无法调用成功）」

| 动作 | 对象 | 说明 |
|---|---|---|
| 加列 | `llm_gateway_alerts.model_name` | `String(128)` NOT NULL default `""`（`""` = 该服务商全模型） |
| 放宽 | `llm_gateway_alerts.gateway_id` | NOT NULL → **可空**（`NULL` = 全网关作用域） |
| 加列 | `llm_business_providers.model_name` | `String(128)` NOT NULL default `""`；**存量行回填**为该服务商当时的 `default_model` |

## 设计要点（迁移必须自己说清「为什么这样改」）

* **`gateway_id` 改可空是「全网关」的表达**：刻意不用哨兵值（`0` 之类）—— 哨兵在
  `db.get()` / JOIN 上语义不明，且一眼看不出是魔法值。放宽（NOT NULL → NULL）
  对存量数据**零影响**（原值全部保留）。
* **`model_name` 用 `""` 而非 NULL 表达「全模型」**：与 `platform`/`function` 的空串
  口径一致（同表同语义同形态），避免「同一件事两种空值」。
* **业务入口 `model_name` 存量回填**：v2.3 前 `resolve_business_provider` 用的是
  服务商 `default_model`（静默兜底）。改成强制显式后，若存量行留空，**所有业务调用
  会立即失败**。故回填为「该映射指向的服务商当前的 `default_model`」—— 这是
  **保持行为不变**的最小迁移（回填前它在用 default_model，回填后它仍在用同一个模型，
  只是从「隐式」变「显式」）。此后新建/修改映射必须显式指定模型，
  `resolve_business_provider` 不再回落。

## 幂等（本项目硬纪律，设计 03 §9.5.1）

SQLite DDL 不在事务里，中途失败会留「改了一半 + 版本没推进」。故加列 / 放宽约束
**各自判现状**（列已存在 / 已可空则跳过），可整体重跑。

## 可逆

`downgrade()` 逆序：删 `llm_business_providers.model_name`、删
`llm_gateway_alerts.model_name`、把 `gateway_id` 收回 NOT NULL。
⚠️ 收回 NOT NULL 前必须把 `gateway_id IS NULL` 的「全网关」行**删除**
（否则该列有 NULL 值，改 NOT NULL 会失败）—— 这是有损的，但降级意味着放弃
「全网关」这个能力本身，删除这些行是唯一自洽的选择。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "9b8c7d6e5f4a"
branch_labels = None
depends_on = None

_TABLE_ALERTS = "llm_gateway_alerts"
_TABLE_BUSINESS = "llm_business_providers"


def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)


def _column_notnull(conn, table: str, column: str) -> bool | None:
    """返回该列的 NOT NULL 标志（1/0）；列不存在 ⇒ None。"""
    rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    for r in rows:
        if r[1] == column:
            return bool(r[3])
    return None


def upgrade() -> None:
    conn = op.get_bind()

    # --------------------------------- ① 预警规则：加 model_name #
    if not _column_exists(conn, _TABLE_ALERTS, "model_name"):
        with op.batch_alter_table(_TABLE_ALERTS, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "model_name",
                    sa.String(length=128),
                    nullable=False,
                    server_default="",
                )
            )

    # ------------------------- ② 预警规则：gateway_id 放宽为可空 #
    if _column_notnull(conn, _TABLE_ALERTS, "gateway_id"):
        with op.batch_alter_table(_TABLE_ALERTS, schema=None) as batch_op:
            batch_op.alter_column(
                "gateway_id",
                existing_type=sa.Integer(),
                nullable=True,
            )

    # ------------------------ ③ 业务入口映射：加 model_name #
    if not _column_exists(conn, _TABLE_BUSINESS, "model_name"):
        with op.batch_alter_table(_TABLE_BUSINESS, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "model_name",
                    sa.String(length=128),
                    nullable=False,
                    server_default="",
                )
            )

    # ------------------ ④ 业务入口映射：存量行回填 default_model #
    # 保持行为不变：v2.3 前用服务商 default_model（隐式），回填后显式记同一个模型。
    # 只回填「映射已指向某服务商」但 model_name 仍空的行；服务商不存在（裸列引用）
    # 或 default_model 也为空的行保持空 —— 让它显式失败（去页面重选），
    # 而不是编一个模型名出来（同「缺值不造假」纪律）。
    conn.execute(
        sa.text(
            f"""
            UPDATE {_TABLE_BUSINESS}
            SET model_name = COALESCE(
                (
                    SELECT c.default_model
                    FROM llm_gateway_configs AS c
                    WHERE c.user_id = {_TABLE_BUSINESS}.user_id
                      AND c.provider_key = {_TABLE_BUSINESS}.provider_key
                ),
                ''
            )
            WHERE model_name = ''
              AND provider_key <> ''
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()

    # ① 删业务入口 model_name
    if _column_exists(conn, _TABLE_BUSINESS, "model_name"):
        with op.batch_alter_table(_TABLE_BUSINESS, schema=None) as batch_op:
            batch_op.drop_column("model_name")

    # ② gateway_id 收回 NOT NULL 前，必须先清掉「全网关」行（gateway_id IS NULL），
    #    否则 alter 会因存在 NULL 而失败。降级 = 放弃「全网关」能力 ⇒ 这些行无归属。
    conn.execute(sa.text(f"DELETE FROM {_TABLE_ALERTS} WHERE gateway_id IS NULL"))
    if not _column_notnull(conn, _TABLE_ALERTS, "gateway_id"):
        with op.batch_alter_table(_TABLE_ALERTS, schema=None) as batch_op:
            batch_op.alter_column(
                "gateway_id",
                existing_type=sa.Integer(),
                nullable=False,
            )

    # ③ 删预警规则 model_name
    if _column_exists(conn, _TABLE_ALERTS, "model_name"):
        with op.batch_alter_table(_TABLE_ALERTS, schema=None) as batch_op:
            batch_op.drop_column("model_name")
