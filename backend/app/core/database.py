from __future__ import annotations

import os

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from backend.app.core import db_dialect
from backend.app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()

# ---------------------------------------------------------------------------
# SQLite 并发配置（2026-09-22，用户口径：「优化数据库读写，定时任务也不可以
# 一直持有锁，需要时再持有」）
#
# 调研结论（市场成熟方案 = SQLite 官方文档 + SQLAlchemy 官方文档口径，无一项
# 需要引入第三方库）：
#
# 1. `journal_mode=WAL` 是**唯一**能解开「读阻塞写」的开关。WAL 下读者读
#    「主库 + WAL」、写者只**追加** WAL ⇒ **读写不互斥**。旧值 `MEMORY`
#    （回滚期刊存内存）有两个问题：① 进程中途死掉**没有期刊可回放 ⇒ 库可能
#    损坏**；② 它与默认的 DELETE 一样让读者持 SHARED 锁**挡住写者** ——
#    这正是「内容库笔记详情 500」的根因（同步任务读平台时开着读事务，
#    内容库详情一 commit 就 `database is locked`）。
# 2. `busy_timeout`：拿不到写锁时**等待**而不是立即抛 `SQLITE_BUSY`
#    （pysqlite 默认 0ms = 0.00 秒就失败，本项目已实测复现）。
# 3. `synchronous=NORMAL`：WAL 下**不会损坏库**，只接受「断电瞬间丢最近
#    几毫秒已提交事务」；换来写路径不再每次 fsync。
# 4. 连接池**保持 SQLAlchemy 对文件型 SQLite 的默认 `NullPool`**：文件连接
#    开销极低，且避免同一连接跨线程复用（pysqlite 不保证线程安全）。
#    **不引第三方连接池** —— 那是「同一语义两份实现」，且本项目的并发瓶颈
#    从来不是「建连接慢」而是「持锁久」。
#
# ⚠️ WAL 的运维代价：备份不能再只 `cp spider_xhs.db`（WAL 里可能还有没
# 合并回主库的已提交事务）⇒ 必须连 `-wal` / `-shm` 一起复制，或用
# `VACUUM INTO`。项目「迁移前先备份」纪律已按此更新。
# ---------------------------------------------------------------------------
_connect_args: dict[str, object] = {}
#: 方言判定**只走 `db_dialect`**（设计 51）：原来这里 4 处各写一遍
#: `settings.database_url.startswith("sqlite")`，换 PG 时漏改一处，那一段就静默失效。
_IS_SQLITE = db_dialect.is_sqlite(settings.database_url)
if _IS_SQLITE:
    _connect_args["check_same_thread"] = False
    import os
    db_path = settings.database_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

#: PostgreSQL 连接池（设计 53 A3，2026-10-23 切主库后必须配置）。
#:
#: 三个参数都不是「调优」，是**正确性**：
#: * `pool_pre_ping=True` —— PG 重启 / 网络闪断后池里的死连接会被取出来用，
#:   不探活就是 `server closed the connection unexpectedly`（SQLite 下不会发生，
#:   因为文件连接不存在「远端断开」）。
#: * `pool_recycle=1800` —— 避开 PG 侧 idle 超时与防火墙断连。30 分钟回收一次。
#: * `pool_size` / `max_overflow` —— 与 PG `max_connections`（本机实测 **100**）对账：
#:   10 + 20 = 30 上限，为调度器 / worker 线程池留足余量。
_PG_POOL: dict[str, object] = {}
if db_dialect.is_postgres(settings.database_url):
    #: ⚠️ 这里只能放 **SQLAlchemy 池参数**，不能放数据库连接参数。
    #: 连接参数（如 connect_timeout / statement_timeout）要走 `connect_args`，
    #: 且 **psycopg3 的参数名是 `connect_timeout`**（不是 psycopg2 的 `connect_timeout` 也不是 `timeout`）。
    #: 2026-10-23 实测踩坑：把 `pool_timeout` 误写成连接参数会得到
    #: `psycopg.ProgrammingError: invalid connection option "timeout"`，
    #: 表现为**服务启动后无报错、无监听、进程静默退出**（uvicorn 把这个异常吞在 lifespan 里）。
    _PG_POOL = {
        "pool_size": 10,
        "max_overflow": 20,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "pool_timeout": 30,
    }
    _connect_args["connect_timeout"] = 10

