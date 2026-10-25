"""系统更新服务（2026-10-13 需求 R1，设计 36）。

## 职责

1. 读「更新源」配置（Gitee Releases API 或静态清单）；
2. 查最新版本 + 更新内容 + 安装包下载 URL；
3. 下载安装包到 `data/updates/`（原子落盘：`.part` → 校验 → 改名）；
4. 触发**静默安装**（Inno Setup `/VERYSILENT /NORESTART /DIR=<原安装目录>`）。

## 更新源（设计 36 §2）

- **首选**：静态清单 `update.manifest_url`（可匿名访问的 `update.json`）—— 零鉴权、最稳；
- **回退**：GitHub Releases API（公开仓库免令牌，匿名可查）；
- 两者都未配置 ⇒ `configured=False`，检查静默返回「无更新」（不打扰用户）。

## 安全网

- 下载写 `.part` 再原子改名（半截包不会被当完整包安装，设计 36 R6）；
- 安装触发只允许 **frozen + Windows**（dev 态只检查不安装，R7）；
- 安装器路径必须是**已下载到 `data/updates/` 的 exe**（不接受任意路径，防注入）。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from backend.app.core.version import compare_versions, current_version

logger = logging.getLogger(__name__)

#: 下载超时（秒）。安装包数量级几十 MB，给足时间但不无限等。
DOWNLOAD_TIMEOUT_SECONDS = 300
#: 查最新版本超时（秒）。
CHECK_TIMEOUT_SECONDS = 15
#: 安装包落盘子目录（相对数据目录）。
UPDATES_SUBDIR = "updates"


@dataclass
class UpdateSource:
    """更新源配置（读 `config/default.yaml` 的 `update:` 段 + 环境变量覆盖）。"""

    #: 静态清单 URL（`update.json`）；非空则**优先**走它。
    manifest_url: str = ""
    #: GitHub 仓库 owner/repo（2026-10-20 用户口径「换成 GitHub Release，Gitee 不要了」）。
    #: 起因：Gitee Release 单个附件硬上限 100 MB，而安装包已达 113.6 MB。
    github_owner: str = "Sir-yuChen"
    github_repo: str = "xhs"
    #: GitHub 私人令牌（**私有仓库**必需；仓库公开后留空即可）。
    github_token: str = ""
    #: GitHub 仓库是否公开（公开 ⇒ 匿名即可查）。
    #: ⚠️ 默认 **False**：缺失该键时不得被当成「已配置」，否则
    #: 「未配置 ⇒ 静默跳过」的契约失效（每次启动都白打一次网络）。
    github_public: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def github_enabled(self) -> bool:
        """GitHub 路径是否可用（**主通道**，与 Gitee 同判据）。"""
        return bool(self.github_token) or bool(self.github_public)

    @property
    def configured(self) -> bool:
        return bool(self.manifest_url) or self.github_enabled


def load_update_source() -> UpdateSource:
    """读更新源配置。环境变量优先级高于 YAML（与全项目配置口径一致）。"""
    data: dict = {}
    try:
        import yaml  # 惰性：避免非打包环境缺依赖

        from backend.app.core.version import _project_root

        candidates = []
        config_file = os.environ.get("CONFIG_FILE")
        if config_file:
            config_path = Path(config_file)
            if not config_path.is_absolute():
                config_path = _project_root() / config_path
            candidates.append(config_path)
        candidates.append(_project_root() / "config" / "default.yaml")
        for path in candidates:
            if path.is_file():
                with open(path, "r", encoding="utf-8-sig") as handle:
                    loaded = yaml.safe_load(handle) or {}
                if isinstance(loaded, dict) and isinstance(loaded.get("update"), dict):
                    data = loaded["update"]
                    break
    except Exception:  # noqa: BLE001 - 配置读不到 ⇒ 视为未配置（不打扰）
        logger.warning("update: 读取更新源配置失败（视为未配置）", exc_info=True)

    return UpdateSource(
        manifest_url=str(os.environ.get("UPDATE_MANIFEST_URL") or data.get("manifest_url") or "").strip(),
        github_owner=str(os.environ.get("UPDATE_GITHUB_OWNER") or data.get("github_owner") or "Sir-yuChen").strip(),
        github_repo=str(os.environ.get("UPDATE_GITHUB_REPO") or data.get("github_repo") or "xhs").strip(),
        github_token=str(os.environ.get("UPDATE_GITHUB_TOKEN") or data.get("github_token") or "").strip(),
        github_public=_as_bool(
            os.environ.get("UPDATE_GITHUB_PUBLIC")
            if os.environ.get("UPDATE_GITHUB_PUBLIC") is not None
            else data.get("github_public") if data.get("github_public") is not None else False
        ),
        raw=data if isinstance(data, dict) else {},
    )


def _as_bool(value: object) -> bool:
    """宽松布尔解析（YAML `true` / 环境变量 `"1"`/`"true"` 都认）。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _http_get_json(url: str, token: str = "", timeout: int = CHECK_TIMEOUT_SECONDS) -> dict:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


