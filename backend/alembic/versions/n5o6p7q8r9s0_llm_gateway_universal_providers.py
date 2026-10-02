"""llm_gateway v1.6 架构 A：令牌两级映射 + 本地调用台账 + 预警规则 + 模型目录

设计 17 §3.11/§3.12/§3.13 + §7.1a/§7.1b（v1.6，2026-09-29 用户口径「架构采用 A；
令牌按平台发、平台下按功能发；预警做站内通知」「两个数据库合并 —— 数据层全归主库，
New API 只做转发」）。

## 变更

| 项 | 语义 |
|---|---|
| `llm_gateway_configs.models_json` | **模型目录持久化**（Text，JSON 数组）。test 端点改打 OpenAI 兼容标准路由 `GET {base}/v1/models`（三方服务商与自托管 New API 通吃），成功后把模型 id 列表写进本列；配置列表响应带 `models` 字段 ⇒ 前端默认模型下拉由目录驱动 |
| `llm_gateway_tokens`（新表） | **业务功能→令牌两级映射**（platform × function → token_name）。令牌密钥在 New API 面板管理，本表只存映射关系；唯一 `(user_id, gateway_id, platform, function)` |
| `llm_gateway_alerts`（新表） | **预警规则**（用量阈值 / 失败率阈值 / 网关健康）。监控任务轮询台账对照触发，超限写 notifications；`last_fired_at` 同日去重 |
| `llm_gateway_usage_logs`（新表） | **本地调用台账**（唯一用量真相源）。项目侧每次经网关发起的 LLM 调用记一行（含 platform/function/token_name/model/token 用量/耗时/成败）。用量展示与预警都聚合本表，与 New API logs 彻底解耦 —— `local_db_path` 列废弃（保留不删） |

## 幂等（本项目硬纪律，设计 03 §9.5.1）

SQLite DDL 不在事务里 ⇒ `upgrade()` 先查 `PRAGMA table_info`：列/表已存在就跳过。

## 可逆

`downgrade()`：DROP 三张新表 + batch_alter_table 删 `models_json` 列。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "n5o6p7q8r9s0"
down_revision = "m4n5o6p7q8r9"
branch_labels = None
depends_on = None

_TABLE_CONFIGS = "llm_gateway_configs"
_TABLE_TOKENS = "llm_gateway_tokens"
_TABLE_ALERTS = "llm_gateway_alerts"
_TABLE_USAGE = "llm_gateway_usage_logs"


def _columns(conn, table: str) -> set[str]:
    """表的列名集合（表不存在 ⇒ 空集）。"""
    rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return {str(row[1]) for row in rows}


def _tables(conn) -> set[str]:
    rows = conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    return {str(row[0]) for row in rows}


def upgrade() -> None:
    conn = op.get_bind()

    # ① 模型目录列（已存在则跳过）
    if _TABLE_CONFIGS in _tables(conn) and "models_json" not in _columns(conn, _TABLE_CONFIGS):
        op.add_column(
            _TABLE_CONFIGS,
            sa.Column("models_json", sa.Text(), nullable=False, server_default="[]"),
        )

    # ② 令牌两级映射表
    if _TABLE_TOKENS not in _tables(conn):
        op.create_table(
            _TABLE_TOKENS,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("gateway_id", sa.Integer(), nullable=False),
            sa.Column("platform", sa.String(length=32), nullable=False),
            sa.Column("function", sa.String(length=32), nullable=False),
            sa.Column("token_name", sa.String(length=128), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id", "gateway_id", "platform", "function",
                name="uq_llm_gateway_tokens_user_gateway_platform_function",
            ),
        )
        op.create_index("ix_llm_gateway_tokens_user_id", _TABLE_TOKENS, ["user_id"])
        op.create_index("ix_llm_gateway_tokens_gateway_id", _TABLE_TOKENS, ["gateway_id"])

    # ③ 预警规则表
    if _TABLE_ALERTS not in _tables(conn):
        op.create_table(
            _TABLE_ALERTS,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("gateway_id", sa.Integer(), nullable=False),
            sa.Column("platform", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("function", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("metric", sa.String(length=16), nullable=False),
            sa.Column("operator", sa.String(length=8), nullable=False, server_default="gt"),
            sa.Column("threshold", sa.Float(), nullable=False),
            sa.Column("window_seconds", sa.Integer(), nullable=False, server_default="86400"),
            sa.Column("level", sa.String(length=16), nullable=False, server_default="warning"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("last_fired_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_llm_gateway_alerts_user_id", _TABLE_ALERTS, ["user_id"])
        op.create_index("ix_llm_gateway_alerts_gateway_id", _TABLE_ALERTS, ["gateway_id"])

    # ④ 本地调用台账表
    if _TABLE_USAGE not in _tables(conn):
        op.create_table(
            _TABLE_USAGE,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("gateway_id", sa.Integer(), nullable=False),
            sa.Column("platform", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("function", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("token_name", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("model_name", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="success"),
            sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_llm_gateway_usage_logs_user_id", _TABLE_USAGE, ["user_id"])
        op.create_index("ix_llm_gateway_usage_logs_gateway_id", _TABLE_USAGE, ["gateway_id"])
        op.create_index("ix_llm_gateway_usage_logs_created_at", _TABLE_USAGE, ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_llm_gateway_usage_logs_created_at", table_name=_TABLE_USAGE)
    op.drop_index("ix_llm_gateway_usage_logs_gateway_id", table_name=_TABLE_USAGE)
    op.drop_index("ix_llm_gateway_usage_logs_user_id", table_name=_TABLE_USAGE)
    op.drop_table(_TABLE_USAGE)
    op.drop_index("ix_llm_gateway_alerts_gateway_id", table_name=_TABLE_ALERTS)
    op.drop_index("ix_llm_gateway_alerts_user_id", table_name=_TABLE_ALERTS)
    op.drop_table(_TABLE_ALERTS)
    op.drop_index("ix_llm_gateway_tokens_gateway_id", table_name=_TABLE_TOKENS)
    op.drop_index("ix_llm_gateway_tokens_user_id", table_name=_TABLE_TOKENS)
    op.drop_table(_TABLE_TOKENS)
    with op.batch_alter_table(_TABLE_CONFIGS) as batch:
        batch.drop_column("models_json")
