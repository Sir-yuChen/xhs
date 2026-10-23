"""LLM 生成笔记的**版本历史**测试（用户口径 2026-10-17）。

> 「千帆生成的笔记不存，LLM 生成笔记记录，且还有一个问题，比如基于指定生成的某一版本，
>   用户只想重新生成正文或只重新配置生成图片」

判据落在**两条不变量**上（不是「字段存在」）：

1. **版本行永不覆盖** —— 生成 N 次 ⇒ N 行，历史可回溯；
2. **同商品最多一条 `is_adopted`** —— 「采用版」是用户的选择，不是时间序。

以及局部重生成的**基线校验**（必须成功、必须同用户、必须是那一次的真记录）。
"""

from __future__ import annotations

import json

import pytest

from backend.app.models.generation import (
    GEN_STATUS_FAILED,
    GEN_STATUS_QUEUED,
    GEN_STATUS_SUCCESS,
    REGEN_SCOPES,
    GenerationRecord,
)
from backend.app.services import note_version_service as nvs



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract


class _User:
    def __init__(self, uid: int = 2):
        self.id = uid


@pytest.fixture()
def db(db_session):
    """公共夹具的会话（`tests/backend/conftest.py`，不自己 create_engine）。

    ⚠️ 用 `db_session` 而不是 `db_factory`：它**同时打桩全部服务模块的 Session 工厂**
    （2026-10-04 实测事故的修复），避免「直接调服务函数」时静默打到生产库。
    """
    return db_session


def _seed_fks(db, *, user_id: int = 2, product_id: int = 2053) -> None:
    """补齐 `generation_records` 的外键行（users / shop_products）。

    ⚠️ `generation_records.user_id` 与 `shop_product_id` 都是**真外键**
    ⇒ 不先建这两行，插入会直接 `FOREIGN KEY constraint failed`
    （不是断言失败，是连数据都进不去）。
    """
    from backend.app.models.platform_account import PlatformAccount
    from backend.app.models.shop_product import ShopProduct
    from backend.app.models.user import User

    if db.get(User, int(user_id)) is None:
        db.add(User(id=int(user_id), username=f"user-{user_id}", password_hash="x"))
        db.flush()
    #: `shop_products.platform_account_id` 也是真外键（ADR-021：台账按账号隔离）
    account_id = 9000 + int(user_id)
    if db.get(PlatformAccount, account_id) is None:
        db.add(
            PlatformAccount(
                id=account_id,
                user_id=int(user_id),
                platform="xhs",
                external_user_id=f"xhs-{account_id}",
                nickname=f"账号 {account_id}",
            )
        )
        db.flush()
    if db.get(ShopProduct, int(product_id)) is None:
        db.add(
            ShopProduct(
                id=int(product_id),
                user_id=int(user_id),
                platform_account_id=account_id,
                item_id=f"item-{product_id}",
                item_title=f"商品 {product_id}",
            )
        )
        db.flush()


def _record(db, *, user_id=2, product=2053, version=1, status=GEN_STATUS_SUCCESS,
            title="标题", body="正文", adopted=False, parent=None, scope="all"):
    _seed_fks(db, user_id=int(user_id), product_id=int(product))
    row = GenerationRecord(
        user_id=user_id,
        shop_product_id=product,
        status=status,
        title=title,
        body=body,
        topics_json=json.dumps(["话题A"], ensure_ascii=False),
        version_no=version,
        regen_scope=scope,
        is_adopted=adopted,
        parent_record_id=parent,
        copy_requirements_json="{}",
    )
    db.add(row)
    db.flush()
    return row


class TestScopes:
    def test_scopes_are_exactly_three(self):
        """范围**按块**：整篇 / 标题 / 正文 / 话题 / 配图。

        ⚠️ 原断言是 `{all, text, media}`（**已被取代** 2026-10-18）——
        用户口径*「后端应支持每个文案区域单独重新生成」*：`text` 把三块一起重写，
        界面上写「只改这一块」是假的，故拆成 `title` / `body` / `topics`。
        `text` 作为**历史字面量**仍接受（见 `REGEN_LEGACY_SCOPES`），但不在新集合里。
        """
        assert set(REGEN_SCOPES) == {"all", "title", "body", "topics", "media"}

    def test_unknown_scope_is_rejected(self):
        """未知范围 ⇒ 400（**不静默按 all** —— 用户点「只重写正文」却整篇重生成了）。"""
        with pytest.raises(nvs.VersionError) as excinfo:
            nvs.normalize_scope("image_only")
        assert excinfo.value.code == "LLM_REGEN_SCOPE_INVALID"
        assert "all/title/body/topics/media" in excinfo.value.message

    def test_empty_scope_defaults_to_all(self):
        assert nvs.normalize_scope("") == "all"
        assert nvs.normalize_scope(None) == "all"


