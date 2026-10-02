"""迁移链产物 vs ORM 模型 —— schema 一致性守卫。

**为什么需要这个用例**（2026-09-30 实测事故）：ADR-021 的迁移 `b3c4d5e6f7a8`
把 `scheduled_job_logs.account_id` 改名 `platform_account_id`（设计 18 §6.3
「列名漂移」），但 `models/scheduled_job.py` 没跟着改。生产库上**任何**
`select(ScheduledJobLog)` 都抛
`OperationalError: no such column: scheduled_job_logs.account_id`
⇒ 任务中心「任务日志」页签一进就弹「任务日志加载失败，请稍后重试。」；
账号卡的「同步中」判据查的是同一张表，一起断。

**为什么全量回归没抓住**：测试库一律 `Base.metadata.create_all()` 建 ——
模型与库**天然一致**，于是「迁移改了库、模型没跟上」这个方向的漂移在
create_all 路线下**结构性不可见**（不是覆盖率不够，是方向不对）。
本用例补的正是那个方向：从**空库**跑完整迁移链，再逐表比对
「模型声明的列」与「迁移产出的列」。

口径：**模型要查的列，迁移链必须产出**（缺一列 = 线上 500）。
反向（迁移有、模型没声明）只报告不断言 —— 多一列不影响查询，
历史遗留列怎么处理属迁移纪律（设计 18），不归本用例管。

⚠️ **整表退场例外**（`RETIRED_TABLES`）：`f8a9b0c1d2e3`（设计 17 §13.2.5 v2.0）
drop 了 `model_configs` 一期表。M2 已完成调用层改接（`api/ai.py` /
`api/model_configs.py` / `ai_service` / `llm_gateway_client` /
`llm_gateway_usage_service` 全部不再引用 ORM 类），但 **ORM 类本身仍保留**
（`models/ai.py::ModelConfig`，字段被 `ResolvedModelContext` 的 docstring 与
测试桩 `_ModelConfigStub` 按名引用；删类会连带删掉 `DEFAULT_TEXT_MODEL_NAME`
的落点，属 M3 前端收口的活）。故本守卫**只对这一张表放行**，且集合里只剩它一条 ——
M3 删类时把条目一并删掉（留条目 = 给下一次真漏迁开了口子）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

import backend.app.models  # noqa: F401  让全部模型完成注册（顺序在 Base 之前无所谓）
from backend.app.core.database import Base
from tests.backend.conftest import run_alembic

#: 已被迁移**主动 drop**、但 ORM 类尚未移除的表（见模块 docstring）。
#:
#: ⚠️ **不是缓存**：每条都是一笔已知的、有下家的债。M3 删完 `ModelConfig`
#: 就把本集合清空 —— 留着不放 = 下一次真漏了一张表，守卫也只会当例外放行。
RETIRED_TABLES: frozenset[str] = frozenset({"model_configs"})


@pytest.mark.slow
def test_migration_chain_columns_cover_models(db_path: Path) -> None:
    """`upgrade head` 建出的库，必须覆盖模型声明要查的每一列。"""
    run_alembic(db_path, "upgrade", "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    inspector = inspect(engine)

    db_tables = set(inspector.get_table_names())
    assert db_tables, (
        "迁移链一张表都没建出来 —— 迁移没跑成功，下面的断言全是空摆（不许静默跳过）"
    )

    missing: dict[str, list[str]] = {}
    extra: dict[str, list[str]] = {}
    for name, table in Base.metadata.tables.items():
        if name not in db_tables:
            if name in RETIRED_TABLES:
                continue
            missing[name] = ["<整张表缺失>"]
            continue
        model_cols = {column.name for column in table.columns}
        db_cols = {column["name"] for column in inspector.get_columns(name)}
        gone = sorted(model_cols - db_cols)
        if gone:
            missing[name] = gone
            extra[name] = sorted(db_cols - model_cols)

    engine.dispose()

    if missing:
        lines = [
            "模型要查的列在迁移库里不存在 —— 线上这类查询必 500：",
        ]
        for name, cols in missing.items():
            lines.append(f"  · {name}: 缺 {cols}；迁移库里多出 {extra.get(name, [])}")
        lines.append(
            "修法：要么把模型改成迁移产出的列名（本次事故就是这一种 —— "
            "`models/scheduled_job.py` 漏改 `platform_account_id`），"
            "要么补一条迁移把列建/改回去。二者必居其一，不许放着。"
        )
        pytest.fail("\n".join(lines))
