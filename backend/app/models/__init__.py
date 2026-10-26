from backend.app.models.ai import DEFAULT_TEXT_MODEL_NAME, AiDraft, AiGeneratedAsset, DraftAsset
from backend.app.models.api_log import ApiLog
from backend.app.models.auto_task import AutoTask
from backend.app.models.forbidden_word import (
    DEFAULT_GROUP,
    ForbiddenWord,
    ForbiddenWordGroupRow,
    MATCH_MODE_CONTAINS,
    MATCH_MODE_EXACT,
    MATCH_MODES,
)
from backend.app.models.generation import (
    AigcMediaAsset,
    GenerationRecord,
    NoteGenerationLink,
)
from backend.app.models.hot_topic import HotTopic
from backend.app.models.keyword_group import KeywordGroup
from backend.app.models.llm_gateway import (
    LlmBusinessProvider,
    LlmGatewayAlert,
    LlmGatewayConfig,
    LlmGatewayModel,
    LlmGatewaySnapshot,
    LlmGatewayUsageLog,
)
from backend.app.models.login_session import LoginSession
from backend.app.models.material import MaterialFile, MaterialFolder
from backend.app.models.no_traffic_cleanup_log import (
    NoTrafficCleanupItem,
    NoTrafficCleanupLog,
)
from backend.app.models.monitoring import (
    MonitoringSnapshot,
    MonitoringTarget,
    NoteMetricSnapshot,
)
from backend.app.models.note import Note, NoteAsset, NoteComment, NoteTopic, Topic
from backend.app.models.note_datacenter_stat import NoteDatacenterStat
from backend.app.models.notification import Notification
#: ⚠️ 设计 75 S5（2026-10-27）：`Order` / `OrderItem` / `OrderShipment` / `AfterSale`
#: 四个 ORM 类随订单域「全实时」整段退场（迁移 `n8o9p0q1r2s3` DROP 四张表）——
#: 不再注册，否则 `create_all()` 会把已 drop 的表建回来。状态口径仍在
#: `backend.app.models.order`（纯常量 + `resolve_order_status`，无 ORM 类）。
from backend.app.models.platform_account import AccountCookieVersion, PlatformAccount
#: ⚠️ `ProductGroup` 已随「彻底移除商品分组」退场（2026-10-05）—— 不再注册该 ORM 类，
#: 否则 `create_all()` 会把迁移已 drop 的 `product_groups` 表建回来。
from backend.app.models.dict import DictItem, DictType
from backend.app.models.menu import Menu
from backend.app.models.role import Role
from backend.app.models.role_menu import RoleMenu
from backend.app.models.data_package_rule import DataPackageRule, DataPackageRuleSet
from backend.app.models.prompt import PromptModule, PromptTemplate
from backend.app.models.publish import PublishAsset, PublishJob
from backend.app.models.scheduled_job import ScheduledJob, ScheduledJobLog
#: ⚠️ 设计 75 S5（2026-10-27）：`ShopProduct` / `NoteGoods` / `ShopNoteGoods` 三个 ORM 类
#: 随「商品 + 笔记↔商品挂链改实时」整段退场（迁移 `n8o9p0q1r2s3` DROP 三张表）。
#: `models/shop.py` 现为空模块（只留说明），`models/shop_product.py` 已删除。
from backend.app.models.business_log import BusinessLog
from backend.app.models.system_setting import SystemSetting
from backend.app.models.task import Task
from backend.app.models.user import User

__all__ = [
    "AccountCookieVersion",
    "AiDraft",
    "AiGeneratedAsset",
    "AigcMediaAsset",
    "ApiLog",
    "AutoTask",
    "DEFAULT_TEXT_MODEL_NAME",
    "DraftAsset",
    "DEFAULT_GROUP",
    "DictItem",
    "DictType",
    "ForbiddenWord",
    "ForbiddenWordGroupRow",
    "GenerationRecord",
    "KeywordGroup",
    "LlmBusinessProvider",
    "LlmGatewayConfig",
    "LlmGatewayModel",
    "LlmGatewaySnapshot",
    "LlmGatewayAlert",
    "LlmGatewayUsageLog",
    "LoginSession",
    "Menu",
    "Role",
    "RoleMenu",
    "MATCH_MODES",
    "MATCH_MODE_CONTAINS",
    "MATCH_MODE_EXACT",
    "MaterialFile",
    "MaterialFolder",
    "MonitoringSnapshot",
    "MonitoringTarget",
    "Note",
    "NoteAsset",
    "NoteComment",
    "NoteDatacenterStat",
    "NoteGenerationLink",
    "NoTrafficCleanupItem",
    "NoTrafficCleanupLog",
    "Notification",
    "PlatformAccount",
    "DataPackageRule",
    "DataPackageRuleSet",
    "PromptModule",
    "PromptTemplate",
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
