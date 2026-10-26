"""商品笔记智能生成与发布测试（设计 13 §31，2026-09-23；2026-09-25 异步 + 发布队列；
同日起生成也走队列，§31.10）。

这批用例盯的是**状态机与链路完整性**，不是平台行为（平台行为由真机验证记录
背书，见设计 13 §31.5.1）：

1. **生成落库四件套**：`notes`（`local_generated`/`pending`/`local_` 前缀 ID）+
   `note_assets`（商品图）+ `note_goods`（关系）+`topics_json`（平台话题）；
2. **门禁失败零平台请求**：未绑店 / 无 ark 令牌 / 台账无此行 —— 三个 400/404，
   且假适配器一次都不被构造；
3. **平台失败不落库**：task `success=false` / 轮询超时 ⇒ 队列项 `failed` + 平台
   原文，库里一条半成品都不留（§31.10 起失败发生在 worker 里，入队端点照常 200）；
4. **批量错误隔离**：3 项挂 1 项 ⇒ 其余 2 项入队并落库、失败项带原因；
5. **发布异步全链**（2026-09-25）：`start_publish` 毫秒级返回 publishing，
   后台 worker 跑完下载 → 上传（permit→PUT）→ 预审核 → 发布，终态落库；
6. **发布失败留现场 + 可重发**：平台拒发 ⇒ `failed` + `publish_error` 含原文、
   临时目录保留、可重发；
7. **队列顺序执行**：批量入队按 FIFO 跑完，进度快照（running/queued/计数）正确；
8. **下载节流与临时目录治理**：张间随机 1–3s（测试置 0 不真等）、下载失败删本次
   目录、文件名带商品 ID、成功按**本地** note_id 删目录（历史缺陷回归）；
9. **item-notes 改关系表源**：`note_goods` join `notes`，本地生成笔记带发布态。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory
from backend.app.api.platforms.xhs.qianfan import get_qianfan_api_adapter_factory as _qf_dep
from backend.app.core.database import Base, get_db
from backend.app.main import app
from backend.app.models import Note, NoteAsset, NoteTopic, PlatformAccount, ShopProduct, Task, Topic, User
from backend.app.models.shop import NoteGoods
from backend.app.services import note_publish_service, product_note_gen_service
from tests.backend.cookie_fixtures import bind_shop_account

from tests.backend.conftest import (  # noqa: PLC0415 - schema 复用助手
    _BUILT_SCHEMAS,
    _SCHEMA_BY_ENGINE,
    make_engine,
    reset_test_data,
)


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[2]

ITEM_ID = "ITEM-1"
SKU_ID = "SKU-1"


class FakePublishAdapter:
    """智能生成 + 发布全链路假适配器（脚本化，不联网）。

    `fail_task` / `reject_preaudit` / `fail_publish` 三个开关分别制造三种失败，
    用来验证「不落库 / 记失败 / 可重发」。

    `upload_gate` / `upload_reached`（2026-09-25 异步测试用）：`upload_gate`
    非 None 时，**第一个** `upload_file` 调用先置 `upload_reached` 再等 gate
    —— 把链路停在「下载已完成、上传未开始」的确定态，用来断言异步启动的
    同步段效果（publishing 已落库、临时文件已按商品 ID 命名）。
    """

    calls: list[str] = []
    published_titles: list[str] = []
    #: 每次调用记录**发起线程名**（2026-10-07）。用来证明「生成/发布由 worker 线程
    #: 发起」而不是在请求线程里内联跑 —— 后者正是 §31.10（生成入队）要消灭的形态，
    #: 而「POST 回来时 calls 是否为空」只是时序假设（worker 可能已经跑完）。
    thread_names: list[str] = []
    upload_gate: threading.Event | None = None
    upload_reached: threading.Event | None = None
    #: 平台 note_id 序号（每次成功发布自增）—— 真机每篇返回不同 ID，
    #: `notes(user_id, note_id)` 有唯一键，同 ID 会撞（批量测试实测踩到）。
    _publish_seq = 0

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    # ---- 生成链路 ----
    def aigc_publish_attributes(self, item_id, *, sku_id=""):
        FakePublishAdapter.calls.append("attributes")
        FakePublishAdapter.thread_names.append(threading.current_thread().name)
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
        FakePublishAdapter.calls.append("sellerpoint_aigc")
        return True, "ok", {"code": 0, "data": {"task_id": "TASK-1"}}

    def aigc_task_result(self, task_id):
        FakePublishAdapter.calls.append("aigc_task_result")
        detail = {
            "success": not FakePublishAdapter.fail_task,
            "errMsg": "平台生成失败（测试）" if FakePublishAdapter.fail_task else "",
            "title": "测试桌布真好用",
            "content": "这是一段用于测试的正文内容，长度足够通过基本校验逻辑。",
            "topics": [{"id": "T1", "name": "桌布"}, {"id": "T2", "name": "桌垫"}],
        }
        return True, "ok", {"code": 0, "data": {"task_status": "COMPLETED", "task_detail": json.dumps(detail)}}

    # ---- 上传 ----
    def upload_web_permit(self, file_count=1):
        FakePublishAdapter.calls.append("upload_permit")
        return True, "ok", {
            "code": 0,
            "data": {
                "uploadTempPermits": [
                    {
                        "uploadAddr": "ros-upload.example.com",
                        "token": "TOKEN",
                        "fileIds": [f"spectrum/F{i}" for i in range(max(file_count, 1))],
                    }
                ]
            },
        }

    def upload_file(self, *, upload_addr, file_id, token, file_path):
        gate = FakePublishAdapter.upload_gate
        if gate is not None:
            # 一次性闸门：只拦第一个上传调用（后续放行），reached 先置位再等。
            FakePublishAdapter.upload_gate = None
            if FakePublishAdapter.upload_reached is not None:
                FakePublishAdapter.upload_reached.set()
            gate.wait(timeout=10)
        FakePublishAdapter.calls.append(f"upload_file:{file_id}")
        return True, "成功", None

    # ---- 预审核 ----
    def preaudit_note(self, *, title, content, topic_names, image_list, text_gen_task_id="", image_gen_task_id=""):
        FakePublishAdapter.calls.append("preaudit")
        if FakePublishAdapter.reject_preaudit:
            # 键名与真机响应一致（下划线风格）
            return True, "ok", {
                "code": 0,
                "data": {
                    "audit_result": {
                        "all_pass": False,
                        "content_result": {"check_pass": False, "fail_msg": "正文含违规词（平台审的）"},
                    }
                },
            }
        return True, "ok", {
            "code": 0,
            "data": {
                "audit_result": {
                    "all_pass": True,
                    "title_result": {"check_pass": True},
                    "content_result": {"check_pass": True},
                    "image_results": [{"file_id": "spectrum/F0", "check_pass": True}],
                }
            },
        }

    # ---- 发布 ----
    def publish_note(self, body):
        FakePublishAdapter.calls.append("publish")
        FakePublishAdapter.published_titles.append(str((body.get("common") or {}).get("title") or ""))
        if FakePublishAdapter.fail_publish:
            return False, "平台发布被拒（测试）", {"success": False, "msg": "平台发布被拒（测试）"}
        FakePublishAdapter._publish_seq += 1
        platform_id = f"REAL-NOTE-{FakePublishAdapter._publish_seq}"
        return True, "成功", {"success": True, "data": {"id": platform_id, "note_id": platform_id}}

    # ---- 发布后回查商品笔记列表（补详情地址，§25.4）----
    def get_goods_note_page(self, **kwargs):
        FakePublishAdapter.calls.append("goods_note_page")
        return True, "ok", {
            "code": 0,
            "data": {"note_infos": list(FakePublishAdapter.goods_note_entries)},
        }

    # ---- 类变量开关（测试间重置）----
    fail_task = False
    reject_preaudit = False
    fail_publish = False
    #: 回查商品笔记列表时返回的条目（默认空 = 平台查无此项 ⇒ 跳过补地址）。
    goods_note_entries: list = []


def _reset():
    FakePublishAdapter.calls = []
    FakePublishAdapter.published_titles = []
    FakePublishAdapter.thread_names = []
    FakePublishAdapter.fail_task = False
    FakePublishAdapter.reject_preaudit = False
    FakePublishAdapter.fail_publish = False
    FakePublishAdapter.upload_gate = None
    FakePublishAdapter.upload_reached = None
    FakePublishAdapter._publish_seq = 0
    FakePublishAdapter.goods_note_entries = []
    # 运行时配置的**进程级缓存**也要清：上一个用例可能开了「允许商品重复发布」
    # （`_enable_duplicate_goods_publish`），TTL（3 秒）内不清会让本用例读到旧值 ——
    # 「该被门禁拦下的用例反而放行」正是这类串味的典型表现。
    from backend.app.services import settings_service

    settings_service.reset_cache_for_tests()


def _setup(tmp_path):
    _reset()
    engine = make_engine(tmp_path / 'publish.db')
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

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_qianfan_api_adapter_factory] = lambda: FakePublishAdapter
    app.dependency_overrides[_qf_dep] = lambda: FakePublishAdapter

    response = client.post("/api/auth/register", json={"username": "owner2", "password": "secret123"})
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    db = SessionLocal()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "owner2"))
        account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="creator", nickname="测试店", status="active"
        )
        db.add(account)
        db.commit()
        account_id = account.id
    finally:
        db.close()
    return headers, account_id, SessionLocal


def _teardown():
    app.dependency_overrides.clear()
    from backend.app.services import settings_service

    settings_service.reset_cache_for_tests()  # 别把开关值留给下一个用例
    # 收掉生成队列 worker（§31.10）：进程级全局线程，不收会跨用例抢队列 /
    # 拿真库认领测试塞的假行（与发布 / 同步 worker 同规）。
    product_note_gen_service._gen_worker_stop.set()
    product_note_gen_service._gen_wakeup.set()
    for thread in list(threading.enumerate()):
        if thread.name == product_note_gen_service.GENERATE_WORKER_THREAD_NAME and thread.is_alive():
            thread.join(timeout=5)
    with product_note_gen_service._gen_queue_lock:
        product_note_gen_service._gen_worker = None
        product_note_gen_service._gen_adapter_factories.clear()
    product_note_gen_service._gen_worker_stop.clear()
    product_note_gen_service._gen_wakeup.clear()


def _patch_gen_worker(monkeypatch, db_factory) -> None:
    """生成队列 worker 打桩：Session 落**临时库** + 轮询间隔置 0（不真等 1.5s）。"""
    monkeypatch.setattr(product_note_gen_service, "_session_factory", db_factory)
    monkeypatch.setattr(product_note_gen_service, "_POLL_INTERVAL", 0)


def _bind_with_product(
    db_factory,
    account_id: int,
    user_id: int = 1,
    *,
    with_ark: bool = True,
    seller_id: str = "SELLER-1",
    image_count: int = 3,
    video_url: str = "",
):
    """绑店 + 落一行台账商品（默认 3 张图）。`with_ark=False` = 绑店但不给千帆令牌。

    `seller_id` 可区分多次绑店（`shops` 唯一键是 `(user_id, seller_id)`）；
    `image_count` / `video_url` 用来验证媒体取舍（设计 13 §32.2.1）。
    返回 (shop_pk, product_pk)。
    """
    from tests.backend.cookie_fixtures import ARK_COOKIES

    db = db_factory()
    try:
        account = db.get(PlatformAccount, account_id)
        bind_shop_account(
            db, account, cookies_text=ARK_COOKIES if with_ark else None, seller_id=seller_id
        )
        db.commit()
        #: ADR-021：台账归属键 = 账号（店铺实体已退场）。
        shop_pk = account.id
        #: 唯一键是 `(platform_account_id, item_id)`。本夹具在同一个用例里可能被多次调用
        #: `(platform_account_id, item_id)`。本夹具在同一个用例里可能被多次调用
        #: （依次演「无 ark → 有 ark」两档门禁）⇒ 商品行**已存在就复用**，
        #: 不重复 INSERT（夹具的债在夹具侧还，DEF-082）。
        existing = db.scalar(
            select(ShopProduct).where(
                ShopProduct.platform_account_id == shop_pk,
                ShopProduct.item_id == ITEM_ID,
            )
        )
        if existing is not None:
            return shop_pk, existing.id
        image_urls = [f"http://qimg.example/{chr(ord('a') + i)}.jpg" for i in range(image_count)]
        raw: dict = {
            "images": [
                {"link": url, "width": 800, "height": 800} for url in image_urls
            ]
        }
        if video_url:
            raw["video"] = {"link": video_url, "width": 720, "height": 1280}
        product = ShopProduct(
            user_id=user_id,
            platform_account_id=shop_pk,
            item_id=ITEM_ID,
            item_title="测试桌布",
            price="5440",
            images_json=json.dumps(image_urls),
            raw_json=json.dumps(raw),
        )
        db.add(product)
        db.commit()
        return shop_pk, product.id
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #


def test_generate_save_enqueues_and_worker_creates_note_assets_and_relation(tmp_path, monkeypatch):
    """单篇生成（§31.10 起走队列）：端点毫秒级回收入队回执，worker 落四件套。"""
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        _patch_gen_worker(monkeypatch, db_factory)
        resp = client.post(
            "/api/xhs/product-notes/generate-save",
            json={"platform_account_id": account_id, "item_id": ITEM_ID},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["status"] == "queued", "入队回执（不是逐项结果）"
        #: 2026-10-07 修：**原判据「POST 回来时 `calls == []`」站不住** ——
        #: 端点在返回前就 `ensure_generate_worker()` 唤醒 worker，而本夹具把
        #: `_POLL_INTERVAL` 打成 0 ⇒ worker 常常**先于断言**把这一项跑完（稳定复现）。
        #:
        #: 原意是「生成不在**请求线程**里跑」，那用「立刻断言没跑」表达是**时序假设**，
        #: 不是不变式。改成观察 **worker 线程确实参与了**（跑完也行、正在跑也行）：
        #:   * `body["status"] == "queued"` 已证明端点只入队不回结果（上面那条）；
        #:   * 这里再钉「生成的调用来自 worker 线程名」，与「请求线程内联跑」互斥。
        assert product_note_gen_service.wait_generate_done(timeout=15), "后台 worker 未在超时内跑完"
        #: ⚠️ 断言放在**等 worker 跑完之后**。原判据「POST 回来时 `calls == []`」是**时序假设**
        #: （端点在返回前就 `ensure_generate_worker()` 唤醒 worker，而夹具把 `_POLL_INTERVAL`
        #: 打成 0 ⇒ worker 常常先于断言跑完）。真正的不变式是**发起线程**：生成必须来自
        #: worker 线程，而不是在请求线程里内联跑（后者正是 §31.10 生成入队要消灭的形态）。
        assert FakePublishAdapter.thread_names, "生成链路一次都没被调用"
        assert all(
            name == product_note_gen_service.GENERATE_WORKER_THREAD_NAME
            for name in FakePublishAdapter.thread_names
        ), f"生成必须在后台 worker 里跑，实际线程 {FakePublishAdapter.thread_names}"

        db = db_factory()
        try:
            item = db.scalar(select(Task).where(Task.task_type == "xhs_note_gen_item"))
            assert item is not None and item.status == "done"
            payload = item.payload if isinstance(item.payload, dict) else {}
            note = db.scalar(select(Note).where(Note.note_id == payload["note_id"]))
            assert note is not None
            assert note.note_type == "local_generated"
            assert note.publish_status == "pending"
            assert note.published is False
            # §34：话题进 topics 字典 + note_topics 关系表（`topics_json` 列已移除，
            # 保留就是同一语义的第二份实现）
            topic_names = db.scalars(
                select(Topic.name)
                .join(NoteTopic, NoteTopic.topic_id == Topic.id)
                .where(NoteTopic.note_id == note.id)
                .order_by(NoteTopic.id)
            ).all()
            assert list(topic_names) == ["桌布", "桌垫"]
            aigc = note.raw_json["aigc"]
            assert aigc["task_id"] == "TASK-1"
            assert aigc["sku_id"] == SKU_ID  # 发布链路要用的 SKU 粒度
            assets = db.scalars(select(NoteAsset).where(NoteAsset.note_id == note.id)).all()
            assert len(assets) == 3
            assert assets[0].url == "http://qimg.example/a.jpg"
            # §26.7：关系行按**平台 ID** 落；发布成功后 `note_id` 会被改写成平台真 ID
            rels = db.scalars(
                select(NoteGoods).where(
                    NoteGoods.user_id == note.user_id, NoteGoods.note_id == note.note_id
                )
            ).all()
            assert len(rels) == 1
        finally:
            db.close()
    finally:
        _teardown()


def test_generate_requires_binding_and_token(tmp_path):
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        # 未绑店 ⇒ 400，且适配器一次都没构造（calls 空）
        resp = client.post(
            "/api/xhs/product-notes/generate-save",
            json={"platform_account_id": account_id, "item_id": ITEM_ID},
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "ARK_SESSION_REQUIRED"
        assert FakePublishAdapter.calls == []

        # 绑店但无 ark 令牌 ⇒ 400（ADR-021：无令牌 = 无 ark 会话，与上一档同码）
        _bind_with_product(db_factory, account_id, with_ark=False, seller_id="SELLER-NO-ARK")
        resp = client.post(
            "/api/xhs/product-notes/generate-save",
            json={"platform_account_id": account_id, "item_id": ITEM_ID},
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "ARK_SESSION_REQUIRED"
        assert FakePublishAdapter.calls == []

        # 台账没这个商品 ⇒ 404
        _bind_with_product(db_factory, account_id, with_ark=True, seller_id="SELLER-2")
        resp = client.post(
            "/api/xhs/product-notes/generate-save",
            json={"platform_account_id": account_id, "item_id": "NO-SUCH-ITEM"},
            headers=headers,
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "QIANFAN_ITEM_NOT_FOUND"
        assert FakePublishAdapter.calls == []
    finally:
        _teardown()


def test_generate_platform_failure_marks_item_failed_and_writes_nothing(tmp_path, monkeypatch):
    """平台生成失败（§31.10 起发生在 worker 里）：队列项 failed + 平台原文，不落库。"""
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        _patch_gen_worker(monkeypatch, db_factory)
        FakePublishAdapter.fail_task = True
        resp = client.post(
            "/api/xhs/product-notes/generate-save",
            json={"platform_account_id": account_id, "item_id": ITEM_ID},
            headers=headers,
        )
        assert resp.status_code == 200, "入队照常成功（平台失败在 worker 里）"
        assert product_note_gen_service.wait_generate_done(timeout=15)

        db = db_factory()
        try:
            assert db.scalar(select(Note)) is None  # 不落库
            item = db.scalar(select(Task).where(Task.task_type == "xhs_note_gen_item"))
            assert item is not None and item.status == "failed"
            payload = item.payload if isinstance(item.payload, dict) else {}
            assert "平台生成失败（测试）" in payload["reason"]
            assert payload["code"] == "AI_GENERATION_FAILED"
        finally:
            db.close()
    finally:
        _teardown()


@pytest.mark.slow
def test_batch_generate_isolates_failures(tmp_path, monkeypatch):
    """批量（§31.10）：门禁不过项不阻断其余；通过项入队并由 worker 顺序跑完。"""
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        _patch_gen_worker(monkeypatch, db_factory)
        db = db_factory()
        try:
            shop_pk = account_id
            # 第二件商品存在，第三件不存在（404 路径）
            db.add(
                ShopProduct(
                    user_id=1,
                    platform_account_id=shop_pk,
                    item_id="ITEM-2",
                    item_title="第二件",
                    images_json=json.dumps(["http://qimg.example/d.jpg"]),
                )
            )
            db.commit()
        finally:
            db.close()

        resp = client.post(
            "/api/xhs/product-notes/batch-generate",
            json={"platform_account_id": account_id, "item_ids": [ITEM_ID, "ITEM-2", "ITEM-3"]},
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert body["started"] == 2, "门禁通过的两项入队"
        assert body["failed"] == 1
        results = {r["item_id"]: r for r in body["results"]}
        assert results[ITEM_ID]["ok"] is True and results[ITEM_ID]["status"] == "queued"
        assert results["ITEM-2"]["ok"] is True
        assert results["ITEM-3"]["ok"] is False
        assert results["ITEM-3"]["code"] == "QIANFAN_ITEM_NOT_FOUND"

        # worker 顺序跑完两项（FIFO）
        assert product_note_gen_service.wait_generate_done(timeout=20)
        db = db_factory()
        try:
            notes = db.scalars(select(Note)).all()
            assert len(notes) == 2, "两项都落库、失败项不落"
        finally:
            db.close()
    finally:
        _teardown()


# --------------------------------------------------------------------------- #
# 发布（2026-09-25 起：异步启动 + 队列顺序执行 + 下载节流 + 临时目录治理）
# --------------------------------------------------------------------------- #


def _set_duplicate_goods_publish(db_factory, allowed: bool, user_id: int = 1) -> None:
    """把「允许商品笔记重复发布」开关设成 `allowed`（改**测试库**里的运行时配置行）。

    走 `settings_service` 的读写函数而不是自己 `db.add`：注册路径已经**播种**过这一行
    （设计 10 §7.3），直接插会撞 `uq_system_settings_user_key`；而且这两个函数内部会
    失效缓存（写后立即生效）。
    """
    from backend.app.services import settings_service

    db = db_factory()
    try:
        key = "xhs_allow_duplicate_goods_publish"
        row = settings_service.find_setting(db, user_id, key)
        if row is None:
            settings_service.create_setting(
                db,
                user_id=user_id,
                key=key,
                value=allowed,
                value_type="bool",
                category="publish",
                label="允许商品笔记重复发布",
            )
        else:
            settings_service.update_setting(db, row, value=allowed)
        db.commit()
    finally:
        db.close()
    settings_service.reset_cache_for_tests()  # 写后必须失效（设计 10 §7.4）


def _enable_duplicate_goods_publish(db_factory, user_id: int = 1) -> None:
    """打开开关。本文件里凡是要**连续发布多篇同商品笔记**的用例（队列 FIFO、当日计数）
    都要先开它：默认关闭下第 2 篇起会被 §31.5.2 的商品重复发布门禁拦下，队列用例就测不到
    队列了。开关自身的用例见 `test_publish_goods_duplicate_guard_*`（那批刻意**不开**）。"""
    _set_duplicate_goods_publish(db_factory, True, user_id)


def _generate_one(db_factory, account_id):
    db = db_factory()
    try:
        account = db.get(PlatformAccount, account_id)
        return product_note_gen_service.generate_note_for_item(
            db, account, ITEM_ID, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
        )["note_id"]
    finally:
        db.close()


def _note_pk(db_factory, note_id: str) -> int:
    db = db_factory()
    try:
        return db.scalar(select(Note.id).where(Note.note_id == note_id))
    finally:
        db.close()


def _rewrite_note_item_id(db_factory, note_pk: int, item_id: str) -> None:
    """把一篇笔记的「挂载商品」改成 `item_id`（空串 = **清空**挂载）。

    只为 §31.5.2 的边界用例服务：生成链路的商品固定是 `ITEM_ID`，要造「不同商品 /
    无挂载」这两种形态只能改**两处**（门禁两处都读）：`note_goods` 关系行 + `raw_json.item_id`。
    """
    db = db_factory()
    try:
        note = db.get(Note, note_pk)
        raw = note.raw_json if isinstance(note.raw_json, dict) else json.loads(note.raw_json or "{}")
        raw = dict(raw or {})
        if item_id:
            raw["item_id"] = item_id
        else:
            raw.pop("item_id", None)
        note.raw_json = raw
        for row in db.scalars(
            select(NoteGoods).where(
                NoteGoods.user_id == note.user_id, NoteGoods.note_id == note.note_id
            )
        ).all():
            db.delete(row)
        if item_id:
            db.add(NoteGoods(user_id=note.user_id, note_id=note.note_id, item_id=item_id))
        db.commit()
    finally:
        db.close()


def _temp_dir(note_id: str) -> Path:
    """测试用的临时目录（**与 service 同一把尺子**：读它的 `get_settings()`）。

    ⚠️ 2026-09-25 修：旧写法直接读真实 `get_settings().storage_dir` ⇒ 测试往
    `backend/app/storage/tmp_note_publish/` 真写文件，每跑一轮回归留几个目录
    （真机积压 184 个的伴生问题）。现在 `_patch_async` 把 service 的 settings
    指到用例 tmp_path，这里读同一份 ⇒ 断言对象与实现同源，且不落真实盘。
    """
    return Path(note_publish_service.get_settings().storage_dir) / "tmp_note_publish" / note_id


class _FakeResponse:
    status_code = 200
    content = b"\xff\xd8\xff\xe0" + b"0" * 2048  # 够 100 字节下限
    headers = {"content-type": "image/jpeg"}

    def raise_for_status(self):
        return None


class _FakeSettings:
    """只带 `storage_dir` 的设置替身（service 只用这一个字段）。"""

    def __init__(self, storage_dir: Path) -> None:
        self.storage_dir = storage_dir


def _patch_downloads(monkeypatch, *, fail_on_second: bool = False) -> None:
    """把媒体下载打到本地假字节（不打真 CDN）。

    `fail_on_second=True` 时第二张抛 `RequestException` —— 制造「下载中途失败」，
    验证临时目录被删（用户 2026-09-25 口径）。
    """
    import requests

    state = {"n": 0}

    def _fake_get(*args, **kwargs):
        state["n"] += 1
        if fail_on_second and state["n"] == 2:
            raise requests.RequestException("CDN 429（测试）")
        return _FakeResponse()

    monkeypatch.setattr(requests, "get", _fake_get)


def _patch_async(monkeypatch, db_factory, tmp_path) -> None:
    """异步测试四件套：worker 用测试库的 session、节流置 0（不真等）、下载打桩、
    临时目录指到用例 tmp_path（不污染真实 storage）。"""
    monkeypatch.setattr(note_publish_service, "_session_factory", db_factory)
    monkeypatch.setattr(note_publish_service, "_download_gap_seconds", lambda: 0.0)
    monkeypatch.setattr(note_publish_service, "get_settings", lambda: _FakeSettings(Path(tmp_path)))
    _patch_downloads(monkeypatch)


def test_publish_success_runs_full_chain(tmp_path, monkeypatch):
    """发布异步全链：启动即返回 publishing，worker 跑完四步 + 终态落库。

    历史缺陷回归（2026-09-25）：成功删除的是**本地 note_id** 目录 —— 旧实现
    先 `note.note_id = 平台 ID` 再删，删的是从未创建的平台 ID 目录，每次成功
    发布都漏一个本地目录（真机曾积压 184 个）。
    """
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        note_id = _generate_one(db_factory, account_id)
        FakePublishAdapter.calls = []  # 只看发布阶段的调用序
        _patch_async(monkeypatch, db_factory, tmp_path)
        note_pk = _note_pk(db_factory, note_id)

        db = db_factory()
        try:
            started = note_publish_service.start_publish(
                db, 1, note_pk, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
            )
            assert started.publish_status == "publishing"  # 同步段即落库
        finally:
            db.close()

        assert note_publish_service.wait_publish_done(note_pk, timeout=15), "后台 worker 未在超时内跑完"

        # 调用序：上传凭证 → 逐文件上传 → 预审核 → 发布 → 回查商品笔记列表
        # （发布成功后补详情地址，§25.4；默认查无此项 ⇒ raw 不动）
        assert FakePublishAdapter.calls[0] == "upload_permit"
        assert FakePublishAdapter.calls[1].startswith("upload_file:")
        assert "preaudit" in FakePublishAdapter.calls
        assert FakePublishAdapter.calls[-2:] == ["publish", "goods_note_page"]

        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.id == note_pk))
            assert note.note_id == "REAL-NOTE-1"  # 平台 ID 回写
            assert note.publish_status == "published"
            assert note.published is True
            assert note.publish_error == ""
            assert note.published_at is not None
            assert note.publish_attempts == 1
            assets = db.scalars(select(NoteAsset).where(NoteAsset.note_id == note.id)).all()
            assert all(not a.local_path for a in assets)
            # 临时目录按**本地** ID 删（历史缺陷回归）
            assert not _temp_dir(note_id).exists()
        finally:
            db.close()
    finally:
        _teardown()


def test_publish_success_backfills_detail_ref_into_raw_json(tmp_path, monkeypatch):
    """发布成功即把**详情地址**补进 `raw_json`（§25.4，2026-09-25 用户报障回归）。

    用户报障原文：「本地生成的笔记已经发布成功的笔记，点击同步按钮『中古风桌旗，
    让家瞬间高级感』同步未完成：这条笔记没有可用的详情地址」。

    根因：平台 `note/publish` 响应只回 `{"id"}`，**不带** `note_url` /
    `xsec_token` ⇒ 本地生成笔记的 `raw_json` 从来没有详情地址 ⇒ 同步按钮的
    `_note_detail_url` 判 `no_url`（按钮在、功能不在）。

    修法：发布成功回写事务里 best-effort 回查一次商品笔记列表
    （`note_ids` 精确查询，与 `shop_goods_service.sync_note_ids` 同一端点），
    把带 token 的 explore URL 合入 `raw_json`。之后**所有**链路（单条同步 /
    批量同步 / 定时补齐）都拿到完整地址，不再依赖无 token 兜底。
    """
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        note_id = _generate_one(db_factory, account_id)
        FakePublishAdapter.calls = []
        FakePublishAdapter.goods_note_entries = [
            {"note_id": "REAL-NOTE-1", "xsec_token": "XT-1", "item_id": ITEM_ID}
        ]
        _patch_async(monkeypatch, db_factory, tmp_path)
        note_pk = _note_pk(db_factory, note_id)

        db = db_factory()
        try:
            started = note_publish_service.start_publish(
                db, 1, note_pk, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
            )
            assert started.publish_status == "publishing"
        finally:
            db.close()

        assert note_publish_service.wait_publish_done(note_pk, timeout=15), "后台 worker 未跑完"

        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.id == note_pk))
            assert note.publish_status == "published"
            assert note.note_id == "REAL-NOTE-1"
            raw = note.raw_json if isinstance(note.raw_json, dict) else json.loads(note.raw_json)
            assert raw["note_url"] == (
                "https://www.xiaohongshu.com/explore/REAL-NOTE-1?xsec_token=XT-1"
            )
            assert raw["xsec_token"] == "XT-1"
            assert raw["item_id"] == ITEM_ID, "生成时落的字段不被抹掉（只增不写）"
            assert FakePublishAdapter.calls[-1] == "goods_note_page"
        finally:
            db.close()
    finally:
        _teardown()


def test_publish_success_when_detail_ref_lookup_finds_nothing(tmp_path, monkeypatch):
    """回查查无该项（非商品笔记 / 平台延迟）⇒ 发布照样成功，raw 保持原样。

    best-effort 的边界：补地址失败**不能**把已成功的发布拖成失败；拿不到 token
    的笔记靠单条同步路径的 explore 兜底（`allow_tokenless`）继续可同步。
    """
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        note_id = _generate_one(db_factory, account_id)
        FakePublishAdapter.calls = []
        FakePublishAdapter.goods_note_entries = []  # 平台查无此项
        _patch_async(monkeypatch, db_factory, tmp_path)
        note_pk = _note_pk(db_factory, note_id)

        db = db_factory()
        try:
            started = note_publish_service.start_publish(
                db, 1, note_pk, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
            )
            assert started.publish_status == "publishing"
        finally:
            db.close()

        assert note_publish_service.wait_publish_done(note_pk, timeout=15), "后台 worker 未跑完"

        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.id == note_pk))
            assert note.publish_status == "published"
            assert note.publish_error == ""
            raw = note.raw_json if isinstance(note.raw_json, dict) else json.loads(note.raw_json)
            assert "note_url" not in raw, "查不到就不写，不造假（缺值口径）"
            assert FakePublishAdapter.calls[-1] == "goods_note_page", "回查仍然发生过"
        finally:
            db.close()
    finally:
        _teardown()


def test_full_chain_generation_to_publish_carries_topics(tmp_path, monkeypatch):
    """**生成 → 发布全链路**：话题一路带到平台发布体（2026-09-25 新增）。

    用户报障原文：「发布成功的笔记均没有话题，只有正文图片」。本用例把整条链跑
    一遍并**逐段断言话题的实际形态**（不是只测组装层）：

    1. **生成**：AI 正文自带话题（平台形态 `#桌布[话题]#` + 老形态 `#复古风`）+
       平台 topics（带 ID）⇒ 正文拆净、话题落 `topics` / `note_topics`、名字不带
       `[话题]` 标记、`platform_id` 落库；
    2. **发布体**：`desc` 末尾是平台形态 `#名字[话题]#`（含没 ID 的）；`hash_tag`
       带平台 ID；
    3. **平台实收**：假适配器捕获的 body 与 2 一致 —— 防「组装层对、发出去又变」。
    """

    def _task_result(self, task_id):
        """AI 结果里放两种话题形态（平台形态 + 老形态），并带两个平台话题 ID。"""
        FakePublishAdapter.calls.append("aigc_task_result")
        detail = {
            "success": True,
            "title": "测试桌布真好用",
            "content": "这是一段用于测试的正文。#桌布[话题]# 复古风。#复古风",
            "topics": [{"id": "T1", "name": "桌布"}, {"id": "T2", "name": "桌垫"}],
        }
        return True, "ok", {
            "code": 0,
            "data": {"task_status": "COMPLETED", "task_detail": json.dumps(detail)},
        }

    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        _patch_async(monkeypatch, db_factory, tmp_path)
        monkeypatch.setattr(FakePublishAdapter, "aigc_task_result", _task_result)

        # ---- 1) 生成落库 ----
        note_id = _generate_one(db_factory, account_id)
        note_pk = _note_pk(db_factory, note_id)
        db = db_factory()
        try:
            note = db.get(Note, note_pk)
            assert "#" not in (note.content or ""), f"正文没拆净：{note.content!r}"
            assert "[话题]" not in (note.content or "")
            rows = db.execute(
                select(Topic.name, Topic.platform_id)
                .join(NoteTopic, NoteTopic.topic_id == Topic.id)
                .where(NoteTopic.note_id == note.id)
                .order_by(NoteTopic.id)
            ).all()
            assert [name for name, _ in rows] == ["桌布", "桌垫", "复古风"]
            assert all("[话题" not in name for name, _ in rows), f"名字带标记：{rows}"
            assert {name: pid for name, pid in rows}["桌布"] == "T1"
        finally:
            db.close()

        # ---- 2/3) 发布并捕获平台真正收到的 body ----
        bodies: list[dict] = []
        original_publish = FakePublishAdapter.publish_note

        def _capture(self, body):
            bodies.append(json.loads(json.dumps(body, ensure_ascii=False)))
            return original_publish(self, body)

        monkeypatch.setattr(FakePublishAdapter, "publish_note", _capture)

        db = db_factory()
        try:
            note_publish_service.start_publish(
                db, 1, note_pk, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
            )
        finally:
            db.close()
        assert note_publish_service.wait_publish_done(note_pk, timeout=15), "后台 worker 未跑完"

        assert len(bodies) == 1, "发布体应只发一次"
        common = bodies[0]["common"]
        desc = common["desc"]
        for name in ("桌布", "桌垫", "复古风"):
            assert f"#{name}[话题]#" in desc, f"desc 缺平台形态话题：{desc!r}"
        assert desc.count("#桌布[话题]#") == 1, f"话题重复拼接：{desc!r}"
        # hash_tag 是并行的结构化参数：只收带平台 ID 的（桌布 T1 / 桌垫 T2）
        assert common["hash_tag"] == [
            {"id": "T1", "name": "桌布", "type": "topic"},
            {"id": "T2", "name": "桌垫", "type": "topic"},
        ]

        db = db_factory()
        try:
            assert db.get(Note, note_pk).publish_status == "published"
        finally:
            db.close()
    finally:
        _teardown()


def test_publish_start_returns_immediately_and_rejects_double_start(tmp_path, monkeypatch):
    """异步启动语义：毫秒级返回 publishing + 落库；发布中再启动 ⇒ 拒绝（不双发）。

    闸门把链路停在「下载完成、上传未开始」的确定态，用来断言同步段效果
    （不等链路）与临时文件名（商品 ID + 序号）。
    """
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        note_id = _generate_one(db_factory, account_id)
        FakePublishAdapter.calls = []
        _patch_async(monkeypatch, db_factory, tmp_path)
        note_pk = _note_pk(db_factory, note_id)

        gate = threading.Event()
        reached = threading.Event()
        FakePublishAdapter.upload_gate = gate
        FakePublishAdapter.upload_reached = reached

        db = db_factory()
        try:
            note_publish_service.start_publish(
                db, 1, note_pk, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
            )
        finally:
            db.close()

        # 同步段已落库（不等后台）：publishing + attempts=1
        assert reached.wait(timeout=10), "worker 没在超时内到达上传（闸门失效？）"
        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.id == note_pk))
            assert note.publish_status == "publishing"
            assert note.publish_attempts == 1
            assert note.note_id == note_id  # 平台 ID 没污染本地 ID
        finally:
            db.close()

        # 下载已完成：文件名 = 商品 ID + 序号（用户 2026-09-25 口径）
        names = sorted(p.name for p in _temp_dir(note_id).iterdir())
        assert names == [f"{ITEM_ID}_001.jpg", f"{ITEM_ID}_002.jpg", f"{ITEM_ID}_003.jpg"]

        # 双发被拒：publishing 不在可发布集合；attempts 不增、不起第二个 worker
        db = db_factory()
        try:
            with pytest.raises(note_publish_service.NotePublishError) as exc:
                note_publish_service.start_publish(
                    db, 1, note_pk, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
                )
            assert exc.value.code == "NOTE_NOT_PUBLISHABLE"
        finally:
            db.close()

        # 放行 → 后台跑完 → 终态 + 删目录
        gate.set()
        assert note_publish_service.wait_publish_done(note_pk, timeout=15)
        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.id == note_pk))
            assert note.publish_status == "published"
            assert note.note_id == "REAL-NOTE-1"
            assert note.publish_attempts == 1  # 双发被拒，没有多加
        finally:
            db.close()
        assert not _temp_dir(note_id).exists()
    finally:
        FakePublishAdapter.upload_gate = None
        FakePublishAdapter.upload_reached = None
        _teardown()


def test_publish_failure_records_reason_and_keeps_temp(tmp_path, monkeypatch):
    """发布失败留现场 + 可重发：平台拒发 ⇒ failed + 原文 + 临时目录保留（本地 ID 键）。"""
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        note_id = _generate_one(db_factory, account_id)
        FakePublishAdapter.fail_publish = True
        FakePublishAdapter.calls = []
        _patch_async(monkeypatch, db_factory, tmp_path)
        note_pk = _note_pk(db_factory, note_id)

        db = db_factory()
        try:
            note_publish_service.start_publish(
                db, 1, note_pk, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
            )
        finally:
            db.close()
        assert note_publish_service.wait_publish_done(note_pk, timeout=15)

        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.id == note_pk))
            assert note.publish_status == "failed"
            assert note.publish_error == "平台发布被拒（测试）"
            assert note.note_id == note_id  # 平台 ID 没污染本地 ID
            # 上传/发布阶段失败：临时目录**保留**（现场可查；§31.3.5）
            assert _temp_dir(note_id).exists()
        finally:
            db.close()

        # 失败后可重发：去掉开关再发一次 → 成功（全链重走）
        FakePublishAdapter.fail_publish = False
        db = db_factory()
        try:
            note_publish_service.start_publish(
                db, 1, note_pk, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
            )
        finally:
            db.close()
        assert note_publish_service.wait_publish_done(note_pk, timeout=15)
        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.id == note_pk))
            assert note.publish_status == "published"
            assert note.publish_attempts == 2
        finally:
            db.close()
        assert not _temp_dir(note_id).exists()
    finally:
        _teardown()


def test_download_failure_deletes_temp_dir(tmp_path, monkeypatch):
    """下载中途失败 ⇒ PUBLISH_UPSTREAM_ERROR + **删本次临时目录**（不留半截文件）。"""
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        note_id = _generate_one(db_factory, account_id)
        FakePublishAdapter.calls = []
        _patch_async(monkeypatch, db_factory, tmp_path)
        _patch_downloads(monkeypatch, fail_on_second=True)  # 第 2 张打挂
        note_pk = _note_pk(db_factory, note_id)

        db = db_factory()
        try:
            note_publish_service.start_publish(
                db, 1, note_pk, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
            )
        finally:
            db.close()
        assert note_publish_service.wait_publish_done(note_pk, timeout=15)

        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.id == note_pk))
            assert note.publish_status == "failed"
            assert "下载媒体失败（第 2 个）" in note.publish_error
            assert "RequestException" in note.publish_error  # 网络异常带类型（DEF-058）
        finally:
            db.close()
        # 半截目录被删（重发是全量重下，留着只会堆积）
        assert not _temp_dir(note_id).exists()
        # 下载就挂了：上传/发布一步都不该走
        assert FakePublishAdapter.calls == []
    finally:
        _teardown()


def test_download_gap_is_random_one_to_three_seconds(tmp_path, monkeypatch):
    """下载节流：张间随机 1–3s（首张不停）；测试内置 0 不真等。"""
    # ① 真实函数采样：边界就是契约（改了上限/下限这条红）
    samples = [note_publish_service._download_gap_seconds() for _ in range(50)]
    assert all(1.0 <= s <= 3.0 for s in samples), f"节流越界：{min(samples)}~{max(samples)}"
    assert len(set(samples)) > 1, "间隔是定值 —— 随机才有意义（固定值本身是机器节奏特征）"

    # ② 循环行为：3 张图 ⇒ 张间调 2 次（首张前不调）
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        note_id = _generate_one(db_factory, account_id)
        FakePublishAdapter.calls = []
        _patch_downloads(monkeypatch)
        monkeypatch.setattr(note_publish_service, "_session_factory", db_factory)
        monkeypatch.setattr(note_publish_service, "get_settings", lambda: _FakeSettings(Path(tmp_path)))
        gaps: list[float] = []
        monkeypatch.setattr(
            note_publish_service, "_download_gap_seconds", lambda: gaps.append(1.5) or 1.5
        )
        note_pk = _note_pk(db_factory, note_id)

        db = db_factory()
        try:
            note_publish_service.start_publish(
                db, 1, note_pk, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
            )
        finally:
            db.close()
        assert note_publish_service.wait_publish_done(note_pk, timeout=15)
        assert len(gaps) == 2, f"3 张图应有 2 次张间停顿，实际 {len(gaps)}"
    finally:
        _teardown()


def test_publish_queue_runs_in_order_with_snapshot(tmp_path, monkeypatch):
    """批量入队按 FIFO 跑完；进度快照（running/queued/四态计数）正确。

    同时覆盖 `GET /notes/publish-queue` 端点（进度卡数据源）与「路由必须注册在
    `/{note_id}` 之前」—— 否则 "publish-queue" 被 int 转换吃掉变 422。
    """
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        # 本用例测的是**队列 FIFO 顺序**（三篇同商品）⇒ 先开重复发布开关，
        # 否则第 2 篇起被 §31.5.2 门禁拦下（那批行为另有用例覆盖）。
        _enable_duplicate_goods_publish(db_factory)
        note_ids = [_generate_one(db_factory, account_id) for _ in range(3)]
        FakePublishAdapter.calls = []
        FakePublishAdapter.published_titles = []
        _patch_async(monkeypatch, db_factory, tmp_path)
        # 三篇不同标题 ⇒ 用发布体标题断言执行顺序（假适配器内容相同，只能靠这个区分）
        pks = []
        db = db_factory()
        try:
            for index, note_id in enumerate(note_ids):
                note = db.scalar(select(Note).where(Note.note_id == note_id))
                note.title = f"队列笔记{index + 1}"
                pks.append(note.id)
            db.commit()
        finally:
            db.close()

        # 闸门拦住第一篇的第一个上传 ⇒ 另两篇必然还在队列里
        gate = threading.Event()
        reached = threading.Event()
        FakePublishAdapter.upload_gate = gate
        FakePublishAdapter.upload_reached = reached

        resp = client.post(
            "/api/notes/batch-publish",
            json={"note_ids": pks},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 3 and body["started"] == 3 and body["failed"] == 0
        assert [item["status"] for item in body["results"]] == ["publishing"] * 3

        assert reached.wait(timeout=10)
        snap = client.get("/api/notes/publish-queue", headers=headers).json()
        assert snap["running"] is not None and snap["running"]["note_pk"] == pks[0]
        assert [q["note_pk"] for q in snap["queued"]] == pks[1:]
        assert snap["counts"]["publishing"] == 3  # 四态原始计数（不归并待发布）
        # 2026-09-26 常驻化 + 当日口径：快照 = 队列实时态（running/queued/total）
        # + 四态计数 + **当日三数**（`today`，进度卡卡面数字的来源）。
        # 原句 `assert set(snap) == {"running", "queued", "total", "counts"}`
        # **被 2026-09-26 当日口径取代**（保留备查）。
        assert set(snap) == {"running", "queued", "total", "counts", "today"}
        assert snap["today"]["pending"] == 3, "当日待处理 = 未终态数（此刻三篇都在队列里）"
        assert snap["total"] == 3, "total = 在跑 + 排队（+ 近期终态，此时还没有）"

        gate.set()
        for pk in pks:
            assert note_publish_service.wait_publish_done(pk, timeout=20), f"note_pk={pk} 未跑完"

        assert FakePublishAdapter.published_titles == ["队列笔记1", "队列笔记2", "队列笔记3"]
        db = db_factory()
        try:
            for pk in pks:
                note = db.scalar(select(Note).where(Note.id == pk))
                assert note.publish_status == "published"
        finally:
            db.close()
        # 队列排空：快照回空闲
        snap = client.get("/api/notes/publish-queue", headers=headers).json()
        assert snap["running"] is None and snap["queued"] == []
        assert snap["counts"]["published"] == 3
        # 2026-09-26 当日口径：三篇今天跑完 ⇒ 当日成功 = 3、待处理 = 0（卡面三数
        # 就是它；「本页累计」那一版已被取代，见模块内其它注释）。
        assert set(snap) == {"running", "queued", "total", "counts", "today"}
        assert snap["today"]["ok"] == 3 and snap["today"]["pending"] == 0
        assert snap["total"] == 3
    finally:
        FakePublishAdapter.upload_gate = None
        FakePublishAdapter.upload_reached = None
        _teardown()


def _wait_publish_idle(timeout: float = 5.0) -> None:
    """等发布队列真正空闲（库里没有未终态的发布项）。

    持久化后（§35.9）「空闲」的判据落在**库**上：没有 `pending` / `running` 的
    `xhs_note_publish_item` 行 = worker 手上没事了，下一批入队必然新开一批
    （`ensure_open_batch` 的判据正是「该批还有没有未终态子行」）。改造前靠等
    内存位 `_publish_running` 收回 None —— 那个位已经不存在了（队列真相源是库）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db = note_publish_service._session_factory()
        try:
            if note_publish_service.count_open_publish_items(db) == 0:
                return
        finally:
            db.close()
        time.sleep(0.02)


