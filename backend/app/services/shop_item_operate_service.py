"""商品批量上架 / 下架 / 删除的**执行器**（2026-10-05 需求 7）。

## 形态：手动触发式任务，逐条循环 + 逐条记账

用户口径（需求 7 原文）：

> 统一使用创建手动触发式任务，**任务中心的任务列表不展示**，参考清理无流量笔记
> 实现方式，对应任务中对每个商品操作上架，下架，删除，**循环操作所有商品**，
> 注意要点：你需要调研不同商品状态可能对应不同操作功能，定时任务中需要**根据任务
> 状态，确定该商品是否支持执行该操作，不支持的只记录不执行操作**。

⇒ 与 `no_traffic_cleanup_service` **逐条对齐**的骨架：

* 一行 `tasks`（`task_type = xhs_shop_item_operate`）代表一次批量操作，
  `payload.actions` 逐条结果落库（不做第二个「日志表」—— 这条链路本来就有任务行）；
* **任务中心的任务列表不展示**：本任务**不**注册进 `job_registry`
  （「任务列表」读的是 `scheduled_jobs` 注册表），也不写 `scheduled_job_logs`
  的队列清单 —— 它是**一次性手动动作**，不是可调度的定时任务；
  进度与结果从 `GET /items/operate/{task_id}` 拿（前端弹窗轮询）；
* **逐条过门禁**（`shop_item_operate_gates.support_reason`）：不支持 ⇒ **只记录不执行**
  （`skipped` + 中文原因），发出真实请求的**永不**是不可做的那条；
* **限流**：复用 `CrawlRateLimiter`（按账号），超限**停本次**并如实记
  「触发限流，剩余未执行」—— 不硬闯（平台写操作密集触发会撞风控）。

## 为什么「按商品分组调用」而不是「一条命令带上全部 id」

平台端点**本身支持** `itemIds: [...]` 批量（真机抓包就是单元素数组，但字段是复数）。
仍然逐条 / 小批调用的理由：

* **可归因**：批量调用的失败回执是 `failData[]`，而**成功与静默忽略**分不开 ——
  一个商品既不在 `failData` 里、又没真的改，我们无法分辨。逐条调用时
  「这个请求对应这个商品」是确定的，记账才有意义（需求 7 要的正是逐条状态）；
* **可控速率**：逐条之间的限流等待是**刹车点**（C5 的精神：没有刹车点的批量不可控）。

⚠️ 真实调用**按动作分类批量**（同动作的商品聚成一次请求，单商品一次 = 最坏情况），
原因是平台对同类动作有幂等/去重语义，逐条发几十个单元素数组请求反而是更大的风控面。
**账仍然逐条记**：响应里的 `failData[]` 按 `itemId` 映射回逐条结果，
没有出现在 `failData` 里的商品 = 成功。
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional

from sqlalchemy import select

from backend.app.core.database import SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models.platform_account import PlatformAccount
from backend.app.models.shop_product import ShopProduct
from backend.app.models.task import Task
from backend.app.services import shop_item_operate_gates as gates
from backend.app.core.rate_limiter import get_rate_limiter
from backend.app.services.shop_products_service import load_product_dict_from_ledger

logger = logging.getLogger(__name__)

#: 本任务在 `tasks` 表的类型（**不进 `scheduled_jobs` 注册表** ⇒ 任务中心任务列表不展示）。
OPERATE_TASK_TYPE = "xhs_shop_item_operate"

#: 一次任务最多处理多少件（**有界**：写操作，用户勾选上限也是这个数）。
MAX_ITEMS_PER_TASK = 200

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

#: 逐条结果的三种结局（与 `no_traffic_cleanup_service` 的 details 口径同构）。
RESULT_OK = "ok"
RESULT_SKIPPED = "skipped"
RESULT_FAILED = "failed"


def _api_factory(cookies: str) -> Any:
    """构造千帆 SDK 客户端。

    ⚠️ 单独一个**模块级函数**（而不是在 `_apply_action` 里直接 `XHS_Qianfan_Apis(…)`）
    是为了让测试能 patch —— 写操作的测试必须能证明「平台有没有被调用」，
    内联构造会把这条观测面焊死（本文件的多条用例都靠 `FakeOperateApi.calls`）。
    """
    from apis.xhs_qianfan_apis import XHS_Qianfan_Apis

    return XHS_Qianfan_Apis(cookies)


def _payload(task: Task) -> dict[str, Any]:
    return task.payload if isinstance(task.payload, dict) else {}


def _mutate_payload(task: Task, **fields: Any) -> None:
    """改 JSON 列**必须重新赋值整个 dict**（in-place 改 SQLAlchemy 看不到）。"""
    task.payload = {**(_payload(task)), **fields}


def start_item_operate(
    *,
    platform_account_id: int,
    user_id: int,
    action: str,
    item_ids: list[str],
) -> dict[str, Any]:
    """建任务行 + 起后台线程，**立即返回任务概要**（HTTP 不阻塞）。

    防重：该账号已有未完成的商品操作任务 ⇒ 返回**已有任务**（`reused=True`）——
    与 `start_shop_goods_sync` 同一口径（同一账号同时只跑一个写操作批，
    避免两条链路对同一批商品各发一次命令）。
    """
    action = str(action or "").strip()
    if action not in gates.ALL_ACTIONS:
        raise ValueError(f"不支持的商品操作：{action}")
    ids: list[str] = []
    seen: set[str] = set()
    for raw in item_ids or []:
        value = str(raw or "").strip()
        if value and value not in seen:
            seen.add(value)
            ids.append(value)
    if not ids:
        raise ValueError("没有选择要操作的商品。")
    if len(ids) > MAX_ITEMS_PER_TASK:
        raise ValueError(f"单次最多操作 {MAX_ITEMS_PER_TASK} 个商品，本次选了 {len(ids)} 个。")

    db = SessionLocal()
    try:
        existing = db.scalars(
            select(Task).where(
                Task.task_type == OPERATE_TASK_TYPE,
                Task.status.in_([STATUS_PENDING, STATUS_RUNNING]),
            )
        ).all()
        for row in existing:
            if int((_payload(row).get("account_id") or 0)) == int(platform_account_id):
                return {"reused": True, **_summary(row)}

        task = Task(
            user_id=user_id,
            platform="xhs",
            task_type=OPERATE_TASK_TYPE,
            status=STATUS_PENDING,
            payload={
                "account_id": int(platform_account_id),
                "action": action,
                "action_label": gates.ACTION_LABELS[action],
                "item_ids": ids,
                "total": len(ids),
                "source": "manual",
            },
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        task_id = task.id
        summary = _summary(task)
    finally:
        db.close()

    thread = threading.Thread(
        target=_run_in_thread,
        args=(task_id, int(platform_account_id), user_id, action, ids),
        name=f"shop-item-operate-{platform_account_id}",
        daemon=True,
    )
    thread.start()
    return {"reused": False, **summary}


def run_item_operate(
    task_id: int,
    *,
    platform_account_id: int,
    user_id: Optional[int],
    action: str,
    item_ids: list[str],
) -> dict[str, Any]:
    """同步跑一次商品操作（后台线程 / 测试直接调用）。**不向上抛异常。**"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if task is None:
            return {"ok": False, "error": "任务不存在"}
        task.status = STATUS_RUNNING
        if task.started_at is None:
            task.started_at = shanghai_now()
        db.commit()

        account = db.get(PlatformAccount, platform_account_id)
        if account is None:
            _finish(db, task, results=[], error="账号不存在")
            return {"ok": False, "error": "账号不存在"}

        # 台账行（判据的原料）—— **零平台请求**，全部本地。
        rows = db.scalars(
            select(ShopProduct).where(
                ShopProduct.platform_account_id == platform_account_id,
                ShopProduct.item_id.in_(item_ids),
            )
        ).all()
        by_id = {str(row.item_id or ""): row for row in rows}

        limiter = get_rate_limiter()
        results: list[dict[str, Any]] = []
        #: 真正要发命令的商品（过了门禁的那些），按 id 保序。
        executable: list[str] = []

        for item_id in item_ids:
            row = by_id.get(item_id)
            if row is None:
                results.append(
                    {
                        "item_id": item_id,
                        "title": "",
                        "result": RESULT_SKIPPED,
                        "reason": "本地台账里没有这个商品（先「同步商品」再试）",
                    }
                )
                continue
            item = load_product_dict_from_ledger(row)
            item["seller_freeze"] = bool(_seller_freeze(db, account))
            reason = gates.support_reason(action, item)
            if reason:
                # ⚠️ **不支持的只记录不执行**（需求 7 原话）——
                # 绝不「先发了再说」，写操作发出去就收不回。
                results.append(
                    {
                        "item_id": item_id,
                        "title": str(item.get("title") or ""),
                        "result": RESULT_SKIPPED,
                        "reason": reason,
                    }
                )
                continue
            executable.append(item_id)

        if executable:
            _apply_action(
                db=db,
                account=account,
                action=action,
                item_ids=executable,
                by_id=by_id,
                results=results,
                limiter=limiter,
            )

        _finish(db, task, results=results)
        return {"ok": True, **_summary(task)}
    except Exception as exc:  # noqa: BLE001 - 后台线程不许抛出去
        logger.error("shop item operate failed (task=%s): %s", task_id, exc)
        try:
            task = db.get(Task, task_id)
            if task is not None:
                _mutate_payload(task, error=str(exc))
                task.status = STATUS_FAILED
                task.finished_at = shanghai_now()
                db.commit()
        except Exception:  # noqa: BLE001
            logger.warning("shop item operate: could not mark task failed")
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def _seller_freeze(db: Any, account: PlatformAccount) -> bool:
    """店铺冻结标记（由写失败的平台原文写入，见 `_note_platform_reason`）。"""
    from backend.app.services.shop_binding_service import get_shop_state

    try:
        return bool(get_shop_state(account).get("seller_freeze"))
    except Exception:  # noqa: BLE001 - 状态读不到就当作未知（不拦）
        return False


