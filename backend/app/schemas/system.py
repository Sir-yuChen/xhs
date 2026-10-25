"""系统管理域 GET 端点的响应模型（设计 61 A6 第一批）。

覆盖 `backend/app/api/system_{users,roles,menus,dict,permissions,logs}.py`
的全部 GET 端点。字段与各端点 `_serialize*` / 手工构造的 return dict
**逐字段一致**（key 原名，不改大小写）—— FastAPI 的 `response_model`
会过滤掉模型未声明的字段，漏一个 = 前端丢数据。

⚠️ 本模块是**非受管中转层**：只准 import pydantic 与内建类型，
不许 import services / models / api（架构方向守卫
`test_arch_dependency_direction.py`）。

时间字段说明：端点手工构造 dict 且已 `isoformat()` 成字符串，
故用 `str | None` 而非 `datetime`（无 `from_attributes` 需求）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# system_users.py
# --------------------------------------------------------------------------- #


class SystemUserOut(BaseModel):
    """用户行（列表脱敏 / 详情给全 —— 形状相同，仅 phone 取值不同）。"""

    id: int
    username: str
    real_name: str
    email: str
    phone: str
    role: str
    role_name: str
    status: str
    online: bool
    must_change_password: bool
    last_login_at: str | None = None
    last_login_ip: str
    created_at: str | None = None
    deleted_at: str | None = None


class SystemUserListOut(BaseModel):
    """`GET /system/users`（分页 + 筛选）。"""

    items: list[SystemUserOut]
    total: int
    page: int
    page_size: int


class SystemUserOnlineOut(BaseModel):
    """`GET /system/users/{user_id}/online`。"""

    user_id: int
    online: bool
    window_seconds: int
    note: str


# --------------------------------------------------------------------------- #
# system_roles.py
# --------------------------------------------------------------------------- #


class SystemRoleOut(BaseModel):
    """角色行（带用户数 / 菜单数）。"""

    id: int
    name: str
    code: str
    description: str
    is_system: bool
    status: str
    sort_order: int
    user_count: int
    menu_count: int
    created_at: str | None = None


class SystemRoleListOut(BaseModel):
    """`GET /system/roles`（无分页）。"""

    items: list[SystemRoleOut]


class SystemRoleMenusOut(BaseModel):
    """`GET /system/roles/{role_id}/menus`（当前授权的菜单 id）。"""

    role_id: int
    role_code: str
    menu_ids: list[int]


class SystemRoleUserItemOut(BaseModel):
    """角色成员行（精简四字段，与完整用户行形状不同，单独建模）。"""

    id: int
    username: str
    real_name: str
    status: str


class SystemRoleUsersOut(BaseModel):
    """`GET /system/roles/{role_id}/users`。"""

    role_id: int
    role_code: str
    items: list[SystemRoleUserItemOut]


# --------------------------------------------------------------------------- #
# system_menus.py
# --------------------------------------------------------------------------- #


class MenuMineNodeOut(BaseModel):
    """侧栏树节点（`/menus/mine`：无 child_count / created_at）。"""

    id: int
    parent_id: int
    name: str
    menu_type: str
    perms: str
    path: str
    component: str
    icon: str
    workspace: str
    sort_order: int
    is_visible: bool
    status: str
    children: list["MenuMineNodeOut"] = Field(default_factory=list)


class MenuMineListOut(BaseModel):
    """`GET /system/menus/mine`。"""

    items: list[MenuMineNodeOut]
    total: int


class MenuTreeNodeOut(BaseModel):
    """管理树节点（`/menus/tree`：多 child_count / created_at）。"""

    id: int
    parent_id: int
    name: str
    menu_type: str
    perms: str
    path: str
    component: str
    icon: str
    workspace: str
    sort_order: int
    is_visible: bool
    status: str
    child_count: int
    created_at: str | None = None
    children: list["MenuTreeNodeOut"] = Field(default_factory=list)


class MenuTreeListOut(BaseModel):
    """`GET /system/menus/tree`。"""

    items: list[MenuTreeNodeOut]
    total: int


class MenuDetailOut(BaseModel):
    """`GET /system/menus/{menu_id}`（单行，无 children）。"""

    id: int
    parent_id: int
    name: str
    menu_type: str
    perms: str
    path: str
    component: str
    icon: str
    workspace: str
    sort_order: int
    is_visible: bool
    status: str
    child_count: int
    created_at: str | None = None


# --------------------------------------------------------------------------- #
# system_dict.py
# --------------------------------------------------------------------------- #


class DictTypeOut(BaseModel):
    """字典类型行（带项数）。"""

    id: int
    code: str
    name: str
    status: str
    remark: str
    is_system: bool
    item_count: int
    created_at: str | None = None


class DictTypeListOut(BaseModel):
    """`GET /system/dict/types`。"""

    items: list[DictTypeOut]
    total: int


class DictItemOut(BaseModel):
    """字典项行（无 created_at —— 表里没有该列）。"""

    id: int
    type_id: int
    type_code: str
    label: str
    value: str
    sort_order: int
    list_class: str
    status: str
    remark: str


class DictItemListOut(BaseModel):
    """`GET /system/dict/items`。"""

    items: list[DictItemOut]
    total: int


class DictOptionOut(BaseModel):
    """下拉/文案用的字典项切片（管理字段不下发 —— 选项消费方用不到）。"""

    value: str
    label: str
    list_class: str = "default"
    sort_order: int = 0


class DictOptionsOut(BaseModel):
    """`GET /system/dict/options` —— 前端统一字典读出口（登录即可、无权限码）。

    `items` 键是字典类型 `code`，值是该组的可用选项（类型与项都只下发
    `status=normal`，停用组整组不出、停用项单条不出）。
    """

    items: dict[str, list[DictOptionOut]]


# --------------------------------------------------------------------------- #
# system_permissions.py
# --------------------------------------------------------------------------- #


class PermissionsMineOut(BaseModel):
    """`GET /system/permissions/mine`（按钮可见性数据源）。"""

    role: str
    is_admin: bool
    permissions: list[str]
    total: int


# --------------------------------------------------------------------------- #
# system_logs.py
# --------------------------------------------------------------------------- #


class AppLogLineOut(BaseModel):
    """文件日志行（行号 + 原文）。"""

    line_no: int
    text: str


class AppLogListOut(BaseModel):
    """`GET /system/logs/app`（文件日志尾部）。

    `file` / `page` / `page_size` 仅在成功读到文件时出现
    （目录不存在 / 无文件 / 不可读的早退分支只有 `items/total/note`）。
    """

    items: list[AppLogLineOut]
    total: int
    file: str | None = None
    page: int | None = None
    page_size: int | None = None
    note: str


class AuditLogOut(BaseModel):
    """API 审计日志行（`api_logs` 表）。"""

    id: int
    user_id: int | None = None
    endpoint: str
    platform: str | None = None
    status: str | None = None
    status_code: int | None = None
    created_at: str | None = None
    trace_id: str | None = None


class AuditLogListOut(BaseModel):
    """`GET /system/logs/audit`（分页）。"""

    items: list[AuditLogOut]
    total: int
    page: int
    page_size: int


class AuditLogDetailOut(AuditLogOut):
    """`GET /system/logs/audit/{log_id}`（多 meta + note；不落 query/body）。"""

    meta: Any | None = None
    note: str
