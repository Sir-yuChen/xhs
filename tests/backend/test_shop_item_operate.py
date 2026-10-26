"""商品批量操作（上架 / 下架 / 删除）与「同步指定商品」的后端契约测试。

对应 2026-10-05 需求 6 / 7 / 8，设计 20 §3、§11。

## 这批用例盯的三件事

1. **门禁判据只有一份**（`shop_item_operate_gates`）——列表端点下发的
   `allowed_actions[]` 与执行循环里用的判据，必须是**同一函数**的产物。
   分开实现就会分叉：前端亮着按钮、后端静默跳过（用户点了个寂寞）。
2. **不支持的「只记录不执行」**（需求 7 原话）——状态不对的商品**一条平台请求都不发**，
   但必须在结果里留一行 `skipped` + 中文原因。测法是**数平台调用次数**：
   若实现「先发了再说」，`calls` 会 > 0，本用例必红。
3. **写操作不被「乐观成功」掩盖**——平台逐条失败原因
   （`failData[].reasons[].message`）必须原样出现在对应条目上，
   不许退化成「整批一个笼统原因」。

⚠️ 真机写接口已单独验证（2026-10-05：`batch_update_availability_v3` 真机商品
从已下架→上架，`code=200`）。这里测的是**我们自己的判据与记账**，
不是平台行为 —— 平台行为由真机冒烟覆盖。
"""
from __future__ import annotations

import inspect
import json
from typing import Callable, NamedTuple

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import PlatformAccount, Task, User
from backend.app.models.shop_product import ShopProduct
from backend.app.services import shop_goods_sync_service as sgs
from backend.app.services import shop_item_operate_gates as gates
from backend.app.services import shop_item_operate_service as operate
from backend.app.services.shop_products_service import load_product_dict_from_ledger


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration

ARK_COOKIES = "access-token-ark.xiaohongshu.com=abc123"


class AllowLimiter:
    def allow(self, account_id: int) -> bool:  # noqa: ARG002
        return True

    def wait_allow(self, account_id: int, **kwargs) -> bool:  # noqa: ARG002
        return True


class DenyLimiter:
    """额度永远没有 —— 用来钉「限流时整批记 skipped 而不是硬闯」。"""

    def allow(self, account_id: int) -> bool:  # noqa: ARG002
        return False

    def wait_allow(self, account_id: int, **kwargs) -> bool:  # noqa: ARG002
        return False


LIMITER: dict = {"impl": AllowLimiter()}

#: `start_item_operate` 真的起的那些线程的参数（拦住了不跑，只记下来）。
_SPAWNED: list = []


class _NoRunThread:
    """`threading.Thread` 的替身：**接住参数但不真跑**。

    ⚠️ 不换掉它的话，`start_item_operate` 起的 daemon 线程会与用例自己调的那次
    `run_item_operate` **并发写同一个任务行、并发用同一个 SQLite 文件** ——
    实测表现极隐蔽（平台调用记录多出一次、快照计数 0/0/0、偶发 `database is locked`）。
    任务行与派单逻辑照旧真实执行，只有「真跑」这一步挪到用例自己手里。
    """

    def __init__(self, *args, **kwargs) -> None:
        _SPAWNED.append(kwargs)

    def start(self) -> None:
        return None


class FakeOperateApi:
    """千帆**写**接口假实现：记录收到的每一次批量，按脚本回执。

    ⚠️ 记录 `calls` 是本文件多条用例的**核心观测面**：
    「不支持的只记录不执行」只能靠「平台一次都没被调用」证明。
    """

    calls: list = []
    #: 每次调用回什么；用完 ⇒ 回成功空回执。
    responses: list = []
    #: 构造时传入的 cookies（钉「用本账号自己的凭据」）。
    cookies_seen: list = []

    def __init__(self, cookies: str) -> None:
        type(self).cookies_seen.append(cookies)

    def batch_update_availability(self, item_ids, available: bool):
        type(self).calls.append({"kind": "availability", "ids": list(item_ids), "available": available})
        index = len(type(self).calls) - 1
        if index < len(type(self).responses):
            return type(self).responses[index]
        return True, "", {"code": 200, "data": {"taskId": 1}}

    def batch_delete_items(self, item_ids):
        type(self).calls.append({"kind": "delete", "ids": list(item_ids)})
        index = len(type(self).calls) - 1
        if index < len(type(self).responses):
            return type(self).responses[index]
        return True, "", {"code": 200, "data": {"taskId": 2}}


class FakeSearchApi:
    """千帆**读**接口假实现（专给「同步指定商品」用）。"""

    calls: list = []
    pages: list = []

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def search_items(self, **kwargs):
        type(self).calls.append(kwargs)
        index = len(type(self).calls) - 1
        if index >= len(type(self).pages):
            return True, "", {"code": 0, "data": {"items": [], "total": 0, "has_more": False}}
        items, total, has_more = type(self).pages[index]
        return True, "", {"code": 0, "data": {"items": items, "total": total, "has_more": has_more}}

    def get_skus_by_item_ids(self, item_ids):
        return True, "", {"code": 200, "data": {"skusMap": {}}}


#: 状态 → 生命周期信号（**真机口径**：平台列表响应不带 `card_type`，
#: 本地按 `status_key_from_raw` 从这两个时间戳推导，见设计 13 §30.3.2）。
#: 这是本文件播种商品的**唯一**合法方式 —— 直接塞 `card_type` 会绕过推导逻辑，
#: 测出来的「门禁能判状态」就不是真的。
#:
#: ⚠️ `buyable` **必须一起给**（2026-10-06 补）：真机 raw_json 里它就是这么写的
#: —— `status_key_from_raw` 的 docstring 记着实测口径「下架行 = `buyable=False`
#: + 无上架时间 + 有下架时间」。此前夹具漏了它，`buyable` 恒为 `False`（`bool(None)`），
#: 于是「上架后 `buyable` 没被刷新」这个**真 bug** 永远测不出来（前端「可售」列 /
#: 详情「可购买」/ 状态 tag 颜色全读它）。
_OFF_SHELF = {"on_shelf_time": None, "off_shelf_time": 1784090000000, "buyable": False}
_ON_SHELF = {"on_shelf_time": 1784084996000, "off_shelf_time": None, "buyable": True}
_FROZEN = {**_ON_SHELF, "item_freeze": True}


