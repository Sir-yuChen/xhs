"""LLM 网关服务商化：provider_key / platform 列 + 令牌映射表退场（设计 17 §13.1 v1.9）

设计 17 v1.9（2026-10-01 用户口径「连接配置应是新增服务商」）的**唯一迁移**。三件事：

| 动作 | 对象 | 说明 |
|---|---|---|
| 加列 + 唯一索引 | `llm_gateway_configs.provider_key` / `.platform` | 服务商唯一 KEY（D1 系统生成）+ 使用平台（D3 枚举） |
| 加列（可空） | `model_configs.provider_key` | D5：模型配置**指向**服务商；非空走服务商解析，为空回落旧行为且不记账 |
| drop table | `llm_gateway_tokens` | §7.1a / §3.11 令牌映射整表退场 |

⚠️ **不动** `llm_gateway_usage_logs.token_name` 列 —— 历史行保留（审计），v1.9 起新行恒落 `""`。

## 存量行回填（关键）

- `llm_gateway_configs.provider_key`：按 D1 算法逐行为存量行生成（`{slug(name)}-{4位}`），
  **不留 NULL** —— 唯一索引建在 `(user_id, provider_key)` 上，留 NULL 在 SQLite 里
  多行 NULL 互不冲突，等于索引失效（该列的语义就是「非空唯一」）。
- `llm_gateway_configs.platform`：回填 `"xhs"` —— 存量两条配置全部服务小红书（实测 2 行，均 xhs 语境）。
- `model_configs.provider_key`：**保持 NULL**（不回填）—— 存量模型配置是历史直连配置，
  强行指向某个服务商 = 替用户猜凭据来源（§5 硬约束 5「缺值不造假」）。用户要启用服务商托管时自行填。

## 幂等（本项目硬纪律，设计 03 §9.5.1）

应用启动自动 `upgrade head`，而 SQLite DDL 不在事务里 ⇒ 中途失败会留「加了一半 + 版本没推进」，
下次从同一条 revision 重跑。故 `upgrade()` 先查 `PRAGMA table_info` / `sqlite_master` 判存在性。

⚠️ 本迁移额外一层：`provider_key` 的**回填**与**建索引**要能在「列已存在但索引未建」的中间态下收敛 ——
故两件事各自独立判存在性，不是一个 if 包住。

## 可逆

`downgrade()`：重建 `llm_gateway_tokens`（结构照 `n5o6p7q8r9s0` 原定义）→ 删两个索引/列。
⚠️ downgrade **不还原被 drop 的令牌映射数据**（原表在建此迁移时实测 0 行，且该功能已退场）。
"""

from __future__ import annotations

import re
import secrets

import sqlalchemy as sa

from alembic import op

revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None

_TABLE_CONFIGS = "llm_gateway_configs"
_TABLE_MODEL_CONFIGS = "model_configs"
_TABLE_TOKENS = "llm_gateway_tokens"

_INDEX_PROVIDER_KEY = "uq_llm_gateway_configs_user_provider_key"

#: 与 `note_queue_service._BATCH_ALPHABET` 同一纪律：去易混字符 `0/O/1/I/L`
#: （设计 17 §35.13）。`provider_key` 会被业务代码引用与人工抄写，混字符代价是排查成本。
_KEY_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

#: 存量行 `platform` 回填值（D3 枚举之一；存量配置全部服务小红书）
_BACKFILL_PLATFORM = "xhs"

#: `default_model` 存量空值回填的兜底文案（见 upgrade 注释）。
#: ⚠️ **刻意不是真实模型名** —— 猜一个真名等于替用户编造凭据（§5 硬约束 5）；
#: 这个显眼的中文占位让用户在列表页一眼看出「这条要补」，且不会误调成功。
_DEFAULT_MODEL_PLACEHOLDER = "未指定-请在页面补填"


def _columns(conn, table: str) -> set[str]:
    """表的列名集合（表不存在 ⇒ 空集）。"""
    rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return {str(row[1]) for row in rows}


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
        {"n": table},
    ).fetchone()
    return row is not None


def _index_exists(conn, index: str) -> bool:
    row = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='index' AND name=:n"),
        {"n": index},
    ).fetchone()
    return row is not None


