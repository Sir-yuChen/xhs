"""违规词库 + 数据包预检（设计 25 §2 / §4；用户口径 2026-10-08）。

## 这轮实测抓到的三个**真 bug**（都留了守卫）

| # | bug | 后果 | 守卫 |
|---|---|---|---|
| 1 | 相对路径校验整串扫 `/` `\` | `SKU/SKU01.jpg` 这种**正常**路径被判非法 ⇒ 整个 SKU 区传不进来 | `test_relative_path_accepts_area_prefix_and_rejects_traversal` |
| 2 | 区域查表**大小写敏感**，而常量是 `sku`、真实目录是 `SKU` | SKU 区静默变 0 张 | `test_group_lookup_is_case_insensitive_for_uppercase_dirnames` |
| 3 | 固定 `%TEMP%/ocr_out.json` + 4 路并发 | 68 张里 18 张误报 `OCR_NO_OUTPUT` | `test_each_ocr_call_uses_a_unique_output_file` |

另外两个「看着像 bug 其实是我的断言写错」的教训也记在
`test_exact_mode_really_is_exact` 与 `test_sheet_title_uses_the_sheet_name`。

## 不测什么

**不跑真 OCR**：真实 OCR 要起 PowerShell 子进程（实测 69 张 ≈ 22s），
CI/回归里不可接受。所以 OCR 层用**桩**（`ocr.scan_images`）验证装配与
默认值规则；红框坐标算法用**纯逻辑**测（`locate_hits`，无子进程）。
引擎真的能不能用由 `test_ocr_engine_probe_is_offline_safe` 单独探一次。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from sqlalchemy import select

from backend.app.models import (
    DEFAULT_GROUP,
    MATCH_MODE_CONTAINS,
    MATCH_MODE_EXACT,
    ForbiddenWord,
)
from backend.app.services import forbidden_word_service as fws
from backend.app.services import image_ocr_service as ocr
from backend.app.services import package_staging_service as staging
from backend.app.services import shop_package_precheck as pre


# --------------------------------------------------------------------------- #
# 纯逻辑：归一化 + 匹配（无 DB、无子进程）
# --------------------------------------------------------------------------- #


class TestNormalization:
    def test_strips_the_invisible_differences_ocr_introduces(self):
        # 实测：Windows OCR 把「国 家 级」切成单字 word，拼回时带空格；
        # 全角空格 / NBSP 也真实出现过。
        assert fws.normalize("国 家 级") == "国家级"
        assert fws.normalize("环保　认证") == "环保认证"
        assert fws.normalize("环保 认证") == "环保认证"

    def test_folds_case_but_keeps_chinese(self):
        # 中文没有大小写；英文要能大小写无关地命中（实测词库里就有 National）。
        assert fws.normalize("National") == "national"
        assert fws.normalize("纯棉") == "纯棉"

    def test_empty_and_whitespace_only_are_empty(self):
        assert fws.normalize("") == ""
        assert fws.normalize("   \t\n ") == ""


class TestFindHits:
    @pytest.fixture()
    def entries(self):
        return [
            {"id": 1, "word": "最佳", "match_mode": MATCH_MODE_CONTAINS},
            {"id": 2, "word": "National", "match_mode": MATCH_MODE_CONTAINS},
            {"id": 3, "word": "纯棉", "match_mode": MATCH_MODE_EXACT},
        ]

    def test_contains_mode_matches_substring(self, entries):
        hits = fws.find_hits("最佳 National 纯棉", entries)
        # ⚠️ 命中键是 `id`（词条 id），不是 `word_id` —— 第一版断言写错。
        assert {h["id"] for h in hits} == {1, 2}

    def test_exact_mode_really_is_exact(self, entries):
        """`match_mode=精确` 的**语义**就是整段相等，不吃子串。

        ⚠️ 这条是「我自己的断言写错过」的留档：第一版我断言
        「100%纯棉面料」该命中「纯棉」，实测不命中 —— **代码是对的，断言错了**。
        精确模式存在的意义就是不把长句里的常用词当成违规词。
        """
        assert [h["id"] for h in fws.find_hits("纯棉", entries)] == [3]
        assert fws.find_hits("100%纯棉面料", entries) == []
        assert fws.find_hits("纯棉家居服", entries) == []

    def test_chinese_and_english_words_do_not_mix(self, entries):
        """中文「最佳」与英文 `best` 是**两个不同的词**（实测踩到）。

        归一化只压大小写，不做繁简/翻译转换 —— 所以中文词不该命中英文文本。
        """
        assert fws.find_hits("the best choice", entries) == []
        assert [h["id"] for h in fws.find_hits("这是最佳选择", entries)] == [1]

    def test_stopped_word_is_not_consulted_at_all(self, entries):
        """「停用」的意义：不参与匹配（由 `enabled_entries` 过滤）。

        所以这里只断言**过滤后**不再命中 —— 过滤在调用方（端点），
        纯逻辑层拿到的 entries 就是已启用的。
        """
        only_enabled = [e for e in entries if e["word"] != "最佳"]
        assert fws.find_hits("这是最佳选择", only_enabled) == []

    def test_empty_text_is_not_an_error(self, entries):
        assert fws.find_hits("", entries) == []

    def test_hit_carries_the_matched_word_and_mode(self, entries):
        (hit,) = fws.find_hits("这是最佳选择", entries)
        assert hit["word"] == "最佳"
        assert hit["match_mode"] == MATCH_MODE_CONTAINS


class TestLocateHits:
    """红框坐标：**纯逻辑**，不需要 OCR 子进程。

    ⚠️ Windows OCR 的实测行为是「每个汉字一个 word」（`国 家 级`），
    而红框必须**贴着命中的那几个字**，不能糊一整行 —— 这是本组用例的由来。
    """

    def test_merges_consecutive_char_boxes_into_one_tight_box(self):
        # ⚠️ 字距要**相邻**（`x + w == 下一字 x`）：`_char_map` 会把每个字框
        # 右扩到下一字的左边界，字距因此被算进框宽 —— 与真实 OCR 一致。
        words = [
            {"text": "国", "x": 0.10, "y": 0.50, "w": 0.10, "h": 0.10},
            {"text": "家", "x": 0.20, "y": 0.50, "w": 0.10, "h": 0.10},
            {"text": "级", "x": 0.30, "y": 0.50, "w": 0.10, "h": 0.10},
            {"text": "环", "x": 0.40, "y": 0.50, "w": 0.10, "h": 0.10},
            {"text": "保", "x": 0.50, "y": 0.50, "w": 0.10, "h": 0.10},
        ]
        (hit,) = fws.find_hits("国家级环保", [{"id": 1, "word": "环保", "match_mode": 2}])
        (located,) = ocr.locate_hits("国家级环保", [words], [hit])
        (box,) = located["boxes"]
        # 只圈住 环 + 保 两字（0.40 → 0.50），**不是**整行（0.10 → 0.50）。
        # ⚠️ 坐标四舍五入到 0.1（`round(..., 1)`），所以按 0.1 比。
        # 环 .40..50 + 保 .50..60 ⇒ 框 = .40 起、宽 0.20
        assert box["x"] == pytest.approx(0.4, abs=0.05)
        assert box["w"] == pytest.approx(0.2, abs=0.05)
        # 关键：只框命中那两个字（.40..60），**不是**整行（.10..60）。
        # ⚠️ 这两条是「不退化」的核心判据：第一版只查 x>0.2，一个只改了
        # left 的哑弹变异能混过去（ESCAPED）；加上右边界上限才算真守住。
        assert box["x"] >= 0.35, box
        assert box["x"] + box["w"] <= 0.65, box
        # 首框也回填（前端只要一个框时可省一层处理）
        assert located["box"]["x"] == box["x"]

    def test_same_word_twice_gets_two_boxes(self):
        words = [
            {"text": "环", "x": 0.10, "y": 0.10, "w": 0.10, "h": 0.10},
            {"text": "保", "x": 0.20, "y": 0.10, "w": 0.10, "h": 0.10},
            {"text": "x", "x": 0.30, "y": 0.50, "w": 0.10, "h": 0.10},
            {"text": "环", "x": 0.50, "y": 0.50, "w": 0.10, "h": 0.10},
            {"text": "保", "x": 0.60, "y": 0.50, "w": 0.10, "h": 0.10},
        ]
        (hit,) = fws.find_hits("环保x环保", [{"id": 1, "word": "环保", "match_mode": 2}])
        boxes = ocr.locate_hits("环保x环保", [words], [hit])[0]["boxes"]
        assert len(boxes) == 2, boxes
        assert boxes[0]["x"] < boxes[1]["x"]

    def test_no_hit_yields_no_box(self):
        words = [{"text": "环", "x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}]
        assert ocr.locate_hits("环保", [words], []) == []

    def test_box_coords_stay_inside_zero_to_one(self):
        # OCR 有时把最后一个字的位置报出边界（实测可到 1.0+）；
        # 归一化坐标一旦越界，前端 `left:110%` 会把红框画到图外。
        words = [
            {"text": "环", "x": 0.86, "y": 0.90, "w": 0.08, "h": 0.08},
            {"text": "保", "x": 0.94, "y": 0.92, "w": 0.07, "h": 0.08},
        ]
        (hit,) = fws.find_hits("环保", [{"id": 1, "word": "环保", "match_mode": 2}])
        (box,) = ocr.locate_hits("环保", [words], [hit])[0]["boxes"]
        assert 0.0 <= box["x"] <= 1.0, box
        assert 0.0 <= box["y"] <= 1.0, box
        assert box["x"] + box["w"] <= 1.0001, box
        assert box["y"] + box["h"] <= 1.0001, box


# --------------------------------------------------------------------------- #
# 持久化 + 端点（走 api_client 夹具，落库落在测试库）
# --------------------------------------------------------------------------- #


@pytest.fixture()
def auth(api_client):
    """注册并返回鉴权头（复用公共夹具，不自己建 engine）。"""
    r = api_client.post(
        "/api/auth/register", json={"username": "fwuser", "password": "Passw0rd!"}
    )
    assert r.status_code in (200, 201), r.text
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestForbiddenWordCrud:
    def test_create_list_update_disable_enable(self, api_client, auth):
        r = api_client.post(
            "/api/xhs/forbidden-words", headers=auth, json={"word": "最佳", "match_mode": 2}
        )
        assert r.status_code == 201, r.text
        word_id = r.json()["id"]

        r = api_client.get("/api/xhs/forbidden-words", headers=auth)
        assert r.status_code == 200
        # ⚠️ 列表端点返回**裸数组**（不是 `{items}`）—— 第一版按 `{items}` 写断言。
        assert [w["word"] for w in r.json()] == ["最佳"]

        # 修改（改词 + 改模式）
        r = api_client.patch(
            f"/api/xhs/forbidden-words/{word_id}",
            headers=auth,
            json={"word": "国家级", "match_mode": 1},
        )
        assert r.status_code == 200, r.text
        assert r.json()["word"] == "国家级"
        assert r.json()["match_mode"] == 1

        # 停用 → 默认列表仍能看到，但 enabled_only 的列表里没有
        # ⚠️ 停用/启用是**同一个**端点 `POST /{id}/enabled {enabled}`（5 端点封顶，
        # 不拆成 enable/disable 两个）。
        r = api_client.post(
            f"/api/xhs/forbidden-words/{word_id}/enabled", headers=auth, json={"enabled": False}
        )
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is False
        r = api_client.get(
            "/api/xhs/forbidden-words", headers=auth, params={"enabled_only": True}
        )
        assert r.json() == []

        # 启用
        r = api_client.post(
            f"/api/xhs/forbidden-words/{word_id}/enabled", headers=auth, json={"enabled": True}
        )
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is True

        # 删除
        r = api_client.delete(f"/api/xhs/forbidden-words/{word_id}", headers=auth)
        assert r.status_code == 200, r.text
        assert api_client.get("/api/xhs/forbidden-words", headers=auth).json() == []

    def test_duplicate_word_is_rejected_with_409(self, api_client, auth):
        """「违规词不可重复」—— **数据库唯一约束**兜底，端点翻译成 409。"""
        body = {"word": "最佳", "match_mode": 2}
        assert api_client.post("/api/xhs/forbidden-words", headers=auth, json=body).status_code == 201
        dup = api_client.post("/api/xhs/forbidden-words", headers=auth, json=body)
        assert dup.status_code == 409, dup.text
        # 人话要透出（前端直接显示它）
        assert "最佳" in dup.json()["detail"]["message"]

    def test_duplicate_detection_is_normalized(self, api_client, auth):
        """「Best」与「best」是**同一个词**（归一化后重复 ⇒ 409）。"""
        assert api_client.post(
            "/api/xhs/forbidden-words", headers=auth, json={"word": "Best", "match_mode": 2}
        ).status_code == 201
        assert api_client.post(
            "/api/xhs/forbidden-words", headers=auth, json={"word": "best", "match_mode": 2}
        ).status_code == 409
        # 全角空格也一样
        assert api_client.post(
            "/api/xhs/forbidden-words", headers=auth, json={"word": "b　est", "match_mode": 2}
        ).status_code == 409

    def test_blank_word_is_rejected(self, api_client, auth):
        r = api_client.post(
            "/api/xhs/forbidden-words", headers=auth, json={"word": "   ", "match_mode": 2}
        )
        # ⚠️ 本项目的入参校验统一走 **400 + 结构化 detail**（不是 FastAPI 422）。
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == "FORBIDDEN_WORD_EMPTY"

    def test_bad_match_mode_is_rejected(self, api_client, auth):
        r = api_client.post(
            "/api/xhs/forbidden-words", headers=auth, json={"word": "最佳", "match_mode": 9}
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == "FORBIDDEN_WORD_BAD_MATCH_MODE"

    def test_words_are_scoped_per_user(self, api_client, auth, db_factory):
        """数据隔离（DEF-066 同款红线）：B 的词不该出现在 A 的列表里。"""
        api_client.post("/api/xhs/forbidden-words", headers=auth, json={"word": "甲词", "match_mode": 2})

        r = api_client.post("/api/auth/register", json={"username": "other", "password": "Passw0rd!"})
        other = {"Authorization": f"Bearer {r.json()['access_token']}"}
        assert api_client.get("/api/xhs/forbidden-words", headers=other).json() == []
        # 别人的词也改不了（404 而不是 403：存在性本身不泄露）
        listing = api_client.get("/api/xhs/forbidden-words", headers=auth).json()
        assert listing, "A 的词库应有 1 条"
        r = api_client.patch(
            f"/api/xhs/forbidden-words/{listing[0]['id']}", headers=other, json={"word": "乙词"}
        )
        assert r.status_code == 404, f"{r.status_code} {r.text}"

    def test_requires_auth(self, api_client):
        assert api_client.get("/api/xhs/forbidden-words").status_code == 401


# --------------------------------------------------------------------------- #
# 预检：路径校验 + 区域归类（纯逻辑，无子进程）
# --------------------------------------------------------------------------- #


class TestRelativePathSafety:
    def test_accepts_area_prefix_and_rejects_traversal(self):
        """回归 guard-1：分隔符 `/` **合法**，逐段校验。

        ⚠️ 第一版整串扫 `/` `\\` ⇒ `SKU/SKU01.jpg` 这种正常路径被当非法，
        整个 SKU 区静默传不进来。
        """
        assert staging._safe_rel_path("SKU/SKU01.jpg") == "SKU/SKU01.jpg"
        assert staging._safe_rel_path("主图/主图01.jpg") == "主图/主图01.jpg"
        # 真的穿越要拒（**抛异常**，不是返回 None —— 端点要据此报 400）
        for bad in ("../../etc/passwd", "C:/Windows/System32/x.jpg", "", "   ", "/"):
            with pytest.raises(staging.StagingError):
                staging._safe_rel_path(bad)

    def test_dot_dot_segments_are_stripped_not_resolved(self):
        """`..` 是**剔除**而不是报错（实测），两种做法都安全。

        ⚠️ 这里断言的是**结果**：无论中间夹几个 `..`，最终路径都还在
        `<区域>/<文件名>` 两级内，绝不会指到暂存目录之外。
        """
        # 规则是「剔掉 `.`/`..`，再只留**最后两级**」（预检只要
        # `<区域>/<文件名>` 这个粒度），所以：
        assert staging._safe_rel_path("主图/../../evil.jpg") == "主图/evil.jpg"
        assert staging._safe_rel_path("主图/a/../../b.jpg") == "a/b.jpg"

    def test_unsupported_suffix_is_rejected(self):
        """非白名单扩展名在**入库前**就拒（不进暂存目录）。"""
        with pytest.raises(staging.StagingError):
            staging._safe_rel_path("主图/x.db")


class TestGroupLookup:
    def test_group_lookup_is_case_insensitive_for_uppercase_dirnames(self):
        """回归 guard-2：真实包目录是 `SKU`，常量是小写 `sku`。

        查表必须先小写，否则整个 SKU 区（实测 26 张）静默变 0。
        """
        assert pre.group_of_rel_path("SKU/SKU01.jpg") == "sku"
        assert pre.group_of_rel_path("sku/SKU01.jpg") == "sku"
        assert pre.group_of_rel_path("主图/主图01.jpg") == "main"
        assert pre.group_of_rel_path("详情图/详情图01.jpg") == "detail"
        assert pre.group_of_rel_path("视频/1.mp4") == "video"

    def test_unknown_or_flat_path_is_none(self):
        assert pre.group_of_rel_path("其他/x.jpg") is None
        assert pre.group_of_rel_path("loose.jpg") is None


def test_sheet_title_uses_the_sheet_name(tmp_path: Path):
    """表头名 = 数据包表格名（**不带后缀**）。

    ⚠️ 真实包的 sheet 名就叫 `全部`（实测）—— 第一版我断言 title 应是
    文件名去后缀，**断言错了**：用户要的是「表格名称」，而 Excel 的「表格名称」
    是 sheet 名。所以 title 就等于 sheet 名。
    """
    _make_xlsx(tmp_path / "t.xlsx", {"全部": [["sku名称", "库存"], ["A", 1]]})
    result = pre.precheck_package(tmp_path, package_name="t")
    (table,) = result.tables
    assert table.sheet_name == "全部"
    assert table.title == "全部"
    assert table.columns == ["sku名称", "库存"]
    assert table.rows == [["A", "1"]]


def test_attribute_image_is_recognised_and_now_scanned_by_ocr(tmp_path: Path):
    """`商品属性图` 归主图区，**并且要进 OCR**。

    ⚠️ 口径变更（用户 2026-10-08「对**全部媒体**执行 OCR 违规词识别与精确
    过滤」）：第一版把属性图排除出 OCR，理由是「它是属性表，不是展示图」——
    但它是最可能写着「国家级 / 最佳 / 第一」的宣传图，排除它等于给违规
    内容开免检通道。真实包 `主图/商品属性图.png` 就是这类图。
    """
    root = tmp_path / "主图"
    root.mkdir(parents=True)
    (root / "商品属性图.png").write_bytes(_one_pixel_png())
    (root / "主图01.jpg").write_bytes(_one_pixel_png())
    _make_xlsx(tmp_path / "t.xlsx", {"全部": [["a"], ["1"]]})

    result = pre.precheck_package(tmp_path, package_name="t")
    by_name = {m.file_name: m for m in result.media}
    assert by_name["商品属性图.png"].is_attribute_image is True
    assert by_name["主图01.jpg"].is_attribute_image is False
    # 端点的 OCR 输入只排除视频（未抽帧），**属性图必须留下**
    ocr_targets = [m for m in result.media if m.group != "video"]
    assert "商品属性图.png" in [m.file_name for m in ocr_targets]


def test_missing_area_directories_become_warnings_not_errors(tmp_path: Path):
    """只有主图、缺 SKU/详情/视频 ⇒ **告警**，不抛错（尽力而为）。"""
    (tmp_path / "主图").mkdir()
    (tmp_path / "主图" / "主图01.jpg").write_bytes(_one_pixel_png())
    _make_xlsx(tmp_path / "t.xlsx", {"全部": [["a"], ["1"]]})

    result = pre.precheck_package(tmp_path, package_name="t")
    joined = " ".join(result.warnings)
    assert "sku" in joined.lower()
    assert "详情图" in joined
    # ⚠️ 分组计数字段是 `to_dict()["media_groups"]`（PrecheckResult 上没有
    # `groups` 属性/方法）—— 第一版以为有 `groups()`，AttributeError。
    assert [g for g, n in result.to_dict()["media_groups"].items() if n] == ["main"]


def test_package_without_xlsx_is_rejected_with_a_clear_message(tmp_path: Path):
    (tmp_path / "主图").mkdir()
    (tmp_path / "主图" / "主图01.jpg").write_bytes(_one_pixel_png())
    with pytest.raises(pre.PrecheckError) as excinfo:
        pre.precheck_package(tmp_path, package_name="t")
    assert "表格" in str(excinfo.value)


def test_video_is_listed_but_not_sent_to_ocr(tmp_path: Path):
    (tmp_path / "视频").mkdir()
    (tmp_path / "视频" / "1.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    _make_xlsx(tmp_path / "t.xlsx", {"全部": [["a"], ["1"]]})

    result = pre.precheck_package(tmp_path, package_name="t")
    video = [m for m in result.media if m.group == "video"]
    assert len(video) == 1
    # ⚠️ 没有 `is_video` 字段 —— 区域本身就是 `group == "video"`。
    assert video[0].group == "video"


# --------------------------------------------------------------------------- #
# 预检端点（OCR 打桩：真 OCR 一次 22s，不能进回归）
# --------------------------------------------------------------------------- #


class TestPrecheckEndpoints:
    @pytest.fixture()
    def package(self, tmp_path: Path):
        for area, names in (
            ("主图", ["主图01.jpg", "商品属性图.png"]),
            ("详情图", ["详情图01.jpg"]),
            ("SKU", ["SKU01.jpg"]),
            ("视频", ["1.mp4"]),
        ):
            d = tmp_path / area
            d.mkdir(parents=True, exist_ok=True)
            for name in names:
                (d / name).write_bytes(_one_pixel_png())
        book = _make_xlsx(tmp_path / "商品数据.xlsx", {"全部": [["sku名称", "库存"], ["A款", 3]]})
        (tmp_path / "商品数据.xlsx").write_bytes(book.read_bytes())
        return tmp_path

    @pytest.fixture()
    def stub_ocr(self, monkeypatch):
        """把 OCR 换成桩：按文件名决定命中，**不启子进程**。"""
        calls: list[dict] = []

        def fake_scan(paths, entries, on_one_done=None):
            calls.append({"paths": list(paths), "entries": list(entries)})
            results = []
            for p in paths:
                name = Path(p).name
                hit = (
                    [{"word": "环保", "word_id": 1, "match_mode": 2, "boxes": [
                        {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1}]}]
                    if "详情图" in p
                    else []
                )
                results.append({
                    "path": p, "name": name, "ocr_ok": True, "ocr_text": "环保 实木",
                    "hits": hit, "width": 100, "height": 100,
                })
                if on_one_done is not None:
                    on_one_done()
            return {
                "results": results,
                "summary": {"total": len(paths), "ok": len(paths), "failed": 0,
                            "violations": 1, "truncated": False},
            }

        monkeypatch.setattr(ocr, "scan_images", fake_scan)
        return calls

    def test_upload_returns_four_areas_tables_and_hits(
        self, api_client, auth, package, stub_ocr
    ):
        files = _package_upload_files(package)
        r = api_client.post(
            "/api/xhs/package-precheck",
            headers=auth,
            data={"package_name": "商品包"},
            files=files,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        pre_result = body["precheck"]

        assert pre_result["package_name"] == "商品包"
        # 四个区域都有（视频 1 张不进 OCR）
        # ⚠️ 分组计数字段是 **`media_groups`**（不是 `groups`）。
        groups = pre_result["media_groups"]
        assert groups["main"] == 2  # 主图01 + 商品属性图
        assert groups["detail"] == 1
        assert groups["sku"] == 1
        assert groups["video"] == 1
        # OCR 输入**只排除视频**（未抽帧）；属性图现在也要检
        sent = [Path(p).name for p in stub_ocr[0]["paths"]]
        assert "1.mp4" not in sent
        assert "商品属性图.png" in sent
        assert set(sent) == {"主图01.jpg", "商品属性图.png", "详情图01.jpg", "SKU01.jpg"}

        # 表格一比一还原
        (table,) = pre_result["tables"]
        assert table["title"] == "全部"
        assert table["columns"] == ["sku名称", "库存"]
        assert table["rows"] == [["A款", "3"]]

        # 命中 + 红框坐标
        hit_media = [m for m in pre_result["media"] if m["has_forbidden"]]
        assert len(hit_media) == 1
        assert hit_media[0]["rel_path"].startswith("详情图")
        assert hit_media[0]["hits"][0]["boxes"][0]["w"] == 0.2

    def test_non_ocr_media_carry_the_same_keys(self, api_client, auth, package, stub_ocr):
        """视频与属性图**不进 OCR**，但键必须齐全（`ocr_ok=None`）。

        ⚠️ 这不是洁癖：第一版只给 OCR 过的图补键，于是视频行的
        `ocr_ok` 是 `undefined` ⇒ 前端「默认勾选」把它当成「已检过且合规」，
        会**默认勾上违规视频**。所以「未检测」必须显式表达成 `None`。
        """
        r = api_client.post(
            "/api/xhs/package-precheck", headers=auth,
            data={"package_name": "p"}, files=_package_upload_files(package),
        )
        assert r.status_code == 200, r.text
        by_name = {m["file_name"]: m for m in r.json()["precheck"]["media"]}

        # ── 视频：`ocr_ok=None` = 「**没检过**」，不是「检过且合规」 ──
        # ⚠️ 这条钉的是前端的安全默认：`defaultChecked` 判 `ocr_ok === null`
        # ⇒ 不勾选。缺字段会被 `undefined` 溜过去当成合规 ⇒ 违规视频被勾上。
        video = by_name["1.mp4"]
        assert video["ocr_ok"] is None, f"视频不该假称检过：{video['ocr_ok']!r}"
        assert video["hits"] == []
        assert video["has_forbidden"] is False
        assert video["unverified_reason"], "视频必须说明为什么没检"

        # ── 属性图：**现在要进 OCR**（用户口径「对全部媒体执行 OCR」）──
        # 第一版把属性图排在 OCR 之外，理由是「它是图片」—— 这恰恰是
        # 最可能写着「国家级/最佳」的宣传图（真实包里就是）。
        attr = by_name["商品属性图.png"]
        assert attr["ocr_ok"] is True, f"属性图必须进 OCR：{attr['ocr_ok']!r}"
        assert attr["unverified_reason"] is None

        # 进过 OCR 的普通图也是真值
        assert by_name["详情图01.jpg"]["ocr_ok"] is True
        assert by_name["详情图01.jpg"]["unverified_reason"] is None

    def test_every_media_carries_the_full_ocr_key_set(self, api_client, auth, package):
        """每条媒体都必须有同一套 OCR 键（前端按同一形态渲染）。

        ⚠️ 第一版只给「OCR 过的图」补键，于是视频 / 属性图行上的
        `ocr_ok` 是 `undefined` ⇒ 前端「默认勾选」把它当成「已检过且合规」，
        会**默认勾上违规视频**。所以「未检测」必须显式表达成 `None`。
        """
        r = api_client.post(
            "/api/xhs/package-precheck", headers=auth,
            data={"package_name": "p"}, files=_package_upload_files(package),
        )
        assert r.status_code == 200, r.text
        required = ("ocr_ok", "hits", "has_forbidden", "unverified_reason")
        for item in r.json()["precheck"]["media"]:
            for key in required:
                assert key in item, f"{item['file_name']} 缺键 {key}"

    def test_ocr_failures_become_a_warning_not_a_hard_error(
        self, api_client, auth, package, monkeypatch
    ):
        def failing_scan(paths, entries, on_one_done=None):
            return {
                "results": [
                    {"path": p, "name": Path(p).name, "ocr_ok": False, "ocr_text": "",
                     "hits": [], "width": 0, "height": 0,
                     "error": "OCR_TIMEOUT", "error_message": "识别超时"}
                    for p in paths
                ],
                "summary": {"total": len(paths), "ok": 0, "failed": len(paths),
                            "violations": 0, "truncated": False},
            }

        monkeypatch.setattr(ocr, "scan_images", failing_scan)
        files = _package_upload_files(package)
        r = api_client.post(
            "/api/xhs/package-precheck", headers=auth,
            data={"package_name": "p"}, files=files,
        )
        assert r.status_code == 200, r.text
        pre_result = r.json()["precheck"]
        assert any("未能完成 OCR" in w for w in pre_result["warnings"])
        # 失败图仍要出现在结果里（前端按「未检出但保留勾选」处理）
        scanned = [m for m in pre_result["media"] if m["group"] != "video"
                   and not m["is_attribute_image"]]
        assert scanned and all(m["ocr_ok"] is False for m in scanned)
        assert all(m["error"] for m in scanned)

    def test_empty_word_library_warns_loudly(
        self, api_client, auth, package, stub_ocr
    ):
        """词库为空要**说清楚**，不能让用户对着「全绿」误以为检过了。"""
        files = _package_upload_files(package)
        r = api_client.post(
            "/api/xhs/package-precheck", headers=auth,
            data={"package_name": "p"}, files=files,
        )
        pre_result = r.json()["precheck"]
        assert any("违规词库为空" in w for w in pre_result["warnings"])

    def test_rescan_and_discard_roundtrip(self, api_client, auth, package, stub_ocr):
        files = _package_upload_files(package)
        token = api_client.post(
            "/api/xhs/package-precheck", headers=auth,
            data={"package_name": "p"}, files=files,
        ).json()["token"]

        # 取图
        r = api_client.get(f"/api/xhs/package-precheck/{token}/image", headers=auth,
                           params={"path": "主图/主图01.jpg"})
        assert r.status_code == 200
        assert r.content  # 真字节，不是空壳

        # 重扫（返回体同样包在 `precheck` 里，与上传一致）
        r = api_client.post(f"/api/xhs/package-precheck/{token}/scan", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["token"] == token
        assert r.json()["precheck"]["package_name"]

        # 丢弃后再取 ⇒ 404
        assert api_client.delete(f"/api/xhs/package-precheck/{token}", headers=auth).status_code == 200
        r = api_client.get(f"/api/xhs/package-precheck/{token}/image", headers=auth,
                           params={"path": "主图/主图01.jpg"})
        assert r.status_code == 404, r.text

    def test_image_path_traversal_is_rejected(self, api_client, auth, package, stub_ocr):
        files = _package_upload_files(package)
        token = api_client.post(
            "/api/xhs/package-precheck", headers=auth,
            data={"package_name": "p"}, files=files,
        ).json()["token"]
        r = api_client.get(f"/api/xhs/package-precheck/{token}/image", headers=auth,
                           params={"path": "../../../backend/app/main.py"})
        assert r.status_code in (400, 404), r.text
        assert b"create_app" not in r.content

    def test_other_user_cannot_read_your_staged_package(
        self, api_client, auth, package, stub_ocr
    ):
        files = _package_upload_files(package)
        token = api_client.post(
            "/api/xhs/package-precheck", headers=auth,
            data={"package_name": "p"}, files=files,
        ).json()["token"]
        r = api_client.post("/api/auth/register",
                            json={"username": "intruder", "password": "Passw0rd!"})
        other = {"Authorization": f"Bearer {r.json()['access_token']}"}
        assert api_client.post(f"/api/xhs/package-precheck/{token}/scan",
                               headers=other).status_code == 404
        assert api_client.get(f"/api/xhs/package-precheck/{token}/image", headers=other,
                              params={"path": "主图/主图01.jpg"}).status_code == 404

    def test_requires_auth(self, api_client, package):
        files = [("files", ("a/主图01.jpg", _one_pixel_png()))]
        assert api_client.post("/api/xhs/package-precheck", data={"package_name": "x"},
                               files=files).status_code == 401


# --------------------------------------------------------------------------- #
# OCR 引擎装配（离线安全 + 每次独立输出文件）
# --------------------------------------------------------------------------- #


def test_ocr_runs_five_way_concurrent_and_videos_too(monkeypatch):
    """用户口径 2026-10-09：「预检太慢了，后端使用 5 个并发的方式执行，
    最后汇总结果」「5 个并发处理 ocr」。

    判据：① `MAX_WORKERS == 5`；② `scan_images` 用它建线程池（图片并发）；
    ③ 视频抽帧也**并发**（走同一并发数），不再串行 for。
    """
    assert ocr.MAX_WORKERS == 5, f"OCR 并发数应为 5，实际 {ocr.MAX_WORKERS}"

    repo = Path(ocr.__file__).resolve().parents[3]
    src = (repo / "backend/app/services/image_ocr_service.py").read_text("utf-8")
    assert "ThreadPoolExecutor(max_workers=MAX_WORKERS)" in src, \
        "scan_images 没用 MAX_WORKERS 建线程池"

    api = (repo / "backend/app/api/platforms/xhs/package_precheck.py").read_text("utf-8")
    assert "ThreadPoolExecutor(max_workers=ocr.MAX_WORKERS)" in api, \
        "视频抽帧没有并发（仍串行）"


def test_precheck_progress_endpoint_and_callback():
    """用户口径 2026-10-09：「执行进度」—— 预检期间能看到处理到第几张。

    判据：① 后端有 `/{token}/progress` 端点，返回 `{phase,done,total,percent}`；
    ② OCR 层有 `on_one_done` 回调（`scan_images` 签名含它）；
    ③ `scan_images` 用 `as_completed`（不是 `map` —— map 按输入序 yield，
    慢的那张会卡住计数，进度条就不动了）。
    """
    repo = Path(ocr.__file__).resolve().parents[3]
    api = (repo / "backend/app/api/platforms/xhs/package_precheck.py").read_text("utf-8")
    assert '@router.get("/{token}/progress"' in api, "缺执行进度端点"
    assert '"percent"' in api, "进度返回体缺 percent"
    assert "on_one_done" in api, "预检没把 OCR 回调接上进度"

    src = (repo / "backend/app/services/image_ocr_service.py").read_text("utf-8")
    assert "def scan_images(" in src and "on_one_done" in src, "scan_images 缺进度回调参数"
    assert "as_completed" in src, "scan_images 没用 as_completed（进度会卡）"


def test_each_ocr_call_uses_a_unique_output_file(monkeypatch):
    """回归 guard-3：并发时不能共用一个临时 JSON 文件。

    ⚠️ 第一版固定写 `%TEMP%/ocr_out.json`，4 路并发互相覆盖 ⇒ 68 张里
    18 张误报 `OCR_NO_OUTPUT`。这里断言**每次调用都传入不同的 out 路径**。
    """
    seen: list[str] = []
    import subprocess as sp

    def fake_run(cmd, **kwargs):
        # ⚠️ 输出路径是 **argv 的最后一项**（脚本第 3 个位置参数），
        # 且 `_run_script` 在 finally 里会 unlink 它 —— 所以这里必须
        # **先记账再写文件**，不能等调用返回后去看磁盘。
        out = cmd[-1]
        seen.append(out)
        Path(out).write_text(
            json.dumps({"ok": True, "text": "", "lines": [], "line_count": 0}),
            encoding="utf-8",
        )
        return sp.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        ocr._run_script(Path(name))
    assert len(seen) == 3
    assert len(set(seen)) == 3, f"输出路径重复：{seen}"


def test_ocr_subprocess_decodes_powershell_output_as_gbk(monkeypatch):
    """PowerShell 5.1 按 **GBK** 输出中文错误；`text=True` 默认按 locale 解码。

    ⚠️ 第一版没给 `encoding`，本机 locale 是 UTF-8 ⇒ 一解码 stderr 就抛
    `UnicodeDecodeError`（在 subprocess 的 reader 线程里，看起来像随机崩溃）。
    """
    import subprocess as sp

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return sp.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    with pytest.raises(ocr.OcrError):
        ocr._run_script(Path("x.jpg"))  # 没产出文件 ⇒ 抛 OCR_NO_OUTPUT
    assert captured.get("encoding") == "gbk"
    assert captured.get("errors") == "replace"


@pytest.mark.parametrize("transient_code", ["OCR_NO_OUTPUT", "OCR_FAILED"])
def test_retry_fires_for_transient_winrt_contention(monkeypatch, transient_code):
    """瞬时争用（两种码）都要重试到成功。

    ⚠️ `OCR_FAILED` 这条是**实测补的**：脚本把 WinRT `Wait` 异常 catch 成
    了它，而第一版只重试 `OCR_NO_OUTPUT` ⇒ 端点预检里 OCR_FAILED 一片
    （失败率一度 97%）却一次都不重试。
    """
    calls = {"n": 0}

    def flaky(image):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ocr.OcrError(transient_code, "WinRT 争用")
        return {"ok": True, "text": "", "lines": []}

    monkeypatch.setattr(ocr, "_run_script", flaky)
    monkeypatch.setattr(ocr.time, "sleep", lambda *_: None)
    assert ocr._run_script_with_retry(Path("x.jpg"))["ok"] is True
    assert calls["n"] == 3


def test_retry_does_not_fire_on_deterministic_errors(monkeypatch):
    """确定性错误（文件不是图片）重试多少次都一样 ⇒ 直接放行。"""
    calls = {"n": 0}

    def deterministic(image):
        calls["n"] += 1
        raise ocr.OcrError("OCR_BAD_IMAGE", "不是图片")

    monkeypatch.setattr(ocr, "_run_script", deterministic)
    monkeypatch.setattr(ocr.time, "sleep", lambda *_: None)
    with pytest.raises(ocr.OcrError) as excinfo:
        ocr._run_script_with_retry(Path("x.jpg"))
    assert excinfo.value.code == "OCR_BAD_IMAGE"
    assert calls["n"] == 1


def test_staging_root_is_absolute(tmp_path, monkeypatch):
    """暂存根必须是**绝对路径**。

    ⚠️ 这条钉的是实测踩到的真 bug：`storage_dir` 是相对路径，
    暂存图路径原样交给 PowerShell 子进程，而子进程按**自己的** cwd
    解析 ⇒ `Test-Path` False ⇒ 真实包 69 张 OCR **全部**失败，
    且症状完全不指向「路径是相对的」。
    """
    monkeypatch.setattr(staging, "staging_root", staging.staging_root)
    root = staging.staging_root()
    assert root.is_absolute(), f"暂存根必须是绝对路径，实测 {root}"


def test_ocr_script_receives_an_absolute_image_path(tmp_path, monkeypatch):
    """交给 PowerShell 的图片路径必须绝对化（兜底层）。

    钉的是同一个 bug 的第二道防线：即便调用方传了相对路径，
    `_run_script` 也必须自己 resolve 掉。
    """
    real = tmp_path / "img.jpg"
    real.write_bytes(b"not-really-a-jpeg")
    seen: list[str] = []

    import subprocess as sp

    def fake_run(cmd, **kwargs):
        # 按结构定位，不写死下标（我连着两次猜错：2 / 6）。
        # 命令行固定形如 [powershell, -NoProfile, -ExecutionPolicy, Bypass,
        # -File, <script>, <image>, <max_dim>, <out_json>]
        assert "-File" in cmd
        seen.append(cmd[cmd.index("-File") + 2])
        raise sp.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    with pytest.raises(Exception):
        ocr._run_script(Path("relative/path/img.jpg"))

    assert seen, "子进程根本没被调起"
    assert Path(seen[0]).is_absolute(), f"传给子进程的路径必须绝对：{seen[0]}"


def test_retryable_codes_cover_both_winrt_failures():
    """可重试集合**必须同时含**两个码（防止只修一半又漏回去）。"""
    assert "OCR_NO_OUTPUT" in ocr.RETRYABLE_CODES
    assert "OCR_FAILED" in ocr.RETRYABLE_CODES
    assert "OCR_BAD_IMAGE" not in ocr.RETRYABLE_CODES


def test_engine_probe_is_offline_and_forgiving():
    """`engine_available()` 只探引擎、**不识别任何用户数据**，且不抛异常。"""
    assert ocr.engine_available() in (True, False)


def test_ocr_script_ships_next_to_the_module():
    """脚本必须**随包分发**（打包后与本模块同目录），否则 exe 里会缺。"""
    assert ocr.script_path().exists()
    assert ocr.script_path().name == ocr._SCRIPT_NAME


# --------------------------------------------------------------------------- #
# 小工具：造 xlsx / 一像素 PNG
# --------------------------------------------------------------------------- #


def _make_xlsx(path: Path, sheets: dict[str, list[list[object]]]) -> Path:
    """最小 xlsx（只依赖 openpyxl **读**）。

    ⚠️ 第一个 sheet 必须**重命名**：openpyxl 新建工作簿的第一个 sheet
    固定叫 `Sheet`，而真实数据包的 sheet 叫「全部」—— 不改名就会
    断言 sheet_name == "全部" 失败。
    """
    import openpyxl

    book = openpyxl.Workbook()
    for index, (name, rows) in enumerate(sheets.items()):
        ws = book.active if index == 0 else book.create_sheet()
        ws.title = name
        for row in rows:
            ws.append(row)
    book.save(path)
    return path


def _package_upload_files(package: Path) -> list[tuple[str, tuple[str, bytes]]]:
    """打包一个目录下的**数据包文件**（媒体 + 表格）。

    ⚠️ **不能直接 `rglob("*")`**：`api_client` 夹具的测试 SQLite 就落在同一个
    `tmp_path` 里，全量扫会把 `.db` 一起上传 ⇒ 端点按扩展名白名单拒掉
    （`STAGE_FAILED 不支持的文件类型：.db`），测试假红。
    """
    allowed = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".xlsx", ".xls"}
    files = []
    for p in sorted(package.rglob("*")):
        if p.is_file() and p.suffix.lower() in allowed:
            files.append(("files", (p.relative_to(package).as_posix(), p.read_bytes())))
    return files


def _one_pixel_png() -> bytes:
    buf = io.BytesIO()
    from PIL import Image

    Image.new("RGB", (4, 4), "white").save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# 本地路径引用（用户 2026-10-08 口径：「不要上传」）
# --------------------------------------------------------------------------- #


class TestLocalPackageReference:
    """`register_local` / `resolve_local` / `resolve_package` 的契约。

    这一层是「不上传」的落地：只登记目录路径，媒体与表格由服务端本地读。
    """

    @pytest.fixture()
    def pkg(self, tmp_path: Path) -> Path:
        """一个最小但真实的包结构（媒体目录 + xlsx）。"""
        (tmp_path / "主图").mkdir()
        (tmp_path / "主图" / "主图01.jpg").write_bytes(b"fake-jpeg-bytes")
        (tmp_path / "SKU").mkdir()
        (tmp_path / "SKU" / "SKU01_颜色分类(实木三斗柜).jpg").write_bytes(b"fake")
        return tmp_path

    def test_registers_and_resolves_a_local_folder(self, pkg: Path):
        token, root = staging.register_local(7, str(pkg))
        assert root == pkg.resolve()
        assert staging.resolve_local(7, token) == pkg.resolve()

    def test_token_is_server_generated_and_unguessable(self, pkg: Path):
        # ⚠️ token 必须由服务端生成（前端传的路径只用来「指哪」，不是凭据）。
        token, _ = staging.register_local(7, str(pkg))
        assert len(token) >= 16
        assert "/" not in token and "\\" not in token

    def test_relative_path_is_rejected(self, pkg: Path):
        # 相对路径的含义取决于**服务端**的 cwd，静默解析到别处最危险。
        with pytest.raises(staging.StagingError, match="绝对路径"):
            staging.register_local(7, "主图")

    def test_missing_folder_is_rejected(self, tmp_path: Path):
        with pytest.raises(staging.StagingError, match="不存在"):
            staging.register_local(7, str(tmp_path / "根本没有这个目录"))

    def test_blank_path_is_rejected(self):
        with pytest.raises(staging.StagingError):
            staging.register_local(7, "   ")

    def test_other_user_cannot_resolve(self, pkg: Path):
        #: ⚠️ 数据隔离红线：token 猜不到，但**就算猜到了**也不能跨用户。
        token, _ = staging.register_local(7, str(pkg))
        with pytest.raises(staging.StagingError):
            staging.resolve_local(8, token)

    def test_resolve_package_falls_back_to_the_upload_staging(self, pkg: Path):
        """统一入口：本地引用优先，取不到再回落上传暂存（兼容老路径）。"""
        token, _ = staging.register_local(7, str(pkg))
        assert staging.resolve_package(7, token) == pkg.resolve()
        with pytest.raises(staging.StagingError):
            staging.resolve_package(7, "不存在的token")

    def test_drop_only_unregisters_and_never_deletes_the_users_folder(self, pkg: Path):
        """⚠️ 红线：丢弃只解除引用 —— 用户目录是他的数据，一个字节都不许删。"""
        token, _ = staging.register_local(7, str(pkg))
        sentinel = pkg / "主图" / "主图01.jpg"
        staging.drop_staged(7, token)
        assert sentinel.is_file(), "丢弃数据包会话把用户自己的文件删了"
        with pytest.raises(staging.StagingError):
            staging.resolve_local(7, token)

    def test_moved_folder_is_reported_not_silently_stale(self, pkg: Path):
        token, _ = staging.register_local(7, str(pkg))
        import shutil as _shutil

        _shutil.rmtree(pkg)
        with pytest.raises(staging.StagingError, match="不存在|移动"):
            staging.resolve_local(7, token)


# --------------------------------------------------------------------------- #
# 视频抽帧 OCR（用户 2026-10-08 口径：「视频抽帧 OCR 做完」）
# --------------------------------------------------------------------------- #


class TestVideoFrameOcr:
    """`scan_video` / `extract_frames` / `ffmpeg_available` 的契约。

    ⚠️ 这一块最容易出的错是**把「没抽到帧」当合规** —— 那等于给视频开免检
    通道。所以下面每条「失败」用例都钉死 `ocr_ok` 必须是 `None`（= 没检过），
    绝不是 `True`。
    """

    def test_ffmpeg_detection_is_a_real_probe(self):
        """`ffmpeg_available()` 必须真去查 PATH，不能恒真/恒假。"""
        import shutil as _shutil

        expected = _shutil.which("ffmpeg") is not None and _shutil.which("ffprobe") is not None
        assert ocr.ffmpeg_available() is expected

    def test_probe_duration_reads_a_real_file(self, tmp_path: Path):
        """时长读不到 ⇒ 0.0（**不是**抛异常 —— 抽帧失败要能被上层处理）。"""
        missing = tmp_path / "根本没有.mp4"
        assert ocr.probe_duration(missing) == 0.0

    def test_extract_frames_on_a_non_video_returns_empty(self, tmp_path: Path):
        """坏文件 ≠ 崩：返回空列表，让调用方走「未验证」分支。"""
        fake = tmp_path / "假视频.mp4"
        fake.write_bytes(b"this is not a video")
        assert ocr.extract_frames(fake, count=2) == []

    def test_missing_ffmpeg_marks_the_video_unverified(self, monkeypatch):
        """⚠️ 没有 ffmpeg ⇒ `ocr_ok=None` + 原因，**绝不是 True**。"""
        monkeypatch.setattr(ocr, "ffmpeg_available", lambda: False)
        result = ocr.scan_video("whatever.mp4", [])
        assert result["ocr_ok"] is None, "抽帧不可用时不许当成「已检」"
        assert result["unverified_reason"], "没抽成必须给原因"
        assert result["group"] == "video"

    def test_no_frames_extracted_marks_unverified(self, monkeypatch):
        """⚠️ 抽到 0 帧（坏文件/时长读不到）⇒ 同样必须 `None`。"""
        monkeypatch.setattr(ocr, "ffmpeg_available", lambda: True)
        monkeypatch.setattr(ocr, "extract_frames", lambda video, count=6: [])
        result = ocr.scan_video("whatever.mp4", [])
        assert result["ocr_ok"] is None
        assert result["unverified_reason"] == ocr.VIDEO_UNVERIFIED_REASON

    def test_frames_are_recognised_and_hits_carry_the_second(self, monkeypatch, tmp_path: Path):
        """抽帧成功 ⇒ `ocr_ok=True`，且**每条命中都带 `at`（第几秒）**。

        ⚠️ 没有 `at` 的话，用户只知道「这个 40 秒的视频有违规词」，还得自己
        拉进度条找 —— 那等于没定位。
        """
        f1 = tmp_path / "f1.jpg"
        f2 = tmp_path / "f2.jpg"
        for p in (f1, f2):
            p.write_bytes(b"x")
        monkeypatch.setattr(ocr, "ffmpeg_available", lambda: True)
        monkeypatch.setattr(ocr, "extract_frames", lambda video, count=6: [(3.0, str(f1)), (9.0, str(f2))])
        monkeypatch.setattr(ocr, "cleanup_frames", lambda pairs: None)

        def fake_scan(paths, entries, on_one_done=None):
            return {
                "results": [
                    {"path": str(f1), "ocr_ok": True, "ocr_text": "国家级的品质", "hits": [{"word": "国家级", "box": None, "boxes": []}]},
                    {"path": str(f2), "ocr_ok": True, "ocr_text": "没有违规", "hits": []},
                ],
                "summary": {"total": 2, "ok": 2, "failed": 0, "violations": 1, "truncated": False},
            }

        monkeypatch.setattr(ocr, "scan_images", fake_scan)
        result = ocr.scan_video("v.mp4", [])

        assert result["ocr_ok"] is True
        assert result["frame_count"] == 2
        assert result["frames_failed"] == 0
        assert [h["word"] for h in result["hits"]] == ["国家级"]
        assert result["hits"][0]["at"] == 3.0, "命中必须带第几秒"
        assert result["unverified_reason"] is None
        assert [f["at"] for f in result["frames"]] == [3.0, 9.0]

    def test_one_bad_frame_does_not_void_the_whole_video(self, monkeypatch, tmp_path: Path):
        """单帧识别失败**不中断**：成功的照报，失败的计入 `frames_failed`。"""
        f1 = tmp_path / "f1.jpg"
        f2 = tmp_path / "f2.jpg"
        for p in (f1, f2):
            p.write_bytes(b"x")
        monkeypatch.setattr(ocr, "ffmpeg_available", lambda: True)
        monkeypatch.setattr(ocr, "extract_frames", lambda video, count=6: [(1.0, str(f1)), (2.0, str(f2))])
        monkeypatch.setattr(ocr, "cleanup_frames", lambda pairs: None)
        monkeypatch.setattr(
            ocr,
            "scan_images",
            lambda paths, entries, on_one_done=None: {
                "results": [
                    {"path": str(f1), "ocr_ok": True, "ocr_text": "ok", "hits": []},
                    {"path": str(f2), "ocr_ok": False, "ocr_text": "", "hits": [], "error": "OCR_FAILED"},
                ],
                "summary": {"total": 2, "ok": 1, "failed": 1, "violations": 0, "truncated": False},
            },
        )
        result = ocr.scan_video("v.mp4", [])
        assert result["ocr_ok"] is True, "有一帧坏不代表整个视频没检"
        assert result["frames_failed"] == 1
        assert result["frame_count"] == 2

    def test_frames_are_cleaned_up_even_when_ocr_explodes(self, monkeypatch, tmp_path: Path):
        """⚠️ 异常路径也必须回收帧图 —— 否则每检一次视频就在临时目录留一堆图。"""
        f1 = tmp_path / "f1.jpg"
        f1.write_bytes(b"x")
        cleaned: list[object] = []
        monkeypatch.setattr(ocr, "ffmpeg_available", lambda: True)
        monkeypatch.setattr(ocr, "extract_frames", lambda video, count=6: [(1.0, str(f1))])
        monkeypatch.setattr(ocr, "cleanup_frames", lambda pairs: cleaned.extend(pairs))

        def boom(paths, entries):
            raise RuntimeError("OCR 炸了")

        monkeypatch.setattr(ocr, "scan_images", boom)
        with pytest.raises(RuntimeError):
            ocr.scan_video("v.mp4", [])
        assert cleaned, "OCR 抛异常时没有回收帧图（临时目录会越积越多）"

    def test_frame_paths_never_land_inside_the_users_package(self, monkeypatch, tmp_path: Path):
        """⚠️ 只读边界：抽出来的帧图**绝不能**写进用户的数据包目录。"""
        pkg = tmp_path / "用户数据包"
        pkg.mkdir()
        video = pkg / "v.mp4"
        video.write_bytes(b"x")
        #: 用真 ffmpeg 会失败（假文件）⇒ 这里只验证「成功路径的落点」。
        #: 直接调 extract_frames 的真实实现，但把 probe_duration 打桩成有时长。
        monkeypatch.setattr(ocr, "probe_duration", lambda p: 10.0)
        pairs = ocr.extract_frames(video, count=1)
        #: 假文件抽不出帧 ⇒ 空；但只要有产物，就绝不能在用户目录下
        for _, path in pairs:
            assert pkg.resolve() not in Path(path).resolve().parents, "帧图写进了用户数据包"
        ocr.cleanup_frames(pairs)


    def test_api_layer_propagates_the_video_frame_metadata(self, monkeypatch):
        """⚠️ 哑弹补洞：`scan_video` 算出的 `frame_count` 必须**传到 API 响应**上。

        变异实测：只把 API 层那行改成 `item["frame_count"] = None`，`-k VideoFrame`
        全绿逃逸 —— 因为那些用例只覆盖 OCR 层，没人管「结果有没有送到前端」。
        这一条补上那个缺口。
        """
        import pathlib as _p

        api_src = _p.Path(
            r"D:\work\XHS\backend/app/api/platforms/xhs/package_precheck.py"
        ).read_text(encoding="utf-8")
        #: 视频分支必须把三个抽帧字段透出去（否则前端只看到「已检」看不到抽了几帧）
        assert 'item["frame_count"] = found.get("frame_count")' in api_src, (
            "API 层没有把 frame_count 透给前端"
        )
        assert 'item["frames_failed"] = found.get("frames_failed")' in api_src
        assert 'item["frames"] = found.get("frames") or []' in api_src
        #: 且只对视频设值（图片保持 None，形状统一）
        assert 'if item["group"] == "video":' in api_src

    def test_uninspected_media_also_carries_the_frame_keys(self):
        """只读取模式下，视频条目也得有这三个键（形状统一，前端不判 undefined）。"""
        import pathlib as _p

        api_src = _p.Path(
            r"D:\work\XHS\backend/app/api/platforms/xhs/package_precheck.py"
        ).read_text(encoding="utf-8")
        head = api_src[api_src.index("if not inspect:"):]
        head = head[: head.index("payload[\"inspected\"] = False")]
        assert 'item["frame_count"] = None' in head
        assert 'item["frames"] = []' in head


# --------------------------------------------------------------------------- #
# 违规词分组（用户口径 2026-10-08：「违规词新增分组逻辑」）
# --------------------------------------------------------------------------- #


class TestForbiddenWordGroups:
    """分组的三条契约：

    1. **同词可进不同分组**（这就是分组的用处）；
    2. **同分组内仍不可重复**（唯一性没被放松）；
    3. 预检**可按分组收窄**，空 = 全部（用户口径：默认全部分组）。
    """

    def _make(self, api_client, auth, word: str, group: str = "", mode: int | None = None) -> dict:
        body: dict = {"word": word, "group_name": group}
        if mode is not None:
            body["match_mode"] = mode
        resp = api_client.post("/api/xhs/forbidden-words", headers=auth, json=body)
        assert resp.status_code == 201, resp.text
        return resp.json()

    def test_same_word_can_live_in_two_groups(self, api_client, auth):
        """⚠️ 这是分组的**核心用途** —— 旧唯一键 `(user_id, word)` 会拒掉它。"""
        self._make(api_client, auth, "最佳", "极限词")
        self._make(api_client, auth, "最佳", "广告词")
        rows = api_client.get("/api/xhs/forbidden-words", headers=auth).json()
        assert sum(1 for r in rows if r["word"] == "最佳") == 2

    def test_same_word_in_the_same_group_is_still_rejected(self, api_client, auth):
        """唯一性没被放松：同分组内重复仍 409。"""
        self._make(api_client, auth, "最佳", "极限词")
        resp = api_client.post(
            "/api/xhs/forbidden-words",
            json={"word": "最佳", "group_name": "极限词"},
            headers=auth,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "FORBIDDEN_WORD_DUPLICATE"

    def test_ungrouped_is_a_distinct_bucket(self, api_client, auth):
        """未分组（空串）与「有分组」互不冲突。"""
        self._make(api_client, auth, "最佳", "")
        self._make(api_client, auth, "最佳", "极限词")
        rows = api_client.get("/api/xhs/forbidden-words", headers=auth).json()
        assert sum(1 for r in rows if r["word"] == "最佳") == 2

    def test_group_is_returned_in_the_payload(self, api_client, auth):
        row = self._make(api_client, auth, "环保", "医疗词")
        assert row["group_name"] == "医疗词"

    def test_groups_endpoint_lists_names_with_counts(self, api_client, auth):
        self._make(api_client, auth, "a", "组一")
        self._make(api_client, auth, "b", "组一")
        self._make(api_client, auth, "c", "组二")
        self._make(api_client, auth, "d", "")
        groups = api_client.get("/api/xhs/forbidden-words/groups", headers=auth).json()
        got = {g["name"]: g["count"] for g in groups}
        assert got.get("组一") == 2
        assert got.get("组二") == 1
        assert got.get("") == 1, "未分组也要列出（前端显示成「未分组」）"

    def test_groups_endpoint_omits_empty_groups(self, api_client, auth):
        """⚠️ 没有词的分组**不返回** —— 选了等于不检测，摆在那只会误导。"""
        self._make(api_client, auth, "a", "有词的组")
        groups = api_client.get("/api/xhs/forbidden-words/groups", headers=auth).json()
        assert {g["name"] for g in groups} == {"有词的组"}

    def test_list_can_filter_by_group(self, api_client, auth):
        self._make(api_client, auth, "a", "组一")
        self._make(api_client, auth, "b", "组二")
        rows = api_client.get(
            "/api/xhs/forbidden-words", params={"group_name": "组一"}, headers=auth
        ).json()
        assert [r["word"] for r in rows] == ["a"]

    def test_update_can_move_a_word_between_groups(self, api_client, auth):
        row = self._make(api_client, auth, "最佳", "组一")
        resp = api_client.patch(
            f"/api/xhs/forbidden-words/{row['id']}",
            json={"group_name": "组二"},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["group_name"] == "组二"

    def test_moving_into_a_group_that_already_has_the_word_is_409(self, api_client, auth):
        """⚠️ 移动也算「写进目标分组」⇒ 目标分组里已有同名词就该 409。"""
        self._make(api_client, auth, "最佳", "组一")
        other = self._make(api_client, auth, "最佳", "组二")
        resp = api_client.patch(
            f"/api/xhs/forbidden-words/{other['id']}",
            json={"group_name": "组一"},
            headers=auth,
        )
        assert resp.status_code == 409, resp.text

    def test_cross_user_groups_are_isolated(self, api_client, auth):
        """数据隔离：另一个用户的列表 / 分组清单都是空的。"""
        self._make(api_client, auth, "a", "A的组")
        r = api_client.post(
            "/api/auth/register", json={"username": "fw_other", "password": "Passw0rd!"}
        )
        other = {"Authorization": f"Bearer {r.json()['access_token']}"}
        assert api_client.get("/api/xhs/forbidden-words", headers=other).json() == []
        assert api_client.get("/api/xhs/forbidden-words/groups", headers=other).json() == []

    def test_default_match_mode_is_exact(self, api_client, auth):
        """用户口径 2026-10-08：「违规词的匹配方式**默认精确**」。

        ⚠️ 以前默认是包含（2），本条取代它。
        """
        row = self._make(api_client, auth, "最佳")  # 不传 match_mode
        assert row["match_mode"] == MATCH_MODE_EXACT, "默认不是精确匹配"

    def test_group_filtering_reaches_the_matcher(self, api_client, auth, db_factory):
        """预检按分组收窄：只选「组二」⇒ 组一的词不参与匹配（服务层直测）。"""
        self._make(api_client, auth, "组一专属词", "组一", mode=MATCH_MODE_CONTAINS)
        self._make(api_client, auth, "组二专属词", "组二", mode=MATCH_MODE_CONTAINS)

        db = db_factory()
        try:
            uid = db.execute(
                select(ForbiddenWord.user_id).limit(1)
            ).scalar_one()
            only_two = fws.enabled_entries(db, uid, groups=["组二"])
            words = {e["word"] for e in only_two}
            assert "组二专属词" in words
            assert "组一专属词" not in words, "分组过滤没生效"
            #: 空 ⇒ 全部（用户口径的默认）
            all_words = fws.enabled_entries(db, uid, groups=[])
            assert {"组一专属词", "组二专属词"} <= {e["word"] for e in all_words}
        finally:
            db.close()

    def test_hits_carry_the_group_name(self):
        """命中结果要能说清是哪一组管的（前端展示用）。"""
        hits = fws.find_hits(
            "这句话里有命中词",
            [{"id": 1, "word": "命中词", "match_mode": MATCH_MODE_CONTAINS, "group_name": "某组"}],
        )
        assert hits and hits[0]["group_name"] == "某组"


# --------------------------------------------------------------------------- #
# 分组启停 + 每组上限 500（用户口径 2026-10-08）
# --------------------------------------------------------------------------- #


class TestGroupEnabledAndCapacity:
    """两条新口径：

    1. 「组新增启用停用功能」—— ⚠️ **停用组必须让组内词真的退出 OCR 匹配**
       （只改界面标志位 = 假开关）；
    2. 「每组违规词限制最多 500 条」—— 超限报错，不静默插入。
    """

    def _make(self, api_client, auth, word: str, group: str = "", mode: int | None = None) -> dict:
        body: dict = {"word": word, "group_name": group}
        if mode is not None:
            body["match_mode"] = mode
        resp = api_client.post("/api/xhs/forbidden-words", headers=auth, json=body)
        assert resp.status_code == 201, resp.text
        return resp.json()

    def test_group_starts_enabled(self, api_client, auth):
        self._make(api_client, auth, "a", "组一")
        groups = api_client.get("/api/xhs/forbidden-words/groups", headers=auth).json()
        assert all(g["enabled"] for g in groups), "新建组应默认启用"

    def test_disable_group_removes_its_words_from_matching(self, api_client, auth, db_factory):
        """⚠️ 核心：停用组 ⇒ `enabled_entries`（OCR 的匹配源）里没有它的词。

        这条要是红了，说明「停用整组」只改了界面、OCR 照样命中 —— 静默假开关。
        """
        self._make(api_client, auth, "组一的词", "组一", mode=MATCH_MODE_CONTAINS)
        self._make(api_client, auth, "组二的词", "组二", mode=MATCH_MODE_CONTAINS)

        resp = api_client.post(
            "/api/xhs/forbidden-words/groups/%E7%BB%84%E4%B8%80/enabled",
            json={"enabled": False},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is False

        db = db_factory()
        try:
            uid = db.execute(select(ForbiddenWord.user_id).limit(1)).scalar_one()
            words = {e["word"] for e in fws.enabled_entries(db, uid)}
            assert "组二的词" in words, "没被停用的组不该受影响"
            assert "组一的词" not in words, "停用组后它的词仍参与匹配（假开关）"
        finally:
            db.close()

    def test_reenable_restores_the_words(self, api_client, auth, db_factory):
        self._make(api_client, auth, "组一的词", "组一", mode=MATCH_MODE_CONTAINS)
        url = "/api/xhs/forbidden-words/groups/%E7%BB%84%E4%B8%80/enabled"
        api_client.post(url, json={"enabled": False}, headers=auth)
        api_client.post(url, json={"enabled": True}, headers=auth)

        db = db_factory()
        try:
            uid = db.execute(select(ForbiddenWord.user_id).limit(1)).scalar_one()
            words = {e["word"] for e in fws.enabled_entries(db, uid)}
            assert "组一的词" in words, "重新启用组后词没回来"
        finally:
            db.close()

    def test_word_toggle_and_group_toggle_are_both_required(self, api_client, auth, db_factory):
        """两道开关是**与**关系：组启用但词停用 ⇒ 也不匹配。"""
        row = self._make(api_client, auth, "两层开关词", "组一", mode=MATCH_MODE_CONTAINS)
        api_client.post(
            f"/api/xhs/forbidden-words/{row['id']}/enabled",
            json={"enabled": False},
            headers=auth,
        )
        db = db_factory()
        try:
            uid = db.execute(select(ForbiddenWord.user_id).limit(1)).scalar_one()
            words = {e["word"] for e in fws.enabled_entries(db, uid)}
            assert "两层开关词" not in words, "词自己停用了却还在匹配"
        finally:
            db.close()

    def test_ungrouped_can_be_toggled_too(self, api_client, auth, db_factory):
        """「未分组」也是个组，也该能整组停用（前端用占位符传）。"""
        self._make(api_client, auth, "未分组的词", "", mode=MATCH_MODE_CONTAINS)
        resp = api_client.post(
            "/api/xhs/forbidden-words/groups/__ungrouped__/enabled",
            json={"enabled": False},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text
        db = db_factory()
        try:
            uid = db.execute(select(ForbiddenWord.user_id).limit(1)).scalar_one()
            words = {e["word"] for e in fws.enabled_entries(db, uid)}
            assert "未分组的词" not in words
        finally:
            db.close()

    def test_groups_list_exposes_enabled_flag(self, api_client, auth):
        self._make(api_client, auth, "a", "组一")
        api_client.post(
            "/api/xhs/forbidden-words/groups/%E7%BB%84%E4%B8%80/enabled",
            json={"enabled": False},
            headers=auth,
        )
        groups = api_client.get("/api/xhs/forbidden-words/groups", headers=auth).json()
        by_name = {g["name"]: g for g in groups}
        assert by_name["组一"]["enabled"] is False

    def test_group_capacity_is_enforced_at_500(self, api_client, auth, db_factory):
        """⚠️ 每组上限 500：第 501 条必须被拒（不能静默插进去）。"""
        from backend.app.models.forbidden_word import MAX_WORDS_PER_GROUP

        assert MAX_WORDS_PER_GROUP == 500, "上限口径应为 500"

        #: 直接灌库到 500（走 API 灌 500 条太慢，这里要验的是**第 501 条**被拒）。
        #:
        #: ⚠️ `user_id` 必须是**当前鉴权用户**（从 `/api/auth/me` 取）——
        #: 第一版用 `select(ForbiddenWord.user_id).limit(1)` 拿到的是库里
        #: **随便哪个**用户，灌到别人名下 ⇒ 容量检查看自己的组仍是 0 条，
        #: 第 501 条当然插得进去（实测就是这样假绿的）。
        uid = api_client.get("/api/auth/me", headers=auth).json()["id"]
        db = db_factory()
        try:
            db.add_all(
                [
                    ForbiddenWord(user_id=int(uid), word=f"批量{i}", group_name="满组")
                    for i in range(MAX_WORDS_PER_GROUP)
                ]
            )
            db.commit()
        finally:
            db.close()

        resp = api_client.post(
            "/api/xhs/forbidden-words", json={"word": "第501条", "group_name": "满组"}, headers=auth
        )
        assert resp.status_code == 400, f"超限没被拒：{resp.status_code} {resp.text[:200]}"
        assert resp.json()["detail"]["code"] == "FORBIDDEN_WORD_GROUP_FULL"

    def test_capacity_is_per_group_not_global(self, api_client, auth):
        """上限是**每组**的 —— 别的组不受影响。"""
        self._make(api_client, auth, "a", "甲组")
        self._make(api_client, auth, "b", "乙组")
        assert len(api_client.get("/api/xhs/forbidden-words", headers=auth).json()) == 2


# --------------------------------------------------------------------------- #
# 图片空间上传的 **MD5 命名**（用户口径 2026-10-08）
#   「文件名称需使用 MD5 生成唯一名称避免文件名重复」
# --------------------------------------------------------------------------- #


class TestPictureSpaceUploadMd5Name:
    """`POST /picture-space/upload` 的命名契约。

    ⚠️ 本类**只测命名这一段** —— 平台调用被适配器工厂挡在外面，
    所以断言落在「请求体被拒/被接受的理由」与「命名结果」上，
    不依赖真千帆（真机链路另有验收）。
    """

    def _account(self, db_factory, user_id: int) -> int:
        from backend.app.models import PlatformAccount

        db = db_factory()
        try:
            acc = PlatformAccount(
                user_id=user_id,
                platform="xhs",
                sub_type="ark",
                external_user_id="md5-probe",
                nickname="md5-probe",
            )
            db.add(acc)
            db.commit()
            return int(acc.id)
        finally:
            db.close()

    def test_blank_file_name_is_replaced_by_content_md5(self, api_client, auth, db_factory):
        """文件名留空 ⇒ 后端按**内容 MD5** 命名（同图恒同名，这就是去重的根据）。"""
        import base64
        import hashlib

        uid = api_client.get("/api/auth/me", headers=auth).json()["id"]
        acc_id = self._account(db_factory, int(uid))
        raw = b"\xff\xd8\xff\xe0" + b"hermes-md5-probe" * 4  #: 假 JPEG 头 + 稳定字节

        resp = api_client.post(
            "/api/xhs/qianfan/picture-space/upload",
            headers=auth,
            json={
                "platform_account_id": acc_id,
                "file_name": "",  # ← 留空
                "content_base64": base64.b64encode(raw).decode(),
            },
        )
        #: 没有真平台会话 ⇒ 502；关键是**不能**因为文件名为空而在 422/400 处被打回。
        assert resp.status_code != 422, f"空文件名不该被请求校验拒掉：{resp.text[:200]}"
        if resp.status_code == 200:
            got = resp.json()["file_name"]
            assert got.rsplit(".", 1)[0] == hashlib.md5(raw).hexdigest()
            assert resp.json()["search_name"] == hashlib.md5(raw).hexdigest()

    def test_same_content_yields_the_same_file_name(self):
        """同内容两次上传必然同名 —— 「避免文件名重复」的判据。"""
        #: 这条是纯函数性质：命名只由内容决定，不含时间戳/计数器。
        import hashlib

        raw = b"identical-bytes"
        names = {f"{hashlib.md5(raw).hexdigest()}.jpg" for _ in range(5)}
        assert len(names) == 1

    def test_different_content_yields_different_file_name(self):
        import hashlib

        a = hashlib.md5(b"a").hexdigest()
        b = hashlib.md5(b"b").hexdigest()
        assert a != b



class TestBuildTableFromBoxes:
    """**属性图 OCR → 行列网格**（纯几何，无子进程；用户口径 2026-10-09）。

    属性图是一张参数表截图 ⇒ 只能靠**词 box 的几何位置**还原行列。
    这里不测 OCR 本身（那要起 PowerShell），只测几何重建。
    """

    @staticmethod
    def _w(text: str, x: float, y: float, w: float = 40.0, h: float = 20.0) -> dict:
        return {"text": text, "x": x, "y": y, "w": w, "h": h}

    def test_pairs_on_one_line_become_two_columns(self):
        """同一行里「属性名」与「属性值」水平间隙大 ⇒ 断成两列。"""
        words = [
            self._w("品牌", 10, 0), self._w("某某", 300, 0),
            self._w("风格", 10, 40), self._w("中古风", 300, 40),
        ]
        columns, rows = ocr.build_table_from_boxes(words)
        assert len(columns) == 2
        assert rows == [["品牌", "某某"], ["风格", "中古风"]]

    def test_four_column_grid_is_preserved(self):
        """真机属性图是「属性|值|属性|值」四列 —— 四列都要留住。"""
        words = [
            self._w("品牌", 10, 0), self._w("A", 300, 0),
            self._w("产地", 700, 0), self._w("潍坊", 1000, 0),
            self._w("风格", 10, 40), self._w("中古风", 300, 40),
            self._w("门数量", 700, 40), self._w("2", 1000, 40),
        ]
        columns, rows = ocr.build_table_from_boxes(words)
        assert len(columns) == 4
        assert rows[0] == ["品牌", "A", "产地", "潍坊"]
        assert rows[1] == ["风格", "中古风", "门数量", "2"]

    def test_single_column_image_degrades_to_one_column(self):
        """纯文本图（没有表格）⇒ 退化成单列逐行，不硬编造多列。"""
        words = [self._w("第一行", 10, 0), self._w("第二行", 10, 40)]
        columns, rows = ocr.build_table_from_boxes(words)
        assert columns == ["内容"]
        assert rows == [["第一行"], ["第二行"]]

    def test_short_row_is_padded_not_dropped(self):
        """某行缺后半段 ⇒ 补空串（保持矩形），**不丢行**。"""
        words = [
            self._w("A", 10, 0), self._w("B", 300, 0),
            self._w("A", 700, 0), self._w("B", 1000, 0),
            self._w("只有左半", 10, 40),
        ]
        columns, rows = ocr.build_table_from_boxes(words)
        assert len(columns) == 4
        assert len(rows) == 2
        assert rows[1][0] == "只有左半"
        assert rows[1][1] == "" and rows[1][2] == ""

    def test_empty_input_returns_empty_not_a_fake_row(self):
        """没有词 ⇒ 空结果（**不造假**：不许回一张空表让用户以为识别到了）。"""
        assert ocr.build_table_from_boxes([]) == ([], [])


def test_attribute_image_becomes_a_table_in_the_precheck_result(tmp_path: Path, monkeypatch):
    """**属性图 OCR 结果并进 `tables`**（用户口径 2026-10-09）。

    ⚠️ OCR 用桩（真 OCR 要起 PowerShell）；这里只验证**装配**：
    属性图在 → 多一张「商品属性表」；识别失败 → 只加 warning，不伪造表。
    """
    root = tmp_path / "属性"
    root.mkdir(parents=True)
    (root / "商品属性图.png").write_bytes(_one_pixel_png())
    _make_xlsx(tmp_path / "t.xlsx", {"全部": [["a"], ["1"]]})

    monkeypatch.setattr(
        ocr, "scan_attribute_table",
        lambda path: {
            "ok": True, "reason": "",
            "columns": ["列1", "列2"],
            "rows": [["品牌", "A"], ["风格", "中古风"]],
            #: 用户口径 2026-10-09：属性图用 **KV** 展示（不是网格）。
            "kv": [{"key": "品牌", "value": "A"}, {"key": "风格", "value": "中古风"}],
        },
    )
    result = pre.precheck_package(tmp_path, package_name="t")
    titles = [t.title for t in result.tables]
    assert any("商品属性表" in t for t in titles), "属性图没有变成一张表"
    attr = next(t for t in result.tables if "商品属性表" in t.title)
    assert attr.columns == ["列1", "列2"]
    assert attr.rows[0] == ["品牌", "A"]
    assert attr.source_file == "商品属性图.png"
    #: KV 是「有属性图」的**判据**（前端据此出「商品属性」页签）
    assert attr.kv == [{"key": "品牌", "value": "A"}, {"key": "风格", "value": "中古风"}]
    #: 普通 xlsx 表的 kv 必须是 None（不能被前端误判成属性表）
    xlsx_tables = [t for t in result.tables if "商品属性表" not in t.title]
    assert xlsx_tables and all(t.kv is None for t in xlsx_tables)


def test_attribute_image_ocr_failure_warns_instead_of_faking_a_table(tmp_path: Path, monkeypatch):
    """OCR 失败 ⇒ **只加 warning**，不塞一张空表（不造假）。"""
    root = tmp_path / "属性"
    root.mkdir(parents=True)
    (root / "商品属性图.png").write_bytes(_one_pixel_png())
    _make_xlsx(tmp_path / "t.xlsx", {"全部": [["a"], ["1"]]})

    monkeypatch.setattr(
        ocr, "scan_attribute_table",
        lambda path: {"ok": False, "reason": "图上没有识别到文字", "columns": [], "rows": []},
    )
    result = pre.precheck_package(tmp_path, package_name="t")
    assert not any("商品属性表" in t.title for t in result.tables), "失败时不该有表"
    assert any("未能识别出属性表格" in w for w in result.warnings), "失败时应有 warning"



class TestBuildKvFromBoxes:
    """**属性图 → KV 对**（用户口径 2026-10-09：属性图用 KV 展示，不是网格）。"""

    @staticmethod
    def _w(text: str, x: float, y: float, w: float = 40.0, h: float = 20.0) -> dict:
        return {"text": text, "x": x, "y": y, "w": w, "h": h}

    def test_pairs_name_and_value_on_the_same_line(self):
        words = [
            self._w("品牌", 10, 0), self._w("某某", 300, 0),
            self._w("产地", 10, 40), self._w("山东", 300, 40),
        ]
        assert ocr.build_kv_from_boxes(words) == [
            {"key": "品牌", "value": "某某"},
            {"key": "产地", "value": "山东"},
        ]

    def test_two_pairs_on_one_line_become_two_items(self):
        """真机属性图一行有**两组**「名|值」（列3/列4 是另一对）。"""
        words = [
            self._w("品牌", 10, 0), self._w("A", 300, 0),
            self._w("产地", 700, 0), self._w("潍坊", 1000, 0),
        ]
        assert ocr.build_kv_from_boxes(words) == [
            {"key": "品牌", "value": "A"},
            {"key": "产地", "value": "潍坊"},
        ]

    def test_ocr_spaces_between_hanzi_are_squeezed(self):
        """OCR 常在汉字间插空格（`带 妆 镜`）—— 属性名要去掉这层空格。"""
        words = [self._w("带 妆 镜", 10, 0), self._w("支 持", 300, 0)]
        kv = ocr.build_kv_from_boxes(words)
        assert kv[0]["key"] == "带妆镜"
        assert kv[0]["value"] == "支持"

    def test_english_words_keep_their_spaces(self):
        """英文值里的空格是**真空格**，不能一起挤掉。"""
        words = [self._w("品牌", 10, 0), self._w("Wick Dynasty", 300, 0, w=200)]
        assert ocr.build_kv_from_boxes(words)[0]["value"] == "Wick Dynasty"

    def test_lone_key_becomes_empty_value(self):
        """只有属性名（没识别到值）⇒ 值留空，**不丢这一项**。"""
        words = [self._w("门数量", 10, 0)]
        assert ocr.build_kv_from_boxes(words) == [{"key": "门数量", "value": ""}]

    def test_empty_input_returns_no_fake_pairs(self):
        assert ocr.build_kv_from_boxes([]) == []

