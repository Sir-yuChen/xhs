"""公共夹具的自证 + 使用范例。

**这个文件本身就是「新测试该怎么写」的样板** —— 照它抄就行。

存在的理由有两个：
1. **证明 conftest 真的能用**（夹具写错只有在被请求时才暴露，尤其是
   `yield` 清理那段 —— `app.dependency_overrides` 是全局的，泄漏了不会报错，
   只会让**后面**的用例莫名其妙连到错的库）；
2. **给后续改动留一个回归点**：谁改了 `tests/backend/conftest.py`，这里会红。
"""
from __future__ import annotations

from sqlalchemy import select

from backend.app.models import User


def test_api_client_is_wired_to_the_test_database_not_the_real_one(api_client, db_factory):
    """落库要走 api_client；且必须落在**测试库**里（不是 data/spider_xhs.db）。"""
    response = api_client.post(
        "/api/auth/register",
        json={"username": "conftest-demo", "password": "secret123"},
    )
    assert response.status_code == 200, response.text

    # 请求之后再查库 ⇒ 工厂必须给一个**新的** Session（请求那个已经关了）。
    db = db_factory()
    try:
        user = db.scalar(select(User).where(User.username == "conftest-demo"))
        assert user is not None, "register 落了 200 但库里查不到 ⇒ override 没生效"
        assert user.id is not None
    finally:
        db.close()


def test_override_does_not_leak_to_the_next_test(client):
    """上一个用例装的 get_db override 必须已经被摘掉。

    这是旧样板最容易出的错：`_override_database()` 只写不清，覆盖会留在
    `app.dependency_overrides` 里，让同会话后面的用例连到**已关闭的** engine。
    """
    from backend.app.core.database import get_db
    from backend.app.main import app

    assert get_db not in app.dependency_overrides, (
        "上一个用例的 override 泄漏了 —— conftest 的 finally 清理没生效"
    )


def test_db_engine_creates_every_table(db_engine):
    """夹具建的表要覆盖模型全集（缺一张表会让用例以诡异的 IntegrityError 失败）。"""
    from backend.app.core.database import Base

    tables = set(db_engine.dialect.get_table_names(db_engine.connect()))
    expected = set(Base.metadata.tables)
    missing = expected - tables
    assert not missing, f"夹具漏建表: {sorted(missing)}"


def test_frontend_runner_reaches_a_real_script(frontend_runner):
    """runner 能找到脚本并跑通（node 缺失 / cwd 错都会在这里暴露）。"""
    result = frontend_runner("detail_message.cjs")
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"


def test_db_session_fixture_is_usable(db_session):
    """单 Session 夹具：不需要「请求后再查」的用例用它更短。"""
    count = db_session.query(User).count()
    assert count == 0, "新库应当是空的"
