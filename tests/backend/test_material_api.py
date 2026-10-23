"""T3 素材库端点契约测试（设计 17 §7.3，7 端点）。

覆盖：归属隔离（跨用户 404）、四种结构化错误码、202 异步语义、
`enabled` 停用不删索引、DELETE 只删索引不碰用户文件、files 列表筛选/排序/分页、
取文件字节流（含「索引在、磁盘已删」的 404）。

⚠️ 一律 `tmp_path` 造假目录，**绝不扫真实磁盘**。
⚠️ 用 `api_client`（自动把模块级 SessionLocal 打到测试库）—— 后台扫描线程自建
Session 也才会落测试库；⚠️ 后台线程是 daemon，断言一律用 `waitFor` 式轮询而不是盲等。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import select

from backend.app.models import MaterialFile, MaterialFolder

import pytest


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration
BS = "\\"


def _register(client: Any, email: str = "a@test.com") -> tuple[str, int]:
    # ⚠️ username 有 ≥3 字符下限，邮箱前缀（如 "u1"）会 422 ⇒ 固定补足长度。
    r = client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "pw123456",
            "username": "user_" + email.split("@")[0],
        },
    )
    assert r.status_code in (200, 201), r.text
    token = r.json()["access_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    return token, me.json()["id"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_image(path: Path, size: tuple[int, int] = (120, 90)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (10, 120, 200)).save(path)


def _folder_state(client: Any, token: str, folder_id: int) -> dict | None:
    r = client.get("/api/xhs/materials/folders", headers=_auth(token))
    for f in r.json():
        if f["id"] == folder_id:
            return f
    return None


def _wait_scan(
    client: Any,
    token: str,
    folder_id: int,
    *,
    after: str | None = None,
    timeout: float = 8.0,
) -> dict:
    """轮询 `last_scan_at` 直到扫描完成（§7.3 声明的判据口径）。

    ⚠️ `after` 是本用例**手动触发**（`POST /folders/{id}/scan`）之前记录的
    `last_scan_at`。`POST /folders` 建库时已经跑过一次首扫（目录当时可能是空的），
    所以「等到 `last_scan_at` 非 None」会**立刻返回**上一次扫描的结果 ——
    必须等它**变得比 `after` 新**，才是本次手动扫描真的落库了。
    """
    deadline = time.time() + timeout
    last: dict | None = None
    while time.time() < deadline:
        state = _folder_state(client, token, folder_id)
        if state is not None:
            last = state
            cur = state["last_scan_at"]
            if cur is not None and (after is None or cur != after):
                return state
        time.sleep(0.05)
    raise AssertionError(f"扫描未在 {timeout}s 内完成，最后状态：{last}")


# ───────────────────────── 文件夹 CRUD ───────────────────────── #

def test_folder_create_then_first_scan(api_client: Any, tmp_path: Path):
    token, _ = _register(api_client)
    media = tmp_path / "media"
    _make_image(media / "one.jpg")
    _make_image(media / "sub" / "two.png")

    r = api_client.post(
        "/api/xhs/materials/folders",
        json={"name": "我的图库", "path": str(media)},
        headers=_auth(token),
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["name"] == "我的图库"
    assert body["enabled"] is True
    assert body["last_scan_at"] is None, "落库当下还没扫"

    done = _wait_scan(api_client, token, body["id"])
    assert done["file_count"] == 2

    files = api_client.get("/api/xhs/materials/files", headers=_auth(token)).json()
    assert files["total"] == 2


def test_folder_create_rejects_unsafe_path(api_client: Any, tmp_path: Path):
    token, _ = _register(api_client)
    for bad in ("C:" + BS + "Windows", "/etc", str(tmp_path / "nope")):
        r = api_client.post(
            "/api/xhs/materials/folders",
            json={"name": "x", "path": bad},
            headers=_auth(token),
        )
        assert r.status_code == 400, (bad, r.text)
        assert r.json()["detail"]["code"] == "MATERIAL_PATH_UNSAFE"


def test_folder_create_duplicate_path_conflicts(api_client: Any, tmp_path: Path):
    token, _ = _register(api_client)
    media = tmp_path / "media"
    media.mkdir()

    first = api_client.post(
        "/api/xhs/materials/folders", json={"name": "a", "path": str(media)}, headers=_auth(token)
    )
    assert first.status_code == 202

    dup = api_client.post(
        "/api/xhs/materials/folders", json={"name": "b", "path": str(media)}, headers=_auth(token)
    )
    assert dup.status_code == 409
    assert dup.json()["detail"]["code"] == "MATERIAL_FOLDER_EXISTS"


def test_folder_create_duplicate_normalized_path_conflicts(api_client: Any, tmp_path: Path):
    """末尾斜杠 / `.` 不得绕过唯一性（创建前统一 `resolve()`）。"""
    token, _ = _register(api_client)
    media = tmp_path / "media"
    media.mkdir()

    api_client.post(
        "/api/xhs/materials/folders", json={"name": "a", "path": str(media)}, headers=_auth(token)
    )
    dup = api_client.post(
        "/api/xhs/materials/folders",
        json={"name": "b", "path": str(media) + BS + "."},
        headers=_auth(token),
    )
    assert dup.status_code == 409


def test_folder_patch_rename_and_disable_keeps_index(api_client: Any, tmp_path: Path):
    token, _ = _register(api_client)
    media = tmp_path / "media"
    _make_image(media / "one.jpg")
    fid = api_client.post(
        "/api/xhs/materials/folders", json={"name": "a", "path": str(media)}, headers=_auth(token)
    ).json()["id"]
    _wait_scan(api_client, token, fid)

    r = api_client.patch(
        f"/api/xhs/materials/folders/{fid}",
        json={"name": "改名后", "enabled": False},
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["name"] == "改名后" and r.json()["enabled"] is False

    # ⚠️ 停用**不物理删**索引（重启用即恢复）
    files = api_client.get("/api/xhs/materials/files", headers=_auth(token)).json()
    assert files["total"] == 1, "停用不得删除索引行"


def test_folder_delete_removes_index_but_not_source_files(api_client: Any, tmp_path: Path):
    token, _ = _register(api_client)
    media = tmp_path / "media"
    src = media / "keep.jpg"
    _make_image(src)
    fid = api_client.post(
        "/api/xhs/materials/folders", json={"name": "a", "path": str(media)}, headers=_auth(token)
    ).json()["id"]
    _wait_scan(api_client, token, fid)

    r = api_client.delete(f"/api/xhs/materials/folders/{fid}", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["removed_files"] == 1

    assert api_client.get("/api/xhs/materials/folders", headers=_auth(token)).json() == []
    assert api_client.get("/api/xhs/materials/files", headers=_auth(token)).json()["total"] == 0
    # ⛔ 硬约束 C2：用户源文件必须原封不动
    assert src.exists() and src.stat().st_size > 0


def test_folder_delete_survives_thumbnail_cleanup_failure(
    api_client: Any, tmp_path: Path, monkeypatch: Any
):
    """缩略图清理炸了也必须返回 200 —— 「失败只记日志」不能是空话（DEF-048 同型）。

    ⚠️ 真机场景：装了「批删守卫」的环境里 `Path.unlink()` 抛的是 **`SystemExit`**
    （`BaseException` 直系，**不是 `OSError`**）。端点若写 `except OSError` 就兜不住
    ⇒ 异常穿透 ⇒ 整个 DELETE 请求 500（2026-10-02 全量回归实测红）。
    本用例把守卫形态钉死：抛 `SystemExit` 时端点仍返回 200、索引行照删、
    用户源文件不动 —— 变异（把兜底改回 `except OSError`）必然红。
    """
    token, _ = _register(api_client)
    media = tmp_path / "media"
    src = media / "keep.jpg"
    _make_image(src)
    fid = api_client.post(
        "/api/xhs/materials/folders", json={"name": "a", "path": str(media)}, headers=_auth(token)
    ).json()["id"]
    _wait_scan(api_client, token, fid)

    # 模拟批删守卫：unlink 直接抛 SystemExit（不是 OSError）
    def _boom(self: Path) -> None:
        raise SystemExit(1)

    monkeypatch.setattr(Path, "unlink", _boom)

    r = api_client.delete(f"/api/xhs/materials/folders/{fid}", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["removed_files"] == 1
    assert api_client.get("/api/xhs/materials/folders", headers=_auth(token)).json() == []
    assert api_client.get("/api/xhs/materials/files", headers=_auth(token)).json()["total"] == 0
    assert src.exists() and src.stat().st_size > 0


def test_folder_scan_endpoint_accepted(api_client: Any, tmp_path: Path):
    token, _ = _register(api_client)
    media = tmp_path / "media"
    _make_image(media / "one.jpg")
    fid = api_client.post(
        "/api/xhs/materials/folders", json={"name": "a", "path": str(media)}, headers=_auth(token)
    ).json()["id"]
    _wait_scan(api_client, token, fid)

    r = api_client.post(f"/api/xhs/materials/folders/{fid}/scan", headers=_auth(token))
    assert r.status_code == 202
    assert r.json()["status"] == "scanning"
    assert r.json()["first_scan"] is False


def test_folder_scan_requires_enabled(api_client: Any, tmp_path: Path):
    token, _ = _register(api_client)
    media = tmp_path / "media"
    media.mkdir()
    fid = api_client.post(
        "/api/xhs/materials/folders", json={"name": "a", "path": str(media)}, headers=_auth(token)
    ).json()["id"]
    api_client.patch(
        f"/api/xhs/materials/folders/{fid}", json={"enabled": False}, headers=_auth(token)
    )

    r = api_client.post(f"/api/xhs/materials/folders/{fid}/scan", headers=_auth(token))
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "MATERIAL_FOLDER_DISABLED"


# ───────────────────────── 归属隔离 ───────────────────────── #

def test_cross_user_folder_is_404(api_client: Any, tmp_path: Path):
    t1, _ = _register(api_client, "u1@test.com")
    t2, _ = _register(api_client, "u2@test.com")
    media = tmp_path / "media"
    media.mkdir()
    fid = api_client.post(
        "/api/xhs/materials/folders", json={"name": "a", "path": str(media)}, headers=_auth(t1)
    ).json()["id"]

    for method, url in (
        ("get", f"/api/xhs/materials/folders/{fid}"),
        ("patch", f"/api/xhs/materials/folders/{fid}"),
        ("delete", f"/api/xhs/materials/folders/{fid}"),
        ("post", f"/api/xhs/materials/folders/{fid}/scan"),
    ):
        r = getattr(api_client, method)(url, headers=_auth(t2), **({"json": {"name": "z"}} if method == "patch" else {}))
        assert r.status_code == 404, (method, url, r.status_code)


def test_folder_list_is_per_user(api_client: Any, tmp_path: Path):
    t1, _ = _register(api_client, "u1@test.com")
    t2, _ = _register(api_client, "u2@test.com")
    m1 = tmp_path / "m1"
    m2 = tmp_path / "m2"
    m1.mkdir()
    m2.mkdir()

    api_client.post("/api/xhs/materials/folders", json={"name": "one", "path": str(m1)}, headers=_auth(t1))
    api_client.post("/api/xhs/materials/folders", json={"name": "two", "path": str(m2)}, headers=_auth(t2))

    assert [f["name"] for f in api_client.get("/api/xhs/materials/folders", headers=_auth(t1)).json()] == ["one"]
    assert [f["name"] for f in api_client.get("/api/xhs/materials/folders", headers=_auth(t2)).json()] == ["two"]


# ───────────────────────── 文件列表 ───────────────────────── #

def _wait_first_scan(api_client: Any, token: str, fid: int, timeout: float = 8.0) -> str:
    """等**建库首扫**真正落库（`last_scan_at` 非 None），返回该时间戳。

    ⚠️ `POST /folders` 是 **202 + 异步首扫**：端点返回时首扫可能还没提交事务。
    若此时立刻取 `last_scan_at` 当「扫描前基线」，会拿到 `None` ⇒ 后续
    `_wait_scan(after=None)` 一看到首扫提交就返回，**手动扫描尚未落库** ⇒
    断言读到 0 条（2026-10-02 实测 `test_files_filter_by_type_and_keyword`
    确定性失败的真因）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = _folder_state(api_client, token, fid)
        if state is not None and state["last_scan_at"] is not None:
            return state["last_scan_at"]
        time.sleep(0.05)
    raise AssertionError(f"建库首扫未在 {timeout}s 内完成 folder={fid}")


