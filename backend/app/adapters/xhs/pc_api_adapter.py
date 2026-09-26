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

        auth = XHSPcAuth.from_cookie(self.cookies, user_id=self.user_id)
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

    def get_note_comments(self, note_url: str) -> Any:
        with direct_xhs_request_env():
            return self._api().get_note_all_comment(url=note_url)

    def get_user_notes(self, user_url: str) -> Any:
        with direct_xhs_request_env():
            return self._api().get_user_all_notes(user_url=user_url)

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
