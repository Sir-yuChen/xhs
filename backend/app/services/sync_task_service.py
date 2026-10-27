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

from typing import Any, Callable, Optional

import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models.task import Task

#: 店铺商品台账同步的任务类型。链路已退场，常量仅用于识别历史任务行。
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
#: 终态 = 不会再变化的三个（成功 / 失败 / 协作式中断）。
#: ⚠️ 与 `note_queue_service.TERMINAL_STATUSES` 必须是**同一个集合** —— 那边是字面量
#: 元组、这里是按 `STATUS_*` 组合出来的，两处漂移会造出「队列认为结束了、同步台账
#: 认为还在跑」的活锁。守卫：`test_testing_spec_guards.py::test_terminal_statuses_agree`。
TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)

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

#: 「任务执行详情」的**刷新粒度**（条）—— 两个账号级同步执行体（笔记同步 / 店铺商品同步）
#: **每处理这么多条**就把任务行进度刷一次（设计 13 §42.3）。
#: 用户口径（2026-09-28）：「店铺商品同步和账号笔记同步**每执行 50 条**应更新一次任务
#: 执行详情数据」。
#:
#: ⚠️ 为什么它与 `DEFAULT_PAGE_SIZE` 是**同一个数**：两个执行体的翻页循环都是
#: 「取一页 → 落库 → 刷进度」，页边界就是天然刷盘点。刻意**不**新造第二个数 ——
#: 一旦粒度小于页（例如页 100、粒度 50），就得把一页拆成两半分别落库刷盘，
#: 复杂度换不来任何可见收益（DEF-044 同型：同一语义只留一个数）。
PROGRESS_FLUSH_EVERY = 50
assert DEFAULT_PAGE_SIZE <= PROGRESS_FLUSH_EVERY, (
    "默认页大小不得超过进度刷新粒度：页是刷盘点，页比粒度大就等于「超过 50 条才刷」"
)

#: ⚠️ **只有「条数」这一个上界，没有时间上界**（2026-09-28 清死代码时删掉了
#: `PROGRESS_FLUSH_SECONDS = 30`）：那个常量**从未被任何代码读过**，只在注释里声明了
#: 一个不存在的机制（幽灵契约 —— 比没有更坏，会让人以为「超过 30 秒一定会刷」）。
#: 真正的「慢慢涨」由**逐篇 / 逐批回调**达成（设计 13 §42.3 的页内刷新两行）：
#: 笔记 `_hydrate_note_rows` 每篇回调、商品 `_fetch_page_skus` 每批回调，
#: 借执行体自己的 commit 刷盘 ⇒ 约 7.5 秒一格，本来就比 30 秒更细。

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


#: 商品同步的**状态范围**（设计 13 §30.3.2；2026-10-02 用户口径「商品同步更新需求为
#: 同步全部状态的商品」）。
#:
#: * ``all`` = **不传 `card_type`**，翻页拿平台侧全部状态（售卖中 / 已下架 / 已冻结 /
#:   整改 / 审核中 / 审核驳回 / 审核通过），**默认值**；
#: * 其余取值 = 只同步该状态（取值域 = `apis.xhs_qianfan_apis.QIANFAN_ITEM_STATUSES`，
#:   与平台页面 7 个 tab 同源）。
#:
#: ⚠️ 默认必须是 ``all`` 而不是 ``on_shelf``：台账是「本地唯一的商品真相源」，
#: 缺了已下架/已冻结的商品，列表页的状态筛选与「删除商品」的连带判断都会静默失真
#: （看起来正常，只是少了一半商品）。
SYNC_ITEM_STATUS_ALL = "all"


def normalize_item_status(value: Any) -> str:
    """把请求里的商品状态范围归一化。

    坏值 → ``all``（**不**退成某个具体状态：把拼写错误退成「只同步售卖中」会让
    一次手滑变成「台账静默少掉已下架 / 已冻结的商品」，与 `normalize_mode` 同一条
    纪律 —— 坏值退回**信息最全**的那个取值，而不是信息最少的）。
    """
    from apis.xhs_qianfan_apis import QIANFAN_ITEM_STATUSES

    text = str(value if value is not None else "").strip().lower()
    if text in QIANFAN_ITEM_STATUSES:
        return text
    return SYNC_ITEM_STATUS_ALL


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



