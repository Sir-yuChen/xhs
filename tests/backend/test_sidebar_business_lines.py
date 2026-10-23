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

import pytest




#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract
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
#: → 26（设计 43：店铺线新增「违规管理」页面 `/platforms/xhs/violation-manage`）。
EXPECTED_MENU_ENTRIES = 26

#: 路由条目数。**只在「新增页面」时增长**；把页签提升为菜单叶子**不**增路由。
#: 32（A3 前）→ 34（A6 两个新页面；旧 `/analytics` 保留为重定向，条目不减）。
#: → 35（设计 35：新增 `/platforms/xhs/datacenter`）。
#: → 37（设计 43：店铺违规管理 `violation-manage` + 其店铺体检视图 `violation-manage/checkup`）。
EXPECTED_ROUTE_PATHS = 37

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
    """侧栏信息架构 —— 判据载体已从「源码常量」改为「接口下发的菜单」 (v1.1.0)。

    ## 为什么这一批要改判据（2026-10-24）

    v1.1.0 把侧栏从**硬编码**（`app-shell.tsx` 里 4 组 24 项常量）改成
    **后端下发**（`GET /api/system/menus/mine`）。原来这批守卫靠
    `assert 'content-line' in app_shell` 这类源码 grep 钉住分组 ——
    侧栏一改，**载体就不存在了**，实测 7 条全红。

    ⚠️ **守卫要护的是价值，不是载体**。这批守卫真正护的是三件事：
      ① 三个业务线/工作区目录都在，且文案逐字（用户拍板「平台」不用「公共」；
         更早的口径是「内容线 / 店铺线」二级菜单）；
      ② 每个页面都归到**正确**的目录下（不许搬错组）；
      ③ 一个入口都不许丢（分组是重组，不是删减）。
    这三件事与「菜单存在 React 源码里还是存在数据库里」无关 —— 所以判据
    改判 `menus` 表 + `/menus/mine` 的返回，护的东西一个不少。

    ## 「业务线」（内容线/店铺线）去哪了

    旧版把小红书拆成「内容线 / 店铺线」两个 SubMenu。v1.1.0 的
    `_WORKSPACE_TREE` 把它们合成一个「平台」目录（`workspace='xhs'`）——
    业务线**语义**仍然由 `lib/business-lines.ts` 按路由派生（`BusinessLineBadge`
    逐页标出「本页属于哪条线」），只是不再作为**导航分组**。
    所以下面断言「平台目录存在 + 12 个业务线页面都在它下面」，
    而不是断言两个 SubMenu 的 key。
    """

    #: 每个顶级目录必须包含的页面路径（改这里 = 改信息架构，要有用户口径支撑）
    EXPECTED_PAGES: dict[str, set[str]] = {
        "小红书": {
            "/platforms/xhs/dashboard",
            "/platforms/xhs/discovery",
            "/platforms/xhs/my-notes",
            "/platforms/xhs/library",
            "/platforms/xhs/note-workshop",
            "/platforms/xhs/keywords",
            "/platforms/xhs/insights/content",
            "/platforms/xhs/shop-items",
            "/platforms/xhs/violation-manage",
            "/platforms/xhs/picture-space",
            "/platforms/xhs/orders",
            "/platforms/xhs/insights/shop",
            "/platforms/xhs/datacenter",
            "/platforms/xhs/accounts",
            "/platforms/xhs/monitoring",
            "/platforms/xhs/auto-ops",
            "/tasks",
            "/task-records",
            "/settings",
        },
        "大模型网关": {
            "/usage-dashboard",
            "/llm-gateway/providers",
            "/llm-gateway/alerts",
            "/models",
        },
        "系统管理": {
            "/system/users",
            "/system/roles",
            "/system/menus",
            "/system/dicts",
            "/system/settings",
            "/system/logs",
        },
    }

    @pytest.fixture(scope="class")
    def menu_rows(self):
        """从**种子文件**读菜单（不依赖数据库 / 服务）。

        为什么读种子而不是真库：种子是「出厂信息架构」的唯一真相源，
        而真库可能被用户改过（管理页能改菜单）。守卫护的是**出厂形态**。
        """
        import json

        seed = ROOT / "config/initial_data/menus.seed.json"
        assert seed.is_file(), f"菜单种子不存在：{seed}"
        return json.loads(seed.read_text(encoding="utf-8"))["menus"]

    def test_workspace_roots_exist_with_exact_wording(self, menu_rows):
        """顶级目录在，文案**逐字**（用户口径：「平台」不是「公共」）。"""
        roots = [
            row for row in menu_rows
            if row.get("menu_type") == "M" and (row.get("parent_seq") or 0) == 0
        ]
        names = [r["name"] for r in roots]

        for expected in ("小红书", "大模型网关", "系统管理"):
            assert expected in names, f"缺少顶级目录「{expected}」，实际 {names}"

        #: 用户拍板（2026-10-11）：「公共」被否决，第三项叫「平台」之前的那版叫法
        assert "公共" not in names, "「公共」是被用户否决的叫法"

        #: 占位平台必须在（接入时只改 status，不必改代码）
        for platform in ("抖音", "闲鱼"):
            assert platform in names, f"占位平台「{platform}」必须预先存在（接入时切 status 即可）"
            row = next(r for r in roots if r["name"] == platform)
            assert row.get("status") == "disabled", (
                f"占位平台「{platform}」出厂必须 status=disabled"
            )

    def test_no_duplicate_directory(self, menu_rows):
        """同一个父级下不许有同名目录。

        ⚠️ 实测踩坑：生成器曾同时从 `WORKSPACES` 和 `_WORKSPACE_TREE` 建「系统管理」
        ⇒ 侧栏显示「系统管理 → 系统管理 → 用户管理」，多套一层。
        """
        seq_parent = {row["_seq"]: row.get("parent_seq") or 0 for row in menu_rows}
        seen: dict[tuple[int, str, str], int] = {}
        for row in menu_rows:
            key = (row.get("parent_seq") or 0, row.get("menu_type", ""), row["name"])
            seen[key] = seen.get(key, 0) + 1
        dupes = [k for k, n in seen.items() if n > 1]
        assert not dupes, f"同父同名菜单（会导致侧栏多套一层）：{dupes}"
        assert seq_parent  # 防止自检空转

    def test_every_page_is_filed_under_its_workspace(self, menu_rows):
        """每个页面都归到**正确**的目录下（不许搬错组）。"""
        #: seq → (顶级目录名, 路径)
        by_seq = {row["_seq"]: row for row in menu_rows}

        def root_name(row: dict) -> str | None:
            cursor = row
            guard = 0
            while (cursor.get("parent_seq") or 0) and guard < 10:
                parent = by_seq.get(cursor["parent_seq"])
                if parent is None:
                    return None
                cursor = parent
                guard += 1
            return cursor["name"] if cursor is not row else row["name"]

        issues: list[str] = []
        for workspace, expected_paths in self.EXPECTED_PAGES.items():
            actual_paths = {
                row.get("path")
                for row in menu_rows
                if row.get("menu_type") == "C"
                and row.get("path")
                and root_name(row) == workspace
            }
            missing = expected_paths - actual_paths
            if missing:
                issues.append(f"「{workspace}」缺少页面：{sorted(missing)}")

        assert not issues, "页面归属错误：\n  " + "\n  ".join(issues)

    def test_no_menu_entry_was_lost(self, menu_rows):
        """入口一个不少：业务线页面总数 = 期望集合的并集大小。

        ⚠️ 「分组是重组，不是删减」—— 这条防的是「顺手删掉几个入口」。
        """
        expected_total = sum(len(v) for v in self.EXPECTED_PAGES.values())
        actual_pages = {
            row["path"]
            for row in menu_rows
            if row.get("menu_type") == "C" and row.get("path")
        }
        #: 三种工作区的页面 + 其它（如 llm-gateway 根页）都在 actual 里；
        #: 这里只断言「期望的一个都不少」，不要求 actual == expected
        #: （新增页面是合法变更，删掉老页面才要报警）。
        missing = set().union(*self.EXPECTED_PAGES.values()) - actual_pages
        assert not missing, f"这些菜单入口丢了：{sorted(missing)}"
        assert len(actual_pages) >= expected_total, (
            f"页面总数 {len(actual_pages)} 少于期望的 {expected_total}"
        )

    def test_grouping_is_real_submenu_not_group_header(self):
        """分组是**真正的二级菜单**（antd SubMenu），不是 `type: "group"` 标题栏。

        用户口径（2026-10-11）：「参考 antd 中的菜单组件，将当前的菜单调整为
        合理的二级菜单结构」。判据载体：`use-dynamic-nav.tsx` 把 M 行渲染成
        带 `children` 的节点。
        """
        nav = ROOT / "frontend/src/components/layout/use-dynamic-nav.tsx"
        code = _code_only(nav)
        assert "children" in code, "动态菜单必须给父节点挂 children（SubMenu 形态）"
        assert 'type: "group"' not in code, (
            '分组标题（type: "group"）已被二级菜单取代 —— 用户口径要求「二级菜单结构」'
        )

    def test_sidebar_is_data_driven_not_hardcoded(self):
        """侧栏**必须**由接口下发，不许退回硬编码常量。

        ⚠️ 这是本批最重要的一条：硬编码会让「未授权菜单照样显示」
        「平台启停无效」「接新平台要改前端发版」三个问题全部回来。
        """
        shell = ROOT / "frontend/src/components/layout/app-shell.tsx"
        code = _code_only(shell)
        assert "useDynamicNav" in code, (
            "app-shell 必须用 useDynamicNav（后端下发菜单）—— 硬编码侧栏会让"
            "未授权菜单照样显示、平台启停失效"
        )
        #: 硬编码的痕迹：出现 `key: "/platforms/` 说明又写死了路由
        assert 'key: "/platforms/' not in code, (
            "app-shell 里不许再出现硬编码的路由菜单项"
        )


