"""生成菜单种子 `config/initial_data/menus.seed.json`（设计 57 S1 迁移 3 的输入）。

## 为什么种子必须是**生成物**，不是手写文件

`menus.perms` 的取值范围 = `core/perms_registry.py` 的 161 条 `PermSpec`
（那是 `require_perm("...")` 与前端 `hasPermission("...")` 的**唯一真相源**）。
手写一份种子，迟早有一个字母对不上 —— 表现是「那个按钮对所有人 403」，
只有真机点到才发现（这正是设计 57 S0 为什么会多出一个 `route_perms.py`）。

⇒ 本脚本由登记表**派生**三层树（M 目录 → C 菜单 → F 按钮），并写进
`config/initial_data/menus.seed.json`；守卫
`test_seed_file_is_derived_from_the_perms_registry` 逐字段断言文件与登记表一致。

用法::

    python scripts/generate_menu_seed.py           # 写文件
    python scripts/generate_menu_seed.py --check   # 只比对，不一致 ⇒ exit 1（CI 用）

## 树的三层

| 层 | 行数 | 来源 |
|---|---|---|
| M 目录 | 12（3 个工作区根 + 4 个目录 + 5 个停用的占位平台） | 工作区/目录由本文件的 `_WORKSPACE_TREE`（**人工维护**：它是信息架构，不是权限码）；占位平台由 `_PLACEHOLDER_PLATFORMS` |
| C 菜单 | 29（登记表 28 条 C 行 + 共用 `xhs:insights:view` 的商品洞察页） | `perms_registry.PERMS` 派生 |
| F 按钮 | 133 | `perms_registry.PERMS` 派生；挂在自己所属 C 行下（借宿页按 `host_path`） |

⚠️ **`/settings`、`/tasks`、`/task-records` 三页不属于任何目录**（它们在前端是
侧栏底部 / 顶级入口），`parent_id` 直接指向 workpace 根 —— 与 `app-shell.tsx`
的 `footerNavItems` 一致。见 `_TOP_LEVEL_C` 与守卫 `test_menu_tree_matches_app_shell`。

## 平台工作区（`douyin` / `xianyu` / `kuaishou` / `weibo` / `taobao`）

`core/platforms.py` 里那 5 个是 `enabled=False` 的占位平台 ⇒ 各出一行根 M
（`status=disabled`），让「菜单管理」页能把它们作为**平台接入开关**展示
（设计 49 模块三 B-菜单-启用/停用）。种子**不给它们任何子行**：没有权限码的页面
不存在，凭空造 `douyin:xxx` 就是编权限。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: `scripts/` 在仓库根下一级 ⇒ `parents[1]`。（原先在 `.hermes/scripts/` 时是 `parents[2]`。）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.core import perms_registry as R  # noqa: E402

SEED_PATH = ROOT / "config" / "initial_data" / "menus.seed.json"

#: 工作区（= 前端「选择平台工作区」的那几张卡 + 系统管理）
WORKSPACES: tuple[tuple[str, str, str], ...] = (
    # (code, 中文名, 图标)
    ("xhs", "小红书", "xiaohongshu"),
    ("ai", "大模型网关", "api"),
    ("system", "系统管理", "setting"),
)

#: 占位平台（`core/platforms.py` 里 `enabled=False` 的那 5 个）—— 各一行 M，停用态
PLACEHOLDER_PLATFORMS: tuple[tuple[str, str], ...] = (
    ("douyin", "抖音"),
    ("kuaishou", "快手"),
    ("weibo", "微博"),
    ("xianyu", "闲鱼"),
    ("taobao", "淘宝"),
)

#: 目录层（M）—— **人工维护**：它是信息架构，不是权限码。
#: 键 = `(workspace, 目录名)`，值 = 该目录下 C 行的 path（按**侧栏顺序**）。
_WORKSPACE_TREE: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    # (workspace, M 名, 图标, 其下 C 行 path（有序）)
    ("xhs", "平台", "safety-certificate", (
        "/platforms/xhs/accounts",
        "/platforms/xhs/monitoring",
        "/platforms/xhs/auto-ops",
    )),
    ("xhs", "内容线", "tags", (
        "/platforms/xhs/discovery",
        "/platforms/xhs/my-notes",
        "/platforms/xhs/library",
        "/platforms/xhs/note-workshop",
        "/platforms/xhs/keywords",
        "/platforms/xhs/insights/content",
    )),
    ("xhs", "店铺线", "shop", (
        "/platforms/xhs/shop-items",
        "/platforms/xhs/violation-manage",
        "/platforms/xhs/picture-space",
        "/platforms/xhs/orders",
        "/platforms/xhs/insights/shop",
        "/platforms/xhs/datacenter",
    )),
    ("system", "系统管理", "setting", (
        "/system/users",
        "/system/roles",
        "/system/menus",
        "/system/dicts",
        "/system/settings",
        #: 2026-10-25 设计 69：系统级任务中心（系统任务 / 工作区任务 / 执行日志三页签）。
        #: 与工作区任务页 `/platforms/xhs/tasks` 是两个页面、两套码 —— 别合并。
        "/system/scheduler",
        "/system/logs",
    )),
)

#: 不属于任何目录的 C 行（前端侧栏底部 / 顶级入口）
_TOP_LEVEL_C: tuple[str, ...] = (
    "/platforms/xhs/dashboard",
    #: 2026-10-25 设计 69 任务中心两级重构：工作区任务页路径由 `/tasks` 迁到
    #: `/platforms/xhs/tasks`（旧 `/tasks` 重定向到系统管理的任务中心）。
    #: ⚠️ 这里必须跟着改 —— 生成器按 `PermSpec.parent_path` 反查 C 行，
    #: 路径对不上会报「/tasks 在 perms_registry 里没有 C 行」并整脚本失败。
    "/platforms/xhs/tasks",
    "/task-records",
    "/usage-dashboard",
    "/llm-gateway/providers",
    "/llm-gateway/alerts",
    "/models",
    "/settings",
    #: 个人资料（v1.1.0 新增，2026-10-24 梳理时发现漏登记）：
    #: 页面 `/profile` 早就实现了（改自己的密码 / 邮箱），但没有菜单入口 ⇒
    #: 用户只能手敲 URL。侧栏底部与「设置」并列放。
    #:
    #: ⚠️ 403 页**不进菜单**：它是错误页不是功能页，给错误页一个入口等于
    #: 让用户以为那里有东西可做。由路由直接可达即可。
    "/profile",
)

#: 页内页签（`path?tab=key` 形态的 C 行）。键 = 宿主页 path，值 = [(页签名, tab key)]。
#:
#: ⚠️ **保持为空**：页签不单独占菜单项 —— 一个页面一个菜单项，页签只在页内 `Tabs` 体现。
#: 要加页签就改页面组件，不要在这里加行（否则侧栏会长出伪页面，授权行也要各配一份）。
_TAB_CHILDREN: dict[str, tuple[tuple[str, str], ...]] = {}
