"""素材文件增删（v2.31，设计 17 §7.3 补章）。

## 用户口径（2026-10-03，逐字）

> 「素材文件区域添加，删除，批量删除，大图预览，且支持左右切换，
>   注意删除会真实删除本地文本，添加提示二次校验」
> 「按照你推荐的真删文件，但删得可逆执行」

## 本文件守什么

v2.31 起**删除语义变了**（从「没有删除能力」到「真删源文件」），
所以判据不能只是「端点返回 200」—— **必须证明磁盘上的文件真的没了**，
且**必须证明是可逆的**（这才是用户要的性质）。

⚠️ 最容易写错的三种实现，本文件都钉住：

| 坏实现 | 后果 | 钉它的用例 |
|---|---|---|
| `os.unlink` 直删 | 误删不可恢复（用户要的是「可逆」） | `test_delete_moves_file_to_recycle_bin_not_unlink` |
| 不校验归属 | 改个 id 能删别人的文件 | `test_delete_is_user_scoped` |
| 上传不查重名 | **不可逆地覆盖**用户已有文件 | `test_upload_refuses_to_overwrite_existing_file` |
"""

from __future__ import annotations

import glob
import os
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.models.material import MaterialFile, MaterialFolder
from backend.app.models.user import User


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration


def _register(api_client, username: str) -> dict:
    response = api_client.post(
        "/api/auth/register",
        json={"username": username, "password": "secret123"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _user_by_name(db_factory, username: str) -> User:
    with db_factory() as db:
        user = db.scalar(select(User).where(User.username == username))
        assert user is not None
        return user


def _seed_folder_and_file(db_factory, tmp_path: Path, username: str, *, filename: str = "a.png"):
    """建「文件夹 + 磁盘上的真文件 + 索引行」，返回 (folder_id, file_id, 文件绝对路径)。"""
    user = _user_by_name(db_factory, username)
    root = tmp_path / f"materials-{username}"
    root.mkdir(parents=True, exist_ok=True)
    target = root / filename
    target.write_bytes(b"fake-image-bytes")

    with db_factory() as db:
        folder = MaterialFolder(user_id=user.id, name="测试素材库", path=str(root), enabled=True)
        db.add(folder)
        db.commit()
        db.refresh(folder)
        row = MaterialFile(
            user_id=user.id,
            folder_id=folder.id,
            filename=filename,
            file_type="image",
            rel_path=filename,
            size_bytes=target.stat().st_size,
            mtime=datetime.fromtimestamp(target.stat().st_mtime),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return folder.id, row.id, target


def test_delete_moves_file_to_recycle_bin_not_unlink(api_client, db_factory, tmp_path):
    """真删**源文件**，但**可逆**（用户口径「真删文件，但删得可逆执行」）。

    ⚠️ 判据是「文件从**原位置**消失」—— 而不是「调用了 send2trash」。
    调用了但没删掉（静默失败）是更坏的坏法：用户以为删了、实际还在。
    """
    headers = _register(api_client, "mat-del")
    _, file_id, target = _seed_folder_and_file(db_factory, tmp_path, "mat-del")

    assert target.exists(), "前置：文件应该先在磁盘上"
    response = api_client.delete(f"/api/xhs/materials/files/{file_id}", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["trashed"] is True, "没真的移走（trashed=False）"

    # **核心判据 ①**：原位置已经没有这个文件了（真删）
    assert not target.exists(), "文件还在磁盘上 —— 没有真删"
    # 索引行也清了（否则列表还会显示一张点不开的图）
    with db_factory() as db:
        assert db.get(MaterialFile, file_id) is None

    # **核心判据 ②**：文件**进了回收站**（「可逆」的实证 —— 不是「调了 send2trash」）。
    # ⚠️ 只断言 ① 是不够的：`os.unlink` 也会让文件从原位置消失，
    #    那样「可逆」这个用户明确要的性质就**没有任何用例在守**
    #    （实测：把这个判据去掉后，`unlink` 变异体 0 红 —— 假绿）。
    #    判据取「回收站根目录下能找到同名残留」—— send2trash 会保留原文件名。
    assert str(target) in _paths_in_recycle_bin(), (
        f"文件不在回收站里 —— 说明是**不可逆删除**（用户口径要求「删得可逆」）。"
        f"回收站现有记录数={len(_paths_in_recycle_bin())}"
    )


def _paths_in_recycle_bin() -> set[str]:
    """回收站里记录着的**原始路径**集合（Windows 的 `$I*` 元数据文件）。

    ⚠️ 两个实测踩过的坑（第一版两条都踩了 ⇒ 假红）：

    ① **回收站里不保留原文件名** —— `send2trash` 改名成系统短名
       （实测 `probe_unique_name.png` → `$I01L56G.png`）。
       「按文件名 glob 回收站」**永远命中不了**。
    ② **`$I` 文件不是「24 字节之后全是路径」** —— 头部除固定字段外，
       **路径前面还夹着一个 UTF-16 码元**（实测 ``）。
       按字节写死 24 会**多读/少读一个字符**、匹配不上。

    ⇒ 鲁棒做法：整段按 UTF-16LE 解码，然后**从盘符开始截**（`C:` / `D:` …）。
    """
    import glob as _glob
    import re as _re

    if os.name != "nt":  # pragma: no cover —— 本项目只跑 Windows
        return set()
    found: set[str] = set()
    #: ⚠️ 第三个实测坑：**回收站按盘符分**（`C:/$Recycle.Bin` 只有 C 盘删的东西）。
    #:    只 glob `C:` 时，删 D 盘的文件会「查不到」⇒ 假红。
    #:    ⇒ 必须枚举**全部盘符**（`A:` ~ `Z:`），存在才读。
    drive_re = _re.compile(r"[A-Za-z]:\\")
    roots: list[str] = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        pattern = f"{letter}:/$Recycle.Bin/*/$I*"
        if _glob.glob(pattern):
            roots.append(pattern)
    for pattern in roots:
        for meta in _glob.glob(pattern):
            try:
                raw = open(meta, "rb").read()
            except OSError:  # pragma: no cover —— 某些 SID 目录无权限
                continue
            text = raw.decode("utf-16-le", errors="ignore")
            match = drive_re.search(text)
            if not match:
                continue
            tail = text[match.start():]
            # 路径本身不含 `\0`；遇到它就说明路径结束（后面的都是填充 / 垃圾）
            candidate = tail.split("\u0000")[0].strip()
            if candidate:
                found.add(candidate)
    return found


def test_delete_is_user_scoped(api_client, db_factory, tmp_path):
    """**别人的文件删不掉**（改 id 也不行）—— 归属纪律与其余素材端点一致。

    ⚠️ **删除路径上有两道归属校验**（文件 + 文件夹），这是**刻意的纵深**：
    只拆掉「文件」那道时另一道会兜住 ⇒ 从**端点行为**看仍然是 404
    （实测：只断言状态码时，`noscope` 变异体 0 红 = 防线被削弱却看不出来）。
    ⇒ 必须**同时**断言底层判据 `_get_owned_file` 跨用户取不到行。
    """
    from backend.app.api.platforms.xhs.material_files import _get_owned_file

    _register(api_client, "mat-owner")
    headers_other = _register(api_client, "mat-other")
    _, file_id, target = _seed_folder_and_file(db_factory, tmp_path, "mat-owner")

    # ① 底层判据：**别的用户取不到这一行**（拆掉这道防线本断言必红）
    other = _user_by_name(db_factory, "mat-other")
    with db_factory() as db:
        with pytest.raises(Exception) as excinfo:
            _get_owned_file(db, file_id, other)
        assert "MATERIAL_FILE_NOT_FOUND" in str(excinfo.value), (
            f"跨用户取到了别人的文件行（归属校验失效）：{excinfo.value}"
        )

    # ② 端点行为：404 且文件还在（端到端确认）
    response = api_client.delete(f"/api/xhs/materials/files/{file_id}", headers=headers_other)
    assert response.status_code == 404, response.text
    assert target.exists(), "跨用户删除把别人的文件删了！"


def test_delete_batch_rejects_over_limit_without_partial_execution(
    api_client, db_factory, tmp_path
):
    """批量**超限整体拒绝**（不部分执行）。

    为什么必须整体拒绝：部分成功会让用户**不知道到底删了几张** ——
    页面报「已删 50 张」而他想删 80 张，剩下 30 张静默留着，他以为删干净了。
    """
    from backend.app.api.platforms.xhs.material_files import MATERIAL_DELETE_BATCH_LIMIT

    headers = _register(api_client, "mat-batch-limit")
    _, file_id, target = _seed_folder_and_file(db_factory, tmp_path, "mat-batch-limit")

    too_many = list(range(1, MATERIAL_DELETE_BATCH_LIMIT + 2))  # 多一个
    response = api_client.post(
        "/api/xhs/materials/files/delete-batch",
        headers=headers,
        json={"file_ids": too_many},
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "MATERIAL_DELETE_BATCH_TOO_LARGE"
    assert target.exists(), "超限被拒，但文件已经被删了（部分执行了）"


def test_delete_batch_deletes_all_and_reports_skipped(api_client, db_factory, tmp_path):
    """正常批量：都删掉，且**如实报**哪些 id 没找到（不谎报数量）。"""
    headers = _register(api_client, "mat-batch-ok")
    folder_id, file_id, target = _seed_folder_and_file(db_factory, tmp_path, "mat-batch-ok")
    user = _user_by_name(db_factory, "mat-batch-ok")
    second = target.parent / "b.png"
    second.write_bytes(b"x")
    with db_factory() as db:
        row_b = MaterialFile(
            user_id=user.id, folder_id=folder_id, filename="b.png", file_type="image",
            rel_path="b.png", size_bytes=1,
            mtime=datetime.fromtimestamp(second.stat().st_mtime),
        )
        db.add(row_b)
        db.commit()
        db.refresh(row_b)
        second_id = row_b.id

    response = api_client.post(
        "/api/xhs/materials/files/delete-batch",
        headers=headers,
        json={"file_ids": [file_id, second_id, 99999]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] == 2, body
    assert body["skipped"] == [99999], "不存在的 id 应如实回报，不静默吞掉"
    assert not target.exists() and not second.exists()


def test_upload_refuses_to_overwrite_existing_file(api_client, db_factory, tmp_path):
    """上传**同名不覆盖** —— 覆盖是**不可逆**地毁用户文件，与「可逆」口径正面冲突。"""
    headers = _register(api_client, "mat-up")
    folder_id, _, target = _seed_folder_and_file(db_factory, tmp_path, "mat-up", filename="dup.png")
    original = target.read_bytes()

    response = api_client.post(
        "/api/xhs/materials/files/upload",
        headers=headers,
        data={"folder_id": str(folder_id)},
        files={"file": ("dup.png", b"NEW-CONTENT", "image/png")},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "MATERIAL_UPLOAD_EXISTS"
    assert target.read_bytes() == original, "已有文件被覆盖了（不可逆地毁用户数据）"


def test_upload_writes_file_and_creates_index_row(api_client, db_factory, tmp_path):
    """上传成功：文件落盘 + **立刻建索引行**。

    ⚠️ 索引行必须当场建：否则用户上传完**刷新页面看不到自己的文件**
    （要等他手动点「刷新」触发扫描），体验是断的。
    """
    headers = _register(api_client, "mat-up-ok")
    folder_id, _, _ = _seed_folder_and_file(db_factory, tmp_path, "mat-up-ok", filename="keep.png")

    response = api_client.post(
        "/api/xhs/materials/files/upload",
        headers=headers,
        data={"folder_id": str(folder_id)},
        files={"file": ("new.png", b"NEW-BYTES", "image/png")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["filename"] == "new.png"
    assert Path(body["path"]).read_bytes() == b"NEW-BYTES"

    with db_factory() as db:
        row = db.scalar(select(MaterialFile).where(MaterialFile.id == body["id"]))
        assert row is not None, "上传后没有索引行 —— 刷新页面看不到这个文件"
        assert row.rel_path == "new.png"


@pytest.mark.parametrize(
    "bad_name",
    ["../evil.png", "..\\evil.png", "sub/evil.png", "sub\\evil.png"],
)
def test_upload_rejects_path_traversal(api_client, db_factory, tmp_path, bad_name):
    """上传文件名**不许带路径分隔符**（穿越防线：`../x` 会把文件写到文件夹外面）。"""
    headers = _register(api_client, "mat-up-trav")
    folder_id, _, _ = _seed_folder_and_file(db_factory, tmp_path, "mat-up-trav", filename="k.png")

    response = api_client.post(
        "/api/xhs/materials/files/upload",
        headers=headers,
        data={"folder_id": str(folder_id)},
        files={"file": (bad_name, b"x", "image/png")},
    )
    # 末段化之后 `../evil.png` 会变成 `evil.png`（合法的），这是可接受的处理；
    # **不可接受**的是把文件写到了文件夹**外面**。
    if response.status_code == 400:
        assert response.json()["detail"]["code"] == "MATERIAL_UPLOAD_BAD_FILENAME"
    else:
        written = Path(response.json()["path"]).resolve()
        root = tmp_path / "materials-mat-up-trav"
        assert written.parent == root.resolve(), f"文件被写到文件夹外：{written}"


def test_upload_rejects_non_image_video_type(api_client, db_factory, tmp_path):
    """只收图 / 视频（本区域不接收任意文件）。"""
    headers = _register(api_client, "mat-up-type")
    folder_id, _, _ = _seed_folder_and_file(db_factory, tmp_path, "mat-up-type", filename="k.png")

    response = api_client.post(
        "/api/xhs/materials/files/upload",
        headers=headers,
        data={"folder_id": str(folder_id)},
        files={"file": ("x.exe", b"MZ", "application/octet-stream")},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "MATERIAL_UPLOAD_BAD_TYPE"
