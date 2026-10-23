"""多表格格式支持（用户口径 2026-10-21「应支持常用的大多数表格格式」）。

## 支持矩阵（**实测**，本机 Python 3.12）

| 后缀 | 读取器 | 说明 |
|---|---|---|
| `.xlsx` / `.xlsm` | `openpyxl`（read_only + data_only） | 每个 sheet 一张表 |
| `.csv` / `.txt` | 标准库 `csv` + 编码探测 | 一个文件一张表 |
| `.xls`（Excel 97-2003） | `xlrd` | 每个 sheet 一张表 |
| `.ods`（LibreOffice） | 标准库 `zipfile` + `ElementTree` 直解析 XML | **不用 odfpy/pandas**（见下） |
| `.xlsb` | `pyxlsb`（可选） | 二进制格式，库缺失时给明确提示 |

⚠️ **为什么 `.ods` 手写解析而不引 `odfpy` + `pandas`**（实测 2026-10-21）：
`odfpy` 写出的 ods 再用 `pandas(engine="odf")` 读回会 `KeyError: There is no item
named 'META-INF/manifest.xml'`；而且 `pandas + odfpy` 合计几十 MB 依赖，只为读一个
表格不值得。ods 本质是 zip 里的 `content.xml`（`table:table` / `table:table-row` /
`table:table-cell`），标准库直读足够，且**行为可单测**。

⚠️ `.xls` 的 `xlrd>=2.0` **只支持 xls**（读 xlsx 会 `XLRDError`）⇒ 后缀分流必须
在调用前做死，不能「先试 xlrd」。
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from backend.app.services import shop_draft_package, shop_package_precheck



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract

HEADER = ["sku名称", "原价", "库存"]
ROWS = [["颜色分类:甲", "99", "10"], ["颜色分类:乙", "88", "5"]]

ODS_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
}


# --------------------------------------------------------------------------- #
# 造各格式样本（只用标准库 / 已装依赖）
# --------------------------------------------------------------------------- #


def _write_csv(path: Path, encoding="utf-8-sig") -> Path:
    payload = "\r\n".join(",".join(r) for r in [HEADER, *ROWS]) + "\r\n"
    path.write_bytes(payload.encode(encoding))
    return path


def _write_xlsx(path: Path) -> Path:
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "全部"
    sheet.append(HEADER)
    for row in ROWS:
        sheet.append([row[0], float(row[1]), int(row[2])])
    book.save(str(path))
    return path


def _write_xls(path: Path) -> Path:
    xlwt = pytest.importorskip("xlwt")
    book = xlwt.Workbook()
    sheet = book.add_sheet("全部")
    for c, v in enumerate(HEADER):
        sheet.write(0, c, v)
    for r, row in enumerate(ROWS, start=1):
        sheet.write(r, 0, row[0])
        sheet.write(r, 1, float(row[1]))
        sheet.write(r, 2, int(row[2]))
    book.save(str(path))
    return path


def _write_ods(path: Path, *, repeated: bool = False) -> Path:
    """用 **odfpy 之外的**最小 XML 手写 ods（模拟 LibreOffice 真实结构）。

    `repeated=True` 时给一行 `number-rows-repeated`，验证解析器不会把重复计数
    当成数据（真实 ods 里很常见，是空行压缩的产物）。
    """
    rows_xml = []
    for row in [HEADER, *ROWS]:
        cells = "".join(
            f'<table:table-cell office:value-type="string"><text:p>{v}</text:p>'
            "</table:table-cell>"
            for v in row
        )
        rows_xml.append(f"<table:table-row>{cells}</table:table-row>")
    if repeated:
        rows_xml.append(
            '<table:table-row table:number-rows-repeated="5">'
            '<table:table-cell table:number-columns-repeated="3"/>'
            "</table:table-row>"
        )
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
        ' xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
        ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        "<office:body><office:spreadsheet>"
        '<table:table table:name="全部">' + "".join(rows_xml) + "</table:table>"
        "</office:spreadsheet></office:body></office:document-content>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        zf.writestr("content.xml", content)
    return path


# --------------------------------------------------------------------------- #
# 1. 预检侧：四种格式都要出表，且列名/行数一致
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "writer,suffix",
    [
        (_write_csv, ".csv"),
        (_write_xlsx, ".xlsx"),
        (_write_xls, ".xls"),
        (_write_ods, ".ods"),
    ],
)
def test_precheck_reads_every_common_table_format(tmp_path, writer, suffix):
    """**同一份数据**的四种常见格式，预检必须得到同样的列名与行数。"""
    writer(tmp_path / f"表{suffix}")

    tables, warnings = shop_package_precheck._read_tables(tmp_path)

    assert len(tables) == 1, f"{suffix} 没读出表：{warnings}"
    table = tables[0]
    assert table.columns == HEADER, f"{suffix} 列名不对：{table.columns}"
    assert [r[0] for r in table.rows] == [r[0] for r in ROWS], f"{suffix} 行不对"
    assert table.is_sku_table is True, f"{suffix} 没识别成 SKU 表"


def test_ods_ignores_trailing_repeated_empty_rows(tmp_path):
    """ods 的 `number-rows-repeated`（空行压缩）不能被当成数据行。"""
    _write_ods(tmp_path / "带重复.ods", repeated=True)

    tables, _ = shop_package_precheck._read_tables(tmp_path)
    rows = tables[0].rows
    assert len(rows) == len(ROWS), f"重复计数被当成数据：{len(rows)} 行"


def test_ods_column_repeat_does_not_widen_the_table(tmp_path):
    """ods 的 `number-columns-repeated` 不能把列宽撑开。

    真实 LibreOffice 文件几乎每行都带这个属性；照字面展开会让表多出成片的空列，
    「哪些列没用上」的提示与实际完全不符。

    ⚠️ 场景必须让 **表头行** 也带重复属性（值是重复的非空单元格）：
    数据行上的空列压缩会被 `_finish()` 的「按表头宽度截齐」兜住，测不出差异
    （实测：数据行重复 ⇒ 变异 ESCAPED）。表头本身被撑宽才是承重的判据。
    """
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
        ' xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
        ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        "<office:body><office:spreadsheet>"
        '<table:table table:name="S">'
        "<table:table-row>"
        '<table:table-cell office:value-type="string"><text:p>sku名称</text:p></table:table-cell>'
        #: 整行 20 列都是同一个表头值（LibreOffice 合并单元格常见写法）
        '<table:table-cell office:value-type="string"'
        ' table:number-columns-repeated="20"><text:p>原价</text:p></table:table-cell>'
        "</table:table-row>"
        "<table:table-row>"
        '<table:table-cell office:value-type="string"><text:p>甲</text:p></table:table-cell>'
        '<table:table-cell office:value-type="float" office:value="10"><text:p>10</text:p></table:table-cell>'
        "</table:table-row>"
        "</table:table></office:spreadsheet></office:body></office:document-content>"
    )
    with zipfile.ZipFile(tmp_path / "压缩.ods", "w") as zf:
        zf.writestr("content.xml", content)

    from backend.app.services import tabular_reader as tr

    parsed = tr.read_tables(tmp_path / "压缩.ods")[0]
    assert parsed.header == ["sku名称", "原价"], f"空列压缩被展开了：{parsed.header}"
    assert parsed.rows == [["甲", "10"]], parsed.rows


def test_gbk_encoded_csv_is_readable(tmp_path):
    """中文 Windows 导出的 csv 默认就是 GBK —— 读不出等于「用户的表格全不能用」。

    ⚠️ 这条必须**断言内容本身**（不能只断言「没报错」）：编码探测顺序丢掉 gb18030
    时，utf-8 解码会抛 UnicodeDecodeError ⇒ 若只断言「有表」就会用「兜底按
    latin-1 硬读」蒙混过关，得到一堆乱码列名而测试仍绿。
    """
    _write_csv(tmp_path / "gbk.csv", "gbk")

    tables, warnings = shop_package_precheck._read_tables(tmp_path)

    assert len(tables) == 1, warnings
    assert tables[0].columns == HEADER, f"gbk 解错了：{tables[0].columns}"
    assert tables[0].rows[0][0] == "颜色分类:甲", tables[0].rows[0]
    assert any("gb18030" in w for w in warnings), f"没告知编码：{warnings}"


def test_display_name_keeps_business_words_and_only_drops_the_extension():
    """展示名**只剥扩展名**：`-表格` 是文件名的一部分，剥掉用户就对不上文件。"""
    from backend.app.services.tabular_reader import sheet_display_name

    name = sheet_display_name("店透-SKU预览-表格-827654559981-2026-10-13.xlsx")
    assert name == "店透-SKU预览-表格-827654559981-2026-10-13", name
    assert "-表格" in name, "业务后缀被剥掉了"


def test_mixed_formats_all_survive_in_one_package(tmp_path):
    """一个包里同时有 csv + xlsx + xls + ods ⇒ 四张表都在（不互相顶掉）。"""
    _write_csv(tmp_path / "a.csv")
    _write_xlsx(tmp_path / "b.xlsx")
    _write_xls(tmp_path / "c.xls")
    _write_ods(tmp_path / "d.ods")

    tables, warnings = shop_package_precheck._read_tables(tmp_path)
    assert len(tables) == 4, f"只读出 {len(tables)} 张：{[t.sheet_name for t in tables]} {warnings}"


def test_unknown_table_format_is_named_in_a_warning(tmp_path):
    """不支持的表格格式（如 .xlsb 无库）要点名，不能只说「没有表格」。"""
    (tmp_path / "表格.xlsb").write_bytes(b"\x00\x01\x02")

    try:
        shop_package_precheck._read_tables(tmp_path)
    except shop_package_precheck.PrecheckError as error:
        assert "xlsx" in str(error)  # 错误文案给出了支持范围
    else:  # pragma: no cover
        raise AssertionError("只有不支持格式时应抛 PrecheckError")


# --------------------------------------------------------------------------- #
# 2. 建草稿侧：同样的四种格式都要能读 SKU 行
# --------------------------------------------------------------------------- #


def _package(tmp_path: Path, writer, suffix: str) -> Path:
    for name in ("主图", "详情图", "SKU图", "视频"):
        (tmp_path / name).mkdir(exist_ok=True)
    (tmp_path / "主图" / "主图01.jpg").write_bytes(b"x")
    writer(tmp_path / f"sku{suffix}")
    return tmp_path


@pytest.mark.parametrize(
    "writer,suffix",
    [
        (_write_csv, ".csv"),
        (_write_xlsx, ".xlsx"),
        (_write_xls, ".xls"),
        (_write_ods, ".ods"),
    ],
)
def test_parse_package_reads_sku_rows_from_every_common_format(tmp_path, writer, suffix):
    """建草稿（发布真正走的路）必须与预检支持同样的格式。"""
    _package(tmp_path, writer, suffix)

    parsed = shop_draft_package.parse_package(str(tmp_path))

    rows = parsed["sku_rows"]
    assert len(rows) == len(ROWS), f"{suffix}: {parsed['warnings']}"
    assert rows[0]["name"] == ROWS[0][0]
    assert rows[0]["original_price"] == 99.0
    assert rows[0]["price"] == 99.0, "计算价格列缺失时应回退原价"
    assert rows[0]["stock"] == 10


# --------------------------------------------------------------------------- #
# 3. 单一实现：两侧共用同一个解码内核
# --------------------------------------------------------------------------- #


def test_both_layers_use_the_one_tabular_reader():
    """两侧都必须走 `tabular_reader`，不许各写一套 —— 否则格式支持迟早对不上。"""
    precheck_src = Path(shop_package_precheck.__file__).read_text(encoding="utf-8")
    draft_src = Path(shop_draft_package.__file__).read_text(encoding="utf-8")
    assert "tabular_reader" in precheck_src, "预检侧没接统一读取内核"
    assert "tabular_reader" in draft_src, "建草稿侧没接统一读取内核"
