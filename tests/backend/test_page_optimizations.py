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
SHOP_ITEMS_VIEW = ROOT / "frontend/src/lib/shop-items-view.ts"
ACCOUNTS = ROOT / "frontend/src/pages/platforms/xhs/accounts-page.tsx"
GLOBAL_CSS = ROOT / "frontend/src/global.css"
SETTINGS = ROOT / "frontend/src/pages/settings/settings-page.tsx"

#: 账号下拉所在的页面（2026-09-24「只展示账号名称」口径的守护范围）。
#: 新增带账号下拉的页面时**必须**加进来 —— 漏一个就等于那条下拉没人守。
ACCOUNT_DROPDOWN_PAGES = (
    "frontend/src/pages/platforms/xhs/my-notes-page.tsx",
    "frontend/src/pages/platforms/xhs/crawler-page.tsx",
    "frontend/src/pages/platforms/xhs/discovery-page.tsx",
    "frontend/src/pages/platforms/xhs/auto-ops-page.tsx",
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
    # ② 未绑定页的长段来源说明与「登录态已失效」独立告警移除（只剩前置门禁那一句提示）
    assert "商品列表来自" not in code, "未绑定页的「商品列表来自…」三步取 Cookie 长文案应已移除"
    assert "千帆登录态已失效" not in code, "失效告警应并入前置门禁的单一提示"
    # ③ 前置门禁：判据与文案都住 lib（`shop-items-view.ts`），页面只引用
    assert "ledgerPrerequisite(" in code, "前置校验必须走 lib 的 ledgerPrerequisite"
    assert "QIANFAN_PREREQUISITE_HINT" in code, "提示文案必须引用 lib 常量"
    assert "function channelUnavailable" not in code, "门禁判据不得在页面里自建第二份"
    assert "请在账号信息卡片中扫码登录千帆后再来查看" not in code, (
        "文案只住 lib：两处都写，改一处就会漂移（同一语义不留两份实现）"
    )
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
    assert "noteCountFilter(" in code, "篇数 → 请求参数必须走 lib 的 noteCountFilter（留空不筛）"
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
    card = _slice_between(code, "商品列表（千帆）", 'title="搜索条件"')
    assert "statusHint ?" in card, "状态说明应改成裸三元（无说明时整块 body 不渲染）"
    assert 'Space direction="vertical" size={10} style={{ width: "100%" }}' not in card, (
        "首卡 body 不得再包这层空 Space：antd Card 判 childNodes.length，空壳也算节点 ⇒ 空白回来"
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
    assert "...(sortArg" in code, "无排序态必须整个省略 sort_field/order（不是送空串去兜底）"
    # ⚠️ 只断言「那段文本在」是不够的（变异实测：把 `: {}` 换成 `: { sort_field: "" }`
    # 依然能通过）。**切片**看无排序分支长什么样，才能钉住「真的不带参数」。
    # 注意 `_slice_between` 是左闭右开、会把结尾的 `}` 截掉 ⇒ 这里手动切到 `}),` 之后。
    sort_chunk_start = code.index("...(sortArg")
    sort_chunk_end = code.index("}),", sort_chunk_start) + len("}),")
    no_sort_branch = code[sort_chunk_start:sort_chunk_end]
    assert ": {}" in no_sort_branch, "无排序分支必须是空对象（真的省掉这两个键）"
    assert '""' not in no_sort_branch, (
        '无排序**不许送空串**：空串仍是「我指定了排序」，会把后端默认序的选择权抢走'
    )
    assert "sort_field: sortArg.field" in code and (
        'order: sortArg.order === "ascend" ? "asc" : "desc"'
    ) in code, "有排序态才把生效字段交给后端（后端排，前端不参与）"
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
    """去掉解释句不等于去掉状态：覆盖率与同步时间必须还在（诚实展示的底线）。"""
    source = _read(MY_NOTES)
    assert "已按需补齐" in source
    assert "条商品笔记" in source
    # 分母未知（门店总数还没被无过滤同步拿到）时只显示 N，不显示 N/0。
    assert "shop_total > 0" in source


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
    assert "goods: detail.goods ?? note.goods ?? null" in body, (
        "详情与列表的合并没有回退到 note.goods —— 详情返回 null 时会丢掉商品（DEF-047）"
    )
    spread_at = body.index("...detail")
    goods_at = body.index("goods: detail.goods ?? note.goods ?? null")
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
    search_card = _slice_between(code, '<Card size="small" style={{ marginBottom: 16 }}>', "</Card>")

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
    """
    code = _code_only(_read(SETTINGS))
    assert "为爱发电" not in code, "捐赠模块已移除，不得回归"
    assert "HeartOutlined" not in code, "该导入只为赞赏码卡片存在"
    assert "Image" not in code, "该导入只为赞赏码卡片存在"
    assert "wx_pay" not in code and "zfb_pay" not in code, "硬编码收款码路径不得回归"
    assert "安全边界" in code, "设置页的「安全边界」卡片应在位（防止整页删空也能让断言变绿）"
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
    "frontend/src/pages/platforms/xhs/auto-ops-page.tsx",
    "frontend/src/pages/platforms/xhs/drafts-page.tsx",
    "frontend/src/pages/platforms/xhs/benchmarks-page.tsx",
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
    # ⚠️ 原锚点是设置页「项目声明」（已移除），换成分析页的常驻说明块。
    analytics = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/analytics-page.tsx"))
    assert '<Alert type="info"' in analytics and "开发优化中" in analytics, (
        "分析页「开发优化中」是常驻静态说明，不该被消息化（原设置页项目声明已按用户口径移除）"
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
