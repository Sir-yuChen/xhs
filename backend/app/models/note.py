from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

#: 笔记类型取值（设计 13 §25，2026-09-22）—— **唯一定义处**，业务代码只许引用常量。
NOTE_TYPE_SYNCED = "synced"
NOTE_TYPE_LOCAL = "local"
#: **本地生成**（设计 13 §31，2026-09-23）：商品笔记智能生成链路落库。
#: 与 `local` 的分工：`local` 是采集/监控/批量保存搬来的**别人**的笔记；
#: 本值是本项目为「自己店铺的商品」AI 生成、尚未发布到平台的笔记。
#: 判据同样是**落库链路**（只升不降）：`product_note_gen_service` 落库即本值，
#: 同步链路命中也只升 `synced`，不回退。
NOTE_TYPE_LOCAL_GENERATED = "local_generated"

#: 笔记**形态**（设计 41 §15，2026-10-16）—— 与小红书平台的**唯二两大类**一一对应：
#: 「**图文笔记**」（1–18 张图）与「**视频笔记**」（1 个视频 + 独立封面）。
#:
#: ⚠️ **平台侧两者互斥**（官方 `xhs.postNote` 契约：`image_resources` /
#: `video_resources` 只能二选一，多传触发校验失败），所以这是**单值枚举**，
#: 不是可叠加的标签。发布时它就是 `common.type` 的取值来源
#: （`image` → `"normal"` / `video` → `"video"`）。
#:
#: ⚠️ **与 `note_type` 正交，别混**（设计 33 §4 的纪律）：`note_type` 说的是
#: 「这条数据从哪来」（同步 / 采集 / 本地生成），`note_form` 说的是
#: 「这条笔记长什么样」（图文 / 视频）。改一个不许顺手改另一个。
NOTE_FORM_IMAGE = "image"
NOTE_FORM_VIDEO = "video"
#: 全部合法取值（写入入口的校验依据）。
NOTE_FORMS = (NOTE_FORM_IMAGE, NOTE_FORM_VIDEO)

#: 笔记**分类**（「我的笔记」离线数据，2026-10-06 需求）：与小红书平台一致，
#: 分「商业笔记」（挂载商品）与「普通笔记」两类。
#: ⛔ **已退役（2026-10-12，设计 35）**：分类权威改 `note_datacenter_stats.note_flag`
#: （平台原生），本列已 DROP、常量已删。商业/普通由千帆数据中心的下载入口决定，
#: 判据只认统计表里的 `note_flag`，不再有「两写入者、判据不同源」的问题。

#: 本地生成笔记的**发布状态机**（设计 13 §31.3.1；**口径被 §32.3 取代**）——
#: 现在是**所有笔记**的发布状态（不再有「空 = 不适用」这一态）：
#: `pending` 待发布 → `publishing` 发布中 → `published` 已发布 / `failed` 发布失败。
#: `failed` 可重发（回到 `publishing`），成功即 `published`。
#:
#: `publishing` 是**内部瞬时态**（发布中崩溃重启后不会被误判为可重发），
#: 界面照常展示为「发布中」，它不是第四种业务状态。
PUBLISH_STATUS_PENDING = "pending"
PUBLISH_STATUS_PUBLISHING = "publishing"
PUBLISH_STATUS_PUBLISHED = "published"
PUBLISH_STATUS_FAILED = "failed"
#: 全部合法取值（写入入口的校验依据）。
PUBLISH_STATUSES = (
    PUBLISH_STATUS_PENDING,
    PUBLISH_STATUS_PUBLISHING,
    PUBLISH_STATUS_PUBLISHED,
    PUBLISH_STATUS_FAILED,
)
#: 可发起发布的状态（门禁判据只此一处）。
PUBLISHABLE_STATUSES = (PUBLISH_STATUS_PENDING, PUBLISH_STATUS_FAILED)
#: 内容库**筛选档 → 实际匹配的存储值**（设计 13 §32.3.3）—— 筛选与统计**共用这一份**
#: 映射，避免「统计把 `publishing` 算进待发布、筛选却不含它」这类两处漂移。
#: `pending` 档含瞬时的 `publishing`（它还没发出去）与迁移前的空值 `""`。
PUBLISH_STATUS_FILTER_GROUPS: dict[str, tuple[str, ...]] = {
    PUBLISH_STATUS_PENDING: (PUBLISH_STATUS_PENDING, PUBLISH_STATUS_PUBLISHING, ""),
    PUBLISH_STATUS_PUBLISHED: (PUBLISH_STATUS_PUBLISHED,),
    PUBLISH_STATUS_FAILED: (PUBLISH_STATUS_FAILED,),
}


