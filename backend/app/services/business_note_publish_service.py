"""「每日发布商业笔记」自动发布任务（设计 15 §19，2026-10-07 用户需求）。

## 用户口径（原文）

> 新增定时任务 每日发布商业笔记
> 　调用参数为：执行账号，发布笔记时间间隔单位秒，默认发布笔记篇数也就是商品数，
>   支持对每个账号指定发布篇数，指定生成笔记的方式千帆智能笔记或LLM生成单选
> 　具体业务逻辑：根据定时任务调用参数获取需要执行的账号，校验账号千帆是否有效，
>   无效的跳过，推送站内消息进行提醒，
> 　　循环执行账号获取该账号下的发布笔记篇数为 0 的指定商品数，对这些商品调用
>   指定生成笔记的方式生成笔记，对生成成功的笔记进行发布

## 这个模块做什么（其余不归它管）

| 做 | 不归它 |
|---|---|
| ① 逐账号：千帆探活 → 无效跳过 + 发站内消息 | 账号登录 / 补登（`shop_health_service` 只探活） |
| ② 取「挂链笔记数为 0」的商品（复用商品列表同一个数） | 商品同步（`shop_goods_sync_service`） |
| ③ 按 `generate_mode` 调**既有**生成链路并**等终态** | 生成实现本身（`product_note_gen_service` / `note_gen_llm_worker`） |
| ④ 对**生成成功**的笔记按 `interval_seconds` 间隔逐篇发布 | 发布实现本身（`note_publish_service`） |

## 三条硬约定

1. **不新增第二套真相源**：商品筛选复用 `shop_products_service` 的 `note_goods`
   相关子查询口径；生成 / 发布都调既有入口，本模块**不含**任何平台请求代码。
2. **生成 / 发布在本轮内等终态**（与页面「入队即返回」相反）：定时任务没有前端，
   「对生成成功的笔记发布」这条因果必须在本轮闭合 —— 否则下一轮无从判断上一篇
   到底成了没有（见设计 15 §19.3）。
3. **无效账号只跳过、不阻断整轮**：一个账号探活失败 / 生成失败 / 发布失败，
   都只影响它自己那一段（逐条 try/except），其余账号照跑。

## 参数形状（设计 15 §19.2）

| 键 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `account_ids` | `int[]` / 逗号串 | `[]` | 空 = 本人全部账号 |
| `limit` | `int` **或** `{"8": 3, "9": 5}` | `DEFAULT_LIMIT` | 默认篇数 / 按账号覆盖 |
| `interval_seconds` | `int` | `DEFAULT_INTERVAL_SECONDS` | 两篇**发布**之间的间隔秒数 |
| `generate_mode` | `"qianfan"` / `"llm"` | `"qianfan"` | 生成方式单选 |
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.models import PlatformAccount, Task
from backend.app.models.note import Note
from backend.app.services import note_queue_service

logger = logging.getLogger(__name__)

#: 生成方式（**单选**，用户口径）。
MODE_QIANFAN = "qianfan"
MODE_LLM = "llm"
GENERATE_MODES: tuple[str, ...] = (MODE_QIANFAN, MODE_LLM)
GENERATE_MODE_LABELS: dict[str, str] = {
    MODE_QIANFAN: "千帆智能笔记",
    MODE_LLM: "LLM 生成",
}

#: 默认「发布笔记篇数」（= 每账号取的商品数）。用户口径「默认发布笔记篇数也就是商品数」，
#: 没说具体值 ⇒ 给 1（最小可跑、最保守的写操作面，符合平台风控口径；用户可在参数里调大）。
DEFAULT_LIMIT = 1
#: 单账号一轮的上限（防御性硬顶）：参数传 10000 不该让一轮跑一整天。
MAX_LIMIT = 50

#: 两篇发布之间的默认间隔（秒）。用户口径只说「单位秒」，没给值 ⇒ 给 30s
#: （平台写操作风控，与 `_sync_account_gap_seconds` 同量级；用户可调）。
DEFAULT_INTERVAL_SECONDS = 30
#: 间隔上限（秒）：一小时已经远超任何合理场景，超过按上限夹（不报错）。
MAX_INTERVAL_SECONDS = 3600

#: 参数键（与注册表 `params_editable` **同一组**）。
PARAM_ACCOUNT_IDS = "account_ids"
PARAM_LIMIT = "limit"
PARAM_INTERVAL_SECONDS = "interval_seconds"
PARAM_GENERATE_MODE = "generate_mode"
#: 2026-10-07 追加（用户口径「发布失败是否重试、重试次数最多 3 次、重试间隔时间」）。
PARAM_RETRY_ON_FAILURE = "retry_on_failure"
PARAM_MAX_RETRIES = "max_retries"
PARAM_RETRY_INTERVAL_SECONDS = "retry_interval_seconds"

#: 默认「发布失败是否重试」= 重试（用户口径把它列成一个可配开关，缺省取更稳妥的一侧）。
DEFAULT_RETRY_ON_FAILURE = True
#: 重试次数上限 —— **用户口径「最多 3 次」**（硬顶，不是建议值；参数传 99 按 3 夹）。
MAX_RETRIES_CAP = 3
DEFAULT_MAX_RETRIES = MAX_RETRIES_CAP
#: 两次重试之间的默认间隔（秒）；上限同 `MAX_INTERVAL_SECONDS`。
DEFAULT_RETRY_INTERVAL_SECONDS = 30

#: 布尔型参数的**唯一**解析口径（`"true"/"1"/"yes"/"on"` ⇒ True；`"false"/"0"/"no"/"off"` ⇒ False）。
_TRUE_WORDS = frozenset({"true", "1", "yes", "y", "on", "是"})
_FALSE_WORDS = frozenset({"false", "0", "no", "n", "off", "否"})


class BusinessPublishParamsError(ValueError):
    """参数不合规（映射到 400 `BUSINESS_PUBLISH_PARAMS_INVALID`）。"""

    def __init__(self, message: str, code: str = "BUSINESS_PUBLISH_PARAMS_INVALID") -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# 参数解析（唯一实现 —— 建 / 改 / 执行三处共用同一把尺子）
# --------------------------------------------------------------------------- #
def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    """宽松取整数：空 / 非法 ⇒ 默认值；越界 ⇒ 夹回（**不报错**，与 `days` 同口径）。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(number, maximum))


