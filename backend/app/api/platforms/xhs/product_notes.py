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

from typing import Any, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory
from backend.app.api.ai import _business_model_context, get_text_ai_client
from backend.app.api.platforms.xhs.qianfan import _owned_account
from backend.app.services.business_provider_keys import BUSINESS_KEY_GENERATE_NOTE
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models.generation import MEDIA_SOURCES, MEDIA_SOURCE_PRODUCT_IMAGE
from backend.app.models.platform_account import PlatformAccount
from backend.app.models.shop_product import ShopProduct
from backend.app.models.user import User
from backend.app.services.note_gen_llm_service import NoteGenerationError
from backend.app.services.note_gen_llm_worker import (
    GEN_LLM_BATCH_MAX_ITEMS,
    llm_generate_queue_snapshot,
    start_llm_batch_generate,
    start_llm_generate_note,
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
            db, current_user, BUSINESS_KEY_GENERATE_NOTE
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
    #: 覆盖默认模型名；空 = 用该业务点映射到的服务商的默认模型（`resolve_business_provider`）。
    model_name: str = Field(default="", max_length=128)
    #: 默认商品图直链（T7 媒体生成落地前唯一可用来源，见 §3.6）。
    media_source: str = Field(default=MEDIA_SOURCE_PRODUCT_IMAGE, max_length=32)
    #: 素材选择 / 参考图 / 封面开关等（T3/T7 落地后才用得上；本期透传留痕）。
    media_options: dict[str, Any] = Field(default_factory=dict)


class BatchGenerateLlmNotesRequest(BaseModel):
    """LLM 批量生成入参（设计 17 §7.4）。"""

    platform_account_id: int = Field(ge=1)
    item_ids: list[str] = Field(min_length=1)
    template_id: int | None = Field(default=None, ge=1)
    gateway_config_id: int | None = Field(default=None, ge=1)
    model_name: str = Field(default="", max_length=128)
    media_source: str = Field(default=MEDIA_SOURCE_PRODUCT_IMAGE, max_length=32)
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


def _ledger_product(db: Session, current_user: User, account: PlatformAccount, item_id: str) -> ShopProduct:
    """`(账号, item_id)` → 台账行（**零平台请求**，缺值不造假）。

    ⚠️ 与一期 `_fetch_product` 的关键差别：一期打千帆取**当下**商品（生成要写价格，
    必须最新）；本期事实来源是**本地台账**（§5.1 商品白名单字段注入的输入），
    设计 17 §1 把「生成链路完全不碰平台」定为定位 —— 即列表页同步过的商品才能生成，
    与「商品台账」页同一个数据源。
    """
    row = db.scalar(
        select(ShopProduct).where(
            ShopProduct.user_id == current_user.id,
            ShopProduct.platform_account_id == account.id,
            ShopProduct.item_id == item_id,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "LLM_PRODUCT_NOT_FOUND",
                "message": f"本账号的台账里找不到商品 {item_id}。请先在「商品台账」同步该商品。",
            },
        )
    return row


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


@router.post("/generate-llm")
def generate_llm_note(
    payload: GenerateLlmNoteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**入队一次 LLM 生成**（设计 17 §7.4 / T6；同步段毫秒级返回）。

    与一期 `/generate-save` 的差别：本期**不需要千帆登录态**（生成阶段不碰平台），
    但要**已有默认文本模型**（否则 400 `LLM_GATEWAY_NOT_CONFIGURED`）。

    终态不靠响应体：轮询 `GET /generate-llm-queue`（与同步 / 发布 / 一期生成
    同一个出口、同一个底座）。返回 `{ok, status:"queued", item_id, record_id}`；
    重复入队（同商品未终态）⇒ `already_queued: true`（幂等，不重复生成）。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    row = _ledger_product(db, current_user, account, payload.item_id)
    media_source = _check_media_source(payload.media_source)
    try:
        receipt = start_llm_generate_note(
            db,
            current_user.id,
            shop_product_id=int(row.id),
            account_id=int(account.id),
            template_id=payload.template_id,
            gateway_config_id=payload.gateway_config_id,
            model_name=payload.model_name,
            media_source=media_source,
        )
    except NoteGenerationError as error:
        raise _llm_generation_error(error)
    return {
        "ok": True,
        "platform_account_id": int(account.id),
        "item_id": payload.item_id,
        **receipt,
    }


@router.post("/batch-generate-llm")
def batch_generate_llm_notes(
    payload: BatchGenerateLlmNotesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """批量入队 LLM 生成（设计 17 §7.4）：逐项过门禁，**单项失败不阻断**。

    与一期批量同语义（`Promise.allSettled` 风格）：响应给逐项回执
    `{item_id, ok, error?, code?}`，前端汇总「已入队 X / 失败 Y」。

    ⚠️ **网关门禁（`LLM_GATEWAY_NOT_CONFIGURED`）在批量入口直接抛 400**（不逐项）：
    它是**账号级**属性 —— 逐项回执会给出 20 条一模一样的失败，用户以为「这些商品
    都有问题」，而真正要修的是「去配一个默认文本模型」。

    `item_ids` 里定位不到台账行的项 ⇒ 逐项回执 `LLM_PRODUCT_NOT_FOUND`（不抛）。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    media_source = _check_media_source(payload.media_source)
    # 先解析「平台 ID → 台账主键」，解析失败项进回执（保持入队集合是纯主键）。
    resolved: list[tuple[str, int]] = []
    pre_results: list[dict[str, Any]] = []
    for item_id in payload.item_ids:
        row = db.scalar(
            select(ShopProduct).where(
                ShopProduct.user_id == current_user.id,
                ShopProduct.platform_account_id == account.id,
                ShopProduct.item_id == item_id,
            )
        )
        if row is None:
            pre_results.append(
                {
                    "item_id": item_id,
                    "ok": False,
                    "code": "LLM_PRODUCT_NOT_FOUND",
                    "error": "本账号的台账里找不到该商品，请先同步。",
                }
            )
            continue
        resolved.append((item_id, int(row.id)))
    try:
        summary = start_llm_batch_generate(
            db,
            current_user.id,
            shop_product_ids=[pk for _, pk in resolved],
            account_id=int(account.id),
            template_id=payload.template_id,
            gateway_config_id=payload.gateway_config_id,
            model_name=payload.model_name,
            media_source=media_source,
            max_items=min(payload.max_items, GEN_LLM_BATCH_MAX_ITEMS),
        )
    except NoteGenerationError as error:
        raise _llm_generation_error(error)
    # 服务层回执按台账主键 ⇒ 换回平台 ID（前端只认平台 ID）。
    pk_to_item = {pk: item_id for item_id, pk in resolved}
    results = list(pre_results)
    for entry in summary.get("results", []):
        merged = dict(entry)
        merged["item_id"] = pk_to_item.get(int(merged.get("shop_product_id") or 0), "")
        merged.pop("shop_product_id", None)
        merged.pop("status", None)  # 成功项由 `ok` 表达，`status` 是内部态
        results.append(merged)
    return {
        "total": len(payload.item_ids),
        "started": int(summary.get("started") or 0),
        "failed": len(payload.item_ids) - int(summary.get("started") or 0),
        "results": results,
    }


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
):
    """重试一条**失败**的 LLM 生成（设计 17 §7.4）。

    语义 = **新队列项 + 原审计记录复位回 `queued`**（不新建审计行 —— 用户视角是
    「这次生成再试一次」，不是两次生成）。只有 `status=failed` 可重试：
    `success` 重试会覆盖已完成产物，`running` 正在跑 ⇒ 一律 400 `LLM_RECORD_NOT_RETRYABLE`。

    重试**不需要**再传商品 / 模板：取原记录上的 `shop_product_id` / `template_id`
    （连同 `gateway_config_id` / `model_name` / `media_source` 一起在新队列项里留痕）。
    """
    try:
        receipt = start_llm_retry(db, current_user.id, record_id=int(record_id))
    except NoteGenerationError as error:
        raise _llm_generation_error(error)
    return {"ok": True, **receipt}
