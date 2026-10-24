"""种子播种（v1.1.0 系统管理域）—— **唯一实现**，迁移与启动期都调它。

## 三份种子（设计 47 §6）

| 种子 | 行数 | 来源 | 判重键 |
|---|---|---|---|
| `roles` | 2（`admin` / `user`） | 本文件 `ROLE_SEEDS` | `code` |
| `menus` | 174（M 12 / C 29 / F 133） | `config/initial_data/menus.seed.json`（**生成物**） | `perms` 非空按 `perms`；`path` 非空按 `path`；两者都空（M 行）按 `(parent_id, name)` |
| `dict_types` / `dict_items` | 12 组 / 57 项 | `config/initial_data/dict_seed.json` | `code` / `(type_id, value)` |
| `role_menus` | admin = 全量；user = 只读集合 | 由前两者派生 | `(role_id, menu_id)` |
| `users` | 1（`admin` / `admin123`） | 本文件 `ADMIN_SEED` | 仅当**系统中没有任何可用 admin** |

## 纪律：**只补缺不覆盖**

每一行都是「已存在 ⇒ `continue`」，绝不 `UPDATE`。理由是这些表在装好之后
是**用户的数据**：

* 在「菜单管理」里把「抖音」改成启用 ⇒ 下次升级不能被打回 `disabled`；
* 在「角色管理」里给 `user` 角色加授权 ⇒ 下次升级不能把它清回种子集合；
* 在「用户管理」里改了某个用户名 ⇒ 升级不能覆盖。

代价要说明白：**种子的后续改动不会重新同步到已播种的库**（新装 / 空库才有）。
这是刻意的取舍（同 `forbidden_words` / `data_package_rules` 的既有纪律）。

⚠️ admin 种子的额外两条判据：
* 它只在 `admin` 行**不存在**时插入 —— 用户删掉了 admin（或改名）就**不重建**，
  否则「删了又回来」比没有更难理解；
* `must_change_password=True` + `register.enabled=false`（防「默认口令 + 开放注册」
  同时成立，设计 46 §395 / 设计 50 §261 的口径）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import func, inspect as _sa_inspect, select, text
from sqlalchemy.orm import Session

from backend.app.core.roles import ROLE_ADMIN, ROLE_USER
from backend.app.core.time import shanghai_now
from backend.app.models.dict import DICT_STATUS_NORMAL, DictItem, DictType
from backend.app.models.menu import MENU_STATUS_NORMAL, Menu
from backend.app.models.role import ROLE_STATUS_NORMAL, Role
from backend.app.models.role_menu import RoleMenu
from backend.app.models.user import USER_STATUS_NORMAL, User

logger = logging.getLogger(__name__)

#: 内置角色（设计 47 §2.2）。`is_system=True` ⇒ 禁删 / 禁停 / 禁改 code / 禁改授权。
ROLE_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "code": ROLE_ADMIN,
        "name": "超级管理员",
        "description": "全部权限（代码派生全量，见 core/perms.role_perms）",
        "is_system": True,
        "sort_order": 0,
    },
    {
        "code": ROLE_USER,
        "name": "普通用户",
        "description": "默认角色，按授权行取权限",
        "is_system": True,
        "sort_order": 10,
    },
)

#: admin 种子（设计 47 §6）。
#:
#: ## ⚠️ 2026-10-24：口令从**固定 `admin123`** 改为**每次随机生成**
#:
#: 原设计是「固定弱口令 + `must_change_password=True` 首登强制改密」。实际风险：
#:
#: * `admin123` 写在设计文档、部署 README、种子代码里 ⇒ **等于公开**；
#: * 「首登强制改密」只发生在**第一次登录之后** —— 在那之前任何人扫到端口
#:   就能用公开口令登录，而首装环境通常还绑着 `0.0.0.0`；
#: * 桌面版退役后是**服务器多用户**形态，攻击面比单机大得多。
#:
#: 改法：口令在**播种那一刻**随机生成（16 位，含大小写数字符号），只输出到
#: 安装日志 / stdout（**不进数据库明文、不进文档、不回显到任何 API**）。
#: 运维在首装时从日志里取一次，登录后 `must_change_password` 仍要求改密。
#:
#: ⚠️ 为什么不直接「首装不建账号 + 走注册审批」：注册默认关闭（同一条安全基线），
#: 那会导致**全新安装没有任何人能登录**，自己把自己锁死。随机口令是两难中的解。
ADMIN_SEED: dict[str, Any] = {
    "username": "admin",
    #: 空串 = 「播种时随机生成」。**不要**在这里写固定口令。
    "password": "",
    "real_name": "超级管理员",
    "role": ROLE_ADMIN,
}

def generate_admin_password(length: int = 16) -> str:
    """生成出厂管理员口令（播种时调用一次）。

    ## 为什么随机而不是固定

    固定口令（原为 `admin123`）写在**设计文档、部署 README、本文件**里 ⇒
    等于公开。配合「首登强制改密」看似安全，但那道闸只在**第一次登录之后**生效 ——
    在它之前，任何扫到端口的人都能用公开口令登进来，而首装环境通常还绑着 `0.0.0.0`。

    桌面版退役后是**服务器多用户**形态，这个风险比单机大得多。

    ## 为什么不用「首装不建账号」

    注册默认关闭（同一条安全基线），那会让全新安装**没有任何人能登录** ——
    自己把自己锁死。随机口令是这两难之间的解。

    ## 口令去向

    只写进**安装日志 / stdout**（`logger.warning` 一行），供运维首装时取一次。
    ⚠️ 不进数据库明文（存的是 hash）、不进文档、不回显到任何 API。
    `must_change_password=True` 仍要求登录后立刻改密。

    字符集刻意去掉了 `0/O/1/l/I` —— 运维要从日志里**手抄**这串字，
    形近字符是实际会抄错的地方。
    """
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#%^*-_+="
    import secrets

    return "".join(secrets.choice(alphabet) for _ in range(length))



#: 种子文件名（`config/initial_data/` 下）
MENUS_SEED_FILENAME = "menus.seed.json"
DICT_SEED_FILENAME = "dict_seed.json"


def _seed_dir() -> Path:
    """`config/initial_data/` —— 与 `core/initial_data.py` 同口径（`app_root()`）。

    ⚠️ 不用 CWD 相对路径：服务器 / 安装版的 CWD 不是仓库根
    （`core/paths.py` 模块 docstring 记着这个坑）。
    """
    from backend.app.core.paths import app_root

    return app_root() / "config" / "initial_data"


def _load_json(filename: str) -> dict[str, Any] | None:
    path = _seed_dir() / filename
    if not path.is_file():
        logger.warning("seed: %s 不存在（%s）—— 本次不播种", filename, path)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:  # noqa: BLE001 —— 坏数据不该让启动失败
        logger.warning("seed: %s 解析失败（%s: %s）", path, type(exc).__name__, exc)
        return None


# --------------------------------------------------------------------------- #
# 一、角色
# --------------------------------------------------------------------------- #
def seed_roles(db: Session) -> list[Role]:
    """补种内置角色（按 `code` 判重）。返回**本次新建**的行。"""
    created: list[Role] = []
    existing = {code for (code,) in db.execute(select(Role.code)).all()}
    for item in ROLE_SEEDS:
        if item["code"] in existing:
            continue
        row = Role(
            code=item["code"],
            name=item["name"],
            description=item["description"],
            is_system=item["is_system"],
            status=ROLE_STATUS_NORMAL,
            sort_order=item["sort_order"],
        )
        db.add(row)
        created.append(row)
    if created:
        db.flush()
    return created


# --------------------------------------------------------------------------- #
# 二、菜单
# --------------------------------------------------------------------------- #
def _identity_of(row: Menu) -> tuple:
    """菜单行的判重键。

    ⚠️ **2026-10-24 修订：`path` 优先于 `perms`**（原来反了）。

    原顺序（`perms` 优先）的理由是「F 行没有 path，只能按 perms 判」。但那个理由
    只说明 **F 行**需要按 perms 判，不能推出「C 行也要先看 perms」。反例是真实存在的：
    **页内页签**（`/platforms/xhs/note-workshop?tab=images` 等 6 行）与宿主
    `/platforms/xhs/note-workshop` **共用同一个查看码** `xhs:workshop:view`
    （页签是同一页面的不同视图，不该各自要一个码）—— 按 perms 判重会把 6 个页签
    全当成宿主的重复而**跳过不播**（实测：种子 179 行，库里只落 173 行，侧栏丢掉
    图片/视频/草稿工坊等 6 个入口，由守卫 `test_three_workshops_merge_into_one_page_with_tabs` 抓到）。

    新顺序：
    1. **C 行有 path** ⇒ 按 path（path 有非空部分唯一索引，且它是「这一行是哪一页」的唯一标识）；
    2. F 行（无 path、有 perms）⇒ 按 perms；
    3. M 行（两者都空）⇒ 按 `(parent_id, name)`。

    副作用不变：改过名字的 M 行升级后会再种一行（M 行只有 11 条，改名远比「菜单凭空消失」少见）。
    """
    if row.path:
        return ("path", row.path)
    if row.perms:
        return ("perms", row.perms)
    return ("name", row.parent_id, row.name)


def seed_menus(db: Session) -> list[Menu]:
    """补种菜单树（设计 47 §6 / 57 S1 迁移 3）。

    ⚠️ **种子里没有数据库主键**。`menus.seed.json` 的每行带 `_seq`（生成器分配的
    临时序号）与 `parent_seq`（父的临时序号）。理由：把种子 `id` 当主键用就是
    **第二份 id 真相源** —— 它会与既有库的自增序列打架（`INSERT id=1` 撞已有行，
    或把序列推后 174）。种子只负责**结构**，主键由 DB 分配。

    ⇒ 播种流程：按 `parent_seq` 分层插父 → `flush()` 取回真实 id → 解析子行。
    判重仍按 `perms` / `path` / `(parent_real, name)`（见模块 docstring）。
    """
    payload = _load_json(MENUS_SEED_FILENAME)
    if not payload:
        return []
    rows: list[dict[str, Any]] = [dict(item) for item in (payload.get("menus") or [])]
    if not rows:
        return []

    # 0) 种子自检：`_seq` 唯一、`parent_seq` 都指向存在的行
    #    （生成器坏了就到这里炸，而不是建出一棵少了几支的树）
    seqs = [int(item.get("_seq") or 0) for item in rows]
    if len(set(seqs)) != len(seqs) or 0 in seqs:
        raise RuntimeError("menus.seed.json 的 `_seq` 必须唯一且非 0")
    known = set(seqs)
    for item in rows:
        pid = int(item.get("parent_seq") or 0)
        if pid and pid not in known:
            raise RuntimeError(f"menus.seed.json 里 parent_seq={pid} 不存在（孤儿行）")

    # 1) 现有行建索引（三种判重键）
    by_perms: dict[str, Menu] = {}
    by_path: dict[str, Menu] = {}
    by_name: dict[tuple[int, str], Menu] = {}
    for row in db.scalars(select(Menu)).all():
        if row.perms:
            by_perms[row.perms] = row
        if row.path:
            by_path[row.path] = row
        by_name[(row.parent_id, row.name)] = row

    created: list[Menu] = []
    seq_to_real: dict[int, int] = {}

    #: **用户删过 ⇒ 不复活**（设计 49「删了又回来」的落地判据）。
    #:
    #: 种子只负责「建出初始的树」，它不能区分「这一行还没建过」和「用户删掉了它」——
    #: 而「下次播种把删掉的菜单加回来」正是设计要避免的那类行为（同
    #: `forbidden_words` 的「删了不复活」纪律）。
    #:
    #: ⚠️ 判据**必须有「树已经种过」这个前置**：全新库上「父 M 行下面一条 C 行都没有」
    #: 同样成立 —— 若只看这一点，空库播种会把**所有 C 行**都跳过（实测：0 条 C 行，
    #: 菜单树只剩目录壳）。所以前置 = 已存在**任何** C 行（树种过）。
    #:
    #: 判据（三者同时成立才算「用户清空了这个目录」）：
    #: ① 库里已有若干 C 行（树种过，不是空库）；
    #: ② 该父 M 行还在；
    #: ③ 种子给该父 M 行安排的 C 行里，**一条都不在库里**。
    #:
    #: 为什么不逐行判断：单条 C 行被删、同目录其它 C 行还在，是合法状态（用户只是
    #: 不要那一页），但种子无从区分「删了」与「从没建过」；而删一条 C 行之前必须先
    #: 撤它的授权（409 `MENU_IN_USE`），代价比补一条菜单高得多 ⇒ 那种情况**允许补回**。
    #: 整目录清空才是强信号。
    seeded_c_by_parent: dict[int, set[str]] = {}
    #: `_seq` → 种子行（`skip_parent` 要判断「父是不是 M 行」）
    seed_by_seq: dict[int, dict[str, Any]] = {}
    for item in rows:
        seed_by_seq[int(item.get("_seq") or 0)] = item
        if str(item.get("menu_type") or "") == "C":
            seeded_c_by_parent.setdefault(int(item.get("parent_seq") or 0), set()).add(
                str(item.get("path") or "")
            )
    existing_c_by_parent: dict[int, set[str]] = {}
    for row in db.scalars(select(Menu)).all():
        if row.menu_type == "C":
            existing_c_by_parent.setdefault(row.parent_id, set()).add(row.path)
    tree_already_seeded = bool(existing_c_by_parent)

    def skip_parent(seq: int) -> bool:
        """该 **M 行（目录）** 下**一条种子 C 行都没了**（且树已种过）⇒ 用户清空过。

        ⚠️ **只对 M 行生效**（2026-10-24 修订）。原来不判类型，于是
        「C 行作为父」也会走这条路径 —— 而**页内页签**的父正是 C 行
        （`/platforms/xhs/note-workshop?tab=images` 的父是笔记工坊这个 C 行）：
        该父名下**全部**种子 C 行就是 6 个页签，库里却是 0 个
        ⇒ 判定「整目录被清空」⇒ 6 个页签全不播（实测：种子 179、库里 173，
        侧栏丢掉图片/视频/草稿工坊等入口）。

        「整目录被清空就不复活」这条纪律本来只针对**用户删掉了整个目录下的页面**，
        页签不是目录下的独立页面，它没有「用户清空」这个语义。
        """
        if not seq or not tree_already_seeded:
            return False
        parent_seed_row = seed_by_seq.get(seq)
        if parent_seed_row is None or parent_seed_row.get("menu_type") != "M":
            return False
        real = seq_to_real.get(seq)
        if real is None:
            return False
        seeded = seeded_c_by_parent.get(seq) or set()
        if not seeded:
            return False
        existing = existing_c_by_parent.get(real) or set()
        #: 注意用 `&`：用户可能自建了别的 C 行挂在这里，那不算「清空」
        return not (seeded & existing)

    #: 被跳过的种子行 _seq 集合 —— 它们的**子行也要跟着跳过**。
    #: ⚠️ 少了这一层，被跳过的 C 行下面的 F 行（按钮）会以 `parent_real` = 父 id 落地，
    #: 变成挂在 M 行（目录）下的孤儿按钮（实测：清空「内容线」后重播，38 条按钮
    #: 挂到了目录节点上）。「父被跳过 ⇒ 整棵子树都不建」必须顺着链传下去。
    skipped_seqs: set[int] = set()

    # 2) **显式分轮**（父 → 子 → 孙）：不依赖种子里的行序。
    #    依赖顺序是隐式契约 —— 改一次生成脚本的排序就会静默建错树。
    remaining = list(rows)
    while remaining:
        progressed = False
        deferred: list[dict[str, Any]] = []
        for item in remaining:
            parent_seq = int(item.get("parent_seq") or 0)
            if parent_seq and parent_seq not in seq_to_real:
                deferred.append(item)
                continue
            parent_real = seq_to_real[parent_seq] if parent_seq else 0

            perms = str(item.get("perms") or "")
            path = str(item.get("path") or "")
            name = str(item.get("name") or "")

            #: 父被跳过 ⇒ 整棵子树都不建（自己也不是例外），由父行「登记成不存在」表达
            if parent_seq and parent_seq in skipped_seqs:
                skipped_seqs.add(int(item["_seq"]))
                progressed = True
                continue

            #: 父目录被用户清空过 ⇒ 其下全部 C/F 不再重建。
            #: ⚠️ 这里也要登记 `seq_to_real`：否则子行会**永远**进 `deferred`
            #: （父链「断开」的假象，实测报 `有环或父链断开`）。
            if str(item.get("menu_type") or "") in ("C", "F") and skip_parent(parent_seq):
                seq_to_real[int(item["_seq"])] = parent_real
                skipped_seqs.add(int(item["_seq"]))
                progressed = True
                continue

            candidate: Menu | None = None
            #: ⚠️ **判重顺序必须与 `_identity_of` 一致：path 优先于 perms**
            #: （2026-10-24）。这里曾是独立的一份实现，改了 `_identity_of` 却没改它
            #: ⇒ 页签（与宿主共用 `xhs:workshop:view`）仍被当成宿主的重复而跳过。
            #: 同一件事两份实现必然漂移（DEF-044）—— 两处都按 path → perms → name。
            if path and path in by_path:
                candidate = by_path[path]
            elif perms and perms in by_perms:
                candidate = by_perms[perms]
            elif not perms and not path and (parent_real, name) in by_name:
                candidate = by_name[(parent_real, name)]

            if candidate is not None:
                # **只补缺不覆盖**：存在就什么都不改（用户可能改过 name / status / 排序）
                seq_to_real[int(item["_seq"])] = candidate.id
                progressed = True
                continue

            row = Menu(
                parent_id=parent_real,
                name=name,
                menu_type=str(item.get("menu_type") or "C"),
                path=path,
                component=str(item.get("component") or ""),
                perms=perms,
                icon=str(item.get("icon") or ""),
                workspace=str(item.get("workspace") or ""),
                sort_order=int(item.get("sort_order") or 0),
                is_visible=bool(item.get("is_visible", True)),
                status=str(item.get("status") or MENU_STATUS_NORMAL),
            )
            db.add(row)
            db.flush()  # 立刻拿真实 id：子行要引用
            seq_to_real[int(item["_seq"])] = row.id
            created.append(row)
            if row.perms:
                by_perms[row.perms] = row
            if row.path:
                by_path[row.path] = row
            by_name[(row.parent_id, row.name)] = row
            progressed = True
        if not progressed:
            stuck = sorted(int(item.get("_seq") or 0) for item in deferred)
            raise RuntimeError(
                f"menus.seed.json 有环或父链断开（无法解析的行 _seq={stuck}）"
            )
        remaining = deferred

    return created


# --------------------------------------------------------------------------- #
# 三、字典
# --------------------------------------------------------------------------- #
def seed_dicts(db: Session) -> tuple[list[DictType], list[DictItem]]:
    """补种字典（按 `code` / `(type_id, value)` 判重）。"""
    payload = _load_json(DICT_SEED_FILENAME)
    if not payload:
        return [], []

    types_by_code: dict[str, DictType] = {t.code: t for t in db.scalars(select(DictType)).all()}
    created_types: list[DictType] = []
    created_items: list[DictItem] = []

    for item in payload.get("types") or []:
        code = str(item.get("code") or "")
        if not code:
            continue
        type_row = types_by_code.get(code)
        if type_row is None:
            type_row = DictType(
                code=code,
                name=str(item.get("name") or code),
                remark=str(item.get("remark") or ""),
                status=DICT_STATUS_NORMAL,
            )
            db.add(type_row)
            db.flush()
            types_by_code[code] = type_row
            created_types.append(type_row)

        existing_values = {
            value
            for (value,) in db.execute(
                select(DictItem.value).where(DictItem.type_id == type_row.id)
            ).all()
        }
        for order, entry in enumerate(item.get("items") or []):
            value = str(entry.get("value") or "")
            if not value or value in existing_values:
                continue
            row = DictItem(
                type_id=type_row.id,
                #: 冗余列**由 type_id 派生**，不接受种子里的值（防两份真相）
                type_code=type_row.code,
                label=str(entry.get("label") or value),
                value=value,
                list_class=str(entry.get("list_class") or "default"),
                sort_order=int(entry.get("sort_order", order * 10)),
                status=DICT_STATUS_NORMAL,
                remark=str(entry.get("remark") or ""),
            )
            db.add(row)
            created_items.append(row)
            existing_values.add(value)

    return created_types, created_items


# --------------------------------------------------------------------------- #
# 四、授权（role_menus）
# --------------------------------------------------------------------------- #
def seed_role_menus(db: Session) -> list[RoleMenu]:
    """给两个内置角色补授权行 —— **只补缺不覆盖**。

    | 角色 | 授权范围 |
    |---|---|
    | `admin` | **全量**菜单行（`core/perms.role_perms("admin")` 虽然直接返回
      `ALL_PERMS`，但授权表仍要全量：菜单树是 `role_menus` 驱动的，
      少了行 ⇒ 超级管理员在「分配菜单」页看到自己是空前授权，会误以为自己没权限） |
    | `user` | **只读集合**：所有 `menu_type="C"` 且 `perms` 以 `:view` 结尾的行，
      加上它们的祖先 M 行。即「进得去所有页、看不到任何按钮」—— 最小权限，
      与 `core/roles.DEFAULT_ROLE` 的注释「新的权限默认不开放」一致 |

    每条授权行分两层（2026-10-24 修订，设计 57 S1 实测反馈）：

    | 层 | `user_id` | 语义 |
    |---|---|---|
    | **模板行** | `NULL` | 「持有该角色的人**默认**看得见什么」。新用户注册即继承，无需逐个播种 |
    | **用户行** | 具体 id | 该用户的**增量**（显式加授 / 显式取消的差异） |

    ⚠️ **初版只播用户行、不播模板行**（`user_id IS NULL` 为 0 行），实测后果：
    新注册用户 `perms_for_role("user")` 返回 0 码 ⇒ 登录后**看不到任何页面**
    （侧栏空白、直输 URL 403）。「新用户默认可见」必须落成模板行，
    否则每来一个新用户都要补一次播种，而那一步永远会被忘掉。
    """
    roles = {r.code: r for r in db.scalars(select(Role)).all()}
    if not roles:
        return []

    menus = db.scalars(select(Menu)).all()
    if not menus:
        return []
    menu_by_id = {m.id: m for m in menus}

    def ancestors_of(row: Menu) -> set[int]:
        """自己 + M 祖先链（祖先链断了 ⇒ 侧栏不显示 ⇒ 授权行必须一起给）。"""
        chain = {row.id}
        cursor = row
        while cursor.parent_id and cursor.parent_id in menu_by_id:
            chain.add(cursor.parent_id)
            cursor = menu_by_id[cursor.parent_id]
        return chain

    admin_menu_ids: set[int] = set(menu_by_id)
    user_menu_ids: set[int] = set()
    for row in menus:
        if row.menu_type == "C":
            user_menu_ids |= ancestors_of(row)

    created: list[RoleMenu] = []

    #: ⚠️ **先把前几轮（角色 / admin / 菜单 / 字典）的待插入行 flush 下去**。
    #: 不 flush 的话，下面第一轮里的 `db.execute(select(RoleMenu...))` 会触发
    #: **autoflush**，把这批 `RoleMenu` 插进库；第二轮再按用户查出来时它们已在库里，
    #: 但 `admin` 与 `user` 两个角色的行被**同一个 user_id** 覆盖时会撞
    #: `UNIQUE(role_id, menu_id)` —— 实测就是这个 `IntegrityError`。
    db.flush()

    #: 本次已排队的授权行（用户, 角色, 菜单）—— 内存里也判重，不只依赖查库：
    #: `db.add` 之后同一事务内 SELECT 看不到未 flush 的行，只查库会把同一行加两遍。
    queued: set[tuple[int | None, int, int]] = set()

    def ensure(owner: int | None, role_id: int, menu_ids: set[int]) -> None:
        """给 `owner`（`None` = 模板行）补齐 `menu_ids`，只补缺不覆盖。"""
        existing = {
            mid
            for (mid,) in db.execute(
                select(RoleMenu.menu_id).where(
                    RoleMenu.role_id == role_id,
                    RoleMenu.user_id.is_(None) if owner is None else RoleMenu.user_id == owner,
                )
            ).all()
        }
        for menu_id in sorted(menu_ids - existing, key=lambda v: (v is None, v or 0)):
            key = (owner, role_id, menu_id)
            if key in queued:
                continue
            queued.add(key)
            db.add(RoleMenu(user_id=owner, role_id=role_id, menu_id=menu_id))
            created.append(RoleMenu(user_id=owner, role_id=role_id, menu_id=menu_id))

    # ① **模板行**（无论有没有用户持有，都要播 —— 这是「新用户默认可见」的来源）
    #    ⚠️ 只有 `role_menus.user_id` 可空时才播：迁移 `c3d4e5f6a1b2`（播种）跑在
    #    `d4e5f6a1b2c3`（改可空）**之前**，那一刻列还是 NOT NULL ⇒ 硬插模板行会
    #    让整个 upgrade 炸掉。所以这里按**结构判据**退化：不可空就只播用户行
    #    （旧行为），等 `d4e5f6a1b2c3` 跑完 + 下次启动播种时再补模板行。
    template_ok = _role_menus_user_id_nullable(db)
    if template_ok:
        for code, menu_ids in (("admin", admin_menu_ids), ("user", user_menu_ids)):
            if code in roles:
                ensure(None, roles[code].id, menu_ids)
    else:
        logger.info(
            "seed: role_menus.user_id 仍是 NOT NULL（迁移 d4e5f6a1b2c3 之前）"
            "⇒ 本次不播模板行，只播用户行"
        )

    # ② 现有用户的**增量**：模板行已给了默认集合，这里只补模板覆盖不到的部分。
    #    当前两个内置角色的模板已覆盖全部应授菜单，所以这一步通常 0 条；
    #    留着是为了「某用户被单独加授过菜单」时能把缺的补上（幂等）。
    holders: dict[str, list[int]] = {}
    if _users_have_seed_columns(db):
        for user_id, role in db.execute(
            select(User.id, User.role).where(User.deleted_at.is_(None))
        ).all():
            holders.setdefault(str(role or ""), []).append(user_id)
    else:
        logger.warning(
            "seed: users 缺 role / deleted_at 列（手写最小夹具，如 test_initial_data 的 "
            "users 夹具）⇒ 本次不播用户级授权行（模板行照播）"
        )

    for code, menu_ids in (("admin", admin_menu_ids), ("user", user_menu_ids)):
        if code not in roles:
            continue
        for user_id in holders.get(code, []):
            ensure(user_id, roles[code].id, menu_ids)

    return created


# --------------------------------------------------------------------------- #
# 五、admin 种子
# --------------------------------------------------------------------------- #
def _users_have_seed_columns(db: Session) -> bool:
    """`users` 是否带齐种子判据需要的列（`role` / `deleted_at`）。

    ⚠️ **这是结构判据，不是数据判据**：真实库上这两列永远在（`deleted_at` 由迁移
    `a1b2c3d4e5f9` 加，`role` 是一期就有的），只有**手写最小夹具**会缺
    （`test_initial_data` 的 `users` 只有 id/username/password_hash/created_at）。
    缺列时种子退化为「不播 admin / 授权」，并在日志里说明 —— 而不是让整个迁移报
    `no such column`（那会让一个与夹具无关的迁移看起来像写坏了）。
    """
    from sqlalchemy import inspect as _inspect

    bind = db.get_bind()
    if not _inspect(bind).has_table("users"):
        return False
    cols = {c["name"] for c in _inspect(bind).get_columns("users")}
    return {"role", "deleted_at"} <= cols


def _role_menus_user_id_nullable(db: Session) -> bool:
    """`role_menus.user_id` 是否允许 NULL（= 能否承载「模板行」）。

    ⚠️ **结构判据**：迁移 `c3d4e5f6a1b2` 播种时表刚由 `a1b2c3d4e5f9` 建出，
    那一刻 `user_id` 仍 NOT NULL；`d4e5f6a1b2c3` 才把它改成可空。
    种子按此判据决定「播不播模板行」，而不是无条件插 —— 否则 upgrade 会炸。
    """
    from sqlalchemy import inspect as _inspect

    bind = db.get_bind()
    inspector = _inspect(bind)
    if not inspector.has_table("role_menus"):
        return False
    for column in inspector.get_columns("role_menus"):
        if column["name"] == "user_id":
            return bool(column.get("nullable"))
    return False


def seed_admin_user(db: Session) -> User | None:
    """当系统里**没有任何可用 admin** 时插入出厂管理员。

    判据 = `role='admin' AND deleted_at IS NULL` 的行数为 0。
    ⚠️ **不看 `status`**：被停用的 admin 也算「存在」—— 那是一个需要人去处理的
    状态（管理员自己停用自己），自动补一个新 admin 会让「谁把这个号停了」
    永远查不清。种子只在**真正没有 admin 行**时兜底启动能力。
    """
    if not _users_have_seed_columns(db):
        #: 手写最小夹具（`test_initial_data` 的 `users` 只有 id/username/…）：
        #: 判据列不存在 ⇒ 不播（真库上这两列都在，不会走到这里）。
        logger.warning("seed: users 缺 role / deleted_at 列 ⇒ 跳过出厂 admin 种子")
        return None

    from backend.app.core.security import hash_password

    count = db.scalar(
        select(func.count()).select_from(User).where(
            User.role == ROLE_ADMIN, User.deleted_at.is_(None)
        )
    )
    if count:
        return None

    username = ADMIN_SEED["username"]
    #: 出厂口令**在这里生成一次**（两个分支共用同一串），并且只写进日志。
    #: ⚠️ 不要把它写回数据库明文 / 文档 / 任何响应体（见 `generate_admin_password`）。
    _admin_password = ADMIN_SEED.get("password") or generate_admin_password()
    if db.scalar(select(User.id).where(User.username == username)) is not None:
        # 用户名被一个**非 admin** 行占了（用户改过角色）：不抢、不改它，
        # 只记日志 —— 静默改别人的账号角色比「没有 admin」更难排查。
        logger.warning(
            "seed: 用户 %r 已存在但不是 admin，跳过出厂管理员种子（请手工分配 admin 角色）",
            username,
        )
        return None

    #: ⚠️ **不用 `db.add(User(...))`，改表级 INSERT**(2026-10-24 实测踩坑)：
    #: `User` 模型带 `email` / `phone`（迁移 `e5f6a1b2c3d4` 才加），而本函数会被
    #: 迁移 `c3d4e5f6a1b2`（播种）调用 —— 那一刻表**还没有这两列**。两者都是
    #: `nullable=False` 且带 Python default，所以 ORM 一定会把它们写进 SQL 的列清单
    #: ⇒ `table users has no column named email`，整个 upgrade 失败。
    #:
    #: 试过两条错路（都实测失败）：
    #:   ① `state.dict.pop("email")` ⇒ 绑定参数里没了，但 SQL 的列清单里还在；
    #:   ② `column.default = None` ⇒ 能过，但**永久改了全局 mapper**，
    #:      之后同进程所有 `User` 插入都不带这两个默认值（污染面太大）。
    #:
    #: 正解 = `insert(User.__table__)` + 只传**库里实际存在**的列。
    #: ⚠️ 必须传 `__table__` 而不是 `User`：`insert(User)` 仍会展开完整列清单。
    #:
    #: 与「模板行」是同一类错误：**播种代码用到了比它自己更新的 schema**。
    values = {
        "username": username,
        "password_hash": hash_password(_admin_password),
        "real_name": ADMIN_SEED["real_name"],
        "role": ADMIN_SEED["role"],
        "status": USER_STATUS_NORMAL,
        "must_change_password": True,
        "token_version": 0,
        "created_at": shanghai_now(),
    }
    #: ⚠️ **用 `text()` 手写 INSERT**（2026-10-24 实测，第三次才对）：
    #:
    #: 试过三条路，前两条都失败（都有实测证据）：
    #:   ① `db.add(User(...))`            ⇒ ORM 按**模型**列清单拼 SQL（含 email）；
    #:   ② `insert(User.__table__).values({Column: v})` ⇒ **表级 insert 仍会补全
    #:      表上所有带 Python default 的列**（`email`/`phone`/`last_login_ip`），
    #:      即使只传了两个列对象 —— 最小复现脚本 `.hermes/tmp/insrep.py` 证明了这点；
    #:   ③ `text()` 显式写列名 ⇒ 只有写到的列进 SQL。✔
    #:
    #: 这不是「绕过框架」，而是**表结构比模型少列**这个事实的必然结果：
    #: 播种迁移 `c3d4e5f6a1b2` 跑在 `e5f6a1b2c3d4`（加 email/phone）之前。
    #:
    #: 与「模板行」是同一类错误：**播种代码用到了比它自己更新的 schema**。
    values = {
        "username": username,
        "password_hash": hash_password(_admin_password),
        "real_name": ADMIN_SEED["real_name"],
        "role": ADMIN_SEED["role"],
        "status": USER_STATUS_NORMAL,
        "must_change_password": True,
        "token_version": 0,
        "created_at": shanghai_now(),
    }
    actual = {c["name"] for c in _sa_inspect(db.get_bind()).get_columns("users")}
    payload = {k: v for k, v in values.items() if k in actual}
    columns = ", ".join(payload)
    placeholders = ", ".join(f":{name}" for name in payload)
    statement = f"INSERT INTO users ({columns}) VALUES ({placeholders})"

    #: ⚠️ **按方言选 RETURNING，绝不用「先插再看 lastrowid」的写法**：
    #: 那样在 PG 上会**插两次**（第一次成没成功不知道，第二次必然撞唯一键）。
    #: PG 原生支持 `RETURNING id`；SQLite 3.35+ 也支持，但为兼容起见走
    #: `lastrowid`（两个分支**只有一条**会执行）。
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        new_id = db.execute(text(f"{statement} RETURNING id"), payload).scalar()
    else:
        result = db.execute(text(statement), payload)
        new_id = getattr(result, "lastrowid", None)

    if new_id is not None:
        #: 出厂口令**只在这里出现一次** —— 运维首装时从日志里取。
        #: ⚠️ 不要改成 `logger.info`（默认级别看不到 ⇒ 运维拿不到口令 ⇒
        #: 新装系统无人能登录），`warning` 是刻意的：它必须显眼。
        logger.warning(
            "seed: 已创建出厂管理员 username=%s password=%s  —— "
            "请立即用它登录并按提示改密；本行只出现一次，不会写进数据库明文。",
            username,
            _admin_password,
        )
    return db.get(User, new_id) if new_id is not None else None


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def seed_system_domain(db: Session) -> dict[str, int]:
    """一次播完（角色 → admin → 菜单 → 字典 → 授权），返回本次新建计数。

    顺序是**判据**：
    1. 角色先有（授权行要 `role_id`）；
    2. admin 用户先有（`role_menus.user_id` 要指向它）；
    3. 菜单先有（授权行的 `menu_id`）；
    4. 授权最后（前四者都可能新增行）。
    """
    roles = seed_roles(db)
    admin = seed_admin_user(db)
    menus = seed_menus(db)
    types, items = seed_dicts(db)
    grants = seed_role_menus(db)
    return {
        "roles": len(roles),
        "admin_users": 1 if admin is not None else 0,
        "menus": len(menus),
        "dict_types": len(types),
        "dict_items": len(items),
        "role_menus": len(grants),
    }
