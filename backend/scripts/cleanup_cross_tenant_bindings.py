"""清理「跨租户」的店铺绑定残留（DEF-054 / DEF-055 的数据侧）。

# ⛔ 已被取代 —— 本脚本**不能再运行**（2026-09-22）

它要清理的东西**已经不存在了**，而且它依赖的表也没了：

| 本脚本依赖 | 现状 |
|---|---|
| `shop_account_bindings` 表 | **整表移除**（迁移 `m5n6o7p8q9r0`）；脚本一查就 `no such table` |
| `shop_note_goods.binding_id` 列 | 改名 `platform_account_id`（迁移 `m5n6o7p8q9r0`） |
| 那条「跨租户残留」本身 | 已由同一次迁移随**多余账号清除**一并删掉（`id IN (1, 15)`） |

同一件事现在由 **DB 约束**兜底，不再靠人工治理：`platform_accounts.shop_id_fk`
上有部分唯一索引 `uq_platform_accounts_shop_id_fk`，跨租户的重复引用会被直接拒绝；
`bind_shop` 另有一次**全库范围**的占用检查（`any_account_for_shop`），把
IntegrityError 换成 409 + 人话。所以本文件保留下来只为**留痕**，不再执行。

**下面这段是原说明（原句保留，供追溯）：**

## 为什么会有这种东西

`shop_account_bindings.shop_id_fk` 指向的店铺是**按用户维度的实体**
（`shops.user_id`，唯一约束 `(user_id, seller_id)`）。正常路径
（`POST /xhs/qianfan/bind`）**不可能**造出「绑定行的 `user_id` ≠ 店铺行的 `user_id`」——
`bind_shop` 先过 `_owned_account`、再 `upsert_shop(current_user.id, …)`，天然同租户。

但真实库里就躺着一条：`user_id=1` 的测试账号「增量测试号」（`ext-incr`）
绑到了 `user_id=2` 的店铺「观隅好物的店」上 —— DEF-049 时期的**脚本直写**遗留
（它的 `shop_user_id='shop-incr'`、`shop_name='增量测试店'` 与所属店铺的
`seller_id` / 店名**完全不符**，所以不可能是 `bind` 端点写的）。

## 症状与代码侧的关系

代码侧的两道闸门（DEF-054 租户闸、DEF-055 软删闸）已经让这条行**不再被计入**
任何界面数字（「同店 N 个账号」/「N 个账号读取」）—— 也就是说**不清它页面也正常**。
但数据本身仍在，会继续污染「这个店还有几个账号绑着」这类原始统计与人工排查，
也让 `drop_binding` 的孤儿清理多一层依赖（现在靠 SQL 闸门兜住，而不是靠数据干净）。

因此本脚本是**数据治理**动作，不是功能修复：让库里的事实与契约一致。

## 用法（**已作废**）

    # 1) 先看（默认 dry-run，只打印计划，不写一行）
    python backend/scripts/cleanup_cross_tenant_bindings.py

    # 2) 确认无误后再写
    python backend/scripts/cleanup_cross_tenant_bindings.py --apply

    # 指定库（默认 data/spider_xhs.db）
    python backend/scripts/cleanup_cross_tenant_bindings.py --db data/spider_xhs.db

⚠️ **跑之前先备份**：`cp data/spider_xhs.db .workbuddy/tmp/pre_cleanup_backup.db`。
脚本**不做**任何备份（把备份交给调用方，才能保证它一定发生在同一个文件系统上）。

## 判据（只删**同时满足**以下两条的行）

1. `binding.user_id != shop.user_id`（租户不一致 —— 唯一真正的「跨租户」定义）；
2. 该 binding 的 `shop_user_id` 与店铺的 `seller_id` **也不符**（双条件是为了不误伤：
   同一租户内因历史原因漏刷冗余展示字段的行，不该被当成跨租户删掉）。

只删 `shop_account_bindings` 与其 `shop_note_goods` 映射；`shops` / `platform_accounts`
**一律不动**（店铺可能还有本租户的合法绑定；账号删除有它自己的软删语义）。
"""
from __future__ import annotations

from pathlib import Path

#: 取代它的迁移（表移除 + 多余账号清除 + 部分唯一索引）。
SUPERSEDED_BY = "backend/alembic/versions/m5n6o7p8q9r0_drop_shop_account_bindings.py"

#: 取代它的运行期守卫（把 IntegrityError 换成 409 + 人话）。
SUPERSEDED_BY_RUNTIME = (
    "backend/app/services/shop_binding_service.py::any_account_for_shop"
    "（qianfan.bind_shop 内的全库占用检查）"
)

#: 事实来源（可执行的契约）。
CONTRACT_TESTS = "backend/tests/test_shop_nm.py"


def _refuse(message: str) -> int:
    print(f"[cleanup_cross_tenant_bindings] ⛔ 已作废：{message}")
    print(f"   取代它的迁移：{SUPERSEDED_BY}")
    print(f"   取代它的守卫：{SUPERSEDED_BY_RUNTIME}")
    print(f"   契约用例：  {CONTRACT_TESTS}")
    print("   （本脚本只留痕，不执行任何写操作。）")
    return 2


def main() -> int:
    # 旧实现（collect_plan / collect_orphan_mappings / audit_only + dry-run / --apply 删除）
    # **已删除**（不是注释掉）：它 SELECT 的表 `shop_account_bindings` 已整表移除，
    # 留着只会在 __main__ 里抛 `no such table: shop_account_bindings` —— 那就是死代码。
    # 判断依据（DEF-048/056 同款）：这条路径在**任何**真实输入下都不会成功执行。
    return _refuse(
        "shop_account_bindings 整表已移除；那条跨租户残留已由迁移 m5n6o7p8q9r0 "
        "随多余账号（id in (1, 15)）清除一并删除。同一件事现由 DB 兜底："
        "platform_accounts.shop_id_fk 上的部分唯一索引 uq_platform_accounts_shop_id_fk。"
    )


if __name__ == "__main__":
    raise SystemExit(main())
