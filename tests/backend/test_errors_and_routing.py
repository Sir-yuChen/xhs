"""架构收口的守卫：统一错误契约（A2）/ 中间件集中装配（A3）/ 路由统一注册表（A4）。

设计 61 §三；错误形状的行为依据是 2026-10-24 的调研实测
（fastapi 0.141 / starlette 1.6，见 `core/errors.py` 模块 docstring）。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.app.core.errors import BizError, register_error_handlers


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.contract


# ---------------------------------------------------------------------------
# A2 · BizError → 统一错误包络
# ---------------------------------------------------------------------------


@pytest.fixture()
def errors_app() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)

    class SubBizError(BizError):
        """子类应被父类 handler 按 MRO 接住（调研实测口径）。"""

    @app.get("/biz")
    def biz():
        raise BizError("X_NO_SESSION", "需要网页会话", status_code=428,
                       platform_message="平台原文", details={"account_id": 7})

    @app.get("/biz-min")
    def biz_min():
        raise BizError("X_MIN", "最小错误")

    @app.get("/biz-sub")
    def biz_sub():
        raise SubBizError("X_SUB", "子类错误", status_code=409)

    @app.get("/http-exc")
    def http_exc():
        raise HTTPException(status_code=404, detail="老形状保持不变")

    @app.get("/validate")
    def validate(page: int):
        return {"page": page}

    return TestClient(app)


def test_biz_error_full_shape(errors_app):
    resp = errors_app.get("/biz", )
    assert resp.status_code == 428
    detail = resp.json()["detail"]
    assert detail == {
        "code": "X_NO_SESSION",
        "message": "需要网页会话",
        "platform_message": "平台原文",
        "details": {"account_id": 7},
    }


def test_biz_error_omits_absent_optional_fields(errors_app):
    """可选字段缺省时**不下发**（不制造「空串 vs 缺失」歧义，缺值口径硬约束⑤）。"""
    body = errors_app.get("/biz-min").json()
    assert body == {"detail": {"code": "X_MIN", "message": "最小错误"}}
    assert errors_app.get("/biz-min").status_code == 400


def test_biz_error_subclass_uses_the_parent_handler(errors_app):
    resp = errors_app.get("/biz-sub")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "X_SUB"


def test_http_exception_shape_is_untouched(errors_app):
    """HTTPException 照走 FastAPI 默认 —— 存量 553 处 raise 的形状一字不变。"""
    resp = errors_app.get("/http-exc")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "老形状保持不变"}


def test_validation_error_keeps_the_array_shape(errors_app):
    """422 数组形状原样保留 —— 前端 `detailMessage()` 的数组分支依赖它。"""
    resp = errors_app.get("/validate")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and detail


def test_biz_error_is_not_an_http_exception():
    """钉住继承契约：与存量 HTTPException 的 raise 零纠缠（调研实测的设计前提）。"""
    assert not issubclass(BizError, HTTPException)


# ---------------------------------------------------------------------------
# A3/A4 · 中间件与路由收口后的装配完整性
# ---------------------------------------------------------------------------


def test_openapi_inventory_is_unchanged_by_the_registry():
    """路由收口后 OpenAPI 集合不变（390 ops / 330 paths，AGENTS §5 口径）。

    这条是 A4 的验收判据：include 顺序逐条原样搬移 ⇒ 匹配优先级与 path 集都不得漂移。
    """
    from backend.app.main import app

    spec = TestClient(app).get("/openapi.json").json()
    paths = spec["paths"]
    ops = sum(
        1
        for item in paths.values()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    )
    assert (len(paths), ops) == (330, 390), (
        "OpenAPI 集合漂移 —— routing.py 的 include 顺序/条目与重构前不一致"
    )


def test_routing_registry_lists_no_router_outside_its_own_module():
    """注册表必须穷尽登记：main.py 里不得再出现裸 `include_router`（装配点唯一）。"""
    import re

    source = (ROOT_MAIN := __import__("pathlib").Path(__file__).resolve().parents[2] / "backend/app/main.py").read_text(encoding="utf-8")
    offenders = [
        f"L{i}"
        for i, line in enumerate(source.splitlines(), 1)
        if "include_router" in line and not line.strip().startswith("#")
    ]
    assert not offenders, f"main.py 仍有 include_router（应收口进 api/routing.py）：{offenders}"


def test_cors_middleware_is_wired_by_setup_middleware():
    """A3：CORS / 审计经 `setup_middleware` 装配 —— 跨域请求头可观测即装配生效。"""
    from backend.app.main import app

    client = TestClient(app)
    resp = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin"), "CORS 未装配（setup_middleware 失效？）"
