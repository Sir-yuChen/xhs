"""商品数据包解析 + 草稿体组装（设计 23）。

## 这个模块做两件事，一件也不多做

1. **解析本地商品数据包**（`parse_package`）—— **纯本地，零平台请求**：
   读 xlsx（SKU 表）+ 列目录下的图片 / 视频名。
2. **组装草稿体**（`build_draft_body`）—— 纯函数，把「已上传的图片条目 +
   SKU 列表」拼成平台草稿体。

> ⚠️ **为什么上传不在这里**：上传是写操作（要账号令牌、要限流、要记账），
> 属于服务层的执行链（`shop_draft_service`）；本模块只做**可单测的纯逻辑**
> —— 数据包解析与草稿体组装都是「进 → 出」的确定映射，用假数据就能测透。

## 数据包结构（2026-10-07 用户实测形态，按实际写死）

```
<包目录>/
  <任意名>.xlsx      # SKU 表：列 = sku名称 | 图片 | 原价 | 计算价格 | 库存 | ID
  SKU/              # SKU 规格图，文件名形如 `SKU01_颜色分类(…).jpg`
  主图/              # 主图（目录里的图**全部**计入主图）
  详情图/            # 详情图 / 长图
  视频/              # 视频
```

⚠️ **不排除任何媒体资源**（用户口径 2026-10-11 最终版：「数据包中有什么媒体资源
就展示什么，不要排除任何媒体资源」）⇒ `主图/` 里有一张就传一张，
曾按文件名剔除「属性图 / 参数图」的做法已撤销。
因为平台草稿体**不接受 `properties`**（真机实测静默丢弃），属性只能由用户
在千帆页面手工录入。这里**如实列进 `warnings`** 提醒，不假装能处理。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

#: 图片扩展名白名单（平台只收 png/jpg/jpeg，见千帆新增商品页原文
#: 「支持 png/jpg/jpeg 格式；大小不超过 5M」）。视频单列。
IMAGE_EXTS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png"})
#: 可识别的视频格式。
#:
#: ⚠️ 用户口径 2026-10-08：「应支持**多种视频格式**预览播放」。
#: 分两类看：
#: - **浏览器原生能播**（`<video>`）：mp4/m4v/webm/ogv —— 这些只需正确 MIME 就能放；
#: - **浏览器多半放不了**（h264-in-avi/mkv、flv、wmv、ts 等）：仍要**识别**出来
#:   （抽帧 OCR 照样能做，ffmpeg 认），预览时明确提示「当前浏览器不支持该格式，
#:   请下载后用本地播放器打开」而不是给个空白框。
VIDEO_EXTS: frozenset[str] = frozenset(
    {
        # 原生可播
        ".mp4", ".m4v", ".mov", ".webm", ".ogv", ".ogg",
        # 可抽帧但浏览器通常放不了
        ".avi", ".mkv", ".flv", ".wmv", ".ts", ".mpg", ".mpeg", ".3gp",
    }
)

#: **平台单张图片大小上限 5M**（新增商品页原文）。超限的图如实进 `warnings`
#: 并**不上传** —— 传了平台必拒，白耗一次写请求。
MAX_IMAGE_BYTES: int = 5 * 1024 * 1024

#: 数据包里的目录名（用户实际形态；大小写不敏感匹配）。
DIR_SKU = "sku"
DIR_MAIN = "主图"
DIR_DETAIL = "详情图"
DIR_VIDEO = "视频"


#: xlsx 表头 → 内部键。**按表头名匹配，不按列序** ——
#: 列序是最容易漂移的东西（用户重排一次列就全错位，而且错得静默）。
_XLSX_COLUMNS: dict[str, str] = {
    "sku名称": "name",
    "sku名": "name",
    "图片": "image",
    "原价": "original_price",
    "计算价格": "price",
    "价格": "price",
    "库存": "stock",
    "id": "item_id",
}

#: SKU 名里的规格分隔符：半角优先（真机数据如此），全角也吃（用户手输会混）。
_VARIANT_SEPARATORS: tuple[str, ...] = (":", "：")


class PackageError(ValueError):
    """数据包不可解析（目录不存在 / 没有 xlsx / 一行 SKU 都没有）。"""


def _list_media(directory: Path, exts: frozenset[str]) -> list[str]:
    """列目录下指定扩展名的文件（按文件名排序；目录不存在 ⇒ 空）。

    ⚠️ **排序必须稳定**：图片顺序 = 主图顺序（平台第一张是封面），
    靠 `os.listdir` 的返回顺序会得到随机的封面。
    """
    if not directory.is_dir():
        return []
    files = [
        str(p)
        for p in sorted(directory.iterdir(), key=lambda p: p.name)
        if p.is_file() and p.suffix.lower() in exts
    ]
    return files


def _read_xlsx_rows(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """读 SKU 表 → (行列表, warnings)。

    用 `openpyxl`（仓库已有依赖）。表头按 `_XLSX_COLUMNS` 匹配；
    **未识别的列进 warnings**（如实告知「这一列没用上」比静默忽略好）。
    """
    warnings: list[str] = []
    try:
        import openpyxl
    except ImportError:  # pragma: no cover - 依赖缺失是环境问题，如实报
        return [], ["缺少 openpyxl 依赖，无法读取 xlsx"]

    try:
        book = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as error:  # noqa: BLE001 - 各种坏文件都要变成可读原因
        return [], [f"读取 xlsx 失败：{type(error).__name__}: {error}"]

    rows: list[dict[str, Any]] = []
    try:
        sheet = book.worksheets[0]
        iterator = sheet.iter_rows(values_only=True)
        header = next(iterator, None)
        if header is None:
            return [], ["xlsx 是空表（没有表头行）"]
        # 表头 → 列下标
        index_of: dict[str, int] = {}
        for position, raw in enumerate(header):
            text = str(raw or "").strip().lower()
            key = _XLSX_COLUMNS.get(text)
            if key and key not in index_of:
                index_of[key] = position
        if "name" not in index_of:
            return [], ["xlsx 里找不到「sku名称」列，无法识别 SKU（请检查表头）"]

        unknown = [
            str(c).strip()
            for c in header
            if str(c or "").strip() and str(c).strip().lower() not in _XLSX_COLUMNS
        ]
        if unknown:
            warnings.append("以下列未被使用：" + "、".join(unknown))

        for raw_row in iterator:
            if raw_row is None:
                continue
            values = list(raw_row)

            def cell(key: str) -> Any:
                pos = index_of.get(key)
                if pos is None or pos >= len(values):
                    return None
                return values[pos]

            name = str(cell("name") or "").strip()
            if not name:
                continue  # 空行（表尾常见）—— 跳过不算错
            #: ⚠️ 价格**两列取一个**：数据包的 `计算价格` 常为空（真机实测
            #: 26 行全空），真正有值的是 `原价`。只认 `计算价格` 会让整批商品
            #: 的价格全空 —— 而草稿建完看不出来（价格留空是合法状态）。
            #: 优先 `计算价格`（它是「算过的最终价」），空则回退 `原价`。
            price = _to_float(cell("price"))
            if price is None:
                price = _to_float(cell("original_price"))
            rows.append(
                {
                    "name": name,
                    "image": str(cell("image") or "").strip(),
                    "original_price": _to_float(cell("original_price")),
                    "price": price,
                    "stock": _to_int(cell("stock")),
                    "item_id": str(cell("item_id") or "").strip(),
                }
            )
    finally:
        try:
            book.close()
        except Exception:  # noqa: BLE001
            pass

    if not rows:
        warnings.append("xlsx 里没有解析到任何 SKU 行（除表头外为空）")
    return rows, warnings


def _to_float(value: Any) -> float | None:
    """尽量转 float；转不了返回 None（缺值口径：**不造假、不填 0**）。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    """尽量转 int（浮点 200.0 → 200）；转不了返回 None。"""
    number = _to_float(value)
    if number is None:
        return None
    return int(number)


