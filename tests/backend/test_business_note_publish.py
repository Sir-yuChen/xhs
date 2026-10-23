"""「每日发布商业笔记」自动发布任务（设计 15 §19，2026-10-07 用户需求）。

盯七件事（都是源码断言 / 自我印证证明不了的）：

1. **参数形状**（§19.2）：`limit` 标量 / 映射两形状、`generate_mode` 单选、
   `interval_seconds` 夹回；非法值**报错**而不是静默退回默认。
2. **商品筛选口径**与商品列表页**同一个数**（`note_goods` 关系表 == 0）。
3. **千帆无效 ⇒ 跳过 + 站内消息**（不是静默 continue）。
4. **生成失败不发布**（用户口径「对**生成成功**的笔记进行发布」）。
5. **逐篇之间按 `interval_seconds` 间隔**（用户口径「发布笔记时间间隔单位秒」）。
6. **注册表 / 种子 / 兜底字典三处齐**（少一处就有一类用户看不到这条任务）。
7. **账号隔离**：只跑行主本人的账号（DEF-066 同口径）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.models import PlatformAccount, ShopProduct, Task, User
from backend.app.models.note import (
    NOTE_TYPE_LOCAL_GENERATED,
    PUBLISH_STATUS_FAILED,
    PUBLISH_STATUS_PENDING,
    PUBLISH_STATUS_PUBLISHED,
    Note,
    set_note_publish_status,
)
from backend.app.models.shop import NoteGoods
from backend.app.services import business_note_publish_service as svc



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract


def _session_factory_of(session):
    """由既有 Session 造一个**同引擎**的工厂（服务层自建 Session 要用）。

    `run_business_note_auto_publish` 调 `SessionLocal()`，所以必须把那个模块级
    工厂指到**夹具库**上 —— 不指就是「直接调服务函数静默打生产库」的老事故
    （conftest `db_session` 的 docstring 有完整实录）。
    """
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(autocommit=False, autoflush=False, bind=session.get_bind())


# --------------------------------------------------------------------------- #
# 1) 参数形状
# --------------------------------------------------------------------------- #
class TestLimitShape:
    """`limit` 两种形状 + 非法值报错（设计 15 §19.2）。"""

    def test_scalar_limit_applies_to_every_account(self):
        assert svc.resolve_account_limit({"limit": 3}, 8) == 3
        assert svc.resolve_account_limit({"limit": 3}, 9) == 3

    def test_missing_limit_falls_back_to_default(self):
        assert svc.resolve_account_limit({}, 8) == svc.DEFAULT_LIMIT
        assert svc.resolve_account_limit(None, 8) == svc.DEFAULT_LIMIT

    def test_map_limit_overrides_per_account_and_falls_back(self):
        params = {"limit": {"8": 5, "9": 2}}
        assert svc.resolve_account_limit(params, 8) == 5
        assert svc.resolve_account_limit(params, 9) == 2
        # 没点名的账号 ⇒ 内置默认（不是 0、不是第一个账号的值）
        assert svc.resolve_account_limit(params, 10) == svc.DEFAULT_LIMIT

    def test_map_limit_key_must_be_account_id(self):
        with pytest.raises(svc.BusinessPublishParamsError):
            svc.resolve_account_limit({"limit": {"abc": 1}}, 8)

    def test_map_limit_value_must_be_positive_int(self):
        with pytest.raises(svc.BusinessPublishParamsError):
            svc.resolve_account_limit({"limit": {"8": 0}}, 8)
        with pytest.raises(svc.BusinessPublishParamsError):
            svc.resolve_account_limit({"limit": {"8": "x"}}, 8)
        with pytest.raises(svc.BusinessPublishParamsError):
            svc.resolve_account_limit({"limit": {"8": svc.MAX_LIMIT + 1}}, 8)

    def test_scalar_limit_is_clamped_to_hard_cap(self):
        # 标量越界**夹回**（不报错）—— 与 `days` 同口径；映射越界才报错（用户显式逐账号写）
        assert svc.resolve_account_limit({"limit": 9999}, 8) == svc.MAX_LIMIT
        assert svc.resolve_account_limit({"limit": 0}, 8) == 1


class TestOtherParams:
    def test_generate_mode_is_single_choice(self):
        assert svc.resolve_generate_mode({}) == svc.MODE_QIANFAN  # 默认千帆
        assert svc.resolve_generate_mode({"generate_mode": "llm"}) == svc.MODE_LLM
        assert svc.resolve_generate_mode({"generate_mode": "LLM"}) == svc.MODE_LLM  # 大小写宽容

    def test_unknown_generate_mode_is_rejected_not_silently_defaulted(self):
        """静默退回默认 = 用户以为选了 LLM 却跑了千帆（写操作，代价真实）。"""
        with pytest.raises(svc.BusinessPublishParamsError):
            svc.resolve_generate_mode({"generate_mode": "nope"})

    def test_interval_defaults_and_is_clamped(self):
        assert svc.resolve_interval_seconds({}) == svc.DEFAULT_INTERVAL_SECONDS
        assert svc.resolve_interval_seconds({"interval_seconds": "7"}) == 7
        assert svc.resolve_interval_seconds({"interval_seconds": 0}) == 0
        assert svc.resolve_interval_seconds({"interval_seconds": 999999}) == svc.MAX_INTERVAL_SECONDS
        # 非法文本 ⇒ 默认（间隔是节流参数，不值得为它让整轮任务失败）
        assert svc.resolve_interval_seconds({"interval_seconds": "abc"}) == svc.DEFAULT_INTERVAL_SECONDS

    def test_validator_uses_the_same_rulers(self):
        """注册表 `params_validator` 与执行期**同一把尺子**（DEF-044）。"""
        svc.validate_business_publish_params({}, None, None)  # 空参合法（全默认）
        with pytest.raises(svc.BusinessPublishParamsError):
            svc.validate_business_publish_params({"generate_mode": "nope"}, None, None)
        with pytest.raises(svc.BusinessPublishParamsError):
            svc.validate_business_publish_params({"limit": {"x": 1}}, None, None)


# --------------------------------------------------------------------------- #
# 2) 注册表 / 种子 / 兜底字典三处齐
# --------------------------------------------------------------------------- #
class TestRegistryWiring:
    def test_registry_entry(self):
        from backend.app.services.job_registry import get_job_spec

        spec = get_job_spec("system.business_note_auto_publish")
        assert spec.name == "每日发布商业笔记"
        assert spec.builtin is True
        assert spec.user_scoped is True
        assert spec.params_editable == (
            "account_ids",
            "limit",
            "interval_seconds",
            "generate_mode",
            # 2026-10-07 追加：发布失败重试三件
            "retry_on_failure",
            "max_retries",
            "retry_interval_seconds",
        )
        assert spec.default_trigger_type == "cron"
        assert spec.default_cron == "0 0 9 * * *"
        # 平台写操作 ⇒ 恒 forbid 并发
        assert spec.allow_concurrent is False
        assert spec.params_validator is svc.validate_business_publish_params

    def test_shipped_seed_row(self):
        from backend.app.core.initial_data import load_initial_scheduled_jobs

        rows = {row["job_key"]: row for row in load_initial_scheduled_jobs()}
        row = rows["system.business_note_auto_publish"]
        assert row["name"] == "每日发布商业笔记"
        assert row["trigger_type"] == "cron"
        assert row["status"] == "active"
        assert row["remark"] == "系统内置任务"
        assert set(row["params"]) == {
            "account_ids",
            "limit",
            "interval_seconds",
            "generate_mode",
            # 2026-10-07 追加：发布失败重试三件套（种子同批补齐，迁移
            # `q1r2s3t4u5v6` 负责存量行）
            "retry_on_failure",
            "max_retries",
            "retry_interval_seconds",
        }

    def test_fallback_seed_defaults_has_it(self):
        """兜底字典也要有 —— 「初始化数据缺该键」时它是唯一播种来源。"""
        from backend.app.services.scheduler_service import _builtin_seed_defaults

        assert "system.business_note_auto_publish" in _builtin_seed_defaults()

    def test_validator_rejects_limit_map_keys_that_are_not_mine(self, db_session):
        """`limit` 映射的**键**必须是我自己的账号（否则参数写了静默不生效）。

        形状合法（是十进制整数）但账号不存在 / 不属于我 ⇒ 400。纯形状校验抓不到
        这一类 —— 它会静默退化成「那个账号照旧跑默认篇数」。
        """
        from backend.app.models import PlatformAccount as PA

        mine = TestSelectBusinessItems._account(db_session, 1)
        # user 2 也要有行（FK 目标）—— 别人的账号用**已存在的 user 2**
        TestSelectBusinessItems._user(db_session, 2)
        theirs = PA(user_id=2, platform="xhs", sub_type="qianfan", nickname="别人的", status="active")
        db_session.add(theirs)
        db_session.commit()
        user = db_session.get(User, 1)

        svc.validate_business_publish_params({"limit": {str(mine.id): 2}}, db_session, user)
        with pytest.raises(svc.BusinessPublishParamsError) as e1:
            svc.validate_business_publish_params({"limit": {str(theirs.id): 2}}, db_session, user)
        assert e1.value.code == "BUSINESS_PUBLISH_ACCOUNT_NOT_OWNED"
        with pytest.raises(svc.BusinessPublishParamsError):
            svc.validate_business_publish_params({"limit": {"999999": 2}}, db_session, user)

    def test_params_editable_keys_have_frontend_hints(self):
        """参数中文名 / 提示齐（缺 ⇒ 前端 KV 编辑器只显 key 裸名，用户看不懂）。

        ⚠️ 2026-10-07：`PARAM_LABELS` 已从 `job-form-modal.tsx` **下移到**
        `params-editor.tsx`（做单一真相源，弹窗那份只是 `export { … } from` 转发）
        ⇒ 判据改成读 `editor`。原句读 `form` 在转发形态下恒假（转发不复制字面量）。
        """
        root = __import__("pathlib").Path(__file__).resolve().parents[2]
        form = (root / "frontend/src/components/schedule/job-form-modal.tsx").read_text(
            encoding="utf-8"
        )
        editor = (root / "frontend/src/components/schedule/params-editor.tsx").read_text(
            encoding="utf-8"
        )
        # 转发必须还在（旧调用方 `import { PARAM_LABELS } from "./job-form-modal"` 不许断）
        assert 'export { PARAM_LABELS } from "./params-editor"' in form
        for key in (
            "limit",
            "interval_seconds",
            "generate_mode",
            "retry_on_failure",
            "max_retries",
            "retry_interval_seconds",
        ):
            assert f'{key}:' in editor, f"PARAM_LABELS 缺 {key}"
            assert f'  {key}:' in editor, f"KNOWN_KEY_HINTS 缺 {key}"
        # 生成方式必须是**枚举下拉**（自由文本会拼出后端不认的值）
        assert "generate_mode: { qianfan:" in editor
        assert "PARAM_ENUM_OPTIONS[trimmedKey]" in editor
        # 重试开关同样是枚举下拉（2026-10-07），取值与后端同值
        assert "retry_on_failure: { true:" in editor
        assert 'false: "不重试"' in editor


# --------------------------------------------------------------------------- #
# 3) 商品筛选：挂链笔记数为 0
# --------------------------------------------------------------------------- #
class TestSelectBusinessItems:
    @staticmethod
    def _user(db, user_id: int) -> None:
        """FK 目标必须存在（`platform_accounts.user_id` → `users.id`）。"""
        if db.get(User, int(user_id)) is None:
            db.add(User(id=int(user_id), username=f"biz-user-{user_id}", password_hash="x"))
            db.flush()

    @classmethod
    def _account(cls, db, user_id: int) -> PlatformAccount:
        cls._user(db, user_id)
        account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="qianfan", nickname="测试店", status="active"
        )
        db.add(account)
        db.flush()
        return account

    @staticmethod
    def _product(db, *, account_id: int, item_id: str, create_ms: int = 1000) -> ShopProduct:
        row = ShopProduct(
            user_id=1,
            platform_account_id=account_id,
            item_id=item_id,
            item_title=f"商品 {item_id}",
            create_time_ms=create_ms,
        )
        db.add(row)
        db.flush()
        return row

    def test_only_items_without_linked_notes(self, db_session):
        """有关系的商品**不选**；无关系的才选（口径 = `note_goods` 关系表）。"""
        db = db_session
        try:
            account = self._account(db, 1)
            free = self._product(db, account_id=account.id, item_id="free-1")
            taken = self._product(db, account_id=account.id, item_id="taken-1")
            # 给 taken 挂一篇笔记（关系表按**平台 item_id**）
            db.add(Note(
                user_id=1, platform_account_id=account.id, platform="xhs",
                note_id="n-1", title="已挂", content="", author_name="",
                note_type=NOTE_TYPE_LOCAL_GENERATED,
            ))
            db.flush()
            db.add(NoteGoods(user_id=1, note_id="n-1", item_id="taken-1"))
            db.commit()

            items = svc.select_business_items(db, account_id=account.id, limit=10)
            assert [i.item_id for i in items] == [free.item_id]
        finally:
            db.commit()

    def test_respects_limit_and_newest_first(self, db_session):
        db = db_session
        try:
            account = self._account(db, 1)
            self._product(db, account_id=account.id, item_id="a", create_ms=100)
            self._product(db, account_id=account.id, item_id="b", create_ms=300)
            self._product(db, account_id=account.id, item_id="c", create_ms=200)
            db.commit()
            items = svc.select_business_items(db, account_id=account.id, limit=2)
            assert [i.item_id for i in items] == ["b", "c"]  # 创建时间倒序
        finally:
            db.commit()

    def test_does_not_leak_other_accounts(self, db_session):
        db = db_session
        try:
            mine = self._account(db, 1)
            theirs = self._account(db, 2)
            self._product(db, account_id=theirs.id, item_id="theirs-1")
            db.commit()
            assert svc.select_business_items(db, account_id=mine.id, limit=10) == []
        finally:
            db.commit()


# --------------------------------------------------------------------------- #
# 4) 业务链路（逐账号：探活 → 生成 → 发布）
# --------------------------------------------------------------------------- #
class TestRunFlow:
    """用**打桩**的生成 / 发布链路驱动 `run_business_note_auto_publish`。

    真生成 / 真发布要打平台（本机不可代验，且会真发出去）⇒ 这里只验**编排**：
    谁被跳过、跳过时有没有通知、生成失败的有没有被发布、间隔有没有起作用。
    """

    @staticmethod
    def _seed_account(db, *, user_id: int = 1, nickname: str = "测试店") -> PlatformAccount:
        if db.get(User, int(user_id)) is None:
            db.add(User(id=int(user_id), username=f"biz-flow-{user_id}", password_hash="x"))
            db.flush()
        account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="qianfan", nickname=nickname, status="active"
        )
        db.add(account)
        db.flush()
        return account

    @staticmethod
    def _seed_product(db, *, account_id: int, item_id: str) -> ShopProduct:
        row = ShopProduct(
            user_id=1,
            platform_account_id=account_id,
            item_id=item_id,
            item_title=f"商品 {item_id}",
            create_time_ms=1000,
        )
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def _seed_note(db, *, pk: int, account_id: int, user_id: int = 1) -> Note:
        """一篇**待发布**的本地生成笔记（发布门禁 / 终态回读都要它真在库里）。"""
        note = Note(
            id=int(pk),
            user_id=int(user_id),
            platform_account_id=int(account_id),
            platform="xhs",
            note_id=f"local_biz_{pk}",
            title=f"笔记 {pk}",
            content="正文",
            author_name="",
            note_type=NOTE_TYPE_LOCAL_GENERATED,
        )
        set_note_publish_status(note, PUBLISH_STATUS_PENDING)
        db.add(note)
        db.flush()
        return note

    def _patch(self, monkeypatch, *, healthy=True, generate_ok=True, publish_ok=True):
        """打桩：探活 / 生成两路 / 发布，各自记账调用次数。"""
        calls = {"health": [], "generate": [], "publish": [], "sleep": [], "notify": []}
        from backend.app.services import (
            note_gen_llm_worker,
            note_publish_service,
            notification_service,
            product_note_gen_service,
            shop_health_service,
        )
        from backend.app.services import scheduler_service as ss

        def fake_health(db, account, *a, **kw):
            calls["health"].append(int(account.id))
            return {"status": "active" if healthy else "invalid", "message": "" if healthy else "千帆登录态已失效"}

        def fake_gen(db, user_id, account, item_id, **kw):
            calls["generate"].append((int(account.id), str(item_id)))
            return {"ok": True, "status": "queued", "item_id": item_id}

        def fake_wait_gen(timeout=0):
            return True

        def fake_llm_gen(db, user_id, *, shop_product_id, **kw):
            calls["generate"].append((int(user_id), int(shop_product_id)))
            return {"ok": True, "status": "queued"}

        def fake_start_publish(db, user_id, note_pk, **kw):
            calls["publish"].append(int(note_pk))
            # 真 worker 会在跑完后把状态写成 published ⇒ 替身也照做（否则「终态回读」
            # 这段判据在这条用例里恒不成立 = 没验到）。
            note = db.get(Note, int(note_pk))
            if note is not None and publish_ok:
                set_note_publish_status(note, PUBLISH_STATUS_PUBLISHED)
                db.flush()
            return None

        def fake_wait_publish(note_pk, timeout=0):
            return True

        def fake_notify(db, *, user_id, account_id, account_name, reason):
            calls["notify"].append((int(account_id), str(reason)))
            return None

        monkeypatch.setattr(shop_health_service, "check_account_health", fake_health)
        monkeypatch.setattr(product_note_gen_service, "start_generate_note", fake_gen)
        monkeypatch.setattr(product_note_gen_service, "wait_generate_done", fake_wait_gen)
        monkeypatch.setattr(note_gen_llm_worker, "start_llm_generate_note", fake_llm_gen)
        monkeypatch.setattr(note_gen_llm_worker, "wait_llm_generate_done", fake_wait_gen)
        monkeypatch.setattr(note_publish_service, "start_publish", fake_start_publish)
        monkeypatch.setattr(note_publish_service, "wait_publish_done", fake_wait_publish)
        monkeypatch.setattr(notification_service, "notify_account_skipped", fake_notify)
        monkeypatch.setattr(ss.time, "sleep", lambda seconds: calls["sleep"].append(seconds))
        return calls

    def _wire_note_pk(self, monkeypatch, note_pk):
        """把「刚生成的笔记主键」固定成一个值（真链路的取法见 `_note_pk_for_item`）。"""
        monkeypatch.setattr(
            svc, "_note_pk_for_item", lambda db, **kw: (None if note_pk is None else int(note_pk))
        )

    def test_invalid_account_is_skipped_and_notified(self, db_session, monkeypatch):
        """用户口径「校验账号千帆是否有效，无效的跳过，推送站内消息进行提醒」。"""
        _override = __import__("tests.backend.conftest", fromlist=["x"]).__dict__
        db = db_session
        try:
            account = self._seed_account(db)
            self._seed_product(db, account_id=account.id, item_id="p1")
            db.commit()
        finally:
            db.commit()
        calls = self._patch(monkeypatch, healthy=False)
        monkeypatch.setattr(svc, "SessionLocal", _session_factory_of(db_session))
        out = svc.run_business_note_auto_publish(1, account_ids=[account.id], limit=1)
        assert calls["health"] == [account.id]
        assert calls["notify"] == [(account.id, "千帆登录态已失效")], "无效必须发站内消息"
        assert calls["generate"] == [] and calls["publish"] == [], "无效账号不许生成 / 发布"
        assert out["skipped"] == 1 and out["published"] == 0

    def test_generate_failure_is_not_published(self, db_session, monkeypatch):
        """用户口径「对**生成成功**的笔记进行发布」。"""
        db = db_session
        try:
            account = self._seed_account(db)
            self._seed_product(db, account_id=account.id, item_id="p1")
            db.commit()
        finally:
            db.commit()
        calls = self._patch(monkeypatch, healthy=True)
        monkeypatch.setattr(svc, "SessionLocal", _session_factory_of(db_session))
        self._wire_note_pk(monkeypatch, None)  # 生成失败 / 取不到笔记
        out = svc.run_business_note_auto_publish(1, account_ids=[account.id], limit=1)
        assert calls["generate"], "应该试过生成"
        assert calls["publish"] == [], "生成失败**不得**发布"
        assert out["published"] == 0

    def test_success_path_publishes_and_uses_interval(self, db_session, monkeypatch):
        """生成成功 ⇒ 发布；两篇之间按 `interval_seconds` 间隔。"""
        db = db_session
        try:
            account = self._seed_account(db)
            self._seed_product(db, account_id=account.id, item_id="p1")
            self._seed_product(db, account_id=account.id, item_id="p2")
            self._seed_note(db, pk=42, account_id=account.id)
            db.commit()
        finally:
            db.commit()
        calls = self._patch(monkeypatch, healthy=True)
        monkeypatch.setattr(svc, "SessionLocal", _session_factory_of(db_session))
        self._wire_note_pk(monkeypatch, 42)
        out = svc.run_business_note_auto_publish(
            1, account_ids=[account.id], limit=2, interval_seconds=7
        )
        assert len(calls["generate"]) == 2
        assert calls["publish"] == [42, 42]
        assert calls["sleep"] == [7, 7], "每篇发布后都要按间隔等（用户口径「时间间隔单位秒」）"
        assert out["published"] == 2

    def test_limit_zero_sleep_is_not_called(self, db_session, monkeypatch):
        """`interval_seconds = 0` ⇒ 不 sleep（用户可能就是想连着发）。"""
        db = db_session
        account = self._seed_account(db)
        self._seed_product(db, account_id=account.id, item_id="p1")
        self._seed_note(db, pk=1, account_id=account.id)
        db.commit()
        calls = self._patch(monkeypatch, healthy=True)
        monkeypatch.setattr(svc, "SessionLocal", _session_factory_of(db_session))
        self._wire_note_pk(monkeypatch, 1)
        svc.run_business_note_auto_publish(1, account_ids=[account.id], limit=1, interval_seconds=0)
        assert calls["sleep"] == []

    def test_llm_mode_routes_to_llm_worker(self, db_session, monkeypatch):
        """`generate_mode=llm` ⇒ 走 LLM 链路（不是千帆三步链路）。"""
        db = db_session
        account = self._seed_account(db)
        self._seed_product(db, account_id=account.id, item_id="p1")
        self._seed_note(db, pk=7, account_id=account.id)
        db.commit()
        calls = self._patch(monkeypatch, healthy=True)
        monkeypatch.setattr(svc, "SessionLocal", _session_factory_of(db_session))
        self._wire_note_pk(monkeypatch, 7)
        from backend.app.services import note_gen_llm_worker

        llm_calls = {"n": 0}
        original = note_gen_llm_worker.start_llm_generate_note

        def counted(*a, **kw):
            llm_calls["n"] += 1
            return original(*a, **kw)

        monkeypatch.setattr(note_gen_llm_worker, "start_llm_generate_note", counted)
        svc.run_business_note_auto_publish(
            1, account_ids=[account.id], limit=1, generate_mode="llm"
        )
        assert llm_calls["n"] == 1, "llm 模式必须走 LLM worker"

    def test_rotating_mode_returns_none_and_only_own_accounts(self, db_session, monkeypatch):
        """轮转模式（不点名账号）返 `None`；且**只跑行主本人的账号**（DEF-066）。"""
        db = db_session
        try:
            mine = self._seed_account(db, user_id=1, nickname="我的店")
            theirs = self._seed_account(db, user_id=2, nickname="别人的店")
            self._seed_product(db, account_id=theirs.id, item_id="theirs-1")
            db.commit()
            mine_id = mine.id
        finally:
            db.commit()
        calls = self._patch(monkeypatch, healthy=True)
        monkeypatch.setattr(svc, "SessionLocal", _session_factory_of(db_session))
        out = svc.run_business_note_auto_publish(1)  # 轮转：account_ids 缺省
        assert out is None, "轮转模式与同族一致返 None"
        assert calls["health"] == [mine_id], "只碰自己的账号（DEF-066）"


# --------------------------------------------------------------------------- #
# 5) 发布失败重试（2026-10-07 用户口径三件套）
# --------------------------------------------------------------------------- #
class TestRetryPolicy:
    """「发布失败是否重试、重试次数最多 3 次、重试间隔时间」。

    三条参数键：`retry_on_failure` / `max_retries` / `retry_interval_seconds`。
    这里的判据分两层：① **解析层**（缺省 / 夹回 / 硬顶 / 宽松布尔）；
    ② **编排层**（真按策略重试、且只重试发布不重试生成）。
    """

    def test_defaults(self):
        assert svc.resolve_retry_on_failure(None) is True, "缺省 = 重试"
        assert svc.resolve_max_retries(None) == 3
        assert svc.resolve_retry_interval_seconds(None) == svc.DEFAULT_RETRY_INTERVAL_SECONDS

    def test_max_retries_is_hard_capped_at_three(self):
        """**用户口径「最多 3 次」是硬顶** —— 传 99 也只重试 3 次（不是防御性夹回那么简单）。"""
        assert svc.resolve_max_retries({"max_retries": 99}) == 3
        assert svc.resolve_max_retries({"max_retries": "99"}) == 3
        assert svc.resolve_max_retries({"max_retries": 1}) == 1
        assert svc.resolve_max_retries({"max_retries": 0}) == 0, "0 = 不重试（等于关掉）"
        assert svc.resolve_max_retries({"max_retries": -5}) == 0

    def test_retry_on_failure_accepts_loose_words(self):
        for word in ("true", "True", "1", "yes", "on", "是"):
            assert svc.resolve_retry_on_failure({"retry_on_failure": word}) is True, word
        for word in ("false", "False", "0", "no", "off", "否"):
            assert svc.resolve_retry_on_failure({"retry_on_failure": word}) is False, word
        # 真布尔（前端 JSON 会传 bool）必须原样透传
        assert svc.resolve_retry_on_failure({"retry_on_failure": False}) is False
        # 认不出来的值 ⇒ **回落默认而不是报错**（与 generate_mode 的取舍刻意不同：
        # 最坏后果只是「多试一次」，不产生错误结果）
        assert svc.resolve_retry_on_failure({"retry_on_failure": "maybe"}) is True

    def test_retry_interval_defaults_and_is_clamped(self):
        assert svc.resolve_retry_interval_seconds({"retry_interval_seconds": 5}) == 5
        assert svc.resolve_retry_interval_seconds({"retry_interval_seconds": 0}) == 0
        assert svc.resolve_retry_interval_seconds({"retry_interval_seconds": 999999}) == svc.MAX_INTERVAL_SECONDS
        assert svc.resolve_retry_interval_seconds({"retry_interval_seconds": "abc"}) == svc.DEFAULT_RETRY_INTERVAL_SECONDS

    def test_validator_covers_the_retry_keys(self):
        """建行校验走**同一把尺子**（DEF-044）—— 重试三件也必须在里面。"""
        src = (Path(__file__).resolve().parents[2] / "backend/app/services/business_note_publish_service.py").read_text(encoding="utf-8")
        body = src[src.index("def validate_business_publish_params") :]
        body = body[: body.index("\ndef ", 10)]
        assert "resolve_retry_policy(params)" in body, "校验器必须覆盖重试三件"

    def test_frontend_enum_values_match_backend(self):
        """前端下拉里选得出来的值，后端必须认（跨语言同值 —— 别只改一侧）。"""
        editor = (
            Path(__file__).resolve().parents[2] / "frontend/src/components/schedule/params-editor.tsx"
        ).read_text(encoding="utf-8")
        assert "retry_on_failure: { true:" in editor
        for token in ('true', 'false'):
            assert svc._coerce_bool(token, default=None) is (token == "true")


class TestRetryFlow:
    """编排层：真按策略重试、且**只重试发布不重试生成**。"""

    _seed_account = staticmethod(lambda db, **kw: TestRunFlow._seed_account(db, **kw))
    _seed_product = staticmethod(lambda db, **kw: TestRunFlow._seed_product(db, **kw))
    _seed_note = staticmethod(lambda db, **kw: TestRunFlow._seed_note(db, **kw))
    _wire_note_pk = TestRunFlow._wire_note_pk

    @staticmethod
    def _patch_first_attempts_fail(monkeypatch, *, fail_times: int, **kw):
        """前 `fail_times` 次发布失败、之后成功（用真 `_patch` 再包一层计数）。"""
        calls = TestRunFlow()._patch(monkeypatch, **kw)
        from backend.app.services import note_publish_service

        state = {"n": 0}

        def flaky(db, user_id, note_pk, **kwargs):
            """前 `fail_times` 次置失败态，之后置成功态（真终态回读才判得出来）。"""
            state["n"] += 1
            calls["publish"].append(int(note_pk))
            note = db.get(Note, int(note_pk))
            if note is not None:
                if state["n"] > fail_times:
                    set_note_publish_status(note, PUBLISH_STATUS_PUBLISHED)
                    note.publish_error = ""
                else:
                    set_note_publish_status(note, PUBLISH_STATUS_FAILED)
                    note.publish_error = "平台风控"
                db.flush()
            return None

        monkeypatch.setattr(note_publish_service, "start_publish", flaky)
        return calls, state

    def test_failure_is_retried_up_to_three_times(self, db_session, monkeypatch):
        """用户口径「发布失败重试，重试次数最多 3 次」—— 连失败 3 次共打 4 次（首发 + 3 重试）。"""
        db = db_session
        account = self._seed_account(db)
        self._seed_product(db, account_id=account.id, item_id="p1")
        self._seed_note(db, pk=42, account_id=account.id)
        db.commit()
        calls, _ = self._patch_first_attempts_fail(monkeypatch, fail_times=99, healthy=True)
        monkeypatch.setattr(svc, "SessionLocal", _session_factory_of(db_session))
        self._wire_note_pk(monkeypatch, 42)
        out = svc.run_business_note_auto_publish(
            1, account_ids=[account.id], limit=1, retry_interval_seconds=0, interval_seconds=0
        )
        assert calls["publish"] == [42, 42, 42, 42], "1 次首发 + 最多 3 次重试"
        assert out["retried"] == 3
        assert out["retry"] == {"enabled": True, "max_retries": 3, "interval_seconds": 0}
        assert out["published"] == 0, "全失败 ⇒ 不计入已发布"

    def test_retry_stops_as_soon_as_it_succeeds(self, db_session, monkeypatch):
        """第 2 次尝试就成功 ⇒ 不再往后试（重试是「补到成功为止」，不是「固定打满」）。"""
        db = db_session
        account = self._seed_account(db)
        self._seed_product(db, account_id=account.id, item_id="p1")
        self._seed_note(db, pk=42, account_id=account.id)
        db.commit()
        calls, _ = self._patch_first_attempts_fail(monkeypatch, fail_times=1, healthy=True)
        monkeypatch.setattr(svc, "SessionLocal", _session_factory_of(db_session))
        self._wire_note_pk(monkeypatch, 42)
        out = svc.run_business_note_auto_publish(
            1, account_ids=[account.id], limit=1, retry_interval_seconds=0, interval_seconds=0
        )
        assert calls["publish"] == [42, 42], "首发失败 + 1 次重试成功 ⇒ 停"
        assert out["published"] == 1 and out["retried"] == 1

    def test_retry_uses_its_own_interval(self, db_session, monkeypatch):
        """重试间隔用 `retry_interval_seconds`，**不复用** `interval_seconds`。"""
        db = db_session
        account = self._seed_account(db)
        self._seed_product(db, account_id=account.id, item_id="p1")
        self._seed_note(db, pk=42, account_id=account.id)
        db.commit()
        calls, _ = self._patch_first_attempts_fail(monkeypatch, fail_times=99, healthy=True)
        monkeypatch.setattr(svc, "SessionLocal", _session_factory_of(db_session))
        self._wire_note_pk(monkeypatch, 42)
        svc.run_business_note_auto_publish(
            1,
            account_ids=[account.id],
            limit=1,
            retry_interval_seconds=9,
            interval_seconds=3,
        )
        # 首发失败 ⇒ 3 次重试，每次前各等 9s（3 个重试间隔）；篇末再等一次 3s（发布后间隔）
        assert calls["sleep"] == [9, 9, 9, 3], calls["sleep"]

    def test_retry_disabled_means_single_attempt(self, db_session, monkeypatch):
        """`retry_on_failure=false` ⇒ 只打一次（用户口径的开关生效）。"""
        db = db_session
        account = self._seed_account(db)
        self._seed_product(db, account_id=account.id, item_id="p1")
        self._seed_note(db, pk=42, account_id=account.id)
        db.commit()
        calls, _ = self._patch_first_attempts_fail(monkeypatch, fail_times=99, healthy=True)
        monkeypatch.setattr(svc, "SessionLocal", _session_factory_of(db_session))
        self._wire_note_pk(monkeypatch, 42)
        out = svc.run_business_note_auto_publish(
            1,
            account_ids=[account.id],
            limit=1,
            retry_on_failure=False,
            interval_seconds=0,
        )
        assert calls["publish"] == [42], "关掉重试后只打一次"
        assert out["retried"] == 0

    def test_max_retries_zero_means_single_attempt(self, db_session, monkeypatch):
        db = db_session
        account = self._seed_account(db)
        self._seed_product(db, account_id=account.id, item_id="p1")
        self._seed_note(db, pk=42, account_id=account.id)
        db.commit()
        calls, _ = self._patch_first_attempts_fail(monkeypatch, fail_times=99, healthy=True)
        monkeypatch.setattr(svc, "SessionLocal", _session_factory_of(db_session))
        self._wire_note_pk(monkeypatch, 42)
        svc.run_business_note_auto_publish(
            1, account_ids=[account.id], limit=1, max_retries=0, interval_seconds=0
        )
        assert calls["publish"] == [42]

    def test_generation_failure_is_not_retried(self, db_session, monkeypatch):
        """**只重试发布，不重试生成** —— 生成属另一条链路，重试会再打一次平台生成接口。"""
        db = db_session
        account = self._seed_account(db)
        self._seed_product(db, account_id=account.id, item_id="p1")
        db.commit()
        calls = TestRunFlow()._patch(monkeypatch, healthy=True)
        monkeypatch.setattr(svc, "SessionLocal", _session_factory_of(db_session))
        self._wire_note_pk(monkeypatch, None)  # 生成失败
        out = svc.run_business_note_auto_publish(
            1, account_ids=[account.id], limit=1, retry_interval_seconds=0, interval_seconds=0
        )
        assert len(calls["generate"]) == 1, "生成失败不得重试"
        assert calls["publish"] == []
        assert out["retried"] == 0