def _item(
    item_id: str,
    *,
    title: str = "商品",
    lifecycle: dict | None = None,
    **extra,
) -> dict:
    """一条千帆商品列表原文。

    `lifecycle` 缺省 = 售卖中；`item_freeze` 等额外键直接进 `extra`。
    """
    row = {
        "item_id": item_id,
        "item_name": title,
        "min_price": 1999,
        "max_price": 1999,
        "total_stock": 10,
        "sale_qty_info": {"acc_sale_qty": 5},
        "categories": [{"name": "家居", "level": 1}],
        "images": [{"link": f"http://img.example/{item_id}.jpg"}],
        "article_no": "1056576341942",
        "create_time": 1784082313000,
        "item_audit_time": 1784089999000,
        **(_ON_SHELF if lifecycle is None else lifecycle),
    }
    row.update(extra)
    return row


class Env(NamedTuple):
    """本文件所有用例共同的上下文（`env` 夹具的返回值）。"""

    headers: dict
    account_id: int
    user_id: int
    db_factory: Callable[[], Session]


@pytest.fixture()
def env(api_client, db_factory, monkeypatch) -> Env:
    """建库 + 注册用户 + 一个带 ark 登录态的账号 + 全部外部接缝打桩。

    ## 为什么用 conftest 夹具而不是自建 engine

    `tests/backend/test_testing_spec_guards.py` 立了硬规矩：**新测试文件不许再抄
    `create_engine` + `create_all` + `dependency_overrides` 三件套**（初版这里抄了，
    被守卫抓住 ⇒ 改为夹具）。夹具同时把「全部模块级 `SessionLocal` 打到测试库」
    这件事变成**夹具的属性**而不是「用例记得申请哪个」的约定 —— 后者迟早在某个
    新用例上漏掉，而漏掉的代价是污染真实库（2026-09-27 实测事故）。

    ## 这里额外要打的三件事（夹具不管的）

    1. `operate._api_factory` —— 写操作的**观测面**：本文件多条用例靠
       「`FakeOperateApi.calls` 为空」证明「门禁拦下的商品一条平台请求都没发」。
       不换掉工厂就没有这个观测面；
    2. `operate.threading.Thread` —— `start_item_operate` 会真起线程。
       在测试里必须拦住：否则线程与用例自己调的那次 `run_item_operate`
       **并发写同一个任务行 + 并发用同一个 SQLite 文件**（实测表现极隐蔽：
       平台调用记录多出一次、快照计数 0/0/0）。任务行 / 派单逻辑照旧真实执行，
       只有「真跑」这一步挪到用例自己手里；
    3. `sgs.get_qianfan_api_adapter_factory` —— 指定同步的读接口替身。
    """
    from tests.backend.cookie_fixtures import bind_shop_account

    _reset_fakes()
    monkeypatch.setattr(operate, "get_rate_limiter", lambda: LIMITER["impl"])
    monkeypatch.setattr(operate, "_api_factory", lambda cookies: FakeOperateApi(cookies))
    monkeypatch.setattr(operate.threading, "Thread", _NoRunThread)
    monkeypatch.setattr(sgs, "get_qianfan_api_adapter_factory", lambda: FakeSearchApi)
    #: 2026-10-11：`items/sync` 改入队后会调 `ensure_goods_sync_worker` 起真实线程；
    #: 测试只验证「入队成功」，不关心 worker 是否真跑 ⇒ 打桩为 no-op。
    monkeypatch.setattr(sgs, "ensure_goods_sync_worker", lambda: None)

    response = api_client.post(
        "/api/auth/register", json={"username": "owner", "password": "secret123"}
    )
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    db = db_factory()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "owner"))
        account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="creator", nickname="我的号"
        )
        db.add(account)
        db.flush()
        #: ADR-021：开通千帆能力 = 给账号导入 `ark` 登录态（不再有「绑店」动作）。
        bind_shop_account(db, account, cookies_text=ARK_COOKIES, shop_name="观隅好物的店")
        db.commit()
        #: 2026-10-26：`/items/operate` 现在**真的判权**（一码一动作）。
        #: 新注册用户只有页面级 view 码 ⇒ 必须显式授予本文件用到的三个按钮码，
        #: 否则每条用例都在权限层 403，测不到「门禁 / 记账」这些真正要守的东西。
        #: ⚠️ 测试库**不播菜单种子**（`role_menus` 是空的）⇒ 只能在
        #: `permission_cache.perms_of` 这一层补码（那是 `require_perm` / `has_perm`
        #: 的唯一取数口）。不要改成直接改缓存 dict —— 那条路绕过了本用例要证明的东西。
        _grant(monkeypatch, ["xhs:item:onShelf", "xhs:item:offShelf", "xhs:item:delete"])
        return Env(headers=headers, account_id=account.id, user_id=user_id, db_factory=db_factory)
    finally:
        db.close()


def _grant(monkeypatch, codes: list[str]) -> None:
    """把 `codes` 追加到「当前用户已有权限」里（只增不减，按用户无关地放行）。

    为什么打 `permission_cache.perms_of` 而不是塞 `role_menus`：
    测试库不播菜单种子，没有菜单行可挂授权；而 `perms_of` 正是
    `require_perm` / `has_perm` 的唯一取数口 —— 打这里等价于「这个用户被授了这些码」，
    且不会顺手放宽任何别的东西。
    """
    from backend.app.core import permission_cache
    from backend.app.core.perms import has_perm as _real_has_perm  # noqa: F401

    original = permission_cache.perms_of

    def patched(db, user):  # noqa: ANN001, ANN202
        return frozenset(original(db, user)) | frozenset(codes)

    monkeypatch.setattr(permission_cache, "perms_of", patched)


