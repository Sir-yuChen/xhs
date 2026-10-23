# 数据分析报告 · dataPanel 基础架构 / 基础组件 / 管理权限 → 迁移到 XHS

> 阶段：**通道 A · 阶段一（需求大纲）** —— 本文只为 Gate 1 审核使用，**不含代码改动**。
> 依据：`docs/00_管理规范/00_需求工作流速查.md`（地基类判据：新建表 / 迁移 / 端点 / 跨模块契约 ⇒ 必须设计先行 + 全量回归）
> 实测时间：2026-10-23　｜　对照源码：`D:\work\GoWorkSpace\dataPanel`（HEAD `134848d 活动管理v1`）
> 当前项目实测基线：**303 paths / 350 operations / 57 表 / alembic head `c1d2e3f4a5b8` / 前端请求函数 300 / 页面文件 47**
> （`curl 127.0.0.1:8000/openapi.json` 实测 + `verify_doc_counts.py` PASS）

---

## 0. 一句话结论

dataPanel 的管理权限是**菜单/按钮两级 RBAC + 权限码三处一致 + 数据归属断言**，落到「3 张身份表 + 2 张权限表 + 中间件」；
XHS 现在只有 **2 个写死角色**、**`require_admin` 零调用点**、**所有写操作对任何登录用户开放**。
本轮要补的**不是 UI 皮肤，而是权限骨架**；其中 3 处必须**按现在还是单人使用的时候定死**，
否则一旦出现第二个用户就无法回退（报告 §5）。

---

## 1. 实况对比（逐项实测）

### 1.1 身份与权限（用户明确点名的重点）

| 项 | dataPanel（对照） | XHS 现状（实测） | 差距 |
|---|---|---|---|
| 用户与角色 | `t_user` / `t_role` / `t_user_role` 三表，一人多角色 | `users.role` 单列，取值 `admin`/`user`（`core/roles.py`） | 无多角色、无角色实体 |
| 菜单 | `t_menu`（`M` 目录 / `C` 菜单 / `F` 按钮）+ `t_role_menu` | **无菜单表**，菜单硬编码在 `app-shell.tsx`（29 个 key） | 菜单与权限码未合并成一张表 |
| 权限码 | `perms` 字符串（`<域>:<动作camelCase>`），登录时随令牌下发，前端 `hasPermission` + 后端 `PermissionMiddleware` + SQL `t_menu.perms` **三处一致** | **零权限码**：`grep hasPermission frontend/src` = 0 命中 | 完全缺失 |
| 后端拦截 | 每个写接口挂 `PermissionMiddleware("x:y")`，后端权威 | `require_admin`（`core/deps.py:80`）**全仓仅 1 处命中，且是注释** | 形同不存在 |
| 数据归属 | `authz.AssertOwned(createdBy, operator, isSuperAdmin)`，非超管只能操作自己创建的；超管豁免 | 按 `user_id` 隔离（`get_owned_active_account` 等），无「创建人」维度 | 缺「同租户内可见性」 |
| 前端行为 | 按钮级：`ActionItem.show: () => hasPermission(...)` / 开关 `disabled` | `AppShell` 与 47 个页面**任何人看到全部** | 无 |
| 登录 | 邮箱 + **邮箱验证码**；`register_enabled` 开关（默认关，注册需管理员审核，`pending` 态） | 用户名 + 密码；注册即建用户、立即获得**全量**数据面 | 注册即全权（最危险） |
| 令牌 | access/refresh + Redis 会话缓存（`GetCachedUserSession`，会话不存在即 401）+ 强制下线 | access/refresh + `token_version`（改密/登出撤销）；**无在线会话概念** | 不能「强制下线」、看不到「在线」 |
| 个人资料 | `/identity/user/profile`、`change-password`、邮箱验证码改敏感信息、头像（base64） | `/auth/me` 只回 `{id, username, role}`；改密走 `/auth/revoke-all-sessions` | 无资料页、无改密入口 |
| 登录安全 | 失败计数 + 锁定 + 验证码 | 内存失败计数 + 429 锁（`auth.py`，DEF-030），重启即清 | 基本够，缺持久审计 |
| 审计 | `t_audit_log`（异步批量消费者）—— ⚠️ 但 **`AuditLogMiddleware` 未全局挂载**，且**无查询端点** | `api_logs`（`core/api_audit.py`），**只记 `/api/xhs/**` 与 `/api/llm/**` 的写 + 任何 4xx/5xx**；**无查询 UI** | 表在、看不见 |

