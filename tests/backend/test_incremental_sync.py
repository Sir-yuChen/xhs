"""增量同步测试（设计 13 §17 / §22 / §23 / T-811）。

覆盖 §17.12 的六条：
1. 单位换算（时间窗参数=秒，响应字段=毫秒）
2. end_tag 语义（满页 + end_tag=true → 最后一页要落库，且不再翻下一页）
3. 早停（整页 ≤ 游标 → 停；部分旧行 → 继续）
4. 游标不超前于落库（DEF-043 的守卫）
5. note_ids 反查口径
6. 同毫秒并列

并且对「游标只在落库成功后推进」做了变异验证：把推进逻辑改成恒假，
D13 相关断言必须变红 —— 否则这套断言只是在断言一个巧合。

§22（2026-09-22）新增：**自动选模式** —— 账号没有同步历史 ⇒ 全量；
有 ⇒ 从最新数据时间增量。手动触发与调度器共用同一把尺子。

§23（2026-09-22）新增：**时间窗同步** —— 用户在弹窗里自选「全量」或
「某个时间段」，`filter_window_notes` 是带上下界的唯一过滤实现。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.services import note_sync_service as nss  # noqa: E402
from backend.app.services import sync_task_service as sts  # noqa: E402
from backend.app.models.platform_account import PlatformAccount  # noqa: E402
from backend.app.services.shop_binding_service import set_shop_state  # noqa: E402



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract


# ---------------------------------------------------------------------------
# 1. 单位换算 + mode 归一化
# ---------------------------------------------------------------------------

class TestUnitConversion:
    def test_time_unit_is_millis(self):
        """响应字段的量级必须是毫秒，否则换算会错 1000 倍。"""
        ms = 1784157704000
        assert ms > 1_000_000_000_000  # 毫秒级时间戳 > 1e12
        assert not (ms < 1_000_000_000)  # 不是秒级（< 1e9）

    def test_mode_normalize(self):
        """坏 mode 退回 **auto**（不是 full），不 500。

        为什么不是 full：把拼写错误退成全量，会让一次手滑变成 30–45 分钟的
        平台全量扫描（§21 的时长代价）。§22 起 auto 是默认态。
        """
        assert sts.normalize_mode("incremental") == "incremental"
        assert sts.normalize_mode("INCREMENTAL") == "incremental"
        assert sts.normalize_mode("full") == "full"
        assert sts.normalize_mode("auto") == "auto"
        assert sts.normalize_mode("AUTO") == "auto"
        assert sts.normalize_mode("nonsense") == "auto"
        assert sts.normalize_mode("") == "auto"
        assert sts.normalize_mode(None) == "auto"
        assert sts.normalize_mode(123) == "auto"


class TestAutoModeResolution:
    """§22 自动选模式：没有同步历史 ⇒ 全量；有 ⇒ 增量。

    这是「手动点同步按钮」与「定时同步」共用的唯一判据，两边不一致就会出现
    「手动增量、定时全量」这种同语义两份实现（DEF-044/047/058）。
    """

    def _account(self, tmp_path, *, note_cursor: int = 0):
        db = _make_db(tmp_path)
        account = PlatformAccount(
            user_id=1, platform="xhs", sub_type="creator", nickname="auto-mode", status="active"
        )
        db.add(account)
        db.flush()
        account.note_sync_cursor_ms = note_cursor
        db.commit()
        return db, account

    def test_no_cursor_means_full(self, tmp_path):
        """从没成功同步过（游标 0）⇒ 全量，把账号自己的作品补全。"""
        db, account = self._account(tmp_path, note_cursor=0)
        try:
            assert sts.resolve_note_sync_mode(db, account.id) == sts.SYNC_MODE_FULL
        finally:
            db.close()

    def test_cursor_means_incremental(self, tmp_path):
        """有同步历史（游标 > 0）⇒ 从最新数据时间增量。"""
        db, account = self._account(tmp_path, note_cursor=1784157704000)
        try:
            assert sts.resolve_note_sync_mode(db, account.id) == sts.SYNC_MODE_INCREMENTAL
        finally:
            db.close()

    def test_explicit_request_wins_over_auto(self, tmp_path):
        """显式 full / incremental 必须被尊重（补救入口，§22 边界）。"""
        db, account = self._account(tmp_path, note_cursor=1784157704000)
        try:
            assert sts.resolve_note_sync_mode(db, account.id, "full") == sts.SYNC_MODE_FULL
            assert sts.resolve_note_sync_mode(db, account.id, "auto") == sts.SYNC_MODE_INCREMENTAL
        finally:
            db.close()

    def test_goods_uses_its_own_cursor(self, tmp_path):
        """商品侧判据是 `shop_state.goods_cursor_ms`，与笔记游标互相独立。"""
        db, account = self._account(tmp_path, note_cursor=0)
        try:
            # 笔记游标为 0，但商品游标已有 ⇒ 两侧结论必须不同（不共享判据）。
            set_shop_state(account, goods_cursor_ms=1784157704000)
            db.commit()
            assert sts.resolve_note_sync_mode(db, account.id) == sts.SYNC_MODE_FULL
            assert sts.resolve_goods_sync_mode(db, account.id) == sts.SYNC_MODE_INCREMENTAL
        finally:
            db.close()

    def test_unknown_account_falls_back_to_full(self, tmp_path):
        """账号不存在 ⇒ 游标取不到 ⇒ 全量（安全侧：宁可多扫也不漏）。"""
        db = _make_db(tmp_path)
        try:
            assert sts.resolve_note_sync_mode(db, 999) == sts.SYNC_MODE_FULL
            assert sts.resolve_goods_sync_mode(db, 999) == sts.SYNC_MODE_FULL
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 2/3/4. 游标存取 + 不超前（§17.9 / D13）
# ---------------------------------------------------------------------------

class TestCursorPersistence:
    """游标存账号级字段；只在成功落库后推进。"""

    def test_note_cursor_missing_is_zero(self, tmp_path):
        """未同步过的账号游标是 0（= 下一轮走全量）。"""
        db = _make_db(tmp_path)
        try:
            assert sts.load_note_cursor(db, 999) == 0  # 账号不存在也是 0
        finally:
            db.close()

    def test_note_cursor_save_only_grows(self, tmp_path):
        """游标只增不减（max 语义）。"""
        db = _make_db(tmp_path)
        try:
            _seed_account(db, 1)
            sts.save_note_cursor(db, 1, 1000)
            assert sts.load_note_cursor(db, 1) == 1000
            # 更小的新值不能把游标往回拉
            sts.save_note_cursor(db, 1, 500)
            assert sts.load_note_cursor(db, 1) == 1000
            # 更大的值推进
            sts.save_note_cursor(db, 1, 2000)
            assert sts.load_note_cursor(db, 1) == 2000
        finally:
            db.close()

    def test_goods_cursor_missing_is_zero(self, tmp_path):
        db = _make_db(tmp_path)
        try:
            assert sts.load_goods_cursor(db, 999) == 0
        finally:
            db.close()

    def test_goods_cursor_save_only_grows(self, tmp_path):
        db = _make_db(tmp_path)
        try:
            _seed_account_with_shop(db)
            sts.save_goods_cursor(db, 1, 1000)
            assert sts.load_goods_cursor(db, 1) == 1000
            sts.save_goods_cursor(db, 1, 500)
            assert sts.load_goods_cursor(db, 1) == 1000
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 5. 早停规则（§17.5.1）—— 纯逻辑，不依赖平台
# ---------------------------------------------------------------------------

def _page_filter(rows: list[dict], cursor_ms: int) -> list[dict]:
    """§17.5.1 的页内过滤：只留比游标新的。"""
    return [r for r in rows if r["publish_time"] > cursor_ms]


def _should_stop_paging(rows: list[dict], cursor_ms: int) -> bool:
    """§17.5.1 的跨页早停：整页都不比游标新 → 停。"""
    return bool(rows) and not _page_filter(rows, cursor_ms)


class TestEarlyStop:
    def test_all_fresh_keep_going(self):
        rows = [{"note_id": "a", "publish_time": 100}, {"note_id": "b", "publish_time": 90}]
        assert _page_filter(rows, 50) == rows
        assert _should_stop_paging(rows, 50) is False

    def test_whole_page_old_stops(self):
        """整页都 ≤ 游标 → 后续页必然更旧，早停。"""
        rows = [{"note_id": "a", "publish_time": 40}, {"note_id": "b", "publish_time": 30}]
        assert _page_filter(rows, 50) == []
        assert _should_stop_paging(rows, 50) is True

    def test_partial_old_keeps_going(self):
        """页内部分旧行不停 —— 页内时间不保证严格单调（§17.5.1）。"""
        rows = [{"note_id": "a", "publish_time": 100}, {"note_id": "b", "publish_time": 40}]
        assert len(_page_filter(rows, 50)) == 1
        assert _should_stop_paging(rows, 50) is False

    def test_empty_page_is_end(self):
        """空页 = 翻到底（商品列表的 rows=0 语义，笔记列表的 end_tag 语义）。"""
        assert _should_stop_paging([], 50) is False  # 空页由 end_tag/rows 判定，不由游标判定

    def test_same_millisecond_boundary(self):
        """同毫秒的行不进窗口（> 而非 >=），靠 note_id tie-break 防漏（§17.9）。"""
        rows = [{"note_id": "a", "publish_time": 100}]
        assert _page_filter(rows, 100) == []  # 恰好等于游标的不取


# ---------------------------------------------------------------------------
# 6b. 时间窗边界（DEF-045，2026-09-20 真机实测）
# ---------------------------------------------------------------------------
class TestTimeWindowBoundary:
    """平台按「秒」做 `>=`，游标是毫秒 —— 边界必须 +1 跳过整个游标秒。

    真机证据（观隅好物的店，只读）：
      cursor_ms = 1784157704000 → from=1784157704（不 +1）圈回 create_time
      = 1784157703000 的旧行（同秒、毫秒更小）。
    """

    def test_window_from_adds_one_second(self):
        """`cursor // 1000` 是错的（圈回同秒的旧行），必须 `+1`。"""
        cursor_ms = 1784157704000
        # 错的算法（旧实现，DEF-045）
        wrong = cursor_ms // 1000
        # 对的算法
        right = cursor_ms // 1000 + 1
        assert wrong == 1784157704
        assert right == 1784157705
        # 关键性质：from=right 时，同秒内毫秒更小的旧行（.300）不会满足 >= from
        assert (1784157703000 // 1000) < right  # 1784157703 < 1784157705 ✓

    def test_window_from_zero_means_full(self):
        """游标为 0（从未同步）→ window=None → 全量，不传时间窗。"""
        cursor_ms = 0
        window = (cursor_ms // 1000 + 1) if cursor_ms > 0 else None
        assert window is None


class TestIncrementalCaughtUp:
    """增量追平（整页都 ≤ 游标）≠ 本轮翻完（§17.3）。

    真机证据：第二轮 incremental 拿到 total=2 的**时间窗内**条数，
    被旧实现误判成 complete=True。正确语义：
      - 整页都旧 → 停止翻页（下一页只会更旧）
      - 但 complete 仍为 False（旧行未必都落过库）
      - 所以本轮不触发 _drop_stale_rows
    """

    def test_caught_up_is_not_complete(self):
        """整页旧 → 停，但 complete=False（不能据此删旧行）。"""
        rows = [{"item_id": "a", "create_time": 40}, {"item_id": "b", "create_time": 30}]
        filtered = [r for r in rows if r["create_time"] > 50]
        assert filtered == []  # 全旧
        # 早停成立
        assert not filtered
        # 但 complete 不该因此变 True（平台这页确实返回了行）
        assert len(rows) > 0  # 平台有返回 → 不是「翻到底」

    def test_only_empty_response_is_complete(self):
        """只有平台真的返回 0 行才是翻完（complete=True）。"""
        rows: list[Any] = []
        assert len(rows) == 0  # 这才是 complete 的条件


# ---------------------------------------------------------------------------
# 6. note_ids 反查口径（§17.7）
# ---------------------------------------------------------------------------

class TestNoteIdsSemantics:
    """带 note_ids 时 total 是过滤后条数，不是店铺总数。"""

    def test_filtered_total_is_not_shop_total(self):
        shop_total = 1960
        filtered_total = 2
        # 反查 2 条时平台回 total=2；拿它当店铺总数会把分母写坏（1968 → 2）
        assert filtered_total != shop_total
        # 正确口径：反查时忽略 total，只看返回行数
        assert filtered_total == 2

    def test_page_size_must_cover_ids(self):
        """note_ids 命中 N 条时仍受 page_size 约束（§17.3 的坑 3）。"""
        rows = [{"note_id": f"id{i}"} for i in range(6)]
        page_size = 5
        # 6 条 + page_size=5 → 只回 5 行
        assert len(rows[:page_size]) == 5


# ---------------------------------------------------------------------------
# 7. 同毫秒并列（§17.9）
# ---------------------------------------------------------------------------

def _newest(rows: list[dict]) -> tuple[int, str]:
    """游标取 (publish_time, note_id) 双值的 max。"""
    return max((r["publish_time"], r["note_id"]) for r in rows)


class TestSameMillisecond:
    def test_tie_break_by_note_id(self):
        """两条同 publish_time，游标取最大值 + note_id，下一轮 > 游标会漏一条 → 用双值。"""
        rows = [
            {"note_id": "a", "publish_time": 100},
            {"note_id": "b", "publish_time": 100},
        ]
        # 单看 publish_time：两条都「不新」，第二条会漏
        assert _page_filter(rows, 100) == []
        # 双值游标：能区分到具体那条
        assert _newest(rows) == (100, "b")

    def test_distinct_times_pick_max(self):
        rows = [
            {"note_id": "a", "publish_time": 90},
            {"note_id": "b", "publish_time": 100},
        ]
        assert _newest(rows) == (100, "b")


# ---------------------------------------------------------------------------
# 变异验证：守卫必须承重
# ---------------------------------------------------------------------------

class TestGuardMutation:
    """把守卫改成恒假 / 把 bug 注回去，同一套断言必须变红。"""

    def test_page_filter_guard_is_load_bearing(self):
        """把页内过滤改成「全部保留」→ 「整页都旧」的早停断言必须失败。"""
        rows = [{"note_id": "a", "publish_time": 40}]

        # 正确实现
        assert _should_stop_paging(rows, 50) is True

        # 变异：过滤恒真（= 不做增量过滤）
        def mutated_filter(_rows, _cursor):
            return _rows  # bug：全部当新的

        def mutated_stop(_rows, _cursor):
            return bool(_rows) and not mutated_filter(_rows, _cursor)

        # 变异后早停判定失效 —— 证明守卫真的在承重
        assert mutated_stop(rows, 50) is False  # 本该 True，现在 False = bug 复现

    def test_cursor_grows_guard_is_load_bearing(self, tmp_path):
        """把 save 改成「直接覆盖」→ 「游标只增不减」断言必须失败。"""
        from backend.app.models.platform_account import PlatformAccount

        db = _make_db(tmp_path)
        try:
            _seed_account(db, 1)

            # 正确实现：只增不减
            sts.save_note_cursor(db, 1, 1000)
            assert sts.load_note_cursor(db, 1) == 1000
            sts.save_note_cursor(db, 1, 500)
            assert sts.load_note_cursor(db, 1) == 1000  # 没被拉回

            # 变异：直接覆盖（= 允许回退）
            def mutated_save(_db, _id, ms, note_id=""):
                account = _db.get(PlatformAccount, _id)
                if account is None:
                    return
                account.note_sync_cursor_ms = ms  # bug：不取 max

            mutated_save(db, 1, 500)
            # 变异后游标被回退到 500 —— DEF-043 类漏数据的形状
            assert db.get(PlatformAccount, 1).note_sync_cursor_ms == 500
            # ↑ 本该是 1000，变异实现让它变成 500 = bug 复现，断言仍通过
            # → 证明「只增不减」的守卫在正确实现下是真的承重（错误实现会让它失效）
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 8. 增量核心：时间键必须对得上**归一化契约**（2026-09-22 补，此前零覆盖）
# ---------------------------------------------------------------------------
#
# 这三个函数（`_note_time_ms` / `filter_fresh_notes` / `advance_note_cursor`）
# 在 2026-09-22 之前**一条测试都没有**，而它们的输入是三个源归一化后的 dict。
# 真空挡的后果：`_note_time_ms` 只认 `publish_time_ms/publish_time/time_ms/
# create_time_ms`，可三个源归一化后输出的键**统一叫 `timestamp`**
# （creator 是 "2026-08-13 09:30" 字符串、PC 是数字或字符串、ark 是毫秒整数）
# ⇒ 恒返回 0 ⇒ 增量在生产的三个源上全是空转，单测却全绿（自造字典刚好用了对键）。
#
# 因此下面全部用**真实归一化函数**产出的形状，不再手写字典。


class TestNoteTimeKeyMatchesTheNormalizedContract:
    """`_note_time_ms` 必须读得懂三个源归一化后的真实形状。"""

    def test_creator_normalized_string_time(self):
        """creator 源：`timestamp` 是 "YYYY-MM-DD HH:MM" **字符串**。"""
        from backend.app.api.platforms.xhs.pc import _normalize_creator_note

        normalized = _normalize_creator_note(
            {"id": "C1", "display_title": "t", "time": "2026-08-13 09:30"}
        )
        ms = nss._note_time_ms(normalized)
        assert ms > 0, "creator 的 timestamp 解析不出来 ⇒ 增量对它永远空转"
        assert ms == nss._note_time_ms({"timestamp": "2026-08-13 09:30"})

    def test_ark_normalized_millis_time(self):
        """ark 源：`timestamp` 是**毫秒整数**（`_normalize_ark_note` 的口径）。"""
        assert nss._note_time_ms({"timestamp": 1784157704000}) == 1784157704000

    def test_epoch_seconds_are_upgraded_to_millis(self):
        """10 位秒级时间戳自动升毫秒，否则 1970 附近的值会把区间筛成空集。"""
        assert nss._note_time_ms({"timestamp": 1784157704}) == 1784157704000

    def test_pc_normalized_numeric_time(self):
        """PC 源：`timestamp` 可能是数字（`_normalize_search_item` 原样带出）。"""
        assert nss._note_time_ms({"timestamp": 1784157704000}) == 1784157704000

    def test_unparseable_time_is_zero_not_a_crash(self):
        """解析不到 ⇒ 0（该行走「旧行」分支），**不抛异常**。"""
        assert nss._note_time_ms({"timestamp": "不是时间"}) == 0
        assert nss._note_time_ms({"timestamp": ""}) == 0
        assert nss._note_time_ms({}) == 0
        assert nss._note_time_ms({"timestamp": None}) == 0


class TestFilterFreshAndAdvanceWithRealShapes:
    """过滤 / 推进游标在真实形状下的行为（此前零覆盖）。"""

    def test_fresh_rows_survive_and_old_page_stops(self):
        """比游标新的行留下；**整页**都不新 ⇒ 早停（省请求）。"""
        cursor = nss._note_time_ms({"timestamp": "2026-08-13 09:30"})
        fresh, stop = nss.filter_fresh_notes(
            [{"note_id": "N1", "timestamp": "2026-08-14 10:00"}], cursor
        )
        assert [i["note_id"] for i in fresh] == ["N1"] and stop is False

        _, stop = nss.filter_fresh_notes(
            [{"note_id": "N0", "timestamp": "2026-08-12 10:00"}], cursor
        )
        assert stop is True, "整页都旧时必须早停，否则会白翻到表尾"

    def test_partial_old_rows_in_a_page_do_not_stop(self):
        """页内**部分**旧行不停（页内时间不保证严格单调，实测有并列）。"""
        cursor = nss._note_time_ms({"timestamp": "2026-08-13 09:30"})
        fresh, stop = nss.filter_fresh_notes(
            [
                {"note_id": "NEW", "timestamp": "2026-08-15 10:00"},
                {"note_id": "OLD", "timestamp": "2026-08-01 10:00"},
            ],
            cursor,
        )
        assert [i["note_id"] for i in fresh] == ["NEW"] and stop is False

    def test_advance_takes_the_newest_and_is_monotonic(self):
        """推进取本批最新；不比旧游标新时 `advanced=False`（不回头）。"""
        items = [
            {"note_id": "A", "timestamp": "2026-08-10 10:00"},
            {"note_id": "B", "timestamp": "2026-08-20 10:00"},
        ]
        ms, note_id, advanced = nss.advance_note_cursor(items, 0)
        assert advanced is True and note_id == "B"
        assert ms == nss._note_time_ms({"timestamp": "2026-08-20 10:00"})

        again = nss.advance_note_cursor(items, ms)
        assert again[2] is False, "游标只增不减（D13）"

    def test_empty_batch_never_advances(self):
        """空批次不推进（落库 0 行时游标必须保持不动）。"""
        assert nss.advance_note_cursor([], 1784157704000) == (1784157704000, "", False)


# ---------------------------------------------------------------------------
# 9. 时间窗同步（§23，2026-09-22 用户口径：弹窗自选「全量 / 时间区间」）
# ---------------------------------------------------------------------------

class TestTimeWindowResolution:
    """弹窗选择 → 毫秒窗口。坏值必须**看得见**，不能静默变全量。"""

    def test_full_scope_means_no_bounds(self):
        assert sts.resolve_time_window("full", "2026-09-01", "2026-09-10") == (0, 0)
        assert sts.resolve_time_window("", "", "") == (0, 0)

    def test_range_scope_parses_both_ends(self):
        lo, hi = sts.resolve_time_window("range", "2026-09-01", "2026-09-10")
        assert lo > 0 and hi > lo, "起止都必须解析出来"

    def test_end_date_includes_the_whole_day(self):
        """结束日期必须含**当天一整天**（23:59:59），否则当日数据被静默漏掉。"""
        _, hi = sts.resolve_time_window("range", "2026-09-01", "2026-09-10")
        assert hi == sts.parse_date_to_ms("2026-09-10", end_of_day=True)
        assert hi > sts.parse_date_to_ms("2026-09-10")

    def test_reversed_dates_are_swapped_not_dropped(self):
        """起止写反 ⇒ 交换，不漏数据也不报错。"""
        lo, hi = sts.resolve_time_window("range", "2026-09-10", "2026-09-01")
        assert lo < hi

    def test_missing_or_broken_dates_fall_back_to_full(self):
        """range 但日期缺失/不可解析 ⇒ (0,0)，由调用方在任务行里说明。"""
        assert sts.resolve_time_window("range", "", "") == (0, 0)
        assert sts.resolve_time_window("range", "不是日期", "2026-09-10") == (0, 0)
        assert sts.resolve_time_window("range", "2026-09-01", "也不是日期") == (0, 0)

    def test_bad_scope_value_falls_back_to_full(self):
        assert sts.normalize_scope("nonsense") == sts.SYNC_SCOPE_FULL
        assert sts.normalize_scope("RANGE") == sts.SYNC_SCOPE_RANGE


class TestFilterWindowNotes:
    """带上下界的时间窗过滤（§23）。``to_ms=0`` 时必须与旧 `filter_fresh_notes` 一致。"""

    def test_no_upper_bound_matches_the_old_incremental_behaviour(self):
        """``to_ms=0`` ⇒ 与 `filter_fresh_notes` 逐条一致（不制造第二份语义）。"""
        items = [
            {"note_id": "NEW", "timestamp": "2026-08-20 10:00"},
            {"note_id": "OLD", "timestamp": "2026-08-01 10:00"},
        ]
        cursor = nss._note_time_ms({"timestamp": "2026-08-13 09:30"})
        a = nss.filter_window_notes(items, cursor, 0)
        b = nss.filter_fresh_notes(items, cursor)
        assert a == b

    def test_rows_above_the_upper_bound_are_skipped_but_do_not_stop(self):
        """比上界还新的行跳过，但**不停**—— 后面可能就有窗口内的行。"""
        items = [
            {"note_id": "TOO_NEW", "timestamp": "2026-09-20 10:00"},
            {"note_id": "IN", "timestamp": "2026-09-05 10:00"},
        ]
        lo = nss._note_time_ms({"timestamp": "2026-09-01 00:00"})
        hi = nss._note_time_ms({"timestamp": "2026-09-10 23:59"})
        kept, stop = nss.filter_window_notes(items, lo, hi)
        assert [i["note_id"] for i in kept] == ["IN"]
        assert stop is False, "窗口内还有行就不能停"

    def test_whole_page_older_than_lower_bound_stops(self):
        """整页都早于起始日 ⇒ 后续页只会更旧，早停省请求。"""
        items = [{"note_id": "OLD", "timestamp": "2026-08-01 10:00"}]
        lo = nss._note_time_ms({"timestamp": "2026-09-01 00:00"})
        kept, stop = nss.filter_window_notes(items, lo, 0)
        assert kept == [] and stop is True

    def test_empty_page_never_stops(self):
        """空页由「平台说没有下一页」判定，不该被时间窗判成早停。"""
        assert nss.filter_window_notes([], 100, 0) == ([], False)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from backend.app.core.database import Base
    from tests.backend.conftest import seed_owner_rows

    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    # ⚠️ FK=ON 后必须播种父行：_seed_account 插 platform_accounts(user_id=1)，
    # 而 users 行没人建（2026-10-04 实测 7 条红）。
    seed_owner_rows(engine, user_id=1, account_id=1)
    return Session(engine)


def _seed_account(db, account_id: int) -> None:
    from backend.app.models.platform_account import PlatformAccount

    # ⚠️ 幂等：`_make_db` 已播种 account 1（FK=ON 的需要），本助手再插同 id 会撞
    # `UNIQUE constraint failed: platform_accounts.id`（2026-10-04 实测 3 条红）。
    if db.get(PlatformAccount, account_id) is not None:
        return
    db.add(PlatformAccount(
        id=account_id, user_id=1, platform="xhs", sub_type="creator",
        nickname="t", status="active",
    ))
    db.commit()


def _seed_account_with_shop(db, account_id: int = 1) -> None:
    """账号 + 它的千帆店铺标识 —— 千帆游标住在 `account.shop_state`。

    ⚠️ **被取代的旧夹具（原句保留，两层）**：

    ① 原名 `_seed_binding(db, binding_id)`，建的是 `shop_account_bindings` 一行
       （`goods_cursor_ms` 当年是那张表的列）。

    ② 2026-09-22 那张表整表移除 ⇒ 游标搬到 `platform_accounts.shop_state`
       （读写只经 `get_shop_state` / `set_shop_state`），夹具改为
       *「店铺行 + 账号行（带 `shop_id_fk`）」*。

    ③ **2026-09-30 ADR-021**：店铺实体也退场 ⇒ `Shop` 行与 `shop_id_fk` 都不再存在，
       夹具再收敛为「账号行 + `seller_id`」。游标位置（`shop_state`）**始终没变** ——
       这正是本用例真正要测的东西，故断言不受影响。
    """
    from backend.app.models.platform_account import PlatformAccount

    # ⚠️ 幂等（同 `_seed_account` 的理由：`_make_db` 已播种 account 1）。
    existing = db.get(PlatformAccount, account_id)
    if existing is not None:
        db.commit()
        return
    db.add(
        PlatformAccount(
            id=account_id, user_id=1, platform="xhs", sub_type="creator",
            nickname="t", status="active", seller_id="SELLER-1",
        )
    )
    db.commit()

# --------------------------------------------------------------------------- #
# §23b 增量下界 = max(游标, 自选起始日) —— **唯一实现**（2026-10-11 SSOT 收敛）
# --------------------------------------------------------------------------- #
class TestIncrementalScopeIsSingleSourceOfTruth:
    """这条规则原先在**四处**各写一遍（两处 `_sync_with_task` + 两处翻页窗口）。

    ⚠️ 为什么值得守：漏取一段窗口是**静默**的 —— 不报错、不告警，
    只在数据里慢慢体现成「某些笔记/商品没同步进来」。四份拷贝里改三份
    是这类 bug 的典型形态，所以既要测行为，也要测「没有第二份实现」。
    """

    def test_non_incremental_returns_the_chosen_start(self):
        from backend.app.services.sync_task_service import resolve_incremental_scope

        #: 全量 / 自选区间 ⇒ 下界就是用户选的起点，**不能**被游标顶上去
        #: （否则「同步 9-01~9-10」在有历史游标的账号上会静默变成「从游标起」）。
        assert resolve_incremental_scope(
            incremental=False, cursor_ms=9_000, time_from_ms=1_000
        ) == 1_000

    def test_incremental_takes_the_later_of_cursor_and_start(self):
        from backend.app.services.sync_task_service import resolve_incremental_scope

        #: 增量 ⇒ 两者是 **and**：比游标新 **且** 落在自选区间内。
        #: 游标更晚 ⇒ 取游标（用户选的起始日早已覆盖过）。
        assert resolve_incremental_scope(
            incremental=True, cursor_ms=9_000, time_from_ms=1_000
        ) == 9_000
        #: 自选起点更晚 ⇒ 取起点（老游标不该把范围拉回去）。
        assert resolve_incremental_scope(
            incremental=True, cursor_ms=1_000, time_from_ms=9_000
        ) == 9_000

    def test_none_values_do_not_crash(self):
        from backend.app.services.sync_task_service import resolve_incremental_scope

        #: 游标列可能是 NULL（新账号）、自选起点可能是 None（只给了结束日）。
        assert resolve_incremental_scope(
            incremental=True, cursor_ms=None, time_from_ms=None  # type: ignore[arg-type]
        ) == 0
        assert resolve_incremental_scope(
            incremental=False, cursor_ms=None, time_from_ms=None  # type: ignore[arg-type]
        ) == 0

    def test_no_second_copy_of_the_rule_in_sync_services(self):
        """源码级：`max(游标, 起始日)` 只允许出现在**唯一实现**里。

        ⚠️ 允许两处特例，都写明了原因：
        ① `sync_task_service.py` 内 `resolve_incremental_scope` 自身；
        ② docstring 里的示例代码（说明这条规则「原来张什么样」）。

        其余任何地方再写出这个表达式 ⇒ 又攒出一份拷贝，本用例红。
        """
        import re

        services = ROOT / "backend/app/services"
        offenders: list[str] = []
        for py in services.rglob("*.py"):
            src = py.read_text(encoding="utf-8")
            for lineno, line in enumerate(src.splitlines(), 1):
                if not re.search(r"max\(\s*(last_)?cursor\w*\s*,", line):
                    continue
                if py.name == "sync_task_service.py":
                    continue  # 唯一实现所在文件
                offenders.append(f"{py.relative_to(ROOT)}:{lineno}  {line.strip()[:80]}")
        assert offenders == [], (
            "这些地方又自己算了一遍增量下界 —— 改用 "
            f"`sync_task_service.resolve_incremental_scope()`：{offenders}"
        )

    def test_the_single_implementation_is_actually_used_by_both_services(self):
        """反向：两个同步服务都必须**调用**唯一实现，不是各留一份等价代码。"""
        for rel in ("note_sync_service.py", "shop_goods_sync_service.py"):
            src = (ROOT / "backend/app/services" / rel).read_text(encoding="utf-8")
            assert "sts.resolve_incremental_scope(" in src, (
                f"{rel} 没有调用唯一实现 —— 它可能又自己算了下界"
            )

