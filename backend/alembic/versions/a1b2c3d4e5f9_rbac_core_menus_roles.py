"""rbac core: menus / roles / role_menus + users +6 columns

v1.1.0 / 设计 47 §2.1-2.3 + §2.6（S1 迁移 1 of 3）。

⚠️ 这是**新的方言中立迁移**（设计 51 §2.4）：只用 `op.create_table` /
`op.add_column` / `op.create_index`，不用 `PRAGMA table_info`、不用
`json_extract`、不用 `datetime(col,'+8 hours')`。列存在性一律走
`sa.inspect(conn).get_columns()`（两个方言通用）。

## 两个刻意的决定

1. **`menus.parent_id` 不建外键**（设计 47 §2.1）：`RESTRICT` 会让删除父节点
   每次都撞一个不带解释的 DB 错；`CASCADE` 会静默删掉整棵子树 —— 而
   `role_menus` 指向那些行的授权会一起消失（= 静默收回别人的权限）。
   改用应用层「有子节点即拒」+ 孤儿守卫测试。
2. **`role_menus` 的两个外键 `ondelete` 不同**：`role_id` CASCADE（删角色 ⇒ 授权行
   无意义）、`menu_id` RESTRICT（有授权引用的菜单不许删，P8 `MENU_IN_USE`）。
   ⚠️ SQLite 默认 **不启用** `PRAGMA foreign_keys` ⇒ 上面的语义在真机上
   **静默失效**。这里在迁移里显式打开（`op.get_bind()` 的连接级设置），
   应用侧由 `core/database.py` 的 connect 事件负责，两边都设。

Revision ID: a1b2c3d4e5f9
Revises: c1d2e3f4a5b8
"""

from __future__ import annotations

import contextlib

import sqlalchemy as sa

from alembic import op

revision = "a1b2c3d4e5f9"
down_revision = "c1d2e3f4a5b8"
branch_labels = None
depends_on = None


#: `users` 新增的 6 列（设计 47 §2.6）。顺序 = 迁移里的追加顺序。
_USER_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    (
        "status",
        sa.Column("status", sa.String(length=16), nullable=False, server_default="normal"),
    ),
    (
        "real_name",
        sa.Column("real_name", sa.String(length=64), nullable=False, server_default=""),
    ),
    (
        "last_login_ip",
        sa.Column("last_login_ip", sa.String(length=64), nullable=False, server_default=""),
    ),
    #: **可空、无 server_default**：从未登录 = NULL（不造假）
    ("last_login_at", sa.Column("last_login_at", sa.DateTime(), nullable=True)),
    (
        "must_change_password",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            #: ⚠️ 布尔列的默认值必须 `false` 字面量：PG 上 `Boolean DEFAULT '0'`
            #: 直接报错，SQLite 上 `'false'` 会被存成字符串（读出恒真）。
            server_default=sa.text("false"),
        ),
    ),
    ("deleted_at", sa.Column("deleted_at", sa.DateTime(), nullable=True)),
)


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    #: ⚠️ 表不存在时 `get_columns` 抛 `NoSuchTableError`（不是返回空集）。
    #: 本迁移的清理段会探测 `api_logs` / `notifications` 的列，而测试里的**手写最小夹具**
    #: 只建了它自己关心的几张表（`test_initial_data` 的夹具只有 `users` + `auto_tasks`）。
    #: ⇒ 表不存在按「没有这些列」处理：那是**结构存在性**判据，不是「跳过数据」判据
    #: （DEF-048 形态是后者），在真库上恒为「表在」，不会静默不干活。
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def sqlite_users_rebuild_is_safe() -> bool:
    """`users` 能不能走 `batch_alter_table` 重建。

    ⚠️ batch 重建 = 「按**反射出的旧表**形状建新表 + 拷数据 → 换名」。**反射不到的列会永久
    丢失**。测试里的手写最小夹具常常只建 `users(id, username, …)` 几列
    （`test_initial_data` 的夹具甚至没有 `role`），一旦重建，后面的迁移读 `users.role`
    就 `no such column`（实测）。

    ⇒ 前置判据：`users` 必须带齐**后续迁移会读的列**。缺列说明这是**夹具**
    （真库上这些列一个不少）⇒ 跳过重建，只逐列 ADD COLUMN。
    这是**结构**判据（不是「有没有数据」判据）：真库上恒为真，不会静默不干活。
    """
    required = {"id", "username", "password_hash", "role", "token_version", "created_at"}
    return required <= _columns("users")


