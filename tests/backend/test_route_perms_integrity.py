"""权限路由表自身的完整性守卫（2026-10-26）。

## 为什么要单独守「字典字面量本身」

`route_perms.py` 的 `ROUTE_PERMS` 是一个**手写的 Python 字典字面量**。
字面量里同一个键写两次时，Python **不报错**，后者静默覆盖前者 ——
于是前一条的权限码变成**死码**：它永远拿不到任何路由，
`test_button_level_perms` 的别名表却仍然写着「这个码盖了 N 条路由」，
两边互相作证说系统是好的。

**实测（2026-10-26 清理前）：338 条条目只有 328 个唯一键，10 个重复**，其中：

| 重复键 | 先写（被吞掉 → 死码） | 后写（生效） |
|---|---|---|
| `POST /api/accounts/login-states` | `xhs:account:create` | `xhs:login:view` |
| `POST /api/accounts/{account_id}/login-state` | `xhs:account:relogin` | `xhs:login:view` |
| `POST /api/xhs/crawl/{data,note-urls,search-notes,user-notes}` | **`xhs:crawl:submit`**（4 条全被吞） | `xhs:crawl:view` |

后果：`xhs:crawl:submit`（「提交抓取」按钮码）路由数恒为 **0** ——
它挂在角色授权页上能被勾选，但后端永远不会按它判权。
其余 4 个重复键是**同值重复**（`autoops:edit` / `scheduler:view` /
`insights:view` / `log:view`），不产生死码，但说明有人在两处各记了一遍。

## 判据

1. `ROUTE_PERMS` 的**唯一键数 == 条目数**（字面量里不许重复键）。
2. 不允许「同一个码同时是某条路由的唯一登记值」出现在两个语义相反的动作上 ——
   这条由人工 review + 下面第 3 条共同兜住。
3. **每个按钮码（`menu_type="F"`）至少要有一条路由**，除非显式登记为
   「纯前端判据」（前端用它控制按钮显隐，后端无对应端点）。

第 3 条的例外必须带**理由**，因为「按钮码零路由」有两种完全不同的成因：
* 正常：入队在体里带动作（`/items/operate` 的 `action=off_shelf`）；
* 缺陷：字面量写重了，先写的那条被吞掉。
两种从代码上分不开，所以只能逐个登记 —— 登记本身就是 review 的痕迹。
"""

from __future__ import annotations

import collections
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROUTE_PERMS_SRC = ROOT / "backend" / "app" / "core" / "route_perms.py"

#: 「按钮码零路由」的**合法**白名单（键 = 码；值 = 为什么没有路由）。
#:
#: ⚠️ 登记前必须回答：这个码是**由前端判据**（后端无端点）还是**由服务层选码**
#: （一个端点按参数分派多码）。后者要在值里点名分派位置。
#:
#: 一个端点按参数分派多个码时，它**不占路由**的那些码登记在这里（键 = 码，值 = 分派位置）。
#: 例：`POST /items/operate` 只登记了 `xhs:item:onShelf`，另外两个动作由端点内
#: `_assert_action_permitted` 按 `action` 选码 —— 那两个码因此是「零路由」。
FRONTEND_ONLY_BUTTON_CODES: dict[str, str] = {
    #: 2026-10-26「生成笔记/上架/下架拆三个码」：三个动作共用一个平台端点。
    "xhs:item:offShelf": "POST /items/operate 的 action=off_shelf，由 goods.py::_assert_action_permitted 选码",
    "xhs:item:delete": "POST /items/operate 的 action=delete，同上选码",
    #: 2026-10-26 设计 71 G1：「启用/停用监控」是 `PATCH /targets/{id}` 的 `status`
    #: 字段，**没有独立端点** ⇒ 与 `edit` 同一请求，静态表只能挂一个码。
    #: 前端用 `enable` 控制开关显隐，后端按 `edit` 判权。
    "xhs:monitor:enable": "PATCH /xhs/monitoring/targets/{id} 的 status 字段，与 edit 同一端点",
}

