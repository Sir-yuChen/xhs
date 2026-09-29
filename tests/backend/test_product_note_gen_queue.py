"""商品生成笔记队列（设计 13 §31.10，2026-09-25 用户口径
「单独生成商品笔记和批量生成商品笔记均需走队列」）。

这批用例盯的是**队列纪律与契约**，不是平台行为（平台行为由真机验证记录背书）：

1. **入队即返回**：`/generate-save` 与 `/batch-generate` 同步段只做门禁 + 入队，
   毫秒级回执；三步生成 + 轮询 + 落库在后台 worker 跑；
2. **门禁失败零平台请求**：未绑店 / 无 ark 令牌 / 台账无此行 —— 三个 400/404，
   且假适配器一次都不被构造、一条队列项都不落；
3. **幂等**：同商品未终态 ⇒ `already_queued`，队列里只有一条（不重复生成）；
4. **批量错误隔离**：门禁不过项不阻断其余（与千帆页面 `Promise.allSettled` 同语义）；
5. **平台失败记在队列上**：worker 里失败 ⇒ 队列项 `failed` + 平台原文，
   **不落库**（无半条笔记），原因只在快照 `finished` 给一次；
6. **FIFO 顺序执行**：一个 worker 逐项跑（顺序 = 平台侧节流的第二道保障）；
7. **守活四件**（DEF-069 同型）：篇内 `BaseException` 网（`SystemExit` 也记 failed）、
   循环外层网、看门狗重建（先复位再起新 worker）、停止出口（不再认领）；
8. **快照形状契约**（2026-09-26 常驻化）：`running` / `queued` / `total` /
   `finished`，**不许有计数字段**（本轮成败由前端累计；底座 `synced` 不许漏出）。

⚠️ 测试卫生（与发布 / 同步 worker 测试同规）：worker 是**进程级全局线程**，
夹具必须显式请求 `monkeypatch`（teardown 时先收 worker、再恢复 `_session_factory`），
否则残留 worker 会拿**真库**认领测试塞的假行 = 数据隔离事故。
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory
from backend.app.core.database import Base, get_db
from backend.app.main import app
from backend.app.models import Note, NoteAsset, PlatformAccount, ShopProduct, Task, User
from backend.app.models.shop import NoteGoods, Shop
from backend.app.models.note import NoteTopic, Topic
from backend.app.services import note_queue_service as nqs
from backend.app.services import product_note_gen_service as pngs
from tests.backend.cookie_fixtures import ARK_COOKIES, bind_shop_account

ITEM_ID = "ITEM-1"
SKU_ID = "SKU-1"


class FakeGenAdapter:
    """智能生成三步链路假适配器（脚本化，不联网）。

    `fail_task=True` 时平台返回 `success=false` —— 制造「平台生成失败」，
    验证「记失败 + 不落库 + 原因只在快照给一次」。构造也计数：门禁失败时
    一次适配器都不该被造出来。

    `hold`（`threading.Event`）非 None 时，`aigc_publish_attributes` 先等它再返回
    —— 把生成停在「已认领未跑完」的**确定态**。幂等用例必须用它：假适配器秒返回
    ⇒ 第一条可能在下一条请求到达前就终态了，`already_queued` 变成竞态假象。
    """

    calls: list[str] = []
    constructed = 0
    fail_task = False
    attributes_order: list[str] = []
    hold: threading.Event | None = None

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies
        FakeGenAdapter.constructed += 1

    # ---- 生成链路（三步）----
    def aigc_publish_attributes(self, item_id, *, sku_id=""):
        if FakeGenAdapter.hold is not None:
            FakeGenAdapter.hold.wait(timeout=10)
        FakeGenAdapter.calls.append("attributes")
        FakeGenAdapter.attributes_order.append(str(item_id))
        return True, "ok", {
            "code": 0,
            "data": {
                "sku_list": [
                    {
                        "sku_id": SKU_ID,
                        "sku_name": "测试商品 规格A",
                        "item_name": "测试商品",
                        "item_image": "http://qimg.example/cover.jpg",
                    }
                ],
                "seller_point": {"seller_point_info": ["防水", "防油"]},
                "topic_list": {
                    "topic_list_info": [
                        {"topic": "桌布", "type": 1, "hot_value": 100},
                        {"topic": "活动话题", "type": 2},
                    ]
                },
                "note_style": {"default_style": "好物推荐"},
                "note_scene": {"default_style": "新品种草"},
            },
        }

    def batch_get_category(self, item_ids):
        return False, "not-supported", None  # 需求详情链降级路径

    def get_category_demand_list(self, leaf_category_id):
        return False, "not-supported", None

    def get_demand_detail(self, demand_id):
        return False, "not-supported", None

    def sellerpoint_aigc(self, *, biz_id, aigc_context):
        FakeGenAdapter.calls.append("sellerpoint_aigc")
        return True, "ok", {"code": 0, "data": {"task_id": "TASK-1"}}

    def aigc_task_result(self, task_id):
        FakeGenAdapter.calls.append("aigc_task_result")
        detail = {
            "success": not FakeGenAdapter.fail_task,
            "errMsg": "平台生成失败（测试）" if FakeGenAdapter.fail_task else "",
            "title": "测试桌布真好用",
            "content": "这是一段用于测试的正文内容，长度足够通过基本校验逻辑。",
            "topics": [{"id": "T1", "name": "桌布"}, {"id": "T2", "name": "桌垫"}],
        }
        return True, "ok", {"code": 0, "data": {"task_status": "COMPLETED", "task_detail": json.dumps(detail)}}


def _reset():
    FakeGenAdapter.calls = []
    FakeGenAdapter.constructed = 0
    FakeGenAdapter.fail_task = False
    FakeGenAdapter.attributes_order = []
    FakeGenAdapter.hold = None


def _quiesce_named_generate_workers(*, timeout: float = 5.0) -> list[str]:
    """把进程里**所有活着的** `note-generate-worker` 线程收掉（先置停止位再 join）。"""
    pngs._gen_worker_stop.set()
    pngs._gen_wakeup.set()
    stopped: list[str] = []
    for thread in list(threading.enumerate()):
        if thread.name != pngs.GENERATE_WORKER_THREAD_NAME or not thread.is_alive():
            continue
        thread.join(timeout=timeout)
        if not thread.is_alive():
            stopped.append(thread.name)
        else:
            raise AssertionError(
                f"有 worker 线程收不掉（{thread.name}）—— 它会抢后续用例的队列，"
                "必须先查清是谁起的、为什么退出不了"
            )
    with pngs._gen_queue_lock:
        pngs._gen_worker = None
        pngs._gen_adapter_factories.clear()
    pngs._gen_worker_stop.clear()
    pngs._gen_wakeup.clear()
    return stopped


@pytest.fixture()
def clean_generate_globals(monkeypatch):
    """收干净 worker 线程 + 工厂缓存（显式请求 `monkeypatch` 保证 teardown 顺序）。"""
    _quiesce_named_generate_workers()
    try:
        yield
    finally:
        _quiesce_named_generate_workers()


@pytest.fixture()
def gen_env(api_client, db_factory, monkeypatch):
    """一套可用的生成队列环境：账号 + 绑店 + 台账商品 + worker 打桩指向临时库。

    返回 `(client, headers, account_id)`；商品由用例按需补（`_add_product`）。
    """
    _reset()
    app.dependency_overrides[get_qianfan_api_adapter_factory] = lambda: FakeGenAdapter
    # worker 自建 Session 必须落在**临时库**（否则既跑不到本篇又是数据隔离事故）
    monkeypatch.setattr(pngs, "_session_factory", db_factory)
    # 轮询间隔置 0：不真等 1.5s（假适配器第一次轮询就 COMPLETED）
    monkeypatch.setattr(pngs, "_POLL_INTERVAL", 0)
    try:
        response = api_client.post(
            "/api/auth/register", json={"username": "gen-owner", "password": "secret123"}
        )
        assert response.status_code == 200, response.text
        headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
        db = db_factory()
        try:
            user = db.scalar(select(User).where(User.username == "gen-owner"))
            account = PlatformAccount(
                user_id=user.id, platform="xhs", sub_type="creator", nickname="生成队列店", status="active"
            )
            db.add(account)
            db.commit()
            account_id = account.id
        finally:
            db.close()
        yield api_client, headers, account_id
    finally:
        app.dependency_overrides.pop(get_qianfan_api_adapter_factory, None)


def _bind(db_factory, account_id: int, *, with_ark: bool = True, seller_id: str = "SELLER-1") -> None:
    """绑店（可选不带 ark 令牌）。"""
    db = db_factory()
    try:
        account = db.get(PlatformAccount, account_id)
        bind_shop_account(
            db, account, cookies_text=ARK_COOKIES if with_ark else None, seller_id=seller_id
        )
        db.commit()
    finally:
        db.close()


def _add_product(db_factory, account_id: int, item_id: str, title: str = "测试桌布") -> None:
    """落一行台账商品（`shop_id_fk` 取账号绑定的店）。"""
    db = db_factory()
    try:
        account = db.get(PlatformAccount, account_id)
        db.add(
            ShopProduct(
                user_id=account.user_id,
                shop_id_fk=account.shop_id_fk,
                item_id=item_id,
                item_title=title,
                price="5440",
                images_json=json.dumps(
                    ["http://qimg.example/a.jpg", "http://qimg.example/b.jpg"]
                ),
                raw_json=json.dumps(
                    {"images": [{"link": "http://qimg.example/a.jpg", "width": 800, "height": 800}]}
                ),
            )
        )
        db.commit()
    finally:
        db.close()


def _gen_items(db_factory) -> list[Task]:
    db = db_factory()
    try:
        return db.scalars(
            select(Task).where(Task.task_type == nqs.TASK_TYPE_GEN_ITEM).order_by(Task.id)
        ).all()
    finally:
        db.close()


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# --------------------------------------------------------------------------- #
# 1) 入队即返回 + worker 落库四件套
# --------------------------------------------------------------------------- #
def test_generate_save_enqueues_and_worker_creates_note(gen_env, db_factory):
    """单篇：端点毫秒级回收入队回执；worker 跑完落「笔记 + 素材 + 关系 + 话题」。"""
    client, headers, account_id = gen_env
    _bind(db_factory, account_id)
    _add_product(db_factory, account_id, ITEM_ID)

    response = client.post(
        "/api/xhs/product-notes/generate-save",
        json={"platform_account_id": account_id, "item_id": ITEM_ID},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "queued", "入队回执（不是逐项结果）"
    assert body["item_id"] == ITEM_ID
    assert "already_queued" not in body

    # 同步段不该跑生成链路（三步一次都没发生）
    assert FakeGenAdapter.calls == [], "生成必须在后台 worker 里跑"

    assert pngs.wait_generate_done(timeout=15), "后台 worker 未在超时内跑完"

    db = db_factory()
    try:
        note = db.scalar(select(Note).where(Note.note_type == "local_generated"))
        assert note is not None, "worker 落了一条本地生成笔记"
        assert note.note_id.startswith("local_")
        assert note.publish_status == "pending" and note.published is False
        assert note.raw_json["aigc"]["sku_id"] == SKU_ID
        assets = db.scalars(select(NoteAsset).where(NoteAsset.note_id == note.id)).all()
        assert len(assets) == 2
        # §26.7：关系行按**平台 ID** 落（本地生成笔记的 note_id 是 `local_…` 合成 ID）
        rels = db.scalars(
            select(NoteGoods).where(
                NoteGoods.user_id == note.user_id, NoteGoods.note_id == note.note_id
            )
        ).all()
        assert len(rels) == 1
        assert rels[0].item_id == ITEM_ID
        topic_names = db.scalars(
            select(Topic.name)
            .join(NoteTopic, NoteTopic.topic_id == Topic.id)
            .where(NoteTopic.note_id == note.id)
            .order_by(NoteTopic.id)
        ).all()
        assert list(topic_names) == ["桌布", "桌垫"]
    finally:
        db.close()

    # 队列项终态 + 「生成了哪篇笔记」写进行体（详情抽屉的来源）
    items = _gen_items(db_factory)
    assert len(items) == 1
    item = items[0]
    assert item.status == "done" and item.progress == 100
    payload = item.payload if isinstance(item.payload, dict) else {}
    assert payload["ok"] is True
    assert payload["item_id"] == ITEM_ID
    assert payload["note_id"].startswith("local_")
    assert payload["note_title"] == "测试桌布真好用"
    assert payload["account_id"] == account_id


def test_generate_queue_snapshot_shape(gen_env, db_factory):
    """快照形状契约：`running` / `queued` / `total` / `finished`，**不许有计数字段**。

    2026-09-26 常驻化（用户口径「不需要队列批次，队列是常驻」）：后端只描述队列
    实时态 + 近期终态；「本轮成败数」由前端按自己入队的商品累计（后端不知道用户
    这一轮点了什么）⇒ 响应里再带 `generated` / `failed` 只会把「近期窗口」说成
    「本轮」。底座通用形状的 `synced` 更不许漏出（透传 = 进度卡恒 0 的老坑）。
    """
    client, headers, account_id = gen_env
    _bind(db_factory, account_id)
    _add_product(db_factory, account_id, ITEM_ID)
    response = client.post(
        "/api/xhs/product-notes/generate-save",
        json={"platform_account_id": account_id, "item_id": ITEM_ID},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert pngs.wait_generate_done(timeout=15)

    db = db_factory()
    try:
        snap = pngs.generate_queue_snapshot(db, 1)
    finally:
        db.close()
    assert set(snap) == {"running", "queued", "total", "finished"}
    assert snap["total"] == 1, "终态项留在近期窗口里（不是「新一轮」把它清掉）"
    assert "generated" not in snap and "failed" not in snap and "synced" not in snap
    done = snap["finished"][0]
    assert done["ok"] is True and done["item_id"] == ITEM_ID
    assert done["title"] == "测试桌布真好用", "终态后显示生成的笔记标题"


# --------------------------------------------------------------------------- #
# 2) 门禁失败：结构化错误 + 零平台请求 + 零队列项
# --------------------------------------------------------------------------- #
def test_generate_save_gate_failures_never_touch_the_platform(gen_env, db_factory):
    """三个门禁（未绑店 / 无令牌 / 台账无此行）都是确定性答案 ⇒ 400/404 + 不入队。"""
    client, headers, account_id = gen_env

    # 未绑店 ⇒ 400
    response = client.post(
        "/api/xhs/product-notes/generate-save",
        json={"platform_account_id": account_id, "item_id": ITEM_ID},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "QIANFAN_BINDING_REQUIRED"
    assert FakeGenAdapter.constructed == 0, "门禁失败时一次适配器都不该造"

    # 绑店但无 ark 令牌 ⇒ 400
    _bind(db_factory, account_id, with_ark=False, seller_id="SELLER-NO-ARK")
    response = client.post(
        "/api/xhs/product-notes/generate-save",
        json={"platform_account_id": account_id, "item_id": ITEM_ID},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "QIANFAN_CREDENTIAL_MISSING"
    assert FakeGenAdapter.constructed == 0

    # 台账没这个商品 ⇒ 404
    _bind(db_factory, account_id, with_ark=True, seller_id="SELLER-2")
    response = client.post(
        "/api/xhs/product-notes/generate-save",
        json={"platform_account_id": account_id, "item_id": "NO-SUCH-ITEM"},
        headers=headers,
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "QIANFAN_ITEM_NOT_FOUND"
    assert FakeGenAdapter.constructed == 0
    assert FakeGenAdapter.calls == []
    assert _gen_items(db_factory) == [], "门禁不过 ⇒ 一条队列项都不落"


# --------------------------------------------------------------------------- #
# 3) 幂等：同商品未终态不重复生成
# --------------------------------------------------------------------------- #
def test_generate_save_is_idempotent_for_the_same_item(gen_env, db_factory):
    """重复点同一个商品 ⇒ `already_queued`，队列里只有一条（不重复烧平台额度）。

    ⚠️ 用 `hold` 把生成停在「已认领未跑完」的确定态：假适配器秒返回 ⇒ 第一条可能在
    第二条请求到达前就终态，`already_queued` 会变成竞态假象（假绿）。
    """
    client, headers, account_id = gen_env
    _bind(db_factory, account_id)
    _add_product(db_factory, account_id, ITEM_ID)
    hold = threading.Event()
    FakeGenAdapter.hold = hold

    first = client.post(
        "/api/xhs/product-notes/generate-save",
        json={"platform_account_id": account_id, "item_id": ITEM_ID},
        headers=headers,
    )
    assert first.status_code == 200 and first.json()["status"] == "queued"

    # 等 worker 认领并卡在生成链路里（running = 未终态 ⇒ 幂等判据成立）
    assert _wait_until(
        lambda: [i.status for i in _gen_items(db_factory)] == ["running"], timeout=10
    ), "worker 未认领该项"

    second = client.post(
        "/api/xhs/product-notes/generate-save",
        json={"platform_account_id": account_id, "item_id": ITEM_ID},
        headers=headers,
    )
    assert second.status_code == 200
    assert second.json().get("already_queued") is True, "已在队列 / 执行中 ⇒ 不重复生成"

    hold.set()  # 放行生成
    assert pngs.wait_generate_done(timeout=15)
    items = _gen_items(db_factory)
    assert len(items) == 1, "同商品只该有一条队列项"
    assert items[0].status == "done"
    # 三步链路只跑了一遍（没有重复生成）
    assert FakeGenAdapter.calls.count("attributes") == 1


# --------------------------------------------------------------------------- #
# 4) 批量：门禁隔离 + FIFO + 逐项终态
# --------------------------------------------------------------------------- #
def test_batch_generate_isolates_gate_failures_and_runs_fifo(gen_env, db_factory):
    """3 项挂 1 项（台账没有）⇒ 入队 2 项；worker 按入队序（FIFO）跑完。"""
    client, headers, account_id = gen_env
    _bind(db_factory, account_id)
    _add_product(db_factory, account_id, "ITEM-1", "第一件")
    _add_product(db_factory, account_id, "ITEM-2", "第二件")
    response = client.post(
        "/api/xhs/product-notes/batch-generate",
        json={"platform_account_id": account_id, "item_ids": ["ITEM-1", "ITEM-2", "ITEM-3"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3
    assert body["started"] == 2, "门禁通过的两项入队"
    assert body["failed"] == 1, "台账没有的那项门禁不过（不阻断其余）"
    results = {r["item_id"]: r for r in body["results"]}
    assert results["ITEM-1"]["ok"] is True and results["ITEM-1"]["status"] == "queued"
    assert results["ITEM-3"]["ok"] is False
    assert results["ITEM-3"]["code"] == "QIANFAN_ITEM_NOT_FOUND"

    assert pngs.wait_generate_done(timeout=15)
    # FIFO：attributes 的调用序 = 入队序（一个 worker 顺序执行）
    assert FakeGenAdapter.attributes_order == ["ITEM-1", "ITEM-2"]

    db = db_factory()
    try:
        notes = db.scalars(select(Note).where(Note.note_type == "local_generated")).all()
        assert len(notes) == 2, "两项都落库"
    finally:
        db.close()

    items = _gen_items(db_factory)
    assert [i.status for i in items] == ["done", "done"], "队列项全部终态"


def test_batch_generate_reports_already_queued_per_item(gen_env, db_factory):
    """同一批里已在队列的商品 ⇒ 该项 `already_queued`，不重复入队。

    `started` 口径与同步批次**逐字一致**（`ok` 项计数，含 `already_queued`）——
    三个队列一个语义，判据看逐项 `already_queued` 与「库里只有一条」。
    同样用 `hold` 造确定态（秒返回的替身会让第一条在第二批到达前终态）。
    """
    client, headers, account_id = gen_env
    _bind(db_factory, account_id)
    _add_product(db_factory, account_id, ITEM_ID)
    hold = threading.Event()
    FakeGenAdapter.hold = hold

    first = client.post(
        "/api/xhs/product-notes/batch-generate",
        json={"platform_account_id": account_id, "item_ids": [ITEM_ID]},
        headers=headers,
    )
    assert first.json()["started"] == 1
    assert _wait_until(
        lambda: [i.status for i in _gen_items(db_factory)] == ["running"], timeout=10
    ), "worker 未认领该项"

    # 第二批同样商品（正在跑）⇒ 幂等
    second = client.post(
        "/api/xhs/product-notes/batch-generate",
        json={"platform_account_id": account_id, "item_ids": [ITEM_ID]},
        headers=headers,
    )
    assert second.json()["total"] == 1 and second.json()["started"] == 1
    assert second.json()["results"][0]["already_queued"] is True

    hold.set()
    assert pngs.wait_generate_done(timeout=15)
    assert len(_gen_items(db_factory)) == 1, "库里只有一条"
    assert FakeGenAdapter.calls.count("attributes") == 1, "只真跑过一次"


# --------------------------------------------------------------------------- #
# 5) 平台失败：记在队列上，不落库
# --------------------------------------------------------------------------- #
def test_platform_failure_marks_item_failed_and_writes_nothing(gen_env, db_factory):
    """平台 `success=false` ⇒ 队列项 `failed` + 平台原文；库里一条半成品都不留。"""
    client, headers, account_id = gen_env
    _bind(db_factory, account_id)
    _add_product(db_factory, account_id, ITEM_ID)
    FakeGenAdapter.fail_task = True
    response = client.post(
        "/api/xhs/product-notes/generate-save",
        json={"platform_account_id": account_id, "item_id": ITEM_ID},
        headers=headers,
    )
    assert response.status_code == 200, "入队照常成功（失败发生在 worker 里）"
    assert pngs.wait_generate_done(timeout=15)

    db = db_factory()
    try:
        assert db.scalar(select(Note)) is None, "生成失败不落库（无半条笔记）"
        assert db.scalar(select(NoteAsset)) is None
        assert db.scalar(select(NoteGoods)) is None
    finally:
        db.close()

    items = _gen_items(db_factory)
    assert len(items) == 1
    item = items[0]
    assert item.status == "failed" and item.progress == 0
    payload = item.payload if isinstance(item.payload, dict) else {}
    assert payload["ok"] is False
    assert "平台生成失败（测试）" in payload["reason"], "平台原文不替换（DEF-058）"
    assert payload["code"] == "AI_GENERATION_FAILED"

    # 快照 finished 把失败原因给一次（与批量同一个出口）
    db = db_factory()
    try:
        snap = pngs.generate_queue_snapshot(db, 1)
    finally:
        db.close()
    assert snap["total"] == 1 and snap["queued"] == [] and snap["running"] is None
    assert snap["finished"][0]["ok"] is False
    assert "平台生成失败（测试）" in snap["finished"][0]["reason"]


# --------------------------------------------------------------------------- #
# 6) 守活四件（DEF-069 同型）
# --------------------------------------------------------------------------- #
def test_run_one_generate_survives_system_exit(clean_generate_globals, gen_env, db_factory, monkeypatch):
    """篇内网接住 `SystemExit`：队列项记 failed，线程不死（安全网不条件性，DEF-048）。"""
    client, headers, account_id = gen_env
    _bind(db_factory, account_id)
    _add_product(db_factory, account_id, ITEM_ID)

    def _boom(*args, **kwargs):
        raise SystemExit("SDK/curl_cffi 静默退出（测试替身）")

    monkeypatch.setattr(pngs, "generate_note_for_item", _boom)

    db = db_factory()
    try:
        row, _ = nqs.enqueue_item(
            db,
            user_id=1,
            item_task_type=nqs.TASK_TYPE_GEN_ITEM,
            account_id=1,
            item_id=ITEM_ID,
            source=nqs.SOURCE_PRODUCT_NOTE_GENERATE_SINGLE,
        )
        db.commit()
        task_id = row.id
    finally:
        db.close()

    pngs._run_one_generate(task_id)

    db = db_factory()
    try:
        item = db.get(Task, task_id)
        assert item.status == "failed"
        payload = item.payload if isinstance(item.payload, dict) else {}
        assert "SystemExit" in payload["reason"], "异常类型进原因（不静默）"
        assert payload["code"] == "GEN_FAILED"
    finally:
        db.close()


def test_watchdog_revives_dead_worker_and_survives_revive_failures(
    clean_generate_globals, monkeypatch
):
    """看门狗循环：探针报死 ⇒ 重建；重建连续失败也不许把自己带走（指数退避重试）。"""
    monkeypatch.setattr(pngs, "generate_worker_thread_alive", lambda: False)
    stamps: list[float] = []

    def _flaky_revive():
        stamps.append(time.monotonic())
        if len(stamps) < 2:
            raise RuntimeError("重建失败（测试替身：库临时不可用）")

    monkeypatch.setattr(pngs, "_revive_generate_worker", _flaky_revive)

    interval = 0.2
    thread = pngs.start_generate_worker_watchdog(interval=interval)
    try:
        assert _wait_until(lambda: len(stamps) >= 3, timeout=5), "失败后要退避重试，不能停"
        assert thread.is_alive() is True, "看门狗自己绝不被一次失败带走（DEF-067 同规）"
        gap = stamps[1] - stamps[0]
        assert gap >= interval * 1.5, f"失败后必须指数退避（实测间隔 {gap:.2f}s，热循环会≈0）"
    finally:
        pngs.stop_generate_worker_watchdog()
    assert _wait_until(lambda: not thread.is_alive(), timeout=5), "stop 必须能收掉看门狗"


def test_revive_resets_stuck_item_and_spawns_fresh_worker(
    clean_generate_globals, gen_env, db_factory, monkeypatch
):
    """`_revive_generate_worker`：先复位卡在 `running` 的行，再起新 worker（不丢项）。"""
    client, headers, account_id = gen_env
    _bind(db_factory, account_id)
    _add_product(db_factory, account_id, ITEM_ID)

    # 挡住新 worker 消费队列：阻塞桩挂在真停止位上（断言期间库纹丝不动）
    monkeypatch.setattr(
        pngs, "_gen_worker_loop", lambda: pngs._gen_worker_stop.wait(timeout=10)
    )

    db = db_factory()
    try:
        row, _ = nqs.enqueue_item(
            db,
            user_id=1,
            item_task_type=nqs.TASK_TYPE_GEN_ITEM,
            account_id=1,
            item_id=ITEM_ID,
            source=nqs.SOURCE_PRODUCT_NOTE_GENERATE_SINGLE,
        )
        db.commit()
        task_id = row.id
        nqs.claim_next_item(db, item_task_type=nqs.TASK_TYPE_GEN_ITEM)
    finally:
        db.close()

    # 一条真死了的线程对象（模块变量会骗人，is_alive 不会）
    dead = threading.Thread(
        target=lambda: None, name=pngs.GENERATE_WORKER_THREAD_NAME, daemon=True
    )
    dead.start()
    dead.join(timeout=5)
    pngs._gen_worker = dead

    pngs._revive_generate_worker()

    db = db_factory()
    try:
        item = db.get(Task, task_id)
        assert item.status == "pending", "卡在 running 的那条必须复位成 pending（不丢项）"
        assert item.retry_count == 1, "复位同时计入一次重试"
    finally:
        db.close()
    assert pngs._gen_worker is not None and pngs._gen_worker.is_alive()


def test_stop_generate_worker_stops_claiming_and_exits(
    clean_generate_globals, gen_env, db_factory, monkeypatch
):
    """`stop_generate_worker`：置位后不再认领新任务并退出；未跑的项留在库里。"""
    client, headers, account_id = gen_env
    _bind(db_factory, account_id)
    _add_product(db_factory, account_id, ITEM_ID)

    claimed: list[int] = []
    monkeypatch.setattr(pngs, "_run_one_generate", lambda task_id: claimed.append(task_id))

    db = db_factory()
    try:
        row, _ = nqs.enqueue_item(
            db,
            user_id=1,
            item_task_type=nqs.TASK_TYPE_GEN_ITEM,
            account_id=1,
            item_id=ITEM_ID,
            source=nqs.SOURCE_PRODUCT_NOTE_GENERATE_SINGLE,
        )
        db.commit()
        task_id = row.id
    finally:
        db.close()

    pngs.stop_generate_worker(timeout=5)  # 先置停止位：之后起的 worker 不该认领任何东西
    worker = threading.Thread(
        target=pngs._gen_worker_loop, name=pngs.GENERATE_WORKER_THREAD_NAME, daemon=True
    )
    worker.start()

    assert _wait_until(lambda: not worker.is_alive(), timeout=5), "停止位 + 唤醒后必须退出"
    assert claimed == [], "停止位生效期间不许认领新任务"
    db = db_factory()
    try:
        assert db.get(Task, task_id).status == "pending", "没被吃掉：留在库里等下次启动"
    finally:
        db.close()


def test_recover_interrupted_covers_gen_items(clean_generate_globals, db_factory):
    """启动恢复（`recover_interrupted`）连商品生成队列一起复位（`QUEUE_ITEM_TASK_TYPES` 收口）。"""
    db = db_factory()
    try:
        row, _ = nqs.enqueue_item(
            db,
            user_id=1,
            item_task_type=nqs.TASK_TYPE_GEN_ITEM,
            account_id=1,
            item_id=ITEM_ID,
            source=nqs.SOURCE_PRODUCT_NOTE_GENERATE_SINGLE,
        )
        db.commit()
        nqs.claim_next_item(db, item_task_type=nqs.TASK_TYPE_GEN_ITEM)
        assert db.get(Task, row.id).status == "running"
    finally:
        db.close()

    result = nqs.recover_interrupted(db_factory())
    assert result["requeued"] == 1

    db = db_factory()
    try:
        item = db.get(Task, row.id)
        assert item.status == "pending" and item.retry_count == 1
    finally:
        db.close()
