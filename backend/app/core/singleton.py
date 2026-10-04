"""调度器单实例闸（T-108 / DEF-003，2026-10-04）。

## 为什么需要它

`BackgroundScheduler` 与 FastAPI **同进程**（`main.py` 的 `lifespan`），
`max_instances=1` 只约束**单进程内**。水平扩容、或本地误开第二个后端时，
每个进程各起一套定时器 ⇒ 同一批 `system.due_publish` 被跑两遍 ⇒ **重复发文**。
（重复发文有三个入口：重复创建 DEF-002 / 重复执行 DEF-031 / **多副本重复调度 DEF-003**。）

## 为什么不用 DB 锁

债文档给了两条路：① DB 分布式锁；② 明确文档化「仅支持单副本」+ 启动检测多实例。
本实现选 **②的加强版**：

- **不引 DB 锁**：`scheduler` 的 jobstore 已经在写同一个 SQLite（`_persistent_jobstores`），
  再叠一把 DB 锁会让「锁等待」与「database is locked」互相纠缠 ——
  DEF-067 那次停摆的根因正是 jobstore 写锁超时。**同一个写点不叠第二把锁**。
- **用文件租约 + PID 存活探针**：原子创建（`O_CREAT|O_EXCL`）一个租约文件，
  里面写 PID；启动时若租约存在且**该 PID 仍活着**，说明已有实例在跑 ⇒ 本进程
  不再启动调度器而是**如实告知**（不是静默跳过 —— 静默跳过正是 DEF-048 那类
  「看着在跑其实没跑」）。
- **租约自愈**：PID 已死的陈旧租约（进程被 kill -9 / 断电）会被**接管**，
  不需要人工删文件。

## 边界（如实说明）

- 只覆盖**同机**多实例（桌面版就是同机场景）。跨机多副本仍需 DB 锁或部署层单副本 ——
  本项目是**桌面单机应用**（`data/spider_xhs.db` 本地文件），跨机不在范围内。
- 租约文件不是权威真相源，**是提示**：拿不到租约只是不启动调度器，
  其余 API / worker 照常（那些链路各有自己的 DB 级幂等，见 T-103）。
"""
from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: 租约文件名（放项目根的 `data/` 下，与运行库同目录 —— 部署时同进同出）。
LEASE_FILENAME = "scheduler_instance.lock"

_lease_path_holder: dict[str, Optional[Path]] = {"path": None}


def _pid_alive(pid: int) -> bool:
    """该 PID 是否仍活着（**只判存活，不判身份** —— 身份靠租约文件本身）。"""
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore

        return psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001 - psutil 缺失 / 平台差异都走兜底
        pass
    if os.name == "nt":  # pragma: no cover - Windows 分支
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:  # pragma: no cover - POSIX 分支
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_lease_pid(path: Path) -> Optional[int]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw)
    except Exception:  # noqa: BLE001 - 文件损坏 / 空 / 非数字都视为「无有效租约」
        return None


def acquire_scheduler_lease(data_dir: Path) -> tuple[bool, str]:
    """尝试取得调度器租约。

    :returns: `(acquired, reason)` —— `acquired=False` 时 `reason` 是**人类可读**的
        原因（调用方应把它记进日志 / 启动横幅，而不是吞掉）。

    取得后租约在**本进程存活期间一直持有**；`atexit` 与显式 `release()` 都会删它。
    """
    path = Path(data_dir) / LEASE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    my_pid = os.getpid()

    for attempt in (0, 1):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = _read_lease_pid(path)
            if holder is not None and holder != my_pid and _pid_alive(holder):
                return False, (
                    f"另一个后端实例（pid {holder}）已持有调度器租约 {path} ⇒ "
                    f"本进程**不启动调度器**（避免重复调度 / 重复发文）。"
                    f"若确认那个进程已不在，请删除该文件后重启。"
                )
            # 陈旧租约（持有者已死 / 文件损坏）⇒ 删掉重试一次（自适应，不需人工介入）
            if attempt == 0:
                try:
                    if holder is not None and holder == my_pid:
                        # 同一个 PID 复用（进程重启后 PID 撞车）：直接接管
                        return _claim(path, my_pid)
                    path.unlink(missing_ok=True)
                except OSError:
                    return False, f"陈旧租约 {path} 无法删除（权限？）⇒ 跳过启动调度器。"
                continue
            return False, f"租约文件 {path} 竞争失败（并发启动？）⇒ 跳过启动调度器。"
        else:
            os.close(fd)
            return _claim(path, my_pid)
    return False, "租约获取未收敛（不应到此）"


def _claim(path: Path, pid: int) -> tuple[bool, str]:
    try:
        path.write_text(str(pid), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 磁盘只读等
        return False, f"租约写入失败：{exc}"
    _lease_path_holder["path"] = path
    atexit.register(release_scheduler_lease)
    return True, f"已取得调度器租约（pid {pid} → {path}）"


def release_scheduler_lease() -> None:
    """释放租约（幂等；只删**自己**持有的那份）。"""
    path = _lease_path_holder.get("path")
    if path is None:
        return
    try:
        if _read_lease_pid(path) == os.getpid():
            path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - 退出路径不抛
        pass
    finally:
        _lease_path_holder["path"] = None
