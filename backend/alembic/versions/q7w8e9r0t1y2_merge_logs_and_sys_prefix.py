"""merge api_logs + business_logs into sys_logs; rename L0 tables to sys_ prefix; tasks -> queue_jobs

Revision ID: q7w8e9r0t1y2
Revises: f0a1b2c3d4e5

2026-10-28 用户口径三条：

1. 「business_logs 和 api_logs 功能重复合并一张表」⇒ `sys_logs`，`log_type` 分 `api` / `business`。
2. 「新增一个规范，系统基础表新增 sys 前缀」⇒ L0 表统一 `sys_`。
3. 「队列不要使用 tasks 容易误解」⇒ `tasks` → `queue_jobs`。

## 命名规范（本轮确立）

| 前缀 | 含义 | 数量 |
|---|---|---|
| `sys_` | L0 项目骨架（跨平台，换平台照样用） | 11 |
| `xhs_` | L1 小红书工作区私有 | 16 |
| `queue_` | 队列 / 异步任务载体 | 1 |
| `llm_` | LLM 网关子系统 | 5 |

## 日志表合并

两表都只用了一部分列，合表后各自留空：

* `api` 行用 `endpoint` / `status` / `status_code` / `meta` / `platform` / `user_id`
* `business` 行用 `level` / `event` / `action` / `actor_id` / `target` / `summary` / `detail_json`

⚠️ 类型判据是 `log_type`，**不是「某列是否为空」** —— 空值不是类型标识。
⚠️ `api_logs.user_id` 与 `business_logs.actor_id` 是两个语义不同的列，合并后都保留。

## 改名连带

PG 的 `ALTER TABLE ... RENAME TO` 保留外键定义，但**索引名/约束名不跟着改** ⇒ 逐个改。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "q7w8e9r0t1y2"
down_revision: Union[str, None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_API = "api_logs"
OLD_BIZ = "business_logs"
NEW_LOG = "sys_logs"

#: L0 改名（顺序即依赖顺序：users 最先，指向它的一律在后）。
RENAMES = (
    ("users", "sys_users"),
    ("roles", "sys_roles"),
    ("menus", "sys_menus"),
    ("role_menus", "sys_role_menus"),
    ("dict_rows", "sys_dicts"),
    ("system_settings", "sys_settings"),
    ("scheduled_jobs", "sys_scheduled_jobs"),
    ("scheduled_job_logs", "sys_scheduled_job_logs"),
    ("notifications", "sys_notifications"),
    ("tasks", "queue_jobs"),
)


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _rename_indexes(bind, old: str, new: str) -> None:
    """把带旧表名的索引/唯一约束名换成新表名（PG 不自动改）。"""
    insp = sa.inspect(bind)
    for idx in insp.get_indexes(new):
        name = idx.get("name")
        if name and old in name:
            op.execute(f'ALTER INDEX "{name}" RENAME TO "{name.replace(old, new)}"')
    try:
        for uc in insp.get_unique_constraints(new):
            name = uc.get("name")
            if name and old in name:
                op.execute(
                    f'ALTER TABLE "{new}" RENAME CONSTRAINT "{name}" TO '
                    f'"{name.replace(old, new)}"'
                )
    except Exception:  # noqa: BLE001 - 某些库不返回约束名，不影响改名
        pass


def upgrade() -> None:
    bind = op.get_bind()

    # ---- 1. 建 sys_logs ----
    if NEW_LOG not in _tables(bind):
        op.create_table(
            NEW_LOG,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("log_type", sa.String(16), nullable=False),
            sa.Column("trace_id", sa.String(32), nullable=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("platform", sa.String(32), nullable=True),
            # api 型
            sa.Column("endpoint", sa.String(256), nullable=True),
            sa.Column("status", sa.String(32), nullable=True),
            sa.Column("status_code", sa.Integer(), nullable=True),
            sa.Column("meta", sa.JSON(), nullable=True),
            # business 型
            sa.Column("level", sa.String(16), nullable=True),
            sa.Column("event", sa.String(64), nullable=True),
            sa.Column("action", sa.String(32), nullable=True),
            sa.Column("actor_id", sa.Integer(), nullable=True),
            sa.Column("actor_name", sa.String(64), nullable=True),
            sa.Column("workspace", sa.String(32), nullable=True),
            sa.Column("target_type", sa.String(32), nullable=True),
            sa.Column("target_id", sa.String(128), nullable=True),
            sa.Column("summary", sa.String(255), nullable=True),
            sa.Column("detail_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        for name, col in (
            ("ix_sys_logs_log_type", "log_type"),
            ("ix_sys_logs_trace", "trace_id"),
            ("ix_sys_logs_created", "created_at"),
            ("ix_sys_logs_user", "user_id"),
            ("ix_sys_logs_event", "event"),
            ("ix_sys_logs_endpoint", "endpoint"),
            ("ix_sys_logs_status_code", "status_code"),
            ("ix_sys_logs_actor", "actor_id"),
        ):
            op.create_index(name, NEW_LOG, [col])

    # ---- 2. 搬 api_logs ----
    if OLD_API in _tables(bind):
        op.execute(
            f"""
            INSERT INTO {NEW_LOG} (
                id, log_type, trace_id, user_id, platform, endpoint, status,
                status_code, meta, created_at
            )
            SELECT
                a.id, 'api', a.trace_id, a.user_id, a.platform, a.endpoint, a.status,
                a.status_code, a.meta, a.created_at
            FROM {OLD_API} a
            WHERE NOT EXISTS (SELECT 1 FROM {NEW_LOG} n WHERE n.id = a.id AND n.log_type = 'api')
            """
        )
        op.execute(
            f"""SELECT setval(pg_get_serial_sequence('{NEW_LOG}','id'),
                 GREATEST(COALESCE((SELECT MAX(id) FROM {NEW_LOG}),1),1))"""
        )

    # ---- 3. 搬 business_logs ----
    if OLD_BIZ in _tables(bind):
        op.execute(
            f"""
            INSERT INTO {NEW_LOG} (
                log_type, trace_id, level, event, action, actor_id, actor_name,
                workspace, target_type, target_id, summary, detail_json, created_at
            )
            SELECT
                'business', b.trace_id, b.level, b.event, b.action, b.actor_id,
                b.actor_name, b.workspace, b.target_type, b.target_id, b.summary,
                b.detail_json, b.created_at
            FROM {OLD_BIZ} b
            WHERE NOT EXISTS (
                SELECT 1 FROM {NEW_LOG} n
                WHERE n.log_type = 'business' AND n.event = b.event
                  AND n.created_at = b.created_at AND n.actor_id IS NOT DISTINCT FROM b.actor_id
            )
            """
        )

    # ---- 4. 删旧日志表 ----
    for old in (OLD_BIZ, OLD_API):
        if old in _tables(bind):
            op.drop_table(old)

    # ---- 5. L0 改名 ----
    for old, new in RENAMES:
        tables = _tables(bind)
        if old in tables and new not in tables:
            op.rename_table(old, new)
            _rename_indexes(bind, old, new)


def downgrade() -> None:
    bind = op.get_bind()

    # ---- 1. 改回原名 ----
    for old, new in reversed(RENAMES):
        tables = _tables(bind)
        if new in tables and old not in tables:
            op.rename_table(new, old)
            _rename_indexes(bind, new, old)

    # ---- 2. 拆回两张日志表 ----
    if OLD_API not in _tables(bind):
        op.create_table(
            OLD_API,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("platform", sa.String(32), nullable=True),
            sa.Column("endpoint", sa.String(256), nullable=False, server_default=""),
            sa.Column("status", sa.String(32), nullable=False, server_default="success"),
            sa.Column("meta", sa.JSON(), nullable=True),
            sa.Column("trace_id", sa.String(32), nullable=True),
            sa.Column("status_code", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
    if OLD_BIZ not in _tables(bind):
        op.create_table(
            OLD_BIZ,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("level", sa.String(16), nullable=False, server_default="info"),
            sa.Column("event", sa.String(64), nullable=False, server_default=""),
            sa.Column("action", sa.String(32), nullable=False, server_default=""),
            sa.Column("actor_id", sa.Integer(), nullable=True),
            sa.Column("actor_name", sa.String(64), nullable=False, server_default=""),
            sa.Column("workspace", sa.String(32), nullable=True),
            sa.Column("target_type", sa.String(32), nullable=False, server_default=""),
            sa.Column("target_id", sa.String(128), nullable=False, server_default=""),
            sa.Column("summary", sa.String(255), nullable=False, server_default=""),
            sa.Column("detail_json", sa.Text(), nullable=False, server_default=""),
            sa.Column("trace_id", sa.String(32), nullable=True),
        )

    if NEW_LOG in _tables(bind):
        op.execute(
            f"""
            INSERT INTO {OLD_API} (id, user_id, platform, endpoint, status, meta,
                                   trace_id, status_code, created_at)
            SELECT id, user_id, platform, COALESCE(endpoint, ''), COALESCE(status, 'success'),
                   meta, trace_id, status_code, created_at
            FROM {NEW_LOG} WHERE log_type = 'api'
              AND NOT EXISTS (SELECT 1 FROM {OLD_API} o WHERE o.id = {NEW_LOG}.id)
            """
        )
        op.execute(
            f"""
            INSERT INTO {OLD_BIZ} (created_at, level, event, action, actor_id, actor_name,
                                   workspace, target_type, target_id, summary, detail_json, trace_id)
            SELECT created_at, COALESCE(level, 'info'), COALESCE(event, ''),
                   COALESCE(action, ''), actor_id, COALESCE(actor_name, ''),
                   workspace, COALESCE(target_type, ''), COALESCE(target_id, ''),
                   COALESCE(summary, ''), COALESCE(detail_json, ''), trace_id
            FROM {NEW_LOG} WHERE log_type = 'business'
            """
        )
        op.drop_table(NEW_LOG)
