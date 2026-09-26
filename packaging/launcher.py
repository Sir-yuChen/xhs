"""SpiderXHS 桌面启动器（设计 14 §3）。

两种身份：
  * **frozen（PyInstaller onedir）**：安装目录里的 ``SpiderXHS.exe``，无控制台；
  * **开发态**：``python packaging/launcher.py``，直接跑仓库代码（便于调试）。

职责（全部经既有扩展点注入，不改后端业务代码）：
  1. 定位安装目录（frozen: ``sys._MEIPASS`` 的上级；dev: 仓库根）；
  2. 运行时文件夹全部落到安装目录：``data/``、``backend/app/storage/**``、``logs/``；
  3. 环境变量注入口径（``setdefault`` 语义 = 外部显式设置优先）：
     ``DATABASE_SQLITE_PATH`` / ``FRONTEND_SERVE_STATIC`` / ``FRONTEND_BUILD_DIR`` /
     ``CONFIG_FILE`` / ``SECRET_KEY``；
  4. ``PATH`` 前插 ``runtime/node``（签名链路裸调 ``node`` + PyExecJS 都走 PATH）；
  5. uvicorn 起 ``127.0.0.1:<port>``（默认 8000，被占按 8001→8010 回退；
     前端 ``baseURL: "/api"`` 是相对路径，回退对 UI 透明）；
  6. 健康检查通过后开**独立应用窗口**（Edge/Chrome 的 ``--app=`` 模式，无地址栏 /
     无标签栏 / 无工具栏，不是浏览器标签页）；托盘图标提供「打开界面 / 退出」；
  7. 已有健康实例在跑 ⇒ 只开独立窗口后退出（单实例）。

日志：``<安装目录>/logs/launcher.log``（windowed 模式下 stderr 不可见，必须落文件）。
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "SpiderXHS"
DEFAULT_PORT = 8000
MAX_PORT_OFFSET = 10
STARTUP_TIMEOUT_SECONDS = 45.0
HEALTH_PATH = "/api/health"

log = logging.getLogger("launcher")


# ---------------------------------------------------------------------------
# 安装目录定位
# ---------------------------------------------------------------------------

def app_root() -> Path:
    """安装目录：frozen 时是 exe 所在目录（``_internal`` 的上级）。"""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS).resolve().parent
    return Path(__file__).resolve().parents[1]


def asset_path(name: str) -> Path:
    """托盘图标：frozen 时在 ``_internal/assets``，dev 时在 ``packaging/assets``。"""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "assets" / name
    return Path(__file__).resolve().parent / "assets" / name


# ---------------------------------------------------------------------------
# 环境与目录
# ---------------------------------------------------------------------------

def prepare_environment(root: Path) -> None:
    """把「数据落安装目录」的口径注入环境变量（外部已设置的优先，不覆盖）。"""
    data_dir = root / "data"
    os.environ.setdefault("DATABASE_SQLITE_PATH", (data_dir / "spider_xhs.db").as_posix())
    os.environ.setdefault("FRONTEND_SERVE_STATIC", "true")
    os.environ.setdefault("FRONTEND_BUILD_DIR", (root / "frontend" / "dist").as_posix())

    config_file = root / "config" / "default.yaml"
    if config_file.is_file():
        # 绝对路径优先于 config.py 内 __file__ 相对定位（frozen 下也成立），
        # 且这个副本用户可编辑。
        os.environ.setdefault("CONFIG_FILE", config_file.as_posix())

    secret = os.environ.get("SECRET_KEY") or ""
    if not secret:
        secret_file = data_dir / "secret.key"
        try:
            if secret_file.is_file():
                secret = secret_file.read_text(encoding="utf-8").strip()
        except OSError:
            secret = ""
        if not secret:
            secret = secrets.token_urlsafe(48)
            data_dir.mkdir(parents=True, exist_ok=True)
            try:
                secret_file.write_text(secret, encoding="utf-8")
            except OSError as exc:  # 只读介质等：本次运行仍可用，下次重新生成
                log.warning("secret.key 写入失败（%s），本次运行使用临时密钥", exc)
        os.environ["SECRET_KEY"] = secret

    node_dir = root / "runtime" / "node"
    if node_dir.is_dir():
        os.environ["PATH"] = f"{node_dir.as_posix()}{os.pathsep}{os.environ.get('PATH', '')}"
        log.info("node runtime 已前插 PATH：%s", node_dir)
    else:
        log.warning("未找到 bundled node（%s），签名链路依赖系统 PATH 上的 node", node_dir)


def ensure_directories(root: Path) -> None:
    for path in (
        root / "data",
        root / "logs",
        root / "backend" / "app" / "storage" / "media",
        root / "backend" / "app" / "storage" / "exports",
        root / "backend" / "app" / "storage" / "tmp_note_publish",
    ):
        path.mkdir(parents=True, exist_ok=True)


def setup_logging(root: Path, console: bool) -> None:
    root_log = root / "logs" / "launcher.log"
    handlers: list[logging.Handler] = [
        logging.FileHandler(root_log, encoding="utf-8"),
    ]
    if console and sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )


def uvicorn_log_config(root: Path, console: bool) -> dict:
    """uvicorn 的日志也写同一文件（windowed 下 stderr 不可见）。"""
    handlers = {
        "file": {
            "class": "logging.FileHandler",
            "filename": str(root / "logs" / "launcher.log"),
            "encoding": "utf-8",
            "formatter": "default",
        },
    }
    if console and sys.stderr is not None:
        handlers["console"] = {"class": "logging.StreamHandler", "formatter": "default"}
    active = list(handlers)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"default": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
        "handlers": handlers,
        "root": {"handlers": active, "level": "INFO"},
        "loggers": {
            "uvicorn": {"handlers": active, "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": active, "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": active, "level": "INFO", "propagate": False},
        },
    }


def ensure_file_logging(root: Path, console: bool) -> None:
    """把文件日志挂回 root（治 alembic 的 fileConfig 黑洞）。

    alembic 的 ``env.py`` 启动迁移时会对 ``alembic.ini`` 做 ``fileConfig``：
    它把 root 的 handler 换成 ini 里的 console（windowed 冻结版 ``sys.stderr``
    是 None ⇒ 此后所有日志凭空消失），并按 ini 的 logger 名单**禁用**其余
    logger（uvicorn / launcher 全灭）⇒ 实测启动迁移之后 launcher.log 只剩
    alembic 那几行，访问日志、「服务就绪」、「托盘就绪」全没有。
    服务就绪后调用本函数恢复：root 重新挂 FileHandler，并显式启用关键 logger。
    """
    target = os.path.abspath(root / "logs" / "launcher.log")
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler) and os.path.abspath(
            getattr(handler, "baseFilename", "")
        ) == target:
            return
    setup_logging(root, console)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "launcher"):
        logger = logging.getLogger(name)
        logger.disabled = False
        logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# 端口探测与起服
# ---------------------------------------------------------------------------

def _healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{HEALTH_PATH}", timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def start_server(root: Path, port: int, console: bool):
    """在端口起 uvicorn；成功返回 ``(server, thread)``，失败返回 None。"""
    import uvicorn

    from backend.app.main import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_config=uvicorn_log_config(root, console),
        access_log=True,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name=f"uvicorn-{port}", daemon=True)
    thread.start()

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _healthy(port):
            return server, thread
        if not thread.is_alive():
            return None
        time.sleep(0.25)

    log.error("端口 %d 启动超时（%ss 内未就绪）", port, STARTUP_TIMEOUT_SECONDS)
    server.should_exit = True
    thread.join(timeout=10)
    return None


# ---------------------------------------------------------------------------
# 独立应用窗口（2026-09-26 起：不再走系统默认浏览器的标签页）
# ---------------------------------------------------------------------------
# 口径：优先 Edge（Win10/11 自带），其次 Chrome，用 ``--app=<url>`` 起「无地址栏 /
# 无标签栏 / 无工具栏」的独立窗口；配**独占 user-data-dir** —— ①不受用户已开着的
# 浏览器实例影响（共用 profile 时 --app 参数可能被已有进程吞掉，窗口不开）；
# ②不共享用户的插件 / 历史 / 登录态。两者都找不到才退回系统默认浏览器。

APP_WINDOW_SIZE = "1440,900"

_BROWSER_CANDIDATES = (
    (
        "Microsoft Edge",
        (
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ),
    ),
    (
        "Google Chrome",
        (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ),
    ),
)


def _candidate_browsers() -> list[tuple[str, Path]]:
    """机器上可能存在的 Edge / Chrome 可执行文件（先系统级，后用户级）。"""
    out: list[tuple[str, Path]] = []
    for label, paths in _BROWSER_CANDIDATES:
        for p in paths:
            out.append((label, Path(p)))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        base = Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe"
        out.append(("Google Chrome", base))
        out.append(("Microsoft Edge", Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
    return out


def _app_profile_dir() -> Path:
    """独立窗口专属 profile：``%LOCALAPPDATA%\\SpiderXHS\\app-profile``。"""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_NAME / "app-profile"
    return app_root() / "runtime" / "app-profile"


def open_app_window(url: str) -> bool:
    """以独立应用窗口打开 UI；Edge/Chrome 都不可用时退回默认浏览器。"""
    profile = _app_profile_dir()
    args = [
        f"--app={url}",
        f"--user-data-dir={profile}",
        f"--window-size={APP_WINDOW_SIZE}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    for label, exe in _candidate_browsers():
        if not exe.is_file():
            continue
        try:
            subprocess.Popen(
                [str(exe), *args],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            log.info("已用 %s 打开独立应用窗口：%s（profile=%s）", label, url, profile)
            return True
        except Exception as exc:
            log.warning("%s 启动失败（%s），尝试下一个候选", label, exc)
    log.warning("未找到 Edge / Chrome，退回系统默认浏览器打开 %s", url)
    try:
        return bool(webbrowser.open(url))
    except Exception as exc:
        log.warning("打开浏览器失败（%s），请手动访问 %s", exc, url)
        return False


# ---------------------------------------------------------------------------
# 托盘
# ---------------------------------------------------------------------------

def run_tray(server, thread: threading.Thread, port: int) -> bool:
    """起托盘图标；返回 False 表示托盘不可用（调用方退化为 join 等待）。"""
    try:
        import pystray
        from PIL import Image
    except Exception as exc:  # pragma: no cover - 打包缺依赖时的兜底
        log.error("托盘不可用（pystray/PIL 导入失败：%s）", exc)
        return False

    icon_file = asset_path("app.ico")
    try:
        image = Image.open(icon_file)
    except Exception as exc:
        log.error("托盘图标读取失败（%s：%s）", icon_file, exc)
        return False

    def open_ui() -> None:
        open_app_window(f"http://127.0.0.1:{port}")

    def quit_app() -> None:
        log.info("用户从托盘退出")
        server.should_exit = True
        icon.stop()

    icon = pystray.Icon(APP_NAME, image, f"{APP_NAME} :{port}", pystray.Menu(
        pystray.MenuItem("打开界面", open_ui),
        pystray.MenuItem("退出", quit_app),
    ))

    def watchdog() -> None:
        """uvicorn 非预期退出时收起托盘，让进程跟着退。"""
        thread.join()
        if not server.should_exit:
            log.error("uvicorn 线程已退出（非用户请求），启动器随之退出")
            icon.stop()

    threading.Thread(target=watchdog, name="tray-watchdog", daemon=True).start()
    log.info("托盘图标就绪（端口 %d）", port)
    icon.run()
    return True


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} desktop launcher")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="首选端口（被占则顺延）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--console", action="store_true", help="强制控制台日志（dev 调试用）")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = app_root()
    frozen = bool(getattr(sys, "frozen", False))
    console = args.console or not frozen

    os.chdir(root)
    ensure_directories(root)
    setup_logging(root, console)
    prepare_environment(root)

    log.info(
        "launcher start (frozen=%s, root=%s, python=%s, port=%d)",
        frozen, root, sys.version.split()[0], args.port,
    )

    if _healthy(args.port):
        log.info("端口 %d 已有健康实例 ⇒ 只打开应用窗口", args.port)
        if not args.no_browser:
            open_app_window(f"http://127.0.0.1:{args.port}")
        return 0

    started = None
    for offset in range(MAX_PORT_OFFSET + 1):
        port = args.port + offset
        if not _bindable(port):
            log.info("端口 %d 不可绑定，跳过", port)
            continue
        started = start_server(root, port, console)
        if started is not None:
            break
        log.warning("端口 %d 启动失败，尝试下一个", port)

    if started is None:
        log.error("所有候选端口均不可用（%d-%d）", args.port, args.port + MAX_PORT_OFFSET)
        return 1

    server, thread = started
    port = server.config.port
    # alembic 的 fileConfig 在启动迁移时会把日志打黑洞，服务就绪后恢复文件日志
    ensure_file_logging(root, console)
    url = f"http://127.0.0.1:{port}"
    log.info("服务就绪：%s", url)
    if not args.no_browser:
        open_app_window(url)

    if run_tray(server, thread, port):
        return 0

    # 托盘不可用：退化为等待 uvicorn 线程（进程无窗口，靠任务管理器结束）
    log.warning("托盘不可用，退化为后台等待模式")
    thread.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
