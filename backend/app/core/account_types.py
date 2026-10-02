"""账号协议链路类型（``sub_type``）的唯一权威定义。

## 语义边界（务必遵守）

``sub_type`` **只表达协议链路**——决定 adapter / 请求域名 / Cookie 域。
它 **不表达业务含义**：能不能用千帆（ark）由账号自己的 ``ark`` 通道令牌决定
（``shop_binding_service.has_ark_session()``），**不由店铺归属推导**。
（历史：「有没有店铺」曾是业务维度，由 ``platform_accounts.shop_id_fk`` 是否为 NULL
推导（2026-09-22 起）；``shop_id_fk`` 已随 ADR-021 退场，更早的 ``shop_account_bindings``
表也已整表移除。⚠️ 换句话说，**从来就不该**用归属判据回答能力问题 —— 账号 9 的 Bug
正是这么来的。）
**禁止**用 ``sub_type`` 判断"是不是店铺账号"（ADR-009）。

## 为什么要显式枚举

``sub_type`` 是一个**路由开关**：选错 adapter 会打错域名、用错 Cookie 域。
此前适配器选择写成 ``creator if sub_type == "creator" else pc``——
新增一种协议链路时，它会**静默走 pc 分支**，错误一路传播到 HTTP 层才暴露，
排查成本极高（缺陷登记见 ``04_测试文档/04_测试跟踪与缺陷标记.md``）。

因此本模块的规则是：**未知取值一律显式失败，不允许兜底**。

新增协议链路的正确做法（三步，缺一不可）：
    1. 在 ``SUB_TYPES`` 中登记新取值；
    2. 在 ``xhs_utils/xhs_core/auth.py`` 增加对应的 ``XHSPlatformConfig`` 实例；
    3. 在**所有** sub_type 分支处补显式 ``elif`` —— 漏一处就会 ``raise``，
       这是有意设计的：宁可启动即失败，也不要静默走错链路。
"""

from __future__ import annotations

from typing import Optional

SUB_TYPE_PC = "pc"
SUB_TYPE_CREATOR = "creator"

#: 全部已知的协议链路取值。新增链路必须在此登记。
SUB_TYPES: tuple[str, ...] = (SUB_TYPE_PC, SUB_TYPE_CREATOR)


class UnknownSubTypeError(ValueError):
    """``sub_type`` 不在已知枚举内。

    继承 ``ValueError`` 以便调用方按普通参数错误处理。
    """


def normalize_sub_type(sub_type: Optional[str]) -> str:
    """把库中读出的 ``sub_type`` 归一为合法取值。

    容忍 ``None`` / 空串（早期历史记录未写入该字段）并按 ``pc`` 处理——
    这是**唯一**允许的隐式默认，且只针对「空值」，**不针对未知的非空值**。

    :raises UnknownSubTypeError: 取值非空但不在 :data:`SUB_TYPES` 中。
    """
    if sub_type is None or sub_type == "":
        return SUB_TYPE_PC
    if sub_type not in SUB_TYPES:
        raise UnknownSubTypeError(
            f"未知的 sub_type={sub_type!r}；已知取值：{', '.join(SUB_TYPES)}。"
            " 新增协议链路需在 backend/app/core/account_types.py 登记，"
            " 并在全部 sub_type 分支处补显式 elif（禁止 else 兜底）。"
        )
    return sub_type


#: 链路**能力强弱**（合并同一小红书身份的多个链路时取最大者）。
#:
#: 为什么不复用 `SUB_TYPES` / `normalize_sub_type`：那两个的契约是「未知取值显式失败」
#: （它们是路由开关，选错会打错域名）；本表是**合并 / 迁移**用的，遇到历史取值
#: （例如库里真实存在的 `qianfan`）必须给出确定答案，而不是把一次建号变成 500。
LINK_RANK: dict[str, int] = {
    "pc": 1,
    #: 千帆链路：只有 B 端会话，不参与发布，但强于纯网页版。
    "qianfan": 2,
    #: 创作者链路：**发布唯一可用的链路**（`ensure_creator_account_for_publish` 要求它）。
    "creator": 3,
}


def strongest_sub_type(*values: Optional[str]) -> str:
    """一组链路取值里**能力最强**的那个（未知 / 空值按最弱处理）。

    ## 谁需要它（2026-09-26，用户口径「同一小红书 ID 不允许出现多行账号」）

    建号时不再按 `(…, sub_type, external_user_id)` 找行，而是**按小红书 ID**
    找 —— 于是「同一个账号先按 PC 扫、再按创作者平台扫」会命中**同一行**，
    那一行的 `sub_type` 必须取两条链路里更强的那个：

    * 取 `pc` ⇒ **静默丢掉发布能力**（发布闸门要求 `sub_type == "creator"`）；
    * 取 `creator` ⇒ 两条链路的令牌都在各自的通道里，一个都不少。

    这也是「一个账号、多平台令牌」模型该有的结论：链路只影响**怎么打平台**，
    不影响「这是几个账号」。
    """
    best = ""
    best_rank = -1
    for value in values:
        text = (value or "").strip()
        if not text:
            continue
        rank = LINK_RANK.get(text, 0)
        if rank > best_rank:
            best, best_rank = text, rank
    return best
