"""同步链路的节流与细节（设计 13 §21.3 / §35.7；TC-LIB-45）。

四组纯函数 / 逻辑断言，对应 TC-LIB-45 的四条契约：

1. **页间停顿「拉长 + 抖动」**（§35.7）：实际停顿 = 基础值 × `uniform(0.8, 1.6)`，
   且 `cap` 硬顶优先、基础值 0 免等待；笔记 / 商品两条同步链路都委托
   `services/sync_pacing.py` 唯一实现（DEF-044：同一语义不留两份）；
2. **DS 签名材料的有限重试**（TC-LIB-45 ②）：前两次失败第三次成功 ⇒ 返回真值；
   全失败**有界**（=3 次）且抛 `ds 接口首次 fetch 失败`；有旧值 ⇒ 降级复用；
3. **详情失败文案人话化**（③）：curl 28 ⇒ `DETAIL_TIMEOUT` + 人话（不含 curl /
   毫秒数）；连接类 ⇒ `DETAIL_NETWORK_ERROR`；未知异常 ⇒ 截断 ≤90 字仍带真原因；
4. **平台 user_id 免 bootstrap**（④）：`XhsPcApiAdapter(cookies, user_id=…)` 把
   user_id 注入 Auth；`_build_pc_adapter` 传账号行 `external_user_id`，并兼容
   只吃一个位置参数的老替身（加固，不是新门禁）。

2026-09-25 DEF-069 追加第五组：**限流额度配置化** —— `CrawlRateLimiter` 的额度
从「构造参数 > 运行时配置 > settings（`crawl_rate_limit_per_minute`）> 5」解析
（原兜底读 `scheduler_interval_seconds`，语义混乱：改限流要改调度周期）。
⚠️ 2026-09-27 起 settings 层的**默认值是 8**（由 60 下调，设计 13 §38.8）；下面的
「60」只出现在**替身**里 —— 那测的是层级优先级，不是产品默认值。

⚠️ **本文件头部曾被误毁并重建**（2026-09-25）：一次 `cat >>` heredoc 覆盖了
模块 docstring / imports 与两条用例（正是 `REF_易错与运行坑.md` §B 末条记的
「 heredoc 毁文件」老坑，第四次踩到）。重建依据 = TC-LIB-45 的断言契约 + 源码
事实 + 幸存的后 14 条用例；两条丢失用例（抖动带宽 / 下限）按
`sync_pacing.jittered_gap` 的实现契约重写。**后写文件一律 Read + Edit / Write，
禁用 `cat >>`。**
"""

from __future__ import annotations

import pytest

from backend.app.services import sync_pacing



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract

# --------------------------------------------------------------------------- #
# ⑤ 限流额度配置化（DEF-069 提速，2026-09-25）
# --------------------------------------------------------------------------- #
def test_rate_limit_quota_prefers_explicit_argument_over_settings(monkeypatch):
    """额度优先级：构造参数 > settings > 5（显式意图不许被产品配置覆盖）。"""
    from backend.app.services.rate_limiter import CrawlRateLimiter

    class _Settings:
        crawl_rate_limit_per_minute = 60

    monkeypatch.setattr("backend.app.services.rate_limiter.get_settings", lambda: _Settings())
    assert CrawlRateLimiter(max_per_minute=3).max_per_minute == 3, "显式传入优先"
    assert CrawlRateLimiter().max_per_minute == 60, "不传则读 settings"

    class _Empty:
        crawl_rate_limit_per_minute = 0

    monkeypatch.setattr("backend.app.services.rate_limiter.get_settings", lambda: _Empty())
    assert CrawlRateLimiter().max_per_minute == 5, "settings 缺省兜底 5"


def test_default_quota_comes_from_settings(monkeypatch):
    """生产默认额度 = `settings.crawl_rate_limit_per_minute`（2026-09-27 起 **8**，设计 13 §38.8）。"""
    from backend.app.core.config import get_settings
    from backend.app.services.rate_limiter import CrawlRateLimiter

    assert getattr(get_settings(), "crawl_rate_limit_per_minute", 0) == 8
    assert CrawlRateLimiter().max_per_minute == 8


