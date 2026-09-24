from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter
from backend.app.core.account_types import (
    SUB_TYPE_CREATOR,
    SUB_TYPE_PC,
    UnknownSubTypeError,
    normalize_sub_type,
)
from backend.app.core.database import SessionLocal
from backend.app.core.config import get_settings
from backend.app.core.time import shanghai_now
from backend.app.models.scheduled_job import (
    CONCURRENT_ALLOW,
    CONCURRENT_FORBID,
    JOB_STATUS_ACTIVE,
    TRIGGER_MANUAL,
)
from backend.app.services.account_service import (
    AccountRejectedError,
    ensure_creator_account_for_publish,
)
from backend.app.services.job_registry import JobSpec, get_job_spec, job_registry
from backend.app.services.schedule_triggers import (
    SCHEDULER_TIMEZONE,
    build_trigger,
    max_instances_for,
    misfire_params,
)
from backend.app.services.scheduled_job_runner import run_scheduled_job
from backend.app.services.publish_service import (
    DuplicatePublishError,
    claim_publish_idempotency,
)
from backend.app.models import (
    MonitoringSnapshot,
    MonitoringTarget,
    Note,
    Notification,
    PlatformAccount,
    PublishAsset,
    PublishJob,
    ScheduledJob,
    Task,
    User,
)


def _latest_account_cookies(db: Session, account_id: int) -> str:
    """定时发布取**创作者平台**的令牌（2026-09-21 拆分，§11.5）。

    `post_note` 打在 `creator.xiaohongshu.com`，所以按平台取 `creator` ——
    不再按「最新一版」猜（拆分后最新一行信封可能只含本次登录碰过的那个平台）。
    """
    from backend.app.services.account_service import CHANNEL_CREATOR, latest_cookies_for_channel

    cookies = latest_cookies_for_channel(db, account_id, CHANNEL_CREATOR)
    if not cookies:
        raise RuntimeError("Account has no cookies")
    return cookies


