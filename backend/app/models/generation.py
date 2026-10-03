from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
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

#: `media_source` 四选一（设计 17 §3.5 / §7.4）。
MEDIA_SOURCE_AI_GENERATE = "ai_generate"
MEDIA_SOURCE_MATERIAL = "material"
MEDIA_SOURCE_PRODUCT_IMAGE = "product_image"
MEDIA_SOURCE_REFERENCE_IMAGE = "reference_image"
#: 视频来源（§13.4.4 v2.14）：与图片**分开取值**（媒体来源决定调哪条客户端链路）。
MEDIA_SOURCE_VIDEO_AI = "video_ai_generate"
MEDIA_SOURCE_VIDEO_REFERENCE = "video_reference"
#: 图片来源（可复用商品原图 / 素材库）。
IMAGE_MEDIA_SOURCES = (
    MEDIA_SOURCE_AI_GENERATE,
    MEDIA_SOURCE_MATERIAL,
    MEDIA_SOURCE_PRODUCT_IMAGE,
    MEDIA_SOURCE_REFERENCE_IMAGE,
)
#: 视频来源（只有 AI 生成 / 参考图两种 —— 商品原图与素材库对视频无意义）。
VIDEO_MEDIA_SOURCES = (
    MEDIA_SOURCE_VIDEO_AI,
    MEDIA_SOURCE_VIDEO_REFERENCE,
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
