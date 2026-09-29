from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.app.api import accounts, ai, auth, auto_tasks, drafts, files, keyword_groups, login_sessions, model_configs, notes, notifications, publish, schedule, settings as settings_api, tasks, topics
from backend.app.api.platforms import registry
from backend.app.api.platforms.xhs import (
    analytics,
    crawl,
    creator,
    monitoring,
    pc,
    product_notes,
    qianfan,
)
from backend.app.core.config import get_settings
from backend.app.core.database import SessionLocal, init_db
from backend.app.services import note_queue_service
from backend.app.services.account_task_service import (
    ensure_account_task_worker,
    recover_account_tasks,
    start_account_task_watchdog,
    stop_account_task_watchdog,
)
from backend.app.services.note_publish_service import (
    ensure_publish_worker,
    start_publish_worker_watchdog,
    stop_publish_worker_watchdog,
)
from backend.app.services.note_sync_service import (
    ensure_sync_worker,
    start_sync_worker_watchdog,
    stop_sync_worker_watchdog,
)
from backend.app.services.product_note_gen_service import (
    ensure_generate_worker,
    start_generate_worker_watchdog,
    stop_generate_worker_watchdog,
)
from backend.app.services.scheduler_service import (
    seed_builtin_jobs_from_initial_data,
    shutdown_scheduler,
    start_scheduler,
    start_scheduler_watchdog,
    stop_scheduler_watchdog,
)
from backend.app.services.settings_service import seed_builtin_settings_from_initial_data


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 应用初始化数据（设计 15 §4.5）：定时任务基线**随安装自动附有**。
    # ⚠️ 位置在调度器之前且**不受 `scheduler_enabled` 影响** —— 桌面版默认不启调度器，
    #    若把播种挂在调度器装配里，任务中心会是空的（「装完即有用」就落空了）。
    seed_builtin_jobs_from_initial_data()
    # 运行时配置基线（设计 10 §7.3，2026-09-27）：与定时任务同一套「随安装附有 + 只补
    # 缺失不覆盖」语义。**必须早于任何运行期读点**（限流器 / worker 池都要读它）——
    # 虽然读点自身有「DB 无行 ⇒ 回落 settings」的兜底，但先播种才能让设置页一打开就有行。
    seed_builtin_settings_from_initial_data()
    settings = get_settings()
    scheduler = start_scheduler() if settings.scheduler_enabled else None
    app.state.scheduler = scheduler
    if scheduler is not None:
        # 守活（DEF-067）：APScheduler 主循环一遇异常就整线程死掉且不自愈
        # （`running` 还骗人地报 True）⇒ 靠看门狗重建，否则作业静默停摆。
        start_scheduler_watchdog(app)
    # 守活（DEF-069）：批量同步 worker 线程一遇 BaseException 就整线程死掉且
    # 不自愈（真机实测：19 篇跑到 8 篇后线程无声消失，剩余 11 篇「待同步」永不
    # 执行）⇒ 看门狗巡检重建 + 卡住篇重入队。与调度器开关无关（用户动作触发）。
    start_sync_worker_watchdog()
    # 守活（DEF-069 同型缺口）：发布 worker 改造前**只有 `except Exception`、
    # 无看门狗、无停止出口** ⇒ 线程被 `SystemExit` 之类带走就永久停摆：队列里的篇
    # 没人跑、笔记永远卡在 publishing。同款看门狗补上。
    start_publish_worker_watchdog()
    # 守活（DEF-069 同型缺口）：商品生成 worker（§31.10，单 / 批量生成都走队列）——
    # 一篇要跑三步 + 轮询 ~6–21s，线程死了同样无声停摆。同款看门狗补上。
    start_generate_worker_watchdog()
    # 守活（DEF-069 同型）：账号级任务 worker（设计 13 §35.16）—— 笔记同步 / 商品同步
    # 此前是**每账号一条独立线程**，线程被带走 / 进程退出后 `running` 行永久卡住。
    start_account_task_watchdog()
    # 重启恢复（设计 13 §35.9.2，用户口径「应用退出再次启动后可继续执行」）：
    # ① 上次进程留下的 `running` 队列项 ⇒ 复位成 `pending`（**只自动重试「中断」**；
    #    业务失败不自动重试，等用户在任务记录里手动点「重试」）；
    # ② 卡在 `publishing` 的笔记行 ⇒ 复位成 `pending`（进程在「先置 publishing 落库」
    #    与「写 published」之间退出会让它永远停在那儿：既不会被重跑，也不进任何
    #    终态统计 —— 现有缺口）。
    # ⚠️ 只在**启动时**做一次：正常运行时 `running` / `publishing` 是「正在跑」的
    # 合法状态，在跑的过程中复位就是把任务从 worker 脚下抽走。
    recovery_db = SessionLocal()
    try:
        note_queue_service.recover_interrupted(recovery_db)
        note_queue_service.recover_publishing_notes(recovery_db)
    finally:
        recovery_db.close()
    # §35.16：账号级任务（笔记同步 / 商品同步 / 清理）的 `running` 行**不在**
    # `QUEUE_ITEM_TASK_TYPES` 里 ⇒ 上面那次复位覆盖不到，此前永远卡在「同步中」——
    # 卡片与记录页显示「同步中」却再也不会结束（真缺陷，DEF-048 同型）。单独复位。
    recover_account_tasks()
    # 复位完**立刻唤醒**四个 worker：积压的 `pending` 项不该等看门狗 30 秒巡检才被
    # 发现（唤醒带兜底超时的长等待，无活可干时它们自动回去睡）。
    ensure_sync_worker()
    ensure_publish_worker()
    ensure_generate_worker()
    ensure_account_task_worker()
    try:
        yield
    finally:
        stop_publish_worker_watchdog()
        stop_sync_worker_watchdog()
        stop_generate_worker_watchdog()
        stop_account_task_watchdog()
        stop_scheduler_watchdog()
        # 取 `app.state` 上的**当前**实例：看门狗可能已经换过一台。
        shutdown_scheduler(getattr(app.state, "scheduler", None))


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.api_title, lifespan=lifespan)

    origins = [origin.strip() for origin in settings.backend_cors_origins.split(",") if origin.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health", tags=["health"])
    def health() -> dict:
        return {"status": "ok", "service": "spider-xhs"}

    app.include_router(registry.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(accounts.router, prefix="/api")
    app.include_router(login_sessions.router, prefix="/api")
    app.include_router(notes.router, prefix="/api")
    app.include_router(files.router, prefix="/api")
    app.include_router(drafts.router, prefix="/api")
    app.include_router(ai.router, prefix="/api")
    app.include_router(tasks.router, prefix="/api")
    app.include_router(model_configs.router, prefix="/api")
    app.include_router(topics.router, prefix="/api")
    app.include_router(notifications.router, prefix="/api")
    app.include_router(keyword_groups.router, prefix="/api")
    app.include_router(publish.router, prefix="/api")
    app.include_router(analytics.router, prefix="/api")
    app.include_router(pc.router, prefix="/api")
    app.include_router(creator.router, prefix="/api")
    app.include_router(qianfan.router, prefix="/api")
    app.include_router(product_notes.router, prefix="/api")
    app.include_router(crawl.router, prefix="/api")
    app.include_router(monitoring.router, prefix="/api")
    app.include_router(auto_tasks.router, prefix="/api")
    app.include_router(schedule.router, prefix="/api")
    # 运行时配置中心（设计 10 §7）：设置页的列表 / 增删改查
    app.include_router(settings_api.router, prefix="/api")

    # Serve pre-built frontend in production / Docker
    if settings.frontend_serve_static:
        frontend_dist = Path(settings.frontend_build_dir)
        if frontend_dist.is_dir():
            from starlette.responses import FileResponse

            # Serve index.html for SPA client-side routing (non-API, non-file paths)
            @app.middleware("http")
            async def _spa_fallback(request, call_next):
                response = await call_next(request)
                path = request.url.path
                if (
                    response.status_code == 404
                    and not path.startswith("/api")
                    and "." not in path.split("/")[-1]
                ):
                    return FileResponse(str(frontend_dist / "index.html"))
                return response

            app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")

    return app


app = create_app()
