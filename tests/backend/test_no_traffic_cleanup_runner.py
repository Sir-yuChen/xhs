"""无流量清理**执行器**的回归测试（DEF-042 的守卫）。

## 为什么单独建这个文件

清理执行器 `run_no_traffic_cleanup` 此前**零测试覆盖** —— 候选查询
（`test_note_cleanup.py`）与删除链（`test_note_delete.py`）各自有覆盖，
但「把它们串起来在后台跑」的那一层没有。于是一个 `ImportError` 活了下来：

    from backend.app.models.account import AccountCookieVersion   # 模块不存在

它只在**函数被调到时**才炸，而 `run_no_traffic_cleanup` 的
`except Exception` 会把它吞掉并标任务 `failed` —— 结果是「无流量清理」
每次都在后台静默失败，页面上看不出任何异常。

本文件钉住三件事：

1. 执行器能拿到 Cookie（`_account_cookies` 真的可调用，不再 ImportError）；
2. 候选 → 权限校验 → 删除的链路真的走通；
3. 任务行被标成 `done`，且 deleted 计数正确。
"""
from __future__ import annotations

import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.adapters.xhs import creator_api_adapter
from backend.app.core.database import Base
from backend.app.core.time import shanghai_now
from backend.app.models import PlatformAccount, Task, User
from backend.app.models.shop import ShopNoteGoods
from backend.app.services import no_traffic_cleanup_service as ncs
from backend.app.services.account_service import CHANNEL_CREATOR
from backend.app.services.shop_binding_service import set_shop_state
from tests.backend.cookie_fixtures import cookie_version


class FakeCreatorApiAdapter:
    """只实现清理链路用到的两步，并记录被删的 note_id。"""

    deleted: list = []
    in_promotion: set = set()

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def validate_delete_permission(self, note_id: str):
        return True, "ok", {
            "data": {"is_note_in_promotion": note_id in type(self).in_promotion}
        }

    def delete_note(self, note_id: str):
        type(self).deleted.append(note_id)
        return True, "ok", {}


def _entry(note_id: str, *, browse: int = 0, orders: int = 0, gmv: int = 0) -> dict:
    return {
        "note_id": note_id,
        "note_title": f"笔记 {note_id}",
        "publish_time": 1_760_000_000_000,
        "browse_num": browse,
        "note_pay_order_cnt": orders,
        "note_pay_gmv": gmv,
        "note_browse_num30d": 0,
    }


def _setup(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cleanup-runner.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    monkeypatch.setattr(ncs, "SessionLocal", SessionLocal)
    monkeypatch.setattr(creator_api_adapter, "XhsCreatorApiAdapter", FakeCreatorApiAdapter)
    FakeCreatorApiAdapter.deleted = []
    FakeCreatorApiAdapter.in_promotion = set()

    db = SessionLocal()
    try:
        user = User(username="owner", password_hash="x")
        db.add(user)
        db.flush()
        account = PlatformAccount(
            user_id=user.id, platform="xhs", sub_type="creator", nickname="我的号"
        )
        db.add(account)
        db.flush()
        # 执行器取 Cookie 走的就是这一行；它曾经因为 import 错模块而必炸。
        # 2026-09-21 拆分：无流量清理打创作者平台，所以给 creator 平台的 jar。
        db.add(
            cookie_version(
                account.id,
                "web_session=abc; a1=xyz; access-token-creator.xiaohongshu.com=ctok; customer-sso-sid=sso",
                CHANNEL_CREATOR,
            )
        )
        #: ADR-021：店铺已退场，归属键 = `platform_account_id`，账号归属写
        #: `seller_id`；状态仍写 `shop_state`。清理由账号发起。
        account.seller_id = "SELLER-1"
        set_shop_state(account, status="active", goods_note_total=3)
        db.flush()
        for note_id, kwargs in (
            ("ZERO-1", {}),                                    # 三项全零 → 候选
            ("HAS-READ", {"browse": 7}),                       # 有人看 → 不删
            ("ZERO-2", {}),                                    # 三项全零 → 候选
        ):
            db.add(
                ShopNoteGoods(
                    user_id=user.id,
                    platform_account_id=account.id,
                    note_id=note_id,
                    item_id="ITEM-1",
                    item_title="商品",
                    entry_json=json.dumps(_entry(note_id, **kwargs), ensure_ascii=False),
                    synced_at=shanghai_now(),
                )
            )
        db.commit()
        account_id = account.id
    finally:
        db.close()
    return account_id, SessionLocal


def test_cleanup_runner_reads_cookies_and_deletes_the_candidates(tmp_path, monkeypatch):
    """回归 DEF-042：执行器曾因 import 错模块而在后台静默失败。"""
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)

    result = ncs.run_no_traffic_cleanup(account_id, user_id=1)

    assert sorted(FakeCreatorApiAdapter.deleted) == ["ZERO-1", "ZERO-2"]
    assert result["deleted"] == 2, result
    assert result["failed"] == 0, result

    db = SessionLocal()
    try:
        task = db.scalars(select(Task).where(Task.task_type == ncs.CLEANUP_TASK_TYPE)).one()
        assert task.status == ncs.STATUS_DONE
        assert task.payload["deleted"] == 2
    finally:
        db.close()


def test_cleanup_runner_skips_notes_in_promotion(tmp_path, monkeypatch):
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)
    FakeCreatorApiAdapter.in_promotion = {"ZERO-1"}

    result = ncs.run_no_traffic_cleanup(account_id, user_id=1)

    assert FakeCreatorApiAdapter.deleted == ["ZERO-2"]
    assert result["skipped"] == 1
    assert result["deleted"] == 1


def test_second_run_is_refused_while_a_task_is_active(tmp_path, monkeypatch):
    """防重：已有未完成任务 → 直接返回它，不再删第二遍。"""
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)

    db = SessionLocal()
    try:
        db.add(
            Task(
                user_id=1,
                platform="xhs",
                task_type=ncs.CLEANUP_TASK_TYPE,
                status=ncs.STATUS_RUNNING,
                payload={"account_id": account_id},
            )
        )
        db.commit()
    finally:
        db.close()

    result = ncs.run_no_traffic_cleanup(account_id, user_id=1)

    assert FakeCreatorApiAdapter.deleted == []
    assert result["deleted"] == 0
    assert "防重复触发" in result["message"]