### 1.2 系统设置

| 项 | dataPanel | XHS 现状 |
|---|---|---|
| 分级 | **三层**：`Settings`（yaml/env 启动期、含密钥、不可热改）→ `t_config`（DB，可热改）→ 内置兜底 | **三层但混在一起**：`Settings`（yaml/env）→ `system_settings`（DB）→ `BUILTIN_DEFAULTS` |
| 归属 | `t_config` 全局 + `?key` 公开取值 | ⚠️ **`system_settings` 唯一键 `(user_id, key)`，所有行必属某用户**（模型注释明确「不存在全局共享行」，`scope` 列已被移除） |
| 生效性标注 | 「配置管理」页 + `clear-cache` | `LIVE_KEYS` 逐个注明**运行时读点**，UI 标「实时生效 / 仅存档」（9 处读点实测） |
| UI | 表格 + 新增/编辑/删除 + 清缓存 + 字典（`sys_*` 12 组） | 单页 1 张卡 + 1 个弹窗（432 行），**无分组 Tab、无搜索**，`system_settings` 与「任务中心/调度」页分离 |
| 危险开关 | `is_dangerous` 二次确认 | 有该列但 **`scheduler.enabled` 等全局量根本不是「行」**（yaml/env），UI 改不到 |

### 1.3 其它基础组件（与 XHS 缺口的对应关系）

| dataPanel 组件 | 作用 | XHS 现状 |
|---|---|---|
| `SearchPanel` | 搜索区一行 + 「更多 ⌄」折叠 + 查询/重置右对齐 | **无**：24 个列表页各自写表单 |
| `useAutoTableY`（ResizeObserver） | 表格高度自适应，分页常驻 | **无**：仅 3 页写死 `calc(100vh - 87px)`，其余 21 页分页会被内容顶出视口 |
| `useActionColumn` + `ActionColumnTitle` + `LayoutSettings` | 操作列 2×2 网格 + 「⋯」溢出 + **表头齿轮按用户调排序/显隐并持久化** | **无**：操作列手写，`shop-items-page.tsx` 单页 4054 行、操作列固定宽度 260 |
| `AppModal` | 宽度令牌 `sm/md/lg/xl` + ≤85vw + body 限高 85vh + 居中 | **无**：5 种写法并存（`centered` / `75vw` / 固定 px / `style.top`） |
| `DictSelect` / `DictTag` / `DictTypeSelect` + `useDict` | 枚举一律走字典表（禁写死），一次请求多实例共享 | **无字典表**：`core/qianfan_item_enums.py` 等 Python 常量 + 前端 `qianfan-enums.ts` 双份 |
| `CopyableText` / `TextColumn` | 长文本省略 + Tooltip；点击复制 | 无（uuid 类展示在各页自己实现） |
| `errorBoundary` / `PrivateRoute` / `TabContent` | 页面级错误兜底 / 守卫 / Tab 内容 | 有 `ProtectedRoute`（仅登录态），**无错误边界** |
| `TabContent` + `tabStore` | 多页签工作区（最多 15，右键关闭） | **无**：单层路由跳转 |
| `usePermission` / `authStore.permissions` | 权限码 hook | **无** |

### 1.4 dataPanel 有、但**不建议**搬的部分（反面清单）