def split_sku_name(name: str) -> tuple[str, str]:
    """SKU 名 → (规格名, 规格值)。

    真机数据形如 `颜色分类:【中古风】实木三斗柜` ⇒ (`颜色分类`, `【中古风】实木三斗柜`)。
    **半角冒号优先、全角也吃**（用户手输会混）。

    拆不出分隔符 ⇒ 规格名留空、整串当规格值（**不猜**是哪个规格）——
    调用方据此走「规格名待补」的提示路径，而不是编一个「颜色分类」出来。
    """
    text = str(name or "")
    for separator in _VARIANT_SEPARATORS:
        head, sep, tail = text.partition(separator)
        if sep and head.strip() and tail.strip():
            return head.strip(), tail.strip()
    return "", text.strip()


def parse_package(package_path: str) -> dict[str, Any]:
    """解析商品数据包（**纯本地，零平台请求**）。

    返回 `sku_rows` / `main_images` / `detail_images` / `videos` /
    `suggested_title` / `warnings`（逐条如实，不静默丢任何东西）。

    ⚠️ **不用「智能猜目录」**：目录名按实测形态匹配（大小写不敏感），
    找不到就进 `warnings` 说清「哪个目录不存在」，而不是把 `详情图`
    当 `主图` 用 —— 那会把顺序搞乱，而顺序就是封面。
    """
    root = Path(str(package_path or "").strip())
    warnings: list[str] = []
    if not root.is_dir():
        raise PackageError(f"数据包目录不存在：{package_path}")

    # 1) xlsx（取第一个；多个时提示）
    xlsx_files = sorted(root.glob("*.xlsx"))
    if not xlsx_files:
        raise PackageError(f"数据包目录里没有 .xlsx（SKU 表）：{package_path}")
    if len(xlsx_files) > 1:
        warnings.append(
            f"目录里有 {len(xlsx_files)} 个 xlsx，只读了第一个：{xlsx_files[0].name}"
        )
    sku_rows, xlsx_warnings = _read_xlsx_rows(xlsx_files[0])
    warnings.extend(xlsx_warnings)
    if not sku_rows:
        raise PackageError("SKU 表里没有任何有效行，无法新增商品")

    # 2) 目录（大小写不敏感匹配）
    subdirs = {p.name.lower(): p for p in root.iterdir() if p.is_dir()}

    def find(name: str) -> Path | None:
        return subdirs.get(name.lower())

    main_dir, detail_dir, sku_dir, video_dir = (
        find(DIR_MAIN), find(DIR_DETAIL), find(DIR_SKU), find(DIR_VIDEO)
    )
    for dirname, path in (
        (DIR_MAIN, main_dir), (DIR_DETAIL, detail_dir), (DIR_SKU, sku_dir), (DIR_VIDEO, video_dir)
    ):
        if path is None:
            warnings.append(f"数据包里没有「{dirname}」目录")

    # 3) 主图 + 大小校验
    #: ⚠️ 用户口径 2026-10-11（**最终版**）：「数据包中有什么媒体资源就展示什么，
    #: **不要排除任何媒体资源**」⇒ 主图目录里的**所有**图都进主图（含属性图）。
    main_images: list[str] = []
    for path_str in _list_media(main_dir, IMAGE_EXTS) if main_dir else []:
        if Path(path_str).stat().st_size > MAX_IMAGE_BYTES:
            warnings.append(
                f"「{Path(path_str).name}」超过平台单图 5M 上限，已跳过上传"
            )
            continue
        main_images.append(path_str)

    detail_images = [
        p for p in (_list_media(detail_dir, IMAGE_EXTS) if detail_dir else [])
        if Path(p).stat().st_size <= MAX_IMAGE_BYTES
    ]
    oversized_detail = [
        p for p in (_list_media(detail_dir, IMAGE_EXTS) if detail_dir else [])
        if Path(p).stat().st_size > MAX_IMAGE_BYTES
    ]
    for path_str in oversized_detail:
        warnings.append(f"「{Path(path_str).name}」超过平台单图 5M 上限，已跳过上传")

    videos = _list_media(video_dir, VIDEO_EXTS) if video_dir else []
    if videos:
        warnings.append(
            f"数据包含 {len(videos)} 个视频 —— 视频不在本期草稿体支持范围"
            "（草稿体只接受类目/名称/主图/详情图/SKU），需在千帆页面手工上传"
        )

    # 4) SKU 图按前缀顺序对上（`SKU01_…` ⇒ 第 1 行）
    sku_images = _list_media(sku_dir, IMAGE_EXTS) if sku_dir else []
    for row, path_str in zip(sku_rows, sku_images):
        row["image_path"] = path_str
    if sku_images and len(sku_images) != len(sku_rows):
        warnings.append(
            f"SKU 图 {len(sku_images)} 张与 SKU 行 {len(sku_rows)} 条数量不一致，"
            "已按顺序一一对应（多余的图未使用）"
        )

    # 5) 价格缺值提示（**不填 0** —— 0 元商品比空值更危险）
    missing_price = [r["name"] for r in sku_rows if r.get("price") is None]
    if missing_price:
        warnings.append(
            f"{len(missing_price)} 条 SKU 没有价格（如「{missing_price[0][:20]}…」），"
            "草稿里价格会留空，需在千帆页面补填"
        )

    return {
        "package_path": str(root),
        "sku_rows": sku_rows,
        "main_images": main_images,
        "detail_images": detail_images,
        "videos": videos,
        "suggested_title": _suggest_title(root, sku_rows),
        "warnings": warnings,
    }


