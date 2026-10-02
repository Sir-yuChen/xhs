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


def service_session_modules() -> list[str]:
    """列出**所有**持有可能自开 Session 的工厂的后端模块（点分模块名）。

    两种形态都算（项目里并存，都必须打桩）：

    * `from backend.app.core.database import SessionLocal` —— 直接持有工厂；
    * `_session_factory = …` —— 模块层的**快照别名**（`note_sync_service` /
      `account_task_service` 等的「测试可注入」惯例）。⚠️ 快照是在 **import 时**取的，
      只 patch 它引用的那个模块**不会**改变它 ⇒ 必须连名字本身一起 patch。

    用 `ast` 扫语法树而不是字符串匹配：注释 / 文档里提到这些名字不算数
    （负向断言禁裸 `in`，这里同理）。

    ⚠️ **函数体内导入也算**（2026-10-02 起）：`ast.walk()` 遍历整棵树而不是只看
    `tree.body`。理由是真实踩过 —— `material_scan_service` 曾在 `_run()` 里
    `from backend.app.core.database import SessionLocal`（函数级 import），
    AST 扫模块层时看不见它 ⇒ 桩打不到 ⇒ **后台 daemon 线程落到生产库**，
    表现是「测试全红但缩略图已落盘、`last_scan_at` 永远是 None」。
    函数级 import 确实拿不到模块属性可打桩（名称只在函数内可见），所以这类模块
    必须**在模块层持有**（`SessionLocal` 或 `_session_factory`）才能被打桩 ——
    本函数把它们扫出来，`patch_service_sessions()` 才有机会替换；
    同时 `test_testing_spec_guards.py` 的守卫会钉住「函数级 import 不算合规持有」。

    ⚠️ **不硬编码名单**：硬编码迟早漏掉新模块，而漏掉的代价是「测试默默写生产库」
    （2026-09-27 已发生两次）。语法错的模块直接抛出，不静默跳过。
    """
    import ast  # noqa: PLC0415

    names = {"SessionLocal", "_session_factory"}
    modules: list[str] = []
    for path in sorted((ROOT / "backend" / "app").rglob("*.py")):
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
    return sorted(set(modules))


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
        " created_at DATETIME NOT NULL,"
        " PRIMARY KEY (id))"
    ),
    # `m4n5o6p7q8r9`（2026-09-29，设计 17 §3.8/§3.9）：llm 网关两张新表。
    # 迁移区间内会自动建（幂等保护：已存在即返回），夹具只需保证「不冲突」。
    "llm_gateway_configs": (
        "CREATE TABLE llm_gateway_configs ("
        " id INTEGER NOT NULL,"
        " user_id INTEGER NOT NULL,"
        " name VARCHAR(128) NOT NULL,"
        " base_url VARCHAR(512) NOT NULL DEFAULT 'http://127.0.0.1:3000',"
        " access_token TEXT NOT NULL DEFAULT '',"
        " default_model VARCHAR(128) NOT NULL DEFAULT '',"
        " enabled BOOLEAN NOT NULL DEFAULT 1,"
        " healthy BOOLEAN NOT NULL DEFAULT 0,"
        " last_health_at DATETIME,"
        " created_at DATETIME NOT NULL,"
        " updated_at DATETIME NOT NULL,"
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
        " created_at DATETIME NOT NULL,"
        " updated_at DATETIME NOT NULL,"
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
        " enabled BOOLEAN NOT NULL DEFAULT 1,"
        " created_at DATETIME NOT NULL,"
        " updated_at DATETIME NOT NULL,"
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
        " enabled BOOLEAN NOT NULL DEFAULT 1,"
        " last_fired_at DATETIME,"
        " created_at DATETIME NOT NULL,"
        " updated_at DATETIME NOT NULL,"
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
        " deleted_at DATETIME,"
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
        " synced_at DATETIME,"
        " raw_json TEXT NOT NULL DEFAULT '{}',"
        " detail_json TEXT DEFAULT '{}',"
        " sku_count INTEGER,"
        " detail_synced_at DATETIME,"
        " platform_missing_at DATETIME,"
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
        " note_id VARCHAR(64),"
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
    # ⚠️ `llm_business_providers` **不在本清单**：迁移对它只建不读，夹具先建会
    #    撞 `table already exists`（同 `scheduled_job_logs` 不在此表的先例）。
    "publish_jobs": (
        "CREATE TABLE publish_jobs ("
        " id INTEGER NOT NULL,"
        " user_id INTEGER,"
        " platform_account_id INTEGER,"
        " status VARCHAR(32),"
        " PRIMARY KEY (id))"
    ),
}


def create_later_migration_tables(conn: Any) -> None:
    """给最小夹具补上「迁移链后半段要碰的表」（幂等；清单见 `LATER_MIGRATION_TABLES`）。

    调用点：四个迁移用例各自建完**自己的**遗留表之后、`conn.commit()` 之前。
    已存在的表**跳过**（夹具将来自己建同名表时不冲突）。
    """
    existing = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for name, ddl in LATER_MIGRATION_TABLES.items():
        if name not in existing:
            conn.execute(ddl)


def run_alembic(db_path: Path, *args: str) -> None:
    """在夹具库上跑 alembic 子命令（`stamp <rev>` / `upgrade <rev>`）。

    四个迁移用例（`test_scheduled_jobs.py` × 3、`test_initial_data.py` × 1）原来各抄一遍
    「`DATABASE_URL` + `subprocess.run([sys.executable, -m, alembic, ...])`」样板 ——
    收在一处（DEF-044 同型：同一语义不留两份实现）。

    ⚠️ **失败时把 alembic 的输出原样带出来**（2026-09-28 实测教训）：原实现用
    `check=True` + `capture_output=True`，而 `CalledProcessError.__str__()` **只有**
    「returned non-zero exit status 1」—— alembic 的真实报错（`no such table: main.notes`
    这种）被吞在 `result.stderr` 里，pytest 只给你一个 `CalledProcessError`。
    后果：四个迁移用例同时红，却分不清是「夹具缺表」还是「迁移写坏了」，只能手工复现。
    现在改成显式判 rc + `AssertionError`（附 stdout/stderr 尾部），一眼能看出根因。
    失败**依然直接炸**，不静默跳过（迁移没跑成功而断言空摆，比红更危险）。
    """
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "backend/alembic.ini", *args],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise AssertionError(
            f"alembic {' '.join(args)} 失败（rc={result.returncode}，库={db_path.name}）—— "
            "迁移没跑成功，后面的断言全是空摆。真实报错：\n"
            f"--- stdout ---\n{result.stdout[-2000:]}\n"
            f"--- stderr ---\n{result.stderr[-2000:]}"
        )
