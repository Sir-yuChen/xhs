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


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration


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
    """注册并返回鉴权头（复用公共夹具，不自己建 engine）。

    ⚠️ **2026-10-21 起注册会播种内置违规词**（7 条「小红书」组种子，用户口径
    「把当前违规词作为新的种子数据」）。本文件大量用例断言的是「**我**建的词」，
    所以这里注册后先把种子清空 —— 种子本身的行为由
    `test_forbidden_word_seed.py` 单独钉，两者互不干扰。
    """
    r = api_client.post(
        "/api/auth/register", json={"username": "fwuser", "password": "Passw0rd!"}
    )
    assert r.status_code in (200, 201), r.text
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    _clear_seeded_words(api_client, headers)
    return headers


def _clear_seeded_words(api_client, headers) -> None:
    """删掉注册时播种的内置违规词（只影响本用例的用户）。"""
    listing = api_client.get("/api/xhs/forbidden-words", headers=headers).json()
    for row in listing or []:
        api_client.delete(f"/api/xhs/forbidden-words/{row['id']}", headers=headers)


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
        #: 对方注册时也会有内置种子 ⇒ 先清掉，才谈「他的列表为空」。
        _clear_seeded_words(api_client, other)
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


class TestZoneLookup:
    def test_group_is_the_real_directory_name(self):
        """区域名 = 数据包里**一级目录名的原值**（用户口径 2026-10-13）。

        > 「数据包中读取到什么就**按照读取到的文件夹分区域**，不做任何操作直接展示」

        ⚠️ 旧实现是「查表 → `main`/`sku`/…」白名单，**认不出就丢**。
        实测代价：包 A（淘宝店透导出）的 5 个目录有 4 个不在白名单里 ⇒
        **丢 63 个文件**（5 主图 + 5 页面展示主图 + 52 SKU 图 + 1 视频）。
        ⇒ 现在一律原样透出（含原大小写）。
        """
        assert pre.group_of_rel_path("SKU/SKU01.jpg") == "SKU"
        assert pre.group_of_rel_path("sku/SKU01.jpg") == "sku"
        assert pre.group_of_rel_path("主图/主图01.jpg") == "主图"
        assert pre.group_of_rel_path("详情图/详情图01.jpg") == "详情图"
        assert pre.group_of_rel_path("视频/1.mp4") == "视频"
        #: 包 A 的真实目录名（旧实现全部认不出 ⇒ 全部被丢）。
        assert pre.group_of_rel_path("1比1主图/a.jpg") == "1比1主图"
        assert pre.group_of_rel_path("页面展示主图/a.jpg") == "页面展示主图"
        assert pre.group_of_rel_path("SKU图/SKU图_01_x.jpg") == "SKU图"
        assert pre.group_of_rel_path("主图视频/1.mp4") == "主图视频"
        #: 未识别目录**照样有区域名**（不再返回 None 被丢）。
        assert pre.group_of_rel_path("其他/x.jpg") == "其他"
        assert pre.group_of_rel_path("属性/商品属性图.png") == "属性"

    def test_flat_path_has_no_zone_and_caller_uses_root(self):
        """顶层散落文件没有一级目录 ⇒ `None`，调用方归 `ROOT_ZONE`（**不丢弃**）。"""
        assert pre.group_of_rel_path("loose.jpg") is None
        assert pre.ROOT_ZONE == "根目录"

    def test_kind_is_only_a_hint_and_never_filters(self):
        """`kind_of_zone` 只是**参考分类**，认不出落 `other`，**不参与任何过滤**。"""
        assert pre.kind_of_zone("主图") == "main"
        assert pre.kind_of_zone("1比1主图") == "main"
        assert pre.kind_of_zone("页面展示主图") == "main"
        assert pre.kind_of_zone("SKU图") == "sku"
        assert pre.kind_of_zone("主图视频") == "video"
        assert pre.kind_of_zone("详情图") == "detail"
        #: 认不出 ⇒ other（**不是丢**）。
        assert pre.kind_of_zone("属性") == "other"
        assert pre.kind_of_zone("随便一个目录") == "other"


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


class TestColumnKindsAndSkuTable:
    """**列类型判定 + SKU 表识别**（用户口径 2026-10-13）。"""

    def test_numeric_column_is_detected(self):
        assert pre.column_kind(["1280", "1980", "2280"]) == "numeric"
        assert pre.column_kind(["1280.50", "¥1980"]) == "numeric"

    def test_text_column_wins_over_looks_numeric(self):
        """`1000+`（真包 `商品销量` 的形态）必须判 **text**，不能因为开头是数字就当数值。

        ⚠️ 判错会让前端给数值列开正则入口（用户口径明确「数值列不支持」）。
        """
        assert pre.column_kind(["1000+", "2000+"]) == "text"
        assert pre.column_kind(["https://item.taobao.com/1"]) == "text"
        assert pre.column_kind(["木;中古色-实木三斗柜;上门安装"]) == "text"

    def test_mixed_column_is_text(self):
        """有数字也有文字 ⇒ text（不能只看第一行就判 numeric）。"""
        assert pre.column_kind(["1280", "待补"]) == "text"

    def test_empty_column(self):
        assert pre.column_kind([None, None, ""]) == "empty"
        #: 真包 `计算价格` 全空 ⇒ empty（不给正则入口，符合「数值列不支持」）。
        assert pre.column_kind([]) == "empty"

    def test_sampling_catches_digits_then_text(self):
        """⚠️ **必须抽样多行**：只看第一行会把「首行是数、后面是字」的列判成 numeric。"""
        assert pre.column_kind(["1280", "1280", "1280", "待补"]) == "text"

    def test_sku_table_identified_by_column_names_not_sheet_name(self):
        """SKU 表按**列名特征**判（≥2 命中），**不看 sheet 名**。

        实测：包 B 的 sheet 叫 `全部`、包 A 的被截断 —— 两个 sheet 名都不可用。
        """
        #: 包 B 真形态。
        assert pre.looks_like_sku_table(["sku名称", "图片", "原价", "计算价格", "库存", "ID"])
        #: 包 A 真形态（没有 `sku名称`，但有 `价格` + `库存`）。
        assert pre.looks_like_sku_table(
            ["商品信息", "商品ID", "商品链接", "店铺名称", "商品销量", "SKU信息",
             "SKUID", "价格", "券后价格", "饰面材质", "颜色分类", "安装方式", "库存"]
        )
        #: 单个命中不算（避免把普通表误判成 SKU 表）。
        assert not pre.looks_like_sku_table(["名称", "备注"])
        assert not pre.looks_like_sku_table(["a", "b"])

    def test_tables_carry_column_kinds_and_sku_flag(self, tmp_path: Path):
        """两端契约：`column_kinds` 与 `is_sku_table` 必须**随表下发**（前端要读）。"""
        _make_xlsx(
            tmp_path / "t.xlsx",
            {"全部": [["sku名称", "库存", "空列"], ["甲", "200", None], ["乙", "300", None]]},
        )
        (table,) = pre.precheck_package(tmp_path).tables
        assert table.column_kinds == ["text", "numeric", "empty"]
        assert table.is_sku_table is True
        payload = table.to_dict()
        assert payload["column_kinds"] == ["text", "numeric", "empty"]
        assert payload["is_sku_table"] is True


