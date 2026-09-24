from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


class Shop(Base):
    """千帆店铺实体（**T-32 起落地**）。

    为什么需要这张表：原来「账号 → 店铺」直接存在**账号行**上（`shop_user_id` /
    `shop_name`），并用 `UNIQUE(platform_account_id)` 把它锁成 **1:1** —— 一个账号
    只能读一个店铺。所以把店铺抽成实体，关系收成账号侧的一个外键
    （`platform_accounts.shop_id_fk`），店铺名 / 卖家 ID 只留在这里一份。

    ⚠️ **被取代的旧动机（原句保留）**：当时抽店铺表的理由是 *「企业店主 + 多子账号」
    的真实形态是 N:1：多个子账号要读同一个店铺*。**2026-09-22 用户拍板否掉了 N:1**：
    *不会再出现「同店铺、多账号」场景，一个店铺下只需存多个平台的会话令牌*。
    ⇒ 关系回到 **1:1**，并且这次**由 DB 强制**（`shop_id_fk` 带 `unique=True`）；
    「一个店铺被几个账号读」不再是产品事实，`shop_account_count` 随之从接口移除。
    抽店铺实体这件事本身保留 —— 店铺名 / 卖家 ID 是真值，不该冗余在账号行上。

    ⚠️ 2026-09-22：原来的关系表 `shop_account_bindings` **整表移除**（用户拍板）。
    它当年同时兼任「关系 / 千帆凭据仓库 / 同步状态机」三件事，是「一个语义两份
    真相源」的温床 —— 拆开后：关系→`platform_accounts.shop_id_fk`、凭据→
    `account_cookie_versions` 的 `ark` 通道、状态与游标→`platform_accounts.shop_state`。

    **只建现在有消费者的字段**（不写没人读的列）：
    `capabilities_json` / `capabilities_verified_at` / `shop_type` / `raw_json`
    属 M3 能力探测（T-301~T-305），现在没有任何代码读它们，等那时再加。

    **唯一约束用 `(user_id, seller_id)` 而不是设计 §9.1 计划的 `(seller_id, shop_id)`**：
    千帆 `GET /api/edith/seller/info/v2` 只回 `user_id`（卖家 ID，本表 `seller_id`）
    与 `seller_name`，**没有独立的店铺 ID**（实测白名单见
    `api/platforms/xhs/qianfan.py::_shop_from_seller_info`）。`shop_id` 先留空 +
    加 `user_id` 前缀（同一平台卖家 ID 在不同用户下是不同的实体），等 M2 拿到店铺 ID
    再把约束收紧到 `(seller_id, shop_id)`。
    """

    __tablename__ = "shops"
    __table_args__ = (
        UniqueConstraint("user_id", "seller_id", name="uq_shops_user_seller"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    #: 千帆侧卖家 `user_id`（实测形如 `6a3250e427a0000000000000`）。
    seller_id: Mapped[str] = mapped_column(String(64), default="")
    #: 平台侧店铺 ID —— 千帆当前接口**不返回**，留空待 M2 补齐（不留空则宁可为空，不造假）。
    shop_id: Mapped[str] = mapped_column(String(64), default="")
    shop_name: Mapped[str] = mapped_column(String(128), default="")
    #: `active` / `suspended` / `closed`（设计 §9.1）。当前只有 `active` 一种来源。
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=shanghai_now, onupdate=shanghai_now
    )


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
    """**笔记 ↔ 商品** 多对多关系（本地实体级，设计 13 §26，2026-09-22）。

    三张表各司其职（用户口径：「笔记相关得信息单独一张表，商品信息单独一张表，
    笔记与商品之间得关系单独一张关系表」）：

    * `notes` 装**笔记信息**（本地 PK = `notes.id`）；
    * `shop_products` 装**商品信息**（本地 PK = `shop_products.id`）；
    * 本表只装**关系** —— 一篇笔记可挂多个商品、一个商品可被多篇笔记挂载（M2M），
      由唯一键 `(note_id, product_id)` 强制（重复解析幂等，不产生重复行）。

    ⚠️ 与 `shop_note_goods` **不是同一语义的两份实现**：后者是**平台证据缓存**
    （按账号 + 平台 note_id 存千帆原文，回答「千帆说这条笔记挂了什么」）；
    本表是**本地实体关系**（回答「本地这篇笔记挂了本地哪些商品」）。
    解析器（`shop_goods_service.link_note_goods`）只读缓存、只写本表，**零平台请求**。

    **只增不删**（§23 / §25.3：同步链路不做删除维度）—— 平台撤了挂载也不删关系行，
    删除语义由对账负责。⚠️ **2026-09-23 两条用户口径 ⇒ 补两个例外，且方向一致**：

    * 「删除笔记时需要删除笔记与商品的关联关系」⇒ **删笔记**时关系行一并删
      （`purge_note` 里显式 `delete(NoteGoods)`）。真实库曾因此留 4 条孤儿
      （note_id 1963/1964/1965/1967 → product_id 1）。
    * 「删除商品时也需要删除笔记与商品的关联关系」⇒ **删商品**时关系行一并删
      （`shop_products_service.delete_items`：先取将删行的本地 `id`，再按
      `product_id` 删关系，最后删商品行 —— **顺序不能反**，删了就对不上了）。

    即**本地实体真删 ⇒ 关系行跟着走**，两个方向同一条规则。⚠️ 第二条把 v1.36
    （§33.10）「删商品时关系行不动、等同步写回后原样接上」的决定**推翻**了 ——
    用户明确要求改，理由也站得住：关系指向一条已删商品就是孤儿行，与删笔记
    留孤儿是同一个病的另一个方向。

    ⚠️ 与 `shop_goods_sync_service._drop_stale_rows` **刻意不同**：那个由
    「完整同步成功后平台已不存在的商品」触发，删的是**平台侧已消失**的行；
    用户口径只针对**显式删除**，同步侧的连带删除本轮不动（要改需另议，§33.12 待办）。

    两个外键仍**不级联删**（`PRAGMA foreign_keys` 未开，级联本来就不生效）
    ⇒ 删除链必须在代码里显式写全：`purge_note`（笔记）与 `delete_items`（商品）
    **各自**都要把关系行写进自己的删除链，漏一边就是孤儿数据。
    """

    __tablename__ = "note_goods"
    __table_args__ = (
        UniqueConstraint("note_id", "product_id", name="uq_note_goods_note_product"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    #: 本地笔记行（`notes.id`）—— **本地主键，不是平台 note_id**。
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id"), index=True)
    #: 本地商品行（`shop_products.id`）—— **本地主键，不是平台 item_id**。
    product_id: Mapped[int] = mapped_column(ForeignKey("shop_products.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
