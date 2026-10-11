"""数据包**预检**（设计 25 §4；用户口径 2026-10-08）。

## 与 `shop_draft_package` 的分工

`shop_draft_package` 是**一期**的「解析 → 拼草稿体」，认的是**服务器本地
目录**（`parse_package(path)`）。预检这一步不一样：数据��在**用户电脑上**，
后端**读不到客户机磁盘**。

⇒ 所以预检走**上传字节流**：前端 `<input webkitdirectory>` 拿到目录后
逐个文件 `POST` 上来，后端只对**上传上来的字节**做解析。
**零客户机磁盘访问**，与只读边界不冲突（这是用户自己给自己的文件）。

## 三个关键取舍

1. **表头取 sheet 名去后缀，不是文件名**
   用户口径「表头名称数据包中表格的名称不带后缀」——真实包是
   `商品ID_935947169189_1_sku数据.xlsx`，sheet 名是 `商品ID_935947169189_1_sku数据`；
   两种口径都指向同一串，所以取 **sheet 名**，并额外剥掉扩展名残留。

2. **表格 1:1 还原 = 不做任何列映射**
   一期 `parse_package` 会把 `sku名称/图片/原价` 映射成内部键；预检**不映射**，
   列原样透出（表头不可改 ⇒ 映射会让用户看到的东西和包里的对不上）。

3. **大文件不进内存**：`UploadFile` 落到临时目录后才处理，处理完即删。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.app.services.shop_draft_package import (
    DIR_DETAIL,
    DIR_MAIN,
    DIR_SKU,
    DIR_VIDEO,
    IMAGE_EXTS,
    MAX_IMAGE_BYTES,
    VIDEO_EXTS,
)

logger = logging.getLogger(__name__)

#: 预检的四个媒体区域（用户口径：分主图区域、视频区域、sku图区域、详情图区域）。
MEDIA_GROUPS: tuple[str, ...] = ("main", "video", "sku", "detail")

#: 区域 → 数据包目录名（用于把上传的相对路径归到某个区域）。
#:
#: ⚠️ **键必须小写**：`group_of_rel_path` 按小写查表。
#: 真实包目录是 `SKU`（大写，实测 2026-10-08），而 `DIR_SKU` 常量是小写
#: `sku` —— 直接拿常量当键会漏掉整个 SKU 区域（第一版实测：`sku: 0`）。
_GROUP_BY_DIRNAME: dict[str, str] = {
    DIR_MAIN.lower(): "main",
    DIR_SKU.lower(): "sku",
    DIR_DETAIL.lower(): "detail",
    DIR_VIDEO.lower(): "video",
}

#: 表头名要剥掉的后缀（用户口径「不带后缀」）。
_SHEET_SUFFIXES: tuple[str, ...] = ("_sku数据", "_sku", "_data", "_sheet", "_表格")


class PrecheckError(ValueError):
    """预检失败（没有 xlsx / 没有可识别区域 / 文件读不出来）。"""


def sheet_display_name(sheet_name: str, fallback: str = "") -> str:
    """sheet 名 → 表头展示名（**去掉后缀**）。

    真实包实测（2026-10-08）：文件名与 sheet 名都是
    `商品ID_935947169189_1_sku数据`，扩展名可能被并进 sheet 名，
    所以先剥扩展名再剥业务后缀。
    """
    text = str(sheet_name or fallback or "").strip()
    if not text:
        return "表格"
    #: 先剥扩展名（`.xlsx` 之类可能被并进 sheet 名）
    text = re.sub(r"\.(xlsx|xlsm|xls|csv)$", "", text, flags=re.IGNORECASE)
    changed = True
    while changed:  # 可能叠加多层后缀（`_1_sku数据` 之类）
        changed = False
        for suffix in _SHEET_SUFFIXES:
            if len(text) > len(suffix) and text.lower().endswith(suffix.lower()):
                text = text[: -len(suffix)]
                changed = True
    return text.strip() or str(sheet_name or fallback or "表格").strip()


@dataclass
class PrecheckMedia:
    """一个媒体文件在预检里的完整身份。"""

    group: str
    #: 区域内稳定序号（0 起）—— 前端做「反选 / 全选」与排序都靠它。
    index: int
    #: 数据包里的**原始相对路径**（如 `主图/主图01.jpg`）—— 第二步据此找回本地文件。
    rel_path: str
    file_name: str
    size_bytes: int
    #: 超过平台 5M 上限（**不静默丢弃**，前端要显示并默认不勾选）。
    oversized: bool = False
    #: 扩展名不在白名单（非图片被放进媒体目录时）。
    unsupported: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "index": self.index,
            "rel_path": self.rel_path,
            "file_name": self.file_name,
            "size_bytes": self.size_bytes,
            "oversized": self.oversized,
            "unsupported": self.unsupported,
        }


@dataclass
class PrecheckTable:
    """数据包里**一个** sheet 的一比一还原（表头只读 + 数据可改）。"""

    #: 展示用表头名（sheet 名去后缀）。
    title: str
    #: 原始 sheet 名（回写用，不给用户改）。
    sheet_name: str
    #: 表头（**只读**，用户口径）。
    columns: list[str] = field(default_factory=list)
    #: 数据行（每行一个 list，与 `columns` 等长；值统一转字符串或 None）。
    rows: list[list[str | None]] = field(default_factory=list)
    #: ⚠️ **来源 xlsx 的文件名**（用户口径 2026-10-08：「表格顶部新增文件名称」）。
    #: 表名只说清是哪个 sheet，一个包里可能有多个 xlsx ⇒ 必须能看出出自哪份文件。
    source_file: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "sheet_name": self.sheet_name,
            "columns": list(self.columns),
            "rows": [list(r) for r in self.rows],
            "source_file": self.source_file,
        }


@dataclass
class PrecheckResult:
    package_name: str
    media: list[PrecheckMedia] = field(default_factory=list)
    tables: list[PrecheckTable] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        groups: dict[str, int] = {g: 0 for g in MEDIA_GROUPS}
        for item in self.media:
            groups[item.group] = groups.get(item.group, 0) + 1
        return {
            "package_name": self.package_name,
            "media_groups": groups,
            "media": [m.to_dict() for m in self.media],
            "tables": [t.to_dict() for t in self.tables],
            "warnings": list(self.warnings),
        }


def _cell_text(value: Any) -> str | None:
    """单元格 → 展示文本（`None` 保持 `None`，**不造假不填 0**）。

    数字用 `str()` 直出（`200.0` 会显示成 `200.0`）—— 所以 float 且是整数
    值时去掉 `.0`，与用户在 Excel 里看到的 `200` 一致。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return text or None


