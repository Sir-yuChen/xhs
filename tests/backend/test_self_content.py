"""「我的笔记」自有内容（my notes / liked / collected / interactions）回归测试。

设计源：`docs/02_设计文档/12_功能设计_我的笔记与互动数据.md`
需求源：`docs/01_需求文档/03_需求规格_我的笔记列表.md`

每条断言锁定设计文档里的一条契约，而不是复述实现细节。重点保护三类容易
回退的地方：

1. **鉴权与归属**——`/users/*` 只能读当前账号自己的数据（设计文档 §10）。
2. **字段名差异**——三个平台接口的列表键不同（§3.2），探测逻辑不得退化为单一假设。
3. **诚实降级**——拿不到令牌或商品时不造假，给结构化错误与显式标注（§6.3、§7）。
"""

from __future__ import annotations

import inspect
import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.api.platforms.xhs.pc import (
    _extract_goods,
    _extract_list,
    _items_from_payload,
    _normalize_identity,
    _pagination_from_payload,
    _parse_profile_url,
    _web_session_required_detail,
    get_xhs_creator_api_adapter_factory,
    get_xhs_pc_api_adapter_factory,
)
from backend.app.api.platforms.xhs.qianfan import get_qianfan_api_adapter_factory
from backend.app.core.database import Base, get_db
from backend.app.core.security import decrypt_text, encrypt_text
from backend.app.main import app
from backend.app.models import AccountCookieVersion, PlatformAccount, User
from backend.app.models.shop import ShopNoteGoods
from backend.app.services.account_service import (
    CHANNEL_CREATOR,
    CHANNEL_WWW,
    dump_channel_envelope,
    parse_channel_envelope,
)
from backend.app.services.shop_binding_service import (
    has_ark_session,
    set_shop_state,
)
from backend.app.services.shop_goods_service import existing_rows, store_entry
from backend.app.core.time import shanghai_now
from tests.backend.cookie_fixtures import cookie_version, give_ark_cookies

import pytest

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

# `id_token` 必须一起给（DEF-051）：真实登录的 www jar 一定有它；
# 只给 `web_session` 等于在模拟**游客会话** —— 平台对未登录访客也会种那个 cookie，
# 而它读不到任何本人数据。夹具漏掉 id_token，就是在替 DEF-051 打掩护。
COOKIE_PAYLOAD = json.dumps(
    {"a1": "cookie-a1-value", "web_session": "cookie-web-session", "id_token": "cookie-id-token"}
)

# Mirrors a real creator-platform jar: it has a1 / galaxy_creator_session_id but
# **no** web_session, which is precisely why PC endpoints reject it (设计文档 §17.2).
# A test that gave the creator account the www payload would silently pass while
# the real defect stayed in place — that is the trap this constant closes.
CREATOR_COOKIE_PAYLOAD = json.dumps(
    {
        "a1": "creator-a1-value",
        "galaxy_creator_session_id": "creator-session",
        "x-user-id-creator.xiaohongshu.com": "creator-user-id",
    }
)


class FakeSelfContentAdapter:
    """Canned platform responses, with a switchable failure mode.

    ``mode`` drives the three branches the routes must distinguish:
      ok        -> happy path
      fail      -> platform rejects with a message (no token supplied)
      no_token  -> same, but the caller also supplied no xsec_token
      bad_id    -> /v2/user/me succeeds but carries no usable user_id
      profile_fail -> the identity call itself fails (cookie problem)
    """

    mode = "ok"
    calls: dict = {}

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    # -- identity ----------------------------------------------------------
    def get_self_profile(self):
        FakeSelfContentAdapter.calls["get_self_profile"] = True
        if FakeSelfContentAdapter.mode == "profile_fail":
            return False, "登录态已失效，请重新登录", None
        if FakeSelfContentAdapter.mode == "bad_id":
            return True, "", {"data": {"nickname": "无 id 的账号"}}
        return True, "", {
            "data": {
                "user_id": "SELF-1",
                "nickname": "我的号",
                "avatar": "https://cdn.example/avatar.jpg",
                "red_id": "red-001",
                "ip_location": "上海",
            }
        }

    # -- note detail -------------------------------------------------------
    def get_note_info(self, url):
        """www feed 的笔记详情。

        **真机形状（§19.1 实测）**：这个 payload 里**结构上没有**任何商品容器
        （`goods_info` / `goods` / `note_goods` 一个都没有）。这正是 DEF-047 的根因 ——
        详情接口的 `goods` 只能靠 `note_id` 从千帆映射合成，不能指望它自己带；
        夹具要是「顺手」在这里放一个商品字段，就等于把缺陷盖住了。
        """
        FakeSelfContentAdapter.calls["detail"] = FakeSelfContentAdapter.calls.get("detail", 0) + 1
        if FakeSelfContentAdapter.mode == "fail":
            return False, "平台拒绝了该请求", None
        note_id = url.split("/explore/")[-1].split("?")[0]
        return True, "", {
            "data": {
                "items": [
                    {
                        "note_card": {
                            "note_id": note_id,
                            "display_title": f"详情 {note_id}",
                            "desc": "正文来自详情接口",
                            "type": "normal",
                            "time": 1700000000,
                            "user": {"user_id": "SELF-1", "nickname": "我的号"},
                            "interact_info": {
                                "liked_count": "1",
                                "collected_count": 0,
                                "comment_count": 0,
                                "share_count": 0,
                            },
                            "image_list": [{"url_default": "https://cdn.example/detail.jpg"}],
                        }
                    }
                ]
            }
        }

    # -- note lists --------------------------------------------------------
    def _note_page(self):
        FakeSelfContentAdapter.calls["posted"] = FakeSelfContentAdapter.calls.get("posted", 0) + 1
        if FakeSelfContentAdapter.mode in ("fail", "no_token"):
            return False, "平台拒绝了该请求", None
        return True, "", {
            "data": {
                "notes": [
                    {
                        "id": "N1",
                        "xsec_token": "TK-1",
                        "note_card": {
                            "note_id": "N1",
                            "display_title": "标题一",
                            "desc": "正文一",
                            "type": "normal",
                            "time": 1700000000,
                            "user": {"user_id": "SELF-1", "nickname": "我的号"},
                            "interact_info": {
                                "liked_count": "1.2万",
                                "collected_count": 34,
                                "comment_count": 5,
                                "share_count": 2,
                            },
                            "image_list": [
                                {"url_default": "https://cdn.example/1.jpg"},
                                {"url_default": "https://cdn.example/2.jpg"},
                            ],
                            "goods_info": {"goods_id": "G9", "title": "商品九", "price": "199"},
                        },
                    }
                ],
                "cursor": "CUR-2",
                "has_more": True,
            },
        }

    def get_user_posted_page(self, user_id, cursor="", xsec_token="", xsec_source=""):
        FakeSelfContentAdapter.calls["posted_args"] = (user_id, cursor, xsec_token, xsec_source)
        return self._note_page()

    def get_user_liked_page(self, user_id, cursor="", xsec_token="", xsec_source=""):
        return self._note_page()

    def get_user_collected_page(self, user_id, cursor="", xsec_token="", xsec_source=""):
        return self._note_page()

    # -- interaction messages ---------------------------------------------
    def get_interaction_messages(self, kind, cursor=""):
        FakeSelfContentAdapter.calls["interaction_kind"] = kind
        return True, "", {
            "data": {
                # you/* endpoints nest their list under message_list, unlike
                # user_posted which uses notes (design doc §3.2).
                "message_list": [
                    {
                        "id": "M1",
                        "content": "赞了你的笔记",
                        "user_info": {"nickname": "路人甲", "avatar": "https://cdn.example/u.jpg"},
                        "note_id": "N9",
                        "note_url": "https://www.xiaohongshu.com/explore/N9",
                    }
                ],
                "has_more": False,
            }
        }


class FakeCreatorAdapter:
    """Canned creator-platform responses.

    Kept separate from :class:`FakeSelfContentAdapter` so a test can assert
    *which* link was used, not merely that something succeeded.
    """

    mode = "ok"
    calls: dict = {}

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def get_posted_notes_page(self, page: int = 0, tab: int = 0):
        FakeCreatorAdapter.calls["posted_page_args"] = (page, tab)
        if FakeCreatorAdapter.mode == "fail":
            return False, "创作者平台拒绝了该请求", None
        return True, "", {
            "data": {
                # Creator note-manager pages by server cursor; -1 terminates.
                "page": -1,
                # The account-wide total exists *only* here — a single page
                # would report "2 of 2" on a 1969-note account.
                "tags": [
                    {
                        "id": "special.note_time_desc",
                        "name": "所有笔记",
                        "notes_count": 1969,
                        "checked": True,
                    },
                    {"id": "special.video", "name": "视频", "notes_count": 843, "checked": False},
                ],
                # Shape copied verbatim from a live account (设计文档 §17.3):
                # flat metrics, ``images_list``, and **no body field at all**.
                "notes": [
                    {
                        "id": "6a7d1e2f000000002c001c33",
                        "display_title": "化妆室浴室地垫仿亚麻编织吸水速干脚垫防滑",
                        "time": "2026-08-13 09:30",
                        "likes": 12,
                        "collected_count": 3,
                        "comments_count": 1,
                        "shared_count": 0,
                        "view_count": 2,
                        "type": "normal",
                        "images_list": [
                            {"url": "https://cdn.example/c1-1.jpg"},
                            {"url": "https://cdn.example/c1-2.jpg"},
                        ],
                        # Empty array — must degrade to "no goods", never to a
                        # phantom entry whose title is the literal "[]".
                        "goods_info": {"goods_name": []},
                        "xsec_token": "TOKEN-1",
                        "sticky": False,
                    },
                    {
                        "id": "6a7d1e2f000000002c001c44",
                        "display_title": "挂了商品的视频笔记",
                        "time": "2026-08-12 08:00",
                        "likes": 0,
                        "collected_count": 0,
                        "comments_count": 0,
                        "shared_count": 0,
                        "view_count": 1,
                        "type": "video",
                        "images_list": [{"url": "https://cdn.example/c2-1.jpg"}],
                        # Array-valued, exactly as the platform returns it.
                        "goods_info": {"goods_name": ["测评商品"], "goods_id": ["G-1"]},
                        "xsec_token": "TOKEN-2",
                    },
                ],
            }
        }

    def get_creator_profile(self):
        FakeCreatorAdapter.calls["get_creator_profile"] = True
        if FakeCreatorAdapter.mode == "fail":
            return False, "创作者登录态已失效", None
        return True, "", {
            "data": {
                "userId": "CREATOR-1",
                "userName": "欢喜",
                "userAvatar": "https://cdn.example/creator.jpg",
                "redId": "49216354104",
                "userDesc": "还没有简介",
                "role": "creator",
                # Must never reach the response — see the whitelist test below.
                "phone": "19253615941",
                "realNameVerified": False,
            }
        }


