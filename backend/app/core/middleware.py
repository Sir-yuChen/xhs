"""中间件与静态托管的集中装配（设计 61 §三 A3，2026-10-24）。

## 为什么收口

原来 4 件横切件散在 `main.create_app` 里：CORS 与 API 审计用 `add_middleware`，
SPA 回退与缓存头用两个内联 `@app.middleware("http")` —— 装配顺序（洋葱模型：
**后注册者在外层**）只能靠读代码行序推断。收口到本模块后：

1. `main.create_app` 只剩一句 `setup_middleware(app)`，装配点唯一；
2. 注册顺序在本文件**自上而下即洋葱从内到外**，一眼可核；
3. 静态托管的三件（SPA 回退 / 缓存头 / StaticFiles 挂载）语义上是一组，
   挪动时**逐行原样搬移**（顺序不变 ⇒ 行为不变，OpenAPI 与响应头不受影响）。

## 注册顺序（⚠️ 洋葱模型实测口径：**后注册者在外层、先执行**，2026-10-24 验证）

按下方代码自上而下的注册顺序，实际的请求穿行顺序（外 → 内）是：

1. `_static_cache_headers`（**最外层**）—— 必须在外：SPA 回退把 404 换成
   `FileResponse(index.html)` 的瞬间，缓存头若在其内层就看不到新响应，
   `index.html` 拿不到 `no-cache` ⇒ 「旧 hash」bug 复发路径（2026-10-10 实测教训）；
2. `_spa_fallback` —— 只有走到最后仍 404 的非 API 路径才回退 index.html；
3. `ApiAuditMiddleware` —— 审计看的是「业务处理完」的响应；
4. `CORSMiddleware`（最内层）—— 跨域响应头补在业务响应上。
   （调研注：成熟做法会把 CORS 放最外以免未处理异常丢跨域头；为「行为与
   重构前逐字节一致」本轮保持原序，后续要调顺序只改本文件。）
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

from backend.app.core.api_audit import ApiAuditMiddleware
from backend.app.core.config import get_settings


def setup_middleware(app: FastAPI) -> None:
    """装配横切中间件（在 `create_app` 里、路由注册之前调用）。"""
    settings = get_settings()

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


def setup_static_serving(app: FastAPI) -> None:
    """托管构建好的前端（SPA 回退 + 缓存头 + StaticFiles 挂载）。

    ⚠️⚠️ **必须在 `include_all_routers(app)` 之后调用**（2026-10-25 实测事故）：
    Starlette 按注册顺序匹配路由 —— `Mount("/")` 若先于 `/api` 路由注册，
    会**吞掉全部 API 请求**（StaticFiles 找不到文件回 404，SPA 回退中间件只救
    非 `/api` 路径）⇒ API 全线 404、前端却正常 —— 正是「看起来活着其实全断」的
    最坏形态。原 main.py 的注册顺序就是「路由在前、静态在后」，A3 收口时一度
    挪反，靠本注释 + `test_errors_and_routing` 的注册顺序守卫钉死。
    """
    settings = get_settings()
    if not settings.frontend_serve_static:
        return
    # `frontend_build_dir` 空 ⇒ 由 `core/paths.frontend_dist()` 兜底（设计 53 A2）：
    # 原来直接 `Path(settings.frontend_build_dir)` 是**相对 CWD** 的，
    # systemd / Docker / 换目录起 uvicorn 任一处 ⇒ 整个前端 404。
    frontend_dist = settings.frontend_build_dir_resolved
    if not frontend_dist.is_dir():
        return

    # Serve index.html for SPA client-side routing (non-API, non-file paths)
    @app.middleware("http")
    async def _spa_fallback(request, call_next):  # noqa: ANN001 - starlette 签名
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
    async def _static_cache_headers(request, call_next):  # noqa: ANN001 - starlette 签名
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif "." not in path.split("/")[-1]:
            #: SPA 入口（及客户端路由）—— 一律回源。
            response.headers["Cache-Control"] = "no-cache"
        return response