class TestVersionNumber:
    def test_first_version_is_one(self, db):
        assert nvs.next_version_no(db, user_id=2, shop_product_id=999999) == 1

    def test_version_increments(self, db):
        _record(db, product=880001, version=1)
        _record(db, product=880001, version=2)
        assert nvs.next_version_no(db, user_id=2, shop_product_id=880001) == 3

    def test_version_uses_max_not_count(self, db):
        """删掉中间一版后仍**单调**（用 `count+1` 会与已有序号撞车、出现两个「第 3 版」）。"""
        _record(db, product=880002, version=1)
        _record(db, product=880002, version=3)  # 第 2 版「被删了」
        assert nvs.next_version_no(db, user_id=2, shop_product_id=880002) == 4

    def test_version_is_scoped_per_product(self, db):
        """不同商品的版本号互不干扰。"""
        _record(db, product=880003, version=5)
        assert nvs.next_version_no(db, user_id=2, shop_product_id=880004) == 1

    def test_version_is_scoped_per_user(self, db):
        """跨租户不串（DEF-066 同精神）。"""
        _record(db, user_id=2, product=880005, version=7)
        assert nvs.next_version_no(db, user_id=99, shop_product_id=880005) == 1


class TestAdoption:
    def test_adopted_is_exclusive_within_product(self, db):
        """同商品**最多一条**采用版（DB 部分唯一索引 + 服务层先清后设）。"""
        first = _record(db, product=880010, version=1, adopted=True)
        second = _record(db, product=880010, version=2)
        nvs.mark_adopted(db, record=second)
        db.flush()
        assert second.is_adopted is True
        assert first.is_adopted is False

    def test_adoption_does_not_leak_across_products(self, db):
        """给 A 商品设采用版，不该把 B 商品的采用版清掉。"""
        other = _record(db, product=880011, version=1, adopted=True)
        target = _record(db, product=880012, version=1)
        nvs.mark_adopted(db, record=target)
        db.flush()
        assert other.is_adopted is True
        assert target.is_adopted is True


class TestBaselineValidation:
    def test_all_scope_needs_no_target_version(self, db):
        assert nvs.validate_target_version(db, user=_User(), target_id=None, scope="all") is None

    def test_text_scope_requires_target_version(self, db):
        with pytest.raises(nvs.VersionError) as excinfo:
            nvs.validate_target_version(db, user=_User(), target_id=None, scope="text")
        assert excinfo.value.code == "LLM_TARGET_REQUIRED"

    def test_media_scope_requires_target_version(self, db):
        with pytest.raises(nvs.VersionError):
            nvs.validate_target_version(db, user=_User(), target_id=None, scope="media")

    def test_failed_target_version_is_rejected(self, db):
        """失败版本没有可沿用的内容 ⇒ 400（不静默产出空笔记）。"""
        bad = _record(db, product=880020, version=1, status=GEN_STATUS_FAILED, body="")
        with pytest.raises(nvs.VersionError) as excinfo:
            nvs.validate_target_version(db, user=_User(), target_id=int(bad.id), scope="text")
        assert excinfo.value.code == "LLM_TARGET_NOT_SUCCESS"

    def test_queued_target_version_is_rejected(self, db):
        """还在排队/执行的版本也不能当基线（内容还没产出）。"""
        pending = _record(db, product=880021, version=1, status=GEN_STATUS_QUEUED, body="")
        with pytest.raises(nvs.VersionError):
            nvs.validate_target_version(db, user=_User(), target_id=int(pending.id), scope="media")

    def test_success_target_version_is_accepted(self, db):
        good = _record(db, product=880022, version=1)
        got = nvs.validate_target_version(db, user=_User(), target_id=int(good.id), scope="text")
        assert got is not None and int(got.id) == int(good.id)

    def test_other_users_target_version_is_not_found(self, db):
        """跨租户取基线 ⇒ 当作不存在（不泄露别人有没有这条记录）。"""
        other = _record(db, user_id=77, product=880023, version=1)
        with pytest.raises(nvs.VersionError) as excinfo:
            nvs.validate_target_version(db, user=_User(2), target_id=int(other.id), scope="text")
        assert excinfo.value.code == "LLM_RECORD_NOT_FOUND"


class TestListing:
    def test_list_is_newest_first_and_includes_failures(self, db):
        """历史列表**含失败版本**且新→旧（用户要看到「第 3 次失败了」）。"""
        _record(db, product=880030, version=1)
        _record(db, product=880030, version=2, status=GEN_STATUS_FAILED, body="")
        _record(db, product=880030, version=3)
        rows = nvs.list_versions(db, user_id=2, shop_product_id=880030)
        assert [r.version_no for r in rows] == [3, 2, 1]
        assert rows[1].status == GEN_STATUS_FAILED

    def test_list_does_not_leak_other_products(self, db):
        _record(db, product=880031, version=1)
        _record(db, product=880032, version=1)
        rows = nvs.list_versions(db, user_id=2, shop_product_id=880031)
        assert len(rows) == 1
        assert int(rows[0].shop_product_id) == 880031


class TestSummary:
    def test_summary_does_not_ship_full_body(self, db):
        """列表形态**只给正文摘要**（几十版 × 300 字全量下发会让接口变几百 KB）。"""
        long_body = "正" * 500
        row = _record(db, product=880040, version=1, body=long_body)
        summary = nvs.version_summary(row)
        assert "body" not in summary
        assert len(summary["body_excerpt"]) == 60
        assert summary["body_length"] == 500

    def test_summary_carries_scope_and_parent(self, db):
        parent = _record(db, product=880041, version=1)
        child = _record(
            db, product=880041, version=2, parent=int(parent.id), scope="text"
        )
        summary = nvs.version_summary(child)
        assert summary["regen_scope"] == "text"
        assert summary["parent_record_id"] == int(parent.id)
        assert summary["version_no"] == 2
