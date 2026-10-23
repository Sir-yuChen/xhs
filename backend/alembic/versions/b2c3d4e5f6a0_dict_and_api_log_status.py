"""dict tables + api_logs.status_code (backfill) + notifications.link

v1.1.0 / 设计 47 §2.4-2.5 + 设计 48 R3 / R4（S1 迁移 2 of 3）。

## `api_logs.status_code` 的回填（70,325 行）—— 为什么在 **Python 侧分块**

线上量（设计 58 §3.2）：`api_logs` **70,325 行**，`meta` 是 JSON
（`{method, status_code, elapsed_ms}`，实测 2000 条采样 100% 含该键）。

❌ `UPDATE api_logs SET status_code = json_extract(meta,'$.status_code')`
—— `json_extract` 是 **SQLite 专有**，PG 上语法错误（设计 51 §2.4 明令禁用）。

✅ 改为**读一页 → 在 Python 里取键 → 分块写回**（每块 5000 行）：两个方言
走**同一条代码路径**，且不依赖驱动把 JSON 当什么类型（SQLAlchemy 的 `JSON`
列在 PG 上是 `json`、在 SQLite 上是 TEXT，读出都已是 Python dict）。

## 回填率判据：≥99%，否则 raise

设计 57 S1 的验收项。为什么不是 100%：`meta` 允许为 NULL（列本身可空），
历史行是真有可能没这个键的 —— 缺值口径「不造假」，留 NULL 而不是写 0。
但**大量**缺失说明提取逻辑错了（键名写错 / 取的是字符串不是 dict），
那种情况必须**炸出来**而不是静默留一堆 NULL。

⚠️ 分子的口径 = 「回填后 `status_code IS NOT NULL` 的行数 / `meta` 非空的行数」。
用 `meta` 非空作分母，是因为 `meta` 为 NULL 的行**本来就没有来源**。

Revision ID: b2c3d4e5f6a0
Revises: a1b2c3d4e5f9
"""

from __future__ import annotations

import json
import logging

import sqlalchemy as sa

from alembic import op

revision = "b2c3d4e5f6a0"
down_revision = "a1b2c3d4e5f9"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

#: 每块写回的行数（设计 51 §3.2 给的口径：70k 行 ≈ 8-15s）
BACKFILL_CHUNK = 5000
#: 最低回填率（设计 57 S1 验收判据）
MIN_BACKFILL_RATIO = 0.99


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)}


def _indexes(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {index["name"] for index in inspector.get_indexes(table)}


def _enable_foreign_keys() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def _create_dict_tables() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "dict_types" not in tables:
        op.create_table(
            "dict_types",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("code", sa.String(length=64), nullable=False),
            sa.Column("remark", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="normal"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('normal', 'disabled')", name="ck_dict_types_status"
            ),
        )
    if "ix_dict_types_code" not in _indexes("dict_types"):
        op.create_index("ix_dict_types_code", "dict_types", ["code"], unique=True)

    if "dict_items" not in tables:
        op.create_table(
            "dict_items",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column(
                "type_id",
                sa.Integer(),
                sa.ForeignKey("dict_types.id", ondelete="CASCADE"),
                nullable=False,
            ),
            #: 冗余列（免 join），写入时由 `type_id` 派生
            sa.Column("type_code", sa.String(length=64), nullable=False),
            sa.Column("label", sa.String(length=64), nullable=False),
            sa.Column("value", sa.String(length=64), nullable=False),
            sa.Column(
                "list_class", sa.String(length=16), nullable=False, server_default="default"
            ),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="normal"),
            sa.Column("remark", sa.String(length=255), nullable=False, server_default=""),
            sa.UniqueConstraint("type_id", "value", name="uq_dict_items_type_value"),
            sa.CheckConstraint("status IN ('normal', 'disabled')", name="ck_dict_items_status"),
            sa.CheckConstraint(
                "list_class IN ('default', 'success', 'warning', 'danger', 'info')",
                name="ck_dict_items_list_class",
            ),
        )
    if "ix_dict_items_code" not in _indexes("dict_items"):
        op.create_index("ix_dict_items_code", "dict_items", ["type_code"], unique=False)


