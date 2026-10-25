"""XHS_ALL_IN_ONE 本地常驻启动器：后端 API(8000) + 前端 Web UI(5173)。

由计划任务 `XHS-AllInOne-App` 在登录时用 **pythonw.exe** 拉起（无控制台窗口），
也可以手工跑：

    "C:\\Users\\admin\\AppData\\Local\\Programs\\Python\\Python312\\pythonw.exe" \\
        D:\\work\\XHS\\.hermes\\scripts\\run-app.py

为什么是一个 Python 监督进程，而不是两个计划任务：
  * 子进程崩了不会自己回来，而本任务的要求是「一直在后台跑」——所以由这里负责重启；
  * 用 pythonw.exe 运行 ⇒ 全程没有控制台窗口（与 `Hermes-SessionSync` 同一惯例）；
  * 不再依赖 PowerShell 的 `Start-Process` + `cmd /c` 重定向（实测在计划任务环境里静默失败，
    子进程根本起不来，日志里连一行子进程输出都没有）。

两个入口都开（覆盖两种使用习惯）：
  * http://127.0.0.1:8000  ← 后端直出**生产构建** `frontend/dist`（本次用环境变量打开
    `FRONTEND_SERVE_STATIC=true`，**不改仓库里的 config/default.yaml**）
  * http://127.0.0.1:5173  ← Vite dev server（带 HMR，`/api` 反代到 8000）

日志：`.hermes/logs/{app,vite}.log`（追加写；每次重启都留一行退出码）
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

# ⚠️ 故意不用 resolve()：项目目录经 junction（D:\work\XHS）访问时 resolve 会
# 解回物理路径，子进程 cwd 就变回旧文件夹名。保留调用方传入的路径形态，
# 让整棵服务树（uvicorn / vite）都跑在用户看到的文件夹路径上。
ROOT = pathlib.Path(__file__).parents[1]
PYTHON = r"C:\Users\admin\AppData\Local\Programs\Python\Python312\python.exe"
NODE = r"C:\Users\admin\AppData\Local\hermes\node\node.exe"
VITE_JS = ROOT / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
LOG_DIR = ROOT / ".hermes" / "logs"
API_LOG = LOG_DIR / "app.log"
VITE_LOG = LOG_DIR / "vite.log"

#: CREATE_NO_WINDOW：即使不小心用 python.exe 启动也不会弹控制台
CREATE_NO_WINDOW = 0x08000000

#: 日志轮转阈值（设计 64 §2.4）：app.log 是 stdout 重定向（Python 侧
#: RotatingFileHandler 管不到 fd）⇒ 在 launcher 的**每次打开句柄前**检查，
#: 超 50MB 就改名滚动（保留 app.log.1~3，最老的丢弃）。原来追加写、无上限
#: —— 只增不减的日志文件最终会吃满盘。
LOG_ROTATE_BYTES = 50 * 1024 * 1024
LOG_ROTATE_KEEP = 3


def rotate_log_if_large(path) -> None:
    """超阈值则把 `path` 滚动成 `path.1`（旧的依次后移，最老丢弃）。"""
    try:
        if not path.exists() or path.stat().st_size < LOG_ROTATE_BYTES:
            return
        for i in range(LOG_ROTATE_KEEP - 1, 0, -1):
            src = path.with_suffix(path.suffix + f".{i}")
            dst = path.with_suffix(path.suffix + f".{i + 1}")
            if src.exists():
                dst.replace(src)
        path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass  # 轮转失败不阻塞启动（下一轮再试）


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rotate_log_if_large(API_LOG)
    with API_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")


#: 重启退避（秒）：连续崩溃时逐级拉长，起来一次就归零。
#: ⚠️ 旧实现固定 `sleep(5)` ⇒ 起不来时每 5 秒崩一次，`app.log` 刷到 8 万行、
#:   CPU 和磁盘都被这棵树自己吃满，反而让「偶尔起得来」变成「永远起不来」。
BACKOFF_STEPS = (5, 15, 45, 120, 300)


def port_listening(host: str, port: int) -> bool:
    """端口是否已有人在 LISTEN。用于「进程活着但没在服务」的兜底判定。"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