def group_of_rel_path(rel_path: str) -> str | None:
    """上传的相对路径 → 媒体区域（`主图/x.jpg` ⇒ `"main"`）。

    **只认第一级目录名**，且大小写不敏感（`主图` / `sku` / `SKU` / `视频`）。
    拿不准就返回 `None` —— 调用方进 `warnings`，不硬塞进某个区域
    （把 `详情图` 当 `主图` 会打乱封面顺序，而顺序就是主图顺序）。
    """
    parts = [
        p
        for p in str(rel_path or "").replace("\\", "/").split("/")
        if p not in (".", "", "/")
    ]
    if len(parts) < 2:
        return None
    return _GROUP_BY_DIRNAME.get(parts[0].strip().lower())


def _scan_media(stage_root: Path) -> tuple[list[PrecheckMedia], list[str]]:
    """扫暂存目录里的媒体文件 → (按区域排序的媒体列表, warnings)。"""
    warnings: list[str] = []
    by_group: dict[str, list[PrecheckMedia]] = {g: [] for g in MEDIA_GROUPS}
    seen_dirs: set[str] = set()

    for path in sorted(stage_root.rglob("*"), key=lambda p: p.as_posix().lower()):
        if not path.is_file():
            continue
        rel = path.relative_to(stage_root).as_posix()
        top = rel.split("/")[0]
        seen_dirs.add(top)
        group = group_of_rel_path(rel)
        if group is None:
            continue  # 区域外的文件（如根目录的 xlsx）不当作媒体
        #: ⚠️ 用户口径 2026-10-11（**最终版，撤销本批次中间那版「排除属性图」**）：
        #: 「**数据包中有什么媒体资源就展示什么，不要排除任何媒体资源**」。
        #: ⇒ 媒体清单 = 数据包里的**全部**媒体文件，一个不减（含属性图 / 参数图）。
        #:
        #: ⚠️ 与「删除图片 OCR 建表」不冲突 —— 那是**表格产出**的事（见
        #: `TestImageToTableOcrIsGone`：`build_table_from_boxes` 已整段移除）。
        #: 本次撤销的只是「按文件名把图排除出媒体清单」这一步。
        suffix = path.suffix.lower()
        exts = VIDEO_EXTS if group == "video" else IMAGE_EXTS
        by_group[group].append(
            PrecheckMedia(
                group=group,
                index=len(by_group[group]),
                rel_path=rel,
                file_name=path.name,
                size_bytes=path.stat().st_size,
                oversized=path.stat().st_size > MAX_IMAGE_BYTES,
                unsupported=suffix not in exts,
            )
        )

    #: 目录名按**小写**比对（真实包是 `SKU`，常量是 `sku`）
    seen_lower = {d.lower() for d in seen_dirs}
    for dirname in (DIR_MAIN, DIR_SKU, DIR_DETAIL, DIR_VIDEO):
        if dirname.lower() not in seen_lower:
            warnings.append(f"数据包里没有「{dirname}」目录")

    media: list[PrecheckMedia] = []
    for group in MEDIA_GROUPS:
        media.extend(by_group[group])
    for item in media:
        if item.oversized:
            warnings.append(
                f"「{item.rel_path}」{item.size_bytes / 1048576:.1f}M "
                "超过平台单文件 5M 上限"
            )
        if item.unsupported:
            warnings.append(f"「{item.rel_path}」不是 {item.group} 区域支持的格式")
    return media, warnings


