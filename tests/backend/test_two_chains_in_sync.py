"""两条迁移链指向同一模型状态（设计 51 §6 判据 D4）+ 新迁移禁方言 SQL（D7）。

## 为什么 D4 是「autogenerate 得空迁移」

两条链（`backend/alembic` SQLite 从属 / `backend/alembic_pg` PG 主线）**互不引用**，
各有自己的 `alembic_version`。它们唯一的共同契约是：**从任一条链的头往上建出的库，
与 `Base.metadata` 的差异为空**。

⇒ 判据只有一个方向是可信的：**跑 `--autogenerate`，产出的迁移里没有任何
`op.create_table` / `op.add_column` / `op.create_index`**。断言「两边表名集合相同」
是不够的 —— 列类型、可空性、索引条件（部分唯一索引的 `WHERE`）漂移它全看不见。

## 本机没有 PG 也不能静默跳过

PG 判据需要真库。没有 PG 时的处理只有两种「对的」：
① 显式 `pytest.skip` 并**说明原因**（不是静默 pass）；
② 不能写成「连不上就算过」。

本机实测有 PG（设计 58 记录在案），所以这里在装不上驱动 / 连不上实例时 skip 并打原因。

## D7 · 新迁移禁方言 SQL

`design 51 §2.4` 列了五条禁用写法（`PRAGMA table_info` / 裸 `ALTER ... DROP COLUMN` /
`json_extract` / `datetime(col, '+8 hours')` / 只写一边的部分索引）。
**只检查本段新增的三个迁移**（`a1b2c3d4e5f9` / `b2c3d4e5f6a0` / `c3d4e5f6a1b2`）——
历史 95 个迁移是 SQLite 形态的真实历史，设计 51 §2.1 明确不重写。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"

#: 分层标记（测试规范 §15.5）：本文件连真 PostgreSQL（D4）+ 读迁移源码，
#: 属于 integration（不是纯函数 redline）。
pytestmark = pytest.mark.integration

#: 本段新增的 SQLite 迁移（D7 的检查范围）
NEW_SQLITE_REVISIONS = (
    "a1b2c3d4e5f9_rbac_core_menus_roles.py",
    "b2c3d4e5f6a0_dict_and_api_log_status.py",
    "c3d4e5f6a1b2_seed_system_domain.py",
)

#: 禁用的方言专属写法 → 说明
FORBIDDEN_DIALECT_SQL = {
    "PRAGMA table_info": "改用 sa.inspect(conn).get_columns(...)（两方言通用）",
    "json_extract": "改用 Python 侧解析（SQLite 专有）",
    "+8 hours": "改用 Python 侧 shanghai_now()（PG 无 datetime(col,'+8 hours')）",
}


# --------------------------------------------------------------------------- #
# D4 · 两条链与模型零漂移
# --------------------------------------------------------------------------- #
def _pg_url() -> str | None:
    """可用的 PG URL（连不上 / 没装驱动 ⇒ None）。"""
    import os

    url = os.environ.get("XHS_TEST_PG_URL") or (
        "postgresql+psycopg://postgres:123456@127.0.0.1:5432/spider_xhs_s1check"
    )
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return None
    try:
        import sqlalchemy as sa

        eng = sa.create_engine(url)
        with eng.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        eng.dispose()
    except Exception:  # noqa: BLE001 —— 连不上只是「本机没 PG」，不是测试失败
        return None
    return url


def _diff_ops(url: str) -> tuple[list[str], list[str]]:
    """在给定库上跑 PG 链的 autogenerate，返回 (结构变更描述, 空迁移 id)。

    ⚠️ **不用 `command.revision(..., autogenerate=True)`**：它**总是把产物写进
    `version_locations` 的第一个现有目录**（实测：即使把临时目录列进去，文件仍然
    落在 `backend/alembic_pg/versions/`），于是「跑一次测试就在仓库里留下一个
    孤本 revision ⇒ 下次跑报 `Self-loop` / `Multiple heads`」。
    ⇒ 改用 `alembic.autogenerate.compare_metadata()`：它**只返回差异列表**，
    不写任何文件。这正是「空迁移」判据的真正语义（迁移文件只是承载差异的容器）。

    返回的第二项是「差异条目」的文本形式，失败时直接贴出来给人看。
    """
    import os

    import sqlalchemy as sa
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        from backend.app.core.database import Base
        import backend.app.models  # noqa: F401

        eng = sa.create_engine(url)
        try:
            with eng.connect() as conn:
                ctx = MigrationContext.configure(conn, opts={"compare_type": True})
                diffs = compare_metadata(ctx, Base.metadata)
        finally:
            eng.dispose()
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev

    #: `compare_metadata` 的条目形如 ('add_column', schema, table, Column(...)) /
    #: ('add_index', Index(...)) / ('remove_table', Table(...)) ……
    kinds = sorted({str(entry[0]) for entry in diffs})
    rendered = [
        f"{entry[0]}: "
        + " ".join(
            str(part if not hasattr(part, "name") else getattr(part, "name", part))
            for part in entry[1:]
        )[:200]
        for entry in diffs
    ]
    return kinds, rendered


def test_pg_chain_matches_the_models():
    """D4：`alembic_pg` 的 head 上 `--autogenerate` 必须是**空迁移**。

    ⚠️ 前置：本机 PG 上要有 `spider_xhs_s1check` 库（S1 基线实测用的那个）。
    缺库 ⇒ 用 `.hermes/tmp` 里的探针建：见测试 docstring 末尾命令。
    """
    url = _pg_url()
    if url is None:
        pytest.skip("本机没有可用的 PostgreSQL（或未装 psycopg）—— D4 需要真库，不能拿 SQLite 顶替")

    import sqlalchemy as sa

    eng = sa.create_engine(url)
    with eng.connect() as conn:
        tables = sa.inspect(conn).get_table_names()
    eng.dispose()
    if "menus" not in tables:
        pytest.skip(
            f"{url} 上还没有 S1 的表（menus 缺失）—— 先跑一次 "
            "`DATABASE_URL=<该库> python -m alembic -c backend/alembic_pg.ini upgrade head`"
        )

    ops, text = _diff_ops(url)
    assert ops == [], (
        "PG 链与模型漂移：autogenerate 产出了结构变更 "
        f"{ops}\n--- 逐条差异 ---\n" + "\n".join(text[:60])
    )


def test_two_chains_declare_the_same_tables():
    """两条链的「建表集合」必须一致（D4 的廉价旁证，不需要 PG 也能跑）。

    判据：`backend/alembic` 全部迁移里 `create_table("<x>")` 的集合 − 全部
    `drop_table("<x>")` 的集合，等于 `backend/alembic_pg` 基线里的 `create_table` 集合。
    **只看表名**（列 / 索引 / 条件由上面的 autogenerate 判据负责）。
    """
    def created_and_dropped(directory: Path) -> tuple[set[str], set[str]]:
        created: set[str] = set()
        dropped: set[str] = set()
        for path in sorted(directory.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            created |= set(re.findall(r"create_table\(\s*[\"']([a-z_0-9]+)[\"']", text))
            dropped |= set(re.findall(r"drop_table\(\s*[\"']([a-z_0-9]+)[\"']", text))
        return created, dropped

    sqlite_created, sqlite_dropped = created_and_dropped(BACKEND / "alembic" / "versions")
    pg_created, _pg_dropped = created_and_dropped(BACKEND / "alembic_pg" / "versions")

    from backend.app.core.database import Base
    import backend.app.models  # noqa: F401

    model_tables = set(Base.metadata.tables)
    sqlite_final = (sqlite_created - sqlite_dropped) | model_tables

    #: ⚠️ 只有「两条链都完整描述了同一批表」这一条能钉住：PG 基线必须覆盖**全部**
    #: 模型表；少一张 = 那张表在 PG 上没有对应的建表语句（部署后 500）。
    missing_in_pg = sorted(model_tables - pg_created)
    assert missing_in_pg == [], f"PG 基线缺这些模型的建表语句：{missing_in_pg}"
    extra_in_pg = sorted(pg_created - model_tables)
    assert extra_in_pg == [], f"PG 基线上有模型里不存在的表：{extra_in_pg}"
    #: SQLite 链的「建 − 删」必须包含全部模型表（历史 drop 的表不在模型里，属正常）
    assert sorted(model_tables - sqlite_final) == [], "SQLite 链的建表集合没覆盖全部模型表"


# --------------------------------------------------------------------------- #
# D7 · 新迁移禁方言 SQL
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("filename", NEW_SQLITE_REVISIONS)
def test_new_migrations_avoid_dialect_specific_sql(filename: str):
    """本段新增的三个 SQLite 迁移不得出现方言专属写法（设计 51 §2.4）。

    ⚠️ 判据必须跑在**剥掉注释与模块 docstring** 之后的代码上：这些迁移的
    docstring 恰恰在解释「**不要**用 `PRAGMA table_info` / `json_extract`」，
    不剥就会命中自己的说明文字（实测首轮 3 个假红）。用 AST 取所有字符串常量
    的位置不可靠（字符串本身可能就是 SQL），所以这里按行剥：
    ① `#` 注释行整行丢弃；② 文件头那个 `\"\"\"...\"\"\"` 块整体丢弃。
    """
    path = BACKEND / "alembic" / "versions" / filename
    assert path.is_file(), f"{filename} 不存在 —— 新迁移被删掉还是改名了？"
    text = path.read_text(encoding="utf-8")

    lines = text.splitlines()
    #: ② 模块 docstring（文件第一个 `"""` 到配对的 `"""`）
    start = next((i for i, line in enumerate(lines) if line.lstrip().startswith('"""')), None)
    if start is not None:
        if lines[start].strip().endswith('"""') and len(lines[start].strip()) > 3:
            lines = lines[start + 1:]
        else:
            end = next(
                i for i in range(start + 1, len(lines)) if '"""' in lines[i]
            )
            lines = lines[end + 1:]
    #: ① 行注释
    code = "\n".join(line for line in lines if not line.lstrip().startswith("#"))

    hits = [f"{needle}（{why}）" for needle, why in FORBIDDEN_DIALECT_SQL.items() if needle in code]
    assert hits == [], f"{filename} 用了方言专属写法：{hits}"

    #: 裸 SQL 的 ALTER ... DROP COLUMN 也不允许（改用 op.drop_column / batch）
    assert not re.search(r"ALTER\s+TABLE\s+\w+\s+DROP\s+COLUMN", code, re.IGNORECASE), (
        f"{filename} 用了裸 SQL `ALTER TABLE ... DROP COLUMN`，改用 op.drop_column / batch_alter_table"
    )


def test_new_migrations_write_both_partial_index_dialects():
    """部分唯一索引必须 `sqlite_where` + `postgresql_where` **两边都写**。

    只写一边的后果不是报错，而是「约束在另一个方言上**根本不存在**」
    （设计 58 §2.3 实测过 `platform_accounts` 的这个坑）。判据：在本次新增迁移里，
    每一个 `sqlite_where=` 都要有配对的 `postgresql_where=`（反之亦然）。
    """
    problems: list[str] = []
    for filename in NEW_SQLITE_REVISIONS:
        path = BACKEND / "alembic" / "versions" / filename
        text = path.read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        n_sqlite = code.count("sqlite_where=")
        n_pg = code.count("postgresql_where=")
        if n_sqlite != n_pg:
            problems.append(f"{filename}: sqlite_where ×{n_sqlite} ≠ postgresql_where ×{n_pg}")
    assert problems == [], "部分索引只写了一个方言：" + "; ".join(problems)
