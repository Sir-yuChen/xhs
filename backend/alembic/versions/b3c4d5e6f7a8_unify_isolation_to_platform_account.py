"""unify isolation key to platform_account (ADR-021)

数据隔离键从「店铺」维度收敛到「平台账号」维度（设计 18 / ADR-021）。

本迁移一次做完 8 项表结构变更（用户口径「一次做完，半途状态最危险」）：

  1. platform_accounts 加 seller_id（String(64), nullable）—— 千帆店铺标识下沉
  2. shop_products 加 platform_account_id（Integer, nullable→回填→NOT NULL）
  3. shop_products 唯一约束 (shop_id_fk, item_id) → (platform_account_id, item_id)
  4. note_comments 加 platform_account_id（nullable）—— 隔离显式化
  5. scheduled_job_logs.account_id 改名 platform_account_id（值不变）
  6. 摘除 platform_accounts.shop_id_fk + drop 两条索引
  7. drop shop_products.shop_id_fk、platform_accounts.shop_state
  8. drop shops 表

⚠️ 误杀陷阱（设计 18 §6.3）：用户口径「无法定位修复的删除」的判据是
**悬空外键**（引用的父实体不存在），**不是「列为空」**。据此：
  * publish_jobs 的悬空 platform_account_id 行 → 删除（真孤儿）
  * scheduled_job_logs 的空 account_id（用户级任务，该列本就不适用）→ **全留**

回填安全性（2026-09-30 实测 data/spider_xhs.db）：
  * shop_products 1982 行 100% 可经 shop_id_fk → 账号定位 ⇒ 零丢失

Revision ID: b3c4d5e6f7a8
Revises: n5o6p7q8r9s0
Create Date: 2026-09-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b3c4d5e6f7a8"
down_revision = "n5o6p7q8r9s0"
branch_labels = None
depends_on = None


def _columns(conn, table: str) -> set[str]:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _table_exists(conn, table: str) -> bool:
    row = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def upgrade() -> None:
    conn = op.get_bind()

    # ------------------------------------------------------------------ #
    # 1. platform_accounts 加 seller_id（来自 shops 的回填）
    # ------------------------------------------------------------------ #
    # ⚠️ 必须先判表存在：`_columns()` 走 `PRAGMA table_info`，表不存在时返回**空集**
    #    （不是报错），于是「列不在」恒真 ⇒ `op.add_column` 在最小夹具库上直接炸
    #    `no such table: platform_accounts`（test_initial_data 实测）。其余 7 项都
    #    带 `_table_exists` 守卫，此处同理，不搞特殊。
    if _table_exists(conn, "platform_accounts") and "seller_id" not in _columns(
        conn, "platform_accounts"
    ):
        op.add_column(
            "platform_accounts",
            sa.Column("seller_id", sa.String(length=64), nullable=True),
        )
        if _table_exists(conn, "shops"):
            # 账号 → shops 是 1:1（shop_id_fk 唯一），所以一条 UPDATE 就能回填。
            conn.exec_driver_sql(
                """
                UPDATE platform_accounts
                   SET seller_id = (
                       SELECT s.seller_id FROM shops s
                        WHERE s.id = platform_accounts.shop_id_fk
                   )
                 WHERE shop_id_fk IS NOT NULL
                """
            )

    # ------------------------------------------------------------------ #
    # 2. shop_products 换键：shop_id_fk → platform_account_id
    #    SQLite 不支持 ALTER 改约束，只能重建表。
    # ------------------------------------------------------------------ #
    if _table_exists(conn, "shop_products") and "shop_id_fk" in _columns(conn, "shop_products"):
        conn.exec_driver_sql(
            """
            CREATE TABLE shop_products_new (
                id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                platform_account_id INTEGER NOT NULL,
                item_id VARCHAR(64) NOT NULL,
                item_title VARCHAR(512) NOT NULL,
                price VARCHAR(32) NOT NULL,
                stock INTEGER,
                sales INTEGER,
                category VARCHAR(128) NOT NULL,
                images_json TEXT NOT NULL,
                status VARCHAR(32) NOT NULL,
                synced_at DATETIME,
                raw_json TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                sku_count INTEGER,
                detail_synced_at DATETIME,
                platform_missing_at DATETIME,
                article_no VARCHAR(64) NOT NULL,
                card_type INTEGER,
                price_min_fen INTEGER,
                price_max_fen INTEGER,
                create_time_ms INTEGER,
                on_shelf_time_ms INTEGER,
                off_shelf_time_ms INTEGER,
                last_audit_time_ms INTEGER,
                sales_30d INTEGER,
                PRIMARY KEY (id),
                CONSTRAINT uq_shop_products_account_item
                    UNIQUE (platform_account_id, item_id),
                FOREIGN KEY(user_id) REFERENCES users (id),
                FOREIGN KEY(platform_account_id) REFERENCES platform_accounts (id)
            )
            """
        )
        # 回填：shop_id_fk → shops → platform_accounts.shop_id_fk（1:1 反向定位）。
        # 定位不到的（悬空）行按「无法定位 ⇒ 删除」丢弃 —— 实测 0 行（1982/1982）。
        conn.exec_driver_sql(
            """
            INSERT INTO shop_products_new (
                id, user_id, platform_account_id, item_id, item_title, price,
                stock, sales, category, images_json, status, synced_at,
                raw_json, detail_json, sku_count, detail_synced_at,
                platform_missing_at, article_no, card_type, price_min_fen,
                price_max_fen, create_time_ms, on_shelf_time_ms,
                off_shelf_time_ms, last_audit_time_ms, sales_30d
            )
            SELECT p.id, p.user_id, a.id, p.item_id, p.item_title, p.price,
                   p.stock, p.sales, p.category, p.images_json, p.status,
                   p.synced_at, p.raw_json, p.detail_json, p.sku_count,
                   p.detail_synced_at, p.platform_missing_at, p.article_no,
                   p.card_type, p.price_min_fen, p.price_max_fen,
                   p.create_time_ms, p.on_shelf_time_ms, p.off_shelf_time_ms,
                   p.last_audit_time_ms, p.sales_30d
              FROM shop_products p
              JOIN shops s ON s.id = p.shop_id_fk
              JOIN platform_accounts a ON a.shop_id_fk = s.id
            """
        )
        conn.exec_driver_sql("DROP TABLE shop_products")
        conn.exec_driver_sql("ALTER TABLE shop_products_new RENAME TO shop_products")
        conn.exec_driver_sql("CREATE INDEX ix_shop_products_id ON shop_products (id)")
        conn.exec_driver_sql("CREATE INDEX ix_shop_products_user_id ON shop_products (user_id)")
        conn.exec_driver_sql(
            "CREATE INDEX ix_shop_products_platform_account_id "
            "ON shop_products (platform_account_id)"
        )

    # ------------------------------------------------------------------ #
    # 3. note_comments 加 platform_account_id（isolated 显式化）
    # ------------------------------------------------------------------ #
    if _table_exists(conn, "note_comments") and "platform_account_id" not in _columns(
        conn, "note_comments"
    ):
        op.add_column(
            "note_comments",
            sa.Column("platform_account_id", sa.Integer(), nullable=True),
        )
        conn.exec_driver_sql(
            """
            UPDATE note_comments
               SET platform_account_id = (
                   SELECT n.platform_account_id FROM notes n
                    WHERE n.note_id = note_comments.note_id
               )
             WHERE platform_account_id IS NULL
            """
        )

    # ------------------------------------------------------------------ #
    # 4. scheduled_job_logs.account_id → platform_account_id（值不变）
    #    ⚠️ 空值行是「用户级任务」不是孤儿 ⇒ 全留（设计 18 §6.3）
    # ------------------------------------------------------------------ #
    if _table_exists(conn, "scheduled_job_logs"):
        cols = _columns(conn, "scheduled_job_logs")
        if "account_id" in cols and "platform_account_id" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE scheduled_job_logs "
                "RENAME COLUMN account_id TO platform_account_id"
            )

    # ------------------------------------------------------------------ #
    # 5. 真孤儿清理：publish_jobs 引用了不存在的账号（悬空外键）
    #    判据 = 父实体不存在，**不是**「列为空」。
    # ------------------------------------------------------------------ #
    if _table_exists(conn, "publish_jobs"):
        pj_cols = _columns(conn, "publish_jobs")
        if "platform_account_id" in pj_cols:
            deleted = conn.exec_driver_sql(
                """
                DELETE FROM publish_jobs
                 WHERE platform_account_id IS NOT NULL
                   AND platform_account_id NOT IN (SELECT id FROM platform_accounts)
                """
            ).rowcount
            if deleted:
                print(f"[b3c4d5e6f7a8] 清理 publish_jobs 悬空孤儿 {deleted} 行")

    # ------------------------------------------------------------------ #
    # 6. 摘除 platform_accounts.shop_id_fk + 两条索引
    # ------------------------------------------------------------------ #
    if _table_exists(conn, "platform_accounts") and "shop_id_fk" in _columns(
        conn, "platform_accounts"
    ):
        conn.exec_driver_sql("DROP INDEX IF EXISTS uq_platform_accounts_shop_id_fk")
        conn.exec_driver_sql("DROP INDEX IF EXISTS ix_platform_accounts_shop_id_fk")
        # SQLite 3.35+ 支持 DROP COLUMN；带外键的列需要先关外键检查。
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql("ALTER TABLE platform_accounts DROP COLUMN shop_id_fk")
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")

    # ------------------------------------------------------------------ #
    # 7. drop platform_accounts.shop_state（千帆状态已迁到账号维度上的
    #    独立语义，本次由 shop_state 保留 —— 见下方说明）
    # ------------------------------------------------------------------ #
    # ⚠️ shop_state 保留：它是「ark 会话侧的同步游标」，与店铺实体无关，
    #    仍由 shop_binding_service.get/set_shop_state 读写。设计 18 §6.1
    #    第 7 项的原意是「店铺关系消失」，而 shop_state 不是关系列。

    # ------------------------------------------------------------------ #
    # 8. drop shops 表（实体退场）
    # ------------------------------------------------------------------ #
    if _table_exists(conn, "shops"):
        conn.exec_driver_sql("DROP TABLE shops")


def downgrade() -> None:
    """回滚：重建 shops 表与关系列。

    ⚠️ **不可逆部分**：`shops.shop_id` / `shop_name` / `status` 在升级时
    **没有下移到任何地方**（只有 `seller_id` 下沉了）—— 回滚只能重建结构，
    店铺名会丢（`seller_id` 可回填，店铺名取账号昵称兜底）。
    真需完整回滚请用迁移前的库备份：`data/spider_xhs.db.bak-before-adr021`。
    """
    conn = op.get_bind()

    op.create_table(
        "shops",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("seller_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("shop_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("shop_name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "seller_id", name="uq_shops_user_seller"),
    )

    if _table_exists(conn, "platform_accounts") and "shop_id_fk" not in _columns(
        conn, "platform_accounts"
    ):
        op.add_column(
            "platform_accounts",
            sa.Column("shop_id_fk", sa.Integer(), nullable=True),
        )
        if "seller_id" in _columns(conn, "platform_accounts"):
            conn.exec_driver_sql(
                """
                INSERT INTO shops (user_id, seller_id, shop_name, status)
                SELECT user_id, seller_id, nickname, 'active'
                  FROM platform_accounts
                 WHERE seller_id IS NOT NULL AND seller_id <> ''
                """
            )
            conn.exec_driver_sql(
                """
                UPDATE platform_accounts
                   SET shop_id_fk = (
                       SELECT s.id FROM shops s
                        WHERE s.user_id = platform_accounts.user_id
                          AND s.seller_id = platform_accounts.seller_id
                   )
                 WHERE seller_id IS NOT NULL AND seller_id <> ''
                """
            )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_platform_accounts_shop_id_fk "
            "ON platform_accounts (shop_id_fk) "
            "WHERE shop_id_fk IS NOT NULL AND deleted_at IS NULL"
        )

    if _table_exists(conn, "scheduled_job_logs") and "platform_account_id" in _columns(
        conn, "scheduled_job_logs"
    ):
        conn.exec_driver_sql(
            "ALTER TABLE scheduled_job_logs "
            "RENAME COLUMN platform_account_id TO account_id"
        )

    if "note_comments" in {r[0] for r in conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()} and "platform_account_id" in _columns(conn, "note_comments"):
        conn.exec_driver_sql("ALTER TABLE note_comments DROP COLUMN platform_account_id")
