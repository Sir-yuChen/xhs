"""账号笔记同步（T-803）的后端契约测试。

盯的是这条链路的**纪律**而非吞吐：

1. **有界**：翻页数由 `max_pages` 硬顶，`has_more=false` / 游标 `-1` /
   已存够 `total` 三种提前结束都要真的生效（C5，禁全量循环）；
2. **限流停本次**：`CrawlRateLimiter` 拒绝时**不抛异常**，已同步的保留，
   原因写任务行；
3. **令牌不静默**：PC 链路没有 `xsec_token` 一律 `failed` +
   `XSEC_TOKEN_REQUIRED`，**不能**出现「同步成功但 0 条」；
4. **防重（N3）**：同一账号第二次触发返回**同一个 task_id**，不新建任务行；
5. **R3**：Creator 每页笔记落库后，把**本页** note_id 交给千帆补挂载商品
   （每页 ≤1 次请求）；
6. **缺值不造假（硬约束⑤）**：平台没给总数 → `progress.total` 是 `null`；
   拿得到才写 `profile_json.notes_count`；
7. **鉴权**：越权 404；参数越界 422。
"""
from __future__ import annotations

import ast
import inspect
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.adapters.xhs import qianfan_api_adapter
from backend.app.api.platforms.xhs import pc
from backend.app.core.database import Base, get_db
from backend.app.main import app
from backend.app.models import Note, NoteAsset, NoteDatacenterStat, PlatformAccount, Task, User
from backend.app.models.shop import ShopNoteGoods
from tests.backend.cookie_fixtures import bind_shop_account
from tests.backend.conftest import patch_service_sessions
from backend.app.services import note_sync_service as nss
from backend.app.services import account_task_service as ats
from backend.app.services import sync_task_service as sts
from backend.app.services import task_cancel


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration

client = TestClient(app)

# 真实登录的 www jar 一定同时带 `web_session` 与 `id_token`（DEF-051）：
# 只有 web_session 的是**游客会话**，平台对未登录访客也会种。
PC_COOKIES = "web_session=abc123; id_token=tok123; a1=xyz"
# 真实形态（2026-09-21 拆分）：带 creator 域令牌 —— 少了它这份 jar 会被
# 分类器判成 www（默认兜底），「按平台取令牌」的契约就测不到了。
CREATOR_COOKIES = (
    "galaxy_creator_session_id=abc123; "
    "access-token-creator.xiaohongshu.com=ctok123; "
    "customer-sso-sid=sso123; a1=xyz"
)

#: 测试用的 Cookie 由它决定（`_account_cookies` 被替换成读这里）。
ENV: dict = {"cookies": CREATOR_COOKIES, "channel": "creator"}
#: **额外通道**的 Cookie：夹具默认只给 `ENV["channel"]` 一个通道；需要 www 详情
#: 会话的用例（§19.14 页内联补齐）往里加 `{"www": PC_COOKIES}`。
EXTRA_CHANNEL_COOKIES: dict = {}


class AllowLimiter:
    def allow(self, account_id: int) -> bool:  # noqa: ARG002
        return True

    def wait_allow(self, account_id: int, **kwargs) -> bool:  # noqa: ARG002
        """全量翻页用的是**等待制**（设计 13 §21.3）：测试里立即放行、不真等待。"""
        return True


class DenyLimiter:
    """额度**永远**没有：`wait_allow` 立即超时（不真等待）= 模拟「等到超时仍无额度」。"""

    def allow(self, account_id: int) -> bool:  # noqa: ARG002
        return False

    def wait_allow(self, account_id: int, **kwargs) -> bool:  # noqa: ARG002
        return False


LIMITER: dict = {"impl": AllowLimiter()}


# --------------------------------------------------------------------------- #
# 假适配器（记录调用，用来断言「打了多少次平台」）
# --------------------------------------------------------------------------- #
class FakePcAdapter:
    has_more_times = 0
    total = None
    calls: list = []
    profile_calls = 0
    #: §19.14 页内联详情补齐：记录打过的详情 URL；默认按 URL 里的 note_id 回一条
    #: 带正文 + 一张图的响应（形状抄 `_normalize_detail_payload` 的输入）。
    detail_calls: list = []
    #: 详情恒失败（测「失败聚合成一行、列表数据照常落库、标记不写」）。
    detail_fail = False

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def get_self_profile(self):
        type(self).profile_calls += 1
        return True, "", {"data": {"user_id": "SELF-1", "nickname": "我的号"}}

    def get_user_posted_page(self, user_id, cursor="", xsec_token="", xsec_source=""):
        type(self).calls.append({"cursor": cursor, "token": xsec_token})
        index = len(type(self).calls)
        data = {
            "notes": [_pc_note(f"PC-N{index}")],
            "cursor": f"CUR-{index + 1}",
            "has_more": index <= type(self).has_more_times,
        }
        if type(self).total is not None:
            data["total"] = type(self).total
        return True, "", {"data": data}

    def get_note_info(self, url: str):
        type(self).detail_calls.append(url)
        if type(self).detail_fail:
            return False, "平台暂时无法浏览", {}
        note_id = _note_id_of_detail_url(url)
        card = {
            "note_id": note_id,
            "display_title": f"详情标题 {note_id}",
            "desc": f"详情正文 {note_id}",
            "type": "normal",
            "image_list": [{"url_default": f"https://cdn.example/{note_id}-detail.jpg"}],
        }
        return True, "", {"data": {"items": [{"id": note_id, "note_card": card}]}}


def _note_id_of_detail_url(url: str) -> str:
    """从详情 URL 里取 note_id（`/explore/<id>?xsec_token=…`）。"""
    marker = "/explore/"
    if marker not in str(url):
        return ""
    return str(url).split(marker, 1)[1].split("?", 1)[0].strip()


class FakeCreatorAdapter:
    calls: list = []
    note_ids: list = ["C-N1", "C-N2"]
    notes_count = 1969
    #: 多页形状（§21 全量翻页用例专用）：`{页码: [note_id, ...]}`，页码从 0 起
    #: （与 `_creator_page_index` 同口径）。`None` = 单页（其余用例的默认形状）。
    pages: "dict | None" = None

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def get_posted_notes_page(self, page: int = 0, tab: int = 0):
        type(self).calls.append(page)
        pages = type(self).pages
        if pages is None:
            ids, next_page = type(self).note_ids, -1
        else:
            # §21：`data.page` 是**下一个**游标，`-1` 终止（`_creator_pagination` 口径）。
            ids = pages.get(page, [])
            next_page = page + 1 if (page + 1) in pages else -1
        return True, "", {
            "data": {
                "notes": [_creator_note(note_id) for note_id in ids],
                "page": next_page,
                "tags": [
                    {"name": "所有笔记", "notes_count": type(self).notes_count, "checked": True}
                ],
            }
        }


class FakeQianfanAdapter:
    note_id_queries: list = []
    calls = 0
    #: 商品笔记**列表**接口被调用的次数（`note_ids` 那类查询**不算**）。
    #: §44 用来断言「普通笔记任务一次都没打千帆列表」/「商品笔记任务没打创作者列表」。
    list_pages = 0
    #: §19：商品笔记**列表**（不带 note_ids 的调用）返回的条目。
    #: 形状抄自 2026-09-21 真机抓包的 `goods-note/list` 响应。
    list_entries: list = []

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def get_goods_note_page(self, page_num: int = 1, page_size: int = 20, *, note_ids=None):
        type(self).calls += 1
        if note_ids:
            type(self).note_id_queries.append(list(note_ids))
            entries = [
                {"note_id": note_id, "item_id": f"ITEM-{note_id}", "item_title": "商品",
                 "goods_infos": []}
                for note_id in note_ids
            ]
            return True, "", {"data": {"note_infos": entries, "total": len(entries)}}
        type(self).list_pages += 1
        entries = type(self).list_entries
        return True, "", {
            "data": {
                "note_infos": entries,
                "total": len(entries),
                # 一页装下 => 直接结束（真机是 end_tag=False + total=1960 翻页）。
                "end_tag": True,
            }
        }


def _ark_note(note_id: str, *, title: str = "", xsec_token: str = "ARK-TK") -> dict:
    """一条千帆商品笔记（§19.1.1 实测字段）。"""
    return {
        "note_id": note_id,
        "note_title": title or f"商品笔记 {note_id}",
        "note_image": f"https://ci.example/{note_id}.jpg",
        "note_type": 1,
        "publish_time": 1786584162000,
        "browse_num": 12,
        "like_num": 3,
        "collect_num": 1,
        "comment_num": 0,
        "share_num": 0,
        "item_id": f"ITEM-{note_id}",
        "item_title": "夜市摆摊灯",
        "item_image": "https://qimg.example/item.jpg",
        "variant_desc": "100W暖黄光",
        "xsec_token": xsec_token,
        "note_flag": "goods_note",
        "goods_infos": [
            {"item_id": f"ITEM-{note_id}", "item_title": "夜市摆摊灯", "variant_desc": "100W暖黄光"}
        ],
    }


