"""**数据包列规则**：执行引擎（纯逻辑）+ 持久化（设计 40）。

## 职责边界

* **纯逻辑**（无 DB、无平台，可单测）：`apply_rule` / `validate_params` / `parse_params`；
* **持久化**（有 DB）：规则包与规则的 CRUD + 启用/停用。

## 为什么是「配置化规则」而不是「临场反推」

真机实测（设计 40 §1.2）：拿一个数据包的规则去套另一个包 ⇒ **0% 命中**
（0/26、0/52）；而**包内**给 1~2 行样例即可覆盖整列（52/52、26/26）。
⇒ 规则**天然按「包的类型」归属**，落库复用才对。用户（领域专家）一眼知道
「这包该取第 2 段」—— 让他**配一次**，比让算法每次猜更准、更快、可审计。

## ⚠️ 三条不可动摇的口径

1. **提不出 ⇒ 留空，不填原值**（AGENTS.md 硬约束 5「缺值不造假」）。
   把原值留在格子里，用户会以为「规则生效了」，然后拿着旧值发布 —— 这是最坏的一类静默错。
2. **纯函数语义**：每次从**该列原值**算，**不在上次结果上迭代**
   （否则反复点「执行」会越算越偏，用户也永远看不到「基于原值是什么样」）。
3. **分隔符切段用 `str.split`，不用正则**：正则在这里是错的工具
   （设计 40 §1.2 结论 3：把数据写死 / 学出枚举）。

## ⚠️ 两条**相反方向**的缺值口径（不要「统一」它们）

| op 家族 | 缺值 / 认不出的值 | 为什么 |
|---|---|---|
| `extract_*` | **提不出 ⇒ 留空**（`None`） | 规则**不适用**。把原值留在格子里，用户会以为规则生效了、拿着旧值发布 —— 最坏的一类静默错（硬约束 5）。 |
| `math_scale` | **空值按 0 参与计算**（用户口径 2026-10-15：「计算时为空的行默认为 0」） | 这是**运算**：整列都要有一个数；空单元格在表里就是「没有数量 / 没有价格」，按 0 算才是用户要的整列结果。留空会让半列结果空洞，用户还得自己填。 |

⇒ 方向相反是**刻意的**：前者怕「假值冒充真值」，后者是「整列参与运算」（设计 40 §9.5）。
落地方式：`apply_rule` 开头的「空串 ⇒ `None`」早退**只服务 `extract_*`**，
`fixed_text` / `math_scale` 走各自的辅助函数、绕过它（见 `apply_math_scale`）。

⚠️ **认不出数也按 0**（`"abc"` ⇒ `0`，不是留空）：与用户口径「为空的行默认为 0」同向
（他在表里看到的就是「这格没数」）。但**要记原因行** —— `execute_for_column` 数出
多少个认不出的格子并写进 `reason`，用户据此知道「有几行不是算出来的」。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.time import shanghai_now
from backend.app.models.data_package_rule import (
    OP_EXTRACT_BETWEEN,
    OP_EXTRACT_SEGMENT,
    OP_EXTRACT_STRIP,
    OP_FIXED_TEXT,
    MATH_MAX_STEPS,
    MATH_STEP_OPS,
    OP_COPY_COLUMN,
    OP_COPY_EXTRACT,
    OP_COPY_EXTRACT,
    COPY_EXTRACT_STEPS,
    OP_MATH_SCALE,
    RULE_OPS,
    DataPackageRule,
    DataPackageRuleSet,
)

logger = logging.getLogger(__name__)

#: 名称 / 备注 / 列名的长度上限（与列宽一致；超长在服务层报 400）。
MAX_NAME_LEN = 100
MAX_REMARK_LEN = 500
MAX_COLUMN_LEN = 100
#: 固定文本的长度上限（与列宽/规则名同量级；超长多半是粘错了东西）。
MAX_FIXED_TEXT_LEN = 500

#: 已实现的 op。
#: ⚠️ 2026-10-15 用户口径把 `fixed_text` / `math_scale` 一并要了（「提取数据规则中新增规则
#: 未支持：固定文本 + 批量运算（+ - × ×+ ×−）相关的规则」）⇒ 五条全实现
#: （枚举与参数口径早在 2026-10-14 就定型了，本轮不动表结构，设计 40 §2.3）。
_IMPLEMENTED_OPS: tuple[str, ...] = (
    OP_EXTRACT_SEGMENT,
    OP_EXTRACT_BETWEEN,
    OP_EXTRACT_STRIP,
    OP_FIXED_TEXT,
    OP_MATH_SCALE,
    OP_COPY_COLUMN,
    OP_COPY_EXTRACT,
)

#: 浮点结果的小数位（`round(v, n)`）。
#: ⚠️ **必须先 round 再格式化**：`1.7 * 3 = 5.1` 在二进制里是 `5.1000000000000005`
#: ⇒ 直接写回表格，用户会看到一串垃圾尾数，并且据此去发布。
_RESULT_DECIMALS = 2

#: `factor` / `offset` 的合法上限（防手滑把 `factor` 填成 1e12 把整列撑爆）。
_MAX_FACTOR = 1e9


class DataPackageRuleError(ValueError):
    """规则操作被拒（带结构化 `code`，由路由映射成 HTTP）。"""

    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# 纯逻辑：参数解析与校验
# --------------------------------------------------------------------------- #


def parse_params(raw: object) -> dict[str, Any]:
    """把 `params_json`（字符串）解析成 dict。

    ⚠️ **坏 JSON 不抛给用户**：库里存的是我们写的，坏了说明是数据问题
    ⇒ 当作 `{}` 处理（规则退化成「提不出」⇒ 留空），而不是让整个列表接口 500。
    """
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _as_int(value: object, *, field: str) -> int:
    """宽松取整（JSON 里可能是 `"2"` 或 `2.0`）。取不到 ⇒ 结构化 400。"""
    if isinstance(value, bool):  # bool 是 int 的子类，必须挡在前面
        raise DataPackageRuleError("DP_RULE_BAD_PARAM", f"「{field}」必须是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-+").isdigit():
        return int(value.strip())
    raise DataPackageRuleError("DP_RULE_BAD_PARAM", f"「{field}」必须是整数")


def _as_text(value: object) -> str:
    return "" if value is None else str(value)


def _as_number(value: object, *, field: str) -> float:
    """宽松取数（JSON 里可能是 `1.7` / `"1.7"` / `2`）。取不到 ⇒ 结构化 400。

    ⚠️ 与 `_as_int` 同理：**`bool` 必须挡在前面**（`True` 是 `int` 的子类，
    不挡就会被当成 `1` 静默通过 —— 用户勾了个勾选框结果整列 ×1）。
    """
    if isinstance(value, bool):
        raise DataPackageRuleError("DP_RULE_BAD_PARAM", f"「{field}」必须是数字")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            number = float(value.strip().replace(",", "").replace("￥", "").replace("¥", ""))
        except ValueError as error:
            raise DataPackageRuleError("DP_RULE_BAD_PARAM", f"「{field}」必须是数字") from error
    else:
        raise DataPackageRuleError("DP_RULE_BAD_PARAM", f"「{field}」必须是数字")
    if number != number or number in (float("inf"), float("-inf")):
        #: `NaN` / `inf` 能过 `float()`，但写进列就是一片 `nan`。
        raise DataPackageRuleError("DP_RULE_BAD_PARAM", f"「{field}」必须是有限数字")
    if abs(number) > _MAX_FACTOR:
        raise DataPackageRuleError(
            "DP_RULE_BAD_PARAM", f"「{field}」最多 ±{int(_MAX_FACTOR)}"
        )
    return number


def _as_number_or_none(value: object) -> Optional[float]:
    """**宽松取数（不抛错）** —— 值侧用（不是参数侧）。

    认不出 ⇒ `None`，由调用方按「空值 ⇒ 0」处置（用户口径 2026-10-15）。
    ⚠️ 参数用 `_as_number`（配错要 400），值用本函数（脏数据不该让整列 500）。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            number = float(value.strip().replace(",", "").replace("￥", "").replace("¥", ""))
        except ValueError:
            return None
    else:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def validate_params(op: str, params: dict[str, Any]) -> None:
    """**校验参数**（纯函数）—— 不合法即抛结构化 400，不静默降级。

    ⚠️ 这里刻意**严格**：参数是**用户配的**，配错了要让他在保存时就知道
    （而不是等到「执行后整列留空」才发现 —— 那时他分不清是规则错还是数据不对）。
    """
    if op not in RULE_OPS:
        raise DataPackageRuleError(
            "DP_RULE_BAD_OP", f"不认识的操作类型「{op}」"
        )
    if op not in _IMPLEMENTED_OPS:
        raise DataPackageRuleError(
            "DP_RULE_BAD_OP",
            f"操作「{op}」本轮尚未实现（当前只支持提取类规则）",
        )

    if op == OP_EXTRACT_SEGMENT:
        sep = _as_text(params.get("sep"))
        if not sep:
            #: 空分隔符切分无意义（`"abc".split("")` 会炸）。
            raise DataPackageRuleError(
                "DP_RULE_BAD_PARAM", "「分隔符」不能为空"
            )
        index = _as_int(params.get("index"), field="取第几段")
        if index == 0:
            #: 段号 1 起数，0 不存在（负数是「从后往前」，`-1` = 最后一段）。
            raise DataPackageRuleError(
                "DP_RULE_BAD_PARAM", "「取第几段」从 1 开始数，0 无意义"
            )
        return

    if op == OP_EXTRACT_BETWEEN:
        if not _as_text(params.get("open")):
            raise DataPackageRuleError(
                "DP_RULE_BAD_PARAM", "「起始符号」不能为空"
            )
        #: `close` 允许为空 = 取到该行末尾（用户可能只想「从某符号之后全要」）。
        occurrence = params.get("occurrence")
        if occurrence is not None:
            occ = _as_int(occurrence, field="第几处")
            if occ < 1:
                raise DataPackageRuleError(
                    "DP_RULE_BAD_PARAM", "「第几处」必须 ≥ 1"
                )
        return

    if op == OP_EXTRACT_STRIP:
        prefix = _as_text(params.get("prefix"))
        suffix = _as_text(params.get("suffix"))
        if not prefix and not suffix:
            raise DataPackageRuleError(
                "DP_RULE_BAD_PARAM", "「去前缀」与「去后缀」至少要填一个"
            )
        if params.get("is_regex"):
            #: 开了正则 ⇒ 必须能编译（否则执行时静默留空，用户不知道为什么）。
            for label, value in (("前缀", prefix), ("后缀", suffix)):
                if not value:
                    continue
                try:
                    re.compile(value)
                except re.error as error:
                    raise DataPackageRuleError(
                        "DP_RULE_BAD_PARAM",
                        f"「{label}」不是合法正则：{error}",
                    ) from error
        return

    if op == OP_FIXED_TEXT:
        #: ⚠️ 「缺 `text`」与「`text` 为空串」要分开：前者是**没配**（400），
        #: 后者是用户**故意**要把整列清空 —— 允许（用户口径 2026-10-15 未限制）。
        if "text" not in params or params.get("text") is None:
            raise DataPackageRuleError("DP_RULE_BAD_PARAM", "「固定文本」不能为空")
        text = _as_text(params.get("text"))
        if len(text) > MAX_FIXED_TEXT_LEN:
            raise DataPackageRuleError(
                "DP_RULE_BAD_PARAM", f"「固定文本」最多 {MAX_FIXED_TEXT_LEN} 个字"
            )
        return

    if op == OP_COPY_COLUMN:
        #: 来源列是**执行参数**（前端传 `source_column`）⇒ 规则自身没有参数。
        return
    if op == OP_COPY_EXTRACT:
        #: 第一步「复制哪一列」是**执行参数**（前端传 `source_column`）⇒ 规则自身不校验它
        #: （与 `OP_COPY_COLUMN` 同一口径）。
        #: 第二步「怎么提取」是**规则参数**（`extract`）⇒ 必须校验到能执行。
        extract = params.get("extract")
        if not isinstance(extract, dict):
            raise DataPackageRuleError(
                "DP_RULE_BAD_PARAM", "「复制 + 提取」必须配置第二步的提取方式"
            )
        step = _as_text(extract.get("op"))
        if step not in COPY_EXTRACT_STEPS:
            raise DataPackageRuleError(
                "DP_RULE_BAD_PARAM",
                f"不认识的提取方式「{step}」（可选："
                + " / ".join(COPY_EXTRACT_STEPS)
                + "）",
            )
        #: ⚠️ **复用同一批校验**：把 `extract` 的字段按对应 `extract_*` 的形状校验
        #: （`segment` 要 `sep` / `index`，`between` 要 `open`，`strip` 要前后缀）——
        #: 不另写一份判据（DEF-044），也不会出现「顶层能存、嵌套存不下」的口径差。
        _validate_extract_step(step, extract)
        return

    if op == OP_MATH_SCALE:
        #: ⚠️ 与 `_as_int` 的 `field` 口径一致：报错里给的是**界面上的字段名**，
        #: 不是 JSON 键名 —— 用户看到 `steps` / `value` 不知道指哪。
        #: 每一步的「值」本身也允许为 0（`+0` 是用户显式配的，不是漏填——
        #: 「随意搭配」的口径下，`+0` 与「不加」不必区分，都由步骤列表表达）。
        _validate_math_steps(params.get("steps"))
        return