engine = create_engine(settings.database_url, connect_args=_connect_args, **_PG_POOL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@event.listens_for(engine, "connect")
def _configure_sqlite_connection(dbapi_connection, connection_record) -> None:
    """每个连接上线时的**逐连接** PRAGMA（都不需要拿锁，可安全重复执行）。"""
    if not _IS_SQLITE:
        return

    cursor = dbapi_connection.cursor()
    # 拿不到写锁时**等 5 秒**再失败（默认 0ms = 立即失败）。5s 是 SQLite
    # 生产实践的常用区间（5–10s），足以覆盖一次页面落库 + 提交。
    cursor.execute("PRAGMA busy_timeout=5000")
    # WAL 下的安全档：不损坏库，只牺牲「断电瞬间的毫秒级持久性」。
    cursor.execute("PRAGMA synchronous=NORMAL")
    # 读缓存 32MB：内容库列表 / 统计都是大表扫描，缓存直接决定读延迟。
    cursor.execute("PRAGMA cache_size=-32000")
    # 临时表与排序走内存，不落盘（本项目排序 / 分组都在内存量级）。
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.close()


# ⚠️ **全局打开 SQLite 外键约束**：SQLite 默认 OFF（与 Postgres 相反），
# ON DELETE SET NULL / CASCADE 全部不生效 ⇒ 删工作流时 workflow_runs.workflow_id
# 不会被置 NULL（设计 19 §6.1「删工作流保留历史」靠它落地）。
# 注册在 **Engine 类**上而不是本模块的 engine 实例上：测试夹具
# （tests/backend/conftest.py::make_engine）自己 create_engine，实例级监听
# 管不到它们 ⇒ 删工作流的 SET NULL 在测试里不生效。全局事件覆盖所有 engine。
from sqlalchemy.engine import Engine as _Engine  # noqa: E402


@event.listens_for(_Engine, "do_connect")
def _sqlite_enable_foreign_keys(dialect, connection_record, cargs, cparams):
    if dialect.name != "sqlite":
        return
    # do_connect 回调里连接还没建好；返回 None 时 SQLAlchemy 会用默认流程建连接，
    # 建好后走 ``connect`` 事件（见下）——两段配合：do_connect 判方言，connect 打 PRAGMA
    connection_record.info["_want_fk"] = True


@event.listens_for(_Engine, "connect")
def _sqlite_enable_foreign_keys_connect(dbapi_connection, connection_record):
    if connection_record.info.get("_want_fk"):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")


def _enable_wal(bind=engine) -> None:
    """把库切到 WAL 模式（**持久化在文件头里**，只需成功一次）。

    只在 `init_db()`（启动期、单线程）调用：切模式需要短暂排他锁，
    放在连接上线路径上做，可能撞上并发事务而失败。
    """
    if not db_dialect.is_sqlite(settings.database_url):
        return
    with bind.begin() as connection:
        connection.execute(text("PRAGMA journal_mode=WAL"))



def init_db(bind=engine) -> None:
    import backend.app.models  # noqa: F401

    # WAL 必须**最先**做：迁移本身是长事务，切模式要排他锁，晚了会撞上。
    _enable_wal(bind)
    _run_alembic_migrations()
    _normalize_model_config_names(bind)
    _normalize_sqlite_datetime_storage(bind)


def _run_alembic_migrations() -> None:
    #: ⚠️ **测试环境跳过迁移链**（2026-10-23）：测试库每次都是空库，跑 95 个迁移
    #: 做的工作就是「建表」，`create_all` 建出相同结构快 20 倍以上（实测单条用例
    #: 16.6s → 0.76s）。迁移能否执行由 `run_alembic()` 的专门守卫测，不靠这里顺路测。
    #: 开关由 `tests/backend/conftest.py` 模块级设置，生产环境永远不设。
    if os.environ.get("HERMES_SKIP_ALEMBIC_IN_TESTS") == "1":
        Base.metadata.create_all(bind=engine)
        return

    try:
        from alembic import command
        from alembic.config import Config

        ini_path = os.path.join(os.path.dirname(__file__), "..", "..", "alembic.ini")
        ini_path = os.path.normpath(ini_path)
        if not os.path.exists(ini_path):
            Base.metadata.create_all(bind=engine)
            return

        alembic_cfg = Config(ini_path)
        alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
        # alembic 的相对 script_location 按 **CWD** 解析；桌面安装版（设计 14 §4）
        # 启动时 CWD = 安装目录，仓库相对的 "backend/alembic" 会找不到 ⇒ 基于 ini
        # 自身路径钉成绝对路径。dev 下 dirname(ini_path)/alembic 与 ini 内原值
        # 指向同一目录，行为等价。
        script_location = os.path.join(os.path.dirname(ini_path), "alembic")
        if os.path.isdir(script_location):
            alembic_cfg.set_main_option("script_location", script_location)

        inspector = inspect(engine)
        has_tables = bool(inspector.get_table_names())
        has_alembic = "alembic_version" in inspector.get_table_names()

        if has_tables and not has_alembic:
            command.stamp(alembic_cfg, "head")
        else:
            command.upgrade(alembic_cfg, "head")
    except ImportError:
        Base.metadata.create_all(bind=engine)


def _normalize_model_config_names(bind) -> None:
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())
    if "model_configs" not in table_names:
        return

    with bind.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS app_migrations ("
                "name VARCHAR(128) PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)"
            )
        )
        applied = connection.execute(
            text("SELECT name FROM app_migrations WHERE name = 'normalize_legacy_gpt_54_model_name_v1'")
        ).first()
        if applied:
            return
        connection.execute(text("UPDATE model_configs SET model_name = 'gpt-5.4' WHERE model_name = 'gpt5.4'"))
        connection.execute(text("INSERT INTO app_migrations (name) VALUES ('normalize_legacy_gpt_54_model_name_v1')"))


