from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.app.api.routing import include_all_routers
from backend.app.core.errors import register_error_handlers
from backend.app.core.middleware import setup_middleware, setup_static_serving
from backend.app.core.route_guard import assert_every_route_guarded
from backend.app.core.config import get_settings
from backend.app.core.database import init_db
from backend.app.core.selfcheck import log_startup_selfcheck
#: ⚠️ 启动编排的**薄壳导入**（设计 61 A3b）：实现都在 `backend/app/startup.py`
#: （组合根 —— 天然依赖全部服务层，放 core 会违反依赖方向合同 C1）。
#: 这些名字**必须模块级导入**：`test_scheduler_sync` 的桩经
#: `monkeypatch.setattr(main_module, "名字", …)` 打在 main 模块属性上，
#: 函数体内导入会让桩打不上（AttributeError）而 AST 守卫照样绿。
from backend.app.startup import (
    acquire_lease_and_start_scheduler,
    recover_and_wake_workers,
    seed_all_builtin_data,
    shutdown_all,
    start_all_watchdogs,
)

logger = logging.getLogger(__name__)
def _data_dir_for_lease(settings) -> Path:
    """租约文件所在目录（单副本调度锁，`core/singleton.py`）。

    ⚠️ v1.1.0（2026-10-24）：原来返回 `database_sqlite_path` 的父目录（即 `data/`），
    理由是「租约与 DB 文件同进同出」。**SQLite 退役后没有 DB 文件了**，
    那个字段也已删除 ⇒ 改为直接用 `paths.data_dir()`（同一个 `data/`，
    由 `core/paths.py` 按文件位置推出绝对路径，不依赖 CWD）。
    """
    from backend.app.core import paths

    #: `app_root()` 是按**文件位置**推仓库根（不依赖 CWD，见 `core/paths.py`），
    #: 所以 systemd / Docker / 换目录起 uvicorn 都指向同一个 `data/`。
    return paths.app_root() / "data"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动编排**薄壳**（设计 61 A3b）：每行一个步骤，实现在 `backend/app/startup.py`。

    ⚠️ 保持裸名调用形态 —— `test_scheduler_sync::test_lifespan_steps_are_all_stubbed_by_this_module`
    用 AST 断言这里的每个裸名调用都被测试桩掉（防「测试写生产库」），
    实现搬走、调用留下的形态让守卫照常工作（见 startup.py 模块 docstring）。
    """
    # 安全自检（设计 53 的 C1，S0）：`secret_key` 仍是出厂默认值 ⇒ **拒绝启动**。
    # 放在最前面 —— 后面每件事都会写库 / 起线程，「先干活再报错」等于让一个
    # 可伪造 JWT 的进程先对外服务了几秒。（守卫：test_lifespan_calls_the_self_check_first）
    get_settings().assert_production_ready()
    # 路由权限守卫（设计 50 §2.4，S3）：每条写路由必须挂权限码，缺一条拒绝启动。
    # 放在 `init_db()` 之前 —— 映射与库无关，早点失败少写一次盘。
    assert_every_route_guarded(app)
    # 启动自检（DEF-028）：Node 签名运行时缺失时全系统的平台调用都会失败，一次性说清。
    app.state.selfcheck = log_startup_selfcheck()
    init_db()
    # 七组内置种子（随安装附有 + 只补缺失不覆盖；自开 Session 写库）。
    seed_all_builtin_data()
    # 多实例闸 + 调度器 + 守活（在 data/ 建租约文件）。
    acquire_lease_and_start_scheduler(app)
    # 五条常驻看门狗。
    start_all_watchdogs()
    # 重启恢复（先复位后唤醒 —— 顺序是判据）+ 六个 worker 立刻唤醒。
    recover_and_wake_workers()
    try:
        yield
    finally:
        shutdown_all(app)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.api_title, lifespan=lifespan)

    #: 横切中间件集中装配（设计 61 §三 A3）：CORS / 审计。
    setup_middleware(app)

    @app.get("/api/health", tags=["health"])
    def health() -> dict:
        return {"status": "ok", "service": "spider-xhs"}

    #: 路由统一注册表（设计 61 §三 A4）：40+ 条 include_router 收口到一处，
    #: 顺序逐条原样搬移 —— 路由匹配与 OpenAPI 集合不变。
    include_all_routers(app)

    #: 统一业务错误 handler（设计 61 §三 A2）：BizError → detail 对象包络；
    #: HTTPException / 422 照走 FastAPI 默认（前端三分支解析零改动）。
    register_error_handlers(app)

    #: 前端静态托管（SPA 回退 + 缓存头 + `Mount("/")`）。
    #: ⚠️ **必须最后调**：`Mount("/")` 若先于 `/api` 路由注册会把 API 全部吞成
    #: 404（2026-10-25 实测事故，机理与守卫见 core/middleware.py 与
    #: test_errors_and_routing 的注册顺序守卫）。
    setup_static_serving(app)

    return app


app = create_app()
