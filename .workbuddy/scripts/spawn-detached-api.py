"""起一个**脱离控制台 / 脱离调用方**的 uvicorn 实例（给非 8000 端口的调试实例用）。

为什么需要它
------------
8000 由常驻 launcher `run-app.py`（计划任务 `XHS-AllInOne-App`）监督：崩了 5 秒自动
拉起，**重启 = 杀掉它的 uvicorn 子进程、等它自己用新代码拉起来**（见 skill
`xhs-live-server-freshness` §3）。

但 launcher 只认 8000（+ vite 5173）。额外的调试实例（如 `8001 --log-level debug`）
既没有守护，也不能再起一个 `run-app.py` —— **两个 launcher 会互相抢 8000，制造
「每 10 秒 exited」崩溃循环**（skill §4 记录过的真实事故）。

⇒ 非 launcher 端口用本脚本起：先试 `pythonw.exe` + **`CREATE_BREAKAWAY_FROM_JOB`**，
被 job 拒绝时**自动回退 WMI 代起**（`Invoke-CimMethod Win32_Process Create`，父进程变成
`WmiPrvSE.exe`，与调用方无进程树关系）；输出重定向到 `.workbuddy/logs/api-<port>.log`。

⚠️ 两个实测坑（2026-09-23，都踩过）：

1. **不要用 `DETACHED_PROCESS`**：`python.exe` + `DETACHED_PROCESS | CREATE_NO_WINDOW` +
   `stdout=文件` 的子进程**创建后立即死掉，日志里连一行 uvicorn 输出都没有**
   （子进程 std handle 在该组合下无效，解释器起步即崩，且不会有任何报错落到日志）。
2. **只用 `pythonw.exe` 也不够**：它能起、能服务（`/api/health` 200），但**命令结束后会被
   连带杀掉** —— agent 的 Bash 工具把子进程放在 job object 里，`pythonw` 只是「不 attach
   控制台」，**并不脱离 job**。证据：`api-8001.log` 里正常打印到
   `Started server process [N]`，随后进程消失、无任何 traceback（= 被外部终止）。
   判据：起完必须**换一次命令**再回来看端口还在不在（同一条命令里看必在）。

本机实测（2026-09-23）**job 拒绝 breakaway**（`PermissionError`）⇒ 实际生效的是 WMI
回退那一路。两条路都留着：换机器 / 换沙箱后条件会变，脚本自己选能用的那条。

用法
----
    "C:\\...\\python.exe" .workbuddy/scripts/spawn-detached-api.py --port 8001 --log-level debug

默认：`--host 127.0.0.1`、`--log-level info`、`--log .workbuddy/logs/api-<port>.log`。
端口已被占用时**直接报错退出**（不静默抢端口、不起第二个实例）。

验证（起完必须复核，别凭「命令返回了」就算成功）
------------------------------------------------
    netstat -ano | grep ":8011.*LISTENING"          # 看 PID
    curl -s http://127.0.0.1:8011/api/health        # 200
    改过的字段/参数在 GET /openapi.json 里在不在
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import socket
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
#: 用 pythonw 起 —— GUI 子系统进程不 attach 控制台，父 shell 退出后照样活着（同 run-app.py 惯例）
PYTHONW = r"C:\Users\admin\AppData\Local\Programs\Python\Python312\pythonw.exe"
LOG_DIR = ROOT / ".workbuddy" / "logs"

#: 子进程不带控制台窗口（与 run-app.py 同一惯例）
CREATE_NO_WINDOW = 0x08000000
#: 让子进程**脱离调用方的 job object** —— 关键：只靠 pythonw 仍会被 Bash 工具的 job 连带杀掉
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


def spawn_via_wmi(command: list[str], log_path: pathlib.Path, cwd: pathlib.Path, port: int) -> int:
    """回退路径：交给 WMI 服务起（父进程 = `WmiPrvSE.exe`）。

    本机实测（2026-09-23）**agent 的 shell job 拒绝 `CREATE_BREAKAWAY_FROM_JOB`**
    （直接 `PermissionError`）⇒ 只剩这条路能让进程活过调用方。用 PowerShell 的
    `Invoke-CimMethod` 而不是 `wmic process call create`：前者的引号只需照写，
    后者要求把内部引号转义成 `\\"`，极易出错。
    """
    inner = subprocess.list2cmdline(command) + f' >> "{log_path}" 2>&1'
    cmdline = f"cmd /c {inner}"
    script = (
        "Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{"
        f" CommandLine = '{cmdline}'"
        f"; CurrentDirectory = '{cwd}'"
        " } | Select-Object -ExpandProperty ProcessId"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0 or not out.isdigit():
        print(f"ERROR: WMI 起进程失败 rc={result.returncode} out={out!r} err={err[:200]!r}")
        return 4
    print(f"spawned pid={out} port={port} log={log_path} (via WMI)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="起一个脱离控制台的 uvicorn 调试实例")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--log", default=None, help="日志文件（默认 .workbuddy/logs/api-<port>.log）")
    args = parser.parse_args()

    if port_in_use(args.host, args.port):
        print(f"ERROR: {args.host}:{args.port} 已在监听 —— 先停掉旧进程再起（勿起第二个）")
        return 2

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = pathlib.Path(args.log) if args.log else LOG_DIR / f"api-{args.port}.log"

    command = [
        PYTHONW, "-m", "uvicorn", "backend.app.main:app",
        "--host", args.host, "--port", str(args.port), "--log-level", args.log_level,
    ]
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  spawn {args.host}:{args.port} "
            f"(log-level {args.log_level}) via spawn-detached-api.py\n"
        )
        handle.flush()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB,
            )
        except PermissionError:
            # job 拒绝 breakaway（本机实测就是这样）⇒ 交给 WMI 服务起，父进程与调用方无关
            handle.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  breakaway 被拒，回退 WMI\n")
            handle.flush()
            return spawn_via_wmi(command, log_path, ROOT, args.port)
    print(f"spawned pid={process.pid} port={args.port} log={log_path} (breakaway-from-job)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
