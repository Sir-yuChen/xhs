# encoding: utf-8
"""explore 页面 SSR 取详情 —— 免掉「每篇翻一遍 `user_posted` 全表」（设计 13 §19.13.8）。

**为什么有这一层**（2026-09-28 真机实测，用户报「同步队列执行不动、单篇卡五分钟」）：

`/api/sns/web/v1/feed` 只认**网页侧**笔记级令牌 ⇒ 为了让每篇详情拿得到正文，同步链路
每篇都要先 `refresh_www_detail_tokens` 翻完整个 `user_posted`（1951 条 ÷ 30/页 ≈ 66 页，
页间停顿 5s × 抖动 ⇒ **5–8 分钟/篇**，批量 N 篇 = N 次全表扫描）。

真机对照（真实窗口 Chrome + CDP 抓包，探针 `.workbuddy/scripts/probe_note_detail_by_id.py`）：

| 访问方式 | 结果 |
|---|---|
| 匿名 + 无 token | 302 → `/404?error_code=300031`「当前笔记暂时无法浏览」 |
| 匿名 + **带 token** | **成功**：`__INITIAL_STATE__.note.noteDetailMap` 给 `desc` 477 字 / `imageList` 4 张 |
| 登录态 + 无 token | 仍 302 → `/404?error_code=300031` |

⇒ **① 笔记级 `xsec_token` 是硬要求（会话替代不了）；② 带 token 时详情就在 SSR HTML 里，
不需要会话、不需要签名头、也不挑令牌来源**（`xsec_source=pc_user` 与 `pc_creatormng`
结果完全一致；纯 HTTP 实测 6/6 篇成功，且这些令牌都不是 `user_posted` 刷来的）。

**职责边界（同一语义不留两份实现）**：本模块**只做两件事** ——
① 取 explore 页 HTML；② 把 SSR 的 **camelCase** 笔记对象映射成 `/api/sns/web/v1/feed` 的
**snake_case** 形状（`data.items[0].note_card`）。**归一化仍归
`api/platforms/xhs/pc._normalize_detail_payload`** —— 本层不重复实现一遍字段抽取。

传输层复用 `xhs_utils.xhs_pc.http.PcHttpClient`（Chrome TLS 指纹，与签名链路同一个客户端），
**不带任何 Cookie**（`discard_cookies=True`）：实测匿名即可，少一处会话依赖。

只读边界：单次 GET 页面；不碰写接口；**不打 Cookie / token**。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from backend.app.adapters.xhs.request_env import direct_xhs_request_env

#: SSR 状态脚本。**必须锚到 `</script>`**：`__INITIAL_STATE__` 是 `{…}`，非贪婪匹配到
#: 最近的 `</script>` 才不会被后续脚本吞掉。
_INITIAL_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", re.S)

#: 页面级超时（秒）。详情页 ~72KB，实测 1–2 秒返回；25 秒是给 CDN 抖动的余量。
FETCH_TIMEOUT_SECONDS = 25.0

_REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.xiaohongshu.com/",
}


def _default_user_agent() -> str:
    """与 PC 传输层的 `PC_IMPERSONATE`（`chrome146`）对齐的 UA。

    ⚠️ **必须显式带 UA**（2026-09-28 实测，最容易踩的一处）：`PcHttpClient` 是
    ``default_headers=False`` 的（签名链路自己拼全套头），而 explore 页面**没有 UA
    会被平台判成非浏览器 ⇒ 302 到 `/login?redirectPath=…`**。同一个 URL、同一个
    token：加这一行拿到 SSR 详情（`noteDetailMap` 命中），不加就被踢到登录页。

    UA 版本从 `PC_IMPERSONATE` 推出来（**不写死版本号**）—— 传输层换指纹时这里跟着走，
    免得出现「TLS 指纹是 chrome146、UA 写 chrome120」这种自相矛盾的组合。
    """
    try:
        from xhs_utils.xhs_pc.http import PC_IMPERSONATE

        match = re.search(r"(\d+)", str(PC_IMPERSONATE))
        major = match.group(1) if match else "146"
    except Exception:  # noqa: BLE001 - 取不到就退保守值，不阻路
        major = "146"
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def _parse_initial_state(html: str) -> Optional[dict]:
    """从页面 HTML 里取出 `window.__INITIAL_STATE__`（取不到 / 解析失败 ⇒ None）。

    SSR 里含 `undefined` 字面量（不是合法 JSON）⇒ 先原样试，失败再把 `undefined`
    换成 `null` 重试一次。两次都失败就交给调用方回退（**不猜、不造**）。
    """
    match = _INITIAL_STATE_RE.search(str(html or ""))
    if not match:
        return None
    text = match.group(1)
    for candidate in (text, text.replace("undefined", "null")):
        try:
            value = json.loads(candidate)
        except Exception:  # noqa: BLE001 - 结构变了就走回退，不在这里抛
            continue
        if isinstance(value, dict):
            return value
    return None


def _image_list_from_ssr(value: Any) -> list[dict[str, Any]]:
    """SSR `imageList` ⇒ `note_card.image_list`（保留 `_first_url` 认得的那几个键）。

    `_first_url` 的优先级是 `info_list[1].url` > `url_default` > `url_pre` > `url`，
    与 API 形状一致 ⇒ 这里把四个来源都摆上，不替它做选择。
    """
    images: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        info_list = []
        for entry in item.get("infoList") or []:
            if isinstance(entry, dict) and entry.get("url"):
                info_list.append(
                    {"url": str(entry["url"]), "image_scene": str(entry.get("imageScene") or "")}
                )
        images.append(
            {
                "url_default": str(item.get("urlDefault") or ""),
                "url_pre": str(item.get("urlPre") or ""),
                "url": str(item.get("url") or ""),
                "info_list": info_list,
                "width": item.get("width"),
                "height": item.get("height"),
            }
        )
    return images


def _video_from_ssr(value: Any) -> dict[str, Any]:
    """SSR `video` ⇒ `note_card.video`（`_video_url` 读的是 snake_case 键）。

    ⚠️ **未经真机验证**（设计 13 §19.13.8 ⑤）：本账号 71 篇全为 `normal`，没有视频样本。
    这里按 SSR 已知形状做 `masterUrl → master_url` / `originVideoKey → origin_video_key`
    的键名对齐；取不到就返回空 dict（`_video_url` 自然回空串，不会造地址）。
    """
    video = value if isinstance(value, dict) else {}
    if not video:
        return {}
    media = video.get("media") if isinstance(video.get("media"), dict) else {}
    stream = media.get("stream") if isinstance(media.get("stream"), dict) else {}
    out_stream: dict[str, Any] = {}
    for codec, streams in stream.items():
        if not isinstance(streams, list):
            continue
        out_stream[str(codec)] = [
            {
                "master_url": str(entry.get("masterUrl") or entry.get("master_url") or ""),
                "backup_urls": entry.get("backupUrls") or entry.get("backup_urls") or [],
            }
            for entry in streams
            if isinstance(entry, dict)
        ]
    consumer = video.get("consumer") if isinstance(video.get("consumer"), dict) else {}
    out: dict[str, Any] = {"media": {"stream": out_stream}}
    origin_key = consumer.get("originVideoKey") or consumer.get("origin_video_key")
    if origin_key:
        out["consumer"] = {"origin_video_key": str(origin_key)}
    return out


def _note_card_from_ssr(note: dict) -> dict[str, Any]:
    """SSR 笔记对象（camelCase）⇒ `/v1/feed` 的 `note_card`（snake_case）。

    **只搬字段、不抽语义**：正文/标题/图/话题/互动的最终解释权在
    `_normalize_detail_payload`（`_normalize_search_item` + `_all_urls` + `_tag_names`
    + `_video_url`），这里保证它读得到的键都在、形状一致。
    """
    user = note.get("user") if isinstance(note.get("user"), dict) else {}
    interact = note.get("interactInfo") if isinstance(note.get("interactInfo"), dict) else {}
    tags = []
    for item in note.get("tagList") or []:
        if isinstance(item, dict) and item.get("name"):
            tags.append({"name": str(item["name"]), "type": str(item.get("type") or "")})
    return {
        "note_id": str(note.get("noteId") or note.get("id") or ""),
        "title": str(note.get("title") or ""),
        "desc": str(note.get("desc") or ""),
        "type": str(note.get("type") or ""),
        "time": note.get("time") or 0,
        "image_list": _image_list_from_ssr(note.get("imageList")),
        "tag_list": tags,
        "user": {
            "user_id": str(user.get("userId") or ""),
            "nickname": str(user.get("nickname") or ""),
            "avatar": str(user.get("avatar") or ""),
        },
        "interact_info": {
            "liked_count": interact.get("likedCount"),
            "collected_count": interact.get("collectedCount"),
            "comment_count": interact.get("commentCount"),
            "share_count": interact.get("shareCount"),
        },
        "video": _video_from_ssr(note.get("video")),
    }


def note_detail_payload_from_html(html: str) -> Optional[dict]:
    """explore 页 HTML ⇒ **与 `/v1/feed` 同形**的原始响应体（取不到 ⇒ None）。

    形状刻意对齐 JSON 端点，好让 `_normalize_detail_payload` **原样复用**：

    ``{"success": True, "code": 0, "msg": "成功",
        "data": {"items": [{"note_card": {…}}]}}``
    """
    state = _parse_initial_state(html)
    if state is None:
        return None
    note_section = state.get("note") if isinstance(state.get("note"), dict) else {}
    detail_map = note_section.get("noteDetailMap")
    if not isinstance(detail_map, dict) or not detail_map:
        return None
    first = next(iter(detail_map.values()))
    note = first.get("note") if isinstance(first, dict) else None
    if not isinstance(note, dict) or not note:
        return None
    return {
        "success": True,
        "code": 0,
        "msg": "成功",
        "data": {"items": [{"note_card": _note_card_from_ssr(note)}]},
    }


def fetch_note_detail_payload(
    url: str,
    *,
    timeout: float = FETCH_TIMEOUT_SECONDS,
    http_client: Any = None,
) -> tuple[bool, str, Optional[dict]]:
    """GET explore 页并返回 ``(success, message, payload)`` —— 与
    `XhsPcApiAdapter.get_note_info` **同形**，调用方可以两条路互换。

    ``http_client`` = 注入点（测试替身 / 复用既有客户端）；缺省自建
    `PcHttpClient` 并在 finally 关掉。

    **不抛异常**：取不到是**预期内的结果**（结构变更 / 风控页 / 网络抖动），
    如实返回 `False` + 原因，由调用方回退到 JSON 端点（DEF-058 失败三分法）。
    """
    client = http_client
    owns_client = False
    try:
        if client is None:
            from xhs_utils.xhs_pc.http import PcHttpClient

            client = PcHttpClient()
            owns_client = True
        with direct_xhs_request_env():
            headers = dict(_REQUEST_HEADERS)
            # ⚠️ 这一行不能省（见 `_default_user_agent` 的说明）：没有 UA ⇒ 被踢到登录页
            headers["User-Agent"] = _default_user_agent()
            response = client.get(
                url,
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
            )
        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            return False, f"explore 页面返回 HTTP {status}", None
        payload = note_detail_payload_from_html(getattr(response, "text", "") or "")
        if payload is None:
            return False, "explore 页面未包含笔记数据（SSR 结构变更或该笔记不可见）", None
        return True, "成功", payload
    except Exception as exc:  # noqa: BLE001 - 取不到是结果，不是异常（调用方回退）
        return False, f"explore 页面请求异常（{type(exc).__name__}）", None
    finally:
        if owns_client and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 关不掉不影响本次结果
                pass


__all__ = [
    "FETCH_TIMEOUT_SECONDS",
    "fetch_note_detail_payload",
    "note_detail_payload_from_html",
]
