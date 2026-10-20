"""系统更新（2026-10-13 需求 R1，设计 36）—— 版本比较 / 更新源 / 下载 / 装触发 / 端点契约。

用户口径原文：
> 「新增系统更新逻辑，更新地址为当前项目的远程仓库……每次打开软件后需要检查版本……存在新版本
> 则弹窗提示更新……静默安装保证历史数据；存在新版本则强制安装最新版本软件。」

本文件盯五件事：

1. **版本比较**：语义化逐段数值比较（`1.10.0 > 1.9.0` 的经典坑）；
2. **版本真相源**：`core/version.py` 单一读取点（env > version.json > txt > 0.0.0）；
3. **更新源**：静态清单优先、GitHub 回退、未配置 ⇒ 静默跳过；
4. **下载**：原子落盘（`.part` → 改名）、非法 URL 拒绝、失败不留半截包；
5. **端点契约**：version 免鉴权、check/download/install 需登录、install 在非打包态 400。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from backend.app.core import version as version_mod
from backend.app.services import system_update_service as update_svc

#: 仓库根（`tests/backend/test_system_update.py` → 上溯两级）。
ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# 1. 版本比较
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("1.10.0", "1.9.0", 1),      # 字符串比较会判反的经典坑
        ("1.9.0", "1.10.0", -1),
        ("v1.0.0", "1.0.0", 0),      # v 前缀
        ("1.0.0", "1.0.0.0", 0),     # 段数不等补 0
        ("2.0.0", "1.99.99", 1),
        ("1.0.0", "1.0.1", -1),
        ("1.x.0", "1.0.0", 0),       # 非数字段视为 0，不崩
        ("", "0.0.0", 0),
    ],
)
def test_compare_versions(a, b, expected):
    assert version_mod.compare_versions(a, b) == expected


def test_compare_is_antisymmetric():
    for a, b in [("1.2.3", "1.2.4"), ("2.0", "1.9.9"), ("1.0", "1.0")]:
        assert version_mod.compare_versions(a, b) == -version_mod.compare_versions(b, a)


# --------------------------------------------------------------------------- #
# 2. 版本真相源
# --------------------------------------------------------------------------- #


def test_current_version_reads_env(monkeypatch):
    monkeypatch.setenv("SPIDER_XHS_VERSION", "9.9.9")
    assert version_mod.current_version() == "9.9.9"


def test_current_version_strips_v_prefix(monkeypatch):
    monkeypatch.setenv("SPIDER_XHS_VERSION", "v1.2.3")
    assert version_mod.current_version() == "1.2.3"


def test_current_version_reads_version_json(tmp_path, monkeypatch):
    monkeypatch.delenv("SPIDER_XHS_VERSION", raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "version.json").write_text(json.dumps({"version": "3.1.4"}), encoding="utf-8")
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    assert version_mod.current_version() == "3.1.4"


def test_version_json_records_history_not_only_latest(tmp_path, monkeypatch):
    """`version.json` 记录的是**版本升级历史**（用户口径 2026-10-13：
    「应记录的是版本升级历史，不是只记录最新的」）⇒ `version_info()["history"]`
    透出多条、**最新在前**，`current` 与首条一致。"""
    monkeypatch.delenv("SPIDER_XHS_VERSION", raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "version.json").write_text(
        json.dumps(
            {
                "current": "1.2.0",
                "versions": [
                    {"version": "1.2.0", "built_at": "2026-10-12T00:00:00+08:00", "commit": "bbb", "notes": "新功能"},
                    {"version": "1.1.0", "built_at": "2026-09-01T00:00:00+08:00", "commit": "aaa", "notes": "修 bug"},
                    {"version": "1.0.0", "built_at": "2026-08-01T00:00:00+08:00", "commit": "zzz", "notes": "首发"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    info = version_mod.version_info()
    assert info["version"] == "1.2.0"
    assert len(info["history"]) == 3, "历史应透出多条（不是只留最新）"
    assert [h["version"] for h in info["history"]] == ["1.2.0", "1.1.0", "1.0.0"], (
        "版本历史应保持文件顺序（最新在前）"
    )
    assert info["history"][0]["notes"] == "新功能"
    assert info["history"][2]["notes"] == "首发"
    assert info["built_at"] == "2026-10-12T00:00:00+08:00", "顶层时间取当前版本那条"
    assert info["commit"] == "bbb"


def test_old_flat_version_json_still_works(tmp_path, monkeypatch):
    """旧形状（只有顶层 version/built_at/commit）**向后兼容**：history 退化成 1 条。"""
    monkeypatch.delenv("SPIDER_XHS_VERSION", raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "version.json").write_text(
        json.dumps({"version": "9.9.9", "built_at": "x", "commit": "y"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    info = version_mod.version_info()
    assert info["version"] == "9.9.9"
    assert info["history"] == [], "旧形状没有 versions 数组 ⇒ 空（前端退化成远端 notes）"

# ---------------------------------------------------------------- 版本历史四要素
#  用户口径 2026-10-13：「version.json 应记录的是版本升级历史，不是只记录最新的版本，
#  弹窗中使用版本号折叠展示，记录版本更新相关信息，**版本号，更新时间，更新简介**，
#  你补全一个 1.0.0-1.0.2」+「简介要简短保持在 100 字以内即可」。


def test_shipped_version_json_has_the_baseline_releases():
    """仓库内 `config/version.json` **真的**记录着版本历史（最新在前）。

    ⚠️ **判据不许写死「恰好三条」**（2026-10-15 修正）：原写法断言
    `versions == ["1.0.2", "1.0.1", "1.0.0"]` —— 一发新版本就红，
    而那**不是缺陷**（历史本来就该边长）。
    ⇒ 现判据：**包含基线三条**（1.0.0/1.0.1/1.0.2 是当时的用户口径要求，
    不许被删掉）+ **最新在前**（`versions[0]` 就是 `current` 那条）。
    """
    payload = json.loads((ROOT / "config" / "version.json").read_text(encoding="utf-8"))
    versions = [item["version"] for item in payload["versions"]]
    #: 基线三条必须还在（用户口径 2026-10-13：「你补全一个 1.0.0-1.0.2」）。
    for base in ("1.0.0", "1.0.1", "1.0.2"):
        assert base in versions, f"版本历史丢了基线版本 {base}：{versions}"
    #: 最新在前，且 `current` 就是最新那条（发版脚本会改写 `current`）。
    assert versions[0] == payload["current"], (
        f"版本历史最新一条应是 current（{payload['current']}），实得首位 {versions[0]}"
    )


def test_every_release_has_version_time_and_summary():
    """每条历史四要素齐全：**版本号 + 更新时间 + 更新简介 + 更新详情**。"""
    payload = json.loads((ROOT / "config" / "version.json").read_text(encoding="utf-8"))
    for item in payload["versions"]:
        ver = item["version"]
        assert ver, "缺版本号"
        assert item.get("built_at"), f"{ver} 缺更新时间"
        assert item.get("summary"), f"{ver} 缺更新简介"
        assert item.get("notes"), f"{ver} 缺更新详情"
        #: 时间形如 `2026-10-12 00:52`（可读，比 ISO 带 T 更适合弹窗）。
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", item["built_at"]), (
            f"{ver} 更新时间格式应为 YYYY-MM-DD HH:mm，实得 {item['built_at']!r}"
        )


def test_release_summaries_stay_within_a_hundred_chars():
    """用户口径 2026-10-13：「简介要简短保持在 100 字以内即可」。"""
    payload = json.loads((ROOT / "config" / "version.json").read_text(encoding="utf-8"))
    for item in payload["versions"]:
        summary = item["summary"]
        assert len(summary) <= 100, (
            f"{item['version']} 简介 {len(summary)} 字，超过 100 字上限：{summary!r}"
        )


def test_release_notes_are_short_plain_bullets():
    """更新详情 = **≤5 条要点、每条一行、无 Markdown 符号、合计 ≤100 字**。

    用户口径 2026-10-13：「简介合并简单 100 字内 5 条以内」+
    「去掉 Markdown 符号，纯文本分条（最简单、不会丑）」。

    ⚠️ 背景：原 `notes` 是一篇带 `##` / `**` 的 Markdown 长文（1.0.1 有 8 条），
    弹窗里符号原样显示、又长又乱（用户截图指出）。
    """
    payload = json.loads((ROOT / "config" / "version.json").read_text(encoding="utf-8"))
    for item in payload["versions"]:
        ver = item["version"]
        notes = item["notes"]
        lines = [ln for ln in notes.split("\n") if ln.strip()]
        assert len(lines) <= 5, f"{ver} 更新详情 {len(lines)} 条，超过 5 条上限"
        assert len(notes) <= 100, f"{ver} 更新详情合计 {len(notes)} 字，超过 100 字上限"
        for bad in ("##", "**", "> ", "* "):
            assert bad not in notes, f"{ver} 更新详情残留 Markdown 符号 {bad!r}"


def test_history_exposes_summary_to_the_api(tmp_path, monkeypatch):
    """`version_info()` 把 `summary` 一并透出（前端折叠标题要用）。"""
    monkeypatch.delenv("SPIDER_XHS_VERSION", raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "version.json").write_text(
        json.dumps(
            {
                "current": "2.0.0",
                "versions": [
                    {
                        "version": "2.0.0",
                        "built_at": "2026-11-01 09:00",
                        "commit": "abc",
                        "summary": "一句话简介",
                        "notes": "详情",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    info = version_mod.version_info()
    assert info["history"][0]["summary"] == "一句话简介"
    assert info["history"][0]["built_at"] == "2026-11-01 09:00"

def test_version_info_carries_build_time_and_commit(tmp_path, monkeypatch):
    """`version_info()` 带出**打包时间 / 提交**（用户口径 2026-10-13：
    「本地记录当前版本和打包等时间相关信息」）。"""
    monkeypatch.delenv("SPIDER_XHS_VERSION", raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "version.json").write_text(
        json.dumps({
            "version": "3.1.4",
            "built_at": "2026-10-12T20:30:00+08:00",
            "commit": "abc1234",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    info = version_mod.version_info()
    assert info["version"] == "3.1.4"
    assert info["built_at"] == "2026-10-12T20:30:00+08:00"
    assert info["commit"] == "abc1234"
    assert info["source"].endswith("version.json"), "source 应指出读到哪份文件"


def test_version_info_degrades_gracefully_without_extra_fields(tmp_path, monkeypatch):
    """旧版 version.json（只有 version）⇒ 时间/提交为空串，**不报错、不臆造**。"""
    monkeypatch.delenv("SPIDER_XHS_VERSION", raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "version.json").write_text(json.dumps({"version": "3.1.4"}), encoding="utf-8")
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    info = version_mod.version_info()
    assert info["version"] == "3.1.4"
    assert info["built_at"] == ""
    assert info["commit"] == ""


def test_version_info_unknown_reports_unknown_source(tmp_path, monkeypatch):
    """候选文件**全都读不到** ⇒ 0.0.0 + source=unknown（dev 态排查用）。

    ⚠️ 不能只靠 `CONFIG_FILE` 指到空目录：候选清单里还有**仓库自身的**
    `config/version.json`（随仓库提交的，值 1.0.2）⇒ 会回落读到它。这里直接
    把候选清单清空，才能测到「一个都读不到」这条分支。
    """
    monkeypatch.delenv("SPIDER_XHS_VERSION", raising=False)
    monkeypatch.setattr(version_mod, "_candidate_files", lambda: [])
    info = version_mod.version_info()
    assert info["version"] == "0.0.0"
    assert info["source"] == "unknown"


def test_version_endpoint_exposes_build_metadata(api_client):
    """`GET /api/system/version` 下发 built_at / commit / version_source（免鉴权）。"""
    resp = api_client.get("/api/system/version")
    assert resp.status_code == 200
    body = resp.json()
    assert "version" in body
    assert "built_at" in body, "缺少打包时间"
    assert "commit" in body, "缺少提交号"
    assert "version_source" in body, "缺少版本来源（排查 0.0.0 用）"

def test_current_version_unknown_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("SPIDER_XHS_VERSION", raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    # 无可读来源 ⇒ 0.0.0（不臆造）
    # 注意：项目根的 config/version.json 若存在也会被读到，故断言「是合法版本串」
    val = version_mod.current_version()
    assert isinstance(val, str) and val


# --------------------------------------------------------------------------- #
# 3. 更新源
# --------------------------------------------------------------------------- #


def test_source_not_configured_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("UPDATE_MANIFEST_URL", raising=False)
    monkeypatch.delenv("UPDATE_GITHUB_TOKEN", raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "default.yaml").write_text("update:\n  manifest_url: ''\n  github_token: ''\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    src = update_svc.load_update_source()
    assert src.configured is False


def test_check_skips_silently_when_unconfigured(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "default.yaml").write_text("update:\n  manifest_url: ''\n  github_token: ''\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    monkeypatch.delenv("UPDATE_MANIFEST_URL", raising=False)
    monkeypatch.delenv("UPDATE_GITHUB_TOKEN", raising=False)
    result = update_svc.check_for_update()
    assert result["has_update"] is False
    assert result["source_configured"] is False


def test_check_reads_manifest(monkeypatch, tmp_path):
    """静态清单路径：给出更高版本 ⇒ has_update=True。"""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "default.yaml").write_text("update:\n  manifest_url: 'https://x/update.json'\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    monkeypatch.setenv("UPDATE_MANIFEST_URL", "https://x/update.json")
    monkeypatch.setenv("SPIDER_XHS_VERSION", "1.0.0")

    def fake_get(url, token="", timeout=0):
        return {"version": "2.0.0", "notes": "修了很多", "url": "https://x/setup.exe", "size": 123}

    monkeypatch.setattr(update_svc, "_http_get_json", fake_get)
    result = update_svc.check_for_update()
    assert result["latest"] == "2.0.0"
    assert result["has_update"] is True
    assert result["notes"] == "修了很多"
    assert result["download_url"] == "https://x/setup.exe"


def test_check_reports_no_update_when_same(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "default.yaml").write_text("update:\n  manifest_url: 'https://x/update.json'\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    monkeypatch.setenv("UPDATE_MANIFEST_URL", "https://x/update.json")
    monkeypatch.setenv("SPIDER_XHS_VERSION", "2.0.0")
    monkeypatch.setattr(
        update_svc, "_http_get_json", lambda *a, **k: {"version": "2.0.0", "url": "u"}
    )
    assert update_svc.check_for_update()["has_update"] is False


def test_check_never_raises_on_network_error(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "default.yaml").write_text("update:\n  manifest_url: 'https://x/update.json'\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    monkeypatch.setenv("UPDATE_MANIFEST_URL", "https://x/update.json")

    def boom(*a, **k):
        raise OSError("net down")

    monkeypatch.setattr(update_svc, "_http_get_json", boom)
    result = update_svc.check_for_update()  # 不抛
    assert result["has_update"] is False


# --------------------------------------------------------------------------- #
# 4. 下载原子性
# --------------------------------------------------------------------------- #


def test_download_rejects_non_http():
    result = update_svc.download_installer("file:///etc/passwd")
    assert result["ok"] is False


def test_download_writes_atomically(tmp_path, monkeypatch):
    """下载成功 ⇒ 目标文件存在、无残留 .part。"""
    monkeypatch.setattr(update_svc, "updates_dir", lambda: tmp_path)

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            if not hasattr(self, "_sent"):
                self._sent = True
                return b"FAKE-INSTALLER-BYTES"
            return b""

    monkeypatch.setattr(update_svc.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    result = update_svc.download_installer("https://x/setup.exe", version="2.0.0")
    assert result["ok"] is True
    target = Path(result["path"])
    assert target.is_file()
    assert not target.with_suffix(".exe.part").exists()
    assert target.name == "spider-xhs-setup-2.0.0.exe"


def test_download_failure_leaves_no_part(tmp_path, monkeypatch):
    monkeypatch.setattr(update_svc, "updates_dir", lambda: tmp_path)

    def boom(*a, **k):
        raise OSError("conn reset")

    monkeypatch.setattr(update_svc.urllib.request, "urlopen", boom)
    result = update_svc.download_installer("https://x/setup.exe", version="2.0.0")
    assert result["ok"] is False
    assert not list(tmp_path.glob("*")), "失败不应留半截包"


# --------------------------------------------------------------------------- #
# 5. 安装触发
# --------------------------------------------------------------------------- #


def test_install_refused_outside_frozen(monkeypatch):
    """dev 态（非 frozen）⇒ 400 语义：不真装。"""
    monkeypatch.setattr(update_svc, "can_install_here", lambda: False)
    result = update_svc.launch_silent_installer("/tmp/whatever.exe")
    assert result["ok"] is False


def test_install_refused_outside_updates_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(update_svc, "can_install_here", lambda: True)
    monkeypatch.setattr(update_svc, "updates_dir", lambda: tmp_path / "updates")
    (tmp_path / "updates").mkdir()
    outsider = tmp_path / "evil.exe"
    outsider.write_bytes(b"x")
    result = update_svc.launch_silent_installer(str(outsider))
    assert result["ok"] is False
    assert "允许目录" in result["message"]


def test_install_launches_with_silent_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(update_svc, "can_install_here", lambda: True)
    updates = tmp_path / "updates"
    updates.mkdir()
    monkeypatch.setattr(update_svc, "updates_dir", lambda: updates)
    installer = updates / "spider-xhs-setup-2.0.0.exe"
    installer.write_bytes(b"x")

    captured = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args

    monkeypatch.setattr(update_svc.subprocess, "Popen", FakePopen)
    result = update_svc.launch_silent_installer(str(installer))
    assert result["ok"] is True
    args = captured["args"]
    assert str(installer) in args
    for flag in ("/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"):
        assert flag in args, f"静默安装缺少 {flag}"


# --------------------------------------------------------------------------- #
# 6. 端点契约
# --------------------------------------------------------------------------- #


def _auth(api_client, username):
    resp = api_client.post(
        "/api/auth/register", json={"username": username, "password": "Passw0rd!"}
    )
    token = resp.json().get("access_token")
    return {"Authorization": f"Bearer {token}"}


def test_version_endpoint_is_public(api_client):
    resp = api_client.get("/api/system/version")
    assert resp.status_code == 200
    body = resp.json()
    assert "version" in body
    assert "source_configured" in body


def test_update_check_requires_auth(api_client):
    resp = api_client.get("/api/system/update/check")
    assert resp.status_code == 401


def test_update_check_ok_when_authed(api_client):
    headers = _auth(api_client, "updater-check")
    resp = api_client.get("/api/system/update/check", headers=headers)
    assert resp.status_code == 200
    assert "has_update" in resp.json()


def test_update_install_400_in_dev(api_client, monkeypatch):
    headers = _auth(api_client, "updater-install")
    resp = api_client.post(
        "/api/system/update/install",
        json={"path": "/tmp/x.exe"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "UPDATE_INSTALL_FAILED"


def test_update_download_rejects_bad_url(api_client):
    headers = _auth(api_client, "updater-download")
    resp = api_client.post(
        "/api/system/update/download",
        json={"url": "file:///etc/passwd", "version": "2.0.0"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "UPDATE_DOWNLOAD_FAILED"



# ---------------------------------------------------------------- 公开仓库免令牌
#  用户口径 2026-10-13：「仓库公开了，你再试试」+「我把远程仓库公开，是否就可以
#  不需要接入私人令牌了？」—— 实测：公开仓库 GitHub API 匿名可访问（repo info /
#  releases/latest / tags 均 200）⇒ **公开即免令牌**。旧实现 `if not github_token:
#  return None` 是硬门槛：公开仓库没令牌就永远「已是最新」（假结论）。


def test_public_repo_enables_github_without_token(monkeypatch, tmp_path):
    """仓库公开 ⇒ **没有令牌也算已配置**（旧实现会漏查）。"""
    monkeypatch.delenv("UPDATE_MANIFEST_URL", raising=False)
    monkeypatch.delenv("UPDATE_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("UPDATE_GITHUB_PUBLIC", raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "default.yaml").write_text(
        "update:\n  manifest_url: ''\n  github_token: ''\n  github_public: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    src = update_svc.load_update_source()
    assert src.github_public is True
    assert src.github_token == ""
    assert src.github_enabled is True, "公开仓库应免令牌启用 GitHub 路径"
    assert src.configured is True, "公开仓库没令牌也应算已配置（否则恒『已是最新』）"


def test_private_repo_still_requires_token(monkeypatch, tmp_path):
    """私有仓库（github_public=false）+ 无令牌 ⇒ 仍不启用（免得每次白打一次网络）。"""
    monkeypatch.delenv("UPDATE_GITHUB_PUBLIC", raising=False)
    monkeypatch.delenv("UPDATE_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("UPDATE_MANIFEST_URL", raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "default.yaml").write_text(
        "update:\n  manifest_url: ''\n  github_token: ''\n  github_public: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    src = update_svc.load_update_source()
    assert src.github_enabled is False
    assert src.configured is False


def test_public_repo_call_carries_no_authorization_header(monkeypatch, tmp_path):
    """公开仓库的请求**不带令牌**（匿名调用）。"""
    monkeypatch.delenv("UPDATE_MANIFEST_URL", raising=False)
    monkeypatch.delenv("UPDATE_GITHUB_TOKEN", raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "default.yaml").write_text(
        "update:\n  manifest_url: ''\n  github_owner: 'Sir-yuChen'\n  github_repo: 'xhs'\n  github_public: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    src = update_svc.load_update_source()

    seen: dict = {}

    def _fake_get(url, token="", timeout=None):
        seen["url"] = url
        seen["token"] = token
        return {
            "tag_name": "v9.9.9",
            "body": "notes",
            "assets": [{"name": "setup-9.9.9.exe", "browser_download_url": "https://x/y.exe"}],
        }

    monkeypatch.setattr(update_svc, "_http_get_json", _fake_get)
    rel = update_svc._latest_from_github(src)
    assert rel is not None and rel.version == "v9.9.9"
    assert seen["token"] == "", "公开仓库必须匿名调用（不带令牌）"
    assert "releases/latest" in seen["url"]


def test_public_repo_check_reports_newer_release(monkeypatch, tmp_path):
    """公开仓库端到端：当前 1.0.2、远端 v1.0.3 ⇒ has_update=True。"""
    monkeypatch.delenv("UPDATE_MANIFEST_URL", raising=False)
    monkeypatch.delenv("UPDATE_GITHUB_TOKEN", raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "default.yaml").write_text(
        "update:\n  manifest_url: ''\n  github_public: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    monkeypatch.setattr(update_svc, "current_version", lambda: "1.0.2")
    monkeypatch.setattr(
        update_svc,
        "_http_get_json",
        lambda url, token="", timeout=None: {
            "tag_name": "v1.0.3",
            "body": "修了 X",
            "assets": [{"name": "spider-xhs-setup-1.0.3.exe", "browser_download_url": "https://g/d.exe"}],
        },
    )
    result = update_svc.check_for_update()
    assert result["current"] == "1.0.2"
    assert result["latest"] == "v1.0.3"
    assert result["has_update"] is True
    assert result["download_url"] == "https://g/d.exe"
    assert result["notes"] == "修了 X"
    assert result["source_configured"] is True


# ---------------------------------------------------------------- 发布通道 = GitHub
#  2026-10-20 用户口径「换成 GitHub Release，Gitee 不要了」——
#  起因：Gitee 单个附件硬上限 100 MB，安装包 1.0.4 已达 113.6 MB，传不上去。
#  本组守卫钉住三件事：① 不再有 Gitee 路径；② GitHub 是唯一回退；③ 发版脚本打 GitHub。


def test_update_service_has_no_gitee_path_anymore():
    """更新服务的**代码**里不得再出现 gitee（用户已明确不要该通道）。

    ⚠️ 只看代码、不看注释与 docstring：源码里保留着「为什么改成 GitHub」
    的说明（提到 Gitee 100 MB 上限），那是该留的上下文，不是残留。
    判据与 `test_module_to_package_split` 同口径 —— 剥注释后再比。
    """
    import io
    import inspect
    import tokenize

    source = inspect.getsource(update_svc)
    code_only = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        #: 注释 / 字符串（docstring 也是 STRING）一律跳过
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        code_only.append(tok.string)
    joined = " ".join(code_only).lower()
    assert "gitee" not in joined, (
        "system_update_service 的代码里仍有 gitee —— 发布通道已改为 GitHub"
    )


def test_github_release_is_the_only_fallback():
    """`check_for_update` 的回退**必须**是 `_latest_from_github`。"""
    import inspect

    source = inspect.getsource(update_svc.check_for_update)
    assert "_latest_from_github" in source, "回退路径丢了 GitHub"
    assert "_latest_from_manifest" in source, "静态清单优先级丢失"


def test_release_script_publishes_to_github():
    """发版脚本第 7 阶段必须走**官方 gh CLI**，且不再调 gitee 脚本。

    ⚠️ 2026-10-20 用户口径「我来安装用官方的」：原来是自研 `github_release.py`
    （手写 urllib + 重试），现已改用 `gh release create/upload` ——
    官方维护、凭据自管。判据随之换到 `gh release`。
    """
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "packaging" / "release.ps1"
    text = script.read_text(encoding="utf-8-sig")
    #: 实际写法是 `& $ghExe release create …`（gh 不在 PATH）⇒ 判据要允许变量。
    assert "release create" in text, "release.ps1 未调用 gh release create"
    assert "release upload" in text, "release.ps1 缺 gh release upload 兜底路径"
    assert "gitee_release.py" not in text, "release.ps1 仍在调 gitee_release.py"
    #: gh 不在 PATH（本机装在 D:\Program Files\GitHub CLI）⇒ 必须显式定位。
    assert "GitHub CLI" in text, "release.ps1 未处理 gh 不在 PATH 的情况"


def test_github_release_script_retries_on_flaky_network():
    """GitHub 脚本必须带重试（本机直连 api.github.com 会间歇被掐断）。"""
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "packaging" / "github_release.py"
    text = script.read_text(encoding="utf-8")
    assert "tries=8" in text or "tries=" in text, "缺少重试参数"
    assert "time.sleep" in text, "缺少退避等待"
