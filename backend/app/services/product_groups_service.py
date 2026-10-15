"""商品分组服务（设计 20 §4，M4 方向 4）—— **纯本地，零平台请求**。

## 这个模块为什么存在

商品的本地台账（`shop_products`）此前没有任何用户自组织的维度 —— 只能按平台给的
状态 / 分类 / 价格区间筛。分组让用户能把商品按**自己的经营意图**分堆
（主推 / 清仓 / 上新 / 测款），这是「商品编辑优化」里**唯一完全在本地、
零平台请求、零风险**的一块。

## 三条纪律（与 `shop_products_service` 同构）

* **零平台请求**：本模块**不许**碰适配器 / 发 HTTP。
* **租户隔离一律带 `user_id`**：分组是用户的（挂 `product_groups.user_id`），
  商品是账号的（`shop_products.platform_account_id`）—— 两套隔离键在
  `product_count` 里**必须同时**出现，少一个就会数到别人的商品。
* **缺值不造假**：分组下没有商品 ⇒ `product_count = 0`（**不是**把分组藏起来），
  用户可以建空分组先摆着。
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.models.product_group import ProductGroup
from backend.app.models.shop_product import ShopProduct


class ProductGroupError(RuntimeError):
    """分组操作的结构化错误（带 `code`，供 API 层映射 HTTP 状态）。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _account_ids_of(db: Session, user_id: int) -> list[int]:
    """用户的**全部**平台账号 id（`product_count` 按它聚合，设计 20 §4.2）。

    ⚠️ 必须是**全部账号**：分组挂 `user_id` 而不是 `platform_account_id`（一个分组
    可以装用户多个账号的商品）⇒ 按某个账号过滤会让计数与列表对不上。
    """
    from backend.app.models.platform_account import PlatformAccount

    return [
        int(row)
        for row in db.scalars(
            select(PlatformAccount.id).where(PlatformAccount.user_id == user_id)
        )
    ]


def _serialize(db: Session, group: ProductGroup) -> dict[str, Any]:
    """分组 → 前端契约（含 `product_count`，每次现算 —— 分组与商品行各自在变）。"""
    account_ids = _account_ids_of(db, group.user_id)
    count = 0
    if account_ids:
        count = int(
            db.scalar(
                select(func.count(ShopProduct.id)).where(
                    ShopProduct.group_id == group.id,
                    ShopProduct.platform_account_id.in_(account_ids),
                )
            )
            or 0
        )
    return {
        "id": group.id,
        "name": group.name,
        "product_count": count,
        "created_at": group.created_at.strftime("%Y-%m-%d %H:%M:%S")
        if group.created_at
        else "",
        "updated_at": group.updated_at.strftime("%Y-%m-%d %H:%M:%S")
        if group.updated_at
        else "",
    }


def list_groups(db: Session, *, user_id: int) -> list[dict[str, Any]]:
    """当前用户的分组列表（按 `id` 升序 —— 创建顺序，不按名字排序：名字会改）。"""
    rows = db.scalars(
        select(ProductGroup)
        .where(ProductGroup.user_id == user_id)
        .order_by(ProductGroup.id.asc())
    ).all()
    return [_serialize(db, row) for row in rows]


