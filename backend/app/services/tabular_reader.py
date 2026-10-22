"""表格读取内核：**所有**表格格式 → 「一个 sheet = 表头 + 数据行」。

用户口径 2026-10-21：「商品发布应支持 xlsx 和 csv 表的读取」→「应支持常用的大多数表格格式」。

## 为什么要独立成模块

数据包在两个地方被读（预检展示 / 建草稿发布），**两处如果各写一套格式判断，
迟早出现「预检能读、发布读不出」这种最难查的错位**。所以格式分流的唯一真相源在这里，
两侧都只调 `read_tables()`。

## 支持矩阵（本机实测，Python 3.12）

| 后缀 | 读取器 | 依赖 | 说明 |
|---|---|---|---|
| `.xlsx` `.xlsm` | `openpyxl`（read_only+data_only） | 已有 | 每个 sheet 一张表 |
| `.xls`（97-2003） | `xlrd`（**>=2.0 只支持 xls**） | 已有 | 每个 sheet 一张表 |
| `.ods`（LibreOffice） | 标准库 `zipfile` + `ElementTree` | **零依赖** | 直解析 `content.xml` |
| `.csv` `.txt` `.tsv` | 标准库 `csv` + 编码探测 | 零依赖 | 一个文件一张表 |
| `.xlsb` | `pyxlsb` | 可选 | 库缺失时给明确提示 |

## 三条必须守住的判据

1. **后缀决定引擎，不许「先试一个再试另一个」** —— `xlrd>=2.0` 读 xlsx 直接抛
   `XLRDError`；`openpyxl` 读 xls 抛 `InvalidFileException`。靠异常兜底会变成
   N 次幻觉重试，且错误原因被掩盖。
2. **同一份数据在四种格式下必须得到同样的列名与行数**（这是用户能感知的唯一口径）。
3. **读不出要说清是哪一种原因**（编码 / 格式不支持 / 库缺失 / 坏文件）——
   用户手里的是文件，不是我们的堆栈。

⚠️ `.ods` 不用 `odfpy`/`pandas`：实测 `odfpy` 写出的 ods 用 `pandas(engine="odf")`
读回会 `KeyError: There is no item named 'META-INF/manifest.xml'`，而且两者合计几十 MB
依赖只为读一张表不值得。ods 本质是 zip 里的 `content.xml`，标准库直读足够且可单测。
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from xml.etree import ElementTree as ET

#: 参与解析的表格后缀（**顺序无关**；分流见 `_reader_for`）。
TABLE_SUFFIXES: frozenset[str] = frozenset(
    {".xlsx", ".xlsm", ".xls", ".ods", ".csv", ".tsv", ".txt", ".xlsb"}
)

#: 展示给用户的「支持哪些格式」文案（错误提示与前端提示共用同一句话，避免两处漂移）。
SUPPORTED_HINT = ".xlsx / .xlsm / .xls / .ods / .csv / .tsv"

#: csv 系编码探测顺序（**顺序即优先级**）：
#: 先剥 BOM（Excel「CSV UTF-8」），再中文 GBK 家族（中文 Windows 导出的默认），最后纯 utf-8。
#: ⚠️ 预检侧与建草稿侧必须用**同一套**，所以它只在这里定义一次。
CSV_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "gb18030", "utf-8")

#: csv 系后缀（都按分隔符文本处理）。
_TEXT_SUFFIXES: frozenset[str] = frozenset({".csv", ".tsv", ".txt"})

#: 各后缀的默认分隔符（`.txt` 由 `sniff` 兜底）。
_DELIMITERS: dict[str, str] = {".csv": ",", ".tsv": "\t"}

_ODS_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
}


@dataclass
class SheetRows:
    """一个 sheet（或一个 csv 文件）的原始二维数据。

    `name` = sheet 名 / 文件名去扩展名；`header` 首行；`rows` 其余行（已按表头宽度截齐）。
    """

    name: str
    header: list[str]
    rows: list[list[Any]]
    source_file: str = ""
    encoding: str = ""
    extra_warnings: list[str] = field(default_factory=list)


class TableReadError(Exception):
    """读某个文件失败（**只影响该文件**，调用方转成 warning 继续读下一个）。"""


# --------------------------------------------------------------------------- #
# 公共入口
# --------------------------------------------------------------------------- #


def is_table_file(path: Path) -> bool:
    return path.suffix.lower() in TABLE_SUFFIXES


def read_tables(path: Path) -> list[SheetRows]:
    """一个表格文件 → 若干 `SheetRows`（csv 系恒为 1 个）。

    读不出抛 `TableReadError`（消息是给用户看的，**必须点名文件与原因**）。
    """
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        return _read_xlsx(path)
    if suffix == ".xls":
        return _read_xls(path)
    if suffix == ".ods":
        return _read_ods(path)
    if suffix in _TEXT_SUFFIXES:
        return _read_delimited(path)
    if suffix == ".xlsb":
        return _read_xlsb(path)
    raise TableReadError(f"「{path.name}」不是支持的表格格式（支持 {SUPPORTED_HINT}）")


def cell_text(value: Any) -> str | None:
    """单元格 → 文本（缺值口径：**空就是 None，不造 0**）。"""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))  # 12345.0 → "12345"（Excel 数值列常见）
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    return text or None


def _trim(values: list[Any], width: int) -> list[str | None]:
    """按表头宽度截齐 + 去重尾空列（`openpyxl` 常在末尾补空列）。"""
    out = [cell_text(v) for v in values[:width]]
    if len(out) < width:
        out += [None] * (width - len(out))
    return out


def _finish(name: str, header: list[Any], rows: Iterator[Any], source_file: str) -> SheetRows:
    columns = [str(c).strip() if c is not None else "" for c in header]
    while columns and not columns[-1]:
        columns.pop()
    width = len(columns)
    data: list[list[Any]] = []
    for raw in rows:
        if raw is None:
            continue
        values = _trim(list(raw), width)
        if not any(v not in (None, "") for v in values):
            continue  # 空行（表尾最常见）
        data.append(values)
    return SheetRows(name=name, header=columns, rows=data, source_file=source_file)


# --------------------------------------------------------------------------- #
# xlsx / xlsm
# --------------------------------------------------------------------------- #


def _read_xlsx(path: Path) -> list[SheetRows]:
    try:
        import openpyxl
    except ImportError as error:  # pragma: no cover - 环境问题
        raise TableReadError("缺少 openpyxl 依赖，无法读取 xlsx") from error
    try:
        book = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as error:  # noqa: BLE001 - 坏文件也要变成可读原因
        raise TableReadError(f"读取「{path.name}」失败：{type(error).__name__}") from error
    try:
        out: list[SheetRows] = []
        for sheet in book.worksheets:
            iterator = sheet.iter_rows(values_only=True)
            header = next(iterator, None)
            if header is None:
                out.append(
                    SheetRows(
                        name=str(sheet.title),
                        header=[],
                        rows=[],
                        source_file=path.name,
                        extra_warnings=[f"「{sheet.title}」是空表（没有表头行）"],
                    )
                )
                continue
            out.append(_finish(str(sheet.title), list(header), iterator, path.name))
        return out
    finally:
        try:
            book.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# xls（Excel 97-2003）
# --------------------------------------------------------------------------- #


def _read_xls(path: Path) -> list[SheetRows]:
    try:
        import xlrd
    except ImportError as error:  # pragma: no cover - 环境问题
        raise TableReadError(
            f"「{path.name}」是老式 .xls，需要 xlrd 才能读取（依赖缺失）；"
            "请用 Excel 另存为 .xlsx 或 .csv"
        ) from error
    try:
        book = xlrd.open_workbook(str(path))
    except Exception as error:  # noqa: BLE001
        raise TableReadError(f"读取「{path.name}」失败：{type(error).__name__}") from error

    out: list[SheetRows] = []
    for sheet in book.sheets():
        if sheet.nrows == 0:
            out.append(
                SheetRows(
                    name=str(sheet.name),
                    header=[],
                    rows=[],
                    source_file=path.name,
                    extra_warnings=[f"「{sheet.name}」是空表（没有表头行）"],
                )
            )
            continue
        header = sheet.row_values(0)
        body = (sheet.row_values(r) for r in range(1, sheet.nrows))
        out.append(_finish(str(sheet.name), list(header), body, path.name))
    return out


# --------------------------------------------------------------------------- #
# ods（LibreOffice / OpenOffice）—— 标准库直解析
# --------------------------------------------------------------------------- #


def _ods_cell_text(cell: ET.Element) -> str | None:
    """一个 `table:table-cell` → 文本。

    ods 把文字放在 `<text:p>` 里（可多个段落）；数值在 `office:value` 属性上。
    """
    paragraphs = [
        "".join(p.itertext()) for p in cell.findall(".//text:p", _ODS_NS)
    ]
    text = "\n".join(p for p in paragraphs if p)
    if text.strip():
        return text
    value = cell.get(f"{{{_ODS_NS['office']}}}value")
    if value:
        return value
    boolean = cell.get(f"{{{_ODS_NS['office']}}}boolean-value")
    if boolean:
        return boolean
    return None


def _read_ods(path: Path) -> list[SheetRows]:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("content.xml")
    except (zipfile.BadZipFile, KeyError) as error:
        raise TableReadError(
            f"读取「{path.name}」失败：不是有效的 ods（{type(error).__name__}）"
        ) from error
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise TableReadError(f"读取「{path.name}」失败：XML 损坏") from error

    out: list[SheetRows] = []
    for table in root.findall(".//table:table", _ODS_NS):
        name = table.get(f"{{{_ODS_NS['table']}}}name") or path.stem
        raw_rows: list[list[str | None]] = []
        for row in table.findall("table:table-row", _ODS_NS):
            #: `number-rows-repeated` 是空行压缩（真实 ods 极常见）——
            #: 空行重复多少次都**不产生数据**，这里只按 1 行处理后续会被空行过滤掉。
            cells: list[str | None] = []
            for cell in row.findall("table:table-cell", _ODS_NS):
                value = _ods_cell_text(cell)
                #: ⚠️ `number-columns-repeated` **一律只取一个**，不管格子空不空：
                #:   · 空列压缩（真实 ods 几乎每行都带）= 撑出几十个空列；
                #:   · 非空重复（LibreOffice 合并单元格）= 凭空造数据，表头 `原价`×20
                #:     会让表宽变 21 列（**实测抓到的真 bug**）。
                #: 两种情况都没有「这一列真的存在」的语义，展开只会把表读歪。
                cells.append(value)
            raw_rows.append(cells)
        if not raw_rows:
            out.append(
                SheetRows(
                    name=name,
                    header=[],
                    rows=[],
                    source_file=path.name,
                    extra_warnings=[f"「{name}」是空表（没有表头行）"],
                )
            )
            continue
        header = raw_rows[0]
        out.append(_finish(name, list(header), iter(raw_rows[1:]), path.name))
    return out


# --------------------------------------------------------------------------- #
# csv / tsv / txt
# --------------------------------------------------------------------------- #


def _decode(raw: bytes) -> tuple[str, str]:
    last: Exception | None = None
    for encoding in CSV_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError as error:
            last = error
    raise TableReadError(
        f"编码无法识别（已试 {' / '.join(CSV_ENCODINGS)}）；请另存为「CSV UTF-8」后重试"
    ) from last


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        return ","


def _read_delimited(path: Path) -> list[SheetRows]:
    try:
        text, encoding = _decode(path.read_bytes())
    except TableReadError as error:
        raise TableReadError(f"读取「{path.name}」失败：{error}") from error
    text = text.lstrip("\ufeff")

    delimiter = _DELIMITERS.get(path.suffix.lower()) or _sniff_delimiter(text[:4096])
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    header = next(reader, None)
    if header is None:
        return [
            SheetRows(
                name=path.stem,
                header=[],
                rows=[],
                source_file=path.name,
                encoding=encoding,
                extra_warnings=[f"「{path.name}」是空表（没有表头行）"],
            )
        ]
    sheet = _finish(path.stem, list(header), reader, path.name)
    sheet.encoding = encoding
    return [sheet]


# --------------------------------------------------------------------------- #
# xlsb（可选依赖）
# --------------------------------------------------------------------------- #


def _read_xlsb(path: Path) -> list[SheetRows]:
    try:
        from pyxlsb import open_workbook
    except ImportError as error:
        raise TableReadError(
            f"「{path.name}」是 .xlsb（Excel 二进制工作簿），需要 pyxlsb 才能读取；"
            "请用 Excel 另存为 .xlsx 或 .csv"
        ) from error
    try:
        out: list[SheetRows] = []
        with open_workbook(str(path)) as book:
            for sheet_name in book.sheets:
                with book.get_sheet(sheet_name) as sheet:
                    iterator = (
                        [cell.v for cell in row] for row in sheet.rows()
                    )
                    header = next(iterator, None)
                    if header is None:
                        continue
                    out.append(_finish(str(sheet_name), list(header), iterator, path.name))
        return out
    except TableReadError:
        raise
    except Exception as error:  # noqa: BLE001
        raise TableReadError(f"读取「{path.name}」失败：{type(error).__name__}") from error


# --------------------------------------------------------------------------- #
# 表格名展示
# --------------------------------------------------------------------------- #

_SUFFIX_RE = re.compile(r"\.(xlsx|xlsm|xls|ods|csv|tsv|txt|xlsb)$", re.IGNORECASE)


def sheet_display_name(sheet_name: str, fallback: str = "") -> str:
    """sheet 名 → 表头展示名（**只剥扩展名**）。

    真实包实测：`openpyxl` 的 sheet 名本身可能带 `.xlsx` 后缀；但**业务后缀不许剥** ——
    2026-10-21 实测 `店透-SKU预览-表格-827654559981-2026-10-13.xlsx` 里的 `-表格`
    是文件名的一部分，剥掉后表头变成 `…-SKU预览-`，用户对不上自己给的文件。
    """
    text = (sheet_name or "").strip() or (fallback or "").strip()
    text = text.replace("/", "-").replace("\\", "-")
    return _SUFFIX_RE.sub("", text).strip() or "未命名表格"
