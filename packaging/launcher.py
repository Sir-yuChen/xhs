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
  7. 已有健康实例在跑 ⇒ 只开独立窗口后退出（单实例）；
  8. windowed 打包下给全部子进程注入 ``CREATE_NO_WINDOW``（签名运行时每次请求
     都会 ``subprocess.run(['node', …])``，不抑制则每个请求闪一个黑色控制台窗口）。

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
    """安装目录（**用户选择的那个**）。

    ⚠️ 唯一判据在 `backend.app.services.env_bootstrap_service.install_root()`：
    依赖下载目录（`deps/`）、`data/`、`logs/` 与这里的 `root` 必须**同源**，
    否则会出现「依赖下到了别处、程序找不到」这类环境相关故障（2026-10-23）。
    后端不可导入时（极端情况下）才退回本地推算。
    """
    try:
        from backend.app.services.env_bootstrap_service import install_root

        return install_root()
    except Exception:  # noqa: BLE001 - 后端不可导入则退回本地推算
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
        log.info("包内无 runtime/node（1.0.6 起 node.exe 外置），按 deps/node 解析")
    #: ⚠️ **用户下载的那份 node 也要前插**（2026-10-23 实机报障）：
    #: node.exe 已外置为「首次启动下载」，落在 `<安装根>/deps/node/`；而签名链路是
    #: 裸 `subprocess.run(['node', ...])`（按 PATH 解析）⇒ 不前插就等于没下载，
    #: 绑定账号会报「JS mnsv2 签名失败或未达到硬门禁（mns0201_）」。
    #: 注意顺序：deps/node（用户下到的，版本可控）优先于包内 runtime/node。
    try:
        from backend.app.services.env_bootstrap_service import refresh_runtime_paths

        refreshed = refresh_runtime_paths()
        if refreshed.get("node"):
            log.info("deps node 已前插 PATH：%s", refreshed["node"])
    except Exception as exc:  # noqa: BLE001 - 依赖服务不可用不该拦住启动
        log.warning("前插 deps node 失败（%s），签名链路依赖其它 node", exc)


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
    """端口上跑的是**本项目的后端**吗（不只是「端口有响应」）。

    ⚠️ **2026-10-23 实测事故**：旧实现只看 `status == 200`，把**任何**占用
    8000 端口的 HTTP 服务都当成「自己的后端已就绪」。真机表现：启动器打印
    「端口 8000 已有健康实例 ⇒ 只打开应用窗口」，实际那个端口上是个**空壳**
    （bind 成功但不返回页面），用户看到的就是「页面打不开」。
    ⇒ 必须核对响应体里的**项目标识**，认不出来一律当「不是我的后端」。
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{HEALTH_PATH}", timeout=1.5) as resp:
            if resp.status != 200:
                return False
            body = resp.read(4096).decode("utf-8", errors="replace")
    except Exception:
        return False
    #: 健康端点必须自报项目名（见 `backend/app/main.py` 的 /api/health）。
    #: 拿不到标识就认为不是本项目 ⇒ 让调用方换端口，而不是盲目复用。
    return HEALTH_MARKER in body


#: 单实例锁（进程持有文件句柄，进程退出即释放）。
#: ⚠️ **为什么不用「端口健康」当单实例判据**（2026-10-23 实测）：
#: 启动顺序是「先查健康 → 再起后端」，而**后端已经在起**的两个瞬间之间
#: （或本进程上一轮遗留的 uvicorn 还没退干净时）健康检查就会返回 True ⇒
#: 误判成「已有实例在跑」而**跳过自己的启动**，用户拿到一个连不上后端的窗口。
#: 文件锁的语义是「有没有**另一个 launcher 进程**」，与端口无关，不会误判。
_SINGLE_INSTANCE_LOCK: list = []


def _acquire_single_instance() -> bool:
    """尝试取得单实例锁。True = 我是第一个实例；False = 已有实例在跑。"""
    if sys.platform != "win32":
        return True
    try:
        import msvcrt  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return True
    lock_path = app_root() / "runtime" / ".launcher.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+")  # noqa: SIM115 - 生命周期覆盖整个进程
    except OSError:
        return True
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return False
    _SINGLE_INSTANCE_LOCK.append(handle)
    return True


def _bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _port_bind_conflict(port: int) -> bool:
    """端口上现在是否**确有服务在监听**（用来区分「被抢占」与「启动报错」）。

    ⚠️ 判据是「连接得上」而不是「bind 得开」：`_bindable()` 此刻必然返回 False
    （端口被占），无法区分是谁占的；能连上说明确实有东西在监听，属于竞态被抢。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect(("127.0.0.1", port))
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
        # 停机兜底：独立窗口若还没关，前端 5s 轮询会不断重连，优雅停机会一直等连接；
        # 给 5s 超时强断，保证托盘「退出」在关窗口失败时也能落停
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name=f"uvicorn-{port}", daemon=True)
    thread.start()

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _healthy(port):
            return server, thread
        if not thread.is_alive():
            #: ⚠️ **线程已退出要分两种**（2026-10-23 真机实测 `[Errno 10048]`）：
            #:   a) 端口在「探测可用」到「真正 bind」之间被别人抢走（TOCTOU 竞态，
            #:      `_bindable()` 已经放行，uvicorn 自己 bind 时撞上）⇒ **换端口重试**；
            #:   b) 其它启动错误 ⇒ 交给调用方换端口并报错。
            #: 原来一律 `return None`，导致 uvicorn 日志里明明「startup complete」了，
            #: 启动器却认为启动失败 ⇒ 用户拿到一个连不上后端的窗口（真机 bug）。
            if _port_bind_conflict(port):
                log.warning("端口 %d 在启动瞬间被占用（TOCTOU），换下一个端口", port)
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

