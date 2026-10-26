# 73 · 缺陷：`has_perm` 对非 admin 恒 False（判权点集体失效）

> 2026-10-26 · 用户口径「再次扫一下菜单权限体系是否正确是否有需要补充完善的问题」→ 全量体检查出。
> **本缺陷的严重性高于所有「码写错了 / 菜单行漂了」类问题**：它让已经写对的判权点**全部失效**，
> 且不报错、不落日志、只影响非 admin（开发者用 admin 测一切正常）。

## 1. 缺陷原文

`backend/app/core/perms.py`：

```python
def has_perm(user: Any, code: str) -> bool:      # ← 签名里没有 db
    if user is None or not code:
        return False
    if code not in ALL_PERMS:
        return False
    return code in user_perms(user)              # ← user_perms 也不带 db

def user_perms(user: Any) -> frozenset[str]:
    return role_perms(getattr(user, "role", None))   # ← 不传 db
```

而 `role_perms(role_code, *, db=None)` 里有一句保守分支：

```python
if normalized == ROLE_ADMIN:
    return ALL_PERMS      # admin 走这里 ⇒ 永远「有权限」
if db is None:
    return frozenset()    # 非 admin 走这里 ⇒ 永远空集
```

⇒ **任何非 admin 用户查任何码都恒 `False`。**

## 2. 为什么它能活很久

| 因素 | 说明 |
|---|---|
| **与「真没权限」不可区分** | 返回 `False` 正是「无权限」的正常返回值 —— 调用方无法分辨 |
| **不报错、不落日志** | 没有异常，没有 warning，`False` 被当成结论 |
| **只影响非 admin** | admin 走 `ALL_PERMS` 分支永远放行 ⇒ **开发者自己用 admin 测，一切正常** |
| **真机表现温和** | 「按钮点了没反应」/「保存后没变化」，不像 500 那样刺眼 |

## 3. 受害面（实测 15 处函数内判权）

| 文件 | 位置 | 受害功能 |
|---|---|---|
| `api/schedule.py` | `_assert_can_create_in_workspace` ×2 | 新增任务（系统级 / 工作区级） |
| | `_assert_can_edit` ×2 | 编辑任务 |
| | `_get_visible_job` ×4 | 执行 / 暂停恢复任务的越权判据 |
| | `list_jobs` ×2 | 任务列表可见性 |
| | `overview` ×1 | 任务中心总览 |
| `api/settings.py` | `_resolve_scope` ×3 | 工作区级配置编辑 / 系统级配置编辑 |

统一症状：**普通管理员（manager）与普通用户点这些按钮 → 403 或「没反应」**，
而 admin 完全正常 ⇒ 测试与真机都由 admin 走过，问题被完全遮蔽。

## 4. 修法

### 4.1 加 `db` 参数，且**缺 db 时 raise**

```python
def user_perms(user, *, db=None) -> frozenset[str]:
    role = normalize_role(getattr(user, "role", None))
    if role == ROLE_ADMIN:
        return ALL_PERMS
    if db is None:
        return frozenset()
    return perms_of(db, user)          # ← 与 require_perm 同源（查库 + TTL 缓存）

def has_perm(user, code, *, db=None) -> bool:
    ...
    if role == ROLE_ADMIN:
        return True
    if db is None:
        raise PermissionResolutionError(...)   # ← 不再静默返回 False
    return code in user_perms(user, db=db)
```

**为什么 raise 而不是继续返回 `False`**：静默正是这个缺陷活下来的唯一原因。
`PermissionResolutionError` 继承 `RuntimeError`，对前端表现为 500，
但日志里一眼看到根因（与「权限不足的 403」彻底分开）。

### 4.2 单一真相源

`permission_cache.perms_of(db, user)` 是**唯一**的「用户 → 码集合」实现
（`require_perm` 依赖走的也是它）。`has_perm` 只是它的薄包装 + 未登录/未登记码的短路判定。
此前两套并行（一套查库、一套恒空）是本缺陷的结构性成因。

### 4.3 顺带修的两处

* **`api/schedule.py::create_job` 校验顺序**：纯格式校验（cron / run_at / params 必填 /
  参数键白名单）**提到判权之前** —— 「你 cron 写错了」与「你没权限」是两件事，
  前者是本地确定性判断，不该被 403 掩盖；而 `_validate_create_params`（查库、验
  `account_ids` 归属）**留在判权之后**（不能给无权限的人当数据探针）。
  ⇒ 为此把键白名单抽成 `_assert_params_keys_allowed`。
