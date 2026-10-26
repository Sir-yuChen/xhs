"""设计 75 S5 收尾：本地台账模型整段退场（商品 / 订单 / 笔记↔商品关系）。

用户口径（2026-10-26）：「不再同步这些数据到本地，转变为展示线上数据，实时查询线上数据」
⇒ 三域数据源改为平台实时查询（`services/realtime_platform_query_service.py` 是唯一实现），
本地表随迁移 `n8o9p0q1r2s3` 整表 DROP。

⚠️ **ORM 类必须与迁移同批删除**：留着类，`Base.metadata.create_all()` 会把迁移刚
DROP 掉的三张表**建回来**（空表）⇒ 之后任何一次 `create_all` 都会让「表已删」
这个事实在库与代码之间对不上，且 `alembic autogenerate` 会一直漂。

被删的类与被取代的关系（原句保留在 `docs/02_设计文档/75_设计_去本地化实时查询.md`
§2.4 与 `docs/02_设计文档/33_…`，这里只留结论）：

| 原表 | 现在从哪来 |
|---|---|
| `shop_products` | `rt.list_products` / `rt.get_product_detail`（平台实时） |
| `shop_note_goods` | `rt.list_goods_notes(item_id)`（`goods-note/list`） |
| `note_goods` | 同上 —— 挂链关系不在本地维护 |
| `orders` / `order_items` / `order_shipments` / `after_sales` | `rt.list_orders` / `rt.get_order_detail` |
"""