def resolve_interval_seconds(params: dict[str, Any] | None) -> int:
    """两篇发布之间的间隔秒数（缺省 / 非法 ⇒ 默认；越界夹回）。"""
    raw = (params or {}).get(PARAM_INTERVAL_SECONDS)
    return _coerce_int(raw, default=DEFAULT_INTERVAL_SECONDS, minimum=0, maximum=MAX_INTERVAL_SECONDS)


def resolve_generate_mode(params: dict[str, Any] | None) -> str:
    """生成方式（未知取值 ⇒ **报错**，不静默退回默认 —— 静默退回 = 用户以为选了 LLM 却跑千帆）。"""
    raw = (params or {}).get(PARAM_GENERATE_MODE)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return MODE_QIANFAN
    mode = str(raw).strip().lower()
    if mode not in GENERATE_MODES:
        raise BusinessPublishParamsError(
            f"生成方式只能是 {' 或 '.join(GENERATE_MODES)}，收到 {raw!r}", code="BUSINESS_PUBLISH_MODE_INVALID"
        )
    return mode


def resolve_default_limit(params: dict[str, Any] | None) -> int:
    """默认「发布笔记篇数」（标量形状；映射形状取不到时回落到它）。"""
    raw = (params or {}).get(PARAM_LIMIT)
    if isinstance(raw, dict):
        # 映射形状没有天然的「默认」⇒ 用内置默认（映射里没点名的账号走它）。
        return DEFAULT_LIMIT
    return _coerce_int(raw, default=DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)


