from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

#: `generation_records.status` 状态机（设计 17 §3.5）：`queued → running → success | failed`。
GEN_STATUS_QUEUED = "queued"
GEN_STATUS_RUNNING = "running"
GEN_STATUS_SUCCESS = "success"
GEN_STATUS_FAILED = "failed"
GEN_TERMINAL_STATUSES = (GEN_STATUS_SUCCESS, GEN_STATUS_FAILED)

#: 这一版**重做了什么**（用户口径 2026-10-17：*「基于指定生成的某一版本，用户只想重新
#: 生成正文或只重新配置生成图片」*）。
#:
#: * `all`   —— 整篇重新生成（标题/正文/话题/图片全换）；`parent_record_id = NULL`；
#: * `text`  —— **只重写文案**（标题/正文/话题），媒体沿用当前版本已有的；
#: * `media` —— **只重配图片**，文案（标题/正文/话题）沿用当前版本已有的。
#:
#: ⚠️ 局部重生成**必须落成新版本行**（不覆盖当前版本）：用户可能想对比「改文案前后」，
#: 覆盖掉了就没得比。当前版本行保持不动 = 历史可回溯。
REGEN_SCOPE_ALL = "all"
REGEN_SCOPE_TEXT = "text"
REGEN_SCOPE_MEDIA = "media"
#: **逐块重生成**（用户口径 2026-10-18：*「后端应支持每个文案区域单独重新生成，
#: 或图片区域单独重新生成」*）—— 上面三个是**旧粒度**（`text` 把标题/正文/话题
#: 一起重写，界面上写「只改一块」是假的），下面四个是**新粒度**：
#:
#: * `title`  —— 只重写标题（正文 / 话题 / 配图全部沿用当前版本，一个字不动）；
#: * `body`   —— 只重写正文；
#: * `topics` —— 只重写话题；
#: * `media`  —— 只重配图片（复用上面的同名字面量，语义一致）。
#:
#: ⚠️ `text` / `all` **保留**：历史版本行的 `regen_scope` 列里有旧值，
#: 删掉字面量会让老数据变成「未知 scope」。新请求只走四个细粒度值。
REGEN_SCOPE_TITLE = "title"
REGEN_SCOPE_BODY = "body"
REGEN_SCOPE_TOPICS = "topics"
#: 新的细粒度集合（**新请求只接受这一组 + `all`**）。
REGEN_SCOPES = (
    REGEN_SCOPE_ALL,
    REGEN_SCOPE_TITLE,
    REGEN_SCOPE_BODY,
    REGEN_SCOPE_TOPICS,
    REGEN_SCOPE_MEDIA,
)
#: 历史字面量（只读，用于展示老数据，**不再接受为新请求的入参**）。
REGEN_LEGACY_SCOPES = (REGEN_SCOPE_TEXT,)
#: 文案类细粒度（这三个都要走文本模型，且必须沿用当前版本里的其余两块）。
REGEN_TEXT_FIELD_SCOPES = (
    REGEN_SCOPE_TITLE,
    REGEN_SCOPE_BODY,
    REGEN_SCOPE_TOPICS,
)

