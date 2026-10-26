"""零路由按钮码的**分类处置表**（设计 71 / 72，2026-10-26）。

## 为什么不是一张「白名单」

清理零路由码时，45 个码混着四种完全不同的情况，用一张 `FRONTEND_ONLY` 白名单
登记会掩盖差异（而且白名单越长越像永久豁免）。本文件把它们**分类**：

| 类 | 含义 | 处置 |
|---|---|---|
| `REMAP_PENDING` | 端点**存在且唯一**，只是路由还挂在旧合并码上 | 下一轮改挂（设计 71 G3+） |
| `DISCRIMINATED` | 一个端点按 body/字段分派多码（平台约束） | 保留；端点内已有/将有判权点 |
| `SERVICE_PICKED` | 两级共用端点，服务层按归属**选码** | 保留（设计 69/70） |
| `ENTRY_ONLY` | 跨域入口码（打开另一个域的弹窗/向导，本域无端点） | 保留，但**必须**同时有后端判权点或属纯显隐 |

⚠️ **`ENTRY_ONLY` 是最后手段**。若依里没有这种码（入口显隐靠菜单行 `visible`）；
本项目多出这一层是因为「弹窗/向导」是页面内入口，没有菜单行可挂。
只有「打开另一个域的面板」才配，同域内的按钮一律用动作码。

## 本轮（G1–G7）已修

`xhs:monitor:edit/delete/refresh`、`xhs:rule:edit/delete/execute`、
`xhs:material:editFolder/deleteFolder/scan`、`xhs:forbiddenWord:edit/delete`、
`xhs:order:sync/shipBatch/remark/export`、`ai:gateway:delete`
—— 这些已不在下表里（有路由了）。

`G7`（2026-10-26，本轮）：
* **删 17 个**：`xhs:draft:generate/rewrite/generateImage/assets`、
  `xhs:library:publish/sync/createDraft`、`xhs:note:batchDelete/batchPublish/export/tag`、
  `xhs:account:create`、`xhs:forbiddenWord:import`、`ai:generate:batchGenerate/retry`、
  `system:setting:create/delete`。
  共同病因：**前端 `hasPermission` 判的是它们，后端路由挂的却是另一套**（素材增删 →
  `xhs:note:edit`、AI 改写 / 生图 → `ai:generate:generate`、LLM 生成笔记 → `xhs:item:generateNote`、
  批量发布 → `xhs:publish:submit`、批量同步 → `xhs:note:batchSync`、建草稿 →
  `xhs:library:batchCreateDrafts` / `xhs:note:create`）。前端另立一套码 ⇒ 授权页勾了也
  不放行，等于没有判权。处置：前端 12 处判权码改成后端真码，本组码整条退场（不留兼容层）。
* **改挂路由 2 条**：`POST /api/notes/export` 由页级 `xhs:library:view` → `xhs:library:export`；
  `POST /api/accounts/{account_id}/login-state` 由 `xhs:login:view` → `xhs:account:relogin`。
* 零路由按钮码从 **45 → 18**；下表 ①+②+③+④ 恰好覆盖这 18 个，`UNDECIDED` 已清空。
"""

from __future__ import annotations

#: ① 端点唯一、只需改挂 —— **2026-10-26（G8）已清空**，6 条全部落实：
#: * `xhs:autoops:run`   → `POST /auto-tasks/{id}/run`（原挂 `autoops:edit`）
#: * `xhs:picture:purge` → `POST /picture-space/permanent-delete`
#:   （与 `permanentDelete` 指同一端点 ⇒ 那个码是重复码，已删）
#: * `xhs:picture:folder`→ `POST /picture-space/folders`（原挂 `picture:view`，那是只读码）
#: * `xhs:record:clean`  → `POST /tasks/clean`
#:   （原挂 `system:scheduler:cleanTasks`，该码已退场）
#: * `ai:gateway:enableModel` / `xhs:autoops:enable` → 都是「同一端点按字段分派」，
#:   归 ② DISCRIMINATED（见下），不是改挂能解决的。
REMAP_PENDING: dict[str, str] = {}

#: ② 一个端点按参数分派多码（平台只给一个接口）。
#: 2026-10-26（G8）：`ai:gateway:enableModel` / `xhs:autoops:enable` **已从本表移出** ——
#: 它们在端点上加了函数内选码（`_assert_can_toggle_model` / `_assert_can_toggle_auto_task`），
#: 由 `test_discriminated_perm_points.py` 钉住，属于「已落地」而不是「欠账」。
DISCRIMINATED: dict[str, str] = {
    "xhs:item:offShelf": "POST /items/operate action=off_shelf → goods.py::_assert_action_permitted",
    "xhs:item:delete": "POST /items/operate action=delete → 同上",
}

