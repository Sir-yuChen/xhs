"""业务线判据的守卫（设计 32 §4）。

设计原则同 `test_m1_governance.py`：**每条断言锁定「修复后不允许回退」的行为**。

| 用例 | 规则 |
|---|---|
| :func:`test_business_lines_are_capability_derived_not_type_derived` | 双线账号必须两条都占（判据锚在能力，不在类型） |
| :func:`test_business_lines_empty_when_no_channel_has_a_token` | 两条通道都没令牌 ⇒ **空集**（不兜底、不假设） |
| :func:`test_soft_deleted_account_belongs_to_no_business_line` | 软删账号不属于任何业务线（DEF-055 同型） |
| :func:`test_require_business_line_rejects_with_readable_reason` | 不具备资格时给可读原因，不是 500 |
| :func:`test_business_line_labels_and_channels_cover_the_two_real_lines` | 展示名 / 门禁通道唯一实现、覆盖两条真线 |
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.core import business_lines as bl


@pytest.fixture()
def stub_channels(monkeypatch):
    """把两条通道的令牌判据换成可控桩。

    ⚠️ 刻意**打桩在能力函数层**（`has_shop_session` / `has_creator_session`），
    而不是在 `latest_cookies_for_channel` 层：前者是两个业务线判据的**接口**，
    后者是数据访问细节。桩得太深，`business_lines_of` 里「先判哪个」的实现细节
    就会被测进去，未来换实现时会假红。
    """
    state = {"ark": False, "creator": False}

    def _fake_shop(db, account_id):
        return state["ark"]

    def _fake_creator(db, account_id):
        return state["creator"]

    monkeypatch.setattr(bl, "has_shop_session", _fake_shop)
    monkeypatch.setattr(bl, "has_creator_session", _fake_creator)
    return state


def _account(account_id=1, deleted_at=None):
    return SimpleNamespace(id=account_id, deleted_at=deleted_at)


def test_business_lines_are_capability_derived_not_type_derived(stub_channels):
    """**双线账号**（账号 8「观隅好物的店」的形态）必须两条都占。

    这是本套用例的**承重断言**：判据若改成按 `sub_type` 单选取值，
    双线账号必然只落一条 ⇒ 必红。

    ⚠️ 刻意不传 `sub_type` 进夹具 —— 判据里根本没有它的位置，传了反而
    会诱导后来者从这里开始做类型分支。
    """
    account = _account(8)

    # 只有 ark（账号 9 形态）
    stub_channels["ark"], stub_channels["creator"] = True, False
    assert bl.business_lines_of(None, account) == frozenset({bl.BUSINESS_LINE_SHOP})

    # 只有 creator（账号 11 形态）
    stub_channels["ark"], stub_channels["creator"] = False, True
    assert bl.business_lines_of(None, account) == frozenset({bl.BUSINESS_LINE_CONTENT})

    # 两者都有（账号 8 形态）—— 两条线都占
    stub_channels["ark"], stub_channels["creator"] = True, True
    assert bl.business_lines_of(None, account) == frozenset(
        {bl.BUSINESS_LINE_SHOP, bl.BUSINESS_LINE_CONTENT}
    )


def test_business_lines_empty_when_no_channel_has_a_token(stub_channels):
    """两条通道都没有令牌 ⇒ **空集**。

    不许兜底成 `{content}`（那是「猜」）：一个既没 ark 也没 creator 令牌的账号
    是**两条线都不属于**，业务上要引导用户去补登，而不是替它选一条。
    """
    stub_channels["ark"], stub_channels["creator"] = False, False
    assert bl.business_lines_of(None, _account(7)) == frozenset()


def test_soft_deleted_account_belongs_to_no_business_line(stub_channels):
    """软删账号不属于任何业务线（DEF-055 同型：删除的数据不得再被使用）。"""
    from backend.app.core.time import shanghai_now

    stub_channels["ark"], stub_channels["creator"] = True, True
    account = _account(9, deleted_at=shanghai_now())
    assert bl.business_lines_of(None, account) == frozenset()


def test_business_lines_of_accepts_an_id_only(stub_channels):
    """只有 account_id 时也能判（页面按 id 传参的场景）。"""
    stub_channels["ark"], stub_channels["creator"] = True, False
    assert bl.business_lines_of(None, None, account_id=9) == frozenset({bl.BUSINESS_LINE_SHOP})

    with pytest.raises(ValueError):
        bl.business_lines_of(None, None)


def test_require_business_line_rejects_with_readable_reason(stub_channels):
    """不具备资格 ⇒ 可读业务错误（400 级），**不是** 500。

    原因里必须点名**缺哪个通道** —— 不然用户不知道该去补登什么。
    """
    stub_channels["ark"], stub_channels["creator"] = False, True

    # 缺店铺线 ⇒ 拒绝，且原因点名 ark
    with pytest.raises(bl.BusinessLineUnavailableError) as excinfo:
        bl.require_business_line(None, 11, bl.BUSINESS_LINE_SHOP)
    assert excinfo.value.code == "BUSINESS_LINE_UNAVAILABLE"
    assert "ark" in str(excinfo.value)

    # 属于内容线 ⇒ 放行（不抛）
    bl.require_business_line(None, 11, bl.BUSINESS_LINE_CONTENT)

    # 未知业务线 ⇒ 另一种可读错误
    with pytest.raises(bl.BusinessLineUnavailableError) as excinfo:
        bl.require_business_line(None, 11, "whatever")
    assert excinfo.value.code == "UNKNOWN_BUSINESS_LINE"


def test_business_line_labels_and_channels_cover_the_two_real_lines():
    """展示名与门禁通道：唯一实现、覆盖两条真线、不含 `shared`。

    ⚠️ `shared`（「平台」）**不该有门禁通道** —— 它不属于任何一条线，
    给它配一个通道等于承认「平台线也要令牌」，那是错的。
    """
    assert set(bl.BUSINESS_LINE_LABELS) == set(bl.BUSINESS_LINES)
    assert set(bl.BUSINESS_LINE_CHANNELS) == {
        bl.BUSINESS_LINE_SHOP,
        bl.BUSINESS_LINE_CONTENT,
    }
    assert bl.BUSINESS_LINE_LABELS[bl.BUSINESS_LINE_SHOP] == "店铺线"
    assert bl.BUSINESS_LINE_LABELS[bl.BUSINESS_LINE_CONTENT] == "内容线"
    assert bl.BUSINESS_LINE_LABELS[bl.BUSINESS_LINE_SHARED] == "平台"
    # 通道名必须与 account_service 的三个常量逐个相等（不另造名字）。
    from backend.app.services.account_service import CHANNEL_ARK, CHANNEL_CREATOR

    assert bl.BUSINESS_LINE_CHANNELS[bl.BUSINESS_LINE_SHOP] == CHANNEL_ARK
    assert bl.BUSINESS_LINE_CHANNELS[bl.BUSINESS_LINE_CONTENT] == CHANNEL_CREATOR
