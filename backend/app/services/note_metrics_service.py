"""笔记指标的口径（设计 17 §13.5，M2 方向 3，2026-10-03）。

## 为什么单独成模块

指标口径（「哪个 raw_json 键是点赞」）此前写在 `api/platforms/xhs/analytics.py` 里
（`METRIC_KEYS` / `_note_metrics`）。若继续让服务层 `from ...api...` 反向依赖接口层，
就形成**分层倒置**（服务层不该知道 HTTP 层长什么样）。

⇒ 本模块是**指标口径的唯一实现**：接口层从这里取，谁也不许再写第二份
（DEF-044「同一语义不留两份实现」）。

## 口径（缺值一律 0，不造假）

平台在 `notes.raw_json` 里给的键名**不止一种写法**（各端接口不同），故每个指标
给一组**候选键**，按序取第一个能解析出整数的。全部落空 ⇒ `0`。

⚠️ **`views`（阅读/曝光）**：没有阅读量就算不出互动率/点击率 —— 那是笔记监控最
基本的分母，故与其余四项同口径采集。

⚠️ **候选键不是「多写几个保险」**：每一个都来自真机实测见过的形态
（`likes` vs `liked_count` vs `like_count` 分别在 feed / creator / ark 三处出现）。
凭空加候选键 = 把「解析不到」伪装成「解析到了别的字段」。

## 2026-10-13 用户口径：指标快照/趋势子系统整块移除

用户口径：「把商品洞察和内容洞察中的指标相关内容逻辑移除，包括店铺商品指标快照
采集、笔记指标快照采集定时任务移除，后续作为专项单独排期」。

⇒ 本模块**只保留指标口径读取**（`note_metrics` / `METRIC_KEYS` / `ENGAGEMENT_KEYS`
/ `_as_int` / `_raw_value`）—— 它仍被榜单/排名的互动量计算（`analytics.py`）使用。
**快照写入与趋势聚合已移除**（原 `stat_date_of` / `snapshot_note` / `collect_snapshots`
/ `trend_series` + `note_metric_snapshots` 表写入）。表结构与 ORM 模型**保留**
（历史数据不删，便于后续专项复用）。
"""

from __future__ import annotations

from typing import Any

from backend.app.models.note import Note

#: 指标 → `raw_json` 里的候选键（**顺序即优先级**，见模块 docstring）。
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


__all__ = [
    "ENGAGEMENT_KEYS",
    "METRIC_KEYS",
    "note_metrics",
]
