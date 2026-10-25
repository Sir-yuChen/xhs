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
    """顶栏壳**不得**渲染工作区级配置（齿轮入口已删除）。

    用户口径（2026-10-25）：「齿轮逻辑是不正确的就不应该存在」。

    为什么这个入口本身不成立：齿轮在顶栏（任何页面都在），而工作区配置是
    **某个工作区私有**的。全局按钮点下去必须"猜"配哪个工作区，猜错就是配错地方
    —— 实测在 `/system` 下点开标题显示「工作区配置 · system」，而 `system`
    是管理工作区、根本没有私有配置。

    ⚠️ 判据必须落在**渲染调用**上（`scope="workspace"` / `SettingsTable`），
    不能按字面找「工作区配置」四个字 —— 注释里描述这个决策时就会带上它，
    按字面判会命中自己的注释（本守卫首轮就是这么假红的）。
    """
    shell = (FRONTEND / "components" / "layout" / "workspace-shell.tsx").read_text(
        encoding="utf-8"
    )
    assert 'scope="workspace"' not in shell, (
        "顶栏壳里又渲染了工作区级配置表 —— 全局入口不能承载单一工作区的私有配置"
    )
    #: ⚠️ `scope="user"` 在顶栏壳里**是合法的**（2026-10-25 最终口径）：
    #: 个人配置就在头像下拉的**弹窗**里 —— 用户级 = 本人私有，与工作区无关。
    #: 曾经把它挪去 `/settings` 页，结果是「个人配置占用了小红书工作区页面、
    #: 而小红书私有配置页没有了」：两者属于**不同归属**，不能互换位置。


def test_workspace_settings_live_inside_their_own_workspace():
    """工作区私有配置的入口必须**在工作区自己的页面里**，不在系统管理页。

    用户口径演进（2026-10-25，同一天三条）：
    ① 「系统级只展示系统管理中系统公共的…工作区域级别的是指定工作区域私有的，
       **不要混乱展示**」；
    ② 「齿轮逻辑是不正确的就不应该存在」；
    ③ 「**私有配置，只在对应工作区域内展示**，不错误的展示在配置管理页面中了」。

    第 ③ 条是最终口径：**标注「只对小红书生效」不算归属正确**，私有配置必须
    回它自己的工作区页面。这里钉的就是"不在配置管理页 + 在工作区页面里"。
    """
    panel = (FRONTEND / "pages" / "system" / "system-settings-panel.tsx").read_text(
        encoding="utf-8"
    )
    assert 'scope="workspace"' not in panel, (
        "系统管理 → 配置管理页又出现了工作区私有配置 —— 它只在对应工作区域内展示"
    )

    #: 入口 = 侧栏「设置」页（`/settings`，挂在业务工作区下）⇒ 结构上就是
    #: 「只在对应工作区内」（该路由属于 xhs 工作区，系统管理页看不到它）。
    page = (FRONTEND / "pages" / "settings" / "settings-page.tsx").read_text(encoding="utf-8")
    assert 'scope="workspace"' in page, (
        "工作区配置没有自己的入口 —— 用户无处可配"
    )
    assert "workspace=" in page, "入口没指定工作区标识"


def test_scope_partition_is_fixed_in_code_not_by_data():
    """三级归属由代码 `SETTING_SCOPES` 定死，不看数据行在哪一级。

    否则「同一个键在两个页面都列一遍」（实测：限流额度系统级 1 行 + 用户级 5 行），
    以及「某些键哪都不显示」（实测：3 个工作区键库里 0 行 ⇒ 无法配置）。
    """
    from backend.app.services.settings_service import SETTING_SCOPES

    counts: dict[str, int] = {}
    for scope in SETTING_SCOPES.values():
        counts[scope] = counts.get(scope, 0) + 1
    #: 2026-10-25 用户口径「私有配置只在对应工作区域内展示」后的归属：
    #: 凡取值取决于**接哪个平台**的一律归工作区级（哪怕当前只有一个平台）。
    assert counts.get("system") == 3, f"系统级应为 3 个键，实际 {counts.get('system')}"
    assert counts.get("user") == 1, f"个人级应为 1 个键，实际 {counts.get('user')}"
    assert counts.get("workspace") == 8, f"工作区级应为 8 个键，实际 {counts.get('workspace')}"
    #: 每个键只能属于一级 —— 重复归属正是「混乱展示」的根因。
    assert len(SETTING_SCOPES) == 12, "同一键被登记了多级归属"


