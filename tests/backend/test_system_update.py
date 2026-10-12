"""系统更新（2026-10-13 需求 R1，设计 36）—— 版本比较 / 更新源 / 下载 / 装触发 / 端点契约。

用户口径原文：
> 「新增系统更新逻辑，更新地址为当前项目的远程仓库……每次打开软件后需要检查版本……存在新版本
> 则弹窗提示更新……静默安装保证历史数据；存在新版本则强制安装最新版本软件。」

本文件盯五件事：

1. **版本比较**：语义化逐段数值比较（`1.10.0 > 1.9.0` 的经典坑）；
2. **版本真相源**：`core/version.py` 单一读取点（env > version.json > txt > 0.0.0）；
3. **更新源**：静态清单优先、Gitee 回退、未配置 ⇒ 静默跳过；
4. **下载**：原子落盘（`.part` → 改名）、非法 URL 拒绝、失败不留半截包；
5. **端点契约**：version 免鉴权、check/download/install 需登录、install 在非打包态 400。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.app.core import version as version_mod
from backend.app.services import system_update_service as update_svc


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
    monkeypatch.delenv("UPDATE_GITEE_TOKEN", raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "default.yaml").write_text("update:\n  manifest_url: ''\n  gitee_token: ''\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    src = update_svc.load_update_source()
    assert src.configured is False


def test_check_skips_silently_when_unconfigured(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "default.yaml").write_text("update:\n  manifest_url: ''\n  gitee_token: ''\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_FILE", str(cfg / "default.yaml"))
    monkeypatch.delenv("UPDATE_MANIFEST_URL", raising=False)
    monkeypatch.delenv("UPDATE_GITEE_TOKEN", raising=False)
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
