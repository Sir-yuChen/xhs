"""add users.role and publish_jobs.idempotency_key

Revision ID: b1e4d7c9a2f3
Revises: 60cd5c95fde1
Create Date: 2026-09-18

对应二开 M1 治理里程碑的两个数据模型任务：

* **T-104** ``users.role`` —— 「系统管理功能」的前置阻塞项。没有角色字段，
  全局级开关（如 ``scheduler.enabled``）就无法限定管理员操作，只能对所有人开放。
* **T-103** ``publish_jobs.idempotency_key`` —— 防重复发布的第四道防线，
  也是四道里唯一可靠的一道（其余三道都在应用层）。见
  ``backend/app/services/publish_service.py`` 的完整论证。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional, Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'b1e4d7c9a2f3'
down_revision: Union[str, None] = '60cd5c95fde1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _normalize_options(options: Any) -> str:
    if options is None:
        return "{}"
    raw = str(options).strip()
    if not raw:
        return "{}"
    try:
        return json.dumps(json.loads(raw), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (ValueError, TypeError):
        return raw


def _build_key(platform: Optional[str], account_id: Any, title: Any, body: Any, options: Any) -> str:
    """与 ``services/publish_service.build_publish_idempotency_key`` 保持一致。

    这里**刻意内联一份**而不 import 业务模块：迁移必须能在任意历史代码状态下
    独立运行。若某天业务算法演进，旧迁移仍须产出当年那套键值。
    """
    fingerprint = "\x1f".join(
        [
            (platform or "").strip(),
            str(account_id if account_id is not None else ""),
            title or "",
            body or "",
            _normalize_options(options),
        ]
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:32]
    return f"{(platform or '').strip()}:{account_id}:{digest}"


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    table_names = set(inspector.get_table_names())

    # ---------------------------------------------------------------- T-104
    if "users" in table_names:
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        if "role" not in user_columns:
            with op.batch_alter_table("users", schema=None) as batch_op:
                batch_op.add_column(
                    sa.Column("role", sa.String(length=32), nullable=True, server_default="user")
                )
                batch_op.create_index(batch_op.f("ix_users_role"), ["role"], unique=False)

            # 历史行统一落到最小权限
            conn.execute(sa.text("UPDATE users SET role = 'user' WHERE role IS NULL OR role = ''"))

            # 引导规则：把**最早创建**的用户提升为 admin。
            # 若不这样做，迁移后不存在任何管理员，全局开关将永久无人可改。
            # 取最早创建者，因为它通常就是部署者本人。
            conn.execute(
                sa.text(
                    "UPDATE users SET role = 'admin' WHERE id = ("
                    "  SELECT id FROM users ORDER BY created_at ASC, id ASC LIMIT 1"
                    ")"
                )
            )

    # ---------------------------------------------------------------- T-103
    if "publish_jobs" in table_names:
        job_columns = {column["name"] for column in inspector.get_columns("publish_jobs")}
        if "idempotency_key" not in job_columns:
            with op.batch_alter_table("publish_jobs", schema=None) as batch_op:
                batch_op.add_column(sa.Column("idempotency_key", sa.String(length=160), nullable=True))
                batch_op.create_index(
                    batch_op.f("ix_publish_jobs_idempotency_key"), ["idempotency_key"], unique=True
                )

            # 只回填**已进入发布**的任务（publishing / published）它们已经占用了
            # 「该账号的该内容」这个槽位。pending / failed / cancelled 保持 NULL——
            # 它们尚未占用任何槽位，将来真正发布时才认领。
            # 这样既补上了历史事实，又不会误伤「建了又取消、之后再建同样内容」的合法流程。
            rows = conn.execute(
                sa.text(
                    "SELECT id, platform, platform_account_id, title, body, publish_options"
                    " FROM publish_jobs WHERE status IN ('publishing', 'published')"
                )
            ).fetchall()

            claimed: set = set()
            for row in rows:
                job_id, platform, account_id, title, body, options = row
                key = _build_key(platform, account_id, title, body, options)
                if key in claimed:
                    # 存量数据中已经存在「同账号同内容」的重复发布。
                    # 唯一索引装不下两条，故保留最早一条、其余留 NULL，
                    # 而不是让迁移失败——数据一条不丢，交人工核实。
                    # 对应的历史事故需按 04_测试文档/04 的 DEF-002 条目登记。
                    continue
                claimed.add(key)
                conn.execute(
                    sa.text("UPDATE publish_jobs SET idempotency_key = :key WHERE id = :job_id"),
                    {"key": key, "job_id": job_id},
                )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    table_names = set(inspector.get_table_names())

    if "publish_jobs" in table_names:
        with op.batch_alter_table("publish_jobs", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_publish_jobs_idempotency_key"))
            batch_op.drop_column("idempotency_key")

    if "users" in table_names:
        with op.batch_alter_table("users", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_users_role"))
            batch_op.drop_column("role")
