"""drop shop_account_bindings: relationship -> platform_accounts.shop_id_fk (1:1, UNIQUE)

2026-09-22 用户拍板（设计 12 §11.6 / §17.10 修订、设计 13 §18.14 修订）。

## 为什么拆

`shop_account_bindings` 一张表同时干**三件不相关的事**，是「一个语义两份真相源」
的温床（`product_notes.py` 就绕过取票函数直读它，于是「账号补了自有千帆令牌」
在商品笔记生成这条路上根本不生效）：

| 角色 | 现在的落点 |
|---|---|
| 账号 ↔ 店铺**关系** | `platform_accounts.shop_id_fk`（**1:1 且 DB 强制**：`UNIQUE`） |
| 千帆**凭据** | `account_cookie_versions` 的 `ark` 通道（与 www / creator 同一仓库） |
| **状态与游标** | `platform_accounts.shop_state`（JSON 文本，读写只经 `shop_binding_service`） |

## 「同店多账号」被否掉（本轮最关键的口径）

原设计是 N:1（企业店主 + 多子账号读同一个店）。**用户否决**：
*不会再出现「同店铺、多账号」场景；一个店铺下只需存**多个平台的会话令牌*** ——
令牌本来就都在该账号自己的 `account_cookie_versions` 里。所以关系是 1:1，
并且**只靠约定拦不住，靠 DB 约束才拦得住**（`shop_id_fk` 带 `UNIQUE`）。
副作用：`shop_account_count` 恒为 0/1，已从接口响应里移除。

## 本迁移做什么（幂等可续跑）

1. `platform_accounts` 加 `shop_id_fk`（+ UNIQUE 索引）与 `shop_state`（JSON 文本）；
2. **凭据搬家**：绑定行那份 jar 解密 → 按**形态**判平台（`channel_for_cookies`）→
   写进**该账号自己的**通道。⚠️ 只在「该账号这个通道还是空的」时写 ——
   账号自有的令牌永远优先，绝不用历史行顶掉它（那是「借用令牌」的翻版）；
3. **关系搬家**：`platform_accounts.shop_id_fk = binding.shop_id_fk`（一个店铺只认
   一个账号；若多个绑定指向同一店，保留最早的一条并记日志，其余账号保持未绑）；
4. **状态搬家**：绑定行的 status / status_message / goods_note_total /
   goods_synced_at / health_checked_at / goods_cursor_ms → `shop_state` JSON；
5. `shop_note_goods.binding_id` → `platform_account_id`（**换列不改语义**：绑定当时
   就是账号级的，一个账号最多一条在用绑定）；唯一约束随之改名；
6. **删表** `shop_account_bindings`；
7. **清掉多余账号的历史数据**（用户 2026-09-22 明确要求）：
   * `id=15`「增量测试号」—— 增量同步的测试夹具，无令牌 / 无笔记；
   * `id=1`「观隅好物(pc)」—— 与 `id=8` 是**同一个小红书账号**（`external_user_id`
     相同）但挂在不同租户下，且**已被软删**；它那份残留 `www` 令牌正是「多余账号的
     历史数据」的形状。
   ⇒ 物理删除这两个账号在 `notes` / `publish_jobs` / `auto_tasks` /
   `monitoring_targets` / `account_cookie_versions` / `shop_note_goods` 下的行与账号行本身。
   ⚠️ 只按**显式 id 列表**删，不做任何「按条件猜」的批量删除。

⚠️ 解密/加密必须在应用层做（Cookie 是加密存储的，SQL 层看不到键名），所以本迁移
import 应用模块 —— 与 alembic env 已 import `backend.app.models` 同一前提。

⚠️ 本库 `PRAGMA foreign_keys` 实测为 0（`core/database.py` 只设 journal_mode），
重建表期间不必处理级联。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

import sqlalchemy as sa

from alembic import op

logger = logging.getLogger("alembic.migration")

revision = "m5n6o7p8q9r0"
down_revision = "k4m5n6o7p8q9"
branch_labels = None
depends_on = None

#: 用户 2026-09-22 要求清掉的多余账号（**显式列举**，不做条件式批量删除）。
REDUNDANT_ACCOUNT_IDS: tuple[int, ...] = (1, 15)

#: 多余账号可能在哪些表里留下行（列名 → 表名）。
_DEPENDENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("notes", "platform_account_id"),
    ("publish_jobs", "platform_account_id"),
    ("monitoring_targets", "platform_account_id"),
    ("auto_tasks", "pc_account_id"),
    ("auto_tasks", "creator_account_id"),
    ("account_cookie_versions", "platform_account_id"),
    ("shop_note_goods", "platform_account_id"),
)


def _table_exists(conn, table: str) -> bool:
    rows = conn.exec_driver_sql(
        "select name from sqlite_master where type='table' and name = ?", (table,)
    ).fetchall()
    return bool(rows)


def _has_column(conn, table: str, column: str) -> bool:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _iso(value) -> str:
    """绑定行的 DATETIME / 字符串 → `shop_state` 里那个 ISO 字符串；空 → 空串。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _read_bindings(conn) -> list[dict]:
    rows = conn.exec_driver_sql(
        "select id, user_id, platform_account_id, shop_id_fk, encrypted_cookies, "
        "       status, status_message, goods_note_total, goods_synced_at, "
        "       health_checked_at, goods_cursor_ms "
        "from shop_account_bindings order by id"
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        out.append(
            {
                "id": row[0],
                "user_id": row[1],
                "platform_account_id": row[2],
                "shop_id_fk": row[3],
                "encrypted_cookies": row[4],
                "status": row[5] or "",
                "status_message": row[6] or "",
                "goods_note_total": int(row[7] or 0),
                "goods_synced_at": _iso(row[8]),
                "health_checked_at": _iso(row[9]),
                "goods_cursor_ms": int(row[10] or 0),
            }
        )
    return out


def _account_channels(conn, account_id: int) -> dict[str, str]:
    """该账号各平台**最新一版**的 jar（`{channel: 请求头串}`）。

    ⚠️ 迁移里**不能**调 `account_service.latest_cookies_for_channel` —— 那个函数要
    的是 ORM `Session`，而 alembic 手里只有 `Connection`。所以这里按同一份口径
    重写一遍（`created_at desc, id desc` + `setdefault`：先出现者赢）。
    口径只有一份的纪律在这里让位给「迁移不能依赖应用会话」—— 若应用的合并口径变了，
    本函数要跟着改（注释互为锚点）。
    """
    from backend.app.core.security import decrypt_text
    from backend.app.services.account_service import parse_channel_envelope

    merged: dict[str, str] = {}
    rows = conn.exec_driver_sql(
        "select encrypted_cookies from account_cookie_versions "
        "where platform_account_id = ? order by created_at desc, id desc",
        (account_id,),
    ).fetchall()
    for (encrypted,) in rows:
        for channel, jar in parse_channel_envelope(decrypt_text(encrypted)).items():
            merged.setdefault(channel, jar)
    return merged


def _move_credentials(conn, bindings: list[dict]) -> int:
    """绑定行的 jar → **该账号自己的**通道。返回写入行数。

    ⚠️ 只在「该账号这个通道还是空的」时才写：账号自有令牌永远优先。
    这正是本迁移要落地的那条口径 —— 历史行不得顶掉账号自己的令牌。
    """
    if not bindings:
        return 0
    from backend.app.core.security import decrypt_text, encrypt_text
    from backend.app.services.account_service import (
        CHANNELS,
        channel_for_cookies,
        dump_channel_envelope,
        parse_channel_envelope,
    )

    written = 0
    for binding in bindings:
        encrypted = binding["encrypted_cookies"]
        account_id = binding["platform_account_id"]
        if not encrypted or account_id is None:
            continue
        try:
            text = decrypt_text(encrypted)
        except Exception as error:  # noqa: BLE001 - 坏数据不该挡住结构变更
            logger.warning(
                "[m5n6o7p8q9r0] 绑定 #%s 的 Cookie 解不开（%s），跳过凭据搬家",
                binding["id"],
                type(error).__name__,
            )
            continue
        if not (text or "").strip():
            continue
        # 老数据是裸 jar；已被前面迁移包过信封的直接用它自己声明的通道。
        channels = parse_channel_envelope(text) or {channel_for_cookies(text): text}
        owned = _account_channels(conn, account_id)
        for channel, jar in channels.items():
            if channel not in CHANNELS or not (jar or "").strip():
                continue
            if owned.get(channel):
                continue  # 账号自己有 → 不覆盖
            conn.execute(
                sa.text(
                    "insert into account_cookie_versions "
                    "(platform_account_id, encrypted_cookies, created_at) "
                    "values (:account_id, :cookies, CURRENT_TIMESTAMP)"
                ),
                {
                    "account_id": account_id,
                    "cookies": encrypt_text(dump_channel_envelope({channel: jar})),
                },
            )
            owned[channel] = jar  # 同一次搬家内不重复写第二遍
            written += 1
    return written


def _move_relations(conn, bindings: list[dict]) -> dict[int, int]:
    """`binding → account` 映射，并把 `shop_id_fk` / `shop_state` 搬到账号行。

    1:1 闸：一个店铺只认一个账号（保留最早那条绑定）。返回 `{binding_id: account_id}`，
    只含**成功建立关系**的绑定 —— 映射表要用它决定哪些 `shop_note_goods` 行留得下。
    """
    claimed: dict[int, int] = {}  # shop_id_fk -> account_id
    mapping: dict[int, int] = {}
    for binding in bindings:
        account_id = binding["platform_account_id"]
        row = conn.exec_driver_sql(
            "select id, deleted_at from platform_accounts where id = ?", (account_id,)
        ).fetchone()
        if row is None:
            logger.warning(
                "[m5n6o7p8q9r0] 绑定 #%s 指向不存在的账号 %s，丢弃",
                binding["id"],
                account_id,
            )
            continue
        mapping[binding["id"]] = account_id

        shop_pk = binding["shop_id_fk"]
        if shop_pk is None:
            continue
        if row[1] is not None:
            # 软删账号不再占用店铺（与部分唯一索引里的 `deleted_at IS NULL` 同口径）：
            # 给它写 shop_id_fk 只会留下一条「看不见的占用」。
            logger.info(
                "[m5n6o7p8q9r0] 账号 %s 已软删，不继承店铺关系（映射仍保留其归属）",
                account_id,
            )
            continue
        holder = claimed.get(shop_pk)
        if holder is not None and holder != account_id:
            # 1:1：同店第二个账号不再成立。保留最早那条，其余保持未绑（不静默改归属）。
            logger.warning(
                "[m5n6o7p8q9r0] 店铺 #%s 已被账号 %s 占用，绑定 #%s（账号 %s）保持未绑 —— "
                "「同店多账号」已被用户口径否决",
                shop_pk,
                holder,
                binding["id"],
                account_id,
            )
            continue
        claimed[shop_pk] = account_id

        state = {
            "status": binding["status"],
            "status_message": binding["status_message"],
            "health_checked_at": binding["health_checked_at"],
            "goods_note_total": binding["goods_note_total"],
            "goods_synced_at": binding["goods_synced_at"],
            "goods_cursor_ms": binding["goods_cursor_ms"],
        }
        conn.execute(
            sa.text(
                "update platform_accounts set shop_id_fk = :shop_pk, shop_state = :state "
                "where id = :account_id"
            ),
            {
                "shop_pk": shop_pk,
                "state": json.dumps(state, ensure_ascii=False),
                "account_id": account_id,
            },
        )
    return mapping


def _add_account_columns(conn) -> None:
    """加 `shop_id_fk`（1:1，部分唯一索引）+ `shop_state`（幂等）。"""
    if not _has_column(conn, "platform_accounts", "shop_id_fk"):
        # ⚠️ 不能靠 `ADD COLUMN ... UNIQUE`：SQLite 明确拒绝带 UNIQUE 的 ADD COLUMN。
        # 所以先加普通列，再单独建索引 —— 索引才是「由 DB 强制 1:1」的落点。
        conn.exec_driver_sql(
            "ALTER TABLE platform_accounts ADD COLUMN shop_id_fk INTEGER "
            "REFERENCES shops (id)"
        )
    if not _has_column(conn, "platform_accounts", "shop_state"):
        conn.exec_driver_sql(
            "ALTER TABLE platform_accounts ADD COLUMN shop_state TEXT NOT NULL DEFAULT '{}'"
        )
    #: **部分唯一索引**（与 `uq_platform_accounts_identity` 同型）：
    #: * `shop_id_fk IS NOT NULL` —— 未绑店不算占用（多行 NULL 不冲突）；
    #: * `deleted_at IS NULL` —— 软删账号不再占用店铺（DEF-055 的新形态）；
    #:   少了它，一条删掉的账号会**永久顶住**这个店的唯一约束，重新导入同一账号时
    #:   `attach_shop` 直接撞 IntegrityError（500，而不是一句能自救的话）。
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_accounts_shop_id_fk "
        "ON platform_accounts (shop_id_fk) "
        "WHERE shop_id_fk IS NOT NULL AND deleted_at IS NULL"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_platform_accounts_shop_id_fk "
        "ON platform_accounts (shop_id_fk)"
    )


