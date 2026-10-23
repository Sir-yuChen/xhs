"""Alembic environment for the **PostgreSQL** chain (main line, design 51 §2.2).

Differences from `backend/alembic/env.py` (the SQLite chain) that matter:

* **`compare_type=True`** — the PG chain is generated with `--autogenerate`, and
  design 58 §2.2 found real drift (BIGINT vs INTEGER for epoch-millisecond
  columns) that only a type comparison catches. The SQLite chain is historical
  and hand-written, so it does not need it.
* **No `render_as_batch`** — batch mode is the SQLite "rebuild the table" trick.
  On PG it turns every ALTER into a copy of the table (slow, and it drops
  indexes that were not recreated).
* **No PRAGMA listener** — `PRAGMA` is a SQLite-only statement; issuing it on PG
  raises `syntax error at or near "PRAGMA"` before anything is migrated.

URL resolution order (same as the SQLite chain, deliberately): `DATABASE_URL`
env override first, then the ini value. Tests pass the override; a bare CLI run
uses the ini. See the skill note in `database-schema-migration` §1 — a *stale*
`DATABASE_URL` in the shell silently redirects the run.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

from backend.app.core.database import Base
import backend.app.models  # noqa: F401  — populates Base.metadata

config = context.config

target_metadata = Base.metadata


def _get_url() -> str:
    return os.environ.get("DATABASE_URL", config.get_main_option("sqlalchemy.url"))


def run_migrations_offline() -> None:
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _get_url()
    if not url.startswith(("postgresql", "postgres")):
        #: 显式拒绝：PG 链跑到 SQLite 上会一半迁移失败、一半静默不生效
        #: （`Compare` 与索引方言参数都不同），留下一个看不出形状的库。
        raise RuntimeError(
            f"alembic_pg 只接受 PostgreSQL URL，当前是 {url.split('://', 1)[0]!r}。"
            "（SQLite 请用 backend/alembic.ini）"
        )

    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_schemas=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
