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
from backend.app.models.hot_topic import HotTopic, TopicGoodsMatch
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
    ShopMetricSnapshot,
)
from backend.app.models.note import Note, NoteAsset, NoteComment, NoteTopic, Topic
from backend.app.models.note_datacenter_stat import NoteDatacenterStat
from backend.app.models.notification import Notification
from backend.app.models.order import (
    AfterSale,
    Order,
    OrderItem,
    OrderRemark,
    OrderShipment,
)
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
from backend.app.models.shop import NoteGoods, ShopNoteGoods
from backend.app.models.shop_product import ShopProduct
from backend.app.models.system_setting import SystemSetting
from backend.app.models.task import Task
from backend.app.models.user import User

__all__ = [
    "AccountCookieVersion",
    "AfterSale",
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
    "NoteGoods",
    "Notification",
    "Order",
    "OrderItem",
    "OrderRemark",
    "OrderShipment",
    "PlatformAccount",
    "DataPackageRule",
    "DataPackageRuleSet",
    "PromptModule",
    "PromptTemplate",
    "PublishAsset",
    "PublishJob",
    "ScheduledJob",
    "ScheduledJobLog",
    "ShopNoteGoods",
    "ShopProduct",
    "SystemSetting",
    "Task",
    "Topic",
    "NoteTopic",
    "User",
]
