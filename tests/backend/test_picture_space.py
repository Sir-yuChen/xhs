"""图片空间只读代理的契约测试（设计 22 §1）。

## 这批用例盯的四件事

1. **零落库**（用户 2026-10-06 硬口径「所有数据不落库本地」）——
   三个端点跑完，**表数不变、无文件写入**。测法：跑一次列表端点，断言
   `sqlite_master` 里没有新增表、且没有触碰任何 ORM 模型（本组端点不 import 模型）。
2. **只读**——本组端点**没有**上传 / 移动 / 删除 / 下载；页面与路由都不得出现写入口。
   签名机制（`_signed_post`）只是**机制**，判据是「有没有改平台状态」。
3. **门禁只有一份**——无 ark 令牌 ⇒ 400 `ARK_SESSION_REQUIRED`（**不借会话**）。
4. **白名单**——排序字段 / 素材类型表外值 ⇒ 400，不静默退化成默认
   （静默退化会让用户以为「按上传时间排了」而实际是别的顺序）。

⚠️ 真机只读验证已单独做过（2026-10-06：账号 9「开心睡大觉的店」，
`total=4910` / `15G` 配额 / 带签名 200）；这里测的是**我们自己的判据与归一化**。
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import select

from backend.app.models import PlatformAccount, User
from backend.app.services import picture_space_service as ps

ARK_COOKIES = "access-token-ark.xiaohongshu.com=abc123"

#: 一条真机实样（字段逐字来自 2026-10-06 抓包，仅 id 做短化）。
SAMPLE_ITEM = {
    "id": "bbf0fa75-4bae-4204-9a80-4efc07f9a10a",
    "sellerId": "6a33d34ece6d1200154ad7f5",
    "type": 2,
    "name": "000709d6-fecb-4966-8d93-900b039e6258",
    "status": 1,
    "size": 73728,
    "createTime": 1784708174179,
    "updateTime": 1784708174179,
    "additionalData": {
        "extension": ".jpeg",
        "width": "800",
        "height": "800",
        "link": "https://qimg.xiaohongshu.com/material_space/bbf0fa75",
        "path": "material_space/bbf0fa75",
        "productCount": "0",
    },
}


class _Adapter:
    """替身适配器：记录被调用的方法，回固定原始响应（**不打平台**）。"""

    def __init__(self, *, ok=True, msg="成功", payload=None, calls=None):
        self.ok = ok
        self.msg = msg
        self.payload = payload or {}
        self.calls: list[tuple] = calls if calls is not None else []

    def search_material_space(self, body):
        self.calls.append(("search", body))
        return self.ok, self.msg, self.payload

    def get_material_directory(self, seller_id):
        self.calls.append(("tree", seller_id))
        return self.ok, self.msg, self.payload

    def get_material_space_usage(self):
        self.calls.append(("usage",))
        return self.ok, self.msg, self.payload


def _seed_account(db_factory, *, with_cookie=True):
    """建一个属于该用户的 xhs 账号（可选：带 / 不带 ark 令牌）。

    ⚠️ 用户取 conftest 播种的基线行 `User(id=1)` —— 不 `select(User)` 再 `.first()`
    （基线里只有 id=1 那个，用 id 直取更稳，且不依赖查询顺序）。
    """
    from tests.backend.cookie_fixtures import give_ark_cookies
    from tests.backend.conftest import seed_owner_rows

    db = db_factory()
    try:
        # 测试库刻意是空的（`conftest.db_engine` 不播种）⇒ 外键父行由本文件自己造。
        seed_owner_rows(db.get_bind(), user_id=1)
        account = PlatformAccount(
            user_id=1, platform="xhs", sub_type="qianfan",
            external_user_id="seller-x", nickname="开心睡大觉的店",
            status="active", seller_id="6a33d34ece6d1200154ad7f5",
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        account_id = account.id
        if with_cookie:
            # `give_ark_cookies` 只 add 不 commit —— 由这里统一提交。
            give_ark_cookies(db, account)
            db.commit()
    finally:
        db.close()
    return account_id


@pytest.fixture()
def qianfan_client(api_client, db_factory, monkeypatch):
    """带 xhs 账号的 TestClient + 可换的适配器桩。"""
    from backend.app.core.deps import get_current_user
    from backend.app.main import app
    from backend.app.models import User as _User

    account_id = _seed_account(db_factory)
    app.dependency_overrides[get_current_user] = lambda: _User(id=1, username="tester")
    try:
        yield api_client, account_id
    finally:
        app.dependency_overrides.pop(get_current_user, None)


class _StubAdapter:
    """图片空间端点用的适配器替身：只实现被调到的少数方法。

    其他方法不存在 ⇒ 调到时 `AttributeError`（比静默返回假值好，能抓住「不该调的调了」）。
    """

    def __init__(self, **methods):
        self._methods = methods

    def __getattr__(self, name):
        if name in self._methods:
            return self._methods[name]
        raise AttributeError(name)


def _use_adapter(monkeypatch, adapter):
    """让图片空间端点绕过真实适配器：直接替换**子模块**的 `_ark_adapter_for`。

    ⚠️ 端点调 `_ark_adapter_for(db, account, adapter_factory)` 是**模块全局查找**（调用时解析）
    —— 覆盖那个模块的属性即可，不必碰 FastAPI 依赖；monkeypatch 自动还原。

    ⚠️ **锚点是子模块，不是包**（2026-10-11 拆包后修）：拆包把
    `qianfan.py` 变成 `qianfan/` 包，端点搬进 `qianfan/picture_space.py`，
    它用 `from ._shared import _ark_adapter_for` **绑定了自己的模块全局**。
    此时再打 `qianfan._ark_adapter_for`（包级属性）**完全无效** ——
    端点在子模块里查名字，与包属性无关。
    失效形态极隐蔽：`raising=False` 连报错都没有，测试却**真的去调平台**
    （实测表现：`ARK_SESSION_REQUIRED` / 502，看着像业务坏了）。
    """
    monkeypatch.setattr(
        "backend.app.api.platforms.xhs.qianfan.picture_space._ark_adapter_for",
        lambda db, account, factory, _a=adapter: _a,
        raising=False,
    )
    #: ⚠️ **补丁生效断言**（拆包踩坑后加）：上面那条 `raising=False` 打空了也不报错，
    #: 必须显式验证锚点真的指向端点会用的那个名字 —— 做法是**调用它并比对返回**，
    #: 而不是 `callable(...)`（那恒真，等于没断言）。
    from backend.app.api.platforms.xhs.qianfan import picture_space as _ps

    assert _ps._ark_adapter_for(None, None, None) is adapter, (  # noqa: SLF001
        "monkeypatch 锚点打空了 —— 端点读的是子模块的 `_ark_adapter_for`，"
        "补丁必须打在 `qianfan.picture_space` 上（打在包 `qianfan` 上无效）"
    )


# --------------------------------------------------------------------------- #
# §1 归一化（唯一实现，纯函数）
# --------------------------------------------------------------------------- #
class TestNormalize:
    def test_display_name_joins_extension(self):
        """平台把扩展名拆进 additionalData ⇒ 后端拼回全名给列表显示。"""
        item = ps.normalize_material_item(SAMPLE_ITEM)
        assert item["display_name"] == "000709d6-fecb-4966-8d93-900b039e6258.jpeg"
        assert item["name"] == "000709d6-fecb-4966-8d93-900b039e6258"
        assert item["extension"] == ".jpeg"

    def test_referenced_flag_from_product_count(self):
        """`productCount > 0` ⇒ 已引用（真机列表的「是否引用」列）。"""
        assert ps.normalize_material_item(SAMPLE_ITEM)["referenced"] is False
        referenced = {**SAMPLE_ITEM, "additionalData": {**SAMPLE_ITEM["additionalData"], "productCount": "3"}}
        assert ps.normalize_material_item(referenced)["referenced"] is True

    def test_missing_dimensions_stay_none_not_zero(self):
        """缺尺寸 ⇒ `None`（**不是 0**）—— 前端据此渲染 `-`（缺值口径）。"""
        raw = {**SAMPLE_ITEM, "additionalData": {"extension": ".jpeg", "link": "x"}}
        item = ps.normalize_material_item(raw)
        assert item["width"] is None and item["height"] is None

    def test_search_payload_tolerates_missing_data(self):
        """`data` 缺失 ⇒ `total=0` / `items=[]`（空结果是合法结果，不抛异常）。"""
        assert ps.normalize_search_payload(None) == {"total": 0, "items": []}
        assert ps.normalize_search_payload({}) == {"total": 0, "items": []}

    def test_usage_missing_stays_none(self):
        """配额取不到 ⇒ 两个值都是 `None`（前端不渲染配额条，不显示 `0 / 0`）。"""
        got = ps.normalize_usage_payload({})
        assert got["used_space"] is None and got["total_space"] is None

    def test_build_body_clamps_page_size(self):
        """单页上限 500（与平台页面同口径：真机下拉 100 / 200 / 500）。"""
        assert ps.build_search_body(page_size=999)["pageSize"] == 500
        assert ps.build_search_body(page_size=0)["pageSize"] == 1

    def test_normalize_resize_conditions_uses_real_captured_payload(self):
        """⚠️ 用**真机抓包原文**钉死（2026-10-07）—— 平台返回的是 snake_case！

        `POST material_space/check_resize_material_space` 的原始响应键名是
        `condition_results` / `check_pass`（平台页面 JS 带 axios transform 才转驼峰）。
        曾按驼峰读 ⇒ 条件恒空、`can_apply` 恒 False。本用例用逐字抓来的原文回归。
        """
        real = {
            "code": 0,
            "success": True,
            "msg": "成功",
            "data": {
                "condition_results": [
                    {"type": 1, "desc": "有在架商品", "check_pass": True},
                    {"type": 2, "desc": "近30天有动销（消费者支付金额>0）", "check_pass": False},
                    {"type": 3, "desc": "容量使用率>=80%", "check_pass": False, "threshold": "80", "current": "3"},
                    {"type": 4, "desc": "剩余扩容次数>0", "check_pass": True, "current": "4", "threshold": "4"},
                ]
            },
        }
        got = ps.normalize_resize_conditions(real)
        assert [c["desc"] for c in got["conditions"]] == [
            "有在架商品",
            "近30天有动销（消费者支付金额>0）",
            "容量使用率>=80%",
            "剩余扩容次数>0",
        ]
        assert [c["pass"] for c in got["conditions"]] == [True, False, False, True]
        assert got["remaining"] == 4  # type=4 的 current
        assert got["can_apply"] is False

    def test_normalize_resize_conditions_all_pass(self):
        """扩容条件：全部 `check_pass` ⇒ `can_apply=True`（2026-10-07 空间扩容）。"""
        got = ps.normalize_resize_conditions(
            {
                "data": {
                    "condition_results": [
                        {"type": 1, "check_pass": True, "desc": "已实名认证", "current": ""},
                        {"type": 4, "check_pass": True, "desc": "可扩容", "current": "15200000000"},
                    ]
                }
            }
        )
        assert len(got["conditions"]) == 2
        assert got["conditions"][0]["pass"] is True
        assert got["conditions"][1]["type"] == 4
        assert got["conditions"][1]["current"] == "15200000000"
        assert got["can_apply"] is True
        assert got["white_seller"] is False

    def test_normalize_resize_conditions_any_fail_blocks_apply(self):
        """任一条不通过 ⇒ 不可申请（平台判据 `some(!checkPass)`）。"""
        got = ps.normalize_resize_conditions(
            {"data": {"condition_results": [{"type": 1, "check_pass": False, "desc": "未实名", "current": ""}]}}
        )
        assert got["can_apply"] is False

    def test_normalize_resize_conditions_empty_is_not_applyable(self):
        """取不到条件 ⇒ 空表 + `can_apply=False`（**不显示「可扩容」**）—— 缺值口径。"""
        assert ps.normalize_resize_conditions(None) == {
            "conditions": [],
            "can_apply": False,
            "white_seller": False,
            "remaining": 0,
        }
        assert ps.normalize_resize_conditions({})["can_apply"] is False

    def test_normalize_resize_conditions_white_seller_blocks_apply(self):
        """白名单卖家：条件全过但平台不给入口 ⇒ `can_apply=False`。"""
        got = ps.normalize_resize_conditions(
            {"data": {"whiteSeller": True, "condition_results": [{"type": 1, "check_pass": True, "desc": "x", "current": ""}]}}
        )
        assert got["white_seller"] is True
        assert got["can_apply"] is False


# --------------------------------------------------------------------------- #
# §2 端点契约
# --------------------------------------------------------------------------- #
class TestResizeEndpoints:
    def test_resize_check_requires_auth(self, client):
        assert (
            client.get("/api/xhs/qianfan/picture-space/resize/check?platform_account_id=1").status_code
            == 401
        )

    def test_resize_requires_auth(self, client):
        assert (
            client.post(
                "/api/xhs/qianfan/picture-space/resize", json={"platform_account_id": 1}
            ).status_code
            == 401
        )

    def test_resize_check_returns_conditions(self, qianfan_client, monkeypatch):
        """`resize/check` 把平台条件透传（desc 原样，不造）。"""
        api, account_id = qianfan_client
        _use_adapter(
            monkeypatch,
            _StubAdapter(
                check_resize_material_space=lambda: (
                    True,
                    "",
                    {
                        "data": {
                            "condition_results": [
                                {"type": 1, "check_pass": True, "desc": "已实名", "current": ""}
                            ]
                        }
                    },
                )
            ),
        )
        res = api.get(
            f"/api/xhs/qianfan/picture-space/resize/check?platform_account_id={account_id}"
        )
        assert res.status_code == 200
        body = res.json()
        assert body["conditions"][0]["desc"] == "已实名"
        assert body["can_apply"] is True

    def test_resize_check_failure_is_502(self, qianfan_client, monkeypatch):
        api, account_id = qianfan_client
        _use_adapter(
            monkeypatch,
            _StubAdapter(check_resize_material_space=lambda: (False, "平台炸了", None)),
        )
        res = api.get(
            f"/api/xhs/qianfan/picture-space/resize/check?platform_account_id={account_id}"
        )
        assert res.status_code == 502
        assert res.json()["detail"]["code"] == "MATERIAL_RESIZE_CHECK_FAILED"

    def test_resize_calls_platform_and_returns_success(self, qianfan_client, monkeypatch):
        """执行扩容：调平台 `resize_material_space`（body 空）并回 success。"""
        api, account_id = qianfan_client
        calls = []
        _use_adapter(
            monkeypatch,
            _StubAdapter(
                resize_material_space=lambda: (calls.append(1), (True, "", {"data": {}}))[1]
            ),
        )
        res = api.post(
            "/api/xhs/qianfan/picture-space/resize", json={"platform_account_id": account_id}
        )
        assert res.status_code == 200
        assert res.json()["success"] is True
        assert calls == [1]

    def test_resize_failure_is_502(self, qianfan_client, monkeypatch):
        api, account_id = qianfan_client
        _use_adapter(monkeypatch, _StubAdapter(resize_material_space=lambda: (False, "拒绝", None)))
        res = api.post(
            "/api/xhs/qianfan/picture-space/resize", json={"platform_account_id": account_id}
        )
        assert res.status_code == 502
        assert res.json()["detail"]["code"] == "MATERIAL_RESIZE_FAILED"


class TestEndpoints:
    def test_items_requires_auth(self, client):
        """未鉴权 ⇒ 401（三端点同口径）。"""
        assert client.post("/api/xhs/qianfan/picture-space/items", json={"platform_account_id": 1}).status_code == 401
        assert client.get("/api/xhs/qianfan/picture-space/tree?platform_account_id=1").status_code == 401
        assert client.get("/api/xhs/qianfan/picture-space/usage?platform_account_id=1").status_code == 401

    def test_items_returns_normalized_items(self, qianfan_client, monkeypatch):
        api, account_id = qianfan_client
        adapter = _Adapter(payload={"code": 200, "data": {"total": 4910, "directoryManageables": [SAMPLE_ITEM]}})
        from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory

        from backend.app.main import app

        # 端点用 `Depends(get_qianfan_api_adapter_factory)`：直接覆盖依赖最稳。
        app.dependency_overrides[get_qianfan_api_adapter_factory] = lambda: (lambda cookies: adapter)
        try:
            resp = api.post(
                "/api/xhs/qianfan/picture-space/items",
                json={"platform_account_id": account_id, "page_size": 10},
            )
        finally:
            app.dependency_overrides.pop(get_qianfan_api_adapter_factory, None)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 4910
        assert body["items"][0]["display_name"].endswith(".jpeg")
        assert body["page"] == 1 and body["page_size"] == 10
        assert adapter.calls and adapter.calls[0][0] == "search"

    def test_items_rejects_unknown_order_field(self, qianfan_client):
        """排序字段表外 ⇒ 400（不静默退化成默认）。"""
        api, account_id = qianfan_client
        resp = api.post(
            "/api/xhs/qianfan/picture-space/items",
            json={"platform_account_id": account_id, "order_field": "hacked"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "MATERIAL_ORDER_UNSUPPORTED"

    def test_items_rejects_unknown_basic_type(self, qianfan_client):
        """素材类型表外 ⇒ 400。"""
        api, account_id = qianfan_client
        resp = api.post(
            "/api/xhs/qianfan/picture-space/items",
            json={"platform_account_id": account_id, "basic_types": [9]},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "MATERIAL_TYPE_UNSUPPORTED"

    def test_items_without_ark_session_is_400_not_borrowed(self, api_client, db_factory):
        """无 ark 令牌 ⇒ 400 `ARK_SESSION_REQUIRED`（**不借别的账号的会话**）。"""
        from backend.app.core.deps import get_current_user
        from backend.app.main import app
        from backend.app.models import User as _User

        account_id = _seed_account(db_factory, with_cookie=False)
        app.dependency_overrides[get_current_user] = lambda: _User(id=1, username="tester")
        try:
            resp = api_client.post(
                "/api/xhs/qianfan/picture-space/items",
                json={"platform_account_id": account_id},
            )
        finally:
            app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "ARK_SESSION_REQUIRED"

    def test_usage_returns_quota(self, qianfan_client):
        api, account_id = qianfan_client
        adapter = _Adapter(payload={"code": 0, "data": {"totalSpace": 16106127360, "usedSpace": 613267456}})
        from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory

        from backend.app.main import app

        app.dependency_overrides[get_qianfan_api_adapter_factory] = lambda: (lambda cookies: adapter)
        try:
            resp = api.get(f"/api/xhs/qianfan/picture-space/usage?platform_account_id={account_id}")
        finally:
            app.dependency_overrides.pop(get_qianfan_api_adapter_factory, None)
        assert resp.status_code == 200
        assert resp.json()["total_space"] == 16106127360
        assert resp.json()["used_space"] == 613267456

    def test_other_users_account_is_404(self, qianfan_client, db_factory):
        """越权账号 ⇒ 404（归属门禁只有一份）。"""
        api, _ = qianfan_client
        resp = api.post("/api/xhs/qianfan/picture-space/items", json={"platform_account_id": 99999})
        assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# §3 零落库 / 只读（结构判据）
# --------------------------------------------------------------------------- #
class TestNoPersistence:
    def test_service_has_no_local_db_writes(self):
        """服务层**不落本地库**（零建表 / 零 ORM 写入）。

        零落库的结构判据：写操作全在**平台侧**（云端回收站），本地不建副本。
        """
        import inspect

        from backend.app.services import picture_space_service

        src = inspect.getsource(picture_space_service)
        assert "sqlalchemy" not in src, "图片空间服务不应依赖 sqlalchemy（本地零落库）"
        assert ".commit()" not in src, "图片空间服务不应有本地 commit（零落库）"

    def test_router_exposes_read_and_write_routes(self):
        """路由层：图片空间同时有**只读**（items/tree/usage）与**写**（folders/move/delete/download）。

        ⚠️ 写端点存在是**用户明确要求**（2026-10-06「操作按钮功能全部要做」）——
        与「探测 / 调研只读」的边界不冲突（那是取证场景；这里是用户对自己空间的显式操作）。
        """
        from backend.app.main import app

        paths = app.openapi()["paths"]
        ps_paths = {p: set(methods) for p, methods in paths.items() if "picture-space" in p}
        assert "/api/xhs/qianfan/picture-space/items" in ps_paths
        assert "/api/xhs/qianfan/picture-space/usage" in ps_paths
        assert "/api/xhs/qianfan/picture-space/folders" in ps_paths
        assert "/api/xhs/qianfan/picture-space/delete" in ps_paths
        # 回收站：还原 + 彻底删除（用户 2026-10-06「回收站功能补充完整」）。
        assert "/api/xhs/qianfan/picture-space/restore" in ps_paths
        assert "/api/xhs/qianfan/picture-space/permanent-delete" in ps_paths
        # 写端点都是 POST（无 PUT/PATCH/DELETE —— 平台侧语义是「批量动作」）。
        for path, methods in ps_paths.items():
            assert not (methods & {"put", "patch", "delete"}), f"{path} 不应是 REST 写方法"


class TestWriteGuards:
    def test_delete_requires_confirm_token(self, qianfan_client):
        """⚠️ 删除**两层确认**：无 `confirmed` / 无 token ⇒ 400，**一次都不打平台**。"""
        api, account_id = qianfan_client
        item = {"id": "x1", "type": 2, "name": "a", "size": 1, "extension": ".jpeg"}
        resp = api.post(
            "/api/xhs/qianfan/picture-space/delete",
            json={"platform_account_id": account_id, "items": [item]},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "MATERIAL_CONFIRM_REQUIRED"

    def test_delete_rejects_wrong_token(self, qianfan_client):
        """token 不匹配（多删 / 换账号）⇒ 400 `MATERIAL_CONFIRM_MISMATCH`。"""
        api, account_id = qianfan_client
        item = {"id": "x1", "type": 2, "name": "a", "size": 1, "extension": ".jpeg"}
        resp = api.post(
            "/api/xhs/qianfan/picture-space/delete",
            json={
                "platform_account_id": account_id,
                "items": [item],
                "confirmed": True,
                "confirm_token": "deadbeef",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "MATERIAL_CONFIRM_MISMATCH"

    def test_preview_token_matches_delete(self, qianfan_client):
        """preview 拿到的 token 必须能通过 delete 的校验（同一份指纹实现）。"""
        api, account_id = qianfan_client
        item = {"id": "x1", "type": 2, "name": "a", "size": 1, "extension": ".jpeg"}
        prev = api.post(
            "/api/xhs/qianfan/picture-space/preview",
            json={"platform_account_id": account_id, "items": [item]},
        )
        assert prev.status_code == 200
        body = prev.json()
        assert body["count"] == 1 and body["confirm_token"]

    def test_move_rejects_empty_selection(self, qianfan_client):
        """空选择 ⇒ 400（平台侧也是「文件信息不能为空」，提前拦更省一次请求）。"""
        api, account_id = qianfan_client
        resp = api.post(
            "/api/xhs/qianfan/picture-space/move",
            json={"platform_account_id": account_id, "items": []},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "MATERIAL_BAD_SELECTION"

    def test_folder_name_validated(self, qianfan_client):
        """文件夹名空 ⇒ 400。"""
        api, account_id = qianfan_client
        resp = api.post(
            "/api/xhs/qianfan/picture-space/folders",
            json={"platform_account_id": account_id, "name": "  "},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "MATERIAL_BAD_NAME"

    def test_permanent_delete_requires_own_token_not_delete_token(self, qianfan_client):
        """⚠️ 彻底删除与删除**token 不可跨用**（action 不同 ⇒ 指纹不同 ⇒ 400）。

        防的是「用进回收站的确认去彻底删」——那是不可还原的操作，必须单独确认。
        """
        api, account_id = qianfan_client
        item = {"id": "x1", "type": 2, "name": "a", "size": 1, "extension": ".jpeg"}
        delete_prev = api.post(
            "/api/xhs/qianfan/picture-space/preview",
            json={"platform_account_id": account_id, "items": [item], "action": "delete"},
        ).json()
        resp = api.post(
            "/api/xhs/qianfan/picture-space/permanent-delete",
            json={
                "platform_account_id": account_id,
                "items": [item],
                "confirmed": True,
                "confirm_token": delete_prev["confirm_token"],
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "MATERIAL_CONFIRM_MISMATCH"

    def test_permanent_delete_preview_token_matches(self, qianfan_client):
        """彻底删除 preview（action=permanent-delete）的 token 能过校验。"""
        api, account_id = qianfan_client
        item = {"id": "x1", "type": 2, "name": "a", "size": 1, "extension": ".jpeg"}
        prev = api.post(
            "/api/xhs/qianfan/picture-space/preview",
            json={"platform_account_id": account_id, "items": [item], "action": "permanent-delete"},
        ).json()
        assert prev["action"] == "permanent-delete"

    def test_restore_needs_no_confirm(self, qianfan_client):
        """还原是**低风险可逆**操作 ⇒ 不需要两层确认；空选择才 400。"""
        api, account_id = qianfan_client
        resp = api.post(
            "/api/xhs/qianfan/picture-space/restore",
            json={"platform_account_id": account_id, "items": []},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "MATERIAL_BAD_SELECTION"


class TestWhitelistSingleSource:
    def test_order_fields_include_update_time(self):
        """⚠️ 排序字段白名单必须含 `updateTime`（回收站按删除时间排序用）。

        2026-10-07 真 bug：`api/platforms/xhs/qianfan.py` 曾**另存一份**白名单
        （只有 name/createTime/size），漏 `updateTime` ⇒ 回收站视图一点就 400
        「加载失败」。现改为从 service 层 import（单一来源）。
        """
        from backend.app.api.platforms.xhs import qianfan
        from backend.app.services import picture_space_service as ps

        assert "updateTime" in qianfan.MATERIAL_ORDER_FIELDS
        # 必须与 service 层**逐字相同**（单一来源，不得各自维护）。
        assert tuple(qianfan.MATERIAL_ORDER_FIELDS) == tuple(ps.MATERIAL_ORDER_FIELDS)


class TestWriteItemShape:
    def test_move_item_sets_father_directory_and_not_status(self):
        """⚠️ 移动条目必须带 `fatherDirectoryId`、**不得带 `status`**（真机 JS bundle 取证）。

        历史：初版误以为移动 = 置 `status:4`（那是 `RECYCLE_BIN`）⇒ 等于把文件
        **移进回收站**（数据丢失级 bug）。本用例把平台真实语义钉死。
        """
        got = ps.build_move_item({"id": "x", "type": 2, "name": "n"}, "99")
        assert got["fatherDirectoryId"] == "99"
        assert "status" not in got, "移动不得带 status（带了会被平台当删除/还原）"
        assert got["id"] == "x"
        # 根目录缺省。
        assert ps.build_move_item({"id": "y"}, "0")["fatherDirectoryId"] == "0"

    def test_delete_item_uses_recycle_bin_status(self):
        """删除条目 = 完整原条目 + `status:4`（`RECYCLE_BIN`，进回收站）。"""
        got = ps.build_delete_item({"id": "x", "type": 2, "name": "n"})
        assert got["status"] == ps.MATERIAL_STATUS_RECYCLE_BIN == 4
        assert got["id"] == "x"

    def test_restore_item_uses_usable_status(self):
        """还原条目 = 完整原条目 + `status:1`（`USABLE`，从回收站还原）。"""
        got = ps.build_restore_item({"id": "x", "type": 2, "name": "n"})
        assert got["status"] == ps.MATERIAL_STATUS_USABLE == 1

    def test_permadelete_elements_are_id_type_only(self):
        """彻底删除元素 = **只 `{id,type}`**（真机 JS bundle 逐字）。"""
        got = ps.build_permadelete_elements([
            {"id": "a", "type": 2, "name": "n", "additionalData": {"x": 1}},
            {"id": "b", "type": 1},
        ])
        assert got == [{"id": "a", "type": 2}, {"id": "b", "type": 1}]

    def test_download_links_shape(self):
        """下载 `links[]`：文件 {type:2,link,name,extension} / 文件夹 {type:1,directoryId}。"""
        got = ps.build_download_links([
            {"id": "f1", "type": 2, "name": "n", "link": "https://x/a", "extension": ".jpeg"},
            {"id": "d1", "type": 1, "name": "dir"},
        ])
        assert got[0] == {"type": 2, "link": "https://x/a", "name": "n", "extension": ".jpeg"}
        assert got[1] == {"type": 1, "directoryId": "d1"}

    def test_add_item_shape(self):
        """新建文件夹条目形状（真机 JS bundle 逐字）。"""
        got = ps.build_add_item("SELLER", "新文件夹")
        assert got["type"] == 1 and got["sellerId"] == "SELLER"
        assert got["size"] == 0
        assert got["additionalData"]["isLeaf"] == "true"

    def test_extract_items_reverse_maps_frontend_contract(self):
        """前端契约条目 → 平台形状（写操作回传要平台字段名）。"""
        front = [{
            "id": "x1", "type": 2, "name": "base", "display_name": "base.jpeg",
            "extension": ".jpeg", "size": 100, "width": 800, "height": 600,
            "link": "https://q/img", "product_count": 2, "referenced": True, "create_time": 1,
        }]
        got = ps.extract_items(front)
        assert got[0]["id"] == "x1"
        assert got[0]["additionalData"]["extension"] == ".jpeg"
        assert got[0]["additionalData"]["productCount"] == "2"
        assert got[0]["additionalData"]["width"] == "800"

    def test_upload_item_shape(self):
        """上传登记条目（`type:2` + `additionalData` 装扩展名/尺寸/path）。

        ⚠️ 真机 JS 抓包（2026-10-07）：文件与文件夹共用 `batch_add`，靠 `type` 区分（2=文件）；
        `path` **就是预分配 fileId 本身**（`arkgoods/<id>` 或 `spectrum/<id>`），
        **绝不加 `material_space/` 前缀**；`name` 是**无扩展名基名**。
        """
        got = ps.build_upload_item(
            "SELLER", file_id="arkgoods/abc", file_name="我的图.jpeg", size=2048,
            width=800, height=600,
        )
        assert got["type"] == 2 and got["sellerId"] == "SELLER"
        assert got["name"] == "我的图", "name 必须是无扩展名基名"
        assert got["size"] == 2048
        ad = got["additionalData"]
        assert ad["extension"] == ".jpeg"
        assert ad["path"] == "arkgoods/abc", "path 就是 fileId，不加 material_space/ 前缀"
        assert ad["width"] == "800" and ad["height"] == "600"
        # ⚠️ `uploaderInfoModel` 必须是**对象**（含 PUT 给回的带签名 `url`），
        # 不是字符串 —— 字符串会让条目在、取图 404（2026-10-07 真机 bug）。
        uim = ad["uploaderInfoModel"]
        assert isinstance(uim, dict), "uploaderInfoModel 必须是对象"
        assert uim["bizName"] == "ark" and uim["scene"] == "goods_image"
        assert uim["fileId"] == "arkgoods/abc"
        assert uim["isSecret"] is True and uim["cloudType"] == 4
        assert uim["url"] == ""  # 未传 preview_url 时为空串（不造）
        # 传了 preview_url ⇒ 原样进 url。
        got_p = ps.build_upload_item(
            "S", file_id="arkgoods/x", file_name="a.jpg", size=1,
            preview_url="https://ros-preview.xhscdn.com/arkgoods/x?sign=abc&t=1",
        )
        assert got_p["additionalData"]["uploaderInfoModel"]["url"].endswith("sign=abc&t=1")
        # 任何 fileId 都原样落 path（不再拼前缀）。
        again = ps.build_upload_item("S", file_id="spectrum/x", file_name="a.png", size=1)
        assert again["additionalData"]["path"] == "spectrum/x"

    def test_upload_item_omits_blank_dimensions(self):
        """⚠️ 尺寸缺值 ⇒ **整个键不发**，绝不发空串（2026-10-07 真机 bug「For input string: ""」）。

        平台是 Java 后端，`additionalData.width/height` 会被 `Integer.parseInt` 解析；
        传 `""` ⇒ `NumberFormatException（For input string: ""）` ⇒ **整个上传失败**。
        缺值口径：不造假、也不送空串 —— 拿不到就省略键。
        """
        got = ps.build_upload_item("S", file_id="spectrum/x", file_name="a.png", size=1)
        ad = got["additionalData"]
        assert "width" not in ad and "height" not in ad, "缺尺寸不得发空串，应省略键"
        # 明确传 None 也一样省略。
        got2 = ps.build_upload_item(
            "S", file_id="spectrum/x", file_name="a.png", size=1, width=None, height=None
        )
        assert "width" not in got2["additionalData"]
        # 有尺寸时必须是可解析的整数字符串。
        got3 = ps.build_upload_item(
            "S", file_id="spectrum/x", file_name="a.png", size=1, width=12, height=34
        )
        assert got3["additionalData"]["width"] == "12"
        assert got3["additionalData"]["height"] == "34"

    def test_upload_uses_picture_space_scene(self, qianfan_client, monkeypatch):
        """⚠️ 图片空间上传**必须** `biz_name=ark&scene=goods_image`（2026-10-07 真机抓包）。

        用发布链路的 `spectrum/image` ⇒ 文件落 `spectrum/` 桶而图片空间按 `arkgoods/` 取
        ⇒ 登记成功但**裂图**。本用例钉死端点传的 scene + 按 qos 取凭证。
        """
        import base64 as _b64

        api, account_id = qianfan_client
        calls = {}

        class _UploadStub:
            def upload_web_permit(self, file_count=1, **kwargs):
                calls["permit"] = {"file_count": file_count, **kwargs}
                return True, "", {"data": {"uploadTempPermits": [
                    {"qos": 1, "uploadAddr": "ros-upload.xiaohongshu.com",
                     "token": "t-low", "fileIds": ["spectrum/low"]},
                    {"qos": 2, "uploadAddr": "ros-upload-ali.xiaohongshu.com",
                     "token": "t-high", "fileIds": ["arkgoods/high"]},
                ]}}

            def upload_file(self, **kwargs):
                calls["put_file_id"] = kwargs.get("file_id")
                return True, "", None

            def register_material_files(self, seller_id, items):
                calls["registered"] = items[0]
                return True, "", None

        from backend.app.api.platforms.xhs.qianfan import picture_space as _ps
        monkeypatch.setattr(
            _ps, "_ark_adapter_for",
            lambda db, acc, factory, _a=_UploadStub(): _a, raising=True,
        )
        #: ⚠️ 上传端点读的是**子模块**的 `_ark_adapter_for`（拆包后）——
        #: 锚点必须打在这里；打在包 `qianfan` 上会静默失效并真的去调平台。
        assert _ps._ark_adapter_for(None, None, None) is not None, "monkeypatch 锚点打空了"

        res = api.post(
            "/api/xhs/qianfan/picture-space/upload",
            json={"platform_account_id": account_id, "file_name": "a.jpg",
                  "content_base64": _b64.b64encode(b"JPEGDATA").decode()},
        )
        assert res.status_code == 200, res.text
        assert calls["permit"].get("biz_name") == "ark", "必须 biz_name=ark"
        assert calls["permit"].get("scene") == "goods_image", "必须 scene=goods_image"
        # 取 qos 最高的那个（2 > 1）⇒ fileId 应是 arkgoods/high。
        assert calls["put_file_id"] == "arkgoods/high", "须按 qos 取凭证（不能盲取 [0]）"
        assert calls["registered"]["additionalData"]["path"] == "arkgoods/high", "path 就是 fileId"

    def test_upload_rejects_bad_base64_and_empty(self, qianfan_client):
        """上传端点：非法 base64 / 空文件必须在**调平台之前**就 400。"""
        client, account_id = qianfan_client
        bad = client.post(
            "/api/xhs/qianfan/picture-space/upload",
            json={"platform_account_id": account_id, "file_name": "a.png", "content_base64": "!!!not-base64!!!"},
        )
        assert bad.status_code == 400
        assert bad.json()["detail"]["code"] == "MATERIAL_BAD_CONTENT"
        empty = client.post(
            "/api/xhs/qianfan/picture-space/upload",
            json={"platform_account_id": account_id, "file_name": "a.png", "content_base64": ""},
        )
        # 空串被 pydantic min_length=1 拦（422）；空字节被端点拦（400）。二者皆可，但必须拦。
        assert empty.status_code in (400, 422)

    def test_validate_batch_limits(self):
        """批量上限 + 必须带 id。"""
        with pytest.raises(ValueError, match="MATERIAL_EMPTY_SELECTION"):
            ps.validate_batch([])
        with pytest.raises(ValueError, match="MATERIAL_ITEM_NO_ID"):
            ps.validate_batch([{"name": "no-id"}])
        with pytest.raises(ValueError, match="MATERIAL_BATCH_TOO_LARGE"):
            ps.validate_batch([{"id": str(i)} for i in range(ps.MATERIAL_BATCH_LIMIT + 1)])
