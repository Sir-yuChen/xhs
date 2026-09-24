"""同步任务台账（笔记同步 / 店铺商品同步共用）。

两个同步服务的任务管理逻辑完全同构（建行 → 防重 → 状态序列化），抽到这里
避免两份会各自漂移的实现。

为什么复用 ``tasks`` 表而不新建表：这张表一期就在用，同步任务与它同构
（user / type / status / progress / payload / 起止时间）。同步任务生命周期短，
把 ``account_id`` 放进 ``payload`` 查询足够 —— 与
``services/no_traffic_cleanup_service.py`` 同范式，不为它单独加列走迁移。

**防重口径（需求 N3）**：同一账号 + 同一 ``task_type`` 若已有
``pending`` / ``running`` 的任务，一律**返回已有任务**，不新建、不打平台。
手动触发与调度轮走的是同一个函数，所以两道入口共享同一条判断。
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models.task import Task

#: 两类同步任务的 task_type。
NOTE_SYNC_TASK_TYPE = "xhs_note_sync"
SHOP_GOODS_SYNC_TASK_TYPE = "xhs_shop_goods_sync"

#: 任务状态（设计 13 §9）：done 允许带 warning（部分完成），
#: failed 只用于「一行数据都没有」。
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
#: 协作式中断的终态（§24.8）：由取消端点写入，任务体检查点命中后自行保持，
#: `_finish` / `_mark_failed` **不得覆写**它。
STATUS_CANCELLED = "cancelled"

#: 「未完成」= 仍占着防重名额的状态。
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RUNNING)

#: **全量翻页**（设计 13 §21，2026-09-22 用户口径）：`None` = 翻到平台没有下一页为止。
#: ⚠️ 旧契约「有界翻页：默认 3 页、上限 20 页 × 100 条」（§8 / C5）**已被 §21 取代** ——
#: 「有界」现在由 `CrawlRateLimiter.wait_allow()` 的**速率**保证，不再由**页数**保证。
DEFAULT_MAX_PAGES: Optional[int] = None
#: 显式指定页数时的上限，**同时**是翻页循环的绝对兜底：平台异常导致 `has_more` 恒真时
#: 必须能停下来（否则「取消上限」= 死循环）。500 页 × 50 条 = 25000 条，
#: 是真实数据量（1961）的 10 倍以上。
#: ⚠️ 旧名 `MAX_MAX_PAGES = 20`（「单轮请求量硬顶」）**已被 §21 取代并删除**。
ABSOLUTE_PAGE_CAP = 500
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

#: 同步模式（设计 13 §17.8 / §22）：
#: * ``full`` = 全量重扫 + upsert，翻到平台没有下一页为止；
#: * ``incremental`` = 游标/时间窗 + 早停；
#: * ``auto`` = **按账号有没有同步历史自动选**（§22，2026-09-22 用户口径）：
#:   游标为 0（该账号从没成功同步过）⇒ ``full``；游标 > 0 ⇒ ``incremental``。
#:
#: ⚠️ ``auto`` 是**默认值**，且坏值也退回 ``auto`` 而不是 ``full`` ——
#: 把拼写错误退成全量，会让一次手滑变成 30–45 分钟的平台扫描。
SYNC_MODE_FULL = "full"
SYNC_MODE_INCREMENTAL = "incremental"
SYNC_MODE_AUTO = "auto"


def normalize_mode(value: Any) -> str:
    """把请求里的 mode 归一化；坏值 / 空值 → ``auto``（不 500）。"""
    text = str(value if value is not None else "").strip().lower()
    if text == SYNC_MODE_INCREMENTAL:
        return SYNC_MODE_INCREMENTAL
    if text == SYNC_MODE_FULL:
        return SYNC_MODE_FULL
    return SYNC_MODE_AUTO


def resolve_note_sync_mode(db: Session, account_id: int, requested: Any = SYNC_MODE_AUTO) -> str:
    """笔记同步的**实际**模式（设计 13 §22）。

    规则（用户 2026-09-22 口径：「如果该账号没有同步的笔记数据则进行全量同步，
    如果有历史数据则从最新数据的时间进行增量同步到现在时间」）：

    * 显式 ``full`` / ``incremental`` ⇒ 尊重调用方（调试 / 补救用）；
    * ``auto``（默认）⇒ 看游标：``note_sync_cursor_ms == 0`` 即「没有同步历史」
      ⇒ 全量；否则从游标增量。

    ⚠️ 「有没有同步历史」判据是**游标**而不是「表里有没有行」：``notes`` 行也可能
    来自采集 / 监控，且 ``notes`` 行里**不存发布时间**（``_raw_with_metrics``
    不落 ``publish_time``，真实库 grep 不到）⇒ 从数据行反推不出增量起点，
    游标是唯一可靠的「最新数据时间」。代价是「只有采集数据、从没同步过」的账号
    第一次会走全量 —— 那正是想要的行为（把账号自己的作品补全）。
    """
    mode = normalize_mode(requested)
    if mode != SYNC_MODE_AUTO:
        return mode
    return SYNC_MODE_INCREMENTAL if load_note_cursor(db, account_id) > 0 else SYNC_MODE_FULL


def resolve_goods_sync_mode(db: Session, account_id: int, requested: Any = SYNC_MODE_AUTO) -> str:
    """商品同步的**实际**模式（设计 13 §22），判据是 ``shop_state.goods_cursor_ms``。"""
    mode = normalize_mode(requested)
    if mode != SYNC_MODE_AUTO:
        return mode
    return SYNC_MODE_INCREMENTAL if load_goods_cursor(db, account_id) > 0 else SYNC_MODE_FULL


# ---------------------------------------------------------------------------
# 时间窗（同步范围）：用户自己在弹窗里选「全量」或「某个时间段」
# ---------------------------------------------------------------------------

#: 同步范围（设计 13 §23，2026-09-22 用户口径：「新增弹窗支持根据时间区间或全量同步，
#: 让用户自己决定同步哪些数据」）。``full`` = 不设时间界；``range`` = 只取
#: ``[start_date, end_date]`` 之间发布/创建的数据。
SYNC_SCOPE_FULL = "full"
SYNC_SCOPE_RANGE = "range"

#: 日期串一律 ``YYYY-MM-DD``（HTML date input 的原生格式，不引入第二种格式）。
DATE_FORMAT = "%Y-%m-%d"


def normalize_scope(value: Any) -> str:
    """把请求里的 scope 归一化；坏值 / 空值 → ``full``（不 500）。

    坏值退 ``full`` 而不是 ``range``：缺了起止日期的 range 等于「什么都没说」，
    静默同步全量至少是用户看得见的行为。
    """
    text = str(value if value is not None else "").strip().lower()
    return SYNC_SCOPE_RANGE if text == SYNC_SCOPE_RANGE else SYNC_SCOPE_FULL


def parse_date_to_ms(value: Any, *, end_of_day: bool = False) -> int:
    """``YYYY-MM-DD`` → 上海时区的毫秒时间戳；解析不了返回 0（调用方按缺值处理）。

    ``end_of_day=True`` 用于结束日期：用户选「到 09-20」指的是**包括** 09-20 一整天，
    否则 09-20 00:00 之后的的数据会被静默漏掉。
    """
    from datetime import datetime, timedelta, timezone

    text = str(value if value is not None else "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.strptime(text, DATE_FORMAT)
    except ValueError:
        return 0
    if end_of_day:
        parsed = parsed + timedelta(hours=23, minutes=59, seconds=59)
    return int(parsed.replace(tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)


def resolve_time_window(scope: Any, start_date: Any, end_date: Any) -> tuple[int, int]:
    """把弹窗选择解析成 ``(from_ms, to_ms)``；``full`` ⇒ ``(0, 0)``。

    * ``from_ms`` = 0 表示没有下界（从最早开始翻）；
    * ``to_ms`` = 0 表示没有上界（一直到最新）。

    ``range`` 但日期缺失 / 不可解析 ⇒ 也退回 ``(0, 0)`` 并在 ``invalid`` 里报出来，
    **不静默**按全量跑 —— 那会让用户以为自己在同步一个区间，实际同步了全部。
    """
    if normalize_scope(scope) != SYNC_SCOPE_RANGE:
        return 0, 0
    from_ms = parse_date_to_ms(start_date)
    to_ms = parse_date_to_ms(end_date, end_of_day=True)
    if from_ms <= 0 or to_ms <= 0:
        return 0, 0
    if from_ms > to_ms:
        from_ms, to_ms = to_ms, from_ms  # 起止写反了就交换，不报错也不漏
    return from_ms, to_ms



def load_note_cursor(db: Session, account_id: int) -> int:
    """上次成功同步的最大 ``publish_time``（**毫秒**），缺值/未同步过返回 0。

    游标存账号级字段而非任务行：任务行会被前端读到且生命周期短，
    而游标必须跨任务持久（D13 / §17.9）。
    """
    from backend.app.models.platform_account import PlatformAccount

    account = db.get(PlatformAccount, account_id)
    if account is None:
        return 0
    return int(account.note_sync_cursor_ms or 0)


def save_note_cursor(db: Session, account_id: int, cursor_ms: int, note_id: str = "") -> None:
    """推进游标。**只在本次同步落库成功之后调用**（D13）：落库失败时游标不动，
    下一轮从头重取，不会漏数据（DEF-043 的守卫）。"""
    from backend.app.models.platform_account import PlatformAccount

    account = db.get(PlatformAccount, account_id)
    if account is None:
        return
    account.note_sync_cursor_ms = max(int(cursor_ms or 0), int(account.note_sync_cursor_ms or 0))
    if note_id:
        account.note_sync_cursor_note_id = note_id


def load_goods_cursor(db: Session, account_id: int) -> int:
    """上次成功同步的商品 ``create_time``（**毫秒**），缺值返回 0。

    游标存在 `platform_accounts.shop_state` 的 JSON 里（2026-09-22 起，
    原绑定表的 `goods_cursor_ms` 列搬过去）。缺值 = 0 = 未同步过 = 下一轮走全量。
    """
    from backend.app.models.platform_account import PlatformAccount
    from backend.app.services.shop_binding_service import get_shop_state

    account = db.get(PlatformAccount, account_id)
    if account is None:
        return 0
    return int(get_shop_state(account)["goods_cursor_ms"] or 0)


def save_goods_cursor(db: Session, account_id: int, cursor_ms: int) -> None:
    """推进游标（**只增不减**的 max 语义，防乱序回退）。

    ⚠️ 必须经 `set_shop_state()`（内部赋新 dict）—— 自己 `json.loads` 这一列再原地改，
    SQLAlchemy 会认为值没变 ⇒ **UPDATE 不发 ⇒ 改动静默丢失**（本项目 JSON 列踩过的坑）。
    """
    from backend.app.models.platform_account import PlatformAccount
    from backend.app.services.shop_binding_service import get_shop_state, set_shop_state

    account = db.get(PlatformAccount, account_id)
    if account is None:
        return
    old = int(get_shop_state(account)["goods_cursor_ms"] or 0)
    set_shop_state(account, goods_cursor_ms=max(int(cursor_ms or 0), old))


def clamp_pages(value: Any, *, default: Optional[int] = DEFAULT_MAX_PAGES) -> Optional[int]:
    """把请求里的页数归一化（设计 13 §21）。

    * `None` / 空串 / 坏值 → `None` = **全量**（翻到平台没有下一页为止）；
    * 数字 → 夹到 `[1, ABSOLUTE_PAGE_CAP]`。

    ⚠️ 旧契约「坏值退回 default（3 页）」**已废**：把「没传」当「传了 3」正是
    「1961 条的账号永远只同步 180 条」的入口（§21.1）。`None` 现在**只表达
    「不限」一种含义**，坏值也走它 —— 请求体里没有可信的页数意图时，全量才是
    用户口径，3 页只是旧实现的猜测。
    """
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number < 1:
        return 1
    return min(number, ABSOLUTE_PAGE_CAP)


def clamp_page_size(value: Any, *, default: int = DEFAULT_PAGE_SIZE, upper: int = MAX_PAGE_SIZE) -> int:
    """把请求里的每页条数夹到 [1, upper]。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number < 1:
        return 1
    return min(number, upper)


