"""商品笔记生成（M4 起步）：`POST /api/xhs/product-notes/generate`。

设计真相源：`docs/02_设计文档/07_功能设计_发布链路.md` §12（生成与护栏契约）、
`docs/02_设计文档/13_功能设计_账号矩阵同步.md` §31（智能生成与发布）、§31.10（走队列）。

## 这一版**故意**只做「干运行」

| 做 | 为什么 |
|---|---|
| 按 `item_id` 从千帆**只读**取回这一个商品（1 次请求） | 事实来源必须是平台当下的数据，不是本地副本（07 §10 口径更正） |
| 用商品字段构造 Prompt → 调文本模型 → 解析成 `{title, body, tags}` | T-401 |
| 生成后跑护栏（违禁词 / 绝对化 / 功效 / 价格一致性）+ 格式校验 | T-403 / T-405 |
| 返回**判定结果 + 正文**（被拦也返回正文） | 页面要能显示「为什么被拦」，静默丢弃等于用户以为模型没输出 |

| 不做 | 归谁 |
|---|---|
| 落库 / 审核闸门 / `prompt_version` | T-402 / T-406（需要 `product_note_jobs` 表，属 M2 之后） |
| 发布、挂链 | M5（写接口，本项目当前零触碰） |

## 落库的两个入口都走队列（§31.10，2026-09-25 用户口径）

`/generate-save`（单篇）与 `/batch-generate`（批量）**同步段只做门禁 + 入队**，
立即返回入队回执；三步生成 + 轮询 + 落库在后台 worker 逐篇跑。终态与真实原因走
`GET /generate-queue` 快照（与发布 / 同步队列同一个出口、同一个底座）。

## 为什么每次都要取一次商品（而不是缓存）

价格/库存会变，而「生成的笔记里写了旧价格」= 违规（07 §9「发布前复核商品状态与价格」）。
缓存 = 第二套真相源。代价是 1 次只读请求，可接受（与「按需补齐每页 ≤1 次」同一纪律）。
"""
from __future__ import annotations

import json
import time

from typing import Any, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory
from backend.app.api.ai import _business_model_context, get_text_ai_client
from backend.app.api.platforms.xhs.qianfan import _owned_account
from backend.app.core.business_provider_keys import BUSINESS_KEY_LLM_NOTE_TEXT
from backend.app.core.database import SessionLocal, get_db
from backend.app.core.deps import get_current_user
from backend.app.core.perm_deps import require_perm

#: **同步发布最长等待**（秒）——发布链路上传 + 预审核 + 平台调用，实测单篇 20~90 秒；
#: 给 180 秒余量。超时返回 504（**不假装成功**），用户可去内容库看终态。
PUBLISH_WAIT_SECONDS = 180
#: 等待期间的轮询间隔（秒）——1 秒足够，库是本地 SQLite。
PUBLISH_WAIT_POLL_SECONDS = 1.0
from backend.app.models.generation import MEDIA_SOURCES, MEDIA_SOURCE_PRODUCT_IMAGE
from backend.app.models.platform_account import PlatformAccount
from backend.app.models.user import User
from backend.app.services import copy_requirements, note_version_service
from backend.app.services.note_gen_llm_service import NoteGenerationError
from backend.app.services.note_gen_llm_worker import (
    GEN_LLM_BATCH_MAX_ITEMS,
    llm_generate_queue_snapshot,
    start_llm_batch_generate,
    regen_llm_note_sync,
    generate_llm_note_sync,
    start_llm_retry,
)
from backend.app.services.product_note_gen_service import (
    GENERATE_BATCH_MAX_ITEMS,
    ProductNoteGenerationError,
    generate_queue_snapshot,
    start_batch_generate,
    start_generate_note,
)
from backend.app.services.product_note_service import generate_product_note
from backend.app.services.shop_binding_service import (
    qianfan_cookies_for_account,
    set_shop_state,
)
from backend.app.services.shop_products_service import normalize_item_from_raw

router = APIRouter(prefix="/xhs/product-notes", tags=["xhs-product-notes"])


class GenerateProductNoteRequest(BaseModel):
    """干运行入参。"""

    platform_account_id: int
    #: 商品 ID（千帆 `item_id`）。**不接受自由文本商品名** —— 名字可能匹配到多个商品，
    #: 而「写错商品」是这个功能最严重的错误类型（笔记挂错链接 / 价格对不上）。
    item_id: str = Field(min_length=1, max_length=64)
    #: 可选风格要求（长度有界，避免把整篇参考文塞进 Prompt）。
    instruction: str = Field(default="", max_length=400)
    #: 已选素材张数；不传 = 生成阶段先不校验图片张数（素材在发布前才定）。
    image_count: int | None = Field(default=None, ge=0, le=30)


class GenerateAndSaveNoteRequest(BaseModel):
    """智能生成 + 落库（设计 13 §31.4）：与干运行的区别就是**写库**。"""

    platform_account_id: int
    item_id: str = Field(min_length=1, max_length=64)


class BatchGenerateNotesRequest(BaseModel):
    """批量智能生成（设计 13 §31.4 步 6）：顺序处理，单项失败不阻断。"""

    platform_account_id: int
    item_ids: list[str] = Field(min_length=1)
    max_items: int = Field(default=20, ge=1, le=20)


def _fetch_product(db: Session, account: PlatformAccount, item_id: str, adapter_factory) -> dict[str, Any]:
    """按 `item_id` 精确取回商品（`whole_keywords` 支持逗号分隔多 ID，实测可精确命中）。

    **取票只走唯一入口** `qianfan_cookies_for_account`（账号自己的 `ark` 通道）——
    本函数曾**绕过**取票函数直读绑定行，于是「给账号补上自有千帆令牌」在商品笔记
    生成这条路上根本不生效（一个语义两份真相源，2026-09-22 随绑定表一起移除）。

    ADR-021（2026-09-30）：**门禁就是下面这次取票**（拿不到令牌 ⇒ 结构化失败），
    不再先查「有没有绑店」—— 判据是凭据，不是关系。
    """
    cookies = qianfan_cookies_for_account(db, account.id)
    if not cookies:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "ARK_SESSION_REQUIRED",
                "message": "本账号没有可用的千帆登录态，无法生成商品笔记。请先登录该账号的千帆。",
            },
        )
    adapter = adapter_factory(cookies)
    success, message, raw = adapter.search_items(
        page_no=1,
        page_size=5,
        keyword=item_id,
        status="all",  # 已下架商品也要能查出来（否则「商品不存在」与「登录态失效」分不清）
    )
    if not success:
        reason = message or "千帆商品查询失败"
        set_shop_state(account, status="invalid", status_message=reason)
        db.commit()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=reason)

    data = raw.get("data") if isinstance(raw, dict) else None
    data = data if isinstance(data, dict) else {}
    items = [row for row in (data.get("items") or []) if isinstance(row, dict)]
    for row in items:
        if str(row.get("item_id") or "") == item_id:
            # 归一化与商品列表**同一份实现**（`shop_products_service`，v1.32 起
            # 唯一契约构造函数）；区别只在数据来源：这里要平台**当下**数据
            # （07 §10：价格会变，生成链路不许用本地副本），商品列表走台账。
            return normalize_item_from_raw(row)
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "QIANFAN_ITEM_NOT_FOUND",
            "message": f"该店铺里找不到商品 {item_id}（可能不属于本店铺，或已被删除）。",
        },
    )


