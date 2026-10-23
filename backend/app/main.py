from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.app.api import accounts, ai, auth, auto_tasks, drafts, files, keyword_groups, llm_gateway, llm_gateway_monitor, llm_prompts, login_sessions, model_configs, notes, notifications, publish, schedule, settings as settings_api, system, system_deps, tasks, topics
from backend.app.api.platforms import registry
from backend.app.api.platforms.xhs import (
    analytics,
    crawl,
    creator,
    data_package_rules,
    forbidden_words,
    material_files,
    materials,
    monitoring,
    package_precheck,
    pc,
    product_notes,
    qianfan,
)
from backend.app.core.api_audit import ApiAuditMiddleware
from backend.app.core.config import get_settings
from backend.app.core.database import SessionLocal, init_db
from backend.app.core.selfcheck import log_startup_selfcheck
from backend.app.core.singleton import acquire_scheduler_lease, release_scheduler_lease
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
from backend.app.services.note_gen_llm_worker import (
    ensure_llm_generate_worker,
    start_llm_generate_worker_watchdog,
    stop_llm_generate_worker_watchdog,
)
from backend.app.services.product_note_gen_service import (
    ensure_generate_worker,
    start_generate_worker_watchdog,
    stop_generate_worker_watchdog,
)
from backend.app.services.shop_goods_sync_service import (
    ensure_goods_sync_worker,
)
from backend.app.services.scheduler_service import (
    seed_builtin_jobs_from_initial_data,
    shutdown_scheduler,
    start_scheduler,
    start_scheduler_watchdog,
    stop_scheduler_watchdog,
)
from backend.app.services.settings_service import seed_builtin_settings_from_initial_data
#: ⚠️ **必须模块级导入**（2026-10-14 修）：此前这一句写在 `lifespan` 函数体内，
#: 于是 `main` 模块**没有**这个属性 ⇒ `test_scheduler_sync` 里那套
#: `monkeypatch.setattr(main_module, "…")` 的桩清单**打不上**（AttributeError），
#: 而「必须给每一步打桩」的守卫（读 AST）却**照样绿** —— 桩清单的真相源是
#: 源码文本，能力却是运行时的属性。与上面两条种子同形（它们本来就是模块级）。
from backend.app.services.llm_gateway_seed_service import seed_gateway_providers_for_all_users
from backend.app.api.llm_prompts import seed_builtin_prompt_templates_for_all_users
from backend.app.services.data_package_rule_service import seed_builtin_rules_for_all_users
#: 违规词内置种子（2026-10-21）。同样**模块级导入** —— `lifespan` 里那套打桩
#: 与「每一步都打桩」的守卫都读模块属性，函数体内导入会让桩打不上（见上注）。
from backend.app.services.forbidden_word_service import seed_builtin_words_for_all_users
from backend.app.services.menu_seed_service import seed_system_domain

logger = logging.getLogger(__name__)


