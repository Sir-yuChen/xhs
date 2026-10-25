"""配置项三级归属的**准入守卫**（设计 68）。

用户口径（2026-10-25 验收时）：「记录规则，后续配置相关的参数按照三级区分」。

前面几条守卫钉的是「现在分得对不对」；本文件钉的是 **「以后新加的也必须分」** ——
新增一个配置键（读点 / 内置默认 / 初始数据 / 分组 / 文案）时，
任何一处漏登记都会在 CI 里红，而不是等界面上出现「某键哪都不显示」才发现。

判据见 `docs/02_设计文档/68_设计_配置项三级归属规则.md` §1：
**换一个平台要不要另配一份** → 工作区级；照用 → 系统级；每人各一份 → 个人级。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _settings_service_text() -> str:
    return (
        ROOT / "backend" / "app" / "services" / "settings_service.py"
    ).read_text(encoding="utf-8")


def test_every_runtime_key_declares_a_scope():
    """每个有运行时读点的键（`LIVE_KEYS`）都必须登记归属。

    漏登记 ⇒ 页面不知道该在哪个页面展示它 ⇒ **三处都不显示**，等于不可配置
    （实测：三个工作区级键曾因此完全不可配）。
    """
    from backend.app.services.settings_service import LIVE_KEYS, SETTING_SCOPES

    missing = sorted(set(LIVE_KEYS) - set(SETTING_SCOPES))
    assert not missing, (
        f"这些键有运行时读点但没登记归属：{missing} —— "
        "未登记的键不知道该在哪个页面出现，三处都不显示等于不可配置"
    )


def test_every_builtin_default_declares_a_scope():
    """每个内置默认值（`BUILTIN_DEFAULTS`）都必须登记归属。

    播种与「缺行合成默认行」都以它为来源；没登记的键会被
    `seed_system_settings` 归进系统级（默认分支），也就是又出现"私有配置错位"。
    """
    from backend.app.services.settings_service import BUILTIN_DEFAULTS, SETTING_SCOPES

    missing = sorted(set(BUILTIN_DEFAULTS) - set(SETTING_SCOPES))
    assert not missing, f"这些键有内置默认值但没登记归属：{missing}"


def test_every_scoped_key_has_a_category_and_label():
    """每个已登记归属的键都要有分组与中文名（否则界面显示裸代码键）。

    用户口径：「分组支持下拉加新增」「配置键要有中文名」——
    裸 `xhs_ocr_parallelism` 出现在界面上等于没有中文名。
    """
    from backend.app.services.settings_service import (
        SETTING_SCOPES,
        _BUILTIN_CATEGORY,
        _BUILTIN_LABEL,
    )

    no_category = sorted(set(SETTING_SCOPES) - set(_BUILTIN_CATEGORY))
    no_label = sorted(set(SETTING_SCOPES) - set(_BUILTIN_LABEL))
    assert not no_category, f"缺分组：{no_category}"
    assert not no_label, f"缺中文名：{no_label}"


def test_every_scoped_key_has_a_short_description():
    """每个已登记归属的键都要有**短说明**（≤30 字）。

    用户口径：「说明简短且只说明核心问题，不必要的说明全部移除」
    「类似文案太多啰嗦，应简要明了或收归到说明图标中」。
    """
    from backend.app.services.settings_service import SETTING_SCOPES, short_description

    missing = sorted(k for k in SETTING_SCOPES if not short_description(k))
    assert not missing, f"缺界面短说明：{missing}"
    too_long = {
        k: len(short_description(k))
        for k in SETTING_SCOPES
        if len(short_description(k)) > 30
    }
    assert not too_long, f"说明超过 30 字（界面会换行占地方）：{too_long}"


def test_scope_values_are_exactly_the_three_levels():
    """归属只能是 system / user / workspace 三值之一。

    拼错（如 `worKspace`）不会报错，只会让那个键**静默消失**（没有任何页面认它）。
    """
    from backend.app.services.settings_service import SETTING_SCOPES

    bad = {k: v for k, v in SETTING_SCOPES.items() if v not in {"system", "user", "workspace"}}
    assert not bad, f"归属值非法：{bad}"


def test_first_level_rule_is_documented_and_referenced():
    """判据文档必须存在且在 AGENTS.md 里可达（后续开发能找到规则）。"""
    doc = ROOT / "docs" / "02_设计文档" / "68_设计_配置项三级归属规则.md"
    assert doc.exists(), "设计 68（配置项三级归属规则）不存在 —— 新配置项没有判据来源"
    text = doc.read_text(encoding="utf-8")
    #: 判据必须是可执行的一句话，不是模糊描述。
    assert "换一个平台" in text, "文档里没有那条核心判据（换平台要不要另配一份）"
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "68_设计_配置项三级归属规则" in agents, (
        "AGENTS.md §6（常见任务→真相源）没有指向设计 68 —— 后续开发找不到这条规则"
    )


def test_adding_a_key_requires_all_five_registrations():
    """五处登记必须一致（这就是「新增配置项」的清单）。

    1. `SETTING_SCOPES`（归属）  2. `BUILTIN_DEFAULTS`（默认值）
    3. `LIVE_KEYS`（有读点才加）  4. `_BUILTIN_CATEGORY`（分组）  5. `_BUILTIN_LABEL`（中文名）

    ⚠️ 本守卫的价值在**失败信息**：哪一处漏了会直接点名，而不是让人逐个文件找。
    """
    from backend.app.services.settings_service import (
        BUILTIN_DEFAULTS,
        LIVE_KEYS,
        SETTING_SCOPES,
        _BUILTIN_CATEGORY,
        _BUILTIN_LABEL,
    )

    #: ⚠️ `BUILTIN_DEFAULTS` / `LIVE_KEYS` **只有部分键有**：
    #: 「运行期写入的状态型配置」（如 `order_sync_cursor_ms` —— 订单同步进度）
    #: 没有产品默认值，也往往没有"读取点"（它是被写入的游标，不是开关）。
    #: 强求两者会逼人给状态型键编一个假默认值 —— 那比缺登记更糟。
    #:
    #: 判据分两组：
    #: * **开关型**（有 `LIVE_KEYS` 读点）⇒ 五处全要求；
    #: * **状态型**（无读点）⇒ 只要归属 + 分组 + 中文名，且**不许**有假默认值。
    scopes = set(SETTING_SCOPES)
    gaps: list[str] = []
    for key in sorted(scopes):
        missing = []
        if key not in _BUILTIN_CATEGORY:
            missing.append("_BUILTIN_CATEGORY(分组)")
        if key not in _BUILTIN_LABEL:
            missing.append("_BUILTIN_LABEL(中文名)")
        if key in LIVE_KEYS:
            #: 开关型：必须同时有产品默认值（否则"连配置都没有"时无基线）。
            if key not in BUILTIN_DEFAULTS:
                missing.append("BUILTIN_DEFAULTS(默认值)")
        else:
            #: 状态型：不许编默认值（造假基线比没有更坑）。
            if key in BUILTIN_DEFAULTS:
                missing.append("多余：状态型键不该有 BUILTIN_DEFAULTS 默认值")
        if missing:
            gaps.append(f"{key} → 缺 {', '.join(missing)}")
    assert not gaps, "配置项登记不完整：\n  " + "\n  ".join(gaps)


def test_workspace_scoped_keys_are_never_seeded_at_system_level():
    """工作区级的键**不得**出现在系统级播种里（代码层判据，不需要数据库）。

    ⚠️ 这是"私有配置错位"的直接来源：清掉孤儿行后重启又回来。
    """
    text = _settings_service_text()
    start = text.index("def seed_system_settings(")
    end = text.index("\ndef ", start + 10)
    body = text[start:end]
    assert 'if SETTING_SCOPES.get(key, "system") != "system":' in body, (
        "seed_system_settings 没有按归属过滤 ⇒ 启动时给工作区级键种系统级孤儿行"
    )
