from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models import AiDraft, AiGeneratedAsset, Task, User
from backend.app.schemas.common import paginated
from backend.app.services import llm_gateway_client
from backend.app.services.ai_service import ImageAiClient, OpenAICompatibleImageClient, OpenAICompatibleTextClient, TextAiClient
from backend.app.core.business_provider_keys import (
    BUSINESS_KEY_GENERATE_TAGS,
    BUSINESS_KEY_WORKSHOP_DRAFT_TEXT,
    BUSINESS_KEY_WORKSHOP_IMAGE,
    BUSINESS_KEY_WORKSHOP_IMAGE_UNDERSTAND,
    BUSINESS_KEY_WORKSHOP_REWRITE_TEXT,
    BUSINESS_KEY_WORKSHOP_TITLE_TEXT,
)
from backend.app.services.llm_gateway_client import LLMGatewayError, PLATFORM_XHS as BUSINESS_PLATFORM_XHS
from backend.app.services.llm_gateway_usage_service import (
    FUNCTION_DESCRIBE,
    FUNCTION_GENERATE,
    FUNCTION_POLISH,
    FUNCTION_REWRITE,
    FUNCTION_TAG,
    FUNCTION_TITLE,
)

router = APIRouter(prefix="/ai", tags=["ai"])


class RewriteNoteRequest(BaseModel):
    draft_id: int
    instruction: str = Field(default="", max_length=800)


class GenerateNoteRequest(BaseModel):
    platform: Literal["xhs", "douyin", "kuaishou", "weibo", "xianyu", "taobao"] = "xhs"
    topic: str = Field(min_length=1, max_length=300)
    reference: str = Field(default="", max_length=4000)
    instruction: str = Field(default="", max_length=1000)


class GenerateTitleRequest(BaseModel):
    title: str = Field(default="", max_length=300)
    body: str = Field(min_length=1, max_length=6000)
    count: int = Field(default=5, ge=1, le=10)


class GenerateTagsRequest(BaseModel):
    title: str = Field(default="", max_length=300)
    body: str = Field(min_length=1, max_length=6000)
    count: int = Field(default=8, ge=1, le=20)


class PolishTextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=6000)
    instruction: str = Field(default="", max_length=800)


class GenerateCoverRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1200)
    draft_id: Optional[int] = None
    size: str = Field(default="1024x1024", max_length=32)
    style: str = Field(default="clean", max_length=120)


class GenerateImageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    reference_images: list[str] = Field(default_factory=list)
    save_to_assets: bool = True


class DescribeImageRequest(BaseModel):
    image_url: str = Field(min_length=1, max_length=4000)
    instruction: str = Field(default="", max_length=800)


def get_text_ai_client() -> TextAiClient:
    return OpenAICompatibleTextClient()


def get_image_ai_client() -> ImageAiClient:
    return OpenAICompatibleImageClient()


def _serialize_draft(draft: AiDraft) -> dict:
    return {
        "id": draft.id,
        "platform": draft.platform,
        "title": draft.title,
        "body": draft.body,
        "source_note_id": draft.source_note_id,
        "created_at": draft.created_at.isoformat(),
    }


def _business_model_context(
    db: Session,
    current_user: User,
    business_key: str,
    platform: str = BUSINESS_PLATFORM_XHS,
) -> tuple[Any, str]:
    """按**业务点固定 key** 取调用上下文（v2.0 唯一入口，设计 17 §13.2.4）。

    取代一期 `_text_model_context` / `_image_model_context`（它们在 v2.0 退场）：
    业务不再按「文本/图像」找默认模型，而是按**业务点**（`generate_note` /
    `generate_cover` …）查「平台 × 业务点 → 服务商」映射 ——
    页面换服务商即刻生效，代码零改动。

    失败一律**结构化、不回落**（`llm_gateway_client` 的业务异常），
    这里把它映射成 HTTP 400 并把 `code` 带进响应体，
    让前端能凭 code 给出「去模型配置页配一个」的指向性提示。
    """
    try:
        ctx = llm_gateway_client.resolve_business_provider(
            db, current_user, business_key, platform
        )
    except LLMGatewayError as exc:
        raise _business_model_error(exc) from exc
    return ctx, ctx.api_key