def _apply_action(
    *,
    db: Any,
    account: PlatformAccount,
    action: str,
    item_ids: list[str],
    by_id: dict[str, ShopProduct],
    results: list[dict[str, Any]],
    limiter: Any,
) -> None:
    """对已过门禁的商品发**真实**写命令，并把回执按 `itemId` 映射回逐条结果。

    ⚠️ 限流：**一次批**先过限流器；不过 ⇒ 整批记 `skipped`（原因如实写
    「触发限流，未执行」）—— 剩下的留给用户重新点一次，不硬闯（写操作撞风控
    的代价远高于「这次没跑完」）。
    """
    from backend.app.services.shop_binding_service import (
        qianfan_cookies_for_account,
        set_shop_state,
    )

    cookies = qianfan_cookies_for_account(db, account.id)
    if not cookies:
        for item_id in item_ids:
            results.append(
                {
                    "item_id": item_id,
                    "title": str(by_id[item_id].item_title or ""),
                    "result": RESULT_FAILED,
                    "reason": "该账号缺少千帆（ark）令牌，无法执行写操作",
                }
            )
        return

    if not limiter.allow(f"{account.id}:goods"):
        for item_id in item_ids:
            results.append(
                {
                    "item_id": item_id,
                    "title": str(by_id[item_id].item_title or ""),
                    "result": RESULT_SKIPPED,
                    "reason": "触发平台调用限流，本次未执行（稍后重试）",
                }
            )
        return

    api = _api_factory(cookies)
    if action == gates.ACTION_DELETE:
        ok, message, payload = api.batch_delete_items(item_ids)
    else:
        ok, message, payload = api.batch_update_availability(
            item_ids, available=(action == gates.ACTION_ON_SHELF)
        )

    #: 平台逐条失败原因（形如 `{"itemId": "…", "reasons": [{"message": "…"}]}`）。
    fail_reasons = _fail_reasons(payload)

    if not ok:
        # 整批失败：**同一个原因**写给每一条（平台没给逐条原因，就不编逐条原因）。
        reason = message or "平台拒绝了本次操作"
        _note_platform_reason(db, account, reason)
        for item_id in item_ids:
            results.append(
                {
                    "item_id": item_id,
                    "title": str(by_id[item_id].item_title or ""),
                    "result": RESULT_FAILED,
                    "reason": reason,
                }
            )
        return

    for item_id in item_ids:
        fail = fail_reasons.get(item_id)
        if fail:
            results.append(
                {
                    "item_id": item_id,
                    "title": str(by_id[item_id].item_title or ""),
                    "result": RESULT_FAILED,
                    "reason": fail,
                }
            )
        else:
            results.append(
                {
                    "item_id": item_id,
                    "title": str(by_id[item_id].item_title or ""),
                    "result": RESULT_OK,
                    "reason": "",
                }
            )

    #: 成功执行 ⇒ 本地台账里的「上下架」信号**立即刷新**（等下一轮同步太久，
    #: 用户会以为没生效）。⚠️ 只改本地乐观值，**不改平台事实** ——
    #: 真正的对账仍在「同步商品」里（平台返回什么以平台为准）。
    _refresh_local_status(db, action=action, item_ids=item_ids, by_id=by_id)
    #: 平台回执里出现了「店铺被冻结」⇒ 记进 `shop_state`，后续按钮直接判不可做。
    task_note = ""
    if any("冻结" in (row.get("reason") or "") for row in results[-len(item_ids):]):
        task_note = "seller_freeze"
    if task_note == "seller_freeze":
        try:
            set_shop_state(account, seller_freeze=True)
            db.commit()
        except Exception:  # noqa: BLE001 - 状态写不进去不影响本次结果
            db.rollback()


