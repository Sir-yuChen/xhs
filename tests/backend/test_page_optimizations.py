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
import re as _re
from pathlib import Path

import pytest


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration

def _strip_comments(text: str) -> str:
    """剥掉 `//` 行注释与 `/* */` 块注释（源码级守卫的公共前置）。

    ⚠️ 本项目多处守卫踩过同一个坑：页面 docstring / 注释里**会提到被守护的符号名**
    （尤其「旧实现是 X、已取代」这类考古说明），不剥注释就会被自己的注释命中。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


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
#: 数据中心页（设计 35，2026-10-12）：无流量清理的**现归属页** ——
#: 原在 `my-notes-page.tsx`，随「数据中心独立成页」整块搬到这里（2026-10-13 锚点重定向）。
DATACENTER_PAGE = ROOT / "frontend/src/pages/platforms/xhs/datacenter-page.tsx"
#: 设计 25（2026-10-08）第一步预检 / 第二步发布两段流程。
#: 2026-10-08：两步拆成的两个 Modal 合并为**一个 antd `Steps` 三步向导**
#: （预检 → 配置商品参数+预览 → 发布）。两个旧文件已删除，守卫统一指向它。
PUBLISH_MODAL = ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"
PRECHECK_MODAL = PUBLISH_MODAL
STEP2 = ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"
#: 纯映射模块（有 vitest，但**接线**要在源码层守：组件里必须真的调它）。
PRECHECK_MAP = ROOT / "frontend/src/lib/precheck-publish.ts"

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


def _read_qianfan() -> str:
    """读**整包**千帆后端源码（2026-10-11 拆包：`qianfan.py` → `qianfan/` 包）。

    ⚠️ 本文件有 10 处守卫断言「这段实现在 qianfan 里」（如 `_normalize` 补 customizable、
    后端上限常量、上传并行度）。拆包后源码分散到 5 个子模块：
    * 继续读旧路径 ⇒ `FileNotFoundError`（守卫红得与被测行为无关）；
    * 只读某一个子模块 ⇒ 断言范围**悄悄缩小**，从此恒绿。

    所以统一读整包，断言语义与拆包前逐字等价。
    """
    from tests.backend.conftest import read_qianfan_source

    return read_qianfan_source(ROOT)



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
    # ⑤ 搜索条件折叠（2026-10-11 用户口径「参考 antd 相关组件，默认展示一行支持折叠，
    #    默认为折叠」「搜索区域应与上图类似布局」「搜索区域使用卡片」）：
    #    * 用 antd `Form layout="inline"`（label 与控件同行，一行排不下自动换行）；
    #    * 折叠 = `{searchExpanded ? ... : null}` **条件式渲染**，初值 false（默认折叠）；
    #    * 所有条件控件统一 180px（同轮口径「所有搜索条件输入框 180px」）。
    #    ⚠️ 原先两条断言随之失效：①「恰好两个 `<Row gutter={12}>`」是按「两行排完」量的，
    #    现在用 `inline` 自动换行，行数不再是口径；②`Collapse` 组件形态已换成条件式渲染。
    assert 'layout="inline"' in code, (
        "搜索区应改用 antd `Form layout=\"inline\"`（label 与控件同行 —— 用户口径）"
    )
    assert "const [searchExpanded, setSearchExpanded] = useState(false);" in code, (
        "折叠开关初值必须是 false（用户口径「默认为折叠」）"
    )
    assert "{searchExpanded ? (" in code and "更多筛选" in code and "收起" in code, (
        "折叠应由「更多筛选 / 收起」切换的**条件式渲染**实现（展开态才出追加条件）"
    )
    assert code.count("style={{ width: 180 }}") >= 8, (
        "所有搜索条件控件应为 180px（同轮口径：账号 / 关键词 / 状态 / 三个区间 / 挂链笔记数 / 创建时间）"
    )
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

    ⚠️ **2026-10-11 第三次修订（设计 32 §5，用户口径「调整为合理的二级菜单结构」）**：
    第 ② 条原为「菜单只剩『笔记工坊』一项，三个旧菜单项**整项**移除（含 key 与标签）」，
    即断言 `图片工坊/视频工坊/草稿工坊` **不在** `app-shell.tsx` 里。
    现在用户要求二级菜单，且要求层级**与页面内页签同形** ⇒ 三个工坊（连同发布中心 /
    素材库 / 提示词模板，共 6 个）成为笔记工坊的 `?tab=` **子项**。
    「合并成一页」这条**规则没变**（⑥ 号判据把它钉得更死：三个工坊**只能**以
    `?tab=` 形态出现，出现独立路由就是拆回了三张页面），变的是「菜单里露几个入口」。
    原句保留在上面，不静默删除。

    ① 新路由渲染工坊页；三个旧路由是 `Navigate` 重定向到对应 tab
       （深链 / 书签 / 外部链接不死）；
    ② 菜单有「笔记工坊」一项，6 个页签是它的**子项**；三个旧菜单 key **整项**移除；
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

    # ② 菜单一项 + 三个工坊作为**它的子项**
    #    ⚠️ **2026-10-24 判据载体变更**（v1.1.0）：侧栏改为由后端下发
    #    （`GET /api/system/menus/mine`），`app-shell.tsx` 里**不再有菜单常量**
    #    ⇒ 原来断言 `'key: "/platforms/xhs/note-workshop"' in shell`、以及
    #    `shell.count(...) == 7` 实测直接红。规则没变（一页 + 6 个 ?tab= 子项），
    #    载体从「React 源码」换成「菜单种子」（出厂信息架构的真相源）。
    import json

    seed_rows = json.loads(
        (ROOT / "config/initial_data/menus.seed.json").read_text(encoding="utf-8")
    )["menus"]
    by_path = {r.get("path"): r for r in seed_rows if r.get("path")}

    assert "/platforms/xhs/note-workshop" in by_path, "菜单种子里没有笔记工坊"
    assert by_path["/platforms/xhs/note-workshop"]["name"] == "笔记工坊"

    #: 6 个 `?tab=` 子项（图片/视频/草稿 + 发布中心/素材库/提示词模板）
    tab_paths = {
        path: row for path, row in by_path.items()
        if path.startswith("/platforms/xhs/note-workshop?tab=")
    }
    assert len(tab_paths) == 6, (
        f"笔记工坊应有 6 个 `?tab=` 子项，实际 {sorted(tab_paths)}"
    )
    host_seq = by_path["/platforms/xhs/note-workshop"]["_seq"]
    for path, row in tab_paths.items():
        assert row["parent_seq"] == host_seq, f"{path} 必须挂在笔记工坊下面"

    label_to_path = {r["name"]: r.get("path") for r in seed_rows}
    for label, tab in (
        ("图片工坊", "images"),
        ("视频工坊", "videos"),
        ("草稿工坊", "drafts"),
    ):
        assert label_to_path.get(label) == f"/platforms/xhs/note-workshop?tab={tab}", (
            f"「{label}」应作为笔记工坊的 `?tab={tab}` 子项"
        )

    #: 反向：三个工坊**只能**以 `?tab=` 形态出现（出现独立路由 = 拆回三张页面）
    for legacy in (
        "/platforms/xhs/image-studio",
        "/platforms/xhs/video-studio",
        "/platforms/xhs/drafts",
    ):
        assert legacy not in by_path, f"菜单里不该再有独立入口 {legacy}"

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

    # ④ 生成配置弹窗：媒体来源齐全 + **形态分流**（§13.4.4 v2.14；v2.27 / 设计 41 §16 重做）
    #    ⚠️ 判据已换锚：重做后**不再有** `VIDEO_SOURCES` 常量（改由 `MEDIA_SOURCE_OPTIONS`
    #    的 `form` 字段分流），原断言「`VIDEO_SOURCES` in modal」随之失锚。新判据落在
    #    **每个来源的 form 标注**上 —— 它才是「视频有没有界面入口」的真载体。
    modal = _code_only(_read(LLM_GEN_MODAL))
    #: ⚠️ `reference_image` **不再是来源**（用户口径 2026-10-16：*「这个下拉参考图正常
    #: 是不需要的，智能生成时在其内支持选择参考图」*）—— 它本就是「智能生成 + 参考图」。
    #: 来源名单只剩 5 项；参考图能力改由 `ai_generate` 内部的选择器承载（见 ⑪）。
    for source in (
        "product_image",
        "material",
        "ai_generate",
        "video_ai_generate",
        "video_reference",
    ):
        assert source in modal, f"生成配置弹窗缺少媒体来源 {source}"
    #: **反向判据**：`reference_image` 不许再作为来源出现在选项表里
    #: （它只应作为「参考图 URL 下发」的历史键名存在于注释/类型里，不出现在下拉）。
    assert 'label: "参考图生成"' not in modal, (
        "「参考图生成」又回到来源下拉里了（用户口径：它应是智能生成内部的可选项）"
    )
    #: 两个视频来源必须标 `form: "video"`（否则选「视频笔记」时它们不出现 = 没入口）。
    assert modal.count('video_ai_generate') >= 1 and 'form: "video"' in modal, (
        "弹窗未按形态分流来源（视频来源没有 form 标注 ⇒ 视频没界面入口）"
    )
    #: 且**图文笔记不得出现视频来源** —— 平台两类互斥（设计 41 §15.2），
    #: 分流就靠 `MEDIA_SOURCE_OPTIONS.filter((o) => o.form === noteForm)`。
    assert "o.form === noteForm" in modal, "来源未按当前形态过滤（图文笔记会出现视频来源）"

    #: ⑤ **弹窗尺寸 = 全屏的 80%**（用户口径 2026-10-16；原 70% 已被 80% 取代）。
    #: 判据落在**视口百分比**上 —— 写固定像素（如 `width={680}`）即红：
    #: 固定像素在大屏上偏小、小屏上溢出，用户既有偏好一律用视口百分比。
    assert 'width="80vw"' in modal, "弹窗宽度必须是视口 80%（固定像素会在大屏偏小 / 小屏溢出）"
    assert "72vh" in modal, "弹窗 body 高度未按视口约束（会随内容无限撑高）"
    assert "centered" in modal, "弹窗未居中"
    #: ⚠️ 反向判据：`centered` 与 `style.top` 同时给会「假居中」（本仓踩过）——
    #: 这条守住「改尺寸时别顺手加 top」。
    #: ⚠️ **判据必须只查 Modal 自身的 props**：早期写法是 `"top:" not in modal`
    #: （全文扫），被弹窗内**子组件**的 CSS（如选中角标的 `top: 0`）误伤 ——
    #: 子组件里的定位与 Modal 居中无关。锚点 = **`<Modal` 到 `styles={{` 之间**。
    _mstart = modal.index("<Modal")
    _mend = modal.index("styles={{", _mstart)
    assert not _re.search(r"style=\{\{[^}]*\btop\s*:", modal[_mstart:_mend]), (
        "`centered` 已用 top:50% 居中，显式 style.top 会顶掉它（假居中）"
    )

    #: ⑥ **图片必须能预览**（用户口径 2026-10-16：*「图片均需要预览，看不见图怎么选」*）。
    #: 看不见图 = 盲选，正是本弹窗重做要解决的问题。四条判据：
    #:   ① `product_image` 段渲染商品图；
    #:   ② `material` 段渲染素材图，且**必须走 object URL**（授权端点直接
    #:      `<img src="/api/...">` 带不了 Authorization ⇒ 必然 401 破图，本仓踩过）；
    #:   ③ 生成类来源**显式说明「生成后才有」**（不装作有图）；
    #:   ④ 宿主把商品图传进来了（没传 = 预览恒空，等于没做）。
    #: ⚠️ 判据已换锚：`MediaPreviewStrip`（只读预览条）已被**选择器**取代 ——
    #: 用户口径 2026-10-16「商品原图，无法被选中」⇒ 商品段也必须是**能点的选择器**。
    assert "<ProductImagePicker" in modal, "商品原图段没接选择器（用户口径：商品原图要能选中）"
    #: ⚠️ 素材段同样是**缩略图选择器** —— 用户口径「通过预览后进行选择」⇒ 预览与选择**合一**。
    assert "<MaterialPicker" in modal, "素材段没接缩略图选择器"
    #: ⑮ **素材二级联动 + 图片/视频都在**（用户口径 2026-10-16：
    #: *「本地素材库会存在多个文件夹地址，需要用户二级联动指定文件夹，文件夹下的素材
    #: 依旧一行展示左右滚动，点击勾选」* + *「文件夹中可能有图片也可能有视频素材」*）。
    #: ① 一级：**文件夹下拉**（值 + 切换回调都是 props，切换要真的重拉）。
    #: ⚠️ 锚点随细粒度调整更新：下拉已从 `MaterialPicker` **上提到卡片头**
    #: 与「来源」下拉并排（用户口径：*「两个下拉放一起」*）⇒ 回调名 `onMaterialFolderChange`。
    assert "onMaterialFolderChange" in modal and "fetchMaterialFolders" in modal, (
        "素材没有文件夹下拉（用户口径：需要用户二级联动指定文件夹）"
    )
    #: ② 切文件夹 **必须重拉**（按 `materialFolderId` 触发）—— 不重拉 = 二级列表是旧的。
    assert 'fetchMaterialFiles({ folderId: materialFolderId' in modal, (
        "切文件夹没有按 folder_id 重新拉取（二级列表不会变）"
    )
    #: ③ **不许按 `file_type` 过滤掉视频**（口径：文件夹里也可能有视频素材）——
    #: 原写法 `.filter((f) => f.file_type === \"image\")` 会把视频**静默藏掉**。
    assert 'file_type === "image")' not in modal, (
        "素材被按类型过滤（视频会被藏掉，用户看得见却选不到）"
    )
    #: ④ 二级素材也**一行横滑**，且**文件夹下拉与缩略图带同处一行**
    #: （用户口径 2026-10-16：*「文件夹 放一行，不要放两行占用页面空间」*）——
    #: 反例是外层 `flexDirection: "column"`（下拉一行、缩略图一行 = 白占两行）。
    assert 'flexDirection: "column", gap: 6 }}>\n      <Select' not in modal, (
        "素材选择器又变成上下两行了（用户口径：文件夹放一行）"
    )
    assert "fetchMaterialImageObjectUrl(materialFileUrl(f.id))" in modal or (
        "fetchMaterialImageObjectUrl(materialFileUrl(id))" in modal
    ), "素材预览必须走 object URL（<img src=\"/api/...\"> 带不了 Authorization ⇒ 401 破图）"
    assert "revokeObjectURL" in modal, "object URL 未释放（泄漏，长会话下明显）"
    assert "生成后才有" in modal, "生成类来源未说明「现在没有图可预览」（会让人以为坏了）"
    #: ⚠️ 图片与**视频**各有各的说明 —— 只写一条时另一类来源仍是「空白无解释」。
    #: 分成两条断言（一条判据只守一件事）：合成一条会被另一条掩盖成**哑弹**（M8 同型）。
    #: ⚠️ 文案已按口径 C1（越少越好）**压缩**，锚点随之更新为压缩后的短句。
    #: ⚠️ **反向断言**（用户口径 2026-10-19：*「这张图要生成后才有，现在无法预览文本移除」*）
    #: —— 原来是「必须有这句」，现在**必须有且仅有**「不许出现这句」：
    #: 生成前当然没有图，这句话是说废话，白占一行高度。
    #: 旧断言（已被取代）：`assert "这张图要生成后才有" in modal`
    assert "这张图要生成后才有" not in modal, "已按用户口径移除的说明文案又出现了"
    assert "生成后才有；异步任务约几分钟" in modal, "视频来源未说明「现在无法预览 + 异步提醒」"
    assert "productImages={llmGenerateItem?.images" in _code_only(_read(SHOP_ITEMS_PAGE)), (
        "宿主没把商品图传给弹窗 ⇒ 商品原图预览恒空"
    )

    #: ⑦ **各区块用卡片分割 + ③ 区分形态布局**（用户口径 2026-10-16：
    #: *「① 笔记类型 弹窗中使用卡片区域给这些区域进行分割」*、*「只留标题，不要序号」*、
    #: *「③ 视频与封面 区域重新做布局」*）。
    #: 判据：
    #:   ① 区块都是 `Card`（不是裸 `<div>` + 标题 —— 卡片才有**区域感**）；
    #:   ② **标题不带序号**（`①`/`②` 等一律不出现在标题里 —— 用户明确不要）；
    #:   ③ ③ 区**按形态分两套布局**：视频笔记 `Row/Col` 两列并排（视频 | 封面）。
    assert modal.count("<Card") >= 2, "区块未用卡片区域分割（用户口径：用卡片区域给这些区域进行分割）"
    for serial in ("①", "②", "③", "④"):
        assert serial not in modal, f"标题里还有序号 {serial}（用户口径：只留标题，不要序号）"
    assert '"视频与封面" : "配图"' in modal, "③ 区标题未按形态分叉"
    #: ⚠️ 判据已换锚两次，最终形态是**上下布局**：
    #:   1. `Row/Col` 两列 ⇒ `Row gutter` 的负外边距把左列顶出容器、裁掉标签（实证）；
    #:   2. CSS grid 两列 ⇒ grid 子项默认 `min-width:auto`，里面有横滑带 ⇒ 撑爆列宽（实证）；
    #:   3. **用户口径 2026-10-16：*「视频与封面 上下布局不要左右布局」*** ⇒ 竖排。
    #: 判据落在**视频分支用竖排容器**上（同时反向钉住「不许再用 Row/grid 两列」）。
    assert 'noteForm === "video"' in modal, "视频分支的形态判断不见了"
    video_branch = modal[modal.index('noteForm === "video"') :]
    video_branch = video_branch[: video_branch.index(") : (")]
    assert 'direction="vertical"' in video_branch, (
        "视频笔记的 ③ 区不是上下布局（用户口径 2026-10-16：视频与封面 上下布局不要左右布局）"
    )
    assert "gridTemplateColumns" not in video_branch, (
        "视频分支还在用 grid 两列（子项 min-width:auto + 内部横滑带 ⇒ 撑爆列宽）"
    )
    assert "<Row gutter" not in modal, (
        "视频分支又用回 Row gutter（负外边距会裁掉左列的标签左边缘）"
    )
    #: ⑧ 单个来源块**抽成共用组件**（图文/视频两套布局共用一份实现）——
    #: 两处各写一遍必然漂移（DEF-044）。
    assert "function SegmentCard(" in modal, "媒体来源块未抽成共用组件（两套布局会各写一遍）"
    assert modal.count("<SegmentCard") >= 2, "SegmentCard 未在两种形态下复用"

    #: ⑨ **弹窗内用两个大类 Tab 拆分「图文 / 视频」**（用户口径 2026-10-16：
    #: *「使用两个大类 tab 将图文和视频笔记进行拆分」* —— 只拆**参数界面**，
    #: 后端链路**公用一条**）。
    #: 判据：弹窗内必须有 `Tabs` 且两个 key 恰好是 `image` / `video`
    #: （写在 `noteForm` 的 state 上 —— 切 Tab 换的是参数界面，不是链路）。
    assert "<Tabs" in modal, "弹窗内没有两个大类 Tab"
    assert '{ key: "image", label: "图文笔记" }' in modal, "缺「图文笔记」Tab"
    assert '{ key: "video", label: "视频笔记" }' in modal, "缺「视频笔记」Tab"
    assert "const [noteForm, setNoteForm] = useState" in modal, (
        "Tab 未驱动形态 state（切 Tab 不会换参数界面）"
    )
    #: ⚠️ **光有 state 不够** —— 必须证明 **Tab 的 onChange 真的写它**
    #: （第一版只查了 `useState` 存在，M19 变异把 `onChange` 改成 no-op 却没被抓住 ⇒ 哑弹）。
    #: ⚠️ 判据换锚（2026-10-17 加「生成记录」页签后）：`onChange` 先写 `activeTab`，
    #: 再**非 history 时**转发给 `setNoteForm` —— 判据必须落在**转发那一步**上，
    #: 只查 `setNoteForm` 出现会漏掉「切 Tab 不再换形态」（等于没绑定）。
    assert 'if (next === "image" || next === "video") switchForm(next);' in modal, (
        "Tab 的 onChange 没有写回 `noteForm`（切 Tab 不换参数界面）"
    )
    #: ⚠️ **后端链路必须仍是公用的**：不许在这个前端弹窗里出现「两条链路」的痕迹
    #: （如按形态分别调不同接口）—— 提交只发 `mediaPlan`，形态由后端派生。
    assert modal.count("generateLlmNote(") == 1, (
        "提交出现了多个生成入口（用户口径：只是 UI 拆分，链路公用）"
    )
    #: ⑨ **本地素材要「先预览、再选择」**（用户口径 2026-10-16：
    #: *「本地素材库的素材也应该支持预览，通过预览后进行选择」*）。
    #: 反向判据：**不许退回只显文件名的 `Select`**（那正是用户说的「看不见图怎么选」）。
    assert "MaterialPicker" in modal, "本地素材未用缩略图选择器（用户要求先预览再选择）"
    assert "materialFiles.map((f) => ({ value: f.id, label: f.filename }))" not in modal, (
        "本地素材退回只显文件名的 Select（看不见图就得选）"
    )
    #: object URL 必须释放（授权端点取 blob，不 revoke 会泄漏）。
    assert "revokeObjectURL" in modal, "素材 object URL 未释放（长会话下泄漏）"
    #: ⑪ **参考图在「智能生成」内部选**（用户口径 2026-10-16：*「智能生成时需要参考AI商品图
    #: 相关逻辑支持选择参考图」* + *「这个下拉参考图正常是不需要的」*
    #: + *「参考图方放在提示词的上面，包括主图详情图，支持多选最多4个」*）。
    #: 判据落在**调用形态**上（「有组件」≠「真的挂进 ai_generate 分支」，本仓 DEF-056 同型）：
    #: 选择器必须出现在 `seg.source === "ai_generate"` 的块里。
    assert "<ReferenceImagePicker" in modal, "智能生成未提供参考图选择（用户要求在其内选参考图）"
    #: ⚠️ 截块锚点必须选**ai_generate 分支之后**的那些分支（`product_image` / `material`）。
    #: 2026-10-17 起「参考图来源」下拉里也出现了 `seg.source === "material"` 字样
    #: （父组件那行），拿它当结束锚点会**截在当前分支内部** ⇒ 假红。
    #: 改用 `{seg.source === "product_image" && (` 这个稳定形态当边界。
    #: ⚠️ 必须锚**分支形态** `{seg.source === "ai_generate" && (`，不能只锚字符串
    #: —— 2026-10-17 起父组件里也有一句 `(seg.source === "ai_generate" || ...)`（参考图
    #: 来源下拉那行，在分支之前）⇒ 锚到它就把块截在分支外部，恒假红。
    _ai_start = modal.index('{seg.source === "ai_generate" && (')
    _boundary = modal.find('{seg.source === "product_image" && (', _ai_start)
    _ai_block = modal[_ai_start : _boundary if _boundary > 0 else len(modal)]
    assert "<ReferenceImagePicker" in _ai_block, (
        "参考图选择器没挂在「智能生成」分支里（挂外面 = 别的来源也受影响）"
    )
    #: **参考图要在提示词上面**（口径原话「放在提示词的上面」）—— 顺序即用法：
    #: 先挑参考、再写提示词。判据 = 选择器在 `Input.TextArea` **之前**出现。
    assert _ai_block.index("<ReferenceImagePicker") < _ai_block.index("Input.TextArea"), (
        "参考图在提示词下面了（用户口径：参考图放在提示词上面）"
    )
    #: 参考图**上限 4 且可多选**（口径原话「支持多选最多4个」）。
    assert "const MAX_REFERENCE_IMAGES = 4;" in modal, "参考图上限不是 4（用户口径：最多 4 个）"
    assert "picked.length >= MAX_REFERENCE_IMAGES" in modal, (
        "参考图没有数量上限判断（会无限选下去）"
    )
    #: **主图 / 详情图合并成一行、左右滚动**（用户口径 2026-10-16：
    #: *「商品原图是包括商品主图和详情图」* + *「主图和详情图合并成一行展示」*
    #: + *「商品原图展示一行，左右滚动即可」*）。
    #: 判据 = **两个选择器都用合并列表 + 横向滚动**（不是「有个组标题」就算了）。
    #: ⚠️ 判据换锚（2026-10-16）：`ReferenceImagePicker` 现在拼的是**三组**且每项是
    #: `{url, preview}`（本地素材要 object URL 预览）⇒ 不再是 `[...productImages, null, ...detailImages]`
    #: 这个字面量。判据改为落在**两处都用了「主图 + 分隔 + 详情图」的拼接形态**上：
    #: 商品原图选择器仍是 `[..., null, ...]`；参考图选择器是三项 map + 分隔位。
    #: ⚠️ 判据再换锚（2026-10-17 去重）：合并列表改由 `productImageList()` 统一产出
    #: （主图 + 详情图 + **保序去重**），不再是 `[...productImages, null, ...detailImages]`
    #: 字面量；参考图那边也不再三组混排（改「来源下拉 + 按来源展示」）。
    #: 判据改为**落在统一入口与分隔线位置**上：
    assert "productImageList(productImages, detailImages)" in modal, (
        "商品原图没走统一合并入口（主图/详情图合并 + 去重）"
    )
    assert "...merged.slice(0, mainUnique), null, ...merged.slice(mainUnique)" in modal, (
        "主图与详情图之间没有分隔线（或分隔线位置没按去重后的主图张数算）"
    )
    assert modal.count("overflowX: \"auto\"") >= 2, (
        "合并行没有左右滚动（用户口径：展示一行，左右滚动即可）"
    )
    #: ⚠️ 分隔位**不能占 `pick` 的下标** —— 算错会让「选第 3 张实际出第 5 张」。
    assert "position < detailOffset ? position : position - 1" in modal, (
        "合并列表的下标没跳过分隔位（pick 下标会整体错位）"
    )
    #: ⚠️ 缩略图不许被 flex 压扁（`flexShrink: 0`）。
    assert modal.count('flex: "0 0 auto"') >= 4, "缩略图未禁止收缩（会被压扁变形）"
    #: 勾选的是 **URL**（两组混选，单一「下标」表达不了组内位置）。
    assert "if (seg.reference_urls?.length) base.reference_image_urls = seg.reference_urls;" in modal, (
        "参考图未随 media_plan 下发（选了也不生效 = 静默失效）"
    )
    #: ⚠️ 到上限后**剩余图禁用 + 点击给提示**（用户口径：*「图片总数达到限制后
    #: 不再支持选择进行提示」*）—— 不能静默吞掉点击。
    assert "最多选" in modal, "到上限后没有提示（用户点了没反应 = 以为坏了）"
    #: ⑬ **商品原图可选中 + 封面只能选 1 张**（用户口径 2026-10-16：
    #: *「商品原图，无法被选中，封面图应限制，只允许选择一个」*）。
    #: ⚠️ 内页的上限**不写死**（= 配置的图片总数 `imageLimit`）—— 写死会让
    #: 「配置调到 10」时内页仍只能选 6 张（配置形同虚设）。
    assert 'seg.role === "cover"' in modal, (
        "封面未被限制为 1 张 / 内页上限未跟随配置（用户口径：封面只允许选一个）"
    )
    #: 单选语义也必须对：`max=1` 时是**替换**不是追加 —— 追加会违反「只能选一个」。
    #: ⚠️ 2026-10-18 起 `pick` 传的是**图片名称**（用户口径「必须是图片名称」），
    #: 不再是下标，故断言里的变量是 `name`。
    assert "onChange(picked.includes(name) ? [] : [name])" in modal, (
        "封面单选逻辑不对（应先清空再放选中项，否则会累加出多张封面）"
    )
    #: 用户的选择**必须下发**（不传 = 后端取前 6 张，选择白做 = 静默失效）。
    assert "if (seg.pick?.length) base.pick = seg.pick;" in modal, (
        "商品图选择未随 media_plan 下发（后端只会取前 6 张）"
    )
    #: ⑭ **张数动态计算 + 总数后端下发**（用户口径 2026-10-16：*「生成张数根据笔记的
    #: 需要的总图数动态计算…总数是固定的」* + *「可以低于上限，不能超过，最低一张图」*
    #: + *「配置到 config 表中支持动态调整」*）。
    #: ① 总数**不写死在前端**（写死 = 改了配置界面还说旧数，两份真相源）；
    assert "fetchSystemSettings" in modal and "xhs_note_image_total_limit" in modal, (
        "笔记图片总数没有从配置读（写死 = 配置改了界面不跟着变）"
    )
    #: ② 上限 = 总数 − **其他段**已占（排除本段，否则填完就锁死自己）；
    assert 's.source !== "ai_generate"' in modal, (
        "张数上限没有排除本段自身（填 3 之后上限剩 3，再想填 4 就填不进去）"
    )
    #: ③ **可填但夹在 1–6 内，默认 3**（不是只读展示）。
    #: ⚠️ 旧断言（已被取代）要求按 `aiCountCap` 夹取 —— 用户口径 2026-10-19
    #: *「生成张数不应该算该篇笔记的图片总数…默认3张最少1张最多6张」* ⇒
    #: 生成张数与笔记图数**解耦**，固定 1–6，不再跟随总数变化。
    assert "Math.min(seg.count ?? 3, 6)" in modal and "max={6}" in modal, (
        "张数没有按 1–6 夹取（生成张数应与笔记图数解耦，固定最多 6 张）"
    )
    #: ④ 生成张数**不再随总数禁用**（用户口径 2026-10-19：生成张数与笔记图数解耦，
    #: 固定 1–6）—— 旧断言（已被取代）要求 `disabled={aiCountCap < 1}`，
    #: 那是「生成张数算进图片总数」时代的口径。现在备选池大小与笔记放几张无关。
    assert "disabled={aiCountCap" not in modal, (
        "生成张数仍被总数联动禁用（已改为 1–6 固定，与笔记图数解耦）"
    )
    #: ⑯ **「高级设置」整块移除 + 不许指定模型名**（用户口径 2026-10-16：
    #: *「高级设计移除，不允许随意设置模型名」*）。
    #: 模型由**业务点映射**决定；弹窗给输入框 = 给映射开后门（填个没配的名 ⇒ 运行期才 502）。
    assert "<Collapse" not in modal, "「高级设置」折叠块又回来了（用户口径：高级设置移除）"
    assert 'name="model_name"' not in modal, "弹窗又能填模型名了（用户口径：不允许随意设置模型名）"
    assert "modelName:" not in modal, (
        "提交体里还在下发 modelName（模型应由业务点映射决定，不走客户端）"
    )
    #: ⑰ **视频分支的封面下拉不许筛选空**（2026-10-16 截图实证的真 bug：
    #: 封面那格显示成原始值 `product_image`）。
    #: 根因：`options` 写成 `sourceOptions.filter(...)`，而 `sourceOptions` 已按
    #: `noteForm`（=video）过滤过 ⇒ 再筛 `form === "image"` 恒得**空数组** ⇒
    #: antd `Select` 匹配不到选项就把**原始 value** 直接渲染出来。
    #: 判据 = **`filter` 的输入必须是「全集」`MEDIA_SOURCE_OPTIONS`**。
    assert "sourceOptions.filter((o) => o.form ===" not in modal, (
        "又从「已按形态筛过的子集」里再筛一次（视频分支的封面会拿到空数组、显示原始值）"
    )
    #: ⚠️ 锚点用**正则**（不写死缩进/换行）：`SegmentCard` 参数块改过缩进后，
    #: 写死 `\n` + 20 空格的锚点会失配成哑弹（本仓变异锚点已三次栽在这上面）。
    assert _re.search(
        r"MEDIA_SOURCE_OPTIONS\.filter\(\s*\(o\)\s*=>\s*o\.form === \(isCover", modal
    ), (
        "视频分支的封面下拉没从全量来源里筛（会显示成 product_image）"
    )
    #: ⑫ **文案与信息密度硬约束**（用户口径 2026-10-16：
    #: *「说明或提示文案不要占用过多的页面布局，越少越好」*，已写入
    #: `00_管理规范/00_需求工作流速查.md` 硬约束区 + 设计模板 §2.5a）。
    #: 判据（可执行，不是纸面规矩）：
    #:   ① 弹窗内**没有独立 `Alert` 说明块**（原写法两条 Alert 白占 4 行高度）；
    #:   ② 没有**超长**说明句（单条 hint 超过 40 字即视为该进 Tooltip / 注释）。
    assert "<Alert" not in modal, (
        "弹窗内出现独立 Alert 说明块（口径 C3/C5：提示不占独立区块、弹窗禁大段说明）"
    )

    #: ⑰ **视频来源只剩两项 / 本地素材不生成视频 / 参考图含本地素材**（用户口径 2026-10-16：
    #: *「视频下拉应只有本地素材和智能生成视频，使用本地素材这无需再生成视频…
    #:   智能生成视频需支持选择参考图，参考图可为本地素材也可以是商品原图」*）。
    #:
    #: ⚠️ 这条**必须验枚举内容**，不是「有没有 video_ai_generate」：
    #:     只查存在 ⇒ 保留 `video_reference` 也能过（那正是要退场的东西）。
    video_options = _re.findall(
        r'value: "(video_[a-z_]+)"', modal
    )
    assert set(video_options) == {"video_material", "video_ai_generate"}, (
        f"视频来源下拉应只有「本地素材 / 智能生成视频」两项，实得 {sorted(set(video_options))}"
    )
    #: 本地素材 = 直接用（不生成）⇒ 不能再有「时长」这类**生成参数**
    #: （判据落在**条件渲染**上：时长只在 `video_ai_generate` 时出现）。
    assert 'seg.source === "video_material"' in modal, (
        "本地素材没有独立分支（会被当成生成来源，白烧一次视频模型调用）"
    )
    #: **生成记录挂在弹窗里 + 只显示该商品**（用户口径 2026-10-17：
    #: *「页面记录在 LLM 生成笔记中实现只展示该商品对应生成的笔记」*）。
    #: 判据：① 弹窗里挂了 `NoteVersionHistory`；② 传的是**商品标识**（不是不带参数）。
    assert "<NoteVersionHistory" in modal, "弹窗里没有生成记录组件"
    assert "platformAccountId={accountId}" in modal and "itemId={itemId}" in modal, (
        "生成记录没有按商品定位（不传就会列出所有商品的记录）"
    )
    #: ⚠️ 记录组件必须**默认收起**（弹窗高度稀缺，口径 C5）
    vh = (ROOT / "frontend/src/pages/platforms/xhs/note-version-history.tsx").read_text(
        encoding="utf-8"
    )
    #: ⚠️ 判据已换口径（2026-10-17 用户：*「生成记录使用新 tab 展示」*）：
    #: 记录现在是**弹窗的一个页签**，不再自带「展开/收起」折叠开关
    #: ⇒ 反向断言不许再出现该开关（否则「Tab 里还藏一层折叠」，多一次点击）。
    assert "收起生成记录" not in vh, "生成记录里还留着折叠开关（它现在是整页 Tab）"
    #: **分页列表 + 抽屉详情**（用户口径 2026-10-17：
    #: *「生成记录使用分页列表展示使用抽屉展示详情」*）。
    #: 判据：① 用了 `Table`（不是横滑卡片堆）；② 配了 `pagination`；③ 详情走 `Drawer`。
    assert "<Table" in vh or "<Table<" in vh, "生成记录不是列表（横滑卡片看不到总数、不能翻页）"
    assert "pagination={{" in vh, "生成记录列表没有分页"
    assert "<Drawer" in vh, "详情没有用抽屉"
    assert "fetchNoteVersion(" in vh, "抽屉没取单版详情（列表只有摘要，正文取不到）"
    assert 'key: "history"' in modal, "生成记录不是一个页签"
    #: 生成参数不能出现在记录页（否则用户以为能在历史页直接点生成）
    #: 生成参数区只在**图文 / 视频**两个页签出现（生成记录 / 本次结果都不该有）
    assert '''(activeTab === "image" || activeTab === "video")''' in modal, "切到生成记录/结果时没有隐藏生成参数区"
    #: 失败版本也要列（用户点过生成却没结果，看不到失败会以为没反应）
    assert 'case "failed"' in vh, "生成记录不显示失败状态"

    #: 参考图**来源下拉**（用户口径 2026-10-17：*「参考图要有一个下拉，是选择原图还是
    #: 本地素材需指定具体文件夹」*）—— 两张截图实证：原先三组混排，用户分不清
    #: 哪张是素材、也没地方选文件夹。
    #: 判据：① 来源下拉两项齐全；② 选「本地素材」时**必须能指定文件夹**。
    assert 'value: "product", label: "商品原图"' in modal, "参考图来源下拉缺「商品原图」"
    assert 'value: "material", label: "本地素材"' in modal, "参考图来源下拉缺「本地素材」"
    assert "onMaterialFolderChange" in modal and "materialFolders" in modal, (
        "参考图选「本地素材」时没有文件夹选择（用户口径：需指定具体文件夹）"
    )
    #: 缩略图**必须走 CDN 缩略图**（用户口径 2026-10-17：*「小图预览都很模糊，正常应清晰」*）。
    #: 实测：商品详情图原图 **2480×2824**，槽位只有 **56×56 CSS** ⇒ 浏览器 44 倍下采样
    #: ⇒ 边缘糊 + 摩尔纹。小红书 CDN 支持 `?imageView2/2/w/{size}`（4KB vs 400KB）。
    #: 判据：渲染 `<img>` 的 src 必须过 `thumbUrl()`，且**按 DPR 放大**取图。
    assert "function thumbUrl(" in modal, "没有缩略图地址函数（会把 2480px 原图硬塞进 56px 槽）"
    assert "imageView2" in modal, "缩略图没走 CDN 的 imageView2 参数"
    assert "devicePixelRatio" in modal, (
        "缩略图没按设备像素比取图（只按 CSS 尺寸取，在高分屏上又会糊）"
    )
    #: 两处渲染（商品原图选择器 / 参考图选择器）都必须用缩略图
    assert modal.count("thumbUrl(") >= 3, (
        "缩略图函数未被两处 `<img>` 使用（只定义不用 = 白写）"
    )
    #: ⚠️ 提交给后端的仍是**原图**（缩略图当参考图 = 模型拿到模糊输入）
    assert "pick" in modal and "reference_urls" in modal, "提交字段不见了"

    #: ⚠️ **`needsMaterial` 必须包含 `ai_generate`**（2026-10-17 截图实证的真 bug：
    #: 参考图来源选「本地素材」时，文件夹下拉显示「暂无数据」、下方「文件夹下没有图片素材」）。
    #: 根因：`ai_generate` 的**媒体来源**不是素材，但它的**参考图**可以来自本地素材
    #: ⇒ 漏掉它就不拉清单，界面给个空下拉（静默空态，比报错更难查）。
    #: 判据 = **凡界面上会出现素材的地方都算**，不是「媒体来源是素材」才算。
    import re as _re2

    _needs = _re2.search(
        r"const needsMaterial = segments\.some\(([^;]+?)\);", modal, _re2.S
    )
    assert _needs, "找不到 needsMaterial 判定（拉素材清单的门禁）"
    for _src in ("material", "ai_generate", "video_material", "video_ai_generate"):
        assert f'"{_src}"' in _needs.group(1), (
            f"needsMaterial 漏了 {_src} ⇒ 该来源下素材清单是空的（空下拉，静默失效）"
        )
    #: 参考图必须能拿到素材（object URL 预览 + 提交 /api 引用）
    assert "materialImages" in modal and "materialFileUrl" in modal, (
        "参考图未接入本地素材（用户口径：参考图可为本地素材也可以是商品原图）"
    )
    #: **商品原图去重**（用户口径 2026-10-17：*「商品原图实现图片去重」*）。
    #: 真机实测 400/400 商品都有重复图（主图与详情图重叠）⇒ 不去重就是同一张图
    #: 出现两三次。⚠️ 必须**保序**（`pick` 是下标，排序会静默错位）。
    assert "function uniqueKeepOrder(" in modal, "前端没有保序去重实现"
    assert "function productImageList(" in modal, "商品原图没有统一入口（两处选择器会各写一份）"
    #: 两处选择器都必须走过去重入口（只改一处 = 一处去重一处不去重，下标必然错位）
    assert modal.count("productImageList(productImages, detailImages)") >= 2, (
        "商品原图选择器与参考图选择器没有都走 productImageList（会与后端下标错位）"
    )
    #: ⚠️ 素材预览必须走 **object URL**：`<img src="/api/...">` 带不了 Authorization
    #: ⇒ 必然 401 破图（本仓踩过，用户口径「本地素材均需要支持预览」）。
    assert "fetchMaterialImageObjectUrl" in modal, (
        "素材预览没用 object URL（会 401 破图）"
    )

    #: 只查**面向用户的文案属性**（`title=` / `placeholder=` / `description=` /
    #: `extra=` / `emptyHint=` / `message=`）—— CSS 值、类名、路径不算。
    for text in _re.findall(
        r'(?:title|placeholder|description|extra|emptyHint|message)=\{?"([^"\n]+)"\}?', modal
    ):
        assert len(text) <= 40, (
            f"存在超长用户文案（{len(text)} 字，口径 C1：越少越好）：{text[:50]}…"
        )
    #: ⚠️ **JSX 文本子节点也要查**（`<Text strong>…</Text>` / `<Tag>…</Tag>`）——
    #: 只在属性上判会漏掉**写在标签体里**的长句（M20 首版锚点正是从这里溜过去的）。
    for text in _re.findall(r">\s*([^<>{}\n]{41,})\s*<", modal):
        #: 缩进/换行会被算进长度 ⇒ 先 strip 再判（否则缩进多一层就误报）。
        text = text.strip()
        assert len(text) <= 40, (
            f"存在超长用户文案（{len(text)} 字，口径 C1：越少越好）：{text[:50]}…"
        )


# --------------------------------------------------------------------------- #
# 2026-10-03 v2.16：「生成AI商品图」弹窗左右两栏布局
# --------------------------------------------------------------------------- #
SHOP_ITEMS_PAGE = ROOT / "frontend/src/pages/platforms/xhs/shop-items-page.tsx"
LIBRARY_PAGE = ROOT / "frontend/src/pages/platforms/xhs/library-page.tsx"


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
        # 2026-10-07 工作流退场 ⇒ 宿主页连 Tabs 也撤了，退回直接渲染 AutoOpsPanel。
        ("frontend/src/pages/platforms/xhs/auto-ops-panel.tsx", ("XHS Auto Operations",)),
        ("frontend/src/pages/platforms/xhs/keywords-page.tsx", ("XHS Keywords",)),
        ("frontend/src/pages/platforms/xhs/monitoring-page.tsx", ("XHS Monitoring",)),
        ("frontend/src/pages/platforms/xhs/orders-page.tsx", ("小红书运营",)),
        ("frontend/src/pages/platforms/xhs/publish-page.tsx", ("XHS Publish",)),
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
    #
    # ⚠️ **2026-10-11 第三次重定向**（设计 32 §6 / A6）：分析页已拆成
    #    `insights-content-page.tsx`（内容侧）/ `insights-shop-page.tsx`（商品侧），
    #    旧 `analytics-page.tsx` 只剩跳转 ⇒ 红块锚点跟着搬到**内容洞察页**。
    #    「结构性红块不随 message 化搬走」这条规则没变，只是换了归属文件。
    #    被取代的原句（读 analytics-page.tsx）保留在 docstring 里，不静默删除。
    insights = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/insights-content-page.tsx"))
    assert 'type="error"' in insights and "内容洞察加载失败" in insights, (
        "内容洞察页的加载失败红块是结构呈现（三分法），不该被消息化"
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


def test_model_config_page_shows_empty_dropdown_without_explanation():
    """下拉为空时**不再解释原因**（用户口径 2026-10-18）。

    > 「该类型的 8 个服务商都还没有可用模型… **移除**，没有对应模型下拉框展示为空即可」

    ## 为什么推翻旧断言

    旧口径（v2.11）要求 placeholder 写「暂无可用的该类型服务商」并配一段
    「请先到『服务商配置』新增」——那是**诊断信息**，用户明确不要了：
    下拉为空本身就是答案，多一段解释只是噪音（同「提示文案越少越好」）。
    """
    code = _code_only(_read(MODEL_CONFIG_PAGE))
    #: 这两句是本次明确要移除的诊断文案
    assert "都还没有可用模型" not in code, "「N 个服务商都还没有可用模型」应已移除"
    assert "暂无可用的该类型服务商" not in code, "「暂无可用的该类型服务商」placeholder 应已移除"
    #: 下拉仍在、placeholder 保留一句中性的
    assert "请选择服务商" in code, "下拉框本身不能删（删了就没法配了）"


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
    """卡内**说明段落已移除**，且**不再用按行诊断文案替代**（用户口径 2026-10-18）。

    > 「该类型的 8 个服务商都还没有可用模型… **移除**，没有对应模型下拉框展示为空即可」

    ⚠️ **本条第三次订正**：
    ① v2.6 要求删掉集中式 Alert（用户「这段说明也不要了」）；
    ② v2.11 改成守「按行空态文案必须保留」（当时的口径是要逐行说清）；
    ③ **本次（2026-10-18）用户明确要求把按行文案也删掉** —— 下拉为空即可。
    断言随之改为「长说明不回来 + 诊断文案也不回来」。
    """
    code = _code_only(_read(MODEL_CONFIG_PAGE))
    assert "服务商在「服务商配置」页单独维护" not in code, "卡内说明段落已按用户口径移除，不应回退"
    assert "都还没有可用模型" not in code, "按行诊断文案已移除（用户：下拉为空即可）"
    assert "该业务点下没有可用模型" not in code, "按行诊断文案已移除（用户：下拉为空即可）"


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
    ③ 提交时把 `template_id` 放进**媒体段**；④ 手输**不被裁剪**
       （后端要靠它做「补充要求」）。

    ⚠️ **判据已换锚（v2.27 / 设计 41 §16 弹窗重做）**：旧版是 `Form.Item
    name="video_template_id"` 的顶层字段，提交时读 `values.video_template_id`；
    重做后视频参数**挂在媒体段上**（每行一个来源，参数就地展开）—— 锚点改为
    `tag`/`seg.template_id` 形态。原句保留在 docstring。
    """
    modal = _code_only(_read(LLM_GEN_MODAL))

    # ① 下拉存在（挂在视频段上，值写进 `seg.template_id`）
    assert "seg.template_id" in modal, "视频区没有模板下拉 —— 模板没用上"
    # ② 按 video 过滤
    assert 't.task_type === "video"' in modal, (
        "视频模板下拉没按 task_type=video 过滤（会选到图片模板）"
    )
    # ③ 提交时发出去（进媒体段）
    assert "base.template_id = seg.template_id" in modal, (
        "视频模板没被发给后端（选了等于没选）"
    )
    # ④ 手输不被裁剪（与模板并存 —— 后端要用它当「补充要求」）
    assert "base.prompt = seg.prompt" in modal, (
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
            #: ⚠️ 结束判据必须要求 `>` 出现在**行尾**（`>\s*$`）。
            #: 旧写法 `(?<!/)>` 会被 JSX 表达式内部的 `>` 提前命中 ——
            #: 例如 `onCancel={() => setUploadDetailOpen(null)}` 里 `=>` 的那个 `>`，
            #: 会让块在第 3 行就「结束」，于是后面几行的 `centered` 看不见 ⇒ **假报未居中**
            #: （2026-10-11 实测踩到：新加的详情弹窗明明写了 centered 却报缺失）。
            end = st
            while end < len(lines) and not _re.search(r"[^=]>\s*$", lines[end]):
                end += 1
            #: 兜底：万一整段都没找到「行尾 >」，就用文件末行（避免 end 越界）。
            end = min(end, len(lines) - 1)
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
    #: ⚠️ 2026-10-20：自建 `absolute` 遮罩已统一为 `<WaitMask scope="panel" mask>`。
    #: 判据跟着换承重点：`scope="panel"` + `mask` + 由 `detailImageGenerating` 驱动。
    assert 'open={detailImageGenerating}' in page, "生成中未挂等待蒙版"
    gen_at = page.index('open={detailImageGenerating}')
    gen_block = page[max(0, gen_at - 300) : gen_at + 400]
    assert 'scope="panel"' in gen_block, (
        "生成蒙版必须是 panel 作用域（只盖弹窗，不盖整页）"
    )
    assert 'mask' in gen_block, "生成期间必须挡交互（mask）"
    #: 仍需一个 `position: relative` 的定位父级（`scope="panel"` 是 absolute 定位）。
    assert 'position: "relative"' in page, (
        "缺 position:relative 父级 —— panel 蒙版会以页面为基准铺满全屏"
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
# M2 切片 3：指标仪表盘面板 —— **2026-10-13 用户口径整块移除**
# --------------------------------------------------------------------------- #
# 用户口径「把商品洞察和内容洞察中的指标相关内容逻辑移除，包括店铺商品指标快照
# 采集、笔记指标快照采集定时任务移除，后续作为专项单独排期」。
#
# ⇒ 原来这里的 7 条守卫（面板调用 4 个请求函数 / 面板挂在宿主页 / 空态用 Empty /
#    价格走区间文案 / 纯逻辑库不依赖 React / 不补零天 / vitest 用例已接线）**随被测
#    对象一并移除**：
#      * `frontend/src/pages/platforms/xhs/metrics-dashboard-panel.tsx`（已删）
#      * `frontend/src/lib/metrics-format.ts` + `metrics-format.test.ts`（已删）
#      * `lib/api.ts` 的 `collectNoteMetrics` / `fetchNoteMetricsTrend` /
#        `collectShopMetrics` / `fetchShopMetricsTrend`（已删）
#      * 后端 4 端点 + 两个定时任务（已删）
#    「指标快照/趋势」后续作**专项单独排期**时，这些判据应随新实现一并回来。


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


def test_sync_specified_modal_enqueues_and_closes_without_polling():
    """按 ID 同步 = **入队后提示 + 关弹窗**，**不再轮询结果**（2026-10-11 用户口径）。

    用户原话：「优化执行 id 同步商品，点击同步按钮之后加入队列**不再轮询查询结果**，
    提示 **XX 条已经加入队列中，执行进度请到详情批次 XXXX 查看**，然后**关闭弹窗**」。

    ⚠️ 这条**取代**了同日 v3 的「前台等结果（整块蒙版 + 已耗时秒表 + 执行中不可关）」。
    判据四件，缺一不可：

    1. **没有轮询**：`pollGoodsSyncBatch` / `POLL_INTERVAL_MS` / `fetchQueueRecords`
       全部不得再出现在本文件（留着就是「还在等结果」，用户口径落空）；
    2. **没有等待 UI**：`Spin` 蒙版 / `waitElapsedSec` 秒表 / `maskClosable={false}`
       一并退场（两条 Tab 现在都是「触发即关」，没有前台等待可言）；
    3. 提示文案含**两个要点**（条数 + 批次号）——由 `enqueueNotice` 单点产出；
    4. 提交成功后**关弹窗**（`onClose()` 在成功分支里，不是只 message 一下）。
    """
    modal = _code_only(_read(SYNC_ITEMS_MODAL))
    for gone in ("pollGoodsSyncBatch", "POLL_INTERVAL_MS", "fetchQueueRecords"):
        assert gone not in modal, f"v4 已去掉轮询，`{gone}` 不该还在（用户口径「不再轮询查询结果」）"
    for gone in ("waitElapsedSec", "beginWait", "endWait", "spinning={idRunning}"):
        assert gone not in modal, f"v4 没有前台等待，`{gone}` 不该还在"
    assert "maskClosable={false}" not in modal, (
        "两条 Tab 都是触发即关 ⇒ 不再需要「执行中不许关」（留着会让用户以为关了会丢结果）"
    )
    #: 提示文案的唯一产出点（含「条数」与「批次号」两个要点）
    assert "function enqueueNotice(" in modal, "入队提示文案必须单点产出（避免两处措辞分叉）"
    notice = _slice_between(modal, "export function enqueueNotice(", "export function SyncSpecifiedItemsModal(")
    assert "条已经加入队列中" in notice, "提示必须报**条数**（用户口径逐字）"
    assert "batch_no" in notice and "执行进度请到" in notice, (
        "提示必须带**批次号**并指向进度入口（用户口径逐字：进度到详情批次 XXXX 查看）"
    )
    #: 提交成功后关弹窗
    body = _slice_between(modal, "const runSync = useCallback(", "[platformAccountId, message, onSynced, onClose],")
    assert "onClose();" in body, "入队成功后必须**关闭弹窗**（用户口径「然后关闭弹窗」）"


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
    #: ① 根级 Spin 挡交互 + WaitMask 显示耗时文案
    #: （2026-10-20 统一蒙版：耗时不再写在 `Spin tip` 里 —— tip 跟内容滚走，
    #:  改由 `<WaitMask elapsed=…>` 画在最上层。断言**新的承重点**。）
    #: ⚠️ 2026-10-20 判据换承重点：**页面根级不再用 antd `<Spin spinning>`**。
    #: 实测根因：antd 6 的 `Spin` 无条件渲染 `Indicator`，即使 `spinning={false}`
    #: 也会多渲染一个「只有圈没有字」的空壳，与 `WaitMask` 的卡片并排出现
    #: ⇒ 用户看到「两个居中提示」。改用**同名类名的普通 div**（`.ant-spin` /
    #: `.ant-spin-container`），`global.css` 的高度链规则不变、antd 不参与渲染。
    assert '<div className="ant-spin">' in page, "页面缺根级等待容器（.ant-spin）"
    assert '<div className="ant-spin-container">' in page, "缺高度链内层（.ant-spin-container）"
    assert "spinning={waitRunning}" not in page, (
        "不许回退到 antd `Spin spinning` —— antd6 会无条件渲染空 Indicator，变成两层等待"
    )
    assert "<WaitMask" in page and "elapsed={waitElapsedSec}" in page, (
        "等待提示与耗时由 `<WaitMask>` 承担"
    )
    assert "mask={false}" in page, (
        "页面级等待不要遮罩底（用户口径 2026-10-20「可以不要蒙版只要居中的弹窗提示」）"
    )
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


def test_note_sync_enqueues_goods_missing_local_info():
    """商业笔记**同步时**（单篇与批量同一条路），本地查不到信息的挂载商品必须入商品同步队列。

    用户原话（2026-10-12，最终口径）：「项目中商业笔记同步笔记时存在商品在本地查询不到，
    则需要将商品 id 放入商品同步队列执行同步商品信息的逻辑」。

    判据（全部落在 `library-page.tsx` 的**代码**上，剥注释后判）：
    ① 单篇「同步」`syncNote` 与批量 `batchSyncNotes` **都**调 `enqueueMissingGoodsInfo(...)`；
    ② **同一份实现**（该函数只定义一次，收一个笔记列表；单篇 = 长度为 1）—— DEF-044；
    ③ 判据与后端缺值口径同源 —— 「有 item_id、但 item_title 与 price 都空」；
    ④ 入队走既有 `syncSpecifiedShopItems`（不另造同步路径）；
    ⑤ 多账号按 `platform_account_id` 分组（该请求函数只收一个账号 ID）。
    """
    page = _code_only(_read(LIBRARY_PAGE))
    # ① 两条路都挂上
    sync_fn = _slice_between(page, "async function syncNote(", "async function ")
    assert "enqueueMissingGoodsInfo(" in sync_fn, "单篇「同步」必须补商品"
    batch_fn = _slice_between(page, "async function batchSyncNotes(", "async function ")
    assert "enqueueMissingGoodsInfo(" in batch_fn, "批量同步也必须补商品（用户最终口径）"
    # ② 同一份实现，只定义一次
    assert page.count("function enqueueMissingGoodsInfo(") == 1, (
        "补商品必须**只有一份实现**（单篇与批量共用），不许各写一份"
    )
    helper = _slice_between(page, "function enqueueMissingGoodsInfo(", "async function syncNote(")
    assert "notes: SavedNote[]" in helper, "helper 应收**笔记列表**（单篇即长度为 1）"
    # ③ 判据
    assert "g.item_id && !g.item_title && !g.price" in helper, (
        "判据必须是「有 item_id、但标题与价格都空」（与后端缺值口径同源）"
    )
    # ④ 复用既有同步路径
    assert "syncSpecifiedShopItems(" in helper, "必须走既有 `/items/sync`（不另造同步路径）"
    # ⑤ 按账号分组（该请求函数只收一个账号 ID）—— 断言**分组结构**，不是「出现过这个名字」
    #    ⚠️ 首版只查 `platform_account_id` 字样 ⇒ 「把账号取值改成 `?? 1`」的变异
    #    照样绿（名字还在，分组没了）= 哑弹。改为钉住分组的两段形态：
    #    ① 取值后**跳过没有账号的笔记**（`if (!accountId) continue;`）
    #    ② 按账号聚合进 `byAccount`，并以 `platform_account_id` 为 Map 键
    assert "if (!accountId) continue;" in helper, (
        "没有账号的笔记必须跳过（`const accountId = note.platform_account_id; if (!accountId) continue;`）"
    )
    assert "byItemId.set(g.item_id, accountId)" in helper, (
        "商品必须带上它所属笔记的账号 ID（否则分组入队无从谈起）"
    )
    assert "for (const [accountId, itemIds] of byAccount)" in helper, (
        "必须按 accountId 分组逐个账号入队（`syncSpecifiedShopItems` 只收一个账号 ID）"
    )
    # 卡片如实说明（不干放「价格待同步」）
    assert "本地查询不到该商品信息" in page, "卡片应如实说明本地查不到该商品信息"


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
# M5 自动化工作流页（设计 19）—— ⛔ 2026-10-07 整条特性退场，本段 6 条守卫已删除
#
# 退场原因（实测三个硬缺口）见 docs/02_设计文档/19_功能设计_自动化工作流.md 头部横幅。
# ⚠️ 原 6 条守卫（调用 11 个请求函数 / 路由与菜单 / lib 单一词表 / 时长缺值不补 0 /
#   lib 不引 UI 框架 / 页面结构）随页面文件一并退场 ——
#   它们守的对象已不存在，留着只会让「文件被删」看起来像「守卫被摘掉」。
# 替代守卫见 `test_menu_integration.py::TestWorkflowRetired`（钉「不得再回来」）。
# --------------------------------------------------------------------------- #
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
    """菜单「监控中心」存在，且不再有「竞品监控」「数据抓取」两个重复入口。

    ⚠️ v1.1.0 起侧栏由后端下发（`GET /api/system/menus/mine`），源码里不再有
    菜单常量 ⇒ 判据改看**菜单种子**。原来断言 `'label: "监控中心"' in app_shell`
    实测直接红（侧栏已改成数据驱动）。
    """
    import json

    root = Path(__file__).resolve().parents[2]
    seed = root / "config/initial_data/menus.seed.json"
    rows = json.loads(seed.read_text(encoding="utf-8"))["menus"]
    names = {r.get("name") for r in rows}
    assert "监控中心" in names, "菜单种子里没有「监控中心」"
    assert "竞品监控" not in names, "重复入口「竞品监控」复活"
    assert "数据抓取" not in names, "重复入口「数据抓取」复活"


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
#
# ⚠️ **2026-10-13 锚点重定向（记录，不静默删除）**：无流量清理的**整块 UI 已从
# `my-notes-page.tsx` 搬到 `datacenter-page.tsx`**（用户口径「数据中心应单独一个页面
# 且数据与当前笔记数据拆分开…主要是展示数据、筛选数据、清理无流量等」，见设计 35）。
# 「入口只有我的笔记页」这句**已被取代** —— 现在唯一入口是**数据中心页**（仍只有一处，
# 只是换了归属）。本组守卫的判据一字未改，只把读取文件从 `my-notes-page.tsx` 改到
# `datacenter-page.tsx`（否则整组恒红 = 假防线）。
# --------------------------------------------------------------------------- #
def _my_notes_code() -> str:
    #: ⚠️ 名字保留（被多处以兼容调用），实际读的是无流量清理的**现归属页**。
    return _code_only(_read(DATACENTER_PAGE))


def test_no_traffic_cleanup_closes_the_modal_on_click():
    """点「开始清理」后**弹窗关闭**（任务已交后端，留着只会挡页面）。

    ⚠️ **2026-10-13 锚点重命名**（数据中心重排把清理改名为批量候选模型）：
    - `setNoTrafficOpen(false)` → **`setCleanupOpen(false)`**（弹窗态变量名换了）；
    - `handleStartCleanup` 现委托 `doStartCleanup(noteIds)`（同一件事，拆出「清全部」路径）。
    判据不变：发起后**关弹窗** + 确实**发起了清理**。
    """
    code = _my_notes_code()
    start = code.index("async function handleStartCleanup")
    body = code[start : start + 2000]
    assert "setCleanupOpen(false)" in body, "发起清理后没有关闭弹窗"
    # 正向锚：清理确实被发起（防「整个函数被删空」也让断言变绿）
    assert "startNoTrafficCleanup(" in body


def test_no_traffic_button_switches_to_busy_and_disables():
    """有任务在跑 ⇒ 按钮文案变「清理无流量中」且**不可点**。

    ⚠️ **2026-10-13 锚点重命名**：旧文案 `"正在清理无流量笔记"` → **`"清理无流量中"`**；
    旧禁用判据 `disabled={cleanupActiveTaskId !== null}` → **`disabled={cleanupRunning}`**
    （数据中心重排把「按钮态」独立成 `cleanupRunning`，`cleanupActiveTaskId` 只用于轮询收口）。
    三件事仍缺一不可：① 独立状态；② 文案切换；③ `disabled` + 后端在途查询（刷新后恢复）。
    """
    code = _my_notes_code()
    assert "cleanupActiveTaskId" in code, "缺少「是否有清理在跑」的状态"
    assert '"清理无流量中"' in code, "按钮没有切换文案"
    assert "disabled={cleanupRunning}" in code, "按钮没有随任务态禁用"
    # 数据源必须是后端在途查询（刷新页面后按钮态也得恢复）——
    # 变异：把探测删掉 ⇒ 刷新后按钮永久显示「清理无流量」，可以重复触发。
    assert "fetchNoTrafficCleanupActive(" in code, "没有查在途任务（刷新后按钮态丢失）"


def test_no_traffic_busy_state_survives_modal_close():
    """按钮态**不属于弹窗生命周期**：弹窗（`cleanupOpen`）关掉后任务态仍在。

    这是最容易做错的一处 —— 把「正在清理」挂在弹窗上，弹窗一关按钮就退回可点，
    用户可以反复点（后端会挡，但界面在骗人）。

    ⚠️ **2026-10-13 锚点重命名**：`noTrafficOpen` → `cleanupOpen`（弹窗态变量名换了）。
    """
    code = _my_notes_code()
    # 任务态不在任何 `cleanupOpen &&` / `cleanupOpen ?` 条件里
    for marker in ("cleanupOpen && cleanupRunning", "cleanupOpen ? cleanupRunning"):
        assert marker not in code, f"按钮态被绑在弹窗开合上：{marker}"
    assert "setCleanupOpen(false)" in code and "setCleanupActiveTaskId(res.task_id)" in code, (
        "关弹窗与置任务态必须是**两个独立动作**（前者不许顺带清掉后者）"
    )


def test_cleanup_log_modal_lists_the_independent_table():
    """日志详情按钮 + 弹窗：列表来自**独立日志表**端点（不是 tasks 行）。

    用户口径「日志表独立」的可执行判据之一：弹窗取数走 `/no-traffic/cleanup-logs`，
    而不是复用 `cleanup/{task_id}`（后者读的是会被保留期清掉的 tasks 行）。

    ⚠️ **2026-10-13 锚点重命名**：日志弹窗标题由 `"清理无流量笔记 · 操作日志"` 改为
    **`"清理日志"`**（与入口按钮同名，弹窗简短标题）；字段列判据不变。
    """
    code = _my_notes_code()
    assert "清理日志" in code, "缺少「清理日志」入口按钮"
    assert "fetchNoTrafficCleanupLogs(" in code, "日志弹窗没有取数"
    assert "no_traffic_cleanup_logs" not in code  # 前端不该知道表名（走端点）


def test_no_traffic_cleanup_has_no_scheduled_entry_at_all():
    """手动入口**唯一**：清理无流量笔记**在定时任务体系里完全不存在**。

    ⚠️ 2026-10-07（用户两条口径）：「无流量笔记清理任务不应在任务列表展示」+
    「无流量笔记清理任务表移除该配置」—— 键连同常量、执行器一起从注册表移除。

    ⚠️ **2026-10-13 锚点重定向**：唯一入口页从 `my-notes-page.tsx` 改为
    **`datacenter-page.tsx`**（数据中心独立成页）。判据仍是「**全站只有一处**入口」。
    """
    from backend.app.core.initial_data import load_initial_scheduled_jobs
    from backend.app.services import scheduler_service
    from backend.app.services.job_registry import job_registry

    assert "system.no_traffic_cleanup" not in job_registry()
    assert "system.no_traffic_cleanup" not in {
        row["job_key"] for row in load_initial_scheduled_jobs()
    }
    assert not hasattr(scheduler_service, "run_scheduled_cleanups")
    # 只有数据中心页有入口（无其他页面提交该端点）
    root = ROOT / "frontend/src"
    hits = []
    for path in root.rglob("*.tsx"):
        if "datacenter-page" in path.name:
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

    ⚠️ **2026-10-13 锚点重写**（数据中心重排后清理日志表在新页重实现）：
    - 笔记明细列名由 `"笔记 ID"` → **`"笔记ID"`**（去空格）；
    - **`childrenColumnName=` 不再需要**：旧页把子行放在 antd 的 `children` 字段上才要
      关自动探测；新页子行来自 **`record.items`**（显式数据源）⇒ 与 antd 的 children
      探测**天然无冲突**（原断言「必须关探测」的**前提消失**，非缺陷）；
    - `"篇（展开查看）"` 入口文案取消 —— 改用 antd 标准展开图标（展开区判据仍在）。
    判据保留：① `expandedRowRender` 里有**独立子表**；② 笔记维度四列；③ 用 `NoTrafficCleanupItemRow` 类型。
    """
    code = _my_notes_code()
    assert "expandedRowRender" in code, "没有展开区 ⇒ 笔记维度可见不了"
    # 展开区必须是**独立子表**（`expandedRowRender` 里放 `<Table>`）
    start = code.index("expandedRowRender")
    block = code[start : start + 1500]
    assert "<Table" in block, "展开区不是独立的子表（笔记维度可见不了）"
    # 笔记维度的列（展开区那张表）
    for column in ("笔记ID", "结果", "原因", "操作时间"):
        assert f'"{column}"' in code, f"笔记明细缺列：{column}"
    assert "NoTrafficCleanupItemRow" in code, "展开区不是按笔记明细类型渲染的"


def test_cleanup_item_results_cover_the_backend_enum():
    """结果文案映射覆盖后端**全部五个**取值，且认不出时**原样显示**（不造假）。

    特别是 `not_attempted`（本批被风控中止、这篇没轮到）必须与 `failed`
    分得开 —— 前者该重试、后者该查原因。

    ⚠️ **2026-10-13 锚点重写**：实现由 `switch` 函数 `cleanupItemResult` 改为
    **`CLEANUP_RESULT_LABEL` 字典 + `?? v` 兜底**（表驱动）。判据改为「字典覆盖全枚举
    + 渲染处 `?? v` 兜底」—— 同样证明「五值全映射 + 认不出原样」。
    """
    from backend.app.models.no_traffic_cleanup_log import CLEANUP_ITEM_RESULTS

    code = _my_notes_code()
    start = code.index("CLEANUP_RESULT_LABEL")
    body = code[start : start + 700]
    for value in CLEANUP_ITEM_RESULTS:
        assert f"{value}:" in body, f"结果 {value} 没有文案映射"
    # 认不出的取值必须**原样显示**（`?? v`），不许静默成空
    assert "?? v" in code, "认不出的取值必须有兜底（原样显示，不许静默成空）"


def test_cleanup_log_failure_reason_column_never_shows_the_coverage_note():
    """**DEF-095 的前端半边**：「失败原因」列只渲染 `reason`，不许渲染 `coverage_note`。

    修复前的现场：`reason` 里装着候选池覆盖度文案，于是平台风控失败时界面显示
    「候选池仅包含已同步千帆挂链映射的笔记…」—— 用户据此追查了错误方向。

    ⚠️ **2026-10-13 锚点精化**（数据中心重排后本表在新页重实现）：新页「失败原因」列
    直接 `dataIndex: "reason"` 且 `render: v => v || "—"` ⇒ **只读 reason**，天然不含
    `coverage_note`。原实现里的 `row.aborted` / 「本批已中止」提示在本页**不复存在**
    （那是旧页形态）；本用例改为钉「失败原因列只读 reason 字段 + 全页无 coverage_note」。
    """
    code = _my_notes_code()
    start = code.index('title: "失败原因"')
    body = code[start : start + 300]
    assert "reason" in body, "失败原因列没读 `reason`"
    assert "coverage_note" not in code, (
        "失败原因列不许渲染覆盖度说明（它曾经就是这样冒充失败原因的）"
    )


# --------------------------------------------------------------------------- #
# 商品草稿箱 = 商品页内的**弹窗**（设计 24 §9.4，2026-10-08 用户口径）
#   「草稿箱弹窗的形式在商品页内展示，账号使用商品页选定的账号」
# 独立页 `product-drafts-page.tsx` 已删除（页 → 弹窗），守卫改盯弹窗文件。
# --------------------------------------------------------------------------- #
DRAFTS_MODAL = ROOT / "frontend/src/pages/platforms/xhs/product-drafts-modal.tsx"


def test_product_drafts_is_a_modal_not_a_page():
    """草稿箱必须是**弹窗**，且独立页文件已删除（用户口径：商品页内展示）。

    变异：把弹窗改回独立页（渲染 `<Page>` / 加回 menu 项）⇒ 本用例红。
    """
    assert DRAFTS_MODAL.exists(), "草稿箱弹窗文件不存在"
    assert not (
        ROOT / "frontend/src/pages/platforms/xhs/product-drafts-page.tsx"
    ).exists(), "独立页应已删除（草稿箱改弹窗）"
    code = _read(DRAFTS_MODAL)
    # 必须是 antd Modal（不是页面外壳）
    assert "from \"antd\"" in code and "Modal" in code, "草稿箱不是弹窗"
    assert "<Modal" in code, "没有渲染 Modal"
    # 宽度按视口百分比（用户既有偏好），且有上限
    assert "width=\"70vw\"" in code, "弹窗宽度不是视口 70%（用户口径 70%）"
    assert "maxWidth" in code, "视口百分比必须配上限（防超宽屏拉满）"


def test_product_drafts_modal_has_no_account_dropdown():
    """弹窗内**不许**有账号下拉 —— 账号只有一个来源（商品页选定）。

    变异：把 `<Select ... accounts>` 加回弹窗 ⇒ 页面看 A、弹窗看 B。
    """
    code = _read(DRAFTS_MODAL)
    assert "fetchAccounts" not in code, "弹窗自己拉账号列表了（账号应跟随商品页）"
    assert "PlatformAccount" not in code, "弹窗引了账号类型（说明它想自选账号）"
    # 账号必须由 prop 传入
    assert "platformAccountId" in code, "弹窗没接收商品页传入的账号"


def test_drafts_modal_is_wired_to_the_page_selected_account():
    """商品页把**本页选定账号**传给草稿箱弹窗，且弹窗由本页按钮打开。

    变异：把 `platformAccountId={selectedAccountId}` 写死成别的值
    ⇒ 弹窗看的不是本页选的店铺。

    ⚠️ 切片必须**只覆盖草稿箱弹窗这一个元素** —— 相邻的发布弹窗也有
    `platformAccountId={selectedAccountId}`，用固定 500 字窗口会**越界读到它**
    而让变异逃逸（实测：窗口放宽时「把账号改成 null」是 ESCAPED）。
    """
    code = _read(SHOP_ITEMS)
    assert "ProductDraftsModal" in code, "商品页没引用草稿箱弹窗"
    assert "draftsModalOpen" in code, "没有草稿箱弹窗的开关状态"
    start = code.index("<ProductDraftsModal")
    end = code.index("/>", start)  # 该元素的闭合标签（第一个 />
    body = code[start:end]
    assert "platformAccountId={selectedAccountId}" in body, (
        "弹窗账号没接本页选定账号"
    )
    assert "open={draftsModalOpen}" in body, "弹窗开关没接"


def test_drafts_modal_is_a_menu_less_entry_in_the_product_page():
    """草稿箱入口在**商品页工具栏**（按钮），且菜单里**已无**独立项。

    变异：把菜单项加回 app-shell ⇒ 又变成独立页入口。
    """
    shell = _read(ROOT / "frontend/src/components/layout/app-shell.tsx")
    assert "product-drafts" not in shell, "菜单里不该再有独立草稿箱项"
    code = _read(SHOP_ITEMS)
    assert "setDraftsModalOpen(true)" in code, "商品页没有打开草稿箱弹窗的按钮"


# ============================================================
# 设计 25 §4/§6：两步流程（第一步预检 → 第二步发布）接线守卫
# ============================================================


def _src(path: Path) -> str:
    return path.read_text("utf-8")


def test_publish_wizard_is_step_wise_with_antd_steps():
    """用户 2026-10-08 口径：「发布商品使用 antd 的步骤，第一预检，第二配置商品参数
    加参数预览，最后点击发布」。

    判据三条：① 真的用 antd `Steps`（不是自己拼的 div）；② 恰好三步且顺序正确；
    ③ 是**一个**弹窗里切步骤（拆成两个 Modal 就不是「步骤」了）。
    """
    src = _src(PUBLISH_MODAL)
    assert "Steps" in src, "没用 antd Steps"
    assert '"预检"' in src and '"配置商品参数"' in src and '"发布"' in src, "三步标题不全"
    #: 顺序：预检 → 配置 → 发布（`items` 里出现的先后）
    order = [src.index('"预检"'), src.index('"配置商品参数"'), src.index('"发布"')]
    assert order == sorted(order), "步骤顺序错误"
    #: Steps 必须和**承载它的那个**弹窗同处一个组件（不能拆成「关 A 开 B」）。
    #: ⚠️ 不数 `<Modal` 总数 —— 大图预览也是一个 Modal，那是正当的第二个。
    main = src.index("title=\"发布商品\"")
    steps = src.index("<Steps")
    #: ⚠️ 阈值由 2000 放宽到 6000（2026-10-11）：判据本意是「Steps 与主弹窗**同组件**」，
    #: 用字符距离只是近似。提交按钮拆成两个（+Popconfirm）后这里长了约 1200 字符，
    #: 原阈值变成假红。真判据是上面那句「同处一个组件」——Steps 必须在本文件内。
    assert abs(main - steps) < 6000, "Steps 和主弹窗离得太远（可能被拆到别的组件里）"


def test_publish_wizard_step_two_merges_the_two_qianfan_pages():
    """用户口径 2026-10-09：「千帆后台发布商品时会有两步，将这两步合并整合到
    当前项目的发布商品的**第二步**中」「第二步参数配置中的商品图片、商品标题、
    商品类目合并到下面各基础信息中」。

    千帆的两页 = 【第一部分 图/标题/类目】(`PublishConfigStep1`) +
    【第二部分 参数配置】(`PublishConfigStep2`)。第一部分现在作为
    `headSlot` **并入第二部分「基础信息」区**，所以判据是：
    ① 第二步渲染 `PublishConfigStep2`；② `PublishConfigStep1` 经 `headSlot`
    传入（不再是独立一块）；③ step2 把 `headSlot` 渲染进「基础信息」区最前。
    """
    src = _src(PUBLISH_MODAL)
    start = src.index("{step === 1 && (")
    end = src.index("{/* ⚠️ 分组选择弹窗", start)
    branch = src[start:end]
    assert "<PublishConfigStep2" in branch, "第二步没渲染第二部分"
    assert "<PublishConfigStep1" in branch, "第二步没把第一部分并进来"
    assert "headSlot={" in branch, "第一部分没走 headSlot 并入基础信息"

    #: step2 必须真的把 headSlot 渲染在「基础信息」区里
    step2 = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "headSlot" in step2, "step2 没接收 headSlot"
    section = step2[step2.index('title="基础信息"') : step2.index('title="基础信息"') + 260]
    assert "{props.headSlot}" in section, "headSlot 没渲染在基础信息区最前"


def test_publish_wizard_has_a_parameter_preview():
    """第二步必须同时有「配置」和「参数预览」（用户口径里点名了预览）。"""
    src = _src(PUBLISH_MODAL)
    assert "参数预览" in src, "缺少参数预览区"
    assert "Descriptions" in src, "预览区应把参数结构化展示"


def test_publish_wizard_wires_the_precheck_payload():
    """第二步**必须**真的消费预检产物，且 token 一路传到请求体。"""
    src = _src(PUBLISH_MODAL)
    assert "buildPublishPayloadFromPrecheck" in src, "向导没接预检映射"
    assert "precheck_token" in src, "发布请求没带 precheck_token"


def test_publish_wizard_does_not_hard_require_the_old_parsed_package():
    """⚠️ 回归点：老实现硬性要求 `parsed`（服务器本地目录解析产物），
    新链路根本没有它 ⇒ 若照抄会让提交永远进不去。"""
    src = _src(PUBLISH_MODAL)
    assert "parseItemDraftPackage" not in src, "向导不该再走服务器本地目录解析"


def test_publish_wizard_sends_off_shelf_after_only_via_the_button_param():
    """⚠️ **口径已反转**（2026-10-11 用户**取消红线**）。

    原条（保留对照）：「⚠️ 红线：发布成功必须立即下架。前端**不得**传
    `off_shelf_after: false`」+ 断言「前端不许出现 off_shelf_after」。
    该口径**已被取代** —— 提交按钮拆成「发布商品（不下架）」/「发布并下架商品」，
    前端**必须**显式传 `off_shelf_after` 才能区分两个动作。

    ⇒ 现行判据：`off_shelf_after` 的取值**只能来源**于 `submit` 的按钮参数
    （`shelfAfterPublish`），不许页面里再冒出一个写死的 `off_shelf_after: true/false`
    （那会让某个按钮的行为与标签不符）。
    """
    code = _code_only(_src(PUBLISH_MODAL))
    assert "off_shelf_after: shelfAfterPublish" in code, (
        "off_shelf_after 必须来自 submit 的按钮参数"
    )
    #: 写死值只允许出现在 `submit(false)` / `submit(true)` 这两个调用点（那是按钮实参，
    #: 由 `test_publish_buttons_are_split…` 逐一对语义）。这里只保证**没有第三个来源**。
    assert "off_shelf_after: true" not in code and "off_shelf_after: false" not in code, (
        "不许在页面里写死 off_shelf_after 的布尔值（只能经 shelfAfterPublish 透传）"
    )


def test_publish_wizard_has_save_draft_button_in_step3():
    """**「保存草稿」按钮在第三步**（用户口径 2026-10-10：千帆发布页底部有
    「保存草稿」次按钮 +「提交商品」主按钮；本弹窗补「保存草稿」，放第三步）。

    判据：① 第三步（`step === 2`）分支里有「保存草稿」；
    ② 走后端 `createItemDraft`（只到草稿箱、不上架），**不**走 `publishShopItem`。
    ⚠️ 2026-10-11：原判据里的「+『确认发布』」**已随按钮拆分退场** ——
    第三步现在是「保存草稿 + 发布商品 + 发布并下架商品」三按钮
    （拆分正确性由 `test_publish_buttons_are_split_with_tip_confirm_and_the_right_colors` 钉）。
    """
    code = _code_only(_src(PUBLISH_MODAL))
    assert "saveDraft" in code, "缺保存草稿逻辑"
    assert "await createItemDraft({" in code, "保存草稿应真调用 createItemDraft（只到草稿箱）"
    assert "保存草稿" in code, "缺「保存草稿」按钮文案"
    #: 按钮在第三步（`step === 2` 分支内，与「确认发布」并列）
    step3 = code[code.index("{step === 2 && ("):]
    step3 = step3[: step3.index("</Steps>")] if "</Steps>" in step3 else step3
    assert "保存草稿" in step3, "「保存草稿」按钮应在第三步"
    #: 保存草稿绝不触发发布（两个独立入口）。
    #: ⚠️ 2026-10-11：`submit()` 现在带参数（`submit(false)` / `submit(true)`）——
    #: 写死 `void submit()` 会假红。
    assert "void saveDraft()" in code, "缺保存草稿入口"
    assert "void submit(false)" in code and "void submit(true)" in code, (
        "发布应是两个独立入口（发布商品 / 发布并下架商品）"
    )
    #: 保存草稿蒙版 + 耗时（用户口径 2026-10-10：保存应与发布一样加蒙版统计耗时）
    #: ⚠️ 2026-10-20 统一蒙版：文案与耗时不再拼在字符串里，改由 `<WaitMask
    #: title=… elapsed={draftElapsedSec}>` 承担（标题与秒数是两个 slot）。
    assert "draftSaving" in code and "draftElapsedSec" in code, "保存草稿应有耗时统计 state"
    assert "<WaitMask" in code and "elapsed={draftElapsedSec}" in code, (
        "保存草稿应加蒙版 + 实时耗时（WaitMask 的 elapsed）"
    )
    assert "正在保存草稿" in code, "保存草稿蒙版缺文案"


def test_publish_wizard_shows_media_before_running_the_precheck():
    """用户口径 2026-10-08：「先将媒体和数据包全部在弹窗展示，点击预检」。
    整合成一步后依旧保留：读取（展示）与预检（检测）是**两次**调用。

    判据：读取阶段传 `inspect:false`（只展示不检测），预检走 scan 端点。
    """
    src = _src(PUBLISH_MODAL)
    assert "inspect: false" in src, "读取阶段应传 inspect:false（只展示不检测）"
    assert "rescanPackagePrecheck" in src, "预检应走 scan 端点（跑 OCR + 抽帧）"


def test_publish_wizard_gates_step_two_on_a_passed_precheck():
    """⚠️ 核心诉求：**预检通过才可进第二步**（判据形态 2026-10-14 修订）。

    ⚠️ 用户口径 2026-10-14：
    > 「点击下一步时**未进行拦截提示**匹配 sku 图」

    根因：这条判据原先靠按钮 `disabled` 实现，而 **disabled 的按钮吞掉点击**
    ⇒ 用户既进不去、也看不到任何提示。改成**点击时逐条报原因**后，
    「预检通过」必须回到 `goStep2` 里当一条**显式判据** ——
    否则去掉 `disabled` 就等于把这道关卡整个拆了。
    """
    src = _src(PUBLISH_MODAL)
    code = _code_only(src)
    assert "precheckPassed" in code, "缺少「预检通过」判据"
    #: ① 不在按钮上禁用（禁用 = 吞点击 = 用户看不到原因）。
    assert "disabled={!precheckPassed" not in code, \
        "「下一步」又回到 disabled 门禁（用户看不到提示，只能猜）"
    #: ② 判据必须在 `goStep2` 里显式存在。
    go = code[code.index("const goStep2 = useCallback("):]
    go = go[:go.index("}, [")]
    assert "precheckPassed" in go, \
        "goStep2 没校验 precheckPassed（去掉 disabled 后预检没过也能进第二步）"


def test_next_step_blocks_on_sku_match_and_item_no():
    """「下一步」的**逐条关卡**（用户口径 2026-10-14）。

    > 「商品发布 sku图自动匹配点击前校验…点击下一步配置商品参数按钮校验
    >   SKU 图需自动匹配触发成功后才可以点击下一步，否则提示请先匹配sku图」
    > 「商品sku表格tab下新增一个商品货号参数，默认是为空**必填**，
    >   点击下一步时需校验非空」

    判据：`goStep2` 里按顺序逐条拦并给**可执行**的提示。
    """
    code = _code_only(_src(PUBLISH_MODAL))
    go = code[code.index("const goStep2 = useCallback("):]
    go = go[:go.index("}, [")]
    assert '"请先匹配 SKU 图"' in go, "没拦「SKU 图未匹配」"
    assert "skuMatchRan" in go, "SKU 图关卡没用 skuMatchRan（用长度会指错方向）"
    assert '"请先填写商品货号"' in go, "没拦「商品货号为空」"
    assert "itemNo" in go, "商品货号关卡没读 itemNo"


def test_apply_match_result_also_marks_sku_match_as_run():
    """**「应用匹配结果」必须置 `skuMatchRan`**（真 bug，2026-10-14）。

    用户原话：
    > 「sku 图已经自动匹配成功并应用了，点击下一步依旧提示请先匹配 SKU 图」

    根因：`onSkuMatchRun`（置 `skuMatchRan` 的**唯一入口**）只挂在
    「执行匹配」按钮上；而用户的真实路径是**「应用匹配结果」**（`onOk`）
    ⇒ 关卡判据恒 false ⇒ 明明配好了却被拦。

    判据：`onOk` 里除写回匹配结果外，**也要**调 `onSkuMatchRun()`。
    """
    code = _code_only(_src(PUBLISH_MODAL))
    #: ⚠️ 判据必须**定位到 SkuMatchModal 的那次调用** —— 页面里另有多处 `onOk`
    #: （如「自动加载参数」的二次确认），`code.index("onOk")` 会抓错一个（实测踩过）。
    at = code.index("<SkuMatchModal")
    call = code[at:code.index("/>", at)]
    assert "applySkuMatch(matchRows)" in call, "onOk 没写回匹配结果"
    assert "onSkuMatchRun()" in call, \
        "「应用匹配结果」没置 skuMatchRan ⇒ 点下一步仍提示「请先匹配 SKU 图」（真 bug 回归）"


def test_reopening_the_wizard_remounts_the_first_step():
    """**重开弹窗要清干净第一步面板的内部 state**（用户口径 2026-10-14）。

    用户原话：
    > 「每次关闭发布商品弹窗，相关的属性弹窗中的参数需要清除干净，
    >   本次打开发现 sku 名称默认读取的不是原始表格中的值，
    >   是上一次正则处理过的列值」

    根因：第一步面板改成 `display:none` **保持挂载**（为了「切步骤不丢匹配结果」）后，
    它的内部 state（表格副本 / 取值正则 / 勾选 / 已应用匹配图）**活得过 `reset()`**
    ⇒ 下次打开看到上一轮的残留。

    判据：`<PrecheckStep>` 外层节点带一个**每次打开都变的 key**（`openSeq`）——
    打开即重挂（内部 state 归零），步骤间切换 key 不变（仍保留）。
    """
    code = _code_only(_src(PUBLISH_MODAL))
    assert "const [openSeq, setOpenSeq] = useState(" in code, "没有 openSeq 状态"
    assert "setOpenSeq((n) => n + 1)" in code, "打开时没自增 openSeq（重开不会重挂）"
    assert "key={openSeq}" in code, \
        "第一步没有被 `key={openSeq}` 强制重挂 ⇒ 重开仍是上一轮的正则处理过的列值"


def test_sku_match_modal_is_destroyed_on_close():
    """SKU 匹配弹窗关掉即销毁（用户口径 2026-10-14：「属性弹窗中的参数清除干净」）。"""
    code = _code_only(_src(PUBLISH_MODAL))
    modal = code[code.index("function SkuMatchModal"):]
    modal = modal[:modal.index("function BatchColumnControl")]
    assert "destroyOnHidden" in modal, "SKU 匹配弹窗关掉没销毁（内部 state 会留到下次）"


def test_item_no_overrides_the_haohao_attribute_on_auto_load():
    """「货号」属性由**属性装配层固定合成**，不再靠「按名匹配平台属性」覆盖。

    ⚠️ 本条 **2026-10-15 被取代**（用户新口径，task t_c986f8d8）：
    > 「第三步，类目生成后，商品的其他属性中没有展示货号属性，
    >   它**应该是固定的其它属性第一个**」

    旧口径（2026-10-14）是「第一步填的货号 ⇒ 在『智能生成商品属性』那一步
    **按名覆盖**平台下发的『货号』属性」。它**恒不命中**：平台
    `desc_properties`（离线真机 22 项逐名核对）与 `predictProductProperty`
    都**不下发**货号 ⇒ `loadedFields` 里根本没有那一项 ⇒ 属性区永远看不到货号。
    这正是用户报的现象的**真因**。

    现口径 = 单实现（DEF-044）：`lib/publish-property-fields.ts` 的
    `withItemNoFirst()` 合成 `{id:"articleNo", name:"货号", kind:"text"}`
    并排在「其他属性」**第 0 位**；**两条属性加载路径**（进第二步的
    `autoLoadAll`、改类目的重载 effect）都必须过它，且都要把第一步填的货号
    落进该输入框。

    形态依据（非新猜测，均为既有实证）：
      · 千帆 bundle `ARTICLE_NO_PROPERTY_ID = "articleNo"`、
        `allProperties = [货号项].concat(平台属性)`；
      · `getFormData()` 里货号**单独走**，值取顶层 `articleNo`；
        `selectedPropertyVals` 把它 **`unshift`** 进属性列表 ⇒ 提交体
        `attributes[]` 第一条即 `{attributeId:"articleNo", attributeValue:<货号>}`；
      · 「货号」**不是**顶层 `articleNo` 那个字段（后者是 2026-10-14 的真机抓包结论）。
    """
    code = _code_only(_src(PUBLISH_MODAL))
    #: ① 合成 + 置首由唯一一份实现负责（不许在页面里再写一遍）。
    helper = _code_only(_read(ROOT / "frontend/src/lib/publish-property-fields.ts"))
    assert 'id: ITEM_NO_PROPERTY_ID' in helper, "货号项的 id 没走常量"
    assert 'const ITEM_NO_PROPERTY_ID = "articleNo"' in helper, \
        "货号属性 id 必须是 articleNo（千帆 bundle ARTICLE_NO_PROPERTY_ID 逐字一致）"
    #: ⚠️ 断言写成 **特征**（「货号在前 + 其余去重在后」）而不是逐字整行：
    #: 该函数 2026-10-15 一度写成泛型 `T`（`as unknown as T`）—— 调用点会把
    #: 「合成的货号项」硬塞成平台字段的具体类型 ⇒ `TS2322`（`PropertyLike`
    #: 不是 `T` 的子类型，`tsc` 实测报错）。定稿为**非泛型**
    #: `withItemNoFirst(fields: PropertyLike[]): PropertyLike[]`
    #: （并集形状），故这里只钉「首项是 `buildItemNoProperty()`、其余过
    #: `withoutItemNo`」这一判据 —— 改成追加到末尾 / 不去重都必红。
    assert re.search(
        r"return\s*\[\s*buildItemNoProperty\(\),\s*\.\.\.withoutItemNo\(fields\)\s*\]",
        helper,
    ), "置首必须是「货号在前 + 其余去重在后」（改成追加到末尾即推翻用户口径）"
    assert "as unknown as T" not in helper, \
        "又退回泛型实现了（调用点必 TS2322；定稿是非泛型 PropertyLike[]）"
    #: ② 去重判据 = id **或** 名（只判一个 ⇒ 平台换个写法就插出两条货号）。
    assert "f.id !== ITEM_NO_PROPERTY_ID" in helper and 'f.name.trim() !== ITEM_NO_PROPERTY_NAME' in helper, \
        "去重必须同时认 id 与名（否则平台下发货号时会出两条）"
    #: ③ `kind` 必须是文本（真机 propertyInputType = TEXT）。
    assert 'kind: "text"' in helper, "货号是文本属性"
    #: ④ 导入 + 调用：两条加载路径都必须过 `withItemNoFirst`。
    assert "withItemNoFirst," in code, "页面没有导入 withItemNoFirst"
    assert code.count("withItemNoFirst(") == 2, \
        f"两条属性加载路径都该插入货号（实测 {code.count('withItemNoFirst(')} 处调用）"
    assert "setOtherProperties(ordered)" in code, "autoLoadAll 没把置首结果写进 otherProperties"
    #: ⑤ 第一步填的货号必须落进货号项的输入框（两条路径各一次）。
    assert code.count("next[ITEM_NO_PROPERTY_ID]") + code.count("[ITEM_NO_PROPERTY_ID]: no") >= 2, \
        "两条路径都要把第一步的货号落进货号输入框"
    assert code.count("{ ...prev, [ITEM_NO_PROPERTY_ID]: no }") >= 1, \
        "改类目重载那条路径没回填货号"
    #: ⑥ 依赖数组必须带 `itemNo`（否则改完货号再点自动加载仍用旧闭包值）。
    assert "    itemNo,\n" in code, "autoLoadAll 的依赖数组漏了 itemNo"
    assert "}, [platformAccountId, configCategoryId, itemNo]);" in code, \
        "改类目重载的 effect 依赖数组漏了 itemNo（回填会用到旧值）"
    #: ⑦ 旧的「按名覆盖」必须**删干净**（两套逻辑打架 = 死代码，DEF-044）。
    assert "itemNoFilled" not in code, "旧的「货号覆盖」遗留（那份恒不命中，只会误导）"
    assert 'f.name.includes("货号")' not in code, "旧的按名兜底匹配还在"


def _run_publish_property_fields(env: dict | None = None):
    script = ROOT / "tests/frontend/publish_property_fields.cjs"
    assert script.exists(), "行为验证脚本缺失（tests/frontend/publish_property_fields.cjs）"
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


def test_item_no_is_the_first_other_property_driven_for_real():
    """「货号」是「其他属性」的**固定第一条** —— 由**真机离线属性表**验证（用户口径 2026-10-15）。

    > 「第三步，类目生成后，商品的其他属性中没有展示货号属性，
    >   它**应该是固定的其它属性第一个**」

    为什么值得单独跑：`withItemNoFirst()` 是纯装配函数，源码断言只能证明
    「那行还在」；本脚本拿 `.hermes/tmp/evidence_publish/step2_default_net.json`
    里真机 22 项 `descProperties`（**逐名核对无「货号」**）当输入，
    检查**真实输出**：23 项、首条是 `{name:"货号", id:"articleNo", kind:"text"}`、
    平台其余项相对顺序不变、平台若也下发则去重不出两条。
    """
    result = _run_publish_property_fields()
    assert result.returncode == 0, (result.stdout + result.stderr)[-900:]
    assert "13 PASS / 0 FAIL" in result.stdout


@pytest.mark.parametrize(
    "mutate,expected",
    [
        ("append", "A3"),          # 置首改成追加到末尾 ⇒ 推翻用户口径
        ("no-dedupe", "B2"),       # 完全不去重 ⇒ 平台下发货号时出现两条
        ("wrong-id", "A4"),        # id 写错 ⇒ 提交体 attributeId 不是 articleNo
        ("no-name-dedupe", "B1"),  # 只按 id 去重 ⇒ 平台按名下发就出两条
    ],
)
def test_publish_property_fields_guards_are_load_bearing(mutate, expected):
    """变异验证：把置首/去重/id 改坏时脚本必须变红（否则它没在守任何东西）。"""
    result = _run_publish_property_fields({"PUBLISH_PROP_FIELDS_MUTATE": mutate})
    assert result.returncode != 0, f"{mutate} 变异必须让脚本失败"
    assert f"FAIL  {expected}" in result.stdout, f"stdout={result.stdout}"


def test_item_no_input_lives_in_the_sku_table_tab():
    """商品货号输入框在**商品sku表格 tab**，且状态住在向导（用户口径 2026-10-14）。

    > 「商品sku表格tab下新增一个商品货号参数」

    ⚠️ 状态必须住在向导：它是 `goStep2` 的判据之一，
    而 `TablesPanel` 会随页签/步骤切换卸载重挂（放里面会丢 —— 与 `skuMatchRan` 同因）。
    """
    code = _code_only(_src(PUBLISH_MODAL))
    assert 'label: `商品sku表格（${tables.length}）`' in code, "商品sku表格页签找不到"
    tab = code[code.index('label: `商品sku表格（${tables.length}）`'):]
    tab = tab[:tab.index("<TablesPanel")]
    assert "商品货号" in tab, "商品sku表格 tab 下没有「商品货号」输入框"
    assert "itemNo" in tab, "货号输入框没接 itemNo"
    assert "onItemNoChange" in tab, "货号输入框没接回传"
    #: 状态在向导（不在面板内部）。
    assert "const [itemNo, setItemNo] = useState(" in code, "货号状态不在向导层"
    assert "itemNo={itemNo}" in code, "货号没透传给面板"


def test_step_switching_keeps_the_panel_mounted():
    """⚠️ **切换步骤不得丢状态**（用户口径 2026-10-14，真 bug）。

    用户原话：
    > 「切换步骤后，匹配好的 sku 图丢了……在来回切换步骤时保持不变」

    根因：`PrecheckStep` 是**条件渲染**（`{step === 0 && <PrecheckStep/>}`）
    ⇒ 切到第二步时组件被**卸载**，内部 state（`appliedSkuImages` 匹配结果、
    `manualPicks` 手选图、勾选、表格编辑）全丢，切回来一片空白。

    修法：**保持挂载、只隐藏**（`display: none`）—— 与第二步
    （`PublishConfigStep2`）此前踩的同一个坑、同一个修法。

    判据（两条，缺一不可）：
      ① 没有 `{step === 0 && <PrecheckStep` 这种条件渲染；
      ② 有一层 `display: step === 0 ? undefined : "none"` 包住它。
    """
    code = _code_only(_src(PUBLISH_MODAL))
    assert "{step === 0 && <PrecheckStep" not in code, \
        "第一步仍是条件渲染 ⇒ 切步会卸载组件、state 全丢（匹配好的 sku 图会丢）"
    assert 'display: step === 0 ? undefined : "none"' in code, \
        "第一步没有「保持挂载只隐藏」的包裹"


def test_precheck_page_does_not_surface_ocr_internals():
    """用户口径 2026-10-09：「OCR 检测移除，页面不要展示 ocr」。

    OCR 是**手段**、违规词命中才是**结果**。页面只展示结果（命中词 / 置灰），
    不把 OCR 的内部指标（已抽 N 帧 / 未检测 / 未做抽帧检测 / 帧识别失败）摊给用户。
    """
    src = _src(PUBLISH_MODAL)
    code = _code_only(src)
    for banned in ("已抽", "未做抽帧检测", "帧未识别", "VideoFrameNote"):
        assert banned not in code, f"页面仍在展示 OCR 内部指标：{banned}"


def test_precheck_pass_requires_no_failed_inspection():
    """⚠️ 「通过」的判据里必须排除**检测失败**的条目 —— 失败 = 没检过 ≠ 合规。"""
    src = _src(PUBLISH_MODAL)
    block = src[src.index("const precheckPassed"):src.index("const precheckPassed") + 500]
    assert "ocr_ok !== false" in block, "通过判据没排除「检测失败」的条目"


def test_read_phase_marks_everything_as_not_yet_inspected():
    """只读取时每条都必须是 `ocr_ok=None`（未检测）—— 不能让「还没检」等于合规。"""
    api = _src(ROOT / "backend/app/api/platforms/xhs/package_precheck.py")
    assert '"尚未预检"' in api, "只读取模式未把条目标成「尚未预检」"
    assert 'payload["inspected"] = False' in api


def test_wizard_media_names_are_single_line_with_a_tooltip():
    """用户口径 2026-10-08：长文件名（实测 SKU21 那条 40+ 字）要**单行省略** +
    Tooltip 看全名 —— 不能撑变形、不能换行把网格挤乱。"""
    src = _src(PUBLISH_MODAL)
    assert "textOverflow" in src and "ellipsis" in src, "文件名没有省略号截断"
    assert "whiteSpace" in src and "nowrap" in src, "文件名会换行"
    assert "Tooltip" in src, "长文件名没有 Tooltip 兜全名"


def test_wizard_media_supports_large_preview_with_arrow_navigation():
    """用户口径：所有媒体支持大图预览，**左右切换不是所有**（只在本组内切）。"""
    src = _src(PUBLISH_MODAL)
    assert "MediaPreview" in src, "没有大图预览组件"
    assert "ArrowLeft" in src and "ArrowRight" in src, "预览不支持左右方向键"
    assert "LeftOutlined" in src and "RightOutlined" in src, "预览没有左右切换按钮"
    #: ⚠️ 切换范围必须是**同组** —— 传的是当前区的 items，不是全包媒体
    assert "onPreview({ items, index })" in src, "预览切换范围不是当前组（会切到全包）"


def test_wizard_scrollbars_are_hidden_but_scrolling_still_works():
    """用户口径：弹窗内所有滚动条隐藏不展示，**保留滚动功能**。

    ⚠️ 判据是「藏外观」不是「禁滚动」—— 用 `display:none` 或 `overflow:hidden`
    会把滚动能力一起干掉，那是另一个需求。
    """
    src = _src(PUBLISH_MODAL)
    assert "hermes-hide-scrollbar" in src, "弹窗内没有应用隐藏滚动条的类"
    css = _src(ROOT / "frontend/src/global.css")
    assert ".hermes-hide-scrollbar" in css
    assert "scrollbar-width: none" in css, "Firefox 的隐藏没写"
    assert "::-webkit-scrollbar" in css, "WebKit 的隐藏没写"
    #: ⚠️ 只查**这个类自身的规则块**（全局 CSS 别处早有 `overflow: hidden`，
    #: 那些是别的用途，拿它们判红就是误伤）。
    block = css[css.index(".hermes-hide-scrollbar"):]
    block = block[: block.index("\n}") + 2]
    #: ⚠️ 只禁**真正会干掉滚动的**写法：`overflow:hidden/auto/scroll`。
    #: `-ms-overflow-style` 是「藏滚动条外观」的老 IE 属性，名字里带 overflow
    #: 但语义相反 —— 按字面判会误伤（第一版就是这么红的）。
    import re as _re

    assert not _re.search(r"overflow\s*:\s*(hidden|auto|scroll)", block), (
        "隐藏滚动条动了 overflow（会把滚动能力一起干掉，应只藏外观）"
    )


def test_wizard_table_shows_the_source_file_name():
    """用户口径：「表格顶部新增文件名称」。"""
    src = _src(PUBLISH_MODAL)
    assert "文件名称" in src, "表格顶部没有文件名"
    assert "source_file" in src, "没读 source_file"
    #: 后端必须真的透出这个字段（否则前端永远显示 `-`）
    #: ⚠️ 2026-10-22 换锚：读表内核已统一到 `tabular_reader.py`（设计 24 §22.19
    #: 多种格式：xlsx/xls/ods/csv…），填充点从 shop_package_precheck 的
    #: `source_file=xlsx.name` 迁到这里，值改为 `path.name`（与格式无关）。
    #: 判据不变：**必须真的把文件名填进去**，不能只是类型上有这个字段。
    reader = _src(ROOT / "backend/app/services/tabular_reader.py")
    assert "source_file=path.name" in reader, "读表内核没填 source_file"


def test_wizard_video_is_previewable():
    """用户口径 2026-10-08：「视频无法预览，应支持多种视频格式预览播放」。

    ⚠️ 原实现 `onClick={() => (isVideo ? undefined : onPreview(index))}` 把视频
    整个跳过 ⇒ 点视频**没反应**（先说现象：点了没动静）。预览体里也全是 `<img>`。
    """
    src = _src(PUBLISH_MODAL)
    assert "<video" in src, "前端没有任何 <video>（视频放不出来）"
    assert "controls" in src, "视频没有播放控件"
    #: 视频也必须能点开预览（不许再被 isVideo 短路掉）
    assert "isVideo ? undefined : onPreview" not in src, "视频仍被排除在预览之外"
    assert "onPreview(index)" in src
    #: 格式不支持时要有明确说明，而不是留在黑屏
    assert "VideoUnsupportedHint" in src, "浏览器放不了的格式没有明确提示"
    assert "onError" in src, "没有处理 <video> 的 onError（黑屏无解释）"


def test_backend_serves_video_with_correct_mime_and_range():
    """⚠️ 视频能播的两个硬条件：**正确 MIME** + **HTTP Range**。

    实测根因：本机 `mimetypes` 对 `.flv` 返回 `None`（响应无 Content-Type ⇒
    `<video>` 不播），`.avi`/`.mkv` 给的也不是标准注册类型 —— 所以不能靠自动猜。
    """
    api = _src(ROOT / "backend/app/api/platforms/xhs/package_precheck.py")
    assert "_MEDIA_TYPES" in api, "没有显式 MIME 表（靠猜会漏 .flv）"
    assert '"video/mp4"' in api, "没有给 mp4 正确 MIME"
    assert '"video/webm"' in api, "没有给 webm 正确 MIME"
    assert "Accept-Ranges" in api, "没声明 Range 支持（拖动进度条会失败）"
    assert "mimetypes" in api, "没有回退到 mimetypes 猜"


def test_multiple_video_formats_are_recognised():
    """用户口径：支持**多种**视频格式 —— 解析层要把常见格式都认成视频。"""
    draft = _src(ROOT / "backend/app/services/shop_draft_package.py")
    for ext in (".mp4", ".mov", ".webm", ".avi", ".mkv", ".flv", ".wmv", ".ts"):
        assert f'"{ext}"' in draft, f"{ext} 没被认成视频格式"
    #: 上传白名单也要跟上（否则走上传路径时被拒）
    staging_src = _src(ROOT / "backend/app/services/package_staging_service.py")
    for ext in (".webm", ".avi", ".mkv", ".flv"):
        assert f'"{ext}"' in staging_src, f"上传白名单缺 {ext}"


def test_forbidden_words_modal_is_sixty_percent_of_the_viewport():
    """用户口径 2026-10-08：「违规词弹窗固定**全屏的 60%**」。

    ⚠️ 是**视口百分比**不是固定像素 —— 换屏幕尺寸比例才一致。
    且与发布弹窗（75%）刻意不同，改成其中一个不该顺手动另一个。
    """
    src = _src(ROOT / "frontend/src/pages/platforms/xhs/forbidden-words-modal.tsx")
    assert 'width="60vw"' in src, "违规词弹窗宽度不是 60vw"
    assert '"60vh"' in src, "违规词弹窗高度不是视口 60%"
    assert 'width="50vw"' not in src, "还留着旧口径 50vw（已纠正为 60vw）"
    assert 'width="60vw"' in src, "弹窗宽度不是 60vw"
    #: 发布弹窗应保持 75vw（两个弹窗的尺寸口径各自独立）
    wizard = _src(PUBLISH_MODAL)
    #: ⚠️ 2026-10-08 口径改为 **宽 80% / 高 90%**（原来 75vw），
    #: 守卫跟着改 —— 钉住旧值等于让页面回退时守卫还是绿的。
    assert 'width="80vw"' in wizard, "发布弹窗宽度不是 80vw"
    assert 'width="75vw"' not in wizard, "还留着旧口径 75vw"


def test_forbidden_words_remark_column_is_180px_with_a_tooltip():
    """用户口径 2026-10-08：「备注列 180px，使用 tip 展示完整内容」。"""
    src = _src(ROOT / "frontend/src/pages/platforms/xhs/forbidden-words-modal.tsx")
    remark = src[src.index('title: "备注"'):]
    remark = remark[: remark.index("},", remark.index("render")) + 2]
    assert "width: 180" in remark, "备注列不是 180px"
    assert "Tooltip" in remark, "备注列没有 Tooltip 看全内容"
    assert "ellipsis" in remark, "备注列没有省略号截断"


def test_fixed_text_has_no_source_column_and_scope_needs_explicit_selection():
    """用户口径 2026-10-21（两条，都是真机报出来的）：

    ① 「**设置固定文本时不需要数据源下拉**」—— 固定文本是「整列无条件写成
       同一个值」，后端 `apply_fixed_text` 连 `values` 都不看 ⇒ 给它数据源下拉
       纯属误导（选了也没作用）。
    ② 「这个规则将当前列**所有数据均改成了**兜底文案，我本是**只想改勾选的数据**」
       —— 根因是老判据 `pickedRows.length > 0 ? scope : null`：用户把勾选**全部取消**
       时 `pickedRows` 是空数组 ⇒ 回落成「全表」⇒ 恰好改掉他刚取消的行。
       ⇒ 判据必须是「**有没有做过选择**」（`hasRowSelection`），不是「选了几行」。
    """
    src = (ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx").read_text(
        encoding="utf-8"
    )

    #: ① 固定文本不给数据源下拉
    k = src.index("const needsSourceColumn =")
    line = src[k : k + 120]
    assert 'pickedRule?.op !== "fixed_text"' in line, (
        f"固定文本仍然显示数据源列下拉：{line}"
    )

    #: ② 作用域**永远是当前勾选行**，没有任何「回落全表」分支
    #:    （用户 2026-10-21 真机：「只勾选了 24 行但执行了全部行」，toast 写「勾选的 456 行」）。
    #: ⚠️ 2026-10-22 换锚：`pickedRows`（state）在 rule worker 的闭包里拿不到最新值，
    #: 已改为读 `pickedRowsRef.current`。判据不变 —— **作用域永远是当前勾选行**，
    #: 不存在「没勾选就回落全表」的分支。
    assert "const scope: Set<number> = new Set(pickedRowsRef.current);" in src, (
        "作用域不是「当前勾选行」（还在做某种回落）"
    )
    #: ⚠️ 三条老写法都要反向排除 —— 它们每一个都会让「空/未初始化」退化成整表：
    for stale in (
        "pickedRows && pickedRows.length > 0 ? new Set(pickedRows) : null",
        "const scope: Set<number> | null = hasRowSelection",
        "const targetRows = scope ? [...scope] : table.rows.map",
    ):
        assert stale not in src, f"仍有「回落全表」的老判据：{stale}"
    #: 空勾选必须**中止**（一行都不改），不许换一种方式改整表。
    assert "if (scope.size === 0) {" in src, "空勾选没有中止"
    assert "当前没有勾选任何行" in src, "空勾选没有给用户提示"

def test_only_failed_toggle_materializes_full_selection_array():
    """用户报 2026-10-21（第 4 次，同一现象）：「还是会处理所有行，全部被覆盖成了 20，
    我只勾选了 24 行」。

    **真根因（前三次都没修到）**：`picked[tIndex]` 是「补丁式」状态 —— 只存用户
    显式改过的位，未覆盖的位一律回落 `true`（全选）。于是用户开着「只看不通过」
    看到的 24 个勾，在状态里**等价于 456 行全勾**，屏幕上其实一行都没勾过。
    只要默认值是 `true`，改默认值 / 改判据都救不了。

    ⇒ 判据：切「只看不通过」这个动作必须**显式落成完整数组**
    （打开 ⇒ 可见行 = 原勾选值、其余 false；关闭 ⇒ 整表 true），
    并且 `pickedRows` 的默认值回到「未定义 = 整表全选」（不再按可见行补）。
    """
    src = (ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx").read_text(
        encoding="utf-8"
    )

    #: ① 切筛选必须落全量数组（两个方向都要）
    k = src.index("let turningOn = false;")
    seg = src[k : k + 1400]
    assert "const next = table.rows.map(() => false);" in seg, (
        "打开筛选时没有落成完整数组（还是补丁式 ⇒ 未写过的行仍算已勾）"
    )
    assert "for (const i of scopeRows) next[i] = cur[i];" in seg, (
        "打开筛选时没有保留原勾选值"
    )
    assert "table.rows.map(() => true)" in seg, "关闭筛选时没有回到整表全选"

    #: ② `pickedRows` 默认值 = **整表按预检结果**（未定义时），不许再按可见行补。
    #: ⚠️ 2026-10-22 口径修订：默认值从「整表全 true」改为
    #: 「整表 − 预检不通过」（用户口径「sku 图未匹配到的默认不勾选，
    #: 规则执行失败无法执行的默认不勾选」）。判据不变的部分：**不按可见行补**。
    assert "const pickedRows = (picked[tIndex] ?? table.rows.map((_, i) => !precheckFailed(i)))" in src, (
        "pickedRows 的默认值又被改成了「按可见行补」"
    )
    assert "table.rows.map((_, i) => scopeRows.includes(i))" not in src, (
        "pickedRows 仍在按可见行生成默认值"
    )

def test_runtime_input_reaches_execute_callback():
    """用户报 2026-10-21：「tip 中输入的是 20，但执行时覆盖的是兜底文案」。

    ⚠️ 根因：`runPickedRule` 是 `useCallback`，**依赖数组漏了 `runtimeText`**
    （也漏了 `isFixedValueRule`）⇒ 回调一直是**首次渲染的闭包**，
    里面 `runtimeText` 恒为 `""` ⇒ `override` 恒 `undefined`
    ⇒ 后端只能取规则里的兜底文案。界面输入框看起来正常，值根本没用上。

    这是同一文件里**第二次**踩这个坑（`sourceColumn` 那次是第一次）。

    ⚠️ 判据必须**先剥注释**：注释里会引用 `runtimeText` 这个名字来解释坑，
    直接全文找会得到**假绿**（本守卫第一版就 ESCAPED，实测证实）。
    """
    src = (ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx").read_text(
        encoding="utf-8"
    )

    def strip_comments(text: str) -> str:
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        return re.sub(r"//[^\n]*", "", text)

    k = src.index("const runPickedRule = useCallback(")
    seg = strip_comments(src[k : k + 6000])
    deps_start = seg.index("}, [")
    deps = seg[deps_start : seg.index("]);", deps_start)]
    for must in ("runtimeText", "isFixedValueRule"):
        assert must in deps, (
            f"runPickedRule 的依赖数组漏了 `{must}` —— 回调会捕获首次渲染的旧值"
        )

def test_price_mapping_targets_equal_qianfan_fixed_columns():
    """用户口径 2026-10-21（截图为证）：

    > 「**下拉框不是全部列**，价格及库存**非动态列**均需要用户配置映射，
    >   且**售价、当前库存、改后库存、市场价均必须配置**，下拉框不修改；
    >   **售价列表头星号移除**；**缺少规格ID、条形码、商家编码**，
    >   你去看千帆后台，**价格及库存固定列必须保持一致**」

    千帆价格及库存表的**非动态列**（真机 CDP 实读，设计 24 §23）：
      售价 / 当前库存 / 改后库存 / 市场价 / 规格ID / 条形码 / 商家编码
    ⇒ 下拉候选必须**恰好是这 7 个**，不多（不许混 SKU名称/规格图/回落列/规格分类）
      不少（规格ID 曾在候选和表头里都缺）。
    """
    lib = (ROOT / "frontend/src/lib/precheck-publish.ts").read_text(encoding="utf-8")

    #: ① 候选恰好是千帆那 7 个固定列
    #: ⚠️ 2026-10-22 换锚：`PRICE_TARGET_OPTIONS` 已于 2026-10-21 删除
    #: （两份清单并存 = 改了没生效），下拉的**唯一真相源**是 `PRICE_TABLE_TARGETS`。
    #: 判据不变：内容仍必须恰好是千帆那 7 个固定列。
    k = lib.index("export const PRICE_TABLE_TARGETS: PriceTargetKey[] = [")
    block = lib[k : lib.index("];", k)]
    for must in (
        '"price"', '"current_stock"', '"stock"', '"market_price"',
        '"spec_id"', '"barcode"', '"seller_code"',
    ):
        assert must in block, f"下拉候选缺 {must}（千帆固定列）"
    for banned in ('"sku_name"', '"spec_image"', '"price_fallback"', '"spec_category"'):
        assert banned not in block, f"下拉候选混进了非固定列 {banned}"

    #: ② 四个必配（售价/当前库存/改后库存/市场价）
    k2 = lib.index("export const REQUIRED_PRICE_TARGETS: readonly PriceTargetKey[] = [")
    req = lib[k2 : lib.index("];", k2)]
    for must in ('"price"', '"current_stock"', '"stock"', '"market_price"'):
        assert must in req, f"必配清单缺 {must}"

    #: ③ 表头：售价**没有**星号；规格ID/条形码/商家编码都在
    page = (ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx").read_text(
        encoding="utf-8"
    )
    assert 'title: "售价 *"' not in page, "售价表头仍有星号（用户要求移除）"
    assert 'title: "售价"' in page, "售价表头丢了"
    for must in ('title: "规格ID"', 'title: "条形码"', 'title: "商家编码"',
                 'title: "当前库存"', 'title: "改后库存"'):
        assert must in page, f"价格表缺列 {must}"

def test_price_table_first_dim_is_main_second_is_sub():
    """用户口径 2026-10-21：「导入多个商品 sku 规格后需与千帆商品的价格及库存一致，
    **第一组规格为主----分第二组规格**」。

    千帆真机（CDP 实读，设计 24 §23）是**单层表头 + 第一列 rowspan 合并**：
    第一维度值 `rowspan=8`（一组一行），第二维度逐行展开，规格图一组一张。
    ⇒ 我方判据：
      ① 第一维度列有 `onCell`（合并），第二维度**没有**（逐行）；
      ② 同组非首行返回 `rowSpan: 0`（被上面那格吃掉）；
      ③ 规格图只在组首行渲染。
    """
    src = (ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx").read_text(
        encoding="utf-8"
    )
    #: ① 只有第一维度挂合并判据
    assert "di === 0" in src, "第一维度的合并判据（di === 0）丢了"
    #: ② 非组首行 rowSpan: 0
    assert "return { rowSpan: 0 }" in src, "非组首行没有 rowSpan:0（会重复显示主维度值）"
    #: ③ 组内 span 计算
    assert "span += 1" in src, "组内行数累加逻辑丢了"
    #: ④ 规格图只在组首行
    assert "if (prevSame) return null;" in src, "规格图没有按「组首才画」去重"

def test_price_table_fixed_columns_follow_the_mapping():
    """用户口径 2026-10-21：「**固定列预设需要用户配置映射关系**」。

    即价格及库存表的**价格块列**不是写死全显示，而是「用户在导入弹窗里配了
    哪几个映射，表里就出现哪几列」。没配的列不显示（配了才有意义）。
    ⚠️ 但**一个都没配时要显示全部** —— 否则初次进来表格只剩行号/规格图，
    用户会以为坏了，而映射入口又在这张表之后。
    """
    src = (ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx").read_text(
        encoding="utf-8"
    )
    assert "const mappedTargets = new Set(" in src, "缺「按映射过滤」的判据"
    #: ⚠️ 2026-10-22 换锚：`current_stock` 已并入（用户口径「当前库存」必须可配，
    #: 且表头要显示），`barcode` / `seller_code` 移出（它们恒显示、不受映射控制）。
    assert 'const mappedCols = ["price", "current_stock", "market_price", "stock"];' in src, (
        "可映射列清单变了（须与 PriceTargetKey 的价格块子集一致）"
    )
    #: 空映射兜底
    assert "mappedTargets.size === 0" in src, "缺「没配映射时显示全部」的兜底"
    assert "? base" in src, "兜底分支没返回全部列"
    #: 平台自身字段不参与映射、恒显示
    #: ⚠️ 2026-10-22 换锚：`current_stock` 已从只读清单移出 —— 用户口径
    #: 「售价、当前库存、改后库存、市场价均必须配置映射」⇒ 当前库存变成
    #: **必配的可映射列**（见上面 mappedCols），不再是平台生成只读列。
    assert 'new Set(["rowNo", "specImage"])' in src, "平台生成列的只读清单变了"

def test_row_selection_defaults_to_visible_rows_and_fixed_text_needs_runtime_value():
    """用户口径 2026-10-21（同一现象第 2、3 次报，根因是两处不同的事）：

    ① 「选了 24 行，固定文本规则**依旧执行了全部 456 行**」——
       根因：`picked[tIndex]` 未定义时用 `?? 整表全 true` 补成「456 行全勾」，
       而「只看不通过」只收窄**渲染**，不改勾选状态 ⇒ 屏幕 24 个勾、状态 456 全勾。
       ⇒ 默认值必须取**当前可见行**（`scopeRows`），且 `scopePicked` 从
       `pickedRows` **派生**（不许再按 `picked[tIndex]` 算一遍）。
    ⚠️ 曾经把「请输入固定文本」误判成规则里的提示语（它其实**是用户输入的值**），
    并据此加了「固定文本必须现场输入」的限制 —— 已在用户澄清后撤销。
    这里只钉真正的根因：勾选默认值必须是**可见行**。
    """
    src = (ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx").read_text(
        encoding="utf-8"
    )

    #: ① 勾选默认值 = **整表按预检结果**（未定义时）—— 老行为「整表全选」已被
    #:    2026-10-22 口径修订为「预检不通过的默认不勾」；
    #:    「只看不通过」的落值仍是**显式**的（见下面 ②），不靠默认值猜。
    assert "const pickedRows = (picked[tIndex] ?? table.rows.map((_, i) => !precheckFailed(i)))" in src, (
        "pickedRows 的默认值语义变了（未定义应为「整表 − 预检不通过」）"
    )
    #: ② 「只看不通过」必须**显式落全量数组**（不能再靠默认值猜作用域）。
    #:    ⚠️ 默认值 `?? 整表全 true` 本身是**老行为**（进页面默认全选），保留；
    #:    问题在切筛选时没有把「可见 24 行」落进状态，导致执行时仍是整表。
    #: ③ `scopePicked` 从 pickedRows 派生
    assert "const pickedSet = new Set(pickedRows);" in src, (
        "scopePicked 没有从 pickedRows 派生（两份实现）"
    )
    #: ③ 现场输入优先、留空用规则里的值（⚠️ 「请输入固定文本」**是用户输入的真值**，
    #:    不是提示语 —— 曾经误判成兜底文案而加了「必须现场输入」的限制，已撤销）。
    assert "isFixedValueRule && runtimeText.trim() ? { text: runtimeText.trim() } : undefined" in src, (
        "固定文本的「现场值优先 / 留空用规则值」判据变了"
    )

def test_precheck_failure_has_one_judgment_with_two_tiers():
    """用户口径 2026-10-21：「预检不通过的数据包括，**规则执行失败**、
    **图片未匹配成功**」。

    两条硬判据：
    ① **唯一判据**：`failureOf(rowIndex)` 一处算，标红 / 置灰 / 提示 / 统计 /
       筛选全部读它 —— 不许任何一处另写 `blockedRows.has(...) || badRuleRows.has(...)`
       （本仓反复踩「同一判据两份实现」的坑，DEF-044）。
    ② **两档分开表达**：图片未匹配是**结构性**不可发布（禁勾选），
       规则失败是**数据**问题（可勾选接管）。合并成一句「不通过」用户不知道
       该补图还是该改数据 —— 上一版就是只按图片判，规则失败的行
       显示「通过：本地图已对齐」而整行发红，自相矛盾。
    """
    src = (ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx").read_text(
        encoding="utf-8"
    )

    #: ① 唯一判据存在，且按两档返回原因（不是布尔）。
    assert "const failureOf = (rowIndex: number): FailReason =>" in src, "缺统一判据 failureOf"
    body_start = src.index("const failureOf = (rowIndex: number): FailReason =>")
    body = src[body_start : body_start + 400]
    assert '"image"' in body and '"rule"' in body, f"判据没分两档：{body[:300]}"
    #: ⚠️ 图片档必须优先（勾了也发不出去 ⇒ 原因更硬）。
    assert body.index('"image"') < body.index('"rule"'), "图片档应在规则档之前判"

    #: ② `isBadRow` 必须**从判据派生**，不许自己再写一遍两个集合。
    k = src.index("const isBadRow = (rowIndex: number) =>")
    line = src[k : k + 120]
    assert "failureOf(rowIndex) !== null" in line, (
        f"isBadRow 没有从 failureOf 派生（两份判据）：{line}"
    )

    #: ③ 预检列必须分档显示（规则失败不许显示成「通过」）。
    v = src.index("const fail = failureOf(record.__index);")
    seg = src[v : v + 700]
    assert 'fail === "rule"' in seg and "规则执行失败" in seg, "预检列没分「规则执行失败」档"
    assert "通过：本地图已对齐" in src, "「通过」文案丢失"

    #: ④ 顶部统计两档都在。
    assert "图片未匹配 {imageFailCount} 行" in src, "顶部缺「图片未匹配」计数"
    assert "规则失败 {ruleFailCount} 行" in src, "顶部缺「规则失败」计数"

def test_select_all_follows_the_visible_filter_not_the_whole_table():
    """用户口径 2026-10-21：「只看不通过数据时，那么复选框勾选的应该是检测不通过的，
    **原理是全选反选**」。

    ⚠️ 根因：筛选原先**只收窄渲染集** —— 表头全选与「已选 N 行」仍按**整表**算。
    于是「只看不通过」里点全选，实际把 456 行全勾上（含屏幕上看不见的通过行），
    用户看到的是「我只勾了十几行，怎么全被勾了」。这条口径要求
    全选 / 反选 / 计数 全部以**可见集合**为准。

    判据（源码级，页面 tsx 不被 vitest 覆盖）：
    ① 存在统一的「可见行」判据 `scopeRows` / `scopeSelectable` / `scopePicked`；
    ② 表头全选的 checked / indeterminate 用 `scopeSelectable`，**不再**用整表的
       `selectableCount`（该变量必须已删除，否则下次改的人又会拿它算）；
    ③ 全选写入只遍历 `scopeSelectable`（不许遍历 `table.rows`）；
    ④ 「已选 N 行」用 `scopePicked`。
    """
    src = (ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx").read_text(
        encoding="utf-8"
    )

    #: ① 统一判据
    for name in ("scopeRows", "scopeSelectable", "scopePicked", "viewingFailedOnly"):
        assert f"const {name}" in src, f"缺可见行判据 {name}"

    #: ② 整表可选数必须删除（留着就会被再次误用）
    assert "const selectableCount" not in src, (
        "整表 selectableCount 仍在 —— 表头全选又会按整表算"
    )

    #: ③ 表头全选的判据指向可见集合
    k = src.index("scopeSelectable.length > 0")
    head = src[k : k + 900]
    assert "scopeSelectable.length" in head and "scopePicked.length" in head, (
        f"表头全选判据没走可见集合：{head[:300]}"
    )

    #: ④ 全选写入只动可见行
    assert "for (const i of scopeSelectable) next[i] = on;" in src, (
        "全选写入没有只作用于可见行（会对整表 table.rows 写）"
    )

    #: ⑤ 「已选」按可见集合
    assert "已选 {scopePicked.length} 行" in src, "「已选 N 行」没按可见集合算"

def test_bad_rows_are_painted_at_cell_level_not_row_level():
    """用户报 2026-10-21：「规则无法处理时**依旧未红框提示**，只取消了勾选」。

    根因：CSS 用 `.publish-sku-row-blocked > td`（`<tr>` 的**直接子级**），
    而 antd 6 虚拟表格里 `<td>` 被包在虚拟容器中 ⇒ 选择器匹配不到，
    红框**一格都没画出来**（预检未通过的行其实也一直没红，只是没人报）。

    ⇒ 判据：标红必须走**单元格**（各列 `onCell`），并且 CSS 不许再依赖 `> td`。
    """
    src = (ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx").read_text(
        encoding="utf-8"
    )
    css = (
        ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.css"
    ).read_text(encoding="utf-8")

    #: ① 统一判据存在（预检未通过 + 规则失败同一套）。
    assert "const isBadRow = " in src, "没有统一的「要标红吗」判据"
    #: ② 至少 4 列走单元格级标红（勾选 / SKU 图 / 数据列 / 预检）。
    assert src.count("publish-sku-cell-blocked") >= 4, (
        "单元格级标红覆盖不足（应 ≥4 列）"
    )
    #: ③ CSS 里必须有该类的样式，且**不许**再靠 `> td` 画红框。
    assert ".publish-sku-cell-blocked" in css, "缺单元格标红样式"
    assert ".publish-sku-row-blocked > td {" not in css, (
        "仍在用 `> td` 选择器画红框 —— 虚拟表格下匹配不到，等于没画"
    )

def test_bad_row_marks_never_touch_values_and_clear_on_user_check():
    """用户口径 2026-10-21（两条，同一处）：

    ① 「红框标记，**原值不要改变**」—— 标红是提示，不是改数据。
    ② 「**用户主动勾选该行时取消红框提示**」—— 用户接管这一行，红框就该消失
       （红框的语义是「请我确认」，勾选 = 已确认）。

    ⚠️ ② 的**前提**是勾选框**不能被禁用** —— 之前规则失败的行 `disabled`，
    用户根本点不了，这条口径物理上无法满足。故判据里同时钉住「不再禁用」。
    """
    src = (ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx").read_text(
        encoding="utf-8"
    )

    #: ① 值不变：两条规则路径的失败分支都必须 `return cell` / `return row`（原样）。
    rules = (ROOT / "frontend/src/lib/precheck-publish.ts").read_text(encoding="utf-8")
    k = rules.index("if (next === null) {")
    seg = rules[k : k + 160]
    assert "badRows.add(rowIndex)" in seg and "return cell;" in seg, (
        f"运算类规则失败时没有保持原值：{seg}"
    )
    pat = (ROOT / "frontend/src/lib/table-pattern.ts").read_text(encoding="utf-8")
    k2 = pat.index("if (value === null) {")
    seg2 = pat[k2 : k2 + 160]
    assert "failed.push(rowIndex)" in seg2 and "return row;" in seg2, (
        f"正则规则失败时没有保持原值：{seg2}"
    )

    #: ② 勾选框不得对「规则失败行」禁用；勾上后必须清掉 badRuleRows。
    k3 = src.index("render: (_: unknown, record: { __index: number }) => (")
    #: ⚠️ 不能用固定窗口（1400 字）—— 注释一多窗口就截断，断言变成假红/假绿。
    #: 按**结构**取：从勾选列 render 起，到下一个列的 key（SKU 图列）为止。
    #: 勾选列的结束边界 = 下一列（SKU 图）的声明行 `key: "__matched_image"`。
    end = src.find('key: "__matched_image"', k3)
    assert end > k3, "找不到勾选列的结束边界（页面结构变了，更新本守卫）"
    chk = src[k3:end]
    #: ⚠️⚠️ 2026-10-22 **口径再次修订**（用户原话）：
    #: 「不管是否预检通过，均应自动勾选，**是否勾选用户决定**」
    #: ⇒ 勾选框**一律不禁用**，预检结论与勾选状态彻底解耦。
    #: 历史沿革（不静默删除）：2026-10-21 那一版对「图片未匹配」档禁用
    #: （`failureOf(...) === "image"`），现已推翻。
    assert "disabled" not in chk, (
        "勾选框被禁用了 —— 用户口径是「是否勾选用户决定」，任何档都必须可勾\n"
        + chk[:400]
    )
    assert "disabled={blockedRows.has(record.__index)}" not in chk, (
        "禁选又按 blockedRows 判回来了"
    )
    assert "setBadRuleRows" in chk and "if (on)" in chk, (
        "勾上时没有清除红框标记（用户口径：主动勾选即取消红框）"
    )

def test_every_rule_path_marks_unprocessable_rows():
    """用户口径 2026-10-21：「**规则处理异常，无法处理的行数据红色标记并取消勾选**」
    +「无法计算的列应红框展示，**适用于所有规则**，规则无法处理的数据，
    该行红框展示取消勾选，**需用户手动处理**」。

    ⚠️ 这条钉的是**覆盖面**：三条规则路径 + 手动改值都必须走同一个收口函数。
    只接一条（曾经只接了数据包规则）时，另外两条改坏了数据也不标红 ——
    用户看到的「执行成功」是假的。
    """
    src = (ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx").read_text(
        encoding="utf-8"
    )

    #: ① 统一收口函数存在，且**标红 + 取消勾选**两件事都做。
    assert "const markRuleFailures = " in src, "没有统一收口函数"
    at = src.index("const markRuleFailures = ")
    body = src[at : src.index("\n  };", at)]
    assert "setBadRuleRows" in body, "收口函数没有标红"
    assert "setPicked" in body, "收口函数没有取消勾选"

    #: ② 四条路径都调它（批量修改 / 正则取值 / 数据包规则 / 手动改值）。
    calls = src.count("markRuleFailures(")
    assert calls >= 4, f"只有 {calls} 处调用（应 ≥4：三条规则路径 + 定义）"

    #: ③ 手动改值也要按判据收口（且改好要**清掉**标记）。
    cell = src[src.index("const setCell = ") :]
    cell = cell[: cell.index("\n  };")]
    assert "numericCellIssue" in cell, "手动改值没按规则判据判"
    assert "next.delete(key)" in cell, "手动改好后没清掉红框标记"

    #: ④ 判据来源必须包含「规则自报的失败行」，不能只看值。
    fn = src[src.index("const failedRowsOfColumn = ") :]
    fn = fn[: fn.index("\n  };")]
    assert "ruleFailedRows" in fn, "没读规则自报的失败行（文本列不命中会被漏掉）"
    assert "batchRuleBadRows" in fn, "没读批量规则的失败行"


def test_forbidden_media_is_selectable_but_unchecked_by_default():
    """用户口径 **2026-10-21（取代 2026-10-08 的「不允许勾选至灰」）**：

    > 「检测到违规词的媒体资源放开也可选择，检查完默认是不选择的，
    >   不再固定为不可选，决策权交给用户」

    三条判据（缺一条都会让用户「勾不上」或「勾了没用」）：
      ① **默认可勾**：违规项**不**进禁选名单（`locked` 只收格式不支持 / 超限）；
      ② **默认不勾**：`applyDefaults` 里仍按 `!has_forbidden` 给初始值；
      ③ **提醒仍在**：红框 + 角标 + 文案（风险提示不能随「放开选择」一起消失）。
    """
    src = _src(PUBLISH_MODAL)

    #: ① 禁选名单的 filter **不能**再含 `has_forbidden`
    at = src.index("const locked = new Set(")
    block = src[at : src.index(");", at) + 2]
    assert "has_forbidden" not in block, (
        f"违规项仍在禁选名单里（应把决策权交给用户）：{block}"
    )
    #: 结构性原因必须仍在名单里（否则会让人勾了发不出去的图）
    assert "unsupported" in block, "平台不收的格式丢了禁选"
    assert "oversized" in block, "超 5M 上限丢了禁选"

    #: ② 默认不勾（唯一保留 has_forbidden 的那处赋值）
    assert "!item.has_forbidden && item.ocr_ok === true" in src, (
        "违规项不再默认不勾了（用户口径要求「检查完默认是不选择的」）"
    )

    #: ③ 提示仍在（**违规词角标**，不是卡片底部那行文字 ——
    #:    用户口径 2026-10-21：「命中违规词（默认未勾选）」文本移除）
    assert "命中违规词（默认未勾选）" not in src, "卡片底部那行说明文案仍存在（用户要求移除）"
    assert "h.word" in src or "item.hits" in src, "违规词角标没了（命中提示必须有）"
    #: 且**不许**再说「不可选」（与放开选择的决策冲突）
    assert "命中违规词，不可选" not in src, "文案仍说「不可选」，与放开选择的决策矛盾"

    #: 批量按钮仍走 locked（规范不变，只是名单内容变了）
    assert "setZone(keys, true, locked)" in src, "全选没遵守禁选名单"
    assert "locked.has" in src, "setZone 里没判禁选"


def test_precheck_can_be_rerun():
    """用户口径 2026-10-08：「支持重复预检」—— 检过之后按钮仍可点。

    ⚠️ 文案已按用户口径 2026-10-13 改口径：「预检按钮只预检图片是否存在违规词，
    **按钮的文案使用重新明确**」⇒ 检过后的入口文案由「重新预检」改为
    **「重新检测违规词」**（同族词「检测违规词」= 未检过的态）。本用例的**判据**
    （检过仍可点）不变，只换文案锚点。
    """
    src = _src(PUBLISH_MODAL)
    assert "重新检测违规词" in src, "没有「重新检测违规词」入口（检过就disabled了）"
    #: ⚠️ 判据落在**预检按钮自己那一小段**里（不能从「重新预检」往后大段切 ——
    #: 那会切到下方的提示文案，把无关的 `!inspected` 当成按钮禁用条件，实测误红）。
    #: 窗口要落在按钮**自己那一段**（从 onClick 起，到 `>` 收口为止）。
    at = src.rindex("onClick={onPrecheck}")
    btn = src[at : src.index(">", at) + 1]
    assert "disabled={!precheck" in btn, "预检按钮没按「有没有读过数据包」禁用"
    #: 禁用条件里不许出现 inspected（那会导致只能检一次）。
    #: 只取 `disabled={...}` 花括号里的表达式（60 字窗口会溢到下一行的
    #: `loading={inspected ...}`，实测就是这样误红的）。
    head = btn[btn.index("disabled=") :]
    expr = head[head.index("{") + 1 : head.index("}")]
    assert "inspected" not in expr, f"按钮被 inspected 锁死（不能重复预检）：{expr}"


def test_precheck_shows_a_mask_with_elapsed_seconds():
    """用户口径 2026-10-08：「点击预检时页面加蒙版避免误触，并蒙版统计耗时」。"""
    src = _src(PUBLISH_MODAL)
    assert "beginWait" in src and "endWait" in src, "没有等待蒙版的开/关"
    assert "waitElapsedSec" in src, "没有耗时秒表"
    #: ⚠️ 2026-10-20 统一蒙版：耗时由 `<WaitMask elapsed={waitElapsedSec}>` 渲染，
    #: 不再是手拼的「已耗时 Ns」字符串（标题与秒数分离成两个 slot）。
    assert "elapsed={waitElapsedSec}" in src, "蒙版上没有显示耗时"
    #: 必须是**幂等**起表（否则重复预检时耗时被反复清零，数字永远很小）
    assert "waitStartedAtRef.current === null" in src, "起表不幂等（耗时会反复清零）"


def test_package_path_input_is_read_only():
    """用户口径 2026-10-08：「地址栏**不允许输入**，只能预览」。

    路径唯一来源是「选择文件夹」（后端弹系统选择器）。用 `readOnly` 而非 `disabled`
    （内容仍要能选中复制核对）。

    ⚠️ **2026-10-12 例外**（取代「绝不允许输入」的绝对化）：后端 `NO_DESKTOP`
    （无图形界面、弹不出选择框）时**解锁手填** —— 否则既不让弹框又不让输 = 死路。
    判据因此从「有 `readOnly` 且**绝无** `onPackagePathChange`」改为
    「有 `readOnly`（常态只读）+ 手填是**受控例外**（`manualPathFallback`）」。
    """
    src = _src(PUBLISH_MODAL)
    assert "readOnly" in src, "地址栏还能输入（用户要求只读）"
    #: 常态只读由 `readOnly={!manualPathFallback}` 表达（手填是 NO_DESKTOP 例外）
    assert "readOnly={!manualPathFallback}" in src, "常态应只读（仅无桌面时例外）"
    assert "manualPathFallback" in src, "缺无桌面手填兜底（否则弹不出框时死路）"
    #: 路径只能由「选择文件夹」写入
    assert "setPackagePath(picked.package_path)" in src, "选择文件夹没有回填路径"


def test_missing_tkinter_does_not_mean_no_desktop():
    """**`tkinter` 缺失 ≠ 没有图形界面**（2026-10-20 实测的真缺陷）。

    现场：装好的客户端点「选择文件夹」，只弹红条
    「当前环境没有可用的图形界面，请直接填写数据包目录的绝对路径」。

    根因两层，两层都要钉住：

    ① `packaging/SpiderXHS.spec` 把 `tkinter` **排除**出包了（注释还写着
      「与桌面运行时无关」—— 实际上这个选目录框就靠它），所以装包用户**必然**
      没有这个模块；
    ② `pick_local_package` 里 `import tkinter` 失败就**直接抛 `NO_DESKTOP`**，
      从不尝试 PowerShell 的原生对话框 —— 于是在有桌面的机器上也报「没有图形界面」。

    判据：ImportError 必须**降级到 PowerShell 回退**，而不是直接报错；
    并且「取消」与「弹不出」必须分开（`""` vs `None`）。
    """
    src = _src(ROOT / "backend/app/api/platforms/xhs/package_precheck.py")
    #: ① 打包必须带上 tkinter
    spec = _src(ROOT / "packaging/SpiderXHS.spec")
    assert '"tkinter"' not in spec.split("excludes=")[1].split("]")[0], (
        "打包把 tkinter 排除了 —— 装包用户点「选择文件夹」必然失败"
    )
    #: ② ImportError 只做标记，不抛错
    assert "tk_missing = True" in src, "tkinter 缺失应降级而不是直接报 NO_DESKTOP"
    assert "if not tk_missing:" in src, "缺 tkinter 时应继续尝试其它弹窗方式"
    #: ③ 缺 tkinter 时走 PowerShell（用两端简单锚点，避开换行差异）
    seg = src[src.index("if not tk_missing:") : src.index("class LocalPackageRequest")]
    assert "_ask_directory_windows()" in seg, "缺 tkinter 时没有回退到 PowerShell 对话框"
    #: ④ 三态要分开：`None` = 弹不出，`""` = 取消
    assert 'if chosen == "" and _has_interactive_desktop():' in src, (
        "「取消」与「弹不出」没分开 —— 会把取消误报成没有图形界面"
    )
    assert "def _has_interactive_desktop()" in src, "缺交互式桌面判据"


def test_forbidden_words_uses_a_tree_list_and_a_search_area():
    """用户口径 2026-10-08：「列表顶部添加搜索区域和搜索条件，分组下拉，
    关键词模糊，列表使用 antd 的**父子列表**展示」。"""
    src = _src(ROOT / "frontend/src/pages/platforms/xhs/forbidden-words-modal.tsx")
    assert "__isGroup" in src, "没有父子行的结构判据"
    assert "children" in src, "没有把词挂成子行（不是父子列表）"
    assert "defaultExpandAllRows" in src, "父子列表没默认展开"
    #: 搜索区：分组下拉 + 关键词 + 两者都在顶部
    assert "filterGroup" in src, "缺少分组搜索条件"
    assert "filterKeyword" in src, "缺少关键词搜索条件"


def test_forbidden_words_add_and_edit_use_a_dialog():
    """用户口径：「违规词添加修改使用**弹窗**使用」—— 不再是顶部内联表单。"""
    src = _src(ROOT / "frontend/src/pages/platforms/xhs/forbidden-words-modal.tsx")
    assert "WordEditor" in src, "没有独立的增改弹窗组件"
    assert "新增违规词" in src, "没有新增入口"
    #: 旧的顶部内联表单形态应已删除（`<Form form={form}` 直接铺在主体里）
    body = src[src.index("return (\n    <>") :]
    assert "<Form form={form}" not in body.split("function WordEditor")[0], (
        "主弹窗里还留着内联表单（应改成子弹窗）"
    )


def test_forbidden_words_group_is_wired_through_the_api():
    """分组必须一路通到后端（建 / 改 / 筛 / 列分组）。"""
    api = _src(ROOT / "frontend/src/lib/api.ts")
    assert "group_name" in api, "API 层没有 group_name"
    assert "fetchForbiddenWordGroups" in api, "没有分组清单接口"
    modal = _src(ROOT / "frontend/src/pages/platforms/xhs/forbidden-words-modal.tsx")
    assert "fetchForbiddenWordGroups" in modal, "弹窗没拉分组"


def test_default_match_mode_is_exact_everywhere():
    """⚠️ 用户口径 2026-10-08：「违规词的匹配方式**默认精确**」。

    三处必须同值（任一处不一致就会出现「界面显示精确、实际按包含跑」）：
    后端模型默认、后端 `validate_match_mode` 兜底、前端表单初值。
    """
    model = _src(ROOT / "backend/app/models/forbidden_word.py")
    assert "default=MATCH_MODE_EXACT" in model, "模型默认不是精确"
    service = _src(ROOT / "backend/app/services/forbidden_word_service.py")
    assert "return MATCH_MODE_EXACT" in service, "validate_match_mode 兜底不是精确"
    modal = _src(ROOT / "frontend/src/pages/platforms/xhs/forbidden-words-modal.tsx")
    assert "const DEFAULT_MATCH_MODE = 1" in modal, "前端默认不是精确"


def test_precheck_lets_you_pick_groups_and_defaults_to_all():
    """用户口径：「点击预检支持选择指定违规词分组，支持多组，**默认全部分组**」。"""
    src = _src(PUBLISH_MODAL)
    assert "precheckGroups" in src, "预检没有分组选择"
    #: 初值必须是空（= 全部分组），不能默认选中某一个
    assert "useState<string[]>([])" in src, "分组初值不是空（默认应为全部分组）"
    api = _src(ROOT / "frontend/src/lib/api.ts")
    assert "groups" in api, "预检请求没带 groups"


def test_group_picker_is_a_dialog_not_an_inline_select():
    """用户口径 2026-10-08：「点击预检发布商品将违规词分组选择使用**弹窗**展示」。

    ⚠️ 判据是「选择器在一个 Modal 里」，不是内联的 `Select` —— 内联那个
    在工具栏里挤不下、分组多了也没法好好列。
    """
    src = _src(PUBLISH_MODAL)
    assert "groupPickerOpen" in src, "没有分组选择弹窗的开关"
    assert "选择违规词检测分组" in src, "弹窗标题不在（可能还是内联的）"
    #: 触发它的按钮
    assert "违规词分组" in src, "没有打开分组弹窗的按钮"
    #: ⚠️ 判据落在**分组选择弹窗**内：它的多选必须用 `Checkbox.Group`
    #: （2026-10-09 修：原先断全文件不得出现 `mode="multiple"`，但商品属性区的
    #: 多选属性（`Select mode="multiple"`）是合法的另一处 —— 全文件判据会误伤）。
    picker = src[src.index("选择违规词检测分组"):]
    picker = picker[: picker.index("</Modal>")]
    assert 'mode="multiple"' not in picker, "分组弹窗里还留着内联多选框（应改成复选组）"
    #: 弹窗用复选组（多组选择）
    assert "Checkbox.Group" in picker, "弹窗里不是多选"
    #: 关了弹窗也要能看见当前生效范围
    assert "检测范围" in src, "没有已选分组摘要"


def test_group_input_is_a_string_valued_control_not_a_tags_select():
    """⚠️ 实测缺陷（2026-10-08 用户报「新增违规词，无法输入分组，输入框不展示
    输入的内容」）：分组字段原来用 `<Select mode="tags">` 拿「可选可输」，
    但 `tags` 形态的受控值是**数组**，而 `Form.Item` 的字段是**字符串** ——
    两者对不上，输入的文字不回填（表现就是「打了字看不见」）。

    判据：分组字段必须是**字符串受控**的控件（`AutoComplete` / `Input`），
    且**不许**在 `Form.Item` 里出现 `mode="tags"`。
    """
    src = _src(ROOT / "frontend/src/pages/platforms/xhs/forbidden-words-modal.tsx")
    field = src[src.index('label="分组"') :]
    field = field[: field.index("</Form.Item>")]
    assert 'mode="tags"' not in field, (
        "分组字段还在用 tags 形态（数组值 vs 字符串字段 ⇒ 输入不回填）"
    )
    assert "AutoComplete" in field, "分组字段应用字符串受控的 AutoComplete"
    #: 表单字段本身仍是字符串类型（与后端 `group_name: str` 一致）
    assert "group_name?: string" in src


def test_group_word_column_is_fixed_180px():
    """用户口径 2026-10-08：「分组 / 违规词列固定 180px」。

    ⚠️ 必须是**固定**宽度（`width: 180`），不是 `minWidth` —— 后者会随内容抖动。
    固定宽度下长内容必须省略 + Tooltip，否则会把列撑开（等于没固定）。
    """
    src = _src(ROOT / "frontend/src/pages/platforms/xhs/forbidden-words-modal.tsx")
    col = src[src.index('title: "分组 / 违规词"') :]
    col = col[: col.index("},\n            {")]
    assert "width: 180" in col, "该列不是固定 180px"
    assert "minWidth: 180" not in col, "用了 minWidth（会随内容抖动，不是固定）"
    assert "ellipsis" in col, "固定宽度下没有省略（长词会把列撑开）"


def test_forbidden_words_pages_by_group_with_size_options():
    """用户口径 2026-10-08：「违规词分页，每页 20/30/50，**按照组分页**」。

    ⚠️ 分页单位必须是**分组**（父行），不是词行 —— 否则会出现「某组只显示一半词」。
    """
    src = _src(ROOT / "frontend/src/pages/platforms/xhs/forbidden-words-modal.tsx")
    assert "pageSizeOptions" in src, "没有每页条数选项"
    for size in ("20", "30", "50"):
        assert size in src, f"缺每页 {size} 的选项"
    #: `total` 必须是**分组数**，不是词数
    assert "total: allGroupRows.length" in src, "分页总量不是分组数（不是按组分页）"
    assert "slice(" in src, "没有对分组做分页切片"


def test_forbidden_word_group_can_be_toggled():
    """用户口径：「组新增启用停用功能」。"""
    modal = _src(ROOT / "frontend/src/pages/platforms/xhs/forbidden-words-modal.tsx")
    assert "toggleGroup" in modal, "没有整组启停的处理器"
    assert "停用整组" in modal and "启用整组" in modal, "父行没有整组启停按钮"
    api = _src(ROOT / "frontend/src/lib/api.ts")
    assert "setForbiddenWordGroupEnabled" in api, "API 层没有整组启停"
    assert "__ungrouped__" in api, "未分组的占位符没处理（空串当路径参数不合法）"


def test_group_toggle_actually_reaches_the_matcher():
    """⚠️ 组停用必须让组内词退出匹配（只改界面标志位 = 假开关）。

    守的是**服务层**那行过滤 —— 界面守得再严，OCR 照命中就是白做。
    """
    svc = _src(ROOT / "backend/app/services/forbidden_word_service.py")
    assert "def enabled_groups" in svc, "没有组启用态的判据"
    entries = svc[svc.index("def enabled_entries") :]
    entries = entries[: entries.index("def get_word")]
    assert "in alive" in entries, "enabled_entries 没有按组启用态过滤（停用组仍参与匹配）"


def test_each_group_is_capped_at_500_words():
    """用户口径：「每组违规词限制最多 500 条」。"""
    model = _src(ROOT / "backend/app/models/forbidden_word.py")
    assert "MAX_WORDS_PER_GROUP = 500" in model, "上限不是 500"
    svc = _src(ROOT / "backend/app/services/forbidden_word_service.py")
    assert "_assert_group_capacity" in svc, "没有容量检查"
    assert "FORBIDDEN_WORD_GROUP_FULL" in svc, "超限没有结构化错误码"
    #: ⚠️ count 必须用 `scalar()`，`scalar_one()` 在多行时会抛异常并被吞成 500，
    #: 表现为上限完全不生效（实测过一次）。
    cap = svc[svc.index("def _assert_group_capacity") : svc.index("def create_word")]
    #: ⚠️ 只查**代码行**：解释「为什么不能用 scalar_one()」的注释里必然出现
    #: 那个名字，整段查会把自己的说明当违规（实测误红）。
    code_lines = [
        ln.strip()
        for ln in cap.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert ".scalar()" in code, "容量计数没用 scalar()"
    assert "scalar_one()" not in code, (
        "容量计数用了 scalar_one()（多行会抛 MultipleResultsFound，上限静默失效）"
    )


def test_publish_wizard_picks_a_folder_through_the_backend():
    """用户口径：「点击加载数据包**选择本地文件夹**，获得本地夹地址后直接读取」。"""
    src = _src(PUBLISH_MODAL)
    assert "pickPackageFolder" in src, "没接后端弹窗接口（浏览器拿不到真实路径）"
    assert "选择文件夹" in src, "缺少「选择文件夹」入口"


def test_publish_wizard_reads_the_local_folder_instead_of_uploading():
    """⚠️ 用户 2026-10-08 口径：「加载本地数据包内的媒体资源**不要上传**」。

    第一版是 `<input webkitdirectory>` 逐文件 POST（70 个文件、几十 MB），
    口径变更后必须**没有**上传路径 —— 否则用户选一次目录就是一次全量上传。
    """
    src = _src(PUBLISH_MODAL)
    code = _code_only(src)
    assert "webkitdirectory" not in code, "不该再走目录上传（用户口径：不要上传）"
    assert "precheckPackage(" not in code, "不该再调上传版预检接口"
    assert "precheckLocalPackage(" in code, "应改调本地路径版预检接口"


def test_publish_wizard_red_line_warning_was_removed_by_request():
    """⚠️ 用户口径 2026-10-08：**移除**「发布是真实上架操作」说明块与
    「我知道本次会真实发布到线上」确认勾选。

    ⚠️ 注意这**不是**放宽红线：后端仍默认 `off_shelf_after=true`（发布成功
    立即下架），红线由后端执行（见 `test_publish_wizard_never_sends_off_shelf_after_false`）。
    本条钉的是「前端不再摆这两处文案与勾选」，防止它们被无意加回来。
    """
    #: ⚠️ 反向断言必须先剥注释 —— 解释「为什么移除」的注释里会提到这些字样
    #: （本仓库的既有纪律，DEF-057 同型）。
    code = _code_only(_src(PUBLISH_MODAL))
    assert "真实上架操作" not in code, "「发布是真实上架操作」说明块应已移除"
    assert "我知道本次会真实发布" not in code, "确认勾选应已移除"
    assert "acknowledged" not in code, "二次确认状态应已移除（否则提交会被它挡住）"


def test_config_step1_only_offers_passed_and_checked_images():
    """第二步商品图片的候选 = **预检通过 且 被选中**的媒体（用户口径 2026-10-08）。

    ⚠️ 曾经的写法只按 `group === "main"` 过滤 ⇒ 用户在第一部取消勾选
    「商品属性图」后，第二步照样把它传上去，勾选形同虚设。
    ⇒ 判据必须是「取 `selectedMedia`（= 勾选的）」，而不是「取整组」。
    """
    wizard = (ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx").read_text(
        encoding="utf-8"
    )
    i = wizard.index("const mainImagePaths = useMemo(")
    block = wizard[i : i + 500]
    #: 必须复用 `selectedMedia`（= 预检通过 且 勾选），不能直接取整组。
    assert "selectedMedia" in block, f"没有复用勾选结果：\n{block}"
    assert "precheck?.media" not in block, (
        "仍在直接取整组（绕过勾选）—— 未勾选的图会被上传：\n" + block
    )
    #: 而且 `selectedMedia` 本身必须是**与**判据（预检通过 + 勾选），
    #: 不能只看勾选 —— 否则「预检没过但手滑勾上」的图会漏进去。
    j = wizard.index("const selectedMedia = useMemo(")
    sel = wizard[j : j + 400]
    assert "passedMedia" in sel, f"没有先过预检判据：\n{sel}"
    k = wizard.index("const passedMedia = useMemo(")
    passed = wizard[k : k + 400]
    assert "ocr_ok === true" in passed, f"预检判据缺 ocr_ok：\n{passed}"
    #: ⚠️ 用户口径 2026-10-21：违规词**不再**是发布侧的排除条件（决策权交用户）——
    #: 用户勾了违规图就该发出去，这里再按 `!has_forbidden` 过滤会变成
    #: 「勾了但静默丢掉」。保留的只有「未检测不许当合规」与结构性不可发布。
    assert "!m.has_forbidden" not in passed, (
        f"passedMedia 仍在按违规词排除（用户勾了会被静默丢弃）：\n{passed}"
    )
    assert "!m.unsupported" in passed and "!m.oversized" in passed, (
        f"结构性不可发布（格式/超限）没排除 —— 选了也发不出去：\n{passed}"
    )

def test_config_step1_images_can_be_previewed():
    """第二步的商品图片**可点开预览**（用户口径 2026-10-08）。

    判据三件（缺一就是「看起来有、点不开」）：
      1. 缩略图可点（绑了打开预览的处理）；
      2. 预览层存在，且有**左右切换**（←/→）；
      3. 键盘 Esc / ← / → 能操作。
    """
    page = ROOT / "frontend/src/pages/platforms/xhs/publish-config-step1.tsx"
    src = page.read_text(encoding="utf-8")
    #: 1) 缩略图绑了打开预览
    assert "slot-thumb" in src and "setPreviewIndex" in src, "缩略图没有预览入口"
    #: 2) 预览层 + 左右切换
    assert "img-preview-mask" in src, "没有预览层"
    assert "img-preview-nav" in src, "预览没有左右切换"
    #: 3) 键盘
    for key in ("Escape", "ArrowLeft", "ArrowRight"):
        assert key in src, f"预览没有键盘支持：{key}"
    #: 样式要在（否则是个裸 div）
    css = (ROOT / "frontend/src/pages/platforms/xhs/publish-config-step1.css").read_text(
        encoding="utf-8"
    )
    assert ".img-preview-mask" in css, "预览层没有样式"

def test_config_step2_is_visible_without_waiting_for_a_category():
    """第二部分**进第二步就可见**（用户口径 2026-10-08）。

    ⚠️ 曾经的写法是 `{configCategoryPath.length > 0 && <PublishConfigStep2/>}` ——
    用户进第二步只看得到第一部分，会以为第二部分没做。
    判据：渲染处**不带条件**（不能有 `configStep2Open &&` 之类的门）。
    """
    wizard = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    assert "<PublishConfigStep2" in wizard, "第二部分没有被渲染"
    #: 找到渲染点，往上取一小段看有没有条件门
    i = wizard.index("<PublishConfigStep2")
    window = wizard[max(0, i - 160) : i]
    for gate in ("configStep2Open", "configCategoryPath.length > 0 &&", "categoryPath &&"):
        assert gate not in window, f"第二部分被条件挡住：{gate}"


def test_config_step2_fetches_properties_by_category_id():
    """属性定义按**类目 id** 从 `getCategoryProperty` 拉（设计 27）。

    ⚠️ 属性**不在** `getProductPropertyConfig`（真机只回两个开关）——
    走错端点会得到一片空属性，而页面不报错（静默退化）。
    """
    wizard = _read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx")
    assert "fetchCategoryProperties" in wizard, "没有拉类目属性"
    assert "configCategoryId" in wizard, "没有按类目 id 拉属性"
    api = _read(ROOT / "frontend/src/lib/api.ts")
    assert "property-config" in api, "属性端点没接"
    #: 端点后端必须调 getCategoryProperty
    q = _read_qianfan()
    assert '"action": "getCategoryProperty"' in q, "后端没有调 getCategoryProperty"


def test_config_step2_covers_the_four_real_sections():
    """第二部分按真机实测的**四分区**铺开（设计 27）。

    真机（2026-10-08）左锚点是：基础信息(2 项必填) / 图文信息 /
    价格库存(1 项必填) / 服务信息 —— 少一个就是没复刻全。
    并且：属性项必须**按类目动态渲染**（不写死属性名），
    否则换个类目就露出不该有的字段。
    """
    page = ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"
    code = _code_only(_read(page))

    #: ① 四个分区都在
    for title in ("基础信息", "图文信息", "价格库存", "服务信息"):
        assert title in code, f"缺少分区：{title}"

    #: ② 真机字段：导购短标题 / 价格表列
    #: ⚠️ 「上传透明图」已按去重口径**移除**（主图只由第一部分维护），
    #: 所以这里不再断言它。「从商品主图填入」亦已按用户口径（2026-10-10）移除。
    #: ⚠️ 别再把它加回断言 —— 它现在是**刻意删除**的字段。
    for label in ("导购短标题", "运费模板", "商品英文名"):
        assert label in code, f"缺少字段：{label}"
    for col in ("售价", "库存", "市场价", "条形码", "商家编码"):
        assert col in code, f"价格表缺少列：{col}"

    #: ③ 属性按类目动态（**不得写死属性名**）
    assert "keyProperties" in code and "otherProperties" in code, "属性没有走动态定义"
    #: 实测里的属性名不许在页面里硬编码
    for hard in ("是否可定制", "饰面材质", "组合形式"):
        assert hard not in code, f"属性名被写死：{hard}（应按类目下发）"
    #: 规格类型同样必须来自平台（页面不得把 6 项写死成唯一来源）
    assert "specTypeOptions" in code, "规格类型没有走平台下发"

def test_config_step2_does_not_duplicate_step1_fields():
    """与第一部分重复的参数**只展示一次、值一致**（用户口径 2026-10-08）。

    ⚠️ 曾经的写法在第二部分又放了一个「商品标题」输入框和一个「上传主图」入口 ——
    两处各存一份 state，值必然漂移。
    判据：标题/主图在第二部分**必须来自 props**，不得自己 `useState` + `Input`。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: 标题：取 props.title，不能自己有 title 状态
    assert "props.title" in code, "标题没有继承第一部分的值"
    assert "const [title, setTitle] = useState" not in code, "第二部分又存了一份标题"
    assert "onChange={(e) => setTitle(" not in code, "第二部分又放了一个可编辑标题框"
    #: 主图：用 props.images（不重复上传入口）
    assert "props.images" in code, "主图没有继承第一部分"
    assert "上传主图" not in code, "第二部分又出现上传主图入口"


def test_config_step2_generates_from_the_platform():
    """第二部分能自动生成的参数**走平台生成**（用户口径 2026-10-08）。

    ⚠️ 与商品标题同款逻辑：平台有生成接口就用平台的，
    不许本地截断/拼装冒充（那种值平台不认、用户也看不出假）。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "generateShortTitle" in code, "导购短标题没有走平台生成"
    assert "智能生成" in code, "短标题缺「智能生成」入口"
    #: action 串在 **SDK** 里（端点 → 适配器 → SDK 是分层，不在 API 层拼 body）。
    sdk = _read(ROOT / "apis/xhs_qianfan_apis.py")
    assert '"action": "generateItemShortTitle"' in sdk, "SDK 没调 generateItemShortTitle"
    assert "def generate_item_short_title" in sdk, "SDK 没有该方法"
    #: 端点确实接了它
    q = _read_qianfan()
    assert "generate_item_short_title" in q, "端点没调 SDK 的短标题方法"
def test_step2_delivery_mode_switches_the_time_fields():
    """**发货模式联动时间字段**（设计 28 第一节，真机实测）。

    现货模式 → 现货发货时间；其余三种 → 全款预售发货时间（时段/定时预售）。
    这是用户明确要求「真实调研后重写」的点，钉死它不得退化成恒定字段。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: 真机是**四态各自独立分支**（不是现货/其余两态）—— 详见四模式守卫
    assert "deliveryMode === DELIVERY_MODES[0]" in code, "发货模式未按模式分支渲染时间字段"
    assert "现货发货时间" in code and "全款预售发货时间" in code
    assert "时段预售" in code and "定时预售" in code
    assert "该天24点前" in code, "定时预售缺少该天24点前"
    assert "天内发货" in code, "时段预售缺少付款后N天内发货"


def test_step2_shipping_template_is_a_dropdown_without_create():
    """**物流模板 = 下拉选择，且不得有新建**（用户口径 + 设计 28 第二节）。"""
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "shippingTemplates.map" in code, "物流模板未渲染下拉候选"
    assert "新建物流模板" not in code, "物流模板不得有新建入口"
    assert "新建运费模板" not in code, "运费模板不得有新建入口"
    assert "系统默认-非偏远地区包邮模版" not in code, "模板候选不得写死"


def test_step2_spec_types_are_checkboxes_in_one_row():
    """**规格类型用复选框一行展示**（用户口径 + 设计 28 第三节），上限 2。"""
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "<Checkbox" in code, "规格类型必须用复选框"
    assert "specTypes.length >= 2" in code, "规格类型必须有上限 2 的约束"
    #: 规格区不再是「多选下拉」形态（属性区的多选控件不算）
    spec_block = code[code.index("商品规格") : code.index("价格及库存")]
    assert 'mode="multiple"' not in spec_block, "规格类型不得再用多选下拉"
    assert "specTypeOptions ?? []" in code, "规格候选必须来自平台"
def test_step2_mode_switch_asks_for_confirmation():
    """**切换发货模式要二次确认**（真机：「切换发货模式后，需重新设置库存等相关信息，

    是否确认切换？」+ 取消/确认切换）—— 不得静默切走。

    ⚠️ **2026-10-13 取代**（用户口径，见提交 55e0924）：发货模式的二次确认弹窗
    （`pendingDeliveryMode` + 「确认切换」）**已被移除** —— 切换直接生效。
    因此本守卫**反向**断言：`pendingDeliveryMode` / 「确认切换」**不再出现**。
    原正向断言保留在 git 历史（`git show 55e0924 -- .../publish-config-step2.tsx`）。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "pendingDeliveryMode" not in code, "二次确认弹窗已按用户口径移除，不该再出现"
    assert "确认切换" not in code, "「确认切换」按钮已移除"


def test_step2_spec_check_brings_out_its_dynamic_config():
    """**勾选规格类型要带出该规格的动态配置**（真机实测）：

    规格名 + 规格值排序等。

    ⚠️ **2026-10-13 取代**（用户口径，提交 55e0924）：独立按钮「添加规格图」
    「批量上传规格图」**已被移除**（规格图改走输入框前缀 + SKU 选择弹窗）。
    原正向断言保留在 git 历史。本守卫改为断言仍成立的动态配置块 + 排序。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "spec-config" in code, "规格勾选后缺动态配置块"
    assert "规格值排序" in code, "缺「规格值排序」"
    #: 真机：每个规格行都有「规格值排序 + 上移 + 下移」
    assert "spec-sort-actions" in code, "规格值排序应每行都有"
    # 反向：被移除的两个独立按钮不该再出现（防「又加回来」而不自知）
    assert "添加规格图" not in code, "「添加规格图」按钮已按用户口径移除"
    assert "批量上传规格图" not in code, "「批量上传规格图」按钮已按用户口径移除"


def test_step2_layout_matches_the_real_label_column():
    """**UI 布局与千帆一致**：标签列 130px（真机 label x=520 / control x=650）。"""
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    assert "grid-template-columns: 126px 1fr" in css, "标签列宽应为真机的 126px"
    assert "text-align: start" in css, "标签应左对齐（真机 text-align: start）"
    assert "gap: 4px" in css, "行间距应为真机的 4px"
def test_step2_property_area_is_a_three_column_grid():
    """**属性区自适应布局**（用户口径 2026-10-09，覆盖早先「三列栅格」）。

    用户原话：「属性**不固定一行数量**，自适应布局自动换行，单数输入框固定 200px，
    所有下拉框也固定最长 200px」。

    ⚠️ 早先用固定 `repeat(3, 1fr)` 列的写法已被取代 —— 真机那 332px 是千帆自己在
    1176px 内容宽度下的结果；本项目右栏随弹窗（80vw）变化，写死会在窄屏把控件挤出格、
    与**邻格属性名重叠**（用户报的「布局拥挤、输入框和标题重叠」）。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    assert "prop-grid" in tsx, "属性区缺容器"
    assert "prop-cell" in tsx, "属性单元格缺"
    #: 改为 flex 自动换行，**不固定列数**
    assert "flex-wrap: wrap" in css, "属性区应自适应换行"
    assert "repeat(3, minmax(0, 1fr))" not in css, "不得再固定三列（用户要求不固定一行数量）"
    assert "repeat(3, 332px)" not in css, "不得用固定 332px 列宽（会与邻格重叠）"
    #: 属性名来自平台，不得写死任何属性名
    for name in ("材质", "风格", "设计元素"):
        assert name not in tsx, f"属性名不得写死：{name}"
def test_step2_brand_is_fixed_to_no_brand():
    """**品牌固定为「无品牌」**（用户口径）—— 不得有品牌下拉 / 申请入口。"""
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "\u65e0\u54c1\u724c" in code, "品牌应固定展示「无品牌」"
    for banned in ("brandList", "setBrand", "\u7533\u8bf7\u54c1\u724c"):
        assert banned not in code, f"品牌不得再有选择/申请入口：{banned}"


def test_step2_no_create_template_entries():
    """**新建物流模板 / 新建运费模板都不实现**（用户口径）。"""
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "\u65b0\u5efa\u7269\u6d41\u6a21\u677f" not in code
    assert "\u65b0\u5efa\u8fd0\u8d39\u6a21\u677f" not in code


def test_step2_property_smart_generate_uses_the_platform():
    """属性区**智能生成**必须走平台预测（`predictProductProperty`），不本地编。"""
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    api = _read(ROOT / "frontend/src/lib/api.ts")
    sdk = _read(ROOT / "apis/xhs_qianfan_apis.py")
    assert "genProperties" in tsx and "predictProperties" in tsx, "缺属性智能生成接线"
    assert "/xhs/qianfan/product/predict-properties" in api, "缺属性预测请求函数"
    assert "predictProductProperty" in sdk, "SDK 缺 predictProductProperty"


def test_step2_required_counts_match_the_single_item_page():
    """**单品**第二页必填口径（真机实测）：基础信息 4 项 / 图文信息 1 项 / 价格库存 1 项。"""
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "\u57fa\u7840\u4fe1\u606f" in code and "4 \u9879\u5fc5\u586b" in code
    assert "\u56fe\u6587\u4fe1\u606f" in code and "1 \u9879\u5fc5\u586b" in code
def test_step2_section_title_matches_the_real_weight():
    """**分区标题字重与千帆一致**（真机实测 14px / weight 400，无竖条）。"""
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    i = css.index("config-section-title")
    block = css[i : i + 260]
    assert "font-weight: 400" in block, "分区标题字重应为真机的 400"
    assert "border-left" not in block, "分区标题不应有竖条装饰（真机没有）"


def test_step2_four_delivery_modes_each_change_different_params():
    """**四种发货模式各自对应不同的参数变化**（用户口径 + 设计 28 单品实测）。

    ① 现货模式        → 现货发货时间（当日/24h/48h + 推荐）
    ② 全款预售模式    → 全款预售发货时间（时段/定时预售 + 付款后 N 天）
    ③ 现货+预售模式   → 发货时效（SHIP_AGING_OPTIONS + 48小时发货）+ 展开预售批次
    ④ 按规格设置      → 「去设置」+ 价格表新增「发货时间」列
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: 四个模式各自有独立分支（不是「现货 vs 其余」两态）
    for i in range(4):
        assert f"deliveryMode === DELIVERY_MODES[{i}]" in code, f"缺模式 {i} 的独立分支"
    #: ③ 现货+预售 = 现货 + 预售两段（预售含展开 + 3~45 天）
    assert "SHIP_AGING_OPTIONS" in code, "缺预售段的天数候选"
    assert "presaleBatchOpen" in code, "缺「展开」预售批次"
    assert "(deliveryMode === DELIVERY_MODES[0] || deliveryMode === DELIVERY_MODES[2])" in code, \
        "现货发货时间应同时出现在 ①现货 与 ③现货+预售"
    #: ③ 价格表列「发货时效」
    assert "\u53d1\u8d27\u65f6\u6548 *" in code, "现货+预售价格表缺「发货时效」列"
    #: 多件装打包在「更多操作」下拉里（用户口径 2026-10-10：库存工具两按钮并入下拉）
    assert "更多操作" in code and "多件装打包" in code, "多件装打包应并入「更多操作」下拉"
    #: ④ 按规格 = **独立「去设置」+ 无两段发货时间 + 价格表「发货时间」列**
    assert "\u53bb\u8bbe\u7f6e" in code, "按规格模式缺「去设置」"
    assert "\u53d1\u8d27\u65f6\u95f4 *" in code, "按规格模式价格表缺「发货时间」列"


def test_step2_publish_helper_panel_exists():
    """**「商品发布助手」提示页**（用户口径 + 真机实测）。

    真机形态：第二页左栏常驻「商品发布助手」+「发布提示 (N)」计数。
    ⚠️ 用户口径 2026-10-10（**取代**旧「只保留字段+原因两列、无分区列」）：
    分区结构与千帆后台一致 —— 类目信息 / 基础信息 / 图文信息 / 价格库存 / 服务信息，
    每个分区标题行带「N 项必填」徽标；「尺码信息」「商品资质」**不进助手**。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    assert "PublishHelper" in code, "缺「商品发布助手」面板组件"
    assert "\u5546\u54c1\u53d1\u5e03\u52a9\u624b" in code, "缺「商品发布助手」标题"
    assert "\u53d1\u5e03\u63d0\u793a ({" in code, "缺「发布提示 (N)」计数"
    #: 千帆一致的分区清单（新口径）
    assert "HELPER_SECTIONS" in code, "缺千帆分区清单常量"
    for section in ("\u7c7b\u76ee\u4fe1\u606f", "\u57fa\u7840\u4fe1\u606f", "\u56fe\u6587\u4fe1\u606f",
                    "\u4ef7\u683c\u5e93\u5b58", "\u670d\u52a1\u4fe1\u606f"):
        assert f'"{section}"' in code, f"助手分区缺：{section}"
    #: 分区标题行 + 「N 项必填」徽标
    assert "publish-helper-section-head" in code, "缺分区标题行"
    assert "项必填" in code, "缺分区级「N 项必填」徽标文案"
    assert "publish-helper-section-count" in css, "缺徽标样式"
    #: 「尺码信息 / 商品资质」不进助手（新口径）
    assert "去设置尺码表" not in code and "去配置资质" not in code, "尺码/资质入口不应在助手里"
    assert "Tooltip" in code, "超长提示应走 Tooltip"
    assert "publishHints" in code, "发布提示未按实时校验计算"
    assert ".publish-helper" in css, "缺面板样式"
def test_spec_image_thumb_is_a_sibling_not_an_input_prefix():
    """**规格图不能挂在输入框 `prefix` 里**（用户实测 2026-10-11）。

    现象：「商品规格输入框文本过多时，向右走会将规格图挤掉，再回到最左侧时，
    规格图也无法展示」。

    根因：`ant-input-prefix` 是**输入框内部**的一个 span，随文本一起横向滚动
    ⇒ 文本一长就把图顶出可视区，滚回来也未必复原。

    ⇒ 判据两条（缺一条就重现）：
      ① 缩略图渲染在 `.spec-value-item` 里（输入框的**兄弟**），不在 `prefix={...}`；
      ② CSS 里图是 `flex: 0 0 ...`（固定，不被文本挤动）。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    #: ① 不许再把规格图塞进 prefix。
    assert "prefix={prefix}" not in code, (
        "规格图不得再作为 `prefix` 传入 —— prefix 在输入框**内部**，会随文本滚动被挤掉"
    )
    assert "prefix={thumb}" not in code, "同上：规格图不得挂 prefix"
    #: 兄弟节点形态必须在（`.spec-value-item` 包 {thumb}{input}）。
    assert 'className="spec-value-item"' in code, "规格图与输入框应是兄弟节点（.spec-value-item）"
    #: ② CSS：图固定不缩。
    assert ".spec-value-item > .spec-value-thumb" in css, "缺「框外固定缩略图」样式"
    blk = css[css.index(".spec-value-item > .spec-value-thumb"):]
    blk = blk[:blk.index("}")]
    #: ⚠️ 判据要**同时**认「不缩」与「有固定宽度」：只查 `flex: 0 0` 会被
    #: 「删掉 flex 但留着 width」的变异逃掉（实测逃逸一次）。
    assert "flex: 0 0" in blk and "width: 22px" in blk, (
        "缩略图必须既固定尺寸（width）又 `flex: 0 0`（不被挤）—— 否则长文本会把它挤走"
    )
    #: 反向：图中不允许出现会让它被压缩的写法。
    assert "flex: 0 1" not in blk and "flex-shrink: 1" not in blk, (
        "缩略图不许可收缩（否则长文本会把它挤扁/挤走）"
    )


def test_spec_import_uploads_images_concurrently_but_still_cancellable():
    """**规格图导入与详情图共用同一条并发链路** + **取消仍真中断**。

    ⚠️ 2026-10-22 **锚点整体重定向**（用户口径「保持和详情图一致逻辑」）：
    SKU 图上传已由「step2 自己分块并发」改为**一次性交给 `uploadManyFromPackage`**
    （与详情图同一条链路，内部滑窗并发取字节 + 一次 `upload-batch` 服务端并发 PUT）。
    ⇒ 并发实现与取消检查现在**分居两个文件**，别指望在同一个文件里找齐：
      · 并发（worker pool / Promise.all / 共享游标）在 `product-publish-wizard.tsx`；
      · 取消（`importCancelRef`）仍在 `publish-config-step2.tsx`。

    三条判据（取消这一侧是与并发最容易打架的地方）：
      ① 并发实现存在且并发度由常量决定（不是假并发）；
      ② SKU 图与详情图**共用同一个函数**（否则又是两套实现，迟早漂移）；
      ③ 取消标志仍是 `useRef`，且**提交下一段之前**检查。
    """
    step2 = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    wiz_src = _read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx")
    wizard = _code_only(wiz_src)

    #: ① 并发实现（在 wizard 侧）
    assert "UPLOAD_FETCH_CONCURRENCY" in wizard, "取字节必须有并发上限常量"
    assert "Promise.all(" in wizard, "取字节应是 worker pool + Promise.all"
    assert "Math.min(UPLOAD_FETCH_CONCURRENCY, items.length)" in wizard, (
        "并发度必须由 `UPLOAD_FETCH_CONCURRENCY` 决定（写成 1 就是假并发）"
    )
    assert "for (;;)" in wizard, (
        "worker 必须循环领活（不循环 ⇒ 每 worker 只干一项，并发度名存实亡）"
    )

    #: ② 与详情图共用同一条链路（这是本轮用户口径的落点）
    assert "onUploadSkuImagesBatch={uploadManyFromPackage}" in wizard, (
        "SKU 图必须与详情图共用 `uploadManyFromPackage`（否则又是两套实现）"
    )

    #: ③ 取消：ref + 提交前检查
    assert "importCancelRef" in step2, "取消标志必须是 ref"
    assert "useRef(false)" in step2, "取消标志必须用 useRef（不是 useState）"
    assert "if (importCancelRef.current) break;" in step2, (
        "取消检查必须在**提交下一段之前**（否则取消后还会多传）"
    )

def test_concurrent_upload_preserves_order_and_reports_per_image():
    """并发上传必须**保序** + **逐张留结果**（用户口径 2026-10-11）。

    > 「并发上传图片时不要打乱图片的顺序，应保证最后拿到的图片顺序与上传前保持一致」
    > 「已上传 14 张详情图…应上传 38 张，缺少的不知道是哪些张」
    > 「新增一个详情点击弹窗展示对应的详情」

    ⚠️ 三类判据（每一类错了都是用户可见故障）：
      ① **保序**：回填只能经 `slots[r.index]` 映射回**原下标**；
         旧版那条「按文件名找」（`decodable.find(x => x.f.name === …)`）在两张图
         内容相同（MD5 同名）时必然串位 ⇒ 必须不存在。
      ② **不丢图**：取字节要**分块限流**（`UPLOAD_FETCH_CONCURRENCY`）——
         旧版 38 路同时 `fetch` ⇒ 连接池耗尽 ⇒ 失败被 `catch` 吞成 null ⇒ 静默丢 24 张。
      ③ **逐张留结果 + 详情入口**：`UploadOutcome` 保留下标位（失败项也在），
         且 `detail` 要报出「N/M 张 + 有几张失败」并指向可点的详情。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))

    #: ① 保序。
    assert "slots.push(i)" in code and "slots[r.index]" in code, (
        "必须用 slots 数组把「提交下标 → 原下标」映射回来（保序的唯一办法）"
    )
    assert "decodable" not in code, (
        "旧的 `decodable` 压缩法已废弃（它的按文件名兜底会在同内容图上串位）"
    )

    #: ② 限流取字节（不再一次打满连接池）。
    #: ⚠️ 2026-10-22 换锚（用户口径「相同名称的图应只取一次」）：
    #: 由「分块循环（块内并行、块间串行）」改为 **共享游标滑窗**——
    #: 任一张完成立刻补下一张，不让最慢的那张拖住其余槽位；并在此之上按
    #: `rel_path` 去重（同一张图只取一次、只传一次，重复项复用结果）。
    #: 判据不变：**必须有并发上限**（无上限 ⇒ 连接池耗尽 ⇒ 失败被 catch 吞成 null 丢图）。
    assert "UPLOAD_FETCH_CONCURRENCY" in code, "取字节必须有并发上限（否则连接池耗尽丢图）"
    assert "let cursor = 0;" in code and "for (;;)" in code, (
        "取字节应为共享游标滑窗（worker 循环领活），不是一次性 Promise.all 全打出去"
    )
    assert "firstIndexByPath" in code, (
        "同名图必须只取一次（按 rel_path 去重，重复项复用已取到的结果）"
    )

    #: ③ 逐张结果 + 详情入口。
    assert "type UploadOutcome = {" in code, "每张图要有逐张结果类型"
    assert "out[index].reason = `取图失败：" in code, (
        "取图失败必须**逐张记名**（不能静默丢）—— 用户要知道缺的是哪些张"
    )
    assert "已上传 ${details.length}/${detailTotal} 张详情图" in code, (
        "详情图进度必须报「N/总数」（旧版只报 N，用户看不出少了）"
    )
    assert "setUploadDetails" in code and "setUploadDetailOpen" in code, "缺逐张详情的状态"
    assert "uploadDetailKeyFor" in code, "阶段标题应能定位到对应详情（可点入口）"
    #: ⚠️ 只查「函数存在」太弱（实测逃逸：把**调用点**去掉仍绿）。
    #: 判据必须落在**调用形态**上：标题真的按它分叉成可点 span。
    assert "const which = uploadDetailKeyFor(s.title);" in code, (
        "阶段行里没有真的调用 uploadDetailKeyFor（详情入口形同虚设）"
    )
    assert "auto-stage-title-link" in code, "可点标题的样式类没接上"
    #: ⚠️ 用户口径（2026-10-11）追加两条：
    #:  · 「已上传 14/38 张详情图（24 张失败…）**应将标题置红色**」
    #:  · 「**在底部添加一个提示，点击标题可查看上传详情信息**」
    #: ① 变红判据必须查**数据里有失败项**（不是查文案里有没有「失败」二字 ——
    #:    文案一改就失效；只有查数据才是真判据）。
    assert "uploadDetails[which].some((r) => !r.ok)" in code, (
        "标题变红必须按「该份详情里真有失败项」判定（不许按文案字符串判）"
    )
    assert "auto-stage-title-failed" in code, "缺「标题置红」的类"
    #: ⚠️ 用户口径追加（2026-10-11）：「出现上传失败则应修改为**红色标题和红色对号图标**」
    #: ⇒ 图标也必须跟着变红。**绿勾 + 红字自相矛盾**，而用户第一眼看的是图标。
    #: 判据：`done` 分支里图标颜色由「该份详情有没有失败项」决定，不能恒绿。
    icon_seg = code[code.index('className="auto-stage-icon"'):]
    icon_seg = icon_seg[:800]
    #: ⚠️ 判据要「红叉在场 + 绿勾**有条件**」，不是「出现了 CloseCircleFilled」——
    #: 只查出现会被「恒绿勾 + 别处有个红叉」骗过（实测逃逸一次）。
    #: 取 `done` 那个分支，断言它**同时**含绿勾与红叉（= 真的按条件分叉）。
    assert "s.status === \"done\"" in icon_seg, "缺少 done 分支"
    done_at = icon_seg.index('s.status === "done"')
    failed_at = icon_seg.find('s.status === "failed"', done_at)
    done_seg = icon_seg[done_at: failed_at if failed_at > 0 else len(icon_seg)]
    assert "CheckCircleFilled" in done_seg and "CloseCircleFilled" in done_seg, (
        "done 分支里必须**绿勾与红叉都有**（按失败与否分叉）；只有绿勾 = 恒绿（用户看到的就是这个）"
    )
    #: 且分叉条件必须查数据（与标题同一判据）。
    assert "uploadDetails[uploadDetailKeyFor(s.title)!" in done_seg, (
        "图标分叉必须按「该份详情真有失败项」判定（与标题同一判据）"
    )
    #: ② 底部提示。
    assert "auto-stage-footer-hint" in code, "缺底部提示（用户要求：点击标题可查看上传详情）"
    assert "点击「上传商品主图」/「上传商品详情图」标题可查看逐张上传详情" in code, (
        "底部提示文案不在"
    )
    css_all = _read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.css")
    assert ".auto-stage-title-link.auto-stage-title-failed" in css_all, (
        "缺标题置红的样式（光有类名不生效）"
    )
    #: ⚠️ 这类元素的样式**只有一条规则**（一个选择器出现一次）—— 写 `>= 2` 是错的
    #: （实测：干净代码上就假红）。判据应是「选择器存在」+「规则体里有可见样式」。
    assert ".publish-wizard .auto-stage-footer-hint" in css_all, "缺底部提示样式选择器"
    hint_css = css_all[css_all.index(".publish-wizard .auto-stage-footer-hint") :]
    hint_css = hint_css[: hint_css.index("}")]
    assert "font-size" in hint_css and "color" in hint_css, "底部提示样式没实际生效的声明"
    assert "<Modal" in code[code.index("uploadDetailOpen !== null") - 200 :], "详情应是点击弹窗"


def test_upload_detail_modal_shows_failed_rows_with_reasons():
    """详情弹窗要**列出失败项与原因**（不是只报个数字）。"""
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    seg = code[code.index("uploadDetailOpen !== null") :]
    seg = seg[: seg.index("</Modal>")]
    assert "成功" in seg and "失败" in seg, "详情要能区分成功/失败"
    assert "r.reason" in seg, "失败行必须显示原因（用户要知道为什么缺）"
    assert "Tag color=\"red\"" in seg, "失败行应有醒目标记"

    css = _read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.css")
    assert ".auto-stage-title-link" in css, "可点标题要有样式（否则用户不知道能点）"
    assert ".upload-detail-reason" in css, "失败原因要有样式"


def test_auto_load_images_upload_concurrently():
    """自动加载参数的主图/详情图**并发上传**（用户口径 2026-10-11 方案 B）。"""
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    assert "uploadManyFromPackage" in code, "缺并发批量上传的封装"
    assert "uploadPictureSpaceFiles" in code, "应调用批量上传端点（方案 B）"
    #: 两个原来的串行循环都要换掉。
    #: ⚠️ 2026-10-11：出口从「直接回 ConfigMainImage[]」改成「回逐张结果 UploadOutcome[]」
    #: （用户要能看「缺的是哪些张」）⇒ 判据改成「两处都调了 uploadManyFromPackage」+
    #: 「返回值经 .filter(o => o.ok) 取成功项」。
    assert code.count("await uploadManyFromPackage(") >= 2, (
        "主图与详情图都应走并发批量上传（有串行循环残留）"
    )
    assert "const mainOutcomes = await uploadManyFromPackage(" in code, "主图未走并发"
    assert "const detailOutcomes = await uploadManyFromPackage(" in code, "详情图未走并发"
    #: 反向：不许再有「逐张 await uploadOneFromPackage」的循环残留。
    assert "await withUploadRetry(() => uploadOneFromPackage" not in code, (
        "仍有串行逐张上传残留（应全部走并发版）"
    )


def test_batch_edit_dropdown_only_lists_numeric_columns_and_rounds_to_two_decimals():
    """批量修改列值：**下拉只列数值列** + **结果两位小数四舍五入**（用户口径 2026-10-11）。

    > 「批量修改列，该列必须为数值类似才可以支持在下拉中展示，
    >   列值最终计算保留两位小数四舍五入」

    ⚠️ 两条一起钉：
      ① 下拉里**不许**出现文本列（条形码 / 商家编码）—— 加减一个数、乘一个倍数
         对文本没意义（它们本就走 `mode === "fixed"` 的整列赋值分支）。
      ② 计算结果必须**收敛到两位小数**，否则浮点毛刺（`18.8 × 1.7 =
         31.960000000000004`）会原样写进价格表提交给平台。
    """
    step2 = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    lib = _code_only(_read(ROOT / "frontend/src/lib/precheck-publish.ts"))

    #: ① 下拉只列数值列。
    #: ⚠️ **必须扫全部 `const fields … = [` 定义**（实测逃逸一次：只切第一个会漏掉
    #: 另一个批量修改弹窗 —— 本文件里有**两个**「批量修改列值」入口，
    #: 口径必须一致，只改一个等于没改）。
    fields_segs: list[str] = []
    idx = 0
    while True:
        found = step2.find("const fields: { key:", idx)
        if found < 0:
            break
        end = step2.index("];", found)
        fields_segs.append(step2[found:end])
        idx = end
    assert fields_segs, "没找到任何批量修改下拉定义（守卫会空转）"
    for _seg in fields_segs:
        for label in ("售价", "库存", "市场价"):
            assert f'label: "{label}"' in _seg, f"数值列「{label}」应在下拉里"
        for label in ("条形码", "商家编码"):
            assert f'label: "{label}"' not in _seg, (
                f"文本列「{label}」不该出现在批量修改下拉里（只支持数值列）"
            )

    #: ② 两位小数四舍五入（判据落在**计算函数**上，不是页面）。
    assert "Math.round(clamped * 100) / 100" in lib, (
        "批量修改的结果必须 `Math.round(x*100)/100` 收敛到两位小数（否则浮点毛刺进价格表）"
    )
    #: 反向：不许裸返回未收敛值。
    assert "return Math.min(max, Math.max(min, next));" not in lib, (
        "仍有未收敛两位小数的返回路径（会被浮点毛刺污染）"
    )


def test_publish_buttons_are_split_with_tip_confirm_and_the_right_colors():
    """第三步提交按钮**拆成两个**（用户口径 2026-10-11）。

    > 「将提交按钮逻辑进行拆分，保留当前的保存草稿，确认发布拆分为，发布商品
    >   （直接上架不再下架），发布并下架商品（发布成功后立刻下架）」
    > 「红线取消，tip二次确认」
    > 「发布商品按钮红色，保存草稿和发布并下架商品为蓝色」

    ⚠️ 三类判据，缺任一条都会跑偏：
      ① **两个按钮 + 语义**：`submit(false)` = 发布商品（不下架）；
         `submit(true)` = 发布并下架商品。顺序/实参写反 = 红线行为与按钮标签对不上。
      ② **tip 二次确认只挂在「发布商品」上**：它是唯一留下在售商品的动作。
         ⚠️ 反向也要钉：旧文案「确认发布」必须消失（否则用户以为还是原来那个按钮）。
      ③ **颜色**：「发布商品」= `danger`（红）；保存草稿 / 发布并下架 = `type="primary"`（蓝）。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))

    #: ① 两个按钮 + 实参语义。
    assert "发布商品" in code and "发布并下架商品" in code, "缺拆分后的两个按钮"
    assert "submit(false)" in code, "「发布商品」应传 submit(false)（发布后不下架）"
    assert "submit(true)" in code, "「发布并下架商品」应传 submit(true)（发布后立刻下架）"
    #: 旧按钮必须退场。
    assert "确认发布</Button>" not in code and ">\n                确认发布" not in code, (
        "旧的「确认发布」按钮应已拆分退场"
    )

    #: ② tip 二次确认挂在「发布商品」上。
    assert "Popconfirm" in code, "「发布商品」需要 tip 二次确认（Popconfirm）"
    pc_at = code.index("<Popconfirm")
    pc_body = code[pc_at : pc_at + 700]
    assert "submit(false)" in pc_body, "Popconfirm 的确认动作应是「发布商品」(submit(false))"
    #: ⚠️ 文案口径**已被修正**（2026-10-11 真机抓包 + 实测）：
    #:  原句要求「不会自动下架」—— 但真实行为是「商品进平台**审核**，审核通过才上架」，
    #:  真机平台自己提交后同样不可售、且提交后**不再调上架接口**。
    #:  ⇒ 判据改为「必须如实说明进审核」+「不许出现与事实不符的『保持在售/不会自动下架』」
    #:    （后者会让人以为点完就已经在售了）。
    assert "审核" in pc_body, "确认文案必须如实说明「商品进平台审核」"
    assert "不会自动下架" not in pc_body, (
        "不许再用「不会自动下架」（与事实不符：商品是进审核，不是立刻在售）"
    )
    #: ⚠️ 成功后提示同样必须如实（原句「发布成功，商品已在售（未下架）」会让人
    #: 以为已上架成功，而真相是「进审核」）。
    assert "已提交平台审核" in code, "成功提示必须如实说明「已提交平台审核」"
    assert "商品已在售" not in code, (
        "不许再出现「商品已在售」（与事实不符 —— 商品进审核，审核通过才上架）"
    )
    #: 反向：「发布并下架」不该被包进 Popconfirm（老路径不加门）。
    #: ⚠️ 只查**开标签** `<Popconfirm` —— 查 `Popconfirm` 会命中闭合标签
    #: `</Popconfirm>`（实测踩过：断言恒红）。
    between = code[code.index("</Popconfirm>") : code.index("发布并下架商品")]
    assert "<Popconfirm" not in between, "「发布并下架商品」不应被包在 Popconfirm 里"

    #: ③ 颜色：发布商品 = danger（红）；另两个 = primary（蓝）。
    danger_at = code.index("<Button danger")
    danger_body = code[danger_at : danger_at + 300]
    assert "发布商品" in danger_body, "「发布商品」应为 danger（红色）"
    #: 保存草稿与发布并下架都是 primary。
    assert code.count('type="primary"') >= 2, "保存草稿与发布并下架都应为主色（蓝）"


def test_publish_mask_copy_follows_the_clicked_button():
    """**提交遮罩的文案必须跟着用户点的按钮走**（用户实测 2026-10-11）。

    > 「点击发布商品按钮弹窗中的文案未改变」

    现象：点「发布商品」（不下架），遮罩却写「正在发布并**立即下架**…」
    「发布成功后会立即下架（**红线**）」—— 文案是**反的**，而且「红线」早已取消。

    ⇒ 判据：遮罩文案不许写死，必须由「本次点的哪个按钮」这个 state 分叉；
    且旧写死串必须消失（留着就说明只改了按钮没改文案）。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))

    #: ① 有记录本次模式的 state（没有它就无从分叉）。
    assert "submitShelfMode" in code, "缺「本次点的是哪个发布按钮」的状态"
    assert "setSubmitShelfMode(shelfAfterPublish)" in code, (
        "submit 应记下本次模式（否则遮罩文案无从分叉）"
    )

    #: ② 遮罩文案分叉：两种模式都要有各自的正文。
    assert "正在发布并下架商品" in code, "缺「发布并下架」模式的遮罩文案"
    assert "正在发布商品（保持在售）" in code, (
        "缺「发布商品」模式的遮罩文案（点不下架时不能复用下架文案）"
    )
    assert "发布成功后商品将保持在售、不会自动下架" in code, (
        "「发布商品」模式的副文案要明说不会自动下架"
    )

    #: ③ 旧的写死串必须退场（这是用户看到的那句）。
    assert "正在发布并立即下架…" not in code, "旧的写死遮罩文案仍在（用户实测就是它）"
    #: ⚠️ 「红线」二字不许再出现在**面向用户**的文案里（已取消，说了是错的）。
    #: 注释里保留历史说明是允许的 ⇒ 只看字符串字面量。
    for cue in ("（红线）", "红线；"):
        assert cue not in code, f"用户可见文案里不该再有「{cue}」（红线已取消）"


def test_submit_sends_explicit_off_shelf_after_matching_the_clicked_button():
    """`submit` **显式传** `off_shelf_after`（红线取消后必须显式表达）。

    ⚠️ 旧版是「刻意不传、靠后端默认 true 兜底」；现在两个按钮语义相反，
    不显式传就永远分不出「发布商品」和「发布并下架」。
    ⚠️ 同时钉**后端默认值仍是 true**（保守）—— 拆按钮不该把「默认安全」也拆掉。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    assert "off_shelf_after: shelfAfterPublish" in code, (
        "submit 必须把参数透传成 off_shelf_after（否则两个按钮行为一样）"
    )
    assert "shelfAfterPublish: boolean" in code, "submit 的入参应声明语义化参数名"

    import inspect

    import backend.app.api.platforms.xhs.qianfan as qianfan

    field = qianfan.ItemPublishRequest.model_fields["off_shelf_after"]
    assert field.default is True, (
        "后端 off_shelf_after 默认必须仍是 True（漏传的调用方拿到保守行为）"
    )
    #: 红线取消后**生效的**禁令必须消失。
    #: ⚠️ 不能简单查「不得」二字 —— 我在 docstring 里**刻意保留**了历史原句
    #: （「原口径：…前端**不得**传 `False`」+ 标注已被取代），那是口径变更的留痕，
    #: 不是生效规则。判据 = 「不得」只能出现在「已被取代 / 原口径」这类**标记过的
    #: 历史段落**里；独立出现的禁令句才算违规。
    doc = inspect.getsource(qianfan.ItemPublishRequest)
    for line in doc.splitlines():
        if "不得" in line:
            assert any(tag in line or tag in doc[: doc.index(line)] for tag in ("已被取代", "原口径", "原句")), (
                f"仍有未标注为历史的禁令句：{line.strip()}"
            )
    #: 直接钉「新口径」必须在场（否则等于只删了旧话、没写新话）。
    assert "红线取消" in doc or "取消红线" in doc, "类 docstring 应写明红线已取消"


def test_submit_callback_deps_include_the_image_arrays():
    """**提交闭包必须依赖图片数组**（用户实测 2026-10-11 的真根因）。

    现象：第三步提交报「图片异常」→ 回第二步删图 → 再提交**仍报同一个错**；
    而第三步**预览是正常的**（图确实删了）。
    根因：`submit` 是 `useCallback`，依赖数组**漏了** `configImages` /
    `configDetailImages` / `specImages` ⇒ 第二步删图只改 state，`submit` 不重建
    ⇒ 提交时用**删图前**的闭包快照组装载荷；而预览直接渲染 prop ⇒ 永久最新。

    ⚠️ 这类「漏依赖」不报错、不告警，真机表现只是「改了没用」—— 必须有守卫。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    #: 定位 `submit` 的 useCallback 依赖数组（从 `const submit = useCallback` 起，
    #: 取到该调用结束的 `]);`）。
    start = code.index("const submit = useCallback")
    tail = code[start:]
    #: ⚠️ 依赖数组的开头是 `}, [`（useCallback 的**第二个参数**）；别写成 `], [`
    #: —— `_code_only` 剥注释后形态固定，但写错字面量会直接 ValueError（实测踩过）。
    deps_at = tail.index("}, [")
    #: ⚠️⚠️ **必须从 `}, [` 之后开始**（实测教训：从 `start` 起切会让函数体里的
    #: `configDetailImages` 命中，删掉依赖也能通过 —— 一条**假守卫**，变异验证当场抓出）。
    #: 只认 `[` 到配对 `]` 之间的内容 = 真正的依赖列表。
    deps_open = deps_at + len("}, ")
    deps = tail[deps_open : tail.index("]", deps_open)]
    #: 自检：依赖列表里不该出现函数体的标志物（如 `publishShopItem(`），
    #: 否则说明切片又切宽了（防「假守卫」回归）。
    assert "publishShopItem(" not in deps, (
        "依赖切片切进了函数体 ⇒ 守卫会形同虚设（必须只切 `[` 到 `]` 之间）"
    )
    #: ⚠️ **必须按整词匹配**：`configImages` 是 `configDetailImages` 的**子串**，
    #: 用 `name in deps` 会让「删掉 configDetailImages」也能通过 —— 第二条假守卫
    #: （变异验证当场抓出）。按逗号/空白切词后比对，才真正逐项判定。
    names = {t.strip().rstrip(",") for t in re.split(r"[\s,]+", deps) if t.strip()}
    for name in ("configImages", "configDetailImages", "specImages"):
        assert name in names, (
            f"`submit` 的依赖数组漏了 `{name}` ⇒ 第二步改它后提交仍用旧闭包快照"
            "（真机表现：删了图还报同一个图片异常）"
        )
    #: 并且要有「材料一变就清旧错误」的 effect（否则第三步会挂着上一轮失败原因）。
    assert "setResult((prev) => (prev && !prev.published ? null : prev))" in code, (
        "缺「提交材料变化 ⇒ 清掉上一轮发布错误」的 effect"
    )


def test_step2_spec_values_input_exists():
    """**勾选规格后要有规格值输入框**（真机：「请选择或输入规格值」，w=186）。"""
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    assert "\u8bf7\u9009\u62e9\u6216\u8f93\u5165\u89c4\u683c\u503c" in code, "缺规格值输入框"
    #: 用户口径：**一个输入框一个规格值**（不是 tags 多值框）
    assert 'mode="tags"' not in code, "规格值不得再用多值 tags 框"
    assert "specValueInputs" in code, "规格值应是一框一值的列表"
    assert "commitSpecValue" in code, "缺规格值回车提交（并追加新空框）"
    assert "SPEC_VALUE_LEN_MAX = 60" in code, "规格值长度上限应为真机的 60"
    assert "specValues" in code, "规格值未按规格类型分别存储"
    assert "spec-config-values" in css, "缺规格值输入框样式"
    #: 用户口径（2026-10-09，后修正）：规格值输入框**固定 200px、横向排列**（多个框
    #: 同一行、超宽自动换行）—— 不要每个单独占一行，否则规格一多整块拉得很高。
    #: ⚠️ 别用「起点 + 固定字数」切片：注释一改就切不到目标规则（实测踩过 ——
    #: 宽度规则被新加的说明挤出 520 字窗口 ⇒ 断言找不到）。切到**两条规则都覆盖**为止。
    _from = css.index(".spec-config-values")
    block = css[_from:css.index(".spec-value-item", _from)]
    assert "flex-direction: row" in block, "规格值输入框应横向排列（不单独占一行）"
    assert "flex-wrap: wrap" in block, "规格值区应自动换行"
    #: ⚠️ 宽度口径**已被取代**（2026-10-11 用户实测「规格文本超出输入框外展示」）：
    #: 旧的「固定 200px」正是根因（`flex:0 0` ⇒ 不长不缩，30+ 字必然被裁）。
    #: 现行 = **按内容自适应**（`width:auto` + `min-width` 保底 + `field-sizing: content`）。
    #: ⚠️ **口径已回到固定 200px**（用户 2026-10-11 定稿：「规格输入框还是需要固定
    #: 200px 长度，长文本也不应该溢出展示在输入框外」）—— 宽度固定、由**输入框自身**
    #: 的 `overflow:hidden` + `title` 保证不溢出，不再靠自适应宽度。
    assert "width: 200px" in block, "规格值输入框宽度应为固定 200px"
    #: ⚠️ 原两条断言「宽度固定 200px / `SPEC_VALUE_INPUT_WIDTH = 200`」**已随口径变更删除**
    #: （2026-10-11：用户实测「规格文本超出输入框外展示」⇒ 固定宽度就是根因）。
    #: 现在只钉**自适应**，宽度写死在别处也会被上面那条 not in 拦下。
    #: （原「宽度常量」断言已删：常量已移除，宽度由 CSS 单点控制。）
    #: 规格值上限 500（真机文案：单个规格值的上限数量为 500 个）
    assert "SPEC_VALUE_MAX = 500" in code, "规格值上限应为真机的 500"
    #: 真机：行内右侧「规格值排序」；**每个规格行都有**上移/下移
    assert "spec-sort-actions" in css, "规格值排序应在行右侧"
    assert "moveSpecType" in code, "缺规格上移/下移实现"
    assert "disabled={gi === 0}" in code, "上移应在第 1 行禁用（真机每行都有；按**组**下标）"
    #: ⚠️ **2026-10-13 取代**（用户口径，提交 55e0924）：独立按钮「添加规格图」「批量上传
    #: 规格图」已移除（规格图改走输入框前缀 + SKU 选择弹窗）⇒ 原正向断言改反向。
    assert "批量上传规格图" not in code, "「批量上传规格图」按钮已按用户口径移除"


def test_step2_spec_value_input_matches_dimension_metadata():
    """**规格值输入按维度元数据区分形态**（用户口径 2026-10-10）。

    真机 `saleProperties[]` 每个维度自带 `propertyInputType`（1=单选/2=多选/0=文本）
    与 `customizable`（是否可自定义输入）。本项目把输入入口按此区分：
    - `kind=select` + customizable → AutoComplete（可输入可下拉选）
    - `kind=select` + 非 customizable → Select（只能选）
    - text / multi / 未下发 → 纯 Input（回落，不造形态）
    ⚠️ 规格图逻辑**不变**（一值一行 + 前缀仍只挂第一组）。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: ① 后端 _normalize 补 customizable（qianfan.py）
    py = _read_qianfan()
    assert '"customizable": bool(item.get("customizable")),' in py, "后端应搬 customizable 字段"
    #: ② 前端类型补 customizable（types/index.ts）
    types = _read(ROOT / "frontend/src/types/index.ts")
    assert "customizable?: boolean" in types, "CategoryPropertyItem 应带 customizable"
    #: ③ SpecAttribute 类型带 kind/customizable
    assert "export type SpecAttribute" in code, "缺 SpecAttribute 类型"
    assert 'kind: "select" | "multi" | "text"' in code, "SpecAttribute 缺 kind"
    assert "customizable: boolean" in code, "SpecAttribute 缺 customizable"
    #: ④ 维度元数据 helper + 按 kind 分支渲染
    assert "specMetaFor" in code, "缺按规格名取维度元数据的 helper"
    assert 'meta?.kind === "select"' in code, "缺按 kind 分支渲染"
    assert "<AutoComplete" in code, "单选可自定义应渲染 AutoComplete"
    assert "spec-value-select" in code, "单选不可自定义应渲染 Select"
    assert "AutoComplete" in code and "customizable" in code, "AutoComplete/customizable 分支应在"


def test_step2_spec_value_image_prefix_only_first_group():
    """**规格图前缀由「规格图开关」决定**（用户口径 2026-10-10 纠正）。

    那个单选开关是**规格图开关**，不是规格启用/停用 —— 规格添加即为启用，
    没有启用停用逻辑。开关开 ⇒ 该组可配置规格图（前缀 + 添加/批量上传）；
    默认第一组开、其余组关（回落 `gi === 0`）。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "specImageEnabled" in code, "缺规格图开关 state"
    assert "const canImage = specImageEnabled[name] ?? gi === 0;" in code,         "前缀可用性应由规格图开关决定（默认回落第一组）"

    #: 开关本体（Switch）+ 显隐联动
    assert "<Switch" in code, "缺规格图开关 Switch"
    assert 'checked={specImageEnabled[name] ?? gi === 0}' in code, "开关默认：第一组开、其余关"
    #: ⚠️ **2026-10-13 取代**（用户口径，提交 55e0924）：「添加规格图」「批量上传规格图」
    #: 两个独立按钮已移除 ⇒ 开关只控制**规格图前缀**可用性（`canImage`），不再控制按钮显隐。
    assert "添加规格图" not in code and "批量上传规格图" not in code, (
        "两个独立按钮已按用户口径移除（规格图走输入框前缀）"
    )


def test_step2_is_a_two_column_layout():
    """**发布提示与参数配置是左右布局**（用户口径 + 真机实测）。

    真机：good-main-detail 是 dir=row 两栏 —— 左 224px（发布提示），右 1176px（参数配置）。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    assert "publish-two-col" in tsx, "缺左右两栏容器"
    assert "publish-col-left" in tsx and "publish-col-right" in tsx, "缺左/右栏"
    #: 左栏里放发布提示，右栏里放参数配置（顺序不能反）
    i_left = tsx.index("publish-col-left")
    i_helper = tsx.index("<PublishHelper", i_left)
    i_right = tsx.index("publish-col-right", i_left)
    assert i_left < i_helper < i_right, "发布提示应在左栏、参数配置在右栏"
    assert "display: flex" in css, "两栏容器应为 flex"
    assert ".publish-col-left" in css and "flex: 0 0 200px" in css, "左栏宽度应固定（真机 224px 含内边距）"
    #: 字段行 label 126px（真机 label-wrapper x=512 w=126）
    assert "grid-template-columns: 126px 1fr" in css, "字段标签列应为真机的 126px"


def test_step2_property_area_splits_key_and_other():
    """**商品属性分「关键属性 / 其他属性」两部分**（用户口径 2026-10-09）。

    ⚠️ 本条**取代**了此前的「关键属性 / 其他属性合并为一个「商品属性」标题」口径
    （用户 2026-10-09 追加：「商品属性应分为关键属性和其它属性两部分，
    且智能生成和填充」）。真机第二部分原文就是两段：
    `关键属性 3/4`（+说明）/ `其他属性 11/19`（+说明）。

    判据：① 两个分组小标题都在，且各带 `已填/总数` 计数；
          ② 标题**始终展示**（不设类目门控，未选类目也展示）；
          ③ 无值时给一行说明（不造假）。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: ① 两个分组小标题 + 计数（真机形态）
    assert "关键属性 {filledCount(props.keyProperties, propValues)}/{props.keyProperties.length}" in code, \
        "缺「关键属性 N/M」分组标题"
    assert "其他属性 {filledCount(props.otherProperties, propValues)}/{props.otherProperties.length}" in code, \
        "缺「其他属性 N/M」分组标题"
    #: ② 标题不得被类目门控包起来（未选类目也要展示）
    assert "props.categoryId && (" not in code[code.index("prop-grid") - 1400:], \
        "商品属性标题不该被类目条件挡住"
    #: ③ 无值时的说明行（不造假；类目未选时说明原因）
    assert "暂无关键属性" in code, "缺关键属性空态说明"
    assert "暂无其他属性" in code, "缺其他属性空态说明"
    assert "请先在上一步选择商品类目" in code, "未选类目时缺原因行"
    #: ④ 佐证「关键属性 = 必填属性」这一真机判据（后端 importantProp 恒 false）
    q = _read_qianfan()
    #: ⚠️ 判据必须落在 **`key` 那一行**（`required` 行也有同样的表达式，
    #: 宽松判据会被它满足 ⇒ 哑弹。实测：只写 `requiredProp` 时变异抓不住）。
    assert '"key": bool(item.get("requiredProp"))' in q, \
        "关键属性的 key 判据应取 requiredProp（真机实测 importantProp 恒 false）"


def test_step2_explanatory_text_uses_icon_tooltip():
    """**说明性文案一律收进「说明图标 + Tooltip」**（用户口径 2026-10-09）。

    ⇒ `Field` 的 `hint` 不再以正文行铺在控件下方（那是占布局的老做法）；
    图片区的两行长说明也改成图标 + Tooltip。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: hint 走 Tooltip，不再渲染 `config-field-hint` 正文块
    assert "config-field-hint" not in tsx, "说明仍在正文行（应收进 Tooltip）"
    assert "section-help-icon" in tsx, "缺说明图标"
    step1 = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step1.tsx")
    assert "image-hint-line" in step1, "图片区说明行未改成图标 + 说明"
    assert "hint-icon" in step1, "图片区缺说明图标"
    #: 老的两行正文说明（className="image-hint"）应只保留在 CSS，不再在页面渲染
    assert '<div className="image-hint">' not in step1, "图片区仍以正文行铺长说明"


def test_step2_media_section_order_is_main_video_detail():
    """**图文信息区按 商品主图 → 商品视频 → 商品详情图 顺序展示**（用户口径 2026-10-09）。"""
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    i_main = code.index('label="商品主图"')
    i_video = code.index('label="商品视频"')
    i_detail = code.index('label="商品详情图"')
    assert i_main < i_video < i_detail, "图文信息三块顺序应为 主图 → 视频 → 详情图"
    #: 删除图标不得常驻（悬停才显形）
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    assert ".media-thumb-wrap:hover .media-thumb-remove" in css, "详情图删除图标应悬停才显形"


def test_step1_image_area_badge_removed_and_renamed():
    """**智能识图**区：图片角标移除 + 区块更名（用户口径 2026-10-09）。

    ⇒ ① 缩略图左上角不再有「主图N」角标；
       ② 区块名从「商品图片」改为「智能识图」（喂大模型识别用，可传主图/详情图）。
    """
    tsx = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step1.tsx")
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step1.css")
    assert "智能识图" in tsx, "区块未更名为「智能识图」"
    assert "slot-badge" not in tsx, "角标仍在渲染（应移除）"
    #: CSS 里的 `.slot-badge` **规则**必须已删（注释里提到它名不算 —— 只认选择器声明）。
    assert "\n.publish-config-step1 .slot-badge {" not in css, "角标样式未删干净"


def test_auto_load_shows_a_centered_stage_progress_modal():
    """**自动加载参数 → 窗口居中弹窗 + 逐阶段执行进度列表**（用户口径 2026-10-09）。

    判据：① 有一个 `centered` 的进度弹窗（不是只用 message tip）；
          ② 列出**每一阶段**的执行情况（状态含 进行中/成功/失败/跳过）；
          ③ 阶段清单里含**按类目加载商品参数**这一步（真机按 categoryId 批量拉参数）。
    """
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    assert "autoProgressOpen" in wiz, "缺进度弹窗可见性状态"
    assert "auto-stage-list" in wiz, "缺阶段列表容器"
    #: 弹窗居中
    i = wiz.index("自动加载参数 · 执行进度")
    assert "centered" in wiz[i: i + 600], "进度弹窗未居中"
    #: 四态齐全（逐阶段执行情况）
    assert 'status === "running"' in wiz, "缺「进行中」态"
    assert 'status === "done"' in wiz, "缺「成功」态"
    assert 'status === "failed"' in wiz, "缺「失败」态"
    assert 'status === "skipped"' in wiz, "缺「跳过」态"
    #: 阶段清单含「按类目加载商品参数」
    assert "按类目加载商品参数" in wiz, "缺「按类目加载商品参数」阶段"
    #: 该阶段确实调了取类目属性的端点
    assert "fetchCategoryProperties" in wiz, "加载阶段未调 fetchCategoryProperties"
    #: CSS 有对应样式
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.css")
    assert ".auto-stage-row" in css, "缺阶段行样式"


def test_short_title_and_property_calls_pass_platform_required_params():
    """**短标题 / 属性预测的入参按真机抓包**（2026-10-09 实测修复）。

    真机入参：
    * `generateItemShortTitle` = `{title, categoryId}`（**没有** mainImages）；
    * `predictProductProperty` = `{title, categoryId, imageUrls[]}`（**必须带图**，
      否则平台恒回空数组 ⇒ 页面误报「平台未给出可填充的属性值」）。

    判据：SDK + 前端请求函数都按此形状传参，且属性预测带上了图片。
    """
    sdk = _read(ROOT / "apis/xhs_qianfan_apis.py")
    #: 短标题入参是 title + categoryId
    i = sdk.index("def generate_item_short_title")
    block = sdk[i: i + 2600]
    assert 'param: dict = {"title"' in block, "短标题入参应为 title（不是 name）"
    assert 'param["categoryId"]' in block, "短标题入参应带 categoryId"
    #: 属性预测入参带 imageUrls
    j = sdk.index("def predict_product_property")
    pblock = sdk[j: j + 2600]
    assert 'param["imageUrls"]' in pblock, "属性预测应传 imageUrls（否则平台恒回空）"
    #: 前端请求函数
    api = _read(ROOT / "frontend/src/lib/api.ts")
    assert "image_urls" in api, "前端属性预测请求函数缺 image_urls"
    assert "category_id: categoryId" in api, "前端短标题请求函数应传 category_id"
    #: 解析路径（真机字段落点）
    q = _read_qianfan()
    assert "generatedShortTitle" in q, "短标题解析应用 generatedShortTitle"
    assert 'it.get("propertyValueList")' in q, "属性解析应用 propertyValueList（值是数组）"


def test_step2_varying_params_are_in_a_dashed_box():
    """**发货模式变化的参数放在「灰底 + 虚线框」子块里，并体现父子关系**
    （用户口径 2026-10-10，**取代** 2026-10-09 的「与现货模式齐平（无框无缩进）」：

    「发货模式切换得到的不同参数属性页面 ui 缩进到与第二个发货模式起头使用**灰色背景**
     单独**虚线框**，页面布局也要看到**父子关系**」；
    并实测「发货模式的 ui 布局不对…应参考开售时间的 ui 布局」）。

    ⇒ 与其它子块**共用 `.child-block`**（灰底 + 虚线框 + 126px 缩进），
      子块内用行式 `.child-param-row`（不套 `Field`，避免双重缩进）。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    assert 'className="child-block delivery-varying"' in tsx, "变化的参数应挂在 `.child-block` 子块里"
    #: 灰底 + 虚线框来自 `.child-block`（共用一套，不再单独定义）
    i = css.index(".child-block {")
    block = css[i : i + 420]
    assert "dashed" in block and "#fafafa" in block, "子块应是「灰底 + 虚线框」"
    #: 顶层发货子块按父字段标签列缩进（父子关系）
    j = css.index(".publish-config-step2 .child-block.delivery-varying")
    assert "margin-left: 126px" in css[j : j + 160], "发货子块应缩进到控件列"
    #: 过时的零框规则不得残留（会把上面的灰底虚线框压掉）
    assert ".publish-config-step2 .delivery-varying {" not in css, "过时的零框规则应已删除"
def test_step2_presale_days_are_expanded_by_default():
    """**预售发货时间默认展示**（用户口径：不需要先点「展开」）。"""
    raw = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx")
    #: 默认展开（注释可能被 _code_only 剥掉，故在**原文**上判）
    assert "presaleBatchOpen, setPresaleBatchOpen] = useState(true)" in raw, \
        "presaleBatchOpen 应默认展开"
    code = _code_only(raw)
    assert "presaleBatchOpen &&" in code, "预售天数应受展开态控制（默认 true）"


def test_step2_delivery_mode_doc_is_split_per_mode():
    """**发货模式说明按模式拆开 + 说明图标（Tip）展示**（用户口径 2026-10-09）。

    判据：① 说明是**按模式键控**的映射（每个模式一条），不再是一个整块文本；
          ② 每个模式挂一个说明图标（Tooltip）。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "DELIVERY_MODE_DOC: Record<string, string>" in tsx, "发货模式说明应为按模式键控的映射"
    for m in ("现货模式:", "全款预售模式:", '"现货+预售模式":', "按规格设置发货时间:"):
        assert m in tsx, f"发货模式说明缺对应模式：{m}"
    assert "DELIVERY_MODE_DOC[m]" in tsx, "发货模式缺说明图标的 Tooltip 接线"
    assert "delivery-mode-option" in tsx, "发货模式 label 缺容器（文案 + 图标同行）"
    #: 总览说明随「现货发货时间」行一起展示（用户口径 2026-10-09：挂在标题后 / 行尾）。
    #: ⚠️ 2026-10-10 子块改行式排布后，说明从 `Field` 的 `hint=` 改为**行尾的说明图标**
    #: （仍是 Tooltip，仍不占布局）—— 判据跟着改到行式形态，别只认旧写法。
    assert "DELIVERY_MODE_OVERVIEW" in tsx, "缺发货模式总览一句"
    assert "<Tooltip title={selectedShippingTemplate ? DELIVERY_MODE_OVERVIEW" in tsx, (
        "总览说明应以说明图标（Tooltip）挂在「现货发货时间」行尾"
    )
def test_step2_ship_options_disabled_without_logistics_template():
    """**物流模板未选时，发货模式/发货时间全部禁用 + 提示「请先选择物流模板」**。

    真机 bundle 实测：`disabled: !logisticsPlanId` + `description: "请先选择物流模板"`。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "selectedShippingTemplate" in code, "缺当前物流模板判据"
    assert "SHIP_DISABLED_HINT" in code, "缺「请先选择物流模板」提示常量"
    assert "\u8bf7\u5148\u9009\u62e9\u7269\u6d41\u6a21\u677f" in code, "提示文案不对"
    n = code.count("disabled={!selectedShippingTemplate}")
    assert n >= 2, f"发货模式与其时间选项都应受物流模板门控（实测 {n} 处）"


def test_step2_field_validation_messages_match_the_bundle():
    """**校验文案与千帆 bundle 一致**（字段名 → 规则）。

    bundle 实测：deliveryMode→「请选择发货模式」；existingValue→「请选择现货发货时间」；
    relativeValue→「请输入发货时间」；fixedValue→「请选择发货时间」。
    """
    code = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx")
    for msg in ("\u8bf7\u9009\u62e9\u53d1\u8d27\u6a21\u5f0f", "\u8bf7\u9009\u62e9\u73b0\u8d27\u53d1\u8d27\u65f6\u95f4",
                "\u8bf7\u8f93\u5165\u53d1\u8d27\u65f6\u95f4", "\u8bf7\u9009\u62e9\u53d1\u8d27\u65f6\u95f4"):
        assert msg in code, f"缺校验文案：{msg}"


def test_step2_rules_match_the_bundle_texts():
    """**规则文案与千帆 bundle 逐字一致**（详情页描述 / 详情页图片 / 开售时间）。"""
    code = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx")
    #: 详情页描述：真机「最大长度为500个字…」
    assert "\u6700\u5927\u957f\u5ea6\u4e3a500\u4e2a\u5b57" in code, "详情页描述规则文案不符"
    #: 详情页图片：真机「最多100张；像素5000*15000px以内…」
    assert "\u6700\u591a100\u5f20" in code, "详情页图片张数规则不符"
    assert "5000*15000px" in code, "详情页图片像素规则不符"
    assert "\u603b\u9ad8\u5ea6\u4e0d\u8d85\u8fc750000px" in code, "详情页图片高度规则不符"
    #: 开售时间：新建 3 项 / 编辑 2 项（真机 isEditMode 分支）
    assert "SHELF_TIME_OPTIONS_CREATE" in code and "SHELF_TIME_OPTIONS_EDIT" in code, \
        "开售时间应按新建/编辑模式区分"
    assert "props.isEditMode ?" in code, "开售时间未按模式分支"
    assert "\u4e0d\u8bbe\u7f6e\u5b9a\u65f6\u5f00\u552e" in code, "编辑模式选项文案不符"


def test_step2_price_row_rules_match_the_bundle():
    """**价格表逐列校验文案与千帆 bundle 一致**（逐字对过）。"""
    code = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx")
    for msg in (
        "\u8bf7\u8f93\u5165\u552e\u4ef7",
        "\u8bf7\u8f93\u5165\u5e02\u573a\u4ef7",
        "\u8bf7\u8f93\u5165\u5e93\u5b58\u53d8\u66f4\u503c",
        "\u6761\u5f62\u7801\u53ea\u652f\u6301\u5b57\u6bcd\u5927\u5c0f\u5199\u3001\u6570\u5b57\u3001-\u3001_/\u548c*",
        "\u4e0d\u80fd\u5c0f\u4e8e0.1\u5143",
        "\u5c0f\u6570\u70b9\u4f4d\u6570\u4e0d\u80fd\u8d85\u8fc71\u4f4d",
        "\u6253\u5305\u5546\u54c1\u7684\u5355\u4e2a\u552e\u4ef7\u4e0d\u80fd\u9ad8\u4e8e\u8be5\u5546\u54c1\u5355\u72ec\u552e\u5356\u4ef7\u683c",
    ):
        assert msg in code, f"缺价格校验文案：{msg}"
    assert "PRICE_ROW_RULES" in code, "缺价格校验常量"


def test_step2_spec_rules_match_the_bundle():
    """**规格规则与千帆 bundle 一致**（规格说明 / 规格图规则；「仅第一组」已被规格图开关取代）。"""
    code = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx")
    assert "SPEC_RULE" in code, "缺商品规格说明常量"
    assert "\u5355\u4e2a\u89c4\u683c\u503c\u7684\u4e0a\u9650\u6570\u91cf\u4e3a500\u4e2a" in code, "规格数量规则不符"
    assert "\u89c4\u683c\u503c\u957f\u5ea6\u4e0a\u9650\u4e3a60\u4e2a\u5b57\u7b26" in code, "规格值长度规则不符"
    assert "SPEC_IMAGE_RULE" in code, "缺规格图规则常量"
    assert "\u56fe\u7247\u5927\u5c0f\u5e94\u5c0f\u4e8e3MB" in code, "规格图规则不符"


def test_step2_keeps_only_the_four_visible_sections():
    """**常驻只保留四个分区**（用户口径 2026-10-09；2026-10-10 补充：尺码/资质
    入口也不再放助手里，见 `test_step2_publish_helper_panel_exists`）。

    千帆 bundle 里 `sizeInfo` / `qualInfo` 是**类目相关、按条件展示**的模块
    （`qualificationConfigs.length` 决定资质区是否出现；尺码表三类同理），
    **不是常驻表单区块** ⇒ 页面里不得有这两个 Section。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: ① 四个常驻分区必须在
    for name in ("\u57fa\u7840\u4fe1\u606f", "\u56fe\u6587\u4fe1\u606f",
                 "\u4ef7\u683c\u5e93\u5b58", "\u670d\u52a1\u4fe1\u606f"):
        assert f'<Section title="{name}"' in code, f"缺分区：{name}"
    #: ② 尺码信息 / 商品资质**不得**再是常驻 Section
    for banned in ("\u5c3a\u7801\u4fe1\u606f", "\u5546\u54c1\u8d44\u8d28"):
        assert f'<Section title="{banned}"' not in code, f"「{banned}」不应是常驻分区"
    #: ③ 弹窗保留（点开才交互），但**入口不在助手**（新口径：助手不放尺码/资质）
    assert "sizeTableOpen" in code and "qualificationOpen" in code, "缺两个弹窗开关"
    assert "SECTION_ANCHORS" in code, "缺分区锚点全表"
    #: 尺码表可选类型（真机 5 种）
    assert "SIZE_TABLE_KINDS" in code, "缺尺码表类型"
    assert "\u6a21\u7279\u8bd5\u7a7f" in code, "尺码表缺「模特试穿」"
    assert "\u7ecf\u6570\u636e\u9a8c\u8bc1\u8865\u5145\u5c3a\u7801\u8868" in code, "缺尺码表建议文案"


def test_step2_size_table_shapes_match_the_bundle():
    """**尺码表结构按 bundle**：三类表（基础/推荐/模特试穿）× 两种配置方式（表格/图片）。"""
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "SIZE_TABLE_SHAPES" in code, "缺尺码表 state 骨架常量"
    assert "SIZE_CONFIG_TYPES" in code, "缺尺码表配置方式常量"
    #: 三类表
    for k in ("baseSizeTable", "recommendSizeTable", "modelSizeTable"):
        assert k in code, f"缺尺码表类型：{k}"
    #: 两种配置方式
    assert "\u8868\u683c\u586b\u5199" in code and "\u4e0a\u4f20\u56fe\u7247" in code, "缺配置方式"
    #: 推荐尺码表的二维数据（complexSizeData）
    assert "complexSizeData" in code, "推荐尺码表缺二维数据字段"



def test_precheck_has_no_attribute_image_tab_or_kv_anywhere():
    """**属性图链路已整条移除**（用户口径 2026-10-11 最终版）。

    ⚠️ **取代** 2026-10-09 的 `test_precheck_attribute_image_adds_its_own_tab_with_kv`
    （该用例断言的四个符号现已全部删除）：

    | 轮次 | 属性图 | 
    |---|---|
    | 2026-10-09 | 属性图 OCR ⇒ KV 表 ⇒ 前端「商品属性」页签（KV 渲染） |
    | **2026-10-11（本条）** | **整条链路移除**（后端不算、前端无页签、字段不存在） |

    守卫方式 = 四类符号**一律 `not in`**（先 `_code_only` 剥注释，否则被迁移说明命中）。
    """
    pre = _code_only(_read(ROOT / "backend/app/services/shop_package_precheck.py"))
    ocr = _code_only(_read(ROOT / "backend/app/services/image_ocr_service.py"))
    #: ⚠️ 属性图**判据本身仍在**（它用于把属性图排除出媒体清单），搬到了叶子模块
    #: `package_media_names.py`。这里只钉「OCR 侧」与「契约侧」的移除。
    for symbol in ("_read_attribute_table",):
        assert symbol not in pre, f"{symbol} 应已从预检服务移除"
    assert not (ROOT / "backend/app/services/image_ocr_service.py").read_text(
        encoding="utf-8"
    ).count("scan_attribute_table"), "属性图 OCR 应已移除"
    for symbol in ("build_kv_from_boxes", "scan_attribute_table"):
        assert symbol not in ocr, f"{symbol} 应已从 OCR 服务移除"
    assert "kv=kv" not in pre, "属性表不再产出 kv"

    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    for symbol in ("attrTableIndex", "AttributeKvPanel", "attr-kv-key", "attr-kv-value"):
        assert symbol not in wiz, f"{symbol} 应已从前端移除"
    assert "Array.isArray(t.kv)" not in wiz, "kv 判据应已移除（字段不存在）"
    assert 'label: "商品属性"' not in wiz, "「商品属性」页签应已移除"

    api = _code_only(_read(ROOT / "frontend/src/lib/api.ts"))
    assert "kv?: { key: string; value: string }[]" not in api, "PrecheckTable 不该再有 kv 字段"

def test_step2_merges_the_two_parts_into_one_with_two_col_layout():
    """**第二步内部把千帆的两部分参数合并为一部分**（用户口径 2026-10-09）：
    「配置商品参数 的两大部分参数合并成一部分」+「合并后左右布局，
    商品发布助手和右侧商品配置」。

    判据：① 不再有内部两步（无 `innerStep` / 无内部步骤条 / 无内部导航）；
    ② 左栏「商品发布助手」+ 右栏「商品配置」的左右两栏结构保留；
    ③ 四个分区（基础信息 / 图文信息 / 价格库存 / 服务信息）依旧齐全。
    """
    code = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: ① 内部两步消失
    assert "innerStep" not in code, "仍有内部步骤态（应合并为一部分）"
    assert "publish-inner-steps" not in code, "仍有内部步骤条"
    assert "publish-inner-nav" not in code, "仍有内部导航按钮"
    assert "其余参数" not in code, "仍按「其余参数」拆步"
    #: ② 左右两栏布局保留
    assert "publish-two-col" in code, "缺左右两栏容器"
    assert "publish-col-left" in code and "publish-col-right" in code, "缺左/右栏"
    assert "商品发布助手" in code, "缺左栏「商品发布助手」"
    #: ③ 四个分区齐全（合并后一次铺完）
    for s in ("基础信息", "图文信息", "价格库存", "服务信息"):
        assert f'title="{s}"' in code, f"缺分区「{s}」"


def test_step2_entry_actions_are_documented():
    """**进第二部分触发的加载接口**已取证并落档（用户口径 2026-10-09）。

    真机实测：进第二部分一次性触发 **26 个不同 action**；
    其中 16 个**只依赖 categoryId** ⇒ 「自动加载参数」的数据来源。
    证据落在设计 28 §十三 / `.hermes/tmp/evidence_publish/step2_entry_actions.json`。
    """
    doc = _read(ROOT / "docs/02_设计文档/28_实测证据_第二部分交互.md")
    assert "getSellerCarriageTemplate" in doc, "设计 28 缺进二部分接口清单"
    assert "getPriceLimit" in doc, "缺价格 limit 接口"
    assert "getSetSizeTableInfo" in doc, "缺尺码表接口"
    assert "getProductQualificationRule" in doc, "缺资质规则接口"
    assert "categoryId" in doc and ("\u53ea\u4f9d\u8d56" in doc or "\u53ea\u9700" in doc), \
        "缺「只依赖 categoryId」这一关键结论"



def test_step2_after_sale_rule_comes_from_platform_by_category():
    """**售后服务按类目从平台取，`must_support` 决定是否可编辑**（用户口径 2026-10-09）。

    用户要求「调研一下、真实测试抓取对应接口参数分析」⇒ 实测 `getSupportAfterSale`：
    `mustSupport=true` 强制支持（勾选禁用）、`false` 可自选；规则内容也按类目变。
    ⇒ 页面按 `props.afterSaleRules` 渲染，**不得写死单条**。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "afterSaleRules" in tsx, "售后服务未按平台规则渲染"
    assert "r.must_support" in tsx, "未按 must_support 判定可编辑性"
    assert "disabled={r.must_support}" in tsx, "平台强制项应禁用勾选"
    #: 不得再写死「支持七天无理由退货」这一条文案
    assert "支持七天无理由退货" not in tsx, "售后服务不得写死单条规则（按类目变）"
    #: 端点 + SDK + 请求函数三层都在
    api = _read(ROOT / "frontend/src/lib/api.ts")
    assert "fetchAfterSaleRules" in api, "缺售后规则请求函数"
    q = _read_qianfan()
    assert "get_support_after_sale" in q, "端点未调 SDK 的售后规则方法"
    sdk = _read(ROOT / "apis/xhs_qianfan_apis.py")
    assert '"action": "getSupportAfterSale"' in sdk, "SDK 缺 getSupportAfterSale"


def test_video_upload_uses_goods_video_scene_and_registers():
    """**商品视频走 `goods_video` 场景，且必须登记**（2026-10-22 真机抓包坐实）。

    ⚠️⚠️ **本条断言被推翻并重写**（原句保留在下面，不静默删除）：

        原文：「商品视频走独立直传（`common_file`），不走图片空间」（2026-10-09）
              assert 'scene="common_file"' in block
              assert "register_material_files" not in block

    **两个断言都是错的**，依据是 `9515.*.js` 里一句 `scene:"common_file"`（那属于
    别的上传场景）。真机抓包（用户配合，商品发布页上传一次视频）显示平台自己：

        GET  /api/edith/upload/web/permit?biz_name=ark&scene=goods_video&file_count=1
        POST https://ros-upload.xiaohongshu.com/?uploads&prefix=arkgoods/<id>
        POST /api/edith/product/material_space/batch_add_directory_manageable   ← 登记

    ⇒ 视频与图片**同桶**（`arkgoods/`），所以**必须登记**拿 `materialId`；
    用 `common_file` 会落 `ark-common/` 桶，登记恒答「获取上传文件[v]信息失败」
    ⇒ `materialId` 空 ⇒ 提交体引用无效 ⇒ **商品发布后主图视频是空白框、播不了**。

    教训：旧守卫把**错误结论固化成了红线**，改代码时被它拦下才知道要一起改。
    详细试错账见 `docs/05_项目沉淀/07_复盘_商品主图视频发布后无法播放.md`。
    """
    q = _read_qianfan()
    assert '"/video/upload"' in q, "缺视频直传端点"
    i = q.index("def video_upload")
    #: ⚠️ 窗口要覆盖到**登记段**（`register_material_files` 在 ~7000 字符处，
    #: 3500 只到「上传凭证没有预分配文件 ID」⇒ 首轮误判「没登记」）。
    block = q[i : q.index("\n@router.", i)]

    assert 'scene="goods_video"' in block, (
        "视频上传申请凭证的 scene 必须是 goods_video（common_file 会落错桶）"
    )
    assert 'scene="common_file"' not in block, (
        "视频仍在用 common_file —— 文件落 ark-common 桶，登记必然失败、发布后播不了"
    )
    assert "register_material_files" in block, (
        "视频必须登记图片空间（拿 materialId），否则提交体 videos[] 引用无效"
    )
    #: 前端：视频不再复用图片上传
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    assert "uploadVideoFromPackage" in wiz, "自动加载未用视频专用上传"
    assert "uploadProductVideo" in wiz, "缺视频上传请求函数接线"
    api = _read(ROOT / "frontend/src/lib/api.ts")
    assert "/xhs/qianfan/video/upload" in api, "前端缺视频上传请求函数"


def test_step2_main_and_detail_images_are_required():
    """**商品主图 / 商品详情图必填**（用户口径 2026-10-09）。"""
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert 'label="商品主图" required' in tsx, "商品主图应标必填"
    assert 'label="商品详情图" required' in tsx, "商品详情图应标必填"


def test_step2_logistics_template_shows_a_default_value():
    """**物流模板要有默认值展示**（用户口径 2026-10-09）。

    ⇒ 未手选时回落第一个可选计划（`selectedShippingTemplate`），开箱即显示，
    不留下拉空着。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    i = tsx.index('label="物流模板"')
    block = tsx[i : i + 1200]
    assert "value={selectedShippingTemplate" in block, \
        "物流模板下拉未回落到默认计划（应显示默认值）"


def test_auto_load_progress_shows_per_stage_elapsed_time():
    """**进度弹窗每一项要有耗时统计**（用户口径 2026-10-09）。

    ⚠️ 判据要**同时看定义与渲染**：只断 `"formatStageElapsed" in wiz` 是**哑弹**
    （把符号整体改名成 `formatStageElapsedX` 仍含该子串）。实测：只断子串 ⇒ 变异 ESCAPED。
    """
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    assert "export function formatStageElapsed(" in wiz, "缺耗时格式化函数定义"
    assert "formatStageElapsed(" in wiz, "耗时格式化未被调用"
    #: 渲染点：耗位列在阶段行里
    assert 'className="auto-stage-elapsed"' in wiz, "进度行缺耗位列渲染"
    #: 运行中要能实时涨（秒表），终态定格
    assert "export function runningElapsedMs(" in wiz, "运行中阶段缺实时耗时函数"
    assert "performance.now()" in wiz, "耗时应用单调钟（performance.now）"
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.css")
    assert ".auto-stage-elapsed {" in css, "缺耗位列样式"


def test_auto_load_confirm_offers_media_upload_retry_settings():
    """「自动加载参数」确认弹窗要有**上传失败重试 + 重试次数**（2026-10-11 用户口径）。

    用户原话：「第二步的自动加载参数弹窗新增**上传媒体资源失败是否重试，默认是**，
    以及**重试次数 1-3，默认 1**」。

    ⚠️ 判据四件，缺一不可（漏一件都会让「配了但没生效」或「生效了但没得配」）：

    1. 弹窗里有开关（是 / 否）+ 数字框（min/max 1–3），**默认 true / 1**；
    2. 开关关闭时数字框 `disabled`（填了也不生效的控件必须明显不可用）；
    3. 有一个**共用的重试包装**（`withUploadRetry`）—— 不共用就会「主图重试、详情图不重试」；
    4. 三条媒体上传链路（主图 / 详情图 / 视频）**都真的经过它**，且视频要用 `ok` 判成功
       （视频失败是返回 `{ok:false}` 而不是抛异常 —— 只 catch 异常的重试等于没有）。
    """
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    #: ① 控件与默认值
    assert "上传媒体资源失败时重试" in wiz, "确认弹窗缺「上传媒体资源失败是否重试」"
    for piece in ("<Radio value=\"yes\">是</Radio>", "<Radio value=\"no\">否</Radio>"):
        assert piece in wiz, f"重试开关缺选项：{piece}"
    assert "useState(true)" in wiz and "useState(1)" in wiz, (
        "默认值必须是「是 / 1 次」（用户口径「默认是」「默认1」）"
    )
    assert "uploadMaxRetries" in wiz and "min={1}" in wiz and "max={3}" in wiz, (
        "重试次数必须是 1–3 的数字框"
    )
    #: ② 关闭重试时数字框不可用
    assert "disabled={!uploadRetryOnFailure}" in wiz, "关掉重试后次数框应禁用"
    #: ③ 共用包装（唯一定义点 + 真的被调用）
    assert "const withUploadRetry = useCallback(" in wiz, "缺共用的重试包装函数"
    #: ⚠️ 2026-10-11 口径变更：主图 / 详情图改走 `uploadManyFromPackage`（并发批量），
    #: **不再逐张过 `withUploadRetry`** —— 批量端点自带「单张失败重试一次」，
    #: 且单项失败不整批失败。故这里不再要求「≥3 处调用」。
    #: 现只要求：视频链路仍用重试包装（它是独立端点，没有批量替代）。
    assert "withUploadRetry(" in wiz, "重试包装应仍被视频链路使用"
    #: 反向：不许主图/详情图又退回逐张串行（那是并发被撤销的信号）。
    assert "uploadManyFromPackage" in wiz, "主图/详情图应走并发批量上传"
    #: ④ 视频的成功判据是 `ok`（不是「没抛异常」）
    assert "(r) => r.ok" in wiz, "视频上传必须以 `ok` 判成功，否则失败结果会被当成功"


def test_step2_price_section_labels_share_one_column():
    """**价格库存区的字段标签共用同一条列**（用户口径 2026-10-09：布局错位）。

    根因两处：① 虚线框整块 `margin-left: 126px` + 框内字段又有 126px 标签列 ⇒ 右移一列；
    ② 框内标签列写成 120px（外层 126px）⇒ 差 6px 对不齐。
    """
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    #: ⚠️ 用户口径（2026-10-10，**取代** 2026-10-09 的「去掉虚线框缩进」）：
    #: 子参数块要有**灰色背景 + 虚线框**并体现**父子关系** ⇒ 用统一的 `.child-block`。
    i = css.index(".child-block {")
    block = css[i : i + 420]
    assert "dashed" in block, "子参数块应是虚线框"
    assert "#fafafa" in block, "子参数块应有灰色背景"
    assert "126px 1fr" in css, "父字段标签列应为 126px"
    #: 外层字段列也是 126px（必须同值，否则错位）
    assert "grid-template-columns: 126px 1fr" in css, "字段标签列应为 126px"
    #: 三处子块都挂 `.child-block`（发货模式 / 商品规格 / 定时开售）
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert tsx.count("child-block") >= 3, "发货模式 / 商品规格 / 定时开售都应挂子参数块"


def test_step2_spot_ship_time_defaults_to_the_recommended_option():
    """**现货发货时间默认选中「48小时（推荐）」**（用户口径 2026-10-09）。

    ⚠️ 实测缺陷：默认值写成 `"48小时"`，而候选是 `"48小时（推荐）"` ——
    值与选项**对不上** ⇒ 单选**一项都不选中**（页面看起来「没有默认」）。
    判据：默认值必须**取自候选数组**（而不是手写字面量），杜绝再次对不上。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: 默认值来自候选数组的「推荐」项（SPOT_SHIP_TIME_OPTIONS 末项 = 真机标推荐的那项）
    assert "SPOT_SHIP_TIME_OPTIONS[SPOT_SHIP_TIME_OPTIONS.length - 1]" in tsx, \
        "现货发货时间默认值应取自候选数组（手写字面量会与选项值对不上）"
    #: 不得再出现与选项值对不上的裸字面量
    assert 'useState<string>("48小时")' not in tsx, "默认值不得写死为「48小时」（与选项值不符）"
    #: 「推荐」项确实是 48 小时
    assert "48小时（推荐）" in tsx, "候选里应有「48小时（推荐）」"


def test_precheck_table_tab_is_named_sku_table():
    """预检「表格」页签改名为**「商品sku表格」**（用户口径 2026-10-09）。"""
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    assert "商品sku表格（" in wiz, "页签名应为「商品sku表格」"
    assert "label: `表格（${" not in wiz, "旧「表格」页签名应已改掉"


def test_precheck_table_supports_batch_price_and_stock_edit():
    """**表格每列都支持批量修改**（用户口径 2026-10-09）。

    三种模式：原参数基础上加减固定值 / 原参数 × 倍数 + 固定值 / 统一改成固定值。
    判据：① 纯逻辑在 lib（可 vitest）；② 页面接线；③ 三种模式文案都在。
    """
    lib = _read(ROOT / "frontend/src/lib/precheck-publish.ts")
    #: ① 三种模式 + 两个入口
    for mode in ('"delta"', '"scale"', '"fixed"'):
        assert mode in lib, f"缺批量模式 {mode}"
    assert "export function applyBatchRule" in lib, "缺批量计算纯函数"
    assert "export function applyBatchRuleToTable" in lib, "缺整列批量函数"
    assert "export function priceColumnIndex" in lib, "缺价格列识别"
    assert "export function stockColumnIndex" in lib, "缺库存列识别"
    #: ② 页面接线
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    #: ⚠️ 2026-10-22 换锚：`BatchColumnBar`（每列一条批量修改条的旧入口）已于
    #: 2026-10-21 删除（全仓无调用点，被 `ColumnPatternControl` 取代）。
    #: 判据不变：页面必须接上**批量修改控件**。
    assert "BatchColumnControl" in wiz, "页面未接批量修改控件"
    assert "applyBatchRuleToTable" in wiz, "页面未调整列批量函数"
    for label in ("加减固定值", "倍数+固定值", "统一改固定值"):
        assert label in wiz, f"缺批量模式选项：{label}"


def test_precheck_batch_column_is_selectable_not_hardcoded():
    """批量修改**先选列**，不写死「价格 / 库存」两列（用户口径 2026-10-09）。

    用户原话：「有原价有计算价格，还有可能是更多其它值，应选择指定的列，
    而不是写价格列和库存列」。
    """
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    #: ① 必须有一个**列选择器**（`Select`，选项来自 `table.columns`）
    assert "options={columns.map((name, index) => ({ label: name, value: index }))}" in wiz, (
        "缺列选择器（应列出表里所有列供选择）"
    )
    assert "value={column}" in wiz and "onChange={onColumnChange}" in wiz, "列选择器未受控"
    #: ② 不能只剩写死的两条（旧写法）
    assert 'label="价格"' not in wiz and 'label="库存"' not in wiz, (
        "不应再写死「价格 / 库存」两条批量条"
    )
    #: ③ 确认文案要写出**改的是哪一列**（轻提示里）
    assert "「${colName}」" in wiz, "动作描述应写明列名"


def test_precheck_batch_apply_has_no_confirm_dialog():
    """批量修改**移除二次弹窗提醒**，点「应用」直接落改（用户口径 2026-10-09）。"""
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    assert "applyDirectly" in wiz, "应改为直接应用（applyDirectly）"
    #: 轻提示要落在 **applyDirectly 这个落改函数里**（不是文件里别处的 message）
    #: ⚠️ 2026-10-22 换锚：`applyDirectly` 已从内联函数改为**由父层注入的 prop**
    #: （`applyDirectly: (text, run) => void`），落改动作在调用处传 `run`。
    #: 判据不变：点「应用」直接落改 + 给一条轻提示，不需要二次确认。
    assert "applyDirectly: (text: string, run: () => void) => void;" in wiz, (
        "`applyDirectly` 应仍是「先落改再轻提示」的签名"
    )
    assert "applyDirectly(`${describe()} 已应用${where}`, () => onApply(column, rule, scope));" in wiz, (
        "应点即落改（把 onApply 直接作为 run 传进去）"
    )
    #: 不得再有确认弹窗
    assert "confirmThen" not in wiz, "confirmThen 二次确认应已移除"
    #: ⚠️ 判据**收敛到「批量修改这一段」** —— 全文件断言 `modal.confirm` 已不成立：
    #: 用户口径 2026-10-14 明确要求「**还原列值需二次确认**」，那一处就该有 `modal.confirm`
    #: （它属于「整表还原」而不是「批量修改」，两者不是同一条链路）。
    #: 把「批量修改这一段」界定为 `BatchColumnControl` 的函数体。
    i = wiz.index("function BatchColumnControl(props: {")
    j = wiz.index("function ", i + 10)
    apply_block = wiz[i:j]
    assert "modal.confirm" not in apply_block, "批量修改不应再弹确认框"


def test_precheck_batch_delta_has_sign_selector():
    """加减 / 倍数模式的**中间符号可切换**（图标点击，默认加）（用户口径 2026-10-09）。

    用户原话：①「加减固定值是加还是减添加加/减符号选择」；
              ②「倍数+固定值，中间可能加也可能是减让用户选择，默认加号」；
              ③「加减做成图标点击切换」。
    """
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    assert 'useState<"+" | "-">("+")' in wiz, "缺加减符号状态（默认应为加号）"
    #: 图标点击切换（不是 radio / 下拉）
    assert "sign === \"+\" ? <PlusOutlined /> : <MinusOutlined />" in wiz, (
        "符号应以图标呈现（加/减各一个图标）"
    )
    assert 'onClick={() => setSign(sign === "+" ? "-" : "+")}' in wiz, (
        "符号未实现点击切换"
    )
    #: 符号选择器要在**加减与倍数两种模式**下都出现
    assert '(mode === "delta" || mode === "scale") && (' in wiz, (
        "符号切换应覆盖加减与倍数两种模式"
    )
    #: 符号要真的并进操作数（减 ⇒ 取负），否则切了也没用
    assert 'const signed = sign === "-" ? -Math.abs(raw) : raw;' in wiz, (
        "符号未并进操作数（切到「减」也还是加）"
    )


def test_precheck_batch_edit_skips_empty_cells():
    """批量修改**空值不参与**（缺值口径：不造假）。"""
    lib = _read(ROOT / "frontend/src/lib/precheck-publish.ts")
    i = lib.index("export function applyBatchRule(")
    block = lib[i : i + 900]
    assert "onlyFilled !== false" in block, "空值应默认跳过"
    #: 整列版也要保留空单元格原样
    j = lib.index("export function applyBatchRuleToTable(")
    tail = lib[j : j + 900]
    #: ⚠️ 2026-10-22 换锚：空值分支在 `return cell` 之前多了一步
    #: `badRows.add(rowIndex)`（用户口径：无法计算的列红框 + 取消勾选）。
    #: 判据不变：**空值不写 0、不造假**，保留原样。
    assert "if (next === null) {" in tail and "return cell;" in tail, (
        "整列批量应跳过空值（保留原样）"
    )


def test_precheck_sku_table_has_row_checkboxes_for_scoped_batch():
    """规格列表加**复选框**，勾选后批量修改**只作用于选中行**（用户口径 2026-10-09）。

    判据：① 表格有 `rowSelection`；② 勾选数上报到批量条；③ 纯逻辑支持行范围；
          ④ 范围外行不被改动（见 vitest `countFilledInColumn` / `onlyRows`）。
    """
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    #: ⚠️ 2026-10-22 换锚（用户口径 2026-10-21「把复选框与 sku 图列拆分为两列」）：
    #: 勾选已从 antd `rowSelection` 隐式列改为**普通数据列**（`COL_CHECK`，宽 48
    #: + `fixed: "left"`）—— 内置列宽度由 antd 定、紧贴 SKU 图列会被缩略图盖住。
    #: 判据不变：表格必须有勾选列，且勾选状态受控于 `pickedRows`。
    assert "COL_CHECK" in wiz, "规格表缺复选框列"
    assert "pickedRows" in wiz, "复选框未受控于勾选行"
    assert "pickedRows" in wiz, "缺勾选行状态"
    assert "pickedRows={pickedRows}" in wiz, "批量条未拿到勾选行"
    #: 作用域提示要能看出「仅对勾选行 / 整列」
    assert "仅对勾选的" in wiz, "缺「仅对勾选行生效」提示"
    #: 纯逻辑的 onlyRows 参数
    lib = _read(ROOT / "frontend/src/lib/precheck-publish.ts")
    i = lib.index("export function applyBatchRuleToTable(")
    block = lib[i : i + 900]
    assert "onlyRows" in block, "整列批量函数缺行范围参数"
    assert "scope.has(rowIndex)" in block, "范围外行应原样不动"
    assert "export function countFilledInColumn(" in lib, "缺按行范围计数（确认文案用）"


def test_precheck_panel_maps_edits_back_to_the_full_table_array():
    """面板改表后**必须嵌回完整数组**（用户实测 2026-10-09：第二次改库存无响应）。

    根因：页面把 `tables.filter(非 kv)` 的**子集**交给面板，面板改完直接上抛子集 ⇒
    属性表（kv）被丢掉 ⇒ 页签集合变化 ⇒ 面板重挂载、`operand` 归零 ⇒ 「应用」按钮
    回到禁用（第二次点击看起来「无响应」）。

    判据：① 面板上抛**单表**（不是整数组）；② 页面用 `mergePanelTable` 嵌回；
          ③ 纯逻辑 `mergePanelTable` / `skuTableIndices` 在 lib（vitest 覆盖）。
    """
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    assert "onTableChange={onTableChange}" not in wiz, "面板不应再直接上抛整数组"
    assert "onTablesChange(mergePanelTable(tables, panelIndex, next))" in wiz, (
        "页面必须用 mergePanelTable 把单表改动嵌回完整数组"
    )
    #: ⚠️ 原先此处断言 `tables.filter((t) => !Array.isArray(t.kv))`（面板只显示 SKU 表）。
    #: 属性图 KV 链移除后**不再需要这个过滤**（数据包里的表全是 SKU 表）⇒ 断言**反向**：
    #: 该过滤应已消失（留着说明「属性表」概念还在，是回归信号）。
    assert "Array.isArray(t.kv)" not in wiz, "kv 过滤应已随属性图链移除"
    lib = _read(ROOT / "frontend/src/lib/precheck-publish.ts")
    assert "export function mergePanelTable(" in lib, "缺 mergePanelTable（嵌回完整数组）"
    assert "export function skuTableIndices(" in lib, "缺 skuTableIndices（SKU 表真实下标）"


def test_precheck_row_keys_are_numeric_row_indices():
    """勾选键必须是**数字行下标**（用户实测 2026-10-09：应用未生效）。

    根因：`rowKey` 写成字符串 ``${tIndex}-${index}`` ⇒ `selectedRowKeys` 里是
    `"0-5"`，而批量修改按 `scope.has(rowIndex)` 比对**数字** 5 ⇒ 永不匹配 ⇒
    勾选后「应用」点了没有任何列变化（未勾选整列时也一样被 scope 拦掉）。
    """
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    assert 'rowKey={(record: { __index: number }) => record.__index}' in wiz, (
        "rowKey 必须返回数字行下标（否则勾选键与 scope 类型不符，批量永不生效）"
    )
    assert "`${tIndex}-${index}`" not in wiz, "rowKey 不得再用字符串 `${tIndex}-${index}`"


def test_property_grid_is_three_per_row_with_fixed_180px_controls():
    """【已被取代】旧口径「一行 5 个 / 控件 180-200px」。

    ⚠️ 2026-10-10 用户口径：「属性调整为**一行两个**、**输入框宽度自适应**、
    **移除多选框 css 用默认**、**属性区最高 400px 可滚动**」⇒ 本条**由
    `test_property_grid_is_two_columns_fluid_and_unstyled_multi` 取代**，
    原断言整段移除（不静默删除，保留取代关系）。
    """

def test_media_thumbs_support_preview_and_hover_delete():
    """主图 / 视频 / 详情图**支持大图预览 + 左右切换**；视频删除与图片一致（悬停右上角）。

    用户口径（2026-10-09）：①「商品视频支持大图预览，删除按钮应与图片类似鼠标滑到
    视频区域内右上角展示删除图标」；②「商品主图详情图均支持大图预览，左右切换」。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: 三种媒体共用同一个缩略图组件（主图 / 视频 / 详情图）
    assert tsx.count("<MediaThumbs") >= 3, "主图 / 视频 / 详情图都应走 MediaThumbs"
    #: 大图预览弹窗 + 左右切换
    assert "setPreviewIndex" in tsx, "缺预览索引状态"
    assert "上一张" in tsx and "下一张" in tsx, "预览要能左右切换"
    #: 视频缩略图也走 preview（点击层）
    assert 'className="media-thumb-open"' in tsx, "缩略图应有点击预览层"
    #: 删图标只在悬停显形（CSS）
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    assert ":hover .media-thumb-remove" in css, "删除图标应悬停才显形"


def test_video_upload_uses_md5_name():
    """视频上传前**按内容 MD5 命名**（用户口径 2026-10-09：「视频和商品图一样上传前改成
    md5 唯一名称」）。

    唯一真相源在后端（`video_upload`）—— 无论前端传什么名，一律按内容 md5 命名。
    """
    src = _read_qianfan()
    i = src.index('@router.post("/video/upload"')
    block = src[i : i + 2600]
    assert "hashlib.md5(raw).hexdigest()" in block, "视频应按内容 md5 命名"
    #: 不得再「名字为空才 md5」——那样前端传原名就不唯一了
    assert 'if not file_name:' not in block, "应一律 md5，不再只在空名时兜底"


def test_spec_value_supports_individual_and_batch_image():
    """每个规格值支持**单独加规格图**（输入框前缀展示）（用户口径 2026-10-09）。

    ⚠️ **2026-10-13 取代**（用户口径，提交 55e0924）：**批量添加**（`pickBatchSpecImages`）
    与「批量上传规格图」按钮已被移除 ⇒ 只保留**单独加**（`pickSpecImage` + 前缀展示）。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "pickSpecImage" in tsx, "缺单独加规格图"
    assert "spec-value-thumb" in tsx, "规格图应作为输入框前缀展示"
    assert "specImages" in tsx, "规格图应有状态（按规格值存）"
    #: 规格图可用性由**规格图开关**决定（用户口径 2026-10-10 纠正，取代「仅第一组」）
    assert "specImageEnabled[name] ?? gi === 0" in tsx, "规格图可用性应由开关决定（默认回落第一组）"
    #: 反向：批量加规格图已移除（用户口径 2026-10-13）
    assert "pickBatchSpecImages" not in tsx, "批量加规格图已按用户口径移除"


def test_price_rows_sync_with_spec_values():
    """添加规格时**价格及库存表同步添加**（用户口径 2026-10-09）。"""
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "spec_combinations(" in tsx, "价格表应按规格组合出行"
    assert "onPriceRowsChange" in tsx, "应把新行上抛给外层"
    lib = _read(ROOT / "frontend/src/lib/publish-config.ts")
    assert "export function spec_combinations(" in lib, "缺规格组合纯函数（可 vitest）"


def test_media_preview_does_not_autoplay_and_stops_on_close():
    """预览弹窗**点击播放才播**，关闭后**停止播放**（用户口径 2026-10-09）。

    用户原话：「视频预览关闭弹窗后依旧在播放，预览弹窗中需点击播放才进行播放，
    不要自动播放」。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    i = tsx.index("function MediaThumbs(")
    block = tsx[i : i + 6000]
    #: 不得自动播放
    assert "autoPlay" not in block, "预览视频不得自动播放"
    #: 关闭前要显式暂停
    assert "v.pause()" in block, "关闭预览应显式暂停视频（否则关了还在响）"
    assert "currentTime = 0" in block, "关闭预览应复位播放进度"
    #: 关闭走 closePreview（而非直接 setPreviewIndex(null)）
    assert "onCancel={closePreview}" in block, "关闭按钮应走 closePreview（停播）"
    assert "preload=\"metadata\"" in block, "视频应 preload=metadata（不预取整段）"


def test_price_table_is_editable_except_spec_and_has_batch_bar():
    """价格及库存表（用户口径 2026-10-09，**覆盖上一版「操作列 + 编辑价格/库存弹窗」**）：
    「价格及库存列表新增规格图列，**移除操作列**，表格顶部新增规格名称搜索，
      新增批量修改列值修改逻辑与预检中的批量修改 sku 表批量修改功能一致」。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: 规格列**带行级启用切换**（用户口径 2026-10-10：默认启用 + 不启用/启用链接）
    #: ⚠️ 2026-10-22 换锚：启用切换已从「单列 `spec` 的 render 分支」改为
    #: **维度列组件的 render**（`dimCols`，只放最后一维，每行一个决定）。
    #: 判据不变：规格行必须有行级启用/停用切换。
    assert "di === dims.length - 1" in tsx, "规格列应有启用切换 render"
    #: ⚠️ 2026-10-22 口径修订：`sku-enabled-btn`（可点击链接）已改为**只读 Tag**
    #: （用户口径「功能列重复，只保留一个操作入口，规格后的不启用/启用用预览 tag」）。
    #: 唯一操作入口 = 操作列的 Switch ⇒ 这里钉 Tag + 反向禁止旧链接回来。
    assert "sku-enabled-btn" not in tsx, (
        "规格列又出现了 `sku-enabled-btn` 可点击链接 —— 与操作列 Switch 功能重复"
    )
    assert '"已启用" : "不启用"' in tsx, "规格列缺状态预览 Tag"
    assert "available !== false" in tsx, "行级启用状态默认应为启用（available !== false）"
    #: ① 新增「规格图」列
    assert '"规格图"' in tsx and "specImageForRow" in tsx, "缺规格图列"
    #: ② 移除操作列（编辑价格 / 编辑库存入口撤掉）
    assert "PriceEditModal" not in tsx and "StockEditModal" not in tsx, "操作列与编辑弹窗应已移除"
    assert "编辑价格" not in tsx and "编辑库存" not in tsx, "不应再有编辑价格/库存入口"
    #: ③ 表顶规格名称搜索
    assert "搜索规格名称" in tsx and "filteredPriceRows" in tsx, "缺规格名称搜索"
    #: ④ 批量填写 + 批量修改列值（用户口径 2026-10-10：两者并存）
    assert "PriceRowBatchBar" in tsx, "缺批量填写"
    assert "只应用于单包商品" in tsx, "批量填写弹窗应带副标题「只应用于单包商品」"
    assert "<PriceRowBatchEditModal" in tsx, "缺「批量修改列值」弹窗"
    assert "批量修改列值" in tsx, "缺「批量修改列值」入口"
    #: 批量修改列值 = 三模式（与预检一致）
    assert "applyBatchRule" in tsx, "批量修改列值应复用 applyBatchRule（三模式）"
    for label in ("加减固定值", "倍数+固定值", "统一改固定值"):
        assert label in tsx, f"批量修改列值缺模式：{label}"
    #: 更多操作下拉
    assert "更多操作" in tsx, "价格库存表顶部应有「更多操作」下拉"
    #: 单元格可编辑（写回外层）
    assert "props.onPriceRowsChange(" in tsx, "单元格编辑应写回价格行"


def test_price_table_has_package_bundle_modal():
    """**多件装打包**（用户口径 2026-10-10，真机 DOM 设计 24 §14）：
    弹窗「选择要打包的数量」，每打包规格一行 `数量`(min=2)+`价格`，「新增打包规格」，
    取消/确定；数据进 ConfigSnapshot（`packageBundles`）。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "PackageBundleModal" in tsx, "缺多件装打包弹窗组件"
    assert "多件装打包" in tsx, "缺「多件装打包」按钮文案"
    assert "选择要打包的数量" in tsx, "弹窗应带说明「选择要打包的数量」"
    assert "新增打包规格" in tsx, "缺「新增打包规格」按钮"
    #: 数量 min=2（真机最少 2 件打包）
    assert "min={2}" in tsx, "打包数量最少应为 2"
    #: 数据上报进快照（提交/预览用）
    assert "packageBundles" in tsx, "打包规格应进 ConfigSnapshot"
    #: 入口在「更多操作」下拉里（用户口径 2026-10-10：库存工具并入下拉）
    assert "多件装打包" in tsx and "更多操作" in tsx, "多件装打包应并入「更多操作」下拉"


def test_logistics_type_is_radio_defaulting_to_no_logistics():
    """物流类型**与类目直接相关**（用户口径 2026-10-09：「物流类型应和类目有直接关系，
    你调研并实际测试一下再完善对应逻辑」）。

    真机 bundle（`8091.*.js`）`logisticsTypeOptions`：默认只给 **「物流配送」**；
    仅当类目支持「无物流发货」时才追加「无物流发货或物流配送」（tip 原文见常量）。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: ⚠️ 2026-10-22 换锚：物流类型候选从写死对象改为枚举常量 + 渲染下发。
    #: 见 `LOGISTICS_TYPE_LABELS`（前端）/ `qianfan_item_enums.py`（后端唯一真相源，设计 24 §23）。
    assert "LOGISTICS_TYPE_LABELS.logistics" in tsx, "「物流配送」候选缺失"
    #: ⚠️ 2026-10-22 换锚：候选文案的唯一真相源已收进 `lib/qianfan-enums.ts`
    #: （与后端 `qianfan_item_enums.py` 同口径），step2 只 import 不再内联。
    enums = _read(ROOT / "frontend/src/lib/qianfan-enums.ts")
    assert 'notLogistics: "无物流发货或物流配送"' in enums, "「无物流发货或物流配送」候选缺失"
    #: 默认「物流配送」
    assert "useState<string>(LOGISTICS_TYPE_LABELS.logistics)" in tsx, "默认应为「物流配送」"
    #: 按类目下发（support_no_logistics）决定是否出现第二个候选
    assert "supportNoLogistics === false" in tsx, "应按类目收口候选"
    #: Tooltip 文案（逐字）—— ⚠️ 2026-10-22 换锚：文案常量在 `lib/qianfan-enums.ts`
    #: （`NOT_LOGISTICS_HINT`），step2 只 import。
    assert "无需上传快递单号即可发货成功" in enums, "物流类型说明文案缺失"


def test_sale_time_schedule_shows_datetime_picker_with_real_rules():
    """开售时间与千帆一致（真机 bundle 逆出，用户口径 2026-10-09：「开售时间的交互逻辑
    和页面 ui 布局，你也测试调研后补充完整与千帆后台一致」）。

    真机事实（`8091.*.js`）：
      - `shelfType` 枚举 ON_SHELF=1 / OFF_SHELF=2 / SCHEDULE_SHELF=3；
      - 新建模式 3 项、编辑模式 2 项（无「审核通过后立即上架开售」）；
      - 选 SCHEDULE_SHELF ⇒ 出现 `expectedPurchaseTime`，规则 min=now+1h、max=now+30d。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: 新建 / 编辑两套候选（真机分支）
    assert 'SHELF_TIME_OPTIONS_EDIT = ["不设置定时开售", "定时开售"]' in tsx, "编辑模式候选项缺失"
    assert "审核通过后立即上架开售" in tsx and "不设置，手工上架开售" in tsx, "新建模式三项缺失"
    #: 选「定时开售」才出现时间选择器
    assert "expectedPurchaseTime" in tsx, "缺开售时间状态（定时开售）"
    assert 'saleTime === "定时开售"' in tsx, "定时开售才出现时间选择器"
    #: 真机规则 min=now+1h / max=now+30d
    assert "Date.now() + 3600_000" in tsx, "min 应为 1 小时后"
    assert "Date.now() + 30 * 86400_000" in tsx, "max 应为 30 天内"


def test_spec_values_all_have_image_upload_thumb():
    """规格图**每个规格值都有**（第一组 / 开启规格图的组）。

    ⚠️ 位置口径**已被取代**（2026-10-11 用户实测：「商品规格输入框文本过多时，向右走
    会将规格图挤掉，再回到最左侧时，规格图也无法展示」）——
    原口径「作为输入框的 `prefix`」正是根因：`ant-input-prefix` 在输入框**内部**，
    随文本横向滚动 ⇒ 图被顶出可视区。
    ⇒ 现口径：规格图是输入框的**兄弟节点**（`.spec-value-item` 直接子元素），
    固定在框外左侧，与文本滚动无关。**「每个规格值都有图」这条语义不变。**
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: 位置：兄弟节点（不再是 prefix）。
    assert 'className="spec-value-item"' in tsx, "规格图应是输入框的兄弟节点（.spec-value-item）"
    assert "prefix={prefix}" not in tsx, (
        "规格图不得再挂 prefix（prefix 在输入框内部，随文本滚动被挤走）"
    )
    assert "{thumb}" in tsx, "缩略图应作为 item 的子节点渲染"
    assert "const canImage = specImageEnabled[name] ?? gi === 0;" in tsx, "每个规格值可加图与否由规格图开关决定（默认回落第一组）"
    #: 点前缀 → 弹窗选**预检通过且选中的 SKU 图**（判据用**渲染形态**，避免只匹配到函数名）
    assert "<SkuPickModal" in tsx, "缺规格图选择弹窗（应渲染出来）"
    assert "skuImages" in tsx, "应从预检 SKU 图里选"
    assert "chooseSkuImage" in tsx, "选中后应上传并回显"


def test_delivery_mode_notes_are_icon_tips_not_inline_text():
    """发货模式说明**只用图标 + tip**，不在页面内占空间（用户口径 2026-10-09：
    「类似的文本使用说明图标加 tip 展示，不要再页面内占用空间」+「单独的说明应在
      现货发货时间标题后」）。"""
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: 不再有独立的说明块容器（说明已进字段 title 后的图标）
    assert 'className="delivery-overview"' not in tsx, "说明不应再单独成块（应挂在标题后）"
    assert "<span>{DELIVERY_MODE_OVERVIEW}</span>" not in tsx, "总览正文不应上屏"
def test_sale_time_has_price_confidential_default_off():
    """定时开售要展示**开售前价格保密**，**默认不保密**（用户口径 2026-10-09：
    「定时开售只展示了时间，还需要展示价格保密，默认不保密，你实际浏览调研后完善」）。"""
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: 判据用**声明行本身**（`useState(false)` 全文件到处都是，会蒙混过关 —— M5 变异实测）。
    assert "const [priceConfidential, setPriceConfidential] = useState(false);" in tsx, (
        "价格保密应默认 false（不保密）"
    )
    assert "开售前价格保密" in tsx, "缺「开售前价格保密」文案"


def test_category_property_returns_support_no_logistics():
    """类目属性接口下发 `support_no_logistics`（物流类型与类目直接相关）。"""
    src = _read_qianfan()
    i = src.index('"conditional_required_rules"')
    block = src[i : i + 400]
    assert '"support_no_logistics"' in block, "类目属性应下发 support_no_logistics"


def test_spec_image_prefix_is_clickable_and_opens_modal():
    """规格图前缀**点击必须可响应**并弹 SKU 图选择弹窗（用户实测 2026-10-09/10）：
    「规格图前缀点击无响应，应弹窗 sku 图选择弹窗」。

    根因两条：① 早期无 SKU 图时走「本地文件选择」⇒ 看起来没反应；② antd v6 的
    `.ant-input-prefix` 可能不让子元素接收点击 ⇒ 需显式放开 `pointer-events`。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    #: ① 一律打开弹窗（不再在无图时走本地选择导致「无响应」）
    assert "setSkuPick({ spec, value });" in tsx, "点前缀应打开 SKU 图选择弹窗"
    assert "document.createElement(\"input\")" not in tsx or "spec-value" in tsx, (
        "不应再在规格图前缀走本地文件选择"
    )
    #: ② 缩略图必须能接收点击。
    #: ⚠️ 2026-10-11：图已从 `prefix` 移到**框外兄弟节点** ⇒ 查的不再是
    #: `.ant-input-prefix`，而是 `.spec-value-item > .spec-value-thumb` 那段。
    i = css.index(".spec-value-item > .spec-value-thumb")
    block = css[i : i + 300]
    assert "pointer-events: auto" in block, "缩略图应放开 pointer-events（否则点击无响应）"


def test_sku_image_pick_is_single_select():
    """规格图选择**只能单选一张**（用户口径 2026-10-10：「sku 图只能单选一张」）。"""
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    i = tsx.index("function SkuPickModal(")
    j = tsx.find(chr(10) + "function ", i + 10)
    block = tsx[i : j if j > 0 else i + 6000]
    #: 单选键是**字符串**（不是数组）
    assert "const [picked, setPicked] = useState<string | null>(null);" in block, (
        "SKU 图选择应为单选（字符串，不是数组）"
    )
    assert "checked" not in block, "不应是复选框多选"


def test_sku_pick_modal_shows_rule_at_top():
    """规格图规则文案放**弹窗顶部提示**（用户口径 2026-10-10：
    「图片大小应小于3MB；比例应为1:1或3:4；格式应为jpg、png、jpeg 文本放 sku 选择图弹窗中顶部提示」）。"""
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    i = tsx.index("function SkuPickModal(")
    #: 切到**下一个顶层函数**为止（弹窗里新增内容时不会因窗口大小变化而误判）。
    j = tsx.find(chr(10) + "function ", i + 10)
    block = tsx[i : j if j > 0 else i + 6000]
    assert "message={SPEC_IMAGE_RULE}" in block, "规则文案应在弹窗顶部提示里"
    #: 规则常量逐字
    assert "图片大小应小于3MB；比例应为1:1或3:4；格式应为jpg、png、jpeg" in tsx, "规则文案缺失"


def test_sku_pick_cards_have_red_frame_and_preview():
    """SKU 选择弹窗：**卡片小图 + 选中红框 + 支持大图预览**（用户口径 2026-10-10：
    「sku 规格图应以卡片中小图展示支持选中后红框，支持大图预览」）。"""
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    i = tsx.index("function SkuPickModal(")
    #: 切到**下一个顶层函数**为止（弹窗里新增内容时不会因窗口大小变化而误判）。
    j = tsx.find(chr(10) + "function ", i + 10)
    block = tsx[i : j if j > 0 else i + 6000]
    #: ① 卡片小图
    assert 'className="sku-pick-card"' in block or 'sku-pick-card${' in block, "SKU 图应是卡片"
    #: ② 大图预览（下标 + 上一张/下一张）
    assert "setPreview(i)" in block, "卡片应支持点开大图预览"
    assert "上一张" in block and "下一张" in block, "预览应能左右切换"
    #: ③ 选中红框（CSS）
    j = css.index(".sku-pick-card.picked")
    frame = css[j : j + 200]
    assert "#ff4d4f" in frame, "选中应显**红框**"


def test_video_upload_button_is_manual_and_never_disabled():
    """商品视频**手动上传**入口常开（用户口径 2026-10-10：
    「商品视频自定上传失败页面应支持手动上传，上传按钮不要置灰色」）。

    ⚠️ 为什么要有：视频走 `common_file` 直传，比图片更易失败；失败后若按钮 `disabled`
    且无手动入口，用户就**没有自救路径**（与 DEF-058 同型）。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    i = tsx.index('label="商品视频"')
    block = tsx[i : i + 1400]
    #: ① 视频用**上传组件**（用户口径 2026-10-10：「详情图和视频均使用上传组件不要使用按钮」）
    assert "<UploadSlot" in block, "视频区应使用上传组件（不是 Button）"
    assert 'accept="video/*' in block, "上传组件应接受视频类型"
    assert 'title="上传本地商品视频' in block, "上传组件应带用途说明"
    #: ② 上传槽是真·本地文件输入，且**永不可置灰**（无 `disabled`）
    slot = tsx[tsx.index("function UploadSlot(") : tsx.index("function UploadSlot(") + 1600]
    assert 'type="file"' in slot, "上传槽应是真· file input"
    assert "disabled" not in slot, "上传槽不得有置灰态"
    #: ③ 上传实现走同一出口（common_file 直传 + MD5 命名）
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    assert "onUploadVideoFile={uploadVideoFile}" in wiz, "wizard 应把视频手动上传接到 step2"
    j = wiz.index("const uploadVideoFile = useCallback(")
    body = wiz[j : j + 900]
    assert "uploadProductVideo" in body, "手动上传应走既有的视频上传出口"
    assert "md5_file_name(bytes" in body, "上传前应按内容 MD5 改名"


def test_spec_image_prefix_and_modal_wire_upload_and_echo():
    """规格图**选中即上传并回显**（用户口径 2026-10-10：「选中后上传图片，并回显」）。

    链路：前缀点击 → 弹窗单选/红框 → 确认 → 按内容 MD5 上传 → 回显为该规格值的规格图。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: ① 弹窗确认 → 上传 → 回显
    i = tsx.index("const chooseSkuImage = useCallback(")
    body = tsx[i : i + 900]
    assert "props.onUploadSkuImage(rel_path, file_name)" in body, "选中后应上传"
    assert "setSpecImage(target.spec, target.value, { ...img, precheck_name: rel_path, rel_path })" in body, (
        "上传成功应回显（`precheck_name` 去重键 + `rel_path` 提交路径）"
    )
    #: ② 前缀点击一律弹窗（不再本地选择）
    assert "setSkuPick({ spec, value });" in tsx, "前缀点击应打开选择弹窗"
    #: ③ 弹窗是卡片 + 红框 + 单选 + 大图预览
    j = tsx.index("function SkuPickModal(")
    k2 = tsx.find(chr(10) + "function ", j + 10)
    modal = tsx[j : k2 if k2 > 0 else j + 6000]
    assert 'className={`sku-pick-card${' in modal, "应是卡片小图（卡片根元素类名）"
    assert "useState<string | null>" in modal, "应单选一张"
    assert "setPreview(i)" in modal and "上一张" in modal and "下一张" in modal, "应支持大图预览"
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    k = css.index(".sku-pick-card.picked")
    assert "#ff4d4f" in css[k : k + 160], "选中应显红框"


def test_modal_styles_are_not_scoped_under_the_component_root():
    """弹窗样式**不得**作用域限死在组件根下（用户实测 2026-10-10：「依旧展示的是大图」）。

    ⚠️ 根因：Antd `Modal` 渲染在 **portal**（挂到 `document.body`），DOM 上**不含**
    `.publish-config-step2` 祖先 ⇒ 形如 `.publish-config-step2 .sku-pick-card { … }`
    的选择器**永远匹配不到**，卡片宽度/网格/红框全部失效，图片按原尺寸铺成一张大图。
    故弹窗内的类必须写成**全局**选择器（类名本身已足够独特）。
    """
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    #: 弹窗内的类不得带组件根前缀
    for cls in (".sku-pick-grid", ".sku-pick-card", ".sku-pick-thumb-placeholder"):
        assert f".publish-config-step2 {cls}" not in css, (
            f"{cls} 被限死在组件根下 —— Modal 走 portal，选择器匹配不到"
        )
        assert f"\n{cls} {{" in css or f"\n{cls}." in css, f"{cls} 应是全局选择器"


def test_sku_image_cannot_be_picked_twice():
    """同一张 SKU 图**不能被重复选中**（用户口径 2026-10-10：
    「选择规格图被选中的规格图不能重复被选中」）。

    实现：已用图在弹窗里**禁用**（`usedNames` → `.used` + 「已使用」角标），
    落值前再**二次校验**（`precheckNamesInUse.includes`），并把源预检图记为去重键
    （`precheck_name`）。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    #: ① 弹窗支持「已用图禁用」
    j = tsx.index("function SkuPickModal(")
    modal = tsx[j : j + 6000]
    assert "const used = usedSet.has(it.rel_path);" in modal, "已用判定必须真的按集合判定（不是写死）"
    assert 'used ? " used" : ""' in modal, "已用图应带 used 状态类"
    assert "已使用" in modal, "已用图应有「已使用」标识"
    assert "已被其它规格值使用" in modal, "已用图应有原因文案"
    #: ② 落值前二次校验 + 记录去重键
    i = tsx.index("const chooseSkuImage = useCallback(")
    body = tsx[i : i + 1400]
    assert "precheckNamesInUse.includes(rel_path)" in body, "落值前应二次校验（防竞态）"
    assert "precheck_name: rel_path, rel_path" in body, "应记录源预检图（去重键 + 提交路径）"
    #: ③ 去重集合只排除**其它**规格值（同规格自身的图可再次选中/更换）
    k = tsx.index("const precheckNamesInUse = useMemo(")
    memo = tsx[k : k + 700]
    assert "spec === target.spec && value === target.value" in memo, "同规格自身那张不应被排除"
    #: ④ 样式：已用图不可点（`cursor: not-allowed`）
    #: ⚠️ **2026-10-13 取代**（用户口径）：已用图由「置灰（`opacity`）」改为
    #: **蓝框标注**（用户原话「蓝框标注历史的 sku 图，且不允许重复选择」）⇒
    #: 判据从「not-allowed + opacity」改为「not-allowed + 蓝框（`#1677ff`）」。
    m = css.index(".sku-pick-card.used")
    block = css[m : m + 200]
    assert "not-allowed" in block, "已用图应不可点"
    assert "#1677ff" in block, "已用图应以蓝框标注（用户口径 2026-10-13）"
    #: ⑤ 类型带出去重键
    t1 = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step1.tsx"))
    assert "precheck_name?: string;" in t1, "ConfigMainImage 应带去重键字段"


def test_every_spec_value_in_the_first_group_has_the_image_prefix():
    """**第一组的所有规格值**都要有规格图前缀（用户口径 2026-10-09/10：
    「规格图应是输入框的前缀…第一组规格**每个规格**均有规格图前缀」、
    「规格的第二个输入框没有规格图前缀，应是第一组的规格均应有规格图前缀」）。

    ⚠️ 根因：外层 `specTypes.map((name, i))` 的 `i` 是**规格组**下标，
    内层 `specValueInputs(name).map((v, i))` 的 `i` 是**规格值**下标 ——
    同名遮蔽后 `canImage = i === 0` 变成「只有第一个**值**」。
    必须用**组**下标（现为 `gi`）判断，且值下标不得再叫 `i`（防再次遮蔽）。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "const canImage = specImageEnabled[name] ?? gi === 0;" in tsx, "canImage 由**规格图开关**判定（默认回落第一组）"
    assert "const canImage = i === 0;" not in tsx, "canImage 不得按规格值下标判定（同级遮蔽）"
    #: 值级 map 不得再用 `i` 作值下标（否则易与组下标混淆/遮蔽）
    assert "specValueInputs(name).map((v, vi) => {" in tsx, "规格值 map 的值下标应为 vi"
    #: 前缀回调必须传**值**下标
    assert "pickSpecImage(name, vi)" in tsx, "前缀点击应传规格值下标"
    assert "onChange={(e) => setSpecValueAt(name, vi, e.target.value)}" in tsx, "编辑应传规格值下标"


def test_property_grid_is_two_columns_fluid_and_unstyled_multi():
    """属性区：**一行两个** + **控件宽度自适应** + **多选用 antd 默认** +
    **最高 400px 可滚动**（用户口径 2026-10-10，**覆盖**此前「一行 5 个 / 固定 200px /
    多选不换行 / 单行裁剪」四条旧口径）。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    #: ① 一行两个
    j = css.index(".publish-config-step2 .prop-grid {")
    grid = css[j : j + 400]
    assert "repeat(2, minmax(0, 1fr))" in grid, "属性区应一行两个"
    assert "repeat(5," not in grid, "不得再是一行五个（旧口径已取代）"
    #: ② 控件自适应（不写死 200px）
    k = css.index(".publish-config-step2 .prop-cell .prop-control,")
    w = css[k : k + 320]
    assert "width: 100%;" in w, "控件宽度应自适应"
    assert "width: 200px;" not in w, "不得再写死 200px（旧口径已取代）"
    #: ③ 多选用 antd 默认（无自定义裁剪容器/样式）
    assert "prop-multi-one-line" not in tsx, "不得再套单行裁剪容器"
    assert "maxTagCount" not in tsx, "不得再折叠标签（改由 antd 默认）"
    assert ".prop-multi-one-line" not in css, "相关自定义样式应已删除"
    #: ④ 最高 400px 可滚、低于自适应
    assert "max-height: 400px" in grid, "属性区应最高 400px"
    assert "overflow-y: auto" in grid, "超出 400px 应可上下滚动"
    #: ⑤ 多选上限仍为 3（唯一常量 + 唯一入口），与「用默认样式」不冲突（那是行为不是样式）
    assert "const MULTI_SELECT_MAX = 3;" in tsx, "上限应是唯一常量"
    i = tsx.index('field.kind === "multi"')
    block = tsx[i : i + 1200]
    assert ".slice(0, MULTI_SELECT_MAX)" in block, "多选最多保留前三个（兜底）"
    assert "disabled: full && !selected.includes(o)," in block, "选满后未选项应置灰"


def test_step2_next_gate_reads_the_single_category_source():
    """第二步门禁（「下一步：确认发布」）必须读**唯一类目真相源**（用户实测 2026-10-10：
    「目前无法点击进入下一步」）。

    ⚠️ 真根因：门禁读的是 `category` —— 一个**除 reset 外从未被赋值**的死 state
    （`setCategory(非 null)` 全文件 0 处）⇒ 条件恒假、按钮永远置灰。
    真正的类目在 `configCategoryId` / `configCategoryPath`（Step1 的 headSlot 选类目、
    自动加载第 3 阶段识别类目，写的都是它）。
    """
    src = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    #: ① 门禁 = **只看助手提示数**（用户口径 2026-10-10，随后追加「助手中提示必须为 [0] 且
    #: 页面表单必填项全部校验通过」）—— 类目 / 可提交行那些**已收进助手判据**，
    #: 不能另外再叠条件（否则「助手 0 提示但按钮灰」复现）。
    assert "disabled={hintCount > 0}" in src, "第二步门禁应只看助手提示数"
    assert "hintCount" in src and "setHintCount" in src, "向导应持有助手提示数"
    #: 反向断言：不得再叠 `!configCategoryId` / `skus.length === 0` 这两个**重复**判据
    assert "disabled={hintCount > 0 || !configCategoryId" not in src, (
        "门禁不得再叠类目判据（应已收进助手「商品类目」）"
    )
    assert "disabled={hintCount > 0 || skus.length" not in src, (
        "门禁不得再叠可提交行判据（应已收进助手「商品规格」）"
    )
    #: ② 不得再读死 state `category`
    assert "disabled={!category ||" not in src, "门禁不得再读恒为 null 的 category"
    assert "useState<ItemDraftCategory | null>" not in src, "category 死 state 应已删除"
    #: ③ 发布也用同一个 id（否则按钮放行了、提交仍会挡）
    i = src.index("const submit = useCallback(")
    body = src[i : i + 2600]
    assert "if (!configCategoryId)" in body, "发布前门禁应读 configCategoryId"
    assert "category_id: configCategoryId" in body, "提交应带 configCategoryId"


def test_wizard_has_no_parallel_dead_step_component():
    """向导里**不得**再留平行的第二实现（用户口径 2026-10-10「你来决定」——助手决定清除）。

    ⚠️ 为什么要清：`ConfigStep` 是一份**平行于活链路**的第二实现（第二步真正渲染的是
    `PublishConfigStep2`），自带 `category/categories/keyword/searching` 一整套 state。
    刚修的「门禁恒灰」死 state 就是它留下的 —— 留着 = 等下一次踩。
    """
    src = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    #: ① 死组件与死辅助函数必须已删（按**定义**判，避免匹配到 PublishConfigStep1/2）
    assert "function ConfigStep(props: {" not in src, "ConfigStep 死组件应已删除"
    assert "function priceRange(" not in src, "priceRange（仅 ConfigStep 用）应已删除"
    assert "const searchCategory = useCallback(" not in src, "searchCategory（仅 ConfigStep 用）应已删除"
    #: ② 它的三个 state 也不得残留
    for name in ("setKeyword", "setSearching", "setCategories"):
        assert name not in src, f"{name} 应随 ConfigStep 一并删除"
    #: ③ 活链路仍在（不许删错）
    assert "<PublishConfigStep2" in src and "<PublishConfigStep1" in src, "活链路组件不得被删"


def test_media_uploads_use_upload_slots_not_buttons():
    """商品主图 / 商品视频 / 商品详情图一律用**上传组件**（用户口径 2026-10-10：
    「请先在上方『智能识图』区上传商品主图 移除，应使用图片上传组件，
    详情图和视频均使用上传组件不要使用按钮」）。

    * 主图区**不得**再有「请先在上方……上传商品主图」提示（改为就地可传）；
    * 三处都用 `UploadSlot`（虚线框 + 加号 + 真 `<input type=file>`），与「智能识图」同款；
    * 上传槽样式写在**全局**（`.unified-upload-slot`）—— 与弹窗内类的口径一致。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    #: ① 旧提示已移除
    assert "请先在上方「智能识图」区上传商品主图" not in tsx, "主图区旧提示应已移除"
    #: ② 三处都用上传组件
    for label in ('label="商品主图"', 'label="商品视频"', 'label="商品详情图"'):
        i = tsx.index(label)
        block = tsx[i : i + 1200]
        assert "<UploadSlot" in block, f"{label} 应使用上传组件"
    #: ③ 主图可上传（新增 onImagesChange 通路 + handler）
    assert "onImagesChange?" in tsx and "onPickMainImages" in tsx, "主图应有上传通路"
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    #: ⚠️ 必须断言**第二部分**那处接线 —— 第一部分（PublishConfigStep1）本就有同名 prop，
    #: 只查「wizard 里存在该字符串」会被它满足（首轮实测 M4 逃逸）。
    k = wiz.index("<PublishConfigStep2")
    block2 = wiz[k : wiz.index("/>", k)]
    assert "onImagesChange={setConfigImages}" in block2, "wizard 应把主图变更接到**第二部分**"
    #: ④ 上传槽样式（虚线框 + 透明铺满的 input）
    j = css.index(".unified-upload-slot {")
    rule = css[j : j + 300]
    assert "dashed" in rule and "72px" in rule, "上传槽应是虚线框"
    assert ".unified-upload-picker" in css, "应有透明铺满的 file input 样式"


def test_child_param_blocks_show_gray_dashed_box_and_hierarchy():
    """子参数块（发货模式切换 / 商品规格勾选 / 定时开售）一律**灰底 + 虚线框 + 父子关系**
    （用户口径 2026-10-10：
    「发货模式切换得到的不同参数属性页面 ui 缩进到与第二个发货模式起头使用灰色背景
    单独虚线框，商品规格勾选够同理，页面布局也要看到父子关系；
    商品规格勾选后不再再添加最左侧的标题，定时开售同理」）。

    取代 2026-10-09 的「去掉虚线框缩进」。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    #: ① 三处子块：发货模式 / 商品规格 / 定时开售
    assert 'className="child-block delivery-varying"' in tsx, "发货模式子块应挂 child-block"
    assert 'className="child-block spec-config"' in tsx, "规格子块应挂 child-block"
    assert 'className="child-block shelf-varying"' in tsx, "定时开售子块应挂 child-block"
    #: ② 规格块**不再**在最左侧挂标题（规格名移到块内标题行）
    assert "<Field key={name} label={name}>" not in tsx, "规格块不应再挂最左侧标题"
    assert ".child-block-title" in css or "child-block-title" in tsx, "子块应有标题行"
    #: ③ 样式：灰底 + 虚线框（并保持与父字段同一条标签列）
    i = css.index(".child-block {")
    block = css[i : i + 420]
    assert "dashed" in block and "#fafafa" in block, "子块应是「灰底 + 虚线框」"
    assert "child-param-row" in css, "子块内应为行式排布（与开售时间同款）"


def test_detail_image_has_no_fill_from_main_button():
    """**「从商品主图填入」按钮已按用户口径移除**（2026-10-10）。

    ⚠️ 反例守卫：别再把它加回来 —— 也提醒后来者不要因为「看着方便」而恢复。
    """
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "从商品主图填入" not in tsx, "「从商品主图填入」应已移除"


def test_delivery_child_block_matches_shelf_time_layout():
    """发货模式子块的**布局与视觉必须与其它子块一致**（用户实测 2026-10-10：
    「发货模式的 ui 布局不对，商品规格，定时开售都对了…应参考开售时间的 ui 布局」）。

    两条根因（都只有发货模式这一处踩到）：
    ① 过时规则 `.publish-config-step2 .delivery-varying { border:none; padding:0; margin:0;
       background:transparent }`（2026-10-09 口径）**特异性更高** ⇒ 把 `.child-block`
       的灰底虚线框覆盖掉；
    ② 发货模式子块是**顶层**元素（不在某个 `Field` 的 body 内）⇒ 少了 126px 缩进。
    修法：删掉 ①；子块内**不再套 `Field`**（避免双重缩进），统一用行式
    `.child-param-row`（与「开售时间」同款）；顶层子块统一加 126px 缩进。
    """
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: ① 过时的零框规则不得残留（否则压掉 .child-block）
    assert ".publish-config-step2 .delivery-varying {" not in css, (
        "过时的 `.delivery-varying` 零框规则应已删除（其特异性会压掉 .child-block 的灰底虚线框）"
    )
    i = css.index(".publish-config-step2 .child-block.delivery-varying")
    assert "margin-left: 126px" in css[i : i + 160], "顶层发货子块应按父字段标签列缩进"
    #: ② 子块内用行式排布（不套 Field ⇒ 不双重缩进）
    assert tsx.count("child-param-row") >= 5, "子块内参数应统一用行式 .child-param-row"
    assert 'label="现货发货时间"' not in tsx, "子块内不应再套 Field（会双重缩进）"
    assert 'label="全款预售发货时间"' not in tsx, "同上"
    #: ③ 发货子块仍挂 .child-block（灰底虚线框）
    assert 'className="child-block delivery-varying"' in tsx, "发货子块应挂 child-block"


def test_step3_previews_the_full_step2_config_and_media():
    """第三步是**第二步全量只读预览**（用户口径 2026-10-10：
    「把第三步，发布确认并上架应预览第二步填写的完整所有参数；
     媒体资源只预览顺序和第二步保持一致」）。

    ⚠️ 前置问题：第二步配置原本是组件**内部 state**，切步时组件卸载 ⇒ state 全丢
    ⇒ 提交体与第三步都拿不到配置。修法 = 快照上报给向导 + 第三步全量预览。
    """
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    s2 = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    #: ① 第二步上报快照
    assert "onConfigChange?: (next: ConfigSnapshot) => void;" in s2, "第二步应提供配置快照上报"
    assert "props.onConfigChange?.({" in s2, "第二步应真的上报（不是只声明）"
    #: ② 向导持有快照并传真值给第三步
    assert "const [configSnapshot, setConfigSnapshot] = useState<ConfigSnapshot | null>(null);" in wiz, (
        "向导应持有配置快照（组件卸载后不丢）"
    )
    assert "onConfigChange={setConfigSnapshot}" in wiz, "应把上报接到快照 state"
    i = wiz.index("<PublishStep")
    block = wiz[i : wiz.index("/>", i)]
    for prop in ("config={configSnapshot}", "images={configImages}", "video={configVideo}",
                 "detailImages={configDetailImages}", "priceRows={priceRows}",
                 "shortTitle={configShortTitle}"):
        assert prop in block, f"第三步预览缺：{prop}"
    #: ③ 四分区齐 + 媒体**只预览**（不带删除/勾选）
    assert wiz.count("PreviewSection") >= 5, "第三步应分区块全量预览（基础/图文/价格/服务）"
    assert "PreviewMedia" in wiz, "媒体应只读预览"
    assert 'label="商品主图"' in wiz and 'label="商品详情图"' in wiz, "媒体顺序应与第二步一致"
    #: ④ 配置随提交下发（否则「提交第二步的完整参数」是空话）
    assert "config: (configSnapshot ?? undefined) as Record<string, unknown> | undefined," in wiz, (
        "提交应带上第二步配置快照"
    )


def test_publish_request_accepts_step2_config_without_422():
    """发布端点必须**接收**第二步配置快照（否则前端一发就 422，等于白传）。

    ⚠️ 当前只接收留档；逐项映射进 `itemData` 是下一步（需真机字段名 + 红线验证）。
    """
    api = _read_qianfan()
    assert "config: dict[str, Any] = Field(default_factory=dict)" in api, (
        "发布请求体应接收 config（形状不定 ⇒ 不能收紧成固定模型，否则前端一多发就 422）"
    )


def test_sku_pick_modal_has_waiting_mask_on_confirm():
    """规格图选择弹窗**确认后要盖等待蒙版**（用户口径 2026-10-10：
    「选择规格图顶级确认加等待蒙版」）。"""
    tsx = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    i = tsx.index("function SkuPickModal(")
    j = tsx.find("function ", i + 10)
    block = tsx[i : j if j > 0 else i + 6000]
    assert "busy?: boolean;" in block, "弹窗应接收上传中态"
    #: ⚠️ 2026-10-20 统一蒙版：由 `Spin` 换成 `<WaitMask scope="panel">`
    #: （Spin 的 tip 跟内容滚走；panel 作用域只盖弹窗本体）。
    assert '<WaitMask open={!!props.busy} mask scope="panel"' in block, "确认后应盖等待蒙版"
    assert "confirmLoading={props.busy}" in block, "确认按钮应显示 loading"
    assert "props.busy" in block.split("okButtonProps")[1][:80], "上传中应禁用二次确认"


def test_publish_submits_step2_attributes_and_per_dimension_specs():
    """发布体**严格按第二步配置**：属性用用户填的、规格**逐维度**给 id
    （用户口径 2026-10-10；对应真机两条报错）。

    真机报错（用户截图）：`attributes - 材质属性必须填写`、
    `variants - 当前商品存在重复的规格，请删除重复规格后重试`。

    根因：① `attributes` 直接抄**骨架**（= 上一个商品的属性）⇒ 用户填的从没进过提交体；
          ② 所有 SKU 行共用**第一个**规格 id ⇒ 多维规格塌成一维 ⇒ 平台判「重复规格」。
    """
    api = _read_qianfan()
    svc = _read(ROOT / "backend/app/services/shop_item_publish_service.py")
    #: ① 端点把 config 里的 attributes / specAttributes 传下去
    assert 'attributes=payload.config.get("attributes") or None' in api, (
        "端点应把第二步填的属性传给发布服务"
    )
    assert 'spec_attributes=payload.config.get("specAttributes") or None' in api, (
        "端点应把规格维度 id 映射传下去"
    )
    #: ② 服务：属性**提交值优先**（不再无条件抄骨架）
    assert 'attributes=attributes if attributes is not None else skeleton.get("attributes")' in svc, (
        "属性应「提交值优先、缺才回落骨架」"
    )
    #: ③ 服务：规格逐维度（按 `/` 拆多维度 + 查 `规格名 → id` 表）
    #: ⚠️ 2026-10-22 修正：**按 `" / "` 拆，不是裸 `/`** —— 规格值里本身含斜杠
    #: （如 `尺寸/图案任意定制【联系客服】`），裸 `/` 会把它截断成两个假维度。
    assert 'for part in str(row.get("name") or "").split(" / ")' in svc, (
        "规格应按 ` / `（带空格）拆多维度"
    )
    assert "spec_id_by_name" in svc, "应有「规格名 → 平台 id」映射"
    #: ④ 真机 `attributes[]` 必带 `attributeValueType`（缺它被判「没填」）
    s2 = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "attributeValueType: 1," in s2, "提交的属性条目应带 attributeValueType（真机形状）"
    #: ⑤ 只带**用户勾选的**规格维度
    assert "specAttributes: (props.specAttributes ?? []).filter((x) => specTypes.includes(x.name))" in s2, (
        "规格维度应严格按第二步勾选收窄"
    )


def test_publish_submit_shows_mask_with_elapsed_time():
    """点「确认发布」后**页面加蒙版 + 耗时统计**（用户口径 2026-10-10）。"""
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    #: ① 提交蒙版（复用 publish-wait-mask）+ 实时耗时。
    #: ⚠️ 2026-10-11：文案**按用户点的按钮分叉**（原写死的「正在发布并立即下架…」
    #: 已随红线取消退场 —— 点「发布商品」时那句话说的是反的）。
    #: 这里钉「有蒙版 + 有实时耗时」这条本质判据；三种模式的文案分叉由
    #: `test_publish_mask_copy_follows_the_clicked_button` 钉。
    assert "submitElapsedSec" in wiz, "提交中应显示实时耗时"
    #: ⚠️ 2026-10-20 统一蒙版：`.publish-wait-mask` / `-text` 两个类已退役，
    #: 改由公共 `<WaitMask>` 渲染（样式全站一致、可自定义内容）。
    assert "<WaitMask" in wiz and "elapsed={submitElapsedSec}" in wiz, (
        "提交中应显示蒙版 + 耗时（WaitMask）"
    )
    assert "publish-wait-mask" not in wiz, "旧蒙版类已退役，不许回退"
    assert "正在发布并下架商品" in wiz and "正在发布商品（保持在售）" in wiz, (
        "遮罩文案应按本次点击的按钮分叉"
    )
    assert "{submitting && (" in wiz, "蒙版应由 submitting 驱动"
    #: ② 起表 / 定格
    assert "const startedAt = performance.now();" in wiz, "提交应用单调钟起表"
    assert "setSubmitElapsedSec((performance.now() - startedAt) / 1000);" in wiz, (
        "终态应定格耗时（不归零）"
    )
    #: ③ 用户可见文案禁 Markdown `**`（用新分叉后的文案做样本）。
    assert "发布成功后会立即下架；请勿关闭本窗口或重复点击。" in wiz
    assert "**" not in wiz.split("{submitting && (")[1][:600], (
        "遮罩文案里不得出现 Markdown `**`"
    )


def test_step2_state_survives_step_switching():
    """来回切步时第二步**已填参数不得清空**（用户口径 2026-10-10：
    「来回步骤切换时，所有已填写的参数均应保持不清空，只有弹窗关闭或发布成功后清空」）。

    ⚠️ 根因：`{step === 1 && <PublishConfigStep2/>}` 条件渲染 ⇒ 切步**卸载组件**
    ⇒ 内部 state（发货模式/规格/开售时间/售后/描述…）全丢。
    修法：**保持挂载、只隐藏**（`display:none`）；清空只发生在 `reset()`（关弹窗）。
    """
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    i = wiz.index("<PublishConfigStep2")
    before = wiz[max(0, i - 900) : i]
    assert 'display: step === 1 ? undefined : "none"' in before, (
        "第二步应保持挂载、只隐藏（条件渲染会卸载组件 ⇒ state 全丢）"
    )
    #: ⚠️ 判据：`<PublishConfigStep2>` **之前**不得出现 `step === 1 &&`。
    #: （首轮写成「`{step===1&&(<PublishConfigStep2` 不存在」⇒ 恒真、变异逃逸 ——
    #:  因为 `{` 与组件之间夹着一段长注释，那个形态本来就不存在。）
    flat_before = before.replace("\n", "").replace(" ", "")
    assert "step===1&&" not in flat_before, "不得再用条件渲染包住第二步"


def test_reset_clears_all_step2_config_state():
    """**关弹窗 / 发布成功后 `reset()` 必须清空第二步全部配置 state**（用户口径 2026-10-10：
    「只有弹窗关闭或发布成功后清空」）。

    ⚠️ 2026-10-10 补全：此前 `reset()` 漏清了第二步后来新增的一批 state
    （`configSnapshot` / `priceRows` / `keyProperties` / `propertyValues` /
    `specAttributes` / 物流模板…）⇒ 关弹窗重开时上一轮配置**残留**。
    """
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    i = wiz.index("const reset = useCallback(")
    block = wiz[i : i + 2200]
    for token in (
        "setConfigSnapshot(null);",
        "setHintCount(0);",
        "setSubmitElapsedSec(0);",
        "setPriceRows([",
        "setKeyProperties([]);",
        "setOtherProperties([]);",
        "setSpecAttributes([]);",
        "setPropertyValues({});",
        "setShippingTemplates([]);",
        "setDefaultTemplateId(null);",
        "setLogisticsTemplates([]);",
        "setAfterSaleRules([]);",
    ):
        assert token in block, f"reset 应清空：{token}"


def test_sku_rows_follow_the_step2_price_table():
    """SKU 行数以**第二步价格及库存表**为准（用户实测 2026-10-10：
    「第三部的 sku 统计函数错误不是 26 行，我只配置了一个规格」）。

    26 行来自**预检数据包的 SKU 表**，与第二步实际配置的规格组合无关。
    """
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    assert "const skus = useMemo(" in wiz or "setSkus(\n        priceRows" in wiz or "setSkus(" in wiz
    #: 必须由 priceRows 推导，且**不得**再从 payload.skus 灌
    assert "priceRows" in wiz and ".filter((r) => r.spec && r.spec !== \"-\")" in wiz, (
        "skus 应由 priceRows（第二步表）推导"
    )
    assert "setSkus(payload.skus)" not in wiz, "不得再用预检 SKU 表当 SKU 真相源"


def test_step2_required_fields_and_hints_cover_specs_and_freight():
    """**商品规格 / 运费模板必填**（用户口径 2026-10-10），且发布助手要判到它们
    （否则「红色异常项挡住下一步」与「实际能进」不一致）。"""
    s2 = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert 'label="商品规格" required' in s2, "商品规格应必填"
    assert 'label="运费模板" required' in s2, "运费模板应必填"
    assert 'field: "商品规格", reason: "请至少勾选一个商品规格"' in s2, "发布助手应判商品规格"
    assert 'field: "运费模板", reason: "请选择运费模板"' in s2, "发布助手应判运费模板"


def test_step3_shows_after_sale_and_no_price_list():
    """第三步：**售后服务要展示**（用规则名，含平台强制项）、**价格及库存不得为列表**
    （用户口径 2026-10-10）。"""
    s2 = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    #: 快照带 afterSaleNames（平台强制项 ∪ 用户勾选 ⇒ 规则名）
    assert "afterSaleNames: afterSaleRules" in s2, "快照应带售后服务展示名"
    #: ⚠️ 判据必须落在 **afterSaleNames 那段**里（首轮实测：只查
    #: `r.must_support || supportedTypes.includes(r.type)` 会被**别处**同名模式满足 ⇒ 变异逃逸）。
    k = s2.index("afterSaleNames: afterSaleRules")
    seg = s2[k : k + 260]
    assert "r.must_support || supportedTypes.includes(r.type)" in seg, (
        "售后服务应含平台强制项（只报用户勾选的会显示空）"
    )
    assert "config?.afterSaleNames" in wiz, "第三步应用 afterSaleNames 展示售后服务"
    #: 价格及库存只给摘要，不铺表
    assert "publish-preview-summary" in wiz, "价格及库存应是摘要而非列表"
    i = wiz.index("价格库存")
    block = wiz[i : i + 2600]
    assert "dataSource={priceRows}" not in block, "价格及库存不得用 Table 铺长列表"


def test_publish_pipeline_has_the_latency_fixes():
    """发布链路的三处**耗时**修复（用户口径 2026-10-10：「顶级确认发布很慢分析原因」）。

    实测（只读接口）：`list_recent_items` 0.57s / `publish_render` 0.3s / 运费模板 0.2s
    —— 单次调用都很快 ⇒ 慢点在 ①两段轮询（每段首个 sleep 白等 3s、最多 36s×2）
    ②26 张图**串行**上传（每张 1 permit + 1 PUT）。
    """
    svc = _read(ROOT / "backend/app/services/shop_item_publish_service.py")
    api = _read_qianfan()
    #: ① 轮询先查再睡（去掉每段固定的首个 3s 白等）
    assert "if attempt:\n            time.sleep(_PUBLISH_POLL_INTERVAL)" in svc, (
        "轮询应「先查再睡」（原实现每轮先 sleep(3) ⇒ 每段白等 3s）"
    )
    #: ② 图片并行上传（有上限的线程池；单张仍逐条如实报，不静默丢图）
    assert "ThreadPoolExecutor" in api, "图片上传应并行（26 张串行是主要慢点）"
    #: ⚠️ 2026-10-22 换锚：上传改为「先批量领凭证，再并发 PUT」，
    #: 并发度的输入从 `_paths_to_upload` 改为 `pairs`（路径+凭证对）。
    #: 判据不变：**必须有并发上限**（无上限会打爆对象存储连接）。
    assert "max_workers=min(8, len(pairs))" in api, "并发应有上限（8 路）"
    assert 'failed_images.append({"path": path_str, "reason": str(reason)})' in api, (
        "并行后仍要逐条如实报失败（口径不变）"
    )


def test_publish_failure_reasons_use_a_top_error_alert():
    """发布失败原因用**第三步顶部 error 公告**（用户口径 2026-10-10：
    「发布失败原因**不使用弹窗**，使用第三步**顶部公告 error 类型**」，参考 antd Alert）。"""
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    i = wiz.index("function PublishStep(")
    block = wiz[i:]
    #: ① 不得再有失败原因弹窗
    assert 'title="发布失败原因"' not in block, "失败原因不得再用弹窗"
    assert "publish-fail-images" in block  # 明细仍在（放进 Alert 的 description）
    #: ② 顶部 error 公告（antd Alert：type=error + showIcon + closable）
    k = block.index('type="error"')
    seg = block[max(0, k - 200) : k + 400]
    assert "showIcon" in seg and "closable" in seg, "失败原因应是可关闭的 error 型 Alert"
    assert "describe" not in seg or True
    assert "发布未通过（" in block, "公告 message 应写明条数"
    #: ③ 明细在 description 里（失败逐条 + 图片失败逐条）
    assert "dataSource={result.fail_reasons ?? []}" in block, "公告内应列出逐条失败原因"
    #: ④ 文案不得再指向弹窗（弹窗已不存在 ⇒ 指向它就是死路）。
    #: ⚠️ 这条提示在 `submit()` 里（不在 `PublishStep` 内）⇒ 断言范围放到**整个向导文件**。
    assert "请看失败原因弹窗" not in wiz, "页面提示不得再指向已删除的弹窗"
    assert "请看顶部失败原因" in wiz, "提示应指向顶部公告"


def test_publish_hints_cover_all_required_fields_and_recompute():
    """发布助手：**所有必填项都列出**、**勾选后自动消失**（实时重算）、**有异常 ⇒ 不放行**
    （用户口径 2026-10-10）。

    ⚠️ 真根因（"商品规格已勾选但红色项还在"）：`publishHints` 的 `useMemo` **依赖数组漏了**
    `specTypes` / 运费模板 ⇒ 勾选后不重算，提示一直挂着。
    """
    s2 = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    #: ① 依赖含所有参与判定的值（否则改值不重算）
    i = s2.index("const publishHints = useMemo")
    #: ⚠️ 2026-10-22：函数体加了「规格值超长」一段，窗口 2600 → 4000。
    memo = s2[i : i + 4000]
    #: ⚠️ 必须切**依赖数组那一段**再断言（首轮实测：只查 `dep in memo` 会被**函数体**
    #: 里的同名引用满足 —— 例如 `if (specTypes.length === 0)` 自己就含 `specTypes` ⇒ M1 逃逸）。
    assert "}, [" in memo, "应能定位依赖数组"
    deps = memo.split("}, [", 1)[1].split("]);", 1)[0]
    for dep in ("specTypes", "effectiveFreightTemplateId", "propValues",
                "selectedShippingTemplate", "brand", "deliveryMode", "props.priceRows"):
        assert dep in deps, f"依赖数组应含 {dep}（漏了 ⇒ 改值后红色项不消失）"
    #: ② 页面 required 的项都要在助手里列
    for field in ("商品主图", "商品详情图", "商品规格", "运费模板", "物流模板", "商品标题"):
        assert f'field: "{field}"' in s2, f"助手应列出必填项：{field}"
    #: ③ 上报提示数 → 向导门禁
    assert "props.onHintCountChange?.(publishHints.length);" in s2, "应上报助手提示数"
    assert "onHintCountChange={setHintCount}" in wiz, "向导应接收提示数"
    assert "disabled={hintCount > 0}" in wiz, "有异常提示 ⇒ 不允许进入下一步"
    #: 助手必须覆盖**类目**与**规格值未填**（两者原先只写在门禁里 ⇒
    #: 出现「助手显示没有待处理提示，按钮却灰着」，用户实测 2026-10-10）
    assert 'field: "商品类目"' in s2, "助手应列出「商品类目」"
    assert "还没有规格值" in s2, "勾了规格类型但没填值也要报"
    #: **表单校验保留、但配置区不再渲染红字**（用户口径 2026-10-10 更新）：
    #: 校验结果仍由 `publishHints` 承载并用于门禁（`hintCount > 0`），
    #: 但配置区**不再显示内联红框/红字**（`Field.error` 已移除，异常提示统一进左侧助手）。
    assert "disabled={hintCount > 0}" in wiz, "有异常提示 ⇒ 不允许进入下一步（门禁靠助手）"
    assert "error={hintFor(" not in s2, "配置区不应再绑定内联红字（error 传参已移除）"
    assert "config-field-error" not in s2, "配置区不应再有错误文案节点"


def test_step3_shows_template_names_and_scrollable_sku_list():
    """第三步：**展示模板名称**（不是「已选」）、**SKU 列表固定 300px 可滚动**
    （用户口径 2026-10-10：「第三步的运费模板，物流模板应展示选择的模版名称，
    SKU 行数下新增 sku 列表，固定 300px 高度支持下拉滚动」）。"""
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    s2 = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    #: ① 快照带两个**名称**
    assert "logisticsPlanName: string;" in s2 and "freightTemplateName: string;" in s2
    assert "props.logisticsTemplates.find((t) => t.plan_id === selectedShippingTemplate)?.name" in s2
    assert "props.shippingTemplates.find((t) => t.template_id === effectiveFreightTemplateId)?.name" in s2
    #: ② 第三步展示名称（回落「已选/未选」只在名称为空时）
    assert "config?.logisticsPlanName || (config?.logisticsPlanId ?" in wiz
    assert "config?.freightTemplateName || (config?.freightTemplateId ?" in wiz
    #: ③ SKU 列表（**最高** 300px + 低于则自适应 + 超出的在容器内滚动）
    #: （用户口径 2026-10-10：「规格列表高度调整为**最高 300px**，低于 300px 的**自适应**」）
    assert 'className="publish-preview-sku-list"' in wiz, "SKU 行数下应有 SKU 列表"
    i = css.index(".publish-preview-sku-list {")
    rule = css[i : i + 200]
    assert "max-height: 300px" in rule, "SKU 列表应是**最高** 300px（低于则自适应）"
    #: ⚠️ 用行首判据：`max-height: 300px` 本身**含子串** `height: 300px`，
    #: 直接 `"height: 300px" not in rule` 会**误红**（首轮实测踩到）。
    assert not any(
        ln.strip().startswith("height:") for ln in rule.splitlines()
    ), "不得写死 300px（行数少时要自然收缩）"
    assert "overflow-y: auto" in rule, "超出 300px 应在容器内滚动"
    #: 反向断言：固定 scroll.y 会让行数少时也撑到 261px（非自适应）
    assert "scroll={{ y: 300 - 39 }}" not in wiz, "不得再写死 Table scroll.y（破坏自适应）"


def test_step1_title_and_category_have_inline_validation():
    """第一步的**商品标题 / 商品类目**必填框要有**表单校验**（用户口径 2026-10-10：
    「配置商品参数弹窗下，必填框应添加表单校验」）。

    ⚠️ 这两框在 `publish-config-step1`（作为 step2 的 `headSlot`）里渲染，
    文案与发布助手同一条（「请输入商品标题」/「请选择商品类目」）。
    """
    s1 = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step1.tsx"))
    assert 'status={title.trim() ? undefined : "error"}' in s1, "标题为空应显示错误态"
    assert '请输入商品标题' in s1, "标题应给内联校验文案"
    assert '{categoryPath.length === 0 && (' in s1, "类目为空应给内联校验"
    assert '请选择商品类目' in s1, "类目应给内联校验文案"


def test_publish_result_alerts_sit_at_the_top_of_the_preview():
    """第三步的**错误/警告信息展示在预览信息的最顶部**（用户口径 2026-10-10：
    「错误信息应展示在预览信息的顶部」）。"""
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    i = wiz.index("function PublishStep(")
    block = wiz[i:]
    space = block.index("<Space direction=\"vertical\"")
    #: 第一条消息就是 Alert（缺价格警告 / 发布失败公告都在最前）
    assert block.index("invalid.length > 0 && (") > space
    assert block.index('type="error"') > space
    first_alert = min(block.index("invalid.length > 0 && ("), block.index('type="error"'))
    first_section = block.index("<PreviewSection")
    assert first_alert < first_section, "错误/警告必须排在第一个预览分区**之前**"


def test_republish_clears_previous_failure_alerts():
    """**再次重新发布时清除历史发布的错误信息**（用户口径 2026-10-10）。

    ⚠️ 失败原因挂在 `result` 上（顶部 error 公告的数据源）⇒ 不清空的话，
    重发过程中会一直显示**上一轮**的失败原因。
    """
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    i = wiz.index("const startedAt = performance.now();")
    seg = wiz[i : i + 400]
    assert "setResult(null);" in seg, "提交前应清空上一轮结果（否则历史错误一直挂在顶部）"


def test_step3_previews_the_selected_product_attributes():
    """第三步**预览选定的商品属性**（用户口径 2026-10-10：
    「第三步，商品属性未展示预览，选定的商品属性应展示预览」）。

    属性是**按类目动态下发**的 ⇒ 不能写死名字，必须铺快照里的 `attributes` 数组。
    """
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    assert '<Descriptions.Item label="商品属性" span={2}>' in wiz, "第三步应预览商品属性"
    assert '(config?.attributes ?? [])' in wiz, "属性应取快照里的数组（按类目动态）"
    assert "a.attributeName}：${a.attributeValue}" in wiz, "应按「属性名：值」逐条展示"


def test_multi_select_property_keeps_at_most_three_values():
    """**下拉多选最多选三个、超过的置灰色**（用户口径 2026-10-10）。

    两件一起钉：① 选满 3 个 ⇒ **未选项 `disabled`**（置灰，已选仍可取消）；
    ② `onChange` 里 `.slice(0, MULTI_SELECT_MAX)` 作为**行为兜底**。
    ⚠️ 上限常量为**唯一来源** `MULTI_SELECT_MAX`（不在两处各写一个 3）。
    """
    s2 = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))
    assert "const MULTI_SELECT_MAX = 3;" in s2, "上限应是唯一常量"
    i = s2.index('if (field.kind === "multi") {')
    block = s2[i : i + 1200]
    assert "const full = selected.length >= MULTI_SELECT_MAX;" in block, "应判「是否已选满」"
    assert "disabled: full && !selected.includes(o)," in block, "选满后未选项应置灰"
    assert ".slice(0, MULTI_SELECT_MAX)" in block, "onChange 应裁到上限（兜底）"
    assert "prop-multi-one-line" not in block, "不得再套单行裁剪容器（改用 antd 默认）"


# ---------------------------------------------------------------------------
# 2026-10-11 发布商品优化轮（第三/四批口径）：列序 · 缩略图尺寸 · 右上角切换图标 · 属性图链路移除
#
# ⚠️ 这四条规则**写在 `.tsx` 里**（页面渲染结构），vitest 导入不到 ⇒ 只能靠源码结构守卫钉住。
# 判据一律先过 `_code_only` 剥注释：本轮我自己写的迁移说明里就写着「不再展示」「已移除」
# 等字样，不剪注释会恒真（DEF-057 同型）。
# ---------------------------------------------------------------------------


def test_wizard_shows_the_file_columns_verbatim():
    """SKU 表格**按文件自己的列渲染**，不再过滤、不再自造列（用户口径 2026-10-14）。

    > 「新增排队商品 sku 表格 tab **应读到什么就展示什么**，
    >   如果为图片则展示小图预览方式，**不应该还有线上图和本地图非表格中原始的列**」

    ⚠️ **取代**原 `test_wizard_hides_the_raw_image_column`（那条要求**过滤掉**
    原始「图片」列，靠自造的「线上图片」列来预览）—— 自造列整块退场后，
    过滤动作也不该存在：文件里有什么列就渲染什么列。
    """
    code = _code_only(_read(PRECHECK_MODAL))
    assert "colIndex !== imageCol" not in code, (
        "不应再过滤「图片」列 —— 文件里的列要原样渲染（图片值由 isImageValue 判、渲染缩略图）"
    )


def test_wizard_has_no_self_invented_columns():
    """**不再自造列**（用户口径 2026-10-14）。

    > 「不应该还有线上图和本地图非表格中原始的列」

    ⚠️ **取代**原 `test_wizard_puts_the_spec_category_column_fourth`
    （那条要求自造「线上图片 / 本地图片 / 规格分类」三列并规定列序）。
    """
    code = _code_only(_read(PRECHECK_MODAL))
    for key in ("__online_image", "__local_image"):
        assert f'key: "{key}"' not in code, f"自造列 {key} 应已退场"
    #: ⚠️ **`__spec_category` 是例外**（用户口径 2026-10-14 **再修订**）：
    #: 「还是需要一列规格分类，否则第二步的参数规格分类映射无法映射，
    #:   固定在原始表读取成功后**在第二列**新增固定列规格分类默认为空」。
    #: ⇒ 它**要**存在（独立于 xlsx 列、默认空、值由正则取值填）。
    #: ⚠️ **被 2026-10-14 口径取代**：「固定的规格分类**移除**，相关逻辑也移除」
    #: ⇒ 不再有自造列，第二步的参数映射改由「用户在弹窗里**手动勾选**商品规格」
    #: （设计 40 §1.3）。
    assert 'key: "__spec_category"' not in code, "「规格分类」列已按口径移除，不该还在"


def test_wizard_sku_thumbnails_share_one_size_constant():
    """线上图与本地图预览**同尺寸**，以线上图为准（用户口径 2026-10-11）。

    两处都走 `SKU_THUMB_SIZE` 单一常量 ⇒ 改一处两边同步；任一列写死数字就脱钩，
    会出现「一列 72 一列 100」的视觉参差（正是本轮要修的）。
    """
    import re as _re
    code = _code_only(_read(PRECHECK_MODAL))
    assert "const SKU_THUMB_SIZE = " in code, "应有共享的缩略图尺寸常量"
    #: ⚠️ 光数「常量出现次数」是**假守卫**（实测被 `thumbsizeunsynced` 变异 ESCAPED）：
    #: 把其中一处换成字面量 `72`，常量仍出现 2 次 ⇒ `count >= 3` 依然绿。
    #: 真判据 = **缩略图壳里不许再出现尺寸字面量** —— 两处必须都是 `SKU_THUMB_SIZE`。
    import re as _re

    #: ⚠️ 判据演化（本轮）：本地图尺寸**不在 TSX 里** —— `PrecheckThumb` 内部是
    #: `width/height:100%`，尺寸由**外壳**决定（表格里 = CSS 的 `.sku-local-thumb`）。
    #: 所以这里的守卫分两半：
    #:   ① 线上图的 `<img>` 必须用常量（不许写死数字）；
    #:   ② 本地图外壳的 CSS 必须与线上图**同尺寸**（都在 CSS 里读得到）。
    thumb_i = code.index("function SkuImageThumb")
    thumb_body = code[thumb_i:code.index("function ", thumb_i + 10)]
    assert "SKU_THUMB_SIZE" in thumb_body, "线上图缩略图应走 SKU_THUMB_SIZE"
    assert not _re.search(r"(?:width|height):\s*\d", thumb_body), (
        "线上图缩略图不许写死尺寸数字（应走 SKU_THUMB_SIZE）"
    )
    #: ② 本地图外壳（CSS）必须与线上图同尺寸 —— 这是「一样大」的**真落点**。
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.css")
    block = css[css.index(".sku-local-thumb {"):]
    block = block[:block.index("}")]
    assert "width: 72px" in block and "height: 72px" in block, (
        "本地图外壳必须定为 72px 方形（与线上图 SKU_THUMB_SIZE 同值），"
        "否则它会撑满单元格而比线上图大"
    )
    #: ③ `fillStyle` 不许再写死尺寸（写死会把预览弹窗里的大图压成小方块）。
    fill_scope = code[code.index("const fillStyle"):]
    fill_scope = fill_scope[:fill_scope.index("};")]
    assert not _re.search(r"(?:width|height):\s*\d", fill_scope), (
        "fillStyle 不许写死尺寸（尺寸归外壳：表格 72px / 弹窗 100%）"
    )
    assert "width: SKU_THUMB_COL_WIDTH" in code, "两个图列应共用同一个列宽常量"


def test_wizard_switch_icon_sits_on_the_local_thumb():
    """**有预览图时**，「切换规格图」图标压在图片**右上角**（用户口径 2026-10-11）。

    取代本批次早先「选择图图标另起一行」的写法：有画布可覆盖时图标应该在图上
    （省一行高度、语义更准）；**没有图**时才退化成独立的「选择」按钮（没画布可压）。
    """
    code = _code_only(_read(PRECHECK_MODAL))
    assert 'className="sku-local-thumb"' in code, "本地图需要有定位容器"
    assert 'className="sku-local-thumb-switch"' in code, "切换图标要挂在缩略图上"
    #: 分支必须存在：无图 ⇒ 独立按钮；有图 ⇒ 缩略图 + 右上角图标。
    #: ⚠️ 判据演化（2026-10-14）：单元格渲染改用局部变量 `rel`，
    #: 分支形式由 `if (!info?.localRel)` 变为 `if (!rel)`（同一语义）。
    #: ⚠️ 锚点已重定向（2026-10-15）：第一列的值改为**读 `rows`**
    #: （用户口径：「把第一列**放入 rows**，它是固定列」）⇒ 变量名从 `rel`
    #: 改为 `relShown`（`rows` 里的值，缺时回落内存态避免闪空）。
    #: **守卫的意图没变**：有图给「缩略图 + 切换图标」，无图才给独立「选择」按钮。
    assert "if (!relShown) {" in code, "「无图才给独立按钮」的分支应保留"
    css = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.css"))
    assert ".sku-local-thumb-switch" in css, "右上角定位样式应在 CSS 里"
    assert "position: absolute" in css


def test_backend_has_no_attribute_image_specific_logic_left():
    """后端**没有任何「按文件名区别对待媒体」的逻辑**（用户口径 2026-10-11 最终版）。

    > 用户原话：「**数据包中有什么媒体资源就展示什么，不要排除任何媒体资源**」

    ⚠️ 多轮口径收敛的终点：**不排除任何媒体**。此前的「按文件名判定属性图/参数图
    并排除」整块撤销（判据函数、标记常量、叶子模块 `package_media_names.py` 全删）。
    删除清单：
      · `is_attribute_image_name` / `_ATTR_IMAGE_MARKERS`（判据）
      · `package_media_names.py`（承载判据的叶子模块）
      · `is_attribute_image` 契约字段 / `kv` 字段

    ⚠️ 同时**保留**另一条已完成的删除：「图片 OCR 建表」（`build_table_from_boxes`
    连同 `_squeeze` / `_merge_cell`）—— 那是**表格产出**，与媒体清单是两件事。
    """
    import re as _re
    pre_src = _code_only(_read(ROOT / "backend/app/services/shop_package_precheck.py"))
    ocr_src = _code_only(_read(ROOT / "backend/app/services/image_ocr_service.py"))
    for symbol in ("is_attribute_image_name", "_ATTR_IMAGE_MARKERS", "is_attribute_image"):
        assert symbol not in pre_src, f"{symbol} 应已整段移除（不排除任何媒体）"
    assert not (ROOT / "backend/app/services/package_media_names.py").exists(), (
        "承载属性图判据的叶子模块应已删除（无调用方）"
    )
    for symbol in ("build_table_from_boxes", "_merge_cell", "_squeeze"):
        assert symbol not in ocr_src, f"{symbol} 应已整段移除（图片 OCR 建表）"


def test_all_columns_have_the_settings_gear():
    """**所有列的表头都有设置图标**（用户口径 2026-10-14）。

    用户原话：
    > 「**所有列均表头有设置图标**，**不再只局限于文本列**」

    ⚠️ 与 `canApplyPattern` 的区别（2026-10-14 起**两者不再是同一个判据**）：
      · 表头**要不要出齿轮** ⇒ `hasColumnGear`（**所有列都要**，因为数据包规则里
        还有 fixed_text / math_scale 这类本来就是给数值列的操作）；
      · **正则区块内部**只给文本列 ⇒ `canApplyPattern`（数值列做正则没意义）。

    ⚠️ 本条**取代**两条旧守卫（它们的对象已随口径退场）：
      · `test_spec_category_header_gear_actually_opens_the_popover`
        —— 「规格分类」列已整条移除（用户口径「将固定的规格分类移除，相关逻辑也移除」）；
      · `test_column_gears_are_icon_only_and_uniform`
        —— 其判据（表头实例传 `withTrigger={false}` ⇒ 只画小齿轮）仍重要，并入下方。
    """
    code = _code_only(_src(PUBLISH_MODAL))

    #: ① 表头判据是「所有列」而不是「文本列」。
    assert "hasColumnGear((table.column_kinds ?? [])[colIndex])" in code, \
        "表头没按「所有列都给齿轮」处理"
    assert "canApplyPattern((table.column_kinds ?? [])[colIndex])" not in code, \
        "表头仍在用 canApplyPattern（数值列会没有齿轮）"

    #: ② 表头实例**只画图标**（用户口径：图标很大且带文字 ⇒ 统一只要图标）。
    i = code.index("initialColumn={colIndex}")
    seg = code[i : i + 300]
    assert "withTrigger={false}" in seg, "表头实例没传 withTrigger={false}（会画出大按钮）"
    assert "withTrigger={true}" not in seg, "表头实例被显式传了 withTrigger={true}"


def test_restore_requires_second_confirmation():
    """**还原原始值需二次确认**（用户口径 2026-10-14），且**只还原当前列**（2026-10-15）。

    用户原话：「**还原列值需二次确认**」；
             「**恢复数据应只恢复当前列而不是全表**」。

    ⚠️ 还原撤掉的原值**不可逆**（快照只留一份）⇒ 必须挡一道。
    ⚠️ 作用域是**当前列**（快照按「表 + 列」存）—— 确认框的「确定」必须把
    **这一列**传回去（`onRestore(column)`）；写成 `onOk: onRestore`（不传列）
    在 TS 上仍能过（React 会把事件对象当实参递过去），但运行时撤的是别的列。
    ⚠️ 确认框走 `App.useApp()` 的 `modal`（项目口径：静态 `Modal.confirm`
    不吃 ConfigProvider 主题）。
    """
    code = _code_only(_src(PUBLISH_MODAL))
    cpc = code[code.index("function ColumnPatternControl"):]
    cpc = cpc[:cpc.index("function SkuMatchModal")]

    assert "modal" in cpc.split("} = props;")[1][:200], "没从 App.useApp() 取 modal"
    assert "modal.confirm" in cpc, "还原没有二次确认"
    i = cpc.index("modal.confirm")
    block = cpc[i : i + 500]
    for need in ("title", "content", "onOk", "cancelText", "okText"):
        assert need in block, f"确认框缺 `{need}`"
    assert "onRestore" in block, "确认框的 onOk 没接上还原动作"
    #: ⚠️ 必须**带上这一列**（用户口径 2026-10-15）—— `onOk: onRestore` 这种
    #: 裸引用会把 React 的事件对象当 `column` 传进去，撤的还是错的列。
    assert "onRestore(column)" in block, \
        "确认框没有把当前列传给还原动作（会撤错列）"
    #: ⚠️ 文案必须说清**只还原当前列**（旧文案「丢弃本表所有列」已随口径作废）。
    assert "本表所有列" not in block, "确认框文案还写着「本表所有列」（旧口径）"
    assert "当前列" in block, "确认框文案没说清只还原当前列"


def test_wait_mask_shows_elapsed_time_once():
    """等待弹窗**进度 + 实时耗时**，且**只展示一处**（用户口径 2026-10-14）。

    用户原话：
    > 「检测违规词提示弹窗，添加**耗时统计**」
    > 「只要**序号统计实时耗时**，**不需要预估耗时**」
    > 「**底部的耗时移除，只展示一个即可**」

    判据：
      ① 进度文案里含**序号** `（done/total）` 与**实时耗时**（取 `waitElapsedSec`）；
      ② 耗时**只出现一处**（底部那行已被用户点名移除）；
      ③ **不得**出现预估（「预计 / 剩余 / ETA / 预估」）。
    """
    code = _code_only(_src(PUBLISH_MODAL))
    #: ⚠️ 2026-10-20 统一蒙版：等待卡片改由公共 `<WaitMask>` 渲染，
    #: 序号在 `title`、耗时在 `elapsed`，**两者必然同处一个组件实例**，
    #: 「底部再挂一行耗时」这种重复在结构上已不可能。判据随之改为钉住这两点：
    #: ① 序号在 `title` 里；② 耗时走 `elapsed` prop（而不是手拼进 title 字符串）。
    wait = code[code.index("{waitLabel !== null && ("):]
    wait = wait[: wait.index("/>") + 2]
    assert "progress.done" in wait and "progress.total" in wait, "进度文案没有序号统计"
    assert "elapsed={waitElapsedSec}" in wait, (
        "等待蒙版缺实时耗时 —— 必须走 WaitMask 的 elapsed prop"
    )
    #: 耗时**不得**手拼进 title（那样它就不再受组件统一控制，且会跟序号耦合）
    title_seg = wait[wait.index("title={") : wait.index("elapsed=")]
    assert "已耗时" not in title_seg, "耗时被手拼进了 title —— 应交给 elapsed prop"

    #: ③ 不许有预估。
    for word in ("预计", "剩余", "ETA", "预估"):
        assert word not in wait, f"等待弹窗出现了预估成分「{word}」（用户明确不要）"


def test_pattern_entry_moved_to_column_headers():
    """正则取值入口：**每列表头一枚图标、弹窗贴着图标、每列独立**（用户口径 2026-10-14 再修订）。

    用户原话：
    > 「将正则取值不要放在顶部，放在**每一列的表头**使用设置图标，
    >   列默认为**当前列**，依旧支持选择」
    > 「只展示在**文本列**，数值列表头不展示设置图标」
    > 「正则取值弹窗的位置应是**点击的对应设置图标**」「**每一列的弹窗均是独立的**，
    >   每次打开**只展示当前列**的正则，**默认空**」
    > 「**还原原始值与设置图标放一起**」

    ⚠️ 「仍支持选择」**已被取代** —— 那是顶部单入口时代的诉求；入口下沉到每列表头、
    每列一个实例后，弹窗里再换列会与「点的是这一枚图标」矛盾 ⇒ `lockColumn`。

    判据：
      ① 表头图标**按文本列过滤**（数值列不出图标）；
      ② 每个图标是**独立实例**（传 `initialColumn={colIndex}` + `lockColumn`）；
      ③ 锁列时**不渲染列选择器**；
      ④ 打开时**正则置空**（默认空）；
      ⑤ **还原原始值**与设置图标同处（`UndoOutlined` 在 `ColumnPatternControl` 里）；
      ⑥ 顶部**不再有**「还原原始值」按钮。
    """
    code = _code_only(_src(PUBLISH_MODAL))

    #: ① 文本列才给图标。
    #: ⚠️ **被 2026-10-14 口径取代**：原判据是「表头图标按是否文本列过滤」；
    #: 用户当天明确「**所有列均表头有设置图标**，不再只局限于文本列」
    #: （因为规则里还有 fixed_text / math_scale 这类本来就是给数值列的操作）。
    #: ⇒ 现在表头用 `hasColumnGear`（所有列都出齿轮），
    #: 而 `canApplyPattern` **降级为「正则区块内部」的判据**（那边仍只给文本列）。
    assert "hasColumnGear((table.column_kinds ?? [])[colIndex])" in code, \
        "数据列表头图标没按「所有列都给」处理"
    #: ② 每列独立实例。
    assert "initialColumn={colIndex}" in code, "表头没把所在列传给弹窗"
    #: ⚠️ **弹窗里的列选择器整块退场**（用户口径 2026-10-14：「反推正则相关的内容
    #: 全部移除」；规则也不再绑列）—— 现在是「哪个表头点开就是哪一列」，
    #: 弹窗内**不再有**换列入口（`lockColumn` 那套连同 `pattern` 一起删）。
    #: ⇒ 反着钉，防回潮。
    for gone in ("lockColumn", "!lockColumn && (", 'setPattern("")'):
        assert gone not in code, f"弹窗内换列 / 正则入口已退场，不该再出现 `{gone}`"
    #: ⑤ 还原与图标同处。
    cpc = code[code.index("function ColumnPatternControl"):]
    cpc = cpc[:cpc.index("function SkuMatchModal")]
    assert "UndoOutlined" in cpc, "「还原原始值」没和设置图标放一起"
    assert "onRestore" in cpc, "还原按钮没接上 onRestore"
    #: ⑥ 顶部工具条不再有还原按钮。
    #: ⚠️ 不能用「全文不含『还原原始值』」—— 二次确认弹窗的 `title` 正是这四个字
    #: （用户口径 2026-10-14：「还原列值需**二次确认**」）。判据落在
    #: 「**还原动作必须走确认框**」+「**设置图标旁才有还原按钮**」。
    assert "modal.confirm" in cpc, "还原没有二次确认（用户口径：还原列值需二次确认）"
    assert "onRestore" in cpc and "UndoOutlined" in cpc, "还原按钮没和设置图标放一起"
    #: ⑦ **作用域 = 当前列**（用户口径 2026-10-15：「恢复数据应只恢复**当前列**
    #: 而不是全表」）—— 快照键是「表 + 列」复合键，撤销必须指名这一列。
    assert "snapshotKey(" in code, "快照没有按「表 + 列」记（还是一次性整表撤）"
    assert "restoreColumn" in code, "没有按列还原的实现"
    #: ⚠️ 反着钉：整表还原那套（`restoreTable`）已退场，防回潮。
    assert "restoreTable" not in code, "整表还原（restoreTable）已退场，不该再出现"


def test_spec_value_container_prevents_horizontal_overflow():
    """规格值容器必须能**约束宽度 + 换行**（用户反馈 2026-10-11：「文本溢出输入框外」）。

    现象：规格值标签跑出「商品规格」卡片。

    根因链（三个都缺）：
      ① `.spec-values` 此前**没有任何 CSS** ⇒ 裸 div，子项按内容宽度撑开；
      ② antd 多选器默认不限制宽度，`.ant-select-selection-overflow` 是
         `nowrap` 的 chips 行 ⇒ 标签一多就横向顶出去；
      ③ 缺 `min-width: 0` ⇒ flex 子项默认 `min-width:auto`，想收窄也收不动。

    判据 = CSS 里这三件事都得在（缺一件就会重现溢出）。
    """
    css = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.css")
    #: ① 容器存在且是换行 flex + 不许横向溢出。
    assert ".publish-config-step2 .spec-values {" in css, "缺 .spec-values 容器规则"
    block = css[css.index(".publish-config-step2 .spec-values {"):]
    block = block[:block.index("}")]
    assert "flex-wrap: wrap" in block, "规格值容器必须允许换行"
    assert "max-width: 100%" in block, "规格值容器必须限制最大宽度"
    #: ③ 子项可收缩。
    assert "min-width: 0" in css, "flex 子项必须 min-width:0（否则拒绝收缩→顶破父容器）"
    #: ② antd chips 行必须换行 + 标签限宽。
    assert ".ant-select-selection-overflow" in css and "flex-wrap: wrap" in css, (
        "antd 多选器的 chips 行必须允许换行"
    )
    assert ".ant-select-selection-item" in css and "text-overflow: ellipsis" in css, (
        "单个规格值标签必须限宽 + 省略号"
    )
    #: ⚠️ **口径定稿**（用户 2026-10-11）：「规格输入框还是需要**固定 200px** 长度，
    #: 长文本也**不应该溢出**展示在输入框外」。
    #: ⇒ 判据是**两条一起**（缺一条就会重现用户看到的缺陷）：
    #:   ① 宽度**固定 200px**（整齐等宽栅格，不要随内容乱变宽）；
    #:   ② 输入框**自身不许溢出**（`overflow:hidden` + 省略）——
    #:      加了 ① 却不加 ② 就是用户实测的「文本溢出到框外」。
    #: 只查**第一条规则**（`spec-config-values` 作用域下的输入框）——
    #: 第二条与 `.ant-select` 共用（多选器的 antd 结构与 input 不同，
    #: 对它要求 `overflow:hidden` 会把下拉箭头一起裁掉，属误伤）。
    blk = css[css.index(".publish-config-step2 .spec-config-values .spec-value-input"):]
    blk = blk[:blk.index("}")]
    assert "width: 200px" in blk, "规格值输入框宽度应为固定 200px（用户定稿口径）"
    assert "overflow: hidden" in blk, (
        "固定宽度时必须 `overflow:hidden`，否则长文本会画出框外（用户实测缺陷）"
    )
    #: 内层 `<input>` 也要限宽（真正承载文字的是它）。
    assert ".spec-value-input > input" in css, "内层 input 也要限宽"


def test_import_can_be_cancelled_midway():
    """导入要能**中途取消**（用户口径 2026-10-11：「导入中点击取消，则中断未导入和
    未上传的数据，停止执行」）。

    ⚠️ 判据必须是「**循环里真的会看取消标志**」，只看「取消按钮有没有置位」不够 ——
    那个是**假守卫**：标志置了但循环从不检查，后台照样把剩下的图传完。

    三个要点（少一个就会「点了取消还在跑」）：
      ① 用 `useRef`（`useState` 在 `await` 长任务里是**闭包快照**，循环看不到新值）；
      ② 上传循环**每张前**检查并 `break`（不是只检查一次）；
      ③ 取消检查要在**一切写入之前**（否则点取消反把历史数据清空 = 数据丢失）。
    """
    code = _code_only(_read(STEP2))

    #: ① ref 而非 state。
    assert "importCancelRef" in code, "缺取消标志"
    assert "useRef(" in code and "importCancelRef = useRef(" in code, (
        "取消标志必须用 useRef（useState 在 await 循环里读不到新值）"
    )

    #: ② 并发 worker 里**领活前**检查取消标志。
    #: ⚠️ 2026-10-11 口径变更：上传循环由 `for (const item of plan.images)` 改成
    #: **worker pool**（并发上传，用户口径「规格上传也实现并发上传缩短时间」）。
    #: 取消语义**必须保留**：worker 每轮先查标志再取 `cursor` ⇒ 取消后不再领新项。
    #: ⚠️ 2026-10-22 换锚：并发 worker 已随「与详情图一致」迁到
    #: `product-publish-wizard.tsx::uploadManyFromPackage`（共享游标滑窗）。
    #: 取消侧改为**提交下一段之前**检查（`if (importCancelRef.current) break;`），
    #: 这两个判据分处两个文件，见 `test_spec_import_uploads_images_concurrently_*`。
    wiz = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"))
    assert "const worker = async (): Promise<void> =>" in wiz, "取字节 worker 缺失"
    assert "await fetchOne(index);" in wiz, "worker 必须真正取图"
    #: 同一张图只取一次（重复项 continue 跳过）
    assert "if (firstIndexByPath.get(items[index].rel_path) !== index) continue;" in wiz, (
        "worker 里没有「同名图只取一次」的跳过判据"
    )
    #: 反向：不许退回串行（退回即并发被撤销）。
    assert "for (const item of plan.images)" not in code, "规格图上传退回串行了"

    #: ③ 取消检查在清表之前。
    cancel_at = code.index("if (importCancelRef.current) {")
    #: 清表那句（规格值/specTypes/价格表置空）必须**晚于**取消检查。
    #: ⚠️ 锚点已重定向（2026-10-15）：清表收敛成**单一入口** `clearImportTargets()`
    #: （点「选择」重开与导入共用），原先那一串 `setSpecValues({})` 等散写已移进它。
    #: **守卫的意图没变**：取消检查必须**先于任何写入**（否则用户在开跑瞬间点取消，
    #: 会得到「历史被清空、新数据没写」的最坏结果）。
    clear_at = code.index("clearImportTargets({ keepPending: true })")
    assert cancel_at < clear_at, (
        "取消检查必须在清空历史数据**之前**，否则点取消会把历史数据清掉（数据丢失）"
    )

    #: 取消按钮要真的置位。
    assert "importCancelRef.current = true" in code, "取消按钮没有置位中断标志"

    #: ⚠️ 判据核心：**不许用 `confirmLoading`**。
    #: antd 在 `confirmLoading=true` 时会把 OK 变 loading，并**连带禁用同排的
    #: 取消按钮** ⇒ 用户点取消毫无反应（实测：「点击取消未中断也未关闭弹窗」）。
    #: 这是本用例存在的**唯一理由** —— 取消能不能点，取决于这个 prop 用没用错。
    #: ⚠️ 标题已改（用户口径 2026-10-15：「『从预检 SKU 导入规格 / 价格 / 库存』
    #: 文本调整为『导入sku数据』」）。
    modal_at = code.index('title="导入sku数据"')
    modal_body = code[modal_at:modal_at + 2000]
    assert "confirmLoading" not in modal_body, (
        "导入弹窗不许用 confirmLoading（会连带禁用取消按钮）—— 用 okButtonProps.loading"
    )
    assert "loading: importRunning" in modal_body, "OK 的 loading 要走 okButtonProps"

    #: 开跑前复位（否则上一次的取消会毒到下一次）。
    assert "importCancelRef.current = false" in code, "开跑前必须重置中断标志"


# --------------------------------------------------------------------------- #
# 2026-10-11 用户口径：**前端所有滚动条统一隐藏，只保留功能**
# --------------------------------------------------------------------------- #

GLOBAL_CSS = ROOT / "frontend/src/global.css"
FRONTEND_SRC = ROOT / "frontend/src"


def test_scrollbars_are_globally_hidden_but_scrolling_still_works():
    """全站滚动条：**隐藏外观，保留滚动能力**（不是 `overflow: hidden`）。

    用户口径：「前端所有滚动条全部统一隐藏，只保留功能」。

    ⚠️ 「统一」这个词最容易在某处**局部样式**上静默破功：局部规则写得更具体
    （`.某弹窗 .ant-table-body { scrollbar-width: thin }`）就**盖过**全局的
    `* { scrollbar-width: none }`，界面上只有那一处露出滚动条 ——
    没有人会为此报 bug，但口径已经不是「统一」了。所以这里同时守两件事：

    ① 全局口径在（两种实现都要有，缺一种会在某浏览器里露出来）；
    ② 全仓**没有**任何组件级滚动条样式（例外必须是有意为之，且同步改这里）。
    """
    css_raw = GLOBAL_CSS.read_text(encoding="utf-8")

    def _css_no_comments(text: str) -> str:
        """剥掉 CSS 注释（`/* … */`）。

        ⚠️ 必须剥：本文件的注释**逐字提到了** `scrollbar-width: none` /
        `ms-overflow-style: none` / `::-webkit-scrollbar { display: none }`
        （讲口径与原因），不剥就会拿注释满足断言 —— 实测三个变异全部 ESCAPED。
        """
        return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)

    css = _css_no_comments(css_raw)

    def _rule(selector: str) -> str:
        """取出某个选择器的**声明块**（`{ … }`，已剥注释）。

        ⚠️ 按**块**断言，不按「全文件里有没有这个字符串」：同一个声明在别处
        （旧的 `.hermes-hide-scrollbar`）也合法存在，全文件查找会被它满足
        ⇒ 全局口径被改坏也照样绿（实测 ESCAPED）。
        """
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        assert match, f"global.css 里找不到 `{selector}` 规则"
        return match.group(1)

    # ① 全局：两种实现缺一不可（各自在**自己的规则块**里）
    star = _rule("*")
    assert "scrollbar-width: none" in star, "`*` 规则缺 `scrollbar-width: none`（Firefox / 新版 Edge 会露出滚动条）"
    assert "-ms-overflow-style: none" in star, "`*` 规则缺 `-ms-overflow-style: none`（旧 Edge 会露出滚动条）"
    webkit = _rule("*::-webkit-scrollbar")
    assert "display: none" in webkit, (
        "`*::-webkit-scrollbar` 规则缺 `display: none`（Chrome / Edge / Safari 会露出滚动条）"
    )
    # ⚠️ 必须是「隐藏外观」而不是「关掉滚动」：`overflow: hidden` 会让内容滚不到。
    #    在**全局两条规则**里都不许出现（别的类名下的 `overflow:hidden` 是正当布局）。
    assert "overflow: hidden" not in star and "overflow: hidden" not in webkit, (
        "滚动条口径不得用 `overflow: hidden` —— 那会让超出容器的内容滚不到"
    )

    # ② 全仓无例外：组件里不许再出现滚动条样式
    def _no_comments(text: str) -> str:
        """剥掉块注释与行注释，**保持行号不变**（注释换成等量空行）。

        ⚠️ 必须剥注释：本文件的注释**大量引用**滚动条属性名（讲口径变更史），
        不剥就会把自己写的说明当成违规样式（实测假红，报了 3 处注释行）。
        """
        def _blank(match: "re.Match[str]") -> str:
            return "\n" * match.group(0).count("\n")
        text = re.sub(r"(?m)(?<![\"'\w])/\*.*?\*/", _blank, text, flags=re.DOTALL)
        return re.sub(r"(?m)^\s*//.*$", "", text)

    offenders: list[str] = []
    for path in FRONTEND_SRC.rglob("*"):
        if path.suffix not in (".css", ".tsx", ".ts"):
            continue
        if path == GLOBAL_CSS:
            continue
        # 只认**样式声明**（`属性: 值`），不认注释里提到属性名。
        for lineno, line in enumerate(_no_comments(path.read_text(encoding="utf-8")).splitlines(), 1):
            if "webkit-scrollbar" in line or re.search(r"scrollbar-width\s*:", line) \
                    or re.search(r"ms-overflow-style\s*:", line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert offenders == [], (
        f"这些地方还写着滚动条样式 —— 「全部统一隐藏」会被更具体的局部规则盖过：{offenders}"
    )


# --------------------------------------------------------------------------- #
# 2026-10-13 用户口径：顶栏「检查版本更新」入口（手动，不强制）
# --------------------------------------------------------------------------- #
# 用户口径原文：
# 「再切换白天和夜间模式图标旁边新增检查版本更新入口图标，点击检查当前版本，存在新
#   版本则弹窗提示更新或不更新，**只有在每次软件打开时的入口才要求强制更新**」。
#
# 判据分两半（**手动入口不强制 / 启动入口强制**，两条一起才完整）：
SHELL_TSX = "frontend/src/components/layout/workspace-shell.tsx"
UPDATE_DIALOG = "frontend/src/components/system/update-dialog.tsx"


def test_manual_update_entry_sits_next_to_theme_toggle():
    """顶栏「检查版本更新」图标与主题切换**并排**，且点击打开手动弹窗。

    ⚠️ 变异守护：把该 Button 删掉、或把 onClick 改成不设 state ⇒ 本用例必红。
    位置判据用**顺序**（主题按钮在前、更新按钮紧随其后），不靠坐标 ——
    源码守卫能判定的是「同一 Space 容器、相邻出现」，真实观感由真机验收。
    """
    src = _code_only(_read(ROOT / SHELL_TSX))
    # 锚点取**按钮上的调用**（不是别处的声明）：`onClick={toggleTheme}` 唯一
    theme_i = src.index("onClick={toggleTheme}")
    update_i = src.index("CloudDownloadOutlined", theme_i)
    assert theme_i < update_i, "更新图标应排在主题切换**之后**（并排同一行）"
    assert "检查版本更新" in src, "缺「检查版本更新」入口文案/title"
    assert "setManualUpdateOpen(true)" in src, "图标没接上打开手动弹窗的动作"
    # 两个按钮必须在**同一个** Space 容器内（否则掉到别的区域去了）
    assert "<Space" in src[max(0, theme_i - 600):theme_i], "主题/更新按钮应同处顶栏 Space"


def test_manual_dialog_is_closable_and_offers_skip():
    """**手动**入口弹窗必须可关闭且给「暂不更新」（用户口径「提示更新或不更新」）。

    ⚠️ 变异守护：把 ManualUpdateDialog 的 `closable` / 「暂不更新」按钮删掉
    ⇒ 本用例必红 —— 那就变成「手动点一下也被强制」，与口径相反。
    """
    src = _code_only(_read(ROOT / UPDATE_DIALOG))
    start = src.index("export function ManualUpdateDialog")
    body = src[start:]
    assert "closable" in body, "手动弹窗必须可关闭"
    assert "暂不更新" in body, "手动弹窗必须给「暂不更新」出口"
    # 手动弹窗**不得**出现强制入口才有的写法
    assert "maskClosable={false}" not in body, "手动弹窗不该锁蒙版（那是启动强制入口的语义）"


def test_startup_dialog_stays_forced():
    """**启动**入口弹窗保持强制：正常路径不可关闭、maskClosable=false、无「暂不更新」。

    ⚠️ 变异守护：把 UpdateDialog 的 `maskClosable={false}` 放松、或给它加「暂不更新」
    ⇒ 本用例必红（那是把「打开软件强制更新」降级，用户口径明确要求保留）。
    """
    src = _code_only(_read(ROOT / UPDATE_DIALOG))
    start = src.index("export function UpdateDialog")
    # 取到下一个 export function 之前（避免把 ManualUpdateDialog 的体算进来）
    rest = src[start:]
    nxt = rest.find("export function", 1)
    body = rest[: nxt if nxt > 0 else len(rest)]
    assert "maskClosable={false}" in body, "启动弹窗必须锁蒙版（强制语义）"
    assert "暂不更新" not in body, "启动弹窗不得给「暂不更新」（那是手动入口才有的）"
    assert "const closable = phase === \"error\"" in body, (
        "启动弹窗的 closable 应只在错误态为真（其余强制不可关）"
    )


def test_update_dialog_folds_releases_by_version_with_time_and_summary():
    """弹窗按**版本号折叠**展示，标题含 **版本号 + 更新时间 + 更新简介**。

    用户口径 2026-10-13：「弹窗中使用版本号折叠展示，记录版本更新相关信息，
    版本号，更新时间，更新简介」。
    历史数据来自 `GET /api/system/version` 的 `history`（本地 `version.json`）。
    """
    src = _code_only(_read(ROOT / UPDATE_DIALOG))
    assert "Collapse" in src, "弹窗应使用折叠面板按版本展示"
    body = src[src.index("function UpdateModalBody") :]
    #: 标题三要素：版本号（带 v 前缀）、更新时间、更新简介
    assert '`v${panel.version}`' in body, "折叠标题应显示版本号（v 前缀）"
    assert "panel.built_at" in body, "折叠标题应显示更新时间"
    assert "panel.summary" in body, "折叠标题应显示更新简介"
    #: 数据源＝本地版本历史（不是只有一个远端 notes）
    assert "history" in body, "折叠数据应来自版本历史 history"
    #: 仍保留详情展开区
    assert "panel.notes" in body, "展开区应显示更新详情"


def test_up_to_date_dialog_also_shows_release_history():
    """「当前已是最新版本」时**也要**展示版本更新记录。

    ⚠️ 2026-10-13 用户截图实测：手动入口点「检查版本更新」、已是最新时，
    弹窗只渲染一行绿字提示 + 大片空白 —— 因为「已是最新」走了一个**独立分支**，
    绕过了 `UpdateModalBody`（版本历史折叠在那里面）。修法 = 抽出 `ReleaseHistory`
    两处共用；本守卫钉死「已是最新」分支必须渲染它。
    """
    src = _code_only(_read(ROOT / UPDATE_DIALOG))
    up_to_date = src.index("flow.upToDate ?")
    else_branch = src.index(") : (", up_to_date)
    branch = src[up_to_date:else_branch]
    assert "ReleaseHistory" in branch, "「已是最新」分支必须渲染版本更新记录（否则弹窗大片空白）"
    #: 抽出的那份要独立成组件，两处共用（DEF-044：不留两份实现）
    assert src.count("function ReleaseHistory(") == 1, "版本历史应只有一份实现"
    assert src.count("<ReleaseHistory ") == 2, "两个位置（主体 + 已是最新）应共用同一份"


def test_update_dialogs_are_forty_percent_of_the_viewport():
    """两个更新弹窗 = **视口 40%**（宽 40vw + 高 40vh；用户口径 2026-10-13「检查版本

    更新 固定全屏的 40%」，随后确认「两个都改成 40%」「宽 40% + 高 40%」）。

    ⚠️ 是**视口百分比**不是固定像素 —— 换屏幕尺寸比例才一致（同违规词 60vw 的纪律）。
    ⚠️ 变异守护：把任一入口的 `40vw`/`40vh` 改回旧口径（50vw / 56vw）或像素值 ⇒ 必红。
    """
    src = _code_only(_read(ROOT / UPDATE_DIALOG))
    assert src.count('width="40vw"') == 2, (
        "启动 / 手动两个更新弹窗都应是 40vw（各一处）"
    )
    assert src.count('height: "40vh"') == 2 and src.count('maxHeight: "40vh"') == 2, (
        "两个弹窗都应固定高 40vh（maxHeight 同值，防内容把弹窗撑开）"
    )
    #: 不得残留旧口径（50vw 是上一轮、56vw 是更早一轮）
    assert 'width="50vw"' not in src, "还留着旧口径 50vw（已纠正为 60vw）"
    assert 'width="56vw"' not in src, "还留着旧口径 56vw"
    #: 不得退回固定像素（换屏幕比例就不一致了）
    assert not re.search(r'width=\{?["\']?\d{3,}', src), "更新弹窗宽度不该是固定像素"


def test_both_dialogs_share_one_state_machine():
    """两种入口**共用同一状态机**（`useUpdateFlow`）——不留两份检查/下载/安装实现。

    ⚠️ 变异守护：把 ManualUpdateDialog 自写一套 fetch ⇒ 本用例必红（DEF-044 同型）。
    """
    src = _code_only(_read(ROOT / UPDATE_DIALOG))
    assert src.count("useUpdateFlow(") >= 2, "两个入口都应调 useUpdateFlow（共用一份）"
    # 检查/下载/安装三者只在 useUpdateFlow 里各调一次（调用点唯一）
    assert src.count("checkSystemUpdate(") == 1, "检查更新调用点应唯一（不得各写一份）"
    assert src.count("downloadSystemUpdate(") == 1, "下载调用点应唯一"
    assert src.count("installSystemUpdate(") == 1, "安装调用点应唯一"


# --------------------------------------------------------------------------- #
# 用户口径 2026-10-13：数据包**区域**改为目录名直出 + 发布位映射 + 表格正则
# --------------------------------------------------------------------------- #


def test_media_zones_come_from_real_directory_names():
    """区域**动态**来自数据包（不再固定四值常量）。

    用户口径：「数据包中读取到什么就**按照读取到的文件夹分区域**，不做任何操作直接展示」。

    ⚠️ 实测代价：包 A（淘宝店透）的 5 个目录有 4 个不在旧白名单里 ⇒ **丢 63 个文件**。
    """
    src = _src(PUBLISH_MODAL)
    #: 旧的固定四值常量必须**已删除** —— 留着就会有人拿它 filter（且恒空）。
    assert "const ZONES" not in src, "固定四区常量应已删除（pack A 上恒空）"
    assert "zoneNamesOf(" in src, "媒体区没有按真实目录名动态渲染"
    assert "mediaOfZone(" in src, "没有按区域名取媒体（单一实现）"
    #: 后端：group 是目录名原值（不再查白名单表）。
    pre = _code_only(_read(ROOT / "backend/app/services/shop_package_precheck.py"))
    assert "_GROUP_BY_DIRNAME" not in pre, "目录名查表白名单应已删除（认不出就丢）"
    assert "return parts[0].strip() or None" in pre, "group 应返回真实目录名"


def test_sku_match_requires_an_explicit_zone_and_column():
    """**SKU 图自动匹配的两道硬前置**（用户口径 2026-10-13，抓的是真 bug）。

    > 「SKU 图自动匹配 弹窗图片区域未指定，已经匹配到了数据包，不正常，
    >   必须用户指定 sku 规格图的媒体资源区，后指定使用哪一些与图片名称去匹配」

    根因：`matchZones` 为空时**回落到「全部 SKU 区域」**，用户什么都没选就看到
    「已经匹配到了数据」—— 他没法判断这些图来自哪。这不是兜底，是替用户做了
    一个他没做的决定。

    判据（都落在**剥掉注释的代码**上 —— 注释里正解释着被删的旧行为）：
    ① `matchRows` 在区域为空 / 列未选时**返回空**（不再回落）；
    ② 弹窗的确认键与执行键在有前置未满足时 `disabled`；
    ③ 传给弹窗的 `column` 是**原值**（`-1` 表示未选），不再「猜一个名称列」。
    """
    src = _src(PUBLISH_MODAL)
    code = _code_only(src)

    #: ① 两道硬前置：空区域 / 未选列 ⇒ 直接返回空结果。
    assert "if (matchZones.length === 0) return [];" in code, \
        "区域为空仍在回落匹配（用户口径：必须先指定区域）"
    assert "if (matchCol < 0) return [];" in code, \
        "名称列未选仍在匹配（用户口径：必须先指定使用哪一列去匹配）"

    #: ② 按钮门禁。
    assert "okButtonProps={{ disabled: !zonePicked || !colPicked }}" in code, \
        "确认键没有按「区域/列已选」禁用"
    #: ⚠️ 2026-10-22 换锚：`disabled` 追加了 `matchBusy`（计算中也要点不到，
    #: 避免双击排两次队）。判据不变：**区域/列未选则禁用**。
    assert "disabled={!zonePicked || !colPicked || matchBusy}" in code, \
        "「执行匹配」没有按前置禁用"

    #: ③ 不再替用户猜名称列 —— **判据落在 SkuMatchModal 的调用点上**。
    #: ⚠️ 第一版写成「全文不得出现 `skuNameColumnIndex(tables[0]...`」⇒ 变异
    #: 逃逸：另有一处合法用法（line 4511，别处的默认名称列），全文判据看不见
    #: 「调用点又加回了猜列」。收紧为：调用点里 `column` 必须**直接传 `matchCol`**。
    at = code.index("<SkuMatchModal")
    call = code[at:code.index("/>", at)]
    assert "column={matchCol}" in call, \
        "SkuMatchModal 的 column 没有直接传 matchCol（仍在回落猜一个名称列）"
    assert "skuNameColumnIndex" not in call, \
        "SkuMatchModal 调用点又加回了「猜名称列」"

    #: ④ 取值正则独立于匹配方式，且提取失败回落原值。
    assert "matchExtract" in code, "没有 SKU 列取值正则的状态"
    assert "applyExtract(" in code, "没有把取值正则接进匹配链路"

    #: ⑤ 弹窗顶部的大提示条已移除（用户口径：「移除占用空间」）。
    modal = code[code.index("function SkuMatchModal"):]
    modal = modal[:modal.index("function BatchColumnControl")]
    assert "Alert" not in modal, "SKU 匹配弹窗仍在用占位大的 Alert 提示条"

    #: ⑥ 「图片名称」列仍在（它是判据 —— 用户要拿它核对为什么命中/没命中）。
    assert 'title: "图片名称"' in modal, "没有图片名称列"
    #: ⑦ ⚠️ **用户口径 2026-10-14 取代原先的列序**：
    #:    「匹配到的 sku 图展示在**第一列**，方式列移除」
    #:    ⇒ 「匹配到的图」必须排在**最前**（原先要求它在「图片名称」之后）。
    assert modal.index('title: "匹配到的图"') < modal.index('title: "图片名称"'), \
        "「匹配到的图」应排在列表第一列"
    #: ⑧ 小图展示（外层固定尺寸容器包裹，因为 PrecheckThumb 是 100% 填充没有 size prop）。
    assert "width: 36, height: 36" in modal, "匹配到的图没有用小图展示"


def test_sku_match_modal_layout_and_modes():
    """**SKU 匹配弹窗**的布局与门禁（用户口径 2026-10-13 → 2026-10-14 演化）。

    判据逐条对应原话：
      * 「弹窗全屏的宽高 50%」→ 纠正为 60%  ⇒ `width="60vw"` + `height: "60vh"`
      * 「图片区域（必选）与 SKU 名称列一行（必选）」⇒ 第 1 行两栏**各占 50%**
      * 「预检通过的所有图片区域均支持被选中」⇒ 区域候选取**全部预检媒体**

    ⚠️ **两条已被取代**（用户口径 2026-10-14，本轮）：
      * 「匹配规则精确/模糊」→ **取消匹配方式**（匹配 = 图片名包含 SKU 名）；
      * 「图片名称取值正则」→ **图片名侧不再需要正则**（改判包含）
        ⇒ 第 2 行由「两条正则并排」降为**单条 SKU 列正则**，`matchModeLabel` 退场。
    """
    src = _src(PUBLISH_MODAL)
    code = _code_only(src)
    modal = code[code.index("function SkuMatchModal"):]
    modal = modal[:modal.index("function BatchColumnControl")]

    #: ① 60% 固定宽**高**（用户口径：「弹窗全屏的宽高 50%」→ 纠正为 60%）。
    assert 'width="60vw"' in modal, "弹窗宽度不是 60vw"
    assert 'height: "60vh"' in modal, "弹窗高度不是 60% 视口"

    #: ② 第 1 行两栏（图片区域 + SKU 名称列），**各占 50%**。
    #: ⚠️ 用户口径 2026-10-14：「图片区域和 sku 列名称也同上布局（各占 50%）」——
    #: 右栏由固定 `width: 200` 改为 `flex: 1` ⇒ 不再有 `flexShrink: 0`。
    assert modal.count('display: "flex"') >= 1, "没有 flex 分栏布局"
    assert modal.count("flex: 1, minWidth: 0") >= 2, \
        "两栏没有各占 50%（应都是 flex:1 + minWidth:0）"

    #: ③ **取消匹配方式**（用户口径 2026-10-14：「取消页面弹窗中的匹配方式」）。
    assert 'value="exact"' not in modal, "匹配方式（精确）应已取消"
    assert 'value="fuzzy"' not in modal, "匹配方式（模糊）应已取消"
    assert 'value="regex"' not in modal, "匹配方式（正则）应已取消"

    #: ⚠️ 原「必选 / 非必须」标注是给 **SKU 列取值正则**输入框的。
    #: 正则入口整块退场（用户口径 2026-10-14：「反推正则相关的内容全部移除」）
    #: ⇒ 标注随之消失，反着钉。
    assert "SKU 列取值正则" not in modal, "取值正则输入框已退场，不该再出现"

    #: ⑤ **图片名称侧不再需要正则**（用户口径 2026-10-14：
    #:    「图片名称不是固定样式的，但一定包含 sku 名」⇒ 改判包含）。
    assert "图片名称取值正则" not in modal, "仍在让用户给图片名称写正则"
    assert "图片名称正则" not in modal, "仍在让用户给图片名称写正则"

    #: ⑥ 区域候选 = 全部预检媒体（不是 `kind === "sku"` 的 skuMedia）。
    assert "zonesSource" in code, "区域候选仍只取 skuMedia（应取全部预检媒体）"
    assert "allMedia: PrecheckMediaItem[]" in code, "TablesPanel 没有接全量媒体"
    assert "allMedia={precheck.media ?? []}" in code, "调用点没把全量媒体传下去"


def test_sku_match_gates_and_chinese_labels():
    """SKU 匹配的**两道门禁**（用户口径 2026-10-13）。

    > 「商品发布 sku图自动匹配点击前校验是否违规词已经检测通过，不通过则提示
    >   请先进行违规词检测；点击下一步配置商品参数按钮校验 SKU 图需自动匹配触发
    >   成功后才可以点击下一步，否则提示请先匹配sku图；匹配方式应展示中文」

    判据：
      ① 「SKU 图自动匹配」按钮点击时校验 `precheckPassed`，不通过 `message.warning`；
      ② 「下一步」`goStep2` 校验 `skuMatchRan`，不通过提示「请先匹配 SKU 图」；
      ③ `skuMatchRan` 由「执行匹配」置真（**不是** `matchRows.length > 0`）。

    ⚠️ 原 ④「方式列走中文映射」**已被取代**（用户口径 2026-10-14：
    「匹配到的 sku 图展示在第一列，**方式列移除**」）⇒ `matchModeLabel`
    与「方式」列同批退场，判据相应删除（不留「函数还在但没人用」的假覆盖）。
    """
    src = _src(PUBLISH_MODAL)
    code = _code_only(src)

    #: ① 匹配按钮的违规词门禁。
    assert '"请先进行违规词检测"' in code, "匹配按钮没有违规词门禁提示"
    at = code.index("SKU 图自动匹配\n")
    seg = code[max(0, at - 700):at]
    assert "precheckPassed" in seg, "匹配按钮的门禁没用 precheckPassed"

    #: ② 下一步的匹配门禁。
    assert '"请先匹配 SKU 图"' in code, "「下一步」没有 SKU 匹配门禁提示"
    go = code[code.index("const goStep2 = useCallback("):]
    go = go[:go.index("}, [")]
    assert "skuMatchRan" in go, "goStep2 没有校验 skuMatchRan"

    #: ③ 置真点是「执行匹配」，且**不是**用 matchRows 长度当判据。
    assert "onRun={onSkuMatchRun}" in code, "执行匹配没接上置真回调"
    assert "setSkuMatchRan(true)" in code, "没有置真 skuMatchRan 的地方"
    assert "skuMatchRan" in code and "matchRows.length > 0 ?" not in go, \
        "门禁错用了 matchRows 长度（无法区分「没匹配」与「没配上」）"

    #: ④（取代原「方式列中文」）用户口径 2026-10-14：
    #:    「匹配到的 sku 图展示在**第一列**，**方式列移除**」。
    assert "matchModeLabel" not in code, "「方式」列应已移除，不该再有 matchModeLabel"
    assert 'title: "方式"' not in code, "「方式」列仍在"
    #: 图片列必须排在规格列**之前**。
    assert code.index('title: "匹配到的图"') < code.index('title: "规格（SKU 名称）"'), \
        "「匹配到的图」没有排在列表第一列"


def test_sku_match_apply_writes_into_the_table_that_renders_it():
    """**「应用匹配结果」必须同时更新商品 SKU 表格**（真 bug，2026-10-14）。

    用户原话：
    > 「点击应用匹配结果后，商品sku表格依旧显示未匹配到本地数据，
    >   第一列已经展示了匹配到的sku列图」

    根因：两张表各读各的 —— 弹窗的表格读 `appliedSkuImages`，
    而**商品 SKU 表格**的「本地图」判据走 `lookupLocalSkuImage(...)` /
    `manualPicks`，完全不看 `appliedSkuImages` ⇒ 匹配成功也看不见。

    判据：`applySkuMatch` 里既要写 `appliedSkuImages`，
    也要写 `manualPicks`（后者才是 SKU 表格的权威覆盖）。
    """
    code = _code_only(_src(PUBLISH_MODAL))
    body = code[code.index("const applySkuMatch ="):]
    body = body[:body.index("\n  };")]
    assert "setAppliedSkuImages(" in body, "没写弹窗自己的第一列预览"
    assert "setManualPicks(" in body, \
        "没写商品 SKU 表格读的 manualPicks ⇒ 点了「应用」表格仍显示未匹配（真 bug 回归）"


def test_sku_match_labels_are_block_wrapped_before_controls():
    """**「下拉图标掉到第二行」的真根因**：标签与控件没分成两块（用户口径 2026-10-13）。

    > 「SKU 名称列的下拉框的下拉图标还是在第二行，正常默认应在第一行末位的，
    >   分析原因是否存在 css，移除对应样式」

    定位结论：**不是 Select 的 CSS**（`global.css` 里没有任何 `.ant-select` 规则，
    本用例同时钉这一点），而是标签写成**行内** `<Text>` 后紧跟块级 `Select` ⇒
    混排把 `Select` 的可用宽度算错，窄容器（50vw 下右栏固定 200）里下拉箭头被挤到第二行。

    ⇒ 修法：**每个控件的标签单独包一层 `<div>`**，控件另起一行，宽度来源单一。
    """
    src = _src(PUBLISH_MODAL)
    code = _code_only(src)
    modal = code[code.index("function SkuMatchModal"):]
    modal = modal[:modal.index("function BatchColumnControl")]

    #: ① 每个标签都有独立的一层 `<div>` 包裹（`lineHeight: "20px"` 是本轮统一写法）。
    
    #: ② 每个控件的标签都在**自己的 div** 里（= 不与控件混排）。
    #:
    #: ⚠️ **不要用「窗口内找 `</div>` / 找控件」这类结构匹配** —— 实测三次假红：
    #:   JSX 注释里也有 `</div>`、窗口会跨列捞到隔壁的 `<Select>`、
    #:   `_code_only` 删注释后偏移错位。这种判据看着严格，实际是**脆的**。
    #: ⇒ 改判**不变量**：标签文字与控件之间必须隔着一次标签块闭合
    #:   （`标签 … </div> … 控件`，而不是 `标签 … 控件`）。
    #:   用**去掉 JSX 注释**的文本判（注释里的 `</div>` 是噪音）。
    no_comment = re.sub(r"\{/\*.*?\*/\}", "", modal, flags=re.S)
    for label in ("图片区域", "SKU 名称列"):
        at = no_comment.index(f"<Text strong>{label}</Text>")
        seg = no_comment[at:]
        close = seg.find("</div>")
        assert close > 0, f"「{label}」标签块没有闭合"
        head = seg[:close]
        assert "<Select" not in head and "<Input" not in head, (
            f"「{label}」的标签与控件混排（控件出现在标签块闭合之前）"
        )

    #: ③ ⚠️ 全站 `global.css` **不得**出现 `.ant-select` 覆盖规则
    #:    （用户要求「分析原因是否存在 css，移除对应样式」—— 定位结论是没有）。
    css = _src(ROOT / "frontend/src/global.css")
    assert ".ant-select" not in css, \
        "global.css 里有 .ant-select 覆盖规则（会干扰 Select 宽度/箭头布局）"


def test_sku_match_uses_the_complete_sku_text():
    """匹配判据 = **图片名包含 SKU 名**（用户口径 2026-10-14 定案）。

    用户原话：
    > 「图片名称**不是固定样式**的，但**一定包含** sku 名」

    ⚠️ 这条**取代**了此前三版口径（都已被推翻，逐版留证）：
      * v1「整串双向包含」⇒ 短串串长串，52/52 全中；
      * v2「取末段」⇒ 末段 `上门安装` 图上根本没有，全落空；
      * v3「拆段逐段比」⇒ 分类段 `木` 命中所有含「木」的图；
      * v4「两侧各跑正则 + 精确相等」⇒ 图片名样式不固定，正则必须靠反推，
        而反推会把该条样例特有的字面量写死（`SKU01`）⇒ 用户报的
        「反推后只匹配成功了第一条」。
    """
    lib = _src(ROOT / "frontend/src/lib/precheck-zones.ts")
    code = _code_only(lib)

    #: ① 旧口径的痕迹必须清干净（拆段 / 末段 / 长度阈值 / 模糊两档）。
    assert "skuNameSegments" not in code, "仍在拆段（已被包含判据取代）"
    assert "lastSkuSegment" not in code, "仍在取末段（已被包含判据取代）"
    assert "MIN_FUZZY_SEGMENT_LEN" not in code, "短段阈值仍在（拆段口径的残留）"
    assert "skuNameText" not in code, "skuNameText 仍在（两侧正则口径的残留）"
    assert "looseMatchKey" not in code, "looseMatchKey 仍在（精确/模糊两档的残留）"
    assert "matchModeLabel" not in code, "matchModeLabel 仍在（方式列已移除）"

    #: ② 判据落点：左 = SKU 列值经**用户正则**取值；右 = 图片名去目录去扩展名。
    fn = code[code.index("export function matchesSkuName"):]
    fn = fn[:fn.index("export function matchSkuRows")]
    assert "applyExtract(" in fn, "左侧没走 SKU 列取值正则"
    assert "imageBaseName(" in fn, "右侧没走「去目录去扩展名」的文件名载体"
    #: ③ **包含**判据（不是相等）—— 这是「样式不固定但一定包含」的落点。
    assert ".includes(" in fn, "没有用包含判据（图片名样式不固定，相等会全废）"

    #: ④ 消歧现状（⚠️ 判据随实现换了形态，见下）。
    #:
    #: 历史：2026-10-14 是「按值长度降序的命中对列表 + 一图一用」；
    #: 2026-10-21 用户口径「SKU 图**支持重复匹配**」⇒ 一图一用退场，
    #: 实现改成**每行独立挑「文件名最短的够用图」**（短值 ⊂ 长图名时
    #: 不会把长图抢走）—— 排序概念随之消失，所以这里不再查 `b.len - a.len`，
    #: 而是查「逐行挑最优」这个新判据。
    rows_fn = code[code.index("export function matchSkuRows"):]
    assert "bestLen" in rows_fn, "没有「每行挑最贴切图」的判据（短值会抢长值的图）"
    assert "cand.fileName.length" in rows_fn, "挑图没按文件名长度比较"
    #: ⚠️ 判据必须是**占用集合本身**，不是子串 `usedCand` ——
    #: 参数名 `usedCandidates` 里就含这几个字符（实测假红）。
    assert "usedCand.add(" not in rows_fn and "usedCand.has(" not in rows_fn, (
        "仍在做「一图一用」（占用去重）—— 同名规格只会第一行匹配上，其余全「未匹配」"
    )
    #: ⚠️ 判据 = 「每行独立挑一张」（`best` 变量 + 赋值一次）——
    #: 原实现用 `usedRow` 集合，随「按行独立挑图」重写后变量名没了。
    #: ⚠️ 2026-10-22 换锚：`matchSkuRows` 已被 `pickSkuImageFor` 取代
    #: （按 key 逐个挑最优候选，天然「一行只取一张」）。
    zones = _read(ROOT / "frontend/src/lib/precheck-zones.ts")
    fn_i = zones.index("export function pickSkuImageFor(")
    fn_body = zones[fn_i : fn_i + 1400]
    assert "let best = -1;" in fn_body and "return { relPath: candidates[best].relPath" in fn_body, (
        "没有「一行只取一张图」的判据（每行仍应只有一个结果）"
    )


def test_sku_match_has_no_mode_type():
    """`SkuMatchMode`（精确/模糊两档）**整类退场**（用户口径 2026-10-14）。

    > 「取消页面弹窗中的匹配方式」

    ⚠️ **取代**原 `test_sku_match_mode_type_has_no_regex`（那条只要求
    类型里不含 `"regex"`，仍保留 exact/fuzzy 两档）—— 两档本身已被取消。
    """
    lib = _src(ROOT / "frontend/src/lib/precheck-zones.ts")
    assert "SkuMatchMode" not in lib, "SkuMatchMode 类型应已整类退场"
    #: 图名侧的抽键函数 `imageMatchKey` 也已退场（改判包含 + 去扩展名）。
    assert "export function imageMatchKey" not in lib, \
        "imageMatchKey 应已退场（图片名侧不再需要正则）"


def test_zone_mapping_gate_before_step_two():
    """**进入第二步的第二个关卡**：区域映射弹窗（用户口径 2026-10-13）。

    > 「点击进入第二步时弹窗提示指定主图区域、详情图区域、商品视频……
    >   选择后才可以进入第二步」「均支持合并多个第一步中的图片区域」
    """
    src = _src(PUBLISH_MODAL)
    assert "ZoneMapModal" in src, "没有区域映射弹窗"
    assert "setZoneMapOpen(true)" in src, "点「下一步」没有弹区域映射"
    #: 确认后才真正进第二步（选择后才可进）。
    assert "confirmZoneMap" in src, "没有映射确认入口"
    assert "setStep(1)" in src, "确认后没有进第二步"
    #: 多选 + 不展示图（用户口径：「不展示图，使用多选下拉展示媒体区域名称即可」）。
    assert 'mode="multiple"' in src, "区域映射不是多选（不能合并多个区域）"
    #: 必填位空 ⇒ 进不去。
    assert "isZoneMapReady" in src, "没有「必填位非空才可进」的判据"
    assert "unusedZones" in src, "没有列出未使用的区域（漏配要能看出来）"
    #: ⚠️ 用户口径 2026-10-13（**后续追加**）：「区域映射弹窗我做了默认值 **不做默认值**」
    #: ⇒ 弹窗必须从**空映射**开始（`emptyZoneMap()`），**不得**按 `kind` 预填。
    #: 预填会让用户直接点确认 = 系统替他决定发布位（`kind` 只是参考分类，
    #: 包 A 的 `1比1主图` 与 `页面展示主图` 都会被猜成主图）。
    code2 = _code_only(src)
    assert "emptyZoneMap()" in code2, "区域映射没有从空映射开始"
    assert "defaultZoneMap(" not in code2, "区域映射仍在按 kind 预填默认值（已被用户否掉）"
    #: 加载数据包时必须是**重置为空**，不是「只在首次为空」。
    at = code2.index("setZoneMap(emptyZoneMap())")
    assert at > 0, "加载数据包没有把映射重置为空"
    #: ⚠️ 发布位**不再**由目录名猜测（4 处硬编码必须清掉）。
    #: 判据落在**剥掉注释的代码**上 —— 注释里到处都是「旧写法 group==="main"」
    #: 这类说明（那正是要保留的历史），不剥就会把注释当成实现。
    code = _code_only(src)
    assert 'm.group === "main"' not in code, "仍有 group===main 硬编码（目录名恒不等）"
    assert 'm.group === "detail"' not in code, "仍有 group===detail 硬编码"
    assert 'm.group === "video"' not in code, "仍有 group===video 硬编码"
    assert 'm.group === "sku"' not in code, "仍有 group===sku 硬编码"


def test_sku_import_uses_matched_images_not_the_whole_zone():
    """第二步「导入 SKU 图」的数据源 = **第一步匹配成功的图**（用户口径 2026-10-13）。

    > 「第二步中的导入 sku 图上传的是第一步 sku 表中**已经匹配成功的**图」
    """
    src = _src(PUBLISH_MODAL)
    #: 匹配结果必须**上抛到向导**（面板 state 会随页签卸载而丢）。
    assert "onSkuMatchChange" in src, "匹配结果没有上抛"
    assert "matchedSkuImages" in src, "向导没有持有匹配结果"
    #: SKU 图候选**取匹配结果**，不是「SKU 区域里的全部图」。
    assert "matchedSkuImages.map(" in src, "skuImagePaths 没有取自匹配结果"


def test_column_pattern_overwrites_in_place_and_can_be_undone():
    """表格正则**覆盖原列**（不做派生列）+ 可撤销（用户口径 2026-10-13）。

    > 「表格列通过正则对原数据中的列取值操作后**覆盖原始列**」
    > 「默认**覆盖全部**，如果存勾选行则**只覆盖勾选行**」

    ⚠️ 撤销的**作用域 = 当前列**（用户口径 2026-10-15：「恢复数据应只恢复当前列
    而不是全表」）—— 快照按「表 + 列」复合键存；这条只管「有没有撤销入口」，
    按列语义由 `test_restore_requires_second_confirmation` 与
    `frontend/src/lib/table-pattern.test.ts` 钉。
    """
    src = _src(PUBLISH_MODAL)
    assert "ColumnPatternControl" in src, "没有表格正则入口"
    assert "applyPatternToColumn" in src, "没有覆盖原列的实现"
    #: **撤销**：破坏性动作必须能还原（原值没了就真没了）。
    assert "snapshotTable" in src, "覆盖前没有存快照（不可撤销）"
    #: ⚠️ 入参从「表」变成「表 + 列」（用户口径 2026-10-15）—— 只传表下标时
    #: 同一张表只能有一份快照，「撤 B 列连带撤掉 A 列」。
    assert "snapshotTable(tables[tIndex], tIndex, column)" in src, \
        "存快照没带上列下标（还原作用域退化成整表）"
    assert "restoreRows" in src, "没有还原入口"
    assert "还原原始值" in src, "界面上没有还原按钮"
    #: ⚠️ **反推正则整条链路已退场**（用户口径 2026-10-14：「**反推正则相关的内容
    #: 全部移除**」）—— 取值改由「数据包规则」承担（规则是**配置好的**、
    #: 按包归属、可复用；反推是临场猜，跨包 0% 命中，见设计 40 §1）。
    #: ⇒ 这里反着钉：**界面与实现里都不许再有反推入口**（防回潮）。
    code = _code_only(src)
    for gone in (
        "DerivePatternModal",
        "setDeriveOpen",
        "deriveExtractPattern",
    ):
        assert gone not in code, f"反推正则链路已退场，不应再出现 `{gone}`"


def test_precheck_button_only_checks_forbidden_words():
    """用户口径 2026-10-13：「预检按钮**只预检图片是否存在违规词**，按钮的文案使用重新明确」，
    且「将预检中的 sku 图与 sku 匹配**拆分出来**」。
    """
    src = _src(PUBLISH_MODAL)
    #: 文案说清检的是什么（不再是一个泛泛的「预检」）。
    assert "检测违规词" in src, "预检按钮文案没有明确"
    #: SKU 图匹配是**独立入口**（在 sku 表 tab 下）。
    assert "SKU 图自动匹配" in src, "SKU 图匹配没有独立按钮"
    assert "SkuMatchModal" in src, "没有 SKU 图匹配弹窗"
    #: 匹配方式（用户口径 2026-10-13 **修订**：「匹配规则调整为**精确和模糊**，
    #: 图片名称不支持再使用正则处理后再匹配，只能使用原始图片名称」）。
    #: ⚠️ 原判据断言三值含「正则表达式」—— **已被本轮口径取代**，
    #: 原句保留在此以免下一个人以为它从没存在过。
    #: ⚠️ 判据落在**剥掉注释**的代码上 —— 文件头 docstring 里刻意引用了
    #: 用户**旧**口径原话（「…或使用正则表达式」），不剥注释就会把那段引文
    #: 当成实现（实测踩过两次）。
    #: ⚠️ 判据只落在 **SkuMatchModal 内部** —— 页面别处（列批量「正则取值」）
    #: **合法地**含「正则」二字，全文判据会把那个功能误伤（实测踩到）。
    code = _code_only(src)
    modal = code[code.index("function SkuMatchModal"):]
    modal = modal[:modal.index("function BatchColumnControl")]
    #: ⚠️ **匹配方式整组已取消**（用户口径 2026-10-14：「取消页面弹窗中的匹配方式」）
    #: ⇒ 原先「精确/模糊两个 Radio.Button」的判据**被取代**，改为反向钉死。
    #: 同一轮图片名侧也不再需要正则（改判「包含 SKU 名」）。
    assert "<Radio.Group" not in modal, "匹配方式（Radio.Group）应已整组取消"
    assert 'value="exact"' not in modal and 'value="fuzzy"' not in modal, \
        "匹配方式（精确/模糊）应已取消"
    #: 匹配结果的**命中率**要显示（漏配要能看出来）。
    assert "命中 " in src, "没有显示命中行数"


def test_sku_table_identified_by_columns_not_sheet_name():
    """SKU 表按**列名特征**判（用户口径 2026-10-13），不靠 sheet 名。

    实测：包 B 的 sheet 叫 `全部`、包 A 的被导出工具截断 ⇒ 两个 sheet 名都不可用。
    """
    pre = _code_only(_read(ROOT / "backend/app/services/shop_package_precheck.py"))
    assert "looks_like_sku_table" in pre, "没有按列名特征识别 SKU 表"
    assert "column_kind" in pre, "没有列类型判定"
    assert "_SKU_COLUMN_HINTS" in pre, "没有列名特征表"
    #: 列类型必须随表下发（前端据此决定给不给正则入口）。
    assert "column_kinds" in pre and "is_sku_table" in pre, "表格契约缺列类型 / SKU 表标记"


def test_non_compliant_media_is_listed_with_a_red_flag():
    """方案 C（用户口径 2026-10-13）：「展示与上传都按平台，但把不合规的**列出来而不是丢掉**，红框提示」。"""
    pre = _code_only(_read(ROOT / "backend/app/services/shop_package_precheck.py"))
    #: 不合规的**照旧进清单**（不是 continue 掉）+ 带人话原因。
    assert "unsupported_reason" in pre, "没有带上不可上传的原因"
    src = _src(PUBLISH_MODAL)
    assert "unsupportedText" in src, "前端没有展示不可上传原因"
    assert "不可发布" in src, "不合规的图没有红框/角标提示"

# =========================================================================== #
# 2026-10-15 四张卡（还原按列 / 预检导入 / 货号 / 固定文本+运算）的守卫
# =========================================================================== #


def test_restore_only_touches_the_current_column():
    """「还原原始值」**只还原当前列**，不是整表（用户口径 2026-10-15）。

    用户原话：
    > 「恢复数据应只恢复当前列而不是全表」

    判据（这条是 ① 的承重面）：
      · 快照是**按列**存的（`TableSnapshot.columns[列] = 该列原值`）；
      · 还原**只覆盖快照里那几列**，其余列原样；
      · 向导层是 `restoreColumn(tIndex, column)`（不是整表的 `restoreTable`）。
    """
    lib = _code_only(_src(ROOT / "frontend/src/lib/table-pattern.ts"))
    #: ① 快照按列。
    assert "columns: Record<number, (string | null)[]>" in lib, \
        "快照不是按列存的（整表快照 ⇒ 还原会波及全表）"
    #: ② 还原只覆盖快照里的列（遍历 snapshot.columns，不是整行替换）。
    assert "snapshot.columns" in lib, "还原没有按列遍历快照"
    assert "colValues" in lib, "还原没有逐列取原值"
    #: ③ 不能再有「整表 rows 直接覆盖」的写法（那是本轮要消掉的行为）。
    assert "rows: snapshot.rows.map" not in lib, \
        "还原仍是整表覆盖（`snapshot.rows` 直接铺回）—— 本轮要改成按列"
    #: ④ 向导层调用的是按列的入口。
    wiz = _code_only(_src(PUBLISH_MODAL))
    assert "restoreColumn" in wiz, "向导层没有按列还原的入口"
    assert "restoreTable" not in wiz, "向导层仍在调整表还原"


def test_spec_import_button_is_not_greyed_when_names_have_no_colon():
    """「预检通过SKU导入」**不该因名称没冒号而恒灰**（用户报的真 bug，2026-10-15）。

    用户原话：
    > 「预检通过SKU import 按钮提示没有可导入的 SKU…实际第一步的 sku 已经预检
    >   通过且有勾选部分数据」

    根因（实测 12 个真实数据包全部同形）：`sku名称` 列值是 `A款` 这类**纯值**，
    `parseSkuName().category` 是空串 ⇒ `specName` 空 ⇒ `sourceSpecNames()` 丢弃
    ⇒ `specImportSources=[]` ⇒ `disabled={length===0}` 恒真。

    判据：无冒号时**退化为单一维度**（`SINGLE_SPEC_NAME`），**不是空串**。
    """
    lib = _src(ROOT / "frontend/src/lib/precheck-publish.ts")
    #: ① 退化常量存在。
    assert 'SINGLE_SPEC_NAME = "规格"' in lib, "没有「无分类时的单一维度」常量"
    #: ② 源头 `passedSkuRows` 就退化了（`|| SINGLE_SPEC_NAME`）。
    #: ⚠️ **锚点已按新口径重定向**（2026-10-15）：判据从 `parsed.category`（拆冒号）
    #: 换成**「规格分类」列**（用户口径：「是从 sku名称 拆冒号得 ⇒ 移除逻辑，
    #: 应读 sku 预检通过的规格分类固定列」）。
    #: **守卫的意图没变** ——「名称形态认不出分类时不许丢行、要退化成单一维度」
    #: （丢了 ⇒ 按钮恒灰，就是这条守卫当初抓的那个 bug）。
    #: ⚠️ 2026-10-22 换锚：用户口径「不要代码推断名称形态，用户使用规则自己控制」
    #: ⇒ 不再有 `category ||` 分支，而是**无条件**取 `SINGLE_SPEC_NAME`
    #: （中性占位，用户在映射弹窗里自己指定平台规格类型）。
    #: 守卫意图不变：**「分类认不出」时不许丢行**（丢了 ⇒ 按钮恒灰）。
    assert "const specName = SINGLE_SPEC_NAME;" in lib, \
        "`passedSkuRows` 没把「分类取不到」退化成单一维度 ⇒ 按钮恒灰（原真 bug）"
    #: ③ 取值**读「规格分类」列**（不再从名称拆）。
    #: ⚠️ 2026-10-22：`specCategoryColumnIndex` 已随「规格分类固定列」一起移除。
    #: 现在的判据反向：**不许再猜名称形态**，值取完整 sku 名称、分类交给用户映射。
    assert "const specValue = rawName;" in lib, \
        "`passedSkuRows` 又退回去猜 sku 名称形态了（值应为完整名称）"
    #: ④ 值 = **整个 sku 名称**（分类已独立成列，名称里的前缀不再切掉）。
    assert "const specValue = rawName;" in lib, \
        "无分类时 `specValue` 取错（应是整个名称原样，不是空串）"
    #: ⚠️ 2026-10-22：`sourceSpecNames` 已随「规格分类固定列」移除（用户口径
    #: 「导入弹窗左侧改为**预检通过列**（表格全部列）」）⇒ 兜底判据改钉
    #: **`passedSkuRows` 本身**绝不能产出空 specName（那会让按钮恒灰）。
    code = _code_only(lib)
    k = code.index("export function passedSkuRows")
    seg = code[k : code.index("export function", k + 10)]
    assert "const specName = SINGLE_SPEC_NAME;" in seg, (
        "`passedSkuRows` 没有兜底 ⇒ 跨层调用仍会恒灰"
    )


def test_item_no_is_the_first_other_property():
    """「货号」**固定是「其他属性」第一条**（用户口径 2026-10-15）。

    用户原话：
    > 「第三步，类目生成后，商品的其他属性中没有展示货号属性，
    >   它应该是固定的其它属性第一个」

    根因：平台 `desc_properties` **不下发**货号，而旧代码假定它在 `loadedFields` 里
    做「覆盖填充」⇒ 永远匹配不到 ⇒ 用户从来看不见它。
    """
    lib = _src(ROOT / "frontend/src/lib/publish-property-fields.ts")
    assert 'ITEM_NO_PROPERTY_ID = "articleNo"' in lib, "货号 id 不是千帆 bundle 的 articleNo"
    assert "export function withItemNoFirst" in lib, "没有「货号置首」的实现"
    code = _code_only(lib)
    i = code.index("export function withItemNoFirst")
    seg = code[i:i + 200]
    #: **承重面**：必须是 `[货号, ...其余]` 的顺序（插到首位，不是追加）。
    assert "[buildItemNoProperty(), ...withoutItemNo(fields)]" in seg.replace(" ", "").replace(
        "[buildItemNoProperty(),...withoutItemNo(fields)]",
        "[buildItemNoProperty(), ...withoutItemNo(fields)]",
    ).replace(",", ", ").replace("  ", " "), \
        "货号没被插到首位（或没去重）"
    #: 去重判据必须「id 或名」（只判 id / 只判名都会插出两条）。
    assert "f.id !== ITEM_NO_PROPERTY_ID" in lib and "f.name.trim() !== ITEM_NO_PROPERTY_NAME" in lib, \
        "去重判据不完整（须 id 或名，否则平台下发货号时会出两条）"
    #: 向导两条加载路径都要用同一份实现（DEF-044）。
    wiz = _code_only(_src(PUBLISH_MODAL))
    assert wiz.count("withItemNoFirst(") >= 2, \
        "向导里没有两条加载路径共用 `withItemNoFirst`（只改一处 ⇒ 另一条路径货号又消失）"


def test_math_rule_has_a_source_column_picker_limited_to_numeric():
    """运算类规则要选「计算基础列」：默认当前列、只列数值列（用户口径 2026-10-15）。

    用户原话：
    > 「涉及到计算时是需要一个前提条件为基于指定列，如果选择的规则是运算类的规则
    >   则需要指定计算的基础列，该列在表头列的设置图标 tip 弹窗中指定，默认为
    >   当前列，可选择指定为其它列，下拉只展示数值列，计算时为空的行默认为 0」
    """
    wiz = _code_only(_src(PUBLISH_MODAL))
    #: ① 只在**需要来源列的 op**出现（运算 = 计算基础列；复制 = 来源列）。
    #: ⚠️ 2026-10-22 换锚：判据从「枚举两个 op 字面量」改为**按 op 求
    #: `needsSourceColumn`**（现在覆盖 math_scale / copy_column / copy_extract）。
    assert 'pickedRule?.op === "copy_column"' in wiz and 'pickedRule?.op === "copy_extract"' in wiz, (
        "没有「哪些 op 要选来源列」的判据"
    )
    assert "{needsSourceColumn && (" in wiz, \
        "来源列下拉没有按 `needsSourceColumn` 包起来（提取类也会看到）"
    #: ② 默认当前列。
    assert "sourceColumn ?? column" in wiz, "基础列没有默认到当前列"
    #: ③ **运算类只列数值列**（判据是后端 `"numeric"`，写 `number` 会恒空且不报错）；
    #:    复制类列全部列；且**当前列必在选项里**（它是默认值）。
    #: ⚠️ 2026-10-22 换锚（用户口径 2026-10-20「**所有规则均支持选择数据源列**」）：
    #: 数据源列不再按 op 过滤成「只数值列」，`it.kind === "numeric"` / `isCopyRule`
    #: 两个分支**整体退场** ⇒ 选项 = 全部列。
    #: 判据改为：选项必须覆盖**全部列**且显示**中文表头名**（不是裸下标）。
    assert "const columnOptions = table.columns.map((name, i) => ({" in wiz, (
        "数据源列选项不是「全部列」（用户口径：所有规则都支持选数据源列）"
    )
    assert "label: name || `第 ${i + 1} 列`" in wiz, (
        "选项没显示表头名（会渲染成裸下标）"
    )
    #: ④ 读基础列、写当前列（「A 列 ×1.7 写到 B 列」靠这两处不同）。
    assert "needsSourceColumn ? (sourceColumn ?? column) : column" in wiz, \
        "执行时没有「读来源列」的分支 ⇒ 换列无效"
    assert "source_column: readCol" in wiz, "没有把基础列回传后端"


def test_run_picked_rule_deps_cover_everything_it_reads():
    """⚠️ `runPickedRule` 的 `useCallback` 依赖数组**必须覆盖它读到的每个 state**。

    用户实测的真 bug（2026-10-15）：
    > 「计算基础列指定的是原价列…最后得到的全是0」

    **根因**：`readCol = needsSourceColumn ? (sourceColumn ?? column) : column`
    读了两个 state，但依赖数组里**两个都没有** ⇒ 回调永远用**首次渲染时**的闭包值
    （`sourceColumn = null`）⇒ 用户选了「原价」，实际读的仍是**当前列**。
    若当前列恰好是空的（如「计算价格」）⇒ 整列按 0 参与运算 ⇒ **结果全是 0**，
    而界面上的提示行显示的是「按原价列计算」（提示读的是**新** state，与执行读的**旧** state 不一致）。

    ⚠️ 这类 bug 的特征是**静默**：不报错、不崩、提示还显示得对，只有结果错。
    ⇒ 必须用守卫钉住依赖项，而不是靠人眼复查（本轮就是人眼漏了）。
    """
    wiz = _code_only(_src(PUBLISH_MODAL))
    i = wiz.index("const runPickedRule = useCallback(")
    j = wiz.index("]);", i)
    body_and_deps = wiz[i:j]

    #: ① 回调体里**真的读了**这几个 state（否则本守卫是空转）。
    for read in ("needsSourceColumn", "sourceColumn", "table.rows", "pickedRows", "column"):
        assert read in body_and_deps, f"`runPickedRule` 没有读 `{read}`（守卫前提变了，需重审）"

    #: ② 依赖数组里**必须都列上**。
    deps = body_and_deps[body_and_deps.index("}, ["):]
    for name in ("sourceColumn", "needsSourceColumn", "pickedRows", "table.rows", "column"):
        assert name in deps, (
            f"`runPickedRule` 的依赖数组漏了 `{name}` ⇒ 回调会用陈旧闭包值"
            f"（实测后果：选了来源列也照读当前列 ⇒ 结果全 0）"
        )


def test_sku_image_import_uses_the_match_result_not_the_dead_regex_path():
    """⚠️ **SKU 图导入的候选必须来自「第 4 步匹配结果」**，不是被推翻的正则解析路线。

    用户实测 bug（2026-10-15）：
    > 「点击确认导入**没有上传**第一步预检通过的 sku 数据对应的图片」

    根因：`passedSkuRows(...)` 的候选参数传的是
    `localSkuImageList(媒体, DEFAULT_SKU_IMAGE_PATTERN)`（**按正则从文件名解析**），
    而那条路线已随「正则方案推翻」整体移除 ⇒ 那份列表**恒为空** ⇒ `row.localRel`
    恒空 ⇒ `plan.images` 为空 ⇒ **一张规格图都不上传**，而且**不报错**（静默）。

    修法：候选改为 `matchedSkuImages`（第 4 步「SKU 图自动匹配」的产物）。
    本守卫钉两件事：① 用的是 `matchedSkuImages`；② **不再出现** `DEFAULT_SKU_IMAGE_PATTERN`
    参与 `passedSkuRows` 的形态（防退回老路）。
    """
    src = PUBLISH_MODAL.read_text(encoding="utf-8")
    i = src.find("const wizardLocalImages = useMemo(")
    assert i > 0, "找不到 wizardLocalImages"
    block = src[i : i + 900]
    assert "matchedSkuImages" in block, "SKU 图候选没接第 4 步匹配结果（会静默不上传图）"
    assert "localSkuImageList" not in block, "SKU 图候选又走回了被推翻的正则解析路线"
    #: 再钉一层：`passedSkuRows` 拿到的必须是这份候选（不是别的变量）。
    j = src.find("passedSkuRows(")
    assert j > 0
    assert "wizardLocalImages" in src[j : j + 400], "passedSkuRows 没接上 SKU 图候选"


@pytest.mark.skip(reason="已退役（2026-10-22 清理）：被测能力「规格分类固定列」于 2026-10-20 由用户口径整体移除（SPEC_CATEGORY_COL / withSpecCategoryColumn / specCategoriesFromColumn 全删，无兼容层；AGENTS.md §5 页面条目有记载）。替代口径 = 导入弹窗「商品 SKU 映射关系」左侧改为「预检通过列」（表格全部列）。")
def test_spec_category_column_is_added_to_the_sku_table():
    """第一步 SKU 表要**新增「规格分类」列**（固定第 2 位/数据列第 0 位，默认空）。

    用户口径 2026-10-15：
    > 「再第一步商品sku表格的**第二列新增一个规格分类默认为空**」
    """
    lib = PRECHECK_MAP.read_text(encoding="utf-8")
    assert 'export const SPEC_CATEGORY_COL = "规格分类"' in lib
    assert "export function withSpecCategoryColumn" in lib
    #: ⚠️ 插列必须**同步插 kind** —— `column_kinds` 与 `columns` 等长是既有契约，
    #: 不同步插会让下标整体错位（「数值列」判据张冠李戴）。
    i = lib.find("export function withSpecCategoryColumn")
    body = lib[i : i + 900]
    assert "column_kinds" in body, "插列没同步插 column_kinds（数值列判据会错位）"
    #: 幂等：已有该列就不动。
    assert "includes(SPEC_CATEGORY_COL)" in body, "插列不是幂等的（重复插会错位）"

    src = PUBLISH_MODAL.read_text(encoding="utf-8")
    #: ⚠️ 锚点已重定向（2026-10-15）：插列入口从 `withSpecCategoryColumn` 改为
    #: **`withDerivedColumns`**（一次插「SKU 图 + 规格分类」两列后处理固定列）。
    assert "withDerivedColumns(" in src, "向导层没真的调用插列"


@pytest.mark.skip(reason="已退役（2026-10-22 清理）：被测能力「规格分类固定列」于 2026-10-20 由用户口径整体移除（SPEC_CATEGORY_COL / withSpecCategoryColumn / specCategoriesFromColumn 全删，无兼容层；AGENTS.md §5 页面条目有记载）。替代口径 = 导入弹窗「商品 SKU 映射关系」左侧改为「预检通过列」（表格全部列）。")
def test_next_step_blocks_on_empty_spec_category_for_picked_rows():
    """点「下一步」要**校验「规格分类」列非空**（用户口径：只查勾选行）。"""
    src = PUBLISH_MODAL.read_text(encoding="utf-8")
    i = src.find("specCategoryColumnIndex(tables[0]")
    assert i > 0, "下一步没有校验规格分类列"
    block = src[i : i + 1200]
    assert "skuTableSelection.picked" in block, "校验没按勾选行收口"
    assert "skuTableSelection.blocked" in block, "校验没排除预检未通过的行"
    assert "message.warning" in block, "校验不过要提示（不是静默 return）"


@pytest.mark.skip(reason="已退役（2026-10-22 清理）：被测能力「规格分类固定列」于 2026-10-20 由用户口径整体移除（SPEC_CATEGORY_COL / withSpecCategoryColumn / specCategoriesFromColumn 全删，无兼容层；AGENTS.md §5 页面条目有记载）。替代口径 = 导入弹窗「商品 SKU 映射关系」左侧改为「预检通过列」（表格全部列）。")
def test_mapping_sources_come_from_the_spec_category_column():
    """映射弹窗左候选 = **第一步「规格分类」列去重后的值**（不是从 sku 名称拆前缀）。"""
    lib = PRECHECK_MAP.read_text(encoding="utf-8")
    assert "export function specCategoriesFromColumn" in lib
    i = lib.find("export function specCategoriesFromColumn")
    body = lib[i : i + 1400]
    assert "seen" in body, "候选没去重（用户口径：去重后下拉展示）"

    src = PUBLISH_MODAL.read_text(encoding="utf-8")
    assert "specCategoriesFromColumn(" in src, "向导层没接新候选"
    #: ⚠️ 必须**剥掉注释再判** —— 旧函数名会被「旧实现是它、已取代」这类说明
    #: **刻意留在注释里**（那是有用的考古信息，不该为了测试删掉）。
    #: 扫带注释的全文 ⇒ 守卫打红的是自己的注释，与真实调用无关（哑弹的反面：
    #: 它红得对不上被测行为，改代码的人会以为守卫坏了）。
    code = _strip_comments(src)
    assert "sourceSpecNames(filledSkuRows)" not in code, "还在用旧的「按冒号拆前缀」路线"


def test_copy_extract_op_is_wired_end_to_end():
    """`copy_extract`（复制 + 提取，两步）三处必须同集合。

    ⚠️ 后端支持、前端下拉没有 = 用户**根本选不到**（上一轮 `math_scale` 就是这么漏的）；
    前端有、后端不认 = 保存即 400。两处都要钉。
    """
    modal = (PUBLISH_MODAL.parent / "data-package-rules-modal.tsx").read_text(encoding="utf-8")
    assert '"copy_extract"' in modal, "规则弹窗下拉没有 copy_extract"
    assert "cx_step" in modal, "没有第二步（怎么提取）的表单"
    api = (ROOT / "frontend/src/lib/api.ts").read_text(encoding="utf-8")
    assert '"copy_extract"' in api, "api.ts 的 op 类型没有 copy_extract"

    import pathlib

    svc = pathlib.Path(
        "D:/work/XHS/backend/app/services/data_package_rule_service.py"
    ).read_text(encoding="utf-8")
    assert "OP_COPY_EXTRACT" in svc
    assert "def apply_copy_extract" in svc
    #: ⚠️ 提取步骤必须**复用**既有三个实现（DEF-044：不留两份算法）。
    i = svc.find("def apply_copy_extract")
    body = svc[i : i + 2000]
    for fn in ("apply_extract_segment", "apply_extract_between", "apply_extract_strip"):
        assert fn in body, f"复制+提取没复用 {fn}（另写了一份算法）"


def test_source_column_ops_match_backend():
    """⚠️ 前端「需要来源列」的 op 集合必须与后端 `OPS_NEED_SOURCE_COLUMN` 同集合。

    漏一个的后果：表头弹窗**不出现来源列下拉** ⇒ 规则静默按当前列执行
    （用户看到的是「规则没生效」，而不是任何报错）。
    """
    import pathlib

    model = pathlib.Path("D:/work/XHS/backend/app/models/data_package_rule.py").read_text(encoding="utf-8")
    i = model.find("OPS_NEED_SOURCE_COLUMN")
    backend_ops = {
        op
        for op in ("math_scale", "copy_column", "copy_extract")
        if op in model[i : i + 400]
    }
    wizard = PUBLISH_MODAL.read_text(encoding="utf-8")
    j = wizard.find("const needsSourceColumn =")
    front = {op for op in backend_ops if f'"{op}"' in wizard[j : j + 400]}
    assert front == backend_ops, f"前端 {front} != 后端 {backend_ops}"


def test_spec_mapping_is_a_table_with_two_rows_max_and_no_duplicates():
    """商品 SKU 映射关系 = **映射表**（不是「勾选 + 下拉」）。

    用户口径 2026-10-15：
    > 「应拿第一步预检通过的**规格分类列去重**与平台类目生成的商品规格下拉选择映射，
    >   且**不允许重复选择**，**最多两个映射关系**，**布局参考同一个弹窗中的其它部分**」
    """
    src = STEP2.read_text(encoding="utf-8")
    #: ⚠️ 区块边界用**两个 import-section** 卡，不用「价格库存映射关系表」这个**词**
    #: —— 我新写的注释里恰好提到它，用词定位会把区块截短（守卫红得对不上被测行为）。
    i = src.find('<div className="import-section-title">商品 SKU 映射关系</div>')
    assert i > 0, "找不到「商品 SKU 映射关系」区块"
    j = src.find('<div className="import-section-title">价格库存映射关系表</div>', i)
    assert j > i, "找不到下一个区块边界"
    block = src[i:j]

    #: ① 布局 = Table（与同弹窗的价格库存映射表同款）。
    assert "<Table" in block, "商品 SKU 映射关系不是表格布局（用户口径：参考同弹窗其它部分）"
    #: ② 两列 + 操作列。
    #: ⚠️ 2026-10-22 换锚（用户口径「导入弹窗左侧改为**预检通过列**」）：
    #: 左列不再是「规格分类」（那个固定列已整体移除），而是预检表格的全部列。
    assert '"预检通过列"' in block, "缺少「预检通过列」列"
    assert '"平台规格类型"' in block, "缺少「平台规格类型」列"
    #: ③ **最多两个映射关系**（用常量，不硬编码 2）。
    assert "MAX_SPEC_MAPPING_ROWS" in block, "没有条数上限（用户口径：最多两个）"
    #: ④ **不允许重复选择**：两个下拉各自 disabled 已选项。
    assert block.count("disabled: others.includes(") >= 2, "两列都要禁止重复选择"
    #: ⑤ 新增行两列都空（与价格库存一致，不设默认值）。
    assert '{ source: "", target: "" }' in block, "新增映射行应两列都空"
    #: ⑥ ⚠️ **空表也必须能加**（真 bug，用户实测 2026-10-15 截图：「没有点击添加映射
    #: 关系的按钮」）。根因：`+` 只挂在**行的操作列**上 ⇒ 零行时没有操作列，
    #: 用户看到「还没有映射，请点操作列的 + 添加」却找不到那个 +，功能等于不存在。
    assert "添加映射关系" in block, "空表时没有可点的添加入口（+ 只在行的操作列上）"
    assert "setImportMapping([{ source: \"\", target: \"\" }])" in block, (
        "空态的添加按钮没有真的加一行"
    )


def test_price_mapping_presets_four_required_rows_with_empty_sources():
    """价格库存映射：**预置 4 行**（SKU图/售价/市场价/库存），但**来源列全空**。

    用户口径 2026-10-15：
    > 「右侧的价格库存中，**SKU图、售价、市场价、库存**所有**必须配置**，
    >   弹窗打开时就**默认展示**，它们对应的**左侧下拉默认为空**展示『请选择』文本」

    ⚠️ 这条**取代**了此前那条 `test_price_mapping_has_no_default_rows`
    （它断言「初始必须是空数组」，那是「不预填**来源列**」的时期口径）。
    现在的口径把两件事**分开**：
      · **行**要预置（让用户知道这四项必须配）—— 本守卫钉这个；
      · **来源列**一律留空（不替他猜是哪一列）—— `DEFAULT_PRICE_MAPPING` 仍是空。
    """
    lib = PRECHECK_MAP.read_text(encoding="utf-8")
    #: ① 四项必修清单齐全（顺序按用户列举）。
    #: ⚠️ 取**代码体**而不是「从现在往后 400 字」—— 注释里也有方括号 /
    #: 目标名（如「推翻 2026-10-15 的『五项必配』」），切片会切到注释里，
    #: 让「缺 price」这类假红（实测踩过）。
    i = lib.index("export const REQUIRED_PRICE_TARGETS")
    #: ⚠️ 从 `= [` 之后开始切（类型标注 `readonly PriceTargetKey[]` 里也有 `[]`，
    #: 从声明处切会在那里被截断 —— 实测切出空串，导致「缺 price」假红）。
    eq = lib.index("= [", i) + 3
    block = lib[eq : lib.index("];", eq)]
    #: ⚠️ **用户口径 2026-10-21 推翻 2026-10-15 的「五项必配」**：
    #: > 「规格图不应在价格库存映射关系表中再次进行配置，应为在预检步骤中 SKU 图是
    #: >   固定列……SKU 名称也应无需再让用户配置映射关系，当用户配置完商品 SKU 映射
    #: >   关系指定的列去重后就是对该组的 sku 规格」
    #: ⇒ 必配只剩三项（售价 / 市场价 / 库存）；`sku_name` 与 `spec_image`
    #: **不许**再出现在预置行里（配两遍 = 两份真相源，必然漂移）。
    #: `block` 已经是 `= [` 与 `];` 之间（见上）⇒ 直接用，别再找方括号。
    items = block
    for required in ("price", "market_price", "stock"):
        assert f'"{required}"' in items, f"必修清单缺 {required}"
    for gone in ("sku_name", "spec_image"):
        assert f'"{gone}"' not in items, (
            f"{gone} 仍在必配行里 —— 它已由商品 SKU 映射 / 固定「SKU 图」列自动决定"
        )
    #: ② 预置行**只带目标、来源全空**。
    j = lib.find("export function defaultPriceMappingRows")
    body = lib[j : j + 300]
    assert "REQUIRED_PRICE_TARGETS.map" in body, "预置行不是按必修清单生成的"
    assert 'source: ""' in body, "预置行的来源列应为**空**（用户口径：默认展示「请选择」）"
    #: ③ 不预填来源的常量仍是空（两件事别混）。
    assert "DEFAULT_PRICE_MAPPING: readonly PriceMappingRow[] = [];" in lib, (
        "DEFAULT_PRICE_MAPPING 不该有内容（那是「预填来源列」，用户口径：不猜）"
    )

    #: ④ 弹窗真的用了预置行，且**每次打开都重置**（否则用户看到上次的半截配置）。
    src = STEP2.read_text(encoding="utf-8")
    i2 = src.find("const [priceMapping, setPriceMapping]")
    assert i2 > 0
    line = src[i2 : src.find(";", i2) + 1]
    assert "defaultPriceMappingRows()" in line, f"初始 state 没用预置行：{line[:200]}"
    assert "resetPriceMappingOnOpen" in src, "打开弹窗时没重置映射"


def test_confirm_import_checks_the_spec_image_mapping():
    """⚠️ 点「确认导入」要**校验 SKU 图的映射**（用户口径 2026-10-15）。

    > 「再点击确认导入映射关系时**校验 SKU图的映射关系**，不再走现在默认的逻辑，
    >   应走映射逻辑」

    ⚠️ 这条咬的是**规格图不再走「按名称猜」的默认路**：
      · 校验：四项必修（含 SKU图）没配来源列 ⇒ 按钮不给点 + 说清是哪几项；
      · 取值：`plan.images` 取 `specImageRel`（映射来的），**不取 `localRel`**（猜的）。
    """
    lib = PRECHECK_MAP.read_text(encoding="utf-8")

    #: ① 校验函数存在，且**列出是哪几项**没配（不是一句「有必填项」）。
    assert "export function missingRequiredPriceTargets" in lib
    i = lib.find("export function missingRequiredPriceTargets")
    body = lib[i : i + 700]
    assert "PRICE_TARGET_LABELS[target]" in body, "没报出**中文名**（用户不知道该配哪项）"

    #: ② 弹窗把校验接进 blockers（按钮 disabled 与提示同源）。
    src = STEP2.read_text(encoding="utf-8")
    assert "missingRequiredPriceTargets(priceMapping)" in src, "确认导入没校验必修项"
    assert "还没有选来源列" in src, "校验失败没给出可操作的提示"

    #: ③ ⚠️ **取值改走映射**：`plan.images` 取 `specImageRel`。
    j = lib.find("if (row.specImageRel)")
    assert j > 0, "plan.images 没按映射取（还在用猜出来的 localRel）"
    seg = lib[j : j + 400]
    assert "row.specImageRel" in seg
    #: 反证：那一段里**不得**再出现 `row.localRel`（走了老路）。
    assert "row.localRel" not in seg, "规格图取值仍走 `localRel`（默认逻辑，用户要求移除）"


def test_other_rules_block_is_removed():
    """「其它规则」整块移除（用户口径 2026-10-15：「其它规则的逻辑移除」）。"""
    src = STEP2.read_text(encoding="utf-8")
    code = _strip_comments(src)
    assert "其它规则" not in code, "「其它规则」区块还在"
    #: ⚠️ **不是删功能**：倍率仍有默认值，导入时照常算市场价。
    lib = PRECHECK_MAP.read_text(encoding="utf-8")
    assert "DEFAULT_PRICE_MULTIPLIER" in lib, "倍率默认值被误删（导入时会算不出市场价）"


def test_manual_column_add_and_drop_are_wired():
    """手动添加列 + 整列删除（用户口径 2026-10-15）。"""
    src = PUBLISH_MODAL.read_text(encoding="utf-8")
    lib = PRECHECK_MAP.read_text(encoding="utf-8")

    #: lib 侧：四个纯函数都在。
    for fn in ("addTableColumn", "dropTableColumn", "canDropColumn", "nextManualColumnKey"):
        assert f"export function {fn}" in lib, f"缺 {fn}"

    #: ① 表头删除按钮：**只有手动列给**（非手动/固定列不支持删除）。
    assert "canDropColumn((table.column_keys ?? [])[colIndex])" in src, (
        "删除按钮没按「是否手动列」判可见性（原始列也会被给到删除按钮）"
    )
    #: ② 添加列入口 + 弹窗（中文名 + 英文名，英文名非必填）。
    assert "添加列" in src, "没有「添加列」入口"
    assert "nextManualColumnKey(" in src, "英文名没有「留空自动生成」的兜底"
    assert "请填写表头中文名" in src, "中文名没有必填校验"
    #: ③ 二次确认要**点名**会连带删掉的配置（用户原话：会同步删除已经映射使用的 XXX）。
    assert "会同步删除已经映射使用的" in src, "删除列没有连带提示（用户口径的点名要求）"


def test_column_drop_warning_is_fed_by_real_mapping_drafts():
    """⚠️ 删列的连带提示必须由**第二步的真实映射草稿**喂 —— 不能是写死的文案。

    用户口径 2026-10-15：「如果该列**在第二步已经被映射使用**，删除该列后二次确认
    提示会**同步删除已经映射使用的 XXX 配置参数**」。

    ⚠️ 这条守卫咬的是**接线**：`TablesPanel` 收到的 `specImportSources` / `priceSources`
    必须来自向导层的 `mappingDraft`（而它由第二步 `onMappingDraftChange` 上报）。
    只断言「页面上有那句话」是哑弹 —— 文案在、但永远走不到「点名」分支。
    """
    src = PUBLISH_MODAL.read_text(encoding="utf-8")
    step2 = STEP2.read_text(encoding="utf-8")

    #: ① 第二步要上报草稿。
    assert "onMappingDraftChange" in step2, "第二步没有上报映射草稿"
    #: ② 上报的依赖数组必须含 `onMappingDraftChange`（漏了 ⇒ 永远报首次渲染那份）。
    i = step2.find("props.onMappingDraftChange?.({")
    assert i > 0
    #: ⚠️ **依赖数组要精确取**：从 `}, [` 起**截到该行行尾** ——
    #: 用固定窗口长度会随新增字段变长而截断（取 400/900 都不够，
    #: 守卫会红得「对不上被测行为」）。依赖数组本身一定在一行里。
    tail = step2[i:]
    dep_start = tail.find("}, [")
    assert dep_start > 0, "找不到上报 effect 的依赖数组"
    dep = tail[dep_start : tail.find("\n", dep_start)]
    assert "props.onMappingDraftChange]" in dep, f"上报 effect 依赖数组漏了回调：{dep}"
    #: ③ 回落列也要在依赖里（漏了 ⇒ 提交体拿到空的回落列，取值错）。
    assert "priceFallbackSource" in dep, f"依赖数组漏了回落列：{dep}"
    #: ③ 向导层接住并透传给第一步表格。
    assert "onMappingDraftChange={setMappingDraft}" in src, "向导层没接住草稿"
    assert "specImportSources={mappingDraft.specSources}" in src, "草稿没接到表格"
    assert "priceSources={mappingDraft.priceSources}" in src, "价格来源草稿没接到表格"


def test_both_mapping_tables_can_add_from_an_empty_state():
    """⚠️ 两张映射表在**空表**时都必须有可点的添加入口。

    真 bug（用户实测 2026-10-15，截图现象：「没有点击添加映射关系的按钮」）：
    我把「商品 SKU 映射关系」从「勾选 + 下拉」换成表格时，`+` 挂在**行的操作列**上
    —— 零行时**没有操作列**，那句「还没有映射，请点操作列的 + 添加」指向一个
    不存在的东西，用户**被卡死**（功能等于不存在）。
    价格库存表本来靠默认值遮住了这个洞；本轮去掉默认值后**两张表同时暴露**。

    ⚠️ 判据分两层：① 空态文案里**有按钮**（不是纯文本引导）；② 按钮**真的加一行**。
    只断言文案（「添加映射关系」这个词在）是哑弹 —— 一个 `disabled` 的按钮也满足它。
    """
    src = STEP2.read_text(encoding="utf-8")

    for label, state, seed in (
        ("商品 SKU 映射关系", "setImportMapping", '{ source: "", target: "" }'),
        ("价格库存映射关系表", "setPriceMapping", '{ target: "", source: "" }'),
    ):
        #: 每个 `emptyText` 后面紧跟的内容里必须有一个 onClick 真加行。
        hits = [
            m.start()
            for m in __import__("re").finditer(r"emptyText:", src)
        ]
        assert hits, "两张表都没有 emptyText（守卫锚点失效）"
        found = False
        for i in hits:
            window = src[i : i + 700]
            if f"{state}([ {seed} ])" in window or f"{state}([{seed}])" in window:
                found = True
                break
        assert found, f"「{label}」空表时没有可点的添加按钮（用户会被卡死）"


def test_derived_fixed_columns_are_not_rendered_twice():
    """⚠️ 「SKU 图」「规格分类」是**固定列**，只能**渲染一次**。

    用户实测截图（2026-10-15）：表头出现**两列「SKU 图」** —— 表格第一列
    （`__matched_image`，固定列）与新插进 `table.columns` 的同名列**各渲染一次**。

    根因：把这两列插进 `columns`/`rows`（**为了能被映射/规则取值**）之后，
    `...table.columns` 展开时会**再渲染一遍**。

    修法：展开时**只过滤「SKU 图」**（用户口径 2026-10-15 两次纠正）：
      · **SKU 图** —— 由**固定列**渲染（要缩略图 + 切换图标，统一渲染做不到）⇒ 跳过；
      · **规格分类** —— **不跳过**，走**统一的数据列渲染**（用户口径：
        「规则分类列恢复到原来历史 **默认为空支持表头的设置图标**」）——
        统一渲染自带表头设置图标，与截图里的历史形态一致。
    ⚠️ **过滤的是渲染，不是数据** —— 两列仍在 `columns`/`rows` 里。
    """
    src = PUBLISH_MODAL.read_text(encoding="utf-8")
    #: ① 展开列时**只**过滤 SKU 图（不能连规格分类一起滤掉 —— 那样它就不显示了）。
    i = src.find("...table.columns")
    assert i > 0, "找不到列展开处"
    seg = src[i : i + 400]
    assert "SKU_IMAGE_COL" in seg, "展开列时没过滤「SKU 图」⇒ 屏幕上会出现两列同名"
    assert "SPEC_CATEGORY_COL" not in seg, (
        "把「规格分类」也过滤掉了 ⇒ 它彻底不显示（用户口径：恢复到历史，带表头设置图标）"
    )
    #: ② 第一列必须**读 rows**（不是只读渲染层状态 —— 那样它就不在表格数据里）。
    j = src.find('key: "__matched_image"')
    assert j > 0
    #: ⚠️ 2026-10-22：列定义块变长（自建勾选列 + 红框 onCell），窗口从 1400 放到 3000。
    blk = src[j : j + 3000]
    assert "skuImageColOf(table)" in blk, "第一列没从 rows 取「SKU 图」列"
    #: ⚠️ 2026-10-22 换锚：取行值改为经 `const rel = skuImageColOf(table);` 再读
    #: `table.rows[record.__index]?.[rel.index]`（多了可选链）。
    #: 判据不变：**必须读 rows**（不是只读渲染层状态，否则它不在表格数据里）。
    assert "table.rows[record.__index]" in blk, "第一列没读 rows（还是两处真相源）"
    #: ③ 预检列里的「选择本地图」**按钮**已移除（第一列那枚图标是唯一入口）。
    #: ⚠️ 判据用**按钮的调用形态**而不是裸词 —— 说明注释里会提到这个按钮名
    #: （「已移除，因为…」那是有用的考古信息，不该为了测试删掉）。
    assert "选择本地图\n                      </Button>" not in src, (
        "「选择本地图」按钮还在（与第一列图标重复）"
    )
    #: 反证：说明里的**状态提示**要留着（用户据此知道去第一列点图标）。
    assert "未匹配到本地图" in src, "状态提示被误删（用户没信号了）"


def test_spec_type_toggle_clears_that_specs_sku_data():
    """⚠️ 取消勾选规格类型时，要**连带清除该规格的全部 SKU 数据**。

    用户口径 2026-10-15：
    > 「价格库存表格是根据商品规格**联动**的，规格**新增**时价格库存表格实时联动，
    >    **删除**规格时依旧实时联动，当商品规格**切换**时被切换到的规格类型
    >    对应的数据**也应同步被清除**」
    > 「该规格下**全部 sku 数据**，包括商品规格与商品规格关联的
    >    **价格库存表中的关联数据**」

    ⚠️ 旧实现只 `setSpecTypes(filter)` —— **只摘掉类型名**，规格值 / 规格图 /
    价格库存行全留着 ⇒ 用户看到「取消勾选了，规格值还在、价格表还占着行」。

    ⚠️ 本守卫钉**三块都要清**（少清一块就是孤儿数据）：
      · `specValues[name]` —— 该规格的值；
      · `specImages[name]` —— 按「维度+值」存的图（不清 ⇒ 下次同名维度**复用到旧图**）；
      · `specImageEnabled[name]` —— 规格图开关（不清 ⇒ 下次勾回继承旧状态）。
      · 价格库存行**不手工删**：重建 effect 按新 `specTypes` 重算笛卡尔积，
        被切走的维度不再参与组合 ⇒ 那批行自然消失。
    """
    src = STEP2.read_text(encoding="utf-8")
    i = src.find("const toggleSpecType = useCallback(")
    assert i > 0, "找不到 toggleSpecType"
    body = src[i : i + 2200]
    for key in ("setSpecValues", "setSpecImages", "setSpecImageEnabled"):
        assert key in body, f"取消勾选时没清 `{key}`（留孤儿数据）"
    #: ⚠️ 反向：**不许**手工删价格库存行（会与重建 effect 打架）。
    assert "onPriceRowsChange" not in body, (
        "toggleSpecType 里手工改了价格库存行 —— 应交给重建 effect 按新 specTypes 重算"
    )

    #: ⚠️ 联动靠重建 effect 的依赖数组 —— 漏 `specTypes`/`specValues` 就不联动了。
    j = src.find("}, [specTypes, specValues, props.priceRows")
    assert j > 0, "重建 effect 的依赖数组漏了 specTypes / specValues（规格变化不联动）"


def test_item_no_goes_to_the_top_level_field_not_into_attributes():
    """⚠️ **货号必须走顶层 `article_no`，不许塞进 `attributes[]`**。

    用户报障 2026-10-15：
    > 「相同的商品信息和类目，在平台后台商品属性中的其他属性下**存在货号**，
    >    但是本地项目上传**提示无货号属性**」

    ⚠️ 两条独立的错，**必须同时成立**才算修好：

    ① **不许进 `attributes[]`** —— 「货号」是前端**自己合成**的 UI 项
       （平台 `desc_properties` 不下发它，见 `buildItemNoProperty`）。
       塞进 `attributes[]` ⇒ 平台把它当成一个 `attributeId` = `articleNo`
       的**类目属性**，而类目里不存在这个属性 ⇒ 报
       「当前类目不存在[货号]属性，请刷新页面后再试」，**整个发布被拒**。
       ⇒ 判据：第二步组 `attributes` 时用了 `withoutItemNo`。

    ② **必须填顶层 `article_no`** —— 真机 `itemData[0].articleNo`。
       后端 `ItemPublishRequest.article_no` 一直收着它，但**前端从没传过** ⇒
       提交体里永远没有货号（`skus[].erpCode` 是**商家编码**，两回事）。
       ⇒ 判据：提交调用里带上了 `article_no`，且空值时不传。
    """
    #: ── ① 不许进 attributes[] ──
    step2 = STEP2.read_text(encoding="utf-8")
    assert "withoutItemNo(props.otherProperties" in _code_only(step2), (
        "第二步组 attributes 时没排除货号 —— 平台会报「当前类目不存在[货号]属性」"
    )

    #: ── ② 必须填顶层 article_no ──
    wizard = PUBLISH_MODAL.read_text(encoding="utf-8")
    i = wizard.find("await publishShopItem({")
    assert i > 0, "找不到发布提交调用"
    call = wizard[i : i + 2600]
    assert "article_no" in call, (
        "提交体没带货号 —— 后端 ItemPublishRequest.article_no 收不到值（货号丢失）"
    )
    #: ⚠️ 空值**不传**（与「缺值留空不造假」同口径；后端 `article_no or None`）。
    assert "itemNo.trim()" in call, (
        "货号没有做空值判断 —— 空串应**不传**该键，而不是传空串"
    )
    #: ⚠️ 反向：**不许**在 attributes 里再塞一条货号（两条路 = 平台必拒）。
    assert "attributeId: ITEM_NO_PROPERTY_ID" not in _code_only(step2), (
        "又把货号塞进 attributes[] 了 —— 这是平台报错的直接成因"
    )


def test_reload_and_full_unspec_and_target_list_consistency():
    """三条用户报障（2026-10-15）的守卫 —— 一次咬三处，各自独立断言。

    ── 一、「重新读取资源后，SKU 导入的数据**未清空**」──
    根因：第二步的 div **没有 `key={openSeq}`**（只有第一步挂了）⇒ `loadWithPath`
    换数据包时第二步**不重挂** ⇒ 内部 SKU 导入产物（`specTypes` / `specValues` /
    `specImages` / `specImageEnabled` / `pendingSpecFills`）整批残留。

    ── 二、「所有商品规格全部取消后，价格及库存列表关联的数据**未清空**」──
    根因：重建 effect 里 `if (next.length === 0) return;` —— 这句是为「平台还没生成
    规格、外层给了一行裸价占位」设的，却把「用户把规格全取消」也一起放过 ⇒
    外层 `priceRows` 保留上一轮的行。

    ── 三、「价格库存列下拉框与价格及库存存在**不一致的列**」──
    根因：下拉用了 `PRICE_TARGET_OPTIONS` **全量**（那是「导入所需 target 总集」），
    多出 `price_fallback`（售价回落列）与 `spec_category`（规格分类）—— 两者都是
    **规则参数**，不是价格库存表的列。
    """
    wizard = PUBLISH_MODAL.read_text(encoding="utf-8")
    step2_src = STEP2.read_text(encoding="utf-8")
    lib = (ROOT / "frontend/src/lib/precheck-publish.ts").read_text(encoding="utf-8")

    #: ── 一：换数据包要强制重挂第二步 + 复位向导层的 priceRows ──
    i = wizard.find("const loadWithPath = useCallback(")
    assert i > 0, "找不到 loadWithPath"
    body = wizard[i : i + 4000]
    #: ⚠️ **判据已改**（2026-10-15）：换数据包**不再靠 `setOpenSeq` 重挂第二步**
    #: —— 那会把第一步 `PrecheckStep`（同一个 key）一起重挂，实测自伤
    #: （两行资源选择框 + 「正在读取」永不消失）。现改用递增 `resetToken` 信号。
    assert "setStep2ResetToken" in body, (
        "换数据包没递增 resetToken ⇒ 第二步收不到清空信号（SKU 导入数据残留）"
    )
    assert "setPriceRows" in body, "换数据包没复位 priceRows（价表残留上一轮的行）"
    #: ⚠️ **「第二步挂 key 重挂」已被推翻**（2026-10-15 实测自伤：会把第一步
    #: `PrecheckStep` 一起重挂 ⇒ 两行资源选择框 + 「正在读取」永不消失）。
    #: 现判据在上面（递增 `resetToken`）与 `test_reload_clears_step2_without_remounting_step1`。
    assert not re.search(r'key=\{openSeq\}\s+style=\{\{ display: step === 1', wizard), (
        "第二步又挂回 key={openSeq} 了（换数据包会把第一步一起重挂，自伤复现）"
    )

    #: ── 二：规格全取消 ⇒ 清空（且只在 specTypes 为空时清，别误伤「值还没填」）──
    assert "specTypes.length === 0 && props.priceRows.length > 0" in _code_only(step2_src), (
        "规格全取消时没清价格库存行 —— 仍会残留上一轮的行"
    )

    #: ── 三：下拉用「价格库存表列」清单，不是全量 ──
    #: ⚠️ **必须查「下拉真的用了它」**，不能只查「名字出现过」—— 只查名字时，
    #: 变异「`options={PRICE_TABLE_TARGETS.map` 改回 `PRICE_TARGET_OPTIONS.map`」
    #: 会被 import 那一行满足 ⇒ **ESCAPED**（实测咬到过一次）。
    assert re.search(r"options=\{PRICE_TABLE_TARGETS\.map", _code_only(step2_src)), (
        "价格库存列下拉没改用 PRICE_TABLE_TARGETS（仍会列出不是表列的项）"
    )
    #: ⚠️ 反向：这张表的下拉**不许**再用全量清单。
    assert not re.search(r"options=\{PRICE_TARGET_OPTIONS\.map", _code_only(step2_src)), (
        "价格库存列下拉用回了 PRICE_TARGET_OPTIONS 全量"
    )
    #: ⚠️ 从 `= [` 起截到配对的 `];` —— 不能写 `[^\]]*]`：类型注解
    #: `PriceTargetKey[]` 里就有 `]`，正则会**在类型处就停**（实测咬过一次）。
    m = re.search(r"PRICE_TABLE_TARGETS[^=]*=\s*\[(.*?)\];", lib, re.S)
    assert m, "precheck-publish.ts 里没有 PRICE_TABLE_TARGETS 定义"
    block = m.group(1)
    #: ⚠️ 两条负向：规则参数类 target **不许**混进这张表的下拉。
    assert '"price_fallback"' not in block, "售价回落列不该出现在价格库存列下拉里"
    assert '"spec_category"' not in block, "规格分类不该出现在价格库存列下拉里"
    #: ⚠️ 2026-10-22 **口径修订**（用户原话 2026-10-21）：
    #: 「**移除下拉中的 SKU 图和 SKU 名称**」+「价格库存列的下拉框中应包含
    #:   **当前 价格及库存全部固定列**」。
    #: ⇒ `sku_name` / `spec_image` 从「必须存在」改为**必须不存在**
    #:   （它们在预检阶段就是固定列，再配一遍 = 两份真相源）。
    for t in ("price", "current_stock", "stock", "market_price", "spec_id", "barcode", "seller_code"):
        assert f'"{t}"' in block, f"价格库存列下拉漏了 {t}"
    for banned in ("sku_name", "spec_image"):
        assert f'"{banned}"' not in block, f"下拉里不该有 {banned}（用户口径：移除 SKU 图和 SKU 名称）"


def test_reload_clears_step2_without_remounting_step1():
    """⚠️ 换数据包清第二步 **不许重挂第一步**（2026-10-15 自伤修复的守卫）。

    用户报障（2026-10-15）：
    > 「加载完数据后展示**两行资源选择框**，「正在读取本地数据包…」文本也一直展示」

    ⚠️ **根因是我自己引入的**：上一版给第二步的 div 挂 `key={openSeq}` 来强制重挂、
    清掉 SKU 导入产物。但 **`openSeq` 同时挂在第一步 `PrecheckStep` 上** ⇒ 自增时
    **第一步也被重挂** ⇒ 它被打回「还没有加载数据包」的初始态（多的那行资源选择框），
    而向导层的 `loading` 还在转 ⇒ 「正在读取」永不消失。

    ⇒ 判据两条：
      ① **第一步的 key 仍绑 `openSeq`**（关弹窗才清，这是既有正确行为）；
      ② **第二步不绑 key**，改用递增的 `resetToken` 信号，由第二步自己清。
    """
    wiz = PUBLISH_MODAL.read_text(encoding="utf-8")
    code = _code_only(wiz)

    #: ① 第一步：`key={openSeq}` 仍在（关弹窗清空这一步不能丢）。
    assert "key={openSeq}" in code, "第一步的 key={openSeq} 丢了（关弹窗不再清空内部 state）"

    #: ② 第二步：**不许**挂 key（挂了就是重挂，会把第一步一起带走）。
    assert not re.search(r'key=\{openSeq\}\s+style=\{\{ display: step === 1', wiz), (
        "第二步又挂回 key={openSeq} 了 —— 换数据包时会把第一步一起重挂（自伤复现）"
    )
    #: ⚠️ **查「真的递增了」而不是「名字出现过」** —— 只查名字时，
    #: 变异「把 `setStep2ResetToken(...)` 整行删掉」照样绿（实测 ESCAPED）。
    assert re.search(r"setStep2ResetToken\(\(n\) => n \+ 1\)", code), (
        "换数据包没**真的**递增 resetToken（第二步收不到信号）"
    )
    #: 且它必须挂在 **loadWithPath 读取成功之后**（读失败不该清用户已配的东西）。
    #: ⚠️ 窗口**从函数起点截到它的依赖数组**（不能写死长度 —— 函数会随注释变长，
    #: 写死 2600 时实测越过了函数体，断言恒假）。
    i0 = wiz.find("const loadWithPath = useCallback(")
    end0 = wiz.find("\n  );", i0)
    body0 = wiz[i0:end0] if end0 > i0 else wiz[i0 : i0 + 4000]
    assert "setStep2ResetToken" in body0, "resetToken 没在 loadWithPath 里递增"

    #: ③ 第二步自己响应信号，且**首次挂载不清**（否则弹窗一打开就清空用户配的东西）。
    step2 = STEP2.read_text(encoding="utf-8")
    #: ⚠️ 同理：必须查 **依赖数组里真的有 `props.resetToken`** ——
    #: 变异「把 `[props.resetToken]` 改回 `[]`」时，只要别处提过这个名字就查不出来（ESCAPED）。
    assert "}, [props.resetToken]);" in _code_only(step2), (
        "第二步的 effect 没把 props.resetToken 放进依赖数组 ⇒ 收到信号也不会清"
    )
    i = step2.find("props.resetToken")
    body = step2[max(0, i - 900) : i + 900]
    assert "lastResetTokenRef" in body, (
        "第二步没做「首次不清」保护 —— 弹窗一打开就会把已配的规格/价格清掉"
    )
    assert "clearImportTargets()" in body, "第二步收到信号后没清 SKU 导入产物"


def test_workflow_feature_stays_removed():
    """⚠️ **自动化工作流整条特性已退场，不许回来**（2026-10-15 立的守卫）。

    背景：`1e09bf7`（2026-10-07）已按用户口径「把工作流相关逻辑表等完整移除，
    直接使用定时任务完成即可」**整条删除**（5 张表 + 5 个后端模块 + 前端页 +
    测试 + job_registry 项），退场理由实测三条硬缺口（详见该提交信息）。

    ⚠️ **2026-10-15 实测复发过**：提交未跟踪文件时**没逐个甄别**，
    把这 7 个已删文件又加回了库（还顺带把它的建表迁移挂回主链 ⇒ 迁移图里
    出现已退场特性的建表迁移，与 drop 迁移语义矛盾）。
    ⇒ 本守卫钉住：**文件不存在 + 迁移不在链上**。
    """
    #: ① 文件不许存在。
    gone_files = [
        "backend/alembic/versions/q8r9s0t1u2v3_workflows.py",
        "backend/app/api/platforms/xhs/workflows.py",
        "backend/app/models/workflow.py",
        "backend/app/services/workflow_engine.py",
        "backend/app/services/workflow_nodes.py",
        "backend/app/services/workflow_service.py",
        "tests/backend/test_workflows.py",
    ]
    for rel in gone_files:
        assert not (ROOT / rel).exists(), (
            f"已退场的自动化工作流文件又回来了：{rel} —— "
            "该特性已按用户口径整条移除，不要重新引入"
        )

    #: ② ORM 里不许有 workflow 表（否则 `create_all` 会把 drop 掉的表建回来，
    #:    这是本项目记过的坑：迁移 drop 了但 ORM 类还在 ⇒ 一启动表又有了）。
    import sys

    sys.path.insert(0, str(ROOT))
    from backend.app.core.database import Base
    import backend.app.models  # noqa: F401  —— 触发模型注册

    wf = [t for t in Base.metadata.tables if "workflow" in t]
    assert not wf, f"ORM 里还有工作流表（create_all 会把它们建回来）：{wf}"

    #: ③ 迁移图必须是**单 head**（工作流建表/删表迁移不应挂回主链）。
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(ROOT / "backend/alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "backend/alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1, f"迁移图不是单 head：{heads}"

    #: ④ 源码里不许再有对已删模块的 import（防止半删状态）。
    for mod in ("models.workflow", "services.workflow_engine", "services.workflow_nodes",
                "services.workflow_service", "xhs.workflows"):
        assert mod not in _code_only(_read(ROOT / "backend/app/main.py")), (
            f"main.py 还在 import 已删的工作流模块：{mod}"
        )


def test_llm_progress_card_is_not_mounted_on_shop_items():
    """商品列表**不展示 LLM 生成进度表**（用户口径 2026-10-18：*「不展示进度表」*）。

    ## 为什么删而不是修

    进度卡把「待处理」算成 `(snapshot.running || 0) + (snapshot.queued || 0)`，
    但底座 `note_queue_service.snapshot()` 的契约是 `running` = **对象或 None**、
    `queued` = **数组**（列表）—— 两者用 `+` 相加得到 `[object Object]`，
    界面上就出现「待处理 [Object Object]」+ 进度条恒 0%。
    该卡片已被用户明确要求移除，**判据是「不许再加回来」**。

    ⚠️ `llmQueueItemIds` 本身**不能删**：它还驱动操作列「LLM 生成」按钮的
    旋转图标（待处理态的唯一表达）。
    """
    from pathlib import Path

    page = (
        Path(__file__).resolve().parents[2]
        / "frontend/src/pages/platforms/xhs/shop-items-page.tsx"
    ).read_text(encoding="utf-8")
    #: 剥注释后再判（本文件顶部的「已移除（保留备查）」注释里写了组件名）
    import re

    code = re.sub(r"/\*.*?\*/", "", page, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)

    assert "<LlmGenerateProgressCard" not in code, "商品列表又挂回 LLM 生成进度表了"
    assert 'from "./llm-generate-progress-card"' not in code, "进度表 import 还在"
    #: 待处理态仍要能被感知（不许连旋转图标一起删掉）
    assert "llmQueueItemIds.has(item.item_id)" in code, "操作列不再标记「生成中」"


def test_precheck_image_fetch_is_deduplicated_at_the_source():
    """同一张暂存包图**只取一次** —— 修在 `fetchPrecheckImage` 源头。

    ⚠️ 真机抓包实测（2026-10-22，用户提供）：1036 个请求 / 825 MB 传输，
    而数据包里只有 24 张 SKU 图（约 29 MB）⇒ **约 28 倍重复**。
    典型样本：`SKU图_08_奇遇童话08.jpg`（1,392 KB）被并发请求 **21 次**，
    耗时 8.30s→7.92s→…→1.25s→441ms **递减**（排队特征）。

    根因不是某一处调用点写错，而是 `fetchPrecheckImage` 有多个调用点
    （缩略图 / 预览 / 上传取字节 …），互相不知道对方取过；antd 虚拟表格
    滚动时行组件还会反复卸载重挂。

    ⇒ 判据必须落在**源头唯一性**上，逐个堵调用点会漏（新增调用点就复发）：
      1. 缓存键 = `token + rel_path`；
      2. 缓存的是 **Promise**（并发也能合并，Map<url> 挡不住并发窗口）；
      3. 失败必须摘除缓存（否则这张图永远拿不到，重试也命中失败态）；
      4. objectURL **不得**在下游 revoke（共享资源，revoke 会坑其他持有者 ——
         表现为「滚走再滚回来图变白」）。
    """
    api_ts = (ROOT / "frontend/src/lib/api.ts").read_text(encoding="utf-8")

    #: ① 缓存容器必须存在且以 Promise 为值
    assert "precheckImageCache = new Map<string, Promise<string>>()" in api_ts, (
        "取图缓存必须是 Map<promise>：Map<url> 只在**已完成**后才命中，"
        "并发请求仍会全部打到后端（正是 21 次重复请求的形态）。"
    )
    #: ② 命中即返回，不得先发请求
    assert "if (cached) return cached;" in api_ts, (
        "`fetchPrecheckImage` 必须先查缓存再发请求，否则缓存形同虚设。"
    )
    #: ③ 失败摘除（⚠️ 必须锚在 `catch` 块里 —— `releasePrecheckImages` 里也有
    #: 同名字的 delete，只搜字符串会被它顶替成假绿，变异实测 ESCAPED 过）
    catch_block = api_ts.split("  } catch (error) {", 1)
    assert len(catch_block) == 2, "`fetchPrecheckImage` 必须有 catch 块处理失败"
    catch_body = catch_block[1].split("}", 1)[0]
    assert "precheckImageCache.delete(key);" in catch_body, (
        "取图失败必须从缓存摘除，否则该图永久卡在失败态、重试也拿不到。"
    )
    #: ④ 有集中释放入口
    assert "function releasePrecheckImages(token: string)" in api_ts, (
        "缓存必须有一个按 token 整批释放的出口，否则 objectURL 永久泄漏。"
    )
    assert "releasePrecheckImages(token);" in api_ts.split(
        "export async function discardPackagePrecheck"
    )[1], "丢弃暂存包时必须释放该 token 下的全部 objectURL。"

    #: ⑤ 下游任何人都不许对共享 objectURL 调 revoke
    wizard = (
        ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx"
    ).read_text(encoding="utf-8")
    wizard_code = _strip_comments(wizard)
    offenders = [
        f"{i}: {line.strip()}"
        for i, line in enumerate(wizard_code.splitlines(), 1)
        if "revokeObjectURL" in line
    ]
    assert not offenders, (
        "product-publish-wizard.tsx 里不得再出现 `revokeObjectURL`："
        "objectURL 由 `fetchPrecheckImage` 全局缓存共享，下游 revoke 会让"
        "其他持有者（表格缩略图）拿到已失效 URL ⇒ 图变白 + 下次重新取图。\n"
        + "\n".join(offenders)
    )


def test_price_stock_table_uses_numeric_scroll_x_and_virtual():
    """第二步价格库存表：`scroll.x` **必须是数字** + 必须 `virtual`。

    ⚠️ 用户口径 2026-10-22 两条：
      · 「UI 黑色主图需要调整优化」——第一维度列（`fixed: "left"`）整列黑成一片；
      · 「导入规格数据后，点击运费模版下拉框切换选项页面反应很慢差不多需要 10 秒」。

    两者同一个根因，读 rc-table 源码定位：
      `scrollX = scroll?.x`（Table.js:148）
      → `scrollWidth = typeof scrollX === "number" ? scrollX : null`（:162）
      → `useWidthColumns()` 在 `scrollWidth === null` 时**整段跳过**
      → `colWidths` 空 → `stickyOffsets.start` 全 0
      → 固定列全部叠在 `left:0`、拿不到实底背景（深色主题下露出黑）
      → 且每次渲染都要重新量宽，456 行 × 十余列在整页重渲染时就是那 10 秒。

    ⚠️ 第一步预检表早已按同一口径修过（`scroll={{ x: 2200, y: 420 }}` + `virtual`），
    第二步价格库存表漏了 —— 这条守卫把两处的口径钉在一起，避免再次只修一边。
    """
    src = (ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx").read_text(
        encoding="utf-8"
    )
    code = _strip_comments(src)

    #: ① 不得再出现字符串型 scroll.x（`"max-content"` / `"100%"` 都走不到数值分支）
    assert '"max-content"' not in code, (
        '第二步价格库存表的 `scroll.x` 不得再用 `"max-content"`：'
        "字符串在 rc-table 里被判定成 null，`stickyOffsets.start` 全 0 ⇒ "
        "固定列叠在 left:0 且背景失效（用户看到的「颜色分类整列黑」）。"
    )

    #: ② 必须是数值型 scroll（x 与 y 同给）
    import re

    scrolls = re.findall(r"scroll=\{\{\s*x:\s*(\d+)\s*,\s*y:\s*(\d+)\s*\}\}", code)
    assert scrolls, (
        "第二步价格库存表的 `scroll` 必须写成 `{ x: <数字>, y: <数字> }` 形态 —— "
        "`typeof scrollX === \"number\"` 是 antd 固定列算 sticky 偏移的唯一入口。"
    )

    #: ③ 必须启用 virtual（456 行非虚拟 = 上万 DOM 节点，下拉切换要重排全部）
    price_table = code.split("scroll={", 1)[1] if "scroll={" in code else ""
    window = code[max(0, code.find("scroll={") - 2000) : code.find("scroll={") + 800]
    assert "virtual" in window, (
        "第二步价格库存表必须启用 `virtual`：456 行非虚拟表格会把每行每列都渲染成"
        "真实 DOM，切一次运费模板就重排一次（用户实测 10 秒）。"
    )


def test_all_sku_rows_are_preselected_regardless_of_precheck_result():
    """**默认值按预检结果分开，但勾选框永远不禁用**。

    用户口径 2026-10-22（两轮修订后的**最终版**，原话）：
    > 「读取到的表格的数据勾选复选框由用户决定是否勾选，
    >    **sku 图未匹配到的默认不勾选**，**规则执行失败无法执行的默认不勾选**，
    >    属于预检不通过」

    演化（原句保留，不静默删除）：
      · 第一轮：「不管是否预检通过，均应自动勾选，是否勾选用户决定」
        ⇒ 当时连默认值都不过滤了；
      · 本轮说清楚：**默认值按预检结果分开**，勾选框仍不禁用。
    ⇒ 二者**不是矛盾，是两层**：
        · 默认值 = 合理起点（预检不通过的先别勾，避免误提交）；
        · 最终决定权 = 用户的（所有行都能手动勾 / 取消）。

    三条判据（缺一条就不成立）：
      ① 默认值必须**同时**读 `blockedRows`（SKU 图未匹配）与 `badRuleRows`
         （规则执行失败）—— 只判前者会让规则失败行被默认勾上；
      ② 勾选框**不得禁用**（否则用户改不了）；
      ③ 全选/反选的**基准值**也要按预检结果（否则一点全选就把不通过的全勾上），
         但**作用域**不得剔除它们（否则永远勾不上）。
    """
    src = _code_only(
        _read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx")
    )

    #: ① 默认值必须同时读两类预检不通过
    assert "const precheckFailed = (i: number) =>" in src, (
        "没有「预检不通过」的合并判据（必须同时覆盖 SKU 图未匹配 + 规则执行失败）"
    )
    assert "blockedRows.has(i) || badRuleRows.has(`${tIndex}:${i}`)" in src, (
        "预检不通过判据只覆盖了一类 —— 用户口径「sku 图未匹配到的默认不勾选，"
        "规则执行失败无法执行的默认不勾选」，两类都算"
    )
    assert "table.rows.map((_, i) => !precheckFailed(i))" in src, (
        "默认勾选没有按预检结果初始化（预检不通过的行应默认不勾）"
    )

    #: ② 勾选列不得出现 disabled
    k3 = src.index("render: (_: unknown, record: { __index: number }) => (")
    end = src.find('key: "__matched_image"', k3)
    assert end > k3, "找不到勾选列的结束边界（页面结构变了，更新本守卫）"
    chk = src[k3:end]
    assert "disabled" not in chk, (
        "勾选列里出现 `disabled` —— 用户口径是「是否勾选用户决定」，"
        "任何预检档位都不许禁用勾选框"
    )

    #: ③ 全选：作用域不剔除（能勾），但首次基准按预检结果（默认不勾）
    assert "const scopeSelectable = scopeRows.filter((i) => !blockedRows.has(i));" not in src, (
        "表头全选的作用域排除了预检不通过的行 —— 那些行会永远勾不上"
    )
    assert "table.rows.map((_, i) => !viewingFailedOnly && !precheckFailed(i))" in src, (
        "表头全选的首次基准值没按预检结果 —— 点一次全选就把不通过的行也勾上了"
    )

    #: ④ 反向：预检结论仍要表达（红框 + 统计 + 筛选），不能连带把信号也删了
    assert 'const blockedRows = new Set(' in src, "预检不通过的判定被整段删了（信号没了）"
    assert 'type FailReason = "image" | "rule" | null;' in src, (
        "失败原因分档被删了（红框与统计都要按原因分）"
    )


def test_sku_name_length_is_enforced_with_display_width_and_blocks_next():
    """规格名 60 字符校验：**按显示宽度**、**拦住下一步**、**不自动调模型**。

    用户口径 2026-10-22（三条）：
      > 「导入 sku 数据时**没有对规格名称添加 60 字符长度校验**，如果长度超出则
      >    **弹窗**展示旧规格名 / 新规格名 / 操作列，新规格名默认为空输入框」
      > 「调用大模型优化的逻辑**由用户触发**，不要再后端默认调用」
      > 「商品规格组的输入框添加 60 字符长度校验，**超出则不允许进入下一步**」

    平台真机报错原文：「规格名不能超过 **30 个字（60 个字符）**」
    ⇒ 判据必须是**显示宽度（汉字计 2）**，不是码点数。

    ⚠️ 三个真 bug 都被这条口径圈出来了，判据要同时钉住：
      ① `Array.from(raw).length`（码点）⇒ 30 个汉字只算 30 ⇒ 前端放行、发布被拒；
      ② 导入入口先 `await optimizeSkuNames(...)` ⇒ 用户没点按钮就烧 token，
         且失败时 catch 静默吞掉直接开导入（用户看到「没校验就进来了」）；
      ③ 校验只在 `commitSpecValue`（回车时）⇒ 不回车直接点「下一步」永远不校验。
    """
    step2 = _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx")
    code = _code_only(step2)

    #: ① 长度判据必须是显示宽度
    assert "Array.from(raw).length > SPEC_VALUE_LEN_MAX" not in code, (
        "规格值长度仍在用**码点数**判断 —— 30 个汉字只算 30，前端放行后发布被平台拒"
    )
    assert "exceedsLimit(raw, SKU_NAME_MAX_CHARS)" in code, (
        "规格值长度必须走 `exceedsLimit`（显示宽度，汉字计 2）"
    )

    #: ② 导入入口不得自动调模型
    entry = code[code.index("const openSpecImportWithNameCheck = useCallback("):]
    entry = entry[: entry.index("}, [")]
    assert "optimizeSkuNames(" not in entry, (
        "导入入口仍在**自动调用**大模型 —— 用户口径「由用户触发，不要默认调用」"
    )
    #: ⚠️ 2026-10-22 口径再修订（「所有 sku 数据导入完毕后，最后弹出优化规格名称的
    #: 弹窗」）：入口**不再拦截弹窗**，校验与优化统一放到导入收尾。
    assert "setSkuOptimizeOpen(true);" not in entry, (
        "导入入口又会先弹优化窗 —— 用户口径是先导完再弹"
    )
    #: ⚠️⚠️ 2026-10-22 **最终口径**：「导入数据后**不要在自动弹窗**优化规格名称，
    #: 只有用户手动触发优化规格名称按钮时才需要弹窗，**两部分逻辑拆分开**」。
    #: ⇒ 收尾处**不许**再 `setSkuOptimizeOpen(true)`，但仍要**说清楚**超长了多少
    #: （用 message.warning 点名条数并指向按钮 —— 不弹窗≠静默）。
    #: ⚠️ 起点要**在 `message.success` 之前** —— 超长统计（`const landed = ...`）
    #: 写在它上面（先算再报），从 success 起切会漏掉统计那几行（首轮误判「没做统计」）。
    finish = code[code.index("const landed = Array.from("):]
    finish = finish[: finish.index("} catch (err)")]
    assert "setSkuOptimizeOpen(true);" not in finish, (
        "导入完成后又在自动弹「优化规格名称」窗 —— 用户口径是「只有手动触发才弹」"
    )
    assert "setSkuOptimizeItems(" not in finish, (
        "导入收尾仍在往弹窗灌数据（逻辑没拆分干净）"
    )
    assert "isTooLong(n, SKU_NAME_MAX_CHARS)" in finish, "收尾处没做超长统计"
    assert "点「优化规格名称」可批量压缩" in finish, (
        "超长时没有提示（不弹窗不等于静默 —— 要指向手动按钮）"
    )
    #: 顶部按钮（用户口径「商品规格列表顶部新增一个优化规格名称按钮」）
    assert "openOptimizeForCurrentSpecs()" in code, "商品规格区顶部缺「优化规格名称」按钮"
    assert "const allSpecValues = useMemo(" in code, "缺「当前全部规格值」的派生"
    #: ⚠️ 2026-10-22 起**超长判定移到导入收尾**（先导完再弹），入口只剩「取规格值」，
    #: 不再自己做筛选 —— 那条断言随口径一起退场，判据由下面的 `finish` 部分承担。
    #: ⚠️⚠️ 校验对象必须是**规格值**，不是列名（用户口径 2026-10-22：
    #: 「导入 SKU 数据 / 导入 sku 映射数据时没有弹出规格名称优化弹窗」）。
    #: 真根因：入口读的 `effectiveImportSources` = `priceSourceOptions(columns)`
    #: = **表头列名**（「商品信息」「SKUID」…），天生短词永远不超 60
    #: ⇒ `longNames` 恒空 ⇒ 弹窗一次都没弹过。
    assert "const names = effectiveImportSources;" not in entry, (
        "导入入口仍在拿**列名**做超长判定 —— 列名永远不超 60，弹窗永远不会弹"
    )
    assert "r.specValue" in entry, (
        "导入入口没有从 `passedRows` 取**规格值**（`specValue`）做判定"
    )
    assert "new Set(" in entry, "规格值没去重（同一值重复出现在弹窗里）"

    #: ③ 超长要进发布助手（`onHintCountChange` 的门禁源头）
    hints = code[code.index("const publishHints = useMemo"):]
    hints = hints[: hints.index("}, [")]
    #: ⚠️ 只查 `exceedsLimit(...)` 字符串会被「函数体在但分支被短路」骗过
    #: （变异 M3 把 `if (longSpecValues.length > 0)` 改 `if (false)` 就逃逸了）。
    #: ⇒ 必须同时判「有筛选」+「真的 push 了一条带 section 的提示」。
    assert "exceedsLimit(v, SKU_NAME_MAX_CHARS)" in hints, (
        "超长规格值没进 `publishHints` —— 不按回车直接点「下一步」就绕过了校验"
    )
    assert "if (longSpecValues.length > 0) {" in hints, (
        "超长判定的 if 分支被短路了（`exceedsLimit` 在但从不执行）"
    )
    assert 'field: "规格值"' in hints and "个规格值超过" in hints, (
        "超长规格值没有真的 push 成一条发布提示（只算不报 = 拦不住下一步）"
    )

    #: ④ 输入框要有实时红框
    #: ⚠️ 规格值输入框有**两处**（AutoComplete 内嵌的 Input + 普通 Input）。
    #: 只判 `in code` 时删掉一处还有另一处 ⇒ 变异 M4 逃逸。
    #: ⇒ 用**出现次数**钉（两处都必须有红框判据）。
    assert code.count('status={exceedsLimit(v, SKU_NAME_MAX_CHARS) ? "error" : undefined}') >= 2, (
        "规格值输入框的红框判据少于 2 处（AutoComplete 内嵌那个 / 普通那个都要有）"
    )

    #: ⑤ 弹窗侧：默认空 + 批量优化按钮存在，且**打开时不自动调模型**
    modal = _code_only(
        _read(ROOT / "frontend/src/pages/platforms/xhs/sku-name-optimize-modal.tsx")
    )
    #: ⚠️「默认空」是在**调用方**给的初值（弹窗只渲染传进来的 items）⇒ 判据落在 step2。
    assert 'optimized: "",' in code, (
        "新规格名不是默认空输入框（调用方给弹窗的初值必须是空串，不是模型结果）"
    )
    #: ⚠️ 文案在注释 / description / 按钮标签里共出现 4 次，只判 `in modal`
    #: 时删掉按钮本体仍能过（变异 M5 逃逸）。⇒ 判**按钮 JSX 形态**：
    #: `<Button ...>统一优化规格名称</Button>` 这种「标签紧邻」的结构。
    import re as _re

    assert _re.search(r"onClick=\{\(\) => void optimizeAll\(\)\}", modal), (
        "缺「统一优化规格名称」按钮的 onClick 接线（按钮本体被删或没接处理函数）"
    )
    assert _re.search(r"\n\s*统一优化规格名称\s*\n\s*</Button>", modal), (
        "缺「统一优化规格名称」按钮本体（要有真正的 <Button>，不能只在文案里提到）"
    )
    assert "const optimizeAll = async () =>" in modal, "批量优化函数缺失"
    #: 反向：弹窗打开时的 useEffect 不得调模型
    eff = modal[modal.index("useEffect(() => {"):]
    eff = eff[: eff.index("}, [")]
    assert "optimizeSkuNames(" not in eff and "regenerateSkuName(" not in eff, (
        "弹窗打开就自动调大模型 —— 用户口径是「由用户触发」"
    )


def test_overlong_manual_spec_value_opens_the_optimize_modal():
    """**手动输入**的规格值超长时也要弹优化窗（不是干报一句错）。

    用户口径 2026-10-22：
    > 「规格名称校验成功了，但是**没有优化规格名的弹窗**」

    根因：优化弹窗只接在了「导入 SKU 数据」那条路上（`openSpecImportWithNameCheck`），
    而截图里红框标的是**手动输入/粘贴**的规格值 —— 它走 `commitSpecValue`，
    那里只 `fail(...)` 报一句就 return，从来没弹窗。

    ⚠️ 两个入口必须**各回各处**（这是最容易漏的一半）：
      · 导入入口 → 确认后把优化结果写回 `specImportSources` 再开导入弹窗；
      · 手动输入 → 确认后把优化结果**写回那个输入框**（靠 `pendingSpecValue` 记住落点）。
    只做「打开弹窗」不做「写回落点」，用户优化完发现值没变。
    """
    code = _code_only(
        _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx")
    )

    #: ① 超长分支必须弹窗（不只是 fail）
    seg = code[code.index("if (exceedsLimit(raw, SKU_NAME_MAX_CHARS)) {"):]
    seg = seg[: seg.index("return;") + 400]
    assert "setSkuOptimizeOpen(true);" in seg, (
        "手动输入的规格值超长时没有打开优化弹窗（只报错就 return 了）"
    )
    assert "setPendingSpecValue({ spec, index })" in seg, (
        "没记住「优化完写回哪个规格的第几格」—— 优化结果会无处可写"
    )
    assert "original: raw," in seg and 'optimized: "",' in seg, (
        "弹窗项没带上原始值 / 新规格名不是默认空"
    )

    #: ② 确认后要按入口分流（两条路各回各处）
    confirm = code[code.index("onConfirm={(applied) => {"):]
    confirm = confirm[: confirm.index("}}")]
    assert "if (pendingSpecValue) {" in confirm, (
        "onConfirm 没区分「手动输入」与「导入」两个入口 —— 手动改完会被弹进导入弹窗"
    )
    assert "setSpecValueAt(spec, index, picked);" in confirm, (
        "手动输入入口确认后没有把优化结果写回输入框"
    )
    assert "setAppliedSkuNames(next);" in confirm, (
        "导入入口确认后没有更新数据源（原有行为被破坏）"
    )
    #: ⚠️ 入口 C：导入完成后统一优化 ⇒ **覆盖商品规格里的旧值**
    #: （用户口径「统一优化点击应用重新覆盖到商品 sku 旧规格名称值」）。
    #: 三类数据都要跟着改名，否则留下指向旧名的孤儿（值改了图/开关就挂不上）。
    assert "if (!specImportOpen) {" in confirm, "onConfirm 没区分「导入后统一优化」这个入口"
    #: ⚠️ 2026-10-22 改：入口 C 不再**手写三份 setState**，统一委托
    #: `renameSpecValue(dim, v, nextName)` —— 那个实现会一起重排
    #: `specValues` / `specImages` / `specImageEnabled` / `priceRows.key`。
    #: 手写版漏了 `priceRows`（正是用户报的「其它值也丢失了」），故此处改为
    #: **反向断言**：不许再出现手写改名。
    assert "renameMap.set(rk(dim, v)" in confirm, "入口 C 没构建批量改名表"
    assert "next[dim] = list.map((v) => renameMap.get(rk(dim, v)) ?? v);" in confirm, (
        "入口 C 没按批量表改 `specValues`"
    )


def test_sku_name_optimize_modal_threshold_blank_and_compact():
    """优化弹窗三条口径（用户口径 2026-10-22，同一轮报的三件事）。

    ① 「**小于 60 字符就不应该展示**在 SKU 规格名称优化预览弹窗列表中」
       ⇒ 阈值是**严格大于**（60 本身合法；平台原文「不能超过 30 个字（60 个字符）」）。
       前口径是「达到即触发」（`>=`）⇒ 列表里混进一堆 60 的合规项（用户截图为证）。
    ② 「调用失败新规格名**应该还是空**不应该使用原规格名」
       ⇒ 后端超长项初值 `optimized=""`；前端 `isNameOk("")` 必须判**不合规**，
          否则空值宽度 0 会被当成合格 ⇒ 既不红框、也不计入「待处理」。
    ③ 「提示音**不要占用页面太多空间**」
       ⇒ 两条 `<Alert>` + 一行按钮合并成一条紧凑操作行。
    """
    from backend.app.services import sku_name_optimize_service as svc

    #: ① 阈值：严格大于（前后端必须同一判据）
    assert svc.is_too_long("中" * 30, 60) is False, (
        "后端 60 字符仍判超长 —— 用户口径是「小于 60 不展示」，恰好 60 合法"
    )
    assert svc.is_too_long("中" * 31, 60) is True, "后端 61 字符没判超长"
    width_lib = _read(ROOT / "frontend/src/lib/sku-name-width.ts")
    #: ⚠️ 必须**按函数体**判，不能全文搜 `> maxChars;` ——
    #: `exceedsLimit` 里有一模一样的一行，把 `isTooLong` 改回 `>=` 时
    #: 全文断言照样绿（变异 Q2 实测逃逸）。
    fn_i = width_lib.index("export function isTooLong(")
    fn_body = width_lib[fn_i : width_lib.index("\n}", fn_i)]
    assert "return displayWidth(name) > maxChars;" in fn_body, (
        "前端 `isTooLong` 不是严格大于（会与后端口径不一致）"
    )
    assert ">=" not in fn_body, "前端 `isTooLong` 里仍有 `>=`（达到即触发的旧口径）"

    #: ② 失败留空 + 空值判不合规
    svc_src = _read(ROOT / "backend/app/services/sku_name_optimize_service.py")
    assert 'optimized="" if is_too_long(n, max_chars) else n,' in svc_src, (
        "后端超长项初值没有留空 —— 失败时会拿原文冒充结果"
    )
    modal = _code_only(
        _read(ROOT / "frontend/src/pages/platforms/xhs/sku-name-optimize-modal.tsx")
    )
    name_ok = modal[modal.index("function isNameOk("):]
    name_ok = name_ok[: name_ok.index("\n}")]
    assert 'if (!text) return false;' in name_ok, (
        "`isNameOk` 没把空值判为不合规 —— 失败行不红框、也不计入待处理"
    )
    #: 红框判据要同时看 reason（重试失败也要红）
    assert "const bad = !!row.reason || !isNameOk(value, maxChars);" in modal, (
        "新规格名红框判据只看长度 —— 调用失败的行不标红"
    )

    #: ③ 紧凑提示：不再用 <Alert>，失败提示走 <Text type="warning">
    assert "<Alert" not in modal, (
        "弹窗里仍有 `<Alert>` 大块提示 —— 用户口径「提示不要占太多空间」"
    )
    assert '<Text type="warning"' in modal, "失败提示没有改成紧凑 Text"
    assert "<Space wrap size={8}" in modal, "操作行没合并成一行"


def test_renaming_spec_value_only_renames_never_resets_rows():
    """规格改名**只换名字**，价格库存行的其它字段一律不动（用户口径 2026-10-22）。

    用户连报三次，其实是**同一个根因**：
      ① 「编辑商品规格输入框后，**规格图丢失**，对应的价格及库存中的**所有列数据
         全部丢失**」；
      ② 「优化预览弹窗点击使用这些名称导入时应**只覆盖原来的规格名称**，目前覆盖后
         价格及库存列表中的**其它值也丢失了**」；
      ③ 「修改时只是名修改规格图，**库存表数据不应该变化清空**」
      + 「**只有删除该规格时**，对应的库存表的整行数据应删除」。

    根因：`PriceRow.key` 就是完整 `spec`（`lib/publish-config.ts::spec_combinations`
    末尾 `{ key: spec, spec }`，含「维度名:值」）。只改 `specValues` 里的字符串时，
    同步 effect 按**新 key** 重建行、`byKey` 里只有**旧 key** ⇒ 匹配不上 ⇒ 走 `base`
    分支把 price/stock/market_price/current_stock/barcode/seller_code 全置 `null`；
    规格图侧 `specImages`/`specImageEnabled` 按值做 key，旧键成了孤儿 ⇒ 图挂不上。
    未改名的行 key 不变所以完好 —— 与截图完全吻合。

    ⇒ 判据（三条，缺一即回归）：
      A. 改名走**单一实现** `renameSpecValue`，且它四类键一起重排；
      B. 价格行**只换 `key` 与 `spec`**，其余字段靠 `...r` 原样带过去（不许 delete/重置）；
      C. 删行只发生在「删除该规格」的路径上 —— 改名路径**不得**减少行数。
    """
    src = _code_only(_read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx"))

    #: A. 单一实现存在，且四类键都重排
    assert "const renameSpecValue = useCallback(" in src, "改名没有统一实现 `renameSpecValue`"
    body_i = src.index("const renameSpecValue = useCallback(")
    #: 右边界用下一个函数声明；src 过了 _code_only（注释已剥），不能拿注释当锚点。
    body = src[body_i : src.index("const setSpecValueAt = useCallback(", body_i)]
    #: ⚠️ 不能只查「setter 名出现过」—— 变异「`return prev;` 让 setSpecImages 空转」时
    #:   `setSpecImages(` 那串仍在（调用还在，只是不做事）⇒ 首轮 S3 ESCAPED。
    #:   判据要落到**真正改键的那几行**上。
    assert "return { ...prev, [dim]: list.map((v) => (v === from ? to : v)) };" in body, (
        "`renameSpecValue` 没重排 `specValues`"
    )
    assert "return { ...prev, [dim]: renamed };" in body, (
        "`renameSpecValue` 没重排 `specImages`（改名后规格图丢失）"
    )
    assert "next[`${dim}\\u0000${to}`] = next[oldKey];" in body, (
        "`renameSpecValue` 没重排 `specImageEnabled` 的扁平键"
    )
    assert "props.onPriceRowsChange?.(" in body, "`renameSpecValue` 没重排价格库存行"

    #: B. 价格行只换两段名字，其余字段原样带过去
    assert "return { ...r, key: spec, spec };" in body, (
        "价格行不是「只换 key 与 spec」—— 其它字段会被重置（用户报的「其它值全丢」）"
    )
    #: 任何在改名实现里删字段/重建行的写法都是回归
    for forbidden in ("price: null", "stock: null", "market_price: null", "delete r."):
        assert forbidden not in body, f"`renameSpecValue` 里出现 `{forbidden}` —— 会把已填数据清掉"

    #: C. 改名路径不许过滤/截断行（行数只能由「删除规格」改变）
    assert ".filter(" not in body, "`renameSpecValue` 里在过滤行 —— 改名不该增删行"

    #: 入口 C（优化弹窗确认）必须走同一实现，不许自己再写一份
    #: ⚠️ `src` 过了 `_code_only`（注释已剥）⇒ 用**代码**锚点，不能用注释文案。
    #: ⚠️⚠️ 入口 C **不能循环里逐条调 `renameSpecValue`**（用户报 2026-10-22 第二次：
    #: 循环里每次都用同一份 `props.priceRows` 闭包快照 + 异步 `onPriceRowsChange`
    #: ⇒ 后一次覆盖前一次 ⇒ 只有最后一条改名落到价格行 ⇒ 其余行被重建清空）。
    #: 正确形态 = **一次算完 `renameMap` → 一次 `onPriceRowsChange`**。
    #: ⚠️ 起点前移到 `const norm` —— 快照（`snapshotByRow`）建在 `renameMap` 之前。
    confirm_i = src.index("const norm = (s: string) =>")
    confirm = src[confirm_i : src.index("void message.success(`已把", confirm_i)]
    assert "renameMap.set(rk(dim, v)" in confirm, "入口 C 没构建批量改名表"
    assert confirm.count("props.onPriceRowsChange?.(") == 1, (
        "入口 C 里 `onPriceRowsChange` 出现了多次 —— 会被闭包旧快照互相覆盖"
    )
    #: ⚠️ 2026-10-22 定稿：改为**按行号快照复原** ⇒ 字段来源是 `snapshot` 而不是 `r`。
    assert "const snapshot = snapshotByRow[i];" in confirm and (
        "return { ...snapshot, key: spec, spec };" in confirm
    ), "入口 C 的价格行不是「只换 key 与 spec、其余字段按快照复原」"
    #: ⚠️ 禁止「循环里逐条调改名」—— 但入口 C 本身要遍历维度构建表，
    #:   所以判据落在**循环体里有没有调 `renameSpecValue`**，而不是循环本身。
    assert "if (nextName && nextName !== v) renameSpecValue(" not in src, (
        "入口 C 又变成循环逐条改名（多次异步回传会互相覆盖，其它值会被清空）"
    )


def test_rename_paths_share_one_implementation_and_never_touch_inputs():
    """改名两条路（手改输入框 / 优化弹窗覆盖）**必须同一套键同步**，且都不重构输入框。

    用户口径 2026-10-22：
      > 「手动修改规格时**数据列没有丢失**」
      > 「分析是否由于你覆盖时**清空了规格输入框**导致数据丢失」
      > 「优化规格名称后，红框中的 sku 导入的数据丢失了，应该**只改规格名称**，
      >   其它规格图以及对应的库存，价格等等列均不变」

    实测差异（这是本条守卫的由来）：
      · 手改：一次只改**一个值**，`priceRows` 的 key 与新值天然同一步到位 ⇒ 从不丢；
      · 覆盖：一条事件里改**多个值**。若循环里逐条调 `renameSpecValue`，
        每次都用**同一份 `props.priceRows` 闭包快照** + 异步 `onPriceRowsChange`
        ⇒ 后一次覆盖前一次 ⇒ 只有最后一条改名落到价格行 ⇒ 其余行 key 停在旧值
        ⇒ 重建 effect 按新 key 找不到 ⇒ 价格/库存/编码被清空（用户截图那几行）。
    """
    src = _code_only(
        _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx")
    )

    #: ① 覆盖路径必须「先算完整张改名表 → 一次回传」，不许循环里逐条回传
    #: ⚠️ 起点必须在 `const norm` —— 快照（`snapshotByRow`）建在 `renameMap` 之前，
    #:   从 `renameMap` 起切会看不到快照那两行（首轮误判「没记录快照」）。
    i = src.index("const norm = (s: string) =>")
    seg = src[i : src.index("void message.success(`已把", i)]
    assert seg.count("props.onPriceRowsChange?.(") == 1, (
        "覆盖路径里 `onPriceRowsChange` 不止一次 —— 闭包旧快照互相覆盖会清空其它行"
    )
    assert "if (nextName && nextName !== v) renameSpecValue(" not in seg, (
        "覆盖路径又变成循环逐条改名（多次异步回传互相覆盖）"
    )

    #: ② 归一化：`applied` 的键与表里的值必须都 trim 后再比对，
    #:    否则「有一项没改到」⇒ 该项三处键不同步 ⇒ 那几行被清空。
    #: ⚠️ `rk` 定义在 `renameMap` **之前** ⇒ 判据落在整个入口 C 片段上（`seg` 从
    #: `renameMap` 起切会看不到定义）。用更宽的 `block` 覆盖「定义 + 使用」。
    block = src[src.index("const norm = (s: string)"):][:3000]
    #: ⚠️ 不能只查「`rk` 这个函数存在」—— 变异「rk 内部去掉 norm」时函数名还在
    #:   （首轮 T2 ESCAPED）。判据要落到**函数体真的用了 norm**。
    assert "${norm(dim)}\u0000${norm(value)}" in block, (
        "改名表的 key 没做两头归一化 —— 带首尾空格的值会漏改"
    )
    assert "renameMap.get(rk(" in seg, "查改名表没用归一化 key"

    #: ③ 覆盖路径**不许**碰规格类型 / 输入框 / 挂起值（那会让「只改名字」变成重建）
    for forbidden in ("setSpecTypes(", "clearImportTargets(", "setPendingSpecFills("):
        assert forbidden not in seg, (
            f"覆盖路径调了 `{forbidden}` —— 用户口径是「只改规格名称」，不是重建整块"
        )

    #: ④ **快照式复原**（用户口径 2026-10-22 最终做法）：
    #: 「先批量覆盖前先记录历史数据，覆盖后**只规格名称变化，其它数据不变复原**」。
    #: ⇒ 覆盖路径必须**先存快照**，且复原时字段取自快照（不是取自可能已被清空的 `r`）。
    #: ⚠️ **按行号复原**（用户口径 2026-10-22：「可以**按照行号复原**」）。
    #: 行号是唯一不依赖名字的索引 —— `priceRows` 的行序是规格值笛卡尔积的固定顺序，
    #: 改名只换标签不改顺序，所以第 i 行改名前后是同一个 SKU。
    assert "const snapshotByRow = props.priceRows.map((r) => ({ ...r }));" in seg, (
        "覆盖前没有按行号记录快照 —— 改名后其它列只能靠猜 key 找回来"
    )
    assert "props.priceRows.map((r, i) => {" in seg, (
        "复原时没有拿到行号 `i`"
    )
    assert "const snapshot = snapshotByRow[i];" in seg, (
        "复原时没有按行号取快照行"
    )
    assert "return { ...snapshot, key: spec, spec };" in seg, (
        "复原时字段来源不是快照 —— 只换名称的承诺没兑现（其它列可能为空）"
    )


def test_rename_restore_by_row_index_handles_multi_spec():
    """改名复原**按行号**，并挡住「多规格改名后重名 ⇒ 行错位」（用户口径 2026-10-22）。

    用户口径演进（三版猜 key 全失败后定稿）：
      · 「先批量覆盖前先**记录历史数据**，覆盖后**只规格名称变化，其它数据不变复原**」
      · 「可以**按照行号复原**」
      · 「此处需注意考虑**多规格**时如何覆盖处理」

    **为什么行号成立**：`priceRows` 由 `spec_combinations` 对每个维度的值做笛卡尔积
    生成，行序是确定的；改名只换标签不改顺序 ⇒ 第 i 行改名前后是同一个 SKU。

    **多规格的真实风险**：改名后同维度出现**重名**（「白色A」与「白色」都改成
    「白色」）⇒ `specValues` 里两个相同值 ⇒ 组合数变少 ⇒ 行号整体错位 ⇒
    按行号复原会把别行的价格/库存搬到错行（比清空更糟：有值但张冠李戴）。
    ⇒ 覆盖前必须拦住这种改名，不许带着错位往下走。
    """
    src = _code_only(
        _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx")
    )
    i = src.index("const norm = (s: string) =>")
    seg = src[i : src.index("void message.success(`已把", i)]

    #: ① 快照按行号 + 复原按行号（不许退回「按 key / 按值序列」猜）
    assert "const snapshotByRow = props.priceRows.map((r) => ({ ...r }));" in seg
    assert "const snapshot = snapshotByRow[i];" in seg
    assert "return { ...snapshot, key: spec, spec };" in seg, (
        "复原字段来源不是行号快照 —— 其它列可能为空或被搬到错行"
    )

    #: ② 多规格重名必须在**写任何状态之前**拦住（`return` 在 setSpecValues 之前）
    clash_at = seg.index("const clashes: string[] = [];")
    first_set = seg.index("setSpecValues((prev) => {")
    assert clash_at < first_set, (
        "重名检查放在改名之后 —— 已经写脏状态再 return，页面会半更新"
    )
    assert "if (clashes.length > 0) {" in seg and "return;" in seg, "重名时没有中断覆盖"
    assert "会导致 SKU 行错位" in seg, "重名提示没说清后果（用户不知道为什么要拦）"

    #: ③ 重名检查必须**按维度分组**（跨维度同名是合法的：颜色「白」+ 款式「白」）
    assert "for (const [dim, list] of Object.entries(specValues)) {" in seg
    assert "const seen = new Set<string>();" in seg


def test_spec_value_duplicates_are_rejected_at_every_entry():
    """**规格名不能重复**（用户口径 2026-10-22：「规格名不能重复添加校验」）。

    为什么要三个入口都拦：规格值有**三条写入路径**，只堵一条就漏
      A. 输入框 `onChange`（逐字符）→ `setSpecValueAt`；
      B. 回车提交 → `commitSpecValue`；
      C. 优化弹窗「使用这些名称导入」批量覆盖 → 入口 C。

    漏 A 的后果最隐蔽：用户还没回车，重复值已经进了 `specValues`
    ⇒ 同维度两个同名值 ⇒ 笛卡尔积**少一行** ⇒ 价格库存表**行错位**
    （比多一行更糟：数据张冠李戴）。
    漏 C 的后果：优化把两个不同名压成同一个短名 ⇒ 同样错位，
    而且发生在**批量**操作上，一次影响整张表。
    """
    src = _code_only(
        _read(ROOT / "frontend/src/pages/platforms/xhs/publish-config-step2.tsx")
    )

    #: A. 逐字符写入路径必须在**写状态之前**拦
    a_i = src.index("const setSpecValueAt = useCallback(")
    #: ⚠️ `src` 过了 `_code_only`（注释已剥）⇒ 用**代码**锚点。
    a_seg = src[a_i : src.index("const commitSpecValue = useCallback(", a_i)]
    assert "规格值名称不可重复" in a_seg, (
        "输入框逐字符写入没有重复校验（用户没回车就已经写进 specValues 了）"
    )
    #: ⚠️ 判据要落在**条件本身**上：只查「提示文案出现过」的话，把 `if (false)` 塞进去
    #: 文案仍在 ⇒ 首轮 V1 ESCAPED。必须钉住「同维度其它值 == 输入值」这个比法。
    cond = "list.some((v, i) => i !== index && v.trim() === typed)"
    assert cond in a_seg, "输入框没按「同维度其它值」判重（或判重条件被架空）"
    dup_at = a_seg.index(cond)
    rename_at = a_seg.index("renameSpecValue(spec, before, next);")
    assert dup_at < rename_at, "重复校验放在改名之后 —— 已经写脏状态才报错"

    #: B. 回车提交路径
    b_i = src.index("const commitSpecValue = useCallback(")
    b_seg = src[b_i : b_i + 3000]
    assert "规格值名称不可重复" in b_seg, "回车提交没有重复校验"

    #: C. 优化覆盖路径（批量）—— 重名必须整批拒绝，不许带着错位往下走
    c_i = src.index("const clashes: string[] = [];")
    c_seg = src[c_i : c_i + 1200]
    assert "if (clashes.length > 0) {" in c_seg and "return;" in c_seg, (
        "优化覆盖没有拦截重名 —— 会把两个值压成同名，价格库存行整体错位"
    )


def test_zone_map_lists_only_checked_zones_with_checked_counts():
    """区域映射下拉：**只列有勾选资源的区域**，**数字 = 已勾选数**（用户口径 2026-10-23）。

    原文：「如果一个区域下**没有被勾选的媒体资源**，那么在指定媒体资源区域下拉框中
    就**不应该展示**，且下拉框选择的统计的数值**应是被勾选选中的数值**」。

    旧实现两个都不对：
      · `zoneCount` = `mediaOfZone(precheck.media, name).length`（**该区域全部媒体**）
        ⇒ 下拉显示「SKU图（52）」而实际只勾了 50 张，用户按这个数会被误导；
      · `zones={mediaZones}`（**数据包里所有区域**）⇒ 能选到一个 0 张勾选的空区域，
        选完 `mediaByZones` 返回空数组 ⇒ 该发布位"已选"但实际没有内容。
    """
    src = _code_only(
        _read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx")
    )

    #: ① 计数 = 已勾选（基于 `selectedMedia`），不是区域全部媒体
    assert "const zoneCount = useCallback(" in src, "缺 zoneCount"
    i = src.index("const zoneCount = useCallback(")
    seg = src[i : i + 400]
    assert "selectedMedia.filter((m) => m.group === name).length" in seg, (
        "zoneCount 没按「已勾选」统计（应基于 selectedMedia）"
    )
    assert "mediaOfZone(precheck?.media ?? [], name).length" not in seg, (
        "zoneCount 仍在统计区域全部媒体 —— 下拉数字会与实际勾选数不符"
    )

    #: ② 候选区域 = 只含有已勾选媒体的区域
    assert "const selectableZones = useMemo(" in src, "缺 selectableZones"
    j = src.index("const selectableZones = useMemo(")
    seg2 = src[j : j + 400]
    assert "const withChecked = new Set(selectedMedia.map((m) => m.group));" in seg2, (
        "selectableZones 没按「有已勾选媒体」过滤"
    )
    assert "return mediaZones.filter((z) => withChecked.has(z.name));" in seg2

    #: ③ 弹窗必须收到过滤后的集合（不许再传全量 mediaZones）
    assert "zones={selectableZones}" in src, "弹窗没传过滤后的区域集合"
    assert "zones={mediaZones}" not in src, (
        "弹窗仍在传全部区域 —— 空区域会出现在下拉里"
    )
    assert "counts={Object.fromEntries(selectableZones.map((z) => [z.name, zoneCount(z.name)]))}" in src, (
        "counts 不是按过滤后的区域算的"
    )


def test_zone_map_requires_video_zone_when_video_is_checked():
    """勾了视频却设空视频区域 ⇒ 映射弹窗**必须拦住**（用户报障 2026-10-23）。

    原文：「勾选之后，使用自动上传但是还是提示数据包中没有视频」。
    真因：视频位 `PUBLISH_SLOT_REQUIRED.video = false`（用户口径 2026-10-13「视频可留空」）
    ⇒ 用户能带着「勾了视频 + 视频区域留空」进第二步 ⇒ `zoneMap.video = []`
    ⇒ `videoPaths = []` ⇒ 自动加载跳过视频，且旧文案写成「数据包内无商品视频」误导。

    修法：**按上下文动态必填** —— 数据包里有已勾选视频时，视频位转必填。
    ⚠️ 不能直接把常量改成 true：数据包**没有**视频时不该制造进不去的关卡。
    """
    lib = _read(ROOT / "frontend/src/lib/precheck-zones.ts")

    #: ① 常量保持 false（2026-10-13 口径仍成立）
    assert "video: false," in lib, "PUBLISH_SLOT_REQUIRED.video 被改成 true 了 —— 无视频的数据包会进不去"

    #: ② 校验函数接收 hasVideo 上下文
    assert "ctx?: { hasVideo?: boolean }" in lib, "validateZoneMap 没有 hasVideo 上下文"
    assert 'slot === "video" && ctx?.hasVideo ? true : PUBLISH_SLOT_REQUIRED[slot]' in lib, (
        "视频位没有按 hasVideo 动态必填"
    )
    assert "数据包内有已勾选的视频，请选择「商品视频」区域" in lib, "缺针对性的报错文案"

    #: ③ 页面把「有已勾选视频」传进去
    src = _code_only(
        _read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx")
    )
    i = src.index("const confirmZoneMap = useCallback(")
    seg = src[i : i + 700]
    assert "const hasCheckedVideo = selectedMedia.some(" in seg, "没统计已勾选视频"
    assert "validateZoneMap(zoneMap, { hasVideo: hasCheckedVideo })" in seg, (
        "没把 hasVideo 传给 validateZoneMap"
    )
    assert "selectedMedia" in seg.split("}, [")[1] if "}, [" in seg else True


def test_skipped_video_reason_distinguishes_missing_zone_from_missing_video():
    """自动加载「跳过视频」的文案必须**区分原因**（用户报障 2026-10-23）。

    旧实现三种原因都写「数据包内无商品视频」⇒ 用户被引去翻数据包目录，
    而真正要改的是**区域映射弹窗里「商品视频」那栏**。
    """
    src = _code_only(
        _read(ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx")
    )
    #: 旧的无条件文案必须消失（它只在真正「没有已勾选视频」时才该出现，且措辞也变了）
    assert 'setStage(4, { status: "skipped", detail: "数据包内无商品视频" });' not in src, (
        "仍是无条件写「数据包内无商品视频」—— 三种原因混为一谈"
    )
    #: 新的三分支判据存在
    assert "const noVideoZone = (zoneMap.video?.length ?? 0) === 0;" in src, "没判「视频区域为空」"
    assert "未指定「商品视频」区域（数据包内已勾选" in src, "缺「没指定区域」的文案"
    assert '"数据包内无已勾选的商品视频"' in src, "缺「真没有视频」的文案"


# --------------------------------------------------------------------------- #
# 撑满布局钉底守卫（设计 63 §6.1 / AGENTS 硬约束 7 —— 通病「撑满丢分页」防复发）
# --------------------------------------------------------------------------- #
def test_fill_table_wrappers_pin_pagination():
    """每一处 `.fill-table`（含旧类名）包装的表格，其 scroll 必须带 `y: "100%"`。

    背景：本项目三次把分页顶出屏（商品台账 / 审计日志 Tab / 应用日志文本区），
    用户口径「这个问题每次都发生……记录下，后续不要再出现」（AGENTS §3 硬约束 7）。
    `.fill-table` 三区布局的表体滚动全靠 `scroll.y="100%"` 从 CSS 高度链拿高度 ——
    少了这个键，表格按内容撑开，分页立刻出屏。
    """
    import re

    src = ROOT / "frontend/src"
    offenders: list[str] = []
    checked = 0
    for path in src.rglob("*.tsx"):
        text = path.read_text(encoding="utf-8")
        # 只数**外层 div 包装**（`rootClassName` 与它包的是同一张表，不算第二处
        # —— shop-items / datacenter 是「div + rootClassName」双挂载同表写法）。
        wrappers = len(re.findall(r'className="(fill-table|shop-items-table|violation-table)"', text))
        if wrappers == 0:
            continue
        checked += 1
        pinned = len(re.findall(r'y:\s*"100%"', text))
        assert pinned >= wrappers, (
            f"{path.relative_to(src)} 有 {wrappers} 处 fill-table 包装，但只有 {pinned} 处 "
            f'`y: "100%"` —— 有表格没走表体内滚动，分页会被顶出屏（AGENTS 硬约束 7）'
        )
        offenders.append(path.name)
    assert checked >= 8, f"守卫只扫到 {checked} 个文件 —— fill-table 接线面缩水了？（应为空转保护）"


def test_app_log_text_area_scrolls_instead_of_overlaying_pagination():
    """应用日志的日志文本区：`flex:1` 的同时必须亲自给 `overflow:auto`。

    2026-10-25 用户截图实测：只给 flex 不给滚动 ⇒ 长文本透明地盖在「较早/较新」
    翻页按钮上（用户原话「透明部分影响操作按钮了」）。flex 子项不自带滚动。
    """
    text = (ROOT / "frontend/src/pages/system/system-logs-panel.tsx").read_text(encoding="utf-8")
    m = re.search(r"flex: 1,\s*\n\s*minHeight: 0,(.*?)whiteSpace", text, re.S)
    assert m, "日志文本区的样式块找不到（结构变了就换锚，别删守卫）"
    assert "overflow" in m.group(1), (
        "日志文本区缺 `overflow: auto` —— 长文本会透明盖住翻页按钮（AGENTS 硬约束 7）"
    )