#: `media_source` 四选一（设计 17 §3.5 / §7.4）。
MEDIA_SOURCE_AI_GENERATE = "ai_generate"
MEDIA_SOURCE_MATERIAL = "material"
MEDIA_SOURCE_PRODUCT_IMAGE = "product_image"
MEDIA_SOURCE_REFERENCE_IMAGE = "reference_image"
#: 视频来源（设计 17 §13.4.4）。
#: ⚠️ **2026-10-16 收敛为两种**（用户口径：*「视频下拉应只有本地素材和智能生成视频」*）：
#:   - `video_material` —— **本地素材直接用**（**不生成视频**，只借素材喂文案链路）；
#:   - `video_ai_generate` —— 文/图生视频（可带参考图）。
#: ⚠️ `video_reference`（独立「参考图生成视频」）**已退场**：它与 `video_ai_generate`
#: 带参考图是同一件事（两入口一实现 ⇒ 概念冗余，与本仓「参考图移进智能生成」同一次收敛）。
#: 旧值仍在白名单里**只为兼容存量队列行**（新请求不再产生它）。
MEDIA_SOURCE_VIDEO_MATERIAL = "video_material"
MEDIA_SOURCE_VIDEO_AI = "video_ai_generate"
MEDIA_SOURCE_VIDEO_REFERENCE = "video_reference"
#: 图片来源（可复用商品原图 / 素材库）。
IMAGE_MEDIA_SOURCES = (
    MEDIA_SOURCE_AI_GENERATE,
    MEDIA_SOURCE_MATERIAL,
    MEDIA_SOURCE_PRODUCT_IMAGE,
    MEDIA_SOURCE_REFERENCE_IMAGE,
)
#: 视频来源（本地素材直接用 / AI 生成）。
#: ⚠️ `MEDIA_SOURCE_VIDEO_REFERENCE` **不在白名单**（已退场，见上方注释）——
#: 但它仍在 `VIDEO_MEDIA_SOURCES` 之外 ⇒ 新请求传它会被 `LLM_MEDIA_SOURCE_INVALID` 拒。
#: 存量队列行不受影响（那些行已经落库，不再过白名单）。
VIDEO_MEDIA_SOURCES = (
    MEDIA_SOURCE_VIDEO_MATERIAL,
    MEDIA_SOURCE_VIDEO_AI,
)
MEDIA_SOURCES = IMAGE_MEDIA_SOURCES + VIDEO_MEDIA_SOURCES

#: `aigc_media_assets.asset_type` 两值（设计 17 §3.6）。
ASSET_TYPE_IMAGE = "image"
ASSET_TYPE_VIDEO = "video"