# --------------------------------------------------------------------------- #
# ① 页间停顿：拉长 + 随机抖动（§35.7）
# --------------------------------------------------------------------------- #
def test_jittered_gap_spreads_over_the_configured_band():
    """200 次采样必须落在 `[0.8, 1.6] × 基础值` 且**各不相同**（等距 = 机器人节奏）。"""
    base = 5.0
    samples = [sync_pacing.jittered_gap(base, cap=60.0) for _ in range(200)]
    assert min(samples) >= base * sync_pacing.JITTER_LOW - 1e-9
    assert max(samples) <= base * sync_pacing.JITTER_HIGH + 1e-9
    assert len(set(samples)) > 100, "抖动必须真的随机（同值过多 = 没在抖）"


def test_jittered_gap_never_falls_below_the_floor():
    """抖动只拉长不缩短下限（系数下限 0.8：最坏也只快 20%，不退化成密集请求）。"""
    for _ in range(100):
        gap = sync_pacing.jittered_gap(5.0, cap=60.0)
        assert gap >= 5.0 * sync_pacing.JITTER_LOW - 1e-9


def test_jittered_gap_respects_cap():
    """硬顶优先于抖动放大（`MAX_PAGE_GAP_SECONDS` 的语义不能破）。"""
    for _ in range(50):
        assert sync_pacing.jittered_gap(50.0, cap=60.0) <= 60.0


def test_jittered_gap_zero_stays_zero():
    """基础值置 0 ⇒ 不抖出正数（测试 monkeypatch 成 0 时必须真的免等待）。"""
    assert sync_pacing.jittered_gap(0.0, cap=60.0) == 0.0
    assert sync_pacing.jittered_gap(-3.0, cap=60.0) == 0.0


def test_sleep_between_pages_returns_the_gap_it_slept(monkeypatch):
    """返回值 = 实际停顿秒数（调用方/测试靠它判断，不许悄悄返回 0）。"""
    slept: list[float] = []
    monkeypatch.setattr(sync_pacing.time, "sleep", lambda seconds: slept.append(seconds))
    gap = sync_pacing.sleep_between_pages(5.0, cap=60.0)
    assert slept == [gap] and gap > 0


