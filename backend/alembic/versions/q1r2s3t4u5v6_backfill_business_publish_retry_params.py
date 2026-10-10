"""补全「每日发布商业笔记」存量任务行的参数（重试三件套）与种子。

## 背景

`system.business_note_auto_publish`（每日发布商业笔记）建行时 `params` 只有四键
（`account_ids` / `limit` / `interval_seconds` / `generate_mode`）。2026-10-07 用户
追加了发布失败重试三件套（`retry_on_failure` / `max_retries` / `retry_interval_seconds`），
注册表 `JobSpec.params_defaults` 已声明、运行器也有兜底，但**存量行里没有这三个键**：

* 前端已按 `params_defaults` 预填（所以编辑弹窗看得见），
* 但 `scheduled_jobs.params` 列本身仍是旧的 —— 用户口径「将每日发布商业笔记任务
  调用参数**补全**」指的是这一层：库里要有，不只是界面上补。

## 动作

给所有 `job_key='system.business_note_auto_publish'` 的行，**缺失的键**按注册表默认值
补齐：`retry_on_failure=true` / `max_retries=3` / `retry_interval_seconds=30`。

⚠️ **只补缺失键，不覆盖已有值** —— 用户已经改过的 `max_retries=1` 必须原样保留。

⚠️ 注册表 `JobSpec.params_defaults` 里已经写了这几个默认值，而 `_builtin_seed_defaults()`
的兜底字典**不含 params**（只管 cron / status）。本迁移因此是**一次性回填**：
新装库由随包种子（`config/initial_data/scheduled_jobs.json`，同批已同步补齐）带全，
存量库由这里补齐，两条路同一份数值。

Revision ID: q1r2s3t4u5v6
Revises: p1q2r3s4t5u6
"""
from __future__ import annotations

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "q1r2s3t4u5v6"
down_revision: Union[str, None] = "p1q2r3s4t5u6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 任务键（与 `job_registry.JOB_KEY_BUSINESS_NOTE_AUTO_PUBLISH` 同值）。
JOB_KEY = "system.business_note_auto_publish"

#: 要补齐的键 → 默认值。**与注册表 `JobSpec.params_defaults` 同值**：
#: 补出来的行必须与「新建的行」长得一样，否则同一个任务两种形状。
BACKFILL_DEFAULTS: dict[str, object] = {
    "retry_on_failure": True,
    "max_retries": 3,
    "retry_interval_seconds": 30,
}


def _columns(table: str) -> set[str]:
    """表当前列集合（对不存在的表返回空集 —— 判据恒真时必须能看出来）。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    if "params" not in _columns("scheduled_jobs"):
        # 表不存在（更早的 revision 上跑）⇒ 没什么可补。
        return
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, params FROM scheduled_jobs WHERE job_key = :key"),
        {"key": JOB_KEY},
    ).fetchall()
    for row_id, raw_params in rows:
        params: dict = {}
        if raw_params:
            try:
                loaded = json.loads(raw_params) if isinstance(raw_params, str) else raw_params
            except (TypeError, ValueError):
                loaded = None
            if isinstance(loaded, dict):
                params = loaded
        changed = False
        for key, value in BACKFILL_DEFAULTS.items():
            # ⚠️ 只补**缺失**的键：已有值（哪怕用户改过）一律不动。
            if key not in params:
                params[key] = value
                changed = True
        if changed:
            bind.execute(
                sa.text("UPDATE scheduled_jobs SET params = :params WHERE id = :id"),
                {"params": json.dumps(params, ensure_ascii=False), "id": row_id},
            )


def downgrade() -> None:
    """移除本迁移补的三键（回到迁移前形状）。

    ⚠️ 无法区分「本迁移补的」与「用户在此之后自己填的」—— 一并移除是本仓对
    回填类迁移的既有口径（回退即回到旧 schema 的语义）。
    """
    if "params" not in _columns("scheduled_jobs"):
        return
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, params FROM scheduled_jobs WHERE job_key = :key"),
        {"key": JOB_KEY},
    ).fetchall()
    for row_id, raw_params in rows:
        params: dict = {}
        if raw_params:
            try:
                loaded = json.loads(raw_params) if isinstance(raw_params, str) else raw_params
            except (TypeError, ValueError):
                loaded = None
            if isinstance(loaded, dict):
                params = loaded
        changed = False
        for key in BACKFILL_DEFAULTS:
            if key in params:
                del params[key]
                changed = True
        if changed:
            bind.execute(
                sa.text("UPDATE scheduled_jobs SET params = :params WHERE id = :id"),
                {"params": json.dumps(params, ensure_ascii=False), "id": row_id},
            )
