"""LLM 生成笔记的**版本历史**（每次生成一条，可基于某版本局部重生成）。

设计来源：用户口径 2026-10-17 ——

> 「千帆生成的笔记不存，LLM 生成笔记记录，且还有一个问题，比如基于指定生成的某一版本，
>   用户只想重新生成正文或只重新配置生成图片」

## 为什么需要这一层

`generation_records` 原先只表达「一次生成本身」，于是：

1. 同商品生成 3 次 ⇒ 只有最后写进 `notes` 的那版可见，**前两版丢了**（无法对比、无法选回）；
2. 「基于第 2 版只重写正文」这个动作**无处表达**（既不是新生成，也不是修改当前版本）。

## 两条不变量（本模块的存在意义）

| 不变量 | 为什么 |
|---|---|
| **版本行永不覆盖** | 用户要对比「改文案前后」；覆盖掉了历史就没了 |
| **同商品最多一条 `is_adopted`** | 「采用版」是用户的选择，不是时间序（可能选回第 1 版）；DB 层有部分唯一索引兜底 |

## 局部重生成怎么落地

* **只重写文案**（`text`）：调模型生成新的标题/正文/话题，**媒体沿用当前版本**的 note_assets；
* **只重配图片**（`media`）：沿用当前版本的标题/正文/话题，**只跑媒体链路**；
* 两种都**落成新版本行**，`parent_record_id` 指向当前版本。

⚠️ 为什么不像「编辑」那样原地改当前版本：原地改 = 丢历史，且 `notes` 行被发布过后
再改会让「已发布的笔记内容」与本地记录不一致（平台侧无法回滚）。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.models.generation import (
    GEN_STATUS_SUCCESS,
    REGEN_LEGACY_SCOPES,
    REGEN_SCOPE_ALL,
    REGEN_SCOPES,
    GenerationRecord,
)


class VersionError(ValueError):
    """版本相关错误（API 层映射 400/404）。"""

    def __init__(self, message: str, code: str = "LLM_VERSION_INVALID") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def next_version_no(db: Session, *, user_id: int, shop_product_id: int) -> int:
    """同商品的下一个版本序号（现有最大 + 1；无历史 ⇒ 1）。

    ⚠️ **按 `max+1` 而不是 `count+1`**：删掉中间某一版后，`count+1` 会与已有序号撞车
    （出现两个「第 3 版」），而 `max+1` 永远单调。
    """
    current = db.scalar(
        select(func.max(GenerationRecord.version_no)).where(
            GenerationRecord.user_id == int(user_id),
            GenerationRecord.shop_product_id == int(shop_product_id),
        )
    )
    return int(current or 0) + 1


def find_record(
    db: Session, *, user_id: int, record_id: int
) -> GenerationRecord:
    """取一条**属于该用户**的生成记录（归属校验，不跨租户）。"""
    record = db.get(GenerationRecord, int(record_id))
    if record is None or int(record.user_id) != int(user_id):
        raise VersionError(
            f"生成记录不存在（id={record_id}）或不属于当前用户。", code="LLM_RECORD_NOT_FOUND"
        )
    return record


def normalize_scope(scope: Any) -> str:
    """`regen_scope` 入参规整（未知取值 ⇒ 400，不静默按 `all` 处理）。

    ⚠️ 静默退 `all` 的后果：用户点「只重写正文」，实际整篇（含图片）都重生成了
    —— 既费钱又把他满意的图换掉了，而他以为只是改了文案。
    """
    value = str(scope or REGEN_SCOPE_ALL).strip().lower()
    #: **旧的 `text` 仍接受**（生成记录 Tab 的「重生文案」走它）—— 语义就是
    #: 「三块一起重写、媒体沿用当前版本」。细粒度的 `title`/`body`/`topics` 是新入口。
    #: ⚠️ 不接受 = 老前端/老调用直接 400，那是把内部枚举变更暴露成用户故障。
    if value in REGEN_LEGACY_SCOPES:
        return value
    if value not in REGEN_SCOPES:
        raise VersionError(
            f"重生成范围只能是 {'/'.join(REGEN_SCOPES)}（实得 {value!r}）",
            code="LLM_REGEN_SCOPE_INVALID",
        )
    return value


def validate_target_version(
    db: Session, *, user: Any, target_id: Optional[int], scope: str
) -> Optional[GenerationRecord]:
    """局部重生成必须给一个**成功的**当前版本。

    判据（用户口径「基于指定生成的某一版本」）：

    * `scope == all` ⇒ 不需要当前版本（`target_id` 有值也忽略，返回 None）；
    * `scope in (text, media)` ⇒ **必须**给当前版本，且该当前版本必须
      - 存在且属于当前用户；
      - `status == success`（失败版本没有可沿用的内容）；
      - **同商品**（跨商品沿用既无意义也会串数据）。
    """
    if scope == REGEN_SCOPE_ALL:
        return None
    if not target_id:
        raise VersionError(
            "局部重生成必须指定当前版本（`target_record_id`）。",
            code="LLM_TARGET_REQUIRED",
        )
    target = find_record(db, user_id=int(user.id), record_id=int(target_id))
    if str(target.status) != GEN_STATUS_SUCCESS:
        raise VersionError(
            f"当前版本（id={target.id}）没有成功产出，无法沿用它重生成。",
            code="LLM_TARGET_NOT_SUCCESS",
        )
    return target


def mark_adopted(db: Session, *, record: GenerationRecord) -> None:
    """把某版标为**当前采用版**（同商品其余版本置 0）。

    ⚠️ 必须先清再设（**同一事务内**），否则会撞部分唯一索引
    `uq_generation_records_adopted`（同商品只能有一条 `is_adopted=1`）。
    """
    siblings = db.scalars(
        select(GenerationRecord).where(
            GenerationRecord.user_id == int(record.user_id),
            GenerationRecord.shop_product_id == record.shop_product_id,
            GenerationRecord.is_adopted.is_(True),
        )
    ).all()
    for sibling in siblings:
        if int(sibling.id) != int(record.id):
            sibling.is_adopted = False
    #: ⚠️ **必须先 flush 清空再设 TRUE**（2026-10-19 实测 500：
    #: `UNIQUE constraint failed: generation_records.user_id, generation_records.shop_product_id`）。
    #: 原因：SQLAlchemy 在同一次 flush 里不保证 UPDATE 顺序，把「新采用版置 1」
    #: 排在「旧采用版置 0」之前 ⇒ 瞬时出现两条 `is_adopted=1`
    #: ⇒ 撞部分唯一索引 `uq_generation_records_adopted`。
    #: 分两次 flush 就把这个瞬时窗口消掉了（同一事务内，不会留下中间态）。
    if db.new or db.dirty:
        db.flush()
    record.is_adopted = True


def serialize_copy_requirements(payload: Any) -> str:
    """文案要求 → JSON 文本（快照落库）。空 ⇒ `"{}"`（不写 `"null"`）。"""
    if not isinstance(payload, dict) or not payload:
        return "{}"
    return json.dumps(payload, ensure_ascii=False)


def list_versions(
    db: Session, *, user_id: int, shop_product_id: int, limit: int = 50
) -> list[GenerationRecord]:
    """同商品的版本历史（**新→旧**）。

    ⚠️ 只返回该用户该商品的记录（跨租户/跨商品不串）。
    ⚠️ 失败版本**也在列表里**：用户要看到「第 3 次生成了但失败了」，
    否则会以为点了没反应。
    """
    return list(
        db.scalars(
            select(GenerationRecord)
            .where(
                GenerationRecord.user_id == int(user_id),
                GenerationRecord.shop_product_id == int(shop_product_id),
            )
            .order_by(GenerationRecord.version_no.desc(), GenerationRecord.id.desc())
            .limit(int(limit))
        ).all()
    )


def version_summary(record: GenerationRecord) -> dict[str, Any]:
    """一条版本记录 → 前端展示形态（**不含正文全文**，列表用）。

    ⚠️ 正文只给摘要（前 60 字）：一个商品几十版、每版 300 字，
    全量下发会让列表接口变成几百 KB。详情用单独接口取。
    """
    body = str(record.body or "")
    try:
        topics = json.loads(record.topics_json or "[]")
    except ValueError:
        topics = []
    #: **这一版用了哪些媒体**（用户口径 2026-10-18：*「生成记录列表新增封面列，
    #: 笔记图/视频数」*）—— 列表要能一眼看出「这版有没有图、几张、是不是视频」。
    #: ⚠️ 只给**封面 + 计数**，不给完整清单：几十版 × 每版 N 张会把列表接口撑大
    #: （完整清单在详情接口里）。
    try:
        media = json.loads(record.media_json or "[]")
    except ValueError:
        media = []
    media = [m for m in media if isinstance(m, dict)]
    cover_url = ""
    image_count = 0
    video_count = 0
    for entry in media:
        kind = str(entry.get("asset_type") or "image")
        if kind == "video":
            video_count += 1
        else:
            image_count += 1
        if not cover_url and str(entry.get("url") or ""):
            #: 封面 = **第一条有 url 的媒体**（排序即图序，第一张就是封面）
            cover_url = str(entry.get("url") or "")
    return {
        "id": int(record.id),
        "version_no": int(record.version_no or 1),
        "parent_record_id": (
            int(record.parent_record_id) if record.parent_record_id else None
        ),
        "regen_scope": str(record.regen_scope or REGEN_SCOPE_ALL),
        "is_adopted": bool(record.is_adopted),
        "status": str(record.status),
        "title": str(record.title or ""),
        "body_excerpt": body[:60],
        "body_length": len(body),
        "topics": topics if isinstance(topics, list) else [],
        "media_source": str(record.media_source or ""),
        #: **形态**（`image` / `video`）—— 用户口径 2026-10-19：*「生成记录新增一个
        #: 类型字段区分，视频笔记或图文笔记」*。列表上直接标出来，回显也按它分页签。
        "note_form": str(getattr(record, "note_form", "") or "image"),
        "model_name": str(record.model_name or ""),
        "error_code": record.error_code,
        "error_message": record.error_message,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        #: 列表展示用（封面列 + 图/视频数）—— 完整清单仍走详情接口
        "cover_url": cover_url,
        "image_count": image_count,
        "video_count": video_count,
    }
