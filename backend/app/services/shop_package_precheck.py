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
    IMAGE_EXTS,
    MAX_IMAGE_BYTES,
    VIDEO_EXTS,
)

logger = logging.getLogger(__name__)

#: 媒体**区域类别**（发布语义）—— 只用于「这个区域像是什么用途」的参考。
#:
#: ⚠️ 用户口径 2026-10-13（**本次改动的核心**）：
#: 「数据包中读取到什么就按照读取到的文件夹分区域，**不做任何操作直接展示**」
#: 「读到什么媒体资源就展示什么」
#:
#: ⇒ `group` **不再是这四个值之一**，而是**数据包里真实的一级目录名原值**
#: （`1比1主图` / `页面展示主图` / `SKU图` / `主图视频` / `详情图` / `属性` …）。
#: `kind` 是对该区域名的**语义猜测**，仅供展示参考与「哪个区像主图」的提示，
#: **不参与任何过滤或丢弃** —— 第二步走哪张图完全由用户在区域映射弹窗里指定。
#:
#: ⚠️ 为什么必须这么改（实测两个真包）：
#:   包 A（淘宝店透导出）目录：`1比1主图`/`页面展示主图`/`SKU图`/`主图视频`/`详情图`
#:   包 B（1688 导出）目录：   `主图`/`详情图`/`SKU`/`视频`/`属性`
#:   硬编码四目录名只认 `主图`/`sku`/`详情图`/`视频` ⇒ **包 A 丢 63 个文件**
#:   （5 主图 + 5 页面展示主图 + 52 SKU 图 + 1 视频），且是**静默丢弃**
#:   （原实现 `continue` 掉，只在 warnings 留一行，用户根本不知道丢了什么）。
MEDIA_KINDS: tuple[str, ...] = ("main", "video", "sku", "detail", "other")

#: 区域名 → 类别 的**关键词**（按声明顺序优先匹配，先命中先得）。
#:
#: ⚠️ 顺序有语义，不能随便调：
#:   · `sku` 必须在 `main` 前 —— `SKU图` 同时含「图」，但更重要的是
#:     `主图视频` 会同时命中 `main`(主图) 与 `video`(视频)，靠 `video` 排前解决；
#:   · `detail` 在 `main` 前 —— 避免「详情主图」这类混合名被误判成主图。
#: ⚠️ 这只是**参考分类**，判错不影响媒体是否展示（不丢任何东西）。
_KIND_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("video", ("视频", "video", "mp4")),
    ("sku", ("sku", "规格")),
    ("detail", ("详情", "detail", "描述")),
    ("main", ("主图", "1比1", "1:1", "1/1", "页面展示", "首图", "白底")),
    ("other", ("属性", "参数", "attr")),
)


def kind_of_zone(zone: str) -> str:
    """区域名 → 类别（**只作参考，不影响媒体是否被展示**）。

    认不出 ⇒ `"other"`（**不丢弃**，也不再进 warnings 说「缺少某目录」——
    那条告警在「目录名直出」的口径下是错的：包 A 没有名为 `主图` 的目录，
    但它的 `1比1主图` 就是主图资源）。
    """
    text = str(zone or "").strip().lower()
    if not text:
        return "other"
    for kind, keywords in _KIND_KEYWORDS:
        if any(k in text for k in keywords):
            return kind
    return "other"


#: 顶层散落文件（不在任何子目录里，如数据包根下的 xlsx）归到这个区域名。
#:
#: ⚠️ 用户口径「读到什么媒体资源就展示什么」⇒ 根目录下的媒体（若有）也要能看见，
#: 不能因为没有一级目录就丢掉。
ROOT_ZONE = "根目录"