def _seed_files(api_client: Any, token: str, media: Path, fid: int) -> None:
    """在目录里造 2 图 + 1 视频，然后**手动触发一次扫描**并等它落库。

    ⚠️ 不能只造文件就等 `last_scan_at`：`POST /folders` 建库时的首扫可能已经跑完
    （目录当时是空的或只有部分文件）⇒ 会读到**上一次**的扫描结果。
    ⚠️ 更要紧的是**先等首扫真正落库**再记基线 —— 否则基线是 `None`，
    `_wait_scan` 会把「首扫提交」误当成本次手动扫描完成（见 `_wait_first_scan`）。
    """
    after = _wait_first_scan(api_client, token, fid)

    _make_image(media / "apple.jpg", (100, 100))
    _make_image(media / "banana.png", (200, 50))
    (media / "clip.mp4").write_bytes(b"\x00" * 32)

    r = api_client.post(f"/api/xhs/materials/folders/{fid}/scan", headers=_auth(token))
    assert r.status_code == 202, r.text
    _wait_scan(api_client, token, fid, after=after)


def test_files_filter_by_type_and_keyword(api_client: Any, tmp_path: Path):
    token, _ = _register(api_client)
    media = tmp_path / "media"
    media.mkdir()
    fid = api_client.post(
        "/api/xhs/materials/folders", json={"name": "a", "path": str(media)}, headers=_auth(token)
    ).json()["id"]
    _seed_files(api_client, token, media, fid)

    videos = api_client.get("/api/xhs/materials/files?file_type=video", headers=_auth(token)).json()
    assert videos["total"] == 1 and videos["items"][0]["filename"] == "clip.mp4"

    kw = api_client.get("/api/xhs/materials/files?keyword=apple", headers=_auth(token)).json()
    assert kw["total"] == 1 and kw["items"][0]["filename"] == "apple.jpg"