@dataclass
class ReleaseInfo:
    """一次「检查更新」的结果。"""

    version: str = ""
    notes: str = ""
    download_url: str = ""
    download_size: int = 0


def _latest_from_manifest(source: UpdateSource) -> ReleaseInfo | None:
    if not source.manifest_url:
        return None
    try:
        payload = _http_get_json(source.manifest_url)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as exc:
        logger.warning("update: 拉取静态清单失败（%s）", exc)
        return None
    version = str(payload.get("version") or "").strip()
    if not version:
        return None
    return ReleaseInfo(
        version=version,
        notes=str(payload.get("notes") or ""),
        download_url=str(payload.get("url") or "").strip(),
        download_size=int(payload.get("size") or 0),
    )


def _latest_from_github(source: UpdateSource) -> ReleaseInfo | None:
    """从 GitHub Release 取最新版（**主通道**，2026-10-20）。

    Gitee 对单个附件有 100 MB 硬上限，安装包超过后传不上去 ⇒ 以 GitHub 为主。
    仓库公开时匿名即可查；私有仓库才需要令牌。
    """
    if not source.github_enabled:
        return None
    owner = urllib.parse.quote(source.github_owner, safe="")
    repo = urllib.parse.quote(source.github_repo, safe="")
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    try:
        payload = _http_get_json(url, token=source.github_token)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as exc:
        logger.warning("update: 拉取 GitHub Release 失败（%s）", exc)
        return None

    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        return None
    notes = str(payload.get("body") or "")
    download_url = ""
    size = 0
    for asset in payload.get("assets") or []:
        name = str(asset.get("name") or "")
        if name.lower().endswith(".exe"):
            download_url = str(asset.get("browser_download_url") or "").strip()
            size = int(asset.get("size") or 0)
            break
    return ReleaseInfo(version=tag, notes=notes, download_url=download_url, download_size=size)


def check_for_update() -> dict:
    """查最新版本；返回契约形状（供端点直接下发）。**绝不抛异常**。"""
    source = load_update_source()
    current = current_version()
    result: dict = {
        "current": current,
        "latest": "",
        "has_update": False,
        "notes": "",
        "download_url": "",
        "download_size": 0,
        "source_configured": source.configured,
    }
    if not source.configured:
        return result

    release = _latest_from_manifest(source)
    if release is None:
        #: 只走 GitHub（2026-10-20 用户口径「Gitee 不要了」）。
        release = _latest_from_github(source)
    if release is None:
        return result

    result["latest"] = release.version
    result["notes"] = release.notes
    result["download_url"] = release.download_url
    result["download_size"] = release.download_size
    result["has_update"] = compare_versions(release.version, current) > 0
    return result