#: **明确不是媒体**的后缀 —— 这些**不进媒体清单**（它们在别处自有去处）。
#:
#: ⚠️ 与「平台不收的图片格式」（如 `.gif`）**不是一回事**：
#:   · 本清单里的（xlsx / zip / exe…）= **压根不是媒体**，进了清单只是噪音；
#:   · 平台不收的图片格式 = **是媒体，只是发不出去** ⇒ 用户口径 2026-10-13
#:     方案 C 要求**照常展示 + 红框**（`PrecheckMedia.unsupported`），**不在这张表里**。
#:
#: ⇒ 判据是「这是什么文件」，不是「平台收不收」——后者绝不能用来丢东西。
_NON_MEDIA_SUFFIXES: frozenset[str] = frozenset(
    {
        # 表格（走 _read_tables，不是媒体）
        ".xlsx", ".xls", ".xlsm", ".xlsb", ".csv", ".ods",
        # 文本 / 文档
        ".txt", ".md", ".json", ".xml", ".html", ".htm", ".pdf",
        ".doc", ".docx", ".ppt", ".pptx",
        # 压缩包
        ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
        # 可执行 / 库 / 工程文件
        ".exe", ".dll", ".msi", ".bat", ".cmd", ".ps1", ".sh",
        ".py", ".js", ".ts", ".css", ".psd", ".ai", ".sketch",
        # 其它明显非媒体
        ".db", ".sqlite", ".log", ".ini", ".cfg", ".toml", ".yaml", ".yml",
    }
)

#: 表头名要剥掉的后缀（用户口径「不带后缀」）。
#: ⚠️ **已废弃，不再参与展示名计算**（2026-10-21）。
#:
#: 历史：2026-10-08 用这套后缀把 `商品ID_935947169189_1_sku数据` 剥成更短的表头。
#: 2026-10-21 实测反例：`店透-SKU预览-表格-827654559981-2026-10-13.xlsx` 里的
#: `-表格` 是**文件名的一部分**，剥掉后表头变成 `…-SKU预览-`，用户认不出是哪个文件
#: （他当时的原话是「应只展示后面一个文件名称」——要的是**完整可辨**，不是更短）。
#: 保留常量只为让历史引用可见，**不要再用它裁展示名**。
_SHEET_SUFFIXES: tuple[str, ...] = ("_sku数据", "_sku", "_data", "_sheet", "_表格")


class PrecheckError(ValueError):
    """预检失败（没有 xlsx / 没有可识别区域 / 文件读不出来）。"""


def sheet_display_name(sheet_name: str, fallback: str = "") -> str:
    """sheet 名 → 表头展示名（**剥扩展名与目录前缀**）。

    真实包实测（2026-10-08）：文件名与 sheet 名都是
    `商品ID_935947169189_1_sku数据`，扩展名可能被并进 sheet 名，
    所以先剥扩展名再剥业务后缀。
    ⚠️ 2026-10-21 实测（`店透-SKU预览-表格-827654559981-2026-10-13.xlsx`）：
    `openpyxl` 的 sheet 名**本身就可能带 `.xlsx` 后缀** ⇒ 只剥扩展名、**不再剥业务后缀**
    （`_表格` 这类是名字的一部分，剥掉会显示成 `…-SKU预览-` ，用户认不出这是哪个文件）。
    """
    text = str(sheet_name or fallback or "").strip()
    if not text:
        return "表格"
    #: 有的包 sheet 名带目录前缀 ⇒ 只保留文件名部分。
    text = text.replace("\\", "/").rsplit("/", 1)[-1].strip()
    #: 剥扩展名（`.xlsx` 之类可能被并进 sheet 名）—— **只剥这一层**。
    text = re.sub(r"\.(xlsx|xlsm|xls|csv)$", "", text, flags=re.IGNORECASE)
    return text.strip() or str(sheet_name or fallback or "表格").strip()