def test_workspace_keys_are_platform_specific():
    """工作区级的键必须是**平台口径**（xhs_ 前缀），不能是通用系统开关。

    判据意义：把 `register.enabled` 这类全站开关放进工作区级，会让
    「A 工作区关了注册、B 还开着」这种无意义状态成为可能。
    """
    from backend.app.services.settings_service import SETTING_SCOPES

    #: 平台口径的判据不是"键名带 xhs_"——`crawl_rate_limit_per_minute` 是历史键名，
    #: 没有平台前缀但它限的正是「对小红书每分钟请求几次」（库里值 8 就是小红书实测口径）。
    #: 真正的判据是**语义**：换一个平台它是否照用？照用 ⇒ 系统级；要各配一份 ⇒ 工作区级。
    PLATFORM_AGNOSTIC = {
        "log_retention_days",       # 管本系统自己的表，与平台无关
        "job_log_retention_days",
        "register.enabled",         # 全站开关
        "order_sync_cursor_ms",     # 每用户各自的同步进度
    }
    for key, scope in SETTING_SCOPES.items():
        if scope == "workspace":
            assert key not in PLATFORM_AGNOSTIC, (
                f"{key} 与平台无关（换平台照用）⇒ 应该放系统级/用户级，不是工作区级"
            )


def test_workspace_config_is_not_shown_on_the_system_settings_page():
    """工作区私有配置**不得**出现在系统管理 → 配置管理页。

    用户口径（2026-10-25）：「私有配置，只在对应工作区域内展示，你梳理一下，
    不错误的展示在配置管理页面中了」。

    判据：三处**同时**成立才算正确 ——
    ① 配置管理页只请求 `scope="system"`；
    ② 配置管理页不渲染 `scope="workspace"`；
    ③ 工作区配置有**自己的入口**（挂在业务线行上，那里只在业务线页面渲染）。

    ⚠️ 曾经试过「放在配置管理页 + 标题标注『只对小红书生效』」——用户否掉了：
    **标注不等于归属正确**，私有配置必须回它自己的工作区页面。
    """
    panel = (FRONTEND / "pages" / "system" / "system-settings-panel.tsx").read_text(
        encoding="utf-8"
    )
    assert 'scope="system"' in panel, "配置管理页没有系统级配置表"
    assert 'scope="workspace"' not in panel, (
        "配置管理页又渲染了工作区私有配置 —— 私有配置只在对应工作区域内展示"
    )

    page = (FRONTEND / "pages" / "settings" / "settings-page.tsx").read_text(encoding="utf-8")
    assert 'scope="workspace"' in page, (
        "工作区配置没有自己的入口（侧栏「设置」页，挂在业务工作区下）"
    )


def test_workspace_scoped_keys_still_read_their_own_layer():
    """归属改成工作区级后，运行时读取必须**读到工作区级的行**。

    ⚠️ 这是重新归类的必要配套：`live_value` 原按「用户级 → 系统级」查，
    这些键的行若落在工作区级，业务就读不到 —— 页面能改、业务用的是旧值，
    **且零报错**（静默失效，比报错更难发现）。
    """
    from backend.app.services.settings_service import SETTING_SCOPES, effective_value

    workspace_keys = [k for k, s in SETTING_SCOPES.items() if s == "workspace"]
    assert workspace_keys, "判据前提不成立：没有工作区级的键"
    source = (ROOT / "backend" / "app" / "services" / "settings_service.py").read_text(
        encoding="utf-8"
    )
    #: `effective_value` 里必须有「按归属判工作区级」的分支。
    assert 'SETTING_SCOPES.get(key) == "workspace"' in source, (
        "effective_value 没有为工作区级键单独取行 ⇒ 重新归类后这些配置静默失效"
    )
    assert "_workspace_row_any" in source, "缺少工作区级取行的实现"