#: ⚠️ 2026-10-26：零路由按钮码的**处置分类表**统一在
#: `tests/backend/test_orphan_perm_classification.py`（REMAP_PENDING /
#: DISCRIMINATED / SERVICE_PICKED / ENTRY_ONLY / UNDECIDED 五类）。
#: 这里不再维护第二份清单 —— 两份清单必然漂移（实测就漂了：同一批码
#: 在一份里删了、另一份没删，两个守卫互相打架）。
#: 判据见 `test_every_button_code_guards_at_least_one_route`。


def _literal_keys() -> list[str]:
    """从**源码字面量**里抽出 `ROUTE_PERMS` 的键（含重复项）。

    ⚠️ 必须读源码而不是 `import` 后的 dict —— 重复键在 dict 里已经消失了，
    `len(ROUTE_PERMS)` 查不出这件事，这正是本守卫存在的理由。
    """
    text = ROUTE_PERMS_SRC.read_text(encoding="utf-8")
    start = text.index("ROUTE_PERMS: Final[dict[str, str]] = {")
    end = text.index("\n}", start)
    return re.findall(
        r'^\s*"((?:GET|POST|PUT|PATCH|DELETE) /api/[^"]+)":', text[start:end], re.M
    )


def test_route_perms_literal_has_no_duplicate_keys():
    """字面量里同一个键只许出现一次（重复 = 前一条静默失效）。"""
    keys = _literal_keys()
    dup = {k: n for k, n in collections.Counter(keys).items() if n > 1}
    assert not dup, (
        "`ROUTE_PERMS` 字面量里出现重复键 —— 后写的会静默覆盖先写的，"
        "被覆盖那个权限码变成**死码**（授权页能勾、后端永不按它判权）：\n  "
        + "\n  ".join(f"{k} × {n}" for k, n in sorted(dup.items()))
    )

    #: 与运行时 dict 对账：两者数量必须一致，否则说明「源码里有键没被解析到」
    #: （比如换行书写、或用了 `**other` 展开）。
    from backend.app.core.route_perms import ROUTE_PERMS

    assert len(ROUTE_PERMS) == len(keys), (
        f"字面量解析出 {len(keys)} 条，运行时 dict 只有 {len(ROUTE_PERMS)} 条 "
        "⇒ 有键被重复覆盖或写法本守卫认不出"
    )


def test_every_button_code_guards_at_least_one_route():
    """每个按钮码要么有路由、要么在分类表里**有明确处置结论**。

    ## 为什么不再维护第二份欠账清单

    原先这里有一份 `KNOWN_ORPHAN_BUTTON_CODES`，与
    `test_orphan_perm_classification.py` 的分类表**内容重叠**。实测漂移过：
    同一批码在一份里删了、另一份没删 ⇒ 两个守卫互相打架（一个说「还在」、
    一个说「已修好」）。⇒ 合并成一份，判据只有一处。

    ## 两种「零路由」，修法完全不同

    * `REMAP_PENDING` —— 端点存在、路由挂错了 ⇒ **改挂路由**（设计 71 G3+）
    * `DISCRIMINATED` / `SERVICE_PICKED` / `ENTRY_ONLY` —— 合法的零路由
    * `UNDECIDED` —— 还没查证（只许变短）
    """
    from backend.app.core import perms_registry as reg
    from backend.app.core.route_perms import ROUTE_PERMS
    from tests.backend.test_orphan_perm_classification import (  # noqa: PLC0415
        DISCRIMINATED,
        ENTRY_ONLY,
        FIELD_DISCRIMINATED_DONE,
        REMAP_PENDING,
        SERVICE_PICKED,
        UNDECIDED,
    )

    used = set(ROUTE_PERMS.values())
    orphan = sorted(
        spec.code
        for spec in reg.PERMS
        if spec.menu_type == "F" and spec.code not in used
    )
    classified = (
        set(REMAP_PENDING)
        | set(DISCRIMINATED)
        | set(SERVICE_PICKED)
        | set(ENTRY_ONLY)
        | set(FIELD_DISCRIMINATED_DONE)
        | set(UNDECIDED)
    )
    unclassified = sorted(set(orphan) - classified)
    assert not unclassified, (
        f"新增了 {len(unclassified)} 个「零路由」按钮码且没有处置结论：\n  "
        + "\n  ".join(unclassified)
        + "\n\n要么把路由改挂到它自己（正解），要么归入分类表并写清理由。"
    )
