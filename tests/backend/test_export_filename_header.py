"""商品台账导出的**响应头契约**（DEF-091，2026-10-04）。

## 这条缺陷为什么值得单独一个文件

`export_shop_items` 把**中文文件名直接写进 `Content-Disposition`**。
Starlette 按 **latin-1** 编码响应头 ⇒ `UnicodeEncodeError` ⇒ **任何一次导出都 500**
（不是边界情况，是恒炸）。而这条链路此前**零测试覆盖**，所以一直没人发现。

## ⚠️ 判据必须**真打端点**

第一版用例是「复刻端点的拼装规则再断言」⇒ 把端点改回裸中文时 **ESCAPED**
（复刻的那份没变，测试当然绿）。所以这里用 `TestClient` 发**真请求**，
读**真响应头** —— 被测对象只能是端点自己。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

# ⚠️ **必须在 `db_engine` 夹具运行前把 ORM 类注册进 `Base.metadata`**：
# 夹具里的 `_base().metadata.create_all()` 只建**已注册**的表；不 import models
# 就建出 0 张表（实测：`no such table: users`）。`test_orders.py` 靠它模块级的
# `from backend.app.models import ...` 顺带完成注册 —— 这里显式写出来。
import backend.app.models  # noqa: F401
from tests.backend.conftest import seed_owner_rows


@pytest.fixture()
def export_client(api_client, db_engine):
    """带鉴权的 `TestClient`（导出端点要登录 + 归属账号）。"""
    from backend.app.core.security import create_access_token

    seed_owner_rows(db_engine, user_id=2, account_id=7)
    session = sessionmaker(bind=db_engine)()
    session.commit()
    session.close()
    token = create_access_token(2)
    return api_client, {"Authorization": f"Bearer {token}"}


class TestExportFilenameHeader:
    def test_export_does_not_500_on_chinese_filename(self, export_client):
        """**核心判据**：真打导出端点 ⇒ 200（不是 500）。

        ⚠️ 这一条就是缺陷本身：修之前中文文件名让 Starlette 抛
        `UnicodeEncodeError` ⇒ 端点必 500。**判据是状态码**，
        不是源码里有没有写 `filename*=`。
        """
        client, headers = export_client
        resp = client.post(
            "/api/xhs/qianfan/items/export",
            json={"platform_account_id": 7},
            headers=headers,
        )
        assert resp.status_code == 200, (
            f"导出端点返回 {resp.status_code}（中文文件名进响应头会 500）：{resp.text[:200]}"
        )

    def test_response_header_is_latin1_safe_and_has_both_forms(self, export_client):
        """响应头必须：① 能过 latin-1；② 同时给 ASCII 回退与 RFC 5987。"""
        client, headers = export_client
        resp = client.post(
            "/api/xhs/qianfan/items/export",
            json={"platform_account_id": 7},
            headers=headers,
        )
        got = resp.headers.get("content-disposition", "")
        assert "attachment" in got
        # ① ASCII 回退（老客户端认它）
        assert 'filename="shop-items-' in got, f"缺 ASCII 回退：{got}"
        # ② RFC 5987 的 UTF-8 形态（现代浏览器认它）
        assert "filename*=UTF-8''" in got, f"缺 RFC 5987 形态：{got}"
        # ③ 不能有裸中文（那正是 500 的成因）
        assert not any("\u4e00" <= ch <= "\u9fff" for ch in got), f"响应头有裸中文：{got}"
        # ④ 中文以百分号编码出现
        assert "%E5%95%86%E5%93%81" in got  # 「商品」

    def test_source_imports_quote(self):
        """**源码守卫**：`quote` 必须在 import 列表里。

        ⚠️ 这正是半成品翻车的地方（写了 `filename*=` 却没 import ⇒ NameError ⇒ 500）。
        只断言「源码里有 filename*=」会让它绿，所以单独查 import。
        """
        from pathlib import Path

        src = Path("backend/app/api/platforms/xhs/qianfan.py").read_text(encoding="utf-8")
        assert "from urllib.parse import quote" in src, (
            "qianfan.py 用了 quote 但没 import（NameError ⇒ 导出 500）"
        )

    def test_starlette_really_rejects_bare_chinese(self):
        """钉死「为什么需要修」：裸中文进响应头 ⇒ Starlette 抛编码错。

        这条不测产品代码，是**缺陷成因的证据**。若哪天 Starlette 改 UTF-8，
        它会变红 —— 那时是**好消息**（可简化），但要人来看一眼。
        """
        from starlette.responses import Response

        with pytest.raises(UnicodeEncodeError):
            Response(
                "x",
                headers={"Content-Disposition": 'attachment; filename="商品台账.csv"'},
            )
