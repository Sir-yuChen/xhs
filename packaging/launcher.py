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
import tempfile
import threading
import time
import urllib.request
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


def _pick_free_port() -> int | None:
    """让**操作系统**分配一个当前空闲的端口（`bind(port=0)`）。

    ⚠️ 取到后立刻关闭 socket 再交给 uvicorn —— 中间仍有一个极短的竞态窗口，
    但由内核分配的端口被抢的概率远低于「遍历固定区间」。真正的兜底是
    `start_server` 里的重试（见 `_port_bind_conflict`）。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
        except OSError:
            return None


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

def _app_profile_dir() -> Path:
    """窗口宿主的 profile 目录（**必须可写**，否则内核起来就崩）。

    ⚠️ **不再假定 `%LOCALAPPDATA%` 存在**（2026-10-23，用户口径「不依赖电脑本地
    环境」）：原实现只要环境变量非空就直接用它，若该目录被组策略重定向、被安全
    软件锁住、或干脆不可写，Chromium 会在启动瞬间失败，而**用户看到的只是
    「窗口没出来」**（函数返回的路径没人校验过）。

    现在的顺序：`%LOCALAPPDATA%` → 安装目录 → 系统临时目录，**逐个实测可写**，
    取第一个真的能写进去的；全都不行才退回安装目录（让上层报错可诊断）。
    """
    #: ⚠️ **只认安装目录**（用户口径 2026-10-23「彻底隔绝电脑环境的影响」）：
    #: 原来优先 `%LOCALAPPDATA%` —— 那个变量可能被组策略重定向、被安全软件锁住、
    #: 或在精简系统上不存在，**同一份程序在不同电脑上状态会落在不同地方**，
    #: 出现「这台能用那台不能」而无法复现。现在内核状态（登录态、缓存）跟安装
    #: 目录走：用户卸载/迁移时整个目录一起动，环境之间零串扰。
    candidates: list[Path] = [app_root() / "runtime" / "app-profile"]

    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return path
        except OSError as exc:
            log.warning("profile 目录不可写（%s）：%s，改用下一个候选", path, exc)
    #: 全不可写：仍返回首个候选，让 Chromium 的报错进入日志（而不是这里静默返回空）
    return candidates[0]


def _window_host_candidates() -> list[Path]:
    """窗口内核的**全部**候选路径。

    ⚠️ **路径来源只有一个**：`env_bootstrap_service.deps_root()`（DEF-044 口径：
    同一语义不留两份实现）。它内部已经区分安装态（`<安装目录>/deps/`）与开发态
    （`<仓库>/.hermes/deps/`）—— launcher 若自己再拼一次 `app_root()/"deps"`，
    开发态就会「下载到 .hermes/deps 却在 deps 找」，表现为**下载成功但找不到内核**
    （2026-10-23 实机弹窗「下载已完成但未找到内核文件」正是这个）。

    ⚠️ **不包含任何 `%LOCALAPPDATA%` 回退**（用户口径「彻底隔绝电脑环境的影响」）：
    按环境变量找 = 同一份程序在不同电脑上找不同地方，正是要根除的不确定性。
    """
    candidates: list[Path] = []
    try:
        from backend.app.services import env_bootstrap_service as env  # noqa: PLC0415

        spec = env.spec_by_key("window_host")
        dir_name = spec.dir_name if spec is not None else "window-host"
        candidates.append(env.deps_root() / dir_name / WINDOW_HOST_EXE)
    except Exception as exc:  # noqa: BLE001 - 拿不到就退回安装目录判据
        log.warning("无法从依赖服务取内核路径（%s），退回安装目录判据", exc)
        candidates.append(app_root() / "deps" / "window-host" / WINDOW_HOST_EXE)

    #: 旧版曾把内核放 runtime/ 下，保留一处**只读**兼容
    candidates.append(app_root() / "runtime" / "window-host" / WINDOW_HOST_EXE)
    return candidates


def _window_host_exe() -> Path | None:
    """内置窗口宿主的可执行文件路径（**零环境依赖**的独立窗口）。

    下载落点与查找顺序（候选逐个试，取第一个真的存在且够大的）：
      ① ``<安装目录>/runtime/window-host/`` —— 常规位置（安装时下载到这里）；
      ② ``<安装目录>/deps/window-host/`` —— 依赖服务统一的下载根（两种形态并存）；
      ③ ``%LOCALAPPDATA%/SpiderXHS/window-host/`` —— 安装目录不可写时的回退。

    ⚠️ **必须逐个实测**「文件存在 **且** 体积像样」：解压中断会留下一个能启动但
    没有 app 代码的 Electron，它起来闪一下就退 —— 真机上正是这样表现为「点了没反应」。

    ⚠️ **候选列表只在 `_window_host_candidates()` 里定义一次**。本函数原来另抄了
    一份（写死 `app_root()/"deps"`），于是开发态「下载到 `.hermes/deps`、却去 `deps`
    找」⇒ 实机弹窗「下载已完成但未找到内核文件」（2026-10-23 真机截图）。
    同一语义两份实现，改一处漏一处。
    """
    for exe in _window_host_candidates():
        try:
            if exe.is_file() and exe.stat().st_size > 20 * 1024 * 1024:
                return exe
        except OSError:
            continue
    return None


def open_app_window(url: str) -> bool:
    """以**内置内核**的独立窗口打开 UI（**唯一方式，无浏览器降级**）。

    用户口径（2026-10-23）：「使用自己下的内核，不再依靠电脑环境中的浏览器」
    「彻底隔绝电脑环境的影响」。

    历史（为什么最终砍掉浏览器方案）：真机实测过三种失败 ——
      * 用户电脑只装了 360，没有 Chrome；
      * Edge 在已有 app-profile 会话时打印「正在现有浏览器会话中打开」并
        **立即退出（code 0）**，窗口其实开了却被当成失败；
      * 360 / QQ 等会吞掉 ``--app=``，退化成普通标签页。
    三者都源于「行为取决于那台电脑装了什么」。内置内核把这件事变成常量。

    现在查不到内核就**明确报错并弹窗**告诉用户去下载，绝不静默换浏览器 ——
    静默降级会让用户以为「软件坏了」，而日志里却是「已用 XXX 打开」。
    """

    profile = _app_profile_dir()

    #: **只用内置内核，不做任何浏览器降级**（用户口径 2026-10-23：
    #: 「使用自己下的内核不再依靠电脑环境中的浏览器」）。
    #:
    #: 这里刻意**没有**"找不到就换 Edge / Chrome / 默认浏览器"的分支 —— 那正是
    #: 之前真机出问题的根源：换到哪个浏览器、是否支持 --app、是否被单例转交，
    #: 全部取决于那台电脑装了什么，行为无法预期。查不到内核时**明确报错**，
    #: 让用户看到「去下载内核」的可操作提示，而不是静默降级成一个随机的观感。
    host = _window_host_exe()
    if host is None:
        searched = "\n  ".join(str(p) for p in _window_host_candidates())
        log.error(
            "未找到内置窗口内核（已查找：\n  %s\n）—— 请在软件里点「检查运行依赖」下载，"
            "下载到安装目录的 deps/window-host/ 下。",
            searched,
        )
        _notify_user(
            "缺少独立窗口内核",
            "软件需要下载内置窗口内核（约 220 MB）才能显示界面。\n\n"
            "请在软件窗口里点击「检查运行依赖」→「开始下载」，"
            "完成后无需重启即可打开。",
        )
        return False

    try:
        proc = subprocess.Popen(
            [str(host), f"--url={url}", f"--profile={profile}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001 - 失败要报出来，不静默
        log.error("内置窗口内核启动失败：%s", exc)
        _notify_user("窗口内核启动失败", f"无法启动内置窗口内核：\n{exc}")
        return False

    time.sleep(_APP_WINDOW_SETTLE_SECONDS)
    if proc.poll() is not None:
        log.error(
            "内置窗口内核启动后 %.1fs 内已退出（code=%s）—— 内核文件可能不完整，"
            "请在「检查运行依赖」里「清空本地重新下载」",
            _APP_WINDOW_SETTLE_SECONDS, proc.returncode,
        )
        _notify_user(
            "窗口内核异常退出",
            f"内置窗口内核启动后立即退出（code={proc.returncode}）。\n\n"
            "可能是下载不完整，请在「检查运行依赖」里点「清空本地重新下载」。",
        )
        return False

    _app_window_procs.append(proc)
    log.info("已用内置窗口内核打开：%s（profile=%s，pid=%d）", url, profile, proc.pid)
    return True


def _ensure_window_host_ready() -> bool:
    """启动时确保内置窗口内核就绪（**首次运行会下载**，带进度提示）。

    为什么要在这一层下（而不是只靠网页弹窗）：**界面本身要靠内核打开** ——
    把下载放在界面里就形成死锁（用户要点下载，但界面打不开）。真机反馈的
    「安装选好文件夹，没有自动下内核，也没有使用下载的自带内核」正是这个死锁。
    启动器在开窗前同步取到手，是唯一能保证「装完就能用」的位置。

    进度用 tkinter 显示（**仅本函数**，失败静默回退到无进度窗口）：
    下载 220 MB 期间若没有任何反馈，用户会以为程序卡死而强杀。
    """
    if _window_host_exe() is not None:
        return True

    log.info("内置窗口内核未就绪，开始下载（首次运行需要）")
    progress = _WindowHostProgressDialog()
    try:
        #: 复用后端依赖服务的实现（下载/重试/探测只有一份，DEF-044 口径）
        from backend.app.services import env_bootstrap_service as env  # noqa: PLC0415

        spec = env.spec_by_key("window_host")
        if spec is None:
            log.error("依赖注册表里没有 window_host —— 打包不完整")
            _notify_user("安装不完整", "程序缺少窗口内核的下载配置，请重新安装。")
            return False

        target = env.deps_root() / spec.dir_name
        progress.show()

        def _sink(label: str, done: int, total: int) -> None:
            progress.update(label, done, total)

        env.set_progress_sink(_sink)
        try:
            env._with_retries(spec.fetch, target, spec.name)
        finally:
            env.set_progress_sink(None)
    except Exception as exc:  # noqa: BLE001 - 失败要明确告诉用户怎么办
        log.error("窗口内核下载失败：%s", exc, exc_info=True)
        progress.close()
        _notify_user(
            "窗口内核下载失败",
            f"无法下载界面内核：\n{exc}\n\n"
            "请检查网络后重新启动软件。\n"
            f"日志：{app_root() / 'logs' / 'launcher.log'}",
        )
        return False
    progress.close()

    if _window_host_exe() is None:
        log.error("下载流程结束但仍未找到内核（%s）", _window_host_candidates())
        _notify_user("窗口内核不可用", "下载已完成但未找到内核文件，请重新安装软件。")
        return False
    log.info("内置窗口内核已就绪：%s", _window_host_exe())
    return True


class _WindowHostProgressDialog:
    """下载进度窗口（tkinter；**任何**异常都降级为无窗口，不阻断下载）。

    ⚠️ tkinter 只在**启动器进程**里用（`SpiderXHS.spec` 未排除它 —— 它由
    launcher 直接使用；排除列表针对的是后端业务代码）。
    """

    def __init__(self) -> None:
        self._root = None
        self._label = None
        self._bar = None
        self._closed = False

    def show(self) -> None:
        try:
            import tkinter as tk  # noqa: PLC0415
            from tkinter import ttk  # noqa: PLC0415

            root = tk.Tk()
            root.title("SpiderXHS 首次准备")
            root.geometry("460x150")
            root.resizable(False, False)
            root.attributes("-topmost", True)
            tk.Label(
                root,
                text="正在准备界面内核（首次运行，约 220 MB）",
                font=("Microsoft YaHei", 11),
            ).pack(pady=(22, 8))
            self._label = tk.Label(root, text="连接下载源…", font=("Microsoft YaHei", 9))
            self._label.pack()
            self._bar = ttk.Progressbar(root, length=380, mode="determinate", maximum=100)
            self._bar.pack(pady=12)
            root.protocol("WM_DELETE_WINDOW", lambda: None)  # 下载中不许关
            root.update()
            self._root = root
        except Exception as exc:  # noqa: BLE001 - 无 GUI 也能继续下载
            log.warning("进度窗口不可用（%s），继续无界面下载", exc)
            self._root = None

    def update(self, label: str, done: int, total: int) -> None:
        if self._root is None or self._closed:
            return
        try:
            if total > 0:
                percent = min(100, int(done * 100 / total))
                self._bar["value"] = percent
                self._label.config(
                    text=f"{done / 1048576:.1f} / {total / 1048576:.1f} MB（{percent}%）"
                )
            else:
                self._label.config(text=label)
            self._root.update()
        except Exception:  # noqa: BLE001 - 进度刷新失败不影响下载
            pass

    def close(self) -> None:
        self._closed = True
        if self._root is None:
            return
        try:
            self._root.destroy()
        except Exception:  # noqa: BLE001
            pass
        self._root = None


#: 主窗口标题（**必须与 `packaging/window-host/main.js` 的 `title` 逐字一致**）。
#: 2026-10-23 实测：实际标题是 `Spider_XHS Operations`（**带下划线**）——
#: 我第一版写成 `SpiderXHS Operations`，导致 EnumWindows 一个都匹配不到，
#: 表现为「第二次启动找不到已有窗口」。改标题要同时改这三处：
#:   main.js 的 BrowserWindow.title / 本常量 / 测试脚本。
WINDOW_TITLE = "Spider_XHS Operations"


def _focus_existing_window() -> bool:
    """把**已存在**的主窗口还原并置前（第二次启动时调用，不开新窗口）。

    为什么用 Win32 枚举而不是记住 pid：上次那个窗口可能是上一个 launcher 进程
    开的（eg. 用户关了托盘图标但窗口留着、或启动器崩溃后重启），pid 早失效。
    按**窗口标题**枚举是所有情况都成立的判据。

    找不到返回 False（调用方只记日志）——「窗口被最小化到托盘」时本来就没有
    顶层窗口，那不是错误。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
    except Exception:  # noqa: BLE001
        return False

    SW_RESTORE = 9
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):  # noqa: ANN001
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value.strip() == WINDOW_TITLE:
            found.append(hwnd)
            return False  # 找到就停
        return True

    user32.EnumWindows(_enum, 0)
    if not found:
        log.info("没找到标题为「%s」的窗口（可能已最小化到托盘）", WINDOW_TITLE)
        return False

    hwnd = found[0]
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        log.info("已把已有窗口带到前台（hwnd=%s）", hwnd)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("置前已有窗口失败（%s）", exc)
        return False


