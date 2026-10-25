"""配置三级归属的界面守卫（设计 66/67，2026-10-25 用户口径）。

用户两条连续口径：
1. 「工作区配置 · xhs 是什么属于哪个级别」→ 追问归属
2. 「工作区配置 · system 应属于什么，它不应该再顶部图标展示」
3. 「齿轮逻辑是不正确的就不应该存在」

第 3 条是结论：**顶栏齿轮这个入口整个不该有**。守卫钉的就是这个结论，
以及它背后的判据（全局入口不能承载某个具体工作区的私有配置）。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend" / "src"


def test_no_global_gear_button_for_workspace_config():
    """顶栏**不得**有「工作区配置」齿轮入口。

    为什么这不是"少一个按钮"的小事：齿轮是**全局**入口（任何页面都在），
    而工作区配置是**某个工作区私有**的。全局按钮点下去必须"猜"配哪个工作区，
    猜错就是配错地方 —— 实测在 `/system` 下点开标题显示「工作区配置 · system」，
    而 `system` 是管理工作区、根本没有私有配置。入口本身表达不了归属 ⇒ 删掉。
    """
    shell = (FRONTEND / "components" / "layout" / "workspace-shell.tsx").read_text(
        encoding="utf-8"
    )
    assert "工作区配置" not in shell, (
        "顶栏壳里又出现了「工作区配置」入口 —— 全局入口不能承载单一工作区的私有配置"
    )
    #: 也不许换个名字回来（判据落在「在壳里渲染 SettingsTable(scope=workspace)」）。
    assert 'scope="workspace"' not in shell, (
        "顶栏壳里又渲染了工作区级配置表 —— 它只能出现在系统管理页"
    )


def test_workspace_settings_live_in_the_system_panel():
    """工作区级配置必须在系统管理页，且**写明是哪个工作区**。

    归属要看得见：用户要看得出这是「小红书配置」而不是「某个配置」。
    """
    panel = (FRONTEND / "pages" / "system" / "system-settings-panel.tsx").read_text(
        encoding="utf-8"
    )
    assert 'scope="workspace"' in panel, "工作区级配置没在系统管理页展示（那它哪都看不到了）"
    assert 'workspace={ws.key}' in panel or "workspace=" in panel, "没传 workspace 标识"
    assert "小红书" in panel, "没写明是哪个工作区（归属不可见 = 又回到混乱展示）"


def test_scope_partition_is_fixed_in_code_not_by_data():
    """三级归属由代码 `SETTING_SCOPES` 定死，不看数据行在哪一级。

    否则「同一个键在两个页面都列一遍」（实测：限流额度系统级 1 行 + 用户级 5 行），
    以及「某些键哪都不显示」（实测：3 个工作区键库里 0 行 ⇒ 无法配置）。
    """
    from backend.app.services.settings_service import SETTING_SCOPES

    counts: dict[str, int] = {}
    for scope in SETTING_SCOPES.values():
        counts[scope] = counts.get(scope, 0) + 1
    assert counts.get("system") == 8, f"系统级应为 8 个键，实际 {counts.get('system')}"
    assert counts.get("user") == 1, f"个人级应为 1 个键，实际 {counts.get('user')}"
    assert counts.get("workspace") == 3, f"工作区级应为 3 个键，实际 {counts.get('workspace')}"
    #: 每个键只能属于一级 —— 重复归属正是「混乱展示」的根因。
    assert len(SETTING_SCOPES) == 12, "同一键被登记了多级归属"


def test_workspace_keys_are_platform_specific():
    """工作区级的键必须是**平台口径**（xhs_ 前缀），不能是通用系统开关。

    判据意义：把 `register.enabled` 这类全站开关放进工作区级，会让
    「A 工作区关了注册、B 还开着」这种无意义状态成为可能。
    """
    from backend.app.services.settings_service import SETTING_SCOPES

    for key, scope in SETTING_SCOPES.items():
        if scope == "workspace":
            assert key.startswith("xhs_"), (
                f"{key} 在工作区级但不是平台口径 —— 通用开关应该放系统级"
            )