def test_both_sync_services_delegate_to_the_single_pacing_implementation():
    """两份同步链路都不许再自己算停顿（同一语义不留两份实现，DEF-044）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for rel in (
        "backend/app/services/note_sync_service.py",
        "backend/app/services/shop_goods_sync_service.py",
    ):
        source = (root / rel).read_text(encoding="utf-8")
        assert "sync_pacing import sleep_between_pages" in source, f"{rel} 未复用唯一实现"
        assert "time.sleep(gap)" not in source, f"{rel} 还留着自己那份 sleep 实现"


# --------------------------------------------------------------------------- #
# ② DS 签名材料 fetch 的有限重试
# --------------------------------------------------------------------------- #
class _FlakyFetcher:
    """按预设序列抛/返回的假 fetch：`["boom", "boom", "123"]` = 前两次失败。"""

    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def __call__(self, **_kwargs):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if outcome == "boom":
            raise RuntimeError("Failed to perform, curl: (28) Operation timed out")
        return outcome


def test_dsl_retries_until_fetch_succeeds(monkeypatch):
    """前两次抖动失败、第三次成功 ⇒ 返回真值（不再把整篇同步判死）。"""
    from xhs_utils.xhs_pc.dsl import DsFetcher

    monkeypatch.setattr("xhs_utils.xhs_pc.dsl._FETCH_RETRY_BACKOFF", 0.0)
    fetcher = DsFetcher()
    fake = _FlakyFetcher(["boom", "boom", "1783929625569"])
    monkeypatch.setattr(fetcher, "_fetch", fake)

    assert fetcher.get() == "1783929625569"
    assert fake.calls == 3, "重试次数应为 3（首次 + 2 次重试）"


def test_dsl_gives_up_after_bounded_attempts(monkeypatch):
    """全失败且无缓存 ⇒ 抛出，但尝试次数**有界**（不许无限重试）。"""
    from xhs_utils.xhs_pc.dsl import DsFetcher, _FETCH_ATTEMPTS

    monkeypatch.setattr("xhs_utils.xhs_pc.dsl._FETCH_RETRY_BACKOFF", 0.0)
    fetcher = DsFetcher()
    fake = _FlakyFetcher(["boom"])
    monkeypatch.setattr(fetcher, "_fetch", fake)

    with pytest.raises(RuntimeError) as error:
        fetcher.get()
    assert "ds 接口首次 fetch 失败" in str(error.value)
    assert fake.calls == _FETCH_ATTEMPTS == 3


def test_dsl_falls_back_to_cached_value_when_refresh_fails(monkeypatch):
    """有旧值 ⇒ 刷新失败也继续用旧值（既有降级口径不能因重试改动而丢）。"""
    from xhs_utils.xhs_pc.dsl import DsFetcher

    monkeypatch.setattr("xhs_utils.xhs_pc.dsl._FETCH_RETRY_BACKOFF", 0.0)
    fetcher = DsFetcher()
    fetcher._value = "1783929625569"
    monkeypatch.setattr(fetcher, "_fetch", _FlakyFetcher(["boom"]))

    assert fetcher.get(force=True) == "1783929625569"


# --------------------------------------------------------------------------- #
# ③ 详情失败文案：人话进用户侧，原始报文只进日志
# --------------------------------------------------------------------------- #
def test_detail_failure_reason_translates_curl_timeout():
    """curl 28 超时 ⇒ 一句人话，**不含** curl 原文（用户实测反馈的原话）。"""
    from backend.app.services.note_sync_service import _classify_detail_failure

    exc = RuntimeError(
        "Failed to perform, curl: (28) Operation timed out after 8009 milliseconds "
        "with 1152 bytes received. See https://curl.se/libcurl/c/libcurl-errors.html "
        "first for more details."
    )
    reason, code = _classify_detail_failure(exc)
    assert code == "DETAIL_TIMEOUT"
    assert "bootstrap" not in reason and "curl" not in reason and "8009" not in reason
    assert "稍后重试" in reason


def test_detail_failure_reason_classifies_connection_errors():
    from backend.app.services.note_sync_service import _classify_detail_failure

    reason, code = _classify_detail_failure(RuntimeError("curl: (7) Failed to connect to x"))
    assert code == "DETAIL_NETWORK_ERROR"
    assert "curl" not in reason


def test_detail_failure_reason_keeps_unknown_cause_but_truncates():
    """非网络异常仍带真原因（失败三分法），但截断 —— 长技术栈不许糊满进度卡。"""
    from backend.app.services.note_sync_service import _classify_detail_failure

    reason, code = _classify_detail_failure(RuntimeError("x" * 500))
    assert code == "DETAIL_REQUEST_FAILED"
    assert len(reason) <= 90 and reason.endswith("…")


# --------------------------------------------------------------------------- #
# ④ 落库的平台 user_id 注入（省掉那一次 bootstrap 请求）
# --------------------------------------------------------------------------- #
def test_pc_adapter_injects_user_id_into_auth():
    """给了 user_id ⇒ Auth 直接带上 ⇒ SDK 不再为 xy-direction 先发 user/me。"""
    from backend.app.adapters.xhs.pc_api_adapter import XhsPcApiAdapter

    adapter = XhsPcApiAdapter("a1=1; web_session=2", user_id="5f8a1c2d3e4f")
    assert adapter._api().auth.user_id == "5f8a1c2d3e4f"


def test_pc_adapter_without_user_id_leaves_bootstrap_to_sdk():
    from backend.app.adapters.xhs.pc_api_adapter import XhsPcApiAdapter

    assert XhsPcApiAdapter("a1=1; web_session=2")._api().auth.user_id == ""


def test_build_pc_adapter_passes_external_user_id(monkeypatch):
    """`hydrate_note_detail` 从账号行取 `external_user_id` 传给工厂。"""
    from backend.app.services.note_sync_service import _build_pc_adapter

    seen: dict[str, str] = {}

    class _Factory:
        def __call__(self, cookies, user_id=""):
            seen["cookies"] = cookies
            seen["user_id"] = user_id
            return object()

    class _Account:
        external_user_id = "5f8a1c2d3e4f"

    _build_pc_adapter(_Factory(), "a1=1", _Account())
    assert seen == {"cookies": "a1=1", "user_id": "5f8a1c2d3e4f"}


def test_build_pc_adapter_tolerates_legacy_single_arg_factory():
    """老替身（只吃一个位置参数）仍可用 —— 这是加固，不是新门禁。"""
    from backend.app.services.note_sync_service import _build_pc_adapter

    class _LegacyFactory:
        def __call__(self, cookies):
            return ("built", cookies)

    class _Account:
        external_user_id = ""

    assert _build_pc_adapter(_LegacyFactory(), "a1=1", _Account()) == ("built", "a1=1")
