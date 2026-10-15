"""图片 OCR 违规词检测（设计 25 §3；用户口径 2026-10-08）。

## 引擎选择：Windows.Media.Ocr（**零新增依赖**，实测已验证）

本机可用性实测（2026-10-08，Python 3.12）：

| 引擎 | 状态 |
|---|---|
| `pytesseract` / `tesseract.exe` | ❌ 未装（不在 PATH） |
| `paddleocr` / `easyocr` / `cnocr` / `rapidocr` | ❌ 未装 |
| **`Windows.Media.Ocr`** | ✅ **可用，`zh-Hans-CN`** |

⇒ 选 Windows 内置 OCR。**不装**新依赖是这个项目的既有纪律（`requirements.txt`
不因一个功能膨胀），且本机实测单图 **0.6s**、69 张图约 **41s**，够用。

## ⚠️ 三条由实测得出的硬约束（别按「文档该有的样子」改）

### 1. Windows OCR 把中文切成**单字**，不是词

实测 `国家级 环保认证` 被切成 `国` `家` `级` `环` `保` `认` `证`
七 个 OcrWord，**每个 OcrWord 一个字**。所以「按 word 找违规词」永远找不到
「国家级」⇒ 必须做**连续字符合并**（`merge_char_boxes`）。

### 2. `OcrLine.BoundingRect` 在本机**恒为 (0,0,0,0)**

实测每一条 line 的 box 都是全零，只有 `OcrWord.BoundingRect` 可信。
⇒ 红框只用**词级**坐标，line 级坐标在 Python 侧由 words 合并得出。

### 3. OCR 字间有空格：`国 家 级` ⇒ 归一化后必须去空格

`forbidden_word_service.normalize` 已经 NFKC + 去空白 + 转小写，本模块
的匹配**一律走它**，不自己再实现一套。

## 红框坐标：相对原图的百分比

返回 `box: {x, y, w, h}` 单位是**原图像素**（脚本已把缩放倍率乘回去）。
前端按容器尺寸换算成百分比叠加（`left = x / img_w * 100%`），这样
图片被 CSS 缩放时红框仍然贴合。
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from backend.app.services import rapid_ocr_service as rapid
from backend.app.services import settings_service
from backend.app.services.forbidden_word_service import find_hits, normalize

logger = logging.getLogger(__name__)


#: 识别前的最大边长。超出的先缩再识别（对超大图慢且吃内存），
#: 引擎会把坐标**乘回原图尺度**，所以红框位置不受影响。
MAX_DIM = 1600

#: 单图 OCR 超时（秒）。RapidOCR 实测 3.3s；留 10 倍余量给冷启动 + 大图。
PER_IMAGE_TIMEOUT = 30.0

#: **初始**并发数（用户口径 2026-10-13）。
#:
#: ⚠️ 这是**起步值**，不是固定值 —— 真实并发由 `resolve_parallelism()` 按
#: 「系统参数 `xhs_ocr_parallelism`」+「本机 CPU 核数」动态算（见该函数）。
#: 默认 5 与用户口径一致；配置项在设置页可调（默认 5）。
DEFAULT_PARALLELISM = 5

#: 并发数硬上界。再高会因 CPU 争用让**总耗时反而上升**（RapidOCR 是 CPU 密集，
#: 每路都在抢核），且内存按路数线性增长（每路的 ONNX 推理缓冲）。
MAX_PARALLELISM = 32

#: **目标总耗时**（秒）。用户口径 2026-10-13：「时间控制在 30 秒以内」。
#:
#: ⚠️ **实测做不到，这里如实标注**（硬约束⑤不造假）：RapidOCR 是纯 CPU 密集，
#: 实测（本机 8 逻辑核）吞吐在 **4 路**就饱和 ——
#: 24 张样本：1 路 41.1s / 4 路 19.4s（2.1x）/ 8 路 19.8s / 12 路 19.4s，
#: 再加路数**毫无收益**。单图推理 ~1.74s（占单图耗时 99.5%，读图只 0.01s）。
#: ⇒ 118 张的**物理下限**约 96s（4 路 × 0.81s/张），30s 只能靠更小的图 / 更强的 CPU。
#: 该常量只用于**反推并发档位**，同时作为上报给前端的**参照值**（前端显示
#: 「实际 Xs / 目标 30s」），不是达标承诺。
TARGET_TOTAL_SECONDS = 30.0

#: 单图预估耗时（秒）。用于「按目标时间反推并发数」—— 实测单图推理 ~1.74s，
#: 4 路并发下有效吞吐 ~0.81s/张。取 1.0 作为**每路有效速率**的保守估计。
SECONDS_PER_IMAGE = 1.0

#: 吞吐**饱和点**（路数）。实测再加路数无收益（见 `TARGET_TOTAL_SECONDS` 注）。
#: 用它作为「按目标耗时上推」的**实际可用上限**，避免开一堆白占内存的线程。
SATURATION_WORKERS = 4

#: 瞬时失败重试次数 + 退避基数（秒）。
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 0.4

#: **可重试**的错误码 —— 引擎层的瞬时争用，不是「图坏了」。
RETRYABLE_CODES: frozenset[str] = frozenset({"OCR_NO_OUTPUT", "OCR_FAILED"})

#: 单个数据包的媒体张数上限（防「用户丢了个 5000 张的目录」把内存吃光）。
MAX_IMAGES = 300


class OcrError(RuntimeError):
    """OCR 链路不可用 / 单图失败（**带 code**，路由据此给结构化错误）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def engine_available() -> bool:
    """OCR 引擎是否可用（**不识别任何图**，只探一次引擎）。"""
    return rapid.engine_available()