def _asset_upload_info(asset: PublishAsset) -> dict[str, Any]:
    try:
        payload = json.loads(asset.creator_upload_info or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Uploaded asset metadata is invalid") from exc
    if not payload.get("fileIds"):
        raise RuntimeError("Uploaded asset is missing Creator upload info")
    return payload


def _external_note_id(payload: dict[str, Any]) -> str:
    for key in ("note_id", "noteId", "id"):
        value = payload.get(key)
        if value:
            return str(value)
    data = payload.get("data")
    if isinstance(data, dict):
        return _external_note_id(data)
    return ""


def _serialize_publish_job(job: PublishJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "platform_account_id": job.platform_account_id,
        "source_draft_id": job.source_draft_id,
        "platform": job.platform,
        "title": job.title,
        "body": job.body,
        "publish_mode": job.publish_mode,
        "status": job.status,
        "scheduled_at": job.scheduled_at.isoformat() if job.scheduled_at else None,
        "external_note_id": job.external_note_id,
        "publish_error": job.publish_error,
        "published_at": job.published_at.isoformat() if job.published_at else None,
        "created_at": job.created_at.isoformat(),
    }


def _load_publish_options(job: PublishJob) -> dict[str, Any]:
    try:
        options = json.loads(job.publish_options or "{}")
    except json.JSONDecodeError:
        return {}
    return options if isinstance(options, dict) else {}


def _clean_topics(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [topic.strip() for topic in value if isinstance(topic, str) and topic.strip()]


def _apply_publish_options(note_info: dict[str, Any], options: dict[str, Any]) -> None:
    topics = _clean_topics(options.get("topics"))
    if topics:
        note_info["topics"] = topics
    location = options.get("location")
    if isinstance(location, str) and location.strip():
        note_info["location"] = location.strip()
    if options.get("privacy_type") in (0, 1):
        note_info["type"] = options["privacy_type"]


def _run_one_due_publish_job(db: Session, current_user: User, job: PublishJob, adapter_factory) -> tuple[bool, dict[str, Any]]:
    # T-109: 走统一账号闸门。定时发布是**无人值守**路径，过期账号在这里
    # 不会被任何人注意到，会持续产生无意义失败与通知噪音——因此本处收益最大。
    try:
        account = ensure_creator_account_for_publish(
            db.get(PlatformAccount, job.platform_account_id), user_id=current_user.id
        )
    except AccountRejectedError as exc:
        raise RuntimeError(exc.detail) from exc

    # T-103: 在改状态之前认领幂等键。定时发布是**无人值守**路径，
    # 一旦重复发文没有任何人会当场察觉，因此这里对库级防线的依赖最强。
    try:
        claim_publish_idempotency(db, job=job)
    except DuplicatePublishError as exc:
        db.rollback()
        raise RuntimeError(exc.message) from exc

    task = Task(
        user_id=current_user.id,
        platform=job.platform,
        task_type="creator_publish_scheduler",
        status="running",
        progress=20,
        payload={"publish_job_id": job.id, "platform_account_id": account.id, "scheduled_at": job.scheduled_at.isoformat() if job.scheduled_at else None},
    )
    db.add(task)
    job.status = "publishing"
    job.publish_error = ""
    db.commit()

    try:
        assets = db.scalars(
            select(PublishAsset).where(PublishAsset.publish_job_id == job.id).order_by(PublishAsset.id.asc())
        ).all()
        if any(asset.asset_type != "image" for asset in assets):
            raise RuntimeError("Only image publish is supported")
        # 2026-09-24（设计 15 §7.2）：补上 pending 素材的上传。此前本路径只认
        # 「已上传」素材，自动运营管线生成的 pending 资产到这里只会拿到一句
        # 「At least one uploaded image asset is required」——上传归发布环节，
        # 与发布页端点（api/publish.py::publish_job_to_creator）同一套逻辑。
        # 幂等键已在上面认领（T-103：昂贵操作之前），此处失败即整单失败。
        pending_assets = [asset for asset in assets if asset.upload_status in ("pending", "failed")]
        if pending_assets:
            # 延迟 import：`_extract_creator_media_id` 的唯一实现在 api/publish.py，
            # 模块层 import 会把 service 挂到 api 上（本文件曾被 api.tasks import）。
            from backend.app.api.publish import _extract_creator_media_id

            creator_cookies = _latest_account_cookies(db, account.id)
            creator_adapter = adapter_factory(creator_cookies)
            for asset in pending_assets:
                try:
                    upload_payload = creator_adapter.upload_media(asset.file_path, asset.asset_type)
                except Exception as exc:
                    asset.upload_status = "failed"
                    asset.upload_error = str(exc)[:500]
                    db.commit()
                    raise RuntimeError(f"Asset upload failed: {exc}") from exc
                asset.upload_status = "uploaded"
                asset.creator_media_id = _extract_creator_media_id(upload_payload)
                asset.creator_upload_info = json.dumps(upload_payload, ensure_ascii=False)
                asset.upload_error = ""
            db.commit()
            db.refresh(job)
            assets = db.scalars(
                select(PublishAsset).where(PublishAsset.publish_job_id == job.id).order_by(PublishAsset.id.asc())
            ).all()
        uploaded_assets = [asset for asset in assets if asset.upload_status == "uploaded"]
        if not uploaded_assets:
            raise RuntimeError("At least one uploaded image asset is required")

        note_info = {
            "title": job.title,
            "desc": job.body,
            "media_type": "image",
            "image_file_infos": [_asset_upload_info(asset) for asset in uploaded_assets],
            "type": 1,
            "postTime": None,
        }
        _apply_publish_options(note_info, _load_publish_options(job))
        payload = adapter_factory(_latest_account_cookies(db, account.id)).post_note(note_info)
        job.status = "published"
        job.external_note_id = _external_note_id(payload)
        job.publish_error = ""
        job.published_at = shanghai_now()
        task.status = "completed"
        task.progress = 100
        task.payload = {**(task.payload or {}), "external_note_id": job.external_note_id, "published_at": job.published_at.isoformat()}
        db.commit()
        db.refresh(job)
        return True, _serialize_publish_job(job)
    except Exception as exc:
        job.status = "failed"
        job.publish_error = str(exc)
        task.status = "failed"
        task.progress = 100
        task.payload = {**(task.payload or {}), "error": str(exc)}
        db.commit()
        db.refresh(job)
        return False, _serialize_publish_job(job)


def run_due_publish_jobs(
    *,
    db: Session,
    current_user: User,
    now: Optional[datetime],
    platform: str,
    adapter_factory,
) -> dict[str, Any]:
    now = now or shanghai_now()
    due_jobs = db.scalars(
        select(PublishJob)
        .join(PlatformAccount, PublishJob.platform_account_id == PlatformAccount.id)
        .where(
            PlatformAccount.user_id == current_user.id,
            PublishJob.platform == platform,
            PublishJob.publish_mode == "scheduled",
            PublishJob.status == "pending",
            PublishJob.scheduled_at.is_not(None),
            PublishJob.scheduled_at <= now,
        )
        .order_by(PublishJob.scheduled_at.asc(), PublishJob.id.asc())
    ).all()

    items: list[dict[str, Any]] = []
    failed_count = 0
    for job in due_jobs:
        succeeded, item = _run_one_due_publish_job(db, current_user, job, adapter_factory)
        items.append(item)
        if not succeeded:
            failed_count += 1

    return {
        "executed_count": len(items),
        "failed_count": failed_count,
        "items": items,
    }


def run_due_publish_jobs_once(
    user_id: int,
    platform: str = "xhs",
    adapter_factory=XhsCreatorApiAdapter,
) -> dict[str, Any]:
    """调度入口：补跑**指定用户**的到期定时发布（设计 15 §4.1.1，DEF-066）。

    ⚠️ 原 `run_due_publish_jobs_for_all_users`（查全库有待发布 job 的用户逐个跑）
    **已被删除** —— 内置任务行按用户各持一份后，「一行触发 ⇒ 只跑行主人的到期 job」；
    全用户包装没有生产调用方，同一语义不留两份实现（DEF-044 纪律）。
    「所有用户的到期任务都被执行到」由 **N 行各自触发** 保证。
    """
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            raise RuntimeError(f"run_due_publish_jobs_once: user {user_id} not found")
        return run_due_publish_jobs(
            db=db,
            current_user=user,
            now=None,
            platform=platform,
            adapter_factory=adapter_factory,
        )
    finally:
        db.close()


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        if cleaned.isdigit():
            return int(cleaned)
    return 0


def _first_metric(raw: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        if key in raw:
            return _as_int(raw.get(key))
    return 0


def _note_metrics(note: Note) -> dict[str, int]:
    raw = note.raw_json or {}
    interaction = raw.get("interact_info") if isinstance(raw.get("interact_info"), dict) else {}
    merged = {**raw, **interaction}
    likes = _first_metric(merged, ("likes", "liked_count", "like_count", "likedCount"))
    collects = _first_metric(merged, ("collects", "collected_count", "collect_count", "collectedCount"))
    comments = _first_metric(merged, ("comments", "comment_count", "commentCount"))
    shares = _first_metric(merged, ("shares", "share_count", "shareCount"))
    return {
        "likes": likes,
        "collects": collects,
        "comments": comments,
        "shares": shares,
        "engagement": likes + collects + comments + shares,
    }


def _serialize_monitoring_note(note: Note) -> dict[str, Any]:
    return {
        "id": note.id,
        "note_id": note.note_id,
        "title": note.title,
        "author_name": note.author_name,
        "created_at": note.created_at.isoformat(),
        **_note_metrics(note),
    }


def _note_haystack(note: Note) -> str:
    raw_text = json.dumps(note.raw_json or {}, ensure_ascii=False)
    return "\n".join([note.note_id, note.title, note.content, note.author_name, raw_text]).lower()


def _note_matches_target(note: Note, target: MonitoringTarget) -> bool:
    needle = target.value.strip().lower()
    if not needle:
        return False
    return needle in _note_haystack(note)


def _matching_notes_for_target(db: Session, target: MonitoringTarget, platform: str) -> list[Note]:
    notes = db.scalars(
        select(Note)
        .where(
            Note.user_id == target.user_id,
            Note.platform == platform,
        )
        .order_by(Note.created_at.desc(), Note.id.desc())
    ).all()
    matched = [note for note in notes if _note_matches_target(note, target)]
    return sorted(matched, key=lambda note: _note_metrics(note)["engagement"], reverse=True)


def _refresh_monitoring_target(db: Session, target: MonitoringTarget, now: datetime, platform: str) -> dict[str, Any]:
    matched_notes = _matching_notes_for_target(db, target, platform)
    snapshot_payload = {
        "matched_count": len(matched_notes),
        "total_engagement": sum(_note_metrics(note)["engagement"] for note in matched_notes),
        "top_notes": [_serialize_monitoring_note(note) for note in matched_notes[:10]],
    }
    target.last_refreshed_at = now
    target.updated_at = now
    snapshot = MonitoringSnapshot(target_id=target.id, payload=snapshot_payload)
    db.add(snapshot)
    db.flush()

    task = Task(
        user_id=target.user_id,
        platform=platform,
        task_type="monitoring_refresh",
        status="completed",
        progress=100,
        payload={
            "target_id": target.id,
            "target_type": target.target_type,
            "value": target.value,
            "snapshot_id": snapshot.id,
            "matched_count": snapshot_payload["matched_count"],
            "scheduler": True,
        },
    )
    db.add(task)
    db.flush()
    return {
        "target_id": target.id,
        "snapshot_id": snapshot.id,
        "matched_count": snapshot_payload["matched_count"],
        "total_engagement": snapshot_payload["total_engagement"],
    }


def run_monitoring_refresh_once(user_id: int, platform: str = "xhs") -> dict[str, Any]:
    """调度入口：刷新**指定用户**的监控目标（设计 15 §4.1.1，DEF-066）。

    ⚠️ 原 `run_monitoring_refresh_for_all_users`（全库 active targets）**已被删除**：
    内置行按用户各持一份 ⇒ 一行触发只刷行主人的 targets。匹配仍按
    `Note.user_id == target.user_id`（target 自带归属，双保险）。
    """
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            raise RuntimeError(f"run_monitoring_refresh_once: user {user_id} not found")
        now = shanghai_now()
        targets = db.scalars(
            select(MonitoringTarget)
            .where(
                MonitoringTarget.user_id == user.id,
                MonitoringTarget.platform == platform,
                MonitoringTarget.status == "active",
            )
            .order_by(MonitoringTarget.id.asc())
        ).all()

        items = [_refresh_monitoring_target(db, target, now, platform) for target in targets]
        db.commit()
        return {
            "refreshed_count": len(items),
            "items": items,
        }
    finally:
        db.close()


def _check_single_account(db: Session, account: PlatformAccount, now: datetime) -> str:
    """Check one account's cookie validity. Returns the new status."""
    from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter
    from backend.app.adapters.xhs.creator_login_adapter import XhsCreatorLoginAdapter
    from backend.app.services.account_service import (
        CHANNEL_CREATOR,
        CHANNEL_WWW,
        decode_cookie_text,
        latest_cookies_for_channel,
    )

    # 显式枚举而非 else 兜底：sub_type 决定域名与 Cookie 域，兜底会让将来
    # 新增的协议链路静默走 pc 分支（T-102）。未知取值抛错，由外层
    # check_all_account_cookies_once 的 per-account try/except 记录警告。
    normalized_sub_type = normalize_sub_type(account.sub_type)
    if normalized_sub_type == SUB_TYPE_CREATOR:
        adapter = XhsCreatorLoginAdapter()
        channel = CHANNEL_CREATOR
    elif normalized_sub_type == SUB_TYPE_PC:
        adapter = XhsPcLoginAdapter()
        channel = CHANNEL_WWW
    else:
        raise UnknownSubTypeError(
            f"sub_type={normalized_sub_type!r} 未在 _check_single_account 中显式分支；"
            "请同步更新本函数（禁止 else 兜底）。"
        )

    # ⚠️ 按**要打哪个平台**取通道，不再「取最新一版再自己解析」：拆分信封
    # （迁移 `j3k4l5m6n7o8`）之后 `decode_cookie_text(信封)` 返回的是
    # `{"v":1,"channels":{...}}` —— 一个**不含任何真实 Cookie 的 dict**，
    # 适配器必然认证失败，于是把好好的账号判成 `expired`（静默误判）。
    cookies_text = latest_cookies_for_channel(db, account.id, channel)
    if not cookies_text:
        account.status = "expired"
        account.status_message = f"该账号没有 {channel} 通道的令牌"
        account.updated_at = now
        return "expired"

    try:
        adapter.get_user_info(decode_cookie_text(cookies_text))
        old_status = account.status
        account.status = "active"
        account.status_message = ""
        account.updated_at = now
        return old_status
    except Exception as exc:
        old_status = account.status
        account.status = "expired"
        account.status_message = str(exc)
        account.updated_at = now
        return old_status


def check_all_account_cookies_once(user_id: int, account_ids: Optional[Sequence[int]] = None) -> None:
    """Scheduled job: check account cookies, update status, notify on expiry.

    ⚠️ **DEF-066（2026-09-24）**：`user_id` 必填（行归属者，runner 注入）——
    原「不传 = 全库账号」的形态已废：账号查询恒带 `PlatformAccount.user_id == user_id`。
    `account_ids` 非空 = 只巡检指定账号（多选，设计 15 §4.1 params.account_ids）；
    空 / 不传 = **本人的全部账号**。归属校验在建 / 改两条路都有（API 层），
    执行层再按行主收一道（防存量脏数据）。
    """
    db = SessionLocal()
    try:
        now = shanghai_now()
        # 软删账号不参与巡检（I3）：它的 Cookie 版本已被清掉，巡检只会刷出一堆
        # 「No stored cookie version」的假过期，还会把状态改坏。
        statement = select(PlatformAccount).where(
            PlatformAccount.user_id == user_id,
            PlatformAccount.deleted_at.is_(None),
        )
        if account_ids:
            statement = statement.where(PlatformAccount.id.in_(list(account_ids)))
        accounts = db.scalars(statement.order_by(PlatformAccount.id.asc())).all()
        checked = 0
        newly_expired = 0
        for account in accounts:
            try:
                old_status = _check_single_account(db, account, now)
                checked += 1
                if account.status == "expired" and old_status != "expired":
                    newly_expired += 1
                    db.add(Notification(
                        user_id=account.user_id,
                        title="账号 Cookie 过期",
                        body=f"账号「{account.nickname or account.external_user_id or account.id}」({account.sub_type or 'pc'}) Cookie 已失效，请重新绑定。",
                        level="warning",
                    ))
            except Exception as exc:
                logger.warning(f"Cookie check failed for account {account.id}: {exc}")
        db.commit()
        logger.info(f"Cookie check completed: {checked} accounts checked, {newly_expired} newly expired")
    except Exception as exc:
        logger.error(f"check_all_account_cookies_once failed: {exc}")
    finally:
        db.close()


def _persistent_jobstores() -> dict[str, Any]:
    """持久化 job store（设计 13 §24.9）：指向应用**同一个** SQLite 库。

    APScheduler 3.x 默认 `MemoryJobStore` ⇒ 进程退出即丢 job，且 interval 的
    `next_run_time` 重新从「现在 + 周期」起算。本机 1~2 小时一次代码重载
    （launcher 杀 uvicorn 自动重拉）⇒ 6 小时 interval 的水永远烧不完 ——
    这是「定时任务一直不执行」的第三因（前两因是两道开关闸）。

    框架自管 `apscheduler_jobs` 表（`scheduler.start()` 时建），**不计入业务
    表数**（`verify_doc_counts.py` 只数 `models/` 里声明的表）。
    ⚠️ 持久化 store 要求 job 函数可被**文本引用**（module 级函数）—— 生产路径
    传的都是 module 级函数；lambda 只能在内存 store 里用（测试）。
    """
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

    return {"default": SQLAlchemyJobStore(url=get_settings().database_url)}


def _stored_job_exists(store: Any, job_id: str) -> bool:
    """store 里是否已有同 id job。

    未启动的调度器 `get_job()` 只看**待注册队列**、看不到 store（实测），
    所以直接查 store 对象。表还没建（首次运行）/ 库暂不可读 ⇒ ``False``
    （按「不存在」处理，本轮照常注册）。
    """
    if store is None:
        return False
    try:
        return store.lookup_job(job_id) is not None
    except Exception:  # noqa: BLE001 - 首跑表不存在是预期路径
        return False


# --------------------------------------------------------------------------- #
# DB 驱动的调度装配（设计 15 §6.1，2026-09-24 重构）
#
# 旧形态（被设计 08 §1.1 记载、已被本段取代）：`build_due_publish_scheduler()`
# 硬编码注册 8 个 job，间隔 / 启停全部来自 config 环境变量、运行期不可见不可调。
# 新形态：`scheduled_jobs` 行是唯一权威 —— 启动 ensure/sync/prune，运行期由
# `/api/schedule` 写行后同步注册 / 摘除。§24.9「store 已有同 id job 跳过注册」
# 的跨重启计时守卫原样保留。
# --------------------------------------------------------------------------- #

#: APScheduler job id = `job_{行 id}`（稳定跨重启；与旧硬编码 id 不共存，prune 清掉残留）
JOB_ID_PREFIX = "job_"


def _apscheduler_job_id(job_id: int) -> str:
    return f"{JOB_ID_PREFIX}{job_id}"


def _cron_every_n_hours(hours: int) -> str:
    """整数小时 → cron（R14：interval 退役后周期类一律用 cron 表达）。"""
    if hours <= 1:
        return "0 0 * * * *"
    if hours <= 23:
        return f"0 0 */{hours} * * *"
    return "0 0 0 * * *"


#: 种子行备注（R24）：初始化数据那条路径的默认文案
_BUILTIN_SEED_REMARK = "系统内置任务（应用初始化数据，随安装附有；调度属性可调）"
#: 兜底路径（`config/initial_data/` 里没写这个 job_key）的文案 —— 显式区分来源
_FALLBACK_SEED_REMARK = "系统内置任务（代码默认种子，未在应用初始化数据中声明；调度属性可调）"


def _builtin_seed_defaults() -> dict[str, dict[str, Any]]:
    """内置 job 的**兜底**种子默认值（**只在初始化数据缺该 job_key 时**才用）。

    ⚠️ **R24（2026-09-24 用户口径）**：种子权威已迁到**随安装包分发的应用初始化数据**
    （`config/initial_data/scheduled_jobs.json`，设计 15 §4.5），本函数降级为兜底 ——
    正常安装路径下它**完全不参与**（连 config 键也一并降级为兜底值）。
    原「迁移写死值 vs 本函数读 config 必须一致」的约束**已作废**：迁移播种段已删。

    ⚠️ R14（2026-09-24 用户口径）：种子触发器一律 **cron**（interval 已退役）；
    选填的 `cron` 键覆盖注册表默认（config 里的小时数照旧可调）。
    """
    settings = get_settings()
    every_minute = max(
        1, int(getattr(settings, "scheduler_interval_seconds", 60) or 60)
    ) // 60

    def hours(key: str, default: int) -> int:
        return max(1, int(getattr(settings, key, default) or default))

    minute_cron = "0 * * * * *" if every_minute <= 1 else f"0 */{min(every_minute, 59)} * * * *"

    return {
        "system.due_publish": {"cron": minute_cron, "status": "active"},
        "system.monitoring_refresh": {"cron": minute_cron, "status": "active"},
        "system.cookie_health_check": {"cron": _cron_every_n_hours(2), "status": "active"},
        "system.qianfan_health_check": {
            "cron": _cron_every_n_hours(hours("qianfan_health_check_interval_hours", 6)),
            "status": "active" if bool(getattr(settings, "qianfan_health_check_enabled", True)) else "paused",
        },
        "system.no_traffic_cleanup": {
            "cron": _cron_every_n_hours(hours("no_traffic_cleanup_interval_hours", 6)),
            "status": "active" if bool(getattr(settings, "no_traffic_cleanup_enabled", True)) else "paused",
        },
        "system.note_sync": {
            "cron": _cron_every_n_hours(hours("xhs_note_sync_interval_hours", 6)),
            "status": "active" if bool(getattr(settings, "xhs_note_sync_enabled", False)) else "paused",
        },
        "system.shop_goods_sync": {
            "cron": _cron_every_n_hours(hours("xhs_shop_goods_sync_interval_hours", 6)),
            "status": "active" if bool(getattr(settings, "xhs_shop_goods_sync_enabled", False)) else "paused",
        },
    }


def seed_payloads() -> list[tuple[JobSpec, dict[str, Any]]]:
    """本次要补种的 `(spec, 值)` 清单：**应用初始化数据优先，代码默认兜底**（§4.5）。

    名单 = 注册表里 `builtin=True` 的 spec —— 好处是**新增内置任务即便忘了写进
    JSON 也会被补上**（用 `JobSpec` 默认值），不会因为漏一行 JSON 就少一个任务。
    反向：JSON 里出现注册表没有的 `job_key` ⇒ warning 并跳过（**不插没有 func 的死行**）。
    """
    from backend.app.core.initial_data import load_initial_scheduled_jobs

    registry = {key: spec for key, spec in job_registry().items() if spec.builtin}
    initial = {row["job_key"]: row for row in load_initial_scheduled_jobs()}
    for key in initial:
        if key not in registry:
            logger.warning("ensure_builtin_jobs: 初始化数据里的 %r 不在注册表，已跳过", key)

    fallbacks = _builtin_seed_defaults()
    payloads: list[tuple[JobSpec, dict[str, Any]]] = []
    for key, spec in registry.items():
        row = initial.get(key)
        if row is None:
            fallback = fallbacks.get(key, {})
            row = {
                "name": spec.name,
                "trigger_type": spec.default_trigger_type,
                "cron_expression": fallback.get("cron", spec.default_cron),
                "misfire_policy": spec.default_misfire_policy,
                "concurrent": CONCURRENT_ALLOW if spec.allow_concurrent else CONCURRENT_FORBID,
                "params": {},
                "status": fallback.get("status", JOB_STATUS_ACTIVE),
                "remark": _FALLBACK_SEED_REMARK,
            }
        payloads.append((spec, row))
    return payloads


def seed_builtin_jobs_for_user(db: Session, user_id: int) -> list[ScheduledJob]:
    """给**一个用户**补种缺失的内置 job 行（幂等：同 `(user_id, job_key)` 不插、不覆盖）。

    DEF-066（2026-09-24，设计 15 §4.1.1）：内置行按用户各持一份 —— 原全局共享单行
    （user_id IS NULL）是多用户互相操作同一份数据的载体，整条退役。判重从
    「`job_key + is_builtin` 全局唯一」改为「`job_key + user_id`」。

    播种值来自**应用初始化数据**（设计 15 §4.5，随安装包分发），代码默认兜底。
    「只补缺失、不覆盖已有」是升级语义的关键：用户在任务中心调过的调度属性
    不会因为装了个新版本被重置回初始化值。

    ⚠️ 种子里的 `account_ids` 统一重置为 `[]`（= 本人全部账号）：初始化数据是随包
    分发的**基线**，不应携带任何账号 id；即便被手改出 id，落给某个用户也是越权引用。
    """
    payloads = seed_payloads()
    if not payloads:
        return []
    existing = set(
        db.scalars(
            select(ScheduledJob.job_key).where(ScheduledJob.user_id == user_id)
        ).all()
    )
    created: list[ScheduledJob] = []
    for spec, row in payloads:
        if spec.key in existing:
            continue
        params = dict(row.get("params") or {})
        # 只清洗**已存在**的 account_ids 键（不发明键）：非账号维度的任务
        # （due_publish / monitoring_refresh）params 保持 `{}`，与初始化数据逐字段一致
        if "account_ids" in params:
            params["account_ids"] = []
        job = ScheduledJob(
            user_id=user_id,
            name=row["name"],
            job_key=spec.key,
            trigger_type=row["trigger_type"],
            cron_expression=row["cron_expression"],
            misfire_policy=row["misfire_policy"],
            concurrent=row["concurrent"],
            params=params,
            status=row["status"],
            is_builtin=True,
            remark=row["remark"] or _BUILTIN_SEED_REMARK,
        )
        db.add(job)
        created.append(job)
    if created:
        db.commit()
    return created


def ensure_builtin_jobs(db: Session) -> list[ScheduledJob]:
    """为**每个已存在用户**补种缺失的内置 job 行（幂等；零用户库 ⇒ 零行）。

    新用户的首批任务行不走本函数（启动时他还不存在）—— 走注册路径
    `POST /auth/register` → `seed_builtin_jobs_for_user`（设计 15 §4.5 播种时机②）。
    """
    users = db.scalars(select(User).order_by(User.id.asc())).all()
    created: list[ScheduledJob] = []
    for user in users:
        created.extend(seed_builtin_jobs_for_user(db, user.id))
    return created


def seed_builtin_jobs_from_initial_data() -> int:
    """应用启动时按**应用初始化数据**播种内置任务行（设计 15 §4.5）。返回新建行数。

    **与 `scheduler_enabled` 解耦**：桌面版默认不启调度器，但「定时任务中心」
    打开就得有那批任务行 —— 播种挂启动（而非挂调度器装配），需求才成立。

    失败只记 warning 不抛：初始化数据坏掉是**可容忍**的（用户仍能进任务中心手动建），
    但不能**静默** —— 所以这里必打一行，且打包侧有正向断言（`build.ps1` @ 自检）。
    """
    db = SessionLocal()
    try:
        created = ensure_builtin_jobs(db)
        if created:
            logger.info("应用初始化数据：补种 %s 个内置任务行", len(created))
        return len(created)
    except Exception as exc:  # noqa: BLE001 - 起服务不能被子系统问题拖死
        logger.warning("应用初始化数据播种失败（已跳过，可在任务中心手动新建）：%s", exc)
        return 0
    finally:
        db.close()


def _register_job_in_scheduler(scheduler: BackgroundScheduler, store: Any, job: ScheduledJob) -> bool:
    """把一行注册进调度器。返回是否真的注册了（False = §24.9 守卫跳过 / 无法注册）。"""
    # R14：手动执行档**不参与调度装配** —— 只能由「执行一次」触发，不注册。
    if job.trigger_type == TRIGGER_MANUAL:
        return False
    job_id = _apscheduler_job_id(job.id)
    if _stored_job_exists(store, job_id):
        # §24.9：store 里已有同 id job ⇒ 跳过，保住跨重启的 next_run_time
        # （无脑 replace_existing 会把它重置为「现在 + 周期」，持久化就白做了）。
        logger.info("scheduler: job %s already persisted, keeping its next_run_time", job_id)
        return False
    try:
        spec = get_job_spec(job.job_key)
    except KeyError:
        logger.warning("job %s references unknown job_key %r; not registered", job.id, job.job_key)
        return False
    try:
        trigger = build_trigger(job)
    except ValueError as exc:
        logger.warning("job %s has invalid trigger (%s); not registered", job.id, exc)
        return False
    grace, coalesce = misfire_params(job.misfire_policy)
    scheduler.add_job(
        run_scheduled_job,
        trigger,
        id=job_id,
        args=(job.id,),
        replace_existing=True,
        max_instances=max_instances_for(job, registry_allows_concurrent=spec.allow_concurrent),
        coalesce=coalesce,
        misfire_grace_time=grace,
    )
    return True


def sync_jobs_from_db(scheduler: BackgroundScheduler, store: Any = None) -> dict[str, int]:
    """启动同步：补种内置行 ⇒ 注册所有 active 行 ⇒ 清掉 store 里的孤儿 job。"""
    db = SessionLocal()
    registered = 0
    pruned = 0
    try:
        ensure_builtin_jobs(db)
        jobs = db.scalars(select(ScheduledJob).where(ScheduledJob.status == "active")).all()
        active_ids = {_apscheduler_job_id(job.id) for job in jobs}
        for job in jobs:
            # manual 档在 _register_job_in_scheduler 里直接返回 False（只能手动触发）。
            if _register_job_in_scheduler(scheduler, store, job):
                registered += 1
        if store is not None:
            try:
                for stored in store.get_all_jobs():
                    if stored.id not in active_ids:
                        store.remove_job(stored.id)
                        pruned += 1
            except Exception as exc:  # noqa: BLE001 - 首跑表不存在是预期路径
                logger.info("scheduler: store prune skipped (%s)", exc)
    finally:
        db.close()
    if pruned:
        logger.info("scheduler: pruned %s orphaned store job(s)", pruned)
    return {"registered": registered, "pruned": pruned}


def sync_job_registration(
    scheduler: Optional[BackgroundScheduler],
    job: ScheduledJob,
    *,
    trigger_changed: bool = True,
) -> None:
    """API 写入后同步（**先 DB commit 再调本函数**；失败只记日志，下次启动自愈）。

    - active ⇒ 注册 / 重注册（`trigger_changed=False` 且已注册 ⇒ 跳过保计时）；
    - paused / completed ⇒ 摘除。
    """
    if scheduler is None or not scheduler.running:
        return
    job_id = _apscheduler_job_id(job.id)
    try:
        # R14：manual 档只能手动触发 ⇒ 任何情况下都不注册，且要摘掉旧注册
        # （用户把 cron 改成 manual 时，残留的调度注册必须清掉）。
        if job.status != "active" or job.trigger_type == TRIGGER_MANUAL:
            _remove_job_quietly(scheduler, job_id)
            return
        if not trigger_changed and scheduler.get_job(job_id) is not None:
            return
        _remove_job_quietly(scheduler, job_id)
        _register_job_in_scheduler(scheduler, None, job)
    except Exception as exc:  # noqa: BLE001 - 同步失败不阻塞请求
        logger.warning(
            "scheduler: sync for job %s failed (%s); will retry on next boot", job.id, exc
        )


def _remove_job_quietly(scheduler: BackgroundScheduler, job_id: str) -> None:
    """从调度器摘除 job；没注册过是正常路径（`JobLookupError` 不炸）。"""
    try:
        scheduler.remove_job(job_id)
    except Exception:  # noqa: BLE001 - JobLookupError: 本就没注册
        pass


def build_scheduler(*, jobstores: Optional[dict[str, Any]] = None) -> BackgroundScheduler:
    """装配调度器（**不启动**；启动在 `start_scheduler`）。

    ``jobstores`` 传持久化 store 时启用「重启不重置计时」守卫（§24.9）；
    默认 ``None`` = APScheduler 默认内存 store（测试与一次性场景）。
    job 的注册不在这里做 —— DB 驱动，见 `sync_jobs_from_db()`。
    """
    scheduler_kwargs: dict[str, Any] = {"timezone": SCHEDULER_TIMEZONE}
    if jobstores:
        # ⚠️ 只在非空时传：显式传 None 会覆盖 APScheduler 的默认值并炸在
        # `_configure`（`config.get("jobstores", {})` 拿到 None）。
        scheduler_kwargs["jobstores"] = jobstores
    return BackgroundScheduler(**scheduler_kwargs)


def start_scheduler() -> BackgroundScheduler:
    """生产入口：装配（带持久化 job store）+ 启动同步 + 启动。"""
    jobstores = _persistent_jobstores()
    scheduler = build_scheduler(jobstores=jobstores)
    sync_jobs_from_db(scheduler, store=jobstores.get("default"))
    scheduler.start()
    return scheduler


def shutdown_scheduler(scheduler: Optional[BackgroundScheduler]) -> None:
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)


# --------------------------------------------------------------------------- #
# M1.9 同步的调度侧（T-806）
# --------------------------------------------------------------------------- #
def _sync_account_gap_seconds() -> int:
    """账号之间的停顿（秒）；配置读不到按 10 秒。"""
    try:
        return max(0, int(getattr(get_settings(), "xhs_sync_account_gap_seconds", 10) or 0))
    except Exception:  # pragma: no cover
        return 10


def _profile_notes_count(account) -> Optional[int]:
    """`profile_json.notes_count`（「有待同步」的分母）。取不到返回 ``None``。"""
    try:
        profile = json.loads(account.profile_json or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(profile, dict):
        return None
    value = profile.get("notes_count")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _local_note_count(db: Any, account_id: int) -> int:
    from sqlalchemy import func

    from backend.app.models.note import Note

    return int(
        db.scalar(
            select(func.count()).select_from(Note).where(Note.platform_account_id == account_id)
        )
        or 0
    )


def account_needs_note_sync(db: Any, account) -> bool:
    """「有待同步」判定（设计 13 §7.2 / D9）。

    平台总数（`profile_json.notes_count`）**缺值视为待同步** —— 宁可多跑一轮，
    不可漏（本地数拿不到平台总数时无从比较）。
    """
    total = _profile_notes_count(account)
    if total is None:
        return True
    return _local_note_count(db, account.id) < total


def _schedulable_accounts(db: Any, accounts: list) -> list:
    """能**无人值守**同步的账号：有创作者平台或千帆令牌的创作者链路账号。

    ⚠️ 2026-09-23 删掉 `has_web_session(www jar)` 过滤（设计 13 §24.9）：
    那是 www 源时代的残留 —— §21.4 起同步源只剩 creator + ark，**不需要**
    URL 级 `xsec_token`，「有 www 网页会话就要留给手动」的前提已不存在。
    留着它会把三通道齐全的账号（如观隅好物 acct 8）永久排除出定时轮。
    源可用性的唯一判据是 `_note_sources` 的 creator 令牌检查，不在这里预判
    （DEF-057：不用快照预判能不能取到）。
    """
    from backend.app.services.account_service import (
        CHANNEL_ARK,
        CHANNEL_CREATOR,
        channel_jars,
    )

    schedulable = []
    for account in accounts:
        # 2026-09-21 拆分：按平台看信封里有没有 creator / ark 平台，不再解密
        # 整行当 jar 用（信封被当 jar 用会产出 `v=1; channels=...` 垃圾 Cookie）。
        platforms = channel_jars(db, account.id)
        if not (CHANNEL_CREATOR in platforms or CHANNEL_ARK in platforms):
            continue
        if account.sub_type == "creator":
            schedulable.append(account)
    return schedulable


def run_scheduled_note_syncs(user_id: int, account_ids: Optional[Sequence[int]] = None) -> None:
    """调度器入口：给「有待同步」的账号跑**一次有界同步**（限行主本人，DEF-066）。

    ⚠️ `user_id` 必填（行归属者，runner 注入）：账号查询恒带
    `PlatformAccount.user_id == user_id` —— 原「空 account_ids = 全库账号」的形态已废。
    `account_ids` 非空 = 只跑指定账号（自定义参数，设计 15 §4.1，建 / 改双路归属校验）；
    空 = 本人的全部账号。

    三道防重/风控一起生效：

    * **轮与轮不重叠** —— job 层面 `max_instances=1 + coalesce`；
    * **同一账号不重入** —— 手动任务在跑时 `find_active_task` 让本轮跳过该账号
      （与手动触发共用同一条判断，所以两道入口不会互相打架）；
    * **账号之间停顿** —— 每处理完一个账号 sleep `xhs_sync_account_gap_seconds`。
    """
    from backend.app.models.platform_account import PlatformAccount
    from backend.app.services import sync_task_service as sts
    from backend.app.services.note_sync_service import run_note_sync

    gap = _sync_account_gap_seconds()
    db = SessionLocal()
    try:
        statement = select(PlatformAccount).where(
            PlatformAccount.user_id == user_id,
            PlatformAccount.platform == "xhs",
            PlatformAccount.deleted_at.is_(None),
        )
        if account_ids:
            statement = statement.where(PlatformAccount.id.in_(list(account_ids)))
        accounts = db.scalars(statement).all()
        candidates = _schedulable_accounts(db, list(accounts))
        skipped = len(accounts) - len(candidates)
        if skipped:
            logger.info(
                "scheduled note sync: skipped %s account(s) needing a pasted xsec_token", skipped
            )
        for account in candidates:
            try:
                if sts.find_active_task(db, sts.NOTE_SYNC_TASK_TYPE, account.id) is not None:
                    continue
                if not account_needs_note_sync(db, account):
                    continue
                run_note_sync(
                    account.id,
                    user_id=account.user_id,
                    source="scheduled",
                    # §22：调度也走 auto。第一次（无同步历史）自动全量补全，
                    # 之后自动增量 —— 与手动触发同一把尺子，不做两套口径。
                    mode=sts.SYNC_MODE_AUTO,
                )
            except Exception as exc:  # noqa: BLE001 - 单个账号失败不拖垮整轮
                logger.warning("scheduled note sync for account %s failed: %s", account.id, exc)
            if gap:
                time.sleep(gap)
    except Exception as exc:  # noqa: BLE001 - 调度器调用，必须吞异常
        logger.error("run_scheduled_note_syncs failed: %s", exc)
    finally:
        db.close()


def run_scheduled_shop_goods_syncs(user_id: int, account_ids: Optional[Sequence[int]] = None) -> None:
    """调度器入口：给每个**绑定了店铺的创作者账号**跑一次有界商品同步（限行主本人，DEF-066）。

    轮内纪律与笔记同步一致（不重叠 / 不重入 / 账号间停顿）。
    ⚠️ `user_id` 必填（行归属者，runner 注入）：账号查询恒带
    `PlatformAccount.user_id == user_id`；`account_ids` 非空 = 只跑指定账号，
    空 = 本人的全部账号。
    """
    from backend.app.models.platform_account import PlatformAccount
    from backend.app.services import sync_task_service as sts
    from backend.app.services.shop_binding_service import account_shop_id
    from backend.app.services.shop_goods_sync_service import run_shop_goods_sync

    gap = _sync_account_gap_seconds()
    db = SessionLocal()
    try:
        statement = select(PlatformAccount).where(
            PlatformAccount.user_id == user_id,
            PlatformAccount.platform == "xhs",
            PlatformAccount.deleted_at.is_(None),
        )
        if account_ids:
            statement = statement.where(PlatformAccount.id.in_(list(account_ids)))
        accounts = db.scalars(statement).all()
        for account in accounts:
            try:
                # 商品只对有店铺的账号有意义（没绑店 → 不做无意义的失败）。
                if account_shop_id(db, account.id) is None:
                    continue
                if sts.find_active_task(db, sts.SHOP_GOODS_SYNC_TASK_TYPE, account.id) is not None:
                    continue
                run_shop_goods_sync(
                    account.id,
                    user_id=account.user_id,
                    source="scheduled",
                    # §22：同笔记侧，auto 让「无同步历史 ⇒ 全量」也覆盖到定时任务。
                    mode=sts.SYNC_MODE_AUTO,
                )
            except Exception as exc:  # noqa: BLE001 - 单个账号失败不拖垮整轮
                logger.warning("scheduled goods sync for account %s failed: %s", account.id, exc)
            if gap:
                time.sleep(gap)
    except Exception as exc:  # noqa: BLE001 - 调度器调用，必须吞异常
        logger.error("run_scheduled_shop_goods_syncs failed: %s", exc)
    finally:
        db.close()


def run_scheduled_cleanups(user_id: int, account_ids: Optional[Sequence[int]] = None) -> None:
    """调度器入口：给有待清理候选的账号跑**一批**（限行主本人，DEF-066）。

    `account_ids` 非空 = 只跑指定账号（自定义参数，设计 15 §4.1）；空 = 本人的全部账号。
    ⚠️ `user_id` 必填（行归属者，runner 注入）—— 无流量清理是**平台删除操作**，
    跨租户触发是最有害的一条路，账号查询恒带 `PlatformAccount.user_id == user_id`。

    * 不重新建任务行：用户手动发起的清理（``run_no_traffic_cleanup_background``）
      已在跑时，``_running_cleanup`` 会让本次直接跳过。
    * 每个账号一批（有上限），跑完就停 —— 大池子靠多轮调度分摊，绝不一次性清空。
    """
    from backend.app.services.no_traffic_cleanup_service import (
        DEFAULT_DELETE_BATCH,
        CLEANUP_TASK_TYPE,
        STATUS_PENDING,
        STATUS_RUNNING,
        run_no_traffic_cleanup,
    )
    from backend.app.services.no_traffic_service import (
        TrafficThresholds,
        list_no_traffic_candidates,
    )

    db = SessionLocal()
    try:
        statement = select(PlatformAccount).where(
            PlatformAccount.user_id == user_id,
            PlatformAccount.platform == "xhs",
            PlatformAccount.deleted_at.is_(None),
        )
        if account_ids:
            statement = statement.where(PlatformAccount.id.in_(list(account_ids)))
        accounts = db.scalars(statement).all()
        for account in accounts:
            try:
                # 已有手动清理在跑 → 跳过，避免重复触发。
                from backend.app.services.no_traffic_cleanup_service import (
                    _running_cleanup,
                )

                if _running_cleanup(db, account.id) is not None:
                    continue
                probe = list_no_traffic_candidates(db, account.id, TrafficThresholds(), limit=1)
                if probe["total"] == 0:
                    continue
                # DEF-066：Task 审计行归属**账号主人**（原不传 user_id ⇒ 兜底
                # `user_id or 1`，任务记录落到 user 1 名下，主人看不到、user 1 看全场）。
                run_no_traffic_cleanup(account.id, batch_limit=DEFAULT_DELETE_BATCH, user_id=account.user_id)
            except Exception as exc:  # noqa: BLE001 - 单个账号失败不拖垮整轮
                logger.warning(f"scheduled no-traffic cleanup for account {account.id} failed: {exc}")
    except Exception as exc:
        logger.error(f"run_scheduled_cleanups failed: {exc}")
    finally:
        db.close()
