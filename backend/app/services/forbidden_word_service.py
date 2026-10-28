"""违规词库的**纯逻辑 + 持久化**（设计 25 §2）。

## 职责边界

* **纯逻辑**（无 DB、无平台，可单测）：`normalize` / `find_hits` / `check_text`；
* **持久化**（有 DB，无平台）：CRUD + 停用/启用。

## ⚠️ 匹配为什么要「归一化」

OCR 出来的文字与用户填的词之间有这些**不可见差异**（实测都会发生）：

| 差异 | 例 |
|---|---|
| 全角/半角 | 用户填 `ＡＢＣ`，OCR 出 `ABC` |
| 中英文空格 | 用户填 `best `（带尾空格），OCR 出 `best` |
| 大小写 | 用户填 `Best`，OCR 出 `best` |
| 零宽字符 | 复制粘贴带进 `best\u200b` |

不归一化 ⇒ 用户明明填了词、界面却显示「未检出违规词」，而他确信图里印着
—— 这是**最坏的一类静默错**（用户会以为系统没检查）。
⇒ 归一化**只用于比较**；**存 / 显示一律用用户原文**（`ForbiddenWord.word`）。
"""

from __future__ import annotations

import re
import unicodedata
import logging
from typing import Any, Iterable

from sqlalchemy import delete, func, select

from backend.app.core.database import SessionLocal
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.time import shanghai_now
from backend.app.models.forbidden_word import (
    DEFAULT_GROUP,
    ForbiddenWord,

    MATCH_MODE_EXACT,
    MATCH_MODES,
    MAX_GROUP_LEN,
    MAX_WORDS_PER_GROUP,
)

logger = logging.getLogger(__name__)

#: 词的输入长度上限（与列宽一致；超长在路由层报 400）。
MAX_WORD_LEN = 120
#: 备注上限。
MAX_REMARK_LEN = 255

#: 归一化时**删掉**的字符（OCR 与手工输入都会混进来）。
#: ⚠️ 只删「不承载语义」的：空白、零宽、软连字符。BOM/ZWSP 都在这里。
_ZERO_WIDTH = re.compile(r"[\s\u200b\u200c\u200d\ufeff\u00ad\u2060]")


class ForbiddenWordError(ValueError):
    """违规词操作被拒（带结构化 `code`，由路由映射成 HTTP）。"""

    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# 纯逻辑
# --------------------------------------------------------------------------- #


def normalize(text: object) -> str:
    """归一化到「可比较」形态：NFKC + 去空白/零宽 + 转小写。

    * **NFKC** 解决全角→半角（`Ａ` → `A`）、兼容字符（`①` → `1`）；
    * 去零宽/空白 —— OCR 的字间空格是排版产物，不是词的一部分；
    * 转小写 —— 英文不区分大小写。

    ⚠️ 中文不受影响（NFKC 对汉字是恒等的），所以不会误伤中文词。
    """
    value = unicodedata.normalize("NFKC", str(text or ""))
    return _ZERO_WIDTH.sub("", value).strip().lower()


def validate_word(raw: object) -> str:
    """校验并返回**用户原文**词（去首尾空白）。

    ⚠️ **不做归一化后存储** —— 存归一化值会让用户在列表里看到
    `best` 而不是他填的 `Best`，像系统改了他的数据。
    """
    value = str(raw or "").strip()
    if not value:
        raise ForbiddenWordError("FORBIDDEN_WORD_EMPTY", "违规词不能为空")
    if len(value) > MAX_WORD_LEN:
        raise ForbiddenWordError(
            "FORBIDDEN_WORD_TOO_LONG", f"违规词最多 {MAX_WORD_LEN} 个字"
        )
    if normalize(value) == "":
        # 整串都是零宽/空白 —— 归一化后为空 = 无法匹配 = 等同于没填。
        raise ForbiddenWordError(
            "FORBIDDEN_WORD_EMPTY", "违规词不能只包含空格或不可见字符"
        )
    return value


def validate_match_mode(raw: object) -> int:
    """匹配模式校验。**坏值报错，不静默退默认**（DEF-044 一致实现）。"""
    if raw is None:
        #: 默认**精确**（用户口径 2026-10-08）。
        return MATCH_MODE_EXACT
    try:
        mode = int(raw)
    except (TypeError, ValueError):
        raise ForbiddenWordError(
            "FORBIDDEN_WORD_BAD_MATCH_MODE", "匹配模式只能是 1（精确）或 2（包含）"
        ) from None
    if mode not in MATCH_MODES:
        raise ForbiddenWordError(
            "FORBIDDEN_WORD_BAD_MATCH_MODE", "匹配模式只能是 1（精确）或 2（包含）"
        )
    return mode