def _pc_note(note_id: str) -> dict:
    """一张 PC 链路笔记卡（形状抄自真机样本）。"""
    return {
        "id": note_id,
        "xsec_token": "TK",
        "note_card": {
            "note_id": note_id,
            "display_title": f"标题 {note_id}",
            "desc": "正文",
            "type": "normal",
            "time": 1700000000,
            "user": {"user_id": "SELF-1", "nickname": "我的号"},
            "interact_info": {
                "liked_count": 1, "collected_count": 2, "comment_count": 3, "share_count": 4,
            },
            "image_list": [{"url_default": f"https://cdn.example/{note_id}.jpg"}],
        },
    }


def _creator_note(note_id: str) -> dict:
    """一条创作者平台的笔记（扁平指标、`images_list`、无正文字段）。"""
    return {
        "id": note_id,
        "display_title": f"创作 {note_id}",
        "time": "2026-08-13 09:30",
        "likes": 12,
        "collected_count": 3,
        "comments_count": 1,
        "shared_count": 0,
        "view_count": 100,
        "images_list": [{"url": f"https://ci.example/{note_id}.jpg"}],
        "goods_info": {"goods_name": []},
    }


def _reset_fakes() -> None:
    FakePcAdapter.has_more_times = 0
    FakePcAdapter.total = None
    FakePcAdapter.calls = []
    FakePcAdapter.profile_calls = 0
    FakePcAdapter.detail_calls = []
    FakePcAdapter.detail_fail = False
    FakeCreatorAdapter.calls = []
    FakeCreatorAdapter.note_ids = ["C-N1", "C-N2"]
    FakeCreatorAdapter.notes_count = 1969
    FakeCreatorAdapter.pages = None
    FakeQianfanAdapter.note_id_queries = []
    FakeQianfanAdapter.calls = 0
    #: §44：商品笔记**列表**的调用计数必须一起归零 —— 它是「这条链路有没有打千帆」的判据，
    #: 漏归零会让第二个用例读到上一个用例的累计值（断言恒真/恒假，全看用例顺序）。
    FakeQianfanAdapter.list_pages = 0
    FakeQianfanAdapter.list_entries = []
    EXTRA_CHANNEL_COOKIES.clear()
    LIMITER["impl"] = AllowLimiter()


def _setup(tmp_path, monkeypatch, *, sub_type: str = "creator", with_binding: bool = True):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'note-sync.db'}", connect_args={"check_same_thread": False}
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
    monkeypatch.setattr(nss, "SessionLocal", SessionLocal)
    # ⚠️ 端点链路会走到 `scheduled_job_runner` / `sync_task_service` 等模块，它们各自
    # 持有模块级 SessionLocal（`from … import SessionLocal`）—— 只 patch `nss` 的话
    # 这些模块**照样写生产库**（2026-09-27 真实发生过：真实库多出一次僵尸任务
    # + 一条假定时日志）。`patch_service_sessions` 按 AST 扫出的全量清单逐一打桩。
    patch_service_sessions(monkeypatch, SessionLocal)
    # §11.5：取 Cookie 必须带通道。这里刻意做成**通道敏感**的假实现 ——
    # 要的通道不对就返回空串，让「拿错通道的 jar 去打接口」这类回归直接炸，
    # 而不是被一个「反正都能用」的假实现掩盖掉。
    monkeypatch.setattr(
        nss, "_account_cookies",
        lambda db, account_id, *, channel="www": (
            ENV["cookies"] if channel == ENV["channel"] else EXTRA_CHANNEL_COOKIES.get(channel, "")
        ),
    )
    monkeypatch.setattr(nss, "PAGE_GAP_SECONDS", 0.0)
    monkeypatch.setattr(nss, "get_rate_limiter", lambda: LIMITER["impl"])
    monkeypatch.setattr(pc, "get_xhs_pc_api_adapter_factory", lambda: FakePcAdapter)
    monkeypatch.setattr(pc, "get_xhs_creator_api_adapter_factory", lambda: FakeCreatorAdapter)
    monkeypatch.setattr(
        qianfan_api_adapter, "get_qianfan_api_adapter_factory", lambda: FakeQianfanAdapter
    )
    # 素材下载在测试里不联网（`_download_asset` 是函数内 import，改模块属性即可生效）。
    monkeypatch.setattr(
        "backend.app.services.asset_downloader.download_asset_to_local", lambda *a, **k: None
    )

    _reset_fakes()
    ENV["cookies"] = CURRENT[0]

    response = client.post(
        "/api/auth/register", json={"username": "owner", "password": "secret123"}
    )
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    db = SessionLocal()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "owner"))
        account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type=sub_type,
            nickname="我的号", status="active",
        )
        db.add(account)
        db.flush()
        if with_binding:
            #: 2026-09-22：关系/凭据/状态三件事各归其位（`shop_account_bindings` 整表移除）。
            #: ⚠️ `sync_note_ids` 取的是**账号自己的 ark 通道**，所以夹具必须给 ark 令牌 ——
            #: 这正是本轮的口径（不再向绑定行借）。
            bind_shop_account(
                db,
                account,
                cookies_text=CREATOR_COOKIES,
                shop_name="观隅好物的店",
            )
        db.commit()
        account_id = account.id
        user_id_out = user_id
    finally:
        db.close()
    return headers, account_id, user_id_out, SessionLocal


def _teardown() -> None:
    app.dependency_overrides.clear()


#: `_setup` 用它把当前用例的 Cookie 类型传进 `ENV`。
CURRENT: list = [CREATOR_COOKIES]


@pytest.fixture
def creator_env(tmp_path, monkeypatch):
    CURRENT[0] = CREATOR_COOKIES
    ENV["channel"] = "creator"    # creator 链路打创作者平台 → creator 平台（§11.5 / 拆分）
    headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    yield headers, account_id, user_id, SessionLocal
    _teardown()


@pytest.fixture
def pc_env(tmp_path, monkeypatch):
    CURRENT[0] = PC_COOKIES
    ENV["channel"] = "www"        # pc 链路打 www → www 通道（§11.5）
    headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch, sub_type="pc")
    yield headers, account_id, user_id, SessionLocal
    _teardown()


@pytest.fixture
def pc_env_no_binding(tmp_path, monkeypatch):
    """PC 账号、**没有任何可用源**（www 通道取不到 + 不建绑定）。

    用来钉「均不可用」的结构化多原因失败（§19.3.1）。
    """
    CURRENT[0] = PC_COOKIES
    ENV["channel"] = "ark"  # www 通道取不到 ⇒ 没有 www 源
    headers, account_id, user_id, SessionLocal = _setup(
        tmp_path, monkeypatch, sub_type="pc", with_binding=False
    )
    yield headers, account_id, user_id, SessionLocal
    _teardown()


@pytest.fixture
def creator_env_no_ark(tmp_path, monkeypatch):
    """创作者会话可用、但**没有千帆（ark）令牌**（**§44**：商品笔记的硬门禁判据）。

    与 `creator_env` 只差 `with_binding=False`（那条路径同时写店铺关系 + ark 凭据）。
    用来钉「商品笔记缺令牌 ⇒ 结构化 `ARK_SESSION_REQUIRED`，一条平台请求都不发」。
    """
    CURRENT[0] = CREATOR_COOKIES
    ENV["channel"] = "creator"
    headers, account_id, user_id, SessionLocal = _setup(
        tmp_path, monkeypatch, sub_type="creator", with_binding=False
    )
    yield headers, account_id, user_id, SessionLocal
    _teardown()


# --------------------------------------------------------------------------- #
# §44：单源路由（普通笔记 = creator / 商品笔记 = ark）
# --------------------------------------------------------------------------- #

def test_note_sources_only_is_the_single_router(creator_env):
    """`_note_sources(only=…)` 是**唯一的路由开关**（§44）：两个源各有自己的硬门禁。

    未知取值**必须抛 `ValueError`**，不许静默退化成某一种 —— 静默退化会让「商品笔记
    同步」在源名打错时变成跑普通笔记，而且任务行看起来一切正常（最难查的一类错）。
    """
    _, account_id, _, SessionLocal = creator_env
    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, account_id)
        # 缺省 = 普通笔记（creator），保持旧调用方（和端点）零感知。
        assert [s["source"] for s in nss._note_sources(db, account)] == ["creator"]
        assert [
            s["source"] for s in nss._note_sources(db, account, only=nss.SOURCE_CREATOR)
        ] == ["creator"]
        assert [
            s["source"] for s in nss._note_sources(db, account, only=nss.SOURCE_ARK)
        ] == ["ark"]
        with pytest.raises(ValueError):
            nss._note_sources(db, account, only="creator,ark")
    finally:
        db.close()