def resolve_incremental_scope(
    *,
    incremental: bool,
    cursor_ms: int,
    time_from_ms: int,
) -> int:
    """增量同步的**下界**（毫秒）—— `max(游标, 用户自选起始日)`；非增量 ⇒ 原样返回自选起点。

    ## 为什么要有这个函数（2026-10-11，SSOT 收敛）

    这条规则原先在**四个地方**各写了一遍，全是同一个表达式::

        scope_from_ms = max(last_cursor_ms, time_from_ms) if incremental else time_from_ms

    两处 `_sync_with_task`（笔记 / 商品）、一处笔记的翻页窗口、一处商品翻页的
    `effective_from_ms` —— 最后一处连 `if incremental` 都省了（它只在增量分支里执行，
    等价）。而它上游的 `load_note_cursor` / `load_goods_cursor` 本身就已经是在抽
    同一规则的两个方言版本。

    ⚠️ 这是**「增量从游标起，用户自选区间的下界也要满足，两者是 and 不是 or」**
    这条口径的唯一实现。要改判定就改这里，别再就地改调用点 ——
    四份拷贝里改三份的最坏结果是**同步静默漏数据**（少取一段窗口），
    它不报错，只在数据里慢慢体现。
    """
    if not incremental:
        return int(time_from_ms or 0)
    return max(int(cursor_ms or 0), int(time_from_ms or 0))


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


def _latest_account_log(
    db: Session, *, user_id: int, account_id: int, job_key: str
) -> Any:
    """该用户该账号**最新一条**该定时任务的日志行（无 ⇒ `None`）。

    「最新」按 `id` 倒序（自增主键 = 落库顺序，不受时钟回拨影响）—— 与
    `latest_finished_task` 同口径。
    """
    from backend.app.models import ScheduledJobLog

    return db.scalars(
        select(ScheduledJobLog)
        .where(
            ScheduledJobLog.user_id == user_id,
            ScheduledJobLog.platform_account_id == account_id,
            ScheduledJobLog.job_key == job_key,
        )
        .order_by(ScheduledJobLog.id.desc())
        .limit(1)
    ).first()


#: 定时任务 job_key → 业务任务类型（账号卡链路；孤儿日志收口按它找回任务行）。
#:
#: ⚠️ **2026-10-27 只剩一行**：`system.note_sync` / `system.goods_note_sync` 两个 job_key
#: 已从任务注册表摘除（设计 75 §4.5），其任务类型也不复存在；留着映射等于「按一个
#: 不会再出现的 job_key 去查任务行」。`system.shop_goods_sync` 同批退场，但它的任务行
#: 仍在历史数据里，孤儿日志收口需要这条映射才能把旧日志对回任务行。
ACCOUNT_JOB_TASK_TYPES: dict[str, str] = {
    "system.shop_goods_sync": SHOP_GOODS_SYNC_TASK_TYPE,
}