def _suggest_title(root: Path, sku_rows: list[dict[str, Any]]) -> str:
    """给一个**可改的**建议标题。

    ⚠️ 不「智能生成」：只做两件确定的事 ——
    ① 从第一条 SKU 名里剥掉「规格名:」前缀（那不是我商品标题）；
    ② 剥掉后为空就退回目录名。**都不做拼接**（拼接出来的标题等于造数据）。
    """
    for row in sku_rows:
        _, value = split_sku_name(str(row.get("name") or ""))
        if value:
            return value[:60]
    name = root.name.strip()
    return name[:60] if name else ""


def build_sku_entry(
    *,
    name: str,
    price_yuan: float | None,
    stock: int | None,
    variant_id: str,
    row_number: int,
    image: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """一条草稿 SKU（**真机回读验证过的形状**，设计 23 §4.3）。

    ⚠️ **`price` 的单位是「元」不是「分」**（与商品列表 `price_from_fen` 相反）。
    真机：传 `1280` 回读 `price: 1280`。写成分不报错，但价格会变成 12.8 万元 ——
    静默错值，是本链路最贵的一个坑。

    ⚠️ **字段名是 `variants` 不是 `skuVariantInfos`**（后者是**读**形态）。
    写形态由 bundle `8091.31a2a571.js` 的 `itemFormSku.getFormData` 逐字逆出。
    """
    variant_name, variant_value = split_sku_name(name)
    variants: list[dict[str, Any]] = []
    if variant_id:
        variants.append(
            {
                "attributeId": str(variant_id),
                "attributeName": variant_name,
                "attributeType": 1,
                "attributeValue": variant_value,
                "attributeValueId": "",
            }
        )
    entry: dict[str, Any] = {
        "rowNumber": int(row_number),
        "ipq": 1,
        "available": True,
        "price": {
            #: 缺值就不写那个键（平台按可选处理），**不填 0** ——
            #: 0 元商品会被平台当成真实价格收下。
            **({"price": float(price_yuan)} if price_yuan is not None else {}),
            **({"originPrice": float(price_yuan)} if price_yuan is not None else {}),
            "includeTax": False,
            "taxType": 0,
        },
        "stock": int(stock or 0),
        "stockChange": int(stock or 0),
        "barcode": "",
        "erpCode": "",
        "variants": variants,
    }
    if image:
        entry["specImage"] = image
    return entry


def build_image_entry(material: dict[str, Any]) -> dict[str, Any]:
    """一张图片的**草稿体条目**（回读命中：`standardImages` 2 张 → 2）。

    `material` 是图片空间条目的归一化形态（`link` / `path` / `extension` /
    `width` / `height` / `materialId` / `size`）。

    ⚠️ `sourceLink` 用 **http**（平台字段本身是 http，真机回读如此）——
    不是笔误，写成 https 平台也收，但回读会对不上，别「顺手修正」它。
    """
    link = str(material.get("link") or "").strip()
    entry: dict[str, Any] = {
        "link": link,
        "path": str(material.get("path") or "").strip(),
        "extension": str(material.get("extension") or "").strip(),
        "materialId": str(material.get("materialId") or "").strip(),
        "size": int(material.get("size") or 0),
        "sourceLink": link.replace("https://", "http://", 1),
    }
    width, height = _to_int(material.get("width")), _to_int(material.get("height"))
    #: ⚠️ 尺寸拿不到就**整个键不发**（设计 22 的 `For input string: ""` 教训：
    #: 平台 Java 后端 `Integer.parseInt("")` ⇒ NumberFormatException 整单失败）。
    if width is not None:
        entry["width"] = width
    if height is not None:
        entry["height"] = height
    return entry


def build_draft_body(
    *,
    category_id: str,
    name: str,
    main_images: list[dict[str, Any]],
    detail_images: list[dict[str, Any]],
    skus: list[dict[str, Any]],
) -> dict[str, Any]:
    """组装草稿体（**只含平台真正接受的五个键**，设计 23 §4.3）。

    ⚠️ **不要往里加 `itemName` / `properties` / `images` / `imageDescriptions` /
    `skuList`** —— 真机实测这五个键被平台**静默丢弃**（接口 200、回读为空）。
    加了不会报错，只会让人以为存上了。守卫 `test_product_draft.py` 钉住这一点。
    """
    return {
        "categoryId": str(category_id),
        "name": str(name),
        "standardImages": list(main_images),
        "longImages": list(detail_images),
        "skuData": list(skus),
    }


def diff_draft_readback(
    submitted: dict[str, Any], readback: dict[str, Any]
) -> list[str]:
    """把「提交的字段」与「回读的字段」做差集 → 人话列表。

    ⚠️ **这一步不是可选的**（设计 23 §4.2）：真机实测「接口 200 但字段被静默
    丢弃」是常态。不校验就是「接口说成功、草稿是空的」，而用户要到千帆页面
    里才会发现 —— 那时已经过了几十分钟。

    只比**数量与存在性**（不逐值比 —— 平台会规范化价格 / 补默认值，
    逐值比对会把正常行为报成差异）。
    """
    problems: list[str] = []
    if str(submitted.get("categoryId") or "") != str(readback.get("category_id") or ""):
        problems.append("类目没有落到草稿里")
    if not str(readback.get("name") or "").strip():
        problems.append("草稿名为空（平台没有接受 name）")
    if len(submitted.get("standardImages") or []) != int(readback.get("image_count") or 0):
        problems.append(
            f"主图数量不符：提交 {len(submitted.get('standardImages') or [])} 张，"
            f"草稿里 {int(readback.get('image_count') or 0)} 张"
        )
    if len(submitted.get("longImages") or []) != int(readback.get("long_image_count") or 0):
        problems.append(
            f"详情图数量不符：提交 {len(submitted.get('longImages') or [])} 张，"
            f"草稿里 {int(readback.get('long_image_count') or 0)} 张"
        )
    if len(submitted.get("skuData") or []) != int(readback.get("sku_count") or 0):
        problems.append(
            f"SKU 数量不符：提交 {len(submitted.get('skuData') or [])} 条，"
            f"草稿里 {int(readback.get('sku_count') or 0)} 条"
        )
    return problems


def _variant_match_key(variant_name: str) -> str:
    """规格名归一化（比较用）：去空白 + 转小写（中英文混排时英文大小写不敏感）。"""
    return re.sub(r"\s+", "", str(variant_name or "")).lower()


__all__ = [
    "PackageError",
    "IMAGE_EXTS",
    "VIDEO_EXTS",
    "MAX_IMAGE_BYTES",
    "build_draft_body",
    "build_image_entry",
    "build_sku_entry",
    "diff_draft_readback",
    "parse_package",
    "split_sku_name",
]