def _indexes(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return set()
    return {index["name"] for index in inspector.get_indexes(table)}


def _enable_foreign_keys() -> None:
    """SQLite 上显式打开外键（PG 上 `PRAGMA` 不存在，必须跳过）。"""
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def sqlite_fk_off() -> "contextlib.AbstractContextManager[None]":
    """SQLite 上**临时关掉外键检查**，退出时恢复。

    ⚠️ 为什么必须有它（2026-10-24 实测，真库副本上必现）：
    `op.batch_alter_table("users")` 在 SQLite 上的实现是
    「建 `_alembic_tmp_users` → 拷数据 → **DROP TABLE users** → 改名」。
    而 `users` 是**被约 50 张业务表外键引用**的父表
    （`notes` / `platform_accounts` / `system_settings` / `notifications` …）。
    一旦 `PRAGMA foreign_keys=ON`（本模块开头刚打开的），那条 `DROP TABLE users`
    直接 `FOREIGN KEY constraint failed` ⇒ **迁移 1 在真库上永远跑不过**。

    关掉检查做重建是 SQLite 官方文档给的做法（`PRAGMA foreign_keys` 只在
    **没有打开的事务**里才能切换，所以先 `commit()` 再切；重建后 `users.id`
    保持不变，引用它的行依然有效 —— 恢复检查后由 `PRAGMA foreign_key_check` 给判据）。

    ⚠️ 不要用 `exec_driver_sql("COMMIT")` 手写：SQLAlchemy 在**没有活动事务**时
    连发 COMMIT 会 `cannot commit - no transaction is active`（实测）。
    走 `bind.commit()`（等于 connection.commit()）由 SQLAlchemy 判事务状态。
    """

    @contextlib.contextmanager
    def _manager():
        bind = op.get_bind()
        if bind.dialect.name != "sqlite":
            yield
            return
        bind.commit()  # 事务内切 PRAGMA 无效
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            yield
        finally:
            bind.commit()
            bind.exec_driver_sql("PRAGMA foreign_keys=ON")

    return _manager()


def upgrade() -> None:
    _enable_foreign_keys()
    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())

    #: ⚠️ **先清掉「上游迁移已回滚、但本迁移的表还在」的残留**。
    #: 场景：`downgrade` 到 `c1d2e3f4a5b8`（迁移 1 回滚 = 删 roles/menus/role_menus）
    #: 之后**再 `upgrade`**：迁移 3 / 2 回滚时没删表（它们只删种子行 / 由自己的
    #: downgrade 删），所以 `dict_types` / `dict_items` / `api_logs.status_code` 还在。
    #: 不清掉就会撞两个错（实测）：
    #: ① `dict_items` 的外键 → `dict_types`，而 `dict_types` 已被迁移 1 的
    #:    `drop_table("roles")` 之前**不在**本迁移范围内（它属于迁移 2）——
    #:    真实报错是 `no such table: roles`（迁移 2 的 FK 解析）；
    #: ② `create_table` 遇到同名残留表。
    #: 判据只看**结构是否存在**（不是「有没有数据」），不属 DEF-048 形态。
    for stale in ("role_menus", "menus", "roles"):
        if stale in existing_tables:
            #: 表在 = 上一次回滚没清干净，先删掉再按本迁移的形状重建
            op.drop_table(stale)
    existing_tables -= {"role_menus", "menus", "roles"}

    #: ⚠️ 同型残留：迁移 2 的表 / 列。`downgrade` 到 `c1d2e3f4a5b8` 后**再 upgrade**
    #: 时它们还在（迁移 2 的 downgrade 只在回退到它时才跑）。而 `dict_items` 的外键
    #: 指向 `dict_types` —— 若先重建 `roles` 再被 `dict_*` 的 FK 解析牵连，
    #: 报的是 `no such table: roles`（实测）。⇒ **按反向依赖序**先清 `dict_items`。
    #: 这是「结构存在性」判据，不是「有没有数据」判据。
    for stale in ("dict_items", "dict_types"):
        if stale in existing_tables:
            op.drop_table(stale)
            existing_tables.discard(stale)
    if "status_code" in _columns("api_logs"):
        if "ix_api_logs_status_code" in _indexes("api_logs"):
            op.drop_index("ix_api_logs_status_code", table_name="api_logs")
        with op.batch_alter_table("api_logs") as batch:
            batch.drop_column("status_code")
    if "link" in _columns("notifications"):
        with op.batch_alter_table("notifications") as batch:
            batch.drop_column("link")

    # ---------------- roles ----------------
    if "roles" not in existing_tables:
        op.create_table(
            "roles",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("code", sa.String(length=32), nullable=False),
            sa.Column("description", sa.String(length=255), nullable=False, server_default=""),
            sa.Column(
                "is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")
            ),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="normal"),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint("status IN ('normal', 'disabled')", name="ck_roles_status"),
            #: 内置角色必须启用（停用内置角色 = 把一整类用户锁在门外）
            #: ⚠️ 与模型 `models/role.py` 同一处坑（2026-10-23 D1）：布尔列在 CHECK 里
            #: 必须用布尔语义（`NOT is_system`），原 `is_system = 0` 是 SQLite 写法，
            #: PG 报 `boolean = integer` ⇒ 建表整体失败。真实库走 create_all 绕过了
            #: 这句 DDL，全新安装（PG 空库跑全链）必踩 —— 已对齐模型的修法。
            sa.CheckConstraint(
                "NOT is_system OR status = 'normal'", name="ck_roles_system_stays_enabled"
            ),
        )
    if "ix_roles_code" not in _indexes("roles"):
        op.create_index("ix_roles_code", "roles", ["code"], unique=True)

    # ---------------- menus ----------------
    if "menus" not in existing_tables:
        op.create_table(
            "menus",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            #: `0` = 根；**不建外键**（见模块 docstring）
            sa.Column("parent_id", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("menu_type", sa.String(length=1), nullable=False, server_default="C"),
            sa.Column("path", sa.String(length=200), nullable=False, server_default=""),
            sa.Column("component", sa.String(length=200), nullable=False, server_default=""),
            sa.Column("perms", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("icon", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("workspace", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "is_visible", sa.Boolean(), nullable=False, server_default=sa.text("true")
            ),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="normal"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint("menu_type IN ('M', 'C', 'F')", name="ck_menus_menu_type"),
            sa.CheckConstraint("status IN ('normal', 'disabled')", name="ck_menus_status"),
        )
    menu_indexes = _indexes("menus")
    #: 两条**部分唯一索引**：`sqlite_where` + `postgresql_where` 必须都写，
    #: 只写一边的后果是「那个方言上约束根本不存在」且不报错（设计 58 §2.3 实测）。
    if "uq_menus_perms" not in menu_indexes:
        op.create_index(
            "uq_menus_perms",
            "menus",
            ["perms"],
            unique=True,
            sqlite_where=sa.text("perms IS NOT NULL AND perms <> ''"),
            postgresql_where=sa.text("perms IS NOT NULL AND perms <> ''"),
        )
    if "uq_menus_path" not in menu_indexes:
        op.create_index(
            "uq_menus_path",
            "menus",
            ["path"],
            unique=True,
            sqlite_where=sa.text("path IS NOT NULL AND path <> ''"),
            postgresql_where=sa.text("path IS NOT NULL AND path <> ''"),
        )
    if "ix_menus_parent" not in menu_indexes:
        op.create_index("ix_menus_parent", "menus", ["parent_id"], unique=False)
    if "ix_menus_workspace" not in menu_indexes:
        op.create_index("ix_menus_workspace", "menus", ["workspace"], unique=False)

    # ---------------- role_menus ----------------
    if "role_menus" not in existing_tables:
        op.create_table(
            "role_menus",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            #: 授权行归属用户（见 `models/role_menu.py` 的说明）
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "role_id",
                sa.Integer(),
                sa.ForeignKey("roles.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "menu_id",
                sa.Integer(),
                sa.ForeignKey("menus.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            #: ⚠️ 唯一键**必须含 `user_id`**：授权行是按用户归属的（见 `models/role_menu.py`），
            #: 而 `user` 角色可能被**多个用户**持有 —— 只写 `(role_id, menu_id)` 时，
            #: 给第二个 user 角色用户授权就会撞 `UNIQUE`（真库副本实测：26 个用户里
            #: 25 个是 user 角色 ⇒ 第一次播种直接 IntegrityError）。
            sa.UniqueConstraint(
                "user_id", "role_id", "menu_id", name="uq_role_menus_user_role_menu"
            ),
        )
    role_menu_indexes = _indexes("role_menus")
    if "ix_role_menus_menu" not in role_menu_indexes:
        op.create_index("ix_role_menus_menu", "role_menus", ["menu_id"], unique=False)
    if "ix_role_menus_user" not in role_menu_indexes:
        op.create_index("ix_role_menus_user", "role_menus", ["user_id"], unique=False)

    # ---------------- users + 6 列 ----------------
    #: ⚠️ SQLite 不支持带约束的 `ALTER`；`batch_alter_table` 两个方言都能跑
    #: （PG 上 SQLAlchemy 会降级为原生 ADD COLUMN）。所有列放**同一个 batch**
    #: ⇒ SQLite 只做一次「建新表 → 拷数据 → 换名」。
    users_columns = _columns("users")
    pending = [(name, col) for name, col in _USER_COLUMNS if name not in users_columns]
    if pending:
        #: ⚠️ 必须关外键检查（见 `sqlite_fk_off`）——`users` 是被约 50 张表引用的父表，
        #: batch 重建里的 `DROP TABLE users` 会在检查打开时直接失败。
        #: 夹具缺列时改为逐列 `ADD COLUMN`（见 `sqlite_users_rebuild_is_safe`）：
        #: 重建会让反射不到的列永久消失，后面的迁移就读不到 `users.role` 了。
        if sqlite_users_rebuild_is_safe():
            with sqlite_fk_off():
                with op.batch_alter_table("users") as batch:
                    for _name, column in pending:
                        batch.add_column(column)
        else:
            for _name, column in pending:
                op.add_column("users", column)
    if "ix_users_status" not in _indexes("users"):
        op.create_index("ix_users_status", "users", ["status"], unique=False)
    #: 重建后核对：`users.id` 未被重排 ⇒ 引用它的行依然有效。
    #: 判据**只看指向 `users` 的外键**（`table` 列 == 'users'）—— 全库
    #: `foreign_key_check` 会把**改动前就存在**的孤儿一起报出来
    #: （实测：`scheduled_job_logs` 有 3 行指向已删的 `scheduled_jobs`，
    #: 与本次迁移无关），那属于独立的数据整洁问题，不能让它挡住结构迁移。
    if pending:
        bind = op.get_bind()
        if bind.dialect.name == "sqlite" and sqlite_users_rebuild_is_safe():
            orphans = [
                row
                for row in bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                if str(row[2]) == "users"
            ]
            if orphans:
                raise RuntimeError(
                    f"users 重建后出现指向 users 的外键孤儿 {len(orphans)} 行：{orphans[:5]}"
                )


def downgrade() -> None:
    _enable_foreign_keys()
    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())

    #: ⚠️ 顺序是**判据**：先删引用别人的表（`role_menus` 指向 roles / menus）。
    if "role_menus" in existing_tables:
        op.drop_table("role_menus")

    users_columns = _columns("users")
    if "ix_users_status" in _indexes("users"):
        op.drop_index("ix_users_status", table_name="users")
    removable = [name for name, _ in _USER_COLUMNS if name in users_columns]
    if removable:
        #: 同 upgrade：batch 重建会 `DROP TABLE users`，必须先关外键检查。
        with sqlite_fk_off():
            with op.batch_alter_table("users") as batch:
                for name in reversed(removable):
                    batch.drop_column(name)

    if "menus" in existing_tables:
        op.drop_table("menus")
    if "roles" in existing_tables:
        op.drop_table("roles")