#: ⑤ **已落地的函数内选码点**（2026-10-26 G8 新增）。
#:
#: 与 ② 的区别：② 是「欠账 / 由平台接口形态决定、暂不拆」；本表是「已经拆开并且
#: 端点里真的判了」。它们天然没有专属路由（静态表挂的是同端点的另一个码），
#: 所以零路由码守卫必须先减掉这一组，否则会把「已生效」误报成「欠账」。
#:
#: 判据由 `test_discriminated_perm_points.py`（7 条 + 变异 5/5）钉住：
#: 端点必须真的调用选码函数、调用必须被 If 包住、且判的是这条码本身。
FIELD_DISCRIMINATED_DONE: dict[str, str] = {
    "xhs:autoops:enable": (
        "PATCH /api/auto-tasks/{task_id} 的 status 字段 → "
        "auto_tasks.py::_assert_can_toggle_auto_task"
    ),
    "ai:gateway:enableModel": (
        "PATCH /llm/gateway-configs/{id}/models/{mid} 的 enabled 字段 → "
        "llm_gateway.py::_assert_can_toggle_model"
    ),
}

#: ③ 两级共用端点，服务层按归属选码（设计 69 / 70）。
SERVICE_PICKED: dict[str, str] = {
    "system:scheduler:create": "POST /schedule/jobs 挂 xhs:taskcenter:create；系统级由 schedule.py 再判",
    "system:scheduler:edit": "PUT /schedule/jobs/{id} 挂 xhs:taskcenter:edit；同上",
    "system:scheduler:run": "POST /schedule/jobs/{id}/run 挂 xhs:taskcenter:run；同上",
    "system:scheduler:toggle": "PATCH /schedule/jobs/{id}/status 挂 xhs:taskcenter:toggle；同上",
    "xhs:taskcenter:viewLogs": "GET /schedule/logs 挂 system:scheduler:view；两级共用",
    "xhs:settings:edit": "api/settings.py 工作区级写权限，has_perm 在函数内判（端点 EXEMPT_SELF）",
}

#: ④ 跨域入口码：打开的是**另一个域**的面板，本域无端点。
ENTRY_ONLY: dict[str, str] = {
    "xhs:item:forbiddenWords": "打开违规词弹窗（动作归 xhs:forbiddenWord:*）",
    "xhs:item:drafts": "打开商品草稿弹窗（动作归 xhs:item:draft）",
    "xhs:item:publish": "打开发布向导（动作归 xhs:publish:submit）",
    "xhs:monitor:enable": "PATCH /targets/{id} 的 status 字段，与 xhs:monitor:edit 同端点",
}

#: ⑤ 待定（需逐个查证：可能是 ②③④ 之一，也可能该删）。
#: ⑤ 待定。**本轮（G7）已清空**：21 条全部处置完毕（17 个删码、1 个改挂路由、
#: 3 个前端对齐后端真码后消解）。留这个集合是为了让守卫在「将来又出现新零路由码」
#: 时能立刻点名 —— 只许变长，不许静默改写成固定码。
UNDECIDED: frozenset[str] = frozenset()


def test_classification_covers_every_orphan():
    """分类表必须**恰好覆盖**当前所有零路由按钮码（不多不少）。

    多了 ⇒ 有人修好了却没从表里删（表变成永久豁免）；
    少了 ⇒ 出现新的零路由码（那可能是真回归）。
    """
    from backend.app.core import perms_registry as reg
    from backend.app.core.route_perms import ROUTE_PERMS

    used = set(ROUTE_PERMS.values())
    orphans = {
        spec.code
        for spec in reg.PERMS
        if spec.menu_type == "F" and spec.code not in used
    }
    classified = (
        set(REMAP_PENDING)
        | set(DISCRIMINATED)
        | set(SERVICE_PICKED)
        | set(ENTRY_ONLY)
        | set(FIELD_DISCRIMINATED_DONE)
        | UNDECIDED
    )

    unclassified = sorted(orphans - classified)
    assert not unclassified, (
        f"{len(unclassified)} 个零路由码没有归类（先归到某一类，再决定改挂还是保留）：\n  "
        + "\n  ".join(unclassified)
    )

    stale = sorted(classified - orphans)
    assert not stale, (
        f"这些码已有路由或有判权点，请从分类表删掉（表只许变短）：\n  "
        + "\n  ".join(stale)
    )


def test_remap_pending_still_needs_remap():
    """`REMAP_PENDING` 里的码**确实**还没挂上自己的路由（防「修了没删」）。"""
    from backend.app.core.route_perms import ROUTE_PERMS

    used = set(ROUTE_PERMS.values())
    fixed = sorted(code for code in REMAP_PENDING if code in used)
    assert not fixed, (
        f"这些码已经挂上路由了，请从 REMAP_PENDING 删除：{fixed}"
    )