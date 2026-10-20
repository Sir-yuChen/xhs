"""自动运营管线（设计 15 §7.2）——关键词 → 搜索 → AI 改写 → 发布任务的**唯一实现**。

2026-09-24 重构前，同一管线有**两份实现**：

- `api/auto_tasks.py::run_auto_task`（手动“立即执行”）：步骤完整、错误处理完整，
  建 `immediate` + `pending` 发布任务（素材 pending，用户在发布页审核后发布）；
- `scheduler_service._execute_auto_task_background`（调度路径）：简化版，AI 失败
  不致命，且自己上传素材 + 直接 post_note（**绕开了发布链路的幂等键与账号闸门**）。

本模块把两者合并：**搜索 / 选优 / 建草稿 / AI 改写 / 建发布任务**只此一份；
发布环节不在这里做——按触发方式建不同 `publish_mode` 的发布任务，由既有发布
链路各自完成（手动 ⇒ 发布页审核后发；调度 ⇒ `due_publish_runner` 补跑，
含账号闸门 T-109 与幂等键 T-103，这正是无人值守路径最需要的东西）。

⚠️ 行为口径（写进发布说明）：
- **手动跑**（立即执行 / 执行一次）= 生成待发布任务（发布页审核后发）；
- **调度跑** = 生成 `scheduled`（scheduled_at=now）发布任务，≤60s 内由
  `due_publish_runner` 自动发布。
- 手动执行不再创建 `tasks` 表行（`auto_ops_run`）——执行记录以
  `scheduled_job_logs` 为准（状态 / 耗时 / 错误 / keyword / draft_id 全在日志里）。
"""

from __future__ import annotations

import random
from typing import Any, Optional

from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models import AiDraft, AutoTask, PublishAsset, PublishJob, User
from backend.app.services.llm_gateway_usage_service import FUNCTION_REWRITE, FUNCTION_TITLE


class AutoTaskRunError(RuntimeError):
    """管线失败（领域错误）。API 层映射为 HTTPException；调度路径进失败日志。"""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


#: 自动运营生成的发布任务最多带几张图（与发布链路的上限一致：图 ≤9）
MAX_AUTO_IMAGES = 9

#: 搜索候选池大小（按互动总量选优）
CANDIDATE_POOL = 10


