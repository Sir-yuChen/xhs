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

#: **第三条协议链路：千帆（ark）** —— 库里真实存在的**历史取值**（如账号 9
#: 「开心睡大觉的店」，与 `LINK_RANK` 早已容纳的 `qianfan` 同一个东西）。
#:
#: ⚠️ **刻意不进 `SUB_TYPES`**：那个元组的契约是「按它选登录适配器」
#: （`api/accounts.py::_select_adapter`），而千帆链路**没有登录适配器** ——
#: 它的登录走 `api/login_sessions.py`（扫码 / 邮箱），凭据只走 `ark` 通道。
#: 把一个选不出 adapter 的取值塞进去，会让 `_select_adapter` 的「显式分支」
#: 变成一句永远不成立的话。
#:
#: ⚠️ **它也不是业务维度**（ADR-009 / 设计 32 §4.3）：店铺线的判据是
#: 「`ark` 通道有没有令牌」（`services/shop_binding_service.has_ark_session`），
#: 与这个字段无关 —— 账号 8「观隅好物的店」`sub_type='creator'` 却是不折不扣的
#: 店铺线账号。**归属用归属判据，能力用能力判据。**
SUB_TYPE_QIANFAN = "qianfan"

#: 千帆链路的取值集合（判断「这是不是那条无适配器的链路」用，**不要**拿它去选 adapter）。
QIANFAN_LINK_SUBTYPES: tuple[str, ...] = (SUB_TYPE_QIANFAN,)


class UnknownSubTypeError(ValueError):
    """``sub_type`` 不在已知枚举内。

    继承 ``ValueError`` 以便调用方按普通参数错误处理。
    """


class SubTypeNotSupportedError(ValueError):
    """``sub_type`` 是**已知**链路，但该链路不支持本操作。

    与 :class:`UnknownSubTypeError` 的区别就是这个类的全部意义：

    * ``UnknownSubTypeError`` = 「我不认识这个取值」⇒ 数据脏了，要显式失败；
    * ``SubTypeNotSupportedError`` = 「我认识它，但它没有可以执行本操作的东西」
      ⇒ 是**业务结果**，调用方该返回可读原因（400），不是 500。

    ``code`` 供 API 层原样下发（前端据此给可执行文案）。
    """

    def __init__(self, message: str, *, code: str = "SUB_TYPE_NOT_SUPPORTED"):
        super().__init__(message)
        self.code = code


def normalize_sub_type(sub_type: Optional[str]) -> str:
    """把库中读出的 `sub_type` 归一为合法取值。

    容忍 `None` / 空串（早期历史记录未写入该字段）并按 `pc` 处理——
    这是**唯一**允许的隐式默认，且只针对「空值」，**不针对未知的非空值**。

    ⚠️ **已知但无适配器的链路**（千帆）也在此显式失败，但抛的是
    :class:`SubTypeNotSupportedError`（**业务结果**，调用方该给 400 + 可读原因），
    与「不认识的脏数据」区分开 —— 后者抛 :class:`UnknownSubTypeError`。
    这个区分是 2026-10-11 补的：真库里账号 9 就是 `qianfan`，此前它让
    `scheduler_service._check_single_account` 抛未捕获的
    `UnknownSubTypeError`（500 级），而「这个账号走的是千帆链路、没有网页版/创作者
    适配器」本来是个**可读答案**，不是故障。

    :raises UnknownSubTypeError: 取值非空但既不在 :data:`SUB_TYPES`、
        也不在 :data:`QIANFAN_LINK_SUBTYPES` 中。
    :raises SubTypeNotSupportedError: 取值是千帆链路（已知但无登录适配器）。
    """
    if sub_type is None or sub_type == "":
        return SUB_TYPE_PC
    if sub_type in QIANFAN_LINK_SUBTYPES:
        raise SubTypeNotSupportedError(
            f"sub_type={sub_type!r} 是千帆（ark）链路：它**没有**登录适配器，"
            "登录态走 api/login_sessions.py、凭据只走 `ark` 通道。"
            "本操作按「该链路不支持」处理（不要当未知取值，更不要走 pc 兜底）。",
            code="LINK_HAS_NO_ADAPTER",
        )
    if sub_type not in SUB_TYPES:
        raise UnknownSubTypeError(
            f"未知的 sub_type={sub_type!r}；已知取值：{', '.join(SUB_TYPES)}"
            f"（另有已知但无适配器的链路：{', '.join(QIANFAN_LINK_SUBTYPES)}）。"
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
