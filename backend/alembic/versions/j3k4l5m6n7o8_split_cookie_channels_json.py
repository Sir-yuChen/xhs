"""split cookie channels into www / creator / ark, store as one JSON field

2026-09-21 用户拍板（设计 12 §11.6 修订 / 设计 13 §18）：

1. **千帆与创作者平台的会话令牌拆分，不再公用**。真机实测（观隅好物 acct 8）
   推翻了我方 09-20 的假设：创作者扫码拿到的 jar 只带
   `access-token-creator.xiaohongshu.com` 域的令牌，打千帆一律
   `-13000 缺失登录凭证`；而千帆（绑定表）那份 jar 是**超集**（同时带 creator
   域与 ark 域令牌），打创作者平台正常。原「共用 ark 通道」导致千帆探针拿着
   创作者 jar 去问千帆 ⇒ 界面显示「千帆已失效」，令牌其实好好的。
2. **数据库不要 channel 字段，一个字段存 JSON，平台信息也存进去**：
   `encrypted_cookies` 改存信封 `{"v":1,"channels":{<平台>:<jar 文本>}}`。
   老数据**全量迁移**（用户口径：不做兼容）。

本迁移做三件事（幂等可续跑）：
  1. 把每行老数据（裸 jar 文本）解密 → 按形态分类（三态）→ 写信封 → 加密回写；
  2. 重建表去掉 `channel` 列（SQLite 走 batch alter）；
  3. 已是信封的行跳过（重跑不重复转换）。

⚠️ 解密/加密必须在应用层做（Cookie 是加密存储的，SQL 层看不到键名）——
所以本迁移 import 应用模块，与 alembic env 已在 import `backend.app.models`
同一前提。降级会把信封拆回「一行一平台 + channel 列」。
"""
from __future__ import annotations

import logging

from alembic import op

from backend.app.core.migration_introspect import column_names

logger = logging.getLogger("alembic.migration")

revision = "j3k4l5m6n7o8"
down_revision = "g7h8i9j0k1l2"
branch_labels = None
depends_on = None


def _has_column(conn, table: str, column: str) -> bool:
    #: v1.1.0 PG 化：原为 `exec_driver_sql("PRAGMA table_info(...)")`（SQLite 专属），统一走内省助手。
    return column in column_names(conn, table)


def _convert_rows_to_envelopes(conn) -> int:
    """老数据 → 信封（幂等）。返回转换行数。"""
    from backend.app.core.security import decrypt_text, encrypt_text
    from backend.app.services.account_service import (
        CHANNELS,
        channel_for_cookies,
        dump_channel_envelope,
        parse_channel_envelope,
    )

    converted = 0
    rows = conn.exec_driver_sql(
        "select id, encrypted_cookies from account_cookie_versions"
    ).fetchall()
    for row_id, encrypted in rows:
        text = decrypt_text(encrypted)
        if parse_channel_envelope(text):
            continue  # 已是信封
        channel = channel_for_cookies(text)
        if channel not in CHANNELS:  # pragma: no cover - 分类只可能返回已知平台
            continue
        conn.exec_driver_sql(
            "update account_cookie_versions set encrypted_cookies = ? where id = ?",
            (encrypt_text(dump_channel_envelope({channel: text})), row_id),
        )
        converted += 1
    return converted


def upgrade() -> None:
    conn = op.get_bind()
    # ① 先转数据（此时 channel 列还在，分类只按 jar 形态、不依赖列值）
    converted = _convert_rows_to_envelopes(conn)
    # ② 再去列（SQLite：batch alter 走重建表）
    if _has_column(conn, "account_cookie_versions", "channel"):
        with op.batch_alter_table("account_cookie_versions") as batch:
            batch.drop_column("channel")
    if converted:
        logger.info("[j3k4l5m6n7o8] 转换 %d 行 Cookie 为信封 JSON（按 jar 形态分类）", converted)


def downgrade() -> None:
    conn = op.get_bind()
    from backend.app.core.security import decrypt_text, encrypt_text
    from backend.app.services.account_service import parse_channel_envelope

    # ① 加回 channel 列（默认 www，历史行先有着落）
    if not _has_column(conn, "account_cookie_versions", "channel"):
        conn.exec_driver_sql(
            "ALTER TABLE account_cookie_versions ADD COLUMN channel VARCHAR(16) NOT NULL DEFAULT 'www'"
        )
    # ② 信封拆成「一行一平台」：一个信封涉及几个平台就拆几行
    rows = conn.exec_driver_sql(
        "select id, platform_account_id, encrypted_cookies, created_at from account_cookie_versions"
    ).fetchall()
    for row_id, account_id, encrypted, created_at in rows:
        channels = parse_channel_envelope(decrypt_text(encrypted))
        if not channels:
            continue  # 不是信封（或空信封）：保持原样
        items = sorted(channels.items())
        first_channel, first_jar = items[0]
        conn.exec_driver_sql(
            "update account_cookie_versions set encrypted_cookies = ?, channel = ? where id = ?",
            (encrypt_text(first_jar), first_channel, row_id),
        )
        for channel, jar in items[1:]:
            conn.exec_driver_sql(
                "insert into account_cookie_versions "
                "(platform_account_id, encrypted_cookies, channel, created_at) "
                "values (?, ?, ?, ?)",
                (account_id, encrypt_text(jar), channel, created_at),
            )
