"""route → 权限码登记表 —— **启动守卫的唯一输入**。

## 为什么不是「从装饰器里扫 require_perm」

把码写在 `Depends(require_perm("..."))` 里是最容易被抄错的形态（设计 49 §模块九
22 页 × 平均 5 个动作 ≈ 250 处），而抄错一个字母的表现是**那个按钮对所有人 403**，
只有在真机上点到那个按钮才会发现。⇒ 单一落点放在这张表里：

* 写路由（POST/PUT/PATCH/DELETE）**必须有**映射，缺一条 ⇒ 启动崩；
* 只读路由（GET/HEAD）**可以**没有（不是每个 GET 都能对应到一条 view 码），
  但 **4 组只读页**（数据中心 / 洞察 / 用量 / 总览）必须显式声明，见
  `READONLY_PAGE_GUARDS` —— 它们的码在 `perms_registry.PAGE_VIEW_PERMS` / `SYSTEM_PERMS` 里。

## 路径口径（重要，别混）

键用 **OpenAPI 的 path 模板**（含 `{id}` 这种单花括号写法，`/api` 前缀已剥），
与 `app.openapi()["paths"]` 的 key **逐字相同** ⇒ 守卫可以直接比对，
不需要再做正则归一（归一必然漂移：`{id}` vs `{menu_id}` 都是同一条路由的两个写法）。

`METHOD route_path` 的 `METHOD` 大写。
"""

from __future__ import annotations

from typing import Final

#: 写方法集合（启动守卫扫这些方法）
WRITE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: 白名单：写路由**允许**不挂权限码的，每条必须给理由，总数 ≤ 8（设计 50 §2.4）。
#: 键同样是 OpenAPI 的 path 模板。
EXEMPT: Final[dict[str, str]] = {
    "/api/auth/login": "登录本身不能要权限",
    "/api/auth/refresh": "刷新令牌（用 refresh token 换 access token）",
    "/api/auth/logout": "登出（任何登录态都该能登出）",
    "/api/auth/register": "注册（受 register.enabled 开关控制）",
    "/api/auth/send-register-code": "同注册：发验证码时还没有账号",
    "/api/auth/send-profile-code": "改资料前发验证码：已登录但邮箱可能为空，校验的是 ticket 不是权限",
    "/api/auth/verify-profile-code": "同上：换 ticket 的入口本身不能再要权限",
    "/api/health": "存活探针（K8s / Docker healthcheck 无凭据）",
}

#: 路由 → 权限码。**S3 批量挂码时逐条往这里加**，`assert_every_route_guarded`
#: 拿它与 `app.openapi()["paths"]` 对账：缺一条写路由 ⇒ RuntimeError 拒绝启动。
#: S0 落骨架时为空；S3 完成后应为 251 条（设计 50 §5.2 的实测口径）。
ROUTE_PERMS: Final[dict[str, str]] = {}

#: 只读页 → 必须挂的 view 码（设计 50 §5.3，用户 2026-10-23 口径「只读≠免授权」）。
#: 键是 path 前缀，守卫检查该前缀下**所有** method 是否都挂了这条码。
#:
#: ⚠️ **前缀按实测的端点族填**（`create_app().openapi()`，2026-10-23）：
#: 设计 50 §5.3 写的是 `api/analytics.py` / `api/insights.py` / `api/llm.py` /
#: `api/dashboard.py` 四个文件，但仓库里**前两个文件不存在** —— 数据中心页实际调的
#: 是 `xhs/analytics/*`（总览/互动/话题/评论/离线笔记）与 `accounts/*`，
#: 内容洞察调的是 `xhs/analytics/{overview,top-content,hot-topics,comment-insights}`，
#: 商品洞察调的是 `xhs/analytics/{overview,keyword-trends,reports,engagement}`。
#: ⇒ 按**端点族**登记（`xhs/analytics` 同时服务三个页，统一挂 `xhs:insights:view`；
#: 数据中心专有的离线笔记 / 无流量清理走 `xhs:datacenter:view` 的那几条在 S3 逐条补）。
READONLY_PAGE_GUARDS: Final[dict[str, str]] = {
    "/api/xhs/analytics": "xhs:insights:view",
    "/api/llm/usage": "ai:usage:view",
}