def test_publish_queue_counts_failures_and_restarts_when_idle(tmp_path, monkeypatch):
    """**当日口径**：失败计入 `today.failed`；空闲后重新入队 ⇒ 当日窗口继续累加。

    2026-09-25 用户口径「发布、同步进度卡做类似的调整」+「待同步数为 0 时，不再
    刷新统计数据」；2026-09-26 用户口径**改口径**「只统计当日发布或同步的数据……
    统计队列记录表中的数据」⇒ 卡面三数不再是「本轮记账」（那是页面自己累计，
    刷新页面就丢），而是**当日 `tasks` 行的现算值**：跑过的篇留在当日窗口里，
    所以再入队一批时上一批的成败**继续算在今天**（不再被「新一轮」清掉）。
    """
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        # 两篇同商品、分两批发布 ⇒ 先开重复发布开关（否则第 2 篇被 §31.5.2 门禁拦下，
        # 本用例要观察的是「平台拒发 ⇒ 当日 failed」那条路径）。
        _enable_duplicate_goods_publish(db_factory)
        note_ids = [_generate_one(db_factory, account_id) for _ in range(2)]
        _patch_async(monkeypatch, db_factory, tmp_path)
        pks = []
        db = db_factory()
        try:
            for note_id in note_ids:
                note = db.scalar(select(Note).where(Note.note_id == note_id))
                pks.append(note.id)
        finally:
            db.close()

        # 第一批：1 篇，成功
        FakePublishAdapter.fail_publish = False
        resp = client.post("/api/notes/batch-publish", json={"note_ids": [pks[0]]}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert note_publish_service.wait_publish_done(pks[0], timeout=20)
        _wait_publish_idle()
        snap = client.get("/api/notes/publish-queue", headers=headers).json()
        # 2026-09-26 当日口径：终态项留在**当日窗口**里 ⇒ today.ok=1；
        # 队列实时态为空（running/queued 空，total 仍是近期终态数）。
        assert set(snap) == {"running", "queued", "total", "counts", "today"}
        assert snap["running"] is None and snap["queued"] == []
        assert snap["total"] == 1, "终态项留在近期窗口（不是「新一轮」把它清掉）"
        assert snap["counts"]["published"] == 1
        assert snap["today"]["ok"] == 1 and snap["today"]["pending"] == 0

        # 第二批：1 篇，被平台拒 ⇒ 笔记行 failed；**上一批的成功仍在当日窗口里**
        FakePublishAdapter.fail_publish = True
        resp = client.post("/api/notes/batch-publish", json={"note_ids": [pks[1]]}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert note_publish_service.wait_publish_done(pks[1], timeout=20)
        snap = client.get("/api/notes/publish-queue", headers=headers).json()
        assert snap["running"] is None and snap["queued"] == []
        assert snap["total"] == 2, "两批的终态项都在近期窗口里"
        assert snap["counts"]["published"] == 1 and snap["counts"]["failed"] == 1
        assert snap["today"]["ok"] == 1 and snap["today"]["failed"] == 1, (
            "当日三数跨批累加（同一天里上一批的成败不清零）"
        )
    finally:
        FakePublishAdapter.fail_publish = False
        _teardown()


def test_publish_gates(tmp_path, monkeypatch):
    """门禁四态：不存在 / 无媒体 / 已发布 / 发布中 —— 全部同步拒绝（400 或抛错）。"""
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        note_id = _generate_one(db_factory, account_id)
        _patch_async(monkeypatch, db_factory, tmp_path)

        db = db_factory()
        try:
            # 不存在 ⇒ 抛 NOTE_NOT_FOUND
            with pytest.raises(note_publish_service.NotePublishError) as exc:
                note_publish_service.start_publish(
                    db, 1, 999999, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
                )
            assert exc.value.code == "NOTE_NOT_FOUND"

            note = db.scalar(select(Note).where(Note.note_id == note_id))
            note_pk = note.id
            # 无媒体 ⇒ 同步门禁拒绝（§31.5 步 0：不进队列、不起 worker）
            db.query(NoteAsset).filter(NoteAsset.note_id == note.id).delete()
            db.commit()
            with pytest.raises(note_publish_service.NotePublishError) as exc:
                note_publish_service.start_publish(
                    db, 1, note_pk, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
                )
            assert exc.value.code == "NOTE_MEDIA_EMPTY"
            db.refresh(note)
            assert note.publish_status == "pending"  # 门禁拒绝不动状态机
        finally:
            db.close()

        # 端点层同一道闸：无媒体 ⇒ 400 结构化错误（文档口径，§31.5 步 0）
        resp = client.post(f"/api/notes/{note_pk}/publish", headers=headers)
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "NOTE_MEDIA_EMPTY"
    finally:
        _teardown()


def test_publish_already_published_is_rejected(tmp_path, monkeypatch):
    """已发布 ⇒ NOTE_ALREADY_PUBLISHED（同步门禁，不起 worker）。"""
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        note_id = _generate_one(db_factory, account_id)
        _patch_async(monkeypatch, db_factory, tmp_path)
        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.note_id == note_id))
            note_pk = note.id
            from backend.app.models.note import set_note_publish_status

            set_note_publish_status(note, "published")
            db.commit()
            with pytest.raises(note_publish_service.NotePublishError) as exc:
                note_publish_service.start_publish(
                    db, 1, note_pk, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
                )
            assert exc.value.code == "NOTE_ALREADY_PUBLISHED"
        finally:
            db.close()
    finally:
        _teardown()


