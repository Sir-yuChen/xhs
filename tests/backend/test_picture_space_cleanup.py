"""图片空间「清理未引用图片」定时任务（2026-10-11 用户需求）。

被守护的对象 = `backend/app/services/picture_space_cleanup_service.py` +
注册表里的 `system.picture_space_cleanup` 项 + 前端 `delete_mode` 枚举。

## 用户口径（逐字，本文件全部判据的来源）

> 图片空间新增清理未图片定时任务，每周一执行一次
> 定时任务调用参数，执行账号默认全部；删除到回收站或彻底删除下拉单选二选一，默认彻底删除
> 定时任务逻辑：根据入参执行账号使用对应的千帆登录会话，未传默认全部小红书账号循环一下逻辑，
> 调用图片空间的翻页查询每页 500 条，每查询一页，循环将未应用的图片放单独数组中，
> 每一百条调用一次批量删除接口，按照每页循环上面的逻辑。
> 所有页查询循环执行完毕后。当调用参数为彻底删除时再执行查询图片回收站列表依旧每页 500 条，
> 每查询一页再分一百条一次调用批量彻底删除图片接口，循环当前页的数据，执行完再查询下一页，
> 直到循环执行到最后一页彻底移除回收站内的所有图片

## 为什么用**假 adapter** 而不是打桩 `search_material_space`

这一条链路的错法绝大多数在**循环结构**上（页大小、批大小、余数、两段顺序、筛选条件），
不在单次调用。假 adapter 让「平台侧看到了什么请求」可断言（调用序列 + 请求体），
这是 `monkeypatch` 单个方法做不到的。
"""

from __future__ import annotations

import pytest

from backend.app.services import picture_space_cleanup_service as svc
from backend.app.services import picture_space_service as ps


# --------------------------------------------------------------------------- #
# 假平台
# --------------------------------------------------------------------------- #
def _file(mid: str, *, product_count: int = 0) -> dict:
    """平台原始条目（`normalize_material_item` 的入参形态）。"""
    return {
        "id": mid,
        "type": ps.MATERIAL_TYPE_FILE,
        "name": mid,
        "size": 100,
        "createTime": 1,
        "updateTime": 2,
        "additionalData": {
            "extension": ".jpg",
            "width": "100",
            "height": "100",
            "link": f"https://cdn/{mid}",
            "productCount": str(product_count),
        },
    }


def _folder(mid: str) -> dict:
    """文件夹条目 —— `productCount` 恒 0，但**绝不能被当成未引用图片删掉**。"""
    return {
        "id": mid,
        "type": ps.MATERIAL_TYPE_FOLDER,
        "name": mid,
        "size": 0,
        "additionalData": {"level": "0", "productCount": "0", "isLeaf": "true"},
    }


class FakeAdapter:
    """按「库」返回分页结果的假千帆适配器。

    * `library` / `recycle` 是两个独立的条目池（正常视图 / 回收站视图）；
    * 每次 `search_material_space` 按请求体的 `pageIndex` / `pageSize` / `statuses`
      切一页 —— **所以「每页 500」是拿请求体断言的，不是我方自说自话**；
    * 删除调用**真的从池子里摘条目**（进回收站 = 从 library 挪到 recycle），
      于是「第二段能不能删到东西」取决于第一段真的挪了。
    """

    def __init__(
        self,
        library: list[dict] | None = None,
        recycle: list[dict] | None = None,
    ) -> None:
        self.library = list(library or [])
        self.recycle = list(recycle or [])
        #: 每次查询的请求体（断言页大小 / statuses 用）。
        self.search_bodies: list[dict] = []
        #: 每次批量删除的「进回收站」批次（每批一个 list）。
        self.delete_batches: list[list[dict]] = []
        #: 每次彻底删除的批次（每批一个 list）。
        self.purge_batches: list[list[dict]] = []
        self.fail_delete = False
        self.fail_purge = False

    def search_material_space(self, body: dict):
        self.search_bodies.append(body)
        page = int(body.get("pageIndex") or 1)
        size = int(body.get("pageSize") or 100)
        statuses = (body.get("filter") or {}).get("statuses") or []
        pool = self.recycle if ps.MATERIAL_STATUS_RECYCLE_BIN in statuses else self.library
        start = (page - 1) * size
        chunk = pool[start : start + size]
        return True, "", {"data": {"total": len(pool), "directoryManageables": chunk}}

    def edit_material_manageables(self, seller_id: str, items: list[dict]):
        self.delete_batches.append(list(items))
        if self.fail_delete:
            return False, "平台拒绝", None
        ids = {str(it.get("id")) for it in items}
        moved = [x for x in self.library if str(x.get("id")) in ids]
        self.library = [x for x in self.library if str(x.get("id")) not in ids]
        self.recycle.extend(moved)
        return True, "", None

    def delete_material_manageables(self, seller_id: str, elements: list[dict]):
        self.purge_batches.append(list(elements))
        if self.fail_purge:
            return False, "平台拒绝", None
        ids = {str(e.get("id")) for e in elements}
        self.recycle = [x for x in self.recycle if str(x.get("id")) not in ids]
        return True, "", None