@pytest.fixture()
def env_without_ark(api_client, db_factory, monkeypatch) -> Env:
    """同 `env`，但账号**没有** ark 登录态（测「没凭据时给可读原因」）。"""
    response = api_client.post(
        "/api/auth/register", json={"username": "owner", "password": "secret123"}
    )
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    _reset_fakes()
    monkeypatch.setattr(sgs, "get_qianfan_api_adapter_factory", lambda: FakeSearchApi)
    db = db_factory()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "owner"))
        account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="creator", nickname="无凭据号"
        )
        db.add(account)
        db.commit()
        return Env(headers=headers, account_id=account.id, user_id=user_id, db_factory=db_factory)
    finally:
        db.close()


def _reset_fakes() -> None:
    """每个用例前清空全部替身的记录（跨用例串味会让断言看到别人的调用）。"""
    _SPAWNED.clear()
    FakeOperateApi.calls = []
    FakeOperateApi.responses = []
    FakeOperateApi.cookies_seen = []
    FakeSearchApi.calls = []
    FakeSearchApi.pages = []
    LIMITER["impl"] = AllowLimiter()


def _seed(SessionLocal, account_id: int, raw_item: dict, user_id: int = 1) -> str:
    """走**生产落库路径**塞一行（`store_products`）—— 保证 raw_json 形状与真机一致。

    ⚠️ 不走裸 `ShopProduct(...)`：门禁读的是 `load_product_dict_from_ledger`
    从 raw 派生的字段，裸行会让判据拿到空值（测出来的结论就不是真的）。
    """
    from backend.app.services.shop_goods_sync_service import store_products

    db = SessionLocal()
    try:
        stored = store_products(db, user_id, account_id, [raw_item])
        assert stored == [raw_item["item_id"]], f"播种失败：{stored}"
        #: ⚠️ `store_products` **自己不 commit**（由调用方决定事务边界）——
        #: 忘了这一句就是「播种静默丢失」，测试会以「台账里没有这个商品」的形式
        #: 伪装成门禁问题（本地实测踩到过）。
        db.commit()
        return raw_item["item_id"]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 门禁：判据本身（纯函数，不起服务）
# --------------------------------------------------------------------------- #


def test_on_shelf_only_applies_to_off_shelf_items():
    """上架只在「已下架」上有意义（真机 tab 口径）。"""
    assert gates.support_reason("on_shelf", {"card_type": 2}) is not None, "在售中不该能上架"
    assert gates.support_reason("on_shelf", {"card_type": 3}) is None, "已下架应能上架"


def test_off_shelf_only_applies_to_on_shelf_items():
    """下架只在「售卖中」上有意义。"""
    assert gates.support_reason("off_shelf", {"card_type": 2}) is None
    assert gates.support_reason("off_shelf", {"card_type": 3}) is not None, "已下架不该能再下架"


def test_delete_is_not_status_gated_but_promotion_gated():
    """删除**不限状态**（页面在多个 tab 都给删除入口），但受定金预售活动拦。

    ⚠️ 这是与上下架的关键差别：顺手给 delete 套上「只有已下架能删」会让
    售卖中的商品删不掉（用户会当成 bug）。
    """
    assert gates.support_reason("delete", {"card_type": 2}) is None, "售卖中也应可删"
    assert gates.support_reason("delete", {"card_type": 3}) is None, "已下架也可删"
    reason = gates.support_reason("delete", {"card_type": 2, "is_item_attend_promotion": True})
    assert reason and "预售" in reason, f"定金预售应拦住删除，实际：{reason}"


def test_seller_freeze_blocks_all_three_actions():
    """店铺冻结 ⇒ 三个动作都不可做（平台把这条放在通用前置里）。"""
    item = {"card_type": 3, "seller_freeze": True}
    for action in gates.ALL_ACTIONS:
        reason = gates.support_reason(action, item)
        assert reason and "冻结" in reason, f"{action} 应被冻结拦住，实际：{reason}"


def test_unknown_status_is_conservative():
    """**状态未知 ⇒ 上下架判「不支持」**（缺值口径⑤：不知道就别动交易侧）。

    ⚠️ 删除**不受状态闸**约束，所以未知状态仍可删（它是不可逆操作但平台允许，
    本地没有理由比平台更严）。
    """
    item: dict = {}
    assert gates.support_reason("on_shelf", item) is not None
    assert gates.support_reason("off_shelf", item) is not None
    assert gates.support_reason("delete", item) is None


def test_allowed_actions_lists_unsupported_ones_too():
    """`allowed_actions` 必须**把不支持的也下发**（带原因）。

    只下发可做的 ⇒ 前端无法回答「为什么这个商品没有上架按钮」，用户会当成 bug。
    """
    rows = gates.allowed_actions({"card_type": 3})
    assert [r["action"] for r in rows] == list(gates.ALL_ACTIONS), "三个动作都要在（顺序固定）"
    by_action = {r["action"]: r for r in rows}
    assert by_action["on_shelf"]["supported"] is True
    assert by_action["off_shelf"]["supported"] is False
    assert by_action["off_shelf"]["reason"], "不支持必须给中文原因"


# --------------------------------------------------------------------------- #
# 执行：门禁 + 记账（需求 7）
# --------------------------------------------------------------------------- #


