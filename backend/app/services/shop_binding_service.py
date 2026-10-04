"""账号 → 千帆侧状态与取票（**2026-09-30 ADR-021 精简**）。

## 店铺实体已退场

本模块曾同时承担「账号↔店铺关系 / 千帆凭据 / 状态机」三件事，其中**关系那一轴
整条消失**（用户 2026-09-30 口径：*「绑定账号后，不要再建店铺行数据，账号下存在
三个平台的令牌，使用对应平台接口时则对应的使用该平台的登录令牌」*）。
`shops` 表、`platform_accounts.shop_id_fk`、`uq_platform_accounts_shop_id_fk`
全部退场；`seller_id` 下沉为 `platform_accounts` 的普通字段。

被取代的旧注释原句保留：

*「账号 ↔ 店铺**关系** | `platform_accounts.shop_id_fk`（真实外列，**1:1 + UNIQUE**）」*

⇒ 现在**关系不存在**，所以没有「绑店」这个动作，也没有「占用了」这个错误。

## 留下什么

| 角色 | 落点 |
|---|---|
| 千帆**凭据** | `account_cookie_versions` 的 `ark` 通道 |
| **状态与游标** | `platform_accounts.shop_state`（JSON 文本，读写只经本模块） |
| **店铺标识** | `platform_accounts.seller_id`（就是账号的一个属性） |

## ⚠️ 不变量：隔离键 ≠ 能力判据

「这个账号能不能用千帆」的唯一判据是 **`ark` 通道有没有令牌**
（`qianfan_cookies_for_account()` 返回非空），**不是** `seller_id` 有没有值。
把两者混为一谈正是账号 9 的 Bug：它有有效 `ark` 令牌，却因为
`shop_id_fk IS NULL`（当时被当成「未绑店」）被前后端双双拦下。

## 唯一取票入口

`qianfan_cookies_for_account()` **只认账号自己的 `ark` 通道**。原实现是「ark 优先、
绑定表兜底」，兜底整条在 2026-09-22 移除（用户口径：每个账号只使用自己账号对应的
平台有效登录令牌）—— 它是「一个语义两份真相源」的活标本：`product_notes.py` 曾
**绕过**取票函数直读绑定行，于是「账号补了自有 ark 令牌」在商品笔记生成这条路上
根本不生效。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import PlatformAccount
from backend.app.models.shop import ShopNoteGoods

#: `shop_state` 的键与默认值。**缺键必须读成这个**（而不是 `None`）：
#: 未同步过 = 游标 0 = 下一轮走全量；计数缺值渲染 0（与卡片口径同源）。
#: 这个字典是**唯一契约** —— 新增键只在这里加，读取侧不用各自兜底。
SHOP_STATE_DEFAULTS: dict[str, Any] = {
    "status": "",
    "status_message": "",
    "health_checked_at": "",
    "goods_note_total": 0,
    "goods_synced_at": "",
    "goods_cursor_ms": 0,
    # v1.32（设计 13 §30.3.5）：商品列表本地化后，页面要诚实展示「本地台账
    # N 件 · 同步于 X」。N 取同步时平台回报的 total；0 = 没同步过（不是
    # 「平台 0 件」—— 读取侧按缺值口径渲染）。
    "products_total": 0,
    "products_synced_at": "",
}


def shop_display_name(account: PlatformAccount) -> str:
    """店铺展示名（**全项目唯一实现**）。

    店铺实体已退场（ADR-021 / 设计 18），店名取「账号昵称」—— 千帆
    `get_seller_info()` 回的 `seller_name` 在登录时就写进了账号昵称的更新链路，
    这里不额外存一份名字（**同一语义不留两份实现**）。

    ⚠️ 商品页与订单页都要展示「所属店铺」⇒ 两处引用同一个函数，不许各写一份。
    """
    return (account.nickname or "").strip()


def get_shop_state(account: PlatformAccount) -> dict[str, Any]:
    """该账号的千帆侧状态（**缺键补默认、坏 JSON 返回默认**，不抛）。

    只保留契约里的键：历史遗留的键会在下次 `set_shop_state()` 时自然消失，
    而不是被无声地一直带下去。
    """
    try:
        parsed = json.loads(account.shop_state or "{}")
    except (ValueError, TypeError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return {**SHOP_STATE_DEFAULTS, **{k: parsed[k] for k in SHOP_STATE_DEFAULTS if k in parsed}}


def set_shop_state(account: PlatformAccount, **changes: Any) -> dict[str, Any]:
    """更新千帆侧状态并返回新状态。

    ⚠️ **必须赋新 dict**：`raw = account.shop_state` 之后原地改再赋回**同一个对象**，
    本项目的 JSON 列踩过这个坑 —— 历史比对认为没变 ⇒ **UPDATE 不发 ⇒ 改动静默丢失**，
    而源码看着完全正确。所以这里每次都构造新 dict（`{**old, **changes}`）。
    """
    state = {**get_shop_state(account), **changes}
    account.shop_state = json.dumps(state, ensure_ascii=False)
    return state


def parse_state_time(value: Any) -> datetime | None:
    """`shop_state` 里的时间字段（ISO 字符串）→ `datetime`；空 / 坏值 → `None`。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def account_for_ark_gate(db: Session, account_id: int) -> PlatformAccount | None:
    """取账号行做**千帆门禁**；不存在 / 已软删 → `None`。

    **软删闸**（DEF-055）：账号删了就当作没有千帆能力 —— 否则一个已被用户删掉的
    账号，其千帆凭据仍会被解析出来并拿去调千帆（商品列表 / 同步 / 商品挂链笔记反查）。

    取代了旧的 `account_shop_id()`：旧函数返回 `shop_id_fk`（一个**关系**），
    新函数返回账号本身（因为门禁要问的是**令牌**，不需要任何关系）。
    """
    account = db.get(PlatformAccount, account_id)
    if account is None or account.deleted_at is not None:
        return None
    return account