@router.post("/generate")
def generate_note_for_product(
    payload: GenerateProductNoteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
    text_ai_client=Depends(get_text_ai_client),
):
    """生成一篇**挂载指定商品**的笔记草稿内容（干运行：不落库、不发布）。

    | 约定 | 做法 |
    |---|---|
    | 商品来源 | 千帆**实时**只读取回（1 次），不用本地缓存（07 §10 口径更正） |
    | 生成 | 商品专用 Prompt（只引用给定事实）+ JSON 输出解析 |
    | 护栏 | 生成后跑：违禁词 / 绝对化用语 / 功效宣称 / **价格一致性**（正文价格必须在商品价格区间内） |
    | 格式 | 标题 ≤20 字、正文 300–800 字（传了 `image_count` 才校验图片张数） |
    | **被拦也返回正文** | 响应带 `ok=false` + `block_reason` + 命中词位置，页面能高亮复核 |
    | 失败可判别 | 未绑定 → 400 `QIANFAN_BINDING_REQUIRED`；商品不在本店铺 → 404 `QIANFAN_ITEM_NOT_FOUND`；未配置文本模型 → 400 `AI_MODEL_NOT_CONFIGURED`；模型拒绝（如 401）→ 502 + 平台原文 |
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    product = _fetch_product(db, account, payload.item_id, adapter_factory)

    try:
        model_config, api_key = _business_model_context(
            db, current_user, BUSINESS_KEY_LLM_NOTE_TEXT
        )
    except HTTPException as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "AI_MODEL_NOT_CONFIGURED",
                "message": error.detail if isinstance(error.detail, str) else "未配置可用的文本模型。",
            },
        )

    try:
        result = generate_product_note(
            client=text_ai_client,
            model_config=model_config,
            api_key=api_key,
            product=product,
            instruction=payload.instruction,
            image_count=payload.image_count,
            platform="xhs",
        )
    except ValueError as error:
        # 护栏与格式之外的问题（模型没按 JSON 回、返回为空）——**照实报**，不要编一段正文
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "AI_GENERATION_FAILED", "message": str(error)},
        )
    except requests.RequestException as error:
        # **模型服务本身不可用**（本机实测：api.deepseek.com 返回 401）——这是最常见的一种
        # 「生成失败」，必须给可读原因而不是 500：500 会让「凭据过期」看起来像「代码崩了」。
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", "")
        detail = f"模型服务返回 {status_code}" if status_code else "模型服务连接失败"
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "AI_PROVIDER_ERROR",
                "message": f"{detail}（{model_config.model_name} @ {model_config.base_url}）。"
                "请检查模型配置里的 API Key 是否有效。",
            },
        )

    return {
        "platform_account_id": payload.platform_account_id,
        "item_id": payload.item_id,
        "model": model_config.model_name,
        "note": {
            "title": result.title,
            "body": result.body,
            "tags": result.tags,
        },
        "guardrails": result.guardrails.as_dict(),
        "format": result.format.as_dict() if result.format else None,
        "ok": result.ok,
        "block_reason": result.block_reason,
        #: 这次一致性的比对基准（商品字段白名单）——让「凭什么判违规」可复核。
        "product": result.product,
        #: 干运行：**没有**写库、没有生成发布任务。
        "dry_run": True,
    }


def _generation_error(error: ProductNoteGenerationError) -> HTTPException:
    """生成失败 → 结构化 HTTP 错误（平台原文不替换，DEF-058）。

    状态码口径与干运行端点一致：门禁类（ardm 登录态 / 无令牌）400、商品不在店铺 404、
    平台侧失败 502。

    ⚠️ ADR-021（2026-09-30）：门禁码已统一为 `ARK_SESSION_REQUIRED`（「本账号
    ark 登录态是否可用」是唯一尺子，`shop_id_fk` 绑店判据随店铺实体退场）。
    旧的 `QIANFAN_BINDING_REQUIRED` 仍在元组里，只为兼容历史在途错误对象——
    漏掉新码的后果是「门禁失败被当成平台故障」落 502（前端会显示成后端崩了）。
    """
    if error.code == "QIANFAN_ITEM_NOT_FOUND":
        http_status = status.HTTP_404_NOT_FOUND
    elif error.code in (
        "ARK_SESSION_REQUIRED",
        "QIANFAN_CREDENTIAL_MISSING",
        "QIANFAN_BINDING_REQUIRED",
    ):
        http_status = status.HTTP_400_BAD_REQUEST
    else:
        http_status = status.HTTP_502_BAD_GATEWAY
    return HTTPException(http_status, detail={"code": error.code, "message": error.message})


@router.post("/generate-save")
def generate_and_save_note(
    payload: GenerateAndSaveNoteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
    _perm: None = Depends(require_perm("xhs:item:generateNote")),
):
    """**智能生成一篇商品笔记并落库为「待发布」**（设计 13 §31.4，用户需求③；§31.10 起走队列）。

    与干运行端点（`/generate`）的分工：干运行是「看一眼草稿」（本地模型、
    不落库）；本端点是「落库待发布」（**千帆后台智能生成**，写 `notes` +
    `note_assets` + `note_goods`）。

    | 约定 | 做法 |
    |---|---|
    | 生成 | 千帆三步链路：`aigc-publish/attributes` → `note/sellerpoint/aigc` → 轮询 `aigctask/result`（真机抓包，无签名） |
    | 商品 | **本地台账** `shop_products`（同一行同时供媒体链接与挂链关系，§31.4 约定 1） |
    | 话题 | 平台 AI 结果自带 `topics[{id,name}]` → `raw_json.aigc`（发布的 `hash_tag` 直接用平台 ID） |
    | 媒体 | 商品图直链落 `note_assets`（发布时再下载重传） |
    | 笔记 ID | `local_<uuid>`（未发布不存在三方 ID；发布成功回写真 ID） |
    | 状态 | `publish_status=pending`、`note_type=local_generated`、`published=False` |
    | 护栏 | 记录不拦截（内容出自平台官方 AI，§31.4 约定） |
    | **异步** | 同步段只做门禁 + 入队，**毫秒级返回**；三步 + 轮询 + 落库在后台 worker 跑（§31.10） |
    | 失败 | 门禁失败（未绑店 / 无令牌 / 台账无此行）400/404 结构化；平台失败 502 + 平台原文，**不落库**（无半条笔记）—— 两者都在快照 `finished` 里再给一次 |

    终态不靠响应体：前端轮询 `GET /generate-queue`（与批量同一个出口）。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    try:
        receipt = start_generate_note(
            db, current_user.id, account, payload.item_id, adapter_factory=adapter_factory
        )
    except ProductNoteGenerationError as error:
        raise _generation_error(error)
    return {
        "ok": True,
        "platform_account_id": payload.platform_account_id,
        **receipt,
    }


