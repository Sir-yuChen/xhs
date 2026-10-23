"""PP-OCRv6(tiny) 接入的守卫（2026-10-20 立，2026-10-21 改档到 v6 tiny）。

## 为什么这组守卫必须存在

本轮换引擎改了三处**「本地能跑、装包版会炸」**的东西：

1. `rapid_ocr_service` 从 `rapidocr_onnxruntime`(1.x) 换成 `rapidocr`(3.x)——
   3.x 的**输出结构完全不同**（`RapidOCROutput.txts/boxes/scores` vs 旧的三元组
   `(box, text, score)`），转换写错不会报错，只会**红框消失/错位**；
2. `requirements.txt` 与 `packaging/SpiderXHS.spec` 都得跟着换包名，漏一处 ⇒
   装包版点「预检」500（1.0.4 已经栽过一次）；
3. 并发口径两轮都算错（`parallelism` 一直停在配置值 5），必须把「饱和点优先、
   配置保底」这个结论钉住，否则下次微调又把 118 张压回 5 路。

判据一律读**真实代码/真实行为**，不做「函数存在」这类空转断言。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.app.services import image_ocr_service as ocr
from backend.app.services import rapid_ocr_service as rapid



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
SERVICE = ROOT / "backend/app/services/rapid_ocr_service.py"
IMAGE_OCR = ROOT / "backend/app/services/image_ocr_service.py"
REQUIREMENTS = ROOT / "requirements.txt"
SPEC = ROOT / "packaging/SpiderXHS.spec"


# --------------------------------------------------------------------------- #
# 1. 引擎版本与后端选择
# --------------------------------------------------------------------------- #


def _requirement_lines(text: str) -> list[str]:
    """只取**生效的依赖行**（剥掉注释与空行）。

    ⚠️ 必须剥注释：`requirements.txt` 里刻意写着「不引入 onnxruntime-directml」
    之类的**说明文字**，按子串匹配会把自己的注释当成依赖（本轮真踩）。
    """
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def test_engine_targets_pp_ocr_v6_tiny_on_cpu():
    """模型代际/规格是**常量**（换模型只改一处），且构造参数真的用上了它。

    2026-10-21 定档 **PP-OCRv6 + tiny**：52 张 SKU 图实测 0.23s/图（v5 mobile 1.07s，
    快 4.7×），全包识别行数 1547 > v5 的 1528、词库命中完全相同（见模块 docstring）。
    """
    assert rapid.OCR_VERSION_NAME == "PP-OCRv6", "模型代际不应被悄悄改回 v5"
    assert rapid.MODEL_TYPE_NAME in {"tiny", "small"}, (
        "只允许 tiny/small：medium 实测 8.22s/图（比 v5 还慢 7 倍）"
    )

    params = rapid._engine_params()
    #: det / rec 两段必须同代际 —— 只给 rec 会出现「检测用 v5、识别用 v6」混搭。
    #: ⚠️ 取 `.value`：rapidocr 的枚举 `str(ModelType.TINY)` 是 `ModelType.TINY`，
    #: 直接 str() 比较会永远不等（本轮真踩）。
    for task in ("Det", "Rec"):
        version = getattr(params[f"{task}.ocr_version"], "value", params[f"{task}.ocr_version"])
        model_type = getattr(params[f"{task}.model_type"], "value", params[f"{task}.model_type"])
        assert str(version).lower().endswith("v6"), f"{task} 没用 v6：{version}"
        assert str(model_type).lower() == rapid.MODEL_TYPE_NAME, f"{task} 规格不一致：{model_type}"
    assert any(k.startswith("EngineConfig.onnxruntime.") for k in params), (
        "每路线程数没设（会 N×核数 抢核；且 3.x 只认 EngineConfig.onnxruntime.* ）"
    )
    #: 模型目录必须显式给：默认目录在用户级 site-packages 里，装包版不可写。
    assert params.get("Global.model_root_dir"), "模型目录没固定（装包版会去找不可写路径）"


def test_no_directml_dependency_anywhere():
    """DirectML 实测与 CPU 同速，**不引入** —— 一条依赖就能让普通电脑装不上。

    判据：**生效依赖行**里不得出现 `onnxruntime-directml` / `onnxruntime-gpu`；
    服务代码里不得开 `use_dml`/`use_cuda`（那是给「有独显的机器」留的口子，
    本项目决定不用：8 核 CPU 与核显 DirectML 实测同速）。
    """
    deps = "\n".join(_requirement_lines(REQUIREMENTS.read_text(encoding="utf-8")))
    assert "onnxruntime-directml" not in deps
    assert "onnxruntime-gpu" not in deps
    src = SERVICE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    code_names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    code_names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not ({"use_dml", "use_cuda"} & code_names), (
        "服务代码不该开 DML/CUDA：实测无收益，且会让依赖表翻倍"
    )


def test_requirements_declare_the_new_engine_package():
    """装依赖的人必须拿到 3.x，而不是旧的 `rapidocr-onnxruntime`。"""
    deps = _requirement_lines(REQUIREMENTS.read_text(encoding="utf-8"))
    assert not any(ln.startswith("rapidocr-onnxruntime") for ln in deps), (
        "requirements 仍把旧包名当依赖（pip 会装 1.x，代码 import `rapidocr` 就缺包）"
    )
    assert any(ln.startswith("rapidocr>=") for ln in deps), "requirements 缺 rapidocr>=3.10"


# --------------------------------------------------------------------------- #
# 2. 3.x 输出 -> 上层结构的转换（纯函数，行为断言）
# --------------------------------------------------------------------------- #


class _FakeOutput:
    """最小化的 `RapidOCROutput` 替身（有 txts / boxes / scores 三个并列属性）。"""

    def __init__(self, txts, boxes, scores):
        self.txts = txts
        self.boxes = boxes
        self.scores = scores


def test_rows_from_result_maps_rows_and_boxes():
    """一行一个"词"、坐标取 4 点包围盒、原图宽高原样回传。"""
    out = _FakeOutput(
        txts=("进口FAS级红橡木", "超大斗柜储物"),
        boxes=[
            [[10.0, 20.0], [110.0, 20.0], [110.0, 50.0], [10.0, 50.0]],
            [[10.0, 60.0], [90.0, 60.0], [90.0, 90.0], [10.0, 90.0]],
        ],
        scores=(0.96, 0.99),
    )
    payload = rapid._rows_from_result(out, 800, 600)

    assert payload["ok"] is True
    assert payload["pixel_width"] == 800 and payload["pixel_height"] == 600
    assert payload["text"] == "进口FAS级红橡木\n超大斗柜储物"
    first = payload["lines"][0]
    assert first["text"] == "进口FAS级红橡木"
    assert len(first["words"]) == 1, "RapidOCR 不做词切分 ⇒ 整行当一个词"
    word = first["words"][0]
    assert (word["x"], word["y"], word["w"], word["h"]) == (10.0, 20.0, 100.0, 30.0)


def test_rows_from_result_drops_low_score_and_broken_rows():
    """低置信行、退化框（w/h<=0）、下标错位都要**整条丢弃**，不能塞个假框。"""
    out = _FakeOutput(
        txts=("保留", "低分", "零宽", ""),
        boxes=[
            [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
            [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
            [[5.0, 5.0], [5.0, 5.0], [5.0, 5.0], [5.0, 5.0]],
            [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
        ],
        scores=(0.99, 0.1, 0.99, 0.99),  # 第二条低于 MIN_SCORE
    )
    lines = rapid._rows_from_result(out, 100, 100)["lines"]
    assert [line["text"] for line in lines] == ["保留"], lines


def test_rows_from_result_tolerates_short_boxes_list():
    """`boxes` 比 `txts` 短（引擎异常时的真实形状）不能整批抛，只丢缺框的行。"""
    out = _FakeOutput(txts=("有框", "没框"), boxes=[[[0.0, 0.0], [9.0, 0.0], [9.0, 9.0], [0.0, 9.0]]], scores=(0.9, 0.9))
    lines = rapid._rows_from_result(out, 50, 50)["lines"]
    assert [line["text"] for line in lines] == ["有框"]


# --------------------------------------------------------------------------- #
# 3. 并发口径（本轮真机两轮都栽在这里）
# --------------------------------------------------------------------------- #


def test_parallelism_saturates_at_the_measured_knee_not_the_config_value(monkeypatch):
    """饱和点优先、用户配置是**保底**。

    真机实证（2026-10-20）：配置值 5 时 `parallelism` 恒为 5，118 张 114.8s；
    手工提到 8 后仍是 114.8s ⇒ 5 路已是饱和，但**配置值不该成为上限** ——
    否则「按 CPU 核数自动调节」这句话在 8 核机上永远不成立。
    """
    monkeypatch.setattr(ocr.settings_service, "live_int", lambda *a, **k: 5)
    assert ocr.resolve_parallelism(118) == ocr.SATURATION_WORKERS
    assert ocr.SATURATION_WORKERS > 5, "饱和点应高于默认配置值，否则自动上推是空话"

    #: 小包不浪费线程
    assert ocr.resolve_parallelism(2) == 2
    assert ocr.resolve_parallelism(1) == 1
    #: 不超硬上界
    assert ocr.resolve_parallelism(10_000) <= ocr.MAX_PARALLELISM


def test_user_config_above_saturation_is_respected(monkeypatch):
    """用户显式配得比饱和点高时按用户的（否则设置页填了不生效，等于假开关）。"""
    monkeypatch.setattr(ocr.settings_service, "live_int", lambda *a, **k: 12)
    got = ocr.resolve_parallelism(300)
    assert got >= min(12, ocr.MAX_PARALLELISM), f"用户配 12 却被压到 {got}"


def test_scan_images_reports_what_it_actually_used(monkeypatch):
    """`summary.parallelism` 必须是**真实用到的**路数（前端/排障都靠它）。"""
    monkeypatch.setattr(ocr, "engine_available", lambda: True)

    def fake_scan_one(path, entries):
        return {
            "path": path, "name": Path(path).name, "ocr_ok": True,
            "ocr_text": "", "hits": [], "error": "", "error_message": "",
            "width": 1, "height": 1,
        }

    monkeypatch.setattr(ocr, "_scan_one", fake_scan_one)
    out = ocr.scan_images([f"/tmp/x{i}.jpg" for i in range(20)], [])
    assert out["summary"]["parallelism"] == min(20, ocr.SATURATION_WORKERS)
    assert out["summary"]["total"] == 20


# --------------------------------------------------------------------------- #
# 4. 打包：引擎与模型必须进包
# --------------------------------------------------------------------------- #


def test_spec_collects_the_ocr_package_and_its_models():
    """`_OCR_PKG_NAME` 一处定义，两处使用（hiddenimports + datas）。"""
    spec = SPEC.read_text(encoding="utf-8")
    assert '_OCR_PKG_NAME = "rapidocr"' in spec
    assert "_safe_submodules(_OCR_PKG_NAME)" in spec
    assert "def _rapidocr_data()" in spec and "datas.extend(_rapidocr_data())" in spec
    assert "importlib.import_module(_OCR_PKG_NAME)" in spec


def test_precheck_payload_carries_segmented_timing():
    """预检返回必须带 `timing` 分段（图片 / 视频），否则无法定位「慢在哪一段」。"""
    api = (ROOT / "backend/app/api/platforms/xhs/package_precheck.py").read_text("utf-8")
    tree = ast.parse(api)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "video_elapsed" in names, "视频段没计时"
    assert '"timing"' in api and '"parallelism"' in api


def test_import_surface_is_clean():
    """换引擎后不该残留旧包名的**代码引用**（注释里的历史对比不算）。

    ⚠️ 按 AST 取 import 名判断，不按子串 —— 模块 docstring 里保留着
    「旧：`rapidocr_onnxruntime` 1.x」这行实测对照表，子串匹配会误伤。
    """
    for path in (SERVICE, IMAGE_OCR):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "rapidocr_onnxruntime" not in imported, f"{path.name} 仍 import 旧包名"