* **`api/settings.py::_resolve_scope` 缺 `db` 形参**：本函数要判配置归属级的写权限，
  但没有 db 可传 ⇒ 加 `db: Session` 首参 + 5 个调用点同步。

## 5. 守卫

`tests/backend/test_perm_resolution_requires_db.py`（**6 条**）：

| # | 判据 |
|---|---|
| ① | 非 admin + 不传 db ⇒ **必须 raise**（且报错点出缺 db、点出是哪个码） |
| ② | 非 admin + 传 db ⇒ 按库里授权返回（打桩 `perms_of` 证明走的是它） |
| ③ | admin 不需要 db（走 `ALL_PERMS`，不查库） |
| ④ | 未登记的码对 admin 也是 `False`（写错字母不放行） |
| ⑤ | `has_any` / `has_all` 必须把 db 转发下去（打桩后断言「传下去的是调用方那个 db」） |
| ⑥ | **静态**：`backend/app` 里每个 `has_perm(...)` 调用都必须带 `db=` |

⚠️ ⑥ 用 **AST** 而不是正则：本仓 docstring 里大量出现 `has_perm(...)` 作为说明文字，
正则会误报（实测 3 处假红）。且要按**完整调用**取参数（跨行调用 `has_perm(\n  a,\n  b\n)`
正则只看到 `has_perm(`，必假红）。

## 6. 真机验证

| 项 | 修前 | 修后 |
|---|---|---|
| `manager` 用户 `user_perms(user, db=db)` | **0 个码** | **150 个码** |
| `manager` 的 `xhs:taskcenter:run` | 无（被误判） | 有 |
| `manager` 的 `system:scheduler:run` | 无 | 无（**正确**：系统级任务归系统管理员） |
| 非 admin 调 `has_perm(user, code)` 不传 db | 静默 `False` | **raise**（点名缺 db） |
| `admin` | 180 个码 | 180 个码（不变） |

## 7. 顺带发现并修掉的真缺陷

### 7.1 `force-logout` 端点的装饰器与签名整块丢失

`api/system_users.py` 里 `@router.post("/{user_id}/force-logout")` + `def force_logout_user(...)`
**整块不见了**，只剩函数体裸挂在 `delete_user` 之后。后果：

* 路由不存在 ⇒ 前端「强制下线」点了没反应；
* `_assert_not_self(actor, user, "强制下线")` 这条**防自锁**判据随之消失；
* `test_system_api::test_self_disable_and_self_delete_forbidden` 断言 400 却拿到 200。

已恢复端点（权限码 `system:user:delete`，与删除同一把尺子）。

### 7.2 菜单表残行 27 行（同日另一次修复，已回写内存）

`menus` 表里 `perms` 已从登记表删除的行共 **27 行**（`xhs:item:operate` / `xhs:note:publish` /
`xhs:draft:*` 等）。种子是「只补缺不覆盖」（保护用户编辑，口径正确），
但**码被删除后那行没人负责清理** ⇒ 权限页上永远挂着勾了不生效的按钮。
处置：删 27 行菜单 + 147 行 `role_menus`，库从 218 → 191 行（与种子 total 吻合）；
另对齐 9 条名字漂移。守卫 `tests/backend/test_menu_rows_match_registry.py`（3 条）。

## 8. G8：`REMAP_PENDING` 六条全部落实（同日收尾）

`test_orphan_perm_classification.py` 的 ① `REMAP_PENDING`（「端点唯一、只需改挂」）
原本挂着 6 条欠账，本轮全部处置，**该集合已清空**：

| 码 | 端点 | 原来挂的 | 处置 |
|---|---|---|---|
| `xhs:autoops:run` | `POST /auto-tasks/{id}/run` | `xhs:autoops:edit` | 改挂 run（能编辑 ≠ 能立刻执行） |
| `xhs:picture:folder` | `POST /picture-space/folders` | `xhs:picture:view` | 改挂 folder（建目录是写操作，原挂只读码） |
| `xhs:picture:purge` | `POST /picture-space/permanent-delete` | `xhs:picture:permanentDelete` | 改挂 purge，**删掉 `permanentDelete`**（两者指同一端点，是重复码；前端只判 purge） |
| `xhs:record:clean` | `POST /tasks/clean` | `system:scheduler:cleanTasks` | 改挂 record:clean，**删掉 `cleanTasks`**（队列记录清理归记录域，与「整理任务定义」是两件事） |
| `ai:gateway:enableModel` | `PATCH /llm/gateway-configs/{id}/models/{model_id}` | — | 归 ② `DISCRIMINATED`（同一路由按 `enabled` 字段分派） |
| `xhs:autoops:enable` | `PATCH /auto-tasks/{task_id}` | — | 同上（按 `status` 字段分派） |