# --------------------------------------------------------------------------- #
# 商品重复发布门禁（设计 13 §31.5.2，2026-09-28 用户口径）
#
# 用户口径原文：「新增一个配置开关，允许商品笔记重复发布**默认关闭**……点击发布和批量
# 发布笔记时通过开关校验，该商品是否已经发布过笔记，如果已经发布过笔记不再发布，该笔记
# 设置为**发布失败**，原因**挂载的商品已经发布过笔记**。」
# --------------------------------------------------------------------------- #
def test_publish_goods_duplicate_guard_blocks_and_marks_failed(tmp_path, monkeypatch):
    """默认关：同一商品已有**已发布**笔记 ⇒ 不再发布，本篇落 `failed` + 原因，零平台请求。"""
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        _enable_duplicate_goods_publish(db_factory)  # 先把第 1 篇正常发出去（占用 ITEM-1）
        first = _generate_one(db_factory, account_id)
        _patch_async(monkeypatch, db_factory, tmp_path)
        pk1 = _note_pk(db_factory, first)
        assert client.post(f"/api/notes/{pk1}/publish", headers=headers).status_code == 200
        assert note_publish_service.wait_publish_done(pk1, timeout=20)

        _set_duplicate_goods_publish(db_factory, False)  # 回到默认态（关）
        second = _generate_one(db_factory, account_id)
        pk2 = _note_pk(db_factory, second)
        calls_before = list(FakePublishAdapter.calls)

        resp = client.post(f"/api/notes/{pk2}/publish", headers=headers)
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "GOODS_ALREADY_PUBLISHED"
        assert detail["message"] == "挂载的商品已经发布过笔记"

        db = db_factory()
        try:
            blocked = db.get(Note, pk2)
            # 用户口径「该笔记设置为发布失败，原因挂载的商品已经发布过笔记」
            assert blocked.publish_status == "failed"
            assert blocked.publish_error == "挂载的商品已经发布过笔记"
            assert blocked.published is False
            # 没进发布链路 ⇒ 不算一次尝试（`publish_attempts` 记的是「真的跑了几次」）
            assert (blocked.publish_attempts or 0) == 0
            # 也没入队：队列里没有未终态项
            assert note_publish_service.count_open_publish_items(db) == 0
        finally:
            db.close()
        assert FakePublishAdapter.calls == calls_before, "命中门禁后不许有任何平台请求"
    finally:
        _teardown()