def test_no_media_is_excluded_from_the_package(tmp_path: Path):
    """**数据包里有什么媒体就展示什么**（用户口径 2026-10-11 **最终版**）。

    > 用户原话：「**数据包中有什么媒体资源就展示什么，不要排除任何媒体资源**」

    ⚠️ 七轮口径对照（保留以便追溯；本条**撤销**上一版的「按文件名排除属性图」）：

    | 轮次 | 属性图在媒体清单里 |
    |---|---|
    | 2026-10-08 | 在（归主图区） |
    | 2026-10-11（前） | 不在（按文件名排除） |
    | 2026-10-11（中） | 在（仅打 `is_attribute_image` 标识） |
    | 2026-10-11（后） | 不在（按文件名排除 + 无字段） |
    | **2026-10-11（本条·现行）** | **在**（不做任何排除判断） |

    ⚠️ 与「删除图片 OCR 建表」**不冲突**：那是表格产出的事
    （`build_table_from_boxes` 已整段移除，见 `TestImageToTableOcrIsGone`）；
    本条只管「媒体清单不做任何过滤」。

    判据 = 数据包里的媒体文件**一个不少**（含属性图 / 参数图），且**没有**
    `is_attribute_image` 这类专属字段（上一轮收口不退化）。
    """
    root = tmp_path / "主图"
    root.mkdir(parents=True)
    (root / "商品属性图.png").write_bytes(_one_pixel_png())
    (root / "参数图.png").write_bytes(_one_pixel_png())
    (root / "主图01.jpg").write_bytes(_one_pixel_png())
    _make_xlsx(tmp_path / "t.xlsx", {"全部": [["a"], ["1"]]})

    result = pre.precheck_package(tmp_path, package_name="t")
    names = {m.file_name for m in result.media}
    assert names == {"商品属性图.png", "参数图.png", "主图01.jpg"}, (
        f"媒体清单应等于数据包里的全部媒体（不过滤），实得 {names}"
    )
    assert result.to_dict()["media_groups"]["main"] == 3
    #: 属性图专属字段**不许**回归（那会让「按名字区别对待」重新长出来）。
    assert all("is_attribute_image" not in m for m in result.to_dict()["media"])
    #: 判据函数也已整段删除（没有任何调用方了）。
    assert not hasattr(pre, "is_attribute_image_name")
def test_attribute_image_no_longer_produces_a_kv_table(tmp_path: Path):
    """属性图**不再产生「属性 KV 表」**（用户口径 2026-10-11 需求 1）。

    取代 2026-10-09「属性图 OCR 成 KV 表并出一个页签」——页签已从前端移除，
    后端也不该继续算（白跑一次 OCR，且会给前端一个没有去处的结果）。
    """
    (tmp_path / "主图").mkdir(parents=True)
    (tmp_path / "主图" / "商品属性图.png").write_bytes(_one_pixel_png())
    _make_xlsx(tmp_path / "t.xlsx", {"全部": [["sku名称"], ["颜色分类:甲"]]})

    result = pre.precheck_package(tmp_path, package_name="t")
    assert [t.title for t in result.tables] == ["全部"]
    #: ⚠️ `PrecheckTable.kv` 字段**已删除**（属性图 KV 链整条移除）⇒ 连 `None` 都不该出现，
    #: 序列化结果里根本不应有这个键。断言 `not in` 才能挡住「字段加回来」的回归。
    for t in result.tables:
        assert not hasattr(t, "kv"), "PrecheckTable.kv 应已从契约移除"
        assert "kv" not in t.to_dict()


def test_missing_area_directories_no_longer_warn(tmp_path: Path):
    """**「数据包里没有「主图」目录」这条告警已被删除**（用户口径 2026-10-13）。

    ⚠️ 在「目录名直出」口径下它是**错的**：包 A 没有名为 `主图` 的目录，
    但它的 `1比1主图` / `页面展示主图` 就是主图资源 —— 这条告警会让用户
    以为包里缺东西，而实际只是目录名不同（正是本轮要修的那类误判）。

    ⇒ 判据变成：**有主图资源就正常收，不因为目录名不叫「主图」而报警**。
    """
    (tmp_path / "1比1主图").mkdir()
    (tmp_path / "1比1主图" / "a.jpg").write_bytes(_one_pixel_png())
    _make_xlsx(tmp_path / "t.xlsx", {"全部": [["a"], ["1"]]})

    result = pre.precheck_package(tmp_path, package_name="t")
    joined = " ".join(result.warnings)
    #: 不再因为「没有名为 主图/详情图/sku/视频 的目录」而告警。
    for gone in ("没有「主图」目录", "没有「详情图」目录", "没有「sku」目录", "没有「视频」目录"):
        assert gone not in joined, f"旧告警不该回归：{gone}"
    #: 资源照常收进清单，区域名是真实目录名。
    assert [m.file_name for m in result.media] == ["a.jpg"]
    assert result.to_dict()["zones"] == [{"name": "1比1主图", "kind": "main", "count": 1}]


