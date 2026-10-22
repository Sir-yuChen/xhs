"""违规词内置种子（2026-10-21 用户口径「把当前违规词作为新的种子数据」）。

## 为什么种子要有守卫

违规词是**用户资产**：一旦播种逻辑写成「每次启动 upsert」，用户删掉的词会在下次
重启时复活 —— 那正是「系统改了我的数据」这类最难被发现的静默错。本文件钉三件事：

1. 种子文件本身合法（分组 / 匹配模式 / 7 条内容与真机核对过的一致）；
2. 播种**只往空分组追加**：幂等、不复活被删的词、删光整组才补回；
3. 接线在（注册时 + 启动补存量），且是**模块级导入**（否则 lifespan 的打桩打不上）。
"""

from __future__ import annotations

import ast
from pathlib import Path

from backend.app.core.initial_data import load_initial_forbidden_words
from backend.app.models.forbidden_word import DEFAULT_GROUP, MATCH_MODE_CONTAINS, MATCH_MODES

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "config/initial_data/forbidden_words.json"
SERVICE = ROOT / "backend/app/services/forbidden_word_service.py"
AUTH = ROOT / "backend/app/api/auth.py"
MAIN = ROOT / "backend/app/main.py"

#: 真机核对过的种子内容（用户 2026-10-21 截图确认的「小红书」组 7 条）。
EXPECTED_WORDS = ["淘宝", "天猫", "超级88", "88VIP", "政府补贴", "好评", "补贴"]


def _rows() -> list[dict]:
    return load_initial_forbidden_words()


# --------------------------------------------------------------------------- #
# 1. 种子文件
# --------------------------------------------------------------------------- #


def test_seed_file_ships_the_seven_agreed_words():
    """内容必须与用户核对过的 7 条一致（多一条少一条都是口径漂移）。"""
    rows = _rows()
    assert [r["word"] for r in rows] == EXPECTED_WORDS, rows
    assert all(r["group_name"] == "小红书" for r in rows), "分组名被改了"
    assert all(r["match_mode"] == MATCH_MODE_CONTAINS for r in rows), (
        "违规词对 OCR 文本必须用「包含」匹配 —— 精确匹配等于永不命中"
    )
    assert all(r["enabled"] for r in rows)
    assert SEED.is_file(), "种子文件不在 config/initial_data/"


def test_seed_loader_drops_invalid_rows_instead_of_crashing(tmp_path, monkeypatch):
    """坏数据只丢该条（含未知 match_mode），不牵连其余 —— 与其它种子同纪律。"""
    from backend.app.core import initial_data as mod

    bad = tmp_path / "forbidden_words.json"
    bad.write_text(
        """{"forbidden_words": [
            {"word": "好词", "match_mode": 2, "group_name": "小红书"},
            {"word": "", "match_mode": 2},
            {"word": "坏模式", "match_mode": 99},
            "not-an-object"
        ]}""",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "FORBIDDEN_WORDS_FILENAME", bad.name)
    monkeypatch.setattr(
        mod, "initial_data_candidates", lambda name: [tmp_path / name]
    )
    rows = mod.load_initial_forbidden_words()
    assert [r["word"] for r in rows] == ["好词"], rows


def test_seed_group_and_modes_come_from_the_model_not_a_copy():
    """分组/匹配模式的白名单**不在这里重抄**（枚举漂移即缺陷）。"""
    src = (ROOT / "backend/app/core/initial_data.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_normalize_forbidden_word"
    )
    imported = {
        alias.name
        for node in ast.walk(fn)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "MATCH_MODES" in imported and "DEFAULT_GROUP" in imported
    assert MATCH_MODE_CONTAINS in MATCH_MODES and DEFAULT_GROUP == ""


# --------------------------------------------------------------------------- #
# 2. 播种语义（用户资产：只往空分组追加）
# --------------------------------------------------------------------------- #


def _session(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import backend.app.models  # noqa: F401 - 注册全部模型
    from backend.app.core.database import Base
    from backend.app.models.user import User

    engine = create_engine("sqlite:///" + str(tmp_path / "seed.db").replace("\\", "/"))
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(id=7, username="seed_probe", password_hash="x"))
    db.commit()
    return db


def _words(db, user_id=7) -> list[str]:
    from sqlalchemy import select

    from backend.app.models.forbidden_word import ForbiddenWord

    return sorted(
        w
        for (w,) in db.execute(
            select(ForbiddenWord.word).where(ForbiddenWord.user_id == user_id)
        ).all()
    )


def test_seed_is_idempotent_and_never_revives_a_deleted_word(tmp_path):
    """① 空库播 7 条；② 再播 0 条；③ 删掉的词**不复活**；④ 删光整组才补回。"""
    from backend.app.models.forbidden_word import ForbiddenWord
    from backend.app.services import forbidden_word_service as fw

    db = _session(tmp_path)

    assert fw.seed_builtin_words(db, user_id=7) == 7
    db.commit()
    assert _words(db) == sorted(EXPECTED_WORDS)

    #: 幂等：该分组已有词 ⇒ 一条都不建
    assert fw.seed_builtin_words(db, user_id=7) == 0

    #: 用户删掉一个词 —— 这是用户的选择，不能被播种复活
    db.execute(
        ForbiddenWord.__table__.delete().where(
            ForbiddenWord.user_id == 7, ForbiddenWord.word == "好评"
        )
    )
    db.commit()
    assert fw.seed_builtin_words(db, user_id=7) == 0
    assert "好评" not in _words(db), "被用户删掉的词被播种复活了"

    #: 删光整组 ⇒ 他承认这组该有 ⇒ 补回
    db.execute(ForbiddenWord.__table__.delete().where(ForbiddenWord.user_id == 7))
    db.commit()
    assert fw.seed_builtin_words(db, user_id=7) == 7


def test_seed_does_not_touch_another_users_words(tmp_path):
    """播种按 user_id 隔离（数据隔离红线）。"""
    from backend.app.models.forbidden_word import ForbiddenWord
    from backend.app.services import forbidden_word_service as fw

    db = _session(tmp_path)
    from backend.app.models.user import User

    #: ⚠️ 必须先 flush 用户行再插词：SQLite 的 FK 在 `commit()` 前就校验，
    #: 同一次 commit 里的「父+子」若顺序不对会 `FOREIGN KEY constraint failed`。
    db.add(User(id=8, username="other_probe", password_hash="x"))
    db.flush()
    db.add(ForbiddenWord(user_id=8, word="别人的词", match_mode=2, group_name="小红书"))
    db.commit()

    assert fw.seed_builtin_words(db, user_id=7) == 7
    db.commit()
    assert _words(db, 8) == ["别人的词"], "播种动到了别的用户的数据"


# --------------------------------------------------------------------------- #
# 3. 接线
# --------------------------------------------------------------------------- #


def test_seed_is_wired_into_register_and_startup():
    """注册即播种 + 启动补存量；两处都必须调用，且启动侧是**模块级导入**。"""
    auth_src = AUTH.read_text(encoding="utf-8")
    assert "seed_builtin_words(db, user_id=user.id)" in auth_src, (
        "注册流程没播种违规词 —— 新用户第一次打开弹窗是空的"
    )

    main_src = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(main_src)
    module_imports = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "seed_builtin_words_for_all_users" in module_imports, (
        "启动播种必须模块级导入（函数体内导入 ⇒ lifespan 的打桩打不上）"
    )
    assert "seed_builtin_words_for_all_users()" in main_src, "lifespan 里没调用"