class FakeAccount:
    """只带 `cleanup_account` 真正读的三个字段。"""

    def __init__(self, account_id: int = 8, seller_id: str = "seller-1") -> None:
        self.id = account_id
        self.seller_id = seller_id
        self.nickname = f"账号{account_id}"


@pytest.fixture(autouse=True)
def _no_real_rate_limit(monkeypatch):
    """限流器放行 + **不真等**（`wait_allow` 的轮询默认 1 秒，真等会把用例拖成分钟级）。"""
    limiter = svc.get_rate_limiter()

    class _Limiter:
        def wait_allow(self, key, timeout=None):  # noqa: ANN001, D401
            return True

        def allow(self, key):  # noqa: ANN001
            return True

    monkeypatch.setattr(svc, "get_rate_limiter", lambda: _Limiter())
    monkeypatch.setattr(limiter, "wait_allow", lambda key, timeout=None: True)


@pytest.fixture(autouse=True)
def _ark_session_present(monkeypatch):
    """默认「该账号有 ark 会话」；没有会话的分支在专门用例里单独改。"""
    from backend.app.services import shop_binding_service as sbs

    monkeypatch.setattr(sbs, "has_ark_session", lambda db, account_id: True)
    monkeypatch.setattr(sbs, "qianfan_cookies_for_account", lambda db, account_id: "c=1")


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
class TestPureHelpers:
    def test_delete_mode_default_is_permanent(self):
        """用户口径「默认彻底删除」—— 缺值、没传、空串一律落 `permanent`。"""
        assert svc.DEFAULT_DELETE_MODE == svc.MODE_PERMANENT
        for value in (None, "", "  ", "unknown", "RECYCLE2"):
            assert svc.coerce_delete_mode(value) == svc.MODE_PERMANENT, value

    def test_delete_mode_accepts_both_whitelisted_values(self):
        """两种合法取值都要认（大小写 / 空白宽容 —— 用户在下拉里选的，不该因空格 400）。"""
        assert svc.coerce_delete_mode("recycle") == svc.MODE_RECYCLE
        assert svc.coerce_delete_mode(" PERMANENT ") == svc.MODE_PERMANENT

    def test_batch_size_and_page_size_match_the_user_spec(self):
        """用户口径逐字：「每页 500 条」「每一百条调用一次批量删除接口」。"""
        assert svc.PAGE_SIZE == 500
        assert svc.DELETE_BATCH_SIZE == 100

    def test_unreferenced_requires_a_file_with_zero_references(self):
        """判据三件：是文件 + 有 id + `referenced` 为假。"""
        assert svc.is_unreferenced(ps.normalize_material_item(_file("a")))
        assert not svc.is_unreferenced(ps.normalize_material_item(_file("a", product_count=2)))
        #: ⚠️ 文件夹**永远不算**未引用 —— 它没有「引用」概念，productCount 恒 0；
        #: 不排除的话一次清理会把用户的整个文件夹树删光。
        assert not svc.is_unreferenced(ps.normalize_material_item(_folder("d1")))
        assert not svc.is_unreferenced({"type": ps.MATERIAL_TYPE_FILE, "id": ""})
        assert not svc.is_unreferenced({})  # type: ignore[arg-type]

    def test_chunks_splits_by_hundred(self):
        items = [{"id": str(i)} for i in range(250)]
        parts = svc._chunks(items, svc.DELETE_BATCH_SIZE)
        assert [len(p) for p in parts] == [100, 100, 50]

    def test_validator_rejects_unknown_mode_but_allows_missing(self):
        """API 边界：坏值 400（抛 `ValueError`），缺值放行（运行期兜底成默认）。"""
        svc.validate_cleanup_params({})  # 缺值 ⇒ 不报
        svc.validate_cleanup_params({"delete_mode": ""})  # 空 ⇒ 不报
        svc.validate_cleanup_params({"delete_mode": "permanent"})  # 合法 ⇒ 不报
        with pytest.raises(ValueError):
            svc.validate_cleanup_params({"delete_mode": "nuke"})

    def test_search_body_shapes_for_both_segments(self):
        """两段的查询体：正常视图 = statuses[1]；回收站 = statuses[4] + 按删除时间倒序。"""
        normal = svc._search_body(page=3, recycle=False)
        assert normal["filter"]["statuses"] == [ps.MATERIAL_STATUS_NORMAL]
        assert normal["filter"]["basicTypes"] == [ps.MATERIAL_TYPE_FILE]
        assert normal["pageIndex"] == 3
        assert normal["pageSize"] == 500

        trash = svc._search_body(page=1, recycle=True)
        assert trash["filter"]["statuses"] == [ps.MATERIAL_STATUS_RECYCLE_BIN]
        assert trash["order"]["orderList"] == [{"field": "updateTime", "asc": False}]


