"""侧栏业务线分组的源码守卫（设计 32 §5，2026-10-11）。

## 为什么需要这一批

「按业务线分组」是个**纯导航层**改动 —— 它不新增端点、不新增表、不改路由，
所以**没有任何后端行为测试能发现它被改回去**。分组被删、被搬错组、被换成
SubMenu（让每个入口多一次点击），全套后端测试照样全绿。

项目没有前端组件测试框架，所以这里用**源码断言**钉住（与
`test_page_optimizations.py` / `test_menu_integration.py` 同一套路）。

## 这批守卫盯的四件事

| 用例 | 规则 |
|---|---|
| :func:`TestSidebarBusinessLineGroups.test_three_business_line_groups_exist` | 三个业务线分组标题在，且**文案逐字**（含用户 D1 拍板的「平台」而非「公共」） |
| :func:`TestSidebarBusinessLineGroups.test_every_page_is_filed_under_its_business_line` | 12 个业务线页面各自在**正确的组**里 |
| :func:`TestSidebarBusinessLineGroups.test_no_menu_entry_or_route_was_lost` | 17 个入口一个不少 + 路由条目数不变 |
| :func:`TestSidebarBusinessLineGroups.test_grouping_is_render_only_not_a_submenu` | 分组是 `type: "group"`（渲染层），不是 SubMenu（多一层点击） |
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHELL = ROOT / "frontend/src/components/layout/app-shell.tsx"
ROUTER = ROOT / "frontend/src/app/router.tsx"

#: 侧栏**叶子**入口总数（SubMenu 父级不计）。**允许增长，不允许减少**。
#: 17（A3 分组前的平铺）
#: → 18（A6 拆「数据洞察」为「内容洞察 / 商品洞察」：−1 +2）
#: → 24（本轮二级菜单：笔记工坊 6 个页签从「页内 Tabs」提升为**菜单叶子**，+6）。
#:   ⚠️ 这 6 项**不是新页面**，是同一宿主页的页签（`note-workshop?tab=…`）——
#:   路由表一条未改，见 `test_no_menu_entry_or_route_was_lost`。
#: → 25（设计 35：新增「数据中心」页面，从「我的笔记」拆出离线商业/普通 Tab）。
EXPECTED_MENU_ENTRIES = 25

#: 路由条目数。**只在「新增页面」时增长**；把页签提升为菜单叶子**不**增路由。
#: 32（A3 前）→ 34（A6 两个新页面；旧 `/analytics` 保留为重定向，条目不减）。
#: → 35（设计 35：新增 `/platforms/xhs/datacenter`）。
EXPECTED_ROUTE_PATHS = 35

#: 二级菜单（SubMenu）父级 → 该父级下必须包含的**叶子** key（设计 32 §5）。
EXPECTED_GROUPS: dict[str, tuple[str, ...]] = {
    "内容线": (
        "/platforms/xhs/discovery",
        "/platforms/xhs/my-notes",
        "/platforms/xhs/library",
        "/platforms/xhs/keywords",
        "/platforms/xhs/insights/content",
        # ⚠️ 宿主项**自身**的 key 也要断言：只查 6 个 `?tab=` 叶子时，
        #    把宿主的 key 改掉（子项不动）照样绿 —— 实测 ESCAPED。
        #    带引号的整串 `"/…/note-workshop"` 不是 `"…/note-workshop?tab=…"` 的子串，
        #    所以这条确实在独立断言宿主。
        "/platforms/xhs/note-workshop",
        # 笔记工坊的 6 个页签（三级：宿主 → 页签）
        "/platforms/xhs/note-workshop?tab=images",
        "/platforms/xhs/note-workshop?tab=videos",
        "/platforms/xhs/note-workshop?tab=drafts",
        "/platforms/xhs/note-workshop?tab=publish",
        "/platforms/xhs/note-workshop?tab=materials",
        "/platforms/xhs/note-workshop?tab=prompts",
    ),
    "店铺线": (
        "/platforms/xhs/shop-items",
        "/platforms/xhs/picture-space",
        "/platforms/xhs/orders",
        "/platforms/xhs/insights/shop",
        "/platforms/xhs/datacenter",
    ),
    "平台": (
        "/platforms/xhs/accounts",
        "/platforms/xhs/monitoring",
        "/platforms/xhs/auto-ops",
    ),
}


def _code_only(path: Path) -> str:
    """剥注释后的源码。

    ⚠️ 本文件的注释里**大量提到**被守护的组名与路由（讲变更史），不剥就会被
    自己的注释命中 —— 这是本项目反复踩过的形态（`test_task_records.py` 实测）。
    """
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?m)(?<![\"'\w])/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    return text


def _const_body(code: str, name: str) -> str:
    """切出具名常量数组的**体**（`const <name>: MenuProps["items"] = [ ... ];`）。

    ⚠️ 用两个锚点之间切片（声明行 → 该数组的收尾 `];`），不用「起点 + 固定宽度」——
    固定宽度会在注释增长后滑出目标区（本项目实测踩过）。
    """
    start = code.index(f"const {name}:")
    end = code.index("];", start)
    return code[start:end]


def _group_target(code: str, label: str) -> str:
    """从 SubMenu 定义里取出它引用的那个常量名（`children: XxxChildren`）。

    SubMenu 块 = 从 `label: "<组名>"` 到该对象结束（`},`）。

    ⚠️ **不能再用「到下一个 `type: "group"`」当右界**（2026-10-11 二级菜单改造后
    组节点已不存在）—— 右界要跟着形态走，否则切片会吞掉后面所有菜单项，
    「归位」断言就变成「整个文件里有没有这个字符串」（守卫失效）。
    """
    marker = f'label: "{label}"'
    start = code.index(marker)
    end = code.index("},", start)
    body = code[start:end]
    match = re.search(r"children:\s*(\w+)", body)
    assert match, f"菜单「{label}」没有 children（二级菜单必须装子项）"
    return match.group(1)


def _parent_keys(code: str) -> list[str]:
    """取所有**父级**（非路由）菜单项的 key —— 即 SubMenu 的标识。

    判据：`key: "xxx", icon` 里 key **不是**以 `/` 开头（路由）的那几个。
    """
    return re.findall(r'key:\s*"([a-z][a-z-]*)",\s*\n?\s*icon', code)


def _item_by_key(code: str, key: str) -> str:
    """按菜单项的 `key: "<key>"` 切出**该对象的完整字面量**（括号配对）。

    ⚠️ 两个都踩过的坑，写在这里免得重来：
    ① **不能用「下一个 `},` / `],`」当右界** —— 那些片段文件里到处都有，
       切片会被拉到文件末尾，断言退化成「整个文件里有没有这个字符串」（实测 ESCAPED）。
    ② **不能从 marker 往后找第一个 `{`** —— marker 在对象内部，往后第一个 `{`
       往往是它 `children` 数组里的**第一个子项**（实测：拿到的是「图片工坊」那一项）。

    正确做法：先用正则定位 `{` + 换行 + `key: "<key>"` 这个**项的开头**，再数括号配平。
    （已跳过字符串字面量，避免 `"}"` 把计数带偏。）
    """
    pattern = r"\{\s*\n?\s*key:\s*\"" + re.escape(key) + r"\","
    match = re.search(pattern, code)
    assert match, f"找不到 key 为 {key!r} 的菜单项"
    start = match.start()
    depth = 0
    i = start
    in_str: str | None = None
    while i < len(code):
        ch = code[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in "\"'`":
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return code[start : i + 1]
        i += 1
    raise AssertionError(f"key={key!r} 的菜单项括号不配平")


class TestSidebarBusinessLineGroups:
    def test_three_business_line_submenus_exist(self):
        """三个业务线**二级菜单**在，且文案**逐字**；「总览」保持一级。

        用户口径（2026-10-11 第二轮）：
        > 「运营总览中只有一个菜单不需要做出两级，你参考 antd 中的菜单组件，
        >  将当前的菜单调整为合理的二级菜单结构」

        ⚠️ 断言三件事，缺一都不算「合理」：
        ① 三个业务线是 **SubMenu**（有 `children`、key 不是路由）；
        ② 「总览」**没有** children（只有一个菜单项 ⇒ 不做两级）；
        ③ 「平台」逐字（助手原推荐「公共」被用户否决）。
        """
        shell = _code_only(SHELL)
        # ① 三个二级菜单的父级 key 在，且都装了子项
        parents = _parent_keys(shell)
        for parent in ("content-line", "shop-line", "platform"):
            assert parent in parents, f"缺少二级菜单父级 {parent}"
            assert f'key: "{parent}"' in shell
        for label in ("内容线", "店铺线", "平台"):
            assert f'label: "{label}"' in shell, f"缺少二级菜单「{label}」"
        assert 'label: "公共"' not in shell, "第三项名是「平台」（用户拍板），不是「公共」"

        # ② 「总览」是一级叶子：**按具名常量的体**断言，不按路由字符串 ——
        #    路由字符串在文件里出现两次（常量本身 + 万一被塞进 children），
        #    「字符串存在」型断言两次都能满足（实测：给它加一层 children 时 ESCAPED）。
        overview_body = _item_by_key(shell, "/platforms/xhs/dashboard")
        assert '"/platforms/xhs/dashboard"' in overview_body, "总览项必须指向总览路由"
        assert "children" not in overview_body, (
            "「总览」只有一个菜单项，按用户口径不做两级（不得给它挂 children）"
        )
        # ⚠️ 反向两条，都是实测 ESCAPED 后补的：
        #    ① 「总览路由只出现一次」不够 —— 把它**下沉**成唯一子项时也还是 1 次。
        #       真正区分的是「总览没有父级」⇒ 断言不存在 `key: "overview"` 这种父节点。
        #    ② 用「唯一子项」形态时，父级 key 必然是个非路由标识。
        assert shell.count('"/platforms/xhs/dashboard"') == 1, (
            "总览路由只应出现一次（多点一层会让它在父子两处各出现一次）"
        )
        assert 'key: "overview"' not in shell and 'label: "运营总览"' not in shell, (
            "「总览」不得有父级 —— 用户口径：只有一个菜单不做出两级"
        )

        # ③ 顺序：**用户口径**（2026-10-11）「菜单把平台放第二位」
        #    ⇒ 总览 → 平台 → 内容线 → 店铺线。
        #    ⚠️ 断言**数组里的相对位置**，不按单条存在性 —— 顺序本身就是这次的口径，
        #    「三项都在」对顺序漂移毫无约束（谁重排一下都照样绿）。
        order = _parent_keys(shell)
        assert order == ["platform", "content-line", "shop-line"], (
            f"二级菜单顺序必须是 平台 → 内容线 → 店铺线（用户口径），实际 {order}"
        )

        # ④ 旧形态（type: "group" 分组标题）已被取代
        assert 'type: "group"' not in shell, (
            "分组标题（type: \"group\"）已被二级菜单取代 —— 用户口径要求「二级菜单结构」"
        )

    def test_every_page_is_filed_under_its_business_line(self):
        """12 个业务线页面各自在**正确的组**里。

        判据 = 该页需要哪条业务线的令牌（`core/business_lines.py` 的口径），
        不是页面名字里的字面提示。

        ⚠️ 断言分两跳（**接线**，不是「字符串在文件里出现过」）：
        ① 分组 → 它引用的那个常量（`children: <Const>`）；
        ② 那个常量的**体**里 → 路由。
        只查①或只查②都会漏：路由在文件里存在、但被搬进另一个常量时，
        「出现过」型断言照样绿（变异逃逸）。
        """
        shell = _code_only(SHELL)
        for label, routes in EXPECTED_GROUPS.items():
            const_name = _group_target(shell, label)
            body = _const_body(shell, const_name)
            for route in routes:
                assert f'"{route}"' in body, (
                    f"路由 {route} 应在「{label}」组（常量 {const_name}）里"
                )

        # 反向：页面**不得**跑进别的线（跨组重复 = 归位判据失效）。
        for label, routes in EXPECTED_GROUPS.items():
            const_name = _group_target(shell, label)
            body = _const_body(shell, const_name)
            others = {
                r
                for other, rs in EXPECTED_GROUPS.items()
                if other != label
                for r in rs
            }
            for route in others:
                assert f'"{route}"' not in body, (
                    f"路由 {route} 不该出现在「{label}」组（常量 {const_name}）里"
                )

    def test_no_menu_entry_or_route_was_lost(self):
        """分组是**重组**不是删减：入口数与路由条目数都不许变。

        这是本批里最容易被无声破坏的一条 —— 搬组时漏掉一项，界面看着仍然「有分组」，
        只有数数才发现少了一个入口。
        """
        shell = _code_only(SHELL)
        entries = shell.count('key: "/')
        assert entries == EXPECTED_MENU_ENTRIES, (
            f"侧栏入口应为 {EXPECTED_MENU_ENTRIES} 个，实测 {entries} —— "
            "分组是重组，不许删入口"
        )

        router = ROUTER.read_text(encoding="utf-8")
        paths = router.count("path=")
        assert paths == EXPECTED_ROUTE_PATHS, (
            f"路由条目应为 {EXPECTED_ROUTE_PATHS} 条，实测 {paths} —— 分组不得动路由表"
        )

    def test_grouping_is_a_real_submenu_not_a_group_header(self):
        """三个业务线是**真二级菜单**（SubMenu），不是只读的组标题。

        用户口径（2026-10-11 第二轮）要求「合理的**二级菜单结构**」。
        组标题（`type: "group"`）是**平的** —— 它只是给一列同级项加个灰字抬头，
        不能折叠、也不表达层级；SubMenu 才是有层级的菜单。

        ⚠️ 同时钉住「层级与页面内页签同形」：笔记工坊的 6 个页签必须是
        **宿主页的子节点**（`children` 里带 `?tab=`），而不是 6 个并列的一级项 ——
        后者会出现「6 个入口指向同一 pathname、只有 query 不同」，
        正是 antd 菜单表达不了、也最让人迷惑的形状。
        """
        shell = _code_only(SHELL)
        assert 'type: "group"' not in shell, "已被二级菜单取代（用户口径）"
        assert "children:" in shell, "二级菜单必须有 children"
        # 三级：内容线 → 笔记工坊 → 6 个页签。
        # ⚠️ 切片的**右界要按「宿主项自身」定**：`],` 在文件里到处都有
        #    （其它 children 数组也以 `],` 收尾），拿它当右界会把切片拉长到
        #    文件末尾 ⇒ 断言退化成「整个文件里有没有 ?tab=」（实测 ESCAPED）。
        #    这里改用 Python 的括号配对，把「笔记工坊」那一项**完整**切出来。
        workshop_item = _item_by_key(shell, "/platforms/xhs/note-workshop")
        for tab in ("images", "videos", "drafts", "publish", "materials", "prompts"):
            assert f"?tab={tab}" in workshop_item, f"笔记工坊页签 {tab} 未挂在宿主页之下"
        # 反向：这 6 个页签**只能**出现在这一项里（摊平到一级就会漏到这里）。
        total_tabs = shell.count("?tab=")
        assert total_tabs == 6, (
            f"`?tab=` 应只出现在笔记工坊的 6 个子项里，实测 {total_tabs} 处"
            "（多了 = 被摊平成一级菜单项）"
        )

        # 高亮判据必须处理「宿主页?tab=」这种 key（否则 6 个页签同时亮）。
        ws = _code_only(ROOT / "frontend/src/components/layout/workspace-shell.tsx")
        # ⚠️ 断言**接线**而不是「名字出现过」：实测只查 `"openKeys" in ws` 时，
        #    把 `openKeys={openKeys}` 从 <Menu> 上摘掉仍 ESCAPED（名字还在其它地方）。
        assert "const activeKeys = selectedKeys ?? resolveActiveKeys(" in ws, (
            "高亮必须真的走 resolveActiveKeys（否则 6 个页签同时亮）"
        )
        assert "openKeys={openKeys}" in ws, "受控 openKeys 必须真的传给 <Menu>"
        assert "onOpenChange" in ws, "缺 onOpenChange 则用户手动展开/收起无效"


# --------------------------------------------------------------------------- #
# 设计 32 §5.2 · 页面业务线标识条（A4）
# --------------------------------------------------------------------------- #

BADGE = ROOT / "frontend/src/components/layout/business-line-badge.tsx"
BUSINESS_LINES_TS = ROOT / "frontend/src/lib/business-lines.ts"


def _route_map(code: str) -> dict[str, str]:
    """把 `ROUTE_BUSINESS_LINES` 解析成 `{路由: 业务线}`。

    ⚠️ 用正则从**代码**里读真值表，而不是把期望值再抄一遍 ——
    抄一遍的话，两张表一起改错时守卫照样绿（自我印证）。
    真值来源是**侧栏分组**（下面那条双向比对用例）。
    """
    body = _const_body(code, "ROUTE_BUSINESS_LINES")
    pairs = re.findall(r'"(/platforms/xhs/[a-z-]+)":\s*"(\w+)"', body)
    assert pairs, "ROUTE_BUSINESS_LINES 解析为空 —— 正则与写法不符，守卫会假绿"
    return dict(pairs)


class TestBusinessLineBadge:
    """页面业务线标识条：**每个业务线页面都要标出自己属于哪条线**。

    为什么需要守卫：「漏标一个页面」在界面上**看不出来**（那条页面只是少了个标签，
    不报错、不 500），而它恰恰是这套 IA 的全部价值所在。
    """

    def test_badge_is_mounted_once_in_the_shell(self):
        """标识条挂在 **AppShell 单一挂载点**，不是 12 个页面里各挂一次。

        ⚠️ 断言「调用形态 + 只能挂一处」：12 份拷贝必然漂移，且漏挂一处看不出来。
        """
        shell = _code_only(SHELL)
        assert "<BusinessLineBadge />" in shell, "AppShell 必须挂载业务线标识条"
        assert shell.count("<BusinessLineBadge />") == 1, "只允许一个挂载点"
        assert "BusinessLineBadge" in shell.split("import")[1] or "business-line-badge" in shell

        # 反向：不许在页面里各挂一份（那会让「漏挂」重新变成不可见的状态）。
        offenders = [
            str(path.relative_to(ROOT))
            for path in (ROOT / "frontend/src/pages").rglob("*.tsx")
            if "<BusinessLineBadge" in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], f"标识条只许挂在 AppShell，这些页面里出现了副本：{offenders}"

    def test_route_map_covers_every_business_line_page_bidirectionally(self):
        """路由 → 业务线的映射与**侧栏分组**双向一致。

        * ① 侧栏每条线里的每个路由，映射里都要有，且线名一致；
        * ② 映射里的每个路由，都要能在侧栏里找到（不许留幽灵条目）。

        为什么是**双向**：单向只能发现「映射少了」，发现不了「映射多了」（幽灵条目
        会让一个已下线的页面继续显示业务线标签）。真值来源是侧栏分组，不是本测试的常量。
        """
        shell = _code_only(SHELL)
        lib = _code_only(BUSINESS_LINES_TS)
        route_map = _route_map(lib)

        # 侧栏分组 → 期望映射
        expected: dict[str, str] = {}
        for label, line in (("内容线", "content"), ("店铺线", "shop"), ("平台", "shared")):
            const_name = _group_target(shell, label)
            body = _const_body(shell, const_name)
            for route in re.findall(r'"(/platforms/xhs/[a-z-]+)"', body):
                expected[route] = line

        missing = sorted(set(expected) - set(route_map))
        extra = sorted(set(route_map) - set(expected))
        assert missing == [], f"这些业务线页面在 ROUTE_BUSINESS_LINES 里漏登记：{missing}"
        assert extra == [], f"这些路由是幽灵条目（侧栏已无此页）：{extra}"

        wrong = {
            route: (route_map[route], expected[route])
            for route in expected
            if route_map[route] != expected[route]
        }
        assert wrong == {}, f"业务线归属与侧栏分组不一致（路由: 映射值/侧栏值）：{wrong}"

    def test_shared_line_has_no_channel(self):
        """「平台」这条线**没有令牌通道** —— 给它配一个等于承认平台线要令牌。"""
        lib = _code_only(BUSINESS_LINES_TS)
        shared_block = lib[lib.index("shared: {") :]
        shared_block = shared_block[: shared_block.index("},")]
        assert 'channel: ""' in shared_block, "shared 的 channel 必须为空"
        assert 'label: "平台"' in shared_block, "组名是「平台」（用户拍板），不是「公共」"
        assert "公共" not in lib, "「公共」是被否决的名字，不许在映射里复活"

    def test_captions_name_the_business_line_and_its_token(self):
        """三行 caption **逐字** —— 它们就是用户看到的标识。

        ⚠️ 断言 caption 逐字而不是「含某个词」：换成「本页」这类泛化词时，
        含词断言照样绿，而那正是这个标识失效的形态。
        """
        lib = _code_only(BUSINESS_LINES_TS)
        for caption in ("内容线 · 创作者令牌", "店铺线 · 千帆令牌", "平台 · 跨业务线"):
            assert f'caption: "{caption}"' in lib, f"缺少标识文案「{caption}」"

    def test_badge_component_never_calls_the_platform(self):
        """标识条**零平台请求** —— 它只标注，不替业务页面判可用性。

        前端重判必然与后端漂移（先例：设计 12 §17.8 删掉 `capabilityFor`）。

        ⚠️ 必须**同时**禁止「调用」与「引入 API 模块」：只禁调用的话，
        `import { fetchAccounts } from "../../lib/api"` 这种「先引入、还没调用」
        的中间态照样绿 —— 而它正是漂移的**第一步**（引入之后，下一步就是在
        组件里 `useEffect` 一下，那时 guard 才红，已经晚了一轮）。
        """
        badge = _code_only(BADGE)
        for forbidden in ("http.", "fetch(", "axios", "useEffect", "lib/api"):
            assert forbidden not in badge, (
                f"标识条不得出现 `{forbidden}` —— 它只标注，不发请求、不判可用性、"
                "也不引入任何 API 模块"
            )


# --------------------------------------------------------------------------- #
# 设计 32 §5.4 · 统一账号范围条（A5）
# --------------------------------------------------------------------------- #

ACCOUNT_SCOPE = ROOT / "frontend/src/lib/account-scope.ts"

#: 页面 → 它该用的那条业务线（与侧栏分组一致）。
PAGES_USING_SHARED_SCOPE: dict[str, str] = {
    "frontend/src/pages/platforms/xhs/my-notes-page.tsx": "content",
    "frontend/src/pages/platforms/xhs/shop-items-page.tsx": "shop",
}

#: 内容库的账号是**筛选条件**（`""` = 不筛），语义不同 ⇒ 不替换成 scope，
#: 但必须**同步**过去（否则同一业务线内各页各记一份）。
LIBRARY_PAGE = "frontend/src/pages/platforms/xhs/library-page.tsx"


class TestAccountScope:
    """「当前账号」按**业务线**跨页共享（此前是每页一份，切换后互相看不见）。

    为什么需要守卫：把某一页改回 `usePersistedFilter(FILTER_CACHE_KEYS.<page>.accountId)`
    **不会有任何断言变红** —— 页面照常工作，只是账号又变成「每页各记一份」，
    而「用错账号操作」是**数据隔离**问题（ADR-021），不是体验问题。
    """

    def test_pages_use_the_business_line_scope_not_per_page_keys(self):
        """内容线 / 店铺线的页面必须用 `useAccountScope("<线>")`。"""
        for rel, line in PAGES_USING_SHARED_SCOPE.items():
            code = _code_only(ROOT / rel)
            assert f'useAccountScope("{line}")' in code, (
                f"{rel} 必须用业务线级账号范围 useAccountScope(\"{line}\")"
            )
            # 反向：不许再用页面级账号键（两条路同时存在 = 两份真相源）。
            for legacy in ("myNotes.accountId", "shopItems.accountId"):
                assert legacy not in code, (
                    f"{rel} 不得再读页面级账号键 {legacy} —— "
                    "账号已改为业务线级共享（设计 32 §5.4）"
                )

    def test_library_filter_syncs_into_the_content_scope(self):
        """内容库的账号筛选必须**同步**进内容线范围（双向可见）。

        内容库的账号是**筛选条件**（`""` = 不筛），与「当前账号」语义不同 ⇒
        保留 `accountFilter`，但要把它写进 `useAccountScope("content")`，
        否则「内容库选 A → 我的笔记」还是各记一份。
        """
        code = _code_only(ROOT / LIBRARY_PAGE)
        assert 'useAccountScope("content")' in code, "内容库必须把账号筛选同步到内容线范围"
        assert "setContentAccountScope" in code, "内容库必须真的写进范围（不只是读）"
        # ⚠️ 空串（不筛）不许写进范围 —— 那会把用户在别的页选的账号清掉。
        assert 'accountFilter === ""' in code, "空串（不筛）必须提前 return，不许写进范围"

    def test_scope_migration_reads_legacy_keys_once(self):
        """迁移路径必须存在：旧键（每页一份）要能被读一次。

        没有它，老用户已经选好的账号会被**静默重置**成「未选」——
        界面上不报错，只是账号选择不见了。
        """
        lib = _code_only(ACCOUNT_SCOPE)
        # ⚠️ 锚点必须是**声明形态**（含 `export const … :` 与冒号），不能只查名字：
        #    `LEGACY_ACCOUNT_KEYS_RENAMED` 这种「改名」在子串断言下照样命中 ⇒ 假绿
        #    （实测：改名变异 ESCAPED，守卫本身没错、是锚点太弱）。
        assert "export const LEGACY_ACCOUNT_KEYS:" in lib, "必须保留旧键迁移源"
        assert "clearFilterCache(legacyKey);" in lib, "迁移后必须清旧键（否则新键缺失时会被旧值覆盖回来）"
        for legacy in (
            "xhs.filters.library.account-id",
            "xhs.filters.my-notes.account-id",
            "xhs.filters.shop-items.account-id",
        ):
            assert legacy in lib, f"迁移源缺少旧键 {legacy}"

    def test_scope_is_per_business_line_not_global(self):
        """三条线各一个键 —— **不是**全局一个。

        全局一份会强迫用户在两条线之间来回改账号（两条线的**工作对象**完全不同：
        商品台账 vs 笔记）。
        """
        lib = _code_only(ACCOUNT_SCOPE)
        for key in (
            "xhs.scope.content.account-id",
            "xhs.scope.shop.account-id",
            "xhs.scope.shared.account-id",
        ):
            assert key in lib, f"缺少业务线级缓存键 {key}"

    def test_sanitize_rejects_non_positive_values(self):
        """`sanitizeAccountId` 只收正数，其余一律 `null`。

        ⚠️ 落到 `0` 或 `NaN` 会让「按 id 取账号」**静默取到错的行**
        （比「没选账号」危险得多 —— 那是拿别的账号的数据在操作）。
        """
        lib = _code_only(ACCOUNT_SCOPE)
        assert "raw > 0" in lib, "sanitizeAccountId 必须判 > 0"
        assert "Math.trunc" in lib, "sanitizeAccountId 必须取整（8.7 不是合法 id）"


# --------------------------------------------------------------------------- #
# 设计 32 §6 · 数据洞察按业务线拆页（A6）
# --------------------------------------------------------------------------- #

INSIGHTS_CONTENT = ROOT / "frontend/src/pages/platforms/xhs/insights-content-page.tsx"
INSIGHTS_SHOP = ROOT / "frontend/src/pages/platforms/xhs/insights-shop-page.tsx"
METRICS_PANEL = ROOT / "frontend/src/pages/platforms/xhs/metrics-dashboard-panel.tsx"


class TestInsightsSplit:
    """原「数据洞察」（一页两 Tab）→ 两条业务线**各自的页面**。

    为什么需要守卫：拆页最容易的退化是**复制面板**（两份图表代码）——
    界面上完全正常，直到两边口径漂移、同一指标两个数（最难查的一类缺陷）。
    """

    def test_two_pages_exist_and_are_wired_to_distinct_routes(self):
        """两个页面存在，且挂在**两条业务线各自的**路由上。"""
        router = ROUTER.read_text(encoding="utf-8")
        assert INSIGHTS_CONTENT.exists(), "内容洞察页不存在"
        assert INSIGHTS_SHOP.exists(), "商品洞察页不存在"
        assert 'path="/platforms/xhs/insights/content"' in router
        assert 'path="/platforms/xhs/insights/shop"' in router
        assert "InsightsContentPage" in router
        assert "InsightsShopPage" in router

    def test_pages_pass_their_scope_to_one_shared_panel(self):
        """两页各自把 `scope` 传给**同一个**面板 —— **不许各写一份图表**。

        ⚠️ 这是本组最承重的一条：断言「两处都调 `<MetricsDashboardPanel`」+
        「面板**只有一份文件**」。若哪天有人把图表复制进某一页，
        第二条断言（面板文件里没有任何一页的私有拷贝）会钉住它。
        """
        content = _code_only(INSIGHTS_CONTENT)
        shop = _code_only(INSIGHTS_SHOP)
        assert '<MetricsDashboardPanel scope="note"' in content, "内容洞察页必须传 scope=\"note\""
        assert '<MetricsDashboardPanel scope="shop"' in shop, "商品洞察页必须传 scope=\"shop\""
        # 面板只允许一个实现文件（复制一份 = 两份真相源）。
        panel_copies = sorted(
            p.relative_to(ROOT).as_posix()
            for p in (ROOT / "frontend/src").rglob("*metric*dashboard*.tsx")
        )
        assert panel_copies == ["frontend/src/pages/platforms/xhs/metrics-dashboard-panel.tsx"], (
            f"指标面板只允许一份实现，实测：{panel_copies}"
        )

    def test_panel_actually_branches_on_scope(self):
        """面板真的按 `scope` 分流 —— 不是「收下参数然后照旧全渲染」。

        ⚠️ 只断言「参数在」会被「收下但不用」的写法骗过（那是**假拆分**：
        两个页面看着分开，实际都在拉两条线的数据）。
        """
        panel = _code_only(METRICS_PANEL)
        assert "scope" in panel
        assert "showNote" in panel and "showShop" in panel, "面板必须把 scope 落成两个分流标志"
        # ⚠️ **分流标志必须真的从 scope 派生**（不能是常量）：
        #    写成 `const showNote = true` 时，下面的「调用形态」断言**照样绿**
        #    （`showNote ? fetch…` 这个字符串还在），但两个页面实际都在拉两条线的数据
        #    = 假拆分。实测：只查调用形态时，这个变异 ESCAPED —— 所以这一条是承重的。
        assert 'const showNote = scope === "note" || scope === "both";' in panel, (
            "showNote 必须由 scope 派生（写成常量 = 面板忽略 scope）"
        )
        assert 'const showShop = scope === "shop" || scope === "both";' in panel, (
            "showShop 必须由 scope 派生（写成常量 = 面板忽略 scope）"
        )
        # 两个方向的请求都要能被跳过（否则拆了页还是每次拉两条线的数据）。
        assert "showNote ? fetchNoteMetricsTrend" in panel, "笔记趋势必须受 scope 控制"
        assert "showShop ? fetchShopMetricsTrend" in panel, "商品趋势必须受 scope 控制"
        assert "showNote ? collectNoteMetrics" in panel, "笔记采集必须受 scope 控制"
        assert "showShop ? collectShopMetrics" in panel, "商品采集必须受 scope 控制"

    def test_legacy_analytics_route_still_redirects(self):
        """旧 `/platforms/xhs/analytics` **保留为重定向**（深链与书签不死）。"""
        legacy = _code_only(ROOT / "frontend/src/pages/platforms/xhs/analytics-page.tsx")
        assert "<Navigate" in legacy, "旧路由必须重定向，不能 404"
        assert "insights/content" in legacy and "insights/shop" in legacy
        # 页面文件里不该再有旧的指标图表实现（它整体搬走了）。
        assert "MetricsDashboardPanel" not in legacy, "旧页只做跳转，不该再渲染面板"