# --------------------------------------------------------------------------- #
# 纯逻辑：执行
# --------------------------------------------------------------------------- #


def _validate_extract_step(step: str, extract: dict[str, Any]) -> None:
    """校验「复制 + 提取」第二步的**提取参数**。

    ⚠️ 刻意**复用顶层同一批判据** —— 把嵌套的 `extract` 按对应 `extract_*` 的字段形状
    校验一遍（`segment` 要 `sep` / `index`，`between` 要 `open`，`strip` 要前后缀），
    而不是另写一份：两份判据迟早会漂移（顶层能存、嵌套存不下），排查时极难定位。
    """
    if step == "segment":
        if not _as_text(extract.get("sep")):
            raise DataPackageRuleError("DP_RULE_BAD_PARAM", "「分隔符」不能为空")
        if _as_int(extract.get("index"), field="取第几段") == 0:
            raise DataPackageRuleError(
                "DP_RULE_BAD_PARAM", "「取第几段」从 1 开始数，0 无意义"
            )
        return
    if step == "between":
        if not _as_text(extract.get("open")):
            raise DataPackageRuleError("DP_RULE_BAD_PARAM", "「起始符号」不能为空")
        occurrence = extract.get("occurrence")
        if occurrence is not None and _as_int(occurrence, field="第几处") < 1:
            raise DataPackageRuleError("DP_RULE_BAD_PARAM", "「第几处」必须 ≥ 1")
        return
    #: step == "strip"
    if not _as_text(extract.get("prefix")) and not _as_text(extract.get("suffix")):
        raise DataPackageRuleError(
            "DP_RULE_BAD_PARAM", "「去前缀」与「去后缀」至少要填一个"
        )


