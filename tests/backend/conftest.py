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
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

#: 测试库（与生产同构：PostgreSQL。SQLite 已退役，测试**不得**再用）
TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:123456@127.0.0.1:5432/spider_xhs_test",
)
ADMIN_URL = os.environ.get(
    "TEST_DATABASE_SERVER_URL",
    "postgresql+psycopg://postgres:123456@127.0.0.1:5432/postgres",
)
#: 测试专用 schema（与生产 `public` 隔离）
TEST_SCHEMA = "test"

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
        #: 只在 schema 为空时建表（避免每次跑都 drop/create 63 张表，~7s）
        if not _tables_exist(_engine):
            Base.metadata.create_all(bind=_engine)
    return _engine


def _tables_exist(engine: Engine) -> bool:
    """`test` schema 里是否已有全部表（有就跳过建表，省掉每次 7 秒）。"""
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
    return expected.issubset(existing)


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
def db(engine: Engine) -> Iterator[Session]:
    """用例用的 Session（与 `client` 共用同一个 engine/schema）。"""
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        _truncate_all(engine)


@pytest.fixture()
def client(engine: Engine) -> Iterator:
    """TestClient，端点写进 `test` schema。"""
    from fastapi.testclient import TestClient

    from backend.app.core.database import get_db
    from backend.app.main import app

    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()

    def _override():
        yield session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        session.close()
        _truncate_all(engine)