| 项 | 不搬的理由 |
|---|---|
| MobX | XHS 用 `useSyncExternalStore` + `useState`，引 MobX 是**换技术栈**（须 ADR），收益为零 |
| `@ant-design/pro-components`（ProTable / ModalForm） | 项目现状是 antd 原生 `Table`；ProTable 会与「上中下三段式固定布局」和既有 18 个页面守卫冲突 |
| Wails 桌面壳 / `sessionsStorage` 记住密码 | XHS 是 PyInstaller + `run-app.py` 监督进程，壳层机制不同 |
| Session 存 Redis | XHS 是 SQLite 单机部署，无 Redis 依赖；会话改用 DB 表 |
| `t_audit_log` 那套（未挂载 + 无查询） | 是 dataPanel 自己的半成品，照抄会复制缺陷 |
| 5 个种子角色（`super_admin/admin/operator/finance/viewer`） | XHS 只有两类登录者；种子过多反而让「谁能改全局」变模糊 |

---

## 2. 必须先定死的 3 个判断（回退成本随用户数指数上升）

| # | 问题 | 现状事实 | 结论（建议） | 不定死的后果 |
|---|---|---|---|---|
| **D1** | `system_settings` 到底「每用户一份」还是「全局一份」 | 模型注释写明「所有行必归属用户，不存在全局共享行」；`scheduler.enabled` 这类全局量**根本不在这张表**（在 yaml/env） | **保留现状**，不改成全局表；「全局量」走一个**只读展示 + 管理员可改的独立分组**（见 D2） | 一旦有多用户、各自调过限流，改成全局表要做**合并策略**（谁的值为准？），无法自动 |
| **D2** | 全局量的「管理员」判据 | `core/roles.py` 只有 `admin`/`user`；`require_admin` 零调用 | **确定唯二身份**：`admin` = 能改全局量 / 管用户；`user` = 只能管自己的数据面。不再引入第三种 | 若先上线「人人可改调度/限流」，后面收权要逐个用户解释 |
| **D3** | 注册开放到什么程度 | 现在注册即建用户、**立即获得全量数据面**（含平台写操作） | 三档之一：**(a) 关闭注册（仅管理员建号，推荐）** / (b) 开放但新用户 `role=user` 且看不到「设置·用户管理·全局量」 / (c) 注册进待审 `pending` | 提交 1.0.7 已发版、安装包已在流通；先放任再收紧会撞「老用户权限突变」 |

> ⚠️ 报告 §5 会再给一份「硬约束回填」清单 —— 这 3 条无论选哪个都要先写进文档再动手。

---

## 3. 迁移方案（模块 × 落地形态）

> 全部按「**补骨架、不换皮肤**」原则：保留 XHS 现有 antd/React 形态与 47 个页面，只**新增**公共层与系统管理面。

### 3.1 后端

