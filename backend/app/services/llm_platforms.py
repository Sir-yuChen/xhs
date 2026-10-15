"""模型行 `platform`（使用平台）列的**解析 / 落库 / 增减** —— 唯一收敛点。

## 为什么单独成模块（2026-10-14）

这四个函数原先住在 `api/llm_gateway.py`，但它们**不是端点逻辑**，
而是模型行一个列值的编解码规则。后果是 `services/llm_gateway_seed_service.py`
为了写一个模型行，**反向 import API 层**（`from backend.app.api.llm_gateway import
_dump_platforms`）—— 依赖方向是反的，且迁移脚本、ORM 注释都指向 API 层那个名字。

下沉到这里之后：**写入方（API 端点 / 种子服务 / 迁移）与读取方共用同一份实现**，
API 层原位置改为 re-export（`_parse_platforms = parse_platforms` 等），
既有调用点与测试不用改。

## 这一列的语义（回答「为什么种子不该写 xhs」）

`platform` = **本模型真的被哪些平台的业务点使用了**，形如 `{"xhs": 2}`
（值 = 该平台下指向该模型的**业务点个数**，是**状态**不是事件）。
⇒ 没有任何业务点用它时，它必须是**空的**。

⚠️ 种子阶段（`llm_gateway_seed_service`）曾无条件写 `{"xhs": 1}` ——
那是「预设」而不是「事实」，会让刚注册、一个业务点都没配的用户看到
「使用平台 = 小红书」。用户口径（2026-10-14）：

> 「模型没有在平台被配置使用，但是使用平台列依旧被设置成了小红书」
> 「**除非默认被配置到了业务点使用才应配置正确的使用列**」

⇒ 唯一正确的来源是**业务点映射时的同步**（见 API 层 `upsert_business_provider`
与 `sync_catalog` 里的 `merge_platforms` / `bump_platform` 调用）。
"""

from __future__ import annotations

import json as _json

__all__ = [
    "bump_platform",
    "dump_platforms",
    "merge_platforms",
    "parse_platforms",
]


def parse_platforms(raw: str | None) -> dict[str, int]:
    """解析模型行 `platform` 列为**引用计数**（v2.28，§13.2.9.7c）。

    形态 = `{"平台": 使用次数}`（如 `{"xhs": 2}`）——**不是**纯标识数组。

    ## 宽入（三种历史形态都要能读，不静默丢数据）

    | 落库值 | 解析结果 | 来源 |
    |---|---|---|
    | `{"xhs": 2}` | `{"xhs": 2}` | **本版形态** |
    | `["xhs"]`（JSON 数组） | `{"xhs": 1}` | v2.9–v2.27 的纯标识数组 |
    | `"xhs"`（裸字符串） | `{"xhs": 1}` | v2.9 前的单值 |

    ⇒ 旧形态按「计数 = 1」读取。⚠️ **不做「旧 = 0」**：那会让存量模型的所有平台
    在第一次读取时就消失（把「兼容」做成「清空」）。
    坏 JSON / 非法类型 ⇒ `{}`（**不臆造平台**，同 `_parse_models_json` 纪律）。
    """
    import json as _json

    text = (raw or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = _json.loads(text)
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        counts: dict[str, int] = {}
        for key, value in data.items():
            name = str(key).strip()
            if not name:
                continue
            try:
                n = int(value)
            except (TypeError, ValueError):
                n = 1
            if n > 0:
                counts[name] = n
        return counts
    if text.startswith("["):
        try:
            data = _json.loads(text)
        except Exception:
            return {}
        if not isinstance(data, list):
            return {}
        return {str(item).strip(): 1 for item in data if str(item).strip()}
    # 旧单值形态（v2.9 前）。⚠️ 也可能是 JSON 字符串标量（带引号）⇒ 先试解析
    if text.startswith('"'):
        try:
            scalar = _json.loads(text)
            if isinstance(scalar, str) and scalar.strip():
                return {scalar.strip(): 1}
        except Exception:
            pass
    return {text: 1}


def dump_platforms(counts: dict[str, int] | None) -> str:
    """引用计数 → 落库字符串（JSON 对象；**丢弃 ≤0 的键**，v2.28 §13.2.9.7c）。

    ⚠️ **归零即移除**（用户口径「当使用次数被减到为 0 时……该平台可以被移除」）：
    这里统一丢弃 `<= 0` 的项 —— 调用方不必自己 pop（**唯一收敛点**）。
    """
    import json as _json

    clean: dict[str, int] = {}
    for key, value in (counts or {}).items():
        name = str(key).strip()
        if not name:
            continue
        try:
            n = int(value)
        except (TypeError, ValueError):
            continue
        if n > 0:
            clean[name] = n
    return _json.dumps(clean, ensure_ascii=False)


def bump_platform(counts: dict[str, int], platform: str, delta: int) -> dict[str, int]:
    """引用计数增减（**唯一收敛点**，v2.28 §13.2.9.7c）。返回新 dict（不改入参）。

    * `delta=+1` ⇒ 该平台计数加一（不存在则从 1 起）；
    * `delta=-1` ⇒ 减一；**减到 0 即删键**（= 该平台下已无配置在用这个模型）。

    ⚠️ **计数语义 = 「该平台下指向该模型的业务点个数」**（**状态**），
    不是「被操作过几次」（**事件**）。所以幂等 upsert 同一业务点**不该** +1 ——
    调用方必须先判断「这条映射是不是改了模型」（见 `upsert_business_provider`）。
    """
    name = str(platform or "").strip()
    if not name:
        return dict(counts)
    result = dict(counts)
    next_value = int(result.get(name, 0)) + delta
    if next_value > 0:
        result[name] = next_value
    else:
        result.pop(name, None)
    return result


def merge_platforms(existing: dict[str, int], incoming: dict[str, int]) -> dict[str, int]:
    """**并入**引用计数（不是覆盖）—— 用户口径「支持多个平台共用一个模型」（§13.2.11.3）。

    ⚠️ 语义：已有 `{"xhs": 1}` + 传入 `{"douyin": 1}` ⇒ `{"xhs": 1, "douyin": 1}`。
    这与「在某平台配置该模型时，把当前平台写入」的口径一致：**在第二个平台配置
    同一个模型，是「追加一个使用平台」，不是「把第一个平台挤掉」**。
    ⚠️ 清空平台走 `PATCH` 显式传 `platform={}`（见 `update_gateway_model`）——
    那时语义是**覆盖为空**，不经本函数。

    ⚠️ 计数按平台**取较大值**（不是相加）：本函数的语义是「并入一批已有事实」（配置目录
    批量写入），不是「再加一次引用」。真正的 +1 走 `bump_platform`。

    ## 去重（2026-10-03 用户口径，原话）

    > 「**使用平台需要去重，每个平台只需要添加一次，存在则不在添加**」

    ⇒ 判据 = 平台键唯一（dict 天然满足）。这是「配置时并入」的下游保证：
    **同一平台配 N 次，列表里仍只有一个**。
    """
    merged = dict(existing)
    for name, value in (incoming or {}).items():
        key = str(name).strip()
        if not key:
            continue
        merged[key] = max(int(merged.get(key, 0)), int(value))
    return merged