class Child:
    """一个被监督的子进程：输出追加到日志，退出后由 :meth:`ensure` 重启。"""

    def __init__(
        self,
        name: str,
        command: list[str],
        cwd: pathlib.Path,
        log_path: pathlib.Path,
        env: dict[str, str] | None = None,
        port: int | None = None,
    ):
        self.name = name
        self.command = command
        self.cwd = cwd
        self.log_path = log_path
        self.env = env
        self.port = port
        self.process: subprocess.Popen | None = None
        self.handle = None
        #: 连续崩溃次数（成功运行一段时间后归零）与下次允许重启的时间点
        self.failures = 0
        self.next_start_at = 0.0
        self.started_at = 0.0

    def start(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if self.handle is not None:
            try:
                self.handle.close()
            except Exception:  # noqa: BLE001 - 关不掉旧句柄不该阻塞重启
                pass
        rotate_log_if_large(self.log_path)
        self.handle = self.log_path.open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            self.command,
            cwd=str(self.cwd),
            stdin=subprocess.DEVNULL,
            stdout=self.handle,
            stderr=subprocess.STDOUT,
            env=self.env,
            creationflags=CREATE_NO_WINDOW,
        )
        self.started_at = time.time()
        #: ⚠️ 只在这里清退避时间 —— **`ensure()` 失败分支绝不能调 `start()`**，
        #: 否则刚算好的 `next_start_at` 会被这行立刻抹掉，退避形同虚设
        #: （2026-10-22 实测：日志写「restart in 300s」但每 5 秒重启一次）。
        self.next_start_at = 0.0
        log(f"{self.name} started (pid {self.process.pid})")

    def ensure(self) -> None:
        if self.process is None:
            if time.time() >= self.next_start_at:
                self.start()
            return
        code = self.process.poll()
        if code is None:
            #: 进程在跑：跑够 60 秒算「起来过」，清空退避。
            if self.failures and time.time() - self.started_at > 60:
                self.failures = 0
            return

        #: ⚠️⚠️ **这里只登记退避，不立即重启**（用户实测 2026-10-22：失败计数涨到 29、
        #: 每条日志都写着「restart in 300s」却**每 5 秒就重启一次**）。
        #:
        #: 两个 bug 叠在一起：
        #:   ① `start()` 里会把 `next_start_at` 清零 —— 在 ensure() 里先算退避、
        #:      紧接着调 `start()` ⇒ 退避刚写就被抹掉，实际间隔永远是主循环的 5s。
        #:      正解：**只设 `process=None` + `next_start_at`**，让下一轮 ensure()
        #:      按时间判断，到点才 start。
        #:   ② `failures` 只增不减 ⇒ 日志里的 `fail #N` 和延迟都失真。
        #:      （跑满 60 秒会归零，这一半是对的，保留。）
        delay = BACKOFF_STEPS[min(self.failures, len(BACKOFF_STEPS) - 1)]
        self.failures += 1
        self.next_start_at = time.time() + delay
        self.process = None
        log(f"{self.name} exited (code {code}) → restart in {delay}s (fail #{self.failures})")
        if self.handle is not None:
            try:
                self.handle.close()
            except Exception:  # noqa: BLE001
                pass
            self.handle = None


def acquire_single_instance_lock():
    """单实例锁：同一时刻只允许**一个** launcher。

    ⚠️ 为什么必须有：launcher 有两个启动来源（登录触发器 + 每 5 分钟兜底触发器），
    再加上手工启动。旧实现没有任何互斥 ⇒ 两个 launcher 各自起一个 uvicorn 抢 8000，
    抢到的那个活着、抢不到的那个每 5 秒崩一次 —— `app.log` 里
    「exited (code 3) → restart」成对出现、每 10 秒一轮，正是这个形态。

    实现用**独占文件句柄**（Windows 上 `msvcrt.locking`）：进程活着句柄就一直占着，
    进程一死（含被强杀）由操作系统自动释放，不需要手工清理陈旧锁。
    """
    lock_path = LOG_DIR / "run-app.lock"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return None
    except ImportError:  # 非 Windows（理论上用不到），退化为不锁
        return handle
    return handle


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _lock = acquire_single_instance_lock()
    if _lock is None:
        #: 已经在跑：**安静退出**，不写日志、不起子进程。
        #: 「每 5 分钟兜底触发器」命中这里就是这种情况 —— 此时什么都不该做。
        return 0

    api_env = dict(os.environ)
    # 后端直出前端生产构建（只影响静态托管开关；数据仍走仓库根 .env + config/default.yaml 的真实库）
    api_env["FRONTEND_SERVE_STATIC"] = "true"
    api_env["PYTHONIOENCODING"] = "utf-8"
    # M1.9 定时同步三道闸（设计 13 §24.9，2026-09-23 用户口径「按推荐优化」）：
    # 总开关 + 两个同步开关在 launcher 用 env 打开，**不动仓库 config/default.yaml**
    # （沿用 FRONTEND_SERVE_STATIC 先例；回退 = 删掉下面三行）。
    # ⚠️ 总开关的连带面已管控（§24.9）：库内无 auto_tasks / active monitoring /
    # scheduled 发布单 ⇒ 那三类周期任务空转无害；**无流量笔记清理是删本地笔记的
    # 破坏性任务，显式保持关闭**，不搭总闸的车。
    api_env["SCHEDULER_ENABLED"] = "true"
    api_env["XHS_NOTE_SYNC_ENABLED"] = "true"
    api_env["XHS_SHOP_GOODS_SYNC_ENABLED"] = "true"
    api_env["NO_TRAFFIC_CLEANUP_ENABLED"] = "false"

    api = Child(
        "api(uvicorn 127.0.0.1:8000)",
        [PYTHON, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        ROOT,
        API_LOG,
        api_env,
        port=8000,
    )
    log(f"launcher start (pid {os.getpid()}, python {sys.executable})")
    api.start()

    web = None
    if VITE_JS.is_file():
        web = Child(
            "web(vite 127.0.0.1:5173)",
            [NODE, str(VITE_JS), "--host", "127.0.0.1", "--port", "5173"],
            ROOT / "frontend",
            VITE_LOG,
            port=5173,
        )
        web.start()
    else:
        log(f"vite 未找到（{VITE_JS}）→ 只跑后端；8000 仍可直出 dist")

    while True:
        time.sleep(5)
        try:
            api.ensure()
            if web is not None:
                web.ensure()
        except Exception as error:  # noqa: BLE001 - 监督进程必须活着
            log(f"ensure failed: {type(error).__name__}: {error}")
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
