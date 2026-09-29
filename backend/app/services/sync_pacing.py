# encoding: utf-8
"""同步节流：页间停顿的**拉长 + 随机抖动**。

背景（2026-09-25 用户口径）
==========================
「后端同步时注意不要触发平台的限流，可以拉长间隔时间，随机化间隔时间」。

平台风控识别的是**节奏**：固定的「每页精确 3 秒」比「每页 4–8 秒、次次不同」
更像机器人 —— 间隔本身不长，但等距得不像人。所以两件事一起做：

1. **拉长**：调用方的基础值上调（笔记同步 3s → 5s，商品同步同值）；
2. **随机抖动**：实际停顿 = 基础值 × `uniform(0.8, 1.6)`，且**只拉长不缩短下限**
   （系数下限 0.8 = 最坏情况也只比基础值快 20%，不会退化成密集请求）。

为什么不放在 `CrawlRateLimiter` 里
==================================
限流器管的是**额度**（每账号每源 N 次/分钟，硬顶，等待式；N 读运行时配置，
默认 60，见 `services/settings_service.py`），它必须精确才可判定；
这里管的是**节奏观感**，是额度之外的第二道柔化。两者职责不同：
额度用完仍然由 `wait_allow()` 阻塞等待，不因为抖动而放大额度。

一份实现
========
笔记同步（`note_sync_service`）与商品同步（`shop_goods_sync_service`）曾各自
抄一份 `_sleep_between_pages()`（DEF-044 的形态）；这里收敛成唯一实现，两处
只保留各自的 `PAGE_GAP_SECONDS` 常量（测试 monkeypatch 的锚点，保持可用）。
"""
from __future__ import annotations

import random
import time

#: 抖动系数下限（基础值的倍数）。**不取小于 1 的下限**会让节奏反而更快，
#: 0.8 是「允许小幅加快、但绝不密集」的折中。
JITTER_LOW = 0.8
#: 抖动系数上限。1.6 = 最多比基础值慢 60%，避免一次停顿拖太久。
JITTER_HIGH = 1.6


def jittered_gap(base_seconds: float, *, cap: float) -> float:
    """基础间隔 → 抖动后的实际间隔（秒）。

    ``base_seconds <= 0`` ⇒ 返回 ``0.0``（测试把它置 0 时不真等）；
    结果不超过 ``cap``（硬顶，与调用方既有的 `MAX_PAGE_GAP_SECONDS` 同义）。
    """
    base = min(max(float(base_seconds), 0.0), float(cap))
    if base <= 0:
        return 0.0
    return min(base * random.uniform(JITTER_LOW, JITTER_HIGH), float(cap))


def sleep_between_pages(base_seconds: float, *, cap: float) -> float:
    """按抖动后的间隔停顿，返回**实际**停顿秒数（测试直接断言它）。"""
    gap = jittered_gap(base_seconds, cap=cap)
    if gap > 0:
        time.sleep(gap)
    return gap


__all__ = ["JITTER_LOW", "JITTER_HIGH", "jittered_gap", "sleep_between_pages"]