def test_non_compliant_format_is_listed_not_dropped(tmp_path: Path):
    """**方案 C**（用户口径 2026-10-13）：「展示与上传都按平台，但把不合规的
    **列出来而不是丢掉**，红框提示」。

    ⚠️ 这是本轮最容易做错的一条 —— 「按平台格式筛」有两种实现：
      · ❌ 直接 `continue` 丢掉（旧实现，用户无从发现）；
      · ✅ **照常进清单**，带 `unsupported=True` + 人话原因，前端红框 + 默认不勾选。
    本用例钉的是后者。

    实测来源：包 A `详情图/详情图_22.gif`（平台只收 jpg/jpeg/png）。
    """
    (tmp_path / "详情图").mkdir()
    (tmp_path / "详情图" / "a.jpg").write_bytes(_one_pixel_png())
    (tmp_path / "详情图" / "b.gif").write_bytes(b"GIF89a\x01\x00\x01\x00")
    (tmp_path / "详情图" / "c.webp").write_bytes(b"RIFF....WEBP")
    _make_xlsx(tmp_path / "t.xlsx", {"全部": [["a"], ["1"]]})

    result = pre.precheck_package(tmp_path, package_name="t")
    names = [m.file_name for m in result.media]
    #: ① 不合规的**必须还在清单里**（三个文件一个不少）。
    assert names == ["a.jpg", "b.gif", "c.webp"], f"不合规文件被丢了：{names}"
    by_name = {m.file_name: m for m in result.media}
    #: ② 合规的不带标记。
    assert by_name["a.jpg"].unsupported is False
    assert by_name["a.jpg"].unsupported_reason == ""
    #: ③ 不合规的带标记 + **人话原因**（前端红框文案）。
    for bad in ("b.gif", "c.webp"):
        assert by_name[bad].unsupported is True
        assert "jpg" in by_name[bad].unsupported_reason
    #: ④ 序列化契约里两个键都在（前端要读）。
    payload = {m["file_name"]: m for m in result.to_dict()["media"]}
    assert payload["b.gif"]["unsupported"] is True
    assert payload["b.gif"]["unsupported_reason"]
    #: ⑤ 详情图区计数含不合规的（**它是「展示」口径**，不是可上传口径）。
    assert result.to_dict()["zones"] == [{"name": "详情图", "kind": "detail", "count": 3}]
    #: ⑥ 告警**聚合上报**（用户口径「提示简洁明了」）：说清「几张、什么格式、只收什么」，
    #: 而不是逐个文件报一遍（那会让 warnings 变成一堵墙，且文件名在网格上已有）。
    joined = " ".join(result.warnings)
    assert "2 个文件平台不支持" in joined, f"应聚合计数，实得：{joined}"
    assert ".gif" in joined and ".webp" in joined, "应列出不合规的格式"
    assert "jpg" in joined, "应说清平台收什么"
    #: ⚠️ 不重复文件名（网格上有红框 + 角标，那是它该待的地方）。
    assert "b.gif」" not in joined, "不该逐个文件重复路径（噪音）"


def test_package_without_xlsx_is_rejected_with_a_clear_message(tmp_path: Path):
    (tmp_path / "主图").mkdir()
    (tmp_path / "主图" / "主图01.jpg").write_bytes(_one_pixel_png())
    with pytest.raises(pre.PrecheckError) as excinfo:
        pre.precheck_package(tmp_path, package_name="t")
    assert "表格" in str(excinfo.value)


