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
import re
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

#: **每路**的有效速率（秒/张），用于「按目标时间反推并发数」。
#:
#: ⚠️ 这里容易搞错（2026-10-20 实测踩到两次）：它不是「单图串行耗时」，而是
#: **N 路并发下的单路吞吐**。旧值 1.0 按 PP-OCRv4 的 ~1.74s 串行估，
#: `needed = ceil(118/30) = 4`，恒小于配置值 5 ⇒ **自动上推永不生效**。
#: v5 实测：串行 ~1.1s/图，`resolve_parallelism` 给 5 路时真机 118 张 115s
#: ⇒ 每路有效 **~0.98s/张**（本机 8 核的实际档位）。
#: ⚠️ 别再往小调（试过 0.5）—— 那会让小包也算出「不需要提速」，
#: 而 5 路对小包本来就够；真正的收益来自 `SATURATION_WORKERS`。
SECONDS_PER_IMAGE = 1.0

#: 吞吐**饱和点**（路数）。实测再加路数无收益（见 `TARGET_TOTAL_SECONDS` 注）。
#: 用它作为「按目标耗时上推」的**实际可用上限**，避免开一堆白占内存的线程。
#:
#: ⚠️ **2026-10-20 由 4 改为 8**（换 PP-OCRv5 后重测，同一包 24 张、8 逻辑核）：
#: 4 路 44.1s / **6 路 25.3s** / 8 路 25.2s / 10 路 23.8s —— 6 路之后收益 ≤5%。
#: 取 8（≈ 核数）而非 6 的理由：`TARGET_TOTAL_SECONDS` 是 30s，118 张在
#: `SECONDS_PER_IMAGE=1.0` 下 `needed=4`，**低于配置值 5** ⇒ 自动上推永不动。
#: 饱和点调到 8 后，`needed = ceil(118/30)=4` 仍小于 start，但 300 张时
#: `needed=10` 会被夹到 8，大包真的会往上走；同时 8 路实测与 6 路同级、
#: 且不越过核数（越过核数只会互相抢核）。用户显式配更高仍尊重（见 ceiling）。
SATURATION_WORKERS = 8

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
    """算**本次实际并发数**。

    ## 口径（2026-10-20 重定，带实测依据）

    1. **基准 = 饱和点** `SATURATION_WORKERS`（8 路，实测见该常量注释）；
    2. **用户配置 `xhs_ocr_parallelism` 是「保底」**：配得比饱和点高就按用户的
       （`max(start, SATURATION)`），配得低则自动上推到饱和点 ——
       否则 118 张永远只跑配置值 5 路（**本轮真机实测就是这个坑**：`parallelism: 5`、
       114.8s，而 8 路一样是 114.8s，说明再往上加路数已无收益）；
    3. **不超过待处理图数**（2 张图开 8 路无意义），也不超 `MAX_PARALLELISM`。

    ## 实测边界（别再靠「加线程」提速）

    24 张样本、同一台 8 逻辑核机器，`intra_op_threads` × 路数全网格实测：
    4 路 ~125s、6 路 ~104s、8 路 ~106s（换算 118 张）⇒ **6 路之后饱和**；
    `intra=1/2/4` 差异 ≤5%，`inter` 固定 1。真正的墙在**单张图的识别网络**上
    （SKU 图 16~19 行文字，单张 4~5s），不是线程调度。
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

    #: ⚠️ 实测吞吐饱和点见 `SATURATION_WORKERS`（2026-10-20 重测为 8 路 = 核数）——
    #: 再加路数**收益 ≤5%**，只会白占内存与发热。
    #:
    #: **用户配置的语义是「保底」**（至少开这么宽），不是上限 —— 2026-10-20
    #: 真机踩坑：配置值 5 通过 `max(start, needed)` 把按目标算出的路数压住，
    #: 118 张永远只跑 5 路（`parallelism: 5`、115s）。所以算式是
    #: **「饱和点」优先**：目标只要 30s，就把可用路数用满，配置只在
    #: 用户配得比饱和点更高时生效（尊重显式配置）。
    cpu = os.cpu_count() or 4
    ceiling = min(MAX_PARALLELISM, max(start, SATURATION_WORKERS, min(cpu, MAX_PARALLELISM)))
    want = min(ceiling, max(start, SATURATION_WORKERS))
    return max(1, min(want, pending))


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

#: **抽帧间隔（秒）**：每 `VIDEO_FRAME_INTERVAL_S` 秒抽 1 帧（用户口径 2026-10-23
#: 「优化为 10 抽一帧」，同日二次调整为 **5 秒一帧** —— 10 秒间隔在 13 秒视频上
#: 只抽 2 帧，首尾两段各留 5 秒盲区，画面文字漏检风险偏高）。
#:
#: 演化（原句保留，不静默删除）：
#:   · 旧口径是「固定抽 6 帧、等间隔取每段中点」—— 短视频浪费（8 秒也抽 6 帧），
#:     长视频覆盖不足（40 秒只有 6 个采样点，中间 7 秒盲区）。
#:   · 现口径「按时长分段，每 10 秒一帧」：采样点密度与视频长度成正比，
#:     9 秒视频抽 1 帧、30 秒抽 3 帧、41 秒抽 5 帧 —— 既不浪费也漏不掉。
#:   · `VIDEO_FRAMES_MAX` 是**兜底上限**：极长视频（如 10 分钟）不抽上百帧，
#:     否则 OCR 时间失控（每帧 ffmpeg ~0.5s + OCR ~0.2s）。
#:
#: ⚠️ **超过上限时不截断尾部，改为「平分」**（2026-10-23 用户口径「最高 50 帧，平分」）：
#:   旧实现 `min(count, wanted)` 是**截断**—— 25 秒以上视频只抽前 50 秒的点，
#:   后面整段不检测（危险：片尾促销标注最容易被漏掉）。
#:   现口径：`wanted > 50` 时仍抽 50 帧，但**采样间隔自动放大**为
#:   `duration / 50`，覆盖全片。
#:
#: ⚠️ **首尾各固定抽一帧**（2026-10-23 用户口径「收尾各一帧固定」）：
#:   0 秒附近与末尾各取一帧，**必抽** —— 片头常是品牌/合规声明、片尾常是促销
#:   标注，都是违规词高发位置；只靠「每段中点」在短视频上会从 1/6 处才开始采，
#:   首尾都有盲区。首尾帧占 2 个名额（总帧数仍受 `VIDEO_FRAMES_MAX` 约束）。
VIDEO_FRAME_INTERVAL_S = 5
VIDEO_FRAMES_MAX = 50

#: 兼容旧引用（`scan_video` 的默认值、既有测试都在用这个名字）。
#: ⚠️ 语义已从「固定帧数」变为「上限」，见上方说明。
VIDEO_FRAMES = VIDEO_FRAMES_MAX

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


def _ffmpeg_exe(name: str) -> str | None:
    """定位可执行文件：**deps/ → 包内 bin/ → PATH**，逐级回落。

    ⚠️ 2026-10-23 修：原实现是「`_bundled_bin_dir()` 一旦有目录就**只**在该目录找」，
    而 `_bundled_bin_dir()` 判断的是 `ffmpeg.exe` 在不在 —— 于是 `deps/ffmpeg/`
    里只下了 ffmpeg、没下 ffprobe 时，`_ffmpeg_exe("ffprobe")` 返回 None
    且**不会回落到 PATH**（开发机 PATH 里有 ffprobe 却被忽略）。
    ⇒ 改成**按 name 逐个候选路径独立判断**，任一命中即用。
    """
    exe_name = f"{name}.exe" if os.name == "nt" else name
    for cand in _candidate_bin_dirs():
        exe = cand / exe_name
        if exe.is_file():
            return str(exe)
    return shutil.which(name)


def _candidate_bin_dirs() -> list[Path]:
    """**按优先级**列出可能放着 ffmpeg/ffprobe 的目录（不判断里面有什么）。

    ① `deps/<dir>`（初始化环境下载的，用户可删）
    ② 包内 `_MEIPASS/bin` 与 `<exe>/bin`（兼容「构建期打进包」的老形态）
    """
    import sys

    dirs: list[Path] = []
    try:
        from backend.app.services.env_bootstrap_service import deps_root

        dirs.append(deps_root() / "ffmpeg")
    except Exception:  # noqa: BLE001 - 依赖服务不可用不该让抽帧崩
        pass

    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        dirs.extend([base / "bin", base.parent / "bin"])
    return dirs


def ffmpeg_available() -> bool:
    """**ffmpeg** 是否可用（抽帧的唯一硬依赖）。

    ⚠️ 判据只要 **ffmpeg**，不要 ffprobe（2026-10-23 修）：
    公共镜像的 ffmpeg-static 包**只含 ffmpeg**，不含 ffprobe；原实现要求两者都有
    ⇒ 明明能抽帧却被判「不可用」⇒ 视频直接不检测 ⇒ `ocr_ok=None` ⇒ 不勾选。
    ffprobe 缺失由 `probe_duration` 用 `ffmpeg -i` 兜底，不影响抽帧。

    不可用时**不静默降级**：视频回到「未验证」而不是被当成合规。
    """
    return _ffmpeg_exe("ffmpeg") is not None


def probe_duration(video: Path) -> float:
    """读视频时长（秒）；读不到 ⇒ 0.0（调用方按「无法抽帧」处理）。

    ⚠️⚠️ **ffprobe 缺失时必须用 ffmpeg 兜底**（2026-10-23 装包版实测根因）：
    - `deps/ffmpeg/` 里只有 `ffmpeg.exe` —— 公共镜像的 ffmpeg-static 包**不含 ffprobe**；
    - 原实现只调 ffprobe，取不到就 `return 0.0`；
    - `extract_frames` 见 `duration <= 0` 直接 `return []`；
    - ⇒ **一帧都不抽** ⇒ `ocr_ok=None` ⇒ 视频不勾选且无任何提示。

    兜底方式：`ffmpeg -i <file>` 把元信息打在 stderr 的 `Duration: HH:MM:SS.cc` 里，
    退出码是 **1**（因为没有输出文件），所以**不能**用 returncode 判成败。
    """
    #: ① 首选 ffprobe（输出干净，好解析）
    ffprobe = _ffmpeg_exe("ffprobe")
    if ffprobe:
        try:
            done = subprocess.run(
                [
                    ffprobe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(video),
                ],
                capture_output=True,
                text=True,
                timeout=FRAME_TIMEOUT_S,
            )
            value = float((done.stdout or "").strip() or 0.0)
            if value > 0:
                return value
        except Exception:  # noqa: BLE001 - 落到 ffmpeg 兜底
            pass

    #: ② 兜底：ffmpeg -i 解析 stderr 里的 Duration
    ffmpeg = _ffmpeg_exe("ffmpeg")
    if not ffmpeg:
        return 0.0
    try:
        done = subprocess.run(
            [ffmpeg, "-i", str(video)],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=FRAME_TIMEOUT_S,
        )
        #: ⚠️ 这里**不看 returncode**（ffmpeg -i 无输出文件必然回 1）
        m = re.search(
            r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", done.stderr or ""
        )
        if not m:
            return 0.0
        hours, minutes, seconds, centis = (int(g) for g in m.groups())
        return hours * 3600 + minutes * 60 + seconds + centis / 100.0
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
    #: **按「每 10 秒一帧」算实际抽帧数**（用户口径 2026-10-23），并用 `count` 封顶。
    #:
    #: `count` 现在的语义是**上限**（默认 `VIDEO_FRAMES_MAX`）：
    #:   · 8 秒视频 → ceil(8/10) = 1 帧（旧实现固定抽 6 帧，纯浪费）；
    #:   · 41 秒视频 → ceil(41/10) = 5 帧；
    #:   · 10 分钟视频 → 60 帧会被 `count` 夹到 20 帧（防 OCR 时间失控）。
    wanted = max(1, math.ceil(duration / VIDEO_FRAME_INTERVAL_S))
    #: ⚠️ **上限是「最多抽几帧」，不是「只抽前几秒」**（2026-10-23 口径「最高 50 帧，平分」）。
    #:
    #: 旧写法 `count = min(count, wanted)` 是截断：`wanted` 被 `count` 夹小之后，
    #: 下面的采样分母用的是夹小的 `count` —— 采样点仍均匀分布（`duration * (i+0.5)/count`），
    #: 所以**行为上确实是平分**，但可读性上容易被误读成「只抽前一段」。
    #: ⇒ 这里显式写成「取二者较小值 = 实际帧数，间隔按 `duration / 实际帧数` 重算」，
    #: 让「平分全片」这个语义一目了然。
    frames_to_take = max(1, min(int(count), wanted))

    #: **采样时刻 = 首帧 + 中间均匀 + 尾帧**（用户口径 2026-10-23「收尾各一帧固定」）。
    #:
    #: 首尾必抽的理由：片头常用于品牌/合规声明、片尾常用于促销标注，
    #: 都是违规词高发位置；「每段中点」在短视频上从 1/6 处才开始采，首尾各有盲区。
    #:
    #: ⚠️ 首尾取 `0.1s` / `duration-0.1s` 而不是 0 / duration：
    #:   端点上 ffmpeg 可能取不到帧（首帧黑场、末帧超出实际可解码范围）。
    #:   `0.1` 足够近、又能稳定解出画面。
    times: list[float] = []
    if frames_to_take == 1:
        times = [duration / 2]
    elif frames_to_take == 2:
        times = [min(0.1, duration / 2), max(duration - 0.1, duration / 2)]
    else:
        times.append(min(0.1, duration * 0.01))          # 首帧
        middle = frames_to_take - 2
        for i in range(middle):
            #: 中间 `middle` 个点均匀落在 (首, 尾) 之间，避开与首尾重叠
            times.append(duration * (i + 1) / (middle + 1))
        times.append(max(duration - 0.1, duration * 0.99))  # 尾帧

    workdir = Path(tempfile.mkdtemp(prefix="video_frames_"))
    out: list[tuple[float, str]] = []
    for index, at in enumerate(times):
        dest = workdir / f"frame{index:03d}.jpg"
        try:
            done = subprocess.run(
                [
                    #: ⚠️⚠️ **必须走 `_ffmpeg_exe()`**（2026-10-23 装包版实测根因）：
                    #: 原来裸写 `"ffmpeg"`，靠 PATH 解析；而 `ffmpeg_available()`
                    #: 用的是**定位后的路径** ⇒ 探测说「有」、实际执行找不到
                    #: ⇒ `FileNotFoundError` 被 `except` 吞掉 ⇒ `pairs` 为空
                    #: ⇒ `ocr_ok=None` ⇒ **视频不勾选且没有任何提示**。
                    #: 这就是「本地正常、打包版视频不勾选」的直接原因。
                    _ffmpeg_exe("ffmpeg") or "ffmpeg", "-v", "error",
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
