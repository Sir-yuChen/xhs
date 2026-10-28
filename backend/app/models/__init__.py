from backend.app.models.ai import DEFAULT_TEXT_MODEL_NAME, AiDraft, AiGeneratedAsset
from backend.app.models.api_log import ApiLog
from backend.app.models.forbidden_word import (
    DEFAULT_GROUP,
    ForbiddenWord,
    ForbiddenWordRow,
    MATCH_MODE_CONTAINS,
    MATCH_MODE_EXACT,
    MATCH_MODES,
)
from backend.app.models.generation import (
    GenerationRecord,
)
#: ⚠️ 2026-10-28（设计 76 §3.2）：`HotTopic`（`hot_topics`）已整类退场，不再注册。
#: 留一个 import 会让 `Base.metadata` 继续持有这张表 ⇒ `create_all()` 把已 DROP 的表建回来。
from backend.app.models.llm_gateway import (
    LlmBusinessProvider,
    LlmGatewayAlert,
    LlmGatewayConfig,
    LlmGatewayModel,
    LlmGatewayUsageLog,
)
from backend.app.models.material import MaterialFile, MaterialFolder
from backend.app.models.no_traffic_cleanup_log import (
    ROW_BATCH,
    ROW_ITEM,
    NoTrafficCleanupRow,
)
from backend.app.models.note import Note, NoteTopic, Topic
from backend.app.models.note_datacenter_stat import NoteDatacenterStat
from backend.app.models.notification import Notification
#: ⚠️ 设计 75 S5（2026-10-27）：`Order` / `OrderItem` / `OrderShipment` / `AfterSale`
#: 四个 ORM 类随订单域「全实时」整段退场（迁移 `n8o9p0q1r2s3` DROP 四张表）——
#: 不再注册，否则 `create_all()` 会把已 drop 的表建回来。状态口径仍在
#: `backend.app.models.order`（纯常量 + `resolve_order_status`，无 ORM 类）。
#: ⚠️ 2026-10-28：`AccountCookieVersion` 已退场 —— 并成 `PlatformAccount.cookies_json`。
from backend.app.models.platform_account import PlatformAccount
#: ⚠️ `ProductGroup` 已随「彻底移除商品分组」退场（2026-10-05）—— 不再注册该 ORM 类，
#: 否则 `create_all()` 会把迁移已 drop 的 `product_groups` 表建回来。
from backend.app.models.dict import DictItem, DictType
from backend.app.models.menu import Menu
from backend.app.models.role import Role
from backend.app.models.role_menu import RoleMenu
from backend.app.models.data_package_rule import DataPackageRule
from backend.app.models.prompt import PromptModule, PromptTemplate
from backend.app.models.publish import (
    ASSET_AIGC,
    ASSET_DRAFT,
    ASSET_NOTE,
    ASSET_PUBLISH,
    PublishAsset,
    PublishJob,
)
from backend.app.models.scheduled_job import ScheduledJob, ScheduledJobLog
#: ⚠️ 设计 75 S5（2026-10-27）：`ShopProduct` / `NoteGoods` / `ShopNoteGoods` 三个 ORM 类
#: 随「商品 + 笔记↔商品挂链改实时」整段退场（迁移 `n8o9p0q1r2s3` DROP 三张表）。
#: `models/shop.py` 现为空模块（只留说明），`models/shop_product.py` 已删除。
from backend.app.models.business_log import BusinessLog
from backend.app.models.system_setting import SystemSetting
from backend.app.models.task import Task
from backend.app.models.user import User

__all__ = [
    "AiDraft",
    "AiGeneratedAsset",
    "ApiLog",
    "DEFAULT_TEXT_MODEL_NAME",
    "DEFAULT_GROUP",
    "DictItem",
    "DictType",
    "ForbiddenWord",
    "ForbiddenWordRow",
    "GenerationRecord",
    "LlmBusinessProvider",
    "LlmGatewayConfig",
    "LlmGatewayModel",
    "LlmGatewayAlert",
    "LlmGatewayUsageLog",
    "Menu",
    "Role",
    "RoleMenu",
    "MATCH_MODES",
    "MATCH_MODE_CONTAINS",
    "MATCH_MODE_EXACT",
    "MaterialFile",
    "MaterialFolder",
    "Note",
    "NoteDatacenterStat",
    "NoTrafficCleanupRow",
    "ROW_BATCH",
    "ROW_ITEM",
    "Notification",
    "PlatformAccount",
    "DataPackageRule",
    "PromptModule",
    "PromptTemplate",
    "ASSET_AIGC",
    "ASSET_DRAFT",
    "ASSET_NOTE",
    "ASSET_PUBLISH",
    "PublishAsset",
    "PublishJob",
    "ScheduledJob",
    "ScheduledJobLog",
    "BusinessLog",
    "SystemSetting",
    "Task",
    "Topic",
    "NoteTopic",
    "User",
]