def test_unsupported_items_are_recorded_but_never_sent_to_platform(env, monkeypatch):
    """**不支持的只记录不执行** —— 判据是「平台一次都没被调用」。

    ⚠️ 这是本文件最重要的一条：实现若「先发了再说」，`FakeOperateApi.calls`
    会非空 ⇒ 直接红。写操作发出去就收不回了。
    """
    headers, account_id, user_id, SessionLocal = env
    #: 在售中（card_type=2），却要「上架」⇒ 门禁必拦。
    _seed(SessionLocal, account_id, _item("ITEM-ON", lifecycle=_ON_SHELF), user_id)
    monkeypatch.setattr(operate, "_api_factory", lambda cookies: FakeOperateApi(cookies))

    result = operate.start_item_operate(
        platform_account_id=account_id, user_id=user_id, action="on_shelf", item_ids=["ITEM-ON"]
    )
    operate.run_item_operate(
        result["task_id"],
        platform_account_id=account_id,
        user_id=user_id,
        action="on_shelf",
        item_ids=["ITEM-ON"],
    )

    assert FakeOperateApi.calls == [], (
        f"被门禁拦下的商品**不得**发平台请求，实际发了：{FakeOperateApi.calls}"
    )
    db = SessionLocal()
    try:
        task = db.get(Task, result["task_id"])
        details = task.payload["results"]
        assert details[0]["result"] == "skipped"
        assert "已下架" in details[0]["reason"] or "不支持" in details[0]["reason"]
    finally:
        db.close()


def test_supported_items_reach_the_platform_with_correct_action(env, monkeypatch):
    """过了门禁的商品**必须真发**，且上下架用不同参数（`available` 真/假）。"""
    headers, account_id, user_id, SessionLocal = env
    _seed(SessionLocal, account_id, _item("ITEM-OFF", lifecycle=_OFF_SHELF), user_id)
    monkeypatch.setattr(operate, "_api_factory", lambda cookies: FakeOperateApi(cookies))

    result = operate.start_item_operate(
        platform_account_id=account_id, user_id=user_id, action="on_shelf", item_ids=["ITEM-OFF"]
    )
    operate.run_item_operate(
        result["task_id"],
        platform_account_id=account_id,
        user_id=user_id,
        action="on_shelf",
        item_ids=["ITEM-OFF"],
    )

    assert FakeOperateApi.calls == [
        {"kind": "availability", "ids": ["ITEM-OFF"], "available": True}
    ], f"上架应带 available=True，实际：{FakeOperateApi.calls}"


def test_off_shelf_sends_available_false(env, monkeypatch):
    """下架与上架**共用同一个平台接口**，只差 `available` —— 这里钉住它是 False。"""
    headers, account_id, user_id, SessionLocal = env
    _seed(SessionLocal, account_id, _item("ITEM-ON", lifecycle=_ON_SHELF), user_id)
    monkeypatch.setattr(operate, "_api_factory", lambda cookies: FakeOperateApi(cookies))

    result = operate.start_item_operate(
        platform_account_id=account_id, user_id=user_id, action="off_shelf", item_ids=["ITEM-ON"]
    )
    operate.run_item_operate(
        result["task_id"],
        platform_account_id=account_id,
        user_id=user_id,
        action="off_shelf",
        item_ids=["ITEM-ON"],
    )
    assert FakeOperateApi.calls[0]["available"] is False
    assert FakeOperateApi.calls[0]["kind"] == "availability"


def test_delete_uses_the_delete_api_not_availability(env, monkeypatch):
    """删除走**另一个**平台接口（`batch_delete_items`），不许复用上下架接口。

    ⚠️ 复用 = 把「删除」变成「下架」（用户以为删了，商品还在），
    这种错在真机上不可逆。
    """
    headers, account_id, user_id, SessionLocal = env
    _seed(SessionLocal, account_id, _item("ITEM-ON", lifecycle=_ON_SHELF), user_id)
    monkeypatch.setattr(operate, "_api_factory", lambda cookies: FakeOperateApi(cookies))

    result = operate.start_item_operate(
        platform_account_id=account_id, user_id=user_id, action="delete", item_ids=["ITEM-ON"]
    )
    operate.run_item_operate(
        result["task_id"],
        platform_account_id=account_id,
        user_id=user_id,
        action="delete",
        item_ids=["ITEM-ON"],
    )
    assert FakeOperateApi.calls == [{"kind": "delete", "ids": ["ITEM-ON"]}]


def test_per_item_platform_failure_reasons_are_kept(env, monkeypatch):
    """平台**逐条**失败原因必须落到对应商品上（不许退化成「整批一个笼统原因」）。"""
    headers, account_id, user_id, SessionLocal = env
    _seed(SessionLocal, account_id, _item("ITEM-A", lifecycle=_OFF_SHELF), user_id)
    _seed(SessionLocal, account_id, _item("ITEM-B", lifecycle=_OFF_SHELF), user_id)
    FakeOperateApi.responses = [
        (
            True,
            "",
            {
                "code": 200,
                "data": {
                    "taskId": 9,
                    "failData": [
                        {"itemId": "ITEM-B", "reasons": [{"message": "商品已参加活动，暂不可上架"}]}
                    ],
                },
            },
        )
    ]
    monkeypatch.setattr(operate, "_api_factory", lambda cookies: FakeOperateApi(cookies))

    result = operate.start_item_operate(
        platform_account_id=account_id,
        user_id=user_id,
        action="on_shelf",
        item_ids=["ITEM-A", "ITEM-B"],
    )
    operate.run_item_operate(
        result["task_id"],
        platform_account_id=account_id,
        user_id=user_id,
        action="on_shelf",
        item_ids=["ITEM-A", "ITEM-B"],
    )

    db = SessionLocal()
    try:
        details = {row["item_id"]: row for row in operate.task_snapshot(db, result["task_id"], user_id=user_id)["items"]}
        assert details["ITEM-A"]["result"] == "ok"
        assert details["ITEM-B"]["result"] == "failed"
        assert "暂不可上架" in details["ITEM-B"]["reason"], details["ITEM-B"]
    finally:
        db.close()


def test_missing_ledger_row_is_skipped_with_actionable_reason(env, monkeypatch):
    """台账里没有的商品 ⇒ `skipped` + **可操作的原因**（引导先同步）。

    ⚠️ 不能报「失败」：它不是平台拒绝，是本地还没有这个商品的数据。
    """
    headers, account_id, user_id, SessionLocal = env
    monkeypatch.setattr(operate, "_api_factory", lambda cookies: FakeOperateApi(cookies))
    result = operate.start_item_operate(
        platform_account_id=account_id, user_id=user_id, action="delete", item_ids=["GHOST"]
    )
    operate.run_item_operate(
        result["task_id"],
        platform_account_id=account_id,
        user_id=user_id,
        action="delete",
        item_ids=["GHOST"],
    )
    assert FakeOperateApi.calls == []
    db = SessionLocal()
    try:
        detail = operate.task_snapshot(db, result["task_id"], user_id=user_id)["items"][0]
        assert detail["result"] == "skipped"
        assert "同步商品" in detail["reason"], detail
    finally:
        db.close()


