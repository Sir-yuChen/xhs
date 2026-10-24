"""模型行新增健康字段 + 模型编码（设计 17 §13.2.12，v2.10）

2026-10-02 用户口径（原文）：

> 「服务商列表的**健康，启用开关，在模型维度展示**」
> 「模型也是跟服务商一样需要有一个人为可读的**唯一ID**，比如 1，2，3 的自然序号」

## 为什么改

v2.7 把「能力类型」与「使用平台」下沉到模型层之后，**健康**与**启用**仍留在服务商层。
用户口径要求这两者也在**模型维度**展示 —— 因为它们描述的是「这个模型还能不能用」，
而不是「这家服务商整体如何」：同一服务商下完全可能「文本模型正常、图片模型挂了」。

| 字段 | 前置状态 | 本次动作 |
|---|---|---|
| `enabled` | **模型表已有**（v2.7 下沉） | **零改动** —— 只是前端从只读 Tag 改为可操作 Switch |
| `healthy` | ❌ 模型表没有 | **本迁移新增** |
| `last_health_at` | ❌ 模型表没有 | **本迁移新增** |
| `model_key` | ❌ 模型表没有 | **本迁移新增**（人类可读唯一 ID，服务商内自然序号） |

## `model_key` 的语义（与 `provider_key` 对齐）

| 取名 | 载体 | 形态 | 可读性 | 用途 |
|---|---|---|---|---|
| `llm_gateway_configs.provider_key` | 服务商 | `{slug(name)}-{4位随机}`（如 `deepseek-a1b2`） | 中 | 业务代码引用 |
| **`llm_gateway_models.model_key`** | 模型 | **`1` / `2` / `3`**（服务商内自然序号） | 高 | 用户口头/文档指代 |

用户口径是「**自然序号**」（`1`、`2`、`3`）而非随机串 —— 模型数量少、且用户要的是
「看一眼就知道在说哪个模型」。故取值 = **该服务商内已有 `model_key` 的最大值 + 1**
（服务商内唯一，不跨服务商全局递增 —— 每个服务商都从 `1` 开始，符合「序号」直觉）。

⚠️ **与自增主键 `id` 的区别（两者不可互相替代）**：
`id` 是 DB 全局自增（跨服务商连续），用户看到「第 37 号模型」没有意义；
`model_key` 是**服务商内**的自然序号，语义上属于该服务商的模型清单。

## 判据（不放宽缺值口径）

| 列 | 类型 | 默认 | 判据 |
|---|---|---|---|
| `healthy` | `Boolean` | `False` | **`last_health_at IS NOT NULL AND healthy`** 才算「正常」 |
| `last_health_at` | `DateTime` | `NULL` | **`NULL` = 从未探测** |

⚠️ **`healthy` 默认 `False` 是老坑（DEF-067 同类）**：只看 `healthy` 会把「从未探测」
误报成「不健康」。前端必须**先判 `last_health_at` 是否为空**再判 `healthy` —— 即
「从未探测 ⇒ 未探测」，不是「异常」。此判据与 `llm_gateway_configs.healthy` 的
既有语义**完全一致**（可见内存 ⛔ 网关预警段：`health` 判据 = `last_health_at is not None
AND not healthy`，否则误报每一个新建服务商）。

⚠️ **为什么不做健康回填**：历史模型行**确实从未被逐模型探测过** ⇒ 回填 `healthy=True`
就是**造假**（违反缺值口径「解析不到就留空 / null，不造假」）。故全部留 `NULL`，
UI 如实显示「未探测」。

## `model_key` 回填

历史模型行**必须有可读 ID**（用户要能指代它们），故本迁移按
**`(user_id, provider_key)` 分组、按 `id` 升序**依次填 `1`、`2`、`3`… ——
这是**确定性**的（同样数据跑两次结果相同），不是「猜」。

## 幂等（本项目硬纪律，设计 03 §9.5.1）

SQLite DDL 不在事务里 ⇒ 建列前判 `PRAGMA table_info`；回填只填 `model_key = ''` 的行
（重跑不会覆盖已填值）。**可整体重跑。**

## 可逆

`downgrade()` 删三列。⚠️ **有损**：删列即丢弃已探测的健康结果与模型编码
（重跑升级后健康全回「未探测」，编码按当时的 `id` 序重排）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

from backend.app.core.migration_introspect import column_names, table_exists

revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None

_TABLE = "llm_gateway_models"


def _column_exists(conn, table: str, column: str) -> bool:
    #: v1.1.0 PG 化：原为 `PRAGMA table_info`（SQLite 专属），统一走内省助手。
    return column in column_names(conn, table)


def _table_exists(conn, table: str) -> bool:
    #: v1.1.0 PG 化：原为 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    return table_exists(conn, table)


def upgrade() -> None:
    conn = op.get_bind()

    if not _table_exists(conn, _TABLE):
        return

    # ① healthy：最近一次逐模型探测是否成功。
    # ⚠️ 默认 False（**不改**）：与 configs.healthy 同形态，判据靠 last_health_at 兜。
    if not _column_exists(conn, _TABLE, "healthy"):
        with op.batch_alter_table(_TABLE, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("healthy", sa.Boolean(), nullable=False, server_default=sa.false())
            )

    # ② last_health_at：NULL = 从未探测（**不回填**，见模块 docstring）。
    if not _column_exists(conn, _TABLE, "last_health_at"):
        with op.batch_alter_table(_TABLE, schema=None) as batch_op:
            batch_op.add_column(sa.Column("last_health_at", sa.DateTime(), nullable=True))

    # ③ model_key：人类可读唯一 ID（服务商内自然序号，如 "1" / "2" / "3"）。
    # 存字符串而非整数：与 `provider_key` 同为「编码」语义，且将来若要加前缀（如 "M1"）
    # 不必再改列类型；排序/比较由应用层按需转 int。
    if not _column_exists(conn, _TABLE, "model_key"):
        with op.batch_alter_table(_TABLE, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("model_key", sa.String(length=32), nullable=False, server_default="")
            )

    # ④ 回填 model_key：按 (user_id, provider_key) 分组、按 id 升序依次给 1,2,3…
    # ⚠️ 只填空串（幂等：重跑不覆盖用户/运行时已生成的编码）。
    rows = conn.execute(
        sa.text(
            f"SELECT id, user_id, provider_key FROM {_TABLE} "
            "WHERE model_key IS NULL OR model_key = '' ORDER BY user_id, provider_key, id"
        )
    ).fetchall()
    counters: dict[tuple[int, str], int] = {}
    for row in rows:
        group = (row.user_id, row.provider_key)
        seq = counters.get(group, 0) + 1
        counters[group] = seq
        conn.execute(
            sa.text(f"UPDATE {_TABLE} SET model_key = :k WHERE id = :i"),
            {"k": str(seq), "i": row.id},
        )


def downgrade() -> None:
    conn = op.get_bind()

    if not _table_exists(conn, _TABLE):
        return

    for column in ("model_key", "last_health_at", "healthy"):
        if _column_exists(conn, _TABLE, column):
            with op.batch_alter_table(_TABLE, schema=None) as batch_op:
                batch_op.drop_column(column)