def test_publish_goods_duplicate_guard_allows_when_switch_on(tmp_path, monkeypatch):
    """开关**打开** ⇒ 不再校验，同一商品可以连续发布多篇（与默认关的行为成对照）。"""
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        _enable_duplicate_goods_publish(db_factory)
        note_ids = [_generate_one(db_factory, account_id) for _ in range(2)]
        _patch_async(monkeypatch, db_factory, tmp_path)
        pks = [_note_pk(db_factory, note_id) for note_id in note_ids]
        for pk in pks:
            assert client.post(f"/api/notes/{pk}/publish", headers=headers).status_code == 200
        for pk in pks:
            assert note_publish_service.wait_publish_done(pk, timeout=20)
        db = db_factory()
        try:
            statuses = [db.get(Note, pk).publish_status for pk in pks]
        finally:
            db.close()
        assert statuses == ["published", "published"], "开关打开 ⇒ 同商品两篇都发得出去"
    finally:
        _teardown()


def test_publish_goods_duplicate_guard_blocks_second_in_batch(tmp_path, monkeypatch):
    """批量同批两篇同商品 ⇒ 第 1 篇入队、第 2 篇被拦（**判据含 `publishing`**）。

    ⚠️ 这条盯的正是判据表里的那格：第 1 篇此刻**还没** `published`（刚置 `publishing`）。
    只认 `published` 就会把第 2 篇也放行 —— 批量勾选同商品多篇时用户「不重复发布」
    的诉求直接落空。
    """
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        note_ids = [_generate_one(db_factory, account_id) for _ in range(2)]
        _patch_async(monkeypatch, db_factory, tmp_path)
        pks = [_note_pk(db_factory, note_id) for note_id in note_ids]

        resp = client.post("/api/notes/batch-publish", json={"note_ids": pks}, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 2 and body["started"] == 1 and body["failed"] == 1
        assert body["results"][0]["ok"] is True
        blocked = body["results"][1]
        assert blocked["ok"] is False
        assert blocked["code"] == "GOODS_ALREADY_PUBLISHED"
        assert blocked["error"] == "挂载的商品已经发布过笔记"

        db = db_factory()
        try:
            second = db.get(Note, pks[1])
            assert second.publish_status == "failed"
            assert second.publish_error == "挂载的商品已经发布过笔记"
        finally:
            db.close()
        assert note_publish_service.wait_publish_done(pks[0], timeout=20)
    finally:
        _teardown()


def test_publish_goods_duplicate_guard_skips_unrelated_or_empty_goods(tmp_path, monkeypatch):
    """边界：**不同商品**与**未挂载商品**的笔记都不受门禁影响。

    判据是「**该**商品是否已有笔记」，不是「有没有任何笔记发布过」：换成别的商品
    （关系行 + `raw_json.item_id` 都改掉）或干脆没有挂载 ⇒ 放行（缺值不造假，硬约束⑤）。
    """
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        _enable_duplicate_goods_publish(db_factory)
        note_ids = [_generate_one(db_factory, account_id) for _ in range(3)]
        _patch_async(monkeypatch, db_factory, tmp_path)
        pks = [_note_pk(db_factory, note_id) for note_id in note_ids]

        # 第 1 篇正常发出去 ⇒ ITEM-1 被占用
        assert client.post(f"/api/notes/{pks[0]}/publish", headers=headers).status_code == 200
        assert note_publish_service.wait_publish_done(pks[0], timeout=20)
        _set_duplicate_goods_publish(db_factory, False)

        # 第 2 篇改挂 ITEM-2（**不同商品**）⇒ 不受 ITEM-1 的占用影响
        _rewrite_note_item_id(db_factory, pks[1], "ITEM-2")
        assert client.post(f"/api/notes/{pks[1]}/publish", headers=headers).status_code == 200
        assert note_publish_service.wait_publish_done(pks[1], timeout=20)
        db = db_factory()
        try:
            assert db.get(Note, pks[1]).publish_status == "published"
        finally:
            db.close()

        # 第 3 篇**清空挂载** ⇒ 没有「重复」可言 ⇒ 放行
        _rewrite_note_item_id(db_factory, pks[2], "")
        assert client.post(f"/api/notes/{pks[2]}/publish", headers=headers).status_code == 200
        assert note_publish_service.wait_publish_done(pks[2], timeout=20)
        db = db_factory()
        try:
            assert db.get(Note, pks[2]).publish_status == "published"
        finally:
            db.close()
    finally:
        _teardown()


def test_publish_preaudit_rejection(tmp_path, monkeypatch):
    """预审核拒绝：平台原文落 publish_error，不调 publish。"""
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        note_id = _generate_one(db_factory, account_id)
        FakePublishAdapter.reject_preaudit = True
        FakePublishAdapter.calls = []
        _patch_async(monkeypatch, db_factory, tmp_path)
        note_pk = _note_pk(db_factory, note_id)

        db = db_factory()
        try:
            note_publish_service.start_publish(
                db, 1, note_pk, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
            )
        finally:
            db.close()
        assert note_publish_service.wait_publish_done(note_pk, timeout=15)

        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.id == note_pk))
            assert note.publish_status == "failed"
            assert "正文含违规词（平台审的）" in note.publish_error
        finally:
            db.close()
        assert "publish" not in FakePublishAdapter.calls  # 预审核拒了就不发布
    finally:
        _teardown()