def test_rate_limited_batch_is_skipped_not_forced(env, monkeypatch):
    """限流不过 ⇒ 整批 `skipped`（**不硬闯**），且平台未被调用。"""
    headers, account_id, user_id, SessionLocal = env
    _seed(SessionLocal, account_id, _item("ITEM-OFF", lifecycle=_OFF_SHELF), user_id)
    LIMITER["impl"] = DenyLimiter()
    monkeypatch.setattr(operate, "_api_factory", lambda cookies: FakeOperateApi(cookies))

    result = operate.start_item_operate(
        platform_account_id=account_id, user_id=user_id, action="on_shelf", item_ids=["ITEM-OFF"]
    )
    operate.run_item_operate(
        result["task_id"],
        platform_account_id=account_id,
        user_id=user_id,
        action="on_shelf",
        item_ids=["ITEM-OFF"],
    )
    assert FakeOperateApi.calls == [], "限流时不得发平台请求"
    db = SessionLocal()
    try:
        detail = operate.task_snapshot(db, result["task_id"], user_id=user_id)["items"][0]
        assert detail["result"] == "skipped"
        assert "限流" in detail["reason"]
    finally:
        db.close()


def test_local_status_is_refreshed_after_successful_availability(env, monkeypatch):
    """上/下架成功后**本地台账的信号立即翻转**（不等下一轮同步）。

    ⚠️ 这是**乐观值**（平台事实仍以下轮同步为准），但不刷的话用户会以为没生效。
    删除成功后那一行**必须还在**（平台删除是异步的，本地等对账才清 —— 提前删会让
    用户看不到结果）。
    """
    headers, account_id, user_id, SessionLocal = env
    _seed(SessionLocal, account_id, _item("ITEM-OFF", lifecycle=_OFF_SHELF), user_id)
    monkeypatch.setattr(operate, "_api_factory", lambda cookies: FakeOperateApi(cookies))
    result = operate.start_item_operate(
        platform_account_id=account_id, user_id=user_id, action="on_shelf", item_ids=["ITEM-OFF"]
    )
    operate.run_item_operate(
        result["task_id"],
        platform_account_id=account_id,
        user_id=user_id,
        action="on_shelf",
        item_ids=["ITEM-OFF"],
    )
    db = SessionLocal()
    try:
        row = db.scalars(select(ShopProduct)).one()
        assert row.card_type == 2, "上架成功后本地应立刻显示「售卖中」"
        #: ⚠️ **raw_json 也必须被改**：门禁读的 `status_key` 由它派生 ——
        #: 只改归一化列的话，用户点完下架、按钮还是「下架」（判据没变）。
        from backend.app.services.shop_products_service import status_key_from_raw

        assert status_key_from_raw(json.loads(row.raw_json)) == "on_shelf", (
            "raw_json 里的生命周期信号没跟上 ⇒ 列表页门禁判据不变（按钮状态不刷新）"
        )
        #: 端到端复核：读列表时下发的 allowed_actions 必须已翻转。
        reloaded = load_product_dict_from_ledger(row)
        assert gates.support_reason("on_shelf", reloaded) is not None
        assert gates.support_reason("off_shelf", reloaded) is None
        #: ⚠️ `buyable` 也必须翻转（2026-10-06 修的真 bug）：它与两个时间戳是
        #: **同一个生命周期信号的三面**。漏刷它 ⇒ 上架后 `status` 已是「售卖中」、
        #: `status_key` 已是 `on_shelf`，但前端「可售」列显示**停售**、详情显示
        #: **不可购买**、状态 tag 若按 `buyable` 上色还是**灰的**（用户实测报告）。
        assert reloaded["buyable"] is True, (
            "上架成功后 `buyable` 没跟着翻 ⇒ 「可售」列/详情「可购买」会与状态标签矛盾"
        )
    finally:
        db.close()

    #: 删除：行必须还在（等对账）
    _seed(SessionLocal, account_id, _item("ITEM-DEL", lifecycle=_ON_SHELF), user_id)
    r2 = operate.start_item_operate(
        platform_account_id=account_id, user_id=user_id, action="delete", item_ids=["ITEM-DEL"]
    )
    operate.run_item_operate(
        r2["task_id"],
        platform_account_id=account_id,
        user_id=user_id,
        action="delete",
        item_ids=["ITEM-DEL"],
    )
    db = SessionLocal()
    try:
        ids = {row.item_id for row in db.scalars(select(ShopProduct)).all()}
        assert "ITEM-DEL" in ids, "删除后本地行不得提前消失（平台是异步删的）"
    finally:
        db.close()


def test_start_rejects_empty_and_unknown_action(env, monkeypatch):
    """参数收口：空清单 / 未知动作 ⇒ `ValueError`（端点翻 400）。"""
    headers, account_id, user_id, SessionLocal = env
    for bad in ([], None):
        try:
            operate.start_item_operate(
                platform_account_id=account_id, user_id=user_id, action="on_shelf", item_ids=bad
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"空清单应报错：{bad!r}")
    try:
        operate.start_item_operate(
            platform_account_id=account_id, user_id=user_id, action="explode", item_ids=["A"]
        )
    except ValueError:
        pass
    else:
        raise AssertionError("未知动作应报错")


