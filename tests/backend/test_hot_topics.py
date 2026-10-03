"""热点驱动生成的口径与采集（设计 17 §13.11，M3 方向 2）。

覆盖四块：
1. **口径**（`parse_topics`）—— 两种载体、`type=2` 无热度、空话题不落、非 dict 容错；
2. **匹配分**（`relevance_for`）—— 可解释成分、空话题 0 分、热度不主导排序；
3. **落库**（`collect_topics_for_item` / `match_topics_to_goods`）—— 覆盖不是追加、
   **平台没给词就不落**（不造）、商品不在台账不匹配；
4. **排序稳定**（`list_matches`）—— 同分有确定次序（否则每次刷新列表乱跳）。
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import Base
from backend.app.models import HotTopic, ShopProduct, TopicGoodsMatch
from backend.app.services import hot_topic_service as svc


def _session(tmp_path, name="hot_topic.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    # ⚠️ 生产库在连接级打开 `PRAGMA foreign_keys=ON`（`core/database.py`），
    # 测试夹具自己 create_engine 不走那条监听，必须**显式**打开同一个 PRAGMA ——
    # 否则外键在测试里是关着的，`platform_account_id=1` 这种引用一个不存在的
    # 账号行也能写进去，与生产行为不一致（打开后这些用例必须先建账号行）。
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _account(db, *, user_id=1, account_id=1):
    """建被外键引用的账号行 —— FK 打开后，商品/话题行的 `platform_account_id` 必须指向真实存在的账号。"""
    from backend.app.models import PlatformAccount, User

    # `users.id` 同样被外键引用（HotTopic.user_id / ShopProduct.user_id），先建用户行。
    if not db.get(User, user_id):
        db.add(User(id=user_id, username=f"测试用户{user_id}", password_hash="x"))
        db.commit()

    account = PlatformAccount(
        id=account_id,
        user_id=user_id,
        platform="xhs",
        nickname=f"测试账号{account_id}",
    )
    db.add(account)
    db.commit()
    return account


def _product(db, *, user_id=1, item_id="i1", title="中式椭圆形桌垫皮革桌布", category="居家布艺 / 桌布"):
    product = ShopProduct(
        user_id=user_id,
        platform_account_id=1,
        item_id=item_id,
        item_title=title,
        category=category,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def _attrs(topics, points=None):
    """构造一次 `aigc-publish/attributes` 的 `data` 载体。"""
    return {
        "topic_list": {"topic_list_info": topics},
        "seller_point": {"seller_point_info": points or []},
    }


# --------------------------------------------------------------------------- #
# ① 口径
# --------------------------------------------------------------------------- #
def test_parse_topics_accepts_both_carriers():
    """**两种载体都要吃**（完整响应 / 已解包的 `data`）。

    ⚠️ 变异守护：去掉「否则用 payload 本身」那一行 ⇒ 后半段断言必红。

    ⚠️ 这条守护来自真机实踩：接口先只认一种载体 ⇒ `HTTP 200 + 0 条`
    （看起来像「平台没给词」，实际是我们取错层 —— 最隐蔽的静默退化）。
    """
    inner = {"topic_list": {"topic_list_info": [{"topic": "餐垫", "type": 1, "hot_value": 4673}]}}
    full = {"code": 0, "data": inner}
    assert svc.parse_topics(full) == svc.parse_topics(inner)
    assert svc.parse_topics(full)[0]["topic"] == "餐垫"


def test_parse_topics_type2_has_no_hot_value():
    """`type=2`（活动话题）平台**不给热度** ⇒ 落 0（**不猜**）。

    ⚠️ 变异守护：给 `type=2` 编一个默认热度 ⇒ 本用例必红。
    """
    rows = svc.parse_topics(_attrs([{"topic": "毛孩子时装周", "type": 2}]))
    assert rows == [{"topic": "毛孩子时装周", "topic_type": 2, "hot_value": 0}]


def test_parse_topics_skips_blank_and_non_dict():
    """空话题词 / 非 dict 项一律跳过（空话题落库无意义，且会污染唯一键）。"""
    rows = svc.parse_topics(
        _attrs(
            [
                {"topic": "", "type": 1, "hot_value": 9},
                {"topic": "   ", "type": 1, "hot_value": 9},
                "not-a-dict",
                {"topic": "有效", "type": 1, "hot_value": 5},
            ]
        )
    )
    assert [r["topic"] for r in rows] == ["有效"]


def test_parse_topics_tolerates_broken_payloads():
    """坏结构一律空列表（不抛异常：「平台没给词」与「结构变了」都不该炸链路）。"""
    for payload in (None, {}, {"data": {}}, {"data": {"topic_list": None}}, "x", []):
        assert svc.parse_topics(payload) == []


# --------------------------------------------------------------------------- #
# ② 匹配分（可解释）
# --------------------------------------------------------------------------- #
def test_relevance_explains_which_field_matched():
    """`match_reason` 必须说清**命中了哪个字段** —— 排序被质疑时的唯一依据。

    ⚠️ 变异守护：把 `match_reason` 写死成空串 / 只写总分 ⇒ 本用例必红。
    """
    product = ShopProduct(item_title="中式桌垫皮革桌布", category="居家布艺 / 桌布")
    score, reason = svc.relevance_for("桌布", product, hot_value=0, seller_points=[])
    assert score == svc.TITLE_WEIGHT + svc.CATEGORY_WEIGHT
    assert "标题" in reason and "类目" in reason


def test_relevance_blank_topic_scores_zero():
    """空话题 **0 分** —— 不得因为「空串是任何串的子串」而满命中。

    ⚠️ 变异守护：去掉开头 `if not keyword: return 0` ⇒ 本用例必红（空串会命中标题）。
    """
    product = ShopProduct(item_title="桌布", category="桌布")
    score, reason = svc.relevance_for("", product, hot_value=9999, seller_points=["桌布"])
    assert score == 0 and reason == ""


def test_hot_value_does_not_dominate_ordering():
    """平台热度**不主导**排序（压成对数档，上限 `HOT_VALUE_CAP`）。

    ⚠️ 变异守护：把 `min(hot // DIV, CAP)` 改成直接 `+ hot_value` ⇒ 本用例必红
    （一个 11214 热度的无关词会压过所有词面命中）。
    """
    product = ShopProduct(item_title="桌布", category="桌布")
    hit_score, _ = svc.relevance_for("桌布", product, hot_value=0, seller_points=[])
    unrelated_score, _ = svc.relevance_for("毫不相关", product, hot_value=999999, seller_points=[])
    assert hit_score > unrelated_score, "词面命中必须压得过纯热度"
    assert unrelated_score == svc.HOT_VALUE_CAP


# --------------------------------------------------------------------------- #
# ③ 落库
# --------------------------------------------------------------------------- #
def test_collect_overwrites_same_day_not_appends(tmp_path):
    """同日同商品同话题**覆盖**（热度会变），不是追加。

    ⚠️ 变异守护：把 `on_conflict_do_update` 换掉 ⇒ 唯一键报错或值不更新，本用例必红。
    追加会让热榜每次刷新行数翻倍。
    """
    db = _session(tmp_path)
    _account(db)
    _product(db, item_id="i1")
    svc.collect_topics_for_item(
        db, user_id=1, item_id="i1", attributes=_attrs([{"topic": "餐垫", "type": 1, "hot_value": 100}]),
        stat_date="2026-10-04",
    )
    svc.collect_topics_for_item(
        db, user_id=1, item_id="i1", attributes=_attrs([{"topic": "餐垫", "type": 1, "hot_value": 999}]),
        stat_date="2026-10-04",
    )
    rows = db.query(HotTopic).all()
    assert len(rows) == 1, "同日重复采集不得追加行"
    assert rows[0].hot_value == 999, "必须取最新热度"


def test_collect_writes_nothing_when_platform_gives_no_topics(tmp_path):
    """平台没给话题 ⇒ **一条不落**（不造数据，硬约束⑤）。

    ⚠️ 变异守护：给空响应塞一个默认话题 / 落一条 0 分占位行 ⇒ 本用例必红。
    """
    db = _session(tmp_path)
    _account(db)
    _product(db, item_id="i1")
    written = svc.collect_topics_for_item(
        db, user_id=1, item_id="i1", attributes={"topic_list": {"topic_list_info": []}},
        stat_date="2026-10-04",
    )
    assert written == 0
    assert db.query(HotTopic).count() == 0


def test_match_skips_when_product_not_in_ledger(tmp_path):
    """商品不在台账 ⇒ 不匹配（匹配结果没有指向对象，落库无意义）。"""
    db = _session(tmp_path)
    _account(db)
    written = svc.match_topics_to_goods(
        db, user_id=1, attributes=_attrs([{"topic": "餐垫", "type": 1, "hot_value": 1}]),
        item_id="ghost", stat_date="2026-10-04",
    )
    assert written == 0
    assert db.query(TopicGoodsMatch).count() == 0


def test_match_persists_reason_and_components(tmp_path):
    """匹配行必须同时带 `relevance_score` / `hot_value` / `match_reason`。

    ⚠️ 只存总分 ⇒ 「为什么这个词排第一」答不上来（DEF-057 同型不可追溯）。
    """
    db = _session(tmp_path)
    _account(db)
    _product(db, item_id="i1")
    svc.match_topics_to_goods(
        db, user_id=1, item_id="i1",
        attributes=_attrs([{"topic": "桌布", "type": 1, "hot_value": 1781}], points=["防水"]),
        stat_date="2026-10-04",
    )
    row = db.query(TopicGoodsMatch).one()
    assert row.relevance_score > 0
    assert row.hot_value == 1781
    assert "标题" in row.match_reason


# --------------------------------------------------------------------------- #
# ④ 排序稳定
# --------------------------------------------------------------------------- #
def test_matches_sorted_by_score_with_stable_tiebreak(tmp_path):
    """按相关度降序；**同分有确定次序**（否则每次刷新列表乱跳）。

    ⚠️ 变异守护：去掉 `item_id` / `topic` 兜底排序 ⇒ 同分项顺序由 DB 决定，
    本用例的「两次调用结果一致」断言在数据量上来后会红。
    """
    db = _session(tmp_path)
    _account(db)
    _product(db, item_id="i1")
    _product(db, item_id="i2")
    topics = [{"topic": f"词{i}", "type": 1, "hot_value": 100} for i in range(6)]
    for item in ("i1", "i2"):
        svc.match_topics_to_goods(
            db, user_id=1, item_id=item, attributes=_attrs(topics), stat_date="2026-10-04"
        )
    first = [(r["item_id"], r["topic"]) for r in svc.list_matches(db, user_id=1)]
    second = [(r["item_id"], r["topic"]) for r in svc.list_matches(db, user_id=1)]
    assert first == second, "同分项次序必须稳定"


def test_list_is_tenant_scoped(tmp_path):
    """租户隔离：只看自己名下的采集结果。"""
    db = _session(tmp_path)
    _account(db)
    _account(db, user_id=2, account_id=2)
    _product(db, user_id=1, item_id="mine")
    _product(db, user_id=2, item_id="theirs")
    svc.collect_topics_for_item(
        db, user_id=1, item_id="mine", attributes=_attrs([{"topic": "我的", "type": 1, "hot_value": 1}]),
        stat_date="2026-10-04",
    )
    svc.collect_topics_for_item(
        db, user_id=2, item_id="theirs", attributes=_attrs([{"topic": "别人的", "type": 1, "hot_value": 1}]),
        stat_date="2026-10-04",
    )
    topics = svc.list_topics(db, user_id=1)
    assert [t["topic"] for t in topics] == ["我的"]