def apply_copy_extract(value: str, params: dict[str, Any]) -> Optional[str]:
    """**复制 + 提取（多步）**：先照搬来源列的值，再按规则提取，结果覆盖当前列。

    用户口径 2026-10-15：
    > 「新增一个复制加提取规则，**分为多步执行**，第一步规则是将指定列**复制到当前列**，
    >   第二步是将复制过来的列值按照规则**提取结果覆盖当前列**」

    ⚠️ 调用方传进来的 `value` **已经是来源列的值**（执行器按 `source_column` 取好了）——
    本函数只做「第二步提取」这半段。**第一步「复制」的价值在意图表达上**：
    整条规则读起来是「A 列 → 取第 2 段 → B 列」，而不是让用户自己拼两条规则。
    ⚠️ 提取实现**直接复用** `apply_extract_*`（DEF-044：同一件事不留两份算法）。
    ⚠️ 提取不出 ⇒ `None`（与顶层 `extract_*` 同口径：调用方据此区分
    「不适用」与「结果本来就是空」）。
    """
    text = _as_text(value)
    if text == "":
        return None
    extract = params.get("extract") or {}
    step = _as_text(extract.get("op"))
    if step == "segment":
        return apply_extract_segment(text, extract)
    if step == "between":
        return apply_extract_between(text, extract)
    if step == "strip":
        return apply_extract_strip(text, extract)
    #: 校验拦过 ⇒ 到不了这里；真到了说明校验被绕过 ⇒ 留空而不是抛（执行期不炸整批）。
    return None


def apply_extract_segment(value: str, params: dict[str, Any]) -> Optional[str]:
    """按分隔符切段取第 N 段（`index` **1 起数**，负数从后往前）。

    ⚠️ **提不出 ⇒ `None`**（不是空串、不是原值）：
    调用方据此区分「规则没生效/不适用」与「提取结果本来就是空」。
    """
    sep = _as_text(params.get("sep"))
    if not sep:
        return None
    index = _as_int(params.get("index"), field="取第几段")
    parts = value.split(sep)
    if index > 0:
        pos = index - 1
    else:
        pos = len(parts) + index  #: `-1` ⇒ 最后一段
    if pos < 0 or pos >= len(parts):
        #: ⚠️ **提不出 ⇒ `None`（留空），绝不是 `return value`** ——
        #: 把原值留在格子里，用户会以为规则生效了、拿着旧值去发布
        #: （硬约束 5，本项目最坏的一类静默错）。
        #: ⚠️ 这个 `return value` 已被改坏过**四次**（三次是变异脚本中断留下，
        #: 一次是别的 worker 写入）⇒ 守卫 `test_not_enough_segments_returns_none`
        #: 必须一直在。
        #: ⚠️ 第四次（2026-10-15）：我**在变异脚本运行时并发跑了 pytest**（它也写
        #: `.hermes/reports/mut_dpr.xml`）⇒ 脚本收尾失败退出，把变异体留在了盘上。
        #: 教训不是「小心点」，而是**变异跑的时候不在同一棵树上做任何别的测试/编辑**。
        return None
    return parts[pos].strip()


