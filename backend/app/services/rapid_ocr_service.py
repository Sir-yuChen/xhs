"""RapidOCR（PP-OCRv4 ONNX，纯 CPU）识别后端 —— 图片违规词检测的主引擎。

## 为什么换掉 Windows OCR（实测依据 2026-10-13）

同一张促销艺术字图（`image_7e7c2b.png`）两家跑出来：

```
Windows OCR:  壬区天ll预售0产只花"1元定锁专和00哑〕,`0,15日20点付定金《免定金1元垆¥0℃1抢…
RapidOCR:     编织王国×天猫双11预售 只花0.01元预定解锁专属福利 立即抢购> 10.15日20点付定金…
```

| 词 | Windows OCR | RapidOCR |
|---|---|---|
| 天猫 | ❌（识成 `天ll`） | ✅ |
| 35% | ❌（整块丢失） | ✅ |
| 双11预售 | ❌ | ✅ |

⚠️ **预处理救不了 Windows OCR**：实测反相 / 灰度反相 / 放大 2x / 放大 2x+反相 /
对比度 2x 六种变体，9 个探针词的命中数**一个都没提升**（最好与原图持平 6/9）。
根因是它对中文艺术字/促销排版基本失能 —— 那是**引擎能力边界**，不是参数问题。

**代价**：单图 0.95s → 3.34s（慢 ~3.5x），随包体积 +约 20MB（onnxruntime + 模型）。
预检是「点一次等一会儿」的操作，这个交换是划得来的。

## 与 Windows OCR 的接口对齐

上层（`_payload_to_scan` / `locate_hits`）吃的是**行 + 词**结构：
`{text, lines:[{text, words:[{text,x,y,w,h}]}], pixel_width, pixel_height}`。

RapidOCR 返回的是**行级 4 点 box**（`[[x1,y1],[x2,y1],[x2,y2],[x1,y2]]`），没有词切分
⇒ 这里把每行**整行当一个"词"**（`words` 只有一项）。红框因此是**行框**而不是词框 ——
对「命中违规词」的展示完全够用（用户要看的是"哪一行有问题"），且省掉一次分词。

⚠️ `pixel_width/height` 必须回**原图**尺寸：上层用它做红框坐标换算，而识别可能
在缩放后的图上做（见 `_load_image`）。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: 识别前的最长边。与 Windows OCR 路径同值（`image_ocr_service.MAX_DIM`）。
#:
#: ⚠️ **RapidOCR 对超大图慢且吃内存**（它是检测+识别两段网络）。缩到 1600 长边后
#: 实测单图 ~3.3s；不缩时 4000px 的详情图会到 10s+ 且内存翻倍。
#: 缩放不丢字：促销图的有效字高一般 > 40px，1600 长边后仍 > 16px。
MAX_DIM = 1600

#: 识别置信度下限。低于它的行**丢弃** —— RapidOCR 对纯装饰花纹会吐出
#: 高置信度的乱码行，这类噪声会污染 `ocr_text` 并可能在词库包含匹配下误命中。
MIN_SCORE = 0.5


class RapidOcrUnavailable(RuntimeError):
    """RapidOCR 不可用（没装 / 模型缺失）。上层据此给结构化错误。"""


#: 引擎**进程级单例**。RapidOCR 的构造要加载两个 ONNX 模型（~30ms + 内存），
#: 每张图构造一次会让批量识别退化到「一图一装载」。线程安全：`_lock` 只保护
#: 首次构造，识别本身（`engine(...)`）由 onnxruntime 内部并发管理。
_engine: Optional[Any] = None
_engine_lock = threading.Lock()

#: onnxruntime **每路**推理线程数。
#:
#: ⚠️ 必须设 1（实测 2026-10-13）：onnxruntime 默认每路吃满所有核，而上层用
#: **N 路线程池**并行 ⇒ N × 核数 个线程抢 核数 个核（8 核 × 8 路 = 64 线程抢 8 核）。
#: 实测后果：包 A 的 118 张图跑 **141.9s**，远达不到 30s 目标；串行基线却是
#: 118 × ~2.6s ≈ 307s ⇒ 并发只加速了 2.2x（8 路本该接近 8x）。
#: 设 `intra_op_num_threads=1` 后由**我们的线程池**独占并行度，总线程数 ≈ 路数，
#: 不再互相抢核。
_INTRA_OP_THREADS = 1


def _get_engine() -> Any:
    """惰性构造**单例**引擎（首次调用约 0.3s，之后复用）。"""
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:  # 双检：等锁期间别人可能已构造好
            return _engine
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as error:
            raise RapidOcrUnavailable(f"未安装 rapidocr-onnxruntime：{error}") from error
        try:
            _engine = RapidOCR(
                #: ⚠️ `intra_op_num_threads` 必须显式给 1 —— RapidOCR 的默认
                #: params 里它是 0（= onnxruntime 自选，通常吃满核）。见上注。
                intra_op_num_threads=_INTRA_OP_THREADS,
            )
        except TypeError:
            #: 旧版 RapidOCR 不吃这个关键字 ⇒ 退回默认（慢，但能跑）。
            _engine = RapidOCR()
        except Exception as error:  # pragma: no cover - 模型文件损坏时才走到
            raise RapidOcrUnavailable(f"RapidOCR 初始化失败：{error}") from error
        return _engine


def engine_available() -> bool:
    """引擎可用？（**不识别任何图**，只构造一次）。

    首次调用会真的加载模型（约 0.3s）；之后是单例命中，纳秒级。
    """
    try:
        _get_engine()
        return True
    except RapidOcrUnavailable:
        return False
    except Exception as error:  # pragma: no cover - 兜住任何模型层异常
        logger.warning("RapidOCR 探测失败：%s", error)
        return False


def _load_image(path: Path) -> tuple[Optional[Any], int, int]:
    """读图 → `(ndarray 或 None, 原图宽, 原图高)`。

    超过 `MAX_DIM` 的先等比缩到长边 `MAX_DIM`。返回的宽高**永远是原图**的
    （上层拿它做红框换算，给缩放后的尺寸会让框整体缩小）。
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError as error:
        raise RapidOcrUnavailable(f"缺少图像依赖：{error}") from error

    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            width, height = img.size
            if max(width, height) > MAX_DIM:
                scale = MAX_DIM / max(width, height)
                img = img.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    Image.LANCZOS,
                )
            #: ⚠️ 必须 `copy()`：`np.array(img)` 在 `with` 退出（img 被关）后
            #: 底层缓冲可能失效，实测偶尔读出全黑图 ⇒ OCR 返回空。
            return np.array(img).copy(), width, height
    except (OSError, ValueError) as error:
        raise RapidOcrUnavailable(f"读图失败（{path.name}）：{error}") from error


