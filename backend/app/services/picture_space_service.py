"""图片空间（千帆 `material_space`）只读代理的**归一化层**（设计 22 §1）。

## 为什么单独一个文件

路由层只做「门禁 + 收参 + 转发 + 报错」，把平台原始字段映射成前端契约
（`display_name` / `referenced` / `width`…）的逻辑集中在这里 —— 与
`shop_products_service` / `order_*` 同一条纪律：**映射只有一份实现**（DEF-044），
页面守卫与端点契约测试都对着它写。

## ⚠️ 零落库

本模块**只读**（不写任何表、不落任何文件），是「图片空间数据不落库」这条需求
（设计 22 §1.1）的落地点。平台侧数据一律现取现渲染，本地不留副本。

## 缺值口径（硬约束⑤）

平台把「扩展名 / 尺寸 / 引用数」都塞进 `additionalData` 且都是**字符串**，
缺失时**不补 0**（缺尺寸就留空，由前端渲染 `-`）。
"""

from __future__ import annotations

from typing import Any

#: 平台 `type` 枚举：1 = 文件夹，2 = 文件（真机抓包确认）。
MATERIAL_TYPE_FOLDER = 1
MATERIAL_TYPE_FILE = 2

#: 平台的「正常」状态（回收站是页面里另一个入口）。
#: ⚠️ 真机 JS bundle `index.5dca98b8.js` 的枚举：`{USABLE:1, UPLOADING:2, UPLOAD_FAILED:3, RECYCLE_BIN:4}`。
MATERIAL_STATUS_NORMAL = 1

#: 「从回收站还原」用的 status（= `USABLE`，与上面同值，语义别名）。
MATERIAL_STATUS_USABLE = MATERIAL_STATUS_NORMAL

#: 「删除进回收站」用的 status。
MATERIAL_STATUS_RECYCLE_BIN = 4

#: `basicTypes` 的合法取值（列表端点靠它区分文件夹 / 文件）。
MATERIAL_BASIC_TYPES: tuple[int, ...] = (MATERIAL_TYPE_FOLDER, MATERIAL_TYPE_FILE)

#: 排序字段白名单（真机下拉 6 项 = 3 字段 × 升降序）。
#: ⚠️ 表外字段 **400**，不静默退化成默认 —— 平台字段名写错会静默给乱序，
#: 而用户会以为「按上传时间排了」。
MATERIAL_ORDER_FIELDS: tuple[str, ...] = ("name", "createTime", "size", "updateTime")

#: 排序字段的中文标签（前端与文档共用一份，避免各写各的）。
MATERIAL_ORDER_LABELS: dict[str, str] = {
    "name": "文件名称",
    "createTime": "上传时间",
    "size": "文件大小",
    "updateTime": "删除时间",
}

#: 单页上限（与平台页面同口径：真机下拉 100 / 200 / 500）。
MATERIAL_PAGE_SIZE_LIMIT = 500


def _text(value: Any) -> str:
    """`Any` → 去空白字符串；`None` → 空串（不造 `"None"`）。"""
    if value is None:
        return ""
    return str(value).strip()


def _int_or_none(value: Any) -> int | None:
    """尽量转 int；转不了 ⇒ `None`（**不是 0** —— 缺值口径）。"""
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def build_search_body(
    *,
    keyword: str = "",
    basic_types: tuple[int, ...] | list[int] | None = None,
    father_directory_id: str = "",
    order_field: str = "name",
    order_asc: bool = True,
    page: int = 1,
    page_size: int = 100,
    statuses: tuple[int, ...] | list[int] | None = None,
) -> dict[str, Any]:
    """按白名单组装 `search_directory_manageable` 的请求体（逐字对齐真机抓包）。

    ⚠️ 这里**不校验** `order_field` —— 白名单校验在路由层（要抛 400 结构化原因），
    本函数只负责拼装，拿到已校验的值。
    """
    return {
        "filter": {
            "keyword": str(keyword or ""),
            "statuses": list(statuses) if statuses else [MATERIAL_STATUS_NORMAL],
            "basicTypes": list(basic_types) if basic_types else list(MATERIAL_BASIC_TYPES),
            "fatherDirectoryId": str(father_directory_id or ""),
        },
        "order": {"orderList": [{"field": str(order_field), "asc": bool(order_asc)}]},
        "pageIndex": max(1, int(page)),
        "pageSize": max(1, min(int(page_size), MATERIAL_PAGE_SIZE_LIMIT)),
        "option": {"withDetail": True},
    }