def test_files_reject_bad_type(api_client: Any, tmp_path: Path):
    token, _ = _register(api_client)
    r = api_client.get("/api/xhs/materials/files?file_type=audio", headers=_auth(token))
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "MATERIAL_FILE_TYPE_INVALID"


def test_files_sort_and_paginate(api_client: Any, tmp_path: Path):
    token, _ = _register(api_client)
    media = tmp_path / "media"
    media.mkdir()
    fid = api_client.post(
        "/api/xhs/materials/folders", json={"name": "a", "path": str(media)}, headers=_auth(token)
    ).json()["id"]
    _seed_files(api_client, token, media, fid)

    asc = api_client.get(
        "/api/xhs/materials/files?sort=filename&order=asc&page=1&page_size=2", headers=_auth(token)
    ).json()
    assert asc["total"] == 3
    assert asc["page_size"] == 2 and len(asc["items"]) == 2
    assert [i["filename"] for i in asc["items"]] == ["apple.jpg", "banana.png"]

    page2 = api_client.get(
        "/api/xhs/materials/files?sort=filename&order=asc&page=2&page_size=2", headers=_auth(token)
    ).json()
    assert [i["filename"] for i in page2["items"]] == ["clip.mp4"]


# ───────────────────────── 取文件内容 ───────────────────────── #

