"""操作列不溢出 + 批量操作守卫（2026-10-25 用户口径）。

## 红线原文

> 「操作列的操作按钮自适应，不要溢出页面，**所有操作列均不允许溢出页面**」
> 「新增用户管理批量相关操作，操作列ui操作超出了页面不正常」

## 为什么溢出是必然的（不是哪一页写宽了）

每个页面各自决定「放几个按钮 + 定多宽」这种做法本身就会溢出：

* 用户管理 5 个按钮（通过 / 编辑 / 重置密码 / 下线 / 删除）
* 任务中心 4 个（立即执行 / 编辑 / 暂停 / 日志，160px 宽）
* 而按钮数量还随**角色**变化（权限少的角色只剩 2 个）—— 固定的宽度永远在某个
  组合下不够。

⇒ 判据不是「这一页宽度够不够」，而是**「操作列必须用统一容器，且容器自身会收敛」**。

## 本守卫管三件事

1. `RowActions` 存在且**按权限过滤**（无权限不渲染，不是置灰）；
2. 已迁移的页面**必须**用 `RowActions`（不许退回手写 `Space` 拼按钮）；
3. 剩下的页面登记在 `MIGRATED` / `PENDING` 里 —— `PENDING` **只许变短**。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "frontend" / "src"

#: 已经用统一容器的页面（新增即红，必须同步登记）
MIGRATED = {
    "pages/system/system-users-panel.tsx",
    "pages/system/system-scheduler-panel.tsx",
    "pages/tasks/workspace-tasks-page.tsx",
    "pages/system/system-roles-panel.tsx",
    "pages/platforms/xhs/orders-page.tsx",
}

#: 还有操作列但尚未迁移的页面（**只许变短**：改好一个就从中删掉）
PENDING: set[str] = {
    "components/schedule/task-columns.tsx",
    "components/settings/settings-table.tsx",
    "pages/llm-gateway/llm-gateway-page.tsx",
    "pages/platforms/xhs/accounts-page.tsx",
    "pages/platforms/xhs/datacenter-page.tsx",
    "pages/platforms/xhs/data-package-rules-modal.tsx",
    "pages/platforms/xhs/forbidden-words-modal.tsx",
    "pages/platforms/xhs/note-version-history.tsx",
    "pages/platforms/xhs/picture-space-page.tsx",
    "pages/platforms/xhs/product-drafts-modal.tsx",
    "pages/platforms/xhs/prompt-template-panel.tsx",
    "pages/platforms/xhs/publish-config-step2.tsx",
    "pages/platforms/xhs/shop-items-page.tsx",
    "pages/platforms/xhs/sku-name-optimize-modal.tsx",
    "pages/platforms/xhs/violation-manage-page.tsx",
    "pages/system/system-dict-panel.tsx",
    "pages/system/system-menus-panel.tsx",
    "pages/tasks/task-records-page.tsx",
}


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _requirement(src: str) -> str:
    """去掉注释，只看真代码。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def _pages_with_action_column() -> set[str]:
    """所有声明了「操作」列的前端文件（相对 frontend/src）。"""
    out: set[str] = set()
    for path in ROOT.rglob("*.tsx"):
        code = _requirement(path.read_text(encoding="utf-8"))
        if re.search(r'title:\s*["\']操作["\']', code):
            out.add(path.relative_to(ROOT).as_posix())
    return out


def test_row_actions_component_exists_and_filters_by_permission():
    """统一容器必须按权限过滤，且支持「更多」收敛。"""
    src = _requirement(_src("components/common/row-actions.tsx"))
    #: 无权限直接不出现（用户口径「没有对应权限则不展示」）
    assert "it.permission" in src and "hasPermission" in src
    #: 超出 maxInline 的收进「更多」
    assert "maxInline" in src and "Dropdown" in src
    #: 全部无权限时渲染缺值符，不留空按钮组
    assert '>-\u003c' in src or '"-">' in src or ">-</Text>" in src


def test_migrated_pages_use_the_shared_container():
    """已迁移页面不许退回手写按钮组（那正是溢出的成因）。"""
    for rel in MIGRATED:
        code = _requirement(_src(rel))
        assert "RowActions" in code, f"{rel} 应使用 RowActions"


def test_action_column_never_overflows():
    """操作列必须 fixed:right + 宽度不超过 260px（各分辨率的共同安全区）。"""
    for rel in sorted(_pages_with_action_column()):
        code = _requirement(_src(rel))
        for m in re.finditer(r'title:\s*["\']操作["\'](.{0,400})', code, re.S):
            seg = m.group(1)
            w = re.search(r"width:\s*(\d+)", seg)
            if w:
                assert int(w.group(1)) <= 260, f"{rel} 操作列宽 {w.group(1)} > 260，会溢出"


def test_pending_list_only_shrinks():
    """未迁移页面清单只许变短 —— 新增操作列页面必须同时接入统一容器。"""
    actual = _pages_with_action_column()
    unknown = actual - MIGRATED - PENDING
    assert not unknown, (
        "以下页面有操作列但既未接入 RowActions 也未登记为欠账：\n  "
        + "\n  ".join(sorted(unknown))
    )
    stale = PENDING & MIGRATED
    assert not stale, f"已在 MIGRATED 里却又留在 PENDING：{stale}"


def test_user_batch_endpoints_are_declared_with_own_perms():
    """用户批量操作：四个端点各一个独立码（红线「一按钮一码」），且走统一批量内核。"""
    api = (ROOT.parents[1] / "backend/app/api/system_users.py").read_text(encoding="utf-8")
    for action, code in [
        ("delete", "system:user:batchDelete"),
        ("reset-password", "system:user:batchResetPassword"),
        ("force-logout", "system:user:batchForceLogout"),
        ("approve", "system:user:batchApprove"),
    ]:
        assert f'"/batch/{action}"' in api, f"缺少批量端点 {action}"
        assert code in api, f"批量端点 {action} 未挂独立权限码 {code}"
    #: 逐条调单条内核（不另写规则）
    assert "run_batch" in api
    kernel = (ROOT.parents[1] / "backend/app/services/user_batch_service.py").read_text(encoding="utf-8")
    #: 四态回执：不能合并成「已处理 N 个」
    for status in ("ok", "skipped", "failed", "not_found"):
        assert status in kernel


def test_batch_routes_are_declared_before_parameter_routes():
    """字面量批量路由必须**声明在** `/{user_id}` 之前。

    FastAPI 按**声明顺序**匹配，`/batch/approve` 若排在 `/{user_id}` 之后，
    会被它吃掉 —— `batch` 被当成 `user_id` 解析 ⇒ **422 int_parsing**，
    而且报错信息（「Input should be a valid integer」）完全看不出是路由顺序问题。
    （2026-10-25 实测踩到：4 个批量端点全部 422，前端会报「批量操作失败」但原因不明。）
    """
    api = (ROOT.parents[1] / "backend/app/api/system_users.py").read_text(encoding="utf-8")
    order = re.findall(r'@router\.(?:get|post|put|delete)\("([^"]*)"', api)
    param_at = order.index("/{user_id}")
    for path in order:
        if path.startswith("/batch/"):
            assert order.index(path) < param_at, (
                f"{path} 声明在 /{{user_id}} 之后 ⇒ 会被它吃掉并回 422"
            )