| 模块 | 新增内容 | 明细 |
|---|---|---|
| **M1 身份与权限（骨架）** | **+3 表**：`menus`（`M`/`C`/`F` 三级 + `perms` + `path` + `component` + `icon` + `sort`）、`roles`（`code` 唯一 + `is_system`）、`role_menus`；**+1 列** `users.status`（`normal`/`disabled`/`pending`）；**+1 列** `users.real_name` | 角色码固定两个内置行（`admin` / `user`，`is_system=1` 禁删）；`users.role` 列**保留作为冗余快照**并双写，读路径逐步切到 `roles`（避免一次性切换砸 47 个页面所属的鉴权链） |
| **M2 权限中间件** | `core/perms.py`（`require_perm("x:y")` / `is_admin` 依赖 + 权限码常量表）+ 给**全部写端点**按域挂码 | 权限码命名 `<域>:<动作camelCase>`，**查询可公用、变更一按钮一码**（dataPanel 硬口径）；先落地 5 个域：`system` / `account` / `note` / `shop` / `publish`，覆盖 267 个写请求里的 P0 面（见 §4 验收） |
| **M3 系统管理面（端点）** | **+14 端点 / +8 path**：用户 CRUD 5（`GET`/`POST /api/system/users`、`GET`/`PUT`/`DELETE /api/system/users/{id}`）、`POST /users/{id}/reset-password`、`POST /users/{id}/force-logout`、角色 CRUD 4（`/api/system/roles` + `/{id}`）、`GET /api/system/roles/{id}/menus` + `PUT`、`GET /api/system/menus/tree`、`GET /api/system/me/menus` | 全部 `admin` 门禁；归属校验复用现有 `get_owned_active_account` 风格（**系统域例外**：由 `is_admin` 统一把关，不做 per-user 归属） |
| **M4 登录/注册/个人资料** | `POST /api/auth/register` 加 `register_enabled` 开关（`system_settings` 的**全局只读项**）+ 新用户默认 `role=user`；`GET`/`PUT /api/auth/me`（扩字段：`real_name` / `status` / `permissions` / `menus`）；`POST /api/auth/change-password`（旧密码） | ⚠️ 现 `/auth/me` 只回 3 字段，前端 `PlatformUser` 类型只有 `{id, username}` —— 要扩类型（**接缝**） |
| **M5 审计查询** | `GET /api/system/audit-logs`（分页 + 按用户/状态/路径筛选） | 复用 `api_logs`，**不新建表**；**只读**，无删除端点（审计不可删） |
| **M6 字典（可选，第二批）** | `dict_types` / `dict_items` 两表 + `GET /api/system/dict/{code}` | 先只读同步 `qianfan_item_enums.py` 一份进表（**单一真相源仍是 Python**，表只做展示/前端下拉），避免枚举双份维护 |

### 3.2 前端（系统设置模块 —— 用户点名重点）

| 页面 / 组件 | 做什么 | 行数预估 |
|---|---|---|
| `pages/system/system-page.tsx` | 系统设置的**宿主页 + Tab**（侧栏一项进四个页签：用户 / 角色 / 菜单 / 审计） | ~180 |
| `pages/system/users-page.tsx` | 用户列表：ID / 用户名 / 姓名 / 身份 / 状态（含「在线」）/ 操作（编辑、分配角色、重置密码、**强制下线**、禁用、删除） | ~380（照 dataPanel `system/user` 的字段与交互，**去 ProTable**） |
| `pages/system/roles-page.tsx` | 角色列表 + **分配菜单权限**（`Tree` checkable，回填半选 `checkStrictly`） | ~300 |
| `pages/system/menus-page.tsx` | 菜单树（`M`/`C`/`F`，树形表格 + 搜索 + 新增/编辑/删除） | ~320 |
| `pages/system/audit-log-page.tsx` | 审计日志分页表 + 筛选（用户 / 状态 / 路径）；只读 | ~160 |
| `pages/profile/profile-page.tsx` | 个人资料（改姓名 / 头像 / 改密）+ 顶栏头像下拉「个人信息 / 退出登录」 | ~200 |
| `pages/login/login-page.tsx`（改） | 加「注册」入口（受 `register_enabled` 控制）+ 明确失败文案（禁用 / 待审） | 改 ~60 |

### 3.3 前端公共组件（补 XHS 缺失的基础组件 —— 本报告第二块交付）

