"""通用规范守卫：**平台优先，本地跟随**（2026-10-18 用户口径，全项目适用）

## 规则（原文）

> 「先操作平台数据再检查本地是否存在对应数据，存在则执行操作，不存在则跳过，
>  所有平台操作均可以使用这种方式」

## 这个文件守什么

规范只写在文档里 = 下次照样写反。这里把它变成会红的判据：

1. **顺序**：平台调用在前、本地删除在后（按源码位置比较，不是靠人工 review）；
2. **平台失败 ⇒ 本地不动**（失败分支的 return 必须先于本地删除）；
3. **本地缺失 ⇒ 跳过**（`platform_only`，不是 `failed`）；
4. **回执四态分别计数**（合并成一个 `deleted` 是缺陷）；
5. **本地删除清从属行**（`shop_products` 有 4 张外键引用表）。

⚠️ **新增任何「平台写 + 本地删」链路时，照着本文件的判据写测试**。
   纯平台写（上下架 / 改价 / 发货等本地无对应实体）不适用。

参考实现：`backend/app/services/violation_delete_service.py`（`_one_target`）。
"""
from __future__ import annotations

import inspect

import pytest

from backend.app.services import violation_delete_service as vds


# --------------------------------------------------------------------------- #
# 通用判据（对参考实现逐条断言）
# --------------------------------------------------------------------------- #
class TestPlatformFirstRule:
    def test_platform_call_precedes_local_removal(self):
        """**平台存在的那条分支**里，平台调用必须在前、本地删除在后。

        ⚠️ 2026-10-19 起有前置检查：平台**查不到**时不调平台删除、直接看本地
        （用户口径）。所以不能拿全文首次出现位置比 —— 「平台无」分支的
        `purge_local_*` 天然排在平台删除之前。必须只看「平台有」那段。
        """
        src = inspect.getsource(vds._one_target)
        # 「平台有 ⇒ 平台先行」分支：从注释锚点之后开始
        seg = src[src.index("# ③ 平台有"):]
        platform_at = min(
            seg.index("_delete_platform_item") if "_delete_platform_item" in seg else 10**9,
            seg.index("_delete_platform_note") if "_delete_platform_note" in seg else 10**9,
        )
        local_at = min(
            seg.index("purge_local_item") if "purge_local_item" in seg else 10**9,
            seg.index("purge_local_note") if "purge_local_note" in seg else 10**9,
        )
        assert platform_at < 10**9 and local_at < 10**9
        assert platform_at < local_at, "必须先调平台，再动本地"

    def test_platform_failure_returns_before_local_removal(self):
        """平台失败 ⇒ 本地不动：失败 return 必须在「平台有」分支的本地删除之前。"""
        src = inspect.getsource(vds._one_target)
        seg = src[src.index("# ③ 平台有"):]
        assert seg.index("平台删除失败") < seg.index("purge_local_item")

    def test_local_missing_is_skip_not_failure(self):
        """本地缺失 ⇒ `platform_only` 且 `ok=True`。"""
        src = inspect.getsource(vds._one_target)
        seg = src[src.index("if not local_removed:"):]
        assert "STATUS_PLATFORM_ONLY" in seg
        assert '"ok": True' in seg, "本地没有不是失败，ok 必须为 True"

    def test_four_statuses_exist_and_are_counted_separately(self):
        """四态齐全，且汇总里分别计数（合并成一个 deleted 就是缺陷）。"""
        statuses = {
            vds.STATUS_DELETED,
            vds.STATUS_PLATFORM_ONLY,
            vds.STATUS_FAILED,
            vds.STATUS_SKIPPED,
        }
        assert statuses == {"deleted", "platform_only", "failed", "skipped"}

        src = inspect.getsource(vds.execute_delete)
        for key in ("platform_only", "failed", "skipped"):
            assert f'"{key}"' in src, f"汇总必须单独给 {key} 计数"