def set_note_publish_status(note: "Note", status: str, *, error: str = "") -> None:
    """**发布状态的唯一写入入口**（设计 13 §32.3.2）—— 同步 / 采集 / 手动切换 /
    发布链路全部走这里，别处不许直接改 `note.publish_status` 或 `note.published`。

    为什么要有这个函数：`publish_status`（四态）与 `published`（布尔）表达的是同一个
    事实的两个粒度，两处各写一次就必然漂移（DEF-044/047 同型：一个语义两份实现）。
    本函数把它们当**一次写入**：状态是权威，`published` 是它的**投影**。

    | 入参 `status` | 副作用 |
    |---|---|
    | `published` | `published=True`、清空 `publish_error`、首次写 `published_at` |
    | `failed` | `published=False`、写 `publish_error`（平台原文；空则保留旧值） |
    | `pending` / `publishing` | `published=False`、清空 `publish_error`（重发中不留旧原因） |

    `published_at` **只写不清**（三方发布成功是历史事实；手动切回待发布也不抹掉）。
    不合法取值抛 `ValueError`（调用方是代码不是用户输入，静默忽略等于埋雷）。
    """
    if status not in PUBLISH_STATUSES:
        raise ValueError(f"未知的发布状态：{status!r}（合法值：{PUBLISH_STATUSES}）")
    note.publish_status = status
    note.published = status == PUBLISH_STATUS_PUBLISHED
    if status == PUBLISH_STATUS_PUBLISHED:
        note.publish_error = ""
        if note.published_at is None:
            note.published_at = shanghai_now()
    elif status == PUBLISH_STATUS_FAILED:
        note.publish_error = error or note.publish_error or ""
    else:
        note.publish_error = ""


def note_supports_sync(note: "Note") -> bool:
    """这条笔记能不能「拉平台最新内容」—— **同步按钮/同步接口的唯一判据**。

    设计 13 §25.7.5（2026-09-23 用户口径：「已经发布过的笔记一定存在同步按钮功能」）。

    两个条件任一成立即返回 `True`：

    1. `note_type == "synced"` —— 三方同步笔记，本来就有平台最新内容可拉；
    2. `publish_status == "published"` —— **发布过**（含本地生成后发布成功的）。

    ⚠️ 条件 2 的理由：**发布过意味着这条笔记在平台上真实存在**，于是它就有了
    「拉最新内容」的对象 —— 这与它是怎么落库来的无关。漏掉它的实际表现
    （真实库已复现，id 1966「新中式桌垫…」）：本地生成笔记发布成功后
    `note_type` **仍是** `local_generated`（§31：生成落库时定类型，发布只改
    `publish_status`）⇒ 只看类型的旧判据把它漏掉，用户看到「明明发出去了，
    却没有同步」。

    ⚠️ 判据用 **`publish_status` 而不是 `published` 投影**（§32.3.2）：
    前者是权威字段，后者只是给 CSV / 既有消费者的投影；判定「用户能做什么事」
    必须看权威那一列。

    ⚠️ **前后端各有一份、必须同口径**：前端在
    `frontend/src/lib/note-filters.ts::canSyncNote()`（渲染层，决定按钮在不在），
    本函数是后端闸门（`POST /notes/{id}/sync`）。两处判据一致时用户体验才一致；
    改一边忘另一边 = 按钮显示了却 400，或按钮没有却还能调。
    """
    if note is None:
        return False
    if (getattr(note, "note_type", "") or "") == NOTE_TYPE_SYNCED:
        return True
    return (getattr(note, "publish_status", "") or "") == PUBLISH_STATUS_PUBLISHED