def test_source_to_task_type_mapping_is_pinned():
    """源 → 任务类型映射钉死（§44）：漂移会让商品笔记写进普通笔记的任务类型。

    两个模块对这组取值各有一份（`sync_task_service.SOURCE_TASK_TYPES` 与
    `note_sync_service` 的源常量），`note_sync_service` 在 import 期有断言兜底；
    这里再把**取值本身**钉一遍（改名 / 加源都必须改这条用例）。
    """
    assert sts.SOURCE_TASK_TYPES == {
        "creator": sts.NOTE_SYNC_TASK_TYPE,
        "ark": sts.GOODS_NOTE_SYNC_TASK_TYPE,
    }
    assert sts.NOTE_SYNC_TASK_TYPE == "xhs_note_sync"
    assert sts.GOODS_NOTE_SYNC_TASK_TYPE == "xhs_goods_note_sync"
    assert sts.task_type_for_source("ark") == sts.GOODS_NOTE_SYNC_TASK_TYPE
    with pytest.raises(ValueError):
        sts.task_type_for_source("both")


# --------------------------------------------------------------------------- #
# §19：三态同步（www / creator / ark 各自独立）
# --------------------------------------------------------------------------- #

def test_missing_creator_session_fails_with_structured_reason(pc_env_no_binding):
    """**创作者平台会话是唯一硬门禁**（§21.4，2026-09-22 用户口径）。

    没有创作者会话 ⇒ 一律 `CREATOR_SESSION_REQUIRED` + `failed`，**且不退化成
    「用 ark 凑合跑一遍」** —— 用户要的是「补齐后再触发」，不是「哪个源能用就用哪个」。
    """
    _, account_id, user_id, _ = pc_env_no_binding

    result = nss.run_note_sync(account_id, user_id=user_id)

    assert result["status"] == sts.STATUS_FAILED
    assert "CREATOR_SESSION_REQUIRED" in result["error"]
    # 一个平台请求都没发（连 ark 都没试）。
    assert FakePcAdapter.calls == []
    assert FakeQianfanAdapter.calls == 0


def test_creator_source_failure_fails_the_task_without_ark_fallback(creator_env, monkeypatch):
    """**普通笔记任务只跑 creator 源**（§44）：它失败 ⇒ 本任务 `failed`，不拿 ark 兜底。

    ⚠️ 这条**取代**了拆分前的 `test_creator_source_failure_keeps_ark_and_reports_reason`
    （原断言「creator 失败但 ark 可用 ⇒ done + 分源明细」）—— §44 删掉了混合双源：
    混在一跑会把「创作者会话失效」伪装成「同步成功，只同步到商品笔记」。
    商品笔记是**另一个任务**（`xhs_goods_note_sync`），自己有自己的门禁与结果行。
    """
    class FailingCreatorAdapter:
        def __init__(self, cookies: str) -> None:
            self.cookies = cookies

        def get_posted_notes_page(self, page: int = 0, tab: int = 0):
            return False, "登录凭证缺失，请重新登录", {"code": -13000, "success": False}

    monkeypatch.setattr(pc, "get_xhs_creator_api_adapter_factory", lambda: FailingCreatorAdapter)
    FakeQianfanAdapter.list_entries = [_ark_note("ARK-N1")]
    _, account_id, user_id, SessionLocal = creator_env

    result = nss.run_note_sync(account_id, user_id=user_id)

    assert result["status"] == sts.STATUS_FAILED, result
    assert "登录凭证缺失" in result["error"]
    # **ark 一次都没打**：普通笔记任务里它不再是兜底源（§44）。
    assert FakeQianfanAdapter.calls == 0
    db = SessionLocal()
    try:
        assert db.scalars(select(Note.note_id)).all() == []
    finally:
        db.close()


def test_without_creator_session_ark_is_not_enough(pc_env, monkeypatch):
    """**只有千帆令牌不够**：同步笔记必须先有创作者平台会话（§21.4）。

    这条钉的是「不退化」—— 旧实现会把 ark 当独立入口跑一遍，任务显示
    「同步成功」，而**一条笔记都没落**，用户以为同步过了。
    """
    FakeQianfanAdapter.list_entries = [_ark_note("ARK-N1")]
    _, account_id, user_id, SessionLocal = pc_env

    result = nss.run_note_sync(account_id, user_id=user_id)

    assert result["status"] == sts.STATUS_FAILED
    assert "CREATOR_SESSION_REQUIRED" in result["error"]
    assert FakeQianfanAdapter.calls == 0  # ark 源一次都没打
    db = SessionLocal()
    try:
        assert db.scalars(select(Note.note_id)).all() == []
    finally:
        db.close()


def test_normal_note_sync_runs_only_the_creator_source(creator_env, monkeypatch):
    """创作者平台 + 千帆**都可用** ⇒ 普通笔记任务仍然**只跑 creator 源**（§44）。

    ⚠️ 这条**取代**了拆分前的 `test_creator_and_ark_sources_run_when_both_usable`
    （原断言 `set(by_source) == {"creator", "ark"}`）。它同时是「混合路径已删」的守卫：
    **把 ark 追加写回 `_note_sources(only="creator")` 本用例必红**。
    """
    FakeQianfanAdapter.list_entries = [_ark_note("ARK-N1")]
    _, account_id, user_id, SessionLocal = creator_env

    result = nss.run_note_sync(account_id, user_id=user_id)

    assert result["status"] == sts.STATUS_DONE, result
    by_source = {entry["source"]: entry for entry in result["sources"]}
    # ⚠️ `ark` **不在**清单里 —— 这正是 §44 删掉的混合路径。
    assert set(by_source) == {"creator"}
    assert by_source["creator"]["stored"] == 2  # C-N1 / C-N2
    # ark **列表**一次都没打（连商品笔记列表都没取）。
    assert FakeQianfanAdapter.list_pages == 0

    db = SessionLocal()
    try:
        assert {"C-N1", "C-N2"} <= set(db.scalars(select(Note.note_id)).all())
        # ARK-N1 **没有**被落库：那是商品笔记任务的活（§44）。
        assert "ARK-N1" not in set(db.scalars(select(Note.note_id)).all())
    finally:
        db.close()


def test_goods_note_sync_runs_only_the_ark_source(creator_env, monkeypatch):
    """商品笔记任务（`only="ark"`）**只跑 ark 源**，且**不碰**账号级口径（§44）。

    钉三件事：① 源清单只有 ark；② 创作者适配器一次都没打；③ `notes_count` 与
    note 游标**不被写** —— 千帆报的 `total` 是商品笔记子集数，写进「账号笔记总数」
    会让卡片把账号笔记数显示成子集数；游标被 ark 推进会让普通笔记的增量同步
    跳过从未同步过的旧笔记。
    """
    FakeQianfanAdapter.list_entries = [_ark_note("ARK-N1")]
    _, account_id, user_id, SessionLocal = creator_env
    creator_calls_before = len(FakeCreatorAdapter.calls)

    result = nss.run_note_sync(account_id, user_id=user_id, only=nss.SOURCE_ARK)

    assert result["status"] == sts.STATUS_DONE, result
    by_source = {entry["source"]: entry for entry in result["sources"]}
    assert set(by_source) == {"ark"}
    assert by_source["ark"]["stored"] == 1
    db = SessionLocal()
    try:
        assert "ARK-N1" in set(db.scalars(select(Note.note_id)).all())
        account = db.get(PlatformAccount, account_id)
        # ① 笔记总数没被写成「商品笔记子集数」（1969 是 creator 列表的 total，
        #    本用例里 creator 源没跑 ⇒ 必须是「没写过」而不是「被覆盖成小值」）。
        assert "notes_count" not in json.loads(account.profile_json or "{}")
        # ② 游标没被推进。
        assert int(account.note_sync_cursor_ms or 0) == 0
    finally:
        db.close()
    # 创作者列表一次都没打（普通笔记是另一个任务）。
    assert len(FakeCreatorAdapter.calls) == creator_calls_before


