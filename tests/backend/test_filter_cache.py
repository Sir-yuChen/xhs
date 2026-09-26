"""页面筛选条件的本地缓存（2026-09-26 用户口径「梳理所有商品列表，我的笔记，内容库页，
筛选条件和账号选择后放本地缓存，避免每次均需要重新选择」）。

两半，各管一件事：

1. **纯逻辑**（`tests/frontend/persisted_filters.cjs`，esbuild 打包**真实**
   `frontend/src/lib/filter-cache.ts`）：坏缓存（半截 JSON / 隐私模式 / 配额满）
   一律静默回默认且**顺手自清**、取值域校验（旧枚举 / 字符串数字 / `NaN` 不许
   流进筛选条件）、写入失败不冒泡、三页键清单不撞、页签集合完整。
2. **结构守卫**（读三个页面源码）：三页必须用 `usePersistedFilter` 接上缓存，
   **不许自己碰 `localStorage`**（DEF-044：同一语义不留两份实现），且被替换掉的
   `useState` 形态不许退回来（退回 = 那个筛选静默不再缓存，而不会有任何断言变红）。

设计口径见 `docs/02_设计文档/04_功能设计_Web运营端.md` §3.7。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: 页面 → 必须引用的缓存键（漏一个 = 那个筛选静默不缓存）。
#: 键字符串与 `FILTER_CACHE_KEYS` 逐字对应（改写键名时必须同时改这里 —— 那正是
#: 我们要挡的「改了实现但没人发现旧 key 还在用」）。
PAGE_CACHE_KEYS: dict[str, tuple[str, ...]] = {
    "frontend/src/pages/platforms/xhs/library-page.tsx": (
        "FILTER_CACHE_KEYS.library.keyword",
        "FILTER_CACHE_KEYS.library.topicId",
        "FILTER_CACHE_KEYS.library.publishStatus",
        "FILTER_CACHE_KEYS.library.accountId",
        "FILTER_CACHE_KEYS.library.noteType",
        "FILTER_CACHE_KEYS.library.itemId",
        "FILTER_CACHE_KEYS.library.hasAssets",
        "FILTER_CACHE_KEYS.library.hasComments",
    ),
    "frontend/src/pages/platforms/xhs/my-notes-page.tsx": (
        "FILTER_CACHE_KEYS.myNotes.accountId",
        "FILTER_CACHE_KEYS.myNotes.tab",
        "FILTER_CACHE_KEYS.myNotes.interactionKind",
        "FILTER_CACHE_KEYS.myNotes.noTrafficThresholds",
        "FILTER_CACHE_KEYS.myNotes.noTrafficQuery",
    ),
    "frontend/src/pages/platforms/xhs/shop-items-page.tsx": (
        "FILTER_CACHE_KEYS.shopItems.accountId",
        "FILTER_CACHE_KEYS.shopItems.form",
        "FILTER_CACHE_KEYS.shopItems.pageSize",
        "FILTER_CACHE_KEYS.shopItems.sort",
    ),
}

#: 页面 → 不许再出现的旧 state 声明（退回 `useState` = 那个筛选不再落缓存）。
LEGACY_STATE_SNIPPETS: dict[str, tuple[str, ...]] = {
    "frontend/src/pages/platforms/xhs/library-page.tsx": (
        "const [keywordFilter, setKeywordFilter] = useState",
        "const [publishStatusFilter, setPublishStatusFilter] = useState",
        "const [accountFilter, setAccountFilter] = useState",
        "const [noteTypeFilter, setNoteTypeFilter] = useState",
        "const [hasAssetsFilter, setHasAssetsFilter] = useState",
        "const [hasCommentsFilter, setHasCommentsFilter] = useState",
    ),
    "frontend/src/pages/platforms/xhs/my-notes-page.tsx": (
        "const [selectedAccountId, setSelectedAccountId] = useState",
        "const [activeTab, setActiveTab] = useState",
        "const [interactionKind, setInteractionKind] = useState",
    ),
    "frontend/src/pages/platforms/xhs/shop-items-page.tsx": (
        "const [selectedAccountId, setSelectedAccountId] = useState",
        "const [pageSize, setPageSize] = useState",
        "const [sort, setSort] = useState",
    ),
}


def _read(relative: str) -> str:
    path = ROOT / relative
    assert path.exists(), f"页面不存在（改名了？）：{relative}"
    return path.read_text(encoding="utf-8")


def test_filter_cache_logic_is_driven_for_real(frontend_runner):
    """真打包产物驱动的行为验证（30 条）。"""
    result = frontend_runner("persisted_filters.cjs")
    assert result.returncode == 0, (result.stdout + result.stderr)[-900:]
    assert "30 PASS / 0 FAIL" in result.stdout


@pytest.mark.parametrize(
    "mutate,expected",
    [
        ("stale", "A3b"),   # 坏 JSON 不清掉 ⇒ 永远自愈不了
        ("throw", "A6"),    # 写失败往外抛 ⇒ 缓存写失败打断页面行为
        ("enum", "B4b"),    # 枚举不校验 ⇒ 旧枚举值流进筛选条件
        ("fields", "B6"),   # 对象型筛选不校验 ⇒ 脏字段（字符串数字）进请求体
        ("draft", "C2d"),   # 草稿不校验状态 ⇒ 空状态发给后端（未知状态 400）
        ("dupkey", "D2"),   # 两页同 key ⇒ 互相覆盖对方的筛选
        ("tabs", "E1"),     # 页签集合缺项 ⇒ 缓存里那个页签被静默退回默认
    ],
)
def test_filter_cache_guards_are_load_bearing(frontend_runner, mutate, expected):
    """变异验证：每个守卫被改坏时脚本必须变红（否则它没在守任何东西）。

    断言带 `FAIL  ` **前缀**：只找 `A3b` 的话，全绿时那行 `PASS  A3b …` 同样命中，
    等于什么都没断言（DEF-058 的教训）。
    """
    result = frontend_runner("persisted_filters.cjs", {"PERSISTED_FILTERS_MUTATE": mutate})
    assert result.returncode != 0, f"{mutate} 变异必须让脚本失败：stdout={result.stdout}"
    assert f"FAIL  {expected}" in result.stdout, f"stdout={result.stdout}"


@pytest.mark.parametrize("relative", sorted(PAGE_CACHE_KEYS))
def test_pages_wire_every_filter_into_the_cache(relative: str):
    """三个页面的筛选条件 / 账号选择**逐项**接了缓存，且不许自己碰 localStorage。"""
    page = _read(relative)

    assert 'from "../../../lib/filter-cache"' in page, (
        "页面必须从 lib/filter-cache 取缓存能力（自己拼 localStorage = 第二份实现）"
    )
    assert "usePersistedFilter(" in page or "usePersistedFilter<" in page, (
        "筛选状态必须走 usePersistedFilter（与 useState 同形）"
    )
    assert "localStorage" not in page, (
        "页面不许直接碰 localStorage：容错 / 取值域校验 / 键命名都只允许在 lib 里有一份"
        "（DEF-044）"
    )

    missing = [key for key in PAGE_CACHE_KEYS[relative] if key not in page]
    assert not missing, f"{relative} 少了这些缓存键（筛选会静默不落缓存）：{missing}"


@pytest.mark.parametrize("relative", sorted(LEGACY_STATE_SNIPPETS))
def test_pages_do_not_fall_back_to_plain_usestate(relative: str):
    """被替换掉的 `useState` 形态不许退回来（退回 = 筛选静默不再缓存）。"""
    page = _read(relative)
    offenders = [snippet for snippet in LEGACY_STATE_SNIPPETS[relative] if snippet in page]
    assert not offenders, (
        f"{relative} 又出现了裸 useState 形态（缓存会失效）：{offenders}。"
        "要改回不缓存请先改本守卫与设计 04 §3.7，别只改页面。"
    )


def test_account_filters_self_heal_against_deleted_accounts():
    """账号类缓存必须能自愈：缓存里的账号可能已被删除。

    只有「候选账号**成功取到**」之后才允许校验（否则账号列表取失败时，空数组会把
    用户存的账号当成已删除而清掉）—— 三个页面各自都留了这道判据。
    """
    library = _read("frontend/src/pages/platforms/xhs/library-page.tsx")
    assert "filterAccountsLoaded" in library, "内容库缺少「候选账号已知」的判据"
    assert "filterAccounts.some(" in library, "内容库没校验缓存的账号是否还在候选里"

    my_notes = _read("frontend/src/pages/platforms/xhs/my-notes-page.tsx")
    assert "loaded.some((account) => account.id === current)" in my_notes, (
        "我的笔记没校验缓存的账号（会被删掉的账号卡住，用户只看到「没数据」）"
    )

    shop = _read("frontend/src/pages/platforms/xhs/shop-items-page.tsx")
    assert "selectable.some((account) => account.id === current)" in shop, (
        "商品列表没校验缓存的账号是否还在可选集合里"
    )