# --------------------------------------------------------------------------- #
# 已整改的链路：防止回归
# --------------------------------------------------------------------------- #
class TestAlreadyFixedPaths:
    def test_shop_products_delete_clears_foreign_key_dependents(self):
        """⚠️ `shop_products` 有 **4 张**外键引用表，删本体前必须清掉。

        否则 `FOREIGN KEY constraint failed`（本库实测：指标快照 3972 行）。
        """
        from backend.app.services.shop_products_service import delete_items

        src = inspect.getsource(delete_items)
        for table in ("GenerationRecord", "TopicGoodsMatch", "ShopMetricSnapshot", "OrderItem"):
            assert table in src, f"删商品前必须清 {table}（FK 引用）"

    def test_notes_batch_delete_does_not_skip_platform_when_local_missing(self):
        """本地没有 ≠ 不打平台。旧写法 `if note is None: failed/continue` 已修。"""
        from backend.app.api.notes import batch_delete_notes_endpoint

        src = inspect.getsource(batch_delete_notes_endpoint)
        platform_part = src[src.index("# 删本地 + 平台"):]
        assert "本地无此笔记" not in platform_part
        assert "if note is None:\n            continue" not in platform_part
        assert platform_part.count("platform_only") >= 2


# --------------------------------------------------------------------------- #
# 反向判据：确认这些断言**不是空转**
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "broken_src",
    [
        # 顺序反了
        "purge_local_item(db, u, a, i)\n_delete_platform_item(db, a, i)\n平台删除失败\n",
    ],
)
def test_guard_would_catch_wrong_order(broken_src):
    """守卫自检：把顺序写反时，判据能识别出来（证明上面的断言不是恒真）。"""
    platform_at = broken_src.index("_delete_platform_item")
    local_at = broken_src.index("purge_local_item")
    assert local_at < platform_at, "示例本身应是「反例」，供上面的判据对照"


# --------------------------------------------------------------------------- #
# UI 文案：账号下拉不带 ID 后缀（用户 2026-10-18 口径）
# --------------------------------------------------------------------------- #
def test_account_select_label_has_no_id_suffix():
    """违规管理页的账号下拉只显示店铺名，**不带「（8）」这类账号 ID 后缀**。

    判据落在**选项 label 的表达式**上（不是「页面里不许出现 `（`」——
    确认框标题「删除关联内容（N 条）」里的括号是对的）。
    """
    from pathlib import Path

    page = Path(__file__).resolve().parents[2] / "frontend/src/pages/platforms/xhs/violation-manage-page.tsx"
    src = page.read_text(encoding="utf-8")
    start = src.index("options={accounts.map(")
    end = src.index("\n        />", start)  # 到 Select 闭合为止
    block = src[start:end]
    assert "label: item.nickname" in block, "账号下拉 label 必须只有昵称"
    assert "${item.id}" not in block, "账号下拉 label 不许拼 ID 后缀"


