"""llm_gateway_configs + llm_gateway_snapshots (LLM 网关连接配置与用量快照)

设计 17 §3.8 / §3.9（v1.3 定稿）。T1：项目侧能连 New API 网关、取模型列表。

## 两张表

| 表 | 语义 |
|---|---|
| `llm_gateway_configs` | 项目侧对 New API 网关的**单行连接配置**（base_url / 访问令牌 / 本地 SQLite 路径 / 默认模型 / 健康状态）。服务商=New API 渠道、账号=New API 令牌，项目侧**不复制**这两层，只存「怎么连网关」；⚠️ 令牌收敛为**单个 `access_token`**（面板签发的 sk- 业务令牌，业务调用 + 拉统计共用）；`local_db_path` 显式存 New API 本地库路径（用量统计读 logs 表用，**不靠代码硬编码推导** —— 部署形态可能变化） |
| `llm_gateway_snapshots` | New API 用量**按日快照**（看板 + `/api/log/stat` 承载实时；项目侧缓存供前端展示，明细不落项目侧库） |

## 幂等（本项目硬纪律，设计 03 §9.5.1）

应用启动时自动 `upgrade head`，而 SQLite 的 DDL **不在事务里** ⇒ 中途失败会留下
「建了一半 + `alembic_version` 没推进」，下次启动从同一条 revision 重跑。
因此 `upgrade()` 先查 `PRAGMA table_info`：表已存在就直接返回。

## 可逆

`downgrade()` 直接 DROP（按依赖序：先快照表再配置表）—— 配置表删了快照无意义
（17 文档 §7.1 DELETE 端点口径「配置没了快照无意义，级联删」）。

## 唯一约束

- `llm_gateway_configs`：`(user_id, name)` —— 用户内网关配置名唯一（17 文档 §3.8）。
- `llm_gateway_snapshots`：`(user_id, gateway_id, stat_date)` —— 每网关每日一行 upsert 幂等（§3.9）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "m4n5o6p7q8r9"
down_revision = "l3m4n5o6p7q8"
branch_labels = None
depends_on = None

_TABLE_CONFIGS = "llm_gateway_configs"
_TABLE_SNAPSHOTS = "llm_gateway_snapshots"


def _columns(conn, table: str) -> set[str]:
    """表的列名集合（表不存在 ⇒ 空集）。"""
    rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return {str(row[1]) for row in rows}


def upgrade() -> None:
    conn = op.get_bind()

    if not _columns(conn, _TABLE_CONFIGS):
        op.create_table(
            _TABLE_CONFIGS,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("base_url", sa.String(length=512), nullable=False, server_default="http://127.0.0.1:3000"),
            sa.Column("access_token", sa.Text(), nullable=False, server_default=""),
            sa.Column("local_db_path", sa.String(length=512), nullable=False, server_default=""),
            sa.Column("default_model", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("healthy", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("last_health_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "name", name="uq_llm_gateway_configs_user_name"),
        )
        op.create_index("ix_llm_gateway_configs_user_id", _TABLE_CONFIGS, ["user_id"])

    if not _columns(conn, _TABLE_SNAPSHOTS):
        op.create_table(
            _TABLE_SNAPSHOTS,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("gateway_id", sa.Integer(), nullable=False),
            sa.Column("stat_date", sa.Date(), nullable=False),
            sa.Column("call_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_quota", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("avg_latency_ms", sa.Integer(), nullable=True),
            sa.Column("source", sa.String(length=16), nullable=False, server_default="newapi"),
            sa.Column("raw_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id", "gateway_id", "stat_date",
                name="uq_llm_gateway_snapshots_user_gateway_date",
            ),
        )
        op.create_index("ix_llm_gateway_snapshots_user_id", _TABLE_SNAPSHOTS, ["user_id"])
        op.create_index("ix_llm_gateway_snapshots_gateway_id", _TABLE_SNAPSHOTS, ["gateway_id"])


def downgrade() -> None:
    # 按依赖序：先删快照（引用 configs），再删配置表
    op.drop_table(_TABLE_SNAPSHOTS)
    op.drop_table(_TABLE_CONFIGS)