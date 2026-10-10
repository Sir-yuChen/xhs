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

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from backend.app.services.forbidden_word_service import find_hits, normalize

logger = logging.getLogger(__name__)

#: 随包分发的 PowerShell 脚本（纯 ASCII —— Windows PowerShell 按系统 ANSI
#: 码页读 .ps1，UTF-8 中文注释会被解析成乱码引号而语法报错，实测踩过）。
_SCRIPT_NAME = "win_ocr_json.ps1"
#: 批量脚本（一进程多图，消除 PowerShell 冷启动开销，见 `_run_batch`）。
_BATCH_SCRIPT_NAME = "win_ocr_batch.ps1"

#: 识别前的最大边长。超出的先缩再识别（Windows OCR 对超大图慢且吃内存），
#: 脚本会把坐标**乘回原图尺度**，所以红框位置不受影响。
MAX_DIM = 1600

#: 单图 OCR 超时（秒）。实测 0.6s；留足 8 倍余量给冷启动的大图。
PER_IMAGE_TIMEOUT = 30.0

#: 并发数。Windows OCR 是 CPU 密集但**进程内可并行**的；用户口径 2026-10-09
#: 要求 **5 个并发**处理 OCR（此前 4）。
#:
#: ⚠️ **这里不是「并发安全」的保证**：并发只在**重试**配合下才稳
#: （见 `RETRYABLE_CODES`）。5 是吞吐与争用的折中，不是「不会失败」。
MAX_WORKERS = 5

#: 瞬时失败（WinRT 并发争用）重试次数 + 退避基数（秒）。
#:
#: ⚠️ 见 `RETRYABLE_CODES` 与 `_run_script_with_retry` 的实测依据。
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 0.4

#: **可重试**的错误码 —— 全是 WinRT 并发争用的表征，不是「图坏了」。
#:
#: ⚠️ `OCR_FAILED` 是脚本 catch-all（含 WinRT `Wait` 异常）；它与
#: `OCR_NO_OUTPUT` 同源，必须一起重试。只重试前者会漏掉大半失败
#: （实测：端点预检里 `OCR_FAILED` 一片，失败率一度 97%）。
RETRYABLE_CODES: frozenset[str] = frozenset({"OCR_NO_OUTPUT", "OCR_FAILED"})

#: 单个数据包的媒体张数上限（防「用户丢了个 5000 张的目录」把内存吃光）。
MAX_IMAGES = 300