def test_ark_source_hydrates_detail_with_its_own_credentials(creator_env):
    """**只登千帆（账号没有 www 会话）** ⇒ ark 源的详情补齐用**源自己的凭证**（2026-10-12）。

    这是本次改动的**端到端主契约**：`ark` 条目自带 explore 地址 + `xsec_token`
    ⇒ `_sync_source` 必须自己造 PC 适配器来打详情，**不能因为 `www_adapter is None`
    就一条都不补**（旧行为：只登千帆的账号，商业笔记永远没有正文）。

    ⚠️ 夹具用 `creator_env`（**有 ark 令牌**）：`creator_env_no_ark` 连千帆票都没有，
    会在 `_note_sources` 的硬门禁就被拦下（那是另一个用例的事）。这里要的是
    「有千帆票、但**没有 www 会话**」那一格 —— 删掉 www 通道即可复现。

    钉两件事：① 详情真被打了（旧实现这里是空列表）；② 正文落库。
    """
    FakeQianfanAdapter.list_entries = [_ark_note("ARK-H1", xsec_token="ARK-TK-H1")]
    FakePcAdapter.detail_calls = []
    _, account_id, user_id, SessionLocal = creator_env
    # 删掉 www 通道（模拟「只登千帆」的账号）—— ark 通道保留。
    db = SessionLocal()
    try:
        from backend.app.models import AccountCookieVersion

        for row in db.scalars(select(AccountCookieVersion)).all():
            if getattr(row, "channel", "") == "www":
                db.delete(row)
        db.commit()
    finally:
        db.close()

    result = nss.run_note_sync(account_id, user_id=user_id, only=nss.SOURCE_ARK)
    assert result["status"] == sts.STATUS_DONE, result

    # ① 详情请求真发出去了（旧实现这里是 0 —— 本用例的全部意义）。
    assert FakePcAdapter.detail_calls, "只登千帆时也必须打详情（用源自己的令牌）"
    assert any("ARK-H1" in url for url in FakePcAdapter.detail_calls), (
        FakePcAdapter.detail_calls
    )
    db = SessionLocal()
    try:
        note = db.scalars(select(Note).where(Note.note_id == "ARK-H1")).one()
        # ② 正文落库（列表不带正文，这一条只可能来自详情）。
        assert note.content == "详情正文 ARK-H1", note.content
    finally:
        db.close()


def test_goods_note_sync_requires_ark_token(creator_env_no_ark):
    """商品笔记任务的硬门禁是 **ark 令牌**（§44）：没有 ⇒ 结构化失败 + **零平台请求**。

    ⚠️ 这条是拆分要修的语义本身：拆分前 ark 只是混合任务的可选附加源，拿不到就
    **静默少跑一个源**，用户看到的是「同步成功」（只是没有商品笔记）。现在它是一个
    独立任务的门禁，缺令牌必须说出来（`ARK_SESSION_REQUIRED`），且任务类型落在
    `xhs_goods_note_sync` 上（而不是普通笔记那条）。
    """
    _, account_id, user_id, SessionLocal = creator_env_no_ark

    result = nss.run_note_sync(account_id, user_id=user_id, only=nss.SOURCE_ARK)

    assert result["status"] == sts.STATUS_FAILED, result
    assert "ARK_SESSION_REQUIRED" in result["error"]
    # 一条平台请求都没发（账号级请求也没有）。
    assert FakeQianfanAdapter.calls == 0
    assert FakeQianfanAdapter.list_pages == 0
    db = SessionLocal()
    try:
        task = db.get(Task, result["task_id"])
        assert task.task_type == sts.GOODS_NOTE_SYNC_TASK_TYPE, (
            "门禁失败的账也必须记在**商品笔记**的任务类型下（否则任务中心看不到这条失败）"
        )
        assert task.status == sts.STATUS_FAILED
    finally:
        db.close()


def test_ark_merge_never_erases_existing_content(creator_env, monkeypatch):
    """ark 源**填空合并**：已落库的正文 / 标题一个字节不动（§19.3.2 语义不变）。

    §44 起 ark 只在**商品笔记任务**（`only="ark"`）里跑 —— 它仍必须「只补空字段」：
    直接整行覆盖会把详情补齐已经填好的正文抹成空。
    """
    _, account_id, user_id, SessionLocal = creator_env

    # 先造一条「已落库、带正文」的行（模拟详情补齐已经填过正文）。
    db = SessionLocal()
    try:
        db.add(
            Note(
                user_id=user_id,
                platform="xhs",
                platform_account_id=account_id,
                note_id="C-N1",
                title="已有标题",
                content="已有正文",
            )
        )
        db.commit()
    finally:
        db.close()

    FakeQianfanAdapter.list_entries = [_ark_note("C-N1", title="千帆侧标题")]
    result = nss.run_note_sync(account_id, user_id=user_id, only=nss.SOURCE_ARK)
    assert result["status"] == sts.STATUS_DONE, result

    db = SessionLocal()
    try:
        note = db.scalars(select(Note).where(Note.note_id == "C-N1")).one()
        assert note.content == "已有正文"  # 空值**不覆盖**非空（这是本用例的全部意义）
        assert note.title == "已有标题"
        assert note.published is True  # 只升不降（§19.12）
        assert note.raw_json["ark"]["likes"] == 3
    finally:
        db.close()


def test_ark_note_flag_maps_to_note_category(creator_env):
    """千帆条目的 `note_flag == "goods_note"` ⇒ 统计表 `note_flag="goods_note"`（2026-10-12 抓包）。

    这是「商业笔记 vs 普通笔记」的**平台原生判据** —— 不是本地推的。列表返回里
    自带 `note_flag`，所以类型字段是**同步时免费拿到**的，不是第二份真相源。
    2026-10-12 设计 35 起落 `note_datacenter_stats.note_flag`（取代 `notes.note_category`）。
    """
    _, account_id, user_id, SessionLocal = creator_env

    FakeQianfanAdapter.list_entries = [
        _ark_note("C-G1"),  # 夹具带 note_flag="goods_note"
        {**_ark_note("C-N1"), "note_flag": ""},  # 平台没报 flag ⇒ 不写分类（缺值不造）
    ]
    result = nss.run_note_sync(account_id, user_id=user_id, only=nss.SOURCE_ARK)
    assert result["status"] == sts.STATUS_DONE, result

    db = SessionLocal()
    try:
        goods = db.scalars(
            select(NoteDatacenterStat).where(NoteDatacenterStat.note_id == "C-G1")
        ).one()
        assert goods.note_flag == "goods_note", goods.note_flag
        plain = db.scalars(
            select(NoteDatacenterStat).where(NoteDatacenterStat.note_id == "C-N1")
        ).one()
        assert plain.note_flag == "", (
            "平台没报 note_flag ⇒ 分类留空（缺值不造；硬约束⑤）"
        )
    finally:
        db.close()


def test_ark_note_category_refreshes_on_existing_row(creator_env):
    """命中既有行时按当前数据源刷新分类（普通→商业转换要跟着变）。

    与 `note_data_service._upsert_datacenter_stat` 的「覆盖更新时按当前数据源刷新」
    同一口径。2026-10-12 设计 35 起落统计表 `note_flag`。
    """
    _, account_id, user_id, SessionLocal = creator_env
    #: ⚠️ 统计行的 upsert 键含 `stat_date`，而**生产侧取的是 `shanghai_now()`**
    #: （见 `note_data_service._upsert_datacenter_stat`）⇒ 夹具若写死一个日期，
    #: 隔天就不再命中，服务会**插入第二行**，本用例的 `.one()` 便抛
    #: `MultipleResultsFound`（2026-10-14 实测：夹具停在 10-12，跑的那天是 10-14）。
    #: 夹具必须与生产**同源**取当天，否则它是一条「只在某些日子成立」的用例。
    from backend.app.core.time import shanghai_now

    today = shanghai_now().strftime("%Y-%m-%d")
    # 先造一条「本地已落库、分类为空」的行（notes + 空统计行）。
    db = SessionLocal()
    try:
        db.add(
            Note(
                user_id=user_id,
                platform="xhs",
                platform_account_id=account_id,
                note_id="C-R1",
                title="已有标题",
            )
        )
        db.add(
            NoteDatacenterStat(
                user_id=user_id,
                platform_account_id=account_id,
                note_id="C-R1",
                stat_date=today,
                window="recent_1",
                note_flag="",
                title="已有标题",
            )
        )
        db.commit()
    finally:
        db.close()

    FakeQianfanAdapter.list_entries = [_ark_note("C-R1")]
    result = nss.run_note_sync(account_id, user_id=user_id, only=nss.SOURCE_ARK)
    assert result["status"] == sts.STATUS_DONE, result

    db = SessionLocal()
    try:
        #: 判据是「C-R1 只有一行、且它被刷成了 goods_note」——
        #: `scalars(...).all()` 才能把「多出一行」暴露成断言失败，
        #: 而不是让 `.one()` 抛个与业务无关的 SQLAlchemy 异常。
        stats = db.scalars(
            select(NoteDatacenterStat).where(NoteDatacenterStat.note_id == "C-R1")
        ).all()
        assert len(stats) == 1, f"同一篇笔记应只有一行统计，实际 {len(stats)} 行"
        assert stats[0].note_flag == "goods_note", stats[0].note_flag
    finally:
        db.close()