def note_supports_batch_sync(note: "Note") -> bool:
    """这条笔记能不能进**批量同步**队列（设计 13 §35.1，2026-09-25 用户口径）。

    判据只有一条：`publish_status == "published"`（用户口径原文：「只有已发布的
    支持被选中」）。

    ⚠️ 比 `note_supports_sync()`（单条「同步」按钮：`synced` **或** `published`）
    **更严**，这是刻意的两把尺子，各守各的门：

    * 单条按钮回答「这条**有没有**可拉的平台最新内容」—— synced 笔记天然有
      （§25.7.5 口径，不回归）；
    * 批量同步是「把一批**已发布**笔记刷新到平台最新」的显式动作 —— 只收已发布。

    **改一边不默认改另一边**：两份判据分别写在各自 docstring 里，同改必须两边都改
    （与 `note_supports_sync` 的前后端同口径纪律同款）。

    前后端同口径：前端 `lib/note-filters.ts::canBatchSyncNote()`（界面侧：确认框
    明示跳过数），本函数是后端闸门（`POST /notes/batch-sync` 逐篇校验）。
    """
    if note is None:
        return False
    return (getattr(note, "publish_status", "") or "") == PUBLISH_STATUS_PUBLISHED


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (
        #: **笔记身份唯一**（台账 §4.9 / 不变量 I2，迁移 `d4e5f6a7b8c9`）：
        #: 同一 `(user_id, note_id)` 只允许一行，落库即**覆盖更新**。
        #:
        #: 键**不含 `platform`**，与代码侧两处 upsert 的查询条件严格一致
        #: （`crawl.py::_save_normalized_notes`、`notes.py::batch_save_notes` 都是按
        #: `(user_id, note_id)` 查既有行）—— 索引与代码口径不一致时，约束挡不住
        #: 真正的重复路径。
        #:
        #: 覆盖更新时**不换 `notes.id`**，因此 `note_assets` / `note_tags` / 草稿引用
        #: 不会被打断；命中既有行时同时刷新 `platform_account_id`，账号换行后笔记能
        #: 重新挂回（旧实现漏了这一步，导致账号删除后笔记永久悬空）。
        UniqueConstraint("user_id", "note_id", name="uq_notes_user_note"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, default=0)
    platform_account_id: Mapped[int] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    note_id: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    author_name: Mapped[str] = mapped_column(String(128), default="")
    raw_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    #: **是否已发布过**（内容库工作流标识，2026-09-22 新增，迁移 `n6o7p8q9r0s1`）。
    #:
    #: 由**落库链路**决定，不在读取侧猜（设计 13 §19.12）：
    #: * 笔记同步链路（www / creator / ark 三源）落库 ⇒ `True`；
    #: * 采集（`crawl.py` ×3）、监控采集、`batch_save_notes` ⇒ 默认 `False`。
    #:
    #: **只升不降**：同步侧只在「置 True」时下笔，采集侧不碰既有行 —— 否则
    #: 「先同步、后发现页又采集到同一条」会把已发布降级成待发布。
    #:
    #: ⚠️ **2026-09-23 起它是 `publish_status` 的投影**（设计 13 §32.3.2）：
    #: `published ⟺ publish_status == "published"`，**不许两处各写一次** ——
    #: 所有写入（同步 / 采集 / 手动切换 / 发布链路）统一经
    #: :func:`set_note_publish_status`，本列不再单独赋值。列之所以保留：CSV 导出
    #: 与既有消费者读它，删列只换来一个列名、却要动迁移与导出契约。
    published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    #: **笔记类型**（设计 13 §25，2026-09-22 新增，迁移 `q9r0s1t2u3v4`）：
    #: 「这条数据是谁搬来的」—— `synced` = 三方同步笔记（**同步链路**落库：
    #: `note_sync_service` 的 creator / ark 源 + `reconcile_service` 补落库）；
    #: `local` = 本地笔记（采集 / 监控 / `batch_save_notes` 落库）。
    #:
    #: 用户 2026-09-22 拍板的分类口径：「定时任务同步得均为三方，只有本地创建生成得
    #: 才是本地」⇒ 判据是**落库链路**，与 `published`（§19.12）同一把尺子：不按触发
    #: 方式判（定时任务与账号卡片手动同步走**同一条路**，都是三方）、不按 `raw_json`
    #: 反推（反推 = 造第二份真相源）。**只升不降**：同步命中既有行升 `synced`；
    #: 采集命中既有行一个字节都不碰 —— 否则「先同步、后采集到同一条」会把同步笔记
    #: 变成本地笔记、详情卡的「同步」按钮消失。
    #:
    #: 与 `published` 的分工：`published` 答「这条在平台上是否已发布」（**用户可
    #: 手动切**的工作流标识）；本列答「这条是谁搬来的」（**用户不可改**的来源
    #: 标识）。把来源钉在可手动切换的列上，一次误切就改掉来源判据 ⇒ 两列不是一列。
    #:
    #: 存量行由迁移按 §19.12 的已文档化不变量回填：`published=1 ⟺ 同步链路落库`。
    note_type: Mapped[str] = mapped_column(
        String(16), default=NOTE_TYPE_LOCAL, nullable=False, server_default=NOTE_TYPE_LOCAL
    )
    #: **笔记形态**（设计 41 §15）—— `image`（图文，1–18 图）/ `video`（视频 + 封面）。
    #: 与 `note_type` **正交**：本列说的是「长什么样」，`note_type` 说的是「从哪来」。
    #: 发布时本列决定 `common.type`（`image` → `"normal"` / `video` → `"video"`）。
    #: 存量行由迁移回填 `image`（历史只成功发布过图文，见迁移 docstring）。
    note_form: Mapped[str] = mapped_column(
        String(16), default=NOTE_FORM_IMAGE, nullable=False, server_default=NOTE_FORM_IMAGE, index=True
    )
    #: ⛔ **`note_category` 已退役并 DROP（2026-10-12，设计 35）**。
    #: 原「笔记分类」（商业/普通）判据收敛为 `note_datacenter_stats.note_flag`（平台原生），
    #: 本列不再存在。历史数据已由数据脚本 `.hermes/scripts/migrate_datacenter_stats.py`
    #: 迁进统计表。分类不再落在 `notes` 表上 —— 笔记删除不影响数据中心的历史统计。
    #: **话题（#标签名列表）**（设计 13 §28，2026-09-22 新增，迁移 `s1t2u3v4w5x6`）。
    #: Text 列存 JSON 数组（与 `shop_products.images_json` 同款记法），默认 `[]`。
    #:
    #: 为什么解耦成专有列：话题是本系统自己要渲染的语义，此前只活在 `raw_json`
    #: （平台原文）里，每个读方都得自己挖一遍形状（creator 扁平 / PC 嵌套）——
    #: 挖法漂移就是显示漂移。`raw_json` 原样保留（证据链），本列是它的**投影**。
    #:
    #: 写入方：详情补齐（`hydrate_note_detail`，详情拿到非空话题才覆盖）与列表
    #: 落库（`_save_normalized_notes`，条目自带话题才写）。空 = 没解析到，不造假。
    #:
    #: ⚠️ **商品信息不进本表**（用户 2026-09-22 追加口径：「笔记表不要再存商品信息，
    #: 直接通过笔记和商品关联表进行查询」）：挂载商品在 `shop_products`（商品信息
    #: 单独一张表），关系在 `note_goods`（多对对关系表），查询走
    #: `note_goods` join `shop_products`（`api/notes.py::_serialize_note` 的
    #: `goods` 字段）—— 不在笔记表冗余一份会漂的商品快照。
    #:
    #: ⛔ **本列已删**（2026-09-25，设计 13 §34，迁移 `b2c3d4e5f6g7`）：话题改落
    #: `topics` 表 + `note_topics` 关系表（正文里的 `#话题` 同步时拆出），序列化的
    #: `topics` 字段改走关系表 join —— 本列留着就是同一语义的第二份实现
    #: （DEF-044/047/058）。原注释与列定义保留在此备查，**不在模型里映射**。
    #: **详情是否已补齐**（正文 + 完整图集，2026-09-22 新增，迁移 `o7p8q9r0s1t2`）。
    #:
    #: 三个源的**列表接口都不带正文**，图片也可能只有一张封面（千帆 `goods-note/list`
    #: 只给 `note_image`）；只有详情接口能同时给出**正文与完整图集**（真机实测：
    #: 同一条笔记详情返回 481 字正文 + 4 张图）。补齐因此是**有界 + 跨轮累积**的：
    #: 每轮同步在限流额度内补若干条，补过就写这里 ⇒ **已补齐的永不重复请求**
    #: （既省额度，也避免「详情永远取不到的行」被无限重试，设计 13 §19.13）。
    #:
    #: `NULL` = 还没补过（迁移把存量行全部置 NULL ⇒ 它们会在后续同步里逐步补齐）；
    #: 非空 = 已补过（不论当时拿到多少字段 —— 拿到多少写多少，不造假）。
    hydrated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: **平台列表里已经没有这条笔记**的对账标记时间（设计 13 §24，2026-09-22）。
    #:
    #: `NULL` = 正常（平台有它，或还没对账过）；非空 = 最近一次对账发现
    #: 「本地有、平台无」的时间。**不删行**（用户口径：本地多出的数据不直接删除，
    #: 打上状态）—— 平台列表可能因筛选 / 分页不完整，删了不可逆。
    #:
    #: 标记是**自愈**的：同一轮对账里平台又返回了这条 ⇒ 立即清回 `NULL`；
    #: 同步链路重新落库同一条时也清（见 `crawl._save_normalized_notes`）。
    platform_missing_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # ------------------------------------------------------------------ #
    # 以下四列：发布状态机（设计 13 §31，2026-09-23；口径被 §32.3 取代为
    # **全笔记通用**）。`publish_status` 是发布状态的**唯一字段**，
    # `published`（上方）是它的布尔投影 —— 写入统一经
    # `set_note_publish_status()`，别处不许直接赋值。
    # ------------------------------------------------------------------ #
    #: 发布状态：`pending` / `publishing` / `published` / `failed`（常量见本模块）。
    #: **所有笔记都有值**（迁移 `v5w6x7y8z9a0` 把存量 `""` 按 `published` 回填；
    #: 新行走 ORM 默认值 `pending`）。列定义里的 `server_default=""` 是历史形态，
    #: 刻意不重建表去改它（SQLite 改默认值要重建表，为零收益冒外键风险）。
    publish_status: Mapped[str] = mapped_column(
        String(16), default=PUBLISH_STATUS_PENDING, nullable=False, server_default="", index=True
    )
    #: 最近一次发布失败原因（平台原文 / 网络异常，DEF-058：不许替换成通用文案）；
    #: 发布成功或重新发起发布即清空。卡片 Tooltip 与详情弹窗展示。
    publish_error: Mapped[str] = mapped_column(Text, default="", nullable=False, server_default="")
    #: **软删除标记**（2026-10-12，无流量清理）。默认 `False` = 正常。
    #:
    #: 无流量清理删笔记时**不再硬删**（原 `purge_note` 真删行），改置本列 `True`：
    #: * 所有**查询**（列表 / 详情 / 同步 / 采集命中）都排除 `is_deleted=True` 的行 ——
    #:   软删的数据不再出现在任何前端页面；
    #: * 同步数据中心笔记指标时**前置校验**：`is_deleted=True` 的 note_id 直接跳过，
    #:   不再落库（否则删掉的笔记会被下一轮同步「复活」）。
    #:
    #: ⚠️ 硬删 `purge_note` 仍保留（笔记基础信息删除等其它链路用），无流量清理这条
    #: 链路**不再走它**。
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="0", index=True)
    #: 发布尝试次数（含失败；重发计数，便于排查「发了几次才成」）。
    publish_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    #: 三方发布成功时间（平台返回真实 note_id 的时刻，本地时区）；只写不清。
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)