| 新组件 / hook | 落地形态 | 改造面 |
|---|---|---|
| `components/ui/search-panel.tsx` | 一行常用条件 + 「更多 ⌄」折叠（面板式，不套框）+ 右侧查询/重置 | 新组件；**先接 3 个页**（settings / 用户 / 审计），后续页按需迁 |
| `hooks/use-table-height.ts` | `ResizeObserver` + `MutationObserver`（照 `useAutoTableY`，去掉 ProTable 选择器，改认 `.xx-table` 与 `flex:1` 容器） | 新 hook；替换 3 处写死的 `calc(100vh - 87px)`，其余 21 页**第二批** |
| `components/ui/action-column.tsx` + `hooks/use-action-column.ts` | 2×2 网格 + 「⋯」溢出 + **列头齿轮**（排序/显隐，`localStorage` 按 `user + page`） | 新组件；`shop-items-page.tsx` 4054 行的操作列是最大受益者（**单页改造**，第二批） |
| `components/ui/app-modal.tsx` | 宽度令牌 `sm=560/md=720/lg=960/xl=1280` + ≤85vw + `body.maxHeight 85vh` | 新组件；新页面全用，存量 5 种写法**第二批**统一 |
| `components/ui/text-column.tsx` / `copyable-text.tsx` | 省略 + Tooltip；点击复制（含「已复制」反馈） | 新组件；新页面全用 |
| `components/ui/error-boundary.tsx` | 页面级异常兜底（antd `Result` + 重新加载 + 回首页） | 包在 `router.tsx` 的每个 route element 外（**一次接线，全站受益**） |
| `hooks/use-permission.ts` + `app/providers` | `hasPermission(code)` / `hasAny` / `isAdmin`，权限码来自 `/auth/me` | 新 hook；菜单与按钮按码过滤 |

> ⚠️ **不做**：MobX / ProTable / Tab 多页签工作区 / 字典全量替换（这四项属「换技术栈或整站改造」，要单独立项 + ADR）。

### 3.4 涉及范围速览

| 层 | 会动到的 |
|---|---|
| **后端** | 端点 **+14/+8 path**（350/303 → **364/311**）；表 **+3**（57 → **60**，另 `users` +2 列）；迁移 **+1**（`d1a2b3c4d5e6`，revises `c1d2e3f4a5b8`，batch mode）；新文件 `core/perms.py`、`api/system_admin.py`、`services/permission_service.py` |
| **前端** | 页面 **+6**（47 → 53）、公共组件 **+8**、请求函数 **+14**（300 → **314**）、路由 **+5** |
| **外部** | **零平台请求**（纯本地管理面，不碰小红书/千帆 ⇒ 无抓包需求） |
| **文档** | 新设计文档（本文升级为功能设计 + 总-分-总）、用例库 **+14 条 / P0 +9**、验收清单新链、`AGENTS.md` §5 数字、`verify_doc_counts.py` 双校验 |

---

## 4. 分阶段建议（每阶段可独立验收，按「逐需求闭环」）

| 阶段 | 内容 | 为什么排这儿 |
|---|---|---|
| **P0（先做）** | M1 三表 + M2 中间件骨架 + `is_admin` 判据 + **给 267 个写端点挂码**（先挂、先拒） | 这是**唯一不能事后补**的部分：越晚，越多数据在「无归属」状态下产生 |
| **P1** | M3 用户/角色/菜单管理页（4 页 + 侧栏「系统设置」） + M4 注册开关/个人资料 | 用户点名的重点，且 P0 的码要能配才用得上 |
| **P2** | 公共组件 4 件（`SearchPanel` / `useTableHeight` / `AppModal` / `ActionColumn`）+ 错误边界 | 纯前端，可独立做；先接系统页，再逐批迁存量页 |
| **P3** | M5 审计查询页 + 登录/审计补强（登录成功/失败、权限拒绝都落 `api_logs`） | 依赖 P0 的码（要能看到「被拒」） |
| **P4（可选）** | M6 字典只读 + `TextColumn`/`CopyableText` 存量替换 | 收益递减，最后做 |

⚠️ 每阶段都必须：`pytest` 相关文件 + `tsc --noEmit` + 三校验脚本 + **真机验证一次**（admin 与 user 两个账号各走一遍）。

---

## 5. 硬约束回填（要写进 `AGENTS.md` §3 —— 因为「不变量必须确定性出现」）