def _rebuild_shop_note_goods(conn, mapping: dict[int, int]) -> int:
    """`binding_id` → `platform_account_id`（**换列不改语义**）。返回保留的行数。

    走 SQLite 官方的显式重建（不是 `batch_op.alter_column`）：既要改列名与值，
    还要改唯一约束的名字与 FK 指向 —— 一次重建比三次 DDL 更不容易留下半状态。
    """
    if not _has_column(conn, "shop_note_goods", "binding_id"):
        return 0  # 已经是新形状（重跑）
    conn.exec_driver_sql(
        """
        CREATE TABLE shop_note_goods_new (
            id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            platform_account_id INTEGER NOT NULL,
            note_id VARCHAR(64) NOT NULL,
            item_id VARCHAR(64) NOT NULL,
            item_title TEXT NOT NULL,
            item_image TEXT NOT NULL,
            variant_desc VARCHAR(255) NOT NULL,
            goods_json TEXT NOT NULL,
            entry_json TEXT NOT NULL,
            synced_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(user_id) REFERENCES users (id),
            FOREIGN KEY(platform_account_id) REFERENCES platform_accounts (id),
            CONSTRAINT uq_shop_note_goods_account_note UNIQUE (platform_account_id, note_id)
        )
        """
    )
    kept = 0
    rows = conn.exec_driver_sql(
        "select id, user_id, binding_id, note_id, item_id, item_title, item_image, "
        "       variant_desc, goods_json, entry_json, synced_at from shop_note_goods"
    ).fetchall()
    for row in rows:
        account_id = mapping.get(row[2])
        if account_id is None:
            # 映射的归属账号已经不存在 ⇒ 这条映射没有意义（不搬家，不落到别的账号上）。
            continue
        conn.execute(
            sa.text(
                "insert into shop_note_goods_new "
                "(id, user_id, platform_account_id, note_id, item_id, item_title, "
                " item_image, variant_desc, goods_json, entry_json, synced_at) "
                "values (:id, :user_id, :account_id, :note_id, :item_id, :item_title, "
                " :item_image, :variant_desc, :goods_json, :entry_json, :synced_at)"
            ),
            {
                "id": row[0],
                "user_id": row[1],
                "account_id": account_id,
                "note_id": row[3],
                "item_id": row[4],
                "item_title": row[5],
                "item_image": row[6],
                "variant_desc": row[7],
                "goods_json": row[8],
                "entry_json": row[9],
                "synced_at": row[10],
            },
        )
        kept += 1
    conn.exec_driver_sql("DROP TABLE shop_note_goods")
    conn.exec_driver_sql("ALTER TABLE shop_note_goods_new RENAME TO shop_note_goods")
    conn.exec_driver_sql(
        "CREATE INDEX ix_shop_note_goods_id ON shop_note_goods (id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX ix_shop_note_goods_user_id ON shop_note_goods (user_id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX ix_shop_note_goods_account_id ON shop_note_goods (platform_account_id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX ix_shop_note_goods_note_id ON shop_note_goods (note_id)"
    )
    return kept


