"""权限判据的唯一性守卫：**全项目只按权限码授权**。

## 用户口径（2026-10-26）

> 「按照归属分流和按照权限码执行是一样吗？项目全部应按照权限码」

不一样。两者的差别不是风格问题，是**判据的对象**不同：

| | 判什么 | 出问题的方式 |
|---|---|---|
| 按归属分流 | 数据属于哪一级（`workspace == "system"`） | 数据性质顶替了权限判定；授权侧再怎么配，接口仍按自己的规则减量 |
| 按身份判权 | 这个人是不是 admin | admin 之外的人**无法**单独获得某项能力，只能整块给 |
| **按权限码** | 这个人**有没有这个动作的码** | 没有 —— 授权配了什么就是什么 |

## 允许保留的三类「非权限码」判断（都要带理由）

1. **防锁死规则**：不许删最后一个 admin、admin 不查授权表 —— 防的是「系统里没人能管」，
   不是权限判定。若写成权限码，删光授权后系统就永久锁死。
2. **数据分区（查询条件）**：`where(workspace == "xhs")` 这种是「取哪些行」，
   不是「能不能取」。同一条判据下，能不能取由权限码决定。
3. **选码**：`if job.workspace == "system": code = "system:scheduler:run"`
   —— 归属只用来决定**查哪个码**，放不放行仍只看 `has_perm()`。

本守卫钉的正是「第三类不许悄悄变成第二类」：选码之后必须调用 `has_perm`。
"""

from __future__ import annotations

import re
from pathlib import Path

API = Path(__file__).resolve().parents[2] / "backend" / "app" / "api"

#: 允许按身份判断的位置（防锁死规则）。新增条目必须写理由。
ROLE_CHECKS_ALLOWED: dict[str, str] = {
    "system_menus.py": "admin 不查授权表 —— 防锁死：授权表被清空后仍要能进菜单管理",
    "system_roles.py": "改 admin 角色权限时不踢令牌 —— 防锁死",
    "system_users.py": "不许删最后一个 admin —— 防锁死（不可恢复的事故）",
    "system_permissions.py": "返回 is_admin 标志给前端做展示，不参与授权判定",
}


def _api_sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(API.glob("*.py"))}


def _strip_docstrings_and_comments(src: str) -> str:
    """去掉注释与文档字符串（行号不变，便于报错定位）。

    ⚠️ 必须去 docstring：本仓 docstring 大量写「不要写成 role==admin」这类反例，
    只剥 # 会把反例当成真代码（本守卫第一版就栽在这）。
    """
    src = re.sub(r'"""[\s\S]*?"""', lambda m: "\n" * m.group(0).count("\n"), src)
    src = re.sub(r"'''[\s\S]*?'''", lambda m: "\n" * m.group(0).count("\n"), src)
    return re.sub(r"#[^\n]*", "", src)


def test_role_checks_only_in_whitelisted_files():
    """除了防锁死那几处，API 层不许用身份判权。"""
    pattern = re.compile(
        r'role\s*==\s*["\']admin["\']'
        r"|normalize_role\([^)]*\)\s*==\s*ROLE_ADMIN"
    )
    bad: list[str] = []
    for name, raw in _api_sources().items():
        for i, line in enumerate(_strip_docstrings_and_comments(raw).splitlines(), 1):
            code = line.strip()
            if pattern.search(code) and name not in ROLE_CHECKS_ALLOWED:
                bad.append(f"{name}:{i} {line.strip()[:90]}")
    assert not bad, (
        "以下位置用**身份**代替权限码判权（用户口径「项目全部应按照权限码」）：\n  "
        + "\n  ".join(bad)
        + "\n\n确属防锁死规则的，登记到 ROLE_CHECKS_ALLOWED 并写理由。"
    )


def test_workspace_branch_always_ends_in_has_perm():
    """按工作区选码之后，必须真的调用 `has_perm` 才放行。

    这是本项目最容易退化的地方：`if job.workspace == "system": return job`
    看起来「系统任务就是系统的」，实际是**按数据归属放行**，
    等于给了一个没人授予过的权限。
    """
    src = (API / "schedule.py").read_text(encoding="utf-8")
    #: 抽出 _get_visible_job 函数体
    start = src.index("def _get_visible_job(")
    end = src.index("\ndef ", start + 10)
    body = src[start:end]

    #: 每个 workspace 分支之后都必须出现 has_perm（不能直接 return job）
    branches = re.findall(r'if .*workspace.*?==\s*["\']system["\']:(.*?)(?=\n    if |\n    return |\Z)', body, re.S)
    assert branches, "_get_visible_job 里找不到 workspace 分支（结构变了，请同步本守卫）"
    for br in branches:
        assert "has_perm" in br, (
            "workspace 分支里没有 has_perm ⇒ 按归属直接放行了（应只按权限码判）：\n" + br[:300]
        )


def test_no_workspace_filter_used_as_permission():
    """接口里不许出现「没这个码就把数据按 workspace 减量」的隐式授权。

    判据：筛 `workspace != "system"` 这种**以权限为条件的减量**。
    单纯按查询参数筛（`workspace == 用户传入值`）不受影响。
    """
    bad: list[str] = []
    for name, raw in _api_sources().items():
        for i, line in enumerate(_strip_docstrings_and_comments(raw).splitlines(), 1):
            code = line.strip()
            if not code:
                continue
            if re.search(r'where\([^)]*workspace\s*!=\s*["\']system["\']', code):
                bad.append(f"{name}:{i} {code[:90]}")
    assert not bad, (
        "以下位置按权限条件对数据做 workspace 减量（应改为「只按权限码判能不能看」）：\n  "
        + "\n  ".join(bad)
    )
