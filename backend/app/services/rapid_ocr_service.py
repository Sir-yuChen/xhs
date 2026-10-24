"""RapidOCR 3.x（PP-OCRv5 ONNX，纯 CPU）识别后端 —— 图片违规词检测的主引擎。

## 为什么仍是 RapidOCR、为什么升到 v5（实测依据 2026-10-20，同一包 118 张真图）

| 配置 | 单图 | 118 张 |
|---|---|---|
| 旧：`rapidocr_onnxruntime` 1.x + PP-OCRv4 mobile | 1.27s | ~150s |
| **新：`rapidocr` 3.10 + PP-OCRv5 mobile（本模块）** | **0.81~1.05s** | **~95~125s** |
| `rapidocr` 3.10 + PP-OCRv5 + DirectML（Intel 核显） | 0.81s | 与 CPU **完全相同** |
| PP-OCRv5 server | 28.5s/图 | 不可用（32 倍） |
| PP-OCRv6 | — | 官方无 `v6 + ch + mobile` 检测模型，构造即 ValueError |

⇒ **只走 CPU（onnxruntime）**，不引入 `onnxruntime-directml`（与 CPU 版同名互斥、
+25MB、实测零收益）。这是「普通电脑装得上」的前提（见 requirements.txt 注释）。

## 精度换算（为 1.5x 换模型值得吗？）

同一批 118 张，v5 对 v4：21 张完全一致、97 张有差异、差异图平均字符相似度 **0.929**；
**v5 有字而 v4 空 = 1 张，反向 = 0 张**；字符 +2%、行 +1.4% ⇒ 违规词召回**不降**。
差异集中在拉丁艺术字（`EDOUARD VUILLARD` 字序），不影响中文词命中。

## 与上层（`image_ocr_service`）的接口

返回与旧实现**同形**的 payload：
`{ok, text, lines: [{text, words: [{text,x,y,w,h}]}], pixel_width, pixel_height}`。

⚠️ RapidOCR 返回的是**行级 4 点 box**（`[[x1,y1],[x2,y1],[x2,y2],[x1,y2]]`），没有词切分
⇒ 这里把每行**整行当一个"词"**。红框因此是**行框**（旧实现同样如此；
`image_ocr_service.locate_hits` 在行内按字符均分做字符级映射）。
这条形态必须保持，否则红框会变成全图框或直接消失（有守卫钉住）。

⚠️ `pixel_width/height` 必须回**原图**尺寸：上层用它做红框坐标换算，而识别发生在
缩放后的图上（见 `_load_image`）。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: 本机 OCR 模型的下载/缓存目录（RapidOCR 3.x 首次使用会把 onnx 下到这里）。
#:
#: ⚠️ 为什么显式指定：默认目录是 `site-packages/rapidocr/models`，而本项目的
#: `rapidocr` 是 `pip install --user` 装在**用户级** site-packages（程序目录无写权限）。
#: 装在谁家取决于当时的 pip 方式 ⇒ 构建机（PyInstaller）与运行机可能看到不同的
#: 模型目录。固定到仓库内 `.hermes/tmp/ocr_models` 之外的地方会污染用户盘，
#: 所以放在**用户缓存目录**，两边都能读写。
def _model_dir() -> Path:
    """OCR 模型目录 —— **转发统一出入口** `env_bootstrap_service.dep_model_dir()`。

    ⚠️ 2026-10-23 修（用户口径「全部统一出入口」+「下载好依赖，应实时生效」）：
    原来这里写死 `~/.cache/xhs/ocr_models`，而「初始化环境」下载器把模型放在
    `<exe同级>/deps/ocr_models/`（装包态）或 `.hermes/deps/ocr_models/`（开发态）
    ⇒ **两处路径不是同一个地方** ⇒ 用户看到「下载完成、可预检仍报模型缺失」，
    而且必须重启才能看到目录变化。
    ⇒ 现在只有 `dep_model_dir()` 一处定义，两侧共用，每次调用重新取。

    （历史说明保留：为什么当初要显式指定 —— 默认目录是
    `site-packages/rapidocr/models`，而 `rapidocr` 可能是 `pip install --user`
    装在用户级 site-packages，构建机与运行机看到的不是同一份。）
    """
    try:
        from backend.app.core.env_bootstrap import dep_model_dir

        return dep_model_dir("ocr_models")
    except Exception:  # noqa: BLE001 - 依赖服务不可用时退回用户缓存目录
        base = Path.home() / ".cache" / "xhs" / "ocr_models"
        base.mkdir(parents=True, exist_ok=True)
        return base

#: 识别前的最长边。与 `image_ocr_service.MAX_DIM` 同值。
#:
#: ⚠️ **别再靠降分辨率提速 —— 已证伪**（2026-10-20，118 张）：缩长边到
#: 1024 / 768 / 640 与用原图相比，单图耗时 1.26~1.30s，**没有差别**
#: （PP-OCR 检测网络自己会把长边归一化到 ~960，我们缩只是白做一次 LANCZOS）。
#: 保留本常量的理由只剩两条：① 超大图（4000px）避免一次性申请大 ndarray；
#: ② 缩放不丢字（促销图有效字高 > 40px，1600 长边后仍 > 16px）。
MAX_DIM = 1600

#: 识别置信度下限。低于它的行**丢弃** —— RapidOCR 对纯装饰花纹会吐出
#: 高置信度的乱码行，这类噪声会污染 `ocr_text` 并可能在包含匹配下误命中。
MIN_SCORE = 0.5

#: 模型代际与规格 —— **换模型的唯一改点**（`_engine_params` 从这里派生）。
#:
#: ## 实测阶梯（2026-10-20/21，52 张 SKU 图、8 路、本机 8 逻辑核）
#:
#: | 配置 | 单图 | 换算 118 张 | 识别行数 | 词库命中 |
#: |---|---|---|---|---|
#: | PP-OCRv5 mobile | 1.07~1.12s | 126~132s | 985 | 6 |
#: | **PP-OCRv6 small** | **0.66~0.68s** | **78~81s** | 989 | 6 |
#: | **PP-OCRv6 tiny** | **0.23s** | **28s** | 993 | 6 |
#: | PP-OCRv6 medium | 8.22s | 970s | 1000 | — |
#:
#: ## 为什么选 **v6 + tiny**
#:
#: ① **产能**：tiny 比 v5 mobile 快 **4.7×**，且全包 118 张的识别行数 **1547 vs 1528**
#:    （不降反升）、字数 9228 vs 9037 ⇒ **不漏检**；
#: ② **精度对违规词无损**：全包按真实词库（淘宝/天猫/超级88/88VIP）匹配，
#:    **三档命中完全相同（6 条、同样的图）**；逐张相似度 tiny 0.948（v6 small 0.932，
#:    tiny 反而更接近 v5）；
#: ③ **体积更小**：tiny det+rec = **6.3MB**，v5 mobile = 21.4MB ⇒ 装包更小；
#: ④ small 保留为「精度优先」档（`MODEL_TYPE_NAME = "small"` 一行即切）。
#:
#: ⚠️ **唯一损失**：`1比1主图_2.jpg` 上 v5 认出孤立字符「11」，tiny/small 都认不出
#: （类似水印样的两个数字）。这类「两三个字符的孤立数字」不在任何违规词里，
#: 但要知道它存在 —— 若要覆盖，切 `MODEL_TYPE_NAME = "small"`（+23ms/张）。
#:
#: ⚠️ **medium 档不可用**：实测 8.22s/图（比 v5 慢 7 倍），它是给 GPU/服务器设计的。
#:
#: ⚠️ **OpenVINO 后端在本项目不可用**：`rapidocr` 的 OpenVINO 引擎是**单会话**，
#: 多线程调用直接 `RuntimeError: Infer Request is busy`（实测），而我们的并发模型
#: 就是多路线程池 ⇒ 想用 OpenVINO 必须「一进程一路」或进程池，复杂度远超收益。
#: 官方宣称的 5.2× 也是基于 OpenVINO，纯 ONNX Runtime 上 v6 的提速来自模型本身。
OCR_VERSION_NAME = "PP-OCRv6"
MODEL_TYPE_NAME = "tiny"


class RapidOcrUnavailable(RuntimeError):
    """RapidOCR 不可用（没装 / 模型缺失 / 下载失败）。上层据此给结构化错误。"""


#: 引擎**进程级单例**。构造要装配 det/cls/rec 三段 ONNX（首次约 1~5s，含模型文件
#: 读取与首次推理预热），每张图构造一次会让批量识别退化成「一图一装载」。
#: 线程安全：`_engine_lock` 只保护首次构造，识别本身由上层线程池并发。
_engine: Optional[Any] = None
_engine_lock = threading.Lock()

#: onnxruntime **每路**推理线程数。
#:
#: ⚠️ 保留这条纪律（2026-10-13 实测）：onnxruntime 默认每路吃满所有核，而上层用
#: **N 路线程池**并行 ⇒ N × 核数 个线程抢 核数 个核（8 核 × 8 路 = 64 线程抢 8 核），
#: 实测把 118 张拖到 141.9s。并行度必须只由**我们的线程池**持有。
#: ⚠️ 2026-10-20：RapidOCR 3.x 的对应键是 `Global.intra_op_num_threads`。
_INTRA_OP_THREADS = 1


def _engine_params() -> dict[str, Any]:
    """构造参数（det/cls/rec 三段的引擎、版本、规格一起给）。

    ⚠️ 三段都要显式给版本 —— 只给 Rec 的话 Det 会落回该包默认代际，
    出现「识别用 v5、检测用 v4」的混搭（官方模型表不保证两家配套）。
    """
    from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion

    #: `"PP-OCRv6"` → `OCRVersion.PPOCRV6`（去掉连字符再并上 `OCR` 前缀）。
    version = getattr(OCRVersion, OCR_VERSION_NAME.replace("-", "").upper())
    model_type = getattr(ModelType, MODEL_TYPE_NAME.upper())
    return {
        "Det.engine_type": EngineType.ONNXRUNTIME,
        "Det.ocr_version": version,
        "Det.model_type": model_type,
        "Det.lang_type": LangDet.CH,
        "Rec.engine_type": EngineType.ONNXRUNTIME,
        "Rec.ocr_version": version,
        "Rec.model_type": model_type,
        "Rec.lang_type": LangRec.CH,
        "Cls.engine_type": EngineType.ONNXRUNTIME,
        #: ⚠️ 线程数在 `EngineConfig.onnxruntime` 下，**不是** `Global.*`
        #: （3.10 的 config.yaml 里没有 `Global.intra_op_num_threads`，写错会
        #: 直接 `ValueError: Global.intra_op_num_threads is not a valid key`）。
        "EngineConfig.onnxruntime.intra_op_num_threads": _INTRA_OP_THREADS,
        "Global.log_level": "error",
        #: 模型目录固定到用户缓存（见 `_model_dir` 注释：用户级 site-packages
        #: 的默认目录在装包版里不可写，且构建机/运行机不一致）。
        "Global.model_root_dir": str(_model_dir()),
        #: ⚠️ 不给 `Global.max_side_len` —— 缩放由 `_load_image` 统一做。
        #: 两处都缩会让坐标映射多一层、且实测无收益。
    }


def _get_engine() -> Any:
    """惰性构造**单例**引擎（首次约 1~5s，之后复用）。"""
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:  # 双检：等锁期间别人可能已构造好
            return _engine
        try:
            from rapidocr import RapidOCR
        except ImportError as error:
            raise RapidOcrUnavailable(f"未安装 rapidocr：{error}") from error
        try:
            _engine = RapidOCR(params=_engine_params())
        except Exception as error:  # pragma: no cover - 模型文件损坏 / 无网络下载
            raise RapidOcrUnavailable(f"RapidOCR 初始化失败：{error}") from error
        return _engine


def engine_available() -> bool:
    """引擎可用？（**不识别任何图**，只构造一次）。

    首次调用会真的加载模型（约 1~5s）；之后是单例命中，纳秒级。
    """
    try:
        _get_engine()
        return True
    except RapidOcrUnavailable:
        return False
    except Exception as error:  # pragma: no cover - 兜住任何模型层异常
        logger.warning("RapidOCR 探测失败：%s", error)
        return False


def _load_image(path: Path) -> tuple[Any, int, int]:
    """读图 → `(ndarray, 原图宽, 原图高)`。

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


