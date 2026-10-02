from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


class ShopProduct(Base):
    """千帆店铺商品台账（T-802，「同步店铺商品」落库）。

    **为什么落库**：「同步店铺商品」要的是本地台账（离线可查、可检索、供商品
    笔记生成取用），与 `GET /xhs/qianfan/items` 的「看当下状态」是两种用途，
    两者并存、不互相替代。

    **归属键 = `platform_account_id`（ADR-021 / 设计 18，迁移 `a1b2c3d4e5f6`）**：
    商品归**账号**所有，不归某个店铺实体。被取代的旧结论原句保留：

    *「**为什么是 `shop_id_fk` 而不是绑定 ID**：商品属于店铺实体，不属于某条
    绑定（企业店主 + 多子账号读同一个店，商品是同一批）。`shops` 表在 T-32
    已落地为 N:M 实体表，可解析。」*

    ⇒ 2026-09-30 用户口径「**所有数据以小红书账号做数据隔离**」：`shops` 退场，
    商品直接挂账号。原先「一个店铺的商品是同一批」的前提已随 N:M 被否决而消失
    （2026-09-22 已拍板不允许同店多账号），所以按账号隔离才是唯一正确的粒度。

    **`platform_account_id` 不允许空**：一个商品必须属于一个账号；定位不到账号
    ⇒ 同步失败留任务行说明原因，**不编账号**（缺值口径，不造假）。
    """

    __tablename__ = "shop_products"
    __table_args__ = (
        UniqueConstraint(
            "platform_account_id", "item_id", name="uq_shop_products_account_item"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    #: 指向 `platform_accounts.id`。**不允许空**（ADR-021：归属键即隔离键）。
    platform_account_id: Mapped[int] = mapped_column(
        ForeignKey("platform_accounts.id"), index=True, nullable=False
    )
    #: 千帆商品 ID（`search_item_v2` 条目的 `item_id`）。
    item_id: Mapped[str] = mapped_column(String(64), default="")
    item_title: Mapped[str] = mapped_column(String(512), default="")
    #: **用字符串存**，避免浮点误差；展示与校验按原样比对（设计 13 §3.1）。
    price: Mapped[str] = mapped_column(String(32), default="")
    #: 库存。千帆字段缺值时留空（nullable），不造 0。
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 销量。同上。
    sales: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 分类路径（千帆 `cate_path`）。缺值留空。
    category: Mapped[str] = mapped_column(String(128), default="")
    #: 商品图列表 JSON（`[]`）。
    images_json: Mapped[str] = mapped_column(Text, default="[]")
    #: **状态标签原文**（不写死枚举）：千帆的状态枚举会变，白名单映射集中在
    #: SDK 层（`apis/xhs_qianfan_apis.py::QIANFAN_ITEM_STATUS_LABELS`），不写死在表里。
    status: Mapped[str] = mapped_column(String(32), default="")
    #: 本次同步时间。**只在完整同步成功后才删旧行**（未扫完就删 = 丢数据）。
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
    #: **平台列表里已经没有这件商品**的对账标记时间（设计 13 §24，2026-09-22）。
    #: `NULL` = 正常；非空 = 最近一次对账发现「本地有、平台无」。**不删行**，
    #: 平台重新返回时自愈清回 `NULL`。
    platform_missing_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: 千帆条目**整条原文**（字段集还会变，真值留本地；新增展示项不需要改表）。
    raw_json: Mapped[str] = mapped_column(Text, default="{}")
    #: **SKU 明细归一化 JSON**（设计 13 §19.4.2 白名单；来源
    #: `get_skus_by_item_ids`，无签名端点）。**空串 = 没补过详情** —— 与
    #: 「补过但 0 SKU」是两件事，后者也会写 `[]`。
    detail_json: Mapped[str] = mapped_column(Text, default="")
    #: SKU 数（缺值留 null，不造 0）。
    sku_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 详情补齐时间。**NULL = 还在待补队列**（§19.4.2 的取数判据）；
    #: 不得把「没补过」渲染成「已同步」。
    detail_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # ------------------------------------------------------------------ #
    # 以下九列：商品列表**本地化**（设计 13 §30.3.3，v1.32）。展示字段由
    # `raw_json` 现场派生（契约不变）；这九列只服务**筛选 / 排序** ——
    # SQL 的 WHERE / ORDER BY 认列不认 JSON。老行由同步任务的「字段补足
    # pass」从 `raw_json` 本地回填（零平台请求）；平台没返回的字段留空。
    # ------------------------------------------------------------------ #
    #: 货号（关键词筛选 + 展示）。
    article_no: Mapped[str] = mapped_column(String(64), default="")
    #: 平台状态枚举（`card_type`，状态筛选用）。`status` 列是展示标签，
    #: 两者**不互推**（标签会随 SDK 白名单变，枚举不会）。
    card_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: 价格区间（**分**，与 `price` 文本列同口径；列形式服务区间筛选）。
    price_min_fen: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    price_max_fen: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: 创建时间（平台原文是**毫秒**，原样存；筛选 + 默认排序）。
    create_time_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: 上架 / 下架 / 审核时间（毫秒；排序字段，平台没给就留 NULL）。
    on_shelf_time_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    off_shelf_time_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_audit_time_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: 近 30 天销量（`item_sale_qty_30` 排序；`sales` 列是累计，不混用）。
    sales_30d: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