def recognize(path: Path) -> dict[str, Any]:
    """识别单张图，返回与 Windows OCR 脚本**同形**的 payload。

    `{ok, text, lines: [{text, words: [{text,x,y,w,h}]}], pixel_width, pixel_height}`
    识别不出内容时 `text=""` 且 `lines=[]`（**不是错误** —— 纯色图合法地没有文字，
    上层按「未检出」处理，与「检查失败」区分）。
    """
    engine = _get_engine()
    image, width, height = _load_image(Path(path))

    try:
        #: `use_det/use_cls/use_rec` 全默认 True；`elapse` 是各阶段耗时，无用途。
        raw, _elapse = engine(image)
    except Exception as error:  # pragma: no cover - onnxruntime 内部异常
        raise RapidOcrUnavailable(f"RapidOCR 识别失败：{error}") from error

    lines: list[dict[str, Any]] = []
    for item in raw or []:
        try:
            box, text, score = item[0], str(item[1]), float(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        text = text.strip()
        if not text or score < MIN_SCORE:
            continue
        try:
            xs = [float(pt[0]) for pt in box]
            ys = [float(pt[1]) for pt in box]
        except (IndexError, TypeError, ValueError):
            continue
        x, y = min(xs), min(ys)
        w, h = max(xs) - x, max(ys) - y
        if w <= 0 or h <= 0:
            continue
        #: 整行当一个"词"（RapidOCR 不做词切分，见模块 docstring）。
        lines.append(
            {
                "text": text,
                "words": [{"text": text, "x": x, "y": y, "w": w, "h": h}],
            }
        )

    return {
        "ok": True,
        "text": "\n".join(line["text"] for line in lines),
        "lines": lines,
        "pixel_width": width,
        "pixel_height": height,
    }


__all__ = [
    "MAX_DIM",
    "MIN_SCORE",
    "RapidOcrUnavailable",
    "engine_available",
    "recognize",
]
