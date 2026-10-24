"""账号身份唯一改为**按小红书 ID**（同一账号只允许一行）

Revision ID: g6h7i8j9k0l1
Revises: f5a6b7c8d9e0
Create Date: 2026-09-26

## 为什么改（用户实测，2026-09-26）

同一个账号先按「PC」扫码、再按「创作者平台」扫码，`platform_accounts` 会留下
**两行** —— 同 `external_user_id`（真实库实测 `5bdc4dddd7773d0001d028f2`）、
同昵称、同小红书号（`profile_json.red_id`），界面上看起来就是
「两个相同的账号信息」，只差一个「类型」。

原因是部分唯一索引 `uq_platform_accounts_identity` 的键**含 `sub_type`**
（`pc` / `creator` 各算一个身份），于是这种重复在 DB 层是**合法**的。

用户口径：「数据库任何场景下账号信息表，根据账号小红书 ID 均不允许出现重复
多行账号信息」。⇒ 键去掉 `sub_type`，改 `(user_id, platform, external_user_id)`。

**链路不会因此丢失**：`sub_type` 仍是单列事实，合并时取更强的那条
（`creator` > `qianfan` > `pc`；取 `pc` 会静默丢掉发布能力，发布闸门要求
`sub_type == "creator"`），两条链路的令牌本来就各在自己的通道里。

## 存量怎么处理（**不丢数据**）

每组重复行保留 `max(id)`（最近使用的那行），其余行：

* `sub_type` 按能力**合并**到保留行；
* 令牌 `account_cookie_versions` **改挂**到保留行 —— 两条链路是本账号的两份
  登录态，删掉等于把用户刚补的登录态扔掉；
* 引用（`notes` / `auto_tasks` / `monitoring_targets` / `publish_jobs` /
  `shop_note_goods`）改挂；`shop_note_goods` 的唯一键是
  `(platform_account_id, note_id)`，先清掉会撞键的行再整体改挂
  （照 `d4e5f6a7b8c9` 的既有范式）；
* 保留行没有店铺而将被删的行有 ⇒ **把店铺关系搬过去**（顺序：先删行再赋值，
  否则两行同时引用同一个店会撞 `uq_platform_accounts_shop_id_fk` 部分唯一索引）。

## 幂等

SQLite 的 DDL 不在事务里（同 `d4e5f6a7b8c9` 踩过的坑）：中途失败时
`alembic_version` 不推进、下次从同一 revision 重跑。所以去重天然幂等
（没有重复就是空操作），建索引用 `CREATE ... IF NOT EXISTS`。
"""
from alembic import op
import sqlalchemy as sa

from backend.app.core.migration_introspect import table_exists

revision = "g6h7i8j9k0l1"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


#: 链路**能力**强弱。与 `backend/app/core/account_types.py::LINK_RANK` 同口径；
#: 这里内联一份是为了让本迁移**自包含** —— 未来重构业务代码不该让一条已经跑过的
#: 迁移失效（迁移一旦失败就是「应用起不来」）。
_LINK_RANK: dict[str, int] = {"pc": 1, "qianfan": 2, "creator": 3}


# ---------------------------------------------------------------------------
# 幂等工具（与 d4e5f6a7b8c9 同形）
# ---------------------------------------------------------------------------


def _rows(conn, sql, **params):
    return conn.execute(sa.text(sql), params).fetchall()


def _run(conn, sql, **params):
    conn.execute(sa.text(sql), params)


def _has_table(conn, name: str) -> bool:
    """表在不在。

    ⚠️ **必须有这道闸**：项目里有几条迁移用例是在**手工建的简化库**上
    `stamp <中间 revision>` 再 `upgrade head` 的（只建 `users` / `auto_tasks`
    等被那条迁移用到的表）。少了它，本迁移会因为「`platform_accounts` 不存在」
    直接炸，而那与账号身份这件事本身毫无关系。
    """
    #: v1.1.0 PG 化：原为经 `_rows` 的 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    return table_exists(conn, name)


def _reassign(conn, table: str, column: str, survivor: int, loser: int) -> None:
    """把 `table.column` 里指向被删行的引用改挂到保留行（表不存在就跳过）。"""
    if not _has_table(conn, table):
        return
    # 表名 / 列名都是本文件里的字面量（非外部输入），f-string 在这里是安全的。
    _run(conn, f"UPDATE {table} SET {column} = :s WHERE {column} = :l", s=survivor, l=loser)


# ---------------------------------------------------------------------------
# 存量合并
# ---------------------------------------------------------------------------


def _pick_strongest_sub_type(rows) -> str:
    """一组 `(id, sub_type)` 里能力最强的那个（未知 / 空按最弱处理）。"""
    best = ""
    best_rank = -1
    for _row_id, sub_type in rows:
        text = (sub_type or "").strip()
        if not text:
            continue
        rank = _LINK_RANK.get(text, 0)
        if rank > best_rank:
            best, best_rank = text, rank
    return best


def _carry_shop_reference(conn, loser: int, survivor: int) -> tuple[int, str] | None:
    """保留行没有店、被删行有 ⇒ 返回要搬过去的 `(shop_id_fk, shop_state)`。

    必须在**删行之前**读（删了就读不到了），但在**删行之后**才写
    （否则两行同时引用同一个店，撞 `uq_platform_accounts_shop_id_fk`）。
    """
    survivor_row = _rows(conn, "SELECT shop_id_fk FROM platform_accounts WHERE id = :i", i=survivor)
    if not survivor_row or survivor_row[0][0] is not None:
        return None
    loser_row = _rows(
        conn,
        "SELECT shop_id_fk, shop_state FROM platform_accounts WHERE id = :i",
        i=loser,
    )
    if not loser_row or loser_row[0][0] is None:
        return None
    return int(loser_row[0][0]), (loser_row[0][1] or "{}")


