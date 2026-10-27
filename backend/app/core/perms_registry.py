"""权限码登记表 —— 161 条 `PermSpec` 的**唯一真相源**。

## 三个消费方

1. **种子生成**：`config/initial_data/menus.seed.json` 由本表生成（S1/S4 的脚本），
   不许手改种子里的 `perms` 字段 —— 手写的种子迟早与 `require_perm(...)` 里的码漂移。
2. **启动守卫**：`route_guard.assert_every_route_guarded()` 用 `ALL_PERMS` 校验
   每条写路由挂的码确实存在（写错一个字母 ⇒ 启动即崩，不是运行到那个按钮才发现）。
3. **前端码校验**：`frontend/src` 里 `hasPermission("...")` 的字面量必须全部命中本表。

## 口径（设计 49 附 A，2026-10-23 定稿）

| 组 | 条数 | 来源 |
|---|---|---|
| 系统域 | 25 | 49 附 A.1 逐码列出（user 7 / role 5 / menu 4 / dict 4 / setting 4 / log 1） |
| 业务写码 | 114 | 49 §模块九 22 页表格，逐页逐动作（`xhs:*` + `ai:*`） |
| 页级查看码 | 22 | 每个 C（页面）行一条 `<域>:<资源>:view`，只读页也不例外 |
| **合计** | **161** | |

⚠️ **系统域的 view 码不重复计入第 3 组**：`system:user:view` 等 6 条已在 25 里
（它们是 49 附 A.1 明文列出的）。第 3 组的 22 条是**业务与公共页**的 C 行码。

⚠️ **22 的来源**：49 第 0 层把小红书工作区列为「22 页挂码」，但那 22 页里
只读页（总览 / 内容洞察 / 商品洞察 / 数据中心）与混合页（笔记工坊 = 工坊 6 +
素材库 4 + 提示词 4 + 生成 3 + 自己的 `view`；大模型网关 = 服务商 6 + 预警 3）
在 §模块九 表格里**没有独立的 view 行**。⇒ 本条按「每个 C 行一条 view」补齐，
逐页列在 :data:`PAGE_VIEW_PERMS`，与前端 `app-shell.tsx` 的 C 行一一对应。
"22 页"与"22 条 view"数字相同是**巧合**，判据是「C 行 ⟺ view 码」而不是「数到 22」。

## 只增不减

`ALL_PERMS` 是编译期常量。新增一条码，admin 自动有（见 `core/perms.py`），
用户角色需要在「角色管理 → 分配菜单」里授。**删除一条码要同时删掉所有
`require_perm(...)` 引用**，否则启动守卫会拦；这是故意的。
"""

from __future__ import annotations

from dataclasses import dataclass

SEP = ":"

#: 工作区取值（与前端 `workspace` 三分一致）
WORKSPACE_SYSTEM = "system"
WORKSPACE_XHS = "xhs"
WORKSPACE_AI = "ai"
#: **不属于任何平台工作区**的行（v1.1.0 / 设计 62，2026-10-25）：个人资料等
#: 「用户级功能」—— 由壳层的头像下拉直达，不进任何工作区侧栏
#: （工作区过滤按行 workspace 精确匹配 ⇒ common 行天然从所有侧栏消失）。
WORKSPACE_COMMON = "common"


