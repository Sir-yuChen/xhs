"""笔记详情补齐（设计 13 §19.13 / §19.14）：列表项没有正文，落库后打一次详情补正文 + 图集。

三个源的**列表接口都不带正文**，图片也可能只有一张封面（千帆 `goods-note/list`
只给 `note_image`）；而**详情接口一次给出正文 + 完整图集** —— 本机真机实测
（只读 GET，账号 8）：`content` 481 字、`image_list` 4 张。

**§19.14（2026-09-23）起补齐改为「页内联」**：`_sync_source` 每翻一页，落库后就地
把**本页**笔记的详情打全（正文 / 图集 / 视频流地址 / 话题），一次同步拿全再落库 ——
账号级补齐队列（`_hydrate_pending_notes`、cap=30、跨轮累积）整段移除。本文件守的
就是这套新纪律：

* **只补本页**：候选 = 传进来的 `note_ids` 里需要补的行，**不外溢**到账号其他行
  （旧队列是账号级跨轮累积，这正是被取代的东西）；
* **已补过不重复请求**：`hydrated_at` 非空且不欠视频资产 ⇒ 跳过（额度 5 次/分钟）；
* **失败不写标记**：列表数据照常落库，`hydrated_at` 留 NULL，下次**全量同步**重试
  （与商品侧 `detail_synced_at` 同口径；旧「失败也写、防饿死」的前提——队列——已不存在）；
* **限流截断可见**：被限流时停本页剩余条目并把条数报出来，不逐条静默 `continue`；
* **填空不覆盖**：正文只在本地为空时写、图片只补本地没有的 url。

**补齐用的会话只有一种来源：这个账号自己的 www 令牌。** 夹具里刻意放了
**另一个账号**（同昵称、持可用 www 会话）当「诱惑项」—— 谁把它当候选，谁就会
把别人的数据面写进本地库。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.app.core.database import Base, get_db
from tests.backend.conftest import seed_owner_rows  # noqa: E402
from backend.app.core.time import shanghai_now  # noqa: E402
from backend.app.models import Note, NoteAsset, PlatformAccount, User  # noqa: E402
from backend.app.services.account_service import (  # noqa: E402
    CHANNEL_CREATOR,
    CHANNEL_WWW,
)
from backend.app.services.rate_limiter import get_rate_limiter  # noqa: E402
from tests.backend.cookie_fixtures import cookie_version, cookie_version_multi  # noqa: E402

#: 测试**统一使用的账号**：真机那个「观隅好物」（小红书号 293066917）。
GUANYU_NICKNAME = "观隅好物"
GUANYU_PROFILE = json.dumps({"red_id": "293066917"})

#: 账号自己的 www 令牌（详情补齐唯一合法来源）。
OWN_WWW_JAR = '{"a1":"creator-a1","web_session":"own-web-session","id_token":"own-id-token"}'
#: 兄弟账号的 www 令牌 —— 出现它就说明发生了跨账号读取。
SIBLING_WWW_JAR = '{"a1":"pc-a1","web_session":"sibling-web-session","id_token":"sibling-id-token"}'

#: 真实形状的笔记 id（24 位 hex）—— 假 id 会让「拼详情 URL」这一步静默退化。
HEX_NOTE_ID = "6a7d14b20000000028001cb8"
HEX_NOTE_ID_2 = "6a7d147100000000050284b5"


def _override_database(tmp_path: Path) -> Any:
    """隔离的 SQLite（与 ``tests/backend/test_api.py`` 同款的自包含实现）。"""
    from backend.app.main import app

    engine = create_engine(
        f"sqlite:///{tmp_path / 'hydrate-test.db'}", connect_args={"check_same_thread": False}
    )
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    # ⚠️ 全局 FK=ON（core/database.py，M5 引入）后必须播种父行：本文件大量插
    # platform_accounts / notes，(user_id, platform_account_id) 指向 users /
    # platform_accounts，而这两个父表此前没人建行（2026-10-04 实测 21 条红）。
    seed_owner_rows(engine, user_id=1, account_id=1)

    def override_get_db() -> Any:
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return override_get_db


def _build_db(tmp_path: Path, *, own_www: bool = True) -> Any:
    """一个 creator 账号 + 一个**同昵称** PC 账号（后者的 www 会话只许存在、不许被用）。

    ``own_www=False`` 用来造「这个账号自己没有网页会话」—— 那种情况下正确行为是
    **一次请求都不发**，哪怕兄弟账号手里有一份可用的 www 会话。
    """
    from backend.app.main import app

    db_dependency = _override_database(tmp_path)
    db = next(app.dependency_overrides[get_db]())
    user = User(username="hydrate-owner", password_hash="x")
    db.add(user)
    db.flush()

    creator = PlatformAccount(
        user_id=user.id, platform="xhs", sub_type="creator",
        nickname=GUANYU_NICKNAME, profile_json=GUANYU_PROFILE, status="active",
    )
    # 同一账号的另一个链路（pc）。它归属另一个用户（`user_id + 1000`）：
    # 连租户都不在一起，更不该被读到。
    # ⚠️ **那个用户必须真建出来**（2026-10-04）：全局 FK=ON 后
    # `platform_accounts.user_id -> users.id` 生效，只给个 id 不建行 ⇒
    # `FOREIGN KEY constraint failed`（此前 FK=OFF 时能过）。
    sibling_user = User(
        id=user.id + 1000, username=f"hydrate-sibling-{user.id}", password_hash="x"
    )
    db.add(sibling_user)
    db.flush()
    sibling = PlatformAccount(
        user_id=sibling_user.id, platform="xhs", sub_type="pc",
        nickname=GUANYU_NICKNAME, profile_json=GUANYU_PROFILE, status="active",
    )
    db.add_all([creator, sibling])
    db.flush()

    channels = {CHANNEL_CREATOR: '{"a1":"creator-a1","galaxy_creator_session_id":"x"}'}
    if own_www:
        channels[CHANNEL_WWW] = OWN_WWW_JAR
    db.add(cookie_version_multi(creator.id, channels))
    db.add(cookie_version(sibling.id, SIBLING_WWW_JAR, CHANNEL_WWW))
    db.commit()
    get_rate_limiter().reset(creator.id)  # 限流器是全局单例：每个用例从干净窗口起
    return db_dependency, db, creator


def _add_note(
    db: Any,
    account: Any,
    note_id: str,
    content: str = "",
    *,
    raw: dict[str, Any] | None = None,
    hydrated: bool = False,
) -> Note:
    note = Note(
        platform_account_id=account.id,
        user_id=account.user_id,
        note_id=note_id,
        platform="xhs",
        title=note_id,
        content=content,
        raw_json=raw,
        hydrated_at=shanghai_now() if hydrated else None,
    )
    db.add(note)
    db.commit()
    return note


def _detail_payload(note_id: str, *, desc: str = "", images: list[str] | None = None) -> dict:
    card: dict[str, Any] = {"note_id": note_id, "desc": desc}
    card["image_list"] = [{"url": url} for url in (images or [])]
    return {"data": {"items": [{"id": note_id, "note_card": card}]}}


def _no_download(monkeypatch: Any) -> None:
    """补齐会真的下图 —— 测试里禁掉（断言的是「补了哪几张」，不是「下没下成功」）。"""
    from backend.app.api.platforms.xhs import crawl

    monkeypatch.setattr(crawl, "_download_asset", lambda url, user_id, kind: "")


def _asset_urls(db: Any, note: Note) -> list[str]:
    return [
        asset.url
        for asset in db.scalars(
            select(NoteAsset).where(NoteAsset.note_id == note.id).order_by(NoteAsset.id.asc())
        ).all()
    ]


class _AllowLimiter:
    """立即放行（§21.3 等待制在测试里不真等待）。"""

    def allow(self, key):  # noqa: ARG002
        return True

    def wait_allow(self, key, **kwargs):  # noqa: ARG002
        return True


class _DenyLimiter:
    """额度**永远**没有：`wait_allow` 立即超时（不真等待）= 模拟「等到超时仍无额度」。"""

    def allow(self, key):  # noqa: ARG002
        return False

    def wait_allow(self, key, **kwargs):  # noqa: ARG002
        return False


def _hydrate(db: Any, account: Any, note_ids: list[str], adapter: Any, *, limiter: Any = None) -> dict:
    """页内联补齐的统一调用口（会话取账号自己的 www 通道）。"""
    import backend.app.services.note_sync_service as nss

    return nss._hydrate_page_notes(
        db,
        account,
        note_ids,
        cookies=nss._account_cookies(db, account.id, channel=CHANNEL_WWW),
        adapter=adapter,
        limiter=limiter or _AllowLimiter(),
    )


class TestHydratePageNotes:
    """页内联补齐的纪律（设计 13 §19.14）。"""

    def test_fills_content_and_images_with_the_accounts_own_session(
        self, tmp_path: Path, monkeypatch: Any
    ):
        """一次详情请求同时补**正文与图集**；会话必须取自**账号自己的** www 通道。

        §19.14 起适配器由编排器用「账号自己 www 通道的令牌」预先构造，所以这里的
        关键断言分两层：① 凭据解析入口（`_account_cookies`）取到的是本账号那份 ——
        兄弟账号（同昵称、另一租户）手里的 www 会话**只许存在、不许被读**；
        ② 补齐结果：正文 + 图集当场落库、标记当场写上。
        """
        import backend.app.services.note_sync_service as nss

        _no_download(monkeypatch)
        db_dependency, db, creator = _build_db(tmp_path)
        try:
            note = _add_note(db, creator, HEX_NOTE_ID, raw={"note_url": f"https://x/{HEX_NOTE_ID}"})
            calls: list[str] = []

            class FakeAdapter:
                def __init__(self, cookies):
                    pass

                def get_note_info(self, url):
                    calls.append(url)
                    return True, "ok", _detail_payload(
                        HEX_NOTE_ID, desc="这是正文", images=["https://img/1", "https://img/2"]
                    )

            # ① 凭据解析：只认本账号自己的 www 通道（关系断言，不是找词）。
            own = nss._account_cookies(db, creator.id, channel=CHANNEL_WWW)
            assert "own-web-session" in own
            assert "sibling-web-session" not in own

            # ② 页内联补齐（编排器把用这份令牌构造的适配器传进来）。
            result = _hydrate(db, creator, [note.note_id], FakeAdapter("x"))

            stored = db.scalars(select(Note).where(Note.note_id == HEX_NOTE_ID)).first()
            assert stored is not None
            assert stored.content == "这是正文"
            # 图集也补上了（旧实现只补正文，图**永远**停在列表给的那一张）。
            assert _asset_urls(db, stored) == ["https://img/1", "https://img/2"]
            assert result["filled"] == 1
            # 一条笔记**只打一次**详情：正文与图是同一次响应给的。
            assert calls == [f"https://x/{HEX_NOTE_ID}"]
            assert stored.hydrated_at is not None
        finally:
            db.close()

    def test_only_the_pages_notes_are_touched(self, tmp_path: Path, monkeypatch: Any):
        """**只补本页**：候选 = 传进来的 note_ids，不外溢到账号其他行。

        这条钉的是 §19.14 与旧队列的本质区别：旧 `_hydrate_pending_notes` 是
        **账号级跨轮累积**（本页之外的欠账行也会被补）；新口径是页内联 ——
        老行等下次**全量同步**枚举到它时再补，本轮一条请求都不多为它发。
        """
        import backend.app.services.note_sync_service as nss

        _no_download(monkeypatch)
        db_dependency, db, creator = _build_db(tmp_path)
        try:
            on_page = _add_note(db, creator, HEX_NOTE_ID, raw={"note_url": "https://x/a"})
            off_page = _add_note(db, creator, HEX_NOTE_ID_2, raw={"note_url": "https://x/b"})
            calls: list[str] = []

            class SpyAdapter:
                def __init__(self, cookies):
                    pass

                def get_note_info(self, url):
                    calls.append(url)
                    return True, "ok", _detail_payload("x", desc="正文")

            result = _hydrate(db, creator, [on_page.note_id], SpyAdapter("x"))

            assert calls == ["https://x/a"], "只许为本页这条发请求"
            assert result["attempted"] == 1
            # 页外那一行**原样不动**：正文仍空、标记仍 NULL（等全量同步）。
            assert off_page.content == "" and off_page.hydrated_at is None
        finally:
            db.close()

    def test_images_are_merged_not_replaced(self, tmp_path: Path, monkeypatch: Any):
        """填空语义：已有封面保留、只补新 url、重复 url 不重复加。"""
        import backend.app.services.note_sync_service as nss

        _no_download(monkeypatch)
        db_dependency, db, creator = _build_db(tmp_path)
        try:
            note = _add_note(db, creator, HEX_NOTE_ID, raw={"note_url": "https://x/n"})
            db.add(NoteAsset(note_id=note.id, asset_type="image", url="https://img/cover"))
            db.commit()

            class FakeAdapter:
                def __init__(self, cookies):
                    pass

                def get_note_info(self, url):
                    return True, "ok", _detail_payload(
                        HEX_NOTE_ID, desc="正文", images=["https://img/cover", "https://img/2"]
                    )

            _hydrate(db, creator, [note.note_id], FakeAdapter("x"))

            # 封面**没有被删**，只多了一张（「先删后插」的整行覆盖语义不适用这里）。
            assert _asset_urls(db, note) == ["https://img/cover", "https://img/2"]
        finally:
            db.close()

    def test_failure_keeps_list_data_and_leaves_the_marker_empty(self, tmp_path: Path):
        """请求异常 → 静默、保留列表数据；**不写** `hydrated_at`（下次全量同步重试）。

        §19.14 起失败不再写标记：账号级队列已移除，「失败也写以防饿死」的前提
        不存在，写了反而让一次瞬时失败**永久跳过**这篇（商品侧详情失败同口径）。
        """
        import backend.app.services.note_sync_service as nss

        db_dependency, db, creator = _build_db(tmp_path)
        try:
            note = _add_note(
                db, creator, HEX_NOTE_ID, content="", raw={"note_url": "https://x/n"}
            )

            class BoomAdapter:
                def __init__(self, cookies):
                    pass

                def get_note_info(self, url):
                    raise RuntimeError("platform down")

            result = _hydrate(db, creator, [note.note_id], BoomAdapter("x"))

            assert note.content == "", "列表数据（空正文）照常落库，不被失败污染"
            assert note.hydrated_at is None, "失败不写标记 ⇒ 下次全量同步会重试"
            assert result["failed"] == 1
        finally:
            db.close()

    def test_no_www_session_makes_no_request(self, tmp_path: Path):
        """账号自己没有 www 会话 ⇒ 编排器传 None 适配器 ⇒ 不构造、不发请求、不写标记。

        兄弟账号手里那份 www 会话**只允许存在、不允许被读**；而且「暂不可用」不是
        「尝试过」—— 用户补登 www 之后这些行还要能被补，所以标记留空。
        """
        import backend.app.services.note_sync_service as nss

        db_dependency, db, creator = _build_db(tmp_path, own_www=False)
        try:
            note = _add_note(db, creator, HEX_NOTE_ID, raw={"note_url": "https://x/n"})

            class SpyAdapter:
                def __init__(self, cookies):
                    raise AssertionError("账号自己没有网页会话 —— 不该构造适配器")

            # 编排器在拿不到 www 会话时传的就是 None（`_sync_with_task` 的口径）。
            result = nss._hydrate_page_notes(
                db, creator, [note.note_id], cookies="", adapter=None, limiter=_AllowLimiter()
            )

            assert result["filled"] == 0 and result["attempted"] == 0
            assert note.content == "" and note.hydrated_at is None
        finally:
            db.close()

    def test_empty_content_is_not_invented(self, tmp_path: Path):
        """详情返回空正文 → 留空，不造假（缺值口径）。"""
        import backend.app.services.note_sync_service as nss

        db_dependency, db, creator = _build_db(tmp_path)
        try:
            note = _add_note(db, creator, HEX_NOTE_ID, raw={"note_url": "https://x/n"})

            class EmptyAdapter:
                def __init__(self, cookies):
                    pass

                def get_note_info(self, url):
                    return True, "ok", _detail_payload(HEX_NOTE_ID, desc="")

            result = _hydrate(db, creator, [note.note_id], EmptyAdapter("x"))

            assert note.content == ""
            # 详情成功但没有任何数据写入 ⇒ 不计入「补齐条数」（不虚报）。
            assert result["filled"] == 0
        finally:
            db.close()

    def test_note_without_detail_url_makes_no_request(self, tmp_path: Path):
        """`note_id` 非平台形态 ⇒ 一次请求都不发，判 `no_url` 并**写标记**。"""
        import backend.app.services.note_sync_service as nss

        db_dependency, db, creator = _build_db(tmp_path)
        try:
            # ⚠️ 必须用**非平台形态**的 id：平台形态 + 无令牌现在是 `no_token`
            # （可自救 ⇒ 不写标记），见下一个用例。
            local_id = "N-LOCAL-1"
            _add_note(db, creator, local_id, raw={})
            calls: list[str] = []

            class SpyAdapter:
                def __init__(self, cookies):
                    pass

                def get_note_info(self, url):
                    calls.append(url)
                    return True, "ok", {}

            result = _hydrate(db, creator, [local_id], SpyAdapter("x"))

            assert calls == []
            # 本地缺详情地址**不会自愈** ⇒ 写标记，否则每轮全量同步都白打一次。
            assert result["no_url"] == 1
            assert result["no_token"] == 0
            note = db.scalars(select(Note).where(Note.note_id == local_id)).one()
            assert note.hydrated_at is not None
        finally:
            db.close()

    def test_note_missing_token_is_blocked_without_marker(self, tmp_path: Path):
        """平台形态 id + 无令牌 ⇒ `no_token`（**用户可自救**）⇒ **不写** `hydrated_at`。

        §19.13.9（2026-09-28 用户口径「没有令牌强行拦截，让用户补齐登录令牌」）：
        缺令牌不是本地数据缺失 —— 补齐网页版登录后这些行还要能被补，必须留 NULL。
        与上一个用例（`no_url` 写标记）刻意相反，两者不能混成一个状态。
        """
        import backend.app.services.note_sync_service as nss

        db_dependency, db, creator = _build_db(tmp_path)
        try:
            _add_note(db, creator, HEX_NOTE_ID, raw={})
            calls: list[str] = []

            class SpyAdapter:
                def __init__(self, cookies):
                    pass

                def get_note_info(self, url):
                    calls.append(url)
                    return True, "ok", {}

            result = _hydrate(db, creator, [HEX_NOTE_ID], SpyAdapter("x"))

            assert calls == [], "缺令牌不硬打（平台对无 token 链接一律 302 → /404）"
            assert result["no_token"] == 1
            assert result["no_url"] == 0
            note = db.scalars(select(Note).where(Note.note_id == HEX_NOTE_ID)).one()
            assert note.hydrated_at is None, "缺令牌可自救 ⇒ 不写标记"
        finally:
            db.close()

    def test_detail_url_carries_the_note_level_token(self, tmp_path: Path):
        """详情 URL 要带上笔记级 `xsec_token`（DEF-053：无 token 时「成功但 0 字」）。"""
        import backend.app.services.note_sync_service as nss

        db_dependency, db, creator = _build_db(tmp_path)
        try:
            _add_note(
                db,
                creator,
                HEX_NOTE_ID,
                raw={
                    "note_url": f"https://www.xiaohongshu.com/explore/{HEX_NOTE_ID}",
                    "xsec_token": "TOKEN-1",
                },
            )
            calls: list[str] = []

            class SpyAdapter:
                def __init__(self, cookies):
                    pass

                def get_note_info(self, url):
                    calls.append(url)
                    return True, "ok", _detail_payload(HEX_NOTE_ID, desc="正文")

            _hydrate(db, creator, [HEX_NOTE_ID], SpyAdapter("x"))

            assert calls == [f"https://www.xiaohongshu.com/explore/{HEX_NOTE_ID}?xsec_token=TOKEN-1"]
        finally:
            db.close()

    def test_already_hydrated_notes_are_never_picked_again(self, tmp_path: Path):
        """补过的行不再进候选 —— 否则每轮全量同步都会重复请求同一批（额度被白烧）。"""
        import backend.app.services.note_sync_service as nss

        db_dependency, db, creator = _build_db(tmp_path)
        try:
            _add_note(
                db, creator, HEX_NOTE_ID, raw={"note_url": "https://x/done"}, hydrated=True
            )
            todo = _add_note(db, creator, HEX_NOTE_ID_2, raw={"note_url": "https://x/todo"})
            calls: list[str] = []

            class SpyAdapter:
                def __init__(self, cookies):
                    pass

                def get_note_info(self, url):
                    calls.append(url)
                    return True, "ok", _detail_payload("x", desc="正文")

            result = _hydrate(db, creator, [HEX_NOTE_ID, todo.note_id], SpyAdapter("x"))

            assert calls == ["https://x/todo"], "已补齐的行不许被重复请求"
            assert result["attempted"] == 1
        finally:
            db.close()

    def test_rate_limit_stops_the_page_and_reports_skipped(self, tmp_path: Path):
        """被限流 → 本页停下，并把「还有几条没补」报出来（不静默 continue）。"""
        import backend.app.services.note_sync_service as nss

        db_dependency, db, creator = _build_db(tmp_path)
        try:
            for suffix, nid in (("a", HEX_NOTE_ID), ("b", HEX_NOTE_ID_2)):
                _add_note(db, creator, nid, raw={"note_url": f"https://x/{suffix}"})
            calls: list[str] = []

            class SpyAdapter:
                def __init__(self, cookies):
                    pass

                def get_note_info(self, url):
                    calls.append(url)
                    return True, "ok", _detail_payload("x", desc="正文")

            result = _hydrate(
                db, creator, [HEX_NOTE_ID, HEX_NOTE_ID_2], SpyAdapter("x"), limiter=_DenyLimiter()
            )

            assert calls == [], "额度用完就不该再发请求"
            assert result["attempted"] == 0
            assert result["skipped_by_limit"] == 2, "被限流挡下的条数要能被上层报给用户"
            assert result["warnings"], "限流截断必须留一行可展示的原因"
        finally:
            db.close()


class TestDetailRefBackfill:
    """本地生成 + 已发布笔记「没有可用的详情地址」的修复（§25.4，2026-09-25 用户报障）。

    真实库 id 1967「中古风桌旗，让家瞬间高级感」：`local_generated` + `published`，
    `raw_json` 只有 `aigc` / `guardrails` / `item_id` —— 平台 `note/publish` 响应
    只回 `{"id"}`，**详情地址从来没被写进去** ⇒ `_note_detail_url` 取不到 URL ⇒
    同步按钮判 `no_url`。用户看到的就是「按钮在、功能不在」。

    三层修复（本类逐层守）：

    1. **发布回写即补地址**：`_execute_publish` 成功段 best-effort 回查千帆商品
       笔记列表（`note_ids` 精确查询），把 `note_url` + `xsec_token` 合入
       `raw_json`（单一实现 `backfill_note_detail_ref`，发布 / 单条同步共用）；
    2. **单条同步先回查再兜底**：`sync_note_from_platform` 打详情前用账号**自己
       的 `ark` 令牌**回查一次（`_backfill_detail_ref_for_account`，§18.14）；
    3. **explore 兜底**：回查不到时 `hydrate_note_detail(allow_tokenless=True)`
       （**仅单条同步路径**）用平台 note_id 拼 explore 地址。批量页内联 / 定时
       同步（`refresh=False`）**不**放开 —— 无 token 是 DEF-053 的「假成功」，
       那两条链路每轮全量重打，放开就白烧额度（第 4 条用例守这条边界）。
    """

    def test_note_detail_url_requires_a_token(self, tmp_path: Path):
        """无令牌 ⇒ 一律返空串（§19.13.9：`allow_tokenless` 兜底已删除）。

        旧实现允许单条同步路径拼一个**无 token** 的 explore 地址；平台对这类链接
        一律 302 → `/404?error_code=300031`（§19.13.8 真机实测，带会话也一样）⇒
        那是 DEF-053「假成功」的入口。现在拿不到令牌就返空串，由调用方拦截。
        """
        import backend.app.services.note_sync_service as nss

        class _Note:
            def __init__(self, note_id: str, raw: dict) -> None:
                self.note_id = note_id
                self.raw_json = raw

        # 平台形态 id 但没令牌 ⇒ 空串（不再拼「看起来能用」的地址）。
        local_generated = _Note(HEX_NOTE_ID, {"aigc": {"x": 1}, "item_id": "ITEM-1"})
        assert nss._note_detail_url(local_generated) == ""
        # 非平台形态 id ⇒ 同样空串（拼出来平台也不认）。
        assert nss._note_detail_url(_Note("N-LOCAL-1", {})) == ""
        # 有 token 的路径不受影响。
        with_token = _Note(
            HEX_NOTE_ID,
            {"note_url": f"https://www.xiaohongshu.com/explore/{HEX_NOTE_ID}", "xsec_token": "T"},
        )
        assert nss._note_detail_url(with_token) == (
            f"https://www.xiaohongshu.com/explore/{HEX_NOTE_ID}?xsec_token=T"
        )

    def test_batch_hydration_blocks_on_missing_token(self, tmp_path: Path):
        """批量 / 定时链路对**缺令牌**的笔记：零请求 + `no_token`，且**不写**标记。

        DEF-053 的防线不能因为修单条同步被顺手拆掉（无 token 的「假成功」在每轮
        全量同步里反复出现）；但也不能把「缺令牌」记成 `no_url` —— 前者用户补齐
        登录就能自愈，后者才是真的没救（写标记出队）。
        """
        import backend.app.services.note_sync_service as nss

        db_dependency, db, creator = _build_db(tmp_path)
        try:
            note = _add_note(db, creator, HEX_NOTE_ID, raw={})
            calls: list[str] = []

            class SpyAdapter:
                def __init__(self, cookies):
                    pass

                def get_note_info(self, url):
                    calls.append(url)
                    return True, "ok", _detail_payload(HEX_NOTE_ID, desc="正文")

            result = nss.hydrate_note_detail(db, creator, note, adapter=SpyAdapter("x"))

            assert calls == []
            assert result["status"] == "no_token"
            assert result["code"] == "NOTE_DETAIL_TOKEN_REQUIRED"
            assert note.hydrated_at is None, "缺令牌可自救 ⇒ 不写标记，补齐登录后重试"
        finally:
            db.close()

    def test_single_sync_blocks_when_token_missing(self, tmp_path: Path, monkeypatch):
        """单条同步：ark 回查也拿不到令牌 ⇒ **拦截**，不翻 `user_posted`、不打详情。

        §19.13.9（2026-09-28 用户口径「移除兜底，不允许出现翻页到底；没有令牌强行
        拦截，让用户补齐登录令牌」）。旧实现在这里拼一个无 token 的 explore 地址
        去打详情 —— 平台一律 302 → `/404`，白烧一次请求（DEF-053 同型）。
        """
        import backend.app.api.platforms.xhs.pc as pc_module
        import backend.app.services.note_sync_service as nss

        db_dependency, db, creator = _build_db(tmp_path)
        try:
            note = _add_note(db, creator, HEX_NOTE_ID, content="旧正文", raw={})
            detail_calls: list[str] = []
            posted_calls: list[str] = []

            class SpyAdapter:
                def __init__(self, cookies):
                    pass

                def get_note_info(self, url):
                    detail_calls.append(url)
                    return True, "ok", _detail_payload(HEX_NOTE_ID, desc="平台最新正文")

                def get_user_posted_page(self, user_id, cursor="", xsec_token="", xsec_source=""):
                    posted_calls.append(cursor)
                    return True, "", {"data": {"notes": [], "has_more": False, "cursor": ""}}

            monkeypatch.setattr(pc_module, "get_xhs_pc_api_adapter_factory", lambda: SpyAdapter)
            monkeypatch.setattr(nss, "get_rate_limiter", lambda: _AllowLimiter())

            result = nss.sync_note_from_platform(db, note)

            assert result["status"] == "no_token", result
            assert result["code"] == "NOTE_DETAIL_TOKEN_REQUIRED"
            assert detail_calls == [], "缺令牌 ⇒ 详情一次都不打"
            assert posted_calls == [], "缺令牌 ⇒ 不翻 user_posted（兜底已移除）"
            assert note.content == "旧正文", "没取到详情就不动本地正文"
        finally:
            db.close()

    def test_backfill_merges_token_into_raw_json(self, tmp_path: Path):
        """`backfill_note_detail_ref`：回查命中 ⇒ 合并 URL + token；不命中 ⇒ 原样不动。"""
        import backend.app.services.note_sync_service as nss

        db_dependency, db, creator = _build_db(tmp_path)
        try:
            note = _add_note(db, creator, HEX_NOTE_ID, raw={"item_id": "ITEM-1"})

            def _fetch(note_ids):
                assert note_ids == [HEX_NOTE_ID]
                return True, "ok", {
                    "data": {"note_infos": [{"note_id": HEX_NOTE_ID, "xsec_token": "XT-1"}]}
                }

            assert nss.backfill_note_detail_ref(db, note, _fetch) is True
            assert note.raw_json["note_url"] == (
                f"https://www.xiaohongshu.com/explore/{HEX_NOTE_ID}?xsec_token=XT-1"
            )
            assert note.raw_json["xsec_token"] == "XT-1"
            assert note.raw_json["item_id"] == "ITEM-1", "既有字段不被抹掉"

            # 已有地址 ⇒ 直接返回 False，不覆盖（只增不写）。
            assert nss.backfill_note_detail_ref(db, note, _fetch) is False
            assert note.raw_json["note_url"].endswith("xsec_token=XT-1")

            # 平台没有这一项 / 没给 token / 请求炸了 ⇒ False 且不炸（best-effort）。
            miss = _add_note(db, creator, HEX_NOTE_ID_2, raw={})
            assert nss.backfill_note_detail_ref(
                db, miss, lambda ids: (True, "ok", {"data": {"note_infos": []}})
            ) is False
            assert nss.backfill_note_detail_ref(
                db, miss, lambda ids: (True, "ok", {"data": {"note_infos": [
                    {"note_id": HEX_NOTE_ID_2}]}})
            ) is False

            def _boom(ids):
                raise RuntimeError("平台超时（测试）")

            assert nss.backfill_note_detail_ref(db, miss, _boom) is False
            assert miss.raw_json == {}
        finally:
            db.close()

    def test_account_backfill_uses_the_accounts_own_ark_token(self, tmp_path: Path, monkeypatch):
        """单条同步前的回查用**账号自己的 ark 令牌**（§18.14），合入后要提交。"""
        import backend.app.services.note_sync_service as nss
        from tests.backend.cookie_fixtures import give_ark_cookies

        db_dependency, db, creator = _build_db(tmp_path)
        try:
            give_ark_cookies(db, creator)
            db.commit()
            note = _add_note(db, creator, HEX_NOTE_ID, raw={})
            seen: list[list[str]] = []

            class FakeQianfanAdapter:
                def __init__(self, cookies: str) -> None:
                    self.cookies = cookies

                def get_goods_note_page(self, **kwargs):
                    seen.append(list(kwargs.get("note_ids") or []))
                    return True, "ok", {
                        "data": {"note_infos": [{"note_id": HEX_NOTE_ID, "xsec_token": "XT-9"}]}
                    }

            import backend.app.api.platforms.xhs.qianfan as qianfan_module

            monkeypatch.setattr(
                qianfan_module, "get_qianfan_api_adapter_factory", lambda: FakeQianfanAdapter
            )

            assert nss._backfill_detail_ref_for_account(db, creator, note) is True
            assert seen == [[HEX_NOTE_ID]]
            assert note.raw_json["note_url"].endswith("xsec_token=XT-9")

            # 提交过：新 session 里也读得到（证明不是只改内存）。
            db.close()
            db = next(db_dependency())
            reloaded = db.scalars(select(Note).where(Note.id == note.id)).one()
            assert reloaded.raw_json["xsec_token"] == "XT-9"
        finally:
            db.close()

    def test_account_backfill_skips_without_ark_token(self, tmp_path: Path):
        """没有 ark 令牌 ⇒ 不回查、不借凭据（调用方有 explore 兜底）。"""
        import backend.app.services.note_sync_service as nss

        db_dependency, db, creator = _build_db(tmp_path)
        try:
            note = _add_note(db, creator, HEX_NOTE_ID, raw={})
            assert nss._backfill_detail_ref_for_account(db, creator, note) is False
            assert note.raw_json == {}
        finally:
            db.close()

    def test_batch_backfill_fills_every_note_in_one_request(self, tmp_path: Path):
        """批量版：一次 `note_ids` 查询补全整批（DEF-069 提速；一篇触发、全批受益）。"""
        import backend.app.services.note_sync_service as nss

        db_dependency, db, creator = _build_db(tmp_path)
        try:
            notes = [
                _add_note(db, creator, HEX_NOTE_ID, raw={"item_id": "ITEM-1"}),
                _add_note(db, creator, HEX_NOTE_ID_2, raw={"item_id": "ITEM-2"}),
            ]
            # 已有地址的那篇不碰（只增不写）。
            done = _add_note(
                db, creator, "6a7d14b20000000028001cba", raw={"note_url": "https://x/done"}
            )
            seen: list[list[str]] = []

            def _fetch(note_ids):
                seen.append(list(note_ids))
                return True, "ok", {
                    "data": {
                        "note_infos": [
                            {"note_id": HEX_NOTE_ID, "xsec_token": "XT-1"},
                            {"note_id": HEX_NOTE_ID_2, "xsec_token": "XT-2"},
                        ]
                    }
                }

            filled = nss.backfill_note_detail_refs(db, [*notes, done], _fetch)

            assert filled == 2
            assert len(seen) == 1, "整批一次请求（不是每篇各一次）"
            assert sorted(seen[0]) == sorted([HEX_NOTE_ID, HEX_NOTE_ID_2])
            assert notes[0].raw_json["note_url"].endswith("xsec_token=XT-1")
            assert notes[1].raw_json["note_url"].endswith("xsec_token=XT-2")
            assert done.raw_json["note_url"] == "https://x/done", "已有地址不被覆盖"
        finally:
            db.close()


def _session(tmp_path: Path, name: str) -> Any:
    engine = create_engine(
        f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    # ⚠️ 全局 FK=ON 后必须播种父行（同 `_override_database`，2026-10-04）。
    seed_owner_rows(engine, user_id=1, account_id=1)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


class _FakeDetailAdapter:
    """详情替身：把「平台返回什么」变成用例的可控输入（不打真网络）。"""

    calls: list[str] = []
    payload: dict[str, Any] | None = None
    success: bool = True

    def __init__(self, cookies):
        pass

    def get_note_info(self, url):
        type(self).calls.append(url)
        payload = type(self).payload
        if payload is None:
            payload = _detail_payload(HEX_NOTE_ID, desc="补齐到的正文", images=["https://img/1"])
        return type(self).success, "ok" if type(self).success else "详情失败", payload


class TestLibraryDetailEndpoint:
    """内容库详情端点：**来源账号**可见 + `hydrate=true` 按需补齐（设计 13 §19.13）。"""

    def _env(self, tmp_path: Path, *, with_www: bool = True) -> Any:
        from fastapi.testclient import TestClient

        from backend.app.main import app

        SessionLocal = _session(tmp_path, "api-hydrate.db")

        def override_get_db() -> Any:
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        client = TestClient(app)
        registered = client.post(
            "/api/auth/register", json={"username": "lib-owner", "password": "secret123"}
        )
        assert registered.status_code == 200, registered.text
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

        db = SessionLocal()
        user = db.scalars(select(User).where(User.username == "lib-owner")).one()
        account = PlatformAccount(
            user_id=user.id, platform="xhs", sub_type="creator",
            nickname=GUANYU_NICKNAME, profile_json=GUANYU_PROFILE, status="active",
        )
        db.add(account)
        db.flush()
        channels = {CHANNEL_CREATOR: '{"a1":"creator-a1"}'}
        if with_www:
            channels[CHANNEL_WWW] = OWN_WWW_JAR
        db.add(cookie_version_multi(account.id, channels))
        note = Note(
            platform_account_id=account.id,
            user_id=user.id,
            platform="xhs",
            note_id=HEX_NOTE_ID,
            title="标题",
            content="",
            raw_json={"note_url": "https://x/n", "xsec_token": "TOK"},
        )
        db.add(note)
        db.commit()
        note_id, account_id = note.id, account.id
        db.close()
        get_rate_limiter().reset(account_id)  # 全局单例：每个用例从干净窗口起
        return client, headers, note_id, account_id

    def _cleanup(self) -> None:
        from backend.app.main import app

        app.dependency_overrides.pop(get_db, None)

    def test_detail_reports_the_source_account(self, tmp_path: Path):
        """**来源账号**必须回传：「已发布」是账号维度的事实，界面不给账号就无从核对。"""
        client, headers, note_id, _ = self._env(tmp_path)
        try:
            response = client.get(f"/api/notes/{note_id}", headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["account_name"] == GUANYU_NICKNAME
        finally:
            self._cleanup()

    def test_detail_is_local_only_and_never_touches_the_platform(self, tmp_path: Path, monkeypatch: Any):
        """**内容库详情只读本地库，零平台请求**（2026-09-22 用户口径）。

        取代旧契约 `test_detail_hydrates_on_demand`（原句保留）：
        ~~`hydrate=true` ⇒ 当场补齐：正文写回本地、响应里带补齐状态。~~

        **为什么删掉那条路径**：它让「打开一个只读页面」变成一次平台写库操作，
        `db.commit()` 会与正在跑的同步任务抢 SQLite 写锁 ⇒ `database is locked`
        （实测 0.00s 失败）且未捕获 ⇒ 用户看到「内容库笔记详情 500」。
        内容库的定位是**本地台账**，不该有平台副作用。

        这里用「构造适配器即炸」的 Spy 钉死：即使调用方还传 `?hydrate=true`
        （老前端 / 缓存页面），后端也**一个平台请求都不发**。
        """
        class SpyAdapter:
            def __init__(self, cookies):
                raise AssertionError("内容库详情不该构造平台适配器")

        monkeypatch.setattr(
            "backend.app.api.platforms.xhs.pc.get_xhs_pc_api_adapter_factory", lambda: SpyAdapter
        )
        client, headers, note_id, _ = self._env(tmp_path)
        try:
            # 刻意带 `?hydrate=true`：参数已被移除，必须被忽略而不是报错。
            response = client.get(f"/api/notes/{note_id}", params={"hydrate": "true"}, headers=headers)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["content"] == "", "本地没正文就照实回空，不打平台补"
            assert "hydrate" not in body, "hydrate 字段已移除，不该再出现"
            assert body["account_name"] == GUANYU_NICKNAME
        finally:
            self._cleanup()

    def test_detail_has_no_hydrate_parameter_in_the_openapi_contract(self, tmp_path: Path):
        """OpenAPI 契约层也**不再有** `hydrate` 查询参数（前后端同源）。"""
        from backend.app.main import app

        params = app.openapi()["paths"]["/api/notes/{note_id}"]["get"].get("parameters", [])
        names = {p.get("name") for p in params if isinstance(p, dict)}
        assert "hydrate" not in names, f"hydrate 参数应已移除，实际还有：{sorted(names)}"

    def test_detail_without_hydrate_never_touches_the_platform(self, tmp_path: Path, monkeypatch: Any):
        """**任何**详情请求都零平台请求（`hydrate` 参数已移除，这条守卫升级为无条件）。"""
        class SpyAdapter:
            def __init__(self, cookies):
                raise AssertionError("内容库详情不该构造平台适配器")

        monkeypatch.setattr(
            "backend.app.api.platforms.xhs.pc.get_xhs_pc_api_adapter_factory", lambda: SpyAdapter
        )
        client, headers, note_id, _ = self._env(tmp_path)
        try:
            response = client.get(f"/api/notes/{note_id}", headers=headers)
            assert response.status_code == 200, response.text
            assert "hydrate" not in response.json()
        finally:
            self._cleanup()


class TestExploreSsrDetailRoute:
    """§19.13.8（2026-09-28）：详情改走 explore SSR，不必再为每篇翻 `user_posted` 全表。

    旧契约（§19.13.6 ⑤）是「每篇详情前先用 www 会话翻 `user_posted` 拿网页侧令牌」
    —— 1951 条 ÷ 30/页 ≈ 66 页、页间停顿 5s × 抖动 ⇒ **5–8 分钟/篇**，批量 N 篇 =
    N 次全表扫描。真机实测（真实窗口 Chrome + CDP，探针
    `.workbuddy/scripts/probe_note_detail_by_id.py`）证明：**explore 页面的 SSR 里
    就带着详情**，一次 GET 拿全，且**不挑令牌来源**（千帆 / 创作者令牌同样成功）。

    本类守三层契约：

    ① **取数路由**：SSR 优先（`get_note_info_html`），拿到就不打 `/v1/feed`；
       SSR 取不到 ⇒ 回退 JSON 端点，行为与改造前逐字一致；
    ② **单条同步零翻页**：本地已有令牌 ⇒ 一次 `user_posted` 都不发（旧实现每篇
       66–200 页）；
    ③ **兜底已整段移除**（§19.13.9，2026-09-28 用户口径「移除兜底，不允许出现翻页
       到底；没有令牌强行拦截，让用户补齐登录令牌」）：源码里不许再出现任何
       「翻 `user_posted` 拿详情令牌」的实现，缺令牌一律结构化拦截。
    """

    def test_ssr_is_preferred_and_json_feed_is_not_called(self, tmp_path: Path):
        """本地有令牌 ⇒ 走 explore SSR，`/v1/feed` **一次都不打**。"""
        import backend.app.services.note_sync_service as nss

        _db_dependency, db, creator = _build_db(tmp_path)
        try:
            note = _add_note(
                db,
                creator,
                HEX_NOTE_ID,
                content="",
                raw={
                    "note_url": f"https://www.xiaohongshu.com/explore/{HEX_NOTE_ID}?xsec_token=TK",
                    "xsec_token": "TK",
                },
            )
            ssr_calls: list[str] = []
            feed_calls: list[str] = []

            class SsrAdapter:
                def __init__(self, cookies: str) -> None:
                    pass

                def get_note_info_html(self, url: str):
                    ssr_calls.append(url)
                    return True, "成功", _detail_payload(HEX_NOTE_ID, desc="SSR 正文")

                def get_note_info(self, url: str):
                    feed_calls.append(url)
                    return True, "ok", _detail_payload(HEX_NOTE_ID, desc="JSON 正文")

            result = nss.hydrate_note_detail(db, creator, note, adapter=SsrAdapter("x"))

            assert result["status"] == "ok", result
            assert len(ssr_calls) == 1, "详情必须走 explore SSR"
            assert feed_calls == [], "SSR 已拿到 ⇒ 不该再打 /v1/feed"
            assert note.content == "SSR 正文"
        finally:
            db.close()

    def test_ssr_failure_falls_back_to_json_feed(self, tmp_path: Path):
        """SSR 取不到（结构变更 / 风控页）⇒ 回退 `/v1/feed`，不判死。"""
        import backend.app.services.note_sync_service as nss

        _db_dependency, db, creator = _build_db(tmp_path)
        try:
            note = _add_note(
                db,
                creator,
                HEX_NOTE_ID,
                raw={
                    "note_url": f"https://www.xiaohongshu.com/explore/{HEX_NOTE_ID}?xsec_token=TK",
                    "xsec_token": "TK",
                },
            )
            ssr_calls: list[str] = []
            feed_calls: list[str] = []

            class SsrFailingAdapter:
                def __init__(self, cookies: str) -> None:
                    pass

                def get_note_info_html(self, url: str):
                    ssr_calls.append(url)
                    return False, "explore 页面未包含笔记数据（SSR 结构变更或该笔记不可见）", None

                def get_note_info(self, url: str):
                    feed_calls.append(url)
                    return True, "ok", _detail_payload(HEX_NOTE_ID, desc="JSON 正文")

            result = nss.hydrate_note_detail(db, creator, note, adapter=SsrFailingAdapter("x"))

            assert result["status"] == "ok", result
            assert len(ssr_calls) == 1 and len(feed_calls) == 1, "SSR 失败必须回退 JSON 端点"
            assert note.content == "JSON 正文"
        finally:
            db.close()

    def test_single_sync_with_local_token_never_lists_user_posted(
        self, tmp_path: Path, monkeypatch: Any
    ):
        """**核心回归**：本地已有令牌 ⇒ 单条同步一次 `user_posted` 翻页都不发。

        改造前这里每篇都要翻 66–200 页（5–8 分钟）；本用例就是那件事的守卫 ——
        一旦有人把「打详情前必刷令牌」加回来，`listed` 立刻非空。
        """
        import backend.app.services.note_sync_service as nss

        _db_dependency, db, creator = _build_db(tmp_path)
        try:
            note = _add_note(
                db,
                creator,
                HEX_NOTE_ID,
                content="旧正文",
                raw={
                    "note_url": f"https://www.xiaohongshu.com/explore/{HEX_NOTE_ID}?xsec_token=TK",
                    "xsec_token": "TK",
                },
            )
            listed: list[str] = []
            ssr_calls: list[str] = []

            class Adapter:
                def __init__(self, cookies: str) -> None:
                    pass

                def get_self_profile(self):
                    # ⚠️ 必须让身份查询**成功**：否则 `refresh_www_detail_tokens` 会在
                    # 取身份那步就退出（`get_self_profile` 抛 AttributeError ⇒ 记 warning
                    # 返回），`get_user_posted_page` 一次都不会被调到 ⇒ 「有没有翻全表」
                    # 这件事就测不出来（变异验证实测：漏了这个方法，本用例对
                    # 「本地有令牌仍刷新」的变异**不敏感**）。
                    return True, "", {"data": {"user_id": "SELF-1"}}

                def get_user_posted_page(
                    self, user_id: str, cursor: str = "", xsec_token: str = "", xsec_source: str = ""
                ):
                    listed.append(cursor)
                    # ⚠️ `has_more` **必须有界**（2026-09-28 实测踩坑）：本用例的语义是
                    # 「一次都不该翻」（正常路径 `listed` 恒空），但变异验证会把那条判断
                    # 摘掉 —— 那时若 `has_more` 恒真，翻页循环会跑到
                    # `WWW_LIST_PAGE_CAP = 200` 页、**每页 `_sleep_between_pages()` 真停
                    # 5 秒** ⇒ 用例挂起 1000 秒被 SIGTERM，变异脚本的 `finally` 来不及
                    # 还原源文件（源文件会留在变异态，后续几轮全跑在坏文件上）。
                    # 有界 + 用例内 `PAGE_GAP_SECONDS=0` ⇒ 变异时也在 3 页内快速变红。
                    return True, "", {
                        "data": {
                            "notes": [],
                            "has_more": len(listed) < 3,
                            "cursor": f"C-{len(listed)}",
                        }
                    }

                def get_note_info_html(self, url: str):
                    ssr_calls.append(url)
                    return True, "成功", _detail_payload(HEX_NOTE_ID, desc="SSR 正文")

                def get_note_info(self, url: str):
                    return True, "ok", _detail_payload(HEX_NOTE_ID, desc="JSON 正文")

            monkeypatch.setattr(nss, "get_rate_limiter", lambda: _AllowLimiter())
            # 页间停顿归零：变异验证会把「本地有令牌就不刷新」摘掉，那时若还按 5 秒/页
            # 真停顿，用例会挂到超时（见替身里 `has_more` 的说明）。
            monkeypatch.setattr(nss, "PAGE_GAP_SECONDS", 0.0)
            monkeypatch.setattr(
                "backend.app.api.platforms.xhs.pc.get_xhs_pc_api_adapter_factory", lambda: Adapter
            )

            result = nss.sync_note_from_platform(db, note)

            assert result["status"] == "ok", result
            assert listed == [], "本地有令牌时不该翻 user_posted（旧实现每篇翻 66–200 页）"
            assert len(ssr_calls) == 1, "详情仍要走 explore SSR"
            assert note.content == "SSR 正文"
        finally:
            db.close()

    def test_no_user_posted_token_sweep_exists_anywhere(self):
        """**兜底已整段移除**（§19.13.9，2026-09-28 用户口径）。

        守卫形态从 §19.13.8 的「每个调用点都要传 `only_note_ids`」升级为**更强的一条**：
        源码里连这个函数都不许再出现 —— 它存在的唯一意义就是「翻 `user_posted` 全表
        拿详情令牌」，而那条路已被判定为不可接受（「不允许出现翻页到底」）。
        行为用例只覆盖已知入口，**新写的实现**只有这条结构守卫挡得住。
        """
        import inspect

        import backend.app.services.note_sync_service as nss

        source = inspect.getsource(nss)
        assert "refresh_www_detail_tokens" not in source, (
            "兜底刷新已移除：不许再引入任何「翻 user_posted 拿详情令牌」的实现"
        )
        assert "WWW_LIST_PAGE_CAP" not in source, "该常量只服务于已删除的兜底"
        assert "WWW_DETAIL_TOKEN_FLAG" not in source

    def test_full_list_sdk_calls_stay_on_the_registered_whitelist(self):
        """全量循环 SDK 方法只允许**已登记的**三处调用；新增即红（C5 / §19.13.9）。

        本次改动的实质是「删掉一条为拿令牌而翻到底的兜底」⇒ 同类逻辑必须能被拦住。
        扫描范围 = `backend/app/adapters/xhs/*.py` 里**每个方法**实际调用的 SDK 全量
        方法名（AST，不看注释，注释提一句不算）。

        现状（2026-09-28 全项目排查 + 处置）**两处**，各有理由：
          * `get_user_notes` → 数据抓取 / 监控巡检（DEF-033 已登记）；
          * `get_note_comments` → 评论抓取（用户主动、按需）。

        ⚠️ 原第三处 `get_published_notes`（`GET /api/xhs/creator/published`，一期遗留、
        无 UI 使用）**已删除**：它与 `POST /api/xhs/pc/users/notes` 数据 100% 重复，且是
        当时唯一还在「翻到底」的读端点（设计 13 §19.13.9）。

        ⚠️ **不许新增**。真需要翻全量时，先看有没有单页方法 + 外层刹车（限流
        `wait_allow` + `ABSOLUTE_PAGE_CAP`），像笔记 / 商品同步那样。
        """
        import ast
        from pathlib import Path

        full_list = {
            "get_user_all_notes",
            "get_user_all_like_note_info",
            "get_user_all_collect_note_info",
            "get_all_posted_notes",
            "get_all_publish_note_info",
            "get_note_all_comment",
            "get_all_likesAndcollects",
            "get_all_metions",
            "get_all_new_connections",
        }
        allowed = {"get_user_notes", "get_note_comments"}

        adapters_dir = Path(__file__).resolve().parents[2] / "backend/app/adapters/xhs"
        offenders: set[str] = set()
        for path in sorted(adapters_dir.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
                called = {
                    call.func.attr
                    for call in ast.walk(fn)
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                }
                if called & full_list:
                    offenders.add(fn.name)

        assert offenders == allowed, (
            f"全量循环 SDK 调用集变了 —— 新增 {sorted(offenders - allowed)}、"
            f"消失 {sorted(allowed - offenders)}。新增必须先论证刹车在哪（C5）；"
            "消失请一并更新白名单。"
        )

    def test_pending_without_token_count_reports_only_the_blocked_ones(self, tmp_path: Path):
        """缺令牌计数只收「待补齐 **且** 无令牌」的篇（用于如实报数，不触发任何刷新）。"""
        import backend.app.services.note_sync_service as nss

        _db_dependency, db, creator = _build_db(tmp_path)
        try:
            _add_note(db, creator, HEX_NOTE_ID, raw={"xsec_token": "TK"})
            _add_note(db, creator, HEX_NOTE_ID_2, raw={})
            # 无令牌但**已补齐**（hydrated_at 非空）⇒ 不进候选。
            _add_note(db, creator, "6a7d14000000000000000001", raw={}, hydrated=True)

            assert nss._pending_hydration_without_token_count(db, creator) == 1
        finally:
            db.close()

    def test_explore_html_parser_maps_initial_state_into_feed_shape(self):
        """SSR HTML ⇒ 与 `/v1/feed` **同形**的 payload（归一化层原样复用，不写第二份）。"""
        from backend.app.adapters.xhs.note_detail_ssr import note_detail_payload_from_html

        state = {
            "note": {
                "noteDetailMap": {
                    "abc": {
                        "note": {
                            "noteId": "abc",
                            "title": "T",
                            "desc": "D",
                            "type": "normal",
                            "time": 1700000000,
                            "imageList": [
                                {"urlDefault": "https://img.example/a.jpg", "width": 1, "height": 2}
                            ],
                            "tagList": [{"name": "话题", "type": "topic"}],
                            "interactInfo": {"likedCount": "3", "collectedCount": "4"},
                            "user": {
                                "userId": "U",
                                "nickname": "N",
                                "avatar": "https://img.example/av.jpg",
                            },
                        }
                    }
                }
            }
        }
        html = "<html><body><script>window.__INITIAL_STATE__ = " + json.dumps(state) + "</script>"

        payload = note_detail_payload_from_html(html)

        assert payload is not None
        assert payload["success"] is True and payload["code"] == 0
        card = payload["data"]["items"][0]["note_card"]
        assert card["note_id"] == "abc"
        assert card["title"] == "T"
        assert card["desc"] == "D"
        assert card["image_list"][0]["url_default"] == "https://img.example/a.jpg"
        assert card["tag_list"] == [{"name": "话题", "type": "topic"}]
        assert card["interact_info"]["liked_count"] == "3"
        assert card["user"]["user_id"] == "U"
        # 结构不符 / 没有详情 ⇒ None（不猜、不造，交给调用方回退 JSON 端点）。
        assert note_detail_payload_from_html("<html><body>风控页</body></html>") is None
        assert note_detail_payload_from_html(
            '<script>window.__INITIAL_STATE__ = {"note":{}}</script>'
        ) is None