# --------------------------------------------------------------------------- #
# R1 / R3：笔记落库 + Creator 顺带补挂载商品
# --------------------------------------------------------------------------- #
def test_creator_sync_stores_notes_and_writes_notes_count(creator_env):
    _, account_id, user_id, SessionLocal = creator_env
    result = nss.run_note_sync(account_id, user_id=user_id, max_pages=3)

    assert result["status"] == sts.STATUS_DONE, result
    assert result["stored"] == 2

    db = SessionLocal()
    try:
        note_ids = set(db.scalars(select(Note.note_id)).all())
        assert {"C-N1", "C-N2"} <= note_ids
        account = db.get(PlatformAccount, account_id)
        # 总数只有创作者响应里那处 `tags[checked].notes_count` 给得出。
        # `profile_json` 是 Text 列存 JSON 串（不是 JSON 列），要解析。
        assert json.loads(account.profile_json)["notes_count"] == 1969
    finally:
        db.close()


def test_creator_sync_backfills_goods_for_the_page_note_ids(creator_env):
    _, account_id, user_id, SessionLocal = creator_env
    nss.run_note_sync(account_id, user_id=user_id, max_pages=1)

    # 一页笔记 → 一次按需补齐，且带的是**本页**的 note_id。
    assert len(FakeQianfanAdapter.note_id_queries) == 1
    assert sorted(FakeQianfanAdapter.note_id_queries[0]) == ["C-N1", "C-N2"]

    db = SessionLocal()
    try:
        stored = set(db.scalars(select(ShopNoteGoods.note_id)).all())
        assert {"C-N1", "C-N2"} <= stored
    finally:
        db.close()


def test_creator_sync_reports_goods_stored_count(creator_env):
    _, account_id, user_id, _ = creator_env
    result = nss.run_note_sync(account_id, user_id=user_id, max_pages=1)
    assert result["goods_stored"] == 2


# --------------------------------------------------------------------------- #
# §19.14 页内联详情补齐：一次同步拿全再落库（不再有账号级补齐队列）
# --------------------------------------------------------------------------- #

def test_creator_sync_hydrates_the_page_inline(creator_env):
    """有 www 会话 ⇒ **本页笔记落库后就地打详情**：正文 / 图集随同步一起落库。

    钉的是用户 2026-09-23 口径「每次同步笔记时，获取到该笔记下所有数据……全部数据后
    再落库」：一条详情请求都不留给「后续任务」—— `hydrated_at` 当场写上，
    `content_filled` 计入同步结果。
    """
    EXTRA_CHANNEL_COOKIES["www"] = PC_COOKIES
    _, account_id, user_id, SessionLocal = creator_env

    result = nss.run_note_sync(account_id, user_id=user_id, max_pages=1)

    assert result["status"] == sts.STATUS_DONE, result
    # 本页两篇各打一次详情（一次请求同时给正文 + 图集）。
    assert len(FakePcAdapter.detail_calls) == 2, FakePcAdapter.detail_calls
    assert result["content_filled"] == 2

    db = SessionLocal()
    try:
        for note_id in ("C-N1", "C-N2"):
            note = db.scalars(select(Note).where(Note.note_id == note_id)).one()
            assert note.content == f"详情正文 {note_id}", "正文没随同步落库"
            assert note.hydrated_at is not None, "补过的行要写标记（不再进后续候选）"
            # 详情里的图也并进来了（列表只给一张封面）。
            urls = [
                a.url for a in db.scalars(select(NoteAsset).where(NoteAsset.note_id == note.id)).all()
            ]
            assert f"https://cdn.example/{note_id}-detail.jpg" in urls
    finally:
        db.close()


def test_creator_sync_without_www_session_stores_list_data_and_says_so(creator_env):
    """没有 www 会话 ⇒ 列表数据照常落库，一条详情请求都不发，任务行安静说明。

    失败三分法：账号级「暂不可用」不是接口异常 —— 不红块、不中止，一行原因就够。
    """
    _, account_id, user_id, SessionLocal = creator_env

    result = nss.run_note_sync(account_id, user_id=user_id, max_pages=1)

    assert result["status"] == sts.STATUS_DONE, result
    assert FakePcAdapter.detail_calls == [], "没有 www 会话不该打详情"
    assert result["content_filled"] == 0
    assert "网页版（www）会话" in result["note"]

    db = SessionLocal()
    try:
        note = db.scalars(select(Note).where(Note.note_id == "C-N1")).one()
        assert note.content == "", "列表源不带正文 ⇒ 留空不造假（缺值口径）"
        assert note.hydrated_at is None, "没打过详情就不写标记（补登后还能被补）"
    finally:
        db.close()


def test_second_full_sync_does_not_refetch_hydrated_notes(creator_env):
    """**重复全量同步不重复请求已补过的行**（额度 5 次/分钟，白烧就是犯罪）。"""
    EXTRA_CHANNEL_COOKIES["www"] = PC_COOKIES
    _, account_id, user_id, _ = creator_env

    nss.run_note_sync(account_id, user_id=user_id, max_pages=1, mode="full")
    assert len(FakePcAdapter.detail_calls) == 2

    FakePcAdapter.detail_calls.clear()
    nss.run_note_sync(account_id, user_id=user_id, max_pages=1, mode="full")
    assert FakePcAdapter.detail_calls == [], "已补齐的行不许被第二轮全量重复请求"


def test_detail_failures_are_aggregated_into_one_line(creator_env):
    """详情请求失败 ⇒ 列表数据**照常落库** + 跨源聚合成**一行**任务说明 + 不写标记。

    与商品侧 `detail_failed` 同口径：失败是可恢复的，`hydrated_at` 留 NULL，
    下次全量同步重试；「同步成功但正文永远为空且不留痕」正是旧实现的坑。
    """
    EXTRA_CHANNEL_COOKIES["www"] = PC_COOKIES
    FakePcAdapter.detail_fail = True
    _, account_id, user_id, SessionLocal = creator_env

    result = nss.run_note_sync(account_id, user_id=user_id, max_pages=1)

    assert result["status"] == sts.STATUS_DONE, result
    assert len(FakePcAdapter.detail_calls) == 2, "本页两篇都该尝试一次"
    assert "2 条笔记详情获取失败" in result["note"]
    assert result["content_filled"] == 0

    db = SessionLocal()
    try:
        note = db.scalars(select(Note).where(Note.note_id == "C-N1")).one()
        assert note.content == "", "列表源不带正文 ⇒ 失败也不造假"
        assert note.hydrated_at is None, "失败不写标记 ⇒ 下次全量同步重试"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# PC 链路：令牌必须来自请求体，缺了要吼出来
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 有界：三种提前结束
# --------------------------------------------------------------------------- #

def test_full_pagination_stops_only_when_platform_says_done(creator_env, monkeypatch):
    """**不传 `max_pages` = 全量**（§21，2026-09-22 用户口径）。

    翻到平台说「没有下一页」为止 —— 不是翻 3 页就停。这条钉的就是用户报的那个 bug：
    `已落库 180 / 共 1961 条` + `已达本次翻页上限（3 页），剩余留待下一轮`。
    """
    FakeCreatorAdapter.pages = {0: ["C-P1"], 1: ["C-P2"], 2: ["C-P3"], 3: ["C-P4"]}
    _, account_id, user_id, _ = creator_env

    result = nss.run_note_sync(account_id, user_id=user_id)  # 刻意不传 max_pages

    assert len(FakeCreatorAdapter.calls) == 4, FakeCreatorAdapter.calls
    assert result["stored"] == 4
    assert "上限" not in result["note"]


def test_full_sync_advances_the_cursor_so_next_run_is_incremental(creator_env, monkeypatch):
    """**全量同步也必须写游标**（§22，2026-09-22 用户口径）。

    这是「增量化」能真正生效的另一半：旧实现把推进写在
    `if incremental and cursor_advanced` 里 ⇒ 全量同步**永不写游标** ⇒ 游标恒为 0
    ⇒ 自动模式永远选全量 ⇒ 用户看到的「手动同步永远是全量、永远 30–45 分钟」。

    断言分两层：① 全量跑完游标被写成本次观测到的最新发布时间；② 下一轮不传 mode
    时自动落到 incremental（不再翻页扫全表）。
    """
    FakeCreatorAdapter.pages = {0: ["C-P1"], 1: ["C-P2"]}
    _, account_id, user_id, SessionLocal = creator_env

    nss.run_note_sync(account_id, user_id=user_id)  # 不传 mode ⇒ auto

    db = SessionLocal()
    try:
        cursor_after_full = sts.load_note_cursor(db, account_id)
    finally:
        db.close()
    assert cursor_after_full > 0, "全量同步没推进游标 ⇒ 自动模式永远选全量"

    # 第二轮：游标已在 ⇒ auto 应解析成 incremental，且早停（整页都不新）。
    FakeCreatorAdapter.calls = []
    FakeCreatorAdapter.pages = {0: ["C-P1"], 1: ["C-P2"]}
    nss.run_note_sync(account_id, user_id=user_id)

    assert len(FakeCreatorAdapter.calls) == 1, "第二轮应增量早停，不该再翻全表"
    db = SessionLocal()
    try:
        assert sts.load_note_cursor(db, account_id) >= cursor_after_full
    finally:
        db.close()


