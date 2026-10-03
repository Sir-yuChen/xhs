"""指标快照的**调度入口**（设计 17 §13.9.2，M2 方向 3 切片 2，2026-10-03）。

## 为什么单独成模块

`job_registry` 里的 `func` 必须是一个**只吃 `user_id` 的可调用对象**
（`user_scoped=True` 的行主隔离约定，DEF-066）。而采集本身在
`note_metrics_service` / `shop_metrics_service` 里，它们要一个 `Session`
（由调用方决定生命周期）。

⇒ 这一层做「调度签名 → 服务签名」的**唯一适配**：开 Session、调服务、关 Session。
放在 `api/` 里会让 `job_registry` 反向依赖接口层（分层倒置），故独立成服务模块。

⚠️ **两个采集都是纯本地零平台请求**：数据源是已落库的 `notes.raw_json` 与
`shop_products` 行 —— 不消耗平台额度、不触发风控，也没有限流器要过。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.app.core.database import SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models import User
from backend.app.services import note_metrics_service, shop_metrics_service

logger = logging.getLogger(__name__)


def run_note_metrics_collect_once(user_id: int) -> dict[str, Any]:
    """调度入口：采集**指定用户**的笔记指标当日快照（DEF-066 行主隔离）。

    `user_scoped`：一行触发只采行主人的笔记；返回写入行数供调度日志记账。
    """
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            # ⚠️ **抛异常而不是静默返回 0**：行主不存在说明调度行指向了已删用户，
            # 静默返回 0 会写一条「成功、0 条」的日志，把数据异常伪装成正常空跑。
            raise RuntimeError(f"run_note_metrics_collect_once: user {user_id} not found")

        stat_date = note_metrics_service.stat_date_of(shanghai_now())
        written = note_metrics_service.collect_snapshots(db, user_id=user.id)
        return {"stat_date": stat_date, "collected": written}
    finally:
        db.close()


def run_shop_metrics_collect_once(user_id: int) -> dict[str, Any]:
    """调度入口：采集**指定用户**的商品指标当日快照（DEF-066 行主隔离）。"""
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            raise RuntimeError(f"run_shop_metrics_collect_once: user {user_id} not found")

        stat_date = shop_metrics_service.stat_date_of(shanghai_now())
        written = shop_metrics_service.collect_snapshots(db, user_id=user.id)
        return {"stat_date": stat_date, "collected": written}
    finally:
        db.close()


__all__ = [
    "run_note_metrics_collect_once",
    "run_shop_metrics_collect_once",
]
