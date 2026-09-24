"""账号与笔记落库唯一性（台账 §4.9 · 不变量 I1 / I2 / I3）

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-20

本迁移做四件事，**顺序不可换**（先清重复，再建索引；否则索引建不上）：

1. `platform_accounts` 加 `deleted_at`（软删列，I3）；
2. **存量去重（保留最新）** —— 账号按 `(user_id, platform, sub_type, external_user_id)`、
   笔记按 `(user_id, note_id)`；被删行的**从属数据全部改挂到保留行**，不是丢数据；
3. 建 **部分唯一索引** `uq_platform_accounts_identity`（`external_user_id` 为空的行不参与，
   因为「空」不构成身份，参与会把所有空 id 的账号锁成一条）；
4. 建唯一索引 `uq_notes_user_note`（`(user_id, note_id)`，与
   `batch_save_notes` / `_save_normalized_notes` 的既有 upsert 键完全一致）。

---

⚠️ **本迁移必须幂等可续跑，这不是洁癖，是踩出来的**（2026-09-20 实测）：

SQLite 的 DDL **不在事务里**（alembic 自己会打印 `Will assume non-transactional DDL`），
所以一旦中途失败，`ALTER TABLE ADD COLUMN` **已经生效**，而 `alembic_version` **没有推进**。
下一次启动会**从同一个 revision 重头再跑一遍**，于是在 `ADD COLUMN` 上
`duplicate column name: deleted_at` 失败 → **应用启动失败 → 监督进程 5 秒后重启 → 再失败**，
形成**崩溃循环**（实测 `uvicorn exited (code 3)` 连续十余次）。

所以：加列前查 `PRAGMA table_info`，建索引用 `CREATE ... IF NOT EXISTS`，
去重本身天然幂等（没有重复就是空操作）。
"""
from alembic import op
import sqlalchemy as sa

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# 幂等工具
# ---------------------------------------------------------------------------


def _rows(conn, sql, **params):
    return conn.execute(sa.text(sql), params).fetchall()


def _run(conn, sql, **params):
    conn.execute(sa.text(sql), params)


def _has_column(conn, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(sa.text(f"PRAGMA table_info({table})")))


def _has_index(conn, name: str) -> bool:
    return (
        conn.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = :n"),
            {"n": name},
        ).fetchone()
        is not None
    )


# ---------------------------------------------------------------------------
# 存量去重（保留最新）
# ---------------------------------------------------------------------------


def _dedupe_bindings(conn) -> int:
    """同一 `(shop_id_fk, platform_account_id)` 只留**最新**一条绑定。

    删一条绑定前必须先把 `shop_note_goods` 改挂到保留的那条，
    否则迁移会造出一批指向不存在绑定的商品行（自己制造孤儿）。
    """
    seen: dict[tuple, int] = {}
    removed = 0
    for shop_fk, account_id, binding_id in _rows(
        conn,
        "SELECT shop_id_fk, platform_account_id, id FROM shop_account_bindings ORDER BY id DESC",
    ):
        key = (shop_fk, account_id)
        if key in seen:
            _run(
                conn,
                "UPDATE shop_note_goods SET binding_id = :keep WHERE binding_id = :drop",
                keep=seen[key],
                drop=binding_id,
            )
            _run(conn, "DELETE FROM shop_account_bindings WHERE id = :drop", drop=binding_id)
            removed += 1
        else:
            seen[key] = binding_id
    return removed


def _dedupe_shop_note_goods(conn) -> int:
    """同一 `(binding_id, note_id)` 只留**最新**一条。"""
    seen: set[tuple] = set()
    removed = 0
    for binding_id, note_id, goods_id in _rows(
        conn,
        "SELECT binding_id, note_id, id FROM shop_note_goods ORDER BY id DESC",
    ):
        key = (binding_id, note_id)
        if key in seen:
            _run(conn, "DELETE FROM shop_note_goods WHERE id = :drop", drop=goods_id)
            removed += 1
        else:
            seen.add(key)
    return removed


def _move_bindings(conn, loser: int, survivor: int) -> None:
    """把 loser 的绑定改挂到 survivor —— **撞唯一约束时先合并**。

    ⚠️ 这里不能直接 `UPDATE ... SET platform_account_id = survivor`：
    `shop_account_bindings` 的唯一键是 `(shop_id_fk, platform_account_id)`，
    而多个重复账号**往往绑的是同一个店**（实测：id 9/10/14 都绑 shop 1）。
    直接改挂会让它们在新键上互相冲突并抛 `IntegrityError`
    —— 这正是本迁移第一次跑挂掉的原因（`UNIQUE constraint failed:
    shop_account_bindings.shop_id_fk, shop_account_bindings.platform_account_id`）。

    正确做法：survivor 上已有同店绑定 → 把 loser 那条的 `shop_note_goods`
    改挂过去再删掉它（同店同账号本来就只有一条逻辑绑定）；否则才改挂。
    """
    for binding_id, shop_fk in _rows(
        conn,
        "SELECT id, shop_id_fk FROM shop_account_bindings WHERE platform_account_id = :l",
        l=loser,
    ):
        twin = conn.execute(
            sa.text(
                """
                SELECT id FROM shop_account_bindings
                WHERE platform_account_id = :s AND shop_id_fk IS :f
                LIMIT 1
                """
            ),
            {"s": survivor, "f": shop_fk},
        ).fetchone()
        if twin is not None:
            _run(
                conn,
                "UPDATE shop_note_goods SET binding_id = :keep WHERE binding_id = :drop",
                keep=twin[0],
                drop=binding_id,
            )
            _run(conn, "DELETE FROM shop_account_bindings WHERE id = :drop", drop=binding_id)
        else:
            _run(
                conn,
                "UPDATE shop_account_bindings SET platform_account_id = :s WHERE id = :b",
                s=survivor,
                b=binding_id,
            )


