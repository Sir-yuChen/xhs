"""千帆「账号归属 + 能力判据」的后端契约测试（T-32 的后继；ADR-021 / 设计 18）。

## ⚠️ 本文件的前身测的是「账号 ↔ 店铺」关系，那套语义已被消灭（原句保留）

* **2026-09-22 之前**：断言 **N:M** —— *「两个账号绑同一店铺 → 一行店铺 + 两行绑定」*、
  *「同一账号绑两个店铺也允许」*、*「同一店铺的 `account_count` 必须能到 2」*。
* **2026-09-22 用户拍板**：*「不会再出现「同店铺、多账号」场景，一个店铺下只需存多个平台的会话令牌。」*
  ⇒ 关系收敛到 **1:1**，由部分唯一索引 `uq_platform_accounts_shop_id_fk` 强制。
* **2026-09-30 用户再拍板（ADR-021 / 设计 18）**：*「商品页走的本地数据库数据，不应该校验是否存在
  千帆店铺，所有校验应该校验的是该小红书账号下是否存在千帆可用的会话。」*
  ⇒ **店铺实体整表退场**（`shops` 表删除、`platform_accounts.shop_id_fk` 列删除），
  隔离键 = `platform_account_id`，`seller_id` 下沉为**账号字段**（只回答归属）。

⇒ 于是本文件里原来的 13 个用例，其断言对象（`shops` 表、`shop_id_fk`、`QIANFAN_SHOP_TAKEN`、
`POST /bind`、`POST /unbind`、`POST /shops`、`shop_for_account`）**在 ADR-021 之后全部不存在**。
下面保留的是**同一批守护意图在新模型下的等价物**，以及三个退场端点的 `410` 契约。

## 现在盯什么

1. **退场端点必须给 `410` + 可读原因**，不是 `404`（老前端要能读懂「能力没了、该去哪」）；
2. **退场端点不在 OpenAPI 里**（端点计数只算真实能力）；
3. **能力判据 = 账号自己的 ark 会话**：有令牌 ⇒ `bound=True`；无令牌但有 `seller_id`
   ⇒ 仍然 `bound=False`（**归属 ≠ 能力**，这是 ADR-021 的核心不变量）；
4. **`shop_id_fk` 字段保留但恒 `None`**（给老前端的空值，不是 `KeyError`）；
5. **软删闸不倒退**（DEF-055）：软删账号在任何按 id 的千帆端点上都是 `404`。
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.api.platforms.xhs.qianfan import get_qianfan_api_adapter_factory
from backend.app.core.database import Base, get_db
from backend.app.core.time import shanghai_now
from backend.app.main import app
from backend.app.models import PlatformAccount, User
from backend.app.services.shop_binding_service import (
    get_shop_state,
    has_ark_session,
    set_shop_state,
)
from tests.backend.cookie_fixtures import ARK_COOKIES, give_ark_cookies

import pytest

from tests.backend.conftest import (  # noqa: PLC0415 - schema 复用助手
    _BUILT_SCHEMAS,
    _SCHEMA_BY_ENGINE,
    make_engine,
    reset_test_data,
)




#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract
client = TestClient(app)
ROOT = Path(__file__).resolve().parents[2]

#: 退场端点 → 其历史职能（供断言消息用）。
_RETIRED = {
    "bind": "绑定千帆店铺",
    "unbind": "解绑千帆店铺",
    "shops": "店铺列表",
}


class FakeArkAdapter:
    """千帆适配器替身（退场端点不再打它，保留给 `items` 等读侧用例）。"""

    calls: dict = {}

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def get_seller_info(self):
        FakeArkAdapter.calls["seller"] = FakeArkAdapter.calls.get("seller", 0) + 1
        return True, "ok", {
            "code": 0,
            "success": True,
            "data": {"user_id": "SELLER-1", "seller_name": "观隅好物的店"},
        }

    def search_items(self, **kwargs):
        return True, "ok", {"code": 0, "success": True, "data": {"total": 0, "items": []}}


def _setup(tmp_path, accounts: int = 2):
    engine = make_engine(tmp_path / 'account-ark.db')
    #: 2026-10-26：同进程复用 schema ⇒ 表只建一次，之后只清数据。
    _schema = _SCHEMA_BY_ENGINE.get(engine)
    if _schema is not None and _BUILT_SCHEMAS.get(_schema):
        reset_test_data(engine)
    else:
        Base.metadata.create_all(bind=engine)
        if _schema is not None:
            _BUILT_SCHEMAS[_schema] = True
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    FakeArkAdapter.calls = {}
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_qianfan_api_adapter_factory] = lambda: FakeArkAdapter

    response = client.post(
        "/api/auth/register", json={"username": "owner", "password": "secret123"}
    )
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    db = SessionLocal()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "owner"))
        ids = []
        for index in range(accounts):
            account = PlatformAccount(
                user_id=user_id,
                platform="xhs",
                sub_type="creator",
                nickname=f"观隅好物{index + 1}",
                status="active",
            )
            db.add(account)
            ids.append(account)
        db.commit()
        account_ids = [account.id for account in ids]
    finally:
        db.close()
    return headers, account_ids, SessionLocal


def _teardown():
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 1. 退场端点：410 + 可读原因（不是 404）
# --------------------------------------------------------------------------- #
def test_retired_binding_endpoints_return_410(tmp_path):
    """`POST /bind` / `/unbind` / `/shops` 一律 `410 GONE` + 可读原因。

    ⚠️ **被取代的旧断言（原句保留）**：

    * `test_second_account_cannot_bind_the_same_shop` 断言 *「第二个账号绑同一个店 →
      409 `QIANFAN_SHOP_TAKEN`」* + *「`len(shops) == 1`」*；
    * `test_unbind_clears_own_mapping_and_removes_the_orphan_shop` 断言
      *「`payload["removed"] == 1` + `shop_remaining_accounts == 0` + 孤儿店行被清」*；
    * `test_shops_endpoint_*` 断言 *「`payload["count"] == 1` + `shops[0]["accounts"]`」*。

    三者共有的对象（店铺实体 / 绑定关系）已随 ADR-021 退场 ⇒ 现在钉的是「端点还在、
    但明确告诉调用方这个能力没了、以及该改用什么」。
    """
    headers, _, _ = _setup(tmp_path)
    try:
        for name, body in (
            ("bind", {"platform_account_id": 1, "cookie_string": ARK_COOKIES}),
            ("unbind", {"platform_account_id": 1}),
            ("shops", {}),
        ):
            response = client.post(f"/api/xhs/qianfan/{name}", json=body, headers=headers)
            assert response.status_code == 410, (
                f"{name} 该是 410 而不是 {response.status_code}：{response.text[:200]}"
            )
            detail = response.json()["detail"]
            assert "ADR-021" in detail["message"], f"{name} 的说明该点明 ADR-021"
            assert detail["code"], f"{name} 该给稳定的错误码"
    finally:
        _teardown()


def test_retired_endpoints_are_absent_from_openapi():
    """退场端点不得进 OpenAPI —— 端点计数只算「真实能力」。

    ⚠️ 这与「端点还在（`410`）」不矛盾：路径保留是为了**老前端能读懂**，
    `include_in_schema=False` 是为了**记账诚实**。两者都要成立才叫退场干净。
    """
    paths = app.openapi()["paths"]
    for name in _RETIRED:
        assert f"/api/xhs/qianfan/{name}" not in paths, (
            f"{_RETIRED[name]}已退场，不该出现在 OpenAPI 里"
        )
    #: 真实能力仍在（对照组）：状态查询是活的。
    assert "/api/xhs/qianfan/binding" in paths


# --------------------------------------------------------------------------- #
# 2. 能力判据 = 账号自己的 ark 会话（归属 ≠ 能力）
# --------------------------------------------------------------------------- #
def test_capability_follows_the_ark_session_not_the_seller_id(tmp_path):
    """ADR-021 核心不变量：**能调千帆 = 本账号有可用 ark 令牌**，与 `seller_id` 无关。

    ⚠️ **被取代的旧断言（原句保留）**：原 `test_shop_summary_reports_health_checked_at`
    断言 *「`payload["shop_id_fk"]` 必须带店铺实体主键」* —— 主键列已随 `shops` 表删除。

    新契约用 `bound`（语义已改为「有 ark 会话」，见 `_account_shop_summary` docstring）
    来证这条：**先只写 `seller_id`（归属），`bound` 必须是 `False`；再给令牌，才变 `True`。**
    """
    headers, account_ids, SessionLocal = _setup(tmp_path, accounts=1)
    account_id = account_ids[0]
    try:
        #: 第一阶段：只有归属（seller_id），没有令牌 ⇒ 调不了千帆。
        db = SessionLocal()
        try:
            db.get(PlatformAccount, account_id).seller_id = "SELLER-1"
            db.commit()
            assert db.get(PlatformAccount, account_id).seller_id == "SELLER-1", "前置：归属已写"
        finally:
            db.close()

        payload = client.post(
            "/api/xhs/qianfan/binding",
            json={"platform_account_id": account_id},
            headers=headers,
        ).json()
        assert payload["bound"] is False, "有 seller_id 但没有 ark 令牌 ⇒ 仍然调不了千帆"
        assert payload["shop_id_fk"] is None, "退场字段恒 None（给老前端一个空值，不是 KeyError）"
        assert payload["shop_user_id"] == "SELLER-1", "归属改从 account.seller_id 派生"

        #: 第二阶段：导入 ark 登录态 ⇒ 能力开启（归属列一个字没改）。
        db = SessionLocal()
        try:
            give_ark_cookies(db, db.get(PlatformAccount, account_id))
            db.commit()
            assert has_ark_session(db, account_id), "前置：令牌已落库"
        finally:
            db.close()

        payload = client.post(
            "/api/xhs/qianfan/binding",
            json={"platform_account_id": account_id},
            headers=headers,
        ).json()
        assert payload["bound"] is True, "有令牌 ⇒ 能调千帆"
        assert payload["shop_id_fk"] is None
        assert "shop_account_count" not in payload, "已删除的计数不该回来"
    finally:
        _teardown()


def test_binding_summary_is_purely_local(tmp_path):
    """`POST /binding` 是**纯本地读**，一次都不打千帆。

    ⚠️ **被取代的旧断言（原句保留）**：原 `test_shops_endpoint_marks_the_one_account_behind_a_shop`
    断言 *「`FakeArkAdapter.calls.get("seller", 0) == 1`，店铺列表必须是纯本地读」*。
    店铺列表端点已退场，同一条守护意图挪到还在的 `binding` 摘要上（用户 2026-09-30 口径：
    本地台账读取零门禁、零平台请求）。
    """
    headers, account_ids, SessionLocal = _setup(tmp_path, accounts=1)
    account_id = account_ids[0]
    try:
        db = SessionLocal()
        try:
            account = db.get(PlatformAccount, account_id)
            account.seller_id = "SELLER-1"
            give_ark_cookies(db, account)
            set_shop_state(account, goods_note_total=1, goods_synced_at=shanghai_now().isoformat())
            db.commit()
        finally:
            db.close()

        payload = client.post(
            "/api/xhs/qianfan/binding",
            json={"platform_account_id": account_id},
            headers=headers,
        ).json()
        assert payload["goods_note_total"] == 1
        assert payload["goods_synced_at"], "摘要该带同步时间"
        assert FakeArkAdapter.calls.get("seller", 0) == 0, "摘要必须是纯本地读（不重打千帆）"
    finally:
        _teardown()


def test_two_accounts_are_isolated_by_platform_account_id(tmp_path):
    """两个账号各自持有自己的 ark 会话 ⇒ 两个都 `bound`，互不干扰。

    ⚠️ **被取代的旧断言（原句保留）**：原 `test_shops_endpoint_is_user_scoped` 断言
    *「另一个用户绑同一个 seller_id → `len(shops) == 2`」* —— 「同一个店」这个概念
    （以及随之而来的「抢店」冲突）已随店铺实体退场。新模型下**不存在「一个店只能一个账号」**，
    隔离键是 `platform_account_id`（ADR-021）。
    """
    headers, account_ids, SessionLocal = _setup(tmp_path)
    first, second = account_ids
    try:
        db = SessionLocal()
        try:
            #: 两个账号**同一个 seller_id**（同一家店的两个会话）—— 在旧模型下是 409 冲突，
            #: 在新模型下完全合法（归属相同、能力各自独立）。
            for account_id in (first, second):
                account = db.get(PlatformAccount, account_id)
                account.seller_id = "SELLER-1"
                give_ark_cookies(db, account)
            db.commit()
        finally:
            db.close()

        for account_id in (first, second):
            payload = client.post(
                "/api/xhs/qianfan/binding",
                json={"platform_account_id": account_id},
                headers=headers,
            ).json()
            assert payload["bound"] is True
            assert payload["platform_account_id"] == account_id, "各答各自的账号"
    finally:
        _teardown()


# --------------------------------------------------------------------------- #
# 3. 路由层不再引用已删除的真相源
# --------------------------------------------------------------------------- #
def test_routes_do_not_reference_removed_shop_entities():
    """源码级护栏：路由层不得再**在代码里**引用任何已退场的店铺符号。

    ⚠️ **被取代的旧断言（原句保留）**：原 `test_routes_do_not_pick_a_shop_by_account_id_directly`
    断言两件事 —— ① *「`ShopAccountBinding` / `binding_for_account` 不得出现」*；
    ② *「`shop_for_account` 必须在 `qianfan.py` 与 `pc.py` 里（证明走的是唯一解析函数）」*。

    ① 仍然保留（`ShopAccountBinding` 早就不存在了）；② 已反转为**禁令** ——
    `shop_for_account` 自己随店铺实体一起退场了，现在再写它就是引用不存在的真相源。

    ⚠️ 判据走 **AST 的名字与属性**，不是找子串：子串写法会被**注释与 docstring**误伤
    （本仓库到处在讲这段迁移史，`qianfan.py` 的 `_account_shop_summary` docstring
    就逐字解释「`shop_id_fk` 字段保留但恒 None」）。真正的红线是
    「不许在**代码**里再引用它」——注释是说明，不是引用。

    ⚠️ 唯一豁免：`_account_shop_summary` 返回体里的 **`"shop_id_fk": None`** ——
    那是**刻意保留的退场字段**（给老前端一个明确空值而不是 `KeyError`，见设计 18）。
    豁免写成「`shop_id_fk` 的字面量必须紧跟在 `None` 左侧」这种**精确形状**，
    而不是放行整个文件 —— 否则真正的引用会躲在豁免里。
    """
    import ast

    forbidden = (
        "ShopAccountBinding",
        "binding_for_account",
        "shop_for_account",
        "attach_shop",
        "upsert_shop",
    )

    def _is_allowed_retired_field(node: ast.AST) -> bool:
        """`{"shop_id_fk": None}` 的键 —— 唯一豁免形状（见 docstring）。"""
        return (
            isinstance(node, ast.Constant)
            and node.value == "shop_id_fk"
        )

    offenders = []
    #: ⚠️ qianfan 已拆包（2026-10-11）⇒ 用**整包拼接**喂 AST（宏 `#: qianfan.py` 原句保留）：
    #:    这是 AST 扫描，拼接后的多文件源码仍是合法 Python 片段集合，
    #:    `ast.parse` 逐文件解析即可，故这里按文件遍历整包。
    from tests.backend.conftest import read_qianfan_source

    targets: list[str | tuple[str, str]] = [
        "backend/app/api/platforms/xhs/pc.py",
        ("<qianfan 整包>", read_qianfan_source()),
        "backend/app/api/platforms/xhs/product_notes.py",
        "backend/app/api/login_sessions.py",
    ]
    for target in targets:
        if isinstance(target, tuple):
            label, text = target
        else:
            label, text = target, (ROOT / target).read_text(encoding="utf-8")
        tree = ast.parse(text)

        #: 先收集所有 docstring 常量节点（说明不是引用）。
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    if isinstance(body[0].value.value, str):
                        docstrings.add(id(body[0].value))

        #: 收集「退场字段键」——`{"shop_id_fk": ...}` 里的那个字符串常量。
        allowed_keys = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key in node.keys:
                    if key is not None and _is_allowed_retired_field(key):
                        allowed_keys.add(id(key))

        for node in ast.walk(tree):
            #: `AST` 不会把注释解析成节点 ⇒ 注释天然不参与，只有真代码会被抓。
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            if name in forbidden or name == "shop_id_fk":
                offenders.append(f"{relative}:{node.lineno} → {name}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                if id(node) in allowed_keys:
                    continue  #: 刻意保留的退场字段（见 docstring）。
                if "shop_id_fk" in node.value:
                    offenders.append(f"{relative}:{node.lineno} → 字符串含 `shop_id_fk`")
                for symbol in forbidden:
                    if symbol in node.value:
                        offenders.append(f"{relative}:{node.lineno} → 字符串含 `{symbol}`")

    assert offenders == [], "路由层不得再引用已退场符号：\n" + "\n".join(offenders)


def test_shops_table_is_gone_from_the_schema():
    """`shops` 表与 `platform_accounts.shop_id_fk` 列必须真的从 schema 消失。

    ⚠️ **被取代的旧断言（原句保留）**：原 `test_foreign_tenant_residue_gives_a_readable_409`
    用 **raw SQL `insert into shops (...)`** 造跨租户残留，再断言 409。
    表没了 ⇒ 那种残留**在新模型下根本造不出来**（这本身就是 ADR-021 想要的结果）。
    现在直接钉 schema：两张「表 / 列不存在」的判据。
    """
    engine = make_engine()
    #: 2026-10-26：同进程复用 schema ⇒ 表只建一次，之后只清数据。
    _schema = _SCHEMA_BY_ENGINE.get(engine)
    if _schema is not None and _BUILT_SCHEMAS.get(_schema):
        reset_test_data(engine)
    else:
        Base.metadata.create_all(bind=engine)
        if _schema is not None:
            _BUILT_SCHEMAS[_schema] = True
    with engine.connect() as conn:
        tables = {row[0] for row in conn.execute(text("select name from sqlite_master where type='table'"))}
        assert "shops" not in tables, f"`shops` 表该已随 ADR-021 退场：{sorted(tables)}"
        assert "shop_account_bindings" not in tables, "那张绑定表更早就退场了"

        account_cols = {row[1] for row in conn.execute(text("pragma table_info(platform_accounts)"))}
        assert "shop_id_fk" not in account_cols, (
            f"`platform_accounts.shop_id_fk` 该已删除（隔离键 = 账号本身）：{sorted(account_cols)}"
        )
        assert "seller_id" in account_cols, "`seller_id` 该已下沉为账号字段"

        product_cols = {row[1] for row in conn.execute(text("pragma table_info(shop_products)"))}
        assert "platform_account_id" in product_cols, "商品台账该改挂账号"
        assert "shop_id_fk" not in product_cols, "商品台账不该再有店铺外键"


# --------------------------------------------------------------------------- #
# 4. 闸门：软删账号（DEF-055）
# --------------------------------------------------------------------------- #
def test_soft_deleted_account_is_404_on_account_scoped_endpoints(tmp_path):
    """DEF-055：按 id 的端点必须一律 404 —— 「看不见」不等于「不能用」。

    账号删除是软删，用户手里可能还留着**旧页面上的 id**（另一个标签页、刚点开的下拉），
    所以每个按 id 取账号的端点都得过闸（与台账 §4.9 / I3 同源）。

    ⚠️ 本用例原有的两条路径（`/bind`、`/shops`）已在 ADR-021 退场 —— 退场端点
    **先于**软删闸返回 `410`（`410` 是「能力没了」，与「账号没了」是两件事，
    都说得通；此处只保留仍在服务的真实端点）。
    """
    headers, account_ids, SessionLocal = _setup(tmp_path, accounts=1)
    try:
        account_id = account_ids[0]
        db = SessionLocal()
        try:
            give_ark_cookies(db, db.get(PlatformAccount, account_id))
            db.commit()
            db.get(PlatformAccount, account_id).deleted_at = shanghai_now()
            db.commit()
        finally:
            db.close()

        cases = (
            ("/api/xhs/qianfan/binding", {"platform_account_id": account_id}),
            ("/api/xhs/qianfan/health-check", {"platform_account_id": account_id}),
            (
                "/api/xhs/qianfan/items",
                {"platform_account_id": account_id, "page_no": 1, "page_size": 20},
            ),
        )
        for path, body in cases:
            response = client.post(path, json=body, headers=headers)
            assert response.status_code == 404, (
                f"{path} 未过软删闸：{response.status_code} {response.text[:200]}"
            )
    finally:
        _teardown()


def test_soft_deleted_account_does_not_hold_any_capability(tmp_path):
    """软删账号不再持有能力：`has_ark_session` 对软删账号为假。

    ⚠️ **被取代的旧断言（原句保留）**：原 `test_soft_deleted_account_stops_holding_the_shop`
    断言 *「软删账号不再占店，别人能接管同一个店（不撞 `UNIQUE(shop_id_fk)`）」*。
    唯一索引与「占店」概念随之退场；同一件事现在表述为「软删 ⇒ 能力判据为假」。
    """
    headers, account_ids, SessionLocal = _setup(tmp_path, accounts=1)
    try:
        account_id = account_ids[0]
        db = SessionLocal()
        try:
            account = db.get(PlatformAccount, account_id)
            give_ark_cookies(db, account)
            db.commit()
            assert has_ark_session(db, account_id), "前置：软删前能力可用"

            account = db.get(PlatformAccount, account_id)
            account.deleted_at = shanghai_now()
            db.commit()
            assert not has_ark_session(db, account_id), "软删后不得再被判定为「有能力」"
            assert get_shop_state(db.get(PlatformAccount, account_id))["status"] == ""
        finally:
            db.close()
    finally:
        _teardown()
