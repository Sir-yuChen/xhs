"""迁移顺序守卫 —— 「播种迁移必须晚于它要读写的所有列」 (v1.1.0 / S3)。

## 这条守卫为什么存在（两次真实事故）

播种迁移 `c3d4e5f6a1b2` 调 `seed_system_domain()`，而那个函数用的是**当前版本的
ORM 模型**（`User` 带 `email`/`phone`）。迁移链的执行时刻是**历史某一点**，
那一刻的表结构可能比模型少列。实测踩了两次：

| 事故 | 表现 | 原因 |
|---|---|---|
| ① 模板行 | `NOT NULL constraint failed: role_menus.user_id` | 播种无条件插 `user_id=NULL`，而当时列还 `NOT NULL` |
| ② email | `table users has no column named email` | `admin` 种子读写 `User.email`，而 `email` 列由**更晚**的迁移加出 |

两次都是同类：**播种代码用到了比它自己更新的 schema**。

## 守卫怎么判

不靠人工读迁移历史，而是**静态解析迁移链**：

1. 找出所有「会调用 `seed_*` / 写数据」的迁移；
2. 找出所有「加列 / 建表」的迁移，记录每列出现在哪一条；
3. 断言：若某条播种迁移读写的列由另一条迁移加出，则**加列那条必须在它之前**。

实现上不追求通用 DDL 解析（那会变成半个 alembic），只覆盖本项目真实用到的形态：
`op.add_column("表", sa.Column("列", ...))` 与 `batch.add_column(...)`。

⚠️ **不要把它写成「跳过检查」的软判据**：这条守卫在第一次就抓到了顺序问题，
如果它允许「查不到就放过」，那两次事故都抓不到。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration

pytestmark = pytest.mark.redline

VERSIONS_DIR = Path(__file__).resolve().parents[2] / "backend" / "alembic" / "versions"

#: 播种迁移：文件名 → 它依赖的表列（人工登记，改动这里必须同时改迁移）。
#: ⚠️ 这份清单是**声明**，守卫会验证它描述的顺序真的成立 —— 而不是让守卫去猜。
#:
#: ⚠️ **必须登记「模型有、但可能还没建出来」的全部列**，不能只登记「迁移自己会写的列」：
#: 第一次写这份清单时只列了 username/password_hash/... 7 列，故意漏掉 `email`/`phone`
#: ⇒ 把迁移顺序改回出错形态，守卫**居然还是绿的**（变异 ESCAPED，哑弹）。
#: 原因：播种走的是**模型**（`User` 对象 / 表级 insert 的默认列展开），
#: 模型上有的列它都可能碰到，所以登记面 = 模型列 ∩ 播种会读写的表。
SEED_MIGRATIONS = {
    "c3d4e5f6a1b2_seed_system_domain.py": {
        "reads": {
            "users": {
                # 显式写入的
                "username", "password_hash", "role", "status",
                "must_change_password", "token_version", "created_at",
                # ⚠️ 模型上带 Python default / NOT NULL 的列：即使用 `text()` 只写上面几列，
                # 读回 `User` 对象（`db.get(User, id)`）时也会 SELECT 它们 ⇒ 表里必须已有。
                # 实测事故：`no such column: users.email`（INSERT 成功、读回时炸）。
                "email", "phone", "real_name", "last_login_ip",
            },
            "roles": {"code", "name", "description", "is_system", "status", "sort_order"},
            "menus": {"parent_id", "name", "menu_type", "perms", "path",
                      "component", "icon", "workspace", "sort_order",
                      "is_visible", "status"},
            "role_menus": {"user_id", "role_id", "menu_id"},
            "dict_types": {"code", "name", "status", "remark"},
            "dict_items": {"type_id", "type_code", "label", "value",
                           "list_class", "sort_order", "status", "remark"},
        },
        "note": "调 seed_system_domain()：roles / admin / menus / dict / grants",
    },
}


def _parse_migrations() -> dict[str, dict]:
    """解析每个迁移文件：revision / down_revision / 它加了哪些列。"""
    import ast

    out: dict[str, dict] = {}
    if not VERSIONS_DIR.is_dir():
        return out

    for path in VERSIONS_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")

        revision = _literal_assignment(text, "revision")
        down = _literal_assignment(text, "down_revision")
        if not revision:
            continue

        #: 加列两种形态：
        #:   ① `op.add_column("users", sa.Column("email", ...))` —— 有表名
        #:   ② `batch.add_column(sa.Column("email", ...))` —— **没有表名**，
        #:      表名在 `batch_alter_table("users")` 里。
        #:
        #: ⚠️ 第一次写只处理了 ①，而 `e5f6a1b2c3d4`（加 email）用的是 ② ⇒
        #: `email` 被当成「没人加过」，守卫对真实的顺序错误**静默放过**
        #: （实测：变异 ESCAPED）。故 ② 必须解析成「batch 所属的表」，
        #: 拿不到就记通配，宁可宽一点也不能漏。
        added: set[tuple[str, str]] = set()
        for match in re.finditer(
            r"add_column\(\s*[\"'](?P<table>\w+)[\"']\s*,\s*(?:sa\.)?Column\(\s*[\"'](?P<col>\w+)[\"']",
            text,
        ):
            added.add((match.group("table"), match.group("col")))

        #: batch 形态：先找所有 `batch_alter_table("表")` 的位置，再给其后的
        #: `add_column(sa.Column("列"))` 归到「最近的那个 batch 表」。
        batch_tables = [
            (m.start(), m.group("table"))
            for m in re.finditer(r"batch_alter_table\(\s*[\"'](?P<table>\w+)[\"']", text)
        ]
        for match in re.finditer(
            r"add_column\(\s*(?:sa\.)?Column\(\s*[\"'](?P<col>\w+)[\"']", text
        ):
            pos = match.start()
            owner = None
            for start, table in batch_tables:
                if start < pos:
                    owner = table
            added.add((owner or "*", match.group("col")))

        out[revision] = {
            "file": path.name,
            "down": down,
            "added": added,
        }
    return out


def _literal_assignment(text: str, name: str) -> str | None:
    match = re.search(rf"^{name}\s*=\s*[\"']([^\"']+)[\"']", text, re.MULTILINE)
    return match.group(1) if match else None


def _order(migrations: dict[str, dict]) -> list[str]:
    """按 `down_revision` 做**拓扑排序**，返回从旧到新的 revision 列表（Kahn 算法）。

    ⚠️ 试过两种错法（都实测会把顺序排错）：
      ① DFS 前序遍历 —— 并行分支的整棵子树插到中间（播种排第 16、加 email 排第 27）；
      ② 按 `depth`（沿 down 走几步）排序 —— 并行分支上同深度会被任意打散
         （播种 44、email 37 ⇒ 仍判成「播种更晚」）。
    正解 = Kahn：每轮取出「所有父都已出队」的节点。链上有并行分支 / 合并点时
    它给出的顺序满足**每一条依赖边**，这正是本守卫需要的语义。
    """
    #: 父集合：`down_revision` 可能是逗号分隔的多父（合并迁移）
    parents: dict[str, set[str]] = {}
    for rev, info in migrations.items():
        raw = str(info["down"] or "")
        names = {p.strip() for p in raw.split(",") if p.strip()}
        parents[rev] = {n for n in names if n in migrations}

    ordered: list[str] = []
    remaining = dict(parents)
    while remaining:
        ready = sorted(r for r, deps in remaining.items() if not deps)
        if not ready:
            #: 环 / 父指向不存在的 revision ⇒ 剩下的按名字追加（不静默丢）
            ordered.extend(sorted(remaining))
            break
        for rev in ready:
            ordered.append(rev)
            remaining.pop(rev)
        for deps in remaining.values():
            deps.difference_update(ready)
    return ordered


def test_seed_migration_comes_after_the_columns_it_uses():
    """播种迁移读写到的列，必须由**更早**的迁移加出。

    ⚠️ 这是 `no such column: users.email` 那类事故的唯一机械判据 ——
    人工读迁移历史时，「播种在加列之前」这种顺序错误**极难看出来**
    （文件名按日期排，看不出依赖方向）。
    """
    migrations = _parse_migrations()
    if not migrations:
        pytest.skip(f"没找到迁移目录：{VERSIONS_DIR}")

    chain = _order(migrations)
    position = {rev: index for index, rev in enumerate(chain)}

    problems: list[str] = []
    for filename, spec in SEED_MIGRATIONS.items():
        rev = next(
            (r for r, info in migrations.items() if info["file"] == filename), None
        )
        if rev is None:
            problems.append(f"登记的播种迁移不存在：{filename}")
            continue
        seed_pos = position.get(rev)
        if seed_pos is None:
            problems.append(f"{filename} 不在迁移链上（孤立 revision？）")
            continue

        for table, columns in spec["reads"].items():
            for column in columns:
                #: 找出「加出这一列」的迁移里**最晚**的那条
                producers = [
                    (position.get(r, -1), r, info["file"])
                    for r, info in migrations.items()
                    if (table, column) in info["added"] or ("*", column) in info["added"]
                ]
                if not producers:
                    continue  #: 列由 baseline / 建表迁移一次给出，不在本守卫范围
                latest_pos, latest_rev, latest_file = max(producers)
                if latest_pos > seed_pos:
                    problems.append(
                        f"{filename} 读写 {table}.{column}，但该列由**更晚**的 "
                        f"{latest_file}（{latest_rev}）加出 ⇒ 播种会报 no such column"
                    )

    assert not problems, (
        "迁移顺序错误（播种早于它用到的列）：\n  " + "\n  ".join(problems)
    )


def test_migration_chain_has_no_duplicate_revision():
    """revision id 不得重复（重复会让 alembic 报 `present more than once`）。"""
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for path in VERSIONS_DIR.glob("*.py"):
        rev = _literal_assignment(path.read_text(encoding="utf-8", errors="replace"), "revision")
        if not rev:
            continue
        if rev in seen:
            duplicates.append(f"{rev}: {seen[rev]} 与 {path.name}")
        seen[rev] = path.name
    assert not duplicates, "revision id 重复：\n  " + "\n  ".join(duplicates)


def test_seed_registry_is_not_empty():
    """守卫自检：登记表为空时上面的判据恒真（假绿）。"""
    assert SEED_MIGRATIONS, "SEED_MIGRATIONS 不能为空，否则守卫是空转"
