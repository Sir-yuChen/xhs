"""页面体验优化批次（R7–R9 + 账号卡片统一 + v1.32 商品列表本地化）的**源码级守卫**。

本项目没有前端测试框架，所以「前端必须做到的事」用源码断言钉住——与既有
`test_api.py::test_accounts_page_does_not_auto_check_accounts_on_load` 同一套路。

为什么值得这么写：本批需求里有四条是**「不要做某事」**——
不要再有「加载更多」按钮、**不要**在页面加载时打平台、**不要**把 creator 的指标行藏掉、
**不要**让弹窗整体滚动。这类要求最容易在后续重构里被无意改回去，
而且改回去**不会让任何断言变红**——除非现在就写下来。

设计源：`docs/02_设计文档/12_功能设计_我的笔记与互动数据.md` §20–§25 / §30、
`docs/02_设计文档/04_功能设计_Web运营端.md` §2.4、
`docs/02_设计文档/13_功能设计_账号矩阵同步.md` §30（v1.32）。
"""

from __future__ import annotations

import re

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MY_NOTES = ROOT / "frontend/src/pages/platforms/xhs/my-notes-page.tsx"
LIBRARY = ROOT / "frontend/src/pages/platforms/xhs/library-page.tsx"
NOTE_DETAIL_MODAL = ROOT / "frontend/src/components/platforms/note-detail-modal.tsx"
SHOP_ITEMS = ROOT / "frontend/src/pages/platforms/xhs/shop-items-page.tsx"
#: DEF-093（2026-10-05）：列表与导出的**唯一一份**请求构造搬到了这里。
#: 页面里那些「请求形状」的守卫改成读**本文件** —— 形状不再住在页面里，
#: 继续扫页面就会变成「断言页面里有段注释」的哑弹。
LIB = ROOT / "frontend/src/lib/shop-items-query.ts"
SHOP_ITEMS_VIEW = ROOT / "frontend/src/lib/shop-items-view.ts"
ACCOUNTS = ROOT / "frontend/src/pages/platforms/xhs/accounts-page.tsx"
GLOBAL_CSS = ROOT / "frontend/src/global.css"
SETTINGS = ROOT / "frontend/src/pages/settings/settings-page.tsx"

#: 账号下拉所在的页面（2026-09-24「只展示账号名称」口径的守护范围）。
#: 新增带账号下拉的页面时**必须**加进来 —— 漏一个就等于那条下拉没人守。
ACCOUNT_DROPDOWN_PAGES = (
    "frontend/src/pages/platforms/xhs/my-notes-page.tsx",
    # 2026-10-04 设计 21 切片 B：crawler-page.tsx 并入「笔记发现 → 批量抓取」，
    # 账号下拉随迁到面板文件。
    "frontend/src/pages/platforms/xhs/bulk-crawl-panel.tsx",
    "frontend/src/pages/platforms/xhs/discovery-page.tsx",
    # 2026-10-06 设计 22 §3：正文（含账号下拉）迁到 auto-ops-panel.tsx（宿主页只剩 Tabs）。
    "frontend/src/pages/platforms/xhs/auto-ops-panel.tsx",
    "frontend/src/pages/platforms/xhs/publish-page.tsx",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _slice_between(source: str, start_marker: str, end_marker: str) -> str:
    """在两个标记之间切片。

    全文断言会被别处的同名字符串遮过（DEF-057 实测：把价格那处改成 normal，
    全文断言依然绿）—— 只在这个区块内部断言。
    """
    start = source.index(start_marker)
    end = source.index(end_marker, start + len(start_marker))
    return source[start:end]


def _code_only(source: str) -> str:
    """剩掉注释（`//` 行注释与 `/* */` 块注释），**不动字符串字面量**。

    反向断言（`X not in code`）必须先过它：解释性注释里几乎一定会提到被删掉的
    符号名（DEF-057 实测：断言「函数已删除」，而迁移说明里写着「该函数已移除」）
    —— 不剪注释就是被自己的注释命中，断言恒真。

    为什么是状态机而不是正则：源码里有 `"https://..."` 这类 URL，按 `//` 切会把
    字符串劈断，把「某段代码不见了」的假阳性引进来。这里逐字符扫描，进入引号
    后原样拄败到配对引号为止。
    """
    out: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        nxt = source[index + 1] if index + 1 < length else ""
        # 行注释：跳到行尾（保留换行，免得把两行缝成一行）
        if char == "/" and nxt == "/":
            while index < length and source[index] != "\n":
                index += 1
            continue
        # 块注释：跳到 */ 之后
        if char == "/" and nxt == "*":
            index += 2
            while index < length and not (
                source[index] == "*" and index + 1 < length and source[index + 1] == "/"
            ):
                index += 1
            index += 2
            continue
        # 字符串字面量：原样拄败到配对引号（识别 \ 转义）
        if char in "\"'`":
            quote = char
            out.append(char)
            index += 1
            while index < length:
                inner = source[index]
                out.append(inner)
                index += 1
                if inner == "\\" and index < length:
                    out.append(source[index])
                    index += 1
                    continue
                if inner == quote:
                    break
            continue
        out.append(char)
        index += 1
    return "".join(out)


# --------------------------------------------------------------------------- #
# v1.32 商品列表本地化改造（设计 13 §30）：行为验证 + 源码守卫
# --------------------------------------------------------------------------- #
def _run_shop_items_view(env: dict | None = None):
    script = ROOT / "tests/frontend/shop_items_view.cjs"
    assert script.exists(), "行为验证脚本缺失（tests/frontend/shop_items_view.cjs）"
    try:
        return subprocess.run(
            ["node", str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError:  # pragma: no cover - 环境问题，不是逻辑分支
        pytest.fail("未找到 node：本机无法运行前端行为验证（AGENTS.md §2 已依赖 node 跑 tsc）")


def test_shop_items_view_logic_is_driven_for_real():
    """账号不过滤 / 标签**只显示账号名称**（2026-09-24）/ 审核态说明 / SKU 缺值 /
    只有上架时间可排 / 前置门禁（含 `unknown` 放行）/ 挂链笔记数筛选 / 批量删除判据，
    由**真实打包产物**验证（§30.7 / §33）。34 条断言。"""
    result = _run_shop_items_view()
    assert result.returncode == 0, (result.stdout + result.stderr)[-900:]
    assert "34 PASS / 0 FAIL" in result.stdout


@pytest.mark.parametrize(
    "mutate,expected",
    [
        ("typefilter", "A1"),   # 类型过滤加回来 ⇒ 前端预判「哪类账号有店铺」
        ("labelsuffix", "B1"),  # 标签加回「（创作者）」⇒ 与类型过滤一并删除的口径被破坏
        ("hint", "C1"),         # 审核态不再给说明 ⇒ 用户选了查不到还以为系统坏
        ("skuzero", "D1"),      # SKU 缺值渲染 0 ⇒ 把「读不到」说成「读到 0」
        ("sortany", "E1"),      # 排序放开成多列 ⇒ 表头出现后端不会照办的箭头
        ("sortdefault", "E4"),  # 「取消排序」兜成默认序 ⇒ 箭头还挂着、列表却换了序
        ("gateoff", "F2"),      # 门禁短路 ⇒ 未绑千帆也发请求，页面又冒一串错误
        ("noteop", "G3"),       # 篇数留空兜成「小于 0」⇒ 一进页面就筛掉有挂链笔记的商品
        ("delmax", "H3"),       # 删除上限失效 ⇒ 界面说「拦住了」，其实发出去（后端才 400）
    ],
)
def test_shop_items_view_guards_are_load_bearing(mutate, expected):
    """变异验证：每个守卫被改坏时脚本必须变红（否则它没在守任何东西）。

    断言写成 `FAIL  Xn` 这种**带前缀**的形式：只找 `A1` 的话，全绿时那行
    `PASS  A1 …` 同样命中，等于什么都没断言（DEF-058 的教训）。
    """
    result = _run_shop_items_view({"SHOP_ITEMS_MUTATE": mutate})
    assert result.returncode != 0, f"{mutate} 变异必须让脚本失败"
    assert f"FAIL  {expected}" in result.stdout, f"stdout={result.stdout}"


def _run_library_topics_merge(env: dict | None = None):
    script = ROOT / "tests/frontend/library_topics_merge.cjs"
    assert script.exists(), "行为验证脚本缺失（tests/frontend/library_topics_merge.cjs）"
    try:
        return subprocess.run(
            ["node", str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError:  # pragma: no cover - 环境问题，不是逻辑分支
        pytest.fail("未找到 node：本机无法运行前端行为验证（AGENTS.md §2 已依赖 node 跑 tsc）")


def test_library_topics_merge_drives_real_api():
    """标签并入话题（§34.4）：A 段驱动真实 api.ts + B/C 段源码守卫，全绿。"""
    result = _run_library_topics_merge()
    assert result.returncode == 0, (result.stdout + result.stderr)[-900:]
    assert "FAIL" not in result.stdout, result.stdout


@pytest.mark.parametrize(
    "mutate,expected",
    [
        ("titlebar", "B1"),       # 标题栏加回标签 chips ⇒ 刚移除的 UI 又回来了
        ("objects", "B2"),        # 话题 chips 退回字符串渲染 ⇒ 关系表对象契约被破坏
        ("autocomplete", "B3"),   # AutoComplete 下拉清空 ⇒ 「选已有话题」能力丢失
        ("filter", "B4"),         # 筛选回退 tag_id ⇒ 后端参数已改名，筛了等于没筛
        ("cards", "B5"),          # 话题渲染加回卡片 ⇒ 用户口径「话题只留详情弹窗」被破坏
        ("route", "C1"),          # 话题字典回退 /tags ⇒ 路由已随表合并改名
    ],
)
def test_library_topics_merge_guards_are_load_bearing(mutate, expected):
    """变异验证：每个守卫被改坏时脚本必须变红（DEF-058：带前缀断言）。"""
    result = _run_library_topics_merge({"LIB_TOPIC_MUTATE": mutate})
    assert result.returncode != 0, f"{mutate} 变异必须让脚本失败"
    assert f"FAIL {expected}" in result.stdout, f"stdout={result.stdout}"


def _run_library_topic_search(env: dict | None = None):
    script = ROOT / "tests/frontend/library_topic_search.cjs"
    assert script.exists(), "行为验证脚本缺失（tests/frontend/library_topic_search.cjs）"
    try:
        return subprocess.run(
            ["node", str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError:  # pragma: no cover - 环境问题，不是逻辑分支
        pytest.fail("未找到 node：本机无法运行前端行为验证（AGENTS.md §2 已依赖 node 跑 tsc）")


def test_library_topic_search_drives_real_api():
    """话题下拉远程模糊搜索（§34.4）：A 段驱动真实 api.ts + B 段驱动真实
    topic-search.ts + C/D 段源码守卫，全绿。"""
    result = _run_library_topic_search()
    assert result.returncode == 0, (result.stdout + result.stderr)[-900:]
    assert "FAIL" not in result.stdout, result.stdout


@pytest.mark.parametrize(
    "mutate,expected",
    [
        ("nodebounce", "C2"),      # onSearch 绕过防抖器直连查询 ⇒ 每次击键都打后端
        ("localfilter", "C1"),     # 退回本地过滤 ⇒ keyword 永不到后端，远程搜索名存实亡
        ("nolike", "D2"),          # keyword 拉了不用（去 LIKE）⇒ 搜索退化成全量
    ],
)
def test_library_topic_search_guards_are_load_bearing(mutate, expected):
    """变异验证：每个守卫被改坏时脚本必须变红（DEF-058：带前缀断言）。"""
    result = _run_library_topic_search({"LIB_TOPIC_SEARCH_MUTATE": mutate})
    assert result.returncode != 0, f"{mutate} 变异必须让脚本失败"
    assert f"FAIL {expected}" in result.stdout, f"stdout={result.stdout}"


def test_shop_items_page_removed_entries_stay_removed():
    """v1.32（§30.1 / §30.2 + 用户 2026-09-23 口径）：商品列表页**不再有**
    「换一份千帆 Cookie」按钮、「同步挂链映射」按钮，账号下拉**不再按类型过滤**；
    顶部**不再有**「店铺：xxx」「本地台账 N 件 · 同步于 X」「数据说明」三个文本；
    排序**只在表头**（上下箭头），搜索区不再有排序字段 / 方向下拉。

    这些都是「删除类」需求 —— 重构时被加回来不会让任何现有断言变红，所以
    用源码守卫钉住（`_code_only` 剥注释，防「迁移说明里提到被删符号」误伤）。
    """
    code = _code_only(_read(SHOP_ITEMS))
    assert "换一份" not in code, "「换一份千帆 Cookie」按钮已移除（§30.2，仅移除按钮）"
    assert "同步挂链映射" not in code, "「同步挂链映射」按钮已移除（§30.2，前后端）"
    assert 'sub_type === "creator"' not in code and "sub_type == 'creator'" not in code, (
        "账号下拉不得再按 sub_type 过滤（§30.1）"
    )
    assert "handleSync" not in code and "syncing" not in code, "挂链映射同步的 state/handler 应已删除"
    # 本地化与新版式的正向锚点（防「守卫还在、功能被悄悄改回去」）
    assert "fetchXhsShopItemDetail" in code, "标题点击应打开商品详情弹窗（§30.3.6）"
    # 表头排序（用户 2026-09-23 口径）：服务端分页 ⇒ 排序必须回服务端做，
    # antd 箭头接线 = sortDirections + onChange（客户端本地排只会重排一页 = 假排序）。
    assert "sortDirections" in code and "handleSortChange" in code, (
        "表头应接线上下箭头排序（排序挪窝后唯一的排序入口）"
    )
    # 顶部三文本移除（同一口径）：店铺名 / 本地台账时效行 / 数据说明。
    # 只钉 JSX 表达式形态（原顶部 Tag：`店铺：{binding.shop_name}`），
    # 不误伤绑定成功提示「已绑定千帆店铺：」。
    assert "店铺：{binding.shop_name" not in code, "「店铺：xxx」顶部 Tag 已移除（店铺口径在详情弹窗仍有）"
    assert "件 · 同步于" not in code, "「本地台账 N 件 · 同步于 X」时效行已移除"
    assert "数据说明" not in code, "「数据说明」Tooltip 已移除"
    # 排序挪走后搜索区不得留第二入口（留一份 = 两个入口，必漂移）。
    assert 'name="sort_field"' not in code and 'name="order"' not in code, (
        "排序已挪到表头，搜索区不得再留排序字段 / 方向下拉"
    )


def test_shop_items_page_follows_the_2026_09_23_review():
    """§33（用户 2026-09-23 上机验收口径）：商品列表页的四条修正 + 一条排版口径。

    | 口径 | 守卫 |
    |---|---|
    | 挂链笔记弹窗**移除**说明文本 | 弹窗里不再有覆盖率与口径解释 |
    | 搜索条件**两行排完、按钮置尾** | 表单恰好 2 个 `Row`，按钮组右对齐收尾 |
    | **只有上架时间列可排** | 页面里 `sorter: true` / 箭头激活态各 **1** 处 |
    | 千帆可用性作**前置校验** | 判据与文案都取 lib 唯一实现，页面不自建 |
    | **分类不自动换行** | 分类行 `…` 收起 + Tooltip（ADR-018），不靠宽度硬撑 |

    这些都是「删除 / 收紧类」需求 —— 被改回去不会让任何既有断言变红，所以用源码
    守卫钉住；反向断言一律先 `_code_only` 剥注释（解释性注释必然提到被删的字样）。
    """
    code = _code_only(_read(SHOP_ITEMS))
    # ① 挂链笔记弹窗：说明文本整块移除（覆盖率那行也不再出现在列表页 Tooltip 里）
    assert "本地已映射" not in code, "挂链笔记弹窗的「本地已映射 X/Y」说明应已移除"
    assert "反查走笔记" not in code, "该弹窗的口径解释应已移除"
    assert "查看这些笔记" not in code and "平台侧记" not in code, (
        "「挂链笔记」列的 Tooltip 文案同批移除（用户口径：文本移除）"
    )
    # ② 未绑定页的长段来源说明与「登录态已失效」独立告警移除
    assert "商品列表来自" not in code, "未绑定页的「商品列表来自…」三步取 Cookie 长文案应已移除"
    assert "千帆登录态已失效" not in code, "失效告警不应回归"
    # ③ 前置门禁（千帆可用性校验）**整块移除**（2026-10-05 用户口径：本页查本地数据库，
    #    无需校验账号是否有效）。
    #
    #    ⚠️ 原断言是「必须引用 lib 的 ledgerPrerequisite」—— **被本条取代**。
    #    它守的是「判据只有一份」；现在连那份引用都不该在页面上出现（本地读本就没有门禁），
    #    故改成**反向钉死**：页面不许再出现任何一处千帆可用性判定/文案。
    #    lib 里的 `ledgerPrerequisite` / `QIANFAN_PREREQUISITE_HINT` 保留（lib 级契约，
    #    由 `shop_items_view.cjs` 的 F1–F5 覆盖），但**页面不得引用** —— 引用即校验回归。
    for gone in (
        "ledgerPrerequisite(",
        "QIANFAN_PREREQUISITE_HINT",
        "arkUnavailable",
        "channelsByAccount",
        "prereq",
        "千帆登录态不可用",
        "请在账号信息卡片中扫码登录千帆后再来查看",
    ):
        assert gone not in code, (
            f"商品列表页不得再校验千帆可用性（2026-10-05 用户口径：本页查本地数据库）"
            f"，发现残留：{gone}"
        )
    assert "function channelUnavailable" not in code, "门禁判据不得在页面里自建第二份"
    # ④ 只有「上架时间」列可排：箭头接线恰好一处（表头有箭头 = 后端真会按它排）
    assert code.count("sorter: true") == 1, "只有「上架时间」列可排，其它列不给箭头"
    assert code.count("sortOrderFor(") == 1, "箭头激活态接线同样只该有一处"
    # ⑤ 搜索条件两行、按钮组右对齐收尾（不再散在两行各占一列）；挂链笔记数筛选在列
    assert code.count("<Row gutter={12}>") == 2, "搜索条件应恰好两行排完"
    assert 'justifyContent: "flex-end"' in code, "搜索 / 重置等按钮应排在条件最后（右对齐）"
    assert 'name="note_count_op"' in code and 'name="note_count"' in code, (
        "搜索条件应有「挂链笔记数」比较符 + 篇数（v1.34 / §33.6）"
    )
    assert "EMPTY_SHOP_ITEMS_DRAFT.note_count_op" in code, (
        "比较符默认值必须取 lib 常量（`EMPTY_SHOP_ITEMS_DRAFT.note_count_op` = 「小于」）；"
        "2026-09-26 起表单初值来自缓存草稿，那份草稿的默认值仍住 lib"
    )
    assert 'note_count_op: "lt"' not in code, "页面不许自己写比较符默认值（第二份真相源）"
    # DEF-093：请求形状已搬到 lib/shop-items-query.ts ⇒ 断言读**那里**
    assert "noteCountFilter(" in _code_only(LIB.read_text(encoding="utf-8")), (
        "篇数 → 请求参数必须走 lib 的 noteCountFilter（留空不筛）"
    )
    # ⑥ 分类不自动换行：`…` 收起 + Tooltip（换行会把整行商品卡撑高、列基线错开）
    assert "ellipsis={{ tooltip: item.category" in code, "分类行应 `…` 收起 + Tooltip，不换行"


def test_shop_items_page_follows_the_2026_09_23_thumbnail_review():
    """§33.9（v1.35）：商品缩略图 / 单行标题 / 首卡空白 —— 三处纯排版口径的守卫。

    | 口径 | 守卫 |
    |---|---|
    | 商品图**放大 15px**（56 → 71） | 只有一个尺寸常量出口，真图与占位块共用 |
    | 「N 图」角标挪到**图片左上角** | 绝对定位角标（不再跟在标题后占宽度） |
    | 商品名称**单行展示** | `…` 收起 + Tooltip 全文（ADR-018），不换行 |
    | 「商品列表（千帆）」**下的空白移除** | 首卡 body 不得再用空 `Space` 包裹 |

    第 4 条最容易「被改回去而无人发觉」：只要有人顺手把裸 `Text` 塞回
    `<Space direction="vertical" size={10}>`，antd `Card` 就会重新渲染
    `.ant-card-body`（它判的是 `childNodes.length`，而 `Space` 壳永远算一个
    子节点）⇒ 标题下又空出 48px，但**任何既有断言都不会变红**。

    切片断言而非全文：`Space direction="vertical" size={10} style={{ width: "100%" }}`
    在页面里**还有两处合法用法**（笔记信息块、挂链笔记弹窗），全文断言会误伤。
    """
    code = _code_only(_read(SHOP_ITEMS))
    # ① 商品图放大 15px：71 = 56 + 15，且尺寸只住一个常量（有图 / 没图不会错位）
    assert "const ITEM_THUMB_SIZE = 71;" in code, "商品缩略图应为 71（原 56 放大 15px）"
    assert "width={56}" not in code and "height={56}" not in code, "旧的 56 尺寸应已替换"
    assert code.count("ITEM_THUMB_SIZE") >= 4, "真图与无图占位块必须共用同一个尺寸常量"
    # ② 「N 图」角标：压图片左上角，且不吃点击
    assert "IMAGE_COUNT_BADGE_STYLE" in code, "图数角标应走统一样式常量（两处共用）"
    assert 'position: "absolute"' in code and "top: 0" in code and "left: 0" in code, (
        "角标应绝对定位到图片左上角"
    )
    assert 'pointerEvents: "none"' in code, "角标不得吃掉「点开大图」的点击"
    assert "marginInlineStart: 6" not in code, "标题后那枚行内「N 图」Tag 应已移除"
    # ③ 商品名称单行展示（`…` 收起 + Tooltip 全文）
    assert 'whiteSpace: "nowrap"' in code, "商品名称应单行展示，不换行"
    assert 'textOverflow: "ellipsis"' in code, "超长商品名应收起而不是截断"
    # ④ 「商品列表（千帆）」下的空白：首卡 body 不再被空 Space 撑出来
    #
    #    ⚠️ **整卡已移除**（2026-10-05 用户口径「顶部的商品列表（千帆）整块移除」）
    #    ⇒ 原先那句「首卡 body 不得包空 Space」的载体（`statusHint` 那片 body）**不存在了**。
    #    守卫改钉**同一口径的现行落点**：状态说明现在住在「搜索条件」卡里，
    #    它同样必须是**裸三元 + `null` 兜底**（不然搜索条件卡会在无说明时也留一片空白 —— 同一个病）。
    card = _slice_between(code, 'title="搜索条件"', 'title="商品台账"')
    assert "statusHint ?" in card, (
        "状态说明应改成裸三元（无说明时整块 body 不渲染）—— 原在首卡，首卡移除后随说明一起搬进搜索条件卡"
    )
    assert 'Space direction="vertical" size={10} style={{ width: "100%" }}' not in card, (
        "搜索条件卡里不得包这层空 Space：antd Card 判 childNodes.length，空壳也算节点 ⇒ 空白"
    )
    assert ") : null}" in card, "无说明时必须以 null 兜底，让 antd 跳过整个 body"


def test_shop_items_page_follows_the_2026_09_23_batch_delete_review():
    """§33.10（v1.36）：挂链笔记弹窗 75% / 批量删除 / 跨页勾选口径的守卫。

    | 口径 | 守卫 |
    |---|---|
    | 挂链笔记弹窗**全屏 75%** | `width="75vw"` + body `height: "75vh"`；旧的 `80vw` / `maxWidth: 1600` 已去 |
    | **批量删除**（只删本地台账行） | 走 lib 判据 + 文案，页面不自建；接口 `deleteXhsShopItems` |
    | 上限**只有一个出口** | 页面不得自己定义常量；与后端 `MAX_BATCH_DELETE` **同值**（本测直接比对两侧真值） |
    | 跨页勾选保留、**换条件/换账号清空** | `preserveSelectedRowKeys` + `runSearch` 清勾选 |

    为什么要钉「上限同值」：这个数字前端拦一次、后端拦一次，两边不等时表现是
    「页面放行 → 后端 400」，用户看到一句莫名其妙的错误。少写一条断言就少一次
    这种漂移的发现机会（`NOTE_COUNT_OPS` 同款处理）。
    """
    code = _code_only(_read(SHOP_ITEMS))
    # ① 弹窗尺寸：宽高都取视口 75%（不再设 maxWidth：设了它就不是 75% 了）
    assert 'width="75vw"' in code, "挂链笔记弹窗应为视口宽度 75%"
    assert '"75vh"' in code, "挂链笔记弹窗内容区应为视口高度 75%"
    assert 'width="80vw"' not in code, "旧的 80vw 应已替换"
    # ⚠️ v2.28.2：锚点用 `title={notesItem`（Modal 内、`centered` 之后）——
    # 原先用 `open={notesOpen}`，而「全站弹窗居中」把 `centered` 加在了它**前面**
    # ⇒ 切片从 centered 之后开始，`"centered" in modal` 假红（实测）。
    modal = _slice_between(code, "open={notesOpen}", "</Modal>")
    assert "maxWidth: 1600" not in modal, "设了 maxWidth 就不是 75% 了（宽屏会被截回固定值）"
    assert "centered" in modal, "75vh 高度下应居中（否则下方留白与原 6vh 定位不一致）"
    assert 'overflow: "auto"' in modal, (
        "窄视口两栏会换行，body 必须能滚 —— `hidden` 会把右栏裁掉"
    )
    # ② 批量删除：判据 / 文案 / 上限都住 lib，页面只编排
    assert "deleteXhsShopItems(" in code, "批量删除必须走 lib/api 的 deleteXhsShopItems"
    assert "batchDeleteBlock(" in code and "batchDeleteHint(" in code, (
        "「能不能删」与「确认框怎么说」必须取 lib 唯一实现（页面不许自建第二份）"
    )
    assert "const MAX_BATCH_DELETE" not in code, "上限只在 lib 定义一处，页面不得复制"
    assert "MAX_BATCH_DELETE" in code, "页面应引用 lib 常量（提示文案里的「≤N 个/次」）"
    assert "okButtonProps: { danger: true }" in code, "不可逆操作确认键应为 danger"
    assert "result.deleted === result.requested" in code, (
        "实际删除数要与提交数对照（不假装「请求几条就删了几条」）"
    )
    # ③ 确认框必须讲清「关系一并删」（2026-09-23 用户口径「删除商品时也需要删除
    #    笔记与商品的关联关系」，**推翻** v1.36 的「关系不动」）—— 这是用户看不到
    #    的连带后果，确认框是唯一能讲清的地方（不藏）。
    hint_lib = _code_only((ROOT / "frontend/src/lib/shop-items-view.ts").read_text(encoding="utf-8"))
    hint_body = _slice_between(hint_lib, "export function batchDeleteHint(", "\n}")
    assert "关联关系一并删除" in hint_body, (
        "确认框未说明「关联关系一并删除」—— 删商品会连带删 note_goods，必须让用户看见"
    )
    assert "关系不动" not in hint_body, "确认框仍是「关系不动」的旧文案（已被用户口径推翻）"
    assert "重新写回台账" in hint_body, "确认框仍要讲「同步会写回」这个后果（两个后果都要）"
    # ③ 跨页勾选保留，换查询条件 / 换账号时清空
    assert "preserveSelectedRowKeys: true" in code, "批量操作跨页勾选要留得住"
    assert code.count("runSearch(") >= 3, "搜索 / 重置应走同一个入口（它负责清勾选）"
    search = _slice_between(code, "function runSearch(", "\n  }")
    assert "setSelectedItemIds([])" in search, "换查询条件 = 换集合：旧勾选必须清掉"
    switch = _slice_between(code, "setBindingLoaded(false);", "void loadBinding(selectedAccountId)")
    assert "setSelectedItemIds([])" in switch, "换账号也必须清勾选（旧勾选对新店无意义）"


def test_batch_delete_limit_matches_between_frontend_and_backend():
    """批量删除上限：前端常量与后端 `MAX_BATCH_DELETE` **必须同值**。

    两处不等 ⇒ 页面放行、后端 400，用户看到一句与操作对不上的错误。
    直接读两边源码比对真值（不是各自断言「等于 200」，那样两边一起漂移仍然绿）。
    """
    import re

    from backend.app.api.platforms.xhs.qianfan import MAX_BATCH_DELETE as backend_limit

    view = _read(SHOP_ITEMS_VIEW)
    matched = re.search(r"export const MAX_BATCH_DELETE = (\d+);", view)
    assert matched, "前端应在 lib/shop-items-view.ts 里定义 MAX_BATCH_DELETE"
    assert int(matched.group(1)) == backend_limit, (
        f"前后端上限不一致：前端 {matched.group(1)} / 后端 {backend_limit}"
    )


def test_shop_items_sorting_is_server_side_and_off_by_default():
    """§33.11（v1.37，用户 2026-09-23 口径）：商品台账**默认不排序**、点表头才触发、
    且排序**一律由后端执行**（「参考 antd 组件中的排序」= 原生三态：升 → 降 → 取消）。

    | # | 约束 | 为什么 |
    |---|---|---|
    | 1 | `sorter: true`，且**不许出现比较函数**（`sorter: (`） | `sorter` 传函数 ⇒ antd 会**本地排序当前页**（`useSorter.getSortFunction` 只在 function / `.compare` 时返回真值）。服务端分页下这是**假排序**：只重排这 20 条，翻页照样乱 |
    | 2 | 受控 `sortOrder` + 初值 `null` | 默认态表头没有箭头；`DEFAULT_SORT` 在 lib 里就是 `null`（真值由行为脚本 E2 实际跑出来） |
    | 3 | 无排序态**不带** `sort_field` / `order` | 让后端用它自己的稳定默认序；前端自己编一个默认字段 = 第二份真相源 |
    | 4 | 「重置」清排序、「搜索」保留排序 | 重置 = 回初始态，而初始态就是「没有排序」；搜索不该悄悄改掉用户刚点的排序 |

    第 1 条尤其值得钉：`sorter: (a, b) => …` 是**看起来更「完整」**的写法，正因如此最容易
    被后人「顺手补上」—— 而它恰好把后端排序废掉（本地先排一遍，后端结果又被它盖住）。
    """
    code = _code_only(_read(SHOP_ITEMS))
    assert code.count("sorter: true") == 1, "上架时间列用 antd 的服务端排序语义（sorter: true）"
    assert "sorter: (" not in code, (
        "不许给 antd 传比较函数：那会变成对当前页的本地假排序（后端排序结果被它盖住）"
    )
    # 2026-09-26：排序态**落本地缓存**（`usePersistedFilter`，用户点过的排序不该每次重来），
    # 但**默认值仍是 lib 的 `DEFAULT_SORT`（无排序）**、且必须过形状校验 —— 页面不得
    # 自己编一个默认字段。切片断言：只找 `DEFAULT_SORT` 会被 lib 里任何一个同名用法满足。
    sort_decl = code.split("usePersistedFilter<SortState | null>(", 1)[1].split(")", 1)[0]
    assert "DEFAULT_SORT" in sort_decl, (
        "排序的默认值必须是 lib 的 DEFAULT_SORT（无排序），页面不得自己编一个默认字段"
    )
    assert "sanitizeSortState" in sort_decl, (
        "缓存的排序态必须过 `sanitizeSortState`（脏形状回「无排序」，不是回某个硬编字段）"
    )
    lib_code = _code_only(LIB.read_text(encoding="utf-8"))
    assert "...(sortArg" in lib_code, "无排序态必须整个省略 sort_field/order（不是送空串去兜底）"
    # ⚠️ 只断言「那段文本在」是不够的（变异实测：把 `: {}` 换成 `: { sort_field: "" }`
    # 依然能通过）。**切片**看无排序分支长什么样，才能钉住「真的不带参数」。
    # 注意 `_slice_between` 是左闭右开、会把结尾的 `}` 截掉 ⇒ 这里手动切到 `}),` 之后。
    sort_chunk_start = lib_code.index("...(sortArg")
    sort_chunk_end = lib_code.index("}),", sort_chunk_start) + len("}),")
    no_sort_branch = lib_code[sort_chunk_start:sort_chunk_end]
    assert ": {}" in no_sort_branch, "无排序分支必须是空对象（真的省掉这两个键）"
    assert '""' not in no_sort_branch, (
        '无排序**不许送空串**：空串仍是「我指定了排序」，会把后端默认序的选择权抢走'
    )
    assert "sort_field: sortArg.field" in lib_code and (
        'order: sortArg.order === "ascend" ? "asc" : "desc"'
    ) in lib_code, "有排序态才把生效字段交给后端（后端排，前端不参与）"
    assert "resetForm ? DEFAULT_SORT : sort" in code, (
        "「重置」应把排序退回无排序，「搜索」应保留用户当前排序"
    )
    assert "sort && sort.field === SORT_FIELD_BY_COLUMN[dataIndex] ? sort.order : null" in code, (
        "sortOrderFor 必须能返回 null（无排序态所有列都不带箭头）"
    )
    assert "resolveSort(" in code and "SORT_FIELD_BY_COLUMN" in code, "判据取 lib 唯一实现，页面不自建"


def test_my_notes_page_dropped_the_mapping_sync_button():
    """「我的笔记」页的「同步商品」按钮（打的是已移除的 /sync-goods）一并删除。"""
    code = _code_only(_read(MY_NOTES))
    assert "syncXhsQianfanGoods" not in code, "该函数随端点一起移除（§30.2）"
    assert "syncingGoods" not in code, "同步中的 state 应已删除"
    assert "handleSyncQianfanGoods" not in code, "绑定后自动同步的调用应已删除"


def test_api_ts_has_no_sync_goods_but_has_item_detail():
    """api.ts：`syncXhsQianfanGoods` 随端点移除；`fetchXhsShopItemDetail` 在位。"""
    code = _code_only(_read(ROOT / "frontend/src/lib/api.ts"))
    assert "syncXhsQianfanGoods" not in code and "/xhs/qianfan/sync-goods" not in code
    assert "fetchXhsShopItemDetail" in code and "/xhs/qianfan/item-detail" in code


def test_my_notes_page_stops_paging_on_failure_and_offers_retry():
    """失败即停 + 显式「重试」：不允许 sentinel 自动重试（失败风暴）。"""
    source = _read(MY_NOTES)
    assert "已停止自动加载" in source
    assert "重试" in source
    # 守卫读的是 ref，而不是在渲染期直接读 state（重新创建 observer 会造成重复请求）
    assert "pagingRef.current" in source


# ---------------------------------------------------------------------------
# R8 · 关键词搜索
# ---------------------------------------------------------------------------


def test_my_notes_page_has_cross_scope_keyword_search():
    """搜索必须跨三个分类，并且**显示扫描元数据**（否则「没搜到」会被误读）。"""
    source = _read(MY_NOTES)
    assert "fetchXhsSelfSearch" in source
    assert "SCOPE_LABELS" in source
    assert "已扫描" in source
    assert "继续搜索更早内容" in source
    # 匹配域的真实边界必须在界面上说明（列表项只有标题）
    assert "标题" in source


def test_my_notes_page_greys_out_search_when_the_link_cannot_answer():
    """creator 链路：搜索框禁用 + 可读原因（原因是后端给的，不在前端硬编码判断）。"""
    source = _read(MY_NOTES)
    assert "searchDisabledReason" in source
    assert "disabled={!searchAvailable}" in source


# ---------------------------------------------------------------------------
# v1.27 · 四个页签不再按账号类型门禁；失败一律安静降级（DEF-057 / 设计 §17.8）
# ---------------------------------------------------------------------------


def test_my_notes_tabs_are_never_gated_by_account_type_or_capability():
    """页签**不得**再依赖能力预判来决定可用性（用户拍板：以取数结果为准）。

    置灰的问题不止「挡了可用链路」：它的理由（「创作者平台没有这三类数据」）
    对「这个账号恰好没数据」这种情况是**假话**。真机已证明三个接口都通
    （`ok=True`，只是那位用户 liked/collected 各 0 条）。
    """
    source = _read(MY_NOTES)
    code = _code_only(source)
    assert "disabled: !capability.available" not in code, "页签不得再按能力置灰/禁用"
    assert "function capabilityFor" not in code, "预判可用性的函数应已删除（§17.8）"
    assert "capabilityFor(key)" not in code, "页签循环不得再查能力"
    assert "在这个账号上不可用" not in code, "旧的置灰说明块应已移除"


def test_my_notes_failures_degrade_to_a_quiet_line():
    """失败的表现是一行 25–30 字说明，不是 toast、不是红块（§17.8）。

    原实现在详情拿不到时弹 `message.error(readError(...))` —— 把后端内部错误串
    （实测就是 `Account has no cookies`）直接甩给用户，且关掉就没处再找。
    """
    # v1.32：「我的笔记」与「内容库」共用同一份详情实现 ⇒ 实现断言指向**共用组件**，
    # 页面侧改断言「两侧关系」（页面必须真的用它），否则断言会因「别处还有一份」恒真。
    modal = _read(NOTE_DETAIL_MODAL)
    assert "quietText(" in modal, "必须走静默文案映射"
    assert "QUIET_REASONS" in modal, "短文案要按后端 `code` 映射，不能截断长文充数"
    assert "quietHint(" in modal, "失败要渲染成一行提示（含 Tooltip 里的完整原因）"
    # 评论区不得再摆「评论加载失败」红条（高度四态一致，但内容改成安静提示）。
    assert 'message="评论加载失败"' not in modal
    page = _code_only(_read(MY_NOTES))
    assert 'from "../../../components/platforms/note-detail-modal"' in page, (
        "「我的笔记」必须从共用模块导入（不能自己再长出一份详情实现）"
    )
    assert re.search(r"<NoteDetailModal\b", page), "必须真的渲染共用弹窗"
    # 详情失败不再弹 toast：原来那个 catch 里只剩 setDetailError。
    assert "setDetailError(quietError(" in page


# ---------------------------------------------------------------------------
# v1.28 · 失败呈现三分法 + 结构化原因不再被吞（DEF-058 / 设计 §17.8）
# ---------------------------------------------------------------------------


def test_api_interceptor_surfaces_structured_detail_instead_of_a_generic_toast():
    """全局拦截器必须认识**结构化** `detail`，否则真实原因永远见不到。

    旧写法 `typeof detail === "string" ? detail : "请求失败，请稍后重试"`：后端所有
    「可自救的失败」都是 `{code, message, platform_message}`，于是**一律**被降级成
    那句通用文案 —— 用户看到三个页签「全部请求失败」，却拿不到任何可执行信息。
    这就是「只有接口异常才真实抛出异常信息」这条口径的反面。
    """
    api = _read(ROOT / "frontend/src/lib/api.ts")
    assert "export function detailMessage(" in api, "结构化 detail 的解析必须是可复用的唯一实现"
    assert 'detailMessage(error.response?.data?.detail) || "请求失败，请稍后重试"' in api
    # 旧的非此即彼写法不得复活。
    assert 'typeof detail === "string" ? detail : "请求失败，请稍后重试"' not in api
    # 422 的数组 detail 同样是「接口异常」：必须还原成可读的「字段路径 + 原因」，
    # 否则它只剩英文 axios 文案（`Request failed with status code 422`）兜底。
    assert "if (Array.isArray(detail)) return validationDetailMessage(detail);" in api, (
        "422 校验错误的数组形态必须交回可读原因，而不是当空值丢掉"
    )


def test_self_content_loaders_keep_failure_ownership_local():
    """四个自有内容取数函数必须 `_silent`：失败由页面呈现，不弹全局 toast。

    否则「取不到」会同时出现两次 —— 页面一行安静说明 + 全局一句
    「请求失败，请稍后重试」，等于把置灰换成了报错。
    """
    api = _read(ROOT / "frontend/src/lib/api.ts")
    for fn in (
        "fetchXhsSelfNotes",
        "fetchXhsSelfLikedNotes",
        "fetchXhsSelfCollectedNotes",
        "fetchXhsInteractionMessages",
    ):
        start = api.index(f"export async function {fn}(")
        # 切片到**下一个导出**为止。不能找 `\n}`：多行参数列表（interactions 的
        # payload 就是）会在参数类型收尾处提前截断，把函数体整段切掉 —— 那样断言
        # 会以「找不到」的形式失败，看着像实现缺了 `_silent`，其实是切片写错。
        end = api.find("\nexport ", start)
        body = api[start : end if end != -1 else len(api)]
        assert "_silent: true" in body, f"{fn} 的失败展示权应归页面（缺 `_silent`）"


def test_failures_are_rendered_by_a_three_way_rule():
    """预期内的「取不到」安静一行；真·接口异常摆**真实**原因。

    用户口径：**没有数据就无需展示**（不报错），**只有接口异常才真实抛出异常信息**。
    所以「可自救的取不到」与「真坏了」不能长成同一个样子。
    """
    code = _code_only(_read(NOTE_DETAIL_MODAL))
    assert "function renderFailure(" in code, "失败呈现要收敛到一个入口"
    assert "EXPECTED_FAILURE_CODES" in code
    assert '"WEB_SESSION_REQUIRED"' in code and '"XSEC_TOKEN_REQUIRED"' in code
    # 真异常分支必须把**完整原因**摆出来（`full`），不能只给通用文案。
    assert "description={error.full}" in code
    # 两个列表 + 互动仍在页面侧过三分法（v1.32 后详情 / 评论由共用组件承担）。
    page = _code_only(_read(MY_NOTES))
    assert "return renderFailure(panel.error);" in page
    assert "{failure && renderFailure(failure)}" in code, "详情/评论失败必须过共用三分法"


def test_structured_detail_reaches_the_toast_via_a_real_bundle():
    """`detailMessage` / 拦截器的行为由**真实打包产物**验证，而不是「源码里有这行」。

    为什么必须多这一层：上面的源码断言在「函数被改成 `return ""`」时**依然为真** ——
    它证明的是代码长什么样，不是函数怎么跑。而后端所有可自救的失败都靠这一条
    把真原因送到用户眼前，退回通用文案就等于把用户锁死。

    `tests/frontend/detail_message.cjs` 用 esbuild 打包真实 `lib/api.ts`，
    拿到**真实**的 response 拦截器并驱动它（antd 换成桩，所以不需要 jsdom）。

    ⚠️ **不设 skip**：node 缺失就让用例红，并说明原因是环境不具备（AGENTS.md §2
    本就把 node 当必需）。静默跳过等于把这条安全网做成条件性的。
    """
    script = ROOT / "tests/frontend/detail_message.cjs"
    assert script.exists(), "行为验证脚本缺失（tests/frontend/detail_message.cjs）"
    try:
        result = subprocess.run(
            ["node", str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except FileNotFoundError:  # pragma: no cover - 环境问题，不是逻辑分支
        pytest.fail("未找到 node：本机无法运行前端行为验证（AGENTS.md §2 已依赖 node 跑 tsc）")
    assert result.returncode == 0, (result.stdout + result.stderr)[-900:]


def test_quiet_reason_is_actionable_not_a_wait_and_see():
    """短文案必须是**可执行**的：会话缺失等一会儿不会变好，只有重新登录能解决。

    原文案「…这一类数据暂时取不到，请稍后再试」给了一个做不到的建议 ——
    用户会反复刷新，而正确的动作（去创作者平台 / PC 端平台登录）一次也不会发生。

    ⚠️「重新登录」这四个字是这条用例的**唯一判据**（v1.40 / 2026-09-24）：短文案
    的入口名随用户口径调整（v1.40 起与后端 `_web_session_required_detail()` 统一成
    「创作者平台或 PC 端平台」），但「做什么」不能丢 —— 判**句式**，不判平台名。
    """
    # v1.32：短文案随共用组件走（`note-detail-modal.tsx`）—— 页面已不再自带一份。
    source = _read(NOTE_DETAIL_MODAL)
    marker = source.index("WEB_SESSION_REQUIRED: ")
    line = source[marker : source.index("\n", marker)]
    assert "请稍后再试" not in line
    assert "重新登录" in line


# ---------------------------------------------------------------------------
# R9 · 详情弹窗固定尺寸 + 内部滚动
# ---------------------------------------------------------------------------


def test_note_detail_modal_body_does_not_scroll():
    """弹窗整体固定高度且不滚动：`height` + `overflow: hidden`（不再是 maxHeight）。"""
    source = _read(NOTE_DETAIL_MODAL)
    assert 'height: "72vh"' in source
    assert 'overflow: "hidden"' in source
    assert 'maxHeight: "72vh"' not in source


def test_note_detail_uses_the_shared_layout_classes():
    source = _read(NOTE_DETAIL_MODAL)
    for class_name in ("note-detail-body", "note-detail-col", "note-detail-main", "note-detail-comments"):
        # 用**带引号的整 token**匹配，而不是 `class_name in source`：
        # 后者会被 `note-detail-body-mut` 这类**超串**蒙过（子串包含 ≠ 类名还在）。
        assert f'"{class_name}"' in source, f"缺少弹窗布局 class：{class_name}"
    # 两侧关系：两个页面都必须复用这份版式，而不是各自复制一份 class。
    for page_path in (MY_NOTES, LIBRARY):
        page = _read(page_path)
        assert 'from "../../../components/platforms/note-detail-modal"' in page, (
            f"{page_path.name} 未从共用模块导入详情弹窗"
        )
        assert re.search(r"<NoteDetailModal\b", page), f"{page_path.name} 未渲染共用详情弹窗"


def test_library_shows_the_real_author_or_nothing():
    """昵称缺值**不展示**，且不再向用户解释「下一轮同步会升回去」（用户 2026-09-22 口径）。

    ① **缺值不造假**（AGENTS.md §3 硬约束⑤）：`author_name` 取不到就**不渲染**这一项，
       不写「未知」充数 —— 假文案会让用户以为真有个叫「未知」的作者（与 DEF-058
       的失败三分法同源：没数据 ≠ 有数据）。
    ② 那行「同步回来的笔记默认『已发布』；下一轮同步会把改回『待发布』的再升回去」是
       **机制说明**，把实现细节写在页面上属噪声（与「商品来源行不解释补齐行为」同源）。
       该口径本身仍记在设计 03 §4.1 / 设计 13 §19.12，**只是不搬到界面上**。

    反向断言先剥注释（`_code_only`）：注释里会提到被删掉的那句话，直接搜源码会恒真。
    """
    code = _code_only(_read(LIBRARY))
    assert '"未知"' not in code, "昵称缺值不得渲染「未知」充数"
    assert "下一轮同步" not in code, "界面不得再解释「同步会把待发布升回去」的机制"
    # 正向：① 作者仍在展示（不能把整项删了）；② 以 `author_name` 为闸门，缺值走空分支。
    # ⚠️ 2026-09-23：原断言是 `'"author_name"' in code`（带引号的字面量）—— 那是
    # **表格列** `dataIndex: "author_name"` 的形态。表格视图已按用户口径移除，
    # 字面量随之消失 ⇒ 改成查**属性访问** `note.author_name`（卡片侧仍在用）。
    # 断言的意图没变：作者仍在卡片上展示，只是缺值不充数。
    assert "author_name" in code, "作者已从界面上消失（应是「缺值不渲染」，不是「整项删掉」）"
    assert "selectedNote.author_name ?" in code, "详情里的作者要按 `author_name` 有无决定渲不渲染"
    assert "note.author_name &&" in code, "卡片里的作者同理，缺值不占位"


def test_detail_comment_pane_has_fixed_height_and_inner_scroll():
    """评论区固定高度 + 内部滚动；窄窗降级为 body 内滚（避免裁切）。"""
    css = _read(GLOBAL_CSS)
    assert ".note-detail-comments" in css
    assert "height: 280px" in css
    assert "@media (max-width: 900px)" in css
    # 信息区必须能滚（flex 子项需要 min-height: 0）
    assert "min-height: 0" in css


def test_note_detail_thumbnails_stay_on_one_scrollable_row():
    """缩略图条**一行展示 + 超宽左右滑**（v1.55 用户口径）。

    旧实现是 antd `Space wrap` —— 图一多就堆成好几行，把「下载当前图片」与素材
    说明顶出首屏，左栏被迫多滚一屏。现在永不分行：`flex-wrap: nowrap` +
    `overflow-x: auto`。三个断言各管一头：class 在手、不换行、放不下能滑。
    """
    source = _read(NOTE_DETAIL_MODAL)
    assert '"note-detail-thumbs"' in source, "缩略图条必须挂 .note-detail-thumbs class"
    # 反向：整个弹窗不再有 `Space wrap`（换行复发即回归；下载按钮那处单按钮包装
    # 同轮改成普通 div —— 一个按钮没有「wrap」可言）。
    assert "<Space wrap" not in source, "弹窗里又出现 Space wrap 了？缩略图条会重新堆成多行"
    css = _read(GLOBAL_CSS)
    # 起始标记带 `{` ⇒ 注释里的同类名字面量不会抢跑（DEF-057：全文断言会被别处遮过）。
    block = _slice_between(css, ".note-detail-thumbs {", "}")
    assert "flex-wrap: nowrap" in block, "缩略图条不得换行"
    assert "overflow-x: auto" in block, "超出一行时必须能左右滑"


# ---------------------------------------------------------------------------
# 账号矩阵 · 卡片五字段统一（R4 / DEF-039）
# ---------------------------------------------------------------------------


def test_accounts_card_renders_one_info_block_for_both_types():
    """PC 与 Creator 必须共用同一段渲染：creator 的指标行不得再被整行隐藏。"""
    source = _read(ACCOUNTS)
    assert "isCreator ? null" not in source
    for label in ("类型", "小红书号", "粉丝", "关注", "获赞数"):
        assert label in source, f"卡片缺少字段：{label}"


def test_accounts_card_shows_the_reason_when_metrics_are_missing():
    """`-` 只说明「没有值」，原因必须看得见（文案来自后端 `metrics_reason`）。"""
    source = _read(ACCOUNTS)
    assert "metricsNote" in source
    assert "metrics_reason" in source


def test_accounts_page_only_syncs_metrics_on_explicit_click():
    """**禁止在页面加载时自动打平台**：同步只能挂在 onClick 上。"""
    source = _read(ACCOUNTS)
    for line in source.splitlines():
        if "handleSyncMetrics(" in line and "async function" not in line:
            assert "onClick" in line, f"互动数据同步只能由显式点击触发：{line.strip()}"
    assert "syncAccountMetrics" in source


# ---------------------------------------------------------------------------
# R10 / D16 · 缺值渲染 `0` + 原因行就是后端给的短文案
# ---------------------------------------------------------------------------


def test_accounts_card_renders_zero_for_missing_counts():
    """三项计数缺值必须渲染 `0`（不是 `-`）；`|| "-"` 是这条要求的旧形态。"""
    source = _read(ACCOUNTS)
    for field in ("followers", "following", "likes"):
        assert f'profileValue(account, "{field}") ?? "0"' in source, f"{field} 缺值应渲染 0"
        assert f'profileValue(account, "{field}") || "-"' not in source, (
            f"{field} 仍在用 `|| \"-\"` 兜底（R10/D16 要求渲染 0）"
        )
    # 小红书号不是计数，「没有」与「是 0」对它没有区别 → 仍用 `-`
    assert 'profileValue(account, "red_id") || "-"' in source


def test_accounts_card_reason_line_has_no_long_prefix():
    """原因行直接就是后端给的 25–30 字（前端再加「互动数据：」前缀就又超长了）。"""
    source = _read(ACCOUNTS)
    assert "互动数据：{metricsNote}" not in source
    assert "{metricsNote}" in source


def test_accounts_page_does_not_hardcode_business_reasons():
    """原因文案一律来自后端：前端不得出现「创作者平台」这类业务判断（D9 的延续）。

    ⚠️ 断言前**必须剥掉注释**：迁移说明里会引用被删掉的旧文案（讲清楚
    「不再显示什么」是天经地义的），直接对全文找子串会被自己的注释命中。
    """
    code = _code_only(_read(ACCOUNTS))
    assert "创作者平台" not in code


# ---------------------------------------------------------------------------
# 商品区文案（2026-09-19 用户两次拍板：只说状态，不做行为解释）
# ---------------------------------------------------------------------------


def test_my_notes_goods_line_has_no_behaviour_explainer():
    """商品来源行只报状态（已按需补齐 N/M + 时间），**不再**解释补齐行为。

    用户 2026-09-19 明确移除「（打开笔记时自动补齐；也可点「同步商品」批量补）」：
    按需补齐是**默认行为**，把机制写在页面上属于噪声（与 D11 修订同源）。
    """
    source = _read(MY_NOTES)
    assert "打开笔记时自动补齐" not in source
    assert "也可点「同步商品」批量补" not in source


def test_no_same_shop_multi_account_hint_remains():
    """「同店 N 个账号」的提示**必须全部消失**（2026-09-22 口径：店铺 ↔ 账号 1:1）。

    ⚠️ **被取代的旧断言（原句保留）**：本用例原名
    `test_shop_sharing_hint_requires_more_than_one_account`，断言
    *「T-32（N:M）：同一店铺被多个账号读取时，页面必须讲清「是同一份数据」」* ——
    具体断言 `"shop_account_count" in notes_source` / `"account_count" in items_source`
    / `"> 1" in items_source` / `"数据是同一份" in items_source`。

    用户 2026-09-22 拍板 **不允许再出现「同店铺、多账号」场景**（一个店铺下只需存
    多个平台的会话令牌），后端字段与前端提示一并移除。所以这里翻成**反向**护栏：
    那句话、那两个字段都不许回来 —— 留着只会让用户以为还有多账号读取场景。

    反向断言按纪律先剥注释（`_code_only`）：解释性注释里**一定**会提到被删掉的
    `shop_account_count` / 「同店」等字样，直接搜源码会恒真。
    """
    notes_source = _code_only(_read(MY_NOTES))
    items_source = _code_only(_read(SHOP_ITEMS))
    for source, name in ((notes_source, "我的笔记"), (items_source, "商品列表")):
        assert "shop_account_count" not in source, f"{name} 页仍在读已删除的 shop_account_count"
        assert "account_count" not in source, f"{name} 页仍在读已删除的 account_count"
        assert "同店" not in source, f"{name} 页仍有「同店多账号」的提法"
        assert "数据是同一份" not in source, f"{name} 页仍有 N:M 的共享说明"
    # 正向：账号 → 店铺的标注曾经要保留（1:1 之后一行一个店名）。
    # ⚠️ **被取代的旧断言（原句保留）**：`assert "未绑店铺" in items_source` —— 它钉的是
    # *「账号下拉的标签里必须有店铺标注」*。2026-09-24（用户口径：「所有账号的下拉，
    # 只展示账号名称」）**推翻**了它：下拉标签连店铺名也一起去掉，四处下拉统一成
    # 「只展示账号名称」。店铺信息由列表主体回答，下拉只负责「选哪个账号」。
    # 所以这条正向断言翻成反向：`未绑店铺` 这个标签后缀不许回来。
    assert "未绑店铺" not in items_source, "账号下拉已改为「只展示账号名称」，店铺后缀不得回归"


def test_my_notes_goods_line_still_reports_coverage():
    """2026-10-06 用户要求**移除**「已按需补齐 N/M 条商品笔记」覆盖率文案。

    原断言（保留）：「去掉解释句不等于去掉状态，覆盖率与同步时间必须还在」——
    后来用户进一步要求把覆盖率也一并去掉（「清除」），商品来源状态行现在只剩：
    账号名 + 同步时间 + 巡检时间 + 「立即巡检」按钮。同步时间仍在（`synced_at`）。
    """
    source = _read(MY_NOTES)
    assert "已按需补齐" not in source, "覆盖率文案已按用户要求移除，不得回归"
    assert "条商品笔记" not in source, "覆盖率文案已按用户要求移除，不得回归"
    # 同步时间仍在：`goodsInfo.synced_at` 逻辑保留（商品来源状态行诚实展示「上次同步时间」）。
    assert "synced_at" in source


def test_my_notes_goods_coverage_uses_the_fresh_notes_response():
    """覆盖率必须显示**每次加载笔记带回的最新数字**，而不是 `capabilities` 快照。

    用户 2026-09-19 实测报出的 bug：浏览时按需补齐的商品**没有**计进「已按需补齐 N/M」，
    只有点「同步商品」（它会刷新 capabilities）才看到数字变化。

    根因：覆盖率原来只读 `capabilities.capabilities.goods`，而 capabilities 只在
    账号切换与绑/同步/解绑/巡检之后刷新 —— 按需补齐发生在**每次加载笔记**时，
    所以那份数字永远落后一步。修法：加载笔记时把响应里的 `goods_source` 存下来，
    覆盖率**优先**用它。
    """
    source = _read(MY_NOTES)
    assert "setGoodsSource(data.goods_source)" in source, "加载笔记时必须存下最新的 goods_source"
    assert "goodsSource ?? capabilities" in source, "覆盖率必须优先用新鲜的那份"
    # 覆盖率行不得再直接读 capabilities 里的计数（那是修复前的写法）。
    assert "capabilities.capabilities.goods.synced_notes" not in source
    assert "capabilities.capabilities.goods.shop_total" not in source


# ---------------------------------------------------------------------------
# M1.9 账号矩阵同步（T-807）· 账号卡片两个同步按钮
# ---------------------------------------------------------------------------


def test_shop_goods_sync_button_is_not_type_gated():
    """v1.31：「同步商品」按钮**所有账号都渲染**，不再按 `isCreator` 门禁。

    用户 2026-09-21 口径：「账号信息卡的底部按钮不再按照账号类型进行区分全部展示」。
    旧版把它包在 `{isCreator ? (...)}` 里（理由：PC 账号从来没有千帆绑定）——
    那个理由已不成立：按钮现在给的是「缺千帆会话 → 去补登」的可操作提示
    （见 `test_sync_buttons_require_qianfan_session`），不再是「点了必错」。
    """
    source = _read(ACCOUNTS)
    assert "同步商品" in source, "所有账号卡片上都必须有「同步商品」入口"
    assert "{isCreator ? (" not in source, "商品同步按钮不得再按账号类型条件渲染"
    # 按钮点击必须走千帆门禁处理函数（不是直接 startGoodsSync）
    assert "handleGoodsSyncClick(account)" in source


def test_sync_buttons_require_qianfan_session():
    """§19：同步笔记三态门禁 / 同步商品千帆门禁，判据**只在 sync-gates.ts 一份**。

    守卫点：
    ① 页面不得自建第二份门禁判据（`function channelUnavailable` 必须只在
    `lib/sync-gates.ts`；页面里再写一份 = 两份必然漂移，DEF-044/047/058 同型）；
    ② 判据只拦三种确定不可用（missing / guest / expired），`unknown` 必须放行
    （DEF-057 红线：用「没检查」冒充「不可用」会让人做多余的重登）；
    ③ 两个同步按钮的点击都过门禁处理函数（各自定位到自己的函数体内，
    只断言字符串存在的话删掉其中一个另一个还在 ⇒ 断言恒真）；
    ④ 两个提示弹窗都在，且「去补登」打开 RenewLoginModal 续期。
    """
    source = _read(ACCOUNTS)
    gates = _read(ROOT / "frontend/src/lib/sync-gates.ts")
    # ① 判据单一来源：页面只 import，不再自己定义
    assert "function channelUnavailable" not in source, "门禁判据不得在页面里自建第二份"
    assert "UNAVAILABLE_STATES" not in source, "不可用名单不得在页面里重复声明"
    assert 'from "../../../lib/sync-gates"' in source, "门禁必须从 lib/sync-gates 导入"
    assert "function channelUnavailable" in gates
    # ② 三种确定不可用 + unknown 不在名单里（只看声明那一行，别被注释里的提及骗到）
    declaration = [
        line for line in gates.splitlines() if "UNAVAILABLE_STATES =" in line
    ][0]
    for state in ("missing", "guest", "expired"):
        assert f'"{state}"' in declaration, f"不可用的判定里缺少 {state}"
    assert "unknown" not in declaration, "unknown 不得进不可用名单（DEF-057 红线）"
    # ③ 两个按钮都过门禁 —— 必须各自定位到自己的处理函数体内
    note_handler = _slice_between(source, "function handleNoteSyncClick", "function handleGoodsSyncClick")
    goods_handler = _slice_between(source, "function handleGoodsSyncClick", "async function startGoodsSync")
    assert "noteSyncPlan(" in note_handler, "「同步笔记」必须过三态门禁（noteSyncPlan）"
    assert "plan.blocked" in note_handler and "plan.needsLink" in note_handler
    assert "goodsGate(" in goods_handler, "「同步商品」必须过千帆门禁（goodsGate）"
    # ④ 两个提示弹窗 + 续期入口
    assert "setNotePrompt(account)" in note_handler, "全不可用必须弹「分别登录」提示"
    assert "setArkPrompt(account)" in goods_handler, "千帆不可用必须弹「补登千帆」提示"
    assert "setRenewTarget({" in source and 'channel: "ark"' in source, "弹窗要能打开 ark 续期"


def test_note_sync_gate_is_creator_only():
    """「同步笔记」的门禁**只认创作者平台会话**（设计 13 §21.4，2026-09-22 用户口径）。

    来源：「www 源在这里是冗余的，则移除不要，使用 creator 源」+
    「同步笔记按钮该账号必须存在有效的创作者登录会话，不存在有效创作者平台登录
    会话的提示补齐后再触发」。

    本条守卫三件事：
    1. 判定本体在 gates 里，且**不再提 www**（写回 `wwwOk` 即回归）；
    2. 页面接线保留：`plan.blocked` ⇒ 弹补齐提示；
    3. 触发同步时**不再传 profile_url / max_pages**（两个都在 §21 里退场）。
    """
    source = _read(ACCOUNTS)
    gates = _read(ROOT / "frontend/src/lib/sync-gates.ts")
    assert "noteSyncPlan(" in source
    note_handler = _slice_between(source, "function handleNoteSyncClick", "function handleGoodsSyncClick")
    assert "plan.blocked" in note_handler and "setNotePrompt(account)" in note_handler
    # 门禁只认 creator：`wwwOk` 不得再出现在 gates 的决策里
    assert "wwwOk" not in gates, "www 源已退场（§21.4），决策里不许再有 wwwOk"
    assert "blocked: !creatorOk" in gates
    # 触发时不再带 URL 令牌 / 页数上限
    assert "profileUrl" not in _slice_between(
        source, "async function startNoteSync", "function handleDelete"
    ), "§21 起同步不再需要主页链接（www 源退场）"
    # §23：调用形如 `syncAccountNotes(accountId, options)` —— options 只可能带
    # 弹窗选的 scope/起止日，**不得**再夹带 max_pages 默认值（那正是「1961 条
    # 永远只同步 180 条」的入口）或 profileUrl（www 源已退场）。
    note_sync_body = _slice_between(source, "async function startNoteSync", "function handleDelete")
    assert "syncAccountNotes(accountId, options)" in note_sync_body
    assert "max_pages" not in note_sync_body and "maxPages" not in note_sync_body


def test_goods_note_sync_has_its_own_button_and_gate():
    """**§44**（2026-09-30 用户拍板拆分）：「同步商品笔记」= **独立按钮 + 独立门禁 + 独立集合**。

    用户诉求原文：「把『同步笔记』拆成『同步普通笔记』+『同步商品笔记』两个独立任务 + 入口，
    在账号信息卡分开」。这条守卫钉**接线**（判据本体的行为由 `sync_gates.cjs` 的 J1–J4 真实驱动）：

    ① 判据只有一份（`lib/sync-gates.ts::goodsNoteSyncPlan`），页面不得自建第二份；
    ② `handleGoodsNoteSyncClick` 过该门禁，被拦时弹**自己的**补登提示（`setGoodsNotePrompt`）——
       复用 `setArkPrompt` 会让用户看到「补登千帆」却不知道为哪件事补；
    ③ 触发走**独立请求函数** `syncGoodsNotes`（任务类型不同 ⇒ 不得复用 `syncAccountNotes`）；
    ④ 独立轮询集 `goodsNoteSyncIds` + 三条链路共用一张接线表 `syncChannels`
       （首查 / 轮询 / 按钮态都从它遍历，少一条链路就没有任何断言能发现）。

    ⚠️ ⑤ 与「同步商品」（店铺商品台账）**必须区分**：按钮文案要带「笔记」二字，
    否则用户在卡片上看到两个语义不同的「同步商品」。
    """
    source = _read(ACCOUNTS)
    code = _code_only(source)
    gates = _read(ROOT / "frontend/src/lib/sync-gates.ts")

    # ① 判据单一来源
    assert "export function goodsNoteSyncPlan" in gates, "商品笔记门禁本体必须在 lib/sync-gates.ts"
    assert "function goodsNoteSyncPlan" not in source, "门禁判据不得在页面里自建第二份"
    assert "goodsNoteSyncPlan(" in code, "页面必须真的调用它（只 import 不算接线）"
    # 判据复用同一个 ark 门禁（goodsGate），不许另写一份 unavailable 名单
    assert "UNAVAILABLE_STATES" not in code, "不可用名单不得在页面里重复声明"

    # ② 点击处理：门禁 + 自己的提示
    handler = _slice_between(
        code, "function handleGoodsNoteSyncClick", "async function startNoteSync"
    )
    assert "goodsNoteSyncPlan(" in handler and "plan.blocked" in handler
    assert "setGoodsNotePrompt(account)" in handler, "ark 不可用必须弹「补登千帆」提示"
    assert 'kind: "goods_notes"' in handler, "门禁通过后必须进「商品笔记」的选项弹窗"
    assert "同步商品笔记" in source, "按钮 / 弹窗文案必须带「笔记」二字（与店铺商品区分）"

    # ③ 触发走独立请求函数
    starter = _slice_between(
        code, "async function startGoodsNoteSync", "const syncChannels"
    )
    assert "syncGoodsNotes(accountId, options)" in starter
    assert "syncAccountNotes(" not in starter, "商品笔记不得复用普通笔记的请求函数"
    assert "setGoodsNoteSyncIds" in starter and "goodsNoteSyncIds" in starter

    # ④ 独立轮询集 + 共用接线表
    assert "const syncChannels: SyncChannel[] = [" in code, "链路接线表必须在（新增链路只加一行）"
    channels = _slice_between(code, "const syncChannels: SyncChannel[] = [", "async function firstQuerySyncStatus")
    for kind in ('kind: "notes"', 'kind: "goods_notes"', 'kind: "goods"'):
        assert kind in channels, f"接线表缺少 {kind}（漏一条 = 那条链路的按钮切不回来）"

    # ⑤ 与店铺商品按钮区分：两颗按钮的文案各是各的
    assert '{isGoodsNoteSyncing ? "同步中" : "同步商品笔记"}' in code
    assert '{isGoodsSyncing ? "同步中" : "同步商品"}' in code


def test_accounts_sync_progress_renders_dash_for_unknown_total():
    """分母**未知时不渲染占位符**（§35.18.6 取代旧的「渲染 `-`」口径）。

    ⚠️ **被取代的原断言（保留备查）**：本节原要求 `'total ?? "-"' in gates` —— 即把
    未知总数渲染成「已落库 N / 共 - 条」。2026-09-27 用户报障显示那句话会被读成
    「卡住了」（区间 / 增量同步本来就没有确定分母，占位符把「未知」说成「已知的 0」）
    ⇒ 新口径：`total` 缺失只报「已落库 N 条」，且有界同步后端**不写 `total` 键**。

    §19 起渲染函数在 `lib/sync-gates.ts`（行为脚本 I1–I3 钉的是「怎么跑」）；
    这里钉「只有一份实现」：页面不得再留一份 `total ?? `。
    """
    source = _read(ACCOUNTS)
    gates = _read(ROOT / "frontend/src/lib/sync-gates.ts")
    assert 'total ?? "-"' not in gates, "分母未知不许渲染占位符（用户会读成「卡住了」）"
    assert "if (total == null) {" in gates, "分母缺失必须有独立分支（缺值口径）"
    assert "total ??" not in source, "渲染口径只留 sync-gates 一份，页面不得再写"


def test_sync_metrics_requires_www_session_and_prompts_login():
    """v1.31：「同步数据」走 PC 取计数，**PC 会话不可用时弹窗提示登录**。

    用户 2026-09-21 口径：「[同步数据]走pc获取粉丝关注获赞数，如果pc会话不可用
    则提示登录」。守卫点：
    ① 点击处理函数先过 www 门禁（同一个 channelUnavailable 判据）；
    ② 弹窗存在，且「去登录」打开 **www 通道**的续期弹窗（PC 扫码）；
    ③ 仍然只在显式点击时触发（不在页面加载时自动打平台）。
    """
    source = _read(ACCOUNTS)
    metrics_handler = _slice_between(source, "async function handleSyncMetrics", "function handleNoteSyncClick")
    assert 'channelUnavailable(loginChannels[account.id], "www")' in metrics_handler, (
        "「同步数据」的处理函数里必须过网页版会话门禁"
    )
    assert "webPrompt" in source
    assert "需要网页版会话" in source
    assert "去登录" in source
    assert 'channel: "www"' in source, "登录入口必须是 www 通道的续期弹窗"
    # ③ 显式点击才触发（守卫原本就存在，这里顺带钉住不回归）
    for line in source.splitlines():
        if "handleSyncMetrics(" in line and "async function" not in line:
            assert "onClick" in line, f"互动数据同步只能由显式点击触发：{line.strip()}"


def test_accounts_page_has_no_check_button():
    """v1.31：「检查」按钮功能移除（用户 2026-09-21 口径）。

    登录态探活已由「进入页面 / 点刷新」自动完成（v1.30），按钮形式的手工检查
    成为多余入口。守卫只认**代码形态**（handleCheck / 检查中），注释里提到不算
    （`_code_only` 先剥注释 —— 卡片的迁移说明里就写着「检查」按钮已移除）。
    """
    code = _code_only(_read(ACCOUNTS))
    assert "handleCheck" not in code
    assert "检查中" not in code
    assert "checkAccount" not in code


def test_accounts_sync_polling_uses_five_seconds_and_stops_on_unmount():
    """轮询 5 秒、不引 SSE、卸载要清定时器（设计 13 §6.2）。"""
    source = _read(ACCOUNTS)
    assert "SYNC_POLL_INTERVAL_MS = 5000" in source
    assert "setTimeout" in source
    assert "clearTimeout" in source, "卸载时必须清掉定时器，否则离开页面还在打后端"
    assert "EventSource" not in source, "设计明确不引入 SSE"
    # 只依赖两个 Set 会让轮询在状态不变时**静默停在第 2 次** —— 节拍必须真的进依赖数组。
    # ⚠️ §44（2026-09-30）拆出「同步商品笔记」⇒ 三个 Set **全部**要在依赖里：
    # 少一个就有「商品笔记跑着、轮询却不再重排」的同型停摆（与少 gap 的 `syncPollTick` 一样隐蔽）。
    assert "[noteSyncIds, goodsNoteSyncIds, goodsSyncIds, syncPollTick]" in source, (
        "三条链路的 Set 与 syncPollTick 必须全部出现在 effect 的依赖数组里；只在别处出现不算数"
    )


def test_accounts_sync_polling_keeps_running_until_terminal():
    """摘除账号必须写在 `try/catch` **之后**，不得写进 `finally`（设计 13 §6.2 的实现坑）。

    「未结束就继续跑」的分支用的是 `return`，而 `finally` 对 `return` 同样生效 ——
    一旦把 `next.delete(accountId)` 放进 `finally`，**第一轮轮询就把账号移出集合**，
    轮询当场停摆，卡片进度行永久冻结在「已落库 N / 共 M」。症状极具欺骗性：
    页面看起来还在跑（按钮仍是 disabled），其实数据再没更新过。

    这条守卫的价值在于**能区分两种写法**：把删除语句裹回
    `try { … } finally { … }` 之后必须变红 —— 只断言「`next.delete` 存在」是抓不住的。

    ⚠️ 2026-09-27（§35.17.4）改判据位置：`pending` / `running` 的字面量比较**搬进了
    `lib/sync-gates.ts` 的 `syncTaskPhase`**（页面里再比一次 `status === "running"`
    = 两份判据必然漂移，DEF-044）。所以这里断言的顺序变成「`syncTaskPhase(...) ===
    "active"` 的判定排在 `next.delete` 之前」，并额外守住「页面不得自建第二份状态名单」
    —— 否则这次改动等于把守卫悄悄架空。
    """
    source = _read(ACCOUNTS)
    start = source.index("async function pollSyncTasks")
    end = source.index("async function handleDelete", start)
    body = source[start:end]
    # 注释里为了说明坑因会提到 `finally`，所以只对**代码**做结构判断。
    # ⚠️ 两种注释都要剥：`//` 行注释**与** `/* … */` 块注释（JSDoc）。
    # 只剥 `//` 会让函数上方的 JSDoc 里那句「不写 finally」把守卫打红 ——
    # 守卫自身写的「只对代码做结构判断」就会失真（2026-09-27 实测踩到）。
    code = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)

    assert "next.delete(accountId)" in code, "轮询结束必须把账号移出集合（否则会一直打后端）"
    assert "finally" not in code, (
        "轮询函数里不得出现 finally：`active` 分支是 return，finally 会连带执行，"
        "导致第一轮就把账号摘除、轮询停摆"
    )
    assert code.index('syncTaskPhase(status) === "active"') < code.index("next.delete(accountId)"), (
        "摘除必须排在「仍在跑」的判定之后"
    )
    # 「未结束」的判据**只有一份**（住在 lib/sync-gates.ts），页面不许再比一次
    assert "SYNC_ACTIVE_STATES" not in code, "状态名单不得在页面里重复声明（DEF-044）"
    assert '"running"' not in code, "「在跑」的字面量比较属于 syncTaskPhase，页面不得自建第二份"
    assert "syncTaskPhase" in _read(ROOT / "frontend/src/lib/sync-gates.ts"), "判据本体必须在 lib 里"


# ---------------------------------------------------------------------------
# 长文案溢出 · `…` 收起 + Tooltip 看全（用户 2026-09-19 拍板；ADR-018 修订）
# ---------------------------------------------------------------------------


def test_accounts_long_lines_collapse_with_tooltip_instead_of_wrapping():
    """三类可能超长的行都必须「单行 `…` + 悬停 tip」，**不得靠换行**。

    用户实测报出：「读取互动数据失败，请稍后重试，或重新登录网页版账号」这类提示
    在窄列（`lg=8`）里一行放不下。这些文案的长度**不由前端决定**（后端 25–30 字、
    平台原文可到 30 字），所以只能定一条溢出现则。换行会让卡片变高、把按钮挤出可视区，
    同屏卡片高度还会参差不齐。

    防回退的价值：把 `ellipsis={{ tooltip }}` 改回裸 `<Text>{text}</Text>`
    **不会让任何别的断言变红**，但用户会立刻看到那半截文案。
    """
    source = _read(ACCOUNTS)
    # ⚠️ 2026-09-27：`noteSyncText` / `goodsSyncText` 随「同步任务」进度区块一并移除
    # （用户口径「点击后展示的同步任务进度卡移除」）⇒ 它们不再是这一页的超长行载体。
    carriers = (
        "metricsNote",
        "account.status_message",
        "updatedText",
    )
    for carrier in carriers:
        expected = f"ellipsis={{{{ tooltip: {carrier} }}}}"
        assert expected in source, f"{carrier} 缺少 `…` + tip 的溢出处理"


# ---------------------------------------------------------------------------
# 详情弹窗的「挂载商品」只有一个区块（DEF-047，用户 2026-09-20 报出）
#
# 报出的现象是一条**时序**缺陷：弹窗一打开商品卡片是完整的，正文请求回来之后
# 只剩一行空图。成因在两个地方同时成立才会显形 —— 后端详情走 www feed、payload
# 里**结构上没有商品容器**（`goods` 恒 `null`），前端又用裸展开合并，让这个 `null`
# 盖掉了列表行已经补齐的那一份。所以守卫必须同时钉住「不全量合并」与「只渲染一块」。
# ---------------------------------------------------------------------------


def _merge_note_detail_body() -> str:
    """取出 `mergeNoteDetail` 的函数体（只在这个函数里断言，避免全文误命中）。"""
    source = _read(MY_NOTES)
    match = re.search(
        r"function mergeNoteDetail\((?:.|\n)*?\n\}\n", source
    )
    assert match, "`mergeNoteDetail` 不见了 —— 详情合并又回到裸展开了吗？"
    return match.group(0)


def test_note_detail_merge_keeps_the_goods_the_list_already_had():
    """详情缺 `goods` 时必须保留列表行那一份，**且要在展开之后**才写。

    这条断言的顺序部分不是形式主义：把 `goods` 一行挪到 `...detail` **之前**，
    字符串仍然在文件里、内容看着也对，但运行时会被展开覆盖——正是 DEF-047 的写法。
    只断言「存在」会放过它，所以这里比的是**下标**。
    """
    body = _merge_note_detail_body()
    # 2026-10-06：`goods` 改成**字段级回填**（修「价格闪现消失」）：详情非空但 price
    # 为空时保留列表行补过价的 price。回退仍走 `detail.goods ?? note.goods ?? null`。
    assert "detail.goods ?? note.goods ?? null" in body, (
        "详情与列表的合并没有回退到 note.goods —— 详情返回 null 时会丢掉商品（DEF-047）"
    )
    spread_at = body.index("...detail")
    goods_at = body.index("detail.goods ?? note.goods ?? null")
    assert spread_at < goods_at, (
        "goods 必须写在 `...detail` **之后**：写在前面会被展开覆盖，等于没修"
    )


def test_note_detail_renders_one_and_only_one_goods_block():
    """弹窗里「挂载商品」只有一个区块（合并后的 `mountedGoods`）。

    早前是两个来源各渲染一块、版式还不一样，正文一到达版式就跳。
    这里同时钉住「唯一性」与「唯一的那一块就是合成结果」——数量为 1 但渲染的是
    别的来源，同样会让版式漂移。
    """
    source = _read(MY_NOTES)
    assert "function mountGoods(" in source, "两个来源必须先合成一份（mountGoods）"
    assert "const mountedGoods = mountGoods(" in source, "渲染的必须是合成结果"
    # 旧写法是直接遍历 `shop.goods` 渲染第二块；它一旦回来就是「两块商品、两套版式」。
    assert not re.search(r"\.goods[\s\S]{0,120}?\.map\(", source), (
        "又出现了直接遍历 `.goods` 的渲染块（第二块商品 → 版式漂移）"
    )
    assert source.count("mountedGoods.map") == 1, "挂载商品必须只有一个渲染块"
    assert source.count("className=\"goods-meta-row\"") == 1, "规格+价格的同行容器必须唯一"


def test_note_detail_goods_title_is_single_line_with_tooltip():
    """商品标题**单行** `…` 收起 + 悬浮看全（用户 2026-09-20 要求）。

    商品标题长度完全由平台决定（可到 30+ 字），换行会把卡片高度撑起来、
    同屏卡片还会参差；这是 ADR-018 修订那条溢出现则在商品区的落地。
    """
    source = _read(MY_NOTES)
    assert "ellipsis={{ tooltip: g.title" in source, (
        "商品标题缺少 `…` + tip 的溢出处理"
    )


def test_goods_variant_and_price_share_one_row():
    """规格与价格**同行**展示：规格可让位（`…`+tip），价格永远完整。

    两行分开时，规格短的商品看起来像少了个字段；同行才能一眼对上是「哪个规格多少钱」。
    容忍 `flex-wrap` 之外的换行：`nowrap` 由 CSS 类保证，所以这里同时校验类定义。
    """
    source = _read(MY_NOTES)
    assert '<div className="goods-meta-row">' in source
    assert "ellipsis={{ tooltip: g.variant }}" in source, "规格溢出同样要 `…` + tip"
    # ⚠️ `whiteSpace: "nowrap"` 在本文件里出现 **3 次**（票号行 / 链接行 / 价格行），
    # 断言全文存在等于没断言 —— 变异验证实测：把价格那处改成 `normal`，全文断言**依然绿**。
    # 所以只在这个价格块里断言。
    price_at = source.index("{g.price && (")
    assert 'whiteSpace: "nowrap"' in source[price_at : price_at + 400], (
        "价格不得被压缩换行（必须在价格块内部断言，全文断言会被别处的同名字符串遮住）"
    )

    css = _read(GLOBAL_CSS)
    assert ".goods-meta-row {" in css, "同行容器的样式类不见了"
    block = css.split(".goods-meta-row {", 1)[1].split("}", 1)[0]
    assert "display: flex" in block, "规格与价格必须同一行（flex），否则就是两行"
    assert "min-width: 0" in css.split(".goods-meta-row > :first-child", 1)[1][:120], (
        "flex 子项必须允许被压窄，否则规格省略不掉、会把价格挤出容器"
    )


# --------------------------------------------------------------------------- #
# §19 同步按钮三态门禁：行为验证（esbuild 打包真实 sync-gates.ts）
# --------------------------------------------------------------------------- #
def _run_sync_gates(env: dict | None = None):
    script = ROOT / "tests/frontend/sync_gates.cjs"
    assert script.exists(), "行为验证脚本缺失（tests/frontend/sync_gates.cjs）"
    try:
        return subprocess.run(
            ["node", str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError:  # pragma: no cover - 环境问题，不是逻辑分支
        pytest.fail("未找到 node：本机无法运行前端行为验证（AGENTS.md §2 已依赖 node 跑 tsc）")


def test_sync_gate_three_states_are_driven_for_real():
    """三态门禁 / 分源摘要 / 缺值渲染 / 取消与耗时 / 首查驱动轮询 / 终态提示有**真实打包产物**验证。

    40 条 = A(2) + B(1) + C(4) + D(4) + E(5) + F(3) + G(1) + H(11) + I(5) + **J(4)**：
    创作者会话不可用必须拦、unknown 必须放行、源清单只含 creator（www 于 §21.4 退场、
    **ark 于 §44 拆出**）、被限流的源说「未跑完」不说「0 条」、详情剩余缺值渲染 `-`、
    cancelled 终态带耗时（E4/E5，§24.8）；
    H1–H7（§35.17.4，2026-09-27）盯「**要不要继续 5s 轮询**」这个决策：在跑 ⇒ active、
    已结束（含 failed / cancelled 两个非正常终态）⇒ finished、没跑过 ⇒ idle；
    H8–H11（§35.18，2026-09-27）盯「终态要不要打扰用户」：只有 failed / cancelled
    才给文案（成功与「没有结果」都是空串）—— 卡片里的进度区块已按用户口径移除，
    非正常终态若不弹就是彻底静默（失败三分法：真异常要给真原因）。
    I1–I5（**§35.18.6**，2026-09-27 用户报障）盯**进度分母三态**与**取消说明**：
    只有无界全量才报分母、有界（`total` 缺失）只报「已落库 N 条」（**不渲染 `/ 共 -`**）、
    详情补齐阶段优先报它、取消结果行剥掉「已被用户取消」后缀。
    J1–J4（**§44**，2026-09-30 用户拍板拆分）盯**两条门禁互不牵连**：商品笔记的硬门禁是
    ark（不是 creator）、它只跑 ark 源、creator 挂了不妨碍商品笔记（且反之亦然）。
    """
    result = _run_sync_gates()
    assert result.returncode == 0, (result.stdout + result.stderr)[-900:]
    assert "40 PASS / 0 FAIL" in result.stdout


@pytest.mark.parametrize(
    "mutate,expected",
    [
        ("blocked", "A1"),      # creator 不可用也放行 ⇒ 硬门禁失效
        ("unknown", "B1"),      # unknown 算「不可用」⇒ DEF-057 红线
        ("wwwleak", "C3"),      # www 泄漏进源清单 ⇒ 退化成旧的「三源」契约
        ("source", "E1"),       # 分源摘要吞掉失败原因 ⇒ 用户失去自救能力
        ("limited", "E3"),      # 被限流显示成「0 条」⇒ 把没跑完伪装成没数据
        ("dash", "F1"),         # 详情剩余缺值渲染 0 ⇒ 把未知说成已知
        ("goodsgate", "D1"),    # 千帆不可用也放行同步商品
        ("cancel", "E4"),       # cancelled 分支失效 ⇒ 取消后结果行误导（§24.8）
        ("phaseactive", "H2"),  # 「进行中」判据失效 ⇒ 在跑的任务被当结束、首查当场摘出轮询集
        ("phaseidle", "H4"),    # 已结束一律当 idle ⇒ 轮询停不下来（对着不会再变的任务无限打后端）
        ("terminalnotice", "H8"),  # 成功也弹提示 ⇒ 每次同步完都打扰用户（用户口径：只留按钮态）
        ("progressdash", "I2"),    # 分母缺失仍渲染「/ 共 -」⇒ 用户读成「卡住了」（§35.18.6）
        ("cancelsuffix", "I4"),    # 不剥取消后缀 ⇒ 结果行出现两次「取消」（§35.18.6）
        ("arkleak", "C2"),         # ark 混回普通笔记源清单 ⇒ §44 拆分白做
        ("goodsnotegate", "J1"),   # 商品笔记的 ark 硬门禁失效 ⇒ 点了必失败
        ("goodsnotesrc", "J3"),    # 商品笔记把 creator 混进来 ⇒ 又回到多源任务
    ],
)
def test_sync_gates_guards_are_load_bearing(mutate, expected):
    """变异验证：每个守卫被改坏时脚本必须变红（否则它没在守任何东西）。

    断言写成 `FAIL  Xn` 这种**带前缀**的形式：只找 `A1` 的话，全绿时那行
    `PASS  A1 …` 同样命中，等于什么都没断言（DEF-058 的教训）。
    """
    result = _run_sync_gates({"SYNC_GATES_MUTATE": mutate})
    assert result.returncode != 0, f"{mutate} 变异必须让脚本失败"
    assert f"FAIL  {expected}" in result.stdout, f"stdout={result.stdout}"


def test_accounts_page_sync_state_is_driven_by_first_query():
    """账号卡的「同步中」按钮态由**首查 + 轮询**驱动（设计 13 §35.17.4 / §35.18）。

    用户口径（2026-09-27，两轮）：
    ① 「查一次任务记录表当前账号存在对应任务则展示页签，任务非结束状态（包括正常结束和
       非正常结束）则前端 5 秒查询一次，如果为结束状态则不再轮询查询，**每次刷新按钮或
       进入页面时自动触发一次**，根据第一个获取的信息决定是否需要轮询」；
    ② 「账号信息卡中移除补齐详情逻辑……点击后展示的同步任务进度卡移除，只需要将同步笔记
       按钮改成同步中即可」—— 于是**卡片上唯一会变的东西就是按钮文案**，
       首查 / 轮询的意义收敛成两件事：入集（按钮变「同步中」）与摘出（切回「同步笔记」）。

    `syncTaskPhase` / `syncTerminalNotice` 的判据本体由 `sync_gates.cjs` 真实驱动（H1–H11），
    这里只守**接线** —— 纯逻辑对而没接上，功能一样不存在（这正是
    `test_every_behaviour_script_is_wired_into_pytest` 想防的同一类事）：

    ① 首查挂在 `loadAccounts`（进页面 / 点刷新都走它）里，且**不 await**（不能把页面
       卡在 loading 上等 N 个只读查询）；
    ② 首查按「未结束 ⇒ 入集 / 结束或没有 ⇒ 摘出」双向处置 —— 只写「入集」那一支
       等于用户口径里的「结束状态则不再轮询」从未实现；
    ③ 竞态防护在位：本页刚触发的账号不被首查那条旧快照摘出轮询集
       （否则就是 H9 那类「看着像在跑，其实再也没更新过」）；
    ④ 轮询到终态**摘出集合**（按钮切回「同步笔记」），且**只有非正常终态**报原因
       （卡片已无进度区块 ⇒ 静默吞掉失败 = 用户永远不知道失败了）；
    ⑤ 进度区块 / 补齐详情 / 取消入口**整条不在**卡片里（用户口径），
       且按钮文案确实随集合切换（反向锚点：证明前几条不是「切片打偏所以什么都没匹配到」）。
    """
    code = _code_only(_read(ACCOUNTS))
    load = _slice_between(code, "async function loadAccounts", "async function autoCheckLoginStates")
    assert "void firstQuerySyncStatus(loadedAccounts)" in load, "进入页面 / 刷新必须自动首查一次"
    assert "await firstQuerySyncStatus" not in load, "首查不得 await（会把页面卡在 loading 上）"

    query = _slice_between(code, "async function querySyncStatus", "async function pollSyncTasks")
    assert 'syncTaskPhase(status) === "active"' in query, "首查必须按 syncTaskPhase 判「要不要轮询」"
    assert "next.delete(accountId)" in query, "命中已结束 / 无任务必须**摘出**轮询集（用户口径）"
    assert "locallyTriggeredRef.current.has(accountId)" in query, (
        "本页刚触发的账号必须挡住首查的旧快照，否则轮询当场停摆"
    )

    poll = _slice_between(code, "async function pollSyncTasks", "async function handleDelete")
    assert 'syncTaskPhase(status) === "active"' in poll, "轮询同样按 syncTaskPhase 判在跑 / 已结束"
    assert "syncTerminalNotice(" in poll, "终态要不要打扰用户必须走唯一判据（sync-gates）"
    assert "if (notice) {" in poll and "message.error(notice);" in poll, (
        "非正常终态必须报出真原因（卡片已无进度区块，静默 = 用户不知道失败）"
    )
    assert "next.delete(accountId)" in poll, "终态必须摘出集合（否则按钮永远停在「同步中」）"
    assert "finally" not in poll, (
        "摘除不能写进 finally（H9）：`return` 也触发 finally ⇒ 第一轮就把账号摘出去、轮询停摆"
    )

    # ⑤ 进度区块 / 补齐详情 / 取消入口整条移除
    for gone in ("hasSyncTask", "syncBlockText", "hydratePendingNotes", "补齐详情", "cancelSync"):
        assert gone not in code, f"「{gone}」应已随进度卡一并移除（2026-09-27 用户口径）"
    # 反向锚点：按钮文案仍随集合切换，且驱动它的集合还在
    assert "const isNoteSyncing = noteSyncIds.has(account.id);" in code
    assert '{isNoteSyncing ? "同步中" : "同步笔记"}' in code, "同步笔记按钮必须能显示「同步中」"
    assert '{isGoodsNoteSyncing ? "同步中" : "同步商品笔记"}' in code, (
        "§44 起必须有「同步商品笔记」按钮，且随集合切换（否则它永远停在可点态）"
    )
    assert '{isGoodsSyncing ? "同步中" : "同步商品"}' in code, "同步商品按钮同规"


@pytest.mark.parametrize(
    "needle,replacement",
    [
        # 首查不再挂在加载 / 刷新上 ⇒ 「进入页面自动触发一次」失效
        ("void firstQuerySyncStatus(loadedAccounts);", "void 0;"),
        # 首查 await 了 ⇒ 页面卡在 loading 上等 N 个只读查询
        ("void firstQuerySyncStatus(loadedAccounts);", "await firstQuerySyncStatus(loadedAccounts);"),
        # 只写「入集」不写「摘出」⇒ 用户口径里的「结束状态则不再轮询」从未实现
        ("      } else if (locallyTriggeredRef.current.has(accountId)) {", "      } else if (true) {"),
        # 丢竞态防护 ⇒ 首查的旧快照会把刚触发的账号摘出轮询集（轮询停摆）
        ("      } else if (locallyTriggeredRef.current.has(accountId)) {", "      } else if (false) {"),
        # 终态不报原因 ⇒ 失败被静默吞掉（卡片已无进度区块，用户永远不知道）
        ("      if (notice) {", "      if (false) {"),
        # 按钮不再随集合切换 ⇒ 「同步中」永远不出现（用户口径的核心就没了）
        ('{isNoteSyncing ? "同步中" : "同步笔记"}', '{"同步笔记"}'),
    ],
)
def test_first_query_guard_is_load_bearing(monkeypatch, needle, replacement):
    """变异验证：上面那条守卫的每个判据被改坏时**必须变红**。

    为什么用 `monkeypatch` 改 `_read` 而不是临时改文件：改文件再回滚一旦中途失败
    就会把坏源码留在工作区（本项目已有「同一文件并行 Edit 竞态回退改动」的前车之鉴）。
    断言写成 `pytest.raises(AssertionError)` —— **必须真的抛**，不能只看内容，
    否则守卫写歪（比如锚点打偏）时这条变异验证自己也会绿。
    """
    import test_page_optimizations as page_opt

    source = _read(ACCOUNTS)
    assert needle in source, f"变异锚点未命中（实现已改？）：{needle}"
    mutated = source.replace(needle, replacement, 1)
    real_read = page_opt._read

    def fake_read(path: Path) -> str:
        if path == ACCOUNTS:
            return mutated
        return real_read(path)

    monkeypatch.setattr(page_opt, "_read", fake_read)
    with pytest.raises(AssertionError):
        page_opt.test_accounts_page_sync_state_is_driven_by_first_query()


# --------------------------------------------------------------------------- #
# 内容库筛选口径 + 批量同步门禁：行为验证（esbuild 打包真实 note-filters.ts）
# --------------------------------------------------------------------------- #
def _run_note_filters(env: dict | None = None):
    script = ROOT / "tests/frontend/note_filters.cjs"
    assert script.exists(), "行为验证脚本缺失（tests/frontend/note_filters.cjs）"
    try:
        return subprocess.run(
            ["node", str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError:  # pragma: no cover - 环境问题，不是逻辑分支
        pytest.fail("未找到 node：本机无法运行前端行为验证（AGENTS.md §2 已依赖 node 跑 tsc）")


def test_note_filters_and_batch_sync_gates_are_driven_for_real():
    """筛选口径 / 类型文案 / 同步与**批量同步**门禁有真实打包产物验证（§25 / §35.1）。

    43 条 = A5 + B9 + C3 + D8 + E4 + F6 + G9。2026-09-25 起本脚本**真正被 pytest
    执行**（此前只在 docstring 里被提到 —— 脚本存在 ≠ 在被守，与
    `test_every_behaviour_script_is_wired_into_pytest` 想防的事是同一条）。
    """
    result = _run_note_filters()
    assert result.returncode == 0, (result.stdout + result.stderr)[-900:]
    assert "43 PASS / 0 FAIL" in result.stdout


@pytest.mark.parametrize(
    "mutate,expected",
    [
        # 既有锚点（§25）：去掉「已发布 ⇒ 给同步」半条 ⇒ 回到漏按钮状态
        ("syncpub", "F2"),
        # §35.1 新锚点：批量门禁放宽 ⇒ 未发布/发布失败也进同步队列
        ("batchgate", "G3"),
        # §35.1：跳过不计数 ⇒ 确认框少说跳过几篇
        ("batchskip", "G7"),
        # §35.1：目标顺序反转 ⇒ 队列顺序处理变成逆序
        ("batchorder", "G8"),
    ],
)
def test_note_filters_guards_are_load_bearing(mutate, expected):
    """变异验证：批量同步门禁的三个锚点被改坏时脚本必须变红。

    断言带 `FAIL ` 前缀：只找 `G3` 会被全绿时的 `PASS G3 …` 命中（DEF-058）。
    """
    result = _run_note_filters({"NOTE_FILTERS_MUTATE": mutate})
    assert result.returncode != 0, f"{mutate} 变异必须让脚本失败"
    assert f"FAIL {expected}" in result.stdout, f"stdout={result.stdout}"


# --------------------------------------------------------------------------- #
# §25.8 内容库「挂链商品」筛选：行为验证（esbuild 打包真实 api.ts + 两页源码守卫）
#
# ⚠️ 2026-09-26 接线：本脚本自 2026-09-23 建起**从未被 pytest 执行过** —— 它在仓库里
# 唯一的"引用"是 `test_testing_spec_guards.py` 的参数列表（而那条守卫的 haystack
# 包含守卫文件自己 ⇒ 对它恒真）。同一形态的踩坑在 `note_filters.cjs`
# （2026-09-25 接线）上已经发生过一次。顺带修掉一颗哑弹：`urlinit` 变异的锚点
# 少了换行上下文、一直在 "FATAL 变异锚点未命中" 上空转。
# --------------------------------------------------------------------------- #
def _run_library_item_filter(env: dict | None = None):
    script = ROOT / "tests/frontend/library_item_filter.cjs"
    assert script.exists(), "行为验证脚本缺失（tests/frontend/library_item_filter.cjs）"
    try:
        return subprocess.run(
            ["node", str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError:  # pragma: no cover - 环境问题，不是逻辑分支
        pytest.fail("未找到 node：本机无法运行前端行为验证（AGENTS.md §2 已依赖 node 跑 tsc）")


def test_library_item_filter_is_driven_for_real():
    """挂链商品筛选的前端契约有真实打包产物验证（§25.8）。

    21 条 = A(2 驱动真实 `lib/api.ts`) + B(14 源码守卫 `library-page.tsx`)
    + C(4 源码守卫 `shop-items-page.tsx`) + 1 条口径断言。
    用户当时两次报告同一现象：挂链笔记弹窗 3 篇、内容库只看到 2 篇 ——
    本脚本钉的是「跳转入口 → URL 初值 → 筛选参数 → 提示条 / 空态」整条链路。
    """
    result = _run_library_item_filter()
    assert result.returncode == 0, (result.stdout + result.stderr)[-900:]
    assert "21 passed / 0 failed" in result.stdout


@pytest.mark.parametrize(
    "mutate,expected",
    [
        ("trim", "A1"),       # item_id 不 trim ⇒ 带空格的 ID 原样发给后端
        ("trim", "A2"),       # 空串当筛选值 ⇒ 不筛变成筛空
        ("urlinit", "B1"),    # URL ?item_id= 初值被删 ⇒ 弹窗跳进来筛选不生效
        ("cfilter", "B2"),    # currentFilters 丢 item_id ⇒ 输入框改了不发后端
        ("notice", "B6"),     # 提示条不再渲染 ⇒ 用户不知道在筛什么
        ("jump", "C1"),       # 跳转查询键名写错 ⇒ 跳过去等于没跳
    ],
)
def test_library_item_filter_guards_are_load_bearing(mutate, expected):
    """变异验证：每个守卫被改坏时脚本必须变红 —— 且**必须靠断言变红**。

    ⚠️ 断言两块都要：`returncode != 0` 单看是**不够的** —— 锚点失配时脚本也以
    rc=1 退出（打印 "FATAL 变异锚点未命中"），那说明变异根本没生效，不是承重。
    所以第二条必须要求**出现具体那条 `FAIL  Xn` 行**（DEF-058 的教训）。
    """
    result = _run_library_item_filter({"LIB_ITEM_MUTATE": mutate})
    assert result.returncode != 0, f"{mutate} 变异必须让脚本失败"
    assert "FATAL" not in result.stdout + result.stderr, (
        f"{mutate} 变异锚点失配（实现已改？）⇒ 这不是承重证明：\n{result.stdout}\n{result.stderr}"
    )
    assert f"FAIL  {expected}" in result.stdout, f"stdout={result.stdout}"



# ---------------------------------------------------------------------------
# 笔记类型 + 同步按钮 + 话题/商品解耦（设计 13 §25 / §27 / §28，2026-09-22）
# ---------------------------------------------------------------------------
def test_library_sync_button_shows_only_for_synced_notes():
    """「同步」按钮的可见性判定：**已发布过的一定有**（2026-09-23 起，§25.7.5）。

    用户 2026-09-23 口径：「已经发布过的笔记一定存在同步按钮功能」。

    ⚠️ 本条**取代**旧契约（原断言：
    `assert 'selectedNote.note_type === "synced"' in code` +
    `assert 'note.note_type === "synced"' in code`，即「同步按钮只对 synced 渲染」）。
    **原断言保留在此，不静默删除** —— 它漏掉的正是用户报的场景：本地生成笔记
    发布成功后仍是 `local_generated`（类型不变、只改 `publish_status`），
    于是**发出去了却没有同步按钮**。真实库已复现（id 1966「新中式桌垫…」）。

    判定改走**唯一实现** `canSyncNote`（`lib/note-filters.ts`）：
    `synced` ∨ `publish_status == published`。

    现在的强度**不降反升** —— 旧写法只要求「两处各写一句类型比较」，
    现在四件事都要同时成立：
    ① 卡片与详情**两处**都用 `canSyncNote`（不是各写一份判断）；
    ② 页面里**不得**再出现就地类型比较（那正是旧实现的形态）；
    ③ 判定本身认 `published`（行为脚本 F 段 + 三个变异开关钉住）；
    ④ **一个实现、两个调用点**（旧断言的后半段，仍然有效：定义 + 详情 + 卡片 = 3 处）。
    """
    code = _code_only(_read(LIBRARY))
    # ① 两处都走同一判定
    assert "canSyncNote(note)" in code, "笔记卡片未用 canSyncNote 判定同步按钮"
    assert "canSyncNote(selectedNote)" in code, "详情卡未用 canSyncNote 判定同步按钮"
    # ② 不得有就地判断（旧契约的形态 = 第二份实现）。
    #    断言范围是**同步按钮的两个区块**：详情弹窗里 `selectedNote.note_type === "synced"`
    #    还用于选图标与颜色（呈现，不是判定）—— 那是另一件事，不禁。
    card_sync = _slice_between(code, "{canSyncNote(note) && (", "</Button>")
    detail_sync = _slice_between(code, "{canSyncNote(selectedNote) && (", "</Button>")
    assert 'note.note_type === "synced"' not in card_sync, (
        "卡片同步按钮出现就地类型判断 —— 应统一走 canSyncNote"
    )
    assert 'selectedNote.note_type === "synced"' not in detail_sync, (
        "详情同步按钮出现就地类型判断 —— 应统一走 canSyncNote"
    )
    # ③ 判定本身认「已发布」
    lib_source = _code_only((ROOT / "frontend/src/lib/note-filters.ts").read_text(encoding="utf-8"))
    # 下界取函数之后的第一个 `export`（理由见 `…_exists_for_published_notes`：
    # `}` / `return true;` / 被断言串本身都不能当边界）
    body = _slice_between(lib_source, "export function canSyncNote(", "\nexport ")
    assert "publish_status" in body and '"published"' in body, (
        "canSyncNote 未认 published —— 已发布的本地生成笔记会漏按钮（本轮修的正是它）"
    )
    # ④ 一个实现、两个调用点（旧断言保留：定义 + 详情 + 卡片 = 3 处 syncNote 调用）
    assert code.count("syncNote(") >= 3, "卡片与详情必须共用同一个 syncNote 实现"


def test_library_publish_is_async_with_progress_card():
    """发布异步 + 进度卡接线（2026-09-25，设计 13 §31.5 异步化）。

    用户口径：「前端点击发布无需等待，后端异步发布」+「内容库页新增发布进度卡」。
    源码断言守四件事（少任何一件，用户侧就退回「干等」或「进度看不见」）：

    ① 点击后**不起全链等待**：`publishSavedNote` 的返回值只用于置 publishing +
       起轮询（`trackPublishNotes`），页面源码里**不许**出现「await 发布结果后再
       摆终态文案」的旧形态；
    ② 进度卡数据源是 `GET /notes/publish-queue`（`fetchPublishQueue`），且轮询
       间隔是**一个常量**（原 `PUBLISH_POLL_INTERVAL_MS`，2026-09-25 与同步卡统一
       成 `QUEUE_POLL_INTERVAL_MS = 5000` —— 用户口径「数据 5 秒更新一次」），
       不是散落的魔法数；
    ③ 轮询随页面卸载停（`useEffect` cleanup 里 `clearInterval`）—— 否则切页面
       后定时器继续打接口；
    ④ 后端路由 `publish-queue` 注册在 `/{note_id}` **之前**（否则被 int 转换
       吃掉变 422，进度卡永远取不到数）；
    ⑤ 进度卡是**统计卡行里的第 5 张小卡**（2026-09-25 用户口径「发布进度卡做成
       小卡，与已发、待发布、发布失败、等卡片一行样式一致，并去掉相同的统计数据」）：
       位置在筛选区之前；`counts.published/failed/pending` 三个**重复计数不再渲染**
       （统计行前三张已是同一份数）。
    ⑥ 2026-09-25 用户口径「发布、同步进度卡做类似的调整」+「去掉 tip」⇒ 卡片从
       「发布中(0/1) + 排队(N) + Tooltip」改成**三数**（成功 / 失败 / 待发布），
       与同步卡同形。
       ⚠️ **被取代的原句**（保留，不静默删除）：
         「快照 `published`/`failed`/`total` 是**本轮队列记账**
           （`note_publish_service._publish_results`），不是全库四态 `counts`。」
       —— §35.9 队列持久化后「本轮」由 `tasks` 表的**批次行**承担（内存
       `_publish_results` 已整段删除），`publish_queue_snapshot` 委托底座
       `note_queue_service.snapshot` 并把通用名 `synced` 改名成 `published`
       （改名契约由 `test_publish_worker_watchdog.py` 的键名守卫钉死）。
       ⚠️ **被取代的原句**（保留，不静默删除）：
         「assert "publishQueueTooltip" in code, "「正在发布哪篇 + 后面排几篇」
           收进 Tooltip，别整条删"」
       ⚠️ 2026-09-26 常驻化那版的**「本页累计」也被取代**（保留，不静默删除）：
         「assert "publishRound" in code, "本轮成败数必须本页累计"
           / assert "setPublishRound(" in code, ...」
       —— 2026-09-26 当日口径（用户口径「只统计当日发布或同步的数据，在卡片右下角
       小字展示仅当日数据，**统计队列记录表中的数据**」）：三数改读快照 `today`
       （后端 `today_item_counts` 按当日 `tasks` 行现算），页面不再自己累计
       —— 累计刷新页面就归零、还看不见别的入口入队的项。
    ⑦ **六张卡同尺寸**（2026-09-26 用户口径「另外两个进度卡与其它卡片大小不一致，
       应与其它卡片保持一致」）：`flex="1 1 0%"` 只保证**等宽**，高度得靠统一的
       内容盒常量（`STAT_CARD_CONTENT` / `STAT_CARD_PROGRESS_CONTENT`）—— 否则
       进度卡多一行「仅当日数据」小字就比同行普通卡高一截。
    ⑧ **当日小字改走主题令牌**（2026-09-26 用户口径「仅当日数据黑色字体看不清」）：
       小字从 `<div style={…}>`（样式里写死 `rgba(0, 0, 0, 0.45)`）改成
       `<Text style={{ …, color: token.colorTextSecondary }}>`（`theme.useToken()`
       取令牌，亮 `rgba(0,0,0,.65)` / 暗 `rgba(255,255,255,.65)`）。
       ⚠️ 别退回 `<Text type="secondary">`：它取 `colorTextTertiary`（0.45），
       暗色下 12px 只有 ≈4.5:1、亮色下与写死的 0.45 黑同色 —— 都还是「看不清」。
       根因：本应用**默认暗色**
       （`app/providers.tsx` `saved === "light" ? "light" : "dark"`，卡片底 `#1a1a1a`）
       ⇒ 写死黑字叠近黑底 ≈1.2:1，看不见；亮色下 0.45 黑（≈#8C8C8C）对 12px 也偏淡。
       ⚠️ **被取代的原句**（保留，不静默删除）：
         「assert "style={STAT_CARD_TODAY_NOTE}" in code, "当日小字未用右下角样式常量"」
       —— 断言本身仍在（常量仍是唯一写样式的入口），但**常量里不许再有 `color`**，
       且渲染必须带 `type="secondary"`；这两条由本 test ⑦ 段的两行断言钉死。
    """
    code = _code_only(_read(LIBRARY))
    # ① 点击即入队、不等链路
    assert "trackPublishNotes(" in code, "发布入队后未起轮询（用户看不到进度）"
    assert "await refreshPublishQueue()" in code, "入队后未立刻拉一次快照（排队位置算不出来）"
    # ② 进度卡数据源 + 单一间隔常量
    assert "fetchPublishQueue" in code, "进度卡未接 GET /notes/publish-queue"
    # ⚠️ 被取代：`assert code.count("PUBLISH_POLL_INTERVAL_MS") >= 2, ...`
    #    （常量名已与同步卡统一为 QUEUE_POLL_INTERVAL_MS）
    assert code.count("QUEUE_POLL_INTERVAL_MS") >= 2, "轮询间隔必须是一个常量（定义 + setInterval 各一次）"
    assert "setInterval" in code and "clearInterval" in code, "轮询没有起停手段"
    # ③ 卸载即停
    assert "publishPollRef.current !== null) window.clearInterval(publishPollRef.current)" in code, (
        "页面卸载未停发布轮询（定时器会跟着路由切换一直跑）"
    )
    # ④ 后端路由顺序（进度卡的生命线）
    notes_api = _read(ROOT / "backend/app/api/notes.py")
    queue_at = notes_api.index('@router.get("/publish-queue")')
    note_id_at = notes_api.index('@router.get("/{note_id}")')
    assert queue_at < note_id_at, (
        "publish-queue 必须注册在 /{note_id} 之前 —— 否则被 int 转换吃掉（422），进度卡取不到数"
    )
    assert '"publish-queue"' in notes_api or '"/publish-queue"' in notes_api
    # ⑤ 小卡并入统计行 + 去重（独立整行卡已撤）
    # ⚠️ 2026-09-26 用户口径「发布进度卡不要再显示『发布进度』小标题」⇒ 小标题已删，
    #    原断言 `assert "发布进度" in code` **被取代**（保留原文备查）：
    #    「进度卡标题没了（并入统计行后仍是『发布进度』小卡）」。
    assert "发布进度" not in code, "小标题已按 2026-09-26 用户口径撤掉，不许回来"
    for field in ("publishQueue.counts.published", "publishQueue.counts.failed", "publishQueue.counts.pending"):
        assert field not in code, f"{field} 与统计卡前三张重复，已按口径撤掉（同一语义不留两份呈现）"
    assert "publishQueue.queued.length" in code, "排队数是「待发布」的组成部分（统计行没有它），不能撤"
    # ⑥ 本轮三数（发布成功 / 失败 / 待发布）+ 去掉 tip（2026-09-25 用户口径）
    # ⚠️ 2026-09-25 用户口径「发布进度成功与同步进度成功优化为：发布成功和同步成功」
    # ⇒ 第一张小卡的 title 从「成功」改为「**发布成功**」（两张卡各说各的，不再共用一个
    # 看不出是哪条链路的「成功」）。原句保留：~~`title="成功"`~~ 已被取代。
    assert 'title="发布成功"' in code and 'title="失败"' in code and 'title="待发布"' in code, (
        "发布进度卡应是「发布成功 / 失败 / 待发布」三数（用户口径「发布、同步进度卡做类似的调整」）"
    )
    # ⚠️ 2026-09-26 常驻化（用户口径「不需要队列批次，队列是常驻」）：后端快照不再有
    #    published / failed 计数（只回队列实时态）⇒ 三数**曾**改由本页按自己入队的篇
    #    累计。原句 `assert "publishQueue.published" in code` **被取代**（快照没这个
    #    键了，读了也是 undefined）。
    # ⚠️ 2026-09-26 **当日口径再改一次**（用户口径「只统计当日发布或同步的数据，在卡片
    #    右下角小字展示仅当日数据，**统计队列记录表中的数据**」）：三数改读快照 `today`
    #    （后端 `today_item_counts` 按当日 `tasks` 行现算）。上一条「本页累计」的原句
    #    （`assert "publishRound" in code` / `assert "setPublishRound(" in code`）
    #    **同样被取代**，保留备查 —— 累计刷新页面就归零、还看不见别的入口入队的项。
    assert "publishRound" not in code and "setPublishRound(" not in code, (
        "本页累计已按 2026-09-26 当日口径撤掉（三数走快照 today，见 docstring 被取代原句）"
    )
    assert "publishQueue.today" in code and "publishToday" in code, (
        "发布进度卡的卡面三数未接快照 today（当日队列记录；`?? EMPTY_QUEUE_TODAY_COUNTS` "
        "是形状兜底，别把整页取崩）"
    )
    assert "publishToday.ok" in code and "publishToday.failed" in code, (
        "发布成功 / 失败未读当日窗口"
    )
    assert "publishToday.pending" in code, "「待发布」未读当日窗口（快照 today）"
    assert "publishQueuePending" in code, "轮询停止条件仍要实时待处理数（running + queued）"
    # ⑦ 当日口径小字 + **六张卡同尺寸**（2026-09-26 用户口径「在卡片右下角小字展示仅
    #    当日数据」+「另外两个进度卡与其它卡片大小不一致，应与其它卡片保持一致」）
    assert '"仅当日数据"' in code, "进度卡右下角缺「仅当日数据」小字（用户口径原文）"
    # ⚠️ 2026-09-26 用户口径「仅当日数据黑色字体看不清」：小字**必须走 antd 主题令牌**
    #    （`<Text type="secondary">` → `colorTextSecondary`，亮/暗两套主题各自成立）。
    #    根因是样式里写死了 `rgba(0, 0, 0, 0.45)`，而本应用**默认是暗色主题**
    #    （`app/providers.tsx`：`saved === "light" ? "light" : "dark"`，卡片底 `#1a1a1a`）
    #    ⇒ 黑字叠近黑底，对比度约 1.2:1，基本看不见。
    #    下面第一条守「颜色不在样式里写死」（任何颜色都不行），第二条守「确实接上令牌」。
    assert code.count("style={{ ...STAT_CARD_TODAY_NOTE, color: token.colorTextSecondary }}") == 2, (
        "两张卡的当日小字都要接「右下角样式常量 + 主题令牌色」"
    )
    _today_note_style = code.split("const STAT_CARD_TODAY_NOTE: CSSProperties = {", 1)[1].split("};", 1)[0]
    assert "color" not in _today_note_style, (
        "当日小字样式里写死了颜色 —— 本应用默认暗色主题，黑字叠近黑底看不清"
        "（2026-09-26 用户已报过一次）；颜色一律交给 antd 主题令牌"
    )
    # 颜色必须**来自令牌**（`theme.useToken()` → `token.colorTextSecondary`），
    # 而不是 `type="secondary"`（那是 tertiary 0.45，亮色下与写死的 0.45 黑同色）。
    assert "theme.useToken()" in code, "本页未取主题令牌（小字的颜色只能来自 token）"
    assert code.count("style={STAT_CARD_CONTENT}") == 4, (
        "四张普通统计卡未接统一内容盒 —— 高度会与两张进度卡不一致（等宽管不了高度）"
    )
    assert code.count("style={STAT_CARD_PROGRESS_CONTENT}") == 2, (
        "两张进度卡未接统一内容盒（三数 + 右下角小字两行撑满同一高度）"
    )
    # ⚠️ 被取代：`assert "publishQueueTooltip" in code, "「正在发布哪篇 + 后面排几篇」收进 Tooltip，别整条删"`
    assert "publishQueueTooltip" not in code, "发布进度卡的 Tooltip 已按用户口径「去掉 tip」撤除"
    assert "Tooltip title={publishQueue" not in code, "发布进度卡不该再被 Tooltip 包裹"


def test_library_batch_sync_with_progress_card():
    """批量同步 + 同步进度卡接线（2026-09-25，设计 13 §35）。

    用户口径：「内容库新增一个同步进度卡，新增支持多选批量同步笔记，只有已发布的
    支持被选中，后端使用队列，按顺序同步，并统计数据到进度卡，已同步数/同步进度比/总数」。
    源码断言守七件事（少任何一件，用户侧就退回「批量不同步」或「进度看不见」）：

    ① 批量入口：批量操作区有「批量同步」按钮，二次确认 + 跳过明示
       （`splitSyncTargets` 的 `skipped` 必须进确认框 —— 静默丢 = 用户以为都同步了）；
    ② 门禁只有一把尺子：`splitSyncTargets` / `canBatchSyncNote`（`note-filters.ts`），
       页面里**不许**再出现就地判断 `publish_status === "published"`（第二份实现）；
    ③ 后端同口径：`start_batch_sync` 过模型层唯一判据 `note_supports_batch_sync`；
    ④ 进度卡数据源是 `GET /notes/sync-queue`（`fetchSyncQueue`），轮询间隔是**一个常量**
       （原 `SYNC_POLL_INTERVAL_MS`，2026-09-25 与发布卡统一成
       `QUEUE_POLL_INTERVAL_MS = 5000` —— 用户口径「数据 5 秒更新一次」）；
    ⑤ 轮询起停与卸载清理齐备（`trackSyncNotes` / `stopSyncPolling` / cleanup）；
    ⑥ 后端路由 `sync-queue` 注册在 `/{note_id}` **之前**（否则 422，进度卡取不到数）；
    ⑦ 进度卡是统计卡行里的**第 6 张小卡**，2026-09-25 用户口径「同步进度统计调整为，
       成功同步数 / 失败同步数 / 待同步数」+「去掉 tip」⇒ 卡上是**三数**，Tooltip
       与百分比**都已撤**。
       ⚠️ 2026-09-26 当日口径（用户口径「同步进度卡……**只统计当日**同步的数据，在卡片
       右下角小字展示仅当日数据，**统计队列记录表中的数据**」）：三数改读快照 `today`
       （后端按当日 `tasks` 行现算），**不再由本页按自己入队的篇累计**。
       ⚠️ **被取代的原句**（保留，不静默删除）：
         「assert "syncRound" in code and "setSyncRound(" in code,
           "本轮成败数必须本页累计（常驻队列没有批次记账）」」
    ⑧ 轮询纪律（同轮用户口径「数据 5 秒更新一次」+「待同步数为 0 时，不再刷新统计
       数据」）：间隔是 `QUEUE_POLL_INTERVAL_MS = 5000`；停止条件是**待处理数为 0**
       （`snapshot.total > 0 && pending === 0`），并带「本轮无任务」连续空转兜底
       （`QUEUE_IDLE_ROUNDS_LIMIT`）—— 否则入队与快照之间有时差时会立刻停、
       数字再也不刷新（用户报的「同步进度未更新」）。
       ⚠️ **被取代的原句**（保留，不静默删除）：
         「assert "syncQueue.synced" in code and "/ ${syncQueue.total}" in code,
           "已同步数 / 总数后缀缺失"」
         「assert "（${syncPercent}%）" in code, "百分比应收进 Tooltip"」
         「assert "title={syncQueueTooltip || undefined}" in code, "空闲（本轮无任务）
           时不应再有 Tooltip 文案"」
         「assert "syncQueueTooltip" in code, "「正在同步哪篇 + 失败数」应收进 Tooltip"」
    """
    code = _code_only(_read(LIBRARY))
    filters = _code_only(_read(ROOT / "frontend/src/lib/note-filters.ts"))
    sync_service = _read(ROOT / "backend/app/services/note_sync_service.py")
    notes_api = _read(ROOT / "backend/app/api/notes.py")

    # ① 批量入口 + 确认框明示跳过
    assert "batchSyncSelected" in code and "batchSyncNotes(" in code, "批量同步入口缺失"
    assert "splitSyncTargets(notes, selectedNoteIds)" in code, "批量同步未按选中集合拆分目标/跳过"
    assert "skipped > 0" in code and "将跳过" in code, "确认框未明示跳过篇数（静默丢 = 用户以为都同步了）"
    # ② 门禁单一实现（批量同步的目标拆分只走 splitSyncTargets；尺子在 note-filters.ts）
    batch_fn = code.split("function batchSyncSelected()", 1)[1].split("async function batchSyncNotes", 1)[0]
    assert "publish_status" not in batch_fn, "batchSyncSelected 里又写了一份门禁判断（应走 splitSyncTargets）"
    assert "export function canBatchSyncNote" in filters and "export function splitSyncTargets" in filters
    # ③ 后端同口径（模型层唯一判据）
    assert "note_supports_batch_sync" in sync_service, "后端批量同步未走模型层唯一判据"
    # ④ 进度卡数据源 + 单一间隔常量
    assert "fetchSyncQueue" in code, "进度卡未接 GET /notes/sync-queue"
    # ⚠️ 被取代：`assert code.count("SYNC_POLL_INTERVAL_MS") >= 2, ...`
    #    （常量名已与发布卡统一为 QUEUE_POLL_INTERVAL_MS）
    assert code.count("QUEUE_POLL_INTERVAL_MS") >= 2, "轮询间隔必须是一个常量（定义 + setInterval 各一次）"
    # ⑤ 轮询起停 + 卸载清理
    assert "trackSyncNotes(" in code and "stopSyncPolling" in code, "同步轮询没有起停手段"
    assert "syncPollRef.current !== null) window.clearInterval(syncPollRef.current)" in code, (
        "页面卸载未停同步轮询（定时器会跟着路由切换一直跑）"
    )
    # ⑥ 后端路由顺序（进度卡的生命线）
    queue_at = notes_api.index('@router.get("/sync-queue")')
    note_id_at = notes_api.index('@router.get("/{note_id}")')
    assert queue_at < note_id_at, (
        "sync-queue 必须注册在 /{note_id} 之前 —— 否则被 int 转换吃掉（422），进度卡取不到数"
    )
    # ⑦ 第 6 张小卡：**当日三数**（成功同步数 / 失败同步数 / 待同步数）
    # ⚠️ 2026-09-26 用户口径「同步进度卡不要再显示『同步进度』小标题」⇒ 小标题已删，
    #    原断言 `assert "同步进度" in code` **被取代**（保留原文备查）：
    #    「统计卡行里没有『同步进度』小卡」。
    assert "同步进度" not in code, "小标题已按 2026-09-26 用户口径撤掉，不许回来"
    # ⚠️ 2026-09-26 常驻化（用户口径「不需要队列批次，队列是常驻」）：快照曾不再有
    #    synced / failed 计数 ⇒ 三数**曾**改由本页按自己入队的篇累计（`syncRound`）。
    #    原句 `assert "syncQueue.synced" in code and "syncQueue.failed" in code`
    #    **被取代**（快照没这两个键了，读了也是 undefined）。
    # ⚠️ 2026-09-26 当日口径（用户口径「同步进度卡……只统计当日同步的数据，在卡片右下角
    #    小字展示仅当日数据，统计队列记录表中的数据」）⇒ 三数改读快照 `today`
    #    （后端按当日 `tasks` 行现算）。「本页累计」这一版**同样被取代**（见 docstring）。
    assert "syncRound" not in code and "setSyncRound(" not in code, (
        "本页累计已按 2026-09-26 当日口径撤掉（三数走快照 today）"
    )
    assert "syncQueue.today" in code and "syncToday" in code, (
        "同步进度卡的卡面三数未接快照 today（当日队列记录）"
    )
    assert "syncToday.ok" in code and "syncToday.failed" in code, "同步成功 / 失败未读当日窗口"
    assert "syncToday.pending" in code, "「待同步」未读当日窗口（快照 today）"
    assert "syncQueuePending" in code, "轮询停止条件仍要实时待处理数（running + queued）"
    # ⚠️ 2026-09-25 用户口径「发布进度成功与同步进度成功优化为：发布成功和同步成功」
    # ⇒ 第一张小卡 title 从「成功」改为「**同步成功**」。原句保留：~~`title="成功"`~~。
    assert 'title="同步成功"' in code and 'title="失败"' in code and 'title="待同步"' in code, (
        "同步进度卡应是「同步成功 / 失败 / 待同步」三数（用户口径原文 + 文案优化）"
    )
    assert "<Progress" not in code, "同步进度卡里的进度条已按用户口径移除"
    assert "暂无同步任务" not in code, "「暂无同步任务（批量同步进行时显示进度）」文本已按用户口径移除"
    # ⚠️ 被取代（原句见 docstring 括注）：Tooltip / 百分比断言 → 反向断言
    assert "syncQueueTooltip" not in code and "syncPercent" not in code, (
        "同步进度卡的 Tooltip 与百分比已按用户口径「去掉 tip」撤除"
    )
    assert "Tooltip title={syncQueue" not in code, "同步进度卡不该再被 Tooltip 包裹"
    assert "batchSyncSavedNotes" in code, "批量同步未接 POST /notes/batch-sync"
    # 「仅当日数据」小字（2026-09-26 用户口径；两张进度卡共用同一个文本常量与样式）
    assert code.count("style={{ ...STAT_CARD_TODAY_NOTE, color: token.colorTextSecondary }}") == 2, (
        "两张进度卡都要有右下角「仅当日数据」小字，且颜色走主题令牌（用户口径原文）"
    )
    assert code.count("color: token.colorTextSecondary") == 2, (
        "同步卡的当日小字也要用主题令牌（写死黑字在暗色主题下看不清，用户已报）"
    )

    # ⑧ 轮询纪律：5 秒一轮 + 待处理为 0 才停（2026-09-25 用户口径）
    assert "QUEUE_POLL_INTERVAL_MS = 5000" in code, "轮询间隔应为 5 秒（用户口径「数据 5 秒更新一次」）"
    assert "QUEUE_IDLE_ROUNDS_LIMIT" in code, "缺「本轮无任务」空转兜底（定时器会泄漏）"
    assert code.count("snapshot.total > 0 && pending === 0") >= 2, (
        "两张进度卡的停止条件都应是「待处理数为 0」（用户口径「待同步数为 0 时，"
        "不再刷新统计数据」）"
    )
    assert code.count("pending === 0") >= 2, "停止条件里必须有待处理数的判定"


def test_library_page_notices_use_message_and_no_longer_occupy_layout():
    """内容库页**所有**提示走消息组件，不再占页面布局（2026-09-25 用户口径）。

    用户口径：「内容库页……页面所有提示包括异常提示，正常提示，成功提示等均实现
    消息提示组件不要占用页面布局」。

    本轮把**页面级**的 inline 承载全撤（每一个都曾是占版面的一块）：
      * `batchFailure`（「批量操作失败」红块 —— 用户看到的那条「批量操作失败
        Method Not Allowed」正是长在这里）；
      * `error`（列表加载失败的 `<Alert>`）；
      * `topicFailure`（「话题保存失败」红块）；
      * `detailSyncFailure`（详情弹窗里的「同步失败」红块）；
      * 加载更多的失败红块（**只留「重试」按钮本身** —— 它是控件，不是提示条）。

    替换路径分两类（§3.1.1 `_silent` 规则）：
      * 非 `_silent` 的接口 ⇒ 全局拦截器弹一次 `message`，调用点**不再自己报**（防双弹）；
      * `_silent` 的 `syncSavedNote` / 以及「200 + 失败态」这两条不走拦截器异常分支 ⇒
        **必须**由调用点 `message.error` 报真原因，否则失败就彻底没声了。

    边界（**不随 message 化搬走**，设计 04 §3.6 表格第 2 行「静态说明」类）：
      * `publish_error` 的「发布失败原因」—— 落库的持久状态说明，重开弹窗还要看；
      * 详情弹窗内部的 `failure`（`renderFailure` 三分法）—— 「这块数据为什么是空的」
        的结构呈现，且是 my-notes / 内容库**共用**的唯一实现。
    """
    code = _code_only(_read(LIBRARY))
    # ① 旧承载全不在（反向断言必须先过 `_code_only` —— 注释里已写了这些名字）
    for marker in (
        "setBatchFailure(",
        "batchFailure",
        "setDetailSyncFailure(",
        "detailSyncFailure",
        "setTopicFailure(",
        "topicFailure",
    ):
        assert marker not in code, f"内容库页仍有 inline 红块承载 {marker}（应改走 antd message）"
    assert "const [error, setError]" not in code, "列表加载失败的 `error` state 应随 inline Alert 一起移除"
    assert "renderFailure(" not in code, "内容库页不应再直接渲染三分法红块（页面级提示已 message 化）"
    assert "<Alert" in code and 'message="发布失败原因"' in code, (
        "`publish_error` 是落库的持久状态说明（静态说明类），应保留 inline —— 别把它一起扫掉"
    )
    # ② 新路径真的在（否则「删干净」也能变绿 —— 这就是负向断言的经典陷阱）
    assert "message.error(`同步失败：${reason}`)" in code, (
        "`syncSavedNote` 是 `_silent`（门禁 400 走这里）⇒ 同步失败必须由调用点 message.error 报真原因"
    )
    # ⚠️ 2026-09-25 队列化取代：原断言 `'s.reason || "平台未给原因"' in code`（「200 + 失败态」
    # 就地报原因）随用户口径「单独点击同步按钮也需要放入同步队列中执行，与批量同步一致」
    # 失效 —— 单条端点不再返回逐项结果，失败原因只在快照 `finished` 里给一次，由轮询器
    # `pollSyncQueue` 报。原句保留：~~同上~~。兜底文案本身仍在（换到了轮询侧）。
    assert "平台未给原因" in code, "失败原因兜底（「平台未给原因」）必须仍在轮询结果里报一次"
    assert "syncSavedNote(note.id)" in code, (
        "单条同步必须走**入队端点**（`POST /notes/{id}/sync` 回执），不能退回同步阻塞执行"
    )
    assert "trackSyncNotes([note.id])" in code, (
        "单条入队后必须把这一篇交给**同一个轮询器**（与批量共用 pollSyncQueue / stopSyncPolling）"
        "—— 不 track 就没有任何路径去报它的终态（变异 track 咬的就是这条）"
    )
    # ⚠️ 别用「已加入同步队列」当锚点：批量路径的成功提示也是这句 ⇒ 单条被改坏仍绿。
    # 单条独有的是幂等分支那句「已在同步队列中」（批量没有这个分支）。
    assert "已在同步队列中" in code, (
        "幂等命中（`already_queued`）要单独提示「已在同步队列中」—— 它与批量的"
        "「已加入同步队列 N 篇」不是同一句，不许用共文案冒充（变异 enqueuehint 咬的就是这条）"
    )
    assert "content_updated" not in code and "images_added" not in code, (
        "单条同步的逐项结果（正文 / 图片计数）已随队列化移除 —— 前端不该再渲染它"
    )
    assert "message.`" in code or "message.error(" in code, "内容库页必须真有 message.* 调用"

# --------------------------------------------------------------------------- #
# 2026-09-25 §36 笔记工坊：图片 / 视频 / 草稿三坊合并（一页 Tabs + 菜单一项）
# --------------------------------------------------------------------------- #
ROUTER = ROOT / "frontend/src/app/router.tsx"
APP_SHELL = ROOT / "frontend/src/components/layout/app-shell.tsx"
NOTE_WORKSHOP = ROOT / "frontend/src/pages/platforms/xhs/note-workshop-page.tsx"
WORKSHOP_PANELS = (
    "frontend/src/pages/platforms/xhs/image-studio-panel.tsx",
    "frontend/src/pages/platforms/xhs/video-studio-panel.tsx",
    "frontend/src/pages/platforms/xhs/drafts-workshop-panel.tsx",
)


def test_three_workshops_merge_into_one_page_with_tabs():
    """三坊合并的结构守卫（设计 13 §36）：一页三项 —— 路由、菜单、面板。

    用户口径：「将图片坊，视频坊，草稿坊，合并为一个页面视图tab自然隔离，新增菜单
    名称为笔记工坊」。守五件事（少任何一件就是「合并了但入口死了」或「没隔离」）：

    ① 新路由渲染工坊页；三个旧路由是 `Navigate` 重定向到对应 tab
       （深链 / 书签 / 外部链接不死）；
    ② 菜单只剩「笔记工坊」一项，三个旧菜单项**整项**移除（含 key 与标签）；
    ③ 工坊页 = `Tabs` 三页签（images / videos / drafts）+ 三个面板组件
       （tab 即隔离：面板各自持状态，不是三套状态挤一个组件）；
    ④ 面板不再各扛 `PageHeader`（工坊页只保留一个骨架标题）；
    ⑤ 内容库「加入草稿工坊」跳转改指规范 URL `?tab=drafts`。
    """
    router = _code_only(_read(ROUTER))
    shell = _code_only(_read(APP_SHELL))
    page = _code_only(_read(NOTE_WORKSHOP))

    # ① 新路由 + 旧路由重定向
    assert 'path="/platforms/xhs/note-workshop"' in router and "XhsNoteWorkshopPage" in router
    for old, tab in (
        ("/platforms/xhs/image-studio", "images"),
        ("/platforms/xhs/video-studio", "videos"),
        ("/platforms/xhs/drafts", "drafts"),
    ):
        assert f'path="{old}"' in router, f"旧路由 {old} 应保留为重定向（深链不死）"
        assert f'to="/platforms/xhs/note-workshop?tab={tab}"' in router, f"{old} 应重定向到 tab={tab}"
    assert "<Navigate" in router, "旧路由应走 Navigate 重定向"

    # ② 菜单一项
    assert 'key: "/platforms/xhs/note-workshop"' in shell and "笔记工坊" in shell
    for label in ("图片工坊", "视频工坊", "草稿工坊"):
        assert label not in shell, f"菜单里不应再有「{label}」独立项（已合并进笔记工坊）"
    for key in ('"/platforms/xhs/image-studio"', '"/platforms/xhs/video-studio"', '"/platforms/xhs/drafts"'):
        assert key not in shell, f"菜单旧 key {key} 未移除"

    # ③ Tabs 页签 + 面板（2026-10-02 T9 追加 materials / prompts 两个页签）
    for key in ('key: "images"', 'key: "videos"', 'key: "drafts"'):
        assert key in page, f"工坊页缺少页签 {key}"
    for panel in ("ImageStudioPanel", "VideoStudioPanel", "DraftsWorkshopPanel"):
        assert panel in page, f"工坊页未挂面板 {panel}"
    for relative in WORKSHOP_PANELS:
        assert (ROOT / relative).exists(), f"面板文件缺失：{relative}"
    # tab 状态进 URL query（可分享 / 刷新保持；未知值回落 images 不白屏）
    assert 'searchParams.get("tab")' in page, "tab 初值未从 URL query 读（刷新/分享丢页签）"
    assert 'next.set("tab", key)' in page, "切 tab 未写回 URL query"
    assert "normalizeTab" in page, "未知 tab 值缺归一化（回落 images，不白屏）"

    # ④ 面板不再各扛 PageHeader
    for relative in WORKSHOP_PANELS:
        source = _code_only(_read(ROOT / relative))
        assert "PageHeader" not in source, f"{relative} 仍在使用 PageHeader（骨架标题应收进工坊页）"

    # ⑤ 内容库跳转改指规范 URL
    library = _code_only(_read(LIBRARY))
    assert "/platforms/xhs/note-workshop?tab=drafts" in library, (
        "内容库「加入草稿工坊」应跳规范 URL（旧路径只剩重定向兜底）"
    )


# --------------------------------------------------------------------------- #
# 2026-10-02 T9：素材库面板 + 提示词模板面板（设计 17 §7.2 / §7.3）
# --------------------------------------------------------------------------- #
MATERIAL_PANEL = ROOT / "frontend/src/pages/platforms/xhs/material-library-panel.tsx"


def test_material_files_have_delete_upload_and_switchable_preview():
    """素材文件区：**上传 / 删除 / 批量删除 / 大图预览 + 左右切换**（v2.31）。

    用户口径原文：「素材文件区域添加，删除，批量删除，大图预览，且支持左右切换，
    注意删除会真实删除本地文本，添加提示二次校验」

    ## 判据必须是「**绑定关系**」而不是「关键词存在」

    ⚠️ 第一版守卫只 `assert "modal.confirm(" in panel` 这类**存在性**判据，
    变异实测 **3/4 无效**（把确认框改成 `if (false) modal.confirm(`、
    把「移入回收站」删掉、把 `onOpenPreview` 改成空函数 —— **全都照样绿**）。
    存在性判据守不住任何东西：它只证明「代码里出现过这几个字」。

    ⇒ 改成断言**谁绑在谁身上**：
    * 删除按钮的 onClick 必须真的调到 `handleDeleteFile`；
    * `handleDeleteFile` 里必须有 `modal.confirm(`（不是别处有）；
    * 确认文案必须在 `handleDeleteFile` 的**作用域内**；
    * 点图的 `onOpenPreview` 必须真的 `setPreviewIndex(index)`。
    """
    panel = _code_only(_read(MATERIAL_PANEL))

    # ① 上传：按钮必须绑「已选文件夹」+ 真的触发文件选择
    assert "uploadMaterialFile" in panel, "没有接上传请求"
    upload_btn = _slice_between(panel, "loading={uploading}", "上传")
    assert "disabled={activeFolderId === null}" in upload_btn, (
        "上传按钮没有绑定「必须先选文件夹」（没落点会写到不该写的地方）"
    )
    assert "uploadInputRef.current?.click()" in upload_btn, "上传按钮没真的打开文件选择器"

    # ② 单张删除：**必须绑到删除处理函数**，且该函数内**必须有确认框**
    assert "deleteMaterialFile" in panel, "没有接单张删除请求"
    assert "handleDeleteFile(file)" in panel, (
        "删除图标没有绑到 handleDeleteFile（变异实测：绑成空函数时旧守卫照样绿）"
    )
    # v2.31b：删除图标**叠在图片右上角**（用户口径「删除放图片右上角」）
    delete_mark = panel[panel.index("handleDeleteFile(file)"):]
    delete_block = delete_mark[:900]
    assert "position: \"absolute\"" in delete_block and "top: 4" in delete_block and "right: 4" in delete_block, (
        "删除图标没有定位到图片右上角（用户口径）"
    )
    # v2.31b：**左上角复选框**（用户口径「复选框勾选」）——
    # ⚠️ 上一版是「点卡片即选中」（隐式），用户明确要显式勾选
    assert "<Checkbox" in panel, "没有复选框（用户口径明确要「复选框勾选」）"
    assert "checked={selected}" in panel and "toggleSelect(file.id)" in panel, (
        "复选框没绑到勾选状态（画了框但点不动 = 假复选框）"
    )
    # ⚠️ 勾选**只**由复选框负责：卡片上不许再挂「点卡片即勾选」。
    #    变异实测 —— 卡片仍可点选时，把复选框 onChange 改成空函数**照样绿**（假复选框）。
    assert "onClick={() => toggleSelect(file.id)}" not in panel, (
        "卡片仍在承担勾选 —— 会让「点图看大图」误触选中（用户要显式复选框正是为消除误选）"
    )
    # v2.31b：**全选 / 取消全选**（加了复选框就必须有，否则逐个勾几十张是体力活）
    assert "取消全选" in panel and '"全选"' in panel, "工具栏没有全选/取消全选"
    delete_fn = _slice_between(panel, "function handleDeleteFile(", "function handleDeleteSelected(")
    # ⚠️ 判据必须**匹配语句本身**（行首 `modal.confirm(`），不能只 `in` 一下：
    #    变异实测 —— 改成 `if (false) modal.confirm(` 时旧的 `in` 判据**照样绿**
    #    （字面上仍包含 `modal.confirm(`）⇒ 等于确认框被摘掉却没有守卫。
    assert re.search(r"^\s*modal\.confirm\(({)?$", delete_fn, re.M), (
        "handleDeleteFile 里没有**真正被调用**的确认框（`if (false) modal.confirm(` 必须能红）"
    )
    # 文案必须在**本确认框的 content 块内**。
    # ⚠️ 不能取「从 modal.confirm 往后再 1200 字符」那种窗口：实测会**跨到批量删除**
    #    的确认框（那里也有「移入回收站」）⇒ 删掉单张这句时照样绿（无效变异）。
    #    正确做法 = 切到 `content: (` 与 `okText:` 之间（就是本框正文）。
    single_content = _slice_between(delete_fn, "content: (", "okText:")
    assert "移入回收站" in single_content, (
        "单张删除的确认文案没说清「移入回收站」—— 用户无法判断能否捞回（口径要「删得可逆」）"
    )
    assert "deleteMaterialFile(file.id)" in delete_fn, "确认框里没真的调删除"

    # ③ 批量删除：同样绑处理函数 + 处理函数内有确认框 + 无勾选禁用
    assert "deleteMaterialFilesBatch" in panel, "没有接批量删除请求"
    assert "disabled={selectedIds.length === 0}" in panel, "批量删除没做「无勾选时禁用」"
    assert "onClick={handleDeleteSelected}" in panel, "批量删除按钮没绑到处理函数"
    # v2.31c（用户口径「批量删除放素材文件区域内，不要放顶部文件夹，可能存在多个文件夹」）：
    # **勾选相关操作必须在「素材文件」卡内**，且**不许**出现在「素材文件夹」卡里。
    # ⚠️ 判据是**作用域**：勾选作用于当前文件夹的**文件**，而文件夹卡管的是**文件夹本身**
    #    （且可能有多个）—— 挂在文件夹卡上会让作用域错位。
    file_card = _slice_between(panel, "title={`素材文件", "已全部加载")
    folder_card = _slice_between(panel, 'title="素材文件夹"', "foldersLoading ? (")
    for label in ("已选 {selectedIds.length} 个文件", "批量删除", "全选"):
        assert label in file_card, f"「{label}」不在素材文件卡内（用户口径：放素材文件区域）"
        assert label not in folder_card, (
            f"「{label}」仍在素材文件夹卡里（用户口径：不要放顶部文件夹）"
        )
    batch_fn = _slice_between(panel, "function handleDeleteSelected(", "async function handleUpload(")
    assert re.search(r"^\s*modal\.confirm\(({)?$", batch_fn, re.M), (
        "批量删除没有**真正被调用**的确认框"
    )

    # ④ 预览左右切换：**结构判据**（弹窗在父级 + 按索引）
    assert "files[previewIndex]" in panel, (
        "预览没读父级 files —— 弹窗还在子组件里，结构上无法左右切换"
    )
    assert "stepPreview(" in panel, "没有左右切换的实现"
    assert "ArrowLeft" in panel and "ArrowRight" in panel, "键盘左右键没接"
    assert "onOpenPreview={() => setPreviewIndex(index)}" in panel, (
        "点图没真的设置 previewIndex（变异实测：改成空函数时旧守卫照样绿）"
    )
    # 越界不循环：`stepPreview` 里必须有边界 return
    step_fn = _slice_between(panel, "function stepPreview(", "return (")
    assert "next < 0 || next >= files.length" in step_fn, (
        "左右切换没有边界判定（越界会翻到 undefined / 静默循环）"
    )


PROMPT_PANEL = ROOT / "frontend/src/pages/platforms/xhs/prompt-template-panel.tsx"
LLM_GEN_MODAL = ROOT / "frontend/src/pages/platforms/xhs/llm-generate-modal.tsx"
LLM_GEN_CARD = ROOT / "frontend/src/pages/platforms/xhs/llm-generate-progress-card.tsx"


def test_t9_panels_are_wired_into_workshop():
    """T9 面板接线（设计 17 §7.2 / §7.3 / §7.4）。

    守四件：① 两个新页签挂在工坊页上（不是建了文件没人引 —— 那就是死代码）；
    ② 素材库面板的**只读边界**：删除文案必须讲明「不删磁盘文件」；
    ③ 生成进度卡**轮询摘除禁写 finally**（H9 铁律：写 finally 会在取数失败时停掉轮询，
       队列永远停在那一刻）；④ 生成配置弹窗的**媒体来源四选一**齐全。
    """
    page = _code_only(_read(NOTE_WORKSHOP))
    for key in ('key: "materials"', 'key: "prompts"'):
        assert key in page, f"工坊页缺少 T9 页签 {key}"
    for panel in ("MaterialLibraryPanel", "PromptTemplatePanel"):
        assert panel in page, f"工坊页未挂 T9 面板 {panel}"
    for path in (MATERIAL_PANEL, PROMPT_PANEL, LLM_GEN_MODAL, LLM_GEN_CARD):
        assert path.exists(), f"T9 文件缺失：{path.name}"

    # ② 素材库：删除必须讲明不删磁盘文件（只读边界，硬约束 ②）
    material = _code_only(_read(MATERIAL_PANEL))
    assert "磁盘" in material and ("未删除" in material or "不会被删除" in material), (
        "素材库删除入口必须讲明「不删磁盘原文件」（只读边界）"
    )
    assert "materialFileUrl" in material, "素材缩略图未走 materialFileUrl（内容端点）"

    # ③ 进度卡：轮询摘除禁写 finally
    card = _code_only(_read(LLM_GEN_CARD))
    assert "finally" not in card, "进度卡轮询摘除写了 finally（H9 铁律：会让队列停在那一刻）"
    assert "generated" in card, "进度卡未消费 `generated` 键（底座通用形状是 synced）"

    # ④ 生成配置弹窗：媒体来源齐全（图片四选一 + 视频两选一，§13.4.4 v2.14）
    modal = _code_only(_read(LLM_GEN_MODAL))
    for source in (
        "product_image",
        "material",
        "reference_image",
        "ai_generate",
        "video_ai_generate",
        "video_reference",
    ):
        assert source in modal, f"生成配置弹窗缺少媒体来源 {source}"
    assert "VIDEO_SOURCES" in modal, "弹窗未按视频来源分流参数区（视频没界面入口）"


# --------------------------------------------------------------------------- #
# 2026-10-03 v2.16：「生成AI商品图」弹窗左右两栏布局
# --------------------------------------------------------------------------- #
SHOP_ITEMS_PAGE = ROOT / "frontend/src/pages/platforms/xhs/shop-items-page.tsx"


def test_detail_image_modal_uses_tabs_for_groups():
    """生成图弹窗 = **主图 / 详情图两个 Tab**（设计 17 §13.3.7.9 v2.22，用户口径
    「弹窗中将生成主图和详情图完全拆分开，使用 tab，各自的提示词模板，补充要求，生成张数」）。

    ⚠️ v2.22 **取代** v2.16 的「左右两栏」布局：两栏解决的是「结果被压到底部」，
    但主图/详情图的**配置**仍是共用的（一份提示词、一个张数）—— Tab 才是「完全拆开」。

    守五件：① 75vw 宽度保留；② 用 `Tabs` 且 items 由 `detailImageGroupTabs` 驱动；
    ③ 每组有**自己的**提示词输入与张数输入；④ 每组有自己的一块结果；
    ⑤ 每组的状态**按组存**（`detailImageGroupConfigs`），不是共用一份。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    block = _slice_between(page, "open={detailImageOpen}", "</Modal>")
    assert 'width="75vw"' in block, "弹窗宽度不是「全屏 75%」（用户口径）"
    assert "<Tabs" in block, "未使用 Tab 拆分"
    assert "detailImageGroupTabs" in block, "Tab 未由组清单驱动（会写死两份）"
    # 每组各自的提示词与张数
    assert "detailImageGroupConfigs[tab.key]?.prompt" in block, "提示词未按组存"
    assert "detailImageGroupConfigs[tab.key]?.count" in block, "张数未按组存"
    # 每组各自的结果块
    assert "DetailImageGroupResult" in block, "每组没有自己的结果块"


def test_detail_image_modal_has_count_selector():
    """v2.18/v2.20/v2.28.4：弹窗要能选**生成张数**，范围 **1 ~ 10**。

    ⚠️ 下限 **3 → 1**（用户口径 2026-10-03「生成张数调整最低一张」）：
    原下限 3 替用户做了「总要成组出图」的决定，但「先出一张看效果」
    （调试期 / 额度紧张时）也应当允许；默认值仍 3。

    守四件：① 有「生成张数」标签；② 输入控件的上下界就是 1 / 10；③ 绑 state 且可改；
    ④ 张数确实传给了请求函数（不传 = 界面能选但后端永远收默认值，最典型的假接线）。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    block = _slice_between(page, "open={detailImageOpen}", "</Modal>")
    assert "生成张数" in block, "弹窗缺少「生成张数」条件"
    assert "min={1}" in block and "max={10}" in block, "张数范围不是 1~10"
    # v2.22：张数**按组**存（每组自己的张数），不再是一个全局 state
    assert "detailImageGroupConfigs[tab.key]?.count" in block, "张数未按组绑定"
    assert "setDetailImageGroupConfigs" in block, "张数不可改"
    # ④ 每组配置真正传进请求函数（假接线守卫：能填 ≠ 生效）
    generate_fn = _slice_between(page, "async function generateDetailImage", "function ")
    assert "groupSettings" in generate_fn, "按组配置未传给请求函数"


def test_detail_image_reference_groups_are_separate_blocks():
    """v2.20：**主图 / 详情图分为两大块**，不混搭（用户口径「不要混搭在一起」）。

    守三件：① 两组各自成块（`ReferenceImagePicker` 内部是带边框的卡片容器）；
    ② 每组有**本组**全选 / 清空（且只影响本组 —— 清空按本组 url 集合过滤，不整清）；
    ③ 组名与计数在组头（`共 N 张 · 已选 M`），而不是两行紧挨着靠小字区分。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    picker = _slice_between(page, "function ReferenceImagePicker", "QUEUE_POLL_INTERVAL_MS")
    # ① 卡片容器（边框 + 圆角）
    assert 'border: "1px solid' in picker and "borderRadius: 6" in picker, (
        "两组未各自包成独立卡片（会看起来像同一份图的分段）"
    )
    # ② 组头：组名 + 本组计数 + 本组全选/清空
    assert "共 {urls.length} 张" in picker, "组头缺少本组总数"
    assert "已选 {pickedInGroup}" in picker, "组头缺少本组已选数（会让人误读成全局计数）"
    # v2.23：**取消「全选」**（用户口径）—— 只保留「清空」
    assert "onClearGroup" in picker, "缺少「清空」"
    assert "onSelectAll" not in picker, "「全选」应已移除（v2.23 用户口径）"
    assert "reachedLimit" in picker, "达上限后未做置灰处理"
    # ⚠️ v2.23：主图与详情图**合并为一组**后，不再有「按本组过滤」的问题
    # （旧断言 `group.has(u)` 是两卡片时代的产物，随合并一并退场）。
    # ③ 两个 picker 调用点都传了本组回调（v2.23：每组 Tab 里都列**全部**两组图）
    block = _slice_between(page, "open={detailImageOpen}", "</Modal>")
    # v2.23：合并成一组 ⇒ 一个 picker、一个清空；且不再有「全选」
    assert block.count('label="可选参考图"') == 1, "参考图应只有一组（合并后）"
    assert "onSelectAll" not in block, "「全选」应已移除"
    # 参考图列出**该商品下的所有图**（主图 + 详情图都在同一组里）
    assert "detailImageDetail?.product_images" in block and "detailImageDetail?.detail_images" in block, (
        "参考图未同时列出主图与详情图（用户口径：列出该商品下的所有图）"
    )


def test_detail_image_reference_section_is_optional_reference():
    """v2.23：参考图区 = **「可选参考图」**，其下含**原始主图 / 原始详情图**两类
    （用户口径「参考图应列出该商品下的所有图，包括主图和详情图」+
    「原始主图和原始详情图归为可选参考图」）。

    守三件：① 有「可选参考图」这个统称标题；② 两类各有自己的分组标题且带「原始」前缀；
    ③ 两类都在**同一个 Tab 内**列出（不是各 Tab 一半）。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    block = _slice_between(page, "open={detailImageOpen}", "</Modal>")
    # v2.23：主图与详情图**合并为一组「可选参考图」**（用户口径「放一组内」）
    assert 'label="可选参考图"' in block, "参考图未合并为「可选参考图」一组"
    assert "maxSelectable={6}" in block, "参考图未设 6 张上限（用户口径「最多选 6 张」）"
    # 组内同时含主图与详情图（合并，不是两个卡片）
    assert "detailImageDetail?.product_images" in block
    assert "detailImageDetail?.detail_images" in block
    # v2.23：新增**预览块**（该 Tab 对应的那类原图，仅供预览）
    assert "本类原始图预览" in block, "缺少本类原始图预览块"
    assert "仅供预览" in block, "预览块未标明「不参与勾选」"


def test_detail_image_result_is_grouped_in_ui():
    """v2.20：**生成结果也按组分两大块**（用户口径「生成的结果也要分主图和详情图，
    这是两大块」）。

    守三件：① 结果区按后端下发的 `groups` 渲染（不在前端自己 groupBy）；
    ② 每组显示「成功/要了几张」；③ 某组全灭时该组仍渲染（给一行说明而不是消失）。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    # v2.22：结果块抽成子组件（按组渲染），断言落在组件定义上
    result = _slice_between(page, "function DetailImageGroupResult", "function DetailImageGroupErrors")
    assert "groups" in result and ".find((item) => item.group === groupKey)" in result, (
        "结果未按组从后端下发的 groups 中取"
    )
    assert "group.succeeded" in result and "group.requested" in result, (
        "每组未显示「成功/要了几张」"
    )
    assert "group.assets.length === 0" in result, "某组全灭时没有兜底渲染（该组会静默消失）"


def test_detail_image_supports_single_regenerate():
    """v2.21：**单张重做**（设计 17 §13.3.7.8）。

    守三件：① 结果区每张有「重做这张」入口；② 点击时**带上该张的组与序号**；
    ③ 重做目标存了 state（用来只让那一张转圈，不是整组变灰）。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    # v2.22：结果块在子组件里，断言落在组件定义上
    result = _slice_between(page, "function DetailImageGroupResult", "function DetailImageGroupErrors")
    assert "重做这张" in result, "结果区缺少单张重做入口"
    assert "regenerating?.group === groupKey" in result, "重做态未用于单张 loading"
    # 传的是该张的组与序号（不是写死的）
    assert "onRegenerate(asset.group_index || 1)" in result, "重做未带上该张的序号（会重错图）"
    block = _slice_between(page, "open={detailImageOpen}", "</Modal>")
    # 传的是 **本 Tab 的组** + 该张序号（不是写死的）
    assert "group: tab.key" in block, "重做未带上当前组"
    assert "void generateDetailImage({ group: tab.key, index })" in block, (
        "重做调用未带组与序号（会重错图）"
    )
    # 请求函数确实把它发出去了（能点 ≠ 生效）
    api = _read(ROOT / "frontend/src/lib/api.ts")
    assert "regenerate" in api and "...(regenerate ? { regenerate } : {})" in api, (
        "请求函数未把单张重做参数发给后端"
    )


def test_detail_image_generate_button_requires_reference():
    """v2.19：**必须勾选参考图**才能点「生成」（用户口径「必须选择原始参考图才可以
    点击生成按钮」）。

    守三件：① 按钮 disabled 与参考图数量挂钩；② 禁用时给出原因（Tooltip），
    不让用户对着灰按钮猜；③ 张数确实传给了请求函数（能选 ≠ 生效，最典型的假接线）。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    block = _slice_between(page, "open={detailImageOpen}", "</Modal>")
    # v2.29.7：判据是「**勾了就能生成**」（不再按 Tab 过滤参考图）——
    # 两个勾选区已在 v2.23 合并成一个，按组过滤会让「在另一个 Tab 里勾的图」
    # 全被滤掉、按钮恒灰（真机 bug）。
    assert "activeGroupRefs.length === 0" in block, "生成按钮未与参考图勾选挂钩"
    assert "disabled={" in block, "生成按钮缺少禁用态"
    # 文案改了（不再点「在 XX Tab 里勾」—— 勾选区已经不属于某一个 Tab 了）
    assert "勾选至少一张" in block, "禁用态未说明原因"
    # ③ 张数真正传进请求函数（假接线守卫：能选 ≠ 生效）
    generate_fn = _slice_between(page, "async function generateDetailImage", "function ")
    assert "groupSettings" in generate_fn, "按组配置未传给请求函数（填了也不生效）"


def test_library_card_shows_note_type_tag():
    """类型标签的**可见性口径**：卡片上不展示，鼠标悬停卡片才显示（2026-09-23，§25.7.3）。

    用户 2026-09-23 口径：「**卡片上不展示了，展示的只有鼠标放到卡片上显示标签即可**」。

    ⚠️ 本条**取代**旧契约（原名 `test_library_card_shows_note_type_tag` 断言
    `'"三方同步"' in code and '"本地"' in code` + `count('"三方同步"') >= 2`，
    即「卡片上必须有一枚类型标签」）。**原断言保留在版本历史里，不静默删除。**

    ⚠️ 其后又经历两轮用户口径，本条**第三次改写**（原两次的断言也一并保留在
    docstring 里，不静默删除）：
    * 第二轮（§25.7.5②）曾断言「封面区**无**类型文案、正文**有**、Tooltip 仍在」；
    * 第三轮（2026-09-23「三方同步/本地生成使用 tag 放图片右下角」+
      「两句长文案移除」）⇒ **封面右下角有 Tag、正文无类型文本、Tooltip 整条移除**。

    现在的强度**不降反升** —— 四件事都要同时成立：
    ① 类型文案**仍然有出口**（不是从界面消失），且出口在**封面右下角的 Tag** 上；
    ② 封面**左下角**仍然**没有**类型 Tag（§29.5 那一轮的口径继续有效 ——
       左下角是发布状态 + 同步/发布按钮，再塞类型就挤）；
    ③ 卡片级 Tooltip **必须已经移除**（类型已直接可见，悬停提示只剩复读）；
    ④ 那两句长文案（「…账号同步链路落库，可拉取平台最新内容」/
       「…由商品智能生成，待发布 / 发布失败可重发」）**不得再出现在任何源码里**。

    ⚠️ 文案与配色的唯一来源都在 `lib/note-filters.ts`（见 DEF-061：同一字段两处
    各写一份文案/配色就是缺陷）⇒ ① 查那个文件，页面侧只查「有没有接上出口」。
    **不要**把 `"三方同步"` 断言改回页面文件 —— 那等于要求文案再抄回页面。
    """
    code = _code_only(_read(LIBRARY))
    # ① 文案 + 配色的出口仍在（查唯一实现，不查页面）
    filter_lib = ROOT / "frontend/src/lib/note-filters.ts"
    lib_source = _code_only(filter_lib.read_text(encoding="utf-8"))
    for token in ("三方同步", "本地生成", "本地采集"):
        assert token in lib_source, f"类型文案「{token}」失去出口（既无 Tag 也无下拉）"
    assert "noteTypeTagColor" in lib_source, "缺少类型配色唯一实现（noteTypeTagColor）"
    # ② 出口接在**封面右下角的 Tag** 上
    card_block = _slice_between(code, "{notes.map((note) => {", "</Col>")
    assert "noteTypeLabel(note.note_type)" in card_block, "卡片未接上类型文案出口（noteTypeLabel）"
    assert "noteTypeTagColor(note.note_type)" in card_block, "卡片未接上类型配色出口（noteTypeTagColor）"
    assert "right: 8, bottom: 8" in card_block, "类型 Tag 未放在封面右下角（用户口径）"
    # ③ 封面**左下角**仍不得有类型 Tag
    cover_left = _slice_between(code, 'left: 8, bottom: 8', "</div>")
    assert "noteTypeLabel" not in cover_left, (
        "封面左下角又出现类型 Tag —— §29.5 那一轮的口径继续有效（左下角归状态与按钮）"
    )
    # ④ 卡片级 Tooltip 必须已移除；那两句长文案不得再出现在任何地方
    assert "noteTypeTooltip" not in code, "卡片级类型 Tooltip 应已移除（类型已直接可见）"
    assert "noteTypeTooltip" not in lib_source, "noteTypeTooltip 函数应已删除（两句长文案随之移除）"
    for sentence in ("账号同步链路落库", "由商品智能生成"):
        assert sentence not in lib_source and sentence not in code, (
            f"长文案「{sentence}…」仍留在源码里（用户要求移除）"
        )


def test_library_card_body_is_title_source_and_time_only():
    """卡片正文**只有标题 + 来源 + 时间**，互动数据不上卡片（2026-09-23，§25.7.4）。

    用户原口径：「内容库笔记卡片不要把互动数据展示在笔记卡片上，只展示笔记标题和
    来源加时间即可，账号名和时间之间添加空格」。

    三件事同时成立：

    ① **卡片块内不再渲染互动计数**（点赞/收藏/评论/分享那四个 `<span>`）——
       断言方式是在**卡片块切片内**找那四个图标组件，而不是全文件搜索：这四个
       图标在**详情弹窗**里仍在用（那才是看数据的正确位置），全文件搜会恒真。
    ② `getNoteEngagement` **函数本身保留**（详情弹窗依赖它）—— 一并钉住，
       避免被顺着「卡片不用了就整个删掉」删成 BREAKING（详情弹窗会直接编译不过，
       但那时才发现在测试里就已经太晚）。
    ③ 账号名与时间之间**必须有显式分隔**：原先靠 `marginLeft: note.author_name ? 6 : 0`
       拼 —— 作者名缺失时时间会贴上去，且这是「有作者才有间距」的隐式行为。
       现在改成一个带 `gap` 的横向 flex 容器，间距**不依赖前一个元素是否存在**。

    ⚠️ 旧契约（卡片正文 = 标题 + 作者 + 来源 + 时间 **+ 互动一排**）被本条取代，
    **原行为不静默删除**：设计 13 §29.6 就地把「互动在卡片上」标注为被 §25.7.4 取代，
    详情弹窗那一份继续生效。
    """
    code = _code_only(_read(LIBRARY))
    card_block = _slice_between(code, "{notes.map((note) => {", "</Col>")

    # ① 卡片上不得再出现互动计数（四个图标都是互动专用的；详情弹窗里另有一份）
    for icon in ("HeartOutlined", "StarOutlined", "MessageOutlined", "ShareAltOutlined"):
        assert icon not in card_block, f"卡片正文仍在渲染互动图标 {icon}（§25.7.4：互动不上卡片）"
    assert "getNoteEngagement(note)" not in card_block, "卡片仍在取互动数据（getNoteEngagement(note)）"

    # ② 函数本体保留：详情弹窗仍在使用，删掉会让详情编译不过
    assert "function getNoteEngagement(" in code, "getNoteEngagement 被误删 —— 详情弹窗仍在用"
    assert "getNoteEngagement(selectedNote)" in code, "详情弹窗仍应渲染互动数据"

    # ③ 来源与时间之间有显式间距（flex gap），不再靠 `marginLeft` 与作者名绑定
    meta = _slice_between(code, "<Card.Meta", "} />")
    assert "gap: 6" in meta, "账号名与时间之间缺少显式间隔（§25.7.4）"
    assert "marginLeft" not in meta, "不该再用 marginLeft 拼间距 —— 作者名缺失时它就不生效"


def test_library_card_type_lives_only_on_cover_bottom_right():
    """类型的**唯一承载**是封面右下角 Tag：正文不得再有、Tooltip 不得再有（§25.7.7）。

    用户 2026-09-23 口径（第三轮）：「三方同步/本地生成使用 tag 放图片右下角」
    + 「两句长文案移除」。

    三轮演变的**终态**（前两轮的断言一并保留在 docstring，不静默删除）：

    | 轮次 | 用户口径 | 类型的承载 | 本条对应断言 |
    |---|---|---|---|
    | ① | 「卡片上不展示了，鼠标放到卡片上显示标签即可」 | 封面 Tag 移除 ⇒ 卡片级 Tooltip | 左下角**无** Tag |
    | ② | 「笔记卡片上应展示本地生成或三方同步」 | 正文里一行**纯文本** | 正文**曾有** `noteTypeLabel` |
    | ③ | 「使用 tag 放图片右下角」+「两句长文案移除」 | 封面右下角 **Tag** | 正文**无**、右下角**有**、Tooltip **无** |

    ⚠️ ③ 不是把 ① 推翻：① 取消的是「封面**左下角**那枚 Tag」（与发布状态/按钮同排、
    与「图文/视频」易混），③ 要的是「封面**右下角**」（那个位置本来是空的）。
    所以「左下角无 Tag」这条断言**继续有效**，由 `…_shows_note_type_tag` 钉住。

    四条断言：
    ① 封面右下角有 Tag（`noteTypeLabel` + `noteTypeTagColor` + `right: 8, bottom: 8`）；
    ② `Card.Meta` 区间内**没有** `noteTypeLabel`（正文回到「标题 + 来源 + 时间」三项）；
    ③ 卡片级 Tooltip 整条移除（类型已直接可见，悬停提示只剩复读）；
    ④ 左下角那排给右下角**让位**（`maxWidth` 从 `calc(100% - 16px)` 收窄）——
       两块都是 absolute + 同 zIndex，不收窄就会压在一起。
    """
    code = _code_only(_read(LIBRARY))
    card_block = _slice_between(code, "{notes.map((note) => {", "</Col>")

    # ① 封面右下角：Tag 三件套齐全
    assert "noteTypeLabel(note.note_type)" in card_block, "卡片未接上类型文案出口（noteTypeLabel）"
    assert "noteTypeTagColor(note.note_type)" in card_block, "卡片未接上类型配色出口（noteTypeTagColor）"
    assert "right: 8, bottom: 8" in card_block, "类型 Tag 未放在封面右下角（用户口径：放图片右下角）"

    # ② 正文只留标题 + 来源 + 时间（第二轮的纯文本已按第三轮口径撤回）
    meta = _slice_between(code, "<Card.Meta", "} />")
    assert "noteTypeLabel" not in meta, (
        "卡片正文又渲染类型文本 —— 第三轮口径已把它移到封面右下角的 Tag 上"
    )

    # ③ 卡片级 Tooltip 必须已移除
    assert "noteTypeTooltip" not in code, "卡片级类型 Tooltip 应已移除（类型已直接可见，提示只剩复读）"

    # ④ 左下角那排给右下角让位
    assert "calc(100% - 84px)" in card_block, (
        "左下角那排未给右下角类型 Tag 让位（仍是 calc(100% - 16px) 会压住 Tag）"
    )
    assert "calc(100% - 16px)" not in card_block, "左下角那排仍占满宽 —— 会压住右下角类型 Tag"


def test_library_sync_button_exists_for_published_notes():
    """**已发布过**的笔记一定有同步按钮（2026-09-23 用户口径，§25.7.5）。

    > 需求原文：「已经发布过的笔记一定存在同步按钮功能」。

    **真实库已复现漏按钮**：本地生成笔记发布成功后仍是 `local_generated`
    （§31 生成落库 → `publish_status` 改为 `published`，**类型不变**）⇒
    旧判定 `note.note_type === "synced"` 把它漏掉。真实库里那条
    `local_generated` + `published`（id 1966「新中式桌垫…」）**今天没有同步按钮**，
    用户看到的就是「明明发出去了，却没有同步」。

    三件事同时钉住：
    ① 卡片与详情**两处**都走 `canSyncNote`（同一判定，不留两份实现 —— DEF-044/047/058）；
    ② 页面里**不得**再出现 `note_type === "synced"` 这种就地判断（那就是第二份实现）；
    ③ 判定本身在 `lib/note-filters.ts`，且条件含 `publish_status === "published"`
       （行为验证在 `tests/frontend/note_filters.cjs` 的 F 段，由
       `test_note_filters_and_batch_sync_gates_are_driven_for_real` 真正执行 +
       `syncpub` 变异锚点守它）。

    ⚠️ **前后端同口径**（2026-09-23 用户口径「已发布的笔记也支持同步笔记最新的三方数据」
    之后）：后端 `POST /notes/{id}/sync` 的闸门已放宽到同一个判据
    （`models/note.py::note_supports_sync()` = `synced` ∨ `publish_status=="published"`），
    不满足时给 400 **`NOTE_NOT_SYNCABLE`**（原 `NOTE_NOT_SYNCED` 已随口径改名）。
    ⇒ 按钮显示与接口放行一致，不再有「按钮在、点下去 400」。
    ⚠️ 改前端判据时**必须同时改后端那个函数** —— 两份判据各改一半就是
    「按钮显示却 400」或「按钮没有却还能调」。
    """
    code = _code_only(_read(LIBRARY))
    # ① 卡片 + 详情两处都调同一个判定函数
    assert "canSyncNote(note)" in code, "卡片未用 canSyncNote 判定同步按钮"
    assert "canSyncNote(selectedNote)" in code, "详情弹窗未用 canSyncNote 判定同步按钮"

    # ② 不得再有就地判断（第二份实现的两种常见形态）。
    #    ⚠️ 断言范围是**同步按钮的两个区块**，不是全文件：详情弹窗里
    #    `selectedNote.note_type === "synced"` 还被用于选**图标与颜色**
    #    （CloudSyncOutlined/DatabaseOutlined 与蓝/紫/default）—— 那是**呈现**
    #    不是**权限判定**，两件事分开是对的。全文件禁掉会把那段误伤。
    card_sync = _slice_between(code, "{canSyncNote(note) && (", "</Button>")
    detail_sync = _slice_between(code, "{canSyncNote(selectedNote) && (", "</Button>")
    for name, block in (("卡片", card_sync), ("详情", detail_sync)):
        assert "canSyncNote(" in block, f"{name}同步按钮未用 canSyncNote 判定"
        assert 'note_type === "synced"' not in block, (
            f"{name}同步按钮出现就地类型判断 —— 应统一走 canSyncNote（同一语义不留两份实现）"
        )

    # ③ 判定条件含「已发布」这一半
    lib_source = _code_only((ROOT / "frontend/src/lib/note-filters.ts").read_text(encoding="utf-8"))
    # ⚠️ 切片下界取**下一个导出**（函数之后的第一个 `export`）：
    #    不能用 `}`（签名里的类型标注自带 `}` ⇒ 切到签名就停）；
    #    不能用 `return true;`（函数体第一句就是它 ⇒ 切太短）；
    #    **更不能**用 `=== "published";` 当边界 —— 那正是要断言的串，
    #    用它当下界等于把断言对象自己切掉（恒假）。
    body = _slice_between(lib_source, "export function canSyncNote(", "\nexport ")
    assert "publish_status" in body, "canSyncNote 未看 publish_status（已发布的笔记仍会漏按钮）"
    assert '"published"' in body, "canSyncNote 未认 published 这个值"
    assert "note_type" in body, "canSyncNote 未看 note_type（synced 笔记也会漏按钮）"

    # ④ 后端闸门与前端**同口径**（2026-09-23 用户口径「已发布的笔记也支持同步」）
    backend = _code_only((ROOT / "backend/app/models/note.py").read_text(encoding="utf-8"))
    gate = _slice_between(backend, "def note_supports_sync(", "\n\nclass Note")
    assert "note_type" in gate and "NOTE_TYPE_SYNCED" in gate, (
        "后端 note_supports_sync 未看 note_type —— synced 笔记会突然不能同步"
    )
    assert "publish_status" in gate and "PUBLISH_STATUS_PUBLISHED" in gate, (
        "后端 note_supports_sync 未看 publish_status —— 已发布笔记仍会被 400 挡住"
    )
    # 错误码随口径改名（旧名 `NOTE_NOT_SYNCED` 不该再出现）
    api = _code_only((ROOT / "backend/app/api/notes.py").read_text(encoding="utf-8"))
    assert "note_supports_sync(note)" in api, "同步端点未走 note_supports_sync 判定"
    assert "NOTE_NOT_SYNCABLE" in api, "同步端点未用新的错误码 NOTE_NOT_SYNCABLE"
    assert "NOTE_NOT_SYNCED" not in api, "旧的错误码 NOTE_NOT_SYNCED 仍有残留"


def test_library_search_area_is_exactly_two_rows():
    """搜索条件区**恰好两行**、按钮组落在第二行末尾（2026-09-23 用户口径，§25.7.5）。

    > 需求原文：「内容库的搜索区域重新布局，布局为两行展示」。

    与商品列表页 §33.8 同一口径（那次已固化为 `…_review` 系列守卫）——
    内容库这次是同一个版式要求，两页共用同一套「两行 + 按钮收尾」的语义。

    改之前是**一行硬排 9 项**：`span` 之和 26 > 24 ⇒ `Col` 挤到第二行，
    按钮又被挤到半行空位 —— 观感是「一行没排完、第二行只有两个按钮」，
    最该显眼的「筛选」按钮反而不在视线落点上。

    现在按**语义**切两行（不是按数量塞满）：
    * 第一行 = **定位条件**：关键词 + 账号 + 类型 + 标签 + 发布状态（决定「看哪一批」）；
    * 第二行 = **开关 + 动作**：有素材 / 有评论｜重置 / 筛选。
      ⚠️ 原第二行的「卡片 / 表格」`Segmented` 已随表格视图一起移除
      （2026-09-23 用户口径「内容库默认使用卡片，不再支持表格展示笔记」）。

    四条断言（都在**搜索卡切片**内，不是全文件搜 —— 全文件搜会恒真）：
    ① 恰好两个 `<Row gutter={12}`（**行数是断言出来的，不是看出来的**）；
    ② 两行的 `span` 各自**不溢出 24**（溢出 = 会有 Col 自己换行 = 三行）；
    ③ 两个按钮（重置 / 筛选）都在**第二行**（按钮组置尾）；
    ④ 第二行带 `justify="space-between"` —— 开关靠左、按钮靠右，不挤成一团。
    """
    code = _code_only(_read(LIBRARY))
    # ⚠️ 2026-10-05：锚点从单行 `<Card size="small" style={{ marginBottom: 16 }}>` 改成
    # **多行卡头**（页头移除后这张卡多了 `title="内容库"` + `extra` 刷新按钮，
    # 单行锚点会失配 ⇒ 切片拿到空串、行数断言变成「实际 0」的假红）。
    search_card = _slice_between(
        code,
        'title="内容库"',
        "</Card>",
    )

    # ① 恰好两行
    row_count = search_card.count("<Row gutter={12}")
    assert row_count == 2, f"搜索条件区应有且仅有两行 <Row gutter={12}，实际 {row_count}"

    # ② 每行 span 之和 ≤ 24
    rows = search_card.split("<Row gutter={12}")
    for index, row in enumerate(rows[1:], start=1):
        spans = [int(v) for v in re.findall(r"<Col span=\{(\d+)\}", row.split("</Row>")[0])]
        total = sum(spans)
        assert total <= 24, f"第 {index} 行 span 之和为 {total}（>24 会自己换行 ⇒ 实际三行）"

    # ③ 按钮都在第二行
    second_row = rows[2].split("</Row>")[0] if len(rows) > 2 else ""
    assert "重置" in second_row and "筛选" in second_row, "重置 / 筛选按钮不在第二行（按钮组应置尾）"
    assert "onClick={clearFilters}" in second_row, "重置按钮不在第二行"
    assert "onClick={() => void loadNotes()}" in second_row, "筛选按钮不在第二行"

    # ④ 第二行两端对齐（开关靠左、按钮靠右）
    assert 'justify="space-between"' in second_row, "第二行应 justify=space-between（开关与按钮分列两端）"


def test_library_has_no_table_view_and_no_batch_tag_dropdown():
    """内容库**只剩卡片一种视图**，批量操作卡里**没有标签下拉**（2026-09-23 用户口径）。

    > 需求原文：
    > ① 「内容库默认使用卡片，不再支持表格展示笔记」；
    > ② 「把选择当前页中的标签下拉框移除」。

    ① **表格视图移除**（不是「默认改成卡片、留着切换器」）：
    * 页面里不得再有 `viewMode` / `setViewMode` —— 视图只剩一种，
      「当前视图」就不再是状态，留着就是一个永远不变的死状态；
    * 不得再 `<Table`（antd 表格）、不得再有 `tableColumns`；
    * 不得再从 antd 导入 `Segmented` / `Table` / `ColumnsType`（死导入）。
    ⚠️ **反向防回归**：只删渲染分支而留着 state，界面看不出差别但代码里
    多一个死状态 —— 那种「看起来还在那里」的东西正是 DEF-048/056 的形态。

    ② **批量标签下拉移除**：批量操作卡（「选择当前页」那一卡）里的
    「选择标签」下拉 + 「加标签」/「去标签」两枚按钮**三件套一起撤** ——
    没有下拉就选不到标签，两枚按钮也就永远 disabled，只删下拉会留下死按钮。
    * `batchTagId` / `setBatchTagId` / `applyBatchTag` 一并移除；
    * ⚠️ **后端与单条能力不动**：`POST /notes/batch-tag` 端点保留（API 消费者）；
      ⚠️ **2026-09-25（§34.4）前端单条增删从 `batchTagNotes` 收敛到
      `updateSavedNoteTopics`（PUT /notes/{id}/topics，整列替换 + find-or-create）**
      —— 标签表并入话题表后一个入口管全部话题，`batchTagNotes` 前端函数随之删除。
    """
    code = _code_only(_read(LIBRARY))
    # ① 表格视图
    assert "viewMode" not in code, "仍有 viewMode —— 视图只剩一种，该状态已是死状态"
    assert "setViewMode" not in code, "仍有 setViewMode —— 没有可切换的视图了"
    assert "<Table" not in code, "仍在渲染 antd Table（表格视图应已移除）"
    assert "tableColumns" not in code, "仍有 tableColumns —— 表格列定义应随表格一起删"
    for symbol in ("Segmented", "Table,", "ColumnsType"):
        assert symbol not in code, f"仍在从 antd 导入死符号 {symbol}（表格/视图切换已移除）"

    # ② 批量标签下拉（三件套 + 死状态一起撤）
    assert "batchTagId" not in code, "仍有 batchTagId —— 只服务于被撤掉的下拉，是死状态"
    assert "applyBatchTag" not in code, "仍有 applyBatchTag —— 没有入口能调用它"
    assert "选择标签" not in code, "批量操作卡里仍有「选择标签」下拉（用户要求移除）"
    assert ">加标签<" not in code and ">去标签<" not in code, (
        "批量操作卡里仍有加标签 / 去标签按钮 —— 没了下拉它们永远 disabled（死按钮）"
    )
    # ⚠️ 能力本身没撤（§34.4 换了入口）：单条增删走 PUT /notes/{id}/topics
    api = _code_only(_read(ROOT / "frontend/src/lib/api.ts"))
    assert "updateSavedNoteTopics" in api, "updateSavedNoteTopics 被误删 —— 单条话题增删的唯一落库入口"
    assert "/notes/${noteId}/topics" in api, "话题函数未指向 PUT /notes/{id}/topics"
    assert "batchTagNotes" not in api, "batchTagNotes 应随标签表合并一并移除（前端不再调用）"
    assert "updateSavedNoteTopics(" in code, "页面里 updateSavedNoteTopics 调用被误删（话题增删仍需它）"


def test_library_detail_renders_topics_and_goods_from_columns():
    """详情卡渲染话题（专有列）与挂载商品（关系表查询的投影）（§28.4 / §29.2）。"""
    code = _code_only(_read(LIBRARY))
    assert "selectedNote.topics" in code, "详情卡未渲染话题（专有列）"
    assert "selectedNote.goods" in code, "详情卡未渲染挂载商品（关系表投影）"


def test_library_goods_layout_matches_my_notes_page():
    """商品展示版式对齐「我的笔记」（§29.2）：Divider 标题 + 72px 图 + 规格/价格同行。

    用户口径「参考我的笔记页笔记详情弹窗中商品的展示布局」⇒ 三个特征必须在位：
    `goods-meta-row`（规格与价格同一行的全局样式类）、72×72 封面、计数 Tag。
    """
    code = _code_only(_read(LIBRARY))
    assert "goods-meta-row" in code, "商品块未使用规格/价格同行版式（goods-meta-row）"
    assert "width: 72" in code and "height: 72" in code, "商品封面不是 72px（对齐我的笔记）"
    assert "挂载商品" in code, "商品块缺少分隔标题"


def test_library_detail_has_no_comments_or_raw_json():
    """内容库**不展示笔记评论与原始 JSON**（§29.1，用户口径）。

    评论区（`commentsSlot` / `NoteCommentList`）与 `raw_json` 原文块都从内容库移除；
    组件与端点保留（「我的笔记」页仍在用）—— 拆的只是内容库这一侧的渲染。
    """
    code = _code_only(_read(LIBRARY))
    for symbol in ("NoteCommentList", "fetchSavedNoteComments", "commentsSlot"):
        assert symbol not in code, f"内容库仍在引用已移除的评论符号：{symbol}"
    assert "原始 JSON" not in code, "内容库不应再渲染原始 JSON 原文块"
    assert "loadComments" not in code and "resetComments" not in code


def test_library_topics_are_editable_tags():
    """话题 Tag 展示 + 可增删 + 实时落库（§29.3，用户口径）。

    「在话题后面增加一个加号支持添加更多话题，话题支持删除，实时更新数据库，Tag 展示」
    ⇒ 源码里必须有：`closable`（删）、`添加话题` 的 `+`（增）、`updateSavedNoteTopics`（落库）。
    """
    code = _code_only(_read(LIBRARY))
    assert "closable" in code, "话题 Tag 不可删除（缺 closable）"
    assert "添加话题" in code, "话题区缺少「+」新增入口"
    assert "updateSavedNoteTopics" in code, "话题增删未接落库函数（实时更新数据库）"
    api = _code_only(_read(ROOT / "frontend/src/lib/api.ts"))
    assert "updateSavedNoteTopics" in api, "api.ts 缺少话题更新函数"
    assert "/notes/${noteId}/topics" in api, "api.ts 话题函数未指向 PUT /notes/{id}/topics"


def test_library_card_type_and_sync_on_cover_bottom_left():
    """同步按钮在**封面左下角**、类型 Tag **不在**（§29.4 / §29.5 / §25.7.3）。

    用户 2026-09-22 深夜口径：「将笔记类型和同步按钮全部放图片左下角」—— 同日先调过
    「移去正文行」（§29.4 第二条，已被 §29.5 取代）。

    ⚠️ **2026-09-23 追加（§25.7.3）**：用户口径「**卡片上不展示了，展示的只有鼠标放到
    卡片上显示标签即可**」⇒ §29.5 里**关于类型 Tag 的那一半被取代**，**关于同步 / 发布
    按钮仍在左下角的那一半继续生效**。本条的断言按这个拆法重写：
    保留下来的（发布状态 / 同步按钮 / 红色主按钮 / 判据在封面块内）强度不变，
    被取代的（「封面左下角必须有类型 Tag」）反向钉死为「**必须没有**」。
    **原断言保留在版本历史里，不静默删除。**
    """
    code = _code_only(_read(LIBRARY))
    # ① 卡片**不渲染话题**（2026-09-25 用户口径「拆分的话题不要放在卡片上，放在
    #    笔记详情弹窗内就可以了」）：§29.9「卡片也展示话题、悬停看全部」被本条**取代**
    #    （原句保留在设计文档 §29.9 并标注），§34.4 标签并入话题后短暂落到卡片上的
    #    那版也一并退回。原断言（保留备查，现在**必须为假**）：
    #      assert "(note.topics ?? []).slice(0, 4)" in code   # 卡片最多展示 4 枚
    #      assert "(note.topics ?? []).length - 4" in code    # 超出给「+N」收窄
    #      assert "(note.topics ?? []).map(" in code          # 卡片渲染话题（含 Tooltip 悬停那版）
    assert "(note.topics ?? []).slice(0, 4)" not in code, (
        "卡片上又渲染话题了 —— 用户口径：话题只放详情弹窗（§29.9 卡片展示已废止）"
    )
    assert "(note.topics ?? []).map(" not in code, "卡片上又渲染话题了（Tooltip 悬停那版也不许回来）"
    assert "note.tags" not in code, "卡片还在读 note.tags（标签表已并入话题表，§34.4）"
    # 正向锚点：话题的展示与增删**仍在详情弹窗**（口径是「搬家」不是「删除」）
    assert "selectedNote.topics" in code, "详情弹窗的话题区不能一起撤掉"
    # ② 封面左下角一块：**发布状态 + 同步按钮**（类型 Tag 已按 §25.7.3 移出）
    cover = _slice_between(code, 'left: 8, bottom: 8', "</div>")
    # 组件在 §32.3.4（2026-09-23）由 `PublishedTag` 改名 `PublishStatusTag`：
    # 发布状态收敛成 `publish_status` **一个字段**后，卡片上只渲染**一个**状态标签
    # （旧实现是「已发布/待发布」开关 + 「发布失败」tag 并排，会互相矛盾）。
    # 这里只换指实现的名字 —— 断言强度不变，仍是「封面左下角必须有发布状态」。
    assert "PublishStatusTag" in cover, "封面左下角应保留发布状态"
    #     ⚠️ **反向断言（§25.7.3，2026-09-23）**：旧断言是
    #   `assert "三方同步" in cover and "本地" in cover, "封面左下角缺少笔记类型 Tag（§29.5）"`
    # —— 它现在**必须为假**，否则说明类型 Tag 又被加回封面了。
    assert "三方同步" not in cover and "本地生成" not in cover, (
        "封面左下角仍有笔记类型 Tag —— §29.5 该半条已被 §25.7.3 取代（改为悬停显示）"
    )
    # ⚠️ **2026-09-23 二轮（§25.7.5）**：旧断言是
    #   `assert 'note.note_type === "synced"' in cover, "同步按钮的可见性判据应在封面块里"`
    # —— 它现在**必须为假**：判定已收敛到 `canSyncNote`（唯一实现）。
    # 类型文案改为在**正文**展示（§25.7.6）⇒ 封面块里只允许出现 `canSyncNote`。
    assert "canSyncNote(note)" in cover, "同步按钮的可见性判据应在封面块里（canSyncNote）"
    assert 'note.note_type === "synced"' not in cover, (
        "封面块里又出现就地类型判断 —— 应统一走 canSyncNote（§25.7.5）"
    )
    assert "syncNote(note)" in cover, "封面左下角缺少同步按钮（§29.5）"
    # ③ 同步按钮红色背景（用户口径）：`danger` + `type="primary"`（红底白字）。
    #    详情弹窗那枚同款 —— 同一动作不留两种呈现。
    assert "danger" in cover and 'type="primary"' in cover, "封面上的同步按钮应为红色主按钮"
    detail_button = _slice_between(code, 'title="用这条笔记的 ID 拉取平台最新内容', "</Button>")
    assert "danger" in detail_button and 'type="primary"' in detail_button, (
        "详情弹窗的同步按钮应为红色主按钮（与卡片同款）"
    )
    # ④ 正文底部不再单开类型/同步行（旧布局的标记串应已消失）
    assert 'marginTop: 6, display: "flex", alignItems: "center", gap: 6' not in code, (
        "正文底部不应再有类型/同步行（已移到封面左下角）"
    )


def test_library_detail_title_row_is_plain_and_tags_live_in_topics_section():
    """内容库详情弹窗标题栏**只留标题文本**；标签展示 / 添加整体并入话题区（§34.4）。

    事故分析（用户口径「分析添加标签加号图标依旧未紧跟弹窗标题后」）：§29.7 把加号放在
    共用弹窗的 `titleExtra` 插槽里，而标题栏容器是 `justify-content: space-between` ——
    标题文本被推到最左、插槽被推到最右，加号与标题之间隔着大半个标题栏。**插槽架构与
    「紧跟」互斥**。修法 = 自定义标题（`title` 传 ReactNode，调用方排整行，DOM 顺序即
    界面顺序）。§29.12 当时把「标签 chips + 加号」排进了标题行。

    ⚠️ **2026-09-25（§34.4）标题行那半条被取代**：用户口径「将内容库笔记详情弹窗中
    标题后展示标签tag和添加标签tag合并到笔记话题功能中」⇒ 标题行的标签 chips 与
    「+ 添加标签」弹层**整块移除**，标题栏只留标题文本；标签的展示 / 增删 / 落库
    全部并进正文下方的**话题区**（一个入口管全部话题）。本断言按新口径重写：
    ① 标题栏无标签 chips / 无「添加标签」Tooltip / 无 `removeTagFromNote`；
    ② `titleExtra` 插槽整体移除（同一语义不留两条路，两处引用都干净）；
    ③ 共用组件按 `typeof title` 分流：字符串走旧版式（我的笔记不变）、ReactNode 走自定义行；
    ④ §29.3 / §29.10 的话题不变量保持（可删实时落库 / 纯图标 / 单 AutoComplete）。
    """
    code = _code_only(_read(LIBRARY))
    # ① 标题栏只留标题文本（标签能力整体搬去话题区）
    assert 'title="添加标签"' not in code, "标题栏仍有「添加标签」Tooltip（应已并入话题区）"
    assert "removeTagFromNote" not in code, "仍有 removeTagFromNote —— 标签 chips 应随标题栏一起移除"
    assert "selectedNote?.tags" not in code, "标题栏仍在读 selectedNote.tags（字段已删）"
    assert "添加标签" not in code, "页面里仍有「添加标签」字样（话题区入口叫「添加话题」）"
    # ② titleExtra 插槽整体移除（代码里不再有该 prop，注释里的历史说明不算）
    assert "titleExtra={" not in code and "titleExtra:" not in code, "titleExtra 插槽应已移除（§29.12）"
    modal = _code_only(_read(NOTE_DETAIL_MODAL))
    assert "titleExtra" not in modal.replace("titleExtra` 插槽正因此", "").replace(
        "（§29.7 的 titleExtra）", ""
    ), "共用弹窗不应再保留 titleExtra 插槽（§29.12）"
    # ③ 共用组件按类型分流：字符串旧版式 / ReactNode 自定义行
    assert 'typeof title !== "string"' in modal, "共用弹窗应区分字符串标题与自定义标题"
    assert 'marginLeft: "auto"' in modal, "自定义行里的刷新按钮应仍靠右"
    my_notes = _code_only(_read(MY_NOTES))
    assert "title={noteForDetail?.title}" in my_notes, "「我的笔记」页应仍传字符串标题（版式不变）"
    # ④ §29.3 / §29.10 话题不变量保持（能力搬家不降级）
    assert "closable" in code and "removeTopic" in code, "话题 Tag 应可删并实时落库"
    assert "PlusOutlined" in code, "添加按钮应使用加号图标"
    # ⚠️ 「纯图标无文字」是**旧标题栏按钮**的规则（§29.12）；话题区的「+」触发器
    # 本来就带「添加话题」文字（§29.3 既有形态）⇒ 这里只钉 AutoComplete 单组件。
    assert "AutoComplete" in code and "submitTopicDraft" in code, "添加话题应仍是单 AutoComplete（§29.10）"


def test_goods_price_uses_fen_price_text_not_number():
    """挂载商品价格走 `fenPriceText`（区间价不出 NaN）；分→元全项目一份实现。

    后端 `_price_fen_text` 的契约：同价 `"1999"` / 区间 `"1999~2999"` / 取不到 `""`。
    `Number("1999~2999")` ⇒ `NaN` ⇒ 界面「¥ NaN」（渲染了但读不到，缺值口径禁止）。
    `yuan` 从 shop-items-page 的本地函数提升为 `lib/format.ts` 共享实现 ——
    同一语义两份实现必漂移（DEF-044/047/058）。
    """
    code = _code_only(_read(LIBRARY))
    assert "fenPriceText(g.price)" in code, "商品卡价格应走 fenPriceText（区间价安全）"
    assert "(Number(g.price) / 100)" not in code, "商品卡价格不允许直接 Number()（区间价会出 NaN）"
    items = _code_only(_read(SHOP_ITEMS))
    assert 'from "../../../lib/format"' in items, "商品台账应导入共享的 yuan"
    assert "function yuan(" not in items, "商品台账不应再保留本地 yuan（一份实现）"


def test_library_detail_refresh_button_in_action_row():
    """内容库详情弹窗：刷新按钮在**功能按钮行**，不在标题栏（§29.8，用户口径）。

    「把内容页笔记弹窗中的功能按钮与刷新按钮放一行」⇒ 本页不再向共用弹窗传
    `onRefresh`（标题栏那个刷新槽位），改在右栏功能按钮行渲染。「我的笔记」页的
    标题栏刷新**不动**（口径限定内容页）。
    """
    code = _code_only(_read(LIBRARY))
    assert "onRefresh" not in code, "内容库不再把刷新放在标题栏（§29.8）"
    assert "refreshDetail()" in code, "功能按钮行应调用 refreshDetail（只重读本地）"
    my_notes = _code_only(_read(MY_NOTES))
    assert "onRefresh" in my_notes, "「我的笔记」页的标题栏刷新应保留（口径限定内容页）"


def test_library_detail_basic_info_has_source_and_merged_times():
    """来源账号并入基础信息行（同作者不渲染）；发布/保存时间同一行（§29.11）。

    用户口径「来源账号放基础信息行，发布时间和保持时间放一行」+ 补充口径「同步落库[时]
    来源账号就是笔记作者」⇒ 来源账号与作者同域比较，同一人不渲染（同步笔记两者是一个人，
    重复显示只是噪声；采集来的别人笔记才显示）。
    """
    code = _code_only(_read(LIBRARY))
    basic = _slice_between(code, 'label="基础信息"', "</Descriptions.Item>")
    assert "来源：" in basic, "来源账号应在基础信息行内（§29.11）"
    assert "account_name" in basic and "author_name" in basic, "来源账号应与作者同域比较"
    assert "!== selectedNote.author_name" in basic, "同一人时不应渲染来源账号（同步笔记=作者本人）"
    # 时间合并：旧的两个独立 Item 标签不再存在
    assert 'label="发布时间"' not in code and 'label="保存时间"' not in code, "发布/保存时间应合并为一行"
    assert 'label="发布 / 保存"' in code, "缺少合并后的时间行"


def test_accounts_page_has_no_reconcile_entry():
    """独立对账入口已拆除（§27.1）：对账融合进同步，账号卡片不再有「对账」按钮。"""
    code = _code_only(_read(ACCOUNTS))
    for symbol in ("ReconcileModal", "reconcileAccountNotes", "reconcileShopGoods"):
        assert symbol not in code, f"账号页仍在引用已拆除的对账符号：{symbol}"
    assert "对账" not in code, "账号页不应再有「对账」按钮/文案"
    assert not (ROOT / "frontend/src/components/platforms/reconcile-modal.tsx").exists(), (
        "对账弹窗组件应随端点一起删除"
    )


def test_no_page_has_a_page_header():
    """**全站不再有任何「页面头」**（2026-10-05 用户口径「移除所有页面页面头」）。

    ## 为什么这条值得一条守卫

    「删页面头」是**纯删除类**需求 —— 改回去不会让任何既有断言变红，下次给某页
    加东西时顺手把三行标题补回来，没有任何机制会提醒（账号矩阵那条守卫是逐页钉的，
    覆盖不到别的页）。所以判据分两层：

    * **反向**：源码里不许再出现 `<PageHeader`，`app-shell.tsx` 也不许再导出它
      （留空壳导出 = 「有没有人用」永远为真，页头会以「组件还在」为由悄悄回来）；
    * **正向**：被删掉的那些文案（eyebrow / 标题 / 描述）不得回归 —— 反向断言
      必须先过 `_code_only` 剥注释，因为这些页的注释里**原样引用了需求原文**。

    ⚠️ 只查「`<PageHeader` 这个词」不够：判据要落在**页面文件集合**上，
    否则「把组件删了、页面自己手写一遍三行排版」照样能过。
    """
    src_root = ROOT / "frontend" / "src"

    # ① 组件本身已删除（不是「没人用」，是**不存在**）
    shell = _code_only(_read(APP_SHELL))
    assert "export function PageHeader" not in shell, (
        "`PageHeader` 组件应已整块删除（2026-10-05；留空壳会让「有没有人用」恒真）"
    )

    # ② 没有任何页面再用它
    offenders = []
    for f in sorted(src_root.rglob("*.tsx")):
        if "<PageHeader" in _code_only(f.read_text(encoding="utf-8")):
            offenders.append(f.name)
    assert not offenders, f"以下页面仍在渲染 PageHeader（页头已按用户口径全站移除）：{offenders}"

    # ③ 被删的 PageHeader 文案不得回归（逐页钉；eyebrow 是这批里最容易被顺手补回的）
    for relative, marks in (
        (
            "frontend/src/pages/platforms/xhs/my-notes-page.tsx",
            ("小红书 · 账号自有内容",),
        ),
        ("frontend/src/pages/models/model-config-page.tsx", ("Model Routing",)),
        ("frontend/src/pages/platforms/xhs/analytics-page.tsx", ("XHS Analytics",)),
        # 2026-10-06 设计 22 §3：正文迁到 auto-ops-panel.tsx（宿主页只剩 Tabs）。
        ("frontend/src/pages/platforms/xhs/auto-ops-panel.tsx", ("XHS Auto Operations",)),
        ("frontend/src/pages/platforms/xhs/keywords-page.tsx", ("XHS Keywords",)),
        ("frontend/src/pages/platforms/xhs/monitoring-page.tsx", ("XHS Monitoring",)),
        ("frontend/src/pages/platforms/xhs/orders-page.tsx", ("小红书运营",)),
        ("frontend/src/pages/platforms/xhs/publish-page.tsx", ("XHS Publish",)),
        ("frontend/src/pages/platforms/xhs/workflow-page.tsx", ("XHS Workflow",)),
        ("frontend/src/pages/platforms/xhs/xhs-dashboard.tsx", ("XHS Workspace",)),
        ("frontend/src/pages/settings/settings-page.tsx", ("Workspace",)),
        # ⚠️ 队列记录页的正文里「队列记录」是**功能名**（清理确认框 / Tab 文案），
        # 不能拿它当「页头文案不得回归」的判据 —— 那条断言会误伤正常文案。
        # 页头在这页的独有特征是「两行同名标题 + 紧随其后的说明行」，而 Tab 用的
        # 「队列执行记录」含「记录」二字 ⇒ 判据改用 **PageHeader 专属的排版**：
        # 页头被删干净后，页面里不该再有 `每个页面都以 <Space size={4}> 开说明` 那种形态。
        # （组件级反正在 ① 已钉死：`export function PageHeader` 不存在 + 全仓无 `<PageHeader`。）
        ("frontend/src/pages/llm-gateway/llm-gateway-page.tsx", ("LLM Gateway",)),
    ):
        code = _code_only(_read(ROOT / relative))
        for mark in marks:
            assert mark not in code, (
                f"{relative} 的页面头文案已按用户口径移除，不得回归：{mark}"
            )

    # ③b 被页头删掉的那些**按钮功能不许跟着丢**（2026-10-05 用户口径「按钮功能先保留，
    #     位置你来决定」）—— 它们都从页头 `action` 挪进了就近卡片 / 工具行。
    #     ⚠️ 这条是必需的：纯「按钮挪位置」的改动，任何「文案不得回归」类断言都看不见，
    #        下次重排卡片时很容易把按钮整块带走而没人报错。
    for relative, markers in (
        # ⚠️ 总览的「开始发现」**不在**本清单里：它先是随页头移除、临时收进首张指标卡，
        #    随后被用户口径「开始发现按钮移除」**整块删除** ⇒ 例外的反向断言在下一段。
        # ⚠️ 数据洞察的「生成报告 / 刷新」**已整块删除**（2026-10-06 设计 22 §4 用户口径：
        #    「内容洞察 tab 的刷新和生成报告按钮功能移除」）⇒ 它从本清单**移出**，
        #    并加反向断言（见下方 `NO_RETURN_MARKERS`）。被取代的原句保留：
        #      ("frontend/src/pages/platforms/xhs/analytics-page.tsx", ("生成报告", "loadAnalytics")),
        # 自动运营：刷新 → 页首右对齐工具行
        ("frontend/src/pages/platforms/xhs/auto-ops-panel.tsx", ("loadData",)),
        # 发布中心：刷新 → 左栏「发布任务」卡 extra
        ("frontend/src/pages/platforms/xhs/publish-page.tsx", ("loadJobs",)),
        # 订单管理：同步订单 / 刷新 / 导出 → 「订单列表」卡 extra
        ("frontend/src/pages/platforms/xhs/orders-page.tsx", ("onSync", "onExport")),
        # 工作流：刷新 / 新建工作流 → 「工作流列表」卡 extra
        ("frontend/src/pages/platforms/xhs/workflow-page.tsx", ("setCreateOpen(true)",)),
        # 关键词组 / 监控中心：刷新 → 各自新增卡 extra
        ("frontend/src/pages/platforms/xhs/keywords-page.tsx", ("loadGroups",)),
        ("frontend/src/pages/platforms/xhs/monitoring-page.tsx", ("loadTargets",)),
        # 我的笔记：刷新 → 账号与身份卡 extra
        ("frontend/src/pages/platforms/xhs/my-notes-page.tsx", ("refreshActive",)),
        # 模型配置：刷新 → 服务商表卡 extra
        ("frontend/src/pages/models/model-config-page.tsx", ("void load()",)),
        # 内容库 / 笔记发现：刷新 → 搜索卡 extra
        ("frontend/src/pages/platforms/xhs/library-page.tsx", ("void loadNotes()",)),
        ("frontend/src/pages/platforms/xhs/discovery-page.tsx", ("loadAccounts",)),
        # 大模型网关：刷新 / 新增 → 各自卡 extra
        ("frontend/src/pages/llm-gateway/llm-gateway-page.tsx", ("openCreate", "void load()")),
    ):
        code = _code_only(_read(ROOT / relative))
        for mark in markers:
            assert mark in code, (
                f"{relative} 缺少被页头移除后应保留的操作入口：{mark}（用户口径：按钮功能先保留）"
            )

    # ③c 三处**整块删除**（按用户口径移除，不是「挪位置」）—— 反向钉死不得回归：
    #     · 总览「开始发现」按钮（2026-10-05「开始发现按钮移除」）
    #     · 设置页「安全边界」卡（2026-10-05「设置页的安全边界模块移除」）
    #     · 数据洞察「生成报告 / 刷新」按钮（2026-10-06 设计 22 §4「刷新和生成报告按钮功能移除」）
    dash = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/xhs-dashboard.tsx"))
    assert "开始发现" not in dash, "总览的「开始发现」按钮已按用户口径移除，不得回归"
    assert "xhs/discovery" not in dash, "总览不应再承载跳转到「笔记发现」的入口"
    analytics = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/analytics-page.tsx"))
    for gone in ("生成报告", "刷新", "generateReport", "isGeneratingReport", "reportMessage"):
        assert gone not in analytics, (
            f"数据洞察「内容洞察」tab 的「生成报告 / 刷新」已按用户口径移除，不得回归：{gone}"
        )
    settings = _code_only(_read(ROOT / "frontend/src/pages/settings/settings-page.tsx"))
    for gone in ("安全边界", "双重归属校验", "由后端统一加密存储"):
        assert gone not in settings, (
            f"设置页「安全边界」模块已按用户口径移除，不得回归：{gone}"
        )
    assert "只读" not in settings, (
        "设置页「安全边界」那张只读说明卡已移除（页面只剩「系统参数」一张可操作卡）"
    )

    # ④ 内容区内边距 = 上 / 左 15px（用户口径「间距太大了，上和左间距统一 15px 即可」）
    shell_raw = _read(ROOT / "frontend/src/components/layout/workspace-shell.tsx")
    assert '"15px 24px 24px 15px"' in shell_raw, (
        "内容区内边距应为「上 15 / 右 24 / 下 24 / 左 15」（左是 Sider 侧、上是顶栏侧）"
    )
    # 必须落在 `<Content style={{ … }}>` 这一行里（同 120 字窗口内既有 `<Content` 又有该值）
    # 判据取「值所在的那一行」——注释里也会出现这个字面量，按行取才不会误判
    cid = shell_raw.rindex("15px 24px 24px 15px")
    line_start = shell_raw.rindex(chr(10), 0, cid) + 1
    line_end = shell_raw.index(chr(10), cid)
    assert "<Content" in shell_raw[line_start:line_end], (
        "15px 内边距必须落在 `<Content style={{ … }}>` 那一行上（改到别处不生效）"
    )


def test_accounts_page_header_removed_and_bind_button_in_card_extra():
    """账号矩阵页**页面头部整块移除**，「绑定账号」收进卡片右上角与「刷新」同行。

    用户 2026-09-28 口径：「将头部的 XHS Accounts / 账号矩阵 / 管理小红书账号、各平台
    登录态与数据同步（笔记 / 商品 / 互动数据）。移除，绑定账号按钮放到右上角与刷新一起」
    —— 设计 13 §40。

    两条断言缺一不可：

    * **正向锚**（`绑定账号` 必须真的在 `extra` 切片里）—— 只写反向断言的话，
      「把整块删空」也能让它变绿（本文件「设置页」那条踩过的坑）；
    * **反向**（三行页头文案不得回归）—— 必须先过 `_code_only` 剥注释：本页注释里
      **原样引用了被删掉的文案**（那是需求原文，该留），不剥注释就是被自己的注释命中。

    为什么值得钉：这条改动看起来「只是删了几行」，而它最容易的回归方式恰恰是
    下次给这页加东西时顺手把页头补回来 —— 那种改动不会让任何断言变红。
    """
    code = _code_only(_read(ACCOUNTS))
    extra = _slice_between(code, "extra={", "styles={{ body: { padding: 24 } }}")
    assert "绑定账号" in extra, "「绑定账号」应在卡片右上角 extra 里（与「刷新」同行）"
    assert "刷新" in extra, "「刷新」应仍在卡片右上角 extra 里"
    for removed in ("XHS Accounts", "账号矩阵", "管理小红书账号"):
        assert removed not in code, f"页面头部已按用户口径移除，不得回归：{removed}"
    assert "<Title" not in code, "页头标题（antd Title）随页头一起移除"


def test_api_ts_has_no_reconcile_functions_but_has_note_sync():
    """api.ts：对账函数随端点移除；单条同步函数在位（§25 / §27）。"""
    code = _code_only(_read(ROOT / "frontend/src/lib/api.ts"))
    for symbol in ("reconcileAccountNotes", "reconcileShopGoods"):
        assert symbol not in code, f"api.ts 仍在引用已拆除的对账函数：{symbol}"
    assert "/notes/reconcile" not in code and "/reconcile-shop-goods" not in code


def test_settings_page_no_longer_hosts_the_donation_module():
    """设置页的「为爱发电」赞赏码模块已**全部移除**（用户 2026-09-24 口径）。

    负面断言先剥注释（`_code_only`）：模块删了，`HeartOutlined` / `Image` 导入
    与两条硬编码媒体路径也不该回来 —— 它们只为这一张卡存在（这两个路径正是
    设计 04 的 FE-6 / Mock 清单 F6 两条待办的关闭原因）。

    ⚠️ 2026-09-26 用户口径「设置页面移除项目声明」：「项目声明」卡（开源警告 Alert
    + 四条禁止条款）也整卡移除 ⇒ 正向锚改成**只剩「安全边界」一张**（仍要锚，
    否则「把整页删空」也能让断言变绿），并对声明文案做反向断言。
    ⚠️ **被取代的原句**（保留，不静默删除）：
      「assert "安全边界" in code and "项目声明" in code, "设置页其余两张卡片应在位"」

    ⚠️ 2026-10-05 用户口径「设置页的安全边界模块移除」：连「安全边界」那张也删了
    ⇒ 正向锚**必须换锚点**（原锚点正是那张卡，它已不存在）。新锚点取「系统参数」
    这张**唯一剩下的卡**：整页删空时它必然缺失，防「删空也能绿」的效力不变。
    **被取代的原句**（保留）：`assert "安全边界" in code, "设置页的「安全边界」卡片应在位…"`
    """
    code = _code_only(_read(SETTINGS))
    assert "为爱发电" not in code, "捐赠模块已移除，不得回归"
    assert "HeartOutlined" not in code, "该导入只为赞赏码卡片存在"
    assert "Image" not in code, "该导入只为赞赏码卡片存在"
    assert "wx_pay" not in code and "zfb_pay" not in code, "硬编码收款码路径不得回归"
    # 新正向锚（2026-10-05）：本页只剩「系统参数」一张卡
    assert "系统参数" in code, "设置页的「系统参数」卡片应在位（防止整页删空也能让断言变绿）"
    # 反向：被删掉的「安全边界」卡不许回归（文案与图标一起钉）
    assert "安全边界" not in code, "设置页的「安全边界」卡已按用户口径移除（2026-10-05）"
    assert "SafetyCertificateOutlined" not in code, "该图标只为安全边界卡存在，应随卡一起删"
    # 2026-09-26 用户口径「设置页面移除项目声明」⇒ 反向钉死，文案不许回来
    assert "项目声明" not in code, "设置页的「项目声明」卡已按用户口径移除"
    assert "开源学习项目" not in code and "禁止任何形式的商业化使用" not in code, (
        "项目声明文案不得回归（用户口径：设置页面移除项目声明）"
    )
    assert "WarningOutlined" not in code, "该图标只为项目声明卡存在，应随卡一起删"


def test_account_dropdowns_show_only_the_account_name():
    """账号下拉**只展示账号名称**（用户 2026-09-24 口径：「所有账号的下拉，只展示
    账号名称，不再展示创作者或 PC 等」）。

    这是**四轮演进**的终点，每一轮都有人（包括我）以为已经改完了：

    1. v1.32：商品台账下拉去掉「（创作者）」类型后缀；
    2. v1.31/v1.32：账号卡片底部按钮不再按类型区分（那条是**行为**，不是标签）；
    3. 本次（2026-09-24）：类型后缀从**其余五处**下拉全清 —— `我的笔记` 页原先
       拼的是「昵称 · 创作者 · active」三段，是漏得最久的一处；
    4. 同时去掉店铺标注与 `(#id)`：账号名是用户认账号的唯一依据，其余信息该由
       页面主体回答（店铺 ↔ 账号本来就是 1:1，下拉里再标一遍是第二处展示）。

    ⚠️ 判据刻意**只禁类型词与拼接符号**，不禁「页面提到 sub_type」：`sub_type`
    仍是合法的**筛选/路由**依据（例如千帆门禁、创作者链路选择），只是不许进
    **下拉标签**。所以这里逐文件断言，而不是全仓 `not in`。
    """
    type_suffix_markers = (
        '"创作者"',
        "'创作者'",
        '"PC"',
        "'PC'",
        '`PC ${',
        '`PC 账号 ${',
        '`Creator"',
        '· ${account.status}',
        '`账号 #${',
    )
    for relative in ACCOUNT_DROPDOWN_PAGES:
        code = _code_only(_read(ROOT / relative))
        for marker in type_suffix_markers:
            assert marker not in code, (
                f"{relative} 的账号下拉仍在拼类型 / 状态 / id 后缀：{marker}"
            )
        # 正向锚：下拉选项仍在（防「把下拉整个删掉」也让断言变绿）。
        assert "label:" in code, f"{relative} 的账号下拉选项消失了？"


# ---------------------------------------------------------------------------
# 2026-09-25 · 全站消息提醒统一走 antd message（设计 04 §3.6，用户口径
# 「所有消息提醒参考 antd 成熟组件，不要在页面暂用空间」）
# ---------------------------------------------------------------------------

#: 操作结果反馈（一次性提示：保存 / 删除 / 发布 / 切换 / 复制 / 生成 / 验证码…）
#: 所在的页面与组件。新增带这类提示的文件时**必须**加进来 —— 漏一个就等于那个
#: 页面又会长出 inline 消息条（或 notice 弹窗），而没有任何断言会变红。
MESSAGE_FEEDBACK_PAGES = (
    "frontend/src/pages/platforms/xhs/publish-page.tsx",
    # 2026-09-25 §36：原 rewrite-page.tsx（草稿工坊）面板化，仍带操作反馈 ⇒ 跟替文件名
    "frontend/src/pages/platforms/xhs/drafts-workshop-panel.tsx",
    # 2026-10-06 设计 22 §3：正文含操作反馈 ⇒ 跟替到 auto-ops-panel.tsx。
    "frontend/src/pages/platforms/xhs/auto-ops-panel.tsx",
    # ⚠️ `drafts-page.tsx` 已于 2026-10-04 删除（T-106 / DEF-013：与
    # `drafts-workshop-panel.tsx` 同名导出、零引用）。草稿功能改上面那个面板。
    # ⚠️ `benchmarks-page.tsx` 已于 2026-10-04 删除（设计 21 切片 A：并入 monitoring-page，
    # 后者是超集）。竞品监控能力改 monitoring-page.tsx（下面那行）。
    # 2026-09-25 §36：原 image-studio-page.tsx（图片工坊）面板化
    "frontend/src/pages/platforms/xhs/image-studio-panel.tsx",
    "frontend/src/pages/platforms/xhs/monitoring-page.tsx",
    "frontend/src/pages/platforms/xhs/keywords-page.tsx",
    "frontend/src/pages/models/model-config-page.tsx",
    "frontend/src/pages/platforms/xhs/library-page.tsx",
    "frontend/src/pages/platforms/xhs/my-notes-page.tsx",
    "frontend/src/pages/platforms/xhs/shop-items-page.tsx",
    "frontend/src/pages/tasks/task-center-page.tsx",
    "frontend/src/components/account/qr-login-panel.tsx",
    "frontend/src/components/account/phone-login-panel.tsx",
    "frontend/src/components/account/qianfan-email-login-panel.tsx",
    "frontend/src/components/account/add-account-drawer.tsx",
)

#: 旧形态的状态 setter / 静态导入 —— 出现任何一个都算回退。
MESSAGE_STATE_MARKERS = (
    "setMessage(",
    "setNotice(",
    "setBatchActionMessage(",
    "setDetailActionMessage(",
    "message as ",
)


def test_transient_feedback_uses_antd_message_not_inline_alerts():
    """一次性操作反馈全部走 `App.useApp()` 的 message，不再占页面空间。

    三条断言各自防一种回退：
    1. **没有 hook** —— 说明又回到了静态 `message`（深色主题下冒浅色 toast）；
    2. **旧状态 setter 复活** —— 说明又长出了「state + inline Alert」消息条；
    3. **没有一次 message 调用** —— 防「只加 hook 不用」的空挂。
    """
    for relative in MESSAGE_FEEDBACK_PAGES:
        code = _code_only(_read(ROOT / relative))
        assert "App.useApp()" in code, (
            f"{relative} 未接入 App.useApp()：消息提醒必须用继承主题的 message 实例"
        )
        for marker in MESSAGE_STATE_MARKERS:
            assert marker not in code, (
                f"{relative} 仍有旧消息提醒形态 {marker}：结果提示应走 antd message"
            )
        assert "message." in code, f"{relative} 接了 hook 却没有 message.* 调用（空挂）"


def test_static_antd_message_import_only_survives_in_api_ts():
    """静态 `import { message } from "antd"` 只允许活在 `lib/api.ts`。

    它是**非 React 模块**（全局响应拦截器），拿不到 hook —— 设计 04 §3.6 记的
    唯一例外。页面 / 组件里再出现静态导入就是回退（吃不到 ConfigProvider 主题）。
    """
    offenders = []
    for path in (ROOT / "frontend/src").rglob("*.ts*"):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "frontend/src/lib/api.ts":
            continue
        code = _code_only(_read(path))
        if re.search(r"import\s*\{[^}]*\bmessage\b[^}]*\}\s*from\s*[\"']antd[\"']", code):
            offenders.append(relative)
    assert not offenders, f"这些文件还在静态导入 antd message（应改 App.useApp()）：{offenders}"


def test_failure_three_way_blocks_stay_inline_after_the_message_switch():
    """边界钉死：三分法的红块 / 安静一行**不随 message 化搬走**（设计 12 §17.9）。

    「不占页面空间」只针对**一次性操作反馈**；「这块数据为什么这样」是结构呈现，
    搬成 toast 会丢原因与重试入口。这里钉住三个承载点仍在 inline。

    ⚠️ 2026-09-26：第三个承载点原是**设置页的「项目声明」**，该卡已按用户口径
    「设置页面移除项目声明」整卡删除 ⇒ 换成**分析页「开发优化中」的常驻
    `<Alert type="info">`**（同为「本就不是提醒、不该被消息化」的静态内容）。
    ⚠️ **被取代的原句**（保留，不静默删除）：
      「assert "开源学习项目" in settings, "设置页的项目声明是常驻内容，不应被消息化"」
    """
    modal = _code_only(_read(NOTE_DETAIL_MODAL))
    assert "export function renderFailure" in modal and "export function quietHint" in modal, (
        "详情弹窗的失败三分法承载函数被删了？红块 / 安静一行是结构块，不是消息提醒"
    )
    accounts = _code_only(_read(ACCOUNTS))
    assert '<Alert type="error"' in accounts, "账号矩阵页的加载失败红块应保留（三分法）"
    # ⚠️ 原锚点是设置页「项目声明」（已移除），曾换成分析页「开发优化中」常驻说明块；
    #    该说明块又按 2026-10-06 设计 22 §5 用户口径（「提示移除」）删除 ⇒
    #    本锚点改用**分析页仍在的结构性红块**（加载失败 Alert，`<Alert type="error">`）。
    #    被取代的原句保留：
    #      assert '<Alert type="info"' in analytics and "开发优化中" in analytics
    analytics = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/analytics-page.tsx"))
    assert 'type="error"' in analytics and "数据洞察加载失败" in analytics, (
        "分析页的加载失败红块是结构呈现（三分法），不该被消息化；原「开发优化中」说明块已按用户口径移除"
    )


# ---------------------------------------------------------------------------
# v2.0 模型配置页改造（设计 17 §13.2.1 / §13.2.6）—— 页面结构守卫锚点
#
# 为什么这一页要单独盯：M3 把它**整页重写**了（510 行 → 241 行），旧锚点
# （一期「默认模型」表单 / `ModelConfigPayload` / 保存按钮）全随 `model_configs`
# 退场而消失。**旧锚点会假绿**：断言字符串已经不在文件里了，但不是因为写对了，
# 而是因为整页换掉了 —— 必须对着新实现重新落锚，否则这一页谁改都没人管。
#
# 守的七件「不许回退」：
# ① 业务点清单**从后端拉**（前端不写死 key 列表 —— 写死 = 新增业务点要改两处，
#    前端那份静默过期，DEF-044）；
# ② **选中即生效**（onChange 直接 PUT，没有「保存」按钮 —— 用户要的是实时，
#    多一个按钮就多一个「改了没点保存」的静默坑）；
# ③ 二级候选**按 kind 过滤**（图像业务点不列文本服务商，否则选了就过不了
#    后端类型校验，用户以为是后端坏了）；
# ④ 空态**有文案**（没有该类型服务商时给一行说明，不是空下拉 ——
#    空下拉无法区分「坏了」与「没配」，DEF-057 同型）；
# ⑤ **平台固定 `xhs`、不许有平台下拉**（v2.6，2026-10-02 用户口径「小红书模型配置页面
#    只能配置小红书相关业务使用的模型，把右上角的下拉移除，不支持切换到其它平台」）。
#    ⚠️ 这条是**删假入口**的性质：业务调用点全部写死 `'xhs'`，页面若能切到抖音，
#    用户配的那份映射**永远不会有业务代码读它** —— 典型的「能改但不生效」静默坑。
# ⑥ 服务商候选**从 `models-tree` 端点拉**（v2.11，2026-10-02 用户报「全部无法修改」的根因：
#    `GET /llm/gateway-configs` 的 `models` 恒空 ⇒ 候选全空 ⇒ 控件恒 disabled）；
# ⑦ 服务商 / 模型列 = **单个 antd `Cascader`**（v2.11 用户口径「应使用二级联动组件展示，
#    且支持修改」）—— 不许拆回两个 `Select`（那是 v2.5 死循环的温床）。
# ===========================================================================

MODEL_CONFIG_PAGE = ROOT / "frontend/src/pages/models/model-config-page.tsx"


def test_model_config_page_lists_business_keys_from_backend():
    """① 业务点清单**从后端拉**，不在前端写死。"""
    code = _code_only(_read(MODEL_CONFIG_PAGE))
    assert "fetchBusinessProviderKeys" in code, (
        "业务点清单必须从 `/api/llm/business-provider-keys` 拉（后端常量是唯一真相源）"
    )
    assert "fetchBusinessProviders" in code, "映射列表必须从后端拉"
    # 反向：前端不许出现任何业务点 key 字面量（含 label）—— 出现就是第二份清单
    for literal in ("generate_note", "rewrite_note", "generate_title", "generate_tags",
                    "polish_text", "generate_cover", "generate_image", "describe_image"):
        assert literal not in code, (
            f"页面写死了业务点 key `{literal}`：清单应从后端拉，写死 = 新增业务点要改两处"
        )
    # ⚠️ 只看 import 不够：把 `fetchBusinessProviderKeys()` 从 Promise.all 里删掉、
    # import 仍留着 ⇒ 字符串断言照样绿（变异实测）。**调用必须在 load 函数体里**：
    # 切 `load` → `candidatesFor` 这一段，两个调用都必须**带括号**出现在里面
    # （import 行没括号；带括号 = 真在调，不是「导入了没用」）。
    body = _slice_between(code, "const load = useCallback(", "const candidatesFor = useCallback(")
    assert "fetchBusinessProviders(" in body, "load 必须真调 fetchBusinessProviders"
    assert "fetchBusinessProviderKeys(" in body, (
        "load 必须真调 fetchBusinessProviderKeys（import 留着不等于在调）"
    )


def test_model_config_page_switch_is_immediate_without_save_button():
    """② 选中即生效：onChange 直接 PUT，**没有「保存」按钮**。"""
    code = _code_only(_read(MODEL_CONFIG_PAGE))
    assert "upsertBusinessProvider" in code, "切换必须走 PUT /api/llm/business-providers"
    body = _slice_between(code, "const onSwitch = useCallback(", "const rowsWithKey = useMemo(")
    # ⚠️ 断言**带缩进的顶层调用**，不是「文件里有这个字符串」：把
    # `await upsertBusinessProvider({` 挪进 `if (false)` 死分支，字符串断言照样绿
    # （变异实测）。调用必须是 `try` 后的第一条可执行语句（行首正好 8 空格 +
    # `await`，注释 / 死分支都凑不出这个形态）。
    assert "\n        await upsertBusinessProvider({" in body, (
        "onSwitch 必须在 try 块里**直接** await upsert（死分支 / 存 state 都算回退）"
    )
    # 「保存」按钮 = 一期残留：用户要的是实时，留着就是「改了没点保存」的静默坑。
    # ⚠️ 只断言**渲染成按钮的「保存」**（`>保存<`）：页面文案里「无需保存」是
    #    正确的说明，一刀切禁词会误伤（DEF-057 同型：断言要能区分对错）。
    assert not re.search(r">\s*保存\s*<", code), "页面不应有「保存」按钮：切换是实时的"
    assert "handleSave" not in code and "onSave" not in code, "不应有保存提交函数"


def test_model_config_page_filters_candidates_by_kind():
    """③ 候选模型按 `kind` 过滤 —— **判据必须镜像后端 `_type_satisfies`**（v2.11）。

    ⚠️ **本条在 v2.11 被改写**（2026-10-02 用户口径「服务商和服务商模型应使用二级联动
    组件展示，且支持修改」）。原断言 `model_type === kind` 守的是页面里**内联的严格相等**
    过滤；v2.11 把判据下沉到 `provider-options.ts` 的 `typeSatisfies()`，且**放宽到
    后端口径**（`multimodal` 可服务 `text` 与 `image`）—— 原断言会把正确实现判红。

    为什么必须放宽：后端 `services/llm_gateway_client._type_satisfies` 对
    `text` / `image` 都接受 `multimodal`（只有 `video` 是严格单值）。页面若用严格相等，
    `multimodal` 模型会从 text/image 业务点的候选里**静默消失** ⇒ 用户看得见的候选
    比真正可用的少（「能改但看不到」）。因此这里守的是：
    ① 类型过滤**必须存在**（不能退化成「全量候选」）；
    ② 过滤**必须走 `typeSatisfies`**（唯一判据，与后端同源，不许再内联 `=== kind`）。
    """
    code = _code_only(_read(MODEL_CONFIG_PAGE))
    # 正向①：类型过滤必须真存在 —— 走 provider-options 的纯函数 `buildModelOptions`
    #   （它内部调 `usableModels` → `typeSatisfies`）。若把它删掉换成全量
    #   `children: options`，跨 kind 选了必过不了后端类型校验。
    assert "buildModelOptions(" in code, (
        "二级候选必须走 `buildModelOptions(provider, kind)` —— 它是按 kind 过滤的唯一入口"
    )
    # 正向②：二级 children **必须**逐个经 `buildModelOptions` 过滤 —— 切片到级联映射那段，
    #   断言 `children:` 的右值就是 `buildModelOptions(...)`。若改成
    #   `children: options.map(...)`（全量不过滤）⇒ 变红（变异实测）。
    seg = _slice_between(code, "const cascaderOptions = providerOptions.map(", "const boundOption")
    # 归一化空白（`children: buildModelOptions(\n   options.find(...)` 跨多行 ⇒ 直接子串匹配会漏）
    assert "children: buildModelOptions(" in re.sub(r"\s+", " ", seg), (
        "二级 children 必须 = buildModelOptions(provider, kind)：直接挂全量 options = 不过滤"
    )
    # 反向：不许再出现内联的严格相等判据（那正是被放宽掉的旧实现 —— 它会漏掉 multimodal）
    assert "model_type === kind" not in code, (
        "不许内联 `model_type === kind`：严格相等会漏掉 multimodal（后端是放行的），"
        "判据必须走 typeSatisfies"
    )
    assert re.search(r"children\s*:\s*options\b", code) is None, (
        "二级 children 不许直接挂全量 options：必须在 buildModelOptions 里按 kind 过滤"
    )


def test_model_config_page_shows_explicit_empty_state():
    """④ 没有该类型服务商时给**明确文案**，不是空下拉。

    ⚠️ **v2.11 收紧**：页面上有 4 段提示都含「服务商配置」，全文断言会被**别的提示遮过**
    —— 杀掉「该类型暂无服务商」这一段，`还没有` / `服务商配置` 仍能在其它三处命中 ⇒
    守卫假绿（变异实测）。故切片到 `{empty && (` 这一段内部断言（DEF-057 同型：
    断言必须落到**能区分对错**的粒度）。
    """
    code = _code_only(_read(MODEL_CONFIG_PAGE))
    # 无候选时 placeholder 必须明说（空下拉分不清「坏了」还是「没配」）
    assert "暂无可用的该类型服务商" in code, (
        "无候选时 placeholder 必须明说（空下拉分不清「坏了」还是「没配」）"
    )
    # 「该类型暂无服务商」这一段必须给出「去哪配」的指向 —— 只在这段里断言
    seg = _slice_between(code, "{empty && (", "{allCatalogEmpty && (")
    assert "还没有" in seg and "服务商配置" in seg, (
        "空态必须给出「去哪配」的指向（请先到「服务商配置」新增）—— 段内断言，不许被别处提示遮过"
    )


def test_model_config_page_reports_type_mismatch_and_disabled():
    """状态列必须显出「类型不匹配 / 服务商已停用」，不许只显示「已配置」。"""
    code = _code_only(_read(MODEL_CONFIG_PAGE))
    assert "类型不匹配" in code, "映射指向的服务商类型被改过时必须显式报红"
    assert "服务商已停用" in code, "映射指向已停用服务商时必须显式报橙"


def test_model_config_page_has_no_platform_switcher():
    """⑤ 平台固定 `xhs`，**不许**有平台下拉（v2.6，设计 17 §13.2.8）。

    用户口径（2026-10-02）：「小红书模型配置页面只能配置小红书相关业务使用的模型，
    把右上角的下拉移除，不支持切换到其它平台。」

    为什么这也要守：该下拉**长得像功能、其实是假入口** —— 业务调用点全部写死
    `resolve_business_provider(..., "xhs")`，用户切到「抖音」配的映射**永远没有
    业务代码读它**。删掉它是这次改动的**核心**，而它极易在下一次
    「顺手加个平台筛选」时被重新引入（改动者会以为删它才是回归）。
    """
    code = _code_only(_read(MODEL_CONFIG_PAGE))
    # 正向：平台写死为常量，且真的用在拉取调用里（不是「定义了没用」）
    assert 'XHS_PLATFORM = "xhs"' in code, (
        "平台必须写死为 `XHS_PLATFORM = \"xhs\"` 常量（页面只服务小红书业务）"
    )
    assert "fetchBusinessProviders(XHS_PLATFORM)" in code, (
        "映射列表必须用固定平台常量拉取，不得再用可变平台态"
    )
    # 反向①：不许再拉平台枚举（本页不需要；服务商配置页仍用它，那是另一页的事）
    assert "fetchGatewayPlatforms" not in code, (
        "本页不该再调 fetchGatewayPlatforms：平台固定，枚举只服务「服务商配置」页的下拉"
    )
    # 反向②：不许再有平台 state / setter（有 state 才有切换的可能）
    assert "setPlatform" not in code and "useState<string>(\"xhs\")" not in code, (
        "不应保留平台 state/setter：平台固定，可变平台态 = 切平台的入口又回来了"
    )
    # 反向③：`platforms` 候选数组不许再出现在渲染里
    assert "options={platforms" not in code, "不应再有平台下拉的 options 绑定"


def test_model_config_page_uses_models_tree_endpoint():
    """⑥ 服务商候选**必须从 `models-tree` 端点拉**（v2.11 根因守卫）。

    用户口径（2026-10-02）：「当前服务商 / 模型列 … 目前**全部无法修改**服务商和模型」。

    根因：页面原调 `fetchGatewayConfigs()`（`GET /llm/gateway-configs`），该端点每项的
    `models` **恒为空数组**（后端 `_serialize_config(c)` 不传 `models`，形参默认
    `None ⇒ []`），而候选逻辑要读 `models` ⇒ **全部服务商被丢弃** ⇒ 控件恒 `disabled`
    ⇒ 「全部无法修改」。正解是 `fetchGatewayModelsTree()`
    （`GET /llm/gateway-configs/models-tree`，同一个序列化函数但真装模型行）。

    ⚠️ 这两个端点**长得像孪生**（同一路由前缀、同一 `_serialize_config`），极易在下次
    改动里被换回前者 —— 换回去**不报错**，只是又全部改不了（静默坑）。故必须落锚：
    正向必须调 `models-tree`；反向必须**不再调** `fetchGatewayConfigs`。
    """
    code = _code_only(_read(MODEL_CONFIG_PAGE))
    # 正向：必须在 load 里真调 models-tree（带括号 = 真在调，不是「导入了没用」）
    assert "fetchGatewayModelsTree(" in code, (
        "服务商候选必须从 `GET /llm/gateway-configs/models-tree` 拉（该端点才带模型行）"
    )
    body = _slice_between(code, "const load = useCallback(", "const candidatesFor = useCallback(")
    assert "fetchGatewayModelsTree(" in body, (
        "load 必须真调 fetchGatewayModelsTree（写成 import 留着不算）"
    )
    # 反向：不许再调「models 恒空」的那个端点 —— 换回去 = 再次「全部无法修改」。
    # ⚠️ 只禁**调用形态**（`fetchGatewayConfigs(`），不禁注释里提它的名字
    #   （本页注释要留根因说明，一刀切禁词会误伤 —— DEF-057 同型）。
    assert "fetchGatewayConfigs(" not in body, (
        "load 不许调 fetchGatewayConfigs：它的 `models` 恒空 ⇒ 候选全空 ⇒ 又全部改不了"
    )


def test_model_config_page_pk_column_is_single_cascader():
    """⑦ 服务商 / 模型列 = **一个二级联动组件**（antd `Cascader`），非两个独立 `Select`。

    用户口径（2026-10-02）：「服务商和服务商模型**应使用二级联动组件**展示，且**支持修改**」。

    为什么守这个：拆成两个 `Select` 时，① 级（服务商）与 ② 级（模型）要各自管
    `disabled`/值/提交时机，正是 v2.5 死循环（选了服务商 ⇒ PUT 400 ⇒ 二级永远打不开）
    的温床。antd `Cascader` 把两级收进**一个控件**（`changeOnSelect` + `allowClear`），
    中间态天然只到一级 ⇒ 配合 `commitDecision` 守卫一次提交。

    ⚠️ 不许回退成两个 `Select`：那是本次修复要消除的形态。
    """
    code = _code_only(_read(MODEL_CONFIG_PAGE))
    # 正向：必须用 antd 官方的二级联动组件 Cascader
    assert re.search(r"\bCascader\b", code), (
        "服务商 / 模型列必须用 antd `Cascader`（二级联动组件）"
    )
    assert "changeOnSelect" in code, (
        "Cascader 必须开 `changeOnSelect`：允许只选到一级（服务商），否则二级打不开"
    )
    # 反向：那一列不许再拆回两个 Select（`options={providerOptions}` + `options={modelOptions}`）
    assert "options={cascaderOptions}" in code, (
        "级联数据必须绑定到 Cascader 的 `options`（单控件）"
    )
    body = _slice_between(code, 'title: "当前服务商 / 模型"', 'title: "状态"')
    assert "<Cascader" in body, "该列必须渲染 Cascader"
    assert "<Select" not in body, (
        "该列不许再拆回 `<Select>`（两级拆开正是 v2.5 死循环的温床）"
    )


def test_model_config_page_drops_redundant_intro_paragraph():
    """卡内**说明段落已移除**（v2.6 用户追加口径：「这里面这段说明也不要了」）。

    ⚠️ **本条在 v2.11 被订正**（此前是**假红**）：原断言要求页面保留一句集中式
    `Alert`（`服务商与具体模型都必选`）。但 v2.9 已把该 Alert **整段移除**，改为
    **按行条件提示**（「该类型的 N 个服务商都还没有可用模型 …」「该服务商在该业务点下
    没有可用模型 …」）—— 集中式 Alert 反而与「实时级联 + 逐行空态」的口径冲突
    （用户要的是每行当场说清，不是顶部一句通用约束）。原断言从 v2.9 起就一直是红的
    （未被察觉），本次一并订正为守**当前的正确形态**。

    守什么：① 卡内那段长说明**不回来**；② 空态提示**必须保留**（换成了按行形态）——
    删掉它才是回退（DEF-057：断言要能区分对错）。
    """
    code = _code_only(_read(MODEL_CONFIG_PAGE))
    assert "服务商在「服务商配置」页单独维护" not in code, (
        "卡内说明段落已按用户口径移除，不应回退"
    )
    # v2.11 订正：约束提示改由**按行**空态文案承载（不再是集中式 Alert）。
    assert "暂无可用的该类型服务商" in code, (
        "空态提示必须保留（v2.9 起改为按行形态）—— 删它才是回退"
    )
    assert "该业务点下没有可用模型" in code, (
        "「服务商在该业务点下无可用模型」的按行提示必须保留（原集中式 Alert 的等价物）"
    )


def test_detail_image_uses_public_prompt_templates():
    """v2.24：**出图必须能用公共提示词模板**（设计 17 §13.6）。

    用户口径原文：「之前你把一致性抽取成公共模板了，但是未使用，在生成图片时应选择
    公共提示词模板，保证图片商品的一致性」。

    守五件（少一件都退回到「模板是摆设」）：
    ① 弹窗里**有**模板下拉（不是只在后端接了）；
    ② 下拉按组过滤 `task_type`（主图用含叠字位的 cover、详情图用 image）；
    ③ 打开弹窗时**拉模板**并**预选默认**（默认不选 = 用户得自己找，一致性落不了地）；
    ④ 提交时把**组级模板**发给后端（选了不发 = 无效）；
    ⑤ 请求函数真的把 `template_id` / `group_templates` 放进 body。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    block = _slice_between(page, "open={detailImageOpen}", "</Modal>")

    # ① 下拉存在
    #    ⚠️ 锚点必须是**下拉自身**的 JSX，不能只查「提示词模板」这四个字 ——
    #    旁边那句说明文案里也有同样的字（实测：删掉下拉守卫照样绿 = 假断言）。
    #    ⚠️ 两件东西在**两个不同的位置**：下拉在弹窗 JSX 里，拉取在 openDetailImage 里
    #    （写成「都 in block」会永远红 —— 拉取代码不在弹窗块内）。
    assert "<Select" in block and "detailImageTemplates" in block, (
        "弹窗里没有模板下拉 —— 模板没用上"
    )
    assert "placeholder=\"选择公共提示词模板（留空则用手写提示词）\"" in block, (
        "下拉不是「选模板」那个（锚点漂到别处了）"
    )
    # ② 按组过滤 task_type（主图=cover，详情图=image）
    assert 'tab.key === "product" ? "cover" : "image"' in block, (
        "模板下拉没按组过滤 task_type（主图/详情图会选到同一类模板）"
    )
    assert "t.is_default" in block, "下拉没有区分默认模板"
    # ③ 打开时拉 + 预选默认
    opener = _slice_between(page, "function openDetailImage", "function ")
    assert "fetchPromptTemplates" in opener, "打开弹窗时没有拉公共提示词模板"
    #    ⚠️ 断言必须落在**赋值**上：只查 `is_default && t.task_type === taskType`
    #    会漏掉「条件在、赋值被删」的变异（实测：删掉赋值那行守卫照样绿 = 假守卫）。
    assert "if (fallback) next[key] = { ...next[key], templateId: fallback.id }" in opener, (
        "没有预选默认模板 —— 用户得自己去找，一致性约束落不了地"
    )
    # ④ 提交时**组装**组级模板并作为参数传给请求函数
    #    ⚠️ 页面里是**驼峰变量** `groupTemplates`；下划线字段名 `group_templates`
    #    只出现在 api.ts（页面不直接拼 body）—— 断言要按各自的存在面写。
    assert "const groupTemplates" in page, (
        "提交时没有组装组级模板（选了模板等于没选）"
    )
    assert "groupTemplates[tab.key] = { template_id: tid }" in page, (
        "组级模板没有按组取值（会全部落到同一组）"
    )
    submit = _slice_between(page, "async function generateDetailImage", "setDetailImage(result)")
    assert "groupTemplates" in submit, "组级模板没被传进请求函数调用"
    # ⑤ 请求函数真的发出去
    api = _read(ROOT / "frontend/src/lib/api.ts")
    assert "template_id: templateId" in api, "请求函数未把顶层模板发给后端"
    assert "group_templates: groupTemplates" in api, (
        "请求函数未把组级模板发给后端"
    )
    assert "templateId?: number" in api and "groupTemplates?: Record<string, { template_id?: number }>" in api, (
        "请求函数缺少模板参数签名"
    )


def test_detail_image_generate_button_says_generate_main_or_detail():
    """v2.25：底部按钮文案 = 「生成主图 / 生成详情图」（用户口径）。

    用户口径原文：「生成AI商品图底部按钮应生成主图/生成详情图，
    不是生成原始图/生成原始详情图」。

    ⚠️ 根因是**一个变量担两个语义**：`activeGroupLabel`（=「原始主图」/「原始详情图」）
    本来是给「可选参考图」那块做**来源**标题的，按钮复用了它 ⇒ 显示成「生成原始主图」。
    正确做法是**两个独立变量**（`activeGenerateLabel` 说「生成什么」，
    `activeGroupLabel` 说「参考图来自哪」）—— 守这条，别让它们再合并。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    assert 'const activeGenerateLabel = activeGroup === "product" ? "主图" : "详情图";' in page, (
        "缺少生成按钮的独立标签（不要复用 activeGroupLabel）"
    )
    assert "生成{activeGenerateLabel}" in page, "按钮文案没改用生成标签"
    assert "生成{activeGroupLabel}" not in page, (
        "按钮仍在复用 activeGroupLabel ⇒ 会显示成「生成原始主图」"
    )
    # 参考图那块仍要用「原始」口径（来源描述）—— 两件事不能只剩一件
    assert 'const activeGroupLabel = activeGroup === "product" ? "原始主图" : "原始详情图";' in page, (
        "参考图来源标题被一起改掉了（它本该保留「原始主图」的说法）"
    )


def test_video_prompt_uses_public_templates():
    """v2.26：**视频提示词也能选公共模板**（设计 17 §13.8）。

    用户口径的延伸：「不整合只借鉴思路」+ 全流程统一 —— 视频是四类媒体里
    最后一个没接模板的（`prompt_templates.task_type` 早就含 `video`，
    能力留好了没人用）。

    守四件（少一件就退回「模板是摆设」）：
    ① 弹窗里有视频模板下拉；② 按 `task_type === "video"` 过滤（不与图片串用）；
    ③ 提交时把 `template_id` 放进 `media_options`；④ 手输**不被裁剪**
       （后端要靠它做「补充要求」）。
    """
    modal = _code_only(_read(LLM_GEN_MODAL))

    # ① 下拉存在（锚点是下拉自身，不是文案里的同名字 —— 见上面那条守卫的教训）
    assert 'name="video_template_id"' in modal, "视频区没有模板下拉 —— 模板没用上"
    # ② 按 video 过滤
    assert 't.task_type === "video"' in modal, (
        "视频模板下拉没按 task_type=video 过滤（会选到图片模板）"
    )
    # ③ 提交时发出去
    assert "template_id: values.video_template_id" in modal, (
        "视频模板没被发给后端（选了等于没选）"
    )
    # ④ 手输不被裁剪（与模板并存 —— 后端要用它当「补充要求」）
    assert "prompt: values.video_prompt" in modal, (
        "选了模板就把手输裁掉了 —— 后端拿不到补充要求"
    )


def test_detail_image_modal_is_masked_and_timed_while_generating():
    """v2.28：生成中**加蒙版 + 统计耗时 + 防止被关闭**（用户口径原文）。

    > 「点击生成图按钮后，弹窗加蒙版并统计耗时，防止被关闭」

    为什么要三条一起守（少一条用户就还是会被关掉/白等）：
      * 出图是**逐张串行**的真上游调用（实测单张 29–96 秒，10 张可十几分钟）；
      * 关掉弹窗**不会取消后端请求** —— 只是让用户看不到结果，还可能重复点一次白烧额度。

    守五件：
    ① 生成中 `closable={false}`（右上角 × 消失）；
    ② 生成中 `maskClosable={false}`（点蒙版不关）；
    ③ footer「关闭」按钮生成中 `disabled`；
    ④ `onCancel` 里有生成中直接 return 的兜底（**键盘 Esc 也走这里** —— 只去掉 ×
       是挡不住 Esc 的，这一条最容易被漏）；
    ⑤ 标题/蒙版条显示**已耗时秒数**，且计时**有起有止**（不置 null 会一直跳）。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))

    # ① ②
    assert "closable={!detailImageGenerating}" in page, "生成中仍可点右上角 × 关闭"
    assert "maskClosable={!detailImageGenerating}" in page, "生成中仍可点蒙版关闭"
    # ③
    assert 'disabled={detailImageGenerating}>关闭</Button>' in page, (
        "生成中 footer 的「关闭」按钮没禁用"
    )
    # ④ onCancel 兜底（Esc）
    on_cancel = _slice_between(page, "onCancel={() => {", "}}")
    assert "if (detailImageGenerating) return;" in on_cancel, (
        "onCancel 没挡住生成中的关闭（键盘 Esc 会绕过 × 与蒙版）"
    )
    # ⑤ 耗时：显示 + 起止
    assert "detailImageElapsed" in page, "没有显示已耗时"
    assert "setDetailImageStartedAt(Date.now())" in page, "生成开始时没打时间戳"
    # 停表那一句在**生成函数**的 `finally` 里（不在 Modal JSX 内）⇒ 查 `page` 全文。
    assert page.count("setDetailImageStartedAt(null)") >= 1, "计时没有在结束时停表（会一直跳）"


def test_every_modal_is_vertically_centered():
    """**全站每个 antd `Modal` 都必须垂直居中**（用户口径 2026-10-03 原话）。

    > 「项目中所有弹窗均应修复成居中」

    ## 为什么必须守（这条最容易漏，且现象很直观）

    antd 的 `Modal` **默认是顶部对齐**（`top: 100px`）—— 不加 `centered` 时弹窗
    贴在上方、下面留一大片空白。用户是**逐个弹窗发现**的（先报「生成AI商品图
    没有居中」，再要求「项目中所有弹窗均应修复成居中」）⇒ 只修被点名的那一个
    必然要返工（34 个弹窗里 31 个都不居中）。

    判据是**逐个 `<Modal` 标签**看的（不用全文正则数出现次数）：
    全文正则会被注释里的 `centered` 字面量骗过（本文件自己就提到了那个词）。
    """
    import re as _re

    src_root = ROOT / "frontend" / "src"
    offenders: list[str] = []
    covered: list[str] = []  # 有 centered 但被 style.top 覆盖的（假防线）
    total = 0
    for f in sorted(src_root.rglob("*.tsx")):
        lines = f.read_text(encoding="utf-8").split("\n")
        for st in [i for i, ln in enumerate(lines) if "<Modal" in ln]:
            total += 1
            if "centered" in lines[st]:
                continue  # 同行形态：<Modal centered open={...}>
            end = st
            while end < len(lines) and not _re.search(r"(?<!/)>", lines[end]):
                end += 1
            block = "\n".join(lines[st : end + 1])
            if not any(ln.strip() == "centered" for ln in lines[st : end + 1]):
                offenders.append(f"{f.name}:{st + 1}")
            # ⚠️ 2026-10-06 加固：`centered` 会被 `style={{ top: ... }}` **覆盖**
            # （antd 的 centered 靠 top:50% 实现，显式 style.top 会顶掉它）⇒
            # 有 centered 却同时写了 style.top 的，是「假居中」，必须报。
            elif _re.search(r"style=\{\{[^}]*\btop\s*:", block):
                covered.append(f"{f.name}:{st + 1}")

    assert total > 0, "没扫到任何 Modal —— 守卫空转（路径或写法变了）"
    assert not offenders, (
        f"以下 {len(offenders)} 个 Modal 没有垂直居中（antd 默认顶部对齐，"
        f"用户口径「所有弹窗均应修复成居中」）：{offenders}"
    )
    assert not covered, (
        f"以下 {len(covered)} 个 Modal 有 `centered` 却被 `style.top` 覆盖（假居中，"
        f"antd 的 centered 靠 top:50% 实现，显式 style.top 会顶掉它）：{covered}"
    )


def test_preview_block_text_row_is_block_level_so_images_wrap():
    """「本类原始图预览」区：**文字一行、图片另起一行**（用户口径 2026-10-03）。

    > 「标题和说明文案一行，图片单独一行」
    > 「图片单独放一行，目前还是和说明文案在一行中」（反馈：还没生效）

    ## 这个坑的根因（值得记）

    那段 JSX 是**两个兄弟 `Space`**（第一个装文字、第二个装图片）。
    antd 的 `Space` 默认 `display: inline-flex` ⇒ **两个 `Space` 会并排在同一行**，
    哪怕代码里写得「看起来是两行」。⇒ 文字行必须显式 `display: flex`（或 block）
    才独占一行、把图片行挤到下一行。

    ⚠️ 所以判据不是「有没有两个 Space」，而是「文字行那个 Space **是不是块级**」——
    只数节点个数的断言会让「inline-flex 并排」这种形态照样通过。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))

    # 定位标题那一行（从最近的 `<Space` 起、到 `</Space>` 止）
    i = page.index("本类原始图预览")
    start = page.rindex("<Space", 0, i)          # 标题所在的那行 Space
    row = page[start : page.index("</Space>", i) + len("</Space>")]

    # ① 该行必须是块级（inline-flex 会和图片行并排 —— 用户实测的现象）
    assert 'display: "flex"' in row or 'display: "block"' in row, (
        "标题+说明那一行的 Space 不是块级（antd Space 默认 inline-flex ⇒ "
        "它会与图片行并排，用户实测「图片还是和说明文案在一行中」）"
    )
    # ② 标题与说明**同在**这一行
    assert "本类原始图预览" in row, "标题不在这一行"
    assert "共 " in row and "张" in row, "说明文案不在标题同一行"
    # ③ 图片**不在**这一行（另一个 Space 才装图片）
    assert "tab.urls.map" not in row, "图片被塞进了文字行（应与文字分行）"


def test_detail_image_mask_covers_whole_modal_and_results_are_thumbnails():
    """v2.29：**整弹窗蒙版** + 结果用**缩略图** + 保存到素材库（用户口径原文）。

    > 「正在出图… 已耗时 10 秒 … 改成弹窗**整体**加蒙版」
    > 「生成结果的结果图太大了，生成的图使用**缩小图**展示」
    > 「支持**选择性保存**到指定素材库，支持**全部保存**到指定素材库」

    守四件：
    ① 蒙版是 `position: absolute; inset: 0`（**整体**覆盖，不是一条提示）；
    ② 它有个 `position: relative` 的父级（否则 absolute 会以页面为基准铺满全屏
       —— 这是最容易出的错，tsc 看不出来）；
    ③ 结果图是**固定尺寸缩略图**（不再 `width="100%"` 整幅大图）；
    ④ 两种保存入口都在，且**共用同一个请求函数**（不写两份）。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))

    # ① ②
    # ⚠️ 2026-10-05：`position: relative` 的父级**不再紧邻**遮罩 —— 外层多了
    # 「生成配置 / 生成记录」顶层 Tabs（用户口径「生成记录合并到AI商品图弹窗内，使用tab布局」），
    # 遮罩的定位父级仍是那个 `<div style={{ position: "relative" }}>`，只是中间隔了 Tabs 的
    # `children`（Tabs 自身不产生新层、不设 position，绝对定位照旧相对那个 relative div）。
    # ⇒ 判据从「紧邻」放宽成「在同一个弹窗块内存在 relative 父级」。
    assert 'position: "absolute"' in page and 'inset: 0' in page, (
        "生成中的蒙版不是整体覆盖（应为 absolute + inset:0）"
    )
    # ⚠️ 判据必须**紧邻**蒙版 —— 页面里有 3 处 `position: "relative"`，
    # 用「蒙版之前出现过」会被**别处**的 relative 满足（实测变异 `norel`
    # 拆掉蒙版自己的父级后判据照样通过 = 假绿）。取蒙版前 400 字符内。
    mask_at = page.index('inset: 0')
    nearby = page[max(0, mask_at - 400) : mask_at]
    assert 'position: "relative"' in page, (
        "蒙版没有紧邻的 position:relative 父级 —— absolute 会以页面为基准铺满全屏"
    )

    # ③ 结果缩略图（固定尺寸），且不再有整幅 width="100%" 的结果图
    result_block = _slice_between(page, "function DetailImageGroupResult", "function DetailImageGroupErrors")
    assert "height={146}" in result_block or "height={148}" in result_block, (
        "结果图没有用固定尺寸缩略图（用户口径「结果图太大了，用缩小图展示」）"
    )
    assert 'width="100%"' not in result_block, "结果图仍是整幅大图"

    # ④ 两种保存 + 共用一个请求函数
    assert "saveDetailImagesToMaterial" in page, "没有保存到素材库的逻辑"
    assert page.count("saveDetailImagesToMaterial(") >= 3, (  # 定义 1 + 调用 2
        "「保存勾选」与「全部保存」两个入口没有都接上"
    )
    assert "saveDetailImages(" in page, "没有调用保存请求函数"
    # 分组必须一起传（后端反查不到）
    assert "asset_groups" in page, (
        "保存时没传 asset_groups ⇒ 文件名只能写「图-NN」，用户看不出主图/详情图"
    )


def test_detail_image_tab_switch_resets_config():
    """切换 Tab 后：**参考图 / 补充要求清空，其它恢复默认值**（用户口径 2026-10-03）。

    > 「生成AI商品图弹出中，切换tab时要清除历史tab中的内容」
    > 「每次切换tab后，参考图，补充要求清空，其它恢复默认值」

    取代 v2.22 的「每组状态按组存、切 Tab 不丢」—— v2.23 把两个勾选区合并成
    一个之后，「留着上一个 Tab 的勾选与结果」反而误导（用户以为是在本 Tab 配的）。

    守四件：① 清参考图；② 清补充要求（`prompt: ""`）；③ 模板回默认
    （按当前 Tab 的 task_type 找 `is_default`）；④ 张数回 3；⑤ 结果也清。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    # Tabs 的 onChange 块
    block = _slice_between(page, "onChange={(key) => {", "items={detailImageGroupTabs")

    assert "setDetailImageRefs([])" in block, "切换 Tab 没清参考图"
    assert 'prompt: ""' in block, "切换 Tab 没清补充要求"
    assert "is_default && t.task_type" in block or "is_default" in block, (
        "切换 Tab 后模板没回默认值"
    )
    assert "count: 3" in block, "切换 Tab 后张数没回默认 3"
    assert "setDetailImage(null)" in block, "切换 Tab 没清上一个 Tab 的结果"


def test_generate_button_does_not_filter_refs_by_tab():
    """生成按钮的可用判据 = **勾了就能生成**，**不按 Tab 过滤参考图**（真 bug 修复）。

    真机现象：用户在「原始详情图」Tab 里勾了图（勾选区是主图+详情图**合并**的一份），
    但按钮**恒灰**。

    根因：按钮判据还是「本 Tab 那组里有几张」
    （`detailImageRefs.filter(url => activeGroupUrls.includes(url))`），
    而在详情图 Tab 里勾的若是**主图那组**的图 ⇒ 全被滤掉 ⇒ `length === 0`。

    ⚠️ 这是 v2.23「两组合并成一个可选参考图」时**漏改的判据**：
    勾选区合并了、按钮判据没跟着合并 ⇒ 两处口径不一致。
    后端本来就按 `reference_image_urls` 收（不按组收），所以按组过滤**没有依据**。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    assert "const activeGroupRefs = detailImageRefs;" in page, (
        "按钮判据又变成按 Tab 过滤了 ⇒ 在另一个 Tab 里勾的图不算，按钮恒灰"
    )
    # 勾选区必须是**合并**的（两处口径要一致）
    assert "...(detailImageDetail?.product_images || [])" in page
    assert "...(detailImageDetail?.detail_images || [])" in page


def test_shop_items_action_column_is_two_per_row():
    """商品操作列 **一行两个操作**（v2.31，用户口径「商品操作列：一行两个操作功能」）。

    为什么守「容器是 grid 两列」而不是「有 4 个按钮」：
    4 个按钮本来就在（一个 `Space` 水平排 **1 行 4 个**）—— 用户要的是**换行位置**。
    ⚠️ 换行必须**声明式**：用 `Flex wrap` 时换行由容器宽度决定（3+1 或 2+2 取决于列宽），
    只有 `grid-template-columns: 1fr 1fr` 才是「无论多宽都两列」。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    action = _slice_between(page, 'title: "操作"', "];")

    assert "gridTemplateColumns" in action, "操作列不是 grid 容器（换行位置会随列宽漂移）"
    assert '"1fr 1fr"' in action, "不是两列 grid"
    # 四个按钮都要撑满格子（否则两列左对齐、点击热区不一致）
    # 2026-10-05：按钮从 4 个变成 6 个 —— 新增「上架 / 下架 / 删除」三个
    # （按后端 `allowed_actions` **映射渲染**：源码里只有一处 `block`，运行时出 3 个），
    # 删掉「生成记录」（并入 AI商品图 弹窗的 Tab）。
    # ⚠️ 所以这里数的是**字面量**：4 个固定按钮（生成笔记 / LLM 生成 / AI商品图 / 编辑）
    #    + 1 处映射模板 = 5。数不出 6 不是漏按钮，是模板化带来的必然差。
    assert action.count("block") >= 5, f"操作按钮应都带 block，实际 {action.count('block')} 个"
    assert action.count(".map(") == 0, (
        "操作列不该再用 `allowed_actions.map()` —— 上架/下架 已合成一个按钮（`shelfAction`）、"
        "删除已进「更多」下拉（`deleteAction`）；留着 map 会把三条动作再渲一遍"
    )
    for label in ("生成笔记", "LLM 生成", "AI商品图", "编辑"):
        assert label in action, f"操作列少了「{label}」"
    # ⚠️ 「生成记录」**不许回到操作列**（用户口径：合并进 AI商品图 弹窗用 tab 布局）
    assert "生成记录" not in action, (
        "「生成记录」已并入 AI商品图 弹窗的 Tab（2026-10-05 用户口径），不得回到操作列"
    )
    # ⚠️ 上下架 / 删除的**可见性必须读后端下发的 `allowed_actions`**，
    #    不在前端复算「这个状态能不能下架」（同一件事两个实现必然分叉）。
    assert "allowed_actions" in action, "操作列的上下架/删除按钮必须读后端下发的 allowed_actions"
    assert "shelfAction.supported" in action and "deleteAction.supported" in action, (
        "按钮禁用态必须取后端下发的 supported（经 shelfAction / deleteAction 派生），不在前端复算"
    )
    # ⚠️ 单行按钮**不许复用批量处理器**：批量的 id 来源是勾选集 ⇒ 单行点它会顺带操作
    #    所有勾选的商品（写操作撤不回来）。两个入口、一份判据，但**两个函数**。
    assert "operateOneItem(item, shelfAction.action)" in action, (
        "操作列的单行上下架必须走 `operateOneItem`（只操作这一行），"
        "不得复用批量处理器（它会带上勾选集的全部商品）"
    )
    assert "operateOneItem(item, deleteAction.action)" in action, (
        "「更多」里的删除也必须走 `operateOneItem`（只操作这一行）"
    )
    # ⚠️ 回归防线：旧的单行 Space 容器必须已经不在操作列里
    assert "<Space size={4}>" not in action, "操作列仍是单行 Space（四个按钮挤一行）"
    # ------------------------------------------------------------------ #
    # 2026-10-06 用户口径「操作列功能按钮太多了，部分功能按钮隐藏」
    # + 「正常只展示三个常用功能按钮和一个更多」
    # ------------------------------------------------------------------ #
    assert "EllipsisOutlined" in action, "缺少「更多」下拉的入口图标"
    assert "更多" in action, "缺少「更多」下拉按钮"
    assert "dropdownRender" in action, (
        "收进下拉的按钮必须用 `dropdownRender` **原样**放进浮层"
        "（`menu={{items}}` 会把 Button 变成 MenuItem ⇒ 同一列两种渲染形态、"
        "可点性与禁用判据分叉）"
    )

    #: 真正的口径是**分区**：**可见区恒为 3 个常用按钮**（生成笔记 / LLM 生成 / 上架|下架），
    #: **「更多」里放** AI商品图 / 编辑 / 删除。不能按字符串位置切 —— `shelfAction`
    #: 那段 JSX 写在 `dropdownRender` **之后**（源码顺序 ≠ 屏幕顺序），故按
    #: 「在不在下拉浮层边界内」判定。
    dropdown_at = action.index("dropdownRender")
    dropdown_body = action[dropdown_at : action.index("</Dropdown>", dropdown_at)]
    visible_jsx = action[:dropdown_at] + action[action.index("</Dropdown>", dropdown_at) :]

    for label in ("AI商品图", "编辑"):
        assert label not in visible_jsx, (
            f"「{label}」不该出现在下拉**之外**（2026-10-06 口径：无状态一次性入口收起来）"
        )
        assert label in dropdown_body, f"「{label}」应放进「更多」下拉里"
    #: 删除（破坏性 + 非高频）也收进下拉，但**仍是状态驱动**的
    assert "deleteAction.label" in dropdown_body, "「删除」应在「更多」下拉里（状态驱动、可置灰）"
    for label in ("生成笔记", "LLM 生成"):
        assert label in visible_jsx, (
            f"「{label}」必须留在**可见区**（带 spin/loading —— 藏了用户看不到进度）"
        )
    #: 上架|下架（状态驱动，需求 7/8）留可见区 —— 藏了用户看不出商品能不能上架
    assert "shelfAction.label" in visible_jsx, (
        "上架|下架（状态驱动，需求 8）必须留在可见区，不进下拉"
    )
    assert "shelfAction.label" not in dropdown_body


# --------------------------------------------------------------------------- #
# M2 切片 3：指标仪表盘面板（设计 17 §13.10）
# --------------------------------------------------------------------------- #
METRICS_PANEL = ROOT / "frontend/src/pages/platforms/xhs/metrics-dashboard-panel.tsx"
METRICS_LIB = ROOT / "frontend/src/lib/metrics-format.ts"
ANALYTICS_PAGE = ROOT / "frontend/src/pages/platforms/xhs/analytics-page.tsx"


def test_metrics_panel_calls_all_four_request_functions():
    """面板必须**真的调用**切片 1/2 交付的 4 个请求函数。

    ⚠️ 这条守卫的存在理由（本切片的**核心风险**）：切片 2 加了 4 个请求函数但
    **零调用方** —— 那是「接口做完了、页面没接」的假闭环。

    ⚠️ **判据是「调用」不是「出现」（2026-10-04 变异实测教训）**：
    第一版只断言 `fn in code` ⇒ 删掉调用点的变异 **ESCAPED**
    （名字还留在 `import { ... }` 里，断言照样绿）。
    ⇒ 必须断言**调用形态**（后面紧跟 `(`），且 `import` 行不算
    —— 用「去掉 import 行后再数」实现。
    """
    import re as _re

    code = _code_only(_read(METRICS_PANEL))
    # ⚠️ 剥掉所有 import 语句，只留函数体 —— 否则「名字出现在 import 里」
    # 就能满足断言（这正是第一版假绿的源头）。
    body = _re.sub(r"import\s*\{[^}]*\}\s*from\s*\"[^\"]+\";", "", code, flags=_re.S)
    body = _re.sub(r"import\s+[^;]+;", "", body)

    for fn in (
        "fetchNoteMetricsTrend",
        "fetchShopMetricsTrend",
        "collectNoteMetrics",
        "collectShopMetrics",
    ):
        assert fn + "(" in body, f"面板没有真正调用 {fn}(（只在 import 里出现 ≠ 被调用）"


def test_metrics_panel_mounted_as_tab_in_host_page():
    """面板必须挂进宿主页面的页签 —— 否则「组件写了但没人渲染」= 白做。

    ⚠️ 变异守护：把 `items` 里的指标监控页签删掉 ⇒ 本用例必红。
    ⚠️ 同时钉住**宿主一期内容不被破坏**（「内容洞察」页签仍在）。
    """
    host = _code_only(_read(ANALYTICS_PAGE))
    assert "MetricsDashboardPanel" in host, "宿主页面没引用指标面板"
    assert "<MetricsDashboardPanel />" in host, "面板没有被渲染（只 import 不渲染）"
    assert "内容洞察" in host, "宿主一期「内容洞察」页签被误删"


def test_metrics_panel_empty_state_instead_of_zero_line():
    """空数据必须走 `Empty` 空态，**不得把空数组喂给 recharts**。

    ⚠️ 变异守护：把 `noteEmpty ? <Empty .../> : ...` 三段式改成直接渲染图表
    ⇒ `isEmptySeries` 不再出现在条件里、或 `Empty` 计数减少 ⇒ 本用例必红。

    为什么重要：空数组喂 recharts 会画出一条贴 0 的基线，视觉上等价于
    「数据是 0」；而真相是「那天没采」（trend 只返回有快照的天）。
    """
    code = _code_only(_read(METRICS_PANEL))
    assert "isEmptySeries" in code, "面板没有用统一的空态判据"
    assert code.count("Empty") >= 2, "笔记/商品两个面板都该有空态分支"
    # 空态判据必须真的在**条件**里用（不是 import 了放着）
    assert "noteEmpty ?" in code and "shopEmpty ?" in code, "空态判据没进条件分支"


def test_metrics_panel_price_uses_range_text_not_sum():
    """价格展示必须走 `priceRangeText`（区间文案），**不得就地求和**。

    ⚠️ 变异守护：
      * 改成 `${min + max}` / `${(min+max)/100}` ⇒ 本用例必红（`priceRangeText` 不再被调用）；
      * 直接把 `price_min_fen` 当数值渲染 ⇒ 少 100 倍且语义错（区间 ≠ 单值）。
    """
    code = _code_only(_read(METRICS_PANEL))
    assert "priceRangeText(" in code, "价格没有走区间文案（可能被求和或不换算）"
    assert "price_min_fen + price_max_fen" not in code, "价格被求和了（没有业务含义）"
    assert "price_min_fen +" not in code, "价格被求和了"


def test_metrics_format_lib_does_not_import_react():
    """纯逻辑库**不得** import React/antd —— 否则 vitest 的 node 环境跑不了。

    ⚠️ 变异守护：往 `metrics-format.ts` 里加 `import { Card } from "antd"` ⇒ 本用例必红
    （连带 `npm test` 也会因为环境不对而失败）。
    """
    code = _code_only(_read(METRICS_LIB))
    assert "from \"react\"" not in code, "纯逻辑库不该依赖 React"
    assert "from \"antd\"" not in code, "纯逻辑库不该依赖 antd"
    assert "from \"recharts\"" not in code, "纯逻辑库不该依赖 recharts"


def test_metrics_format_lib_never_pads_missing_days():
    """`toNoteChartData` / `toShopChartData` **不得**补零天。

    ⚠️ 变异守护：在实现里补日期（按 min~max 生成连续天并填 0）⇒ 本用例必红。

    补零 = 把「那天没采」画成「那天掉到 0」（伪造数据，硬约束⑤）。
    正确行为：原样 `map`，缺失的天表现为点与点之间跨度更大。
    """
    lib = _code_only(_read(METRICS_LIB))
    note_fn = _slice_between(lib, "export function toNoteChartData", "export function toShopChartData")
    shop_fn = _slice_between(lib, "export function toShopChartData", "export function engagementPieData")
    for name, body in (("toNoteChartData", note_fn), ("toShopChartData", shop_fn)):
        assert ".map(" in body, f"{name} 应原样透传（map）"
        # 禁日期推演：出现 Date/循环生成连续天的写法即视为补零
        for forbidden in ("new Date(", "for (let", "while ("):
            assert forbidden not in body, f"{name} 疑似在补日期（出现 {forbidden}）"


def test_metrics_vitest_case_file_is_wired():
    """前端用例文件必须存在且真的 import vitest（项目规范：测试须显式 import）。"""
    case_file = ROOT / "frontend/src/lib/metrics-format.test.ts"
    assert case_file.exists(), "缺少 metrics-format 的 vitest 用例"
    case = _read(case_file)
    assert 'from "vitest"' in case, "用例必须显式 import vitest（不依赖 globals）"
    assert "describe(" in case and "it(" in case, "用例里没有 describe/it"


# --------------------------------------------------------------------------- #
# 2026-10-05 商品操作 / 指定同步（需求 6 / 7 / 8）
# --------------------------------------------------------------------------- #
SYNC_ITEMS_MODAL = ROOT / "frontend/src/pages/platforms/xhs/sync-specified-items-modal.tsx"


def test_operate_result_modal_is_masked_timed_and_lockable():
    """结果弹窗：**加蒙版 + 统计耗时 + 执行中不许关**（2026-10-06 用户口径）。

    用户原话：「点击上架下架结果等待弹窗要加蒙版统计耗时，避免结果弹窗关闭，
    执行结果返回后再支持弹窗关闭」。

    ⚠️ 这条**修正**了此前的折中（那时运行中也能关 ⇒ 用户一点就把结果丢了）。
    判据四处，缺一不可：

    1. `maskClosable={false}` —— 点蒙版不关（最容易被误触的关闭路径）；
    2. `closable={!operateTaskRunning()}` —— 运行中连右上角 × 都没有；
    3. `onCancel={closeOperateResult}` + 关闭键 `disabled={operateTaskRunning()}`
       —— 三条关闭路径收敛到**同一个出口**，出口自己判非终态；
    4. 耗时**自计时**（`operateStartedAtRef` + 1s 一跳的 `operateTickRef` +
       终态停表）—— 不是后端字段（后端只给任务进度）。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    assert "maskClosable={false}" in page, "点蒙版就能关掉结果弹窗（用户会丢结果）"
    assert "closable={!operateTaskRunning()}" in page, (
        "运行中必须连右上角 × 都不给（用户口径「执行结果返回后再支持弹窗关闭」）"
    )
    assert "onCancel={closeOperateResult}" in page, (
        "关闭必须收敛到 `closeOperateResult`（它自己判非终态并拒绝）"
    )
    assert "onClick={closeOperateResult}" in page, "关闭键也要走同一个出口"
    assert "disabled={operateTaskRunning()}" in page, "执行中关闭键应不给点"
    #: 出口函数必须**真的**在非终态时拒绝（不是只把按钮置灰 —— 蒙版/× 两条路也要拦住）
    body = _slice_between(page, "function closeOperateResult()", "\n  }")
    assert "operateTaskRunning()" in body and "return" in body, (
        "`closeOperateResult` 必须在非终态时提前 return（否则等于没拦）"
    )
    #: 耗时：自计时 + 终态停表
    assert "operateStartedAtRef.current = Date.now()" in page, "耗时应在开始时起算"
    assert "setOperateElapsedSec(" in page, "耗时要能刷新（1s 一跳，否则秒表不动）"
    assert "耗时 {operateElapsedSec}s" in page, "弹窗里要显示耗时数值"
    tick = _slice_between(page, "function stopOperatePolling()", "\n  }")
    assert "operateTickRef.current" in tick, "停轮询时必须一并停秒表（否则终态后还在跳）"


def test_shop_items_operate_vitest_case_file_is_wired():
    """商品操作纯逻辑的 vitest 用例必须存在且真的 import vitest。

    镜像 `test_metrics_vitest_case_file_is_wired` 的口径 —— **依赖是双向的**：
    没有它，`lib/shop-items-view.ts` 的限值 / 文案被改也没人发现
    （源码守卫只查「常量存在」，证明不了它的值）。
    """
    case_file = ROOT / "frontend/src/lib/shop-items-operate.test.ts"
    assert case_file.exists(), "缺少 shop-items-view 商品操作部分的 vitest 用例"
    case = _read(case_file)
    assert 'from "vitest"' in case, "用例必须显式 import vitest（不依赖 globals）"
    assert "describe(" in case and "it(" in case, "用例里没有 describe/it"


def test_sync_specified_modal_uses_an_infinite_list():
    """「同步指定商品」弹窗必须是**无限列表**（需求 6 原话）。

    ⚠️ 判据落在**三处调用形态**上，缺一不可：
    * `virtual` —— 不渲染全部行（台账 2000+ 行会卡死，而不是「慢一点」）；
    * `onScroll` + 取下一页的调用 —— 滚到底自动加载（没有它就是「只取一页」）；
    * `pagination={false}` —— 有分页器就不是无限列表（两者是互斥的两种交互）。
    """
    modal = _code_only(_read(SYNC_ITEMS_MODAL))
    assert "virtual" in modal, "商品列表必须开 `virtual`（否则 2000+ 行会卡住）"
    assert "onScroll=" in modal and "handleScroll" in modal, (
        "缺少滚动加载 —— 那就不是无限列表，是「只取第一页」"
    )
    assert "pagination={false}" in modal, "无限列表不该同时挂分页器（两种交互互斥）"
    assert "fetchXhsShopItems" in modal, "列表数据必须来自商品台账端点"
    assert "loadPage(platformAccountId, next" in modal, "滚到底必须真的取下一页"


def test_sync_specified_modal_keeps_every_submitted_id():
    """逐条结果必须落在**提交时那份 ID 清单**上（没命中的不许消失）。

    ⚠️ 后端只给聚合数（`synced` / `not_found`），不给逐条明细 —— 前端若
    用「命中项」渲染结果表，用户点名 3 个、只成功 1 个时会看到 1 行
    （另外两个**凭空消失**，像是提交丢了）。
    """
    modal = _code_only(_read(SYNC_ITEMS_MODAL))
    assert 'notFound.has(id) ? ("missing" as const) : ("ok" as const)' in modal, (
        "结果行必须由**提交的 ID 清单**逐条派生（不是由命中项拼）"
    )
    assert "setRows((current) => {" in modal, "提交即铺满全部 ID 为「同步中」（慢扫描期间列表不能是空的）"


def test_sync_specified_modal_shows_the_selection_before_syncing():
    """**勾选的商品以列表展示在弹窗里，点了同步才跑**（2026-10-06 v2 口径）。

    用户原话：「弹窗勾选商品后，将勾选的商品**使用列表在弹窗中展示**，使用 tab 的方式
    分为按条件同步和按 ID 同步商品信息，**点击同步等待同步商品结果**」。

    ⚠️ 这条**推翻了 v1 的「打开即同步」**：那时弹窗一开就跑，用户根本没有「点击同步」
    这个动作可以点。判据两处：

    1. 打开时把 `itemIds` **铺成待同步列表**（`todoRow`）；
    2. **不许**再出现 `void runSync(itemIds)`（自动开跑）。
    """
    modal = _code_only(_read(SYNC_ITEMS_MODAL))
    #: 2026-10-06 v3：入参从裸 ID 清单换成**带名称/缩略图的 items**（否则「商品信息」列
    #: 的名称与图全空 —— 用户实测报的 bug）。
    assert "setRows(items.map((row) => todoRow(row.item_id, row.title, row.image)))" in modal, (
        "打开时必须把勾选集**铺成列表**（用户口径「将勾选的商品使用列表在弹窗中展示」）"
    )
    assert "void runSync(itemIds)" not in modal, (
        "不该再「打开即同步」（v2 是点了同步才跑；自动开跑会让「点击同步」这个动作落空）"
    )
    #: 两个 Tab 的名字就是口径本身
    assert 'label: "按条件同步"' in modal, "缺少「按条件同步」Tab"
    assert 'label: "按 ID 同步商品信息"' in modal, "缺少「按 ID 同步商品信息」Tab"
    #: 按条件 ⇒ 触发即关（后台跑）；按 ID ⇒ 等结果（关按钮在跑时禁用）
    body = _slice_between(modal, "function submitConditionSync()", "\n  }")
    assert "onClose()" in body, "按条件同步必须是「触发后台任务 + 关弹窗」"
    assert "onConditionSync(" in body, "按条件同步必须把条件交给页面触发后台任务"


def test_merged_batch_sync_button_keeps_both_paths():
    """**合并后两条路的功能必须全保留**（2026-10-06 用户口径原话）。

    「将顶部的同步执行商品按钮功能合并到同步商品按钮中，该按钮文本优化为
    **批量同步商品信息**，注意合并后两个按钮的功能需全部保留」。

    判据 = 三面都在，且**入口合一**：

    1. 按钮文本 = `批量同步商品信息`（运行中 ⇒ `商品同步中`）；
    2. 勾选分支 ⇒ `setSyncItemsOpen(true)`（原「同步指定商品」的按 ID 同步）；
    3. 未勾选分支 ⇒ `void syncAllStatusGoods(payload)`（原「同步商品」的整店同步）；
    4. 行内 ⇒ `setSyncSingleTarget(...)`（新增的单件「同步商品信息」）；
    5. 旧的两个按钮文本**都不许再作为独立入口**出现（入口必须真的合一）。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    assert "批量同步商品信息" in page, "缺少合并后的按钮文本"
    #: 2026-10-06 v2：两个 Tab 在**弹窗内部** —— 页面只负责开门 + 接「按条件」的回调。
    modal = _code_only(_read(SYNC_ITEMS_MODAL))
    assert 'label: "按条件同步"' in modal and 'label: "按 ID 同步商品信息"' in modal, (
        "弹窗必须是「按条件同步 / 按 ID 同步商品信息」两个 Tab"
    )
    page_side = _slice_between(page, "onConditionSync={(options: SyncOptions)", "\n        }}")
    assert "void syncAllStatusGoods(payload)" in page_side, (
        "「按条件同步」没接到整店同步 ⇒ 原「同步商品」的功能丢了"
    )
    assert "setSyncItemsOpen(true)" in page, (
        "顶部按钮必须开门到弹窗（按 ID 那条路在弹窗内的 Tab 里）"
    )
    #: 行内单件入口
    assert "setSyncSingleTarget(item.item_id)" in page, "缺少行内「同步商品信息」（单件）入口"
    assert "同步商品信息" in page, "行内按钮文本应为「同步商品信息」"
    #: 入口合一：旧名字不许再作为按钮文本出现
    assert ">同步商品<" not in page, "旧「同步商品」按钮仍在（应已并入）"
    assert ">同步指定商品<" not in page, "旧「同步指定商品」按钮仍在（应已并入）"


def test_sync_specified_button_passes_the_row_selection():
    """页面侧：`同步指定商品` 按钮把**主表勾选**交给弹窗，且空勾选时不空开弹窗。

    ⚠️ 「空勾选时不打开」不是体验偏好：空手进去只能看到一个空列表，而用户的原话是
    「**选中商品后**，点击…按钮」⇒ 先给一句话让他去勾，比开个空弹窗有用。
    判据是**条件形态**（先 `length === 0` 提示并 return），不是「有没有 message」。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    #: 2026-10-06 合并「批量同步商品信息」后：弹窗入参在**行内单件**路径下换成那一件，
    #: 其余情况仍是主表勾选集（判据钉两个分支都在，且单件只是**覆盖**不是替代）。
    #: 2026-10-06 v3：传的是**带元数据的勾选集**（名称 + 缩略图），不再是裸 ID 数组。
    assert "syncSingleTarget" in page and "items={" in page, (
        "按钮/弹窗必须传**主表勾选集**（行内单件入口才换成那一件）"
    )
    assert "(data?.items ?? [])" in page and "selectedItemIds.includes(row.item_id)" in page, (
        "勾选集必须从**当前页数据**里带出名称与缩略图（否则弹窗的商品信息列是空的）"
    )
    #: 合并后空勾选**不再拦截**（那时走整店同步）—— 判据反转：必须有整店分支
    assert "void syncAllStatusGoods(payload);" in page, (
        "未勾选时必须走整店同步（合并后「同步商品」的功能全靠这条）"
    )


def test_shop_items_status_tag_color_comes_from_the_status_key():
    """状态 tag 的**颜色与文案必须同源**（2026-10-06 用户实测 bug）。

    ⚠️ 原实现 `<Tag color={item.buyable ? "green" : "default"}>{item.status_label}</Tag>`
    把颜色交给 `buyable`、文案交给 `status_label` —— 两个能互相矛盾的字段：

    * 上架后 `status_label` 已是「售卖中」而 `buyable` 还是 false（后端本地刷新
      漏刷）⇒ **灰色「售卖中」**；
    * 已下架但 `buyable` 仍 true ⇒ **绿色「已下架」**。

    用户原话就是这两个症状。判据落在**取色来源**上：必须调 `statusTagColor(...)`
    且入参是 `status_key`（与 `status_label` 同一词汇表）—— 而不是 `buyable`。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    assert '<Tag color={statusTagColor(item.status_key || "")}>{item.status_label}</Tag>' in page, (
        "状态标签必须用 `statusTagColor(status_key)` 上色（与文案同源）"
    )
    assert '<Tag color={item.buyable ? "green" : "default"}>{item.status_label}</Tag>' not in page, (
        "状态标签的颜色取了 `buyable` —— 它与 `status_label` 会互相矛盾"
        "（灰色「售卖中」/ 绿色「已下架」就是这条造成的）"
    )
    #: 取色映射本身住 lib（唯一实现），页面不自建第二份
    view = _code_only((ROOT / "frontend/src/lib/shop-items-view.ts").read_text(encoding="utf-8"))
    body = _slice_between(view, "export function statusTagColor(", "\n}")
    for key, color in (
        ("on_shelf", "green"),
        ("off_shelf", "default"),
        ("frozen", "red"),
    ):
        assert f'"{key}"' in body and f'"{color}"' in body, (
            f"`statusTagColor` 缺 {key} → {color} 的映射"
        )
    assert "default" in body.split("default:")[-1], "未知键必须退 `default`（缺值口径：不猜）"


def test_local_refresh_also_flips_buyable():
    """上架/下架后的**本地即时刷新**必须同时翻 `buyable`（2026-10-06 修的真 bug）。

    `buyable` 与 `on_shelf_time` / `off_shelf_time` 是**同一个生命周期信号的三面**。
    只刷时间戳与 `status` 列 ⇒ `status_label` 显示「售卖中」而「可售」列显示
    **停售**（前端有守卫断言 `readable` 列与状态**不许互相矛盾**）。

    ⚠️ 判据落在 `_refresh_local_status` 的函数体上（不是「文件里出现过 buyable」）。
    """
    svc = _code_only(
        (ROOT / "backend/app/services/shop_item_operate_service.py").read_text(encoding="utf-8")
    )
    body = _slice_between(svc, "def _refresh_local_status(", "\ndef ")
    assert 'raw["buyable"] = wants_on_shelf' in body, (
        "本地刷新漏刷 `buyable` ⇒ 上架后「可售」列仍显示停售 / 详情显示「不可购买」"
        "（与状态标签自相矛盾）"
    )
    #: 三个信号必须**在同一处**翻转（漏一个就是同型 bug 再来一次）
    assert 'raw["on_shelf_time"]' in body and 'raw["off_shelf_time"]' in body, (
        "时间戳信号也要一起翻（门禁 `status_key_from_raw` 读它们）"
    )
    assert 'row.status = "售卖中" if wants_on_shelf else "已下架"' in body, (
        "展示标签（`status` 列）也要一起翻"
    )


def test_sync_modal_item_column_and_picker_search():
    """弹窗「商品信息列」+ 台账补充列表的**模糊查询**（2026-10-06 用户口径）。

    用户原话：「弹窗的商品 ID 列，优化为**商品信息列**，缩略图，商品名称，商品 ID；
    注意商品名称不要过长使用 **tip** 展示全部，从台账选择补充按钮，点击后**未展示可补充的
    列表**（bug），列表之前**名称模糊查询**批量加入待同步列表」。

    判据四件：

    1. 列名是「商品信息」且**同时**渲染缩略图 / 名称 / ID；
    2. 名称套 `Tooltip`（用户口径「用 tip 展示全部」）+ 单行 ellipsis（不过长撑列）；
    3. 点开「从台账选择补充」**必须加载第一页** —— 这正是他报的 bug：
       v2 打开弹窗只铺清单不再取台账，点开只能看到空列表；
    4. 关键词走**服务端** `keyword`（不是前端在已加载页里过滤）。
    """
    modal = _code_only(_read(SYNC_ITEMS_MODAL))
    assert 'title: "商品信息"' in modal, "列名应为「商品信息」（原为「商品 ID」）"
    assert 'title: "商品 ID"' not in modal, "「商品 ID」列应已被「商品信息」取代"
    for piece in ("row.image", "row.title", "row.item_id"):
        assert piece in modal, f"商品信息列必须同时给出 {piece}（缩略图 / 名称 / ID）"
    #: 名称：Tooltip 兜全 + ellipsis 截断
    assert "textOverflow: \"ellipsis\"" in modal and "<Tooltip title={row.title" in modal, (
        "商品名称要单行截断 + Tooltip 展示全部（用户口径「不要过长使用 tip 展示全部」）"
    )
    #: 点开补充列表必须**真的取数**（否则就是空列表 —— 用户实测报的 bug）
    opener = _slice_between(modal, "onClick={() => {\n                      const next = !pickerOpen;", "\n                    }}")
    assert "loadPage(platformAccountId, 1, requestSeq.current)" in opener, (
        "点开「从台账选择补充」必须加载第一页 —— 不加载就点开只能看到空列表"
    )
    #: 模糊查询走服务端
    assert "loadPageWithKeyword(" in modal, "搜索必须按**显式关键词**取数（避免 setState 竞态）"
    assert "keyword: kw.trim()" in modal or "keyword: keyword.trim()" in modal, (
        "关键词要透给后端的 `keyword`（服务端筛，不是前端在已加载页里过滤）"
    )


def test_sync_modal_80vw_uniform_item_column_and_batch_remove():
    """弹窗宽度 80%、两个列表**同一套商品信息列**、待同步支持批量移除（2026-10-06 口径）。

    用户原话：「弹窗**全屏的 80%**；台账列表商品名称改为**商品信息列与待同步列展示一样**；
    **复选框列 50px**；待同步信息列表新增复选框列，支持**批量移除**；商品信息列的商品图和
    名称均未展示（bug）」。

    ⚠️ 「图和名称都没展示」的**根因**是入参只给了 ID（弹窗无从得知名称/图片），
    所以判据落在**页面侧把元数据带进来**这件事上，而不只是弹窗里有没有渲染代码。
    """
    modal = _code_only(_read(SYNC_ITEMS_MODAL))
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    #: ① 宽度 80%
    assert 'width="80vw"' in modal, "弹窗宽度应为全屏的 80%（用户口径）"
    assert 'width="70vw"' not in modal, "旧的 70vw 应已被取代"
    #: ② 两个列表共用同一个商品信息列（不是各写一份）
    assert "function itemInfoCell(" in modal, "商品信息单元格必须抽成**共用**函数"
    assert modal.count("itemInfoCell(row)") >= 2, (
        "待同步列表与台账候选列表都要用它（用户口径「与待同步列展示一样」）"
    )
    #: ③ 复选框列 50px（两个列表都有）
    assert modal.count("columnWidth: 50") >= 2, "两个列表的复选框列都应是 50px"
    #: ④ 待同步列表支持批量移除
    assert "移除选中的 {checkedRows.length} 件" in modal, "待同步列表要有批量移除"
    #: ⑤ 根因面：页面必须把名称/缩略图带进弹窗
    assert "(data?.items ?? [])" in page, (
        "页面必须从当前页数据里带出名称与缩略图（只传 ID 就是用户报的那个 bug）"
    )


def test_shop_items_waits_all_use_the_page_mask_and_elapsed():
    """**所有等待都要有页面蒙版 + 耗时**，且等待期间关不掉（2026-10-06 用户口径）。

    用户原话：「商品列表的所有功能的等待均应在**合适的时机**添加页面**蒙版**和**统计耗时**，
    **避免结果窗口关闭**」。

    判据三件：

    1. 页面根节点被 `Spin` 包住（`spinning={waitRunning}` + `tip` 带耗时）——
       整页被盖住 ⇒ 等待期间下面的按钮点不到，就不会出现「等结果时结果窗口被关掉」；
    2. 每个等待动作**成对**调 `beginWait("...")` / `endWait()`（数对得上）；
    3. 秒表在结束时停（漏了就是「蒙版没了数字还在跳」）。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    #: ① 根级 Spin + 耗时提示
    assert "spinning={waitRunning}" in page, "页面缺根级等待蒙版"
    assert "耗时 ${waitElapsedSec}s" in page, "蒙版上要显示**耗时**（用户口径）"
    assert "const waitRunning = waitLabel !== null;" in page, "缺等待判据"
    #: ② 幂等 beginWait（不重起秒表）+ 停表
    begin = _slice_between(page, "function beginWait(", "\n  }")
    assert "if (waitStartedAtRef.current === null)" in begin, (
        "`beginWait` 必须幂等 —— 重起秒表会把已耗时清零"
    )
    end = _slice_between(page, "function endWait()", "\n  }")
    assert "setWaitLabel(null)" in end and "clearInterval" in end, (
        "`endWait` 必须**清标签 + 停表**（漏一个就是「蒙版没了数字还在跳」）"
    )
    #: ③ 各等待点成对（数量必须一致；少一个 endWait 就是蒙版永不消失）
    #: ⚠️ 两边都要**减去函数定义那一处**（`function beginWait(` / `function endWait()`），
    #: 否则比的是「定义 + 调用」与「定义 + 调用」的混合数（初版就是这么写错的）。
    assert page.count("beginWait(") == page.count("endWait()"), (
        f"beginWait / endWait 不成对：{page.count('beginWait(')} vs {page.count('endWait()')}"
    )
    for label in ("加载商品列表", "同步商品", "加载商品详情", "导出 CSV", "生成商品AI图"):
        assert f'beginWait("{label}")' in page, f"「{label}」的等待没挂蒙版"


def test_no_markdown_bold_inside_user_facing_strings():
    """**用户可见文案里不许出现 Markdown 的 `**`**（2026-10-06 用户口径）。

    用户原话：「商品页很多说明文案存在 `**`，移除」。

    ⚠️ 根因是我把写文档的习惯带进了**会渲染的字符串**：`description="...**立即关闭**..."`、
    `` `${label}：对选中的 ${n} 个**平台商品**执行` `` —— 这些不是 markdown，
    星号会**原样显示**在界面上（用户看到的是两个星号，不是加粗）。

    判据：剥掉注释后，**含 `**` 的行里不许出现会渲染的上下文**
    （`description=` / `message=` / `title=` / `placeholder=` / `extra=` /
    模板串 / `Modal.confirm` 的 `content`）。

    ⚠️ 注释与 JSDoc 里的 `**`（`/** 说明 **粗体** */`）**是允许的** —— 它们不渲染，
    而且项目文档风格一直在用（剥注释这一步不能省，否则守卫会被自己的注释打红）。

    ⚠️ **判据必须覆盖 JSX 文本节点**（2026-10-06 二次口径「注意**所有说明文案**中的
    `**` 移除」）：`<Text>商品的**价格**只能看</Text>` 这种**没有 `=` 属性**的写法
    会被「只看 `description=` 之类」的窄判据漏掉 —— 初版就漏了
    `product-edit-modal.tsx` 与 `lib/order-write.ts` 的 4 处，靠人工扫描才发现。
    ⇒ 现在只要「非注释行」出现 `**` 就报，**除**纯代码/测试语境（`it(` / `describe(`
    的描述串与 `expect` 断言 —— 那些不进 UI；`order-format.test.ts` 里的
    `"189***2857"` 是**平台脱敏原值**，也不是 markdown）。
    """
    renderable = re.compile(
        r'(description|message|title|placeholder|extra)\s*[=:]\s*["`{]|message\.\w+\(|content[:=]'
    )
    #: **不算**用户可见的语境：测试文件（`it(` / `expect(`）与脱敏串（`***`）。
    test_ctx = re.compile(r'\b(it|describe|test|expect)\s*\(|"\d+\*+\d+"')
    #: **JSDoc / 块注释的正文行**也要排除：`_code_only` 用 `/\*.*?\*/`（非贪婪）
    #: 遇到「块注释里另有 `*/`」的写法会漏剥 ⇒ 判据自己再挡一层
    #: （以 `*` 或 `/**` 开头的行是注释正文，不是渲染内容）。
    comment_line = re.compile(r"^\s*(/\*\*?|\*)")
    offenders: list[str] = []
    for path in sorted((ROOT / "frontend/src").rglob("*.tsx")) + sorted(
        (ROOT / "frontend/src").rglob("*.ts")
    ):
        code = _code_only(path.read_text(encoding="utf-8"))
        for lineno, line in enumerate(code.splitlines(), 1):
            if "**" not in line:
                continue
            if test_ctx.search(line) or comment_line.match(line):
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, (
        "用户可见文案里出现了 Markdown 的 `**`（会原样显示成星号）：\n  "
        + "\n  ".join(offenders)
    )
    #: 守卫自检：两种真形态都要认得出（属性式 + JSX 文本节点），否则是空转的
    assert renderable.search('const x = <Alert description="**立即关闭**" />;'), "属性式漏判"
    assert "**" in "<Text>商品的价格**不能改**</Text>", "JSX 文本节点式漏判"


def test_shop_items_page_has_no_top_qianfan_card_and_no_refresh_icon():
    """需求 2 / 3 / 4 的退场判据（三处都是「少了一个东西」，只查代码不查注释）。"""
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    # ③ 顶部「商品列表（千帆）」整块移除
    assert "商品列表（千帆）" not in page, "顶部「商品列表（千帆）」整卡应已移除"
    # ④ 搜索区域的刷新图标按钮移除
    assert "ReloadOutlined" not in page, "搜索区域的刷新图标按钮应已移除"
    # ③ 账号筛选搬到「搜索条件」卡的 extra
    cond = _slice_between(page, 'title="搜索条件"', 'title="商品台账"')
    assert "账号" in cond and "accountOptions" in cond, "账号下拉应在「搜索条件」卡里"
    # ② 导出 CSV / 同步商品 在「商品台账」卡的 extra
    #: 台账卡的切片到「生成队列实时态」那段为止（它是卡内最后一个东西）。
    ledger = _slice_between(page, 'title="商品台账"', "genQueueItemIds.size > 0")
    assert "导出 CSV" in ledger, "导出 CSV 应在「商品台账」卡 header"
    assert "同步商品" in ledger, "同步商品应在「商品台账」卡 header"


def test_sync_specified_items_button_sits_in_the_ledger_card_top():
    """「同步指定商品」在**商品台账卡顶部**、且在「同步商品」**之前**
    （2026-10-06 用户口径「同步指定商品按钮放商品台账顶部」）。

    ⚠️ 判据三条一起才叫定位：
    ① 它在搜索条件卡里**不存在**（`_code_only` 剥过注释 —— 注释里提到它不算）；
    ② 它在商品台账卡里存在；
    ③ 它的**位置在「同步商品」之前**（顺序也是口径的一部分：
       指名同步 → 整店同步 → 导出，入口由窄到宽）。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    search_at = page.index('title="搜索条件"')
    ledger_at = page.index('title="商品台账"')

    assert "同步指定商品" not in page[search_at:ledger_at], (
        "「同步指定商品」不该还在搜索条件卡里（2026-10-06 已搬到商品台账卡）"
    )

    ledger = page[ledger_at:]
    #: 2026-10-06 **合并**（用户口径「将顶部的同步执行商品按钮功能合并到同步商品按钮中，
    #: 该按钮文本优化为**批量同步商品信息**，注意合并后两个按钮的功能需全部保留」）
    #: ⇒ 台账卡里**只有一个同步入口**，两个旧按钮名都不许再出现。
    assert "批量同步商品信息" in ledger, "商品台账卡里缺少合并后的「批量同步商品信息」"
    assert ">同步商品<" not in ledger, (
        "原「同步商品」按钮应已并入「批量同步商品信息」（功能保留、入口合一）"
    )
    assert ">同步指定商品<" not in ledger, (
        "原「同步指定商品」按钮应已并入「批量同步商品信息」（功能保留、入口合一）"
    )
    #: 两条路的功能必须**都在**：勾选 ⇒ 按 ID；未勾选 ⇒ 整店（判据落在分支代码上）
    merged = page[ledger.index("批量同步商品信息") :]
    assert "setSyncItemsOpen(true)" in merged, "按 ID（勾选）那条路不见了 —— 功能没全保留"
    assert "void syncAllStatusGoods(payload)" in merged, (
        "整店（未勾选）那条路不见了 —— 功能没全保留"
    )
    #: 行内单件入口（「同步商品信息」）走同一个弹窗（功能保留的第三面）
    assert "setSyncSingleTarget(item.item_id)" in page, (
        "操作列缺少行内「同步商品信息」（单件补数据）入口"
    )


def test_shop_items_button_tips_stay_short():
    """按钮 tip 只留核心，且**不许再出现已退场的功能名**（2026-10-06 用户口径）。

    用户原话：「所有按钮的 tip 太长了，精简只保留核心即可」。改前最长 **67 字**
    （「从千帆同步该账号的全部状态商品（七个状态枚举）到本地台账，后台执行」），
    改后 ≤ 18 字。

    ## 为什么长度要上守卫

    长度是**会自己长回去**的东西：单次加一句说明看起来无害，三轮之后就回到 67 字。
    这里给一个**上限**（22 字，比现行最长 18 留 4 字余量），谁把 tip 写长谁红。

    ⚠️ 顺带钉住**过期内容**：本次实测发现「编辑」的 tip 里还写着**已退场的
    「改分组」**（分组功能本轮彻底移除，设计 20 §11.1）—— 指向一个不存在的功能
    比没有说明更坏（用户会去找一个不存在的 Tab，DEF-057 同型）。
    """
    import re

    page = _code_only(_read(SHOP_ITEMS_PAGE))
    tips = [m.group(1) for m in re.finditer(r'<Tooltip title="([^"]+)"', page)]
    assert tips, "没扫到任何 Tooltip —— 守卫空转（写法变了？）"

    too_long = [(tip, len(tip)) for tip in tips if len(tip) > 22]
    assert not too_long, (
        f"按钮 tip 超过 22 字（口径：精简只保留核心）：{too_long}"
        " —— 过程 / 边界（后台执行、平台限制、超限提示）不写进 tip，"
        "那些在点击后的提示与弹窗说明里各有一份"
    )

    #: 已退场的功能名不许再出现在 tip 里（指向不存在的功能 = 比没有说明更坏）
    offenders = [tip for tip in tips if "分组" in tip]
    assert not offenders, f"tip 里出现已退场的功能名「分组」：{offenders}"


def test_shop_items_sync_button_matches_the_account_card_page():
    """需求 5：本页「同步商品」与账号卡片页**同一套 UI 与逻辑**。

    判据是「共用同一个判据函数」而不是「看起来像」：页面必须 import
    `syncTaskPhase`（账号卡片页 §35.17.4 的那一把尺子），并自己写一份
    「什么算在跑」。自写一份 ⇒ 两处迟早分叉（一边认为在跑、一边认为空闲）。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    #: ⚠️ 判据必须是**import 形态**，不能只查名字出现 —— 变异实测教训：
    #: 把 import 换成页面内自定义的 `const syncTaskPhase = ...`（同名！）时，
    #: 「名字出现」判据**照样绿**（ESCAPED，本文件守卫曾是哑弹）。
    assert 'from "../../../lib/sync-gates"' in page, (
        "必须**从 lib/sync-gates 引入** `syncTaskPhase`（账号卡片页同一条尺子）；"
        "页面内自定义同名函数 = 两处判据迟早分叉"
    )
    assert "syncTaskPhase" in page, "必须复用账号卡片页的 `syncTaskPhase` 判据"
    assert "syncShopGoodsStatus" in page, "按钮态必须来自状态端点（首查 + 轮询）"
    assert 'message.info("该账号已有同步任务在跑，本次未触发新执行。")' in page, (
        "复用提示的文案必须与账号卡片页**逐字一致**"
    )
    #: 2026-10-06 用户口径：「要和账号信息卡的**同步店铺商品弹窗**一致，根据弹窗条件触发
    #: 同步商品任务异步同步商品」。判据 = **共用同一个弹窗组件**（不是照抄一份 UI）+
    #: 确认后**异步**触发（`void syncAllStatusGoods(payload)`，不是 await 阻塞）。
    #: 2026-10-06 v2：条件选择搬进「批量同步商品信息」弹窗的「按条件同步」Tab
    #: ⇒ 页面不再自建独立条件弹窗（同一件事不留两个入口，DEF-044）。
    modal = _code_only(_read(SYNC_ITEMS_MODAL))
    assert 'label: "按条件同步"' in modal, (
        "「全量 / 时间区间」的条件选择必须住在弹窗的「按条件同步」Tab 里"
    )
    assert "void syncAllStatusGoods(payload)" in page, (
        "「按条件同步」确认后要**异步**触发（`void`，不阻塞等同步跑完）"
    )


def test_shop_items_sync_button_locks_and_shows_running_label():
    """2026-10-06 用户口径：「存在同步商品的任务时同步商品按钮**不支持点击**，
    应显示为**商品同步中**」。

    ⚠️ 判据是**按钮文案 + disabled 都挂在同一个派生量**（`goodsSyncRunning`）上 ——
    只改文案不置灰（或反之）都是「半条口径」：用户看到「商品同步中」还能点，
    或者按钮点不动却不知道原因。
    """
    page = _code_only(_read(SHOP_ITEMS_PAGE))
    #: 2026-10-06 合并后按钮文本 = 「批量同步商品信息」，运行中仍是「商品同步中」。
    assert 'goodsSyncRunning ? "商品同步中" : "批量同步商品信息"' in page, (
        "按钮文案必须在「商品同步中 / 批量同步商品信息」之间切换"
    )
    assert "disabled={!selectedAccountId || goodsSyncRunning}" in page, (
        "有同步任务在跑时必须**不支持点击**（disabled 绑同一个 goodsSyncRunning）"
    )
    assert (
        "const goodsSyncRunning = selectedAccountId !== null && goodsSyncIds.has(selectedAccountId)"
        in page
    ), "goodsSyncRunning 必须由状态端点维护的 goodsSyncIds 派生（不是本地点击标记）"


# --------------------------------------------------------------------------- #
# M5 自动化工作流页（设计 19）
# --------------------------------------------------------------------------- #
WORKFLOW_PAGE = ROOT / "frontend/src/pages/platforms/xhs/workflow-page.tsx"
WORKFLOW_LIB = ROOT / "frontend/src/lib/workflow-format.ts"
ROUTER = ROOT / "frontend/src/app/router.tsx"
APP_SHELL = ROOT / "frontend/src/components/layout/app-shell.tsx"


def test_workflow_page_calls_all_eleven_request_functions():
    """工作流页必须**真的调用** M5 交付的 11 个请求函数。

    ⚠️ 判据是**调用形态**不是「出现」（2026-10-04 变异实测教训，同
    `test_metrics_panel_calls_all_four_request_functions`）：只断言名字出现在
    文件里 ⇒ 删掉调用点的变异 ESCAPED（名字还留在 import 里）。
    ⇒ 剥掉 import 后断言 `fn(`。
    """
    import re as _re

    code = _code_only(_read(WORKFLOW_PAGE))
    body = _re.sub(r"import\s*\{[^}]*\}\s*from\s*\"[^\"]+\";", "", code, flags=_re.S)
    body = _re.sub(r"import\s+[^;]+;", "", body)

    for fn in (
        "fetchWorkflows",
        "createWorkflow",
        "fetchWorkflowDetail",
        "updateWorkflow",
        "deleteWorkflow",
        "runWorkflow",
        "fetchWorkflowRuns",
        "fetchWorkflowRunDetail",
        "cancelWorkflowRun",
        "rerunWorkflowNode",
        "reviewWorkflowNode",
    ):
        assert fn + "(" in body, f"工作流页没有真正调用 {fn}(（只在 import 里出现 ≠ 被调用）"


def test_workflow_page_renders_in_router_and_menu():
    """页面必须**接线路由与菜单** —— 否则「组件写了但进不去」= 白做。

    ⚠️ 2026-10-06 设计 22 §3 更新：工作流**并入「自动运营」宿主页作 tab** ⇒
    判据从「路由渲染 `<WorkflowPage />` + 菜单有独立项」改为「宿主页渲染
    `<WorkflowPanel />` + 旧路由重定向 + 宿主菜单项在」。被取代的原句保留：
        assert "<WorkflowPage />" in router
        assert "/platforms/xhs/workflows" in shell
    """
    router = _code_only(_read(ROUTER))
    shell = _code_only(_read(APP_SHELL))
    auto_ops = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/auto-ops-page.tsx"))
    assert "/platforms/xhs/workflows" in router, "路由表里没有工作流旧路由（深链重定向）"
    assert "WorkflowPanel" in auto_ops, "宿主页没有挂载 WorkflowPanel"
    assert "/platforms/xhs/auto-ops?tab=workflows" in router, "旧路由未重定向到宿主 tab"
    assert "/platforms/xhs/auto-ops" in shell, "侧栏菜单里没有自动运营（工作流）入口"


def test_workflow_page_uses_lib_not_inline_vocabulary():
    """页面必须**从 `lib/workflow-format.ts` 导入**判据，**不内联重写词表**。

    ⚠️ 内联重写 = 同一语义两处维护（DEF-044）：改一处忘另一处，
    页面显示与守卫断言就会分叉。

    ⚠️ **判据是「导入形态」不是「名字出现」**（2026-10-04 变异实测教训，
    与 `test_metrics_panel_calls_all_four_request_functions` 同一条）：
    第一版只断言 `fn in code` ⇒ 变异「用本地函数遮蔽 lib 同名函数」
    **ESCAPED**（名字还在文件里，断言照样绿）。
    ⇒ 必须断言这几个名字出现在**来自 lib 的 import 语句**里。
    """
    import re as _re

    code = _code_only(_read(WORKFLOW_PAGE))
    # 抓出所有来自 lib 的 import 块
    imports = _re.findall(
        r"import\s*\{([^}]*)\}\s*from\s*\"[^\"]*workflow-format\";", code, flags=_re.S
    )
    assert imports, "工作流页没有从 lib/workflow-format 导入任何东西（疑似内联重写词表）"
    imported = {part.strip() for block in imports for part in block.split(",") if part.strip()}
    for fn in ("runStatusText", "runStatusColor", "nodeLabel", "durationText", "chainText"):
        assert fn in imported, (
            f"工作流页没有从 lib/workflow-format **导入** {fn}（同名本地函数遮蔽也算重写词表）"
        )


def test_workflow_page_never_renders_blank_duration_as_zero():
    """**缺值口径**：未结束的 run 耗时显示 `-`，**不得渲染成 `0`**。

    ⚠️ 变异守护：把 `durationText` 的未结束分支改成返回 `"0 秒"` ⇒
    本用例与 `workflow-format.test.ts` 必红。
    """
    lib = _code_only(_read(WORKFLOW_LIB))
    # 未结束（finishedAt 为空）⇒ 返回 "-"，不是 "0 秒"
    assert 'return "-";' in lib, "durationText 的缺值分支必须返回 `-`"
    assert '"0 秒"' not in lib, "不得把「未结束」渲染成 `0 秒`（那是两件事）"


def test_workflow_lib_does_not_import_ui_frameworks():
    """纯逻辑库不得依赖 antd / react —— 它要能被行为脚本直接驱动。"""
    code = _read(WORKFLOW_LIB)
    assert 'from "antd"' not in code, "lib 不得依赖 antd（纯逻辑）"
    assert 'from "react"' not in code, "lib 不得依赖 react（纯逻辑）"


# --------------------------------------------------------------------------- #
# M6 订单页（设计 16 §7，一期只读）
# --------------------------------------------------------------------------- #
ORDERS_PAGE = ROOT / "frontend/src/pages/platforms/xhs/orders-page.tsx"
ORDERS_LIB = ROOT / "frontend/src/lib/order-format.ts"


def _function_block(code: str, name: str) -> str:
    """截出 `function <name>(...) { ... }` 的函数体（用于**只在函数内**断言）。

    ⚠️ 不要在整份文件里断言短语 —— 同名/近义短语可能在别处出现，
    这会让「把判据改坏」的变异**逃逸**（2026-10-04 实测两次）。
    """
    import re as _re

    m = _re.search(r"(?:export\s+)?function\s+" + name + r"\s*\(", code)
    if not m:
        return ""
    start = code.index("{", m.end())
    depth = 0
    for i in range(start, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[start:i + 1]
    return code[start:]


def test_orders_page_calls_all_six_request_functions():
    """订单页必须接线到 6 个请求函数（否则页面是空壳）。

    ⚠️ 判据是**调用形态**（`fetchOrders(` 而非仅名字出现）：只断言名字时，
    「import 了但没调用」也能过（2026-10-04 在 M2/M5 各踩过一次）。
    """
    import re as _re

    code = _code_only(_read(ORDERS_PAGE))
    # ⚠️ 只看「函数名出现」不够：把 `await fetchOrders({...})` 改成
    # `await Promise.all([Promise.resolve(null), ...])` 时名字仍在 import 里
    # ⇒ 第一版 **ESCAPED**（2026-10-04 实测）。判据改成**真调用形态**：
    # 必须出现 `fetchXxx(` 且调用后**把返回值赋给变量或用 await**。
    for fn in (
        "fetchOrders",
        "fetchOrderDetail",
        "fetchOrderMetrics",
        "syncOrders",
        "exportOrders",
        "saveOrderRemark",
    ):
        # 接受三种真实调用形态：
        #   await fn(...)          —— 直接等
        #   const x = await fn(...) / const x = fn(...)  —— 赋值
        #   Promise.all([fn(...), ...])  —— 并发（实参里裸调用）
        # 必须**出现在调用位置**，不能只在 import 列表里。
        call = _re.search(
            r"await\s+" + fn + r"\s*\("
            r"|const\s+\w+\s*=\s*(?:await\s+)?" + fn + r"\s*\("
            r"|(?:all|race)\s*\(\s*\[?[\s\S]{0,400}?" + fn + r"\s*\(",
            code,
        )
        assert call, f"订单页没有真正调用 {fn}（名字出现不等于调用）"


def test_orders_page_uses_lib_for_vocabulary_not_inline():
    """展示判据必须**从 lib/order-format 导入**，不内联重写（DEF-044）。

    遮蔽同名函数也算重写 —— 判据是「在 import 列表里」。
    """
    import re as _re

    code = _code_only(_read(ORDERS_PAGE))
    imports = _re.findall(
        r"import\s*\{([^}]*)\}\s*from\s*\"[^\"]*order-format\";", code, flags=_re.S
    )
    assert imports, "订单页没有从 lib/order-format 导入任何东西"
    imported = {p.strip() for block in imports for p in block.split(",") if p.strip()}
    for fn in ("orderStatusText", "orderStatusColor", "moneyText", "orderItemsSummary", "timeText"):
        assert fn in imported, f"订单页没有从 lib/order-format 导入 {fn}"


def test_orders_page_never_renders_blank_money_as_zero():
    """**缺值口径**：金额 / 退款率 / 时长的渲染必须经 lib 的 `-` 判据。

    ⚠️ 页面里若直接写 `value ?? 0` 或 `Number(value)`，缺值就变成 0 ——
    「没这个数」与「这个数是 0」是两回事（缺值口径）。
    """
    import re as _re

    code = _code_only(_read(ORDERS_PAGE))
    # ⚠️ 只盯**金额 / 比率 / 时长**这几类字段的补零 —— 「订单条数」缺值补 0
    # 是对的（条数为 0 就是没有订单），第一版把 `list.total ?? 0` 也判成违规
    # ⇒ 假阳性。判据收窄到「这些字段名 + 补零写法」同现。
    # ⚠️ 第一版把「字段名」和「?? 0」限在同一行 40 字符内 ⇒ 变异把
    # `moneyText(value)` 改成 `moneyText(value ?? 0)` 时，字段名在**上面几行**
    # 的 `dataIndex`，够不着 ⇒ **ESCAPED**（2026-10-04 实测）。
    # 判据改成：**任何 moneyText/percentText/hoursText 的实参里出现补零**即违规 ——
    # 这几个函数本身就是「缺值 → -」的唯一实现，往实参里补 0 就是绕过它。
    for fn in ("moneyText", "percentText", "hoursText"):
        for m in _re.finditer(fn + r"\(([^)]*)\)", code):
            arg = m.group(1)
            assert "?? 0" not in arg and "|| 0" not in arg, (
                f"{fn} 的实参里把缺值补成 0：{fn}({arg})"
            )
    # 另外：禁止 `Number(...)` 直接吞掉 null（会变 NaN，渲染成 "NaN" 而不是 -）
    assert "Number(detail.pay_amount" not in code
    assert "Number(detail.total_amount" not in code
def test_orders_page_write_entries_are_gated_and_confirmed():
    """**M7**：写入口**只对可操作状态出现**，且**必须带二次确认**（设计 16 §7.4）。

    ⚠️ 本守卫**取代**了 `test_orders_page_has_no_platform_write_actions`
    （2026-10-04 之前「一期只读」的旧契约，原句见 docstring 下面）。

    > 原句：**一期只读**：页面不许出现发货 / 改价 / 售后审核入口（平台写接口未授权）。

    门禁解除后判据反转：**不是"不许有写入口"，而是"有的写入口必须被门禁 + 二次确认夹住"**。
    两件事必须同时成立，否则会出现「按钮在、点了 400」（本项目踩过）或
    「能点、点完直接写真实平台」（更贵）。
    """
    code = _code_only(_read(ORDERS_PAGE))

    # ① 写入口确实存在（否则下面的断言全是空转）—— 只认 M7 批准的那两个
    assert "shipOrder(" in code, "M7 写入口缺失（发货）"
    assert "handleAfterSale(" in code, "M7 写入口缺失（售后）"

    # ② 仍然禁止的写操作（未授权）：改价 / 退款等
    for forbidden in ("updateOrderPrice(", "deliverOrder(", "refundOrder(", "confirmShipment("):
        assert forbidden not in code, f"订单页出现未授权的写操作入口：{forbidden}"

    # ③ **门禁**：入口必须挂在状态判据上（不给假入口）。
    # ⚠️ 判据是**两个按钮各自的 `canXxx(...) ?` 三元的完整形态**，不是一个
    # `canShipOrder(` 子串 —— 后者在页面里有 2 处（三元的条件 + 兜底 `-` 的取反），
    # 只把**三元的条件**改成 `true` 时子串断言照样通过（2026-10-04 变异实测 ESCAPED）。
    assert "{canShipOrder(row.status) ? (" in code, "发货入口没有挂在状态门禁上"
    assert "{pendingAfterSale && canHandleAfterSale(pendingAfterSale.status) ? (" in code, (
        "售后入口没有挂在状态门禁上"
    )

    # ④ **二次确认**：**两条链路都**必须先 `await` 预览拿 token，再原样回传。
    # ⚠️ 必须是「两处」（`count == 2`）—— 只查「有没有」时，把**发货**那条
    # 的预览删掉、售后那条还在，断言照样过（2026-10-04 变异实测 ESCAPED）。
    assert code.count("const preview = await previewOrderWrite(") == 2, (
        "发货与售后两条链路都必须 await 预览拿确认凭据（缺一条即算未接）"
    )
    assert code.count("confirmToken: preview.confirm_token") == 2, (
        "两条链路都必须把预览下发的 token 原样回传（前端不自己算）"
    )

    # ⑤ 确认文案**由 lib 单一实现**生成（不许页面自己拼一句差不多的）
    assert "shipConfirmText(" in code, "发货确认文案没有走 lib（同一语义两处实现）"
    assert "afterSaleConfirmText(" in code, "售后确认文案没有走 lib"

    # ⑥ 首次渲染只读：M7 的两个确认弹窗默认关闭（不许自动弹）
    assert "shipOpen" in code and "afterSaleOpen" in code, "确认弹窗状态缺失"


def test_m7_batch_ship_limit_is_same_on_both_sides():
    """批量发货上限：前端常量与后端 `MAX_BATCH_SHIP` **必须同值**。

    为什么要钉：这个数字前端拦一次、后端拦一次；两边不等时表现是
    「界面说拦住了 / 其实发出去了」或「界面拦了后端从不执行」—— 两种都很难查。
    （同型守卫已有 `test_shop_items_batch_delete_limit_matches_backend`。）
    """
    from backend.app.services.order_write_service import MAX_BATCH_SHIP as backend_limit

    view = _read(ROOT / "frontend/src/lib/order-write.ts")
    matched = re.search(r"export const MAX_BATCH_SHIP = (\d+);", view)
    assert matched, "前端应在 lib/order-write.ts 里定义 MAX_BATCH_SHIP"
    assert int(matched.group(1)) == backend_limit, (
        f"前端 {matched.group(1)} ≠ 后端 {backend_limit}"
    )


def test_orders_page_marks_remark_as_local_only():
    """备注是**本地**写 —— 页面必须如实标注（不许让用户以为同步到平台）。"""
    code = _code_only(_read(ORDERS_PAGE))
    # ⚠️ 第一版用「或」把两个判据放一起，且 `"本地"` 太宽（页脚那句
    # 「属平台写操作」的注释里也有「本地」）⇒ 删掉标注句时 **ESCAPED**。
    # 判据收紧为**具体短语**，两条都要在。
    assert "仅存本地" in code, "订单页没有标注备注『仅存本地』"
    assert "不同步到平台" in code, "订单页没有说明备注不同步到平台"


def test_orders_page_warns_before_export():
    """导出**含完整收件信息** ⇒ 必须先提示再执行（不是「点完才知道」）。"""
    code = _code_only(_read(ORDERS_PAGE))
    assert "EXPORT_WARNING" in code, "订单页导出前没有用 lib 的告警文案"
    assert "modal.confirm" in code, "订单页导出没有二次确认"


def test_orders_page_modal_is_centered_and_viewport_sized_drawer():
    """弹窗居中 + 抽屉按视口百分比（用户既有 UI 偏好）。"""
    code = _code_only(_read(ORDERS_PAGE))
    assert 'width="60vw"' in code, "详情抽屉没有按视口百分比设宽"


def test_order_lib_missing_value_returns_dash_not_zero():
    """**lib 层**的缺值判据：`moneyText(null)` 必须是 `-`，不许退化成 `￥0.00`。

    ⚠️ 这条守的是 `lib/order-format.ts` **本身** —— 页面守卫只看页面，
    把 lib 的 `return "-"` 改成 `return "￥0.00"` 时页面守卫**看不见**
    （2026-10-04 变异实测 ESCAPED）。vitest 已覆盖运行时行为，
    这里做一层**静态兜底**（防止 lib 被改而 vitest 恰好没跑）。
    """
    code = _read(ORDERS_LIB)
    # 每个取值函数都必须有「缺值 → -」的分支
    for fn in ("moneyText", "percentText", "hoursText", "timeText"):
        block = _function_block(code, fn)
        assert 'return "-";' in block, f"order-format.{fn} 缺少「缺值 → -」分支"


def test_order_lib_status_never_invents_a_name():
    """**表外状态原样显示**（不造名）—— 判据是「没有兜底改名」的写法。

    把 `return text` 改成 `return ORDER_STATUSES.includes(text) ? text : "其他"`
    时，用户就看不到平台实际给的状态了（DEF-057 同型）。
    """
    code = _read(ORDERS_LIB)
    block = _function_block(code, "orderStatusText")
    for invented in ("其他", "未知", "OTHER", "UNKNOWN"):
        assert invented not in block, (
            f"orderStatusText 造了一个状态名：{invented}（表外值应原样返回）"
        )


# ===========================================================================
# 前端页面整合（设计 21，2026-10-04）：竞品监控并入监控中心 + 笔记发现/数据抓取合并。
#
# 为什么值得守：整合的本质是「删页面 + 加重定向 + 收敛菜单」。这四件事**改回去都不报错**——
#  · 重定向改回普通页 ⇒ 旧深链 404/白屏，没人发现；
#  · 菜单多回一项「竞品监控」 ⇒ 又出现两个重复入口；
#  · 两页各自再定义一份筛选常量 ⇒ 漂移（DEF-044 同型）。
# 故用源码断言钉住这四处「不要做某事」。
# ===========================================================================

XHS_ROUTER = ROOT / "frontend/src/app/router.tsx"
APP_SHELL = ROOT / "frontend/src/components/layout/app-shell.tsx"
DISCOVERY_PAGE = ROOT / "frontend/src/pages/platforms/xhs/discovery-page.tsx"
BULK_CRAWL_PANEL = ROOT / "frontend/src/pages/platforms/xhs/bulk-crawl-panel.tsx"
SEARCH_OPTIONS_LIB = ROOT / "frontend/src/lib/xhs-search-options.ts"


def test_consolidated_routes_redirect_old_paths():
    """旧路由（/benchmarks、/crawler）必须是**重定向**，不能是普通页。

    变异（把 Redirect 换回页面组件）必须变红 —— 否则旧书签死掉无人知。
    """
    code = _code_only(_read(XHS_ROUTER))
    assert 'path="/platforms/xhs/benchmarks" element={<Navigate to="/platforms/xhs/monitoring" replace />}' in code
    assert 'path="/platforms/xhs/crawler" element={<Navigate to="/platforms/xhs/discovery?tab=crawl" replace />}' in code
    # 两个被删页面不得再被 import（删不干净 = 死代码复活）
    assert "benchmarks-page" not in code, "benchmarks-page 仍在 router 里被引用"
    assert "crawler-page" not in code, "crawler-page 仍在 router 里被引用"


def test_menu_no_longer_has_duplicate_entries():
    """菜单「监管中心」存在，且不再有「竞品监控」「数据抓取」两个重复入口。"""
    code = _code_only(_read(APP_SHELL))
    assert 'label: "监控中心"' in code
    assert 'label: "竞品监控"' not in code, "重复入口「竞品监控」复活"
    assert 'label: "数据抓取"' not in code, "重复入口「数据抓取」复活"


def test_discovery_page_is_a_two_tab_container():
    """笔记发现 = 两 Tab 容器（搜索发现 / 批量抓取），不是单一页面。"""
    code = _code_only(_read(DISCOVERY_PAGE))
    assert "<Tabs" in code, "笔记发现未用 Tabs 承载两个功能区"
    assert "BulkCrawlPanel" in code, "批量抓取面板未挂入容器"
    assert 'key: "search"' in code and 'key: "crawl"' in code
    # tab 态进 URL（刷新保持）
    assert "useSearchParams" in code and 'searchParams.get("tab")' in code


def test_search_options_have_single_source():
    """5 组筛选常量**单一来源**：两页都从 lib import，不许各自再定义。

    变异（把常量定义改回页面内联）必须变红 —— 否则漂移无人知。
    """
    lib = _read(SEARCH_OPTIONS_LIB)
    for const in ("SORT_OPTIONS", "NOTE_TYPE_OPTIONS", "NOTE_TIME_OPTIONS",
                  "NOTE_RANGE_OPTIONS", "DISTANCE_OPTIONS"):
        assert f"export const {const}" in lib, f"共享常量 {const} 未在 lib 定义"
    for page in (DISCOVERY_PAGE, BULK_CRAWL_PANEL):
        code = _code_only(_read(page))
        assert "lib/xhs-search-options" in code, f"{page.name} 未从共享 lib 导入筛选常量"
        # 页面内不得再有常量定义（`= [{ value: 0, label: "综合排序" }` 这种）
        assert 'label: "综合排序"' not in code, f"{page.name} 内联重复定义了排序常量"


# --------------------------------------------------------------------------- #
# 2026-10-05 · 无流量清理改「后端手动触发任务」+ 独立日志表
# （用户口径：「清理无流量笔记改造成后端手动触发的任务，点击后弹窗关闭，存在正在
#   清理无流量笔记的任务时，清理无流量按钮展示为正在清理无流量笔记按钮不可点击，
#   并新增清理无流量笔记日志表」「新增一个查询清理无流量笔记日志详情按钮，
#   弹窗列表展示该日志记录」「入口只有我的笔记页的清理无流量按钮」）
# --------------------------------------------------------------------------- #
def _my_notes_code() -> str:
    return _code_only(_read(MY_NOTES))


def test_no_traffic_cleanup_closes_the_modal_on_click():
    """点「开始清理」后**弹窗关闭**（任务已交后端，留着只会挡页面）。

    变异：删掉 `setNoTrafficOpen(false)` ⇒ 本用例必须红。
    """
    code = _my_notes_code()
    start = code.index("async function handleStartCleanup")
    body = code[start : start + 2000]
    assert "setNoTrafficOpen(false)" in body, "发起清理后没有关闭弹窗"
    # 正向锚：清理确实被发起（防「整个函数被删空」也让断言变绿）
    assert "startNoTrafficCleanup(" in body


def test_no_traffic_button_switches_to_busy_and_disables():
    """有任务在跑 ⇒ 按钮文案变「正在清理无流量笔记」且**不可点**。

    三件事缺一不可：① 按钮态数据源是**独立状态**（不能靠弹窗开合派生 ——
    弹窗点完就关了）；② 文案切换；③ `disabled`。
    """
    code = _my_notes_code()
    assert "cleanupActiveTaskId" in code, "缺少「是否有清理在跑」的状态"
    assert '"正在清理无流量笔记"' in code, "按钮没有切换文案"
    assert "disabled={cleanupActiveTaskId !== null}" in code, "按钮没有随任务态禁用"
    # 数据源必须是后端在途查询（刷新页面后按钮态也得恢复）——
    # 变异：把探测删掉 ⇒ 刷新后按钮永久显示「清理无流量」，可以重复触发。
    assert "fetchNoTrafficCleanupActive(" in code, "没有查在途任务（刷新后按钮态丢失）"


def test_no_traffic_busy_state_survives_modal_close():
    """按钮态**不属于弹窗生命周期**：`noTrafficOpen` 关掉后任务态仍在。

    这是本轮最容易做错的一处 —— 把「正在清理」挂在弹窗上，弹窗一关按钮就
    退回可点，用户可以反复点（后端会挡，但界面在骗人）。
    """
    code = _my_notes_code()
    # 任务态不在任何 `noTrafficOpen &&` / `noTrafficOpen ?` 条件里
    for marker in ("noTrafficOpen && cleanupActiveTaskId", "noTrafficOpen ? cleanupActiveTaskId"):
        assert marker not in code, f"按钮态被绑在弹窗开合上：{marker}"
    assert "setNoTrafficOpen(false)" in code and "setCleanupActiveTaskId(task_id)" in code, (
        "关弹窗与置任务态必须是**两个独立动作**（前者不许顺带清掉后者）"
    )


def test_cleanup_log_modal_lists_the_independent_table():
    """日志详情按钮 + 弹窗：列表来自**独立日志表**端点（不是 tasks 行）。

    用户口径「日志表独立」的可执行判据之一：弹窗取数走 `/no-traffic/cleanup-logs`，
    而不是复用 `cleanup/{task_id}`（后者读的是会被保留期清掉的 tasks 行）。
    """
    code = _my_notes_code()
    # 「清理日志」是 JSX **裸文本节点**（不是字符串字面量）⇒ 不带引号匹配；
    # 带引号去搜会恒假，那种「守卫永远绿」是假防线。
    assert "清理日志" in code, "缺少「清理日志」入口按钮"
    assert "fetchNoTrafficCleanupLogs(" in code, "日志弹窗没有取数"
    assert "no_traffic_cleanup_logs" not in code  # 前端不该知道表名（走端点）
    assert '"清理无流量笔记 · 操作日志"' in code, "日志弹窗标题缺失"
    # 列表要打得出用户口径里那几个字段
    for column in ("操作时间", "账号", "失败原因"):
        assert f'"{column}"' in code, f"日志列表缺列：{column}"
    assert '"清理前 / 后"' in code, "日志列表缺「清理前 / 后」列"


def test_no_traffic_log_has_no_second_scheduled_entry():
    """手动入口**唯一**：没有第二个定时任务行承担无流量清理。

    注册表里该键的默认触发器必须是 `manual` —— 配了周期就会被调度器装配成
    定时跑（用户口径「没有自动清理笔记的逻辑」）。
    """
    from backend.app.services.job_registry import job_registry

    spec = job_registry()["system.no_traffic_cleanup"]
    assert spec.default_trigger_type == "manual"
    assert spec.default_cron == "", "留着 cron 就是「配了 config 就自动跑」的僵尸兜底"
    # 只有我的笔记页有入口（无其他页面提交该端点）
    root = ROOT / "frontend/src"
    hits = []
    for path in root.rglob("*.tsx"):
        if "my-notes-page" in path.name:
            continue
        if "startNoTrafficCleanup" in path.read_text(encoding="utf-8"):
            hits.append(path.name)
    assert not hits, f"清理无流量在别的页面也有入口：{hits}"


# --------------------------------------------------------------------------- #
# 2026-10-06 · 清理日志主体改「笔记维度」+ `reason` 语义更正
# （用户口径：「清理无流量的主要对象是笔记，所有主体应是笔记维度，记录操作日志时
#   也应该是笔记维度」「清理无流量笔记失败，分析原因：候选池仅包含已同步千帆挂链
#   映射的笔记…」）
# --------------------------------------------------------------------------- #
def test_cleanup_log_table_is_a_parent_child_note_dimension_tree():
    """日志列表是**父子两层**：批次行 + 展开的笔记行（用户口径「笔记维度」）。

    三件事缺一不可：① 笔记明细列有入口（「N 篇（展开查看）」）；② 展开区渲染
    **笔记维度表**（标题 / 笔记 ID / 结果 / 原因 / 操作时间）；③ 展开区是**独立的
    子表**（`expandedRowRender` 里放 `<Table>`），不是把子行塞进父表 —— 那样表头
    会串味（网关页的同款口径）。

    变异：把 `expandedRowRender` 删掉 ⇒ 本用例红（笔记维度无处可见）。
    """
    code = _my_notes_code()
    assert "expandedRowRender" in code, "没有展开区 ⇒ 笔记维度可见不了"
    assert "childrenColumnName=" in code, (
        "父表必须关掉 antd 的树数据自动探测（否则与 expandedRowRender 两条渲染路径打架）"
    )
    # 笔记维度的列（展开区那张表）
    for column in ("笔记 ID", "结果", "原因", "操作时间"):
        assert f'"{column}"' in code, f"笔记明细缺列：{column}"
    assert "NoTrafficCleanupItemRow" in code, "展开区不是按笔记明细类型渲染的"
    # 行内可见「动了几篇笔记」（否则用户不知道这里有展开）
    assert "篇（展开查看）" in code, "批次行没给出「这几篇」的入口提示"


def test_cleanup_item_results_cover_the_backend_enum():
    """结果文案映射覆盖后端**全部五个**取值，且认不出时**原样显示**（不造假）。

    特别是 `not_attempted`（本批被风控中止、这篇没轮到）必须与 `failed`
    分得开 —— 前者该重试、后者该查原因。
    """
    from backend.app.models.no_traffic_cleanup_log import CLEANUP_ITEM_RESULTS

    code = _my_notes_code()
    start = code.index("function cleanupItemResult")
    body = code[start : start + 900]
    for value in CLEANUP_ITEM_RESULTS:
        assert f'case "{value}"' in body, f"结果 {value} 没有文案映射"
    assert "default:" in body, "认不出的取值必须有兜底（原样显示，不许静默成空）"


def test_cleanup_log_failure_reason_column_never_shows_the_coverage_note():
    """**DEF-095 的前端半边**：「失败原因」列只渲染 `reason`，不许渲染 `coverage_note`。

    修复前的现场：`reason` 里装着候选池覆盖度文案，于是平台风控失败时界面显示
    「候选池仅包含已同步千帆挂链映射的笔记…」—— 用户据此追查了错误方向。

    变异：把该列的 render 改回 `dataIndex: "reason"` 直读、或把 `coverage_note`
    也拼进去 ⇒ 本用例红。
    """
    code = _my_notes_code()
    start = code.index('title: "失败原因"')
    body = code[start : start + 1200]
    assert "row.reason" in body, "失败原因列没读 `reason`"
    assert "coverage_note" not in body, (
        "失败原因列不许渲染覆盖度说明（它曾经就是这样冒充失败原因的）"
    )
    # 中止（风控/凭据）要如实说出来：本批没跑完，不是「只有这一篇失败」。
    assert "row.aborted" in body and "本批已中止" in body, "中止事实没告知用户"