@dataclass
class PrecheckMedia:
    """一个媒体文件在预检里的完整身份。"""

    #: ⚠️ **区域名 = 数据包里第一级目录名的原值**（用户口径 2026-10-13
    #: 「按照读取到的文件夹分区域，不做任何操作直接展示」）。
    #: 不再是 `main`/`video`/`sku`/`detail` 四选一 —— 包 A 的 `1比1主图` 原样透出。
    group: str
    #: 区域内稳定序号（0 起）—— 前端做「反选 / 全选」与排序都靠它。
    index: int
    #: 数据包里的**原始相对路径**（如 `主图/主图01.jpg`）—— 第二步据此找回本地文件。
    rel_path: str
    file_name: str
    size_bytes: int
    #: 超过平台 5M 上限（**不静默丢弃**，前端要显示并默认不勾选）。
    oversized: bool = False
    #: **平台不收这个格式**（展示照常 + 红框，但默认不勾选、发布时跳过）。
    #:
    #: ⚠️ 用户口径 2026-10-13 方案 C：「展示与上传都按平台，但把不合规的
    #: **列出来而不是丢掉**，红框提示」。与旧版「扩展名不认识 ⇒ 直接丢」不同。
    unsupported: bool = False
    #: `unsupported` 的人话原因（前端红框上的提示文案）。空 = 合规。
    unsupported_reason: str = ""
    #: 区域**参考类别**（`main`/`video`/`sku`/`detail`/`other`）—— 只作展示提示，
    #: **不参与过滤**。走哪张图由用户在第二步的区域映射弹窗里指定。
    kind: str = "other"

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "kind": self.kind,
            "index": self.index,
            "rel_path": self.rel_path,
            "file_name": self.file_name,
            "size_bytes": self.size_bytes,
            "oversized": self.oversized,
            "unsupported": self.unsupported,
            "unsupported_reason": self.unsupported_reason,
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
    #: **逐列类型**（`numeric` / `text` / `empty`，与 `columns` 等长）。
    #:
    #: ⚠️ 用户口径 2026-10-13：「**数值列不支持**（正则）只能读取原始值」
    #: ⇒ 前端据此决定哪些列给正则入口。**后端判、前端不猜**。
    column_kinds: list[str] = field(default_factory=list)
    #: 这张表**是不是 SKU / 规格表**（列名特征命中 ≥2）。
    #:
    #: ⚠️ 不能靠 sheet 名（实测包 B 叫 `全部`、包 A 被截断）。
    is_sku_table: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "sheet_name": self.sheet_name,
            "columns": list(self.columns),
            "rows": [list(r) for r in self.rows],
            "source_file": self.source_file,
            "column_kinds": list(self.column_kinds),
            "is_sku_table": self.is_sku_table,
        }


@dataclass
class PrecheckResult:
    package_name: str
    media: list[PrecheckMedia] = field(default_factory=list)
    tables: list[PrecheckTable] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_name": self.package_name,
            #: ⚠️ `media_groups` 是**旧契约**（固定四值计数），已由 `zones` 取代。
            #: 保留它以免老前端/老测试整块炸掉 —— 但它现在只统计 `kind` 参考类别，
            #: **不再能反映真实分区**（包 A 的 `1比1主图` 会算进 `main` 计数，
            #: 而真实区域名是 `1比1主图`）。新代码一律读 `zones`。
            "media_groups": _legacy_kind_counts(self.media),
            #: **区域清单**（保序）—— 前端按它渲染分区页签与区域映射下拉。
            "zones": zone_summary(self.media),
            "media": [m.to_dict() for m in self.media],
            "tables": [t.to_dict() for t in self.tables],
            "warnings": list(self.warnings),
        }


#: 列类型（后端判定后随表下发；前端据此决定要不要给正则入口）。
#:
#: ⚠️ 用户口径 2026-10-13：「**数值列不支持**（正则）只能读取原始值」。
#: 判定用「抽样多行」而不是只看第一行 —— 只看第一行会把「第 2 行起全空」
#: 的列（真包 `券后价格` 就是这样）误判成空列 / 数值列。
COLUMN_KINDS: tuple[str, ...] = ("numeric", "text", "empty")

#: 识别为「SKU 表」的列名特征（命中 **≥2 个**即判为 SKU 表）。
#:
#: ⚠️ 用户口径 2026-10-13：「sku表识别」不能靠 sheet 名 ——
#: 实测包 B 的 sheet 叫 `全部`、包 A 的 sheet 名被导出工具截断成
#: `店透-SKU预览-表格-827654555981-2026-1`，**两个包的 sheet 名都不可用**。
#: 改成看**列名特征命中数**：SKU 表一定有「规格 / 价格 / 库存 / 名称」里的若干个。
_SKU_COLUMN_HINTS: tuple[str, ...] = (
    "sku名称", "sku名", "规格", "颜色分类", "尺码", "原价", "价格", "售价",
    "库存", "数量", "编码", "货号",
)

#: 判定数值列的抽样行数（前 N 行有值的数据行）。
_TYPE_SAMPLE_ROWS = 24