def _dedupe_accounts(conn) -> tuple[int, int]:
    """账号去重，**保留最新**（`max(id)`）。返回（重复组数, 删除行数）。"""
    groups = _rows(
        conn,
        """
        SELECT user_id, platform, sub_type, external_user_id
        FROM platform_accounts
        WHERE external_user_id IS NOT NULL AND external_user_id <> ''
        GROUP BY user_id, platform, sub_type, external_user_id
        HAVING COUNT(*) > 1
        """,
    )
    removed = 0
    for user_id, platform, sub_type, external_id in groups:
        ids = [
            row[0]
            for row in _rows(
                conn,
                """
                SELECT id FROM platform_accounts
                WHERE user_id = :u AND platform = :p
                  AND sub_type IS :s AND external_user_id = :e
                ORDER BY id ASC
                """,
                u=user_id,
                p=platform,
                s=sub_type,
                e=external_id,
            )
        ]
        survivor, losers = ids[-1], ids[:-1]
        for loser in losers:
            # 笔记改挂：**只动 `platform_account_id`，不动唯一键 (user_id, note_id)**，
            # 所以在 `_dedupe_notes` 之后不可能撞约束（调用顺序保证了这一点）。
            _run(
                conn,
                "UPDATE notes SET platform_account_id = :s WHERE platform_account_id = :l",
                s=survivor,
                l=loser,
            )
            _move_bindings(conn, loser, survivor)
            # Cookie 不留：它是账号身份的凭据，行合并后由保留行自己的一版生效
            _run(conn, "DELETE FROM account_cookie_versions WHERE platform_account_id = :l", l=loser)
            _run(conn, "DELETE FROM platform_accounts WHERE id = :l", l=loser)
            removed += 1
    return len(groups), removed


def _dedupe_notes(conn) -> tuple[int, int]:
    """笔记去重，**保留最新**（`max(id)`）；子表（素材 / 评论 / 标签）改挂。"""
    groups = _rows(
        conn,
        "SELECT user_id, note_id FROM notes GROUP BY user_id, note_id HAVING COUNT(*) > 1",
    )
    removed = 0
    for user_id, note_id in groups:
        ids = [
            row[0]
            for row in _rows(
                conn,
                "SELECT id FROM notes WHERE user_id = :u AND note_id = :n ORDER BY id ASC",
                u=user_id,
                n=note_id,
            )
        ]
        keeper, losers = ids[-1], ids[:-1]
        for loser in losers:
            _run(conn, "UPDATE note_assets SET note_id = :k WHERE note_id = :l", k=keeper, l=loser)
            _run(conn, "UPDATE note_comments SET note_id = :k WHERE note_id = :l", k=keeper, l=loser)
            # note_tags 的 PK 是 (note_id, tag_id)：先清掉会与保留行撞主键的，再整体改挂
            _run(
                conn,
                """
                DELETE FROM note_tags
                WHERE note_id = :l
                  AND tag_id IN (SELECT tag_id FROM note_tags WHERE note_id = :k)
                """,
                k=keeper,
                l=loser,
            )
            _run(conn, "UPDATE note_tags SET note_id = :k WHERE note_id = :l", k=keeper, l=loser)
            _run(conn, "DELETE FROM notes WHERE id = :l", l=loser)
            removed += 1
    return len(groups), removed


# ---------------------------------------------------------------------------
# 迁移主体
# ---------------------------------------------------------------------------


def upgrade() -> None:
    conn = op.get_bind()

    # ① 软删列（I3）——可空：NULL = 未删除。幂等：列已存在（上次半途失败留下的）就跳过。
    if not _has_column(conn, "platform_accounts", "deleted_at"):
        op.add_column(
            "platform_accounts",
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
        )

    # ② 存量去重（保留最新）——必须在建索引之前，否则索引建不上。
    #
    # **顺序是有原因的**：先笔记、再商品、再绑定，最后才合并账号。
    # `_dedupe_accounts` 会改 `platform_account_id`，而它是绑定表唯一键的一部分
    # （笔记表不是），所以把「可能撞键的活」全安排在账号合并之前，
    # 账号合并阶段改挂笔记就绝不会冲突。
    _dedupe_notes(conn)
    _dedupe_shop_note_goods(conn)
    _dedupe_bindings(conn)
    _dedupe_accounts(conn)
    # 收尾再跑一次：账号合并后可能又出现同店同账号的绑定
    _dedupe_bindings(conn)

    # ③ 账号身份唯一（**部分索引**：空 external_user_id 不参与）
    _run(
        conn,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_accounts_identity
        ON platform_accounts (user_id, platform, sub_type, external_user_id)
        WHERE external_user_id IS NOT NULL AND external_user_id <> ''
        """,
    )

    # ④ 笔记身份唯一（与代码侧 upsert 键一致：(user_id, note_id)）
    _run(
        conn,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_notes_user_note
        ON notes (user_id, note_id)
        """,
    )


def downgrade() -> None:
    conn = op.get_bind()
    _run(conn, "DROP INDEX IF EXISTS uq_notes_user_note")
    _run(conn, "DROP INDEX IF EXISTS uq_platform_accounts_identity")
    if _has_column(conn, "platform_accounts", "deleted_at"):
        with op.batch_alter_table("platform_accounts") as batch:
            batch.drop_column("deleted_at")