class GenerationRecord(Base):
    """LLM 生成审计记录（设计 17 §3.5，T5）。

    语义：**一次 LLM 生成 = 一条审计记录**（谁在何时用哪个供应商/模板生成什么），
    支持失败重试与溯源。

    为什么建：一期生成失败原因只在队列快照给一次（§31.4「原因只在快照给一次」），
    LLM 生成依赖外部供应商，失败/限流/超时频次高，必须有持久记录支撑「重试/排查/统计」。

    **落库纪律**：
    - 标题/正文 ✅ 落（生成产物，用户可编辑后发布）；
    - **完整 Prompt ❌ 不落**（体积大且含商品细节，按 `template_id`+`module_ids` 可重建）；
    - **供应商密钥 ❌ 不落**（响应原文不落，`error_message` 只记摘要）；
    - 同名失败原因**只在快照 `finished` 给一次** —— 本表落 `error_message` 摘要，
      快照展示从本表取（不再只靠轮询窗口，这是本表存在的核心理由）。
    """

    __tablename__ = "generation_records"
    #: ⚠️ 2026-10-23 补：这两个索引此前**只写在迁移 `c1d2e3f4a5b7` 里，模型层没声明**。
    #: 后果：用 `create_all` 建出的库（测试库 / PG 基线）会**静默丢掉它们** ——
    #: `uq_generation_records_adopted` 是**唯一约束**（同商品只能有一条采用版），
    #: 丢了 = 「采用版」可以出现两条，业务判据失效且不报错。
    __table_args__ = (
        Index(
            "uq_generation_records_adopted",
            "user_id",
            "shop_product_id",
            unique=True,
            sqlite_where=text("is_adopted = 1"),
            postgresql_where=text("is_adopted = true"),
        ),
        Index(
            "ix_generation_records_product_version",
            "user_id",
            "shop_product_id",
            "version_no",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 归属（DEF-066）。
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    #: 对应队列项（一个生成任务一条）。重试 = 新队列项 + 新记录行。
    task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id"), index=True, nullable=True
    )
    #: 源商品（本地台账行）。
    shop_product_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("shop_products.id"), index=True, nullable=True
    )
    #: 所用模板（`null` = 未用模板）。
    template_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("prompt_templates.id"), nullable=True
    )
    #: 所用上游连接配置。
    gateway_config_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("llm_gateway_configs.id"), index=True, nullable=True
    )
    #: 实际使用的模型名（快照，改配置不丢审计）。
    model_name: Mapped[str] = mapped_column(String(128), default="")
    #: `ai_generate`/`material`/`product_image`/`reference_image`。
    media_source: Mapped[str] = mapped_column(String(32), default=MEDIA_SOURCE_PRODUCT_IMAGE)
    #: **笔记形态**：`image`（图文）/ `video`（视频）。
    #: 用户口径 2026-10-19：*「生成记录新增一个类型字段区分，视频笔记或图文笔记」* ——
    #: 原来只能靠 `media_json` 里有没有 `asset_type=video` 去猜，
    #: 一旦媒体生成失败（一条都没落）就无从判断，回显还会错页签。
    #: 落库时由 `plan_form(plan)` 派生（与 `notes.note_form` 同口径）。
    note_form: Mapped[str] = mapped_column(String(16), default="image", index=True)
    #: `queued`/`running`/`success`/`failed`（状态机见模块常量）。
    status: Mapped[str] = mapped_column(String(32), index=True, default=GEN_STATUS_QUEUED)
    #: 生成的标题（护栏通过后落）。
    title: Mapped[str] = mapped_column(Text, default="")
    #: 生成的正文（护栏通过后落）。
    body: Mapped[str] = mapped_column(Text, default="")
    #: 生成的话题名列表（归一化后），JSON 文本。
    topics_json: Mapped[str] = mapped_column(Text, default="[]")
    #: 失败原因（**供应商原文不落**，摘要记）。
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: 供应商响应时间。
    ai_generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # ------------------------------------------------------------------ #
    # 版本历史（用户口径 2026-10-17：*「基于指定生成的某一版本，用户只想重新生成
    # 正文或只重新配置生成图片」*）
    #
    # 一张表同时是「审计」与「版本历史」：每次生成落一条，**永不覆盖旧记录**
    # （原先只有「一次生成本身」，同商品生成 3 次后看不到前两版，用户无法对比/选回）。
    # ------------------------------------------------------------------ #
    #: 同商品内的版本序号（1,2,3…）。⚠️ **不是主键**，只是展示用的次序。
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    #: 当前版本：全量生成 ⇒ `None`；「基于第 2 版只重生成正文/图片」⇒ 指向第 2 版那条。
    #: 自引用外键（当前版本被删 ⇒ 置空，不连带删掉这条历史）。
    parent_record_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("generation_records.id", ondelete="SET NULL"), nullable=True
    )
    #: 这一版重做了什么：`all`（整篇新生成）/ `text`（只重写文案，图沿用当前版本）/
    #: `media`（只重配图片，文案沿用当前版本）。见 `REGEN_SCOPES`。
    regen_scope: Mapped[str] = mapped_column(String(16), default=REGEN_SCOPE_ALL)
    #: 是否是**当前采用版**（同商品最多一条为真，DB 层有部分唯一索引兜底）。
    #: ⚠️ 不是「最新一条」—— 用户可能生成 3 版后选回第 1 版。
    is_adopted: Mapped[bool] = mapped_column(Boolean, default=False)
    #: 当时的**文案要求快照**（区间/表情/三块提示词），JSON 文本。
    #: ⚠️ 必须存快照而不是只存模板 id：模板被改过之后，只存 id 还原不出当时的要求。
    copy_requirements_json: Mapped[str] = mapped_column(Text, default="{}")
    #: 这一版**实际用了哪些媒体**（JSON 数组，元素 `{url, local_path, sort_order}`）。
    #:
    #: ⚠️ 为什么必须存（2026-10-18 真机实证）：
    #: `product_image` 来源是**直接引用台账图直链**，不走 `AigcMediaAsset`
    #: （那是 AI 生成图的溯源表）—— 于是「图确实生成了、笔记也带图了」，
    #: 但生成记录里**一个字都没提图** ⇒ 版本详情抽屉无从展示，
    #: 用户以为「配图没生成」。这里把清单存进记录本身，与媒体来源无关。
    media_json: Mapped[str] = mapped_column(Text, default="[]")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=shanghai_now, onupdate=shanghai_now
    )