def _meta_status_code(raw: object) -> int | None:
    """从 `api_logs.meta` 取 `status_code`。取不到 / 不是合理整数 ⇒ `None`（不造假）。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict):
        return None
    value = raw.get("status_code")
    if isinstance(value, bool):  # bool 是 int 的子类，先排掉
        return None
    if isinstance(value, int):
        return value if 100 <= value <= 599 else None
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if 100 <= number <= 599 else None
    return None


def _backfill_status_codes() -> None:
    """回填 + 判回填率。⚠️ 只写 `status_code IS NULL` 的行（幂等）。"""
    bind = op.get_bind()
    meta = sa.MetaData()
    api_logs = sa.Table("api_logs", meta, autoload_with=bind)

    total = bind.execute(
        sa.select(sa.func.count()).select_from(api_logs).where(api_logs.c.meta.is_not(None))
    ).scalar_one()
    if not total:
        logger.info("api_logs.status_code: meta 全为空，无需回填")
        return

    filled = 0
    last_id = 0
    while True:
        rows = bind.execute(
            sa.select(api_logs.c.id, api_logs.c.meta)
            .where(api_logs.c.status_code.is_(None), api_logs.c.id > last_id)
            .order_by(api_logs.c.id)
            .limit(BACKFILL_CHUNK)
        ).all()
        if not rows:
            break
        payload: list[dict[str, object]] = []
        for row in rows:
            last_id = row.id
            code = _meta_status_code(row.meta)
            if code is None:
                continue
            payload.append({"id": row.id, "status_code": code})
        if payload:
            #: **分块** UPDATE（两方言同一路径）。
            #:
            #: ⚠️ **2026-10-23 实测踩坑**：初版写 `bind.execute(sa.update(api_logs), payload)`，
            #: 而 payload 里带 `id` ⇒ 生成的是
            #: `UPDATE api_logs SET id=%(id)s, status_code=%(status_code)s`
            #: —— `id` 被当成**待更新列**而不是 WHERE 条件，于是每一行都被改写成
            #: 自己的 id 值，PG 立刻报 `duplicate key (id)=(1) already exists`。
            #: 单个 execute 传 dict 列表时 SQLAlchemy **不会**猜出主键去做 WHERE，
            #: 必须用 `where(... == bindparam("_id"))` 显式指定。
            #: 取 `id` 时 payload 存到 `_id`，避免与 SET 子句的列名撞。
            stmt = (
                sa.update(api_logs)
                .where(api_logs.c.id == sa.bindparam("_id"))
                .values(status_code=sa.bindparam("_status_code"))
            )
            bind.execute(
                stmt,
                [{"_id": item["id"], "_status_code": item["status_code"]} for item in payload],
            )
            filled += len(payload)

    remaining = bind.execute(
        sa.select(sa.func.count())
        .select_from(api_logs)
        .where(api_logs.c.meta.is_not(None), api_logs.c.status_code.is_(None))
    ).scalar_one()
    covered = total - remaining
    ratio = covered / total if total else 1.0
    logger.info(
        "api_logs.status_code 回填：%s / %s（%.2f%%），本次写入 %s 行",
        covered,
        total,
        ratio * 100,
        filled,
    )
    if ratio < MIN_BACKFILL_RATIO:
        raise RuntimeError(
            f"api_logs.status_code 回填率 {ratio:.2%} < {MIN_BACKFILL_RATIO:.0%}"
            f"（{covered}/{total}）—— 提取逻辑可能错了（键名或 JSON 形态），"
            "不允许带着大量空列继续升级"
        )


def upgrade() -> None:
    _enable_foreign_keys()
    _create_dict_tables()

    # ---------------- api_logs.status_code ----------------
    if "status_code" not in _columns("api_logs"):
        op.add_column("api_logs", sa.Column("status_code", sa.Integer(), nullable=True))
    if "ix_api_logs_status_code" not in _indexes("api_logs"):
        op.create_index("ix_api_logs_status_code", "api_logs", ["status_code"], unique=False)
    _backfill_status_codes()

    # ---------------- notifications.link ----------------
    if "link" not in _columns("notifications"):
        op.add_column("notifications", sa.Column("link", sa.String(length=255), nullable=True))
    #: ⚠️ `link` **不回填**：存量的 4 条通知里 3 条 `source_id` 为 NULL，历史上
    #: 本来就没有可靠跳转目标 —— 补一个按 `source_type` 猜的路径是造假。
    #: 前端 `lib/notification-routes.ts` 的映射表继续当兜底。


def downgrade() -> None:
    _enable_foreign_keys()
    if "link" in _columns("notifications"):
        op.drop_column("notifications", "link")

    if "ix_api_logs_status_code" in _indexes("api_logs"):
        op.drop_index("ix_api_logs_status_code", table_name="api_logs")
    if "status_code" in _columns("api_logs"):
        op.drop_column("api_logs", "status_code")

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    #: 先删子表（`dict_items` 有指向 `dict_types` 的外键）
    if "dict_items" in tables:
        op.drop_table("dict_items")
    if "dict_types" in tables:
        op.drop_table("dict_types")
