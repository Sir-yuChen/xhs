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
    SubTypeNotSupportedError,
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
from backend.app.services.job_registry import (
    JobSpec,
    get_job_spec,
    job_registry,
    workspace_for_job,
)
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
            select(PublishAsset).where(PublishAsset.row_type == ASSET_PUBLISH, PublishAsset.parent_id == job.id).order_by(PublishAsset.id.asc())
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
                    upload_payload = creator_adapter.upload_media(asset.local_path, asset.asset_type)
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
                select(PublishAsset).where(PublishAsset.row_type == ASSET_PUBLISH, PublishAsset.parent_id == job.id).order_by(PublishAsset.id.asc())
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
    #
    # ⚠️ **千帆（ark）链路是例外 —— 它不该抛错**（2026-10-11）：`qianfan` 是库里
    # 真实存在的取值（账号 9），而「它没有 www/creator 适配器」是个**可读答案**，
    # 不是故障。此前它抛 UnknownSubTypeError ⇒ 被外层吞成一行 warning，
    # 且**卡片上看不到任何原因**（用户只看到状态没变）。现在如实写进 status_message。
    try:
        normalized_sub_type = normalize_sub_type(account.sub_type)
    except SubTypeNotSupportedError as exc:
        account.status_message = str(exc)
        account.updated_at = now
        return account.status
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

    url = get_settings().database_url
    #: v1.1.0（2026-10-24，SQLite 退役）：不再按方言分支 —— 只可能是 PG。
    #:
    #: ⚠️ 历史坑（保留说明，因为它是「静默退出」的经典形态）：
    #: `SQLAlchemyJobStore` **自建引擎**，原来传过 `timeout` / `check_same_thread`
    #: 这类 SQLite 专用参数给 psycopg3 ⇒ `ProgrammingError: invalid connection
    #: option "timeout"` ⇒ 异常在 jobstore 内部抛出、被 uvicorn 吞在 lifespan 里
    #: ⇒ 表现为**无报错、无监听、进程静默退出**。改成 PG 参数即可，但真正
    #: 该做的是**根本不要有方言分支**（分支本身就是漏改的来源）。
    engine_options: dict[str, Any] = {
        "connect_args": {"connect_timeout": 10},
        "pool_pre_ping": True,
    }

    return {
        "default": SQLAlchemyJobStore(
            url=url,
            engine_options=engine_options,
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
        # 🔴 `system.note_sync` / `system.goods_note_sync` 的兜底**已删除**
        # （2026-10-27，设计 75 §4.5，去本地化实时查询 S4）：两个任务已从
        # `job_registry` 摘除，笔记改读平台实时数据，不再有「定时同步落本地」这一层。
        # 兜底项留在这里只会变成一条永不生效的死配置，还会在
        # 「初始化数据缺该键」路径上把任务种回列表。
        # 🔴 `system.shop_goods_sync` 的兜底**已删除**（2026-10-27，设计 75 §2.4）：
        # 任务已从 `job_registry` 摘除，兜底项在这里只会变成一条永不生效的死配置。
        # 原值（保留备查）：{"cron": _cron_every_n_hours(hours("xhs_shop_goods_sync_interval_hours", 6)),
        #                   "status": "active" if xhs_shop_goods_sync_enabled else "paused"}
        # 2026-10-06 需求：数据中心笔记指标同步（商业+普通，默认每 6 小时一次，恒 active）。
        # ⚠️ 2026-10-13 用户口径由「离线笔记数据同步」**改名**（落 `note_datacenter_stats`，
        # 供数据中心页展示 + 无流量清理判定）。同轮**移除**指标快照采集两任务
        # （`system.note_metrics_collect` / `system.shop_metrics_collect`）。
        # ⚠️ 兜底键**只有 cron/status**（名称走 `JobSpec.name`）—— 种子文件
        # `config/initial_data/scheduled_jobs.json` 才是显示名的真相源，改名时两处都要动。
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


def seed_builtin_jobs(db: Session) -> list[ScheduledJob]:
    """补种缺失的内置 job 行（幂等：同 `job_key` 不插、不覆盖）。

    ## 设计 69（2026-10-25）**取代** DEF-066「每用户各持一份」

    用户口径：「任务中心分为系统级别和工作区域级别，工作区域为每个区域中私有」
    「收为公共」「暂时不允许用户创建任务」。

    DEF-066 当时按用户各持一份，是为了避免「全局共享行被多用户互相操作」。
    但那条整改把两件事压进同一个 `user_id`：

    * **这个任务属于哪个级别** ⇒ 现在用 `workspace`（`system` / `xhs`）；
    * **执行时跑谁的数据** ⇒ 现在由 `JobSpec.user_scoped` 在运行时 fan-out。

    于是定义行**每种任务只有一行**（`job_key` 全表唯一），`user_id` 留空
    （= 内置行，没人改过）。

    ⚠️ 播种值来自**应用初始化数据**（设计 15 §4.5，随安装包分发），
    代码默认兜底。「只补缺失、不覆盖已有」是升级语义的关键：管理员在任务中心
    调过的调度属性不会因为装了个新版本被重置回初始化值。

    ⚠️ 种子里的 `account_ids` 统一重置为 `[]`（= 该任务执行时按 fan-out 自己
    遍历账号）：初始化数据是随包分发的**基线**，不应携带任何账号 id。
    """
    payloads = seed_payloads()
    if not payloads:
        return []
    #: 判重键 = `job_key`（全表唯一）。设计 69 后不再看 user_id。
    existing = set(db.scalars(select(ScheduledJob.job_key)).all())
    created: list[ScheduledJob] = []
    for spec, row in payloads:
        if spec.key in existing:
            continue
        params = dict(row.get("params") or {})
        if "account_ids" in params:
            #: 空列表 = 「该账号维度的任务自己遍历」—— fan-out 后不再靠行上的 id。
            params["account_ids"] = []
        job = ScheduledJob(
            user_id=None,
            name=row["name"],
            job_key=spec.key,
            workspace=workspace_for_job(spec.key),
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


def seed_builtin_jobs_for_user(db: Session, user_id: int) -> list[ScheduledJob]:
    """**已退役**（设计 69）：任务定义不再按用户播种。

    保留这个名字只为接住旧调用点（`POST /auth/register` 等处）——
    直接转调全局播种，返回空列表（注册一个新用户**不该**产生任何任务行）。
    """
    seed_builtin_jobs(db)
    return []


def ensure_builtin_jobs(db: Session) -> list[ScheduledJob]:
    """补种内置 job 行（设计 69 后与用户数无关：不再遍历用户）。

    名字保留（旧调用点 + 测试引用了它），语义改为「全局补种一次」。
    """
    return seed_builtin_jobs(db)


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


def run_scheduled_note_data_syncs(
    user_id: int,
    account_ids: Optional[Sequence[int]] = None,
    days: int = 90,
) -> Optional[dict[str, Any]]:
    """调度器入口：给账号跑一次**离线笔记数据同步**（商业 + 普通两类，限行主本人，DEF-066）。

    「我的笔记」离线落库（2026-10-06 需求）：千帆数据中心 `note-data/goods`（商业）
    与 `note-data/normal`（普通）两类，各自走 `butterfly/data` 列表接口（无签名直连），
    分页拉全量落 `notes`（基础信息）+ `note_datacenter_stats`（指标/分类，`note_flag` 区分）+ `note_goods` 关系。

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
            #: 继续拿去同步商品笔记。
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