class AigcMediaAsset(Base):
    """AIGC 媒体资产溯源（设计 17 §3.6，T5）。

    **为什么建独立表而不塞 `note_assets`**：`note_assets` 是「笔记媒体清单」
    （发布要读它），溯源信息（prompt/model/aigcTaskId）不该污染笔记媒体行；
    发布时按需 join 本表取 `aigc_task_id`（平台合规要求 `aigcTaskId`）。

    ⚠️ 不含**平台发布**相关字段（发布状态在 `publish_jobs`），本表只登记「AI 生成」事实。
    """

    __tablename__ = "aigc_media_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    #: 关联生成记录。
    generation_record_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("generation_records.id"), index=True, nullable=True
    )
    #: `image` / `video`。
    asset_type: Mapped[str] = mapped_column(String(16))
    #: 本地落盘路径（`storage/media/` 或素材库副本）。
    local_path: Mapped[str] = mapped_column(Text)
    #: 供应商回传 URL（若有）。
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: 平台 AIGC 溯源号（发布时写入 `feature.aigcTaskId`）。
    aigc_task_id: Mapped[str] = mapped_column(Text, default="")
    #: 本次生成所用的图片/视频 Prompt（**溯源用，必留**）。
    prompt: Mapped[str] = mapped_column(Text, default="")
    #: 用哪个模型生成。
    model_name: Mapped[str] = mapped_column(String(128), default="")
    #: v2.30（§13.3.11）：**商品关联** —— 这张图是为哪个商品生成的。
    #:
    #: ⚠️ 用**业务 ID**（`shop_products.item_id`）而不是台账主键：台账重同步会换
    #: 主键，业务 ID 不会。可空 = 与商品无关的生成（如笔记配图）——**不造假**。
    #:
    #: ⚠️ **历史行不回填**（迁移只加列）：旧图只有任务 payload 里可能有 item_id，
    #: 但「payload 里有」≠「这张图属于这个商品」（一次任务可能出多张不同组的图）
    #: ⇒ 旧的保持空，如实反映「不知道」。
    item_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    #: v2.30：**平台账号** —— 多账号下同一个 `item_id` 是可能的（不同店铺）
    #: ⇒ 只按 item_id 关联会串号，必须带上账号维度。
    platform_account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)


class NoteGenerationLink(Base):
    """生成笔记 ↔ 商品关系（设计 17 §3.7，T5）。

    语义：**LLM 生成链路**的「笔记 ↔ 商品」多对多。一期 `note_goods` 是**平台 ID 级**
    （`note_id`/`item_id`），而 LLM 生成落库时笔记是本地 `local_<uuid>`（无平台 ID）、
    商品可能还没同步进台账 —— 需要一张**本地主键级**关系表。

    **与一期 `note_goods` 的关系（不是两份实现）**：一期关系表是「**平台证据缓存**」
    （千帆说挂什么就落什么，平台 ID），本表是「**本地生成编排**」（从台账挑商品做生成，
    本地主键）。发布成功后两者**都保留**。

    ⚠️ 前端展示挂链商品**仍读 `note_goods`**（契约不变）；本表只服务「生成链路追查」。
    """

    __tablename__ = "note_generation_links"
    __table_args__ = (
        UniqueConstraint(
            "note_pk", "shop_product_id", name="uq_note_generation_links_note_product"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    #: 本地笔记主键。
    note_pk: Mapped[int] = mapped_column(ForeignKey("notes.id"), index=True)
    #: 本地台账商品主键。
    shop_product_id: Mapped[int] = mapped_column(
        ForeignKey("shop_products.id"), index=True
    )
    #: **是哪次生成产出这篇笔记**（版本历史 ↔ 笔记的唯一桥）。
    #: ⚠️ 2026-10-17 新增：此前本表只有「笔记 ↔ 商品」，**没有指向生成记录** ⇒
    #: 「基于第 2 版只重写文案」时需要从当前版本记录反查它产出的笔记（沿用其媒体），
    #: 而这条路径原本是断的（实测本表 0 行，无法溯源）。
    generation_record_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("generation_records.id", ondelete="SET NULL"), index=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