def normalize_material_item(raw: dict[str, Any]) -> dict[str, Any]:
    """平台条目 → 前端契约（**唯一实现**）。

    | 平台字段 | 归一化字段 | 口径 |
    |---|---|---|
    | `name` + `additionalData.extension` | `display_name` | 平台把扩展名拆走 ⇒ 后端拼回全名给列表显示 |
    | `size` | `size` | 字节（int；缺失 ⇒ 0，因为「大小」列必须有数） |
    | `additionalData.width` / `height` | `width` / `height` | int；**缺失 ⇒ `None`**（前端渲染 `-`，不补 0） |
    | `additionalData.link` | `link` | 直链（无扩展名，可直接当 `<img src>`） |
    | `additionalData.productCount` | `product_count` + `referenced` | `> 0` ⇒ 已引用 |
    | `createTime` | `create_time` | epoch ms（原样透传，前端负责格式化） |
    | `type` | `type` | 1=文件夹 2=文件 |
    """
    detail = raw.get("additionalData") or {}
    if not isinstance(detail, dict):
        detail = {}
    base_name = _text(raw.get("name"))
    extension = _text(detail.get("extension"))
    product_count = _int_or_none(detail.get("productCount")) or 0
    return {
        "id": _text(raw.get("id")),
        "type": _int_or_none(raw.get("type")),
        "name": base_name,
        "display_name": f"{base_name}{extension}",
        "extension": extension,
        "size": _int_or_none(raw.get("size")) or 0,
        "width": _int_or_none(detail.get("width")),
        "height": _int_or_none(detail.get("height")),
        "link": _text(detail.get("link")),
        "product_count": product_count,
        "referenced": product_count > 0,
        "create_time": _int_or_none(raw.get("createTime")),
        "update_time": _int_or_none(raw.get("updateTime")),
    }


def normalize_search_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """`search_directory_manageable` 的 `data` → 前端契约。

    返回 `{total, items, page, page_size}`；`total` 缺失 ⇒ 0（列表为空是**合法**结果，
    不报错 —— 与 `shop_products` 的「空台账返回空列表」同口径）。
    """
    data = (payload or {}).get("data") or {}
    if not isinstance(data, dict):
        data = {}
    raw_items = data.get("directoryManageables") or []
    if not isinstance(raw_items, list):
        raw_items = []
    items = [normalize_material_item(item) for item in raw_items if isinstance(item, dict)]
    return {
        "total": _int_or_none(data.get("total")) or 0,
        "items": items,
    }


def normalize_usage_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """`get_seller_space_usage` 的 `data` → 前端契约。

    ⚠️ 配额取不到时两个值都是 `None` —— 前端据此**不渲染**配额条（不显示 `0 / 0`，
    那会让人以为空间是空的）。
    """
    data = (payload or {}).get("data") or {}
    if not isinstance(data, dict):
        data = {}
    return {
        "used_space": _int_or_none(data.get("usedSpace")),
        "total_space": _int_or_none(data.get("totalSpace")),
        "white_seller": bool(data.get("whiteSeller")),
    }


def normalize_resize_conditions(payload: dict[str, Any] | None) -> dict[str, Any]:
    """`check_resize_material_space` 的 `data` → 前端契约（2026-10-07 空间扩容）。

    ⚠️ **真机抓包实测**（`POST material_space/check_resize_material_space` 原始响应）：

    ```json
    {"code":0,"success":true,"msg":"成功","data":{"condition_results":[
      {"type":1,"desc":"有在架商品","check_pass":true},
      {"type":2,"desc":"近30天有动销（消费者支付金额>0）","check_pass":false},
      {"type":3,"desc":"容量使用率>=80%","check_pass":false,"threshold":"80","current":"3"},
      {"type":4,"desc":"剩余扩容次数>0","check_pass":true,"current":"4","threshold":"4"}]}}
    ```

    ⇒ 键名是 **snake_case**（`condition_results` / `check_pass`）—— 平台页面 JS 带 axios
    `transform:true` 才转成驼峰，我们走原始 JSON 所以必须读下划线（曾按驼峰读 = 恒空）。

    ⚠️ **颜色 = 平台逐条返回的 `check_pass`**（2026-10-07 真机两账号对照实测）：弹窗那行
    「近30天有动销」的颜色**不是我方判定的**，而是平台说 `check_pass=false` ⇒ 红。用户截图
    看着像绿色是视觉误判 —— 我方一律**照 `check_pass` 上色**，不做二次推断。

    * 每条 `desc` 是平台原文（**原样透传，不造**）；
    * `type=4` 的 `current` = 「剩余扩容次数」（平台页脚「剩余扩容次数：N次」）；
    * `can_apply` = 全部条件通过 ∧ 非白名单卖家。
    """
    data = (payload or {}).get("data") or {}
    if not isinstance(data, dict):
        data = {}
    raw_items = data.get("condition_results")
    if not isinstance(raw_items, list):
        raw_items = []
    conditions: list[dict[str, Any]] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        conditions.append(
            {
                "type": _int_or_none(it.get("type")),
                "pass": bool(it.get("check_pass")),
                "desc": _text(it.get("desc")),
                "current": _text(it.get("current")),
            }
        )
    all_pass = bool(conditions) and all(c["pass"] for c in conditions)
    remaining = 0
    for c in conditions:
        if c["type"] == 4:
            try:
                remaining = max(0, int(str(c["current"]).strip() or "0"))
            except (TypeError, ValueError):
                remaining = 0
            break
    return {
        "conditions": conditions,
        "can_apply": all_pass and not bool(data.get("whiteSeller")),
        "white_seller": bool(data.get("whiteSeller")),
        "remaining": remaining,
    }


