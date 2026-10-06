"""商品上架 / 下架 / 删除的**状态门禁**（2026-10-05 需求 7/8 的判据层）。

## 这个模块存在的唯一理由

千帆**商品页面自己**对每个商品算「这个动作能不能做」，不能做时给一句中文原因。
把它抄成后端一份、前端再抄一份 = 同一件事两个实现（DEF-044），平台一改口径
两边就分叉，而且**前端那份永远比后端晚知道**。

⇒ 判据**只在这里实现**，前端拿后端下发的 `allowed_actions[]` 渲染按钮
（`support_reason()` 的唯一消费者是 `qianfan.py` 的商品列表端点与
`shop_item_operate_service` 的执行循环）。

## 判据来源（**不是拍脑袋，是从千帆 bundle 逆出来的**）

2026-10-05 从 `ark-app-root` / `itemark` 的 webpack 产物里逆出三个校验函数，
原文与结构逐条对照（`832.d09dac0d.js`）：

```js
// 上架（available=true）
function R(e){ … 商品存在违规，不可上架 / {lockDesc}，不可上架 … }
// 下架（available=false）
function V(e){ … 商品正在参加定金预售活动，定金期结束后可以下架 / {lockDesc}，不可下架 … }
// 删除
function q(e){ … 商品正在参加定金预售活动，定金期结束后可以删除 / {lockDesc}，不可删除 … }
// 三者共用的两条前置
function M(e){ 店铺被冻结… / 商品被禁售，不允许编辑，请先解除禁售状态 }
// lock 解析：按 lockField + lockDimension(ITEM/SKU) + lockSkuIds 匹配
function O(e,t,n){ … lockField === t && lockDimension === "ITEM" … }
```

同时用真机 DOM 核对了「不同状态 tab 下页面给哪些行操作」：

| tab | 行操作 |
|---|---|
| 售卖中 | 编辑 / **下架** / 更多 |
| 已下架 | 编辑 / **上架** / 更多 |
| 已冻结 | （无上下架） |
| 审核中 / 审核驳回 / 审核通过 / 整改 | （无上下架） |

⇒ 「上架」只在**已下架**上有意义、「下架」只在**售卖中**上有意义，其余状态
**记录但不执行**（需求 7 的原话）。

⚠️ **本地判定与平台判定的边界**：本模块给出的是**本地台账能算出来的**判据
（状态 / 标志位 / lock 列表都在 `shop_products.raw_json` 里，零平台请求）。
平台侧仍可能因「活动/锁」等我们看不到的实时原因拒绝 —— 那种情况由**写接口的
逐条回执**（`failData[].reasons[].message`）如实报出，**不在这里猜**。
"""

from __future__ import annotations

from typing import Any, Optional

#: 三个动作的键（**唯一取值来源**；前端按钮、API 参数、任务 payload 都用它）。
ACTION_ON_SHELF = "on_shelf"
ACTION_OFF_SHELF = "off_shelf"
ACTION_DELETE = "delete"

#: 动作 → 中文名（提示文案的唯一来源，前后端不各写一份）。
ACTION_LABELS: dict[str, str] = {
    ACTION_ON_SHELF: "上架",
    ACTION_OFF_SHELF: "下架",
    ACTION_DELETE: "删除",
}

#: 允许的动作全集（顺序 = 前端按钮展示顺序）。
ALL_ACTIONS: tuple[str, ...] = (ACTION_ON_SHELF, ACTION_OFF_SHELF, ACTION_DELETE)

#: 平台 `item_lock_infos[].lockField` → 我们关心的三个动作。
#: ⚠️ 只认识这三个值（真机 bundle 里出现的全部）；别的 lockField（价格 / 库存等）
#: **不影响**本模块的三个动作 —— 多认会让「能下的被误判成不能下」。
_LOCK_FIELD_BY_ACTION: dict[str, str] = {
    ACTION_ON_SHELF: "ON_SHELF",
    ACTION_OFF_SHELF: "OFF_SHELF",
    ACTION_DELETE: "DELETE_SKU",
}


