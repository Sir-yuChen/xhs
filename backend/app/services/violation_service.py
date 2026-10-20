"""店铺违规管理 · 服务层（设计 42，2026-10-18）

## 这个模块存在的理由

千帆违规域的请求体有一套**只有真机才测得出来**的约定（字段名、单位、枚举、
「全部」到底该省字段还是给哨兵值）。把这些约定散落在路由里，会出现两份实现，
然后漂移。所以：**请求体组装与结果归一化只在本模块实现一次**，路由只做
鉴权 / 调适配器 / 错误映射。

单元测试直接测本模块的纯函数，期望值**逐字来自**真机抓包
（`docs/02_设计文档/_调研_千帆店铺违规管理_2026-10-18.md`）。

## 三条硬约定

1. **零落库**：本模块不 import 任何 ORM / Session，不写库。数据现取现渲染。
2. **tagName 不 trim、不去重**：真机 `user_tag` 里有 `违反法律法规` 与
   `违反法律法规 `（尾空格）两条不同 tag，tagIds 分别是 `80001101…` 与
   `80005677`。按名字 trim 去重会把两组 tagIds 合并，按名字过滤就会漏数据。
3. **缺值留 `None`**：平台没给的字段如实为 `None`，前端渲染 `-`；
   不补 `0`、不补空串（否则「没有」会被显示成「0 次」）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Literal

#: 违规域的页面路径（也是 SDK 的 referer）。
VIOLATION_REFERER = "/app-violation/violation-manage/list"

#: 列表场景：违规 / 预警。同一端点，仅 `warnFlag` 不同。
SCENE_VIOLATION = 0
SCENE_WARNING = 1

#: 违规对象里「全部」的枚举值（`entity_enum` 真机值）。
ENTITY_ALL = 999

#: 时间区间：「近 7 日」走 `violation7days` 语义 = 现在往前 7 天（含今天）。
RECENT_DAYS = 7


# --------------------------------------------------------------------- #
# 时间：唯一的毫秒换算实现
# --------------------------------------------------------------------- #
def _parse_date(value: str | date | None) -> date | None:
    """`YYYY-MM-DD` / `date` / `None` ⇒ `date`。解析不了返回 `None`（不抛）。

    前端日期控件给的就是 `YYYY-MM-DD`；容错解析是为了不因为一个脏值让整页 500。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _day_start_ms(day: date) -> int:
    """当天 00:00:00.000 的毫秒时间戳（本地时区，与真机一致）。"""
    return int(datetime(day.year, day.month, day.day).timestamp() * 1000)


def _day_end_ms(day: date) -> int:
    """当天 23:59:59.999 的毫秒时间戳。

    ⚠️ **必须是 `.999` 而不是次日 00:00:00**：真机抓包 2026-10-10~10-17 的
    结束值是 `1792252799999`。写成次日零点会漏掉最后一天 23:59:59.9xx 的记录，
    而且请求照样 200 —— 静默少几条，没有报错。
    """
    return int(datetime(day.year, day.month, day.day, 23, 59, 59, 999000).timestamp() * 1000)


def day_range_to_ms(
    start: str | date | None, end: str | date | None
) -> tuple[int | None, int | None]:
    """日期区间 ⇒ `(start_ms, end_ms)`，**唯一换算实现**。

    真机对照：`2026-10-10 ~ 2026-10-17` ⇒ `(1791561600000, 1792252799999)`。
    """
    start_day = _parse_date(start)
    end_day = _parse_date(end)
    return (
        _day_start_ms(start_day) if start_day else None,
        _day_end_ms(end_day) if end_day else None,
    )


def recent_window_ms(days: int = RECENT_DAYS, *, today: date | None = None) -> tuple[int, int]:
    """「近 N 日」窗口 ⇒ `(start_ms, end_ms)`；`now` 作为上界（真机用当前时间）。"""
    end = datetime.now() if today is None else datetime.combine(today, datetime.min.time())
    start = end - timedelta(days=days)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