def column_kind(values: list[str | None]) -> str:
    """一列的值 → `numeric` / `text` / `empty`。

    **抽样多行**判（不是只看第一行）：

    · 全部有值项都能解析成数 ⇒ `numeric`；
    · 一个值都没有 ⇒ `empty`；
    · 其余 ⇒ `text`（含「有数字也有文字」的混合列）。

    ⚠️ 为什么必须抽样：真包 `券后价格` 第 1 行有值、第 2 行起全空，
    只看第一行会判成 `numeric` ⇒ 前端给正则入口，但正则对空列毫无意义。
    反过来 `商品销量` 是 `1000+`（带加号的文本），只看一眼容易误判成数。
    """
    filled = [v for v in values if v not in (None, "")]
    if not filled:
        return "empty"
    for raw in filled[:_TYPE_SAMPLE_ROWS]:
        text = str(raw).strip().replace(",", "").replace("￥", "").replace("¥", "")
        if not text:
            continue
        try:
            float(text)
        except ValueError:
            return "text"
    return "numeric"


def looks_like_sku_table(columns: list[str]) -> bool:
    """列名特征命中 **≥2** ⇒ 判为 SKU / 规格表（用户口径 2026-10-13）。

    ⚠️ 不靠 sheet 名（实测两个真包的 sheet 名一个是 `全部`、一个被截断）。
    """
    lowered = [str(c or "").replace(" ", "").lower() for c in columns]
    hits = 0
    for hint in _SKU_COLUMN_HINTS:
        if any(hint in name for name in lowered):
            hits += 1
    return hits >= 2


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
    """上传的相对路径 → **媒体区域名**（= 数据包里第一级目录名的**原值**）。

    ⚠️ 用户口径 2026-10-13：「按照读取到的文件夹分区域，**不做任何操作直接展示**」。

    | 输入 | 返回 |
    |---|---|
    | `主图/主图01.jpg` | `"主图"` |
    | `SKU/SKU01.jpg` | `"SKU"`（**保留原大小写**，不做小写归一） |
    | `SKU图/SKU图_01_x.jpg` | `"SKU图"` |
    | `1比1主图/a.jpg` | `"1比1主图"` |
    | `loose.jpg`（顶层散落文件） | `None` —— 调用方归 `ROOT_ZONE` |

    ⚠️ **不再做白名单查表**：查表就是「认不出就丢」，而包 A 的 5 个目录名
    有 4 个不在旧白名单里 ⇒ 实测丢 63 个文件。现在凡是有一级目录的一律归区，
    区域名原样透出，前端按区域名分区展示（顺序 = 数据包里的目录顺序）。
    """
    parts = [
        p
        for p in str(rel_path or "").replace("\\", "/").split("/")
        if p not in (".", "", "/")
    ]
    if len(parts) < 2:
        #: 顶层散落文件：没有一级目录 ⇒ 交给调用方归 `ROOT_ZONE`（不丢弃）。
        return None
    return parts[0].strip() or None