def test_stops_early_when_platform_reports_no_more(creator_env, monkeypatch):
    """平台 `page=-1` ⇒ 当页就停，**不再多打一次请求**。"""
    _, account_id, user_id, _ = creator_env

    result = nss.run_note_sync(account_id, user_id=user_id)

    assert len(FakeCreatorAdapter.calls) == 1
    # §19.14：没有 www 会话 ⇒ 任务行**诚实两行**（本页没补 + 剩余量与补救路径），
    # 不再像旧队列那样把 `no_session` 原因静默丢掉。
    assert "网页版（www）会话" in result["note"]
    assert "正文待补齐 2 条" in result["note"]


def test_stops_early_when_stored_reaches_total(creator_env, monkeypatch):
    """已落库 ≥ `total` ⇒ 提前结束（不为了凑页数继续翻）。"""
    FakeCreatorAdapter.pages = {0: ["C-P1"], 1: ["C-P2"], 2: ["C-P3"]}
    FakeCreatorAdapter.notes_count = 1  # 平台说总共就 1 条
    _, account_id, user_id, _ = creator_env

    nss.run_note_sync(account_id, user_id=user_id)

    assert len(FakeCreatorAdapter.calls) == 1

# --------------------------------------------------------------------------- #
# 风控：限流停本次，不抛异常，保留已同步
# --------------------------------------------------------------------------- #

def test_rate_limit_wait_timeout_stops_without_raising(creator_env):
    """等待额度**超时** ⇒ 不抛异常：任务 done、原因写任务行（§21 等待制）。

    ⚠️ 与旧契约的区别：这里断言的**不是**「第一页就被限流挡住」，而是
    「等不到额度才停」。`wait_allow` 而不是 `allow` —— 改回去本用例必红。
    """
    _, account_id, user_id, _ = creator_env
    LIMITER["impl"] = DenyLimiter()

    result = nss.run_note_sync(account_id, user_id=user_id)

    assert FakeCreatorAdapter.calls == []
    assert result["status"] == sts.STATUS_DONE  # 不是异常，是「本次到此为止」
    assert "限流" in result["note"]


def test_rate_limit_keeps_what_the_first_page_already_stored(creator_env, monkeypatch):
    """第 1 页放行、第 2 页起等待超时：**已同步的必须留下来**。"""
    FakeCreatorAdapter.pages = {0: ["C-P1"], 1: ["C-P2"]}

    class OneShotLimiter:
        """第 1 次等额度就成功，之后一律超时。"""

        def __init__(self) -> None:
            self.n = 0

        def allow(self, key) -> bool:  # noqa: ARG002
            self.n += 1
            return self.n == 1

        def wait_allow(self, key, **kwargs) -> bool:  # noqa: ARG002
            return self.allow(key)

    LIMITER["impl"] = OneShotLimiter()
    _, account_id, user_id, SessionLocal = creator_env

    result = nss.run_note_sync(account_id, user_id=user_id)

    assert len(FakeCreatorAdapter.calls) == 1
    assert result["stored"] == 1
    db = SessionLocal()
    try:
        assert db.scalar(select(Note.id).where(Note.note_id == "C-P1")) is not None
    finally:
        db.close()

# --------------------------------------------------------------------------- #
# §19.11：限流键必须带源（真机教训：三源共用一个账号键 ⇒ ark 源第 1 页就被挡）
# --------------------------------------------------------------------------- #
def test_rate_limit_key_is_per_source():
    """账号键耗光后，**带源的键必须还能放行**（平台风控按平台各自独立）。

    这条钉的是修复本身：改回 `allow(account.id)` 单键，本测试必红。
    """
    from backend.app.services.rate_limiter import CrawlRateLimiter

    limiter = CrawlRateLimiter(max_per_minute=3)
    assert all(limiter.allow(42) for _ in range(3))
    assert limiter.allow(42) is False  # 账号键的额度用完了

    # 另一个平台（ark / goods）的键与账号键互不相干 —— 真实请求量没超。
    assert limiter.allow("42:ark") is True
    assert limiter.allow("42:www") is True
    assert limiter.allow("42:goods") is True
    # reset 也按 key（清账号键不会清掉源键）。
    limiter.reset(42)
    assert limiter.allow(42) is True



def test_limited_source_is_marked_limited(creator_env, monkeypatch):
    """源被限流 ⇒ `limited=True`、stored=0（**不是**「千帆 0 条笔记」）。

    真机教训（2026-09-22，acct 8）：翻页耗光额度后源被挡，结果行显示
    「千帆商品笔记 0 条」—— 把「没跑完」显示成了「没数据」。

    §44 起两条链路各自有「被限流」的可能，这里两个方向都钉：creator 源（普通笔记任务）
    与 ark 源（商品笔记任务），判据同一条 —— 限流桶按 `<账号>:<源>` 分（§38.3），
    所以只挡其中一个源时另一个源照常跑完。
    """
    _, account_id, user_id, _ = creator_env

    class PerKeyLimiter:
        """只挡某一个源的假限流器（按限流键判定，不按账号）。"""

        def __init__(self, suffix: str) -> None:
            self.suffix = suffix

        def allow(self, key) -> bool:  # noqa: ARG002
            return not str(key).endswith(self.suffix)

        def wait_allow(self, key, **kwargs) -> bool:  # noqa: ARG002
            return self.allow(key)

    # ① 商品笔记任务：ark 源被挡 ⇒ limited=True、stored=0（不是「千帆没笔记」）。
    LIMITER["impl"] = PerKeyLimiter(":ark")
    FakeQianfanAdapter.list_entries = [_ark_note("ARK-N1")]
    ark_result = nss.run_note_sync(account_id, user_id=user_id, only=nss.SOURCE_ARK)
    ark_by_source = {entry["source"]: entry for entry in ark_result["sources"]}
    assert set(ark_by_source) == {"ark"}
    assert ark_by_source["ark"]["stored"] == 0
    assert ark_by_source["ark"]["limited"] is True

    # ② 普通笔记任务：creator 源被挡 ⇒ 同上；而且**ark 源一次都没被碰**
    #    （拆分之后两条链路不共享额度，也不再互相兜底）。
    LIMITER["impl"] = PerKeyLimiter(":creator")
    FakeQianfanAdapter.list_pages = 0
    creator_result = nss.run_note_sync(account_id, user_id=user_id)
    creator_by_source = {entry["source"]: entry for entry in creator_result["sources"]}
    assert set(creator_by_source) == {"creator"}
    assert creator_by_source["creator"]["stored"] == 0
    assert creator_by_source["creator"]["limited"] is True
    assert FakeQianfanAdapter.list_pages == 0

# --------------------------------------------------------------------------- #
# 缺值不造假（硬约束⑤）
# --------------------------------------------------------------------------- #
def test_creator_total_missing_does_not_write_a_zero(creator_env):
    """拿不到 `tags[checked].notes_count` 时**不许**用「本页条数」冒充总数。

    `pc.py::_creator_total` 在字段缺失时退化成 `len(items)` —— 对展示接口是
    「不精确」，对我们会把「有待同步」的分母写成 2（于是永远判定已同步完）。
    """
    _, account_id, user_id, SessionLocal = creator_env
    FakeCreatorAdapter.notes_count = 0

    result = nss.run_note_sync(account_id, user_id=user_id, max_pages=1)

    assert result["total"] is None
    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, account_id)
        assert "notes_count" not in json.loads(account.profile_json or "{}")
    finally:
        db.close()