def payload_account_id(task: Task) -> Optional[int]:
    """``payload.account_id`` 的容错读取。"""
    payload = task.payload if isinstance(task.payload, dict) else {}
    try:
        return int(payload.get("account_id"))
    except (TypeError, ValueError):
        return None


def find_active_task(db: Session, task_type: str, account_id: int) -> Optional[Task]:
    """该账号是否已有**未完成**的同类同步任务（防重第一道）。"""
    rows = db.scalars(
        select(Task).where(Task.task_type == task_type, Task.status.in_(ACTIVE_STATUSES))
    ).all()
    for row in rows:
        if payload_account_id(row) == account_id:
            return row
    return None


def latest_finished_task(db: Session, task_type: str, account_id: int) -> Optional[Task]:
    """最近一次**已完成**的同类同步任务（供状态接口回 ``last_result``）。

    ``cancelled`` 也算「已完成」的一种（§24.8）：用户取消后卡片要能显示
    「已取消 · 耗时 X」，把它排除在外会让结果行退化成「同步已结束」——
    取消是用户刚做过的操作，恰恰是最该被看见的那个结果。
    """
    rows = db.scalars(
        select(Task)
        .where(Task.task_type == task_type, Task.status.in_((STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)))
        .order_by(Task.id.desc())
    ).all()
    for row in rows:
        if payload_account_id(row) == account_id:
            return row
    return None


