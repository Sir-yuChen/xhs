"""按钮码路由归属守卫（设计 71，2026-10-26）。

## 这条红线的病根

权限体系有四层，**前三层可以全绿而第四层完全空着**：

| 层 | 文件 | 只查这一层会得到什么假结论 |
|---|---|---|
| ① 码定义 | `core/perms_registry.py` | 「码存在 = 授权页能勾」✅ |
| ② 菜单行 | `config/initial_data/menus.seed.json` | 「行存在 = 能授出去」✅ |
| ③ 路由登记 | `core/route_perms.py` | 「登记了 = 受保护」**❌ 假** |
| ④ 运行时依赖 | `Depends(require_perm(...))` | 只有这条才是真的 |

**「零路由码」= 授权页上能勾、后端永远不按它判权。** 实测（2026-10-26 清理前）
173 个按钮码里 **61 个零路由** —— 主因是「码拆出来了、路由还挂在旧合并码上」
（`xhs:monitor:edit/delete/refresh` 三条全零，四个动作都由 `monitor:create` 顶着）。

## 本文件的判据分两层

1. **正文（本文件后半）**：按设计 71 的分组，逐条断言
   `ROUTE_PERMS["METHOD path"] == "期望的码"` —— 防「改回去」。
   这是**回归守卫**：它证明「这一组确实挂对了」，而不是「大概挂对了」。

2. **欠账单**：`test_route_perms_integrity.py` 的 `KNOWN_ORPHAN_BUTTON_CODES`
   只许变短 —— 每修好一组，就从那里删掉对应条目。

## ⚠️ 分组时先回答「这个码有没有独立端点」

不是所有零路由码都是缺陷。三种合法形态必须登记，不能一律当 bug 改：

| 形态 | 例子 | 处理 |
|---|---|---|
| 一个端点按参数分派多码 | `POST /items/operate` + `action` | 白名单 + 点名分派函数 |
| 两级共用端点，服务层选码 | `system:scheduler:*` = `xhs:taskcenter:*` | 白名单 + 点名 `_get_visible_job` |
| 纯前端判据，后端无端点 | `xhs:draft:*` | 白名单 + 写清「动作归哪个域的码」 |

把第 3 种硬改成「给它挂一条路由」会**凭空造出一个不该有的权限边界**
（例：`xhs:draft:save` 若硬挂到 `POST /notes/batch-save`，等于说「能在草稿工坊保存
= 能在内容库批量保存」，那是两个不同入口）。
"""

from __future__ import annotations

import pytest

ROOT_MSG = "（结构变了，请同步本守卫）"


def _route_perms() -> dict[str, str]:
    from backend.app.core.route_perms import ROUTE_PERMS

    return ROUTE_PERMS


# --------------------------------------------------------------------------- #
# G1 · 监控中心（2026-10-26）
# --------------------------------------------------------------------------- #
class TestMonitorCodes:
    """`xhs:monitor:*` —— 四个写动作各判各的，不再全由 `create` 顶着。

    ## 为什么这几条是**真缺陷**而不是「一个端点分派多码」

    监控中心是四个**独立端点**（`POST /targets`、`PATCH /targets/{id}`、
    `DELETE /targets/{id}`、`POST /targets/{id}/refresh`），不是同一个端点靠
    body 分派 —— 所以它们必须各自挂自己的码。

    ## `xhs:monitor:enable` 为什么**不**在这组里

    「启用/停用监控」是 `PATCH /targets/{id}` 的 `status` 字段，**没有独立端点**
    ⇒ 它和 `edit` 落在同一个请求上。静态表只能挂一个码 ⇒ 归 `edit`，
    `enable` 已登记进 `FRONTEND_ONLY_BUTTON_CODES`（前端用它控制开关显隐）。
    """

    @pytest.mark.parametrize(
        ("route", "code"),
        [
            ("POST /api/xhs/monitoring/targets", "xhs:monitor:create"),
            ("PATCH /api/xhs/monitoring/targets/{target_id}", "xhs:monitor:edit"),
            ("DELETE /api/xhs/monitoring/targets/{target_id}", "xhs:monitor:delete"),
            (
                "POST /api/xhs/monitoring/targets/{target_id}/refresh",
                "xhs:monitor:refresh",
            ),
        ],
    )
    def test_each_action_has_its_own_code(self, route: str, code: str):
        actual = _route_perms().get(route)
        assert actual == code, (
            f"{route} 应挂 {code}，实际 {actual}"
            + (
                " —— 全挂 create 的后果：想让某人『刷新监控』必须连『删除监控』一起给"
                if actual == "xhs:monitor:create"
                else ""
            )
        )


# --------------------------------------------------------------------------- #
# G2 · 数据包规则（2026-10-26）
# --------------------------------------------------------------------------- #
class TestDataPackageRuleCodes:
    """`xhs:rule:*` —— 规则的增 / 改 / 删 / 试跑各判各的。

    ## 为什么这组是**真缺陷**

    四个动作是**四个独立端点**（`POST /rules`、`PATCH /rules/{id}`、
    `DELETE /rules/{id}`、`POST /execute`），却全挂 `xhs:rule:create`。
    ⇒ 授权页上勾了「编辑规则」没有任何效果；而想让人「试跑规则」，
    必须连「删除规则」一起给 —— 试跑是只读动作，删除不可恢复。
    """

    @pytest.mark.parametrize(
        ("route", "code"),
        [
            ("POST /api/xhs/data-package-rules/sets", "xhs:rule:create"),
            ("POST /api/xhs/data-package-rules/rules", "xhs:rule:create"),
            ("PATCH /api/xhs/data-package-rules/sets/{set_id}", "xhs:rule:edit"),
            ("PATCH /api/xhs/data-package-rules/rules/{rule_id}", "xhs:rule:edit"),
            ("DELETE /api/xhs/data-package-rules/sets/{set_id}", "xhs:rule:delete"),
            ("DELETE /api/xhs/data-package-rules/rules/{rule_id}", "xhs:rule:delete"),
            ("POST /api/xhs/data-package-rules/execute", "xhs:rule:execute"),
        ],
    )
    def test_each_action_has_its_own_code(self, route: str, code: str):
        actual = _route_perms().get(route)
        assert actual == code, f"{route} 应挂 {code}，实际 {actual}"
        assert actual != "xhs:rule:create" or code == "xhs:rule:create", (
            f"{route} 又退化回 create（一码盖四动作）"
        )
