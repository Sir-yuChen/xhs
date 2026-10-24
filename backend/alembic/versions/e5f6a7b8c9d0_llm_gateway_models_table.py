"""服务商 / 模型数据分层：能力类型与使用平台下沉到模型级（设计 17 §13.2.9，v2.7）

2026-10-02 用户口径（原文）：「模型网关中的服务商页面，**将服务商和服务商模型的数据才分开**，
因为服务商信息是其下的模型**公用的**比如服务商接口 key、调用地址是公共的，**具体模型才需要分
文本，多模态，图片，视频类似等等**，对应的逻辑进行调整，**使用平台下沉到具体模型**，
当前具体平台配置后同步修改这个参数。」

| 动作 | 对象 | 说明 |
|---|---|---|
| **建表** | `llm_gateway_models` | 模型层：`provider_key` + `model_name` + `model_type` + `platform` + `enabled` + `remark` |
| **回填** | 同上 | 从 ① `llm_business_providers` 的映射 ② 服务商 `default_model` **反推**，不猜 |
| **删列** | `llm_gateway_configs.model_type` | 下沉完成，服务商层不再承载能力类型 |
| **删列** | `llm_gateway_configs.platform` | 下沉完成，服务商层不再承载使用平台 |

## 为什么删列而不是保留（用户 2026-10-02 明确选择）

选项问：「服务商表上的 model_type / platform 怎么处置？」用户答：
**「不保留，你直接使用 sql 修复存量数据」**。

⇒ 搬走（回填到模型层）后**删列**。保留为「默认值」会让两处同语义字段并存，
后来者分不清哪个生效（本项目 DEF-044「同一语义不留两份实现」的同型风险）。

## 回填规则（每一条都有来源，**绝不猜**）

1. **映射反推（主力）**：`llm_business_providers` 每行的 `(provider_key, model_name)`
   ⇒ 一条模型行。`model_type` 取**该映射 business_key 的 kind** —— 这是**代码常量**，
   `generate_cover` 必然是 `image`，比服务商的旧 `model_type` **更可信**（业务点 kind 是
   调用契约的一部分，服务商旧列是用户手标的）。`platform` 取映射行的 `platform`。
2. **默认模型补漏**：服务商 `default_model` 若未被上面产生，补一条；
   `model_type` = 该服务商旧 `model_type`，`platform` = 该服务商旧 `platform`。
3. **其余不补**：目录（`models_json`）里未被任何映射 / 默认引用的模型**不建行** ——
   用户没表态 ⇒ 不猜类型（同 §13.2.1a「判不准时回 text、绝不猜 image」的纪律延伸）。

⚠️ **冲突处理**：同一 `(provider_key, model_name)` 被**不同 kind** 的映射引用，
以**服务商旧 `model_type`** 为准并留告警日志（不静默丢）。理论上不该发生
（v2.0 的映射校验已保证 `kind == 服务商 model_type`），但**存量数据不信理论**。

## 幂等（本项目硬纪律，设计 03 §9.5.1）

SQLite DDL 不在事务里，中途失败会留「改了一半 + 版本没推进」。故：
建表判 `sqlite_master`、建索引判 `sqlite_master`、回填用 `INSERT ... WHERE NOT EXISTS`
（重跑不产生重复行）、删列判 `PRAGMA table_info`。**可整体重跑。**

## 可逆

`downgrade()` 逆序：建回两列（`model_type` default `text` / `platform` default `xhs`）
⇒ 从模型层**取每个服务商的第一条模型行**回填 ⇒ drop 表。
取不到模型行时回落 `text` / `xhs`（`text` 是 v2.0 §13.2.1a 定的安全默认）。

⚠️ **降级有损**：一个服务商下**多条不同平台的模型行**，降级只能保留一条的
`platform`（服务商层无法表达「同服务商多平台」）。这是**降级的本质代价**
（降级 = 放弃 v2.7 的分层能力），非实现缺陷。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

from backend.app.core.migration_introspect import column_names, index_exists, table_exists

revision = "e5f6a7b8c9d0"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None

_TABLE_MODELS = "llm_gateway_models"
_TABLE_CONFIGS = "llm_gateway_configs"
_TABLE_BUSINESS = "llm_business_providers"

#: 业务点 kind 反查表 —— **代码常量的 SQL 镜像**（`services/business_provider_keys.py`）。
#: ⚠️ 迁移必须**冻结**当时的常量值：将来业务点增删**不得**回头改这里
#: （迁移是历史快照，改它等于篡改已发生的迁移）。
_KIND_BY_BUSINESS_KEY: dict[str, str] = {
    # 文本类（5）
    "generate_note": "text",
    "rewrite_note": "text",
    "generate_title": "text",
    "generate_tags": "text",
    "polish_text": "text",
    # 图像类（4）
    "generate_cover": "image",
    "generate_image": "image",
    "describe_image": "image",
    "generate_product_detail_image": "image",
}


def _table_exists(conn, table: str) -> bool:
    #: v1.1.0 PG 化：原为 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    return table_exists(conn, table)


def _column_exists(conn, table: str, column: str) -> bool:
    #: v1.1.0 PG 化：原为 `PRAGMA table_info`（SQLite 专属），统一走内省助手。
    return column in column_names(conn, table)


def _index_exists(conn, index: str) -> bool:
    #: v1.1.0 PG 化：原为 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    return index_exists(conn, index)


def upgrade() -> None:
    conn = op.get_bind()

    # ------------------------------------ ① 建模型层表 #
    if not _table_exists(conn, _TABLE_MODELS):
        op.create_table(
            _TABLE_MODELS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("provider_key", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("model_name", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("model_type", sa.String(length=16), nullable=False, server_default="text"),
            sa.Column("platform", sa.String(length=32), nullable=False, server_default="xhs"),
            # v1.1.0 PG 化：布尔列默认值写 sa.true()（原 sa.text("1") PG 报 DatatypeMismatch）。
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("remark", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "user_id", "provider_key", "model_name",
                name="uq_llm_gateway_models_user_provider_model",
            ),
        )

    if not _index_exists(conn, "ix_llm_gateway_models_user_id"):
        op.create_index("ix_llm_gateway_models_user_id", _TABLE_MODELS, ["user_id"])

    # ---------------------------- ② 回填：从业务点映射反推 #
    # kind 取业务点常量（比服务商旧列更可信）；platform 取映射行。
    # INSERT ... WHERE NOT EXISTS ⇒ 幂等，重跑不产生重复行。
    for business_key, kind in _KIND_BY_BUSINESS_KEY.items():
        conn.execute(
            sa.text(
                f"""
                INSERT INTO {_TABLE_MODELS}
                    (user_id, provider_key, model_name, model_type, platform,
                     enabled, remark, created_at, updated_at)
                SELECT
                    b.user_id, b.provider_key, b.model_name, :kind, b.platform,
                    TRUE, '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                FROM {_TABLE_BUSINESS} AS b
                WHERE b.business_key = :bkey
                  AND b.provider_key <> ''
                  AND b.model_name <> ''
                  AND NOT EXISTS (
                      SELECT 1 FROM {_TABLE_MODELS} AS m
                      WHERE m.user_id = b.user_id
                        AND m.provider_key = b.provider_key
                        AND m.model_name = b.model_name
                  )
                """
            ),
            {"kind": kind, "bkey": business_key},
        )

    # ------------------- ③ 回填：服务商 default_model 补漏 #
    # 仅在「该服务商该模型尚无行」时补；类型取服务商旧 model_type，平台取服务商旧 platform。
    # ⚠️ **三个旧列都要判存在**（2026-10-02 修）：`f6a7b8c9d0e1`（v2.8）会 drop
    #    `default_model` 并改 `model_type`/`platform`，而「stamp 回早期 revision 再
    #    upgrade」是 alembic 的正常用法（迁移用例 ④ 幂等步骤）⇒ 重跑时列已不在，
    #    语句里的 `c.default_model` 会直接 `OperationalError: no such column`。
    if (
        _column_exists(conn, _TABLE_CONFIGS, "default_model")
        and _column_exists(conn, _TABLE_CONFIGS, "model_type")
        and _column_exists(conn, _TABLE_CONFIGS, "platform")
    ):
        conn.execute(
            sa.text(
                f"""
                INSERT INTO {_TABLE_MODELS}
                    (user_id, provider_key, model_name, model_type, platform,
                     enabled, remark, created_at, updated_at)
                SELECT
                    c.user_id, c.provider_key, c.default_model, c.model_type, c.platform,
                    TRUE, '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                FROM {_TABLE_CONFIGS} AS c
                WHERE c.default_model <> ''
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

    # --------------------------- ④ 冲突告警：同模型名被不同 kind 引用 #
    # 理论不该发生（v2.0 映射校验保证 kind == 服务商 model_type），但存量数据不信理论。
    # 以服务商旧 model_type 为准（回填已如此），此处只**留痕**、不静默丢。
    rows = conn.execute(
        sa.text(
            f"""
            SELECT m.provider_key, m.model_name,
                   string_agg(DISTINCT m.model_type, ',') AS types
            FROM {_TABLE_MODELS} AS m
            GROUP BY m.provider_key, m.model_name
            HAVING COUNT(DISTINCT m.model_type) > 1
            """
        )
    ).fetchall()
    #: ⚠️ v1.1.0 PG 化：原为 SQLite 的 GROUP_CONCAT，PG 等价物是
    #: `string_agg(col, ',')`（SQLite 默认分隔符就是逗号，语义一致）。
    for r in rows:
        print(
            f"[mig e5f6a7b8c9d0] WARN 模型类型冲突："
            f"provider_key={r[0]} model_name={r[1]} types={r[2]} —— "
            f"已以服务商旧 model_type 为准，请到服务商配置页复核"
        )

    # ------------------------------------ ⑤ 删服务商旧列 #
    # 放在回填之后：回填要读这两列。
    if _column_exists(conn, _TABLE_CONFIGS, "model_type"):
        with op.batch_alter_table(_TABLE_CONFIGS, schema=None) as batch_op:
            batch_op.drop_column("model_type")

    if _column_exists(conn, _TABLE_CONFIGS, "platform"):
        with op.batch_alter_table(_TABLE_CONFIGS, schema=None) as batch_op:
            batch_op.drop_column("platform")


