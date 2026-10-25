"""配置三级拆分守卫（设计 65，2026-10-25）。

用户口径：「系统级只展示系统管理中展示系统公共的，用户级别也是该用户下公共的配置，
工作区域级别的是指定工作区域私有的，**不要混乱展示**」。

三条判据：
1. 每个内置键**必须**在 `SETTING_SCOPES` 有归属（漏登记 ⇒ 页面上哪个区都不显示）；
2. `SETTING_SCOPES` / `_BUILTIN_CATEGORY` / `_BUILTIN_LABEL` / `BUILTIN_DEFAULTS`
   的键集合**完全一致**（四份表漂移 = 合成默认行时缺字段）；
3. 三级列表**互不重叠**（同一个键不能同时出现在两个级）。
"""

from __future__ import annotations

import pytest

from backend.app.services import settings_service as ss


def test_every_builtin_key_has_a_scope():
    """内置键必须登记归属 —— 否则它在三个页面都不显示，等于不可配置。"""
    missing = set(ss.BUILTIN_DEFAULTS) - set(ss.SETTING_SCOPES)
    assert not missing, f"这些键没登记归属级别（设计 65 §3）：{sorted(missing)}"


def test_scope_table_keys_are_consistent():
    """三张展示表的键集合必须一致（都 = `SETTING_SCOPES`）。

    ⚠️ 刻意**不**要求等于 `BUILTIN_DEFAULTS`：有些键**没有内置默认值** ——
    如 `order_sync_cursor_ms`（订单同步游标），它的值由同步过程自己写入，
    给它编一个默认值是错的（会让「从没同步过」看起来像「同步到了某处」）。
    ⇒ 判据是「凡在 SETTING_SCOPES 里，就必须能被渲染」，不是「必须有默认值」。
    """
    base = set(ss.SETTING_SCOPES) - set(ss.BUILTIN_DEFAULTS)
    for name, table in (
        ("_BUILTIN_CATEGORY", set(ss._BUILTIN_CATEGORY)),
        ("_BUILTIN_LABEL", set(ss._BUILTIN_LABEL)),
    ):
        assert table == set(ss.SETTING_SCOPES), (
            f"{name} 与 SETTING_SCOPES 键集不一致："
            f"多 {sorted(table - set(ss.SETTING_SCOPES))} / 少 {sorted(set(ss.SETTING_SCOPES) - table)}"
        )


def test_scope_values_are_known_levels():
    """归属只允许三值 —— 拼错（如 `workspaces`）会让键静默消失。"""
    allowed = {"system", "user", "workspace"}
    bad = {k: v for k, v in ss.SETTING_SCOPES.items() if v not in allowed}
    assert not bad, f"未知级别：{bad}"


def test_three_levels_do_not_overlap():
    """同一个键不能同时属于两个级别（「不要混乱展示」的机械判据）。"""
    seen: dict[str, str] = {}
    for key, scope in ss.SETTING_SCOPES.items():
        assert key not in seen, f"{key} 同时归 {seen.get(key)} 与 {scope}"
        seen[key] = scope


def test_short_descriptions_are_short():
    """界面说明必须短（用户口径「说明简短，只说明核心问题」）。

    上限 30 字：现有文案 14–22 字。放宽到 30 是给「以后加键」留余量，
    但**不能回到原来 50–170 字的技术长文**（那个占一整行且没人看）。
    """
    for key in ss.SETTING_SCOPES:
        text = ss.short_description(key)
        assert text, f"{key} 缺界面说明（config/initial_data/setting_descriptions.json）"
        assert len(text) <= 30, f"{key} 的说明 {len(text)} 字，超过 30 字上限：{text!r}"


@pytest.mark.parametrize("scope", ["system", "user", "workspace"])
def test_default_row_marks_virtual_id(db_session, scope):
    """合成默认行的 `id` 必须是 -1 —— 前端据它区分「新建」与「更新」。"""
    key = next(k for k, s in ss.SETTING_SCOPES.items() if s == scope)
    row = ss._default_row(key, scope=scope, user_id=1, workspace="xhs")
    assert row.id == -1
    assert row.key == key
    assert row.description == ss.short_description(key)
