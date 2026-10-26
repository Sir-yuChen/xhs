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
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend.app.core.time import shanghai_now
from backend.app.models import HotTopic
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
    topic: str, product: dict[str, Any], *, hot_value: int, seller_points: list[str]
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

    #: ⚠️ 设计 75 S5：`product` 现在是**实时归一化 dict**（原为 `ShopProduct` 行）。
    title = str(product.get("title") or "")
    if keyword in title:
        score += TITLE_WEIGHT
        hits.append("标题")

    category = str(product.get("category") or "")
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
    #: ⚠️ 设计 75 S5（2026-10-27）：商品标题原来从本地台账取；台账表已 DROP。
    #: 话题行里的标题只是**展示用附带信息**，取不到就留空（缺值口径，不造假）——
    #: 不为一个展示字段再打一次平台（本函数在批量采集里被逐个商品调用）。
    title = ""

    written = 0
    for row in rows:
        statement = pg_insert(HotTopic).values(
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
    """⚠️ **已退场**（设计 75 S5，2026-10-27）：恒返回 0（保留签名，调用方无感）。

    原实现按本地台账行（`shop_products`）算「话题 ↔ 某商品」的可解释相关度
    （标题 / 类目 / 卖点命中），落 `topic_goods_match`。两处前提都没了：

    * 台账表与其主键 `product_pk` 已随迁移 `n8o9p0q1r2s3` 整表 DROP；
    * 「卖点」来自商品属性面，本地不再缓存。

    ⚠️ 为什么不是「改成实时查商品再算」：本函数在**批量采集**里逐商品调用
    （一次选 20 个商品 = 20 个话题集合），改成实时 = 20 次额外平台请求，
    而结论是一张**已经在别处展示过**的排序表。用户需要的是热度词本身
    （`collect_topics_for_item` 仍然正常工作），不是这张派生表。
    """

    return 0


def list_matches(
    db: Session, *, user_id: int, item_id: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """⚠️ **已退场**（设计 75 S5）：恒返回空列表（`topic_goods_match` 表已 DROP）。

    保留函数与返回形状，是为了让端点 / 前端不必为一个"要下线的能力"改契约。
    """
    return []


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
    "list_matches",
    "parse_topics",
    "relevance_for",
    "stat_date_of",
]
