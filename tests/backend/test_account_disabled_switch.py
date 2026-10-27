"""账号启用 / 停用开关。

头号不变量：`disabled` **只有用户操作能写** —— 任何定时任务 / 探活 / 登录刷新
都不许写它，否则用户设的停用会被下一次巡检冲掉（页面上只表现为「开关自己弹回去」）。

与 `status` 分开的原因：`status` 由巡检自动写（登录探活结论），
「用不用」是人工决定，混在一列必然互相覆盖。
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend" / "app"
SCHEDULER = BACKEND / "services" / "scheduler_service.py"
ACCOUNTS_API = BACKEND / "api" / "accounts.py"
MODEL = BACKEND / "models" / "platform_account.py"
ACCOUNT_SERVICE = BACKEND / "services" / "account_service.py"
ACCOUNTS_PAGE = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "src"
    / "pages"
    / "platforms"
    / "xhs"
    / "accounts-page.tsx"
)
API_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "api.ts"


def _code(path: Path) -> str:
    """剥注释（本仓注释里大量出现被守护的符号名，不剥就是假绿）。"""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"\{/\*.*?\*/\}", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    text = re.sub(r'^\s*""".*?"""', "", text, flags=re.S | re.M)
    text = re.sub(r"^\s*#.*$", "", text, flags=re.M)
    return text


class TestDisabledIsAUserOnlySwitch:
    """头号不变量：`disabled` 只有用户能写。"""

    def test_scheduler_never_writes_disabled(self) -> None:
        """定时任务链路**一处都不许**写 `disabled`（含 Cookie 巡检 / 商品笔记同步）。

        这是「不依靠定时任务」的机械判据：巡检只写 `status`。
        """
        src = _code(SCHEDULER)
        writes = re.findall(r"\.disabled\s*=(?!=)", src)
        assert not writes, f"定时任务链路上出现了写 disabled 的赋值：{writes}"

    def test_scheduler_does_not_filter_by_disabled(self) -> None:
        """定时任务**当前不按停用过滤** —— 这是 2026-10-27 主动放弃的范围，不是遗漏。

        ## 记录（推翻不静默）

        原口径：「包括定时任务，非启动的账号跳过」。曾落地为「调度器 + 5 个后台服务的
        账号查询各加 `disabled.is_(False)`」，随后用户决定**放弃该需求**并已全部回退，
        因此现在这条**反向**守着：

        * `disabled` 只有**用户开关 + 页面展示**两件事，不参与定时任务的派发筛选；
        * 若要重新做这件事，判据是「派发点的账号查询带 `disabled.is_(False)`」，
          而不是在 `_check_single_account` 这类单账号函数里就地跳过（那样请假 / 停用的
          语义会散落到各处）。

        ⚠️ 头号不变量仍然成立且更重要：**定时任务绝不写 `disabled`**（见上一条用例）——
        否则用户设的停用会被巡检冲掉。不筛 ≠ 可写。
        """
        src = _code(SCHEDULER)
        assert "PlatformAccount.disabled.is_(False)" not in src, (
            "调度器又按停用过滤了 —— 该需求已被用户放弃；要重做请先更新本用例与设计文档"
        )

    def test_login_refresh_does_not_touch_disabled(self) -> None:
        """登录刷新 / 探活（`accounts.py` 的 check 与 metrics）都不许改 `disabled`。"""
        src = _code(ACCOUNTS_API)
        #: 唯一允许写它的地方是切换端点自己。
        assert src.count(".disabled = ") <= 1, "accounts.py 里有多处写 disabled"
        assert "account.disabled = bool(payload.disabled)" in src, "切换端点没写 disabled"

    def test_toggle_endpoint_does_not_touch_status(self) -> None:
        """切换端点**不碰 `status`** —— 那是登录态的自动结论，混在一起就又回去了。"""
        src = _code(ACCOUNTS_API)
        start = src.index("def set_account_disabled(")
        end = src.index("@router.post", start)
        body = src[start:end]
        assert ".status =" not in body, "切换端点改了 status（会把登录态结论冲掉）"
        assert ".status_message =" not in body, "切换端点改了 status_message"


class TestSchemaAndSerialization:
    def test_model_has_disabled_column(self) -> None:
        src = _code(MODEL)
        assert "disabled: Mapped[bool]" in src, "模型没有 disabled 列"
        #: 必须非空 + 服务端默认 false（否则迁移加列时存量行是 NULL ⇒ 前端拿不到布尔）。
        assert "server_default=text(\"false\")" in src, "缺 server_default ⇒ 存量行会是 NULL"

    def test_serialize_exposes_disabled(self) -> None:
        src = _code(ACCOUNT_SERVICE)
        assert '"disabled": bool(account.disabled)' in src, "列表没下发 disabled，前端无从渲染"

    def test_response_model_declares_disabled(self) -> None:
        """`AccountItem` 必须声明 `disabled`，否则接口响应里**静默没有这个键**。

        ⚠️ 真机实测踩到：`serialize_account()` 明明给了 `disabled`，但
        `response_model=AccountListResponse` 按模型剔字段 ⇒ 响应里没这个键 ⇒
        前端拿到 `undefined` ⇒ 开关恒显示「正常」，**且不报任何错**。
        这类「后端有值、接口没有、前端不报错」的静默退化，只能靠响应模型声明兜住。
        """
        from backend.app.schemas.accounts import AccountItem

        assert "disabled" in AccountItem.model_fields, (
            "AccountItem 没声明 disabled ⇒ response_model 会把它静默剔掉"
        )
        #: 缺省必须是 `False`（旧响应 / 构造时没给 ⇒ 语义是「正常使用」，不是停用）。
        assert AccountItem.model_fields["disabled"].default is False, (
            "disabled 的缺省不是 False ⇒ 老数据/缺值会被当成停用"
        )


class TestFrontendSwitch:
    def test_status_column_is_not_rendered(self) -> None:
        """状态列**不上页面**（2026-10-27 用户口径「账号页的状态列没有意义展示不展示」）。

        ## 这条推翻了什么（不静默）

        曾把状态列做成用户可控的「启用 / 停用」开关。用户最终口径：

        *「账号页的状态列没有意义展示不展示，**数据库默认开启状态，为后续其它功能保留扩展能力**」*

        ⇒ 列去掉，但**数据库列与开关端点都保留**（`disabled` 默认 `false` = 正常，
        后续功能可直接用），因此下一条用例仍然守着「只有用户能写它」。
        """
        src = _code(ACCOUNTS_PAGE)
        assert 'dataIndex: "disabled"' not in src, "状态列又渲染了"
        assert 'title="状态"' not in src, "状态列的表头还在"
        #: 连带的无用面也必须清掉（留着就是死代码 + 误导下一个人）。
        assert "setAccountDisabled" not in src, "开关处理器没清干净"
        assert "togglingIds" not in src, "开关 loading 状态没清干净"

    def test_toggle_capability_is_kept_for_future_use(self) -> None:
        """**能力保留**：端点与请求函数都还在，只是页面上不展示。

        用户明确要「为后续其它功能保留扩展能力」—— 页面撤列 ≠ 删能力。
        删了端点就得重做一遍（含权限码、路由守卫、序列化），那是白扔。
        """
        api_src = _code(ACCOUNTS_API)
        assert "def set_account_disabled(" in api_src, "停用端点被误删"
        ts_src = _code(API_TS)
        assert "export async function setAccountDisabled" in ts_src, "请求函数被误删"

    def test_api_function_exists(self) -> None:
        src = _code(API_TS)
        assert "export async function setAccountDisabled" in src, "请求函数缺失"
        assert "`/accounts/${accountId}/disabled`" in src, "请求路径不对"
