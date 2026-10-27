"""笔记任务队列的**持久化底座**（设计 13 §35.9，2026-09-25；2026-09-26 常驻化）。

**为什么要有这个模块**：发布 / 同步 / 商品生成三个队列原来是**纯内存**
（`deque` + `dict`），进程一退、没跑完的篇就彻底丢失（用户口径「应用退出再次启动后
可继续执行」）。看门狗（DEF-069）只救「不重启也停摆」那一半，救不了「重启即丢」。

**复用 `tasks` 表，不造第二张表**（`models/task.py`）：它已经把
`user_id / platform / task_type / status / progress / payload(JSON) / created_at /
started_at / finished_at / error_type / retry_count / max_retries` 备齐，且
`/tasks` 列表、`/tasks/{id}`、`/tasks/{id}/cancel`（协作式中断）、
`/tasks/{id}/retry` 都已存在 —— 新造一张同语义表就是 DEF-044。

**⚠️ 批次形态（2026-09-26 两轮口径，后者覆盖前者 —— 别只看其中一轮）**：

* 第一轮（当日早）：用户口径「**不需要队列批次**，队列是常驻，只需要队列详情记录
  即可」⇒ 批次父行整层移除（迁移 `e4f5a6b7c8d9` 删行、子行升为顶级记录）；
* 第二轮（当日晚，**现行**）：用户口径「**将队列执行详情表使用批次+详情父子树风格
  展示，批次逻辑不再移除，生成唯一批次号**」⇒ 批次层回来，但**形态换了一种**：

  - **一次入队动作 = 一个批次**（一次点击 / 一次 API 调用）；批次行是
    `tasks` 表的一行父行（`parent_task_id IS NULL`，`batch_no` 有值），
    队列项是它的子行（`parent_task_id = 批次行.id`）；
  - 被淘汰的那版是「**轮次**记账」（`ensure_open_batch` 复用未闭合批次 ⇒ 单条
    点击也挂进一个长命批次）；现行是「动作记账」，一次动作一个号，**不可复用**；
  - 批次号 `NB{yyyymmddHHMMSS}-{4 位}` 落**真列** `tasks.batch_no` +
    唯一索引 `ux_tasks_batch_no`（迁移 `f5a6b7c8d9e0`）—— 要「唯一」就得让 SQL
    能约束它，塞进 `payload` 只能靠应用层扫表自证，并发下挡不住重号；
  - 批次行**懒创建**（真有一项入队才落行）：门禁全不过的一次动作不留空批次行。

* 队列**仍是常驻 FIFO**：一行 = 一个队列项（一篇笔记 / 一个商品），worker 认领只认
  队列项类型（`QUEUE_ITEM_TASK_TYPES`）—— 批次行**永不**被认领、不进快照计数；
* 进度卡的「本轮成败」仍由**前端按自己入队的项累计**（后端只给队列实时态 + 近期
  终态），底座不承担轮次记账；
* 批次行的 `status` / `started_at` / `finished_at` / `progress` 是**派生值**：
  唯一实现 `batch_rollup()`，读取时按子行现算、子行终态时写回一次（`finish_item`）
  —— 窗口不靠「谁记得刷新」，写回只是让别的读取方（任务中心等）看到的也准。

**三个队列共用本底座**（`QUEUE_REGISTRY`，顺序即前端展示顺序）：笔记同步、
笔记发布、商品生成笔记（§31.10）。第三个与另两个的差别只在队列项**绑不绑
笔记**：生成的笔记还不存在 ⇒ `note_bound=False`，行上存 `item_id` / `item_name`，
幂等判据与快照展示都走 `item_ref_of()` 而不是 `note_pk`。

**隔离（DEF-066 同规）**：所有读写都带 `user_id`。唯一的例外是
`claim_next_item()` —— 认领是**全局 FIFO**（一个 worker 服务所有用户，顺序即队列
顺序），但认领后只按行上的 `user_id` 执行与记账，不跨用户读数据。
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session, aliased

from backend.app.core.database import SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models import Note, Task

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 类型常量
# --------------------------------------------------------------------------- #
#: 队列项类型（**认领只认这三个**；一行 = 一篇笔记 / 一个商品）。
TASK_TYPE_SYNC_ITEM = "xhs_note_sync_item"
TASK_TYPE_PUBLISH_ITEM = "xhs_note_publish_item"
#: 商品生成笔记队列项（一篇 = 一个商品；**不绑定笔记** —— 生成前笔记还不存在）。
TASK_TYPE_GEN_ITEM = "xhs_note_gen_item"
#: **LLM 全自动生成**队列项（设计 17 §8.1，T6）：一篇 = 一个商品，与
#: `TASK_TYPE_GEN_ITEM`（一期商品生成）**并存**而非替代 ——
#: 一期链路走 `product_note_service`（同步干跑 / 入队生成，链路固定），
#: LLM 链路走 `note_gen_llm_service`（可配模板 / 网关 / 素材，产物落
#: `generation_records`）。两条链路产物契约不同（一期落 notes 即可，LLM 链路
#: 还要落审计 + 媒体溯源），**合并会丢掉其中一边的语义**。
TASK_TYPE_GEN_LLM_ITEM = "xhs_note_gen_llm_item"
#: **同步商品信息队列项**（2026-10-11）：一个商品 = 一行，**不绑定笔记**（同步的是
#: 商品台账，不是笔记）⇒ 业务主键 = `item_id`（与商品生成队列同判据）。
TASK_TYPE_GOODS_SYNC_ITEM = "xhs_goods_sync_item"
#: ⚠️ **详情补齐队列项（`xhs_note_hydrate_item`）已删除**（2026-09-27 用户口径：
#: 「账号信息卡中移除补齐详情逻辑，点击同步笔记时，应获取到当前笔记的所有信息后再落库，
#: 在一个任务中完成，不需要再点击补齐详情」）。补齐能力并入**同步任务本身**
#: （设计 13 §35.18：`_sync_with_task` 收尾对账号全部待补笔记内联补齐）⇒ 独立队列
#: 失去唯一入口，留着就是死代码 + 与同步内补齐重复（DEF-044）。

QUEUE_ITEM_TASK_TYPES: tuple[str, ...] = (
    TASK_TYPE_SYNC_ITEM,
    TASK_TYPE_PUBLISH_ITEM,
    TASK_TYPE_GEN_ITEM,
    TASK_TYPE_GEN_LLM_ITEM,
    TASK_TYPE_GOODS_SYNC_ITEM,
)

#: **批次父行**类型（2026-09-26 用户口径「批次逻辑不再移除，生成唯一批次号」）：
#: 一次入队动作一行，`parent_task_id IS NULL` + `batch_no` 有值，队列项挂它下面。
#: ⚠️ **与队列项类型严格分开**：worker 认领只认 `QUEUE_ITEM_TASK_TYPES`，批次行
#: 不是可执行任务（混进去 = 一个「没有笔记主键」的项被认领，跑一次必然失败）。
TASK_TYPE_SYNC_BATCH = "xhs_note_sync_batch"
TASK_TYPE_PUBLISH_BATCH = "xhs_note_publish_batch"
TASK_TYPE_GEN_BATCH = "xhs_note_gen_batch"
#: LLM 全自动生成的批次父行（设计 17 §8.1）。
TASK_TYPE_GEN_LLM_BATCH = "xhs_note_gen_llm_batch"
#: 同步商品信息批次父行（2026-10-11）。
TASK_TYPE_GOODS_SYNC_BATCH = "xhs_goods_sync_batch"

QUEUE_BATCH_TASK_TYPES: tuple[str, ...] = (
    TASK_TYPE_SYNC_BATCH,
    TASK_TYPE_PUBLISH_BATCH,
    TASK_TYPE_GEN_BATCH,
    TASK_TYPE_GEN_LLM_BATCH,
    TASK_TYPE_GOODS_SYNC_BATCH,
)

# --------------------------------------------------------------------------- #
# 队列注册表（设计 13 §35.11，2026-09-25 用户口径「后端服务可能有很多不同的队列」）
# --------------------------------------------------------------------------- #
#: **为什么要有注册表**：队列记录页（前端）需要知道「系统里有哪些队列、各叫什么」。
#: 前端写死两份类型常量 = 第三种队列上线时界面不显示，又变成「按钮在、功能不在」。
#: 这里是**代码侧白名单**（与 `job_registry` 同一模式）：新增队列 = 加一行 + 发版，
#: 前端从 `GET /tasks/queues` 拿清单渲染，**零前端改动**。
@dataclass(frozen=True)
class QueueSpec:
    """一个队列的声明（键、中文名、队列项 task_type）。"""

    key: str
    """队列键（前端分组 / 深链用，稳定不变）。"""
    label: str
    """中文名（页面上显示的就是它）。"""
    item_task_type: str
    """队列项类型（一篇 / 一个商品 = 一行；认领只认它）。"""
    batch_task_type: str
    """批次父行类型（一次入队动作 = 一个批次行，挂 N 个队列项；**永不**被认领）。"""
    note_bound: bool = True
    """队列项是否绑定一篇笔记（详情抽屉据此决定要不要拉笔记标题）。"""


#: 队列清单（**顺序即前端展示顺序**）。
QUEUE_REGISTRY: tuple[QueueSpec, ...] = (
    QueueSpec(
        key="note_sync",
        label="笔记同步队列",
        item_task_type=TASK_TYPE_SYNC_ITEM,
        batch_task_type=TASK_TYPE_SYNC_BATCH,
    ),
    QueueSpec(
        key="note_publish",
        label="笔记发布队列",
        item_task_type=TASK_TYPE_PUBLISH_ITEM,
        batch_task_type=TASK_TYPE_PUBLISH_BATCH,
    ),
    QueueSpec(
        # 商品生成笔记队列（设计 13 §31.10，2026-09-25 用户口径「单独生成商品笔记和
        # 批量生成商品笔记均需走队列」）：一篇 = 一个商品，**不绑定笔记**（生成前
        # 笔记不存在）⇒ `note_bound=False`，详情抽屉据此显示商品名而不是拉笔记标题。
        key="note_generate",
        label="商品生成笔记队列",
        item_task_type=TASK_TYPE_GEN_ITEM,
        batch_task_type=TASK_TYPE_GEN_BATCH,
        note_bound=False,
    ),
    QueueSpec(
        # LLM 全自动生成队列（设计 17 §8.1，T6）：一篇 = 一个商品，**不绑定笔记**
        # （生成前笔记不存在）⇒ `note_bound=False`（与上一队列同判据）。
        key="note_generate_llm",
        label="LLM 生成笔记队列",
        item_task_type=TASK_TYPE_GEN_LLM_ITEM,
        batch_task_type=TASK_TYPE_GEN_LLM_BATCH,
        note_bound=False,
    ),
    QueueSpec(
        # 同步商品信息队列（2026-10-11 用户口径「新增一个同步商品信息队列」）：
        # 一个商品 = 一行，**不绑定笔记**（同步的是商品台账）⇒ `note_bound=False`。
        key="sync_goods",
        label="同步商品信息队列",
        item_task_type=TASK_TYPE_GOODS_SYNC_ITEM,
        batch_task_type=TASK_TYPE_GOODS_SYNC_BATCH,
        note_bound=False,
    ),
)

#: 队列项类型的反查（task_type → spec）。
_QUEUE_BY_ITEM: dict[str, QueueSpec] = {spec.item_task_type: spec for spec in QUEUE_REGISTRY}
#: 批次父行类型的反查（递归记录页按批次行找队列声明用）。
_QUEUE_BY_BATCH: dict[str, QueueSpec] = {spec.batch_task_type: spec for spec in QUEUE_REGISTRY}


def queue_spec_for(task_type: str) -> Optional[QueueSpec]:
    """按 task_type 找队列声明（**只认队列项**；不是队列项 ⇒ None）。

    「这一行是不是可执行的队列项」与「这一行属于哪个队列」是两个问题：
    本函数回答前者（worker / 快照 / 清单计数都要的是它）。要连带批次行一起解析
    用 :func:`queue_spec_for_any`。
    """
    return _QUEUE_BY_ITEM.get(task_type)


def queue_spec_for_any(task_type: str) -> Optional[QueueSpec]:
    """按 task_type 找队列声明（**队列项 + 批次行都认**；都不是 ⇒ None）。

    递归记录页用它把每一行（无论父行还是子行）落到一个队列上（筛选 / 分组）。
    """
    return _QUEUE_BY_ITEM.get(task_type) or _QUEUE_BY_BATCH.get(task_type)


def list_queue_specs() -> list[QueueSpec]:
    """队列清单（前端渲染用；顺序稳定）。"""
    return list(QUEUE_REGISTRY)


# ⚠️ 自校验（模块导入即跑）：注册表与上面的两个常量元组必须**逐个对上** ——
# 一边加队列忘了另一边，worker 就认领不到新队列的项（静默停摆），或者批次行
# 没有类型可落（「点一次没记录」）。
assert {spec.item_task_type for spec in QUEUE_REGISTRY} == set(QUEUE_ITEM_TASK_TYPES), (
    "QUEUE_REGISTRY 与 QUEUE_ITEM_TASK_TYPES 不一致：新增队列要两边一起加"
)
assert {spec.batch_task_type for spec in QUEUE_REGISTRY} == set(QUEUE_BATCH_TASK_TYPES), (
    "QUEUE_REGISTRY 与 QUEUE_BATCH_TASK_TYPES 不一致：新增队列要两边一起加"
)

#: **账号级任务**类型（设计 13 §35.16，2026-09-27 用户口径「账号卡点同步商品，任务中心
#: 任务日志没有新增」）：粒度是「一次账号级动作」而不是「一篇笔记 / 一个商品」⇒
#: **不是队列项**（队列认领只认 `QUEUE_ITEM_TASK_TYPES`），但**共用**同一套认领 / 记账 /
#: 复位 / 保留期机制（`account_task_service`）。四类各自被账号任务 worker 逐个跑。
#: ⚠️ §44（2026-09-30）：`xhs_note_sync` 由「混合双源」改为**仅普通笔记**，
#: 商品笔记拆成独立的 `xhs_goods_note_sync`（两条链路各有自己的 worker 线程 ⇒ 可并行）。
ACCOUNT_TASK_TYPES: tuple[str, ...] = (
    "xhs_no_traffic_cleanup",
)

#: 清理任务认的「队列详情记录」白名单：三个队列的**队列项行 + 批次父行**，外加
#: **账号级任务行**（`ACCOUNT_TASK_TYPES` —— 它们不是队列，但与队列记录同生命周期、
#: 同保留期，分开清会出现「队列记录 15 天、账号级行永久」的双轨）。
#: ⚠️ 刻意**不含** `ai_*` / `monitoring_crawl` —— 那些不是「队列详情记录」，
#: 删了会丢别处的排查依据（各自模块另有记录）。
#: ⚠️ 2026-09-27 §35.16：原句「刻意**不含** `xhs_shop_goods_sync` / `xhs_no_traffic_cleanup`」
#: **被本次取代** —— 两者是账号级同步 / 清理，与队列记录同生命周期，故纳入白名单。
#: ⚠️ 2026-09-27 §35.18：详情补齐队列的项 / 批次类型**整条移除**（队列已不存在）。
QUEUE_RECORD_TYPES: tuple[str, ...] = (
    TASK_TYPE_SYNC_ITEM,
    TASK_TYPE_PUBLISH_ITEM,
    TASK_TYPE_GEN_ITEM,
    TASK_TYPE_GEN_LLM_ITEM,
    TASK_TYPE_SYNC_BATCH,
    TASK_TYPE_PUBLISH_BATCH,
    TASK_TYPE_GEN_BATCH,
    TASK_TYPE_GEN_LLM_BATCH,
    *ACCOUNT_TASK_TYPES,
)
assert set(ACCOUNT_TASK_TYPES).issubset(set(QUEUE_RECORD_TYPES)), (
    "账号级任务必须全部纳入队列记录白名单（否则保留期清理留下永久行）"
)

#: 终态：不再被认领、可被保留期清理。`pending` / `running` **永不清理**。
TERMINAL_STATUSES: tuple[str, ...] = ("done", "failed", "cancelled")

#: 快照 `finished` 最多回多少条近期终态（进度卡按业务主键累积结果，窗口只影响
#: 单次响应体量；一次批量 ≤ 20，50 足够覆盖任何一轮 + 余量）。
FINISHED_WINDOW = 50

# --------------------------------------------------------------------------- #
# 来源（tasks.payload.source，2026-09-26 用户口径）
# --------------------------------------------------------------------------- #
#: **为什么必须有固定值**：队列记录页的「来源」列回答「这条记录从哪来的」。
#: 2026-09-26 用户两次口径：「来源列不能是未知，你排查所有来源后给每个来源固定值」+
# 「来源不能是简单的手动或自动，应具体到按钮」⇒ 取值**具体到触发它的那个按钮**：
#: 页面入口（三个队列 × 单条 / 批量）+ 账号卡三个同步按钮（§35.17 / §44）+ 重试 + 重启恢复 + 定时。
#: **新入口必须在这里加常量 + 前端 lib 加中文名，不许留空**（留空 = 「未知」）。
SOURCE_NOTE_SYNC_SINGLE = "note_sync_single"
"""内容库 · 单条「同步」按钮（`POST /notes/{id}/sync`）。"""

SOURCE_NOTE_SYNC_BATCH = "note_sync_batch"
"""内容库 · 「批量同步」按钮（`POST /notes/batch-sync`）。"""

SOURCE_ACCOUNT_CARD_NOTE_SYNC = "account_card_note_sync"
"""账号卡 · 「同步笔记」按钮（`POST /accounts/{id}/notes/sync`，设计 13 §35.17）。

