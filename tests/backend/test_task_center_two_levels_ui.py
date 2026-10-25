"""任务中心两级架构守卫（设计 69）。

## 为什么需要这些守卫

用户口径（2026-10-25，最终版）：
* 「任务中心分为两级，系统级和工作区域级」
* 「系统任务在系统管理中展示操作等，工作区域任务在对应工作区内展示，
   **以及相关的操作等**，相互独立展示不集中在系统管理中」
* 「暂时不允许用户创建任务」

这几句话落地时会各自跑偏，而且跑偏的方式**都不报错**：

1. **只有系统级入口** —— 工作区级任务定义还在（`workspace="xhs"`），
   但用户在小红书工作区里找不到它们。我第一版就是这样：全塞进
   「系统管理 → 任务中心」的三个页签。用户原话：「小红书中的任务中心没有了」。
2. **把「不许创建」放大成「什么都不能做」** —— 我第二版把工作区页做成严格只读，
   连「立即执行 / 暂停」都去掉。用户纠正：「以及相关的操作等」。
   **不许新建 ≠ 不许操作已注册的任务**。
3. **「不允许创建」只在前端藏按钮** —— 用户构造 `POST /api/schedule/jobs` 照样 200。

所以守卫分三组：**入口存在性且互不混放**（组 1）、**工作区可操作**（组 2）、
**新建/删除仍归 admin**（组 3）。
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
SYSTEM_PANEL = ROOT / "frontend" / "src" / "pages" / "system" / "system-scheduler-panel.tsx"


def _seed_rows() -> list[dict]:
    payload = json.loads(SEED.read_text(encoding="utf-8"))
    return payload.get("menus") or payload.get("items") or []


def _code_only(path: Path) -> str:
    """剥掉注释行 —— 文档会解释「为什么这样做」，不剥会被自己的说明命中。"""
    return "\n".join(
        ln for ln in path.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith(("*", "//", "/*"))
    )


class TestTwoLevelEntries:
    """两级各有独立入口，且不互相混放。"""

    def test_system_level_entry_lives_under_system_admin(self):
        """系统级任务在「系统管理」下（`/system/scheduler`）。"""
        rows = _seed_rows()
        hit = [r for r in rows if r.get("path") == "/system/scheduler"]
        assert hit, "菜单种子里没有系统级任务中心入口"
        assert hit[0].get("workspace") == "system", "系统级任务中心必须归属 system 工作区"

    def test_workspace_level_entry_lives_inside_its_workspace(self):
        """**工作区级任务必须在该工作区自己的菜单里**。

        这是用户「小红书中的任务中心没有了」的直接判据。
        """
        rows = _seed_rows()
        hit = [r for r in rows if r.get("path") == "/platforms/xhs/tasks"]
        assert hit, "菜单种子里没有小红书工作区级任务入口 ⇒ 用户进小红书侧栏看不到任务"
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

    def test_system_panel_does_not_carry_workspace_tabs(self):
        """系统管理里**不许**再出现「小红书任务」页签。

        用户口径「相互独立展示不集中在系统管理中」。
        """
        code = _code_only(SYSTEM_PANEL)
        assert 'key: "xhs"' not in code, "系统管理任务中心里还挂着小红书任务页签"
        assert 'workspace="xhs"' not in code, "系统管理任务中心还在渲染 xhs 工作区的任务表"


class TestWorkspaceCanOperate:
    """工作区成员能操作**本工作区**的任务（用户：「以及相关的操作等」）。"""

    def test_registry_exposes_a_manage_code_for_the_workspace(self):
        """工作区要有自己的执行码 —— 只有 view 的话按钮点了必然 403。"""
        src = PERMS.read_text(encoding="utf-8")
        assert "xhs:taskcenter:manage" in src, (
            "没有 xhs:taskcenter:manage ⇒ 工作区成员点「立即执行」会 403"
        )

    def test_workspace_page_renders_operations(self):
        """工作区页**必须**有执行/暂停操作（第二版做成只读被用户否掉）。"""
        code = _code_only(WS_PAGE)
        for label in ("立即执行", "暂停"):
            assert label in code, f"工作区任务页缺少「{label}」操作"
        assert "runScheduleJobOnce" in code, "没有接执行请求函数"
        assert "changeScheduleJobStatus" in code, "没有接暂停/恢复请求函数"


class TestCreateStaysAdminOnly:
    """新建 / 删除仍只归系统管理（「暂时不允许用户创建任务」）。"""

    def test_no_workspace_create_or_delete_code(self):
        """不能给工作区开新建/删除的权限码。"""
        src = PERMS.read_text(encoding="utf-8")
        for forbidden in ("xhs:taskcenter:create", "xhs:taskcenter:delete", "xhs:job:create"):
            assert forbidden not in src, (
                f"注册表出现 {forbidden} ⇒ 用户侧能建/删任务，"
                "违反「暂时不允许用户创建任务」"
            )

    @pytest.mark.parametrize(
        "route",
        [
            "POST /api/schedule/jobs",
            #: 改任务只有 `/status` 这一个入口（没有裸 PATCH `/jobs/{id}`）——
            #: 别按 REST 惯例猜，猜错等于这条守卫永远抓不到真口子。
            "DELETE /api/schedule/jobs/{job_id}",
        ],
    )
    def test_create_and_delete_require_admin_code(self, route):
        """建/删必须 admin 专属（工作区码够不着）。"""
        from backend.app.core import route_perms  # noqa: PLC0415

        spec = route_perms.ROUTE_PERMS.get(route)
        assert spec is not None, f"{route} 没登记权限码 ⇒ 写路由守卫会拒绝启动"
        assert "system:scheduler:manage" in str(spec), (
            f"{route} 的权限码是 {spec}，不是 admin 专属 ⇒ 普通用户可建/删任务"
        )

    def test_workspace_page_has_no_create_or_delete(self):
        """工作区页不许出现新建/删除入口。"""
        code = _code_only(WS_PAGE)
        for label in ("新建任务", "删除任务"):
            assert label not in code, f"工作区页出现「{label}」"
        assert "createScheduleJob" not in code, "工作区页接了创建请求函数"
        assert "deleteScheduleJob" not in code, "工作区页接了删除请求函数"
