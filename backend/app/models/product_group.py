"""商品分组（设计 20 §1.1，M4 方向 4）。

## 为什么需要它

设计 16 §5.4 的「达到效果」里要「本地分组」，而商品的本地台账（`shop_products`）
此前没有任何用户自组织的维度 —— 只能按平台给的状态 / 分类 / 价格区间筛。
分组让用户能把商品按**自己的经营意图**分堆（主推 / 清仓 / 上新 / 测款），
这是「商品编辑优化」里**唯一完全在本地、零平台请求、零风险**的一块。

## 挂 `user_id` 而不是 `platform_account_id`

分组是**用户的组织方式**，不是账号的属性 —— 用户可能把几个账号的商品放进同一个
「主推」分组里跨账号比较。商品侧的归属仍由 `shop_products.platform_account_id`
保证（不可越权），分组只按 `user_id` 隔离。

⚠️ 这条决定有一个**必须记住的连带后果**：`product_count`（分组里有几件商品）
要按「**当前用户的全部账号**」聚合，不能按某个账号过滤 —— 否则计数与列表对不上
（设计 20 §4.2，已在服务层与测试里钉住）。

## 删除分组

**不级联删商品**：分组是弱关联，删分组只把引用它的 `shop_products.group_id`
**清空**（商品一件不少）。清空由服务层显式做（`product_groups_service.delete_group`），
**不靠 FK** —— SQLite 的 `ADD COLUMN` 不支持带 FK 的列（迁移里已记），
且显式清空能在响应里报告「本次解除了 N 件商品的分组」，比 FK 的隐式行为可审计。

## 唯一键

`(user_id, name)`：同一用户下分组名不重复（重名会让「按分组筛」歧义 ——
筛出来的不知道是哪一堆）。跨用户允许同名（「清仓」谁都可以有）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


class ProductGroup(Base):
    """商品的本地分组（用户自建，纯本地组织方式）。"""

    __tablename__ = "product_groups"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_product_groups_user_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    #: 分组名（用户自取）。同一用户下唯一；跨用户允许同名。
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=shanghai_now, onupdate=shanghai_now
    )