def _merge_duplicate_accounts(conn) -> tuple[int, int]:
    """按 `(user_id, platform, external_user_id)` 合并重复账号。返回（组数, 删除行数）。"""
    if not _has_table(conn, "platform_accounts"):
        return 0, 0
    groups = _rows(
        conn,
        """
        SELECT user_id, platform, external_user_id
        FROM platform_accounts
        WHERE external_user_id IS NOT NULL AND external_user_id <> ''
        GROUP BY user_id, platform, external_user_id
        HAVING COUNT(*) > 1
        """,
    )
    removed = 0
    for user_id, platform, external_id in groups:
        rows = _rows(
            conn,
            """
            SELECT id, sub_type FROM platform_accounts
            WHERE user_id = :u AND platform = :p AND external_user_id = :e
            ORDER BY id ASC
            """,
            u=user_id,
            p=platform,
            e=external_id,
        )
        ids = [row[0] for row in rows]
        survivor, losers = ids[-1], ids[:-1]

        # ① 链路合并（取最强）：放在最前面，保留行的身份表达能力先补齐。
        strongest = _pick_strongest_sub_type(rows)
        if strongest:
            _run(
                conn,
                "UPDATE platform_accounts SET sub_type = :s WHERE id = :i",
                s=strongest,
                i=survivor,
            )

        for loser in losers:
            carry_shop = _carry_shop_reference(conn, loser, survivor)

            # ② 引用改挂：只动 `platform_account_id`（各表唯一键里没有它，
            #    除了 shop_note_goods —— 那一张单独处理）。
            _run(
                conn,
                "UPDATE notes SET platform_account_id = :s WHERE platform_account_id = :l",
                s=survivor,
                l=loser,
            )
            _run(
                conn,
                "UPDATE monitoring_targets SET platform_account_id = :s "
                "WHERE platform_account_id = :l",
                s=survivor,
                l=loser,
            )
            _run(
                conn,
                "UPDATE publish_jobs SET platform_account_id = :s WHERE platform_account_id = :l",
                s=survivor,
                l=loser,
            )
            _run(
                conn,
                "UPDATE auto_tasks SET pc_account_id = :s WHERE pc_account_id = :l",
                s=survivor,
                l=loser,
            )
            _run(
                conn,
                "UPDATE auto_tasks SET creator_account_id = :s WHERE creator_account_id = :l",
                s=survivor,
                l=loser,
            )

            # ③ 令牌改挂（**不删**）：两条链路是本账号的两份登录态。
            _run(
                conn,
                "UPDATE account_cookie_versions SET platform_account_id = :s "
                "WHERE platform_account_id = :l",
                s=survivor,
                l=loser,
            )

            # ④ 商品映射：唯一键 `(platform_account_id, note_id)` ⇒ 先清撞键的再整体改挂。
            _run(
                conn,
                """
                DELETE FROM shop_note_goods
                WHERE platform_account_id = :l
                  AND note_id IN (
                      SELECT note_id FROM shop_note_goods WHERE platform_account_id = :s
                  )
                """,
                s=survivor,
                l=loser,
            )
            _run(
                conn,
                "UPDATE shop_note_goods SET platform_account_id = :s WHERE platform_account_id = :l",
                s=survivor,
                l=loser,
            )

            _run(conn, "DELETE FROM platform_accounts WHERE id = :l", l=loser)
            removed += 1

            # ⑤ 店铺关系最后搬（两行不能同时引用同一个店，见函数 docstring）。
            if carry_shop is not None:
                _run(
                    conn,
                    "UPDATE platform_accounts SET shop_id_fk = :fk, shop_state = :st WHERE id = :i",
                    fk=carry_shop[0],
                    st=carry_shop[1],
                    i=survivor,
                )
    return len(groups), removed


# ---------------------------------------------------------------------------
# 迁移主体
# ---------------------------------------------------------------------------


def upgrade() -> None:
    conn = op.get_bind()

    # ① 存量合并 —— 必须在建新索引之前，否则索引建不上。
    _merge_duplicate_accounts(conn)

    if not _has_table(conn, "platform_accounts"):
        # 简化夹具库（只建了被某条迁移用到的几张表）—— 账号身份这件事在这里不适用。
        return

    # ② 旧索引退休：键含 `sub_type`，正是「同一账号两行」在 DB 层合法的原因。
    #    先删再建（同名不同键会撞），旧名不再使用。
    _run(conn, "DROP INDEX IF EXISTS uq_platform_accounts_identity")

    # ③ 新索引：**按小红书账号 ID** 唯一（空 id 不参与 —— 「空」不构成身份，
    #    否则按店铺建的千帆账号会互相冲突）。
    _run(
        conn,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_accounts_xhs_identity
        ON platform_accounts (user_id, platform, external_user_id)
        WHERE external_user_id IS NOT NULL AND external_user_id <> ''
        """,
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "platform_accounts"):
        return
    _run(conn, "DROP INDEX IF EXISTS uq_platform_accounts_xhs_identity")
    _run(
        conn,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_accounts_identity
        ON platform_accounts (user_id, platform, sub_type, external_user_id)
        WHERE external_user_id IS NOT NULL AND external_user_id <> ''
        """,
    )
    # 被合并掉的行**不还原**：合并是本迁移的目的，且数据已改挂。
    # downgrade 只把索引恢复成旧形态（旧形态不会拒绝单行数据）。
