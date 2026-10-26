from __future__ import annotations

import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from backend.app.core import db_dialect
from backend.app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()

# ---------------------------------------------------------------------------
# PostgreSQL 连接池（v1.1.0 / 2026-10-24：用户口径「SQLite 完全退役，全量切 PG」）
#
# ## 曾经这里有一大段 SQLite 配置（已删除，不是注释掉）
#
# WAL / busy_timeout / synchronous / cache_size / temp_store /
# `check_same_thread` / `PRAGMA foreign_keys` / `_enable_wal()` /
# `_normalize_sqlite_datetime_storage()`（把 UTC 文本时间 +8h 归一）。
# 它们是 2026-09-22「优化 SQLite 锁竞争」时按官方文档调的，**在 PG 上全部无意义**
# —— PG 没有 WAL 文件模式、没有 PRAGMA、时间列是 `timestamptz` 不是文本
# （最危险的是时区归一：真跑在 PG 上会把已是 +08 的时间再 +8 小时）。
#
# ⇒ 全部删除。**不要为了「向后兼容」把它们留成 if 分支** —— 那等于两套语义
# 各写一份，而本项目已有过「方言分支漏改一处就静默失效」的教训（见 db_dialect
# 模块 docstring）。SQLite 现在**任何地方都不存在**：测试也走 PG（conftest 用
# 「每用例一个 schema」隔离），只有迁移用例各用一个一次性 PG 库。
#
# ## 池参数不是「调优」，是正确性
#
# * `pool_pre_ping=True` —— PG 重启 / 网络闪断后池里的死连接会被取出来用，
#   不探活就是 `server closed the connection unexpectedly`。
# * `pool_recycle=1800` —— 避开 PG 侧 idle 超时与防火墙断连。30 分钟回收一次。
# * `pool_size` / `max_overflow` —— 与 PG `max_connections`（本机实测 **100**）对账：
#   10 + 20 = 30 上限，为调度器 / worker 线程池留足余量。
#
# ⚠️ 这里只能放 **SQLAlchemy 池参数**，不能放数据库连接参数。
# 连接参数要走 `connect_args`，且 **psycopg3 的参数名是 `connect_timeout`**。
# 2026-10-23 实测踩坑：把 `pool_timeout` 误写成连接参数会得到
# `psycopg.ProgrammingError: invalid connection option "timeout"`，
# 表现为**服务启动后无报错、无监听、进程静默退出**（uvicorn 把这个异常吞在 lifespan 里）。
# ---------------------------------------------------------------------------
if not db_dialect.is_postgres(settings.database_url):
    #: 启动期硬失败：只在 PG 上跑的代码已经删光（`ON CONFLICT` / `ILIKE` /
    #: `timestamptz` / 部分索引），静默降级回 SQLite 会在**第一次遇到这些语法时**
    #: 才炸，那时更难查。
    #:
    #: 没有任何逃逸口 —— 用户口径「完全放弃 SQLite，全部迁移到 PG」。
    raise RuntimeError(
        f"数据库必须是 PostgreSQL，当前 URL 方言 = "
        f"{db_dialect.dialect_of(settings.database_url)!r}。"
        "SQLite 已于 v1.1.0 彻底退役，代码路径已删除。"
    )

_PG_POOL: dict[str, object] = {
    "pool_size": 10,
    "max_overflow": 20,
    "pool_pre_ping": True,
    "pool_recycle": 1800,
    "pool_timeout": 30,
}
_connect_args: dict[str, object] = {"connect_timeout": 10}



engine = create_engine(settings.database_url, connect_args=_connect_args, **_PG_POOL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)



def init_db(bind=engine) -> None:
    """启动期初始化：跑迁移 + 一处历史数据订正。

    ⚠️ v1.1.0 删掉了两步 **SQLite 专属**的初始化（用户口径「SQLite 完全退役」）：
    `_enable_wal()`（切 WAL 文件模式）与 `_normalize_sqlite_datetime_storage()`
    （把 UTC 文本时间 +8h）。它们的前提是「时间在 SQLite 里存成无时区字符串」，
    PG 的 `timestamptz` 不需要，而且对 PG 再 +8 小时会**改错数据**。
    """
    import backend.app.models  # noqa: F401

    _run_alembic_migrations()


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


#: ⚠️ `_normalize_sqlite_datetime_storage()` 已删除（v1.1.0 / 2026-10-24）。
#:
#: 它的作用是「把 SQLite 里存成 UTC 文本的时间 +8 小时转成上海时间」，
#: 覆盖 14 张表的 created_at / updated_at / scheduled_at。SQLite 退役后
#: **整段无用** —— PG 的 `timestamptz` 自带时区，不需要字符串改写；
#: 更关键的是**对 PG 再 +8 小时会把数据改错**（静默的数据损坏，比崩溃更难发现）。
#:
#: 存量数据已在 `sqlite_to_pg.py` 迁移时按 PG 类型正确落库，无需二次归一。


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