def close_orphan_running_logs(db: Session) -> int:
    """把**没有对应在跑任务**的 `running` 日志收口（进程重启后的孤儿）。**不 commit**。

    **为什么需要**（2026-09-27 实测，用户报「再次分析账号笔记同步任务是否真实还在执行」）：
    观测线程 `_watch_task_run` 是**进程内**的；进程重启后它没了，而它负责的那条日志仍是
    `running`。此时任务行已被启动恢复复位（`pending`）或已重跑完成（`done`）⇒ **日志再也
    没人写终态** ⇒ 永远「正在执行」，账号卡据此显示「同步中」，**而任务其实早已结束**
    （实测 `log=14821` running ↔ `task=51` done，卡片骗了用户一个多小时）。

    与 `_watch_task_run` 的「无限等」是**互补的两条腿**：那条保证「进程活着时日志不因超时
    误判」，这条保证「进程重启后日志不留孤儿」。缺任一条都会出现「日志与实际任务不一致」。

    收口口径（**优先精确**）：日志 `task_id` 能查到任务行 ⇒ 用它的真实终态；查不到
    （存量 NULL / 任务行被记录清理）⇒ 回落「该账号最近一次终态任务」；连那个也没有 ⇒
    `failed` + 「执行被应用退出中断」（「日志 running 而任务行连终态都没有」只可能是中断）。
    """
    from backend.app.models import ScheduledJobLog
    from backend.app.models.scheduled_job import LOG_FAILED, LOG_RUNNING, LOG_SUCCESS

    rows = db.scalars(
        select(ScheduledJobLog).where(ScheduledJobLog.status == LOG_RUNNING)
    ).all()
    closed = 0
    for log in rows:
        task_type = ACCOUNT_JOB_TASK_TYPES.get(str(log.job_key or ""))
        if task_type is None:
            continue  # 非账号卡链路（cron 全量 / 系统作业）没有这条腿
        account_id = log.platform_account_id
        if account_id is None:
            continue  # 缺账号 ⇒ 归属不明，不动它（缺值口径：不猜）
        if find_active_task(db, task_type, int(account_id)) is not None:
            continue  # 真在跑 ⇒ 日志该是 running
        task = db.get(Task, int(log.task_id)) if log.task_id else None
        if task is None:
            task = latest_finished_task(db, task_type, int(account_id))
        if task is not None:
            payload = task_payload(task)
            log.status = LOG_SUCCESS if str(task.status) == STATUS_DONE else LOG_FAILED
            log.finished_at = task.finished_at or shanghai_now()
            log.duration_ms = _duration_ms(task)
            log.message = str(payload.get("note") or "")
            log.exception_info = str(payload.get("error") or "")
        else:
            log.status = LOG_FAILED
            log.finished_at = shanghai_now()
            log.message = "执行被应用退出中断"
            log.exception_info = "执行被应用退出中断"
        closed += 1
    if closed:
        db.flush()
    return closed


def close_running_logs_for_task(db: Session, task_id: int) -> int:
    """把**某次业务任务**对应的 `running` 日志行收口。**不 commit**。

    **为什么需要**（2026-09-27 用户口径「点击取消按钮二次确认后真实取消再执行的任务并修改
    对应任务日志记录相关状态和数据」）：取消走 `POST /tasks/{id}/cancel`，它原本只改**任务行**；
    日志行只由观测线程（`_watch_task_run`）在任务到终态时收口 —— 而观测线程可能已随进程消失
    （重启）或还在轮询周期里 ⇒ **用户点了取消、任务也真取消了，日志却仍显示「正在执行」**，
    与「日志状态 = 实际任务状态」这条不变量直接冲突。

    日志状态表没有 `cancelled` 一档（`running`/`success`/`failed`/`skipped`）⇒ 记 `failed`
    + message「已被用户取消」（`_terminal_fallback_reason` 同口径：**用户取消不算成功**）。
    观测线程若还活着，写终态前会看到日志已终态 ⇒ **不覆写**（幂等，不会把 cancelled 改回别的）。
    """
    from backend.app.models import ScheduledJobLog
    from backend.app.models.scheduled_job import LOG_FAILED, LOG_RUNNING

    rows = db.scalars(
        select(ScheduledJobLog).where(
            ScheduledJobLog.task_id == int(task_id),
            ScheduledJobLog.status == LOG_RUNNING,
        )
    ).all()
    closed = 0
    for log in rows:
        log.status = LOG_FAILED
        log.finished_at = shanghai_now()
        log.message = "已被用户取消"
        log.exception_info = ""
        closed += 1
    if closed:
        db.flush()
    return closed


