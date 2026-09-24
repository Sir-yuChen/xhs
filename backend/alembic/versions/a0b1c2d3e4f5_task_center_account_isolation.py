"""task center account isolation (design 15 §4.1.1, DEF-066)

Revision ID: a0b1c2d3e4f5
Revises: k1l2m3n4o5p6
Create Date: 2026-09-24

2026-09-24 用户口径：「数据库中的所有数据均需要按照平台账号维度进行数据隔离，避免多用户
相互操作同一份数据」。隔离核查（DEF-066）发现定时任务中心三个缺口：① 内置任务是
`user_id IS NULL` 的**全局共享单行**（任何登录用户都能改 / 暂停 / 删除 / 执行它，
A 配的 account_ids 写在共享行上，B 执行同一行跑 A 的账号）；② `POST /jobs` 不过
account_ids 归属校验（PUT 过）；③ 调度日志零隔离（表无 user_id，三端点无过滤）。

本迁移做四件事（顺序不能反）：

1. `scheduled_job_logs` 加 `user_id` 列（可空 + 索引）—— 应用层由执行包装器从任务行
   快照写入；列可空只为迁移兼容，NULL = 归属不明（见第 4 步兜底）。
2. 日志 `user_id` 从任务行回填（`job_id` 还在的行）。
3. **全局内置行按用户克隆后删除**：每个现存用户各得一份同 `job_key` 行
   （`params.account_ids` 重置为 `[]` = 本人全部账号；该用户已有同 key 行则跳过），
   随后删除全部 `user_id IS NULL` 行。首个用户（id 最小）的克隆行**继承原全局行的
   历史日志链接**（`job_id` 改指克隆行），其余视角里这些历史日志的 `job_id` 置空
   （快照字段仍在，可读）。
4. 兜底归属：回填后仍为 NULL 的日志（孤儿日志——任务早已删除；全局行日志——源行
   user 为 NULL）一律归属**首个用户**（单用户库即唯一真实用户；零用户库则留 NULL，
   注册新用户后启动播种会为其补种，历史日志对新人不可见 —— 不猜归属）。

另做一件事：存量用户行的 `params.account_ids` 按归属**清洗**（滤掉不属于行主的 id）——
这些行是「POST 不校验」时期可能写进的外账号引用；空 = 本人全部账号，不猜、不保留越权引用。

⚠️ 运行态计数（run_count / fail_count / last_* / backoff_until）克隆行**不继承**：
共享行的计数混合了所有用户的执行历史，分给任何一个人都是假账；历史由日志承载
（首个用户的日志已挂回其克隆行）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a0b1c2d3e4f5"
down_revision: Union[str, None] = "k1l2m3n4o5p6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _shanghai_now() -> datetime:
    """与 `core/time.py::shanghai_now()` 同口径（迁移自包含，不 import 业务代码）。"""
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)


def _table_exists(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # 0. 表不存在（极端路径）⇒ 什么都不做 ------------------------------ #
    if not _table_exists(bind, "scheduled_jobs"):
        return

    # 1. 日志表加 user_id 列 + 索引 ------------------------------------- #
    if "user_id" not in _columns(bind, "scheduled_job_logs"):
        with op.batch_alter_table("scheduled_job_logs", schema=None) as batch_op:
            batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        op.create_index("ix_scheduled_job_logs_user_id", "scheduled_job_logs", ["user_id"])

    # 2. 日志 user_id 从任务行回填 --------------------------------------- #
    bind.execute(
        sa.text(
            "UPDATE scheduled_job_logs SET user_id = "
            "(SELECT user_id FROM scheduled_jobs WHERE scheduled_jobs.id = scheduled_job_logs.job_id) "
            "WHERE job_id IS NOT NULL"
        )
    )

    # 3. 全局内置行按用户克隆 ------------------------------------------- #
    users = [
        row[0]
        for row in bind.execute(sa.text("SELECT id FROM users ORDER BY id ASC")).all()
    ]
    first_user = users[0] if users else None

    globals_rows = bind.execute(
        sa.text(
            "SELECT id, name, job_key, trigger_type, cron_expression, run_at, "
            "misfire_policy, concurrent, params, status, remark, is_builtin "
            "FROM scheduled_jobs WHERE user_id IS NULL ORDER BY id ASC"
        )
    ).mappings().all()

    # 每个用户已有的 job_key（克隆判重：同用户同 key 不插第二份）
    owned_keys: dict[int, set[str]] = {}
    for user_id, job_key in bind.execute(
        sa.text("SELECT user_id, job_key FROM scheduled_jobs WHERE user_id IS NOT NULL")
    ).all():
        owned_keys.setdefault(user_id, set()).add(job_key)

    # 首个用户的克隆行 {job_key: clone_id}（历史日志改指用）
    first_user_clone: dict[str, int] = {}
    now = _shanghai_now()

    for row in globals_rows:
        params = row["params"]
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (TypeError, ValueError):
                params = {}
        if not isinstance(params, dict):
            params = {}
        # account_ids 重置为空 = 本人全部账号（隔离后「全部」的语义收口到行主）
        params = {key: value for key, value in params.items() if key != "account_ids"}
        params["account_ids"] = []
        params_json = json.dumps(params, ensure_ascii=False)

        for user_id in users:
            if row["job_key"] in owned_keys.get(user_id, set()):
                continue
            values = {
                "user_id": user_id,
                "name": row["name"],
                "job_key": row["job_key"],
                "trigger_type": row["trigger_type"],
                "cron_expression": row["cron_expression"],
                "run_at": row["run_at"],
                "misfire_policy": row["misfire_policy"],
                "concurrent": row["concurrent"],
                "params": params_json,
                "status": row["status"],
                "remark": row["remark"],
                "is_builtin": row["is_builtin"],
                # `created_at` 是 NOT NULL 且无 server_default —— 克隆行必须显式给
                # （真机迁移踩到：sqlite3.IntegrityError: NOT NULL constraint failed）
                "created_at": now,
            }
            job_columns = _columns(bind, "scheduled_jobs")
            insert_columns = [name for name in values if name in job_columns]
            placeholders = ", ".join(f":{name}" for name in insert_columns)
            bind.execute(
                sa.text(
                    f"INSERT INTO scheduled_jobs ({', '.join(insert_columns)}) "
                    f"VALUES ({placeholders})"
                ),
                {name: values[name] for name in insert_columns},
            )
            if user_id == first_user:
                clone_id = bind.execute(sa.text("SELECT last_insert_rowid()")).scalar()
                first_user_clone[row["job_key"]] = int(clone_id)
            owned_keys.setdefault(user_id, set()).add(row["job_key"])

    # 4. 全局行日志收尾 + 删除全局行 ------------------------------------- #
    # 首个用户的日志改指其克隆行（历史可见性不丢）；其余置空 job_id（快照字段仍在）。
    # ⚠️ 匹配条件必须含 `user_id IS NULL`：全局行的日志在第 2 步回填出的 user_id
    #    就是 NULL（源行 user 为 NULL），第 5 步兜底还没跑 —— 只写 `= 首个用户`
    #    一条都匹配不上（实测踩到：改指静默失效，历史日志全部变孤儿）。
    for row in globals_rows:
        clone_id = first_user_clone.get(row["job_key"])
        if clone_id is not None and first_user is not None:
            bind.execute(
                sa.text(
                    "UPDATE scheduled_job_logs SET job_id = :clone_id "
                    "WHERE job_id = :old_id AND (user_id IS NULL OR user_id = :user_id)"
                ),
                {"clone_id": clone_id, "old_id": row["id"], "user_id": first_user},
            )
        bind.execute(
            sa.text("UPDATE scheduled_job_logs SET job_id = NULL WHERE job_id = :old_id"),
            {"old_id": row["id"]},
        )
    bind.execute(sa.text("DELETE FROM scheduled_jobs WHERE user_id IS NULL"))

    # 5. 兜底归属：仍为 NULL 的日志（孤儿 / 全局行日志）归首个用户 --------- #
    if first_user is not None:
        bind.execute(
            sa.text(
                "UPDATE scheduled_job_logs SET user_id = :user_id WHERE user_id IS NULL"
            ),
            {"user_id": first_user},
        )

    # 6. 存量用户行的 account_ids 按归属清洗 ------------------------------ #
    # ⚠️ 表不存在（极端夹具路径）⇒ 没有任何账号存在 ⇒ 所有 account_ids 皆无归属 ⇒
    #    一律清洗为空（不猜、不保留越权引用）。真实库必有 platform_accounts。
    owned_accounts: dict[int, set[int]] = {}
    if _table_exists(bind, "platform_accounts"):
        for user_id, account_id in bind.execute(
            sa.text("SELECT user_id, id FROM platform_accounts WHERE deleted_at IS NULL")
        ).all():
            owned_accounts.setdefault(user_id, set()).add(account_id)

    for job_id, user_id, params in bind.execute(
        sa.text("SELECT id, user_id, params FROM scheduled_jobs WHERE user_id IS NOT NULL")
    ).all():
        if not isinstance(params, str) or not params:
            continue
        try:
            parsed = json.loads(params)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        raw_ids = parsed.get("account_ids")
        if not isinstance(raw_ids, list):
            continue
        allowed = owned_accounts.get(user_id, set())
        cleaned = [value for value in raw_ids if isinstance(value, int) and value in allowed]
        if cleaned == raw_ids:
            continue
        parsed["account_ids"] = cleaned
        bind.execute(
            sa.text("UPDATE scheduled_jobs SET params = :params WHERE id = :job_id"),
            {"params": json.dumps(parsed, ensure_ascii=False), "job_id": job_id},
        )


def downgrade() -> None:
    # 数据不还原（按用户克隆 → 全局单行的逆向合并不了义：N 份配置合成哪一份？）。
    # 只撤掉本迁移加的列与索引；播种改由运行期按用户补种（设计 15 §4.5）。
    if "user_id" in _columns(op.get_bind(), "scheduled_job_logs"):
        op.drop_index("ix_scheduled_job_logs_user_id", "scheduled_job_logs")
        with op.batch_alter_table("scheduled_job_logs", schema=None) as batch_op:
            batch_op.drop_column("user_id")
