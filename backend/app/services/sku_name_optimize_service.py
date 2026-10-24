"""SKU 规格名称超长压缩。

用户口径 2026-10-20：

> 「导入 sku 数据 / 导入 sku 预检通过的数据时，如果发现 sku 名称超过 30 字
>   （这个数值配置的设置中动态调整实时生效），则调用大模型将标题优化为 30 字以内，
>   文本内容意思不可改变，优化后使用弹窗展示优化前后的对应以供用户预览比较；
>   如果不满意可单独对一个 sku 规格名称重新生成并覆盖历史值」
>
> 补充口径：「30 字 60 个字符」「以 60 字符为准」

## 计量口径（**唯一真相源**）

长度一律按 **显示宽度** 算：**汉字计 2、ASCII 计 1** ⇒ 上限 60 == 30 个汉字。
判据是「**达到即优化**」：`display_width(name) >= max_chars` 就送大模型压缩。

⚠️ 不能用 `len(name)`：Python 的 `len` 把汉字当 1，60 个汉字等于 120 字符，
按 `len` 判会把「看起来 60 个汉字」的名字当成没超长而漏优化。

## 语义保护

提示词明确要求「只压缩长度、**不得改变语义**」；返回后本地再校验一次：
① 宽度必须降到上限内（超了就用截断兜底并标记 `truncated=True`）；
② 不许为空。任一不满足都退回原文（宁可长一点也不给出语义改变的结果）。

## 本次不落库

优化结果**只用于本次导入**（用户口径「只影响本次导入结果」）——
不回写 SKU 表、不改规格名。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: 默认上限（**字符**，汉字计 2）。与 `settings_service.BUILTIN_DEFAULTS` 同值。
DEFAULT_MAX_CHARS = 60

_SYSTEM_PROMPT = (
    "你是电商商品规格名称的精简专家。"
    "用户会给你一批 SKU 规格名称，每个都超出了长度上限。"
    "你的任务是**只压缩长度**、**绝对不能改变语义**：\n"
    "· 保留全部关键信息（颜色、尺寸、材质、型号、款式等差异点）；\n"
    "· 可以删掉冗余的修饰词、重复词、语气词、括号里的次要说明；\n"
    "· 不得臆造原文没有的信息，不得把两个不同的规格合并成同一个说法；\n"
    "· 长度按「汉字计 2、ASCII 计 1」计算，每个结果必须严格小于给定上限。\n"
    "只输出 JSON 数组，形如 [{\"id\": 1, \"name\": \"压缩后的名称\"}]，不要任何解释。"
)


def display_width(text: str) -> int:
    """按「汉字计 2、ASCII 计 1」算显示宽度。

    ⚠️ 这是本项目**唯一**的 SKU 名称长度判据 —— 前后端与提示词都必须用同一口径，
    否则会出现「页面说超长、后端说没超」的静默错位。
    """
    width = 0
    for ch in text or "":
        #: 东亚宽字符（CJK 统一表意文字 / 全角符号 / 假名 / 韩文）按 2 算。
        #: 用码位区间判断而不是 `unicodedata.east_asian_width`——后者对部分
        #: 兼容区返回 'A'（ambiguous），Windows/Linux 渲染宽度还不一致，
        #: 判据会飘；这里只认明确宽的那个区间。
        code = ord(ch)
        if (
            0x1100 <= code <= 0x115F  # 韩文字母
            or 0x2E80 <= code <= 0xA4CF  # CJK 部首 / 假名 / 汉字
            or 0xAC00 <= code <= 0xD7A3  # 韩文音节
            or 0xF900 <= code <= 0xFAFF  # CJK 兼容汉字
            or 0xFE30 <= code <= 0xFE6F  # CJK 兼容形式
            or 0xFF00 <= code <= 0xFF60  # 全角标点
            or 0xFFE0 <= code <= 0xFFE6  # 全角符号
            or 0x20000 <= code <= 0x3FFFD  # CJK 扩展 B+
        ):
            width += 2
        else:
            width += 1
    return width


def is_too_long(name: str, max_chars: int = DEFAULT_MAX_CHARS) -> bool:
    """是否**超长**（严格大于）—— 与前端 `isTooLong` 同一函数口径。

    ⚠️⚠️ 用户口径 2026-10-22：「**小于 60 字符就不应该展示**在 SKU 规格名称
    优化预览弹窗列表中」。
    演化：2026-10-20 是「**达到**即触发」（`>=`）—— 那时弹窗只是「可优化的提醒」；
    现在弹窗语义是「这些必须处理」，而平台原文「不能超过 30 个字（60 个字符）」
    ⇒ **60 本身合法** ⇒ 必须严格大于，否则 60 的合规项也会进列表（用户截图为证）。
    """
    return display_width(name) > max_chars


def truncate_to_width(text: str, max_chars: int) -> str:
    """兜底截断：按显示宽度切，不切断字符。"""
    if max_chars <= 0:
        return ""
    width = 0
    out: list[str] = []
    for ch in text or "":
        w = display_width(ch)
        if width + w > max_chars:
            break
        out.append(ch)
        width += w
    return "".join(out)


@dataclass
class SkuNameOptimizeItem:
    """一条 SKU 名称的优化结果。"""

    #: 调用方给的稳定标识（前端用行下标或 SKU 名，原样回传）。
    key: str
    original: str
    optimized: str
    #: 是否真的改动了（没超长的项恒 False，`optimized == original`）。
    changed: bool
    #: 大模型给的超长结果被本地截断了（语义可能略损，界面上要能看出）。
    truncated: bool = False
    #: 失败原因（成功时为空）。失败项 `optimized` 退回原文。
    reason: str = ""


def _parse_names(raw: str, expected_ids: list[int]) -> dict[int, str]:
    """从大模型输出里抠出 `{id: name}`；容忍 ```json 围栏与前后废话。"""
    text = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    #: 退一步：抓第一个 `[` 到最后一个 `]`（模型常在前后加解释）。
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return {}
    if not isinstance(payload, list):
        return {}
    out: dict[int, str] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        if idx not in expected_ids:
            continue
        name = str(row.get("name") or "").strip()
        if name:
            out[idx] = name
    return out


def optimize_names(
    *,
    db: Any,
    user_id: int,
    items: list[tuple[str, str]],
    max_chars: int = DEFAULT_MAX_CHARS,
    temperature: float = 0.2,
) -> list[SkuNameOptimizeItem]:
    """把超长的 SKU 名称压到 `max_chars` 以内（只压超长的）。

    `items` 是 `[(key, name), ...]`；返回顺序与入参一致。
    没超长的项原样返回、`changed=False`（前端据此只展示需要确认的行）。
    """
    #: ⚠️ **超长项初值 `optimized` 必须留空**（用户口径 2026-10-22：
    #: 「调用失败新规格名**应该还是空**不应该使用原规格名」）。
    #:
    #: 旧写法统一 `optimized=n`（= 原文）⇒ 失败时用户看到「新规格名 = 原规格名」，
    #: 既没红框（长度判据看的是原文，本身就是超长的 —— 但值不为空时前端
    #: `!!row.optimized && width <= max` 的判据会被绕成「有值但超长」，
    #: 视觉上分不清「模型给出了结果」还是「失败退回原文」）。
    #: ⇒ 没超长的项**保持原样**（`optimized = n`，`changed=False`，前端不展示）；
    #:   超长的项**先清空**，只有模型真的给出结果才填上。
    results: list[SkuNameOptimizeItem] = [
        SkuNameOptimizeItem(
            key=k,
            original=n,
            #: 超长的先留空（等模型结果）；没超长的原样返回（前端不展示它）。
            optimized="" if is_too_long(n, max_chars) else n,
            changed=False,
        )
        for k, n in items
    ]
    #: 只把**超长**的送去压缩（没超长的调模型纯属浪费额度）。
    pending = [(i, r) for i, r in enumerate(results) if is_too_long(r.original, max_chars)]
    if not pending:
        return results

    try:
        from backend.app.models.user import User
        from backend.app.core.business_provider_keys import (
            BUSINESS_KEY_PRODUCT_SKU_NAME_OPTIMIZE,
        )
        from backend.app.services.llm_gateway_client import (
            complete_text,
            resolve_business_provider,
        )

        user = db.get(User, user_id) if hasattr(db, "get") else None
        if user is None:
            raise RuntimeError(f"用户 {user_id} 不存在")
        context = resolve_business_provider(
            db, user, BUSINESS_KEY_PRODUCT_SKU_NAME_OPTIMIZE, platform="xhs"
        )
    except Exception as exc:  # noqa: BLE001 —— 未配置服务商时不能让导入整条挂掉
        logger.warning("SKU 名称优化：LLM 服务商不可用（%s）", str(exc)[:200])
        for _, row in pending:
            row.reason = f"未配置可用的文本模型：{str(exc)[:120]}"
        return results

    #: 一次最多送 40 条：规格名很短，40 条也不会撑爆上下文；多了分批。
    batch_size = 40
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        payload = [
            {"id": pos + 1, "name": row.original}
            for pos, (_, row) in enumerate(batch)
        ]
        user_prompt = (
            f"长度上限：{max_chars} 个字符（汉字计 2，ASCII 计 1）。\n"
            "请把下面每个名称压到**严格小于**该上限，语义不得改变：\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        try:
            raw = complete_text(
                model_config=context,
                api_key=context.api_key,
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                temperature=temperature,
                platform="xhs",
                function=BUSINESS_KEY_PRODUCT_SKU_NAME_OPTIMIZE.key,
            )
        except Exception as exc:  # noqa: BLE001 —— 单批失败只影响这批
            logger.warning("SKU 名称优化调用失败：%s", str(exc)[:200])
            for _, row in batch:
                row.reason = f"大模型调用失败：{str(exc)[:120]}"
            continue

        mapping = _parse_names(raw, list(range(1, len(batch) + 1)))
        for pos, (_, row) in enumerate(batch):
            got = (mapping.get(pos + 1) or "").strip()
            if not got:
                row.reason = "大模型未返回这条的结果"
                continue
            if got == row.original:
                #: 原样返回 = 没压下来，按失败处理（不假装成功）。
                row.reason = "大模型未改动，仍超长"
                continue
            #: ② 本地兜底：模型没压够就截断（宁可略损也不破坏上限契约）。
            truncated = False
            if display_width(got) >= max_chars:
                got = truncate_to_width(got, max_chars - 1)
                truncated = True
                if not got:
                    row.reason = "大模型结果无法截断到上限内"
                    continue
            row.optimized = got
            row.changed = True
            row.truncated = truncated
            row.reason = ""

    return results