# --------------------------------------------------------------------------- #
# 单账号执行
# --------------------------------------------------------------------------- #
class TestCleanupAccount:
    def test_permanent_mode_moves_then_purges(self):
        """完整链路：未引用 ⇒ 进回收站 ⇒ 回收站里那批 ⇒ 彻底删除。

        这是用户口径的主链路，一次把三段都钉住。
        """
        adapter = FakeAdapter(library=[_file("a"), _file("b", product_count=1), _file("c")])
        out = svc.cleanup_account(
            None, FakeAccount(), delete_mode=svc.MODE_PERMANENT, adapter=adapter
        )

        assert out["status"] == "done"
        assert out["unreferenced"] == 2 and out["moved"] == 2
        assert out["purged"] == 2
        assert out["failed"] == 0
        #: 已引用的那张**一张都没动**（红线：删掉它会让在售商品的图裂开）。
        assert [x["id"] for x in adapter.library] == ["b"]
        assert adapter.recycle == [], "第二段应把回收站里的都彻底删掉"

    def test_recycle_mode_stops_after_first_segment(self):
        """用户口径「当调用参数为彻底删除时**再**执行查询图片回收站列表」⇒
        非彻底删除模式**不得**打第二个端点。"""
        adapter = FakeAdapter(library=[_file("a"), _file("b")])
        out = svc.cleanup_account(
            None, FakeAccount(), delete_mode=svc.MODE_RECYCLE, adapter=adapter
        )

        assert out["moved"] == 2
        assert out["purged"] == 0
        assert adapter.purge_batches == [], "recycle 模式不该调彻底删除"
        assert len(adapter.recycle) == 2, "只应停进回收站"
        assert adapter.library == []

    def test_batches_are_cut_at_hundred(self):
        """用户口径「每一百条调用一次批量删除接口」—— 250 张 = 100 / 100 / 50 三批。"""
        adapter = FakeAdapter(library=[_file(f"f{i:03d}") for i in range(250)])
        out = svc.cleanup_account(
            None, FakeAccount(), delete_mode=svc.MODE_RECYCLE, adapter=adapter
        )

        assert out["moved"] == 250
        assert [len(b) for b in adapter.delete_batches] == [100, 100, 50]

    def test_tail_batch_is_cleaned(self):
        """**余数必删**：<100 的尾巴最容易漏（漏了就是每次固定残留几张、且永远清不掉）。"""
        adapter = FakeAdapter(library=[_file(f"f{i}") for i in range(7)])
        out = svc.cleanup_account(
            None, FakeAccount(), delete_mode=svc.MODE_RECYCLE, adapter=adapter
        )
        assert out["moved"] == 7
        assert [len(b) for b in adapter.delete_batches] == [7]

    def test_pages_are_requested_at_five_hundred(self):
        """用户口径「翻页查询每页 500 条」，且**翻到平台没有下一页为止**（不设页数上限）。

        ⚠️ 因为每页都在删（删了就不前进页号），1200 条会切成 3 页（500+500+200），
        页号序列是 `1,1,1`（池子一直在缩短）—— 断言的是**请求次数与页大小**，
        不是「页号递增」（那正是本实现刻意不做的）。
        """
        adapter = FakeAdapter(library=[_file(f"f{i:04d}") for i in range(1200)])
        out = svc.cleanup_account(
            None, FakeAccount(), delete_mode=svc.MODE_RECYCLE, adapter=adapter
        )

        normal = [
            b for b in adapter.search_bodies
            if b["filter"]["statuses"] == [ps.MATERIAL_STATUS_NORMAL]
        ]
        assert all(b["pageSize"] == 500 for b in normal), "每页必须是 500"
        #: 1200 = 500 + 500 + 200 ⇒ 三页取完（最后一页不足 500 后池子空 ⇒ 再取一次空页即停）。
        assert len(normal) == 4, f"应按 500 取到空为止，实际取了 {len(normal)} 次"
        assert out["moved"] == 1200, "**一行都不能漏**（删后不前进页号就是为了这个）"

    def test_deleting_does_not_skip_the_tail_of_each_page(self):
        """**删后仍停在原页号**：否则每删一页，平台池子缩短会静默跳过等量的条目。

        这是一条**回归守卫**：首版实现是「每页删完就 `page += 1`」，实测 1200 条只删掉
        700 条（500 条被跳过）——因为池子缩短后 `pageIndex=2` 已经不再覆盖原来的
        第 501~1000 条了。判据用「一条不漏」而不是「调了几次接口」，那才是用户关心的。
        """
        adapter = FakeAdapter(library=[_file(f"f{i:04d}") for i in range(1000)])
        out = svc.cleanup_account(
            None, FakeAccount(), delete_mode=svc.MODE_RECYCLE, adapter=adapter
        )
        assert out["moved"] == 1000
        assert adapter.library == [], "池子里不该残留任何未引用图片"

    def test_directories_are_never_deleted(self):
        """文件夹 `productCount` 恒 0 ⇒ 不加 `type` 判据就会把它们删光。"""
        adapter = FakeAdapter(library=[_folder("d1"), _file("a")])
        out = svc.cleanup_account(
            None, FakeAccount(), delete_mode=svc.MODE_RECYCLE, adapter=adapter
        )
        assert out["moved"] == 1
        assert [x["id"] for x in adapter.library] == ["d1"], "文件夹必须原样留着"

    def test_platform_failure_is_counted_not_hidden(self):
        """平台拒绝时**按整批计 `failed`**，不拆成「逐条猜」，且状态降级为 `partial`。"""
        adapter = FakeAdapter(library=[_file(f"f{i}") for i in range(5)])
        adapter.fail_delete = True
        out = svc.cleanup_account(
            None, FakeAccount(), delete_mode=svc.MODE_RECYCLE, adapter=adapter
        )
        assert out["failed"] == 5 and out["moved"] == 0
        assert out["status"] == "partial"

    def test_without_ark_session_it_fails_structurally(self, monkeypatch):
        """用户口径「使用对应的千帆登录会话」—— 拿不到就**如实报**，不借别的账号（DEF-051）。"""
        from backend.app.services import shop_binding_service as sbs

        monkeypatch.setattr(sbs, "has_ark_session", lambda db, account_id: False)
        out = svc.cleanup_account(None, FakeAccount(), delete_mode=svc.MODE_PERMANENT)

        assert out["status"] == "failed"
        assert "千帆" in out["error"]
        assert out["moved"] == 0 and out["purged"] == 0

    def test_recycle_view_is_paged_at_five_hundred_too(self):
        """用户口径「依旧每页 500 条」；第二段是**独立的一段翻页**，不是只在第一页删。

        ⚠️ 同第一段：回收站里也在删 ⇒ 页号不递增（停在 1 直到池子扫空）。
        判据落在「页大小 + 一条不漏」上。
        """
        trash = [_file(f"t{i:04d}") for i in range(700)]
        adapter = FakeAdapter(library=[], recycle=trash)
        out = svc.cleanup_account(
            None, FakeAccount(), delete_mode=svc.MODE_PERMANENT, adapter=adapter
        )

        recycle_bodies = [
            b for b in adapter.search_bodies
            if b["filter"]["statuses"] == [ps.MATERIAL_STATUS_RECYCLE_BIN]
        ]
        assert all(b["pageSize"] == 500 for b in recycle_bodies)
        assert len(recycle_bodies) == 3, f"700 条按 500 取到空 ⇒ 3 次，实际 {len(recycle_bodies)}"
        assert out["purged"] == 700, "回收站里的**一条都不能剩**"
        assert adapter.recycle == []