def has_ark_session(db: Session, account_id: int) -> bool:
    """该账号是否有**可用的千帆（ark）会话** —— 商品页门禁的唯一判据。

    ⚠️ 判据是「`ark` 通道有令牌」，**不是** `seller_id` 有值、也不是任何店铺关系。
    账号 9 的 Bug 正是把后者当成了前者。

    软删账号恒为 `False`（DEF-055 同型：删除的数据不得再被使用）。
    """
    return bool(qianfan_cookies_for_account(db, account_id))


def qianfan_cookies_for_account(db: Session, account_id: int) -> str:
    """千帆调用该用哪份 Cookie：**账号自己的 `ark` 通道，没有就是空串**。

    ⚠️ 2026-09-22 用户拍板「每个账号只使用自己账号对应的平台有效登录令牌」：
    原实现（§11.6 选 C）是「ark 通道优先、`shop_account_bindings` 兜底」，
    兜底整条**已移除**。**取不到就如实返回空**，由调用方给结构化原因让用户去补登录 ——
    **不向任何其他账号 / 任何历史行借令牌**。

    ⚠️ `ark` 通道**只放千帆 jar**（按 `channel_for_cookies` 形态判定）；创作者平台的
    jar 落在 `creator` 通道，不会被这里拿到 —— 拿创作者 jar 打千帆只会拿到
    `-13000 缺失登录凭证`（观隅好物的教训）。

    软删账号返回空串（DEF-055 同型：删除的数据不得再被使用）。
    """
    account = db.get(PlatformAccount, account_id)
    if account is None or account.deleted_at is not None:
        return ""
    from backend.app.services.account_service import CHANNEL_ARK, latest_cookies_for_channel

    return latest_cookies_for_channel(db, account_id, CHANNEL_ARK)


# ⚠️ 已退场（ADR-021 / 设计 18，2026-09-30）—— 下面这批函数整条删除，原句保留在
# 设计文档与 ADR 里，此处只记名字以免后来者重新发明：
#
#   attach_shop / ShopBindError / bind_shop_for_account / detach_shop
#   account_for_shop / any_account_for_shop / shops_for_user / upsert_shop
#
# 为什么能删干净：**关系不存在了**。绑店 / 解绑 / 占用检查 / 换店作废旧数据这一整套
# 逻辑的存在前提是「账号 → 店铺」是一条需要维护的关系；改用「能不能调千帆 = ark
# 令牌有没有」之后，这些动作连语义都没有了。


def synced_note_count(db: Session, account_id: int) -> int:
    """该账号已落库的商品笔记映射条数。"""
    return len(
        db.scalars(
            select(ShopNoteGoods.id).where(ShopNoteGoods.platform_account_id == account_id)
        ).all()
    )
