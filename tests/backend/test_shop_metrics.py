"""商品指标快照与趋势的测试（设计 17 §13.9，M2 方向 3 切片 2，2026-10-03）。

覆盖三块：
1. **口径**（`shop_metrics`）—— 价格**只读分列**（不解 `price` 字符串）、缺值一律 0、
   `cart_count` 恒 0（平台无该指标 ⇒ 列留位不造假）。
2. **采集**（`collect_snapshots`）—— 覆盖语义（同日重采取最新，不是追加）、
   `stat_date` 可注入（防跨零点假 flaky）、租户隔离。
3. **趋势**（`trend_series`）—— 按日聚合、**价格取区间最值不求和**、不补零、
   租户隔离、可按单个商品过滤。

⚠️ 与 `test_note_metrics.py` **同形**（刻意）：知道一个就知道另一个。
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import Base
from tests.backend.conftest import seed_owner_rows
from backend.app.models.monitoring import ShopMetricSnapshot
from backend.app.models.shop_product import ShopProduct
from backend.app.services import shop_metrics_service as svc


def _session(tmp_path, name="shop_metrics.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    # ⚠️ FK=ON 后必须播种父行（同 test_note_metrics，2026-10-04）。
    seed_owner_rows(engine, user_id=1, account_id=1)
    return sessionmaker(bind=engine)()


def _ensure_user(db, user_id):
    """保证 `users` 行存在（FK=ON 后 `shop_products.user_id` 有外键；跨租户用例用 id=2）。"""
    from backend.app.models.user import User

    if db.get(User, user_id) is None:
        db.add(User(id=user_id, username=f"u{user_id}", password_hash="x"))
        db.flush()


def _product(
    db,
    *,
    user_id=1,
    item_id="i1",
    title="商品",
    price="10680~55977",
    price_min_fen=10680,
    price_max_fen=55977,
    stock=68200,
    sales=0,
    sales_30d=0,
):
    _ensure_user(db, user_id)
    product = ShopProduct(
        user_id=user_id,
        platform_account_id=1,
        item_id=item_id,
        item_title=title,
        price=price,
        price_min_fen=price_min_fen,
        price_max_fen=price_max_fen,
        stock=stock,
        sales=sales,
        sales_30d=sales_30d,
        category="测试类目",
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


# --------------------------------------------------------------------------- #
# ① 口径
# --------------------------------------------------------------------------- #
def test_metrics_use_fen_columns_not_price_string():
    """价格**只读分列**，不解 `price` 字符串 —— 这是本模块最核心的口径。

    ⚠️ 变异守护：把取值改成解析 `product.price`（`"10680~55977"` 那个形态）
    ⇒ 本用例必红（解析字符串要么抛错、要么得到与分列不符的值）。

    为什么要钉死：`price` 是平台的**展示形态**（格式会变），分列是同步链路
    归一化好的**稳定口径**。解析字符串 = 复刻一份脆弱解析，且与同步链路口径分叉
    （DEF-044「同一语义不留两份实现」）。
    """
    product = ShopProduct(
        price="10680~55977",
        price_min_fen=10680,
        price_max_fen=55977,
    )
    metrics = svc.shop_metrics(product)
    assert metrics["price_min_fen"] == 10680
    assert metrics["price_max_fen"] == 55977


def test_metrics_missing_values_are_zero_not_fabricated():
    """缺值一律 0（不造假）—— 商品列全是可空 INTEGER。"""
    product = ShopProduct(stock=None, sales=None, sales_30d=None,
                          price_min_fen=None, price_max_fen=None)
    metrics = svc.shop_metrics(product)
    assert metrics == {
        "stock": 0,
        "sales": 0,
        "sales_30d": 0,
        "price_min_fen": 0,
        "price_max_fen": 0,
        "cart_count": 0,
    }


def test_cart_count_is_always_zero_platform_has_no_metric():
    """`cart_count` 平台侧**无该指标** ⇒ 恒 0（列留位）。

    ⚠️ 变异守护：图省事把它填成 `sales`（或库存）⇒ 本用例必红。
    填一个「看起来合理」的值 = 用编造数据冒充缺值（硬约束⑤）。
    """
    product = ShopProduct(stock=100, sales=50, price_min_fen=1, price_max_fen=2)
    assert svc.shop_metrics(product)["cart_count"] == 0


def test_metrics_tolerate_string_numbers():
    """字符串数字（SQLite 宽容类型下可能出现）按数值解析。"""
    product = ShopProduct(stock="120", sales="7", price_min_fen="300", price_max_fen="900")
    metrics = svc.shop_metrics(product)
    assert metrics["stock"] == 120
    assert metrics["sales"] == 7
    assert metrics["price_min_fen"] == 300


# --------------------------------------------------------------------------- #
# ② 采集
# --------------------------------------------------------------------------- #
def test_collect_writes_snapshot_per_product(tmp_path):
    db = _session(tmp_path)
    _product(db, item_id="i1")
    _product(db, item_id="i2")
    written = svc.collect_snapshots(db, user_id=1, stat_date="2026-10-03")
    assert written == 2
    rows = db.query(ShopMetricSnapshot).all()
    assert len(rows) == 2
    assert {r.stat_date for r in rows} == {"2026-10-03"}


def test_collect_overwrites_same_day_not_appends(tmp_path):
    """同一天采两次 ⇒ **行数不变**、值被覆盖（不是追加）。

    ⚠️ 变异守护：把 `on_conflict_do_update` 换成普通 insert ⇒ 唯一键报错、
    或换成 `INSERT OR IGNORE` ⇒ 值不更新（本用例第二段断言必红）。
    追加会把趋势线画成锯齿；OR IGNORE 会让当天永远停在第一次的旧值。
    """
    db = _session(tmp_path)
    product = _product(db, item_id="i1", stock=100)
    svc.collect_snapshots(db, user_id=1, stat_date="2026-10-03")

    product.stock = 55
    db.commit()
    svc.collect_snapshots(db, user_id=1, stat_date="2026-10-03")

    rows = db.query(ShopMetricSnapshot).all()
    assert len(rows) == 1, "同日重复采集不得追加行"
    assert rows[0].stock == 55, "同日重复采集必须取最新值"


def test_collect_different_days_accumulate(tmp_path):
    db = _session(tmp_path)
    _product(db, item_id="i1")
    svc.collect_snapshots(db, user_id=1, stat_date="2026-10-01")
    svc.collect_snapshots(db, user_id=1, stat_date="2026-10-02")
    assert db.query(ShopMetricSnapshot).count() == 2


def test_collect_is_scoped_to_user(tmp_path):
    """租户隔离：只采自己名下的商品。"""
    db = _session(tmp_path)
    _product(db, user_id=1, item_id="mine")
    _product(db, user_id=2, item_id="theirs")
    written = svc.collect_snapshots(db, user_id=1, stat_date="2026-10-03")
    assert written == 1
    rows = db.query(ShopMetricSnapshot).all()
    assert len(rows) == 1 and rows[0].user_id == 1


# --------------------------------------------------------------------------- #
# ③ 趋势
# --------------------------------------------------------------------------- #
def test_trend_aggregates_by_day(tmp_path):
    db = _session(tmp_path)
    _product(db, item_id="i1", stock=10)
    _product(db, item_id="i2", stock=20)
    svc.collect_snapshots(db, user_id=1, stat_date="2026-10-03")
    series = svc.trend_series(db, user_id=1, days=30)
    assert len(series) == 1
    assert series[0]["stat_date"] == "2026-10-03"
    assert series[0]["stock"] == 30


def test_trend_takes_price_range_not_sum(tmp_path):
    """价格取**区间最值**（`min(min)` ~ `max(max)`），**不求和**。

    ⚠️ 变异守护：把 `func.min(price_min_fen)` / `func.max(price_max_fen)`
    改成 `func.sum(...)` ⇒ 本用例必红。

    价格求和没有业务含义（两件 100 元的商品加起来不是「200 元的价格」）。
    """
    db = _session(tmp_path)
    _product(db, item_id="i1", price_min_fen=1000, price_max_fen=2000)
    _product(db, item_id="i2", price_min_fen=3000, price_max_fen=9000)
    svc.collect_snapshots(db, user_id=1, stat_date="2026-10-03")
    series = svc.trend_series(db, user_id=1, days=30)
    assert series[0]["price_min_fen"] == 1000, "价格下界取最小值，不是求和"
    assert series[0]["price_max_fen"] == 9000, "价格上界取最大值，不是求和"


def test_trend_does_not_fill_missing_days(tmp_path):
    """只返回**有快照的天**，不补零 —— 补零 = 把「没采」画成「掉到 0」。"""
    db = _session(tmp_path)
    _product(db, item_id="i1")
    svc.collect_snapshots(db, user_id=1, stat_date="2026-09-01")
    svc.collect_snapshots(db, user_id=1, stat_date="2026-10-03")
    series = svc.trend_series(db, user_id=1, days=0)
    assert [row["stat_date"] for row in series] == ["2026-09-01", "2026-10-03"]


def test_trend_can_filter_single_product(tmp_path):
    db = _session(tmp_path)
    a = _product(db, item_id="i1", stock=10)
    _product(db, item_id="i2", stock=20)
    svc.collect_snapshots(db, user_id=1, stat_date="2026-10-03")
    series = svc.trend_series(db, user_id=1, days=30, product_pk=a.id)
    assert len(series) == 1 and series[0]["stock"] == 10


def test_trend_is_scoped_to_user(tmp_path):
    db = _session(tmp_path)
    _product(db, user_id=1, item_id="mine", stock=10)
    _product(db, user_id=2, item_id="theirs", stock=99)
    svc.collect_snapshots(db, user_id=1, stat_date="2026-10-03")
    svc.collect_snapshots(db, user_id=2, stat_date="2026-10-03")
    series = svc.trend_series(db, user_id=1, days=30)
    assert series[0]["stock"] == 10, "不得看到别人的商品"


def test_trend_empty_when_no_snapshots(tmp_path):
    db = _session(tmp_path)
    assert svc.trend_series(db, user_id=1, days=30) == []


def test_stat_date_matches_note_side_contract():
    """`stat_date` 口径必须与笔记侧**逐字一致**（`YYYY-MM-DD`）。

    ⚠️ 变异守护：改格式（如 `%Y/%m/%d`）⇒ 本用例必红。

    两张表按日期聚合、在仪表盘上**并排展示** —— 日期口径分叉会让同一天
    在两张表里差一天（最隐蔽的一类跨模块 bug）。
    """
    from datetime import datetime

    from backend.app.services import note_metrics_service

    moment = datetime(2026, 10, 3, 23, 59, 59)
    assert svc.stat_date_of(moment) == note_metrics_service.stat_date_of(moment)
    assert svc.stat_date_of(moment) == "2026-10-03"


# --------------------------------------------------------------------------- #
# ④ 调度入口（注册表契约）
# --------------------------------------------------------------------------- #
def test_metric_jobs_are_registered_as_builtin():
    """两个采集任务必须在注册表里且 `builtin=True`（否则不会播种给用户）。

    ⚠️ 变异守护：把 `builtin` 改成 `False` ⇒ 本用例必红。
    `builtin=False` 的任务**不会**被 `seed_builtin_jobs_for_user` 播种，
    新用户永远不会自动拥有采集任务 ⇒ 快照永远不自动沉淀（本切片的核心目的落空）。
    """
    from backend.app.services.job_registry import job_registry

    registry = job_registry()
    for key in ("system.note_metrics_collect", "system.shop_metrics_collect"):
        assert key in registry, f"{key} 未注册"
        spec = registry[key]
        assert spec.builtin is True, f"{key} 必须 builtin=True 才会播种"
        assert spec.user_scoped is True, f"{key} 必须 user_scoped（DEF-066 行主隔离）"


def test_metric_job_names_are_unique():
    """中文名唯一 —— 任务中心按名字展示，重名会让用户分不清两个采集。"""
    from backend.app.services.job_registry import job_registry

    names = [spec.name for spec in job_registry().values()]
    assert len(names) == len(set(names)), f"任务中文名重复: {names}"


def test_metric_jobs_run_off_the_hour():
    """两个采集都**避开整点**、且相互错开 —— 减少 SQLite 写锁竞争（DEF-085 教训）。

    ⚠️ 变异守护：把任一条改成 `0 0 * * * *`（整点）⇒ 本用例必红。
    整点是同步/发布/巡检的集中时段（那些任务默认 `0 0 */6 * * *`）。
    """
    from backend.app.services.job_registry import job_registry

    registry = job_registry()
    note_minute = registry["system.note_metrics_collect"].default_cron.split()[1]
    shop_minute = registry["system.shop_metrics_collect"].default_cron.split()[1]
    assert note_minute != "0", "笔记采集不得落在整点"
    assert shop_minute != "0", "商品采集不得落在整点"
    assert note_minute != shop_minute, "两个采集必须错开分钟，避免同时抢写锁"