def create_task(
    db: Session,
    *,
    user_id: int,
    task_type: str,
    account_id: int,
    source: str,
    max_pages: Optional[int],
    page_size: int,
    status: str = STATUS_PENDING,
    extra: Optional[dict[str, Any]] = None,
) -> Task:
    """建一行同步任务（未提交）；调用方负责 ``commit`` + ``refresh``。"""
    payload: dict[str, Any] = {
        "account_id": account_id,
        "source": source,
        "max_pages": max_pages,
        "page_size": page_size,
        "progress": {"stored": 0, "total": None},
        "error": "",
        "note": "",
    }
    if extra:
        payload.update(extra)
    task = Task(
        user_id=user_id,
        platform="xhs",
        task_type=task_type,
        status=status,
        payload=payload,
    )
    db.add(task)
    return task


def task_payload(task: Task) -> dict[str, Any]:
    return dict(task.payload) if isinstance(task.payload, dict) else {}


def task_summary(task: Optional[Task]) -> dict[str, Any]:
    """任务摄要（设计 13 §4.0）：给触发端点与状态端点共用同一形态。

    §24.8：带 ``started_at`` / ``finished_at`` / ``duration_ms``（后端算好，
    前端不做时间算术）。无任务时三个字段给 ``None``（缺值口径，不造 0）。
    """
    if task is None:
        return {
            "task_id": None,
            "task_type": None,
            "status": "idle",
            "progress": None,
            "note": "",
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
        }
    payload = task_payload(task)
    return {
        "task_id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "progress": payload.get("progress"),
        "note": payload.get("note", ""),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "duration_ms": _duration_ms(task),
    }