class Topic(Base):
    """**话题字典**（设计 13 §34，2026-09-25；**原 `tags` 标签表 + 同步拆出的平台话题**）。

    用户 ② 口径「将当前系统的笔记标签表合并成话题表」⇒ `tags` / `note_tags` 两表
    整表并入本表与 `NoteTopic`（迁移 `b2c3d4e5f6g7`）。标签（用户给笔记打的标记）与
    话题（笔记正文里拆出来的 `#标签`）在**界面与写入路径上完全同构**（都是 M2M、
    都可增删、详情卡都渲染 chips）—— 合成一张表，消掉「一个语义两张表」。

    **按用户归属**（不是全局字典）：与 `tags` 原形态一致，也守住账号维度隔离
    （DEF-066 同款纪律）；`(user_id, name)` 唯一 ⇒ 同一用户下标签与话题同名时
    **天然合并成一行**（迁移按此归并，关系行进 `NoteTopic`）。

    `platform_id`：**平台话题 ID**。商品笔记 AI 生成结果自带 `topics[{id, name}]`，
    发布链路的 `hash_tag` 要平台 ID 才认（`note_publish_service._build_publish_body`）——
    生成时存下、发布时直接取。同步链路拆出的话题没有平台 ID（`_tag_names` 只取
    名字）⇒ 留空，**不造假**。
    """

    __tablename__ = "topics"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_topics_user_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    #: 话题 / 标签名 —— 存**不带 `#`** 的纯名字（展示层自己拼 `#`）。
    name: Mapped[str] = mapped_column(String(64), default="")
    #: chips 配色（自 `tags` 表沿用；新话题默认蓝，与话题区 UI 一致）。
    color: Mapped[str] = mapped_column(String(24), default="#1677ff", server_default="#1677ff")
    #: 平台话题 ID（商品笔记 AI 生成时存下；同步拆出的话题没有 ⇒ 空串）。
    platform_id: Mapped[str] = mapped_column(String(64), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)