def _payload_from_log(log: Any) -> dict[str, Any]:
    """日志行 ⇒ 状态端点响应体（**形状与 `task_summary` + `last_result` 逐字兼容**）。

    映射（用户口径「只有任务日志状态在『正在执行』，才展示同步中」）：

    * 日志 `running` ⇒ `status="running"`（前端 `syncTaskPhase` 判 active ⇒ 按钮「同步中」）；
    * 日志 `success` / `failed` / `skipped` ⇒ `status="idle"` + `last_result` ⇒ 按钮回到可点，
      失败原因走 `last_result`（`syncTerminalNotice` 只在「没成功」时打扰用户）。
      ⚠️ `skipped`（被并发门挡下）映射成 `done`：跳过不是失败，弹一条失败提示是错归因。
    """
    # ⚠️ 常量住在 `models/scheduled_job`（`backend.app.models` 包**不导出**它们 ——
    # 从包里 import 会 ImportError，实测 500 就是这么来的）。
    from backend.app.models.scheduled_job import LOG_FAILED, LOG_RUNNING

    running = str(log.status or "") == LOG_RUNNING
    started = log.started_at.isoformat() if log.started_at else None
    finished = log.finished_at.isoformat() if log.finished_at else None
    note = str(log.message or "")
    error = str(log.exception_info or "")
    body: dict[str, Any] = {
        # 日志不指向业务任务行（`scheduled_job_logs.task_id` 在账号卡路径上是空的）
        # ⇒ 给 `None` 而不是编一个（缺值口径）。
        "task_id": None,
        "task_type": log.job_key,
        "status": "running" if running else "idle",
        "progress": log.progress,
        "note": note,
        "started_at": started,
        "finished_at": None if running else finished,
        "duration_ms": None if running else log.duration_ms,
        "last_result": None,
    }
    if not running:
        body["last_result"] = {
            "task_id": None,
            "status": "done" if str(log.status or "") != LOG_FAILED else "failed",
            "progress": log.progress,
            "goods_stored": None,
            "note": note,
            "error": error,
            "finished_at": finished,
            "started_at": started,
            "duration_ms": log.duration_ms,
            "sources": [],
            "detail_done": None,
            "detail_total": None,
        }
    return body


def status_payload(
    db: Session,
    task_type: str,
    account_id: int,
    *,
    user_id: Optional[int] = None,
    job_key: str = "",
) -> dict[str, Any]:
    """状态端点响应体：当前任务 + 最近一次结果（设计 13 §4.2 / §19.5）。

    ⚠️ **账号卡的判据是「该账号最新一条同步日志的状态」**（2026-09-27 用户口径：
    「账号信息卡的判断逻辑应只需要关注最新一条的同步笔记任务日志的状态即可」+
    「根据任务日志详情的状态判断终态即可」「只有任务日志状态在『正在执行』，才展示同步中」）。
    为什么改判据：业务任务行与「用户点的那一下」不是一一对应（同一个任务行可能被复用 /
    被恢复重跑），而**日志行才是「一次点击 = 一条记录」**；且任务行的终态由执行方写，
    日志的终态由观测方写，两者曾出现「日志 failed / 任务 running」的相反结论。

    传了 `user_id` + `job_key` ⇒ 优先按日志判；日志缺失（存量数据 / 非账号卡入口）⇒
    **回落到既有业务任务行判据**（行为与改造前逐字一致，不制造新的空态）。

    无进行中任务时 ``task_id=None, status="idle"`` —— 前端据此停轮询。

    ``last_result`` 的附加字段（§19，**缺值给 None/[] 不造 0**）：
    * ``sources``：笔记同步的分源明细 ``[{source, stored, total, error}]``；
    * ``detail_done`` / ``detail_total``：商品同步本轮补了多少 SKU 详情 /
      队列还剩多少（``None`` = 不知道，前端显示 ``-``）。
    """
    # ⚠️ **不做兼容回落**（2026-09-27 用户口径「不做兼容逻辑，历史错误数据修复」）：
    # 判据**只有**「该账号最新一条同步日志」这一条 —— 没有日志就是 `idle`（空态），
    # 而不是回头去读业务任务行。两套判据并存正是「日志 failed / 卡片同步中」的土壤
    # （同一个事实两个答案，读的人永远不知道该信哪个）。存量缺 `account_id` 的日志
    # 由数据修复脚本补齐，**不靠读取侧猜**。
    if user_id is None or not job_key:
        raise ValueError(
            "status_payload 必须给出 user_id 与 job_key —— 判据只有「最新一条日志」一条路"
        )
    log = _latest_account_log(
        db, user_id=user_id, account_id=account_id, job_key=job_key
    )
    if log is None:
        # 这个账号还没跑过（或日志被清理）⇒ 空态：不显示同步中、也没有结果行。
        return {
            "task_id": None,
            "task_type": job_key,
            "status": "idle",
            "progress": None,
            "note": "",
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
            "last_result": None,
        }
    return _payload_from_log(log)


