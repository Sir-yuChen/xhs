"""T3 素材库扫描服务契约测试（设计 17 §6）。

覆盖面：
1. **路径安全** `is_safe_material_path()` —— 空 / 不存在 / 非目录 / 盘符根 / 系统目录 / 合法目录；
2. **增量扫描** `scan_folder()` —— 首扫建索引、mtime 未变跳过、源文件消失删索引行；
3. **类型与缩略图** —— 图片生成 80×80 缩略图 + 读尺寸；视频 `thumb_url=""`（无 opencv 的降级）；
4. **只读边界（C2）** —— 扫描前后用户源文件字节数 / 修改时间不变；
5. **归属隔离** —— 扫描只动本 folder 的行，不越界别的 folder / 别的 user。

⚠️ 一律在 `tmp_path` 里造假目录，**绝不扫真实磁盘路径**。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import select

from backend.app.models import MaterialFile, MaterialFolder
from backend.app.services import material_scan_service as svc

import pytest




#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract

# --------------------------------------------------------------------------- #
# 造数助手
# --------------------------------------------------------------------------- #
def _make_image(path: Path, size: tuple[int, int] = (200, 150)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (200, 30, 60)).save(path)


def _make_video_stub(path: Path) -> None:
    """视频只造**扩展名对的文件**（不解析内容，服务层也不解析）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 64)


def _seed_folder(db: Any, user_id: int, name: str, path: str) -> MaterialFolder:
    """造素材目录行（**连带保证 `users` 行存在**）。

    ⚠️ 2026-10-04 加：隔离修好之后，此前「静默读生产库」的用例暴露了自己依赖
    生产中已存在的 users 行。跨用户用例用 `user_id=7` 这类夹具库没有的 id ⇒
    `FOREIGN KEY constraint failed`。外键指向的行由**造数助手自己**造出来，
    比在 20 个用例里各播一次种可靠（漏一处就是一条假红）。
    """
    from backend.app.models.user import User

    if db.get(User, user_id) is None:
        db.add(User(id=user_id, username=f"mat-test-{user_id}", password_hash="x"))
        db.flush()
    folder = MaterialFolder(user_id=user_id, name=name, path=path, enabled=True)
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return folder


# --------------------------------------------------------------------------- #
# ① 路径安全
# --------------------------------------------------------------------------- #
def test_safe_path_rejects_empty_and_blank():
    assert svc.is_safe_material_path("")[0] is False
    assert svc.is_safe_material_path("   ")[0] is False


def test_safe_path_rejects_missing(tmp_path: Path):
    ok, reason = svc.is_safe_material_path(str(tmp_path / "nope"))
    assert ok is False and reason == "路径不存在"


