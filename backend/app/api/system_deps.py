"""初始化环境依赖的 HTTP 端点（设计 24 §27，用户口径 2026-10-23）。

四个端点：

* `GET  /api/system/deps`            —— 全部依赖的当前状态（**只探测，不下载**）
* `POST /api/system/deps/download`   —— 开始下载一个依赖（**异步**，立即返回）
* `POST /api/system/deps/reset`      —— **清空本地重新下载**（删目录 + 状态回 missing）
* `GET  /api/system/deps/progress`   —— 轮询下载进度（前端弹窗用它刷进度条）

⚠️ 下载**必须异步**：ffmpeg 5.7 MB + 模型 15 MB 在慢网下几十秒，
挂成同步请求会让前端弹窗「一直转圈但没有任何进度」—— 用户要的正是**看进度**。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.app.core.deps import get_current_user
from backend.app.models import User
from backend.app.core import env_bootstrap as env

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system", tags=["system"])

#: 下载进度表：`key -> {status, percent, error, started_at}`。
#: ⚠️ 进程内内存即可 —— 下载是**一次性**动作，重启后按「本地文件可用与否」
#: 重新判定（`probe`），不需要持久化一个瞬时状态。
_PROGRESS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


class DownloadRequest(BaseModel):
    key: str


class ResetRequest(BaseModel):
    key: str


@router.get("/deps")
def list_dependencies(_: User = Depends(get_current_user)) -> dict:
    """全部依赖状态（探测本地是否可用；**不触发下载**）。"""
    return env.all_status()


@router.get("/deps/progress")
def dependency_progress(_: User = Depends(get_current_user)) -> dict:
    """下载进度快照（前端轮询）。

    ⚠️ 返回**每个 key 的进度 + 当前可用状态**：下载刚完成但 `probe` 还没跑完的
    一瞬间，前端以 `progress.status` 为准，不要凭 `ready` 猜。
    """
    with _LOCK:
        snapshot = {k: dict(v) for k, v in _PROGRESS.items()}
    status = env.all_status()
    for item in status["items"]:
        state = snapshot.get(item["key"])
        #: 本地已可用 ⇒ 进度归位为 done（覆盖「上次失败但文件其实下好了」的情形）
        if item["ready"] and (state is None or state.get("status") != "running"):
            snapshot[item["key"]] = {
                "key": item["key"],
                "status": "done",
                "percent": 100,
                "error": "",
            }
        elif state is None:
            snapshot[item["key"]] = {
                "key": item["key"],
                "status": "idle",
                "percent": 0,
                "error": "",
            }
    return {"progress": snapshot, "deps": status}


@router.post("/deps/download")
def start_download(
    payload: DownloadRequest, _: User = Depends(get_current_user)
) -> dict:
    """开始下载（**异步**，立即返回；进度走 `/deps/progress`）。"""
    spec = env.spec_by_key(payload.key)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"未知依赖：{payload.key}")

    with _LOCK:
        current = _PROGRESS.get(payload.key)
        if current and current.get("status") == "running":
            #: 已在下载 ⇒ 幂等返回，不重复起线程（重复下载会互相覆盖文件）
            return {"ok": True, "already_running": True, "key": payload.key}
        _PROGRESS[payload.key] = {
            "key": payload.key,
            "status": "running",
            "percent": 5,
            "error": "",
        }

    target = env.deps_root() / spec.dir_name

    def _run() -> None:
        try:
            with _LOCK:
                _PROGRESS[payload.key] = {
                    "key": payload.key,
                    "status": "running",
                    "percent": 20,
                    "error": "",
                }
            #: **真实下载进度上报**（2026-10-23，用户口径「下载进度需要反馈给用户」）。
            #: 原来这里是硬编码的 5 / 20 / 95 / 100 假进度，220 MB 的内核下载期间
            #: 界面只会「卡在 20%」然后突然 100%，用户以为死机。
            #: 现在 `_download_first_ok` 每读一块就回调，这里换算成百分比写进 _PROGRESS。
            def _sink(label: str, done: int, total: int) -> None:
                if total > 0:
                    #: 留 5%~95% 给下载本身（0-5% 是启动、95-100% 是解包+探测）
                    percent = 5 + int(done * 90 / total)
                    note = f"{label}：{done / 1048576:.1f} / {total / 1048576:.1f} MB"
                else:
                    percent = 20
                    note = label
                with _LOCK:
                    _PROGRESS[payload.key] = {
                        "key": payload.key,
                        "status": "running",
                        "percent": min(percent, 95),
                        "error": "",
                        "note": note,
                    }

            env.set_progress_sink(_sink)
            try:
                #: 失败**自动重试 3 次**（用户口径 2026-10-23）；仍失败才抛给前端。
                env._with_retries(spec.fetch, target, spec.name)
            finally:
                env.set_progress_sink(None)
            with _LOCK:
                _PROGRESS[payload.key] = {
                    "key": payload.key,
                    "status": "running",
                    "percent": 95,
                    "error": "",
                }
            #: 下完必须**真探测一次**：下载成功 ≠ 可用（半截/损坏都要判出来）
            if not spec.probe(target):
                raise RuntimeError("下载完成但本地探测不可用（文件可能损坏）")
            #: ⚠️ **下完必须立刻把搜索路径刷新出去**（2026-10-23 实机报障）：
            #: 裸 `subprocess.run(['node', ...])` 按 PATH 解析，而 PATH 只在进程启动
            #: 时前插过一次 ⇒ 先启动后下载的 node 整个进程生命周期内都「不存在」
            #: ⇒ 绑定账号报「JS mnsv2 签名失败或未达到硬门禁（mns0201_）」，重开才好。
            refreshed = env.refresh_runtime_paths() if payload.key == "node" else None
            if refreshed and refreshed.get("changed"):
                logger.info("node 依赖就绪，已前插搜索路径：%s", refreshed.get("node"))
            with _LOCK:
                _PROGRESS[payload.key] = {
                    "key": payload.key,
                    "status": "done",
                    "percent": 100,
                    "error": "",
                }
        except Exception as exc:  # noqa: BLE001 - 失败原因要原样回给用户
            logger.warning("依赖下载失败 key=%s: %s", payload.key, exc)
            with _LOCK:
                _PROGRESS[payload.key] = {
                    "key": payload.key,
                    "status": "failed",
                    "percent": 0,
                    "error": str(exc),
                }

    threading.Thread(target=_run, name=f"dep-download-{payload.key}", daemon=True).start()
    return {"ok": True, "key": payload.key}


@router.post("/deps/reset")
def reset_dependency(
    payload: ResetRequest, _: User = Depends(get_current_user)
) -> dict:
    """**清空本地重新下载**（用户口径：下载失败可点它清空后重下）。"""
    try:
        result = env.reset_dependency(payload.key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    with _LOCK:
        _PROGRESS.pop(payload.key, None)
    return {"ok": True, **result}
