"""小红书话题的**平台正文形态**原语（设计 61 §三 A7 下沉，2026-10-24）。

## 为什么在 adapters 层

`#名字[话题]#` 是**小红书平台的富文本形态**（实证见 `note_topic_service` 模块
docstring 的两条记录）：写它的是创作者端发布适配器（回填正文）、读它的是业务层
（解析 / 归一 / 落库）。形态定义属于平台适配器层 —— 业务层向下 import 合法，
适配器向上 import 业务层非法（依赖方向合同 C2，此前 creator 适配器函数级
import `note_topic_service.topic_token` 就是违规点，本轮清除）。

⚠️ 本模块必须保持**零业务依赖**（只有 re）—— 守卫 C2 会扫它。
`note_topic_service` re-export 这两个函数与常量以保持既有用法（实现只有一份；
解析侧常量 `_ZERO_WIDTH_RE` / `_TOPIC_LEAD` / `_TOPIC_TAIL` 属解析专用，留在服务层）。

以下常量与函数自原 `note_topic_service.py` **逐字搬移**（git 原文，非重打）。
"""
from __future__ import annotations

import re

_TOPIC_MARKER_RE = re.compile(r"\[话题\]?#?$")

_TOPIC_NOISE = "\ufeff\u200b\u200c\u200d\xa0"

_TRIM_CHARS = "，。！？、；：""''（）()【】[]{}!?.,;:%~…—·\t\r\n " + _TOPIC_NOISE

def strip_topic_marker(name: str) -> str:
    """剥掉话题的**平台形态标记**：`#名字[话题]#` → `名字`（唯一实现）。

    平台把正文里的话题存成 `#名字[话题]#`（富文本标记）。历史解析器只认 `#名字`
    ⇒ 标记被当成名字的一部分（库里 118 行 `拼车[话题`）。本函数是**唯一**的剥
    标记入口：解析（:func:`parse_topic_tokens`）、归一
    （:func:`normalize_topic_names`）、落库（:func:`link_note_topics`）全走它 ——
    各写一份必然漂移。
    """
    text = str(name or "").strip()
    for _ in range(2):  # 两轮：先剥标记，strip 掉残留标点后再确认一次
        text = text.lstrip("#").strip()
        text = _TOPIC_MARKER_RE.sub("", text)
        text = text.strip(_TRIM_CHARS).strip()
    return text


def topic_token(name: str) -> str:
    """话题的**平台正文形态**：`#名字[话题]#`（发布回填的唯一实现）。

    实证两条：① 平台同步回来的正文就是这个形态（`#拼车[话题]#`）；② 项目既有的
    创作者端发布实现 `adapters/xhs/creator_api_adapter.py` 用同一形态回填。
    发布侧写 `#名字` 平台不认（当普通文本）—— 发布的笔记会一个话题都没有。
    """
    return f"#{strip_topic_marker(name)}[话题]#"




def topic_token(name: str) -> str:
    """话题的**平台正文形态**：`#名字[话题]#`（发布回填的唯一实现）。

    实证两条：① 平台同步回来的正文就是这个形态（`#拼车[话题]#`）；② 项目既有的
    创作者端发布实现 `adapters/xhs/creator_api_adapter.py` 用同一形态回填。
    发布侧写 `#名字` 平台不认（当普通文本）—— 发布的笔记会一个话题都没有。
    """
    return f"#{strip_topic_marker(name)}[话题]#"