#: 健康响应里必须出现的**项目标识**（区分「本项目的后端」与「别的程序占了端口」）。
#: 与 `backend/app/api/health.py`（或 main.py 的健康端点）的返回体保持一致。
HEALTH_MARKER = "spider-xhs"

APP_WINDOW_SIZE = "1440,900"

#: 内置窗口宿主的可执行文件名（安装时下载到 `<安装目录>/runtime/window-host/`）。
WINDOW_HOST_EXE = "SpiderXHSWindow.exe"

#: 判定「窗口真的起来了」的观察窗口（秒）。
#: Chromium 单例转交 / `--app` 被吞时，进程会在 1 秒内退出 ⇒ 这个值只要能盖住
#: 那个退出延迟即可；给 2.0 秒留余量（启窗本身就是异步的，不宜更长，否则拖慢启动）。
_APP_WINDOW_SETTLE_SECONDS = 2.0

#: 起过的**每一个**应用窗口进程；托盘退出时逐个收掉。
#:
#: ⚠️ 必须是**列表**（2026-09-26 用户实测「托盘退出后应用窗口依旧未关闭」）：
#: 单变量只记住最后一次 Popen 的句柄，而 Chromium 系浏览器有**单例机制** ——
#: 同一个 `--user-data-dir` 已有实例在跑时，新进程把 URL 转交给它之后**立刻退出**。
#: 于是「启动时开窗 + 用户点托盘『打开界面』再开一次」之后，句柄指向的是一个
#: 已经退出的空壳：旧实现 `if proc.poll() is not None: return` 因此**什么都没关**，
#: 而屏幕上那个窗口属于上一个进程（它的 pid 已经没人记着了）。
_app_window_procs: list[subprocess.Popen] = []

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