def _coerce_bool(value: Any, *, default: bool) -> bool:
    """宽松取布尔：空 / 无法识别 ⇒ 默认值（**不报错**，与 `_coerce_int` 同口径）。

    ⚠️ 与 `generate_mode` 的取舍刻意不同：`generate_mode` 未知值**报错**（选错会跑到
    另一条生成链路，后果大）；`retry_on_failure` 未知值**回落默认**（最坏是「多试一次」，
    不产生错误结果）。判据 = 「错了之后用户会看到什么」。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return default


def resolve_retry_on_failure(params: dict[str, Any] | None) -> bool:
    """发布失败后**是否重试**（缺省 = `DEFAULT_RETRY_ON_FAILURE`）。"""
    return _coerce_bool((params or {}).get(PARAM_RETRY_ON_FAILURE), default=DEFAULT_RETRY_ON_FAILURE)


def resolve_max_retries(params: dict[str, Any] | None) -> int:
    """**每次**发布失败后的最大重试次数（缺省 3；**硬顶 `MAX_RETRIES_CAP` = 3**）。

    ⚠️ 硬顶是**用户口径的一部分**（「重试次数最多 3 次」）—— 所以这里是 `min(...)` 夹回
    而**不是**像 `limit` 那样只管防御。哪怕参数写 99 也只重试 3 次。
    """
    raw = (params or {}).get(PARAM_MAX_RETRIES)
    return _coerce_int(raw, default=DEFAULT_MAX_RETRIES, minimum=0, maximum=MAX_RETRIES_CAP)


def resolve_retry_interval_seconds(params: dict[str, Any] | None) -> int:
    """两次重试之间的间隔秒数（缺省 30；越界夹回，**不报错**）。"""
    raw = (params or {}).get(PARAM_RETRY_INTERVAL_SECONDS)
    return _coerce_int(
        raw, default=DEFAULT_RETRY_INTERVAL_SECONDS, minimum=0, maximum=MAX_INTERVAL_SECONDS
    )


def resolve_retry_policy(params: dict[str, Any] | None) -> tuple[bool, int, int]:
    """**唯一实现**：(enabled, max_retries, interval_seconds)。

    `enabled` 为 False 时后两个值**仍会被解析并返回**（保持纯函数语义，调用方自己判
    `enabled`）—— 早退会让「关了重试还配了次数」这种组合在测试里观察不到。
    """
    return (
        resolve_retry_on_failure(params),
        resolve_max_retries(params),
        resolve_retry_interval_seconds(params),
    )


def resolve_account_limit(params: dict[str, Any] | None, account_id: int) -> int:
    """**唯一实现**：`(params, account_id)` → 该账号这一轮取几件商品。

    两种形状（设计 15 §19.2）：
    * `limit` 是标量 / 缺省 ⇒ 所有账号都用它（缺省 = `DEFAULT_LIMIT`）；
    * `limit` 是映射 ⇒ 命中 `str(account_id)` 用映射值，未命中回落默认。

    ⚠️ 映射的**键**必须是合法账号 id（十进制整数）—— 无法解析的键 ⇒ 抛
    `BusinessPublishParamsError`（**不静默丢弃**：丢一个键 = 那个账号悄悄用默认值，
    是「参数写了没生效」这类静默缺陷的温床）。值同样不合法 ⇒ 抛。
    """
    payload = params or {}
    raw = payload.get(PARAM_LIMIT)
    if not isinstance(raw, dict):
        return _coerce_int(raw, default=DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)

    normalized: dict[int, int] = {}
    for key, value in raw.items():
        try:
            key_id = int(str(key).strip())
        except (TypeError, ValueError) as error:
            raise BusinessPublishParamsError(
                f"按账号指定篇数时，键必须是账号 ID，收到 {key!r}"
            ) from error
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError) as error:
            raise BusinessPublishParamsError(
                f"账号 {key_id} 的篇数必须是整数，收到 {value!r}"
            ) from error
        if number < 1 or number > MAX_LIMIT:
            raise BusinessPublishParamsError(
                f"账号 {key_id} 的篇数必须在 1–{MAX_LIMIT} 之间，收到 {number}"
            )
        normalized[key_id] = number
    return normalized.get(int(account_id), DEFAULT_LIMIT)


def validate_business_publish_params(params: dict[str, Any] | None, db: Any, user: Any) -> None:
    """注册表 `params_validator` 钩子：建 / 改任务行时把参数先过一遍。

    只调上面那几个解析器 —— **同一把尺子**（DEF-044）：建行时拒掉的形状，
    执行时不会再换一种判法。

    ⚠️ **还校验 `limit` 映射的键是不是本人的账号**（DEF-066 归属口径）：
    键写错一个数字（如 `{"80": 3}` 而账号是 `8`）在纯形状校验下是合法的，
    而它的后果是**静默不生效**（那个账号照旧跑默认篇数）—— 「参数写了没生效」
    这类静默缺陷正是要在这里挡掉的。`account_ids` 的归属校验在
    `api/schedule.py::_validate_account_ids`；两处判据都指向**同一个事实**
    （账号必须属于行主），不合并成一处是因为两者的入参形状不同（列表 vs 映射键）。
    """
    resolve_generate_mode(params)
    resolve_default_limit(params)
    resolve_interval_seconds(params)
    # 重试三件（2026-10-07 追加）：同一把尺子 —— 建行时拒掉的形状执行时不会再换判法。
    resolve_retry_policy(params)
    # 逐账号形状：把映射的键 / 值全过一遍（用一个不存在的 id 探默认值即可，
    # 真正的逐账号取值在 `resolve_account_limit` 里）。
    resolve_account_limit(params, -1)

    raw_limit = (params or {}).get(PARAM_LIMIT)
    if not isinstance(raw_limit, dict) or db is None or user is None:
        return
    try:
        wanted = {int(str(key).strip()) for key in raw_limit}
    except (TypeError, ValueError):
        return  # 形状问题已由上面的解析器报错，这里不再重复报
    owned = set(
        db.scalars(
            select(PlatformAccount.id).where(
                PlatformAccount.user_id == int(getattr(user, "id", user)),
                PlatformAccount.deleted_at.is_(None),
            )
        ).all()
    )
    strangers = sorted(wanted - owned)
    if strangers:
        raise BusinessPublishParamsError(
            f"按账号指定篇数时，这些账号不属于当前用户：{strangers}",
            code="BUSINESS_PUBLISH_ACCOUNT_NOT_OWNED",
        )


# --------------------------------------------------------------------------- #
# 取「挂链笔记数为 0」的商品
# --------------------------------------------------------------------------- #
def select_business_items(
    db: Session, account: PlatformAccount, *, limit: int
) -> list[dict[str, Any]]:
    """该账号下**还没挂过商品笔记**的商品，最多 `limit` 件（**平台实时 + 本地比对**）。

    ⚠️ 设计 75 S5（2026-10-27）：原来是一条 SQL（`shop_products` LEFT JOIN
    `note_goods` 计数为 0）。两张表都已 DROP ⇒ 口径改成两步：

    1. **平台实时**取该账号最近创建的一批商品（`rt.list_products`，按创建时间倒序）；
    2. **本地判重**：这批商品里，`generation_records` 已有成功记录（`item_id` 命中）
       的跳过 —— 那就是「已经生成过笔记」的，不再重复生成。

    为什么用 `generation_records` 而不是"挂链笔记数"：挂链数现在只能**逐个商品**
    实时查（一页 20 条 = 20 次请求），自动发布是后台任务，不该为判重打几十次平台。
    生成记录是**本系统**「我是否已经为它做过笔记」的权威记载，正是这个判据要的。

    取数上限取 `limit * 4`（预取一批，判重后从中取够 `limit` 件）——
    全部已生成过时宁可返回不足也不再去打平台（后台任务不该无界翻页）。
    """
    from backend.app.models.generation import GenerationRecord
    from backend.app.services import realtime_platform_query_service as rt
    from backend.app.services.shop_binding_service import qianfan_cookies_for_account
    from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory

    wanted = max(1, int(limit))
    cookies = qianfan_cookies_for_account(db, account.id)
    if not cookies:
        return []
    adapter = get_qianfan_api_adapter_factory()(cookies)
    result = rt.list_products(
        adapter, status="all", page=1, page_size=min(max(wanted * 4, 20), 100),
        sort_field="create_time", order="desc",
    )
    candidates = [row for row in (result.get("items") or []) if str(row.get("item_id") or "")]

    done: set[str] = set()
    ids = [str(row["item_id"]) for row in candidates]
    if ids:
        for value in db.scalars(
            select(GenerationRecord.item_id).where(
                GenerationRecord.user_id == int(account.user_id),
                GenerationRecord.item_id.in_(ids),
                GenerationRecord.status == "success",
            )
        ).all():
            done.add(str(value or ""))

    picked: list[dict[str, Any]] = []
    for row in candidates:
        if str(row["item_id"]) in done:
            continue
        picked.append(row)
        if len(picked) >= wanted:
            break
    return picked


# --------------------------------------------------------------------------- #
# 生成（两种方式，都等终态）
# --------------------------------------------------------------------------- #
def _open_item_note_pk(db: Session, *, user_id: int, item_task_type: str) -> dict[int, int]:
    """未终态队列项 → `{task.id: note_pk}`（用来在等待后取回「生成了哪篇」）。

    生成 worker 的落库形状（两期一致，实测核对）：**先 `db.commit()` 落笔记**，
    再 `finish_item(extra={"note_id": ...})` **另起一次提交**写行体。因此等待
    `wait_*_done()` 返回时，笔记行**一定已经在库里**（提交顺序保证），而行体里的
    `note_pk` 可能还在上一次快照里。

    ⚠️ **行体的 `note_id` 是本地合成 ID（`local_<uuid>`），不是主键** —— 不能直接
    `db.get(Note, note_id)`。行体里确实有 `note_pk`（两期 worker 都写了），但它是
    `finish_item` 补写的；这里**同时**留一条按 `payload.note_id` 查的兜底，
    两条都不中才返回 `None`（缺值 ⇒ 不发布，见调用方）。
    """
    rows = db.scalars(
        select(Task).where(Task.user_id == int(user_id), Task.task_type == item_task_type)
    ).all()
    out: dict[int, int] = {}
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        try:
            pk = int(payload.get("note_pk") or 0)
        except (TypeError, ValueError):
            pk = 0
        if pk:
            out[int(row.id)] = pk
    return out


def _generate_with_qianfan(
    db: Session, *, account: PlatformAccount, product: dict[str, Any]
) -> Optional[int]:
    """千帆智能笔记：入队 + 等终态，返回生成的笔记主键（失败 / 超时 ⇒ `None`）。"""
    from backend.app.services import product_note_gen_service

    try:
        # ⚠️ 形参顺序是 `(db, user_id, account, item_id)`（位置参数），不是关键字 ——
        #    写错会 `TypeError` 被下面的 except 吞成「生成失败」，静默跑空整轮。
        product_note_gen_service.start_generate_note(
            db, int(account.user_id), account, str(product.get("item_id") or "")
        )
    except Exception as exc:  # noqa: BLE001 - 门禁失败 = 这件商品跳过，不拖垮整轮
        logger.warning("auto-publish: qianfan generate gate failed item=%s: %s", product.get("item_id"), exc)
        return None
    if not product_note_gen_service.wait_generate_done(timeout=_GENERATE_WAIT_SECONDS):
        logger.warning("auto-publish: qianfan generate timed out item=%s", product.get("item_id"))
        return None
    return _note_pk_for_item(db, user_id=int(account.user_id), item_id=str(product.get("item_id") or ""))


def _generate_with_llm(
    db: Session, *, account: PlatformAccount, product: dict[str, Any]
) -> Optional[int]:
    """LLM 生成：入队 + 等终态，返回生成的笔记主键（失败 / 超时 ⇒ `None`）。"""
    from backend.app.services import note_gen_llm_worker

    try:
        note_gen_llm_worker.start_llm_generate_note(
            db,
            int(account.user_id),
            account_id=int(account.id),
            item_id=str(product.get("item_id") or ""),
            source=note_queue_service.SOURCE_AUTO_BUSINESS_PUBLISH,
        )
    except Exception as exc:  # noqa: BLE001 - 门禁失败（未配网关 / 模板缺）= 这件商品跳过
        logger.warning("auto-publish: llm generate gate failed item=%s: %s", product.get("item_id"), exc)
        return None
    if not note_gen_llm_worker.wait_llm_generate_done(timeout=_LLM_GENERATE_WAIT_SECONDS):
        logger.warning("auto-publish: llm generate timed out item=%s", product.get("item_id"))
        return None
    return _note_pk_for_item(
        db, user_id=int(account.user_id), item_id=str(product.get("item_id") or "")
    )


#: 生成等待上限（秒）。千帆三步链路真机 ~6–21s，120s 是平台卡死线；LLM 上游更慢，给 240s。
_GENERATE_WAIT_SECONDS = 120.0
_LLM_GENERATE_WAIT_SECONDS = 240.0
#: 发布等待上限（秒）：一篇的媒体下载 + 上传 + 发布，真机数十秒。
_PUBLISH_WAIT_SECONDS = 180.0


def _note_pk_for_item(db: Session, *, user_id: int, item_id: str) -> Optional[int]:
    """刚生成的笔记主键：**按队列项行体 + 关系表**两条腿取（唯一实现）。

    顺序（第一条命中即返回）：
    ① **未终态生成项的行体 `note_pk`** —— worker `finish_item` 补写的权威值；
    ② 行体 `note_id`（本地合成 ID `local_<uuid>`）→ 按 `notes.note_id` 查主键；
    ③ `note_goods` 关系表按 `item_id` 找**最新一篇**本地生成笔记。

    都没命中 ⇒ `None`（调用方**不发布**：宁可少发一篇，也不猜一篇发出去）。
    """
    from backend.app.models.note import NOTE_TYPE_LOCAL_GENERATED

    open_qianfan = _open_item_note_pk(
        db, user_id=user_id, item_task_type=note_queue_service.TASK_TYPE_GEN_ITEM
    )
    open_llm = _open_item_note_pk(
        db, user_id=user_id, item_task_type=note_queue_service.TASK_TYPE_GEN_LLM_ITEM
    )
    for candidate in dict(open_qianfan, **open_llm).values():
        note = db.get(Note, int(candidate))
        if note is not None:
            return int(note.id)

    # ② 行体 `note_id`
    rows = db.scalars(
        select(Task)
        .where(Task.user_id == int(user_id))
        .where(Task.task_type.in_((note_queue_service.TASK_TYPE_GEN_ITEM, note_queue_service.TASK_TYPE_GEN_LLM_ITEM)))
        .order_by(Task.id.desc())
    ).all()
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        local_id = str(payload.get("note_id") or "").strip()
        if not local_id:
            continue
        pk = db.scalar(select(Note.id).where(Note.note_id == local_id))
        if pk:
            return int(pk)

    # ③ 兜底（商品 → 最新一篇本地生成笔记）
    #    ⚠️ 设计 75 S5（2026-10-27）：原来第三跳 join `note_goods` 关系表，该表已随迁移
    #    `n8o9p0q1r2s3` DROP ⇒ 改读**笔记自己的** `raw_json.item_id`（生成链路落库时
    #    就写进去的字段，比关系表更靠近真相：关系表是派生物，raw_json 是当时的事实）。
    stmt = (
        select(Note.id)
        .where(Note.user_id == int(user_id))
        .where(Note.note_type == NOTE_TYPE_LOCAL_GENERATED)
        .order_by(Note.id.desc())
        .limit(50)
    )
    for note_id_value in db.scalars(stmt).all():
        note = db.get(Note, int(note_id_value))
        raw = note.raw_json if note is not None and isinstance(note.raw_json, dict) else {}
        if str(raw.get("item_id") or "") == str(item_id):
            return int(note_id_value)
    return None


# --------------------------------------------------------------------------- #
# 发布（等终态）
# --------------------------------------------------------------------------- #
def _publish_and_wait(db: Session, *, user_id: int, note_pk: int) -> dict[str, Any]:
    """发布一篇并等终态。返回 `{ok, note_pk, error}`（**不抛**，失败即结果）。"""
    from backend.app.services import note_publish_service

    try:
        note_publish_service.start_publish(db, user_id=user_id, note_pk=int(note_pk))
    except Exception as exc:  # noqa: BLE001 - 门禁失败 = 这一篇不发
        return {"ok": False, "note_pk": int(note_pk), "error": str(exc)}
    if not note_publish_service.wait_publish_done(int(note_pk), timeout=_PUBLISH_WAIT_SECONDS):
        return {"ok": False, "note_pk": int(note_pk), "error": "发布超时（等待终态超时）"}
    db.expire_all()
    note = db.get(Note, int(note_pk))
    if note is None:
        return {"ok": False, "note_pk": int(note_pk), "error": "笔记已被删除"}
    from backend.app.models.note import PUBLISH_STATUS_PUBLISHED

    if str(note.publish_status) == PUBLISH_STATUS_PUBLISHED:
        return {"ok": True, "note_pk": int(note_pk), "error": ""}
    return {
        "ok": False,
        "note_pk": int(note_pk),
        "error": str(note.publish_error or "发布未成功"),
    }


# --------------------------------------------------------------------------- #
# 调度器入口
# --------------------------------------------------------------------------- #
def run_business_note_auto_publish(
    user_id: int,
    account_ids: Optional[Sequence[int]] = None,
    **params: Any,
) -> Optional[dict[str, Any]]:
    """调度器入口（注册表 `system.business_note_auto_publish`）。

    逐账号：探活 → 取 0 篇挂链商品 → 按方式生成（等终态）→ 对生成成功的逐篇发布
    （两篇之间 `interval_seconds`）。

    * 千帆无效 ⇒ 发站内消息 + 跳过该账号（不阻断整轮）；
    * 生成失败 ⇒ **不发布**这一篇（用户口径「对生成成功的笔记进行发布」）；
    * 定向模式（`account_ids` 非空）返回逐条结果；轮转模式返 `None`（与同族一致）。
    """
    from backend.app.services.shop_health_service import check_account_health
    from backend.app.services.notification_service import notify_account_skipped

    targeted = bool(account_ids)
    mode = resolve_generate_mode(params)
    interval = resolve_interval_seconds(params)
    retry_enabled, max_retries, retry_interval = resolve_retry_policy(params)
    results: list[dict[str, Any]] = []
    db = SessionLocal()
    try:
        statement = select(PlatformAccount).where(
            PlatformAccount.user_id == user_id,
            PlatformAccount.platform == "xhs",
            PlatformAccount.deleted_at.is_(None),
        )
        if account_ids:
            statement = statement.where(PlatformAccount.id.in_(list(account_ids)))
        accounts = list(db.scalars(statement).all())

        for account in accounts:
            limit = resolve_account_limit(params, int(account.id))
            # ---- ① 千帆探活（只读）；无效 ⇒ 通知 + 跳过 ----
            try:
                health = check_account_health(db, account)
                db.commit()
            except Exception as exc:  # noqa: BLE001 - 探活异常按「无效」处理（跳过更安全）
                logger.warning("auto-publish: health check failed account=%s: %s", account.id, exc)
                health = {"status": "invalid", "message": f"探活异常：{exc}"}
            if str(health.get("status")) != "active":
                reason = str(health.get("message") or "千帆登录态无效")
                notify_account_skipped(
                    db,
                    user_id=user_id,
                    account_id=int(account.id),
                    account_name=(account.nickname or "").strip() or f"账号 #{account.id}",
                    reason=reason,
                )
                db.commit()
                results.append({"account_id": account.id, "skipped": True, "reason": reason})
                continue

            # ---- ② 取「挂链笔记数为 0」的商品 ----
            try:
                products = select_business_items(db, account, limit=limit)
            except Exception as exc:  # noqa: BLE001 - 平台查不到 ⇒ 本轮该账号跳过（不拖垮其它账号）
                logger.warning("auto-publish: 取商品失败 account=%s: %s", account.id, exc)
                results.append({"account_id": account.id, "skipped": True, "reason": str(exc)})
                continue
            published = 0
            failed = 0
            retried = 0
            for product in products:
                # ---- ③ 生成（按方式，等终态）----
                note_pk = (
                    _generate_with_qianfan(db, account=account, product=product)
                    if mode == MODE_QIANFAN
                    else _generate_with_llm(db, account=account, product=product)
                )
                if note_pk is None:
                    failed += 1
                    continue
                # ---- ④ 生成成功 ⇒ 发布（等终态）----
                outcome = _publish_and_wait(db, user_id=int(account.user_id), note_pk=int(note_pk))
                # ---- ⑤ 发布失败 ⇒ 按重试策略再试（2026-10-07 用户口径）----
                # 只对**发布**失败重试；**生成失败不重试**（生成是另一条链路，重试会
                # 再打一次平台生成接口 —— 用户口径的重试对象是「发布失败」）。
                retries_used = 0
                while not outcome["ok"] and retry_enabled and retries_used < max_retries:
                    retries_used += 1
                    retried += 1
                    logger.warning(
                        "auto-publish: publish failed note_pk=%s (第 %s/%s 次重试): %s",
                        note_pk,
                        retries_used,
                        max_retries,
                        outcome["error"],
                    )
                    if retry_interval:
                        time.sleep(retry_interval)
                    outcome = _publish_and_wait(
                        db, user_id=int(account.user_id), note_pk=int(note_pk)
                    )
                if outcome["ok"]:
                    published += 1
                else:
                    failed += 1
                    logger.warning(
                        "auto-publish: publish failed note_pk=%s: %s", note_pk, outcome["error"]
                    )
                if interval:
                    time.sleep(interval)
            results.append(
                {
                    "account_id": account.id,
                    "limit": limit,
                    "candidates": len(products),
                    "published": published,
                    "failed": failed,
                    "retried": retried,
                }
            )
    except Exception as exc:  # noqa: BLE001 - 调度器调用，必须吞异常（DEF-067 同型）
        logger.error(f"run_business_note_auto_publish failed: {exc}")
    finally:
        db.close()

    if not targeted:
        return None
    return {
        "mode": mode,
        "interval_seconds": interval,
        "retry": {
            "enabled": retry_enabled,
            "max_retries": max_retries,
            "interval_seconds": retry_interval,
        },
        "results": results,
        "published": sum(int(r.get("published") or 0) for r in results),
        "retried": sum(int(r.get("retried") or 0) for r in results),
        "skipped": sum(1 for r in results if r.get("skipped")),
    }
