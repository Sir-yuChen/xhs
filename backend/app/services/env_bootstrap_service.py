"""**初始化环境依赖下载**（设计 24 §27）—— 用户口径 2026-10-23。

背景
----
1.0.5 安装包实测踩到：**装包版进程不继承开发者的 PATH** ⇒ 视频抽帧的 `ffmpeg`
找不到 ⇒ `ocr_ok=None` ⇒ 视频被 `passedMedia` 过滤掉 ⇒ 用户勾了视频也传不上去，
提示还是「数据包中没有视频」。根因不是逻辑写错，是**依赖没随包**。

用户口径：「新增一个初始化环境的弹窗，展示各个依赖下载的进度，如果下载失败支持
点击清空本地重新下载」+「所有三方的依赖全部统一在用户安装时进行下载」。

设计取舍
--------
* **只延后「资源类」依赖**（可下载的二进制 / 模型文件）。`cv2` / `onnxruntime` /
  `numpy` 是编译进 exe 的二进制扩展，**不能**改成下载（那要把打包形态从单文件
  exe 改成让用户装 Python 环境，属架构改动）—— 这些**不在此清单内**，见
  `BUNDLED_ONLY_NOTE`。
* 每个依赖一个 `DependencySpec`：`key`（稳定标识，前端用来跟踪）、`name`
  （中文名）、`size_hint`（前端显示预计体积）、`dest`（落盘位置）、
  `probe`（**判据：本地是否已可用**）、`fetch`（下载实现）。
* **判据优先于文件存在**：`probe()` 返回可用才算 ready。例如 ffmpeg 要求
  `ffmpeg.exe` **能跑起来**（`-version` 退出码 0），光有文件不够
  （下载中断留下的半截文件必须被判为「不可用」，否则用户重下按钮永远不出现）。
* **清空重下**：`reset()` 删掉该依赖的落盘目录，状态回到 `missing`。
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

#: ~~打包内嵌、不可改成下载的依赖说明~~ —— **已清空**（用户口径 2026-10-23：
#: 「OpenCV / onnxruntime / numpy 属于编译进程序的运行库…文本移除」）。
#: ⚠️ 常量保留为空串（不是删字段）：前端 `bundled_note` 是响应契约的一部分，
#: 删字段会让已发布的旧前端读不到键；给空串则前端自然不渲染那一行。
BUNDLED_ONLY_NOTE = ""

#: 下载超时（秒）。5.7 MB 的 ffmpeg 在 1Mbps 下约 46s，给足余量。
DOWNLOAD_TIMEOUT_S = 120

#: ⚠️⚠️ **下载源必须国内可达**（2026-10-23 实机实测，不做假设）：
#:
#:   | 源                              | 实测结果                        |
#:   |---------------------------------|---------------------------------|
#:   | `github.com` / `api.github.com`  | **超时 22s，下载 0 字节**（被墙）|
#:   | `huggingface.co` / `hf-mirror.com`| **连不上**                      |
#:   | `cdn.npmmirror.com` (npmmirror)  | ✅ 200，ffmpeg 27.6 MB / 0.9s   |
#:   | `modelscope.cn` (阿里魔搭)        | ✅ 200，模型 4.5 MB / 0.4s      |
#:
#: ⇒ **只依赖这两个公共开源镜像，不依赖自建存储**。用户口径 2026-10-23：
#:   「你调研一下市场没有开源公共的地址吗，使用开源公共的，准备两套」。
#:
#: **两套源（主 + 备）**：每个资源两个 URL，主源失败自动切备源（见 `_download_first_ok`）。
#: 这样任一镜像临时故障都不会让用户卡在「下载失败」。

#: ffmpeg 的两套公共镜像（都是 npmmirror，不同 CDN 节点/路径形态）。
#:
#: ⚠️ 必须**两个都试**：`registry.npmmirror.com/-/binary/...` 返回 302 跳转，
#: `cdn.npmmirror.com/binaries/...` 直出 200 —— 跳转链路在某些网络下会断。
FFMPEG_URLS = (
    "https://cdn.npmmirror.com/binaries/ffmpeg-static/b6.0/ffmpeg-win32-x64.gz",
    "https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.0/ffmpeg-win32-x64.gz",
)

#: node 的两套公共镜像（npmmirror 主，华为云备）。
#:
#: ⚠️ **必须按本机架构选包**（2026-10-23 实机报障）：原来只有一个 x64 包，
#: 32 位 Windows 上跑 x64 的 node.exe 会直接失败 ⇒ 签名全挂。x64 / x86 两套都有，
#: 由下层镜像提供（两条 URL 每个架构各一套，取到不可用时继续试下一个）。
_NODE_VERSION = "v22.11.0"

#: 本机 node 安装包的架构（x64 / x86）—— 见 `_node_arch()`。
NODE_URLS_BY_ARCH: dict[str, tuple[str, ...]] = {
    "x64": (
        f"https://cdn.npmmirror.com/binaries/node/{_NODE_VERSION}/node-{_NODE_VERSION}-win-x64.zip",
        f"https://mirrors.huaweicloud.com/nodejs/{_NODE_VERSION}/node-{_NODE_VERSION}-win-x64.zip",
    ),
    "x86": (
        f"https://cdn.npmmirror.com/binaries/node/{_NODE_VERSION}/node-{_NODE_VERSION}-win-x86.zip",
        f"https://mirrors.huaweicloud.com/nodejs/{_NODE_VERSION}/node-{_NODE_VERSION}-win-x86.zip",
    ),
}

#: 兼容旧调用点 / 测试（= x64 那套；取用请走 `node_urls()`）
NODE_URLS = NODE_URLS_BY_ARCH["x64"]

#: OCR 模型的**两套公共源**（ModelScope 主，npmmirror 备）。
#:
#: 每个模型是 `(文件名, [url1, url2, ...])`。实测 ModelScope 上
#: `PP-OCRv6_det_tiny.onnx` 下载得 **1,829,618 字节**，与本机 `~/.cache/xhs/ocr_models`
#: 里的文件**逐字节一致**（rec/cls 同理）⇒ 镜像内容可信。
#: ModelScope 的**两种下载路径形态**（实测两者都返回同一字节内容）：
#:   ① `modelscope.cn/models/<repo>/resolve/master/<path>`  —— 网页「直接下载」用的
#:   ② `modelscope.cn/api/v1/models/<repo>/repo?Revision=master&FilePath=<path>` —— API 形态
#: 主源用 ①、备源用 ②：同站不同链路，其中一条被限流/挡掉时另一条通常还能走。
MODELSCOPE_RESOLVE = (
    "https://modelscope.cn/models/RapidAI/RapidOCR/resolve/master/onnx"
)
MODELSCOPE_API = (
    "https://modelscope.cn/api/v1/models/RapidAI/RapidOCR/repo"
    "?Revision=master&FilePath=onnx"
)

OCR_MODEL_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "PP-OCRv6_det_tiny.onnx",
        (
            f"{MODELSCOPE_RESOLVE}/PP-OCRv6/det/PP-OCRv6_det_tiny.onnx",
            f"{MODELSCOPE_API}/PP-OCRv6/det/PP-OCRv6_det_tiny.onnx",
        ),
    ),
    (
        "PP-OCRv6_rec_tiny.onnx",
        (
            f"{MODELSCOPE_RESOLVE}/PP-OCRv6/rec/PP-OCRv6_rec_tiny.onnx",
            f"{MODELSCOPE_API}/PP-OCRv6/rec/PP-OCRv6_rec_tiny.onnx",
        ),
    ),
    (
        "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
        (
            f"{MODELSCOPE_RESOLVE}/PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx",
            f"{MODELSCOPE_API}/PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx",
        ),
    ),
)



#: **下载失败自动重试次数**（用户口径 2026-10-23：「下载失败自动重试 3 次，
#: 依旧失败，支持用户触发重复下载」）。
#:
#: ⚠️ 重试放在**下载实现内部**（`_with_retries` 包一层），不放前端 ——
#: 前端重试要先拿回失败、再发一次请求，中间还隔着轮询间隔，会把 3 次试完拖到
#: 分钟级；而且「重试」属于下载语义，不该由 UI 承担。
DOWNLOAD_RETRIES = 3

#: 流式下载的读取块大小（用于上报进度；64 KB 是速度与回调频率的折中）。
_PROGRESS_CHUNK_BYTES = 64 * 1024

#: 当前下载的**进度上报钩子**（2026-10-23，用户口径「下载进度需要反馈给用户」）。
#:
#: 为什么用模块级变量而不是给每个 `fetch()` 加参数：`DependencySpec.fetch` 的签名是
#: 统一的 `Callable[[Path], None]`，四个依赖（node / ffmpeg / ocr / 窗口内核）各写一遍
#: 进度参数会立刻长出四份重复实现。这里由下载入口在开始时挂上、结束时摘掉，
#: `_download_first_ok` 每读一块就调一次 —— **只有一处实现**。
_progress_sink: "Callable[[str, int, int], None] | None" = None


def set_progress_sink(sink: "Callable[[str, int, int], None] | None") -> None:
    """挂/摘下载进度钩子。`sink(label, 已下载字节, 总字节)`；总字节未知时为 0。"""
    global _progress_sink

    _progress_sink = sink


def _emit_progress(label: str, done: int, total: int) -> None:
    sink = _progress_sink
    if sink is None:
        return
    try:
        sink(label, done, total)
    except Exception:  # noqa: BLE001 - 进度上报永不影响下载
        pass

#: 每次重试前的等待基数（秒）；第 n 次等 `RETRY_BACKOFF_S * n`（2s / 4s）。
RETRY_BACKOFF_S = 2


def install_root() -> Path:
    """**用户选择的安装目录**（三处共用的唯一判据）。

    * 装包态：`sys._MEIPASS` 的上级 —— PyInstaller **onedir** 下是 exe 所在目录；
      onefile 下 `_MEIPASS` 是解压临时目录，其上级**不是**安装目录，
      所以 onefile 时改用 `sys.executable` 的父目录。
    * 开发态：仓库根。

    ⚠️ 为什么统一到这里（2026-10-23 用户口径「下载的依赖统一也要在用户选择的安装
    目录下」）：原来 `packaging/launcher.py::app_root()` 与
    `env_bootstrap_service.deps_root()` **各自判断一次安装目录** —— 前者用
    `_MEIPASS` 的上级、后者用 `sys.executable` 的父目录。onedir 下两者恰好相同
    （所以看起来「是对的」），但这是**巧合不是约定**：一旦打包形态变成 onefile，
    依赖就会落到解压临时目录里，下次启动全丢，而 `data/` 却仍在安装目录。
    现在两处都问 `install_root()`，没有第二份判据。
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        exe_dir = Path(sys.executable).resolve().parent
        if meipass:
            candidate = Path(meipass).resolve().parent
            #: onedir：`<安装目录>\_internal` 的上级 = 安装目录。判据用「该目录下
            #: 确有与 sys.executable 同名的 exe」——不能只比目录相等（onefile 时
            #: `_MEIPASS` 在 %TEMP%\_MEIxxxx，其上级是 %TEMP%，恰好**不等于** exe 目录，
            #: 但写成等值比较会让「符号链接/短路径」情形误判，用文件存在更稳）。
            exe_name = Path(sys.executable).name
            if (candidate / exe_name).is_file():
                return candidate
        return exe_dir
    return Path(__file__).resolve().parents[3]