# --------------------------------------------------------------------------- #
# 调度入口（多账号）
# --------------------------------------------------------------------------- #
class TestRunEntrypoint:
    """调度入口的多账号行为（`user_id` 收口 / 定向 / 单账号失败不拖垮整轮）。

    ⚠️ 建账号行前必须**先落一个真实用户**：`platform_accounts.user_id` 有外键，
    凭空 id 会撞 `FOREIGN KEY constraint failed`（不是本模块的问题，是 SQLite
    `PRAGMA foreign_keys=ON` 在起作用）。
    """

    @staticmethod
    def _seed(db, *accounts) -> None:
        from backend.app.models.user import User
        from backend.app.models.platform_account import PlatformAccount

        db.add(User(id=1, username="owner", password_hash="x"))
        db.add(User(id=2, username="other", password_hash="x"))
        db.flush()
        for account_id, user_id in accounts:
            db.add(
                PlatformAccount(
                    id=account_id, user_id=user_id, platform="xhs", nickname=f"账号{account_id}"
                )
            )
        db.commit()

    def test_scopes_accounts_to_the_row_owner(self, db_factory, monkeypatch):
        """DEF-066：账号查询恒带 `user_id` —— 别人的账号一行都不许碰。"""
        db = db_factory()
        self._seed(db, (1, 1), (2, 2))
        db.close()

        seen: list[int] = []

        def fake_cleanup(db, account, *, delete_mode=svc.DEFAULT_DELETE_MODE, adapter=None):
            seen.append(account.id)
            return {"account_id": account.id, "status": "done", "moved": 0, "purged": 0}

        monkeypatch.setattr(svc, "cleanup_account", fake_cleanup)
        monkeypatch.setattr(svc, "SessionLocal", db_factory)
        out = svc.run_picture_space_cleanup(user_id=1)

        assert seen == [1], "只该跑行主本人的账号"
        assert out["accounts"] == 1

    def test_explicit_account_ids_narrow_the_scope(self, db_factory, monkeypatch):
        """用户口径「根据入参执行账号」—— 传了就只跑点名的那几个。"""
        db = db_factory()
        self._seed(db, (1, 1), (2, 1))
        db.close()

        seen: list[int] = []
        monkeypatch.setattr(
            svc,
            "cleanup_account",
            lambda db, account, delete_mode=svc.DEFAULT_DELETE_MODE, adapter=None: (
                seen.append(account.id)
                or {"account_id": account.id, "status": "done", "moved": 0, "purged": 0}
            ),
        )
        monkeypatch.setattr(svc, "SessionLocal", db_factory)
        svc.run_picture_space_cleanup(user_id=1, account_ids=[2])

        assert seen == [2]

    def test_one_account_failure_does_not_stop_the_round(self, db_factory, monkeypatch):
        """单账号异常不拖垮整轮（与既有同步任务同款纪律）。"""
        db = db_factory()
        self._seed(db, (1, 1), (2, 1))
        db.close()

        def boom(db, account, *, delete_mode=svc.DEFAULT_DELETE_MODE, adapter=None):
            if account.id == 1:
                raise RuntimeError("boom")
            return {"account_id": account.id, "status": "done", "moved": 1, "purged": 0}

        monkeypatch.setattr(svc, "cleanup_account", boom)
        monkeypatch.setattr(svc, "SessionLocal", db_factory)
        out = svc.run_picture_space_cleanup(user_id=1)

        assert out["accounts"] == 2
        assert out["cleaned"] == 1, "第二个账号的成果不该被第一个的异常吞掉"
        assert any(r.get("status") == "failed" for r in out["results"])

    def test_entrypoint_has_the_scheduler_signature(self):
        """调度器按 `func(user_id, **params)` 调 —— 首参必须是 `user_id`（位置）。"""
        import inspect

        sig = inspect.signature(svc.run_picture_space_cleanup)
        params = list(sig.parameters.values())
        assert params[0].name == "user_id", "调度入口首参必须是 user_id（runner 注入）"
        assert "account_ids" in sig.parameters and "delete_mode" in sig.parameters