def apply_extract_between(value: str, params: dict[str, Any]) -> Optional[str]:
    """取包裹符之间的内容（`occurrence` 指定第几处，默认第 1 处）。

    ⚠️ 只给 `open` 不给 `close` ⇒ 取到该行末尾。
    """
    open_s = _as_text(params.get("open"))
    if not open_s:
        return None
    close_s = _as_text(params.get("close"))
    occurrence = _as_int(params.get("occurrence") or 1, field="第几处")

    start = -1
    from_pos = 0
    for _ in range(occurrence):
        start = value.find(open_s, from_pos)
        if start < 0:
            return None  #: 没那么多处 ⇒ 提不出
        from_pos = start + len(open_s)

    body_start = start + len(open_s)
    if not close_s:
        return value[body_start:].strip()
    end = value.find(close_s, body_start)
    if end < 0:
        #: ⚠️ 只有起始符没有结束符 ⇒ **提不出**（`None`）。
        #: 别猜「到末尾」—— 那是 `close` **为空**时的语义（用户显式配的），
        #: 用户没这么配就不该这么解释（硬约束 5：缺值留空，不替用户脑补）。
        return None
    return value[body_start:end].strip()


def apply_extract_strip(value: str, params: dict[str, Any]) -> Optional[str]:
    """去前缀 / 去后缀（默认**按字面**，`is_regex` 打开才按正则）。

    ⚠️ **默认不用正则** —— 正则不做普通用户的默认工具（设计 40 §1.2 结论 3）。
    """
    prefix = _as_text(params.get("prefix"))
    suffix = _as_text(params.get("suffix"))
    is_regex = bool(params.get("is_regex"))
    out = value

    if prefix:
        if is_regex:
            #: 只去掉**开头**的匹配（`^` 锚定），否则会把中段也削掉。
            out = re.sub("^(?:" + prefix + ")", "", out, count=1)
        elif out.startswith(prefix):
            out = out[len(prefix):]
    if suffix:
        if is_regex:
            out = re.sub("(?:" + suffix + ")$", "", out, count=1)
        elif out.endswith(suffix):
            out = out[: -len(suffix)]
    return out.strip()


def apply_fixed_text(params: dict[str, Any]) -> str:
    """整列设为固定文本（**无条件**，包括原值为空的行）。

    ⚠️ 与 `extract_*` 语义**不同**：那是「从原值里取出一段」（取不到就留空），
    这条是「这一列统统写成这个值」 —— 原值为空的行**也要写**，
    否则用户看到「大部分行写上了、空行没写」，会以为规则漏跑了。
    """
    return _as_text(params.get("text"))


def format_math_result(number: float) -> str:
    """运算结果 → 写回表格的文本（整数不带小数点，小数最多 2 位）。

    ⚠️ **尾数由末尾的 `f"{...:.2f}"` 截**（`21 * 1.7 = 35.699999999999996`
    ⇒ `"35.7"`）。实测：去掉这里的 `round()` **输出逐字不变**
    ⇒ `round()` 是**冗余**的一道保险（保留无妨，但**别把它当唯一防线**，
    更别据此写「去掉它就会漏尾数」的变异 —— 那是哑弹，实测踩过）。
    ⚠️ 整数走 `int()` 而不是 `5.0`：表格里 `200.0` 与 `200` 是两个观感
    （本项目 `_cell_text` 也刻意去掉 `.0`，口径一致）——
    这一支**是真承重面**（改成 `f"{rounded:.2f}"` 会让整列变成 `200.00`）。
    """
    rounded = round(float(number), _RESULT_DECIMALS)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.{_RESULT_DECIMALS}f}".rstrip("0").rstrip(".")


def _validate_math_steps(raw: object) -> list[dict[str, Any]]:
    """校验并归一 `math_scale` 的**步骤列表**（用户口径 2026-10-15：「加减乘可随意搭配」）。

    ⚠️ **顺序有意义**（`+10` 后 `×2` ≠ `×2` 后 `+10`）⇒ 原样保留、不排序不归并。
    ⚠️ 至少一步、最多 `MATH_MAX_STEPS` 步（上限防手滑粘贴几百行）。
    """
    if not isinstance(raw, list) or not raw:
        raise DataPackageRuleError(
            "DP_RULE_BAD_PARAM", "「运算步骤」至少要一步（如 ×1.7）"
        )
    if len(raw) > MATH_MAX_STEPS:
        raise DataPackageRuleError(
            "DP_RULE_BAD_PARAM", f"「运算步骤」最多 {MATH_MAX_STEPS} 步"
        )
    steps: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise DataPackageRuleError("DP_RULE_BAD_PARAM", f"第 {index} 步格式不对")
        step_op = item.get("op")
        if step_op not in MATH_STEP_OPS:
            raise DataPackageRuleError(
                "DP_RULE_BAD_PARAM", f"第 {index} 步的运算符只能是 乘 / 加 / 减"
            )
        value = _as_number(item.get("value"), field=f"第 {index} 步的数值")
        steps.append({"op": step_op, "value": value})
    return steps


def parse_math_steps(params: dict[str, Any]) -> list[dict[str, Any]]:
    """取（并顺带校验）步骤列表 —— `apply_math_scale` 与 `make_summary` **共用同一份**。"""
    return _validate_math_steps(params.get("steps"))