class FakeQianfanAdapter:
    """Canned 千帆（ark）responses —— 商品映射的唯一真实来源。

    `pages` 是「千帆商品笔记列表」的可变桩数据，测试可直接改它来构造
    「已同步 / 未同步 / 同步后商品被删」三种情形。
    """

    mode = "ok"
    total = 2
    pages: dict = {}
    calls: dict = {}
    #: 商品接口桩：`item_id -> (最低价分, 最高价分)`；未登记的 ID 视为平台查不到。
    item_prices: dict = {}
    #: 每次 `search_items` 收到的实参，用来断言「一页只查一次、ID 逗号分隔」。
    item_queries: list = []
    #: 每次**按需补齐**（带 `note_ids`）收到的 ID 列表，用来断言「一页一次 / 第二次零请求」。
    note_id_queries: list = []
    #: `search_items` 的失败开关（"ok" / "fail"）。
    items_mode = "ok"

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def search_items(self, **kwargs):
        FakeQianfanAdapter.calls["items"] = FakeQianfanAdapter.calls.get("items", 0) + 1
        FakeQianfanAdapter.item_queries.append(kwargs)
        if FakeQianfanAdapter.items_mode == "fail":
            return False, "千帆商品接口暂时不可用", {"code": -1, "success": False}
        rows = []
        for raw in str(kwargs.get("keyword") or "").split(","):
            item_id = raw.strip()
            prices = FakeQianfanAdapter.item_prices.get(item_id)
            if not item_id or prices is None:
                continue
            rows.append({"item_id": item_id, "min_price": prices[0], "max_price": prices[1]})
        return True, "ok", {
            "code": 0,
            "success": True,
            "data": {"items": rows, "total": len(rows)},
        }

    def get_seller_info(self):
        FakeQianfanAdapter.calls["seller"] = FakeQianfanAdapter.calls.get("seller", 0) + 1
        if FakeQianfanAdapter.mode == "expired":
            return False, "缺失登录凭证, 请重新登录", {"code": -13000, "success": False}
        return True, "ok", {
            "code": 0,
            "success": True,
            "data": {
                "user_id": "6a3250e427a0000000000000",
                "seller_name": "观隅好物的店",
                "user_name": "观隅好物",
                # 敏感字段：白名单构造必须把它挡在响应之外。
                "user_email": "247*******@**.com",
                "config_values": {"alipay_smid": "2088780175999744"},
            },
        }

    def get_goods_note_page(self, page_num: int = 1, page_size: int = 20, *, note_ids=None):
        """翻页（无过滤）与**按需补齐**（`note_ids` 过滤）两种调用都走这里。

        `note_ids` 传入时模拟平台真实语义：只返回这些笔记的条目，
        且把调用参数记录下来，供「一页只查一次 / 第二次零请求」的断言使用。
        """
        FakeQianfanAdapter.calls["goods_page"] = FakeQianfanAdapter.calls.get("goods_page", 0) + 1
        if note_ids:
            FakeQianfanAdapter.note_id_queries.append(list(note_ids))
        if FakeQianfanAdapter.mode == "expired":
            return False, "缺失登录凭证, 请重新登录", {"code": -13000, "success": False}
        if FakeQianfanAdapter.mode == "goods_fail":
            return False, "商品笔记列表暂时不可用", {"code": -1, "success": False}

        if note_ids:
            wanted = {str(item) for item in note_ids}
            index: dict = {}
            for entries in FakeQianfanAdapter.pages.values():
                for entry in entries:
                    index[str(entry.get("note_id"))] = entry
            hits = [index[note_id] for note_id in note_ids if str(note_id) in index]
            return True, "ok", {
                "code": 0,
                "success": True,
                "data": {
                    "note_infos": hits,
                    # 平台语义：带 note_ids 时 total 是**过滤后**条数
                    "total": len(hits),
                    "end_tag": True,
                },
            }

        entries = FakeQianfanAdapter.pages.get(page_num)
        if not entries:
            return True, "ok", {
                "code": 0,
                "success": True,
                "data": {"note_infos": [], "total": FakeQianfanAdapter.total, "end_tag": True},
            }
        return True, "ok", {
            "code": 0,
            "success": True,
            "data": {
                "note_infos": entries,
                "total": FakeQianfanAdapter.total,
                "end_tag": False,
            },
        }


def _qianfan_entry(note_id: str, item_id: str, title: str, variant: str = "") -> dict:
    """千帆商品笔记条目，字段名照抄真实响应（设计文档 §19.3）。

    指标部分同样是真实字段：`browse_num` / `note_browse_num30d` / 四项互动 /
    `note_pay_gmv` / `note_pay_order_cnt` / `note_status` —— 「创作者账户数据
    全部接入千帆」指的就是它们要原样到达前端。
    """
    return {
        "note_id": note_id,
        "note_title": f"笔记 {note_id}",
        "note_flag": "goods_note",
        "note_image": f"http://ci.example/{note_id}.jpg",
        "browse_num": 286,
        "note_browse_num30d": 26,
        "like_num": 12,
        "collect_num": 3,
        "comment_num": 1,
        "share_num": 0,
        "note_pay_gmv": 0,
        "note_pay_order_cnt": 0,
        "note_status": 0,
        "note_type": 1,
        "publish_time": 1786584623000,
        "xsec_token": "ARK-TOKEN-1",
        "item_id": item_id,
        "item_title": title,
        "item_image": f"http://qimg.example/{item_id}.jpg",
        "variant_desc": variant,
        "goods_infos": [
            {
                "item_id": item_id,
                "item_title": title,
                "item_image": f"http://qimg.example/{item_id}.jpg",
                "variant_desc": variant,
            }
        ],
    }


def _bind_shop(headers, account_id: int, shop_name: str = "观隅好物的店", total: int = 2, seed: bool = True):
    """给账号开通千帆能力（ADR-021：不再有「绑店」动作）；映射行**直写存储层**播种。

    原实现打 `POST /xhs/qianfan/bind`，该端点已随店铺实体退场（返回 `410`
    `QIANFAN_BINDING_REMOVED`）。现在等价动作是**给账号导入千帆（ark）登录态**
    —— 凭据操作，不是绑定。`shop_name` 参数保留只为不改调用方（真值现在取自
    账号自身 `nickname`，本夹具据此回写以保证断言仍成立）。

    v1.32 起 `/xhs/qianfan/sync-goods` 已移除（设计 13 §30.2）⇒ 测试不再
    经 HTTP 播种映射，改为直接调 `store_entry`（按需补齐 / 笔记同步**同一个**
    存储实现，语义不变）。`FakeQianfanAdapter.pages` 即种子的页面来源，
    调用前设置好即可；`total` 写进 `shop_state.goods_note_total`（覆盖率分母）。
    """
    db0 = _session()
    try:
        account0 = db0.get(PlatformAccount, account_id)
        assert account0 is not None
        account0.nickname = shop_name
        give_ark_cookies(db0, account0)
        db0.commit()
    finally:
        db0.close()
    FakeQianfanAdapter.total = total
    if seed:
        db = _session()
        try:
            account = db.get(PlatformAccount, account_id)
            rows = existing_rows(db, account_id)
            for page in FakeQianfanAdapter.pages.values():
                for entry in page:
                    store_entry(db, account.user_id, account_id, rows, entry)
            set_shop_state(
                account,
                goods_note_total=total,
                goods_synced_at=shanghai_now().isoformat(),
            )
            db.commit()
        finally:
            db.close()
    #: 返回形状对齐旧端点响应：调用方只读 `shop_name`（账号昵称即真值）。
    return {"shop_name": shop_name}


#: 测试**统一使用的账号**：真机那个「观隅好物」（小红书号 293066917）。
#: 目标账号一律用它 —— 真机复现、平台响应、截图、日志对照的都是同一个账号，
#: 免得读用例时要在「我的PC号 / 我的创作号 / …」之间换算。
#: 需要**对比**的行（别人的号、陌生会话）保留各自的区分名，否则测不出越权。
GUANYU_NICKNAME = "观隅好物"
GUANYU_RED_ID = "293066917"
GUANYU_PROFILE = json.dumps({"red_id": GUANYU_RED_ID})


def _setup(tmp_path, mode: str = "ok"):
    """Build an isolated DB with two users and five accounts.

    The accounts are chosen to pin down the capability-first routing rule
    (设计文档 §17.3) rather than the account's declared type:

    | account            | sub_type | web_session | expected link          |
    |--------------------|----------|-------------|------------------------|
    | pc                 | pc       | yes         | pc                     |
    | creator            | creator  | no          | creator                |
    | creator_with_web   | creator  | yes         | pc   ← capability wins |
    | stale_pc           | pc       | no          | refused (400)          |
    | no_cookie          | pc       | —           | 400 no cookies         |

    Returns ``(owner_headers, intruder_headers, account_ids)``.
    """
    engine = make_engine(tmp_path / 'self-content.db')
    #: 2026-10-26：同进程复用 schema ⇒ 表只建一次，之后只清数据。
    _schema = _SCHEMA_BY_ENGINE.get(engine)
    if _schema is not None and _BUILT_SCHEMAS.get(_schema):
        reset_test_data(engine)
    else:
        Base.metadata.create_all(bind=engine)
        if _schema is not None:
            _BUILT_SCHEMAS[_schema] = True
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # 供用例直接查/改测试库（例如「清掉映射，模拟从未同步过的店铺」）。
    global _TEST_SESSION
    _TEST_SESSION = SessionLocal

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    FakeSelfContentAdapter.mode = mode
    FakeSelfContentAdapter.calls = {}
    FakeCreatorAdapter.mode = mode
    FakeCreatorAdapter.calls = {}
    FakeQianfanAdapter.mode = "ok"
    FakeQianfanAdapter.calls = {}
    FakeQianfanAdapter.total = 2
    FakeQianfanAdapter.items_mode = "ok"
    FakeQianfanAdapter.item_queries = []
    FakeQianfanAdapter.note_id_queries = []
    # 商品接口的价格桩：默认只登记映射里那件商品（ITEM-A，54.40~144.00 元）。
    FakeQianfanAdapter.item_prices = {"ITEM-A": (5440, 14400)}
    # 默认桩：一条商品笔记，note_id 与 FakeCreatorAdapter 的第一条对齐 —— 「我的笔记」
    # 与千帆之间的 join 键就是这个 note_id。
    FakeQianfanAdapter.pages = {
        1: [_qianfan_entry("6a7d1e2f000000002c001c33", "ITEM-A", "挂载商品甲", "喵宠开心/40*60CM")]
    }
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: FakeSelfContentAdapter
    app.dependency_overrides[get_xhs_creator_api_adapter_factory] = lambda: FakeCreatorAdapter
    app.dependency_overrides[get_qianfan_api_adapter_factory] = lambda: FakeQianfanAdapter

    def register(username: str) -> str:
        response = client.post("/api/auth/register", json={"username": username, "password": "secret123"})
        assert response.status_code == 200, response.text
        return response.json()["access_token"]

    owner_headers = {"Authorization": f"Bearer {register('owner')}"}
    intruder_headers = {"Authorization": f"Bearer {register('intruder')}"}

    db = SessionLocal()
    try:
        owner_id = db.scalar(select(User.id).where(User.username == "owner"))
        intruder_id = db.scalar(select(User.id).where(User.username == "intruder"))
        owned_pc = PlatformAccount(
            user_id=owner_id, platform="xhs", sub_type="pc",
            nickname=GUANYU_NICKNAME, profile_json=GUANYU_PROFILE, status="active"
        )
        owned_creator = PlatformAccount(
            user_id=owner_id, platform="xhs", sub_type="creator",
            nickname=GUANYU_NICKNAME, profile_json=GUANYU_PROFILE, status="active"
        )
        owned_no_cookie = PlatformAccount(
            user_id=owner_id, platform="xhs", sub_type="pc",
            nickname=GUANYU_NICKNAME, profile_json=GUANYU_PROFILE, status="active"
        )
        # A creator-type account that *does* hold a www session. It must be served
        # by the PC link — proof that capability, not sub_type, decides.
        owned_creator_with_web = PlatformAccount(
            user_id=owner_id, platform="xhs", sub_type="creator",
            nickname=GUANYU_NICKNAME, profile_json=GUANYU_PROFILE, status="active"
        )
        # The mirror image: a pc-type account whose stored jar has no www session.
        # Type alone would wave it through, capability must refuse it.
        owned_stale_pc = PlatformAccount(
            user_id=owner_id, platform="xhs", sub_type="pc",
            nickname=GUANYU_NICKNAME, profile_json=GUANYU_PROFILE, status="active"
        )
        foreign_pc = PlatformAccount(
            user_id=intruder_id, platform="xhs", sub_type="pc", nickname="别人的号", status="active"
        )
        db.add_all(
            [owned_pc, owned_creator, owned_no_cookie, owned_creator_with_web, owned_stale_pc, foreign_pc]
        )
        db.commit()
        # Cookie versions, one per account that should have one.
        db.add_all(
            [
                cookie_version(owned_pc.id, COOKIE_PAYLOAD, CHANNEL_WWW),
                # 拆分后（2026-09-21）：创作者 jar 落 **creator 平台**，不再是 ark ——
                # 它没有 ark 域令牌，打千帆只会 -13000（观隅好物的教训）。
                cookie_version(owned_creator.id, CREATOR_COOKIE_PAYLOAD, CHANNEL_CREATOR),
                cookie_version(owned_creator_with_web.id, COOKIE_PAYLOAD, CHANNEL_WWW),
                cookie_version(owned_stale_pc.id, CREATOR_COOKIE_PAYLOAD, CHANNEL_CREATOR),
            ]
        )
        db.commit()
        ids = {
            "pc": owned_pc.id,
            "creator": owned_creator.id,
            "no_cookie": owned_no_cookie.id,
            "creator_with_web": owned_creator_with_web.id,
            "stale_pc": owned_stale_pc.id,
            "foreign": foreign_pc.id,
        }
        return owner_headers, intruder_headers, ids
    finally:
        db.close()


def _teardown():
    app.dependency_overrides.clear()


#: `_setup()` 建的测试库会话工厂；用例用它直接查/改数据（不改生产代码路径）。
_TEST_SESSION = None


def _session():
    """拿到当前测试库的 Session（必须在 `_setup()` 之后调用）。"""
    assert _TEST_SESSION is not None, "先调 _setup()"
    return _TEST_SESSION()


# ---------------------------------------------------------------------------
# 鉴权与账号归属（设计文档 §10）
# ---------------------------------------------------------------------------