def test_duplicate_ids_are_deduped_once(env, monkeypatch):
    """重复 id 去重（否则同一商品会被发两次命令 —— 写操作发两次不是「幂等」）。"""
    headers, account_id, user_id, SessionLocal = env
    _seed(SessionLocal, account_id, _item("ITEM-OFF", lifecycle=_OFF_SHELF), user_id)
    monkeypatch.setattr(operate, "_api_factory", lambda cookies: FakeOperateApi(cookies))
    result = operate.start_item_operate(
        platform_account_id=account_id,
        user_id=user_id,
        action="on_shelf",
        item_ids=["ITEM-OFF", "ITEM-OFF", " ITEM-OFF "],
    )
    operate.run_item_operate(
        result["task_id"],
        platform_account_id=account_id,
        user_id=user_id,
        action="on_shelf",
        item_ids=["ITEM-OFF"],
    )
    assert sum(1 for c in FakeOperateApi.calls) == 1, FakeOperateApi.calls
    assert FakeOperateApi.calls[0]["ids"] == ["ITEM-OFF"]


# --------------------------------------------------------------------------- #
# 任务形态：不进任务中心（需求 7）+ 快照
# --------------------------------------------------------------------------- #


def test_operate_task_is_not_in_the_schedule_registry():
    """**任务中心的任务列表不展示**（需求 7 原话）—— 落实方式 = 不进注册表。

    ⚠️ 与 `no_traffic_cleanup` 同一实现方式：`tasks` 表里有记录（队列记录页可见），
    但 `job_registry` 里没有它 ⇒ 定时任务中心看不到、也不能被调度。
    """
    from backend.app.services import job_registry

    keys = set(job_registry.job_registry())
    assert operate.OPERATE_TASK_TYPE not in keys, (
        "商品操作任务不得进任务中心注册表（需求 7：任务中心的任务列表不展示）"
    )


def test_task_snapshot_exposes_progress_and_details(env, monkeypatch):
    """快照契约：进度三数 + 逐条明细（前端弹窗的**唯一**数据源）。"""
    headers, account_id, user_id, SessionLocal = env
    _seed(SessionLocal, account_id, _item("ITEM-ON", lifecycle=_ON_SHELF), user_id)
    _seed(SessionLocal, account_id, _item("ITEM-OFF", lifecycle=_OFF_SHELF), user_id)
    monkeypatch.setattr(operate, "_api_factory", lambda cookies: FakeOperateApi(cookies))
    result = operate.start_item_operate(
        platform_account_id=account_id,
        user_id=user_id,
        action="on_shelf",
        item_ids=["ITEM-ON", "ITEM-OFF"],
    )
    operate.run_item_operate(
        result["task_id"],
        platform_account_id=account_id,
        user_id=user_id,
        action="on_shelf",
        item_ids=["ITEM-ON", "ITEM-OFF"],
    )
    db = SessionLocal()
    try:
        snap = operate.task_snapshot(db, result["task_id"], user_id=user_id)
    finally:
        db.close()
    assert snap["status"] == "done"
    assert snap["total"] == 2
    #: ⚠️ 计数键是 `succeeded` / `skipped` / `failed`（与 `_finish` 落的 payload 同源）。
    assert snap["succeeded"] == 1 and snap["skipped"] == 1, snap
    assert len(snap["items"]) == 2


def test_snapshot_is_user_scoped(env, monkeypatch):
    """跨用户取别人任务 ⇒ `None`（不是 403、不是别人数据）。"""
    headers, account_id, user_id, SessionLocal = env
    result = operate.start_item_operate(
        platform_account_id=account_id, user_id=user_id, action="delete", item_ids=["X"]
    )
    db = SessionLocal()
    try:
        assert operate.task_snapshot(db, result["task_id"], user_id=user_id + 999) is None
    finally:
        db.close()


def test_same_account_reuses_in_flight_task(env, monkeypatch):
    """同一账号已有在跑的操作任务 ⇒ 复用（`reused=True`），不并发发两批命令。"""
    headers, account_id, user_id, SessionLocal = env
    first = operate.start_item_operate(
        platform_account_id=account_id, user_id=user_id, action="delete", item_ids=["A"]
    )
    second = operate.start_item_operate(
        platform_account_id=account_id, user_id=user_id, action="delete", item_ids=["B"]
    )
    assert second["reused"] is True, f"第二次应复用：{second}"
    assert second["task_id"] == first["task_id"]
    assert len(_SPAWNED) == 1, "复用不得再起一个线程（否则同一批商品会被发两遍命令）"


# --------------------------------------------------------------------------- #
# 同步指定商品（需求 6）
# --------------------------------------------------------------------------- #


def test_specified_sync_matches_exact_ids_only(env, monkeypatch):
    """**平台按模糊关键字返回 ⇒ 本地必须按精确 id 再筛一遍**。

    ⚠️ 这条是本功能的根因守卫：`whole_keywords` 是模糊匹配（搜 ID 会命中标题里
    凑巧含该串的商品）。不筛 ⇒ 台账里会多出**用户没点名**的商品，
    比「少同步了一个」更坏（台账是用户对账的依据）。
    """
    headers, account_id, user_id, SessionLocal = env
    FakeSearchApi.pages = [
        (
            [
                _item("ITEM-WANT"),
                #: 标题里含 "ITEM" 串的无关商品 —— 模糊匹配会把它带回来。
                _item("ITEM-OTHER", title="ITEM-WANT 同款桌垫"),
            ],
            2,
            False,
        )
    ]
    monkeypatch.setattr(sgs, "get_qianfan_api_adapter_factory", lambda: FakeSearchApi)

    out = sgs.sync_specified_items(account_id, user_id=user_id, item_ids=["ITEM-WANT"])

    assert out["synced"] == 1, out
    assert out["not_found"] == [], out
    db = SessionLocal()
    try:
        ids = {row.item_id for row in db.scalars(select(ShopProduct)).all()}
        assert ids == {"ITEM-WANT"}, f"不该落进无关商品，实际：{ids}"
    finally:
        db.close()


