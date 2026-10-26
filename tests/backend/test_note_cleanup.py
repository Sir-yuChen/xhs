"""无流量笔记候选查询的后端契约测试。

盯的是这条链路的**口径**而非性能（数据量在本地是几十到几千行）：

1. **三项全 0 才算无流量**：阅读 / 支付订单 / 支付金额任意一项非 0 或缺失 → 不进候选；
2. **缺值不是 0**（硬约束 ⑤）：`entry_json` 里缺键的笔记**被排除**，不造假；
3. **零平台请求**：候选查询全程不打千帆（FakeArkAdapter 记录调用数）；
4. **条件可配置**：时间区间、30 日阅读开关、阈值真的生效，不是写死全库；
5. **分页**：`total` 与窗口一致，翻页不丢不重；
6. **诚实边界**：响应必带 `coverage`（本地池大小 / 店铺笔记总数），让人知道池子多大；
7. **鉴权**：越权 404（账号不属于当前用户）；⚠️ **未绑千帆不是错**（ADR-021 起本地读零门禁
   ⇒ 200 + 空池 + `bound: false`，见设计 18 §5）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.api.platforms.xhs.qianfan import get_qianfan_api_adapter_factory
from backend.app.core.database import Base, get_db
from backend.app.core.time import shanghai_now
from backend.app.main import app
from backend.app.models import PlatformAccount, User
from backend.app.models.shop import ShopNoteGoods
from tests.backend.cookie_fixtures import bind_shop_account

import pytest

from tests.backend.conftest import (  # noqa: PLC0415 - schema 复用助手
    _BUILT_SCHEMAS,
    _SCHEMA_BY_ENGINE,
    make_engine,
    reset_test_data,
)


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration
client = TestClient(app)

COOKIES = "a1=x; access-token-ark.xiaohongshu.com=y"
SHANGHAI = timezone(timedelta(hours=8))


def _millis(days_ago: int, hour: int = 9) -> int:
    moment = datetime(2026, 9, 19, hour, 0, 0, tzinfo=SHANGHAI) - timedelta(days=days_ago)
    return int(moment.timestamp() * 1000)


class FakeArkAdapter:
    """千帆假实现：记下每一次调用，用来断言「候选查询零平台请求」。"""

    calls: dict = {}

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def get_seller_info(self):
        FakeArkAdapter.calls["seller"] = FakeArkAdapter.calls.get("seller", 0) + 1
        return True, "ok", {
            "code": 0,
            "success": True,
            "data": {"user_id": "SELLER-1", "seller_name": "观隅好物的店"},
        }

    def get_goods_note_page(self, page_num: int = 1, page_size: int = 20, *, note_ids=None):
        FakeArkAdapter.calls["goods_page"] = FakeArkAdapter.calls.get("goods_page", 0) + 1
        return True, "ok", {"code": 0, "success": True, "data": {"note_infos": [], "total": 0}}

    def search_items(self, **kwargs):
        FakeArkAdapter.calls["items"] = FakeArkAdapter.calls.get("items", 0) + 1
        return True, "ok", {"code": 0, "success": True, "data": {"total": 0, "items": []}}


def _entry(
    note_id: str,
    title: str,
    *,
    browse: int | None = 0,
    orders: int | None = 0,
    gmv: int | None = 0,
    browse_30d: int | None = 0,
    days_ago: int = 10,
    item_id: str | None = None,
) -> dict:
    """构造一条千帆商品笔记原文；`None` 表示该字段在原文里**不存在**。

    `item_id` 缺省取 `ITEM-<note_id>` —— 映射行的 `item_id` 列非空（模型 `String(64)`），
    不传就落不进去（NOT NULL 违反），而这条用例集根本不关心商品 ID。
    """
    entry = {
        "note_id": note_id,
        "note_title": title,
        "item_id": item_id if item_id is not None else f"ITEM-{note_id}",
        "note_image": f"http://qimg.example/{note_id}.jpg",
        "publish_time": _millis(days_ago),
        "xsec_token": "fake-token",
    }
    if browse is not None:
        entry["browse_num"] = browse
    if orders is not None:
        entry["note_pay_order_cnt"] = orders
    if gmv is not None:
        entry["note_pay_gmv"] = gmv
    if browse_30d is not None:
        entry["note_browse_num30d"] = browse_30d
    return entry


def _setup(tmp_path, *, with_binding: bool = True):
    engine = make_engine(tmp_path / 'note-cleanup.db')
    #: 2026-10-26：同进程复用 schema ⇒ 表只建一次，之后只清数据。
    _schema = _SCHEMA_BY_ENGINE.get(engine)
    if _schema is not None and _BUILT_SCHEMAS.get(_schema):
        reset_test_data(engine)
    else:
        Base.metadata.create_all(bind=engine)
        if _schema is not None:
            _BUILT_SCHEMAS[_schema] = True
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    FakeArkAdapter.calls = {}
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_qianfan_api_adapter_factory] = lambda: FakeArkAdapter

    response = client.post(
        "/api/auth/register", json={"username": "owner", "password": "secret123"}
    )
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    db = SessionLocal()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "owner"))
        account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="creator",
            nickname="观隅好物", status="active",
        )
        db.add(account)
        db.flush()
        owner_account_id = None
        if with_binding:
            #: ADR-021：`shops` 已退场 —— 账号归属写 `seller_id`，状态写
            #: `shop_state`。映射的归属键随之从 `binding_id` 换成
            #: `platform_account_id`（**换列不改语义**：绑定当年就是账号级的）。
            #:
            #: ⚠️ **必须留默认的 `ark` 令牌**（不能再传 `cookies_text=""`）：
            #: 旧口径下「绑了店」本身就是读候选池的凭据，所以当年可以只要状态不要票；
            #: ADR-021 把判据换成 `has_ark_session`（= `ark` 通道有令牌）之后，
            #: 没票的账号走的是 `list_candidates` 的「无登录态」早退分支 ⇒ 恒空池，
            #: 于是整份用例集会以「ZERO-1 不在候选里」这种指错方向的方式集体变红。
            bind_shop_account(
                db,
                account,
                shop_name="观隅好物的店",
                goods_note_total=1968,
            )
            owner_account_id = account.id
        db.commit()
        account_id = account.id
    finally:
        db.close()
    return headers, account_id, owner_account_id, SessionLocal


def _seed(SessionLocal, owner_account_id: int, entries: list[dict]) -> None:
    """直接把构造好的 `entry_json` 写进本地映射（绕过千帆）。"""
    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, owner_account_id)
        for entry in entries:
            db.add(
                ShopNoteGoods(
                    user_id=account.user_id,
                    platform_account_id=owner_account_id,
                    note_id=entry["note_id"],
                    item_id=entry["item_id"],
                    item_title=entry.get("item_title", "商品"),
                    entry_json=json.dumps(entry, ensure_ascii=False),
                    synced_at=shanghai_now(),
                )
            )
        db.commit()
    finally:
        db.close()


def _teardown():
    app.dependency_overrides.clear()


NOTES = [
    _entry("ZERO-1", "三项全零", browse=0, orders=0, gmv=0, days_ago=5),
    _entry("ZERO-2", "另一条全零", browse=0, orders=0, gmv=0, days_ago=2),
    _entry("HAS-READ", "有人看", browse=3, orders=0, gmv=0, days_ago=8),
    _entry("HAS-ORDER", "有人买", browse=0, orders=1, gmv=0, days_ago=8),
    _entry("HAS-GMV", "有钱", browse=0, orders=0, gmv=9900, days_ago=8),
    _entry("NO-BROWSE", "缺阅读字段", browse=None, orders=0, gmv=0, days_ago=8),
    _entry("NO-ORDER", "缺订单字段", browse=0, orders=None, gmv=0, days_ago=8),
    _entry("NO-GMV", "缺金额字段", browse=0, orders=0, gmv=None, days_ago=8),
    _entry("STRINGS", "脏数据字符串", browse="0", orders="0", gmv="0", days_ago=1),
    _entry("OLD-ZERO", "很早的全零", browse=0, orders=0, gmv=0, days_ago=200),
]


def _candidates(headers, account_id: int, **params):
    payload = {"platform_account_id": account_id, **params}
    response = client.post(
        "/api/xhs/qianfan/note-cleanup/candidates", json=payload, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 1. 三项全 0 才算无流量；缺值不是 0
# --------------------------------------------------------------------------- #
def test_only_all_zero_notes_are_candidates(tmp_path):
    headers, account_id, owner_account_id, SessionLocal = _setup(tmp_path)
    try:
        _seed(SessionLocal, owner_account_id, NOTES)
        result = _candidates(headers, account_id)

        ids = [item["note_id"] for item in result["candidates"]]
        # 有阅读 / 有订单 / 有金额 各一项非零 → 都不算
        assert "HAS-READ" not in ids
        assert "HAS-ORDER" not in ids
        assert "HAS-GMV" not in ids
        # 缺字段的笔记被排除（缺值 ≠ 0，不造假）
        assert "NO-BROWSE" not in ids
        assert "NO-ORDER" not in ids
        assert "NO-GMV" not in ids
        # 三项全 0 的才进
        assert "ZERO-1" in ids
        assert "ZERO-2" in ids
        assert "OLD-ZERO" in ids
        # 脏数据 "0" 字符串按 0 处理（千帆字段集在变，留点容错）
        assert "STRINGS" in ids
        assert result["total"] == 4
    finally:
        _teardown()


def test_missing_metric_fields_exclude_the_note(tmp_path):
    """缺值口径单独钉一次：只有一项缺失就足以把笔记排除。"""
    headers, account_id, owner_account_id, SessionLocal = _setup(tmp_path)
    try:
        _seed(SessionLocal, owner_account_id, [_entry("X", "缺阅读", browse=None)])
        result = _candidates(headers, account_id)
        assert result["total"] == 0
        assert result["candidates"] == []
    finally:
        _teardown()


# --------------------------------------------------------------------------- #
# 2. 零平台请求
# --------------------------------------------------------------------------- #
def test_query_makes_zero_platform_calls(tmp_path):
    headers, account_id, owner_account_id, SessionLocal = _setup(tmp_path)
    try:
        _seed(SessionLocal, owner_account_id, NOTES)
        _candidates(headers, account_id)
        _candidates(headers, account_id, page=2, page_size=3)
        assert FakeArkAdapter.calls == {}, (
            f"候选查询必须零平台请求，实际调了：{FakeArkAdapter.calls}"
        )
    finally:
        _teardown()


# --------------------------------------------------------------------------- #
# 3. 条件可配置（时间区间 / 30 日窗口 / 阈值）
# --------------------------------------------------------------------------- #
def test_publish_time_range_filters(tmp_path):
    headers, account_id, owner_account_id, SessionLocal = _setup(tmp_path)
    try:
        _seed(SessionLocal, owner_account_id, NOTES)
        recent = _candidates(
            headers, account_id, publish_from="2026-09-01", publish_to="2026-09-19"
        )
        recent_ids = {item["note_id"] for item in recent["candidates"]}
        assert "OLD-ZERO" not in recent_ids  # 200 天前，被时间区间排除
        assert "ZERO-1" in recent_ids

        ancient = _candidates(headers, account_id, publish_from="2026-01-01", publish_to="2026-03-31")
        ancient_ids = {item["note_id"] for item in ancient["candidates"]}
        assert ancient_ids == {"OLD-ZERO"}  # 200 天前 ≈ 2026-03-03，只有它在区间里
    finally:
        _teardown()


def test_publish_time_range_rejects_inverted_and_garbage(tmp_path):
    headers, account_id, owner_account_id, SessionLocal = _setup(tmp_path)
    try:
        _seed(SessionLocal, owner_account_id, NOTES)
        inverted = client.post(
            "/api/xhs/qianfan/note-cleanup/candidates",
            json={
                "platform_account_id": account_id,
                "publish_from": "2026-09-19",
                "publish_to": "2026-09-01",
            },
            headers=headers,
        )
        assert inverted.status_code == 400, inverted.text
        assert inverted.json()["detail"]["code"] == "NOTE_CLEANUP_TIME_RANGE_INVALID"

        garbage = client.post(
            "/api/xhs/qianfan/note-cleanup/candidates",
            json={"platform_account_id": account_id, "publish_from": "上周"},
            headers=headers,
        )
        assert garbage.status_code == 400, garbage.text
    finally:
        _teardown()


def test_30d_window_is_optional_and_strict(tmp_path):
    headers, account_id, owner_account_id, SessionLocal = _setup(tmp_path)
    try:
        _seed(SessionLocal, owner_account_id, NOTES)
        # 默认不看 30 日阅读：30 日有读的全零笔记仍在候选里
        base = _candidates(headers, account_id)
        base_30d = {item["note_id"] for item in base["candidates"] if item.get("browse_num_30d")}
        assert base_30d == set(), "没开 30 日窗口时不应回该字段"

        # 开了 30 日窗口 + 上限 0：30 日阅读 > 0 的被排除
        strict = _candidates(headers, account_id, include_30d=True, browse_30d_max=0)
        strict_ids = {item["note_id"] for item in strict["candidates"]}
        assert "ZERO-1" in strict_ids  # browse_30d=0
        assert "OLD-ZERO" in strict_ids

        # 开了窗口但上限放宽：30 日有读的也回来
        loose = _candidates(headers, account_id, include_30d=True, browse_30d_max=100)
        loose_ids = {item["note_id"] for item in loose["candidates"]}
        assert "ZERO-2" in loose_ids
        item = next(x for x in loose["candidates"] if x["note_id"] == "ZERO-2")
        assert item["browse_num_30d"] is not None
    finally:
        _teardown()


def test_thresholds_are_configurable(tmp_path):
    """阈值不是写死 0：放宽到「阅读 ≤ 3」时，有少量阅读的笔记也进候选。"""
    headers, account_id, owner_account_id, SessionLocal = _setup(tmp_path)
    try:
        _seed(SessionLocal, owner_account_id, NOTES)
        loose = _candidates(
            headers, account_id, browse_num_max=3, pay_order_cnt_max=0, pay_gmv_max=0
        )
        ids = {item["note_id"] for item in loose["candidates"]}
        assert "HAS-READ" in ids  # browse=3，在放宽后的阈值内
        assert "HAS-ORDER" not in ids  # orders=1，仍超阈值
        assert "HAS-GMV" not in ids
    finally:
        _teardown()


# --------------------------------------------------------------------------- #
# 4. 分页
# --------------------------------------------------------------------------- #
def test_pagination_windows_and_total(tmp_path):
    headers, account_id, owner_account_id, SessionLocal = _setup(tmp_path)
    try:
        _seed(SessionLocal, owner_account_id, NOTES)
        first = _candidates(headers, account_id, page=1, page_size=2)
        second = _candidates(headers, account_id, page=2, page_size=2)
        assert first["total"] == 4
        assert len(first["candidates"]) == 2
        assert len(second["candidates"]) == 2
        ids = {item["note_id"] for item in first["candidates"]}
        ids |= {item["note_id"] for item in second["candidates"]}
        assert ids == {"ZERO-1", "ZERO-2", "OLD-ZERO", "STRINGS"}
        # 越界页返回空窗口，total 仍是真实候选数
        beyond = _candidates(headers, account_id, page=99, page_size=2)
        assert beyond["candidates"] == []
        assert beyond["total"] == 4
    finally:
        _teardown()


def test_candidates_sorted_by_publish_time_desc(tmp_path):
    headers, account_id, owner_account_id, SessionLocal = _setup(tmp_path)
    try:
        _seed(SessionLocal, owner_account_id, NOTES)
        result = _candidates(headers, account_id)
        times = [item["publish_time"] for item in result["candidates"]]
        assert times == sorted(times, reverse=True), times
    finally:
        _teardown()


# --------------------------------------------------------------------------- #
# 5. 诚实边界 + 鉴权
# --------------------------------------------------------------------------- #
def test_coverage_makes_pool_size_explicit(tmp_path):
    """用户已接受「本地数据不全面」：响应必须让人看出池子有多大。"""
    headers, account_id, owner_account_id, SessionLocal = _setup(tmp_path)
    try:
        _seed(SessionLocal, owner_account_id, NOTES)
        result = _candidates(headers, account_id)
        assert result["coverage"]["pool"] == len(NOTES)
        assert result["coverage"]["goods_note_total"] == 1968
        # 分子 < 分母 时这个差距是可见的
        assert result["total"] <= result["coverage"]["pool"]
        # ADR-021：店铺名 = 账号昵称（千帆 seller_name 与昵称同源，不再有 shops 表）
        assert result["shop_name"] == "观隅好物"
    finally:
        _teardown()


def test_unbound_account_reads_an_empty_pool(tmp_path):
    """没绑千帆 ⇒ **不是错误**，是空池（ADR-021：纯本地读，零门禁）。

    被取代的旧断言原句保留：*「没绑千帆就没有候选池（缺值口径：不能把空池包装成
    「0 条无流量」）…… `assert response.status_code == 404` /
    `detail["code"] == "QIANFAN_BINDING_REQUIRED"`」* —— 该 404 门禁已随
    2026-09-30 用户口径「本地库查询无需校验千帆是否可用」移除（设计 18 §5）。
    现在未登录也能读已同步的数据；**取不到东西 ≠ 出错**，按失败三分法给空态即可。
    """
    headers, account_id, _, _ = _setup(tmp_path, with_binding=False)
    try:
        response = client.post(
            "/api/xhs/qianfan/note-cleanup/candidates",
            json={"platform_account_id": account_id},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["candidates"] == []
        assert body["total"] == 0
        assert body["coverage"] == {"pool": 0, "goods_note_total": 0}
        assert body["bound"] is False, "空池要自曝「没登录态」，别冒充「0 条无流量」"
        assert "QIANFAN_BINDING_REQUIRED" not in response.text
    finally:
        _teardown()


def test_other_users_account_is_404(tmp_path):
    headers, account_id, _, _ = _setup(tmp_path)
    try:
        other = client.post(
            "/api/auth/register", json={"username": "other", "password": "secret123"}
        )
        assert other.status_code == 200, other.text
        other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
        response = client.post(
            "/api/xhs/qianfan/note-cleanup/candidates",
            json={"platform_account_id": account_id},
            headers=other_headers,
        )
        assert response.status_code == 404, response.text
    finally:
        _teardown()


def test_query_writes_nothing(tmp_path):
    """只读：查完之后本地映射行数一个不变。"""
    headers, account_id, owner_account_id, SessionLocal = _setup(tmp_path)
    try:
        _seed(SessionLocal, owner_account_id, NOTES)
        db = SessionLocal()
        before = db.scalar(select(func.count()).select_from(ShopNoteGoods))
        db.close()
        _candidates(headers, account_id)
        db = SessionLocal()
        after = db.scalar(select(func.count()).select_from(ShopNoteGoods))
        db.close()
        assert before == after == len(NOTES)
    finally:
        _teardown()
