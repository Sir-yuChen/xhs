"""千帆（ark）店铺的**只读**登录态巡检（T-31）。

## 为什么需要

千帆登录态是**独立凭据**（`access-token-ark.xiaohongshu.com`）：它不能用账号 Cookie
复用（实测 `code=-13000 缺失登录凭证`），会自然过期。没有巡检时，失效只在用户**点
「同步商品」那一刻**才暴露 —— 期间页面看起来「一切正常，只是没商品」，这正是要消掉的
一种静默失败。

## 四条纪律（与一期账号 Cookie 巡检同档，且更保守）

| 纪律 | 做法 |
|---|---|
| **只读** | 只调 `GET /api/edith/seller/info/v2`；**不碰**同步、绑定、写接口 |
| **低频** | 默认 `qianfan_health_check_interval_hours = 6`；按 `health_checked_at` 判到期，**未到期直接跳过** —— 所以手动点一次也不会变成刷请求 |
| **失败不重试风暴** | 一次失败即标 `invalid` + 写原因 + 通知一次，然后**等下一次巡检**（沿用项目「失败不自动重试」纪律） |
| **不毁数据** | 失效**不清空**已同步的映射（`shop_note_goods` 原样保留）—— 见 T-31 既有结论 |

## 通知为什么要「只在状态翻转时」

巡检是周期性的：若每次都通知，一次未处理的失效会在 24 小时内堆 4 条同样的铃铛。
所以只在 `active/unknown → invalid` 的**那一次**写通知，后续轮次只刷新时间与原因。

## 2026-09-22：巡检对象从「绑定行」换成「账号」

`shop_account_bindings` 整表移除后，巡检对象是**有 `shop_id_fk` 的存活账号**；
状态与巡检时间落在 `platform_accounts.shop_state`（读写只经 `shop_binding_service`，
避免 JSON 列「原地改不标脏」的坑）。取票走唯一入口
`qianfan_cookies_for_account()`（只认账号自己的 `ark` 通道）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory
from backend.app.core.database import SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models import Notification, PlatformAccount

logger = logging.getLogger(__name__)

#: 巡检周期（小时）。默认 6 小时 —— 千帆登录态按天/周级过期，无需更密。
DEFAULT_HEALTH_INTERVAL_HOURS = 6

#: `status_message` 落库前的截断长度（平台原文可能很长，页面只需要可读的一句话）。
_STATUS_MESSAGE_LIMIT = 200

#: 千帆登录态失效时给用户的固定原因（平台原文为空时用它兜底）。
INVALID_REASON_FALLBACK = "千帆登录态已失效，请在千帆重新登录后重新绑定"

#: 账号没有千帆令牌时的固定原因。
MISSING_CREDENTIAL_REASON = "该账号还没有千帆（ark）令牌，无法巡检"


def _clamp_message(message: str) -> str:
    text = (message or "").strip() or INVALID_REASON_FALLBACK
    return text[:_STATUS_MESSAGE_LIMIT]


def check_account_health(
    db: Session,
    account: PlatformAccount,
    adapter_factory: Any = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """对一个账号做一次**只读**探活，就地更新它的 `shop_state`。

    返回 `{"previous_status", "status", "message", "flipped_to_invalid", "shop_name"}`。
    **不 commit**（由调用方决定事务边界），也不抛异常 —— 探活失败本身就是业务结果。
    """
    from backend.app.services.shop_binding_service import (
        get_shop_state,
        qianfan_cookies_for_account,
        set_shop_state,
        shop_for_account,
    )

    now = now or shanghai_now()
    previous = str(get_shop_state(account)["status"] or "unknown")
    factory = adapter_factory or get_qianfan_api_adapter_factory()
    shop = shop_for_account(db, account.id)

    cookies = qianfan_cookies_for_account(db, account.id)
    if not cookies:
        # 「没有令牌」不是「探活失败」，是**没东西可探** —— 如实写原因，
        # 不要用一句「已失效」让用户以为自己去登录过、只是过期了（DEF-057 同型）。
        set_shop_state(
            account,
            status="invalid",
            status_message=MISSING_CREDENTIAL_REASON,
            health_checked_at=now.isoformat(),
        )
        flipped = previous != "invalid"
        if flipped:
            _notify_invalid(db, account)
        return {
            "previous_status": previous,
            "status": "invalid",
            "message": MISSING_CREDENTIAL_REASON,
            "flipped_to_invalid": flipped,
            "shop_name": shop.shop_name if shop is not None else "",
        }

    try:
        adapter = factory(cookies)
        success, message, raw_payload = adapter.get_seller_info()
    except Exception as exc:  # noqa: BLE001 - 探活失败 = 结果为 invalid，不是程序错误
        success, message, raw_payload = False, f"千帆探活异常：{exc}", None

    if success:
        set_shop_state(account, status="active", status_message="")
        # 店铺信息可能改名：只有平台确实给了才覆盖（拿不到就保持原值，不写空）。
        data = raw_payload.get("data") if isinstance(raw_payload, dict) else None
        if isinstance(data, dict) and shop is not None:
            name = str(data.get("seller_name") or "").strip()
            seller_id = str(data.get("user_id") or "").strip()
            if name:
                shop.shop_name = name
            if seller_id and not shop.seller_id:
                shop.seller_id = seller_id
    else:
        set_shop_state(account, status="invalid", status_message=_clamp_message(str(message or "")))

    set_shop_state(account, health_checked_at=now.isoformat())
    state = get_shop_state(account)
    flipped = state["status"] == "invalid" and previous != "invalid"
    if flipped:
        # 通知**在这里**发（而不是在定时任务里）：手动「立即巡检」与定时巡检走同一条路径，
        # 于是「状态翻转 → 一条铃铛」这个规则对两种触发方式都成立，且都只发一次。
        _notify_invalid(db, account)
    return {
        "previous_status": previous,
        "status": str(state["status"]),
        "message": str(state["status_message"]),
        "flipped_to_invalid": flipped,
        "shop_name": shop.shop_name if shop is not None else "",
    }


def _notify_invalid(db: Session, account: PlatformAccount) -> None:
    """失效通知（铃铛）。文案必须**可执行**：说清是谁、哪家店、去做什么。"""
    from backend.app.services.shop_binding_service import shop_for_account

    shop = shop_for_account(db, account.id)
    shop_name = shop.shop_name if shop is not None else ""
    nickname = account.nickname or account.external_user_id
    db.add(
        Notification(
            user_id=account.user_id,
            title="千帆登录态失效",
            body=(
                f"账号「{nickname or account.id}」绑定的千帆店铺"
                f"「{shop_name or '（未命名店铺）'}」登录态已失效，"
                "商品信息将无法更新，请重新登录千帆并导入令牌。"
            ),
            level="warning",
        )
    )


def account_health_due(
    account: PlatformAccount,
    now: datetime,
    interval_hours: int = DEFAULT_HEALTH_INTERVAL_HOURS,
) -> bool:
    """是否到期该巡检：从未巡检 → 到期；否则按 `health_checked_at + interval` 判。"""
    from backend.app.services.shop_binding_service import get_shop_state, parse_state_time

    checked = parse_state_time(get_shop_state(account)["health_checked_at"])
    if checked is None:
        return True
    return checked <= now - timedelta(hours=max(1, int(interval_hours)))


def run_shop_health_checks(
    db: Session,
    *,
    now: Optional[datetime] = None,
    adapter_factory: Any = None,
    interval_hours: int = DEFAULT_HEALTH_INTERVAL_HOURS,
    only_due: bool = True,
    limit: Optional[int] = None,
    account_ids: Optional[Sequence[int]] = None,
    user_id: int,
) -> dict[str, Any]:
    """对**指定用户**已绑店的存活账号做一轮巡检（**有界**：默认只查到期的那几条）。

    ⚠️ `user_id` 必填（DEF-066，设计 15 §4.1.1）：账号查询恒带
    `PlatformAccount.user_id == user_id` —— 原「全库已绑店账号」的形态已废，
    一行触发只巡检行主本人的账号。`account_ids` 非空 = 只巡检指定账号
    （建 / 改双路归属校验，API 层收口）。

    * 单条失败不影响其他账号（per-account try/except，沿用一期巡检写法）；
    * 失效**只通知一次**（状态翻转时），避免周期巡检把铃铛刷满；
    * **不触碰** `shop_note_goods` —— 失效不等于数据作废。
    """
    now = now or shanghai_now()
    statement = select(PlatformAccount).where(
        PlatformAccount.user_id == user_id,
        PlatformAccount.shop_id_fk.isnot(None),
        PlatformAccount.deleted_at.is_(None),
    )
    if account_ids:
        statement = statement.where(PlatformAccount.id.in_(list(account_ids)))
    # ⚠️ SQLAlchemy 2.0 的 `Select` **没有 `.all()`**（1.x 遗留写法）⇒ 必须 `db.scalars(...).all()`。
    # 与 `api/accounts.py:205` 同一写法，别再写回 `statement.all()`。
    accounts = db.scalars(statement.order_by(PlatformAccount.id.asc())).all()

    checked = 0
    skipped = 0
    flipped = 0
    items: list[dict[str, Any]] = []
    for account in accounts:
        if limit is not None and checked >= limit:
            break
        if only_due and not account_health_due(account, now, interval_hours):
            skipped += 1
            continue
        try:
            result = check_account_health(db, account, adapter_factory, now=now)
        except Exception as exc:  # pragma: no cover - 防御：check_account_health 内部已兜底
            logger.warning("Shop health check failed for account %s: %s", account.id, exc)
            continue
        checked += 1
        if result["flipped_to_invalid"]:
            flipped += 1
        items.append(
            {
                "platform_account_id": account.id,
                **{k: v for k, v in result.items() if k != "previous_status"},
            }
        )

    db.commit()
    return {
        "checked": checked,
        "skipped": skipped,
        "flipped_to_invalid": flipped,
        "items": items,
    }


def run_shop_health_checks_once(
    user_id: int,
    interval_hours: Optional[int] = None,
    account_ids: Optional[Sequence[int]] = None,
) -> None:
    """调度器入口：自带会话的巡检（与 `check_all_account_cookies_once` 同形）。

    ⚠️ `user_id` 必填（DEF-066，行归属者，runner 注入）：只巡检行主本人的账号；
    `account_ids` 非空 = 只巡检指定账号，空 = 本人的全部账号。
    """
    db = SessionLocal()
    try:
        result = run_shop_health_checks(
            db,
            interval_hours=interval_hours or DEFAULT_HEALTH_INTERVAL_HOURS,
            account_ids=account_ids,
            user_id=user_id,
        )
        logger.info(
            "Qianfan shop health check completed: %s checked, %s skipped (not due), %s newly invalid",
            result["checked"],
            result["skipped"],
            result["flipped_to_invalid"],
        )
    except Exception as exc:  # noqa: BLE001 - 定时任务不得把异常抛回调度器
        logger.error("run_shop_health_checks_once failed: %s", exc)
    finally:
        db.close()