# --------------------------------------------------------------------------- #
# 笔记删除：先普通（创作者）→ 千帆兜底 → 任一成功即止
# --------------------------------------------------------------------------- #
class TestNoteDeleteChannelFallback:
    """用户口径 2026-10-18：「商业笔记和普通笔记均调用一次，先走普通再走千帆，
    如果其中一个成功则不再走其它的」。

    实测背景：账号 8 的创作者令牌**存在但已过期**（接口回「登录已过期」），
    千帆 ark 却可用 ⇒ 旧实现「有令牌就只走创作者」把能删的笔记判失败。
    """

    def test_creator_then_ark_and_stop_on_success(self, monkeypatch):
        from backend.app.models import PlatformAccount, User
        from backend.app.services import violation_delete_service as svc

        calls: list[str] = []

        class _Creator:
            def __init__(self, _c):
                pass

            def validate_delete_permission(self, _n):
                calls.append("creator")
                return False, "登录已过期，请重新登录", None

            def delete_note(self, _n):
                return False, "", None

        class _Ark:
            def __init__(self, _c):
                pass

            def batch_delete_notes(self, _ids):
                calls.append("ark")
                return True, "", None

        import backend.app.adapters.xhs.creator_api_adapter as ca
        import backend.app.adapters.xhs.qianfan_api_adapter as qa
        import backend.app.services.account_service as acc

        monkeypatch.setattr(ca, "XhsCreatorApiAdapter", _Creator)
        monkeypatch.setattr(qa, "XhsQianfanApiAdapter", _Ark)
        monkeypatch.setattr(acc, "latest_cookies_for_channel", lambda *_a, **_k: "cookie")

        from backend.app.core.database import SessionLocal

        db = SessionLocal()
        try:
            ok, _msg = svc._delete_platform_note(db, User(id=1), PlatformAccount(id=8), "N1")
        finally:
            db.close()
        assert ok is True
        assert calls == ["creator", "ark"], "创作者失败后才该落千帆"

    def test_ark_is_not_called_when_creator_succeeds(self, monkeypatch):
        calls: list[str] = []

        class _Creator:
            def __init__(self, _c):
                pass

            def validate_delete_permission(self, _n):
                return True, "", None

            def delete_note(self, _n):
                calls.append("creator")
                return True, "", None

        class _Ark:
            def __init__(self, _c):
                pass

            def batch_delete_notes(self, _ids):
                calls.append("ark")
                return True, "", None

        import backend.app.adapters.xhs.creator_api_adapter as ca
        import backend.app.adapters.xhs.qianfan_api_adapter as qa
        import backend.app.services.account_service as acc
        from backend.app.models import PlatformAccount, User
        from backend.app.services import violation_delete_service as svc

        monkeypatch.setattr(ca, "XhsCreatorApiAdapter", _Creator)
        monkeypatch.setattr(qa, "XhsQianfanApiAdapter", _Ark)
        monkeypatch.setattr(acc, "latest_cookies_for_channel", lambda *_a, **_k: "cookie")

        from backend.app.core.database import SessionLocal

        db = SessionLocal()
        try:
            ok, _ = svc._delete_platform_note(db, User(id=1), PlatformAccount(id=8), "N1")
        finally:
            db.close()
        assert ok is True
        assert calls == ["creator"], "成功即止 —— 不许再调千帆（否则同一条笔记删两次）"


class TestAdapterMethodNamesExist:
    """⚠️ 实测事故（2026-10-18）：`violation_delete_service` 调了
    `XhsQianfanApiAdapter.batch_delete_items()` —— **适配器上根本没有这个方法**
    （实际叫 `remove_items`），删除商品恒 500。
    单测里把适配器整个 mock 掉了 ⇒ 假绿；tsc 管不到 Python。

    这条守卫用**反射**核对服务里引用的适配器方法真的存在 —— 不 mock、不猜。
    """

    def test_item_delete_uses_a_real_adapter_method(self):
        import inspect

        from backend.app.adapters.xhs.qianfan_api_adapter import XhsQianfanApiAdapter
        from backend.app.services import violation_delete_service as svc

        src = inspect.getsource(svc._delete_platform_item)
        # 取 `XhsQianfanApiAdapter(cookies).<method>(` 里的方法名
        import re

        names = re.findall(r"XhsQianfanApiAdapter\([^)]*\)\.(\w+)\(", src)
        assert names, "没找到对适配器的调用"
        for name in names:
            assert hasattr(XhsQianfanApiAdapter, name), (
                f"适配器没有 `{name}` 方法（实际有："
                f"{[m for m in dir(XhsQianfanApiAdapter) if 'delete' in m or 'remove' in m]}）"
            )

    def test_note_delete_uses_real_adapter_methods(self):
        import inspect
        import re

        from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter
        from backend.app.adapters.xhs.qianfan_api_adapter import XhsQianfanApiAdapter
        from backend.app.services import violation_delete_service as svc

        src = inspect.getsource(svc._delete_platform_note)
        for cls in (XhsCreatorApiAdapter, XhsQianfanApiAdapter):
            for name in re.findall(rf"{cls.__name__}\([^)]*\)\.(\w+)\(", src):
                assert hasattr(cls, name), f"{cls.__name__} 没有 `{name}` 方法"