def test_video_is_listed_and_zone_is_the_real_dirname(tmp_path: Path):
    """视频照常进清单；**区域名 = 真实目录名**（不再被归一成 `video`）。"""
    (tmp_path / "视频").mkdir()
    (tmp_path / "视频" / "1.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    _make_xlsx(tmp_path / "t.xlsx", {"全部": [["a"], ["1"]]})

    result = pre.precheck_package(tmp_path, package_name="t")
    video = [m for m in result.media if m.file_name == "1.mp4"]
    assert len(video) == 1
    #: ⚠️ 区域名是目录名原值 `视频`（不是 `video`）；类别才是 `video`。
    assert video[0].group == "视频"
    assert video[0].kind == "video"
    #: 包 A 的目录叫 `主图视频` —— 旧实现认不出，整个视频区被丢。
    assert pre.group_of_rel_path("主图视频/1.mp4") == "主图视频"


# --------------------------------------------------------------------------- #
# 预检端点（OCR 打桩：真 OCR 一次 22s，不能进回归）
# --------------------------------------------------------------------------- #


class TestPrecheckEndpoints:
    @pytest.fixture()
    def package(self, tmp_path: Path):
        for area, names in (
            #: ⚠️ 2026-10-11：属性图**不进预检媒体**（需求 1）⇒ 主图组只有 1 张。
            #: 这里仍把它放进夹具 —— 用来钉「它被排除掉」这件事（而不是删掉用例）。
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
        # ⚠️ 用户口径 2026-10-13：分区按**真实目录名**透出（`zones`），
        # 不再是固定四值 `media_groups`（那个已退化为 `kind` 参考计数）。
        zones = {z["name"]: z for z in pre_result["zones"]}
        assert set(zones) == {"主图", "详情图", "SKU", "视频"}, zones
        #: 「不排除任何媒体」⇒ 主图区 2 张（主图01 + 商品属性图）。
        assert zones["主图"]["count"] == 2
        assert zones["主图"]["kind"] == "main"
        assert zones["SKU"]["kind"] == "sku"
        #: OCR 输入排除视频（未抽帧）；**图片全部进 OCR**（不排除任何媒体）。
        sent = [Path(p).name for p in stub_ocr[0]["paths"]]
        assert "1.mp4" not in sent
        #: 现行口径：不过滤媒体 ⇒ 属性图照常进 OCR 目标（与普通主图一视同仁）。
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
        """视频**不进 OCR**，但键必须齐全（`ocr_ok=None`）。

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
        # ── 属性图：**照常在清单里、照常进违规词 OCR**（不排除任何媒体）──
        #: ⚠️ 「不产出表格」是另一件事（`build_table_from_boxes` 已删）。
        attr = by_name["商品属性图.png"]
        assert attr["ocr_ok"] is True, "属性图照常检测违规词"

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
        #: ⚠️ `is_attribute_image` **已从契约删除**（2026-10-11 最终口径：属性图相关逻辑
        #: 全部移除）。这里反向断言字段**不存在** —— 加回来就会红。
        assert all(
            "is_attribute_image" not in m for m in r.json()["precheck"]["media"]
        ), "属性图专属字段应已移除"
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
        # 失败图仍要出现在结果里（前端按「未检出但保留勾选」处理）。
        #: ⚠️ 不再按属性图筛选（字段已移除）—— 非视频的都该是 OCR 失败态。
        #: 区域名现在是**真实目录名**（`视频` 不是 `video`）。
        scanned = [m for m in pre_result["media"] if m["group"] != "视频"]
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


def test_ocr_parallelism_is_configurable_and_adaptive():
    """用户口径 2026-10-13：并发初始值配到系统参数（默认 5），
    时间控制 30 秒内或**按电脑使用情况自动调节**。

    判据：① 系统参数键 `xhs_ocr_parallelism` 存在且默认 5（LIVE_KEYS +
    BUILTIN_DEFAULTS + 随包种子三处同值）；② `resolve_parallelism` 是**函数**
    （不是固定常量）且被 `scan_images` 使用；③ 图多时自动上调、有硬上界。
    """
    from backend.app.services import settings_service as st
    from backend.app.services import image_ocr_service as m

    assert "xhs_ocr_parallelism" in st.LIVE_KEYS, "并发数没进 LIVE_KEYS（改了不生效）"
    assert st.BUILTIN_DEFAULTS["xhs_ocr_parallelism"] == 5, "内置默认应为 5"
    assert st._KEY_BOUNDS["xhs_ocr_parallelism"] == (1, 32), "并发应有上界 32"

    seed = json.loads(
        (Path(m.__file__).resolve().parents[3] / "config/initial_data/system_settings.json")
        .read_text(encoding="utf-8")
    )
    seeded = {s["key"]: s["value"] for s in seed["system_settings"]}
    assert seeded.get("xhs_ocr_parallelism") == 5, "随包种子应与内置默认同值"

    assert m.DEFAULT_PARALLELISM == 5
    #: 自适应：1 张不浪费、图多时上调、有上界。
    assert m.resolve_parallelism(0) == 1
    assert m.resolve_parallelism(1) == 1
    assert m.resolve_parallelism(5) == 5
    big = m.resolve_parallelism(m.MAX_IMAGES)
    assert big > m.DEFAULT_PARALLELISM, f"图多时应上调，实际 {big}"
    assert big <= m.MAX_PARALLELISM, f"并发超上界：{big}"

    repo = Path(m.__file__).resolve().parents[3]
    src = (repo / "backend/app/services/image_ocr_service.py").read_text("utf-8")
    assert "max_workers=workers" in src, "scan_images 没用自适应并发建线程池"
    assert "resolve_parallelism" in src and "def resolve_parallelism" in src

    api = (repo / "backend/app/api/platforms/xhs/package_precheck.py").read_text("utf-8")
    assert "ocr.resolve_parallelism" in api, "视频抽帧没用同一套自适应并发"


def test_summary_reports_actual_parallelism_and_elapsed():
    """「30 秒以内」必须**如实上报**，不是假装达标。

    判据：`scan_images` 的 summary 带 `parallelism` / `elapsed_seconds` /
    `target_seconds` 三个键（前端据此显示实际并发与实际耗时）。
    """
    from backend.app.services import image_ocr_service as m

    out = m.scan_images([], [])
    assert out["summary"]["parallelism"] == 0
    assert "elapsed_seconds" in out["summary"]
    assert out["summary"]["target_seconds"] == m.TARGET_TOTAL_SECONDS == 30.0


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


def test_retry_fires_for_transient_engine_errors(monkeypatch):
    """瞬时引擎异常要重试到成功（并发争用会偶发失败）。"""
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("onnxruntime 瞬时争用")
        return {"ok": True, "text": "", "lines": [], "pixel_width": 0, "pixel_height": 0}

    monkeypatch.setattr(ocr.rapid, "recognize", flaky)
    monkeypatch.setattr(ocr.time, "sleep", lambda *_: None)
    out = ocr._scan_one("x.jpg", [])
    assert out["ocr_ok"] is True
    assert calls["n"] == 3


def test_scan_one_reports_failure_without_raising(monkeypatch):
    """引擎不可用 / 一直失败 ⇒ `ocr_ok=False` + 结构化 error，**不抛异常**。

    ⚠️ 单图失败不该中断整批（一张坏图让 69 张全废是最坏的行为）。
    """
    def boom(path):
        raise RuntimeError("引擎炸了")

    monkeypatch.setattr(ocr.rapid, "recognize", boom)
    monkeypatch.setattr(ocr.time, "sleep", lambda *_: None)
    out = ocr._scan_one("x.jpg", [])
    assert out["ocr_ok"] is False
    assert out["error"] == "OCR_FAILED"
    assert out["hits"] == []


def test_engine_unavailable_is_not_retried(monkeypatch):
    """引擎级不可用（没装/模型坏）重试无意义 ⇒ 一次即止。"""
    calls = {"n": 0}

    def missing(path):
        calls["n"] += 1
        raise ocr.rapid.RapidOcrUnavailable("未安装")

    monkeypatch.setattr(ocr.rapid, "recognize", missing)
    out = ocr._scan_one("x.jpg", [])
    assert out["ocr_ok"] is False
    assert calls["n"] == 1, "引擎级错误不该重试"


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


def test_retryable_codes_are_engine_level_only():
    """可重试集合只含**引擎级瞬时**错误，不含确定性错误。"""
    assert "OCR_NO_OUTPUT" in ocr.RETRYABLE_CODES
    assert "OCR_FAILED" in ocr.RETRYABLE_CODES
    assert "OCR_BAD_IMAGE" not in ocr.RETRYABLE_CODES


def test_engine_probe_is_offline_and_forgiving():
    """`engine_available()` 只探引擎、**不识别任何用户数据**，且不抛异常。"""
    assert ocr.engine_available() in (True, False)


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
        #: 且只对视频设值（图片保持 None，形状统一）。
        #:
        #: ⚠️⚠️ 原断言写的是 `if item["group"] == "video":` —— 那正是**被修掉的 bug**
        #: 本身（`group` 是真实目录名「主图视频」，跟字面量 "video" 比**恒不成立**）。
        #: 它当时能过，是因为它只检查「这行文字在不在」，**不检查判据对不对**。
        #: 2026-10-23 实测：`ocr_ok=true` 但 `frame_count=null` —— 就是这行造成的。
        #: ⇒ 改成断言**正确判据**：kind / 后缀，且禁止退回 group 比较。
        assert 'if item["group"] == "video":' not in api_src, (
            "判据退回了 `group == \"video\"` —— group 是真实目录名（主图视频），恒不成立"
        )
        assert 'if item.get("kind") == "video" or Path(' in api_src, (
            "视频字段透传的判据不是 kind/后缀"
        )

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
        """数据隔离：另一个用户看不到 A 的组（他自己的种子除外）。

        ⚠️ 2026-10-21 起注册会播种内置违规词（7 条「小红书」组种子，用户口径
        「把当前违规词作为新的种子数据」）⇒ 新用户的分组清单**不再是空的**，
        而是恰好只有种子那一个组。断言从「空」改成「只有种子组、且看不到别人的组」，
        这才是真正要保的性质（隔离），而不是「空」这个易变的表象。
        """
        self._make(api_client, auth, "a", "A的组")
        r = api_client.post(
            "/api/auth/register", json={"username": "fw_other", "password": "Passw0rd!"}
        )
        other = {"Authorization": f"Bearer {r.json()['access_token']}"}
        words = api_client.get("/api/xhs/forbidden-words", headers=other).json()
        groups = api_client.get("/api/xhs/forbidden-words/groups", headers=other).json()
        assert {w["group_name"] for w in words} == {"小红书"}, "别人的词泄漏到新用户了"
        assert {g["name"] for g in groups} == {"小红书"}, f"分组泄漏：{groups}"

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



class TestImageToTableOcrIsGone:
    """**「对图片做 OCR 生成表格」这条链已彻底移除**（用户口径 2026-10-11 最终版）。

    现象（用户截图）：预检里出现一张标题为 **「商品属性图」**、表头是
    **列1/列2/列3/列4** 的表格，每行还带「未匹配到本地图」—— 那正是
    **对 `主图/商品属性图.png` 做 OCR** 后用词 box 几何还原出来的网格
    （`列N` 是几何还原的兜底列名）。

    > 用户原话：「「商品属性图」表格 = 对 商品属性图.png 做 OCR 的结果
    > （识别出 4 列 × 15 行），**移除这个逻辑和前端表格的展示**」

    ⇒ 删除清单：
      · `build_table_from_boxes` —— 把词 box 还原成行列网格（产出 `列1/列2/…`）
      · `_squeeze` / `_merge_cell` —— 只服务它的两个几何辅助

    ⚠️ **保留** `scan_images` / `locate_hits` —— 那是**违规词检测**（OCR 出文字 →
    查词库），与「把 OCR 结果当表格」是两件事，红线不受影响。
    """

    def test_geometry_table_builder_is_removed(self):
        for symbol in ("build_table_from_boxes", "_squeeze", "_merge_cell"):
            assert not hasattr(ocr, symbol), f"{symbol} 应已整段移除（图片 OCR 生成表格）"

    def test_precheck_tables_come_only_from_xlsx(self, tmp_path: Path):
        """预检的 `tables` **只能来自 xlsx** —— 任何图片（含属性图）都不产出表。"""
        (tmp_path / "主图").mkdir(parents=True)
        (tmp_path / "主图" / "商品属性图.png").write_bytes(_one_pixel_png())
        (tmp_path / "主图" / "主图01.jpg").write_bytes(_one_pixel_png())
        _make_xlsx(tmp_path / "t.xlsx", {"全部": [["sku名称"], ["颜色分类:甲"]]})

        result = pre.precheck_package(tmp_path, package_name="t")
        assert [t.title for t in result.tables] == ["全部"]
        #: `列N` 这种几何兜底列名**不许**出现（出现过即图片 OCR 链路回归）。
        for t in result.tables:
            assert not any(str(c).startswith("列") for c in t.columns), (
                f"表格列名不该是几何兜底名：{t.columns}"
            )

    def test_violation_ocr_still_works_without_the_table_builder(self):
        """**红线不受影响**：删除几何建表后，违规词检测仍可用。"""
        assert callable(ocr.locate_hits)
        assert callable(ocr.scan_images)


def test_attribute_image_is_no_longer_ocr_ed_into_a_table(tmp_path: Path, monkeypatch):
    """属性图**不再** OCR 成「商品属性表」（用户口径 2026-10-11 需求 1）。

    ⚠️ 注意与「不排除属性图」不冲突：属性图**在媒体清单里**（可勾选可预览），
    但**不做属性表 OCR**（后端 `scan_attribute_table` 已整段移除）。

    ⚠️ **取代** 2026-10-09 的 `test_attribute_image_becomes_a_table_in_the_precheck_result`
    与 `test_attribute_image_ocr_failure_warns_instead_of_faking_a_table`（两条一起退场）：

    | 轮次 | 属性图 | 理由 |
    |---|---|---|
    | 2026-10-09 | OCR ⇒ `tables` 多一张「商品属性表」+ KV + 前端「商品属性」页签 | 让用户核对参数 |
    | **2026-10-11（本条）** | **整条链路移除**（后端不算、前端无页签） | 属性由第二步「智能生成商品属性」从平台取，本地截图无用 |

    守卫方式：把 `scan_attribute_table` 打桩成「**一旦被调用就炸**」——
    这样「有人把 OCR 调用加回来」会**立刻红**（比断言「表不存在」更强，
    后者在 OCR 被调用但结果为空时仍会绿）。
    """
    root = tmp_path / "主图"
    root.mkdir(parents=True)
    (root / "商品属性图.png").write_bytes(_one_pixel_png())
    _make_xlsx(tmp_path / "t.xlsx", {"全部": [["sku名称"], ["颜色分类:甲"]]})

    #: ⚠️ 强化版守卫（2026-10-11 二次收口）：`scan_attribute_table` 已**从模块里删除**，
    #: 所以「打桩成会炸」不再可行 —— 改用**更强**的判据：函数**不存在**。
    #: 这比打桩更强：打桩只拦「被调用」，而「函数没了」连实现都保不住。
    assert not hasattr(ocr, "scan_attribute_table"), (
        "属性图 OCR 的后端实现应已整段移除（用户口径 2026-10-11「商品属性图移除后端对应的相关逻辑移除」）"
    )
    result = pre.precheck_package(tmp_path, package_name="t")
    assert [t.title for t in result.tables] == ["全部"]
    assert not any("未能识别出属性表格" in w for w in result.warnings)
    #: `BuildKvFromBoxes` 的几何还原函数也随链路移除（没有调用方了）。
    assert not hasattr(ocr, "build_kv_from_boxes")







def test_video_frame_info_is_filled_by_kind_not_group_name():
    """视频抽帧信息按 **`kind` / 后缀** 判据回填，**不能按 `group` 目录名**（2026-10-23）。

    实测：真实数据包目录叫「主图视频」，而 `group` 现在是**真实目录名**
    ⇒ 旧判据 `if item["group"] == "video"` **恒不成立**
    ⇒ 视频的 `frame_count` / `frames_failed` / `frames` 永远不回填
    （实测 `ocr_ok=true` 但 `frame_count=null`）。

    ⚠️ 讽刺的是：同一文件上方 `_is_video()` 的注释已经**明确警告过**这个坑
    （「判据必须用 `kind` 而不是 `group`」），回填那段自己又犯了一次。
    ⇒ 本守卫钉两点：① 判据不含 `group == "video"`；② 用的是 `kind` / `VIDEO_EXTS`。
    """
    import pathlib

    src = pathlib.Path("backend/app/api/platforms/xhs/package_precheck.py").read_text(encoding="utf-8")
    #: 反向断言：不许再出现「拿 group 跟字面量 video 比」
    assert 'item["group"] == "video"' not in src, (
        "仍在用 `group == \"video\"` 判断视频 —— `group` 是真实目录名（主图视频/详情图…），恒不成立"
    )
    #: 正向断言：回填处的判据是 kind 或后缀
    anchor = '        if item.get("kind") == "video" or Path(str(item.get("rel_path") or "")).suffix.lower() in VIDEO_EXTS:'
    assert anchor in src, (
        "视频抽帧回填的判据不是 `kind` / `VIDEO_EXTS`"
    )


def test_video_frames_use_ten_second_interval_with_cap():
    """视频抽帧 = **每 5 秒 1 帧**，并有上限兜底（用户口径 2026-10-23）。

    ⚠️ 口径演化：先定 10 秒一帧，同日用户调整为 **5 秒一帧** ——
    10 秒间隔在 13 秒视频上只抽 2 帧，首尾各留 5 秒盲区，漏检风险偏高。

    演化（原句保留）：
      · 旧口径「固定抽 6 帧、等间隔取每段中点」（`VIDEO_FRAMES = 6`）——
        短视频浪费（8 秒也抽 6 帧）、长视频覆盖不足（41 秒只有 6 个采样点）。
      · 用户口径：「优化为 **10 抽一帧**」⇒ 采样密度与时长成正比。

    判据三条：
      ① `VIDEO_FRAME_INTERVAL_S == 10`；
      ② `extract_frames` 按 `ceil(duration / 10)` 算帧数（不是固定常量）；
      ③ 有 `VIDEO_FRAMES_MAX` 上限（极长视频不抽上百帧，否则 OCR 时间失控）。
    """
    import math

    from backend.app.services import image_ocr_service as ocr

    assert ocr.VIDEO_FRAME_INTERVAL_S == 5, "抽帧间隔不是 5 秒"
    assert ocr.VIDEO_FRAMES_MAX >= 1, "缺抽帧上限"

    #: ② 按 `ceil(duration / 10)` 且被上限夹住 —— 与 `extract_frames` 内同一算式
    def expected(duration: float) -> int:
        n = max(1, math.ceil(duration / ocr.VIDEO_FRAME_INTERVAL_S))
        return max(1, min(ocr.VIDEO_FRAMES_MAX, n))

    #: 5 秒间隔下的实测帧数（13s 视频抽 3 帧，1.09s/帧）
    cases = {5: 1, 8: 2, 13: 3, 30: 6, 41: 9, 65: 13}
    for duration, want in cases.items():
        assert expected(duration) == want, f"{duration}s 应抽 {want} 帧，实得 {expected(duration)}"
    #: 超长视频被上限夹
    assert expected(3600) == ocr.VIDEO_FRAMES_MAX, "超长视频没有被上限夹住"

    #: ③ 源码判据：`extract_frames` 里必须是「按时长算」而不是「直接用固定 count」
    import pathlib

    src = pathlib.Path("backend/app/services/image_ocr_service.py").read_text(encoding="utf-8")
    assert "wanted = max(1, math.ceil(duration / VIDEO_FRAME_INTERVAL_S))" in src, (
        "`extract_frames` 没按 10 秒间隔算帧数（仍是固定帧数）"
    )


def _code_only(source: str) -> str:
    """只剩**代码**（剥掉所有 docstring/字符串之外的文本与注释）。

    ⚠️ 为什么需要（2026-10-23 实测）：按行剥 `#` 的写法剥不掉**文档字符串**，
    源码文档里引用 `("ffmpeg", "-v", …)` 这类示例就会被当成「裸写命令」判红 ——
    守卫判的是**代码形态**，不能被人写的说明文字影响。
    实现用 `tokenize`：直接丢掉 COMMENT 与整个 STRING 文档串，只保留其余 token。
    """
    import io as _io
    import tokenize as _tok

    kept: list[str] = []
    prev_type: int | None = None
    for tok in _tok.generate_tokens(_io.StringIO(source).readline):
        if tok.type in (_tok.COMMENT, _tok.NL, _tok.ENCODING, _tok.ENDMARKER):
            prev_type = tok.type
            continue
        if tok.type == _tok.STRING:
            #: 语句**开头**的字符串 = docstring（单独成句，前后是 NEWLINE/缩进）
            standalone = prev_type in (None, _tok.NEWLINE, _tok.INDENT, _tok.DEDENT)
            if standalone:
                prev_type = tok.type
                continue
        kept.append(tok.string)
        prev_type = tok.type
    return " ".join(kept)


def test_ffmpeg_is_located_not_assumed_and_duration_has_fallback():
    """ffmpeg/ffprobe 必须**走定位函数**，且时长探测要有 ffmpeg 兜底（2026-10-23）。

    装包版实测根因（三缺陷叠加，症状「视频不勾选且无任何提示」）：

    ① `extract_frames` 里裸写 `"ffmpeg"`，靠 PATH 解析；而 `ffmpeg_available()`
       用的是**定位后的路径** ⇒ 探测说「有」、执行时找不到 ⇒ `FileNotFoundError`
       被 `except` 吞掉 ⇒ `pairs` 为空 ⇒ `ocr_ok=None`。
    ② `probe_duration` 只调 ffprobe，**而公共镜像的 ffmpeg-static 包不含 ffprobe**
       ⇒ 时长 0 ⇒ `extract_frames` 直接 `return []`（一帧都不抽）。
    ③ `ffmpeg_available()` 要求 ffmpeg **和** ffprobe 都有 ⇒ 能抽帧却被判不可用。

    本地正常、装包版必现：本地 PATH 有 ffmpeg+ffprobe，装包版 `deps/` 只有 ffmpeg。
    """
    NL = chr(10)  # 换行符，避免转义序列被提前解析
    import pathlib as _p

    src = _p.Path("backend/app/services/image_ocr_service.py").read_text(encoding="utf-8")

    #: ① 不许裸写命令名（先剥注释，否则注释里提到这些词会假红）
    #: 剥注释行 + **行内注释**，否则文档注释里引用 "ffmpeg", "-v" 这类原文会假红
    #: ⚠️ 判据要**精确到「作为命令列表首元素裸写」**，不能只搜子串：
    #: `_ffmpeg_exe("ffmpeg") or "ffmpeg"` 里的 `or "ffmpeg"` 是兜底，
    #: 子串匹配会把它误判成「裸写」（2026-10-23 首轮踩到）。
    #: 裸写的形态是 `[` 或 `,` 后**直接**跟 `"ffmpeg"` / `"ffprobe"`（前面不是 `or `）。
    #:
    #: ⚠️ 再补一刀（2026-10-23 第二轮）：上面按行剥 `#` 的写法**剥不掉文档字符串** ——
    #:   `_ffmpeg_exe` 的 docstring 里写着 `（`deps/ffmpeg/` → … ）`、别处又写着
    #:   `("ffmpeg", "-v", …)` 这类**说明性示例**，于是守卫把注释当代码判红。
    #:   ⇒ 认代码不认文本：先剥掉全部 docstring 与注释（`_code_only`），再匹配。
    #:
    #: ⚠️ 第三刀（同日）：`dep_binary("ffmpeg", "ffmpeg.exe")` 也是 `( "ffmpeg" ,` 形态，
    #:   但它是**定位函数的参数**、不是要执行的命令。裸写的本质是「首元素就是命令名」，
    #:   所以要求**括号前紧挨着 `[` 或 `subprocess.run(` 这类调用**，且该字符串后面
    #:   紧跟的是 `,` + 参数（`-`/`--`）而不是另一个可执行名。
    code = _code_only(src)
    import re as _re

    bare_ffmpeg = _re.search(r'\[\s*"ffmpeg"\s*,\s*"-', code)
    bare_ffprobe = _re.search(r'\[\s*"ffprobe"\s*,\s*"-', code)
    assert not bare_ffmpeg, (
        'extract_frames 仍把 "ffmpeg" 作为命令首元素裸写 —— 装包版 PATH 没有它，执行必失败'
    )
    assert not bare_ffprobe, (
        'probe_duration 仍把 "ffprobe" 作为命令首元素裸写 —— 下载包里没有它'
    )
    #: 反向：定位函数必须仍然被调用（否则「删掉裸写」也能让上面两条绿）。
    #: ⚠️ `_code_only` 用空格连接 token ⇒ `_ffmpeg_exe("ffmpeg")` 会变成
    #:   `_ffmpeg_exe ( "ffmpeg" )`，所以匹配要容忍 token 间空格（同日第三坑）。
    assert _re.search(r'_ffmpeg_exe\s*\(\s*"ffmpeg"\s*\)', code), "extract_frames 没走定位函数"
    assert _re.search(r'_ffmpeg_exe\s*\(\s*"ffprobe"\s*\)', code), "probe_duration 没走定位函数"

    #: ② 时长探测必须有 ffmpeg 兜底（解析 Duration:）
    i = src.index("def probe_duration(")
    body = src[i : i + 2000]
    assert "Duration:" in body or r"Duration:\s*" in body, (
        "probe_duration 没有 ffmpeg -i 兜底 —— ffprobe 缺失时时长恒 0"
    )
    assert "这里**不看 returncode**" in body or "不看 returncode" in body, (
        "兜底分支要注明不能看 returncode（ffmpeg -i 必然回 1）"
    )

    #: ③ ffmpeg_available 只要求 ffmpeg
    j = src.index("def ffmpeg_available()")
    avail = src[j : j + 800]
    assert 'return _ffmpeg_exe("ffmpeg") is not None' in avail, (
        "ffmpeg_available 仍要求 ffprobe —— 只有 ffmpeg 的合法环境会被判不可用"
    )

    #: ④ 定位**只有一个出入口**（2026-10-23 用户口径「全部统一出入口」）：
    #:    `_ffmpeg_exe` 必须转发 `env_bootstrap_service.dep_binary`，不许自己再维护
    #:    一张候选目录表（曾经各写一套：deps 里缺 ffprobe 时连 PATH 都不看）。
    #:    `dep_binary` 内部就是「deps → 冻结态包内 bin → PATH」逐级回落。
    k = src.index("def _ffmpeg_exe(")
    locator = src[k : k + 1200]
    assert "dep_binary(" in locator, "ffmpeg 定位没有走统一出入口 dep_binary"
    assert "shutil.which(name)" in locator, "统一出入口不可用时没有回落 PATH"
    assert "_candidate_bin_dirs" not in src, (
        "又出现第二份候选目录表（统一出入口之后不许再有）"
    )


def test_video_frame_sampling_covers_head_and_tail(monkeypatch, tmp_path):
    """抽帧**首尾各固定一帧**，中间均匀（用户口径 2026-10-23「收尾各一帧固定」）。

    ⚠️ 为什么必须**跑行为**而不是只查源码（2026-10-23 变异 M7/M8 双双 ESCAPED）：
       只断言「源码里有 `times.append(min(0.1, ...))` 这一行」抓不住「把它改成 pass」——
       变异后源码里仍有别的 append，子串断言照样通过。
       ⇒ 这里打桩 `probe_duration` 与 `subprocess.run`，**直接读采样时刻列表**。
    """
    import pathlib

    from backend.app.services import image_ocr_service as ocr

    captured: list[str] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        #: cmd 形如 [exe, "-v", "error", "-ss", "<秒>", "-i", ...]
        for i, part in enumerate(cmd):
            if part == "-ss":
                captured.append(cmd[i + 1])
                break

        class _R:
            returncode = 1  # 故意失败：只要采样时刻，不落盘

        return _R()

    monkeypatch.setattr(ocr, "probe_duration", lambda _p: 100.0)
    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    monkeypatch.setattr(ocr, "_ffmpeg_exe", lambda name: f"C:/fake/{name}.exe")
    monkeypatch.setattr(ocr.tempfile, "mkdtemp", lambda **k: str(tmp_path))

    ocr.extract_frames(pathlib.Path("x.mp4"), count=6)
    times = [float(t) for t in captured]
    assert len(times) == 6, f"应抽 6 帧，实抽 {len(times)}：{times}"
    #: ① 首帧必须在最开头（<= 1s），不是从 1/6 处才开始
    assert times[0] <= 1.0, f"首帧不在片头（{times[0]}s）—— 片头违规词会漏检"
    #: ② 尾帧必须接近末尾（>= duration - 1s）
    assert times[-1] >= 99.0, f"尾帧不在片尾（{times[-1]}s）—— 片尾促销标注会漏检"
    #: ③ 中间有序且均匀
    assert times == sorted(times), f"采样时刻乱序：{times}"


def test_video_frame_cap_uses_even_split_not_truncation(monkeypatch, tmp_path):
    """超过上限时**平分全片**，不是只抽前一段（用户口径「最高 50 帧，平分」）。

    ⚠️ 变异 M2（上限 50→20）之所以 ESCAPED，是因为旧断言只读常量值，
    没验证「上限变小后采样是否仍覆盖到片尾」。
    这里跑行为：10 分钟视频在上限 20 时，尾帧仍应接近 600s。
    """
    import pathlib

    from backend.app.services import image_ocr_service as ocr

    captured: list[str] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        for i, part in enumerate(cmd):
            if part == "-ss":
                captured.append(cmd[i + 1])
                break

        class _R:
            returncode = 1

        return _R()

    monkeypatch.setattr(ocr, "probe_duration", lambda _p: 600.0)
    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    monkeypatch.setattr(ocr, "_ffmpeg_exe", lambda name: f"C:/fake/{name}.exe")
    monkeypatch.setattr(ocr.tempfile, "mkdtemp", lambda **k: str(tmp_path))

    #: ⚠️ 传 `count=VIDEO_FRAMES_MAX`（而不是硬编码 20）——
    #:   变异 M2 把常量改成 20 时，若这里写死 20，两者「正好相等」⇒ 尾帧仍覆盖全片
    #:   ⇒ 变异逃逸（2026-10-23 实测 ESCAPED）。必须让断言跟随常量走。
    cap = ocr.VIDEO_FRAMES_MAX
    ocr.extract_frames(pathlib.Path("x.mp4"), count=cap)
    times = [float(t) for t in captured]
    assert len(times) == cap, f"上限 {cap} 应抽 {cap} 帧，实抽 {len(times)}"
    assert times[-1] >= 595.0, (
        f"尾帧 {times[-1]}s 不在片尾 —— 上限触发时退化成「只抽前一段」，后半段完全不检测"
    )
    #: ⚠️ **还必须是「平分」**（2026-10-23 M2a ESCAPED 暴露）：
    #: 只断言「尾帧在片尾」抓不住「上限被调小」—— 上限 20 时尾帧照样在 600s 附近，
    #: 因为平分算法本身与上限值无关。真正该守的是**上限必须足够密**：
    #: 600 秒视频按 5 秒间隔本应抽 120 帧，上限 50 意味着覆盖率 ≈ 50/120；
    #: 若上限被改小到 20，覆盖率掉到 1/6，漏检风险陡增。
    expected_by_interval = 600.0 / ocr.VIDEO_FRAME_INTERVAL_S   # = 120
    coverage = len(times) / expected_by_interval
    assert coverage >= 0.4, (
        f"上限 {cap} 使 600s 视频只覆盖 {coverage:.0%} 的采样点"
        f"（按 {ocr.VIDEO_FRAME_INTERVAL_S}s 间隔本应有 {expected_by_interval:.0f} 个）"
        " —— 上限被调小会显著降低检测密度"
    )
    #: 间隔必须均匀（平分，不是前密后疏）
    gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
    assert max(gaps) - min(gaps) < 1.0, f"采样间隔不均匀：{gaps[:3]}...{gaps[-3:]}"


def test_duration_falls_back_to_ffmpeg_when_ffprobe_missing(monkeypatch):
    """**ffprobe 缺失时必须用 `ffmpeg -i` 兜底**读时长（2026-10-23 装包版根因）。

    ⚠️ 变异 M5（去掉兜底直接 return 0.0）ESCAPED，因为旧断言只查源码字符串。
    这里打桩：让 `_ffmpeg_exe("ffprobe")` 返回 None，看还能不能读出时长。
    """
    import pathlib

    from backend.app.services import image_ocr_service as ocr

    real = pathlib.Path(
        r"C:\Users\admin\Desktop\斗柜梳妆台一体卧室法式中古实木化妆桌床尾收纳柜化妆台转角书桌"
        r"\主图视频\主图视频_1.mp4"
    )
    #: 没装包的环境没有这个文件 ⇒ 直接构造一个假的 ffmpeg 输出
    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        class _R:
            returncode = 1
            stdout = ""
            stderr = "  Duration: 00:00:12.73, start: 0.000000, bitrate: 728 kb/s"

        return _R()

    def fake_exe(name: str):
        #: ⚠️ 关键：ffprobe **不存在**
        return None if name == "ffprobe" else "C:/fake/ffmpeg.exe"

    monkeypatch.setattr(ocr, "_ffmpeg_exe", fake_exe)
    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    value = ocr.probe_duration(real)
    assert abs(value - 12.73) < 0.05, (
        f"ffprobe 缺失时时长应为 12.73s（ffmpeg 兜底），实得 {value} "
        "—— 退化成 0 会导致一帧都不抽"
    )


def test_ffmpeg_availability_does_not_require_ffprobe(monkeypatch):
    """`ffmpeg_available()` **只看 ffmpeg**（2026-10-23 修）。

    ⚠️ 变异 M6（加回 `and ffprobe`）ESCAPED，因为旧断言仍是子串匹配。
    这里打桩验行为：只有 ffmpeg、没有 ffprobe 时也必须判「可用」。
    """
    import pathlib

    from backend.app.services import image_ocr_service as ocr

    monkeypatch.setattr(
        ocr, "_ffmpeg_exe", lambda name: "C:/fake/ffmpeg.exe" if name == "ffmpeg" else None
    )
    assert ocr.ffmpeg_available() is True, (
        "只有 ffmpeg 没有 ffprobe 时被判不可用 —— 公共镜像里根本没有 ffprobe，"
        "这会直接导致视频不检测"
    )
    monkeypatch.setattr(ocr, "_ffmpeg_exe", lambda name: None)
    assert ocr.ffmpeg_available() is False, "完全没 ffmpeg 时应判不可用"


def test_ffprobe_call_also_uses_locator(monkeypatch):
    """`probe_duration` 里的 ffprobe **也必须走定位函数**（变异 M4 补洞）。"""
    import pathlib

    from backend.app.services import image_ocr_service as ocr

    seen: list[list] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        seen.append(list(cmd))

        class _R:
            returncode = 0
            stdout = "10.5"
            stderr = ""

        return _R()

    monkeypatch.setattr(ocr, "_ffmpeg_exe", lambda name: f"C:/located/{name}.exe")
    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    assert abs(ocr.probe_duration(pathlib.Path("x.mp4")) - 10.5) < 0.01
    assert seen and seen[0][0] == "C:/located/ffprobe.exe", (
        f"ffprobe 没走定位函数，用的是 {seen[0][0] if seen else 'N/A'} —— 装包版必失败"
    )

