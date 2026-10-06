"""公共「清理记录表」任务（`system.queue_record_cleanup` 升级而来）。

2026-10-05 用户口径：「将队列清理任务改造升级为公共的清理记录表任务
`system.queue_record_cleanup`，主要任务是定期清理项目中**所有的**指定记录表；
优化定时任务支持携带参数，将清理记录表任务、记录表保留时间设置为任务携带的参数，
每次执行任务时可修改，默认记录表保留最新 15 天」。

## 设计：注册表驱动，而不是 if/else

每张可清理的记录表登记一条 `CleanableRecord`（键 + 中文名 + purge 函数）。
新增一张记录表 = **加一条登记**，不改清理循环、不改 API、不改前端 —— 与
`QUEUE_REGISTRY`（队列）、`JOB_REGISTRY`（任务）同一条无重复原则（DEF-044）。

⚠️ **清理范围只放「记录表」**（跑完就没用的执行痕迹）：队列详情记录、无流量清理
日志。刻意**不含** `notes` / `shop_products` / `monitoring_targets` 等业务数据表 ——
那些删了就是丢数据，不是"清理记录"。

## 保留期是任务参数

`retention_days`（默认 `DEFAULT_RETENTION_DAYS = 15`）由任务行 `params` 携带，
在**任务中心的新增 / 编辑弹窗**里改（`params_editable=("retention_days",)`），
每次执行时读取 —— 改完下一轮即生效，不需要重建任务行。

⚠️ 2026-09-27 的原口径「保留期是**固定口径**，不做成可调项」**已被本次取代**
（用户 2026-10-05 明确要求可配 + 默认 15 天）；原句保留在
`docs/02_设计文档/15_功能设计_定时任务中心.md` §4.5 并附对照表。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional, Sequence

from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.time import shanghai_now

logger = logging.getLogger(__name__)

#: 默认保留期（天）。用户口径「默认记录表保留最新 15 天」⇒ 默认值 15，
#: 可经任务行 `params.retention_days` 覆盖。
DEFAULT_RETENTION_DAYS = 15

#: 保留期上限（防止把 3650 天写进去后清理任务形同虚设）。
MAX_RETENTION_DAYS = 3650


@dataclass(frozen=True)
class CleanableRecord:
    """一张可清理的记录表。"""

    key: str
    """注册键（出现在返回结果 / 日志里，不要改 —— 改动会让历史日志对不上）。"""

    label: str
    """中文名（任务日志与前端展示用）。"""

    purge: Callable[[Session, datetime, Optional[int]], int]
    """`purge(db, cutoff, user_id) -> 删除行数`。

    ⚠️ **必须只删「终态 / 已完结」且早于 `cutoff` 的行** —— 活着的行删了就是丢任务。
    ⚠️ **必须按 `user_id` 收口**（`None` = 不过滤，仅供系统级调用）—— 定时任务是
    `user_scoped` 的，越租户删除是多用户数据事故。
    """


def _purge_queue_records(db: Session, cutoff: datetime, user_id: Optional[int]) -> int:
    """队列详情记录（三个队列的项 + 批次父行 + 账号级任务行）。

    复用 `note_queue_service` 的既有实现 —— 那里的「先删子行再删已无子行的批次行」
    顺序是有原因的（撞自引用外键 / 造孤儿），不要在别处再写一遍。
    """
    from backend.app.services.note_queue_service import cleanup_records

    if user_id is None:
        # 系统级调用（无归属）不删任何东西：本函数的每一路都是按 user 收口的，
        # 放开就等于「删全库队列记录」——那是一次误调用毁所有历史。
        logger.warning("queue record cleanup skipped: no user scope")
        return 0
    result = cleanup_records(db, user_id=int(user_id), cutoff=cutoff)
    return int(result["items"]) + int(result["batches"]) + int(result["account_tasks"])


def _purge_no_traffic_logs(db: Session, cutoff: datetime, user_id: Optional[int]) -> int:
    """无流量清理日志。

    ⚠️ 用户口径「日志表独立」指**独立成表、独立保留**，不是「永不清理」——
    它也走同一条保留期闸（否则这张表只增不减）。只删**已完结**的行：
    `running` 是正在跑的清理，删了等于把操作记录抹掉。
    """
    from sqlalchemy import delete

    from backend.app.models.no_traffic_cleanup_log import NoTrafficCleanupLog

    stmt = delete(NoTrafficCleanupLog).where(
        NoTrafficCleanupLog.status.in_(("done", "failed")),
        NoTrafficCleanupLog.created_at < cutoff,
    )
    if user_id is not None:
        stmt = stmt.where(NoTrafficCleanupLog.user_id == int(user_id))
    else:
        # 无归属不删（同队列记录那一路的理由）。
        logger.warning("no-traffic log cleanup skipped: no user scope")
        return 0
    return int(db.execute(stmt).rowcount or 0)


#: 可清理记录表清单。**顺序即执行顺序**，返回结果里的键与之一致。
#: `user_scoped` 任务只清行主本人的数据 ⇒ purge 里不必再带 user_id（调用方已按
#: 归属过滤）—— 但队列记录那一路的 `cleanup_records` 仍按 user_id 收口，
#: 由 `run_record_cleanup` 统一注入。
RECORD_CLEANERS: tuple[CleanableRecord, ...] = (
    CleanableRecord(key="queue", label="队列执行记录", purge=_purge_queue_records),
    CleanableRecord(
        key="no_traffic_cleanup_log",
        label="无流量笔记清理记录",
        purge=_purge_no_traffic_logs,
    ),
)


def cleaners_for_keys(keys: Optional[Sequence[str]]) -> tuple[CleanableRecord, ...]:
    """按注册键取子集（`None` = 全部）。未知键直接忽略（不报错：参数可能残留旧键）。"""
    if not keys:
        return RECORD_CLEANERS
    wanted = set(keys)
    return tuple(c for c in RECORD_CLEANERS if c.key in wanted)


def run_record_cleanup(
    user_id: int,
    *,
    retention_days: Optional[int] = None,
    records: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """定时任务入口（`system.queue_record_cleanup`，默认每天 07:00）。

    :param user_id: 行归属者，由 runner 注入（`user_scoped=True`，DEF-066）。
    :param retention_days: 保留期（天）。缺值 / 非法值 / 越界一律退
        `DEFAULT_RETENTION_DAYS`（不抛异常 —— 定时任务因参数写错而整轮不跑、
        记录永远清不掉，比退默认值糟得多）。
    :param records: 只清这些注册键（缺值 = 全部）。

    返回 `{"retention_days": n, "cutoff": iso, "cleaned": {key: rows},
    "total": rows}`。
    """
    days = _coerce_retention_days(retention_days)
    cutoff = shanghai_now().replace(microsecond=0) - _timedelta_days(days)
    db = SessionLocal()
    try:
        cleaned: dict[str, int] = {}
        for record in cleaners_for_keys(records):
            try:
                cleaned[record.key] = int(record.purge(db, cutoff, user_id) or 0)
            except Exception as exc:  # noqa: BLE001 - 一张表失败不该拖垮其余
                logger.error("record cleanup failed (%s): %s", record.key, exc)
                cleaned[record.key] = 0
        db.commit()
        result = {
            "retention_days": days,
            "cutoff": cutoff.isoformat(),
            "cleaned": cleaned,
            "total": sum(cleaned.values()),
        }
        logger.info("record cleanup: %s", result)
        return result
    finally:
        db.close()


def _timedelta_days(days: int):
    from datetime import timedelta

    return timedelta(days=days)


def _coerce_retention_days(raw: Any) -> int:
    """把任务参数里的保留期收敛成合法天数（**永不抛异常**）。"""
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    if days < 1:
        return DEFAULT_RETENTION_DAYS
    return min(days, MAX_RETENTION_DAYS)


def validate_retention_days(params: dict, db: Any = None, user: Any = None) -> None:
    """`params_validator` 钩子：`retention_days` 必须是 1..MAX 的整数。

    浅校验（键存在性）管不了「写了 0 天 / 写了 `"abc"`」—— 那种值如果被静默接受，
    下次执行时 `_coerce_retention_days` 会退默认值，用户在界面看到自己填的 0
    却按 15 天跑，是**界面骗人**。所以在写入时就 400。
    """
    if "retention_days" not in (params or {}):
        return
    raw = params.get("retention_days")
    try:
        days = int(raw)
    except (TypeError, ValueError):
        raise ValueError("retention_days 必须是 1..3650 的整数") from None
    if not (1 <= days <= MAX_RETENTION_DAYS):
        raise ValueError(f"retention_days 必须在 1..{MAX_RETENTION_DAYS} 之间")
