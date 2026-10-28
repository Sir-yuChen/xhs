"""热点数据模型（已退场，2026-10-28）。

平台热度词来源：`POST /api/edith/aigc-publish/attributes`（只读，按 `item_id` 调用）
返回 `data.topic_list.topic_list_info[]` —— `type=1` 带 `hot_value`、`type=2` 是活动话题。
它是「与该商品相关」的热度词，**不是全站热搜总榜**（`apis/` 层无 `hot_search` 类方法）。

本文件原有 `HotTopic`（`hot_topics`）与 `TopicGoodsMatch`（`topic_goods_match`），均已退场。
"""

from __future__ import annotations

#: `HotTopic`（`hot_topics`）已退场：4 条 `platform-hot-topics` 端点前端零调用，
#: 唯一「像在读它」的 `GET /xhs/analytics/hot-topics` 读的是 `note_topics` + `raw_json`，
#: 33 行数据从未被任何界面消费 ⇒ 迁移 `u7v8w9x0y1z2` DROP。
#: 热度词功能仍在 `product_note_gen_service._build_aigc_context()`（现取现用，不落库）。
#:
#: `TopicGoodsMatch` 更早已退场（设计 75 S5）：它挂 `shop_products.id`，台账 DROP 后失去载体。