# --------------------------------------------------------------------- #
# 违规原因：tagName → tagIdGroup
# --------------------------------------------------------------------- #
def reason_tag_ids(tags: list[dict[str, Any]], reason_name: str) -> list[str]:
    """按违规原因名查出 `tagIdGroup`。

    **精确匹配，不 trim**（硬约定 2）。查不到返回空列表 —— 调用方应当把它当作
    「这个原因不存在」而不是「不过滤」（后者会把用户明确选的条件静默丢掉）。
    """
    target = str(reason_name or "")
    for tag in tags or []:
        if not isinstance(tag, dict):
            continue
        if tag.get("tagName") == target:
            ids = tag.get("tagIds") or []
            return [str(i) for i in ids if i]
    return []


def normalize_reason_tags(raw: Any) -> list[dict[str, Any]]:
    """`user_tag` 的 `data.tags` ⇒ 前端下拉数据（**原样保留 tagName 的空格**）。"""
    out: list[dict[str, Any]] = []
    for tag in raw or []:
        if not isinstance(tag, dict):
            continue
        name = tag.get("tagName")
        if name is None:
            continue
        out.append(
            {
                "tag_name": name,          # ⚠️ 不 trim
                "tag_ids": [str(i) for i in (tag.get("tagIds") or []) if i],
            }
        )
    return out


# --------------------------------------------------------------------- #
# 列表请求体
# --------------------------------------------------------------------- #
def build_list_body(
    *,
    scene: int = SCENE_VIOLATION,
    page_no: int = 1,
    page_size: int = 10,
    keyword: str = "",
    entity_types: list[int] | None = None,
    reason_name: str = "",
    reason_tags: list[dict[str, Any]] | None = None,
    statuses: list[int] | None = None,
    severe_only: bool = False,
    allow_appeal: bool | None = None,
    correctable: bool | None = None,
    unread_only: bool = False,
    exam_only: bool = False,
    pending_only: bool = False,
    recent_days: bool = False,
    date_start: str | date | None = None,
    date_end: str | date | None = None,
) -> dict[str, Any]:
    """组装违规 / 预警列表请求体（**唯一实现**，字段名逐字来自真机）。

    映射表（真机实测，见调研报告 §3）：

    | 界面控件 | 字段 |
    |---|---|
    | 关键词 | `fuzzyMatchId` |
    | 违规对象 | `violationEntityTypes:[int]` |
    | 违规原因 | `tagIdGroup:[tagId]` |
    | 处罚状态 | `violationStatus:[int]` |
    | 严重违规 | `severePunishmentFlag:true` |
    | 可申诉 / 可整改 / 未读 | `allowAppeal` / `correctableFlag` / `readMark:0` |
    | 可考试 | `examStatusList:[1]` |
    | 待处理 | `filterStatus:"COMPOSITE"` |
    | 时间区间 | `violationStartTime` / `violationEndTime`（ms） |
    """
    body: dict[str, Any] = {
        "dataFlag": True,
        "warnFlag": int(scene),
        "pageNo": max(1, int(page_no)),
        "pageSize": max(1, int(page_size)),
    }

    word = str(keyword or "").strip()
    if word:
        body["fuzzyMatchId"] = word

    types = [int(t) for t in (entity_types or []) if int(t) != ENTITY_ALL]
    if types:
        body["violationEntityTypes"] = types

    if reason_name:
        ids = reason_tag_ids(reason_tags or [], reason_name)
        if ids:
            body["tagIdGroup"] = ids

    status_list = [int(s) for s in (statuses or [])]
    if status_list:
        body["violationStatus"] = status_list

    if severe_only:
        body["severePunishmentFlag"] = True
    if allow_appeal is not None:
        body["allowAppeal"] = bool(allow_appeal)
    if correctable is not None:
        body["correctableFlag"] = bool(correctable)
    if unread_only:
        body["readMark"] = 0
    if exam_only:
        body["examStatusList"] = [1]
    if pending_only:
        body["filterStatus"] = "COMPOSITE"

    start_ms, end_ms = day_range_to_ms(date_start, date_end)
    if start_ms is not None:
        body["violationStartTime"] = start_ms
    if end_ms is not None:
        body["violationEndTime"] = end_ms
    if recent_days and start_ms is None and end_ms is None:
        win_start, win_end = recent_window_ms()
        body["violationStartTime"] = win_start
        body["violationEndTime"] = win_end

    return body