def test_safe_path_rejects_file_not_dir(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    ok, reason = svc.is_safe_material_path(str(f))
    assert ok is False and reason == "路径不是文件夹"


def test_safe_path_rejects_drive_root():
    assert svc.is_safe_material_path("C:" + "\\\\")[0] is False
    assert svc.is_safe_material_path("C:/")[0] is False


def test_safe_path_rejects_unix_root():
    assert svc.is_safe_material_path("/")[0] is False


def test_safe_path_rejects_system_dirs():
    # ⚠️ 这些路径在真机上必然存在，正好用来验黑名单**先于**存在性判定。
    bslash = "\\"
    cases = [
        "C:" + bslash + "Windows",
        "C:" + bslash + "Windows" + bslash + "System32",
        "c:" + bslash + "windows",
        "C:" + bslash + "Program Files",
        "/etc",
        "/usr/local",
    ]
    for raw in cases:
        ok, reason = svc.is_safe_material_path(raw)
        assert ok is False, raw
        assert "系统目录" in reason, (raw, reason)


def test_safe_path_accepts_real_dir(tmp_path: Path):
    ok, reason = svc.is_safe_material_path(str(tmp_path))
    assert ok is True and reason == ""


def test_resolve_abs_path_normalizes(tmp_path: Path):
    messy = str(tmp_path / "sub" / ".." / ".")
    got = svc.resolve_abs_path(messy)
    assert Path(got).is_absolute()
    assert ".." not in got


# --------------------------------------------------------------------------- #
# ② 增量扫描
# --------------------------------------------------------------------------- #
def test_scan_indexes_images_and_videos(tmp_path: Path, db_session: Any):
    _make_image(tmp_path / "a.jpg", (200, 150))
    _make_image(tmp_path / "sub" / "b.png", (100, 100))
    _make_video_stub(tmp_path / "c.mp4")
    (tmp_path / "ignore.txt").write_text("x", encoding="utf-8")

    folder = _seed_folder(db_session, user_id=1, name="t", path=str(tmp_path))
    stats = svc.scan_folder(db_session, folder)

    assert stats["added"] == 3
    assert stats["removed"] == 0
    rows = db_session.scalars(
        select(MaterialFile).where(MaterialFile.folder_id == folder.id)
    ).all()
    assert {r.filename for r in rows} == {"a.jpg", "b.png", "c.mp4"}
    # 父目录内的 txt 不被索引
    assert all(r.file_type in ("image", "video") for r in rows)

    db_session.refresh(folder)
    assert folder.file_count == 3
    assert folder.last_scan_at is not None


def test_scan_is_incremental_on_unchanged_mtime(tmp_path: Path, db_session: Any):
    _make_image(tmp_path / "a.jpg")
    folder = _seed_folder(db_session, user_id=1, name="t", path=str(tmp_path))

    first = svc.scan_folder(db_session, folder)
    assert first["added"] == 1

    second = svc.scan_folder(db_session, folder)
    assert second["added"] == 0, "mtime 未变不得重复索引"
    assert second["skipped"] == 1
    assert second["updated"] == 0


def test_scan_removes_index_when_source_deleted(tmp_path: Path, db_session: Any):
    f = tmp_path / "gone.jpg"
    _make_image(f)
    folder = _seed_folder(db_session, user_id=1, name="t", path=str(tmp_path))
    svc.scan_folder(db_session, folder)

    f.unlink()
    stats = svc.scan_folder(db_session, folder)

    assert stats["removed"] == 1
    assert db_session.scalars(
        select(MaterialFile).where(MaterialFile.folder_id == folder.id)
    ).all() == []
    db_session.refresh(folder)
    assert folder.file_count == 0


def test_scan_skips_noise_dirs(tmp_path: Path, db_session: Any):
    _make_image(tmp_path / "ok.jpg")
    _make_image(tmp_path / "node_modules" / "x.jpg")
    _make_image(tmp_path / ".git" / "y.png")
    folder = _seed_folder(db_session, user_id=1, name="t", path=str(tmp_path))

    svc.scan_folder(db_session, folder)
    rows = db_session.scalars(
        select(MaterialFile).where(MaterialFile.folder_id == folder.id)
    ).all()
    assert {r.filename for r in rows} == {"ok.jpg"}


# --------------------------------------------------------------------------- #
# ③ 类型与缩略图
# --------------------------------------------------------------------------- #
def test_scan_image_gets_thumb_and_dimensions(tmp_path: Path, db_session: Any):
    _make_image(tmp_path / "pic.jpg", (320, 240))
    folder = _seed_folder(db_session, user_id=1, name="t", path=str(tmp_path))
    svc.scan_folder(db_session, folder)

    row = db_session.scalars(select(MaterialFile)).one()
    assert row.width == 320 and row.height == 240
    assert row.thumb_url.startswith(f"/api/xhs/materials/folders/{folder.id}/thumbs/")
    # 缩略图**副本**确实落盘了
    thumb_dir = svc.thumbs_dir() / str(folder.id)
    assert thumb_dir.is_dir() and any(thumb_dir.iterdir())


def test_scan_video_has_no_thumb(tmp_path: Path, db_session: Any):
    _make_video_stub(tmp_path / "v.mp4")
    folder = _seed_folder(db_session, user_id=1, name="t", path=str(tmp_path))
    svc.scan_folder(db_session, folder)

    row = db_session.scalars(select(MaterialFile)).one()
    assert row.file_type == "video"
    assert row.thumb_url == ""
    assert row.width is None and row.height is None


def test_scan_broken_image_degrades_without_failing(tmp_path: Path, db_session: Any):
    """损坏的 .jpg 不该让整次扫描失败（一个坏文件只是一条空缩略图）。"""
    (tmp_path / "broken.jpg").write_bytes(b"not an image at all")
    _make_image(tmp_path / "good.jpg")
    folder = _seed_folder(db_session, user_id=1, name="t", path=str(tmp_path))

    stats = svc.scan_folder(db_session, folder)
    assert stats["added"] == 2  # 坏文件也索引（只是没缩略图）

    rows = {r.filename: r for r in db_session.scalars(select(MaterialFile)).all()}
    assert rows["broken.jpg"].thumb_url == ""
    assert rows["broken.jpg"].width is None
    assert rows["good.jpg"].thumb_url != ""


# --------------------------------------------------------------------------- #
# ④ 只读边界（C2）
# --------------------------------------------------------------------------- #
def test_scan_never_touches_user_source_files(tmp_path: Path, db_session: Any):
    f = tmp_path / "keep.jpg"
    _make_image(f)
    before = (f.stat().st_size, f.stat().st_mtime, f.read_bytes())

    folder = _seed_folder(db_session, user_id=1, name="t", path=str(tmp_path))
    svc.scan_folder(db_session, folder)

    after = (f.stat().st_size, f.stat().st_mtime, f.read_bytes())
    assert before == after, "扫描不得改动用户源文件（C2 只读边界）"
    assert f.exists()


# --------------------------------------------------------------------------- #
# ⑤ 归属隔离
# --------------------------------------------------------------------------- #
def test_scan_does_not_touch_other_folder(tmp_path: Path, db_session: Any):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    _make_image(dir_a / "x.jpg")
    _make_image(dir_b / "y.jpg")

    fa = _seed_folder(db_session, user_id=1, name="A", path=str(dir_a))
    fb = _seed_folder(db_session, user_id=1, name="B", path=str(dir_b))

    svc.scan_folder(db_session, fa)  # 只扫 A

    a_rows = db_session.scalars(
        select(MaterialFile).where(MaterialFile.folder_id == fa.id)
    ).all()
    b_rows = db_session.scalars(
        select(MaterialFile).where(MaterialFile.folder_id == fb.id)
    ).all()
    assert {r.filename for r in a_rows} == {"x.jpg"}
    assert b_rows == [], "扫 A 不得波及 B"


def test_scan_rows_carry_folder_owner(tmp_path: Path, db_session: Any):
    _make_image(tmp_path / "x.jpg")
    folder = _seed_folder(db_session, user_id=7, name="t", path=str(tmp_path))
    svc.scan_folder(db_session, folder)

    row = db_session.scalars(select(MaterialFile)).one()
    assert row.user_id == 7 == folder.user_id


# --------------------------------------------------------------------------- #
# ⑥ 隔离：缩略图必须落进**沙箱**，不许碰生产 storage（DEF-094，2026-10-05）
# --------------------------------------------------------------------------- #
def test_scan_writes_thumbs_into_the_sandboxed_root(tmp_path: Path, material_storage, db_session: Any):
    """扫描产出的缩略图必须落在 (`material_storage`) 里，且**生产 storage 一个字节不动**。

    DEF-094 实测事故：`Settings.storage_dir` 是写死的相对路径，`thumbs_dir()` 每次
    现算 ⇒ 任何调扫描的用例都会把缩略图**原地覆盖**到 `backend/app/storage/materials/thumbs/`。
    只跑一遍用例就把用户素材库的两张缩略图改了（358 → 482 字节）。

    ⚠️ 判据用**快照对比**而不是「文件不存在」：夹具库里的行 id 从小整数开始，
    文件名与生产文件**会撞名**（`f1-1.jpg`），「存在」这件事本身说明不了任何问题 ——
    真正的判据是**内容/修改时间没被这轮改写**。
    """
    prod_dir = Path("backend/app/storage/materials/thumbs/1")
    before = {
        p.name: (p.stat().st_size, p.stat().st_mtime_ns)
        for p in prod_dir.glob("*.jpg")
    }

    src_dir = tmp_path / "src"
    _make_image(src_dir / "a.jpg", (300, 200))
    folder = _seed_folder(db_session, user_id=1, name="t", path=str(src_dir))

    svc.scan_folder(db_session, folder)

    row = db_session.scalars(select(MaterialFile)).one()
    assert row.thumb_url, "图片必须生成缩略图（url 是端点契约）"
    fid = folder.id
    produced = material_storage / "materials" / "thumbs" / str(fid) / f"f{fid}-{row.id}.jpg"
    assert produced.is_file(), f"缩略图没落进沙箱：{produced}"

    after = {
        p.name: (p.stat().st_size, p.stat().st_mtime_ns)
        for p in prod_dir.glob("*.jpg")
    }
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert not changed, (
        f"生产 storage 的缩略图被测试改动了：{sorted(changed)} —— 素材隔离漏了（DEF-094 复现）"
    )