def _rows_from_result(result: Any, width: int, height: int) -> dict[str, Any]:
    """把 `RapidOCROutput` 转成上层认的行/词结构（**纯函数**，可单测）。

    3.x 的输出是并列三元组：`result.txts`（文本）、`result.boxes`（行级 4 点
    ndarray）、`result.scores`（置信度）。三者按下标一一对应；任一取不到
    该行整条丢弃（宁可少框，不框错位置）。
    """
    lines: list[dict[str, Any]] = []
    txts = getattr(result, "txts", None) or ()
    boxes = getattr(result, "boxes", None)
    scores = getattr(result, "scores", None) or ()
    for index, raw_text in enumerate(txts):
        try:
            box = boxes[index]
            score = float(scores[index])
        except (IndexError, TypeError, ValueError):
            continue
        text = str(raw_text or "").strip()
        if not text or score < MIN_SCORE:
            continue
        try:
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
        except (IndexError, TypeError, ValueError):
            continue
        x, y = min(xs), min(ys)
        w, h = max(xs) - x, max(ys) - y
        if w <= 0 or h <= 0:
            continue
        #: 整行当一个"词"（RapidOCR 不做词切分，见模块 docstring）。
        lines.append(
            {"text": text, "words": [{"text": text, "x": x, "y": y, "w": w, "h": h}]}
        )
    return {
        "ok": True,
        "text": "\n".join(line["text"] for line in lines),
        "lines": lines,
        "pixel_width": width,
        "pixel_height": height,
    }


def recognize(path: Path) -> dict[str, Any]:
    """识别单张图，返回与旧 Windows OCR 脚本**同形**的 payload。

    `{ok, text, lines: [{text, words: [{text,x,y,w,h}]}], pixel_width, pixel_height}`
    识别不出内容时 `text=""` 且 `lines=[]`（**不是错误** —— 纯色图合法地没有文字，
    上层按「未检出」处理，与「检查失败」区分）。
    """
    engine = _get_engine()
    image, width, height = _load_image(Path(path))

    try:
        result = engine(image)
    except Exception as error:  # pragma: no cover - onnxruntime 内部异常
        raise RapidOcrUnavailable(f"RapidOCR 识别失败：{error}") from error

    return _rows_from_result(result, width, height)


__all__ = [
    "MAX_DIM",
    "MIN_SCORE",
    "MODEL_TYPE_NAME",
    "OCR_VERSION_NAME",
    "RapidOcrUnavailable",
    "engine_available",
    "recognize",
]