def normalize_directory_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """`get_directory` 的 `data.directoryManageable`（根节点）→ 前端契约。

    本页只做一层文件夹树（真机根节点 `id="0"` / `name="我的图片"`）。
    """
    data = (payload or {}).get("data") or {}
    root = (data.get("directoryManageable") if isinstance(data, dict) else None) or {}
    if not isinstance(root, dict):
        root = {}
    detail = root.get("additionalData") or {}
    if not isinstance(detail, dict):
        detail = {}
    return {
        "id": _text(root.get("id")) or "0",
        "name": _text(root.get("name")) or "我的图片",
        "seller_id": _text(root.get("sellerId")),
        "is_leaf": _text(detail.get("isLeaf")).lower() == "true",
        "product_count": _int_or_none(detail.get("productCount")) or 0,
    }


# --------------------------------------------------------------------------- #
# 写链路（设计 22 §1.5，2026-10-06「操作按钮功能全部要做」）
#
# ⚠️ 平台要求**回传完整条目**（不是只传 id）：`batch_edit` / `batch_delete` /
# `batch_download` 的 `directoryManageables[]` 都是「原条目 + 改动」。
# 所以前端提交时要带够字段，本层负责拼成平台要的形状（**唯一实现**）。
# --------------------------------------------------------------------------- #

#: 单次批量操作上限。
#: ⚠️ 平台未实测拒绝过大值；与既有商品批量（`MAX_BATCH_*`）同口径保守取 100。
MATERIAL_BATCH_LIMIT = 100


def build_add_item(seller_id: str, name: str, *, level: int = 0) -> dict[str, Any]:
    """新建文件夹时**一条** `directoryManageables` 条目（真机 JS bundle 形状）。

    ```json
    {"sellerId": …,"type":1,"name":"<文件夹名>","size":0,
     "additionalData":{"level":"0","productCount":"0","isLeaf":"true"}}
    ```
    """
    return {
        "sellerId": str(seller_id),
        "type": MATERIAL_TYPE_FOLDER,
        "name": str(name),
        "size": 0,
        "additionalData": {
            "level": str(level),
            "productCount": "0",
            "isLeaf": "true",
        },
    }


def build_move_item(raw: dict[str, Any], father_directory_id: str) -> dict[str, Any]:
    """移动时**一条**条目的平台形状 —— `{…原条目…, fatherDirectoryId: <目标>}`。

    ⚠️ **2026-10-07 真机 JS bundle `332.d754c099.js` 取证**（`case 3`）：

    ```js
    {sellerId, directoryManageables: [{...item, fatherDirectoryId: <K>}], option:{async:true}}
    ```

    移动**只改 `fatherDirectoryId`**、**不动 `status`**。
    ⚠️ 曾经的错误实现把移动也置 `status:4` ⇒ `4 = RECYCLE_BIN` ⇒ **把文件移进了回收站**
    （数据丢失级 bug，真机语义与代码相反）。已纠正并由测试钉死。
    """
    item = dict(raw or {})
    item["fatherDirectoryId"] = str(father_directory_id or "0")
    # 移动**不得**带 status（带了会被平台当删除/还原处理）。
    item.pop("status", None)
    return item