#: ⚠️ **优先级固定为三档，不枚举品牌**（用户口径 2026-10-23）：
#:
#:   ① **Edge** ② **Chrome** ③ **电脑默认浏览器**
#:
#: 原实现在 ①② 都没找到时直接 `webbrowser.open()` —— 于是「默认浏览器是 360」的
#: 机器上就变成在 360 的**标签页**里打开，用户看到的是「无法打开单独的窗口」。
#: 这不是缺陷，是**第 ③ 档的设计行为**；真正要修的是 ①② 的查找方式：
#:
#:   * 硬编码两条路径**不够**（Store 版 / 精简版 Edge 落在 WindowsApps，
#:     Chrome 装在非默认盘、或只有 Beta/Dev 通道）；
#:   * ⇒ 先用**注册表 `App Paths`**（浏览器安装时自己写的，权威且不枚举品牌），
#:     再兜底硬编码路径，都找不到才走 ③。
#:
#: 明确**不**在这里加 360 / QQ / 搜狗 / 夸克 之类的品牌清单 —— 那是把「某一台机器的
#: 环境」写进代码，换台机器又要加一条（同一类坑）。要独立窗口，要么机器上有
#: Edge/Chrome，要么用 ③ 并在托盘里明确告诉用户当前是标签页模式。


def _candidate_browsers() -> list[tuple[str, Path]]:
    """按**固定优先级**列出可用于独立窗口的浏览器：**先 Edge，再 Chrome**。

    ⚠️ 排序规则（用户口径 2026-10-23「不要硬编码，默认优先级时，Edge，谷歌，
    最后是电脑默认浏览器」）：本函数**只输出 Edge / Chrome 两个品牌**，第 ③ 档
    （电脑默认浏览器）由 `open_app_window()` 在本函数全部落空后走
    `_open_default_browser()` —— 这里不枚举它们，也不枚举 360 / QQ / 搜狗。

    每个品牌内部的查找顺序（**先权威、后常见路径**）：
      1. 注册表 `App Paths`（浏览器安装时自己写的；Store 版 / 非默认盘 / Beta 通道都在）
      2. 程序目录硬编码路径
      3. `%LOCALAPPDATA%` 用户级安装路径

    去重按**大小写不敏感的完整路径**做 —— 同一 exe 被上面几条各命中一次时，
    重复会让 `open_app_window` 对同一个 exe 白试两遍（第二次必然被单例转交）。
    """
    out: list[tuple[str, Path]] = []
    local = os.environ.get("LOCALAPPDATA") or ""

    def add(label: str, path: Path | None) -> None:
        if path is not None:
            out.append((label, path))

    for label, exe_name, hardcoded, user_relative in (
        (
            "Microsoft Edge",
            "msedge.exe",
            (
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ),
            "Microsoft/Edge/Application/msedge.exe",
        ),
        (
            "Google Chrome",
            "chrome.exe",
            (
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ),
            "Google/Chrome/Application/chrome.exe",
        ),
    ):
        add(label, _app_paths_from_registry(exe_name))
        for p in hardcoded:
            add(label, Path(p))
        if local:
            add(label, Path(local) / user_relative)

    seen: set[str] = set()
    uniq: list[tuple[str, Path]] = []
    for label, path in out:
        key = _win_path_key(path)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((label, path))
    return uniq


def _win_path_key(path: Path) -> str:
    """Windows 路径比较键（大小写不敏感；非 Windows 原样）。"""
    text = str(path)
    return text.lower() if sys.platform == "win32" else text