def progress_percent(progress: Any) -> Optional[int]:
    """任务行 `payload.progress` → 0–100 的百分比；**取不到给 `None`**（缺值口径，不造 0）。

    **唯一实现**（DEF-044）：两处消费同一份判据 ——

    * 进度泵的探针（`task_progress_probe`，执行期间每 2s 写一次日志 `progress`）；
    * 观测线程写**日志终态**时（`scheduled_job_runner._watch_task_run`）取任务的
      最终进度 —— 泵最多晚 2s，终态这一取才是「失败时停在哪个比例」的准确答案
      （2026-09-27 用户口径「任务执行失败后任务进度不要变，应展示执行的进度比例」）。

    ## 三个口径的优先级（2026-09-28 重排，设计 13 §42.4）

    1. **`processed / work_total`**（**首选**，两个执行体都写）：`processed` = 本任务
       **累计已处理条数**（列表落库 + 详情补齐都算「处理」，每篇/每商品一次），
       `work_total` = 本任务**启动时可知的总工作量**（任务开始时算一次，中途不缩）。
       这两个键是**唯一**能做到「进度条慢慢涨、且绝不 0→100 反复跳」的口径 ——
       分子只增、分母固定 ⇒ 比值单调。
    2. `detail_done / detail_total`（账号级详情补齐阶段）：仅在没写 `processed` 时兜底。
    3. `stored / total`（列表落库）：同样只是兜底。

    ⚠️ **为什么旧口径会跳**（2026-09-28 第二轮要修的那件事）：`detail_done / detail_total`
    曾经被页内补齐借用，而它每页的 `detail_total` 只有**本页**待补数 ⇒ 一页内 0→100、
    下一页又回 0。现在页内补齐**不再写 `detail_*`**，改累加 `processed`。
    ⚠️ **分母语义（2026-09-28 第三轮修订，设计 13 §43）**：`work_total` 是「**启动时
    可知**的工作量」—— 无界全量的平台总量、启动时的详情欠账数、平台按**同一条查询条件**
    报出的商品总量（千帆接口按 `create_time_from` 服务端过滤 ⇒ 有界同步它也是本次工作量）。
    **运行中才发现的量（本页枚举数 / 已落库条数）一律不进分母**：它与分子数的是**同一批
    对象**，比值恒 100% ⇒ 进度条第一页就打满（旧实现的有界分支，用户报障「刚开跑就 100%」）。
    ⇒ 有界同步因此可能**没有** `work_total`（欠账为 0 时）⇒ 本函数返回 `None` ⇒
    前端如实渲染「比例未知」的不定量条（「未知」≠ 0%，见设计 13 §35.18.6 / §43.1）。
    """
    if not isinstance(progress, dict):
        return None
    processed = progress.get("processed")
    work_total = progress.get("work_total")
    if isinstance(processed, int) and isinstance(work_total, int) and work_total > 0:
        return max(0, min(100, int(processed * 100 / work_total)))
    done = progress.get("detail_done")
    todo = progress.get("detail_total")
    if isinstance(todo, int) and todo > 0 and isinstance(done, int):
        return max(0, min(100, int(done * 100 / todo)))
    stored = progress.get("stored")
    total = progress.get("total")
    if not isinstance(total, int) or total <= 0 or not isinstance(stored, int):
        return None
    return max(0, min(100, int(stored * 100 / total)))


