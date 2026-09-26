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

from backend.app.core.database import Base, get_db  # noqa: E402
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
    sibling = PlatformAccount(
        user_id=user.id + 1000, platform="xhs", sub_type="pc",
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
        """本地没有可用详情地址 → 一次请求都不发（没有读取目标就别硬上）。"""
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

            assert calls == []
            # 本地缺详情地址**不会自愈** ⇒ 写标记，否则每轮全量同步都白打一次。
            assert result["no_url"] == 1
            note = db.scalars(select(Note).where(Note.note_id == HEX_NOTE_ID)).one()
            assert note.hydrated_at is not None
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

    def test_note_detail_url_tokenless_branch_is_gated(self, tmp_path: Path):
        """兜底拼 explore 只对「hex 平台 ID + 显式允许」放开，其余一律不拼。"""
        import backend.app.services.note_sync_service as nss

        class _Note:
            def __init__(self, note_id: str, raw: dict) -> None:
                self.note_id = note_id
                self.raw_json = raw

        local_generated = _Note(HEX_NOTE_ID, {"aigc": {"x": 1}, "item_id": "ITEM-1"})
        assert nss._note_detail_url(local_generated) == ""
        assert nss._note_detail_url(local_generated, allow_tokenless=True) == (
            f"https://www.xiaohongshu.com/explore/{HEX_NOTE_ID}"
        )
        # 非平台形态的 id（本地临时 id）⇒ 再允许也不拼（拼出来平台也不认）。
        local_temp = _Note("N-LOCAL-1", {})
        assert nss._note_detail_url(local_temp, allow_tokenless=True) == ""
        # 有 token 的老路径不受影响（DEF-053 防线原样）。
        with_token = _Note(
            HEX_NOTE_ID,
            {"note_url": f"https://www.xiaohongshu.com/explore/{HEX_NOTE_ID}", "xsec_token": "T"},
        )
        assert nss._note_detail_url(with_token) == (
            f"https://www.xiaohongshu.com/explore/{HEX_NOTE_ID}?xsec_token=T"
        )

    def test_batch_hydration_keeps_the_no_url_guard(self, tmp_path: Path):
        """批量 / 定时链路（`refresh=False`）对无地址笔记仍然**零请求 + no_url**。

        DEF-053 的防线不能因为修单条同步被顺手拆掉：无 token 的「假成功」在
        每轮全量同步里反复出现，比一条笔记同步不了贵得多。
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
            assert result["status"] == "no_url"
            assert note.hydrated_at is not None, "no_url 不会自愈 ⇒ 写标记出队"
        finally:
            db.close()

    def test_single_sync_falls_back_to_explore_url(self, tmp_path: Path, monkeypatch):
        """单条同步（refresh=True）对无地址笔记：回查不到 token 也能打详情。"""
        import backend.app.api.platforms.xhs.pc as pc_module
        import backend.app.services.note_sync_service as nss

        db_dependency, db, creator = _build_db(tmp_path)
        try:
            note = _add_note(db, creator, HEX_NOTE_ID, content="旧正文", raw={})
            calls: list[str] = []

            class SpyAdapter:
                def __init__(self, cookies):
                    pass

                def get_note_info(self, url):
                    calls.append(url)
                    return True, "ok", _detail_payload(HEX_NOTE_ID, desc="平台最新正文")

            monkeypatch.setattr(pc_module, "get_xhs_pc_api_adapter_factory", lambda: SpyAdapter)
            monkeypatch.setattr(nss, "get_rate_limiter", lambda: _AllowLimiter())

            result = nss.sync_note_from_platform(db, note)

            assert result["status"] == "ok", result
            # 兜底 URL 就是 explore/{平台 id}（无 token）；不再判 no_url。
            assert calls == [f"https://www.xiaohongshu.com/explore/{HEX_NOTE_ID}"]
            # 刷新语义：详情有非空正文 ⇒ 覆盖本地（§25.3）。
            assert note.content == "平台最新正文"
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
