"""账号域响应模型（设计 65 §3 · L2 第 1 优先）。

账号矩阵是登录后的**入口页**，前端消费频率最高，因此排在 L2 第一位。

字段逐字段抄自 `services/account_service.serialize_account()`（:684-699）
与 `api/accounts.py::get_accounts` 拼进去的 `login_channels`。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class LoginChannelState(BaseModel):
    """单条登录链路的状态（实测字段：`state` / `reason` / `probed` / `label`）。

    三条链路恒在（`www` / `creator` / `ark`），哪怕不可用也出现 —— 缺项消失会让
    前端误以为「这个账号没有这个平台」和「后端没算」是两回事。

    * 列表加载时 `probe=False` ⇒ `probed=false`、`state` 是**本地判据**的结论；
    * 页面动作时 `probe=True` ⇒ 真打只读探针，`state` 反映真实可用性。
    """

    #: `ok` / `expired` / `missing` / `unknown` …（前端 `loginStateView()` 映射成文案）
    state: Optional[str] = None
    #: 人话原因（缺值时前端要能显示「为什么不可用」）
    reason: Optional[str] = None
    #: 本次是否真的发过探针
    probed: Optional[bool] = None
    #: 通道中文名，如「网页版（PC）」
    label: Optional[str] = None


class AccountItem(BaseModel):
    """一个平台账号（`serialize_account()` 的产物 + `login_channels`）。"""

    id: int
    #: `xhs` / `douyin` / `xianyu` …
    platform: str
    #: 更强的那条链路（`creator` > `qianfan` > `pc`）—— 发布闸门要求 `creator`
    sub_type: Optional[str] = None
    #: 平台侧账号 ID（身份键的一部分：`(user_id, platform, external_user_id)`）
    external_user_id: Optional[str] = None
    nickname: Optional[str] = None
    avatar_url: Optional[str] = None
    #: `active` / `invalid` / `expired` …
    status: Optional[str] = None
    status_message: Optional[str] = None
    #: 平台资料原始字段（粉丝数等；形状随平台变 ⇒ 宽松）
    profile: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    #: 两条登录链路的本地状态。**随列表一次给全**（前端不逐卡片再问）。
    login_channels: dict[str, LoginChannelState] = Field(default_factory=dict)


class AccountListResponse(BaseModel):
    """`GET /api/accounts`。"""

    total: int = 0
    page: int = 1
    page_size: int = 20
    items: list[AccountItem] = Field(default_factory=list)