def updates_dir() -> Path:
    """安装包落盘目录（`data/updates/`）。

    ⚠️ v1.1.0（2026-10-24）：原来优先读 `DATABASE_SQLITE_PATH` 环境变量、
    取其父目录，理由是「安装包与库文件同进同出」。**SQLite 退役后那个变量
    再也没有人设**（`Settings` 里也已删该字段）⇒ 该分支恒不命中，属于死代码。
    现在直接用 `core/paths.app_root()/data`（按文件位置推仓库根，不依赖 CWD）。
    """
    from backend.app.core import paths

    target = paths.app_root() / "data" / UPDATES_SUBDIR
    target.mkdir(parents=True, exist_ok=True)
    return target


def download_installer(url: str, *, version: str = "") -> dict:
    """下载安装包到 `data/updates/`（原子落盘）。返回 `{ok, path, size, message}`。

    ⚠️ 只接受 http(s)；写 `.part` 再改名（半截包不落地为可用包）。
    """
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "path": "", "size": 0, "message": "下载地址非法（须为 http/https）"}

    safe_version = "".join(c for c in (version or "") if c.isalnum() or c in ".-_") or "latest"
    filename = f"spider-xhs-setup-{safe_version}.exe"
    target = updates_dir() / filename
    part = target.with_suffix(".exe.part")

    req = urllib.request.Request(url)
    req.add_header("User-Agent", "SpiderXHS-Updater")
    try:
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp:
            size = 0
            with open(part, "wb") as handle:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    handle.write(chunk)
                    size += len(chunk)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
        logger.warning("update: 下载安装包失败（%s）", exc)
        return {"ok": False, "path": "", "size": 0, "message": f"下载失败：{exc}"}

    if size <= 0:
        try:
            part.unlink()
        except OSError:
            pass
        return {"ok": False, "path": "", "size": 0, "message": "下载内容为空"}

    # 原子改名（同目录 rename 在 Windows 上原子）
    os.replace(part, target)
    logger.info("update: 安装包已下载 %s（%s bytes）", target, size)
    return {"ok": True, "path": str(target), "size": size, "message": ""}


def can_install_here() -> bool:
    """能否在本机触发静默安装（仅 frozen + Windows；dev 态只检查不安装）。"""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def launch_silent_installer(installer_path: str) -> dict:
    """触发静默安装（`/VERYSILENT /NORESTART` + 复用原安装目录）。

    ⚠️ 三条约束（设计 36 §4/R7）：
    1. 只在 frozen + Windows 下真正执行，否则返回 `{ok: False, ...}`；
    2. `installer_path` 必须落在 `data/updates/` 内（防任意路径注入）；
    3. 静默参数：`/VERYSILENT /NORESTART /SUPPRESSMSGBOXES` —— 不弹 UI、不重启系统。
       复用原安装目录靠 `UsePreviousAppDir=yes` + 当前运行目录（安装器默认行为）。
    """
    if not can_install_here():
        return {"ok": False, "launching": False, "message": "当前环境不支持静默安装（仅打包版 Windows）"}

    path = Path(installer_path).resolve()
    updates = updates_dir().resolve()
    try:
        path.relative_to(updates)
    except ValueError:
        return {"ok": False, "launching": False, "message": "安装包不在允许目录内"}

    if not path.is_file():
        return {"ok": False, "launching": False, "message": "安装包不存在（请先下载）"}

    # 复用原安装目录：frozen 下 sys.executable 所在目录即安装目录。
    install_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None
    args = [str(path), "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES", "/NOCANCEL"]
    if install_dir is not None:
        args.append(f"/DIR={install_dir}")

    try:
        # 安装器要替换**正在运行的程序文件** ⇒ 必须以独立进程启动（脱离本进程退出）。
        subprocess.Popen(
            args,
            close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except OSError as exc:
        logger.warning("update: 拉起安装器失败（%s）", exc)
        return {"ok": False, "launching": False, "message": f"启动安装器失败：{exc}"}

    logger.info("update: 已拉起静默安装器 %s", path)
    return {"ok": True, "launching": True, "message": ""}
