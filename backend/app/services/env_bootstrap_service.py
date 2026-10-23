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
import tempfile
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
NODE_URLS = (
    "https://cdn.npmmirror.com/binaries/node/v22.11.0/node-v22.11.0-win-x64.zip",
    "https://mirrors.huaweicloud.com/nodejs/v22.11.0/node-v22.11.0-win-x64.zip",
)

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

#: 每次重试前的等待基数（秒）；第 n 次等 `RETRY_BACKOFF_S * n`（2s / 4s）。
RETRY_BACKOFF_S = 2


def deps_root() -> Path:
    """依赖落盘根目录。

    * 开发态：仓库下 `.hermes/deps/`（不污染 `data/`）。
    * 装包态：**exe 同级的 `deps/`**（用户可见、可删；不要放 `_internal`，
      那里升级安装会被整体覆盖，下载的东西会丢）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "deps"
    return Path(__file__).resolve().parents[3] / ".hermes" / "deps"


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



def _download_first_ok(urls: tuple[str, ...], label: str) -> bytes:
    """**依次尝试多个公共源**，返回第一个成功且体积合理的响应体。

    用户口径 2026-10-23：「准备两套」—— 主源挂了自动切备源，用户不必手动换。

    * 判据：HTTP 成功 **且** 体积 ≥ 1KB（0 字节/半截都算失败，继续试下一个）；
    * 全部失败 ⇒ 抛 `RuntimeError`，列出每个源的实际错误（前端要能看出是网络问题）。
    """
    errors: list[str] = []
    for url in urls:
        host = url.split("/")[2] if url.count("/") >= 2 else url
        try:
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as resp:
                data = resp.read()
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
    data = _download_first_ok(FFMPEG_URLS, "ffmpeg")
    #: 源是 gzip 压缩的单文件
    raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
    if len(raw) < 1_000_000:
        raise RuntimeError(f"解压后过小（{len(raw)} 字节）")
    part.write_bytes(raw)
    part.replace(target / _exe_name())


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
    """下载官方 zip → 解出 `node.exe` 到 `target/`。"""
    import zipfile

    target.mkdir(parents=True, exist_ok=True)
    part = target / "node.zip.part"
    part.write_bytes(_download_first_ok(NODE_URLS, "node"))
    with zipfile.ZipFile(part) as zf:
        member = next(
            (n for n in zf.namelist() if n.endswith("/node.exe")), None
        )
        if member is None:
            raise RuntimeError("压缩包里没有 node.exe")
        data = zf.read(member)
    (target / _node_name()).write_bytes(data)
    part.unlink(missing_ok=True)


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
