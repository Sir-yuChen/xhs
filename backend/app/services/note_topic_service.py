"""笔记话题：正文拆分 + 话题表落库 + 关系维护（设计 13 §34，2026-09-25 用户口径）。

## 用户口径原文（两条）

> ① 「同步笔记和内容库页中的同步笔记按钮，执行同步笔记时，将笔记中的话题进行拆分出来，
> 写入到话题表中，并新增话题与笔记的关系，新增话题前先检查话题是否存在，存在则不新增
> 话题，只新增当前话题与笔记之间的关联关系，实现后笔记正文不再存在话题，话题与正文
> 进行拆分，发布笔记时注意查询该笔记下的话题进行添加」；
> ② 「将当前系统的笔记标签表合并成话题表……」

## 本模块是什么

**话题的唯一实现**：解析（正文里的 `#话题`）→ 拆分（正文摘除）→ 落库（`topics`
find-or-create + `note_topics` 关系）→ 查询（发布链路取数）。同步链路、采集链路、
商品生成、手动增删（`PUT /notes/{id}/topics`）、发布链路**全部走这里的函数**——
各写一份必然漂移（DEF-044/047/058 的教训）。

## 三张表的分工（§34.1）

| 表 | 装什么 |
|---|---|
| `notes` | 笔记信息（**不再有 `topics_json` 列** —— 删了，它是第二份真相源） |
| `topics` | 话题字典（原 `tags` 标签表并入；按用户归属，`(user_id, name)` 唯一） |
| `note_topics` | 笔记 ↔ 话题关系（M2M，`(note_id, topic_id)` 唯一） |

## 两种写入语义（§34.5，别混）

* :func:`apply_sync_topics`（同步 / 采集 / 详情补齐 / 商品生成）——
  **只增不删**：话题存在就不新建（用户口径），关系只补缺失的；平台撤了话题
  也不删关系行（§26 同款：同步链路不做删除维度）；
* :func:`replace_note_topics`（`PUT /notes/{id}/topics` 手动增删）——
  **整列替换**（§29.3 原契约）：调用方传增删后的完整列表，后端不猜；
  关系行删到与列表严格一致后按**输入顺序**重建（顺序是用户事实）。

## 解析规则（§34.3，诚实优先：拆错比漏拆更坏）

* **平台声明（detail 归一化的 `tags`）是权威**：`declared` 非空时直接信；
* **正文解析是兜底**：`#` + 非空白非 `#` 字符（≤50 字），修剪首尾常见标点，
  **长度 ≥2 且含中文或字母**才算（滤掉 `#1` / `#2026` 这类序号噪声）；
* **摘除形态**：`#名字` 带上可选的平台标记 `[话题]#`，整词匹配（后随边界不能是
  名字字符）、大小写不敏感 —— 连同一个前导分隔空格一起摘；仅当确有摘除时才对
  受影响行 rstrip 并整体 strip（**最小改动**，没拆过话题的正文一个字节都不碰）；
* **限量**：20 条 / 50 字（:func:`normalize_topic_names`，与 §29.3 同一把尺子）。

## 平台形态 `#名字[话题]#`（2026-09-25 修，**别退回 `#名字`**）

小红书正文里的话题是**富文本标记**：`#名字[话题]#`。两条实证：

1. 从平台同步回来的正文原文就是这个形态（`#拼车[话题]# #顺风车[话题]# #郑州[话题]#`）；
2. 项目既有的创作者端发布实现 `adapters/xhs/creator_api_adapter.py` 用
   `f" #{name}[话题]# "` 回填（第三方逆向可用实现，同形态）。

因此：**解析要剥掉标记**（:func:`strip_topic_marker`）、**发布回填要写回标记**
（:func:`topic_token`）。此前只认 `#名字` ⇒ 标记被当成名字的一部分，库里落下
118 行 `拼车[话题` 脏话题名、91 篇正文摘不净；发布侧回填的 `#名字` 平台不认，
**发布的笔记一个话题都没有**（用户 2026-09-25 报障）。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models.note import Note, NoteTopic, Topic

#: 话题归一化上限（设计 13 §29.3）：展示字段，超限**静默截断**不报错。
NOTE_TOPICS_MAX_ITEMS = 20
NOTE_TOPICS_MAX_LEN = 50

#: 正文里 `#话题` 的形态：`#` + 非空白非 `#` 字符（≤50 字）。
_TOKEN_RE = re.compile(r"#([^\s#]{1,50})")

#: 平台话题形态的尾部标记：`[话题]`，可能带一个收尾 `#`（`#名字[话题]#`）；
#: **未闭合的 `[话题` 也要吃** —— 库里 118 行脏话题名（`拼车[话题`）就是这么来的。
_TOPIC_MARKER_RE = re.compile(r"\[话题\]?#?$")

#: 平台排版噪声（BOM / 零宽空格 / 零宽连接符 / 不换行空格）—— 平台正文里就夹在
#: 话题标记前后（`#A[话题]#\ufeff\ufeff\xa0#B[话题]#`）。它既是**分隔**也是**边界**：
#: 不认它，摘除的「整词」断言会被挡下（91 篇正文残留 `[话题]#` 的真凶之一），
#: 摘完还会留下孤立的 `#`。
_TOPIC_NOISE = "\ufeff\u200b\u200c\u200d\xa0"

#: 零宽类噪声（**删除**；`\xa0` 另按空格处理，见 :func:`_normalize_noise`）。
_ZERO_WIDTH_RE = re.compile(r"[\ufeff\u200b\u200c\u200d]+")

#: 话题标记前面可以吃掉的排版字符（空格 + 噪声）。
_TOPIC_LEAD = r"[ \t" + re.escape(_TOPIC_NOISE) + r"]*"

#: 「整词」的后随边界：后面不能是真正的内容字符（可以空白 / `#` / 噪声）。
_TOPIC_TAIL = r"(?![^#\s" + re.escape(_TOPIC_NOISE) + r"])"

#: 解析出的 token 首尾要修剪的标点（中文/英文常见标点，不含 `#` 与空白；含噪声）。
_TRIM_CHARS = "，。！？、；：""''（）()【】[]{}!?.,;:%~…—·\t\r\n " + _TOPIC_NOISE

#: 「这像个话题」的判据：≥2 字，且含中文或英文字母（滤掉 `#1` / `#2026` 序号）。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LETTER_RE = re.compile(r"[A-Za-z]")


def _normalize_noise(text: str) -> str:
    """归一平台排版噪声：零宽字符**删除**、不换行空格 → 普通空格。

    只在**确有摘除**的正文上调用（没拆过话题的正文一字节不动，§34.3 最小改动）。
    """
    return _ZERO_WIDTH_RE.sub("", str(text or "").replace("\u00a0", " "))


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


def _looks_like_topic(name: str) -> bool:
    if len(name) < 2:
        return False
    return bool(_CJK_RE.search(name) or _LETTER_RE.search(name))


def parse_topic_tokens(content: str) -> list[str]:
    """从正文里解析 `#话题` token（保序、按小写去重、保留首次出现的形态）。

    只认「≥2 字且含中文或字母」的 token（修剪首尾标点后判定）—— `#1` / `#2026`
    这类序号不当话题（§34.3：拆错比漏拆更坏）。

    **例外**：带平台标记（`[话题]`）的 token 长度 1 也认 —— 标记是平台自己的
    认定（与「`declared` 权威」同口径），不掺我们的猜测；`#鞋[话题]#` 就是这种。
    """
    text = str(content or "")
    if not text or "#" not in text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        raw = match.group(1)
        # 平台形态 `#名字[话题]#` ⇒ 剥掉 `[话题]` 标记（唯一实现；别只 strip 标点，
        # 那会把 `]` 去掉只留 `拼车[话题` —— 库里 124 行脏数据就是这么来的）。
        marked = "[话题]" in raw
        name = strip_topic_marker(raw.strip(_TRIM_CHARS))
        if not name:
            continue
        if marked:
            # 平台标记 = 平台认定 ⇒ 长度 1 也认（`#鞋[话题]#`）；但**纯数字仍不认**
            # （`#2026[话题]#` 是文本噪声，不是话题）。
            if not (_CJK_RE.search(name) or _LETTER_RE.search(name)):
                continue
        elif not _looks_like_topic(name):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= NOTE_TOPICS_MAX_ITEMS:
            break
    return out


def normalize_topic_names(raw: Iterable[Any]) -> list[str]:
    """话题名归一化（**唯一实现**，§29.3 / §34.3）：trim → 去空 → 去重 → 限量。

    去重键 = 去空格后的**小写**（`#露营` 与 `#露营 ` 是同一个话题）；保留**首次
    出现的大小写形态**（用户敲的原样）。空列表合法（= 清空话题）。
    """
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        # 平台形态 / 用户粘贴的 `#名字[话题]#` 也归一成纯名字（唯一入口）——手动
        # 增删（PUT 整列替换）与平台声明都从这里过，脏名字进不了库。
        text = strip_topic_marker(item)
        if not text:
            continue
        if len(text) > NOTE_TOPICS_MAX_LEN:
            text = text[:NOTE_TOPICS_MAX_LEN]
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= NOTE_TOPICS_MAX_ITEMS:
            break
    return out


def split_topics_from_content(content: str, names: Iterable[str]) -> tuple[str, bool]:
    """把 `#名字` 从正文里摘掉，返回 `(新正文, 是否有摘除)`。

    * 整词匹配（后随边界不能是「名字字符」），大小写不敏感；
    * 连同一个前导分隔空格（`[ \\t]*`）一起摘 —— 不吃换行，不并行；
    * **仅当确有摘除**才对受影响行做 rstrip 并整体 strip：没拆过话题的正文
      原样返回（最小改动，§34.3）；
    * 同名按长度降序摘（`露营装备` 先于 `露营`），防部分重叠。
    """
    text = str(content or "")
    distinct: dict[str, str] = {}
    for name in names:
        value = str(name or "").strip()
        if value:
            distinct.setdefault(value.lower(), value)
    if not text or not distinct:
        return text, False
    removed = False
    for _, value in sorted(distinct.items(), key=lambda kv: len(kv[1]), reverse=True):
        # 平台形态 `#名字[话题]#` 整块摘掉（标记 + 收尾 `#` 一起）；没有标记时退化成
        # `#名字`（老形态，仍要能摘）。收尾 `#` 只在吃了标记之后才吃 —— 否则
        # `#露营#周末` 这种连写会被摘成 `周末`（丢一个 `#`）。
        pattern = re.compile(
            _TOPIC_LEAD + "#" + re.escape(value) + r"(?:\[话题\]#?)?" + _TOPIC_TAIL,
            re.IGNORECASE,
        )
        text, count = pattern.subn("", text)
        if count:
            removed = True
    if not removed:
        return text, False
    lines = [_normalize_noise(line).rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip(), True


def _topic_names_of(rows: Iterable[Topic]) -> list[str]:
    return [str(row.name or "") for row in rows if str(row.name or "")]


def note_topic_names(db: Session, note_pk: int) -> list[str]:
    """该笔记当前的话题名列表（关系行 id 排序 = 落库顺序）。发布链路与序列化共用。"""
    rows = db.scalars(
        select(Topic)
        .join(NoteTopic, NoteTopic.topic_id == Topic.id)
        .where(NoteTopic.note_id == note_pk)
        .order_by(NoteTopic.id.asc())
    ).all()
    return _topic_names_of(rows)


def note_topic_rows(db: Session, note_pk: int) -> list[Topic]:
    """该笔记当前的话题行（含 `platform_id` / `color`；关系行 id 排序）。"""
    return list(
        db.scalars(
            select(Topic)
            .join(NoteTopic, NoteTopic.topic_id == Topic.id)
            .where(NoteTopic.note_id == note_pk)
            .order_by(NoteTopic.id.asc())
        ).all()
    )


def _find_or_create_topics(
    db: Session,
    user_id: int,
    names: list[str],
    *,
    platform_ids: Optional[dict[str, str]] = None,
    default_color: str = "#1677ff",
) -> tuple[dict[str, Topic], int]:
    """按 `(user_id, name)` 找或建话题行。返回 `(小写名 → Topic 行, 新建行数)`。

    **存在就不新建**（用户口径原文：「新增话题前先检查话题是否存在，存在则不新增
    话题，只新增当前话题与笔记之间的关联关系」）。`platform_ids` 已知则补填
    （不覆盖已有非空值 —— 同一次落库里多个来源不互相冲）。
    """
    wanted = list(dict.fromkeys(names))
    if not wanted:
        return {}, 0
    existing: dict[str, Topic] = {}
    for start in range(0, len(wanted), 50):
        chunk = wanted[start : start + 50]
        rows = db.scalars(select(Topic).where(Topic.user_id == user_id, Topic.name.in_(chunk))).all()
        for row in rows:
            existing[str(row.name).lower()] = row
    by_name: dict[str, Topic] = {}
    created = 0
    for name in wanted:
        key = name.lower()
        row = existing.get(key)
        if row is None:
            platform_id = (platform_ids or {}).get(name, "")
            row = Topic(
                user_id=user_id,
                name=name,
                color=default_color,
                platform_id=platform_id,
            )
            db.add(row)
            db.flush()
            created += 1
        else:
            platform_id = (platform_ids or {}).get(name, "")
            if platform_id and not str(row.platform_id or ""):
                row.platform_id = platform_id
        by_name[key] = row
    return by_name, created


def link_note_topics(
    db: Session,
    note: Note,
    names: Iterable[str],
    *,
    platform_ids: Optional[dict[str, str]] = None,
) -> dict[str, int]:
    """把话题名 upsert 进 `topics` + 补 `note_topics` 关系（**只增不删**）。

    返回 `{"topics_created": 新建话题行数, "relations_added": 新建关系行数}`。
    话题已存在 ⇒ 不新建话题行；关系已存在 ⇒ 不新建关系行（唯一键兜底幂等）。
    """
    # 归一兜底：脏名字（`拼车[话题`）不许进库 —— 与 normalize 同一实现。
    wanted = list(dict.fromkeys(name for name in (strip_topic_marker(n) for n in names) if name))
    if not wanted:
        return {"topics_created": 0, "relations_added": 0}
    by_name, created = _find_or_create_topics(
        db, int(note.user_id or 0), wanted, platform_ids=platform_ids
    )
    existing_relations = set(
        db.scalars(
            select(NoteTopic.topic_id).where(NoteTopic.note_id == note.id)
        ).all()
    )
    added = 0
    for name in wanted:
        row = by_name.get(name.lower())
        if row is None or row.id in existing_relations:
            continue
        db.add(NoteTopic(note_id=note.id, topic_id=row.id))
        existing_relations.add(row.id)
        added += 1
    # flush（不是 commit）：让同事务内后续查询（序列化 / 发布取数）立刻看得见关系行。
    # 落库仍由调用方 commit（同步链路 / 端点各自的事务边界）。
    db.flush()
    return {"topics_created": created, "relations_added": added}


def apply_sync_topics(
    db: Session,
    note: Note,
    declared: Iterable[Any] = (),
    *,
    platform_ids: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """同步链路的**唯一话题入口**：拆正文 → 落话题表 → 补关系（§34.2）。

    * `declared` = 平台声明的话题名（详情归一化的 `tags`；列表 / ark 源没有 ⇒ 传空）；
    * 话题集合 = `declared` ∪ 正文解析（declared 在前，保平台原序）；
    * 正文里的 `#话题` 被摘掉（**实现后笔记正文不再存在话题**）；
    * 话题 / 关系 **只增不删**（平台撤了也不删；手动编辑走 replace）。

    返回 `{"topics": 最终话题名, "content_stripped": 正文是否被拆,
    "topics_created": n, "relations_added": n}`。调用方负责 commit。
    """
    declared_names = normalize_topic_names(declared)
    seen = {name.lower() for name in declared_names}
    parsed = [name for name in parse_topic_tokens(note.content) if name.lower() not in seen]
    names = declared_names + parsed
    if not names:
        return {"topics": [], "content_stripped": False, "topics_created": 0, "relations_added": 0}
    new_content, stripped = split_topics_from_content(str(note.content or ""), names)
    if stripped:
        note.content = new_content
    outcome = link_note_topics(db, note, names, platform_ids=platform_ids)
    return {
        "topics": names,
        "content_stripped": stripped,
        "topics_created": outcome["topics_created"],
        "relations_added": outcome["relations_added"],
    }


def replace_note_topics(db: Session, note: Note, names: Iterable[Any]) -> list[str]:
    """手动增删的**唯一落库入口**（`PUT /notes/{id}/topics`，§29.3 / §34.4）。

    **整列替换**：`names` 是增删后的**完整列表**，后端不猜「加了哪个删了哪个」
    （猜 = 造第二份真相源）。话题行 find-or-create（不重复建典）；关系行删到与
    列表严格一致后按**输入顺序**重建 —— 顺序是用户事实（§29.3 既有契约）。

    返回归一化后的最终话题名列表。调用方负责 commit。
    """
    final = normalize_topic_names(names)
    wanted_keys = {name.lower() for name in final}
    # 1) 删掉不在新列表里的关系（整列替换的「删」半边）
    current = db.scalars(select(NoteTopic).where(NoteTopic.note_id == note.id)).all()
    keep: dict[str, NoteTopic] = {}
    for relation in current:
        row = db.get(Topic, relation.topic_id)
        key = str(row.name).lower() if row is not None and row.name else ""
        if key in wanted_keys:
            keep[key] = relation
        else:
            db.delete(relation)
    db.flush()
    # 2) find-or-create 话题行 + 按输入顺序补缺失关系（「增」半边）
    by_name, _ = _find_or_create_topics(db, int(note.user_id or 0), final)
    for name in final:
        key = name.lower()
        if key in keep:
            continue
        row = by_name.get(key)
        if row is None:
            continue
        db.add(NoteTopic(note_id=note.id, topic_id=row.id))
    db.flush()
    return final