def test_item_notes_reads_relation_table(tmp_path):
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        note_id = _generate_one(db_factory, account_id)
        resp = client.post(
            "/api/xhs/qianfan/item-notes",
            json={"platform_account_id": account_id, "item_id": ITEM_ID},
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        note = body["notes"][0]
        assert note["note_id"] == note_id
        assert note["note_type"] == "local_generated"
        assert note["publish_status"] == "pending"
        assert note["title"] == "测试桌布真好用"
        assert len(note["images"]) == 3
    finally:
        _teardown()


# --------------------------------------------------------------------------- #
# 媒体上限 / 「生成不发布」/ 状态投影（设计 13 §32）
# --------------------------------------------------------------------------- #

#: 发布侧的调用名（生成链路必须**一个都没有**，见 §32.2.2）。
_PUBLISH_SIDE_CALLS = ("upload_permit", "preaudit", "publish")


def _publish_side_calls() -> list[str]:
    """本次链路里出现过的发布侧调用（含逐文件上传）。"""
    return [
        call
        for call in FakePublishAdapter.calls
        if call in _PUBLISH_SIDE_CALLS or call.startswith("upload_file:")
    ]


def test_generate_caps_images_at_six_and_never_publishes(tmp_path, monkeypatch):
    """媒体上限 6 张（用户口径「图片最多 6 张」）+ **生成不发布**（§32.2）。

    商品有 10 张图 ⇒ 只落 6 张；整条生成链路里发布侧调用**一次都没有**
    （上传凭证 / 上传 / 预审核 / 发布）—— 只有内容库点「发布」才走发布链路。
    §31.10 起生成走队列：断言对象从响应体搬到 worker 跑完后的库 / 队列项。
    """
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id, image_count=10)
        _patch_gen_worker(monkeypatch, db_factory)
        resp = client.post(
            "/api/xhs/product-notes/generate-save",
            json={"platform_account_id": account_id, "item_id": ITEM_ID},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "queued"
        assert product_note_gen_service.wait_generate_done(timeout=15), "后台 worker 未跑完"

        db = db_factory()
        try:
            item = db.scalar(select(Task).where(Task.task_type == "xhs_note_gen_item"))
            payload = item.payload if isinstance(item.payload, dict) else {}
            note = db.scalar(select(Note).where(Note.note_id == payload["note_id"]))
            assert note.publish_status == "pending"  # 待发布
            assert note.published is False  # 投影同步
            assets = db.scalars(select(NoteAsset).where(NoteAsset.note_id == note.id)).all()
            assert len(assets) == 6  # 10 张图里只落 6 张
            assert all(asset.asset_type == "image" for asset in assets)
        finally:
            db.close()

        # 守卫：生成链路不许碰发布链路（谁把发布接进来，这条立刻红）
        assert _publish_side_calls() == []
    finally:
        _teardown()


def test_generate_prefers_video_over_images(tmp_path, monkeypatch):
    """视频优先（§32.2.1）：有视频 ⇒ 只落 1 个视频资产、不落图片。

    ⚠️ 商品视频当前**无签名源**（§31.1.3 真机 0 命中），所以这条用构造数据验证
    取舍分支本身；现实数据下走的是上面那条纯图片路径。§31.10 起走队列。
    """
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id, image_count=4, video_url="http://qimg.example/v.mp4")
        _patch_gen_worker(monkeypatch, db_factory)
        resp = client.post(
            "/api/xhs/product-notes/generate-save",
            json={"platform_account_id": account_id, "item_id": ITEM_ID},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "queued"
        assert product_note_gen_service.wait_generate_done(timeout=15), "后台 worker 未跑完"

        db = db_factory()
        try:
            item = db.scalar(select(Task).where(Task.task_type == "xhs_note_gen_item"))
            payload = item.payload if isinstance(item.payload, dict) else {}
            note = db.scalar(select(Note).where(Note.note_id == payload["note_id"]))
            assets = db.scalars(select(NoteAsset).where(NoteAsset.note_id == note.id)).all()
            assert len(assets) == 1
            assert assets[0].asset_type == "video"
            assert assets[0].url == "http://qimg.example/v.mp4"
        finally:
            db.close()
        assert _publish_side_calls() == []
    finally:
        _teardown()


def test_publish_video_only_note_is_rejected_with_reason(tmp_path, monkeypatch):
    """「形态=图文但只有视频素材」⇒ 明确失败 + 可读原因（设计 41 §15）。

    ⚠️ **原判据已被取代**：本用例原名 `…video_unsupported…`，断言
    `NOTE_VIDEO_UNSUPPORTED`（「视频笔记发布链路尚未接通」，设计 13 §32.7）。
    §15 接通视频链路后，**视频笔记可发布**；真正该拦的是「形态与素材不一致」——
    界面说图文、素材却只有视频（生成侧与形态脱钩）。原句保留在 docstring。

    守的是**不静默失败**：用户看到的是 `NOTE_FORM_MEDIA_MISMATCH` 与一句说清的话。
    异步后这是**同步门禁**（§31.5 步 0）—— 点击即知发不了，不进队列。
    """
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id, image_count=0, video_url="http://qimg.example/v.mp4")
        note_id = _generate_one(db_factory, account_id)
        _patch_async(monkeypatch, db_factory, tmp_path)
        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.note_id == note_id))
            #: 造出「形态=图文 但只有视频」的不一致态（正常情况下生成侧会置 video）。
            note.note_form = "image"
            db.commit()
            with pytest.raises(note_publish_service.NotePublishError) as exc:
                note_publish_service.start_publish(
                    db, 1, note.id, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
                )
            assert exc.value.code == "NOTE_FORM_MEDIA_MISMATCH"
            assert "标记为「图文笔记」" in exc.value.message
        finally:
            db.close()
    finally:
        _teardown()