def validate_group(raw: object) -> str:
    """分组名校验：去空白、限长，**且必须非空**。

    2026-10-28 用户口径：「必须先添加分组，不允许未分组数据」⇒ 违规词必须归属
    某个分组，空串不再是合法状态。
    """
    text = str(raw or "").strip()
    if not text:
        raise ForbiddenWordError(
            "FORBIDDEN_WORD_GROUP_REQUIRED",
            "请先选择分组（不支持未分组的违规词）",
        )
    if len(text) > MAX_GROUP_LEN:
        raise ForbiddenWordError(
            "FORBIDDEN_WORD_BAD_GROUP",
            f"分组名最长 {MAX_GROUP_LEN} 个字",
        )
    return text

def find_hits(text: str, entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """在一段文字里找出**命中的违规词**（纯函数，可单测）。

    `entries` 是「已按 user 过滤出 enabled」的词条，形如
    `[{"id":1,"word":"最佳","match_mode":2}, ...]`。

    返回 `[{"id","word","match_mode","matched_text"}]`，**按词条顺序**返回
    （与用户词库顺序一致 ⇒ 前端展示稳定，不随字典序抖动）。

    ⚠️ **空文本返回空列表**（不报错）：OCR 可能对纯色图返回空，
    那是「没检出」而不是「检查失败」—— 两者在前端要显示不同的东西。
    """
    haystack = normalize(text)
    if not haystack:
        return []
    hits: list[dict[str, Any]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        word = normalize(entry.get("word"))
        if not word:
            continue
        #: 缺值时按**精确**兜底（与 create/validate 的默认一致）。
        mode = int(entry.get("match_mode") or MATCH_MODE_EXACT)
        matched = haystack == word if mode == 1 else (word in haystack)
        if matched:
            hits.append(
                {
                    "id": entry.get("id"),
                    "word": str(entry.get("word") or ""),
                    "match_mode": mode,
                    "matched_text": word,
                    #: 分组透给前端 —— 用户看到「最佳」被拦会想知道是哪组在管它。
                    "group_name": str(entry.get("group_name") or DEFAULT_GROUP),
                }
            )
    return hits


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #


def list_words(
    db: Session,
    user_id: int,
    *,
    enabled_only: bool = False,
    groups: Iterable[str] | None = None,
) -> list[ForbiddenWord]:
    """列出该用户的词条（默认按 id 升序 = 创建顺序）。

    `groups` 非空 ⇒ **只取这些分组**（预检时用户可指定分组，见
    `enabled_entries`）。传 `None` 或空 ⇒ 不过滤（= 全部分组）。
    """
    #: 只取**词行**（`parent_id` 非空）；分组行（`parent_id IS NULL`）不是词。
    stmt = select(ForbiddenWord).where(
        ForbiddenWord.user_id == int(user_id),
        ForbiddenWord.parent_id.is_not(None),
    )
    if enabled_only:
        stmt = stmt.where(ForbiddenWord.enabled.is_(True))
    wanted = [str(g) for g in (groups or []) if str(g) != ""]
    if wanted:
        stmt = stmt.where(ForbiddenWord.group_name.in_(wanted))
    return list(db.execute(stmt.order_by(ForbiddenWord.id.asc())).scalars().all())


def list_groups(db: Session, user_id: int) -> list[dict[str, Any]]:
    """列出该用户**已有的分组**（含未分组）与其词条数。

    ⚠️ 词条数为 0 的分组**不返回** —— 词都删光了还留个空分组在预检下拉里，
    用户会以为选它有用（选了等于不检测）。

    「未分组」用空串表示，前端负责显示成「未分组」而不是空标签。
    """
    counts: dict[str, int] = {}
    for row in list_words(db, user_id):
        key = row.group_name or DEFAULT_GROUP
        counts[key] = counts.get(key, 0) + 1

    #: 显式的组开关（有组行的按行；没有组行的默认启用）。
    flags = {
        name: bool(enabled)
        for name, enabled in db.execute(
            select(ForbiddenWord.name, ForbiddenWord.enabled).where(
                ForbiddenWord.user_id == int(user_id),
                ForbiddenWord.parent_id.is_(None),
            )
        ).all()
    }
    #: ⚠️ 组行可能指向一个**已经没有词**的组（词删光了但开关还在）——
    #: 那种组也列出来（否则用户没法把它重新打开）。
    names = set(counts) | set(flags)
    return [
        {
            "name": name,
            "count": counts.get(name, 0),
            "enabled": flags.get(name, True),
        }
        for name in sorted(names, key=lambda n: (n != DEFAULT_GROUP, n))
    ]


def enabled_groups(db: Session, user_id: int) -> set[str]:
    """取**启用中**的分组名集合。

    ⚠️ 没有组行 = **默认启用**（用户建了词但没碰过组开关，就该生效）。
    只有明确存在且 `enabled=False` 的组才算停用 —— 这样「组停用」是
    一个**显式动作**，不会被存量数据误判。
    """
    rows = db.execute(
        select(ForbiddenWord.name, ForbiddenWord.enabled).where(
            ForbiddenWord.user_id == int(user_id),
            ForbiddenWord.parent_id.is_(None),
        )
    ).all()
    disabled = {name for name, enabled in rows if not enabled}
    seen = {name for name, _ in rows}
    #: 出现在词里但**没有组行**的分组也算启用（未被显式停用）。
    all_names = {row.group_name or DEFAULT_GROUP for row in list_words(db, user_id)}
    return (all_names | seen) - disabled


def enabled_entries(
    db: Session,
    user_id: int,
    *,
    groups: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """取**启用中**的词条（供 OCR 匹配用）。

    ## 两道开关是「与」关系（⚠️ 这是 OCR 侧最容易漏的一处）

    一条词要参与匹配，必须**同时**满足：

    1. 词自己的 `enabled` 为真（逐条停用）；
    2. 它所在的**分组**没被停用（用户口径 2026-10-08：「组新增启用停用功能」
       —— 组停用 ⇒ 组内所有词都不参与匹配，不必逐条关）。

    漏掉第 2 条的话，「停用某个组」在界面上看着生效、实际 OCR 照样命中，
    是个静默失效的假开关。

    `groups` 非空 ⇒ 再按分组收窄（用户口径：「点击预检支持选择指定违规词
    分组，支持多组，**默认全部分组**」）。空 / `None` = 全部。
    """
    alive = enabled_groups(db, user_id)
    return [
        {
            "id": row.id,
            "word": row.word,
            "match_mode": row.match_mode,
            #: 带上分组是为了命中结果里能说清「踩的是哪一组的词」
            #: （用户看到「最佳」被拦，会想知道是哪个分组在管它）。
            "group_name": row.group_name or DEFAULT_GROUP,
        }
        for row in list_words(db, user_id, enabled_only=True, groups=groups)
        #: ⚠️ 组被停用 ⇒ 整组退出匹配（OCR 侧必须真的照做，见函数 docstring）。
        if (row.group_name or DEFAULT_GROUP) in alive
    ]


def set_group_enabled(db: Session, user_id: int, name: str, enabled: bool) -> dict[str, Any]:
    """**整组启用 / 停用**（用户口径 2026-10-08：「组新增启用停用功能」）。

    组行不存在就**建出来**（用户可能是第一次给这个组设开关）。这样
    「组」的开关状态永远是显式的，而不是靠「有没有词」推断。
    """
    group = validate_group(name)
    row = db.execute(
        select(ForbiddenWord).where(
            ForbiddenWord.user_id == int(user_id),
            ForbiddenWord.name == group,
            ForbiddenWord.parent_id.is_(None),
        )
    ).scalar_one_or_none()
    if row is None:
        row = ForbiddenWord(user_id=int(user_id), parent_id=None, name=group, enabled=bool(enabled))
        db.add(row)
    else:
        row.enabled = bool(enabled)
        row.updated_at = shanghai_now()
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise ForbiddenWordError(
            "FORBIDDEN_WORD_GROUP_DUPLICATE", "分组状态写入冲突，请重试", status_code=409
        ) from error
    db.refresh(row)
    return {"name": row.name, "enabled": bool(row.enabled), "count": _group_word_count(db, user_id, group)}


def _group_word_count(db: Session, user_id: int, name: str) -> int:
    """该组的词条数（分组清单与启停回执都要用）。"""
    #: 同 `_assert_group_capacity`：计数要用 `scalar()`（见那里的说明）。
    return int(
        db.execute(
            select(func.count(ForbiddenWord.id)).where(
                ForbiddenWord.user_id == int(user_id),
                ForbiddenWord.name == (name or DEFAULT_GROUP),
                ForbiddenWord.parent_id.is_not(None),
            )
        ).scalar()
        or 0
    )


def get_word(db: Session, user_id: int, word_id: int) -> ForbiddenWord:
    """取一条；**不存在或不属于该用户都 404**（不泄露「存在但不是你的」）。"""
    row = db.get(ForbiddenWord, int(word_id))
    #: 分组行（`parent_id IS NULL`）不是词 ⇒ 同样按「不存在」处理。
    if row is None or int(row.user_id) != int(user_id) or row.parent_id is None:
        raise ForbiddenWordError(
            "FORBIDDEN_WORD_NOT_FOUND", "违规词不存在", status_code=404
        )
    return row


def _assert_not_duplicate(
    db: Session,
    user_id: int,
    word: str,
    *,
    group_name: str = DEFAULT_GROUP,
    exclude_id: int | None = None,
) -> None:
    """「不可重复」的应用层前置检查（**友好错误码**）。

    ⚠️ **范围是「同一分组内」** —— 同一个词进不同分组是**正常需求**
    （「最佳」在「极限词」和「广告词」里各有一条），不该被判重复。

    ⚠️ 这**不是**保证 —— 并发写仍可能双双通过。真正的保证在数据库唯一索引
    （`ux_forbidden_words_user_group_word`），`create_word` / `update_word`
    会把 `IntegrityError` 翻译成 409。
    """
    group = group_name or DEFAULT_GROUP
    needle = normalize(word)
    where = f"（分组：{group}）" if group else "（未分组）"
    stmt = select(ForbiddenWord.id).where(
        ForbiddenWord.user_id == int(user_id),
        ForbiddenWord.word == word,
        ForbiddenWord.name == group,
        ForbiddenWord.parent_id.is_not(None),
    )
    if exclude_id is not None:
        stmt = stmt.where(ForbiddenWord.id != int(exclude_id))
    if db.execute(stmt).first() is not None:
        raise ForbiddenWordError(
            "FORBIDDEN_WORD_DUPLICATE", f"违规词「{word}」已存在{where}", status_code=409
        )
    #: 归一化后相等也算重复（「Best」与「best」是同一个词）。
    for row in list_words(db, user_id):
        if exclude_id is not None and row.id == int(exclude_id):
            continue
        if (row.group_name or DEFAULT_GROUP) != group:
            continue
        if normalize(row.word) == needle:
            raise ForbiddenWordError(
                "FORBIDDEN_WORD_DUPLICATE", f"违规词「{word}」已存在{where}", status_code=409
            )


def _assert_group_capacity(db: Session, user_id: int, group_name: str) -> None:
    """⚠️ **每组最多 500 条**（用户口径 2026-10-08）。

    超限 ⇒ 400 而不是静默插入：分组是给预检挑选用的，一个组塞几千条
    会让「按分组收窄」失去意义（选一组 = 选全部）。

    ⚠️ 应用层计数**不是**并发保证（两个请求可能同时看到 499）。真正的硬上限
    要靠数据库约束或串行化；这里保证的是**常规路径**不会越滚越大 —— 与
    `_assert_not_duplicate` 同一定位（友好报错在前，兜底在库）。
    """
    total = db.execute(
        select(func.count(ForbiddenWord.id)).where(
            ForbiddenWord.user_id == int(user_id),
            ForbiddenWord.name == (group_name or DEFAULT_GROUP),
            ForbiddenWord.parent_id.is_not(None),
        )
    ).scalar()
    #: ⚠️ 用 `scalar()` 不是 `scalar_one()` —— `count()` 在**多行**时
    #: `scalar_one()` 会抛 MultipleResultsFound，被上层当 500 吞掉，
    #: 表现为「上限完全不生效」（实测第 501 条照样 201 插进去了）。
    if int(total or 0) >= MAX_WORDS_PER_GROUP:
        where = f"分组「{group_name}」" if group_name else "未分组"
        raise ForbiddenWordError(
            "FORBIDDEN_WORD_GROUP_FULL",
            f"{where}已达上限 {MAX_WORDS_PER_GROUP} 条，请分批或另建分组",
        )


def find_group_row(db: Session, user_id: int, name: str) -> Optional[ForbiddenWord]:
    """取该用户的**分组行**（`parent_id IS NULL`）；不存在返回 None。"""
    return db.execute(
        select(ForbiddenWord).where(
            ForbiddenWord.user_id == int(user_id),
            ForbiddenWord.name == validate_group(name),
            ForbiddenWord.parent_id.is_(None),
        )
    ).scalar_one_or_none()


def require_group_row(db: Session, user_id: int, name: str) -> ForbiddenWord:
    """取分组行；**不存在就报错**（不自动建组 —— 用户口径「必须先添加分组」）。"""
    row = find_group_row(db, user_id, name)
    if row is None:
        raise ForbiddenWordError(
            "FORBIDDEN_WORD_GROUP_NOT_FOUND",
            f"分组「{validate_group(name)}」不存在，请先添加分组",
            status_code=404,
        )
    return row


def create_group(db: Session, user_id: int, *, name: str, enabled: bool = True) -> dict[str, Any]:
    """**新建分组**（不允许无分组数据 ⇒ 建词前必须先有分组）。重名 ⇒ 409。"""
    group = validate_group(name)
    if find_group_row(db, user_id, group) is not None:
        raise ForbiddenWordError(
            "FORBIDDEN_WORD_GROUP_DUPLICATE", f"分组「{group}」已存在", status_code=409
        )
    row = ForbiddenWord(user_id=int(user_id), parent_id=None, name=group, enabled=bool(enabled))
    db.add(row)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise ForbiddenWordError(
            "FORBIDDEN_WORD_GROUP_DUPLICATE", f"分组「{group}」已存在", status_code=409
        ) from error
    db.refresh(row)
    return {"name": row.name, "enabled": bool(row.enabled), "count": 0}

def create_word(
    db: Session,
    user_id: int,
    *,
    word: str,
    match_mode: object = None,
    remark: str = "",
    enabled: bool = True,
    group_name: object = DEFAULT_GROUP,
) -> dict[str, Any]:
    """新增一条违规词。**同分组内**重复 ⇒ 409（应用层查 + 数据库唯一兜底）。

    分组容量上限见 `MAX_WORDS_PER_GROUP`（超限 ⇒ 400）。
    """
    value = validate_word(word)
    mode = validate_match_mode(match_mode)
    group = validate_group(group_name)
    note = str(remark or "").strip()[:MAX_REMARK_LEN]
    _assert_group_capacity(db, user_id, group)
    _assert_not_duplicate(db, user_id, value, group_name=group)

    parent = require_group_row(db, user_id, group)
    row = ForbiddenWord(
        user_id=int(user_id),
        parent_id=parent.id,
        #: 组名冗余在子行上（按组过滤不必 join 父表）。
        name=group,
        word=value,
        match_mode=mode,
        enabled=bool(enabled),
        remark=note,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as error:
        #: ⚠️ 并发写入撞唯一索引 —— 翻译成 409 而不是 500。
        db.rollback()
        raise ForbiddenWordError(
            "FORBIDDEN_WORD_DUPLICATE", f"违规词「{value}」已存在", status_code=409
        ) from error
    db.refresh(row)
    return serialize(row)


def update_word(
    db: Session,
    user_id: int,
    word_id: int,
    *,
    word: str | None = None,
    match_mode: object = None,
    remark: str | None = None,
    enabled: bool | None = None,
    group_name: object = None,
) -> dict[str, Any]:
    """修改一条（可改词 / 分组 / 匹配模式 / 备注 / 停用启用）。不存在 ⇒ 404。"""
    row = get_word(db, user_id, word_id)
    #: ⚠️ 改分组也要过重复检查，且**按新分组**判 —— 改完撞上目标分组里的同名词
    #: 就是真重复（`_assert_not_duplicate` 内已按 `group_name` 收口）。
    if group_name is not None:
        target = validate_group(group_name)
        #: 改分组 = 改「挂在哪个组行下」+ 同步冗余的组名；目标组不存在就建出来。
        row.parent_id = require_group_row(db, user_id, target).id
        row.name = target
    if word is not None:
        value = validate_word(word)
        _assert_not_duplicate(
            db,
            user_id,
            value,
            group_name=row.group_name or DEFAULT_GROUP,
            exclude_id=row.id,
        )
        row.word = value
    if match_mode is not None:
        row.match_mode = validate_match_mode(match_mode)
    if remark is not None:
        row.remark = str(remark or "").strip()[:MAX_REMARK_LEN]
    if enabled is not None:
        row.enabled = bool(enabled)
    row.updated_at = shanghai_now()
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise ForbiddenWordError(
            "FORBIDDEN_WORD_DUPLICATE", "违规词已存在", status_code=409
        ) from error
    db.refresh(row)
    return serialize(row)


def set_enabled(db: Session, user_id: int, word_id: int, enabled: bool) -> dict[str, Any]:
    """**停用 / 启用**（独立入口 —— 前端「停用」「启用」是两个按钮）。"""
    return update_word(db, user_id, word_id, enabled=bool(enabled))


def delete_word(db: Session, user_id: int, word_id: int) -> None:
    """删除一条（**不可恢复**）。不存在或非本人 ⇒ 404。"""
    row = get_word(db, user_id, word_id)
    db.delete(row)
    db.commit()


def serialize(row: ForbiddenWord) -> dict[str, Any]:
    """ORM → 前端契约（**唯一实现**）。"""
    return {
        "id": row.id,
        "word": row.word,
        "match_mode": row.match_mode,
        "enabled": bool(row.enabled),
        "remark": row.remark or "",
        "group_name": row.group_name or DEFAULT_GROUP,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# --------------------------------------------------------------------------- #
# 内置种子（2026-10-21 用户口径「把当前违规词作为新的种子数据」）
# --------------------------------------------------------------------------- #


def seed_builtin_words(db: Session, *, user_id: int) -> int:
    """为某用户播种内置违规词（返回**新建条数**）。

    ## 为什么是「按分组」判重，而不是「按用户」

    违规词是**用户资产**：他可能只用「小红书」组，也自己建了别的组。
    判定粒度取**每个种子分组**：该组一条都没有才播，有任何一条就整组跳过。
    这样「用户删光了某组」会被重新补上（他承认这组该有），
    而「用户删掉了其中几个词」不会复活（`已有词 ⇒ 整组不动`）。

    ⚠️ **绝不覆盖 / 绝不复活被删的词**：只在空组里追加，不 update、不 delete。

    调用时机：注册新用户后（`api/auth.py`）+ 应用启动补存量（`main.py`）。
    """
    from backend.app.core.initial_data import load_initial_forbidden_words

    specs = load_initial_forbidden_words()
    if not specs:
        return 0

    #: 该用户每个分组已有多少条（一次查完，避免逐条 COUNT）。
    existing = {
        (row[0] or DEFAULT_GROUP): int(row[1])
        for row in db.execute(
            select(ForbiddenWord.group_name, func.count())
            .where(ForbiddenWord.user_id == int(user_id))
            .group_by(ForbiddenWord.group_name)
        ).all()
    }

    created = 0
    touched: set[str] = set()
    for spec in specs:
        group = spec["group_name"]
        if existing.get(group) or group in touched:
            #: 该组已有词（用户资产）⇒ 整组跳过；同组多条只判一次。
            touched.add(group)
            continue
        #: 播种是唯一的自动建组场景（内置分组是随包内容，不是用户数据）。
        parent = find_group_row(db, user_id, group)
        if parent is None:
            parent = ForbiddenWord(user_id=int(user_id), parent_id=None, name=group, enabled=True)
            db.add(parent)
            db.flush()
        db.add(
            ForbiddenWord(
                user_id=int(user_id),
                parent_id=parent.id,
                name=group,
                word=spec["word"],
                match_mode=spec["match_mode"],
                enabled=spec["enabled"],
                remark=spec["remark"],
            )
        )
        created += 1
    if created:
        db.flush()
    return created


def seed_builtin_words_for_all_users() -> int:
    """应用启动时为**已有用户**补种（独立 Session、逐用户提交）。

    ⚠️ 与 `seed_builtin_rules_for_all_users` 同型：单用户失败不影响其它人。
    """
    from backend.app.models import User

    total = 0
    db = SessionLocal()
    try:
        user_ids = list(db.execute(select(User.id)).scalars().all())
    finally:
        db.close()

    for uid in user_ids:
        db = SessionLocal()
        try:
            total += seed_builtin_words(db, user_id=uid)
            db.commit()
        except Exception:  # noqa: BLE001 - 单个用户失败不拖垮其它人
            db.rollback()
            logger.exception("为用户 %s 播种违规词种子失败", uid)
        finally:
            db.close()
    return total


__all__ = [
    "ForbiddenWordError",
    "MAX_REMARK_LEN",
    "MAX_WORD_LEN",
    "create_word",
    "delete_word",
    "enabled_entries",
    "find_hits",
    "get_word",
    "enabled_groups",
    "list_groups",
    "list_words",
    "set_group_enabled",
    "normalize",
    "seed_builtin_words",
    "seed_builtin_words_for_all_users",
    "serialize",
    "set_enabled",
    "update_word",
    "validate_group",
    "validate_match_mode",
    "validate_word",
]