def _notify_user(title: str, message: str) -> None:
    """弹一个**原生**消息框（窗口没起来时，用户没有别的途径知道发生了什么）。

    ⚠️ 不用 `tkinter`（打包时被排除，导入即失败），也不依赖 Web 界面
    （界面正是打不开的那个东西）。Win32 `MessageBoxW` 零依赖、必然可见。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes  # noqa: PLC0415

        ctypes.windll.user32.MessageBoxW(0, message, title, 0x30)  # MB_ICONWARNING
    except Exception:  # noqa: BLE001 - 弹窗失败不影响主流程
        pass


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

    #: ⚠️ **不再有「用默认浏览器打开」**（用户口径 2026-10-23「使用自己下的内核，
    #: 不再依靠电脑环境中的浏览器」）：那个入口点下去会用**用户电脑上那个浏览器**
    #: 打开，行为随机器而异 —— 正是要根除的东西。界面统一走内置内核。
    icon = pystray.Icon(APP_NAME, image, f"{APP_NAME} :{port}", pystray.Menu(
        pystray.MenuItem("打开界面", open_ui),
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
            log.error(
                "已有实例在运行，但 %ss 内没找到它的健康端口（%d-%d）",
                STARTUP_TIMEOUT_SECONDS, args.port, args.port + MAX_PORT_OFFSET,
            )
            return 1

        #: ⚠️ **只把已有窗口带到前台，绝不再开一个**（用户口径 2026-10-23：
        #: 「应用安装后不支持重复启动多个窗口」）。
        #: 原实现在这里又调了一次 `open_app_window()` —— 于是每双击一次图标就
        #: 多一个主窗口（还各自占着同一个 profile，互相抢锁）。
        #: 正确语义是「单实例 = 单窗口」：第二次启动 = 「请把那个窗口显示出来」。
        log.info("已有实例在端口 %d 运行 ⇒ 把它的窗口带到前台（不新开窗口）", target_port)
        if not _focus_existing_window():
            log.info("未能定位已有窗口（可能被最小化到托盘），仅保留现有实例")
        return 0

    #: ⚠️ **端口改为「让系统分配」**（2026-10-23，用户口径「不依赖电脑本地环境」）：
    #: 原来是从 8000 起、最多回退到 8010 的**固定区间** —— 这就是环境依赖：
    #: 用户电脑上这 11 个端口若全被别的程序占着（开发机常见：本地起了别的服务），
    #: 程序直接 `return 1` 退出，而用户只看到「点了没反应」。
    #: 真机日志实证过一次：`[Errno 10048] bind 8000 失败`。
    #:
    #: 现在：**先试用户指定端口（默认 8000，保持「固定地址」的便利），失败就让
    #: 操作系统分配一个确定空闲的端口**（`bind(port=0)` 由内核挑）。这样
    #:   * 任何电脑上都能起来（不赌端口是否空闲）；
    #:   * 前端用相对路径 `/api`，端口变化对界面透明；
    #:   * 老用户的书签/固定端口习惯在「8000 可用」时依然成立。
    started = None
    attempts: list[int] = [args.port]
    for offset in range(1, MAX_PORT_OFFSET + 1):
        attempts.append(args.port + offset)
    #: `0` 放最后 = 「让系统挑」，它几乎不可能失败。
    attempts.append(0)

    for candidate in attempts:
        port = candidate or _pick_free_port()
        if port is None:
            continue
        if candidate and not _bindable(port):
            log.info("端口 %d 不可绑定，跳过", port)
            continue
        started = start_server(root, port, console)
        if started is not None:
            if candidate == 0:
                log.info("固定端口均不可用 ⇒ 已由系统分配端口 %d", port)
            break
        log.warning("端口 %d 启动失败，尝试下一个", port)

    if started is None:
        log.error("无可用端口（已尝试固定区间 %d-%d 与系统分配）", args.port, args.port + MAX_PORT_OFFSET)
        return 1

    server, thread = started
    port = server.config.port
    # alembic 的 fileConfig 在启动迁移时会把日志打黑洞，服务就绪后恢复文件日志
    ensure_file_logging(root, console)
    url = f"http://127.0.0.1:{port}"
    log.info("服务就绪：%s", url)

    #: ⚠️ **开窗前必须先把内核准备好**（2026-10-23 用户口径「安装时自动下载」
    #: 「使用自己下的内核」）。这里有个死锁必须破掉：
    #:   用户要在**界面里**点「检查运行依赖」下载内核，可**界面本身要靠内核打开**
    #:   ⇒ 没人点，内核永远没有，界面永远打不开（真机反馈的正是这个）。
    #: 所以在启动器这一层先同步把内核取到手（带进度提示 + 3 次重试），
    #: 拿到才开窗；拿不到就弹原生窗说明原因（那种情况下 W eb 界面根本不存在）。
    if not args.no_browser:
        if not _ensure_window_host_ready():
            log.error("内置窗口内核不可用，无法打开界面")
            return 1
        open_app_window(url)

    if run_tray(server, thread, port):
        return 0

    # 托盘不可用：退化为等待 uvicorn 线程（进程无窗口，靠任务管理器结束）
    log.warning("托盘不可用，退化为后台等待模式")
    thread.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