def test_set_note_publish_status_keeps_projection_in_sync():
    """唯一写入入口：状态与 `published` 投影**一次写入**，非法取值直接抛（§32.3.2）。"""
    from backend.app.models.note import (
        PUBLISH_STATUS_FAILED,
        PUBLISH_STATUS_PENDING,
        PUBLISH_STATUS_PUBLISHED,
        set_note_publish_status,
    )

    note = Note(user_id=1, platform_account_id=1, platform="xhs", note_id="local_probe")
    set_note_publish_status(note, PUBLISH_STATUS_PUBLISHED)
    assert note.publish_status == PUBLISH_STATUS_PUBLISHED
    assert note.published is True
    assert note.published_at is not None  # 首次置位写时间
    first_at = note.published_at

    set_note_publish_status(note, PUBLISH_STATUS_FAILED, error="平台原文原因")
    assert note.publish_status == PUBLISH_STATUS_FAILED
    assert note.published is False  # 投影跟着走
    assert note.publish_error == "平台原文原因"
    assert note.published_at == first_at  # 历史发布成功时间只写不清

    set_note_publish_status(note, PUBLISH_STATUS_PENDING)
    assert note.published is False
    assert note.publish_error == ""  # 重发中不留旧原因

    with pytest.raises(ValueError):
        set_note_publish_status(note, "archived")


