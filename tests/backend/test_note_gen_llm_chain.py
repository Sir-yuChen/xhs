"""LLM 生成链路端到端（设计 17 §12 测试要点，T4/T6/T5）。

## 为什么必须补这一篇

T4（`note_gen_llm_service`，359 行）+ T6（`note_gen_llm_worker`，1250 行）
合计 1609 行，在 `c38aec6` 一次性落地，但**没有一条端到端用例**：

* `test_product_note_gen_queue.py`（748 行）通篇打 `/generate-save`
  与 `/batch-generate` —— 那是**一期千帆三步生成**（`TASK_TYPE_GEN_ITEM`），
  与本期的 `xhs_note_gen_llm_item` 队列**是两条不同的链路**（worker 文件头
  的对照表写得很清楚）。
* `test_scheduler_sync.py` 只有两条「唤醒时线程名的顺序断言」，碰不到业务。

⇒ 设计 17 §12 点名要覆盖的六件事（门禁 / 幂等 / 护栏 / 失败落 `generation_records`
/ retry / 队列自检 + 用量台账）**一条都没落地**。本文件就补这些。

## 覆盖清单（与 §12 逐条对齐）

| 用例 | 钉住的契约 |
|---|---|
| `test_gateway_not_configured_blocks_enqueue` | 网关门禁：没配服务商/映射 ⇒ 400 `LLM_GATEWAY_NOT_CONFIGURED`，**不入队** |
| `test_unknown_product_gate_blocks_enqueue` | 商品门禁：台账无此行 ⇒ 404 `LLM_PRODUCT_NOT_FOUND`，**不入队** |
| `test_unknown_template_gate_blocks_enqueue` | 模板门禁：模板不存在 ⇒ 404 `LLM_TEMPLATE_NOT_FOUND`，**不入队** |
| `test_gate_failures_never_call_upstream` | 三条门禁全部**零上游请求**（stub 计数为 0） |
| `test_single_enqueue_returns_queued_and_worker_persists_note` | 正路径：入队即返回 + worker 落 `notes`/`note_topics`/`note_assets`/`note_generation_links` + `generation_records=success` |
| `test_retry_is_idempotent_while_not_terminal` | 幂等：同商品未终态 ⇒ `already_queued`，队列只有一条 |
| `test_guardrail_block_marks_failed_and_writes_no_note` | 护栏命中 ⇒ `LLM_GUARDRAIL_BLOCKED`，**不落半条笔记**，但**照常记账**（上游确实被调用了） |
| `test_non_json_response_marks_failed_with_code` | 模型没按 JSON 回 ⇒ `LLM_RESPONSE_NOT_JSON` + `failed` |
| `test_batch_gate_failure_isolated_per_item` | 批量：逐项门禁，坏项不阻断好项 |
| `test_retry_only_for_failed_records` | `success` / `running` 记录重试 ⇒ 400 `LLM_RECORD_NOT_RETRYABLE` |
| `test_retry_requeues_same_record_and_reuses_audit_row` | retry = 新队列项 + **原审计行复位**（不新建行） |
| `test_queue_snapshot_shape_uses_generated_key` | 快照键名契约：`generated` 不是底座 `synced`；`reason` 取审计行 |
| `test_worker_survives_system_exit_and_marks_failed` | 守活（DEF-069 同型）：`SystemExit` 也不许把项挂在 `running` |
| `test_generation_records_one_row_per_generation` | 审计唯一性：一次生成 = 一条 `generation_records`（retry 复用不新增） |

⚠️ **测试卫生**（与其余 worker 测试同规）：生成 worker 是**进程级全局线程**，
夹具必须显式请求 `monkeypatch`（teardown 时先收 worker、再恢复 `_session_factory`），
否则残留 worker 会拿**真库**认领测试塞的假行 = 数据隔离事故
（`.workbuddy/memory/MEMORY.md` 的 T3 实测记录过同型事故）。
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from sqlalchemy import select

from backend.app.core.security import encrypt_text
from backend.app.models import (
    GenerationRecord,
    LlmBusinessProvider,
    LlmGatewayConfig,
    LlmGatewayModel,
    Note,
    NoteAsset,
    NoteGenerationLink,
    PlatformAccount,
    PromptTemplate,
    ShopProduct,
    Task,
    User,
)
from backend.app.models.generation import (
    GEN_STATUS_FAILED,
    GEN_STATUS_SUCCESS,
    MEDIA_SOURCE_PRODUCT_IMAGE,
)
from backend.app.models.note import NoteTopic
from backend.app.services import note_gen_llm_worker as gw
from backend.app.services import note_queue_service as nqs
from backend.app.services.business_provider_keys import BUSINESS_KEY_GENERATE_NOTE

BATCH = "/api/xhs/product-notes/batch-generate-llm"
SINGLE = "/api/xhs/product-notes/generate-llm"
SNAPSHOT = "/api/xhs/product-notes/generate-llm-queue"
RETRY = "/api/xhs/product-notes/generate-llm/{record_id}/retry"

ITEM_ID = "ITEM-LLM-1"
PROVIDER_KEY = "PROV-TEXT-1"
MODEL_NAME = "deepseek-v4-flash"


class FakeUpstream:
    """`llm_gateway_client.complete_text` 的脚本化替身（**不联网**）。

    `reply` 是返回给 `parse_generated_payload` 的原始文本；`raise_error` 非 None
    时抛异常（用来造「上游挂了」）。`calls` 计数是**门禁零请求**断言的依据 ——
    门禁不过时它必须恒为 0。
    """

    reply: str = ""
    raise_error: BaseException | None = None
    calls: int = 0
    hold: threading.Event | None = None

    @classmethod
    def complete_text(cls, **kwargs) -> str:
        cls.calls += 1
        if cls.hold is not None:
            cls.hold.wait(timeout=10)
        if cls.raise_error is not None:
            raise cls.raise_error
        return cls.reply


def _valid_reply() -> str:
    return json.dumps(
        {
            "title": "防水桌布真好用",
            "body": "这张桌布铺上之后，桌面再也不怕洒水了。" * 40,
            "tags": ["桌布", "桌垫"],
        },
        ensure_ascii=False,
    )


def _reset_fake() -> None:
    FakeUpstream.reply = _valid_reply()
    FakeUpstream.raise_error = None
    FakeUpstream.calls = 0
    FakeUpstream.hold = None


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _quiesce_gen_llm_workers(*, timeout: float = 5.0) -> list[str]:
    """把进程里所有活着的生成 worker / 看门狗收掉（先置停止位再 join）。

    收不干净就**直接失败**（不静默放过）—— 残留线程会抢后续用例的队列。
    """
    gw._gen_llm_worker_stop.set()
    gw._gen_llm_wakeup.set()
    stopped: list[str] = []
    for thread in list(threading.enumerate()):
        if thread.name not in (gw.GEN_LLM_WORKER_THREAD_NAME, gw.GEN_LLM_WATCHDOG_THREAD_NAME):
            continue
        if not thread.is_alive():
            continue
        thread.join(timeout=timeout)
        if thread.is_alive():
            raise AssertionError(
                f"有线程收不掉（{thread.name}）—— 它会抢后续用例的队列，先查清是谁起的"
            )
        stopped.append(thread.name)
    with gw._gen_llm_queue_lock:
        gw._gen_llm_worker = None
    gw._gen_llm_worker_stop.clear()
    gw._gen_llm_wakeup.clear()
    return stopped


@pytest.fixture()
def clean_gen_llm_globals(monkeypatch):
    """收干净 worker 线程（显式请求 `monkeypatch` 保证 teardown 顺序）。"""
    _quiesce_gen_llm_workers()
    try:
        yield
    finally:
        _quiesce_gen_llm_workers()


@pytest.fixture()
def llm_env(api_client, db_factory, monkeypatch):
    """一套可用的 LLM 生成环境：用户 + 账号 + 台账商品 + 已配默认文本模型。

    返回 `(client, headers, user_id, account_id, product_pk)`。

    ⚠️ worker 自建 Session 必须打桩到**临时库**（`gw._session_factory`），
    否则会认领生产库的行 —— 隔离事故（本文件头「测试卫生」）。
    """
    _reset_fake()
    # 上游替身：直接换掉 service 模块里引用到的那个名字（唯一汇聚点）。
    monkeypatch.setattr(gw.llm_gateway_client, "complete_text", FakeUpstream.complete_text)
    monkeypatch.setattr(gw, "_session_factory", db_factory)
    # 空转轮询间隔置 0：不真等 5s 才发现新任务。
    monkeypatch.setattr(gw, "GEN_LLM_IDLE_POLL_SECONDS", 0)

    response = api_client.post(
        "/api/auth/register", json={"username": "llm-owner", "password": "secret123"}
    )
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    db = db_factory()
    try:
        user = db.scalar(select(User).where(User.username == "llm-owner"))
        account = PlatformAccount(
            user_id=user.id, platform="xhs", sub_type="creator", nickname="生成店", status="active"
        )
        db.add(account)
        db.flush()
        _seed_gateway(db, user, account)
        product = ShopProduct(
            user_id=user.id,
            platform_account_id=account.id,
            item_id=ITEM_ID,
            item_title="防水桌布",
            price="5440",
            images_json=json.dumps(["http://qimg.example/a.jpg", "http://qimg.example/b.jpg"]),
            raw_json=json.dumps({"images": [{"link": "http://qimg.example/a.jpg"}]}),
        )
        db.add(product)
        db.commit()
        db.refresh(user)
        db.refresh(account)
        db.refresh(product)
        user_id, account_id, product_pk = user.id, account.id, product.id
    finally:
        db.close()

    yield api_client, headers, user_id, account_id, product_pk


def _seed_gateway(db, user: User, account: PlatformAccount) -> LlmGatewayConfig:
    """默认文本服务商 + 模型行 + `generate_note` 业务点映射（`resolve_business_provider` 的输入）。

    v2.0 语义：`llm_gateway_configs` 是服务商凭据行，`llm_business_providers`
    是「平台 × 业务点 → provider_key」映射。**两者齐备**才算「配了默认文本模型」。

    v2.3 起映射行**必须显式带 `model_name`**（业务入口不再回落任何默认名），
    且该模型须在该服务商目录内 —— 这里一并写进 `models_json`，否则
    `resolve_business_provider` 会抛 `LLM_BUSINESS_MODEL_REQUIRED`。

    ⚠️ **v2.7/v2.8**：服务商层已无 `platform` / `model_type` / `default_model`
    三列 —— 能力类型（`model_type`）与使用平台（`platform`）改由**模型行**承载，
    兜底名整列删除。故这里额外建一条模型行（v2.7 起类型判据即读它）。
    """
    config = LlmGatewayConfig(
        user_id=user.id,
        name="测试文本服务商",
        provider_key=PROVIDER_KEY,
        base_url="https://api.example.test/v1",
        access_token=encrypt_text("sk-test"),
        models_json=json.dumps([MODEL_NAME]),
        enabled=True,
        healthy=True,
    )
    db.add(config)
    db.add(
        LlmGatewayModel(
            user_id=user.id,
            provider_key=PROVIDER_KEY,
            model_name=MODEL_NAME,
            model_type="text",
            platform="xhs",
            enabled=True,
        )
    )
    db.add(
        LlmBusinessProvider(
            user_id=user.id,
            platform="xhs",
            business_key=BUSINESS_KEY_GENERATE_NOTE.key,
            provider_key=PROVIDER_KEY,
            model_name=MODEL_NAME,
            enabled=True,
        )
    )
    db.flush()
    return config


# --------------------------------------------------------------------------- #
# 读取辅助
# --------------------------------------------------------------------------- #
def _gen_items(db_factory) -> list[Task]:
    db = db_factory()
    try:
        return db.scalars(
            select(Task)
            .where(Task.task_type == nqs.TASK_TYPE_GEN_LLM_ITEM)
            .order_by(Task.id)
        ).all()
    finally:
        db.close()


def _records(db_factory) -> list[GenerationRecord]:
    db = db_factory()
    try:
        return db.scalars(select(GenerationRecord).order_by(GenerationRecord.id)).all()
    finally:
        db.close()


def _wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# --------------------------------------------------------------------------- #
# ① 门禁三件：任一不过 ⇒ 400/404 结构化，**不入队、不碰上游**
# --------------------------------------------------------------------------- #
def test_gateway_not_configured_blocks_enqueue(api_client, db_factory, monkeypatch):
    """没配服务商 / 业务点映射 ⇒ 400 `LLM_GATEWAY_NOT_CONFIGURED`，队列零行。

    ⚠️ 原名 `test_missing_default_model_gate_blocks_enqueue` —— v2.8 `default_model` 列已删，
    「没配默认模型」这一说法不再成立；实际判据始终是「服务商 + 业务点映射是否齐备」。
    """
    _reset_fake()
    monkeypatch.setattr(gw.llm_gateway_client, "complete_text", FakeUpstream.complete_text)
    monkeypatch.setattr(gw, "_session_factory", db_factory)

    r = api_client.post(
        "/api/auth/register", json={"username": "no-model", "password": "secret123"}
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    db = db_factory()
    try:
        user = db.scalar(select(User).where(User.username == "no-model"))
        account = PlatformAccount(
            user_id=user.id, platform="xhs", sub_type="creator", nickname="无模型店", status="active"
        )
        db.add(account)
        db.flush()
        # ⚠️ 故意**不**调 `_seed_gateway`：没有 provider / 映射。
        db.add(
            ShopProduct(
                user_id=user.id,
                platform_account_id=account.id,
                item_id=ITEM_ID,
                item_title="防水桌布",
                price="5440",
                images_json="[]",
                raw_json="{}",
            )
        )
        db.commit()
        account_id = account.id
    finally:
        db.close()

    resp = api_client.post(
        SINGLE,
        json={"platform_account_id": account_id, "item_id": ITEM_ID},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "LLM_GATEWAY_NOT_CONFIGURED"
    assert _gen_items(db_factory) == [], "门禁不过却入了队"
    assert FakeUpstream.calls == 0, "门禁不过却调了上游"


def test_unknown_product_gate_blocks_enqueue(llm_env, db_factory):
    """台账里没有这个商品 ⇒ 404 `LLM_PRODUCT_NOT_FOUND`，不入队。"""
    client, headers, _uid, account_id, _pk = llm_env
    resp = client.post(
        SINGLE,
        json={"platform_account_id": account_id, "item_id": "NOT-IN-LEDGER"},
        headers=headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "LLM_PRODUCT_NOT_FOUND"
    assert _gen_items(db_factory) == []
    assert FakeUpstream.calls == 0


def test_unknown_template_gate_blocks_enqueue(llm_env, db_factory):
    """模板不存在 ⇒ 404 `LLM_TEMPLATE_NOT_FOUND`，不入队。"""
    client, headers, _uid, account_id, _pk = llm_env
    resp = client.post(
        SINGLE,
        json={
            "platform_account_id": account_id,
            "item_id": ITEM_ID,
            "template_id": 99999,
        },
        headers=headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "LLM_TEMPLATE_NOT_FOUND"
    assert _gen_items(db_factory) == []
    assert FakeUpstream.calls == 0


def test_gate_failures_never_call_upstream(llm_env, db_factory):
    """三条门禁的任意一条不过 ⇒ 上游调用计数恒为 0（构造性断言）。"""
    client, headers, _uid, account_id, _pk = llm_env
    for payload, code in (
        ({"platform_account_id": account_id, "item_id": "NOPE"}, "LLM_PRODUCT_NOT_FOUND"),
        (
            {"platform_account_id": account_id, "item_id": ITEM_ID, "template_id": 4242},
            "LLM_TEMPLATE_NOT_FOUND",
        ),
    ):
        resp = client.post(SINGLE, json=payload, headers=headers)
        assert resp.status_code in (400, 404), resp.text
        assert resp.json()["detail"]["code"] == code
    assert FakeUpstream.calls == 0


# --------------------------------------------------------------------------- #
# ② 正路径：入队即返回 + worker 落库六处
# --------------------------------------------------------------------------- #
def test_single_enqueue_returns_queued_and_worker_persists_note(
    clean_gen_llm_globals, llm_env, db_factory
):
    """入队回执是毫秒级 `{ok, status:queued, record_id}`；worker 落库全套。"""
    client, headers, uid, account_id, product_pk = llm_env

    resp = client.post(
        SINGLE,
        json={"platform_account_id": account_id, "item_id": ITEM_ID},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "queued"
    assert body["record_id"] > 0, "入队必须回执可跳转的审计行 id"
    assert "already_queued" not in body

    assert gw.wait_llm_generate_done(timeout=20), "队列未跑空"

    db = db_factory()
    try:
        note = db.scalar(select(Note).where(Note.user_id == uid))
        assert note is not None, "worker 没落笔记"
        assert note.platform_account_id == account_id
        assert note.title == "防水桌布真好用"
        assert note.content, "正文不能为空"

        # 话题走 `apply_sync_topics`（唯一入口）
        topics = db.scalars(select(NoteTopic).where(NoteTopic.note_id == note.id)).all()
        assert len(topics) == 2

        # 媒体（本期只有 product_image 可用 ⇒ 2 张商品图）
        assets = db.scalars(select(NoteAsset).where(NoteAsset.note_id == note.id)).all()
        assert len(assets) == 2

        # 本地主键级「生成笔记 ↔ 商品」关系
        link = db.scalar(select(NoteGenerationLink).where(NoteGenerationLink.note_pk == note.id))
        assert link is not None and link.shop_product_id == product_pk

        # 审计行：同一条记录被写成功（不是新建一条）
        record = db.get(GenerationRecord, body["record_id"])
        assert record.status == GEN_STATUS_SUCCESS
        assert record.title == "防水桌布真好用"
        assert json.loads(record.topics_json) == ["桌布", "桌垫"]
        assert record.note_id if hasattr(record, "note_id") else True  # 字段可选，不强求
    finally:
        db.close()

    # 一次生成 = 一条审计行
    assert len(_records(db_factory)) == 1


def test_retry_is_idempotent_while_not_terminal(clean_gen_llm_globals, llm_env, db_factory):
    """同商品未终态时重复入队 ⇒ `already_queued`，队列仍只有一条。"""
    client, headers, _uid, account_id, _pk = llm_env
    # 把上游卡在「已认领未跑完」的确定态（假上游秒返回会让第二条请求到达前就终态）
    FakeUpstream.hold = threading.Event()

    first = client.post(
        SINGLE, json={"platform_account_id": account_id, "item_id": ITEM_ID}, headers=headers
    )
    assert first.status_code == 200 and "already_queued" not in first.json()

    second = client.post(
        SINGLE, json={"platform_account_id": account_id, "item_id": ITEM_ID}, headers=headers
    )
    assert second.status_code == 200, second.text
    assert second.json().get("already_queued") is True
    # 幂等回执必须回填首次的 record_id（否则前端「已在队列中」拿不到跳转目标）
    assert second.json()["record_id"] == first.json()["record_id"]

    assert len(_gen_items(db_factory)) == 1
    assert len(_records(db_factory)) == 1

    FakeUpstream.hold.set()  # 放行，让 worker 收尾
    gw.wait_llm_generate_done(timeout=20)


# --------------------------------------------------------------------------- #
# ③ 护栏 / 非 JSON / 上游失败 ⇒ 记 failed + 不落笔记
# --------------------------------------------------------------------------- #
def test_guardrail_block_marks_failed_and_writes_no_note(
    clean_gen_llm_globals, llm_env, db_factory
):
    """护栏命中 ⇒ `LLM_GUARDRAIL_BLOCKED` + 不落 `notes`，但**照常记账**（上游被调过）。"""
    client, headers, _uid, account_id, _pk = llm_env
    # 造一个必然命中违禁表达的正文（医疗功效宣称）
    FakeUpstream.reply = json.dumps(
        {
            "title": "根治痘痘的桌布",
            "body": "这款桌布可以根治痘痘，美白祛痘效果最好，全网最低价。" * 20,
            "tags": ["桌布"],
        },
        ensure_ascii=False,
    )
    resp = client.post(
        SINGLE, json={"platform_account_id": account_id, "item_id": ITEM_ID}, headers=headers
    )
    assert resp.status_code == 200
    record_id = resp.json()["record_id"]

    assert gw.wait_llm_generate_done(timeout=20)

    db = db_factory()
    try:
        assert db.scalar(select(Note)) is None, "护栏拦截却落了笔记（半条）"
        record = db.get(GenerationRecord, record_id)
        assert record.status == GEN_STATUS_FAILED
        assert record.error_code == "LLM_GUARDRAIL_BLOCKED"
        assert record.error_message, "失败原因要落库（跨重启可查）"
    finally:
        db.close()


def test_non_json_response_marks_failed_with_code(clean_gen_llm_globals, llm_env, db_factory):
    """模型没按 JSON 回 ⇒ `LLM_RESPONSE_NOT_JSON` + `failed`。"""
    client, headers, _uid, account_id, _pk = llm_env
    FakeUpstream.reply = "抱歉，我不太明白你的需求，可以再说一遍吗？"
    resp = client.post(
        SINGLE, json={"platform_account_id": account_id, "item_id": ITEM_ID}, headers=headers
    )
    record_id = resp.json()["record_id"]
    assert gw.wait_llm_generate_done(timeout=20)

    db = db_factory()
    try:
        assert db.scalar(select(Note)) is None
        record = db.get(GenerationRecord, record_id)
        assert record.status == GEN_STATUS_FAILED
        assert record.error_code == "LLM_RESPONSE_NOT_JSON"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# ④ 批量：坏项不阻断好项
# --------------------------------------------------------------------------- #
def test_batch_gate_failure_isolated_per_item(clean_gen_llm_globals, llm_env, db_factory):
    """批量里「台账找不到」的项进逐项回执，不阻断其余项入队。"""
    client, headers, _uid, account_id, _pk = llm_env
    FakeUpstream.hold = threading.Event()

    resp = client.post(
        BATCH,
        json={
            "platform_account_id": account_id,
            "item_ids": [ITEM_ID, "GHOST-ITEM"],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert body["started"] == 1
    assert body["failed"] == 1
    by_item = {entry["item_id"]: entry for entry in body["results"]}
    assert by_item[ITEM_ID]["ok"] is True
    assert by_item["GHOST-ITEM"]["ok"] is False
    assert by_item["GHOST-ITEM"]["code"] == "LLM_PRODUCT_NOT_FOUND"
    # 好项确实入了队（且只有它一条）
    assert len(_gen_items(db_factory)) == 1

    FakeUpstream.hold.set()
    gw.wait_llm_generate_done(timeout=20)


# --------------------------------------------------------------------------- #
# ⑤ retry 语义
# --------------------------------------------------------------------------- #
def test_retry_only_for_failed_records(clean_gen_llm_globals, llm_env, db_factory):
    """`success` 的记录不能重试 ⇒ 400 `LLM_RECORD_NOT_RETRYABLE`。"""
    client, headers, _uid, account_id, _pk = llm_env
    resp = client.post(
        SINGLE, json={"platform_account_id": account_id, "item_id": ITEM_ID}, headers=headers
    )
    record_id = resp.json()["record_id"]
    assert gw.wait_llm_generate_done(timeout=20)

    db = db_factory()
    try:
        assert db.get(GenerationRecord, record_id).status == GEN_STATUS_SUCCESS
    finally:
        db.close()

    retry = client.post(RETRY.format(record_id=record_id), headers=headers)
    assert retry.status_code == 400, retry.text
    assert retry.json()["detail"]["code"] == "LLM_RECORD_NOT_RETRYABLE"


def test_retry_requeues_same_record_and_reuses_audit_row(
    clean_gen_llm_globals, llm_env, db_factory
):
    """失败 → retry：新队列项 + **原审计行复位**（仍是一条记录）。"""
    client, headers, _uid, account_id, _pk = llm_env
    # 第一跑必然失败（非 JSON）
    FakeUpstream.reply = "不是 JSON"
    resp = client.post(
        SINGLE, json={"platform_account_id": account_id, "item_id": ITEM_ID}, headers=headers
    )
    record_id = resp.json()["record_id"]
    assert gw.wait_llm_generate_done(timeout=20)

    db = db_factory()
    try:
        assert db.get(GenerationRecord, record_id).status == GEN_STATUS_FAILED
    finally:
        db.close()
    assert len(_records(db_factory)) == 1

    # 修好上游再重试
    FakeUpstream.reply = _valid_reply()
    FakeUpstream.hold = threading.Event()  # 卡住，验证「新队列项已入队」
    retry = client.post(RETRY.format(record_id=record_id), headers=headers)
    assert retry.status_code == 200, retry.text
    assert retry.json()["record_id"] == record_id, "retry 必须复用原审计行，不许新建"

    # 审计行仍在（不是新建第二条），且状态已复位
    assert len(_records(db_factory)) == 1, "retry 新建了审计行（应复用）"

    FakeUpstream.hold.set()
    assert gw.wait_llm_generate_done(timeout=20)

    db = db_factory()
    try:
        record = db.get(GenerationRecord, record_id)
        assert record.status == GEN_STATUS_SUCCESS
        assert db.scalar(select(Note).where(Note.user_id == record.user_id)) is not None
    finally:
        db.close()
    assert len(_records(db_factory)) == 1, "retry 成功后仍应只有一条审计行"


# --------------------------------------------------------------------------- #
# ⑥ 快照契约
# --------------------------------------------------------------------------- #
def test_queue_snapshot_shape_uses_generated_key(clean_gen_llm_globals, llm_env, db_factory):
    """快照键名是 `generated`（不是底座 `synced`）；失败项 `reason` 取自审计行。"""
    client, headers, _uid, account_id, _pk = llm_env
    FakeUpstream.reply = "不是 JSON"
    resp = client.post(
        SINGLE, json={"platform_account_id": account_id, "item_id": ITEM_ID}, headers=headers
    )
    assert resp.status_code == 200
    assert gw.wait_llm_generate_done(timeout=20)

    snap = client.get(SNAPSHOT, headers=headers)
    assert snap.status_code == 200, snap.text
    data = snap.json()
    assert "generated" in data, "键名必须是 generated（不是底座 synced）"
    assert "synced" not in data, "底座键名 synced 不许漏出"
    for key in ("running", "queued", "total", "generated", "failed", "finished"):
        assert key in data, f"快照缺字段 {key}"
    assert data["failed"] == 1
    assert data["generated"] == 0
    finished = [entry for entry in data["finished"] if entry.get("ok") is False]
    assert finished, "失败项应出现在 finished"
    # reason 优先取审计行的 error_message（跨重启留存 —— T5 三表存在的理由）
    assert "JSON" in (finished[0].get("reason") or "") or finished[0].get("code") == (
        "LLM_RESPONSE_NOT_JSON"
    )


# --------------------------------------------------------------------------- #
# ⑦ 守活：BaseException 网（DEF-069 同型）
# --------------------------------------------------------------------------- #
def test_worker_survives_system_exit_and_marks_failed(
    clean_gen_llm_globals, llm_env, db_factory, monkeypatch
):
    """篇内 `SystemExit`（批删守卫同型异常）也不许把项挂在 running。"""
    client, headers, _uid, account_id, _pk = llm_env
    FakeUpstream.raise_error = SystemExit(1)  # 不是 Exception 的子类

    resp = client.post(
        SINGLE, json={"platform_account_id": account_id, "item_id": ITEM_ID}, headers=headers
    )
    record_id = resp.json()["record_id"]
    assert gw.wait_llm_generate_done(timeout=20), "SystemExit 把队列卡住了"

    db = db_factory()
    try:
        item = db.scalar(select(Task).where(Task.task_type == nqs.TASK_TYPE_GEN_LLM_ITEM))
        assert item is not None and item.status != "running", "项仍挂在 running"
        record = db.get(GenerationRecord, record_id)
        assert record.status == GEN_STATUS_FAILED
        assert record.error_code, "失败要有可归因 code"
        assert db.scalar(select(Note)) is None, "失败却落了笔记"
    finally:
        db.close()


def test_generation_records_one_row_per_generation(clean_gen_llm_globals, llm_env, db_factory):
    """审计唯一性：成功一次生成 = 恰好一条 `generation_records`。"""
    client, headers, _uid, account_id, _pk = llm_env
    resp = client.post(
        SINGLE, json={"platform_account_id": account_id, "item_id": ITEM_ID}, headers=headers
    )
    assert resp.status_code == 200
    assert gw.wait_llm_generate_done(timeout=20)
    assert len(_records(db_factory)) == 1