def deps_root() -> Path:
    """**依赖下载根目录**：`<安装目录>/deps/`（开发态 `仓库/.hermes/deps/`）。

    * 必须落在**用户选择的安装目录**下（用户口径 2026-10-23）：用户可以看见、可以删、
      卸载时不随之消失（与 `data/`、`logs/` 同级）。
    * **不能**放 `_internal`：升级安装会把整个 `_internal` 覆盖，下载的东西会丢。
    * 与 launcher 的 `app_root()` **同源**（都走 `install_root()`），不再是两份判断。
    """
    if getattr(sys, "frozen", False):
        return install_root() / "deps"
    return install_root() / ".hermes" / "deps"


# ---------------------------------------------------------------------------
# **统一出入口**（2026-10-23 用户口径：「全部统一出入口」）
# ---------------------------------------------------------------------------
# 背景：1.0.6 把 node / ffmpeg / OCR 模型外置成「首次启动下载」之后，
# **四份各自独立的定位逻辑**散落在四个文件里：
#
#   | 依赖 | 原来的定位实现 | 问题 |
#   |---|---|---|
#   | node | `apis/xhs_qianfan_apis._node_exe` + `xhs_utils/xhs_*` 裸 `'node'` | 两份；下载完不实时生效 |
#   | ffmpeg | `image_ocr_service._ffmpeg_exe`（自带候选目录表） | 又一份 |
#   | OCR 模型 | `rapid_ocr_service._model_dir()`（`~/.cache/xhs/ocr_models`） | 与 deps 下载目录**不是同一个地方**：下载器下到 `deps/ocr_models`，引擎却去 `~/.cache` 找 |
#   | deps 根 | 各处 `from ... import deps_root` | 尚可，但依赖每个调用方自觉 |
#
# 后果有两类，都是「本地环境不同则行为不同」：
#   ① 下载成功但引擎找不到（OCR 模型那格是**确定性的错**：两处路径不同）；
#   ② 下载成功后**必须重开软件**才生效（node 的 PATH 时效问题）。
#
# 现在只留一个出入口：**所有外部依赖都问 `dep_binary()` / `dep_model_dir()`**，
# 它们内部只认 `deps/<key>/`（+ 冻结态的包内兼容目录 + PATH 兜底），**每次调用
# 都重新判定**（`os.path.isfile` 一次，代价可忽略）⇒ 下载完立刻生效。

