"""LLM 生成队列（`note_generate_llm`）的入队 / worker / 快照（设计 17 §8，T6）。

## 与一期商品生成队列（`product_note_gen_service`）的分工

| | 一期 `note_generate` | 本期 `note_generate_llm` |
|---|---|---|
| 内容来自 | **千帆后台 AI**（三步链路 + 轮询，外部平台） | **本项目自建薄客户端**（任意 OpenAI 兼容端点，设计 17） |
| 落库 | `notes` + `note_assets` + `note_goods` | 同上 + **`generation_records` / `note_generation_links`**（审计 / 追溯） |
| 护栏 | 记录不拦截（内容出自平台官方 AI） | **命中即失败**（内容出自第三方模型，必须自守） |
| 产物契约 | 平台 `topics[{id,name}]` | 自产 `{title, body, topics}`（无平台 ID） |
| 队列类型 | `xhs_note_gen_item` | `xhs_note_gen_llm_item` |

两队列**并存而非替代**（DEF-044 不适用：产物契约、失败语义、落库表都不同，
合并会丢语义 —— 见 `note_queue_service.TASK_TYPE_GEN_LLM_ITEM` 的注释）。

## 为什么同步段只做门禁 + 入队

生成一篇要调上游模型（实测 3–30s 不等，长文更久）。同步段跑完会把 HTTP 请求挂死，
且批量 20 篇 = 分钟级。⇒ 与发布 / 同步 / 一期生成**同构**：端点只做**确定性门禁**
（失败仍 400/404 结构化）+ 入队即返回，逐篇在后台 worker 跑。

## 一条笔记的落库是**一个事务**

`notes` + `note_topics`（关系）+ `note_assets` + `note_generation_links` +
`generation_records(status=success)` 同生同死 —— 不留半条（§31.4 约定 2 同精神）。

## 失败边界

* **门禁失败**（无默认文本模型 / 模板不存在 / 台账无商品）：**不入队**，
  同步段 400/404 结构化（确定性答案 ⇒ 不用等 worker 才知道）。
* **生成失败**（上游拒绝 / 非 JSON / 护栏拦截）：`generation_records` 落
  `failed` + `error_code` + `error_message` 摘要；**不落 `notes`**；
  队列项落 `failed`，原因在快照 `finished` 给（与一期同出口）。
  用户可在记录页点「重试」（`source=llm_generate_retry`）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models import (
    GenerationRecord,
    NoteGenerationLink,
    PromptModule,
    PromptTemplate,
    Task,
)
from backend.app.models.generation import (
    GEN_STATUS_FAILED,
    GEN_STATUS_QUEUED,
    GEN_STATUS_RUNNING,
    GEN_STATUS_SUCCESS,
    MEDIA_SOURCE_PRODUCT_IMAGE,
)
from backend.app.models.note import (
    NOTE_TYPE_LOCAL_GENERATED,
    PUBLISH_STATUS_PENDING,
    Note,
    NoteAsset,
    set_note_publish_status,
)
from backend.app.models.shop_product import ShopProduct
from backend.app.models.user import User
from backend.app.services import llm_gateway_client, note_queue_service
from backend.app.services.business_provider_keys import BUSINESS_KEY_GENERATE_NOTE
from backend.app.services.llm_gateway_client import LLMGatewayError
from backend.app.services.note_gen_llm_service import (
    GeneratedNote,
    NoteGenerationError,
    generate_full_note,
)
from backend.app.services.note_topic_service import apply_sync_topics
from backend.app.services.shop_products_service import load_product_dict_from_ledger

logger = logging.getLogger(__name__)

#: 本地生成笔记的 ID 前缀（与一期生成一致：平台 ID 发布后才回写）。
_LOCAL_NOTE_PREFIX = "local_"

#: 批量入队单次上限（与一期生成 / 发布 / 同步同值 20）。
GEN_LLM_BATCH_MAX_ITEMS = 20

#: 队列空闲轮询间隔（秒）。唤醒信号只是**提示**：`wait(timeout=…)` 是兜底，
#: 任何一次 `set` 丢失最坏只晚这么久发现新任务，不会永久停摆（DEF-048）。
GEN_LLM_IDLE_POLL_SECONDS = 5.0

#: worker / 看门狗线程名（探针与重建都认它；DEF-069：存活判据只能是线程在不在）。
GEN_LLM_WORKER_THREAD_NAME = "note-gen-llm-worker"
GEN_LLM_WATCHDOG_THREAD_NAME = "note-gen-llm-watchdog"

#: 看门狗巡检间隔 / 重建失败指数退避上限（与其余 worker 同值）。
GEN_LLM_WATCHDOG_INTERVAL_SECONDS = 30.0
GEN_LLM_WATCHDOG_MAX_BACKOFF_SECONDS = 600.0

#: 队列持久化（同 §35.9 同型）：真相源是 `tasks` 表里的 `xhs_note_gen_llm_item` 行，
#: 内存里只留一个唤醒信号 —— 进程重启 = 什么都不丢（未跑的项还是 `pending` 行）。
_gen_llm_wakeup = threading.Event()
_gen_llm_queue_lock = threading.Lock()
_gen_llm_worker: Optional[threading.Thread] = None
#: 收尾 / 测试专用出口（置位 + set wakeup ⇒ 不再认领新任务即退；生产不置位）。
_gen_llm_worker_stop = threading.Event()
_gen_llm_watchdog_stop = threading.Event()

#: 后台线程自建 Session 的工厂（测试注入临时库的 sessionmaker）。
_session_factory = SessionLocal


# --------------------------------------------------------------------------- #
# 门禁（同步段与 worker 重跑共用唯一实现）
# --------------------------------------------------------------------------- #
def _resolve_template(db: Session, user_id: int, template_id: Optional[int]) -> Optional[PromptTemplate]:
    """取模板（**归属校验**：不是本人的模板 ⇒ 视为不存在，DEF-066）。

    `template_id` 为 `None`/`0` ⇒ 返回 `None`（无模板 ⇒ 走内置兜底骨架，
    设计 17 里模板是可选的）。
    """
    if not template_id:
        return None
    template = db.get(PromptTemplate, int(template_id))
    if template is None or int(template.user_id) != int(user_id):
        return None
    return template


def _load_template_modules(db: Session, user_id: int, template: Optional[PromptTemplate]) -> list[PromptModule]:
    """模板引用的模块（按 `module_ids` 顺序取，**过滤停用 + 非本人 + 已删**）。

    顺序即拼接序（§5.1）；`render_modules` 内部还会按 `sort_order` 再排 —— 两处
    顺序规则不冲突（`module_ids` 是模板作者的声明序，`sort_order` 是模块自己的
    全局序，取后者为准）。缺模块**不报错**：模板引用了被删模块只是少一段
    （渲染器不做业务过滤，过滤归调用方）。
    """
    if template is None:
        return []
    raw_ids = template.module_ids if isinstance(template.module_ids, (list, tuple)) else []
    ids = [int(v) for v in raw_ids if isinstance(v, int) or (isinstance(v, str) and str(v).isdigit())]
    if not ids:
        return []
    rows = db.scalars(
        select(PromptModule).where(
            PromptModule.user_id == user_id,
            PromptModule.id.in_(ids),
            PromptModule.enabled.is_(True),
        )
    ).all()
    order = {module_id: index for index, module_id in enumerate(ids)}
    return sorted(rows, key=lambda row: order.get(int(row.id), len(order)))


def _product_row_for_gate(db: Session, user_id: int, shop_product_id: int) -> ShopProduct:
    """取台账商品行（**归属校验**），不存在 ⇒ `NoteGenerationError`。

    ⚠️ 与一期生成（`product_note_gen_service._product_row`）的差别：一期按
    `item_id` + 账号查（要打平台取实时价），本期按**本地主键** + 用户查
    —— LLM 生成的事实来源是**本地台账**（§5.1「商品白名单字段注入」的输入），
    **零平台请求**（生成链路完全不碰小红书 / 千帆，设计 17 §1 定位）。
    """
    row = db.get(ShopProduct, int(shop_product_id))
    if row is None or int(row.user_id) != int(user_id):
        raise NoteGenerationError(
            f"台账里找不到商品记录（id={shop_product_id}）",
            code="LLM_PRODUCT_NOT_FOUND",
        )
    return row


def gate_for_llm_generation(
    db: Session, user_id: int, *, shop_product_id: int, template_id: Optional[int] = None
) -> tuple[ShopProduct, Optional[PromptTemplate], list[PromptModule]]:
    """LLM 生成门禁：**商品行 → 模板 → 模块**（顺序即失败优先级）。

    三项都是**确定性答案**（本地库可判）⇒ 不过即抛，同步段 400/404 结构化，
    不入队（设计 17 §13 T6「门禁失败不入队」）。

    worker 重跑时**再走一遍**：排队期间模板可能被删 / 商品行可能被清理
    （现取现判，不缓存 —— 缓存 = 第二套真相源）。

    ⚠️ **网关门禁不在这里**，由 `gate_llm_gateway_ready()` 单独做 —— 入队三入口
    （单篇 / 批量 / 重试）都要过它，但它需要 `User` 实例（`resolve_business_provider`
    的入参），而本函数只吃 `user_id`。**判据就是「该业务点的映射可用」**：
    §7.4 的 `LLM_GATEWAY_NOT_CONFIGURED` 在 v2.0 落到「`generate_note` 这个业务点
    有没有配服务商映射」上（`gate_llm_gateway_ready()` 调 `resolve_business_provider`，
    与 worker 实际调用**同一入口**）。
    """
    row = _product_row_for_gate(db, user_id, shop_product_id)
    template = _resolve_template(db, user_id, template_id)
    if template_id and template is None:
        raise NoteGenerationError(
            f"提示词模板不存在（id={template_id}），可能已被删除。",
            code="LLM_TEMPLATE_NOT_FOUND",
        )
    modules = _load_template_modules(db, user_id, template)
    return row, template, modules


def gate_llm_gateway_ready(db: Session, user_id: int) -> User:
    """网关门禁（§7.4 的 `LLM_GATEWAY_NOT_CONFIGURED`，v1.7 语义）。

    确定性答案（只查本地表，**不发探活请求**）⇒ 不通过即 400，不入队。
    返回 `User` 以便调用方复用（省一次 `db.get`）。

    ⚠️ **不做 `healthy` 探测**：§7.4 原文提到「`healthy=false` 时不拦但触发一次
    探测」是 v1.3 对**外挂 New API** 的口径 —— v1.7 去外挂后没有「网关健康状态」
    这个概念（直连任意 OpenAI 兼容端点）。硬做探活 = 同步段替用户发一次真实
    上游请求（费用 + 延迟），且与「入队即返回（毫秒级）」的定位冲突。
    真实可达性由 worker 调用的那次请求如实反映（失败落 `LLM_GATEWAY_CALL_FAILED`）。
    """
    user = db.get(User, int(user_id))
    if user is None:
        raise NoteGenerationError(f"用户不存在（id={user_id}）", code="LLM_ITEM_INVALID")
    try:
        # v2.0（设计 17 §13.2.4）：入队门禁也走**业务点映射**——
        # 与 worker 实际调用同一入口，门禁过了就不会在 worker 里才发现没配。
        llm_gateway_client.resolve_business_provider(db, user, BUSINESS_KEY_GENERATE_NOTE)
    except LLMGatewayError as error:
        raise NoteGenerationError(error.message, code="LLM_GATEWAY_NOT_CONFIGURED") from error
    return user


# --------------------------------------------------------------------------- #
# 入队（端点调；同步段只做门禁 + 入队）
# --------------------------------------------------------------------------- #
#: 幂等键的组成（设计 17 §7.4）：同商品 + 同模板 + 同上游配置 = 同一次生成意图。
#: 落进队列项 `payload`，供 worker 与重试读取。
def _llm_payload(
    *,
    shop_product_id: int,
    account_id: Optional[int],
    source: str,
    template_id: Optional[int] = None,
    gateway_config_id: Optional[int] = None,
    model_name: str = "",
    media_source: str = MEDIA_SOURCE_PRODUCT_IMAGE,
    media_options: Optional[dict] = None,
    record_id: Optional[int] = None,
    item_name: str = "",
) -> dict:
    """队列项行体（**入队即完整落库**：展示与执行所需的全在这里，不回头补查）。

    ⚠️ `item_id` 用**台账主键的字符串形态**：`enqueue_item` 的幂等判据在
    非笔记绑定队列上按 `item_id` 去重（`item_ref_of`），而本队列的商品主键是
    本地 `shop_products.id` —— 直接用它，不再塞平台 `item_id`（那需要多查一次
    台账，且平台 ID 可能为空）。
    """
    payload: dict[str, Any] = {
        "account_id": int(account_id) if account_id is not None else None,
        "source": str(source),
        "shop_product_id": int(shop_product_id),
        "item_id": str(int(shop_product_id)),
    }
    if item_name:
        payload["item_name"] = str(item_name).strip()
    if template_id:
        payload["template_id"] = int(template_id)
    if gateway_config_id:
        payload["gateway_config_id"] = int(gateway_config_id)
    if model_name:
        payload["model_name"] = str(model_name)
    payload["media_source"] = str(media_source or MEDIA_SOURCE_PRODUCT_IMAGE)
    if media_options:
        payload["media_options"] = dict(media_options)
    if record_id:
        payload["record_id"] = int(record_id)
    return payload


def _enqueue_llm_item(
    db: Session,
    *,
    user_id: int,
    row: ShopProduct,
    account_id: Optional[int],
    source: str,
    batch: Optional[note_queue_service.BatchRequest],
    template_id: Optional[int] = None,
    gateway_config_id: Optional[int] = None,
    model_name: str = "",
    media_source: str = MEDIA_SOURCE_PRODUCT_IMAGE,
) -> tuple[Task, bool]:
    """入队一条 LLM 生成项（内部用：`enqueue_item` 直插 + 行体全部字段）。

    为什么不用 `enqueue_item(..., item_id=…, item_name=…)` 的通用形参：本队列
    行体字段比一期多（模板 / 上游 / 媒体来源 / 审计 `record_id`），而
    `enqueue_item` 只透传 `item_id` / `item_name` / `note_title`。若把额外字段
    拼进 `item_name`（或事后 update）就等于**同一语义两处维护**（DEF-044）。
    这里复用 `enqueue_item`（幂等 + 批次懒创建 + 索引回填全在它内部，**唯一
    实现**），只在入队后补写额外行体字段 —— 补写在 `flush` 之前，同一次事务。
    """
    payload_extra = _llm_payload(
        shop_product_id=int(row.id),
        account_id=account_id,
        source=source,
        template_id=template_id,
        gateway_config_id=gateway_config_id,
        model_name=model_name,
        media_source=media_source,
        item_name=str(getattr(row, "item_title", "") or ""),
    )
    task_row, created = note_queue_service.enqueue_item(
        db,
        user_id=user_id,
        item_task_type=note_queue_service.TASK_TYPE_GEN_LLM_ITEM,
        account_id=account_id,
        item_id=payload_extra["item_id"],
        item_name=payload_extra.get("item_name", ""),
        source=source,
        batch=batch,
    )
    if created:
        # 补写额外行体（⚠️ 必须赋新 dict：SQLAlchemy JSON 列原地改不标脏，老坑）。
        merged = dict(task_row.payload or {})
        for key, value in payload_extra.items():
            if key == "source":
                continue  # `enqueue_item` 已写，别覆盖它自己那一步
            merged[key] = value
        task_row.payload = merged
        db.flush()
    return task_row, created


def start_llm_generate_note(
    db: Session,
    user_id: int,
    *,
    shop_product_id: int,
    account_id: Optional[int] = None,
    template_id: Optional[int] = None,
    template_name: str = "",
    gateway_config_id: Optional[int] = None,
    model_name: str = "",
    media_source: str = MEDIA_SOURCE_PRODUCT_IMAGE,
    source: str = note_queue_service.SOURCE_PRODUCT_NOTE_GENERATE_LLM_SINGLE,
) -> dict:
    """单篇 LLM 生成**入队即返回**（设计 17 §7.4）。

    同步段只做门禁（:func:`gate_for_llm_generation`，确定性答案 ⇒ 不过仍
    400/404 结构化）+ 入队 + 写一条 `generation_records(status=queued)` 审计行。
    返回 `{ok, status:"queued", record_id, shop_product_id, already_queued?}`；
    **终态与原因不在这里** —— 走 `GET /xhs/product-notes/generate-llm-queue`。

    幂等：同用户未终态队列里已有同商品（同 `item_id`）⇒ `already_queued: true`，
    **不新建审计行**（新建会造出「两条 queued 记录对一条队列项」的假审计）。
    """
    gate_llm_gateway_ready(db, user_id)
    row, template, _modules = gate_for_llm_generation(
        db, user_id, shop_product_id=shop_product_id, template_id=template_id
    )
    batch = note_queue_service.BatchRequest(
        user_id=user_id,
        item_task_type=note_queue_service.TASK_TYPE_GEN_LLM_ITEM,
        source=source,
    )
    task_row, created = _enqueue_llm_item(
        db,
        user_id=user_id,
        row=row,
        account_id=account_id,
        source=source,
        batch=batch,
        template_id=(int(template.id) if template is not None else None),
        gateway_config_id=gateway_config_id,
        model_name=model_name,
        media_source=media_source,
    )
    record_id = 0
    if not created:
        # 幂等命中：队列项已在，审计行也已存在（由首次入队创建）。
        # ⚠️ 必须把已有 `record_id` 回填给调用方 —— 返回 0 会让前端「已在队列中」
        # 的提示拿不到可跳转的审计行（且与首次入队的回执形状不一致）。
        record_id = int((task_row.payload or {}).get("record_id") or 0)
    if created:
        record = GenerationRecord(
            user_id=user_id,
            task_id=int(task_row.id),
            shop_product_id=int(row.id),
            template_id=(int(template.id) if template is not None else None),
            gateway_config_id=(int(gateway_config_id) if gateway_config_id else None),
            model_name=str(model_name or ""),
            media_source=str(media_source or MEDIA_SOURCE_PRODUCT_IMAGE),
            status=GEN_STATUS_QUEUED,
        )
        db.add(record)
        db.flush()
        record_id = int(record.id)
        # 回填队列项：worker 按 `record_id` 更新同一条审计（不为一次生成建两条记录）。
        merged = dict(task_row.payload or {})
        merged["record_id"] = record_id
        if template_name:
            merged["template_name"] = str(template_name)
        task_row.payload = merged
        db.flush()
    db.commit()
    # ⚠️ 唤醒放在**提交之后**：worker 自建 Session，看不到未提交的行。
    ensure_llm_generate_worker()
    logger.info(
        "llm note generate enqueued (product=%s, template=%s, created=%s)",
        shop_product_id,
        template_id,
        created,
    )
    return {
        "ok": True,
        "status": "queued",
        "shop_product_id": int(shop_product_id),
        "record_id": record_id,
        **({} if created else {"already_queued": True}),
    }


def start_llm_batch_generate(
    db: Session,
    user_id: int,
    *,
    shop_product_ids: list[int],
    account_id: Optional[int] = None,
    template_id: Optional[int] = None,
    template_name: str = "",
    gateway_config_id: Optional[int] = None,
    model_name: str = "",
    media_source: str = MEDIA_SOURCE_PRODUCT_IMAGE,
    max_items: int = GEN_LLM_BATCH_MAX_ITEMS,
) -> dict:
    """批量 LLM 生成入队（设计 17 §7.4）：逐项过门禁，通过项按序入队。

    **单项失败不阻断**（门禁失败项也回执，不抛）—— 与一期批量生成同语义。
    返回 `{total, started, failed, results:[{shop_product_id, ok, error?, code?}]}`。
    一次批量 = 一个批次（懒创建：全部门禁不过 ⇒ 不留空批次行）。
    """
    # ⚠️ 网关门禁在**循环外**判一次并直接抛出：网关可用性是**账号级**属性，
    # 不是商品级 —— 逐项判会重复查表，且失败时给 20 条一模一样的 `results` 回执，
    # 用户看到的是「20 个商品都不行」而不是「网关没配」。批量入口与单篇入口在
    # 「网关没配」这件事上给同一种答案（400 + `LLM_GATEWAY_NOT_CONFIGURED`）。
    gate_llm_gateway_ready(db, user_id)
    cleaned: list[int] = []
    for value in shop_product_ids:
        try:
            cleaned.append(int(value))
        except (TypeError, ValueError):
            continue
    distinct = list(dict.fromkeys(cleaned))[: max(1, int(max_items))]

    batch = note_queue_service.BatchRequest(
        user_id=user_id,
        item_task_type=note_queue_service.TASK_TYPE_GEN_LLM_ITEM,
        source=note_queue_service.SOURCE_PRODUCT_NOTE_GENERATE_LLM_BATCH,
    )
    results: list[dict] = []
    for product_pk in distinct:
        try:
            row, template, _modules = gate_for_llm_generation(
                db, user_id, shop_product_id=product_pk, template_id=template_id
            )
        except NoteGenerationError as error:
            results.append(
                {
                    "shop_product_id": product_pk,
                    "ok": False,
                    "error": error.message,
                    "code": error.code,
                }
            )
            continue
        try:
            task_row, created = _enqueue_llm_item(
                db,
                user_id=user_id,
                row=row,
                account_id=account_id,
                source=note_queue_service.SOURCE_PRODUCT_NOTE_GENERATE_LLM_BATCH,
                batch=batch,
                template_id=(int(template.id) if template is not None else None),
                gateway_config_id=gateway_config_id,
                model_name=model_name,
                media_source=media_source,
            )
        except BaseException as error:  # noqa: BLE001 - 单项失败不阻断（一口锅不能被一颗坏豆子毁掉）
            db.rollback()
            logger.exception("llm batch generate enqueue failed (product=%s)", product_pk)
            results.append(
                {
                    "shop_product_id": product_pk,
                    "ok": False,
                    "error": f"{type(error).__name__}: {error}",
                    "code": "LLM_ENQUEUE_FAILED",
                }
            )
            continue
        if created:
            record = GenerationRecord(
                user_id=user_id,
                task_id=int(task_row.id),
                shop_product_id=int(row.id),
                template_id=(int(template.id) if template is not None else None),
                gateway_config_id=(int(gateway_config_id) if gateway_config_id else None),
                model_name=str(model_name or ""),
                media_source=str(media_source or MEDIA_SOURCE_PRODUCT_IMAGE),
                status=GEN_STATUS_QUEUED,
            )
            db.add(record)
            db.flush()
            merged = dict(task_row.payload or {})
            merged["record_id"] = int(record.id)
            if template_name:
                merged["template_name"] = str(template_name)
            task_row.payload = merged
            db.flush()
        results.append(
            {
                "shop_product_id": product_pk,
                "ok": True,
                "status": "queued",
                **({} if created else {"already_queued": True}),
            }
        )
    db.commit()
    ensure_llm_generate_worker()  # 同上：提交后才唤醒
    started = sum(1 for entry in results if entry.get("ok"))
    logger.info("llm note batch generate enqueued (total=%s, started=%s)", len(distinct), started)
    return {
        "total": len(distinct),
        "started": started,
        "failed": len(distinct) - started,
        "results": results,
    }


def start_llm_retry(
    db: Session,
    user_id: int,
    *,
    record_id: int,
    account_id: Optional[int] = None,
) -> dict:
    """重试一条**失败**的生成记录（设计 17 §7.4 `POST …/generate-llm/{id}/retry`）。

    语义：**重试 = 新队列项 + 原记录复位回 `queued`**（不新建审计行 —— 用户视角
    是「这次生成再试一次」，不是两次生成）。`generation_records` 的 `task_id`
    指向最新一次尝试，历史尝试数由队列记录页的批次子行体现。

    只有 `status=failed` 可重试（`success` 重试会覆盖已完成产物；`running` 正在跑）
    ⇒ 否则抛 `LLM_RECORD_NOT_RETRYABLE`（400）。
    """
    record = db.get(GenerationRecord, int(record_id))
    if record is None or int(record.user_id) != int(user_id):
        raise NoteGenerationError(f"生成记录不存在（id={record_id}）", code="LLM_RECORD_NOT_FOUND")
    if record.status != GEN_STATUS_FAILED:
        raise NoteGenerationError(
            f"只有失败的生成记录可以重试（当前状态：{record.status}）。",
            code="LLM_RECORD_NOT_RETRYABLE",
        )
    gate_llm_gateway_ready(db, user_id)
    row, template, _modules = gate_for_llm_generation(
        db,
        user_id,
        shop_product_id=int(record.shop_product_id or 0),
        template_id=record.template_id,
    )
    batch = note_queue_service.BatchRequest(
        user_id=user_id,
        item_task_type=note_queue_service.TASK_TYPE_GEN_LLM_ITEM,
        source=note_queue_service.SOURCE_LLM_GENERATE_RETRY,
    )
    task_row, created = _enqueue_llm_item(
        db,
        user_id=user_id,
        row=row,
        account_id=account_id,
        source=note_queue_service.SOURCE_LLM_GENERATE_RETRY,
        batch=batch,
        template_id=(int(template.id) if template is not None else None),
        gateway_config_id=record.gateway_config_id,
        model_name=str(record.model_name or ""),
        media_source=str(record.media_source or MEDIA_SOURCE_PRODUCT_IMAGE),
    )
    if created:
        # 复位原记录（同一行复用：审计链不断，历史尝试由批次子行承载）。
        record.status = GEN_STATUS_QUEUED
        record.task_id = int(task_row.id)
        record.error_code = None
        record.error_message = None
        merged = dict(task_row.payload or {})
        merged["record_id"] = int(record.id)
        task_row.payload = merged
        db.flush()
    db.commit()
    ensure_llm_generate_worker()
    return {
        "ok": True,
        "status": "queued",
        "record_id": int(record.id),
        **({} if created else {"already_queued": True}),
    }


# --------------------------------------------------------------------------- #
# 落库（一次生成 = 一个事务）
# --------------------------------------------------------------------------- #
def _local_note_id() -> str:
    return f"{_LOCAL_NOTE_PREFIX}{uuid.uuid4().hex[:12]}"


def _media_assets_for(row: ShopProduct, media_source: str) -> list[dict]:
    """按 `media_source` 决定笔记媒体（设计 17 §3.6 / §7.4）。

    ⚠️ **T7（媒体生成）尚未落地**：本期只有 `product_image`（商品图直链，
    复刻一期生成的做法）可用；`ai_generate` / `material` / `reference_image`
    需要 T3 素材库与 T7 图像生成 —— 这里**照实返回空列表**（不造假图片），
    由 worker 记为「媒体待补」，不阻塞笔记落库（正文才是本队列的产物）。
    """
    if media_source != MEDIA_SOURCE_PRODUCT_IMAGE:
        return []
    try:
        urls = json.loads(row.images_json or "[]")
    except ValueError:
        urls = []
    urls = [str(url) for url in urls if isinstance(url, str) and url.strip()][:6]
    return [
        {"asset_type": "image", "url": url, "local_path": "", "sort_order": index}
        for index, url in enumerate(urls)
    ]


def _persist_generated_note(
    db: Session,
    *,
    row: ShopProduct,
    account_id: Optional[int],
    generated: GeneratedNote,
    record: GenerationRecord,
    media_source: str,
) -> dict[str, Any]:
    """把生成产物落库（**调用方持事务**；本函数只 `flush`，不 `commit`）。

    落五处：`notes` + `note_topics`（经 `apply_sync_topics`，**唯一话题入口**）
    + `note_assets` + `note_generation_links`（本地主键级关系，§3.7）
    + 回写 `generation_records`（`status=success` + 标题 / 正文 / 话题 + 耗时）。

    护栏**已在上游拦截**（`generate_full_note` 返回 `ok=False` 时不会走到这里）
    ⇒ 本函数只处理「通过」的产物。
    """
    note = Note(
        user_id=row.user_id,
        platform_account_id=account_id,        platform="xhs",
        note_id=_local_note_id(),
        title=generated.title,
        content=generated.body,
        author_name="",
        note_type=NOTE_TYPE_LOCAL_GENERATED,
        raw_json={
            "guardrails": generated.guardrails,
            "generation": {
                "record_id": int(record.id),
                "model_name": generated.model_name,
                "latency_ms": generated.latency_ms,
                "media_source": media_source,
            },
            "shop_product_id": int(row.id),
        },
    )
    # 状态与 `published` 投影**一次写入**（唯一入口）：生成 ⇒ 待发布。
    # 生成链路到此为止 —— **不调用发布链路的任何一步**（§32.2.2）。
    set_note_publish_status(note, PUBLISH_STATUS_PENDING)
    db.add(note)
    db.flush()  # 先拿 note.id（关系 / 关系表的外键）

    # §34：话题落 `topics` + `note_topics`（唯一实现）。LLM 产物无平台话题 ID
    # ⇒ 不传 `platform_ids`（缺值留空，不造假）。
    apply_sync_topics(db, note, generated.topics)

    for entry in _media_assets_for(row, media_source):
        db.add(
            NoteAsset(
                note_id=note.id,
                asset_type=entry["asset_type"],
                url=entry["url"],
                local_path=entry["local_path"],
                sort_order=entry["sort_order"],
            )
        )

    # 本地主键级「生成笔记 ↔ 商品」关系（§3.7）。与一期 `note_goods`（平台 ID 级）
    # 并存：两者维度不同，不是两份实现（见 `NoteGenerationLink` docstring）。
    db.add(
        NoteGenerationLink(
            user_id=note.user_id,
            note_pk=int(note.id),
            shop_product_id=int(row.id),
        )
    )

    record.status = GEN_STATUS_SUCCESS
    record.title = generated.title
    record.body = generated.body
    record.topics_json = json.dumps(generated.topics, ensure_ascii=False)
    record.model_name = generated.model_name or record.model_name
    record.error_code = None
    record.error_message = None
    record.ai_generated_at = shanghai_now()
    db.flush()

    return {
        "note_id": note.note_id,
        "note_pk": int(note.id),
        "title": generated.title,
        "topics": list(generated.topics),
        "model_name": generated.model_name,
        "latency_ms": generated.latency_ms,
        "media_source": media_source,
        "media_count": len(_media_assets_for(row, media_source)),
    }


def _load_record(db: Session, item: Task, user_id: int) -> Optional[GenerationRecord]:
    """按队列项行体里的 `record_id` 取审计行（**归属校验**，防脏行体改别人的记录）。"""
    payload = item.payload if isinstance(item.payload, dict) else {}
    raw_id = payload.get("record_id")
    try:
        record_id = int(raw_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not record_id:
        return None
    record = db.get(GenerationRecord, record_id)
    if record is None or int(record.user_id) != int(user_id):
        return None
    return record


def _mark_record_failed(
    db: Session,
    *,
    item: Task,
    user_id: int,
    code: str,
    message: str,
) -> None:
    """把审计行写成 `failed`（`error_message` 只记**摘要**，供应商原文不落）。

    `status` 只在 `queued` / `running` 时降为 `failed` —— 已 `success` 的记录不被
    一个迟到的失败覆盖（那意味着产物已经落库，失败是后续某步的噪声）。
    """
    record = _load_record(db, item, user_id)
    if record is None:
        return
    if record.status == GEN_STATUS_SUCCESS:
        return
    record.status = GEN_STATUS_FAILED
    record.error_code = str(code or "LLM_GENERATE_FAILED")[:64]
    record.error_message = str(message or "")[:2000]


def _run_one_llm_generate(item_id: int) -> None:
    """跑一条 LLM 生成项（按 `tasks.id`）：自建 Session；任何漏网异常都不许卡队列。

    安全网不条件性（DEF-048）：**任何**异常路径都必须把这条写成终态 —— 否则它
    永远挂在 `running` 上（进度卡「生成中」永远转圈）。异常分支**先 `db.rollback()`
    再记失败**（失败事务留着回滚状态，不回滚连记账动作本身也会炸）。

    模型上下文**现取**（`resolve_business_provider`）：排队期间用户可能刚配上
    默认文本模型 ⇒ 不该因为入队时没有就永久失败（与一期「凭据现取」同精神）。
    """
    db = _session_factory()
    try:
        item = db.get(Task, item_id)
        if item is None:
            return  # 行已被清理任务删掉 / 不存在：无事可做
        payload = item.payload if isinstance(item.payload, dict) else {}
        try:
            shop_product_id = int(payload.get("shop_product_id") or 0)
        except (TypeError, ValueError):
            shop_product_id = 0
        template_id = payload.get("template_id")
        media_source = str(payload.get("media_source") or MEDIA_SOURCE_PRODUCT_IMAGE)
        account_id = note_queue_service.account_id_of(item)
        user_id = int(item.user_id)
        if not shop_product_id:
            _mark_record_failed(
                db, item=item, user_id=user_id, code="LLM_ITEM_INVALID", message="队列项缺少商品信息"
            )
            note_queue_service.finish_item(
                db, item, ok=False, reason="队列项缺少商品信息", code="LLM_ITEM_INVALID"
            )
            return

        # ---- ① 门禁（现取现判：模板 / 商品可能已被删） ----
        try:
            row, template, modules = gate_for_llm_generation(
                db, user_id, shop_product_id=shop_product_id, template_id=template_id
            )
        except NoteGenerationError as error:
            _mark_record_failed(
                db, item=item, user_id=user_id, code=error.code, message=error.message
            )
            note_queue_service.finish_item(db, item, ok=False, reason=error.message, code=error.code)
            return

        # ---- ② 上游模型上下文（默认文本模型；缺 ⇒ 可归因失败） ----
        user = db.get(User, user_id)
        if user is None:
            # 归属行没了（用户被删）：可归因失败，不留半条。
            _mark_record_failed(
                db, item=item, user_id=user_id, code="LLM_ITEM_INVALID", message="队列项归属用户不存在"
            )
            note_queue_service.finish_item(
                db, item, ok=False, reason="队列项归属用户不存在", code="LLM_ITEM_INVALID"
            )
            return
        try:
            # v2.0（设计 17 §13.2.4）：按**业务点固定 key** 取上下文，
            # 不再走一期 `model_configs`（已退场）。
            # ⚠️ 返回值是 **`ResolvedModelContext` 标量载体**，不是 `(config, key)`
            #    二元组 —— 不许 `a, b = ...` 解包（会 `TypeError` 并把每次生成都
            #    打成 `LLM_GENERATE_FAILED`，实测踩过）。其余调用点
            #    （`api/ai.py` / `auto_ops_service.py`）一律按属性读，此处对齐。
            context = llm_gateway_client.resolve_business_provider(
                db, user, BUSINESS_KEY_GENERATE_NOTE
            )
        except LLMGatewayError as error:
            # 基类一并接住：映射未配 / 类型不匹配 / 服务商停用 都是
            # 「可归因失败」，各自带结构化 code，都要落进队列项终态。
            _mark_record_failed(
                db, item=item, user_id=user_id, code=error.code, message=error.message
            )
            note_queue_service.finish_item(db, item, ok=False, reason=error.message, code=error.code)
            return

        # 队列项行体里的模型名是**入队时**的快照；实际用哪个以上游解析结果为准
        # （用户可能换了默认模型）⇒ 记回行体，快照页显示的才是真用过的那个。
        used_model = str(getattr(context, "model_name", "") or "")

        # ---- ③ 生成（护栏在内部；记账在 _complete 内部） ----
        skeleton = template.prompt_skeleton if template is not None else ""
        record = _load_record(db, item, user_id)
        try:
            generated = generate_full_note(
                db=db,
                user_id=user_id,
                product=load_product_dict_from_ledger(row),
                model_config=context,
                api_key=context.api_key,
                skeleton=skeleton,
                modules=modules,
            )
        except NoteGenerationError as error:
            _mark_record_failed(
                db, item=item, user_id=user_id, code=error.code, message=error.message
            )
            db.flush()
            note_queue_service.finish_item(db, item, ok=False, reason=error.message, code=error.code)
            return

        # ---- ④ 护栏拦截 ⇒ 失败（**不落库半条笔记**，§4.7） ----
        if not generated.ok:
            message = generated.block_reason or "未通过内容护栏"
            _mark_record_failed(
                db, item=item, user_id=user_id, code="LLM_GUARDRAIL_BLOCKED", message=message
            )
            db.flush()
            note_queue_service.finish_item(
                db, item, ok=False, reason=message, code="LLM_GUARDRAIL_BLOCKED"
            )
            return

        if record is None:
            # 行体缺 `record_id`（脏行 / 早期存量）：补一条审计行，别让成功调用无据可查。
            record = GenerationRecord(
                user_id=user_id,
                task_id=int(item.id),
                shop_product_id=int(row.id),
                template_id=(int(template.id) if template is not None else None),
                model_name=used_model,
                media_source=media_source,
                status=GEN_STATUS_RUNNING,
            )
            db.add(record)
            db.flush()

        # ---- ⑤ 落库（一个事务：笔记 + 话题 + 媒体 + 关系 + 审计） ----
        # ⚠️ `notes.platform_account_id` 是 **NOT NULL**：以**台账行**为权威归属
        # （ADR-021：商品行必属某平台账号），行体 `account_id` 只作兜底 ——
        # 早期/脏行体可能是 `None`，直接透传会撞 NOT NULL 约束（实测踩过一次）。
        owner_account_id = int(row.platform_account_id or account_id or 0) or None
        if not owner_account_id:
            _mark_record_failed(
                db,
                item=item,
                user_id=user_id,
                code="LLM_ITEM_INVALID",
                message="无法确定生成笔记的归属账号（台账行与队列项都缺 platform_account_id）",
            )
            note_queue_service.finish_item(
                db,
                item,
                ok=False,
                reason="无法确定归属账号",
                code="LLM_ITEM_INVALID",
            )
            return
        outcome = _persist_generated_note(
            db,
            row=row,
            account_id=owner_account_id,
            generated=generated,
            record=record,
            media_source=media_source,
        )
        db.commit()
        note_queue_service.finish_item(
            db,
            item,
            ok=True,
            # 成功后把「生成了哪篇笔记」补进行体：快照 / 详情抽屉直接显示，不回头反查。
            extra={
                "note_id": outcome["note_id"],
                "note_title": outcome["title"],
                "record_id": int(record.id),
                "model_name": outcome["model_name"],
            },
        )
    except BaseException as error:  # noqa: BLE001 - 兜底安全网：SystemExit 也不许卡队列
        logger.warning("llm generate worker crashed (task=%s): %s", item_id, error, exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 - 恢复也失败只能留痕，不能再抛
            logger.exception("llm generate rollback failed (task=%s)", item_id)
        try:
            item = db.get(Task, item_id)
            if item is not None:
                _mark_record_failed(
                    db,
                    item=item,
                    user_id=int(item.user_id),
                    code="LLM_GENERATE_FAILED",
                    message=f"{type(error).__name__}: {error}",
                )
                note_queue_service.finish_item(
                    db,
                    item,
                    ok=False,
                    reason=f"{type(error).__name__}: {error}",
                    code="LLM_GENERATE_FAILED",
                )
        except Exception:  # noqa: BLE001 - 连记账都失败：留痕，行留 running 由看门狗复位捡回
            logger.exception("llm generate failed to record failure (task=%s)", item_id)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# worker 五件套（与其余 worker 同型：单线程 FIFO + 看门狗 + 停止出口）
# --------------------------------------------------------------------------- #
def ensure_llm_generate_worker() -> None:
    """确保有一条活着的 worker（惰性起；已活则只唤醒它）。

    公开（不是 `_` 前缀）：**启动恢复**也要用它 —— `main.py::lifespan` 复位完
    中断行后立刻唤醒，把上次进程留下的 `pending` 项接着跑。
    """
    with _gen_llm_queue_lock:
        if _gen_llm_worker is None or not _gen_llm_worker.is_alive():
            _spawn_llm_generate_worker()
    _gen_llm_wakeup.set()


def _llm_gen_worker_loop() -> None:
    """DB 队列 worker：醒来 → 认领一条 → 跑一条 → 再认领 …… 认不到就回去等。

    ⚠️ DEF-069 同型缺口（本模块从一开始就补齐四件）：篇内 `except BaseException`
    网、循环外层第二层网、看门狗（`start_llm_generate_worker_watchdog`）、停止出口
    （`stop_llm_generate_worker`）。「不丢正在排队的项」由**库**保证。
    """
    while not _gen_llm_worker_stop.is_set():
        _gen_llm_wakeup.wait(timeout=GEN_LLM_IDLE_POLL_SECONDS)
        _gen_llm_wakeup.clear()
        while not _gen_llm_worker_stop.is_set():
            item_id = _claim_one_llm_item()
            if item_id is None:
                break
            try:
                _run_one_llm_generate(item_id)
            except BaseException as error:  # noqa: BLE001 - 线程不死（DEF-069）
                logger.error(
                    "llm gen worker: run escaped the inner net (task=%s): %s",
                    item_id,
                    error,
                    exc_info=True,
                )


def _claim_one_llm_item() -> Optional[int]:
    """认领一条待生成项，返回其 `tasks.id`（无活可干 / 认领出错 ⇒ `None`）。

    认领用**独立 Session**（`claim_next_item` 内部提交换状态，用完即关）；
    认领期异常在这里收住 —— 认领失败只是这一轮空转（外层 `wait(timeout=…)`
    天然退避），不许把 worker 带走。
    """
    db = _session_factory()
    try:
        item = note_queue_service.claim_next_item(
            db, item_task_type=note_queue_service.TASK_TYPE_GEN_LLM_ITEM
        )
        return int(item.id) if item is not None else None
    except BaseException:  # noqa: BLE001 - 认领失败退避，不死 worker
        logger.exception("llm gen worker: claim failed")
        return None
    finally:
        db.close()


def count_open_llm_items(db: Session) -> int:
    """还有多少条待生成 / 在生成（测试与启动判断用）。"""
    return note_queue_service.count_open_items(
        db, item_task_type=note_queue_service.TASK_TYPE_GEN_LLM_ITEM
    )


def _spawn_llm_generate_worker() -> None:
    """起一条新的 worker 线程（调用方持 `_gen_llm_queue_lock`）。

    起新世代时清掉历史停止位 —— 重建出来的 worker 不该被上一世代的收尾位挡住。
    """
    global _gen_llm_worker
    _gen_llm_worker_stop.clear()
    _gen_llm_worker = threading.Thread(
        target=_llm_gen_worker_loop,
        name=GEN_LLM_WORKER_THREAD_NAME,
        daemon=True,
    )
    _gen_llm_worker.start()


def llm_gen_worker_thread_alive() -> bool:
    """进程里有没有**活着的** `note-gen-llm-worker` 线程（唯一可信存活判据，DEF-069）。

    为什么不看 `_gen_llm_worker` 模块变量：线程带异常 / `SystemExit` 死后它仍指着
    那个 `Thread` 对象，而 `ensure_llm_generate_worker` 只在**入队时**检查一次 ——
    队列跑起来之后线程死了，没有任何现存路径会发现（同 scheduler DEF-067、其余
    worker DEF-069 的教训：状态位会骗人，线程本身不会）。
    """
    return any(
        thread.name == GEN_LLM_WORKER_THREAD_NAME and thread.is_alive()
        for thread in threading.enumerate()
    )


def stop_llm_generate_worker(*, timeout: float = 5.0) -> None:
    """让当前 worker **立即停手退出**（**测试 / 进程收尾专用**，生产不调用）。

    只置停止位不够：worker 常态卡在 `_gen_llm_wakeup.wait(timeout=…)` 上，必须再
    `set` 一下唤醒它回来检查停止位。唤醒后它**不再认领新任务**、手上那条跑完即退。
    """
    _gen_llm_worker_stop.set()
    _gen_llm_wakeup.set()
    worker = _gen_llm_worker
    if worker is not None and worker.is_alive():
        worker.join(timeout=timeout)


def _revive_llm_generate_worker() -> None:
    """worker 线程死了 ⇒ 复位它卡住的那条 + 重建 worker（不丢项）。

    线程死后，它认领的那条 `tasks` 行**永远停在 `running`** ⇒ 不复位的话它既不会
    被重跑、也永远算在「生成中」里。复位就是把它改回 `pending`
    （`recover_interrupted` 只动 `running` 行）。

    ⚠️ **顺序：先复位、再起新 worker**。反过来新 worker 可能刚认领一条就被复位
    抢回 `pending` ⇒ 同一条被跑两遍。
    """
    recovered = {"requeued": 0, "failed": 0}
    try:
        db = _session_factory()
        try:
            recovered = note_queue_service.recover_interrupted(
                db, item_task_types=(note_queue_service.TASK_TYPE_GEN_LLM_ITEM,)
            )
        finally:
            db.close()
    except BaseException:  # noqa: BLE001 - 复位失败也要把 worker 建起来
        logger.exception("LLM 生成 worker 重建：中断行复位失败（继续重建 worker）")
    with _gen_llm_queue_lock:
        if _gen_llm_worker is None or not _gen_llm_worker.is_alive():
            _spawn_llm_generate_worker()
    _gen_llm_wakeup.set()  # 新 worker 可能已在 wait 上：不等下一批也要立刻跑积压
    logger.error(
        "note-gen-llm-worker 已死 —— 已重建（中断行 requeued=%s failed=%s）",
        recovered.get("requeued"),
        recovered.get("failed"),
    )


def start_llm_generate_worker_watchdog(
    *, interval: float = GEN_LLM_WATCHDOG_INTERVAL_SECONDS
) -> threading.Thread:
    """守活线程：worker 死了就重建（DEF-069 同型缺口）。

    与其余 worker 同型：单线程组件没有自愈，任何够不着的死法（`SystemExit` 之类，
    `except Exception` 抓不到、threading 对它静默）都是永久停摆。看门狗自己绝不被
    一次失败带走（`except BaseException` + 失败指数退避，上限
    `GEN_LLM_WATCHDOG_MAX_BACKOFF_SECONDS`）。进程收尾调
    :func:`stop_llm_generate_worker_watchdog`。
    """
    _gen_llm_watchdog_stop.clear()

    def _loop() -> None:
        backoff = interval
        while not _gen_llm_watchdog_stop.is_set():
            if _gen_llm_watchdog_stop.wait(backoff):  # 可被 stop 打断的等待
                return
            try:
                if llm_gen_worker_thread_alive():
                    backoff = interval
                    continue
                _revive_llm_generate_worker()
                backoff = interval
            except BaseException as error:  # noqa: BLE001 - 看门狗自己也不能死
                logger.exception("LLM 生成 worker 看门狗重建失败，%.0f 秒后重试", backoff)
                backoff = min(backoff * 2, GEN_LLM_WATCHDOG_MAX_BACKOFF_SECONDS)

    thread = threading.Thread(target=_loop, name=GEN_LLM_WATCHDOG_THREAD_NAME, daemon=True)
    thread.start()
    return thread


def stop_llm_generate_worker_watchdog() -> None:
    """让看门狗退出巡检（幂等；下一次 `start_llm_generate_worker_watchdog` 自动复位）。"""
    _gen_llm_watchdog_stop.set()


# --------------------------------------------------------------------------- #
# 快照（进度卡）
# --------------------------------------------------------------------------- #
def llm_generate_queue_snapshot(
    db: Session, user_id: int, *, platform: Optional[str] = None
) -> dict:
    """LLM 生成进度快照（`GET /xhs/product-notes/generate-llm-queue`；设计 17 §7.4）。

    键名 `generated`（**不是**底座通用形状的 `synced`）—— 与一期生成队列同一口径；
    `reason` 取 `generation_records.error_message`（本队列有持久审计行 ⇒ 原因不必
    只靠轮询窗口给一次，这正是 T5 三表存在的核心理由）。

    `finished` 逐项形状（在底座形状上**补**审计字段）：
    `{item_id, ok, reason, code, title, note_id, record_id, model_name}`。

    ⚠️ 关联键是 **`item_id`**（本队列把它用作台账主键的字符串形态），**不是**
    `task_id` —— 底座 `_entry` 对商品生成队列只回 `{note_pk:0, item_id, title,
    note_id}` 四键，没有 `task_id`（照 `task_id` 关联会静默全 miss，实测踩过）。
    """
    raw = note_queue_service.snapshot(
        db,
        user_id=user_id,
        item_task_type=note_queue_service.TASK_TYPE_GEN_LLM_ITEM,
        platform=platform,
    )
    # 补审计字段：一次查回本用户近期生成记录，按 `shop_product_id` 索引。
    product_ids: list[int] = []
    for entry in raw.get("finished", []):
        if not isinstance(entry, dict):
            continue
        try:
            product_ids.append(int(entry.get("item_id") or 0))
        except (TypeError, ValueError):
            continue
    record_by_product: dict[int, GenerationRecord] = {}
    if product_ids:
        rows = db.scalars(
            select(GenerationRecord)
            .where(
                GenerationRecord.user_id == user_id,
                GenerationRecord.shop_product_id.in_(product_ids),
            )
            # 同一商品可能有多条历史记录（重试会复用行，但早期版本可能新建过）
            # ⇒ 取最新一条（id 倒序）作为展示依据。
            .order_by(GenerationRecord.id.desc())
        ).all()
        for row in rows:
            record_by_product.setdefault(int(row.shop_product_id or 0), row)
    finished: list[dict] = []
    for entry in raw.get("finished", []):
        if not isinstance(entry, dict):
            continue
        merged = dict(entry)
        try:
            key = int(merged.get("item_id") or 0)
        except (TypeError, ValueError):
            key = 0
        record = record_by_product.get(key)
        if record is not None:
            merged["record_id"] = int(record.id)
            merged["model_name"] = str(record.model_name or "")
            # `reason` 口径：**优先用审计行**（跨重启留存），快照行体里的原因作兜底
            # （早期行 / 审计行被清理）。两处都空 ⇒ 保持空（缺值不造假）。
            if record.status == GEN_STATUS_FAILED and record.error_message:
                merged["reason"] = record.error_message
            elif record.status == GEN_STATUS_SUCCESS:
                merged["reason"] = ""
        finished.append(merged)
    # 计数口径（设计 17 §7.4「键名 `generated`」+ 与一期 `generate_queue_snapshot` 同构）：
    #   `total`  = 底座形状 = 近期终态数 + 在队数 + 运行中（**不是**「本轮总数」）
    #   `generated` / `failed` = **近期终态项内**的成功 / 失败数
    # ⚠️ 不能用 `total - failed` 反推 `generated`：`total` 含未终态的 queued/running，
    #    减法会把还在跑的项算进「已生成」。一律按 `finished` 逐项数。
    succeeded = len([entry for entry in finished if entry.get("ok") is True])
    failed = len([entry for entry in finished if entry.get("ok") is False])
    return {
        "running": raw["running"],
        "queued": raw["queued"],
        "total": raw["total"],
        "generated": succeeded,
        "failed": failed,
        "finished": finished,
    }


def wait_llm_generate_done(timeout: float = 60.0) -> bool:
    """等 LLM 生成队列跑空（测试用；判定落在库上 —— 不信内存位）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db = _session_factory()
        try:
            if count_open_llm_items(db) == 0:
                return True
        finally:
            db.close()
        time.sleep(0.1)
    return False


__all__ = [
    "GEN_LLM_BATCH_MAX_ITEMS",
    "GEN_LLM_IDLE_POLL_SECONDS",
    "GEN_LLM_WATCHDOG_INTERVAL_SECONDS",
    "GEN_LLM_WORKER_THREAD_NAME",
    "count_open_llm_items",
    "ensure_llm_generate_worker",
    "gate_for_llm_generation",
    "gate_llm_gateway_ready",
    "llm_gen_worker_thread_alive",
    "llm_generate_queue_snapshot",
    "start_llm_batch_generate",
    "start_llm_generate_note",
    "start_llm_generate_worker_watchdog",
    "start_llm_retry",
    "stop_llm_generate_worker",
    "stop_llm_generate_worker_watchdog",
    "wait_llm_generate_done",
]
