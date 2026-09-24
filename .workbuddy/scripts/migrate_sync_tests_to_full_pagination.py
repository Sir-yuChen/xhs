# -*- coding: utf-8 -*-
"""把同步测试从「三源 + 有界翻页」契约迁移到「creator 强制 + 全量翻页」（设计 13 §21）。

一次性脚本，跑完即弃。按**函数名**定位并整体替换，替换后打印命中情况。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NOTE = ROOT / "tests/backend/test_note_sync.py"
GOODS = ROOT / "tests/backend/test_shop_goods_sync.py"


def locate(text: str, name: str) -> tuple[int, int]:
    pattern = re.compile(rf"\ndef {re.escape(name)}\(.*?(?=\ndef |\n# ---|\Z)", re.S)
    match = pattern.search(text)
    if not match:
        raise LookupError(name)
    return match.start() + 1, match.end()


def replace_func(text: str, name: str, new_code: str) -> str:
    start, end = locate(text, name)
    return text[:start] + new_code.rstrip("\n") + "\n\n" + text[end:].lstrip("\n")


def delete_func(text: str, name: str) -> str:
    start, end = locate(text, name)
    return text[:start] + text[end:].lstrip("\n")


# --------------------------------------------------------------------------- #
# 1) 假适配器：创作者列表支持多页（全量翻页用例需要）
# --------------------------------------------------------------------------- #
FAKE_CREATOR_OLD = '''class FakeCreatorAdapter:
    calls: list = []
    note_ids: list = ["C-N1", "C-N2"]
    notes_count = 1969
'''
FAKE_CREATOR_NEW = '''class FakeCreatorAdapter:
    calls: list = []
    note_ids: list = ["C-N1", "C-N2"]
    notes_count = 1969
    #: 多页形状（§21 全量翻页用例专用）：`{页码: [note_id, ...]}`，页码从 0 起
    #: （与 `_creator_page_index` 同口径）。`None` = 单页（其余用例的默认形状）。
    pages: "dict | None" = None
'''

CREATOR_PAGE_OLD = '''    def get_posted_notes_page(self, page: int = 0, tab: int = 0):
        type(self).calls.append(page)
        return True, "", {
            "data": {
                "notes": [_creator_note(note_id) for note_id in type(self).note_ids],
                # 创作者列表用服务端游标翻页，-1 终止。
                "page": -1,
'''
CREATOR_PAGE_NEW = '''    def get_posted_notes_page(self, page: int = 0, tab: int = 0):
        type(self).calls.append(page)
        pages = type(self).pages
        if pages is None:
            ids, next_page = type(self).note_ids, -1
        else:
            # §21：`data.page` 是**下一个**游标，`-1` 终止（`_creator_pagination` 口径）。
            ids = pages.get(page, [])
            next_page = page + 1 if (page + 1) in pages else -1
        return True, "", {
            "data": {
                "notes": [_creator_note(note_id) for note_id in ids],
                "page": next_page,
'''

RESET_OLD = "    FakeCreatorAdapter.notes_count = 1969\n"
RESET_NEW = "    FakeCreatorAdapter.notes_count = 1969\n    FakeCreatorAdapter.pages = None\n"

# --------------------------------------------------------------------------- #
# 2) 待替换的用例
# --------------------------------------------------------------------------- #
REPLACEMENTS: "dict[str, str]" = {}

REPLACEMENTS["test_all_sources_unavailable_fails_with_multi_cause"] = '''
def test_missing_creator_session_fails_with_structured_reason(pc_env_no_binding):
    """**创作者平台会话是唯一硬门禁**（§21.4，2026-09-22 用户口径）。

    没有创作者会话 ⇒ 一律 `CREATOR_SESSION_REQUIRED` + `failed`，**且不退化成
    「用 ark 凑合跑一遍」** —— 用户要的是「补齐后再触发」，不是「哪个源能用就用哪个」。
    """
    _, account_id, user_id, _ = pc_env_no_binding

    result = nss.run_note_sync(account_id, user_id=user_id)

    assert result["status"] == sts.STATUS_FAILED
    assert "CREATOR_SESSION_REQUIRED" in result["error"]
    # 一个平台请求都没发（连 ark 都没试）。
    assert FakePcAdapter.calls == []
    assert FakeQianfanAdapter.calls == 0
'''

REPLACEMENTS["test_every_source_failing_reports_each_reason"] = '''
def test_creator_source_failure_keeps_ark_and_reports_reason(creator_env, monkeypatch):
    """创作者源失败但 ark 源可用 ⇒ **部分成功**（done），原因记在分源明细里。

    §19.3 的纪律不变：源与源互不阻塞，失败的那条**结构化**留在 `sources` 里。
    """
    class FailingCreatorAdapter:
        def __init__(self, cookies: str) -> None:
            self.cookies = cookies

        def get_posted_notes_page(self, page: int = 0, tab: int = 0):
            return False, "登录凭证缺失，请重新登录", {"code": -13000, "success": False}

    monkeypatch.setattr(pc, "get_xhs_creator_api_adapter_factory", lambda: FailingCreatorAdapter)
    monkeypatch.setattr(nss, "_hydrate_pending_notes", lambda *a, **k: {"filled": 0, "pending": 0})
    FakeQianfanAdapter.list_entries = [_ark_note("ARK-N1")]
    _, account_id, user_id, _ = creator_env

    result = nss.run_note_sync(account_id, user_id=user_id)

    assert result["status"] == sts.STATUS_DONE, result
    by_source = {entry["source"]: entry for entry in result["sources"]}
    assert "登录凭证缺失" in by_source["creator"]["error"]
    assert by_source["ark"]["stored"] == 1
'''

REPLACEMENTS["test_ark_only_account_syncs_qianfan_notes"] = '''
def test_without_creator_session_ark_is_not_enough(pc_env, monkeypatch):
    """**只有千帆令牌不够**：同步笔记必须先有创作者平台会话（§21.4）。

    这条钉的是「不退化」—— 旧实现会把 ark 当独立入口跑一遍，任务显示
    「同步成功」，而**一条笔记都没落**，用户以为同步过了。
    """
    monkeypatch.setattr(nss, "_hydrate_pending_notes", lambda *a, **k: {"filled": 0, "pending": 0})
    FakeQianfanAdapter.list_entries = [_ark_note("ARK-N1")]
    _, account_id, user_id, SessionLocal = pc_env

    result = nss.run_note_sync(account_id, user_id=user_id)

    assert result["status"] == sts.STATUS_FAILED
    assert "CREATOR_SESSION_REQUIRED" in result["error"]
    assert FakeQianfanAdapter.calls == 0  # ark 源一次都没打
    db = SessionLocal()
    try:
        assert db.scalars(select(Note.note_id)).all() == []
    finally:
        db.close()
'''

REPLACEMENTS["test_both_www_and_ark_sync_when_tokens_usable"] = '''
def test_creator_and_ark_sources_run_when_both_usable(creator_env, monkeypatch):
    """创作者平台 + 千帆均可用 ⇒ 两个源各自落库（§21：**www 已不参与**）。"""
    monkeypatch.setattr(nss, "_hydrate_pending_notes", lambda *a, **k: {"filled": 0, "pending": 0})
    FakeQianfanAdapter.list_entries = [_ark_note("ARK-N1")]
    _, account_id, user_id, SessionLocal = creator_env

    result = nss.run_note_sync(account_id, user_id=user_id)

    assert result["status"] == sts.STATUS_DONE, result
    by_source = {entry["source"]: entry for entry in result["sources"]}
    # ⚠️ `www` **不在**清单里 —— 这是 §21.4 的那条删除，写回 www 源本用例必红。
    assert set(by_source) == {"creator", "ark"}
    assert by_source["creator"]["stored"] == 2  # C-N1 / C-N2
    assert by_source["ark"]["stored"] == 1

    db = SessionLocal()
    try:
        assert {"C-N1", "C-N2", "ARK-N1"} <= set(db.scalars(select(Note.note_id)).all())
    finally:
        db.close()
'''

REPLACEMENTS["test_ark_merge_never_erases_pc_content"] = '''
def test_ark_merge_never_erases_existing_content(creator_env, monkeypatch):
    """ark 源**填空合并**：已落库的正文 / 标题一个字节不动（§19.3.2 语义不变）。"""
    monkeypatch.setattr(nss, "_hydrate_pending_notes", lambda *a, **k: {"filled": 0, "pending": 0})
    # 创作者源这一轮**不返回 C-N1**，让 C-N1 只由 ark 源处理（否则整行覆盖会先清空正文）。
    FakeCreatorAdapter.note_ids = ["C-OTHER"]
    _, account_id, user_id, SessionLocal = creator_env

    # 先造一条「已落库、带正文」的行（模拟详情补齐已经填过正文）。
    db = SessionLocal()
    try:
        db.add(
            Note(
                user_id=user_id,
                platform="xhs",
                platform_account_id=account_id,
                note_id="C-N1",
                title="已有标题",
                content="已有正文",
            )
        )
        db.commit()
    finally:
        db.close()

    FakeQianfanAdapter.list_entries = [_ark_note("C-N1", title="千帆侧标题")]
    result = nss.run_note_sync(account_id, user_id=user_id)
    assert result["status"] == sts.STATUS_DONE, result

    db = SessionLocal()
    try:
        note = db.scalars(select(Note).where(Note.note_id == "C-N1")).one()
        assert note.content == "已有正文"  # 空值**不覆盖**非空（这是本用例的全部意义）
        assert note.title == "已有标题"
        assert note.published is True  # 只升不降（§19.12）
        assert note.raw_json["ark"]["likes"] == 3
    finally:
        db.close()
'''

REPLACEMENTS["test_page_cap_bounds_the_run"] = '''
def test_full_pagination_stops_only_when_platform_says_done(creator_env, monkeypatch):
    """**不传 `max_pages` = 全量**（§21，2026-09-22 用户口径）。

    翻到平台说「没有下一页」为止 —— 不是翻 3 页就停。这条钉的就是用户报的那个 bug：
    `已落库 180 / 共 1961 条` + `已达本次翻页上限（3 页），剩余留待下一轮`。
    """
    monkeypatch.setattr(nss, "_hydrate_pending_notes", lambda *a, **k: {"filled": 0, "pending": 0})
    FakeCreatorAdapter.pages = {0: ["C-P1"], 1: ["C-P2"], 2: ["C-P3"], 3: ["C-P4"]}
    _, account_id, user_id, _ = creator_env

    result = nss.run_note_sync(account_id, user_id=user_id)  # 刻意不传 max_pages

    assert len(FakeCreatorAdapter.calls) == 4, FakeCreatorAdapter.calls
    assert result["stored"] == 4
    assert "上限" not in result["note"]
'''

REPLACEMENTS["test_stops_early_when_has_more_is_false"] = '''
def test_stops_early_when_platform_reports_no_more(creator_env, monkeypatch):
    """平台 `page=-1` ⇒ 当页就停，**不再多打一次请求**。"""
    monkeypatch.setattr(nss, "_hydrate_pending_notes", lambda *a, **k: {"filled": 0, "pending": 0})
    _, account_id, user_id, _ = creator_env

    result = nss.run_note_sync(account_id, user_id=user_id)

    assert len(FakeCreatorAdapter.calls) == 1
    assert result["note"] == ""
'''

REPLACEMENTS["test_stops_early_when_stored_reaches_total"] = '''
def test_stops_early_when_stored_reaches_total(creator_env, monkeypatch):
    """已落库 ≥ `total` ⇒ 提前结束（不为了凑页数继续翻）。"""
    monkeypatch.setattr(nss, "_hydrate_pending_notes", lambda *a, **k: {"filled": 0, "pending": 0})
    FakeCreatorAdapter.pages = {0: ["C-P1"], 1: ["C-P2"], 2: ["C-P3"]}
    FakeCreatorAdapter.notes_count = 1  # 平台说总共就 1 条
    _, account_id, user_id, _ = creator_env

    nss.run_note_sync(account_id, user_id=user_id)

    assert len(FakeCreatorAdapter.calls) == 1
'''

REPLACEMENTS["test_rate_limit_stops_the_run_without_raising"] = '''
def test_rate_limit_wait_timeout_stops_without_raising(creator_env):
    """等待额度**超时** ⇒ 不抛异常：任务 done、原因写任务行（§21 等待制）。

    ⚠️ 与旧契约的区别：这里断言的**不是**「第一页就被限流挡住」，而是
    「等不到额度才停」。`wait_allow` 而不是 `allow` —— 改回去本用例必红。
    """
    _, account_id, user_id, _ = creator_env
    LIMITER["impl"] = DenyLimiter()

    result = nss.run_note_sync(account_id, user_id=user_id)

    assert FakeCreatorAdapter.calls == []
    assert result["status"] == sts.STATUS_DONE  # 不是异常，是「本次到此为止」
    assert "限流" in result["note"]
'''

REPLACEMENTS["test_rate_limit_keeps_what_the_first_page_already_stored"] = '''
def test_rate_limit_keeps_what_the_first_page_already_stored(creator_env, monkeypatch):
    """第 1 页放行、第 2 页起等待超时：**已同步的必须留下来**。"""
    monkeypatch.setattr(nss, "_hydrate_pending_notes", lambda *a, **k: {"filled": 0, "pending": 0})
    FakeCreatorAdapter.pages = {0: ["C-P1"], 1: ["C-P2"]}

    class OneShotLimiter:
        """第 1 次等额度就成功，之后一律超时。"""

        def __init__(self) -> None:
            self.n = 0

        def allow(self, key) -> bool:  # noqa: ARG002
            self.n += 1
            return self.n == 1

        def wait_allow(self, key, **kwargs) -> bool:  # noqa: ARG002
            return self.allow(key)

    LIMITER["impl"] = OneShotLimiter()
    _, account_id, user_id, SessionLocal = creator_env

    result = nss.run_note_sync(account_id, user_id=user_id)

    assert len(FakeCreatorAdapter.calls) == 1
    assert result["stored"] == 1
    db = SessionLocal()
    try:
        assert db.scalar(select(Note.id).where(Note.note_id == "C-P1")) is not None
    finally:
        db.close()
'''

REPLACEMENTS["test_limited_source_is_marked_limited"] = '''
def test_limited_source_is_marked_limited(creator_env, monkeypatch):
    """ark 源被限流 ⇒ `limited=True`、stored=0（**不是**「千帆 0 条笔记」）。

    真机教训（2026-09-22，acct 8）：creator 源翻页耗光额度后 ark 源被挡，
    结果行显示「千帆商品笔记 0 条」—— 把「没跑完」显示成了「没数据」。
    """
    monkeypatch.setattr(nss, "_hydrate_pending_notes", lambda *a, **k: {"filled": 0, "pending": 0})
    _, account_id, user_id, _ = creator_env

    class PerKeyLimiter:
        """只挡 ark 源的假限流器（按限流键判定，不按账号）。"""

        def allow(self, key) -> bool:  # noqa: ARG002
            return str(key) != f"{account_id}:ark"

        def wait_allow(self, key, **kwargs) -> bool:  # noqa: ARG002
            return self.allow(key)

    LIMITER["impl"] = PerKeyLimiter()
    FakeQianfanAdapter.list_entries = [_ark_note("ARK-N1")]

    result = nss.run_note_sync(account_id, user_id=user_id)

    by_source = {entry["source"]: entry for entry in result["sources"]}
    assert set(by_source) == {"creator", "ark"}
    assert by_source["creator"]["stored"] == 2
    assert by_source["creator"]["limited"] is False
    # ark 源第 1 页前就被挡 ⇒ limited=True（0 条是没跑完，不是没数据）
    assert by_source["ark"]["stored"] == 0
    assert by_source["ark"]["limited"] is True
'''

REPLACEMENTS["test_out_of_range_pages_are_rejected"] = '''
def test_page_bounds_follow_the_new_contract(creator_env, monkeypatch):
    """参数边界（§21）：`0` 越界 422；`21` **现在合法**（上限已放到 `ABSOLUTE_PAGE_CAP`）；
    超过绝对兜底 422；`None`/缺省 = 全量。"""
    headers, account_id, _user_id, _ = creator_env
    monkeypatch.setattr(nss, "_run_in_thread", lambda *a, **k: None)

    reject = ({"max_pages": 0}, {"max_pages": 501}, {"page_size": 0}, {"page_size": 101})
    for body in reject:
        response = client.post(
            f"/api/accounts/{account_id}/notes/sync", headers=headers, json=body
        )
        assert response.status_code == 422, (body, response.text)

    # 21 页**不再是越界**（旧契约 le=20 已废）；缺省也是合法请求（= 全量）。
    for body in ({"max_pages": 21}, {}):
        response = client.post(
            f"/api/accounts/{account_id}/notes/sync", headers=headers, json=body
        )
        assert response.status_code == 200, (body, response.text)
'''

DELETE = [
    "test_pc_sync_uses_the_token_from_the_request",
    "test_missing_token_failure_is_recorded_on_the_task_row",
    "test_progress_total_is_null_when_platform_gives_none",
]

APPEND = '''

# --------------------------------------------------------------------------- #
# §21：全量翻页契约（2026-09-22 用户口径）
# --------------------------------------------------------------------------- #
def test_clamp_pages_none_is_full_and_explicit_is_capped():
    """`clamp_pages`：`None` / 坏值 = **全量**；显式数字夹到 `[1, ABSOLUTE_PAGE_CAP]`。

    ⚠️ 旧契约「坏值退回 3 页」已废 —— 把「没传」当「传了 3」正是那个 bug 的入口。
    """
    assert sts.clamp_pages(None) is None
    assert sts.clamp_pages("") is None
    assert sts.clamp_pages("abc") is None
    assert sts.clamp_pages(0) == 1
    assert sts.clamp_pages(7) == 7
    assert sts.clamp_pages(10**6) == sts.ABSOLUTE_PAGE_CAP


def test_absolute_page_cap_exists_as_a_dead_loop_guard():
    """绝对兜底必须存在且**远高于**真实数据量（否则「取消上限」= 死循环）。"""
    assert sts.DEFAULT_MAX_PAGES is None
    assert sts.ABSOLUTE_PAGE_CAP >= 100


def test_absolute_cap_stops_a_platform_that_never_says_done(creator_env, monkeypatch):
    """平台永远不说「没有下一页」时，循环必须在绝对兜底处停下并如实说明。"""
    monkeypatch.setattr(nss, "_hydrate_pending_notes", lambda *a, **k: {"filled": 0, "pending": 0})
    monkeypatch.setattr(sts, "ABSOLUTE_PAGE_CAP", 3)
    FakeCreatorAdapter.pages = {0: ["C-P1"], 1: ["C-P2"], 2: ["C-P3"], 3: ["C-P4"]}
    _, account_id, user_id, _ = creator_env

    result = nss.run_note_sync(account_id, user_id=user_id)

    assert len(FakeCreatorAdapter.calls) == 3
    assert "兜底" in result["note"]


def test_wait_allow_returns_false_on_timeout_without_hanging():
    """`wait_allow`：额度够就立即放行；超时窗口内等不到就返回 False（不挂死）。"""
    from backend.app.services.rate_limiter import CrawlRateLimiter

    fresh = CrawlRateLimiter(max_per_minute=2)
    assert fresh.wait_allow("9:creator", timeout=0) is True

    used = CrawlRateLimiter(max_per_minute=1)
    assert used.allow("8:creator") is True
    assert used.wait_allow("8:creator", timeout=0) is False
'''

# GOODS 侧只改文案/边界
GOODS_REPLACE = [
    (
        '    for body in ({"max_pages": 0}, {"max_pages": 21}, {"page_size": 0}, {"page_size": 101}):',
        '    # §21：`21` 现在**合法**（上限已放到 `ABSOLUTE_PAGE_CAP=500`）。\n'
        '    for body in ({"max_pages": 0}, {"max_pages": 501}, {"page_size": 0}, {"page_size": 101}):',
    ),
    (
        '"本次未翻完，保留全部既有商品行（不删）"',
        '"未翻完，保留既有行"',
    ),
]


def main() -> int:
    text = NOTE.read_text(encoding="utf-8")

    for old, new in (
        (FAKE_CREATOR_OLD, FAKE_CREATOR_NEW),
        (CREATOR_PAGE_OLD, CREATOR_PAGE_NEW),
        (RESET_OLD, RESET_NEW),
    ):
        assert old in text, old[:60]
        text = text.replace(old, new, 1)

    for name, code in REPLACEMENTS.items():
        text = replace_func(text, name, code)
        print(f"REPLACED {name}")

    for name in DELETE:
        text = delete_func(text, name)
        print(f"DELETED  {name}")

    text = text.rstrip("\n") + "\n" + APPEND
    NOTE.write_text(text, encoding="utf-8")

    goods = GOODS.read_text(encoding="utf-8")
    for old, new in GOODS_REPLACE:
        if old in goods:
            goods = goods.replace(old, new)
            print(f"GOODS ok: {old[:50]!r}")
        else:
            print(f"GOODS MISS: {old[:50]!r}")
    GOODS.write_text(goods, encoding="utf-8")

    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