#: 快捷筛选的**前端值 → 查询参数**（真机 bundle 原文的顺序与语义）。
QUICK_VIOLATION_KEYS: tuple[str, ...] = (
    "all",
    "pending",
    "unread",
    "severe",
    "recent7",
    "correctable",
    "examinable",
    "appealable",
)
QUICK_WARNING_KEYS: tuple[str, ...] = ("all", "high_risk", "correctable", "appealable")


def quick_filter_body(key: str, *, scene: int = SCENE_VIOLATION) -> dict[str, Any]:
    """快捷筛选 ⇒ 该条件下的请求体（分页由调用方叠加）。

    真机对照（`themisark_violation2_quick_filter`）：
    ``{0:全部, 1:近7日违规, 2:严重违规, 3:可整改, 4:可考试, 5:可申诉, 6:未读, 7:待处理}``。
    预警页是另一套：``全部 / 高危预警 / 可整改 / 可申诉``。

    ⚠️ 「近 7 日」是**两段叠加**：`violationStatus:[0,1,2]` + 时间窗；时间窗
    上界是「现在」（真机 `violationEndTime = Date.now()`，不是当天 23:59:59）。
    """
    if scene == SCENE_WARNING:
        mapping: dict[str, dict[str, Any]] = {
            "all": {},
            # ⚠️ 键名是**服务层入参**（`severe_only`），不是平台字段名（`severePunishmentFlag`）
            "high_risk": {"severe_only": True},
            "correctable": {"correctable": True},
            "appealable": {"allow_appeal": True},
        }
        return build_list_body(scene=scene, **mapping.get(key, {}))

    mapping = {
        "all": {},
        "pending": {"pending_only": True},
        "unread": {"unread_only": True},
        "severe": {"severe_only": True, "statuses": [0, 1, 2]},
        "correctable": {"correctable": True},
        "examinable": {"exam_only": True},
        "appealable": {"allow_appeal": True},
    }
    if key == "recent7":
        start_ms, end_ms = recent_window_ms()
        return {
            "dataFlag": True,
            "warnFlag": SCENE_VIOLATION,
            "violationStatus": [0, 1, 2],
            "violationStartTime": start_ms,
            "violationEndTime": end_ms,
        }
    return build_list_body(scene=scene, **mapping.get(key, {}))