**净变化**：码数 **180 → 178**；种子 **191 → 189 行**；库菜单同步 189 行
（registry ↔ 种子 ↔ 库三方 178↔178↔189 全对齐）。

⚠️ 一处踩坑：`purge` 本已登记在 `SPLIT_BUTTON_PERMS`，加进 `_f()` 会**重复登记**
（`assert_registry_sane` 直接抛 `ValueError`）—— 教训是改 `_f()` 的 action 串之前
先全表 grep 该码是否已在别的元组里。

## 9. G8b：两个「按字段选码」端点落地（同日再收一项）

`ai:gateway:enableModel` 与 `xhs:autoops:enable` 是**同一端点承载两种写**的码：
只翻开关与改内容性质完全不同，但静态路由表只能挂一个码。此前它们的状态是
「登记表有、授权页能勾、勾了对接口没有任何作用」—— 与幽灵菜单同为**假授权**。

### 落地方式（同 `qianfan/items/operate`）

| 端点 | 改内容 → 路由表的码 | 只翻开关 → 函数内选码 |
|---|---|---|
| `PATCH /api/auto-tasks/{id}` | `name`/`keywords`/`ai_instruction`/`schedule` → `xhs:autoops:edit` | `status` → `xhs:autoops:enable` |
| `PATCH /llm/gateway-configs/{id}/models/{mid}` | `model_type`/`platform`/`remark` → 路由表的码 | `enabled` → `ai:gateway:enableModel` |

判定条件写成「**只**翻了那个字段 且 **值确实变了**」：
* 同时带其它字段 ⇒ 走路由表的码，不再额外要启停码（否则「有编辑权的人改内容
  顺带翻开关」会被莫名拦住）；
* 值没变 ⇒ 不算启停（幂等 PATCH 不该要码）。

两个 helper（`_assert_can_toggle_auto_task` / `_assert_can_toggle_model`）
都走 `permission_cache.perms_of`，与 `require_perm` 同一份真相源，admin 直接放行。

### 守卫

`tests/backend/test_discriminated_perm_points.py`（**7 条**）+ 变异 **5/5 CAUGHT**：

| # | 判据 |
|---|---|
| ① | 端点必须真的调用选码函数，**且调用落在 If 的 body 里**（不是裸语句） |
| ② | 该 If 的条件不能是常量 `False`/`0`/`None`（写成常量 ⇒ 恒不执行 = 没判） |
| ③ | 调用必须**早于**对受守卫字段的赋值（防「先写库再判权」） |
| ④ | helper 里判的必须是**那条码**（不是兄弟码） |
| ⑤ | 两个 helper 都必须走 `permission_cache.perms_of` 且给 admin 放行 |
| ⑥⑦ | 「只翻开关」的判定表达式：值变了才算、同时改内容不算 |

⚠️ **本轮第一版守卫是假绿的**：只查「有没有调用」，把 `if status_only_change:`
改成 `if False:` 后调用还在、守卫照绿（变异 2/5 ESCAPED）。补上「必须在 If 里且
条件非恒假」后才 5/5。这是 `mutation-verified-guards` 那条纪律的又一次实证。

### 分类表新增一组

`test_orphan_perm_classification.py` 增加 ⑤ `FIELD_DISCRIMINATED_DONE` ——
「已落地的函数内选码点」。它们**天然没有专属路由**（静态表挂的是同端点的另一个码），
若不单独分组，零路由守卫会把「已生效」误报成「欠账」。

## 11. G9：种子生成器修复 + 半成品测试移除（同日收尾）

### 11.1 `generate_menu_seed.py` 修通了

该脚本此前**跑不通**（`/tasks` 在 perms_registry 里没有 C 行），导致菜单种子只能手维护。
实测有 **4 个独立缺陷**，逐个修掉：