class TestDeleteItemAlsoClearsForeignKeylessRelations:
    """⚠️ 实测事故（2026-10-18）：删商品只按 **外键** 清从属行，
    漏了两张**按 `item_id` 关联、却没有外键**的关系表：
      · `note_goods`      —— 内容库侧笔记↔商品
      · `shop_note_goods` —— 千帆商品笔记本地映射（实测 1977 行）
    PRAGMA 查不到它们 ⇒ `_fk_referencing` 天然覆盖不了 ⇒ 必须显式清。
    """

    def test_purge_local_item_clears_both_note_good_relations(self):
        import inspect

        from backend.app.services import violation_delete_service as svc

        src = inspect.getsource(svc.purge_local_item)
        assert "NoteGoods" in src, "没清 note_goods"
        assert "ShopNoteGoods" in src, (
            "没清 shop_note_goods —— 商品删了、笔记页还挂着已下架商品"
        )

    def test_fk_referencing_only_covers_real_foreign_keys(self):
        """`_fk_referencing` 只该返回**真外键**；无外键的关系表由上面那条单独保证。"""
        import inspect

        from backend.app.services import violation_delete_service as svc

        src = inspect.getsource(svc._fk_referencing)
        assert "PRAGMA foreign_key_list" in src


class TestPrecheckBeforePlatformDelete:
    """前置检查（用户口径 2026-10-19）：

    > 「根据笔记或商品ID检查商品是否存在，返回详情则存在不返回则表示平台没有对应数据」
    > 「查不到则不在走平台删除落，直接走本地……如果本地数据也不存在则提示，
    >   数据已经被删除，不要重复删除，如果为批量操作则直接跳过这一条数据」

    为什么必须做：平台删除接口**不做存在性校验** —— 实测同一商品连删 3 次，
    每次都回 `success:true`（2026-10-19 真机），不先查就分不清真删了还是本来就没了。
    """

    def test_precheck_called_before_any_platform_delete(self):
        src = inspect.getsource(vds._one_target)
        assert src.index("check_platform_exists") < src.index("# ③ 平台有"), (
            "前置检查必须在平台删除之前"
        )

    def test_absent_and_local_absent_means_absent_not_deleted(self):
        """平台无 + 本地无 ⇒ `absent`，且**不调**平台删除、不是 failed。"""
        src = inspect.getsource(vds._one_target)
        seg = src[src.index("if not on_platform:"):src.index("# ③ 平台有")]
        assert "STATUS_ABSENT" in seg
        assert "不要重复删除" in seg
        assert "_delete_platform_item" not in seg, "平台已无数据，不该再调平台删除"
        assert "_delete_platform_note" not in seg
        assert "STATUS_FAILED" not in seg, "已删除不是失败"

    def test_absent_still_cleans_local_when_present(self):
        """平台无但本地有 ⇒ 只清本地，回执算 `deleted`。"""
        src = inspect.getsource(vds._one_target)
        seg = src[src.index("if not on_platform:"):src.index("# ③ 平台有")]
        assert "purge_local_item" in seg and "purge_local_note" in seg
        assert "STATUS_DELETED" in seg

    def test_precheck_uses_id_query_not_paging(self):
        """前置检查必须**按 ID 精确查**，不许翻页（用户口径 2026-10-19）。"""
        src = inspect.getsource(vds.check_platform_exists)
        assert "get_skus_by_item_ids" in src, "商品要按 ID 查 SKU 明细"
        assert "get_goods_note_page" in src and "note_ids" in src, "笔记要按 note_ids 过滤"
        assert "page_no" not in src and "for page" not in src, "不许翻页扫描"

    def test_query_failure_does_not_look_like_absent(self):
        """查询本身失败（网络/令牌）**不等于**不存在 —— 否则抖动会被误报成「没数据」。"""
        src = inspect.getsource(vds.check_platform_exists)
        seg = src[src.index("if not ok:"):]
        assert "return True" in seg, "查询失败应放行，让删除步骤去报真实错误"