def _app_paths_from_registry(exe_name: str) -> Path | None:
    """读 `HKLM/HKCU\\...\\App Paths\\<exe>` 拿真实安装路径（读不到返回 None）。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg  # noqa: PLC0415 - 仅 Windows 存在
    except Exception:  # pragma: no cover - 非 Windows
        return None
    sub = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, sub) as key:
                value, _ = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        #: 注册表里常带引号（`"C:\...\msedge.exe"`）
        text = str(value or "").strip().strip('"')
        if text:
            return Path(text)
    return None


def _app_profile_dir() -> Path:
    """独立窗口专属 profile：``%LOCALAPPDATA%\\SpiderXHS\\app-profile``。"""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_NAME / "app-profile"
    return app_root() / "runtime" / "app-profile"


def _window_host_exe() -> Path | None:
    """内置窗口宿主的可执行文件路径（**零环境依赖**的独立窗口）。

    安装时下载到 ``<安装目录>/runtime/window-host/``（打包时不进安装包，
    见 `packaging/INSTALL_DEPS.md`）。找不到就返回 None，由调用方降级到浏览器。
    """
    exe = app_root() / "runtime" / "window-host" / WINDOW_HOST_EXE
    return exe if exe.is_file() else None


def open_app_window(url: str) -> bool:
    """以独立应用窗口打开 UI。

    **优先级（2026-10-23 起）**：
      ① **内置窗口宿主**（自带 Chromium，不读用户任何浏览器）—— 首选，
         见 `packaging/window-host/main.js`；
      ② Edge → ③ Chrome（``--app=``，只是兜底）；
      ④ 电脑默认浏览器（标签页）。

    为什么把内置宿主放第一（用户口径「不再依赖不同电脑的不同环境，无法确认」）：
    真机实测过三种失败 —— 用户没装 Chrome；Edge 在已有 app-profile 会话时打印
    「正在现有浏览器会话中打开」并**立即退出（code 0）**，窗口确实开了却被误判成
    失败；360/QQ 等浏览器会吞掉 ``--app=`` 变普通标签页。只有自带内核能让所有
    电脑的行为一致。
    """

    profile = _app_profile_dir()

    #: ① **内置窗口宿主**（自带 Chromium）—— 与用户装了什么都无关。
    host = _window_host_exe()
    if host is not None:
        try:
            proc = subprocess.Popen(
                [str(host), f"--url={url}", f"--profile={profile}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except Exception as exc:
            log.warning("内置窗口宿主启动失败（%s），降级到浏览器候选", exc)
        else:
            time.sleep(_APP_WINDOW_SETTLE_SECONDS)
            if proc.poll() is None:
                _app_window_procs.append(proc)
                log.info("已用内置窗口宿主打开（零环境依赖）：%s（pid=%d）", url, proc.pid)
                return True
            log.warning(
                "内置窗口宿主启动后 %.1fs 内已退出（code=%s），降级到浏览器候选",
                _APP_WINDOW_SETTLE_SECONDS, proc.returncode,
            )
    else:
        log.warning(
            "未找到内置窗口宿主（%s）—— 说明安装时依赖未下载成功，"
            "将降级到 Edge/Chrome/默认浏览器。请在「检查运行依赖」里重新下载。",
            app_root() / "runtime" / "window-host" / WINDOW_HOST_EXE,
        )

    args = [
        f"--app={url}",
        f"--user-data-dir={profile}",
        f"--window-size={APP_WINDOW_SIZE}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    tried: list[str] = []
    for label, exe in _candidate_browsers():
        if not exe.is_file():
            continue
        tried.append(f"{label}={exe}")
        try:
            proc = subprocess.Popen(
                [str(exe), *args],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except Exception as exc:
            log.warning("%s 启动失败（%s），尝试下一个候选", label, exc)
            continue
        time.sleep(_APP_WINDOW_SETTLE_SECONDS)
        if proc.poll() is not None:
            #: 单例转交 / 参数被吞 / 进程直接崩 —— 三种都在这里落网
            log.warning(
                "%s 启动后 %.1fs 内已退出（code=%s），继续下一个候选",
                label, _APP_WINDOW_SETTLE_SECONDS, proc.returncode,
            )
            continue
        _app_window_procs.append(proc)
        log.info(
            "已用 %s 打开独立应用窗口：%s（profile=%s，pid=%d）",
            label, url, profile, proc.pid,
        )
        return True
    log.warning(
        "第①②档均不可用（候选=%s），改用**电脑默认浏览器**打开 %s（标签页模式，不是独立窗口）",
        tried or "无", url,
    )
    return _open_default_browser(url)


def _open_default_browser(url: str) -> bool:
    """用**系统默认浏览器**打开（独立窗口不可用时的兜底；托盘菜单也调它）。"""
    try:
        ok = bool(webbrowser.open(url))
        if ok:
            log.info("已用系统默认浏览器打开 %s", url)
        else:
            log.warning("系统默认浏览器打开失败，请手动访问 %s", url)
        return ok
    except Exception as exc:
        log.warning("打开浏览器失败（%s），请手动访问 %s", exc, url)
        return False


def _taskkill_tree(pid: int) -> None:
    """按 pid 连整棵进程树强杀（独占 user-data-dir ⇒ 树里全是我们的窗口）。"""
    if sys.platform != "win32":
        return
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            # 进程已退出是最常见的原因，不是错误 —— 记一行就够（退出流程不该卡住）
            log.info("taskkill pid=%d 返回 %d（进程可能已退出）", pid, result.returncode)
    except Exception as exc:
        log.warning("taskkill pid=%d 失败：%s", pid, exc)


def _kill_browsers_using_profile(profile: Path) -> None:
    """兜底：**按命令行里的 profile 路径**收掉所有还开着的窗口进程。

    为什么 pid 之外还要这一刀（2026-09-26 用户实测：托盘退出后窗口还在）：

    Chromium 的单例机制会把「用同一个 user-data-dir 再开一次」变成
    **转交给已有实例**，新进程随即退出 —— 我们手里的 pid 于是指向一个空壳，
    而屏幕上的窗口属于更早的那个进程。只按 pid 杀必然漏。

    **为什么不会误伤用户自己的浏览器**：`--user-data-dir` 是**我们独占**的
    专用目录（`%LOCALAPPDATA%\\SpiderXHS\\app-profile`），用户日常浏览器绝不会
    带这个参数。命令行里带它的进程，按定义就是本应用开的窗口。
    """
    if sys.platform != "win32":
        return
    needle = str(profile).replace("'", "''")
    script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        f"$needle = '{needle}'; "
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or Name='msedge.exe'\" | "
        "Where-Object { $_.CommandLine -like ('*' + $needle + '*') } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=20,
        )
        log.info("已按 profile 兜底清理窗口进程（profile=%s）", profile)
    except Exception as exc:
        log.warning("按 profile 清理窗口进程失败（%s）", exc)


def _close_app_window() -> None:
    """关掉我们起的独立窗口（独占 profile ⇒ 独立进程树，不影响用户自己的浏览器）。

    ⚠️ **两条腿都要走**（2026-09-26 用户实测「托盘退出后应用窗口依旧未关闭」）：
    pid 是我们**以为**的窗口进程、profile 是**真相**。只靠 pid 会在浏览器
    单例转交时静默失效（见 `_app_window_procs` 的说明），所以 pid 全杀一遍之后
    再按 profile 兜底扫一遍；反过来也不行 —— 万一 CIM 查询不可用（老系统 / 权限），
    pid 那一刀仍然有效。
    """
    global _app_window_procs
    procs = list(_app_window_procs)
    _app_window_procs.clear()
    for proc in procs:
        log.info("关闭独立应用窗口（pid=%d，poll=%s）", proc.pid, proc.poll())
        _taskkill_tree(proc.pid)
    # 兜底：浏览器单例转交会让上面那几刀落到空壳上（见 `_app_window_procs`）
    _kill_browsers_using_profile(_app_profile_dir())


# ---------------------------------------------------------------------------
# 子进程控制台抑制（2026-09-26，用户实测：页面每有操作就闪黑窗、无法操作）
# ---------------------------------------------------------------------------
# 根因：打包版 SpiderXHS.exe 是 windowed（--noconsole）进程，自身没有控制台；
# 而 Windows 的规则是——无控制台进程 spawn 一个**控制台**程序时，系统会为子进程
# 新建一个控制台窗口。签名运行时（xhs_utils/xhs_core|xhs_pc|xhs_creator 的
# runtime.py、xhs_pc/params.py）每次调用都是裸 ``subprocess.run(['node', …])``，
# PyExecJS（xhs_creator_util）内部同样起 node ⇒ **每个走签名的页面请求都闪一个
# 黑色控制台窗口**（前端 5s 轮询 + 用户操作，频繁到无法操作）。
# dev 模式看不到：uvicorn 跑在用户终端里，子进程继承该控制台，不会新建窗口。
# 修法：进程内给 ``Popen`` 默认注入 ``CREATE_NO_WINDOW``——subprocess.run 与
# PyExecJS 全部经由 Popen，一处 patch 覆盖现有 6 处调用点和未来新增调用点；
# 不逐个改 runtime.py（同一语义不留两份实现）。GUI 子进程（Edge）不受影响。

def suppress_child_console_windows() -> None:
    """windowed 打包下，所有控制台子进程不再弹黑色窗口。"""
    if sys.platform != "win32":
        return
    create_no_window = 0x08000000
    original = subprocess.Popen.__init__

    def patched(self, *args, **kwargs):
        flags = kwargs.get("creationflags") or 0
        if not flags & create_no_window:
            kwargs["creationflags"] = flags | create_no_window
        return original(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched
    log.info("子进程控制台抑制已启用（CREATE_NO_WINDOW，frozen windowed）")


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
        # 窗口是独立 profile 的浏览器进程：不关它，页面会一直停在连不上的界面上，
        # 且轮询连接不断会让 uvicorn 优雅停机互相等待（窗口关掉 = 连接断 = 停机加速）
        _close_app_window()
        icon.stop()

    icon = pystray.Icon(APP_NAME, image, f"{APP_NAME} :{port}", pystray.Menu(
        pystray.MenuItem("打开界面", open_ui),
        #: ⚠️ 兜底入口（用户口径 2026-10-23：「默认被浏览器打开，无法打开单独的窗口」）：
        #: 有些机器装的是精简版 / 商店版 Edge、或浏览器装在非常规路径，`--app=` 起不来，
        #: 于是退回系统默认浏览器（标签页）。给一个**明确**的兜底入口，用户不必去翻
        #: 开始菜单找 exe。默认浏览器打开的是同一个页面，功能完全一致。
        pystray.MenuItem("用默认浏览器打开", lambda: _open_default_browser(f"http://127.0.0.1:{port}")),
        pystray.MenuItem("退出", quit_app),
    ))

    def watchdog() -> None:
        """uvicorn 非预期退出时**先告诉用户去哪看日志**，再收起托盘退出。

        ⚠️ 2026-10-23：原实现只写一行日志就静默退出 —— 真机上用户看到的是
        「程序自己没了」，无从下手。现在必须给出可操作的信息（日志路径）。
        """
        thread.join()
        if not server.should_exit:
            log_path = app_root() / "logs" / "launcher.log"
            log.error("uvicorn 线程已退出（非用户请求），启动器随之退出")
            try:
                import ctypes  # noqa: PLC0415

                ctypes.windll.user32.MessageBoxW(
                    0,
                    f"后端服务意外退出，程序无法继续。\n\n"
                    f"日志：{log_path}\n"
                    f"请把该文件发给技术支持。",
                    "SpiderXHS",
                    0x10,  # MB_ICONERROR
                )
            except Exception:  # noqa: BLE001 - 弹窗失败不影响退出
                pass
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
    if frozen:
        # 必须在 uvicorn 线程启动前生效：签名请求全在服务线程里 spawn node
        suppress_child_console_windows()

    os.chdir(root)
    ensure_directories(root)
    setup_logging(root, console)
    prepare_environment(root)

    log.info(
        "launcher start (frozen=%s, root=%s, python=%s, port=%d)",
        frozen, root, sys.version.split()[0], args.port,
    )

    #: 单实例判据用**文件锁**，不用端口健康（见 `_acquire_single_instance` 的注释）。
    if not _acquire_single_instance():
        #: 已有实例：找出它在哪个端口上（从 8000 起逐个探，且必须**认出是本项目**），
        #: 找到就只是把自己的窗口并过去；找不到说明那个实例还没就绪 ⇒ 等它。
        target_port = None
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            for offset in range(MAX_PORT_OFFSET + 1):
                port = args.port + offset
                if _healthy(port):
                    target_port = port
                    break
            if target_port is not None:
                break
            time.sleep(0.5)
        if target_port is None:
            log.error("已有实例在运行，但 %ss 内没找到它的健康端口（%d-%d）", STARTUP_TIMEOUT_SECONDS, args.port, args.port + MAX_PORT_OFFSET)
            return 1
        log.info("已有实例在端口 %d 运行 ⇒ 只打开应用窗口", target_port)
        if not args.no_browser:
            open_app_window(f"http://127.0.0.1:{target_port}")
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
