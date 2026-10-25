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
    PermSpec("system:setting:create", "新增配置", "F", "/system/settings", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:setting:edit", "编辑配置", "F", "/system/settings", WORKSPACE_SYSTEM, "system"),
    PermSpec("system:setting:delete", "删除配置", "F", "/system/settings", WORKSPACE_SYSTEM, "system"),
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
    PermSpec("system:scheduler:cleanTasks", "清理任务记录", "F", "/system/scheduler", WORKSPACE_SYSTEM, "system"),
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
    *_f("xhs:account", "create/edit/delete/importCookie/relogin/probe/syncNotes/syncGoodsNotes/syncShopGoods",
        "添加账号/编辑账号/删除账号/导入Cookie/重新登录/探活检测/同步笔记/同步商品笔记/同步店铺商品",
        "/platforms/xhs/accounts", _X),
    # 我的笔记 10
    *_f("xhs:note", "sync/batchSync/create/edit/delete/batchDelete/publish/batchPublish/tag/export",
        "同步笔记/批量同步/新建笔记/编辑笔记/删除笔记/批量删除/发布笔记/批量发布/打标签/导出笔记",
        "/platforms/xhs/my-notes", _X),
    # 内容库 5
    *_f("xhs:library", "save/delete/batchCreateDrafts/importData/syncData",
        "保存/删除/批量建草稿/导入数据/同步数据",
        "/platforms/xhs/library", _X),
    # 笔记工坊 6
    *_f("xhs:workshop", "generate/regenerate/adopt/uploadAsset/deleteAsset/saveDraft",
        "生成笔记/重新生成/采用版本/上传素材/删除素材/保存草稿",
        "/platforms/xhs/note-workshop", _X),
    # 商品列表 10
    *_f("xhs:item", "edit/polish/export/onShelf/offShelf/delete/sync/generateNote/generateImage/draft",
        "编辑商品/润色/导出商品/上架/下架/删除商品/同步商品/生成商品笔记/生成商品图/存入草稿箱",
        "/platforms/xhs/shop-items", _X),
    # 商品发布 5
    *_f("xhs:publish", "submit/retry/cancel/precheck/rule",
        "提交发布/重试发布/取消发布/发布预检/数据包规则",
        "/platforms/xhs/shop-items", _X),
    # 订单管理 6
    *_f("xhs:order", "sync/ship/shipBatch/remark/export/afterSaleHandle",
        "同步订单/发货/批量发货/订单备注/导出订单/售后处理",
        "/platforms/xhs/orders", _X),
    # 违规管理 1（全页唯一写操作）
    *_f("xhs:violation", "relatedDelete", "删除关联内容",
        "/platforms/xhs/violation-manage", _X),
    # 图片空间 7（`download` 归查看码，见 49 §模块九 旁注）
    *_f("xhs:picture", "upload/delete/move/resize/restore/permanentDelete/download",
        "上传图片/删除图片/移动图片/调整尺寸/恢复图片/永久删除/下载图片",
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
    *_f("xhs:forbiddenWord", "create/edit/delete/import", "新建违规词/编辑违规词/删除违规词/批量导入",
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
    *_f("ai:gateway", "create/edit/delete/test/syncModels/enableModel",
        "新建服务商/编辑服务商/删除服务商/连通性测试/同步模型/启用模型",
        "/llm-gateway/providers", _A),
    # 模型配置 4
    *_f("ai:model", "create/edit/delete/setBusiness",
        "新建模型/编辑模型/删除模型/设置业务点",
        "/models", _A),
    # 提示词 3 + 生成 3
    *_f("ai:prompt", "create/edit/delete", "新建提示词/编辑提示词/删除提示词",
        "/platforms/xhs/note-workshop?tab=prompts", _A),
    *_f("ai:generate", "generate/batchGenerate/retry", "生成/批量生成/重试生成",
        "/platforms/xhs/note-workshop?tab=prompts", _A),
)

# ---------------------------------------------------------------------------
# 三、页级查看码 22 条（每个 C 行一条）
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
    PermSpec("xhs:workshop:view", "笔记工坊", "C", "/platforms/xhs/note-workshop", _X, _X),
    PermSpec("xhs:keyword:view", "关键词组", "C", "/platforms/xhs/keywords", _X, _X),
    PermSpec("xhs:insights:view", "内容洞察 / 商品洞察", "C", "/platforms/xhs/insights/content", _X, _X),
    PermSpec("xhs:item:view", "商品列表", "C", "/platforms/xhs/shop-items", _X, _X),
    PermSpec("xhs:violation:view", "违规管理", "C", "/platforms/xhs/violation-manage", _X, _X),
    PermSpec("xhs:picture:view", "图片空间", "C", "/platforms/xhs/picture-space", _X, _X),
    PermSpec("xhs:order:view", "订单管理", "C", "/platforms/xhs/orders", _X, _X),
    PermSpec("xhs:datacenter:view", "数据中心", "C", "/platforms/xhs/datacenter", _X, _X),
    PermSpec("xhs:settings:view", "设置", "C", "/settings", _X, _X),
    PermSpec("xhs:settings:edit", "工作区配置编辑", "F", "/settings", _X, _X),
    # 大模型网关工作区 3
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
#: 与 `xhs:item:edit`（盖 23 条，还混着登录会话与提示词模板）里拆出来的按钮码。
#: 单独一个元组、追加进 `PERMS` —— 不改动既有三个元组，避免误伤。
SPLIT_BUTTON_PERMS: tuple[PermSpec, ...] = (
    # 商品
    PermSpec("xhs:item:create", "新增商品", "F", "/platforms/xhs/shop-items", _X, _X),
    PermSpec("xhs:item:operate", "上下架", "F", "/platforms/xhs/shop-items", _X, _X),
    #: 商品列表页的四个独立入口（2026-10-25 红线「一按钮一码」）：
    #: 导出是**只读但会拉全量**、违禁词库是配置、草稿/发布是生成链路入口 ——
    #: 与商品的读写/上下架都不是同一个动作。
    PermSpec("xhs:item:forbiddenWords", "违禁词库", "F", "/platforms/xhs/shop-items", _X, _X),
    PermSpec("xhs:item:drafts", "查看商品草稿", "F", "/platforms/xhs/shop-items", _X, _X),
    PermSpec("xhs:item:publish", "发布商品笔记", "F", "/platforms/xhs/shop-items", _X, _X),
    PermSpec("xhs:item:bind", "绑定千帆", "F", "/platforms/xhs/shop-items", _X, _X),
    PermSpec("xhs:item:healthCheck", "店铺探活", "F", "/platforms/xhs/shop-items", _X, _X),
    PermSpec("xhs:item:aiAssist", "AI 商品文案", "F", "/platforms/xhs/shop-items", _X, _X),
    # 图片空间
    PermSpec("xhs:picture:uploadBatch", "批量上传", "F", "/platforms/xhs/picture-space", _X, _X),
    PermSpec("xhs:picture:compose", "合成图片", "F", "/platforms/xhs/picture-space", _X, _X),
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