def create_group(db: Session, *, user_id: int, name: str) -> dict[str, Any]:
    """建分组（`name` 去空白后 1~64 字符）。

    | 约定 | 做法 |
    |---|---|
    | 名字为空 | 400 `PRODUCT_GROUP_NAME_REQUIRED`（不静默用「未命名」） |
    | 名字超长 | 400 `PRODUCT_GROUP_NAME_TOO_LONG` |
    | 同名 | 409 `PRODUCT_GROUP_NAME_EXISTS`（**不静默覆盖** —— 覆盖会让旧分组里的商品一夜换堆） |

    ⚠️ 同名判据有两层：服务层先 `SELECT`（能给出带「已有分组 id」的可读错误），
    唯一索引是**最后防线**（并发下仍可能撞上）⇒ `IntegrityError` 也回退到 409，
    **不抛 500**（同一语义，两个出口不能两个状态码）。
    """
    normalized = (name or "").strip()
    if not normalized:
        raise ProductGroupError(
            "分组名不能为空。", code="PRODUCT_GROUP_NAME_REQUIRED"
        )
    if len(normalized) > 64:
        raise ProductGroupError(
            f"分组名最多 64 个字符，本次 {len(normalized)} 个。",
            code="PRODUCT_GROUP_NAME_TOO_LONG",
        )

    existing = db.scalars(
        select(ProductGroup).where(
            ProductGroup.user_id == user_id, ProductGroup.name == normalized
        )
    ).first()
    if existing is not None:
        raise ProductGroupError(
            f"已有同名分组（id={existing.id}），请换个名字或编辑原分组。",
            code="PRODUCT_GROUP_NAME_EXISTS",
        )

    group = ProductGroup(user_id=user_id, name=normalized)
    db.add(group)
    try:
        db.commit()
    except IntegrityError as exc:
        # 并发下另一个请求抢先插了同名行 ⇒ 同一语义，退 409 不退 500。
        db.rollback()
        raise ProductGroupError(
            "已有同名分组（并发冲突），请刷新后重试。",
            code="PRODUCT_GROUP_NAME_EXISTS",
        ) from exc
    db.refresh(group)
    return _serialize(db, group)


def _get_owned_group(db: Session, group_id: int, user_id: int) -> ProductGroup:
    """取分组并校验归属；不属本用户 ⇒ `ProductGroupError`（404）。

    ⚠️ **报 404 不报 403**：403 会泄露「这个 id 存在、只是不是你的」，
    对一个只按 id 查的资源，404 是唯一不泄露存在性的答法。
    """
    group = db.get(ProductGroup, int(group_id))
    if group is None or group.user_id != user_id:
        raise ProductGroupError(
            "分组不存在或不属于当前用户。", code="PRODUCT_GROUP_NOT_FOUND"
        )
    return group


def rename_group(db: Session, *, group_id: int, user_id: int, name: str) -> dict[str, Any]:
    """改分组名（校验同 `create_group`，同名时 409）。"""
    group = _get_owned_group(db, group_id, user_id)
    normalized = (name or "").strip()
    if not normalized:
        raise ProductGroupError(
            "分组名不能为空。", code="PRODUCT_GROUP_NAME_REQUIRED"
        )
    if len(normalized) > 64:
        raise ProductGroupError(
            f"分组名最多 64 个字符，本次 {len(normalized)} 个。",
            code="PRODUCT_GROUP_NAME_TOO_LONG",
        )
    if normalized == group.name:
        # 名字没变：**不报错也不写库**（避免 `updated_at` 无意义跳动）。
        return _serialize(db, group)

    existing = db.scalars(
        select(ProductGroup).where(
            ProductGroup.user_id == user_id,
            ProductGroup.name == normalized,
            ProductGroup.id != group.id,
        )
    ).first()
    if existing is not None:
        raise ProductGroupError(
            f"已有同名分组（id={existing.id}），请换个名字。",
            code="PRODUCT_GROUP_NAME_EXISTS",
        )

    group.name = normalized
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ProductGroupError(
            "已有同名分组（并发冲突），请刷新后重试。",
            code="PRODUCT_GROUP_NAME_EXISTS",
        ) from exc
    db.refresh(group)
    return _serialize(db, group)


