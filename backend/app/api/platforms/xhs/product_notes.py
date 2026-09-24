"""商品笔记生成（M4 起步）：`POST /api/xhs/product-notes/generate`。

设计真相源：`docs/02_设计文档/07_功能设计_发布链路.md` §12（生成与护栏契约）。

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

## 为什么每次都要取一次商品（而不是缓存）

价格/库存会变，而「生成的笔记里写了旧价格」= 违规（07 §9「发布前复核商品状态与价格」）。
缓存 = 第二套真相源。代价是 1 次只读请求，可接受（与「按需补齐每页 ≤1 次」同一纪律）。
"""
from __future__ import annotations

from typing import Any

import requests
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory
from backend.app.api.ai import _text_model_context, get_text_ai_client
from backend.app.api.platforms.xhs.qianfan import _owned_account
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models.platform_account import PlatformAccount
from backend.app.models.user import User
from backend.app.services.product_note_service import generate_product_note
from backend.app.services.product_note_gen_service import (
    ProductNoteGenerationError,
    batch_generate_notes,
    generate_note_for_item,
)
from backend.app.services.shop_binding_service import (
    account_shop_id,
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
    """
    if account_shop_id(db, account.id) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_BINDING_REQUIRED",
                "message": "尚未绑定千帆商家后台，无法读取商品信息。请先绑定后再生成。",
            },
        )
    cookies = qianfan_cookies_for_account(db, account.id)
    if not cookies:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_CREDENTIAL_MISSING",
                "message": "该账号还没有千帆（ark）令牌，请先在浏览器登录千帆后导入。",
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
        model_config, api_key = _text_model_context(db, current_user)
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

    状态码口径与干运行端点一致：门禁类（未绑店 / 无令牌）400、商品不在店铺 404、
    平台侧失败 502。
    """
    if error.code == "QIANFAN_ITEM_NOT_FOUND":
        http_status = status.HTTP_404_NOT_FOUND
    elif error.code in ("QIANFAN_BINDING_REQUIRED", "QIANFAN_CREDENTIAL_MISSING"):
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
    """**智能生成一篇商品笔记并落库为「待发布」**（设计 13 §31.4，用户需求③）。

    与干运行端点（`/generate`）的分工：干运行是「看一眼草稿」（本地模型、
    不落库）；本端点是「落库待发布」（**千帆后台智能生成**，写 `notes` +
    `note_assets` + `note_goods`）。

    | 约定 | 做法 |
    |---|---|
    | 生成 | 千帆三步链路：`aigc-publish/attributes` → `note/sellerpoint/aigc` → 轮询 `aigctask/result`（真机抓包，无签名） |
    | 商品 | **本地台账** `shop_products`（同一行同时供媒体链接与挂链关系，§31.4 约定 1） |
    | 话题 | 平台 AI 结果自带 `topics[{id,name}]` → `topics_json` + `raw_json.aigc`（发布的 `hash_tag` 直接用平台 ID） |
    | 媒体 | 商品图直链落 `note_assets`（发布时再下载重传） |
    | 笔记 ID | `local_<uuid>`（未发布不存在三方 ID；发布成功回写真 ID） |
    | 状态 | `publish_status=pending`、`note_type=local_generated`、`published=False` |
    | 护栏 | 记录不拦截（内容出自平台官方 AI，§31.4 约定） |
    | 失败 | 平台 `errMsg` 原文 502；门禁失败 400；**不落库**（无半条笔记） |
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    try:
        outcome = generate_note_for_item(db, account, payload.item_id, adapter_factory=adapter_factory)
    except ProductNoteGenerationError as error:
        raise _generation_error(error)
    return {
        "ok": True,
        "platform_account_id": payload.platform_account_id,
        "item_id": payload.item_id,
        "note_id": outcome["note_id"],
        "title": outcome["title"],
        "topics": outcome["topics"],
        #: 落库的媒体（用户口径「图片最多 6 张，视频最多一个」，设计 13 §32.2.1）：
        #: 视频优先 ⇒ 有视频时 `image_count=0`、`video_count=1`；两个数都如实回。
        "image_count": outcome["image_count"],
        "video_count": outcome["video_count"],
        "guardrails": outcome["guardrails"],
    }


@router.post("/batch-generate")
def batch_generate_notes_endpoint(
    payload: BatchGenerateNotesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """批量智能生成（设计 13 §31.4 步 6；用户需求⑤）。

    逐项走 `/generate-save` 同一链路；**单项失败不阻断**（与千帆页面
    `Promise.allSettled` 同语义）。响应给逐项结果，前端汇总
    「成功 X / 失败 Y」并列出失败原因。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    return batch_generate_notes(
        db, account, payload.item_ids, adapter_factory=adapter_factory, max_items=payload.max_items
    )
