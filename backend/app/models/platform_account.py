from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
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
        #: **账号身份唯一 —— 按小红书账号 ID**（台账 §4.9 / 不变量 I1；迁移
        #: `g6h7i8j9k0l1`，取代原 `d4e5f6a7b8c9` 的 `uq_platform_accounts_identity`）。
        #:
        #: 键 = `(user_id, platform, external_user_id)`。**`sub_type` 不再进键**
        #: （2026-09-26 用户口径：「数据库任何场景下账号信息表，根据账号小红书 ID
        #: 均不允许出现重复多行账号信息」）。原键含 `sub_type` ⇒ 同一个账号先按
        #: 「PC」扫、再按「创作者平台」扫就留下**两行**，界面上看起来是
        #: 「两个相同的账号信息」（真实库实测：同 `external_user_id`、
        #: 同昵称、同小红书号，只差一个「类型」）。
        #:
        #: 链路不进键 ≠ 丢掉链路：`sub_type` 仍是单列事实，两条链路合并成一行时
        #: 取**更强**的那条（`account_types.strongest_sub_type`，`creator` > `pc`，
        #: 否则会静默丢掉发布能力）；两条链路的令牌本来就各在自己的通道里
        #: （`www` / `creator` / `ark`）。
        #:
        #: **为什么是部分索引**：`external_user_id` 为空的行不参与约束 —— 「空」不构成
        #: 身份，若参与，多个空 id 账号（例如只导入千帆会话、还没拿到小红书 id 的账号）会互相冲突。
        #: 空 id 的重复改由业务层用 `(user_id, platform, sub_type, nickname)` 兜底匹配
        #: 拦住（见 `account_service.upsert_platform_account_from_login`）。
        Index(
            "uq_platform_accounts_xhs_identity",
            "user_id",
            "platform",
            "external_user_id",
            unique=True,
            sqlite_where=text("external_user_id IS NOT NULL AND external_user_id <> ''"),
            #: ⚠️ 2026-10-23 补：**此前只有 `sqlite_where`，PG 上这个索引根本不存在**
            #: （实测 `pg_indexes ... indexdef ilike '%WHERE%'` 返回 0 条）⇒
            #: 「同一小红书 ID 不能绑定两次」在 PG 上**失效**且不报错。
            #: 两个方言的部分索引条件必须**同时声明**（`<> ''` 在 PG 同样成立）。
            postgresql_where=text("external_user_id IS NOT NULL AND external_user_id <> ''"),
        ),
        #: ⚠️ **`uq_platform_accounts_shop_id_fk` 已退场**（ADR-021 / 设计 18，
        #: 迁移 `a1b2c3d4e5f6`）：**店铺不再是实体**，`shops` 表与
        #: `platform_accounts.shop_id_fk` 一并删除。被取代的旧注释原句保留：
        #: *「**店铺 1:1**（用户 2026-09-22 口径，迁移 `m5n6o7p8q9r0`）：
        #: 一个店铺最多被一个账号引用。」*
        #: —— 关系形态被取代，但当时那两条「为什么是部分索引」的理由
        #: （NULL 不占位 / 软删释放）在新的身份索引上仍然成立。
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    sub_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    external_user_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    nickname: Mapped[str] = mapped_column(String(128), default="")
    avatar_url: Mapped[str] = mapped_column(Text, default="")
    #: 登录探活的最近结论（`active` / `expired` / `unknown`），由巡检与登录刷新**自动写**。
    #: 不是「用不用」—— 那是 `disabled` 列。
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    status_message: Mapped[str] = mapped_column(Text, default="")
    #: 用户手动控制的停用位（`False` = 正常使用）。**只有用户操作能写它** ——
    #: 巡检 / 探活 / 登录刷新一律不写（守卫 `test_account_disabled_switch.py`）。
    #: 停用不删数据、不吊销令牌。与 `status` 分开是因为后者来源是自动的。
    disabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"), index=True
    )
    profile_json: Mapped[str] = mapped_column(Text, default="{}")
    #: 增量同步游标（设计 13 §17.9 / D13）：上次**成功落库**的最大 `publish_time`（毫秒）。
    #: 只在落库成功后推进；缺值 = 0 = 未同步过 = 下一轮走全量。
    note_sync_cursor_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    #: 同毫秒并列时的 tie-breaker（防止两条同 `publish_time` 的笔记漏行）。
    note_sync_cursor_note_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    #: **千帆店铺标识（卖家 ID）—— 店铺不再是实体，它只是本账号的一个属性**
    #: （ADR-021 / 设计 18，迁移 `a1b2c3d4e5f6`）。
    #:
    #: 千帆 `GET /api/edith/seller/info/v2` 回的 `user_id`（形如
    #: `6a3250e427a0000000000000`）落在这里。原先是 `shops` 表的 `seller_id`
    #: 列，经 `platform_accounts.shop_id_fk` 间接可达 —— 本次下沉为账号字段，
    #: `shops` 表整表退场。
    #:
    #: **为什么不再是实体**（用户 2026-09-30 口径）：*「绑定账号后，不要再建店铺
    #: 行数据，账号下存在三个平台的令牌，使用对应平台接口时则对应的使用该平台的
    #: 登录令牌」*。店铺没有独立身份 —— 它就是某个小红书商家账号在千帆侧的名字，
    #: 一对一是数据事实而不是要被强制的关系，所以不需要一张表和一条唯一约束。
    #:
    #: ⚠️ **本列不是能力判据**：「能不能用千帆」看的是 `ark` 通道有没有令牌
    #: （`qianfan_cookies_for_account()`），**不是**本列有没有值。账号 9 的
    #: Bug 正是把能力判据写成了归属判据（`shop_id_fk IS NULL` ⇒ 判定不可用），
    #: 而它其实持有效 `ark` 令牌。**归属用归属列，调接口用令牌有效性**。
    #:
    #: 缺值口径：千帆没返回就**留空、不编造**（DEF-057 / 缺值不造假）。
    seller_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
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
    #: 店铺名 / 卖家 ID **不在这里**：ADR-021 之后**店铺名就是本账号昵称**
    #: （千帆 `seller_name` 与昵称同源），卖家 ID 是上面的 `seller_id` 列 ——
    #: 同一份数据不留两处，也不再有第三方表可查。
    shop_state: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now, onupdate=shanghai_now)
    #: **软删标记**（台账 §4.9 / 不变量 I3）：非空 = 已删除。
    #:
    #: 删除**不落地物理删**：行留在原位，唯一索引因此永久成立，重新导入同一账号时
    #: upsert 会命中本行并**复活**（清空本列），而不是新建一条 —— 从根上消灭
    #: 「删了再导入又来一条」的重复源；同时它名下 `notes` / 商品台账不会变成孤儿。
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