| # | 新硬约束 | 为什么必须落到 AGENTS（不能只写在设计文档里） |
|---|---|---|
| C-1 | **权限码三处一致**：前端 `hasPermission(code)` ↔ 后端 `require_perm("code")` ↔ `menus.perms`；**后端权威**，漏挂=越权 | 与现有 C1–C6 同级，属「违反就返工」 |
| C-2 | **一切变更操作一按钮一码**；查询可公用；码名 `<域>:<动作camelCase>`，禁写死角色名 | dataPanel 实测：共用管理码 ⇒ 「谁能删」说不清 |
| C-3 | **数据归属**：业务数据按 `user_id` 隔离（不变）；**系统域例外**由 `is_admin` 统一把关，且**不做** per-user 归属（否则 admin 看不到别人的号） | 两条规则并存必须写清边界，否则会出现「按创建人过滤掉超管该看的行」 |
| C-4 | **新增路由必须登记菜单行**：`menuKey` → `perms` → `path` 三件一起加；`router.tsx` 的 route 与 `menus` 表**同名同 path** | 否则「页面能进但菜单不显示」或反之 |
| C-5 | **前端菜单一律来自 `/auth/me` 的 `menus`**，禁在 `app-shell.tsx` 硬编码可见性（现 29 个 key 是唯一真相源，要改成表） | 硬编码菜单 + 后端权限 = 两处真相 |

---

## 6. 风险与已知坑

| # | 风险 | 缓解 |
|---|---|---|
| R1 | **267 个写端点逐个挂码**会漏 | 写守卫测试：遍历 `app.routes`，**每个非 GET 端点必须能解析出权限码**，否则红 |
| R2 | 前端 47 页已上线，改菜单来源可能让某页「点了没反应」 | 菜单改造期间**双读**（表优先、硬编码兜底），守卫断言「两边 key 集合相等」，一致后再删硬编码 |
| R3 | `users.role` 列与 `roles` 表双写不同步 | 只允许经 `permission_service` 改角色；守卫：改 `role_menus` 后断言 `users.role` 快照一致 |
| R4 | 迁移撞 head | 新迁移 `down_revision = "c1d2e3f4a5b8"`（实测唯一 head）；**库副本三向实测**（升级/重跑幂等/降级再升级） |
| R5 | 注册开关默认值影响已发版的 1.0.7 安装包 | 默认 `register_enabled=false` 需在同版本内说明；老库升级后**首个用户自动为 `admin`**（沿用 `roles.py` 的 bootstrap 规则） |
| R6 | 前端 `tsconfig include: ["src"]` ⇒ 测试文件参与 `tsc` | 新组件测试写 `*.test.ts` 时必须类型干净 |

---

## 7. 明确不做

1. 不引入 MobX / `@ant-design/pro-components` / Wails / Redis（技术栈零变更）。
2. 不做「Tab 多页签工作区」（`tabStore` 那套）—— 与现有路由模型不兼容，收益低于风险。
3. 不把 `system_settings` 改成全局表（见 D1）。
4. 不做字典表对 `qianfan_item_enums` 的**替换**（只做只读镜像）。
5. 不动小红书/千帆任何平台写接口的语义（权限只**加闸**，不改链路）。
6. 不搬 dataPanel 的 `t_audit_log` 半成品；审计复用 `api_logs`。

---

## 8. Gate 1 需要你拍的四件事

| # | 决策 | 我的建议 |
|---|---|---|
| Q1 | 注册策略（D3）：关闭注册 / 开放但限权 / 待审 | **(a) 默认关闭，管理员建号** —— 1.0.7 已发版，先收紧再放开比反向简单 |
| Q2 | 是否现在就把 267 个写端点全部挂码（P0），还是先只挂 5 个域 | **全部挂**，但先「只拒不改」：非 admin 对 `/api/system/**` 与全局量 403，其余域先记「待配置」不发 403 |
| Q3 | 前端公共组件的改造范围：只新页面用，还是连同存量 24 个列表页一起改 | **新页面 + 系统页先用**，存量按页分批（`shop-items-page` 首批），避免一次动 4000 行 |
| Q4 | 审计日志保留多久 / 是否可删 | **不可删**（只读），保留期跟随现有「队列记录清理」任务的保留天数 |

---

<!-- gate2: 总-分-总 总体设计/前端设计/后端设计/前后端关联 -->
