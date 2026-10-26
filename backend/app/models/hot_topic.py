"""热点驱动生成的数据模型（设计 17 §13.11，M3 方向 2，2026-10-04）。

## 数据源真机结论（决定了这两张表的形状）

平台侧**有**真实热度来源：`POST /api/edith/aigc-publish/attributes`（只读）
返回 `topic_list.topic_list_info[]` —— `{"topic": "餐垫", "type": 1, "hot_value": 4673}`，
`type=1` 带平台 `hot_value`、`type=2` 是活动话题（无热度）。

⚠️ **该接口按 `item_id` 调用** ⇒ 话题是「**与该商品相关**的平台热度词」，
**不是**全站热搜总榜 ⇒ 表主键必须带 `item_id`（否则会把不同商品的话题混成一锅）。

⚠️ **没有「不选商品拉全站热搜榜」的能力**（`apis/` 层实测无 `hot_search` /
`trending` / `rank_list` 类方法）。若将来找到该入口，再加一张「全站榜」表，
**不要**改本表的语义（那会让历史数据的口径无法解释）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


class HotTopic(Base):
    """平台**按商品推荐**的热度话题快照（设计 17 §13.11.1）。

    ## 判据

    | 列 | 语义 | 缺值口径 |
    |---|---|---|
    | `item_id` | 平台商品 ID | ⚠️ **必填** —— 话题是「与该商品相关」的，脱离商品无意义 |
    | `topic` | 话题词 | 空串 ⇒ **不落库**（无意义行）|
    | `topic_type` | `1`=平台热度词 / `2`=活动话题 | 平台原值，不转换 |
    | `hot_value` | 平台给的**热度**（`type=2` 无此字段 ⇒ `0`）| 缺 ⇒ `0`（不猜、不排序时当 0）|
    | `stat_date` | 采集日（`YYYY-MM-DD`）| 字典序即时间序 |

    ⚠️ **唯一键 `(user_id, item_id, topic, stat_date)`**：同日同商品同话题**只一行**，
    重复刷新**覆盖**（`hot_value` 会变）—— 不是追加（追加会让热榜每次刷新翻倍）。

    ⚠️ **`hot_value` 是平台原值，不做归一化**：归一化会让「平台热度 4673」变成
    「0.87 分」这种看不出原始含义的数，用户无法与平台后台对账。
    """

    __tablename__ = "hot_topics"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "item_id", "topic", "stat_date", name="uq_hot_topic_item_topic_date"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    #: 平台商品 ID（`shop_products.item_id`）—— 话题是**按商品**推的，不是全站榜。
    item_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    #: 商品标题快照（展示用；商品改名不影响历史话题的可读性）。
    item_title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    topic: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    #: 平台 `type`：1 = 热度词（带 `hot_value`）/ 2 = 活动话题（无热度）。
    topic_type: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: 平台热度原值（`type=2` / 缺字段 ⇒ 0）。
    hot_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    platform_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("platform_accounts.id"), index=True, nullable=True
    )
    stat_date: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=shanghai_now, onupdate=shanghai_now
    )

# ⚠️ **已退场**（设计 75 S5，2026-10-27）：`TopicGoodsMatch`（`topic_goods_match`）整类删除。
#
# 它按 `product_pk`（FK → `shop_products.id`，nullable）记「话题 ↔ 商品」相关度；商品台账
# 随迁移 `n8o9p0q1r2s3` 整表 DROP ⇒ 这张表连 DROP 都过不去（FK 还在）。
#
# 功能侧：算相关度需要商品事实（标题 / 类目 / 卖点），原是本地台账给的；商品改实时后，
# 逐商品去查平台才能算，而它是列表页的派生排序表 —— 不值得为它打几十次平台请求。
# 平台热度词本身仍在（`hot_topics` + `GET /platform-analytics/platform-hot-topics`）。