#: 「业务点未配服务商」的结构化 400 错误码（`llm_gateway_client` 给的值）。
#: ⚠️ 独立常量而不是散写字面量：`_business_model_error()` 与 `goods.py` 的
#: 「本次**不生成**该组」判据都按它分支，两处必须同源（DEF-044）。
BUSINESS_KEY_NOT_CONFIGURED_CODE = "LLM_BUSINESS_PROVIDER_NOT_CONFIGURED"


def _business_model_error(exc: "LLMGatewayError") -> HTTPException:
    """业务点解析失败 ⇒ **结构化 400**（`_business_model_context` 的唯一映射实现）。

    ⚠️ **`resolve_business_provider` 可能在两个时机抛**，`_business_model_context`
    只兜住了**第一个**：
    ① 调用点**立即**执行（`rewrite-note` 的 246 行等）⇒ 被上面的 `try` 接住；
    ② 被 `lambda` **延迟**到 `_recorded_image_task` 的 `action()` 里执行
       （`image_ai_client.generate_images(...)` 的实参里读 `model_config.model_name`
       ⇒ 属性访问时**重新求值**）⇒ 此时已在 `except Exception` 的地界上。

    ② 的后果不是「报错」，是**报错报成 502**：真正的原因是「未配服务商」（400 +
    `LLM_BUSINESS_PROVIDER_NOT_CONFIGURED`，前端据此给「去模型配置页」的指向性提示），
    却按「上游出图失败」出队 —— 用户看到 502 只会去查服务商额度，
    **真实原因被归错类**（同 `detailMessage` 注释里那段「真实原因被丢掉、用户无从自救」）。
    `qianfan/goods.py::generate_detail_image` 是**已知会走 ②** 的那条链路
    （它的 `generate_images(...)` 实参里同时读了 `model_config` 与 `api_key`）。

    故本函数是 **唯一** 的「业务异常 → 400」映射点，`_business_model_context` 与
    `_recorded_text_task` / `_recorded_image_task` 的异常分支都调它 ——
    三种字段组合（缺模型 / 服务商停用 / 未配映射）在两处得到**逐字相同**的状态码与响应体。
    """
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": exc.code, "message": exc.message},
    )


def _serialize_generated_asset(asset: AiGeneratedAsset) -> dict[str, Any]:
    return {
        "id": asset.id,
        "draft_id": asset.draft_id,
        "prompt": asset.prompt,
        "model_name": asset.model_name,
        "params": asset.params or {},
        "file_path": asset.file_path,
        "created_at": asset.created_at.isoformat(),
    }


def _recorded_text_task(
    *,
    db: Session,
    current_user: User,
    platform: str,
    task_type: str,
    payload: dict[str, Any],
    action: Callable[[], Any],
):
    task = Task(
        user_id=current_user.id,
        platform=platform,
        task_type=task_type,
        status="running",
        progress=10,
        payload=payload,
    )
    db.add(task)
    # ⚠️ **必须先 commit 再跑 action**（DEF-085 的根因修复）：
    # `flush()` 只把 INSERT 发出去，事务**仍然开着并持有 SQLite 写锁**；
    # 而 action 是一次真实 LLM 调用（数秒到数十秒）。记账（`record_usage`）
    # **另开连接**写 `llm_gateway_usage_logs` ⇒ 全程撞 `database is locked`，
    # 5s busy_timeout 等不过一个 30s 的上游调用 ⇒ **每次调用都静默丢台账**。
    # commit 后锁只持有毫秒级，记账才有机会落库。
    # 语义不变：task 行照样先落库（失败时再改状态），只是不再把「跑业务」
    # 圈在事务里 —— 那本来也不该在事务里。
    db.commit()
    try:
        result = action()
    except LLMGatewayError as exc:
        # ⚠️ **先于 `except ValueError` / `except Exception`**：`LLMGatewayError`
        # 继承 `RuntimeError`，两条都不接它，故顺序不影响匹配 —— 但必须**显式**接住，
        # 否则业务点解析失败会被下面那条 `except Exception` 归成 502（真实原因被归错类，
        # 见 `_business_model_error` 的注释）。
        task.status = "failed"
        task.progress = 100
        task.payload = {**(task.payload or {}), "error": exc.message}
        db.commit()
        raise _business_model_error(exc) from exc
    except ValueError as exc:
        task.status = "failed"
        task.progress = 100
        task.payload = {**(task.payload or {}), "error": str(exc)}
        db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        task.status = "failed"
        task.progress = 100
        task.payload = {**(task.payload or {}), "error": str(exc)}
        db.commit()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"AI text generation failed: {exc}") from exc

    task.status = "completed"
    task.progress = 100
    return task, result