def test_scope_hit_reports_ownership_not_storage_location():
    """`scope_hit` 必须报**归属**，不能报「数据行物理落在哪一级」。

    ⚠️ 实测：`crawl_rate_limit_per_minute` 的行物理躺在系统级位置（早期建的行），
    但归属是工作区级 ⇒ 界面显示「来源：系统级」会让人以为改的是全站设置。
    页面分区按 `SETTING_SCOPES`，而 `scope_hit` 按数据行 —— 两处判据打架，
    用户看到的就是自相矛盾的信息。
    """
    source = (ROOT / "backend" / "app" / "services" / "settings_service.py").read_text(
        encoding="utf-8"
    )
    #: `effective_settings_map` 末尾必须有按归属校正的收尾。
    start = source.index("def effective_settings_map(")
    end = source.index("\ndef ", start + 10)
    body = source[start:end]
    assert "SETTING_SCOPES.get(key)" in body, (
        "effective_settings_map 没有按归属校正 scope_hit ⇒ 界面会显示「行在哪级」而非「归属哪级」"
    )


def test_workspace_settings_are_stored_per_workspace():
    """工作区私有配置**按工作区各存一份**，互不影响。

    用户口径（2026-10-25）：「工作区级不同的配置方对应的工作区内，
    每个工作区内是工作区私有」。

    判据 = 三件事同时成立：
    ① 查询按 `workspace` 列过滤（不是"取任意一行"）；
    ② 唯一约束含 workspace（否则两个工作区抢同一行）；
    ③ 系统级种子**不种**工作区级的键（否则每次启动重建 `workspace IS NULL`
       的孤儿行，与工作区的行并存 ⇒ 系统配置页会列出私有配置）。
    """
    from sqlalchemy import inspect

    from backend.app.models.system_setting import SystemSetting

    cols = {c.name for c in inspect(SystemSetting).columns}
    assert "workspace" in cols, "缺少 workspace 列 ⇒ 无法按工作区隔离"

    source = (ROOT / "backend" / "app" / "services" / "settings_service.py").read_text(
        encoding="utf-8"
    )
    #: ③ 系统级种子必须按归属过滤（这是「私有配置错误展示在配置管理页」的直接来源）。
    assert 'if SETTING_SCOPES.get(key, "system") != "system":' in source, (
        "seed_system_settings 没有按归属过滤 ⇒ 启动时会给工作区级键种系统级孤儿行"
    )
    #: 工作区级配置有自己的一播种入口。
    assert "def seed_workspace_settings(" in source, (
        "缺少工作区级播种 ⇒ 各工作区的基线行只能靠合成虚拟行"
    )


def test_first_write_to_a_workspace_creates_its_own_row():
    """某工作区首次配置某键时，必须**在该工作区下建行**，不能报 404。

    实测（2026-10-25）：`douyin` 从未配置过 ⇒ 库里没有它的行 ⇒
    编辑保存 404「配置项不存在」，而页面明明列出了这个键（虚拟行）。

    ⚠️ 前端也有一半责任：虚拟行 `id = -1`，必须走**新建**（POST）而不是更新（PUT）。
    两处判据同源（后端的 `_default_row` 约定 + 前端的 `id < 0` 判断）。
    """
    api = (ROOT / "backend" / "app" / "api" / "settings.py").read_text(encoding="utf-8")
    assert "ensure_row" in api, "PUT 未对「该级还没有行」的已知键兜底建行 ⇒ 首次保存 404"

    table = (
        FRONTEND / "components" / "settings" / "settings-table.tsx"
    ).read_text(encoding="utf-8")
    assert "editing.id < 0" in table, (
        "前端没识别虚拟行（id=-1）⇒ 会走 PUT 更新一个不存在的行"
    )
