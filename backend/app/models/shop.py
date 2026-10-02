from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


"""千帆侧模型：**店铺已不再是实体**（ADR-021 / 设计 18）。

本文件曾含 `Shop` 表（T-32 的店铺实体）。2026-09-30 用户口径「所有数据以
小红书账号做数据隔离」⇒ `shops` 表整表退场、`seller_id` 下沉为
`platform_accounts` 的字段（迁移 `a1b2c3d4e5f6`）。此处只保留两张**按账号
归属**的关系表：

* `shop_note_goods` —— 商品笔记的本地映射（商品挂链）
* `note_goods`     —— 笔记 ↔ 商品 多对多（平台 ID 级）

被取代的旧结论原句保留：*「千帆店铺实体（T-32 起落地）……店铺名 / 卖家 ID 是
真值，不该冗余在账号行上。」* —— 该判断的前提（店铺是独立实体、可被多账号
引用）已随 2026-09-22「不允许同店多账号」否决而消失，故实体退场。
"""


class ShopNoteGoods(Base):
    """千帆「商品笔记」列表的本地映射：`note_id` → 挂载商品。

    为什么要有这张表，而不是每次请求现查千帆：千帆的商品笔记列表是
    **按页**给的（`page_num` / `page_size`），而「我的笔记」一页只有 10 条，
    若为了这 10 条去翻千帆的页，等于把翻页成本摊到每一次浏览上。
    所以：**同步一次、本地按 note_id join**。

    ⚠️ 「千帆没有按 `note_id` 查询的口子」是**早前结论，已被实测取代**：
    参数名是**复数** `note_ids`（单数 `note_id` 会被忽略），详见
    `services/shop_goods_service.py` 的实测表与设计文档 §24.1。
    """

    __tablename__ = "shop_note_goods"
    __table_args__ = (
        #: 映射按**账号**挂（2026-09-22 起）：原键是 `(binding_id, note_id)`，换列
        #: **不改语义** —— 绑定当时就是「账号级」的（一个账号最多一条在用绑定），
        #: 所以 `(platform_account_id, note_id)` 与它一一对应。
        UniqueConstraint("platform_account_id", "note_id", name="uq_shop_note_goods_account_note"),
        #: ⚠️ **索引名刻意不跟随列名**（列叫 `platform_account_id`，索引却叫 `..._account_id`）：
        #: 重建表时（迁移 `m5n6o7p8q9r0`）用的就是这个名字，真实库与它一致。若这里改回
        #: SQLAlchemy 的默认名（`ix_shop_note_goods_platform_account_id`），模型与库就会各说一套，
        #: 下次 `alembic revision --autogenerate` 会凭空多出一条改索引的迁移。
        #: ⇒ **一份真相源**：以库里已有的名字为准，显式声明（**别"顺手改成列名"**）。
        Index("ix_shop_note_goods_account_id", "platform_account_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    #: 这条映射属于哪个账号（千帆 jar 的持有者）。
    #: ⚠️ 与上一条同因：`index=True` 会生成 `ix_shop_note_goods_platform_account_id`，
    #: 故此处**不开** `index=True`，索引由 `__table_args__` 显式声明。
    platform_account_id: Mapped[int] = mapped_column(ForeignKey("platform_accounts.id"))
    note_id: Mapped[str] = mapped_column(String(64), index=True)
    item_id: Mapped[str] = mapped_column(String(64), default="")
    item_title: Mapped[str] = mapped_column(Text, default="")
    item_image: Mapped[str] = mapped_column(Text, default="")
    variant_desc: Mapped[str] = mapped_column(String(255), default="")
    #: 千帆返回的 `goods_infos[]` 原文（一条笔记可能挂多个商品）。
    goods_json: Mapped[str] = mapped_column(Text, default="[]")
    #: 千帆条目的**整条原文**（标题 / 封面 / 四项互动 / 阅读数 / 30 日阅读 /
    #: 支付金额与订单数 / 发布状态 / 发布时间 / xsec_token…）。
    #: 存整条而不是逐列建模的原因：这些字段只用于展示，且千帆的字段集还在变；
    #: 真值保留在本地，新增展示项不需要再打一次平台、也不需要改表。
    entry_json: Mapped[str] = mapped_column(Text, default="{}")
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)


class NoteGoods(Base):
    """**笔记 ↔ 商品** 多对多关系（**平台 ID 级**，设计 13 §26.7，2026-09-27 改键）。

    三张表各司其职（用户口径：「笔记相关得信息单独一张表，商品信息单独一张表，
    笔记与商品之间得关系单独一张关系表」）—— 分工不变，**只有本表的键换了**：

    * `notes` 装**笔记信息**（身份 `(user_id, note_id)`，`note_id` 是平台 ID）；
    * `shop_products` 装**商品信息**（身份 `(platform_account_id, item_id)`，`item_id` 是平台 ID；
      ADR-021：归属键是账号，不再是 `shop_id_fk`）；
    * 本表只装**关系** —— 一篇笔记可挂多个商品、一个商品可被多篇笔记挂载（M2M），
      由唯一键 `(user_id, note_id, item_id)` 强制（重复解析幂等，不产生重复行）。

    ⚠️ **键为什么从本地主键改成平台 ID**（用户 2026-09-27 口径，**取代** 2026-09-22 的
    「两边都解析成功才建关系」）：

    * 原文：「同步笔记时，如果笔记挂有商品必须将笔记与商品的关系落库到笔记与商品
      关系表，**无需校验商品是否存在**」＋「**关系表改存平台商品 ID 和平台笔记 ID**」；
    * 旧键 `(notes.id, shop_products.id)` 是**本地主键** ⇒ 商品台账里没有这件商品就
      **写不进关系**（外键指向不存在的行），而 `shop_note_goods` 缓存里明明有它的
      `item_id`（真实库实测：1933 个挂载条目里 **16 个** 因台账缺行永远落不了库）；
    * 新键不依赖台账、也不依赖店铺（未绑店的账号同样能落）⇒「笔记挂了什么就落什么」。

    **结构上没有孤儿**：两个外键**全部去掉**（不再指向 `notes.id` / `shop_products.id`）
    ⇒ 「关系指向一条已删本地行」这种数据自相矛盾在结构上不可能出现。

    ⚠️ 与 `shop_note_goods` **不是同一语义的两份实现**：后者是**平台证据缓存**
    （按账号 + 平台 note_id 存千帆原文，回答「千帆说这条笔记挂了什么」）；
    本表是**关系**（回答「这条笔记挂了哪些商品」）。解析器
    （`shop_goods_service.link_note_goods`）只读缓存、只写本表，**零平台请求**。

    **只增不删**（§23 / §25.3：同步链路不做删除维度）—— 平台撤了挂载也不删关系行。
    ⚠️ **2026-09-23 两条用户口径 ⇒ 补两个例外，且方向一致**（2026-09-27 仍现行）：

    * 「删除笔记时需要删除笔记与商品的关联关系」⇒ **删笔记**时关系行一并删
      （`purge_note` 里显式 `delete(NoteGoods)`，按 `(user_id, note_id)`）；
    * 「删除商品时也需要删除笔记与商品的关联关系」⇒ **删商品**时关系行一并删
      （`shop_products_service.delete_items`：⚠️ 2026-09-27 起直接按 `item_id` 删，
      **不再需要**旧实现里「先取本地 `id` → 再按 `product_id` 删」那一步顺序纪律）。

    即**本地实体真删 ⇒ 关系行跟着走**，两个方向同一条规则（v1.36 的「删商品时关系行
    不动、等同步写回后原样接上」已被 2026-09-23 口径推翻，原句保留在 §33.10.2）。

    ⚠️ 与 `shop_goods_sync_service._drop_stale_rows` **刻意不同**：那个由
    「完整同步成功后平台已不存在的商品」触发，删的是**平台侧已消失**的台账行；
    用户口径只针对**显式删除**，同步侧的连带删除本轮不动（要改需另议，§33.12 待办）。
    平台侧商品消失后关系行**留着**（按 `item_id` 仍然成立），读取时退化成只显示 `item_id`。
    """

    __tablename__ = "note_goods"
    __table_args__ = (
        UniqueConstraint("user_id", "note_id", "item_id", name="uq_note_goods_user_note_item"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    #: **归属**（DEF-066 同款纪律）。`notes` 的身份键是 `(user_id, note_id)`，本表对齐。
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    #: **平台笔记 ID**（`notes.note_id`；本地生成笔记是 `local_…` 合成 ID，发布成功后
    #: 会随 `notes.note_id` 一起被改写成平台真 ID —— 见 `note_publish_service`）。
    note_id: Mapped[str] = mapped_column(String(128), index=True)
    #: **平台商品 ID**（`shop_products.item_id`；台账里没有这件商品也照样有值）。
    item_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
