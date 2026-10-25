"""入口与权限补全守卫（2026-10-25 用户口径）。

## 红线原文

> 「注意如果用户没有对应的权限，则目录菜单或页面，或按钮，则应不展示，
>   有对应的权限才展示，**菜单动态，页面按钮也是动态**」
> 「任务中心页面没有新增和编辑入口，点击执行没有每次需要修改的任务参数」
> 「入口及权限补全」

## 三层判据

| 层 | 判据 | 谁来保证 |
|---|---|---|
| 菜单 | 无权限的菜单项不出现 | 后端 `/system/menus/mine` 按权限过滤（已有）|
| 页面 | 无 `view` 码进不去 | 路由守卫 + `READONLY_ROUTES`（已有）|
| **按钮** | 无按钮码 ⇒ **不渲染** | 前端 `hasPermission(...) &&  <Button>` 包住（本守卫）|

## 为什么按钮这层必须用守卫

后端 403 挡住的是「点了之后」，用户看到的是一个能点、点了才报错的按钮
—— 那是「半权限」：既没保护体验，又暴露了功能存在。
用户口径要的是**不展示**，所以判据落在源码结构上：每个 `onClick` 打写接口的
按钮，源码里必须有 `hasPermission("<自己的码>")` 在前。

⚠️ 判据是「同一文件内有 `hasPermission(...)` 且包含该按钮文案」，
不是精确 AST 绑定 —— 精确绑定对 TSX 太脆，而过半的退化（整块删掉判断）
仍然抓得住。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WS_PAGE = ROOT / "frontend" / "src" / "pages" / "tasks" / "workspace-tasks-page.tsx"
SYS_PAGE = ROOT / "frontend" / "src" / "pages" / "system" / "system-scheduler-panel.tsx"
PERMS = ROOT / "backend" / "app" / "core" / "perms_registry.py"
ROUTE_PERMS = ROOT / "backend" / "app" / "core" / "route_perms.py"
MENU_SEED = ROOT / "config" / "initial_data" / "menus.seed.json"


def _code(path: Path) -> str:
    """剥注释后的源码（docstring 会解释判据本身，不剥会自命中）。"""
    return "\n".join(
        ln
        for ln in path.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith(("*", "//", "/*"))
    )


class TestEveryButtonHasItsOwnCode:
    """每个写操作按钮都要有自己的权限码，且前端按它判断。"""

    #: (页面, 按钮文案, 权限码)
    BUTTONS = [
        (WS_PAGE, "新增任务", "xhs:taskcenter:create"),
        (WS_PAGE, "编辑", "xhs:taskcenter:edit"),
        (WS_PAGE, "立即执行", "xhs:taskcenter:run"),
        (WS_PAGE, "暂停", "xhs:taskcenter:toggle"),
        (WS_PAGE, "日志", "xhs:taskcenter:viewLogs"),
        (SYS_PAGE, "新增任务", "system:scheduler:create"),
        (SYS_PAGE, "编辑", "system:scheduler:edit"),
        (SYS_PAGE, "立即执行", "system:scheduler:run"),
        (SYS_PAGE, "暂停", "system:scheduler:toggle"),
    ]

    def test_button_codes_are_registered(self):
        src = PERMS.read_text(encoding="utf-8")
        for _, _, code in self.BUTTONS:
            assert f'"{code}"' in src, f"按钮码 {code} 没登记"

    def test_buttons_are_gated_by_their_own_code(self):
        """无权限 ⇒ 不渲染：源码里必须出现 `hasPermission("<码>")` 的守卫。"""
        for page, label, code in self.BUTTONS:
            code_src = _code(page)
            assert label in code_src, f"{page.name} 里没有「{label}」按钮"
            assert f'hasPermission("{code}")' in code_src, (
                f"{page.name} 的「{label}」按钮没有用 hasPermission(\"{code}\") 守卫 "
                "⇒ 无权限用户也会看到它（点了才 403）"
            )

    def test_buttons_are_conditionally_rendered_not_just_disabled(self):
        """必须是**条件渲染**（`{hasPermission(...) && ...}`），不是 `disabled`。

        用户口径「没有对应权限则**不展示**」—— `disabled` 仍然是展示。
        """
        for page, label, code in self.BUTTONS:
            code_src = _code(page)
            pattern = re.compile(
                rf'hasPermission\("{re.escape(code)}"\)\s*&&'
            )
            assert pattern.search(code_src), (
                f"{page.name} 的「{label}」用了 hasPermission(\"{code}\") 但没写成 "
                "`&&` 条件渲染（可能是 disabled 或三元），与「不展示」口径不符"
            )


class TestEntryExists:
    """用户点名的两个缺口：新增入口、执行时可改参数。"""

    def test_workspace_page_has_create_entry(self):
        code = _code(WS_PAGE)
        assert "JobFormModal" in code, "工作区任务页没接新增/编辑弹窗"
        assert "xhs:taskcenter:create" in code, "缺新增按钮权限码"

    def test_workspace_page_has_edit_entry(self):
        assert "xhs:taskcenter:edit" in _code(WS_PAGE), "缺编辑按钮权限码"

    def test_run_opens_parameter_modal(self):
        """「立即执行」必须先进参数弹窗，不能直接打接口。

        用户原话：「点击执行没有每次需要修改的任务参数」。
        """
        for page in (WS_PAGE, SYS_PAGE):
            code = _code(page)
            assert "RunJobModal" in code, f"{page.name} 没接执行参数弹窗"

    def test_run_endpoint_still_supports_params(self):
        """后端 `params` 覆盖能力必须在（弹窗才有意义）。"""
        src = (ROOT / "backend" / "app" / "api" / "schedule.py").read_text(encoding="utf-8")
        assert "class RunJobRequest" in src
        assert "params_override" in src, "执行端点丢了参数覆盖能力"


class TestMenuIsDynamic:
    """菜单按权限动态生成（无权限不出现），按钮码不进侧栏。"""

    def test_workspace_task_menu_uses_view_code(self):
        import json

        rows = json.loads((MENU_SEED).read_text(encoding="utf-8"))
        items = rows.get("menus") or rows.get("items") or []
        mine = next((r for r in items if r.get("path") == "/platforms/xhs/tasks"), None)
        assert mine is not None
        assert mine.get("menu_type") == "C"
        assert mine.get("perms") == "xhs:taskcenter:view", (
            "菜单行必须挂 view 码（后端按它过滤菜单）"
        )

    def test_sidebar_does_not_render_button_rows(self):
        """F（按钮）行不进侧栏 —— 它们是权限码，不是导航项。"""
        nav = (ROOT / "frontend" / "src" / "components" / "layout" / "use-dynamic-nav.tsx").read_text(
            encoding="utf-8"
        )
        assert "menu_type" in nav or "menuType" in nav, "导航组件没按 menu_type 过滤"
        assert '"F"' in nav or "'F'" in nav, "导航组件没有排除 F（按钮）行"


class TestCreateEditAreWorkspaceScoped:
    """新增/编辑按**任务归属**判码（工作区成员够得着，系统级仍是 admin）。"""

    def test_create_routes_to_workspace_code(self):
        from backend.app.core import route_perms  # noqa: PLC0415

        assert route_perms.ROUTE_PERMS["POST /api/schedule/jobs"] == "xhs:taskcenter:create"

    def test_edit_routes_to_workspace_code(self):
        from backend.app.core import route_perms  # noqa: PLC0415

        assert (
            route_perms.ROUTE_PERMS["PUT /api/schedule/jobs/{job_id}"] == "xhs:taskcenter:edit"
        )

    def test_service_layer_guards_system_workspace(self):
        """服务层必须再判一次「系统级任务要 system 码」。"""
        src = (ROOT / "backend" / "app" / "api" / "schedule.py").read_text(encoding="utf-8")
        assert "_assert_can_create_in_workspace" in src
        assert "_assert_can_edit" in src
        assert "system:scheduler:create" in src
        assert "system:scheduler:edit" in src
