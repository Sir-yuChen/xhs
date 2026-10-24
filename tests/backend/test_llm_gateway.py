"""T1：llm_gateway_configs CRUD + test 端点测试（设计 17 §7.1 / §12）。

覆盖：
- CRUD（列表 / 创建 / 编辑 / 删除）
- 令牌：加密落库（库里不是明文）、响应只给 has_* 布尔、永不回传明文
- 唯一约束：`(user_id, name)` 冲突 409 LLM_GATEWAY_NAME_EXISTS
- 越权：别人的配置 404
- 编辑时改 base_url ⇒ healthy 自动置 False
- v1.7：`local_db_path` 进出参均已移除（extra="forbid" 传了即 422）
- v1.7：删除不再级联清理废弃表 `llm_gateway_snapshots`（历史行保留），台账日志保留
- test 端点：v1.7 纯 OpenAI 兼容（`/models` 判活，无 `/api/status`）/ 无令牌跳过探测
- **v1.8（设计 17 §7.1.0）**：`base_url` = 模型调用地址本身，**原值落库**、后端**不补剥 `/v1`**
  （守卫：带 `/v1` 的地址 ⇒ `…/v1/models`；根地址 ⇒ `…/models`，均无 `/v1/v1/`）
- v1.4 工作平台维度：路径挂 /api/llm/（不挂 /xhs/），旧路径 404 守卫
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.core.security import decrypt_text
from backend.app.core.time import shanghai_now
from backend.app.models import LlmGatewayAlert, LlmGatewayConfig, User


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration


def _auth_header(api_client) -> dict:
    """注册 + 登录拿 bearer token。

    ⚠️ **v1.1.0**：新注册用户是 `status=pending`（待审）且没有任何权限码。
    这些用例被测的是网关/用量业务逻辑，不是权限，所以直接把测试用户提成
    **admin**（admin 的权限由代码派生 `ALL_PERMS`，不查 `role_menus`）——
    否则每条都要先造模板行授权，跑题且脆弱。

    权限本身的验证在 `test_system_api.py` / `test_rbac_service.py`，那里用
    真实的 pending → 批准 → 继承模板行路径。
    """
    api_client.post("/api/auth/register", json={"username": "llm_tester", "password": "testpass123"})
    _promote_to_admin(api_client, "llm_tester")
    resp = api_client.post("/api/auth/login", json={"username": "llm_tester", "password": "testpass123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _promote_to_admin(api_client, username: str) -> None:
    """把测试用户提成 admin 并置 `normal`（测试库没有播种 `user` 模板行）。"""
    from sqlalchemy import select

    from backend.app.core.database import SessionLocal
    from backend.app.models.user import User

    db = SessionLocal()
    try:
        row = db.scalar(select(User).where(User.username == username))
        if row is not None:
            row.role = "admin"
            row.status = "normal"
            db.commit()
    finally:
        db.close()


def _other_auth(api_client) -> dict:
    """另一个用户（越权测试用）。"""
    api_client.post("/api/auth/register", json={"username": "llm_other", "password": "testpass123"})
    resp = api_client.post("/api/auth/login", json={"username": "llm_other", "password": "testpass123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_create_and_serialize_hides_tokens(api_client, db_factory):
    headers = _auth_header(api_client)
    resp = api_client.post(
        "/api/llm/gateway-configs",
        json={
            "name": "接入中转站",
            "base_url": "http://127.0.0.1:3000",
            "access_token": "sk-gateway-secret",
            # ⚠️ v2.7/v2.8：platform / model_type 下沉到模型行；`default_model` 已删。
            # 模型随服务商一并创建（§13.2.9.5）。
            "models": [
                {"model_name": "deepseek-v4-flash", "model_type": "text", "platform": ["xhs"]}
            ],
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["has_access_token"] is True
    # 令牌字段永不回传
    assert "access_token" not in body
    # v1.7：local_db_path 已从出参移除
    assert "local_db_path" not in body
    assert body["healthy"] is False
    # v1.9：服务编码系统生成（非空、唯一）
    assert body["provider_key"]
    # ⚠️ v2.7：platform / model_type **不再在服务商出参**（已下沉模型层）——
    # 传了反而 422（extra="forbid"）。
    assert "platform" not in body
    assert "model_type" not in body
    # ⚠️ **v2.8**：`default_model` 出参移除（列已删，§13.2.10）。
    assert "default_model" not in body
    # v2.7：模型行随创建落库并回传（结构数组）
    assert [m["model_name"] for m in body["models"]] == ["deepseek-v4-flash"]
    assert body["models"][0]["model_type"] == "text"
    # v2.28：`platform` 由标识数组改为**引用计数**（`{"xhs": 1}`）。
    # 入参仍是 `["xhs"]`（用户口径「使用平台」），出参是计数 —— 计数 = 业务点引用个数。
    assert body["models"][0]["platform"] == {"xhs": 1}
    # 同步下发的键列表（前端渲染用），顺序稳定
    assert body["models"][0]["platforms"] == ["xhs"]

    # 库里是加密文本，不是明文
    with db_factory() as db:
        row = db.scalar(select(LlmGatewayConfig).where(LlmGatewayConfig.id == body["id"]))
        assert row is not None
        assert "sk-gateway-secret" not in row.access_token
        assert decrypt_text(row.access_token) == "sk-gateway-secret"


def test_create_seeds_default_alerts(api_client, db_factory):
    """建网关 ⇒ 自动补种**默认预警规则**（设计 17 §4.5，2026-10-02）。

    口径（用户裁决：**预警规则设置后即可生效**）：
    - 播种源 = `config/initial_data/llm_gateway_alerts.json`，**恰好 2 条**；
    - 两条均 `enabled=true`（落库即生效，不是「先摆着不开」）；
    - 门槛保守：`fail_rate gt 0.5`（24h 失败率过半）+ `health eq 0`（探测失败）；
    - **不播 `calls` 绝对量阈值**（各网关用量量级不同，播绝对值必然误报）。
    """
    headers = _auth_header(api_client)
    resp = api_client.post(
        "/api/llm/gateway-configs",
        json={
            "name": "带种子的网关",
            "base_url": "http://127.0.0.1:3000",
            "access_token": "sk-seed",
            "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}],
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    gateway_id = resp.json()["id"]

    with db_factory() as db:
        rows = db.scalars(
            select(LlmGatewayAlert).where(LlmGatewayAlert.gateway_id == gateway_id)
        ).all()
        # 恰好两条默认规则，且都已开启
        assert len(rows) == 2, [r.metric for r in rows]
        assert all(r.enabled is True for r in rows)
        keys = {(r.metric, r.operator, float(r.threshold)) for r in rows}
        assert keys == {("fail_rate", "gt", 0.5), ("health", "eq", 0.0)}
        # 绝不播 calls 绝对量阈值
        assert all(r.metric != "calls" for r in rows)


def test_create_seeding_is_idempotent(api_client, db_factory):
    """播种**幂等**：对同一网关重复调用不产生重复行（设计 17 §4.5）。

    同时守住「用户改过/删过的规则不还魂」：删掉一条后再补种，只补回缺失的那条。
    """
    from backend.app.services import llm_gateway_monitor_service

    headers = _auth_header(api_client)
    resp = api_client.post(
        "/api/llm/gateway-configs",
        json={
            "name": "幂等网关",
            "base_url": "http://127.0.0.1:3000",
            "access_token": "sk-idem",
            "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}],
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    gateway_id = resp.json()["id"]

    # 直接再调一次播种 ⇒ 一条都不新建
    with db_factory() as db:
        created = llm_gateway_monitor_service.ensure_default_alerts_for_gateway(
            db, user_id=1, gateway_id=gateway_id
        )
        assert created == 0
        assert (
            len(db.scalars(select(LlmGatewayAlert).where(LlmGatewayAlert.gateway_id == gateway_id)).all())
            == 2
        )

    # 删掉 health 那条 ⇒ 再播种只补回它（fail_rate 不重复插）
    with db_factory() as db:
        db.query(LlmGatewayAlert).filter(
            LlmGatewayAlert.gateway_id == gateway_id,
            LlmGatewayAlert.metric == "health",
        ).delete()
        db.commit()
        created = llm_gateway_monitor_service.ensure_default_alerts_for_gateway(
            db, user_id=1, gateway_id=gateway_id
        )
        assert created == 1
        rows = db.scalars(select(LlmGatewayAlert).where(LlmGatewayAlert.gateway_id == gateway_id)).all()
        assert len(rows) == 2
        assert {r.metric for r in rows} == {"fail_rate", "health"}


def test_loader_ignores_bad_alert_file(monkeypatch):
    """播种源缺失 / 坏 ⇒ 返回 0 条，**不臆造默认值**（与定时任务种子同纪律）。"""
    from backend.app.core import initial_data

    monkeypatch.setattr(initial_data, "_load_payload", lambda _name: None)
    assert initial_data.load_initial_gateway_alerts() == []

    monkeypatch.setattr(initial_data, "_load_payload", lambda _name: {"gateway_alerts": "not-a-list"})
    assert initial_data.load_initial_gateway_alerts() == []


def test_create_assigns_unique_provider_code(api_client):
    """v1.9：服务编码**系统生成、本用户内唯一**（D1 + D2）。

    ⚠️ 本用例**取代** `test_create_duplicate_name_409` 的「同 payload 连建两条」形态
    （v1.9 每条都必带 platform 等必填字段，且服务编码各不相同）。
    重名仍须 409 —— 保留该断言。
    """
    headers = _auth_header(api_client)
    payload = {
        "name": "dup",
        "base_url": "http://127.0.0.1:3000",
        "access_token": "sk-t",
        "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}],
    }
    first = api_client.post("/api/llm/gateway-configs", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    # 重名 ⇒ 409（与 v1.9 之前一致）
    resp = api_client.post("/api/llm/gateway-configs", json=payload, headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "LLM_GATEWAY_NAME_EXISTS"

    # 换名 ⇒ 服务编码不同（系统生成、不撞车）
    second = api_client.post(
        "/api/llm/gateway-configs",
        json={**payload, "name": "dup2"},
        headers=headers,
    )
    assert second.status_code == 201, second.text
    assert first.json()["provider_key"] != second.json()["provider_key"]


def test_create_rejects_missing_required_fields(api_client):
    """必填项校验（v1.9 令牌必填；v2.7 起类型/平台必填**下沉到模型行**）。

    ⚠️ 缺值 ⇒ 结构化错误，**不静默用默认值补齐** —— 补齐等于替用户编造一份凭据，
    第一次调用才会发现是错的。

    ⚠️ **v2.8（§13.2.10）**：`default_model` 与 `LLM_PROVIDER_MODEL_REQUIRED` 已删除
    （列已删）⇒ 本用例**取代** v1.9 的「默认模型必填」断言；模型必填性现由
    **模型行**的 `model_type` 必填承担（`LLM_PROVIDER_MODEL_TYPE_REQUIRED`）。
    """
    headers = _auth_header(api_client)
    base = {
        "name": "n",
        "base_url": "http://127.0.0.1:3000",
        "access_token": "sk-t",
        "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}],
    }

    # 服务商层 `platform` 已退场 ⇒ 传了即 422（extra="forbid"，不再有 400 PLATFORM_REQUIRED）
    r = api_client.post("/api/llm/gateway-configs", json={**base, "platform": "xhs"}, headers=headers)
    assert r.status_code == 422
    # 服务商层 `model_type` 已退场 ⇒ 传了即 422
    r = api_client.post("/api/llm/gateway-configs", json={**base, "model_type": "text"}, headers=headers)
    assert r.status_code == 422
    # v2.8：服务商层 `default_model` 已退场 ⇒ 传了即 422（不再是 400 MODEL_REQUIRED）
    r = api_client.post("/api/llm/gateway-configs", json={**base, "default_model": "m"}, headers=headers)
    assert r.status_code == 422
    # 令牌空
    r = api_client.post("/api/llm/gateway-configs", json={**base, "access_token": ""}, headers=headers)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "LLM_PROVIDER_TOKEN_REQUIRED"
    # 模型行的 model_type 空 ⇒ 400（类型必填下沉到模型行）
    r = api_client.post(
        "/api/llm/gateway-configs",
        json={**base, "models": [{"model_name": "m", "model_type": "", "platform": ["xhs"]}]},
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "LLM_PROVIDER_MODEL_TYPE_REQUIRED"
    # 模型行的 platform 非法 ⇒ 400
    r = api_client.post(
        "/api/llm/gateway-configs",
        json={**base, "models": [{"model_name": "m", "model_type": "text", "platform": ["bogus"]}]},
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "LLM_PROVIDER_PLATFORM_REQUIRED"


def test_provider_key_not_editable_via_patch_422(api_client):
    """v1.9：服务编码**不可编辑** —— PATCH 带它一律 **422**（服务端约束，前端绕不过）。"""
    headers = _auth_header(api_client)
    created = api_client.post(
        "/api/llm/gateway-configs",
        json={"name": "locked", "base_url": "http://127.0.0.1:3000",
              "access_token": "sk-t", "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}]},
        headers=headers,
    ).json()

    resp = api_client.patch(
        f"/api/llm/gateway-configs/{created['id']}",
        json={"provider_key": "HACKED-0000"},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text

    # 原值未变
    after = api_client.get("/api/llm/gateway-configs?page=1&page_size=20", headers=headers).json()["items"][0]
    assert after["provider_key"] == created["provider_key"]


def test_provider_platforms_enum_endpoint(api_client):
    """v1.9：使用平台枚举由**后端下发**（D3），前端不许写死选项。"""
    headers = _auth_header(api_client)
    resp = api_client.get("/api/llm/providers/platforms", headers=headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    values = [i["value"] for i in items]
    assert values[0] == "xhs"  # xhs 在前（本项目主平台）
    assert set(values) == {"xhs", "douyin", "kuaishou", "weibo", "xianyu", "taobao"}
    assert all(i["label"] for i in items)  # 中文名必须齐


def test_delete_is_gone_410_and_keeps_row(api_client, db_factory):
    """v1.9：删除功能**退场** ⇒ 410（不是 200，也不是 404），且**数据一行不删**。

    ⚠️ 本用例**取代** `test_delete_no_longer_touches_snapshots`（原断言 200 + 真删）。
    为什么 410 而非 404：404 无法区分「id 不存在」与「功能已退场」。
    """
    headers = _auth_header(api_client)
    created = api_client.post(
        "/api/llm/gateway-configs",
        json={"name": "nodelete", "base_url": "http://127.0.0.1:3000",
              "access_token": "sk-t", "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}]},
        headers=headers,
    ).json()

    resp = api_client.delete(f"/api/llm/gateway-configs/{created['id']}", headers=headers)
    assert resp.status_code == 410, resp.text
    assert resp.json()["detail"]["code"] == "LLM_PROVIDER_DELETE_GONE"

    # 数据仍在（服务编码已被业务引用，删行会让引用悬空）
    with db_factory() as db:
        assert db.get(LlmGatewayConfig, created["id"]) is not None


def test_list_only_own(api_client):
    headers = _auth_header(api_client)
    other = _other_auth(api_client)
    api_client.post(
        "/api/llm/gateway-configs",
        json={"name": "mine", "base_url": "http://127.0.0.1:3000", "access_token": "sk-t", "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}]},
        headers=headers,
    )
    api_client.post(
        "/api/llm/gateway-configs",
        json={"name": "theirs", "base_url": "http://127.0.0.1:3000", "access_token": "sk-t", "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}]},
        headers=other,
    )
    resp = api_client.get("/api/llm/gateway-configs?page=1&page_size=50", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    names = [i["name"] for i in items]
    #: ⚠️ 2026-10-14：注册即**播种 6 家国内服务商**（需求 R4）⇒ 新用户自带种子，
    #: 「列表里只有我建的那条」不再成立。**隔离的判据不是「只有一条」**，
    #: 而是「**我的**在、**别人的**不在」—— 改成这个，判据反而更贴规则本身。
    assert "mine" in names, f"自己的服务商应可见：{names}"
    assert "theirs" not in names, f"别的用户的服务商绝不能出现：{names}"


def test_cross_user_404(api_client):
    headers = _auth_header(api_client)
    other = _other_auth(api_client)
    created = api_client.post(
        "/api/llm/gateway-configs",
        json={"name": "secret", "base_url": "http://127.0.0.1:3000", "access_token": "sk-t", "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}]},
        headers=headers,
    ).json()
    resp = api_client.patch(
        f"/api/llm/gateway-configs/{created['id']}",
        json={"enabled": False},
        headers=other,
    )
    assert resp.status_code == 404
    resp = api_client.delete(f"/api/llm/gateway-configs/{created['id']}", headers=other)
    assert resp.status_code == 404


def test_update_base_url_resets_healthy(api_client):
    headers = _auth_header(api_client)
    created = api_client.post(
        "/api/llm/gateway-configs",
        json={"name": "cfg", "base_url": "http://127.0.0.1:3000", "access_token": "sk-t", "models": [{"model_name": "m1", "model_type": "text", "platform": ["xhs"]}]},
        headers=headers,
    ).json()
    # 假装探活过
    api_client.patch(
        f"/api/llm/gateway-configs/{created['id']}",
        json={"enabled": True},
        headers=headers,
    )
    # 改 base_url ⇒ healthy 自动置 False
    resp = api_client.patch(
        f"/api/llm/gateway-configs/{created['id']}",
        json={"base_url": "http://127.0.0.1:3999"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["base_url"] == "http://127.0.0.1:3999"
    assert resp.json()["healthy"] is False


def test_update_token_blank_keeps_old(api_client, db_factory):
    headers = _auth_header(api_client)
    created = api_client.post(
        "/api/llm/gateway-configs",
        json={"name": "tok", "base_url": "http://127.0.0.1:3000", "access_token": "sk-original", "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}]},
        headers=headers,
    ).json()
    resp = api_client.patch(
        f"/api/llm/gateway-configs/{created['id']}",
        json={"access_token": ""},
        headers=headers,
    )
    assert resp.status_code == 200
    with db_factory() as db:
        row = db.get(LlmGatewayConfig, created["id"])
        assert decrypt_text(row.access_token) == "sk-original"  # 留空 = 保持不变


# ⚠️ ~~test_delete_no_longer_touches_snapshots~~ **v1.9 删除**：删除功能已退场（410 墓碑），
# 该用例断言「200 + 真删 + 快照孤儿保留」，现由 `test_delete_is_gone_410_and_keeps_row` 取代。
# 原句保留于设计 17 §7.1 的旧口径对照表。

def test_local_db_path_rejected(api_client):
    """v1.7 守卫：`local_db_path` 已从请求模型移除（extra="forbid" ⇒ 传了即 422）。

    防「New API 特化字段复活」（设计 17 §3.8 标「v1.7 彻底弃用」）。
    """
    headers = _auth_header(api_client)
    resp = api_client.post(
        "/api/llm/gateway-configs",
        json={"name": "legacy", "base_url": "http://127.0.0.1:3000", "local_db_path": "data/newapi/new-api.db"},
        headers=headers,
    )
    assert resp.status_code == 422

    created = api_client.post(
        "/api/llm/gateway-configs",
        json={"name": "legacy2", "base_url": "http://127.0.0.1:3000", "access_token": "sk-t", "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}]},
        headers=headers,
    ).json()
    resp = api_client.patch(
        f"/api/llm/gateway-configs/{created['id']}",
        json={"local_db_path": "x.db"},
        headers=headers,
    )
    assert resp.status_code == 422


def test_test_endpoint_unreachable_400(api_client):
    """v1.7：可达性判据 = `GET {base}/v1/models`（纯 OpenAI 兼容，无 /api/status）。"""
    headers = _auth_header(api_client)
    created = api_client.post(
        "/api/llm/gateway-configs",
        json={
            "name": "dead",
            "base_url": "http://127.0.0.1:59999",  # 不可达端口
            "access_token": "sk-x",
            "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}],
        },
        headers=headers,
    ).json()
    resp = api_client.post(f"/api/llm/gateway-configs/{created['id']}/test", json={}, headers=headers)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "LLM_GATEWAY_UNREACHABLE"


def test_test_endpoint_no_token_skips_probe(api_client, db_factory):
    """v1.7：无令牌 ⇒ 跳过探测、只置健康（不臆造失败，message 说明原因）。

    ⚠️ **v1.9 订正**：令牌**新建时必填**（400 `LLM_PROVIDER_TOKEN_REQUIRED`），
    不能再「用空令牌创建」造这个场景 —— 改为**建完直接清空库里的令牌列**
    （模拟存量数据 / 令牌被外部清掉）。探测逻辑本身未变（无令牌即跳过）。
    """
    headers = _auth_header(api_client)
    created = api_client.post(
        "/api/llm/gateway-configs",
        json={"name": "notoken", "base_url": "http://127.0.0.1:59999",
              "access_token": "sk-t", "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}]},
        headers=headers,
    ).json()
    # v1.9：清空令牌列以复现「无令牌」状态（绕过新建必填）
    # ⚠️ 夹具 Session `expire_on_commit=True` ⇒ commit 后属性过期；refresh 取回
    # （否则下一行读 `row.access_token` 会触发惰性加载，虽在本用例无害，但端点里
    # 的 `db.get()` 拿到的是**另一个** Session，不受这里 commit 过期的影响 ——
    # 此处 refresh 只为保证「清空确实落库」这一断言本身可读）。
    with db_factory() as db:
        row = db.get(LlmGatewayConfig, created["id"])
        row.access_token = ""
        db.commit()
        db.refresh(row)
        assert row.access_token == ""

    resp = api_client.post(f"/api/llm/gateway-configs/{created['id']}/test", json={}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["models"] == []
    assert "未配置访问令牌" in body["message"]
    with db_factory() as db:
        assert db.get(LlmGatewayConfig, created["id"]).healthy is True


def test_test_endpoint_models_path_only(api_client, monkeypatch):
    """v1.8 守卫：test 端点打 `{base_url}/models`（`/v1` 由用户地址自带，**不得**后端硬拼）。

    两件事一起守：
    ① 请求 URL = base_url + `/models`（用户填 `http://127.0.0.1:3000/v1` ⇒ `…/v1/models`）；
    ② 不再打 New API 的 `/api/status`（v1.7 移除的特化探测）。
    用假 requests 记录实际请求 URL：把 `/v1` 硬拼回去、或把 `/api/status` 加回来都会被抓住。
    """
    import requests as real_requests

    called_urls: list[str] = []

    class _Resp:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload if payload is not None else {}

        def json(self):
            return self._payload

    def _fake_get(url, **kwargs):
        called_urls.append(url)
        return _Resp(200, {"data": [{"id": "m1"}, {"id": "m2"}]})

    def _fake_post(url, **kwargs):
        called_urls.append(url)
        return _Resp(500)

    monkeypatch.setattr(real_requests, "get", _fake_get)
    monkeypatch.setattr(real_requests, "post", _fake_post)

    headers = _auth_header(api_client)
    # ⚠️ 地址**自带 `/v1`**（真实服务商形态）⇒ 期望请求 `…/v1/models`，而非 `/v1/v1/models`
    created = api_client.post(
        "/api/llm/gateway-configs",
        json={"name": "probe", "base_url": "http://127.0.0.1:3000/v1", "access_token": "sk-x", "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}]},
        headers=headers,
    ).json()
    resp = api_client.post(f"/api/llm/gateway-configs/{created['id']}/test", json={}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["models"] == ["m1", "m2"]
    assert "http://127.0.0.1:3000/v1/models" in called_urls, called_urls
    assert not any("/v1/v1/" in u for u in called_urls), called_urls
    assert not any("/api/status" in u for u in called_urls), called_urls


def test_test_endpoint_root_base_url_no_v1_appended(api_client, monkeypatch):
    """v1.8 守卫：地址**不带 `/v1`** 时，后端**也不补** `/v1`（用户填什么就是什么）。

    自托管网关（New API 等）常填根地址 `http://127.0.0.1:3000` —— 此时实际请求必须
    是 `http://127.0.0.1:3000/models`，**不得**被后端擅自补成 `/v1/models`。
    """
    import requests as real_requests

    called_urls: list[str] = []

    class _Resp:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload if payload is not None else {}

        def json(self):
            return self._payload

    def _fake_get(url, **kwargs):
        called_urls.append(url)
        return _Resp(200, {"data": []})

    monkeypatch.setattr(real_requests, "get", _fake_get)
    monkeypatch.setattr(real_requests, "post", lambda url, **kw: _Resp(500))

    headers = _auth_header(api_client)
    created = api_client.post(
        "/api/llm/gateway-configs",
        json={"name": "rootaddr", "base_url": "http://127.0.0.1:3000", "access_token": "sk-x", "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}]},
        headers=headers,
    ).json()
    assert created["base_url"] == "http://127.0.0.1:3000", created
    resp = api_client.post(f"/api/llm/gateway-configs/{created['id']}/test", json={}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert "http://127.0.0.1:3000/models" in called_urls, called_urls
    assert not any("/v1/models" in u for u in called_urls), called_urls


def test_base_url_stored_verbatim(api_client):
    """v1.8 守卫：`base_url` **原值落库**（仅 strip + 去末尾斜杠），不补剥 `/v1`。

    ⚠️ 这是本次缺陷（discovery 404）的根因守卫：旧口径「后端硬拼 `/v1`」逼着用户
    填不带 `/v1` 的地址，而真实服务商文档普遍要求带 `/v1` ⇒ 用户按文档填就 404。
    """
    headers = _auth_header(api_client)
    created = api_client.post(
        "/api/llm/gateway-configs",
        json={"name": "verbatim", "base_url": "  https://discovery-api.intern-ai.org.cn/v1  ", "access_token": "sk-t", "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}]},
        headers=headers,
    ).json()
    assert created["base_url"] == "https://discovery-api.intern-ai.org.cn/v1", created

    # 末尾斜杠只做拼接语法层的归一（不改变指向的地址）
    trailing = api_client.post(
        "/api/llm/gateway-configs",
        json={"name": "trailing", "base_url": "https://api.deepseek.com/v1/", "access_token": "sk-t", "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}]},
        headers=headers,
    ).json()
    assert trailing["base_url"] == "https://api.deepseek.com/v1", trailing


def test_stats_remote_gateway_empty_state(api_client):
    """stats 端点（v1.6）：非本机 base_url 也支持统计（读本地台账），空台账 ⇒ 空态。

    v1.6 取代 v1.3「非本机 400 LLM_GATEWAY_REMOTE_UNSUPPORTED」—— 用量真相源改本地
    台账 `llm_gateway_usage_logs`，与 base_url 是否本机彻底脱钩。空台账返回
    available=False（空态，不是错误），不是 400。
    """
    headers = _auth_header(api_client)
    created = api_client.post(
        "/api/llm/gateway-configs",
        json={
            "name": "remote",
            "base_url": "https://discovery-api.intern-ai.org.cn",  # 远程三方网关
            "access_token": "sk-t",
            "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}],
        },
        headers=headers,
    ).json()
    resp = api_client.get(f"/api/llm/gateway-configs/{created['id']}/stats", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["today"]["calls"] == 0


def test_stats_empty_ledger_empty_state(api_client):
    """stats 端点：本机但台账无记录 ⇒ 空态（available=False），不 500。"""
    headers = _auth_header(api_client)
    created = api_client.post(
        "/api/llm/gateway-configs",
        json={
            "name": "nodb",
            "base_url": "http://127.0.0.1:3000",
            "access_token": "sk-t",
            "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}],
        },
        headers=headers,
    ).json()
    resp = api_client.get(f"/api/llm/gateway-configs/{created['id']}/stats", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["today"]["calls"] == 0


def test_legacy_xhs_path_is_gone(api_client):
    """v1.4 工作平台维度守卫（设计 17 §0 修订 ⑦）：网关已脱离 /xhs/ 平台前缀。

    旧路径 `/api/xhs/llm/gateway-configs` 必须整体下线（404，不是 401——
    用带有效 token 的请求打它，证明是「路由不存在」而不是「没鉴权」）。
    防「同一语义留两份实现 / 旧路由复活」的回归（DEF-044 同型）。
    """
    headers = _auth_header(api_client)
    assert api_client.get("/api/llm/gateway-configs", headers=headers).status_code == 200
    resp = api_client.get("/api/xhs/llm/gateway-configs", headers=headers)
    assert resp.status_code == 404


# ───────────────────────── v1.6：令牌两级映射（§7.1a）─────────────────────────


def _create_gateway(api_client, headers, name="gw"):
    """建一条服务商（v1.9 令牌必填；v2.7 起类型/平台随**模型行**必填）。

    ⚠️ v2.8：服务商层 `default_model` 已删（§13.2.10）—— 模型真相源 = 模型行，
    故此处改为随创建带一条 `deepseek-v4-flash`（text / xhs）模型行。
    """
    return api_client.post(
        "/api/llm/gateway-configs",
        json={
            "name": name,
            "base_url": "http://127.0.0.1:3000",
            "access_token": "sk-test-token",
            "models": [
                {"model_name": "deepseek-v4-flash", "model_type": "text", "platform": ["xhs"]}
            ],
        },
        headers=headers,
    ).json()


# ───────────── v1.9：令牌映射整体退场（§7.1a ⇒ 410 墓碑）─────────────


def test_token_mapping_endpoints_are_gone_410(api_client):
    """v1.9：4 个令牌映射端点**退场**，一律 410（不是 404、不是 201）。

    ⚠️ 为什么是 410 而不是删掉路由：删掉会回 404，而 404 无法区分
    「这个 id 不存在」与「该功能已退场」（设计 17 §7.1a / ADR-021 惯例）。
    ⚠️ 本用例**取代** `test_token_mapping_crud` 与 `test_token_mapping_unique_and_cross_user`
    （原用例断言 201 + 字段 + 409，随表退场已不成立）。
    """
    headers = _auth_header(api_client)

    # GET / POST 列表与新增
    assert api_client.get("/api/llm/gateway-tokens", headers=headers).status_code == 410
    assert (
        api_client.post(
            "/api/llm/gateway-tokens",
            json={"gateway_id": 1, "platform": ["xhs"], "function": "generate", "token_name": "t", "model_type": "text"},
            headers=headers,
        ).status_code
        == 410
    )
    # PATCH / DELETE 单条
    assert (
        api_client.patch("/api/llm/gateway-tokens/1", json={"token_name": "t"}, headers=headers).status_code
        == 410
    )
    assert api_client.delete("/api/llm/gateway-tokens/1", headers=headers).status_code == 410

    # 结构化的 code 让前端能区分「功能没了」与「路径写错」
    body = api_client.get("/api/llm/gateway-tokens", headers=headers).json()
    assert body["detail"]["code"] == "LLM_GATEWAY_TOKENS_GONE"


# ───────────────────────── v1.6：预警规则（§7.1b）─────────────────────────


def test_alert_crud_and_validation(api_client):
    headers = _auth_header(api_client)
    gw = _create_gateway(api_client, headers)

    # 创建用量阈值规则
    resp = api_client.post(
        "/api/llm/gateway-alerts",
        json={"gateway_id": gw["id"], "platform": "xhs", "function": "generate", "metric": "calls", "operator": "gt", "threshold": 100},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    alert = resp.json()
    assert alert["metric"] == "calls"
    assert alert["threshold"] == 100

    # 非法 metric ⇒ 400
    resp = api_client.post(
        "/api/llm/gateway-alerts",
        json={"gateway_id": gw["id"], "metric": "bogus", "operator": "gt", "threshold": 1},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "LLM_ALERT_METRIC_UNSUPPORTED"

    # 非法 operator ⇒ 400
    resp = api_client.post(
        "/api/llm/gateway-alerts",
        json={"gateway_id": gw["id"], "metric": "calls", "operator": "ne", "threshold": 1},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "LLM_ALERT_OPERATOR_UNSUPPORTED"

    # 编辑阈值
    resp = api_client.patch(
        f"/api/llm/gateway-alerts/{alert['id']}",
        json={"threshold": 200},
        headers=headers,
    )
    assert resp.json()["threshold"] == 200

    # 删除
    resp = api_client.delete(f"/api/llm/gateway-alerts/{alert['id']}", headers=headers)
    assert resp.status_code == 200


def test_alert_cross_user_404(api_client):
    headers = _auth_header(api_client)
    other = _other_auth(api_client)
    gw = _create_gateway(api_client, headers)
    alert = api_client.post(
        "/api/llm/gateway-alerts",
        json={"gateway_id": gw["id"], "metric": "calls", "operator": "gt", "threshold": 1},
        headers=headers,
    ).json()
    resp = api_client.patch(f"/api/llm/gateway-alerts/{alert['id']}", json={"enabled": False}, headers=other)
    assert resp.status_code == 404


# ───────────── v2.3：预警三级作用域（全网关 / 服务商 / 具体模型）─────────────


def _create_alert(api_client, headers, **overrides):
    """建一条预警规则（默认全网关作用域，可覆盖任意字段）。"""
    payload = {
        "gateway_id": None,
        "platform": "",
        "function": "",
        "model_name": "",
        "metric": "calls",
        "operator": "gt",
        "threshold": 1,
    }
    payload.update(overrides)
    return api_client.post("/api/llm/gateway-alerts", json=payload, headers=headers)


def test_alert_scope_all_gateway(api_client):
    """全网关作用域：`gateway_id` 省略/null + `model_name=""` ⇒ scope=`all`。"""
    headers = _auth_header(api_client)
    resp = _create_alert(api_client, headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["gateway_id"] is None
    assert body["model_name"] == ""
    assert body["scope"] == "all"


def test_alert_scope_provider(api_client):
    """服务商作用域：`gateway_id`=id + `model_name=""` ⇒ scope=`gateway`。"""
    headers = _auth_header(api_client)
    gw = _create_gateway(api_client, headers)
    resp = _create_alert(api_client, headers, gateway_id=gw["id"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["gateway_id"] == gw["id"]
    assert body["model_name"] == ""
    assert body["scope"] == "gateway"


def test_alert_scope_model(api_client):
    """具体模型作用域：`gateway_id`=id + `model_name` 非空 ⇒ scope=`model`。"""
    headers = _auth_header(api_client)
    gw = _create_gateway(api_client, headers)
    resp = _create_alert(api_client, headers, gateway_id=gw["id"], model_name="deepseek-v4-pro")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["gateway_id"] == gw["id"]
    assert body["model_name"] == "deepseek-v4-pro"
    assert body["scope"] == "model"


def test_alert_scope_conflict_all_plus_model_400(api_client):
    """全网关 + 指定模型 ⇒ 400 `LLM_ALERT_SCOPE_CONFLICT`（唯一非法组合）。

    全网关不绑单个服务商，而「某个模型」必须依附某个服务商才有意义；
    两者并存只会产生一条语义不明、永远不命中的规则 —— 提前拦掉。
    """
    headers = _auth_header(api_client)
    resp = _create_alert(api_client, headers, gateway_id=None, model_name="deepseek-v4-pro")
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "LLM_ALERT_SCOPE_CONFLICT"


def test_alert_scope_gateway_id_not_owned_404(api_client):
    """服务商作用域指向**别人**的服务商 ⇒ 404（不泄露他人网关）。"""
    headers = _auth_header(api_client)
    other = _other_auth(api_client)
    gw = _create_gateway(api_client, headers)
    resp = _create_alert(api_client, other, gateway_id=gw["id"])
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "LLM_GATEWAY_NOT_FOUND"


def test_alert_list_without_gateway_filter_returns_all_scopes(api_client, db_factory):
    """列表**不带 gateway_id 过滤** ⇒ 三种作用域全部返回（含全网关的 NULL 行）。

    ⚠️ 关键回归点：全网关规则 `gateway_id IS NULL`，若列表按某个 gateway_id 过滤，
    这些规则会**永远看不到**（用户以为规则消失了）。前端刷新页时必须不带过滤。
    """
    headers = _auth_header(api_client)
    gw = _create_gateway(api_client, headers)
    # 建服务商会按用户自动补种默认规则（§4.5）⇒ 先清掉，只留本用例自己造的三种作用域。
    with db_factory() as db:
        db.query(LlmGatewayAlert).delete()
        db.commit()
    _create_alert(api_client, headers)  # all
    _create_alert(api_client, headers, gateway_id=gw["id"])  # gateway
    _create_alert(api_client, headers, gateway_id=gw["id"], model_name="m-1")  # model

    resp = api_client.get("/api/llm/gateway-alerts", headers=headers)
    assert resp.status_code == 200
    scopes = sorted(i["scope"] for i in resp.json()["items"])
    assert scopes == ["all", "gateway", "model"]


def test_alert_list_gateway_filter_excludes_null_rows(api_client, db_factory):
    """带 `gateway_id` 过滤 ⇒ 只剩该服务商的行；全网关（NULL）行被排除。

    这是过滤的**定义行为**（不是缺陷）；本用例把它钉住，避免有人误把它当 bug「修掉」。
    """
    headers = _auth_header(api_client)
    gw = _create_gateway(api_client, headers)
    with db_factory() as db:
        db.query(LlmGatewayAlert).delete()  # 清掉自动补种的默认规则
        db.commit()
    _create_alert(api_client, headers)  # all
    _create_alert(api_client, headers, gateway_id=gw["id"])  # gateway

    resp = api_client.get(f"/api/llm/gateway-alerts?gateway_id={gw['id']}", headers=headers)
    assert resp.status_code == 200
    scopes = [i["scope"] for i in resp.json()["items"]]
    assert scopes == ["gateway"]


# ───────────────────────── v1.6：用量展示（§7.5）─────────────────────────


def test_usage_endpoints_empty_state(api_client):
    headers = _auth_header(api_client)
    gw = _create_gateway(api_client, headers)

    # 台账空 ⇒ overview 返回 0（空态，不是错误）
    resp = api_client.get("/api/llm/usage/overview", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["today"]["calls"] == 0

    resp = api_client.get("/api/llm/usage/daily", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == []

    resp = api_client.get("/api/llm/usage/breakdown?dimension=platform", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == []

    # 非法 dimension ⇒ 400
    resp = api_client.get("/api/llm/usage/breakdown?dimension=bogus", headers=headers)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "LLM_USAGE_DIMENSION_UNSUPPORTED"

    # ⚠️ v1.9：`token` 维度**已从白名单移除**（令牌映射退场，设计 17 §7.1a）
    # —— 台账该列保留但不再作为可选维度，留着它只会让用户看到永远为空的维度。
    resp = api_client.get("/api/llm/usage/breakdown?dimension=token", headers=headers)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "LLM_USAGE_DIMENSION_UNSUPPORTED"


def test_usage_aggregates_ledger(api_client, db_factory):
    """台账有记录 ⇒ overview/daily/breakdown 正确聚合（按 platform/function 切片）。"""
    from backend.app.models import LlmGatewayUsageLog

    headers = _auth_header(api_client)
    gw = _create_gateway(api_client, headers)

    with db_factory() as db:
        db.add_all([
            LlmGatewayUsageLog(user_id=1, gateway_id=gw["id"], platform="xhs", function="generate", token_name="xhs-gen", model_name="m1", prompt_tokens=10, completion_tokens=20, latency_ms=100, status="success"),
            LlmGatewayUsageLog(user_id=1, gateway_id=gw["id"], platform="xhs", function="generate", token_name="xhs-gen", model_name="m1", prompt_tokens=5, completion_tokens=5, latency_ms=200, status="failed", error_message="timeout"),
            LlmGatewayUsageLog(user_id=1, gateway_id=gw["id"], platform="douyin", function="review", token_name="dy-rev", model_name="m2", prompt_tokens=1, completion_tokens=1, latency_ms=50, status="success"),
        ])
        db.commit()

    # overview：今日 3 次调用，2 成功 1 失败
    resp = api_client.get("/api/llm/usage/overview", headers=headers)
    body = resp.json()
    assert body["today"]["calls"] == 3
    assert body["today"]["success"] == 2
    assert body["today"]["fail"] == 1
    assert body["today"]["total_tokens"] == 42  # 30 + 10 + 2

    # breakdown 按 platform：xhs 2 次，douyin 1 次
    resp = api_client.get("/api/llm/usage/breakdown?dimension=platform", headers=headers)
    items = {i["key"]: i for i in resp.json()}
    assert items["xhs"]["calls"] == 2
    assert items["douyin"]["calls"] == 1

    # breakdown 按 function
    resp = api_client.get("/api/llm/usage/breakdown?dimension=function", headers=headers)
    items = {i["key"]: i for i in resp.json()}
    assert items["generate"]["calls"] == 2
    assert items["review"]["calls"] == 1


def test_usage_filter_by_model(api_client, db_factory):
    """筛选粒度到「服务商下具体模型」：`model_name` 参数过滤 overview/daily/breakdown。

    §7.5 用户口径「筛选条件应达到服务商下的具体模型，目前只有服务商维度」——
    三个用量端点统一新增 `model_name` 筛选（台账 `LlmGatewayUsageLog.model_name` 直接过滤）。
    """
    from backend.app.models import LlmGatewayUsageLog

    headers = _auth_header(api_client)
    gw = _create_gateway(api_client, headers)

    with db_factory() as db:
        db.add_all([
            LlmGatewayUsageLog(user_id=1, gateway_id=gw["id"], platform="xhs", model_name="m1", prompt_tokens=10, completion_tokens=0, latency_ms=100, status="success"),
            LlmGatewayUsageLog(user_id=1, gateway_id=gw["id"], platform="xhs", model_name="m1", prompt_tokens=5, completion_tokens=0, latency_ms=200, status="success"),
            LlmGatewayUsageLog(user_id=1, gateway_id=gw["id"], platform="xhs", model_name="m2", prompt_tokens=1, completion_tokens=0, latency_ms=50, status="success"),
        ])
        db.commit()

    # 不筛模型 ⇒ 3 次
    resp = api_client.get("/api/llm/usage/overview", headers=headers)
    assert resp.json()["today"]["calls"] == 3

    # 筛 m1 ⇒ 2 次
    resp = api_client.get("/api/llm/usage/overview?model_name=m1", headers=headers)
    assert resp.json()["today"]["calls"] == 2

    # 筛 m2 ⇒ 1 次
    resp = api_client.get("/api/llm/usage/overview?model_name=m2", headers=headers)
    assert resp.json()["today"]["calls"] == 1

    # daily 端点也吃 model_name（m1 两天？—— 这里同一天，验证列表长度与聚合值）
    resp = api_client.get("/api/llm/usage/daily?model_name=m1", headers=headers)
    daily = resp.json()
    assert len(daily) == 1
    assert daily[0]["calls"] == 2

    # breakdown 端点也吃 model_name：dimension=platform 只留 m1 的那两条
    resp = api_client.get("/api/llm/usage/breakdown?dimension=platform&model_name=m1", headers=headers)
    items = {i["key"]: i for i in resp.json()}
    assert items["xhs"]["calls"] == 2

    # 筛不存在的模型 ⇒ 空态（缺值口径：不造假，回空/0）
    resp = api_client.get("/api/llm/usage/overview?model_name=ghost", headers=headers)
    assert resp.json()["today"]["calls"] == 0


# ───────────────────────── v1.6：监控预警（§4.5）─────────────────────────


def test_monitor_fires_notification_on_threshold(api_client, db_factory, monkeypatch):
    """监控任务：台账调用量超阈值 ⇒ 写 notifications + 记录 last_fired_at（同日去重）。"""
    from backend.app.models import LlmGatewayAlert, LlmGatewayUsageLog, Notification
    from backend.app.services.llm_gateway_monitor_service import run_llm_gateway_monitor_once

    headers = _auth_header(api_client)
    gw = _create_gateway(api_client, headers)

    # 建网关端点会**按用户自动补种默认预警规则**（设计 17 §4.5，2026-10-02）⇒
    # 本用例要精确断言 fired 数，先把种子规则清掉，只留自己加的那条。
    with db_factory() as db:
        db.query(LlmGatewayAlert).delete()
        db.commit()

    # 造台账：3 次成功调用
    with db_factory() as db:
        db.add_all([
            LlmGatewayUsageLog(user_id=1, gateway_id=gw["id"], platform="xhs", function="generate", status="success"),
            LlmGatewayUsageLog(user_id=1, gateway_id=gw["id"], platform="xhs", function="generate", status="success"),
            LlmGatewayUsageLog(user_id=1, gateway_id=gw["id"], platform="xhs", function="generate", status="success"),
        ])
        # 预警规则：xhs/generate 调用量 > 2 触发
        db.add(LlmGatewayAlert(
            user_id=1, gateway_id=gw["id"], platform="xhs", function="generate",
            metric="calls", operator="gt", threshold=2, window_seconds=86400,
        ))
        db.commit()

    # 跑监控任务
    result = run_llm_gateway_monitor_once(1)
    assert result["fired"] == 1

    with db_factory() as db:
        notifs = db.scalars(select(Notification).where(Notification.source_type == "llm_gateway_alert")).all()
        assert len(notifs) == 1
        assert "预警" in notifs[0].title
        alert = db.scalar(select(LlmGatewayAlert))
        assert alert.last_fired_at is not None

    # 同日再跑 ⇒ 去重，不重复发
    result2 = run_llm_gateway_monitor_once(1)
    assert result2["fired"] == 0


def test_monitor_health_alert(api_client, db_factory):
    """health 指标：**探测过且不健康** ⇒ 触发；**从未探测** ⇒ 不触发（v2.2 缺陷修复）。

    ⚠️ v2.2（设计 17 §4.5）：原实现只看 `healthy`，而该列**默认 False** ⇒ 任何新建服务商在
    第一次点「测试连接」之前都会发一条假的「健康探测失败（不可达）」通知（实测复现 fired=1）。
    新判据 = `last_health_at is not None AND healthy is False`。
    """
    from backend.app.models import LlmGatewayAlert, LlmGatewayConfig, Notification
    from backend.app.services.llm_gateway_monitor_service import run_llm_gateway_monitor_once

    headers = _auth_header(api_client)
    gw = _create_gateway(api_client, headers)  # healthy 默认 False 且 last_health_at=None

    # 同上：先清掉端点自动补种的默认规则，避免与用例自己那条 health 规则重复计数。
    with db_factory() as db:
        db.query(LlmGatewayAlert).delete()
        db.commit()

    with db_factory() as db:
        db.add(LlmGatewayAlert(
            user_id=1, gateway_id=gw["id"],
            metric="health", operator="eq", threshold=0,  # 0 = 不健康
        ))
        db.commit()

    # ① 从未探测 ⇒ 不发（「没测过」不是「测失败」）
    result = run_llm_gateway_monitor_once(1)
    assert result["fired"] == 0, "从未探测的服务商不该触发健康预警"
    with db_factory() as db:
        assert db.scalars(select(Notification).where(Notification.source_type == "llm_gateway_alert")).all() == []

    # ② 探测过且不健康 ⇒ 发
    with db_factory() as db:
        row = db.get(LlmGatewayConfig, gw["id"])
        row.healthy = False
        row.last_health_at = shanghai_now()
        db.commit()

    result = run_llm_gateway_monitor_once(1)
    assert result["fired"] == 1
    with db_factory() as db:
        notifs = db.scalars(select(Notification).where(Notification.source_type == "llm_gateway_alert")).all()
        assert len(notifs) == 1
        assert "健康" in notifs[0].title


def test_monitor_all_gateway_scope_hits_each_provider(api_client, db_factory):
    """**全网关作用域**（方案 A）：遍历用户名下全部服务商，逐个判、逐个发（v2.3）。

    造 2 个服务商各 2 次调用、全网关规则 `调用量 > 1` ⇒ `fired == 2`，
    两条通知分别指明**是哪个服务商**超限（否则用户不知道该去看谁）。
    """
    from backend.app.models import LlmGatewayAlert, LlmGatewayUsageLog, Notification
    from backend.app.services.llm_gateway_monitor_service import run_llm_gateway_monitor_once

    headers = _auth_header(api_client)
    gw1 = _create_gateway(api_client, headers, name="gw1")
    gw2 = _create_gateway(api_client, headers, name="gw2")

    with db_factory() as db:
        db.query(LlmGatewayAlert).delete()  # 清掉端点自动补种的默认规则
        db.commit()

    with db_factory() as db:
        for gw in (gw1, gw2):
            db.add_all([
                LlmGatewayUsageLog(user_id=1, gateway_id=gw["id"], platform="xhs", function="generate", status="success"),
                LlmGatewayUsageLog(user_id=1, gateway_id=gw["id"], platform="xhs", function="generate", status="success"),
            ])
        # 全网关作用域：gateway_id=None, model_name=""
        db.add(LlmGatewayAlert(
            user_id=1, gateway_id=None, platform="", function="", model_name="",
            metric="calls", operator="gt", threshold=1, window_seconds=86400,
        ))
        db.commit()

    result = run_llm_gateway_monitor_once(1)
    assert result["fired"] == 2, f"全网关规则应命中每个服务商各一次，实际 {result['fired']}"

    with db_factory() as db:
        notifs = db.scalars(select(Notification).where(Notification.source_type == "llm_gateway_alert")).all()
        assert len(notifs) == 2
        bodies = " ".join(n.body for n in notifs)
        assert "gw1" in bodies and "gw2" in bodies, "每条通知必须指明是哪个服务商超限"


def test_monitor_model_scope_filters_usage_by_model(api_client, db_factory):
    """**具体模型作用域**：台账按 `model_name` 再过滤（只统计该模型）。

    造同一服务商下 m1 三次、m2 零次；规则 = 服务商 + `model_name="m1"` + `调用量 > 2`
    ⇒ 命中（m1 三次）；再改成 `model_name="m2"` ⇒ 不命中（m2 无数据，**不用 0 冒充**）。
    """
    from backend.app.models import LlmGatewayAlert, LlmGatewayUsageLog, Notification
    from backend.app.services.llm_gateway_monitor_service import run_llm_gateway_monitor_once

    headers = _auth_header(api_client)
    gw = _create_gateway(api_client, headers)

    with db_factory() as db:
        db.query(LlmGatewayAlert).delete()
        db.commit()

    with db_factory() as db:
        db.add_all([
            LlmGatewayUsageLog(user_id=1, gateway_id=gw["id"], model_name="m1", status="success"),
            LlmGatewayUsageLog(user_id=1, gateway_id=gw["id"], model_name="m1", status="success"),
            LlmGatewayUsageLog(user_id=1, gateway_id=gw["id"], model_name="m1", status="success"),
        ])
        db.add(LlmGatewayAlert(
            user_id=1, gateway_id=gw["id"], platform="", function="", model_name="m1",
            metric="calls", operator="gt", threshold=2, window_seconds=86400,
        ))
        db.commit()

    result = run_llm_gateway_monitor_once(1)
    assert result["fired"] == 1, "m1 有 3 次调用，应命中"

    # 换成 m2（无台账数据）⇒ 不命中，且**不新增**通知
    with db_factory() as db:
        alert = db.scalar(select(LlmGatewayAlert))
        alert.model_name = "m2"
        alert.last_fired_at = None  # 清掉同日去重（本用例第 1 段已发过一次）
        db.commit()

    result = run_llm_gateway_monitor_once(1)
    assert result["fired"] == 0, "m2 无台账数据 ⇒ 不触发（无数据不用 0 冒充）"
    with db_factory() as db:
        notifs = db.scalars(select(Notification).where(Notification.source_type == "llm_gateway_alert")).all()
        assert len(notifs) == 1, "只有 m1 那次应发通知"


# ───────────── v1.9：未保存时拉模型目录（models-preview）+ 单条详情 ─────────────


def test_models_preview_requires_token(api_client):
    """v1.9：`models-preview` 没有令牌 ⇒ 400 `LLM_PROVIDER_TOKEN_REQUIRED`。

    ⚠️ 与 `/{id}/test` 的「无令牌跳过探测」**刻意不同**：那是已保存配置的巡检（跳过合理），
    而这里是**用户主动点「拉取模型」** —— 拉不到就说清楚，返回空列表等于骗人。
    """
    headers = _auth_header(api_client)
    resp = api_client.post(
        "/api/llm/gateway-configs/models-preview",
        json={"base_url": "http://127.0.0.1:3000/v1", "access_token": ""},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "LLM_PROVIDER_TOKEN_REQUIRED"


def test_models_preview_requires_base_url(api_client):
    headers = _auth_header(api_client)
    resp = api_client.post(
        "/api/llm/gateway-configs/models-preview",
        json={"base_url": "   ", "access_token": "sk-t"},
        headers=headers,
    )
    assert resp.status_code in (400, 422), resp.text


def test_models_preview_uses_stored_token_when_blank(api_client, monkeypatch):
    """编辑态令牌留空 ⇒ 用**库里已存**的令牌兜底（用户不必重填）。

    打桩 `requests.get` 记录实际请求，验证：① 打的 URL 是 `{base_url}/models`（`/v1` 不硬拼）；
    ② 带的是库里的令牌。
    """
    import requests as real_requests

    calls: list[tuple[str, str]] = []

    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "m-1"}, {"id": "m-2"}]}

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.append((url, (headers or {}).get("Authorization", "")))
        return FakeResp()

    monkeypatch.setattr(real_requests, "get", fake_get)

    headers = _auth_header(api_client)
    created = api_client.post(
        "/api/llm/gateway-configs",
        json={"name": "pv", "base_url": "http://127.0.0.1:3000/v1",
              "access_token": "sk-stored", "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}]},
        headers=headers,
    ).json()

    # 令牌留空 + 给 id ⇒ 用库里令牌
    resp = api_client.post(
        "/api/llm/gateway-configs/models-preview",
        json={"base_url": "http://127.0.0.1:3000/v1", "access_token": "", "id": created["id"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["models"] == ["m-1", "m-2"]
    assert calls, "应真的打了服务商 /models"
    url, auth = calls[-1]
    assert url == "http://127.0.0.1:3000/v1/models", f"`/v1` 不得被后端改写（实际 {url}）"
    assert "sk-stored" in auth, "令牌留空时应使用库里已存的令牌"


def test_models_preview_does_not_persist(api_client, monkeypatch):
    """**预览不落库**：拉目录不得写 `models_json`、不得建行。"""
    import requests as real_requests

    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "only-here"}]}

    monkeypatch.setattr(real_requests, "get", lambda *a, **kw: FakeResp())

    headers = _auth_header(api_client)
    before = api_client.get("/api/llm/gateway-configs?page=1&page_size=20", headers=headers).json()
    api_client.post(
        "/api/llm/gateway-configs/models-preview",
        json={"base_url": "http://127.0.0.1:3000/v1", "access_token": "sk-t"},
        headers=headers,
    )
    after = api_client.get("/api/llm/gateway-configs?page=1&page_size=20", headers=headers).json()
    assert len(after["items"]) == len(before["items"]), "预览不得新建服务商行"


def test_get_single_config_and_platforms_not_shadowed(api_client):
    """`GET /{config_id}` 上线后，`GET /platforms` **不得被它抢占**（字面量优先）。"""
    headers = _auth_header(api_client)
    created = api_client.post(
        "/api/llm/gateway-configs",
        json={"name": "single", "base_url": "http://127.0.0.1:3000",
              "access_token": "sk-t", "models": [{"model_name": "m", "model_type": "text", "platform": ["xhs"]}]},
        headers=headers,
    ).json()

    # 单条详情
    resp = api_client.get(f"/api/llm/gateway-configs/{created['id']}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["provider_key"] == created["provider_key"]

    # ⚠️ 关键回归：`/platforms` 仍回枚举（不是被当成 config_id 而 422/404）
    resp = api_client.get("/api/llm/providers/platforms", headers=headers)
    assert resp.status_code == 200, resp.text
    assert any(i["value"] == "xhs" for i in resp.json()["items"])

    # 越权/不存在 ⇒ 404
    assert api_client.get("/api/llm/gateway-configs/999999", headers=headers).status_code == 404


# ───────── v2.9：服务商 × 模型**两级列表**（2026-10-02 用户口径「父子列表」）─────────


def test_models_tree_returns_provider_with_its_models(api_client):
    """`GET /models-tree` = 每个服务商**带上它的全部模型行**（父子列表的数据源）。

    ⚠️ 与 `GET /` 的关键差别：`GET /` 的每项 `models` 恒为 `[]`（只够渲染父表），
    前端若拿它渲染子表会**永远空着且不报错** —— 本用例把两者钉住，防止回退。
    """
    headers = _auth_header(api_client)
    created = _create_gateway(api_client, headers, name="父子列表")

    resp = api_client.get("/api/llm/gateway-configs/models-tree", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["items"]
    #: ⚠️ 2026-10-14：注册即播种 6 家服务商（需求 R4）⇒ 不能断言「只有 1 条」。
    #: 本用例要证的是「**建的那条**在树里且带着它的模型行」，按 id 定位即可。
    rows = [i for i in items if i["id"] == created["id"]]
    assert len(rows) == 1, f"刚建的服务商应在树里：{[i['name'] for i in items]}"

    row = rows[0]
    assert row["id"] == created["id"]
    # 父层字段（服务商维度）齐全
    assert row["name"] == "父子列表"
    assert row["provider_key"] == created["provider_key"]
    assert "access_token" not in row
    # 子层真的装了模型行（这是一个端点存在的全部理由）
    assert [m["model_name"] for m in row["models"]] == ["deepseek-v4-flash"]
    assert row["models"][0]["model_type"] == "text"
    # 模型行没有令牌字段（凭据在服务商层）
    assert "access_token" not in row["models"][0]

    # 对照组：`GET /` 的 models 恒为空数组（不是「恰好空」，是设计如此）
    listed = api_client.get("/api/llm/gateway-configs?page=1&page_size=20", headers=headers).json()
    assert listed["items"][0]["models"] == []


def test_models_tree_not_shadowed_by_config_id_route(api_client):
    """⚠️ 路由顺序回归：`/models-tree` 不得被 `GET /{config_id}` 抢走。

    Starlette 按**声明顺序**匹配 ⇒ 若 `/models-tree` 定义在 `GET /{config_id}` **之后**，
    请求会被后者接住（`config_id="models-tree"` 解析 int 失败 ⇒ 422），
    前端拿不到数据且报错信息与真实原因无关（最难查的那类）。
    """
    headers = _auth_header(api_client)
    resp = api_client.get("/api/llm/gateway-configs/models-tree", headers=headers)
    assert resp.status_code == 200, f"被 /{{config_id}} 抢走了：{resp.status_code} {resp.text}"
    assert "items" in resp.json()


def test_models_tree_only_own(api_client):
    """跨用户隔离：只返回自己的服务商（与 `GET /` 同一口径）。"""
    headers = _auth_header(api_client)
    other_headers = _other_auth(api_client)
    _create_gateway(api_client, headers, name="我的")

    mine = api_client.get("/api/llm/gateway-configs/models-tree", headers=headers).json()["items"]
    theirs = api_client.get("/api/llm/gateway-configs/models-tree", headers=other_headers).json()["items"]
    #: ⚠️ 2026-10-14：注册即播种 ⇒ 两边都可能有种子服务商。
    #: 隔离的真正判据 = 「我的那条在我的里面 / **不在**对方的里面」——
    #: 比「对方是空列表」更直接也更稳（后者依赖「新用户什么都没有」这个已被 R4 改掉的前提）。
    assert "我的" in [r["name"] for r in mine]
    assert "我的" not in [r["name"] for r in theirs], "别的用户不该看到我的服务商"


def test_models_tree_empty_is_empty_list_not_error(api_client, db_factory):
    """**一个服务商都没有**时 ⇒ 空列表（空态由前端 `locale.emptyText` 呈现，不是错误）。

    ⚠️ 2026-10-14 改写：需求 R4 之后「注册即播种 6 家服务商」是**正确行为**，
    所以「新注册用户的 models-tree 为空」这个前提**已不成立**。
    本用例要证的仍是「空的时候不报错」—— 于是把该用户的服务商**先清掉**
    （走 DB 直删，不经过会触发校验的 API），再断言空态。
    这样测的是端点行为本身，而不是「新用户恰好没数据」这个会被产品决策推翻的巧合。
    """
    from sqlalchemy import delete

    from backend.app.models import LlmGatewayModel

    headers = _auth_header(api_client)
    db = db_factory()
    try:
        user = db.scalars(
            select(User).where(User.username == "llm_tester")
        ).one_or_none()
        assert user is not None, "前提：刚才注册的用户应在库里"
        db.execute(delete(LlmGatewayModel).where(LlmGatewayModel.user_id == user.id))
        db.commit()
        #: 服务商父行也一并清掉（models-tree 是服务商带模型，父行在就会非空）
        from backend.app.models import LlmGatewayConfig

        db.execute(delete(LlmGatewayConfig).where(LlmGatewayConfig.user_id == user.id))
        db.commit()
    finally:
        db.close()

    resp = api_client.get("/api/llm/gateway-configs/models-tree", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []
    assert resp.json()["total"] == 0


# ───── v2.10：模型自然序号 / 模型级健康 / **已在使用的模型无法停用**（2026-10-02 用户口径）─────


def test_model_rows_carry_model_key_and_health_fields(api_client):
    """模型行出参带 `model_key`（服务商内自然序号）+ 模型级健康两字段。"""
    headers = _auth_header(api_client)
    _create_gateway(api_client, headers, name="序号")

    rows = api_client.get("/api/llm/gateway-configs/models-tree", headers=headers).json()["items"]
    model = rows[0]["models"][0]
    # 创建时后端的**首个**模型 ⇒ 服务商内自然序号从 1 起
    assert model["model_key"] == "1"
    # 从未探测 ⇒ healthy=False 且 last_health_at=None（**两字段必须一起给**，
    # 只给 healthy 会让前端把「从未探测」渲染成「异常」）
    assert model["healthy"] is False
    assert model["last_health_at"] is None
    # 无业务点映射 ⇒ usages 空数组（不是 None：前端按 length 判置灰）
    assert model["usages"] == []


def test_model_keys_are_sequential_within_provider(api_client):
    """同一服务商下多个模型 ⇒ `model_key` 为 1/2/3… 递增（用户口径「服务商内不重复即可」）。"""
    headers = _auth_header(api_client)
    created = _create_gateway(api_client, headers, name="多模型")
    for name in ("m-second", "m-third"):
        resp = api_client.post(
            f"/api/llm/gateway-configs/{created['id']}/models",
            json={"model_name": name, "model_type": "text", "platform": ["xhs"]},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text

    rows = api_client.get("/api/llm/gateway-configs/models-tree", headers=headers).json()["items"]
    keys = [m["model_key"] for m in rows[0]["models"]]
    assert keys == ["1", "2", "3"], keys


def test_model_key_scoped_per_provider(api_client):
    """⚠️ 自然序号是**服务商内**的 ⇒ 两个服务商各自从 1 起（不是全局递增）。"""
    headers = _auth_header(api_client)
    _create_gateway(api_client, headers, name="甲")
    _create_gateway(api_client, headers, name="乙")

    rows = api_client.get("/api/llm/gateway-configs/models-tree", headers=headers).json()["items"]
    assert all(r["models"][0]["model_key"] == "1" for r in rows), rows


def test_disable_model_in_use_by_business_is_rejected_400(api_client, db_factory):
    """**已在使用的模型无法停用**（用户口径 2026-10-02）。

    被测：`PATCH /{config_id}/models/{model_id} {enabled: false}` ——
    若该 `(provider_key, model_name)` 正被某业务点映射引用 ⇒ 400 `LLM_MODEL_IN_USE_BY_BUSINESS`，
    且**不得落库**（不是「先停用再报错」—— 那会让业务点变成「配好了但永远跑不通」的死配置）。
    """
    headers = _auth_header(api_client)
    created = _create_gateway(api_client, headers, name="在用")
    provider_key = created["provider_key"]
    model_id = created["models"][0]["id"]

    # 把一个业务点映射到这个模型（business_key / model_name 与 `_create_gateway` 造的模型对齐）
    resp = api_client.put(
        "/api/llm/business-providers",
        json={
            "platform": "xhs",
            "business_key": "generate_note",
            "provider_key": provider_key,
            "model_name": "deepseek-v4-flash",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    # 现在该模型「在使用中」⇒ 停用被拒
    resp = api_client.patch(
        f"/api/llm/gateway-configs/{created['id']}/models/{model_id}",
        json={"enabled": False},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "LLM_MODEL_IN_USE_BY_BUSINESS"
    # 结构化带上「被谁在用」，前端提示才能指名道姓
    assert [u["business_key"] for u in detail["usages"]] == ["generate_note"]
    assert detail["usages"][0]["label"] == "生成笔记"

    # ⚠️ 关键：**没有落库**（读回仍是启用）
    rows = api_client.get("/api/llm/gateway-configs/models-tree", headers=headers).json()["items"]
    assert rows[0]["models"][0]["enabled"] is True


def test_disable_model_not_in_use_is_allowed(api_client):
    """无人引用的模型 ⇒ 停用照常放行（守卫只拦「在用」，不是把停用功能一刀切砍掉）。"""
    headers = _auth_header(api_client)
    created = _create_gateway(api_client, headers, name="空闲")
    model_id = created["models"][0]["id"]

    resp = api_client.patch(
        f"/api/llm/gateway-configs/{created['id']}/models/{model_id}",
        json={"enabled": False},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False


def test_enable_model_in_use_is_allowed(api_client, db_factory):
    """⚠️ 守卫**只拦停用**：在用模型的「启用」不拦 —— 那是在修复「指着已停用模型」的死配置。

    构造：先（手写库）把模型置为停用 + 建好指向它的业务点映射，再 PATCH `enabled=True`
    ⇒ 必须放行（否则用户被锁死：映射要求模型可用，而启用又被拒）。
    """
    headers = _auth_header(api_client)
    created = _create_gateway(api_client, headers, name="救回")
    provider_key = created["provider_key"]
    model_id = created["models"][0]["id"]

    api_client.put(
        "/api/llm/business-providers",
        json={
            "platform": "xhs",
            "business_key": "generate_note",
            "provider_key": provider_key,
            "model_name": "deepseek-v4-flash",
        },
        headers=headers,
    )

    # 直接改库把模型停用（绕过 PATCH 的守卫 —— 模拟「映射是后来加的」这种历史态）
    from backend.app.models.llm_gateway import LlmGatewayModel

    with db_factory() as db:
        row = db.scalars(select(LlmGatewayModel).where(LlmGatewayModel.id == model_id)).one()
        row.enabled = False
        db.commit()

    resp = api_client.patch(
        f"/api/llm/gateway-configs/{created['id']}/models/{model_id}",
        json={"enabled": True},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True


def test_models_tree_reports_usages_for_mapped_model(api_client, db_factory):
    """`usages` 供前端把「启用」开关置灰 —— 映射到哪个业务点就报哪个。"""
    headers = _auth_header(api_client)
    created = _create_gateway(api_client, headers, name="带用")
    api_client.put(
        "/api/llm/business-providers",
        json={
            "platform": "xhs",
            "business_key": "generate_note",
            "provider_key": created["provider_key"],
            "model_name": "deepseek-v4-flash",
        },
        headers=headers,
    )

    rows = api_client.get("/api/llm/gateway-configs/models-tree", headers=headers).json()["items"]
    usages = rows[0]["models"][0]["usages"]
    assert [u["business_key"] for u in usages] == ["generate_note"]
    assert usages[0]["label"] == "生成笔记"
    assert usages[0]["platform"] == "xhs"