def _scan_media(stage_root: Path) -> tuple[list[PrecheckMedia], list[str]]:
    """扫目录里的媒体文件 → (按区域分组的媒体列表, warnings)。

    ⚠️ 用户口径 2026-10-13（本次改动的核心）：

    > 「数据包中读取到什么就**按照读取到的文件夹分区域**，**不做任何操作直接展示**」
    > 「属性目录……**不应被丢弃**，读到什么媒体资源就展示什么」

    ⇒ 两条铁律：
      ① **一个媒体都不丢** —— 凡是有扩展名在白名单里的文件，全部进媒体清单
         （含 `属性/商品属性图.png` 这种）；
      ② **区域 = 真实目录名** —— 不再做「认不出就 `continue`」。

    区域顺序 = 数据包里目录**首次出现**的顺序（按路径排序扫一遍自然得到），
    与原实现「按 `MEDIA_GROUPS` 常量顺序」不同 —— 后者会把用户自己的目录次序
    重排成固定四区，而目录次序本身就是用户组织素材的意图。
    """
    warnings: list[str] = []
    #: 区域名 → 该区媒体（用 dict 保序 = 目录首次出现顺序）。
    by_zone: dict[str, list[PrecheckMedia]] = {}
    seen_other: set[str] = set()

    for path in sorted(stage_root.rglob("*"), key=lambda p: p.as_posix().lower()):
        if not path.is_file():
            continue
        rel = path.relative_to(stage_root).as_posix()
        suffix = path.suffix.lower()
        #: ⚠️ 用户口径 2026-10-13（**方案 C**）：「展示与上传都按平台，
        #: 但把不合规的**列出来而不是丢掉**，红框提示」。
        #:
        #: ⇒ 不合规的文件**照样进清单**（带 `unsupported=True` + `unsupported_reason`），
        #: 由前端**红框**显示并默认不勾选 —— **不静默丢弃**。
        #: 旧实现是 `continue` 掉，用户根本不知道包里有什么没被收（包 A 丢 63 个）。
        #:
        #: 唯一不进清单的是**一眼可知的非媒体文件**（表格/文本/压缩包/可执行）——
        #: 它们不是「不合规的图片」，是压根不是媒体；进了清单只会让用户看到
        #: 一堆跟商品图无关的东西（xlsx 走 `_read_tables`，自有去处）。
        if suffix in _NON_MEDIA_SUFFIXES:
            seen_other.add(rel.split("/")[0])
            continue
        is_video = suffix in VIDEO_EXTS
        zone = group_of_rel_path(rel) or ROOT_ZONE
        bucket = by_zone.setdefault(zone, [])
        #: 平台判据：视频按 `VIDEO_EXTS`，图片按 `IMAGE_EXTS`（真机：jpg/jpeg/png）。
        uploadable = is_video or suffix in IMAGE_EXTS
        reason = ""
        if not uploadable:
            reason = f"平台不支持 {suffix or '（无扩展名）'} 格式的图片（只收 jpg / jpeg / png）"
        bucket.append(
            PrecheckMedia(
                group=zone,
                index=len(bucket),
                rel_path=rel,
                file_name=path.name,
                size_bytes=path.stat().st_size,
                oversized=(not is_video) and path.stat().st_size > MAX_IMAGE_BYTES,
                unsupported=not uploadable,
                unsupported_reason=reason,
                kind=kind_of_zone(zone),
            )
        )

    #: ⚠️ **删掉「数据包里没有「主图」目录」这条告警**（原实现在此处）：
    #: 在「目录名直出」口径下它是**错的** —— 包 A 没有名为 `主图` 的目录，
    #: 但它的 `1比1主图` 就是主图资源。留着会误导用户以为包缺东西。

    media: list[PrecheckMedia] = []
    for zone, items in by_zone.items():
        media.extend(items)
    #: 不合规格式**聚合上报**（用户口径：提示简洁明了）。
    #: ⚠️ 逐个文件报会让 warnings 变成一堵墙（包 A 那样 60+ 条），
    #: 而「哪张图不合规」在媒体网格上已经标出来了（红框 + 角标 + hover 原因）
    #: ⇒ 这里只需一句话说清「有几张、什么格式、只收什么」。
    bad_formats: set[str] = set()
    bad_count = 0
    for item in media:
        if item.oversized:
            warnings.append(
                f"「{item.rel_path}」{item.size_bytes / 1048576:.1f}M "
                "超过平台单文件 5M 上限"
            )
        if item.unsupported:
            #: ⚠️ **方案 C**：不合规的**列出来**（不是丢掉）—— 但提示要**简洁**。
            #: 文件名已在媒体网格上（带「不可发布」角标 + hover 原因），
            #: 这里不必再重复一遍路径（那是噪音），只留核心结论 + 计数。
            bad_formats.add(Path(item.rel_path).suffix.lower() or "（无扩展名）")
            bad_count += 1
    if bad_count > 0:
        formats = "、".join(sorted(bad_formats))
        warnings.append(f"{bad_count} 个文件平台不支持（{formats}），仅 jpg / jpeg / png 可发布")
    return media, warnings


def _legacy_kind_counts(media: list[PrecheckMedia]) -> dict[str, int]:
    """旧契约 `media_groups` 的兼容计数（按 `kind` **参考类别**统计）。

    ⚠️ 仅用于兼容 —— 它**无法反映真实分区**（`1比1主图` 与 `页面展示主图`
    都会算进 `main`）。新代码读 `zones`。
    """
    counts: dict[str, int] = {k: 0 for k in MEDIA_KINDS}
    for item in media:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    return counts


def zone_summary(media: list[PrecheckMedia]) -> list[dict[str, object]]:
    """媒体清单 → **区域清单**（保序）；前端按它渲染分区与区域映射下拉。

    每项 `{name, kind, count}`：
      · `name` = 区域名原值（数据包里的目录名，或 `根目录`）；
      · `kind` = 参考类别（见 `kind_of_zone`），**只作提示**；
      · `count` = 该区媒体数。

    ⚠️ 顺序 = 媒体清单里区域的**首次出现顺序**（`_scan_media` 已按目录序排好）。
    """
    out: list[dict[str, object]] = []
    for item in media:
        if out and out[-1]["name"] == item.group:
            out[-1]["count"] = int(out[-1]["count"]) + 1
            continue
        hit = next((z for z in out if z["name"] == item.group), None)
        if hit is not None:
            hit["count"] = int(hit["count"]) + 1
            continue
        out.append({"name": item.group, "kind": item.kind, "count": 1})
    return out


