"""测试基座自检（2026-10-26 重建）。

**这三条是整个测试体系的地基**：它们证明 `conftest.py` 的三条不变量成立。
不成立时，任何功能点测试都会出现「单跑绿、串行红」或「写进去了查不到」——
旧基座就栽在这，攒了 3334 条测试却大面积不可信。

写新功能点测试前先跑这个文件，绿了再写。

    pytest tests/backend/test_00_baseline.py -q
"""

#: 用例预算：6（基座自检，不随功能点增长 —— 骨架就这几条）

from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.backend.conftest import TEST_SCHEMA


def test_engine_points_to_test_schema(engine):
    """① engine 的每条连接都落在测试 schema（不是生产 `public`）。

    ⚠️ schema 名**按 worker 派生**（`test_gw0` / `test_gw1` / 串行时 `test`），
    所以判据是「等于 conftest 声明的那个」，不是硬编码字符串 `"test"`。
    硬编码会在 `pytest.ini` 默认的 `-n 4` 下把正当的隔离判成失败（实测过）。
    """
    with engine.connect() as conn:
        schema = conn.execute(text("SELECT current_schema()")).scalar()
    assert schema == TEST_SCHEMA, f"engine 连的是 {schema}，应为 {TEST_SCHEMA}"
    assert schema != "public", "测试 engine 绝不能连生产 public"


def test_api_write_is_visible_to_case(client, db):
    """② 端点写的数据，同用例内的 `db` 立刻查得到。

    这是「端点与用例共用同一个库」的最小证明。旧基座在这里就不成立：
    端点写 `public`、用例查 `t_xxx`。
    """
    from backend.app.models.user import User

    r = client.post(
        "/api/auth/register", json={"username": "baseline-a", "password": "secret123"}
    )
    assert r.status_code == 200, r.text
    assert db.query(User).filter_by(username="baseline-a").count() == 1


def test_previous_case_data_is_gone(client, db):
    """③ 上一条用例写的数据看不见（`TRUNCATE` 隔离生效）。

    依赖上一条用例注册的 `baseline-a` —— 若本文件单独跑，上一条也会先跑。
    """
    from backend.app.models.user import User

    assert db.query(User).filter_by(username="baseline-a").count() == 0


def test_same_username_can_register_again(client, db):
    """④ 跨用例重复注册同一 username 不撞唯一约束（`EMAIL_TAKEN` 的根治）。"""
    from backend.app.models.user import User

    r = client.post(
        "/api/auth/register", json={"username": "baseline-a", "password": "secret123"}
    )
    assert r.status_code == 200, r.text
    assert db.query(User).filter_by(username="baseline-a").count() == 1


@pytest.mark.parametrize(
    #: ⚠️ 设计 75 S4（2026-10-27）：`note_sync_service` 已退场（同步笔记任务全链路移除，
    #: 笔记改实时查询）⇒ 不再有模块级 `SessionLocal` 可打桩，从清单移除。
    "service_name", ["scheduler_service", "scheduled_job_runner"]
)
def test_service_sessionlocal_points_to_test_schema(service_name):
    """⑤ 服务层模块级 `SessionLocal` 也落在 `test` schema。

    **旧基座最致命的缺口**：这些工厂在 import 时快照，不打桩就写生产库。
    这也是「端点写成功但用例查不到」的直接原因。
    """
    mod = __import__(f"backend.app.services.{service_name}", fromlist=["SessionLocal"])
    with mod.SessionLocal() as s:
        schema = s.execute(text("SELECT current_schema()")).scalar()
    assert schema == TEST_SCHEMA, f"{service_name}.SessionLocal 连的是 {schema}，应为 {TEST_SCHEMA}"