def test_bounded_sync_does_not_report_a_progress_denominator(creator_env, monkeypatch):
    """§35.18.6：**有界同步**（增量 / 用户选的时间区间）不报进度分母。

    2026-09-27 用户报障的诱因：`progress.total` 取的是**平台报告的账号笔记总量**
    （真实库 1951），而区间同步只落个位数 ⇒ 进度条恒 0–1% ⇒ 用户以为卡死、反复取消
    ⇒ 收尾段（详情令牌刷新 + 账号级补齐）整段没跑 ⇒ 详情弹窗里正文全空。

    口径：只有**无界全量**的分母是真的（全量会翻到底，`stored` 最终收敛到它）；
    有界时一律**不写 `total` 键**（缺值口径：不知道就不报，不拿账号总量冒充）。

    ⚠️ **2026-09-28 同日第三轮收紧**（用户报障「任务进度列的进度条自动达到了 100% 但是
    此时任务刚刚开始」，设计 13 §43）：`work_total` **也不许拿运行中的量充数** ——
    有界 + **零欠账**时，列表阶段要翻多少条只有跑完才知道，旧实现拿「已落库条数」当分母，
    而它与分子的「本页已处理」数的是**同一批笔记**（两边每页同步 +N）⇒ 比值恒 100%
    ⇒ 进度条第一页就打满。现在：**零欠账 ⇒ 不报分母**（`progress_percent` 返回 `None`
    ⇒ 前端渲染「比例未知」的不定量条）；**有欠账 ⇒ 照报**（欠账是启动时数得清的工作量，
    分母 = 已落库条数 + 欠账，保证 `processed ≤ work_total`）。
    """
    _, account_id, user_id, SessionLocal = creator_env

    # ① 无界全量 ⇒ 分母 = 平台报告的账号总量（本夹具 2 条）
    nss.run_note_sync(account_id, user_id=user_id, max_pages=1, mode=sts.SYNC_MODE_FULL)
    db = SessionLocal()
    try:
        task = db.scalars(
            select(Task).where(Task.task_type == sts.NOTE_SYNC_TASK_TYPE).order_by(Task.id.desc())
        ).first()
        full_progress = sts.task_payload(task)["progress"]
        # 分母的具体值由适配器报告（本夹具非 2 —— 那是「本页条数」的另一回事），
        # 这里只钉「无界全量**必须**有分母」。
        assert full_progress.get("total") is not None, "无界全量必须报出分母"
    finally:
        db.close()

    # ② 用户选了时间区间（只有上界也算有界）⇒ **不写** `total` 键（早停 ⇒ 分母不可知）
    nss.run_note_sync(
        account_id,
        user_id=user_id,
        max_pages=1,
        mode=sts.SYNC_MODE_FULL,
        time_from_ms=0,
        time_to_ms=2**50,
    )
    db = SessionLocal()
    try:
        task = db.scalars(
            select(Task).where(Task.task_type == sts.NOTE_SYNC_TASK_TYPE).order_by(Task.id.desc())
        ).first()
        progress = sts.task_payload(task)["progress"]
        assert "total" not in progress, f"有界同步不该报分母：{progress}"
        assert progress["stored"] == 2, "已落库数照常报（它是事实，不是比例）"
        assert isinstance(progress.get("processed"), int), f"已处理条数必须报出：{progress}"
        # ③ 零欠账的有界同步 ⇒ **不报分母**（旧实现在这里报 `work_total = stored`，
        #    与分子的「本页已处理」同源 ⇒ 比值恒 100%，正是用户报障的那个形态）。
        assert progress.get("work_total") is None, (
            f"零欠账的有界同步不该报分母（分母会与分子同源 ⇒ 恒 100%）：{progress}"
        )
        assert sts.progress_percent(progress) is None, (
            "没有可预知的分母 ⇒ 比例**未知**（不是 0%、更不是 100%）——"
            "前端据此渲染不定量条（设计 13 §43.1）"
        )
    finally:
        db.close()

    # ④ 有界 + **有欠账** ⇒ 分母照报：欠账是「启动时数得清」的工作量，
    #    且 `processed ≤ work_total` 恒成立（分母含本任务已落库条数那一段，
    #    与分子的「本页已处理」一一对应 ⇒ 不虚高、恰在欠账补完那一刻到 100%）。
    monkeypatch.setattr(nss, "_pending_hydration_count", lambda *a, **k: 7)
    monkeypatch.setattr(nss, "_pending_hydration_without_token_count", lambda *a, **k: 0)
    nss.run_note_sync(
        account_id,
        user_id=user_id,
        max_pages=1,
        mode=sts.SYNC_MODE_FULL,
        time_from_ms=0,
        time_to_ms=2**50,
    )
    db = SessionLocal()
    try:
        task = db.scalars(
            select(Task).where(Task.task_type == sts.NOTE_SYNC_TASK_TYPE).order_by(Task.id.desc())
        ).first()
        progress = sts.task_payload(task)["progress"]
    finally:
        db.close()

    work_total = int(progress.get("work_total") or 0)
    processed = int(progress.get("processed") or 0)
    assert work_total >= 7, f"有欠账的有界同步必须报出分母（含欠账数）：{progress}"
    assert processed <= work_total, f"已处理数不得超过分母（分母算错了会虚高到 100%）：{progress}"


def test_note_sync_reports_task_scoped_monotonic_progress(creator_env):
    """2026-09-28 用户口径：进度条**慢慢增加**且**绝不 0→100 反复跳**（设计 13 §42.4）。

    判据（口径层的三条不变量，不依赖具体平台数据）：
    ① 进度看的是 `processed / work_total`（**本任务**累计已处理 / 预计总工作量），
       不是「一页内 0→100」的 `detail_done / detail_total`；
    ② `work_total` 是**本次任务**的工作量（无界全量 = 平台报告的账号总量 + 可处理的详情欠账），
       且 `processed ≤ work_total`（超了就是分母算错，进度条会虚高到 100 后停住）；
    ③ `progress_percent` 单调：同一任务里先后两次读到的百分比**不许变小**。
    """
    _, account_id, user_id, SessionLocal = creator_env

    nss.run_note_sync(account_id, user_id=user_id, max_pages=1, mode=sts.SYNC_MODE_FULL)
    db = SessionLocal()
    try:
        task = db.scalars(
            select(Task).where(Task.task_type == sts.NOTE_SYNC_TASK_TYPE).order_by(Task.id.desc())
        ).first()
        progress = sts.task_payload(task)["progress"]
    finally:
        db.close()

    processed = int(progress.get("processed") or 0)
    work_total = int(progress.get("work_total") or 0)
    assert work_total > 0, f"必须报出本次任务的预计工作量：{progress}"
    assert processed <= work_total, f"已处理数不得超过预计工作量（分母算错了）：{progress}"
    assert sts.progress_percent(progress) == max(
        0, min(100, int(processed * 100 / work_total))
    ), "百分比必须走 processed / work_total 这条口径"
    # 单调性：把「已处理」往后推一点，百分比**只许变大或不变**
    before = sts.progress_percent(progress)
    after = sts.progress_percent({**progress, "processed": min(processed + 1, work_total)})
    assert after >= before, f"进度不许倒退：{before} → {after}"


def test_cancel_reports_the_detail_backlog(creator_env, monkeypatch):
    """§35.18.6：取消 ⇒ 说明行必须报出「正文与图集还欠多少」并给出下一步。

    2026-09-27 用户报障：取消后详情弹窗里全是「列表源不带正文」/「（无商品标题）
    价格待同步」，而任务行只写「已被用户取消」⇒ 用户以为同步已经完成、数据本该如此。
    根因是**收尾段（详情令牌刷新 + 账号级补齐）整段没跑**，欠账必须如实报出来。

    同时钉住取消后缀**只出现一次**：调用方传业务说明，后缀由 `_finish` 统一拼
    （旧实现两处都拼 ⇒「已被用户取消；已被用户取消」，真实库 task=11 实测）。
    """
    _, account_id, user_id, SessionLocal = creator_env

    # 前置：先落一批「列表有、详情没补」的行（本夹具**没有 www 会话** ⇒ 正文留空、
    # `hydrated_at` 留 NULL —— 正是收尾段要补的那批）。
    nss.run_note_sync(account_id, user_id=user_id, max_pages=1)

    # 再跑一次，但一开跑就命中取消信号 ⇒ 收尾段整段不执行。
    monkeypatch.setattr(task_cancel, "is_cancelled", lambda _task_id: True)
    nss.run_note_sync(account_id, user_id=user_id, max_pages=1)

    db = SessionLocal()
    try:
        task = db.scalars(
            select(Task).where(Task.task_type == sts.NOTE_SYNC_TASK_TYPE).order_by(Task.id.desc())
        ).first()
        assert task.status == sts.STATUS_CANCELLED
        note = sts.task_payload(task)["note"]
    finally:
        db.close()

    assert "待补 2 条" in note, f"取消必须报出欠账：{note}"
    assert "再次同步可继续补" in note, f"要给出下一步：{note}"
    assert note.count("已被用户取消") == 1, f"取消后缀只许出现一次：{note}"


