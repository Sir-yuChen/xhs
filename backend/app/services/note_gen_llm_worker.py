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
    REGEN_SCOPE_BODY,
    REGEN_SCOPE_TITLE,
    REGEN_SCOPE_TOPICS,
    REGEN_TEXT_FIELD_SCOPES,
)
from backend.app.models.shop import NoteGoods
from backend.app.models.note import (
    NOTE_TYPE_LOCAL_GENERATED,
    PUBLISH_STATUS_PENDING,
    Note,
    NoteAsset,
    set_note_publish_status,
)
from backend.app.models.shop_product import ShopProduct
from backend.app.models.user import User
from backend.app.services import (
    copy_requirements,
    llm_gateway_client,
    note_queue_service,
    note_version_service,
)
from backend.app.core.business_provider_keys import BUSINESS_KEY_LLM_NOTE_TEXT
from backend.app.services.llm_gateway_client import LLMGatewayError
from backend.app.services.media_generation import (
    MediaGenerationError,
    build_media_plan_assets,
    build_note_media,
)
#: v2.26（M-C1）：组合计划的归一化 / 校验 / 摘要（**唯一实现**）。
from backend.app.services.media_plan import (
    has_cover,
    normalize_plan,
    plan_form,
    plan_summary,
)
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
        llm_gateway_client.resolve_business_provider(db, user, BUSINESS_KEY_LLM_NOTE_TEXT)
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
    media_plan: Optional[list] = None,
    record_id: Optional[int] = None,
    item_name: str = "",
    copy_requirements: Optional[dict] = None,
    regen_scope: str = "",
    target_record_id: Optional[int] = None,
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
    #: v2.26（M-C1）：**组合计划**随行体落库（有序段列表）。缺省不写键
    #: —— 由消费端按「缺 ⇒ 退化为单来源」处理（归一化只有 `media_plan` 一处）。
    if media_plan:
        payload["media_plan"] = [dict(seg) for seg in media_plan]
    if record_id:
        payload["record_id"] = int(record_id)
    #: **文案要求**（用户口径 2026-10-17）：随行体落库（worker 重试时同一份要求）。
    #: 缺省不写键 —— 缺 ⇒ 走默认（不把所有老行体都补成一份默认配置）。
    if copy_requirements:
        payload["copy_requirements"] = dict(copy_requirements)
    #: **局部重生成参数必须随行体落库**（2026-10-18 实测踩过）：worker 是从
    #: `payload` 读 `regen_scope` 的，漏写 ⇒ 拿到 `None` ⇒ 按 `all` 走
    #: ⇒ 逐块合并 / 媒体复用**全部不生效**（表现为「点只改正文，标题也变了」）。
    if regen_scope and str(regen_scope) != "all":
        payload["regen_scope"] = str(regen_scope)
    if target_record_id:
        payload["target_record_id"] = int(target_record_id)
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
    media_options: Optional[dict] = None,
    media_plan: Optional[list] = None,
    copy_requirements: Optional[dict] = None,
    regen_scope: str = "all",
    target_record_id: Optional[int] = None,
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
        media_options=media_options,
        media_plan=media_plan,
        item_name=str(getattr(row, "item_title", "") or ""),
        copy_requirements=copy_requirements,
        regen_scope=regen_scope,
        target_record_id=target_record_id,
    )
    task_row, created = note_queue_service.enqueue_item(
        db,
        user_id=user_id,
        item_task_type=note_queue_service.TASK_TYPE_GEN_LLM_ITEM,
        account_id=account_id,
        #: ⚠️ **局部重生成必须用独立幂等键**（2026-10-18 实测踩过）：
        #: 幂等按 `item_id` 判重，而整篇生成与局部重生成都以「商品主键」为 `item_id`
        #: ⇒ 点重生成会命中**上一次整篇生成残留的未终态项**，复用它 ⇒
        #: `record_id` 是旧值、`regen_scope` 是 `all`，最终去改错了行
        #: （实测报 `UNIQUE constraint ... generation_records`）。
        #: 加 `regen@当前版本id` 前缀后：重生成只与**同一条当前版本的重生成**判重，
        #: 既不撞整篇生成，也不撞别的版本。
        item_id=(
            f"regen@{int(target_record_id)}"
            if target_record_id
            else payload_extra["item_id"]
        ),
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


def generate_llm_note_sync(
    db: Session,
    user_id: int,
    *,
    shop_product_id: int,
    account_id: Optional[int] = None,
    template_id: Optional[int] = None,
    gateway_config_id: Optional[int] = None,
    model_name: str = "",
    media_source: str = MEDIA_SOURCE_PRODUCT_IMAGE,
    media_plan: Optional[list] = None,
    copy_requirements: Optional[Any] = None,
) -> dict[str, Any]:
    """**同步**生成一篇 LLM 笔记：入队后**在本线程立刻跑完**，返回最终结果。

    用户口径 2026-10-18：

    > 「调整一下生成笔记**不要放入队列中直接同步等待**」

    ## 为什么不重写生成逻辑

    生成 + 落库 + 记账这一整段在 `_run_one_llm_generate(item_id)` 里，它**自建
    Session**（worker 线程够不着 FastAPI 的注入层）。同步路径复用同一实现：

    1. 用 `start_llm_generate_note()` 建队列项（**保留**幂等判重、门禁、行体归一）；
    2. **本线程**直接调 `_run_one_llm_generate(item_id)` —— 跑完再返回；
    3. 从队列项行体取回 `record_id / note_id`（worker 成功时已写进 payload）；
    4. 读 `generation_records` 组装返回体（标题/正文/话题/**媒体清单**/文案要求快照）。

    ⚠️ 第 1 步的队列项**不是**「入队等别人跑」—— 它只是承载参数与审计的载体，
    建完就由当前请求线程消费掉。好处是两条路径共用同一份幂等与失败记账，
    不会出现「同步一套、队列一套」的两份真相。

    ⚠️ **不吞异常**：worker 内部把失败写进队列项的 `status/reason`（它不许往外抛，
    否则队列会卡 running）。所以这里跑完必须**回读状态**：`failed` ⇒ 抛
    `NoteGenerationError`，把原因原样交给 API 层翻译成 4xx/5xx。
    """
    receipt = start_llm_generate_note(
        db,
        user_id,
        shop_product_id=shop_product_id,
        account_id=account_id,
        template_id=template_id,
        gateway_config_id=gateway_config_id,
        model_name=model_name,
        media_source=media_source,
        media_plan=media_plan,
        copy_requirements=copy_requirements,
        source=note_queue_service.SOURCE_PRODUCT_NOTE_GENERATE_LLM_SINGLE,
    )
    item_id = int(receipt.get("item_id") or 0)
    if not item_id:
        raise NoteGenerationError("生成任务未能建立（缺少队列项主键）", code="LLM_ITEM_INVALID")

    #: 同步执行（worker 自建 Session，跑完即回）
    _run_one_llm_generate(item_id)

    return _collect_sync_result(item_id)


def regen_llm_note_sync(
    db: Session,
    user_id: int,
    *,
    shop_product_id: int,
    regen_scope: str,
    target_record_id: int,
    template_id: Optional[int] = None,
    media_source: str = MEDIA_SOURCE_PRODUCT_IMAGE,
    media_plan: Optional[list] = None,
    copy_requirements: Optional[Any] = None,
) -> dict[str, Any]:
    """**同步**做一次局部重生成：跑完才返回，不用轮询。

    用户口径 2026-10-18：*「重新生成要同步接口等待」* ——
    与「整篇生成」同一形态：点完按钮就等结果回填，而不是收一个
    「已提交（后台执行）」再让用户去版本列表里翻。

    ## 复用同一条链路（不写第二套）

    建队列项 → **本线程**直接跑 → 回读组装。与 `generate_llm_note_sync`
    只差 `regen_scope` / `target_record_id` 两个参数 —— 幂等判重、失败记账、
    逐块合并（`_merge_text_scope`）全走同一份实现。

    ⚠️ 当前版本校验（归属 / 商品存在 / scope 合法性）由 API 层先做，这里不再判：
    那几条都是**确定性答案**，应当在扣费前就 400 掉。
    """
    receipt = start_llm_generate_note(
        db,
        user_id,
        shop_product_id=shop_product_id,
        account_id=None,
        template_id=template_id,
        media_source=media_source,
        media_plan=media_plan or None,
        copy_requirements=copy_requirements,
        regen_scope=regen_scope,
        target_record_id=int(target_record_id),
        source="llm_generate_regen_sync",
    )
    item_id = int(receipt.get("item_id") or 0)
    if not item_id:
        raise NoteGenerationError("重生成任务未能建立（缺少队列项主键）", code="LLM_ITEM_INVALID")

    #: 同步执行（worker 自建 Session，跑完即回）
    _run_one_llm_generate(item_id)
    return _collect_sync_result(item_id)


def _collect_sync_result(item_id: int) -> dict[str, Any]:
    """跑完之后**回读队列项**组装返回值（同步生成与同步重生成共用）。

    ⚠️ 回读要**新开 Session**：worker 用的是它自己那个，请求线程的 `db` 里看不到
    它提交的改动（SQLite + 独立连接 ⇒ 旧快照）。
    """
    check = _session_factory()
    try:
        item = check.get(Task, item_id)
        if item is None:
            raise NoteGenerationError("生成任务已不存在（可能被清理）", code="LLM_ITEM_INVALID")
        payload = item.payload if isinstance(item.payload, dict) else {}
        if str(item.status or "") != "done":
            reason = str(payload.get("reason") or "生成失败（未给出原因）")
            code = str(payload.get("code") or "LLM_GENERATE_FAILED")
            raise NoteGenerationError(reason, code=code)

        record_id = int(payload.get("record_id") or 0)
        record = check.get(GenerationRecord, record_id) if record_id else None
        if record is None:
            raise NoteGenerationError("生成成功但记录行缺失", code="LLM_RECORD_MISSING")

        media: list[dict] = []
        try:
            raw_media = json.loads(record.media_json or "[]")
            if isinstance(raw_media, list):
                media = [entry for entry in raw_media if isinstance(entry, dict)]
        except (TypeError, ValueError):
            media = []

        return {
            "record_id": record_id,
            "note_id": payload.get("note_id") or "",
            "title": record.title or "",
            "body": record.body or "",
            "topics": json.loads(record.topics_json or "[]"),
            "media": media,
            "copy_requirements": json.loads(record.copy_requirements_json or "{}"),
            "model_name": record.model_name or "",
            "version_no": int(record.version_no or 1),
            "is_adopted": bool(record.is_adopted),
            #: **形态**（`image` / `video`）—— 前端据此把回显放进对应页签。
            "note_form": str(record.note_form or "image"),
        }
    finally:
        check.close()


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
    media_options: Optional[dict] = None,
    media_plan: Optional[list] = None,
    hot_topic: str = "",
    copy_requirements: Optional[Any] = None,
    regen_scope: str = "all",
    target_record_id: Optional[int] = None,
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
        # ⚠️ **热词驱动（M3 方向 2）走 `media_options` 这条已通的管道**：
        # 它是入队行体里**已经**全程透传到 worker 的字段（`_llm_payload` →
        # `payload["media_options"]` → worker 读出 → `_finalize_generated`）。
        # 不新开一条 payload 键 = 不做第二次表单（DEF-044）。
        # 语义上它只是「本次生成的附加参数袋」，不是「媒体专属」。
        media_options=({**(media_options or {}), "hot_topic": hot_topic} if hot_topic else media_options),
        #: v2.26（M-C1）组合计划：**原样透传**（校验已在端点层做完，此处不再判）。
        media_plan=media_plan,
        #: 文案要求（用户口径 2026-10-17）：端点已 `normalize` 过 ⇒ 这里只取落库形态。
        #: `CopyRequirements` 实例或 dict 都接受（便于重试管线复用）。
        copy_requirements=(
            copy_requirements.as_dict()
            if hasattr(copy_requirements, "as_dict")
            else copy_requirements
        ),
        #: **局部重生成**：`all` / `text`（只重写文案）/ `media`（只重配图片）。
        #: worker 按它决定跑文案链路还是媒体链路（或两者）。
        regen_scope=str(regen_scope or "all"),
        target_record_id=(int(target_record_id) if target_record_id else None),
    )
    record_id = 0
    if not created:
        # 幂等命中：队列项已在，审计行也已存在（由首次入队创建）。
        # ⚠️ 必须把已有 `record_id` 回填给调用方 —— 返回 0 会让前端「已在队列中」
        # 的提示拿不到可跳转的审计行（且与首次入队的回执形状不一致）。
        record_id = int((task_row.payload or {}).get("record_id") or 0)
    if created and target_record_id:
        #: **局部重生成 = 覆盖当前这一版**（用户口径 2026-10-19：*「局部生成是只对当前版本
        #: 数据进行覆盖」*；与 **每次点「生成」都新建版本** 是两件不同的事）。
        #: 版本号不变、行不新增：用户只是在改这一版里的某一块，不是要再产一版。
        #: ⚠️ 覆盖的是**当前这一版**（`target_record_id` 就是弹窗里正在看的那版），
        #: 不是「另一条当前版本」—— 队列项 payload 里那个字段承担的就是「覆盖哪一行」。
        record_id = int(target_record_id)
        record = db.get(GenerationRecord, record_id)
        if record is None:
            raise NoteGenerationError(
                "要覆盖的版本已不存在，无法重生成", code="LLM_TARGET_MISSING"
            )
        #: ⚠️ **先存原值再改行**：覆盖会立刻写 `task_id` / `status`，
        #: 之后再读「改动前的内容」读到的是本次新值（「只改正文却把标题也换掉」的真因）。
        merged_snapshot = _snapshot_record_text(record)
        record.task_id = int(task_row.id)
        record.status = GEN_STATUS_QUEUED
        record.regen_scope = str(regen_scope or "all")
        record.error_code = None
        record.error_message = None
        if copy_requirements is not None:
            record.copy_requirements_json = note_version_service.serialize_copy_requirements(
                copy_requirements.as_dict() if hasattr(copy_requirements, "as_dict")
                else copy_requirements
            )
        db.flush()
        merged = dict(task_row.payload or {})
        merged["record_id"] = record_id
        merged["text_snapshot"] = merged_snapshot
        if template_name:
            merged["template_name"] = str(template_name)
        task_row.payload = merged
        db.flush()
    elif created:
        #: **整篇生成** = 新版本行，**永不覆盖**（用户口径 2026-10-17）。
        record = GenerationRecord(
            user_id=user_id,
            task_id=int(task_row.id),
            shop_product_id=int(row.id),
            template_id=(int(template.id) if template is not None else None),
            gateway_config_id=(int(gateway_config_id) if gateway_config_id else None),
            model_name=str(model_name or ""),
            media_source=str(media_source or MEDIA_SOURCE_PRODUCT_IMAGE),
            status=GEN_STATUS_QUEUED,
            version_no=note_version_service.next_version_no(
                db, user_id=int(user_id), shop_product_id=int(row.id)
            ),
            parent_record_id=(int(target_record_id) if target_record_id else None),
            regen_scope=str(regen_scope or "all"),
            #: 文案要求**快照**（模板/区间后来被改过也能还原当时的实际要求）
            copy_requirements_json=note_version_service.serialize_copy_requirements(
                copy_requirements.as_dict() if hasattr(copy_requirements, "as_dict")
                else copy_requirements
            ),
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
        #: **队列项主键**（同步路径靠它去消费这一条并回读终态）。
        #: ⚠️ 曾经没返回 ⇒ `generate_llm_note_sync` 拿到 0 就报「缺少队列项主键」。
        "item_id": int(task_row.id),
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
    media_options: Optional[dict] = None,
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
                media_options=media_options,
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
    """**同步兜底**媒体清单（只处理商品图直链；其余来源走 `build_note_media`）。

    ⚠️ **T7 已落地**（设计 17 §13.4）：真媒体产出走
    :func:`backend.app.services.media_generation.build_note_media`（需要网络 IO /
    事务，由调用方在 `_persist_generated_note` 里执行）。本函数只保留
    **纯同步**的商品图直链分支，供**不持 db 的调用点**（如快照 `media_count`
    估算）复用 —— 保持「无副作用」。
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


def _required_fields_for(scope: str) -> tuple[str, ...]:
    """局部重生成只要求**目标块**存在；整篇 / 旧 `text` 仍要求标题 + 正文。"""
    return {
        REGEN_SCOPE_TITLE: ("title",),
        REGEN_SCOPE_BODY: ("body",),
        REGEN_SCOPE_TOPICS: (),  # 话题有独立兜底，不作为硬失败
    }.get(scope, ("title", "body"))


def _compose_regen_tail(scope: str, *, snapshot: Optional[dict[str, Any]] = None) -> str:
    """只取「局部重生成说明」那一段（供校验脚本复现实际提示词）。"""
    if scope not in REGEN_TEXT_FIELD_SCOPES:
        return ""
    return copy_requirements.build_regen_instruction(scope, snapshot=snapshot)


def _compose_instruction(
    copy_req: Any,
    *,
    scope: str,
    snapshot: Optional[dict[str, Any]] = None,
) -> str:
    """拼「发给模型的补充说明」：**用户要求在前，局部重生成说明在后**。

    ⚠️ 顺序有意义：后出现的指令对模型权重更高。局部重生成那段说的是
    「只输出这一块」，必须排在「字数/话题数量」等常规要求**之后**，
    否则模型会按整篇任务写三块（实测旧行为就是这样）。
    """
    base = copy_requirements.build_instruction(copy_req) if copy_req is not None else ""
    extra = ""
    if scope in REGEN_TEXT_FIELD_SCOPES:
        extra = copy_requirements.build_regen_instruction(scope, snapshot=snapshot)
    #: ⚠️ 这个整体会被 `generate_full_note` 追加在**整个提示词的最末尾**
    #: （`[prompt] + 【额外风格要求】instruction`），所以局部重生成那段天然排在
    #: 模板的【⑥ 输出格式】**之后** —— 这是它能压过「输出整块 JSON」的原因。
    return chr(10).join(part for part in (base, extra) if part)


def _snapshot_record_text(record: GenerationRecord) -> dict[str, Any]:
    """取记录**当前**的文案三块（用于原地重生成时保住其余两块）。

    ⚠️ 必须在模型调用**之前**取值：局部重生成是**原地更新同一行**，
    只要 `_persist_generated_note` 一落库，行里的旧值就没了 ——
    那时再读「当前版本」读到的是刚写进去的新值，等于三块全变。
    """
    try:
        topics = json.loads(record.topics_json or "[]")
    except ValueError:
        topics = []
    return {
        "title": str(record.title or ""),
        "body": str(record.body or ""),
        "topics": [str(t) for t in topics] if isinstance(topics, list) else [],
    }


def _merge_text_scope(
    db: Session,
    *,
    record: GenerationRecord,
    generated: Any,
    scope: str,
    snapshot: Optional[dict[str, Any]] = None,
) -> Any:
    """**逐块合并**：只保留这次真正要换的那一块，其余两块回退到当前版本。

    用户口径 2026-10-18：*「后端应支持每个文案区域单独重新生成」* ——
    在结果面板里点「正文」的重新生成，**标题和话题必须一个字都不变**。

    ⚠️ 为什么必须由后端做：模型一次调用总是把三块一起写出来（模板的输出契约就是
    三块 JSON），前端无法「只要正文」；只有拿到当前版本再做合并才能保证其余两块不变。

    三个 scope 各自只换一块：
      * `title`  ⇒ 标题换、正文/话题用当前版本；
      * `body`   ⇒ 正文换、标题/话题用当前版本；
      * `topics` ⇒ 话题换、标题/正文用当前版本。

    当前版本取不到（早期记录没存这几列）⇒ **报错不猜**：静默用新值等于又改了三块。
    """
    #: **原地更新**时以调用方传入的 `snapshot` 为准（那是本行被改写前的原值）；
    #: 老路径（插新行）没有 snapshot ⇒ 退回按 `parent_record_id` 取当前版本。
    if snapshot is not None:
        base_title = str(snapshot.get("title") or "")
        base_body = str(snapshot.get("body") or "")
        base_topics = list(snapshot.get("topics") or [])
        if not base_body or not base_title:
            raise NoteGenerationError(
                "当前版本没有可沿用的文案（标题或正文为空），请改用「整篇重新生成」。",
                code="LLM_TARGET_TEXT_MISSING",
            )
        class _Base:  # 只为复用下方字段名
            ok = True
            title = base_title
            body = base_body
            topics = base_topics
        target = _Base()
    else:
        target = _load_target_text(db, record)
        if not getattr(target, "ok", True):
            raise NoteGenerationError(
                "当前版本缺失，无法只改这一块（请改用「整篇重新生成」）",
                code="LLM_TARGET_MISSING",
            )
    #: ⚠️ **每个分支只保留目标块的新值**，其余两块一律回退当前版本的**原值**。
    if scope == REGEN_SCOPE_TITLE:
        # 标题换（新值保留）；正文 / 话题回当前版本
        generated.body = target.body
        generated.topics = list(target.topics)
    elif scope == REGEN_SCOPE_BODY:
        # 正文换；标题 / 话题回当前版本
        generated.title = target.title
        generated.topics = list(target.topics)
    elif scope == REGEN_SCOPE_TOPICS:
        # 话题换；标题 / 正文回当前版本
        generated.title = target.title
        generated.body = target.body
    return generated


def _load_target_text(db: Session, record: GenerationRecord) -> GeneratedNote:
    """「只重配图片」时，从**当前版本**取回标题/正文/话题，包成 `GeneratedNote`。

    ⚠️ 必须造一个**新的 `GeneratedNote` 对象**（不是直接返回当前版本的 record）：
    下游 `_persist_generated_note` 会读取 `.guardrails` / `.model_name` /
    `.latency_ms` 等字段并写进新笔记，而 `GenerationRecord` 没有这些属性。

    ⚠️ 当前版本缺文案（`body` 为空）⇒ 抛错而不是产出空正文笔记：
    空正文的笔记发不出去，静默产出等于把错误推到发布环节。
    """
    if not record.parent_record_id:
        raise NoteGenerationError(
            "「只重配图片」必须先指定当前版本。", code="LLM_TARGET_REQUIRED"
        )
    target = db.get(GenerationRecord, int(record.parent_record_id))
    if target is None:
        raise NoteGenerationError(
            f"当前版本（id={record.parent_record_id}）不存在，无法沿用其文案。",
            code="LLM_TARGET_REQUIRED",
        )
    body = str(target.body or "").strip()
    title = str(target.title or "").strip()
    if not body or not title:
        raise NoteGenerationError(
            "当前版本没有可沿用的文案（标题或正文为空），请改用「整篇重新生成」。",
            code="LLM_TARGET_TEXT_MISSING",
        )
    try:
        topics = json.loads(target.topics_json or "[]")
    except ValueError:
        topics = []
    return GeneratedNote(
        title=title,
        body=body,
        topics=topics if isinstance(topics, list) else [],
        #: 沿用当前版本的模型名（这篇的文案确实是那个模型写的）
        model_name=str(target.model_name or ""),
        latency_ms=0,
        guardrails={},
        #: ⚠️ `ok` 是**只读属性**（`not block_reason`），不能当构造参数传 ——
        #: 留空 `block_reason` 即表示「通过」，这也正是沿用的语义（当前版本已过护栏）。
        block_reason="",
    )


def _own_media_entries(record: GenerationRecord) -> list[dict]:
    """本行 `media_json` 里的**成功条目**（失败段不算可用媒体）。"""
    try:
        raw = json.loads(record.media_json or "[]")
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        #: ⚠️ **失败占位也保留**（用户口径 2026-10-18）：只重写文案时，图的成败
        #: 与这次动作无关 —— 把失败占位过滤掉会让「图 4」变「图 3」，
        #: 用户会以为「重生成标题把一张图弄丢了」。
        #: 保留原样 ⇒ 失败那张仍然显示为占位图 + 原因，可以单独重配图。
        failed = entry.get("ok") is False
        if failed:
            out.append(
                {
                    "url": "",
                    "local_path": "",
                    "sort_order": int(entry.get("sort_order") or len(out)),
                    "asset_type": str(entry.get("asset_type") or "image"),
                    "ok": False,
                    "error": str(entry.get("error") or "生成失败（未返回原因）"),
                    "role": str(entry.get("role") or ""),
                    #: 失败占位同样要带来源与段位（否则占位图归不到那一段）
                    "source": str(entry.get("source") or ""),
                    "slot_index": entry.get("slot_index"),
                }
            )
            continue
        url = str(entry.get("url") or "")
        local = str(entry.get("local_path") or "")
        if not url and not local:
            continue
        out.append(
            {
                "url": url,
                "local_path": local,
                "sort_order": int(entry.get("sort_order") or len(out)),
                "asset_type": str(entry.get("asset_type") or "image"),
                #: ⚠️ **来源与段位必须跟着搬**（2026-10-19 实测 bug）：局部重写文案时
                #: 沿用当前版本媒体，这里漏了 `source` / `slot_index` ⇒ 落回 `media_json`
                #: 后全是空 / 0 ⇒ 前端按段位还原时**所有图挤进封面**（用户看到
                #: 「27 版回显全部又回显到了封面中」）。
                "source": str(entry.get("source") or ""),
                "role": str(entry.get("role") or ""),
                "slot_index": entry.get("slot_index"),
            }
        )
    return out


def _target_media_entries(db: Session, *, record: GenerationRecord) -> list[dict]:
    """取**当前版本**那篇笔记的媒体条目（供「只重写文案」沿用）。

    ⚠️ 走 `note_generation_links` 找当前版本的 notes 行 —— 那是「笔记 ↔ 生成记录」的
    唯一关系表。找不到 ⇒ 返回空（由调用方报 `LLM_TARGET_MEDIA_MISSING`）。
    ⚠️ 复制的是 **note_assets 的字段**（url / local_path / sort_order），
    不重新下载文件：文件已在本地，重复下载既慢又可能因源站限流失败。
    """
    #: ⚠️ **先看本行自己有没有媒体**（用户口径 2026-10-18 实测缺陷：
    #: 第 20 版重生成标题报「当前版本没有可沿用的媒体」）——
    #: 局部重生成是**原地更新同一行**，这一行本身就有 `media_json`（4 张图），
    #: 完全不需要再沿 `parent_record_id` 去父版本找。
    #: 原实现只认父版本 ⇒ 原地更新后的行（parent 为空）必然报错。
    own = _own_media_entries(record)
    if own:
        return own
    if not record.parent_record_id:
        return []
    link = db.scalar(
        select(NoteGenerationLink).where(
            NoteGenerationLink.generation_record_id == int(record.parent_record_id)
        )
    )
    if link is None:
        return []
    assets = db.scalars(
        select(NoteAsset)
        .where(NoteAsset.note_id == int(link.note_pk))
        .order_by(NoteAsset.sort_order)
    ).all()
    return [
        {
            "asset_type": str(a.asset_type),
            "url": str(a.url or ""),
            "local_path": str(a.local_path or ""),
            "sort_order": int(a.sort_order or 0),
        }
        for a in assets
    ]


def _persist_generated_note(
    db: Session,
    *,
    user: User,
    row: ShopProduct,
    account_id: Optional[int],
    generated: GeneratedNote,
    record: GenerationRecord,
    media_source: str,
    media_options: Optional[dict] = None,
    media_plan: Optional[list] = None,
    regen_scope: str = "all",
) -> dict[str, Any]:
    """把生成产物落库（**调用方持事务**；本函数只 `flush`，不 `commit`）。

    ⚠️ **`regen_scope` 决定复用哪一半**（用户口径 2026-10-17）：
      - `text`  ⇒ 只重写了文案 ⇒ **媒体沿用当前版本原有的**（`_target_media_entries`）；
      - `media` ⇒ 只重配了图片 ⇒ **文案沿用当前版本**（`generated` 已由调用方按当前版本填好）；
      - `all`   ⇒ 全都新产出。

    落五处：`notes` + `note_topics`（经 `apply_sync_topics`，**唯一话题入口**）
    + `note_assets`（经 `build_note_media`，T7）+ `aigc_media_assets`（AIGC 溯源）
    + `note_generation_links`（本地主键级关系，§3.7）
    + 回写 `generation_records`（`status=success` + 标题 / 正文 / 话题 + 耗时）。

    护栏**已在上游拦截**（`generate_full_note` 返回 `ok=False` 时不会走到这里）
    ⇒ 本函数只处理「通过」的产物。

    ⚠️ **媒体失败不废正文**（§13.4.2）：`build_note_media` 内部对「出图 / 下载失败」
    已降级为空媒体；此处只接住**配置级** `MediaGenerationError`（业务点未配 /
    素材不存在）—— 那种情况**整条记失败**（用户该知道媒体没成），不落半条笔记。
    """
    # ---- T7：媒体产出（可能调上游出图，故先于 note 落库执行） ----
    #: ⚠️ **只重写文案时不再跑媒体**（`text`）：既省一次出图费用，也保住用户满意的图。
    #: 媒体条目从**当前版本**复制（复制的是 note_assets 行，不是重新下载文件）。
    #: 文案类细则（`title` / `body` / `topics`）与旧的 `text` **同一条路**：
    #: 都只重写文案、**媒体一律沿用当前版本**（省一次出图费，也保住用户满意的图）。
    #: ⚠️ 三者与 `text` 的唯一区别在「哪一块被替换」（见下方 `_apply_text_scope`）。
    if regen_scope == "text" or regen_scope in REGEN_TEXT_FIELD_SCOPES:
        media_entries = _target_media_entries(db, record=record)
        if not media_entries:
            raise MediaGenerationError(
                "当前版本没有可沿用的媒体，无法只重写文案（请改用「整篇重新生成」）",
                code="LLM_TARGET_MEDIA_MISSING",
            )
        media_segments: list[dict[str, Any]] = []
        plan = normalize_plan(media_plan if media_plan else media_source)
        aigc_origins: list[Any] = []
        #: 跳过下面的媒体生成整段（直接进 note 落库）
        _skip_media_generation = True
    else:
        _skip_media_generation = False

    # ⚠️ **v2.26（M-C1）**：改走 `build_media_plan_assets()` —— 它按段循环调用
    # `build_note_media()`（**不重造出图逻辑**），并做到「逐段隔离失败」：
    # 内页段失败**半成功可交付**，封面段失败**由下方 cover_ok 判整篇失败**。
    # ⚠️ 无 `media_plan` 时由 `normalize_plan` 把单来源**降级为一段** ——
    # 归一化只有一处，调用方不各判一次（DEF-044）。
    if not _skip_media_generation:
        plan = normalize_plan(media_plan if media_plan else media_source)
        #: **按入口取媒体模型**（用户口径 2026-10-18「各个功能单独配置」）：
        #: 这里是「LLM 生成笔记」入口，配图/视频走它自己的 key —— 与笔记工坊的
        #: 配图模型**互不影响**（改一处不会串到另一处）。
        from backend.app.core.business_provider_keys import (
            BUSINESS_KEY_LLM_NOTE_IMAGE,
        )

        media_entries, aigc_origins, media_segments = build_media_plan_assets(
            db,
            user=user,
            row=row,
            media_plan=plan,
            media_options=media_options,
            record=record,
            #: ⚠️ 图片与视频在 `build_note_media` 内按 `media_source` 分流，
            #: 视频分支会用 `BUSINESS_KEY_LLM_NOTE_VIDEO`；这里传图片 key 作基准。
            business_key=BUSINESS_KEY_LLM_NOTE_IMAGE.key,
        )
        #: R1：**无封面 ⇒ 整篇失败**（不可半成功 —— 没封面的笔记不可发布）。
        #: 判据用「计划里有没有 cover 段」而非「cover 段成不成功」：两者都要，
        #: 顺序是先有段、再有产出（缺段是端点层就该拦的输入错误，缺产出是这里）。
        if has_cover(plan) and not any(
            seg["ok"] and seg["role"] == "cover" for seg in media_segments
        ):
            raise MediaGenerationError(
                "封面媒体生成失败（封面缺失的笔记不可发布）", code="LLM_MEDIA_COVER_FAILED"
            )

    #: ⚠️ **一版生成记录只对应一篇笔记**（用户口径 2026-10-19：*「它应该发布当前用户指定的
    #: 生成的笔记信息，不是选内容库中未发布的笔记，这两条逻辑线」*）——
    #: 原实现每次走到这里都 `Note(...)` 新建 ⇒ 同一 `record_id` 下越攒越多
    #: （实测 record 28 累积 4 篇、record 29 有 2 篇），发布时根本分不清该发哪篇，
    #: 只能从一堆重复里挑，于是「挑到没有商品关系的那篇」这类错必然发生。
    #: 现在：这一版**已经产出过笔记** ⇒ 就地覆盖它（标题/正文/话题/资产全部重写），
    #: 不新增行；首次生成才创建。
    existing_note = None
    for candidate in db.scalars(
        select(Note).where(
            Note.user_id == row.user_id, Note.note_type == NOTE_TYPE_LOCAL_GENERATED
        )
    ).all():
        candidate_raw = candidate.raw_json
        if not isinstance(candidate_raw, dict):
            try:
                candidate_raw = json.loads(candidate_raw or "{}")
            except (TypeError, ValueError):
                candidate_raw = {}
        if not isinstance(candidate_raw, dict):
            candidate_raw = {}
        if (
            int(((candidate_raw.get("generation") or {}).get("record_id")) or 0)
            == int(record.id)
        ):
            existing_note = candidate
            break

    if existing_note is not None:
        note = existing_note
        note.title = generated.title
        note.content = generated.body
        note.note_form = plan_form(plan)
        #: 资产整份重建（图可能换了；不重建会留着上一次的图）
        for asset in db.scalars(
            select(NoteAsset).where(NoteAsset.note_id == int(note.id))
        ).all():
            db.delete(asset)
        db.flush()
    else:
        note = Note(
        user_id=row.user_id,
        platform_account_id=account_id,        platform="xhs",
        note_id=_local_note_id(),
        title=generated.title,
        content=generated.body,
        author_name="",
        note_type=NOTE_TYPE_LOCAL_GENERATED,
        #: §15：形态由计划派生（有视频段 ⇒ 视频笔记，否则图文）—— 发布时决定 `common.type`。
        note_form=plan_form(plan),
        raw_json={
            "guardrails": generated.guardrails,
            "generation": {
                "record_id": int(record.id),
                "model_name": generated.model_name,
                "latency_ms": generated.latency_ms,
                "media_source": media_source,
                #: v2.26（M-C1）：组合摘要 + 逐段结果（用户要能看出「哪个来源失败+为什么」）。
                "media_plan_summary": plan_summary(plan),
                "media_segments": media_segments,
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

    #: ⚠️ **`sort_order` 必须是全局递增序号**（2026-10-19 实测 bug：*「笔记封面页不是最新的」*）——
    #: 原先直接写 `entry["sort_order"]`，而那个值来自**段内序号**（`_assets_from_product_image`
    #: 每段都从 0 开始）⇒ 封面 1 张 + 内页 3 张时出现 3 个 `sort_order=0`，
    #: 发布链路 `_select_media()` 按 `order_by(sort_order)` 取第 1 张当封面 ⇒
    #: **封面变成哪张取决于数据库返回顺序**（不可复现的错图）。
    #: 这里按**条目在 `media_entries` 里的既有顺序**（已是「封面段 → 内页 1 → 内页 2」）
    #: 重编 0..N-1，图序因此与用户在界面上看到的一致。
    for position, entry in enumerate(media_entries):
        db.add(
            NoteAsset(
                note_id=note.id,
                asset_type=entry["asset_type"],
                url=entry["url"],
                local_path=entry["local_path"],
                sort_order=position,
            )
        )

    # AIGC 溯源（§13.4.3）：与笔记落库**同一事务**；非 AI 来源（商品图 / 素材）无溯源行。
    for origin in aigc_origins:
        db.add(origin)

    # 本地主键级「生成笔记 ↔ 商品」关系（§3.7）。与一期 `note_goods`（平台 ID 级）
    # 并存：两者维度不同，不是两份实现（见 `NoteGenerationLink` docstring）。
    #: ⚠️ **覆盖同一篇笔记时先清旧关联行**（`UNIQUE(note_pk, shop_product_id)`）——
    #: 重新生成会重走这里，不清就撞唯一约束，整次生成本该成功却报 500。
    for stale in db.scalars(
        select(NoteGenerationLink).where(NoteGenerationLink.note_pk == int(note.id))
    ).all():
        db.delete(stale)
    db.flush()
    db.add(
        NoteGenerationLink(
            user_id=note.user_id,
            note_pk=int(note.id),
            shop_product_id=int(row.id),
            #: **哪次生成产出了这篇**（版本历史 ↔ 笔记的桥）。
            #: ⚠️ 必须写：否则「基于这版只重写文案」时反查不到它产出的笔记
            #: （实测 0 行 = 溯源断了，媒体沿用也就拿不到）。
            generation_record_id=int(record.id),
        )
    )

    #: ⚠️ **平台 ID 级的商品关系必须写**（用户口径 2026-10-19：*「注意笔记发布均需要
    #: 挂当前商品的链接」*）：发布链路 `_goods_item_ids_for()` 读的是
    #: `note_goods`（平台 `item_id` 级）+ `raw_json.item_id`，**不读**下面那张
    #: `NoteGenerationLink`（本地主键级）。只写本地主键级 ⇒ 发布时找不到商品 ⇒
    #: 笔记发出去是「裸笔记」，不挂任何商品链接（实测 note 3886 两张表都空）。
    #: 两套关系维度不同、各司其职，缺一不可（§26.7 / §3.7）。
    #: ⚠️ 覆盖同一篇笔记时先清旧行（`UNIQUE(user_id, note_id, item_id)`）
    for stale_goods in db.scalars(
        select(NoteGoods).where(
            NoteGoods.user_id == note.user_id, NoteGoods.note_id == str(note.note_id or "")
        )
    ).all():
        db.delete(stale_goods)
    db.flush()
    db.add(
        NoteGoods(
            user_id=note.user_id,
            note_id=str(note.note_id or ""),
            item_id=str(row.item_id or ""),
        )
    )
    #: 同一事实写进 `raw_json.item_id`（发布链路的第一优先读点）—— 关系行还没
    #: 落库/被清理时仍认得出「这篇挂的是什么商品」。
    _raw = note.raw_json if isinstance(note.raw_json, dict) else {}
    _raw = dict(_raw or {})
    _raw["item_id"] = str(row.item_id or "")
    note.raw_json = _raw

    record.status = GEN_STATUS_SUCCESS
    #: **整篇生成**才把这一版标成「当前采用版」（新笔记就是用户现在要用的那篇）。
    #: ⚠️ **局部重生成不动采用状态**（用户口径 2026-10-18：*「只是在历史版本进行更新
    #: 生成记录」*）—— 他只是改了这一版的某一块，**没有**说「改用这一版」；
    #: 强行标采用会撞部分唯一索引（实测 `UNIQUE constraint failed:
    #: generation_records.user_id, generation_records.shop_product_id`）。
    #: 采用与否由用户点「采用此版」显式决定（`adopt` 端点）。
    if regen_scope in ("all", "text"):
        #: 整篇 / 旧的「重写文案」= 产出一篇可用的新笔记 ⇒ 顺手标采用。
        #: 细粒度（`title`/`body`/`topics`/`media`）原地改历史版 ⇒ **不动**采用状态。
        note_version_service.mark_adopted(db, record=record)
    #: **记录这一版的形态**（用户口径 2026-10-19：*「生成记录新增一个类型字段区分，
    #: 视频笔记或图文笔记」*）—— 与 `notes.note_form` 同一判据（`plan_form`），
    #: 这样「生成记录列表」不用再从 `media_json` 猜（媒体失败时根本猜不出来）。
    record.note_form = plan_form(plan)
    record.title = generated.title
    record.body = generated.body
    record.topics_json = json.dumps(generated.topics, ensure_ascii=False)
    #: **这一版实际用了哪些图/视频**（2026-10-18）：详情抽屉据此展示配图、
    #: 用户据此勾选「保留哪些 / 重生哪些」。存的是**落库时的最终清单**，
    #: 不是入参 plan —— 段级失败会让实际张数少于计划，记录必须反映实际。
    #: ⚠️ **失败段也要落进来**（用户口径 2026-10-18：*「智能生成图如果为错误则需要
    #: 展示一个站位图，并在图内展示异常原因，tip 展示完整异常原因，支持重新生成
    #: 改部分的图」*）—— 只写成功项的话前端根本不知道「这里本该有一张图但失败了」，
    #: 用户看到的是「少了一张」而不是「这张失败了、原因是什么、可以重试」。
    #: 每条失败段记 `ok=False` + `error`（`role`/`slot_index` 让前端把它放回原位）。
    _failed = [
        {
            "url": "",
            "local_path": "",
            "sort_order": int(seg.get("slot_index") or len(media_entries)),
            "asset_type": str(seg.get("media_type") or "image"),
            "ok": False,
            "error": str(seg.get("error") or "生成失败（未返回原因）"),
            "role": str(seg.get("role") or ""),
            #: ⚠️ **失败段必须带 `source`**（2026-10-19 实测 bug）：前端按 `source`
            #: 把失败归回它那一段再画占位图；这里漏了 ⇒ `source=None` ⇒ 找不到段
            #: ⇒ 用户看到的是「生成后显示」（像是没跑），而不是「生成失败 + 原因」。
            "source": str(seg.get("source") or ""),
        }
        for seg in (media_segments or [])
        if not seg.get("ok")
    ]
    record.media_json = json.dumps(
        [
            {
                "url": str(entry.get("url") or ""),
                "local_path": str(entry.get("local_path") or ""),
                "sort_order": int(entry.get("sort_order") or 0),
                "asset_type": str(entry.get("asset_type") or "image"),
                "ok": True,
                "role": str(entry.get("role") or ""),
                #: **来源与段位**（用户口径 2026-10-19「应记录图片内页来源」）——
                #: 回显时据此把图放回它原来那一段，并在候选图上标勾选。
                "source": str(entry.get("source") or ""),
                "slot_index": int(entry.get("slot_index") or 0),
            }
            for entry in media_entries
        ]
        + _failed,
        ensure_ascii=False,
    )
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
        "media_count": len(media_entries),
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
        media_options = payload.get("media_options")
        #: v2.26（M-C1）：组合计划（缺 ⇒ None，由归一化降级为单来源）。
        media_plan = payload.get("media_plan")
        if not isinstance(media_options, dict):
            media_options = None
        #: 热词驱动（M3）：话题名随 `media_options` 一起落库，这里取出来交给
        #: `generate_full_note(extra=…)` ⇒ 渲染进 Prompt（`{hot_topic}` 插槽）。
        hot_topic = ""
        if media_options:
            hot_topic = str(media_options.get("hot_topic") or "").strip()
        #: **文案要求**（用户口径 2026-10-17）：随行体落库 ⇒ 这里读出并规整一次。
        #: ⚠️ 再规整而不是直接用：行体是**历史数据**（可能由老版本写入、或缺键），
        #: 直接信任会让「缺字段 ⇒ KeyError」变成整篇失败。
        copy_req = copy_requirements.normalize(payload.get("copy_requirements") or {})
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
                db, user, BUSINESS_KEY_LLM_NOTE_TEXT
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
        #: **原地重生成要先用原值**：本行马上会被改判/改写，必须在调模型之前
        #: 把「改动前」的标题/正文/话题存下来，作为「其余两块保持不变」的来源。
        #: **优先用入队时存的快照**（`start_llm_generate_note` 在改写行之前落的），
        #: 那里的值才是「用户点重生成之前」的原文；`record` 上的值此时可能已被复位。
        _text_snapshot = payload.get("text_snapshot")
        if not isinstance(_text_snapshot, dict):
            _text_snapshot = (
                _snapshot_record_text(record) if record is not None else None
            )
        #: **只重配图片**（`media`）：不调文案模型，直接用当前版本的标题/正文/话题。
        #: ⚠️ 这样既省一次文本调用，也保证「用户满意的文案」一个字都不变
        #: （重新生成必然措辞不同 —— 那正是用户在避免的事）。
        _regen_scope = str(payload.get("regen_scope") or "all")
        try:
            if _regen_scope == "media":
                generated = _load_target_text(db, record)
            else:
                generated = generate_full_note(
                    db=db,
                    user_id=user_id,
                    product=load_product_dict_from_ledger(row),
                    model_config=context,
                    api_key=context.api_key,
                    skeleton=skeleton,
                    modules=modules,
                    #: 热词驱动：把话题交给 Prompt 渲染（模板可用 `{hot_topic}` 引用；
                    #: **模板没引用就当没有** —— 与 `extra` 的既有语义一致，不硬塞）。
                    #:
                    #: ⚠️ **字数 / 表情 / 话题数量 / 用户补充段也在这一步拼接**
                    #: （用户口径 2026-10-17：*「字数，表情以及话题设置均有默认值，
                    #: 公共模板中应无需固定添加固定提示词，调用大模型时需最终拼接」*）。
                    #: 模板里只写槽位名（`{title_length}` 等），数值由这里填 ——
                    #: 于是模板不含任何写死的数量，改前端设置即全局生效。
                    extra={
                        **({"hot_topic": hot_topic} if hot_topic else {}),
                        **copy_requirements.build_slots(copy_req),
                    },
                    instruction=_compose_instruction(
                        copy_req,
                        scope=_regen_scope,
                        snapshot=_text_snapshot,
                    ),
                    #: **只要求目标块**：局部重生成的提示词明说「只输出正文」，
                    #: 按整篇判「缺标题」就自相矛盾（其余块由 `_merge_text_scope` 补回）。
                    required_fields=_required_fields_for(_regen_scope),
                    #: 把**页面上的配置参数**交给生成函数做兜底纠偏（标题超长裁掉、
                    #: 话题超量截断）—— 参数已进提示词，但模型不一定遵守
                    #: （实测要求正文 30–150 字、实际写了 769 字）。
                    copy_requirements=copy_req,
                )
                #: **逐块合并**（用户口径 2026-10-18：*「后端应支持每个文案区域
                #: 单独重新生成」*）—— 模型这次会给出三块全新文字（标题/正文/话题），
                #: 但用户只要求换其中一块：另外两块**从当前版本原样搬回来**。
                #: ⚠️ 不可省：只重写正文却把标题也换掉 =「点一块、改三块」。
                if _regen_scope in REGEN_TEXT_FIELD_SCOPES:
                    generated = _merge_text_scope(
                        db,
                        record=record,
                        generated=generated,
                        scope=_regen_scope,
                        snapshot=_text_snapshot,
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
        try:
            outcome = _persist_generated_note(
                db,
                user=user,
                row=row,
                account_id=owner_account_id,
                generated=generated,
                record=record,
                media_source=media_source,
                media_options=media_options,
                media_plan=media_plan,
                #: ⚠️ **必须传**（2026-10-18 实测踩过）：不传 ⇒ 函数内取默认 `all`
                #: ⇒ `mark_adopted` 照跑 ⇒ 局部重生成把历史版标成采用版
                #: ⇒ 撞部分唯一索引 `UNIQUE constraint failed: generation_records...`。
                regen_scope=_regen_scope,
            )
        except MediaGenerationError as error:
            # ---- 媒体配置级失败（业务点未配 / 素材不存在）⇒ 整条失败（不落半条笔记） ----
            db.rollback()
            _mark_record_failed(
                db, item=item, user_id=user_id, code=error.code, message=error.message
            )
            db.flush()
            note_queue_service.finish_item(
                db, item, ok=False, reason=error.message, code=error.code
            )
            return
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