# --------------------------------------------------------------------- #
# 归一化
# --------------------------------------------------------------------- #
def normalize_violation_row(dto: dict[str, Any]) -> dict[str, Any]:
    """列表 dto ⇒ 前端行（**原字段透传 + 少量补算**，缺值留 `None`）。

    不在这里裁剪平台字段：详情弹窗与列表用的是同一份 dto，裁掉就得再取一次。
    """
    if not isinstance(dto, dict):
        return {}
    row = dict(dto)
    status_info = dto.get("violationStatusInfo") or {}
    appeal_info = dto.get("appealStatusInfo") or {}
    level = dto.get("violationLevel") or {}
    entity_type = dto.get("violationEntityType") or {}

    actions: list[str] = []
    action_codes: list[str] = []
    for group in dto.get("violationActions") or []:
        for action in (group or {}).get("actions") or []:
            name = (action or {}).get("displayName")
            if name:
                actions.append(str(name))
            code = (action or {}).get("actionCode")
            if code:
                action_codes.append(str(code))

    display = dto.get("displayEntity") or {}
    row.update(
        {
            "violation_id": _text(dto.get("violationId")),
            "violation_reason": _text(dto.get("violationReason")),
            "violation_desc": _text(dto.get("violationDesc")),
            "violation_time": dto.get("violationTime"),
            "tenant": _text(dto.get("tenant")),
            "entity_type": entity_type.get("type"),
            "entity_type_name": _text(entity_type.get("displayName")),
            "entity_id": _text(dto.get("violationEntityId")),
            "entity_name": _text(display.get("displayName")),
            "entity_icon": _text(display.get("icon")),
            "entity_ext": display.get("ext") or None,
            "level": level.get("violationLevel"),
            "level_name": _text(level.get("name")),
            "status": status_info.get("violationStatus"),
            "status_name": _text(status_info.get("displayName")),
            "appeal_status": appeal_info.get("appealStatus"),
            "appeal_status_name": _text(appeal_info.get("displayName")),
            "actions": actions,
            "action_codes": action_codes,
            "can_appeal": dto.get("canAppeal"),
            "can_punished": dto.get("canPunished"),
            "correctable": dto.get("correctableFlag"),
            "severe": dto.get("severePunishmentFlag"),
            "read": dto.get("readMark"),
            "exam_status": _text(dto.get("examStatus")),
            "tag_id": _text(dto.get("tagId")),
            "warn_flag": dto.get("warnFlag"),
            "modify_advice": _text(dto.get("modifyAdvice")),
            "modify_advice_link": _text(dto.get("modifyAdviceLink")),
            "ext_info": _text(dto.get("extInfo")),
            "period_end_time": dto.get("periodEndTime"),
        }
    )
    return row


def _text(value: Any) -> str | None:
    """空值统一成 `None`（不造空串），非空转字符串。"""
    if value is None:
        return None
    text = str(value)
    return text if text != "" else None


def normalize_detail(payload: Any) -> dict[str, Any]:
    """详情接口 ⇒ 前端详情结构。

    入参**兼容两种形态**（真机实测两种都出现过调用方直接传的情况）：

    * 完整 envelope `{code, success, msg, data:{detail:{info:{...}}}}`
    * 已剥掉 envelope 的 `{detail:{info:{...}}}`

    归一化层自己辨认，而不是要求调用方记得剥 —— 忘记剥的那次会让所有字段变
    `null`，而接口是 200，没有任何报错（本卡真机实测踩过一次）。
    """
    node = payload if isinstance(payload, dict) else {}
    if isinstance(node.get("data"), dict):
        node = node["data"]

    info: Any = {}
    detail = node.get("detail") if isinstance(node, dict) else None
    if isinstance(detail, dict):
        info = detail.get("info") or {}
    elif isinstance(detail, list) and detail:
        first = detail[0]
        if isinstance(first, dict):
            info = first.get("info") or first
    elif isinstance(node, dict) and node.get("violationId"):
        # 调用方直接给了 info 本身
        info = node

    if not isinstance(info, dict):
        return {}
    if not info:
        # 平台没给 `info` ⇒ 空 dict（**不造行**：调用方按「平台暂未提供详情」渲染）
        return {}
    row = normalize_violation_row(info)
    # 详情比列表多出的两样：每条处罚的执行时间与状态
    action_details: list[dict[str, Any]] = []
    for group in info.get("violationActions") or []:
        for action in (group or {}).get("actions") or []:
            action_details.append(
                {
                    "name": _text((action or {}).get("displayName")),
                    "code": _text((action or {}).get("actionCode")),
                    "time_info": _text((action or {}).get("actionTimeInfo")),
                    "status": (action or {}).get("actionStatus"),
                }
            )
    row["action_details"] = action_details
    return row


def normalize_list_payload(payload: Any) -> dict[str, Any]:
    """列表接口 `data` ⇒ `{total, items[]}`（同时兼容 `dtos` 与 `vos` 两种键）。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    rows = data.get("dtos")
    if rows is None:
        rows = data.get("vos")
    rows = rows or []
    total = data.get("counts")
    if total is None:
        total = data.get("total")
    return {
        "total": int(total) if isinstance(total, (int, float)) else 0,
        "items": [normalize_violation_row(r) for r in rows if isinstance(r, dict)],
    }


def normalize_plain_list(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """取 `data[<key>]` 列表（赔付 / 仲裁 / 负反馈等各自的键名不同）。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    for key in keys:
        rows = data.get(key)
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    return []


