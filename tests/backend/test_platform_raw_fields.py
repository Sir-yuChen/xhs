"""商品详情接口的「平台原始数据」契约。

## 为什么要这组断言

1. **视频曾整条链路缺失**（2026-10-27）：平台 `search_item_v2` 下发 `videos`
   （实测某商品 1 条 / 13 秒 / 720×960 / 2.18MB），但 `product_image_links` 那套
   只处理 `images` 与 `image_descriptions`，视频从 service 到端点**没人取**，
   界面上也就永远没有。这是「平台有、我们丢了」的静默数据丢失。

2. **用户口径「根据 json 自动生成一张实时表，展示接口返回的所有数据」**：
   端点必须把平台原始行**整行**带出（`raw_fields`），而不是由后端挑选十来个字段。
   挑选逻辑一变，平台新增字段就永远看不到。

## 关于 `raw_fields` 剔除三个媒体数组

`images` / `image_descriptions` / `videos` 容量大（实测 5 + 50 + 1），
已单独提取成 `product_images` / `detail_images` / `videos` 三个键；
若不剔除，同一批图会在「全部字段」表里展开成几十行 JSON，把表格冲垮。
"""

from __future__ import annotations

import pytest

from backend.app.services.shop_products_service import (
    product_image_links,
    product_video_links,
    detail_image_links,
)


#: 平台 `search_item_v2` 返回行的**真实字段子集**（真机采样，2026-10-27）。
RAW_ROW = {
    "item_id": "6adaf9dd238c6b00019c0dd7",
    "item_name": "美式乡村实木抽屉柜",
    "min_price": 230400,
    "max_price": 896400,
    "total_stock": 10400,
    "buyable": True,
    "create_time": 1792735709000,
    "images": [
        {"link": "http://qimg/1.jpg", "width": 800, "height": 800},
        {"link": "http://qimg/2.jpg", "width": 800, "height": 800},
    ],
    "image_descriptions": [
        {"link": "http://qimg/d1.jpg", "width": 1240, "height": 346},
    ],
    "videos": [
        {
            "path": "arkgoods/1040g29g326lh2lquk6004bna396tqa7ierinogg",
            "width": 720,
            "height": 960,
            "duration": 13,
            "size": 2285089,
            "extension": ".mp4",
        }
    ],
    "categories": [{"level": 1, "name": "住宅家具"}],
    "attributes": [{"attribute_name": "风格", "attribute_value": "美式乡村"}],
}


class TestVideoExtraction:
    """`product_video_links` —— 平台有视频就必须取出来。"""

    def test_extracts_every_video_with_its_metadata(self) -> None:
        videos = product_video_links(RAW_ROW)
        assert len(videos) == 1
        v = videos[0]
        assert v["path"] == "arkgoods/1040g29g326lh2lquk6004bna396tqa7ierinogg"
        assert v["duration"] == 13
        assert (v["width"], v["height"]) == (720, 960)
        assert v["size"] == 2285089
        assert v["extension"] == ".mp4"

    @pytest.mark.parametrize(
        "row",
        [
            {},
            {"videos": None},
            {"videos": []},
            {"videos": ["not-a-dict"]},
            {"videos": [{"width": 720}]},  # 缺 path ⇒ 没有可播放/可定位的东西
        ],
        ids=["no-key", "none", "empty", "wrong-type", "no-path"],
    )
    def test_absent_or_malformed_video_list_returns_empty(self, row) -> None:
        """缺值口径：没有视频就返回空列表，不造占位条目。"""
        assert product_video_links(row) == []

    def test_video_is_not_mixed_into_image_groups(self) -> None:
        """视频不属于图片图集 —— 混进去会让「生成详情图」的参考图多出怪东西。"""
        assert len(product_image_links(RAW_ROW)) == 2
        assert len(detail_image_links(RAW_ROW)) == 1


class TestRawFieldsContract:
    """端点把平台原始行带出来的方式（与 `goods.py` 的实现保持同一份判据）。"""

    MEDIA_KEYS = {"images", "image_descriptions", "videos"}

    def _raw_fields(self, row: dict) -> dict:
        """复刻端点的 `raw_fields` 构造（三个媒体数组剔除）。"""
        return {k: v for k, v in row.items() if k not in self.MEDIA_KEYS}

    def test_keeps_every_platform_field_except_media_arrays(self) -> None:
        fields = self._raw_fields(RAW_ROW)
        expected = set(RAW_ROW) - self.MEDIA_KEYS
        assert set(fields) == expected, "raw_fields 漏掉了平台的字段"

    def test_media_arrays_are_excluded(self) -> None:
        """三个媒体数组不重复出现在字段表里（已在媒体 Tab 单独展示）。"""
        fields = self._raw_fields(RAW_ROW)
        assert not (self.MEDIA_KEYS & set(fields))
        #: 反面对照：非媒体字段必须还在，避免「整片删空」式假绿
        assert "item_name" in fields and "categories" in fields

    def test_complex_structures_survive_as_structures(self) -> None:
        """数组/对象不能被字符串化 —— 前端要按类型决定怎么渲染。"""
        fields = self._raw_fields(RAW_ROW)
        assert isinstance(fields["categories"], list)
        assert isinstance(fields["attributes"], list)
        assert fields["categories"][0]["name"] == "住宅家具"

    def test_millisecond_timestamps_are_kept_raw(self) -> None:
        """时间戳原样保留（毫秒整数）；转人话是前端的责任，后端不改数据。"""
        fields = self._raw_fields(RAW_ROW)
        assert fields["create_time"] == 1792735709000
        assert isinstance(fields["create_time"], int)


