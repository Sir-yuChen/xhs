"""业务线（业务主线）的**唯一判据实现**。

## 这个模块解决什么

项目有**两条业务主线**（用户 2026-10-11 口径）：

* **店铺线（千帆商家）** —— 有千帆（ark）登录态的账号，能管商品 / 订单 / 图片空间；
* **内容线（非店铺号）** —— 有创作者登录态的账号，发种草笔记、无流量清理、内容洞察。

在此之前这两个名字在**代码里一次都没出现过**（`grep 个人号|店铺号|非店铺` ⇒ 0 命中），
「谁是店铺线」全靠约定（`docs/02_设计文档/08` §3.1 写对了模型，但没落成结构）——
于是同一个页面上两条线的东西互相混入，用户看不出「这一页要哪条线的令牌」。

## 设计的核心：**能力派生，不落库、不进 `sub_type`**

```
平台          xhs                                （已有 platform 列）
 └ 业务线     店铺线 / 内容线                      ← 本模块（纯派生，不落库）
    └ 账号     platform_accounts                  （已有，隔离键不变）
       └ 数据  notes / shop_products / note_goods  （已有，键不变）
```

判据是**能力**（某条通道有没有令牌），不是**类型**（`sub_type`）：

* ❌ **不用 `sub_type` 判业务线** —— ADR-009 已否决（`sub_type` 是路由开关，
  改绑即改路由 ⇒ 登录态立刻失效；且「creator 账号既可能有店也可能没店」，映射是猜的）。
* ❌ **不用 `seller_id IS NULL` 判能力** —— 那是把**归属判据**当**能力判据**用，
  账号 9 的 Bug 正是这么来的（见 `models/platform_account.py` 的同段告警）。
* ❌ **不把业务线落成数据库列** —— 落库就变成需要同步的状态，与「能力派生」自相矛盾。

## 一个账号可以**同时属于两条线**

账号 8「观隅好物的店」= `sub_type='creator'`（有创作者令牌）+ 有 `seller_id` +
1998 件商品台账 ⇒ **两条线都占**。所以 `business_lines_of()` 返回**集合**，不是单选枚举。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

#: 业务线：店铺线（千帆商家）。判据 = `ark` 通道有令牌。
BUSINESS_LINE_SHOP = "shop"

#: 业务线：内容线（非店铺号）。判据 = `creator` 通道有令牌。
BUSINESS_LINE_CONTENT = "content"

#: 业务线：平台级（不属于任一业务线）—— 采集 / 监控 / 内容库 / 大模型网关。
#: 它是**页面/能力的归属标签**，不会出现在 `business_lines_of()` 的返回值里
#: （一个账号不会「属于平台线」，只有**功能**才可能标成它）。
BUSINESS_LINE_SHARED = "shared"

#: 全部取值（含 `shared`），供 API 层做白名单校验 / 前端做展示映射。
BUSINESS_LINES: tuple[str, ...] = (
    BUSINESS_LINE_SHOP,
    BUSINESS_LINE_CONTENT,
    BUSINESS_LINE_SHARED,
)

#: 展示名（**唯一实现**；前端要显示中文就调 API 拿，不要各写一份）。
BUSINESS_LINE_LABELS: dict[str, str] = {
    BUSINESS_LINE_SHOP: "店铺线",
    BUSINESS_LINE_CONTENT: "内容线",
    BUSINESS_LINE_SHARED: "平台",
}

#: 各业务线的**门禁通道**（前端用来提示「本页要哪条线的令牌」）。
BUSINESS_LINE_CHANNELS: dict[str, str] = {
    BUSINESS_LINE_SHOP: "ark",
    BUSINESS_LINE_CONTENT: "creator",
}


class BusinessLineUnavailableError(ValueError):
    """账号不属于（或不满足）指定业务线 —— 是**业务结果**，不是故障。

    调用方该翻成 400 + 可读原因（沿用 `metrics_reason` / `code` 的既有范式），
    不要让它变成 500。
    """

    def __init__(self, message: str, *, code: str = "BUSINESS_LINE_UNAVAILABLE"):
        super().__init__(message)
        self.code = code


def has_creator_session(db: Session, account_id: int) -> bool:
    """该账号是否有**可用的创作者（creator）会话** —— 内容线门禁的唯一判据。

    ⚠️ 判据是「`creator` 通道有令牌」，不是 `sub_type == "creator"`：
    两者在**当下**大多一致，但 `sub_type` 是「这个账号最近一次登录走了哪条链路」的
    记录（路由用），而本函数回答的是「现在能不能打创作者接口」。
    一条过期但未清理的 creator 通道令牌会让两者分叉，而**能力**才是业务要的答案。

    软删账号恒为 `False`（DEF-055 同型：删除的数据不得再被使用）。
    """
    from backend.app.services.account_service import (
        CHANNEL_CREATOR,
        latest_cookies_for_channel,
    )

    return bool(latest_cookies_for_channel(db, int(account_id), CHANNEL_CREATOR))


def has_shop_session(db: Session, account_id: int) -> bool:
    """该账号是否有**可用的千帆（ark）会话** —— 店铺线门禁的唯一判据。

    **委托**给 `services/shop_binding_service.has_ark_session`（那一处是既有唯一实现），
    本函数只是把它收进业务线的命名空间下 —— 业务线判据从这里取，别在别处再造一份。
    """
    from backend.app.services.shop_binding_service import has_ark_session

    return has_ark_session(db, account_id)


def business_lines_of(
    db: Session,
    account: Optional[object],
    *,
    account_id: Optional[int] = None,
) -> frozenset[str]:
    """该账号**属于哪些业务线**（集合；两条都占是正常情况）。

    :param account: `PlatformAccount` 或任何有 `id` / `deleted_at` 的对象。
        传 `None` 时用 `account_id`（便于调用方只有 id 的场景）。
    :returns: `{BUSINESS_LINE_SHOP}` / `{BUSINESS_LINE_CONTENT}` /
        `{两者}` / **空集**（两条通道都没有令牌的账号 —— 如实为空，不兜底、
        不假设它是哪条线）。
    """
    if account is None and account_id is None:
        raise ValueError("business_lines_of 需要 account 或 account_id 之一")

    if account_id is None:
        account_id = int(getattr(account, "id"))
    account_id = int(account_id)

    # 软删账号不属于任何业务线（DEF-055 同型）。两个能力判据各自也会拦一次，
    # 这里提前判是为了不白白解密两份令牌。
    if account is not None and getattr(account, "deleted_at", None) is not None:
        return frozenset()

    lines: set[str] = set()
    if has_shop_session(db, account_id):
        lines.add(BUSINESS_LINE_SHOP)
    if has_creator_session(db, account_id):
        lines.add(BUSINESS_LINE_CONTENT)
    return frozenset(lines)


def require_business_line(db: Session, account_id: int, line: str) -> None:
    """断言账号属于指定业务线；不属于则抛可读的业务错误。

    :raises BusinessLineUnavailableError: 不属于该线（含 `line` 不是已知业务线）。
    """
    if line not in (BUSINESS_LINE_SHOP, BUSINESS_LINE_CONTENT):
        raise BusinessLineUnavailableError(
            f"未知业务线 {line!r}；已知：{BUSINESS_LINE_SHOP} / {BUSINESS_LINE_CONTENT}",
            code="UNKNOWN_BUSINESS_LINE",
        )
    if line not in business_lines_of(db, None, account_id=account_id):
        channel = BUSINESS_LINE_CHANNELS[line]
        label = BUSINESS_LINE_LABELS[line]
        raise BusinessLineUnavailableError(
            f"该账号没有可用的「{label}」令牌（{channel} 通道），请先在账号矩阵补登后重试。",
            code="BUSINESS_LINE_UNAVAILABLE",
        )