def _data_dir_for_lease(settings) -> Path:
    """运行库所在目录 —— 租约与 DB 同进同出（部署时一起搬家）。"""
    return Path(settings.database_sqlite_path).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 安全自检（设计 53 的 C1，S0）：服务器形态（非 SQLite）下 `secret_key`
    # 仍是出厂默认值 ⇒ **拒绝启动**。放在最前面 —— 后面每件事都会写库 / 起线程，
    # 「先干活再报错」等于让一个可伪造 JWT 的进程先对外服务了几秒。
    get_settings().assert_production_ready()
    # 启动自检（DEF-028）：Node 签名运行时缺失时**全系统的平台调用都会失败**，
    # 而失败形态是每个请求各报一次看不懂的错。这里一次性说清（只 WARN 不阻断 ——
    # 看本地已同步数据不需要签名）。
    app.state.selfcheck = log_startup_selfcheck()
    init_db()
    # 应用初始化数据（设计 15 §4.5）：定时任务基线**随安装自动附有**。
    # ⚠️ 位置在调度器之前且**不受 `scheduler_enabled` 影响** —— 桌面版默认不启调度器，
    #    若把播种挂在调度器装配里，任务中心会是空的（「装完即有用」就落空了）。
    seed_builtin_jobs_from_initial_data()
    # 运行时配置基线（设计 10 §7.3，2026-09-27）：与定时任务同一套「随安装附有 + 只补
    # 缺失不覆盖」语义。**必须早于任何运行期读点**（限流器 / worker 池都要读它）——
    # 虽然读点自身有「DB 无行 ⇒ 回落 settings」的兜底，但先播种才能让设置页一打开就有行。
    seed_builtin_settings_from_initial_data()
    # 内置提示词模板 / 模块（设计 17 §13.5.4，v2.24）：用户口径「生成AI商品图 没有公共
    # 提示模板相关逻辑」⇒ 没有种子时出图弹窗的下拉是**空的**，选了也无内容，
    # 「公共模板保证一致性」就落不了地。与上面两项同一语义：随安装附有 + 只补缺失不覆盖。
    seed_builtin_prompt_templates_for_all_users()
    # 设计 40：为存量用户补种数据包列规则的示例包（只补「一个都没有」的用户）。
    seed_builtin_rules_for_all_users()
    # LLM 网关服务商种子**补存量**（2026-10-13 用户报障「模型服务商依旧未配置」）：
    # 种子原只在 `register()` 播 ⇒ 存量老账号永远拿不到这 6 个国内常见服务商。
    # 与上面同语义：随安装附有 + **只补缺失不覆盖**（同名服务商 / 已有业务点映射一律不碰）。
    seed_gateway_providers_for_all_users()
    # 2026-10-21：违规词内置种子**补存量**（用户口径「把当前违规词作为新的种子数据」）。
    # 与上面同语义：只往**空分组**里追加，用户改过 / 删过的不重建、不覆盖。
    seed_builtin_words_for_all_users()
    # 系统管理域（v1.1.0 / 设计 47 §6，S1）：角色 / 菜单 / 字典 / 出厂 admin / 授权。
    # 与上面几项同一语义 —— **只补缺不覆盖**：用户在「菜单管理」「角色管理」里改过的
    # 行不会被下次启动打回。放在这里是**判据**：`create_all` 建库（测试库 / PG 基线）
    # 不经过迁移链，只有启动期会播种；而「刚 upgrade 完的库」由迁移 3 播一次。
    try:
        _db = SessionLocal()
        try:
            seed_system_domain(_db)
            _db.commit()
        finally:
            _db.close()
    except Exception as exc:  # noqa: BLE001 - 播种失败不该让整个应用起不来
        logger.warning("lifespan: seed system domain failed: %s", exc)
    settings = get_settings()
    # 多实例闸（T-108 / DEF-003）：`BackgroundScheduler` 与 FastAPI 同进程，
    # `max_instances=1` 只约束单进程内 ⇒ 误开第二个后端会让**同一批定时任务跑两遍**
    # （重复发文三个入口之一）。抢不到租约的进程**不启动调度器**（其余 API / worker
    # 照常，那些链路各有自己的 DB 级幂等），并把原因**如实记进日志** ——
    # 静默跳过正是 DEF-048 那类「看着在跑其实没跑」。
    scheduler_lease_acquired, scheduler_lease_reason = True, "未启用多实例闸（scheduler 关闭）"
    if settings.scheduler_enabled:
        scheduler_lease_acquired, scheduler_lease_reason = acquire_scheduler_lease(
            _data_dir_for_lease(settings)
        )
        if not scheduler_lease_acquired:
            logger.warning("调度器未启动：%s", scheduler_lease_reason)
    app.state.scheduler_lease_acquired = scheduler_lease_acquired
    app.state.scheduler_lease_reason = scheduler_lease_reason
    scheduler = (
        start_scheduler()
        if settings.scheduler_enabled and scheduler_lease_acquired
        else None
    )
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
    # 守活（DEF-069 同型）：LLM 生成队列 worker（设计 17 §7.4 / T6）—— 一次生成
    # 要走「渲染 Prompt → 上游调用（60s 超时）→ 解析 → 护栏 → 落库」，是最长的一条；
    # 线程被带走则队列里的项无人认领，`generation_records` 永远卡在 `queued`。
    start_llm_generate_worker_watchdog()
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
    ensure_llm_generate_worker()
    ensure_goods_sync_worker()
    ensure_account_task_worker()
    try:
        yield
    finally:
        stop_publish_worker_watchdog()
        stop_sync_worker_watchdog()
        stop_generate_worker_watchdog()
        stop_llm_generate_worker_watchdog()
        stop_account_task_watchdog()
        stop_scheduler_watchdog()
        # 取 `app.state` 上的**当前**实例：看门狗可能已经换过一台。
        shutdown_scheduler(getattr(app.state, "scheduler", None))
        # 释放多实例租约（T-108）—— 只在**确实持有**时删（`atexit` 是兜底）。
        release_scheduler_lease()


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
    # API 审计（DEF-019 / T-18）：`api_logs` 自建表起零写入点。
    # 只记「审计前缀下的写 + 任何失败」，不落 query/body（敏感值不进表）。
    app.add_middleware(ApiAuditMiddleware)

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
    # LLM 网关配置（设计 17 §7.1，T1）：上游连接配置 CRUD + 健康探测（v1.7 纯 OpenAI 兼容，无外挂进程）
    app.include_router(llm_gateway.router, prefix="/api")
    # v1.9：使用平台枚举下发（§7.1：`GET /api/llm/providers/platforms`，独立前缀）
    app.include_router(llm_gateway.providers_router, prefix="/api")
    # v2.0（设计 17 §13.2.6）：「平台 × 业务点 → 服务商」映射端点
    app.include_router(llm_gateway.business_router, prefix="/api")
    # LLM 网关令牌映射 + 预警规则 + 用量展示（设计 17 §7.1a/§7.1b/§7.5，v1.6）
    app.include_router(llm_gateway_monitor.router, prefix="/api")
    # 提示词模块 / 模板（设计 17 §7.2，T2）：两表 CRUD + 渲染预览
    app.include_router(llm_prompts.router, prefix="/api")
    app.include_router(topics.router, prefix="/api")
    app.include_router(notifications.router, prefix="/api")
    app.include_router(keyword_groups.router, prefix="/api")
    app.include_router(publish.router, prefix="/api")
    app.include_router(analytics.router, prefix="/api")
    app.include_router(pc.router, prefix="/api")
    app.include_router(creator.router, prefix="/api")
    app.include_router(qianfan.router, prefix="/api")
    app.include_router(product_notes.router, prefix="/api")
    app.include_router(materials.router, prefix="/api")
    # v2.50：违规词库（本地自维护词表，供数据包图片 OCR 精确过滤；设计 25 §2）
    app.include_router(forbidden_words.router, prefix="/api")
    # 设计 40（2026-10-14）：数据包列规则（提取/运算规则可配置；父子表 CRUD + 执行）
    app.include_router(data_package_rules.router, prefix="/api")
    # v2.50：数据包预检（上传 → 解析 → OCR 红框；设计 25 §4）
    app.include_router(package_precheck.router, prefix="/api")
    # v2.31：素材**文件**的增删（会动用户磁盘 ⇒ 另一套语义，单独成模块）
    app.include_router(material_files.router, prefix="/api")
    app.include_router(crawl.router, prefix="/api")
    app.include_router(monitoring.router, prefix="/api")
    app.include_router(auto_tasks.router, prefix="/api")
    app.include_router(schedule.router, prefix="/api")
    # 运行时配置中心（设计 10 §7）：设置页的列表 / 增删改查
    app.include_router(settings_api.router, prefix="/api")
    # 系统更新（设计 36，2026-10-13）：版本查询 + 检查更新 + 下载 + 静默安装
    app.include_router(system.router, prefix="/api")
    # 初始化环境依赖（设计 24 §27，2026-10-23）：状态 / 下载 / 清空重下 / 进度
    app.include_router(system_deps.router, prefix="/api")

    # Serve pre-built frontend in production / Docker
    if settings.frontend_serve_static:
        # `frontend_build_dir` 空 ⇒ 由 `core/paths.frontend_dist()` 兜底（设计 53 A2）：
        # 原来直接 `Path(settings.frontend_build_dir)` 是**相对 CWD** 的，
        # systemd / Docker / 换目录起 uvicorn 任一处 ⇒ 整个前端 404。
        frontend_dist = settings.frontend_build_dir_resolved
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

            #: ⚠️ 缓存头（2026-10-10 实测补）：Starlette 的 StaticFiles **不发 Cache-Control**
            #: ⇒ 浏览器走**启发式缓存**，`index.html` 被缓存后不再回源校验，于是长期指向
            #: **旧的** `index-<hash>.js`（用户多轮报「依旧」的真根因 —— 服务端已是新包，
            #: 但页面加载的是旧 hash）。
            #: 口径：`index.html` **必须回源校验**（`no-cache`），带 hash 的 `assets/*`
            #: **可长缓存**（内容变了 hash 必变，天然免冲突）。
            @app.middleware("http")
            async def _static_cache_headers(request, call_next):
                response = await call_next(request)
                path = request.url.path
                if path.startswith("/assets/"):
                    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
                elif "." not in path.split("/")[-1]:
                    #: SPA 入口（及客户端路由）—— 一律回源。
                    response.headers["Cache-Control"] = "no-cache"
                return response

    return app


app = create_app()