@router.post("/batch-generate")
def batch_generate_notes_endpoint(
    payload: BatchGenerateNotesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
    _perm: None = Depends(require_perm("xhs:item:generateNote")),
):
    """批量智能生成入队（设计 13 §31.4 步 6 + §31.10；用户需求⑤）。

    逐项过门禁（绑店 → ark 令牌 → 台账商品），通过项按序入队；**单项失败不阻断**
    （与千帆页面 `Promise.allSettled` 同语义）。响应给逐项回执，前端汇总
    「已入队 X / 失败 Y」并列出失败原因；**逐项终态**（生成成功 / 平台失败）
    走 `GET /generate-queue` 快照 `finished` —— 与单篇同一个出口。

    `max_items` 上限与 `GENERATE_BATCH_MAX_ITEMS`（20）对齐：批量是显式动作，
    超限分多次即可。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    return start_batch_generate(
        db,
        current_user.id,
        account,
        payload.item_ids,
        adapter_factory=adapter_factory,
        max_items=min(payload.max_items, GENERATE_BATCH_MAX_ITEMS),
    )


@router.get("/generate-queue")
def get_generate_queue(
    platform: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """生成进度快照（商品列表页「生成进度卡」；设计 13 §31.10）。

    回**当前用户**的生成队列：`running` / `queued`（FIFO 序，条目带 `item_id` /
    `title`）+ 计数（`generated` 已生成 / `failed` 失败 / `total` 本轮总数）+
    `finished`（已出结果的逐项 `ok` 与 `reason` —— 生成失败不落库到笔记行，
    原因只在这里给一次）。

    ⚠️ 键名是 `generated` 不是底座通用形状的 `synced`（改名由
    `generate_queue_snapshot` 做，直接透传底座结果 = 进度卡三数恒 0 而后端不报错）。
    """
    return generate_queue_snapshot(db, current_user.id, platform=platform)


# --------------------------------------------------------------------------- #
# LLM 生成链路（设计 17 §7.4，T6）
#
# 与上面一期 `/generate-*` 的分工（**两队列并存，不是替代**）：
#
# | 维度 | 一期 `/generate-*` | 本期 `/generate-llm*` |
# |---|---|---|
# | 内容来源 | 千帆后台智能生成（平台 AI） | 本项目调 OpenAI 兼容上游（本地 Prompt） |
# | 平台依赖 | 需千帆登录态（`ARK_SESSION_REQUIRED`） | **零平台请求**（只读本地台账） |
# | 护栏 | 不拦截（内容出自平台官方 AI） | **拦截即失败**，不落半条笔记（§4.7） |
# | 提示词 | 平台固定 | 模板 + 模块（T2 可配） |
# | 审计 | 队列项 payload | **`generation_records` 持久行**（跨重启留存） |
#
# 请求体统一是**平台标识**（`platform_account_id` + `item_id`），与前端商品列表
# 的选中项同形（不用本地主键 —— 前端的天然标识是平台 ID）；**端点层**解析成
# 台账行主键后传给服务层（服务层只认主键，精确且不含歧义）。
# --------------------------------------------------------------------------- #
class GenerateLlmNoteRequest(BaseModel):
    """LLM 生成单篇入参（设计 17 §7.4）。"""

    platform_account_id: int = Field(ge=1)
    #: 商品平台 ID。⚠️ `item_id` **只在账号内唯一**（`uq_shop_products_account_item`）
    #: ⇒ 必须与 `platform_account_id` 一起用才唯一定位；单给 `item_id` 会有歧义。
    item_id: str = Field(min_length=1, max_length=64)
    template_id: int | None = Field(default=None, ge=1)
    #: 网关配置 id（v1.7 只作审计快照；实际调用取用户的默认文本模型）。
    gateway_config_id: int | None = Field(default=None, ge=1)
    #: ⚠️ **已停用**（用户口径 2026-10-16：*「不允许随意设置模型名」*）。
    #:
    #: 模型由**业务点映射**决定（`resolve_business_provider`：`generate_note` → 服务商
    #: → 默认模型）。原先这里允许前端覆盖 ⇒ 等于给映射开了后门：
    #: 填一个没配的模型名，**入队时看不出问题、运行期才 502**；而且同一业务点
    #: 在「模型配置页」和「弹窗」两处各说各话（两份真相源）。
    #: 现在**传非空即 400**（不静默忽略 —— 静默忽略会让调用方以为改了生效）。
    #: 字段本身保留（去掉会让老客户端的请求体校验失败，是更差的行为）。
    model_name: str = Field(default="", max_length=128)
    #: 媒体来源四选一（设计 17 §13.4.1）：`product_image` / `material` /
    #: `reference_image` / `ai_generate`。
    media_source: str = Field(default=MEDIA_SOURCE_PRODUCT_IMAGE, max_length=32)
    #: **媒体组合计划**（v2.26，M-C1，设计 41 §11.2）：有序段列表，每段
    #: `{source, role, count?, …来源参数}`。**给了它就以它为准**（`media_source` 变兜底）。
    #: 规则：封面唯一且排第一（R1）/ 总张数上限（R2）/ 同来源可重复（R3）/
    #: 视频不占图片位（R4）。非法 ⇒ 400 结构化（不静默回落）。
    media_plan: list[dict[str, Any]] = Field(default_factory=list)
    #: 媒体参数载体（§13.4.1）：`material` ⇒ `material_file_ids`；
    #: `reference_image` ⇒ `reference_image_urls?`；`ai_generate` ⇒
    #: `prompt?` / `reference_image_urls?` / `count?`。不落库，只进队列项行体。
    media_options: dict[str, Any] = Field(default_factory=dict)
    #: **文案要求**（用户口径 2026-10-17）：标题 / 正文 / 话题**各自**的提示词、
    #: 字数区间、是否带表情、话题数量区间。空 dict ⇒ 全走默认
    #: （`services/copy_requirements.CopyRequirements` 的默认值）。
    #: ⚠️ 校验在**同步段**做（`copy_requirements.normalize`）：区间倒挂等表意错误
    #: 当场 400，不进队列 —— 入了队才失败，用户要等几分钟才知道自己填错了。
    #: ⚠️ **不能叫 `copy`**：Pydantic v2 的 `BaseModel` 已有 `copy()` 方法，
    #: 同名字段会遮蔽它（`UserWarning: shadows an attribute in parent`），
    #: 任何 `model.copy()` 调用会变成「取字段」而不是「复制模型」。
    copy_requirements: dict[str, Any] = Field(default_factory=dict)


class BatchGenerateLlmNotesRequest(BaseModel):
    """LLM 批量生成入参（设计 17 §7.4）。"""

    platform_account_id: int = Field(ge=1)
    item_ids: list[str] = Field(min_length=1)
    template_id: int | None = Field(default=None, ge=1)
    gateway_config_id: int | None = Field(default=None, ge=1)
    #: ⚠️ **已停用**（同单篇：模型走业务点映射，不允许客户端指定）。传非空即 400。
    model_name: str = Field(default="", max_length=128)
    #: 媒体来源四选一（同单篇，设计 17 §13.4.1）。
    media_source: str = Field(default=MEDIA_SOURCE_PRODUCT_IMAGE, max_length=32)
    #: 媒体参数载体（§13.4.1；同单篇）。
    media_options: dict[str, Any] = Field(default_factory=dict)
    #: 上限与一期 `GENERATE_BATCH_MAX_ITEMS` 对齐（批量是显式动作，超限分多次）。
    max_items: int = Field(default=GEN_LLM_BATCH_MAX_ITEMS, ge=1, le=GEN_LLM_BATCH_MAX_ITEMS)


def _llm_generation_error(error: NoteGenerationError) -> HTTPException:
    """LLM 生成失败 → 结构化 HTTP 错误（设计 17 §7.4；平台原文不替换，DEF-058）。

    码 → 状态码（门禁类 400 / 找不到 404 / 其余 400 带原因，**不落 5xx**）：
    本链路的失败几乎都是「配置问题或数据问题」，都能由用户自己修 —— 落 5xx
    会让前端显示成「后端崩了」（一期 `_generation_error` 的同一教训）。
    """
    not_found = {"LLM_PRODUCT_NOT_FOUND", "LLM_TEMPLATE_NOT_FOUND", "LLM_RECORD_NOT_FOUND"}
    http_status = status.HTTP_404_NOT_FOUND if error.code in not_found else status.HTTP_400_BAD_REQUEST
    return HTTPException(http_status, detail={"code": error.code, "message": error.message})


def _ledger_product(
    db: Session, current_user: User, account: PlatformAccount, item_id: str
) -> dict[str, Any]:
    """`(账号, item_id)` → 商品事实字典（**实时查千帆**，缺值不造假）。

    ⚠️ 设计 75 S5（2026-10-27）：原来是「查本地台账行（零平台请求）」。台账表
    （`shop_products`）已随迁移 `n8o9p0q1r2s3` 整表 DROP，用户口径也明确
    「展示线上数据，实时查询线上数据」⇒ 生成链路取商品的**平台当下值**
    （价格会变，这也正是生成要写进笔记的内容，用历史副本反而是错的）。

    归一化与商品列表**同一份实现**（`normalize_item_from_raw`）——
    两个入口显示不同字段是 DEF-044 的同型病。

    失败分三档（不把凭据问题报成「商品不存在」）：
    平台正常但没这件商品 ⇒ 404 `LLM_PRODUCT_NOT_FOUND`；
    平台报错 ⇒ 502 带平台原话；其它 ⇒ 原样抛。
    """
    from backend.app.services import realtime_platform_query_service as rt
    from backend.app.services.shop_products_service import normalize_item_from_raw

    adapter = _ark_adapter(db, account)
    try:
        result = rt.list_products(
            adapter, keyword=str(item_id or "").strip(), status="all", page=1, page_size=20
        )
    except rt.RealtimeQueryError as error:
        code = getattr(error, "code", "QIANFAN_QUERY_FAILED")
        raise HTTPException(
            status_code=(
                status.HTTP_400_BAD_REQUEST
                if code == "QIANFAN_CREDENTIAL_EXPIRED"
                else status.HTTP_502_BAD_GATEWAY
            ),
            detail={
                "code": code,
                "message": getattr(error, "message", str(error)),
            },
        ) from error
    for row in result.get("items") or []:
        if str(row.get("item_id") or "") == str(item_id or "").strip():
            return normalize_item_from_raw(row)
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "LLM_PRODUCT_NOT_FOUND",
            "message": f"平台上找不到商品 {item_id}（可能已被删除，或不属于该账号）。",
        },
    )


def _ark_adapter(db: Session, account: PlatformAccount):
    """按账号取千帆适配器（**实时查询的唯一取法**，不借任何别的凭据）。"""
    cookies = qianfan_cookies_for_account(db, account.id)
    if not cookies:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "ARK_SESSION_REQUIRED",
                "message": "该账号没有可用的千帆（ark）登录态，无法读取商品信息。",
            },
        )
    from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory

    return get_qianfan_api_adapter_factory()(cookies)


def _check_media_source(value: str) -> str:
    """媒体来源白名单（非法值 ⇒ 400，而不是静默落到默认值）。

    静默回落 = 用户选了「AI 生成图」却拿到商品图，且没有任何提示 ——
    比报错难查（DEF-058 的「真异常要红块 + 真原因」）。
    """
    if value not in MEDIA_SOURCES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "LLM_MEDIA_SOURCE_INVALID",
                "message": f"媒体来源不支持：{value}（可选：{'/'.join(MEDIA_SOURCES)}）。",
            },
        )
    return value


def _checked_media_plan(media_plan: list[dict[str, Any]], media_source: str) -> list[dict]:
    """入参 → 已校验的 `media_plan`（v2.26，M-C1）。

    ⚠️ **在端点层校验**（不在 worker 里）：规则违反是**确定性的用户输入错误**，
    必须**入队前**给结构化 400 —— 入队后才失败会让用户去队列记录页翻原因，
    而真正要改的是他填的那个组合（DEF-058 同型的「错误要靠前报」）。
    """
    from backend.app.services.media_plan import MediaPlanError, build_plan

    try:
        return build_plan(media_plan=media_plan or None, media_source=media_source)
    except MediaPlanError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": error.code, "message": error.message},
        ) from error


def _checked_copy_requirements(raw: dict[str, Any]) -> copy_requirements.CopyRequirements:
    """文案要求入参 → 规整对象（非法 ⇒ 结构化 400）。

    ⚠️ **同步段必须校验**：区间倒挂（min > max）、表情取值非法都是**用户表意错误**，
    当场告诉他比让他等几分钟看一条失败记录好得多。确定性答案 ⇒ 不入队。
    """
    try:
        return copy_requirements.normalize(raw)
    except copy_requirements.CopyRequirementError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": error.code, "message": error.message},
        ) from error


def _reject_model_name_override(model_name: str) -> None:
    """**模型名不允许客户端指定**（用户口径 2026-10-16：*「不允许随意设置模型名」*）。

    模型由**业务点映射**决定（`resolve_business_provider`）；允许覆盖 = 给映射开后门：
    填一个没配的模型名，**入队时看不出问题、运行期才 502**，而且同一业务点在
    「模型配置页」和「弹窗」两处各说各话。

    ⚠️ **传非空即 400，不静默忽略** —— 静默忽略会让调用方以为「我改了模型」，
    实际没用上（本仓最厌恶的静默失效）。要换模型 ⇒ 去「模型配置」页改业务点映射。
    """
    if str(model_name or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "LLM_MODEL_OVERRIDE_NOT_ALLOWED",
                "message": "模型由业务点映射决定，不支持在请求里指定模型名；请到「模型配置」页调整映射。",
            },
        )


@router.post("/generate-llm")
def generate_llm_note(
    payload: GenerateLlmNoteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    _perm: None = Depends(require_perm("xhs:item:generateNote")),
):
    """**同步生成一篇 LLM 笔记并直接返回结果**（用户口径 2026-10-18）。

    > 「调整一下生成笔记**不要放入队列中直接同步等待**，因为用户生成后应支持用户
    >   对生成后的结果部分位置进行重复生成，也允许用户对生成图视频等选择性保留
    >   或重新生成补充等」

    ## 与「入队」的差别（为什么改）

    入队是「毫秒级回执 + 轮询看终态」，它适合**批量**（20 篇排队跑几分钟）。
    但单篇生成的真实诉求是**看到结果就地调整**：用户点完就要看到标题/正文/话题/图，
    然后决定「这段重写」「这图换一张」。

    排队路径把这件事拆成了两段（入队 → 轮询 → 再点开版本记录），
    中间还有「到底跑没跑」的不确定感。同步路径一步到位。

    ## 契约

    * 成功 ⇒ `{ok: true, record_id, note_id, title, body, topics, media[], copy_requirements}`
      —— 前端拿到就能渲染，不必再查版本列表；
    * 失败 ⇒ 结构化 4xx/5xx（`LLM_GATEWAY_NOT_CONFIGURED` / `LLM_RESPONSE_NOT_JSON` /
      `LLM_GUARDRAIL_BLOCKED` / 上游超时等），**原因可读**；
    * 重复点：同商品**未终态**的排队项仍会被幂等复用（不重复生成），
      但同步路径本身**不再产生排队项**。

    ⚠️ 超时上限：文本调用 `TEXT_COMPLETION_TIMEOUT`（300s），媒体生成各自有上限。
    前端必须显示「生成中 + 已用 N 秒」，不能让页面看起来卡死。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    row = _ledger_product(db, current_user, account, payload.item_id)
    #: 模型名不许客户端指定（用户口径 2026-10-16）—— **同步段**拒，不入队。
    _reject_model_name_override(payload.model_name)
    media_source = _check_media_source(payload.media_source)
    #: v2.26（M-C1）：组合计划（**组合优先，单值兜底**）；非法 ⇒ 结构化 400。
    plan = _checked_media_plan(payload.media_plan, media_source)
    #: **文案要求**（用户口径 2026-10-17）：同步段规整 —— 区间倒挂等表意错误当场 400
    #: （跑几分钟才失败，用户要等半天才知道自己填反了）。
    copy_req = _checked_copy_requirements(payload.copy_requirements)
    try:
        result = generate_llm_note_sync(
            db,
            current_user.id,
            account_id=int(account.id),
            item_id=str(row.get("item_id") or payload.item_id),
            template_id=payload.template_id,
            gateway_config_id=payload.gateway_config_id,
            model_name=payload.model_name,
            media_source=media_source,
            media_plan=plan,
            copy_requirements=copy_req,
        )
    except NoteGenerationError as error:
        raise _llm_generation_error(error)
    return {
        "ok": True,
        "platform_account_id": int(account.id),
        "item_id": payload.item_id,
        **result,
    }


