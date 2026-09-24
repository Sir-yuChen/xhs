"""千帆「商品笔记」映射的**存储与按需补齐**（T-30 新方案）。

## 为什么是「按需补齐」而不是「后台全量同步」

店铺可能有近 2000 条商品笔记（真机 1968），而「我的笔记」一页只有 10 条。
两种补数据的方式：

| 方案 | 成本 | 结论 |
|---|---|---|
| 后台全量慢跑 | 每个店铺要翻 ~百页，且**用户可能永远不看**那些笔记 | ❌ 被否（用户 2026-09-19 明确要求不做） |
| **按需补齐**（本模块） | 只看用户真正打开的那一页：**每页 ≤1 次只读请求**，`note_ids` 逗号拼接一次拿到 | ✅ 采用 |

## 关键事实（2026-09-19 实测，纠正早前结论）

`GET /api/edith/goods-note/list` **有**按笔记查询的口子，但参数名是**复数** `note_ids`：

| 输入 | total | 说明 |
|---|---|---|
| `note_ids=<真实 note_id>` | 1 | 精确命中，行的 `item_id` 与店铺口径一致 |
| `note_ids=<不存在的 id>` | 0 | 真在过滤（不是恰好一条） |
| `note_ids=a,b,c…`（2/6/10/20/50 个） | = 个数 | **一页笔记一次请求**的依据 |
| `note_ids` 混入不存在的 id | 仍是真实条数 | 不会污染结果 |
| 单数 `note_id` / `keyword` / `search_filter.*` 等 15 个候选 | 1968（= 全量） | 被平台忽略 |

早前「没有按 note_id 查询的口子」的结论来自**只试了单数 `note_id`**，此处以实测更正
（设计文档 §24.1 已同步标注被取代）。

## 两条纪律

* **只写本页**：只 upsert 传进来的那批 `note_id`，**从不删除**任何行（删除只发生在
  完整翻页同步里，见 `qianfan.py::sync_goods`）；`goods_note_total`（覆盖率分母）
  也**只在无过滤的翻页同步里更新** —— 带 `note_ids` 的响应里 `total` 是**过滤后条数**，
  拿它当店铺总数会把分母写坏（1968 → 2）。
* **失败静默**：浏览页面时千帆挂了不该让人看不到笔记；补齐失败就照常渲染（商品缺）。
  唯一的例外是凭据失效（`-13000`）：把绑定标 `invalid` 并写原因，让页面的失效提示接管。

## 笔记 ↔ 商品关系（设计 13 §26，2026-09-22）

``note_goods`` 是**本地实体关系表**（`notes.id` ↔ `shop_products.id`，M2M）。
本模块提供它的**唯一解析入口** `link_note_goods`：

* **纯本地解析、零平台请求** —— 只读 `shop_note_goods` 缓存 / 调用方给的挂载条目，
  在本地 `notes` 与 `shop_products` 里找主键；
* **两边都解析成功才建关系** —— 任一侧本地行缺失（商品还没同步 / 笔记还没落库）⇒ 跳过，
  **不造假**，等下一次同步自然补上；
* **只增不删**（§23：同步链路不做删除维度）。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.time import shanghai_now
from backend.app.models import Note, NoteGoods, PlatformAccount
from backend.app.models.shop import ShopNoteGoods
from backend.app.models.shop_product import ShopProduct

#: 一次按需补齐最多带多少个 note_id（一页笔记 10–50 条；实测 50 个仍精确返回）。
ON_DEMAND_NOTE_LIMIT = 50

#: 解析关系时 `IN (...)` 查询的分块大小（SQLite 变量数上限 32766，留足余量；
#: 自愈解析一次要过几千个 item_id，不分块会撞上限）。
_RESOLVE_CHUNK = 400


def _text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def store_entry(
    db: Session,
    user_id: int,
    account_id: int,
    existing: dict[str, ShopNoteGoods],
    entry: dict[str, Any],
) -> bool:
    """把一条千帆商品笔记条目 upsert 进本地映射（`note_id` 为键）。

    `existing` 是「本账号已存在的行」字典，由调用方一次性查好传入 —— 翻页同步时
    避免每行一次查询。
    """
    note_id = _text(entry.get("note_id"))
    if not note_id:
        return False
    goods_infos = entry.get("goods_infos") if isinstance(entry.get("goods_infos"), list) else []
    primary = goods_infos[0] if goods_infos and isinstance(goods_infos[0], dict) else {}
    row = existing.get(note_id)
    if row is None:
        row = ShopNoteGoods(user_id=user_id, platform_account_id=account_id, note_id=note_id)
        db.add(row)
        existing[note_id] = row
    row.item_id = _text(entry.get("item_id"), primary.get("item_id"))
    row.item_title = _text(entry.get("item_title"), primary.get("item_title"))
    row.item_image = _text(entry.get("item_image"), primary.get("item_image"))
    row.variant_desc = _text(entry.get("variant_desc"), primary.get("variant_desc"))
    row.goods_json = json.dumps(goods_infos, ensure_ascii=False)
    row.entry_json = json.dumps(entry, ensure_ascii=False)
    row.synced_at = shanghai_now()
    return True


def existing_rows(db: Session, account_id: int) -> dict[str, ShopNoteGoods]:
    """本账号已有的映射行（`note_id` → 行）。"""
    return {
        row.note_id: row
        for row in db.scalars(
            select(ShopNoteGoods).where(ShopNoteGoods.platform_account_id == account_id)
        ).all()
    }


def missing_note_ids(rows: dict[str, ShopNoteGoods], note_ids: Iterable[str]) -> list[str]:
    """哪些笔记还没有映射（去重、保序、**有界**）。"""
    seen: set[str] = set()
    missing: list[str] = []
    for note_id in note_ids:
        text = _text(note_id)
        if not text or text in seen or text in rows:
            continue
        seen.add(text)
        missing.append(text)
        if len(missing) >= ON_DEMAND_NOTE_LIMIT:
            break
    return missing


def sync_note_ids(
    db: Session,
    account: PlatformAccount,
    note_ids: list[str],
    adapter_factory: Any,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """**按需补齐**：一次只读请求取回这批 `note_id` 的挂载商品并落库。

    返回 `{"requested", "stored", "error", "credential_invalid"}`；**不抛异常**。
    `note_ids` 为空 → 直接返回（**零请求**，调用方据此保证「不需要就不打平台」）。

    千帆凭据只认**本账号自己的 `ark` 通道**（`qianfan_cookies_for_account`）：
    没有令牌时返回 `credential_invalid=True` 并如实写明原因，**不借任何别的凭据**。
    """
    ids = [item for item in dict.fromkeys(note_ids) if item][:ON_DEMAND_NOTE_LIMIT]
    if not ids or adapter_factory is None:
        return {"requested": 0, "stored": 0, "error": "", "credential_invalid": False}

    from backend.app.services.shop_binding_service import (
        qianfan_cookies_for_account,
        set_shop_state,
    )

    try:
        cookies = qianfan_cookies_for_account(db, account.id)
        if not cookies:
            set_shop_state(
                account, status="invalid", status_message="该账号还没有千帆（ark）令牌"
            )
            db.commit()
            return {
                "requested": len(ids),
                "stored": 0,
                "error": "该账号尚未导入千帆（ark）令牌",
                "credential_invalid": True,
            }
        adapter = adapter_factory(cookies)
        success, message, payload = adapter.get_goods_note_page(
            page_num=1, page_size=len(ids), note_ids=ids
        )
    except Exception as exc:  # noqa: BLE001 - 补齐失败不影响页面渲染
        return {"requested": len(ids), "stored": 0, "error": f"{type(exc).__name__}: {exc}",
                "credential_invalid": False}

    if not success:
        # 凭据失效要留下痕迹（页面据此显示「重新登录千帆」），其余失败静默。
        # 判定读 **payload 的 code**（平台原文里没有"-13000"字样，只有中文提示）。
        code = payload.get("code") if isinstance(payload, dict) else None
        credential_invalid = code == -13000 or "-13000" in str(message)
        if credential_invalid:
            set_shop_state(account, status="invalid", status_message=str(message)[:200])
            db.commit()
        return {"requested": len(ids), "stored": 0, "error": str(message),
                "credential_invalid": credential_invalid}

    data = payload.get("data") if isinstance(payload, dict) else None
    entries = [item for item in ((data or {}).get("note_infos") or []) if isinstance(item, dict)]
    rows = existing_rows(db, account.id)
    stored = 0
    for entry in entries:
        if store_entry(db, account.user_id, account.id, rows, entry):
            stored += 1
    # 注意：**不**用 filtered 响应的 total 覆盖 goods_note_total（那是过滤后条数）。
    set_shop_state(account, goods_synced_at=(now or shanghai_now()).isoformat())
    db.commit()
    return {"requested": len(ids), "stored": stored, "error": "", "credential_invalid": False}


# --------------------------------------------------------------------------- #
# 笔记 ↔ 商品关系（设计 13 §26）：本地实体解析，零平台请求
# --------------------------------------------------------------------------- #
def cached_goods_mappings(
    db: Session, account_id: int, note_ids: Optional[Iterable[str]] = None
) -> list[tuple[str, list[dict[str, Any]]]]:
    """从**平台证据缓存**读「平台 note_id → 挂载商品条目」（`shop_note_goods.goods_json`）。

    `note_ids=None` ⇒ 该账号的**全部**缓存行（商品同步完的自愈解析用）；
    传了 ⇒ 只读这些笔记（笔记同步每页用，≤50 个）。
    """
    statement = select(ShopNoteGoods).where(ShopNoteGoods.platform_account_id == account_id)
    if note_ids is not None:
        wanted = [_text(item) for item in note_ids]
        wanted = [item for item in dict.fromkeys(wanted) if item]
        if not wanted:
            return []
        statement = statement.where(ShopNoteGoods.note_id.in_(wanted))
    mappings: list[tuple[str, list[dict[str, Any]]]] = []
    for row in db.scalars(statement).all():
        try:
            goods = json.loads(row.goods_json or "[]")
        except (TypeError, json.JSONDecodeError):
            goods = []
        if not isinstance(goods, list):
            goods = []
        mappings.append((row.note_id, [g for g in goods if isinstance(g, dict)]))
    return mappings


def _item_id_of(goods: dict[str, Any]) -> str:
    """挂载条目 → 平台商品 ID。只认 item 级键（`item_id` / `goods_id`）——
    不认 `id`：那在部分响应里是 SKU 级 id，拿来对商品会解析错行。"""
    return _text(goods.get("item_id"), goods.get("goods_id"))


def link_note_goods(
    db: Session,
    account: PlatformAccount,
    mappings: list[tuple[str, list[dict[str, Any]]]],
) -> dict[str, int]:
    """把「平台 note_id → 挂载商品」解析成**本地实体关系**，upsert 进 `note_goods`（§26）。

    三个解析步，全部本地：

    1. 本地笔记行：`notes` 按 `(user_id, 平台 note_id)` 找（同步链路刚落库的就是这批行）；
    2. 本地商品行：`shop_products` 按 `(shop_id_fk, item_id)` 找（`shop_id_fk` 取账号的店）；
    3. 既有关系：按 `note_id` 查一次，去重后只插**缺失**的行（唯一键兜底幂等）。

    **两边都解析成功才建关系**；任一侧缺失 ⇒ 计入 `skipped` 并跳过（不造假）。
    **只增不删** —— 平台撤了挂载也不删关系行（§23：同步链路不做删除维度）。

    返回 `{"linked": 新建关系行数, "skipped": 任一侧缺失而跳过的 (笔记, 商品) 对数}`。
    """
    from backend.app.services.shop_binding_service import account_shop_id

    # 收集 (平台 note_id, item_id) 对（去重、保序）
    pairs: list[tuple[str, str]] = []
    for note_id, goods_list in mappings:
        platform_note_id = _text(note_id)
        if not platform_note_id:
            continue
        for goods in goods_list or []:
            if not isinstance(goods, dict):
                continue
            item_id = _item_id_of(goods)
            if item_id:
                pairs.append((platform_note_id, item_id))
    distinct_pairs = set(pairs)
    if not distinct_pairs:
        return {"linked": 0, "skipped": 0}

    shop_pk = account_shop_id(db, account.id)
    if shop_pk is None:
        # 账号没有店铺 ⇒ 商品行无从解析（shop_products 按 shop_id_fk 归属）。
        return {"linked": 0, "skipped": len(distinct_pairs)}

    note_ids = list(dict.fromkeys(nid for nid, _ in distinct_pairs))
    item_ids = list(dict.fromkeys(iid for _, iid in distinct_pairs))

    note_pk: dict[str, int] = {}
    for start in range(0, len(note_ids), _RESOLVE_CHUNK):
        chunk = note_ids[start : start + _RESOLVE_CHUNK]
        rows = db.execute(
            select(Note.id, Note.note_id).where(
                Note.user_id == account.user_id, Note.note_id.in_(chunk)
            )
        ).all()
        for pk, platform_note_id in rows:
            note_pk[platform_note_id] = pk

    product_pk: dict[str, int] = {}
    for start in range(0, len(item_ids), _RESOLVE_CHUNK):
        chunk = item_ids[start : start + _RESOLVE_CHUNK]
        rows = db.execute(
            select(ShopProduct.id, ShopProduct.item_id).where(
                ShopProduct.shop_id_fk == shop_pk, ShopProduct.item_id.in_(chunk)
            )
        ).all()
        for pk, item_id in rows:
            product_pk[item_id] = pk

    wanted = {
        (note_pk[nid], product_pk[iid])
        for nid, iid in distinct_pairs
        if nid in note_pk and iid in product_pk
    }
    skipped = len(distinct_pairs) - len(wanted)
    if not wanted:
        return {"linked": 0, "skipped": skipped}

    existing: set[tuple[int, int]] = set()
    note_pks = list(dict.fromkeys(pk for pk, _ in wanted))
    for start in range(0, len(note_pks), _RESOLVE_CHUNK):
        chunk = note_pks[start : start + _RESOLVE_CHUNK]
        rows = db.execute(
            select(NoteGoods.note_id, NoteGoods.product_id).where(NoteGoods.note_id.in_(chunk))
        ).all()
        existing.update((note_id, product_id) for note_id, product_id in rows)

    fresh = wanted - existing
    for note_pk_, product_pk_ in sorted(fresh):
        db.add(NoteGoods(note_id=note_pk_, product_id=product_pk_))
    db.commit()
    return {"linked": len(fresh), "skipped": skipped}
