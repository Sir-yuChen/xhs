"""data_package_rules 改名为 xhs_data_package_rules

用户 2026-10-28 口径：「数据包是小红书私有，xhs 前缀」。该表只服务小红书商品发布
链路（数据包列规则），属 L1 工作区私有表 ⇒ 加 `xhs_` 前缀，命名与
`xhs_ai_drafts` / `xhs_no_traffic_cleanup_logs` 等其它 L1 表一致。

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-10-28
"""

from alembic import op
import sqlalchemy as sa

revision = "b4c5d6e7f8a9"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None

OLD = "data_package_rules"
NEW = "xhs_data_package_rules"


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                "select 1 from information_schema.tables "
                "where table_schema = 'public' and table_name = :t"
            ),
            {"t": table},
        ).first()
    )


def upgrade() -> None:
    if _table_exists(OLD) and not _table_exists(NEW):
        op.rename_table(OLD, NEW)

    bind = op.get_bind()
    # 索引 / 约束名一起换，免得与旧名混淆（幂等：存在才改名）。
    for old_name, new_name in (
        ("ix_data_package_rules_parent_id", "ix_xhs_data_package_rules_parent_id"),
        ("ix_data_package_rules_user_id", "ix_xhs_data_package_rules_user_id"),
    ):
        if bind.execute(
            sa.text("select 1 from pg_indexes where indexname = :n"), {"n": old_name}
        ).first():
            op.execute(sa.text(f'alter index "{old_name}" rename to "{new_name}"'))

    self_fk = bind.execute(
        sa.text(
            "select conname from pg_constraint "
            "where conrelid = cast(:t as regclass) and contype = 'f' "
            "and confrelid = cast(:t as regclass)"
        ),
        {"t": NEW},
    ).scalar()
    if self_fk and self_fk.startswith("fk_dp_rules_parent"):
        op.execute(sa.text(f'alter table "{NEW}" rename constraint "{self_fk}" to "fk_xhs_dp_rules_parent"'))


def downgrade() -> None:
    if _table_exists(NEW) and not _table_exists(OLD):
        bind = op.get_bind()
        for new_name, old_name in (
            ("ix_xhs_data_package_rules_parent_id", "ix_data_package_rules_parent_id"),
            ("ix_xhs_data_package_rules_user_id", "ix_data_package_rules_user_id"),
        ):
            if bind.execute(
                sa.text("select 1 from pg_indexes where indexname = :n"), {"n": new_name}
            ).first():
                op.execute(sa.text(f'alter index "{new_name}" rename to "{old_name}"'))
        op.rename_table(NEW, OLD)