def test_self_endpoints_require_authentication(tmp_path):
    """六个端点未认证一律 401 —— 不允许匿名读任何账号数据。"""
    _setup(tmp_path)
    try:
        cases = [
            ("/api/xhs/pc/users/me", {"account_id": 1}),
            ("/api/xhs/pc/users/capabilities", {"account_id": 1}),
            ("/api/xhs/pc/users/notes", {"account_id": 1}),
            ("/api/xhs/pc/users/liked", {"account_id": 1}),
            ("/api/xhs/pc/users/collected", {"account_id": 1}),
            ("/api/xhs/pc/users/interactions", {"account_id": 1, "kind": "mention"}),
        ]
        for path, body in cases:
            response = client.post(path, json=body)
            assert response.status_code == 401, f"{path} 应要求认证，实际 {response.status_code}"
    finally:
        _teardown()


def test_self_notes_rejects_account_owned_by_another_user(tmp_path):
    """越权防护：读到别人的 account_id 必须 404，不能给出任何数据。"""
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post("/api/xhs/pc/users/notes", json={"account_id": ids["foreign"]}, headers=headers)
        assert response.status_code == 404
        assert response.json()["detail"] == "Account not found"
        assert "posted" not in FakeSelfContentAdapter.calls, "被拒的请求不得触发任何平台调用"
    finally:
        _teardown()


