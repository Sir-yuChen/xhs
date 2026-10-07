"""菜单整合与图片空间的**源码级守卫**（设计 22，2026-10-06 六条需求）。

本项目没有前端测试框架，「前端必须做到的事」用源码断言钉住（与
`test_page_optimizations.py` 同一套路）。

## 这批守卫盯的六件事（每条都是「改回去不会让别的用例变红」的那类）

| 需求 | 判据 |
|---|---|
| N1 图片空间 | 菜单有项、路由有页；**只读**（页面无上传/移动/删除/下载入口） |
| N2 发布中心并入笔记工坊 | 主菜单**无**独立项；旧路由重定向到 `?tab=publish`；tab key 已登记 |
| N3 自动运营（工作流已退场）| 主菜单/子菜单**无**工作流入口；无 `?tab=workflows` 重定向；源码文件已从盘上删除 |
| N4 数据洞察去按钮 | 页面**无**「刷新」「生成报告」按钮、无 `generateReport`、无死 import |
| N5 提示移除 | 页面**无**「正在开发优化中」文案 |
| N6 执行一次参数弹窗 | 见 `tests/frontend/schedule_center.cjs`（行为脚本，另有断言） |
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHELL = ROOT / "frontend/src/components/layout/app-shell.tsx"
ROUTER = ROOT / "frontend/src/app/router.tsx"
ANALYTICS = ROOT / "frontend/src/pages/platforms/xhs/analytics-page.tsx"
PICTURE_SPACE = ROOT / "frontend/src/pages/platforms/xhs/picture-space-page.tsx"
NOTE_WORKSHOP = ROOT / "frontend/src/pages/platforms/xhs/note-workshop-page.tsx"
AUTO_OPS = ROOT / "frontend/src/pages/platforms/xhs/auto-ops-page.tsx"
API_TS = ROOT / "frontend/src/lib/api.ts"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code_only(path: Path) -> str:
    """剥注释（块注释 + 行注释）后的源码。

    ⚠️ 判据必须落在**代码**上：本批页面里满是解释「为什么不渲染上传按钮」的
    注释，不剥注释就会被自己的注释命中（`test_task_records.py` 实测踩过）。

    ⚠️ **块注释正则要避开「字符串里的 `/*`」**（2026-10-06 实测）：JSX 的
    `accept="image/*"` 里有个 `/*`，朴素 `re.sub(r"/\\*.*?\\*/")` 会把它当块注释
    开头、一路吞到下一个 `*/`，**把中间的真实代码整段剥掉** ⇒ 守卫假红
    （「上传本地图片」按钮凭空消失）。所以只认**行首/空白后**的 `/*`
    （真注释都长这样），字符串里的 `image/*` 自然被排除。
    """
    text = _read(path)
    text = re.sub(r"(?m)(?<![\"'\w])/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    return text


# --------------------------------------------------------------------------- #
# N1 图片空间
# --------------------------------------------------------------------------- #
class TestPictureSpaceMenu:
    def test_menu_has_picture_space_entry(self):
        """主菜单有「图片空间」项，且指向正确路由。"""
        shell = _code_only(SHELL)
        assert "/platforms/xhs/picture-space" in shell
        assert '"图片空间"' in shell

    def test_route_is_wired(self):
        router = _read(ROUTER)
        assert 'path="/platforms/xhs/picture-space"' in router
        assert "PictureSpacePage" in router

    def test_page_exists(self):
        assert PICTURE_SPACE.exists()

    def test_page_has_all_operation_buttons(self):
        """⚠️ 用户 2026-10-06 追加口径「操作按钮功能全部要做」⇒ 写入口**必须在**。

        初版「只读不渲染写入口」的决策已被推翻；本用例反向钉住「不许再退回只读」。

        ⚠️ 判据**不只查标签文本**（标签在但按钮 disabled = 点不动 = 骗用户）：
        「新建文件夹」必须真的绑了 `setFolderOpen(true)`（可点）。
        """
        code = _code_only(PICTURE_SPACE)
        for label in ("上传本地图片", "新建文件夹", "批量移动到", "批量下载", "批量删除", "复制链接", "移动到"):
            assert label in code, f"图片空间缺少操作按钮「{label}」（用户口径：操作按钮功能全部要做）"
        # 可点性：新建文件夹 / 删除 / 移动 / 下载 都必须绑着 onClick。
        assert "setFolderOpen(true)" in code, "「新建文件夹」按钮必须可点（绑定打开弹窗）"
        assert "deleteItems(" in code, "「删除」按钮必须可点"
        assert "moveItems(" in code, "「移动到」按钮必须可点"
        assert "downloadItems(" in code, "「下载」按钮必须可点"

    def test_upload_button_is_wired_not_disabled(self):
        """⚠️ 用户 2026-10-06：「上传本地图片功能未实现」⇒ 必须**真的可用**。

        判据三层（缺一层就是「看着有、点不动」）：
        ① 按钮文案在；② **没有 `disabled`**（初版是 `disabled` 占位，被点名）；
        ③ 绑了 `fileInputRef.current?.click()`（点了真能唤起选文件）；
        ④ 选完文件调 `uploadPictureSpaceFile`（真上传，不是只开个框）。
        """
        code = _code_only(PICTURE_SPACE)
        assert "上传本地图片" in code
        # ② 那条按钮不许再带 disabled（`disabled` 在别处合法，这里按**邻接**判）。
        upload_btn = [ln for ln in code.splitlines() if "上传本地图片" in ln]
        assert upload_btn, "找不到上传按钮"
        # 按钮本体（含 icon/onClick 的整段）里不许出现 disabled。
        idx = code.index("上传本地图片")
        segment = code[max(0, idx - 300) : idx + 40]
        assert "disabled" not in segment, "「上传本地图片」按钮不得为 disabled（用户口径：上传功能要真的做）"
        # ③④ 必须真接上传链路。
        assert "fileInputRef.current?.click()" in code, "上传按钮必须能唤起文件选择框"
        assert "uploadPictureSpaceFile" in code, "上传必须真调上传接口"
        assert 'accept="image/*"' in code, "文件选择框必须限定图片类型"

    def test_account_select_lists_all_xhs_accounts_with_infinite_scroll(self):
        """⚠️ 用户 2026-10-06：账号下拉**应支持全部小红书账号**（不止千帆），且**无限下拉**。

        初版两个错（用户原话「目前只有开心睡大觉的店，无限下拉未实现」）：
        ① `list.filter(a => a.sub_type === "qianfan")` ⇒ 千帆账号外的其它小红书账号不显示；
        ② `fetchAccounts`（只取第一页，默认 page_size=20 静默截断）⇒ 账号超 20 个就丢。
        修法 = 去掉 sub_type 过滤 + 改走 `fetchAccountsPage` 分页 + `onPopupScroll` 追加。
        """
        code = _code_only(PICTURE_SPACE)
        # ① 不许再按 sub_type 过滤账号。
        assert 'sub_type === "qianfan"' not in code, "账号下拉不得只列千帆账号（用户口径：支持全部小红书账号）"
        assert "fetchAccountsPage" in code, "账号下拉必须走分页接口（否则超一页的账号会丢）"
        # ② 无限下拉真接上。
        assert "onPopupScroll" in code, "账号下拉必须支持滚动加载（无限下拉）"
        assert "loadMoreAccounts" in code, "滚动到底必须真的追加下一页"
        # `fetchAccounts`（无分页的旧实现）不得再用于账号下拉。
        assert "fetchAccounts(" not in code, "账号下拉不得再用不分页的 fetchAccounts（会截断到 20 个）"

    def test_pagination_matches_platform_options(self):
        """⚠️ 分页每页条数须与千帆页面一致：真机下拉 **100 / 200 / 500 条/页**。

        用户 2026-10-07「图片分页展示与千帆页面功能保持一致」。初版是 20/50/100（错）。
        """
        code = _code_only(PICTURE_SPACE)
        assert "PAGE_SIZES = [100, 200, 500]" in code, "每页条数须与千帆一致（100/200/500）"
        assert "PAGE_SIZES = [20, 50, 100]" not in code, "旧的 20/50/100 已被平台口径取代"

    def test_recycle_bin_view_is_complete(self):
        """⚠️ 回收站功能补完（2026-10-07 用户「回收站功能补充完整」）。

        判据：① 左栏有「回收站」入口；② 切 `recycle` 走 `recycle: true`；
        ③ 行内 + 批量都有「恢复」「彻底删除」；④ 彻底删除走两层确认（preview +
        permanent-delete）；⑤ 走 `updateTime` 倒序（真机口径）。
        """
        code = _code_only(PICTURE_SPACE)
        assert "回收站" in code, "左栏必须有回收站入口"
        assert 'scene === "recycle"' in code, "必须有回收站视图分支"
        assert "recycle: true" in code, "回收站视图必须走 recycle 查询"
        assert "restorePictureSpaceItems" in code, "必须有「恢复」调用"
        assert "permanentDeletePictureSpaceItems" in code, "必须有「彻底删除」调用"
        assert "彻底删除" in code and "恢复" in code
        # 彻底删除两层确认：preview(action=permanent-delete) + 独立 token。
        assert '"permanent-delete"' in code, "彻底删除须用独立 action 拿确认凭据"
        assert "updateTime" in code, "回收站按删除时间倒序（真机口径）"

    def test_action_column_is_pinned_right_with_buttons_in_one_row(self):
        """⚠️ 操作列须**钉最右**（`fixed:"right"`）+ **三个按钮一行展示**（2026-10-07 用户口径）。

        背景：名称列固定 280px 后全表宽 ~1016 > 容器（1366 视口 ~872）⇒ 表**内部**横向
        滚动。若不钉右，操作列会被滚出视口（用户「列表超出页面」）；钉右后常驻可见。
        且 `fixed:"right"` **依赖** `scroll.x` —— 两者必须同在（去掉 scroll.x 固定失效）。

        ⚠️ 用户 2026-10-07 口径演进：先「一行两个功能按钮」→ 后「三个按钮一行展示」
        （**以后者为准**，前者被取代）。主视图用 `1fr 1fr 1fr` 三列并排。
        """
        code = _code_only(PICTURE_SPACE)
        assert 'fixed: "right"' in code, "操作列必须 fixed:right（钉最右）"
        assert "scroll={{" in code, "固定列依赖 scroll 容器（须有 scroll.x）"
        # 三个按钮一行：grid 三列。
        assert 'gridTemplateColumns: "1fr 1fr 1fr"' in code, "操作列须三个按钮一行（grid 1fr 1fr 1fr）"

    def test_name_column_is_fixed_200_with_tooltip(self):
        """⚠️ 名称列固定 200px + Tooltip 展示完整名（2026-10-07 用户口径）。

        口径演进：先「名称列固定280px」→ 后「名称列宽度固定200px，使用tip展示完整名称」
        （**以后者为准**）。
        """
        code = _code_only(PICTURE_SPACE)
        assert "width: 200" in code, "名称列须固定 200px"
        assert "ellipsis: true" in code, "名称列须保留 ellipsis（长 UUID 截断）"
        # 完整名走 Tooltip（名称列 render 里显式包 Tooltip）。
        assert "Tooltip title={text" in code, "名称列须用 Tooltip 展示完整名称"

    def test_pagination_is_shared_and_fixed_at_the_bottom(self):
        """⚠️ 分页须是卡片/列表**公共组件**且**底部固定**（2026-10-07 用户口径）。

        口径：「分页应为卡片和列表的公共组件，底部固定」「顶部和底部固定不动，只有数据行可滚动」。
        判据：① 表格自带分页**关掉**（`pagination={false}`）；
        ② 页面里有**一个**独立的 `<Pagination`（两视图共用）；
        ③ 它在一个 `flexShrink: 0` 的底栏里（不随数据区滚动）。
        """
        code = _code_only(PICTURE_SPACE)
        assert "pagination={false}" in code, "表格自带分页须关掉（改用公共分页条）"
        assert "<Pagination" in code, "须有一个公共 <Pagination>"
        assert "flexShrink: 0" in code, "分页底栏须 flexShrink:0（固定不滚动）"
        # 数据区滚动条隐藏（外观隐藏、滚动保留）。
        assert "ps-scroll-hidden" in code, "数据区须挂滚动条隐藏类"

    def test_card_view_has_checkbox_and_select_all_invert(self):
        """⚠️ 卡片视图须支持复选框 + 全选 / 反选 / 全选未引用（2026-10-07 用户口径）。

        「卡片页支持复选框和添加全选按钮，反选按钮」+「新增全选未被引用的图」——判据：
        ① 卡片里有 `Checkbox`（与列表**共用** `selectedIds`，切视图选择不丢）；
        ② 工具栏有「全选」「反选」「全选未引用」三个按钮。
        """
        code = _code_only(PICTURE_SPACE)
        assert "<Checkbox" in code, "卡片须有复选框"
        # 四个选择按钮：图标 + Tooltip（用户口径「全部使用图标替代文本，文本使用tip展示」）。
        for tip in ("全选（当前页）", "取消全选", "反选", "全选未引用的图"):
            assert tip in code, f"须有「{tip}」按钮（Tooltip 文案）"
        # 「全选未引用」只挑 `!it.referenced`。
        assert "!it.referenced" in code, "全选未引用须按 referenced=false 过滤"
        # 取消全选 = 清空。
        assert "setSelectedIds([])" in code, "须有「取消全选」（清空选择）"

    def test_card_shows_reference_tag_and_click_toggles_selection(self):
        """⚠️ 卡片右上角显示「是否引用」+ 点卡片 = 选中/取消（2026-10-07 用户口径）。"""
        code = _code_only(PICTURE_SPACE)
        assert "已引用" in code, "卡片须显示「已引用」标签"
        assert "未引用" in code, "卡片须显示「未引用」标签"
        # 点卡片切换选中。
        assert "onClick={() => toggleSelect(item.id)}" in code, "点卡片须切换选中"

    def test_picture_space_has_storage_resize(self):
        """⚠️ 图片空间「空间扩容」（2026-10-07 用户「新增空间扩容功能」+「弹窗展示是否支持扩容申请」）。

        一比一对照千帆：配额条右侧「空间扩容 >」入口（白名单卖家不渲染），
        点击开弹窗、打开即拉条件、逐条展示是否通过、全通过才可申请。
        """
        code = _code_only(PICTURE_SPACE)
        # 入口：白名单不渲染（`white_seller` 反向判据）。
        assert "空间扩容" in code, "须有「空间扩容」入口"
        assert "!usage.white_seller" in code, "白名单卖家不渲染扩容入口"
        # 弹窗：标题 / 小节 / 条件逐条 / 页脚（与千帆一致，2026-10-07 用户口径）。
        assert '"图片空间扩容"' in code, "弹窗标题须为「图片空间扩容」"
        assert "扩容条件" in code, "弹窗须有「扩容条件」小节标题"
        assert "fetchPictureSpaceResizeConditions" in code, "弹窗打开须拉扩容条件"
        assert "resizeCheck.conditions.map" in code, "扩容条件须逐条渲染"
        assert "can_apply" in code, "须按 can_apply 判定是否可提交"
        assert "剩余扩容次数" in code, "页脚须显示剩余扩容次数（平台原文）"
        assert "尚未满足条件" in code, "页脚须有未满足条件的原文文案"
        assert "applyResize" in code, "须有「提交」扩容"

    def test_page_root_clamps_width_to_avoid_page_level_horizontal_scroll(self):
        """⚠️ 页面根须收口宽度（2026-10-07）——防 flex 子项被撑出视口。

        flex 子项默认 `min-width:auto`，右区表格内容一宽就把整个 flex 行撑破，
        左栏（含「回收站」入口）会被顶出视口左侧。
        """
        code = _code_only(PICTURE_SPACE)
        assert 'overflowX: "hidden"' in code, "页面根须 overflowX:hidden（横向不出页面）"
        assert 'maxWidth: "100%"' in code, "页面根须 maxWidth:100%"
        assert "minWidth: 0" in code, "右区须 minWidth:0（flex 子项才能收缩）"

    def test_page_calls_write_apis(self):
        """写操作接的是**写端点**（不是只读那三个）。"""
        code = _code_only(PICTURE_SPACE)
        for fn in (
            "createPictureSpaceFolder",
            "movePictureSpaceItems",
            "deletePictureSpaceItems",
            "downloadPictureSpaceItems",
            "previewPictureSpaceDelete",
        ):
            assert fn in code, f"图片空间写操作未接 {fn}"

    def test_delete_requires_two_layer_confirm(self):
        """删除必须**两层确认**（preview 拿 token → 确认框 → 带 token 真删）。"""
        code = _code_only(PICTURE_SPACE)
        assert "previewPictureSpaceDelete" in code, "删除前必须走 preview 拿确认凭据"
        assert "confirm_token" in code, "删除必须回传 confirm_token（两层确认）"
        assert "modal.confirm" in code, "删除必须弹确认框（列出将删的文件）"

    def test_read_only_listing_still_works(self):
        """只读列表仍在（写入口是在只读基础上的增量）。"""
        code = _code_only(PICTURE_SPACE)
        assert "fetchPictureSpaceItems" in code
        assert "fetchPictureSpaceUsage" in code

    def test_write_apis_are_declared(self):
        """写请求函数在 `lib/api.ts` 里确实存在（不是页面里凭空调用）。"""
        api = _read(API_TS)
        for fn in (
            "createPictureSpaceFolder",
            "movePictureSpaceItems",
            "deletePictureSpaceItems",
            "downloadPictureSpaceItems",
            "previewPictureSpaceDelete",
            "fetchPictureSpaceTask",
        ):
            assert f"export async function {fn}" in api, f"lib/api.ts 缺少 {fn}"

    def test_page_distinguishes_itself_from_local_material_library(self):
        """页面必须点明「与本地素材库不是一回事」（用户最易混淆处）。"""
        assert "千帆云" in _read(PICTURE_SPACE)


# --------------------------------------------------------------------------- #
# N2 发布中心 → 笔记工坊
# --------------------------------------------------------------------------- #
class TestPublishMovedIntoWorkshop:
    def test_main_menu_dropped_publish_entry(self):
        """主菜单**不再**有独立「发布中心」项。"""
        code = _code_only(SHELL)
        assert '"/platforms/xhs/publish"' not in code

    def test_workshop_has_publish_tab(self):
        code = _code_only(NOTE_WORKSHOP)
        assert '"publish"' in code
        assert "XhsPublishPage" in code
        assert "发布中心" in code

    def test_old_route_redirects_to_publish_tab(self):
        """旧路由重定向到 `?tab=publish`（深链、书签不死）。"""
        router = _read(ROUTER)
        assert 'path="/platforms/xhs/publish"' in router
        assert "/platforms/xhs/note-workshop?tab=publish" in router


# --------------------------------------------------------------------------- #
# N3 自动运营（2026-10-07：工作流整条特性退场）
#
# 变更史：2026-10-06 本组叫 `TestWorkflowMergedIntoAutoOps`，断言「自动运营」与
# 「自动化工作流」两个 Tab 并存。2026-10-07 用户口径「把工作流相关逻辑表等完整移除，
# 直接使用定时任务完成即可」⇒ 工作流退场 ⇒ 本组改为**退场守卫**（防止它以任何形式回来）。
# 原断言已被这组替代，理由即上面这两句（不静默删除）。
# --------------------------------------------------------------------------- #
class TestWorkflowRetired:
    def test_no_workflow_entry_anywhere_in_nav(self):
        """主菜单 / 子菜单都**不得**再有工作流入口。"""
        code = _code_only(SHELL)
        assert '"/platforms/xhs/workflows"' not in code
        assert "自动化工作流" not in code

    def test_auto_ops_host_is_a_plain_page(self):
        """宿主页退回**直接渲染** AutoOpsPanel —— 单 Tab 的分段控件是冗余。

        ⚠️ 判据落在「调用形态」而不是「符号出现」：宿主页自己的 docstring 会提到
        `WorkflowPanel`（讲变更史），只看符号存在就永远绿（变异逃逸）。
        """
        code = _code_only(AUTO_OPS)
        assert "<AutoOpsPanel" in code
        assert "WorkflowPanel" not in code
        assert "useSearchParams" not in code

    def test_workflow_route_is_gone(self):
        """旧路由**不再**存在（不做重定向 —— 它指向的能力已不存在）。"""
        router = _read(ROUTER)
        assert 'path="/platforms/xhs/workflows"' not in router
        assert "tab=workflows" not in router

    def test_workflow_sources_are_deleted(self):
        """后端 5 个模块 + 前端 3 个文件必须**真的不在盘上**。

        比「搜不到引用」硬：文件还在、只是没人 import，搜引用照样是绿的 ——
        而「留在盘上」正是下次有人手滑 import 回去的入口。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        gone = [
            "backend/app/models/workflow.py",
            "backend/app/services/workflow_engine.py",
            "backend/app/services/workflow_nodes.py",
            "backend/app/services/workflow_service.py",
            "backend/app/api/platforms/xhs/workflows.py",
            "frontend/src/pages/platforms/xhs/workflow-page.tsx",
            "frontend/src/lib/workflow-format.ts",
            "frontend/src/lib/workflow-format.test.ts",
        ]
        present = [rel for rel in gone if (root / rel).exists()]
        assert not present, f"工作流退场后这些文件仍在盘上：{present}"

    def test_api_client_has_no_workflow_functions(self):
        """`lib/api.ts` 不得再有工作流请求函数 / 类型。"""
        code = _read(API_TS)
        for name in ("fetchWorkflows", "WorkflowListItem", "WorkflowRunDetail", "workflow-runs"):
            assert name not in code, f"lib/api.ts 仍含 {name}"