# --------------------------------------------------------------------------- #
# 防重（N3）+ 状态接口
# --------------------------------------------------------------------------- #
def test_second_trigger_reuses_the_active_task(creator_env, monkeypatch):
    headers, account_id, _user_id, SessionLocal = creator_env
    # 不让线程真的跑：本用例只盯任务行的防重语义。
    monkeypatch.setattr(ats, "ensure_account_task_worker", lambda *a, **k: None)

    first = client.post(f"/api/accounts/{account_id}/notes/sync", headers=headers, json={})
    assert first.status_code == 200, first.text
    second = client.post(f"/api/accounts/{account_id}/notes/sync", headers=headers, json={})
    assert second.status_code == 200, second.text

    assert first.json()["task_id"] == second.json()["task_id"]
    db = SessionLocal()
    try:
        rows = db.scalars(select(Task).where(Task.task_type == sts.NOTE_SYNC_TASK_TYPE)).all()
        assert len(rows) == 1
    finally:
        db.close()


def test_status_is_idle_before_any_task_and_after_one_finishes(creator_env):
    """`last_result` 的来源 = **该账号最新一条同步日志**（2026-09-27 判据变更，设计 13 §35.17）。

    ⚠️ **被取代的原断言（保留备查）**：改造前这里跑完 `nss.run_note_sync(...)` 就直接断言
    `last_result["status"] == STATUS_DONE` 且 `progress["stored"] == 2` —— 那时 `last_result`
    读的是**业务任务行**。判据改成「该账号最新一条同步日志」后**不做兼容回落**（两套判据
    并存正是「日志 failed / 卡片同步中」的土壤）⇒ 任务行在、日志不在 ⇒ 端点回**空态**。
    账号卡完整链路（点按钮 ⇒ 任务行 + 日志行 ⇒ 终态一致）由
    `test_account_card_sync_job_log.py` 覆盖；这里钉的是**端点读的是日志**。
    """
    from backend.app.core.time import shanghai_now
    from backend.app.models.scheduled_job import ScheduledJobLog
    from backend.app.services.job_registry import JOB_KEY_NOTE_SYNC

    headers, account_id, user_id, SessionLocal = creator_env

    idle = client.get(f"/api/accounts/{account_id}/notes/sync/status", headers=headers)
    assert idle.status_code == 200
    assert idle.json()["task_id"] is None
    assert idle.json()["status"] == "idle"
    assert idle.json()["last_result"] is None

    # 只落业务任务行（不写日志）⇒ 端点**仍是空态**：判据只认日志
    nss.run_note_sync(account_id, user_id=user_id, max_pages=1)
    task_only = client.get(f"/api/accounts/{account_id}/notes/sync/status", headers=headers)
    assert task_only.json()["status"] == "idle"
    assert task_only.json()["last_result"] is None, "判据只认日志，不回读业务任务行"

    # 补一条终态日志（账号卡路径由 `scheduled_job_runner.run_targeted_job` 写）⇒ 端点回它
    db = SessionLocal()
    try:
        db.add(
            ScheduledJobLog(
                user_id=user_id,
                platform_account_id=account_id,
                job_key=JOB_KEY_NOTE_SYNC,
                job_name="同步笔记",
                trigger_type="manual",
                status="success",
                progress=100,
                duration_ms=1234,
                started_at=shanghai_now(),
                finished_at=shanghai_now(),
                result={"stored": 2, "total": 2},
            )
        )
        db.commit()
    finally:
        db.close()

    after = client.get(f"/api/accounts/{account_id}/notes/sync/status", headers=headers)
    body = after.json()
    assert body["task_id"] is None
    assert body["status"] == "idle"
    assert body["last_result"]["status"] == sts.STATUS_DONE
    assert body["last_result"]["progress"] == 100
    assert body["last_result"]["finished_at"]


# --------------------------------------------------------------------------- #
# 鉴权与参数边界
# --------------------------------------------------------------------------- #
def test_other_users_account_is_404(creator_env, monkeypatch):
    _, account_id, _user_id, _ = creator_env
    monkeypatch.setattr(ats, "ensure_account_task_worker", lambda *a, **k: None)

    other = client.post("/api/auth/register", json={"username": "intruder", "password": "secret123"})
    assert other.status_code == 200, other.text
    headers = {"Authorization": f"Bearer {other.json()['access_token']}"}

    assert client.post(f"/api/accounts/{account_id}/notes/sync", headers=headers).status_code == 404
    assert (
        client.get(f"/api/accounts/{account_id}/notes/sync/status", headers=headers).status_code
        == 404
    )



def test_page_bounds_follow_the_new_contract(creator_env, monkeypatch):
    """参数边界（§21）：`0` 越界 422；`21` **现在合法**（上限已放到 `ABSOLUTE_PAGE_CAP`）；
    超过绝对兜底 422；`None`/缺省 = 全量。"""
    headers, account_id, _user_id, _ = creator_env
    monkeypatch.setattr(ats, "ensure_account_task_worker", lambda *a, **k: None)

    reject = ({"max_pages": 0}, {"max_pages": 501}, {"page_size": 0}, {"page_size": 101})
    for body in reject:
        response = client.post(
            f"/api/accounts/{account_id}/notes/sync", headers=headers, json=body
        )
        assert response.status_code == 422, (body, response.text)

    # 21 页**不再是越界**（旧契约 le=20 已废）；缺省也是合法请求（= 全量）。
    for body in ({"max_pages": 21}, {}):
        response = client.post(
            f"/api/accounts/{account_id}/notes/sync", headers=headers, json=body
        )
        assert response.status_code == 200, (body, response.text)

# --------------------------------------------------------------------------- #
# 硬约束 C5：源码级守卫，不许出现全量循环
# --------------------------------------------------------------------------- #
def test_service_never_calls_full_loop_methods():
    """用 AST 查**真实调用**，不查文档字符串。

    直接对源码文本做 `in` 判断会被自己的注释/文档字符串误伤（本模块的文档
    字符串就正好在讲「禁用 `get_all_posted_notes`」）—— 那种守卫会在「改注释」
    时变红、在「真调用」时漏判，两边都错。所以只看 Attribute 调用。
    """
    tree = ast.parse(inspect.getsource(nss))
    banned = {"get_all_posted_notes", "get_user_all_notes", "get_all_notes"}
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (called & banned), f"笔记同步不得调用全量循环方法：{called & banned}"


# --------------------------------------------------------------------------- #
# §21：全量翻页契约（2026-09-22 用户口径）
# --------------------------------------------------------------------------- #
def test_clamp_pages_none_is_full_and_explicit_is_capped():
    """`clamp_pages`：`None` / 坏值 = **全量**；显式数字夹到 `[1, ABSOLUTE_PAGE_CAP]`。

    ⚠️ 旧契约「坏值退回 3 页」已废 —— 把「没传」当「传了 3」正是那个 bug 的入口。
    """
    assert sts.clamp_pages(None) is None
    assert sts.clamp_pages("") is None
    assert sts.clamp_pages("abc") is None
    assert sts.clamp_pages(0) == 1
    assert sts.clamp_pages(7) == 7
    assert sts.clamp_pages(10**6) == sts.ABSOLUTE_PAGE_CAP


def test_absolute_page_cap_exists_as_a_dead_loop_guard():
    """绝对兜底必须存在且**远高于**真实数据量（否则「取消上限」= 死循环）。"""
    assert sts.DEFAULT_MAX_PAGES is None
    assert sts.ABSOLUTE_PAGE_CAP >= 100


def test_absolute_cap_stops_a_platform_that_never_says_done(creator_env, monkeypatch):
    """平台永远不说「没有下一页」时，循环必须在绝对兜底处停下并如实说明。"""
    monkeypatch.setattr(sts, "ABSOLUTE_PAGE_CAP", 3)
    FakeCreatorAdapter.pages = {0: ["C-P1"], 1: ["C-P2"], 2: ["C-P3"], 3: ["C-P4"]}
    _, account_id, user_id, _ = creator_env

    result = nss.run_note_sync(account_id, user_id=user_id)

    assert len(FakeCreatorAdapter.calls) == 3
    assert "兜底" in result["note"]


def test_wait_allow_returns_false_on_timeout_without_hanging():
    """`wait_allow`：额度够就立即放行；超时窗口内等不到就返回 False（不挂死）。"""
    from backend.app.services.rate_limiter import CrawlRateLimiter

    fresh = CrawlRateLimiter(max_per_minute=2)
    assert fresh.wait_allow("9:creator", timeout=0) is True

    used = CrawlRateLimiter(max_per_minute=1)
    assert used.allow("8:creator") is True
    assert used.wait_allow("8:creator", timeout=0) is False
