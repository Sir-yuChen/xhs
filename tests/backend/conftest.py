"""后端测试的公共夹具 —— 消灭「每个文件各建一份 engine / TestClient」的样板。

## 为什么要有这个文件

改之前：**41 个后端测试文件、30836 行**里，有 **29 个文件各自 `create_engine` +
`create_all`**、**21 个文件各自 `TestClient(app)`**，`_override_database()` 这个
十来行的助手函数被抄了近 30 份（彼此只差一个数据库文件名）。代价不是跑得慢
（建表现在只占几十毫秒），而是**三件事**：

1. 想统一改点什么（换 SQLite 参数、加索引、换隔离级别）要动 30 处；
2. `app.dependency_overrides` 是**全局**的，各文件写法不一，有的 `pop` 有的
   不 `pop` —— 不 pop 的那个会把覆盖泄漏给同会话里后面的用例（xdist 下更明显）；
3. 新写的测试文件继续抄样板，重复代码只增不减。

## 使用方式（新测试一律这样写）

```python
def test_something(api_client, db_factory):
    api_client.post("/api/auth/register", json={...})   # 已连到测试库
    db = db_factory()
    row = db.scalar(select(Note).where(...))
```

## 硬约束

* **惰性 import**：本模块在 import 期**不**引入 `backend.app.main`，只有 fixture
  第一次被请求时才引入（理由见 `tests/conftest.py` 的说明）。
* **yield + 清理**：`api_client` 退出时**必定** `pop` 掉自己加的 override，
  不让覆盖跨用例泄漏 —— 这正是旧样板最容易出错的地方。
* **不抢既有文件的活**：本文件是**新增**的，没有改动任何既有测试文件。既有
  文件仍用各自的 `_override_database()`；把它们迁到本夹具是**独立的后续工作**
  （见 `docs/04_测试文档/05_测试规范.md` 的迁移清单），迁移一个验一个，
  不搞一次性大爆炸。
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

#: 仓库根（`tests/backend/conftest.py` → 上两级）。
ROOT = Path(__file__).resolve().parents[2]


def _base():
    """惰性取 `Base`（首次调用才 import models，之后走模块缓存）。"""
    from backend.app.core.database import Base  # noqa: PLC0415

    return Base


def make_engine(db_path: Path | str) -> Engine:
    """建一个「与生产同形」的 SQLite engine。

    `check_same_thread=False` 是必需的：`TestClient` 在另一线程里跑请求，
    而夹具线程也可能同时开会话。
    """
    return create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """本用例独享的 SQLite 文件路径（pytest 已按用例名隔离，xdist 下各 worker 还有独立 basetemp）。"""
    return tmp_path / "test.db"


@pytest.fixture()
def db_engine(db_path: Path) -> Engine:
    """建好全部表的 engine。"""
    engine = make_engine(db_path)
    _base().metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_factory(db_engine: Engine) -> Callable[[], Session]:
    """Session 工厂：`db = db_factory()`，用完自己 `db.close()`。

    返回工厂而不是单个 Session，是因为大量用例要在请求**之后**再查一次库
    （断言落库结果）—— 那时请求线程的 Session 已经关了。
    """
    factory = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)
    return factory


@pytest.fixture()
def db_session(db_factory: Callable[[], Session]) -> Iterator[Session]:
    """单个 Session（不需要「请求后再查」的用例用它更短）。"""
    session = db_factory()
    yield session
    session.close()


@pytest.fixture()
def api_client(db_engine: Engine):
    """连到测试库的 `TestClient(app)`；退出时清掉自己加的 override。

    ⚠️ 退出时**必定** `pop`：`app.dependency_overrides` 是模块级全局，
    旧样板的 `_override_database()` 只写不清，会把库指向泄漏给同会话后面的用例。
    """
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from backend.app.core.database import get_db  # noqa: PLC0415
    from backend.app.main import app  # noqa: PLC0415

    factory = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)

    def override_get_db() -> Any:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        # 只摘自己放进去的那一个；别人的 override（用例自己装的适配器桩）不碰。
        if app.dependency_overrides.get(get_db) is override_get_db:
            del app.dependency_overrides[get_db]


@pytest.fixture()
def client():
    """**不**改数据库的 `TestClient(app)`。

    只给「纯结构 / 纯 openapi / 纯静态源码」类用例用；要落库的用例一律用
    `api_client`，否则会打到真实库上。
    """
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from backend.app.main import app  # noqa: PLC0415

    return TestClient(app)


@pytest.fixture()
def frontend_runner() -> Callable[[str, dict[str, str] | None], Any]:
    """跑 `tests/frontend/*.cjs` 行为脚本的 runner。

    ```python
    result = frontend_runner("add_account_modal.cjs")
    assert result.returncode == 0, result.stdout
    ```

    把「node 从哪来、cwd 设成什么、环境怎么传」收在一处 —— 这三个细节在
    `test_login_state.py` / `test_scheduled_jobs.py` 等文件里各写了一遍。
    """
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    def _run(script: str, env: dict[str, str] | None = None) -> Any:
        return subprocess.run(
            ["node", str(ROOT / "tests" / "frontend" / script)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env={**os.environ, **(env or {})},
        )

    return _run
