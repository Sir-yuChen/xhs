"""后端测试的公共夹具 —— 消灭「每个文件各建一份 engine / TestClient」的样板。

## ⚠️ 模块级 `SessionLocal` 必须打桩（2026-09-27 立规）

`get_db` **不是**唯一的数据库入口：`services/` 里 12 个模块各自
`from backend.app.core.database import SessionLocal`（模块属性，绑在自己的名字空间里），
服务函数自开 Session 时用的是**它**。`api_client` 只覆盖 FastAPI 依赖 ⇒ 一旦端点链路
走到服务层，请求就**落到生产库**上。

代价已经真实发生过两次（2026-09-27 本日两回）：账号卡同步端点改造后链路变长
（`scheduled_job_runner` → `scheduler_service` → `note_sync_service`），测试只打桩了
自己知道的那一个模块，于是**用户真实库**里多了一行 `running` 的僵尸任务 + 7 条假定时日志。

对策是**两条一起上**（缺任一条都会复发）：

1. `patch_service_sessions(monkeypatch, factory)` —— 打桩清单由 **AST 扫源码**得出
   （`service_session_modules()`），不是人肉维护的名单 ⇒ 新增服务模块自动被覆盖；
2. `api_client` **默认**打桩（见夹具 docstring）⇒ 新写的端点测试不用记得这件事。

守卫见 `test_testing_spec_guards.py::test_service_session_registry_covers_every_session_owner`。

**第三次（2026-09-28）—— 形态不同，上面两条对策都挡不住**：`test_scheduler_sync.py` 里那个
「进真 `lifespan`」的用例**不用 `api_client`**（它手动 `monkeypatch` 自己关心的那几个依赖）
⇒ 第 2 条对策覆盖不到它。它漏桩了 `lifespan` 里的两个**播种**步骤
（`seed_builtin_jobs_from_initial_data` / `seed_builtin_settings_from_initial_data`）以及
`recover_account_tasks()`（自开 Session 写库）与 `ensure_account_task_worker()` /
`start_account_task_watchdog()`（起常驻线程去认领真实数据）。此前它们一直是 no-op
（内置行都在）⇒ 看不出问题；本轮**新增一个运行时配置键**后播种第一次真的写
⇒ **全量回归把 6 行写进了 `data/spider_xhs.db`**（18:08:47，6 个用户各一行）。

⇒ 本轮补的判据是**结构性的**：`test_scheduler_sync.py::test_lifespan_steps_are_all_stubbed_by_this_module`
用 AST 取 `lifespan` 的**裸名调用集合**，断言 ⊆ 该用例的桩清单（桩清单同样从它的源码 AST 取，
不在守卫里另抄一份 —— 两份名单必然漂移）⇒ `lifespan` 新增任何步骤而没同步桩就红。
**凡「手动进 `lifespan` / 手动 `create_engine`」的用例都要有同款守卫**（`api_client` 的
自动打桩管不到它们）。


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

import os
import tempfile

#: ⚠️ v1.1.0（2026-10-24）用户口径「完全放弃 SQLite」：原来这里有个
#: `HERMES_ALLOW_SQLITE_FOR_TESTS=1` 逃逸口，让 conftest 的 SQLite 临时库能跑。
#: **已删除** —— 测试库也必须是 PG（见 `make_engine` 的说明）。


from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

#: ⚠️ **测试环境跳过 alembic 迁移链，直接 `create_all`**（2026-10-23 立，工程化提速）。
#:
#: 实测依据：`TestClient(app)` 进 `lifespan` → `init_db()` → `_run_alembic_migrations()`，
#: 空库走 `upgrade head` = **跑完全部 95 个迁移**。而测试库**每次都是空库**
#: ⇒ 95 个迁移做的工作就是「建表」，`create_all` 建出**完全相同的结构**只要 0.76 秒。
#: 代价实测：单条用例 17.2 秒里 **16.6 秒**花在 `alembic upgrade` 上
#: （cProfile: `sqlite3.Cursor.execute` 506 次 / 16.559s；SQLAlchemy 事件测到真正
#: 的业务 SQL 只有 0.03 秒 —— 差额全在迁移）。
#:
#: 迁移链的执行正确性属一次性验收（2026-10-24 用户口径），已用空库全链探针
#: 验证并移除迁移执行类测试（见台账 59 §十）。这里跳过的只是
#: 「每条用例为了建表而顺路重跑一遍迁移」这层浪费。
#:
#: 必须在 import `backend.app.core.database` **之前**设好 ⇒ 放模块级。
os.environ.setdefault("HERMES_SKIP_ALEMBIC_IN_TESTS", "1")

#: 仓库根（`tests/backend/conftest.py` → 上两级）。
ROOT = Path(__file__).resolve().parents[2]


def _base():
    """惰性取 `Base`（首次调用才 import models，之后走模块缓存）。

    ⚠️ **必须先 import `backend.app.models`**（2026-10-04 修）：`Base.metadata`
    只登记**已被 import 过的**模型类，而 `core/database.py` 只在 `init_db()` 里
    import 它们（那是生产路径，测试不走）。此前只取 `Base` ⇒ 若本进程**还没有**
    任何用例触发过 `backend.app.main`（它会传递地 import models），
    `create_all` 建出的是**空库**。
    表现是**顺序依赖的假红**：同一文件里先跑的用例（用了 `api_client`）把 models
    带进来，后面的就正常；单独跑 `-n0` 或恰好排在前面时 `no such table: users`。
    （实测：`test_m9_governance.py` 单独跑时命中。）

    import 只登记元数据、**不播种任何行** ⇒ `test_conftest_fixtures` 的
    「新库是空的」契约不受影响。
    """
    import backend.app.models  # noqa: F401, PLC0415 - 登记全部表到 metadata

    from backend.app.core.database import Base  # noqa: PLC0415

    return Base


#: 测试库的连接串（**PostgreSQL**，v1.1.0 / 2026-10-24 用户口径「完全放弃 SQLite」）。
#:
#: 复用一个**独立测试库**，靠「每用例一个 schema」做隔离：
#: * 原 SQLite 方案是「每用例一个 `.db` 文件」，天然隔离；
#: * PG 没有「文件」概念 ⇒ 等价物是 `CREATE SCHEMA test_xxx` + `search_path`
#:   指向它，用例结束 `DROP SCHEMA ... CASCADE`。
#:
#: ⚠️ 为什么不用 transaction-rollback 隔离（更快）：项目里有大量「服务函数自开
#: Session」（`from ...database import SessionLocal`，见本文件顶部的打桩说明），
#: 它们在**另一个连接**上 commit，回滚外层事务看不到也管不住 ⇒ 脏数据会漏到
#: 下一条用例。schema 隔离对「多连接」也成立，这才是与 SQLite 文件等价的语义。
TEST_PG_ADMIN_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:123456@127.0.0.1:5432/spider_xhs_test",
)
#: 若测试库不存在，用生产库的凭据建出来（首次跑测试时自动建，省一次手工操作）。
#: ⚠️ 只建库，不建表 —— 表由每条用例的 `Base.metadata.create_all` 在**自己的 schema** 里建。
TEST_PG_SERVER_URL = os.environ.get(
    "TEST_DATABASE_SERVER_URL",
    "postgresql+psycopg://postgres:123456@127.0.0.1:5432/postgres",
)

_SCHEMA_COUNTER = {"n": 0}
#: `id(engine) -> schema`。⚠️ 不能用 `engine.info` —— SQLAlchemy 的 `Engine` 没有
#: 这个属性（那是 `Connection` 的）。也不用 `id()` 直接判等：engine 被 GC 后
#: id 可能被复用 ⇒ 用 `weakref` 让条目随 engine 一起消失。
_SCHEMA_BY_ENGINE: "weakref.WeakKeyDictionary[Any, str]" = __import__("weakref").WeakKeyDictionary()


def ensure_test_database() -> None:
    """确保 `spider_xhs_test` 库存在（不存在就建一个空的）。

    ⚠️ 连 `postgres` 库来建，不能连目标库自己。`CREATE DATABASE` 不能进事务块
    ⇒ 必须用 AUTOCOMMIT 隔离级别，否则 PG 报
    `CREATE DATABASE cannot run inside a transaction block`。
    """
    from sqlalchemy import create_engine, text  # noqa: PLC0415

    admin = create_engine(TEST_PG_SERVER_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = 'spider_xhs_test'")
            ).first()
            if not exists:
                try:
                    conn.execute(text("CREATE DATABASE spider_xhs_test"))
                except Exception:  # noqa: BLE001 - `pytest -n auto` 下多 worker 并发首跑
                    #: 两个 worker 同时发现库不存在、同时 CREATE ⇒ 后到的撞
                    #: duplicate database。库已经在了就是目的达成，吞掉。
                    if conn.execute(
                        text("SELECT 1 FROM pg_database WHERE datname = 'spider_xhs_test'")
                    ).first() is None:
                        raise
    finally:
        admin.dispose()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """占位路径 —— **PG 下无实际用途**（v1.1.0 SQLite 退役）。

    原来返回「本用例独享的 SQLite 文件路径」。现在 `make_engine` 用 **schema 隔离**，
    不需要文件；保留这个夹具只为 `db_engine` 的签名不变
    （避免改动 200+ 条用例的签名）。
    """
    return tmp_path / "test.db"


def make_engine(db_path: Path | str | None = None) -> Engine:
    """建一个「**每用例独享 schema**」的 PostgreSQL engine。

    ## 为什么参数还叫 `db_path`

    历史遗留：原实现是 SQLite 文件路径。现在参数**被忽略**（PG 没有文件），
    保留它只为不破坏既有调用方（`db_engine` 夹具仍传 `tmp_path`）。
    ⚠️ 新代码别依赖它 —— 想换库用 `TEST_DATABASE_URL` 环境变量。

    ## 隔离方式

    `CREATE SCHEMA <唯一名>` + `connect_args={"options": "-csearch_path=<名>"}`
    ⇒ 这个 engine 上的一切 `CREATE TABLE` / `INSERT` 都落在该 schema 里，
    与其它用例、与真实库的 `public` schema **完全隔离**。
    用例结束由 `drop_schema()` 清理（`CASCADE` 连表带数据一起删）。

    ## 为什么 schema 名要带进程号 + 自增

    `pytest -n auto` 下多个 worker 并发跑，`tmp_path` 已按 worker 隔离；
    schema 名额外带 PID 前缀，避免「上一轮异常退出残留同名 schema」撞车。
    """
    from sqlalchemy import create_engine, text  # noqa: PLC0415

    ensure_test_database()

    _SCHEMA_COUNTER["n"] += 1
    schema = f"t_{os.getpid()}_{_SCHEMA_COUNTER['n']}"

    engine = create_engine(
        TEST_PG_ADMIN_URL,
        #: `search_path` 只设这个 engine 的连接 ⇒ 不影响同进程里别的 engine。
        connect_args={"options": f"-csearch_path={schema},public"},
        pool_pre_ping=True,
    )
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))

    #: 登记到模块级表，供 `drop_schema()` 回收（fixture teardown 用）。
    _SCHEMA_BY_ENGINE[engine] = schema
    return engine


def drop_schema(bind: Engine) -> None:
    """删掉本用例的 schema（`CASCADE`：表、序列、索引一起走）。"""
    from sqlalchemy import text  # noqa: PLC0415

    schema = _SCHEMA_BY_ENGINE.get(bind)
    if not schema:
        return
    try:
        with bind.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    except Exception:  # noqa: BLE001 - 清理失败不该让用例失败
        pass
    finally:
        _SCHEMA_BY_ENGINE.pop(bind, None)


# ---------------------------------------------------------------------------
# 迁移测试基建已移除（2026-10-24 用户口径）
# ---------------------------------------------------------------------------
# 「迁移等一次性的工作，只需要保持迁移当前的正确即可，测试验收通过后，测试移除，
#  无需保留，也就无需全量回归。」—— 迁移链 PG 兼容的一次性验收 =
# 空库 `alembic upgrade head` 全链探针（11 轮收敛到 head，证据见台账 59 §十.2）；
# 九个迁移执行类测试与 `run_alembic` / `migration_db` / `LATER_MIGRATION_TABLES`
# 等专属基建已随验收通过一并删除。`core/migration_introspect.py` 是**运行时**
# 迁移代码的公共助手，保留（迁移本体仍在用它）。
















# ---------------------------------------------------------------------------
# 测试禁止真实外连（2026-10-23 立规，工程化基础设施）
# ---------------------------------------------------------------------------
# 实测代价：`test_llm_gateway.py` 里 `base_url="http://127.0.0.1:3000"` 而本机
# 3000 端口没人监听 ⇒ 每条用例真实 SYN 两次、各等约 2 秒 ⇒ **55 条用例 4m19s**，
# 全量回归因此被拖到 **116 分钟**（历史基线 150 秒）。
#
# 这比慢更糟：**测试通过与否取决于跑测试的机器上有没有起 3000 端口**。
# 在有网关的机器上「上游可达」走一条分支，在没有的机器上走异常分支 —— 同一份代码
# 两种结论。测试必须是**确定的**。
#
# 口径：**默认禁掉一切非回环外连**。需要「上游不可达」语义的用例，显式用
# `unreachable_upstream()` 打桩（立即 ConnectionRefused，不耗时）；
# 确实要连外网的（如真实抓包脚本）显式 `@pytest.mark.allow_network` 或调用
# `allow_network(monkeypatch)` 豁免。

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}

#: 测试里**唯一**允许真实回环连接的场景：`TestClient` 对自己进程的内置 ASGI
#: 服务发请求（httpx → `TestClientTransport`，走的是内存 transport，但个别路径
#: 会落到 socket）。其余回环连接一律禁止 —— 实测 Windows 上连「没在监听的
#: 127.0.0.1:3000」**要等 2.04 秒**（SYN 重试），不是立即 RST。
_ALLOW_LOOPBACK_ENV = "HERMES_TEST_ALLOW_LOOPBACK"


def _host_is_loopback(host: Any) -> bool:
    try:
        return str(host) in _LOOPBACK
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="session", autouse=True)
def register_switch_open_for_tests():
    """**session 级 autouse**：测试进程内始终允许注册（v1.1.0 S1 起）。

    ## 为什么必须是全局而不是只在 `api_client` 里

    产品基线是 `register.enabled=false`（防「出厂弱口令 + 任何人可注册」，设计 46 P11）。
    但全仓有 **十几个测试文件**在模块级建 `client = TestClient(app)`（`test_api.py` /
    `test_login_state.py` / `test_note_sync.py` …），它们**不走 `api_client` 夹具**，
    而几十处用例都靠 `POST /auth/register` 造用户。开关关着 ⇒ 那一步全 403。

    实测：先只 patch 在 `api_client` 里 ⇒ 全量回归 **107 条失败**，错误清一色
    `assert 403 == 200`（`REGISTER_DISABLED`），看着像权限骨架把业务打挂了，
    实际是「测试造不出用户」。所以必须提到 session 级。

    ## 为什么 patch `BUILTIN_DEFAULTS` 而不是 `effective_value`

    `live_bool` → `live_value` → `effective_value`：先查 `system_settings` 行，
    查不到才回落 `BUILTIN_DEFAULTS`。测试库是空的 ⇒ 回落分支即生效，不必再 patch
    更深一层的函数（patch 越深越容易在重构时失锚）。

    ⚠️ 这条 fixture **不影响**「基线是否关着」的守卫：那些守卫（如
    `test_register_switch_baseline_is_off`）读的是源码 / 随包种子字面值，
    不是运行时的 `BUILTIN_DEFAULTS`。
    """
    from backend.app.services import settings_service as _settings

    #: ⚠️ **同时改两层**（2026-10-24 实测）：
    #: `live_global_bool` 的回落顺序是 `settings`（= `config/default.yaml`）
    #: → `BUILTIN_DEFAULTS`，**settings 优先**。只改 `BUILTIN_DEFAULTS` 时，
    #: yaml 里的 `false` 仍然胜出 ⇒ 注册仍 403（实测踩了 3 轮）。
    #: 这里把 settings 那一层也 patch 成 True。
    original = _settings.BUILTIN_DEFAULTS.get("register.enabled")
    _settings.BUILTIN_DEFAULTS["register.enabled"] = True
    _settings.invalidate(key="register.enabled")

    original_settings_default = _settings.settings_default

    def _patched_settings_default(key: str):
        if key == "register.enabled":
            return True
        return original_settings_default(key)

    _settings.settings_default = _patched_settings_default  # type: ignore[assignment]
    try:
        yield
    finally:
        _settings.settings_default = original_settings_default  # type: ignore[assignment]
        if original is None:
            _settings.BUILTIN_DEFAULTS.pop("register.enabled", None)
        else:
            _settings.BUILTIN_DEFAULTS["register.enabled"] = original
        _settings.invalidate(key="register.enabled")


@pytest.fixture(autouse=True)
def block_real_network(request, monkeypatch):
    """**autouse**：禁止真实网络连接（含回环）。

    为什么连回环也禁（2026-10-23 实测）：Windows 上 `connect(("127.0.0.1", 3000))`
    对**没人监听**的端口要 **2.04 秒**（SYN 重试），返回 `ConnectionRefusedError` 之前
    已经等了两秒。`test_llm_gateway` 每条用例建 2 个配置、各探测一次上游
    ⇒ 每条约 4 秒 ⇒ 55 条 **4m19s**，全量回归被拖到 **116 分钟**（基线 150 秒）。

    更要紧的是**确定性**：测试结论不该取决于「跑测试的机器上 3000 端口有没有起」。
    需要「上游不可达」这一语义时，用 `unreachable_upstream()` 打桩（立即失败）；
    需要访问本机真实服务时，显式 `@pytest.mark.allow_network` 并写明理由。
    """
    if request.node.get_closest_marker("allow_network"):
        return

    import os  # noqa: PLC0415
    import socket  # noqa: PLC0415

    allow_loopback = bool(os.environ.get(_ALLOW_LOOPBACK_ENV))
    real_create_connection = socket.create_connection

    def _guard(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) and address else address
        if allow_loopback and _host_is_loopback(host):
            return real_create_connection(address, *args, **kwargs)
        #: ⚠️ `TestClient` 内部会建 socket（starlette `_TestClientTransport` 走 anyio
        #: 到本进程的 ASGI 服务）⇒ 必须放行。判据用**调用栈**而不是主机名：
        #: 按主机放行等于对「所有回环」开闸，于是「没人监听的 127.0.0.1:3000」
        #: 又能等 2 秒了（这正是本轮的起点）。
        import sys  # noqa: PLC0415

        frame = sys._getframe(1)
        while frame is not None:
            name = frame.f_code.co_filename.replace("\\", "/").lower()
            if "starlette/testclient.py" in name or "fastapi/testclient.py" in name:
                return real_create_connection(address, *args, **kwargs)
            frame = frame.f_back
        raise RuntimeError(
            f"测试禁止真实网络连接：试图连接 {host!r}。"
            "上游不可达用 unreachable_upstream(monkeypatch)；"
            "确需真实外连请加 @pytest.mark.allow_network 并写明理由。"
        )

    #: ⚠️ **拦截点必须选对**（2026-10-23 三次实测）：
    #:   ① 直接替换 `socket.socket.connect` 方法 ⇒ anyio / ProactorEventLoop 的
    #:      内部 socket 也被接管，`TestClient` 整体崩（54/55 红，
    #:      `'ProactorEventLoop' object has no attribute '_ssock'`）；
    #:   ② 替换 `socket.create_connection` ⇒ **覆盖不到 urllib3**：它自带一份同名
    #:      实现（`urllib3.util.connection.create_connection`，内部直接 `sock.connect`），
    #:      于是 requests 出站照旧等 2 秒（3m28s 没变快）；
    #:   ③ 正解 = 拦 **urllib3 自己的那份** + `socket.create_connection`：
    #:      前者覆盖 requests / urllib3（本项目全部出站 HTTP），
    #:      后者覆盖其它用了标准库的代码，且都不碰事件循环内部 socket。
    import urllib3.util.connection as _u3_conn  # noqa: PLC0415

    real_u3_create = _u3_conn.create_connection

    def _u3_guard(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) and address else address
        if allow_loopback and _host_is_loopback(host):
            return real_u3_create(address, *args, **kwargs)
        raise RuntimeError(
            f"测试禁止真实网络连接（urllib3）：试图连接 {host!r}。"
            "上游不可达用 unreachable_upstream(monkeypatch)；"
            "确需真实外连请加 @pytest.mark.allow_network 并写明理由。"
        )

    monkeypatch.setattr(_u3_conn, "create_connection", _u3_guard)
    monkeypatch.setattr(socket, "create_connection", _guard)


def unreachable_upstream(monkeypatch, *, url_prefix: str = "http") -> None:
    """把 `requests` 的 GET/POST 变成**立即失败**（模拟上游不可达，零等待）。

    用途：`base_url` 指向的网关在本机没起时，用例要的是「上游不可达」这个**语义**，
    而不是真的等 2 秒 TCP 超时。写法：

        unreachable_upstream(monkeypatch)          # 所有 http 请求
        monkeypatch.setattr(requests, "get", ...)  # 只要更细控制时自己打
    """
    import requests  # noqa: PLC0415

    def _boom(*args, **kwargs):
        raise requests.exceptions.ConnectionError("模拟：上游不可达（测试桩）")

    monkeypatch.setattr(requests, "get", _boom, raising=False)
    monkeypatch.setattr(requests, "post", _boom, raising=False)
    monkeypatch.setattr(requests, "put", _boom, raising=False)
    monkeypatch.setattr(requests, "delete", _boom, raising=False)


def seed_owner_rows(engine: Engine, *, user_id: int = 1, account_id: int | None = None) -> None:
    """播种外键父行（`users`，可选 `platform_accounts`），幂等。

    ## 为什么需要它（2026-10-04）

    M5 的改动把 `PRAGMA foreign_keys=ON` 注册到 **`Engine` 类**上（`core/database.py`）
    —— 为的是让「删工作流保留历史」的 `ON DELETE SET NULL` 真正生效。代价是
    **所有** engine（含各测试文件自己 `create_engine` 的那些）都开始强制外键。

    此前这些测试「能过」，只是因为 SQLite 默认 `foreign_keys=OFF` ——
    它们插 `platform_accounts(user_id=1)` / `notes(user_id=1)` 时**从不建 `users` 行**。
    FK 一开，64 条用例立刻红（`FOREIGN KEY constraint failed`）。

    ⚠️ **这是测试夹具的缺陷，不是 FK=ON 的错**：外键约束本来就该是开的
    （它能让「删父留子」这类脏数据在测试期就暴露）。修法是让夹具自洽，
    **不是**把 PRAGMA 关回去。

    ⚠️ **父行必须先 `flush()` 再插子行**：同一 commit 里 SQLAlchemy 不保证语句顺序，
    实测 `platform_accounts.user_id -> users.id` 会撞外键。

    用法（在各测试文件自己的建库助手末尾加一行）::

        from tests.backend.conftest import seed_owner_rows
        engine = create_engine(...); Base.metadata.create_all(engine)
        seed_owner_rows(engine, user_id=1, account_id=1)
    """
    from backend.app.models.platform_account import PlatformAccount  # noqa: PLC0415
    from backend.app.models.user import User  # noqa: PLC0415

    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = factory()
    try:
        if db.get(User, user_id) is None:
            db.add(User(id=user_id, username=f"test-user-{user_id}", password_hash="x"))
            db.flush()
        if account_id is not None and db.get(PlatformAccount, account_id) is None:
            db.add(
                PlatformAccount(
                    id=account_id,
                    user_id=user_id,
                    platform="xhs",
                    nickname=f"test-account-{account_id}",
                )
            )
        db.commit()
    finally:
        db.close()


#: `service_session_modules()` 的**进程级结果缓存**（见该函数 docstring 的「进程级缓存」）。
#: 一个测试进程只真正扫描一次；清空请调 `invalidate_session_scan_cache()`。
_SESSION_SCAN_RESULT: list[str] | None = None


def service_session_modules() -> list[str]:
    """列出**所有**持有可能自开 Session 的工厂的后端模块（点分模块名）。

    两种形态都算（项目里并存，都必须打桩）：

    * `from backend.app.core.database import SessionLocal` —— 直接持有工厂；
    * `_session_factory = …` —— 模块层的**快照别名**（`note_sync_service` /
      `account_task_service` 等的「测试可注入」惯例）。⚠️ 快照是在 **import 时**取的，
      只 patch 它引用的那个模块**不会**改变它 ⇒ 必须连名字本身一起 patch。

    用 `ast` 扫语法树而不是字符串匹配：注释 / 文档里提到这些名字不算数
    （负向断言禁裸 `in`，这里同理）。

    ⚠️ **函数体内导入也算**：`ast.walk()` 遍历整棵树而不是只看 `tree.body`。
    理由是真实踩过 —— `material_scan_service` 曾在 `_run()` 里
    `from backend.app.core.database import SessionLocal`（函数级 import），
    AST 扫模块层时看不见它 ⇒ 桩打不到 ⇒ **后台 daemon 线程落到生产库**。

    ⚠️ **不硬编码名单**：硬编码迟早漏掉新模块，而漏掉的代价是「测试默默写生产库」。
    语法错的模块直接抛出，不静默跳过。

    ⚠️ **进程级缓存（2026-10-23）**：本函数被**每个** setup 调用
    （`api_client` → `patch_service_sessions`），原实现每次都 `ast.parse` 全仓
    191 个文件 = **2.26 秒/次**，全量回归里是纯重复劳动。

    缓存策略：**一个进程只真正扫描一次**，之后直接返回。理由（实测支撑）：

    * 扫一次的成本分两块：遍历 191 个文件 **0.25s**（Windows 文件系统固有成本，
      `os.walk` / `rglob` 都一样）+ `ast.parse` 全量 **约 2s**。两者都是纯浪费；
    * 「文件改了要不要重扫」—— **测试进程的生命期就是一次运行**，源码改了就意味着
      重新起 pytest（新进程、缓存天然为空）⇒ 不需要进程内失效检测；
    * `pytest-xdist` 每个 worker 是独立进程，各扫一次（8 核 = 8 次），仍比
      「每条用例扫一次」快几十倍。

    ⚠️ 若**同一进程内**改了源码还想让守卫看到新结果，调用
    `invalidate_session_scan_cache()` 显式清空（仅供这种罕见场景）。
    """
    global _SESSION_SCAN_RESULT

    import ast  # noqa: PLC0415

    if _SESSION_SCAN_RESULT is not None:
        return list(_SESSION_SCAN_RESULT)

    app_dir = ROOT / "backend" / "app"
    paths = sorted(app_dir.rglob("*.py"))

    names = {"SessionLocal", "_session_factory"}
    modules: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # ⚠️ 遍历整棵树（含函数体）：函数级 import 也是「会自开 Session 的模块」，
        #    漏掉它 = 线程静默连生产库（2026-10-02 material_scan_service 实测）。
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            elif isinstance(node, ast.ImportFrom):
                targets = [ast.Name(id=alias.name) for alias in node.names]
            if any(isinstance(t, ast.Name) and t.id in names for t in targets):
                modules.append(_module_name(path))
                break

    _SESSION_SCAN_RESULT = sorted(set(modules))
    return list(_SESSION_SCAN_RESULT)


def invalidate_session_scan_cache() -> None:
    """清空 `service_session_modules()` 的进程级缓存。

    只在「同一进程内改了 `backend/app` 源码还想立刻看到新扫描结果」时用
    （正常测试不需要：改源码 = 重新起 pytest = 新进程 = 缓存天然为空）。
    """
    global _SESSION_SCAN_RESULT

    _SESSION_SCAN_RESULT = None


def _module_name(path: Path) -> str:
    """`…/backend/app/services/x.py` → `backend.app.services.x`。"""
    relative = path.relative_to(ROOT).with_suffix("")
    return ".".join(relative.parts)


def patch_service_sessions(monkeypatch: Any, session_factory: Callable[[], Session]) -> list[str]:
    """把**全部**模块层 Session 工厂打到夹具库上，返回被替换的模块名（升序）。

    每个模块按它**实际持有**的名字替换（`SessionLocal` / `_session_factory` 都可能，
    也可能两个都有）—— 只替换存在的那个，不做「猜」。

    调用方拿到返回值可以断言「确实打到了想打的那个模块」（例如本用例关心的服务层），
    而不是靠「测试没炸」推断 —— 没打到桩时测试往往是**假绿**（断言看不到自己写的行，
    而请求已经落到生产库上了）。

    用 `monkeypatch` 而非裸 `setattr`：退出时自动还原，不会把替换泄漏给后面的用例。
    """
    import importlib  # noqa: PLC0415

    patched: list[str] = []
    for name in service_session_modules():
        module = importlib.import_module(name)
        for attribute in ("SessionLocal", "_session_factory"):
            if hasattr(module, attribute):
                monkeypatch.setattr(module, attribute, session_factory)
                patched.append(f"{name}.{attribute}")
    return patched


@pytest.fixture()
def db_engine(db_path: Path, monkeypatch) -> Engine:
    """建好全部表的 engine。

    ⚠️ **顺带播种 `users` id=1 与 `platform_accounts` id=10**（2026-10-04 加）。
    原因：隔离修好（见 `db_session`）之后，一批此前**静默读生产库**的用例
    立刻暴露了「它们依赖生产库里已存在的 users / platform_accounts 行」——
    测试库是空的，于是 `INSERT INTO material_folders (user_id=1)`、`notes
    (platform_account_id=10)` 之类会 `FOREIGN KEY constraint failed`。

    ⚠️ 这**不是**放宽容忍度，而是把「夹具库必须自洽」补上：外键指向的行由夹具
    自己造，用例不必各自重复播种（重复播种 = 20 处各写一份，迟早漏一处）。
    """
    engine = make_engine(db_path)
    _base().metadata.create_all(bind=engine)
    #: 🔴 **必须在这里也打桩**（2026-10-25 修）：`settings_service` 等多处是
    #: 「模块级持有 `SessionLocal`，服务函数自开会话」（见本文件模块 docstring）。
    #: 此前只有 `api_client` 打桩 ⇒ 用 `db_factory` / `db_session` 的用例里，
    #: 服务自开的会话**连到真实库**。实测后果：真实库的 `system_settings` 与
    #: 用例自建的 schema 结构不一致时报 `字段 xxx 不存在` —— 看起来像代码 bug。
    #: 打桩放这里 ⇒ 所有用 engine 的夹具（含 db_factory / db_session）自动覆盖。
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    patch_service_sessions(monkeypatch, factory)
    #: 🔴 **清配置缓存**（2026-10-25）：`settings_service` 的进程级缓存按
    #: `(user_id, key)` 存，**不带 schema 维度**。xdist 下同一 worker 顺序跑多个
    #: 用例时，上一条用例读到的值会带到下一条 —— 而下一条连的是**另一个 schema**
    #: （每用例新建）。表现是 `字段 xxx 不存在` 或「读到了上一条用例的值」。
    #: 清缓存必须在**建表之后、用例开始之前**，所以放这里而不是 teardown。
    try:
        from backend.app.services import settings_service as _ss

        _ss.reset_cache_for_tests()
    except Exception:  # noqa: BLE001 - 无缓存实现时不影响夹具可用
        pass
    # ⚠️ **刻意不播种任何行**（2026-10-04）：`test_conftest_fixtures.py` 的契约是
    # 「新库应当是空的」，而大量用例（如 `test_llm_gateway`）也依赖「库里没有东西」
    # 才断言得准。曾经在这里加过 `users`/`platform_accounts` 基线行 ⇒ 一次红 20+ 条。
    # **外键父行由需要它的文件自己造**（见 `seed_owner_rows` 的用法）。
    yield engine
    #: 🔴 **必须 DROP 本用例的 schema**（2026-10-25 补回；此前漏掉了）。
    #:
    #: SQLite 时代是「每用例一个 .db 文件，进程退出就没了」；PG 的 schema 会
    #: **一直留在库里**。漏了这一步的实测后果：测试库累积了几十个旧 schema，
    #: 其中一批是**建表语句旧版**（没有 `system_settings.workspace` 列）⇒
    #: 某些用例的 `search_path` 命中旧 schema，报
    #: `字段 system_settings.workspace 不存在` —— 看起来像代码 bug，实际是
    #: 测试环境脏了。**这类假红极难定位**，所以 teardown 不能省。
    drop_schema(engine)
    engine.dispose()


def _seed_baseline_rows(engine: Engine) -> None:
    """播种外键基线行（`users` / `platform_accounts`），幂等。"""
    from backend.app.models.platform_account import PlatformAccount  # noqa: PLC0415
    from backend.app.models.user import User  # noqa: PLC0415

    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = factory()
    try:
        # ⚠️ 必须**先 flush 父行再插子行**：同一个 commit 里 SQLAlchemy 不保证
        # 按声明顺序发语句，实测 `platform_accounts.user_id -> users.id` 会撞
        # `FOREIGN KEY constraint failed`（种子行自己就违反外键）。
        if db.get(User, 1) is None:
            db.add(User(id=1, username="test-user-1", password_hash="x"))
            db.flush()
        if db.get(PlatformAccount, 10) is None:
            db.add(
                PlatformAccount(
                    id=10, user_id=1, platform="xhs", nickname="test-account-10"
                )
            )
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def db_factory(db_engine: Engine) -> Callable[[], Session]:
    """Session 工厂：`db = db_factory()`，用完自己 `db.close()`。

    返回工厂而不是单个 Session，是因为大量用例要在请求**之后**再查一次库
    （断言落库结果）—— 那时请求线程的 Session 已经关了。
    """
    factory = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)
    return factory


@pytest.fixture()
def db_session(
    db_factory: Callable[[], Session], monkeypatch
) -> Iterator[Session]:
    """单个 Session（不需要「请求后再查」的用例用它更短）。

    ⚠️ **本夹具同时打桩全部服务模块的 Session 工厂**（2026-10-04 加）：
    此前只有 `api_client` 打桩，于是「用 `db_session` + 直接调服务函数」的用例
    会**静默打到生产库**。实测事故（2026-10-04）：一个「直接调服务函数」的用例往
    `data/spider_xhs.db` 写了 21 条业务行（服务自开 Session，本夹具不管它），
    另有同批 4 条 `workflows` + 22 条 `workflow_runs` + 12 条 `workflow_nodes`。
    ⚠️ 触发它的那个模块（工作流）**已于 2026-10-07 整条移除**，但**这条纪律与打桩本身
    必须留下** —— 漏打桩的代价是污染真实数据，与哪个模块无关。

    教训：**隔离必须是夹具的属性，不能是「用例记得申请哪个夹具」的约定** ——
    后者迟早在某个新用例上漏掉，而漏掉的代价是污染真实数据。
    """
    patch_service_sessions(monkeypatch, db_factory)
    # DEF-094：素材缩略图的落盘根目录也要搬走 —— 否则「用 db_session + 直调素材服务」
    # 会读**生产库**的文件夹行、把缩略图原地覆盖到生产 storage（实测事故）。
    sandbox_material_storage_always()
    session = db_factory()
    yield session
    session.close()


def sandbox_material_storage(monkeypatch: Any, root: Path) -> Path:
    """把**素材缩略图/上传物**的落盘根目录指到 `root`（测试自己的 tmp），返回它。

    ## 为什么必须有这件（DEF-094，2026-10-05 实测事故）

    `Settings.storage_dir` 是**写死的相对路径** `backend/app/storage`，且 `thumbs_dir()`
    每次调用都现算 —— 于是**任何**调素材扫描的用例都会：
    ① 读**生产库**里的文件夹行（拿到用户的素材目录，例如 `D:/work/XHS/SCK`），
    ② 把缩略图**原地覆盖**到生产 storage（`thumbs/{fid}/f{nid}-{nid}.jpg`）。

    实测：只跑一遍素材扫描用例，就把用户素材库里两张缩略图覆盖了
    （358 → 482 字节）+ 新增一张 —— 页面显示「缩略图取不到」正是这么来的。
    这与「按 AST 打桩全部 Session 工厂」是同一类债：**隔离必须是夹具的属性，
    不能靠用例记得去申请**（2026-10-04 那次事故的教训，这里换了条链路重演）。

    ⚠️ 只打桩 `thumbs_dir`（服务模块体内的引用），**不改 `Settings.storage_dir`**：
    后者同时被导出（`storage/exports`）、发布（`tmp_note_publish`）、生成媒体
    （`storage/media`）用着，全局搬走会动到别人的真实文件。

    ⚠️ 必须打桩在**服务模块上**：`_make_image_thumb` / 缩略图端点是
    `from ...material_scan_service import thumbs_dir`（模块级名字），改 `Settings`
    对它无效。
    """
    from backend.app.services import material_scan_service  # noqa: PLC0415

    target = Path(root) / "material_storage"
    thumbs = target / "materials" / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)

    def _thumbs_dir() -> Path:
        return thumbs

    # ⚠️ 必须带同一个标记：`db_session` / `api_client` 里的兜底版本靠它判断
    #    「已经有沙箱了，别再盖一层」。不带标记的话，夹具执行顺序一变
    #    （兜底后跑）就会把用例自己的 tmp 沙箱顶掉 ⇒ 用例断言落空（实测踩到）。
    _thumbs_dir._hermes_sandboxed = True  # type: ignore[attr-defined]
    monkeypatch.setattr(material_scan_service, "thumbs_dir", _thumbs_dir)
    return target


@pytest.fixture()
def material_storage(tmp_path, monkeypatch) -> Path:
    """用它的用例 = 「素材落盘写进 tmp_path，不碰生产 storage」。

    没申请它的用例仍可能经 `db_session` 走到素材服务 —— 那条路由
    `db_session` 自己兜（见该夹具的 body），**两道**都在。
    """
    return sandbox_material_storage(monkeypatch, tmp_path)


def sandbox_material_storage_always() -> None:
    """兜底版本：文件系统级一次性隔离（供 `db_session` / `api_client` 用）。

    ⚠️ 它**不接收 `monkeypatch`**（夹具体里的 `monkeypatch` 绑在各自的 tmp 上，
    这里要的是「进程级隔离」），所以直接 `setattr` —— 被测代码只会把它改成 tmp 下的
    路径，**不会**改成生产路径，泄漏的方向是安全的。
    """
    import tempfile  # noqa: PLC0415

    from backend.app.services import material_scan_service  # noqa: PLC0415

    if getattr(material_scan_service.thumbs_dir, "_hermes_sandboxed", False):
        return  # 已经是沙箱（夹具版或本函数自己装过）⇒ 不再盖一层

    target = Path(tempfile.mkdtemp(prefix="hermes-material-sandbox-")) / "materials" / "thumbs"
    target.mkdir(parents=True, exist_ok=True)

    def _thumbs_dir() -> Path:
        return target

    _thumbs_dir._hermes_sandboxed = True  # type: ignore[attr-defined]
    material_scan_service.thumbs_dir = _thumbs_dir


@pytest.fixture()
def api_client(db_engine: Engine, monkeypatch):
    """连到测试库的 `TestClient(app)`；退出时清掉自己加的 override。

    ⚠️ 退出时**必定** `pop`：`app.dependency_overrides` 是模块级全局，
    旧样板的 `_override_database()` 只写不清，会把库指向泄漏给同会话后面的用例。

    ⚠️ **同时把全部模块级 `SessionLocal` 打到本夹具库上**（2026-09-27 起，见模块
    docstring）：光覆盖 `get_db` 只挡住「用 `Depends(get_db)` 的代码」，服务层自开
    Session 的那部分照样打到生产库 —— 端点一旦在链路里调服务函数，测试就会写真实数据。
    这条默认行为让「用 `api_client` = 落库落在测试库」成为**唯一约定**。

    用例若还想断言「本用例关心的那个服务模块确实被打到桩上」，直接调
    `patch_service_sessions(monkeypatch, db_factory)` 取返回值即可（重复打桩幂等）。
    """
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from backend.app.core.database import get_db  # noqa: PLC0415
    from backend.app.main import app  # noqa: PLC0415

    factory = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)
    patch_service_sessions(monkeypatch, factory)
    #: ⚠️ **`init_db` 必须打桩**（2026-10-23）：`main.py` 在 import 期
    #: `from backend.app.core.database import init_db` 做了**名字快照**，
    #: 而 `init_db()` 内部用的是 `core/database.py` 的**模块级默认 engine**
    #: （`sqlite:///./data/spider_xhs.db` = **真实生产库**），不是本夹具的测试库。
    #: 后果两条：
    #:   ① **每条用例都往真实库跑一遍 alembic 迁移链**（95 个迁移，实测单条 16.6 秒）
    #:      —— 这是全量回归从 150 秒涨到 116 分钟的主因；
    #:   ② 测试**会写真实库**（建表 / 迁移 / `_normalize_*` 回填），与「测试不碰生产数据」
    #:      的红线冲突，只是平时看起来「没坏」所以一直没被发现。
    #: 这里把 `init_db` 换成「在测试 engine 上建结构」：语义等价（空库 → 建表），
    #: 但不碰真实库、且不走迁移链（迁移能否执行由 `run_alembic()` 的专门守卫覆盖）。
    def _init_db_on_test_engine(*_a, **_k) -> None:
        _base().metadata.create_all(bind=db_engine)

    monkeypatch.setattr("backend.app.main.init_db", _init_db_on_test_engine, raising=False)
    #: **注册开关按夹具打开**（v1.1.0 S1）：产品基线是 `register.enabled=false`
    #: （防「出厂弱口令 + 任何人可注册」），但全仓几十处 `POST /auth/register`
    #: 是「造一个用户」的夹具手段、不是被测量对象 —— 关着它们会让所有用例在建用户
    #: 那步 403（实测：不打开时全量红 400+ 条，错误全是 `assert 403 == 200`）。
    #:
    #: ⚠️ 为什么在**夹具**里而不是 `tests/conftest.py` 做成会话级 autouse：
    #: 那会让「读产品基线字面值」的守卫（`test_register_switch_baseline_is_off`
    #: / `test_settings_baseline_is_one_number_everywhere`）读到打过补丁的值，
    #: 把「基线是否关着」变成永真断言。夹具级只在**用了 `api_client`** 的用例里生效，
    #: 而那些用例本来就是在测端点行为（不是测基线）。
    #: 用 `monkeypatch.setitem` ⇒ 用例结束自动复原。
    from backend.app.services import settings_service as _settings_service  # noqa: PLC0415

    monkeypatch.setitem(_settings_service.BUILTIN_DEFAULTS, "register.enabled", True)
    _settings_service.invalidate(key="register.enabled")
    # DEF-094：素材端点（新建文件夹 / 重扫 / 上传 / 缩略图）会落盘 —— 一律搬进 tmp。
    sandbox_material_storage(monkeypatch, Path(tempfile.mkdtemp(prefix="hermes-material-api-")))

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


#: `upgrade head` 路上「**区间内后续迁移会碰、但最小夹具不建**」的表（DDL 逐条在此）。
#:
#: 为什么需要它（DEF-082 实测）：四个迁移用例（`test_scheduled_jobs.py` × 3、
#: `test_initial_data.py` × 1，见 `run_alembic`）都是「建**最小**遗留库 → `stamp <父 revision>`
#: → `upgrade head`」。最小库只建**被测迁移要碰的表**，而 `upgrade head` 会把
#: `(stamp, head]` 区间里的**全部**迁移跑一遍 —— 区间里任何一条迁移碰了夹具没有的表，
#: 用例就红在与它毫无关系的地方（head 迁移 `l3m4n5o6p7q8` 建
#: `ix_notes_user_created(user_id, created_at)`，四个夹具都没有 `notes` ⇒ 四条 slow
#: 用例一起红，而报错被 `CalledProcessError` 吞掉，看不出根因）。
#:
#: ⚠️ **不要给迁移加「表不存在就跳过」的兜底**：那会让迁移在真库上**静默不干活**
#: （DEF-048「安全网不能条件性」同型）。缺表是**夹具**的债，在夹具侧还。
#: ⚠️ **新增 head 迁移若碰了新表 ⇒ 只改这里一处**（四个夹具共用本表，不各抄一份，DEF-044）。
LATER_MIGRATION_TABLES: dict[str, str] = {
    # `l3m4n5o6p7q8`（2026-09-28，设计 13 §41.6）：建 `notes(user_id, created_at)` 复合索引。
    # 夹具只需要「这张表存在 + 有这两列」——迁移只建索引，不读数据。
    # ⚠️ 2026-09-30 补 `note_id` / `platform_account_id`：`b3c4d5e6f7a8` 给
    #    `note_comments` 回填隔离键时要 `SELECT n.platform_account_id FROM notes n
    #    WHERE n.note_id = note_comments.note_id` —— 少这两列就是
    #    `no such column: n.platform_account_id`。
    "notes": (
        "CREATE TABLE notes ("
        " id INTEGER NOT NULL,"
        " user_id INTEGER,"
        " note_id VARCHAR(64),"
        " platform_account_id INTEGER,"
        " created_at TIMESTAMP NOT NULL,"
        " PRIMARY KEY (id))"
    ),
    # `m4n5o6p7q8r9`（2026-09-29，设计 17 §3.8/§3.9）：llm 网关两张新表。
    # 迁移区间内会自动建（幂等保护：已存在即返回），夹具只需保证「不冲突」。
    # ⚠️ `default_model` / `models_json` **必须声明**（2026-10-02 修）：
    #    stamp 点之后的 `d5e6f7a8b9c0`（v1.9）要读这两列做存量回填
    #    （`SELECT id, models_json ... WHERE default_model IS NULL OR TRIM(default_model)=''`）
    #    —— 少列即 `no such column: default_model`，四条最小夹具用例同时红。
    #    夹具语义是「**stamp 点当时的形状**」，不是 head 的形状：后续 v2.8
    #    （`f6a7b8c9d0e1`）删列是**迁移自己**的事，夹具不得替它预先删掉。
    "llm_gateway_configs": (
        "CREATE TABLE llm_gateway_configs ("
        " id INTEGER NOT NULL,"
        " user_id INTEGER NOT NULL,"
        " name VARCHAR(128) NOT NULL,"
        " base_url VARCHAR(512) NOT NULL DEFAULT 'http://127.0.0.1:3000',"
        " access_token TEXT NOT NULL DEFAULT '',"
        # ⚠️ `local_db_path` 必须声明（2026-10-13 补）：它在 `m4n5o6p7q8r9` 建表时就有
        #    （早于本夹具的 stamp 点 `h9i0j1k2l3m4`）⇒ 属于「stamp 点当时的形状」的一部分。
        #    缺列时，head 迁移 `c2d3e4f5a6b8`（网关服务商补种，走 ORM）会
        #    `table llm_gateway_configs has no column named local_db_path` ——
        #    夹具的债在夹具侧还（同 `job_name` / `notes` 的先例）。
        " local_db_path VARCHAR(512) NOT NULL DEFAULT '',"
        " default_model VARCHAR(128) NOT NULL DEFAULT '',"
        " models_json TEXT NOT NULL DEFAULT '[]',"
        " enabled BOOLEAN NOT NULL DEFAULT TRUE,"
        " healthy BOOLEAN NOT NULL DEFAULT FALSE,"
        " last_health_at TIMESTAMP,"
        " created_at TIMESTAMP NOT NULL,"
        " updated_at TIMESTAMP NOT NULL,"
        " PRIMARY KEY (id))"
    ),
    "llm_gateway_snapshots": (
        "CREATE TABLE llm_gateway_snapshots ("
        " id INTEGER NOT NULL,"
        " user_id INTEGER NOT NULL,"
        " gateway_id INTEGER NOT NULL,"
        " stat_date DATE NOT NULL,"
        " call_count INTEGER NOT NULL DEFAULT 0,"
        " success_count INTEGER NOT NULL DEFAULT 0,"
        " fail_count INTEGER NOT NULL DEFAULT 0,"
        " total_quota INTEGER NOT NULL DEFAULT 0,"
        " total_tokens INTEGER NOT NULL DEFAULT 0,"
        " avg_latency_ms INTEGER,"
        " source VARCHAR(16) NOT NULL DEFAULT 'newapi',"
        " raw_json TEXT NOT NULL DEFAULT '{}',"
        " created_at TIMESTAMP NOT NULL,"
        " updated_at TIMESTAMP NOT NULL,"
        " PRIMARY KEY (id))"
    ),
    # `d5e6f7a8b9c0`（2026-10-01，设计 17 §13.1 v1.9）：服务商化 ——
    # `llm_gateway_configs` 加 `provider_key`/`platform`、`model_configs` 加 `provider_key`、
    # drop `llm_gateway_tokens`。
    # ⚠️ `model_configs` 是**首迁移**（`3bd70519ad3c`）建的**一期表** —— 最小夹具没有它，
    #    而本迁移要 `ALTER TABLE model_configs ADD COLUMN` ⇒ 缺表即
    #    `no such table: model_configs`（实测踩到）。故给最小形状（只需表存在 + 加列不冲突）。
    "model_configs": (
        "CREATE TABLE model_configs ("
        " id INTEGER NOT NULL,"
        " user_id INTEGER,"
        " name VARCHAR(128) NOT NULL,"
        " model_type VARCHAR(32) NOT NULL,"
        " provider VARCHAR(64) NOT NULL,"
        " model_name VARCHAR(128),"
        " base_url TEXT,"
        " encrypted_api_key TEXT,"
        " is_default BOOLEAN,"
        " PRIMARY KEY (id))"
    ),
    "llm_gateway_tokens": (
        # v1.9 该表被 drop；夹具先建出来，迁移才会走「存在 ⇒ drop」分支。
        "CREATE TABLE llm_gateway_tokens ("
        " id INTEGER NOT NULL,"
        " user_id INTEGER NOT NULL,"
        " gateway_id INTEGER NOT NULL,"
        " platform VARCHAR(32) NOT NULL,"
        " function VARCHAR(32) NOT NULL,"
        " token_name VARCHAR(128) NOT NULL,"
        " enabled BOOLEAN NOT NULL DEFAULT TRUE,"
        " created_at TIMESTAMP NOT NULL,"
        " updated_at TIMESTAMP NOT NULL,"
        " PRIMARY KEY (id))"
    ),
    # ⚠️ `b2c3d4e5f6a7`（2026-10-02，设计 17 §4.5 v2.3）：预警规则三级作用域 ——
    #    它 `batch_alter_table(llm_gateway_alerts)` 加 `model_name`、放宽 `gateway_id`。
    #    建这张表的 `n5o6p7q8r9s0` 在**全部** stamp 点之前（链上第 55 / 共 61），
    #    故 `upgrade head` 区间内**不会**重建它 ⇒ 夹具必须先给形状，
    #    否则 `no such table: llm_gateway_alerts`（2026-10-02 实测，三个迁移用例齐红）。
    #    ⚠️ 与 `scheduled_job_logs` / `llm_business_providers` 同型例外：**表不在区间内、
    #    迁移只读不改结构**（`batch_alter_table` 走 SQLite 重建，要求表已存在）。
    #    列清单 = 与该迁移 `PRAGMA table_info` / 重建 SELECT 对齐的最小形状。
    "llm_gateway_alerts": (
        "CREATE TABLE llm_gateway_alerts ("
        " id INTEGER NOT NULL,"
        " user_id INTEGER NOT NULL,"
        " gateway_id INTEGER NOT NULL,"
        " platform VARCHAR(32) NOT NULL DEFAULT '',"
        " function VARCHAR(32) NOT NULL DEFAULT '',"
        " metric VARCHAR(16) NOT NULL,"
        " operator VARCHAR(8) NOT NULL DEFAULT 'gt',"
        " threshold FLOAT NOT NULL,"
        " window_seconds INTEGER NOT NULL DEFAULT 86400,"
        " level VARCHAR(16) NOT NULL DEFAULT 'warning',"
        " enabled BOOLEAN NOT NULL DEFAULT TRUE,"
        " last_fired_at TIMESTAMP,"
        " created_at TIMESTAMP NOT NULL,"
        " updated_at TIMESTAMP NOT NULL,"
        " PRIMARY KEY (id))"
    ),
    # `b3c4d5e6f7a8`（2026-09-30，设计 18 / ADR-021）：隔离键从店铺收敛到平台账号 ——
    # 加 `platform_accounts.seller_id`（带 shops 回填）、`shop_products` 换键重建、
    # `note_comments.platform_account_id`、`scheduled_job_logs` 列改名、drop `shops`。
    # 迁移会读 `notes.platform_account_id`、`shops.id/seller_id`，故一并给最小形状。
    # ⚠️ 只给「迁移要碰的列」，不复制生产全貌（夹具债在夹具侧还，见上方立规注释）。
    # ⚠️ 列清单 = 「stamp 点之后的**全部**迁移读到的列」并集，不只是 ADR-021 自己那几列：
    #    * `shop_id_fk` / `shop_state` —— `b3c4d5e6f7a8` 与 `g6h7i8j9k0l1` 都读；
    #    * `sub_type` —— `g6h7i8j9k0l1` 合并重复账号时读（`_pick_strongest_sub_type`）；
    #    * `external_user_id` —— `g6h7i8j9k0l1` 按小红书 ID 合并/建索引读；
    #    * `deleted_at` —— `a0b1c2d3e4f5` 清洗 `account_ids` 时读
    #      （`SELECT user_id, id FROM platform_accounts WHERE deleted_at IS NULL`）。
    #    漏一列就是一条 `no such column: xxx`，而且报错被 `run_alembic` 包成
    #    AssertionError，看起来像个与用例无关的迁移债 —— 2026-09-30 连踩两次
    #    （先 `scheduled_job_logs already exists`、再 `no such column: deleted_at`）。
    "platform_accounts": (
        "CREATE TABLE platform_accounts ("
        " id INTEGER NOT NULL,"
        " user_id INTEGER,"
        " platform VARCHAR(32),"
        " sub_type VARCHAR(32),"
        " nickname VARCHAR(128),"
        " external_user_id VARCHAR(128),"
        " deleted_at TIMESTAMP,"
        " shop_id_fk INTEGER,"
        " shop_state TEXT NOT NULL DEFAULT '{}',"
        " PRIMARY KEY (id))"
    ),
    "shops": (
        "CREATE TABLE shops ("
        " id INTEGER NOT NULL,"
        " user_id INTEGER,"
        " seller_id VARCHAR(64),"
        " shop_id VARCHAR(64),"
        " shop_name VARCHAR(128),"
        " PRIMARY KEY (id))"
    ),
    # ⚠️ `b3c4d5e6f7a8` **重建**这张表（SQLite 改不了约束）：先
    #    `INSERT INTO shop_products_new SELECT p.<25 列> FROM shop_products p`
    #    再 DROP/RENAME。所以夹具必须给**它 SELECT 的那 25 列**（少一列就是
    #    `no such column: p.stock` —— 2026-09-30 实测踩到），不能只给「看起来
    #    跟换键有关」的几列。列清单与该迁移 `INSERT ... SELECT` 严格对齐。
    "shop_products": (
        "CREATE TABLE shop_products ("
        " id INTEGER NOT NULL,"
        " user_id INTEGER,"
        " shop_id_fk INTEGER,"
        " item_id VARCHAR(64),"
        " item_title VARCHAR(512),"
        " price VARCHAR(32),"
        " stock INTEGER,"
        " sales INTEGER,"
        " category VARCHAR(128),"
        " images_json TEXT,"
        " status VARCHAR(32),"
        " synced_at TIMESTAMP,"
        " raw_json TEXT NOT NULL DEFAULT '{}',"
        " detail_json TEXT DEFAULT '{}',"
        " sku_count INTEGER,"
        " detail_synced_at TIMESTAMP,"
        " platform_missing_at TIMESTAMP,"
        " article_no VARCHAR(64),"
        " card_type INTEGER,"
        " price_min_fen INTEGER,"
        " price_max_fen INTEGER,"
        " create_time_ms INTEGER,"
        " on_shelf_time_ms INTEGER,"
        " off_shelf_time_ms INTEGER,"
        " last_audit_time_ms INTEGER,"
        " sales_30d INTEGER,"
        " PRIMARY KEY (id))"
    ),
    "note_comments": (
        "CREATE TABLE note_comments ("
        " id INTEGER NOT NULL,"
        # ⚠️ `note_id` 是指向 notes.id 的**整型外键**（模型 NoteComment）——
        #    曾误写成 VARCHAR(64)（与 notes.note_id 平台字符串 ID 混淆），
        #    b3c4d5e6f7a8 的回填 JOIN（`n.id = note_comments.note_id`）在 PG 上
        #    会撞 `integer = character varying`。列类型必须与真实库一致。
        " note_id INTEGER,"
        " comment_id VARCHAR(64),"
        " content TEXT,"
        " PRIMARY KEY (id))"
    ),
    # ⚠️ `scheduled_job_logs` **不在此表**（2026-09-30 修正）：它是 `y8z9a0b1c2d3`
    #    自己 `create_table` 建的，而 stamp 在 `x7y8z9a0b1c2`（= 它的父 revision）的两个
    #    用例（`test_scheduled_jobs.py` / `test_initial_data.py`）跑 `upgrade head` 时，
    #    这条迁移**就在区间内**。夹具先建 ⇒ 迁移再建 ⇒
    #    `OperationalError: table scheduled_job_logs already exists`（e3q8），
    #    且这条错误会被 `run_alembic` 包成 AssertionError，看起来像个无关的迁移债。
    #    stamp 更晚的那几个用例（`h8i9j0k1l2m3` / `k1l2m3n4o5p6` / `b2c3d4e5f6g7` /
    #    `h9i0j1k2l3m4`）各自在夹具里建了 `scheduled_job_logs`，不依赖本表。
    #    ⇒ 本表只收「迁移**只读不改**、夹具又不得不给形状」的表。
    # `f8a9b0c1d2e3`（2026-10-01，设计 17 §13.2.5 v2.0）：业务点映射建表 +
    # `llm_gateway_configs.model_type` 加列 + `model_configs` **退场（drop）**。
    # ⚠️ `model_configs` 的最小形状**沿用上面的 d5e6 定义**（同表同列）——
    #    本迁移**读**它（存量兼容：is_default 行的 base_url 归一化后反查服务商
    #    建映射）再 drop 它 ⇒ 夹具必须先建出来，否则存量兼容分支空跑、测不到。
    #    ⚠️ `llm_business_providers` 曾经**不在本清单**（理由「迁移对它只建不读」）——
    #    该理由 2026-10-23 起不成立，见下方同名条目的说明。
    # ⚠️ `llm_business_providers`（2026-10-23 补）：**必须建**，且**不含 `kind` 列**。
    #    原注释写「迁移对它只建不读」——已过时：区间内的 `b5c6d7e8f9a0`
    #    （split_business_keys_by_entry）会 `SELECT … kind FROM llm_business_providers`
    #    并 INSERT 带 `kind` 的行。夹具不建这张表 ⇒ `no such table`；
    #    建了但不带 `kind` ⇒ 正是本夹具要还原的「stamp 点当时的形状」，
    #    后面 `f9a0b1c2d3e4` 才给 `kind` 加列并回填 —— 那条迁移自身幂等。
    "llm_business_providers": (
        "CREATE TABLE llm_business_providers ("
        " id INTEGER NOT NULL,"
        " user_id INTEGER NOT NULL,"
        " platform VARCHAR(32) NOT NULL DEFAULT 'xhs',"
        " business_key VARCHAR(48) NOT NULL,"
        " provider_key VARCHAR(64) NOT NULL DEFAULT '',"
        " enabled BOOLEAN NOT NULL DEFAULT TRUE,"
        " created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
        " updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
        " PRIMARY KEY (id))"
    ),
    "publish_jobs": (
        "CREATE TABLE publish_jobs ("
        " id INTEGER NOT NULL,"
        " user_id INTEGER,"
        " platform_account_id INTEGER,"
        " status VARCHAR(32),"
        " PRIMARY KEY (id))"
    ),
    # ⚠️ `users`（2026-10-02，设计 17 §13 v2.9）：`a7b8c9d0e1f2` 对
    #    `llm_gateway_models` 做 `batch_alter_table`（SQLite 改约束只能重建表）——
    #    SQLAlchemy 重建前会**反射整表**，`llm_gateway_models` 建表时（`e5f6a7b8c9d0`）
    #    带 `sa.ForeignKey("users.id")` ⇒ 反射要解析 FK 目标表 `users`
    #    ⇒ 夹具没有它即 `NoSuchTableError: users`（2026-10-02 实测，4 个迁移用例齐红）。
    #    ⚠️ 与 `llm_gateway_alerts` 同型例外：**表不在 stamp 区间内、迁移只读不改结构**
    #    （本表是**首迁移** `3bd70519ad3c` 建的一期表，链上远在全部 stamp 点之前）。
    #    ⚠️ 只给「足以让反射解析 FK」的最小形状（主键 + 迁移会读的常见列），
    #    不复制生产全貌（夹具债在夹具侧还，见上方立规注释）。
    "users": (
        "CREATE TABLE users ("
        " id INTEGER NOT NULL,"
        " username VARCHAR(64),"
        " email VARCHAR(128),"
        " hashed_password VARCHAR(256),"
        " is_active BOOLEAN NOT NULL DEFAULT TRUE,"
        " created_at TIMESTAMP,"
        " updated_at TIMESTAMP,"
        " PRIMARY KEY (id))"
    ),
}





# --------------------------------------------------------------------------- #
# 千帆源码读取（2026-10-11 拆包后）
# --------------------------------------------------------------------------- #
def upsert_business_mapping(
    db: Any,
    *,
    user_id: int,
    business_key: str,
    provider_key: str,
    model_name: str,
    platform: str = "xhs",
    enabled: bool = True,
) -> None:
    """把「用户 × 平台 × 业务点」的映射**配成**给定服务商（幂等 upsert）。

    ⚠️ **为什么必须有它**（2026-10-13）：R4 服务商种子（设计 37）在**注册时**就为新用户
    播种了默认业务点映射（`seed_default_business_map_for_user`）⇒ `llm_business_providers`
    上 `(user_id, platform, business_key)` 的唯一约束让**盲 `db.add` 必撞**
    `UNIQUE constraint failed`。用例里「把某业务点指到某服务商」的意图都该走本函数：
    已有行**改指**，没有才新增。各文件别再各写一份（DEF-044）。
    """
    from backend.app.models import LlmBusinessProvider
    from sqlalchemy import select as _select

    row = db.scalar(
        _select(LlmBusinessProvider).where(
            LlmBusinessProvider.user_id == user_id,
            LlmBusinessProvider.platform == platform,
            LlmBusinessProvider.business_key == business_key,
        )
    )
    if row is None:
        db.add(
            LlmBusinessProvider(
                user_id=user_id,
                platform=platform,
                business_key=business_key,
                provider_key=provider_key,
                model_name=model_name,
                enabled=enabled,
            )
        )
    else:
        row.provider_key = provider_key
        row.model_name = model_name
        row.enabled = enabled


def clear_business_mapping(db: Any, *, user_id: int, business_key: str, platform: str = "xhs") -> None:
    """删掉「用户 × 平台 × 业务点」的映射行（幂等）。

    ⚠️ **为什么需要它**（2026-10-13）：R4 服务商种子（设计 37）在**注册时**就为新用户
    播种了默认业务点映射 ⇒ 「这个新用户没有映射」这个前提**不再天然成立**。
    凡是要测「未配映射」分支的用例，必须先 `clear_business_mapping(...)` 把种子行删掉，
    否则测到的是种子服务商的另一条分支（如 `LLM_PROVIDER_KEY_MISSING`）。
    """
    from backend.app.models import LlmBusinessProvider
    from sqlalchemy import delete as _delete

    db.execute(
        _delete(LlmBusinessProvider).where(
            LlmBusinessProvider.user_id == user_id,
            LlmBusinessProvider.platform == platform,
            LlmBusinessProvider.business_key == business_key,
        )
    )
    db.commit()


def clear_all_business_mappings(db: Any, *, user_id: int, platform: str = "xhs") -> None:
    """删掉该用户在某平台上的**全部**业务点映射（幂等）。

    ⚠️ 同 `clear_business_mapping` 的由来（2026-10-13 R4 种子）：测「什么都没配」
    或「全部未配置」的用例需要先回到空态。
    """
    from backend.app.models import LlmBusinessProvider
    from sqlalchemy import delete as _delete

    db.execute(
        _delete(LlmBusinessProvider).where(
            LlmBusinessProvider.user_id == user_id,
            LlmBusinessProvider.platform == platform,
        )
    )
    db.commit()


def read_qianfan_source(root: Path | None = None) -> str:
    """**整包**千帆后端源码拼接（`qianfan/` 包下全部 `.py`）。

    ## 为什么需要它（原 `qianfan.py` 已拆包）

    拆包前有 8 个守卫直接读 `backend/app/api/platforms/xhs/qianfan.py`，
    断言「这段实现在 qianfan 里」—— 例如导出用了 `quote` 就必须 import、
    `shop_for_account` 必须走唯一解析函数。文件变成**包**之后：

    * 继续读旧路径 ⇒ `FileNotFoundError`（读源码型守卫全红，却与被测行为无关）；
    * 改成只读某一个子模块 ⇒ 断言范围被**悄悄缩小**（实现在 `goods.py`、
      守卫却盯着 `publish.py`，从此恒绿）。

    所以统一走本函数：**读整包**，断言语义与拆包前逐字等价 —— 只关心
    「这段话在千帆这块实现里」，不关心它在哪个文件。

    ⚠️ 调用方**不要**自己拼 `glob`（各写一份迟早漂移）。也不要用
    `pkg.iterdir()` 之类的浅遍历 —— 将来若再分二级子包会漏读。
    """
    base = (root or Path(__file__).resolve().parents[2]) / "backend/app/api/platforms/xhs/qianfan"
    assert base.is_dir(), f"千帆包不存在：{base}（拆包后又挪走了？）"
    files = sorted(base.rglob("*.py"))
    assert files, f"千帆包内没有 .py：{base}"
    return "\n".join(f.read_text(encoding="utf-8") for f in files)

