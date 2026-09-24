"""把同一身份的多行账号合并成一行（§11.6「数据库只有一个账号」的存量清理）。

典型场景：观隅好物在库里是两行 —— acct 1（pc / user 1）与 acct 8（creator / user 2），
`external_user_id` 相同却属于不同租户。改造后新建路径不会再产生这种行（PC 派生的创作者
Cookie 挂回同一账号），但**存量**要单独清理。

做什么（对每个「同 `external_user_id` 多行」的组）：
  1. 选**保留行** = 该组里 id 最大的行（数据最新的那个）；
  2. 其余行的 `notes` / `shop_note_goods` **改挂**到保留行，`user_id` 一并改成保留行的租户
     （H8：身份与内容必须绑在同一份数据上）；
  3. **店铺关系**（`shop_id_fk` / `shop_state`）随身份搬 —— **但必须在软删之后写**
     （1:1 的部分唯一索引 `uq_platform_accounts_shop_id_fk` 会把「同店第二个**存活**账号」挡下；
     反过来写必然 IntegrityError）；
  4. 其余行**软删**（`deleted_at`），身份行留在原位让唯一索引继续成立。

**默认不搬 Cookie**：被合并行的凭据**可能已过期**，搬过去会让能力端点谎报「该通道可用」
（`channels.www=true` 但一读就失败）。需要搬时显式加 `--with-cookies`。

⚠️ 跨租户搬运：`user_id` 会变，原租户将看不到这些数据。**默认 dry-run，必须显式 `--apply`。**

## 2026-09-22 修的两处「schema 已变」bug

本脚本是**手工治理工具**（无测试覆盖），所以两处 schema 变更后没人发现它已经跑不通；
本次一并修掉（不是留痕作废）：

| 曾经的写法 | 为什么会崩 | 现在的做法 |
|---|---|---|
| `select id, channel from account_cookie_versions` | `channel` 列**已去掉**（信封 JSON 改造 `j3k4l5m6n7o8`）⇒ `no such column: channel` | 走**现成**信封入口 `parse_channel_envelope(decrypt_text(...))` 读、`dump_channel_envelope` + `encrypt_text` 写 —— **不另写一份信封解析** |
| `for table in ("notes", "shop_account_bindings")` | 绑定表**已整表移除**（迁移 `m5n6o7p8q9r0`）⇒ `no such table` | 改 `("notes", "shop_note_goods")`，并把**店铺关系**按上面第 3 条随身份搬 |

⚠️ **同通道不并存**（与原实现的差异，写清楚免得后人以为是漏了）：信封的键**就是平台名**，
同一通道没法在同一账号下存两份。所以保留行**已有**该通道时，被合并行那一份**不搬**（只打印提示）
—— 它仍在库里、随被合并行一起软删保留，可人工取用。原实现想用 `<channel>-merged-<id>` 造个
伪通道名来并存，在信封模型下做不到（`dump_channel_envelope` 只认已知平台名），
**刻意不造伪键**（造假键 = 造第二份真相源）。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.core.security import decrypt_text, encrypt_text  # noqa: E402
from backend.app.services.account_service import (  # noqa: E402
    dump_channel_envelope,
    parse_channel_envelope,
)


def _channels_of(db: sqlite3.Connection, account_id: int) -> dict[str, str]:
    """该账号各通道的**最新一版** jar（`{channel: jar}`，没有的通道不在字典里）。

    合并口径与 `account_service.latest_cookies_for_channels()` **同源**：
    `created_at desc, id desc` + **setdefault**（先出现者赢）。升序覆盖写会让旧信封
    反过来顶掉新令牌 —— 这类漂移的后果是「同一行令牌，页面说有效、调用点说没有」。
    """
    rows = db.execute(
        "select encrypted_cookies from account_cookie_versions "
        "where platform_account_id=? order by created_at desc, id desc",
        (account_id,),
    ).fetchall()
    merged: dict[str, str] = {}
    for row in rows:
        for channel, jar in parse_channel_envelope(decrypt_text(row["encrypted_cookies"])).items():
            merged.setdefault(channel, jar)
    return merged


def _move_cookies(
    *,
    db: sqlite3.Connection,
    dropped_id: int,
    keep_id: int,
    keep_channels: dict[str, str],
    with_cookies: bool,
    apply: bool,
) -> None:
    """把被合并行的令牌按**通道**搬给保留行（`--with-cookies` 才真搬）。"""
    channels = _channels_of(db, dropped_id)
    if not channels:
        return
    for channel, jar in sorted(channels.items()):
        if not with_cookies:
            print(f"      Cookie 通道 {channel} → **不搬**（可能已过期；要搬加 --with-cookies）")
            continue
        if channel in keep_channels:
            print(
                f"      Cookie 通道 {channel} → **不搬**（保留行已有该通道；"
                f"被合并行那一份仍在库里、随该行软删保留，可人工取用）"
            )
            continue
        print(f"      Cookie 通道 {channel} → 保留行（新增一版信封）")
        if apply:
            db.execute(
                "insert into account_cookie_versions (platform_account_id, encrypted_cookies, created_at) "
                "values (?, ?, ?)",
                (
                    keep_id,
                    encrypt_text(dump_channel_envelope({channel: jar})),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
                ),
            )
            keep_channels[channel] = jar


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/spider_xhs.db")
    parser.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    parser.add_argument("--with-cookies", action="store_true",
                        help="连同令牌一起搬（默认不搬：过期凭据会让能力端点谎报可用）")
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

    groups = db.execute(
        """
        select external_user_id, count(*) n, group_concat(id) ids
          from platform_accounts
         where external_user_id is not null and external_user_id != ''
           and deleted_at is null
         group by external_user_id
        having count(*) > 1
         order by external_user_id
        """
    ).fetchall()
    if not groups:
        print("没有需要合并的重复身份。")
        db.close()
        return

    for group in groups:
        ext = group["external_user_id"]
        rows = db.execute(
            "select * from platform_accounts where external_user_id=? and deleted_at is null order by id",
            (ext,),
        ).fetchall()
        keep = rows[-1]
        drop = rows[:-1]
        print(f"\n=== 身份 {ext}：{len(rows)} 行 → 保留 acct {keep['id']}"
              f"（user {keep['user_id']} / {keep['sub_type']} / {keep['nickname']}）")
        keep_channels = _channels_of(db, keep["id"])
        keep_shop = keep["shop_id_fk"]
        for row in drop:
            print(f"    合并 acct {row['id']}（user {row['user_id']} / {row['sub_type']}）")
            _move_cookies(
                db=db,
                dropped_id=row["id"],
                keep_id=keep["id"],
                keep_channels=keep_channels,
                with_cookies=args.with_cookies,
                apply=args.apply,
            )
            for table in ("notes", "shop_note_goods"):
                moved = db.execute(
                    f"select count(*) n from {table} where platform_account_id=?",
                    (row["id"],),
                ).fetchone()["n"]
                if moved:
                    print(f"      {table} {moved} 行 → 改挂保留行（user_id 一并改为 {keep['user_id']}）")
                    if args.apply:
                        db.execute(
                            f"update {table} set platform_account_id=?, user_id=? where platform_account_id=?",
                            (keep["id"], keep["user_id"], row["id"]),
                        )
            #: 店铺关系（1:1）跟着身份走。**顺序是硬要求**：先软删、再赋 `shop_id_fk` ——
            #: 部分唯一索引 `uq_platform_accounts_shop_id_fk`
            #: （`WHERE shop_id_fk IS NOT NULL AND deleted_at IS NULL`）会把「同店第二个**存活**账号」
            #: 挡在外面，反过来写必然 IntegrityError。
            #: ⚠️ 两行**都有**店铺时**不自动搬**：1:1 下保留行只能有一个店，被合并那行挂的店会变成
            #: 「无活跃账号的店铺」—— 这是需要人工判断的账，只打印、不改。
            if row["shop_id_fk"] and keep_shop:
                print(
                    f"      ⚠️ 店铺关系**未搬**：被合并行挂着 shop#{row['shop_id_fk']}，"
                    f"保留行也挂着 shop#{keep_shop}（1:1 只能留一个）—— 请人工决定后处理"
                )
            elif row["shop_id_fk"]:
                print(
                    f"      店铺关系 shop#{row['shop_id_fk']} → 保留行"
                    f"（连同 shop_state，**在软删之后**写）"
                )
            print(f"      软删 acct {row['id']}")
            if args.apply:
                db.execute(
                    "update platform_accounts set deleted_at=? where id=?", (now, row["id"])
                )
                if row["shop_id_fk"] and not keep_shop:
                    db.execute(
                        "update platform_accounts set shop_id_fk=?, shop_state=? where id=?",
                        (row["shop_id_fk"], row["shop_state"], keep["id"]),
                    )
                    keep_shop = row["shop_id_fk"]
        if args.apply:
            # 保留行补齐身份字段（取被合并行里非空的值）
            for row in drop:
                if not keep["external_user_id"] and row["external_user_id"]:
                    db.execute("update platform_accounts set external_user_id=? where id=?",
                               (row["external_user_id"], keep["id"]))
    if args.apply:
        db.commit()
        print("\n✅ 已写入（软删，未物理删除；重新导入同一身份会命中保留行并复活）")
    else:
        print("\n（dry-run；加 --apply 才真正写。跨租户搬运会改变 user_id，请先确认）")
    db.close()


if __name__ == "__main__":
    main()
