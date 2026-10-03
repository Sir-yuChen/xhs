"""笔记指标的口径与采集（设计 17 §13.5，M2 方向 3，2026-10-03）。

## 为什么单独成模块

指标口径（「哪个 raw_json 键是点赞」）此前写在 `api/platforms/xhs/analytics.py` 里
（`METRIC_KEYS` / `_note_metrics`）。M2 要**按日采集落快照**，采集逻辑在服务层 ——
若继续让服务层 `from ...api...` 反向依赖接口层，就形成**分层倒置**
（服务层不该知道 HTTP 层长什么样）。

⇒ 本模块是**指标口径的唯一实现**：接口层与采集层都从这里取，谁也不许再写第二份
（DEF-044「同一语义不留两份实现」）。

## 口径（缺值一律 0，不造假）

平台在 `notes.raw_json` 里给的键名**不止一种写法**（各端接口不同），故每个指标
给一组**候选键**，按序取第一个能解析出整数的。全部落空 ⇒ `0`。

⚠️ **`views`（阅读/曝光）此前被漏掉**（2026-10-03 实测 `raw_json` 里确实有
`view_count`）：没有阅读量就算不出互动率/点击率 —— 那是笔记监控最基本的分母。
本次补上，并**与其余四项同口径**。

⚠️ **候选键不是「多写几个保险」**：每一个都来自真机实测见过的形态
（`likes` vs `liked_count` vs `like_count` 分别在 feed / creator / ark 三处出现）。
凭空加候选键 = 把「解析不到」伪装成「解析到了别的字段」。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import String, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from backend.app.core.time import shanghai_now
from backend.app.models.note import Note
from backend.app.models.monitoring import NoteMetricSnapshot

#: 指标 → `raw_json` 里的候选键（**顺序即优先级**，见模块 docstring）。
#: `views` 是 v2.24 补的（原缺口）；其余四项沿用一期口径，名字一字未改。
METRIC_KEYS: dict[str, tuple[str, ...]] = {
    "likes": ("likes", "liked_count", "like_count"),
    "collects": ("collects", "collected_count", "collect_count"),
    "comments": ("comments", "comment_count"),
    "shares": ("shares", "share_count"),
    "views": ("view_count", "views", "read_count"),
}

#: 参与「互动」求和的四项（**不含 `views`** —— 阅读是分母，不是互动）。
ENGAGEMENT_KEYS: tuple[str, ...] = ("likes", "collects", "comments", "shares")


def _as_int(value: Any) -> int:
    """宽容转整数（缺值 ⇒ 0，见模块 docstring 的缺值口径）。"""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return 0
        try:
            return int(float(text))
        except ValueError:
            return 0
    return 0


def _raw_value(raw: dict[str, Any], keys: tuple[str, ...]) -> int:
    """按候选键取第一个**存在**的值，支持**嵌套形态**。

    ⚠️ **三种数据形态都真实存在**（真机实测，不是「多写几个保险」）：
    - **平铺**：`{"likes": 3}` —— 创作者/千帆列表项；
    - **`data` 下**：`{"data": {"likes": 3}}` —— 网页版 feed 包装；
    - **`note` 下**：`{"note": {"likes": 3}}` —— 详情接口包装。

    ⚠️ 顺序：**平铺优先**，再 `data`、再 `note`。同一键在多层出现时取最外层
    （外层是接口的规范化结果，内层是原始嵌套）。
    """
    for key in keys:
        if key in raw:
            return _as_int(raw.get(key))
        data = raw.get("data")
        if isinstance(data, dict) and key in data:
            return _as_int(data.get(key))
        note = raw.get("note")
        if isinstance(note, dict) and key in note:
            return _as_int(note.get(key))
    return 0


def note_metrics(note: Note) -> dict[str, int]:
    """从 `raw_json` 解析单篇笔记的指标（**唯一实现**）。

    返回含 `engagement`（四项互动之和）；`views` 单独给（它是分母，不进互动和）。
    """
    raw = note.raw_json if isinstance(note.raw_json, dict) else {}
    metrics = {name: _raw_value(raw, keys) for name, keys in METRIC_KEYS.items()}
    metrics["engagement"] = sum(metrics.get(key, 0) for key in ENGAGEMENT_KEYS)
    return metrics


def stat_date_of(moment: Any) -> str:
    """把时间点归一为**统计日** `YYYY-MM-DD`（上海时区）。

    ⚠️ 统一从 `shanghai_now()` 的时区体系取 —— 快照的「日」必须与用户看到的「日」
    一致（用 UTC 会出现在中国时间凌晨 0~8 点采集的笔记落到前一天的错位）。
    """
    if moment is None:
        moment = shanghai_now()
    return moment.strftime("%Y-%m-%d")


def snapshot_note(note: Note, *, stat_date: str | None = None) -> NoteMetricSnapshot:
    """构造一篇笔记在 `stat_date` 的**快照对象**（不落库，由调用方 `add` + `commit`）。

    ⚠️ 调用方负责**同 (note_pk, stat_date) 的覆盖语义**（唯一键在 DB 层强制）：
    本函数只负责「这一次采集的值是多少」，不查旧行 —— 覆盖用 upsert，
    见 `collect_snapshots`。
    """
    metrics = note_metrics(note)
    return NoteMetricSnapshot(
        user_id=note.user_id,
        note_pk=note.id,
        stat_date=stat_date or stat_date_of(shanghai_now()),
        likes=metrics["likes"],
        collects=metrics["collects"],
        comments=metrics["comments"],
        shares=metrics["shares"],
        views=metrics["views"],
        engagement=metrics["engagement"],
    )


def collect_snapshots(db: Session, *, user_id: int, stat_date: str | None = None) -> int:
    """采集该用户全部笔记当日的指标快照，返回**写入行数**。

    ⚠️ **覆盖语义**（同日重复采集取最新值）：`(note_pk, stat_date)` 上有唯一键，
    这里用 `ON CONFLICT DO UPDATE` 更新 —— 而不是先删后插（那是两次写、中间有窗口）
    也不是 `INSERT OR IGNORE`（那会永远保留当天第一次的旧值，越采越不准）。

    ⚠️ **只采有 `raw_json` 的笔记**：没同步过的笔记没有任何指标来源，
    采它只会写一堆 0 —— 那会把「未同步」伪装成「零互动」（缺值口径不允许）。
    """
    day = stat_date or stat_date_of(shanghai_now())
    # ⚠️ **判据必须按「实际存储形态」写，不能用 `raw_json IS NOT NULL`**：
    # SQLAlchemy 的 JSON 类型把 Python `None` 存成**字符串 `'null'`**（`typeof=text`），
    # 不是 SQL NULL ⇒ `IS NOT NULL` **恒真**，会把「从未同步」的笔记也采进来，
    # 在仪表盘上表现为「真实的零互动」（缺值口径不允许）。
    # 实测形态：`None` → `'null'`；`{}` → `'{}'`；有数据 → `'{"likes": 1}'`。
    # 故：排除 `NULL`、`'null'`、`''`，以及**空对象** `'{}'`（空对象同样无指标可采）。
    notes = db.scalars(
        select(Note).where(
            Note.user_id == user_id,
            Note.platform == "xhs",
            Note.raw_json.is_not(None),
            func.trim(func.coalesce(func.cast(Note.raw_json, String), "")) != "",
            func.trim(func.coalesce(func.cast(Note.raw_json, String), "")) != "null",
            func.trim(func.coalesce(func.cast(Note.raw_json, String), "")) != "{}",
        )
    ).all()

    written = 0
    for note in notes:
        metrics = note_metrics(note)
        statement = sqlite_insert(NoteMetricSnapshot).values(
            user_id=note.user_id,
            note_pk=note.id,
            stat_date=day,
            likes=metrics["likes"],
            collects=metrics["collects"],
            comments=metrics["comments"],
            shares=metrics["shares"],
            views=metrics["views"],
            engagement=metrics["engagement"],
        )
        # 冲突 ⇒ 用新值覆盖（同日取最新；见函数 docstring 的覆盖语义）。
        statement = statement.on_conflict_do_update(
            index_elements=["note_pk", "stat_date"],
            set_={
                "likes": statement.excluded.likes,
                "collects": statement.excluded.collects,
                "comments": statement.excluded.comments,
                "shares": statement.excluded.shares,
                "views": statement.excluded.views,
                "engagement": statement.excluded.engagement,
            },
        )
        db.execute(statement)
        written += 1

    db.commit()
    return written


def trend_series(
    db: Session, *, user_id: int, days: int = 30, note_pk: int | None = None
) -> list[dict[str, Any]]:
    """按日聚合的趋势序列（该用户；可选只取某篇）。

    每一天返回该日**所有笔记快照的合计**（互动四项各自求和 + 阅读求和 + 互动合计）。
    ⚠️ 只返回**有快照的天**（不补零）：缺失的天用 0 填充会把「那天没采」
    画成「那天互动掉到 0」，那是**伪造数据**（缺值口径）。
    """
    statement = (
        select(
            NoteMetricSnapshot.stat_date,
            func.sum(NoteMetricSnapshot.likes).label("likes"),
            func.sum(NoteMetricSnapshot.collects).label("collects"),
            func.sum(NoteMetricSnapshot.comments).label("comments"),
            func.sum(NoteMetricSnapshot.shares).label("shares"),
            func.sum(NoteMetricSnapshot.views).label("views"),
            func.sum(NoteMetricSnapshot.engagement).label("engagement"),
        )
        .where(NoteMetricSnapshot.user_id == user_id)
        .group_by(NoteMetricSnapshot.stat_date)
        .order_by(NoteMetricSnapshot.stat_date)
    )
    if note_pk is not None:
        statement = statement.where(NoteMetricSnapshot.note_pk == note_pk)
    if days > 0:
        # 只取最近 N 天（按日期字符串比较 —— `YYYY-MM-DD` 的字典序即时间序）。
        cutoff = (shanghai_now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        statement = statement.where(NoteMetricSnapshot.stat_date >= cutoff)

    return [
        {
            "stat_date": row.stat_date,
            "likes": int(row.likes or 0),
            "collects": int(row.collects or 0),
            "comments": int(row.comments or 0),
            "shares": int(row.shares or 0),
            "views": int(row.views or 0),
            "engagement": int(row.engagement or 0),
        }
        for row in db.execute(statement).all()
    ]


__all__ = [
    "ENGAGEMENT_KEYS",
    "METRIC_KEYS",
    "collect_snapshots",
    "note_metrics",
    "snapshot_note",
    "stat_date_of",
    "trend_series",
]