def _slug(name: str) -> str:
    """服务商名 → KEY 前缀：只留字母数字，转大写，截 24 位。

    ⚠️ 非 ASCII 名（如纯中文「本机网关」）过滤后可能为空 —— 此时返回 `PROVIDER`，
    保证 KEY 仍是一个合法且可读的标识（**不返回空串**，空串会让 KEY 变成 `-XXXX`）。
    """
    slug = re.sub(r"[^A-Za-z0-9]", "", name or "").upper()[:24]
    return slug or "PROVIDER"


def _new_provider_key(name: str, taken: set[str]) -> str:
    """按 D1 生成 `{slug(name)}-{4位}`，在 `taken` 内查重后返回。

    ⚠️ 迁移里的查重范围是**同批已生成的键**（跨 user 也全收）—— 唯一索引是
    `(user_id, provider_key)`，严格说不同 user 可以同 key；但**主动避重**能让
    KEY 在日志/排障里全局可读（同一个 KEY 不再指两行），代价只是循环次数。
    """
    for _ in range(64):
        candidate = f"{_slug(name)}-{''.join(secrets.choice(_KEY_ALPHABET) for _ in range(4))}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    # 64 次全撞（36^4 = 168 万空间，实际不可能）—— 加时间片兜底，宁可长一点也不能留 NULL
    import time

    candidate = f"{_slug(name)}-{int(time.time()) % 100000}"
    taken.add(candidate)
    return candidate


def upgrade() -> None:
    conn = op.get_bind()

    # ------------------------------------------------ ① llm_gateway_configs #
    config_cols = _columns(conn, _TABLE_CONFIGS)

    if "provider_key" not in config_cols:
        # 先加**可空**列（SQLite 不能给非空列配「存量行回填」以外的默认值），
        # 回填完再靠应用层保证非空；DB 层不设 NOT NULL 是为了让本迁移在
        # 「列已加、回填未完」的中间态下可重跑（幂等纪律）。
        op.add_column(
            _TABLE_CONFIGS,
            sa.Column("provider_key", sa.String(length=64), nullable=False, server_default=""),
        )
        # 逐行回填：同一 user 内不重复（避重集合按 user 分开维护 —— 唯一索引是 (user_id, key)）
        rows = conn.execute(
            sa.text(f"SELECT id, user_id, name FROM {_TABLE_CONFIGS} ORDER BY id")
        ).fetchall()
        taken_by_user: dict[int, set[str]] = {}
        for row_id, user_id, name in rows:
            taken = taken_by_user.setdefault(int(user_id), set())
            key = _new_provider_key(str(name or ""), taken)
            conn.execute(
                sa.text(f"UPDATE {_TABLE_CONFIGS} SET provider_key = :k WHERE id = :i"),
                {"k": key, "i": int(row_id)},
            )

    if "platform" not in config_cols:
        op.add_column(
            _TABLE_CONFIGS,
            sa.Column(
                "platform",
                sa.String(length=32),
                nullable=False,
                server_default=_BACKFILL_PLATFORM,
            ),
        )
        # server_default 已把存量行填成 xhs（SQLite ADD COLUMN 带默认值 ⇒ 存量行取默认）——
        # 显式再 UPDATE 一次是为了防「旧版 SQLite 不填存量行」的方言差异（幂等，重复无害）。
        conn.execute(
            sa.text(
                f"UPDATE {_TABLE_CONFIGS} SET platform = :p "
                f"WHERE platform IS NULL OR platform = ''"
            ),
            {"p": _BACKFILL_PLATFORM},
        )

    # 唯一索引单独判存在性（与加列解耦：列在、索引缺的中间态要能收敛）
    if not _index_exists(conn, _INDEX_PROVIDER_KEY):
        op.create_index(
            _INDEX_PROVIDER_KEY,
            _TABLE_CONFIGS,
            ["user_id", "provider_key"],
            unique=True,
        )

    # `default_model` 存量空值回填（v1.9「默认模型必填」的存量面）。
    # 策略：优先取该行 `models_json` 目录的第一个模型；目录空 ⇒ 落占位文案。
    # ⚠️ 回填而非留空：留空 = 一条永远调不通、而用户看不出原因的服务商。
    # ⚠️ 也**不猜真实模型名**（那是替用户编造凭据，§5 硬约束 5）。
    # ⚠️ **必须先判列存在**（2026-10-02 修）：后续迁移 `f6a7b8c9d0e1`（v2.8）会 **drop**
    #    这列，而「stamp 回早期 revision 再 upgrade」是 alembic 的正常用法（迁移用例
    #    的 ④ 幂等步骤就这么做）⇒ 重跑时该列已不存在，`SELECT default_model` 直接
    #    `OperationalError: no such column`。与下方 ② 判表存在同型（`_columns()` 对
    #    缺列同样返回「不在集合」，故统一用 `_columns()` 判列）。
    import json as _json

    if "default_model" in _columns(conn, _TABLE_CONFIGS):
        blanks = conn.execute(
            sa.text(
                f"SELECT id, models_json FROM {_TABLE_CONFIGS} "
                f"WHERE default_model IS NULL OR TRIM(default_model) = ''"
            )
        ).fetchall()
        for row_id, models_json in blanks:
            picked = ""
            try:
                models = _json.loads(models_json or "[]")
                if isinstance(models, list):
                    picked = next(
                        (m for m in models if isinstance(m, str) and m.strip()), ""
                    )
            except Exception:
                picked = ""
            conn.execute(
                sa.text(f"UPDATE {_TABLE_CONFIGS} SET default_model = :m WHERE id = :i"),
                {"m": picked or _DEFAULT_MODEL_PLACEHOLDER, "i": int(row_id)},
            )

    # --------------------------------------------------- ② model_configs #
    # ⚠️ **必须同时判表存在**：`_columns()` 对不存在的表返回空集，于是
    # 「provider_key not in 空集」恒真 ⇒ 会对已退场的表发 ALTER 而炸。
    # 这不是假想路径：v2.0 的 `f8a9b0c1d2e3` 会 **drop** 这张表，而
    # 「stamp 回早期 revision 再 upgrade」是 alembic 的正常用法
    #（项目的迁移用例 ④ 幂等步骤就这么做）⇒ 重跑时该表已不存在。
    if _table_exists(conn, _TABLE_MODEL_CONFIGS) and "provider_key" not in _columns(conn, _TABLE_MODEL_CONFIGS):
        # D5：**可空**（存量历史直连配置不强行指向服务商 —— 见模块 docstring）
        op.add_column(
            _TABLE_MODEL_CONFIGS,
            sa.Column("provider_key", sa.String(length=64), nullable=True),
        )

    # ----------------------------------------- ③ llm_gateway_tokens 退场 #
    if _table_exists(conn, _TABLE_TOKENS):
        op.drop_table(_TABLE_TOKENS)


