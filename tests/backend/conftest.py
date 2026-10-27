"""后端测试基座（2026-10-26 重建，遵循 `docs/04_测试文档/12_功能点测试规范.md`）。

## 设计要点

1. **一个 engine**，`connect_args` 钉死 `search_path=test`；
2. **表建在 `test` schema**，与生产 `public` 隔离；
3. **服务层的 `SessionLocal` 自动指向它** —— 因为所有服务模块都是
   `from backend.app.core.database import SessionLocal`，只改**源头一处**即可；
4. 用例之间靠 **`TRUNCATE`（带 schema 前缀）** 隔离。

## 为什么不像旧基座那样遍历模块打桩

旧基座用 `pkgutil.walk_packages` 把 `backend.app` 下 **150+ 模块全部 import 一遍**
去替换 `SessionLocal` —— 实测 **单次启动 20~40 秒**，而实际只有
`core.database` 一处定义，其余全是重新导出。改源头一处即可，**快 20 倍**。

## 用法

```python
def test_create_order(client, db):
    r = client.post("/api/orders", json={...})       # 写在 test schema
    assert r.status_code == 200
    assert db.query(Order).count() == 1              # 立刻查得到
    # 用例结束自动清库
```
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

#: 测试进程标识：`auth._register_lenient()` 靠它认「正在跑测试」。
os.environ.setdefault("PYTEST_CURRENT_TEST", "conftest-collection")

#: 跳过 alembic 迁移链（`core/database.py::_run_alembic_migrations` 认这个开关）。
#: 表由 `_get_engine()` 的 `create_all` 建，跑 95 个迁移只是等价地建表，慢 20 倍。
os.environ.setdefault("HERMES_SKIP_ALEMBIC_IN_TESTS", "1")

#: 测试库（与生产同构：PostgreSQL。SQLite 已退役，测试**不得**再用）
TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:123456@127.0.0.1:5432/spider_xhs_test",
)
ADMIN_URL = os.environ.get(
    "TEST_DATABASE_SERVER_URL",
    "postgresql+psycopg://postgres:123456@127.0.0.1:5432/postgres",
)
#: 测试专用 schema 前缀（与生产 `public` 隔离）。
#:
#: 🔴 **每个 xdist worker 一个独立 schema**（2026-10-27 实测）：
#: `pytest.ini` 默认 `-n 4`，4 个 worker 共用同一个 `test` schema 时会互相踩：
#: 一个 worker 在 `_truncate_all()` 清表，另一个 worker 的用例数据当场消失
#: （表现为 `REGISTER_DISABLED` / 计数 0 这类「时好时坏」的假红）；
#: 「先查再写」的幂等也会在两个 worker 同时查不到时变竞态。
#: 一个 worker 一个 schema ⇒ 进程内串行、进程间零共享，与 `-n0` 行为完全一致。
#: ⚠️ 串行（`-n0`）时 `PYTEST_XDIST_WORKER` 未设 ⇒ 前缀为空 ⇒ schema 仍是 `test`
#: （与历史口径、手工排查习惯兼容）。
TEST_SCHEMA_PREFIX = f"test_{os.environ['PYTEST_XDIST_WORKER']}" if os.environ.get(
    "PYTEST_XDIST_WORKER"
) else "test"
#: 兼容旧名（既有文档 / 手工 SQL 里引用的是 `test`）。
TEST_SCHEMA = TEST_SCHEMA_PREFIX

_engine: Engine | None = None


def _ensure_database() -> None:
    """确保测试库存在（不存在就建；**不建表**）。"""
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            if not conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = 'spider_xhs_test'")
            ).scalar():
                conn.execute(text('CREATE DATABASE "spider_xhs_test"'))
    finally:
        admin.dispose()


def _get_engine() -> Engine:
    """会话唯一 engine；表建在 `test` schema。

    🔴 `connect_args` 的 `search_path` 是整套基座的关键：
    服务层的 `SessionLocal` 用的是**同一个 engine**，新开的连接也落在 `test`
    ⇒ 端点与用例看到同一份数据。
    """
    global _engine
    if _engine is None:
        import backend.app.models  # noqa: F401  注册全部模型
        from backend.app.core.database import Base

        _ensure_database()
        _engine = create_engine(
            TEST_DB_URL,
            pool_pre_ping=True,
            connect_args={"options": f"-csearch_path={TEST_SCHEMA},public"},
        )
        with _engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{TEST_SCHEMA}"'))
        #: 表与列都和模型一致才跳过重建（避免每次跑都 drop/create 63 张表，~7s）。
        #: ⚠️ 不一致时必须先 **drop 整个 schema** 再建 —— `create_all` 只建**不存在**的表，
        #: 光调它补不上缺失的列（加了列之后旧表照样"存在"，见 `_tables_exist` 的说明）。
        if not _tables_exist(_engine):
            with _engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE'))
                conn.execute(text(f'CREATE SCHEMA "{TEST_SCHEMA}"'))
            Base.metadata.create_all(bind=_engine)
    return _engine


def _tables_exist(engine: Engine) -> bool:
    """`test` schema 里的表**和列**是否都与模型一致（一致就跳过建表，省掉每次 7 秒）。

    ⚠️ 只比对**表名**是不够的（2026-10-27 实测踩过）：`create_all` 只建**不存在**的表，
    已有的表**不会补列**。于是模型加了一列之后，旧 schema 里那张表照样"存在" ⇒
    跳过建表 ⇒ 所有用例在第一条 INSERT 上炸
    `UndefinedColumn: 关系 "platform_accounts" 的 "disabled" 字段不存在`，
    一看就是十几条红，而根因只是测试库的结构没跟上模型。

    修法：把**列**也纳入判据。缺任何一列 ⇒ 判定「不一致」⇒ 走下面的重建分支。
    """
    from backend.app.core.database import Base

    with engine.connect() as conn:
        existing = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = :s"
                ),
                {"s": TEST_SCHEMA},
            ).fetchall()
        }
        expected = {t.name for t in Base.metadata.sorted_tables}
        if not expected.issubset(existing):
            return False
        #: 列级比对：按 (表名, 列名) 拉一次，与模型一次性对完。
        actual_columns = {
            (r[0], r[1])
            for r in conn.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = :s"
                ),
                {"s": TEST_SCHEMA},
            ).fetchall()
        }
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if (table.name, column.name) not in actual_columns:
                logger_ = __import__("logging").getLogger(__name__)
                logger_.warning(
                    "test schema 缺列 %s.%s ⇒ 重建测试表（模型已改、库没跟上）",
                    table.name,
                    column.name,
                )
                return False
    return True


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """会话唯一 engine（表全在 `test` schema）。"""
    eng = _get_engine()
    yield eng


@pytest.fixture(scope="session", autouse=True)
def _bind_service_sessions(engine: Engine) -> Iterator[None]:
    """把 `core.database.SessionLocal` 指向测试 engine。

    ⚠️ 只改一处：`services/` 下所有模块都是
    `from backend.app.core.database import SessionLocal`（import 时快照同一个对象），
    改源头后它们**全部**自动生效 —— 不需要遍历模块（旧基座就是那样慢 20 倍的）。
    """
    from backend.app.core import database as db_module

    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    original = db_module.SessionLocal
    db_module.SessionLocal = factory
    try:
        yield
    finally:
        db_module.SessionLocal = original


def _truncate_all(engine: Engine) -> None:
    """清空 `test` schema 全部表（结构保留）。用例隔离靠它。

    ⚠️ **必须带 schema 前缀**：`TRUNCATE "users"` 走连接的 `search_path`，
    服务层新开的连接若 `search_path` 已变会**静默**清 `public`（空表）且不报错。
    """
    from backend.app.core.database import Base

    names = [t.name for t in reversed(Base.metadata.sorted_tables)]
    if not names:
        return
    quoted = ", ".join(f'"{TEST_SCHEMA}"."{n}"' for n in names)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))


@pytest.fixture()
def conn(engine: Engine) -> Iterator[Connection]:
    """用例内**唯一一条连接** —— `db` 与 `client` 都从它建会话。

    ## 为什么必须是「同一条连接」而不是「同一个 engine」

    2026-10-27 实测卡点：`db` 与 `client` 各开一条连接时，`db` 在点 `client.post`
    之前就已经开始了事务（读路径落快照）；端点在**另一条连接**上 commit 之后，
    `db` 仍看到自己事务开始时的旧快照 ⇒ 计数恒 0。SQLAlchemy 的会话之间
    互不可见，这不是「同一个 engine」能解决的。

    ## 用例隔离靠「外层事务 + 收尾回滚」，**不靠 TRUNCATE**（2026-10-27 修）

    原实现每条用例结束时 `TRUNCATE` 60+ 张表，与端点会话争 `AccessExclusiveLock`
    ⇒ 实测 `DeadlockDetected`，整批用例连锁失败（一个 worker 里 15 failed / 2 errors
    全部由死锁引起，与被测逻辑无关）。

    现在就一条连接、一个外层事务，结束时 `rollback()` —— 用例之间零残留，
    且**一条 `TRUNCATE` 都不发**（DDL 锁彻底消失）。
    """
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture()
def db(conn: Connection) -> Iterator[Session]:
    """用例用的 Session（与 `client` 共用同一条连接 ⇒ 端点的提交立刻可见）。

    ⚠️ `join_transaction_mode="create_savepoint"`：端点的 `commit()` 只提交到
    **SAVEPOINT**，不会把外层事务一起提交掉 —— 所以 `conn` 收尾的 `rollback()`
    仍然能把整条用例的数据全部抹掉（这正是「不用 TRUNCATE 也能隔离」的关键）。
    """
    session = Session(
        bind=conn,
        autocommit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(conn: Connection) -> Iterator:
    """TestClient，端点写进 `test` schema，且与 `db` 共用同一条连接。

    ## 卡点 B 的修法：lifespan 必须打桩（2026-10-27 实测）

    `with TestClient(app)` 会**真跑 `lifespan`**（`backend/app/main.py`）：
    `init_db()` 跑 alembic 迁移、`seed_all_builtin_data()` 写库、
    `acquire_lease_and_start_scheduler()` 起调度器、
    `start_all_watchdogs()` 起 5 条常驻看门狗、`recover_and_wake_workers()` 起 6 个 worker
    ⇒ 进程里留下一批**永不退出**的线程，pytest 退出时被它们挂住
    （实测单条用例跑到 90s 仍未结束）。表由 `_get_engine()` 的 `create_all` 负责建，
    种子 / 调度器 / worker 与本文件无关 ⇒ 整段换成空壳最稳。

    ⚠️ 端点用**自己的 Session**（与 `db` 分开）但绑在**同一条连接**上：
    分开是为了端点的 `commit()` 不把用例的断言一起提交掉；
    同连接是为了 PG 里已提交的数据对 `db` 立即可见。
    """
    from contextlib import asynccontextmanager

    from fastapi.testclient import TestClient

    from backend.app import main as main_module
    from backend.app.core.database import get_db

    def _override():
        #: ⚠️ 必须 `create_savepoint` —— 端点的 `commit()` 只提交到 SAVEPOINT，
        #: 外层事务仍由 `conn` 收尾回滚 ⇒ 用例数据不留到下一个用例。
        #: 不加这一项，端点的 `commit()` 会把外层事务一起提交掉，`conn.rollback()`
        #: 就回滚不了任何东西，「用例之间隔离」当场失效。
        endpoint_session = Session(
            bind=conn,
            autocommit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield endpoint_session
        finally:
            endpoint_session.close()

    @asynccontextmanager
    async def _no_lifespan(app):  # noqa: ARG001 - 形状必须与真实 lifespan 一致
        """空壳 lifespan：一个启动步骤都不跑（原因见 docstring）。"""
        yield

    app = main_module.app
    previous_lifespan = app.router.lifespan_context
    previous_overrides = dict(app.dependency_overrides)
    app.router.lifespan_context = _no_lifespan
    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.router.lifespan_context = previous_lifespan
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


@pytest.fixture(autouse=True)
def _open_register_switch(db: Session) -> Iterator[None]:
    """把 `register.enabled` 打开（系统级行）—— 造用户的用例靠它。

    `POST /api/auth/register` 在开关关闭时返回 **403 REGISTER_DISABLED**
    （v1.1.0 基线默认 false）。旧基座靠真 lifespan 的 `seed_all_builtin_data()`
    播种，而本基座把 lifespan 打桩掉了 ⇒ 库里没有那一行 ⇒ 一律 403。
    这里显式写一行，与 `live_global_bool` 读的 `_system_row`
    （`user_id IS NULL ∧ workspace IS NULL`）同形状。
    """
    from sqlalchemy import select

    from backend.app.models.system_setting import VALUE_TYPE_BOOL, SystemSetting

    #: ⚠️ 必须**幂等**：表只在 schema 为空时建一次，但上一次进程崩溃/中断会在库里
    #: 留下这一行；直接 `add()` 会撞 `uq_system_settings_system_key`（实测报过）。
    existing = db.scalar(
        select(SystemSetting).where(
            SystemSetting.user_id.is_(None),
            SystemSetting.workspace.is_(None),
            SystemSetting.key == "register.enabled",
        )
    )
    if existing is None:
        db.add(
            SystemSetting(
                user_id=None,
                workspace=None,
                key="register.enabled",
                value="true",
                value_type=VALUE_TYPE_BOOL,
                category="feature",
                label="开放注册",
            )
        )
    else:
        existing.value = "true"
        existing.value_type = VALUE_TYPE_BOOL
    db.commit()
    yield
