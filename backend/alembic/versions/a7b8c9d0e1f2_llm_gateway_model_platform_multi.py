"""模型行 platform 由单值改多值数组（设计 17 §13.2.11，v2.9）

2026-10-02 用户口径（原文）：

> 「配置服务商时应直接父子树展示该服务商下所有模型……**支持多个平台公共一个服务商下的
> 一个具体模型**」（「公共」= **共用**）

## 为什么改

v2.7 把「使用平台」下沉到模型层时用的是**单值** `String(32)`（默认 `xhs`）。
用户口径要求**同一模型可被多个平台共用** ⇒ 单值表达不了，必须多值。

## 列形态选择：JSON 数组字符串（**不是**逗号分隔）

| 方案 | 取舍 |
|---|---|
| 逗号分隔 `xhs,douyin` | 平台码不含逗号，但**值里出现逗号就歧义**；且判空/切分要各处手写 |
| **JSON 数组字符串** `["xhs","douyin"]` | 与 `models_json` 同风格（本项目已有先例）；用 `json.loads` 解析无歧义 |

⇒ 选 JSON 数组。列类型仍为 `TEXT`（SQLite 无原生数组；`String(255)` 够放 6 个平台码）。
解析/落库统一走 `api/llm_gateway._parse_platforms` / `_dump_platforms`（**唯一收敛点**）。

## 动作

| 动作 | 对象 | 说明 |
|---|---|---|
| **重命名** | `llm_gateway_models.platform` → `platform_legacy` | 保留旧单值，避免「就地改写」把数据冲掉 |
| **建新列** | `llm_gateway_models.platform`（`String(255)`，默认 `[]`） | JSON 数组字符串 |
| **回填** | 新列 ← 旧列 | 非空单值 ⇒ `["xhs"]`；空串 ⇒ `[]`（**不臆造平台**） |
| **删旧列** | `platform_legacy` | 回填完成后清理 |

⚠️ **为什么「重命名 + 新列」而不是「就地改写」**：SQLite 的 `UPDATE` 里做字符串拼接
容易在边界情形（空串 / 已带方括号）写出坏数据；分列后回填是**幂等且可校验**的，
且中途出错也能从 `platform_legacy` 重跑。

## 幂等（本项目硬纪律，设计 03 §9.5.1）

SQLite DDL 不在事务里。故：建列/删列判 `PRAGMA table_info`；重命名判「旧名在不在」；
回填用 `WHERE platform_legacy IS NOT NULL`（重跑不会重复转义 —— 转完旧列就被删）。
**可整体重跑。**

## 可逆

`downgrade()` 把 JSON 数组**取第一个元素**回填到单值列 —— ⚠️ **有损**：
多平台共用的模型降级后只剩第一个平台（旧代码没有多值表达力，这是**不可避免**的损失）。
故本迁移上生产前**必须**用库副本三向实测（升级 → 校验 → 降级 → 再升级），同 `REF §F`。
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

from backend.app.core.migration_introspect import column_names, table_exists

revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None

_TABLE = "llm_gateway_models"
_LEGACY = "platform_legacy"


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

    # ------------------------------ ① 旧单值列改名（保留数据）#
    if _column_exists(conn, _TABLE, "platform") and not _column_exists(conn, _TABLE, _LEGACY):
        with op.batch_alter_table(_TABLE, schema=None) as batch_op:
            batch_op.alter_column("platform", new_column_name=_LEGACY)

    # ------------------------------ ② 建新列（JSON 数组字符串）#
    if not _column_exists(conn, _TABLE, "platform"):
        with op.batch_alter_table(_TABLE, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("platform", sa.String(length=255), nullable=False, server_default="[]")
            )

    # ------------------------------ ③ 回填：单值 → JSON 数组 #
    # ⚠️ 逐行用 Python 转（而非 SQL 字符串拼接）：能正确处理
    # 「空串 ⇒ []」「已是 JSON 数组 ⇒ 原样保留」「裸值 ⇒ [value]」三种形态，
    # 且与运行时 `_parse_platforms` 的宽入语义**一致**（迁移与运行时不脱节）。
    if _column_exists(conn, _TABLE, _LEGACY):
        rows = conn.execute(
            sa.text(f"SELECT id, {_LEGACY} AS legacy FROM {_TABLE}")
        ).fetchall()
        for row in rows:
            raw = row.legacy
            text = (raw or "").strip()
            if not text:
                value: list[str] = []
            elif text.startswith("["):
                # 已是数组形态（理论上不该出现在旧列里，但宽入不丢数据）
                try:
                    parsed = json.loads(text)
                    value = [str(x).strip() for x in parsed if str(x).strip()] if isinstance(parsed, list) else []
                except Exception:
                    value = []
            else:
                # 旧单值 ⇒ 单元素数组
                value = [text]
            conn.execute(
                sa.text(f"UPDATE {_TABLE} SET platform = :v WHERE id = :i"),
                {"v": json.dumps(value, ensure_ascii=False), "i": row.id},
            )

    # ------------------------------ ④ 删旧列 #
    if _column_exists(conn, _TABLE, _LEGACY):
        with op.batch_alter_table(_TABLE, schema=None) as batch_op:
            batch_op.drop_column(_LEGACY)


def downgrade() -> None:
    conn = op.get_bind()

    if not _table_exists(conn, _TABLE):
        return

    # ------------------------------ ① 建回单值列 #
    if not _column_exists(conn, _TABLE, _LEGACY):
        with op.batch_alter_table(_TABLE, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(_LEGACY, sa.String(length=32), nullable=False, server_default="")
            )

    # ------------------------------ ② 多值 → 单值（取第一个）#
    # ⚠️ **有损**：多平台共用的模型只留第一个平台（见模块 docstring「可逆」段）。
    if _column_exists(conn, _TABLE, "platform"):
        rows = conn.execute(sa.text(f"SELECT id, platform FROM {_TABLE}")).fetchall()
        for row in rows:
            raw = (row.platform or "").strip()
            first = ""
            if raw:
                if raw.startswith("["):
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, list):
                            for item in parsed:
                                value = str(item).strip()
                                if value:
                                    first = value
                                    break
                    except Exception:
                        first = ""
                else:
                    first = raw
            conn.execute(
                sa.text(f"UPDATE {_TABLE} SET {_LEGACY} = :v WHERE id = :i"),
                {"v": first, "i": row.id},
            )

    # ------------------------------ ③ platform 还原为单值列 #
    # 顺序：先删多值列，再把 legacy 改回 platform 名。
    if _column_exists(conn, _TABLE, "platform"):
        with op.batch_alter_table(_TABLE, schema=None) as batch_op:
            batch_op.drop_column("platform")
    if _column_exists(conn, _TABLE, _LEGACY):
        with op.batch_alter_table(_TABLE, schema=None) as batch_op:
            batch_op.alter_column(_LEGACY, new_column_name="platform")