def test_item_note_count_follows_relation_table(tmp_path):
    """商品列表的挂链笔记数按**关系表**统计（设计 13 §32.4，用户问题④）。

    生成一篇挂该商品的笔记 ⇒ `note_count` 立刻 +1（平台口径的
    `platform_note_count` 不变 —— 平台还不知道这篇的存在）。
    """
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        # `status=all` 绕开 card_type 筛选（本 fixture 的台账行没有 card_type）
        before = client.post(
            "/api/xhs/qianfan/items",
            json={"platform_account_id": account_id, "status": "all"},
            headers=headers,
        ).json()
        assert before["items"][0]["note_count"] == 0

        _generate_one(db_factory, account_id)

        after = client.post(
            "/api/xhs/qianfan/items",
            json={"platform_account_id": account_id, "status": "all"},
            headers=headers,
        ).json()
        assert after["items"][0]["note_count"] == 1  # 关系表口径：本地生成的这篇算进来了
        assert after["items"][0]["platform_note_count"] == 0  # 平台侧还不知道
        # 排序口径随响应回传（设计 13 §32.1：后端是排序的唯一权威）
        assert after["sort_field"] == "create_time" and after["order"] == "desc"
    finally:
        _teardown()


# --------------------------------------------------------------------------- #
# 前端行为脚本（note-publish-gates.ts 纯逻辑）：接线 + 变异（2026-09-25 补）
# --------------------------------------------------------------------------- #