def build_delete_item(raw: dict[str, Any]) -> dict[str, Any]:
    """删除时一条条目的平台形状 —— `{…原条目…, status: RECYCLE_BIN}`（= 进回收站）。

    ⚠️ **2026-10-07 真机 JS bundle `332.d754c099.js` 取证**（`case 14`）：
    批量删除 = `batch_edit` + `{...item, status: y7.RECYCLE_BIN}`（`4`）。
    `batch_delete_directory_manageable` 是**彻底删除**（不可还原），键名不同，见
    `build_permadelete_elements`。

    ⇒ 保留 `id` / `type` / `name` / `additionalData` 等原字段，只改 `status`。
    """
    item = dict(raw or {})
    item["status"] = MATERIAL_STATUS_RECYCLE_BIN
    return item


def build_restore_item(raw: dict[str, Any]) -> dict[str, Any]:
    """还原时一条条目的平台形状 —— `{…原条目…, status: USABLE}`（= 从回收站还原）。

    ⚠️ **2026-10-07 真机 JS bundle `417.d61d3cd8.js` 取证**：
    恢复 = `batch_edit` + `{...item, status: y7.USABLE}`（`1`），与删除同一端点、只差 status。
    """
    item = dict(raw or {})
    item["status"] = MATERIAL_STATUS_USABLE
    return item


def build_permadelete_elements(raw_items: "list[dict] | tuple[dict, ...]") -> list[dict[str, Any]]:
    """**彻底删除**的请求元素 —— 只 `{id, type}`（真机 JS bundle 逐字）。

    发出时放进 `directoryManageableElements` 键（**不是** `directoryManageables`）。

    ⚠️ **2026-10-07 真机 JS bundle `417.d61d3cd8.js` 取证**：
    `{sellerId, directoryManageableElements:[{id, type}], option:{async:true}}`。
    单条与批量同形（单条时 `[ {id:e.id, type:e.type} ]`）。
    """
    out: list[dict[str, Any]] = []
    for it in raw_items or []:
        item = it if isinstance(it, dict) else {}
        out.append({"id": _text(item.get("id")), "type": _int_or_none(item.get("type"))})
    return out


def build_download_links(raw_items: "list[dict] | tuple[dict, ...]") -> list[dict[str, Any]]:
    """**批量下载**的 `links[]`（真机 JS bundle `332.d754c099.js` `case 9` 逐字）。

    * 文件夹：`{"type": 1, "directoryId": "<id>"}`；
    * 文件：  `{"type": 2, "link": "<https url>", "name": "<名>", "extension": "<.ext>"}`。

    ⚠️ 请求键是 `links`（不是 `directoryManageables`）。
    """
    out: list[dict[str, Any]] = []
    for it in raw_items or []:
        item = it if isinstance(it, dict) else {}
        item_type = _int_or_none(item.get("type"))
        if item_type == MATERIAL_TYPE_FOLDER:
            out.append({"type": MATERIAL_TYPE_FOLDER, "directoryId": _text(item.get("id"))})
        else:
            out.append(
                {
                    "type": MATERIAL_TYPE_FILE,
                    "link": _text(item.get("link")),
                    "name": _text(item.get("name")),
                    "extension": _text(item.get("extension")),
                }
            )
    return out


def validate_batch(items: "list[dict] | tuple[dict, ...]") -> None:
    """批量参数校验（服务层，抛 `ValueError` 由路由转 400）。

    * 空 ⇒ 报错（平台侧也是 `文件信息不能为空`，提前拦更省一次请求）；
    * 超上限 ⇒ 报错；每条必须有 `id`（平台认 id）。
    """
    if not items:
        raise ValueError("MATERIAL_EMPTY_SELECTION: 请先勾选文件")
    if len(items) > MATERIAL_BATCH_LIMIT:
        raise ValueError(
            f"MATERIAL_BATCH_TOO_LARGE: 一次最多操作 {MATERIAL_BATCH_LIMIT} 条，"
            f"当前 {len(items)} 条"
        )
    for item in items:
        if not _text((item or {}).get("id")):
            raise ValueError("MATERIAL_ITEM_NO_ID: 条目缺少 id（请刷新后重试）")


def validate_folder_name(name: str, existing: "list[str] | tuple[str, ...] | None" = None) -> str:
    """文件夹名校验（平台输入框 `maxlength=20`）：去空白 + 非空 + ≤20 + 不重名。"""
    value = _text(name)
    if not value:
        raise ValueError("MATERIAL_NAME_REQUIRED: 文件夹名不能为空")
    if len(value) > 20:
        raise ValueError("MATERIAL_NAME_TOO_LONG: 文件夹名最多 20 个字")
    if existing and value in {_text(n) for n in existing}:
        raise ValueError("MATERIAL_NAME_DUPLICATE: 已有同名文件夹")
    return value