def _pick_best_note(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return max(
        candidates,
        key=lambda n: (n.get("likes", 0) + n.get("collects", 0) + n.get("comments", 0) + n.get("shares", 0)),
    )


def run_keyword_pipeline(
    *,
    auto_task_id: int,
    publish_mode: str = "scheduled",
    db: Optional[Session] = None,
) -> dict[str, Any]:
    """跑一次自动运营管线。

    :param auto_task_id: `auto_tasks.id`（业务配置行）。
    :param publish_mode: `"scheduled"`（调度触发，runner 自动发布）或
        `"immediate"`（手动触发，发布页审核后发布）。
    :param db: 可选外部 session（不传则自开自关，调度路径用）。
    :returns: 富详情 dict（keyword / source_note / draft / publish_job），
        由调用方写入调度日志的 `result` 或直接返回前端。
    """
    owns_session = db is None
    if owns_session:
        db = SessionLocal()
    try:
        return _run_pipeline(db, auto_task_id=auto_task_id, publish_mode=publish_mode)
    finally:
        if owns_session:
            db.close()


def _run_pipeline(db: Session, *, auto_task_id: int, publish_mode: str) -> dict[str, Any]:
    # 延迟 import：拆掉 api ↔ services 的导入环（与重构前两份实现的取法一致）。
    from backend.app.adapters.xhs.pc_api_adapter import XhsPcApiAdapter
    from backend.app.api.platforms.xhs.crawl import _data_items
    from backend.app.api.platforms.xhs.pc import (
        _get_owned_pc_account_cookies,
        _normalize_search_item,
    )
    from backend.app.services.ai_service import OpenAICompatibleTextClient

    task = db.get(AutoTask, auto_task_id)
    if task is None:
        raise AutoTaskRunError("Auto task not found", status_code=404)
    user = db.get(User, task.user_id)
    if user is None:
        raise AutoTaskRunError("Auto task owner not found", status_code=404)

    # 1. 随机挑一个关键词 --------------------------------------------------- #
    keywords = task.keywords or []
    if not keywords:
        raise AutoTaskRunError("No keywords configured")
    keyword = random.choice(keywords)

    # 2. PC 账号搜索（搜索打 www；凭据只认本账号，拿不到就失败，不借会话） ---- #
    pc_cookies = _get_owned_pc_account_cookies(db, user, task.pc_account_id)
    adapter = XhsPcApiAdapter(pc_cookies)
    success, message, raw_payload = adapter.search_note(keyword, page=1)
    if not success:
        raise AutoTaskRunError(message or "XHS search failed", status_code=502)

    # 3. 按互动总量选最优 ---------------------------------------------------- #
    items = _data_items(raw_payload)
    normalized_items = [_normalize_search_item(item) for item in items]
    if not normalized_items:
        raise AutoTaskRunError("No notes found for keyword", status_code=404)
    candidates = normalized_items[:CANDIDATE_POOL]
    best_note = _pick_best_note(candidates)

    # 4. 建草稿 -------------------------------------------------------------- #
    draft = AiDraft(
        user_id=user.id,
        platform="xhs",
        title=str(best_note.get("title") or ""),
        body=str(best_note.get("content") or ""),
    )
    db.add(draft)
    db.flush()

    # 5. AI 改写（默认文本模型缺失 / 改写失败 = 致命：无人值守路径要的是
    #    响亮的失败 + 退避，不是悄悄产出一篇抄袭稿） -------------------------- #
    # v2.0（设计 17 §13.2.4）：一期「默认文本模型」随 `model_configs` 退场，
    # 改由「平台 × 业务点」映射解析服务商。映射缺失 ⇒ 抛结构化错误
    # （**不回落**：回落 = 用错服务商而不报错，同 §13.2.3 D4）。
    from backend.app.services.business_provider_keys import BUSINESS_KEY_AUTO_OPS_TEXT
    from backend.app.services.llm_gateway_client import (
        LLMGatewayError,
        resolve_business_provider,
    )

    try:
        context = resolve_business_provider(db, user, BUSINESS_KEY_AUTO_OPS_TEXT, platform="xhs")
    except LLMGatewayError as exc:
        raise AutoTaskRunError(f"AI 服务商未配置：{exc}") from exc
    model_config, api_key = context, context.api_key
    text_client = OpenAICompatibleTextClient()
    try:
        rewritten_body = text_client.rewrite_note(
            model_config=model_config,
            api_key=api_key,
            title=draft.title,
            body=draft.body,
            instruction=task.ai_instruction or "改写为原创小红书笔记，保持核心信息，提升表达和语感",
            platform="xhs",
            function=FUNCTION_REWRITE,
        )
        draft.body = rewritten_body
    except Exception as exc:
        raise AutoTaskRunError(f"AI rewrite failed: {exc}", status_code=502) from exc

    # 5b. 标题改写（非致命：失败就用原标题） ---------------------------------- #
    try:
        titles = text_client.generate_titles(
            model_config=model_config,
            api_key=api_key,
            title=draft.title,
            body=draft.body,
            count=1,
            platform="xhs",
            function=FUNCTION_TITLE,
        )
        if titles:
            draft.title = str(titles[0]).strip().strip('"').strip("'").strip("《》")
    except Exception:
        pass

    # 6. 建发布任务（素材 pending：上传归发布环节 —— 手动走发布页、调度走
    #    due_publish_runner，两边都不在这里重复上传逻辑） ---------------------- #
    scheduled_at = shanghai_now() if publish_mode == "scheduled" else None
    publish_job = PublishJob(
        user_id=user.id,
        platform_account_id=task.creator_account_id,
        source_draft_id=draft.id,
        platform="xhs",
        title=draft.title,
        body=draft.body,
        publish_mode=publish_mode,
        scheduled_at=scheduled_at,
        status="pending",
    )
    db.add(publish_job)
    db.flush()

    image_urls = best_note.get("image_urls") or []
    if isinstance(image_urls, list):
        for url in image_urls[:MAX_AUTO_IMAGES]:
            if isinstance(url, str) and url:
                db.add(PublishAsset(
                    publish_job_id=publish_job.id,
                    asset_type="image",
                    file_path=url,
                    upload_status="pending",
                ))

    # 7. 计数器（沿用旧口径：产出发布任务即计数；last_run_at = 本次执行时间） -- #
    task.total_published = (task.total_published or 0) + 1
    task.last_run_at = shanghai_now()

    db.commit()
    db.refresh(draft)
    db.refresh(publish_job)

    return {
        "keyword": keyword,
        "publish_mode": publish_mode,
        "source_note": {
            "note_id": best_note.get("note_id"),
            "title": best_note.get("title"),
            "likes": best_note.get("likes", 0),
            "collects": best_note.get("collects", 0),
            "comments": best_note.get("comments", 0),
        },
        "draft": {
            "id": draft.id,
            "title": draft.title,
            "body": draft.body,
            "created_at": draft.created_at.isoformat(),
        },
        "publish_job": {
            "id": publish_job.id,
            "status": publish_job.status,
            "platform_account_id": publish_job.platform_account_id,
        },
    }