# --------------------------------------------------------------------------- #
# N4 + N5 数据洞察页瘦身
# --------------------------------------------------------------------------- #
class TestAnalyticsSlimming:
    def test_no_refresh_or_report_buttons(self):
        """内容洞察 tab 里**没有**「刷新」「生成报告」按钮。"""
        code = _code_only(ANALYTICS)
        assert "生成报告" not in code
        # 「刷新」在别的页面是通用词；本页必须一个都没有（那两个按钮是唯一来源）。
        assert "刷新" not in code

    def test_generate_report_logic_removed(self):
        """报告生成逻辑整条移除（不留死代码）。"""
        code = _code_only(ANALYTICS)
        assert "generateReport" not in code
        assert "isGeneratingReport" not in code
        assert "reportMessage" not in code

    def test_report_imports_removed(self):
        """`createXhsAnalyticsReport` / `downloadExportFile` / 两个图标 import 一并删。

        留死 import 会让 `verify_doc_counts.py` 的死代码判据变红。
        """
        code = _code_only(ANALYTICS)
        for token in ("createXhsAnalyticsReport", "downloadExportFile", "DownloadOutlined", "ReloadOutlined"):
            assert token not in code, f"数据洞察页应删掉 {token}"

    def test_dev_notice_removed(self):
        """「正在开发优化中」提示整行移除。"""
        assert "正在开发优化中" not in _read(ANALYTICS)

    def test_load_analytics_still_runs(self):
        """⚠️ 删按钮**不等于**删加载 —— `loadAnalytics` 仍在首查驱动。"""
        code = _code_only(ANALYTICS)
        assert "loadAnalytics" in code
        assert "useEffect" in code


# --------------------------------------------------------------------------- #
# API 层（三个新请求函数）
# --------------------------------------------------------------------------- #
class TestApiFunctions:
    def test_three_picture_space_request_functions(self):
        code = _read(API_TS)
        for fn in ("fetchPictureSpaceItems", "fetchPictureSpaceUsage", "fetchPictureSpaceTree"):
            assert f"export async function {fn}" in code
