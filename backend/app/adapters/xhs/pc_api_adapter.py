from __future__ import annotations

from typing import Any

from backend.app.adapters.xhs.request_env import direct_xhs_request_env


class XhsPcApiAdapter:
    def __init__(self, cookies: str, user_id: str = "") -> None:
        self.cookies = cookies
        #: 落库的 `PlatformAccount.external_user_id`（**平台**用户 id）。非空时直接
        #: 注入 Auth ⇒ `XHS_Apis._request_params` 里「要 xy-direction 但没有 user_id
        #: 就先 bootstrap」那条分支不再触发 —— 少一次 `/v2/user/me`（而那次请求本身
        #: 要先取 DS 签名材料，是整条链路里最脆的一环：实测 CDN 抖动 8 秒超时就把
        #: 整篇同步判死）。
        self.user_id = str(user_id or "")

    def _api(self):
        from apis.xhs_pc_apis import XHS_Apis
        from xhs_utils.xhs_pc import XHSPcAuth
        from xhs_utils.xhs_pc.http import PcHttpClient

        from backend.app.adapters.xhs.b1_cache import PLATFORM_WWW, cached_b1
        from backend.app.adapters.xhs.http_session import SESSION_WWW, shared_client

        # 进程级共享会话（设计 13 §18.18.8）：实测冷连接 157–213 ms、复用连接 40–48 ms。
        # curl_cffi 的 Session 官方声明线程安全且连接池按线程隔离 ⇒ 跨线程共享是安全的。
        auth = XHSPcAuth.from_cookie(
            self.cookies,
            user_id=self.user_id,
            http_client=shared_client(SESSION_WWW, PcHttpClient),
        )
        # b1 跨请求复用（设计 13 §18.18.7）：SDK 的 `_named_b1_values` 挂在**设备档案
        # 实例**上，而这里每次都新建档案 ⇒ 那个缓存跨请求永远命中不了，于是每个请求
        # 都要起一次 node（实测冷启动 480–530 ms，b1 单次约 145 ms）。
        auth.profile.fixed_b1 = cached_b1(PLATFORM_WWW, self.cookies, auth.profile)
        return XHS_Apis(auth)

    def search_note(
        self,
        keyword: str,
        page: int = 1,
        sort_type_choice: int = 0,
        note_type: int = 0,
        note_time: int = 0,
        note_range: int = 0,
        pos_distance: int = 0,
        geo: str = "",
    ) -> Any:
        with direct_xhs_request_env():
            return self._api().search_note(
                query=keyword,
                page=page,
                sort_type_choice=sort_type_choice,
                note_type=note_type,
                note_time=note_time,
                note_range=note_range,
                pos_distance=pos_distance,
                geo=geo,
            )

    def get_note_info(self, url: str) -> Any:
        with direct_xhs_request_env():
            return self._api().get_note_info(url=url)

    def search_user(self, keyword: str, page: int = 1) -> Any:
        """搜索用户（T-107：`POST /search/users` 的真实现，单页 + 限流插槽）。

        ⚠️ 单页 by design（同 `search_note`）：翻页由调用方决定，不在这里 `while True`。
        """
        from backend.app.core.rate_limiter import get_rate_limiter

        get_rate_limiter().wait_allow(f"search_user:{self.user_id or 'anon'}")
        with direct_xhs_request_env():
            return self._api().search_user(query=keyword, page=page)

    def get_homefeed_channels(self) -> Any:
        """主页频道列表（T-107：`GET /homefeed/channels` 的真实现）。"""
        from backend.app.core.rate_limiter import get_rate_limiter

        get_rate_limiter().wait_allow(f"homefeed:{self.user_id or 'anon'}")
        with direct_xhs_request_env():
            return self._api().get_homefeed_all_channel()

    def get_homefeed_recommend(
        self,
        category: str,
        cursor_score: str = "",
        refresh_type: int = 1,
        note_index: int = 0,
        *,
        num: int = 20,
    ) -> Any:
        """主页推荐（T-107：`POST /homefeed/recommend` 的真实现，**单次请求**）。

        ⚠️ 用单次的 `get_homefeed_recommend`，**不用** `get_homefeed_recommend_by_num`
        —— 后者按 `require_num` 循环请求，属硬约束 C5 禁止的无插槽全量方法。
        调用方要更多内容自己带 `cursor_score` 再来一次。
        """
        from backend.app.core.rate_limiter import get_rate_limiter

        get_rate_limiter().wait_allow(f"homefeed:{self.user_id or 'anon'}")
        with direct_xhs_request_env():
            return self._api().get_homefeed_recommend(
                category=category,
                cursor_score=cursor_score,
                refresh_type=refresh_type,
                note_index=note_index,
                num=num,
            )

    def get_note_info_html(self, url: str) -> Any:
        """详情走 explore 页面 SSR（设计 13 §19.13.8）：**一次 GET 拿全**，不挑令牌来源。

        与 :meth:`get_note_info` 返回**同形**三元组 `(success, message, payload)`，
        调用方（`hydrate_note_detail`）可以两条路互换：SSR 拿不到就回退到 JSON 端点。

        ⚠️ **不用本适配器持有的 Cookie**（`PcHttpClient` 是 `discard_cookies=True`）——
        真机实测匿名即可拿到 SSR 详情，少一处会话依赖。
        """
        from backend.app.adapters.xhs.note_detail_ssr import fetch_note_detail_payload

        return fetch_note_detail_payload(url)

    def get_note_comments(self, note_url: str) -> Any:
        """笔记评论（一级 + 每条的子评论）：**逐页翻完**，每页都过一次限流器。

        ⚠️ **2026-10-04（DEF-033 同类修复）**：原实现直接调 SDK 的
        `get_note_all_comment` —— 它内部是 `while True` 一级评论 + **对每条一级评论
        再 `while True` 子评论**（嵌套无插槽），硬约束 C5 明文禁止。现在改为本层自建：
        每个请求（一级页 / 每条子评论页）前都 `wait_allow()`，超限**等待**不停止。

        返回 `(success, message, out_comment_list)`，其中每条一级评论带 `sub_comments`
        （与 SDK 原形状一致，调用方零改动）。
        """
        import urllib.parse

        from backend.app.core.rate_limiter import get_rate_limiter

        parsed = urllib.parse.urlparse(note_url or "")
        note_id = parsed.path.split("/")[-1]
        query = urllib.parse.parse_qs(parsed.query)
        xsec_token = (query.get("xsec_token") or [""])[0]

        limiter = get_rate_limiter()
        out_comments: list[Any] = []
        with direct_xhs_request_env():
            api = self._api()
            cursor = ""
            while True:
                limiter.wait_allow(note_id or "unknown")
                success, message, raw = api.get_note_out_comment(note_id, cursor, xsec_token)
                if not success:
                    return False, message, out_comments
                data = (raw or {}).get("data") or {}
                page_comments = data.get("comments") or []
                for comment in page_comments:
                    # 子评论逐页（原 SDK 同样是嵌套循环，这里是它的**带刹车**等价物）
                    sub_cursor = ""
                    subs: list[Any] = []
                    while True:
                        limiter.wait_allow(note_id or "unknown")
                        sub_ok, _sub_msg, sub_raw = api.get_note_inner_comment(
                            comment, sub_cursor, xsec_token
                        )
                        if not sub_ok:
                            break
                        sub_data = (sub_raw or {}).get("data") or {}
                        page_subs = sub_data.get("comments") or []
                        subs.extend(page_subs)
                        if not sub_data.get("has_more") or not page_subs:
                            break
                        sub_cursor = str(sub_data.get("cursor") or "")
                        if not sub_cursor:
                            break
                    comment["sub_comments"] = subs
                out_comments.extend(page_comments)
                if not data.get("has_more") or not page_comments:
                    break
                cursor = str(data.get("cursor") or "")
                if not cursor:
                    break
        return True, "", out_comments

    def get_user_notes(self, user_url: str) -> Any:
        """用户主页笔记：**逐页翻到没有下一页为止**，每页都过一次限流器。

        ⚠️ **2026-10-04（DEF-033 / T-107 同批）**：原实现直接调 SDK 的
        `get_user_all_notes` —— 那个方法内部 `while True` **没有分页/限流插槽**
        （硬约束 C5 明文禁止：调用即失控，中间没有刹车点）。现在改为**本层自建翻页**：
        每页前 `wait_allow()`（超限**等待**而不是停止），直到 `has_more` 为 false。

        ⚠️ 与 `get_user_posted_page`（单页，供「我的笔记」用）的区别：那个是**用户点
        「加载更多」驱动**，这个是**一次性把该用户的主页翻完**（抓取/监控链路要全量）。
        两者共用底层单页方法 `get_user_note_info`，**各有各的刹车点**。
        """
        import urllib.parse

        from backend.app.core.rate_limiter import get_rate_limiter

        parsed = urllib.parse.urlparse(user_url or "")
        user_id = parsed.path.split("/")[-1]
        query = urllib.parse.parse_qs(parsed.query)
        xsec_token = (query.get("xsec_token") or [""])[0]
        xsec_source = (query.get("xsec_source") or ["pc_search"])[0]

        limiter = get_rate_limiter()
        notes: list[Any] = []
        cursor = ""
        with direct_xhs_request_env():
            api = self._api()
            while True:
                # ⚠️ 每页都过限流器：超限**等待**（return False 只在长超时后发生），
                # 不在这里停止 —— 与设计 13 §21「要求翻到平台没有下一页为止」一致。
                limiter.wait_allow(user_id or "unknown")
                success, message, raw = api.get_user_note_info(
                    user_id, cursor, xsec_token, xsec_source
                )
                if not success:
                    return False, message, notes
                data = (raw or {}).get("data") or {}
                page_notes = data.get("notes") or []
                notes.extend(page_notes)
                if not data.get("has_more") or not page_notes:
                    break
                cursor = str(data.get("cursor") or "")
                if not cursor:
                    break
        return True, "", notes

    def get_self_info(self) -> Any:
        with direct_xhs_request_env():
            success, message, res_json = self._api().get_user_me()
        payload = (res_json or {}).get("data") if isinstance(res_json, dict) else None
        if not success or not payload:
            raise RuntimeError(message or "XHS self profile refresh failed")
        return payload

    def get_user_other_info(self, user_id: str) -> Any:
        """单页账号资料（``/api/sns/web/v1/user/otherinfo``）。

        这是**账号级互动计数（粉丝 / 关注 / 获赞）的唯一真实来源**。
        ⚠️ 不要改用 ``/v2/user/me``：实测它的 ``data`` 只有 8 个字段
        （``desc/gender/guest/imageb/images/nickname/red_id/user_id``），
        **没有 ``interactions``** —— 读它会让三项恒为空（DEF-039）。
        返回 ``(success, message, payload)`` 三元组，调用方自行判定失败原因。
        """
        with direct_xhs_request_env():
            return self._api().get_user_info(user_id)

    def get_self_profile(self) -> Any:
        """Return the signed-in account identity as a ``(success, message, payload)`` triple.

        Deliberately separate from :meth:`get_self_info`: that method asserts
        success and raises, which suits the account health-check path. The
        self-content routes need to tell "cookie expired" apart from
        "xsec_token missing" and degrade accordingly, so they need the raw
        message preserved instead of an exception.
        """
        with direct_xhs_request_env():
            return self._api().get_user_me()

    def get_user_posted_page(
        self,
        user_id: str,
        cursor: str = "",
        xsec_token: str = "",
        xsec_source: str = "",
    ) -> Any:
        """Single page of notes authored by ``user_id`` (``/v1/user_posted``).

        Single-page only by design. The SDK's ``get_user_all_notes`` loops until
        ``has_more`` is false, which for an account with hundreds of notes means
        dozens of consecutive requests — a rate-limit risk with no user-visible
        benefit over explicit "load more".
        """
        with direct_xhs_request_env():
            return self._api().get_user_note_info(user_id, cursor, xsec_token, xsec_source)

    def get_user_liked_page(
        self,
        user_id: str,
        cursor: str = "",
        xsec_token: str = "",
        xsec_source: str = "",
    ) -> Any:
        """Single page of notes liked by ``user_id`` (``/v1/note/like/page``)."""
        with direct_xhs_request_env():
            return self._api().get_user_like_note_info(user_id, cursor, xsec_token, xsec_source)

    def get_user_collected_page(
        self,
        user_id: str,
        cursor: str = "",
        xsec_token: str = "",
        xsec_source: str = "",
    ) -> Any:
        """Single page of notes collected by ``user_id`` (``/v2/note/collect/page``)."""
        with direct_xhs_request_env():
            return self._api().get_user_collect_note_info(user_id, cursor, xsec_token, xsec_source)

    def get_interaction_messages(self, kind: str, cursor: str = "") -> Any:
        """Single page of interaction notifications.

        ``kind`` is one of ``like_collect`` / ``mention`` / ``connection``. The
        SDK already exposes a matching single-page method for each, so this
        introduces no new platform surface.
        """
        with direct_xhs_request_env():
            if kind == "like_collect":
                return self._api().get_likesAndcollects(cursor)
            if kind == "mention":
                return self._api().get_metions(cursor)
            if kind == "connection":
                return self._api().get_new_connections(cursor)
            raise ValueError(f"Unsupported interaction kind: {kind}")