#: 冻结态下「包内兼容目录」的映射（老形态：曾经把 ffmpeg 打进 `<exe>/bin`）。
_BUNDLED_DIR_COMPAT: dict[str, tuple[str, ...]] = {
    "node": ("runtime/node",),
    "ffmpeg": ("bin",),
}


def dep_dir(key: str) -> Path:
    """某个依赖的**落盘目录**（唯一口径：`deps/<key>/`）。"""
    spec = spec_by_key(key)
    return deps_root() / (spec.dir_name if spec else key)


def dep_binary(key: str, name: str | None = None) -> str | None:
    """**统一的可执行文件出入口**：`node` / `ffmpeg` / `ffprobe`。

    查找顺序：`deps/<key>/<name>` → 冻结态包内兼容目录 → `PATH`。

    * 传 `name` 用于「同目录两个 exe」的情形（`ffmpeg` / `ffprobe` 都在 `deps/ffmpeg/`）；
      默认取该依赖的主可执行名。
    * **每次都重新判定** ⇒ 用户在应用里下完、或点「清空重下」之后，**无需重启**。
    * 找不到返回 `None`（调用方各自决定是报错还是降级 —— 但**不许**自己再拼一套路径）。
    """
    import shutil

    spec = spec_by_key(key)
    if spec is None:
        return None
    #: 主可执行名：Windows 带 `.exe`，其余平台不带（`node` / `ffmpeg`）。
    default_name = f"{key}.exe" if os.name == "nt" else key
    exe_name = name or default_name
    if os.name != "nt" and exe_name.endswith(".exe"):
        exe_name = exe_name[: -len(".exe")]
    candidates: list[Path] = [dep_dir(key) / exe_name]
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        for rel in _BUNDLED_DIR_COMPAT.get(key, ()):
            candidates.append(base / rel / exe_name)
            candidates.append(base.parent / rel / exe_name)
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    #: 最后兜底 PATH（开发机 / 用户自己装了 Node.js 或 ffmpeg）。
    bare = exe_name[: -len(".exe")] if exe_name.endswith(".exe") else exe_name
    return shutil.which(exe_name) or shutil.which(bare)


def dep_model_dir(key: str = "ocr_models") -> Path:
    """**统一的模型目录出入口**（OCR 模型）。

    ⚠️ 原来 `rapid_ocr_service._model_dir()` 用 `~/.cache/xhs/ocr_models`，而下载器把
    模型放在 `deps/ocr_models/` ⇒ **下载成功但引擎永远找不到**，表现为「下载完成可
    预检仍报模型缺失」。现在两边都只认这一个函数，路径没有第二份。
    """
    target = dep_dir(key)
    target.mkdir(parents=True, exist_ok=True)
    return target