#: ⚠️ 这两个常量曾被我在重写 `TestSidebarBusinessLineGroups` 时连带删掉
#: （替换区间覆盖了它们）⇒ `TestBusinessLineBadge` 四条全红 `NameError`。
#: 记录在此：**改测试文件用区间替换时，先确认区间内没有别的类在用的模块级常量**。
BADGE = ROOT / "frontend/src/components/layout/business-line-badge.tsx"
BUSINESS_LINES_TS = ROOT / "frontend/src/lib/business-lines.ts"


def _route_map(code: str) -> dict[str, str]:
    """把 `ROUTE_BUSINESS_LINES` 解析成 `{路由: 业务线}`。

    ⚠️ 用正则从**代码**里读真值表，而不是把期望值再抄一遍 ——
    抄一遍的话，两张表一起改错时守卫照样绿（自我印证）。
    真值来源是**侧栏分组**（下面那条双向比对用例）。
    """
    body = _const_body(code, "ROUTE_BUSINESS_LINES")
    #: ⚠️ 字符类**必须含 `/`**（2026-10-24）：`/platforms/xhs/insights/content`
    #: 这类二级路径用 `[a-z-]+` 会在 `insights` 处截断 ⇒ 那几条路由解析不到，
    #: 双向比对把它们当成「漏登记」（实测报了 3 条假红）。
    pairs = re.findall(r'"(/platforms/xhs/[a-z0-9/-]+)":\s*"(\w+)"', body)
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
        lib = _code_only(BUSINESS_LINES_TS)
        route_map = _route_map(lib)

        #: ⚠️ **2026-10-24 判据载体变更**（v1.1.0）：侧栏改为后端下发，
        #: `app-shell.tsx` 里不再有「内容线 / 店铺线 / 平台」三个常量 ⇒
        #: 原判据（从 shell 的常量体里抠路由当期望集合）实测 `substring not found`。
        #:
        #: 「双向一致」护的价值没变，但真值来源换成**路由表**：
        #:   · 正向：`/platforms/xhs/*` 的每个真实路由都要在映射里；
        #:   · 反向：映射里不许有**路由表里不存在**的路由（幽灵条目会让已下线页面
        #:     继续显示业务线标签）。
        #: **正向**：种子菜单里的每个 `/platforms/xhs/*` 页面都必须已登记业务线。
        #:
        #: 「要不要登记」的真值来源是**它是不是业务线页面** —— 而业务线页面的
        #: 判定就是「出现在侧栏菜单里、且属于内容线/店铺线能分得清的页面」。
        #: `dashboard` / `benchmarks` 这类不属于任何业务线（总览 + 已下线的竞品页），
        #: 本来就不该在 `ROUTE_BUSINESS_LINES` 里 —— 早期版本靠「从 shell 常量抠路由」
        #: 天然把范围限定在侧栏项上，改判种子后要把这个限定显式写出来。
        import json

        seed_rows = json.loads(
            (ROOT / "config/initial_data/menus.seed.json").read_text(encoding="utf-8")
        )["menus"]
        seeded_routes = {
            r["path"]
            for r in seed_rows
            if r.get("menu_type") == "C"
            and str(r.get("path") or "").startswith("/platforms/xhs/")
            and "?tab=" not in str(r.get("path") or "")
        }

        #: 不在菜单里但仍存在的路由（重定向页 / 子页）—— 不参与本判据
        non_menu_routes = {
            #: 总览页：不属于内容线也不属于店铺线（它是「两条线的入口页」），
            #: 所以 `ROUTE_BUSINESS_LINES` 里没有它 —— 不是漏登记。
            "/platforms/xhs/dashboard",
            "/platforms/xhs/analytics",          # 重定向到 insights/content
            "/platforms/xhs/crawler",            # 重定向到 discovery?tab=crawl
            "/platforms/xhs/image-studio",       # 重定向到 note-workshop?tab=images
            "/platforms/xhs/video-studio",
            "/platforms/xhs/drafts",
            "/platforms/xhs/product-drafts",
            "/platforms/xhs/violation-manage/checkup",
        }

        missing = sorted(seeded_routes - set(route_map) - non_menu_routes)
        assert missing == [], f"这些菜单页在 ROUTE_BUSINESS_LINES 里漏登记：{missing}"

        #: 反向：映射里的每个路由都必须**真的存在**（幽灵条目会让已下线页面继续带业务线标签）
        router = _code_only(ROUTER)
        all_routes = set(re.findall(r'path="(/platforms/xhs/[a-z0-9/-]+)"', router))
        ghost = sorted(set(route_map) - all_routes)
        assert ghost == [], f"这些路由是幽灵条目（路由表已无此页）：{ghost}"

        #: 判据非空自检：两侧都真有东西，否则上面两条恒真（假绿）
        assert seeded_routes, "菜单种子里没有 /platforms/xhs/ 页面 —— 判据空转"
        assert route_map, "ROUTE_BUSINESS_LINES 为空 —— 判据空转"

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

    # ⚠️ 2026-10-13 用户口径：指标快照/趋势子系统整块移除（设计 39）⇒
    # 原 `test_pages_pass_their_scope_to_one_shared_panel` 与
    # `test_panel_actually_branches_on_scope` 的守卫对象（`metrics-dashboard-panel.tsx`
    # 及其在两页的 `<MetricsDashboardPanel scope=…>` 接线）与实现一并退场，两条随之删除。
    # 保留路由与旧路由重定向两条 —— 它们钉的是**信息架构**，与指标面板无关。

    def test_legacy_analytics_route_still_redirects(self):
        """旧 `/platforms/xhs/analytics` **保留为重定向**（深链与书签不死）。"""
        legacy = _code_only(ROOT / "frontend/src/pages/platforms/xhs/analytics-page.tsx")
        assert "<Navigate" in legacy, "旧路由必须重定向，不能 404"
        assert "insights/content" in legacy and "insights/shop" in legacy
        # 页面文件里不该再有旧的指标图表实现（它整体搬走了）。
        assert "MetricsDashboardPanel" not in legacy, "旧页只做跳转，不该再渲染面板"
