"""店铺违规管理 · 关联内容删除（设计 44，2026-10-18）

## 用户口径（2026-10-18，本模块的唯一依据）

> 「改为删除平台数据后，检查本地，本地存在对应的数据则删除，不存在则跳过，
>  所有平台操作均可以使用这种方式，**先操作平台数据再检查本地是否存在对应数据，
>  存在则执行操作，不存在则跳过**」

⇒ 顺序被钉死：**平台先行，本地跟随**。

| 阶段 | 做什么 | 失败怎么办 |
|---|---|---|
| ① 平台删除 | 商品 → `product/batch_delete_sku`；笔记 → 商业笔记 `note/batch_delete_note`、普通笔记 `creator/delete_note` | 平台失败 ⇒ **本地不动**（平台还留着、本地已删 = 两边对不上），如实回报失败原因 |
| ② 本地核对 | 平台成功后，查本地有没有这条数据 | 本地**没有** ⇒ `skipped`（这很正常：本系统只存用户同步过的数据），**不算失败** |
| ③ 本地删除 | 本地有 ⇒ 删本地行（含全部从属行） | 本地删除异常 ⇒ 如实回报，但平台已删的事实不改 |

## 为什么「本地没有就跳过」是对的

本地是**缓存**，不是平台的全量镜像 —— 用户可能从没同步过这条商品/笔记。
把它当失败会让「删 10 条 → 7 条报错」看起来像平台坏了，实际是本地本来就没有。

## 边界

- **违规记录本身删不掉**：千帆违规域没有删除接口（调研已确认），违规单是平台处罚凭证。
  所以本模块删的永远是违规**指向的商品 / 笔记**，文案也这么写。
- 店铺(0) / 账号(2) / 直播间(4)：没有可删的实体 ⇒ `skipped` + 原因。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.app.models import (
    Note,
    NoteGoods,
    PlatformAccount,
    ShopNoteGoods,
    ShopProduct,
    User,
)
from backend.app.services.note_purge_service import purge_note

logger = logging.getLogger(__name__)

#: 违规对象类型码（真机 `violationEntityType.type`）。
ENTITY_SHOP = 0
ENTITY_ITEM = 1
ENTITY_ACCOUNT = 2
ENTITY_NOTE = 3
ENTITY_LIVE = 4

#: 有平台删除能力 + 可能有本地缓存的两类。
DELETABLE_ENTITY_TYPES = (ENTITY_ITEM, ENTITY_NOTE)

#: 单次请求条数上限。
MAX_DELETE_TARGETS = 200

#: 单条结果状态。
STATUS_DELETED = "deleted"      # 平台删成功 + 本地有 ⇒ 本地也删了
STATUS_PLATFORM_ONLY = "platform_only"  # 平台删成功 + 本地没有 ⇒ 跳过本地（不是失败）
STATUS_FAILED = "failed"        # 平台删除失败 ⇒ 本地不动
STATUS_SKIPPED = "skipped"      # 该对象没有可删实体（店铺/账号/直播间）
#: 前置检查判定**平台已无此数据**，且本地也没有 ⇒ 已在别处删过，不重复删
#: （用户口径 2026-10-19：「如果本地数据也不存在则提示，数据已经被删除，
#:   不要重复删除，如果为批量操作则直接跳过这一条数据」）
STATUS_ABSENT = "absent"


def entity_type_label(entity_type: int | None) -> str:
    return {
        ENTITY_SHOP: "店铺",
        ENTITY_ITEM: "商品",
        ENTITY_ACCOUNT: "账号",
        ENTITY_NOTE: "笔记",
        ENTITY_LIVE: "直播间",
    }.get(entity_type if entity_type is not None else -1, "未知对象")


def is_deletable(entity_type: int | None) -> bool:
    """这一行有没有「可删的东西」。"""
    return entity_type in DELETABLE_ENTITY_TYPES


def _owned_account(db: Session, user: User, platform_account_id: int) -> PlatformAccount:
    from fastapi import HTTPException, status

    account = db.get(PlatformAccount, platform_account_id)
    if account is None or account.user_id != user.id or account.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "ACCOUNT_NOT_FOUND", "message": "平台账号不存在或不属于当前用户"},
        )
    return account


def _fk_referencing(db: Session, table: str) -> list[tuple[str, str]]:
    """查出**真实库**里所有外键指向 `table.id` 的 (表名, 列名)。

    ⚠️ 不能写死表名单：本需求第一版按 ORM 声明手写了 4 张表，
    而真实库里 `generation_records` 对 `shop_products` 的外键**已经不存在**
    （改成了 `parent_record_id` 自引用）—— 写死就会「多删一张本不该删的表」
    或「漏删一张实际存在的」（2026-10-18 实测）。
    以 PRAGMA 的真实外键为准，表结构以后变化也不用改代码。
    """
    from sqlalchemy import text

    refs: list[tuple[str, str]] = []
    rows = db.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars().all()
    for other in rows:
        if other in {"sqlite_sequence", table}:
            continue
        for fk in db.execute(text(f'PRAGMA foreign_key_list("{other}")')).all():
            # PRAGMA 列序：id, seq, table, from, to, on_update, on_delete, match
            if fk[2] == table:
                refs.append((other, fk[3]))
    return refs


def purge_local_item(db: Session, user: User, account: PlatformAccount, item_id: str) -> bool:
    """删本地商品台账行 + **全部从属行**。返回「本地到底有没有删掉」。

    顺序：① `note_goods` 关系行（按**平台 ID**，§26.7）→ ② 按真实外键逐表清引用
    → ③ 删 `shop_products` 本体。

    ⚠️ 从属表**按 PRAGMA 实测外键动态取**（见 `_fk_referencing`），不写死名单 ——
    写死会随表结构演进而漏删/误删（本需求第一版就踩过）。
    ⚠️ 自引用（如 `parent_record_id`）会被 PRAGMA 报成「指向自己」，
    这里按「先清指向本体行的行」处理，删本体时已无引用。
    """
    from sqlalchemy import text

    product_pk = db.scalar(
        select(ShopProduct.id).where(
            ShopProduct.platform_account_id == account.id,
            ShopProduct.item_id == item_id,
        )
    )
    if product_pk is None:
        return False

    # ① 关系行（按**平台 item_id** 删，与 §26.7 一致）
    #    ⚠️ 有两张「笔记↔商品」关系表，都按 item_id 挂、都**没有外键**
    #    （PRAGMA 查不到 ⇒ `_fk_referencing` 覆盖不了）：
    #      · `note_goods`      —— 内容库侧的笔记↔商品
    #      · `shop_note_goods` —— 千帆商品笔记列表的本地映射（1977 行，实测）
    #    漏掉任一张 ⇒ 商品删了、笔记页还挂着「已下架商品」（2026-10-18 实测补漏）
    db.execute(delete(NoteGoods).where(
        NoteGoods.user_id == user.id, NoteGoods.item_id == item_id
    ))
    db.execute(delete(ShopNoteGoods).where(
        ShopNoteGoods.platform_account_id == account.id,
        ShopNoteGoods.item_id == item_id,
    ))

    # ② 从属行：按真实外键逐表清
    for table, column in _fk_referencing(db, "shop_products"):
        db.execute(
            text(f'DELETE FROM "{table}" WHERE "{column}" = :pk'), {"pk": product_pk}
        )

    # ③ 本体
    db.execute(delete(ShopProduct).where(ShopProduct.id == product_pk))
    return True


def purge_local_note(db: Session, user: User, account: PlatformAccount, note_id: str) -> bool:
    """删本地笔记（`purge_note`，含全部从属行）。返回「本地到底有没有删掉」。

    `purge_note` 是项目里**笔记删除链的唯一实现**（删笔记 / 删话题 / 对账三处共用），
    这里直接用它，不复制。
    """
    note = db.scalar(
        select(Note).where(
            Note.user_id == user.id,
            Note.platform_account_id == account.id,
            Note.note_id == note_id,
        )
    )
    if note is None:
        return False
    purge_note(db, note)
    return True


def _delete_platform_note(
    db: Session, user: User, account: PlatformAccount, note_id: str
) -> tuple[bool, str]:
    """删平台笔记：**创作者通道先试 → 千帆兜底 → 任一成功即止**。

    用户口径（2026-10-18）：

    > 「商业笔记和普通笔记均调用一次，先走普通再走千帆，如果其中一个成功则不再走其它的」

    ## 为什么不再按 `note_flag` 分流

    旧实现按 `note_datacenter_stats.note_flag == "goods_note"` 选通道，有两个坑：

    1. **本地没有统计行 ⇒ 一律判普通**（创作者）。而账号 8 实测创作者会话已过期
       （接口回「登录已过期，请重新登录」），千帆 ark 却完全可用 ⇒ 能删的笔记被判失败
       （用户实际撞到：「该账号没有创作者登录态，无法删除笔记」）。
    2. 类型是**平台侧**的事实，本地统计只是它的缓存；缓存缺失/过期不该决定能不能删。

    ## 现在的顺序

    1. **创作者通道**（`validate_delete_permission` + `delete_note`）—— 普通笔记的正路；
    2. 失败（含没有会话）⇒ **千帆 `note/batch_delete_note`** 再试一次；
    3. **任一成功即返回成功，不再调另一条**（避免对同一条笔记重复删、也避免误报失败）。

    ⚠️ 两条都失败时，**两边的原文都带出来** —— 只报最后一条会让用户看不出真正原因。
    ⚠️ 顺序固定为「普通 → 千帆」，与笔记类型无关（用户明确「均调用一次」）。
    """
    from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter
    from backend.app.adapters.xhs.qianfan_api_adapter import XhsQianfanApiAdapter
    from backend.app.services.account_service import (
        CHANNEL_ARK,
        CHANNEL_CREATOR,
        latest_cookies_for_channel,
    )

    # ---- ① 创作者通道（普通笔记的正路）
    creator_cookies = latest_cookies_for_channel(db, account.id, CHANNEL_CREATOR)
    if creator_cookies:
        adapter = XhsCreatorApiAdapter(creator_cookies)
        try:
            ok, msg, _raw = adapter.validate_delete_permission(note_id)
            if ok:
                ok, msg, _raw = adapter.delete_note(note_id)
        except Exception as exc:  # noqa: BLE001 - 网络/解析异常也要落到千帆兜底
            ok, msg = False, f"创作者通道异常：{type(exc).__name__}: {exc}"
        if ok:
            return True, ""
        creator_failure = msg or "平台删除失败"
    else:
        creator_failure = "该账号没有创作者登录态"

    # ---- ② 千帆通道（兜底）
    ark_cookies = latest_cookies_for_channel(db, account.id, CHANNEL_ARK)
    if ark_cookies:
        try:
            ok, msg, _raw = XhsQianfanApiAdapter(ark_cookies).batch_delete_notes([note_id])
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"千帆通道异常：{type(exc).__name__}: {exc}"
        if ok:
            return True, ""
        ark_failure = msg or "平台删除失败"
    else:
        ark_failure = "该账号没有千帆登录态"

    # ---- ③ 两条都不通：两边原文都带出来
    return False, f"普通笔记通道：{creator_failure}；千帆通道：{ark_failure}"


def _delete_platform_item(db: Session, account: PlatformAccount, item_id: str) -> tuple[bool, str]:
    """删平台商品（千帆 `product/batch_delete_sku`）。"""
    from backend.app.adapters.xhs.qianfan_api_adapter import XhsQianfanApiAdapter
    from backend.app.services.account_service import CHANNEL_ARK, latest_cookies_for_channel

    cookies = latest_cookies_for_channel(db, account.id, CHANNEL_ARK)
    if not cookies:
        return False, "该账号没有千帆登录态，无法删除平台商品"
    ok, msg, _raw = XhsQianfanApiAdapter(cookies).remove_items([item_id])
    return (True, "") if ok else (False, msg or "平台删除失败")


#: 平台返回「查不到详情」的判定词（各接口措辞不一，统一按语义归一）
_ABSENT_HINTS = ("不存在", "未找到", "not found", "已被删除", "已删除", "not exist")


def _detail_is_empty(payload: Any) -> bool:
    """平台「查不到详情」的统一判据：payload 为空 / 空 dict / 空 list。

    各接口的「不存在」表现形式不同（有的返回 `data: null`，有的返回空列表，
    有的把目标 ID 从 map 里省掉）⇒ 统一收敛成「有没有拿到这个对象」。
    """
    if payload is None:
        return True
    if isinstance(payload, (dict, list, str, tuple, set)) and len(payload) == 0:
        return True
    return False


def check_platform_exists(
    db: Session, account: PlatformAccount, entity_type: int, entity_id: str
) -> tuple[bool, str]:
    """**删除前置检查**：按 ID 查平台详情，返回 `(存在, 说明)`。

    用户口径（2026-10-19）：
    > 「加一个删除平台数据的前置逻辑，根据笔记或商品ID检查商品是否存在，
    >   返回详情则存在不返回则表示平台没有对应数据」

    为什么必须做这一步：平台删除接口**不做存在性校验** ——
    实测同一个商品连删 3 次，平台每次都回 `{"code":200,"success":true}`，
    即使商品早就没了（2026-10-19 真机）。不先查，用户会收到「删除成功」而
    实际上什么也没发生，也无法区分「真删了」与「本来就没了」。

    | 实体 | 查询通路 | 判据 |
    |---|---|---|
    | 商品 | `get_skus_by_item_ids([id])` | `skusMap` 里有该 ID |
    | 笔记 | `get_goods_note_page(note_ids=[id])` | `note_infos` 里有该 ID |

    ⚠️ **按 ID 精确查询，不翻页**（用户口径 2026-10-19：「不允许使用翻页方式，
    使用商品id调用」）—— 两个接口都是服务端按 ID 过滤。
    ⚠️ 查询本身失败（网络 / 令牌过期）不等于「不存在」：那种情况返回
    `(True, 原因)` 让删除照常走，由删除接口去报真实错误 —— 否则一次网络抖动
    会被误报成「平台没有这条数据」。
    """
    from backend.app.adapters.xhs.qianfan_api_adapter import XhsQianfanApiAdapter
    from backend.app.services.account_service import CHANNEL_ARK, latest_cookies_for_channel

    cookies = latest_cookies_for_channel(db, account.id, CHANNEL_ARK)
    if not cookies:
        return True, "没有千帆登录态，跳过前置检查（由删除步骤报真实原因）"
    adapter = XhsQianfanApiAdapter(cookies)

    if entity_type == ENTITY_ITEM:
        ok, msg, raw = adapter.get_skus_by_item_ids([entity_id])
        if not ok:
            return True, f"前置检查未完成（{msg}），继续按原流程删除"
        skus_map = ((raw or {}).get("data") or {}).get("skusMap") or {}
        hit = [k for k in skus_map if str(k) == str(entity_id)]
        if hit and not _detail_is_empty(skus_map.get(hit[0])):
            return True, f"平台存在（{len(skus_map[hit[0]] or [])} 个 SKU）"
        return False, "平台查不到该商品详情，判定平台已无此数据"

    # 笔记
    ok, msg, raw = adapter.get_goods_note_page(note_ids=[entity_id], page_size=20)
    if not ok:
        return True, f"前置检查未完成（{msg}），继续按原流程删除"
    infos = ((raw or {}).get("data") or {}).get("note_infos") or []
    hit = [n for n in infos if str(n.get("note_id")) == str(entity_id)]
    if hit:
        return True, "平台存在"
    return False, "平台查不到该笔记详情，判定平台已无此数据"


def _one_target(
    db: Session,
    user: User,
    account: PlatformAccount,
    entity_type: int,
    entity_id: str,
) -> dict[str, Any]:
    """处理一条：**前置查平台 → 平台删 → 核对本地**。

    用户口径（2026-10-19）：

    > 「加一个删除平台数据的前置逻辑，根据笔记或商品ID检查商品是否存在，
    >   返回详情则存在不返回则表示平台没有对应数据」
    > 「查不到则不在走平台删除落，直接走本地，看本地数据是否存在，
    >   如果本地数据也不存在则提示，数据已经被删除，不要重复删除，
    >   如果为批量操作则直接跳过这一条数据」

    分档：

    | 平台详情 | 本地 | 动作 | 回执 |
    |---|---|---|---|
    | 有 | 有 | 删平台 → 删本地 | `deleted` |
    | 有 | 无 | 删平台 → 本地跳过 | `platform_only` |
    | 无 | 有 | **不调平台删除** → 只删本地 | `deleted` |
    | 无 | 无 | 什么都不做 | `absent`（「数据已经被删除，不要重复删除」） |
    """
    base = {"entity_type": entity_type, "entity_id": entity_id}

    if entity_type == ENTITY_SHOP:
        return {**base, "ok": True, "status": STATUS_SKIPPED,
                "reason": "店铺没有可删除的实体"}
    if entity_type == ENTITY_ACCOUNT:
        return {**base, "ok": True, "status": STATUS_SKIPPED,
                "reason": "账号是登录主体，不在违规页删除"}
    if entity_type == ENTITY_LIVE:
        return {**base, "ok": True, "status": STATUS_SKIPPED,
                "reason": "直播间本系统不存本地实体"}

    # ① 前置检查：按 ID 查平台详情（不翻页、不猜）
    on_platform, check_note = check_platform_exists(db, account, entity_type, entity_id)

    # ② 平台没有 ⇒ **不走平台删除**，只看本地
    if not on_platform:
        local_removed = (
            purge_local_item(db, user, account, entity_id)
            if entity_type == ENTITY_ITEM
            else purge_local_note(db, user, account, entity_id)
        )
        if local_removed:
            return {**base, "ok": True, "status": STATUS_DELETED,
                    "reason": f"{check_note}；已清理本地数据"}
        return {**base, "ok": True, "status": STATUS_ABSENT,
                "reason": "数据已经被删除，不要重复删除"}

    # ③ 平台有 ⇒ 平台先行
    if entity_type == ENTITY_ITEM:
        ok, msg = _delete_platform_item(db, account, entity_id)
    else:
        ok, msg = _delete_platform_note(db, user, account, entity_id)

    if not ok:
        # 平台没删掉 ⇒ 本地**不动**（否则两边对不上）
        return {**base, "ok": False, "status": STATUS_FAILED,
                "reason": f"平台删除失败：{msg}（本地未改动）"}

    # ④ 平台已删 ⇒ 核对本地
    local_removed = (
        purge_local_item(db, user, account, entity_id)
        if entity_type == ENTITY_ITEM
        else purge_local_note(db, user, account, entity_id)
    )
    if not local_removed:
        return {**base, "ok": True, "status": STATUS_PLATFORM_ONLY,
                "reason": "平台已删除；本地没有这条数据，已跳过"}
    return {**base, "ok": True, "status": STATUS_DELETED,
            "reason": "平台已删除；本地数据也已删除"}


def execute_delete(
    db: Session,
    user: User,
    platform_account_id: int,
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    """删除入口。调用方负责 commit。

    返回：
    ```
    {
      "requested": n,
      "deleted": 平台删成功且本地也删的条数,
      "platform_only": 平台删成功但本地没有（跳过）的条数,
      "failed": 平台删除失败的条数,
      "skipped": 该类型没有可删实体的条数,
      "absent": 平台已无此数据且本地也没有（已在别处删过，不重复删）的条数,
      "results": [...]
    }
    ```
    ⚠️ 四类分别计数 —— 把它们混成一个 `deleted` 会让用户看不出「哪几条其实没动」。
    """
    account = _owned_account(db, user, platform_account_id)

    results: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for raw in targets:
        entity_type = raw.get("entity_type")
        entity_id = str(raw.get("entity_id") or "").strip()
        if entity_type is None or not entity_id:
            results.append({"entity_type": entity_type, "entity_id": entity_id,
                            "ok": False, "status": STATUS_FAILED,
                            "reason": "缺少对象类型或 ID"})
            continue
        try:
            code = int(entity_type)
        except (TypeError, ValueError):
            results.append({"entity_type": entity_type, "entity_id": entity_id,
                            "ok": False, "status": STATUS_FAILED,
                            "reason": "对象类型非法"})
            continue
        key = (code, entity_id)
        if key in seen:  # 去重：同一条传两次不该删两次
            continue
        seen.add(key)
        results.append(_one_target(db, user, account, code, entity_id))

    def count(status: str) -> int:
        return sum(1 for r in results if r.get("status") == status)

    summary = {
        "requested": len(targets),
        "deleted": count(STATUS_DELETED),
        "platform_only": count(STATUS_PLATFORM_ONLY),
        "failed": count(STATUS_FAILED),
        "skipped": count(STATUS_SKIPPED),
        "absent": count(STATUS_ABSENT),
        "results": results,
    }
    logger.info(
        "violation related delete: account=%s %s", account.id,
        {k: summary[k] for k in (
            "requested", "deleted", "platform_only", "failed", "skipped", "absent"
        )},
    )
    return summary
