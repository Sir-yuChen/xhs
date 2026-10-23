"""启动自检（DEF-028 / DEF-027 / T-20，2026-10-04）。

## 为什么

签名运行时是 **Node.js 子进程**（算法唯一实现源在 `xhs_utils/**/js/`）—— Node
不在 PATH 时**全系统的平台调用都会失败**，而失败形态是「每个请求各报一次
看不懂的错」，排查要从业务一路剥到运行环境。启动时**一次性说清**，代价最低。

## 口径

- **缺失只 WARN 不阻断**：只读浏览 / 看历史数据不需要签名，把应用直接拒绝启动
  会让「我只是想看看数据」的场景也用不了。**醒目地告警 + 落到启动横幅**即可。
- **实测而非查 PATH**：`node --version` 真跑一次（PATH 里有不代表能执行——
  权限、损坏的安装、版本管理器 shim 失效都会让它在 PATH 里但跑不起来）。
- **带超时**：Node 装坏了可能在启动时挂住，不能让它把整个 `lifespan` 拖死。
"""
from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

#: 自检超时（秒）—— 签名运行时不参与启动，只是要它「能跑」。
NODE_PROBE_TIMEOUT_SECONDS = 5.0


def check_node_runtime() -> tuple[bool, str]:
    """Node 可用性自检，返回 `(ok, 人类可读结论)`。

    ⚠️ 结论串**两种情况下都要能读**：可用时给版本（排查「是不是版本太老」要看它），
    不可用时给**怎么办**（装 Node / 修 PATH），不是只丢一个 `False`。

    ⚠️ 查的是**统一出入口**（`deps/node` → 包内 → PATH），不是裸 `shutil.which`：
    1.0.6 起 node.exe 外置为「首次启动下载」，只查 PATH 会把「已经下载好但 PATH
    没刷新」误报成「未安装 Node.js」（2026-10-23 实机报障的排查噪声之一）。
    """
    exe = None
    try:
        from xhs_utils.xhs_core.runtime import node_executable_or_none

        exe = node_executable_or_none()
    except Exception:  # noqa: BLE001 - 依赖服务不可用则退回 PATH
        exe = shutil.which("node")
    if not exe:
        return False, (
            "未找到 Node.js（deps/node、包内 runtime/node 与 PATH 都没有 `node`）"
            "⇒ 平台签名无法生成，**所有小红书平台调用都会失败**"
            "（浏览本地已同步数据不受影响）。"
            "请在「检查运行依赖」里下载 Node 运行时（或自行安装 Node.js），无需重启。"
        )
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=NODE_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"`node` 在 {NODE_PROBE_TIMEOUT_SECONDS:.0f} 秒内没有响应（{exe}）⇒ "
            "安装可能已损坏或被安全软件拦截。请重装 Node.js 后重启。"
        )
    except OSError as exc:
        return False, f"`node` 存在但无法执行（{exe}）：{exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:200]
        return False, f"`node --version` 退出码 {proc.returncode}：{detail}"
    version = (proc.stdout or "").strip()
    return True, f"Node.js 可用（{version}，{exe}）"


def log_startup_selfcheck() -> dict[str, object]:
    """跑全部启动自检并把结论写进日志（`lifespan` 调用）。

    :returns: `{"node_ok": bool, "node_detail": str, ...}` —— 也放到 `app.state`
        上，方便健康检查 / 排查时读，不必翻日志。
    """
    node_ok, node_detail = check_node_runtime()
    if node_ok:
        logger.info("启动自检 · %s", node_detail)
    else:
        #: ⚠️ 先试一次「用户已下载的 node」再报警（2026-10-23 实机报障）：
        #: launcher 只在**启动那一刻**前插过 PATH，而用户可能上次是「先启动、
        #: 后下载 node」⇒ 这次启动 deps/node 已经在了，但不前插就永远找不到。
        try:
            from backend.app.services.env_bootstrap_service import refresh_runtime_paths

            refreshed = refresh_runtime_paths()
        except Exception:  # noqa: BLE001 - 依赖服务不可用不该让自检崩
            refreshed = {}
        if refreshed.get("node"):
            node_ok, node_detail = check_node_runtime()
            if node_ok:
                logger.info("启动自检 · %s（已前插 %s）", node_detail, refreshed["node"])
                return {"node_ok": node_ok, "node_detail": node_detail}
        # ⚠️ 用 warning 而不是 error：不阻断启动（见模块 docstring 的口径）。
        logger.warning("启动自检 · %s", node_detail)
    return {"node_ok": node_ok, "node_detail": node_detail}