def test_specified_sync_honours_the_time_window(env, monkeypatch):
    """**按 ID 同步也必须认条件弹窗的时间区间**（2026-10-06 合并后的新面）。

    合并「批量同步商品信息」后，两条路共用**同一个条件弹窗**（全量 / 时间区间）。
    原实现的按 ID 同步**只认 ID**（那时它是独立按钮、没有条件弹窗）⇒ 用户选了
    「按时间区间」这条路会**静默失效**：他以为限了区间，实际扫了全店。

    判据落在**真的发给平台的参数**上（`create_time_from`），不是「有没有传参」。
    ⚠️ 平台参数单位是**秒**且 `>=` 比较（DEF-045）⇒ 毫秒下界须 `//1000 + 1`。
    """
    headers, account_id, user_id, SessionLocal = env
    FakeSearchApi.pages = [([_item("ITEM-WANT")], 1, False)]
    monkeypatch.setattr(sgs, "get_qianfan_api_adapter_factory", lambda: FakeSearchApi)
    FakeSearchApi.calls = []

    out = sgs.sync_specified_items(
        account_id,
        user_id=user_id,
        item_ids=["ITEM-WANT"],
        scope="range",
        start_date="2026-10-01",
        end_date="2026-10-02",
    )
    assert out["synced"] == 1, out
    assert FakeSearchApi.calls, "应至少发一次平台请求"
    sent = FakeSearchApi.calls[0].get("create_time_from")
    assert sent, "按 ID 同步没把时间区间透给平台 ⇒ 用户在弹窗里选的区间静默失效"

    #: `full` ⇒ 不设下界（与整店同步同口径）
    FakeSearchApi.calls = []
    sgs.sync_specified_items(account_id, user_id=user_id, item_ids=["ITEM-WANT"], scope="full")
    assert FakeSearchApi.calls[0].get("create_time_from") in (None, 0), (
        "全量同步不该带下界（带上就变成「只同步某天以后的」，与选项名不符）"
    )


def test_specified_sync_never_deletes_existing_rows(env, monkeypatch):
    """**只补不删** —— 命中之外的商品一行不动（与全量同步刻意不同）。"""
    headers, account_id, user_id, SessionLocal = env
    _seed(SessionLocal, account_id, _item("OLD-1"), user_id)
    _seed(SessionLocal, account_id, _item("OLD-2"), user_id)
    FakeSearchApi.pages = [([_item("ITEM-WANT")], 1, False)]
    monkeypatch.setattr(sgs, "get_qianfan_api_adapter_factory", lambda: FakeSearchApi)

    sgs.sync_specified_items(account_id, user_id=user_id, item_ids=["ITEM-WANT"])

    db = SessionLocal()
    try:
        ids = {row.item_id for row in db.scalars(select(ShopProduct)).all()}
        assert ids == {"OLD-1", "OLD-2", "ITEM-WANT"}, f"指定同步不得删旧行，实际：{ids}"
    finally:
        db.close()


def test_specified_sync_reports_not_found(env, monkeypatch):
    """没找到的 id ⇒ 出现在 `not_found` 里（**不静默消失** —— 用户要知道哪个没成功）。"""
    headers, account_id, user_id, SessionLocal = env
    FakeSearchApi.pages = [([_item("ITEM-A")], 1, False)]
    monkeypatch.setattr(sgs, "get_qianfan_api_adapter_factory", lambda: FakeSearchApi)

    out = sgs.sync_specified_items(
        account_id, user_id=user_id, item_ids=["ITEM-A", "ITEM-MISSING"]
    )
    assert out["synced"] == 1
    assert out["not_found"] == ["ITEM-MISSING"]
    assert "未在平台找到" in out["note"]


def test_specified_sync_queries_with_status_all(env, monkeypatch):
    """读接口必须按 **`all`** 状态查。

    ⚠️ 要补的商品可能已下架 / 审核中，按默认的「售卖中」筛会漏掉它们
    —— 而用户点名要的就是它最新的状态。
    """
    headers, account_id, user_id, SessionLocal = env
    FakeSearchApi.pages = [([_item("ITEM-A")], 1, False)]
    #: ⚠️ 显式清一次：`FakeSearchApi` 是**类级**替身，`calls` 跨用例共享。
    #: 夹具已在 `_reset_fakes` 里清过，但这里读的是 `calls[0]` —— 多一道自清，
    #: 免得将来夹具顺序变化时本用例变成「看到别人的调用」而假红。
    FakeSearchApi.calls = []
    monkeypatch.setattr(sgs, "get_qianfan_api_adapter_factory", lambda: FakeSearchApi)
    sgs.sync_specified_items(account_id, user_id=user_id, item_ids=["ITEM-A"])
    assert FakeSearchApi.calls, "应至少发一次平台请求"
    assert FakeSearchApi.calls[0]["status"] == sgs.sts.SYNC_ITEM_STATUS_ALL, FakeSearchApi.calls[0]
    assert FakeSearchApi.calls[0]["keyword"] == "ITEM-A"


def test_specified_sync_without_ark_session_is_readable(env_without_ark):
    """没凭据 ⇒ 可读原因（不是异常栈、不是静默返回 0）。"""
    headers, account_id, user_id, SessionLocal = env_without_ark
    out = sgs.sync_specified_items(account_id, user_id=user_id, item_ids=["ITEM-A"])
    assert out["synced"] == 0
    assert "千帆" in out["note"], out


def test_specified_sync_empty_ids_short_circuits(env, monkeypatch):
    """空清单 ⇒ 直接返回（`not_found` 也空），**不发任何平台请求**。"""
    headers, account_id, user_id, SessionLocal = env
    monkeypatch.setattr(sgs, "get_qianfan_api_adapter_factory", lambda: FakeSearchApi)
    out = sgs.sync_specified_items(account_id, user_id=user_id, item_ids=["  ", ""])
    assert out["synced"] == 0 and out["not_found"] == []
    assert FakeSearchApi.calls == []