def test_creator_account_can_load_its_published_notes(tmp_path):
    """creator 账号的「我发布的」必须可用，并走创作者平台接口。

    这是用户反馈的原始缺陷：创作者账号同样能发布笔记，不能因为账号类型就把整页挡掉。
    v1.0 的实现用 `sub_type != "pc"` 直接 404，本用例锁定"路由到创作者链路"这一契约。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post("/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["items"], "创作者账号应能取到笔记列表"
        assert payload["token_source"] == "creator"
        assert FakeCreatorAdapter.calls.get("posted_page_args") == (0, 0), "首请求必须是第 0 页"
        # 关键：不得为了图省事去调 PC 链路
        assert "posted" not in FakeSelfContentAdapter.calls, "creator 账号不应触碰 PC 列表接口"
    finally:
        _teardown()


def test_creator_note_items_are_normalized(tmp_path):
    """创作者笔记按**实测字段**归一化，而不是按猜测的字段名。

    夹具与真实响应逐字段对齐（``id`` / ``display_title`` / ``images_list`` /
    扁平 ``likes`` / 字符串 ``time``）。一个把字段猜错的实现会在这里全红，
    而不是像 2026-09-18 那样在真机上「title 对、封面空、点赞恒为 0」。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post("/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers)
        item = response.json()["items"][0]
        assert item["note_id"] == "6a7d1e2f000000002c001c33"
        assert item["title"] == "化妆室浴室地垫仿亚麻编织吸水速干脚垫防滑"
        # The explore URL must carry the list's own xsec_token: without it the
        # note page rejects the scene-less link (design doc §18.4).
        assert item["note_url"] == (
            "https://www.xiaohongshu.com/explore/6a7d1e2f000000002c001c33?xsec_token=TOKEN-1"
        )
        assert item["likes"] == 12 and item["collects"] == 3 and item["comments"] == 1
        assert item["shares"] == 0 and item["views"] == 2
        assert item["type"] == "normal"
        assert item["timestamp"] == "2026-08-13 09:30", "time 是字符串日期，不是时间戳"
        assert item["raw"]["id"] == "6a7d1e2f000000002c001c33", "原始条目必须保留，便于结构变更时排查"
    finally:
        _teardown()


def test_creator_note_images_come_from_images_list(tmp_path):
    """多图必须来自 ``images_list``（真实字段），封面取第一张。"""
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post("/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers)
        item = response.json()["items"][0]
        assert item["image_urls"] == [
            "https://cdn.example/c1-1.jpg",
            "https://cdn.example/c1-2.jpg",
        ]
        assert item["cover_url"] == "https://cdn.example/c1-1.jpg"
    finally:
        _teardown()


def test_creator_list_has_no_body_and_says_so_by_staying_empty(tmp_path):
    """创作者列表没有正文字段 —— ``content`` 必须是空串，而不是猜测或截断。

    这条断言描述的是**平台能力**，不是实现细节：正文只能靠详情接口按需取。
    若将来平台补上正文字段，这条会红，那正是需要回写设计文档的信号。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post("/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers)
        item = response.json()["items"][0]
        assert item["content"] == ""
    finally:
        _teardown()


def test_empty_goods_array_never_becomes_a_phantom_goods_entry(tmp_path):
    """``goods_name: []`` 必须退化为「无商品」。

    历史实现用 ``_first_text`` 读数组，``str([])`` → ``"[]"``，于是每篇笔记都
    会挂着一个标题为 ``"[]"`` 的假商品。这条断言把这个回归钉死。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post("/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers)
        items = response.json()["items"]
        assert items[0]["goods"] is None, "空商品数组不得造出商品对象"
        assert items[1]["goods"]["title"] == "测评商品", "真实商品数组要能读出来"
        assert items[1]["goods"]["goods_id"] == "G-1"
    finally:
        _teardown()


def test_creator_total_comes_from_tag_counts_not_the_page_size(tmp_path):
    """总数取 ``tags`` 里选中项的 ``notes_count``，不得等于当前页条数。"""
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post("/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers)
        payload = response.json()
        assert payload["total"] == 1969
        assert payload["total"] != len(payload["items"]), "页大小冒充总数会让分页显示失控"
    finally:
        _teardown()


def test_creator_note_url_keeps_its_own_xsec_token(tmp_path):
    """创作者列表的 ``note_url`` 必须带上该条自带的 ``xsec_token``。

    平台按签发场景绑定令牌：一个不带令牌的 explore 链接会被笔记页拒绝，
    用户点「在小红书打开」就打不开。这不是展示问题，是链接可用性问题。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post("/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers)
        items = response.json()["items"]
        assert "xsec_token=TOKEN-1" in items[0]["note_url"], "链接必须携带列表自带的令牌"
        assert items[0]["note_url"].startswith("https://www.xiaohongshu.com/explore/")
    finally:
        _teardown()


def test_creator_note_url_without_token_still_resolves_without_fabricating(tmp_path):
    """没有 ``xsec_token`` 时给出不带令牌的链接，不得编造令牌或空指针。

    令牌缺失时链接仍应可点（平台对部分条目确实不发令牌），只是不能假装
    拿到了令牌。
    """
    import copy

    original = FakeCreatorAdapter.get_posted_notes_page

    def without_token(self, page: int = 0, tab: int = 0):
        success, message, payload = original(self, page, tab)
        payload = copy.deepcopy(payload)
        for note in (payload or {}).get("data", {}).get("notes", []):
            note.pop("xsec_token", None)
            note.pop("xsec_source", None)
        return success, message, payload

    FakeCreatorAdapter.get_posted_notes_page = without_token
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post("/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers)
        item = response.json()["items"][0]
        assert item["xsec_token"] == ""
        assert item["note_url"] == "https://www.xiaohongshu.com/explore/6a7d1e2f000000002c001c33"
        assert "xsec_token=" not in item["note_url"], "缺令牌时不得编造令牌参数"
    finally:
        FakeCreatorAdapter.get_posted_notes_page = original
        _teardown()


def test_capability_beats_account_type_when_creator_holds_web_session(tmp_path):
    """creator 账号若持有网页会话，应走 PC 链路 —— 能力优先于类型。

    证明路由顺序是「先问能力、再问类型」，而不是把类型当成能力的替身。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post(
            "/api/xhs/pc/users/notes", json={"account_id": ids["creator_with_web"]}, headers=headers
        )
        assert response.status_code == 200, response.text
        assert "posted" in FakeSelfContentAdapter.calls, "有 web_session 就该走 PC 链路"
        assert "posted_page_args" not in FakeCreatorAdapter.calls, "不该绕道创作者接口"
    finally:
        _teardown()


def test_pc_account_without_web_session_is_refused_with_actionable_reason(tmp_path):
    """pc 账号但 Cookie 无网页会话 → 400 WEB_SESSION_REQUIRED，且原因是可读的。

    类型正确不等于能力具备；这里的重点是**不能**给出一个用户无从下手的原因。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post("/api/xhs/pc/users/notes", json={"account_id": ids["stale_pc"]}, headers=headers)
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["code"] == "WEB_SESSION_REQUIRED"
        assert "网页会话" in detail["message"]
        assert "posted" not in FakeSelfContentAdapter.calls
    finally:
        _teardown()


def test_creator_account_without_own_web_session_is_refused(tmp_path):
    """creator 账号自己没有网页会话 → 400 + 可读原因（这是**唯一**的降级口径）。

    置灰（可读的 400）而不是 404 —— 404 会被理解成「账号不存在」。

    这三类数据只有 www 提供，而这个账号手里没有 www 令牌 ⇒ 拒绝，并**零平台请求**。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        cases = [
            ("/api/xhs/pc/users/liked", {"account_id": ids["creator"]}),
            ("/api/xhs/pc/users/collected", {"account_id": ids["creator"]}),
            ("/api/xhs/pc/users/interactions", {"account_id": ids["creator"], "kind": "mention"}),
        ]
        for path, body in cases:
            response = client.post(path, json=body, headers=headers)
            assert response.status_code == 400, f"{path} 实际 {response.status_code}"
            assert response.json()["detail"]["code"] == "WEB_SESSION_REQUIRED"
        assert not FakeSelfContentAdapter.calls, "没有凭据就不该发任何平台请求"
    finally:
        _teardown()


def test_creator_note_detail_is_refused_even_when_a_sibling_session_exists(tmp_path):
    """账号只有 creator 令牌时，详情**不借任何别的账号的会话**，直接给结构化失败。

    这里刻意造出「库里确实存在一份可用的 www 会话」（同一个人的另一个账号）：
    那是**最容易顺手用掉的一种**，也正是本条要钉住的越权 —— 借来的会话读到的
    始终是**别人的数据面**。正确动作是让用户给**这个账号**补登网页版。

    判据有三条，缺一不可：① 400（不是 500、也不是假装成功的空正文）；
    ② `code = WEB_SESSION_REQUIRED`；③ **一个平台请求都没发**
    （不试探、不探活 —— 连「能不能借」都不去问平台）。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        _add_sibling(ids, nickname="另一个账号", external_user_id="OTHER-1")
        url = "https://www.xiaohongshu.com/explore/6a7d1e2f000000002c001c33"
        response = client.post(
            "/api/xhs/pc/notes/detail", json={"account_id": ids["creator"], "url": url}, headers=headers
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "WEB_SESSION_REQUIRED"
        assert not FakeSelfContentAdapter.calls, "别人的会话不该被碰，连探活都不行"
    finally:
        _teardown()


def test_note_detail_without_own_web_session_gives_structured_reason(tmp_path):
    """没有任何可用会话时：400 是**结构化**原因，且不得漏出内部错误码（§17.7）。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        # 清掉库里所有 www 令牌（含信封里多平台的那一行只摘 www）。
        db = _session()
        for row in db.scalars(select(AccountCookieVersion)).all():
            platforms = parse_channel_envelope(decrypt_text(row.encrypted_cookies))
            if CHANNEL_WWW not in platforms:
                continue
            if set(platforms) == {CHANNEL_WWW}:
                db.delete(row)
            else:
                # 一行多平台时只摘掉 www（其它平台原样留着）
                platforms.pop(CHANNEL_WWW)
                row.encrypted_cookies = encrypt_text(dump_channel_envelope(platforms))
        db.commit()
        db.close()

        url = "https://www.xiaohongshu.com/explore/6a7d1e2f000000002c001c33"
        response = client.post(
            "/api/xhs/pc/notes/detail", json={"account_id": ids["creator"], "url": url}, headers=headers
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert isinstance(detail, dict), f"应为结构化原因，实际：{detail!r}"
        assert detail["code"] == "WEB_SESSION_REQUIRED"
        # 内部错误码不得再漏到界面上（用户 2026-09-21 实测报出的就是这一句）。
        assert "Account has no cookies" not in str(detail)
        # 原因必须**可执行**：告诉用户给哪个账号、去哪里补登录。
        # ⚠️ 这里刻意不去找某个旧词（负向 `not in` 会因别处改文案而静默恒真），
        # 只钉住「说的是补登录这件事」。
        assert "网页会话" in detail["message"]
        assert "登录" in detail["message"]
        assert not FakeSelfContentAdapter.calls, "没有凭据就不该发任何平台请求"
    finally:
        _teardown()


def test_ownership_errors_are_not_swallowed_by_the_fallback(tmp_path):
    """兜底**不能变成越权**：只吞「这个账号没存令牌」，归属错误必须照旧 404。

    DEF-055 的租户闸与软删闸在 `get_owned_active_account()` 里，它在兜底之前执行 ——
    这条断言就是防「顺手把整个 400/404 都 catch 掉」的写法。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        for path, body in (
            ("/api/xhs/pc/notes/detail", {"account_id": ids["foreign"], "url": "https://www.xiaohongshu.com/explore/x"}),
            ("/api/xhs/pc/notes/comments", {"account_id": ids["foreign"], "note_url": "https://www.xiaohongshu.com/explore/x"}),
            ("/api/xhs/pc/users/notes", {"account_id": ids["foreign"]}),
        ):
            response = client.post(path, json=body, headers=headers)
            assert response.status_code == 404, f"{path} 实际 {response.status_code}"
        assert not FakeSelfContentAdapter.calls, "越权请求不该触发任何平台调用"
    finally:
        _teardown()


def test_user_scope_endpoints_never_use_a_sibling_session(tmp_path):
    """用户维度三分类**一个别的账号的会话都不碰**。

    这三类回答的是「**这个人**做过什么」。借别人的会话会把别人的赞过 / 收藏 /
    收到的互动当成自己的展示 —— 那不是「少数据」，是**显示错数据**。

    必须走到结构化拒绝，且**零平台调用** —— 连「这份会话还能不能用」都不去问平台。

    ⚠️ 原用例参数化了「同一个人 / 陌生人」两种形状；**2026-09-26 起前者已不可能
    存在**（同一小红书 ID 只允许一行，见 `test_same_xhs_identity_never_produces_two_rows`），
    所以只剩「别人的可用会话」这一种诱因 —— 它也正是这条要钉住的越权。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        _add_sibling(ids, nickname="别人的号", external_user_id="OTHER-9")
        for path, body in (
            ("/api/xhs/pc/users/liked", {"account_id": ids["creator"]}),
            ("/api/xhs/pc/users/collected", {"account_id": ids["creator"]}),
            ("/api/xhs/pc/users/interactions", {"account_id": ids["creator"], "kind": "mention"}),
        ):
            response = client.post(path, json=body, headers=headers)
            assert response.status_code == 400, f"{path} 实际 {response.status_code}"
            assert response.json()["detail"]["code"] == "WEB_SESSION_REQUIRED"
        assert not FakeSelfContentAdapter.calls, "兄弟账号的会话不该被碰，连探活都不行"
    finally:
        _teardown()


def _add_sibling(ids, *, nickname, external_user_id, payload=COOKIE_PAYLOAD):
    """给 `creator` 账号挂一个**兄弟账号**（另一个身份 + 一份可用的 www 会话）。

    用途：造出「库里确实存在一份可用的 www 会话」这个**最容易顺手用掉**的形状，
    验证接口仍然**只认本账号**的凭据（借来的会话读到的始终是别人的数据面）。

    ⚠️ **2026-09-26 起「同一个人的第二行」造不出来了**：用户口径「同一小红书 ID
    不允许出现重复多行账号」⇒ 部分唯一索引换成
    `uq_platform_accounts_xhs_identity (user_id, platform, external_user_id)`
    （原索引含 `sub_type`，`pc` / `creator` 各算一个身份，正是同一个账号在库里
    留下两行的原因）。所以现在唯一的诱因形状就是**别人**的可用会话 ——
    与「凭据只认本账号」要钉住的东西完全一致（借来的会话本来就是别人的数据面）。
    「同一身份只允许一行」这件事由
    `test_same_xhs_identity_never_produces_two_rows` 单独钉住。
    """
    db = _session()
    try:
        creator = db.get(PlatformAccount, ids["creator"])
        sibling = PlatformAccount(
            user_id=creator.user_id,
            platform="xhs",
            sub_type="pc",
            nickname=nickname,
            external_user_id=external_user_id,
            profile_json="{}",
            status="active",
        )
        db.add(sibling)
        db.commit()
        db.add(cookie_version(sibling.id, payload, CHANNEL_WWW))
        db.commit()
        return sibling.id
    finally:
        db.close()


def test_same_xhs_identity_never_produces_two_rows(tmp_path):
    """**同一小红书 ID 不允许出现第二行**（用户 2026-09-26 口径）。

    真实故障：同一个账号先按「PC」扫码、再按「创作者平台」扫码，库里留下两行 ——
    同 `external_user_id`、同昵称、同小红书号，界面显示「两个相同的账号信息」，
    只差一个「类型」。原因是原唯一索引的键含 `sub_type`（两条链路各算一个身份）。

    两道锁都要钉（缺一条都拦不住）：

    ① **DB 层**：直接插一行同 `(user_id, platform, external_user_id)` 会被
       `uq_platform_accounts_xhs_identity` 拒绝 —— 这是最后一道，任何写入路径都绕不过；
    ② **业务层**：`upsert_platform_account_from_login()` 按小红书 ID 找行，
       第二条链路**合并到同一行**，且 `sub_type` 取**更强**的那个
       （取 `pc` 会静默丢掉发布能力 —— 发布闸门要求 `sub_type == "creator"`）。
    """
    from sqlalchemy.exc import IntegrityError

    from backend.app.services.account_service import upsert_platform_account_from_login

    headers, _, ids = _setup(tmp_path)
    try:
        db = _session()
        try:
            creator = db.get(PlatformAccount, ids["creator"])
            creator.external_user_id = "SAME-XHS-1"
            db.commit()

            # ① DB 层：同身份第二行必须被拒
            db.add(
                PlatformAccount(
                    user_id=creator.user_id,
                    platform="xhs",
                    sub_type="pc",
                    nickname="观隅好物",
                    external_user_id="SAME-XHS-1",
                    status="active",
                )
            )
            try:
                db.commit()
                raise AssertionError("同一个小红书 ID 的第二行竟然写进去了")
            except IntegrityError:
                db.rollback()

            # ② 业务层：第二条链路合并到同一行，sub_type 取更强的那个
            account, action = upsert_platform_account_from_login(
                db=db,
                user_id=creator.user_id,
                platform="xhs",
                sub_type="pc",
                user_info={"external_user_id": "SAME-XHS-1", "nickname": "观隅好物"},
                cookies_text=COOKIE_PAYLOAD,
            )
            assert account.id == creator.id, "第二条链路必须合并到同一行"
            assert action == "updated"
            rows = db.scalars(
                select(PlatformAccount).where(PlatformAccount.external_user_id == "SAME-XHS-1")
            ).all()
            assert len(rows) == 1, f"同一个小红书 ID 出现了 {len(rows)} 行"
            assert rows[0].sub_type == "creator", "合并不得把发布链路降级成 pc"
        finally:
            db.close()
    finally:
        _teardown()


def test_sibling_session_does_not_change_the_outcome(tmp_path):
    """**同一个人的另一个账号**在网页版登录过，也不改变本账号的结论。

    这条是「凭据只认本账号」的核心：库里存在一份**完全可用、且属于同一个人**的
    www 会话，最容易被认为是「自己的」，但接口仍然给 400 + `WEB_SESSION_REQUIRED`，
    并且**一个平台请求都不发**。给用户的动作是「给这个账号补登网页版」。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        _add_sibling(ids, nickname="另一个账号", external_user_id="OTHER-1")
        for path, body in (
            ("/api/xhs/pc/users/liked", {"account_id": ids["creator"]}),
            ("/api/xhs/pc/users/collected", {"account_id": ids["creator"]}),
            ("/api/xhs/pc/users/interactions", {"account_id": ids["creator"], "kind": "mention"}),
        ):
            response = client.post(path, json=body, headers=headers)
            assert response.status_code == 400, f"{path} 实际 {response.status_code}"
            assert response.json()["detail"]["code"] == "WEB_SESSION_REQUIRED"
        assert not FakeSelfContentAdapter.calls, "兄弟账号的会话不该被碰"
    finally:
        _teardown()


def test_stale_sibling_session_is_irrelevant(tmp_path):
    """兄弟账号的会话是死是活，与本账号能不能读**完全无关**（连探活都不发）。

    老实现要先 `get_self_profile()` 探活才敢用借来的 jar，于是「兄弟的会话过期了」
    会变成用户看到的一次失败。判据换成「只认自己的令牌」之后，这条路整个消失：
    无论兄弟那份 jar 有效还是失效，本接口的行为与它无关。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        _add_sibling(ids, nickname="另一个账号", external_user_id="OTHER-1")
        FakeSelfContentAdapter.mode = "profile_fail"  # 兄弟账号的会话已过期
        response = client.post(
            "/api/xhs/pc/users/liked", json={"account_id": ids["creator"]}, headers=headers
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "WEB_SESSION_REQUIRED"
        assert not FakeSelfContentAdapter.calls, "与本账号无关的会话，连探活都不该发生"
    finally:
        _teardown()


def test_capabilities_report_own_session_source_only(tmp_path):
    """能力声明只讲**账号自己的**会话来源，前端才不会被点亮成「可读」。

    * 账号自己没有 www 会话 → `session_source=""`、三类 `available=False` + 原因行；
    * 即使库里存在同身份的可用 www 会话，也**不得**把它算进来（否则 Tab 会亮着，
      一点就报错）；
    * 全程**零平台调用**：不再有「探活借来的会话」这一步。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        _add_sibling(ids, nickname="另一个账号", external_user_id="OTHER-1")
        payload = client.post(
            "/api/xhs/pc/users/capabilities", json={"account_id": ids["creator"]}, headers=headers
        ).json()
        assert payload["has_web_session"] is False
        assert payload["session_source"] == ""
        for scope in ("liked", "collected", "interactions"):
            assert payload["capabilities"][scope]["available"] is False, scope
            assert payload["capabilities"][scope]["reason"], "置灰必须带原因"
            assert payload["capabilities"][scope]["session_source"] == ""
        assert not FakeSelfContentAdapter.calls, "能力端点不得触发任何平台调用"
    finally:
        _teardown()


def test_creator_identity_is_whitelisted_and_never_leaks_phone(tmp_path):
    """创作者身份接口返回 phone，响应里绝不能出现。

    安全契约：平台载荷是不可控的外部输入，必须白名单构造，不能整体透传。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post("/api/xhs/pc/users/me", json={"account_id": ids["creator"]}, headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["user_id"] == "CREATOR-1"
        assert payload["nickname"] == "欢喜"
        assert payload["link"] == "creator"
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "19253615941" not in serialized, "手机号绝不能出现在响应中"
        assert "phone" not in serialized
        assert "realNameVerified" not in serialized
    finally:
        _teardown()


def test_capabilities_endpoint_is_purely_local(tmp_path):
    """能力端点只读本地账号行与 Cookie —— 一次平台调用都不能有。

    这是它能在身份都拿不到时依然可用的前提（前端要靠它决定置灰哪些 Tab）。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post(
            "/api/xhs/pc/users/capabilities", json={"account_id": ids["creator"]}, headers=headers
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["has_web_session"] is False
        assert payload["notes_link"] == "creator"
        assert payload["capabilities"]["notes"]["available"] is True
        for scope in ("liked", "collected", "interactions"):
            assert payload["capabilities"][scope]["available"] is False
            assert payload["capabilities"][scope]["reason"], "置灰必须带原因，不能让用户猜"
        assert payload["capabilities"]["goods"]["available"] is False
        assert not FakeSelfContentAdapter.calls, "能力端点不得触发任何平台调用"
        assert not FakeCreatorAdapter.calls, "能力端点不得触发任何平台调用"
    finally:
        _teardown()


def test_capabilities_for_pc_account_opens_everything_but_goods(tmp_path):
    """有网页会话的账号：四个业务 Tab 全开；商品因链路限制仍为不可用。"""
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post(
            "/api/xhs/pc/users/capabilities", json={"account_id": ids["pc"]}, headers=headers
        )
        payload = response.json()
        assert payload["has_web_session"] is True
        assert payload["notes_link"] == "pc"
        for scope in ("notes", "liked", "collected", "interactions"):
            assert payload["capabilities"][scope]["available"] is True
        assert payload["capabilities"]["goods"]["available"] is False
    finally:
        _teardown()


def test_goods_capability_reports_ark_source_and_stays_actionable(tmp_path):
    """商品能力必须声明**真实来源（千帆 / ark）**，并把「怎么才能看到」讲出来。

    历史：这里曾断言「两条链路都不返回商品」，那是在**没有千帆接线**时的诚实结论。
    2026-09-19 决定把商品来源切到千帆（实测该账号确实有店铺且笔记挂了商品，
    见设计文档 §19），于是这条用例的契约随之升级：

    * 未绑定时 `available=False`，原因必须点名千帆 / ark，且给出可执行动作（绑定 / 同步）；
    * 不允许回退成「PC 链路不返回……」这种**没有承接方**的占位说明。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        for key in ("pc", "creator"):
            response = client.post(
                "/api/xhs/pc/users/capabilities", json={"account_id": ids[key]}, headers=headers
            )
            goods = response.json()["capabilities"]["goods"]
            assert goods["source"] == "ark", "商品能力的来源必须显式声明为千帆"
            assert goods["available"] is False
            reason = goods["reason"]
            assert reason, "不可用必须带原因，不能让用户猜"
            assert "千帆" in reason or "ark" in reason, f"原因必须点名来源，实际：{reason!r}"
            assert "绑定" in reason, f"原因必须给出可执行动作，实际：{reason!r}"
            # 反向守卫：旧的占位文案（没有承接方）不得回归
            assert "待千帆商家后台接入" not in reason
            assert "PC 链路不返回" not in reason
    finally:
        _teardown()


def test_goods_capability_flips_to_available_once_bound(tmp_path):
    """绑定千帆后，商品能力必须变为可用（这是本次二开的核心验收点）。"""
    headers, _, ids = _setup(tmp_path)
    try:
        before = client.post(
            "/api/xhs/pc/users/capabilities", json={"account_id": ids["creator"]}, headers=headers
        ).json()["capabilities"]["goods"]
        assert before["available"] is False

        _bind_shop(headers, ids["creator"], shop_name="观隅好物的店", total=1968)

        after = client.post(
            "/api/xhs/pc/users/capabilities", json={"account_id": ids["creator"]}, headers=headers
        ).json()["capabilities"]["goods"]
        assert after["available"] is True
        assert after["reason"] == "", "可用时不得再挂原因文案"
        assert after["shop_name"] == "观隅好物的店"
        assert after["shop_total"] == 1968
        assert after["synced_notes"] == 1
    finally:
        _teardown()


# ---------------------------------------------------------------------------
# 千帆（ark）商品接线（设计文档 §19 / 本次二开）
#
# 这一组的每一条都锁着一个「读不到却被读成没有」的失败模式：
# 商品来自另一个后台，join 键是 note_id，同步是有界的。
# ---------------------------------------------------------------------------


def test_note_detail_returns_the_same_goods_the_list_carries(tmp_path):
    """详情接口必须给**与列表同一形状**的挂载商品（DEF-047）。

    用户 2026-09-20 报出的现象：笔记详情弹窗一打开商品卡片是完整的，正文加载完之后
    只剩一行空图。根因是详情走 www feed，那个 payload **结构上没有商品容器**（§19.1）
    → `goods` 恒 `None`；前端 `{...note, ...detail}` 用这个 `null` 覆盖掉列表行已经
    补齐的那一份，而弹窗里另一块 `shop.goods` 版式不同、字段还少（无店铺名/无价格）。

    断言的是「**两边同形状**」而不是「详情也有商品」：只有形状一致，前端才可能只写
    一套渲染 —— 否则就是「同一语义两份实现」，改一处、漂一处。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        account_id = ids["creator_with_web"]
        _bind_shop(headers, account_id)
        # 让千帆映射的 note_id 对齐 www 列表里的那条笔记（现实里本来就是同一个 note_id）。
        db = _session()
        # 2026-09-30（ADR-021）：`shops` 实体与「绑店」动作一并退场 —— 「这个账号
        # 有没有千帆能力」现在**只问令牌**（`has_ark_session`），不再问店铺关系
        # （隔离键 ≠ 能力判据；账号 9 Bug 的根因就是把两者混为一谈）。
        owner = db.get(PlatformAccount, account_id)
        assert owner is not None
        assert has_ark_session(db, account_id), "夹具给该账号导入了 ark 会话"
        # `shop_note_goods.user_id` 是 NOT NULL（归属随账号走）：夹具必须按真实写入路径给值，
        # 少这一列会撞约束红在这里，而不是红在被测代码上。
        db.add(
            ShopNoteGoods(
                user_id=owner.user_id,
                platform_account_id=account_id,
                note_id="N1",
                item_id="ITEM-A",
                item_title="挂载商品甲",
                item_image="http://qimg.example/ITEM-A.jpg",
                variant_desc="喵宠开心/40*60CM",
                goods_json=json.dumps(
                    [
                        {
                            "item_id": "ITEM-A",
                            "item_title": "挂载商品甲",
                            "item_image": "http://qimg.example/ITEM-A.jpg",
                            "variant_desc": "喵宠开心/40*60CM",
                        }
                    ]
                ),
                entry_json="{}",
            )
        )
        db.commit()
        db.close()

        listed = client.post(
            "/api/xhs/pc/users/notes", json={"account_id": account_id}, headers=headers
        )
        assert listed.status_code == 200, listed.text
        from_list = listed.json()["items"][0]["goods"]
        assert from_list is not None and from_list["goods_id"] == "ITEM-A"

        detail = client.post(
            "/api/xhs/pc/notes/detail",
            json={
                "account_id": account_id,
                "url": "https://www.xiaohongshu.com/explore/N1?xsec_token=TK-1&xsec_source=pc_user",
            },
            headers=headers,
        )
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["goods"] is not None, "详情必须带回挂载商品，否则前端一进详情就丢（DEF-047）"
        assert set(body["goods"]) == set(from_list), "详情与列表必须同形状（同一份 NoteGoods 契约）"
        assert body["goods"]["goods_id"] == "ITEM-A"
        assert body["goods"]["title"] == "挂载商品甲"
        assert body["goods"]["variant"] == "喵宠开心/40*60CM"
        assert body["goods"]["shop_name"] == "观隅好物的店"
        assert body["goods"]["url"] == "https://www.xiaohongshu.com/goods-detail/ITEM-A"
        # 详情路径**不**额外打商品接口补价（「打开笔记 ≤1 次」的请求口径不放松）→
        # 价格留空，由前端优先采用列表行那一份（它带价）；缺值不造假（§18.2）。
        assert body["goods"]["price"] == ""
        # 详情本来就带的商品数组原样保留（多商品由前端合并展示）。
        assert body["shop"]["goods"][0]["item_id"] == "ITEM-A"
    finally:
        _teardown()


def test_creator_notes_carry_qianfan_goods(tmp_path):
    """创作者账号的「我发布的」必须带上千帆的挂载商品（本次二开的核心验收点）。

    真实数据：创作者平台的 `goods_info` 是 `{"goods_name": []}` 空壳，
    而同一条 note_id 在千帆商品笔记列表里带着 `item_title` / `item_image`
    （2026-09-19 实测）。所以这条断言就是「修复是否真的生效」。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        _bind_shop(headers, ids["creator"])

        response = client.post(
            "/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        first = payload["items"][0]
        assert first["note_id"] == "6a7d1e2f000000002c001c33"
        assert first["goods"] is not None, "千帆已同步的笔记必须能读到商品"
        assert first["goods"]["title"] == "挂载商品甲"
        assert first["goods"]["goods_id"] == "ITEM-A"
        assert first["goods"]["url"] == "https://www.xiaohongshu.com/goods-detail/ITEM-A"
        assert first["goods"]["shop_name"] == "观隅好物的店"
        assert first["goods"]["variant"] == "喵宠开心/40*60CM"
        # 价格**不在**千帆商品笔记列表里（§19.3 实测）→ 由商品接口按 id 批量补一次：
        # 54.40 与 144.00 元是同价区间的两端（分 → 元只在这里换算一次）。
        assert first["goods"]["price"] == "54.40~144.00"
        assert FakeQianfanAdapter.calls.get("items") == 1, "一页笔记只查一次商品接口"
        assert FakeQianfanAdapter.item_queries[0]["keyword"] == "ITEM-A"
        assert FakeQianfanAdapter.item_queries[0]["status"] == "all", "已下架商品也要能补价"
        # 千帆映射里没有的笔记保持原样：第二条的 `goods_info` 来自创作者列表自身
        # （平台确实给了这个字段时用它），而不是被千帆的映射覆盖成空。
        assert payload["items"][1]["goods"]["goods_id"] == "G-1"

        source = payload["goods_source"]
        assert source["source"] == "ark" and source["bound"] is True
        assert source["shop_total"] == 2 and source["synced_notes"] == 1
    finally:
        _teardown()


def test_page_goods_prices_come_from_one_batched_lookup(tmp_path):
    """价格必须来自商品接口，且**一页只查一次**（多个商品 ID 逗号拼在一个 keyword 里）。

    这是「补齐价格」与「浏览页面不放大平台请求」能同时成立的依据：
    千帆 `whole_keywords` 支持逗号分隔多个商品 ID，实测 3 个 ID 一次请求全部命中。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        # 两条创作者笔记各自挂一件商品 → 本页待补价 2 件。
        FakeQianfanAdapter.pages = {
            1: [
                _qianfan_entry("6a7d1e2f000000002c001c33", "ITEM-A", "挂载商品甲", "喵宠开心/40*60CM"),
                _qianfan_entry("6a7d1e2f000000002c001c44", "ITEM-B", "挂载商品乙", "单件"),
            ]
        }
        FakeQianfanAdapter.total = 2
        FakeQianfanAdapter.item_prices = {"ITEM-A": (5440, 14400), "ITEM-B": (990, 990)}
        _bind_shop(headers, ids["creator"], total=2)

        payload = client.post(
            "/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers
        ).json()
        by_note = {item["note_id"]: item for item in payload["items"]}

        assert FakeQianfanAdapter.calls.get("items") == 1, "两个商品也必须合成一次查询"
        assert FakeQianfanAdapter.item_queries[0]["keyword"] == "ITEM-A,ITEM-B"
        assert FakeQianfanAdapter.item_queries[0]["page_size"] == 2
        # 区间价给两端、单一价给一个数（分 → 元，两位小数）
        assert by_note["6a7d1e2f000000002c001c33"]["goods"]["price"] == "54.40~144.00"
        assert by_note["6a7d1e2f000000002c001c44"]["goods"]["price"] == "9.90"
        assert payload["goods_source"]["price_source"] == "ark:items"
    finally:
        _teardown()


def test_goods_price_lookup_failure_never_breaks_my_notes(tmp_path):
    """查价是**增强项**：商品接口失败时价格留空，但「我的笔记」必须照常 200。

    商品本身（标题/主图/规格/链接）来自本地映射，不依赖这次查询 —— 失败只损失价格。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        _bind_shop(headers, ids["creator"])
        FakeQianfanAdapter.items_mode = "fail"

        response = client.post(
            "/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers
        )
        assert response.status_code == 200, response.text
        first = response.json()["items"][0]
        assert first["goods"]["title"] == "挂载商品甲", "商品本体不受查价失败影响"
        assert first["goods"]["price"] == "", "拿不到价就留空，不造假"
        assert FakeQianfanAdapter.calls.get("items") == 1
    finally:
        _teardown()


def test_goods_price_lookup_only_runs_when_there_is_something_to_price(tmp_path):
    """没绑店铺、或本页没有任何挂链商品时，**一次**商品接口都不许发。

    「浏览我的笔记不产生千帆请求」这条边界在不需要补价时必须依然成立。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        # 情形一：未绑定千帆。
        client.post(
            "/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers
        )
        assert FakeQianfanAdapter.calls.get("items") is None
        assert FakeQianfanAdapter.item_queries == []

        # 情形二：已绑定，但本页笔记都没有千帆映射（映射的是别的 note_id）。
        FakeQianfanAdapter.pages = {
            1: [_qianfan_entry("another-note-id", "ITEM-Z", "别处的商品")],
        }
        FakeQianfanAdapter.item_prices = {"ITEM-Z": (100, 100)}
        _bind_shop(headers, ids["creator"], total=2)
        payload = client.post(
            "/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers
        ).json()
        # 映射的是「别处的 note_id」→ 本页第一条仍然没有商品；
        # 第二条带的是**创作者列表自己给的** `goods_info`（G-1），与千帆无关。
        assert payload["items"][0]["goods"] is None
        assert payload["items"][1]["goods"]["goods_id"] == "G-1"
        assert FakeQianfanAdapter.calls.get("items") is None, "没有待补价的商品就不该查"
    finally:
        _teardown()


def test_browsing_notes_backfills_goods_on_demand(tmp_path):
    """**按需补齐**（T-30 新方案）：打开「我的笔记」时，只为**本页缺映射**的笔记打一次千帆。

    取代了早前的「后台全量同步」：店铺近 2000 条商品笔记，而一页只有 10 条 ——
    只补用户真正看到的那页，把成本从「上百页翻取」压到「**每页 ≤1 次**只读请求」。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        _bind_shop(headers, ids["creator"])
        # 绑定后清掉映射，模拟「从未同步过的店铺」。
        db = _session()
        try:
            for row in db.scalars(select(ShopNoteGoods)).all():
                db.delete(row)
            db.commit()
        finally:
            db.close()

        FakeQianfanAdapter.calls = {}
        FakeQianfanAdapter.note_id_queries = []
        payload = client.post(
            "/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers
        ).json()

        # ① 一页笔记只打一次千帆，且带的是**这一页的 note_id**（逗号分隔由 SDK 负责）。
        assert FakeQianfanAdapter.calls.get("goods_page") == 1
        assert FakeQianfanAdapter.note_id_queries == [["6a7d1e2f000000002c001c33", "6a7d1e2f000000002c001c44"]]

        # ② 商品本体来自千帆补齐的那条（本地映射里本来没有）。
        first = payload["items"][0]
        assert first["goods"]["goods_id"] == "ITEM-A"
        assert first["goods"]["title"] == "挂载商品甲"
        source = payload["goods_source"]
        assert source["on_demand"]["requested"] == 2 and source["on_demand"]["stored"] == 1
        assert source["synced_notes"] == 1, "补齐的行必须落库"
        # ③ 分页分母不能被「过滤后的 total」写坏（带 note_ids 时 total 是命中条数）。
        assert source["shop_total"] == 2, "shop_total 只由无过滤的翻页同步更新"
    finally:
        _teardown()


def test_second_browse_does_not_requery_notes_already_backfilled(tmp_path):
    """已经补齐过的笔记**不会重复查**：第二次只查「仍未命中」的那条。

    语义要点：本页第二条笔记在千帆的商品笔记列表里**不存在**（它没挂商品），
    所以它会被再查一次 —— 这是「确认没商品」的唯一办法（平台不返回「无商品」的负记录）。
    但**已补齐的第一条绝不再查**，所以每页加载最多 1 次、且请求里的 ID 会逐次减少。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        _bind_shop(headers, ids["creator"])
        client.post("/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers)
        FakeQianfanAdapter.calls = {}
        FakeQianfanAdapter.note_id_queries = []

        payload = client.post(
            "/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers
        ).json()
        assert FakeQianfanAdapter.calls.get("goods_page") == 1, "每页仍至多 1 次"
        assert FakeQianfanAdapter.note_id_queries == [["6a7d1e2f000000002c001c44"]], (
            "已补齐的第一条不该再出现在请求里"
        )
        assert payload["items"][0]["goods"]["goods_id"] == "ITEM-A", "商品来自本地映射"
    finally:
        _teardown()


def test_on_demand_failure_never_breaks_my_notes(tmp_path):
    """按需补齐失败（千帆挂了）：页面照常 200，只是这几条暂时没有商品。"""
    headers, _, ids = _setup(tmp_path)
    try:
        _bind_shop(headers, ids["creator"])
        db = _session()
        try:
            for row in db.scalars(select(ShopNoteGoods)).all():
                db.delete(row)
            db.commit()
        finally:
            db.close()
        FakeQianfanAdapter.mode = "goods_fail"

        response = client.post(
            "/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["items"][0]["goods"] is None, "拿不到就留空，不造假"
        assert payload["goods_source"]["on_demand"]["error"], "原因要能回传给页面"
        assert len(payload["items"]) >= 2, "笔记本身照常渲染"
    finally:
        _teardown()


def test_on_demand_credential_failure_marks_binding_invalid(tmp_path):
    """按需补齐遇到 `-13000`：绑定标 `invalid` + 可读原因，**已有映射不清**。"""
    headers, _, ids = _setup(tmp_path)
    try:
        _bind_shop(headers, ids["creator"])
        FakeQianfanAdapter.mode = "expired"
        payload = client.post(
            "/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers
        ).json()
        source = payload["goods_source"]
        assert source["status"] == "invalid"
        assert "-13000" in source["status_message"] or "登录凭证" in source["status_message"]
        assert source["synced_notes"] == 1, "失效不清已同步的映射"
    finally:
        _teardown()


def test_on_demand_never_queries_more_than_the_page(tmp_path):
    """有界：一次补齐只带**本页**的 note_id（且不超过 50 个）。"""
    headers, _, ids = _setup(tmp_path)
    try:
        _bind_shop(headers, ids["creator"])
        db = _session()
        try:
            for row in db.scalars(select(ShopNoteGoods)).all():
                db.delete(row)
            db.commit()
        finally:
            db.close()
        FakeQianfanAdapter.note_id_queries = []
        payload = client.post(
            "/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers
        ).json()
        assert len(FakeQianfanAdapter.note_id_queries) == 1
        queried = FakeQianfanAdapter.note_id_queries[0]
        page_ids = [item["note_id"] for item in payload["items"]]
        assert queried == page_ids, "只查这一页的笔记，不多查"
        assert len(queried) <= 50
    finally:
        _teardown()


def test_creator_notes_carry_qianfan_metrics(tmp_path):
    """「创作者账户数据全部接入千帆」：千帆为每条笔记给出的指标必须全部到达前端。

    只补商品是不够的 —— 千帆的商品笔记列表还带阅读数 / 近 30 日阅读 /
    支付金额 / 支付订单数 / 发布状态，这些是创作者后台拿不到或口径不同的数据。
    缺值一律 `None`（不是 0），由渲染层决定不显示。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        _bind_shop(headers, ids["creator"])
        payload = client.post(
            "/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers
        ).json()
        shop = payload["items"][0]["shop"]
        assert shop["note_id"] == "6a7d1e2f000000002c001c33"
        assert shop["browse_num"] == 286
        assert shop["browse_num_30d"] == 26
        assert shop["likes"] == 12 and shop["collects"] == 3
        assert shop["comments"] == 1 and shop["shares"] == 0, "真 0 必须保留成 0"
        assert shop["pay_gmv"] == 0 and shop["pay_order_cnt"] == 0
        assert shop["note_flag"] == "goods_note" and shop["status"] == 0
        assert shop["publish_time"] == 1786584623000
        assert shop["cover"].endswith("6a7d1e2f000000002c001c33.jpg")
        # 千帆没有这条笔记时不得凭空造一个 shop 块
        assert payload["items"][1].get("shop") in (None, {})
    finally:
        _teardown()


def test_notes_without_binding_report_source_and_no_goods(tmp_path):
    """未绑定时：商品为空，但 `goods_source` 必须把「为什么空」讲清楚。"""
    headers, _, ids = _setup(tmp_path)
    try:
        payload = client.post(
            "/api/xhs/pc/users/notes", json={"account_id": ids["creator"]}, headers=headers
        ).json()
        # 第一条没有平台商品字段、也没有千帆映射 → None；第二条是平台自己给的商品字段。
        assert payload["items"][0]["goods"] is None
        assert payload["items"][1]["goods"]["goods_id"] == "G-1"
        assert payload["items"][0].get("shop") in (None, {})
        source = payload["goods_source"]
        assert source["bound"] is False and source["source"] == "ark"
        assert source["synced_notes"] == 0
    finally:
        _teardown()


def test_pc_link_notes_also_get_goods_from_the_same_mapping(tmp_path):
    """有网页会话的账号同样要吃这份映射：商品笔记不区分由哪条链路读。"""
    headers, _, ids = _setup(tmp_path)
    try:
        _bind_shop(headers, ids["pc"], seed=False)
        FakeQianfanAdapter.pages = {
            1: [_qianfan_entry("N1", "ITEM-PC", "网页链路的商品")]
        }
        # v1.32：`/sync-goods` 端点已移除（设计 13 §30.2）⇒ 直写存储层播种
        # （`store_entry` 是按需补齐 / 笔记同步共用的唯一存储实现）。
        db = _session()
        try:
            account = db.get(PlatformAccount, ids["pc"])
            rows = existing_rows(db, ids["pc"])
            for entry in FakeQianfanAdapter.pages[1]:
                store_entry(db, account.user_id, ids["pc"], rows, entry)
            db.commit()
        finally:
            db.close()
        payload = client.post(
            "/api/xhs/pc/users/notes", json={"account_id": ids["pc"]}, headers=headers
        ).json()
        assert payload["items"][0]["note_id"] == "N1"
        assert payload["items"][0]["goods"]["title"] == "网页链路的商品"
    finally:
        _teardown()


def test_sync_goods_endpoint_is_gone(tmp_path):
    """`POST /xhs/qianfan/sync-goods` 已移除（v1.32，设计 13 §30.2）⇒ 404。

    挂链映射改由**笔记同步 ark 源 + 我的笔记页按需补齐**维护（只增不删）；
    端点留着而前端不再调用 = 一条没人走的暗路径，删掉才是干净移除。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        _bind_shop(headers, ids["creator"], seed=False)
        response = client.post(
            "/api/xhs/qianfan/sync-goods",
            json={"platform_account_id": ids["creator"], "max_pages": 1, "page_size": 50},
            headers=headers,
        )
        assert response.status_code == 404, "端点必须真的不存在（不是 200 空转）"
    finally:
        _teardown()


def test_retired_binding_endpoints_return_410(tmp_path):
    """**已退场端点**（ADR-021 / 设计 18）：`bind` 与 `unbind` 都回 `410`，且说明补救路径。

    这两条路径**刻意保留**（不是 `404`）：删掉的话老前端只会拿到「找不到」，
    要排查很久；显式 `410` + 结构化 `code` 让调用方一眼知道「这个动作已经没有了、
    该去账号矩阵导入千帆登录态」。`unbind` 同理 —— 解绑动作随店铺关系一并消失。

    被取代的旧用例保留说明：
    * `test_bind_requires_owned_account_and_hides_sensitive_fields` —— 断言「绑定要过归属
      校验 + 响应字段白名单」。归属校验本身仍由 `_owned_account` 统一承担（其余端点照测），
      但**绑定这个动作没有了**，故只回 `410`，白名单断言已无对象。
    * `test_unbind_removes_binding_and_mapping` —— 断言「解绑连本地映射一起清」。
      映射现在的归属是账号（`platform_account_id`），清理由**删账号**触发，走账号删除链路，
      不再是「解绑」这个独立动作。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        for path in ("/api/xhs/qianfan/bind", "/api/xhs/qianfan/unbind"):
            response = client.post(
                path,
                json={"platform_account_id": ids["creator"]},
                headers=headers,
            )
            assert response.status_code == 410, f"{path} 必须回 410（不是 404/200）"
            detail = response.json()["detail"]
            assert detail["code"] == "QIANFAN_BINDING_REMOVED", detail
            assert "ADR-021" in detail["message"], detail
    finally:
        _teardown()


def test_account_ownership_check_does_not_consult_sub_type(tmp_path):
    """架构守卫：归属校验里不得再出现 sub_type 判定。

    v1.0 的缺陷正是把 `sub_type` 当能力用（pc.py:539）。这条用 AST 直接检查源码，
    使「改回硬编码」在任何一次重构中都会立刻变红。
    """
    import ast

    from backend.app.api.platforms.xhs import pc as pc_module

    with open(pc_module.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    node = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_account_cookies"
        ),
        None,
    )
    assert node is not None, "未找到 _account_cookies；若已重命名，请同步更新本守卫"

    referenced = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
    referenced |= {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    assert "sub_type" not in referenced, "归属校验不得读取 sub_type（设计文档 §17.1）"


def test_self_notes_requires_stored_cookies(tmp_path):
    """账号存在但从未登录过 → 400，且给的是**结构化可读原因**（v1.27 契约变更）。

    旧断言钉的是裸英文串 `"Account has no cookies"` —— 那句话是给「一条凭据都没存」
    用的**内部错误码**，却被直接甩到界面上（用户 2026-09-21 实测报出）。
    现在统一成 `WEB_SESSION_REQUIRED` + 中文原因：机器可读（前端按 `code` 安静降级）
    且人可读（讲清补救办法）。见设计文档 §17.7。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post("/api/xhs/pc/users/notes", json={"account_id": ids["no_cookie"]}, headers=headers)
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert isinstance(detail, dict), f"应为结构化原因，实际：{detail!r}"
        assert detail["code"] == "WEB_SESSION_REQUIRED"
        assert detail["message"], "必须带可读原因"
        assert "Account has no cookies" not in str(detail)
    finally:
        _teardown()


def test_web_session_reason_does_not_repeat_a_disproven_claim(tmp_path):
    """「借不到网页会话」的原因里不得再写「创作者平台不提供这三类数据」（v1.28 / DEF-058）。

    那句话在 2026-09-21 的真机抓包（生产适配器、只读、三轮）里被**推翻**：创作者
    账号持 www 会话时，`sns/web/v1/note/like/page` / `v2/note/collect/page` /
    `v1/you/*` **全部可达**。障碍只是**凭据**，不是平台能力。

    为什么必须守：用户维度借不到会话时，这句话会**原样推到界面上**。把一个
    「凭据缺失」说成「平台没有这个能力」，用户就**永远不会去重登** —— 错的归因
    会让人放弃唯一有效的动作。原因必须同时是**真的**和**可执行的**。

    ⚠️ v1.40（2026-09-24 用户口径「错误内容太长了，只需提示登录创作者平台或
    pc 端平台」）起，文案改为「…请登录创作者平台或 PC 端平台后重新导入 Cookie」
    —— 这里的「创作者平台」是**登录入口指引**（与 `notes_reason` 的「请在网页版
    或创作者平台登录同一个账号后重试」同一口径），**不是**能力归因。所以判据
    从「禁平台名」收紧为「禁归因句式」：「不提供」/「不返回」这类把凭据缺失说成
    平台没有能力的说法才是回归信号。被推翻的原文句式另有全仓 AST 扫描守着
    （`test_login_state.py::test_disproven_phrase_is_nowhere_in_the_backend`）。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post(
            "/api/xhs/pc/users/liked", json={"account_id": ids["creator"]}, headers=headers
        )
        assert response.status_code == 400
        message = response.json()["detail"]["message"]
        assert "不提供" not in message, "该归因已被真机抓包推翻，不得留在原因里"
        assert "不返回" not in message, "原因应讲凭据，而不是把锅推给平台能力"
        # 可执行：给出补救动作（去哪里登录 + 重新导入），且不塞做不到的建议。
        assert "登录" in message and "重新导入" in message
        assert "稍后" not in message, "等一会儿不会变好，别给做不到的建议"
    finally:
        _teardown()


def test_web_session_reason_is_single_well_formed_and_actionable():
    """拿不到网页会话的原因**只有一句**（没有第二个变体），且必须可执行。

    两个维度（用户维度的 liked/collected/interactions、笔记维度的 detail/comments）
    现在的判据与动作是同一件事 —— **给这个账号补登网页版** —— 所以原因也只留一份。
    两处各写一版就是「一处改、一处漏」的温床（DEF-058 实证：同一个问题，一处说的
    是对的、另一处说的是已被真机抓包推翻的归因）。
    """
    detail = _web_session_required_detail()
    assert detail["code"] == "WEB_SESSION_REQUIRED"
    # 必然性是**结构性**的：函数一个参数都不接受，所以不存在第二个变体。
    assert not inspect.signature(_web_session_required_detail).parameters
    # 可执行：说清缺什么（网页会话）+ 去哪补登（创作者平台 / PC 端平台）+ 做什么
    # （重新导入 Cookie）；不塞做不到的建议（v1.40 用户口径：只留登录指引）。
    assert "网页会话" in detail["message"]
    assert "登录" in detail["message"] and "重新导入" in detail["message"]
    assert "创作者平台" in detail["message"] and "PC" in detail["message"]
    assert "稍后" not in detail["message"], "等一会儿不会变好，别给做不到的建议"
    # 该归因已被真机抓包推翻（创作者账号持 www 会话时三类端点全部可达）。
    assert "不提供" not in detail["message"]


# ---------------------------------------------------------------------------
# 正常路径与归一化（设计文档 §4.6、§6）
# ---------------------------------------------------------------------------


def test_self_notes_normalizes_items_and_pagination(tmp_path):
    """列表项必须带上四项互动数、图文素材、原链接与商品。"""
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post("/api/xhs/pc/users/notes", json={"account_id": ids["pc"]}, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["cursor"] == "CUR-2"
        assert body["has_more"] is True
        assert body["total"] == 1
        assert body["user_id"] == "SELF-1"
        assert body["token_source"] == "auto"
        assert body["identity"]["nickname"] == "我的号"

        note = body["items"][0]
        assert note["title"] == "标题一"
        # 互动四项，其中 "1.2万" 必须被解析成 12000 而不是 1 或 0
        assert note["likes"] == 12000
        assert note["collects"] == 34
        assert note["comments"] == 5
        assert note["shares"] == 2
        assert note["image_urls"] == ["https://cdn.example/1.jpg", "https://cdn.example/2.jpg"]
        # 原链接应带上 xsec_token，否则跳出后打不开
        assert "xsec_token=TK-1" in note["note_url"]
        # 挂载商品
        assert note["goods"]["goods_id"] == "G9"
        assert note["goods"]["title"] == "商品九"
        assert note["goods"]["price"] == "199"
    finally:
        _teardown()


def test_self_profile_returns_identity(tmp_path):
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post("/api/xhs/pc/users/me", json={"account_id": ids["pc"]}, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["user_id"] == "SELF-1"
        assert body["nickname"] == "我的号"
        assert body["red_id"] == "red-001"
    finally:
        _teardown()


def test_self_profile_unreadable_identity_is_502(tmp_path):
    """平台成功但拿不到 user_id —— 必须报错而不是返回空身份。"""
    headers, _, ids = _setup(tmp_path, mode="bad_id")
    try:
        response = client.post("/api/xhs/pc/users/me", json={"account_id": ids["pc"]}, headers=headers)
        assert response.status_code == 502
        assert "user_id" in response.json()["detail"]
    finally:
        _teardown()


def test_self_interactions_normalizes_message_list(tmp_path):
    """you/* 的 message_list 必须被正确读出（不能只认 notes/items）。"""
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post(
            "/api/xhs/pc/users/interactions",
            json={"account_id": ids["pc"], "kind": "like_collect"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["kind"] == "like_collect"
        assert FakeSelfContentAdapter.calls["interaction_kind"] == "like_collect"
        item = body["items"][0]
        assert item["user_name"] == "路人甲"
        assert item["content"] == "赞了你的笔记"
        assert item["note_url"].endswith("/N9")
    finally:
        _teardown()


def test_self_interactions_rejects_unknown_kind(tmp_path):
    """kind 由 Literal 约束，非法值在进业务逻辑之前就被 422 拦下。"""
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post(
            "/api/xhs/pc/users/interactions",
            json={"account_id": ids["pc"], "kind": "not_a_kind"},
            headers=headers,
        )
        assert response.status_code == 422
        assert "interaction_kind" not in FakeSelfContentAdapter.calls
    finally:
        _teardown()


# ---------------------------------------------------------------------------
# 降级路径（设计文档 §7）
# ---------------------------------------------------------------------------


def test_self_notes_returns_structured_error_when_token_missing(tmp_path):
    """无令牌且平台拒绝 → 400 且带 code，供前端弹出「粘贴主页 URL」引导。

    这是 L3 降级：错误必须**可判别**，否则前端只能靠中文文案猜。
    """
    headers, _, ids = _setup(tmp_path, mode="no_token")
    try:
        response = client.post("/api/xhs/pc/users/notes", json={"account_id": ids["pc"]}, headers=headers)
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["code"] == "XSEC_TOKEN_REQUIRED"
        assert detail["message"]
        # 平台原始原因必须保留，否则误判时无从排查
        assert detail["platform_message"] == "平台拒绝了该请求"
    finally:
        _teardown()


def test_self_notes_uses_pasted_profile_url_and_skips_identity_call(tmp_path):
    """L1：粘贴的主页 URL 是权威来源，且不再多打一次身份请求。"""
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post(
            "/api/xhs/pc/users/notes",
            json={
                "account_id": ids["pc"],
                "profile_url": "https://www.xiaohongshu.com/user/profile/URL-USER?xsec_token=URL-TOK&xsec_source=pc_user",
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["token_source"] == "url"
        user_id, _cursor, token, source = FakeSelfContentAdapter.calls["posted_args"]
        assert user_id == "URL-USER"
        assert token == "URL-TOK"
        assert source == "pc_user"
        assert "get_self_profile" not in FakeSelfContentAdapter.calls
    finally:
        _teardown()


def test_self_notes_platform_failure_with_token_is_502(tmp_path):
    """已有令牌仍失败 → 502 并透传平台原因（不再误导为令牌问题）。"""
    headers, _, ids = _setup(tmp_path, mode="fail")
    try:
        response = client.post(
            "/api/xhs/pc/users/notes",
            json={"account_id": ids["pc"], "xsec_token": "HAS-TOKEN"},
            headers=headers,
        )
        assert response.status_code == 502
        assert response.json()["detail"] == "平台拒绝了该请求"
    finally:
        _teardown()


def test_self_notes_identity_failure_is_502_with_platform_message(tmp_path):
    """身份调用就失败（Cookie 失效）→ 502，且给出平台原文。"""
    headers, _, ids = _setup(tmp_path, mode="profile_fail")
    try:
        response = client.post("/api/xhs/pc/users/notes", json={"account_id": ids["pc"]}, headers=headers)
        assert response.status_code == 502
        assert response.json()["detail"] == "登录态已失效，请重新登录"
    finally:
        _teardown()


# ---------------------------------------------------------------------------
# mock 消除（需求 §八 验收指标）
# ---------------------------------------------------------------------------


def test_users_notes_no_longer_returns_mock_payload(tmp_path):
    """`/users/notes` 原是 sample_notes() mock，现必须返回真实归一化结构。

    依据：`04_测试文档/05_Mock清单与接线覆盖度.md` 的 mock 清零目标。
    """
    headers, _, ids = _setup(tmp_path)
    try:
        response = client.post("/api/xhs/pc/users/notes", json={"account_id": ids["pc"]}, headers=headers)
        assert response.status_code == 200
        body = response.json()
        # 旧 mock 的特征字段，必须已消失
        assert "total" in body and "items" in body
        assert "cursor" in body and "has_more" in body and "token_source" in body
        # 且必须真的调用了平台
        assert FakeSelfContentAdapter.calls.get("posted") == 1
    finally:
        _teardown()


# ---------------------------------------------------------------------------
# 单元：探测与降级（设计文档 §3.2、§6.2、§6.3）
# ---------------------------------------------------------------------------


def test_extract_list_probes_the_three_known_shapes():
    assert len(_extract_list({"notes": [{"id": "a"}]}, ("notes", "items"))) == 1
    assert len(_extract_list({"items": [{"id": "a"}]}, ("notes", "items"))) == 1
    assert len(_extract_list({"message_list": [{"id": "a"}]}, ("message_list",))) == 1
    assert len(_extract_list([{"id": "a"}], ("notes",))) == 1
    # 未知结构返回空列表而不是抛异常
    assert _extract_list({"unexpected": [1, 2]}, ("notes",)) == []
    assert _extract_list(None, ("notes",)) == []


def test_items_from_payload_prefers_data_envelope():
    payload = {"data": {"notes": [{"id": "a"}]}, "notes": [{"id": "b"}, {"id": "c"}]}
    assert _items_from_payload(payload, ("notes",)) == [{"id": "a"}]
    # 无 data 信封时退回顶层
    assert len(_items_from_payload({"notes": [{"id": "b"}]}, ("notes",))) == 1


def test_pagination_from_payload_reads_cursor_and_has_more():
    assert _pagination_from_payload({"data": {"cursor": "c1", "has_more": True}}) == ("c1", True)
    assert _pagination_from_payload({"data": {"has_more": False}}) == ("", False)
    assert _pagination_from_payload({}) == ("", False)
    assert _pagination_from_payload(None) == ("", False)


def test_parse_profile_url_handles_url_id_and_empty():
    user_id, token, source = _parse_profile_url(
        "https://www.xiaohongshu.com/user/profile/ABC?xsec_token=T&xsec_source=pc_user"
    )
    assert (user_id, token, source) == ("ABC", "T", "pc_user")
    # 无 token 的 URL 仍然可用（只是拿不到令牌）
    assert _parse_profile_url("https://www.xiaohongshu.com/user/profile/ABC") == ("ABC", "", "")
    # 裸 id
    assert _parse_profile_url("ABC") == ("ABC", "", "")
    assert _parse_profile_url("") == ("", "", "")
    assert _parse_profile_url("   ") == ("", "", "")


def test_extract_goods_returns_none_instead_of_fabricating():
    """商品解析不到时必须返回 None —— 这是「0 处假数据」验收指标的基础。"""
    assert _extract_goods({}) is None
    assert _extract_goods({"goods_info": {}}) is None
    # 容器在但没有任何可识别字段 -> 不是商品对象
    assert _extract_goods({"goods": {"unrelated": "x"}}) is None
    # 列表形式的容器取第一个
    assert _extract_goods({"goods_list": [{"item_id": "G1"}]})["goods_id"] == "G1"


def test_extract_goods_partial_fields_stay_empty_not_zero():
    goods = _extract_goods({"product_info": {"title": "只有标题"}})
    assert goods["title"] == "只有标题"
    assert goods["price"] == ""
    assert goods["image"] == ""
    assert goods["url"] == ""
    assert goods["shop_name"] == ""


def test_normalize_identity_probes_nested_shapes():
    flat = _normalize_identity({"user_id": "U1", "nickname": "平铺"})
    assert flat["user_id"] == "U1" and flat["nickname"] == "平铺"

    nested = _normalize_identity({"basic_info": {"user_id": "U2", "nickname": "嵌套", "image": "https://cdn/a.jpg"}})
    assert nested["user_id"] == "U2"
    assert nested["nickname"] == "嵌套"
    assert nested["avatar"] == "https://cdn/a.jpg"

    assert _normalize_identity(None)["user_id"] == ""


def test_new_self_content_adapter_methods_avoid_full_list_sdk_helpers():
    """本次新增的自有内容方法禁止调用全量循环方法（设计文档 §8.2 / 需求 C5）。

    一次全量循环意味着：一个有几百篇笔记的账号会在**单次请求**里打出几十个
    平台调用，既触发风控也可能超出前端 120s 超时。所以单页是硬约束。

    断言范围刻意限定在**本次新增的五个方法**上。适配器里既有的
    ``get_user_notes`` 是全量语义且仍被监控巡检与数据抓取使用
    （`services/monitoring_crawl_service.py:90`、`api/platforms/xhs/crawl.py:317`），
    那属于既有链路，已单独登记为缺陷 DEF-033，不在本需求内改造。
    """
    import ast
    import inspect

    from backend.app.adapters.xhs import pc_api_adapter

    new_methods = {
        "get_self_profile",
        "get_user_posted_page",
        "get_user_liked_page",
        "get_user_collected_page",
        "get_interaction_messages",
    }
    forbidden = {
        "get_user_all_notes",
        "get_user_all_like_note_info",
        "get_user_all_collect_note_info",
        "get_all_likesAndcollects",
        "get_all_metions",
        "get_all_new_connections",
    }

    tree = ast.parse(inspect.getsource(pc_api_adapter))
    offenders: dict[str, set[str]] = {}
    for cls in [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]:
        for fn in [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in new_methods]:
            called = {
                node.func.attr
                for node in ast.walk(fn)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }
            hits = forbidden & called
            if hits:
                offenders[fn.name] = hits

    assert not offenders, f"新增自有内容方法不得调用全量循环方法：{offenders}"


def test_user_posted_page_requests_a_single_page():
    """单页语义：适配器不得自行循环消费 cursor。"""
    import ast
    import inspect

    from backend.app.adapters.xhs import pc_api_adapter

    tree = ast.parse(inspect.getsource(pc_api_adapter))
    target = None
    for cls in [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]:
        for fn in [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "get_user_posted_page"]:
            target = fn
    assert target is not None, "get_user_posted_page 必须存在"
    # 方法体内出现 while 循环即意味着它在翻页
    assert not any(isinstance(node, ast.While) for node in ast.walk(target)), (
        "get_user_posted_page 必须是单页调用，不得内含翻页循环"
    )


# ---------------------------------------------------------------------------
# 自有内容关键词搜索（R8 / 设计文档 §22）
#
# 这里的夹具刻意做成**两页 + 可切换失败**，因为要钉住的是三件容易写错的事：
#   ① 三个分类都要被扫（不是只扫当前 Tab）；
#   ② 扫描页数有**上界**（不是 `while has_more` 全量循环）；
#   ③ 某分类失败不能把整单打挂（部分结果 + 每类自己的错误）。
# ---------------------------------------------------------------------------


class FakeSearchAdapter:
    """两页夹具：第 1 页 `has_more=true`，第 2 页 `has_more=false`。

    `mode`：
      ok            —— 三分类都能扫
      liked_fail    —— 「我赞过的」平台拒绝，另两类照常
      always_more   —— 永远 has_more=true（用来证明扫描页数被预算夹住）
    """

    mode = "ok"
    calls: dict = {}

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def get_self_profile(self):
        return True, "", {"data": {"user_id": "SELF-1", "nickname": "我的号"}}

    def _page(self, scope: str, cursor: str):
        FakeSearchAdapter.calls.setdefault(scope, []).append(cursor)
        if scope == "liked" and FakeSearchAdapter.mode == "liked_fail":
            return False, "平台拒绝了该请求", None

        page_index = 1 if not cursor else 2
        titles = {
            "notes": {1: "地垫测评 第1页", 2: "地垫测评 第2页"},
            "liked": {1: "我赞过的地垫", 2: "与关键词无关的一条"},
            "collected": {1: "我收藏的地垫", 2: "与关键词无关的一条"},
        }
        has_more = FakeSearchAdapter.mode == "always_more" or page_index == 1
        return True, "", {
            "data": {
                "notes": [
                    {
                        "note_id": f"{scope}-{page_index}",
                        "display_title": titles[scope][page_index],
                        "type": "normal",
                        "time": 1700000000,
                        "xsec_token": f"TK-{scope}-{page_index}",
                        "interact_info": {"liked_count": "1"},
                    }
                ],
                "cursor": f"P{page_index + 1}",
                "has_more": has_more,
            }
        }

    def get_user_posted_page(self, user_id, cursor="", xsec_token="", xsec_source=""):
        return self._page("notes", cursor)

    def get_user_liked_page(self, user_id, cursor="", xsec_token="", xsec_source=""):
        return self._page("liked", cursor)

    def get_user_collected_page(self, user_id, cursor="", xsec_token="", xsec_source=""):
        return self._page("collected", cursor)


def _setup_search(tmp_path, mode: str = "ok"):
    """owner 有 pc（带 web_session）与 creator（无 web_session）两个账号。"""
    engine = make_engine(tmp_path / 'self-search.db')
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

    FakeSearchAdapter.mode = mode
    FakeSearchAdapter.calls = {}
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: FakeSearchAdapter

    response = client.post("/api/auth/register", json={"username": "searcher", "password": "secret123"})
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    db = SessionLocal()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "searcher"))
        pc = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="pc",
            nickname=GUANYU_NICKNAME, profile_json=GUANYU_PROFILE, status="active"
        )
        creator = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="creator",
            nickname=GUANYU_NICKNAME, profile_json=GUANYU_PROFILE, status="active"
        )
        db.add_all([pc, creator])
        db.commit()
        db.add_all(
            [
                cookie_version(pc.id, COOKIE_PAYLOAD, CHANNEL_WWW),
                # 拆分后（2026-09-21）：创作者 jar（`x-user-id-creator.*`）落 **creator 平台**。
                # 夹具显式声明平台，否则「按平台取令牌」这条契约在测试里等于没测。
                cookie_version(creator.id, CREATOR_COOKIE_PAYLOAD, CHANNEL_CREATOR),
            ]
        )
        db.commit()
        return headers, {"pc": pc.id, "creator": creator.id}
    finally:
        db.close()


def test_self_search_matches_across_all_three_scopes(tmp_path):
    """一次搜索必须同时覆盖「我发布的 / 我赞过的 / 我收藏的」，且每条带来源标签。"""
    headers, ids = _setup_search(tmp_path)
    try:
        response = client.post(
            "/api/xhs/pc/users/search",
            headers=headers,
            json={"account_id": ids["pc"], "keyword": "地垫"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()

        assert {item["scope"] for item in payload["items"]} == {"notes", "liked", "collected"}
        assert payload["total_matched"] == 4
        # 分域求和 == 合计（统计自洽在响应层也成立）
        assert sum(meta["matched"] for meta in payload["scopes"].values()) == payload["total_matched"]
        assert "已到底部" not in response.text
    finally:
        _teardown()


def test_self_search_scans_until_each_scope_is_exhausted(tmp_path):
    """两页夹具：每类都扫到第 2 页（has_more=false）后停止，并标记 exhausted。"""
    headers, ids = _setup_search(tmp_path)
    try:
        payload = client.post(
            "/api/xhs/pc/users/search",
            headers=headers,
            json={"account_id": ids["pc"], "keyword": "地垫"},
        ).json()
        for scope in ("notes", "liked", "collected"):
            assert payload["scopes"][scope]["scanned_pages"] == 2
            assert payload["scopes"][scope]["exhausted"] is True
            assert payload["scopes"][scope]["error"] == ""
        assert payload["has_more"] is False
    finally:
        _teardown()


def test_self_search_respects_page_budget_and_returns_cursors(tmp_path):
    """`pages_per_scope=1` 时每类只扫一页，并回传游标供「继续搜索」。"""
    headers, ids = _setup_search(tmp_path)
    try:
        payload = client.post(
            "/api/xhs/pc/users/search",
            headers=headers,
            json={"account_id": ids["pc"], "keyword": "地垫", "pages_per_scope": 1},
        ).json()
        for scope in ("notes", "liked", "collected"):
            assert payload["scopes"][scope]["scanned_pages"] == 1
            assert payload["scopes"][scope]["exhausted"] is False
            assert payload["cursors"][scope] == "P2"
        assert payload["has_more"] is True
    finally:
        _teardown()


def test_self_search_budget_holds_even_when_platform_never_ends(tmp_path):
    """平台永远说 has_more=true 时，扫描页数仍必须等于预算（**有界**的证明）。"""
    headers, ids = _setup_search(tmp_path, mode="always_more")
    try:
        payload = client.post(
            "/api/xhs/pc/users/search",
            headers=headers,
            json={"account_id": ids["pc"], "keyword": "地垫", "pages_per_scope": 2},
        ).json()
        for scope in ("notes", "liked", "collected"):
            assert payload["scopes"][scope]["scanned_pages"] == 2
        # 每类恰好被调用 2 次：多一次都说明预算没生效
        assert all(len(calls) == 2 for calls in FakeSearchAdapter.calls.values())
    finally:
        _teardown()


def test_self_search_reports_scan_metadata_when_nothing_matches(tmp_path):
    """没搜到也要给出「扫了多少页」的依据，不能让用户以为账号里没有这条笔记。"""
    headers, ids = _setup_search(tmp_path)
    try:
        payload = client.post(
            "/api/xhs/pc/users/search",
            headers=headers,
            json={"account_id": ids["pc"], "keyword": "绝不存在的关键词"},
        ).json()
        assert payload["items"] == []
        assert payload["total_matched"] == 0
        assert all(meta["scanned_pages"] == 2 for meta in payload["scopes"].values())
    finally:
        _teardown()


def test_self_search_partial_failure_keeps_other_scopes(tmp_path):
    """某分类失败只记自己的错误，其余分类的结果照常返回（不整单 500）。"""
    headers, ids = _setup_search(tmp_path, mode="liked_fail")
    try:
        response = client.post(
            "/api/xhs/pc/users/search",
            headers=headers,
            json={"account_id": ids["pc"], "keyword": "地垫"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["scopes"]["liked"]["error"]
        assert payload["scopes"]["liked"]["scanned_pages"] == 0
        assert payload["scopes"]["notes"]["error"] == ""
        assert payload["total_matched"] == 3
    finally:
        _teardown()


def test_self_search_requires_web_session_and_does_not_fake_it(tmp_path):
    """creator-only 账号：400 `WEB_SESSION_REQUIRED`，且**一次平台请求都不发**。"""
    headers, ids = _setup_search(tmp_path)
    try:
        response = client.post(
            "/api/xhs/pc/users/search",
            headers=headers,
            json={"account_id": ids["creator"], "keyword": "地垫"},
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "WEB_SESSION_REQUIRED"
        assert FakeSearchAdapter.calls == {}
    finally:
        _teardown()


def test_self_search_requires_authentication(tmp_path):
    _, ids = _setup_search(tmp_path)
    try:
        response = client.post(
            "/api/xhs/pc/users/search", json={"account_id": ids["pc"], "keyword": "地垫"}
        )
        assert response.status_code == 401
    finally:
        _teardown()


def test_search_keyword_tokens_are_and_semantics():
    """多词关键词按 AND 匹配：少命中一个 token 就不算命中。"""
    from backend.app.api.platforms.xhs.pc import _keyword_tokens, _matches_keyword

    assert _keyword_tokens("  地垫   防滑 ") == ["地垫", "防滑"]
    note = {"title": "地垫防滑测评", "content": ""}
    assert _matches_keyword(note, ["地垫", "防滑"]) is True
    assert _matches_keyword(note, ["地垫", "收纳"]) is False
    # 空 token（纯空白关键词）不命中任何东西，而不是命中全部
    assert _matches_keyword(note, []) is False


def test_keyword_search_uses_single_page_methods_only():
    """架构守卫：搜索路由体内禁止出现全量循环方法（需求 C5）。"""
    import ast
    import inspect

    from backend.app.api.platforms.xhs import pc as pc_module

    tree = ast.parse(inspect.getsource(pc_module))
    target = None
    for fn in [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]:
        if fn.name == "user_search":
            target = fn
    assert target is not None, "user_search 必须存在于 pc 模块"

    called = {
        node.func.attr
        for node in ast.walk(target)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    forbidden = {
        "get_user_all_notes",
        "get_user_all_like_note_info",
        "get_user_all_collect_note_info",
        "get_all_likesAndcollects",
        "get_all_metions",
        "get_all_new_connections",
    }
    assert not (called & forbidden), f"搜索不得调用全量循环方法：{called & forbidden}"


