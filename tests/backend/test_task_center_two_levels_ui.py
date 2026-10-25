"""任务中心两级架构守卫（设计 69）。

## 为什么需要这些守卫

用户口径（2026-10-25）：
* 「任务中心分为系统级别和工作区域级别，工作区域为每个区域中私有」
* 「暂时不允许用户创建任务」

这两句话落地时会各自跑偏，而且跑偏的方式**都不报错**：

1. **只有系统级入口** —— 工作区级任务定义还在（`workspace="xhs"` 10 条），
   但用户在小红书工作区里找不到它们。我第一版就是这样：把所有东西塞进
   「系统管理 → 任务中心」的三个页签，页签名叫「小红书任务」，看着像做了，
   实际用户进小红书侧栏看不到任何任务入口。用户原话：「小红书中的任务中心没有了」。
2. **「不允许创建」被做成「只在前端藏按钮」** —— 用户直接构造
   `POST /api/schedule/jobs` 照样 200。

所以守卫分两组：**入口存在性**（组 1-3）与**只读强制性**（组 4-5）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "config" / "initial_data" / "menus.seed.json"
ROUTER = ROOT / "frontend" / "src" / "app" / "router.tsx"
PERMS = ROOT / "backend" / "app" / "core" / "perms_registry.py"
WS_PAGE = ROOT / "frontend" / "src" / "pages" / "tasks" / "workspace-tasks-page.tsx"


def _seed_rows() -> list[dict]:
    payload = json.loads(SEED.read_text(encoding="utf-8"))
    return payload.get("menus") or payload.get("items") or []


class TestTwoLevelEntries:
    """两级各有独立入口。"""

    def test_system_level_entry_lives_under_system_admin(self):
        """系统级任务在「系统管理」下（`/system/scheduler`）。"""
        rows = _seed_rows()
        hit = [r for r in rows if r.get("path") == "/system/scheduler"]
        assert hit, "菜单种子里没有系统级任务中心入口"
        assert hit[0].get("workspace") == "system", (
            "系统级任务中心必须归属 system 工作区"
        )

    def test_workspace_level_entry_lives_inside_its_workspace(self):
        """**工作区级任务必须在该工作区自己的菜单里**，不能只在系统管理里挂页签。

        这是用户「小红书中的任务中心没有了」的直接判据。
        """
        rows = _seed_rows()
        hit = [r for r in rows if r.get("path") == "/platforms/xhs/tasks"]
        assert hit, (
            "菜单种子里没有小红书工作区级任务入口 ⇒ 用户进小红书侧栏看不到任务"
        )
        row = hit[0]
        assert row.get("workspace") == "xhs", "该入口必须归属 xhs 工作区"
        assert row.get("perms") == "xhs:taskcenter:view", (
            "工作区级任务入口要用工作区自己的权限码，不能借用 system:scheduler:*"
        )

    def test_workspace_entry_route_is_registered(self):
        """菜单加了但路由没注册 ⇒ 点进去白屏。"""
        src = ROUTER.read_text(encoding="utf-8")
        assert "/platforms/xhs/tasks" in src, "router.tsx 没有注册工作区任务路由"
        assert "WorkspaceTasksPage" in src, "router.tsx 没引用工作区任务页组件"


class TestReadOnlyEnforcement:
    """「不允许用户创建任务」必须由后端强制，不能只靠前端不给按钮。"""

    def test_perm_registry_exposes_only_a_view_code(self):
        """注册表里**只能**有 view 码 —— 出现 manage/create 码即说明又开了口子。"""
        src = PERMS.read_text(encoding="utf-8")
        assert "xhs:taskcenter:view" in src, "工作区任务权限码没登记"
        for forbidden in (
            "xhs:taskcenter:manage",
            "xhs:taskcenter:create",
            "xhs:taskcenter:edit",
            "xhs:job:create",
        ):
            assert forbidden not in src, (
                f"注册表出现 {forbidden} ⇒ 用户侧写权限又开了，违反「暂时不允许用户创建任务」"
            )

    @pytest.mark.parametrize(
        "route",
        [
            "POST /api/schedule/jobs",
            #: 改任务只有 `/status` 这一个入口（没有裸 PATCH `/jobs/{id}`）——
            #: 别按 REST 惯例猜，猜错等于这条守卫永远抓不到真口子。
            "PATCH /api/schedule/jobs/{job_id}/status",
            "DELETE /api/schedule/jobs/{job_id}",
            "POST /api/schedule/jobs/{job_id}/run",
        ],
    )
    def test_write_endpoints_require_admin_code(self, route):
        """写端点必须挂 `system:scheduler:manage`（工作区 view 码拿不到）。"""
        from backend.app.core import route_perms  # noqa: PLC0415

        spec = route_perms.ROUTE_PERMS.get(route)
        assert spec is not None, f"{route} 没登记权限码 ⇒ 写路由守卫会拒绝启动"
        assert "system:scheduler:manage" in str(spec), (
            f"{route} 的权限码是 {spec}，不是 admin 专属 ⇒ 普通用户可写"
        )

    def test_workspace_page_renders_no_write_buttons(self):
        """页面里不许出现写操作按钮（只读页的形态约束）。"""
        src = WS_PAGE.read_text(encoding="utf-8")
        #: 剥注释：docstring 里会解释"为什么不给这些按钮"，不剥会被自己的注释命中。
        lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith(("*", "//", "/*"))]
        code = "\n".join(lines)
        for label in ("立即执行", "暂停", "恢复", "删除任务", "新建任务"):
            assert label not in code, f"只读页出现「{label}」按钮"