def _purge_redundant_accounts(conn) -> dict[str, int]:
    """物理删除 `REDUNDANT_ACCOUNT_IDS` 的账号行与它们的从属行。返回删了多少。"""
    ids = list(REDUNDANT_ACCOUNT_IDS)
    if not ids:
        return {}
    removed: dict[str, int] = {}
    for table, column in _DEPENDENT_COLUMNS:
        if not _table_exists(conn, table) or not _has_column(conn, table, column):
            continue
        placeholders = ",".join(str(int(i)) for i in ids)
        result = conn.exec_driver_sql(
            f"DELETE FROM {table} WHERE {column} IN ({placeholders})"
        )
        if result.rowcount:
            removed[f"{table}.{column}"] = result.rowcount
    # 最后删账号行本身。
    placeholders = ",".join(str(int(i)) for i in ids)
    result = conn.exec_driver_sql(
        f"DELETE FROM platform_accounts WHERE id IN ({placeholders})"
    )
    removed["platform_accounts"] = result.rowcount or 0
    return removed


def upgrade() -> None:
    conn = op.get_bind()
    is_sqlite = conn.dialect.name == "sqlite"

    # ① 先加两列（后面所有回填都依赖它们存在）。
    _add_account_columns(conn)

    if _table_exists(conn, "shop_account_bindings"):
        bindings = _read_bindings(conn)
        # ② 凭据搬家（账号自有令牌优先）。
        credentials = _move_credentials(conn, bindings)
        # ③ 关系 + 状态搬家，拿到 binding → account 映射。
        mapping = _move_relations(conn, bindings)
        logger.info(
            "[m5n6o7p8q9r0] 绑定 %d 行 → 账号关系；凭据新写 %d 条",
            len(bindings),
            credentials,
        )
    else:
        # 重跑或已在目标态：靠 shop_id_fk 反推映射（一个账号最多一个店）。
        mapping = {}
        logger.info("[m5n6o7p8q9r0] shop_account_bindings 不存在，跳过搬家")

    # ④ 映射表换列。
    if is_sqlite:
        kept = _rebuild_shop_note_goods(conn, mapping)
        logger.info("[m5n6o7p8q9r0] shop_note_goods 保留 %d 行（binding_id → platform_account_id）", kept)
    else:  # pragma: no cover - 本项目只跑 SQLite；别家库按同语义手工处理
        raise RuntimeError("本迁移按 SQLite 显式重建编写，其它方言请另行处理")

    # ⑤ 删表。
    if _table_exists(conn, "shop_account_bindings"):
        conn.exec_driver_sql("DROP TABLE shop_account_bindings")

    # ⑥ 清多余账号的历史数据（显式 id 列表）。
    removed = _purge_redundant_accounts(conn)
    if removed:
        logger.info("[m5n6o7p8q9r0] 清理多余账号 %s：%s", REDUNDANT_ACCOUNT_IDS, removed)


