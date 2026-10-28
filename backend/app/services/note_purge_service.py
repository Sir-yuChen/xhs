"""笔记删除链（**唯一实现**，DEF-044）。

`purge_note` 删一条笔记及其全部从属行（话题关系 / 资产 / 评论 / 商品关联 /
指标快照 / 草稿引用）。为什么单独成模块：

* 它被两处调用 —— `api/notes.py`（手工删除 / 批量删除）与
  `no_traffic_cleanup_service.py`（无流量清理平台删成功后同步删本地）。
* `api/notes.py` import `pc.py`（adapter factory），而 `pc.py` 的端点调用
  `no_traffic_cleanup_service`；若后者再 import `api/notes.py` 就成循环。
* 把删除链抽到这里，两处各自 import，**同一份代码**（不是两份实现）。

调用方负责 ``db.commit()``。
"""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session

from backend.app.models import (
    AiDraft,
    Note,
    PublishAsset,
    NoteTopic,
)


def purge_note(db: Session, note: Note) -> None:
    """删一条笔记**及其全部从属行**（标签 / 资产 / 评论 / 商品关联 / 草稿引用）。

    ⚠️ **唯一的删除链实现**（§24 起对账也要删行）：手工删除与对账删除必须是
    同一份代码，否则某一边漏清关联行就会留下孤儿数据。调用方负责 commit。

    ⚠️ 2026-09-23 用户口径「删除笔记时需要删除笔记与商品的关联关系」⇒ 补上
    **`note_goods` 关系行**（§26：`note_goods` 是笔记↔商品的多对多关系表）。
    漏清它的实际后果（真实库已复现）：删掉 4 条本地生成笔记后留下 4 条孤儿关系行
    （note_id 1963/1964/1965/1967 → product_id 1）。它们**不参与任何列表口径**，
    于是「挂链笔记数」之类的关系表计数**不会**被它们污染 —— 但每次 join 都要多扫
    几行、且数据本身自相矛盾（关系指向一条不存在的笔记）。
    ⚠️ 与**商品台账删除**的口径刻意不同（§33.10）：删商品时关系行**不级联删**
    （商品被同步写回时关系要原样接上）；删笔记时关系行**必须删**（笔记是真删，
    没有「写回时接上」这回事）。同一个关系表、两个方向、两种正确处理。
    ⚠️ 2026-09-25（§34.7）：`note_tags` ⇒ **`note_topics`**（标签表已并入话题表）——
    同一条删除链规则：本地实体真删 ⇒ 关系行跟着走，两个方向（删笔记 / 删话题
    词典项）同一把尺子。

    ⚠️ **2026-09-27（§26.7）两处口径确认**（用户当轮两条要求）：

    * 「**删除笔记时需删除笔记与商品之间的关系**」⇒ 下面 `note_goods` 那一句保留，
      但**判据改成平台 ID + 归属**（`(user_id, note.note_id)`）—— 关系表已不存本地主键；
    * 「**内容库页点击删除笔记时，话题只删关系表**」⇒ `note_topics` 只删**关系行**，
      `topics` **字典行不动**（话题 / 标签是用户资产，删一篇笔记不该让别处的话题消失）。
      这与「删话题词典项」方向不同（那边删典 ⇒ 关系行一并删，见 `api/topics.py`）。
    """
    db.execute(delete(NoteTopic).where(NoteTopic.note_id == note.id))
    db.execute(delete(PublishAsset).where(PublishAsset.parent_id == note.id))
    #: 漏了删笔记必撞外键 500。只删桥行，`generation_records` 是生成历史、保留。
    # 草稿只解除引用、不连坐：AI 草稿是用户资产，笔记没了它还在（§12）。
    db.query(AiDraft).filter(AiDraft.source_note_id == note.id).update({"source_note_id": None})
    db.delete(note)
