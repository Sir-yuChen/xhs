"""商品列表「查询构造」的**跨层契约守卫**（DEF-093，2026-10-05）。

## 这个缺陷为什么能活到验收

`POST /items/export` 必然 **422**：前端交的键名是本地缓存草稿的
`create_from` / `create_to`，而导出端点的模型（`extra="forbid"`）只认
`create_time_from` / `create_time_to`。

**后端全绿、真机 100% 复现** —— 因为 `test_export_filename_header.py` /
`test_product_edit.py::TestExport` 都是**后端直调、自己拼正确键名**：
一层「前端构造的 payload」在测试里根本不存在。这正是本项目反复出现的
`single-source-of-truth-judgments` 失败模式：**两边各自看都对，中间没有一条断言**。

## 这个文件补的就是那条断言

1. 从**后端 Pydantic 模型**（唯一真相源）取字段表，经 `--models` 传给
   `tests/frontend/shop_items_query.cjs`；
2. cjs 用 esbuild 打包**真实** `lib/shop-items-query.ts`，断言
   「导出形态的键 ⊆ `ItemsExportRequest` 字段」且「列表形态的键 ⊆ `ShopItemsRequest` 字段」；
3. 再断言**导出与列表除分页外逐字段相同** —— 这是链路判据
   「CSV 行数 == 列表命中总数」（设计 20 §2.2）在前端侧的充要条件。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# ⚠️ 必须写成**单字面串**：接线守卫（`test_testing_spec_guards.py::_mentions`）只认
# `tests/frontend/<name>.cjs` 这一种形态，分段拼路径它看不见 ⇒ 脚本会被判「没人跑」。
SCRIPT = ROOT / "tests/frontend/shop_items_query.cjs"


def _model_fields() -> dict[str, list[str]]:
    """从后端模型取字段名（**唯一真相源**，不手抄）。"""
    sys.path.insert(0, str(ROOT))
    from backend.app.api.platforms.xhs.qianfan import (  # noqa: PLC0415
        ItemsExportRequest,
        ShopItemsRequest,
    )

    return {
        "list": sorted(ShopItemsRequest.model_fields),
        "export": sorted(ItemsExportRequest.model_fields),
    }


def _run(env: dict[str, str] | None = None, models: dict[str, list[str]] | None = None):
    args = ["node", str(SCRIPT)]
    if models is not None:
        args += ["--models", json.dumps(models)]
    return subprocess.run(
        args,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env={**os.environ, **(env or {})},
    )


def test_export_and_list_payloads_match_the_backend_models() -> None:
    """导出 / 列表的前端 payload 键，必须全部落在后端模型字段内。"""
    models = _model_fields()
    assert models["list"], "取不到 ShopItemsRequest 字段 —— 模型改名了？"
    assert models["export"], "取不到 ItemsExportRequest 字段 —— 模型改名了？"

    result = _run(models=models)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    # 显式钉住「跨层契约这一段真的跑了」，不是被 SKIP 掉：
    assert "SKIP" not in result.stdout, f"跨层契约被跳过：{result.stdout}"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        # DEF-093 回退：日期用缓存草稿的键名（后端 extra="forbid" ⇒ 真机 422）
        ("draftkeys", "create_from"),
        # 导出带上分页 ⇒ 只导出一页（「CSV 行数 == 列表命中总数」判据失守）
        ("exportpaged", "page_no"),
        # 挂链笔记留空兜成「小于 0」⇒ 缺省态偷偷筛数据
        ("noteop", "note_count"),
        # 无排序态兜一个默认序 ⇒ 前端替后端做主（第二份真相源）
        ("sortdefault", "sort_field"),
    ],
)
def test_query_builder_guard_is_load_bearing(mutate: str, expected: str) -> None:
    """每个变异体改坏一处 ⇒ 行为脚本必须红（守卫承重，不是空转）。"""
    result = _run(env={"SHOP_ITEMS_QUERY_MUTATE": mutate}, models=_model_fields())
    assert result.returncode != 0, (
        f"变异 {mutate} 没被抓住 —— 守卫是哑弹。stdout={result.stdout}"
    )
    assert expected in result.stdout, f"红了但理由不对：{result.stdout}"