def downgrade() -> None:
    conn = op.get_bind()

    # ③ 还原令牌映射表（结构照 n5o6p7q8r9s0 原定义；⚠️ 不还原数据，见 docstring）
    if not _table_exists(conn, _TABLE_TOKENS):
        op.create_table(
            _TABLE_TOKENS,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("gateway_id", sa.Integer(), nullable=False),
            sa.Column("platform", sa.String(length=32), nullable=False),
            sa.Column("function", sa.String(length=32), nullable=False),
            sa.Column("token_name", sa.String(length=128), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id", "gateway_id", "platform", "function",
                name="uq_llm_gateway_tokens_user_gateway_platform_function",
            ),
        )
        op.create_index("ix_llm_gateway_tokens_user_id", _TABLE_TOKENS, ["user_id"])
        op.create_index("ix_llm_gateway_tokens_gateway_id", _TABLE_TOKENS, ["gateway_id"])

    # ② model_configs.provider_key
    if "provider_key" in _columns(conn, _TABLE_MODEL_CONFIGS):
        op.drop_column(_TABLE_MODEL_CONFIGS, "provider_key")

    # ① llm_gateway_configs 索引 + 两列
    if _index_exists(conn, _INDEX_PROVIDER_KEY):
        op.drop_index(_INDEX_PROVIDER_KEY, table_name=_TABLE_CONFIGS)

    config_cols = _columns(conn, _TABLE_CONFIGS)
    if "platform" in config_cols:
        op.drop_column(_TABLE_CONFIGS, "platform")
    if "provider_key" in config_cols:
        op.drop_column(_TABLE_CONFIGS, "provider_key")
