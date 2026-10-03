"""热点驱动生成的口径与采集（设计 17 §13.11，M3 方向 2，2026-10-04）。

## 数据源（真机实测，**不要按想象改**）

`POST /api/edith/aigc-publish/attributes`（**只读**，适配器已存在）返回：

```
data.topic_list.topic_list_info:
  [{"topic": "餐垫", "type": 1, "hot_value": 4673}, …]
  tips: "平台结合所选商品和社区热门笔记，为您推荐热度较高的词"
```

⚠️ **该接口按 `item_id` 调用** ⇒ 话题是「**与该商品相关**的平台热度词」，
**不是全站热搜总榜**（`apis/` 层实测无 `hot_search` / `trending` / `rank_list`）。
页面文案与接口注释都必须如实这么写。

## 匹配分口径（可解释，不是黑盒）

```
relevance = 词面命中权重（标题/类目/卖点） + 平台热度项
```

⚠️ **不引入语义模型**（无 embedding 依赖）：
① 平台已经把「相关」做过一遍 —— 这些词本来就是**按该商品**推的；
② 词面命中**可解释、可测试**；引入黑盒相似度后「为什么这个词排第一」就答不上来
（DEF-057 同型的不可追溯问题）。

⚠️ **两个成分都存**（`match_reason` + `hot_value`），不只存总分。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from backend.app.core.time import shanghai_now
from backend.app.models import HotTopic, ShopProduct, TopicGoodsMatch
from backend.app.services.sqlite_retry import retry_on_lock

logger = logging.getLogger(__name__)

#: 匹配分权重（**可解释**：命中哪个字段得多少分，都写进 `match_reason`）。
#: 标题权重最高 —— 话题词出现在商品标题里是最强的相关性信号；
#: 类目次之（品类对得上）；卖点词再次（平台按该商品给的卖点，相关但不是「主题」）。
TITLE_WEIGHT = 100
CATEGORY_WEIGHT = 40
SELLER_POINT_WEIGHT = 20
#: 平台热度项：热度是**量级差很大**的值（2794 vs 438），直接相加会被它主导排序，
#: 故压成对数档（`hot//500`，最多贡献 ~50）—— **价格不主导、只做微调**。
HOT_VALUE_DIVISOR = 500
HOT_VALUE_CAP = 50


def stat_date_of(moment: Any) -> str:
    """快照统计日（`YYYY-MM-DD`，与两张指标快照表同口径）。"""
    return moment.strftime("%Y-%m-%d")


def _seller_point_terms(item_id: str, attributes: dict[str, Any]) -> list[str]:
    """从平台属性面取卖点词（`seller_point.seller_point_info`）。"""
    seller = attributes.get("seller_point") or {}
    terms = seller.get("seller_point_info") or []
    return [str(t).strip() for t in terms if str(t).strip()]


def parse_topics(payload: Any) -> list[dict[str, Any]]:
    """从 `aigc_publish_attributes` 响应里抽出话题（**口径唯一实现**）。

    ⚠️ **返回空列表 ≠ 出错**：平台可以合法地不给话题（新商品 / 无相关热度词）。
    调用方据此决定「不落库」还是「落库 0 条」—— **不许**用别处的词填充（编造）。

    ⚠️ `type=2`（活动话题）**平台不给 `hot_value`** ⇒ 落 `0`（**不猜热度**）。
    """
    if not isinstance(payload, dict):
        return []
    # ⚠️ **两种载体都要吃**（2026-10-04 实测踩坑，同一层错踩了两次）：
    #   ① 完整响应 `{"code":…, "data": {"topic_list": …}}`（适配器返回形态）
    #   ② 已解包的 `data` 本身 `{"topic_list": …}`（调用点传进来的形态）
    # 只认一种 ⇒ 另一种静默返回空列表（**HTTP 200 + 0 条**，看起来像「平台没给词」，
    # 实际是我们取错层 —— 最隐蔽的一类静默退化）。
    data = payload.get("data")
    data = data if isinstance(data, dict) else payload
    topic_list = data.get("topic_list")
    if not isinstance(topic_list, dict):
        return []
    rows: list[dict[str, Any]] = []
    for raw in topic_list.get("topic_list_info") or []:
        if not isinstance(raw, dict):
            continue
        topic = str(raw.get("topic") or "").strip()
        if not topic:
            # 空话题词落库没有意义（且会让唯一键出现空串行）。
            continue
        rows.append(
            {
                "topic": topic,
                "topic_type": _as_int(raw.get("type")) or 1,
                "hot_value": _as_int(raw.get("hot_value")),
            }
        )
    return rows


def _as_int(value: Any) -> int:
    """宽容转整数（缺值 ⇒ 0，与全项目缺值口径一致）。"""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return 0
    return 0


def relevance_for(
    topic: str, product: ShopProduct, *, hot_value: int, seller_points: list[str]
) -> tuple[int, str]:
    """算一个话题对某商品的**相关度**，并给出**可解释成分**。

    返回 `(分数, 命中说明)`。命中说明会同时出现在 `match_reason` 里 ——
    排序被质疑时靠它回答「为什么它排第一」。

    ⚠️ **话题词为空 ⇒ 0 分**（不得因为「长度 0 是任何串的子串」而满命中）。
    """
    keyword = (topic or "").strip()
    if not keyword:
        return 0, ""

    hits: list[str] = []
    score = 0

    title = (product.item_title or "")
    if keyword in title:
        score += TITLE_WEIGHT
        hits.append("标题")

    category = (product.category or "")
    if keyword in category:
        score += CATEGORY_WEIGHT
        hits.append("类目")

    matched_points = [term for term in seller_points if keyword in term or term in keyword]
    if matched_points:
        score += SELLER_POINT_WEIGHT
        hits.append(f"卖点({'/'.join(matched_points[:3])})")

    # 平台热度项（对数档，不主导排序）。
    hot_bonus = min(hot_value // HOT_VALUE_DIVISOR, HOT_VALUE_CAP)
    score += hot_bonus

    reason_parts = []
    if hits:
        reason_parts.append("命中 " + "、".join(hits))
    if hot_bonus:
        reason_parts.append(f"平台热度 +{hot_bonus}")
    reason = "; ".join(reason_parts) or "仅平台推荐（无词面命中）"
    return score, reason


def collect_topics_for_item(
    db: Session,
    *,
    user_id: int,
    item_id: str,
    attributes: dict[str, Any],
    platform_account_id: int | None = None,
    stat_date: str | None = None,
) -> int:
    """把一次属性面响应的话题落库（覆盖语义），返回写入条数。

    ⚠️ **`attributes` 为空 ⇒ 写 0 条**（不是「写一堆 0 分」）：
    平台没给话题就是没给，**不许**用别处的词填充（硬约束⑤）。
    """
    rows = parse_topics(attributes)
    if not rows:
        return 0

    day = stat_date or stat_date_of(shanghai_now())
    product = db.scalars(
        select(ShopProduct).where(
            ShopProduct.user_id == user_id, ShopProduct.item_id == item_id
        )
    ).first()
    title = (product.item_title if product else "") or ""

    written = 0
    for row in rows:
        statement = sqlite_insert(HotTopic).values(
            user_id=user_id,
            item_id=item_id,
            item_title=title,
            topic=row["topic"],
            topic_type=row["topic_type"],
            hot_value=row["hot_value"],
            platform_account_id=platform_account_id,
            stat_date=day,
        )
        # 冲突 ⇒ 覆盖（同日刷新取最新热度），**不是追加**。
        statement = statement.on_conflict_do_update(
            index_elements=["user_id", "item_id", "topic", "stat_date"],
            set_={
                "hot_value": statement.excluded.hot_value,
                "topic_type": statement.excluded.topic_type,
                "item_title": statement.excluded.item_title,
                "updated_at": shanghai_now(),
            },
        )
        retry_on_lock(lambda s=statement: db.execute(s), what=f"热点话题落库（{item_id}）")
        written += 1

    retry_on_lock(db.commit, what=f"热点话题提交（user_id={user_id}）")
    return written


def match_topics_to_goods(
    db: Session,
    *,
    user_id: int,
    attributes: dict[str, Any],
    item_id: str,
    stat_date: str | None = None,
) -> int:
    """把话题与**该商品**匹配并落 `topic_goods_match`，返回写入条数。"""
    rows = parse_topics(attributes)
    if not rows:
        return 0

    product = db.scalars(
        select(ShopProduct).where(
            ShopProduct.user_id == user_id, ShopProduct.item_id == item_id
        )
    ).first()
    if product is None:
        # 商品不在台账 ⇒ 不匹配（匹配结果没有指向对象，落库没有意义）。
        return 0

    seller_points = _seller_point_terms(item_id, attributes)
    day = stat_date or stat_date_of(shanghai_now())

    written = 0
    for row in rows:
        score, reason = relevance_for(
            row["topic"], product, hot_value=row["hot_value"], seller_points=seller_points
        )
        statement = sqlite_insert(TopicGoodsMatch).values(
            user_id=user_id,
            item_id=item_id,
            product_pk=product.id,
            topic=row["topic"],
            relevance_score=score,
            hot_value=row["hot_value"],
            match_reason=reason,
            stat_date=day,
        )
        statement = statement.on_conflict_do_update(
            index_elements=["user_id", "topic", "item_id", "stat_date"],
            set_={
                "relevance_score": statement.excluded.relevance_score,
                "hot_value": statement.excluded.hot_value,
                "match_reason": statement.excluded.match_reason,
                "product_pk": statement.excluded.product_pk,
            },
        )
        retry_on_lock(lambda s=statement: db.execute(s), what=f"话题匹配落库（{item_id}）")
        written += 1

    retry_on_lock(db.commit, what=f"话题匹配提交（user_id={user_id}）")
    return written


def list_matches(
    db: Session, *, user_id: int, item_id: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """列出匹配结果，**按相关度降序**。

    ⚠️ **同分要有稳定次序**（`item_id` + `topic` 兜底）：
    只按分数排时同分项顺序由 DB 决定 ⇒ 每次刷新列表乱跳，用户会以为数据在变。
    """
    statement = (
        select(TopicGoodsMatch)
        .where(TopicGoodsMatch.user_id == user_id)
        .order_by(
            TopicGoodsMatch.relevance_score.desc(),
            TopicGoodsMatch.hot_value.desc(),
            TopicGoodsMatch.item_id.asc(),
            TopicGoodsMatch.topic.asc(),
        )
        .limit(limit)
    )
    if item_id:
        statement = statement.where(TopicGoodsMatch.item_id == item_id)

    return [
        {
            "topic": row.topic,
            "item_id": row.item_id,
            "product_pk": row.product_pk,
            "relevance_score": row.relevance_score,
            "hot_value": row.hot_value,
            "match_reason": row.match_reason,
            "stat_date": row.stat_date,
        }
        for row in db.scalars(statement).all()
    ]


def list_topics(
    db: Session, *, user_id: int, item_id: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """列出已采集的热点话题，按平台热度降序（同热度用话题词兜底保稳定）。"""
    statement = (
        select(HotTopic)
        .where(HotTopic.user_id == user_id)
        .order_by(
            HotTopic.hot_value.desc(), HotTopic.topic.asc(), HotTopic.item_id.asc()
        )
        .limit(limit)
    )
    if item_id:
        statement = statement.where(HotTopic.item_id == item_id)

    return [
        {
            "topic": row.topic,
            "item_id": row.item_id,
            "item_title": row.item_title,
            "topic_type": row.topic_type,
            "hot_value": row.hot_value,
            "stat_date": row.stat_date,
        }
        for row in db.scalars(statement).all()
    ]


__all__ = [
    "CATEGORY_WEIGHT",
    "HOT_VALUE_CAP",
    "HOT_VALUE_DIVISOR",
    "SELLER_POINT_WEIGHT",
    "TITLE_WEIGHT",
    "collect_topics_for_item",
    "list_matches",
    "list_topics",
    "match_topics_to_goods",
    "parse_topics",
    "relevance_for",
    "stat_date_of",
]
