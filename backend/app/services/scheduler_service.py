from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional, Sequence

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

    ⚠️ **T-111 / DEF-016（2026-10-04）**：`crawl_interval_minutes` 此前是**死配置**
    （只被序列化下发、调度不读）⇒ 用户改了周期不生效，且没有任何提示。
    现在它是**真正的节流闸**：距上次刷新不足 `crawl_interval_minutes` 的目标
    本轮跳过（`skipped_count` 如实回报），到点的才刷。这样「周期」这个字段
    对用户是**真的**（与限流额度去硬编码 DEF-076 同一类修复）。
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

        items: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for target in targets:
            interval = max(int(target.crawl_interval_minutes or 60), 1)
            last = target.last_refreshed_at
            if last is not None and (now - last).total_seconds() < interval * 60:
                # 未到点：跳过（带原因如实回报 —— 静默跳过正是 DEF-048 那类问题）
                skipped.append(
                    {
                        "target_id": target.id,
                        "reason": "not_due",
                        "crawl_interval_minutes": interval,
                        "last_refreshed_at": last.isoformat(),
                    }
                )
                continue
            items.append(_refresh_monitoring_target(db, target, now, platform))
        db.commit()
        return {
            "refreshed_count": len(items),
            "skipped_count": len(skipped),
            "skipped": skipped,
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


#: job store 引擎等写锁的上限（秒，DEF-067）。默认只有 pysqlite 的 5 秒。
JOBSTORE_BUSY_TIMEOUT_SECONDS = 30

#: APScheduler 后台主循环的线程名（3.11 `BackgroundScheduler.start` 里写死）。
SCHEDULER_THREAD_NAME = "APScheduler"

#: 守活看门狗的巡检间隔（秒）与重建失败后的退避上限（秒）。
WATCHDOG_INTERVAL_SECONDS = 30.0
WATCHDOG_MAX_BACKOFF_SECONDS = 600.0
#: 看门狗退出信号（`stop_scheduler_watchdog()` 置位，启动时复位）。
_watchdog_stop = threading.Event()


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

    ⚠️ **`engine_options` 是必需项，别删（DEF-067）**：job store **自建引擎**、
    不走 `core/database.py` 的事件监听 ⇒ 拿不到 `PRAGMA busy_timeout=5000`，
    只剩 pysqlite 默认的 **5 秒**。5 秒内拿不到写锁就抛 `database is locked`；
    而这一抛发生在 **APScheduler 主循环里**（`base.py::_process_jobs` 的
    `jobstore.update_job`）⇒ **整个调度线程当场死掉**（实测 2026-09-25
    16:00:06 起，作业 14 分钟一个没跑；`scheduler.running` 仍是 `True`，
    状态端点看不出来）。给足等待时长把「抛异常」变成「等一会儿」。
    """
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

    return {
        "default": SQLAlchemyJobStore(
            url=get_settings().database_url,
            engine_options={
                "connect_args": {
                    "timeout": JOBSTORE_BUSY_TIMEOUT_SECONDS,
                    "check_same_thread": False,
                }
            },
        )
    }


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


#: 种子行备注（2026-10-07 用户口径「备注全部改为：系统内置任务」）：
#: 原先按来源分两种长文案（随包初始化数据 / 代码兜底），用户看到的只是一句
#: 来路说明、对使用没有价值 ⇒ **两种来源统一成一句话**。
#: ⚠️ 刻意**合并成一个常量**：两个常量若留着一个不同的值，就还会漏出一种文案。
#: 旧文案的存量行由迁移 `j1k2l3m4n5o6` 统一改写成这个值。
_BUILTIN_SEED_REMARK = "系统内置任务"
#: 兜底路径**同一个值**（保留别名只为不改动调用点语义，值必须与上面一致）。
_FALLBACK_SEED_REMARK = _BUILTIN_SEED_REMARK


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
        # ⚠️ 2026-10-07 用户口径「无流量笔记清理任务表移除该配置」：该任务的
        #    注册表项、随包种子与**本兜底字典**三者必须同时消失 —— 少删一处
        #    （尤其是这里）就会让「初始化数据缺该键」的路径又把它播种回任务列表。
        #    （原句「保留条目只为兼容路径」见 git 历史；清理本身未退场，入口仍在
        #    「我的笔记」页。）
        "system.note_sync": {
            "cron": _cron_every_n_hours(hours("xhs_note_sync_interval_hours", 6)),
            "status": "active" if bool(getattr(settings, "xhs_note_sync_enabled", False)) else "paused",
        },
        # §44（2026-09-30）：商品笔记同步独立成任务 ⇒ 独立开关 / 周期（与普通笔记同款兜底语义）。
        "system.goods_note_sync": {
            "cron": _cron_every_n_hours(hours("xhs_goods_note_sync_interval_hours", 6)),
            "status": (
                "active"
                if bool(getattr(settings, "xhs_goods_note_sync_enabled", False))
                else "paused"
            ),
        },
        "system.shop_goods_sync": {
            "cron": _cron_every_n_hours(hours("xhs_shop_goods_sync_interval_hours", 6)),
            "status": "active" if bool(getattr(settings, "xhs_shop_goods_sync_enabled", False)) else "paused",
        },
        # 2026-10-03 M2 切片 2（设计 17 §13.9）：两个指标快照采集。
        # ⚠️ **无开关**（兜底恒 `active`）：采集是纯本地零平台请求，没有「该不该跑」
        # 的成本问题；关掉它只会让趋势断档。真要停，用户在任务中心暂停即可。
        # ⚠️ 分钟错开（5 / 35）见 `job_registry` 的同一注释（减少写锁竞争，DEF-085）。
        "system.note_metrics_collect": {"cron": "0 5 * * * *", "status": "active"},
        "system.shop_metrics_collect": {"cron": "0 35 * * * *", "status": "active"},
        # 2026-10-06 需求：离线笔记数据同步（商业+普通，默认每 6 小时一次，恒 active）。
        "system.note_data_sync": {"cron": "0 0 */6 * * *", "status": "active"},
        # 2026-10-07 用户需求「每日发布商业笔记」（设计 15 §19）：每天 9:00 跑一轮。
        # ⚠️ 默认 active —— 与两个同步任务不同（它们默认关，因为会大量打平台只读
        # 接口）；本任务只在**用户已配好千帆 + 有 0 篇挂链商品**时才真的出手。
        "system.business_note_auto_publish": {"cron": "0 0 9 * * *", "status": "active"},
        # 2026-10-11 用户需求：图片空间「清理未引用图片」—— 用户口径「每周一执行一次」。
        # ⚠️ 默认 **paused**：它是**平台写操作**（批量删除 + 彻底删除不可还原），
        # 与「每日发布商业笔记」不同 —— 那个只在配好千帆后才有动作，这个一旦跑起来
        # 就真的删图。让用户先去任务中心确认参数（尤其 `delete_mode`）再手动启用。
        "system.picture_space_cleanup": {"cron": "0 0 0 * * MON", "status": "paused"},
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
    """生产入口：装配（带持久化 job store）+ 启动同步 + 启动。

    ⚠️ **T-108 / DEF-003：本函数只负责**启动**；多实例防护在 `single_instance_guard`
    （`core/singleton.py`）—— `main.lifespan` 先过闸，抢不到租约的进程**根本不进来**。
    在这里再加一道锁会与 apscheduler 的 jobstore 抢同一个 DB 写点（反而更脆）。
    """
    jobstores = _persistent_jobstores()
    scheduler = build_scheduler(jobstores=jobstores)
    sync_jobs_from_db(scheduler, store=jobstores.get("default"))
    scheduler.start()
    return scheduler


def shutdown_scheduler(scheduler: Optional[BackgroundScheduler]) -> None:
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)


# --------------------------------------------------------------------------- #
# 调度器守活（DEF-067，2026-09-25）
# --------------------------------------------------------------------------- #


def scheduler_thread_alive() -> bool:
    """APScheduler 主循环线程是否还活着（**唯一可信的存活判据**）。

    ⚠️ 别用 `scheduler.running`：3.11 的 `state` 只在 `shutdown()` 里改，
    主循环线程因异常死掉时它仍是 `STATE_RUNNING` ⇒ `running` 返回 `True`，
    `/api/schedule` 状态端点会报「运行中」而实际上一个作业都不会跑
    （2026-09-25 实测：16:00 起作业静默停摆 14 分钟，端点照样说 running）。
    """
    return any(
        thread.name == SCHEDULER_THREAD_NAME and thread.is_alive()
        for thread in threading.enumerate()
    )


def revive_scheduler(
    holder: Any,
    *,
    build: Optional[Callable[[], BackgroundScheduler]] = None,
) -> BackgroundScheduler:
    """主循环已死 ⇒ **重建一台新的**并换进 `holder.scheduler`。

    为什么不是「重启同一台」：3.11 的 `shutdown()` 会把 `ThreadPoolExecutor`
    一起关掉（`BasePoolExecutor.shutdown` → `_pool.shutdown()`），而
    `BaseExecutor.start()` **不会重建池子** ⇒ 复用同一台会造出「循环在跑、
    作业提交不了」的僵尸。只有 `BackgroundScheduler(...)` 新实例是干净的。

    旧实例照旧 `shutdown`（关执行器与 job store，释放它可能还攥着的库连接）。
    """
    old = getattr(holder, "scheduler", None)
    if old is not None:
        try:
            old.shutdown(wait=False)
        except Exception:  # noqa: BLE001 - 旧实例已异常，关不掉也要继续重建
            logger.warning("旧调度器 shutdown 失败（主循环已死，继续重建）", exc_info=True)
    builder = build or start_scheduler
    scheduler = builder()
    holder.scheduler = scheduler
    logger.warning("调度器已重建并接管（旧主循环已死，作业由 DB 重新注册）")
    return scheduler


def start_scheduler_watchdog(
    holder: Any,
    *,
    interval: float = WATCHDOG_INTERVAL_SECONDS,
    build: Optional[Callable[[], BackgroundScheduler]] = None,
) -> threading.Thread:
    """守活线程：定期检查主循环线程，死了就重建（APScheduler 自己不自愈）。

    重建失败按 `interval` 指数退避（上限 `WATCHDOG_MAX_BACKOFF_SECONDS`），
    防「库长期不可用」时每 30 秒造一台调度器把线程/连接耗光。
    进程收尾调 :func:`stop_scheduler_watchdog`（否则测试里 app 下线后
    看门狗还会复活一台指向真库的调度器）。
    """
    _watchdog_stop.clear()

    def _loop() -> None:
        backoff = interval
        while not _watchdog_stop.is_set():
            if _watchdog_stop.wait(backoff):  # 可被 stop 打断的等待
                return
            try:
                if scheduler_thread_alive():
                    backoff = interval
                    continue
                logger.error("调度主循环已死（`%s` 线程不存在）—— 重建调度器", SCHEDULER_THREAD_NAME)
                revive_scheduler(holder, build=build)
                backoff = interval
            except Exception:  # noqa: BLE001 - 看门狗本身绝不能被一次失败带走
                logger.exception("调度看门狗重建失败，%.0f 秒后重试", backoff)
                backoff = min(backoff * 2, WATCHDOG_MAX_BACKOFF_SECONDS)

    thread = threading.Thread(target=_loop, name="scheduler-watchdog", daemon=True)
    thread.start()
    return thread


def stop_scheduler_watchdog() -> None:
    """让看门狗退出巡检（幂等；下一次 `start_scheduler_watchdog` 会自动复位）。"""
    _watchdog_stop.set()


# --------------------------------------------------------------------------- #
# M1.9 同步的调度侧（T-806）
# --------------------------------------------------------------------------- #
def _sync_account_gap_seconds() -> int:
    """账号之间的停顿（秒）；配置读不到按 10 秒。

    ⚠️ 2026-09-27（设计 10 §7.5）：读点改走**运行时配置**（`system_settings` 行优先，
    回落 settings / 内置 10）—— 它此前只读 `get_settings()`，于是「设置页改了它」
    不会有任何效果（而白名单却把它标成「实时生效」⇒ 负向失真）。
    进程级开关（调度轮不是某个账号私有的）⇒ 用 `live_int_process`。
    """
    try:
        from backend.app.services.settings_service import live_int_process

        return max(
            0,
            int(
                live_int_process(
                    "xhs_sync_account_gap_seconds",
                    default=int(getattr(get_settings(), "xhs_sync_account_gap_seconds", 10) or 10),
                )
            ),
        )
    except Exception:  # pragma: no cover - 配置读不到不该让调度轮停摆
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


#: 定向执行等待业务任务到终态的轮询间隔（秒）。
TARGETED_AWAIT_POLL_SECONDS = 2.0

#: 定向执行等待业务任务终态的上限（秒）。60 分钟 = §21.5 实测全量同步（30–48 分钟）
#: 之上留余量；超时不「静默变成成功」，而是记失败并说明任务仍在后台跑。
TARGETED_AWAIT_TIMEOUT_SECONDS = 60 * 60


#: 定向执行（`account_ids` 非空）也**照旧**过省流与账号间停顿：
#: 「用户点名了这几个账号」不等于「每一轮都无条件打平台」——省流判据是**账号级**的
#: 保护（本地已是最新就别打），与「跑哪些账号」是两个正交的问题。真正需要「立刻、
#: 不打折」的是账号卡的按钮，那条路不走这里（它直接建任务行，§35.17.2）。


def _targeted_outcome(results: list[dict[str, Any]], *, what: str) -> dict[str, Any]:
    """定向执行的统一收口（设计 13 §35.17.2）：有失败 ⇒ **抛错**，否则回摘要。

    为什么有失败必须抛：`execute_job` 只有「执行体抛异常」才会把 `scheduled_job_logs`
    写成 `failed`。若执行体自己吞掉失败、正常返回，日志会显示「执行成功」而 `result`
    里躺着错误原因 —— 那就是**假成功**，正是本次要消灭的形态（§35.17.1 实测：最新
    10 条定时日志全是 `success` / `progress=100` / 0–133ms，而实际什么都没跑）。

    `skipped`（省流判定本地已最新）与「该账号已有任务在跑」都**不算失败** ——
    那是「没执行」，不是「执行失败」，说明留在 `result` 里给用户看即可。
    """
    failed = [
        row
        for row in results
        if str(row.get("status") or "") == "failed" or row.get("error")
    ]
    if failed:
        detail = "；".join(
            "账号 %s：%s" % (row.get("account_id"), row.get("error") or row.get("note") or "未知原因")
            for row in failed
        )
        raise RuntimeError(f"{what}失败：{detail}"[:1000])
    return {"targeted": True, "what": what, "accounts": results}


def _targeted_result(account_id: int, terminal: dict[str, Any]) -> dict[str, Any]:
    """把一个业务任务的终态整理成 `scheduled_job_logs.result` 里的一行（缺值给 None）。"""
    progress = terminal.get("progress") or {}
    return {
        "account_id": account_id,
        "task_id": terminal.get("task_id"),
        "status": terminal.get("status"),
        "stored": progress.get("stored"),
        "total": progress.get("total"),
        "note": terminal.get("note") or "",
        "error": terminal.get("error") or "",
    }


def _run_targeted_note_sync(account: Any) -> dict[str, Any]:
    """定向（点名了账号）的单账号执行：**建行 → 账号任务 worker 跑 → 等终态**。

    为什么不在这里直接 `run_note_sync`（批量路径就是那么干的）：§35.16 确立的
    「单 worker 串行」是用户要过的能力（不与笔记级队列抢平台额度）—— 直接同步跑就等于
    把它绕过去了，两个账号同时点就是并排打平台。建行 + worker ⇒ 串行天然成立；
    这里的等待只为拿到**真实终态**写日志（§35.17.2）。

    ⚠️ 来源写 `scheduled`（不是 `account_card_*`）：这条路的调用方是**调度侧**
    （cron 或任务中心的「执行一次」）。账号卡的按钮走的是另一条路
    （端点自己建行、来源 `account_card_*`，§35.17.2）—— 两处都写同一个来源就等于
    在数据里造假历史（§35.12「来源要具体到按钮」）。
    """
    from backend.app.services import sync_task_service as sts
    from backend.app.services.note_queue_service import SOURCE_SCHEDULED
    from backend.app.services.note_sync_service import start_note_sync

    summary = start_note_sync(account.id, user_id=account.user_id, source=SOURCE_SCHEDULED)
    task_id = summary.get("task_id")
    if summary.get("reused"):
        return {
            "account_id": account.id,
            "task_id": task_id,
            "status": summary.get("status"),
            "note": f"该账号已有同步任务在跑（任务 #{task_id}），本次不重入",
        }
    return _targeted_result(account.id, _await_targeted_task(task_id))


def _run_targeted_goods_sync(account: Any) -> dict[str, Any]:
    """定向（点名了账号）的单账号**商品**执行 —— 与笔记侧**逐字同构**（只是函数不同）。

    同构是刻意的：两条链路的差别只有 `task_type` 与执行体，纪律（建行 → worker 串行 →
    等终态 → 记真实结果）必须一致，否则又会长出两套会各自漂移的实现。
    """
    from backend.app.services.note_queue_service import SOURCE_SCHEDULED
    from backend.app.services.shop_goods_sync_service import start_shop_goods_sync

    summary = start_shop_goods_sync(account.id, user_id=account.user_id, source=SOURCE_SCHEDULED)
    task_id = summary.get("task_id")
    if summary.get("reused"):
        return {
            "account_id": account.id,
            "task_id": task_id,
            "status": summary.get("status"),
            "note": f"该账号已有商品同步任务在跑（任务 #{task_id}），本次不重入",
        }
    return _targeted_result(account.id, _await_targeted_task(task_id))


def _await_targeted_task(task_id: Optional[int]) -> dict[str, Any]:
    """等定向建出的业务任务到终态（实现住在 `sync_task_service`，这里只给口径）。"""
    from backend.app.services import sync_task_service as sts

    if not task_id:
        return {"task_id": None, "status": "missing", "error": "任务行未建出来（建行即失败）"}
    return sts.await_task_terminal(
        task_id,
        timeout_seconds=TARGETED_AWAIT_TIMEOUT_SECONDS,
        poll_seconds=TARGETED_AWAIT_POLL_SECONDS,
    )


def run_scheduled_note_syncs(
    user_id: int,
    account_ids: Optional[Sequence[int]] = None,
) -> Optional[dict[str, Any]]:
    """调度器入口：给账号跑一次有界同步（限行主本人，DEF-066）。

    ⚠️ `user_id` 必填（行归属者，runner 注入）：账号查询恒带
    `PlatformAccount.user_id == user_id` —— 原「空 account_ids = 全库账号」的形态已废。
    `account_ids` 非空 = 只跑指定账号（自定义参数，设计 15 §4.1，建 / 改双路归属校验）；
    空 = 本人的全部账号。

    **只有一处按 `account_ids` 分流**（设计 13 §35.17.2）：预筛怎么处理「跑不了的账号」。

    | 维度 | `account_ids` 空（定时轮转） | 非空（点名了账号） |
    |---|---|---|
    | 预筛 | `_schedulable_accounts` 挑创作者链路账号，**安静跳过** | **不预筛** ⇒ 交给下游给结构化失败（如 `CREATOR_SESSION_REQUIRED`）并**进 `result`**：用户点了名，必须知道这个账号为什么没跑，而不是看到一条安静的「执行成功」 |
    | 省流判据 | `account_needs_note_sync` 生效 | **同样生效**（账号级保护，与「跑哪些账号」正交）；被判「本地已最新」时进 `result` 说明，不静默 |
    | 账号间停顿 | `xhs_sync_account_gap_seconds` | **同样生效**（点名了 5 个账号 ≠ 可以并排打平台） |
    | 返回 | `None` | 每账号结果摘要（进 `scheduled_job_logs.result`）；有失败则**抛错** ⇒ 日志记 `failed`（不是假成功） |

    三道防重/风控一起生效（**两种模式都保留**）：

    * **轮与轮不重叠** —— job 层面 `max_instances=1 + coalesce`；
    * **同一账号不重入** —— 已有在跑的任务时本轮跳过该账号（与手动触发共用同一条判断，
      所以两道入口不会互相打架）；
    * **账号之间停顿** —— 每处理完一个账号 sleep `xhs_sync_account_gap_seconds`。

    ⚠️ 账号卡的「同步笔记」按钮**不走这里**（§35.17.2 已改）：它要的是「立刻跑一次、
    别省流」，并且要留下 `trigger_type=manual` 的定时日志 —— 那条路在端点里直接建任务行，
    本函数是**调度侧**的入口，职责没变。
    """
    from backend.app.models.platform_account import PlatformAccount
    from backend.app.services import sync_task_service as sts
    from backend.app.services.note_queue_service import SOURCE_SCHEDULED
    from backend.app.services.note_sync_service import run_note_sync

    targeted = bool(account_ids)
    gap = _sync_account_gap_seconds()
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
        accounts = db.scalars(statement).all()
        if targeted:
            candidates = list(accounts)
        else:
            candidates = _schedulable_accounts(db, list(accounts))
            skipped = len(accounts) - len(candidates)
            if skipped:
                logger.info(
                    "scheduled note sync: skipped %s account(s) not on the creator channel", skipped
                )
        for account in candidates:
            try:
                active = sts.find_active_task(db, sts.NOTE_SYNC_TASK_TYPE, account.id)
                if active is not None:
                    if targeted:
                        results.append(
                            {
                                "account_id": account.id,
                                "task_id": active.id,
                                "status": active.status,
                                "note": (
                                    f"该账号已有笔记同步任务在跑（任务 #{active.id}，"
                                    f"{active.status}），本次不重入"
                                ),
                            }
                        )
                    continue
                if not account_needs_note_sync(db, account):
                    if targeted:
                        results.append(
                            {
                                "account_id": account.id,
                                "status": "skipped",
                                "note": "本地数据已是最新（省流判据），本次未打平台",
                            }
                        )
                    continue
                if targeted:
                    # 建行 + 账号任务 worker 串行执行 + 等终态（见 `_run_targeted_note_sync`）。
                    results.append(_run_targeted_note_sync(account))
                    continue
                run_note_sync(
                    account.id,
                    user_id=account.user_id,
                    source=SOURCE_SCHEDULED,
                    # §22：调度也走 auto。第一次（无同步历史）自动全量补全，
                    # 之后自动增量 —— 与手动触发同一把尺子，不做两套口径。
                    mode=sts.SYNC_MODE_AUTO,
                )
            except Exception as exc:  # noqa: BLE001 - 单个账号失败不拖垮整轮
                logger.warning("scheduled note sync for account %s failed: %s", account.id, exc)
                if targeted:
                    results.append({"account_id": account.id, "status": "failed", "error": str(exc)})
            if gap:
                time.sleep(gap)
    except Exception as exc:  # noqa: BLE001 - 调度器调用，必须吞异常
        logger.error("run_scheduled_note_syncs failed: %s", exc)
    finally:
        db.close()

    if not targeted:
        return None
    return _targeted_outcome(results, what="笔记同步")


def _run_targeted_goods_note_sync(account: Any) -> dict[str, Any]:
    """定向（点名了账号）的单账号**商品笔记**执行 —— 与笔记侧**逐字同构**（§44）。

    同构是刻意的（DEF-044）：两条链路的差别只有入口函数与 `task_type`，纪律
    （建行 → worker 串行 → 等终态 → 记真实结果）必须一致。
    """
    from backend.app.services.note_queue_service import SOURCE_SCHEDULED
    from backend.app.services.note_sync_service import start_goods_note_sync

    summary = start_goods_note_sync(account.id, user_id=account.user_id, source=SOURCE_SCHEDULED)
    task_id = summary.get("task_id")
    if summary.get("reused"):
        return {
            "account_id": account.id,
            "task_id": task_id,
            "status": summary.get("status"),
            "note": f"该账号已有商品笔记同步任务在跑（任务 #{task_id}），本次不重入",
        }
    return _targeted_result(account.id, _await_targeted_task(task_id))


def _schedulable_goods_note_accounts(db: Any, accounts: list) -> list:
    """能**无人值守**同步商品笔记的账号：**有 ark 通道**的账号（设计 13 §44）。

    为什么判据是「通道」而不是 `sub_type`：商品笔记源（千帆 `goods-note/list`）只认
    **账号自己的 ark 令牌**（`qianfan_cookies_for_account` + `has_ark_token`，
    `_note_sources(only="ark")` 同一把尺子）。有 ark 令牌的账号既可能是 `creator`
    也可能是 `qianfan` 链路，`sub_type` 不表达「有没有千帆令牌」。

    这里只做**零请求**的 jar 形态预筛（L2 口径，与注释同步的纪律一致）：真能不能取到
    由翻页时的第一次请求回答（DEF-057：不预判）。⚠️ 与 `run_scheduled_note_syncs` 的
    `_schedulable_accounts`（要 `sub_type == "creator"`）是**两把不同的尺子**：
    那个筛的是「有创作者会话」，这个筛的是「有千帆令牌」。
    """
    from backend.app.services.account_service import CHANNEL_ARK, channel_jars

    schedulable = []
    for account in accounts:
        jars = channel_jars(db, account.id)
        if CHANNEL_ARK not in jars:
            continue
        schedulable.append(account)
    return schedulable


def run_scheduled_goods_note_syncs(
    user_id: int,
    account_ids: Optional[Sequence[int]] = None,
) -> Optional[dict[str, Any]]:
    """调度器入口：给账号跑一次**商品笔记**同步（限行主本人，DEF-066；设计 13 §44）。

    与 `run_scheduled_note_syncs` **逐字同构**，三处差异（其余纪律全部照搬）：

    | 维度 | 本函数 | 差异说明 |
    |---|---|---|
    | 任务类型 | `xhs_goods_note_sync` | 独立成行 + 独立 worker（⇒ 与普通笔记**可并行**） |
    | 预筛 | `_schedulable_goods_note_accounts`（**有 ark 通道**） | 那条筛的是创作者会话 |
    | 省流判据 | **不适用**（恒跑） | `account_needs_note_sync` 判的是 **note 游标**，那是 creator 列表的位置，与商品笔记无关 |

    定向模式（`account_ids` 非空）**不预筛** ⇒ 交给下游给结构化失败
    （缺 ark 令牌 ⇒ 任务行 `ARK_SESSION_REQUIRED`）并进 `result`：用户点了名，
    必须知道这个账号为什么没跑，而不是看到一条安静的「执行成功」。
    """
    from backend.app.models.platform_account import PlatformAccount
    from backend.app.services import sync_task_service as sts
    from backend.app.services.note_queue_service import SOURCE_SCHEDULED
    from backend.app.services.note_sync_service import SOURCE_ARK, run_note_sync

    targeted = bool(account_ids)
    gap = _sync_account_gap_seconds()
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
        accounts = db.scalars(statement).all()
        if targeted:
            candidates = list(accounts)
        else:
            candidates = _schedulable_goods_note_accounts(db, list(accounts))
            skipped = len(accounts) - len(candidates)
            if skipped:
                logger.info(
                    "scheduled goods-note sync: skipped %s account(s) without ark channel", skipped
                )
        for account in candidates:
            try:
                active = sts.find_active_task(
                    db, sts.GOODS_NOTE_SYNC_TASK_TYPE, account.id
                )
                if active is not None:
                    if targeted:
                        results.append(
                            {
                                "account_id": account.id,
                                "task_id": active.id,
                                "status": active.status,
                                "note": (
                                    f"该账号已有商品笔记同步任务在跑（任务 #{active.id}，"
                                    f"{active.status}），本次不重入"
                                ),
                            }
                        )
                    continue
                if targeted:
                    # 建行 + 账号任务 worker 串行执行 + 等终态（见 `_run_targeted_goods_note_sync`）。
                    results.append(_run_targeted_goods_note_sync(account))
                    continue
                run_note_sync(
                    account.id,
                    user_id=account.user_id,
                    source=SOURCE_SCHEDULED,
                    # §44：商品笔记链路没有可比游标 ⇒ auto 在那边落 full（幂等重扫 + 填空合并）。
                    mode=sts.SYNC_MODE_AUTO,
                    only=SOURCE_ARK,
                )
            except Exception as exc:  # noqa: BLE001 - 单个账号失败不拖垮整轮
                logger.warning(
                    "scheduled goods-note sync for account %s failed: %s", account.id, exc
                )
                if targeted:
                    results.append({"account_id": account.id, "status": "failed", "error": str(exc)})
            if gap:
                time.sleep(gap)
    except Exception as exc:  # noqa: BLE001 - 调度器调用，必须吞异常
        logger.error("run_scheduled_goods_note_syncs failed: %s", exc)
    finally:
        db.close()

    if not targeted:
        return None
    return _targeted_outcome(results, what="商品笔记同步")


def run_scheduled_shop_goods_syncs(
    user_id: int,
    account_ids: Optional[Sequence[int]] = None,
) -> Optional[dict[str, Any]]:
    """调度器入口：给每个**绑定了店铺的创作者账号**跑一次有界商品同步（限行主本人，DEF-066）。

    轮内纪律与笔记同步一致（不重叠 / 不重入 / 账号间停顿 / 省流），分流规则同
    `run_scheduled_note_syncs`（见其 docstring 的差异表，设计 13 §35.17.2）。
    ⚠️ `user_id` 必填（行归属者，runner 注入）：账号查询恒带
    `PlatformAccount.user_id == user_id`；`account_ids` 非空 = 只跑指定账号，
    空 = 本人的全部账号。

    **没绑店的账号**：批量模式**安静跳过**（没绑店跑商品同步是无意义的失败）；定向模式
    **不跳过**，改为收进 `results` 当失败 —— 用户点了名，必须知道「这个账号还没绑店铺」，
    而不是看到一条安静的「执行成功」。
    """
    from backend.app.models.platform_account import PlatformAccount
    from backend.app.services import sync_task_service as sts
    from backend.app.services.note_queue_service import SOURCE_SCHEDULED
    from backend.app.services.shop_binding_service import has_ark_session
    from backend.app.services.shop_goods_sync_service import run_shop_goods_sync

    targeted = bool(account_ids)
    gap = _sync_account_gap_seconds()
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
        accounts = db.scalars(statement).all()
        for account in accounts:
            try:
                if not has_ark_session(db, account.id):
                    if targeted:
                        results.append(
                            {
                                "account_id": account.id,
                                "status": "failed",
                                "error": "该账号没有可用的千帆登录态，无法同步商品（请先登录该账号的千帆）",
                            }
                        )
                    continue
                if sts.find_active_task(db, sts.SHOP_GOODS_SYNC_TASK_TYPE, account.id) is not None:
                    if targeted:
                        results.append(
                            {
                                "account_id": account.id,
                                "status": "running",
                                "note": "该账号已有商品同步任务在跑，本次不重入",
                            }
                        )
                    continue
                if targeted:
                    # 建行 + 账号任务 worker 串行执行 + 等终态（与笔记侧同构）。
                    results.append(_run_targeted_goods_sync(account))
                    continue
                run_shop_goods_sync(
                    account.id,
                    user_id=account.user_id,
                    source=SOURCE_SCHEDULED,
                    # §22：同笔记侧，auto 让「无同步历史 ⇒ 全量」也覆盖到定时任务。
                    mode=sts.SYNC_MODE_AUTO,
                )
            except Exception as exc:  # noqa: BLE001 - 单个账号失败不拖垮整轮
                logger.warning("scheduled goods sync for account %s failed: %s", account.id, exc)
                if targeted:
                    results.append({"account_id": account.id, "status": "failed", "error": str(exc)})
            if gap:
                time.sleep(gap)
    except Exception as exc:  # noqa: BLE001 - 调度器调用，必须吞异常
        logger.error("run_scheduled_shop_goods_syncs failed: %s", exc)
    finally:
        db.close()

    if not targeted:
        return None
    return _targeted_outcome(results, what="商品同步")


def run_scheduled_order_syncs(
    user_id: int,
    account_ids: Optional[Sequence[int]] = None,
) -> Optional[dict[str, Any]]:
    """调度器入口：给每个**绑定了店铺的创作者账号**跑一次订单同步（限行主本人，DEF-066）。

    与 `run_scheduled_shop_goods_syncs` 同构（不重叠 / 不重入 / 账号间停顿 / 省流），
    但同步的是**订单**（`order_sync_service.run_order_sync`，只读落库，M6 方向 6）。

    **没 ark 登录态的账号**：批量模式安静跳过（跑也是无意义的失败）；定向模式收进
    `results` 当失败 —— 用户点了名，必须知道「这个账号还没千帆登录态」。
    """
    from backend.app.models.platform_account import PlatformAccount
    from backend.app.services import sync_task_service as sts
    from backend.app.services.note_queue_service import SOURCE_SCHEDULED
    from backend.app.services.shop_binding_service import has_ark_session
    from backend.app.services.order_sync_service import run_order_sync, ORDER_SYNC_TASK_TYPE

    targeted = bool(account_ids)
    gap = _sync_account_gap_seconds()
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
        accounts = db.scalars(statement).all()
        for account in accounts:
            try:
                if not has_ark_session(db, account.id):
                    if targeted:
                        results.append(
                            {
                                "account_id": account.id,
                                "status": "failed",
                                "error": "该账号没有可用的千帆登录态，无法同步订单（请先登录该账号的千帆）",
                            }
                        )
                    continue
                if sts.find_active_task(db, ORDER_SYNC_TASK_TYPE, account.id) is not None:
                    if targeted:
                        results.append(
                            {
                                "account_id": account.id,
                                "status": "running",
                                "note": "该账号已有订单同步任务在跑，本次不重入",
                            }
                        )
                    continue
                run_order_sync(
                    account.id,
                    user_id=account.user_id,
                    source=SOURCE_SCHEDULED,
                )
            except Exception as exc:  # noqa: BLE001 - 单个账号失败不拖垮整轮
                logger.warning("scheduled order sync for account %s failed: %s", account.id, exc)
                if targeted:
                    results.append({"account_id": account.id, "status": "failed", "error": str(exc)})
            if gap:
                time.sleep(gap)
    except Exception as exc:  # noqa: BLE001 - 调度器调用，必须吞异常
        logger.error("run_scheduled_order_syncs failed: %s", exc)
    finally:
        db.close()

    if not targeted:
        return None
    return _targeted_outcome(results, what="订单同步")


def run_scheduled_note_data_syncs(
    user_id: int,
    account_ids: Optional[Sequence[int]] = None,
    days: int = 90,
) -> Optional[dict[str, Any]]:
    """调度器入口：给账号跑一次**离线笔记数据同步**（商业 + 普通两类，限行主本人，DEF-066）。

    「我的笔记」离线落库（2026-10-06 需求）：千帆数据中心 `note-data/goods`（商业）
    与 `note-data/normal`（普通）两类，各自走 `butterfly/data` 列表接口（无签名直连），
    分页拉全量落 `notes`（`note_category` 区分）+ `note_goods` 关系。

    `days`（2026-10-06 用户口径「同步最长 90 天内数据」）：同步的时间窗口天数，
    映射到平台预设 `date_select_type`（`1→recent_1 / 7→recent_7 / 30→recent_30 /
    90→recent_90 / 180→recent_180`），**上限 90**（超出夹回 90）。定时任务默认 90 天。

    预筛判据与 `_schedulable_goods_note_accounts` 同一把尺子：**有 ark 通道**的账号
    （千帆数据中心要 ark 登录态）。定向模式（`account_ids` 非空）不预筛，交给
    `sync_account_note_data` 给结构化 `error`（缺 ark 令牌 ⇒ `ARK_SESSION_REQUIRED`）。

    每个账号跑两类（goods + normal），结果合并进 `results`。
    """
    from backend.app.models.platform_account import PlatformAccount
    from backend.app.services.note_data_service import CATEGORIES, sync_account_note_data

    targeted = bool(account_ids)
    gap = _sync_account_gap_seconds()
    results: list[dict[str, Any]] = []
    # 天数 → 平台预设 date_select_type（上限 90，2026-10-06 用户口径「最长 90 天」）。
    _days = max(1, min(int(days), 90))
    if _days <= 1:
        date_select_type = "recent_1"
    elif _days <= 7:
        date_select_type = "recent_7"
    elif _days <= 30:
        date_select_type = "recent_30"
    else:
        date_select_type = "recent_90"
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
        if targeted:
            candidates = list(accounts)
        else:
            candidates = _schedulable_goods_note_accounts(db, list(accounts))
        for account in candidates:
            for category in CATEGORIES:
                try:
                    out = sync_account_note_data(
                        account_id=account.id,
                        category=category,
                        user_id=account.user_id,
                        date_select_type=date_select_type,
                    )
                    if targeted or out.get("error"):
                        results.append({"account_id": account.id, **out})
                except Exception as exc:  # noqa: BLE001 - 单个账号失败不拖垮整轮
                    logger.warning(
                        "scheduled note-data sync account %s category %s failed: %s",
                        account.id, category, exc,
                    )
                    if targeted:
                        results.append(
                            {"account_id": account.id, "category": category, "error": str(exc)}
                        )
            if gap:
                time.sleep(gap)
    except Exception as exc:  # noqa: BLE001 - 调度器调用，必须吞异常
        logger.error(f"run_scheduled_note_data_syncs failed: {exc}")
    finally:
        db.close()

    if not targeted:
        return None
    return _targeted_outcome(results, what="离线笔记数据同步")
