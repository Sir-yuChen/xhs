from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


class PlatformAccount(Base):
    __tablename__ = "platform_accounts"
    __table_args__ = (
        #: **账号身份唯一**（台账 §4.9 / 不变量 I1，迁移 `d4e5f6a7b8c9`）。
        #:
        #: 键**必须含 `sub_type`**：同一小红书账号的 `pc` 与 `creator` 是两条
        #: **合法链路**（`_sync_creator_account_from_pc_cookie()` 有意从 PC Cookie
        #: 换出创作者链路，账号卡片上也明确区分「类型：PC / Creator」），
        #: 把两条合成一条会**丢掉发布能力**（发布要求 `sub_type == "creator"`）。
        #:
        #: **为什么是部分索引**：`external_user_id` 为空的行不参与约束 —— 「空」不构成
        #: 身份，若参与，多个空 id 账号会互相冲突。空 id 的重复改由业务层用
        #: `(user_id, platform, sub_type, nickname)` 兜底匹配拦住（见
        #: `account_service.upsert_platform_account_from_login`）。
        Index(
            "uq_platform_accounts_identity",
            "user_id",
            "platform",
            "sub_type",
            "external_user_id",
            unique=True,
            sqlite_where=text("external_user_id IS NOT NULL AND external_user_id <> ''"),
        ),
        #: **店铺 1:1**（用户 2026-09-22 口径，迁移 `m5n6o7p8q9r0`）：
        #: 一个店铺最多被一个账号引用。
        #:
        #: **为什么是部分索引**（与上面那条同型）：
        #: * `shop_id_fk IS NOT NULL` —— 「未绑店」不是一个占用；不加这条，
        #:   多行 NULL 会互相冲突（而那正是缺值口径要的形状）。
        #: * `deleted_at IS NULL` —— **软删账号不再占用店铺**（DEF-055 的新形态）：
        #:   账号删了就当作没绑过，否则一条删掉的账号会**永久顶住**这个店的唯一约束，
        #:   用户重新导入同一账号时 `attach_shop` 直接撞 IntegrityError（表现为 500，
        #:   而不是一句能自救的话）。
        Index(
            "uq_platform_accounts_shop_id_fk",
            "shop_id_fk",
            unique=True,
            sqlite_where=text("shop_id_fk IS NOT NULL AND deleted_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    sub_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    external_user_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    nickname: Mapped[str] = mapped_column(String(128), default="")
    avatar_url: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    status_message: Mapped[str] = mapped_column(Text, default="")
    profile_json: Mapped[str] = mapped_column(Text, default="{}")
    #: 增量同步游标（设计 13 §17.9 / D13）：上次**成功落库**的最大 `publish_time`（毫秒）。
    #: 只在落库成功后推进；缺值 = 0 = 未同步过 = 下一轮走全量。
    note_sync_cursor_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    #: 同毫秒并列时的 tie-breaker（防止两条同 `publish_time` 的笔记漏行）。
    note_sync_cursor_note_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    #: 千帆店铺（`shops.id`）。**「账号 ↔ 店铺」的关系只有这一处**。
    #:
    #: 2026-09-22 起由 `shop_account_bindings` 搬到这里（那张表整表移除）。
    #: **粒度是 1:1，且由 DB 强制**（用户口径：*不允许再出现「同店铺、多账号」场景；
    #: 一个店铺下只需存**多个平台的会话令牌*** —— 令牌本来就都在该账号自己的
    #: `account_cookie_versions` 里）。唯一性由上面的**部分唯一索引**
    #: `uq_platform_accounts_shop_id_fk` 承担（所以这里**不能**再写 `unique=True`：
    #: 那是列级全量唯一，会把「未绑店的多行 NULL」和「软删账号的残留引用」也卷进去）。
    shop_id_fk: Mapped[Optional[int]] = mapped_column(
        ForeignKey("shops.id"), index=True, nullable=True
    )
    #: 千帆（ark）侧的**状态与游标**，JSON 文本。原绑定行的那几个字段搬到这里：
    #:
    #:   ``{"status","status_message","health_checked_at",
    #:      "goods_note_total","goods_synced_at","goods_cursor_ms"}``
    #:
    #: 为什么是 Text + 显式 ``json.dumps`` 而不是 SQLAlchemy 的 ``JSON`` 列：
    #: 本项目的 `JSON` 列踩过「**原地改 dict 不标记脏 ⇒ UPDATE 不发、改动静默丢失**」
    #: 的坑（源码看着完全正确）。这里把读写收口到 `shop_binding_service`
    #: 的 `get_shop_state()` / `set_shop_state()`（**唯一实现**，内部赋新 dict），
    #: 任何调用点都不该自己 `json.loads` 这一列再原地改。
    #:
    #: 店铺名 / 卖家 ID **不在这里**：它们是 `shops` 表的真值（同一份数据不留两处）。
    shop_state: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now, onupdate=shanghai_now)
    #: **软删标记**（台账 §4.9 / 不变量 I3）：非空 = 已删除。
    #:
    #: 删除**不落地物理删**：行留在原位，唯一索引因此永久成立，重新导入同一账号时
    #: upsert 会命中本行并**复活**（清空本列），而不是新建一条 —— 从根上消灭
    #: 「删了再导入又来一条」的重复源；同时它名下 `notes` / 店铺绑定不会变成孤儿。
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AccountCookieVersion(Base):
    __tablename__ = "account_cookie_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform_account_id: Mapped[int] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    #: **一个字段存信封 JSON**（用户 2026-09-21 口径：不拆多字段、不加列，
    #: 平台信息也存进 JSON 里）：
    #:   {"v": 1, "channels": {"www": "<jar>", "creator": "<jar>", "ark": "<jar>"}}
    #: 同一个账号下三个平台的令牌**互不通用**（2026-09-21 拆分，用户拍板）：
    #:
    #: * ``www`` —— 网页版（`web_session` + `id_token`），供 `www` / `edith` / `sns/web`；
    #: * ``creator`` —— 创作者平台（`access-token-creator.*`）；
    #: * ``ark`` —— 千帆（`access-token-ark.*`）。
    #:
    #: 存到哪个平台由 `account_service.channel_for_cookies()` 按 Cookie 形态判定，
    #: 不靠调用方传；读时按「要打哪个平台」取对应平台（`latest_cookies_for_channel`）。
    encrypted_cookies: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