class TestSkuSpecImage:
    """SKU 规格图（`specImage.link` → `spec_image`）—— 接口有，界面必须能拿到。"""

    #: `get_skus_by_item_ids` 的 SKU 原始形状（真机采样）
    RAW_SKU = {
        "skuId": "6adaf9dd238c6b00019c0dd8",
        "skuName": "美式乡村实木抽屉柜 中古色-实木三斗柜",
        "specImage": {
            "link": "http://qimg.xiaohongshu.com/arkgoods/1040g0o0326lh5n4e46004bna396tqa7ilp530u8",
            "width": 800,
            "height": 800,
        },
        "price": 230400,
        "originalPrice": 391680,
    }

    def test_spec_image_link_is_extracted(self) -> None:
        from backend.app.services.realtime_platform_query_service import normalize_sku

        sku = normalize_sku(self.RAW_SKU)
        assert sku["spec_image"].startswith("http://qimg.xiaohongshu.com/arkgoods/"), (
            "SKU 的 `specImage.link` 没有映射到 `spec_image` —— 界面就拿不到规格图了"
        )

    @pytest.mark.parametrize(
        "raw",
        [
            {"skuId": "s1"},
            {"skuId": "s1", "specImage": None},
            {"skuId": "s1", "specImage": {}},
            {"skuId": "s1", "specImage": {"link": ""}},
        ],
        ids=["no-field", "none", "empty-dict", "empty-link"],
    )
    def test_missing_spec_image_is_empty_string_not_fabricated(self, raw) -> None:
        """缺值口径：没有规格图就留空串，不造占位 URL。"""
        from backend.app.services.realtime_platform_query_service import normalize_sku

        assert normalize_sku(raw)["spec_image"] == ""


class TestVideoPlayableUrl:
    """视频**可播放地址**规则（2026-10-27 抓包得出）。

    平台在 `videos[]` 里只给对象存储 `path`、**不给 `link`**（对比图片对象是带的），
    所以一度误判「服务端签名才能播」。实际在千帆商品编辑页抓 `<video>` 元素得到的是
    `https://store.xiaohongshu.com/{path}`，**无签名参数**，实测 206 + ftypisom。

    走过的弯路：`sns-video-bd` / `qimg` / `ci` / `fe-video-qc` 全 404；
    `ros-preview`（图片签名域名）与 `sns-video-qc` 是 403。
    """

    def test_video_url_uses_store_host_without_signature(self) -> None:
        videos = product_video_links(RAW_ROW)
        assert len(videos) == 1
        url = videos[0]["url"]
        assert url == (
            "https://store.xiaohongshu.com/"
            "arkgoods/1040g29g326lh2lquk6004bna396tqa7ierinogg"
        ), f"视频播放地址规则变了：{url}"

    def test_video_url_has_no_signature_params(self) -> None:
        """该域名**不需要**签名 —— 带 `?sign=` 说明用了图片那套签名域名（会 403）。"""
        url = product_video_links(RAW_ROW)[0]["url"]
        assert "sign=" not in url and "?" not in url
        assert "ros-preview" not in url, (
            "`ros-preview` 是**图片**签名域名，视频走它会 403"
        )

    @pytest.mark.parametrize(
        "path",
        ["", "   ", "/"],
        ids=["empty", "spaces", "slash-only"],
    )
    def test_blank_path_yields_no_url(self, path: str) -> None:
        """缺值口径：path 为空就不造地址（只有 path 非空才有 URL）。"""
        row = {"videos": [{"path": path, "width": 720, "height": 960}]}
        assert product_video_links(row) == [], "空 path 不该产出视频条目"

    def test_leading_slash_is_normalized(self) -> None:
        """`/arkgoods/x` 与 `arkgoods/x` 归一成同一地址（不出现 `//`）。"""
        row = {"videos": [{"path": "/arkgoods/abc"}]}
        assert product_video_links(row)[0]["url"] == "https://store.xiaohongshu.com/arkgoods/abc"