class OcrError(RuntimeError):
    """OCR 链路不可用 / 单图失败（**带 code**，路由据此给结构化错误）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def script_path() -> Path:
    """定位随包分发的 OCR 脚本（打包后与本模块同目录）。"""
    local = Path(__file__).with_name(_SCRIPT_NAME)
    if local.exists():
        return local
    fallback = Path(__file__).parent.parent / "bin" / _SCRIPT_NAME
    if fallback.exists():
        return fallback
    raise OcrError("OCR_SCRIPT_MISSING", f"找不到 OCR 脚本（{_SCRIPT_NAME}）")


def engine_available() -> bool:
    """OCR 引擎是否可用（**不识别任何图**，只探一次引擎）。

    用一个 1×1 的内存 PNG 试跑 —— 比解析 PowerShell 装配类型可靠，
    因为「类型没装载」和「引擎没装」都会在这里一次性暴露。
    """
    try:
        from PIL import Image
    except ImportError:
        return False
    tmp_dir = Path(tempfile.mkdtemp(prefix="ocr_probe_"))
    try:
        probe = tmp_dir / "probe.png"
        Image.new("RGB", (8, 8), "white").save(probe)
        return _run_script(probe) is not None
    except OcrError:
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _run_script(image: Path) -> dict[str, Any]:
    """跑一次 PowerShell 脚本，返回其 JSON。

    ⚠️ **每次调用必须用独立的输出文件**（第一版实测踩过的坑）：固定写
    ``%TEMP%/ocr_out.json`` 时，4 路并发互相删/覆盖对方的结果 ⇒ 68 张图里
    18 张误报 `OCR_NO_OUTPUT`。所以这里用 `uuid` 给每次调用一个专属路径。
    """
    script = script_path()
    out_file = Path(tempfile.gettempdir()) / f"ocr_out_{uuid.uuid4().hex}.json"
    #: ⚠️ **必须绝对路径**。PowerShell 子进程按**它自己的** cwd 解析相对
    #: 路径，实测踩到：`storage_dir` 是相对路径，暂存图路径原样传过去
    #: ⇒ `Test-Path` False ⇒ 每张图 `OCR_NO_OUTPUT`（同一张图、同一份代码，
    #: 换成绝对路径就 1/1 成功）。`staging_root()` 已 resolve 过一次，
    #: 这里再兜一层 —— 漏一处就复现一次，而症状（全部 OCR 失败）完全
    #: 不指向「路径是相对的」。
    image = Path(image).resolve()
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            str(image),
            str(MAX_DIM),
            str(out_file),
        ],
        capture_output=True,
        text=True,
        #: ⚠️ **必须显式给编码**（第一版实测踩到的坑）：Windows PowerShell 5.1
        #: 按 **GBK** 输出中文错误信息，而 `text=True` 的默认解码是 locale
        #: 编码（本机为 UTF-8）⇒ stderr 一解码就抛 `UnicodeDecodeError`，
        #: 在 reader 线程里炸掉，**看起来像随机崩溃**。
        #: 实际结果走 JSON 文件（UTF-8），这里只是读诊断信息 ⇒ 用
        #: `errors="replace"` 兜住非法字节，宁可看到乱码也不要抛异常。
        encoding="gbk",
        errors="replace",
        timeout=PER_IMAGE_TIMEOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if not out_file.exists():
        raise OcrError(
            "OCR_NO_OUTPUT",
            f"OCR 脚本没有产出结果（退出码 {proc.returncode}）：{(proc.stderr or '')[:200]}",
        )
    try:
        payload = json.loads(out_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise OcrError("OCR_BAD_JSON", f"OCR 结果不是合法 JSON：{error}") from error
    finally:
        out_file.unlink(missing_ok=True)

    if not payload.get("ok"):
        code = str(payload.get("error") or "OCR_FAILED")
        message = str(payload.get("message") or "OCR 识别失败")
        if code == "NO_ENGINE":
            raise OcrError(
                "OCR_ENGINE_MISSING",
                "本机没有可用的 Windows OCR 引擎（设置 > 时间和语言 > 语言 > 添加「光学字符识别」）",
            )
        raise OcrError(code, message)
    return payload


def batch_script_path() -> Path:
    """定位**批量** OCR 脚本（打包后与本模块同目录）。"""
    local = Path(__file__).with_name(_BATCH_SCRIPT_NAME)
    if local.exists():
        return local
    fallback = Path(__file__).parent.parent / "bin" / _BATCH_SCRIPT_NAME
    if fallback.exists():
        return fallback
    raise OcrError("OCR_SCRIPT_MISSING", f"找不到批量 OCR 脚本（{_BATCH_SCRIPT_NAME}）")


def _run_batch(pairs: list[tuple[str, Path]]) -> dict[str, dict[str, Any]]:
    """**一次 PowerShell 进程**识别多张图，返回 `{image_path: payload}`。

    ⚠️ **为什么批量**（实测 2026-10-09）：每次 `powershell -File` 冷启动
    ~1.2s，而真正 OCR 只 ~0.3s ⇒ 一图一进程时 **80% 时间花在进程启动上**。
    68 张图一进程跑 ~5s（对照 4 路并发逐图跑 ~30s+）。

    `pairs` = `[(uid, image_path)]`；脚本按 uid 写结果文件，这里再读回来。
    单图失败**不影响整批**（脚本逐图 try/catch，写各自的错误 JSON）。
    """
    script = batch_script_path()
    work = Path(tempfile.mkdtemp(prefix="ocr_batch_"))
    try:
        map_file = work / "mapping.tsv"
        out_dir = work / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        map_file.write_text(
            "\n".join(f"{uid}\t{Path(p).resolve()}" for uid, p in pairs),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(script), str(map_file), str(MAX_DIM), str(out_dir),
            ],
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="replace",
            timeout=PER_IMAGE_TIMEOUT * max(1, len(pairs)) + 60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        results: dict[str, dict[str, Any]] = {}
        for uid, path in pairs:
            out_file = out_dir / f"{uid}.json"
            if not out_file.exists():
                results[str(path)] = {
                    "ok": False, "error": "OCR_NO_OUTPUT",
                    "message": f"批量 OCR 未产出该图结果（进程退出码 {proc.returncode}）",
                }
                continue
            try:
                results[str(path)] = json.loads(out_file.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                results[str(path)] = {
                    "ok": False, "error": "OCR_BAD_JSON", "message": "结果不是合法 JSON",
                }
        return results
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
            "summary": {"total": 0, "ok": 0, "failed": 0, "violations": 0, "truncated": False},
        }
    if not engine_available():
        raise OcrError("OCR_ENGINE_MISSING", "本机没有可用的 Windows OCR 引擎")

    #: ① 批量路径（首选）：一次进程跑完，逐图回调进度。
    try:
        batch_script_path()
        batch_ok = True
    except OcrError:
        batch_ok = False

    if batch_ok:
        #: **分块并行批量**（实测 2026-10-09）：一进程跑完 68 张 7.1s；
        #: 拆成 N=MAX_WORKERS 块、每块一个批量进程并行跑到 3.8s。既省了
        #: 「一图一进程」的冷启动，又用上多核 ⇒ 综合最快。块内仍是批量脚本。
        n_chunks = min(MAX_WORKERS, len(paths))
        chunks = [paths[i::n_chunks] for i in range(n_chunks)]

        def _run_chunk(chunk: list[str]) -> dict[str, dict[str, Any]]:
            pairs = [(uuid.uuid4().hex, Path(p)) for p in chunk]
            return _run_batch(pairs)

        results: list[dict[str, Any]] = [{} for _ in paths]
        idx_by_path = {p: i for i, p in enumerate(paths)}
        with ThreadPoolExecutor(max_workers=n_chunks) as pool:
            for chunk, payloads in zip(chunks, pool.map(_run_chunk, chunks)):
                for original in chunk:
                    payload = (
                        payloads.get(str(Path(original).resolve()))
                        or payloads.get(str(original))
                        or {}
                    )
                    idx = idx_by_path[original]
                    if payload.get("ok"):
                        results[idx] = _payload_to_scan(original, payload, entries)
                    else:
                        results[idx] = {
                            "path": original, "name": os.path.basename(original),
                            "ocr_ok": False, "ocr_text": "", "hits": [],
                            "error": str(payload.get("error") or "OCR_FAILED"),
                            "error_message": str(payload.get("message") or "OCR 识别失败"),
                            "width": 0, "height": 0,
                        }
                    if on_one_done is not None:
                        on_one_done()
    else:
        #: ② 回退：逐图 + 线程池（旧路径）。
        results = [{} for _ in paths]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            future_to_idx = {
                pool.submit(_scan_one, path, entries): i for i, path in enumerate(paths)
            }
            for fut in as_completed(future_to_idx):
                idx = future_to_idx[fut]
                results[idx] = fut.result()
                if on_one_done is not None:
                    on_one_done()

    ok = sum(1 for r in results if r.get("ocr_ok"))
    return {
        "results": results,
        "summary": {
            "total": len(results),
            "ok": ok,
            "failed": len(results) - ok,
            "violations": sum(1 for r in results if r.get("hits")),
            "truncated": len(image_paths) > len(results),
        },
    }


def _run_script_with_retry(image: Path, attempts: int = RETRY_ATTEMPTS) -> dict[str, Any]:
    """跑 OCR 脚本，**瞬时失败重试**。

    ⚠️ **为什么要重试**（实测踩到，且我第一版的诊断是**错的**）：
    `Windows.Media.Ocr` 走 WinRT 异步 await。**并发的多个 PowerShell
    子进程**里，个别进程会在 `Wait` 上抛「发生一个或多个错误」。

    这里有个坑：我最初只在**独立脚本**里测并发（1/2/4/8 档 × 69 张都是
    0 失败），据此写下「并发安全、不用重试」的注释。**在应用进程内跑就
    翻车了** —— 真实端点预检 70 张图，`OCR_FAILED` 报错一大片。所以
    判据必须是**端点实测**，不是孤立脚本。

    两种码都重试（它们都是同一类瞬时争用，不是「图坏了」）：
    * `OCR_NO_OUTPUT` —— 子进程没产出文件；
    * `OCR_FAILED` —— 脚本 catch 到了 WinRT 异常并写进 JSON。

    「文件损坏 / 不是图片」走 `OCR_BAD_IMAGE`，重试多少次都一样 ⇒
    直接放行（不重试，避免把确定性错误拖成慢失败）。
    """
    last: OcrError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _run_script(image)
        except OcrError as error:
            last = error
            if error.code not in RETRYABLE_CODES:
                raise
            if attempt < attempts:
                time.sleep(RETRY_BACKOFF_S * attempt)
    assert last is not None  # pragma: no cover - attempts >= 1
    raise last


def _scan_one(path: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """单图：OCR → 匹配 → 定位红框。**异常不外泄**（转成 `ocr_ok=False`）。"""
    name = os.path.basename(path)
    try:
        payload = _run_script_with_retry(Path(path))
    except OcrError as error:
        return {
            "path": path, "name": name, "ocr_ok": False, "ocr_text": "",
            "hits": [], "error": error.code, "error_message": error.message,
            "width": 0, "height": 0,
        }
    except subprocess.TimeoutExpired:
        return {
            "path": path, "name": name, "ocr_ok": False, "ocr_text": "",
            "hits": [], "error": "OCR_TIMEOUT", "error_message": "识别超时",
            "width": 0, "height": 0,
        }
    return _payload_to_scan(path, payload, entries)


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


def build_table_from_boxes(words: list[dict[str, Any]]) -> tuple[list[str], list[list[str]]]:
    """把词 box 按**几何位置**聚成行列网格（纯函数，便于单测）。

    返回 `(columns, rows)`；`columns` 是 `["属性", "值", ...]` 这种**占位列名**
    （图上没有真实表头时用序号；有表头行则用首行文字）。

    算法：
      1. 行高 = 所有词高度的中位数（挡住个别放大字）；
      2. **聚行**：按中心 y 排序后顺序扫，与当前行中心差 < 行高 × 0.6 归同行；
      3. **聚列**：同一行内按 x 排序，相邻词左边界 - 前词右边界 > 行高 × 1.2
         则断列 ⇒ 该行分成若干「格」；
      4. **对齐**：以格数最多的行定列数，按**格中心 x** 就近吸附到列（容差 = 行高 × 2），
         吸不到的格单独占位；缺位补空串。

    ⚠️ 不猜语义：只做几何对齐，值原样透出（用户可在新页签里改）。
    """
    if not words:
        return [], []

    heights = sorted(w["h"] for w in words if w["h"] > 0)
    if not heights:
        return [], []
    unit = heights[len(heights) // 2] or 1.0

    #: ① 聚行
    ordered = sorted(words, key=lambda w: w["y"] + w["h"] / 2)
    line_rows: list[list[dict[str, Any]]] = []
    for w in ordered:
        cy = w["y"] + w["h"] / 2
        if line_rows:
            prev = line_rows[-1]
            prev_cy = sum(x["y"] + x["h"] / 2 for x in prev) / len(prev)
            if abs(cy - prev_cy) < unit * 0.6:
                prev.append(w)
                continue
        line_rows.append([w])

    #: ② 每行聚列（按水平间隙断格）
    gap_limit = unit * 1.2
    grid: list[list[dict[str, Any]]] = []
    for row in line_rows:
        cells: list[dict[str, Any]] = []
        cur: list[dict[str, Any]] = []
        last_right = None
        for w in sorted(row, key=lambda w: w["x"]):
            if last_right is not None and w["x"] - last_right > gap_limit:
                cells.append(_merge_cell(cur))
                cur = []
            cur.append(w)
            last_right = w["x"] + w["w"]
        if cur:
            cells.append(_merge_cell(cur))
        grid.append(cells)

    if not grid:
        return [], []

    #: ③ 定列数 + 按中心 x 吸附
    ncols = max(len(r) for r in grid)
    if ncols == 1:
        #: 单列：就是纯文本行，按行直出
        return ["内容"], [[c["text"]] for r in grid for c in r]

    #: 用「格数最多」的行做列锚点
    anchor_row = max(grid, key=len)
    col_centers = [c["cx"] for c in anchor_row]
    tol = unit * 2

    out_rows: list[list[str]] = []
    for cells in grid:
        line = [""] * ncols
        for c in cells:
            idx = min(range(ncols), key=lambda i: abs(col_centers[i] - c["cx"]))
            if abs(col_centers[idx] - c["cx"]) > tol:
                continue  #: 吸不到就不硬塞
            line[idx] = (line[idx] + " " + c["text"]).strip() if line[idx] else c["text"]
        out_rows.append(line)

    columns = [f"列{i + 1}" for i in range(ncols)]
    return columns, out_rows


def build_kv_from_boxes(words: list[dict[str, Any]]) -> list[dict[str, str]]:
    """把属性图词 box 还原成 **KV 列表**（用户口径 2026-10-09）。

    属性图是「属性名 | 属性值」成对的参数表（可能并排多组 ⇒ 一张图 2~4 对/行）。
    与 `build_table_from_boxes` 的区别：那边输出**网格**，这边输出**配对**：

      1. 同样按 y 聚行、按水平间隙断格（复用同一套几何）；
      2. 每行内**相邻两格配成一对**（1-2、3-4…）；
      3. 属性名去 OCR 的**字间空格**（`带 妆 镜` → `带妆镜`）；
      4. 奇数个格时，末格单独成项（值留空）。

    返回 `[{"key": "品牌", "value": "WickDynasty…"}, ...]`（按阅读顺序）。
    """
    if not words:
        return []

    heights = sorted(w["h"] for w in words if w["h"] > 0)
    if not heights:
        return []
    unit = heights[len(heights) // 2] or 1.0

    ordered = sorted(words, key=lambda w: w["y"] + w["h"] / 2)
    line_rows: list[list[dict[str, Any]]] = []
    for w in ordered:
        cy = w["y"] + w["h"] / 2
        if line_rows:
            prev = line_rows[-1]
            prev_cy = sum(x["y"] + x["h"] / 2 for x in prev) / len(prev)
            if abs(cy - prev_cy) < unit * 0.6:
                prev.append(w)
                continue
        line_rows.append([w])

    gap_limit = unit * 1.2
    pairs: list[dict[str, str]] = []
    for row in line_rows:
        cells: list[dict[str, Any]] = []
        cur: list[dict[str, Any]] = []
        last_right = None
        for w in sorted(row, key=lambda w: w["x"]):
            if last_right is not None and w["x"] - last_right > gap_limit:
                cells.append(_merge_cell(cur))
                cur = []
            cur.append(w)
            last_right = w["x"] + w["w"]
        if cur:
            cells.append(_merge_cell(cur))

        #: 相邻两格配一对
        for i in range(0, len(cells), 2):
            key = _squeeze(cells[i]["text"])
            value = _squeeze(cells[i + 1]["text"]) if i + 1 < len(cells) else ""
            if not key and not value:
                continue
            pairs.append({"key": key, "value": value})
    return pairs


def _squeeze(text: str) -> str:
    """OCR 常在汉字间插空格（`带 妆 镜`）—— 去掉**汉字之间**的空格。"""
    out: list[str] = []
    for i, ch in enumerate(text):
        if ch == " " and out and i + 1 < len(text):
            prev, nxt = out[-1], text[i + 1]
            if "\u4e00" <= prev <= "\u9fff" and "\u4e00" <= nxt <= "\u9fff":
                continue
        out.append(ch)
    return "".join(out).strip()


def _merge_cell(words: list[dict[str, Any]]) -> dict[str, Any]:
    """把同一格内的词合并（按 x 拼文本，带中心 x 便于列对齐）。"""
    ws = sorted(words, key=lambda w: w["x"])
    text = " ".join(str(w["text"]) for w in ws if str(w["text"]).strip()).strip()
    x0 = min(w["x"] for w in ws)
    x1 = max(w["x"] + w["w"] for w in ws)
    return {"text": text, "cx": (x0 + x1) / 2}


def scan_attribute_table(path: str | Path) -> dict[str, Any]:
    """OCR「商品属性图」→ 还原成**行列网格**（用户口径 2026-10-09）。

    千帆的属性图是一张**参数表截图**（左边「属性名」、右边「属性值」，可能多列对）。
    Windows OCR 只给逐词 box，所以这里按几何重建表格：

    1. 取所有词 box（`lines[].words[]`，含 x/y/w/h）；
    2. **按 y 聚行**：中心 y 相近（< 行高 × 0.6）的归同一行；
    3. **按 x 聚列**：把同一行内的词按 x 排序，相邻词若水平间隙
       超过「列间隙阈值」则断列 —— 阈值取整图文字高度的 ~1.2 倍；
    4. 以**列数最多的行**为准补齐短行（缺的补空串，保证矩形）。

    ⚠️ **猜不出就说猜不出**：识别失败 / 没有词 → 返回 `ok=False` + `reason`，
    不伪造空表（前端据此提示「未识别出表格」）。
    """
    try:
        payload = _run_script_with_retry(Path(path))
    except OcrError as error:
        return {"ok": False, "reason": error.message, "columns": [], "rows": []}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "识别超时", "columns": [], "rows": []}

    words: list[dict[str, Any]] = []
    for line in (payload.get("lines") or []):
        for w in (line.get("words") or []):
            if not isinstance(w, dict):
                continue
            try:
                words.append({
                    "text": str(w.get("text") or ""),
                    "x": float(w["x"]), "y": float(w["y"]),
                    "w": float(w["w"]), "h": float(w["h"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
    if not words:
        return {"ok": False, "reason": "图上没有识别到文字", "columns": [], "rows": []}

    columns, rows = build_table_from_boxes(words)
    kv = build_kv_from_boxes(words)
    if not rows and not kv:
        return {"ok": False, "reason": "无法从识别结果还原出行列",
                "columns": [], "rows": [], "kv": []}
    return {"ok": True, "reason": "", "columns": columns, "rows": rows, "kv": kv}


__all__ = [
    "FRAME_MAX_WIDTH",
    "MAX_DIM",
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
    "build_kv_from_boxes",
    "build_table_from_boxes",
    "scan_attribute_table",
    "scan_images",
    "scan_video",
    "script_path",
]
