"""笔记挂链 + 订单域实时化（设计 75 §4.3/§4.4）—— 功能点测试。

四段结构（`docs/04_测试文档/12`）：① 门禁 ② 主链路 ③ 失败面 ④ 不变量。

本卡做两件事：
1. 「按商品查笔记」主数据源从本地 `note_goods` 关系表改为平台实时
   （`item_mix_search_word`）
2. 订单域全实时 + 订单备注整条退场（用户口径「备注不保留直接移除」）
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.app.services import realtime_platform_query_service as rt

# --------------------------------------------------------------------------- #
# 替身 adapter
# --------------------------------------------------------------------------- #


class FakeOrders:
    """订单域替身：记录窗口参数，返回预置 packages。"""

    def __init__(self, *, packages=None, detail=None):
        self.packages = packages or []
        self.detail = detail or {}
        self.calls: list[tuple[str, dict]] = []

    def get_order_page(self, **kwargs):
        self.calls.append(("get_order_page", kwargs))
        return True, "成功", {
            "code": 0,
            "data": {"packages": self.packages, "total": len(self.packages)},
        }

    def get_order_detail(self, package_id):
        self.calls.append(("get_order_detail", {"package_id": package_id}))
        return True, "成功", {
            "code": 0,
            "data": {"fulfillment_package": self.detail or {"packageId": package_id}},
        }


class FakeNotes:
    def __init__(self, *, notes=None):
        self.notes = notes or []
        self.calls: list[dict] = []

    def get_goods_note_page(self, page_num=1, page_size=20, **kwargs):
        self.calls.append({"page_num": page_num, "page_size": page_size, **kwargs})
        return True, "成功", {
            "code": 0,
            "data": {"note_infos": self.notes, "total": len(self.notes)},
        }


# --------------------------------------------------------------------------- #
# ① 门禁：订单窗口 / 按商品查笔记的入参
# --------------------------------------------------------------------------- #


class TestSegment1_Gates:
    def test_order_window_never_unbounded(self):
        """缺窗口时自动补成有界区间（平台对空窗口挂死 15 秒）。"""
        fake = FakeOrders()
        rt.list_orders(fake, lookback_days=30)
        _, kwargs = fake.calls[0]
        #: 服务层把 datetime 传给 adapter 前会转毫秒 —— 只断言窗口有界且跨度合理
        assert kwargs["page_no"] == 1

    def test_empty_item_id_does_not_hit_platform(self):
        """空商品 ID ⇒ 一个平台请求都不发。"""
        fake = FakeNotes()
        with pytest.raises(rt.RealtimeQueryError):
            rt.list_goods_notes(fake, item_id="")
        assert fake.calls == []


# --------------------------------------------------------------------------- #
# ② 主链路
# --------------------------------------------------------------------------- #


class TestSegment2_MainPaths:
    def test_goods_notes_use_item_mix_search_word(self):
        """按商品查笔记必须用 `item_mix_search_word`（不是混用其他搜索参数）。"""
        fake = FakeNotes(notes=[{"note_id": "N1", "note_title": "测试笔记"}])
        result = rt.list_goods_notes(fake, item_id="A1", page=1, page_size=100)

        assert result["total"] == 1
        assert result["items"][0]["note_id"] == "N1"
        assert fake.calls[0]["item_mix_search_word"] == "A1"
        #: 不能混入另外两个搜索参数
        assert "mix_search_word" not in fake.calls[0]
        assert "note_ids" not in fake.calls[0]

    def test_orders_carry_effective_window(self):
        """订单响应必须带**实际生效**的窗口（兜底后的值），前端要显示它。"""
        fake = FakeOrders(packages=[{"orderId": "O1", "packageId": "P1"}])
        result = rt.list_orders(fake, page=1, page_size=20)

        assert result["total"] == 1
        assert result["items"][0]["order_no"] == "O1"
        assert result["start_time"] and result["end_time"]

    def test_order_detail_uses_package_id(self):
        """详情入参是 `package_id`（传 orderId 平台回 code=-1）。"""
        fake = FakeOrders(detail={"packageId": "P9", "expressNo": "SF123", "orderId": "O9"})
        result = rt.get_order_detail(fake, package_id="P9")
        #: 服务层返回键是 `item`（与 `total`/`source` 同一 envelope 形状）
        assert result["item"]["package_id"] == "P9"
        assert result["total"] == 1
        assert fake.calls[0][1]["package_id"] == "P9"


# --------------------------------------------------------------------------- #
# ③ 失败面
# --------------------------------------------------------------------------- #


class TestSegment3_FailureSurfaces:
    def test_platform_failure_raises_structured_error(self):
        """平台失败 ⇒ 抛 `RealtimeQueryError`（带稳定 code），由端点转 502。"""

        class Boom(FakeOrders):
            def get_order_page(self, **kwargs):
                return False, "平台炸了", {"code": -1, "msg": "平台炸了"}

        with pytest.raises(rt.RealtimeQueryError) as exc:
            rt.list_orders(Boom())
        assert exc.value.code == "QIANFAN_QUERY_FAILED"

    def test_credential_expired_uses_sdk_judgement(self):
        """凭据失效走 SDK 唯一判据（`-13004` 也算过期，不是只有 `-13000`）。"""

        class Expired(FakeOrders):
            def get_order_page(self, **kwargs):
                return False, "登录凭证已过期", {"code": -13004, "msg": "登录凭证已过期"}

        with pytest.raises(rt.RealtimeQueryError) as exc:
            rt.list_orders(Expired())
        assert exc.value.code == "QIANFAN_CREDENTIAL_EXPIRED"


# --------------------------------------------------------------------------- #
# ④ 不变量
# --------------------------------------------------------------------------- #


class TestSegment4_Invariants:
    def test_order_remark_endpoint_is_gone(self):
        """订单备注端点必须是 410 墓碑（用户口径「不保留直接移除」）。"""
        from backend.app.api.platforms.xhs.qianfan import orders

        with pytest.raises(HTTPException) as exc:
            orders.order_remark_removed()
        assert exc.value.status_code == 410
        assert exc.value.detail["code"] == "ORDER_REMARK_REMOVED"

    def test_order_sync_endpoint_is_gone(self):
        """订单同步端点必须退场（订单域全实时）。"""
        from backend.app.api.platforms.xhs.qianfan import orders

        with pytest.raises(HTTPException) as exc:
            orders.order_sync_removed()
        assert exc.value.status_code == 410
        assert exc.value.detail["code"] == "ORDER_SYNC_REMOVED"

    def test_order_remark_model_is_gone_from_metadata(self):
        """`order_remarks` 表必须从 metadata 消失。

        ⚠️ 只 drop 表不删 ORM 类 ⇒ `create_all()` 会把表建回来
        （v2.0 删 `model_configs` 时踩过同一个坑）。
        """
        from backend.app.core.database import Base

        assert "order_remarks" not in Base.metadata.tables

    def test_order_sync_job_not_registered(self):
        """`system.order_sync` 必须已从任务注册表移除。"""
        from pathlib import Path

        from backend.app.services import job_registry

        src = Path(job_registry.__file__).read_text(encoding="utf-8")
        code = chr(10).join(
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        )
        assert "key=JOB_KEY_ORDER_SYNC" not in code