def apply_math_scale(value: object, params: dict[str, Any]) -> str:
    """批量运算：**按 `steps` 的顺序依次执行**，加 / 减 / 乘随意搭配、不限步数。

    ⚠️ **空值按 0 参与计算**（用户口径 2026-10-15：「计算时为空的行默认为 0」）——
    这是**运算**：整列都要有一个数。与 `extract_*` 的「提不出 ⇒ 留空」**方向相反**，
    是刻意的（见模块 docstring 那张表）。

    ⚠️ **认不出的内容也按 0**（`"abc"` ⇒ 0，不是留空）：与用户口径同向
    （他在表里看到的就是「这格没数」），但要**记原因行**告诉他有几行不是算出来的
    —— 那一行由 `count_unrecognized` 数，**本函数不管计数**
    （此前它返回 `(结果, 是否认不出)` 的元组，但**没有任何调用方读第二项**
    ⇒ 死代码 + 与 `count_unrecognized` 构成两份实现，DEF-044 ⇒ 已收敛为单实现）。
    """
    steps = parse_math_steps(params)

    #: 认不出 ⇒ 按 0（口径见 docstring）。判据与 `count_unrecognized` **同一份**
    #: （都用 `_as_number_or_none`），不另立一套。
    result = _as_number_or_none(value)
    if result is None:
        result = 0.0

    #: ⚠️ **严格按顺序执行** —— 用户口径「加减乘可随意搭配」，
    #: 顺序是规则语义的一部分（`+10` 后 `×2` ≠ `×2` 后 `+10`），不许排序/归并。
    for step in steps:
        if step["op"] == "mul":
            result *= step["value"]
        elif step["op"] == "add":
            result += step["value"]
        else:  # "sub"
            result -= step["value"]
    return format_math_result(result)


def apply_copy_column(value: object) -> Optional[str]:
    """**复制列值**：把源列这一行的值**原样**写过来（用户口径 2026-10-15）。

    ⚠️ 与 `extract_*` 的区别：那条是「从文本里抠一段」，提不出 ⇒ 留空；
    这条是「照搬整格」—— **源格是空就搬空**（`None`），不是「提不出」。
    前端会把它落成 `null`（留空），与「源本来就是空」在结果上一致。
    """
    text = _as_text(value)
    return text if text != "" else None


def apply_rule(value: object, op: str, params: dict[str, Any]) -> Optional[str]:
    """**对单个值套一条规则**（纯函数，可单测）—— 提不出返回 `None`。

    ⚠️ 纯函数语义：输出只依赖 `(value, op, params)`，**不依赖上次结果**
    （见模块 docstring 第 2 条）。
    """
    if op == OP_FIXED_TEXT:
        #: ⚠️ **在「空值早退」之前**：固定文本是「无条件设为」，原值为空的行也要写。
        return apply_fixed_text(params)
    if op == OP_MATH_SCALE:
        #: ⚠️ 同理绕过早退：空值按 0 参与运算（口径见模块 docstring）。
        return apply_math_scale(value, params)
    if op == OP_COPY_COLUMN:
        #: 「照搬整格」—— 在早退之前（空值也要照搬，见 `apply_copy_column`）。
        return apply_copy_column(value)
    if op == OP_COPY_EXTRACT:
        #: ⚠️ 也在早退**之前**：本 op 的语义是「**复制**过来再提取」——
        #: 复制来的值是空 ⇒ 提取不出东西（`apply_copy_extract` 返回 `None`），
        #: 但**不适用**当前列的原值（不能因为原值空就跳过，原值本来就要被覆盖）。
        return apply_copy_extract(value, params)

    text = _as_text(value)
    if text == "":
        #: 原值本身为空 ⇒ 没什么可提取的（留空）。
        return None
    if op == OP_EXTRACT_SEGMENT:
        return apply_extract_segment(text, params)
    if op == OP_EXTRACT_BETWEEN:
        return apply_extract_between(text, params)
    if op == OP_EXTRACT_STRIP:
        return apply_extract_strip(text, params)
    raise DataPackageRuleError("DP_RULE_BAD_OP", f"不认识的操作类型「{op}」")


def count_unrecognized(values: Iterable[object], op: str, params: dict[str, Any]) -> int:
    """`math_scale` 里**原值认不出数**的行数（按 0 算了，要告诉用户有几行）。

    非 `math_scale` 一律 0 —— 那些 op 的缺值语义是「留空」，由 `missed` 表达。
    """
    if op != OP_MATH_SCALE:
        return 0
    return sum(1 for v in values if _as_number_or_none(v) is None)


def apply_rule_to_column(
    values: Iterable[object], op: str, params: dict[str, Any]
) -> list[Optional[str]]:
    """整列套用（`None` = 该行提不出 ⇒ 前端留空并计数报原因）。"""
    return [apply_rule(v, op, params) for v in values]


# --------------------------------------------------------------------------- #
# 持久化：序列化
# --------------------------------------------------------------------------- #


def make_summary(op: str, params: dict[str, Any]) -> str:
    """**人话摘要**（`按「;」取第 2 段` / `取 ( ) 之间`）—— 给前端显示。

    ⚠️ 不要给用户看 `{"sep":";","index":2}`：看不懂，还会以为存的是乱码
    （设计 40 §5.1）。
    """
    if op == OP_EXTRACT_SEGMENT:
        sep = _as_text(params.get("sep"))
        idx = params.get("index")
        return f"按「{sep}」取第 {idx} 段"
    if op == OP_EXTRACT_BETWEEN:
        o = _as_text(params.get("open"))
        c = _as_text(params.get("close"))
        occ = params.get("occurrence") or 1
        where = f"第 {occ} 处" if int(occ) > 1 else ""
        return f"取 {o} {c} 之间的内容{where}" if c else f"取 {o} 之后的内容{where}"
    if op == OP_EXTRACT_STRIP:
        bits = []
        if _as_text(params.get("prefix")):
            bits.append("去前缀")
        if _as_text(params.get("suffix")):
            bits.append("去后缀")
        tag = "（正则）" if params.get("is_regex") else ""
        return " + ".join(bits) + tag
    if op == OP_FIXED_TEXT:
        return f"固定为「{_as_text(params.get('text'))}」"
    if op == OP_COPY_COLUMN:
        #: 来源列由执行时选 ⇒ 摘要里说不出具体列名，只说语义
        #: （表头弹窗的提示行会带上真实列名，那边才知道）。
        return "复制来源列的值"
    if op == OP_COPY_EXTRACT:
        #: 两段拼：第一步（复制）+ 第二步（提取方式）。
        #: ⚠️ 与 `apply_copy_extract` 的两步**逐字对应** —— 摘要读起来就是它做的事。
        extract = params.get("extract") or {}
        step = _as_text(extract.get("op"))
        if step == "segment":
            how = f"取第 {_as_int(extract.get('index'), field='取第几段')} 段"
        elif step == "between":
            how = f"取「{_as_text(extract.get('open'))}」之间"
        else:
            how = "去前后缀"
        return f"复制来源列的值 → {how}"
    if op == OP_MATH_SCALE:
        #: ⚠️ **顺次拼接**，与 `apply_math_scale` 的**执行顺序逐字对应**
        #: （用户口径「加减乘可随意搭配」，摘要读起来就是他配的那串）。
        #: 符号用 U+2212 减号（`−`）而不是 ASCII `-`：与 `×` 同字号，读着不歪。
        parts: list[str] = []
        for step in parse_math_steps(params):
            if step["op"] == "mul":
                parts.append(f"×{format_math_result(step['value'])}")
            elif step["op"] == "add":
                parts.append(f"+{format_math_result(step['value'])}")
            else:  # "sub"
                parts.append(f"−{format_math_result(step['value'])}")
        #: 用空格连（`×1.7 + 5 − 3`）—— 每步一个 token，不挤成一坨。
        return " ".join(parts)
    return op