@dataclass(frozen=True)
class DependencySpec:
    key: str
    name: str
    size_hint: str
    #: 落盘子目录名（`deps/<dir_name>/`）
    dir_name: str
    #: 本地是否**可用**（不是「文件在」——半截文件必须判不可用）
    probe: Callable[[Path], bool]
    #: 下载到 `target_dir`；失败抛异常（调用方负责把原因写进状态）
    fetch: Callable[[Path], None]
    detail: str = ""
    #: 失败后可点的「清空重下」文案（走 `reset_dependency`）
    resettable: bool = field(default=True)
    #: **必装**项（下不成功软件不可用）—— 前端弹窗据此决定能否跳过/关闭。
    required: bool = field(default=False)


# ---------------------------------------------------------------------------
# ffmpeg（视频抽帧的硬依赖，5.7 MB 静态构建）
# ---------------------------------------------------------------------------

#: 静态构建（eugeneware/ffmpeg-static b6.0）—— 只收 ffmpeg，不收 ffprobe。
#: ⚠️ `probe_duration` 用 ffprobe 读时长，但**抽帧本身只需要 ffmpeg**
#: （`extract_frames` 里 ffmpeg 直接 `-ss` 取帧）。所以下载源只提供一个也行，
#: `probe_duration` 在 ffprobe 缺失时回落「读不出来 ⇒ 按整段估算」。



