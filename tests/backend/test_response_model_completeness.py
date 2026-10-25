"""响应模型完整性守卫：`serialize_*` 产出的键必须在 response_model 里声明。

## 为什么需要（2026-10-25 实测）

系统管理 → 任务中心 → 执行日志，「工作区」列全是 `-`。

排查链路：库里 `workspace` 有值（81271 行全非空）→ `serialize_log()` 也确实
把它放进了返回 dict → 接口 200 无报错 → **前端拿不到**。

根因：`ScheduledJobLogItem`（Pydantic response_model）**没声明 `workspace`**，
FastAPI 按模型过滤响应 ⇒ 字段被静默丢弃。

## 为什么这是「必须用守卫」的一类缺陷

它同时满足三个条件，人工看不出来：

1. **不报错**：FastAPI 接受多余键，Pydantic 直接忽略；
2. **类型检查也不报**：前端类型里写了 `workspace?: string`，TS 认为可能存在；
3. **只在界面上表现为缺值**：列渲染 `-`，与「数据本来就没有」完全无法区分。

⇒ 每加一个响应字段，都要同步 schema；漏了就由本守卫点名。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEDULE_API = ROOT / "backend" / "app" / "api" / "schedule.py"
SCHEDULE_SCHEMAS = ROOT / "backend" / "app" / "schemas" / "schedule_responses.py"


def _serialize_keys(func_name: str) -> set[str]:
    """取 `serialize_xxx()` 返回 dict 里的字面量键集合。"""
    src = SCHEDULE_API.read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name
        ),
        None,
    )
    assert node is not None, f"schedule.py 里找不到 {func_name}"

    keys: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Dict):
            for key in sub.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
    return keys


def _model_fields(model_name: str) -> set[str]:
    """取 Pydantic 模型里声明的字段名（含带注解的 class 属性）。"""
    src = SCHEDULE_SCHEMAS.read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == model_name),
        None,
    )
    assert node is not None, f"schedule_responses.py 里找不到 {model_name}"
    return {
        stmt.target.id
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    }


class TestLogResponseModelIsComplete:
    def test_serialize_log_keys_are_all_declared(self):
        """`serialize_log()` 的每个键都必须在 `ScheduledJobLogItem` 里声明。

        漏一个 ⇒ 该字段在接口响应里消失，前端表现为「这一列全是 `-`」。
        """
        produced = _serialize_keys("serialize_log")
        declared = _model_fields("ScheduledJobLogItem")
        missing = sorted(produced - declared)
        assert not missing, (
            "serialize_log 产出了但 response_model 没声明（会被 Pydantic 静默丢弃）："
            f"{missing}"
        )

    def test_workspace_is_present(self):
        """工作区列是两级任务中心的判据字段，单独钉一条（回归防线）。"""
        assert "workspace" in _model_fields("ScheduledJobLogItem"), (
            "ScheduledJobLogItem 缺 workspace ⇒ 执行日志「工作区」列全为 `-`"
        )


class TestJobResponseModelIsComplete:
    def test_serialize_job_keys_are_all_declared(self):
        """`_serialize_job()` 同理（`workspace` 曾在这里也差点漏掉）。"""
        produced = _serialize_keys("_serialize_job")
        declared = _model_fields("ScheduledJobItem")
        missing = sorted(produced - declared)
        assert not missing, (
            "serialize_job 产出了但 response_model 没声明（会被 Pydantic 静默丢弃）："
            f"{missing}"
        )


class TestSystemPagesArePermAuthorized:
    """系统管理模块靠**菜单权限**授权，不是「仅管理员」。

    用户口径（2026-10-25）：「系统管理模块也是依靠菜单权限授权，并不是仅管理员」。

    ⇒ 两类硬编码都算违规：
    1. 界面上写死「仅管理员」标签（管理员把菜单勾给别的角色后，标签就成了假话）；
    2. 端点内 `role == admin` 代替权限码（勾了菜单也照样 403，页面成空壳）。
    """

    FORBIDDEN_UI_LABELS = ("仅管理员", "仅超级管理员", "只有超级管理员")

    def test_no_hardcoded_admin_label_in_system_pages(self):
        pages = list((ROOT / "frontend" / "src" / "pages" / "system").glob("*.tsx"))
        assert pages, "没找到系统管理页面"
        offenders = []
        for page in pages:
            src = page.read_text(encoding="utf-8")
            #: 剥注释（docstring 会解释「为什么不写这个标签」）
            code = "\n".join(
                ln
                for ln in src.splitlines()
                if not ln.lstrip().startswith(("*", "//", "/*"))
            )
            for label in self.FORBIDDEN_UI_LABELS:
                if label in code:
                    offenders.append(f"{page.name}: {label}")
        assert not offenders, "系统管理页面写死管理员身份（应靠权限码）：\n" + "\n".join(offenders)

    def test_schedule_overview_uses_perm_code_not_role(self):
        """`GET /api/schedule/overview` 必须判权限码，不能判 role。"""
        from backend.app.core import route_perms  # noqa: PLC0415

        spec = route_perms.ROUTE_PERMS.get("GET /api/schedule/overview")
        assert spec == "system:scheduler:view", (
            f"总览端点挂的是 {spec} —— 应为 system:scheduler:view"
            "（EXEMPT_ADMIN = 端点内硬判 role，与「靠菜单授权」矛盾）"
        )

    def test_settings_scope_uses_perm_code_not_role(self):
        """配置端点解析归属级时必须判权限码。"""
        src = (ROOT / "backend" / "app" / "api" / "settings.py").read_text(encoding="utf-8")
        assert 'has_perm(current_user, "system:setting:edit")' in src, (
            "settings.py 没有用 system:setting:edit 判权限"
        )
        #: 函数体内不许再出现 role 硬判（docstring 里提到不算）
        start = src.index("def _resolve_scope")
        end = src.index("def _wake_runtime_consumers")
        body = src[start:end]
        code = "\n".join(
            ln for ln in body.splitlines() if not ln.lstrip().startswith(("*", "#"))
        )
        assert "ROLE_ADMIN" not in code, "settings.py 仍在用 ROLE_ADMIN 判权限"
