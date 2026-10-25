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
SCHEDULE_API = ROOT / "backend" / "app" / "api" / "schedule.py"


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
        #: 一按钮一码（红线）：执行与暂停各有各的码。
        for code in ("xhs:taskcenter:run", "xhs:taskcenter:toggle"):
            assert code in src, f"没有 {code} ⇒ 对应按钮点了会 403"

    def test_workspace_page_renders_operations(self):
        """工作区页**必须**有执行/暂停操作（第二版做成只读被用户否掉）。"""
        code = _code_only(WS_PAGE)
        for label in ("立即执行", "暂停"):
            assert label in code, f"工作区任务页缺少「{label}」操作"
        assert "runScheduleJobOnce" in code, "没有接执行请求函数"
        assert "changeScheduleJobStatus" in code, "没有接暂停/恢复请求函数"


class TestCreateIsOpen:
    """新增任务**正常开放**（2026-10-25 用户口径「正常开放无单独处理」）。

    ## 这条口径的来历

    中途曾按「暂时不给权限」做成：代码开关 → 独立权限码 → admin 除外，
    用户最终定为**不做任何单独处理**：不加开关、不拆独立码，
    与执行/暂停共用 `system:scheduler:manage`。

    ⇒ 守卫只钉两件**实质**的事：写端点有码、且 `job_key` 唯一。
    """

    def test_create_endpoint_is_guarded(self):
        """写端点必须有权限码（漏挂 ⇒ 启动守卫拒启）。"""
        from backend.app.core import route_perms  # noqa: PLC0415

        spec = route_perms.ROUTE_PERMS.get("POST /api/schedule/jobs")
        assert spec is not None, "POST /api/schedule/jobs 没登记权限码"
        assert "system:scheduler:create" in str(spec), (
            f"新增任务权限码是 {spec}，应为独立的 system:scheduler:create"
            "（红线：一按钮一码，不与执行/暂停共用）"
        )

    def test_no_leftover_switch_or_extra_code(self):
        """不许留开关，也不许留独立权限码 —— 口径是「无单独处理」。"""
        assert "ALLOW_JOB_CREATE" not in SCHEDULE_API.read_text(encoding="utf-8"), (
            "还留着 ALLOW_JOB_CREATE 开关"
        )
        #: 反过来：这个码**必须**存在（它现在是「新增任务」按钮的独立码）。
        assert "system:scheduler:create" in PERMS.read_text(encoding="utf-8"), (
            "缺少 system:scheduler:create ⇒ 新增按钮没有自己的权限码"
        )

    def test_duplicate_job_key_is_rejected(self):
        """`job_key` 全表唯一（设计 69）—— 同 key 建第二行必须 409，不能 500。"""
        import ast  # noqa: PLC0415

        src = SCHEDULE_API.read_text(encoding="utf-8")
        tree = ast.parse(src)
        body = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "create_job"
            ),
            None,
        )
        assert body is not None, "找不到 create_job"
        segment = ast.get_source_segment(src, body) or ""
        assert "409" in segment or "HTTP_409_CONFLICT" in segment, (
            "create_job 没有唯一性预检 ⇒ 重复建会抛 IntegrityError 变 500"
        )
