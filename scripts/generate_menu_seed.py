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

#: 本文件在仓库根下一级 `scripts/` ⇒ 仓库根 = `parents[1]`。
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

#: **页面展示元数据**（icon + 侧栏排序号）—— **人工维护**，不是权限信息。
#: 登记表只回答「这个页面需要什么权限码」，回答不了「它在侧栏长什么样、排第几」；
#: 这两件事本来就是信息架构，与 `_WORKSPACE_TREE` 同类。新增页面时**必须**补一行，
#: 否则生成器给 `icon=""` + 追加序号（不报错，但侧栏会少图标、位置可能靠后）。
#: 少数页面的**菜单名**与登记表 `PermSpec.label` 不同：登记表名字要同时覆盖该码下
#: 的所有页面（`xhs:insights:view` 管内容洞察 + 商品洞察），而菜单行只代表一个页面。
_MENU_NAME_OVERRIDE: dict[str, str] = {
    "/platforms/xhs/insights/content": "内容洞察",
}

_PAGE_META: dict[str, tuple[str, int]] = {
    "/platforms/xhs/dashboard": ("dashboard", -10),
    "/platforms/xhs/accounts": ("safety-certificate", 0),
    "/platforms/xhs/monitoring": ("aim", 1),
    "/platforms/xhs/auto-ops": ("thunderbolt", 2),
    "/platforms/xhs/discovery": ("search", 0),
    "/platforms/xhs/my-notes": ("profile", 1),
    "/platforms/xhs/library": ("database", 2),
    "/platforms/xhs/note-workshop": ("tool", 3),
    "/platforms/xhs/keywords": ("key", 4),
    "/platforms/xhs/insights/content": ("bar-chart", 5),
    "/platforms/xhs/shop-items": ("shopping", 0),
    "/platforms/xhs/violation-manage": ("warning", 1),
    "/platforms/xhs/picture-space": ("picture", 2),
    "/platforms/xhs/orders": ("shopping-cart", 3),
    "/platforms/xhs/insights/shop": ("line-chart", 4),
    "/platforms/xhs/datacenter": ("database", 5),
    "/system/users": ("team", 0),
    "/system/roles": ("safety-certificate", 1),
    "/system/menus": ("menu", 2),
    "/system/dicts": ("book", 3),
    "/system/settings": ("appstore", 4),
    "/system/scheduler": ("appstore", 5),
    "/system/logs": ("file-text", 6),
    "/platforms/xhs/tasks": ("appstore", 101),
    "/task-records": ("history", 102),
    "/usage-dashboard": ("fund", 103),
    "/llm-gateway/providers": ("api", 104),
    "/llm-gateway/alerts": ("alert", 105),
    "/models": ("robot", 106),
    "/settings": ("setting", 107),
    "/profile": ("user", 108),
}

#: 页内页签（`path?tab=key` 形态的 C 行）。键 = 宿主页 path，值 = [(页签名, tab key)]。
#:
#: ⚠️ **保持为空**：页签不单独占菜单项 —— 一个页面一个菜单项，页签只在页内 `Tabs` 体现。
#: 要加页签就改页面组件，不要在这里加行（否则侧栏会长出伪页面，授权行也要各配一份）。
_TAB_CHILDREN: dict[str, tuple[tuple[str, str], ...]] = {}


