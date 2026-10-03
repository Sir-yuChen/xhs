"""测试隔离守卫：**用例不得静默读写生产库**（2026-10-04 建）。

## 为什么需要这条守卫

2026-10-04 实测事故：`tests/backend/test_workflows.py` 的 `run_workflow_job` 用例
往**真实库** `data/spider_xhs.db` 写了 4 条 `workflows` + 22 条 `workflow_runs`
+ 12 条 `workflow_nodes`。

根因**不是**「某个用例忘了申请正确的夹具」，而是**结构的**：
只有 `api_client` 夹具会调 `patch_service_sessions()`，而
`db_session` 不会 —— 于是「用 `db_session` + 直接调自开 Session 的服务函数」
这类用例**必然**打到生产库。项目此前已发生两次同类事故（conftest docstring
记载：2026-09-27 两次、2026-10-02 `material_scan_service` 一次）。

## 本文件钉住三件事

1. **`db_session` 必须打桩**（源码级断言，防止有人把那一行删掉）；
2. **`db_engine` 必须播种外键基线行**（`users` / `platform_accounts`）——
   否则隔离修好后，一批用例会因「夹具库自洽性缺失」而红；
3. **行为级证明**：用 `db_session` 调 `run_workflow_job` 后，
   **真实库的行数不变**（这是唯一不可伪造的判据）。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONFTEST = ROOT / "tests" / "backend" / "conftest.py"
PROD_DB = ROOT / "data" / "spider_xhs.db"


def _conftest_source() -> str:
    return CONFTEST.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# ① 源码级：db_session 必须打桩
# --------------------------------------------------------------------------- #
def test_db_session_fixture_patches_service_sessions():
    """`db_session` 里必须有 `patch_service_sessions(...)` 调用。

    ⚠️ 变异守护：删掉那一行 ⇒ 本用例必红。
    （这是 2026-10-04 事故的直接修复点，删掉就复发。）
    """
    source = _conftest_source()
    # 只取 db_session 函数体
    start = source.index("def db_session(")
    end = source.index("\ndef ", start + 1)
    body = source[start:end]
    assert "patch_service_sessions(" in body, (
        "db_session 夹具必须打桩服务层 Session 工厂，否则「用 db_session + 调服务函数」"
        "的用例会静默写生产库（2026-10-04 实测 22 行 workflow_runs 落到真实库）"
    )


def test_db_engine_fixture_does_not_seed_rows():
    """`db_engine` **不得**播种任何行 —— 契约是「新库应当是空的」。

    ⚠️ 这条守卫的方向曾经写反过（2026-10-04）：一度要求 `db_engine` 播种
    `users` / `platform_accounts` 基线行，结果红了 20+ 条 ——
    `test_conftest_fixtures::test_db_session_fixture_is_usable` 的契约是
    `assert count == 0`（「新库应当是空的」），`test_llm_gateway` 等也依赖空库。

    **外键父行由需要它的文件自己造**（`seed_owner_rows` 的用法），
    不在公共夹具里替所有用例决定。

    ⚠️ 变异守护：在 `db_engine` 里加回 `_seed_baseline_rows(...)` ⇒ 本用例必红。
    """
    source = _conftest_source()
    start = source.index("def db_engine(")
    end = source.index("\ndef ", start + 1)
    body = source[start:end]
    assert "_seed_baseline_rows(" not in body, (
        "db_engine 不得播种 —— 夹具契约为「新库应当是空的」"
        "（`test_conftest_fixtures` 会红，且一批依赖空库的用例断言失真）"
    )


def test_seed_baseline_flushes_parent_before_child():
    """`seed_owner_rows` 必须**先 flush 父行**（`users`）再插子行（`platform_accounts`）。

    ⚠️ 变异守护：删掉 `db.flush()` ⇒ 本用例必红。
    实测：同一 commit 里 SQLAlchemy 不保证按声明顺序发语句，
    `platform_accounts.user_id -> users.id` 会撞 `FOREIGN KEY constraint failed`
    （种子行自己就违反外键，表现为**全部用例 setup 失败**）。
    """
    source = _conftest_source()
    start = source.index("def seed_owner_rows(")
    end = source.index("\ndef ", start + 1)
    body = source[start:end]
    user_add = body.index("User(")
    account_add = body.index("PlatformAccount(")
    between = body[user_add:account_add]
    assert "db.flush()" in between, (
        "插 users 之后、platform_accounts 之前必须 flush，否则外键失败"
    )


# --------------------------------------------------------------------------- #
# ② 行为级：真实库不得被写
# --------------------------------------------------------------------------- #
def _count(table: str) -> int:
    if not PROD_DB.exists():
        pytest.skip("无真实库，跳过（CI / 新克隆环境）")
    with sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True) as conn:
        try:
            return conn.execute(f"select count(*) from {table}").fetchone()[0]
        except sqlite3.OperationalError:
            return -1


@pytest.mark.parametrize("table", ["workflows", "workflow_runs", "workflow_nodes"])
def test_workflow_tables_in_production_are_not_touched(table: str):
    """**行为判据**：用 `db_session` 调工作流服务后，真实库行数不变。

    ⚠️ 变异守护：把 `db_session` 里的打桩去掉 ⇒ 本用例必红
    （服务自开 Session 会真的往生产库插行）。

    这条是**不可伪造的判据** —— 源码断言可以被绕过，行数变化不会。
    """
    before = _count(table)
    if before < 0:
        pytest.skip(f"真实库无 {table} 表")

    from backend.app.services.workflow_service import run_workflow_job

    # 用一个必然不存在的 id：走 "workflow not found" 分支，**不产生业务副作用**，
    # 但只要 Session 未被打桩，它连的就是真实库（下面的行数就是判据）。
    result = run_workflow_job(10**9, user_id=1)
    assert result.get("skipped") is True

    assert _count(table) == before, (
        f"真实库 {table} 行数被测试改动 —— 说明服务层 Session 未打到夹具库"
    )