def _read_tables(stage_root: Path) -> tuple[list[PrecheckTable], list[str]]:
    """读数据包里的**每个** xlsx 的**每个** sheet → 一比一还原的表格。"""
    warnings: list[str] = []
    try:
        import openpyxl
    except ImportError:  # pragma: no cover - 依赖缺失
        return [], ["缺少 openpyxl 依赖，无法读取 xlsx"]

    tables: list[PrecheckTable] = []
    for xlsx in sorted(stage_root.rglob("*.xlsx"), key=lambda p: p.name.lower()):
        try:
            book = openpyxl.load_workbook(str(xlsx), read_only=True, data_only=True)
        except Exception as error:  # noqa: BLE001
            warnings.append(f"读取「{xlsx.name}」失败：{type(error).__name__}")
            continue
        try:
            for sheet in book.worksheets:
                iterator = sheet.iter_rows(values_only=True)
                header = next(iterator, None)
                if header is None:
                    warnings.append(f"「{sheet.title}」是空表（没有表头行）")
                    continue
                columns = [str(c).strip() if c is not None else "" for c in header]
                while columns and not columns[-1]:
                    columns.pop()  # openpyxl 常在末尾补空列
                if not any(columns):
                    warnings.append(f"「{sheet.title}」没有有效表头")
                    continue
                rows: list[list[str | None]] = []
                for raw in iterator:
                    if raw is None:
                        continue
                    values = [_cell_text(c) for c in list(raw)[: len(columns)]]
                    if not any(v not in (None, "") for v in values):
                        continue  # 空行（表尾常见）
                    values += [None] * (len(columns) - len(values))
                    rows.append(values)
                tables.append(
                    PrecheckTable(
                        title=sheet_display_name(sheet.title, fallback=xlsx.stem),
                        sheet_name=str(sheet.title),
                        columns=columns,
                        rows=rows,
                        source_file=xlsx.name,
                    )
                )
        finally:
            try:
                book.close()
            except Exception:  # noqa: BLE001
                pass

    if not tables:
        raise PrecheckError("数据包里没有可读取的 xlsx 表格（无法还原商品表格）")
    return tables, warnings


def precheck_package(stage_root: str | Path, package_name: str = "") -> PrecheckResult:
    """对**已上传到暂存目录**的数据包做预检（纯本地，零平台请求）。

    `stage_root` 是前端逐文件上传后落盘的目录（结构与用户本地目录一致）。

    ⚠️ **不对图片做 OCR 建表**（用户口径 2026-10-11 最终版）：
    「对图片 OCR → 再用词框几何还原成行列」这条路已**整段移除**
    （`build_table_from_boxes` 连同 `_squeeze` / `_merge_cell` 一并删除）。
    预检的 `tables` **只来自数据包里的 xlsx**（表头是 xlsx 的原值，不是 `列N` 兜底名）。

    ⚠️ 媒体清单**不排除任何文件** —— 数据包里有什么媒体就展示什么
    （含 `商品属性图.png`）；后者照常进**违规词** OCR（红线），只是不再产出表格。
    """
    root = Path(str(stage_root))
    if not root.is_dir():
        raise PrecheckError(f"数据包暂存目录不存在：{stage_root}")

    media, media_warnings = _scan_media(root)
    tables, table_warnings = _read_tables(root)
    return PrecheckResult(
        package_name=package_name or root.name,
        media=media,
        tables=tables,
        warnings=[*media_warnings, *table_warnings],
    )
