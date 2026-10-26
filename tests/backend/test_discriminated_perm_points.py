"""「同一端点按字段分派两码」的两个落地点（2026-10-26 G8）。

## 为什么需要这批守卫

`ai:gateway:enableModel` 与 `xhs:autoops:enable` 的端点**同时承载两种写**：

| 端点 | 改内容 | 只翻开关 |
|---|---|---|
| `PATCH /api/auto-tasks/{id}` | `name`/`keywords`/`ai_instruction`/`schedule` → `xhs:autoops:edit` | `status` → `xhs:autoops:enable` |
| `PATCH /llm/gateway-configs/{id}/models/{mid}` | `model_type`/`platform`/`remark` → 路由表的码 | `enabled` → `ai:gateway:enableModel` |

静态路由表只能挂一个码，所以这两条码**只有在函数内选码才可能生效**。
没有这批守卫时，它们的处境是「登记表里有、授权页能勾、但勾了对接口没有任何作用」
—— 与之前 27 行幽灵菜单是同一类「假授权」。

## 判据

1. 行为：只翻开关 + 无该码 ⇒ 403 且 `detail.code` 指名；有码 ⇒ 放行；
2. 行为：**同时**改内容与开关 ⇒ 走路由表的码，不再额外要启停码（否则「有编辑权
   的人改内容顺带翻开关」会被莫名拦住）；
3. 静态：两个端点函数体里必须真的调用了对应的 `_assert_can_toggle_*`（防「注释写了、
   代码没调」）；且必须在**改动生效之前**判（防「先写库再判权」）。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AUTO = ROOT / "backend" / "app" / "api" / "auto_tasks.py"
GATEWAY = ROOT / "backend" / "app" / "api" / "llm_gateway.py"


# --------------------------------------------------------------------------- #
# 静态：函数内确实调了选码函数，且调用在赋值之前
# --------------------------------------------------------------------------- #

def _func_node(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


@pytest.mark.parametrize(
    ("path", "func", "callee", "guarded_field"),
    [
        (AUTO, "update_auto_task", "_assert_can_toggle_auto_task", "status"),
        (GATEWAY, "update_gateway_model", "_assert_can_toggle_model", "enabled"),
    ],
)
def test_endpoint_calls_the_field_level_guard(path, func, callee, guarded_field):
    """① 端点里必须真的调用选码函数，且**早于**对那个字段的赋值。

    ⚠️ 只查「有没有调用」不够（实测：把 `if status_only_change:` 改成
    `if False:` 后调用还在，守卫照样绿 —— 这是**假绿**）。
    因此还要查：那个调用必须**落在一条 If 的 body 里**（不是裸语句），
    否则恒不执行 = 等于没判。
    """
    node = _func_node(path, func)
    assert node is not None, f"{func} 不存在"

    call_nodes = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == callee:
            call_nodes.append(sub)
    assert call_nodes, (
        f"{func} 没调用 {callee} ⇒ {guarded_field} 的启停权限对接口不生效（假授权）"
    )

    #: 收集「被 If 的 body 包住的调用行号」
    guarded_linenos = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.If):
            for inner in ast.walk(sub):
                if isinstance(inner, ast.Call) and getattr(inner.func, "id", None) == callee:
                    guarded_linenos.add(inner.lineno)
                    #: 条件必须是**真的判据**，不能是常量 False / 0 / None
                    assert not isinstance(sub.test, ast.Constant) or bool(sub.test.value), (
                        f"{func} 里 {callee} 的守卫条件被写成常量 "
                        f"{ast.unparse(sub.test)!r} ⇒ 恒不执行，等于没判"
                    )

    call_lineno = min(c.lineno for c in call_nodes)
    assert call_lineno in guarded_linenos, (
        f"{func} 里 {callee}（第 {call_lineno} 行）不在任何 If 的 body 里"
        f" —— 「只翻 {guarded_field}」这条判据必须是有条件的，否则每次改内容都会多要一个码"
    )

    assign_lineno = None
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assign):
            for target in sub.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == guarded_field
                    and isinstance(target.value, ast.Name)
                    and target.value.id in ("row", "auto_task")
                ):
                    assign_lineno = sub.lineno if assign_lineno is None else min(
                        assign_lineno, sub.lineno)

    assert assign_lineno is not None, f"{func} 里没找到对 {guarded_field} 的赋值（判据失效）"
    assert call_lineno < assign_lineno, (
        f"{func} 里 {callee} 在第 {call_lineno} 行，而 {guarded_field} 在第 "
        f"{assign_lineno} 行就被写了 —— **先写库再判权**，等于没判"
    )


@pytest.mark.parametrize(
    ("path", "func", "code"),
    [
        (AUTO, "update_auto_task", "xhs:autoops:enable"),
        (GATEWAY, "update_gateway_model", "ai:gateway:enableModel"),
    ],
)
def test_guard_actually_checks_the_right_code(path, func, code):
    """② 守卫函数体里判的必须是**那条码**（不是兄弟码）。"""
    src = path.read_text(encoding="utf-8")
    idx = src.find(f"def {func}")
    assert idx != -1
    #: 该函数的 docstring/说明里要出现这条码；真正的判据在 `_assert_can_toggle_*` 里
    helper = "_assert_can_toggle_auto_task" if "auto_tasks" in str(path) else "_assert_can_toggle_model"
    hidx = src.find(f"def {helper}")
    assert hidx != -1, f"{helper} 不存在"
    body = src[hidx:hidx + 2000]
    assert f'"{code}"' in body, f"{helper} 里没判 {code}"


# --------------------------------------------------------------------------- #
# 行为：只翻开关 vs 同时改内容
# --------------------------------------------------------------------------- #

def test_auto_task_status_only_change_requires_the_enable_code():
    """③ 只改 status 且没该码 ⇒ 403；同时改内容 ⇒ 不再额外要 enable 码。

    直接对**判定表达式**取真值，不构造 HTTP 与数据库 —— 这里要钉的是
    「哪种请求算『只翻开关』」这条判据本身。
    """
    def status_only(payload, current):
        return (
            payload.get("status") is not None
            and payload["status"] != current
            and all(payload.get(f) is None for f in ("name", "keywords", "ai_instruction", "schedule"))
        )

    assert status_only({"status": "paused"}, "active") is True
    #: 值没变 ⇒ 不算启停（幂等 PATCH 不该要码）
    assert status_only({"status": "active"}, "active") is False
    #: 同时改名 ⇒ 走 edit 的码，不再要 enable
    assert status_only({"status": "paused", "name": "x"}, "active") is False
    #: 只改名 ⇒ 与 enable 无关
    assert status_only({"name": "x"}, "active") is False


def test_gateway_model_enabled_only_change_requires_the_toggle_code():
    """④ 同③，模型启停那一侧。"""
    def enabled_only(payload, current):
        return (
            payload.get("enabled") is not None
            and payload["enabled"] != current
            and all(payload.get(f) is None for f in ("model_type", "platform", "remark"))
        )

    assert enabled_only({"enabled": False}, True) is True
    assert enabled_only({"enabled": True}, True) is False        # 值没变
    assert enabled_only({"enabled": False, "remark": "x"}, True) is False  # 同时改备注
    assert enabled_only({"remark": "x"}, True) is False


def test_both_helpers_use_the_shared_permission_source():
    """⑤ 两个 `_assert_can_toggle_*` 都必须走 `permission_cache.perms_of`。

    自己另写一套查库 = 与 `require_perm` 之间出现第二份真相源（缓存失效不同步）。
    """
    for path, helper in (
        (AUTO, "_assert_can_toggle_auto_task"),
        (GATEWAY, "_assert_can_toggle_model"),
    ):
        src = path.read_text(encoding="utf-8")
        idx = src.find(f"def {helper}")
        body = src[idx:idx + 2000]
        assert "permission_cache" in body or "perms_of" in body, (
            f"{helper} 没走 permission_cache.perms_of（第二份真相源）"
        )
        assert "ROLE_ADMIN" in body, (
            f"{helper} 必须像 require_perm 一样给 admin 直接放行（admin 由代码派生全量码）"
        )