@router.post("/batch-generate-llm", include_in_schema=False)
def batch_generate_llm_notes(
    payload: BatchGenerateLlmNotesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**已退场**：批量 LLM 生成（2026-10-18 用户口径）。

    > 「**暂时不支持批量生成**，改成同步等待，调整一下生成笔记不要放入队列中直接
    >   同步等待，因为用户生成后应支持用户对生成后的结果部分位置进行重复生成…」

    批量与「生成后就地调整」是两条相反的路：批量求快（排队跑），单篇求**可控**
    （看到结果马上改文案 / 换图）。用户明确选了后者，且顺序执行 20 篇会远超
    任何 HTTP 超时，所以批量**整条退场**，不是「改成同步批量」。

    * 前端入口同步移除（商品列表的「批量生成智能笔记」按钮）；
    * 端点保留 **410 墓碑**（`include_in_schema=False`，不进 OpenAPI）——
      老客户端/书签拿到明确原因，而不是 404 让人以为路由写错。
    * 服务层的 `start_llm_batch_generate` 暂留（单测仍覆盖它的参数归一），
      但没有 HTTP 出口。
    """
    raise HTTPException(
        status_code=410,
        detail={
            "code": "LLM_BATCH_GENERATE_GONE",
            "message": "批量生成已停用，请逐个商品生成（生成后可对结果局部重复生成）。",
        },
    )


@router.get("/generate-llm-queue")
def get_llm_generate_queue(
    platform: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """LLM 生成进度快照（设计 17 §7.4）：`running` / `queued` + `generated` / `failed` / `total`。

    `finished` 逐项带 `ok` + `reason`；**`reason` 优先取 `generation_records.error_message`**
    （跨重启留存 —— 这正是 T5 三表存在的理由：一期「原因只在轮询窗口给一次」，
    后台线程重启后用户就查不到为什么失败了）。

    ⚠️ 键名 `generated` 不是底座通用形状的 `synced`（改名由
    `llm_generate_queue_snapshot` 做）。
    """
    return llm_generate_queue_snapshot(db, current_user.id, platform=platform)


@router.post("/generate-llm/{record_id}/retry")
def retry_llm_generate(
    record_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    _perm: None = Depends(require_perm("xhs:item:generateNote")),
):
    """重试一条**失败**的 LLM 生成（设计 17 §7.4）。

    语义 = **新队列项 + 原审计记录复位回 `queued`**（不新建审计行 —— 用户视角是
    「这次生成再试一次」，不是两次生成）。只有 `status=failed` 可重试：
    `success` 重试会覆盖已完成产物，`running` 正在跑 ⇒ 一律 400 `LLM_RECORD_NOT_RETRYABLE`。

    重试**不需要**再传商品 / 模板：取原记录上的 `item_id` / `template_id`
    （连同 `gateway_config_id` / `model_name` / `media_source` 一起在新队列项里留痕）。
    """
    try:
        receipt = start_llm_retry(db, current_user.id, record_id=int(record_id))
    except NoteGenerationError as error:
        raise _llm_generation_error(error)
    return {"ok": True, **receipt}


# --------------------------------------------------------------------------- #
# 版本历史（用户口径 2026-10-17：
#   *「LLM 生成笔记记录，且还有一个问题，比如基于指定生成的某一版本，
#     用户只想重新生成正文或只重新配置生成图片」*）
#
# 三条判据（都在服务层实现，端点只做鉴权 + 参数传递）：
#   1. **版本行永不覆盖** —— 每次生成 = 新版本，历史可回溯；
#   2. **同商品最多一条 `is_adopted`** —— 「采用版」是用户的选择，不是时间序；
#   3. **局部重生成覆盖当前版本** —— 版本号不变、不新增行，只替换目标块。
# --------------------------------------------------------------------------- #
class UpdateNoteVersionRequest(BaseModel):
    """**就地编辑某一版文案**（不回模型、不改版本号）。

    ⚠️ 三个字段都是**可选**且 `None` 表示「不改」—— 前端只传改过的那一块，
    不会因为漏传把正文清空。
    """

    title: str | None = None
    body: str | None = None
    topics: list[str] | None = None
    #: **配图**（用户口径 2026-10-19：*「更新记录按钮为保存最新的数据，勾选了新的图片未保存」*）
    #: —— 用户在配置区勾选的图必须能存进这一版，否则「更新记录」只改文案、
    #: 下次采用回来勾选还是旧的。`None` = 不动；给了就**整份替换** `media_json`。
    #: 每项 `{url, local_path?, asset_type?, source?, role?, slot_index?}`。
    media: list[dict] | None = None


class RegenFromVersionRequest(BaseModel):
    """基于某版本**局部重生成**（只重写文案 / 只重配图片）。"""

    #: 要覆盖的那一版（必填；必须是**成功**且**同商品**的版本）。
    target_record_id: int = Field(ge=1)
    #: 重生成范围：`text`（只重写标题/正文/话题，媒体沿用当前版本已有的）
    #: / `media`（只重配图片，文案沿用当前版本已有的）。⚠️ **不接受 `all`** ——
    #: 整篇重新生成走 `/generate-llm`（那里能同时改模板/图片组合，语义更全）。
    regen_scope: str = Field(default="text", max_length=16)
    #: 只重写文案时可换模板（空 = 沿用当前版本那次用的模板）。
    template_id: int | None = Field(default=None, ge=1)
    #: 只重写文案时的文案要求（空 = 沿用当前版本的快照）。
    copy_requirements: dict[str, Any] = Field(default_factory=dict)
    #: 只重配图片时的媒体计划 / 参数（空 = 沿用当前版本已有的）。
    media_plan: list[dict[str, Any]] = Field(default_factory=list)
    media_options: dict[str, Any] = Field(default_factory=dict)


@router.post("/versions/{record_id}/publish")
def publish_note_version(
    record_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """**把某一版生成结果直接发布**（用户口径 2026-10-19：*「LLM 生成笔记弹窗新增一个
    发布笔记按钮，生成用户满意的笔记后可以直接发布」*）。

    为什么按 `record_id` 而不是 `note_id`：弹窗手里只有「当前采用的那一版」，
    而发布链路要的是 `notes.id`（本地主键）。两者由 `notes.raw_json.generation.record_id`
    关联 —— 生成时写死的那一处，**唯一可靠的反查口**（不靠标题/时间猜）。

    发布本身**完全复用**既有链路（`note_publish_service.start_publish`）：
    不新造发布实现，也不跳过任何门禁（ark 令牌 / 媒体 / 已发布 / 发布中）。
    异步语义与原端点一致：立即返回 `publishing`，终态由前端轮询笔记行。
    """
    from backend.app.models.note import Note
    from backend.app.services.note_publish_service import NotePublishError, start_publish

    #: **入口留痕**（诊断用）：确认前端确实打到了这个端点。
    #: 没有这行 = 请求根本没到后端（前端提前失败 / 打错地址 / 产物旧）。
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "PUBLISH-ENDPOINT HIT record_id=%s user=%s", record_id, current_user.id
    )

    try:
        record = note_version_service.find_record(
            db, user_id=int(current_user.id), record_id=int(record_id)
        )
    except note_version_service.VersionError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": error.code, "message": error.message},
        ) from error

    #: 这一版对应的笔记行（生成时 `raw_json.generation.record_id` 写下的一对一关系）。
    #: ⚠️ **找不到就是找不到**：不按标题/时间兜底猜 —— 发错篇比报错更糟。
    #: 判据在 `raw_json.generation.record_id` —— 逐条比对（一个用户几十条，代价可忽略）。
    candidates = db.scalars(
        select(Note).where(
            Note.user_id == int(current_user.id),
            Note.note_type == "local_generated",
        )
    ).all()
    #: ⚠️ **同一 `record_id` 可能对应多篇笔记**（局部重生成覆盖同一版时会另起一行，
    #: 历史上也留下过重复）⇒ 不能「取第一条」：实测拿到的是**没有挂商品**的旧篇
    #: ⇒ 发布出去照样是裸笔记，用户看到「依旧没挂商品」。
    #: 排序判据（按优先级）：
    #:   ① **挂了商品**的优先（否则这次必挂不上）—— 判据 = `raw_json.item_id`
    #:      （设计 75 S5：`note_goods` 关系表已 DROP，`raw_json.item_id` 成为唯一落点）；
    #:   ② **未发布**的优先（避免重复发同一篇，也避免撞平台重复内容限制）；
    #:   ③ 最新的优先（`id` 倒序，最接近用户当前看到的那一版）。
    matched: list[Note] = []
    for row in candidates:
        raw = row.raw_json if isinstance(row.raw_json, dict) else {}
        try:
            payload = json.loads(row.raw_json) if isinstance(row.raw_json, str) else raw
        except ValueError:
            payload = {}
        generation = (payload or {}).get("generation") or {}
        if int(generation.get("record_id") or 0) == int(record.id):
            matched.append(row)
    if not matched:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "LLM_NOTE_VERSION_NOT_PUBLISHABLE",
                "message": "这一版还没有对应的笔记内容，请先生成完整笔记再发布。",
            },
        )

    def _rank(note_row: Note) -> tuple[int, int, int]:
        raw_payload = note_row.raw_json if isinstance(note_row.raw_json, dict) else {}
        has_goods = bool(str((raw_payload or {}).get("item_id") or "").strip())
        not_published = note_row.publish_status != "published"
        return (1 if has_goods else 0, 1 if not_published else 0, int(note_row.id))

    note = max(matched, key=_rank)
    #: ⚠️ **发布前确保商品关系存在**（用户口径 2026-10-19：*「笔记发布均需要挂当前商品的
    #: 链接」*）—— 发布链路 `_goods_item_ids_for()` 认 `raw_json.item_id`。
    #: 本端点手里有 `record.item_id`（平台商品 ID）⇒ 直接补齐，
    #: 不依赖生成侧是否写过（历史笔记就是这样缺的，实测 note 3886 两处都空）。
    #: ⚠️ 设计 75 S5（2026-10-27）：`note_goods` 关系表已 DROP ⇒ **唯一落点**是
    #: `raw_json.item_id`（发布链路 `_goods_item_ids_for()` 读它）。商品 ID 直接取
    #: 记录行上的平台 ID（`generation_records.item_id`），不再回查台账主键。
    item_id = str(record.item_id or "").strip()
    if item_id:
        raw = note.raw_json if isinstance(note.raw_json, dict) else {}
        raw = dict(raw or {})
        raw["item_id"] = item_id
        note.raw_json = raw
        db.commit()
    else:
        #: 取不到商品 ⇒ **不假装挂了**（缺值不造假，硬约束⑤）：明说出原因。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "LLM_NOTE_GOODS_MISSING",
                "message": "这一版没有关联商品，无法挂商品链接，请先重新生成。",
            },
        )

    try:
        start_publish(db, int(current_user.id), int(note.id), adapter_factory=adapter_factory)
    except NotePublishError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": error.code, "message": error.message},
        ) from error

    #: ⚠️ **同步等待终态**（用户口径 2026-10-19：*「发布笔记按钮改为同步按钮，
    #: 发布成功关闭弹窗，失败则异常提示」*）—— 发布内部仍是异步 worker
    #: （不改造那条链路：下载/上传/预审核/发布都在后台跑，是被验证过的路径），
    #: 这里只**等它落终态**再返回：前端拿到 `published` 就关弹窗，
    #: 拿到 `failed` 就弹平台原文。超时不算成功（不假装发布成功）。
    from backend.app.models.note import PUBLISH_STATUS_FAILED, PUBLISH_STATUS_PUBLISHED

    deadline = time.monotonic() + PUBLISH_WAIT_SECONDS
    #: ⚠️ **轮询用独立 Session**：worker 在别的线程写库，本 Session 若持有旧快照
    #: （identity map）会一直读到 `publishing`；`expire_all()` 虽能绕过，但它同时
    #: 让**本 Session 未提交的改动失效**（这里的 `note.raw_json` 刚改过）。
    #: 独立 Session 读到的就是库里真值，且不干扰本请求的事务。
    #: ⚠️ `SessionLocal` 必须**模块级持有**（2026-10-23）：函数级 import 拿不到模块属性，
    #: `patch_service_sessions()` 打桩不到它 ⇒ 测试里这条探测会落到**真实生产库**
    #: （守卫 `test_service_session_registry_covers_every_session_owner` 抓到的正是这个）。
    while time.monotonic() < deadline:
        with SessionLocal() as probe:
            current = probe.get(Note, int(note.id))
        if current is not None and current.publish_status in (
            PUBLISH_STATUS_PUBLISHED,
            PUBLISH_STATUS_FAILED,
        ):
            if current.publish_status == PUBLISH_STATUS_FAILED:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "NOTE_PUBLISH_FAILED",
                        "message": str(current.publish_error or "发布失败（平台未返回原因）"),
                    },
                )
            return {
                "ok": True,
                "note_pk": int(current.id),
                "note_id": str(current.note_id or ""),
                #: ⚠️ **没有 `platform_note_id` 这一列**（`Note` 只有 `note_id`，
                #: 发布成功时它就被改写成平台 ID）—— 原写法在**返回时**抛
                #: `AttributeError` ⇒ 前端看到 500，而平台上**已经发布成功**
                #: （2026-10-19 实测：用户报「500 但实际发成功了」的真根因）。
                "platform_note_id": str(current.note_id or ""),
                "publish": {
                    "ok": True,
                    "status": PUBLISH_STATUS_PUBLISHED,
                    "record_id": int(record.id),
                },
            }
        time.sleep(PUBLISH_WAIT_POLL_SECONDS)
    raise HTTPException(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        detail={
            "code": "NOTE_PUBLISH_TIMEOUT",
            "message": f"发布已提交但 {PUBLISH_WAIT_SECONDS} 秒内未完成，请到内容库查看结果。",
        },
    )


@router.get("/versions")
def list_note_versions(
    platform_account_id: int | None = None,
    item_id: str | None = None,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """某商品的**生成版本历史**（新→旧；含失败版本）。

    ⚠️ 失败版本也在列表里：用户要看到「第 3 次生成了但失败了」，
    否则会以为点了没反应。

    ⚠️ **只按平台标识定位**（设计 75 S5，2026-10-27）：`platform_account_id` + `item_id`。
    原 `shop_product_id`（本地台账主键）参数随台账表 DROP 退场 —— 版本行的分组键
    也换成了平台 `item_id`（`generation_records.item_id`）。
    """
    resolved_id = str(item_id or "").strip()
    if not resolved_id:
        if True:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "LLM_VERSION_QUERY_INVALID",
                    "message": "需要 item_id（平台商品 ID，可配 platform_account_id 指定账号）",
                },
            )
        account = db.get(PlatformAccount, int(platform_account_id))
        if account is None or int(account.user_id) != int(current_user.id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "LLM_ACCOUNT_NOT_FOUND", "message": "账号不存在或不属于当前用户"},
            )

    rows = note_version_service.list_versions(
        db,
        user_id=int(current_user.id),
        item_id=resolved_id,
        limit=max(1, min(int(limit), 200)),
    )
    return {
        "ok": True,
        "item_id": resolved_id,
        "total": len(rows),
        "items": [note_version_service.version_summary(row) for row in rows],
    }


@router.get("/versions/{record_id}")
def get_note_version(
    record_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """单版详情（**含正文全文** + 当时的文案要求快照）。"""
    try:
        record = note_version_service.find_record(
            db, user_id=int(current_user.id), record_id=int(record_id)
        )
    except note_version_service.VersionError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": error.code, "message": error.message},
        ) from error
    summary = note_version_service.version_summary(record)
    try:
        copy_snapshot = json.loads(record.copy_requirements_json or "{}")
    except ValueError:
        copy_snapshot = {}
    #: **完整媒体清单**（用户口径 2026-10-18：*「抽屉中没有图、视频」*）——
    #: 摘要只给封面 + 计数（列表用，避免接口被撑大），详情必须给全清单，
    #: 否则抽屉里看不到这一版到底用了哪些图/视频。
    try:
        media_list = json.loads(record.media_json or "[]")
    except ValueError:
        media_list = []
    media = [m for m in media_list if isinstance(m, dict)] if isinstance(media_list, list) else []
    return {
        "ok": True,
        "item": {
            **summary,
            "body": str(record.body or ""),
            "copy_requirements": copy_snapshot,
            "template_id": int(record.template_id) if record.template_id else None,
            #: `[{url, local_path, sort_order, asset_type}]`（顺序即图序，第 1 张是封面）
            "media": media,
        },
    }


@router.post("/versions/regen")
def regen_from_version(
    payload: RegenFromVersionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**基于某版本局部重生成**（用户口径：*「只想重新生成正文或只重新配置生成图片」*）。

    * `regen_scope=text`  ⇒ 只调文案模型，**媒体沿用当前版本**（不烧一次出图费用）；
    * `regen_scope=media` ⇒ 只跑媒体链路，**文案沿用当前版本**（措辞一个字不变）。

    两种都**覆盖当前这一版**（版本号不变、不新增行），只替换目标块。
    """
    scope = note_version_service.normalize_scope(payload.regen_scope)
    if scope == "all":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "LLM_REGEN_SCOPE_INVALID",
                "message": "整篇重新生成请用 /generate-llm（这里只做局部重生成）",
            },
        )
    try:
        target = note_version_service.validate_target_version(
            db,
            user=current_user,
            target_id=payload.target_record_id,
            scope=scope,
        )
    except note_version_service.VersionError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": error.code, "message": error.message},
        ) from error
    assert target is not None  # scope != all ⇒ validate_target_version 保证非 None

    target_item_id = str(target.item_id or "").strip()
    if not target_item_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "LLM_TARGET_INVALID",
                "message": "这一版没有关联商品，无法重生成",
            },
        )
    #: 商品归属账号：取该版本记录所属的账号（`note_generation_links.account` 不可用时
    #: 退回请求方最近的账号）。⚠️ **不猜平台 ID** —— 记录行的 `item_id` 就是平台 ID。
    from backend.app.models.platform_account import PlatformAccount as _PA

    account = db.scalar(
        select(_PA)
        .where(_PA.user_id == int(current_user.id), _PA.deleted_at.is_(None))
        .order_by(_PA.id.asc())
    )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "LLM_ACCOUNT_NOT_FOUND", "message": "当前账号下没有平台账号"},
        )

    #: 文案要求：给了就用新的，没给就**沿用当前版本的快照**（保持「只改图」时文案参数不变）。
    if payload.copy_requirements:
        copy_req = _checked_copy_requirements(payload.copy_requirements)
    else:
        try:
            snapshot = json.loads(target.copy_requirements_json or "{}")
        except ValueError:
            snapshot = {}
        copy_req = _checked_copy_requirements(snapshot if isinstance(snapshot, dict) else {})

    #: **同步执行**（用户口径 2026-10-18：*「重新生成要同步接口等待」*）——
    #: 原实现只建队列项就返回「已提交（后台执行）」，用户还得去版本列表里翻；
    #: 现在本请求线程跑完才返回，前端拿到 `title/body/topics/media` 直接回填。
    try:
        result = regen_llm_note_sync(
            db,
            int(current_user.id),
            account_id=int(account.id),
            item_id=target_item_id,
            regen_scope=scope,
            target_record_id=int(target.id),
            template_id=payload.template_id or target.template_id,
            media_source=str(target.media_source or MEDIA_SOURCE_PRODUCT_IMAGE),
            media_plan=payload.media_plan or None,
            copy_requirements=copy_req,
        )
    except NoteGenerationError as error:
        raise _llm_generation_error(error)
    return {
        "ok": True,
        "regen_scope": scope,
        "target_record_id": int(target.id),
        **result,
    }


