"""删除已退场同步链路的菜单行 + 遗留占位行

设计 75 §4.5 / §5.3（同步商品 / 同步笔记任务全链路移除）。

菜单种子的播种策略是「只补缺不覆盖」，这对新增权限是对的，但退场的行会**永远删不掉**：
代码与种子里都没了，库里那行还在 ⇒ 角色分配界面照旧列出可勾选项，勾了却不生效
（码不在 `ALL_PERMS`，被 rbac 过滤）。

⇒ 退场行必须由迁移显式删除，并连带清掉 `role_menus` 授权行（否则是孤儿）。
"""

from alembic import op
import sqlalchemy as sa

revision = "p2q3r4s5t6u7"
down_revision = "o1p2q3r4s5t6"
branch_labels = None
depends_on = None

#: 已退场、库里必须没有菜单行的权限码（**不含** `xhs:library:syncData`，见 docstring）。
RETIRED_PERMS = (
    "xhs:account:syncNotes",
    "xhs:account:syncGoodsNotes",
    "xhs:note:sync",
    "xhs:note:batchSync",
    "xhs:item:sync",
    "xhs:order:sync",
    "xhs:order:remark",
)

#: 早期排查权限链路时塞进库的桩行（`perms_registry` 里从没有这三个码）。
STRAY_PERMS = (
    "xhs:account:TMPA",
    "xhs:account:TMPB",
    "xhs:account:TMPC",
)

TARGET_PERMS = RETIRED_PERMS + STRAY_PERMS


def upgrade() -> None:
    conn = op.get_bind()
    perms_table = sa.table("menus", sa.column("id", sa.Integer), sa.column("perms", sa.String))

    menu_ids = [
        row[0]
        for row in conn.execute(
            sa.select(perms_table.c.id).where(perms_table.c.perms.in_(TARGET_PERMS))
        )
    ]
    if not menu_ids:
        return

    #: 先清授权行（`role_menus.menu_id` 指向菜单，留着就是孤儿）。
    #: `expanding=True` = SQLAlchemy 把列表摊成 `IN (:id_1, :id_2, …)`，
    #: PG 与 SQLite 都吃这个形态（不写方言分支）。
    ids = sa.bindparam("ids", value=menu_ids, expanding=True)
    conn.execute(sa.text("DELETE FROM role_menus WHERE menu_id IN :ids").bindparams(ids))
    conn.execute(sa.text("DELETE FROM menus WHERE id IN :ids").bindparams(ids))


def downgrade() -> None:
    """**不重建**已退场的菜单行。

    这些行的 `perms` 已经不在 `perms_registry` 里 —— 重建出来就是「看得见、
    勾了不生效」的假按钮（正是本迁移要消灭的形态）。桩行（TMPA/B/C）更不该回来。
    降级只承诺「不回滚这条数据变更」，应用侧照常启动。
    """
    return
