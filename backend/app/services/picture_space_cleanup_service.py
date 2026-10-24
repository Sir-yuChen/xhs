"""图片空间「清理未引用图片」定时任务（2026-10-11 用户需求）。

## 用户口径（逐字）

> 图片空间新增清理未图片定时任务，每周一执行一次
> 定时任务调用参数，执行账号默认全部；删除到回收站或彻底删除下拉单选二选一，默认彻底删除
> 定时任务逻辑：根据入参执行账号使用对应的千帆登录会话，未传默认全部小红书账号循环一下逻辑，
> 调用图片空间的翻页查询每页 500 条，每查询一页，循环将未应用的图片放单独数组中，
> 每一百条调用一次批量删除接口，按照每页循环上面的逻辑。
> 所有页查询循环执行完毕后。当调用参数为彻底删除时再执行查询图片回收站列表依旧每页 500 条，
> 每查询一页再分一百条一次调用批量彻底删除图片接口，循环当前页的数据，执行完再查询下一页，
> 直到循环执行到最后一页彻底移除回收站内的所有图片

## 为什么在服务层而不是端点

与 `no_traffic_cleanup_service` / `shop_goods_sync_service` 同一条纪律：**定时任务入口是
`user_scoped` 的 module 级函数**（`job_registry.JobSpec.func`），签名固定
`func(user_id, **params)`。写成端点则无法被调度器引用（调度器按文本 `module:name` 持久化）。

## 三条硬约定

1. **零落库**：本模块不写任何表、不落任何文件（设计 22 §1.1「图片空间数据不落库」）——
   平台侧数据现取现删，本地不留副本。唯一被写的是**调度日志行**（由 runner 负责，不是本模块）。
2. **只删未引用**：判据 = 平台 `additionalData.productCount == 0`（`picture_space_service`
   已把它归一成 `referenced: False`）。**已引用的一张都不动** —— 删了它会让在售商品的图裂开。
3. **限流在每页 / 每批之间**（硬约束③的替代形态）：翻页与批量删除都过
   `CrawlRateLimiter.wait_allow()` —— **不设页数上限**（§21：刹车靠速率，不靠上限）。

## 为什么「彻底删除」要多跑一遍回收站

平台语义分两层（`picture_space_service` 的常量注释）：
`batch_edit_directory_manageable {status:4}` = **进回收站**（可还原）；
`batch_delete_directory_manageable` = **彻底删除**（不可还原，只能对回收站里的条目做）。

⇒ 传「彻底删除」时链路是**两段**：先把未引用图片收进回收站（第一段，与「删除到回收站」
完全相同），**全部页跑完**后再翻回收站、逐批彻底删。两段共用一份翻页实现，只有「删除动作」
与「筛选条件」不同。
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from sqlalchemy import select

from backend.app.core.database import SessionLocal
from backend.app.models.platform_account import PlatformAccount
from backend.app.services import picture_space_service as ps
from backend.app.core.rate_limiter import get_rate_limiter

logger = logging.getLogger(__name__)

#: 平台的单页上限（真机下拉 100 / 200 / 500；用户口径「每页 500 条」）。
PAGE_SIZE = 500

#: 单次批量删除的条数（用户口径「每一百条调用一次批量删除接口」）。
#: 与 `picture_space_service.MATERIAL_BATCH_LIMIT`（手工端点上限）**同值但语义不同**：
#: 那个是「用户在界面上一次最多勾多少」，这个是「定时任务一批打多少」。
#: ⚠️ 保持相等是有意的 —— 平台的批量上限只验证过这一档，两边取同一个数最保守。
DELETE_BATCH_SIZE = 100

#: 删除方式（定时任务调用参数 `delete_mode` 的合法取值）。
#: * `recycle` —— 只把未引用图片移进平台回收站（可在平台还原）；
#: * `permanent` —— 移进回收站**再彻底删除**（不可还原）。**默认值**（用户口径）。
MODE_RECYCLE = "recycle"
MODE_PERMANENT = "permanent"
DELETE_MODES: tuple[str, ...] = (MODE_RECYCLE, MODE_PERMANENT)
DEFAULT_DELETE_MODE = MODE_PERMANENT

#: 任务中文名 / 键（注册表与文档引用的**唯一真相源**在 `job_registry`，这里只给默认值用）。
JOB_KEY = "system.picture_space_cleanup"

#: 限流键后缀（与 `shop_goods_sync_service` 的 `:goods` 同一形态）。
#: ⚠️ **必须与商品同步分开**：图片空间的额度是平台侧独立的，共用一个键会让先跑的
#: 那条把额度耗光、后跑的刚起步就被挡。
_LIMIT_SUFFIX = ":material"


def coerce_delete_mode(value: Any) -> str:
    """把任意入参收敛到合法删除方式（缺值 / 坏值 ⇒ **默认彻底删除**）。

    ⚠️ 坏值**不报错**（与「缺值口径」相反，这是有意的）：定时任务在没人看着的
    凌晨跑，一次笔误不该让整周任务空转 —— 落到默认值并**在结果里如实报**
    （`mode_invalid`），比抛异常有用。手工端点才需要 400。
    """
    text = str(value or "").strip().lower()
    if text in DELETE_MODES:
        return text
    return DEFAULT_DELETE_MODE


def validate_cleanup_params(params: dict, db: Any = None, user: Any = None) -> None:
    """`params_validator` 钩子：`delete_mode` 只收白名单取值（其余抛 `ValueError`）。

    ⚠️ 两者分工**刻意不同**，不要合并：
    * **API 边界**（本函数，用户在建 / 改 / 执行一次时点的）⇒ 坏值 **400**，
      用户当场就知道自己填错了；
    * **运行期**（`coerce_delete_mode`，调度器凌晨自动跑的）⇒ 坏值**落默认值**，
      不因为一行脏数据让整周任务空转。

    合并成一处会二选一地丢掉一边：只留 400 ⇒ 脏行永远跑不起来（任务列表上
    一直红）；只留默认值 ⇒ 用户填错了没人告诉他。
    """
    value = params.get("delete_mode")
    if value is None or str(value).strip() == "":
        return  # 缺值 ⇒ 运行期兜底成默认值（不算错）
    if str(value).strip().lower() not in DELETE_MODES:
        raise ValueError(
            f"delete_mode 只支持 {' / '.join(DELETE_MODES)}，收到 {value!r}"
        )


def is_unreferenced(item: dict[str, Any]) -> bool:
    """这一条**未应用**吗（⇒ 该清）。

    判据 = 平台 `productCount == 0`（归一化后是 `referenced: False`）。
    ⚠️ **只认文件**（`type == 2`）：文件夹没有「引用」概念，`productCount` 恒 0 ⇒
    不排除的话会把用户整个文件夹树删光。
    """
    if not isinstance(item, dict):
        return False
    #: `type` 已由 `normalize_material_item` 归一成 int|None；比对不上就不是文件。
    if item.get("type") != ps.MATERIAL_TYPE_FILE:
        return False
    if not str(item.get("id") or "").strip():
        return False
    return not bool(item.get("referenced"))


def _chunks(items: Sequence[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    """按 `size` 切块（用户口径「每一百条调用一次」）。"""
    step = max(1, int(size))
    return [list(items[i : i + step]) for i in range(0, len(items), step)]


def _search_body(*, page: int, recycle: bool) -> dict[str, Any]:
    """一页的查询体。两段链路只差**筛选条件**（`statuses` / 排序 + 是否回收站视图）。

    ⚠️ 与 `qianfan.py` 的路由实现**同口径**（回收站 = `statuses=[4]` + 只取文件 +
    按删除时间倒序）：平台这一套是照着真机抓包逐字对的，两处写岔任何一处都会
    「列表看起来对、删起来全空」。
    """
    if recycle:
        return ps.build_search_body(
            basic_types=[ps.MATERIAL_TYPE_FILE],
            order_field="updateTime",
            order_asc=False,
            page=page,
            page_size=PAGE_SIZE,
            statuses=[ps.MATERIAL_STATUS_RECYCLE_BIN],
        )
    return ps.build_search_body(
        basic_types=[ps.MATERIAL_TYPE_FILE],
        page=page,
        page_size=PAGE_SIZE,
        statuses=[ps.MATERIAL_STATUS_NORMAL],
    )


def _scan_pages(
    adapter: Any,
    *,
    recycle: bool,
    limit_key: str,
    handle_page: Any,
) -> None:
    """逐页扫描并调用 `handle_page(items)`；**翻到平台没有下一页为止**（不设页数上限）。

    `handle_page(items) -> int` 返回**本页被处理掉（删掉）的条数**。

    ## ⚠️ 为什么「删了就不前进页号」

    平台分页是按**当前池子的偏移**切的（`pageIndex` × `pageSize`）。每页删掉 k 条之后
    池子缩短 k，同一 `pageIndex` 拿到的就是**往后挪了 k 位**的那一批 —— 如果这时还
    `page += 1`，中间那 k 条会被整段跳过（实测：1200 条只删掉 700 条，500 条静默漏掉）。

    ⇒ 判据是「本页有没有删成功」：
    * 删了 ⇒ **停在原页号**再取一次（腾出来的位置把后面的一批顶了上来）；
    * 一条没删 ⇒ 前进一页（池子没变，偏移仍然正确）。

    另有防死循环兜底（`_scan_pages` 不假定平台真的删掉了）：同一页号**连续两次**拿到
    **完全相同的 id 集合**时强制前进 —— 那说明「回执说成功、池子其实没变」，
    停在原地只会空转。判据是 id 集合而不是条数：条数相同但换了一批是正常前进。
    """
    limiter = get_rate_limiter()
    page = 1
    prev_ids: set[str] = set()
    while True:
        # 每页都过一次限流器（超限**等待**而非停止 —— §21 的刹车形态）。
        if not limiter.wait_allow(limit_key):
            logger.warning("picture-space cleanup: rate limit timeout while paging (%s)", limit_key)
            return
        ok, msg, raw = adapter.search_material_space(_search_body(page=page, recycle=recycle))
        if not ok:
            logger.warning("picture-space cleanup: page %s query failed: %s", page, msg)
            return
        items = ps.normalize_search_payload(raw).get("items") or []
        if not items:
            return
        ids = {str(it.get("id") or "") for it in items}
        removed = int(handle_page(items) or 0)
        if removed > 0 and ids != prev_ids:
            prev_ids = ids
            continue  # 留在原页号
        prev_ids = set()
        page += 1


def cleanup_account(
    db: Any,
    account: PlatformAccount,
    *,
    delete_mode: str = DEFAULT_DELETE_MODE,
    adapter: Any = None,
) -> dict[str, Any]:
    """清理**一个账号**的图片空间（同步执行；调度器在后台线程里调它）。

    返回账目（逐条都在 `results` 里如实报，失败不吞）：

    ```
    {
      "account_id": 8, "status": "done"|"failed",
      "scanned": 1234,        # 两段一共扫到多少条
      "unreferenced": 300,    # 判为未引用的多少条
      "moved": 300,           # 成功移进回收站多少条
      "purged": 300,          # 成功彻底删除多少条（recycle 模式恒 0）
      "failed": 0,            # 平台拒绝的条数（批量失败时按整批计）
      "delete_mode": "permanent",
      "error": ""             # status=failed 时的原因（平台原文或本地结构化原因）
    }
    ```

    ⚠️ **失败按「批」计**：平台一个批量请求要么整体成功要么整体失败，所以一批失败
    会把该批全部条数记进 `failed` —— 不拆成「逐条猜」，那是在编数据。
    """
    from backend.app.services.shop_binding_service import has_ark_session, qianfan_cookies_for_account

    mode = coerce_delete_mode(delete_mode)
    out: dict[str, Any] = {
        "account_id": account.id,
        "account_name": str(getattr(account, "nickname", "") or ""),
        "status": "done",
        "delete_mode": mode,
        "scanned": 0,
        "unreferenced": 0,
        "moved": 0,
        "purged": 0,
        "failed": 0,
        "error": "",
    }
    if not has_ark_session(db, account.id):
        # 没登录态 ⇒ 结构化失败（用户口径「根据入参执行账号使用对应的千帆登录会话」——
        # 拿不到就如实报，**不借别的账号的会话**，与 DEF-051 同一把尺子）。
        out["status"] = "failed"
        out["error"] = "该账号没有可用的千帆（ark）登录态，无法清理图片空间"
        return out

    if adapter is None:
        from backend.app.adapters.xhs.qianfan_api_adapter import XhsQianfanApiAdapter

        adapter = XhsQianfanApiAdapter(qianfan_cookies_for_account(db, account.id))

    limit_key = f"{account.id}{_LIMIT_SUFFIX}"
    limiter = get_rate_limiter()

    def _delete_batch(chunk: list[dict[str, Any]]) -> int:
        """一批未引用图片 ⇒ 移进回收站。返回**真正删掉的条数**（平台拒绝 ⇒ 0）。"""
        out["unreferenced"] += len(chunk)
        if not limiter.wait_allow(limit_key):
            out["failed"] += len(chunk)
            logger.warning("picture-space cleanup: rate limit timeout before delete (%s)", limit_key)
            return 0
        ok, msg, _ = adapter.edit_material_manageables(
            account.seller_id or "",
            [ps.build_delete_item(it) for it in chunk],
        )
        if not ok:
            out["failed"] += len(chunk)
            logger.warning("picture-space cleanup: move-to-recycle failed: %s", msg)
            return 0
        out["moved"] += len(chunk)
        return len(chunk)

    def _purge_batch(chunk: list[dict[str, Any]]) -> int:
        """一批回收站条目 ⇒ 彻底删除。返回**真正删掉的条数**。"""
        if not limiter.wait_allow(limit_key):
            out["failed"] += len(chunk)
            logger.warning("picture-space cleanup: rate limit timeout before purge (%s)", limit_key)
            return 0
        ok, msg, _ = adapter.delete_material_manageables(
            account.seller_id or "", ps.build_permadelete_elements(chunk)
        )
        if not ok:
            out["failed"] += len(chunk)
            logger.warning("picture-space cleanup: purge failed: %s", msg)
            return 0
        out["purged"] += len(chunk)
        return len(chunk)

    # ---- 第一段：未引用图片 ⇒ 回收站 ------------------------------------- #
    #: 用户口径「每查询一页，循环将未应用的图片放单独数组中，**每一百条调用一次**批量删除接口」。
    #: ⚠️ 分块**只在页内做**（`_chunks` 的末块是余数）—— 不跨页累积余数：停留重扫时
    #: 那些余数会被同一页再收一遍，攒起来就重复删同一批（同一 id 发两次请求）。
    def _handle_normal(items: list[dict[str, Any]]) -> int:
        out["scanned"] += len(items)
        removed = 0
        for chunk in _chunks(
            [it for it in items if is_unreferenced(it)], DELETE_BATCH_SIZE
        ):
            removed += _delete_batch(chunk)
        return removed

    _scan_pages(adapter, recycle=False, limit_key=limit_key, handle_page=_handle_normal)

    # ---- 第二段（仅「彻底删除」）：回收站 ⇒ 永久移除 ----------------------- #
    #: 用户口径「当调用参数为彻底删除时**再**执行查询图片回收站列表……直到循环执行到最后一页」。
    if mode == MODE_PERMANENT:

        def _handle_recycle(items: list[dict[str, Any]]) -> int:
            out["scanned"] += len(items)
            removed = 0
            for chunk in _chunks(
                [it for it in items if str(it.get("id") or "").strip()], DELETE_BATCH_SIZE
            ):
                removed += _purge_batch(chunk)
            return removed

        _scan_pages(adapter, recycle=True, limit_key=limit_key, handle_page=_handle_recycle)

    if out["failed"]:
        out["status"] = "partial"
    return out


def run_picture_space_cleanup(
    user_id: int,
    account_ids: Optional[Sequence[int]] = None,
    delete_mode: Any = DEFAULT_DELETE_MODE,
) -> dict[str, Any]:
    """调度入口：给**行主本人**的账号清理图片空间（DEF-066 行主隔离）。

    `account_ids` 空 = 本人的全部小红书账号（用户口径「未传默认全部小红书账号」）；
    非空 = 只跑点名的那几个（建 / 改双路归属校验由 API 层承担，这里只认 id）。

    ⚠️ `user_id` 必填：账号查询**恒带** `PlatformAccount.user_id == user_id` ——
    定时任务绝不能碰别人的账号（DEF-066）。

    **一个账号失败不拖垮整轮**（与既有同步任务同款）：逐账号 try 包裹，结果收进
    `results`，整体返回 `{accounts, results}` 供调度日志记账。
    """
    mode = coerce_delete_mode(delete_mode)
    db = SessionLocal()
    results: list[dict[str, Any]] = []
    try:
        statement = select(PlatformAccount).where(
            PlatformAccount.user_id == user_id,
            PlatformAccount.platform == "xhs",
            PlatformAccount.deleted_at.is_(None),
        )
        if account_ids:
            statement = statement.where(PlatformAccount.id.in_([int(a) for a in account_ids]))
        accounts = list(db.scalars(statement).all())
        for account in accounts:
            try:
                results.append(cleanup_account(db, account, delete_mode=mode))
            except Exception as exc:  # noqa: BLE001 - 单账号失败不拖垮整轮
                logger.warning("picture-space cleanup for account %s failed: %s", account.id, exc)
                results.append(
                    {
                        "account_id": account.id,
                        "account_name": str(getattr(account, "nickname", "") or ""),
                        "status": "failed",
                        "delete_mode": mode,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
    except Exception as exc:  # noqa: BLE001 - 调度器调用，必须吞异常
        logger.error("run_picture_space_cleanup failed: %s", exc)
        return {"accounts": 0, "results": results, "error": str(exc), "delete_mode": mode}
    finally:
        db.close()

    return {
        "accounts": len(results),
        "cleaned": sum(int(r.get("moved") or 0) for r in results),
        "purged": sum(int(r.get("purged") or 0) for r in results),
        "failed": sum(int(r.get("failed") or 0) for r in results),
        "delete_mode": mode,
        "results": results,
    }


__all__ = [
    "DELETE_BATCH_SIZE",
    "DELETE_MODES",
    "DEFAULT_DELETE_MODE",
    "JOB_KEY",
    "MODE_PERMANENT",
    "MODE_RECYCLE",
    "PAGE_SIZE",
    "cleanup_account",
    "coerce_delete_mode",
    "is_unreferenced",
    "run_picture_space_cleanup",
    "validate_cleanup_params",
]
