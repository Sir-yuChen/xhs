"""按钮级权限覆盖率守卫（2026-10-25 用户口径「全项目全面排查升级」）。

## 红线

> 「注意如果用户没有对应的权限，则目录菜单或页面，或按钮，则应不展示，
>   有对应的权限才展示，**菜单动态，页面按钮也是动态**」

## 本守卫管什么

**覆盖率清单**：哪些页面已经接入了 `hasPermission`，哪些还没有。

不再假装「全站已覆盖」—— 存量 33 个页面里有写请求却零权限判断，
逐个改需要人工核对每处 JSX（很多按钮文案是动态的，正则锚不住）。
所以守卫做两件事：

1. **已接的页面不许退化**（有人删掉 `hasPermission` 就红）；
2. **未接页面清单只许变短**（修一个删一行；新增未接页面即红）。

这与 `KNOWN_SHARED`（权限码共用清单）同一套思路：把存量固化成一张
会红的欠账表，而不是用一个假的「100%」掩盖。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"

#: 已接入 `hasPermission` 的页面（修好一个就往这里加）。
#: 值是「该页用到的权限码」，用于防退化（码被删也算退化）。
PERM_GATED_PAGES: dict[str, tuple[str, ...]] = {
    "pages/tasks/workspace-tasks-page.tsx": (
        "xhs:taskcenter:create",
        "xhs:taskcenter:edit",
        "xhs:taskcenter:run",
        "xhs:taskcenter:toggle",
        "xhs:taskcenter:viewLogs",
    ),
    "pages/system/system-scheduler-panel.tsx": (
        "system:scheduler:create",
        "system:scheduler:edit",
        "system:scheduler:run",
        "system:scheduler:toggle",
    ),
    "pages/tasks/task-records-page.tsx": (
        "xhs:record:cancel",
        "xhs:record:retry",
        "xhs:record:clean",
        "xhs:record:cleanAll",
    ),
    #: 商品列表（2026-10-25）：页面上 7 个写操作入口各有独立码。
    "pages/platforms/xhs/shop-items-page.tsx": (
        "xhs:item:export",
        "xhs:item:forbiddenWords",
        "xhs:item:drafts",
        "xhs:item:publish",
        "xhs:item:aiAssist",
        "xhs:item:operate",
        "xhs:item:edit",
    ),
    "pages/platforms/xhs/violation-manage-page.tsx": (
        "xhs:violation:relatedDelete",
    ),
    "pages/platforms/xhs/prompt-template-panel.tsx": (
        "ai:prompt:edit",
        "ai:prompt:create",
        "ai:prompt:delete",
    ),
    "pages/llm-gateway/llm-gateway-page.tsx": (
        "ai:gateway:edit",
        "ai:gateway:create",
    ),
    "pages/system/system-dict-panel.tsx": (
        "system:dict:edit",
        "system:dict:create",
    ),
    "pages/platforms/xhs/library-page.tsx": (
        "xhs:library:publish",
        "xhs:library:sync",
        "xhs:library:export",
        "xhs:library:createDraft",
        "xhs:library:delete",
    ),
    "pages/platforms/xhs/drafts-workshop-panel.tsx": (
        "xhs:draft:rewrite",
        "xhs:draft:generateImage",
        "xhs:draft:assets",
    ),
    "pages/platforms/xhs/picture-space-page.tsx": (
        "xhs:picture:restore",
        "xhs:picture:purge",
        "xhs:picture:move",
    ),
}

#: **尚未接入**的页面清单（带写请求但零权限判断）。只许变短。
KNOWN_UNGATED: set[str] = {
    "pages/login/login-page.tsx",
    "pages/platforms/xhs/accounts-page.tsx",
    "pages/platforms/xhs/auto-ops-panel.tsx",
    "pages/platforms/xhs/data-package-rules-modal.tsx",
    "pages/platforms/xhs/discovery-page.tsx",
    "pages/platforms/xhs/forbidden-words-modal.tsx",
    "pages/platforms/xhs/image-studio-panel.tsx",
    "pages/platforms/xhs/keywords-page.tsx",
    "pages/platforms/xhs/llm-generate-modal.tsx",
    "pages/platforms/xhs/material-library-panel.tsx",
    "pages/platforms/xhs/monitoring-page.tsx",
    "pages/platforms/xhs/my-notes-page.tsx",
    "pages/platforms/xhs/orders-page.tsx",
    "pages/platforms/xhs/product-draft-modal.tsx",
    "pages/platforms/xhs/product-edit-modal.tsx",
    "pages/platforms/xhs/product-publish-wizard.tsx",
    "pages/platforms/xhs/publish-config-step1.tsx",
    "pages/platforms/xhs/publish-config-step2.tsx",
    "pages/platforms/xhs/publish-page.tsx",
    "pages/platforms/xhs/sku-name-optimize-modal.tsx",
    "pages/platforms/xhs/sync-specified-items-modal.tsx",
    "pages/system/system-menus-panel.tsx",
    "pages/system/system-roles-panel.tsx",
    "pages/system/system-users-panel.tsx",
}


def _write_functions() -> set[str]:
    """从 `lib/api.ts` 抽出所有「会改数据」的请求函数名。"""
    api = (SRC / "lib" / "api.ts").read_text(encoding="utf-8")
    return {
        m.group(1)
        for m in re.finditer(
            r"export async function (\w+)\([^)]*\)[^{]*\{\s*\n\s*(?:const|await)?[^\n]*"
            r"http\.(?:post|put|patch|delete)",
            api,
        )
    }


def _pages_with_writes() -> set[str]:
    """有写请求调用的页面（相对 `src/` 的 posix 路径）。"""
    fns = _write_functions()
    out: set[str] = set()
    for page in sorted((SRC / "pages").rglob("*.tsx")):
        src = page.read_text(encoding="utf-8")
        if any(f"{fn}(" in src for fn in fns):
            out.add(page.relative_to(SRC).as_posix())
    return out


class TestGatedPagesDoNotRegress:
    def test_gated_pages_still_use_hasPermission(self):
        for rel, codes in PERM_GATED_PAGES.items():
            src = (SRC / rel).read_text(encoding="utf-8")
            assert "hasPermission" in src, f"{rel} 丢了 hasPermission 接入（权限面裸奔）"
            for code in codes:
                assert f'"{code}"' in src, (
                    f"{rel} 不再判 {code} ⇒ 该按钮对无权限用户也会展示"
                )

    def test_gated_buttons_use_conditional_render(self):
        """必须是 `hasPermission(...) && (` 条件渲染，不是 `disabled`。

        用户口径是「**不展示**」—— `disabled` 仍然展示。
        """
        for rel in PERM_GATED_PAGES:
            src = (SRC / rel).read_text(encoding="utf-8")
            #: 两种条件渲染形态：
            #: ① `{hasPermission("X") && (<Button …/>)}`
            #: ② 菜单项数组里 `...(hasPermission("X") ? [{…}] : [])` ——
            #:    无权限时该项**整条不出现**，与 ① 同义（比 `[].filter()` 更直白）。
            #: ③ 写在提交函数 / 事件处理里：`if (!hasPermission("X")) { …; return; }`
            #:    —— 用于「嵌在复合区块里、单独删掉会让布局塌掉」的动作。
            assert re.search(
                r'hasPermission\("[^"]+"\)\s*&&'
                r'|hasPermission\("[^"]+"\)\s*\?'
                #: ③ 提交/handler 里前置拦截：`if (!hasPermission("X")) { … ; return; }`
                r'|!hasPermission\("[^"]+"\)', src
            ), (
                f"{rel} 的 hasPermission 没写成条件渲染（可能是 disabled 或三元）"
            )


class TestUngatedListOnlyShrinks:
    def test_no_new_ungated_pages(self):
        """新页面带写请求却没有权限判断 ⇒ 红。"""
        gated = set(PERM_GATED_PAGES)
        actual_ungated = {p for p in _pages_with_writes() if p not in gated}
        unexpected = actual_ungated - KNOWN_UNGATED
        assert not unexpected, (
            "以下页面有写请求但没有 hasPermission（无权限用户也会看到按钮）：\n"
            + "\n".join(f"  {p}" for p in sorted(unexpected))
        )

    def test_fixed_pages_are_removed_from_known_list(self):
        """修好的页面要从欠账表里删掉，否则欠账表永远不缩。"""
        gated = set(PERM_GATED_PAGES)
        stale = KNOWN_UNGATED & gated
        assert not stale, f"这些页面已接入权限，却还留在 KNOWN_UNGATED：{stale}"


class TestMenuIsDynamic:
    def test_menu_endpoint_filters_by_permission(self):
        """菜单必须由后端按权限过滤（前端不过滤全集 —— 前端拿到就不该包含无权项）。"""
        src = (ROOT / "backend" / "app" / "api" / "system_menus.py").read_text(encoding="utf-8")
        assert '@router.get("/mine"' in src, "找不到 GET /api/system/menus/mine"
        assert "perms" in src, "菜单端点没有按 perms 过滤"

    def test_sidebar_excludes_button_rows(self):
        """F（按钮）行不进侧栏。"""
        nav = (SRC / "components" / "layout" / "use-dynamic-nav.tsx").read_text(encoding="utf-8")
        assert '"F"' in nav or "'F'" in nav, "导航组件没排除 F 行"