def resolve_parallelism(pending: int) -> int:
    """算**本次实际并发数**：配置起始值 → 按核数夹 → 按目标耗时上推。

    三档依据（用户口径 2026-10-13「时间控制在 30 秒以内，或按电脑使用情况自动调节」）：

    1. **起始值** = 系统参数 `xhs_ocr_parallelism`（默认 5，设置页可调）；
    2. **按核数夹**：RapidOCR 是 CPU 密集且 onnxruntime 每路自带线程池
       ⇒ 路数 ≈ 核数时吞吐最高，超过核数只是互相抢核（总耗时反而上升）。
       故上界取 `max(起始值, 核数)` —— **用户的配置是下限，不被机器缩小**
       （否则用户在 16 核机上填 12 却被夹到 8 会以为配置没生效）；
    3. **按目标耗时上推**：`ceil(pending × 单图预估 / 30s)`。图多时自动加路，
       让批量预检落在 `TARGET_TOTAL_SECONDS` 内。

    最后夹到 `[1, MAX_PARALLELISM]`，且**不超过待处理图数**（2 张图开 16 路无意义）。

    ⚠️ **这是估算驱动的调参**，不是「保证 30 秒」的承诺：单图耗时随图内容波动
    （纯色图 0.5s、密集文字图 6s+）。做不到时如实反映在返回值上（见 `scan_images`
    的 `summary.parallelism` / `summary.elapsed_seconds`），不假装达标。
    """
    if pending <= 0:
        return 1
    try:
        start = settings_service.live_int(
            None, None, "xhs_ocr_parallelism", default=DEFAULT_PARALLELISM
        )
    except Exception:  # pragma: no cover - 配置层异常不该让 OCR 跑不起来
        start = DEFAULT_PARALLELISM
    start = max(1, int(start))

    #: ⚠️ 实测吞吐饱和点是 4 路（见 `SATURATION_WORKERS` 注）—— 再加路数**无收益**，
    #: 只会白占内存。所以这里把「按目标耗时上推」的结果**夹在饱和点内**；
    #: 用户把配置填得比饱和点高时不拦（尊重显式配置），但自动上推不会越过它。
    cpu = os.cpu_count() or 4
    ceiling = min(MAX_PARALLELISM, max(start, SATURATION_WORKERS, min(cpu, MAX_PARALLELISM)))

    needed = math.ceil(pending * SECONDS_PER_IMAGE / TARGET_TOTAL_SECONDS)
    needed = min(needed, SATURATION_WORKERS) if start <= SATURATION_WORKERS else needed
    return max(1, min(ceiling, max(start, needed), pending))