@dataclass(frozen=True)
class PermSpec:
    """一条权限码的完整登记信息。

    :param code: 权限码，形如 `system:user:create`（格式由 `core/perms.is_valid_perm` 校验）
    :param label: 按钮 / 页面的中文名，种子的 `title` 用它（导出审计时人也读它）
    :param menu_type: `"F"` = 按钮（F 行，挂在自己所属页的树里）、
        `"C"` = 页面（C 行自己的 `perms`，管「进不进得去」）
    :param parent_path: 所属 C 行的 path（`menu_type="C"` 时就是自己）。
        种子生成时靠它把 F 行挂到正确的父节点下；为空表示顶层（工作区根）。
    :param workspace: `system` / `xhs` / `ai`
    :param group: 分组标签，种子里用作 M 层（模块）行的 key。
        同一页的码共用一个 group ⇒ 生成器能一次产出 M → C → F 三层。
    :param host_path: **只有「借宿别人的页面」的按钮码才填**。例如素材库 / 提示词 /
        生成的按钮长在笔记工坊页里（设计 49 第 0 层「笔记工坊 6 + 素材库 4 +
        提示词 4 + 生成 3」），它们的 `parent_path` 是**自己的资源前缀**（`/platforms/xhs/note-workshop?tab=materials`），
        而菜单树上真正存在的 C 行是父页。留空表示「parent_path 就是页面行」。
    """

    code: str
    label: str
    menu_type: str
    parent_path: str
    workspace: str
    group: str
    host_path: str = ""

    @property
    def menu_parent_path(self) -> str:
        """种子生成时这条码该挂到哪个 C 行的 path（有 host_path 就借宿）。"""
        return self.host_path or self.parent_path