def task_progress_probe(task_id: int) -> Callable[[], Optional[int]]:
    """造一个**进度探针**（设计 13 §35.17.3）：返回 0–100；取不到给 `None`。

    给 `scheduled_job_runner` 的进度泵用 —— 泵跑在**独立线程**里，不能复用请求的
    session，所以每次调用**自查一次库**（2s 一次，开销可忽略）。

    缺值口径（§35.17.3，与全项目一致）：任务行已不在、或分页总数未知
    （`total` 为 `None` —— 首页还没回来时就是这种）⇒ `None`，泵据此**不写** progress，
    而不是写个 0 冒充「刚开始」。

    ⚠️ 口径是**任务号**不是「账号当前的活动任务」：调用方（账号卡端点）已经确切知道
    自己建的是哪一行，按 `task_id` 取才不会把「另一个刚被建出来的任务」的进度算进来。
    笔记与商品**共用这一份实现**（DEF-044：同一语义不留两份）。
    """
    def _probe() -> Optional[int]:
        db = SessionLocal()
        try:
            task = db.get(Task, task_id)
            if task is None:
                return None
            # 换算走唯一实现 `progress_percent`（与观测线程写终态同一把尺子）。
            return progress_percent(task_payload(task).get("progress"))
        except Exception:  # noqa: BLE001 - 探针永不抛：观测不得影响执行
            return None
        finally:
            db.close()

    return _probe


def await_task_terminal(
    task_id: int,
    *,
    timeout_seconds: Optional[float],
    poll_seconds: float,
) -> dict[str, Any]:
    """等一个业务任务行走到**终态**，返回 `{task_id, status, progress, note, error}`。

    两处调用方，**共用这一份实现**（DEF-044）：定时任务日志的观测线程
    （`scheduled_job_runner._watch_task_run`）与调度侧定向执行
    （`scheduler_service._run_targeted_*`）。两边都要「按真实终态写结果」，
    各写一份迟早一边记得处理 `cancelled`、另一边漏掉。

    三种收尾都给**明确答案**（不返回 `None`，不留「不知道」给调用方去猜）：

    * `done` / `failed` / `cancelled` ⇒ 原样带出（含任务 payload 里的 `note` / `error`）；
    * 行不存在（被记录清理删掉）⇒ `status="missing"`；
    * 超时 ⇒ `status="timeout"`（缺值口径：**不**猜成成功，也不猜成失败原因）。

    ⚠️ 返回体**不从 `task_summary` 取**：那个形状不含 `error`（失败原因），而日志要写它。

    ⚠️ `timeout_seconds=None` ⇒ **无限等**（2026-09-27 用户口径「任务无需超时校验」）：
    长同步任务一次拿全正文与商品（实测 105 分钟仍在中途）是**正常**的；用超时把观测方
    提前放走，只会得到「一条失败日志 + 一个还在跑的任务」这种自相矛盾（用户报「日志
    记录依旧是失败，为何实际任务未终止，状态与实际任务不一致」）。要超时是**调用方**
    的选择，函数本身两种都支持。
    """
    #: `None` = 无限等（不给 deadline）；否则到点返回 `timeout`。
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    while True:
        db = SessionLocal()
        try:
            row = db.get(Task, task_id)
            if row is None:
                return {
                    "task_id": task_id,
                    "status": "missing",
                    "progress": None,
                    "note": "",
                    "error": f"任务 #{task_id} 已不存在（可能被记录清理删除），本次执行没有结果可记",
                }
            if row.status in TERMINAL_STATUSES:
                payload = task_payload(row)
                return {
                    "task_id": task_id,
                    "status": row.status,
                    "progress": payload.get("progress"),
                    "note": payload.get("note") or "",
                    "error": payload.get("error") or "",
                }
        finally:
            db.close()
        if deadline is not None and time.monotonic() >= deadline:
            return {
                "task_id": task_id,
                "status": "timeout",
                "progress": None,
                "note": "",
                "error": (
                    f"等待任务 #{task_id} 超过 {int((timeout_seconds or 0) / 60)} 分钟仍未结束，"
                    "任务仍在后台继续执行，可在账号卡查看"
                ),
            }
        time.sleep(poll_seconds)