def _scan_one(path: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """单图：OCR → 匹配 → 定位红框。**异常不外泄**（转成 `ocr_ok=False`）。"""
    name = os.path.basename(path)
    last: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            payload = rapid.recognize(Path(path))
        except rapid.RapidOcrUnavailable as error:
            #: 引擎级不可用（没装/模型坏）—— 重试无意义，直接判失败。
            return {
                "path": path, "name": name, "ocr_ok": False, "ocr_text": "",
                "hits": [], "error": "OCR_FAILED", "error_message": str(error),
                "width": 0, "height": 0,
            }
        except Exception as error:  # pragma: no cover - 引擎内部瞬时异常
            last = error
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_S * attempt)
            continue
        return _payload_to_scan(path, payload, entries)
    return {
        "path": path, "name": name, "ocr_ok": False, "ocr_text": "",
        "hits": [], "error": "OCR_FAILED",
        "error_message": str(last or "识别失败"),
        "width": 0, "height": 0,
    }


# --------------------------------------------------------------------------- #
# 纯逻辑：把单字 box 合并成词 box
# --------------------------------------------------------------------------- #


def merge_char_boxes(words: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 Windows OCR 的**单字** boxes 按行合并成**词** boxes。

    输入是一行内的 `OcrWord` 列表（每个可能只有一个汉字）。相邻且
    **垂直重叠**的字合并 —— 垂直重叠是同一行的可靠判据（同一行字的
    y 区间必然相交），比「x 间距 < 阈值」稳（后者会被标点、字距差异打乱）。

    返回 `[{text, x, y, w, h}]`，`text` 是**原样拼接**（不归一化 —— 匹配时
    再走 `normalize`）。

    ⚠️ 空/坏 box 整条丢弃：宁可少框，不框错位置（错框比不框更误导）。
    """
    items: list[dict[str, Any]] = []
    for word in words or []:
        try:
            text = str(word.get("text") or "")
            x = float(word["x"])
            y = float(word["y"])
            w = float(word["w"])
            h = float(word["h"])
        except (KeyError, TypeError, ValueError):
            continue
        if not text or w <= 0 or h <= 0:
            continue
        items.append({"text": text, "x": x, "y": y, "w": w, "h": h})
    if not items:
        return []

    items.sort(key=lambda i: (i["x"], i["y"]))
    merged: list[dict[str, Any]] = []
    for item in items:
        if merged:
            last = merged[-1]
            # 垂直重叠 > 50% 视为同一行；同行的字按 x 顺序依次并入。
            overlap = min(last["y"] + last["h"], item["y"] + item["h"]) - max(
                last["y"], item["y"]
            )
            min_h = min(last["h"], item["h"]) or 1.0
            if overlap / min_h > 0.5:
                right = max(last["x"] + last["w"], item["x"] + item["w"])
                bottom = max(last["y"] + last["h"], item["y"] + item["h"])
                last["text"] += item["text"]
                last["x"] = min(last["x"], item["x"])
                last["y"] = min(last["y"], item["y"])
                last["w"] = right - last["x"]
                last["h"] = bottom - last["y"]
                continue
        merged.append(dict(item))
    return merged


def _char_map(words: Iterable[dict[str, Any]]) -> tuple[str, list[dict[str, float]]]:
    """把一行 words 摊平成「归一化文本 + 每个字符的 box」。

    ⚠️ **关键：这里刻意不走 `merge_char_boxes`。** 那个函数把一整行并成
    **一个**大 box（实测「国 家 级 环 保 认 证」7 字 → 1 个 404px 宽的框），
    拿去当字符级 box 会让每个字都共享那个大框 ⇒ 红框横跨半张图，没有指向性
    （这是第一版实现的实测 bug）。这里要的是**原始 word 的 box** ——
    Windows OCR 已经把中文切到单字，**每个 word 恰好就是一个字的框**。

    若某个 word 本身是多个字符（英文单词 / 数字串），退化成按**字符数均分**
    该 word 的宽度：宁可框略宽一点，也不能让下标错位。

    归一化**逐字符**做：整串 NFKC 可能改变长度，字符↔box 的下标就会断。
    """
    text_parts: list[str] = []
    boxes: list[dict[str, float]] = []
    for word in words or []:
        try:
            raw = str(word.get("text") or "")
            x = float(word["x"]); y = float(word["y"])
            w = float(word["w"]); h = float(word["h"])
        except (KeyError, TypeError, ValueError):
            continue
        if not raw or w <= 0 or h <= 0:
            continue
        box = {"x": round(x, 1), "y": round(y, 1), "w": round(w, 1), "h": round(h, 1)}
        # 该 word 归一化后占几个字符（逐字符算，丢弃空的）
        pieces = [(ch, normalize(ch)) for ch in raw]
        pieces = [(ch, p) for ch, p in pieces if p]
        if not pieces:
            continue
        step = box["w"] / len(pieces)
        for index, (_ch, piece) in enumerate(pieces):
            text_parts.append(piece)
            # 最后一个字用右边界对齐，避免均分带来的累计 1px 误差。
            left = round(box["x"] + step * index, 1)
            right = box["x"] + box["w"] if index == len(pieces) - 1 else round(box["x"] + step * (index + 1), 1)
            boxes.append({**box, "x": left, "w": round(right - left, 1)})
    return "".join(text_parts), boxes


def locate_hits(
    text: str,
    words_by_line: list[list[dict[str, Any]]],
    hits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """给每条命中找**红框坐标**（可多个 —— 同一个词在一张图里出现两次）。

    算法：逐行摊平成字符级映射 → 在归一化行文本里找违规词的**子串位置**
    → 取该子串覆盖到的所有字符 box 的**并集**（紧框）。

    ⚠️ 同一个词在一行里出现多次 ⇒ 多个框（前端全画）。前端画 `boxes` 全部，
    `box` 只是首框（兼容只看单个框的调用方）。

    ⚠️ **找不到位置时 `box` 为 `None`**（例如违规词被 OCR 切在两行中间）。
    前端对 `box=None` 要**只标红缩略图、不画框**，不能画个 (0,0,0,0) 的假框。
    """
    # 预先摊平所有行（一次算完，多个 hit 共用）
    line_maps = [_char_map(words) for words in words_by_line]

    out: list[dict[str, Any]] = []
    for hit in hits:
        needle = normalize(hit.get("word"))
        boxes: list[dict[str, float]] = []
        if needle:
            for line_text, char_boxes in line_maps:
                start = line_text.find(needle)
                while start != -1:
                    end = start + len(needle)
                    span = char_boxes[start:end]
                    if span:
                        left = min(b["x"] for b in span)
                        top = min(b["y"] for b in span)
                        right = max(b["x"] + b["w"] for b in span)
                        bottom = max(b["y"] + b["h"] for b in span)
                        boxes.append(
                            {
                                "x": round(left, 1),
                                "y": round(top, 1),
                                "w": round(right - left, 1),
                                "h": round(bottom - top, 1),
                            }
                        )
                    start = line_text.find(needle, end)
        out.append({**hit, "box": boxes[0] if boxes else None, "boxes": boxes})
    return out


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #


#: 视频不进 OCR 时给前端的**明确**说明（不是「检过且合规」）。
#: ⚠️ 现在有了抽帧（见下方 `scan_video`），这条只在**抽帧失败**时用。
VIDEO_UNVERIFIED_REASON = "视频未能抽帧检测，请人工确认画面是否有违规词"

#: 抽帧数（等间隔）。真机实测 40.8s / 1224 帧的视频抽 6 帧 ≈ 18s（含 ffmpeg 启动），
#: 6 帧已能覆盖「片头标注 + 中段卖点 + 片尾促销」这类常见文字出现位置。
VIDEO_FRAMES = 6

#: 单帧抽取消耗超时（秒）。ffmpeg 抽一帧实测亚秒级，留足余量给大视频首帧解码。
FRAME_TIMEOUT_S = 60

#: 抽帧后的缩放上限（宽）。视频常见 720~1080 宽，这一档既保文字清晰又省 OCR 时间。
FRAME_MAX_WIDTH = 1280


def note_unverified_videos(video_paths: list[str]) -> dict[str, dict[str, Any]]:
    """给视频条目落一份「**未验证**」结果，供 `scan_images` 一起返回。

    ⚠️ **为什么要显式标记，而不是让视频查不到就不返回**：
    预检契约里每条媒体都必须有 `ocr_ok` / `hits` 这套键。前端
    「默认勾选」逻辑判的是 `ocr_ok !== false`，一个字段缺失会被当成
    「检过且合规」⇒ **违规视频被自动勾上**（我第一版就是这个坑）。
    `ocr_ok=None` = 「没检过」⇒ 前端默认不勾，并提示人工确认。
    """
    return {
        str(path): {
            "path": str(path),
            "name": Path(str(path)).name,
            "group": "video",
            "ocr_ok": None,
            "ocr_text": "",
            "hits": [],
            "unverified_reason": VIDEO_UNVERIFIED_REASON,
        }
        for path in video_paths
    }


def ffmpeg_available() -> bool:
    """ffmpeg 是否可用（抽帧的硬依赖）。

    ⚠️ 实测本机 `ffmpeg 9.0.1`（gyan.dev full build）在 PATH 里。
    不可用时**不静默降级**：视频回到「未验证」而不是被当成合规。
    """
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe_duration(video: Path) -> float:
    """读视频时长（秒）；读不到 ⇒ 0.0（调用方按「无法抽帧」处理）。"""
    try:
        done = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video),
            ],
            capture_output=True,
            text=True,
            timeout=FRAME_TIMEOUT_S,
        )
        return float((done.stdout or "").strip() or 0.0)
    except Exception:  # noqa: BLE001 - 探不到时长不该让预检崩
        return 0.0


def extract_frames(video: Path, count: int = VIDEO_FRAMES) -> list[tuple[float, str]]:
    """等间隔抽 `count` 帧到临时目录，返回 `[(秒, 帧图路径)]`。

    ⚠️ **帧图放临时目录，不进用户的数据包**（只读边界：绝不往用户目录写东西）。
    调用方负责用 `cleanup_frames` 删掉。
    """
    duration = probe_duration(video)
    if duration <= 0:
        return []
    workdir = Path(tempfile.mkdtemp(prefix="video_frames_"))
    out: list[tuple[float, str]] = []
    #: 取每段的中点，避开首尾的黑场/转场（首帧常是纯色 logo）。
    for index in range(count):
        at = duration * (index + 0.5) / count
        dest = workdir / f"frame{index:03d}.jpg"
        try:
            done = subprocess.run(
                [
                    "ffmpeg", "-v", "error",
                    "-ss", f"{at:.3f}",
                    "-i", str(video),
                    "-frames:v", "1",
                    "-vf", f"scale='min({FRAME_MAX_WIDTH},iw)':-2",
                    "-q:v", "3",
                    "-y", str(dest),
                ],
                capture_output=True,
                text=True,
                timeout=FRAME_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            continue
        if done.returncode == 0 and dest.is_file() and dest.stat().st_size > 0:
            out.append((round(at, 2), str(dest)))
    return out


def cleanup_frames(pairs: Iterable[tuple[float, str]]) -> None:
    """删掉抽出来的帧图与它们的临时目录（**尽力而为**，不抛异常）。"""
    dirs: set[Path] = set()
    for _, path in pairs:
        try:
            p = Path(path)
            dirs.add(p.parent)
            p.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    for d in dirs:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


def scan_video(
    video_path: str,
    entries: list[dict[str, Any]],
    frames: int = VIDEO_FRAMES,
) -> dict[str, Any]:
    """**视频抽帧 OCR**：抽 N 帧 → 逐帧 OCR → 合并命中。

    返回形状与 `_scan_one` 一致（多了 `frames` / `frame_count` 信息），
    所以上层可以直接塞进 `by_path`，前端也按同一套键渲染。

    ## 三态（与图片同口径）

    | 情况 | `ocr_ok` | 说明 |
    |---|---|---|
    | 抽帧成功且全部识别完 | `True` | `hits` 汇总所有帧，每条带 `at`（第几秒） |
    | 抽帧成功但有帧没识别出来 | `True` | 命中的照样报，失败帧计入 `frames_failed` |
    | 抽帧失败 / ffmpeg 缺失 / 时长读不到 | `None` | `unverified_reason` 说明原因，**默认不勾选** |

    ⚠️ **绝不把「没抽到帧」当合规**：那正是这条链最危险的静默失败
    （用户以为视频检过了，其实一帧没看）。
    """
    path = str(video_path)
    name = os.path.basename(path)
    base: dict[str, Any] = {
        "path": path,
        "name": name,
        "group": "video",
        "ocr_ok": None,
        "ocr_text": "",
        "hits": [],
        "error": "",
        "error_message": "",
        "width": 0,
        "height": 0,
        "frames": [],
        "frame_count": 0,
        "frames_failed": 0,
        "unverified_reason": None,
    }

    if not ffmpeg_available():
        base["unverified_reason"] = "本机没有可用的 ffmpeg，无法抽帧检测，请人工确认"
        return base

    pairs = extract_frames(Path(path), frames)
    if not pairs:
        base["unverified_reason"] = VIDEO_UNVERIFIED_REASON
        return base

    try:
        scan = scan_images([p for _, p in pairs], entries)
        by_frame = {r["path"]: r for r in scan["results"]}

        merged_hits: list[dict[str, Any]] = []
        texts: list[str] = []
        frame_meta: list[dict[str, Any]] = []
        failed = 0
        for at, frame_path in pairs:
            found = by_frame.get(frame_path) or {}
            if not found.get("ocr_ok"):
                failed += 1
            text = str(found.get("ocr_text") or "")
            if text:
                texts.append(text)
            #: ⚠️ 每条命中都带上**第几秒** —— 前端要能告诉用户「第 12.3 秒有违规词」，
            #: 否则一个 40 秒的视频报「有违规词」，用户得自己拉进度条找。
            for hit in found.get("hits") or []:
                merged_hits.append({**hit, "at": at})
            frame_meta.append(
                {
                    "at": at,
                    "ocr_ok": bool(found.get("ocr_ok")),
                    "text": text,
                    "hits": [h.get("word") for h in (found.get("hits") or [])],
                }
            )

        base["ocr_ok"] = True
        base["ocr_text"] = "\n".join(texts)
        base["hits"] = merged_hits
        base["frames"] = frame_meta
        base["frame_count"] = len(pairs)
        base["frames_failed"] = failed
        return base
    finally:
        cleanup_frames(pairs)


def scan_images(
    image_paths: list[str],
    entries: list[dict[str, Any]],
    on_one_done: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """批量识别 + 过滤。**一进程多图（批量），结果顺序与输入一致**。

    返回 `{ results: [...], summary: {...} }`；每条
    `{ path, name, group, ocr_ok, ocr_text, hits: [...], error? }`。

    ⚠️ **单图失败不中断整批** —— 一张图格式损坏不该让 69 张全废。
    失败条目 `ocr_ok=False` + `error`，前端把它当「未检出」但**保留勾选**
    （缺数据 ≠ 安全，见下方「三态」）。

    ⚠️ **为什么不再是「一图一进程 + 线程池」**（实测 2026-10-09）：
    每次 `powershell -File` 冷启动 ~1.2s，而真正 OCR 只 ~0.3s ⇒ 一图一进程
    时约 **80% 时间花在进程启动**。改成**一次进程 OCR 全部图**（引擎只装配
    一次），68 张图从 ~30s+ 降到 ~5s。批量脚本不可用时**回退**到旧的
    逐图 + 线程池路径（保证非 Windows / 脚本缺失时仍能跑，只是慢）。

    `on_one_done`：每完成一张回调一次（**执行进度**用，用户口径 2026-10-09）。
    """
    paths = [str(p) for p in image_paths][:MAX_IMAGES]
    if not paths:
        return {
            "results": [],
            "summary": {
                "total": 0, "ok": 0, "failed": 0, "violations": 0,
                "truncated": False, "parallelism": 0, "elapsed_seconds": 0.0,
                "target_seconds": TARGET_TOTAL_SECONDS,
            },
        }
    if not engine_available():
        raise OcrError("OCR_ENGINE_MISSING", "OCR 引擎不可用（RapidOCR 未安装或模型缺失）")

    workers = resolve_parallelism(len(paths))
    started = time.monotonic()
    results: list[dict[str, Any]] = [{} for _ in paths]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_idx = {
            pool.submit(_scan_one, path, entries): i for i, path in enumerate(paths)
        }
        for fut in as_completed(future_to_idx):
            idx = future_to_idx[fut]
            try:
                results[idx] = fut.result()
            except Exception as error:  # pragma: no cover - `_scan_one` 已兜住
                p = paths[idx]
                results[idx] = {
                    "path": p, "name": os.path.basename(p), "ocr_ok": False,
                    "ocr_text": "", "hits": [], "error": "OCR_FAILED",
                    "error_message": str(error), "width": 0, "height": 0,
                }
            if on_one_done is not None:
                on_one_done()

    elapsed = time.monotonic() - started
    ok = sum(1 for r in results if r.get("ocr_ok"))
    return {
        "results": results,
        "summary": {
            "total": len(results),
            "ok": ok,
            "failed": len(results) - ok,
            "violations": sum(1 for r in results if r.get("hits")),
            "truncated": len(image_paths) > len(results),
            #: 用户口径 2026-10-13「时间控制在 30 秒以内」—— 实际并发与耗时
            #: **如实上报**（不假装达标，也不藏），前端可据此显示「N 路并发 / Xs」。
            "parallelism": workers,
            "elapsed_seconds": round(elapsed, 2),
            "target_seconds": TARGET_TOTAL_SECONDS,
        },
    }


def _payload_to_scan(
    path: str, payload: dict[str, Any], entries: list[dict[str, Any]]
) -> dict[str, Any]:
    """把成功 payload 转成单图结果（含命中 + 红框）。"""
    name = os.path.basename(path)
    text = str(payload.get("text") or "")
    words_by_line = [
        [w for w in (line.get("words") or []) if isinstance(w, dict)]
        for line in (payload.get("lines") or [])
    ]
    hits = locate_hits(text, words_by_line, find_hits(text, entries))
    return {
        "path": path,
        "name": name,
        "ocr_ok": True,
        "ocr_text": text,
        "hits": hits,
        "error": "",
        "error_message": "",
        "width": int(payload.get("pixel_width") or 0),
        "height": int(payload.get("pixel_height") or 0),
    }
__all__ = [
    "DEFAULT_PARALLELISM",
    "FRAME_MAX_WIDTH",
    "MAX_DIM",
    "TARGET_TOTAL_SECONDS",
    "MAX_IMAGES",
    "VIDEO_FRAMES",
    "OcrError",
    "cleanup_frames",
    "engine_available",
    "extract_frames",
    "ffmpeg_available",
    "locate_hits",
    "merge_char_boxes",
    "note_unverified_videos",
    "probe_duration",
    "scan_images",
    "resolve_parallelism",
    "scan_video",
]