# ---------------------------------------------------------------------------
# 一、系统域 25 条（设计 49 附 A.1 逐条）
# ---------------------------------------------------------------------------
SYSTEM_PERMS: tuple[PermSpec, ...] = (
    # 用户管理 7
    PermSpec("system:user:view", "用户管理", "C", "/system/users", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:user:create", "新建用户", "F", "/system/users", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:user:edit", "编辑用户", "F", "/system/users", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:user:delete", "删除用户", "F", "/system/users", WORKSPACE_SYSTEM, "system"),
    #: 批量操作按钮（2026-10-25 用户口径「新增用户管理批量相关操作」）。
    #: 一按钮一码：批量的四个动作各一个码，不与单条码共用
    #: （有人可以单条删但不能批量删，这是有意义的授权粒度）。
    PermSpec("system:user:batchDelete", "批量删除", "F", "/system/users", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:user:batchResetPassword", "批量重置密码", "F", "/system/users", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:user:batchForceLogout", "批量下线", "F", "/system/users", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:user:batchApprove", "批量审核", "F", "/system/users", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:user:resetPassword", "重置密码", "F", "/system/users", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:user:forceLogout", "强制下线", "F", "/system/users", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:user:approve", "通过审核", "F", "/system/users", WORKSPACE_SYSTEM, "system"),
    # 角色管理 5
    PermSpec("system:role:view", "角色管理", "C", "/system/roles", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:role:create", "新建角色", "F", "/system/roles", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:role:edit", "编辑角色", "F", "/system/roles", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:role:delete", "删除角色", "F", "/system/roles", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:role:assignMenu", "分配菜单", "F", "/system/roles", WORKSPACE_SYSTEM, "system"),
    # 菜单管理 4
    PermSpec("system:menu:view", "菜单管理", "C", "/system/menus", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:menu:create", "新增菜单", "F", "/system/menus", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:menu:edit", "编辑菜单", "F", "/system/menus", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:menu:delete", "删除菜单", "F", "/system/menus", WORKSPACE_SYSTEM, "system"),
    # 字典管理 4
    PermSpec("system:dict:view", "字典管理", "C", "/system/dicts", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:dict:create", "新增字典", "F", "/system/dicts", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:dict:edit", "编辑字典", "F", "/system/dicts", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:dict:delete", "删除字典", "F", "/system/dicts", WORKSPACE_SYSTEM, "system"),
    # 配置管理 4
    PermSpec("system:setting:view", "配置管理", "C", "/system/settings", WORKSPACE_SYSTEM, "system"),

    PermSpec("system:setting:edit", "编辑配置", "F", "/system/settings", WORKSPACE_SYSTEM, "system"),

    # 日志查看 1（只有查看码，没有按钮码 —— 审计不可删、不可改）
    PermSpec("system:log:view", "日志查看", "C", "/system/logs", WORKSPACE_SYSTEM, "system"),
    # 定时任务总览（设计 64 §3.3）：跨工作区只读 + 管理动作；数据端点 admin 判角色。
    PermSpec("system:scheduler:view", "任务中心", "C", "/system/scheduler", WORKSPACE_SYSTEM, "system"),
    #: 系统级任务按钮：**每个按钮一个码**（2026-10-25 红线，颗粒度到按钮）。
    PermSpec("system:scheduler:create", "新增任务", "F", "/system/scheduler", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:scheduler:edit", "编辑任务", "F", "/system/scheduler", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:scheduler:delete", "删除任务", "F", "/system/scheduler", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:scheduler:run", "立即执行", "F", "/system/scheduler", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:scheduler:toggle", "暂停恢复", "F", "/system/scheduler", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:scheduler:deleteLogs", "删除日志", "F", "/system/scheduler", WORKSPACE_SYSTEM, "system"),
    #: 2026-10-26（G8）：`cleanTasks` 已退场 —— 它唯一的端点
    #: `POST /api/tasks/clean` 改挂 `xhs:record:clean`（队列记录清理归记录域，
    #: 与「整理/清理任务定义」是两件事，不该共用一个系统级码）。
    PermSpec("system:scheduler:runDue", "手动跑到期任务", "F", "/system/scheduler", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:scheduler:cleanLogs", "清理日志", "F", "/system/scheduler", WORKSPACE_SYSTEM, "system"),
)

# ---------------------------------------------------------------------------
# 二、业务写码 114 条（设计 49 §模块九，22 页逐动作）
# ---------------------------------------------------------------------------
_X = WORKSPACE_XHS
_A = WORKSPACE_AI


#: 必须在启动守卫生效前把「借宿」标出来（见 `PermSpec.host_path`）。
_HOST_WORKSHOP = "/platforms/xhs/note-workshop"
_HOST_SHOP_ITEMS = "/platforms/xhs/shop-items"
_HOST_KEYWORDS = "/platforms/xhs/keywords"
_HOST_GATEWAY = "/llm-gateway/providers"
_HOST_RECORDS = "/task-records"

#: (码前缀, 动作, 中文名, 所属资源前缀) → 自动补 host_path
_LODGER_HOSTS: dict[str, str] = {
    "xhs:material": _HOST_WORKSHOP,
    "ai:prompt": _HOST_WORKSHOP,
    "ai:generate": _HOST_WORKSHOP,
    "xhs:publish": _HOST_SHOP_ITEMS,
    "xhs:rule": _HOST_SHOP_ITEMS,
    "xhs:forbiddenWord": _HOST_SHOP_ITEMS,
    "xhs:topic": _HOST_KEYWORDS,
    "ai:alert": _HOST_GATEWAY,
    "xhs:record": _HOST_RECORDS,
}


def _f(prefix: str, actions: str, labels: str, path: str, ws: str) -> tuple[PermSpec, ...]:
    """按 `前缀 + 动作:/中文名/` 批量生成 F 行 —— 只在这里省打字，产物仍是 161 条常量。

    `actions` / `labels` 用 `/` 分隔，**必须等长**（少写一个中文名会在导入期
    直接 `ValueError`，而不是生成一条 label 错位的码）。
    """
    acts = [a for a in actions.split("/") if a]
    names = [n for n in labels.split("/") if n]
    if len(acts) != len(names):
        raise ValueError(f"{prefix}: 动作 {len(acts)} 个、中文名 {len(names)} 个，必须等长")
    host = _LODGER_HOSTS.get(prefix, "")
    return tuple(
        PermSpec(f"{prefix}:{a}", n, "F", path, ws, ws, host_path=host)
        for a, n in zip(acts, names)
    )


BUSINESS_PERMS: tuple[PermSpec, ...] = (
    # 账号矩阵 9
    *_f("xhs:account", "edit/delete/importCookie/relogin/probe/syncNotes/syncGoodsNotes",
        "编辑账号/删除账号/导入Cookie/重新登录/探活检测/同步笔记/同步商品笔记",
        "/platforms/xhs/accounts", _X),
    # 我的笔记 10
    *_f("xhs:note", "sync/batchSync/create/edit/delete",
        "同步笔记/批量同步/新建笔记/编辑笔记/删除笔记",
        "/platforms/xhs/my-notes", _X),
    # 内容库 5
    *_f("xhs:library", "save/delete/batchCreateDrafts/importData/syncData",
        "保存/删除/批量建草稿/导入数据/同步数据",
        "/platforms/xhs/library", _X),
    # 笔记工坊 6
    #: ⚠️ 2026-10-26：`uploadAsset` / `deleteAsset` / `saveDraft` 后端无端点
    #: （素材写走 materials 域、草稿保存是前端状态）⇒ 已删，不留空壳码。
    *_f("xhs:workshop", "generate/regenerate/adopt",
        "生成笔记/重新生成/采用版本",
        "/platforms/xhs/note-workshop", _X),
    # 商品列表 10
    #: ⚠️ 2026-10-26 用户口径「商品列表的『生成笔记』与『上架下架』拆分成三个权限码」：
    #: `operate`（上下架+删除三合一）整条退场，改由 `onShelf` / `offShelf` /
    #: `generateNote` 各判各的（三条码此前只有菜单行、零路由零按钮，属幽灵码）。
    #: `delete` 仍留给**平台删除**；删**本地台账行**是另一件事 ⇒ 新增 `deleteLocal`
    #: （原来那处借用了 `edit` 的码，等于「能改就能删」）。
    *_f("xhs:item", "edit/polish/export/onShelf/offShelf/delete/deleteLocal/sync/generateNote/generateImage/draft",
        "编辑商品/润色/导出商品/上架/下架/删除商品/删除台账行/同步商品/生成商品笔记/生成商品图/存入草稿箱",
        "/platforms/xhs/shop-items", _X),
    # 商品发布 5
    #: ⚠️ 2026-10-26：`rule`（数据包规则入口）后端无端点 —— 实际动的是
    #: `xhs:rule:*` 那组码 ⇒ 已删，不留两套码指同一件事。
    *_f("xhs:publish", "submit/retry/cancel/precheck",
        "提交发布/重试发布/取消发布/发布预检",
        "/platforms/xhs/shop-items", _X),
    # 订单管理 4
    #: ⚠️ 2026-10-27：`sync`（同步订单）与 `remark`（订单备注）随订单域全实时退场
    #: （设计 75 §4.4）：订单列表直接查平台，不再有同步任务；备注功能整条移除。
    #: 两个码在库里由迁移 `m7n8o9p0q1r2` 一并删行（不留幽灵码）。
    *_f("xhs:order", "ship/shipBatch/export",
        "发货/批量发货/导出订单",
        "/platforms/xhs/orders", _X),
    # 违规管理 1（全页唯一写操作）
    *_f("xhs:violation", "relatedDelete", "删除关联内容",
        "/platforms/xhs/violation-manage", _X),
    # 图片空间 6
    #: 2026-10-26（G8）：`permanentDelete` 已退场 —— 它与 `purge`（彻底删除）
    #: 指**同一个端点** `POST /picture-space/permanent-delete`，是重复码；
    #: 前端只判 `purge` ⇒ 留 `purge`（它有真实消费方，登记在 `SPLIT_BUTTON_PERMS`），
    #: 端点改挂 `purge`。这里**不能再列 purge**（否则重复登记）。
    *_f("xhs:picture", "upload/delete/move/resize/restore/download",
        "上传图片/删除图片/移动图片/调整尺寸/恢复图片/下载图片",
        "/platforms/xhs/picture-space", _X),
    # 监控中心 5
    *_f("xhs:monitor", "create/edit/delete/refresh/enable",
        "新建监控/编辑监控/删除监控/立即刷新/启用监控",
        "/platforms/xhs/monitoring", _X),
    # 自动运营 5
    *_f("xhs:autoops", "create/edit/delete/run/enable",
        "新建任务/编辑任务/删除任务/立即执行/启用任务",
        "/platforms/xhs/auto-ops", _X),
    # 数据抓取 1
    *_f("xhs:crawl", "submit", "提交抓取", "/platforms/xhs/discovery", _X),
    # 关键词组 3
    *_f("xhs:keyword", "create/edit/delete", "新建关键词组/编辑关键词组/删除关键词组",
        "/platforms/xhs/keywords", _X),
    # 话题 3
    *_f("xhs:topic", "create/edit/delete", "新建话题/编辑话题/删除话题",
        "/platforms/xhs/keywords", _X),
    # 数据包规则 4
    *_f("xhs:rule", "create/edit/delete/execute", "新建规则/编辑规则/删除规则/试跑规则",
        "/platforms/xhs/shop-items", _X),
    # 违规词 4
    *_f("xhs:forbiddenWord", "create/edit/delete", "新建违规词/编辑违规词/删除违规词",
        "/platforms/xhs/shop-items", _X),
    # 素材库 4
    *_f("xhs:material", "createFolder/editFolder/deleteFolder/scan",
        "新建文件夹/编辑文件夹/删除文件夹/扫描素材",
        "/platforms/xhs/note-workshop?tab=materials", _X),
    # 队列记录 4
    *_f("xhs:record", "cancel/retry/clean/cleanAll",
        "取消记录/重试记录/清理记录/一键清理",
        "/task-records", _X),
    # 大模型网关 6
    #: 2026-10-26（G7）：`delete` 已退场 —— 用户口径「移除删除功能」（v1.9），
    #: 端点 `DELETE /llm/gateway-configs/{config_id}` 是 410 墓碑（`include_in_schema=False`），
    #: 前端 `deleteGatewayConfig` 已删、页面无判权点 ⇒ 留码就是空壳（若依纪律一）。
    *_f("ai:gateway", "create/edit/test/syncModels/enableModel",
        "新建服务商/编辑服务商/连通性测试/同步模型/启用模型",
        "/llm-gateway/providers", _A),
    # 模型配置 4
    *_f("ai:model", "create/edit/delete/setBusiness",
        "新建模型/编辑模型/删除模型/设置业务点",
        "/models", _A),
    # 提示词 3 + 生成 3
    *_f("ai:prompt", "create/edit/delete", "新建提示词/编辑提示词/删除提示词",
        "/platforms/xhs/note-workshop?tab=prompts", _A),
    *_f("ai:generate", "generate", "生成",
        "/platforms/xhs/note-workshop?tab=prompts", _A),
)

# ---------------------------------------------------------------------------
# 三、页级查看码 31 条（24 条 C 行 + 7 条**随页面登记**的按钮码）
# ---------------------------------------------------------------------------
#: ⚠️ 这 22 条**不含**系统域的 6 条 view（已在 `SYSTEM_PERMS` 里，49 附 A.1 明文列出）。
#: 判据 = 前端 `app-shell.tsx` 的 C 行 ⟺ 本表（`test_perms_core.py` 比对。
PAGE_VIEW_PERMS: tuple[PermSpec, ...] = (
    # 小红书工作区 16
    PermSpec("xhs:dashboard:view", "总览", "C", "/platforms/xhs/dashboard", _X, _X),
    PermSpec("xhs:account:view", "账号矩阵", "C", "/platforms/xhs/accounts", _X, _X),
    PermSpec("xhs:monitor:view", "监控中心", "C", "/platforms/xhs/monitoring", _X, _X),
    PermSpec("xhs:autoops:view", "自动运营", "C", "/platforms/xhs/auto-ops", _X, _X),
    PermSpec("xhs:crawl:view", "笔记发现", "C", "/platforms/xhs/discovery", _X, _X),
    PermSpec("xhs:note:view", "我的笔记", "C", "/platforms/xhs/my-notes", _X, _X),
    PermSpec("xhs:library:view", "内容库", "C", "/platforms/xhs/library", _X, _X),
    #: 内容库的写操作（2026-10-25 红线「一按钮一码」）：
    #: 发布 / 同步 / 导出 / 建草稿四类动作互相独立（能看能导，不等于能往平台发）。
    *_f("xhs:library", "export", "导出笔记", "/platforms/xhs/library", "xhs"),
    PermSpec("xhs:workshop:view", "笔记工坊", "C", "/platforms/xhs/note-workshop", _X, _X),
    #: 工坊的 6 个 Tab（2026-10-26 用户口径「菜单管理中有很多缺失」）：
    #: 它们此前只是**同一个 URL 的 query 参数**、零权限码 ⇒ 授权页勾不了、
    #: 后端也拦不住。按「一个功能区一个码」补齐，前端 Tab 与后端路由两侧都判。
    #: 商品洞察与内容洞察是**两个页面**（两个路由），此前只有 insights:view 一个码
    #: 且只挂了评论洞察一条路由 ⇒ 页面级码拦不住任何东西。
    PermSpec("xhs:insights:shop", "商品洞察", "C", "/platforms/xhs/insights/shop", _X, _X),
    PermSpec("xhs:keyword:view", "关键词组", "C", "/platforms/xhs/keywords", _X, _X),
    PermSpec("xhs:insights:view", "内容洞察 / 商品洞察", "C", "/platforms/xhs/insights/content", _X, _X),
    PermSpec("xhs:item:view", "商品列表", "C", "/platforms/xhs/shop-items", _X, _X),
    PermSpec("xhs:violation:view", "违规管理", "C", "/platforms/xhs/violation-manage", _X, _X),
    PermSpec("xhs:picture:view", "图片空间", "C", "/platforms/xhs/picture-space", _X, _X),
    PermSpec("xhs:order:view", "订单管理", "C", "/platforms/xhs/orders", _X, _X),
    PermSpec("xhs:datacenter:view", "数据中心", "C", "/platforms/xhs/datacenter", _X, _X),
    PermSpec("xhs:settings:view", "设置", "C", "/settings", _X, _X),
    #: ⚠️ 2026-10-26：`xhs:settings:edit` **不能删** —— 它在
    #: `api/settings.py:119` 是真实判权点（工作区级配置的写权限按
    #: `f"{workspace}:settings:edit"` 拼接判断）。它没有登记路由，是因为
    #: 配置写端点本身是 `EXEMPT_SELF`（每用户自己的配置不许挂码），
    #: 工作区级那一层用 `has_perm` 在函数内判 —— 属「有判权点但不占路由」。
    PermSpec("xhs:settings:edit", "工作区配置编辑", "F", "/settings", _X, _X),
    PermSpec("ai:usage:view", "用量看板", "C", "/usage-dashboard", _A, _A),
    PermSpec("ai:gateway:view", "服务商", "C", "/llm-gateway/providers", _A, _A),
    PermSpec("ai:model:view", "模型配置", "C", "/models", _A, _A),
    # 任务中心 / 队列记录 / 个人资料 3
    PermSpec("xhs:record:view", "队列记录", "C", "/task-records", _X, _X),
    #: 工作区级任务中心（设计 69，2026-10-25 用户口径「任务中心分为两级，
    #: 系统级和工作区域级」「工作区域任务在对应工作区内展示，**以及相关的操作等**」）。
    #:
    #: `view` = 看本工作区任务与日志；`manage` = 立即执行 / 暂停恢复。
    #: ⚠️ 两级的边界是**任务归属**不是**操作能力**：工作区任务可以由工作区成员
    #: 操作（只作用于本工作区数据），但**不能新建**（那是系统注册表的事，
    #: 创建端点只接受已注册的 job_key）。
    PermSpec("xhs:taskcenter:view", "任务中心", "C", "/platforms/xhs/tasks", _X, _X),
    #: 工作区级任务按钮：**每个按钮一个码**（2026-10-25 红线「所有按钮的权限必须独立，
    #: 不允许合并使用同一个权限码，颗粒度达到按钮级别」）。
    #: 2026-10-25 用户口径「任务中心页面没有新增和编辑入口」+「入口及权限补全」
    #: ⇒ 新增 / 编辑各一个独立按钮码（红线：一按钮一码，不共用）。
    PermSpec("xhs:taskcenter:create", "新增任务", "F", "/platforms/xhs/tasks", _X, _X),
    PermSpec("xhs:taskcenter:edit", "编辑任务", "F", "/platforms/xhs/tasks", _X, _X),
    PermSpec("xhs:taskcenter:run", "立即执行", "F", "/platforms/xhs/tasks", _X, _X),
    PermSpec("xhs:taskcenter:toggle", "暂停恢复", "F", "/platforms/xhs/tasks", _X, _X),
    PermSpec("xhs:taskcenter:viewLogs", "查看日志", "F", "/platforms/xhs/tasks", _X, _X),
    #: ⚠️ **v1.1.0 梳理补登（2026-10-24）**：原注释写「个人资料不挂码，故不计」，
    #: 那是把两件事混了 ——
    #:   * 「**端点**不挂码」是对的（每个人都要能改自己的密码，挂码会把人锁死，
    #:     见 `GET/POST /auth/*` 的 EXEMPT 说明）；
    #:   * 「**页面菜单**不挂码」是错的：v1.1.0 侧栏由 `menus` 表下发，而菜单行
    #:     必须带内码才能被 `role_menus` 授权、才能出现在侧栏。没有码 ⇒ 这个页面
    #:     在侧栏里**根本不存在**，用户只能手敲 `/profile`（实测：改了密码都没入口）。
    #: ⇒ 补一条页面级 view 码；它**不保护端点**，只决定「侧栏里有没有这个入口」。
    # workspace=common（设计 62）：个人资料是用户级功能，入口在头像下拉，
    # 不进任何平台侧栏 —— 工作区过滤（menus/mine?workspace=）天然排除 common 行。
    PermSpec("xhs:profile:view", "个人资料", "C", "/profile", WORKSPACE_COMMON, _X),
    # ⚠️ 第 22 条：预警规则页（大模型网关工作区，`/llm-gateway/alerts`）
    PermSpec("ai:alert:view", "预警规则", "C", "/llm-gateway/alerts", _A, _A),
)

#: 全量登记表。冒号分隔三元组，顺序 = 系统 → 业务写码 → 页级 view。
#: 2026-10-25 红线「一按钮一码」：从 `xhs:item:view`（曾一个码盖 29 条路由）
#: 从过粗的按钮码（曾用 `xhs:item:edit` 盖 23 条，还混着登录会话与提示词模板）里拆出来的按钮码。
#: 单独一个元组、追加进 `PERMS` —— 不改动既有三个元组，避免误伤。
SPLIT_BUTTON_PERMS: tuple[PermSpec, ...] = (
    # 商品
    PermSpec("xhs:item:create", "新增商品", "F", "/platforms/xhs/shop-items", _X, _X),
    #: 2026-10-26 用户口径「商品列表的『生成笔记』与『上架下架』拆分，应拆分成三个权限码」：
    #: 上架 / 下架 / 生成笔记**各一码**（原 `xhs:item:operate` 一个码盖三个动作，
    #: 授权页只能整块给 —— 想让人上下架就得连平台删除一起给）。
    #: `onShelf` / `offShelf` / `generateNote` 三条码在 `BUSINESS_PERMS` 里已登记，
    #: 此前只有菜单行、**没有任何路由与按钮真的判它们**（幽灵码）；本轮落实。
    #: ⚠️ 三者共用同一个端点 `POST /items/operate`（平台只给一个批量动作接口）⇒
    #: 路由表挂 `onShelf` 作静态声明，端点内按 `action` 选码再 `has_perm`。
    #: 平台删除走 `action="delete"` ⇒ 归 `xhs:item:delete`（不是 operate）。
    #: 商品列表页的独立入口（2026-10-25 红线「一按钮一码」）：
    #: 导出是**只读但会拉全量**、违禁词库是配置、草稿/发布是生成链路入口 ——
    #: 与商品的读写/上下架都不是同一个动作。
    PermSpec("xhs:item:forbiddenWords", "违禁词库", "F", "/platforms/xhs/shop-items", _X, _X),
    PermSpec("xhs:item:drafts", "查看商品草稿", "F", "/platforms/xhs/shop-items", _X, _X),
    #: ⚠️ 2026-10-26：`xhs:draft:*` 四条码**整条删除**。
    #: 它们此前只有菜单行，而前端 `hasPermission` 判的是这些码、后端路由挂的却是
    #: **另一套**（素材增删 → `xhs:note:edit`；AI 改写 / 生图 → `ai:generate:generate`；
    #: LLM 生成笔记 → `xhs:item:generateNote`）。前端另立一套码 ⇒ 授权页勾了
    #: `xhs:draft:generate` 也不会让后端放行（反之亦然）—— 两边对不上，等于没有判权。
    #: 若依的纪律是**同一个字符串只写一处**：前端判权码必须等于后端路由码。
    #: ⇒ 前端 12 处 `hasPermission` 改成后端真码，本组码退场（不留兼容层）。
    PermSpec("xhs:item:publish", "发布商品笔记", "F", "/platforms/xhs/shop-items", _X, _X),
    PermSpec("xhs:item:bind", "绑定千帆", "F", "/platforms/xhs/shop-items", _X, _X),
    PermSpec("xhs:item:healthCheck", "店铺探活", "F", "/platforms/xhs/shop-items", _X, _X),
    PermSpec("xhs:item:aiAssist", "AI 商品文案", "F", "/platforms/xhs/shop-items", _X, _X),
    # 图片空间
    PermSpec("xhs:picture:uploadBatch", "批量上传", "F", "/platforms/xhs/picture-space", _X, _X),
    PermSpec("xhs:picture:compose", "合成图片", "F", "/platforms/xhs/picture-space", _X, _X),
    #: 图片空间的写操作（2026-10-25 红线「一按钮一码」，按动作分开）：
    #: 删除/彻底删除是一对（回收站前 vs 后），移动与新建目录、复制链接各不相干。
    PermSpec("xhs:picture:purge", "彻底删除", "F", "/platforms/xhs/picture-space", _X, _X),
    PermSpec("xhs:picture:folder", "管理图片目录", "F", "/platforms/xhs/picture-space", _X, _X),
    PermSpec("xhs:files:view", "文件列表", "C", "/platforms/xhs/picture-space", _X, _X),
    PermSpec("xhs:files:upload", "上传文件", "F", "/platforms/xhs/picture-space", _X, _X),
    PermSpec("xhs:files:delete", "删除文件", "F", "/platforms/xhs/picture-space", _X, _X),
    # 登录会话（原先错挂在「商品编辑」下）
    PermSpec("xhs:login:view", "登录记录", "C", "/platforms/xhs/accounts", _X, _X),
    PermSpec("xhs:login:creator", "创作者登录", "F", "/platforms/xhs/accounts", _X, _X),
    PermSpec("xhs:login:pc", "网页版登录", "F", "/platforms/xhs/accounts", _X, _X),
    PermSpec("xhs:login:qianfan", "千帆登录", "F", "/platforms/xhs/accounts", _X, _X),
    # 提示词（原先也错挂在「商品编辑」下）
)

PERMS: tuple[PermSpec, ...] = (
    SYSTEM_PERMS + BUSINESS_PERMS + PAGE_VIEW_PERMS + SPLIT_BUTTON_PERMS
)



#: 三组条数的**登记口径**（不要在测试里再写一遍字面量 —— 漂移时只改这一处会显得
#: 「测试跟着代码改」，所以测试断言的是 161 这个**需求值**，这里只是分组自检用）。
GROUP_COUNTS: dict[str, int] = {
    "system": len(SYSTEM_PERMS),
    "business": len(BUSINESS_PERMS),
    "page_view": len(PAGE_VIEW_PERMS),
}

#: 权限码全集（frozenset：`core/perms.ALL_PERMS` 的直接来源）
CODES: frozenset[str] = frozenset(spec.code for spec in PERMS)

#: `code -> spec`（启动守卫报错时要说清是哪个按钮 / 哪一页）
BY_CODE: dict[str, PermSpec] = {spec.code: spec for spec in PERMS}