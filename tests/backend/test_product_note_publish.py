"""商品笔记智能生成与发布测试（设计 13 §31，2026-09-23）。

这批用例盯的是**状态机与链路完整性**，不是平台行为（平台行为由真机验证记录
背书，见设计 13 §31.5.1）：

1. **生成落库四件套**：`notes`（`local_generated`/`pending`/`local_` 前缀 ID）+
   `note_assets`（商品图）+ `note_goods`（关系）+ `topics_json`（平台话题）；
2. **门禁失败零平台请求**：未绑店 / 无 ark 令牌 / 台账无此行 —— 三个 400/404，
   且假适配器一次都不被构造；
3. **平台失败不落库**：task `success=false` / 轮询超时 ⇒ 502 + 平台原文，
   库里一条半成品都不留；
4. **批量错误隔离**：3 项挂 1 项 ⇒ 其余 2 项落库、失败项带原因；
5. **发布成功全链**：下载 → 上传（permit→PUT）→ 预审核 → 发布，四步调用序，
   成功后 `note_id` 换平台 ID、三态置位、**临时目录已删**、`local_path` 清空；
6. **发布失败留现场**：平台拒发 ⇒ `failed` + `publish_error` 含原文、
   **临时目录保留**、可重发；
7. **item-notes 改关系表源**：`note_goods` join `notes`，本地生成笔记带发布态。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory
from backend.app.api.platforms.xhs.qianfan import get_qianfan_api_adapter_factory as _qf_dep
from backend.app.core.database import Base, get_db
from backend.app.main import app
from backend.app.models import Note, NoteAsset, PlatformAccount, ShopProduct, User
from backend.app.models.shop import NoteGoods, Shop
from backend.app.services import note_publish_service, product_note_gen_service
from tests.backend.cookie_fixtures import bind_shop_account

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[2]

ITEM_ID = "ITEM-1"
SKU_ID = "SKU-1"


class FakePublishAdapter:
    """智能生成 + 发布全链路假适配器（脚本化，不联网）。

    `fail_task` / `reject_preaudit` / `fail_publish` 三个开关分别制造三种失败，
    用来验证「不落库 / 记失败 / 可重发」。
    """

    calls: list[str] = []

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    # ---- 生成链路 ----
    def aigc_publish_attributes(self, item_id, *, sku_id=""):
        FakePublishAdapter.calls.append("attributes")
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
        if FakePublishAdapter.fail_publish:
            return False, "平台发布被拒（测试）", {"success": False, "msg": "平台发布被拒（测试）"}
        return True, "成功", {"success": True, "data": {"id": "REAL-NOTE-1", "note_id": "REAL-NOTE-1"}}

    # ---- 类变量开关（测试间重置）----
    fail_task = False
    reject_preaudit = False
    fail_publish = False


def _reset():
    FakePublishAdapter.calls = []
    FakePublishAdapter.fail_task = False
    FakePublishAdapter.reject_preaudit = False
    FakePublishAdapter.fail_publish = False


def _setup(tmp_path):
    _reset()
    engine = create_engine(
        f"sqlite:///{tmp_path / 'publish.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
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
        shop_pk = account.shop_id_fk
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
            shop_id_fk=shop_pk,
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


def test_generate_save_creates_note_assets_and_relation(tmp_path):
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        resp = client.post(
            "/api/xhs/product-notes/generate-save",
            json={"platform_account_id": account_id, "item_id": ITEM_ID},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["note_id"].startswith("local_")
        assert body["topics"] == ["桌布", "桌垫"]

        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.note_id == body["note_id"]))
            assert note is not None
            assert note.note_type == "local_generated"
            assert note.publish_status == "pending"
            assert note.published is False
            assert json.loads(note.topics_json) == ["桌布", "桌垫"]
            aigc = note.raw_json["aigc"]
            assert aigc["task_id"] == "TASK-1"
            assert aigc["sku_id"] == SKU_ID  # 发布链路要用的 SKU 粒度
            assets = db.scalars(select(NoteAsset).where(NoteAsset.note_id == note.id)).all()
            assert len(assets) == 3
            assert assets[0].url == "http://qimg.example/a.jpg"
            rels = db.scalars(select(NoteGoods).where(NoteGoods.note_id == note.id)).all()
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
        assert resp.json()["detail"]["code"] == "QIANFAN_BINDING_REQUIRED"
        assert FakePublishAdapter.calls == []

        # 绑店但无 ark 令牌 ⇒ 400 CREDENTIAL_MISSING
        _bind_with_product(db_factory, account_id, with_ark=False, seller_id="SELLER-NO-ARK")
        resp = client.post(
            "/api/xhs/product-notes/generate-save",
            json={"platform_account_id": account_id, "item_id": ITEM_ID},
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "QIANFAN_CREDENTIAL_MISSING"
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


def test_generate_platform_failure_writes_nothing(tmp_path):
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        FakePublishAdapter.fail_task = True
        resp = client.post(
            "/api/xhs/product-notes/generate-save",
            json={"platform_account_id": account_id, "item_id": ITEM_ID},
            headers=headers,
        )
        assert resp.status_code == 502
        assert "平台生成失败（测试）" in resp.json()["detail"]["message"]
        db = db_factory()
        try:
            assert db.scalar(select(Note)) is None  # 不落库
        finally:
            db.close()
    finally:
        _teardown()


def test_batch_generate_isolates_failures(tmp_path):
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        db = db_factory()
        try:
            from backend.app.services.shop_binding_service import account_shop_id

            shop_pk = account_shop_id(db, account_id)
            # 第二件商品存在，第三件不存在（404 路径）
            db.add(
                ShopProduct(
                    user_id=1,
                    shop_id_fk=shop_pk,
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
        assert body["succeeded"] == 2
        assert body["failed"] == 1
        results = {r["item_id"]: r for r in body["results"]}
        assert results[ITEM_ID]["ok"] is True
        assert results["ITEM-2"]["ok"] is True
        assert results["ITEM-3"]["ok"] is False
        assert results["ITEM-3"]["code"] == "QIANFAN_ITEM_NOT_FOUND"
    finally:
        _teardown()


# --------------------------------------------------------------------------- #
# 发布
# --------------------------------------------------------------------------- #


def _generate_one(db_factory, account_id):
    db = db_factory()
    try:
        account = db.get(PlatformAccount, account_id)
        return product_note_gen_service.generate_note_for_item(
            db, account, ITEM_ID, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
        )["note_id"]
    finally:
        db.close()


def test_publish_success_runs_full_chain(tmp_path, monkeypatch):
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        note_id = _generate_one(db_factory, account_id)
        FakePublishAdapter.calls = []  # 只看发布阶段的调用序

        # 把下载目标指向本地假服务器太贵 —— 直接 monkeypatch requests.get 回假图字节
        import requests

        class _FakeResponse:
            status_code = 200
            content = b"\xff\xd8\xff\xe0" + b"0" * 2048  # 够 100 字节下限
            headers = {"content-type": "image/jpeg"}

            def raise_for_status(self):
                return None

        monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse())

        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.note_id == note_id))
            result = note_publish_service.publish_note(
                db, 1, note.id, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
            )
        finally:
            db.close()

        assert result["ok"] is True
        assert result["platform_note_id"] == "REAL-NOTE-1"
        # 调用序：上传凭证 → 逐文件上传 → 预审核 → 发布
        assert FakePublishAdapter.calls[0] == "upload_permit"
        assert FakePublishAdapter.calls[1].startswith("upload_file:")
        assert "preaudit" in FakePublishAdapter.calls
        assert FakePublishAdapter.calls[-1] == "publish"

        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.id == note.id))
            assert note.note_id == "REAL-NOTE-1"  # 平台 ID 回写
            assert note.publish_status == "published"
            assert note.published is True
            assert note.publish_error == ""
            assert note.published_at is not None
            assets = db.scalars(select(NoteAsset).where(NoteAsset.note_id == note.id)).all()
            assert all(not a.local_path for a in assets)
            # 临时目录已删
            from backend.app.core.config import get_settings

            tmp_dir = Path(get_settings().storage_dir) / "tmp_note_publish" / "REAL-NOTE-1"
            assert not tmp_dir.exists()
        finally:
            db.close()
    finally:
        _teardown()


def test_publish_failure_records_reason_and_keeps_temp(tmp_path, monkeypatch):
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        note_id = _generate_one(db_factory, account_id)
        FakePublishAdapter.fail_publish = True
        FakePublishAdapter.calls = []

        import requests

        class _FakeResponse:
            status_code = 200
            content = b"\xff\xd8\xff\xe0" + b"0" * 2048
            headers = {"content-type": "image/jpeg"}

            def raise_for_status(self):
                return None

        monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse())

        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.note_id == note_id))
            local_id = note.note_id
            result = note_publish_service.publish_note(
                db, 1, note.id, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
            )
        finally:
            db.close()

        assert result["ok"] is False
        assert "平台发布被拒（测试）" in result["error"]
        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.id == note.id))
            assert note.publish_status == "failed"
            assert note.publish_error == "平台发布被拒（测试）"
            assert note.note_id == local_id  # 平台 ID 没污染本地 ID
            # 临时目录保留（重发不必重下）
            from backend.app.core.config import get_settings

            tmp_dir = Path(get_settings().storage_dir) / "tmp_note_publish" / local_id
            assert tmp_dir.exists()
        finally:
            db.close()

        # 失败后可重发：去掉开关再发一次 → 成功
        FakePublishAdapter.fail_publish = False
        db = db_factory()
        try:
            result2 = note_publish_service.publish_note(
                db, 1, note.id, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
            )
        finally:
            db.close()
        assert result2["ok"] is True
    finally:
        _teardown()


def test_publish_gates(tmp_path, monkeypatch):
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        note_id = _generate_one(db_factory, account_id)
        db = db_factory()
        try:
            # 不存在 ⇒ 抛 NOTE_NOT_FOUND（门禁类错误走异常，端点层转 400）
            try:
                note_publish_service.publish_note(
                    db, 1, 999999, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
                )
                raise AssertionError("should raise")
            except note_publish_service.NotePublishError as error:
                assert error.code == "NOTE_NOT_FOUND"

            note = db.scalar(select(Note).where(Note.note_id == note_id))
            # 无媒体 ⇒ 记 failed + NOTE_MEDIA_EMPTY（链路上的失败走返回值）
            db.query(NoteAsset).filter(NoteAsset.note_id == note.id).delete()
            db.commit()
            result = note_publish_service.publish_note(
                db, 1, note.id, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
            )
            assert result["ok"] is False
            assert result["code"] == "NOTE_MEDIA_EMPTY"
            db.refresh(note)
            assert note.publish_status == "failed"
            assert note.publish_error  # 原因非空
        finally:
            db.close()

        # 端点层同一道闸：无媒体的笔记走 HTTP ⇒ 200 + publish.ok=false
        # （链路失败回 200 是刻意设计：与「同步」端点同哲学 —— 试过的失败不是接口错误）
        resp = client.post(f"/api/notes/{_pk_of(db_factory, note_id)}/publish", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["publish"]["ok"] is False
        assert resp.json()["publish"]["code"] == "NOTE_MEDIA_EMPTY"
    finally:
        _teardown()


def _pk_of(db_factory, note_id: str) -> int:
    db = db_factory()
    try:
        return db.scalar(select(Note.id).where(Note.note_id == note_id))
    finally:
        db.close()


def test_publish_preaudit_rejection(tmp_path, monkeypatch):
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id)
        note_id = _generate_one(db_factory, account_id)
        FakePublishAdapter.reject_preaudit = True
        FakePublishAdapter.calls = []

        import requests

        class _FakeResponse:
            status_code = 200
            content = b"\xff\xd8\xff\xe0" + b"0" * 2048
            headers = {"content-type": "image/jpeg"}

            def raise_for_status(self):
                return None

        monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse())

        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.note_id == note_id))
            result = note_publish_service.publish_note(
                db, 1, note.id, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
            )
        finally:
            db.close()

        assert result["ok"] is False
        assert result["code"] == "PUBLISH_AUDIT_REJECTED"
        assert "正文含违规词（平台审的）" in result["error"]
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


def test_generate_caps_images_at_six_and_never_publishes(tmp_path):
    """媒体上限 6 张（用户口径「图片最多 6 张」）+ **生成不发布**（§32.2）。

    商品有 10 张图 ⇒ 只落 6 张；整条生成链路里发布侧调用**一次都没有**
    （上传凭证 / 上传 / 预审核 / 发布）—— 只有内容库点「发布」才走发布链路。
    """
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id, image_count=10)
        resp = client.post(
            "/api/xhs/product-notes/generate-save",
            json={"platform_account_id": account_id, "item_id": ITEM_ID},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["image_count"] == 6
        assert body["video_count"] == 0

        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.note_id == body["note_id"]))
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


def test_generate_prefers_video_over_images(tmp_path):
    """视频优先（§32.2.1）：有视频 ⇒ 只落 1 个视频资产、不落图片。

    ⚠️ 商品视频当前**无签名源**（§31.1.3 真机 0 命中），所以这条用构造数据验证
    取舍分支本身；现实数据下走的是上面那条纯图片路径。
    """
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id, image_count=4, video_url="http://qimg.example/v.mp4")
        resp = client.post(
            "/api/xhs/product-notes/generate-save",
            json={"platform_account_id": account_id, "item_id": ITEM_ID},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["video_count"] == 1
        assert body["image_count"] == 0

        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.note_id == body["note_id"]))
            assets = db.scalars(select(NoteAsset).where(NoteAsset.note_id == note.id)).all()
            assert len(assets) == 1
            assert assets[0].asset_type == "video"
            assert assets[0].url == "http://qimg.example/v.mp4"
        finally:
            db.close()
        assert _publish_side_calls() == []
    finally:
        _teardown()


def test_publish_video_only_note_is_rejected_with_reason(tmp_path):
    """「只有视频」的笔记发布 ⇒ 明确失败 + 可读原因（§32.7：视频发布链路未接通）。

    守的是**不静默失败**：用户看到的是 `NOTE_VIDEO_UNSUPPORTED` 与一句说清的话，
    不是「发布失败（未给原因）」。
    """
    headers, account_id, db_factory = _setup(tmp_path)
    try:
        _bind_with_product(db_factory, account_id, image_count=0, video_url="http://qimg.example/v.mp4")
        note_id = _generate_one(db_factory, account_id)
        db = db_factory()
        try:
            note = db.scalar(select(Note).where(Note.note_id == note_id))
            result = note_publish_service.publish_note(
                db, 1, note.id, adapter_factory=lambda cookies: FakePublishAdapter(cookies)
            )
        finally:
            db.close()
        assert result["ok"] is False
        assert result["code"] == "NOTE_VIDEO_UNSUPPORTED"
        assert "视频笔记发布链路尚未接通" in result["error"]
    finally:
        _teardown()


def test_set_note_publish_status_keeps_projection_in_sync():
    """唯一写入入口：状态与 `published` 投影**一次写入**，非法取值直接抛（§32.3.2）。"""
    import pytest as _pytest

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

    with _pytest.raises(ValueError):
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