def _read_tables(stage_root: Path) -> tuple[list[PrecheckTable], list[str]]:
    """读数据包里的**每个**表格文件 → 一比一还原的表格。

    ## 支持的文件类型（用户口径 2026-10-21：「应支持常用的大多数表格格式」）

    | 后缀 | 读取器 |
    |---|---|
    | `.xlsx` / `.xlsm` | openpyxl（每个 sheet 一张表） |
    | `.xls`（Excel 97-2003） | xlrd（每个 sheet 一张表） |
    | `.ods`（LibreOffice） | 标准库 zip + XML（每个 sheet 一张表） |
    | `.csv` / `.tsv` / `.txt` | 标准库 csv + 编码探测（一个文件一张表） |
    | `.xlsb` | pyxlsb（可选依赖） |

    ⚠️ **格式分流只在 `tabular_reader` 一处**，本函数只负责「把 SheetRows 组装成
    PrecheckTable」。两侧（预检 / 建草稿）各写一套格式判断 ⇒ 迟早出现
    「预检能读、发布读不出」这种最难查的错位。

    ⚠️ 读不出的文件（坏文件 / 编码不认识 / 库缺失）**只影响它自己**，转成 warning
    继续读下一个；一个都读不出才报错 —— 而错误文案要给出**支持范围**，不能只说
    「包里没有表格」（用户会以为放错了目录）。
    """
    from backend.app.services import tabular_reader

    warnings: list[str] = []
    tables: list[PrecheckTable] = []
    files = sorted(
        (p for p in stage_root.rglob("*") if p.is_file() and tabular_reader.is_table_file(p)),
        key=lambda p: p.name.lower(),
    )

    for path in files:
        try:
            sheets = tabular_reader.read_tables(path)
        except tabular_reader.TableReadError as error:
            warnings.append(str(error))
            continue
        for sheet in sheets:
            warnings.extend(sheet.extra_warnings)
            table = _build_table(sheet, fallback_stem=path.stem)
            if table is None:
                warnings.append(f"「{sheet.name}」没有有效表头")
                continue
            if sheet.encoding and sheet.encoding != "utf-8-sig":
                #: 编码只在「不是最常规的 UTF-8」时说一句，方便排查乱码来源（不打扰正常包）。
                warnings.append(f"「{path.name}」按 {sheet.encoding} 编码读取（非 UTF-8）")
            tables.append(table)

    if not tables:
        raise PrecheckError(
            f"数据包里没有可读取的表格（支持 {tabular_reader.SUPPORTED_HINT}）"
        )
    return tables, warnings


def _build_table(sheet: Any, *, fallback_stem: str) -> PrecheckTable | None:
    """`SheetRows` → `PrecheckTable`（**预检侧唯一组装点**）。

    列名 / 行 / 逐列类型 / 是否 SKU 表的判据都只在这里写一次。
    """
    from backend.app.services.tabular_reader import cell_text, sheet_display_name

    columns = [str(c).strip() if c is not None else "" for c in sheet.header]
    while columns and not columns[-1]:
        columns.pop()
    if not any(columns):
        return None
    width = len(columns)
    rows: list[list[str | None]] = []
    for raw in sheet.rows:
        values = [cell_text(v) for v in list(raw)[:width]]
        values += [None] * (width - len(values))
        if not any(v not in (None, "") for v in values):
            continue
        rows.append(values)
    return PrecheckTable(
        title=sheet_display_name(sheet.name, fallback=fallback_stem),
        sheet_name=str(sheet.name),
        columns=columns,
        rows=rows,
        source_file=sheet.source_file,
        #: 逐列类型（用户口径 2026-10-13）：数值列不给正则入口。
        column_kinds=[
            column_kind([r[i] for r in rows if i < len(r)]) for i in range(len(columns))
        ],
        #: 「是不是 SKU 表」按**列名特征**判，不看 sheet 名。
        is_sku_table=looks_like_sku_table(columns),
    )



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
