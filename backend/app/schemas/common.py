from __future__ import annotations

from typing import Any, Iterable


def paginated(items: Iterable[Any], page: int = 1, page_size: int = 20) -> dict:
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)
    materialized = list(items)
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size
    return {
        "total": len(materialized),
        "page": safe_page,
        "page_size": safe_page_size,
        "items": materialized[start:end],
    }


def paginated_with_total(
    items: Iterable[Any], page: int, page_size: int, total: int
) -> dict:
    """**真分页**响应装配：`items` 已是本页数据（调用方在 SQL 层做了 LIMIT/OFFSET），
    `total` 是全量计数。

    ⚠️ 与 `paginated()` 的分工（**不要混用**）：
    - `paginated()` = 「全量进内存后切片」的**旧口径**，适合本**就该全量读出的小集合**；
    - 本函数 = **数据量大、必须 DB 层分页**的集合（通知、台账……）—— 调用方先
      `COUNT(*)` 取 `total`、再 `LIMIT/OFFSET` 取本页。

    两者返回形状**逐字段一致**（`total`/`page`/`page_size`/`items`），前端无需区分。
    """
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)
    return {
        "total": int(total),
        "page": safe_page,
        "page_size": safe_page_size,
        "items": list(items),
    }
