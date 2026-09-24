"""只读探针：确认视频笔记的**详情**端点是否带可播放的视频地址。

背景：内容库 843 条视频笔记、0 条视频资产。创作者**列表**只给 `video_info.duration`，
不带流地址 ⇒ 视频播不了。本探针验证详情端点有没有（决定修法）。

只读：只打 GET 详情，不写库、不打印 Cookie。
"""
import json
import sys

sys.path.insert(0, r"D:\work\XHS")

import sqlite3  # noqa: E402

from backend.app.api.platforms.xhs.pc import (  # noqa: E402
    _normalize_detail_payload,
    get_xhs_pc_api_adapter_factory,
)
from backend.app.services.note_sync_service import (  # noqa: E402
    _account_cookies,
    _note_detail_url,
)
from backend.app.core.database import SessionLocal  # noqa: E402
from backend.app.models.note import Note  # noqa: E402

ACCOUNT_ID = 8

db = SessionLocal()
try:
    rows = db.scalars(
        __import__("sqlalchemy").select(Note).where(Note.platform_account_id == ACCOUNT_ID)
    ).all()
    video_ids = []
    for r in rows:
        raw = r.raw_json if isinstance(r.raw_json, dict) else {}
        if str(raw.get("type") or "") == "video" and r.note_id:
            video_ids.append(r.note_id)
    print(f"账号 {ACCOUNT_ID} 视频笔记数：{len(video_ids)}")

    if not video_ids:
        print("没有视频笔记可探针。")
        sys.exit(0)

    cookies = _account_cookies(db, ACCOUNT_ID, channel="www")
    if not cookies:
        print("该账号没有 www 会话，无法打详情。")
        sys.exit(1)

    adapter = get_xhs_pc_api_adapter_factory()(cookies)
    note = next(r for r in rows if r.note_id == video_ids[0])
    url = _note_detail_url(note)
    print(f"探针 note_id={video_ids[0]}  url={url[:70]}...")

    ok, message, payload = adapter.get_note_info(url)
    print("详情请求 success =", ok, "| message =", str(message)[:80])
    if not ok:
        sys.exit(1)

    normalized = _normalize_detail_payload(payload, url)
    print("归一化 image_urls 条数：", len(normalized.get("image_urls") or []))
    print("归一化 video_url      =", repr(normalized.get("video_url"))[:120])
    print("归一化 video_addr     =", repr(normalized.get("video_addr"))[:120])
    print("归一化 content 长度   =", len(str(normalized.get("content") or "")))

    # 顺带看原始 note_card 里跟视频有关的键，确认字段名（供后续解析用）
    data = (payload or {}).get("data") or {}
    items = data.get("items") or []
    card = (items[0] if items and isinstance(items[0], dict) else {}).get("note_card") or {}
    videoish = {k: (str(v)[:80]) for k, v in card.items() if "video" in k.lower()}
    print("note_card 里视频相关键：", json.dumps(videoish, ensure_ascii=False)[:600])
finally:
    db.close()