def test_note_publish_behaviour_script_passes(frontend_runner):
    """`note_publish.cjs` 必须**真的被 pytest 跑到**（DEF-063 同型：没接 = 没守）。

    脚本打包真实的 `lib/note-publish-gates.ts`，验证可发布判据只有一条、
    已发布不许重发、批量跳过明示、顺序保持。
    """
    result = frontend_runner("note_publish.cjs")
    assert result.returncode == 0, (result.stdout + result.stderr)[-900:]


@pytest.mark.parametrize("mutate", ["gate", "repub", "skip", "label", "order"])
def test_note_publish_mutations_are_caught(mutate):
    """五个变异开关必须把脚本打红（守卫不是「恰好为真」）。

    没有这一层，`gate` / `repub` 这些锚点可能只是恰好匹配 —— 变异没接入
    pytest = 变异不算数。
    """
    import os
    import subprocess

    env = dict(os.environ)
    env["NOTE_PUBLISH_MUTATE"] = mutate
    result = subprocess.run(
        ["node", str(ROOT / "tests" / "frontend" / "note_publish.cjs")],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, f"变异 {mutate} **未被抓住**（守卫只是恰好为真）：\n{output[-600:]}"


# --------------------------------------------------------------------------- #
# 下载媒体必须带 User-Agent（2026-10-07 用户报「下载媒体失败（第 2 个）：405」）
# --------------------------------------------------------------------------- #
class TestMediaDownloadHeaders:
    """平台 CDN 对**没有 User-Agent** 的请求回 **405**（不是 403）—— 症状极具误导性。

    实测矩阵（同一个 `http://qimg.xiaohongshu.com/material_space/<uuid>` 直链）：

    | 请求 | 结果 |
    |---|---|
    | 无 UA（原实现只有 `Referer: ""`） | **405** |
    | `Referer: ""` + UA | **200** |
    | 只带 UA | **200** |

    用户实测表现：`system.business_note_auto_publish` 真跑发布 ⇒ 第 2 张图 405 ⇒
    整篇 `publish_status=failed`（`publish_attempts=3` 三次重试全 405，是确定性失败
    不是抖动）。项目里**同类坑已踩过一次**（`note_detail_ssr._default_user_agent`：
    没 UA 被判成非浏览器、踢到登录页），下载媒体这条当时漏了。
    """

    def test_headers_include_a_user_agent(self):
        from backend.app.services import note_publish_service as svc

        headers = svc._media_download_headers()
        assert headers.get("User-Agent"), "下载媒体必须带 User-Agent（缺 ⇒ CDN 回 405）"
        assert "Mozilla/5.0" in headers["User-Agent"], headers

    def test_referer_is_still_cleared(self):
        """`Referer` 置空是**既有口径**（带小红书 Referer 会触发防盗链）—— 别在修 UA 时改掉它。"""
        from backend.app.services import note_publish_service as svc

        assert svc._media_download_headers().get("Referer") == ""

    def test_download_call_uses_the_shared_headers(self):
        """判据落在**调用形态**上：`_download_media` 必须调它，不能手拼 header。

        ⚠️ 只查「`_media_download_headers` 函数存在」会漏掉「函数在、调用点改回手拼」
        这种回退（本仓踩过同型假绿，DEF-056）。
        """
        src = (
            Path(__file__).resolve().parents[2]
            / "backend/app/services/note_publish_service.py"
        ).read_text(encoding="utf-8")
        assert "headers=_media_download_headers()" in src, (
            "下载媒体的 requests.get 必须用 `_media_download_headers()`（唯一实现）"
        )

    def test_no_bare_referer_only_get_remains(self):
        """反向：不许再出现「只有 Referer、没有 UA」的旧写法。"""
        src = (
            Path(__file__).resolve().parents[2]
            / "backend/app/services/note_publish_service.py"
        ).read_text(encoding="utf-8")
        assert 'headers={"Referer": ""}' not in src, "旧写法（只有 Referer、无 UA）不得回来"