def _fail_reasons(payload: Any) -> dict[str, str]:
    """平台回执 → `{item_id: 中文原因}`（**逐条**）。

    ⚠️ 三级兜底取原因键（`reasons[].message` / `reason` / `message`）——
    只认一个键会让「平台换了字段名」变成「失败但没原因」（用户看不出为什么）。
    """
    out: dict[str, str] = {}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return out
    for row in data.get("failData") or []:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("itemId") or row.get("item_id") or "").strip()
        if not item_id:
            continue
        reasons = row.get("reasons")
        text = ""
        if isinstance(reasons, list) and reasons:
            first = reasons[0]
            if isinstance(first, dict):
                text = str(first.get("message") or first.get("reason") or "")
            else:
                text = str(first)
        if not text:
            text = str(row.get("message") or row.get("reason") or "")
        out[item_id] = text or "平台未给出原因"
    return out


def _note_platform_reason(db: Any, account: PlatformAccount, reason: str) -> None:
    """把平台的**整批失败原因**里能识别的语义写进账号 `shop_state`。

    ⚠️ 只识别**平台原文里出现过的**一句（「店铺被冻结」）—— 不猜别的
    （猜错会把能用的入口锁死）。
    """
    if "冻结" not in (reason or ""):
        return
    try:
        from backend.app.services.shop_binding_service import set_shop_state

        set_shop_state(account, seller_freeze=True)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def _refresh_local_status(
    db: Any, *, action: str, item_ids: list[str], by_id: dict[str, ShopProduct]
) -> None:
    """写成功后**立即刷新本地台账的上下架信号**（乐观值，下一轮同步会对账）。

    ⚠️ **必须同时改 `raw_json`**：门禁（`allowed_actions`）读的是台账 dict 里
    由 `raw_json` 现场派生的 `status_key`（`status_key_from_raw` 的第一判据是
    `off_shelf_time` / `on_shelf_time`，都在 raw 里）。只改归一化列的话，
    用户点完下架、回到列表**按钮还是「下架」**（判据没变），下一轮同步前都这样 ——
    那是「点了没反应」的同一种观感。

    所以这里改**两处**，口径一致：
    * `raw_json` 里的 `on_shelf_time` / `off_shelf_time`（门禁与展示的真实判据）；
    * `card_type` + `status` 两个归一化列（筛选 / 排序 / 展示标签用）。

    ⚠️ 这是**乐观更新**：平台事实仍以平台为准 —— `synced_at` 不动
    （不伪装成「刚同步过」）。删除**什么都不改**（不擅自从台账删行：
    平台侧删除要等下一轮「同步商品」对账后才该消失，本地先删会让
    「同步」看起来吞了数据）。

    `card_type` 的取值来自 SDK 白名单（`QIANFAN_ITEM_STATUSES`），
    **不在这里写死数字**。
    """
    if action == gates.ACTION_DELETE:
        return
    from apis.xhs_qianfan_apis import QIANFAN_ITEM_STATUSES

    wants_on_shelf = action == gates.ACTION_ON_SHELF
    target = QIANFAN_ITEM_STATUSES["on_shelf" if wants_on_shelf else "off_shelf"]
    now_ms = int(shanghai_now().timestamp() * 1000)
    for item_id in item_ids:
        row = by_id.get(item_id)
        if row is None:
            continue
        try:
            raw = json.loads(row.raw_json) if row.raw_json else {}
        except (TypeError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        if wants_on_shelf:
            raw["on_shelf_time"] = now_ms
            raw.pop("off_shelf_time", None)
        else:
            raw["off_shelf_time"] = now_ms
        #: ⚠️ `buyable` **也要一起改**（2026-10-06 修）：它是「这件商品能否购买」的
        #: 平台原值，与 `on_shelf_time` / `off_shelf_time` 是**同一个生命周期信号的三面**
        #: （`status_key_from_raw` 的 docstring 记着实测口径「下架行 = `buyable=false`
        #: + 无上架时间 + 有下架时间」）。
        #: 漏改它 ⇒ 上架后 `status` 列与 `status_key` 都变成「售卖中」，但 `buyable`
        #: 仍是 false：前端「可售」列显示**停售**、详情弹窗显示**不可购买**，状态 tag
        #: 若按 `buyable` 上色还会是灰的（用户 2026-10-06 实测报告的两个症状）。
        #: 注意 `status_key_from_raw` **不读 `buyable`**（只读时间字段与 `item_freeze`）
        #: ⇒ 补写它**不会**影响上下架门禁。
        raw["buyable"] = wants_on_shelf
        raw["card_type"] = target
        row.raw_json = json.dumps(raw, ensure_ascii=False)
        row.card_type = target
        #: 展示标签（`status` 列）与门禁键**不同口径**（列是中文标签），
        #: 这里只改列；键由 `status_key_from_raw(raw)` 现算 —— 不要去写它。
        row.status = "售卖中" if wants_on_shelf else "已下架"
    db.commit()


def _finish(db: Any, task: Task, *, results: list[dict[str, Any]], error: str = "") -> None:
    """落终态：逐条结果 + 三个计数（成功 / 跳过 / 失败）。"""
    ok = sum(1 for row in results if row.get("result") == RESULT_OK)
    skipped = sum(1 for row in results if row.get("result") == RESULT_SKIPPED)
    failed = sum(1 for row in results if row.get("result") == RESULT_FAILED)
    task.status = STATUS_DONE if not error else STATUS_FAILED
    task.finished_at = shanghai_now()
    task.progress = 100
    _mutate_payload(
        task,
        results=results,
        succeeded=ok,
        skipped=skipped,
        failed=failed,
        executed=ok + failed,
        error=error,
    )
    db.commit()


def _summary(task: Task) -> dict[str, Any]:
    """任务行 → 前端契约（`/items/operate/{task_id}` 与启动响应共用）。"""
    payload = _payload(task)
    results = payload.get("results") or []
    return {
        "task_id": task.id,
        "status": task.status,
        "action": str(payload.get("action") or ""),
        "action_label": str(payload.get("action_label") or ""),
        "platform_account_id": int(payload.get("account_id") or 0),
        "total": int(payload.get("total") or 0),
        "succeeded": int(payload.get("succeeded") or 0),
        "skipped": int(payload.get("skipped") or 0),
        "failed": int(payload.get("failed") or 0),
        "executed": int(payload.get("executed") or 0),
        "error": str(payload.get("error") or ""),
        "finished_at": task.finished_at.strftime("%Y-%m-%d %H:%M:%S") if task.finished_at else "",
        "items": results,
    }


def task_snapshot(db: Any, task_id: int, *, user_id: Optional[int] = None) -> Optional[dict[str, Any]]:
    """按 id 取任务快照（**归属校验**：非本用户 ⇒ `None`，上层映射 404）。"""
    task = db.get(Task, task_id)
    if task is None or task.task_type != OPERATE_TASK_TYPE:
        return None
    if user_id is not None and task.user_id != user_id:
        return None
    return _summary(task)


def _run_in_thread(
    task_id: int, platform_account_id: int, user_id: int, action: str, item_ids: list[str]
) -> None:
    run_item_operate(
        task_id,
        platform_account_id=platform_account_id,
        user_id=user_id,
        action=action,
        item_ids=item_ids,
    )


__all__ = [
    "MAX_ITEMS_PER_TASK",
    "OPERATE_TASK_TYPE",
    "RESULT_FAILED",
    "RESULT_OK",
    "RESULT_SKIPPED",
    "run_item_operate",
    "start_item_operate",
    "task_snapshot",
]
