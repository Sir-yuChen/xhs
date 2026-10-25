"""配置域响应模型（设计 61 §A6 第二期）。

## 为什么要有这些模型

端点不声明 `response_model` ⇒ OpenAPI 的 200 响应 schema 为空 ⇒ 前端 300+
个请求函数的返回类型全靠手写、零校验；后端改一个字段名，前端要到运行时
才炸（甚至不炸，只是静默取到 `undefined`）。这批模型让契约进入 OpenAPI。

## 推导方式

字段**逐字段**抄自实现，不凭设计文档猜：
* `SettingItem` ← `settings_service.serialize_setting()`（:759-781）
* `SettingListResponse` ← `api/settings.py::list_settings` 的 `return {...}`
* `EffectiveSettingsResponse` ← `settings_service.effective_settings_map()`

⚠️ 文档与实现不一致时以实现为准。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 单条配置
# ---------------------------------------------------------------------------


class SettingItem(BaseModel):
    """一条运行时配置（`settings_service.serialize_setting()` 的逐字段产物）。"""

    id: int
    #: 该行归属（设计 64 三级配置）：`system` / `user` / `workspace`。
    scope: str = Field(description="system / user / workspace")
    workspace: Optional[str] = None
    user_id: Optional[int] = Field(default=None, description="系统级行为 NULL")
    key: str
    #: 已按 `value_type` 反序列化的值（前端直接用）。
    value: Any = None
    #: 库里的原始字符串（诊断用；前端一般不用）。
    raw_value: Optional[str] = None
    value_type: str = "string"
    category: str = "general"
    label: str = ""
    description: str = ""
    requires_restart: bool = False
    is_dangerous: bool = False
    #: 改完是否**即时生效**（不需要重启）。由 `LIVE_KEYS` 派生。
    is_live: bool = False
    #: 该键的运行时读点（给人看的，如「同步限流器」）。
    live_read_point: str = ""
    updated_by: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SettingListResponse(BaseModel):
    """`GET /api/settings` —— 某一级的配置列表。"""

    scope: str = Field(description="实际生效的读取级别（user/system/workspace）")
    workspace: Optional[str] = Field(default=None, description="工作区级时的 workspace 值")
    items: list[SettingItem] = Field(default_factory=list)
    total: int = 0
    #: 哪些键改完即时生效（前端据此显示「实时生效」徽标）。
    live_keys: list[str] = Field(default_factory=list)


class SettingDetailResponse(SettingItem):
    """`GET /api/settings/{key}` —— 单个键（形状与列表项一致）。"""


# ---------------------------------------------------------------------------
# 级联生效视图
# ---------------------------------------------------------------------------


class EffectiveSettingItem(BaseModel):
    """级联合并后的「一格」：值 + 它到底命中哪一级。

    ⚠️ `scope_hit` 是这张视图的**主要价值** —— 用户看到某个开关是 true，
    需要知道那是自己设的，还是继承了系统级基线。

    字段严格对齐 `settings_service.effective_settings_map()`（:438-462）的产出：
    `value / value_type / category / label / scope_hit / updated_at`。
    ⚠️ 它**不含** description / requires_restart / is_dangerous —— 那些信息在
    列表端点（`GET /api/settings`）里给，本视图只回答「值是什么、哪来的」。
    别在这里「顺手补几个字段」：模型是契约，不是愿望清单。
    """

    key: str
    value: Any = None
    value_type: str = "string"
    category: str = "general"
    label: str = ""
    #: 命中级别：`workspace` / `user` / `system` / `builtin`（内置默认）。
    scope_hit: Optional[str] = None
    updated_at: Optional[str] = None


class EffectiveSettingsResponse(BaseModel):
    """`GET /api/settings/effective` —— 三级级联结果。"""

    workspace: Optional[str] = None
    items: list[EffectiveSettingItem] = Field(default_factory=list)
    total: int = 0