# --------------------------------------------------------------------------- #
# 注册表 / 种子 / 前端枚举（三处必须逐字一致）
# --------------------------------------------------------------------------- #
class TestRegistryWiring:
    def test_job_spec_is_registered_as_a_builtin_account_scoped_task(self):
        from backend.app.services.job_registry import get_job_spec

        spec = get_job_spec(svc.JOB_KEY)
        assert spec.builtin is True, "内置任务（任务中心的种子行）"
        assert spec.user_scoped is True, "账号维度任务 ⇒ runner 注入行主（DEF-066）"
        assert spec.allow_concurrent is False, "平台写操作 ⇒ 恒 forbid 并发"
        assert spec.name == "清理未引用图片"

    def test_job_spec_cron_is_every_monday(self):
        """用户口径「每周一执行一次」= `0 0 0 * * MON`（6 位 cron：秒 分 时 日 月 周）。"""
        from backend.app.services.job_registry import get_job_spec

        spec = get_job_spec(svc.JOB_KEY)
        assert spec.default_trigger_type == "cron"
        assert spec.default_cron == "0 0 0 * * MON"

    def test_job_spec_params_match_the_user_spec(self):
        """用户口径：执行账号（默认全部）+ 删除方式（默认彻底删除）。"""
        from backend.app.services.job_registry import get_job_spec

        spec = get_job_spec(svc.JOB_KEY)
        assert set(spec.params_editable) == {"account_ids", "delete_mode"}
        assert spec.params_defaults["account_ids"] == []
        assert spec.params_defaults["delete_mode"] == svc.DEFAULT_DELETE_MODE
        assert spec.params_validator is svc.validate_cleanup_params

    def test_seed_file_and_registry_agree(self):
        """随包种子（`config/initial_data/scheduled_jobs.json`）与注册表**同一份数据**。

        ⚠️ 两处漂移的后果是「装完看到的默认值和代码兜底不一样」—— 用户改的时候
        以界面为准，代码以注册表为准，于是同一件事有了两个答案。
        """
        from backend.app.core.initial_data import load_initial_scheduled_jobs

        rows = {r["job_key"]: r for r in load_initial_scheduled_jobs()}
        row = rows[svc.JOB_KEY]
        assert row["name"] == "清理未引用图片"
        assert row["cron_expression"] == "0 0 0 * * MON"
        assert row["params"]["delete_mode"] == svc.DEFAULT_DELETE_MODE
        assert row["params"]["account_ids"] == []
        assert row["status"] == "paused", "平台写操作 ⇒ 默认不启用（用户确认参数后再开）"

    def test_fallback_defaults_agree_with_seed(self):
        """代码兜底字典（`_builtin_seed_defaults`）也要同值 —— 它是种子文件缺失时的退路。"""
        from backend.app.services.scheduler_service import _builtin_seed_defaults

        row = _builtin_seed_defaults()[svc.JOB_KEY]
        assert row["cron"] == "0 0 0 * * MON"
        assert row["status"] == "paused"

    def test_frontend_enum_values_equal_backend_whitelist(self):
        """前端下拉的取值必须与后端白名单**逐字相同**（跨语言同值）。

        ⚠️ 这条是「下拉里选得出来、后端必须认」的机械化：改一侧必红。
        """
        from pathlib import Path

        src = Path("frontend/src/components/schedule/params-editor.tsx").read_text(
            encoding="utf-8"
        )
        assert "delete_mode: { recycle: \"删除到回收站\", permanent: \"彻底删除\" }" in src, (
            "前端 `PARAM_ENUM_OPTIONS.delete_mode` 的取值/中文名必须与本文档口径一致"
        )
        for value in svc.DELETE_MODES:
            assert f"{value}:" in src or f"{value}:" in src, value
        assert "delete_mode: \"删除方式\"" in src, "参数中文名（PARAM_LABELS）"