def _recorded_image_task(
    *,
    db: Session,
    current_user: User,
    task_type: str,
    payload: dict[str, Any],
    action: Callable[[], Any],
):
    task = Task(
        user_id=current_user.id,
        platform="xhs",
        task_type=task_type,
        status="running",
        progress=10,
        payload=payload,
    )
    db.add(task)
    # ⚠️ 同 `_recorded_text_task`：**先 commit 再跑 action**（DEF-085）——
    # `flush()` 会持有 SQLite 写锁直到事务结束，而 action 是一次真实的
    # 图像生成调用（更慢，数十秒），记账另开连接必然撞锁 ⇒ 静默丢台账。
    db.commit()
    try:
        result = action()
    except LLMGatewayError as exc:
        # ⚠️ 同 `_recorded_text_task`：业务点解析失败必须如实回 **400 结构化**，
        # 不能被下面那条 `except Exception` 归成 502「上游出图失败」。
        task.status = "failed"
        task.progress = 100
        task.payload = {**(task.payload or {}), "error": exc.message}
        db.commit()
        raise _business_model_error(exc) from exc
    except ValueError as exc:
        task.status = "failed"
        task.progress = 100
        task.payload = {**(task.payload or {}), "error": str(exc)}
        db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        task.status = "failed"
        task.progress = 100
        task.payload = {**(task.payload or {}), "error": str(exc)}
        db.commit()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"AI image generation failed: {exc}") from exc

    task.status = "completed"
    task.progress = 100
    return task, result


@router.post("/rewrite-note")
def rewrite_note(
    payload: RewriteNoteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    text_ai_client: TextAiClient = Depends(get_text_ai_client),
):
    draft = db.get(AiDraft, payload.draft_id)
    if draft is None or draft.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")

    model_config, api_key = _business_model_context(db, current_user, BUSINESS_KEY_WORKSHOP_REWRITE_TEXT)
    task, rewritten_body = _recorded_text_task(
        db=db,
        current_user=current_user,
        platform=draft.platform,
        task_type="ai_rewrite",
        payload={"draft_id": draft.id, "model_config_id": model_config.id, "instruction": payload.instruction},
        action=lambda: text_ai_client.rewrite_note(
            model_config=model_config,
            api_key=api_key,
            title=draft.title,
            body=draft.body,
            instruction=payload.instruction,
            platform=draft.platform,
            function=FUNCTION_REWRITE,
        ),
    )
    draft.body = rewritten_body
    task.payload = {**(task.payload or {}), "result_draft_id": draft.id, "result_length": len(rewritten_body)}
    db.commit()
    db.refresh(draft)
    return _serialize_draft(draft)