def extract_items(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """从**归一化后**的条目列表里取出平台原始字段（写操作要回传原条目）。

    前端提交的是「前端契约」条目（`display_name` / `referenced`…），写端点需要
    **平台字段**（`additionalData` / `createTime`…）。为避免让前端原样回传平台
    原始 JSON（既冗长又易被篡改），本函数把前端条目**反推**成平台要的最小形状：
    `id` / `type` / `name` / `size` / `additionalData.{extension,width,height,link,productCount}`。
    """
    out: list[dict[str, Any]] = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        out.append(_normalize_to_platform_item(item))
    return out


def _normalize_to_platform_item(item: dict[str, Any]) -> dict[str, Any]:
    """前端条目 → 平台条目（写操作回传用）。字段名对齐平台抓包。"""
    return {
        "id": _text(item.get("id")),
        "type": _int_or_none(item.get("type")),
        "name": _text(item.get("name")),
        "size": _int_or_none(item.get("size")) or 0,
        "additionalData": {
            "extension": _text(item.get("extension")),
            "width": _text(item.get("width")) if item.get("width") is not None else "",
            "height": _text(item.get("height")) if item.get("height") is not None else "",
            "link": _text(item.get("link")),
            "productCount": str(_int_or_none(item.get("product_count")) or 0),
        },
    }


def build_upload_item(
    seller_id: str,
    *,
    file_id: str,
    file_name: str,
    size: int,
    width: int | None = None,
    height: int | None = None,
    preview_url: str = "",
    father_directory_id: str = "0",
) -> dict[str, Any]:
    """上传后**登记**图片空间时一条 `type:2` 条目（真机抓包 `batch_add` 请求体）。

    ⚠️ 真机实抓的形状（2026-10-07）——与初版差异极大：

    ```json
    {"sellerId":"…","type":2,"fatherDirectoryId":"0","name":"123","size":254242,
     "additionalData":{"width":"1440","height":"1440","extension":".jpg",
       "path":"arkgoods/<id>",
       "uploaderInfoModel":{"url":"https://ros-preview.xhscdn.com/arkgoods/<id>?sign=…&t=…",
         "bizName":"ark","scene":"goods_image","fileId":"arkgoods/<id>",
         "isSecret":true,"cloudType":4}}}
    ```

    * `path` = **预分配 `fileId` 原样**（`arkgoods/<id>`）——**不**加 `material_space/` 前缀；
    * `uploaderInfoModel` 是**对象**（含 PUT 响应头给回的**带签名** `url`），不是字符串；
    * `name` = **无扩展名基名**（扩展名单独放 `additionalData.extension`）。
    """
    base, _, ext = str(file_name).rpartition(".")
    name = base if base and ext else str(file_name)
    extension = f".{ext}" if base and ext else ""
    # `path` 就是预分配 fileId 本身（`arkgoods/<id>` 或 `spectrum/<id>`）。
    path = str(file_id or "").strip()
    additional: dict[str, Any] = {
        "extension": extension,
        "path": path,
        "productCount": "0",
        # 平台按这个「上传者信息」把对象登记到云并发布公开链接；`url` 必须是 PUT 时
        # 响应头 `X-Ros-Preview-Url` 给的**带签名**地址（缺它 ⇒ 条目在、取图 404）。
        "uploaderInfoModel": {
            "url": str(preview_url or ""),
            "bizName": "ark",
            "scene": "goods_image",
            "fileId": path,
            "isSecret": True,
            "cloudType": 4,
        },
    }
    # ⚠️ **尺寸缺值时不要写空串**（2026-10-07 真机 bug「For input string: ""」）：
    # 平台是 Java 后端，`additionalData.width/height` 会被 `Integer.parseInt` 解析；
    # 传 `""` ⇒ `NumberFormatException（For input string: ""）` ⇒ 上传整单失败。
    # 拿不到尺寸就**整个键不发**（缺值口径：不造假、也不送空串）。
    if width is not None:
        additional["width"] = str(int(width))
    if height is not None:
        additional["height"] = str(int(height))
    return {
        "sellerId": str(seller_id),
        "type": MATERIAL_TYPE_FILE,
        "fatherDirectoryId": str(father_directory_id or "0"),
        "name": name,
        "size": int(size or 0),
        "additionalData": additional,
    }
