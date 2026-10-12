"""同步链路内置的数据对账：**标记 + 自愈**（设计 13 §27，2026-09-22 用户口径：
「数据对账功能融合到同步笔记和同步商品中，每次根据时间区间和全量同步时，对本次
指定得区间进行数据对账保证数据得一致性，如果数据不一致，则将三方不存在得数据再
本地数据行将数据得同步状态标记为三方不存在数据状态」）。

## 为什么融合（而不是保留独立入口）

早前对账是**独立入口**（§24.4：两个端点 + 前端「对账」按钮 + 报告/修正两段式）。
它的全部输入——平台在同步范围内有哪些 id——同步链路**本轮刚刚枚举过**。独立入口
为此把整个平台列表**再翻一遍**：双倍平台请求、双倍时长，结论还与同步不同步。

⇒ 融合后：同步链路把自己的枚举结果交给本模块的**标记通道**，**零额外平台请求**，
「拉取」与「对账」在同一个任务行里完成。

## 本模块只做「标记 + 自愈」（纯本地，零平台请求）

| 差异 | 处置 | 谁做 |
|---|---|---|
| 平台有、本地无（范围内） | **补落库** | 同步链路自己（`_save_normalized_notes` / `store_products`） |
| 两边都有、字段不一致 | **按平台改正** | 同步链路自己（落库即覆盖） |
| 本地有、平台无（范围内） | **不删行，打标记** | **本模块**（`platform_missing_at`） |

标记写**既有列** `platform_missing_at`（可空时间列，迁移 `p8q9r0s1t2u3`）：
`NULL` = 正常；非空 = 最近一次发现「本地有、平台无」的时间。**自愈**：平台又有了
⇒ 同步链路看到它 ⇒ 清回 `NULL`。

⚠️ 复用 `platform_missing_at` 而**不新增状态列**：用户要的「三方不存在数据状态」
与 §24.4 拍板的「本地多出的数据不直接删除，打上状态」是同一件事 —— 再造一列就是
让同一个事实有两个真相源。

## 匹配键 = 数据的唯一 ID（用户 2026-09-22 追加口径）

* **笔记**按平台 **`note_id`** 比对（本地行 `notes.note_id` ⇄ 平台条目的 `note_id`/`id`）；
* **商品**按平台 **`item_id`** 比对（本地行 `shop_products.item_id` ⇄ 平台条目的 `item_id`）。

**只有唯一 ID 参与匹配**：标题 / 作者 / 价格 / 状态**会变**（改个标题就不匹配 =
把「改过标题的商品」误判成「平台没有」），发布时间只用于**范围判定**（这行在不在
本轮对账的区间里），不用于匹配。拿会变的字段当匹配键，结论必然漂移。

## 「枚举完整」是判据前提，不是可选安全网

**调用方负责判「本轮枚举是否完整」，不完整就根本不该调本模块**（限流等待超时 /
绝对页数兜底 / 显式 `max_pages` / 后续页取数失败 ⇒ 没翻完）。样本不完整时
「平台没有」这个结论本身就不成立（DEF-057 同族：用不完整的信息下确定性结论，
比没有结论更糟）。这不是「安全网时开时关」（DEF-048 的教训），而是**判据成立的条件**。

笔记侧另有**源类型判据**：只有「能枚举全量笔记的源」（`creator` / `pc`）完整翻完
才谈得上「平台没有它」—— `ark` 源只看得到商品笔记（子集），凭它下结论会把普通
笔记全标错。判据在 `note_sync_service._sync_with_task` 里做，不在本模块猜。

## 范围 = 同步范围（不另写一份解析）

``time_from_ms`` / ``time_to_ms`` 由调用方按同步的 `scope` / 起止日 / 增量游标
算好传进来：`full` ⇒ 全量；`range` ⇒ 用户选的时间区间；`incremental` ⇒ 游标以后。
取不到发布时间的本地行**不参与判定**（不造假），计入返回值的 `unknown_time`。

> 📌 **被取代（原句保留）**：本模块原有 `reconcile_notes()` / `reconcile_goods()`
> 两个**自己翻平台**的函数与 `apply_fixes` 两段式（§24.4 的独立对账入口）。
> 2026-09-22 用户口径「数据对账功能融合到同步笔记和同步商品中」后整体移除 ——
> 对应端点 `POST /api/accounts/{id}/notes/reconcile`、`POST /api/xhs/qianfan/reconcile-shop-goods`
> 与前端「对账」按钮一并拆除，对账改由同步链路调用下面的标记通道完成。
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.time import shanghai_now
from backend.app.models.note import Note
from backend.app.models.platform_account import PlatformAccount
from backend.app.services import note_sync_service as nss

#: 本地行的时间键（`raw_json` 存的是**平台原始条目**，不是归一化后的 dict）：
#: creator 源是 `time`（"YYYY-MM-DD HH:MM" 字符串）、ark 源是 `publish_time`（毫秒整数）。
_LOCAL_NOTE_TIME_KEYS = ("time", "create_time", "publish_time", "timestamp")


def _local_note_time_ms(row: Note) -> int:
    """本地笔记的发布时间（毫秒）；取不到返回 0（= 时间未知）。

    ⚠️ 读的是 `raw_json` 里的**平台原始字段**：`notes` 表没有发布时间列，
    而 `_raw_with_metrics` 把原始条目原样摊平存了进来（creator 的 `time`、
    ark 的 `publish_time` 都在里面）。PC 源的 `raw_json` 是整条响应，时间埋在
    `data.items[0].note_card` 里 —— 那种取不到，返回 0 由调用方按「时间未知」处理。
    """
    raw = row.raw_json if isinstance(row.raw_json, dict) else {}
    for key in _LOCAL_NOTE_TIME_KEYS:
        value = raw.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            stamp = int(value)
            return stamp * 1000 if stamp < 10_000_000_000 else stamp
        text = str(value).strip()
        if not text:
            continue
        if text.isdigit():
            stamp = int(text)
            return stamp * 1000 if stamp < 10_000_000_000 else stamp
        parsed = nss._parse_note_time_text(text)
        if parsed is not None:
            return parsed
    return 0


def _local_goods_time_ms(row: Any) -> int:
    """本地商品的 `create_time`（毫秒）—— 同样从 `raw_json` 的原始条目里取。"""
    import json

    try:
        raw = json.loads(row.raw_json) if isinstance(row.raw_json, str) else (row.raw_json or {})
    except Exception:  # noqa: BLE001 - 坏 JSON 按「时间未知」处理，不猜
        return 0
    if not isinstance(raw, dict):
        return 0
    value = raw.get("create_time")
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    return int(text) if text.isdigit() else 0


def _in_scope(stamp_ms: int, time_from_ms: int, time_to_ms: int) -> bool:
    """发布时间是否落在对账范围内（`stamp_ms <= 0` = 时间未知 ⇒ 调用方另行处理）。"""
    if time_from_ms and stamp_ms <= time_from_ms:
        return False
    if time_to_ms and stamp_ms > time_to_ms:
        return False
    return True


def mark_notes_missing(
    db: Session,
    account: PlatformAccount,
    platform_ids: Iterable[str],
    *,
    time_from_ms: int = 0,
    time_to_ms: int = 0,
) -> dict[str, Any]:
    """同步收尾的**笔记对账**：标记「本地有、平台无」的行（§27）。纯本地、零平台请求。

    **匹配键 = 平台唯一 ID `note_id`**（用户 2026-09-22 口径）：本地行
    `notes.note_id` 在不在 ``platform_ids`` 里，只认这一把尺子 —— 标题 / 作者会变，
    不参与匹配；发布时间只用于**范围判定**（这行在不在本轮对账区间内）。

    ``platform_ids`` = 同步链路**本轮枚举到**的平台 note_id 集合（调用方已判过
    「枚举完整」，见模块 docstring 的判据前提）。范围内本地行不在其中 ⇒
    `platform_missing_at = now`（已标过的保留首次时间，不刷新）；在其中 ⇒
    标记清回 `NULL`（自愈）。

    返回 `{"marked", "healed", "unknown_time"}`。
    """
    platform = {str(item).strip() for item in platform_ids if str(item).strip()}
    now = shanghai_now()
    marked = 0
    healed = 0
    unknown_time = 0
    for row in db.scalars(
        select(Note).where(Note.platform_account_id == account.id, Note.is_deleted.is_(False))
    ).all():
        stamp = _local_note_time_ms(row)
        if stamp <= 0:
            unknown_time += 1
            continue
        if not _in_scope(stamp, time_from_ms, time_to_ms):
            continue
        if str(row.note_id) in platform:
            # 自愈：平台又有了 ⇒ 清标记（§24.4 同语义）。
            if row.platform_missing_at is not None:
                row.platform_missing_at = None
                healed += 1
        elif row.platform_missing_at is None:
            # 本地有、平台无（范围内）⇒ 打「三方不存在」标记，**不删行**。
            row.platform_missing_at = now
            marked += 1
    db.commit()
    return {"marked": marked, "healed": healed, "unknown_time": unknown_time}


def mark_goods_missing(
    db: Session,
    account_id: int,
    platform_item_ids: Iterable[str],
    *,
    time_from_ms: int = 0,
    time_to_ms: int = 0,
) -> dict[str, Any]:
    """同步收尾的**商品对账**：标记「本地有、平台无」的商品行（§27）。与笔记侧同构。

    **匹配键 = 平台唯一 ID `item_id`**（用户 2026-09-22 口径）：本地行
    `shop_products.item_id` 在不在 ``platform_item_ids`` 里，只认这一把尺子 ——
    标题 / 价格 / 状态会变，不参与匹配；`create_time` 只用于**范围判定**。

    ``account_id`` = 账号主键（`shop_products.platform_account_id`，ADR-021：
    归属键即隔离键，商品直接挂账号）。
    完整**全量**商品同步本来就会删「本次未出现」的行（§19.4：完整才删）⇒ 标记
    通道主要作用于**区间 / 增量**同步（范围外不删、范围内多出的行打标记）。
    """
    from backend.app.models.shop_product import ShopProduct

    platform = {str(item).strip() for item in platform_item_ids if str(item).strip()}
    now = shanghai_now()
    marked = 0
    healed = 0
    unknown_time = 0
    for row in db.scalars(select(ShopProduct).where(ShopProduct.platform_account_id == account_id)).all():
        stamp = _local_goods_time_ms(row)
        if stamp <= 0:
            unknown_time += 1
            continue
        if not _in_scope(stamp, time_from_ms, time_to_ms):
            continue
        if str(row.item_id) in platform:
            if row.platform_missing_at is not None:
                row.platform_missing_at = None
                healed += 1
        elif row.platform_missing_at is None:
            row.platform_missing_at = now
            marked += 1
    db.commit()
    return {"marked": marked, "healed": healed, "unknown_time": unknown_time}