⚠️ 这条入口**同时**落两处账：定时任务日志（`scheduled_job_logs`，`trigger_type=manual`）
与业务任务行（`tasks`，就是本来源值所在的那行）。原先把按钮写成遗留值 `manual`，
界面上显示成「页面按钮（早期数据）」—— 那是**假历史**，2026-09-27 用户口径一并纠正。
"""

SOURCE_ACCOUNT_CARD_GOODS_SYNC = "account_card_goods_sync"
SOURCE_ACCOUNT_CARD_ORDER_SYNC = "account_card_order_sync"
"""账号卡 · 「同步订单」按钮（`POST /api/xhs/qianfan/orders/sync`，设计 16 §7）。

与「同步商品」（**店铺商品台账**）分开记账：订单同步落 `orders` 等 5 张只读表，
任务日志的来源列要能区分这两个按钮（§35.12「来源要具体到按钮」）。
"""
"""账号卡 · 「同步商品」按钮（`POST /api/xhs/qianfan/sync-shop-goods`，设计 13 §35.17）。"""

SOURCE_ACCOUNT_CARD_GOODS_NOTE_SYNC = "account_card_goods_note_sync"

#: 同步商品信息队列的来源（2026-10-11）：三个入口各传各的按钮级来源。
SOURCE_GOODS_SYNC_BATCH = "goods_sync_batch"
"""商品列表 · 「批量同步商品信息」按钮。"""
SOURCE_GOODS_SYNC_SPECIFIED = "goods_sync_specified"
"""商品列表 · 「同步指定商品」按钮。"""
SOURCE_GOODS_SYNC_PUBLISHED = "goods_sync_published"
"""发布商品成功后的自动同步。"""
"""账号卡 · 「**同步商品笔记**」按钮（`POST /accounts/{id}/goods-notes/sync`，设计 13 §44）。

⚠️ 与上面的「同步商品」（**店铺商品台账**，SKU / 价格）**不是一回事**：这条是千帆
`goods-note/list` 的**挂链笔记**同步（`notes` + `note_goods`）。按钮级来源值必须区分开，
否则队列记录页把两件事显示成同一个来源（§35.12「来源要具体到按钮」）。
"""

SOURCE_NOTE_PUBLISH_SINGLE = "note_publish_single"
"""内容库 · 单条「发布」按钮（`POST /notes/{id}/publish`）。"""

SOURCE_NOTE_PUBLISH_BATCH = "note_publish_batch"
"""内容库 · 「批量发布」按钮（`POST /notes/batch-publish`）。"""

SOURCE_PRODUCT_NOTE_GENERATE_SINGLE = "product_note_generate_single"
"""商品列表 · 单条「生成笔记」按钮（`POST /xhs/product-notes/generate`）。"""

SOURCE_PRODUCT_NOTE_GENERATE_BATCH = "product_note_generate_batch"
"""商品列表 · 「批量生成智能笔记」按钮（`POST /xhs/product-notes/batch-generate`）。"""

SOURCE_PRODUCT_NOTE_GENERATE_LLM_SINGLE = "product_note_generate_llm_single"
"""商品列表 · 单条「LLM 生成笔记」按钮（`POST /xhs/product-notes/generate-llm`）。"""

SOURCE_PRODUCT_NOTE_GENERATE_LLM_BATCH = "product_note_generate_llm_batch"
"""商品列表 · 「批量 LLM 生成笔记」按钮（`POST /xhs/product-notes/batch-generate-llm`）。"""

SOURCE_LLM_GENERATE_RETRY = "llm_generate_retry"

#: 「每日发布商业笔记」定时任务（设计 15 §19，2026-10-07 用户需求）：生成 / 发布
#: 都由后台任务发起（**无人在前端点**）⇒ 队列记录页的来源列要能一眼区分它。
SOURCE_AUTO_BUSINESS_PUBLISH = "auto_business_note_publish"
"""生成记录页点「重试」重新入队的（`POST /xhs/product-notes/generate-llm/{id}/retry`）。