def downgrade() -> None:
    conn = op.get_bind()

    # ------------------------------------ ① 建回服务商两列 #
    if not _column_exists(conn, _TABLE_CONFIGS, "model_type"):
        with op.batch_alter_table(_TABLE_CONFIGS, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "model_type",
                    sa.String(length=16),
                    nullable=False,
                    server_default="text",
                )
            )

    if not _column_exists(conn, _TABLE_CONFIGS, "platform"):
        with op.batch_alter_table(_TABLE_CONFIGS, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "platform",
                    sa.String(length=32),
                    nullable=False,
                    server_default="xhs",
                )
            )

    # ------------------------- ② 从模型层取首条模型行回填 #
    # 取不到 ⇒ 保持列默认（text / xhs）。⚠️ 有损：同服务商多平台模型只能保留一条（见文件头）。
    if _table_exists(conn, _TABLE_MODELS):
        conn.execute(
            sa.text(
                f"""
                UPDATE {_TABLE_CONFIGS}
                SET model_type = COALESCE(
                        (
                            SELECT m.model_type FROM {_TABLE_MODELS} AS m
                            WHERE m.user_id = {_TABLE_CONFIGS}.user_id
                              AND m.provider_key = {_TABLE_CONFIGS}.provider_key
                            ORDER BY m.id
                            LIMIT 1
                        ),
                        'text'
                    ),
                    platform = COALESCE(
                        (
                            SELECT m.platform FROM {_TABLE_MODELS} AS m
                            WHERE m.user_id = {_TABLE_CONFIGS}.user_id
                              AND m.provider_key = {_TABLE_CONFIGS}.provider_key
                            ORDER BY m.id
                            LIMIT 1
                        ),
                        'xhs'
                    )
                """
            )
        )

    # ------------------------------------ ③ drop 模型层表 #
    if _index_exists(conn, "ix_llm_gateway_models_user_id"):
        op.drop_index("ix_llm_gateway_models_user_id", table_name=_TABLE_MODELS)
    if _table_exists(conn, _TABLE_MODELS):
        op.drop_table(_TABLE_MODELS)
