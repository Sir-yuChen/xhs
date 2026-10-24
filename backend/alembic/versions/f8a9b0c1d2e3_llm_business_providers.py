"""LLM 网关 v2.0 业务点映射：`llm_business_providers` 建表 + `model_type` 加列 +
`model_configs` 退场（设计 17 §13.2.5）

设计 17 v2.0（2026-10-01 用户口径「模型配置移除，业务中的使用点映射成固定 key，
新增一个模型配置页，不同平台和不同的业务可以实时切换服务商」）的**唯一迁移**。

| 动作 | 对象 | 说明 |
|---|---|---|
| 建表 | `llm_business_providers` | 「平台 × 业务点」→ 服务商映射（§13.2.3），唯一键三段 |
| 加列 | `llm_gateway_configs.model_type` | 服务商的模型类型（§13.2.1a）：`text` / `image` |
| drop table | `model_configs` | §13.2.4：一期模型配置表**退场**（调用链改走映射） |

⚠️ **不动** `llm_gateway_usage_logs.token_name` 列 —— 历史行保留（审计），
v1.9 既定。

## `model_configs` 的存量兼容（§13.2.5 第 3 条）

drop 前对每条 `model_configs.is_default=1` 的记录做一次「尽量不丢配置」的迁移：

* 其 `base_url`（**归一化**：`strip()` + `rstrip('/')`，与
  `llm_gateway_usage_service._normalize_base_url` 同口径）能匹配到该用户名下
  某条服务商 ⇒ 为该用户建**文本类**业务点的映射，指向该服务商；
* ⚠️ **图像类 key 一律不建** —— 存量服务商的 `model_type` 回填为 `text`，
  建了图像映射也过不了 M2 的类型匹配校验，等于在页面上摆一条假配置。
* ⚠️ **匹配不到 ⇒ 不建映射**（宁可让业务点显示「未配置」，也不认错服务商 ——
  §5 硬约束 5「缺值不造假」；用户到页面自己选）。

存量实测：`model_configs` 仅 1 行（user 1，`base_url=https://api.deepseek.com`），
两条服务商分别是 `http://127.0.0.1:3000` / `https://discovery-api.intern-ai.org.cn/v1`
⇒ **匹配不到 ⇒ 不建任何映射**。这是「不造假」纪律的正确落点，不是漏迁。

## `model_type` 存量回填（§13.2.1a）

**一律 `text`**：判不准时也回 `text`，**绝不猜 `image`** —— 猜错会让用户在
「模型配置页」看到一条假的图像服务商（拿文本模型打 `/images/generations`）。

## 幂等（本项目硬纪律，设计 03 §9.5.1）

应用启动自动 `upgrade head`，而 SQLite DDL 不在事务里 ⇒ 中途失败会留
「改了一半 + 版本没推进」，下次从同一条 revision 重跑。故 `upgrade()` 先查
`PRAGMA table_info` / `sqlite_master` 判存在性，建表/加列/建索引/drop 各自独立判。

⚠️ **`model_configs` 的 drop 与存量兼容的顺序**：必须**先读后删** ——
存量兼容在 drop 之前跑，且自身幂等（映射已存在则跳过，避免重跑时唯一约束撞重）。

## 可逆

`downgrade()` 只**重建空表结构**，**不还原被 drop 的 `model_configs` 数据**
（§13.2.5：drop 是不可逆的数据删除，同 `llm_gateway_tokens` 的先例）。
`llm_business_providers` 的映射数据一并丢弃（downgrade 不重建它 ⇒ 连表一起删）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

from backend.app.core.migration_introspect import column_names, index_exists, table_exists

revision = "f8a9b0c1d2e3"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None

_TABLE_CONFIGS = "llm_gateway_configs"
_TABLE_BUSINESS = "llm_business_providers"
_TABLE_MODEL_CONFIGS = "model_configs"

_INDEX_BUSINESS_UNIQUE = "uq_llm_business_providers_user_platform_business_key"

#: 存量服务商 `model_type` 回填值（§13.2.1a：一律 text，绝不猜 image）。
_BACKFILL_MODEL_TYPE = "text"

#: 存量映射的 `platform` 回填值（与 §3.8 的 `PLATFORM_ENUM` 同源；存量业务全在小红书）。
_BACKFILL_PLATFORM = "xhs"

#: v2.0 业务点固定 key（与 `services/business_provider_keys.py` 同源；此处**不 import**
#: 业务模块 —— 迁移只许依赖 alembic/op 与标准库，避免循环导入与启动期顺序耦合）。
_TEXT_BUSINESS_KEYS = (
    "generate_note",
    "rewrite_note",
    "generate_title",
    "generate_tags",
    "polish_text",
)


def _columns(conn, table: str) -> set[str]:
    """表的列名集合（表不存在 ⇒ 空集）。"""
    #: v1.1.0 PG 化：原为 `PRAGMA table_info`（SQLite 专属），统一走内省助手。
    return column_names(conn, table)


def _table_exists(conn, table: str) -> bool:
    #: v1.1.0 PG 化：原为 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    return table_exists(conn, table)


def _index_exists(conn, index: str) -> bool:
    #: v1.1.0 PG 化：原为 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    return index_exists(conn, index)


def _normalize_base_url(base_url) -> str:
    """归一化 base_url 供反查。

    与 `llm_gateway_usage_service._normalize_base_url` **同口径**：只做
    「去首尾空白 + 去末尾斜杠」。凡能拼出同一 endpoint 的两个 base_url，
    在这里必须归一到同一个键，否则反查必然漏掉（设计 17 §4.3.1）。

    ⚠️ 复制而非 import：迁移不许依赖业务模块（见 `_TEXT_BUSINESS_KEYS` 注释）。
    两处实现刻意保持**肉眼对齐**，任一处改动须同步另一处（有 schema 守卫
    `test_schema_matches_models` 与 M2 的映射用例兜住漂移）。
    """
    return (base_url or "").strip().rstrip("/")


def _migrate_model_configs_defaults(conn) -> None:
    """`model_configs` 的 is_default 行 → `llm_business_providers` 映射（§13.2.5 第 3 条）。

    策略（逐条 is_default=1 的模型配置）：
    1. 归一化其 `base_url`，按 `(user_id, 归一化 base_url)` 反查该用户的服务商；
    2. 命中 ⇒ 为**每个文本类 key** 建一条映射指向它（图像类不建，见模块 docstring）；
    3. 未命中 ⇒ **不建**（缺值不造假，不认错服务商）；
    4. 已存在的映射跳过（幂等：重跑不撞唯一约束）。
    """
    if not _table_exists(conn, _TABLE_MODEL_CONFIGS):
        # ⚠️ 表不存在不是「跳过」—— 是本次运行根本没东西可迁。上层 drop 同样判存在性。
        return

    defaults = conn.execute(
        sa.text(
            f"SELECT id, user_id, base_url FROM {_TABLE_MODEL_CONFIGS} "
            # v1.1.0 PG 化：`is_default` 是布尔列，比较写 TRUE（原 `= 1` 是 SQLite 写法）。
            f"WHERE is_default = TRUE ORDER BY id"
        )
    ).fetchall()
    if not defaults:
        return

    # 该用户名下「归一化 base_url → provider_key」索引（一次查完，逐条匹配）
    providers = conn.execute(
        sa.text(f"SELECT user_id, base_url, provider_key FROM {_TABLE_CONFIGS}")
    ).fetchall()
    by_user_url: dict[int, dict[str, str]] = {}
    for user_id, base_url, provider_key in providers:
        by_user_url.setdefault(int(user_id), {})[_normalize_base_url(base_url)] = str(
            provider_key or ""
        )

    # 已有映射（幂等判据：(user_id, platform, business_key) 命中即跳过）
    existing = {
        (int(uid), str(plat), str(bk))
        for uid, plat, bk in conn.execute(
            sa.text(
                f"SELECT user_id, platform, business_key FROM {_TABLE_BUSINESS}"
            )
        ).fetchall()
    }

    for _row_id, user_id, base_url in defaults:
        user_id = int(user_id)
        url_key = _normalize_base_url(base_url)
        provider_key = by_user_url.get(user_id, {}).get(url_key)
        if not provider_key:
            # 匹配不到该用户名下的服务商 ⇒ 不建映射（用户到页面自己选）
            continue
        for business_key in _TEXT_BUSINESS_KEYS:
            if (user_id, _BACKFILL_PLATFORM, business_key) in existing:
                continue
            conn.execute(
                sa.text(
                    f"INSERT INTO {_TABLE_BUSINESS} "
                    f"(user_id, platform, business_key, provider_key, enabled) "
                    f"VALUES (:u, :p, :b, :k, 1)"
                ),
                {
                    "u": user_id,
                    "p": _BACKFILL_PLATFORM,
                    "b": business_key,
                    "k": provider_key,
                },
            )
            existing.add((user_id, _BACKFILL_PLATFORM, business_key))


def upgrade() -> None:
    conn = op.get_bind()

    # --------------------------------- ① llm_business_providers 建表 #
    if not _table_exists(conn, _TABLE_BUSINESS):
        op.create_table(
            _TABLE_BUSINESS,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("platform", sa.String(length=32), nullable=False, server_default="xhs"),
            sa.Column("business_key", sa.String(length=48), nullable=False),
            sa.Column("provider_key", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            #: ⚠️ 时间戳**必须带 server_default**：本迁移的存量兼容路径用裸 INSERT
            #: 建映射行（不走 ORM，`shanghai_now` 默认值不生效），缺默认值 ⇒
            #: `NOT NULL constraint failed: ...created_at`（SQLite 没有_now()）。
            #: ORM 侧 `LlmBusinessProvider` 的 `default=shanghai_now` 照样生效
            #: （应用层插入时 ORM 会显式带值，server_default 只是兜底）。
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            f"ix_{_TABLE_BUSINESS}_user_id",
            _TABLE_BUSINESS,
            ["user_id"],
        )

    # 唯一约束单独判存在性（与建表解耦：表在、约束缺的中间态要能收敛）
    if not _index_exists(conn, _INDEX_BUSINESS_UNIQUE):
        op.create_index(
            _INDEX_BUSINESS_UNIQUE,
            _TABLE_BUSINESS,
            ["user_id", "platform", "business_key"],
            unique=True,
        )

    # ------------------------------ ② llm_gateway_configs 加 model_type #
    if "model_type" not in _columns(conn, _TABLE_CONFIGS):
        # 先加**可空**列（SQLite 不能给非空列配「存量行回填」以外的默认值），
        # 回填完再靠应用层保证非空（列层不设 NOT NULL = 中间态可重跑，幂等纪律）。
        op.add_column(
            _TABLE_CONFIGS,
            sa.Column(
                "model_type",
                sa.String(length=16),
                nullable=False,
                server_default=_BACKFILL_MODEL_TYPE,
            ),
        )

    # server_default 已把存量行填成 text；显式再 UPDATE 一次是防「旧版 SQLite
    # 不填存量行」的方言差异（幂等，重复无害）。
    conn.execute(
        sa.text(
            f"UPDATE {_TABLE_CONFIGS} SET model_type = :t "
            f"WHERE model_type IS NULL OR TRIM(model_type) = ''"
        ),
        {"t": _BACKFILL_MODEL_TYPE},
    )

    # --------------------- ③ model_configs 存量兼容（必须先于 drop） #
    _migrate_model_configs_defaults(conn)

    # ------------------------------- ④ model_configs 退场（不可逆） #
    if _table_exists(conn, _TABLE_MODEL_CONFIGS):
        op.drop_table(_TABLE_MODEL_CONFIGS)


def downgrade() -> None:
    conn = op.get_bind()

    # ④ 重建 `model_configs` **空表**（⚠️ 不还原数据，见模块 docstring）
    if not _table_exists(conn, _TABLE_MODEL_CONFIGS):
        op.create_table(
            _TABLE_MODEL_CONFIGS,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("name", sa.String(length=128), nullable=True),
            sa.Column("model_type", sa.String(length=32), nullable=True),
            sa.Column("provider", sa.String(length=64), nullable=True),
            sa.Column("model_name", sa.String(length=128), nullable=True),
            sa.Column("base_url", sa.Text(), nullable=True),
            sa.Column("encrypted_api_key", sa.Text(), nullable=True),
            sa.Column("provider_key", sa.String(length=64), nullable=True),
            sa.Column("is_default", sa.Boolean(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_model_configs_user_id", _TABLE_MODEL_CONFIGS, ["user_id"])
        op.create_index("ix_model_configs_model_type", _TABLE_MODEL_CONFIGS, ["model_type"])

    # ② llm_gateway_configs.model_type
    if "model_type" in _columns(conn, _TABLE_CONFIGS):
        op.drop_column(_TABLE_CONFIGS, "model_type")

    # ① llm_business_providers（含唯一索引与建表）
    if _index_exists(conn, _INDEX_BUSINESS_UNIQUE):
        op.drop_index(_INDEX_BUSINESS_UNIQUE, table_name=_TABLE_BUSINESS)

    if _table_exists(conn, _TABLE_BUSINESS):
        #: ⚠️ **索引也要判存在再删**（2026-10-23）：SQLite 的
        #: `DROP INDEX` 对不存在的索引直接报 `no such index`，整条 downgrade 中断。
        #: 索引「不存在」是合法状态 —— 例如最小夹具只建了表、或历史库里该索引
        #: 曾被手工删掉。downgrade 的语义是「回到旧形态」，不该因缺一个可重建的
        #: 索引而失败。
        if _index_exists(conn, f"ix_{_TABLE_BUSINESS}_user_id"):
            op.drop_index(f"ix_{_TABLE_BUSINESS}_user_id", table_name=_TABLE_BUSINESS)
        op.drop_table(_TABLE_BUSINESS)