def delete_group(db: Session, *, group_id: int, user_id: int) -> dict[str, Any]:
    """删分组（**不删商品**：先把引用它的 `shop_products.group_id` 清空）。

    | 约定 | 做法 |
    |---|---|
    | 影响面 | 先 `COUNT` 再清，响应回传 `released`（让用户知道解除了几件商品的分组） |
    | 清空范围 | 限定在**当前用户的账号集合**内（`group_id` 是本地 id，跨用户理论不可能命中，带上是同 `delete_items` 的一致性纪律） |
    | 顺序 | **先清列后删行** —— 反了的话，中间态的查询会读到「分组不存在的 group_id」 |
    | 商品 | **一件不动**（分组是弱关联，删组织方式不删数据） |

    ⚠️ **不用 FK 的 `ON DELETE SET NULL`**：SQLite 的 `ADD COLUMN` 不支持带 FK 的列
    （迁移 `g0a1b2c3d4e5` 里已记），且显式清空能在响应里报告影响面，比 FK 的
    隐式行为可审计。
    """
    group = _get_owned_group(db, group_id, user_id)
    account_ids = _account_ids_of(db, user_id)

    released = 0
    if account_ids:
        released = int(
            db.scalar(
                select(func.count(ShopProduct.id)).where(
                    ShopProduct.group_id == group.id,
                    ShopProduct.platform_account_id.in_(account_ids),
                )
            )
            or 0
        )
        if released:
            db.execute(
                ShopProduct.__table__.update()
                .where(
                    ShopProduct.group_id == group.id,
                    ShopProduct.platform_account_id.in_(account_ids),
                )
                .values(group_id=None)
            )

    db.delete(group)
    db.commit()
    return {"ok": True, "id": group.id, "name": group.name, "released": released}


def edit_product(
    db: Session,
    *,
    platform_account_id: int,
    item_id: str,
    group_id: Optional[int] = None,
    ai_title: Optional[str] = None,
    ai_selling_point: Optional[str] = None,
    touched: Optional[dict[str, bool]] = None,
) -> dict[str, Any]:
    """商品本地编辑（设计 20 §2.1 端点 5 的执行体）—— **纯本地写，零平台请求**。

    ## 「不传 = 不动」vs「传 null = 清空」（PATCH 语义的核心）

    三个可编辑字段在 API 层用 **sentinel 默认值**（不是 `None`）区分两种语义：

    * 请求体**没有**这个键 ⇒ 保持原值（`touched[key] is False`）；
    * 请求体**显式**给 `null` ⇒ 清空（写成 `None`）。

    ⚠️ 直接写 `Optional[int] = None` 会把两件事混成一件 —— 这是 PATCH 语义最常见
    的静默错值（用户只想改分组，结果标题被清空了）。

    ## `updated[]`

    只回**真正变了**的字段（新值与旧值不同才算）。前端据此提示
    「已更新分组」而不是「已更新商品」—— 全字段都报「已更新」等于没说。

    ## 归属校验在 API 层（`_owned_account`）

    本函数收到的 `platform_account_id` 已经过 `_owned_account` 校验（含软删闸），
    这里只按它定位商品行；传别的账号的 `item_id` ⇒ 查不到行 ⇒ 404。
    """
    row = db.scalars(
        select(ShopProduct).where(
            ShopProduct.platform_account_id == platform_account_id,
            ShopProduct.item_id == str(item_id or "").strip(),
        )
    ).first()
    if row is None:
        raise ProductGroupError(
            "本账号的商品台账里没有这个商品（先同步商品再试）。",
            code="PRODUCT_ITEM_NOT_FOUND",
        )

    #: API 层已解析好的「哪些字段被显式传了」。
    flags = touched or {}
    updated: list[str] = []

    if flags.get("group_id"):
        # 显式传了 group_id（含 null = 移出分组）。校验它指向**自己的**分组
        # （传别人的分组 id ⇒ 404，不静默收下）。
        if group_id is not None:
            _get_owned_group(db, int(group_id), int(row.user_id))
        if row.group_id != group_id:
            row.group_id = group_id
            updated.append("group_id")
    if flags.get("ai_title"):
        value = (ai_title or "").strip() or None
        if row.ai_title != value:
            row.ai_title = value
            updated.append("ai_title")
    if flags.get("ai_selling_point"):
        value = (ai_selling_point or "").strip() or None
        if row.ai_selling_point != value:
            row.ai_selling_point = value
            updated.append("ai_selling_point")

    # ⚠️ `ShopProduct` 没有 `updated_at` 列 —— 不写时间戳（加了反而是没有来源的假字段）。
    # 「这次改了什么」由返回的 `updated[]` 承担，它比一个时间戳信息量大得多。
    db.commit()
    db.refresh(row)

    return {
        "item_id": row.item_id or "",
        "group_id": row.group_id,
        "ai_title": row.ai_title,
        "ai_selling_point": row.ai_selling_point,
        "updated": updated,
    }