def test_get_file_content_streams_bytes(api_client: Any, tmp_path: Path):
    token, _ = _register(api_client)
    media = tmp_path / "media"
    media.mkdir()
    src = media / "pic.jpg"
    _make_image(src, (64, 48))
    fid = api_client.post(
        "/api/xhs/materials/folders", json={"name": "a", "path": str(media)}, headers=_auth(token)
    ).json()["id"]
    _wait_scan(api_client, token, fid)

    file_id = api_client.get("/api/xhs/materials/files", headers=_auth(token)).json()["items"][0]["id"]
    r = api_client.get(f"/api/xhs/materials/files/{file_id}/file", headers=_auth(token))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")
    assert r.content == src.read_bytes()


def test_get_file_content_404_when_source_gone(api_client: Any, tmp_path: Path):
    """索引在、磁盘已删 ⇒ 统一 `MATERIAL_FILE_MISSING`（真实脱钩场景）。"""
    token, _ = _register(api_client)
    media = tmp_path / "media"
    media.mkdir()
    src = media / "pic.jpg"
    _make_image(src)
    fid = api_client.post(
        "/api/xhs/materials/folders", json={"name": "a", "path": str(media)}, headers=_auth(token)
    ).json()["id"]
    _wait_scan(api_client, token, fid)

    file_id = api_client.get("/api/xhs/materials/files", headers=_auth(token)).json()["items"][0]["id"]
    src.unlink()  # 用户手动删了文件，还没重扫

    r = api_client.get(f"/api/xhs/materials/files/{file_id}/file", headers=_auth(token))
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "MATERIAL_FILE_MISSING"


def test_get_file_content_cross_user_is_404(api_client: Any, tmp_path: Path):
    t1, _ = _register(api_client, "u1@test.com")
    t2, _ = _register(api_client, "u2@test.com")
    media = tmp_path / "media"
    media.mkdir()
    _make_image(media / "pic.jpg")
    fid = api_client.post(
        "/api/xhs/materials/folders", json={"name": "a", "path": str(media)}, headers=_auth(t1)
    ).json()["id"]
    _wait_scan(api_client, t1, fid)

    file_id = api_client.get("/api/xhs/materials/files", headers=_auth(t1)).json()["items"][0]["id"]
    r = api_client.get(f"/api/xhs/materials/files/{file_id}/file", headers=_auth(t2))
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "MATERIAL_FILE_MISSING"