def _normalize_sqlite_datetime_storage(bind) -> None:
    if not db_dialect.is_sqlite(settings.database_url):
        return

    datetime_columns = {
        "users": ["created_at"],
        "platform_accounts": ["created_at", "updated_at"],
        "account_cookie_versions": ["created_at"],
        "login_sessions": ["created_at"],
        "notes": ["created_at"],
        "model_configs": ["created_at"],
        "ai_drafts": ["created_at"],
        "ai_generated_assets": ["created_at"],
        "publish_jobs": ["created_at", "published_at"],
        "tasks": ["created_at"],
        "monitoring_targets": ["created_at", "updated_at", "last_refreshed_at"],
        "monitoring_snapshots": ["created_at"],
        "keyword_groups": ["created_at", "updated_at"],
        "api_logs": ["created_at"],
    }
    with bind.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS app_migrations ("
                "name VARCHAR(128) PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)"
            )
        )
        applied = connection.execute(
            text("SELECT name FROM app_migrations WHERE name = 'sqlite_datetime_asia_shanghai_v1'")
        ).first()

        inspector = inspect(bind)
        table_names = set(inspector.get_table_names())
        if not applied:
            for table_name, column_names in datetime_columns.items():
                if table_name not in table_names:
                    continue
                existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
                for column_name in column_names:
                    if column_name not in existing_columns:
                        continue
                    connection.execute(
                        text(f"UPDATE {table_name} SET {column_name} = datetime({column_name}, '+8 hours') WHERE {column_name} IS NOT NULL")
                    )
            connection.execute(text("INSERT INTO app_migrations (name) VALUES ('sqlite_datetime_asia_shanghai_v1')"))

        scheduled_at_applied = connection.execute(
            text("SELECT name FROM app_migrations WHERE name = 'sqlite_publish_scheduled_at_asia_shanghai_v1'")
        ).first()
        if scheduled_at_applied:
            return
        if "publish_jobs" in table_names and "scheduled_at" in {column["name"] for column in inspector.get_columns("publish_jobs")}:
            connection.execute(
                text("UPDATE publish_jobs SET scheduled_at = datetime(scheduled_at, '+8 hours') WHERE scheduled_at IS NOT NULL")
            )
        connection.execute(text("INSERT INTO app_migrations (name) VALUES ('sqlite_publish_scheduled_at_asia_shanghai_v1')"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
