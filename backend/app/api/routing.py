"""路由统一注册表（设计 61 §三 A4，2026-10-24）。

## 为什么收口

原来 40+ 条 `include_router` 手工罗列在 `main.create_app` 里，与 27 行模块 import
混在一起 —— 「对外统一出入口」的清单散落两处。收口后：

* `main.create_app` 只剩一句 `include_all_routers(app)`；
* 本文件的 include **顺序即原 main.py 的顺序，逐条原样搬移** —— FastAPI 的路由
  匹配按注册顺序（先注册先匹配），OpenAPI path 集与优先级都不变（守卫见
  `test_api_routing_registry`）；
* 前缀约定：平台域 `/api/xhs/*`（由各 router 自带）、管理域 `/api/system/*`、
  网关域 `/api/llm/*`，其余顶层挂 `/api` —— 新增路由一律在这里登记一行。

⚠️ 本模块在 api 根域内 import 各 api 模块 —— 依赖方向守卫（C3「域内自由」）放行。
"""
from __future__ import annotations

from fastapi import FastAPI

from backend.app.api import (
    accounts,
    ai,
    auth,
    drafts,
    files,
    llm_gateway,
    llm_gateway_monitor,
    llm_prompts,
    login_sessions,
    model_configs,
    notes,
    notifications,
    publish,
    schedule,
    settings as settings_api,
    system,
    system_deps,
    system_dict,
    system_logs,
    system_menus,
    system_permissions,
    system_roles,
    system_users,
    tasks,
    topics,
)
from backend.app.api.platforms import registry
from backend.app.api.platforms.xhs import (
    analytics,
    crawl,
    creator,
    data_package_rules,
    forbidden_words,
    material_files,
    materials,
    package_precheck,
    pc,
    product_notes,
    qianfan,
)


def include_all_routers(app: FastAPI) -> None:
    """按既有顺序注册全部业务路由（前缀统一 `/api`）。"""
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
    app.include_router(schedule.router, prefix="/api")
    # 运行时配置中心（设计 10 §7）：设置页的列表 / 增删改查
    app.include_router(settings_api.router, prefix="/api")
    # 系统更新（设计 36，2026-10-13）：版本查询 + 检查更新 + 下载 + 静默安装
    app.include_router(system.router, prefix="/api")
    # v1.1.0 系统管理域（设计 49 / 55）：用户 / 角色 / 菜单 / 字典 / 日志
    app.include_router(system_users.router, prefix="/api")
    app.include_router(system_roles.router, prefix="/api")
    app.include_router(system_menus.router, prefix="/api")
    app.include_router(system_dict.router, prefix="/api")
    app.include_router(system_permissions.router, prefix="/api")
    app.include_router(system_logs.router, prefix="/api")
    # 初始化环境依赖（设计 24 §27，2026-10-23）：状态 / 下载 / 清空重下 / 进度
    app.include_router(system_deps.router, prefix="/api")
