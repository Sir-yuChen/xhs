"""账号 ↔ 千帆店铺：关系、千帆侧状态与取票（2026-09-22 重构）。

## 这次改了什么，为什么

`shop_account_bindings` 那张表同时干**三件不相关的事**（账号↔店铺关系 / 千帆凭据
仓库 / 同步状态机）。2026-09-22 用户拍板把三件事拆开：

| 角色 | 现在的落点 |
|---|---|
| 账号 ↔ 店铺**关系** | `platform_accounts.shop_id_fk`（真实外列，**1:1 + UNIQUE**） |
| 千帆**凭据** | `account_cookie_versions` 的 `ark` 通道（与 www / creator 同一个仓库） |
| **状态与游标** | `platform_accounts.shop_state`（JSON 文本，读写只经本模块） |

## 「同店多账号」已被否决（用户 2026-09-22 口径）

原方案是 N:1（多个子账号读同一个店）。**用户拍板不允许**：*不会再出现「同店铺、
多账号」场景；一个店铺下只需存多个平台的会话令牌* —— 令牌本来就都在该账号自己的
`account_cookie_versions` 里（`www` / `creator` / `ark` 三通道）。所以关系是 **1:1**，
并且由 DB 层的 `UNIQUE(platform_accounts.shop_id_fk)` **强制**：
靠约定拦不住，靠约束才拦得住。

副作用：`shop_account_count` 恒为 1，已从接口响应里移除（前端「同店 N 个账号」的
展示随之删掉）。绑第二个账号到同一个店会被拒（409 + 可读原因）。

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
from backend.app.models.shop import Shop, ShopNoteGoods

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


def account_shop_id(db: Session, account_id: int) -> int | None:
    """账号当前关联的店铺主键；没有 / 已软删 → `None`。

    **软删闸**（DEF-055）：账号删了就当作没有店铺 —— 否则一个已被用户删掉的账号，
    其千帆凭据仍会被解析出来并拿去调千帆（商品列表 / 同步 / 商品挂链笔记反查）。
    """
    account = db.get(PlatformAccount, account_id)
    if account is None or account.deleted_at is not None:
        return None
    return account.shop_id_fk


def shop_for_account(db: Session, account_id: int) -> Shop | None:
    """账号关联的店铺实体（关系闸与软删闸见 `account_shop_id`）。

    **租户闸**：店铺是按用户维度的实体（唯一约束 `(user_id, seller_id)`），
    所以只认与账号同租户的店铺行 —— 少了它，一条**租户错位**的残留引用会被当成
    这个账号的合法店铺（真实库实测存在过这种形状：`user_id=1` 的测试遗留挂在本租户
    的店铺上，界面因此显示「同店 2 个账号」）。
    """
    shop_pk = account_shop_id(db, account_id)
    if shop_pk is None:
        return None
    shop = db.get(Shop, shop_pk)
    if shop is None:
        return None
    account = db.get(PlatformAccount, account_id)
    if account is not None and shop.user_id != account.user_id:
        return None
    return shop


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


def attach_shop(db: Session, account: PlatformAccount, shop_row: Shop) -> None:
    """把账号关联到这个店铺（幂等：重复调用结果相同）。

    **换店 = 旧店的一切作废**：如果账号原本挂着**另一个**店铺，先把旧的解掉 ——
    包括它名下的本地商品映射。为什么必须清：`shop_note_goods` 是按**账号**挂的
    （不区分店），不清就会把旧店的挂链商品串到新店的数据里，而页面上看不出错。

    **不 commit**（由调用方决定事务边界）。
    """
    if account.shop_id_fk is not None and account.shop_id_fk != shop_row.id:
        detach_shop(db, account)
    account.shop_id_fk = shop_row.id


class ShopBindError(Exception):
    """绑店失败（**业务结果**，不是程序错误）。

    `code` / `status_code` 由 HTTP 层原样映射（409 + 可读原因），
    与 `AccountRejectedError` 同型：调用点不再各写一套 `if/raise`。
    """

    code = "QIANFAN_SHOP_BIND_FAILED"
    status_code = 409

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


def bind_shop_for_account(
    db: Session,
    *,
    user_id: int,
    account: PlatformAccount,
    shop_user_id: str,
    shop_name: str,
) -> Shop:
    """把千帆店铺绑到账号上（关系 + 状态）。**这是全项目唯一的绑店实现。**

    ## 为什么要有它（2026-09-26 用户实测）

    原先只有 `POST /qianfan/bind`（用户手动粘贴 Cookie）建关系，而
    **扫码 / 邮箱密码登录这两条路只写令牌**。于是「补登千帆成功」之后账号仍然
    `shop_id_fk IS NULL`：我的笔记页与商品列表照旧显示
    「当前账号尚未绑定店铺」，用户明明刚在同一个弹窗里登录过。
    登录时已经把店铺信息读出来了（`get_shop_info`），不落关系等于白读。

    幂等：重复绑同一个店 = 重新验一次凭据（`upsert_shop` 只更新非空店名）。

    :raises ShopBindError: 店铺已被**别的**账号占用（1:1 闸）。
    """
    if not (shop_user_id or shop_name):
        raise ShopBindError(
            "千帆响应中既没有店铺名也没有店铺 ID，无法确认绑定对象",
            code="QIANFAN_SHOP_UNKNOWN",
        )
    shop_row = upsert_shop(db, user_id, seller_id=shop_user_id, shop_name=shop_name)

    # 1:1 闸（用户 2026-09-22 口径：不允许「同店铺、多账号」）。DB 层另有部分唯一索引
    # `uq_platform_accounts_shop_id_fk` 兜底；这里是为了给一句人话，而不是让用户
    # 看到一句 IntegrityError。
    holder = account_for_shop(db, shop_row.id, user_id=user_id)
    if holder is not None and holder.id != account.id:
        raise ShopBindError(
            f"店铺「{shop_row.shop_name or shop_row.seller_id}」已经绑在账号"
            f"「{holder.nickname or holder.id}」上。一个店铺只能绑一个账号，"
            "请先在那个账号上解绑。",
            code="QIANFAN_SHOP_TAKEN",
        )
    # 全库范围再查一次：唯一索引是**全局**的，而上面的归属闸是**租户内**的。
    foreign_holder = any_account_for_shop(db, shop_row.id)
    if foreign_holder is not None and foreign_holder.id != account.id:
        raise ShopBindError(
            f"店铺「{shop_row.shop_name or shop_row.seller_id}」已被另一个账号占用"
            "（可能是历史测试数据留下的引用）。请在那个账号上解绑，或联系管理员清理。",
            code="QIANFAN_SHOP_TAKEN",
        )

    attach_shop(db, account, shop_row)
    # 刚用这份 Cookie 验过 ⇒ `active`，旧原因一并清掉。
    set_shop_state(account, status="active", status_message="")
    return shop_row


def detach_shop(db: Session, account: PlatformAccount) -> int:
    """解除**本账号**与店铺的关联，并删掉它的本地商品映射。返回删除的映射行数。

    * 只动**本账号**的映射与关联（别的账号的数据一律不碰）；
    * 关联清空后若这个店不再被任何存活账号使用（1:1 下几乎总是如此），把店铺行也删掉
      —— 否则「解绑了却还在店铺列表里」会被读成「还绑着」。
    """
    rows = db.scalars(
        select(ShopNoteGoods).where(ShopNoteGoods.platform_account_id == account.id)
    ).all()
    removed = len(rows)
    for row in rows:
        db.delete(row)

    shop_pk = account.shop_id_fk
    account.shop_id_fk = None
    set_shop_state(
        account,
        status="",
        status_message="",
        health_checked_at="",
        goods_note_total=0,
        goods_synced_at="",
        goods_cursor_ms=0,
    )
    # 先落库再判孤儿：`account_for_shop` 查的是「还有谁引用」（关联已经改过了）。
    db.flush()

    if shop_pk is not None and account_for_shop(db, shop_pk, user_id=account.user_id) is None:
        shop = db.get(Shop, shop_pk)
        if shop is not None:
            db.delete(shop)
    return removed


def account_for_shop(db: Session, shop_pk: int, *, user_id: int) -> PlatformAccount | None:
    """该店铺当前被哪个**本租户存活账号**使用（1:1，至多一条）。

    两道闸门，缺一不可：

    * **租户闸**（DEF-054）—— `shops` 是按用户维度的实体（唯一约束
      `(user_id, seller_id)`），所以「这个店归谁」是一个**租户内**的事实。少了这道闸，
      别的用户名下的账号会被当成这个店的占用者，界面就会显示一个**当前用户根本选不到**
      的账号（真实库实测：界面显示「同店 2 个账号」，其中一个是别人的残留行）。
    * **软删闸**（DEF-055）—— 账号已软删，它的关联就**不再算占用**，也不该再顶住
      `detach_shop` 的孤儿清理（否则店铺行**永久**删不掉）。

    `user_id` 设成必传的关键字参数（而非可选）：漏传就等于漏闸，让它**在语法上无法被忘记**。
    """
    return db.scalars(
        select(PlatformAccount)
        .where(
            PlatformAccount.shop_id_fk == shop_pk,
            PlatformAccount.user_id == user_id,
            PlatformAccount.deleted_at.is_(None),
        )
        .order_by(PlatformAccount.id)
        .limit(1)
    ).first()


def any_account_for_shop(db: Session, shop_pk: int) -> PlatformAccount | None:
    """该店铺当前被哪个**存活**账号占用（**不设租户闸**，只用于给人话）。

    为什么要它：`UNIQUE(platform_accounts.shop_id_fk)` 是**全局**约束，而
    `account_for_shop` 带了租户闸 —— 一条**别的租户**的残留引用（真实库出现过：
    脚本直写留下 `user_id=1` 的测试账号挂在本租户的店铺上）会被租户闸滤掉，
    于是本地检查放行、DB 约束却拒绝，用户看到的是一个 500 而不是一句原因。
    这里补一次「全库范围」的占用检查，只为把 IntegrityError 换成 409 + 人话。

    **不得**把它当成归属判据（那会漏掉租户闸与软删闸）：归属一律用 `account_for_shop`。
    """
    return db.scalars(
        select(PlatformAccount)
        .where(PlatformAccount.shop_id_fk == shop_pk, PlatformAccount.deleted_at.is_(None))
        .order_by(PlatformAccount.id)
        .limit(1)
    ).first()


def shops_for_user(db: Session, user_id: int) -> list[Shop]:
    return list(
        db.scalars(select(Shop).where(Shop.user_id == user_id).order_by(Shop.id)).all()
    )


def synced_note_count(db: Session, account_id: int) -> int:
    """该账号已落库的商品笔记映射条数。"""
    return len(
        db.scalars(
            select(ShopNoteGoods.id).where(ShopNoteGoods.platform_account_id == account_id)
        ).all()
    )


def upsert_shop(db: Session, user_id: int, *, seller_id: str, shop_name: str) -> Shop:
    """按 `(user_id, seller_id)` 取或建店铺实体（唯一约束同口径）。

    只更新**非空**店名：千帆偶发不返回 `seller_name` 时，不要把已有店名擦成空串
    （展示字段被静默清空是「看不出错」的那类问题）。

    **没有 `seller_id` 时按店名兜底**：千帆理论上总会给 `user_id`，但真缺了也不能让
    「每个账号造一行店铺」——那就退化成 1:1 了。所以此时按 `(user_id, shop_name)` 匹配，
    找不到才新建，`seller_id` 保持空串（**不编一个假的 ID 出来**）。
    """
    shop = db.scalars(
        select(Shop).where(Shop.user_id == user_id, Shop.seller_id == seller_id)
    ).first()
    if shop is None and not seller_id and shop_name:
        shop = db.scalars(
            select(Shop).where(Shop.user_id == user_id, Shop.shop_name == shop_name)
        ).first()
    if shop is None:
        shop = Shop(user_id=user_id, seller_id=seller_id, shop_name=shop_name)
        db.add(shop)
        db.flush()
    elif shop_name and shop.shop_name != shop_name:
        shop.shop_name = shop_name
        db.flush()
    return shop
