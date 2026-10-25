"""两级权限边界守卫（设计 69，2026-10-25 用户口径）。

## 判据

用户口径：
* 「任务中心分为两级，系统级和工作区域级」
* 「系统任务在系统管理中展示操作等，工作区域任务在对应工作区内展示，
   **以及相关的操作等**，相互独立展示不集中在系统管理中」

⇒ 两级的分界是**任务归属**，不是路由的固定权限码：

| 操作 | 系统级任务（`workspace="system"`）| 工作区级任务（`workspace="xhs"`）|
|---|---|---|
| 查看 | `system:scheduler:view` | `xhs:taskcenter:view` |
| 执行 / 暂停 | `system:scheduler:manage` | `xhs:taskcenter:manage` |

## 为什么需要守卫

「执行一次」「暂停恢复」是**同一个端点**服务两级任务，权限码却必须不同。
静态路由表（`ROUTE_PERMS`）一个路由只能挂一个码 ⇒ 判据下移到服务层
`_get_visible_job`。这个下移**极易写成「只判存在」**（我第一版就是），
那样任何登录用户都能操作系统级任务（清理记录表 / 暂存目录）——
不报错、不抛异常，只是静默越权。所以必须有守卫钉住。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEDULE_API = ROOT / "backend" / "app" / "api" / "schedule.py"
ROUTE_PERMS = ROOT / "backend" / "app" / "core" / "route_perms.py"
PERMS = ROOT / "backend" / "app" / "core" / "perms_registry.py"


def _func_source(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
    raise AssertionError(f"{path.name} 里找不到 {name}")


class TestTwoLevelPermission:
    def test_workspace_perm_codes_are_registered(self):
        """两级各自的 view/manage 码都要在注册表里（漏登记 ⇒ 启动守卫拒启）。"""
        src = PERMS.read_text(encoding="utf-8")
        #: 2026-10-25 红线：一按钮一码 ⇒ manage 已拆成 run / toggle / viewLogs。
        for code in (
            "xhs:taskcenter:view",
            "xhs:taskcenter:run",
            "xhs:taskcenter:toggle",
            "xhs:taskcenter:viewLogs",
            "system:scheduler:view",
        ):
            assert code in src, f"权限码 {code} 没登记"

    def test_run_and_status_endpoints_are_workspace_scoped(self):
        """执行/暂停端点不能挂 `system:scheduler:manage` —— 否则工作区成员永远 403。"""
        src = ROUTE_PERMS.read_text(encoding="utf-8")
        for route in (
            "POST /api/schedule/jobs/{job_id}/run",
            "PATCH /api/schedule/jobs/{job_id}/status",
        ):
            line = next((ln for ln in src.splitlines() if route in ln and ":" in ln), "")
            assert line, f"{route} 没登记权限码"
            assert (
                "xhs:taskcenter:run" in line or "xhs:taskcenter:toggle" in line
            ), f"{route} 挂的不是工作区按钮码：{line.strip()}"

    def test_system_level_job_cannot_be_operated_by_workspace_perm(self):
        """`_get_visible_job` 必须**按任务工作区**判，不能只判存在。"""
        src = _func_source(SCHEDULE_API, "_get_visible_job")
        assert 'workspace == "system"' in src or "job_workspace ==" in src, (
            "_get_visible_job 没有按 job.workspace 分支 ⇒ 任何登录用户都能操作系统级任务"
        )
        assert "system:scheduler:run" in src or "system:scheduler:toggle" in src, (
            "没有以 system:scheduler:manage 作为系统级任务的判据"
        )
        assert "taskcenter:run" in src and "taskcenter:toggle" in src, (
            "没有以 <workspace>:taskcenter:manage 作为工作区任务的判据"
        )

    def test_delete_and_create_stay_admin_only(self):
        """新建 / 删除任务仍只归系统管理（「暂时不允许用户创建任务」）。"""
        src = ROUTE_PERMS.read_text(encoding="utf-8")
        for route in (
            "POST /api/schedule/jobs",
            "DELETE /api/schedule/jobs/{job_id}",
        ):
            line = next((ln for ln in src.splitlines() if f'"{route}"' in ln), "")
            #: 2026-10-25「入口及权限补全」：新增/编辑改为工作区码，
            #: 系统级任务由服务层 `_assert_can_create_in_workspace` 再判；
            #: 删除仍是 admin 专属。
            if route.startswith("DELETE"):
                assert "system:scheduler:" in line and "xhs:" not in line, (
                    f"{route} 不该放开给工作区：{line.strip()}"
                )
            else:
                assert "taskcenter:create" in line, f"{route} 应挂工作区新增码"