def _download_first_ok(
    urls: tuple[str, ...],
    label: str,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> bytes:
    """**依次尝试多个公共源**，返回第一个成功且体积合理的响应体。

    用户口径 2026-10-23：「准备两套」—— 主源挂了自动切备源，用户不必手动换。
    「国内源优先」：各依赖的 URL 元组里国内镜像排在前面（npmmirror → 华为云）。

    * 判据：HTTP 成功 **且** 体积 ≥ 1KB（0 字节/半截都算失败，继续试下一个）；
    * 全部失败 ⇒ 抛 `RuntimeError`，列出每个源的实际错误（前端要能看出是网络问题）。

    `on_progress(已下载字节, 总字节)`（2026-10-23 新增，用户口径「下载进度需要
    反馈给用户」）：**流式**分块读取并回调 —— 原来用 `resp.read()` 一次性读进内存，
    期间没有任何中间状态可上报，220 MB 的内核下载在界面上是「卡住不动」。
    总字节取自 `Content-Length`，拿不到时回传 0（调用方据此显示已下载量而非百分比）。
    """
    errors: list[str] = []
    for url in urls:
        host = url.split("/")[2] if url.count("/") >= 2 else url
        try:
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                chunks: list[bytes] = []
                done = 0
                while True:
                    chunk = resp.read(_PROGRESS_CHUNK_BYTES)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    done += len(chunk)
                    if on_progress is not None:
                        on_progress(done, total)
                    _emit_progress(label, done, total)
                data = b"".join(chunks)
            if len(data) < 1024:
                errors.append(f"{host}: 内容过小 {len(data)}B")
                logger.warning("%s 源 %s 返回过小（%s 字节）", label, host, len(data))
                continue
            logger.info("%s 从 %s 下载成功（%s 字节）", label, host, len(data))
            return data
        except Exception as exc:  # noqa: BLE001 - 换下一个源
            errors.append(f"{host}: {exc}")
            logger.warning("%s 源 %s 失败：%s", label, host, exc)
    raise RuntimeError(f"{label} 所有下载源均失败 —— " + "；".join(errors))


def _exe_name() -> str:
    return "ffmpeg.exe" if os.name == "nt" else "ffmpeg"


#: ffmpeg 静态构建（eugeneware/ffmpeg-static b6.0）在 npmmirror 上的真实文件形态。
#: ⚠️ 2026-10-23 实测订正：文件名是**扁平**的 `ffmpeg-win32-x64.gz`（没有架构子目录，
#: `/win32-x64/ffmpeg.exe.gz` 是 404）。这里 x64 / ia32 各一套两镜像。
#: ⚠️ 顺带可取 `ffprobe-win32-x64.gz` —— 有了它 `probe_duration` 不必靠
#:   `ffmpeg -i` 解析 Duration 兜底（抽帧本身只需要 ffmpeg）。
_FFMPEG_BASE = "ffmpeg-static/b6.0"


def _ffmpeg_urls_for(arch_suffix: str, name: str = "ffmpeg") -> tuple[str, ...]:
    """`name` = `ffmpeg` / `ffprobe`；`arch_suffix` = `win32-x64` / `win32-ia32`。"""
    tail = f"{name}-{arch_suffix}.gz"
    return (
        f"https://cdn.npmmirror.com/binaries/{_FFMPEG_BASE}/{tail}",
        f"https://registry.npmmirror.com/-/binary/{_FFMPEG_BASE}/{tail}",
    )


FFMPEG_URLS_BY_ARCH: dict[str, tuple[str, ...]] = {
    "x64": _ffmpeg_urls_for("win32-x64"),
    "x86": _ffmpeg_urls_for("win32-ia32"),
}

#: ffprobe 的两套源（与 ffmpeg 同源位，仅文件名不同）
FFPROBE_URLS_BY_ARCH: dict[str, tuple[str, ...]] = {
    "x64": _ffmpeg_urls_for("win32-x64", "ffprobe"),
    "x86": _ffmpeg_urls_for("win32-ia32", "ffprobe"),
}


def ffmpeg_urls() -> tuple[str, ...]:
    """本机架构对应的 ffmpeg 下载源（两套镜像）。"""
    return FFMPEG_URLS_BY_ARCH.get(_node_arch(), FFMPEG_URLS_BY_ARCH["x64"])


def ffprobe_urls() -> tuple[str, ...]:
    """本机架构对应的 ffprobe 下载源（两套镜像）。"""
    return FFPROBE_URLS_BY_ARCH.get(_node_arch(), FFPROBE_URLS_BY_ARCH["x64"])


def probe_ffmpeg(target: Path) -> bool:
    """**能跑起来**才算可用（半截文件 / 无执行权限 / 架构不符都判不可用）。"""
    exe = target / _exe_name()
    if not exe.is_file() or exe.stat().st_size < 1_000_000:
        return False
    try:
        done = subprocess.run(
            [str(exe), "-version"],
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return done.returncode == 0
    except Exception:  # noqa: BLE001 - 探测失败即「不可用」，不抛
        return False


def fetch_ffmpeg(target: Path) -> None:
    """下载 `.gz` 静态构建 → 解压到 `target/ffmpeg.exe`。

    ⚠️ 先写 `.part` 再原子改名：下载中断不会留下「看起来正常」的半截 exe
    （否则 `probe` 之外的用户手点「重下」永远发现不了问题）。
    """
    target.mkdir(parents=True, exist_ok=True)
    part = target / "ffmpeg.exe.part"
    data = _download_first_ok(ffmpeg_urls(), "ffmpeg")
    #: 源是 gzip 压缩的单文件
    raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
    if len(raw) < 1_000_000:
        raise RuntimeError(f"解压后过小（{len(raw)} 字节）")
    part.write_bytes(raw)
    part.replace(target / _exe_name())
    #: ffprobe **尽力而为**：它只服务「读视频时长」，缺失时 `probe_duration` 会用
    #: `ffmpeg -i` 解析 Duration 兜底 ⇒ 下不到不该让整项判失败（抽帧只需要 ffmpeg）。
    probe_exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    probe_part = target / f"{probe_exe}.part"
    try:
        pdata = _download_first_ok(ffprobe_urls(), "ffprobe")
        praw = gzip.decompress(pdata) if pdata[:2] == b"\x1f\x8b" else pdata
        if len(praw) >= 1_000_000:
            probe_part.write_bytes(praw)
            probe_part.replace(target / probe_exe)
            logger.info("ffprobe 一并下载完成（%s 字节）", len(praw))
    except Exception as exc:  # noqa: BLE001 - 可选依赖，失败只记日志
        logger.info("ffprobe 未下到（%s）；时长解析走 ffmpeg -i 兜底", exc)
    finally:
        probe_part.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# OCR 模型（PP-OCRv6 tiny —— 决定预检速度与召回率）
# ---------------------------------------------------------------------------

#: OCR 模型文件名（**只读展示用**；实际下载走 `OCR_MODEL_SOURCES` 的多源表）。
OCR_MODEL_FILES = (
    "PP-OCRv6_det_tiny.onnx",
    "PP-OCRv6_rec_tiny.onnx",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
)


def probe_ocr_models(target: Path) -> bool:
    """三个 onnx 都在且体积合理才算可用。"""
    #: ⚠️ 阈值取「**实测量级**的 ~90%」，不是估的数（2026-10-23 踩过）：
    #: 初版按估算写了 3 MB / 5 MB，而 tiny 档实际只有 **1,829,618 / 4,489,813** 字节
    #: ⇒ 文件下对了却 `probe=False`，用户会看到「下载成功但不可用」的死循环。
    #: 实测（ModelScope 与 `~/.cache/xhs/ocr_models` 逐字节一致）：
    #:   det_tiny 1,829,618 / rec_tiny 4,489,813 / cls 585,532
    want = {
        "PP-OCRv6_det_tiny.onnx": 1_600_000,
        "PP-OCRv6_rec_tiny.onnx": 4_000_000,
        "ch_ppocr_mobile_v2.0_cls_mobile.onnx": 500_000,
    }
    for name, min_size in want.items():
        f = target / name
        if not f.is_file() or f.stat().st_size < min_size:
            return False
    return True


def fetch_ocr_models(target: Path) -> None:
    """逐个下载三个模型（任一失败即抛，已下好的保留 —— 重下只补缺的）。"""
    target.mkdir(parents=True, exist_ok=True)
    for name, urls in OCR_MODEL_SOURCES:
        part = target / f"{name}.part"
        try:
            part.write_bytes(_download_first_ok(urls, name))
            if part.stat().st_size < 100_000:
                raise RuntimeError(f"{name} 体积异常（{part.stat().st_size} 字节）")
            part.replace(target / name)
        finally:
            part.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# node（请求签名运行时，83 MB —— 体积最大的可外置项）
# ---------------------------------------------------------------------------

#: Node 官方 Windows x64 压缩包（zip）。装包版不需要用户装 Python，但**签名
#: 靠 node 跑 `sign.js`**，所以它属于「必须下成功才能用」的依赖（`required`）。



def _node_name() -> str:
    return "node.exe" if os.name == "nt" else "node"


def _node_arch() -> str:
    """本机该下哪种 node 包：`x64` / `x86`。

    判据用 `platform.machine()`（进程自身位数），**不是** `os.environ` 里的
    `PROCESSOR_ARCHITECTURE` —— 那个描述的是**系统**，32 位系统上装了 64 位 CPU
    也会报 x86（此时确实该下 x86，一致），但 64 位系统上跑 32 位 Python 时报
    AMD64 而我们下 x64 包也一样能跑。用 `platform.machine()` 更简单且与「我们
    这个进程能不能加载它」直接相关。
    """
    import platform

    machine = (platform.machine() or "").lower()
    if machine in ("arm64", "aarch64"):
        #: node 官方 Windows ARM64 包名不同；暂不特殊处理 —— 回落到 x64（Win11 ARM 可模拟）
        logger.info("检测到 ARM64，回落 x64 的 node 包（可模拟运行）")
        return "x64"
    if machine in ("x86", "i386", "i686"):
        return "x86"
    return "x64"


def node_urls() -> tuple[str, ...]:
    """本机架构对应的 node 下载源（两套镜像）。"""
    return NODE_URLS_BY_ARCH.get(_node_arch(), NODE_URLS_BY_ARCH["x64"])


def probe_node(target: Path) -> bool:
    """**能跑起来且报出版本**才算可用。"""
    exe = target / _node_name()
    if not exe.is_file() or exe.stat().st_size < 10_000_000:
        return False
    try:
        done = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return done.returncode == 0
    except Exception:  # noqa: BLE001 - 探测失败即不可用
        return False


def fetch_node(target: Path) -> None:
    """下载官方 zip → 解出 `node.exe` 到 `target/`（**按本机架构**选包）。

    ⚠️ 先写 `.part`、成功才改名 + `finally` 清残留（2026-10-23 补齐）：
    原实现把 zip 解出来的内容**直接写最终名**，且 `part.unlink` 不在 `finally` 里 ——
    下载失败 / 解压异常时会留下几十 MB 的 `node.zip.part` 与**半截 node.exe**，
    下次「清空重下」不删它、`probe` 又判它不可用 ⇒ 用户看到死循环。
    """
    import zipfile

    target.mkdir(parents=True, exist_ok=True)
    part = target / "node.zip.part"
    staged = target / (".staged_" + _node_name())
    try:
        part.write_bytes(_download_first_ok(node_urls(), "node"))
        with zipfile.ZipFile(part) as zf:
            member = next(
                (n for n in zf.namelist() if n.endswith("/node.exe")), None
            )
            if member is None:
                raise RuntimeError("压缩包里没有 node.exe")
            staged.write_bytes(zf.read(member))
        if staged.stat().st_size < 10_000_000:
            raise RuntimeError(f"解出的 node.exe 体积异常（{staged.stat().st_size} 字节）")
        staged.replace(target / _node_name())
    finally:
        part.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)


#: 窗口宿主（自带 Chromium 的独立窗口）下载源。
#: 用 Electron 官方发布包 + npmmirror 镜像（项目口径：只用 npmmirror / ModelScope）。
#: ⚠️ 这不是「打包进安装包」的依赖（+220 MB），而是**安装/首次启动时下载**
#: （用户口径 2026-10-23「安装时自动下载」）。
WINDOW_HOST_VERSION = "33.2.0"
#: ⚠️ **国内源优先，且配两套**（用户口径 2026-10-23）：
#:   ① `cdn.npmmirror.com`（CDN 加速，实测 206 / 114,923,352 B）
#:   ② `mirrors.huaweicloud.com`（备用，实测同样可用）
#: 实测过 `mirrors.cloud.tencent.com` 下没有 electron 目录（404），不要加回来。
def _window_host_urls(arch: str) -> tuple[str, ...]:
    ver = WINDOW_HOST_VERSION
    tail = f"electron-v{ver}-win32-{arch}.zip"
    return (
        f"https://cdn.npmmirror.com/binaries/electron/v{ver}/{tail}",
        f"https://mirrors.huaweicloud.com/electron/v{ver}/{tail}",
    )


WINDOW_HOST_URLS: dict[str, tuple[str, ...]] = {
    "x64": _window_host_urls("x64"),
    "ia32": _window_host_urls("ia32"),
}
#: 下载后必须存在的可执行文件名（打包时把 electron.exe 改名）。
WINDOW_HOST_EXE = "SpiderXHSWindow.exe"
#: 只保留这两个语言包（locales 原始 41 MB / 55 个）。
WINDOW_HOST_LOCALES = ("zh-CN.pak", "en-US.pak")
#: 裁剪后仍应大于这个体积，否则视为解包失败（防止半截产物被判「可用」）。
WINDOW_HOST_MIN_BYTES = 150 * 1024 * 1024


def probe_window_host(target: Path) -> bool:
    """窗口宿主是否可用：`SpiderXHSWindow.exe` 在，且 resources/app/main.js 在。

    只检查 exe 存在是不够的 —— 解压中断会留下一个能启动但**没有 app 代码**的
    Electron，它起来后闪一下就退（真机上会被当成「点了没反应」）。
    """
    exe = target / WINDOW_HOST_EXE
    main_js = target / "resources" / "app" / "main.js"
    return exe.is_file() and main_js.is_file()


def fetch_window_host(target: Path) -> None:
    """下载 Electron → 解包 → 裁掉多余语言包 → 改名为 `SpiderXHSWindow.exe`。

    为什么自带内核（用户口径「不再依赖不同电脑的不同环境」）：
    真机实测过三种失败 —— 用户没装 Chrome；Edge 在已有会话时打印
    「正在现有浏览器会话中打开」并立即退出（code 0），窗口开了却被误判为失败；
    360/QQ 等吞掉 `--app=` 变普通标签页。内置内核让所有电脑行为一致。
    """
    import zipfile

    target.mkdir(parents=True, exist_ok=True)
    part = target / "electron.zip.part"
    staged = target / ".staged_window_host"
    try:
        part.write_bytes(_download_first_ok(WINDOW_HOST_URLS.get(_node_arch(), WINDOW_HOST_URLS["x64"]), "window-host"))
        if staged.exists():
            import shutil

            shutil.rmtree(staged, ignore_errors=True)
        with zipfile.ZipFile(part) as zf:
            zf.extractall(staged)

        #: 裁剪：locales 只留中文/英文（41 MB → 约 0.5 MB）
        locales = staged / "locales"
        if locales.is_dir():
            for pak in locales.iterdir():
                if pak.name not in WINDOW_HOST_LOCALES:
                    pak.unlink(missing_ok=True)

        exe_src = staged / "electron.exe"
        if not exe_src.is_file():
            raise RuntimeError("压缩包里没有 electron.exe")
        exe_src.replace(staged / WINDOW_HOST_EXE)

        #: 宿主代码来源**两处**（按存在性取，不猜环境）：
        #:   ① 安装态：`<安装目录>/runtime/window-host-src/`（打包时由 build.ps1 拷进去）
        #:   ② 开发态：`<仓库根>/packaging/window-host/`
        #: 这样「开发机点一下下载」与「装好的用户点一下下载」走同一条代码路径。
        app_dir = staged / "resources" / "app"
        app_dir.mkdir(parents=True, exist_ok=True)
        candidates = (
            install_root() / "runtime" / "window-host-src",
            Path(__file__).resolve().parents[3] / "packaging" / "window-host",
        )
        src_dir = next((c for c in candidates if (c / "main.js").is_file()), candidates[0])
        for name in ("main.js", "package.json"):
            src = src_dir / name
            if not src.is_file():
                raise RuntimeError(f"缺少窗口宿主代码 {src}（安装包不完整）")
            (app_dir / name).write_bytes(src.read_bytes())

        total = sum(f.stat().st_size for f in staged.rglob("*") if f.is_file())
        if total < WINDOW_HOST_MIN_BYTES:
            raise RuntimeError(f"解出的窗口宿主体积异常（{total} 字节）")

        #: 整体替换（先删旧目录，避免旧版残留文件混在里面）
        import shutil

        for old in target.iterdir():
            if old.name == part.name or old == staged:
                continue
            shutil.rmtree(old, ignore_errors=True) if old.is_dir() else old.unlink(missing_ok=True)
        for item in staged.iterdir():
            item.replace(target / item.name)
    finally:
        part.unlink(missing_ok=True)
        import shutil

        shutil.rmtree(staged, ignore_errors=True)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

DEPENDENCIES: tuple[DependencySpec, ...] = (
    DependencySpec(
        key="node",
        name="Node 运行时（请求签名）",
        size_hint="≈83 MB",
        dir_name="node",
        probe=probe_node,
        fetch=fetch_node,
        detail="小红书接口签名必需；缺少时所有平台请求都无法签名（**必装**）。",
        required=True,
    ),
    DependencySpec(
        key="ffmpeg",
        name="ffmpeg（视频抽帧）",
        size_hint="≈28 MB",
        dir_name="ffmpeg",
        probe=probe_ffmpeg,
        fetch=fetch_ffmpeg,
        detail="用于商品视频抽帧检测违规词；缺失时视频无法自动检测，会默认不勾选。",
    ),
    DependencySpec(
        key="window_host",
        name="独立窗口内核（内置 Chromium）",
        size_hint="≈220 MB",
        dir_name="window-host",
        probe=probe_window_host,
        fetch=fetch_window_host,
        detail=(
            "让界面以**独立窗口**打开，不依赖电脑上装了什么浏览器。"
            "缺失时会退回用 Edge/Chrome/默认浏览器打开（行为因电脑而异，"
            "真机上出现过「点了没反应」）。"
        ),
        #: 不设 required：`window_host` 沿用 DependencySpec 其它字段默认值。
    ),
    DependencySpec(
        key="ocr_models",
        name="OCR 模型（PP-OCRv6 tiny）",
        size_hint="≈7 MB",
        dir_name="ocr_models",
        probe=probe_ocr_models,
        fetch=fetch_ocr_models,
        detail="违规词检测的识别模型；缺失时「检测违规词」不可用。",
    ),
)


def _with_retries(fetch: Callable[[Path], None], target: Path, label: str) -> None:
    """跑一次下载，**失败自动重试 `DOWNLOAD_RETRIES` 次**（用户口径 2026-10-23）。

    * 每次重试前清掉可能的半截产物（`*.part` 与目标目录里的残留），
      否则第二次会撞上第一次留下的坏文件。
    * 全部失败 ⇒ 抛最后一次的异常（调用方把原因写进进度，前端显示「清空重下」）。
    """
    import time

    last: Exception | None = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            #: 每次尝试前把「第 N/3 次」报到进度里 —— 用户口径「下载进度需要反馈给用户」：
            #: 220 MB 的内核第一次失败后要等 2 秒再试，界面若一片安静，用户会以为卡死。
            _emit_progress(f"{label}（第 {attempt}/{DOWNLOAD_RETRIES} 次）", 0, 0)
            fetch(target)
            return
        except Exception as exc:  # noqa: BLE001 - 重试后仍失败才抛
            last = exc
            logger.warning(
                "依赖下载失败（%s）第 %s/%s 次：%s", label, attempt, DOWNLOAD_RETRIES, exc
            )
            if attempt < DOWNLOAD_RETRIES:
                #: 清半截产物再重试
                for part in target.glob("*.part"):
                    part.unlink(missing_ok=True)
                time.sleep(RETRY_BACKOFF_S * attempt)
    raise RuntimeError(
        f"重试 {DOWNLOAD_RETRIES} 次仍失败：{last}"
    ) from last


def spec_by_key(key: str) -> DependencySpec | None:
    for spec in DEPENDENCIES:
        if spec.key == key:
            return spec
    return None


def dependency_status(spec: DependencySpec) -> dict:
    """单个依赖的状态（**只读探测，不下载**）。"""
    target = deps_root() / spec.dir_name
    ready = False
    error = ""
    try:
        ready = bool(spec.probe(target))
    except Exception as exc:  # noqa: BLE001 - 探测异常 = 不可用，原因回给前端
        error = str(exc)
    return {
        "key": spec.key,
        "name": spec.name,
        "size_hint": spec.size_hint,
        "detail": spec.detail,
        "dir": str(target),
        "ready": ready,
        "status": "ready" if ready else "missing",
        "error": error,
        "resettable": spec.resettable,
        "required": spec.required,
    }


def all_status() -> dict:
    """全部依赖状态 + 汇总。"""
    items = [dependency_status(s) for s in DEPENDENCIES]
    missing = [i["key"] for i in items if not i["ready"]]
    required_missing = [i["key"] for i in items if i["required"] and not i["ready"]]
    return {
        "items": items,
        "missing": missing,
        "required_missing": required_missing,
        "ready": not missing,
        "root": str(deps_root()),
        "bundled_note": BUNDLED_ONLY_NOTE,
    }


def reset_dependency(key: str) -> dict:
    """**清空本地重新下载** —— 删掉该依赖的落盘目录。

    用户口径：「如果下载失败支持点击清空本地重新下载」⇒ 清空必须**真的删**
    （不是只把状态改成 missing），否则半截文件仍在，重下会撞上它。
    """
    spec = spec_by_key(key)
    if spec is None:
        raise KeyError(f"未知依赖：{key}")
    target = deps_root() / spec.dir_name
    removed = False
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
        removed = True
        logger.info("已清空依赖目录 %s（key=%s）", target, key)
    return {"key": key, "removed": removed, "dir": str(target)}


# ---------------------------------------------------------------------------
# 依赖**修好之后要把搜索路径刷新出去**（2026-10-23 实机报障）
# ---------------------------------------------------------------------------
# 1.0.6 装到别的电脑：**下载 node 成功，但绑定账号立刻报
#   「XHS PCQR code generation failed: JS mnsv2 签名失败或未达到硬门禁
#   （档位 mns0201_，无短签/静态兜底）」** —— 重开软件就好了。
#
# 根因不是下载，也不是签名算法：
#   `apis/xhs_qianfan_apis._node_exe()` / `xhs_utils/**/runtime.py` 里的
#   `subprocess.run(['node', ...])` 按 **PATH** 解析，而 launcher 只在**启动那一刻**
#   前插一次 PATH；用户是**先启动、后下载** node ⇒ 该进程整个生命周期里都找不到 node
#   ⇒ `run_signer` 返回 None ⇒ `params.py` 抛出那句 mnsv2 硬门禁错误。
#   重开软件才重新探测 ⇒ 正常（用户实测「重开就好了」）。
#
# 所以：**依赖落地成功即刷新进程内搜索路径**，不要求用户重开软件。


def refresh_runtime_paths() -> dict:
    """把刚下载好的 node 目录**前插进程 PATH**（`deps/node` → 包内 `runtime/node`）。

    * 裸 `subprocess.run(['node', ...])`（`xhs_utils/xhs_pc/runtime.py`、
      `xhs_creator/runtime.py`、`xhs_core/runtime.py`、`xhs_pc/params.py`）与
      PyExecJS 都按 PATH 解析 ⇒ 前插后**无需重启**即可签名。
    * 幂等：已前插过的目录不再重复插入，重复调用不会把 PATH 越拉越长。
    * 开发机本来就有 node，前插走不到（无 deps/node、非 frozen）⇒ 无副作用。

    :returns: `{"node": "<前插的目录>" | "", "changed": bool}`
    """
    dirs: list[Path] = []
    node_dir = deps_root() / "node"
    #: ⚠️ 只有**真的可用**才前插（`probe_node` 跑 `--version`）：
    #: 只判 `is_file()` 会把下载到一半的 node.exe 前插进 PATH，结果「有 node 但跑不起来」，
    #: 报错从「找不到 node」变成「node 退出码 1」，同样难查（2026-10-23）。
    if node_dir.is_dir() and probe_node(node_dir):
        dirs.append(node_dir)
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        for cand in (base / "runtime" / "node", base.parent / "runtime" / "node"):
            if (cand / _node_name()).is_file() and cand not in dirs:
                dirs.append(cand)
    if not dirs:
        return {"node": "", "changed": False}

    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    changed = False
    for cand in reversed(dirs):  # 倒序前插 ⇒ 最终顺序与 dirs 一致（deps/node 最优先）
        text = str(cand)
        if text in parts:
            continue
        parts.insert(0, text)
        changed = True
    if changed:
        os.environ["PATH"] = os.pathsep.join(parts)
        logger.info("已前插 node 搜索路径：%s", os.pathsep.join(str(d) for d in dirs))
    return {"node": str(dirs[0]), "changed": changed}
