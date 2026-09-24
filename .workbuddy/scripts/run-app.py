"""XHS_ALL_IN_ONE 本地常驻启动器：后端 API(8000) + 前端 Web UI(5173)。

由计划任务 `XHS-AllInOne-App` 在登录时用 **pythonw.exe** 拉起（无控制台窗口），
也可以手工跑：

    "C:\\Users\\admin\\AppData\\Local\\Programs\\Python\\Python312\\pythonw.exe" \\
        D:\\work\\XHS\\.workbuddy\\scripts\\run-app.py

为什么是一个 Python 监督进程，而不是两个计划任务：
  * 子进程崩了不会自己回来，而本任务的要求是「一直在后台跑」——所以由这里负责重启；
  * 用 pythonw.exe 运行 ⇒ 全程没有控制台窗口（与 `Hermes-SessionSync` 同一惯例）；
  * 不再依赖 PowerShell 的 `Start-Process` + `cmd /c` 重定向（实测在计划任务环境里静默失败，
    子进程根本起不来，日志里连一行子进程输出都没有）。

两个入口都开（覆盖两种使用习惯）：
  * http://127.0.0.1:8000  ← 后端直出**生产构建** `frontend/dist`（本次用环境变量打开
    `FRONTEND_SERVE_STATIC=true`，**不改仓库里的 config/default.yaml**）
  * http://127.0.0.1:5173  ← Vite dev server（带 HMR，`/api` 反代到 8000）

日志：`.workbuddy/logs/{app,vite}.log`（追加写；每次重启都留一行退出码）
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
PYTHON = r"C:\Users\admin\AppData\Local\Programs\Python\Python312\python.exe"
NODE = r"C:\Users\admin\AppData\Local\hermes\node\node.exe"
VITE_JS = ROOT / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
LOG_DIR = ROOT / ".workbuddy" / "logs"
API_LOG = LOG_DIR / "app.log"
VITE_LOG = LOG_DIR / "vite.log"

#: CREATE_NO_WINDOW：即使不小心用 python.exe 启动也不会弹控制台
CREATE_NO_WINDOW = 0x08000000


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with API_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")


class Child:
    """一个被监督的子进程：输出追加到日志，退出后由 :meth:`ensure` 重启。"""

    def __init__(self, name: str, command: list[str], cwd: pathlib.Path, log_path: pathlib.Path, env: dict[str, str] | None = None):
        self.name = name
        self.command = command
        self.cwd = cwd
        self.log_path = log_path
        self.env = env
        self.process: subprocess.Popen | None = None
        self.handle = None

    def start(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
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
        log(f"{self.name} started (pid {self.process.pid})")

    def ensure(self) -> None:
        if self.process is None:
            self.start()
            return
        code = self.process.poll()
        if code is None:
            return
        log(f"{self.name} exited (code {code}) → restart in 5s")
        if self.handle is not None:
            self.handle.close()
        time.sleep(5)
        self.start()


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

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
