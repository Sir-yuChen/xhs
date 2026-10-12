"""LLM 网关预警**自动触发**（2026-10-13 用户口径）。

## 用户口径原文

> 「大模型的预警**移除 LLM 网关预警任务**，实现**根据配置的规则自动触发**，并站内消息提醒。」

## 设计（取代定时轮询）

**旧形态**：内置任务 `system.llm_gateway_monitor`（cron `0 */10 * * * *`）每 10 分钟
轮询本地台账 `llm_gateway_usage_logs`，对照 `llm_gateway_alerts` 规则判断超阈值 ⇒ 写通知。
代价：**最坏 10 分钟延迟**（台账写入与预警判定之间），且多一个常驻调度任务。

**新形态**：台账按写入即判定 —— 每次 `record_usage()` 成功落一行后，**异步**触发一次
对该 `(user_id, gateway_id)` 的规则评估。延迟从「≤10 分钟」降到「秒级」，且不再需要
调度任务。

## 为什么「异步 + 去抖」

`record_usage` 在**业务调用路径**上（`ai_service._complete` 之后），且自己另开 Session。
若在那里**同步**跑规则评估（多次聚合查询），会把延迟加到每一次 LLM 调用上 ——
不可接受（记账是旁路，不能拖慢业务，DEF-048 同一纪律）。

⇒ 用一个**进程内的轻量调度器**：
- `notify_usage_recorded(user_id, gateway_id)` 只**投递一个待办**（非阻塞，O(1)）；
- 后台**单线程 worker** 取出待办、按 `(user_id, gateway_id)` **去抖合并**
  （同一目标在 `DEBOUNCE_SECONDS` 内的多次投递只评估一次 —— 连续调用不会把评估
  放大成 N 次）、跑 `evaluate_alert`、超阈值写通知；
- worker 是 daemon 线程，随进程退出；**首次投递时惰性启动**（不占启动时间）。
- **安全网**：worker 内任何异常只 warning，绝不影响业务（同 DEF-048 / _sync_worker 纪律）。

## 与旧定时任务的关系

`system.llm_gateway_monitor` 内置任务**整条移除**（`job_registry.py` 删除 JobSpec +
迁移清存量行）。`run_llm_gateway_monitor_once` 与 `evaluate_alert` 等**判定逻辑保留复用**
（本模块直接调用 `llm_gateway_monitor_service.evaluate_alert`）—— 删的是「定时器」，
不是「判定规则」。

## 去重（保留旧语义）

仍按 `alert.last_fired_at` 做**同日去重**（一天一条），与本模块的去抖**正交**：
去抖管「同一秒的多次投递」，同日去重管「同一规则一天只响一次」。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta

from sqlalchemy import select

from backend.app.core.database import SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models import LlmGatewayAlert, LlmGatewayConfig, User
from backend.app.services.notification_service import _create

logger = logging.getLogger(__name__)

#: 同一 `(user_id, gateway_id)` 在此秒数内的多次投递**合并为一次评估**（去抖）。
#: 取值权衡：太小 ⇒ 连续批量调用会重复评估；太大 ⇒ 首次越界要多等一会。
#: 5 秒对「失败率/调用量」这类 24h 窗口指标足够（延迟本就是分钟级语义）。
DEBOUNCE_SECONDS = 5.0

#: 后台扫描间隔（秒）：worker 没有待办时按此周期醒来，避免忙等。
_IDLE_POLL_SECONDS = 2.0

_pending: set[tuple[int, int]] = set()
_last_done: dict[tuple[int, int], float] = {}
_lock = threading.Lock()
_wakeup = threading.Event()
_worker: threading.Thread | None = None


def notify_usage_recorded(user_id: int, gateway_id: int) -> None:
    """投递一个「台账刚写入」的待办（**非阻塞、O(1)、绝不抛异常**）。

    ⚠️ 只在**记账成功后**调用（调用点见 `llm_gateway_usage_service.record_usage`）——
    没写台账就没必要评估（规则本就基于台账聚合）。
    """
    try:
        with _lock:
            _pending.add((user_id, gateway_id))
        _wakeup.set()
        _ensure_worker()
    except Exception:  # noqa: BLE001 - 投递失败不得影响业务调用（DEF-048）
        logger.warning("LLM 预警投递失败（已忽略）", exc_info=True)


def _ensure_worker() -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(
            target=_run_worker, name="llm-gateway-alert-worker", daemon=True
        )
        _worker.start()
        logger.info("LLM 网关预警 worker 已启动（自动触发）")


def _run_worker() -> None:
    """后台单线程：取出待办 → 去抖 → 评估 → 写通知。任何异常只 warning（常驻不退出）。"""
    while True:
        try:
            with _lock:
                targets = list(_pending)
                _pending.clear()
            for target in targets:
                _handle_one(*target)
        except Exception:  # noqa: BLE001 - 循环外层网：worker 绝不因单次异常退出
            logger.warning("LLM 网关预警 worker 循环异常（已忽略）", exc_info=True)

        if not _pending:
            _wakeup.wait(timeout=_IDLE_POLL_SECONDS)
            _wakeup.clear()


def _handle_one(user_id: int, gateway_id: int) -> None:
    key = (user_id, gateway_id)
    now = time.monotonic()
    with _lock:
        last = _last_done.get(key)
        if last is not None and (now - last) < DEBOUNCE_SECONDS:
            # 去抖：刚评估过 ⇒ 把待办放回去，下一轮再说（合并本次与后续投递）。
            _pending.add(key)
            return
        _last_done[key] = now

    try:
        fired = evaluate_and_notify(user_id, gateway_id)
        if fired:
            logger.info("LLM 网关预警自动触发：user=%s gateway=%s 发出 %s 条", user_id, gateway_id, fired)
    except Exception:  # noqa: BLE001 - 单目标评估失败不影响其它目标
        logger.warning("LLM 网关预警评估失败（user=%s gateway=%s）", user_id, gateway_id, exc_info=True)


def evaluate_and_notify(user_id: int, gateway_id: int) -> int:
    """对该服务商跑一遍预警规则，超阈值写站内通知。返回发出条数。

    复用 `llm_gateway_monitor_service.evaluate_alert`（**判定逻辑唯一实现**）——
    本模块只负责「何时触发」，「是否命中」的规则不在这里重写（DEF-044）。
    仍保留同日去重（`last_fired_at`）与三级作用域语义（全网关/服务商/具体模型）。
    """
    from backend.app.services.llm_gateway_monitor_service import evaluate_alert

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            return 0

        now = shanghai_now()
        # ⚠️ 只评估「作用域命中该服务商」的规则：服务商作用域（gateway_id 相同）
        # 与全网关作用域（gateway_id 为 None，遍历全部服务商）—— 具体模型作用域
        # 也挂在某个 gateway_id 上。**与旧轮询逐条 evaluate_alert 的口径一致**：
        # 旧实现遍历该用户全部 enabled 规则、对每条调用 evaluate_alert（内部按作用域
        # 决定判哪些服务商）；这里为了「只做与该服务商相关的评估」先做一次粗筛，
        # 再交给 evaluate_alert 精确判定。
        alerts = db.scalars(
            select(LlmGatewayAlert).where(
                LlmGatewayAlert.user_id == user_id,
                LlmGatewayAlert.enabled == True,  # noqa: E712
            )
        ).all()

        gw_label = ""
        gw = db.get(LlmGatewayConfig, gateway_id)
        if gw is not None:
            gw_label = gw.name or gw.provider_key or f"#{gateway_id}"

        fired = 0
        for alert in alerts:
            # 粗筛：只处理「作用域含该服务商」的规则（全网关 or 该服务商）。
            if alert.gateway_id is not None and alert.gateway_id != gateway_id:
                continue
            hits = evaluate_alert(db, alert, user_id)
            if not hits:
                continue
            # 同日去重（保留旧语义）
            if alert.last_fired_at is not None and alert.last_fired_at.date() == now.date():
                continue
            for gid, value in hits:
                if gid != gateway_id:
                    # 全网关作用域可能命中别的服务商 ⇒ 交给那个服务商自己的投递处理，
                    # 避免本服务商的写入顺带评估全部（会重复发）。
                    continue
                label = gw_label or f"#{gid}"
                title, body = _describe(alert, value, label)
                _create(
                    db,
                    user_id=user_id,
                    title=title,
                    body=body,
                    level=alert.level or "warning",
                    source_type="llm_gateway_alert",
                    source_id=alert.id,
                )
                fired += 1
            if alert.gateway_id is not None and alert.gateway_id == gateway_id:
                alert.last_fired_at = now

        db.commit()
        return fired
    finally:
        db.close()


def _describe(alert: LlmGatewayAlert, value: float, gateway_label: str) -> tuple[str, str]:
    """生成通知标题与正文（与旧 `llm_gateway_monitor_service._describe` 同口径）。"""
    scope = f"{alert.platform or '全平台'}/{alert.function or '全功能'}"
    if alert.model_name:
        scope = f"{scope}/{alert.model_name}"
    metric_label = {"calls": "调用量", "fail_rate": "失败率", "health": "健康状态"}[alert.metric]
    title = f"LLM 网关预警: {metric_label}超限"
    if alert.metric == "fail_rate":
        body = f"服务商「{gateway_label}」{scope} 近 {alert.window_seconds // 3600}h 失败率 {value:.2%}，超过阈值 {alert.threshold:.2%}"
    elif alert.metric == "health":
        body = f"服务商「{gateway_label}」健康探测失败（不可达）"
    else:
        body = f"服务商「{gateway_label}」{scope} 近 {alert.window_seconds // 3600}h {metric_label} {value:g}，超过阈值 {alert.threshold:g}"
    return title, body


#: 供测试重置进程内状态（worker 是进程级全局，用例间必须隔离）。
def _reset_for_tests() -> None:
    with _lock:
        _pending.clear()
        _last_done.clear()
    _wakeup.clear()
