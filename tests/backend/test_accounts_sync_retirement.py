"""矩阵页「同步笔记 / 同步商品笔记」全链路退场守卫（2026-10-27 用户口径）。

> 用户原话：「矩阵页中的同步笔记和同步商品笔记全链路逻辑移除」。

## 为什么这一整条要单独一个文件

「全链路退场」在本仓指的是**八个层一起删**（设计 75 §4.5）：前端按钮 → 请求函数 →
纯逻辑 → 端点 → 服务层 → 任务类型 → 权限码 → 菜单行。任何**一层**漏删都会留下
这一类缺陷中最难排查的形态：

* 只删按钮不删请求函数 → 下一个人 grep 到函数就以为链路还在；
* 只删端点不留墓碑 → 旧版页面拿到裸 404，分不清「功能没了」和「路径写错」；
* 只删权限码不删菜单行 → 启动守卫拒绝启动（`route_guard`）；
* 只删菜单行不删库里的行 → 授权页上挂着一个「勾了不生效」的空按钮。

所以断言必须**逐层**做，且用「剥注释后仍不出现」的判据 —— 本仓代码注释里大量
写着被守护的符号名（历史教训：不剥注释时守卫会被自己的说明文字命中）。

## 与本轮其它改动的边界

* 「内容库批量同步」「我的笔记同步数据」**不在本轮范围**（那是另一条链路），
  这里只钉矩阵页的两条；
* `xhs:library:syncData`（`POST /notes/sync-data`）**仍在用**，不许被顺手删掉 ——
  下面专门有一条反向断言防「一次删过头」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ACCOUNTS_PAGE = Path("frontend/src/pages/platforms/xhs/accounts-page.tsx")
API_TS = Path("frontend/src/lib/api.ts")
SYNC_GATES = Path("frontend/src/lib/sync-gates.ts")
ACCOUNTS_API = Path("backend/app/api/accounts.py")
ACCOUNT_TASK_SVC = Path("backend/app/services/account_task_service.py")
NOTE_QUEUE_SVC = Path("backend/app/services/note_queue_service.py")
SCHEDULER_SVC = Path("backend/app/services/scheduler_service.py")
PERMS_REGISTRY = Path("backend/app/core/perms_registry.py")
ROUTE_PERMS = Path("backend/app/core/route_perms.py")
MENU_SEED = Path("config/initial_data/menus.seed.json")
SETTINGS_API = Path("backend/app/api/settings.py")


def _code(path: Path) -> str:
    """剥掉 TS/TSX/PY 注释后的源码（本仓注释里常写被守护的符号名）。"""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"\{/\*.*?\*/\}", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    text = re.sub(r'^\s*""".*?"""', "", text, flags=re.S | re.M)
    text = re.sub(r"^\s*#.*$", "", text, flags=re.M)
    return text


class TestFrontendChainIsGone:
    """第 1–3 层：按钮 / 请求函数 / 纯逻辑。"""

    def test_account_card_has_no_note_sync_buttons(self) -> None:
        """账号卡不许再出现两个同步按钮与其按钮态判据。"""
        src = _code(ACCOUNTS_PAGE)
        for banned in (
            "同步商品笔记",
            "handleNoteSyncClick",
            "handleGoodsNoteSyncClick",
            "startGoodsNoteSync",
            "isNoteSyncing",
            "isGoodsNoteSyncing",
            "noteSyncIds",
            "goodsNoteSyncIds",
        ):
            assert banned not in src, f"矩阵页还留着「{banned}」"

    def test_account_card_keeps_metrics_and_delete(self) -> None:
        """⚠️ 反面对照：「同步数据」（粉丝/关注/获赞）与「删除账号」必须**保留**。

        这条防「一次删过头」—— 用户要删的是笔记同步，不是整张卡片的动作集。
        """
        src = _code(ACCOUNTS_PAGE)
        assert "同步数据" in src, "「同步数据」按钮被误删（它取的是粉丝/关注/获赞）"
        assert "handleSyncMetrics" in src, "`handleSyncMetrics` 被误删"
        assert "删除账号" in src, "「删除账号」被误删"

    def test_request_functions_and_types_are_gone(self) -> None:
        """`lib/api.ts` 里五个函数 + 六个类型必须消失（它们只服务退场的四条端点）。"""
        src = _code(API_TS)
        for name in (
            "syncAccountNotes",
            "syncGoodsNotes",
            "syncAccountNotesStatus",
            "syncGoodsNotesStatus",
            "postNoteSync",
        ):
            assert f"export async function {name}(" not in src, f"请求函数 `{name}` 还在"
        for name in (
            "XhsSyncTriggerResult",
            "XhsSyncTaskStatus",
            "XhsSyncTaskSummary",
            "XhsSyncLastResult",
            "XhsSyncProgress",
            "SyncNotesOptions",
        ):
            assert f"interface {name}" not in src, f"类型 `{name}` 还在"
            assert f"type {name}" not in src, f"类型别名 `{name}` 还在"

    def test_sync_gates_keeps_only_the_login_channel_ruler(self) -> None:
        """`sync-gates.ts` 只留门禁判据；同步决策 / 结果行文案整批退场。

        `channelUnavailable` / `channelState` **必须留着** —— 商品列表与违规管理页
        仍在用同一把尺子（写第二份必然漂移）。
        """
        src = _code(SYNC_GATES)
        for banned in (
            "noteSyncPlan",
            "goodsNoteSyncPlan",
            "goodsGate",
            "syncTaskPhase",
            "syncResultText",
            "goodsResultText",
            "syncTerminalNotice",
            "syncProgressText",
        ):
            assert banned not in src, f"`sync-gates` 还留着 `{banned}`"
        assert "channelUnavailable" in src, "`channelUnavailable` 被误删（三处页面共用）"
        assert "channelState" in src, "`channelState` 被误删（续期弹窗判据）"


class TestAccountsPageIsATable:
    """账号页的**形态**守卫（2026-10-27 用户口径，全是「删了容易被改回去」的决策）。

    用户口径六连：「使用列表展示不在使用卡片」→「不分页，无限下拉」→「每页 50」
    →「使用 antd 成熟组件，不要自研」→「完全拆分成 table 表」→「账号数放标题处」。
    """

    def test_uses_antd_table_without_pagination(self) -> None:
        """必须是 antd `Table` + `pagination={false}`（不渲染分页器，靠下拉加载）。

        ⚠️ 判据必须钉在**表格那一行**上，不能只查「文件里有没有 `pagination={false}`」：
        变异实测（本轮）—— 在别处再塞一个 `pagination={{ pageSize: 50 }}` 时，原字符串
        仍在，只查存在性的守卫照样绿（ESCAPED）。所以这里连同 `scroll` 一起匹配
        **同一个 `<Table>` 的属性块**。
        """
        src = _code(ACCOUNTS_PAGE)
        assert "<Table<" in src, "账号页不是 antd Table"
        #: 表格的属性块里「分页关掉」与「表体撑满」必须**同时**在场（同一个 JSX 元素）。
        block = re.search(r"<Table<\w+>(?P<body>.*?)\n\s*columns=", src, re.S)
        assert block, "找不到 `<Table>` 的属性块"
        body = block.group("body")
        assert "pagination={false}" in body, "表格的分页器没关（用户要「tabe默认无限不使用分页」）"
        assert 'y: "100%"' in body, "表体没撑满（`scroll.y` 不是 100%）"
        #: 卡片网格形态（`<Row>` + `<Col>` 拼卡片）不许回来。
        assert "<Col " not in src, "又用 Col 拼卡片网格了（用户要表格）"
        assert "List.Item.Meta" not in src, "又用 List 的 Meta 槽了（应完全拆成表格）"

    def test_checkbox_is_native_row_selection(self) -> None:
        """复选框必须走 antd 原生 `rowSelection`，不许自研 Checkbox 塞进行里。"""
        src = _code(ACCOUNTS_PAGE)
        assert "rowSelection={{" in src, "没用表格原生 rowSelection"
        assert "selectedRowKeys" in src, "rowSelection 没接 selectedRowKeys（受控选中态）"
        assert "<Checkbox" not in src, "还在自己摆 Checkbox（表格已有原生行选择）"

    def test_page_size_is_50(self) -> None:
        """每页 50 条（用户口径）；改小会让首次加载明显变慢，改大则首屏请求过重。"""
        src = _code(ACCOUNTS_PAGE)
        assert "const ACCOUNT_PAGE_SIZE = 50;" in src, "每页条数不是 50"

    def test_title_carries_the_count(self) -> None:
        """账号数放标题处，且格式与「字典管理（N）」一致（标题即计数，不在别处重复）。"""
        src = _code(ACCOUNTS_PAGE)
        assert "账号管理（{total}）" in src, "标题没有带账号数（用户口径：放标题处）"
        #: 底部不许再重复一遍总数（那是同一信息两处展示）。
        assert "共 {accounts.length} 个账号" not in src, "列表底部又重复了一遍总数"

    def test_infinite_scroll_loads_next_page(self) -> None:
        """滚到底要追加下一页，且只在**表体**滚动时触发。"""
        src = _code(ACCOUNTS_PAGE)
        assert "onScroll={" in src, "没有监听滚动 ⇒ 无限下拉失效"
        assert "table-body" in src, "没限定滚动目标（表头/固定列的镜像滚动会重复触发）"
        assert "loadMore()" in src, "没调用 loadMore"

    def test_sync_metrics_button_requires_web_session(self) -> None:
        """守「同步数据」与「删除」两个按钮都是**纯图标**形态，且 `title` 写全动作名。
        图标按钮没有可见文字，`title` 是唯一的可读说明。"""
        src = _code(ACCOUNTS_PAGE)
        #: 不许再按会话可用性隐藏按钮（那条逻辑已被撤回）。
        assert "disabled={channelUnavailable(" not in src, "又回到「按会话显隐/置灰」了"
        #: 图标按钮：`type="text"` + 无 children（自闭合 `/>`）。
        assert src.count('type="text"') >= 2, "两个操作按钮不都是图标形态"
        assert "同步数据（粉丝 / 关注 / 获赞数，走网页版接口）" in src, "同步按钮的 title 不完整"
        assert 'title="删除账号"' in src, "删除按钮的 title 不完整"

    def test_delete_has_no_second_confirmation(self) -> None:
        """删除点击即删、不弹二次确认。
        后端是软删（`deleted_at`），数据不丢、重新导入会复活同一行，误点可挽回。"""
        src = _code(ACCOUNTS_PAGE)
        assert "Modal.confirm({" not in src, "删除还在弹二次确认框"
        assert "删除账号「" not in src, "删除确认文案还在（那是 confirm 用的）"

    def test_login_column_has_hint_icon(self) -> None:
        """「登录态」表头带说明图标（「状态」列已撤，不再是两列）。
        说明必须点明三个平台各自判定、`未检测` ≠ `已失效`，否则会让人白做一次重登。"""
        src = _code(ACCOUNTS_PAGE)
        #: 先把 `HeaderWithHint` 的函数体切出来，判据**只在函数体内**成立 ——
        #: 页面别处还有别的 `<Tooltip>`（行内彩色标签），不切块就会被它满足
        #: （变异实测：把函数体换成纯文本，全文件搜 Tooltip 仍能找到 ⇒ ESCAPED）。
        fn_start = src.index("function HeaderWithHint(")
        #: 终点用「函数结尾反括号 + 空行」，而不是「下一个 function 关键字」——
        #: 下面紧邻的是 `export function XhsAccountsPage()`，那个关键字前面有 `export`
        #: 导致匹配不到，于是切块越界、`index` 直接 ValueError（实测踩过）。
        fn_end = src.index("\n}\n", fn_start) + 3
        body = src[fn_start:fn_end]
        tip_start = body.index("<Tooltip title={hint}")
        tip_block = body[tip_start : body.index("</Tooltip>", tip_start)]
        assert "QuestionCircleOutlined" in tip_block, "Tooltip 里包的不是说明图标"
        assert "<span>?</span>" not in tip_block, "用问号占位冒充图标"
        assert 'title="登录态"' in src, "登录态表头没有走 HeaderWithHint"
        #: 状态列不许回来（列头 / 字段都不许）。
        assert 'title="状态"' not in src, "状态列又出现了"
        #: 登录态说明必须点明「未检测 ≠ 已失效」—— 这条最容易让人白做一次重登。
        assert "网络问题不算失效" in src, "登录态说明没点明未检测的语义"

    def test_note_cell_never_shows_raw_status_message(self) -> None:
        """「说明」列不许显示 `status_message`。
        它是巡检 / 适配器异常的原文（英文异常、含代码路径的开发者说明），
        用户看不懂也做不了任何事。该位置只放能指导动作的原因。"""
        src = _code(ACCOUNTS_PAGE)
        assert "account.status_message" not in src, "说明列又直接显示 status_message 了"
        assert "function accountDataHint(" in src, "缺少面向用户的原因函数"
        #: 有数据时不许再解释（三数都在还挂一句原因，等于凭空制造疑问）。
        assert "if (hasAnyMetric)" in src, "有计数时没有跳过说明"

    def test_qianfan_gets_no_data_hint(self) -> None:
        """千帆账号不出说明。
        「没有数据」不是「出了问题」—— 该链路本来就不取这三个数，不必解释。
        判据落在行为：说明函数里不许再按 `sub_type` 分支出千帆文案。"""
        src = _code(ACCOUNTS_PAGE)
        start = src.index("function accountDataHint(")
        end = src.index("\n}\n", start) + 3
        body = src[start:end]
        assert "qianfan" not in body, "说明函数又给千帆开了分支"
        assert "千帆" not in body, "说明里又出现千帆字样"

    def test_metrics_are_hidden_without_web_session(self) -> None:
        """没有网页版会话 ⇒ 三个数显示 `-`，不显示 `0`。
        结构上取不到的数说成 0 是在断言「真的 0 粉丝」。
        判据必须落在**门禁行**上（只查函数名存在会被别处调用满足）。"""
        src = _code(ACCOUNTS_PAGE)
        assert "function metricCell(" in src, "缺少带门禁的计数单元格"
        start = src.index("function metricCell(")
        end = src.index("\n}\n", start) + 3
        body = src[start:end]
        #: 判据是**正向**的：只有 `valid` 才显示（2026-10-27「网页版登录态有效则才展示」）。
        #: 写成 `!channelUnavailable(...)` 会让 `unknown` 也显示 —— 那是没确认过有效。
        assert 'channelState(channels, "www") !== "valid"' in body, "计数单元格没按「有效才显示」判"
        assert 'return "-";' in body, "网页版无效时没显示 -"
        #: 三列都必须走它（直接调 accountNumber 就等于绕过门禁）。
        for key in ("followers", "following", "likes"):
            assert f'metricCell(account, loginChannels[account.id], "{key}")' in src, (
                f"{key} 列没走 metricCell ⇒ 无会话时仍会显示 0"
            )


    def test_refresh_icon_only_when_web_session_valid(self) -> None:
        """刷新图标只在网页版登录态**确定有效**（`valid`）时展示。
        必须用正向判据 `=== "valid"`；反向的 `!channelUnavailable(...)` 会放行 `unknown`
        （没探过 ≠ 有效）。"""
        src = _code(ACCOUNTS_PAGE)
        assert 'channelState(loginChannels[account.id], "www") === "valid" && (' in src, (
            "刷新图标没有按「网页版登录态有效」正向门禁"
        )
        #: 反向断言：不许退回「不可用才隐藏」。
        assert '!channelUnavailable(loginChannels[account.id], "www")' not in src, (
            "又退回「不可用才隐藏」了 —— 那样 unknown 也会显示按钮"
        )
        #: 按钮本体必须在条件渲染里（否则条件写了也没用）。
        assert 'title={isSyncing ? "正在同步粉丝 / 关注 / 获赞数…"' in src, "刷新按钮消失了"


    def test_no_hint_when_web_session_absent(self) -> None:
        """网页版会话无效 ⇒ 说明列留空，不解释。
        「没有数据」是账号形态决定的既定事实，不是异常；摆一句解释只会打扰用户。
        唯一保留的文案是「尚未同步，点「同步数据」获取」—— 它能被执行。"""
        src = _code(ACCOUNTS_PAGE)
        assert "未登录网页版，无法获取粉丝数据" not in src, "那句已经明确不要了"
        start = src.index("function accountDataHint(")
        end = src.index("\n}\n", start) + 3
        body = src[start:end]
        #: 会话无效分支必须 `return ""`（不是返回一句解释）。
        import re as _re

        invalid_branch = _re.search(
            r'channelState\(channels, "www"\) !== "valid"\)\s*\{\s*return "";', body
        )
        assert invalid_branch, "网页版无效时仍返回了文案（应为空串）"
        #: 唯一允许出现的文案是能执行的那句。
        assert "尚未同步，点「同步数据」获取" in body, "可执行的那句提示不该删"


    def test_refresh_waits_with_shared_mask_and_retries_probe(self) -> None:
        """刷新 = 公共等待组件 + 两次探活，两次都失败才判无效。
        1. 按钮走 `refreshAccounts()`（不是裸 `loadAccounts`）；
        2. 等待用公共 `WaitMask`，不自研 Spin；
        3. 探活最多两次、且第二次被「第一次失败」包住 —— 一次失败就报无效会把网络抖动说成账号问题。"""
        src = _code(ACCOUNTS_PAGE)
        assert "onClick={() => void refreshAccounts()}" in src, "刷新按钮没走 refreshAccounts"
        assert "<WaitMask" in src and 'title="正在刷新账号与登录态…"' in src, "刷新没有用公共等待组件"
        assert "refreshing" in src, "缺少刷新等待态"

        #: 探活重试逻辑必须存在且是「第一次失败才第二次」。
        start = src.index("async function refreshAccounts(")
        end = src.index("\n}\n", start) + 3
        body = src[start:end]
        #: ⚠️ 只数调用次数抓不住「把 if 条件改成 false」（调用点还在、代码不跑）——
        #: 变异实测过：`single_probe` 那条 ESCAPED。所以判据落在**调用被条件包住**这件事上。
        import re as _re

        guarded = _re.search(
            r'if \(first === "failed"\) \{\s*second = await autoCheckLoginStates\(\);', body
        )
        assert guarded, "第二次探活没有被「第一次失败」这个条件包住"
        assert body.count("await autoCheckLoginStates()") == 2, "探活不是恰好两次"
        assert 'first === "failed" && second === "failed"' in body, (
            "不是「两次都失败」才判无效"
        )
        assert "请刷新重试" in body, "失败后没有给出可执行动作（刷新重试）"


    def test_row_refresh_shows_mask_and_outcome(self) -> None:
        """行内刷新也要蒙版 + 结果提示。
        1. 用公共 `WaitMask`；
        2. 三种结局分别说话（取到数 / 平台没给数 / 请求失败）——
           把「没取到数」说成成功，用户下次还会再点；
        3. 失败原因同时写进「说明」列（toast 会消失，那一列留档）。"""
        src = _code(ACCOUNTS_PAGE)
        assert 'title="正在同步粉丝 / 关注 / 获赞…"' in src, "行内刷新没有用公共等待组件"

        start = src.index("async function handleSyncMetrics(")
        end = src.index("\n}\n", start) + 3
        body = src[start:end]
        #: ⚠️ 只查 `message.success(` 这种**关键字存在性**抓不住「把内容换成 void x;」——
        #: 变异实测三条 ESCAPED（no_success / no_error_toast / no_note_on_fail）。
        #: 判据必须落在**实际消息内容**上。
        assert "粉丝 / 关注 / 获赞已更新" in body, "成功提示没有具体内容"
        assert "同步完成，但未取到数据" in body, "「同步完成但没取到数」没有单独提示"
        assert "同步失败：" in body, "失败没有提示"
        assert "setMetricsNotes((current) => ({ ...current, [account.id]: detail }))" in body, (
            "失败原因没有落到说明列（只弹 toast 会随消失）"
        )


class TestBackendChainIsGone:
    """第 4–6 层：端点墓碑 / 服务层 / 任务类型。"""

    def test_endpoints_are_tombstones_not_missing(self) -> None:
        """四条端点**保留 410 墓碑**（不是裸 404），但不在 OpenAPI 里。

        ⚠️ 这里刻意**不要求删除端点**：删掉会让旧版页面拿到 404，分不清
        「功能已移除」与「路径写错」。S4/S5 的处置口径就是留墓碑。
        """
        from backend.app.main import app

        paths = app.openapi()["paths"]
        for path in (
            "/api/accounts/{account_id}/notes/sync",
            "/api/accounts/{account_id}/goods-notes/sync",
            "/api/accounts/{account_id}/notes/sync/status",
            "/api/accounts/{account_id}/goods-notes/sync/status",
        ):
            assert path not in paths, f"{path} 又回到 OpenAPI 了（应保持墓碑）"

        src = _code(ACCOUNTS_API)
        assert "_NOTE_SYNC_GONE" in src, "墓碑的共用抛出体被删了"
        #: 墓碑只回答「功能没了」—— 不许在墓碑里读库或解析请求体（那说明它还在干活）。
        assert "class NoteSyncRequest" not in src, "`NoteSyncRequest` 还在（触发链路的请求体）"
        assert "def _start_account_sync(" not in src, "`_start_account_sync` 还在（零调用方）"

    def test_worker_no_longer_claims_retired_task_types(self) -> None:
        """账号任务 worker 不许认领已退场的两个类型，且不许再 import 已删除的执行体。"""
        src = _code(ACCOUNT_TASK_SVC)
        assert '"xhs_note_sync"' not in src, "worker 仍在认领 `xhs_note_sync`"
        assert '"xhs_goods_note_sync"' not in src, "worker 仍在认领 `xhs_goods_note_sync`"
        assert "run_note_sync(" not in src, "worker 还在调已删除的 `run_note_sync`"

        queue_src = _code(NOTE_QUEUE_SVC)
        assert '"xhs_note_sync"' not in queue_src, "`ACCOUNT_TASK_TYPES` 还含 `xhs_note_sync`"
        assert '"xhs_goods_note_sync"' not in queue_src, (
            "`ACCOUNT_TASK_TYPES` 还含 `xhs_goods_note_sync`"
        )

    def test_scheduler_dead_targeted_helpers_are_gone(self) -> None:
        """调度侧三个死函数（调已删除的入口）必须消失。"""
        src = _code(SCHEDULER_SVC)
        for banned in (
            "def _run_targeted_note_sync(",
            "def _run_targeted_goods_note_sync(",
            "def account_needs_note_sync(",
        ):
            assert banned not in src, f"`scheduler_service` 还留着 `{banned}`"

    def test_settings_wake_hook_no_longer_imports_a_deleted_name(self) -> None:
        """⚠️ 真 bug 守卫：保存 `xhs_sync_account_parallelism` 曾必然 ImportError。

        `_wake_runtime_consumers` 里 `from note_sync_service import ensure_sync_worker`
        而那个名字随同步引擎删了 —— 配置一保存就 500。现在该函数是空操作，
        **不许再把那个 import 写回来**。
        """
        src = _code(SETTINGS_API)
        assert "ensure_sync_worker" not in src, (
            "`settings.py` 又 import 了已删除的 `ensure_sync_worker`（保存配置会 500）"
        )


class TestPermissionAndMenuRowsAreGone:
    """第 7–8 层：权限码 / 菜单行（含库里的行 —— 那个只能由迁移删）。"""

    def test_retired_codes_are_out_of_the_registry(self) -> None:
        from backend.app.core.perms import ALL_PERMS

        for code in ("xhs:account:syncNotes", "xhs:account:syncGoodsNotes"):
            assert code not in ALL_PERMS, f"权限码 `{code}` 仍在登记表里"

    def test_menu_seed_has_no_retired_rows(self) -> None:
        """种子是生成物 —— 它必须与登记表一致（`generate_menu_seed.py --check`）。"""
        seed = json.loads(MENU_SEED.read_text(encoding="utf-8"))
        perms = {row.get("perms") for row in seed["menus"]}
        for code in ("xhs:account:syncNotes", "xhs:account:syncGoodsNotes"):
            assert code not in perms, f"菜单种子还有 `{code}` 的行"
        #: 早期排查时塞进库的桩行，不许在种子里复活。
        assert not [p for p in perms if p and p.startswith("xhs:account:TMP")], (
            "菜单种子里有 TMPA/B/C 桩行"
        )

    def test_library_sync_data_code_is_kept(self) -> None:
        """⚠️ 反面对照：`xhs:library:syncData` **必须保留**（`/notes/sync-data` 仍在用）。

        这条防「一次删过头」：它与「账号笔记同步」是两件事（千帆数据中心上线）。
        """
        from backend.app.core.perms import ALL_PERMS
        from backend.app.main import app

        assert "xhs:library:syncData" in ALL_PERMS, (
            "`xhs:library:syncData` 被误删（`/notes/sync-data` 仍在用）"
        )
        assert "/api/notes/sync-data" in app.openapi()["paths"], "`/notes/sync-data` 端点被误删"

    def test_borrowed_routes_were_remounted(self) -> None:
        """两个**借用**退场码的仍活路由必须改挂到别的码（否则启动守卫拒绝启动）。"""
        from backend.app.core.perms import ALL_PERMS

        src = _code(ROUTE_PERMS)
        assert '"POST /api/notes/{note_id}/hydrate": "xhs:note:sync"' not in src, (
            "`hydrate` 仍挂在已退场的 `xhs:note:sync` 上"
        )
        assert '"POST /api/xhs/pc/users/no-traffic/cleanup": "xhs:account:syncNotes"' not in src, (
            "无流量清理仍挂在已退场的 `xhs:account:syncNotes` 上（且归属本来就是错的）"
        )
        for code in ("xhs:note:edit", "xhs:note:delete"):
            assert code in ALL_PERMS, f"改挂的目标码 `{code}` 不在登记表里"

    def test_tombstoned_routes_stay_guarded(self) -> None:
        """墓碑端点仍要过启动守卫（`include_in_schema=False` 也要挂码/豁免）。"""
        from backend.app.core.route_guard import collect_violations
        from backend.app.main import app
        from backend.app.core.perms import assert_registry_sane

        assert_registry_sane()
        problems = collect_violations(app)
        assert not problems, "启动守卫不通过：\n" + "\n".join(problems)


class TestMigrationDeletesLibraryRows:
    """库里的行只能由迁移删 —— 种子「只补缺不覆盖」永远删不掉它。"""

    def test_migration_exists_and_targets_the_retired_codes(self) -> None:
        migration = Path(
            "backend/alembic/versions/p2q3r4s5t6u7_delete_retired_sync_menu_rows.py"
        )
        assert migration.exists(), "删菜单行的迁移不存在（库里会永远留着那两行）"
        src = migration.read_text(encoding="utf-8")
        for code in ("xhs:account:syncNotes", "xhs:account:syncGoodsNotes"):
            assert code in src, f"迁移没覆盖 `{code}`"
        #: ⚠️ 不许把 `xhs:library:syncData` 写进删除清单（它仍在用）。
        assert '"xhs:library:syncData"' not in src, (
            "迁移把仍在用的 `xhs:library:syncData` 也列进删除清单了"
        )
        #: `role_menus` 必须先删（孤儿授权行指向不存在的菜单）。
        assert "role_menus" in src, "迁移没清 `role_menus` 授权行"

    def test_migration_is_the_only_head_chain_link(self) -> None:
        """`down_revision` 必须接在真 HEAD 上（接错会 `Cycle is detected`）。"""
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(Config("backend/alembic.ini"))
        heads = script.get_heads()
        #: 头号判据：**链不分叉**（多 head = `Cycle is detected` / upgrade 失败）。
        assert len(heads) == 1, f"存在多个 head（迁移链分叉）：{heads}"
        #: ⚠️ **不要钉具体 revision**：head 每加一条迁移就前移，钉死等于让这条守卫
        #: 每次都被迫手改（2026-10-27 一天内改了两次：q3r4s5t6u7v8 → r4s5t6u7v8w9）。
        #: 真正要守的是「本轮那条迁移**仍在链上**」—— 用 ancestor 判，与 head 是谁无关。
        with_ancestors = {rev.revision for rev in script.walk_revisions()}
        for revision in ("p2q3r4s5t6u7", "q3r4s5t6u7v8", "r4s5t6u7v8w9"):
            assert revision in with_ancestors, f"迁移 {revision} 从链上消失了"
        #: 仍在守的一条具体关系：`disabled` 那列必须接在本轮的父之后（接错就分叉）。
        assert script.get_revision("q3r4s5t6u7v8").down_revision == "p2q3r4s5t6u7"