def _lock_of(item: dict[str, Any], action: str) -> Optional[dict[str, Any]]:
    """在 `item_lock_infos` 里找该动作的锁（ITEM 级优先，退化到 SKU 级）。

    与 bundle 里的 `O()` 同构：**ITEM 维度的锁对整件商品生效**（优先），
    SKU 维度的锁只在指定 `lockSkuIds` 上生效 —— 本模块做的是**商品级**动作，
    所以把「任一 SKU 被锁」也算进原因（宁可如实说不能做，也不要发了才知道）。
    """
    locks = item.get("item_lock_infos")
    if not isinstance(locks, list):
        return None
    field = _LOCK_FIELD_BY_ACTION.get(action)
    if not field:
        return None
    rows = [row for row in locks if isinstance(row, dict) and row.get("lockField") == field]
    if not rows:
        return None
    for row in rows:
        if str(row.get("lockDimension") or "").upper() == "ITEM":
            return row
    return rows[0]


def _common_reasons(item: dict[str, Any]) -> list[str]:
    """两个动作都适用的前置（店铺冻结 / 商品禁售）。

    ⚠️ 平台原文照抄（「您的店铺被冻结…」/「商品被禁售…」）—— 文案是**平台给的
    结论**，改写成自己的说法会让用户拿着我们的话去问平台客服而人家不认识。
    """
    reasons: list[str] = []
    if item.get("seller_freeze"):
        reasons.append("您的店铺被冻结，解冻后可发布、编辑商品")
    if item.get("item_freeze"):
        reasons.append("商品被禁售，不允许编辑，请先解除禁售状态")
    return reasons


def support_reason(action: str, item: dict[str, Any], *, status_key: str = "") -> Optional[str]:
    """这个自能否对该商品执行 —— 返回 `None` = 支持，非空 = 中文原因。

    `item` 是**列表项契约**（`shop_products_service.normalize_item_from_raw` 的产物
    + 台账列），所以判据全部落在已经下发给前端的字段上，前端与后端看的是同一份事实。

    `status_key` = 台账的状态筛选键（`on_shelf` / `off_shelf` / `frozen` / …）。
    不传时由 `item["card_type"]` 反推（同一套映射，见 `QIANFAN_ITEM_STATUSES`）。
    """
    if action not in ALL_ACTIONS:
        return f"不支持的商品操作：{action}"

    reasons = _common_reasons(item)
    # 店铺冻结时三个动作都不可做（平台原文也是拦在通用前置里）。
    if reasons:
        return reasons[0]

    key = status_key or _status_key_of(item)
    lock = _lock_of(item, action)
    lock_desc = str((lock or {}).get("lockDesc") or "").strip()

    if action == ACTION_ON_SHELF:
        # ⚠️ 只有「已下架」能上架（真机 tab 口径）。
        if key != "off_shelf":
            return f"当前状态「{_status_label(item)}」不支持上架（只有已下架的商品可以上架）"
        if _has_violation(item):
            return "商品存在违规，不可上架"
        if lock_desc:
            return f"{lock_desc}，不可上架"
        return None

    if action == ACTION_OFF_SHELF:
        if key != "on_shelf":
            return f"当前状态「{_status_label(item)}」不支持下架（只有在售中的商品可以下架）"
        if item.get("is_item_attend_promotion"):
            return "商品正在参加定金预售活动，定金期结束后可以下架"
        if lock_desc:
            return f"{lock_desc}，不可下架"
        return None

    # ACTION_DELETE
    # ⚠️ 删除**不限状态**（页面在「已下架」「已冻结」「全部」tab 都给删除入口），
    # 只受冻结 / 活动 / 锁三条限制 —— 这点与上下架不同，别顺手套上状态闸。
    if item.get("is_item_attend_promotion"):
        return "商品正在参加定金预售活动，定金期结束后可以删除"
    if lock_desc:
        return f"{lock_desc}，不可删除"
    return None