def downgrade() -> None:
    """回到绑定表形态（结构完整、数据尽力而为）。

    ⚠️ 诚实边界：**多余账号的历史数据不会回来**（⑥ 是物理删除，且 DDL 无法撤销）；
    `shop_state` 里的状态能搬回绑定行，但 `shop_note_goods` 的列名可以换回
    `binding_id`，其值会按「账号当前绑的店」重新对应到新建的绑定行。
    """
    conn = op.get_bind()

    # ① 重建绑定表（新的空表）。
    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS shop_account_bindings (
            id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            platform_account_id INTEGER NOT NULL,
            shop_id_fk INTEGER,
            shop_user_id VARCHAR(64) NOT NULL,
            shop_name VARCHAR(128) NOT NULL,
            encrypted_cookies TEXT NOT NULL,
            status VARCHAR(32) NOT NULL,
            status_message TEXT NOT NULL,
            goods_note_total INTEGER NOT NULL,
            goods_synced_at DATETIME,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            health_checked_at DATETIME,
            goods_cursor_ms INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (id),
            FOREIGN KEY(platform_account_id) REFERENCES platform_accounts (id),
            FOREIGN KEY(user_id) REFERENCES users (id),
            FOREIGN KEY(shop_id_fk) REFERENCES shops (id),
            UNIQUE (shop_id_fk, platform_account_id)
        )
        """
    )

    # ② 每个「绑了店的账号」还原成一条绑定行（关系 + 状态）。
    mapping: dict[int, int] = {}
    rows = conn.exec_driver_sql(
        "select a.id, a.user_id, a.shop_id_fk, a.shop_state, s.seller_id, s.shop_name "
        "from platform_accounts a join shops s on s.id = a.shop_id_fk "
        "where a.shop_id_fk is not null"
    ).fetchall()
    from backend.app.core.security import encrypt_text
    from backend.app.services.account_service import CHANNEL_ARK

    for account_id, user_id, shop_pk, raw_state, seller_id, shop_name in rows:
        try:
            state = json.loads(raw_state or "{}")
        except ValueError:
            state = {}
        state = state if isinstance(state, dict) else {}
        #: 凭据：把账号 ark 通道的 jar 搬回绑定行（老形态就是这样存的）。
        ark = _account_channels(conn, account_id).get(CHANNEL_ARK, "")
        result = conn.execute(
            sa.text(
                "insert into shop_account_bindings "
                "(user_id, platform_account_id, shop_id_fk, shop_user_id, shop_name, "
                " encrypted_cookies, status, status_message, goods_note_total, "
                " goods_synced_at, created_at, updated_at, health_checked_at, goods_cursor_ms) "
                "values (:user_id, :account_id, :shop_pk, :seller_id, :shop_name, :cookies, "
                " :status, :status_message, :total, :synced_at, CURRENT_TIMESTAMP, "
                " CURRENT_TIMESTAMP, :health, :cursor)"
            ),
            {
                "user_id": user_id,
                "account_id": account_id,
                "shop_pk": shop_pk,
                "seller_id": seller_id or "",
                "shop_name": shop_name or "",
                "cookies": encrypt_text(ark or ""),
                "status": str(state.get("status") or ""),
                "status_message": str(state.get("status_message") or ""),
                "total": int(state.get("goods_note_total") or 0),
                "synced_at": state.get("goods_synced_at") or None,
                "health": state.get("health_checked_at") or None,
                "cursor": int(state.get("goods_cursor_ms") or 0),
            },
        )
        mapping[account_id] = result.lastrowid

    # ③ 映射表换回 binding_id（值 = 该账号新绑定行的 id）。
    if _has_column(conn, "shop_note_goods", "platform_account_id"):
        conn.exec_driver_sql(
            """
            CREATE TABLE shop_note_goods_old (
                id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                binding_id INTEGER NOT NULL,
                note_id VARCHAR(64) NOT NULL,
                item_id VARCHAR(64) NOT NULL,
                item_title TEXT NOT NULL,
                item_image TEXT NOT NULL,
                variant_desc VARCHAR(255) NOT NULL,
                goods_json TEXT NOT NULL,
                entry_json TEXT NOT NULL,
                synced_at DATETIME NOT NULL,
                PRIMARY KEY (id),
                FOREIGN KEY(user_id) REFERENCES users (id),
                FOREIGN KEY(binding_id) REFERENCES shop_account_bindings (id),
                CONSTRAINT uq_shop_note_goods_binding_note UNIQUE (binding_id, note_id)
            )
            """
        )
        for row in conn.exec_driver_sql(
            "select id, user_id, platform_account_id, note_id, item_id, item_title, "
            "       item_image, variant_desc, goods_json, entry_json, synced_at "
            "from shop_note_goods"
        ).fetchall():
            binding_id = mapping.get(row[2])
            if binding_id is None:
                continue
            conn.execute(
                sa.text(
                    "insert into shop_note_goods_old "
                    "(id, user_id, binding_id, note_id, item_id, item_title, item_image, "
                    " variant_desc, goods_json, entry_json, synced_at) "
                    "values (:id, :user_id, :binding_id, :note_id, :item_id, :item_title, "
                    " :item_image, :variant_desc, :goods_json, :entry_json, :synced_at)"
                ),
                {
                    "id": row[0],
                    "user_id": row[1],
                    "binding_id": binding_id,
                    "note_id": row[3],
                    "item_id": row[4],
                    "item_title": row[5],
                    "item_image": row[6],
                    "variant_desc": row[7],
                    "goods_json": row[8],
                    "entry_json": row[9],
                    "synced_at": row[10],
                },
            )
        conn.exec_driver_sql("DROP TABLE shop_note_goods")
        conn.exec_driver_sql("ALTER TABLE shop_note_goods_old RENAME TO shop_note_goods")
        conn.exec_driver_sql("CREATE INDEX ix_shop_note_goods_id ON shop_note_goods (id)")
        conn.exec_driver_sql(
            "CREATE INDEX ix_shop_note_goods_user_id ON shop_note_goods (user_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX ix_shop_note_goods_binding_id ON shop_note_goods (binding_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX ix_shop_note_goods_note_id ON shop_note_goods (note_id)"
        )

    # ④ 去掉账号行上的两列（含唯一索引）。
    conn.exec_driver_sql("DROP INDEX IF EXISTS uq_platform_accounts_shop_id_fk")
    conn.exec_driver_sql("DROP INDEX IF EXISTS ix_platform_accounts_shop_id_fk")
    for column in ("shop_state", "shop_id_fk"):
        if _has_column(conn, "platform_accounts", column):
            conn.exec_driver_sql(f"ALTER TABLE platform_accounts DROP COLUMN {column}")
