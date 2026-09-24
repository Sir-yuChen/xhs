"""一次性补验探针：对商品 6a581608ab42de00152e782c 的挂链笔记 1075 打一次只读详情。

背景（2026-09-23 用户二次报告）：该商品挂链弹窗 3 篇，内容库只看到 2 篇 ——
缺的是同步笔记 1075（平台 ID 6a5d4ce7000000000100de3b）：数据层在库里，
但 hydrated_at IS NULL（正文空、话题空），排在 889 位搜不到。

本脚本复用 hydrate_note_detail（详情补齐唯一实现），只读 GET 一次 PC 详情，
补正文 / 图集 / 话题。失败不写标记（可恢复），与同步链路口径一致。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.core.database import get_db
from backend.app.models import Note, PlatformAccount
from backend.app.services.account_service import latest_cookies_for_channel, CHANNEL_WWW
from backend.app.services.note_sync_service import hydrate_note_detail

db = next(get_db())
note = db.get(Note, 1075)
account = db.get(PlatformAccount, note.platform_account_id)
cookies = latest_cookies_for_channel(db, account.id, CHANNEL_WWW)
print(f"note={note.note_id} account={account.id}({account.nickname}) www_session={'有' if cookies else '无'}")

result = hydrate_note_detail(db, account, note, account_cookies=cookies)
print("结果:", {k: result[k] for k in ("status", "content_filled", "content_updated", "title_updated", "images_added", "video_added", "reason")})

db.refresh(note)
print("补后 content 长度:", len(note.content or ""))
print("补后 hydrated_at:", note.hydrated_at)
print("补后 topics:", (note.topics_json or "")[:120])