@router.post("/generate-note")
def generate_note(
    payload: GenerateNoteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    text_ai_client: TextAiClient = Depends(get_text_ai_client),
):
    model_config, api_key = _business_model_context(db, current_user, BUSINESS_KEY_WORKSHOP_DRAFT_TEXT)
    task, result = _recorded_text_task(
        db=db,
        current_user=current_user,
        platform=payload.platform,
        task_type="ai_generate_note",
        payload={"model_config_id": model_config.id, "topic": payload.topic},
        action=lambda: text_ai_client.generate_note(
            model_config=model_config,
            api_key=api_key,
            topic=payload.topic,
            reference=payload.reference,
            instruction=payload.instruction,
            platform=payload.platform,
            function=FUNCTION_GENERATE,
        ),
    )
    draft = AiDraft(
        user_id=current_user.id,
        platform=payload.platform,
        title=result.get("title") or payload.topic,
        body=result.get("body") or "",
    )
    db.add(draft)
    db.flush()
    task.payload = {**(task.payload or {}), "result_draft_id": draft.id}
    db.commit()
    db.refresh(draft)
    return _serialize_draft(draft)


@router.post("/generate-title")
def generate_title(
    payload: GenerateTitleRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    text_ai_client: TextAiClient = Depends(get_text_ai_client),
):
    model_config, api_key = _business_model_context(db, current_user, BUSINESS_KEY_WORKSHOP_TITLE_TEXT)
    task, items = _recorded_text_task(
        db=db,
        current_user=current_user,
        platform="xhs",
        task_type="ai_generate_title",
        payload={"model_config_id": model_config.id, "count": payload.count},
        action=lambda: text_ai_client.generate_titles(
            model_config=model_config,
            api_key=api_key,
            title=payload.title,
            body=payload.body,
            count=payload.count,
            platform="xhs",
            function=FUNCTION_TITLE,
        ),
    )
    task.payload = {**(task.payload or {}), "result_count": len(items)}
    db.commit()
    return {"items": items}


@router.post("/generate-tags")
def generate_tags(
    payload: GenerateTagsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    text_ai_client: TextAiClient = Depends(get_text_ai_client),
):
    model_config, api_key = _business_model_context(db, current_user, BUSINESS_KEY_GENERATE_TAGS)
    task, items = _recorded_text_task(
        db=db,
        current_user=current_user,
        platform="xhs",
        task_type="ai_generate_tags",
        payload={"model_config_id": model_config.id, "count": payload.count},
        action=lambda: text_ai_client.generate_tags(
            model_config=model_config,
            api_key=api_key,
            title=payload.title,
            body=payload.body,
            count=payload.count,
            platform="xhs",
            function=FUNCTION_TAG,
        ),
    )
    task.payload = {**(task.payload or {}), "result_count": len(items)}
    db.commit()
    return {"items": items}


@router.post("/polish-text")
def polish_text(
    payload: PolishTextRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    text_ai_client: TextAiClient = Depends(get_text_ai_client),
):
    #: **业务点已退场**（2026-10-18：无任何前端调用 ⇒ 移出模型配置）。
    #: 端点保留但不再解析模型映射 —— 若外部仍在调它，这里给出明确原因，
    #: 而不是抛一个「业务点未配置」让人以为去页面配一下就好。
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "code": "AI_POLISH_TEXT_GONE",
            "message": "文本润色已停用（无调用方）。改用笔记工坊的「改写草稿」。",
        },
    )
    task, text = _recorded_text_task(
        db=db,
        current_user=current_user,
        platform="xhs",
        task_type="ai_polish_text",
        payload={"model_config_id": model_config.id, "instruction": payload.instruction},
        action=lambda: text_ai_client.polish_text(
            model_config=model_config,
            api_key=api_key,
            text=payload.text,
            instruction=payload.instruction,
            platform="xhs",
            function=FUNCTION_POLISH,
        ),
    )
    task.payload = {**(task.payload or {}), "result_length": len(text)}
    db.commit()
    return {"text": text}