def serialize_rule(row: DataPackageRule) -> dict[str, Any]:
    params = parse_params(row.params_json)
    return {
        "id": row.id,
        "set_id": row.set_id,
        "rule_name": row.rule_name,
        "op": row.op,
        "params": params,
        "summary": make_summary(row.op, params),
        "enabled": bool(row.enabled),
        "sort_order": row.sort_order,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def serialize_set(
    row: DataPackageRuleSet, *, rules: Optional[list[DataPackageRule]] = None
) -> dict[str, Any]:
    """规则包。传 `rules` 则一并带上（**只带启用的**，供列头弹窗用）。"""
    data: dict[str, Any] = {
        "id": row.id,
        "name": row.name,
        "remark": row.remark or "",
        "enabled": bool(row.enabled),
        "sort_order": row.sort_order,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if rules is not None:
        data["rules"] = [serialize_rule(r) for r in rules]
        data["rule_count"] = len(rules)
    return data

# --------------------------------------------------------------------------- #
# 持久化：规则包（父）
# --------------------------------------------------------------------------- #


def _clean_text(raw: object, *, field: str, max_len: int, allow_empty: bool) -> str:
    text = str(raw or "").strip()
    if not text and not allow_empty:
        raise DataPackageRuleError("DP_RULE_BAD_PARAM", f"「{field}」不能为空")
    if len(text) > max_len:
        raise DataPackageRuleError(
            "DP_RULE_BAD_PARAM", f"「{field}」最多 {max_len} 个字"
        )
    return text


def list_sets(
    db: Session, user_id: int, *, enabled_only: bool = False, with_rules: bool = False
) -> list[DataPackageRuleSet]:
    """规则包清单（稳定排序：`sort_order` 再 `id`）。

    ⚠️ **排序必须有第二键 `id`**：`sort_order` 默认全 0，只按它会得到
    SQLite 的返回顺序（用户会看到「刷新一下就换位」）。
    """
    stmt = select(DataPackageRuleSet).where(DataPackageRuleSet.user_id == user_id)
    if enabled_only:
        stmt = stmt.where(DataPackageRuleSet.enabled.is_(True))
    stmt = stmt.order_by(DataPackageRuleSet.sort_order, DataPackageRuleSet.id)
    return list(db.execute(stmt).scalars().all())


def get_set(db: Session, user_id: int, set_id: int) -> DataPackageRuleSet:
    """取规则包 —— **归属不符一律 404**（不 403：不泄露「这个 id 存在」）。"""
    row = db.execute(
        select(DataPackageRuleSet).where(
            DataPackageRuleSet.id == set_id,
            DataPackageRuleSet.user_id == user_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise DataPackageRuleError("DP_RULE_SET_NOT_FOUND", "规则包不存在", status_code=404)
    return row


def create_set(
    db: Session, user_id: int, *, name: str, remark: str = "", enabled: bool = True
) -> DataPackageRuleSet:
    clean = _clean_text(name, field="规则包名", max_len=MAX_NAME_LEN, allow_empty=False)
    row = DataPackageRuleSet(
        user_id=user_id,
        name=clean,
        remark=_clean_text(remark, field="备注", max_len=MAX_REMARK_LEN, allow_empty=True),
        enabled=bool(enabled),
        sort_order=0,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        #: 撞 `uq_dp_rule_sets_user_name` ⇒ 友好 409（不回 500）。
        raise DataPackageRuleError(
            "DP_RULE_SET_DUPLICATE_NAME", f"已经有叫「{clean}」的规则包了", status_code=409
        ) from error
    db.refresh(row)
    return row


def update_set(
    db: Session,
    user_id: int,
    set_id: int,
    *,
    name: Optional[str] = None,
    remark: Optional[str] = None,
    enabled: Optional[bool] = None,
    sort_order: Optional[int] = None,
) -> DataPackageRuleSet:
    row = get_set(db, user_id, set_id)
    if name is not None:
        row.name = _clean_text(name, field="规则包名", max_len=MAX_NAME_LEN, allow_empty=False)
    if remark is not None:
        row.remark = _clean_text(
            remark, field="备注", max_len=MAX_REMARK_LEN, allow_empty=True
        )
    if enabled is not None:
        #: ⚠️ **只改父行**，不级联改子行 `enabled`（设计 40 §4.3）：
        #: 父恢复启用后，子行按自己原来的状态生效 —— 级联改是破坏用户数据
        #: （用户把某条规则单独关掉、又临时停了整包，恢复整包时那条规则
        #:  会**被无声打开**，且界面看不出是谁改的）。
        #:
        #: ⚠️ 这里曾出现过一个与**本注释直接矛盾**的实现：多了一段
        #: `db.execute(update(DataPackageRule)...values(enabled=...))`，
        #: 把子行全部改成与父行同值（`test_set_enabled_does_not_touch_children`
        #: 抓住了它）。⚠️ 注释对、代码错比注释错更危险 —— 后来的人读注释会
        #: 以为约束已生效，复审时直接跳过。
        row.enabled = bool(enabled)
    if sort_order is not None:
        row.sort_order = int(sort_order)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise DataPackageRuleError(
            "DP_RULE_SET_DUPLICATE_NAME", "已经有同名规则包了", status_code=409
        ) from error
    db.refresh(row)
    return row


def delete_set(db: Session, user_id: int, set_id: int) -> int:
    """删规则包 —— **级联删子规则**，返回被连带删掉的规则数（前端二次确认要用）。"""
    row = get_set(db, user_id, set_id)
    child_count = int(
        db.execute(
            select(func.count())
            .select_from(DataPackageRule)
            .where(DataPackageRule.set_id == row.id)
        ).scalar_one()
    )
    #: ⚠️ 显式删子行：`ondelete=CASCADE` 在 SQLite 上**要开 `PRAGMA foreign_keys`**
    #: 才生效（本项目连接默认没开）⇒ 依赖它会留下孤儿规则行。
    db.execute(delete(DataPackageRule).where(DataPackageRule.set_id == row.id))
    db.delete(row)
    db.commit()
    return child_count


# --------------------------------------------------------------------------- #
# 持久化：规则（子）
# --------------------------------------------------------------------------- #


def list_rules(
    db: Session, user_id: int, set_id: int, *, enabled_only: bool = False
) -> list[DataPackageRule]:
    """某包下的规则（先校验包归属 ⇒ 不是自己的包一律 404）。"""
    get_set(db, user_id, set_id)
    stmt = select(DataPackageRule).where(DataPackageRule.set_id == set_id)
    if enabled_only:
        stmt = stmt.where(DataPackageRule.enabled.is_(True))
    stmt = stmt.order_by(DataPackageRule.sort_order, DataPackageRule.id)
    return list(db.execute(stmt).scalars().all())


def get_rule(db: Session, user_id: int, rule_id: int) -> DataPackageRule:
    row = db.execute(
        select(DataPackageRule)
        .join(DataPackageRuleSet, DataPackageRule.set_id == DataPackageRuleSet.id)
        .where(DataPackageRule.id == rule_id, DataPackageRuleSet.user_id == user_id)
    ).scalar_one_or_none()
    if row is None:
        raise DataPackageRuleError("DP_RULE_NOT_FOUND", "规则不存在", status_code=404)
    return row


def _dump_params(params: object) -> str:
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise DataPackageRuleError("DP_RULE_BAD_PARAM", "参数格式不对")
    return json.dumps(params, ensure_ascii=False)


def create_rule(
    db: Session,
    user_id: int,
    *,
    set_id: int,
    rule_name: str,
    op: str,
    params: object,
    enabled: bool = True,
) -> DataPackageRule:
    get_set(db, user_id, set_id)  # 归属校验
    clean_name = _clean_text(
        rule_name, field="规则名称", max_len=MAX_COLUMN_LEN, allow_empty=False
    )
    if op not in RULE_OPS:
        raise DataPackageRuleError("DP_RULE_BAD_OP", f"不认识的操作类型「{op}」")
    validate_params(op, params if isinstance(params, dict) else {})

    row = DataPackageRule(
        set_id=set_id,
        rule_name=clean_name,
        op=op,
        params_json=_dump_params(params),
        enabled=bool(enabled),
        sort_order=0,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise DataPackageRuleError(
            "DP_RULE_DUPLICATE_NAME",
            f"这个规则包里已经有叫「{clean_name}」的规则了",
            status_code=409,
        ) from error
    db.refresh(row)
    return row


def update_rule(
    db: Session,
    user_id: int,
    rule_id: int,
    *,
    rule_name: Optional[str] = None,
    op: Optional[str] = None,
    params: object = None,
    enabled: Optional[bool] = None,
    sort_order: Optional[int] = None,
) -> DataPackageRule:
    row = get_rule(db, user_id, rule_id)
    if rule_name is not None:
        row.rule_name = _clean_text(
            rule_name, field="规则名称", max_len=MAX_COLUMN_LEN, allow_empty=False
        )
    next_op = op if op is not None else row.op
    if op is not None:
        if op not in RULE_OPS:
            raise DataPackageRuleError("DP_RULE_BAD_OP", f"不认识的操作类型「{op}」")
        row.op = op
    if params is not None or op is not None:
        #: 改了 op 但没给 params ⇒ 用现有 params 校验新 op（避免「换 op 后参数不配套」）。
        next_params = params if params is not None else parse_params(row.params_json)
        validate_params(next_op, next_params if isinstance(next_params, dict) else {})
        if params is not None:
            row.params_json = _dump_params(params)
    if enabled is not None:
        row.enabled = bool(enabled)
    if sort_order is not None:
        row.sort_order = int(sort_order)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise DataPackageRuleError(
            "DP_RULE_DUPLICATE_COLUMN", "这个规则包里已经有该列的规则了", status_code=409
        ) from error
    db.refresh(row)
    return row


def delete_rule(db: Session, user_id: int, rule_id: int) -> None:
    row = get_rule(db, user_id, rule_id)
    db.delete(row)
    db.commit()


def execute_for_column(
    db: Session,
    user_id: int,
    *,
    set_id: int,
    rule_id: int,
    values: list[object],
    source_column: Optional[int] = None,
) -> dict[str, Any]:
    """**执行一条规则**（后端算，前端不重复实现 —— DEF-044 单一实现）。

    返回 `{"values": [...], "missed": N, "summary": "…", "reason": "…"}`。

    ⚠️ `missed > 0` 时给 `reason` 一句话（25–30 字）说明「为什么有的格是空的」
    —— 用户口径要求「前端计数缺值渲染 0 并由原因行区分」（AGENTS.md 硬约束 5）。

    ⚠️ `source_column` = **计算时读哪一列**（用户口径 2026-10-15：「运算类的规则需要
    指定计算的基础列，默认当前列，可选择指定为其它列」）。它**不参与计算**，
    只随结果回传给前端 —— 前端据此把结果写回**点开的那个表头所在列**（目标列），
    而基础列可以是另一列（「把 A 列 ×1.7 写到 B 列」）。后端不落任何列状态。
    ⚠️ `values` 一律是**基础列**的值（前端按 `source_column` 取值后传进来）。
    """
    get_set(db, user_id, set_id)
    rule = get_rule(db, user_id, rule_id)
    if rule.set_id != set_id:
        raise DataPackageRuleError(
            "DP_RULE_NOT_FOUND", "该规则不属于这个规则包", status_code=404
        )
    params = parse_params(rule.params_json)
    validate_params(rule.op, params)
    out = apply_rule_to_column(values, rule.op, params)
    missed = sum(1 for v in out if v is None)
    reason = ""
    if missed:
        reason = f"{missed} 行的内容不符合规则（{make_summary(rule.op, params)}），已留空"
    if not reason:
        #: ⚠️ 运算类**不会**留空（空值按 0 算）⇒ 缺值原因行改说「有几行不是算出来的」：
        #: 用户口径是「为空的行默认为 0」，但「本来写着 abc」的行也是按 0 算的，
        #: 不说就是「悄悄把一列里的文字当成 0 做了乘法」（硬约束 5 的同型静默错）。
        unrecognized = count_unrecognized(values, rule.op, params)
        if unrecognized:
            reason = f"{unrecognized} 行的原值不是数字，已按 0 参与计算"
    return {
        "values": out,
        "missed": missed,
        "total": len(out),
        "summary": make_summary(rule.op, params),
        "reason": reason,
        "source_column": source_column,
        "rule_id": rule.id,
        "set_id": rule.set_id,
        "updated_at": rule.updated_at.isoformat() if rule.updated_at else None,
    }



# --------------------------------------------------------------------------- #
# 种子（新用户起点；设计 40 §6）
# --------------------------------------------------------------------------- #


def seed_builtin_rules(db: Session, *, user_id: int) -> int:
    """为某用户播种内置规则包（返回**新建的规则包数**）。

    ⚠️ **只补「该用户还没有任何规则包」的情况**：
    规则是**用户资产**（每个人的数据包列名/分隔符各自不同），
    他改过 / 删过的**不重建、不覆盖** —— 与「代码常量」有本质区别。
    ⚠️ 判定粒度是**整个用户**（不是按包名逐条）：只要他有一个包，就说明他已经
    开始配自己的规则了，此时再塞两个「示例包」进去只会碍事。

    调用时机：注册新用户后（`api/auth.py`）+ 应用启动补存量（`main.py::lifespan`）。
    """
    from backend.app.core.initial_data import resolve_initial_data_file

    existing = db.execute(
        select(func.count())
        .select_from(DataPackageRuleSet)
        .where(DataPackageRuleSet.user_id == user_id)
    ).scalar_one()
    if existing:
        return 0

    path = resolve_initial_data_file("data_package_rules.json")
    if path is None:
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        logger.warning("data_package_rules.json 读取失败，跳过播种")
        return 0

    specs = payload.get("data_package_rule_sets") or []
    created = 0
    for spec in specs:
        st = DataPackageRuleSet(
            user_id=user_id,
            name=str(spec.get("name") or "").strip() or "规则包",
            remark=str(spec.get("remark") or ""),
            enabled=bool(spec.get("enabled", True)),
            sort_order=int(spec.get("sort_order") or 0),
        )
        db.add(st)
        db.flush()  # 拿 id 给子行用
        for rspec in spec.get("rules") or []:
            params = rspec.get("params") or {}
            op = str(rspec.get("op") or "")
            #: ⚠️ 种子也要过校验 —— 种子写错了（如 op 未实现）不该把脏数据落库。
            try:
                validate_params(op, params if isinstance(params, dict) else {})
            except DataPackageRuleError:
                logger.warning("种子规则 %r/%r 校验不过，已跳过", spec.get("name"), rspec.get("rule_name"))
                continue
            db.add(
                DataPackageRule(
                    set_id=st.id,
                    rule_name=str(rspec.get("rule_name") or ""),
                    op=op,
                    params_json=json.dumps(params, ensure_ascii=False),
                    enabled=bool(rspec.get("enabled", True)),
                    sort_order=int(rspec.get("sort_order") or 0),
                )
            )
        created += 1
    db.flush()
    return created


def seed_builtin_rules_for_all_users() -> int:
    """应用启动时为**已有用户**补种（独立 Session、逐用户提交）。

    ⚠️ 与 `seed_builtin_prompt_templates_for_all_users` 同型：单用户失败不影响其它人。
    """
    from backend.app.core.database import SessionLocal
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
            total += seed_builtin_rules(db, user_id=uid)
            db.commit()
        except Exception:  # noqa: BLE001 —— 单个用户失败不拖垮其它人
            db.rollback()
            logger.exception("为用户 %s 播种数据包规则失败", uid)
        finally:
            db.close()
    return total


__all__ = [
    "DataPackageRuleError",
    "MAX_COLUMN_LEN",
    "MAX_FIXED_TEXT_LEN",
    "MAX_NAME_LEN",
    "MAX_REMARK_LEN",
    "apply_extract_between",
    "apply_extract_segment",
    "apply_extract_strip",
    "apply_fixed_text",
    "apply_math_scale",
    "apply_rule",
    "apply_rule_to_column",
    "apply_copy_column",
    "count_unrecognized",
    "create_rule",
    "create_set",
    "delete_rule",
    "delete_set",
    "execute_for_column",
    "format_math_result",
    "get_rule",
    "get_set",
    "list_rules",
    "list_sets",
    "make_summary",
    "parse_params",
    "seed_builtin_rules",
    "seed_builtin_rules_for_all_users",
    "serialize_rule",
    "serialize_set",
    "update_rule",
    "update_set",
    "validate_params",
]