def list_total(payload: Any) -> int:
    """取 `data.total` / `data.count`（各端点的总数键名不同）。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return 0
    for key in ("total", "count", "counts"):
        value = data.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


#: 负反馈列表的固定入参（真机逐字）。两段拼接：列表 + 汇总。
def build_negative_feedback_body(
    *,
    kind: Literal["quality", "logistics"],
    page_no: int = 1,
    page_size: int = 10,
    time_window_days: int = 30,
    control_flow: int = 0,
    rectify: int = 1,
) -> dict[str, Any]:
    """负反馈风险列表请求体（`quality` / `logistics` 字段集不同，逐字来自真机）。"""
    if kind == "quality":
        return {
            "controlFlowSearch": control_flow,
            "negativePayPkgCntAsc": 0,
            "pageNo": page_no,
            "pageSize": page_size,
            "rectifySearch": rectify,
            "timeWindowCode": str(time_window_days),
        }
    return {
        "controlFlowSearch": control_flow,
        "pageNo": page_no,
        "pageSize": page_size,
        "rectifySearch": rectify,
        "shipLogisticsNegativeCountOrderField": 0,
        "showNeedSolve": 0,
        "timeWindowCode": time_window_days,
    }


def build_exemption_body(
    *, page_no: int = 1, page_size: int = 10, start: str = "", end: str = ""
) -> dict[str, Any]:
    """免罚机会 · 爆单明细请求体（`startDtm` / `endDtm` 是 `YYYY-MM-DD`，不是毫秒）。"""
    start_day = _parse_date(start)
    end_day = _parse_date(end)
    if start_day is None or end_day is None:
        end_day = datetime.now().date()
        start_day = end_day - timedelta(days=29)
    return {
        "pageNo": page_no,
        "pageSize": page_size,
        "startDtm": start_day.isoformat(),
        "endDtm": end_day.isoformat(),
    }


def normalize_meta(
    *,
    statuses: Any,
    entities: Any,
    reason_tags: Any,
    spot_check_tags: Any = None,
) -> dict[str, Any]:
    """三类枚举 ⇒ 前端下拉（`{value, label}`）。**前端不硬编码枚举**。

    两套原因 tag 分开给：违规列表用 `reason_tags`，抽检用 `spot_check_tags`
    （真机 `bizScene:"SPOT_CHECK"` 返回的 tags 为空数组 —— 空就如实空）。
    """
    def _options(rows: Any, value_key: str, label_key: str) -> list[dict[str, Any]]:
        out = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            value = row.get(value_key)
            label = row.get(label_key)
            if value is None or label is None:
                continue
            out.append({"value": value, "label": label})
        return out

    return {
        "violation_status": _options(statuses, "violationStatus", "displayName"),
        "violation_entity": _options(entities, "type", "displayName"),
        "reason_tags": normalize_reason_tags(reason_tags),
        "spot_check_reason_tags": normalize_reason_tags(spot_check_tags),
    }


__all__ = [
    "VIOLATION_REFERER",
    "SCENE_VIOLATION",
    "SCENE_WARNING",
    "ENTITY_ALL",
    "QUICK_VIOLATION_KEYS",
    "QUICK_WARNING_KEYS",
    "day_range_to_ms",
    "recent_window_ms",
    "reason_tag_ids",
    "normalize_reason_tags",
    "build_list_body",
    "quick_filter_body",
    "normalize_violation_row",
    "normalize_detail",
    "normalize_list_payload",
    "normalize_plain_list",
    "list_total",
    "build_negative_feedback_body",
    "build_exemption_body",
    "normalize_meta",
]