| # | 缺陷 | 根因 | 修法 |
|---|---|---|---|
| 1 | `/tasks` 缺 C 行 | 设计 69 把工作区任务页路径从 `/tasks` 迁到 `/platforms/xhs/tasks`，`_TOP_LEVEL_C` 没跟 | 改成新路径 |
| 2 | `/system/scheduler` 漏菜单 | 设计 69 新增的系统级任务中心没进 `_WORKSPACE_TREE` | 加进「系统管理」目录 |
| 3 | `xhs:files:view` / `xhs:login:view` 被静默丢弃 | `_page_rows` 按 **path** 去重，而同一 path 挂了两个 C 码（`picture-space` 挂 `picture:view` + `files:view`；`accounts` 挂 `account:view` + `login:view`）⇒ 后一条被丢 | 改成**每条 C 码一行**（键从 path 改成 perms），树侧补 `page_by_path` 反查，并给同 path 的其它 C 码各出一行 |
| 4 | 6 个 `?tab=` 页签行丢失 | `_TAB_CHILDREN` 声明了但树不生成 | 随 3 一起恢复 |

**现在 `--check` 通过，且种子与登记表 178 ↔ 178 零差异、零多余。**

### 11.2 一个测试期望随之修正

`test_menu_permission_alignment::test_every_page_menu_row_has_a_permission_code`
原先把 6 个 `?tab=` 页签报成「没权限码」。但页签**本就不该有独立码**
（它是同一页的视图，`perms` 留空、复用宿主页码 —— 设计 57 / `_TAB_CHILDREN`）。
⇒ 给断言加页签豁免，并写明理由。

### 11.3 `test_resolve_business_provider.py` 整文件移除

该文件是**半成品**（docstring 未闭合、import 语句残缺），且引用的
`BUSINESS_KEY_GENERATE_COVER` 已随「生成封面」业务点退场（用户口径：彻底移除）。
收集期直接 `ImportError`，**一条用例都没跑过**，长期以「collection error」污染全量
回归的四数。按用户 2026-10-26 口径**彻底移除**（不留 skip）。

### 11.4 数据库连接耗尽的插曲

本轮并行跑测试时 PostgreSQL 报 `已经太多客户端`（`too many clients`）——
是**残留的 pytest worker 进程**（我先前启动的后台全量回归没退出）占着连接。
处置：`Stop-Process` 杀掉残留 pytest 进程后立即恢复。
⚠️ 教训：跑全量回归**别叠着开**（AGENTS.md 已记「长跑任务彼此不并发」），
且 `-n auto` 的 worker 在异常退出时会留下连接。

## 12. 体检结论（本轮全量扫描）

| 层 | 结果 |
|---|---|
| 登记表 | **178** 码（C 33 / F 145），四组 S36+B93+P31+SPLIT18 之和 = 178 ✓ |
| 路由表 | `ROUTE_PERMS` 328 条，154 个唯一码；写路由 **272/279 已登记**（7 条 auth 端点属设计内豁免） |
| 库菜单 | **195** 行（M 11 / C 39 / F 145，含 6 个 `?tab=` 页签），含码行 **178**；幽灵码 **0**，登记表有码但库无菜单 **0** |
| 种子 | **195** 行，与登记表 **178 ↔ 178 零差异**；`generate_menu_seed.py --check` **通过** |
| 前端 | 判了 64 个不同码，死码 **0** |
| 零路由码 | **14 个**，全部有归属：② `DISCRIMINATED` 2 / ⑤ `FIELD_DISCRIMINATED_DONE` 2 / ③ `SERVICE_PICKED` 6 / ④ `ENTRY_ONLY` 4；① `REMAP_PENDING` 与 `UNDECIDED` **均已清空** |
| 菜单树层级 | 层级异常 **0** |
| 权限相关测试 | **360 条 0 失败**（`-n0` 串行，含全部守卫） |

## 13. 仍保留的设计形态（不是欠账）

| 项 | 说明 |
|---|---|
| 4 个 `ENTRY_ONLY` 码 | 打开别域弹窗的入口码，只决定按钮显隐；若依里没有这种形态（它靠菜单行 `visible`），但本项目弹窗确实需要独立显隐控制 ⇒ 保留，已带理由登记 |
| 6 个 `SERVICE_PICKED` 码 | 两级任务中心共用端点，服务层按归属选码 —— 设计 69/70 的**有意形态** |
| `?tab=` 页签 `perms` 为空 | 页签是同一页的视图，复用宿主页的查看码（设计 57） |
