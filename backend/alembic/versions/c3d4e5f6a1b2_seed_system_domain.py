"""seed system domain: roles 2 + menus 174 + dict 12 groups + admin user + grants

v1.1.0 / 设计 47 §6 + 设计 57 S1 迁移 3 of 3。

**播种逻辑不写在这里**：唯一实现在 `services/menu_seed_service.py`
（启动期也要播，两处各写一份 = 必然漂移，DEF-044）。本迁移只负责「在
`upgrade head` 的那一刻调它一次并提交」。

为什么迁移里也播一次（而不是只靠启动期）：
* 迁移是**可重复执行的代码**，它的存在让「刚 upgrade 完的库」就是可用的
  （不必先启动一次应用才有 admin / 菜单）；
* 而启动期那次覆盖的是 `create_all` 建库的路径（测试库 / PG 基线），
  以及「用户在菜单管理里删了 admin 之后不会再补」的语义。

两者都调同一个函数、都遵守**只补缺不覆盖** ⇒ 重复执行是幂等的。

⚠️ 用 **ORM Session** 而不是 `op.execute(...)`：`menus` 的判重键涉及
`parent_id` 与自增 id 的映射关系，用裸 SQL 写一遍就是第二份实现。
ORM 走的是与应用完全相同的列定义。

Revision ID: c3d4e5f6a1b2
Revises: b2c3d4e5f6a0
"""

from __future__ import annotations

import json
import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

revision = "c3d4e5f6a1b2"
down_revision = "b2c3d4e5f6a0"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def _seed_payload(filename: str) -> dict | None:
    from backend.app.core.paths import app_root

    path = app_root() / "config" / "initial_data" / filename
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def upgrade() -> None:
    #: 惰性 import：`alembic` 的 `env.py` 已经把 `backend.app.models` 载入，
    #: 但服务层会拉进 `core.security` 等更多东西。放在函数内，
    #: 让「只跑 alembic」与「被 pytest 单独 import」只有一条装配路径。
    from backend.app.services.menu_seed_service import seed_system_domain

    session = Session(bind=op.get_bind())
    try:
        created = seed_system_domain(session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    logger.info("S1 种子：%s", created)


def downgrade() -> None:
    """删掉**种子行本身**（用户自建 / 改过的行保留）。

    判据是「这一行还是不是种子」：
    * `role_menus` 全清（授权行只是种子派生，且 `roles` 要删）；
    * `roles` 只删 `is_system` 的 2 行；
    * `menus` / `dict_items` / `dict_types` 按种子文件的 identity **逐条 DELETE**，
      且**带上「其余字段未被改动」的条件** —— 用户在菜单管理里改过 `status` /
      `name` 的行不匹配 ⇒ 保留（回滚不该把用户的修改一起抹掉）；
    * 出厂 `admin` **不删**：删账号 = 删数据，回滚一个结构迁移不该带走一个账号，
      而且「这个账号是不是种子」在库层面不可判定。
    """
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "role_menus" in tables:
        bind.execute(sa.text("DELETE FROM role_menus"))
    #: ⚠️ `roles` 可能已被迁移 1 的 downgrade 删掉（见下方 dict 段的同型说明）。
    if "roles" in tables:
        bind.execute(sa.text("DELETE FROM roles WHERE is_system = 1"))

    menus_seed = _seed_payload("menus.seed.json")
    if menus_seed and "menus" in tables:
        for item in menus_seed.get("menus") or []:
            params = {
                "name": str(item.get("name") or ""),
                "menu_type": str(item.get("menu_type") or ""),
                "status": str(item.get("status") or ""),
                "sort_order": int(item.get("sort_order") or 0),
            }
            sql = (
                "DELETE FROM menus WHERE name = :name AND menu_type = :menu_type "
                "AND status = :status AND sort_order = :sort_order"
            )
            perms = str(item.get("perms") or "")
            path = str(item.get("path") or "")
            if perms:
                sql += " AND perms = :perms"
                params["perms"] = perms
            elif path:
                sql += " AND path = :path"
                params["path"] = path
            else:
                sql += " AND perms = '' AND path = ''"
            bind.execute(sa.text(sql), params)

    dict_seed = _seed_payload("dict_seed.json")
    #: ⚠️ 表可能已经不在了：`downgrade(-1)` 走的是迁移 3 → 2 → 1，
    #: 迁移 2 的 downgrade 会 `drop_table("dict_items")`；而本函数是在**迁移 3 的
    #: downgrade 里**执行的（先于迁移 2），所以顺序上表还在 —— 但如果有人手工
    #: `downgrade 2 次` 后重跑，或者表被外部删掉，DELETE 会 `no such table` 直接炸。
    #: 判据：表存在才删（这是**结构存在性**判据，不是「有没有数据」判据，不属 DEF-048 形态）。
    if dict_seed:
        if "dict_items" in tables:
            for item in dict_seed.get("types") or []:
                code = str(item.get("code") or "")
                for entry in item.get("items") or []:
                    bind.execute(
                        sa.text(
                            "DELETE FROM dict_items WHERE type_code = :code AND value = :value"
                        ),
                        {"code": code, "value": str(entry.get("value") or "")},
                    )
        if "dict_types" in tables:
            for item in dict_seed.get("types") or []:
                bind.execute(
                    sa.text("DELETE FROM dict_types WHERE code = :code"),
                    {"code": str(item.get("code") or "")},
                )
