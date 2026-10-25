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

#: 路由 → 权限码。`assert_every_route_guarded` 拿它与 `app.openapi()["paths"]`
#: 对账：缺一条写路由 ⇒ RuntimeError 拒绝启动（设计 50 §2.4）。
#:
#: 2026-10-24 实测：274 条写路由，扣掉 4 条 EXEMPT 后 **270 条全部登记**。
#: （S0 落骨架时这里是空字典 —— 那时守卫**从未被 main.py 调用**，等于没有守卫。
#:  本轮补全映射 + 接进 lifespan，两道一起才真的生效。）
#:
#: ## 值里有两个特殊标记
#:
#: | 值 | 含义 |
#: |---|---|
#: | `EXEMPT_SELF` | **本人操作**，不挂码（改自己的密码 / 改自己的配置）。端点内已按 `user_id` 收口 |
#: | `EXEMPT_ADMIN` | 影响全站但**没有对应权限码**（`settings/apply-to-all`），端点内判 `role == admin` |
#:
#: ⚠️ 为什么 `POST /api/settings` 等是 `EXEMPT_SELF` 而不是挂 `system:setting:edit`：
#: 配置是**每个用户自己的一份**（`system_settings.user_id NOT NULL`）。给它挂权限码
#: 等于「只有某种角色才能改自己的设置」—— 那会让普通用户连自己的偏好都改不了。
#:
#: ⚠️ 为什么千帆大量的**只读查询**归 `view` 码：平台契约里那些查询就是 POST
#: （body 带筛选条件），挂写码会逼用户「有写权限才能搜索」。
ROUTE_PERMS: Final[dict[str, str]] = {
    "DELETE /api/accounts/{account_id}": "xhs:account:delete",
    "DELETE /api/ai/images/assets/{asset_id}": "ai:generate:generate",
    "DELETE /api/auto-tasks/{task_id}": "xhs:autoops:delete",
    "DELETE /api/drafts/{draft_id}": "xhs:note:delete",
    "DELETE /api/drafts/{draft_id}/assets/{asset_id}": "xhs:note:edit",
    "DELETE /api/files/images/{file_name}": "xhs:picture:delete",
    "DELETE /api/keyword-groups/{group_id}": "xhs:keyword:delete",
    "DELETE /api/llm/gateway-alerts/{alert_id}": "ai:alert:view",
    "DELETE /api/llm/gateway-configs/{config_id}/models/{model_id}": "ai:model:delete",
    "DELETE /api/notes/{note_id}": "xhs:library:delete",
    "DELETE /api/notes/{note_id}/assets/{asset_id}": "xhs:note:edit",
    "DELETE /api/publish/assets/{asset_id}": "xhs:note:edit",
    "DELETE /api/publish/jobs/{job_id}": "xhs:publish:cancel",
    "DELETE /api/schedule/jobs/{job_id}": "xhs:job:delete",
    "DELETE /api/schedule/logs": "xhs:record:clean",
    "DELETE /api/schedule/logs/clean": "xhs:record:clean",
    "DELETE /api/settings/{key}": "EXEMPT_SELF",
    "DELETE /api/system/dict/items/{item_id}": "system:dict:delete",
    "DELETE /api/system/dict/types/{type_id}": "system:dict:delete",
    "DELETE /api/system/menus/{menu_id}": "system:menu:delete",
    "DELETE /api/system/roles/{role_id}": "system:role:delete",
    "DELETE /api/system/users/{user_id}": "system:user:delete",
    "DELETE /api/topics/{topic_id}": "xhs:topic:delete",
    "DELETE /api/xhs/data-package-rules/rules/{rule_id}": "xhs:rule:create",
    "DELETE /api/xhs/data-package-rules/sets/{set_id}": "xhs:rule:create",
    "DELETE /api/xhs/forbidden-words/{word_id}": "xhs:forbiddenWord:create",
    "DELETE /api/xhs/llm/prompt-modules/{module_id}": "xhs:item:edit",
    "DELETE /api/xhs/llm/prompt-templates/{template_id}": "xhs:item:edit",
    "DELETE /api/xhs/materials/files/{file_id}": "xhs:material:createFolder",
    "DELETE /api/xhs/materials/folders/{folder_id}": "xhs:material:createFolder",
    "DELETE /api/xhs/monitoring/targets/{target_id}": "xhs:monitor:create",
    "DELETE /api/xhs/package-precheck/{token}": "xhs:publish:precheck",
    "PATCH /api/auto-tasks/{task_id}": "xhs:autoops:edit",
    "PATCH /api/drafts/{draft_id}": "xhs:note:edit",
    "PATCH /api/drafts/{draft_id}/assets/{asset_id}": "xhs:note:edit",
    "PATCH /api/keyword-groups/{group_id}": "xhs:keyword:edit",
    "PATCH /api/llm/gateway-alerts/{alert_id}": "ai:alert:view",
    "PATCH /api/llm/gateway-configs/{config_id}": "ai:gateway:edit",
    "PATCH /api/llm/gateway-configs/{config_id}/models/{model_id}": "ai:model:edit",
    "PATCH /api/notes/{note_id}/publish-status": "xhs:note:edit",
    "PATCH /api/publish/jobs/{job_id}": "xhs:publish:submit",
    "PATCH /api/schedule/jobs/{job_id}/status": "xhs:job:edit",
    "PATCH /api/topics/{topic_id}": "xhs:topic:edit",
    "PATCH /api/xhs/data-package-rules/rules/{rule_id}": "xhs:rule:create",
    "PATCH /api/xhs/data-package-rules/sets/{set_id}": "xhs:rule:create",
    "PATCH /api/xhs/forbidden-words/{word_id}": "xhs:forbiddenWord:create",
    "PATCH /api/xhs/llm/prompt-modules/{module_id}": "xhs:item:edit",
    "PATCH /api/xhs/llm/prompt-templates/{template_id}": "xhs:item:edit",
    "PATCH /api/xhs/materials/folders/{folder_id}": "xhs:material:createFolder",
    "PATCH /api/xhs/monitoring/targets/{target_id}": "xhs:monitor:create",
    "PATCH /api/xhs/product-notes/versions/{record_id}": "xhs:workshop:adopt",
    "PATCH /api/xhs/qianfan/items/edit": "xhs:item:edit",
    "POST /api/accounts/import-cookie": "xhs:account:importCookie",
    "POST /api/accounts/login-states": "xhs:account:create",
    "POST /api/accounts/{account_id}/check": "xhs:account:probe",
    "POST /api/accounts/{account_id}/goods-notes/sync": "xhs:account:syncGoodsNotes",
    "POST /api/accounts/{account_id}/login-state": "xhs:account:relogin",
    "POST /api/accounts/{account_id}/metrics": "xhs:account:probe",
    "POST /api/accounts/{account_id}/notes/sync": "xhs:account:syncNotes",
    "POST /api/accounts/{account_id}/qianfan-cookie": "xhs:account:edit",
    "POST /api/ai/generate-note": "ai:generate:generate",
    "POST /api/ai/generate-tags": "ai:generate:generate",
    "POST /api/ai/generate-title": "ai:generate:generate",
    "POST /api/ai/images/describe": "ai:generate:generate",
    "POST /api/ai/images/generate": "ai:generate:generate",
    "POST /api/ai/images/generate-cover": "ai:generate:generate",
    "POST /api/ai/polish-text": "ai:generate:generate",
    "POST /api/ai/rewrite-note": "ai:generate:generate",
    "POST /api/auth/change-password": "EXEMPT_SELF",
    "POST /api/auth/revoke-all-sessions": "EXEMPT_SELF",
    "POST /api/auto-tasks": "xhs:autoops:create",
    "POST /api/auto-tasks/{task_id}/run": "xhs:autoops:edit",
    "POST /api/drafts": "xhs:note:create",
    "POST /api/drafts/{draft_id}/assets": "xhs:note:edit",
    "POST /api/drafts/{draft_id}/send-to-publish": "xhs:publish:submit",
    "POST /api/files/images/compose": "xhs:picture:resize",
    "POST /api/files/images/resize": "xhs:picture:resize",
    "POST /api/files/upload": "xhs:picture:upload",
    "POST /api/keyword-groups": "xhs:keyword:create",
    "POST /api/llm/gateway-alerts": "ai:alert:view",
    "POST /api/llm/gateway-configs": "ai:gateway:create",
    "POST /api/llm/gateway-configs/models-preview": "ai:gateway:edit",
    "POST /api/llm/gateway-configs/{config_id}/models": "ai:model:create",
    "POST /api/llm/gateway-configs/{config_id}/sync-models": "ai:gateway:syncModels",
    "POST /api/llm/gateway-configs/{config_id}/test": "ai:gateway:test",
    "POST /api/notes/batch-create-drafts": "xhs:library:batchCreateDrafts",
    "POST /api/notes/batch-delete": "xhs:library:delete",
    "POST /api/notes/batch-publish": "xhs:publish:submit",
    "POST /api/notes/batch-save": "xhs:library:save",
    "POST /api/notes/batch-sync": "xhs:note:batchSync",
    "POST /api/notes/batch-tag": "xhs:topic:edit",
    "POST /api/notes/export": "xhs:library:view",
    "POST /api/notes/import-data": "xhs:library:importData",
    "POST /api/notes/sync-data": "xhs:library:syncData",
    "POST /api/notes/{note_id}/assets": "xhs:note:edit",
    "POST /api/notes/{note_id}/hydrate": "xhs:note:sync",
    "POST /api/notes/{note_id}/publish": "xhs:publish:submit",
    "POST /api/notes/{note_id}/sync": "xhs:note:sync",
    "POST /api/notifications/read-all": "xhs:note:edit",
    "POST /api/notifications/{notification_id}/read": "xhs:note:edit",
    "POST /api/publish/assets/{asset_id}/upload": "xhs:note:edit",
    "POST /api/publish/jobs/{job_id}/assets": "xhs:note:edit",
    "POST /api/publish/jobs/{job_id}/cancel": "xhs:publish:cancel",
    "POST /api/publish/jobs/{job_id}/publish": "xhs:publish:submit",
    "POST /api/publish/jobs/{job_id}/retry": "xhs:publish:retry",
    "GET /api/schedule/overview": "EXEMPT_ADMIN",
    "POST /api/schedule/cron/validate": "xhs:job:view",
    "POST /api/schedule/jobs": "xhs:job:create",
    "POST /api/schedule/jobs/{job_id}/run": "xhs:job:run",
    "POST /api/settings": "EXEMPT_SELF",
    "POST /api/system/deps/download": "system:setting:edit",
    "POST /api/system/deps/reset": "system:setting:edit",
    "POST /api/system/dict/items": "system:dict:create",
    "POST /api/system/dict/types": "system:dict:create",
    "POST /api/system/menus": "system:menu:create",
    "POST /api/system/menus/reorder": "system:menu:edit",
    "POST /api/system/roles": "system:role:create",
    "POST /api/system/update/download": "system:setting:edit",
    "POST /api/system/update/install": "system:setting:edit",
    "POST /api/system/users": "system:user:create",
    "POST /api/system/users/{user_id}/approve": "system:user:approve",
    "POST /api/system/users/{user_id}/force-logout": "system:user:forceLogout",
    "POST /api/system/users/{user_id}/reset-password": "system:user:resetPassword",
    "POST /api/tasks/clean": "xhs:record:clean",
    "POST /api/tasks/clean-all": "xhs:record:cleanAll",
    "POST /api/tasks/run-due": "xhs:job:run",
    "POST /api/tasks/{task_id}/cancel": "xhs:record:cancel",
    "POST /api/tasks/{task_id}/retry": "xhs:record:retry",
    "POST /api/topics": "xhs:topic:create",
    "POST /api/xhs/analytics/benchmarks/{target_id}/create-drafts": "xhs:insights:view",
    "POST /api/xhs/analytics/platform-hot-topics/generate": "xhs:insights:view",
    "POST /api/xhs/analytics/platform-hot-topics/refresh": "xhs:insights:view",
    "POST /api/xhs/analytics/reports": "xhs:insights:view",
    "POST /api/xhs/crawl/data": "xhs:crawl:submit",
    "POST /api/xhs/crawl/note-urls": "xhs:crawl:submit",
    "POST /api/xhs/crawl/search-notes": "xhs:crawl:submit",
    "POST /api/xhs/crawl/user-notes": "xhs:crawl:submit",
    "POST /api/xhs/creator/assets/upload": "xhs:note:edit",
    "POST /api/xhs/creator/locations/search": "xhs:topic:create",
    "POST /api/xhs/creator/publish/image": "xhs:publish:submit",
    "POST /api/xhs/creator/publish/video": "xhs:publish:submit",
    "POST /api/xhs/creator/topics/search": "xhs:topic:create",
    "POST /api/xhs/data-package-rules/execute": "xhs:rule:create",
    "POST /api/xhs/data-package-rules/rules": "xhs:rule:create",
    "POST /api/xhs/data-package-rules/sets": "xhs:rule:create",
    "POST /api/xhs/forbidden-words": "xhs:forbiddenWord:create",
    "POST /api/xhs/forbidden-words/groups/{name}/enabled": "xhs:forbiddenWord:create",
    "POST /api/xhs/forbidden-words/{word_id}/enabled": "xhs:forbiddenWord:create",
    "POST /api/xhs/llm/prompt-modules": "xhs:item:edit",
    "POST /api/xhs/llm/prompt-templates": "xhs:item:edit",
    "POST /api/xhs/llm/prompt-templates/render-preview": "ai:prompt:create",
    "POST /api/xhs/login-sessions/creator/phone/confirm": "xhs:item:edit",
    "POST /api/xhs/login-sessions/creator/phone/send-code": "xhs:item:edit",
    "POST /api/xhs/login-sessions/creator/qrcode": "xhs:item:edit",
    "POST /api/xhs/login-sessions/pc/phone/confirm": "xhs:item:edit",
    "POST /api/xhs/login-sessions/pc/phone/send-code": "xhs:item:edit",
    "POST /api/xhs/login-sessions/pc/qrcode": "xhs:item:edit",
    "POST /api/xhs/login-sessions/qianfan/email-login": "xhs:item:edit",
    "POST /api/xhs/login-sessions/qianfan/qrcode": "xhs:item:edit",
    "POST /api/xhs/materials/files/delete-batch": "xhs:material:createFolder",
    "POST /api/xhs/materials/files/upload": "xhs:material:createFolder",
    "POST /api/xhs/materials/folders": "xhs:material:createFolder",
    "POST /api/xhs/materials/folders/{folder_id}/scan": "xhs:material:createFolder",
    "POST /api/xhs/monitoring/targets": "xhs:monitor:create",
    "POST /api/xhs/monitoring/targets/{target_id}/refresh": "xhs:monitor:create",
    "POST /api/xhs/package-precheck": "xhs:publish:precheck",
    "POST /api/xhs/package-precheck/local": "xhs:publish:precheck",
    "POST /api/xhs/package-precheck/sku-name-optimize": "xhs:item:polish",
    "POST /api/xhs/package-precheck/sku-name-regenerate": "xhs:item:polish",
    "POST /api/xhs/package-precheck/{token}/scan": "xhs:publish:precheck",
    "POST /api/xhs/pc/homefeed/recommend": "xhs:note:view",
    "POST /api/xhs/pc/notes/comments": "xhs:note:view",
    "POST /api/xhs/pc/notes/detail": "xhs:note:view",
    "POST /api/xhs/pc/search/notes": "xhs:note:view",
    "POST /api/xhs/pc/search/users": "xhs:note:view",
    "POST /api/xhs/pc/users/capabilities": "xhs:note:view",
    "POST /api/xhs/pc/users/collected": "xhs:note:view",
    "POST /api/xhs/pc/users/interactions": "xhs:note:view",
    "POST /api/xhs/pc/users/liked": "xhs:note:view",
    "POST /api/xhs/pc/users/me": "xhs:note:view",
    "POST /api/xhs/pc/users/no-traffic/candidates": "xhs:note:view",
    "POST /api/xhs/pc/users/no-traffic/cleanup": "xhs:account:syncNotes",
    "POST /api/xhs/pc/users/notes": "xhs:note:view",
    "POST /api/xhs/pc/users/search": "xhs:note:view",
    "POST /api/xhs/product-notes/batch-generate": "xhs:workshop:generate",
    "POST /api/xhs/product-notes/generate": "xhs:workshop:generate",
    "POST /api/xhs/product-notes/generate-llm": "xhs:workshop:generate",
    "POST /api/xhs/product-notes/generate-llm/{record_id}/retry": "xhs:workshop:generate",
    "POST /api/xhs/product-notes/generate-save": "xhs:workshop:generate",
    "POST /api/xhs/product-notes/versions/regen": "xhs:workshop:regenerate",
    "POST /api/xhs/product-notes/versions/{record_id}/adopt": "xhs:workshop:adopt",
    "POST /api/xhs/product-notes/versions/{record_id}/publish": "xhs:workshop:adopt",
    "POST /api/xhs/qianfan/after-sales/handle": "xhs:order:afterSaleHandle",
    "POST /api/xhs/qianfan/arbitration-tags": "xhs:violation:view",
    "POST /api/xhs/qianfan/arbitrations": "xhs:violation:view",
    "POST /api/xhs/qianfan/binding": "xhs:item:view",
    "POST /api/xhs/qianfan/checkup/advice": "xhs:violation:view",
    "POST /api/xhs/qianfan/checkup/negative-feedback": "xhs:violation:view",
    "POST /api/xhs/qianfan/checkup/risks": "xhs:violation:view",
    "POST /api/xhs/qianfan/compensations": "xhs:violation:view",
    "POST /api/xhs/qianfan/exemptions/items": "xhs:violation:view",
    "POST /api/xhs/qianfan/exemptions/overview": "xhs:violation:view",
    "POST /api/xhs/qianfan/generate-detail-image": "xhs:item:view",
    "POST /api/xhs/qianfan/health-check": "xhs:item:view",
    "POST /api/xhs/qianfan/item-detail": "xhs:item:view",
    "POST /api/xhs/qianfan/item-drafts": "xhs:item:edit",
    "POST /api/xhs/qianfan/item-drafts/delete": "xhs:item:edit",
    "POST /api/xhs/qianfan/item-drafts/parse": "xhs:item:edit",
    "POST /api/xhs/qianfan/item-generations": "xhs:item:view",
    "POST /api/xhs/qianfan/item-notes": "xhs:item:view",
    "POST /api/xhs/qianfan/items": "xhs:item:edit",
    "POST /api/xhs/qianfan/items/delete": "xhs:item:delete",
    "POST /api/xhs/qianfan/items/export": "xhs:item:edit",
    "POST /api/xhs/qianfan/items/operate": "xhs:item:edit",
    "POST /api/xhs/qianfan/items/polish": "xhs:item:edit",
    "POST /api/xhs/qianfan/items/publish": "xhs:publish:submit",
    "POST /api/xhs/qianfan/items/sync": "xhs:item:edit",
    "POST /api/xhs/qianfan/note-cleanup/candidates": "xhs:item:view",
    "POST /api/xhs/qianfan/orders": "xhs:order:ship",
    "POST /api/xhs/qianfan/orders/detail": "xhs:order:ship",
    "POST /api/xhs/qianfan/orders/export": "xhs:order:ship",
    "POST /api/xhs/qianfan/orders/remark": "xhs:order:ship",
    "POST /api/xhs/qianfan/orders/ship": "xhs:order:ship",
    "POST /api/xhs/qianfan/orders/ship-batch": "xhs:order:ship",
    "POST /api/xhs/qianfan/orders/ship/preview": "xhs:order:ship",
    "POST /api/xhs/qianfan/orders/sync": "xhs:order:ship",
    "POST /api/xhs/qianfan/picture-space/delete": "xhs:item:view",
    "POST /api/xhs/qianfan/picture-space/download": "xhs:item:view",
    "POST /api/xhs/qianfan/picture-space/folders": "xhs:item:view",
    "POST /api/xhs/qianfan/picture-space/items": "xhs:item:view",
    "POST /api/xhs/qianfan/picture-space/move": "xhs:item:view",
    "POST /api/xhs/qianfan/picture-space/permanent-delete": "xhs:item:view",
    "POST /api/xhs/qianfan/picture-space/preview": "xhs:item:view",
    "POST /api/xhs/qianfan/picture-space/resize": "xhs:item:view",
    "POST /api/xhs/qianfan/picture-space/restore": "xhs:item:view",
    "POST /api/xhs/qianfan/picture-space/upload": "xhs:item:view",
    "POST /api/xhs/qianfan/picture-space/upload-batch": "xhs:item:view",
    "POST /api/xhs/qianfan/product/after-sale-rule": "xhs:item:view",
    "POST /api/xhs/qianfan/product/logistics-templates": "xhs:item:view",
    "POST /api/xhs/qianfan/product/predict-properties": "xhs:item:view",
    "POST /api/xhs/qianfan/product/property-config": "xhs:item:view",
    "POST /api/xhs/qianfan/product/recommend-category": "xhs:item:view",
    "POST /api/xhs/qianfan/product/recommend-title": "xhs:item:view",
    "POST /api/xhs/qianfan/product/shipping-templates": "xhs:item:view",
    "POST /api/xhs/qianfan/product/short-title": "xhs:item:view",
    "POST /api/xhs/qianfan/save-detail-images": "xhs:item:view",
    "POST /api/xhs/qianfan/spot-checks": "xhs:violation:view",
    "POST /api/xhs/qianfan/sync-shop-goods": "xhs:item:view",
    "POST /api/xhs/qianfan/video/upload": "xhs:item:view",
    "POST /api/xhs/qianfan/violation/appeal-record": "xhs:violation:view",
    "POST /api/xhs/qianfan/violation/counts": "xhs:violation:view",
    "POST /api/xhs/qianfan/violation/detail": "xhs:violation:view",
    "POST /api/xhs/qianfan/violation/evidence": "xhs:violation:view",
    "POST /api/xhs/qianfan/violation/list": "xhs:violation:view",
    "POST /api/xhs/qianfan/violation/meta": "xhs:violation:view",
    "POST /api/xhs/qianfan/violation/related-delete": "xhs:violation:relatedDelete",
    "PUT /api/drafts/{draft_id}/assets/reorder": "xhs:note:edit",
    "PUT /api/llm/business-providers": "ai:model:setBusiness",
    "PUT /api/notes/{note_id}/assets/reorder": "xhs:note:edit",
    "PUT /api/notes/{note_id}/topics": "xhs:topic:edit",
    "PUT /api/schedule/jobs/{job_id}": "xhs:job:edit",
    "PUT /api/settings/{key}": "EXEMPT_SELF",
    "PUT /api/system/dict/items/{item_id}": "system:dict:edit",
    "PUT /api/system/dict/types/{type_id}": "system:dict:edit",
    "PUT /api/system/menus/{menu_id}": "system:menu:edit",
    "PUT /api/system/roles/{role_id}": "system:role:edit",
    "PUT /api/system/roles/{role_id}/menus": "system:role:assignMenu",
    "PUT /api/system/roles/{role_id}/users": "system:role:assignMenu",
    "PUT /api/system/users/{user_id}": "system:user:edit",
    # ── 只读页 GET（用户口径「只读，无变更码，也需要授权」）──────────
    "GET /api/xhs/analytics/overview": "xhs:insights:view",
    "GET /api/xhs/analytics/top-content": "xhs:insights:view",
    "GET /api/xhs/analytics/hot-topics": "xhs:insights:view",
    "GET /api/xhs/analytics/engagement": "xhs:insights:view",
    "GET /api/xhs/analytics/keyword-trends": "xhs:insights:view",
    "GET /api/xhs/analytics/comment-insights": "xhs:insights:view",
    "GET /api/xhs/analytics/platform-hot-topics": "xhs:insights:view",
    "GET /api/xhs/analytics/platform-hot-topics/matches": "xhs:insights:view",
    "GET /api/llm/usage/overview": "ai:usage:view",
    "GET /api/llm/usage/daily": "ai:usage:view",
    "GET /api/llm/usage/breakdown": "ai:usage:view",
}

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
    #: ⚠️ **只留「整族全是只读」的前缀**。`/api/xhs/analytics` 曾被列在这里，
    #: 但该前缀下还有 `POST /topic`（生成热度话题）—— 前缀规则要求**该前缀下
    #: 所有 method** 都挂 view 码，于是那个 POST 也会被要求挂 `xhs:insights:view`，
    #: 或者反过来让「只读页守卫」把写端点也当成只读页。
    #: 2026-10-24 实测：该前缀下有 1 条写端点 ⇒ 前缀规则不适用，改为逐条登记
    #: （见 `READONLY_ROUTES`）。
    "/api/llm/usage": "ai:usage:view",
}

