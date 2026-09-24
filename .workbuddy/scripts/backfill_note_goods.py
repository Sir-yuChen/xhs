"""一次性回填：把**存量**笔记 ↔ 商品关系补进 `note_goods`（设计 13 §26 存量回填）。

## 为什么需要它

`note_goods` 关系行只在**同步链路**里创建（`note_sync_service` 的 creator / ark 源 +
`shop_goods_sync_service` 的自愈，共三个调用点）。2026-09-22 关系表随 §26 落地时，库里
已有 1962 条**更早同步**的笔记 —— 它们永远不会被「新同步」碰到，于是：

* `shop_products` 有货（1974 行）、`shop_note_goods` 证据缓存有货（1935 行，1923 行带商品），
* 但 `note_goods` 是 **0 行** ⇒ 内容库详情卡查不到挂载商品（用户 2026-09-22 深夜报出
  「笔记挂载的商品不展示」）。

本脚本用**同一个解析器**（`shop_goods_service.link_note_goods`，纯本地、零平台请求、
幂等只增不删）把证据缓存里的存量映射补成关系行。**不是第二份实现**（DEF-044/058）。

## 用法

```bash
# 先备份（WAL 纪律：db + -wal + -shm 三个一起）
cp data/spider_xhs.db data/spider_xhs.db.bak-<日期>-pre-note-goods-backfill
cp data/spider_xhs.db-wal data/spider_xhs.db.bak-<日期>-pre-note-goods-backfill-wal 2>/dev/null

# 跑（自动用应用同一个库：core.config.get_settings().database_url）
"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe" .workbuddy/scripts/backfill_note_goods.py
```

可重复跑（幂等）：已在库里的关系行不会重复插（唯一键兜底）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import select

from backend.app.core.database import SessionLocal
from backend.app.models import PlatformAccount
from backend.app.services.shop_goods_service import cached_goods_mappings, link_note_goods


def main() -> int:
    db = SessionLocal()
    try:
        accounts = db.scalars(
            select(PlatformAccount).where(PlatformAccount.deleted_at.is_(None))
        ).all()
        if not accounts:
            print("没有账号，无需回填。")
            return 0

        total_linked = 0
        total_skipped = 0
        for account in accounts:
            # 证据缓存里这个账号的**全部**映射（与商品同步自愈同一条读取路径）。
            mappings = cached_goods_mappings(db, account.id)
            if not mappings:
                print(f"账号 {account.id}（{account.nickname or '未命名'}）：无证据缓存，跳过")
                continue
            result = link_note_goods(db, account, mappings)
            total_linked += result["linked"]
            total_skipped += result["skipped"]
            print(
                f"账号 {account.id}（{account.nickname or '未命名'}）："
                f"缓存 {len(mappings)} 条 ⇒ 新关系 {result['linked']} 行，"
                f"跳过（笔记或商品缺失）{result['skipped']} 对"
            )
        db.commit()
        print(f"完成：共新关系 {total_linked} 行，跳过 {total_skipped} 对。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