class NoteTopic(Base):
    """**笔记 ↔ 话题** 多对多关系（设计 13 §34，2026-09-25）。

    与 `note_goods`（§26）同款分工：`notes` 装笔记信息、`topics` 装话题字典、
    本表只装**关系** —— 一篇笔记可挂多个话题、一个话题可属于多篇笔记（M2M），
    由唯一键 `(note_id, topic_id)` 强制（重复落库幂等，不产生重复行）。

    写入语义分两种入口（§34.5）：

    * **同步 / 采集 / 详情补齐 / 商品生成** ⇒ `note_topic_service.apply_sync_topics`：
      **只增不删**（find-or-create 话题 + 补关系；平台撤了话题也不删关系行）；
    * **`PUT /notes/{id}/topics`（手动增删）** ⇒ `replace_note_topics`：
      **整列替换**（§29.3 原契约：调用方传增删后的完整列表，后端不猜）。

    删除链（§34.7）：FK 不级联（`PRAGMA foreign_keys` 未开）⇒ 删笔记
    （`purge_note`）与删话题词典项（`DELETE /topics/{id}`）都必须**显式**删关系行，
    漏一边就是孤儿数据。
    """

    __tablename__ = "note_topics"
    __table_args__ = (
        UniqueConstraint("note_id", "topic_id", name="uq_note_topics_note_topic"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    #: 本地笔记行（`notes.id`）—— **本地主键，不是平台 note_id**。
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id"), index=True)
    #: 话题字典行（`topics.id`）。
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)


class NoteAsset(Base):
    __tablename__ = "note_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id"), index=True)
    asset_type: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(Text)
    local_path: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class NoteComment(Base):
    __tablename__ = "note_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id"), index=True)
    comment_id: Mapped[str] = mapped_column(String(128), index=True)
    user_name: Mapped[str] = mapped_column(String(128), default="")
    user_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    content: Mapped[str] = mapped_column(Text, default="")
    like_count: Mapped[int] = mapped_column(Integer, default=0)
    parent_comment_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at_remote: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    raw_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    #: 账号归属（ADR-021 / 迁移 `b3c4d5e6f7a8`）：此前本表隔离是**隐含的**（只有
    #: `note_id` 这一条路，离开 `notes` 就说不清是谁的数据），现按
    #: `note_id → notes.platform_account_id` 回填成显式列。**刻意不加外键**：
    #: 同 `notes` 的口径 —— 账号删了，历史评论仍要能回答「当时属于哪个账号」。
    platform_account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


# ⛔ `Tag` / `note_tags`（原「用户标签」表 + 连接表）已于 2026-09-25 **整表移除**
# （设计 13 §34，迁移 `b2c3d4e5f6g7`）：用户口径「将当前系统的笔记标签表合并成
# 话题表」⇒ 并入 `Topic` / `NoteTopic`（见上方两个类）。原表形态存档：
# `tags` = `id` / `user_id` / `name`(String 64) / `color`(String 24, 默认 `#111111`)；
# `note_tags` = `note_id` + `tag_id` 复合主键。数据搬迁规则见 §34.4。