#: 只读页的 GET 路由 → view 码（用户口径「只读，无变更码，**也需要授权**」）。
#:
#: 为什么单列一张表而不是混进 `ROUTE_PERMS`：`ROUTE_PERMS` 守的是「写路由必须挂码」，
#: 那是**拒启**级；只读页是「进不去就 403」，漏了不会让系统崩，但会让
#: 「没有内容洞察权限的人能通过直敲 URL 看到数据」—— 一样是越权，所以也要守。
#:
#: 键 = `METHOD path`；值 = view 码。这些码在 `perms_registry` 里已存在。
READONLY_ROUTES: Final[dict[str, str]] = {
    # 数据洞察（4 组只读页之一）
    "GET /api/xhs/analytics/overview": "xhs:insights:view",
    "GET /api/xhs/analytics/top-content": "xhs:insights:view",
    "GET /api/xhs/analytics/hot-topics": "xhs:insights:view",
    "GET /api/xhs/analytics/engagement": "xhs:insights:view",
    "GET /api/xhs/analytics/keyword-trends": "xhs:insights:view",
    "GET /api/xhs/analytics/comment-insights": "xhs:insights:view",
    "GET /api/xhs/analytics/platform-hot-topics": "xhs:insights:view",
    "GET /api/xhs/analytics/platform-hot-topics/matches": "xhs:insights:view",
}

#: 只读页前缀 → view 码（`READONLY_PAGE_GUARDS` 的历史形态，保留兼容）。