⚠️ **不复用 `SOURCE_RETRY`**：那个是队列记录页的通用重试（一期），本重试的入口在
生成记录维度（按 `generation_records.id`），来源列要能区分「从哪一页点的重试」。
"""

SOURCE_RETRY = "retry"
"""队列记录页点「重试」重新入队的（`POST /tasks/{id}/retry`）。"""

SOURCE_RECOVERY = "recovery"
"""应用重启后由启动恢复自动续跑的（`recover_interrupted` 复位的断点行）。"""

SOURCE_SCHEDULED = "scheduled"
"""定时任务触发（`run_scheduled_*` 给每个账号写的那行，`scheduled_job_logs` 侧同源）。

⚠️ 2026-09-27 更正：原注释写「当前无队列入队走这条……先占位」——**已是真实入口**
（定时轮转的笔记 / 商品同步行一直用它，设计 13 §35.17 起账号卡定向执行也归到定时任务那条路）。"""

SOURCE_MANUAL = "manual"
"""**遗留值**：按钮级细分之前入队的行（迁移 `d3e4f5a6b7c8` 回填的存量）。

代码**不再写它**（每个入口都传按钮级来源）；保留常量 + 前端标签
「页面按钮（早期数据）」是为了让存量行有处可归、有案可查。
"""

#: 全部合法来源（守卫 / 测试 / 文档引用这一个常量，不各写一份）。
#: ⚠️ 含遗留值 `manual`（不再写入，但存量行带着它）。
QUEUE_SOURCES: tuple[str, ...] = (
    SOURCE_NOTE_SYNC_SINGLE,
    SOURCE_NOTE_SYNC_BATCH,
    SOURCE_ACCOUNT_CARD_NOTE_SYNC,
    SOURCE_ACCOUNT_CARD_GOODS_SYNC,
    SOURCE_ACCOUNT_CARD_GOODS_NOTE_SYNC,
    SOURCE_NOTE_PUBLISH_SINGLE,
    SOURCE_NOTE_PUBLISH_BATCH,
    SOURCE_PRODUCT_NOTE_GENERATE_SINGLE,
    SOURCE_PRODUCT_NOTE_GENERATE_BATCH,
    SOURCE_PRODUCT_NOTE_GENERATE_LLM_SINGLE,
    SOURCE_PRODUCT_NOTE_GENERATE_LLM_BATCH,
    SOURCE_LLM_GENERATE_RETRY,
    SOURCE_AUTO_BUSINESS_PUBLISH,
    SOURCE_RETRY,
    SOURCE_RECOVERY,
    SOURCE_SCHEDULED,
    SOURCE_MANUAL,
)

#: 中断标记：进程退出 / 线程死亡留下的 `running` 行在恢复时打这个 `error_type`。
ERROR_INTERRUPTED = "interrupted"

#: 任务记录最长保留天数（用户 2026-09-25 口径「最长保留 15 天」）。
DEFAULT_RETENTION_DAYS = 15


def note_pk_of(row: Task) -> int:
    """取队列项行上的 `note_pk`（`0` = 脏行 / 缺失 / **非笔记绑定队列**）。

    公开（不是 `_` 前缀）：同步与发布两个 worker 都要用它，**同一语义不留两份
    实现**（DEF-044/047/058）—— 各写一个 `_item_note_pk` 迟早有一边忘改。
    """
    payload = row.payload if isinstance(row.payload, dict) else {}
    try:
        return int(payload.get("note_pk") or 0)
    except (TypeError, ValueError):
        return 0


def item_ref_of(row: Task) -> str:
    """取队列项行上的**业务主键**（非笔记绑定队列用）：商品生成队列 = `item_id`。

    为什么需要：`note_pk` 对商品生成队列恒为 `0`（生成前笔记不存在）⇒ 幂等判据
    不能只认 `note_pk`，否则所有生成项互为「重复」。笔记绑定队列返回空串
    （判据回落到 `note_pk`）。
    """
    payload = row.payload if isinstance(row.payload, dict) else {}
    return str(payload.get("item_id") or "").strip()


# --------------------------------------------------------------------------- #
# 批次（2026-09-26 用户口径「批次逻辑不再移除，生成唯一批次号」）
# --------------------------------------------------------------------------- #
#: 批次号前缀（人类可读 + 便于 grep；改前缀会让存量号与新号分属两族，慎改）。
BATCH_NO_PREFIX = "NB"

#: 批次号随机段字母表：**去掉 0/O/1/I/L** 这类看串行的字符 —— 批次号是要被用户
#: 念出来、抄给客服、写在工单上的，同形字符会把「查不到」变成玄学。
_BATCH_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

#: 重号重试次数：只有「同一秒并发」才会撞，8 次足够（每次重新取随机段）。
_BATCH_NO_MAX_ATTEMPTS = 8


@dataclass
class BatchRequest:
    """一次入队动作的批次上下文（**懒创建**：真有一项入队才落批次行）。

    为什么懒创建：门禁全不过的一次动作（例如批量勾了 5 篇、5 篇都不可同步）
    不该在记录页留一个「0 项批次」—— 空批次行是噪声，还会让清理逻辑多出
    「没有子行算不算可清」的分支。反过来，一个动作里只有一部分过门禁时，
    批次里的项数 = **实际入队的项数**（`enqueue_item` 里 `created=True` 的那几条）；
    重复入队（`created=False`，已在队列里）**不挂进本次批次** —— 它属于当初那次动作。

    用法：调用方 `_enqueue_*` 造一个本对象，把同一实例传给这一动作里的每次
    `enqueue_item(batch=…)`（`row` 字段由底座回填，调用方不用管）。
    """

    user_id: int
    item_task_type: str
    source: str
    row: Optional[Task] = field(default=None)
    """懒创建后回填的批次行（调用方**只读**；写入由 :func:`_batch_row_for` 负责）。"""


def new_batch_no(now=None) -> str:
    """生成一个批次号候选值（`NB20260926153324-7K2M`）。**唯一性另由库保证**。

    为什么不带自增序号：批次号是**跨会话 / 跨库**要被引用的标识（用户报障时会念
    它），自增序号在「清过一次记录」后会对不上；时间戳 + 随机段则自带时间线索，
    且不依赖任何需要维护的计数器。
    """
    stamp = (now or shanghai_now()).strftime("%Y%m%d%H%M%S")
    tail = "".join(secrets.choice(_BATCH_ALPHABET) for _ in range(4))
    return f"{BATCH_NO_PREFIX}{stamp}-{tail}"


def _create_batch_row(
    db: Session,
    *,
    user_id: int,
    item_task_type: str,
    source: str,
) -> Task:
    """落一行批次父行（批次号与库内现存值比对，重号则重取）。**不 commit**。

    唯一性两道：先按 `batch_no` 查重（应用层，给出清晰的报错），再由
    `ux_tasks_batch_no` 兜底（SQL 层，并发下真正的闸门 —— DEF-048：安全网不能
    条件性，能靠库的地方不要只靠应用层）。
    """
    spec = queue_spec_for(item_task_type)
    if spec is None:
        raise ValueError(f"未知队列项类型（没有对应批次类型）：{item_task_type}")
    batch_no = ""
    for _ in range(_BATCH_NO_MAX_ATTEMPTS):
        candidate = new_batch_no()
        if db.scalar(select(Task.id).where(Task.batch_no == candidate)) is None:
            batch_no = candidate
            break
    if not batch_no:
        raise RuntimeError(
            f"生成唯一批次号失败（连续 {_BATCH_NO_MAX_ATTEMPTS} 次重号，检查 tasks.batch_no 唯一索引）"
        )
    row = Task(
        user_id=user_id,
        platform="xhs",
        task_type=spec.batch_task_type,
        status="pending",
        progress=0,
        payload={"source": str(source), "queue_key": spec.key},
        batch_no=batch_no,
    )
    db.add(row)
    db.flush()
    return row


def _batch_row_for(db: Session, request: BatchRequest) -> Task:
    """取（必要时创建）这次动作的批次行。同一 `BatchRequest` 只创建一次。"""
    if request.row is None:
        request.row = _create_batch_row(
            db,
            user_id=request.user_id,
            item_task_type=request.item_task_type,
            source=request.source,
        )
    return request.row


def children_of(
    db: Session,
    *,
    parent_id: int,
    user_id: Optional[int] = None,
) -> list[Task]:
    """批次行下的队列项（**FIFO 序**：`id` 升序 = 入队序）。

    `user_id` 给定时按归属收口（DEF-066）；批次行与子行同属一个用户，收口是
    纵深防御（拿别人的批次 id 也翻不出他的项）。
    """
    statement = select(Task).where(Task.parent_task_id == int(parent_id))
    if user_id is not None:
        statement = statement.where(Task.user_id == int(user_id))
    return list(db.scalars(statement.order_by(Task.id)).all())


def batch_rollup(statuses: Iterable[str]) -> str:
    """按子行状态**派生**批次状态（记录页 / 写回 / 前端判据的唯一实现）。

    ⚠️ **分支顺序就是语义**，三处都依赖它：

    * `running` → `pending` → 有活的就报活的（批次没结束）；
    * `failed` / `exhausted` **先于** `cancelled`：`[成功, 取消, 失败]` 的批次
      报「已取消」会把「有篇没跑成」藏起来 —— 用户要能看到红；
    * 这个顺序同时保证**可清理判据自洽**：`failed` / `exhausted` / `pending` /
      `running` 都不可清（`CLEANABLE_STATUSES`）⇒ 只要有一个这样的子行，派生
      状态也必然不可清，前端 `canCleanTask(批次状态)` 直接可用，不必另判子行。

    空子行（子行被单独清掉 / 脏数据）⇒ `done`（没有可报的失败）。
    """
    items = [str(status or "") for status in statuses]
    if "running" in items:
        return "running"
    if "pending" in items:
        return "pending"
    if "failed" in items or "exhausted" in items:
        return "failed"
    if "cancelled" in items:
        return "cancelled"
    return "done"


def refresh_batch_status(db: Session, batch: Task) -> str:
    """把批次行的状态 / 计时 / 进度按当前子行**写回**（幂等；**不 commit**）。

    读取路径**不依赖**它（记录页读到的是按子行现算的值）：写回是给**别的**读取方
    看的（任务中心列表、导出、`/tasks` 平铺模式）—— 让它们别看到「明明跑完了还写
    `pending`」的假象。这也正是被淘汰的那版批次层踩过的坑：状态只靠「谁记得刷新」，
    一旦漏了一处就永久停在 `running`（所以这里只把写回当**冗余**，不当真相源）。

    返回写回后的批次状态（调用方常要拿它做后续判断）。
    """
    children = children_of(db, parent_id=int(batch.id))
    statuses = [str(child.status or "") for child in children]
    batch.status = batch_rollup(statuses)
    started = [child.started_at for child in children if child.started_at]
    finished = [child.finished_at for child in children if child.finished_at]
    batch.started_at = min(started) if started else None
    #: 还有活着的子行 ⇒ 批次没有「结束时刻」（缺值口径：没跑完的计时不是 0，是 None）
    still_open = any(status in ("pending", "running") for status in statuses)
    batch.finished_at = None if still_open else (max(finished) if finished else None)
    settled = sum(1 for status in statuses if status not in ("pending", "running"))
    batch.progress = int(settled * 100 / len(statuses)) if statuses else 0
    db.flush()
    return str(batch.status)


def batch_summary(children: list[Task]) -> dict:
    """批次行的**派生视图**（记录页读取用；与 `refresh_batch_status` 同源同判据）。

    只接收子行（批次行自身的字段**一个都不用** —— 全部按子行现算）：
    这样记录页就不会出现「库里写的」与「页面算的」两套数。

    与写回版的区别只有一处：**只读不写** —— 记录页是 GET 路径，不许在读取里改库。

    返回的每个键都是「按子行现算」的事实：

    * `status` —— 派生状态（`batch_rollup`，与可清理判据自洽）；
    * `total` / `done` / `failed` / `cancelled` / `running` / `pending` —— 明细计数
      （`failed` 把 `exhausted` 并进来：对用户是同一件事「没跑成，可重试」）；
    * `settled` —— 已终态条数（进度分子）；
    * `progress` —— `settled / total` 的百分数（无子行 ⇒ 0）；
    * `started_at` / `finished_at` / `duration_ms` —— 整批的时间跨度（首条开始 →
      最后一条结束）；**还有活着的子行 ⇒ `finished_at` / `duration_ms` 为 None**
      （缺值口径：没跑完的计时不是 0）。
    """
    statuses = [str(child.status or "") for child in children]
    counts = {key: 0 for key in ("done", "failed", "cancelled", "running", "pending")}
    for status in statuses:
        key = "failed" if status in ("failed", "exhausted") else status
        if key in counts:
            counts[key] += 1
    settled = sum(1 for status in statuses if status not in ("pending", "running"))
    still_open = settled < len(statuses)
    started = [child.started_at for child in children if child.started_at]
    finished = [child.finished_at for child in children if child.finished_at]
    first_start = min(started) if started else None
    last_finish = None if still_open else (max(finished) if finished else None)
    duration_ms = (
        int((last_finish - first_start).total_seconds() * 1000)
        if first_start is not None and last_finish is not None
        else None
    )
    return {
        "status": batch_rollup(statuses),
        "total": len(statuses),
        "done": counts["done"],
        "failed": counts["failed"],
        "cancelled": counts["cancelled"],
        "running": counts["running"],
        "pending": counts["pending"],
        "settled": settled,
        "progress": int(settled * 100 / len(statuses)) if statuses else 0,
        "started_at": first_start,
        "finished_at": last_finish,
        "duration_ms": duration_ms,
    }


def batch_account_ids(children: list[Task]) -> list[int]:
    """批次行下**用到哪些平台账号**（去重 + 保序，按子行 FIFO）。

    2026-09-26 用户口径「批次记录执行的账号」：**批次级的账号同样由子行派生**
    —— 与 `batch_summary` 的状态 / 计数同一条原则（批次行的字段一个都不用，
    全部按子行现算）。理由不只是「统一」：一次批量可以跨账号勾选，在批次行上
    另存一个 `account_id` 只会在跨账号时**说错**（报成第一个账号，而页面上的
    明细有 3 个账号），派生出来的列表永远与明细一致。

    保序 = 子行 FIFO 序（与明细展示顺序一致，用户对照时不会错位）。
    `payload.account_id` 缺失 / 为 `None` / 非正整数的行**跳过**：不知道是谁跑的
    就不编一个（缺值口径），调用方按空列表渲染 `-`。
    """
    seen: list[int] = []
    for child in children:
        payload = child.payload if isinstance(child.payload, dict) else {}
        raw = payload.get("account_id")
        if raw is None:
            continue
        try:
            account_id = int(raw)
        except (TypeError, ValueError):
            continue
        if account_id > 0 and account_id not in seen:
            seen.append(account_id)
    return seen


# --------------------------------------------------------------------------- #
# 入队
# --------------------------------------------------------------------------- #
def open_note_pk_index(
    db: Session,
    *,
    user_id: int,
    item_task_type: str,
) -> dict[int, Task]:
    """预取「本用户 + 本类型**未终态**队列项」的 `note_pk → 行` 索引（批量入队用）。

    给 `enqueue_item(known_open=…)` 用（设计 13 §35.15）：账号级补齐一次要入队上千篇
    （账号 8 实测 1867 条待补），逐篇自查未终态行是 O(N²)，一次预取把它压成 O(N)。

    ⚠️ **where 条件与 `enqueue_item` 的自查路径逐字相同**（`user_id` + `task_type` +
    `pending`/`running`）—— 这是同一判据的两条取材路径，不是两份判据：改状态口径时
    两处必须一起改（它们在同一文件、相邻两段，紧邻放置就是为了这个）。

    只索引 `note_pk`（笔记绑定队列的幂等主键）；非笔记绑定队列（商品生成）认
    `item_id`，不适用 —— 传进 `known_open` 会被 `enqueue_item` 直接拒掉。
    """
    rows = db.scalars(
        select(Task).where(
            Task.user_id == user_id,
            Task.task_type == item_task_type,
            Task.status.in_(("pending", "running")),
        )
    ).all()
    index: dict[int, Task] = {}
    for row in rows:
        pk = note_pk_of(row)
        if pk:
            index.setdefault(pk, row)
    return index


def enqueue_item(
    db: Session,
    *,
    user_id: int,
    item_task_type: str,
    note_pk: int = 0,
    account_id: Optional[int] = None,
    item_id: str = "",
    item_name: str = "",
    note_title: str = "",
    source: str,
    batch: Optional[BatchRequest] = None,
    known_open: Optional[dict[int, Task]] = None,
) -> tuple[Task, bool]:
    """入队一个队列项（幂等）。返回 `(行, created)`；`created=False` = 已在队列 / 执行中。

    幂等判据与内存版同口径：同一用户、同类型、**未终态**（`pending` / `running`）
    的行里已有同主键 ⇒ 不重复入队、不重复请求。主键 = `note_pk`（笔记绑定队列）
    或 `item_id`（**商品生成队列**：生成前笔记不存在，`note_pk` 恒 0，只认商品 ID）。
    用 Python 侧过滤而不是 JSON 路径 SQL（`payload["note_pk"]` 的可移植性在
    SQLite / PG 上不一致，且并发未终态行有界，扫描成本可忽略）。

    **入队即完整落库**（2026-09-26 用户口径「进入队列的数据先进行持久化」）：
    行上带齐展示所需的全部数据 —— `source`（来源列，**具体到按钮**）、
    `note_title`（同步 / 发布队列项的主体名，入队时笔记已存在，仿商品队列
    `item_name` 的快照先例）—— 队列记录页直接渲染，不为每行回头拉笔记。

    `batch` = 这次动作的批次上下文（2026-09-26 用户口径「批次逻辑不再移除」）：
    **真落库的行**才挂父行（`parent_task_id`），批次行在第一次真入队时懒创建；
    `created=False`（已在队列里）**不改挂** —— 那一行属于当初入队它的那次动作，
    把它挪到本次批次下等于篡改历史。`batch=None` 时该行是**顶级记录**（启动恢复
    / 早期存量 / 测试直插都会这样）。

    ⚠️ `source` **必填、无默认值**：每个入口都必须显式声明自己是谁（按钮级常量
    见本模块顶部）。给默认值就等于允许「忘了传还静悄悄显示手动」—— 那正是
    2026-09-26 用户投诉的「未知 / 手动」来源列。

    ``known_open``（设计 13 §35.15，2026-09-27）：调用方**预取**的「本用户 + 本类型
    未终态行」索引（`note_pk → 行`，由 :func:`open_note_pk_index` 产出，唯一实现）。
    不传（默认）⇒ 本函数自己查一遍 —— 单条入口的既有行为**逐字不变**。

    为什么需要它：账号级「补齐详情」一次要入队上千篇（账号 8 实测 1867 条待补），
    而每篇都自查一遍未终态行 = N 次「查全表 + 建 N 个 ORM 对象」⇒ O(N²)
    （1867² ≈ 350 万次比较，请求要转几十秒）。给了索引 ⇒ 判重退化为一次 dict 命中。

    ⚠️ 判重口径**仍是同一段代码**（本函数的既有分支），不产生第二份实现
    （DEF-044）：索引只是把「未终态行从哪来」换成调用方预取。
    ⚠️ 新入队的行会**回填进索引**，所以调用方只需预取一次、循环里无需自己维护。
    ⚠️ 只支持**笔记绑定队列**（索引键是 `note_pk`）：非笔记绑定队列（商品生成）
    传它 ⇒ 直接 `ValueError` —— 静默退化成「不判重」会在商品侧造成重复入队
    （安全网不能条件性，DEF-048）。

    ⚠️ 不 commit：调用方提交（一次批量 = 一个事务）。
    """
    if not str(source or "").strip():
        raise ValueError("enqueue_item: source 必填（按钮级来源常量，见 note_queue_service）")
    note_pk = int(note_pk or 0)
    ref = str(item_id or "").strip()
    if known_open is not None and not note_pk:
        raise ValueError("enqueue_item: known_open 只适用于笔记绑定队列（note_pk 必填）")
    if known_open is not None:
        # 批量入口：索引已预取（本次动作在单事务内跑完，索引期间不会失效）。
        hit = known_open.get(note_pk)
        if hit is not None:
            return hit, False
    else:
        open_rows = db.scalars(
            select(Task).where(
                Task.user_id == user_id,
                Task.task_type == item_task_type,
                Task.status.in_(("pending", "running")),
            )
        ).all()
        for row in open_rows:
            if note_pk:
                if note_pk_of(row) == note_pk:
                    return row, False
            elif ref and item_ref_of(row) == ref:
                return row, False

    payload: dict = {
        "note_pk": note_pk,
        "account_id": int(account_id) if account_id is not None else None,
        "source": str(source),
    }
    if ref:
        # 商品生成队列的业务主键 + 展示名（快照 / 详情抽屉直接显示，不回头查商品表）
        payload["item_id"] = ref
        payload["item_name"] = str(item_name or "").strip()
    title = str(note_title or "").strip()
    if title:
        # 笔记绑定队列的主体快照（入队时笔记必存在；缺省不写，不造假）
        payload["note_title"] = title
    # ⚠️ 批次行在**这里**才懒创建（前面幂等命中直接 return 了，没走到这一步）：
    # 门禁全不过的一次动作不留空批次行。幂等命中时**不挂载**（见 docstring）。
    parent = _batch_row_for(db, batch) if batch is not None else None
    row = Task(
        user_id=user_id,
        platform="xhs",
        task_type=item_task_type,
        status="pending",
        progress=0,
        payload=payload,
        parent_task_id=(int(parent.id) if parent is not None else None),
    )
    db.add(row)
    db.flush()
    if known_open is not None and note_pk:
        # 回填索引：不回填就等于「这批内的重复候选不判重」（同一循环里先后两次
        # 命中同一篇时，第二次会再插一行 —— 幂等判据不能只在批间成立）。
        known_open[note_pk] = row
    if parent is not None:
        # 批次行刚多了一个子行 ⇒ 它的派生状态 / 进度跟着变（写回是冗余不是真相源，
        # 但让并发的列表读取看到一致值）
        refresh_batch_status(db, parent)
    # 设计 64 §2.3：入队成功埋点（与业务同事务 —— 回滚则日志也回滚，不产生幽灵记录）。
    _biz_record(
        "queue.enqueued",
        action="create",
        actor_id=user_id,
        target_type="task",
        target_id=row.id,
        summary=f"入队 {item_task_type}（{item_name or note_title or note_pk or ref or '-'}）",
        detail={"source": source, "batch": (getattr(batch, "batch_no", None) or getattr(getattr(batch, "row", None), "batch_no", None))},
        db=db,
    )
    return row, True


# --------------------------------------------------------------------------- #
# 认领 / 记账
# --------------------------------------------------------------------------- #
#: 认领扫描窗口：一次最多看这么多条 `pending` 行来决定认领哪一条。
#: 为什么不是 1：带 `exclude_account_ids` 时要跳过「账号已被别的 worker 占住」的项，
#: 窗口太小会让「队头恰好是忙账号」直接变成「这一轮没活干」（假空转）。
#: 64 远大于并行度上限 8，够跳过所有忙账号还有余量；窗口本身只是一次带 `LIMIT` 的
#: 主键扫描，成本可忽略。
CLAIM_SCAN_WINDOW = 64


def account_id_of(row: Task) -> Optional[int]:
    """队列项所属平台账号（`payload.account_id`）。

    与 `note_pk_of` / `item_ref_of` 同族：**Python 侧读 JSON**（不写
    `payload["account_id"]` 的 SQL 路径 —— SQLite / PG 的可移植性不一致，本项目
    在 `enqueue_item` 的幂等判据里已立过这条规矩）。
    缺值 / 脏值 ⇒ `None`（**不猜成某个账号**：猜错会让两个 worker 同时跑同一账号）。
    """
    payload = row.payload if isinstance(row.payload, dict) else {}
    try:
        value = payload.get("account_id")
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def claim_next_item(
    db: Session,
    *,
    item_task_type: str,
    exclude_account_ids: Iterable[int] = (),
) -> Optional[Task]:
    """全局 FIFO 认领一条 `pending` 项并置 `running`（条件 UPDATE 防抢同一行）。

    为什么用「先查 id、再条件 UPDATE」而不是「查出来直接改」：多一条 worker
    （看门狗重建的瞬间新旧并存）时，两边可能查到同一行；条件 UPDATE 带
    `status == "pending"` 且按 `rowcount` 判定 ⇒ 只有一个能认领成功，另一个
    重试下一行 —— 这是「同一行不被跑两遍」的唯一闸门。

    `exclude_account_ids`（设计 13 §38.3，2026-09-27）：**跳过这些账号的项**，
    用于跨账号并行的**账号级互斥** —— 同一账号的任何时刻至多 1 条在跑。

    * 不传（默认 `()`）⇒ 行为与改造前**逐字一致**（发布 / 生成两个队列复用本函数，
      不受影响）；
    * `payload.account_id` 缺值的项**不参与排除**（排除不了就照常认领：宁可让脏行
      按 FIFO 跑掉，也不要因为它把整条队列堵住 —— 「未知账号」不等于「忙账号」）。
    """
    blocked = {int(value) for value in exclude_account_ids if value is not None}
    for _ in range(8):
        candidate_ids = list(
            db.scalars(
                select(Task.id)
                .where(Task.task_type == item_task_type, Task.status == "pending")
                .order_by(Task.id)
                .limit(CLAIM_SCAN_WINDOW)
            ).all()
        )
        if not candidate_ids:
            return None

        candidate_id: Optional[int] = candidate_ids[0]
        if blocked:
            candidate_id = None
            # 只在**有账号被占住**时才多查一次行体（无并行 / 无占用时零额外开销）
            for row in db.scalars(
                select(Task).where(Task.id.in_(candidate_ids)).order_by(Task.id)
            ).all():
                account_id = account_id_of(row)
                if account_id is None or account_id not in blocked:
                    candidate_id = int(row.id)
                    break
            if candidate_id is None:
                # 窗口内全是忙账号的项 ⇒ 本轮不认领（**不是**「队列空了」：
                # 调用方据此继续等，另一条 worker 跑完就会让这些项重新可选）
                return None

        result = db.execute(
            update(Task)
            .where(Task.id == candidate_id, Task.status == "pending")
            .values(status="running", started_at=shanghai_now())
        )
        db.commit()
        if result.rowcount == 1:
            return db.get(Task, candidate_id)
        logger.info("note queue: lost claim race on task %s, retrying", candidate_id)
    return None


def finish_item(
    db: Session,
    item: Task,
    *,
    ok: bool,
    reason: str = "",
    code: str = "",
    extra: Optional[dict] = None,
) -> None:
    """写队列项的终态（`done` / `failed`）+ 原因。

    `extra` = 成功后要补进行体的业务字段（商品生成队列写 `note_id` / `note_title`，
    让快照与详情抽屉能显示「生成了哪篇笔记」；失败时不写）。

    ⚠️ `payload` 必须**赋新 dict**（SQLAlchemy JSON 列原地改 dict **不标脏**，
    UPDATE 不会发 —— 老坑）。
    """
    payload = dict(item.payload or {})
    payload.update({"ok": bool(ok), "reason": str(reason or ""), "code": str(code or "")})
    if extra:
        payload.update({str(k): v for k, v in extra.items()})
    item.payload = payload
    item.status = "done" if ok else "failed"
    item.error_type = None if ok else (str(code or "") or "unknown")
    item.finished_at = shanghai_now()
    item.progress = 0 if not ok else 100
    db.flush()
    # 子行终态 ⇒ 批次行的派生状态 / 结束时刻跟着走到位（2026-09-26 批次层）：
    # 不刷的话列表虽然按子行现算仍是对的，但「跑完还把批次写成进行中」会被
    # 别处的读取方（任务中心 / 导出）看到 —— 冗余写回就是为了不出现这种假象。
    _refresh_parent_batch(db, item)
    db.commit()
    # 设计 64 §2.3：终态埋点（done / failed；cancelled 走取消端点有自己的收口）。
    # ⚠️ `db=None`（自开短会话立即提交）：这里业务事务**已经 commit 完**，再挂到
    #    老会话上只会 flush 出一条没人提交的孤儿行。
    _biz_record(
        "queue.finished" if ok else "queue.failed",
        action="update",
        actor_id=item.user_id,
        target_type="task",
        target_id=item.id,
        summary=(
            f"队列项{('成功' if ok else '失败')}：{item.task_type}"
            f"{('（' + str(reason or code or '') + '）') if not ok and (reason or code) else ''}"
        ),
        detail={"task_type": item.task_type, "reason": reason, "code": code},
        level="info" if ok else "warning",
        db=None,
    )


def _biz_record(*args, **kwargs) -> None:
    """业务日志便捷转发（设计 64 §2.3）。

    为什么包一层：`business_log_service` 会在函数内 `import note_queue_service`
    的若干符号吗？不会 —— 但它自开 Session 的路径与队列 worker 的会话生命周期
    交叉时，这层延迟 import 保证**队列模块先可加载**（循环依赖防火墙，与
    `rate_limiter.QUOTA_PROVIDER` 的注册式注入同型）。
    """
    try:
        from backend.app.services import business_log_service

        business_log_service.record(*args, **kwargs)
    except Exception:  # noqa: BLE001 - 埋点失败绝不打断队列
        pass


def _refresh_parent_batch(db: Session, item: Task) -> None:
    """队列项状态变了 ⇒ 刷新它的批次父行（无父 / 父已不在 ⇒ 静默跳过）。不 commit。"""
    parent_id = int(item.parent_task_id or 0)
    if not parent_id:
        return
    parent = db.get(Task, parent_id)
    if parent is None:
        # 父行被清理掉（手动清理整批 / 保留期）后子行还在：不可能由本模块产生
        # （清批次连子行一起删），但**脏数据不该让 finish 炸** —— 记账比层级的
        # 完整性更要紧（少一次刷新 = 少一次冗余写回，不影响真相源）。
        logger.warning("note queue: item %s references missing batch %s", item.id, parent_id)
        return
    refresh_batch_status(db, parent)


def count_open_items(db: Session, *, item_task_type: str) -> int:
    """还有多少条待跑 / 在跑（worker 判断要不要继续转、启动时判断要不要唤醒）。"""
    return int(
        db.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.task_type == item_task_type, Task.status.in_(("pending", "running")))
        )
        or 0
    )


def queue_summaries(db: Session, *, user_id: int) -> list[dict]:
    """队列清单 + 每个队列的实时计数（`GET /tasks/queues`，队列记录页顶部小卡用）。

    计数按 `user_id` 收口（DEF-066）：`open` = 待跑 + 在跑，`done` / `failed` 是
    **保留期内**的终态数（清理任务每天 07:00 删 15 天前的）—— 不是全库累计，
    页面文案因此写「近 15 天」而不是「累计」。
    """
    cutoff = shanghai_now() - timedelta(days=DEFAULT_RETENTION_DAYS)
    result: list[dict] = []
    for spec in QUEUE_REGISTRY:
        row = db.execute(
            select(Task.status, func.count())
            .where(
                Task.user_id == user_id,
                Task.task_type == spec.item_task_type,
                Task.created_at >= cutoff,
            )
            .group_by(Task.status)
        ).all()
        counts = {str(status): int(count) for status, count in row}
        result.append(
            {
                "key": spec.key,
                "label": spec.label,
                "item_task_type": spec.item_task_type,
                # 批次父行类型：**只给前端 `queueKeyForTask` 把批次行归回本队列**
                # （不上屏 —— 它是注册表常量，每个队列恒定；2026-09-26 用户口径
                # 「直接使用队列类型不就可以了，唯一 key 肯定是唯一的，不需要多
                # 一个批次」。真实批次号在任务行的 `batch_no` 上，不由清单下发）
                "batch_task_type": spec.batch_task_type,
                "note_bound": spec.note_bound,
                "open": counts.get("pending", 0) + counts.get("running", 0),
                "done": counts.get("done", 0),
                "failed": counts.get("failed", 0) + counts.get("exhausted", 0),
                "cancelled": counts.get("cancelled", 0),
            }
        )
    return result


def today_start(now: Optional[datetime] = None) -> datetime:
    """当日 00:00（上海时区，与库内 `created_at` / `finished_at` 同源）。"""
    current = now or shanghai_now()
    return current.replace(hour=0, minute=0, second=0, microsecond=0)


def today_item_counts(db: Session, *, user_id: int, item_task_type: str) -> dict:
    """**当日**队列项三数（内容库两张进度卡用，2026-09-26 用户口径）。

    用户口径原文：「内容库页的同步进度卡和笔记发布进度卡，只统计当日发布或同步的
    数据，在卡片右下角小字展示仅当日数据，**统计队列记录表中的数据**」⇒ 数字不再由
    页面按自己入队的篇累计（刷新页面就丢、也看不见别的入口入队的），改为**从
    `tasks` 表现算**：

    * `ok` = `done` —— **今天出结果的**成功项；
    * `failed` = `failed` + `exhausted`（与 `queue_summaries` 同一归并口径）；
    * `pending` = `pending` + `running` —— **当前还没出结果的**（队列实时态）。

    ⚠️ **两把尺子各管一段，不要合成一条**：
      * 终态按 `finished_at >= 当日 00:00` 收口 —— 「今天成功/失败了几篇」问的是
        **跑完的那天**。按 `created_at` 收口会把「昨晚入队、今晨跑完」的篇漏掉
        （恢复重建的续跑行就是这种：`created_at` 还是昨天）；
      * 未终态**不设窗口** —— 「待处理」问的是队列**现在**还有没有活，跨天的滞留项
        照样得算进来（否则卡上显示待处理 0，队列却在跑）。
    `cancelled` **不进任何一档**（取消既不是成功也不是失败，硬塞会伪造成败）。

    ⚠️ 按 `user_id` 收口（DEF-066 同规）：别人的队列数不进我的卡。
    ⚠️ 不按平台筛：队列行的 `platform` 恒为 `xhs`（`enqueue_item` 写死），
    队列本身不跨平台。
    """
    start = today_start()
    rows = db.execute(
        select(Task.status, func.count())
        .where(
            Task.user_id == user_id,
            Task.task_type == item_task_type,
            or_(
                Task.status.in_(("pending", "running")),
                and_(Task.status.in_(("done", "failed", "exhausted")), Task.finished_at >= start),
            ),
        )
        .group_by(Task.status)
    ).all()
    counts = {str(status): int(total or 0) for status, total in rows}
    return {
        # 统计口径的日期（`YYYY-MM-DD`）——「仅当日数据」小字说的是哪一天。
        "date": start.date().isoformat(),
        "ok": counts.get("done", 0),
        "failed": counts.get("failed", 0) + counts.get("exhausted", 0),
        "pending": counts.get("pending", 0) + counts.get("running", 0),
    }


def list_note_pks_by_status(
    db: Session,
    *,
    item_task_type: str,
    statuses: tuple[str, ...] = ("pending",),
) -> list[int]:
    """按状态列出队列项对应的 `note_pk`（FIFO 序）。

    worker 的「同批一次补齐」优化用：走在前面的篇发现自己缺详情地址时，把**队列里
    同样缺地址的篇一次请求全补上**（`_backfill_detail_ref_for_accounts`），否则
    N 篇本地生成笔记要打 N 次千帆（1–2 秒/次）。DB 版按 `id` 升序 = 入队序。
    """
    rows = db.scalars(
        select(Task)
        .where(Task.task_type == item_task_type, Task.status.in_(statuses))
        .order_by(Task.id)
    ).all()
    return [pk for pk in (note_pk_of(row) for row in rows) if pk]


# --------------------------------------------------------------------------- #
# 快照（进度卡；2026-09-26 常驻化后重定义）
# --------------------------------------------------------------------------- #
def snapshot(
    db: Session,
    *,
    user_id: int,
    item_task_type: str,
    platform: Optional[str] = None,
) -> dict:
    """队列快照（进度卡用）：**常驻队列的实时态 + 近期终态**。

    2026-09-26 批次层移除后（用户口径「不需要队列批次，队列是常驻」），「本轮」
    不再由批次行承担 —— 后端只如实描述**队列现在什么样**：

    * `running` = 正在跑的那一条（单 worker，至多一条）；
    * `queued` = 排队中的（FIFO 序）；
    * `finished` = **近期**终态项（按 `finished_at` 倒序取 `FINISHED_WINDOW` 条，
      含逐项 `ok` 与 `reason` —— 同步 / 生成失败不落库到笔记行，原因只在这里给一次）；
    * `total` = 上述三者计数之和（进度卡的停止条件「`total > 0` 且待处理为 0」
      靠它成立：跑过的项留在 `finished` 窗口里，`total` 不会一跑完就归零）。

    「本轮成败数」由**前端按自己入队的项累计**（后端不知道用户这一轮点了什么），
    因此本快照**不再有** `synced` / `failed` 计数字段 —— 带着它们只会让进度卡把
    「近期窗口」说成「本轮」。

    条目形状按队列而变：笔记绑定队列三键（`note_pk` / `title` / `note_id`，标题
    本地库取）；**商品生成队列**多两个键（`item_id` / `item_name` 演进后的
    `note_title`），见 :func:`_entry`。域契约改名（`synced` → `published` /
    `generated`）由各 service 的包装函数做，底座保持通用形状。
    """
    open_rows = db.scalars(
        select(Task)
        .where(
            Task.user_id == user_id,
            Task.task_type == item_task_type,
            Task.status.in_(("pending", "running")),
        )
        .order_by(Task.id)
    ).all()
    finished_rows = db.scalars(
        select(Task)
        .where(
            Task.user_id == user_id,
            Task.task_type == item_task_type,
            Task.status.in_(("done", "failed")),
        )
        .order_by(Task.finished_at.desc(), Task.id.desc())
        .limit(FINISHED_WINDOW)
    ).all()

    pks = sorted({note_pk_of(row) for row in [*open_rows, *finished_rows] if note_pk_of(row)})
    titles: dict[int, tuple[str, str]] = {}
    if pks:
        statement = select(Note).where(Note.id.in_(pks), Note.user_id == user_id)
        if platform:
            statement = statement.where(Note.platform == platform)
        for note in db.scalars(statement).all():
            titles[note.id] = (note.title or "", note.note_id)

    def _entry(row: Task) -> Optional[dict]:
        """一条队列项的展示体。**两种队列两种来源**：

        * 笔记绑定（同步 / 发布）：标题本地库取（前端直接显示），形状三键不变；
        * 商品生成（`note_pk == 0`）：生成前笔记不存在 ⇒ 取行上的 `item_id` /
          `item_name`，终态后 `payload` 里的 `note_title` / `note_id` 由
          `finish_item(extra=…)` 补。**两条路都不许返回 None 把项丢掉** ——
          进度卡少一条 = 用户看到「总数对不上」。
        """
        payload = row.payload if isinstance(row.payload, dict) else {}
        note_pk = note_pk_of(row)
        if note_pk:
            if note_pk not in titles:
                return None
            title, note_id = titles[note_pk]
            return {"note_pk": note_pk, "title": title, "note_id": note_id}
        item_id = str(payload.get("item_id") or "")
        if not item_id:
            return None  # 既没笔记又没商品 = 脏行
        title = str(payload.get("note_title") or payload.get("item_name") or item_id)
        return {
            "note_pk": 0,
            "item_id": item_id,
            "title": title,
            "note_id": str(payload.get("note_id") or ""),
        }

    running_entry: Optional[dict] = None
    queued_entries: list[dict] = []
    for row in open_rows:
        status = str(row.status or "")
        base = _entry(row)
        if status == "running":
            if base is not None:
                running_entry = base
        elif base is not None:
            queued_entries.append(base)

    finished: list[dict] = []
    for row in finished_rows:  # 已按 finished_at 倒序
        status = str(row.status or "")
        base = _entry(row)
        if base is None:
            continue
        payload = row.payload if isinstance(row.payload, dict) else {}
        ok = bool(payload.get("ok")) if status == "done" else False
        finished.append(
            {
                **base,
                "ok": ok,
                "status": "ok" if ok else "failed",
                "reason": str(payload.get("reason") or ""),
                "code": str(payload.get("code") or ""),
            }
        )

    return {
        "running": running_entry,
        "queued": queued_entries,
        "total": len(finished) + len(queued_entries) + (1 if running_entry is not None else 0),
        "finished": finished,
    }


# --------------------------------------------------------------------------- #
# 记录页读取（父子树；设计 13 §35.11，2026-09-26 用户口径「批次+详情父子树」+
# 「新增列表支持使用唯一批次号查询」）
# --------------------------------------------------------------------------- #
#: 记录页一次列多少**顶级行**（= 批次行，一次入队动作一行）。每行还能带 ≤ 20 个子行
#: （批量上限），所以 50 个批次 ≈ 最多 1050 行明细 —— 一页足够翻，不至于把
#: 「最近一次批量」挤出窗口。
QUEUE_RECORD_PAGE_SIZE = 50

#: 上限（端点 `limit` 的 le 用同一个数，别两处各写一个）。
QUEUE_RECORD_MAX_ROWS = 100


def list_queue_record_roots(
    db: Session,
    *,
    user_id: int,
    limit: int = QUEUE_RECORD_PAGE_SIZE,
    batch_no: str = "",
) -> list[Task]:
    """队列记录页的**顶级行** = **批次行**（只有批次行），按创建时间倒序。

    记录页是**批次 + 明细的父子树**（2026-09-26 用户口径「将队列执行详情表使用批次+
    详情父子树风格展示」）：顶级 = 一次入队动作（批次行），明细挂在它的 `children`。

    ⚠️ **不列「无父的队列项行」**（用户口径「不做兼容逻辑，数据已清空」）：批次行是
    队列项的唯一入口（`enqueue_item` 先建批次行、再挂子行），不再有「孤儿项」这种
    存量形态。以前为存量留的兼容分支已删 —— 数据不存在了还留着猜测路径，会让
    「顶级行 = 批次行」这个不变量在代码里说不清（也就守不住）。

    * `batch_no` 不为空 ⇒ 只列**批次号匹配**的批次行（2026-09-26 用户口径
      「新增列表支持使用唯一批次号查询」）。匹配用**不区分大小写的包含**：
      批次号是手抄 / 口述的东西，用户往往只记得尾巴四位数（`-7K2M`），卡死等值
      等于逼他一个字不差地打全。
    """
    statement = select(Task).where(
        Task.user_id == user_id,
        Task.task_type.in_(QUEUE_BATCH_TASK_TYPES),
    )
    key = str(batch_no or "").strip().upper()
    if key:
        statement = statement.where(func.upper(Task.batch_no).like(f"%{key}%"))
    safe_limit = max(1, min(int(limit), QUEUE_RECORD_MAX_ROWS))
    return list(
        db.scalars(
            statement.order_by(Task.created_at.desc(), Task.id.desc()).limit(safe_limit)
        ).all()
    )


# --------------------------------------------------------------------------- #
# 启动恢复（设计 13 §35.9.2）
# --------------------------------------------------------------------------- #
def recover_interrupted(
    db: Session,
    *,
    item_task_types: tuple[str, ...] = QUEUE_ITEM_TASK_TYPES,
    exclude_task_ids: Iterable[int] = (),
) -> dict:
    """启动恢复：上次进程留下的 `running` 行按口径复位（**自动续跑**）。

    口径（用户 2026-09-25 拍板）：**只自动重试「中断」** —— 进程退出 / 线程死亡
    留下的 `running` 行就是断点，自动回到 `pending` 重跑；**业务失败不自动重试**
    （平台拒绝 / 无凭据那种重跑一次还是失败，只会白烧限流额度），只标 `failed`
    等用户在任务记录里手动点「重试」。

    `retry_count` 用完（`>= max_retries`）的中断行转 `failed` +
    `error_type="interrupted"` —— 不无限循环（一个必然让进程挂掉的篇不该把启动
    拖成死循环）。

    复位行的来源改标 `recovery`（2026-09-26 用户口径「给每个来源固定值」）：
    这一跑是应用重启续跑，不是用户当时点的那次。

    `exclude_task_ids`（设计 13 §38.3，2026-09-27）：**跳过这些行**。

    ⚠️ 为什么池化后**必须**有它：看门狗在「池里死了一条、其余还活着」时也要复位，
    而本函数的判据是「所有 `running` 行」—— 不做排除就会把**活着的那几条 worker
    手上的任务从脚下抽走**（改回 `pending`）⇒ 同一条被跑两遍 + 原 worker 跑完后
    把一条已不属于它的行写成终态。启动路径不传（那时没有活 worker，全部该复位）。
    """
    now = shanghai_now()
    keep = {int(value) for value in exclude_task_ids if value is not None}
    rows = list(
        db.scalars(
            select(Task).where(
                Task.task_type.in_(item_task_types),
                Task.status == "running",
            )
        ).all()
    )
    if keep:
        rows = [row for row in rows if int(row.id) not in keep]
    requeued = failed = 0
    for row in rows:
        if int(row.retry_count or 0) < int(row.max_retries or 3):
            row.status = "pending"
            row.retry_count = int(row.retry_count or 0) + 1
            row.started_at = None
            # ⚠️ **`finished_at` 必须一并清**（2026-09-27 真机实测）：复位是
            # 「重新排队」，留着上一轮的结束时间 ⇒ 行处于「`pending`/`running`
            # 但已有 `finished_at`」的**自相矛盾**状态。后果是「任务结束了吗」
            # 有两个答案 —— 账号卡按钮读 `status`（显示「同步中」），任务中心
            # 读时间戳（显示已结束），用户看到的就是「任务已失败，卡片还在转」。
            row.finished_at = None
            payload = dict(row.payload or {})
            payload["source"] = SOURCE_RECOVERY
            row.payload = payload
            requeued += 1
        else:
            payload = dict(row.payload or {})
            payload.update(
                {"ok": False, "reason": "应用退出导致中断，自动重试次数已用尽", "code": "INTERRUPTED"}
            )
            row.payload = payload
            row.status = "failed"
            row.error_type = ERROR_INTERRUPTED
            row.finished_at = now
            failed += 1
    if rows:
        db.flush()
        db.commit()
    if requeued or failed:
        logger.info(
            "note queue recovery: requeued=%s failed=%s", requeued, failed
        )
    return {"requeued": requeued, "failed": failed}


def recover_publishing_notes(db: Session) -> int:
    """把卡在 `publishing` 的笔记行复位（启动恢复的**第二半**）。

    为什么必须有：`_execute_publish` 先把行置 `publishing` 落库，平台确认后再写
    `published`。进程若在这两步之间退出，那一行就**永远**停在 `publishing`
    —— 既不会被队列重跑，也不会出现在任何终态统计里（现有缺口，本轮补上）。
    复位成 `pending` ⇒ 它可被重新入队 / 重新发布。

    ⚠️ **只在启动时调用一次**：正常运行时 `publishing` 是「正在跑」的合法状态，
    在跑的过程中复位就是把它从 worker 脚下抽走。
    """
    from backend.app.models.note import PUBLISH_STATUS_PENDING, PUBLISH_STATUS_PUBLISHING

    result = db.execute(
        update(Note)
        .where(Note.publish_status == PUBLISH_STATUS_PUBLISHING)
        .values(publish_status=PUBLISH_STATUS_PENDING)
    )
    db.commit()
    if result.rowcount:
        logger.warning(
            "note queue recovery: reset %s note(s) stuck in publishing", result.rowcount
        )
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------- #
# 手动清理（2026-09-26 用户口径「新增手动批量清理」）
# --------------------------------------------------------------------------- #
#: **可被手动清理的状态**：只有终态且**不可重试**的两种。
#: `done`（成功）/ `cancelled`（用户自己取消的）没有后续动作，删掉不丢任何东西。
#:
#: ⚠️ **另外两种终态不许清**：`failed` / `exhausted` 带「重试」按钮（待重试）——
#: 删了用户就失去自救路径；`pending` / `running` 是活的队列项（进行中）—— 删了
#: 就是丢任务（清理入口绝不能变成第二个丢任务的入口，与保留期清理同纪律）。
#: 用户口径原文：「清理逻辑不包含待重试和进行中状态的记录，前端不允许被选中」。
CLEANABLE_STATUSES: tuple[str, ...] = ("done", "cancelled")

#: **正在执行 / 待执行**：用户口径「批次中存在正在执行或待执行得记录，则不支持该
#: 批次全部清理」里点名的两类 —— 活的队列项，worker 会（或正在）认领，删了就是丢任务。
ACTIVE_STATUSES: tuple[str, ...] = ("pending", "running")

#: **待重试**（`failed` / `exhausted`）：行上带「重试」入口，删了用户失去自救路径。
RETRYABLE_STATUSES: tuple[str, ...] = ("failed", "exhausted")


def batch_clean_blockers(children: list[Task]) -> dict:
    """一个批次**能不能整批清理**：返回阻塞计数 `{"active": n, "retryable": m}`。

    清理有**两个维度**（2026-09-26 用户口径「支持按照批次清理记录……清理功能分为两个
    维度，批次维度和批次内的操作」）：

    * **批次维度**（点名批次行 id / 一键清除）＝ 批次与它的**全部明细**一次性删掉：
      只有两个计数都是 0 才允许（此时子行只可能是 `done` / `cancelled`）；
    * **批次内维度**（点名明细行 id）＝ 只删这一条，批次留着（`CLEANABLE_STATUSES`
      说了算）。

    为什么不「跳过那几条、清其它的」：整批清理是**原子动作**（用户口径是「不支持该
    批次**全部**清理」，不是「部分清理」）。半清会在父子树里留下一个残缺的批次 ——
    用户下次看到「共 3 条明细」却只剩 1 条，会以为记录丢了。要清单条请用批次内维度
    （展开明细逐条清）。
    """
    statuses = [str(child.status or "") for child in children]
    return {
        "active": sum(1 for status in statuses if status in ACTIVE_STATUSES),
        "retryable": sum(1 for status in statuses if status in RETRYABLE_STATUSES),
    }


def clean_records(
    db: Session,
    *,
    user_id: int,
    task_ids: Optional[list[int]] = None,
) -> dict:
    """手动清理队列详情记录（用户口径 2026-09-26「新增手动批量清理」）。

    * `task_ids` 给定时**只清这些**（单条 = 一个 id 的批量；批量 = 选中的多个）；
      为 `None` 时清本用户**全部可清**记录（「一键清除全部」）；
    * **只删 `CLEANABLE_STATUSES`**（`done` / `cancelled`）：待重试（`failed` /
      `exhausted`）与进行中（`pending` / `running`）一律不删 —— 见
      `CLEANABLE_STATUSES` 上方的口径说明；
    * 按 `user_id` 收口（DEF-066）：别家的行**物理上够不着**（WHERE 自带归属）；
    * 显式给 id 时**类型不限**（用户手动选中即明确意图，以状态判据为准 —— 类型是
      展示维度，不是安全维度；状态才是）。「一键清除全部」则**只在本页认识的
      队列记录范围内**删（批次行 + 队列项行）：页面上看不到的行（账号级同步 /
      AI / 抓取）不该在这里被顺手清掉。

    **两个维度（2026-09-26 用户口径「支持按照批次清理记录，批次中存在正在执行或
    待执行得记录，则不支持该批次全部清理……分为两个维度，批次维度和批次内的操作」）**：

    * **批次维度**：点名**批次行** ⇒ 整批清理（批次 + 全部子行一次删）。⚠️ 批内只要
      有**正在执行 / 待执行**（`pending` / `running`）或**待重试**（`failed` /
      `exhausted`）的明细 ⇒ **不支持整批清理**（`batch_clean_blockers`）：整批是原子
      动作，半清会留下残缺批次；待重试的明细删了用户还失去自救路径；
    * **批次内维度**：点名**明细行** ⇒ 只删这一条（按 `CLEANABLE_STATUSES`），批次
      留着（顺手刷新父行的派生状态）。用户想清掉一条已成功的明细、同批还有要重试的
      篇时，不该被迫整批保留；
    * **一键清除全部**（`task_ids=None`）：只清**整批可清**的批次（含它们的明细）；
      批内有活 / 待重试明细的，**连它可清的明细也不动** —— 一键清是兜底动作，把还在
      使用的批次拆散只会让用户看不懂「这批怎么只剩两条」。

    返回（`cleaned + skipped == requested` 恒成立）：

    * `cleaned` / `skipped` / `requested` —— 行数总计（批次被清时，被它一起带走的
      **被选中**子行也计入 `cleaned`）；
    * `batches` —— `{"cleaned": a, "blocked": b}`：批次维度的结果（`blocked` = 因
      批内有活 / 待重试明细而**整批未清**的批次数）；
    * `items` —— `{"cleaned": c, "skipped": d}`：明细维度的结果；
    * `blocked` —— 被挡下的批次清单（`id` / `batch_no` / `active` / `retryable`）：
      前端据此说明「为什么这个批次没清掉」，而不是只回一个冷冰冰的 `skipped`。
      ⚠️ 前端已禁用不可清的按钮，这里是**后端第二道闸** —— 安全网不能条件性
      （DEF-048：前端禁用是体验，后端拒绝是边界）。
    """
    statement = select(Task).where(Task.user_id == user_id)
    if task_ids is not None:
        cleaned_ids = [int(i) for i in task_ids if str(i).strip()]
        if not cleaned_ids:
            return {"cleaned": 0, "skipped": 0, "requested": 0}
        statement = statement.where(Task.id.in_(cleaned_ids))
    else:
        # 一键清除全部：只认队列记录（批次行 + 队列项行），别的类型不碰。
        statement = statement.where(
            Task.task_type.in_((*QUEUE_ITEM_TASK_TYPES, *QUEUE_BATCH_TASK_TYPES))
        )
    rows = list(db.scalars(statement).all())

    batch_rows = [row for row in rows if str(row.task_type or "") in QUEUE_BATCH_TASK_TYPES]
    item_rows = [row for row in rows if str(row.task_type or "") not in QUEUE_BATCH_TASK_TYPES]

    cleaned = skipped = 0
    batch_stats = {"cleaned": 0, "blocked": 0}
    item_stats = {"cleaned": 0, "skipped": 0}
    blocked: list[dict] = []
    blocked_batch_ids: set[int] = set()

    # ① **批次维度**：批内没有「正在执行 / 待执行」也没有「待重试」的明细 ⇒ 整批
    #    （批次 + 全部子行）一次删；否则**整批不动**（原子动作，不半清）。
    swallowed: set[int] = set()
    for batch in batch_rows:
        children = children_of(db, parent_id=int(batch.id), user_id=user_id)
        blockers = batch_clean_blockers(children)
        if blockers["active"] or blockers["retryable"]:
            batch_stats["blocked"] += 1
            skipped += 1
            blocked_batch_ids.add(int(batch.id))
            blocked.append({"id": int(batch.id), "batch_no": batch.batch_no, **blockers})
            continue
        for child in children:
            swallowed.add(int(child.id))
            db.delete(child)
        # ⚠️ **必须 flush 把子行的 DELETE 先发出去，再删父行**（2026-10-04 实测）：
        # `tasks.parent_task_id -> tasks.id` 是自引用外键，全局 `PRAGMA
        # foreign_keys=ON`（core/database.py）之后，SQLAlchemy 的 unit of work
        # **不保证**按 `delete()` 调用顺序发语句 —— 实测它先发父行的 DELETE
        # ⇒ `FOREIGN KEY constraint failed`，整批清理失败（真机同样会中招，
        # 不只是测试）。flush 一次把顺序钉死；代价是每批一次额外往返。
        db.flush()
        db.delete(batch)
        batch_stats["cleaned"] += 1
        cleaned += 1

    # ② **批次内维度** / 一键清除的明细部分。被批次带走的子行**也计入 cleaned**
    #   （它确实被清掉了），否则「选了批次 + 它的子行」会让
    #   cleaned + skipped < requested。
    touched_parents: set[int] = set()
    for row in item_rows:
        if int(row.id) in swallowed:
            cleaned += 1
            item_stats["cleaned"] += 1
            continue
        parent_id = int(row.parent_task_id or 0)
        # ⚠️ 一键清除（没点名具体行）时**不拆散**被挡下的批次：批内那几条可清明细
        #    也一并跳过 —— 兜底动作不该把还在使用的批次清成残的。要点名清单条走
        #    批次内维度（用户可以展开明细逐条清）。
        if task_ids is None and parent_id in blocked_batch_ids:
            skipped += 1
            item_stats["skipped"] += 1
            continue
        if str(row.status or "") in CLEANABLE_STATUSES:
            if parent_id:
                touched_parents.add(parent_id)
            db.delete(row)
            cleaned += 1
            item_stats["cleaned"] += 1
        else:
            skipped += 1
            item_stats["skipped"] += 1

    # ③ 被清掉子行的批次：派生状态可能变了（例如同批里的失败篇被清掉后整批变可清），
    #    写回一次让它与「按子行现算」一致（写回是冗余，不是真相源 —— 见
    #    `refresh_batch_status`）。父行自己已被删的（同批清掉）就跳过。
    for parent_id in touched_parents:
        parent = db.get(Task, parent_id)
        if parent is not None:
            refresh_batch_status(db, parent)

    db.commit()
    result = {
        "cleaned": int(cleaned),
        "skipped": int(skipped),
        "requested": len(rows),
        "batches": batch_stats,
        "items": item_stats,
        "blocked": blocked,
    }
    logger.info("queue records manual clean: %s", result)
    return result


# --------------------------------------------------------------------------- #
# 保留期清理（定时任务 T-32，每天 07:00）
# --------------------------------------------------------------------------- #
def cleanup_records(
    db: Session,
    *,
    user_id: int,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    cutoff: Optional[datetime] = None,
) -> dict:
    """删掉本用户**超过保留期**的终态队列详情记录（`QUEUE_RECORD_TYPES`）。

    **只碰终态**（`done` / `failed` / `cancelled`）：`pending` / `running` 是活的
    队列项，删了就是丢任务（清理任务绝不能变成第二个丢任务的入口）。

    **批次层的顺序不可换**（2026-09-26 批次层回来）：

    1. 先删队列项行（含批次子行）—— 终态 + 超期；
    2. 再删**已无任何子行**的超期批次行。

    反过来（先删批次行）会：① 撞自引用外键（子行还指着父行）；② 把还活着的子行
    变成孤儿。条件「已无子行」正好等价于「这一批的篇全清了 / 本来就是空的」，而
    **还有子行就说明还有兜不住的活项**（能清的都清了，剩下的只能是 `pending` /
    `running` / 超期后被重试过的）⇒ 批次跟着留下，与「活任务永不删」同纪律。

    类型仍**从注册表派生**（新队列上线自动纳入）：写死就等于「第三种队列的记录
    永远不清」，又是一种静默漏项（DEF-044 同型）。

    返回 `{"retention_days": n, "cutoff": iso, "items": n, "batches": n,
    "account_tasks": n}`。
    """
    if cutoff is None:
        cutoff = shanghai_now() - timedelta(days=int(retention_days))
    # 类型**从注册表派生**（不写死两个）：新队列上线时清理范围自动跟上。
    item_types = tuple(spec.item_task_type for spec in QUEUE_REGISTRY)
    batch_types = tuple(spec.batch_task_type for spec in QUEUE_REGISTRY)

    items = db.execute(
        delete(Task).where(
            Task.user_id == user_id,
            Task.task_type.in_(item_types),
            Task.status.in_(TERMINAL_STATUSES),
            Task.created_at < cutoff,
        )
    ).rowcount

    # 超期 + 已无子行的批次行。用「先查 id 再按 id 删」而不是一条 DELETE 带相关子查询：
    # 自引用子查询在 SQLite / PG 上的支持面不一致（本仓两种库都要能跑），且分两步的
    # 意图（父行还留着子行就不删）在读代码时一眼可见。
    child = aliased(Task)
    stale_batch_ids = [
        int(row_id)
        for row_id in db.scalars(
            select(Task.id).where(
                Task.user_id == user_id,
                Task.task_type.in_(batch_types),
                Task.created_at < cutoff,
                ~select(child.id).where(child.parent_task_id == Task.id).exists(),
            )
        ).all()
    ]
    batches = 0
    if stale_batch_ids:
        batches = db.execute(delete(Task).where(Task.id.in_(stale_batch_ids))).rowcount

    # 账号级任务行（§35.16）：粒度是「一次账号级动作」，不在 `QUEUE_REGISTRY` 派生集里，
    # 但同为「队列详情记录」⇒ 同一保留期。原实现只覆盖 `xhs_note_sync`，另两类漏清。
    account_tasks = db.execute(
        delete(Task).where(
            Task.user_id == user_id,
            Task.task_type.in_(ACCOUNT_TASK_TYPES),
            Task.status.in_(TERMINAL_STATUSES),
            Task.created_at < cutoff,
        )
    ).rowcount

    db.commit()
    result = {
        "retention_days": int(retention_days),
        "cutoff": cutoff.isoformat(),
        "items": int(items or 0),
        "batches": int(batches or 0),
        "account_tasks": int(account_tasks or 0),
    }
    logger.info("note task record cleanup: %s", result)
    return result


def cleanup_queue_records(user_id: int) -> dict:
    """定时任务入口（`system.queue_record_cleanup`，每天 07:00）。

    `user_id` 由 runner 注入（`user_scoped=True`，DEF-066）⇒ **只清行主本人的**
    队列详情记录，不越租户。保留期固定 15 天（`DEFAULT_RETENTION_DAYS`）—— 口径是
    「最长保留 15 天」，不做成用户可调项（可调会让「能翻多久的历史」变成每个人
    不一样，排查时对不上口径）。
    """
    db = SessionLocal()
    try:
        return cleanup_records(db, user_id=user_id)
    finally:
        db.close()