# --------------------------------------------------------------------------- #
# 构建
#
# 种子结构（顺序即前端渲染顺序，**不要打乱**）：
#   ① M 行：3 个工作区根 + 5 个停用占位平台 + 目录层（`_WORKSPACE_TREE`）
#   ② 一段「C 行 + 紧跟它的 F 子行」的重复块（`sort_order` 按父级内序号从 0 起）
#
# ⚠️ M 行的 `path` 是**空串**、`parent_seq` 工作区根为 0 —— M 行是分组标题，
#    不指向页面；前端靠 `menu_type` 判层级。
# --------------------------------------------------------------------------- #
def _build_menus() -> list[dict]:
    """按**侧栏渲染顺序**生成：根 M → 占位平台 → (目录 M → 其 C 行 → 各 C 的 F 子行)*。

    ⚠️ 顺序不是「所有 M 在前、所有 C 在后」：种子是**深度优先**的 —— 目录 M 行
    紧跟它自己的 C/F 子树。照抄「M 全在前」会让「内容线 / 店铺线」跑到所有页面
    之前（实测 171 行错位）。
    """
    rows: list[dict] = []
    root_seq: dict[str, int] = {}

    # ① 工作区根（`workspace` 字段就是它，供 C 行挂父级）
    for code, name, icon in WORKSPACES:
        rows.append({
            "_seq": len(rows) + 1, "name": name, "menu_type": "M", "path": "",
            "perms": "", "icon": icon, "workspace": code, "sort_order": 0,
            "is_visible": True, "status": "normal", "parent_seq": 0,
        })
        root_seq[code] = rows[-1]["_seq"]

    # ② 停用占位平台（`enabled=False`，icon 就是它的 code）
    for code, name in PLACEHOLDER_PLATFORMS:
        rows.append({
            "_seq": len(rows) + 1, "name": name, "menu_type": "M", "path": "",
            "perms": "", "icon": code, "workspace": code, "sort_order": 0,
            "is_visible": True, "status": "disabled", "parent_seq": 0,
        })

    c_by_path: dict[str, list] = {}
    for spec in R.PERMS:
        if spec.menu_type == "C":
            c_by_path.setdefault(spec.parent_path, []).append(spec)
    f_by_path: dict[str, list] = {}
    for spec in R.PERMS:
        if spec.menu_type == "F":
            f_by_path.setdefault(spec.menu_parent_path, []).append(spec)

    emitted_f: set[str] = set()

    def emit_pages(page_paths, m_seq: int, dir_icon: str = "") -> None:
        """在一个 M 行下依次输出它的 C 行与各自的 F 子行。

        同一 path 有多条 C 行时（`/accounts` → 账号矩阵 + 登录记录），它们**连续输出**，
        而 F 子行挂在该 path **第一条** C 行下并紧跟其后。
        """
        for path in page_paths:
            specs = c_by_path.get(path)
            if not specs:
                continue
            page_icon, page_sort = _PAGE_META.get(path, (dir_icon, 1000))
            first_c_seq = 0
            for spec in specs:
                rows.append({
                    "_seq": len(rows) + 1, "name": _MENU_NAME_OVERRIDE.get(path, spec.label), "menu_type": "C",
                    "path": path, "perms": spec.code, "icon": page_icon,
                    "workspace": spec.workspace or _workspace_of(path),
                    "sort_order": page_sort, "is_visible": True, "status": "normal",
                    "parent_seq": m_seq,
                })
                if not first_c_seq:
                    first_c_seq = rows[-1]["_seq"]
                else:
                    #: 同 path 的第二条及以后 C 行（例：`/accounts` 的「登录记录」）
                    #: 排在该页主行之后：主行序号 + 100。
                    rows[-1]["sort_order"] = page_sort + 100
            # F 子行挂「该 path 第一条 C 行」，并且插在**所有** C 行之后
            if path not in emitted_f:
                for f_order, fspec in enumerate(f_by_path.get(path, [])):
                    rows.append({
                        "_seq": len(rows) + 1, "name": fspec.label,
                        "menu_type": "F", "path": "", "perms": fspec.code,
                        "icon": "", "workspace": "", "sort_order": f_order,
                        "is_visible": True, "status": "normal",
                        "parent_seq": first_c_seq,
                    })
                emitted_f.add(path)

    # ③ 目录树：每个非根目录一行 M + 它的页面子树
    for workspace, dir_name, icon, paths in _WORKSPACE_TREE:
        if (workspace, dir_name) in {(c, n) for c, n, _i in WORKSPACES}:
            # 这是工作区根自身（如 system）：只输出它的页面，不额外发 M 行
            emit_pages(paths, root_seq[workspace], icon)
            continue
        rows.append({
            "_seq": len(rows) + 1, "name": dir_name, "menu_type": "M", "path": "",
            "perms": "", "icon": icon, "workspace": workspace, "sort_order": 0,
            "is_visible": True, "status": "normal", "parent_seq": root_seq[workspace],
        })
        emit_pages(paths, rows[-1]["_seq"], icon)

    # ④ 不属于任何目录的顶级页（侧栏底部 / 独立入口）：挂各自工作区根
    _root_icon = {code: icon for code, _n, icon in WORKSPACES}
    for _p in [p for p in _TOP_LEVEL_C if p not in emitted_f]:
        emit_pages([_p], root_seq[_workspace_of(_p)], _root_icon.get(_workspace_of(_p), "")) 
    return rows


def _workspace_of(path: str) -> str:
    for code, _name, _icon in WORKSPACES:
        if path.startswith(f"/{code}"):
            return code
    return "system"


def build() -> dict:
    """种子整体（含 `counts` 声明 —— 守卫按它比对实际行数）。"""
    rows = _build_menus()
    from collections import Counter

    counts = Counter(r["menu_type"] for r in rows)
    return {
        "_comment": "由 scripts/generate_menu_seed.py 生成，勿手改；改登记表后重新生成。",
        "version": 1,
        "counts": {
            "M": counts["M"], "C": counts["C"], "F": counts["F"], "total": len(rows)
        },
        "menus": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成菜单种子")
    parser.add_argument("--check", action="store_true", help="只比对，不一致则 exit 1")
    args = parser.parse_args()

    payload_obj = build()
    payload = json.dumps(payload_obj, ensure_ascii=False, indent=2) + chr(10)
    if args.check:
        current = SEED_PATH.read_text(encoding="utf-8") if SEED_PATH.exists() else ""
        if current != payload:
            print("FAIL: 种子与登记表不一致，跑一次不带 --check 重新生成", file=sys.stderr)
            return 1
        print("OK:", payload_obj["counts"])
        return 0
    SEED_PATH.write_text(payload, encoding="utf-8")
    print("written:", payload_obj["counts"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