def test_specified_sync_dedupes_ids(env, monkeypatch):
    """重复 id 去重保序（「请求几条」与「处理几条」不许虚报）。"""
    headers, account_id, user_id, SessionLocal = env
    FakeSearchApi.pages = [([_item("ITEM-A")], 1, False)]
    monkeypatch.setattr(sgs, "get_qianfan_api_adapter_factory", lambda: FakeSearchApi)
    out = sgs.sync_specified_items(
        account_id, user_id=user_id, item_ids=["ITEM-A", "ITEM-A"]
    )
    assert out["synced"] == 1
    assert len(FakeSearchApi.calls) == 1, "去重后只发一次"


# --------------------------------------------------------------------------- #
# 端到端（走真端点 —— 门禁 / 归属 / 410 墓碑）
# --------------------------------------------------------------------------- #


def test_operate_endpoint_requires_owned_account(env, api_client):
    """端点：别人的账号 ⇒ 404（不是 403、不是 200）。"""
    headers, account_id, user_id, SessionLocal = env
    response = api_client.post(
        "/api/xhs/qianfan/items/operate",
        json={"platform_account_id": account_id + 999, "action": "delete", "item_ids": ["A"]},
        headers=headers,
    )
    assert response.status_code == 404, response.text


def test_operate_endpoint_rejects_unknown_action_and_oversize(env, api_client):
    """端点参数收口：未知动作 / 超上限 ⇒ 400（结构化，不是 422 空白）。"""
    headers, account_id, user_id, SessionLocal = env
    bad_action = api_client.post(
        "/api/xhs/qianfan/items/operate",
        json={"platform_account_id": account_id, "action": "explode", "item_ids": ["A"]},
        headers=headers,
    )
    #: 端点自己收口 ⇒ 400 **带结构化 code**（比返一个空壳 422 有用得多）。
    assert bad_action.status_code == 400, bad_action.text
    assert bad_action.json()["detail"]["code"] == "QIANFAN_OPERATE_ACTION_UNSUPPORTED"

    too_many = api_client.post(
        "/api/xhs/qianfan/items/operate",
        json={
            "platform_account_id": account_id,
            "action": "delete",
            "item_ids": [f"I-{i}" for i in range(operate.MAX_ITEMS_PER_TASK + 1)],
        },
        headers=headers,
    )
    assert too_many.status_code == 422, too_many.text


def test_sync_endpoint_rejects_empty_and_oversize(env, api_client):
    """`POST /items/sync`：空清单 / 超上限 ⇒ 400（不静默返回成功）。"""
    headers, account_id, user_id, SessionLocal = env
    empty = api_client.post(
        "/api/xhs/qianfan/items/sync",
        json={"platform_account_id": account_id, "item_ids": ["   "]},
        headers=headers,
    )
    assert empty.status_code == 400, empty.text
    assert empty.json()["detail"]["code"] == "QIANFAN_ITEM_IDS_REQUIRED"

    oversize = api_client.post(
        "/api/xhs/qianfan/items/sync",
        json={
            "platform_account_id": account_id,
            "item_ids": [f"I-{i}" for i in range(operate.MAX_ITEMS_PER_TASK + 1)],
        },
        headers=headers,
    )
    assert oversize.status_code == 422, oversize.text


def test_sync_endpoint_returns_readable_result(env, api_client, monkeypatch):
    """端点回执（2026-10-11 改入队）：`{enqueued, already, batch_no, note}` 键齐全。

    ⚠️ 用户口径「新增一个同步商品信息队列」：`items/sync` 由同步阻塞改为**入队**，
    worker 异步执行（`sync_specified_items` 的逐条同步逻辑不变，只是被 worker 认领后调）。
    """
    headers, account_id, user_id, SessionLocal = env
    monkeypatch.setattr(sgs, "get_qianfan_api_adapter_factory", lambda: FakeSearchApi)
    response = api_client.post(
        "/api/xhs/qianfan/items/sync",
        json={"platform_account_id": account_id, "item_ids": ["ITEM-A"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) >= {"enqueued", "already", "batch_no", "note"}
    assert body["enqueued"] == 1


def test_front_back_max_items_are_the_same_value():
    """**前后端同值**：前端放行后端拒 = 用户点个注定失败的按钮。

    ⚠️ 与 `qianfan.MAX_OPERATE_ITEMS` 的 import 期断言是**两道**保证：
    那道守后端内部两个常量，这道守「前端 TS 常量 == 后端 Python 常量」
    （跨语言，只能靠读源码 —— 但那正是它值得被测的理由）。
    """
    from pathlib import Path
    import re

    from tests.backend.conftest import read_qianfan_source

    source = read_qianfan_source()  # 整包（2026-10-11 拆包）
    assert "MAX_OPERATE_ITEMS: int = 200" in source, "后端上限变了，前端常量要跟着改"

    ts = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "lib"
        / "shop-items-view.ts"
    ).read_text(encoding="utf-8")
    match = re.search(r"export const MAX_OPERATE_ITEMS\s*=\s*(\d+)", ts)
    assert match, "前端找不到 MAX_OPERATE_ITEMS —— 前后端同值纪律的载体没了"
    assert int(match.group(1)) == operate.MAX_ITEMS_PER_TASK, (
        f"前端 {match.group(1)} != 后端 {operate.MAX_ITEMS_PER_TASK}"
    )


def test_gates_module_has_no_platform_dependency():
    """门禁模块**零平台依赖**（纯本地判据 ⇒ 列表端点能便宜地调它）。

    ⚠️ 门禁一旦 import 了 SDK / adapter，列表端点（每页 50 行 × 3 动作）就会
    有被误用成「顺手发个请求」的风险。
    """
    import ast

    source = inspect.getsource(gates)
    tree = ast.parse(source)
    #: ⚠️ 只看**模块顶层** import：函数内的延迟 import（`_status_label` 里取
    #: `QIANFAN_ITEM_STATUS_LABELS`）是刻意为之 —— 它不在端点每次调用时执行，
    #: 也不构成「门禁顺手能发请求」的风险。顶层 import 才是那个风险面。
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    offenders = [name for name in imported if name and ("apis" in name or "adapter" in name)]
    assert not offenders, f"门禁不得在顶层依赖平台 SDK：{offenders}"