def _duration_ms(task: Task) -> Optional[int]:
    """``finished_at - started_at`` 的毫秒数；任一端缺值 ⇒ ``None``（不造 0）。"""
    if task.started_at is None or task.finished_at is None:
        return None
    return int((task.finished_at - task.started_at).total_seconds() * 1000)


def status_payload(db: Session, task_type: str, account_id: int) -> dict[str, Any]:
    """状态端点响应体：当前任务 + 最近一次结果（设计 13 §4.2 / §19.5）。

    无进行中任务时 ``task_id=None, status="idle"`` —— 前端据此停轮询。

    ``last_result`` 的附加字段（§19，**缺值给 None/[] 不造 0**）：
    * ``sources``：笔记同步的分源明细 ``[{source, stored, total, error}]``；
    * ``detail_done`` / ``detail_total``：商品同步本轮补了多少 SKU 详情 /
      队列还剩多少（``None`` = 不知道，前端显示 ``-``）。
    """
    active = find_active_task(db, task_type, account_id)
    last = latest_finished_task(db, task_type, account_id)
    body = task_summary(active)
    body["last_result"] = None
    if last is not None:
        payload = task_payload(last)
        finished_at = last.finished_at.isoformat() if last.finished_at else None
        body["last_result"] = {
            "status": last.status,
            "progress": payload.get("progress"),
            "goods_stored": payload.get("goods_stored"),
            "note": payload.get("note", ""),
            "error": payload.get("error", ""),
            "finished_at": finished_at,
            "started_at": last.started_at.isoformat() if last.started_at else None,
            "duration_ms": _duration_ms(last),
            "sources": payload.get("sources") or [],
            "detail_done": payload.get("detail_done"),
            "detail_total": payload.get("detail_total"),
        }
    return body