@router.patch("/versions/{record_id}")
def update_note_version(
    record_id: int,
    payload: "UpdateNoteVersionRequest",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**就地编辑某一版的文案**（用户口径 2026-10-19：*「文案也使用回显的方式」*）。

    与「重新生成」的区别：**不调模型、不花钱、不动版本号** —— 用户自己改字，
    改完就存。典型场景：模型写得不错但有个词想换掉、话题想删一个。

    只接受三个可编辑字段（`title` / `body` / `topics`）：
    媒体与生成参数不走这里（媒体有「重新生成 / 编辑回显」，参数在设置里）。
    每个字段**只在传入时才更新**（`None` = 不动），避免前端漏传把内容清空。
    """
    try:
        record = note_version_service.find_record(
            db, user_id=int(current_user.id), record_id=int(record_id)
        )
    except note_version_service.VersionError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": error.code, "message": error.message},
        ) from error

    if payload.title is not None:
        title = str(payload.title).strip()
        if not title:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "LLM_TITLE_EMPTY", "message": "标题不能为空"},
            )
        if len(title) > copy_requirements.TITLE_MAX:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "LLM_TITLE_TOO_LONG",
                    "message": f"标题最多 {copy_requirements.TITLE_MAX} 字（实得 {len(title)}）",
                },
            )
        record.title = title
    if payload.body is not None:
        body = str(payload.body).strip()
        if not body:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "LLM_BODY_EMPTY", "message": "正文不能为空"},
            )
        if len(body) > copy_requirements.BODY_MAX:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "LLM_BODY_TOO_LONG",
                    "message": f"正文最多 {copy_requirements.BODY_MAX} 字（实得 {len(body)}）",
                },
            )
        record.body = body
    if payload.topics is not None:
        topics = [str(t).strip() for t in payload.topics if str(t).strip()]
        record.topics_json = json.dumps(topics[:20], ensure_ascii=False)
    if payload.media is not None:
        #: **整份替换**这一版的媒体清单（用户改的是「这一版用哪几张图」这件事本身）。
        #: ⚠️ 保留每条自带的 `source` / `role` / `slot_index` —— 回显靠它们把图归回
        #: 正确的段（封面 / 内页 N），丢了就又变成「全挤进封面」。
        cleaned: list[dict] = []
        for entry in payload.media:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "").strip()
            local = str(entry.get("local_path") or "").strip()
            if not url and not local:
                continue
            cleaned.append(
                {
                    "url": url,
                    "local_path": local,
                    "sort_order": int(entry.get("sort_order") or len(cleaned)),
                    "asset_type": str(entry.get("asset_type") or "image"),
                    "ok": entry.get("ok") is not False,
                    "source": str(entry.get("source") or ""),
                    "role": str(entry.get("role") or ""),
                    "slot_index": entry.get("slot_index"),
                }
            )
        record.media_json = json.dumps(cleaned, ensure_ascii=False)

        #: ⚠️ **必须同步写回 `note_assets`**（用户口径 2026-10-19：*「封面还是生成记录未更新
        #: 前一次的封面」*）—— 发布链路 `_select_media()` 读的是 **`note_assets`**，
        #: 不是 `media_json`；只改后者 ⇒ 界面上封面已经换了、发出去的还是旧封面。
        #: 找到这一版产出的那篇笔记（`raw_json.generation.record_id` 反查），
        #: **整份重建**资产并重编 `sort_order`（0..N-1，第 1 条即封面）。
        from backend.app.models.note import Note, PublishAsset

        for note in db.scalars(
            select(Note).where(
                Note.user_id == int(current_user.id),
                Note.note_type == "local_generated",
            )
        ).all():
            raw = note.raw_json if isinstance(note.raw_json, dict) else {}
            generation = (raw or {}).get("generation") or {}
            if int(generation.get("record_id") or 0) != int(record.id):
                continue
            for asset in db.scalars(
                select(PublishAsset).where(PublishAsset.parent_id == int(note.id))
            ).all():
                db.delete(asset)
            db.flush()
            for position, entry in enumerate(cleaned):
                db.add(
                    PublishAsset(
                        note_id=int(note.id),
                        asset_type=str(entry.get("asset_type") or "image"),
                        url=str(entry.get("url") or ""),
                        local_path=str(entry.get("local_path") or ""),
                        sort_order=position,
                    )
                )
            break

    db.commit()
    db.refresh(record)
    return {
        "ok": True,
        "record_id": int(record.id),
        "version_no": int(record.version_no or 1),
        "title": record.title or "",
        "body": record.body or "",
        "topics": json.loads(record.topics_json or "[]"),
    }


@router.post("/versions/{record_id}/adopt")
def adopt_note_version(
    record_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把某版标为**当前采用版**（同商品其余版本取消）。

    ⚠️ 只是**标记**，不改 `notes` 表里已有的那篇 —— 「采用」是「我认这版」的意图记录。
    真正把它写成正式笔记是发布链路的事（另一步，避免这里误改已发布内容）。
    """
    try:
        record = note_version_service.find_record(
            db, user_id=int(current_user.id), record_id=int(record_id)
        )
    except note_version_service.VersionError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": error.code, "message": error.message},
        ) from error
    if str(record.status) != "success":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "LLM_VERSION_NOT_SUCCESS",
                "message": "只有成功产出的版本才能标为采用版",
            },
        )
    note_version_service.mark_adopted(db, record=record)
    db.commit()
    return {"ok": True, "adopted_record_id": int(record.id), "version_no": int(record.version_no or 1)}
