"""测试隔离守卫：**用例不得静默读写生产库**（2026-10-04 建）。

## 为什么需要这条守卫

2026-10-04 实测事故：`tests/backend/test_workflows.py` 的 `run_workflow_job` 用例
往**真实库** `data/spider_xhs.db` 写了 4 条 `workflows` + 22 条 `workflow_runs`
+ 12 条 `workflow_nodes`。

根因**不是**「某个用例忘了申请正确的夹具」，而是**结构的**：
只有 `api_client` 夹具会调 `patch_service_sessions()`，而
`db_session` 不会 —— 于是「用 `db_session` + 直接调自开 Session 的服务函数」
这类用例**必然**打到生产库。项目此前已发生两次同类事故（conftest docstring
记载：2026-09-27 两次、2026-10-02 `material_scan_service` 一次）。

## 本文件钉住三件事

1. **`db_session` 必须打桩**（源码级断言，防止有人把那一行删掉）；
2. **`db_engine` 必须播种外键基线行**（`users` / `platform_accounts`）——
   否则隔离修好后，一批用例会因「夹具库自洽性缺失」而红；
3. **行为级证明**：用 `db_session` 调**自开 Session 的服务**后，
   **真实库的行数不变**（这是唯一不可伪造的判据）。
   ⚠️ 2026-10-07：原判据用的是 `run_workflow_job`（工作流服务），该特性已整条移除
   ⇒ 现在的行为级证明落在**素材缩略图落盘面**（见文件末尾两条 `..._sandboxes_material_storage`）。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
CONFTEST = ROOT / "tests" / "backend" / "conftest.py"
PROD_DB = ROOT / "data" / "spider_xhs.db"
#: 生产素材缩略图目录 —— 下面两条「落盘面」守卫盯的就是它不被测试改写。
#: ⚠️ 这个常量原本夹在工作流那条用例附近，2026-10-07 删工作流用例时被连带删掉
#:    ⇒ 两条落盘守卫当场 `NameError`。教训：删除用例时**只删用例体**，常量另议。
STORAGE_THUMBS = ROOT / "backend" / "app" / "storage" / "materials" / "thumbs"


def _conftest_source() -> str:
    return CONFTEST.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# ① 源码级：db_session 必须打桩
# --------------------------------------------------------------------------- #
def test_db_session_fixture_patches_service_sessions():
    """`db_session` 里必须有 `patch_service_sessions(...)` 调用。

    ⚠️ 变异守护：删掉那一行 ⇒ 本用例必红。
    （这是 2026-10-04 事故的直接修复点，删掉就复发。）
    """
    source = _conftest_source()
    # 只取 db_session 函数体
    start = source.index("def db_session(")
    end = source.index("\ndef ", start + 1)
    body = source[start:end]
    assert "patch_service_sessions(" in body, (
        "db_session 夹具必须打桩服务层 Session 工厂，否则「用 db_session + 调服务函数」"
        "的用例会静默写生产库（2026-10-04 实测 22 行 workflow_runs 落到真实库）"
    )


def test_db_engine_fixture_does_not_seed_rows():
    """`db_engine` **不得**播种任何行 —— 契约是「新库应当是空的」。

    ⚠️ 这条守卫的方向曾经写反过（2026-10-04）：一度要求 `db_engine` 播种
    `users` / `platform_accounts` 基线行，结果红了 20+ 条 ——
    `test_conftest_fixtures::test_db_session_fixture_is_usable` 的契约是
    `assert count == 0`（「新库应当是空的」），`test_llm_gateway` 等也依赖空库。

    **外键父行由需要它的文件自己造**（`seed_owner_rows` 的用法），
    不在公共夹具里替所有用例决定。

    ⚠️ 变异守护：在 `db_engine` 里加回 `_seed_baseline_rows(...)` ⇒ 本用例必红。
    """
    source = _conftest_source()
    start = source.index("def db_engine(")
    end = source.index("\ndef ", start + 1)
    body = source[start:end]
    assert "_seed_baseline_rows(" not in body, (
        "db_engine 不得播种 —— 夹具契约为「新库应当是空的」"
        "（`test_conftest_fixtures` 会红，且一批依赖空库的用例断言失真）"
    )


def test_seed_baseline_flushes_parent_before_child():
    """`seed_owner_rows` 必须**先 flush 父行**（`users`）再插子行（`platform_accounts`）。

    ⚠️ 变异守护：删掉 `db.flush()` ⇒ 本用例必红。
    实测：同一 commit 里 SQLAlchemy 不保证按声明顺序发语句，
    `platform_accounts.user_id -> users.id` 会撞 `FOREIGN KEY constraint failed`
    （种子行自己就违反外键，表现为**全部用例 setup 失败**）。
    """
    source = _conftest_source()
    start = source.index("def seed_owner_rows(")
    end = source.index("\ndef ", start + 1)
    body = source[start:end]
    user_add = body.index("User(")
    account_add = body.index("PlatformAccount(")
    between = body[user_add:account_add]
    assert "db.flush()" in between, (
        "插 users 之后、platform_accounts 之前必须 flush，否则外键失败"
    )


# --------------------------------------------------------------------------- #
# ② 行为级：真实库不得被写
# --------------------------------------------------------------------------- #
def _count(table: str) -> int:
    if not PROD_DB.exists():
        pytest.skip("无真实库，跳过（CI / 新克隆环境）")
    with sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True) as conn:
        try:
            return conn.execute(f"select count(*) from {table}").fetchone()[0]
        except sqlite3.OperationalError:
            return -1


# ⚠️ 2026-10-07：`test_workflow_tables_in_production_are_not_touched` 整条**退场** ——
# 它守的是「工作流服务自开 Session 会写生产库」，而工作流整条特性已删（用户口径
# 「把工作流相关逻辑表等完整移除」）⇒ 服务模块不存在，判据无处可依。
# ⚠️ **但那条判据揭示的夹具纪律仍然有效、且仍有守卫**：见下面两条
# `..._sandboxes_material_storage`（落盘面）与本文件其余 Session 打桩断言（写库面）。
# 原用例与事故记录（22 行 workflow_runs 落到真实库）向上保留在本文件 docstring 里。


def test_db_session_fixture_sandboxes_material_storage():
    """`db_session` 必须把素材落盘根目录搬进沙箱（源码级断言）。

    ⚠️ 变异守护：删掉 `sandbox_material_storage_always()` ⇒ 本用例必红。
    （配套行为判据见 `test_material_scan_service.py`，变异脚本
    `.hermes/scripts/mutate_verify_material_storage_isolation.py`。）
    """
    source = _conftest_source()
    start = source.index("def db_session(")
    end = source.index("\ndef ", start + 1)
    body = source[start:end]
    assert "sandbox_material_storage_always()" in body, (
        "db_session 必须搬走素材落盘根目录，否则「用 db_session + 调素材扫描」的用例"
        "会读生产库的文件夹行、把缩略图原地覆盖到生产 storage（DEF-094 实测）"
    )


def test_api_client_fixture_sandboxes_material_storage():
    """`api_client` 同理（素材端点：新建文件夹 / 重扫 / 上传 / 缩略图都会落盘）。"""
    source = _conftest_source()
    start = source.index("def api_client(")
    end = source.index("\ndef ", start + 1)
    body = source[start:end]
    assert "sandbox_material_storage(" in body, (
        "api_client 必须搬走素材落盘根目录 —— 素材端点全部会写缩略图/上传物"
    )


def test_material_thumbs_in_production_are_not_touched_by_a_scan(tmp_path, db_session):
    """**行为判据**：走夹具把临时目录扫一遍后，生产 thumbs 目录**不多不少**。

    ⚠️ 变异守护：关掉兜底隔离 ⇒ 本用例必红（实测确认，见
    `.hermes/scripts/mutate_verify_material_storage_isolation.py`）。

    这条与**已退场**的 `test_workflow_tables_in_production_are_not_touched`（2026-10-07
    随工作流特性删除）曾是同一判据的两个面：那条看**写库**，这条看**落盘** ——
    「只堵住 Session 打桩，素材扫描照样会写生产 storage」这个缺口至今有效，故本用例保留。
    """
    if not STORAGE_THUMBS.exists():
        pytest.skip("无生产 thumbs 目录，跳过（新克隆环境）")
    before = sorted(p.relative_to(STORAGE_THUMBS).as_posix() for p in STORAGE_THUMBS.rglob("*.jpg"))

    from PIL import Image

    from backend.app.models import MaterialFile, MaterialFolder
    from backend.app.models.user import User
    from backend.app.services import material_scan_service as svc

    assert "hermes-material" in str(svc.thumbs_dir()) or "material_storage" in str(
        svc.thumbs_dir()
    ) or "pytest-of-" in str(svc.thumbs_dir()), (
        f"素材落盘根目录没被搬走：{svc.thumbs_dir()}"
    )

    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (240, 160), (10, 120, 200)).save(src_dir / "probe.jpg")

    if db_session.get(User, 1) is None:
        db_session.add(User(id=1, username="iso-probe", password_hash="x"))
        db_session.flush()
    folder = MaterialFolder(user_id=1, name="iso-probe", path=str(src_dir), enabled=True)
    db_session.add(folder)
    db_session.commit()
    db_session.refresh(folder)

    svc.scan_folder(db_session, folder)
    assert db_session.query(MaterialFile).count() == 1

    after = sorted(p.relative_to(STORAGE_THUMBS).as_posix() for p in STORAGE_THUMBS.rglob("*.jpg"))
    assert after == before, f"生产 thumbs 被动过：{set(after) ^ set(before)}"