def allowed_actions(item: dict[str, Any], *, status_key: str = "") -> list[dict[str, Any]]:
    """该商品**当前可做的动作清单**（列表项下发用，`allowed_actions[]`）。

    形如：`[{"action": "off_shelf", "label": "下架", "supported": true, "reason": ""}, …]`

    ⚠️ **不可做的也下发**（`supported: false` + `reason`），不是只下发可做的 ——
    只给可做的，前端就没法回答「为什么这个商品没有上架按钮」（用户会当成 bug）。
    """
    key = status_key or _status_key_of(item)
    out: list[dict[str, Any]] = []
    for action in ALL_ACTIONS:
        reason = support_reason(action, item, status_key=key)
        out.append(
            {
                "action": action,
                "label": ACTION_LABELS[action],
                "supported": reason is None,
                "reason": reason or "",
            }
        )
    return out


def supported_actions(item: dict[str, Any], *, status_key: str = "") -> list[str]:
    """可做动作的键（执行侧用；`allowed_actions` 的轻量版）。"""
    return [row["action"] for row in allowed_actions(item, status_key=status_key) if row["supported"]]


# --------------------------------------------------------------------------- #
# 内部：状态口径
# --------------------------------------------------------------------------- #

#: 台账 `card_type` → 状态键（与 `QIANFAN_ITEM_STATUSES` 反向）。
#: ⚠️ 反向映射只在**这里**建，不散落到调用方。
_STATUS_KEY_BY_CARD_TYPE: dict[int, str] = {
    2: "on_shelf",
    3: "off_shelf",
    4: "frozen",
    5: "auditing",
    6: "audit_rejected",
    7: "audit_passed",
    10: "rectify",
}


def _status_key_of(item: dict[str, Any]) -> str:
    """台账条目 → 状态筛选键。

    ⚠️ 优先用条目里已经算好的 `status_key`（读取层有权威实现）；没有才按
    `card_type` 反查。两条路都取不到 ⇒ 空串（**未知**）—— 此时上下架都会被判
    「不支持」，那是**保守但正确**的（缺值口径⑤：不知道就别动交易侧的东西）。
    """
    key = str(item.get("status_key") or "").strip()
    if key:
        return key
    try:
        card_type = int(item.get("card_type"))
    except (TypeError, ValueError):
        return ""
    return _STATUS_KEY_BY_CARD_TYPE.get(card_type, "")


def _status_label(item: dict[str, Any]) -> str:
    """状态中文名（提示文案用）。取不到给平台原始口径的兜底说法。"""
    label = str(item.get("status_label") or "").strip()
    if label:
        return label
    key = _status_key_of(item)
    from apis.xhs_qianfan_apis import QIANFAN_ITEM_STATUS_LABELS

    return QIANFAN_ITEM_STATUS_LABELS.get(key, "未知")


def _has_violation(item: dict[str, Any]) -> bool:
    """是否有违规（上架的硬拦）。

    bundle 里对应 `(0,_.Dp)(e) || (0,_.zo)(e) || (0,_.JB)(e)` 三个谓词的或 ——
    它们读的是条目里的审核 / 处罚结果字段。本地台账口径取其中**可判**的两个：

    * `item_punish_result`（处罚）：非 `1` = 有处罚（真机 1 = 正常）；
    * `item_audit_status`：`2` = 审核通过（真机值域里 2 是正常态）。

    ⚠️ **取不到 ⇒ 不判违规**（`False`）：缺值不编造（硬约束⑤）—— 把「不知道」
    当成「违规」会让本来能上架的卖不出去，那是更贵的错。
    """
    punish = item.get("item_punish_result")
    if punish is not None:
        try:
            if int(punish) not in (1, 0):
                return True
        except (TypeError, ValueError):
            pass
    audit = item.get("audit_status", item.get("item_audit_status"))
    if audit is not None:
        try:
            #: 真机「审核通过」= 2；其余（驳回 / 审核中等）在**上架**这一步都算异常。
            if int(audit) not in (2,):
                return True
        except (TypeError, ValueError):
            pass
    return False


__all__ = [
    "ALL_ACTIONS",
    "ACTION_DELETE",
    "ACTION_LABELS",
    "ACTION_OFF_SHELF",
    "ACTION_ON_SHELF",
    "allowed_actions",
    "support_reason",
    "supported_actions",
]