@router.get("/images/assets")
def generated_image_assets(
    page: int = 1,
    page_size: int = 20,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    assets = db.scalars(
        select(AiGeneratedAsset)
        .where(AiGeneratedAsset.user_id == current_user.id)
        .order_by(AiGeneratedAsset.created_at.desc(), AiGeneratedAsset.id.desc())
    ).all()
    return paginated([_serialize_generated_asset(asset) for asset in assets], page, page_size)


@router.delete("/images/assets/{asset_id}")
def delete_generated_image_asset(
    asset_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    asset = db.get(AiGeneratedAsset, asset_id)
    if asset is None or asset.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    db.delete(asset)
    db.commit()
    return {"id": asset_id, "status": "deleted"}


@router.post("/images/generate-cover")
def generate_cover(
    payload: GenerateCoverRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    image_ai_client: ImageAiClient = Depends(get_image_ai_client),
):
    if payload.draft_id is not None:
        draft = db.get(AiDraft, payload.draft_id)
        if draft is None or draft.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")

    #: **业务点已退场**（同 `polish_text`：前端零调用）。
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "code": "AI_GENERATE_COVER_GONE",
            "message": "生成封面已停用（无调用方）。改用「LLM 生成笔记」的配图/封面来源。",
        },
    )
    task, result = _recorded_image_task(
        db=db,
        current_user=current_user,
        task_type="ai_image_generate_cover",
        payload={"model_config_id": model_config.id, "prompt": payload.prompt, "size": payload.size, "style": payload.style},
        action=lambda: image_ai_client.generate_cover(
            model_config=model_config,
            api_key=api_key,
            prompt=payload.prompt,
            size=payload.size,
            style=payload.style,
        ),
    )
    asset = AiGeneratedAsset(
        user_id=current_user.id,
        draft_id=payload.draft_id,
        prompt=payload.prompt,
        model_name=model_config.model_name,
        params={"size": payload.size, "style": payload.style, "raw": result.get("raw")},
        file_path=result.get("url") or "",
    )
    db.add(asset)
    db.flush()
    task.payload = {**(task.payload or {}), "asset_id": asset.id}
    db.commit()
    db.refresh(asset)
    return _serialize_generated_asset(asset)


@router.post("/images/generate")
def generate_image(
    payload: GenerateImageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    image_ai_client: ImageAiClient = Depends(get_image_ai_client),
):
    model_config, api_key = _business_model_context(db, current_user, BUSINESS_KEY_WORKSHOP_IMAGE)
    task, result = _recorded_image_task(
        db=db,
        current_user=current_user,
        task_type="ai_image_generate",
        payload={"model_config_id": model_config.id, "prompt": payload.prompt, "reference_images": payload.reference_images},
        action=lambda: image_ai_client.generate_image(
            model_config=model_config,
            api_key=api_key,
            prompt=payload.prompt,
            reference_images=payload.reference_images or None,
        ),
    )
    response_data: dict = {"url": result.get("url") or "", "raw": result.get("raw")}
    if payload.save_to_assets:
        asset = AiGeneratedAsset(
            user_id=current_user.id,
            prompt=payload.prompt,
            model_name=model_config.model_name,
            params={"reference_images": payload.reference_images, "raw": result.get("raw")},
            file_path=result.get("url") or "",
        )
        db.add(asset)
        db.flush()
        task.payload = {**(task.payload or {}), "asset_id": asset.id}
        db.commit()
        db.refresh(asset)
        response_data["asset"] = _serialize_generated_asset(asset)
    else:
        db.commit()
    return response_data


@router.post("/images/describe")
def describe_image(
    payload: DescribeImageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    image_ai_client: ImageAiClient = Depends(get_image_ai_client),
):
    model_config, api_key = _business_model_context(db, current_user, BUSINESS_KEY_WORKSHOP_IMAGE_UNDERSTAND)
    task, text = _recorded_image_task(
        db=db,
        current_user=current_user,
        task_type="ai_image_describe",
        payload={"model_config_id": model_config.id, "image_url": payload.image_url, "instruction": payload.instruction},
        action=lambda: image_ai_client.describe_image(
            model_config=model_config,
            api_key=api_key,
            image_url=payload.image_url,
            instruction=payload.instruction,
            platform="xhs",
            function=FUNCTION_DESCRIBE,
        ),
    )
    task.payload = {**(task.payload or {}), "result_length": len(text)}
    db.commit()
    return {"text": text}
