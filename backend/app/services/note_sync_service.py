"""账号笔记同步：把平台的笔记拉进本地 ``notes`` 表（需求 R1 / R3 / §19）。

## 为什么需要它

一期起「我的笔记」就是**纯实时、完全不落库**（设计 12 §17）。这次要的是本地
沉淀：离线可查、可检索、供后续生成与统计取用。两条链路并存，不互相替代。

## 多源独立同步（§19.3，2026-09-21 用户口径：三态）

**每个可用源各同步各的，互不阻塞**（用户口径：pc 与千帆令牌均可用 ⇒ 两个平台的
笔记全部落库；只有一个可用 ⇒ 只同步该平台；均不可用 ⇒ 结构化多原因失败）：

| 源 | 令牌 | 链路 |
|---|---|---|
| ``www`` | 账号 www 通道（`web_session`+`id_token`） | PC（``get_user_posted_page``）——**仅非 creator 账号**（§21.4 移除冗余源） |
| ``creator`` | 账号 creator 通道 | 创作者平台（``get_posted_notes_page``），**仅 creator 账号** |
| ``ark`` | 账号**自己的** ark 通道（`qianfan_cookies_for_account`，§20 起**无兜底、不借用**） | 千帆商品笔记（``get_goods_note_page``，§19.1.1 实测） |

**为什么保留 creator 源**：创作者账号今天靠它同步；砍掉 = 只有 creator 令牌的
账号彻底不能同步（回归）。用户原文只说「pc 和千帆」两个平台，实现是超集 —
**每个可用源都同步**；要严格两平台，删源清单里一行即可。

**PC 链路缺 ``xsec_token`` 时明确失败**（``XSEC_TOKEN_REQUIRED``），不静默同步
空集 —— 「同步成功但 0 条」会把「令牌缺失」伪装成「账号没笔记」。

**落库合并语义**（§19.3.2）：PC/creator 源先跑（有正文，整行覆盖）；
ark 源后跑，**填空式合并**（只补空字段、raw_json 补键不冲键、无资产才下图）——
直接覆盖会用空正文抹掉已落库的正文。

## 风控（需求 N1 / 硬约束 C5 —— 2026-09-22 按设计 13 §21 修订）

* **全量翻页**：翻到平台没有下一页为止（`has_more=false` / 游标 `-1` / 已落库 ≥ `total`）。
  页数上限**已取消**（~~默认 3 页、上限 20 页~~ 被 §21 取代；用户口径原文
  「不要设置翻页上限，应为全量同步」）。
* **按账号限流（等待制）**：复用 `CrawlRateLimiter`，但用 **`wait_allow()`** ——
  额度用完**阻塞等到窗口腾出**再继续。⚠️ **只删页数上限而不改这里 = 每轮仍然只跑
  ~5 页**（额度 5 次/分钟、页间只 sleep 3 秒 ⇒ 20 秒烧完额度），问题原样存在。
* **页间停顿**：``PAGE_GAP_SECONDS``（上限 60s）。
* **绝对兜底**：``ABSOLUTE_PAGE_CAP``（500 页）—— 平台异常导致 `has_more` 恒真时
  必须能停下，否则「取消上限」= 死循环。
* **仍然禁**：不用 ``get_all_posted_notes`` / ``get_user_all_notes`` 这类
  **没有分页 / 限流插槽**的一次性全量方法（调用即失控）。**逐页翻到底不在此列。**
* **时长代价**（§21.5）：1961 条 creator ≈ 66 页 ≈ 13 分钟；三源串行，
  单次任务 30–45 分钟量级（改造前 ~20 秒）。

## Creator 增强（需求 R3）

Creator 账号每一页笔记落库后，把**本页 note_id** 批量交给
``shop_goods_service.sync_note_ids``（一次只读请求，≤50 个 id）落挂载商品 →
笔记与商品一个任务里一起完成，不拆成两个任务。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.account_types import SUB_TYPE_CREATOR
from backend.app.core.database import SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models.note import (
    NOTE_TYPE_SYNCED,
    PUBLISH_STATUS_PUBLISHED,
    Note,
    note_supports_batch_sync,
    note_supports_sync,
    set_note_publish_status,
)
from backend.app.models.platform_account import PlatformAccount
from backend.app.models.task import Task
from backend.app.services import sync_task_service as sts
from backend.app.services.account_service import (
    CHANNEL_CREATOR,
    CHANNEL_WWW,
    has_ark_token,
    has_creator_token,
)
from backend.app.services import note_queue_service
from backend.app.services.note_topic_service import apply_sync_topics
from backend.app.services.rate_limiter import get_rate_limiter
from backend.app.services import task_cancel

logger = logging.getLogger(__name__)

#: 页与页之间的停顿**基础值**（秒）。测试把它置 0 以避免真等待。
#: 2026-09-25 用户口径「后端同步时注意不要触发平台的限流，可以拉长间隔时间，
#: 随机化间隔时间」⇒ 3.0 → **5.0**；实际停顿还要乘抖动系数（见 `sync_pacing`），
#: 落在 [0.8, 1.6] × 基础值之间且不超过 `MAX_PAGE_GAP_SECONDS`。
PAGE_GAP_SECONDS = 5.0

#: 单页停顿的硬顶（设计 13 §8）。time.sleep 永远不会超过这个值。
MAX_PAGE_GAP_SECONDS = 60.0
SOURCE_CREATOR = "creator"
SOURCE_ARK = "ark"


class SyncFailure(Exception):
    """本次同步失败（原因可展示给用户，不含 Cookie 等敏感值）。"""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


#: 详情请求异常的**用户可读**文案分类（2026-09-25）。
#:
#: 为什么不全量透传异常字符串：底层是 curl_cffi，超时抛的是
#: `Failed to perform, curl: (28) Operation timed out after 8009 milliseconds with
#: 1152 bytes received. See https://curl.se/libcurl/c/libcurl-errors.html first for
#: more details.` —— 这段原文**只对排查有意义**，端到进度卡上就是噪音（用户实测
#: 反馈）。原文仍然进日志（`logger.warning`），库里/接口给的是下面这句人话。
_DETAIL_TIMEOUT_MARKERS = ("timed out", "timeout", "curl: (28)")
_DETAIL_NETWORK_MARKERS = (
    "curl: (6)",
    "curl: (7)",
    "curl: (28)",
    "curl: (35)",
    "curl: (52)",
    "curl: (56)",
    "connection",
    "resolve",
    "ssl",
    "network is unreachable",
    "proxy",
)


def _classify_detail_failure(exc: Exception) -> tuple[str, str]:
    """详情请求异常 → `(用户可读原因, 机器码)`。**原始报文只进日志**。"""
    text = str(exc)
    lowered = text.lower()
    if any(marker in lowered for marker in _DETAIL_TIMEOUT_MARKERS):
        return "详情请求超时（网络或平台响应慢），请稍后重试", "DETAIL_TIMEOUT"
    if any(marker in lowered for marker in _DETAIL_NETWORK_MARKERS):
        return "详情请求网络连接失败，请稍后重试", "DETAIL_NETWORK_ERROR"
    brief = " ".join(text.split())
    if len(brief) > 80:
        brief = brief[:80] + "…"
    return f"详情请求失败：{brief}", "DETAIL_REQUEST_FAILED"


def _build_pc_adapter(adapter_factory, cookies: str, account: Optional[PlatformAccount]):
    """构造 PC 适配器，**尽量把落库的平台 user_id 带进去**（§19.14 的加固）。

    带进去的收益：SDK 不再为「算 xy-direction」先发一次 `/v2/user/me`
    （那次请求自己还要先取 DS 签名材料，是最脆的一环）。取不到就照旧让 SDK 自己
    bootstrap —— 这是**加固**，不是新门禁：缺 `external_user_id` 不影响同步能不能跑。

    `TypeError` 兜底是给**测试替身**的（老替身只吃一个位置参数）；生产工厂
    `XhsPcApiAdapter` 显式支持 `user_id`。
    """
    external_user_id = str(getattr(account, "external_user_id", "") or "")
    try:
        return adapter_factory(cookies, user_id=external_user_id)
    except TypeError:
        return adapter_factory(cookies)


def _note_sources(db: Session, account: PlatformAccount) -> list[dict[str, Any]]:
    """本次同步的笔记源清单（§19.3.1 / §21.4）。**按 creator → ark 顺序**：

    creator 先跑（条目带正文潜力，走整行覆盖）；ark 后跑（填空合并，补商品与指标）。
    顺序写死在这里，不靠调用方传。**www 源已整体移除**（§21.4，2026-09-22）。

    可用性判据是**零请求**的 L2 口径（jar 形态），与登录态服务同一把尺子；
    能不能真取到由翻页时的第一次请求回答（DEF-057：不预判）。
    """
    sources: list[dict[str, Any]] = []

    # **强制前置条件：必须有创作者平台登录会话**（2026-09-22 用户口径原文：
    # 「同步笔记按钮该账号必须存在有效的创作者登录会话，不存在有效创作者平台
    # 登录会话的提示补齐后再触发」）。
    #
    # www 源**整体不再参与**（同日用户口径：「www 源在这里是冗余的，则移除不要，
    # 使用 creator 源」）：creator 源读的是同一批笔记且**更全**（含仅自己可见的），
    # 而 www 源额外要一个 URL 级 `xsec_token` —— 那不是登录令牌，是网页版主页 URL
    # 上的查询参数，账号卡片触发时拿不到 ⇒ 每轮白打一次平台请求 + 结果行里一条
    # 看起来像故障的 `XSEC_TOKEN_REQUIRED`。
    #
    # ⚠️ `_sync_source` 的 pc 分支**保留代码但当前无调用方** —— 它是 `_fetch_page`
    # 的合法源类型之一，要恢复 www 源时在这里加回三行即可（不要改 `_sync_source`）。
    creator_cookies = _account_cookies(db, account.id, channel=CHANNEL_CREATOR)
    if not has_creator_token(creator_cookies):
        # 返回空 ⇒ 上层给**结构化**原因（`CREATOR_SESSION_REQUIRED`）。
        # **刻意不退化成「用 ark 凑合」**：用户要的是「补齐后再触发」，
        # 而不是「哪个源能用就用哪个」—— 后者会让「没创作者会话」表现为
        # 「同步成功但只同步到商品笔记」。
        return []
    sources.append(
        {"source": SOURCE_CREATOR, "kind": "creator", "cookies": creator_cookies}
    )

    # ark：**只认账号自己的 ark 通道**（与 `_probe_ark` 同一口径）。2026-09-22 起
    # 「绑定表兜底」整条移除 —— 没有独立 ark 令牌的账号就是没有千帆源。
    from backend.app.services.shop_binding_service import qianfan_cookies_for_account

    ark_cookies = qianfan_cookies_for_account(db, account.id)
    if has_ark_token(ark_cookies):
        sources.append({"source": SOURCE_ARK, "kind": "ark", "cookies": ark_cookies})

    return sources


def _account_cookies(db: Session, account_id: int, *, channel: str = CHANNEL_WWW) -> str:
    """账号某平台最新一版 Cookie（解密后转成请求头用的字符串）。

    **后台线程没有请求上下文**，所以这里只按 ``platform_account_id`` 取，
    不做归属校验（调用方在 API 层已经校验过）。

    ``channel`` 必须显式给（§11.5）：同一账号下三个平台的令牌**互不覆盖**，
    按「最新一版」猜会拿到另一个平台的 jar —— 拿创作者 jar 打 www、或拿 www 的
    打创作者接口，平台都只会回「缺失登录凭证」，而错误信息完全指错方向。
    """
    from backend.app.services.account_service import latest_cookies_for_channel

    return latest_cookies_for_channel(db, account_id, channel)


def _store_notes_count(db: Session, account: PlatformAccount, total: Optional[int]) -> None:
    """把平台总数写进 ``profile_json.notes_count``（设计 13 §3.4 / D9）。

    「有待同步」的分母。**缺值不写**（保持原样，不造 0）—— 硬约束⑤。
    `profile_json` 是 **Text 列存 JSON 串**（不是 JSON 列），所以这里与
    ``account_service.merge_metrics_into_profile`` 同一手法：读→改→写回字符串，
    并且**只在真的变了才写**（避免无意义的 UPDATE）。
    """
    if not total:
        return
    try:
        profile = json.loads(account.profile_json or "{}")
        if not isinstance(profile, dict):
            profile = {}
    except (TypeError, json.JSONDecodeError):
        profile = {}
    value = int(total)
    if profile.get("notes_count") == value:
        return
    profile["notes_count"] = value
    account.profile_json = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
    account.updated_at = shanghai_now()


def _sync_goods_for_notes(db: Session, account: PlatformAccount, items: list[dict[str, Any]]) -> int:
    """Creator：本页 note_id → 千帆挂载商品落库（R3）。返回落库条数。

    失败**不中断笔记落库**（商品缺，笔记仍在）—— 与 ``sync_note_ids`` 自身的
    「失败静默」纪律一致；凭据失效由它把账号的千帆状态标 ``invalid``。

    **顺带解析「本地笔记 ↔ 本地商品」关系**（§26）：挂载商品落进证据缓存后，
    用同一个解析器（`link_note_goods`，纯本地、零平台请求）把关系写进
    `note_goods`。失败同样不中断笔记落库。
    """
    note_ids = [str(item.get("note_id") or "").strip() for item in items]
    note_ids = [note_id for note_id in note_ids if note_id]
    if not note_ids:
        return 0

    from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory
    from backend.app.services.shop_binding_service import account_shop_id
    from backend.app.services.shop_goods_service import sync_note_ids

    if account_shop_id(db, account.id) is None:
        return 0
    try:
        result = sync_note_ids(db, account, note_ids, get_qianfan_api_adapter_factory())
    except Exception as exc:  # noqa: BLE001 - 商品补齐失败不影响笔记落库
        logger.warning("note sync: goods backfill failed (account=%s): %s", account.id, exc)
        return 0

    # §26：证据缓存刚落库 ⇒ 就地解析本地关系（只读缓存 + 本地两张表，零平台请求）。
    try:
        from backend.app.services.shop_goods_service import cached_goods_mappings, link_note_goods

        link_note_goods(db, account, cached_goods_mappings(db, account.id, note_ids))
    except Exception as exc:  # noqa: BLE001 - 关系解析失败不影响笔记落库
        logger.warning("note sync: note-goods link failed (account=%s): %s", account.id, exc)
    return int(result.get("stored") or 0)


def _link_ark_note_goods(db: Session, account: PlatformAccount, items: list[dict[str, Any]]) -> None:
    """ark 源：条目**自带**的 `goods_infos` → 本地关系（§26）。

    与 creator 源的分工：creator 源的关系来自 `sync_note_ids` 刚落库的**证据缓存**
    （平台按 note_ids 现查）；ark 源的挂载商品**随列表条目 inline 给出**
    （`goods-note/list` 实测字段），不需要再打一次平台。两条路汇到同一个解析器
    `link_note_goods`（同一语义不留两份实现）。
    """
    mappings: list[tuple[str, list[dict[str, Any]]]] = []
    for normalized in items:
        raw = normalized.get("raw") if isinstance(normalized.get("raw"), dict) else {}
        goods = raw.get("goods_infos") if isinstance(raw.get("goods_infos"), list) else []
        note_id = str(normalized.get("note_id") or "").strip()
        if note_id and goods:
            mappings.append((note_id, [g for g in goods if isinstance(g, dict)]))
    if not mappings:
        return
    try:
        from backend.app.services.shop_goods_service import link_note_goods

        link_note_goods(db, account, mappings)
    except Exception as exc:  # noqa: BLE001 - 关系解析失败不影响笔记落库
        logger.warning("note sync: ark note-goods link failed (account=%s): %s", account.id, exc)


def _is_hex_note_id(note_id: str) -> bool:
    """笔记 id 只认「全 hex 且 ≥16 位」（与 `_note_id_from_url` 同一判据）。"""
    value = str(note_id or "").strip()
    return len(value) >= 16 and all(char in "0123456789abcdefABCDEF" for char in value)


def _note_detail_url(note: Any) -> str:
    """详情请求要用的 URL：**必须带**笔记级 `xsec_token`（§19.13 / §19.13.9）。

    ⚠️ **无令牌一律返空串**（2026-09-28 用户口径：「没有令牌强行拦截，让用户补齐
    登录令牌」）：平台对无 token 的 explore 链接回 302 → `/404?error_code=300031`，
    **即使带会话也拿不到**（§19.13.8 真机实测，匿名 / 登录态都是这个结果）。旧实现
    曾在**单条同步**路径用 `allow_tokenless` 兜底拼 explore 地址 —— 那是 DEF-053
    「假成功」的入口，已整段移除。调用方拿到空串即结构化拦截。

    `raw_json` 里另存了 token 就补进 URL：千帆条目（顶层 `xsec_token`）与创作者
    列表项都在自己那层带着它。
    """
    raw = note.raw_json if isinstance(note.raw_json, dict) else {}
    url = str(raw.get("note_url") or "").strip()
    if "xsec_token=" in url:
        return url
    token = str(raw.get("xsec_token") or "").strip()
    if not token:
        ark = raw.get("ark") if isinstance(raw.get("ark"), dict) else {}
        token = str(ark.get("xsec_token") or "").strip()
    if not token or not _is_hex_note_id(note.note_id):
        # 无令牌（可自救 ⇒ 由调用方拦截并指引补齐登录）／note_id 非平台形态
        # （拼出来平台也不认）⇒ 一律返空，不拼「看起来能用」的地址。
        return url
    if url:
        return f"{url}{'&' if '?' in url else '?'}xsec_token={token}"
    return f"https://www.xiaohongshu.com/explore/{str(note.note_id).strip()}?xsec_token={token}"


def backfill_note_detail_ref(db: Session, note: Any, fetch: Any) -> bool:
    """把**详情地址**（explore URL + 笔记级 `xsec_token`）补进 `raw_json`（§25.4）。

    场景：本地生成笔记发布成功后，平台 `note/publish` 响应只回 `{"id"}`，
    **不带** `note_url` / `xsec_token` ⇒ `raw_json` 没有详情地址 ⇒ 内容库
    「同步」按钮的 `_note_detail_url` 判 `no_url`，用户看到「这条笔记没有可用的
    详情地址」—— 按钮在、功能不在（真实库 id 1967「中古风桌旗…」）。

    做法：用千帆商品笔记列表的 `note_ids` **精确查询**（与
    `shop_goods_service.sync_note_ids` 同一端点）取回该项的 `xsec_token`，
    拼成 explore URL 落进 `raw_json`（顶层 `note_url` + `xsec_token`，与
    `_normalize_ark_note` 同形态）。之后**所有**链路（单条同步 / 批量同步 /
    定时补齐）都拿到带 token 的完整地址。

    ``fetch``：一次只读查询的注入点（`callable(note_ids) -> (ok, msg, payload)`，
    与适配器三元组同形）—— 发布路径传发布适配器的 `get_goods_note_page`
    （凭据已在手），单条同步路径传用账号自己 `ark` 令牌构造的同一个方法
    （§18.14：千帆只认本账号 ark）。异常 / 平台报错 / 查无项都返回 False
    （**best-effort，调用方不抛**）。

    ⚠️ **不 commit**：事务边界归调用方（发布路径在成功回写事务里，同步路径
    单独提交）。返回 True 表示 `note.raw_json` 已被修改、调用方必须提交。

    单篇版是**批量版的特例**（委托 :func:`backfill_note_detail_refs`）——
    批量同步时一篇触发、整批一次请求补全（DEF-069 提速，见该函数 docstring）。
    """
    return backfill_note_detail_refs(db, [note], fetch) > 0


def backfill_note_detail_refs(db: Session, notes: list[Any], fetch: Any) -> int:
    """**批量**补详情地址：一次 `note_ids` 精确查询补全整批（§25.4，2026-09-25 提速）。

    为什么需要批量版：批量同步 N 篇本地生成笔记时，旧实现每篇各回查一次千帆
    ⇒ N 次请求（每次 1-2 秒）。而 `get_goods_note_page(note_ids=[...])` 本身
    支持一次查多个（与 `shop_goods_service.sync_note_ids` 同参数）⇒ 合并为
    **一次请求**。由 worker 在「本篇缺地址」时触发，把当时队列里同样缺地址的
    篇一起补上（一篇触发、全批受益）—— 单条按钮路径走单篇版，行为不变。

    只补**缺地址**的行（已有 `note_url` 的不碰）；返回补齐篇数（调用方据此
    决定要不要 commit）。hex 判据是 `_note_detail_url` 拼 explore 的关切；
    这里只要求有平台 id（真实平台恒为 hex，替身 / 历史行不保证）。
    """
    pending = []
    for note in notes:
        raw = note.raw_json if isinstance(note.raw_json, dict) else {}
        if str(raw.get("note_url") or "").strip():
            continue
        note_id = str(note.note_id or "").strip()
        if note_id:
            pending.append((note, note_id))
    if not pending or fetch is None:
        return 0
    try:
        ok, _message, payload = fetch([note_id for _note, note_id in pending])
    except Exception as exc:  # noqa: BLE001 - 补地址失败不影响调用方主流程
        logger.info(
            "note sync: detail ref backfill skipped (%d notes): %s", len(pending), exc
        )
        return 0
    if not ok or not isinstance(payload, dict):
        return 0
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    entries = [e for e in (data.get("note_infos") or []) if isinstance(e, dict)]
    tokens = {
        str(e.get("note_id") or "").strip(): str(e.get("xsec_token") or "").strip()
        for e in entries
    }
    filled = 0
    for note, note_id in pending:
        token = tokens.get(note_id) or ""
        if not token:
            continue
        merged = dict(note.raw_json if isinstance(note.raw_json, dict) else {})
        merged["note_url"] = (
            f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={token}"
        )
        merged["xsec_token"] = token
        note.raw_json = merged
        filled += 1
    if filled:
        logger.info("note sync: detail ref backfilled (%d/%d notes)", filled, len(pending))
    return filled


def _backfill_detail_ref_for_account(db: Session, account: PlatformAccount, note: Any) -> bool:
    """用**账号自己的 `ark` 令牌**回查商品笔记列表，补详情地址（§25.4 / §18.14）。

    千帆凭据只认本账号 `ark`（`qianfan_cookies_for_account`，无令牌返空串）——
    拿不到就静默跳过（调用方有 explore 兜底），**不借任何别的凭据**、不发探活。
    """
    return _backfill_detail_ref_for_accounts(db, account, [note]) > 0


def _backfill_detail_ref_for_accounts(
    db: Session, account: PlatformAccount, notes: list[Any]
) -> int:
    """批量版：一次 `note_ids` 精确查询补全整批（DEF-069 提速，2026-09-25）。

    批量同步 N 篇本地生成笔记时，逐篇回查 = N 次千帆请求（1-2 秒/次）；
    合并为一次后，worker 在「本篇缺地址」时把队列里同样缺地址的篇一起补上
    （一篇触发、全批受益）。单条按钮路径走单篇版，行为不变。
    """
    from backend.app.services.shop_binding_service import qianfan_cookies_for_account

    cookies = qianfan_cookies_for_account(db, account.id)
    if not cookies:
        return 0
    try:
        from backend.app.api.platforms.xhs.qianfan import (
            get_qianfan_api_adapter_factory,
        )

        adapter = get_qianfan_api_adapter_factory()(cookies)
        goods_page = getattr(adapter, "get_goods_note_page", None)
    except Exception as exc:  # noqa: BLE001 - 工厂取不到就跳过，不阻路
        logger.info("note sync: detail ref backfill factory failed: %s", exc)
        return 0
    if goods_page is None:
        return 0

    def _fetch(note_ids: list[str]):
        # 与 `shop_goods_service.sync_note_ids` 同一组参数（note_ids 精确查询）。
        return goods_page(page_num=1, page_size=len(note_ids), note_ids=note_ids)

    filled = backfill_note_detail_refs(db, notes, _fetch)
    if filled <= 0:
        return 0
    db.commit()
    return filled


def _merge_note_images(db: Session, note: Any, urls: list[Any]) -> int:
    """把详情里的图**补**进本地资产：只加本地没有的 url，不删不改已有（填空语义）。

    ⚠️ **实现委托给 `crawl._merge_note_assets`**（§23）：落库链路与详情补齐链路
    的「只增不删」必须是**同一份代码**。旧实现在这里各写一份，两边对
    「什么算重复」的判断已经不一致（一个只看 url、一个看 (url, asset_type)）——
    正是 DEF-044/047/058 反复吃的亏。
    """
    from backend.app.api.platforms.xhs.crawl import _merge_note_assets

    return _merge_note_assets(db, note, [str(u) for u in urls], asset_type="image")


def hydrate_note_detail(
    db: Session,
    account: PlatformAccount,
    note: Any,
    *,
    account_cookies: Optional[str] = None,
    adapter: Any = None,
    adapter_factory: Any = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """对**一条**已落库笔记打一次详情：补正文 + 补缺失图片（设计 13 §19.13 / §19.14）。

    这是「详情补齐」的**唯一实现** —— 同步链路的页内联补齐（`_hydrate_page_notes`，
    §19.14）与内容库「同步」按钮（`sync_note_from_platform`）都调这里；写两份必然漂移
    （DEF-044/047/058 的教训）。

    为什么必须打详情：三个源的**列表接口都不带正文**，图片也可能只有一张封面
    （千帆 `goods-note/list` 只给 `note_image`）。真机实测（只读 GET 详情）同一条
    笔记能给出 **481 字正文 + 4 张图** —— 数据一直都在，只是没取。

    ``refresh``（设计 13 §25.3）区分两种落库语义，**其余纪律逐字相同**：

    * ``False``（默认，同步链路补齐）：正文**只在本地为空时写** —— 列表源恒不带
      正文，「归一化后 content 为空」是固有形态，无条件覆盖会把详情补过的正文抹掉
      （§24.3）；
    * ``True``（内容库「同步」按钮）：详情拿到**非空**正文档期就**覆盖**本地 ——
      平台改过正文时本地必须跟上（「拉当前笔记最新得内容更新落库」）。详情正文为
      空时**不碰本地**（不抹掉已落库的正文）。标题同理（非空才覆盖）。

    纪律：

    * **图片只增不删**（两种模式一致）：只补本地没有的 url，不删不改已有；
    * **正文按模式落地**：见上文 ``refresh`` 说明（同步链路 = 填空；「同步」按钮 =
      刷新），共同底线是**详情正文为空时不抹本地**；
    * **话题拆分**（§34.2，2026-09-25）：正文里的 `#话题` 摘进 `topics` 表 +
      `note_topics` 关系（正文不再存话题）；平台声明的 ``tags`` 是权威、正文解析
      是兜底；话题 / 关系**只增不删**（唯一实现 ``apply_sync_topics``）；
    * **`hydrated_at` = 详情取到的标记**：批量同步（页内联，§19.14）只挑
      `hydrated_at IS NULL` 或「视频笔记却没有视频资产」的行 ⇒ 失败的行**不写**标记，
      下次**全量同步**自然重试（与商品侧「详情失败 ⇒ `detail_synced_at` 留空、下轮重试」
      同口径）。想立刻重试的用户路径是**内容库详情卡片的「同步」按钮** —— 那条路径不看标记；
    * **失败静默**：拿不到就保持原样（笔记与已落库的正文都在），原因放在返回值里。
    * **取数路由**（§19.13.8，2026-09-28）：详情**先走 explore SSR**（`get_note_info_html`，
      一次 GET 拿全、不挑令牌来源），拿不到再回退 JSON 端点 `/v1/feed`。两条路共用本函数
      的归一化 / 状态 / 落库纪律 —— **不写第二份实现**；替身没实现 SSR 方法时自动只走
      JSON 端点（行为与改造前逐字一致）。

    **无令牌 ⇒ `no_token`（不写 `hydrated_at`）**（2026-09-28 用户口径：「没有令牌
    强行拦截，让用户补齐登录令牌」）：`_note_detail_url` 拿不到带 token 的地址时，
    若 `note_id` 是**平台形态**（hex）就只可能是「缺令牌」—— 那是**用户可自救**的
    （补齐网页版登录 / 重新同步该笔记）⇒ 不写标记、下次同步重试；只有 `note_id`
    非平台形态（本地临时 id，拼了平台也不认）才走 `no_url` 写标记出队。
    旧实现的 `allow_tokenless`（单条同步拼无 token 的 explore 地址）已删除：平台对
    无 token 的链接一律 302 → `/404`（§19.13.8 真机实测），那是 DEF-053「假成功」入口。

    ``hydrated_at`` 写不写**按状态区分**（既不是一律写、也不是一律不写）：

    | status | 含义 | 写标记？ | 为什么 |
    |---|---|---|---|
    | `ok` | 打过详情且平台返回成功 | ✅ 写 | 已补齐，出队 |
    | `failed` | 请求异常 / 平台报错 | ❌ **不写** | **可恢复**：下次全量同步重试。§19.14 起账号级队列已移除，「失败也写以防饿死」的前提（队列）不存在，写了反而让一次瞬时失败永久跳过这篇 |
    | `no_token` | 本地缺详情令牌（**用户可自救**） | ❌ **不写** | 补齐网页版登录后这些行还要能被补（与 `no_session` 同口径） |
    | `no_url` | `note_id` 非平台形态、无详情地址 | ✅ 写 | 本地数据缺失、**不会自愈**，重试无意义 |
    | `no_session` | 账号缺 www 会话 | ❌ 不写 | **账号级、可恢复**：用户补登 www 后这些行还要能被补 |
    """
    from backend.app.api.platforms.xhs.pc import (
        _has_web_session,
        _normalize_detail_payload,
    )

    result: dict[str, Any] = {
        "status": "failed",
        "content_filled": False,
        "content_updated": False,
        "title_updated": False,
        "images_added": 0,
        "video_added": False,
        "reason": "",
        # §34：话题拆分结果（正文摘除 + 关系行新增数）。
        "topics_linked": 0,
        "content_stripped": False,
    }
    url = _note_detail_url(note)
    if not url:
        if _is_hex_note_id(note.note_id):
            # 平台形态的 note_id 却拿不到带令牌的地址 ⇒ **缺令牌**。用户可自救
            # （补齐登录 / 重新同步）⇒ **不写** `hydrated_at`，下次同步重试。
            result.update(
                status="no_token",
                reason=(
                    "这条笔记缺详情令牌（xsec_token），无法读取正文；"
                    "请补齐该账号的网页版登录后重新同步"
                ),
                code="NOTE_DETAIL_TOKEN_REQUIRED",
            )
            return result
        # note_id 非平台形态（本地临时 id）⇒ 拼出来平台也不认、**不会自愈** ⇒ 写标记：
        # 留着只会永远占住「待补齐」剩余计数（用户看到那个数字永不下降）。
        # 与 `no_token` / `no_session` 的「不写」刻意相反，理由见 docstring 的状态表。
        note.hydrated_at = shanghai_now()
        db.commit()
        result.update(status="no_url", reason="这条笔记没有可用的详情地址")
        return result

    if account_cookies is None:
        account_cookies = _account_cookies(db, account.id, channel=CHANNEL_WWW)
    if not _has_web_session(account_cookies):
        # 「暂不可用」不是「尝试过」：**不写** `hydrated_at`，用户补登 www 后仍会被补。
        result.update(
            status="no_session",
            reason="该账号没有可用的网页版（www）会话，无法读取笔记详情",
        )
        return result

    if adapter is None:
        if adapter_factory is None:
            # 走项目统一的工厂（模块级、可替换）：测试替身进得来，不会打真网络
            # （DEF-048 的教训：探针的工厂必须依赖注入，不能直接用模块级类）。
            from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory

            adapter_factory = get_xhs_pc_api_adapter_factory()
        adapter = _build_pc_adapter(adapter_factory, account_cookies, account)

    # 详情取数路由（设计 13 §19.13.8）：① explore SSR（一次 GET 拿全、不挑令牌来源）
    # → ② JSON 端点 `/v1/feed`（与改造前**逐字一致**）。两条路返回同形三元组。
    # ⚠️ SSR 方法用 `getattr` 取：既有测试注入的替身没有它 ⇒ 自动只走 JSON 端点，
    # 不必逐个改替身（也保证「替身没实现新能力」时行为退回改造前）。
    ssr_fetch = getattr(adapter, "get_note_info_html", None)
    success: Any = False
    message = ""
    raw_payload: Any = None
    if callable(ssr_fetch):
        try:
            success, message, raw_payload = ssr_fetch(url)
        except Exception as exc:  # noqa: BLE001 - SSR 取不到就回退，不在这里判死
            logger.info(
                "note sync: explore SSR 详情异常（%s），回退 /v1/feed：%s", note.note_id, exc
            )
            success, message, raw_payload = False, str(exc), None
        if not success:
            logger.info(
                "note sync: explore SSR 未取到详情（%s）：%s，回退 /v1/feed",
                note.note_id,
                message,
            )

    if not success:
        try:
            success, message, raw_payload = adapter.get_note_info(url)
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响其余
            logger.warning("note sync: detail hydrate failed (%s): %s", note.note_id, exc)
            # ⚠️ 失败**不写** `hydrated_at`（§19.14）：账号级队列已移除，「防饿死」的
            # 前提不存在；留着 NULL ⇒ 下次全量同步重试（商品详情失败同口径）。
            reason, code = _classify_detail_failure(exc)
            result.update(reason=reason, code=code)
            return result

    if not success:
        # 同上：平台报错是**可恢复**失败，不写标记，下次全量同步重试。
        result.update(reason=str(message or "详情接口返回失败"))
        return result

    # ⚠️ §19.13.5（2026-09-27 真机实测）：「`success=True` 但没拿到笔记对象」
    # **不是**「这条笔记没有正文」。平台在令牌无效 / 笔记不可见时回
    # `{"code":0,"success":true,"data":{},"msg":""}` —— items 为空。旧实现把它
    # 当 `ok` ⇒ 写 `hydrated_at` ⇒ **永久跳过**（「同步任务跑完、正文永远没有」的
    # 直接原因，DEF-053「假成功」第三次复发）。
    #
    # 判据刻意用「有没有 items」而不是「正文是否为空」：**空正文的纯图片笔记真实
    # 存在**（拿到对象、正文就是空），那种写标记是对的（不会自愈）；而「没拿到
    # 对象」是**可恢复**的取数失败（令牌可能已过期 ⇒ 重新同步 / 补登录后重试），
    # 必须留 NULL 让下次同步重试。
    detail_data = (raw_payload or {}).get("data") if isinstance(raw_payload, dict) else None
    detail_items = detail_data.get("items") if isinstance(detail_data, dict) else None
    if not detail_items:
        result.update(
            status="failed",
            reason="平台未返回笔记内容（详情令牌可能已失效或该笔记不可见）",
            code="DETAIL_EMPTY",
        )
        return result

    note.hydrated_at = shanghai_now()

    normalized = _normalize_detail_payload(raw_payload, url)
    content = str(normalized.get("content") or "")
    if refresh:
        # 刷新语义（§25.3，内容库「同步」按钮）：详情有非空正文 ⇒ 覆盖本地
        # （平台改过正文时本地要跟上）；详情正文为空 ⇒ 不碰本地（不抹已落库正文）。
        if content and content != str(note.content or ""):
            note.content = content
            result["content_updated"] = True
        title = str(normalized.get("title") or "")
        if title and title != str(note.title or ""):
            note.title = title
            result["title_updated"] = True
    elif content and not str(note.content or "").strip():
        # 填空语义（§19.13，同步链路）：正文只在本地为空时写。
        note.content = content
        result["content_filled"] = True
    # 话题（§34.2）：正文里的 `#话题` 拆进 `topics` 表 + `note_topics` 关系，
    # 正文不再存话题（用户 2026-09-25 口径）。平台声明的 `tags` 是权威，正文解析
    # 是兜底；话题 / 关系**只增不删**（`apply_sync_topics` 唯一实现）。
    detail_topics = [str(t).strip() for t in (normalized.get("tags") or []) if str(t).strip()]
    topic_outcome = apply_sync_topics(db, note, detail_topics)
    result["topics_linked"] = topic_outcome["relations_added"]
    result["content_stripped"] = topic_outcome["content_stripped"]
    result["images_added"] = _merge_note_images(db, note, normalized.get("image_urls") or [])
    # ⚠️ **视频流地址也必须落库**（2026-09-22 修）：列表端点只给 `video_info.duration`、
    # 不带流地址（真机实测），可播放地址**只有详情有**。旧实现拿到了 `video_url`
    # 却只 merge 图片 ⇒ 视频地址被丢掉 ⇒ 内容库详情只能看封面、播不了。
    video_url = str(normalized.get("video_url") or normalized.get("video_addr") or "")
    if video_url:
        # 与 `_merge_note_images` 同一份「只增不删」实现（住在 crawl，避免两份语义）。
        from backend.app.api.platforms.xhs.crawl import _merge_note_assets

        _merge_note_assets(db, note, [video_url], asset_type="video")
        result["video_added"] = True
    db.commit()
    result["status"] = "ok"
    return result


def sync_note_from_platform(
    db: Session,
    note: Any,
    *,
    account: Optional[PlatformAccount] = None,
    adapter_factory: Any = None,
) -> dict[str, Any]:
    """内容库「同步」按钮的**单条刷新**：用笔记 id 拉平台最新内容，更新落库（§25.3–25.4）。

    与定时任务同步笔记逻辑**同源**，落地方式是复用同一批函数而不是抄一份：

    * 详情拉取 + 落库合并委托 `hydrate_note_detail(refresh=True)` —— 同步链路的
      页内联补齐（`_hydrate_page_notes`）与这里用的是**同一个函数**；
    * 限流同一把尺子：`wait_allow(f"{account.id}:www")`，**等待不即停**（§21.3）；
    * 资产合并、`hydrated_at` 三态、`platform_missing_at` 自愈（详情成功 ⇒ 平台
      此刻有这条 ⇒ 清标记）与同步链路逐字相同。

    **为什么不套 creator 硬门禁（§21.4）**：本动作的数据源是 **www 详情端点**，
    全程不碰创作者平台；缺 www 会话 ⇒ `no_session` + `WEB_SESSION_REQUIRED`
    （前端安静一行指引补登网页版），与定时链路 hydrate 步骤的口径一致。

    **缺详情令牌 ⇒ 拦截，不翻 `user_posted` 兜底**（§19.13.9，2026-09-28 用户口径
    「移除兜底，不允许出现翻页到底；没有令牌强行拦截，让用户补齐登录令牌」）：
    `raw_json` 里拿不到 `xsec_token` 时先做一次 **ark 精确回查**（按 `note_ids` 查
    商品笔记列表，**不是翻全表**）；仍拿不到 ⇒ `no_token` +
    `NOTE_DETAIL_TOKEN_REQUIRED`，**一次平台请求都不再发**。旧实现会在这里翻
    66–200 页去赌令牌出现，已整段删除。

    **不建任务行、不轮询**：一次详情请求秒级返回。⚠️ 尤其不能用 `xhs_note_sync`
    任务行 —— 账号卡片轮询的正是那个状态，库里点一下同步就让卡片显示「同步中」是误导。

    返回 `{"status", "content_updated", "title_updated", "images_added",
    "video_added", "reason", "code"}`；`status` 与 `hydrate_note_detail` 同源
    （`ok` / `no_token` / `no_url` / `no_session` / `failed`）。**不向上抛异常** ——
    预期内的取不到是结果数据，不是接口异常（§25.4）。
    """
    result: dict[str, Any] = {
        "status": "failed",
        "content_updated": False,
        "title_updated": False,
        "images_added": 0,
        "video_added": False,
        "reason": "",
        "code": "",
    }
    if account is None:
        account = (
            db.get(PlatformAccount, note.platform_account_id)
            if note.platform_account_id
            else None
        )
    if account is None:
        result.update(
            status="failed",
            reason="这条笔记没有关联账号，无法同步",
            code="NOTE_ACCOUNT_REQUIRED",
        )
        return result

    limiter = get_rate_limiter()
    if not limiter.wait_allow(f"{account.id}:www"):
        result.update(
            status="failed",
            reason="平台请求额度已满（等待 2 分钟仍未腾出），请稍后再试",
            code="RATE_LIMITED",
        )
        return result

    # 本地没有详情地址时，先用账号自己的 ark 令牌回查一次商品笔记列表补上
    # （§25.4：平台 publish 响应只回 id）。⚠️ 这是**按 note_ids 的精确查询**，
    # 不是翻全表 —— 令牌缺失的自救路径就靠它。
    if not _note_detail_url(note):
        _backfill_detail_ref_for_account(db, account, note)

    # ⚠️ **无令牌 ⇒ 拦截，不再翻 `user_posted` 兜底**（2026-09-28 用户口径：
    # 「移除兜底，不允许出现翻页到底；没有令牌强行拦截，让用户补齐登录令牌」）。
    #
    # 沿革：§19.13.5 要求「打详情前必刷网页侧令牌」（每篇翻 66–200 页）→ §19.13.8
    # 改成「本地有令牌就不刷，只在完全没令牌时兜底」→ **本轮把兜底整段删除**。
    # 那条兜底是**唯一**会翻全表的地方，而它要救的场景（本地生成 + 已发布笔记）
    # 已由上面的 ark 精确回查覆盖。
    #
    # 拦的是**取数前提**而不是失败：缺令牌是用户可自救的（网页版登录 / 重新同步
    # 该笔记），所以给结构化 code + 可执行指引，不给「平台未返回内容」这种用户
    # 无法行动的归因（DEF-058）。
    if "xsec_token=" not in _note_detail_url(note):
        result.update(
            status="no_token",
            reason=(
                "这条笔记本地没有详情令牌（xsec_token），无法读取正文。"
                "请先在网页版（xiaohongshu.com）登录该账号后重新同步这条笔记；"
                "若为本地生成笔记，请先在商品笔记列表同步一次以补齐令牌。"
            ),
            code="NOTE_DETAIL_TOKEN_REQUIRED",
        )
        return result

    outcome = hydrate_note_detail(
        db, account, note, refresh=True, adapter_factory=adapter_factory,
    )
    result.update(
        status=outcome["status"],
        content_updated=bool(outcome.get("content_updated")),
        title_updated=bool(outcome.get("title_updated")),
        images_added=int(outcome.get("images_added") or 0),
        video_added=bool(outcome.get("video_added")),
        reason=str(outcome.get("reason") or ""),
    )
    if outcome["status"] == "no_session":
        # 前端安静降级白名单里的既有 code（不新造）。
        result["code"] = "WEB_SESSION_REQUIRED"
    elif outcome["status"] == "ok" and note.platform_missing_at is not None:
        # 自愈（§25.3）：详情请求成功 ⇒ 这条笔记此刻在平台上存在（比列表枚举更强
        # 的证据）⇒ 对账标记清回 NULL。
        note.platform_missing_at = None
        db.commit()
    return result


# --------------------------------------------------------------------------- #
# 批量同步：单 worker FIFO 队列（设计 13 §35，2026-09-25 用户口径）
#
# 用户口径原文：「内容库新增一个同步进度卡，新增支持多选批量同步笔记，只有已发布的
# 支持被选中，后端使用队列，按顺序同步，并统计数据到进度卡，已同步数/同步进度比/总数」。
#
# **队列持久化（§35.9，2026-09-25 用户口径「应用退出再次启动后可继续执行」）**：
# 原来是**纯内存** deque —— 进程一退、没跑完的篇就丢了。现在队列项的**唯一真相源
# 是 `tasks` 表**（`note_queue_service`，复用不造表），内存里只剩一个唤醒信号
# `_sync_wakeup`（不是队列，是「可能有活干了」的提示）；worker 每次醒来都去 DB
# `claim_next_item()` 认领。⇒ 重启后未跑完的篇仍在库里，由启动恢复重新入队。
#
# ⚠️ **不做内存 + DB 双写**：那是两份真相源（DEF-044 级），一致性迟早崩。
# 内存结构（deque / results dict / running 元组）本轮**整段删除**，不留影子。
#
# 与发布队列（`note_publish_service`，§31.5）**同构不同责**：
#   * 同一个形态：FIFO + 单 worker + 快照 + 路由注册在 `/{note_id}` 之前；
#   * 不同的活：这里逐篇跑 `sync_note_from_platform`（**唯一实现复用** —— 详情拉取、
#     合并落库、限流等待、hydrated_at 三态与单条按钮、定时任务同一把尺子），
#     不新写一份同步逻辑（DEF-044/047/058）。
#   * 不同的账：同步失败**不落库到笔记行**（没有 publish_error 那样的持久位），
#     结果在 `tasks` 行的 `payload` 里（快照 `finished` 逐篇给出）。
# --------------------------------------------------------------------------- #

#: 批量同步单次上限（篇）。与发布批次（`BatchPublishNotesRequest.max_items`）同值：
#: 批量是显式动作，超限分多次即可；同步单篇 = 一次详情请求（额度按账号读运行时配置，
#: 默认 60 次/分钟 ⇒ 20 篇 ≈ 20 秒的平台请求 + 落库开销），取 20 与发布批次对齐。
#: ⚠️ 2026-09-27 前这里写的是「www 额度 5 次/分钟 ⇒ 20 篇 ≈ 4 分钟」——那个 5 是
#: 代码里写死的值，不是平台额度（设计 13 §38.1）。
SYNC_BATCH_MAX_ITEMS = 20

#: worker 空闲时的兜底巡检间隔（秒）。唤醒信号是「提示」不是「唯一入口」：
#: 一旦 `_sync_wakeup` 的 set 因任何原因没被等到，最坏情况也只是**晚这么久**发现
#: 新任务，不会永久停摆（DEF-048「安全网不能条件性」）。
SYNC_IDLE_POLL_SECONDS = 5.0

#: worker 线程名（探针与重建都认它；DEF-067 同型：存活判据只能是**线程在不在**）。
#: ⚠️ 池化（设计 13 §38.3）后：第 1 条**保持这个原名**（既有探针 / 测试 / 日志
#: 全都不改），第 2..N 条是 `note-batch-sync-worker#2` … `#N`；
#: 探针一律按**前缀**匹配（`thread_name_matches_sync_worker`）。
SYNC_WORKER_THREAD_NAME = "note-batch-sync-worker"
#: 池大小上限（配置可调，但要有个硬顶：手滑填 200 会同时打 200 个账号）。
SYNC_MAX_PARALLELISM = 8
#: 并行度默认值（与 `config/default.yaml` 的 `xhs.sync.account_parallelism` 一致）。
SYNC_DEFAULT_PARALLELISM = 3

#: 队列内存态保护锁：本轮起只保护「停止位 + 唤醒位 + 池成员表」的复合读写。
_sync_queue_lock = threading.Lock()
#: 唤醒信号（**不是队列**）：入队后 set，worker 醒来去 DB 认领。
_sync_wakeup = threading.Event()
#: worker 收尾 / 测试专用出口（置位 + set wakeup ⇒ 排空即退；生产永不置位）。
_sync_worker_stop = threading.Event()
#: **主 worker**（池里第 1 条）。保留这个单数变量是为了兼容既有探针 / 测试 /
#: `_revive_sync_worker` 的判据；池的完整成员表是 `_sync_workers`。
_sync_worker: Optional[threading.Thread] = None
#: 池成员表（含主 worker；权威的池大小判据）。
_sync_workers: list[threading.Thread] = []
#: **每线程**停止位（降并行度时要能只收掉多余的那几条）。
#: 键 = 线程名；缺项 = 该线程只受全局停止位约束（测试自建线程走这条）。
_sync_worker_stops: dict[str, threading.Event] = {}
#: 各 worker **当前持有的账号**（`payload.account_id`；`None` = 空闲）。
#: 这是账号级互斥的**唯一**依据（设计 13 §38.3）—— 同账号至多 1 条在跑。
_sync_busy_accounts: dict[str, Optional[int]] = {}
#: 各 worker **当前持有的队列项 id**（看门狗复位中断行时要跳过它们，见
#: `note_queue_service.recover_interrupted` 的 `exclude_task_ids`）。
_sync_held_items: dict[str, int] = {}


def _alive_worker_names() -> set[str]:
    """活着成员的线程名集合（判定「占用是否还有主」用）。"""
    return {thread.name for thread in _alive_sync_workers()}


def _busy_account_ids() -> set[int]:
    """**当前被占用**的账号 id 集合（账号级互斥的输入）。

    ⚠️ 只算**活着**的线程：死线程留下的占用必须失效，否则那个账号会被永久
    「占住」—— 队列里它的项再也没人认领（比死锁更隐蔽：界面显示「排队中」却永不开始）。
    """
    alive = _alive_worker_names()
    return {
        int(account_id)
        for name, account_id in _sync_busy_accounts.items()
        if name in alive and account_id is not None
    }


def _held_task_ids() -> set[int]:
    """**仍被活着 worker 持有**的队列项 id（复位中断行时跳过它们）。"""
    alive = _alive_worker_names()
    return {int(item_id) for name, item_id in _sync_held_items.items() if name in alive}


def _mark_worker_idle(thread_name: str) -> None:
    """这条 worker 跑完了（**必须放掉占用**，否则账号被永久占住）。

    放掉之后顺手 `set` 一次唤醒位：刚空出来的账号可能正是别的 worker 想认领却被
    排除掉的那些项 ⇒ 唤醒它们立刻重试，而不是干等 `SYNC_IDLE_POLL_SECONDS`。
    代价有界（每跑完一条项才 set 一次），收益是「多账号交错排队」不再有 5 秒级的空转。
    """
    with _sync_queue_lock:
        _sync_held_items.pop(thread_name, None)
        _sync_busy_accounts.pop(thread_name, None)
    _sync_wakeup.set()
#: 后台线程自建 Session 的工厂（测试注入临时库的 sessionmaker）。
_session_factory = SessionLocal


def thread_name_matches_sync_worker(name: Any) -> bool:
    """线程名是否属于本池（**前缀**匹配：`note-batch-sync-worker` / `…#2` / `…#8`）。

    ⚠️ 必须是前缀而不是相等：池化后名字带序号，写死相等会让探针只认第 1 条 ——
    第 1 条死、第 2 条活着时报「worker 全死」，看门狗会去重建出重复线程。
    """
    text = str(name or "")
    return text == SYNC_WORKER_THREAD_NAME or text.startswith(f"{SYNC_WORKER_THREAD_NAME}#")


def sync_parallelism() -> int:
    """**并行账号数**（运行时配置，夹到 `[1, SYNC_MAX_PARALLELISM]`）。

    读点走 `settings_service.live_int_process`（进程级开关：所有用户的该键行取最大值
    ⇒ 任何用户在设置页调大都真的生效），回落 `settings.xhs_sync_account_parallelism`
    （YAML / env，默认 3），再回落内置 3。

    ⚠️ 任何异常都退默认值：并行度读不到不该让同步停摆。
    """
    try:
        from backend.app.services.settings_service import live_int_process

        value = live_int_process(
            "xhs_sync_account_parallelism", default=SYNC_DEFAULT_PARALLELISM
        )
    except Exception:  # noqa: BLE001 - 配置读不到就用默认并行度
        logger.warning("note sync: 读并行度失败，本次用默认 %s", SYNC_DEFAULT_PARALLELISM, exc_info=True)
        value = SYNC_DEFAULT_PARALLELISM
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = SYNC_DEFAULT_PARALLELISM
    return max(1, min(number, SYNC_MAX_PARALLELISM))


def _enqueue_sync(
    db: Session,
    *,
    user_id: int,
    note_pk: int,
    note_title: str = "",
    account_id: Optional[int] = None,
    source: str,
    batch: Any = None,
) -> tuple[Any, bool]:
    """入队一篇到 `tasks` 表（幂等）。返回 `(task_row, created)`。

    `created=False` = 已在队列 / 执行中（不重复请求）。调用方据此回
    `{ok: true, status: "queued", already_queued: true}`。

    `note_title` = 入队时的笔记标题快照（§35.9：入队即完整落库 —— 队列记录页
    直接显示主体，不为每行回头拉笔记）。`source` = **按钮级来源**（必填，见
    `note_queue_service` 的来源常量；单条 / 批量入口各传各的）。

    `account_id` = 这篇笔记所属平台账号（`note.platform_account_id`）—— 队列记录页
    的「账号」列要在**每条明细**上回答「这行是哪个账号跑的」（2026-09-26 用户口径
    「批次记录执行的账号」）。与发布 / 商品生成两个队列**同形**：入队即写进
    `payload.account_id`（原先只有这里漏了 ⇒ 同步队列项的账号恒为 `None`，页面上
    整列都是 `-`）。**批次行的账号由子行派生**（`batch_account_ids`），不在批次行上
    另存一份（一次批量可以跨账号勾选 ⇒ 批次级只有一个账号 id 是错的）。

    `batch` = 这次入队动作的批次上下文（2026-09-26 用户口径「批次逻辑不再移除，
    生成唯一批次号」）：同一动作的每次调用传**同一个** `BatchRequest` 实例，
    底座据此把真落库的项挂到同一个批次号下。

    ⚠️ 不 commit、**不唤醒 worker**：一次批量 = 一个事务，提交与唤醒都归
    :func:`start_batch_sync`（worker 自建 Session，看不到未提交的行）。
    """
    return note_queue_service.enqueue_item(
        db,
        user_id=user_id,
        item_task_type=note_queue_service.TASK_TYPE_SYNC_ITEM,
        note_pk=note_pk,
        account_id=account_id,
        note_title=note_title,
        source=source,
        batch=batch,
    )


def ensure_sync_worker() -> None:
    """确保 worker 池**满员**（惰性起；已满则只唤醒）。

    池化后语义从「有一条活着的」升级为「**对齐到配置的并行度**」：
    缺 ⇒ 补齐；多（用户把并行度调小了）⇒ 收掉多余的。两个方向都要做，否则
    「页面把并行度从 3 改成 1」会变成假配置（改了不生效）。

    公开（不是 `_` 前缀）：**启动恢复**也要用它 —— `main.py::lifespan` 复位完
    中断行后立刻唤醒，把上次进程留下的 `pending` 项接着跑（否则要等看门狗 30 秒
    后巡检才发现「没 worker」）；**设置页改并行度**也用它（`api/settings.py`
    的 `_wake_runtime_consumers`）。
    """
    with _sync_queue_lock:
        _reconcile_sync_worker_pool()
    _sync_wakeup.set()


def _enqueue_notes(
    db: Session,
    user_id: int,
    note_pks: list[int],
    *,
    gate: Any,
    gate_message: str,
    max_items: int,
    source: str,
) -> dict:
    """**入队的唯一实现**（单条与批量共用；设计 13 §35.2 / §35.10）。

    只有「门禁函数 + 门禁文案 + 上限 + **来源**」四处不同，其余（去重 / 归属校验 /
    幂等入队 / 整批一事务 / 提交后唤醒）逐字相同 —— 单条另写一份就是 DEF-044。

    ⚠️ **门禁两把尺子，不要合并**：单条 `note_supports_sync`（`synced` **或**
    已发布）vs 批量 `note_supports_batch_sync`（**仅**已发布，用户口径「只有已发布
    的支持被选中」）。改一边不默认改另一边。

    `source` = 按钮级来源（必填）：单条入口传 `note_sync_single`，批量入口传
    `note_sync_batch`（2026-09-26 用户口径「来源要具体到按钮」）。

    **一个动作 = 一个批次**（2026-09-26 用户口径「批次逻辑不再移除，生成唯一
    批次号」）：本函数（无论单条还是批量调用）建**一个** `BatchRequest`，通过篇
    全挂同一个批次号 —— 用户在记录页看到的就是「一次点击 = 一个批次 + N 条明细」。
    批次行**懒创建**（一篇都没通过门禁 ⇒ 不留空批次行）。

    **一个事务**（要么整批入队、要么整批不落）。
    """
    cleaned = [int(pk) for pk in note_pks if str(pk).strip()]
    distinct = list(dict.fromkeys(cleaned))[:max_items]
    # 一次动作一个批次上下文（懒创建：真有一篇入队才落批次行）
    batch = note_queue_service.BatchRequest(
        user_id=user_id,
        item_task_type=note_queue_service.TASK_TYPE_SYNC_ITEM,
        source=source,
    )

    results: list[dict] = []
    try:
        for note_pk in distinct:
            note = db.get(Note, note_pk)
            if note is None or note.user_id != user_id:
                results.append(
                    {"note_pk": note_pk, "ok": False, "error": "笔记不存在或不属于当前用户", "code": "NOTE_NOT_FOUND"}
                )
                continue
            if not gate(note):
                results.append(
                    {"note_pk": note_pk, "ok": False, "error": gate_message, "code": "NOTE_NOT_SYNCABLE"}
                )
                continue
            _row, created = _enqueue_sync(
                db,
                user_id=user_id,
                note_pk=note.id,
                note_title=str(note.title or ""),
                # 账号快照（队列记录页「账号」列的数据来源）：`note` 就在手边，
                # 顺手带上 —— 发布队列那边也是在入队时取 `note.platform_account_id`。
                account_id=(
                    int(note.platform_account_id) if note.platform_account_id else None
                ),
                source=source,
                batch=batch,
            )
            results.append(
                {
                    "note_pk": note.id,
                    "ok": True,
                    "status": "queued",
                    "note_id": note.note_id,
                    **({} if created else {"already_queued": True}),
                }
            )
        db.commit()
    except BaseException:
        db.rollback()  # 安全网不条件性：半批入队比不入队更难排查
        raise

    # ⚠️ 唤醒放在**提交之后**：worker 自建 Session，看不到未提交的行 ——
    # 提前唤醒只会让它空转一轮再回 wait。
    ensure_sync_worker()
    started = sum(1 for r in results if r.get("ok"))
    logger.info(
        "note sync enqueued (total=%s, started=%s, already=%s)",
        len(distinct),
        started,
        sum(1 for r in results if r.get("already_queued")),
    )
    return {"total": len(distinct), "started": started, "failed": len(distinct) - started, "results": results}


def start_batch_sync(
    db: Session,
    user_id: int,
    note_pks: list[int],
    *,
    max_items: int = SYNC_BATCH_MAX_ITEMS,
) -> dict:
    """批量同步入口（设计 13 §35.2）：逐篇过门禁，通过篇按序入队，**毫秒级返回**。

    门禁（与前端 `canBatchSyncNote` 同口径，模型层唯一判据
    `note_supports_batch_sync`）：① 归属（`user_id`）；② **已发布**
    （`publish_status == "published"`）。不达标篇**不入队**、进 `results`
    （`ok:false, code:"NOTE_NOT_SYNCABLE"` / `"NOTE_NOT_FOUND"`），不阻断其余。

    立即返回 `{total, started, failed, results}`：通过篇
    `{ok:true, status:"queued"}`（重复入队的带 `already_queued:true`）。终态由
    前端轮询 `GET /notes/sync-queue`（快照 `finished` 带逐篇结果与原因）。
    """
    return _enqueue_notes(
        db,
        user_id,
        note_pks,
        gate=note_supports_batch_sync,
        gate_message="只有已发布过的笔记支持批量同步",
        max_items=max_items,
        source=note_queue_service.SOURCE_NOTE_SYNC_BATCH,
    )


def start_sync_note(db: Session, user_id: int, note_pk: int) -> dict:
    """单条同步入口（内容库「同步」按钮）：**与批量同一个队列、同一个 worker**。

    2026-09-25 用户口径：「单独点击同步按钮也需要放入同步队列中执行，与批量同步
    一致」。改造前单条是**同步阻塞执行**（端点在请求线程里直接调
    `sync_note_from_platform`）⇒ 与批量并存时两条链路各打各的平台请求，
    限流额度下互相插队、限流等待叠加；队列化后由 worker 逐篇跑，
    顺序执行就是限流的第一道保障（§21.3 等待式限流）。
    ⚠️ 原句「www 额度（5 次/分钟）」中的 5 是**代码写死的值**，不是平台额度
    （设计 13 §38.1 F1/F2，2026-09-27 已修）。

    门禁仍是**单条那把尺子** `note_supports_sync`（`synced` **或**已发布）——
    比批量（仅 published）宽，**不随这次改造收紧**（页面按钮的渲染条件
    `canSyncNote` 与它同口径，收紧会让「按钮在、点了 400」重现）。

    返回与批量 `results` 单项**同形**：`{note_pk, ok, status:"queued", note_id,
    already_queued?}`；门禁不过 / 笔记不存在时 `ok:false` + `code` + `error`，
    **不抛异常**（调用方按 `ok` 决定回 400 还是 200）。

    ⚠️ 不返回同步的**逐项细节**（正文 / 图片各更新了什么）—— 那是
    `sync_note_from_platform` 的返回，现在只发生在 worker 线程里。终态与原因由
    `GET /notes/sync-queue` 的快照 `finished` 给一次（与批量同一出口）。
    """
    payload = _enqueue_notes(
        db,
        user_id,
        [note_pk],
        gate=note_supports_sync,
        gate_message="只有三方同步笔记或已发布过的笔记支持同步",
        max_items=1,
        source=note_queue_service.SOURCE_NOTE_SYNC_SINGLE,
    )
    item = payload["results"][0] if payload["results"] else {
        "note_pk": int(note_pk), "ok": False, "error": "笔记不存在或不属于当前用户", "code": "NOTE_NOT_FOUND",
    }
    logger.info(
        "note single sync enqueued (note_pk=%s, ok=%s, already=%s)",
        note_pk,
        item.get("ok"),
        item.get("already_queued", False),
    )
    return item


def _sync_worker_loop(thread_name: Optional[str] = None) -> None:
    """DB 队列 worker：醒来 → 认领一条 → 跑一条 → 再认领 …… 认不到就回去等。

    **池化**（设计 13 §38.3，2026-09-27）：本函数是池内**每一条**线程的执行体，
    靠 `thread_name`（缺省取当前线程名）认领自己的停止位与占用登记。

    * **跨账号并行**：N 条 worker 同时跑 N 个不同账号的项；
    * **账号级互斥**：认领时排除「已被别的 worker 占住的账号」⇒ 同一账号任何时刻
      至多 1 条在跑（同账号并发只会互抢同一个限流桶，既不提速又让额度语义失真）。

    ⚠️ 唤醒信号只是**提示**：`wait(timeout=SYNC_IDLE_POLL_SECONDS)` 带兜底超时
    ⇒ 任何一次 `set` 丢失，最坏也只是晚 5 秒发现新任务，不会永久停摆
    （DEF-048：安全网不能条件性）。

    ⚠️ DEF-069（2026-09-25 真机）：`_run_one_sync` 篇内的 `except BaseException`
    挡「一篇炸」，但**线程自身**仍可能被 BaseException（`SystemExit` 等 ——
    threading 对它们**静默**，连 traceback 都不打）或漏网路径带走 ⇒ 队列剩余篇
    永久停摆（用户看到「待同步 N」永不下降）。外层再包一层网；真死了由看门狗
    补齐（`start_sync_worker_watchdog`），补齐时把**确实无人认领**的 `running` 行复位
    （还活着的那几条手上的项**不动** —— 见 `_revive_sync_worker`）。

    `_sync_worker_stop`（全局）与 `_sync_worker_stops[名字]`（本线程）任一置位
    ⇒ **不再认领新任务**、立即退出。生产只在进程收尾时置全局位；单线程位用于
    「并行度调小 ⇒ 收掉多余的那几条」（设计 13 §38.3）。
    ⚠️ 「不丢正在排队的篇」**由库保证**（未跑的项是 `pending` 行，下次启动接着跑），
    不再依赖 worker「排空」；停止位只负责别再干新活。
    """
    name = str(thread_name or threading.current_thread().name)

    def _stopped() -> bool:
        if _sync_worker_stop.is_set():
            return True
        own = _sync_worker_stops.get(name)
        return own is not None and own.is_set()

    while not _stopped():
        _sync_wakeup.wait(timeout=SYNC_IDLE_POLL_SECONDS)
        _sync_wakeup.clear()
        while not _stopped():
            # ⚠️ **「算占用 → 认领 → 登记占用」必须在同一把锁里**（2026-09-27 实测踩到）：
            # 拆开做时三条 worker 会**同时**算出「没有账号被占」（那一刻确实还没有人
            # 登记），于是同一账号的三条项被三条线程一起领走 —— 账号级互斥形同虚设，
            # 三条线程互抢同一个限流桶。锁只包住这几行（认领是一次毫秒级 DB 操作），
            # 不包执行体，所以并行度不受影响。
            with _sync_queue_lock:
                if _stopped():
                    break
                # 认领顺序：同步队列（用户刚点的那篇优先）→ 发布 / 生成各有自己的 worker。
                # 笔记详情补齐已并入同步任务本身（设计 13 §35.18）⇒ 这里不再有第二个队列。
                item = _claim_one_sync_item(exclude_account_ids=_busy_account_ids())
                if item is None:
                    break
                # 登记占用（**直接写**，不走 `_mark_worker_busy` —— 那是同一把非重入锁）
                _sync_held_items[name] = int(item.id)
                _sync_busy_accounts[name] = note_queue_service.account_id_of(item)
            try:
                _run_safely("note batch sync worker", _run_one_sync, int(item.id))
            finally:
                # 放掉占用（**必须在 finally**：`_run_safely` 吞 BaseException，
                # 但漏网路径也要保证账号不被永久占住）
                _mark_worker_idle(name)


def _run_safely(label: str, runner: Any, item_id: int) -> None:
    """跑一条并把**线程级**漏网异常吃掉（DEF-069 的第二层网）。

    `_run_one_sync` 内部的 `except BaseException` 挡「一篇炸」，
    这里挡「把线程带走」—— threading 对 `BaseException`（`SystemExit` 等）**静默**
    （不打 traceback、不重启线程）⇒ worker 无声死亡、队列永久停摆。真死了由看门狗
    重建（`start_sync_worker_watchdog`），重建时把卡在 `running` 的行复位。

    `label` = 日志前缀（发布 / 生成 worker 也共用本函数，**不各写一份同样的网**）。
    """
    try:
        runner(item_id)
    except BaseException as error:  # noqa: BLE001 - 线程不死（DEF-069）
        logger.error(
            "%s: run escaped the inner net (task=%s): %s",
            label,
            item_id,
            error,
            exc_info=True,
        )


def _claim_one_item(
    item_task_type: str,
    *,
    label: str,
    exclude_account_ids: Optional[set[int]] = None,
) -> Optional[Task]:
    """认领一条指定队列类型的待跑项，返回**任务行**（无活可干 / 认领出错 ⇒ None）。

    认领用**独立 Session**（`claim_next_item` 内部提交换状态，用完即关）；
    用它自带的 `except BaseException` 兜住认领期的 DB 异常 —— 认领失败只是这一轮
    空转（外层 `wait(timeout=…)` 天然退避），不许把 worker 带走。

    `label` = 日志前缀（保持两个队列各自的原文案，便于日志里分辨是哪条队列在报错）。
    `exclude_account_ids` = 账号级互斥的排除集（发布 / 生成队列不传 ⇒ 行为不变）。

    ⚠️ 返回**行**而不是 id（改造前返回 id）：调用方要拿 `payload.account_id` 去登记
    「这个账号被占住了」。让调用方再查一次库只为拿一个字段，是把一次读取拆成两次。
    """
    db = _session_factory()
    try:
        return note_queue_service.claim_next_item(
            db,
            item_task_type=item_task_type,
            exclude_account_ids=exclude_account_ids or (),
        )
    except BaseException:  # noqa: BLE001 - 认领失败退避，不死 worker
        logger.exception("%s: claim failed", label)
        return None
    finally:
        db.close()


def _claim_one_sync_item(exclude_account_ids: Optional[set[int]] = None) -> Optional[Task]:
    """认领一条待跑的**同步**项（类型绑定包装，实现见 `_claim_one_item`）。"""
    return _claim_one_item(
        note_queue_service.TASK_TYPE_SYNC_ITEM,
        label="note batch sync worker",
        exclude_account_ids=exclude_account_ids,
    )


def _item_note_pk(item: Task) -> int:
    """从队列项的 `payload` 取 `note_pk`（0 = 脏行，当不存在处理）。

    委托持久化底座的 `note_pk_of`（原实现从 `_note_pk_of` 私有名提升为公开名：
    发布 worker 也要用同一份，**同一语义不留两份实现**）。
    """
    return note_queue_service.note_pk_of(item)


def _run_one_sync(item_id: int) -> None:
    """跑一条（按 `tasks.id`）：自建 Session；任何漏网异常都不许把队列卡死。

    ⚠️ DEF-069：`except BaseException` 而不是 `except Exception` —— `SystemExit`
    是 `BaseException` 子类，`except Exception` 抓不到，而 threading 对它**静默**
    （不打 traceback、不重启线程）⇒ worker 无声死亡、队列停摆。

    安全网不条件性（DEF-048）：**任何**异常路径都必须把这条写成终态（`failed` +
    原因），否则它会永远挂在 `running` 上（快照里「正在同步」永远转圈）。
    """
    db = _session_factory()
    try:
        item = db.get(Task, item_id)
        if item is None:
            return  # 行已被清理任务删掉 / 不存在：无事可做
        note_pk = _item_note_pk(item)
        note = db.get(Note, note_pk) if note_pk else None
        if note is None:
            note_queue_service.finish_item(
                db, item, ok=False, reason="笔记不存在", code="NOTE_NOT_FOUND"
            )
            return
        # 提速（DEF-069）：本篇缺详情地址（本地生成 + 已发布的既有形态）时，把
        # **队列里同样缺地址的篇一次请求全部补上** —— 否则 N 篇本地生成笔记要
        # 打 N 次千帆（1-2 秒/次）。一篇触发、全批受益；后续篇 `_note_detail_url`
        # 已能取到地址，`sync_note_from_platform` 内部的单篇回查自然跳过。
        if not _note_detail_url(note):
            pending_pks = note_queue_service.list_note_pks_by_status(
                db, item_task_type=note_queue_service.TASK_TYPE_SYNC_ITEM
            )
            batch = [row for row in (db.get(Note, pk) for pk in pending_pks) if row is not None]
            batch = [row for row in batch if row.platform_account_id == note.platform_account_id]
            if batch:
                account = db.get(PlatformAccount, note.platform_account_id)
                if account is not None:
                    _backfill_detail_ref_for_accounts(db, account, [note, *batch])
        # **唯一实现复用**：与单条「同步」按钮、定时任务同步同一把尺子（限流等待 +
        # 详情合并落库 + hydrated_at 三态），不另写一份同步逻辑。
        outcome = sync_note_from_platform(db, note)
        note_queue_service.finish_item(
            db,
            item,
            ok=outcome.get("status") == "ok",
            reason=str(outcome.get("reason") or ""),
            code=str(outcome.get("code") or ""),
        )
    except BaseException as error:  # noqa: BLE001 - 兜底安全网：SystemExit 也不许把队列卡死
        logger.warning("note batch sync worker crashed (task=%s): %s", item_id, error)
        try:
            db.rollback()  # 失败的 commit 留待回滚状态；不回滚则记结果的动作也会炸
        except Exception:  # noqa: BLE001 - 恢复也失败只能留痕，不能再抛
            logger.exception("note batch sync rollback failed (task=%s)", item_id)
        try:
            item = db.get(Task, item_id)
            if item is not None:
                note_queue_service.finish_item(
                    db,
                    item,
                    ok=False,
                    reason=f"{type(error).__name__}: {error}",
                    code="SYNC_FAILED",
                )
        except Exception:  # noqa: BLE001 - 连记账都失败：留痕，行留在 running 由启动恢复捡回
            logger.exception("note batch sync failed to record failure (task=%s)", item_id)
    finally:
        db.close()


#: 看门狗巡检间隔 / 重建失败指数退避上限（与调度器守活 `scheduler_service` 同值）。
SYNC_WATCHDOG_INTERVAL_SECONDS = 30.0
SYNC_WATCHDOG_MAX_BACKOFF_SECONDS = 600.0

_sync_watchdog_stop = threading.Event()


def _alive_sync_workers() -> list[threading.Thread]:
    """进程里**活着的**本池线程（按线程名前缀枚举，DEF-069 的唯一可信存活判据）。

    为什么不看模块变量：线程带异常 / `SystemExit` 死后变量仍指着那个 `Thread`
    对象，而 `_enqueue_sync` 只在**入队时**检查一次 —— 队列跑起来之后线程死了，
    没有任何现存路径会发现。与 `scheduler_service.scheduler_thread_alive()`
    同判据（DEF-067 的教训：状态位会骗人，线程本身不会）。
    """
    return [
        thread
        for thread in threading.enumerate()
        if thread_name_matches_sync_worker(thread.name) and thread.is_alive()
    ]


def sync_worker_thread_alive() -> bool:
    """进程里有没有**活着的**本池 worker（任一即真）。

    ⚠️ 池化（设计 13 §38.3）后判据从「有那一条」变成「**至少还有一条**」：
    看门狗据此决定要不要补人，**期望条数**另由 `sync_worker_pool_deficit()` 回答。
    """
    return bool(_alive_sync_workers())


def sync_worker_pool_size() -> int:
    """当前活着的池成员数（观测 / 测试 / 看门狗用）。"""
    return len(_alive_sync_workers())


def sync_worker_pool_deficit() -> int:
    """还缺几条 worker（期望 = `sync_parallelism()`，实际 = 活着的条数）。"""
    return max(0, sync_parallelism() - sync_worker_pool_size())


def sync_worker_thread_name(index: int) -> str:
    """池内第 `index`（0 起）条 worker 的线程名。

    第 1 条**保持原名**（兼容既有探针 / 测试 / 日志检索），其余带 `#序号`。
    """
    return SYNC_WORKER_THREAD_NAME if index == 0 else f"{SYNC_WORKER_THREAD_NAME}#{index + 1}"


def _spawn_sync_worker(index: int = 0) -> threading.Thread:
    """起一条新的 worker 线程（调用方持 `_sync_queue_lock`）。

    起新世代时清掉历史停止位 —— 重建出来的 worker 不该被上一世代的收尾位挡住
    （`_revive_sync_worker` 的路径）。⚠️ 但**只在建第 1 条（index=0）时清**：
    补人（池里死了 1 条、补 1 条）时清全局停止位会把「收尾中」的池重新激活。

    `index` = 池内序号（决定线程名与是否当主 worker）。
    """
    global _sync_worker
    # ⚠️ 线程名只能由 `sync_worker_thread_name` 产出（它以 `SYNC_WORKER_THREAD_NAME`
    # 为基名，探针按此前缀匹配）—— 这里**不许写裸字符串**，否则探针认不到这些线程。
    name = sync_worker_thread_name(index)
    if index == 0:
        _sync_worker_stop.clear()
    stop = threading.Event()
    _sync_worker_stops[name] = stop
    # ⚠️ **不传 `kwargs`**：`_sync_worker_loop` 自己用 `threading.current_thread().name`
    # 认身份。传参会让「把 `_sync_worker_loop` 换成零参替身」的测试直接 TypeError
    # （线程静默死掉 ⇒ 池里凭空少人），而零参替身正是既有测试的写法。
    thread = threading.Thread(
        target=_sync_worker_loop,
        name=name,
        daemon=True,
    )
    if index == 0:
        _sync_worker = thread
    _sync_workers.append(thread)
    thread.start()
    return thread


def _reconcile_sync_worker_pool(*, spawn_missing: bool = True) -> list[threading.Thread]:
    """把池对齐到 `sync_parallelism()`：**补缺**（必要时）并**收掉多余的**。

    调用方持 `_sync_queue_lock`。返回对齐后活着的成员。

    两个方向都要做，否则「页面把并行度从 3 改成 1」会变成一个**假的实时配置**
    （改了不生效，正是设计 10 §1 定义的管理缺陷）：

    * 缺 ⇒ `_spawn_sync_worker(index)` 补到期望条数（`spawn_missing=False` 时只报告）；
    * 多 ⇒ 给多余的线程置**它自己的**停止位（不碰全局停止位，其余线程照跑）。

    ⚠️ 主 worker（`_sync_worker`）**优先保留**：它是既有探针 / 测试的判据锚点。
    """
    # ⚠️ `_sync_worker` 是**模块级**探针锚点（`_sync_workers` 是原地改的列表，不需要
    # global）。漏这句会让本函数里的赋值被当成**局部变量** ⇒ 走到最后那行直接
    # `UnboundLocalError`（实测：任何一次入队都炸，同步链路整体不可用）。
    global _sync_worker
    desired = sync_parallelism()
    alive = _alive_sync_workers()
    if len(alive) > desired:
        keep = alive[:desired]
        for thread in alive[desired:]:
            stop = _sync_worker_stops.get(thread.name)
            if stop is not None:
                stop.set()
        _sync_wakeup.set()  # 唤醒它们回来检查自己的停止位
        alive = keep
    if spawn_missing:
        for index in range(len(alive), desired):
            alive.append(_spawn_sync_worker(index))
    # 池成员表收敛到实际活着的那些（死线程 / 被收掉的都不留在表里）
    _sync_workers[:] = list(alive)
    if _sync_worker is not None and not _sync_worker.is_alive():
        _sync_worker = alive[0] if alive else None
    # 清掉已死线程的账号占用（不让死线程把某个账号永久「占住」）
    for name in [n for n in _sync_busy_accounts if n not in {t.name for t in alive}]:
        _sync_busy_accounts.pop(name, None)
    return alive


def stop_sync_worker(*, timeout: float = 5.0) -> None:
    """让**全部** worker **立即停手退出**（**测试 / 进程收尾专用**，生产不调用）。

    只置停止位不够：worker 常态卡在 `_sync_wakeup.wait(timeout=…)` 上，必须再
    `set` 一下 wakeup 才能唤醒它回来检查停止位。唤醒后它们**不再认领新任务**、
    手上那条跑完即退。

    ⚠️ 池化后是**整池**停（`_sync_worker_stops` 里每一条都置位）—— 只停第 1 条会让
    测试 teardown 后还剩 N−1 条真 worker 继续打平台（正是「测试写生产库」那类事故的形态）。
    ⚠️ 「不丢正在排队的篇」**由库保证**：未跑的项还是 `pending` 行，下次启动
    （或下一次唤醒）接着跑 —— 不靠 worker「排空」。
    """
    _sync_worker_stop.set()
    for stop in _sync_worker_stops.values():
        stop.set()
    _sync_wakeup.set()
    for worker in list(_sync_workers):
        if worker.is_alive():
            worker.join(timeout=timeout)


def _revive_sync_worker() -> None:
    """worker 线程死了 ⇒ 复位它卡住的那条 + 重建 worker（不丢篇）。

    线程死后，它认领的那条 `tasks` 行**永远停在 `running`**（`_sync_worker_loop`
    的 finally 都没机会跑）⇒ 不复位的话它既不会被重跑、也永远算在「正在同步」里，
    快照的待同步数含它、前端永远轮询。复位就是把它改回 `pending`
    （`recover_interrupted` 只动 `running` 行，见其 docstring）。

    ⚠️ **顺序：先复位、再起新 worker**。反过来新 worker 可能刚认领一条就被复位
    抢回 `pending` ⇒ 同一条被跑两遍。

    ⚠️ **复位要覆盖这条线程能跑的**所有**队列类型**：`note-batch-sync-worker` 现在只跑
    同步队列项（详情补齐项已并入同步任务本身，设计 13 §35.18）—— 但**同步任务现在会
    长时间跑账号级补齐**，占住 worker 的那条行同样必须能复位，否则它永远算在
    「正在同步」里。

    判据是**池的缺口**（`sync_worker_pool_deficit()`）：探针回答「还有没有能干活的」，
    这里回答「要不要补人」——池化后可能是「死了 2 条、还剩 1 条」，只判「有没有」会漏掉。

    ⚠️ **复位与补人是两件事，顺序固定**：只要**有任何一条** worker 死过（不论池里还剩
    几条），就可能有一条 `running` 行被丢下 —— 所以只要缺口 > 0 就先复位，再补人。
    """
    recovered = {"requeued": 0, "failed": 0}
    try:
        db = _session_factory()
        try:
            recovered = note_queue_service.recover_interrupted(
                db,
                item_task_types=(note_queue_service.TASK_TYPE_SYNC_ITEM,),
                # ⚠️ 跳过还活着的那几条 worker 手上的项（池化后必须给）：
                # 不给就会把它们的任务从脚下抽走（改回 pending）⇒ 同一条被跑两遍。
                exclude_task_ids=_held_task_ids(),
            )
        finally:
            db.close()
    except BaseException:  # noqa: BLE001 - 复位失败也要把 worker 建起来
        logger.exception("同步 worker 重建：中断行复位失败（继续重建 worker）")
    with _sync_queue_lock:
        alive = _reconcile_sync_worker_pool()
    _sync_wakeup.set()  # 新 worker 可能已在 wait 上：不等下一批也要立刻跑积压
    logger.error(
        "%s 池缺人 —— 已补齐到 %s 条（中断行 requeued=%s failed=%s）",
        SYNC_WORKER_THREAD_NAME,
        len(alive),
        recovered.get("requeued"),
        recovered.get("failed"),
    )


def start_sync_worker_watchdog(
    *, interval: float = SYNC_WATCHDOG_INTERVAL_SECONDS
) -> threading.Thread:
    """守活线程：worker 死了就重建（DEF-069，2026-09-25）。

    现场（真机，不是推演）：批量同步 19 篇跑到 8 篇后 `note-batch-sync-worker`
    线程**无声消失** —— `py-spy dump` 进程里查无此线程、日志无 traceback、DB 无
    新写入、SQLite 写锁无竞争 ⇒ 剩余 11 篇「待同步」永不执行、前端按
    `pending != 0` 一直轮询。与 DEF-067 的 APScheduler **同型**：单线程组件
    没有自愈，任何 `except Exception` 够不着的死法都是永久停摆。

    看门狗自己绝不被一次失败带走（`except BaseException` + 失败指数退避，
    上限 `SYNC_WATCHDOG_MAX_BACKOFF_SECONDS`）。进程收尾调
    :func:`stop_sync_worker_watchdog`。
    """
    _sync_watchdog_stop.clear()

    def _loop() -> None:
        backoff = interval
        while not _sync_watchdog_stop.is_set():
            if _sync_watchdog_stop.wait(backoff):  # 可被 stop 打断的等待
                return
            try:
                # 池化后判据是**缺口**不是「有没有」：死 2 条还剩 1 条时
                # `sync_worker_thread_alive()` 仍是 True —— 只判它就会漏补。
                if sync_worker_pool_deficit() > 0:
                    _revive_sync_worker()
                else:
                    # 满员时也要对一次账：用户可能把并行度**调小**了（收掉多余的）。
                    # 两个方向都由 `_reconcile_sync_worker_pool` 负责。
                    with _sync_queue_lock:
                        _reconcile_sync_worker_pool()
                backoff = interval
            except BaseException:  # noqa: BLE001 - 看门狗自己也不能死
                logger.exception("同步 worker 看门狗重建失败，%.0f 秒后重试", backoff)
                backoff = min(backoff * 2, SYNC_WATCHDOG_MAX_BACKOFF_SECONDS)

    thread = threading.Thread(target=_loop, name="sync-worker-watchdog", daemon=True)
    thread.start()
    return thread


def stop_sync_worker_watchdog() -> None:
    """让看门狗退出巡检（幂等；下一次 `start_sync_worker_watchdog` 自动复位）。"""
    _sync_watchdog_stop.set()


def sync_queue_snapshot(db: Session, user_id: int, *, platform: Optional[str] = None) -> dict:
    """同步队列快照（进度卡用；设计 13 §35.3 / §35.9）。

    2026-09-26 常驻化（用户口径「不需要队列批次，队列是常驻」）：快照只描述
    **队列实时态 + 近期终态** —— `running` = 正在同步的那条，`queued` = 排队中的
    （FIFO 序），`finished` = 近期终态项（含逐项 `ok` 与 `reason` —— 同步失败
    不落库到笔记行，原因只在这里给一次），`total` = 三者计数之和（进度卡停止
    条件「`total > 0` 且待处理为 0」靠它成立）。

    **不再有 `synced` / `failed` 计数字段**：「本轮成败」由前端按自己入队的项
    累计（后端不知道用户这一轮点了什么），窗口计数只会把「近期」说成「本轮」。

    ⚠️ 2026-09-26 用户口径改口径（「**只统计当日同步的数据**……统计队列记录表中的
    数据」）：卡上三数**不再由前端按自己入队的篇累计**（刷新页面就丢、也看不见
    别的入口入队的），改由 `today` 键（`note_queue_service.today_item_counts`：
    当日 `tasks` 行现算 成功 / 失败 / 待处理）承担。逐篇失败原因仍走 `finished`。

    形状与另两个队列对齐（前端三卡同一套轮询骨架）；实现直接委托持久化底座
    （`note_queue_service.snapshot`），本模块不留第二份实现（DEF-044）。
    """
    raw = note_queue_service.snapshot(
        db,
        user_id=user_id,
        item_task_type=note_queue_service.TASK_TYPE_SYNC_ITEM,
        platform=platform,
    )
    return {
        "running": raw["running"],
        "queued": raw["queued"],
        "total": raw["total"],
        "finished": raw["finished"],
        # 进度卡的**当日三数**（2026-09-26 用户口径「只统计当日同步的数据，……
        # 统计队列记录表中的数据」）：来自 `tasks` 表，不是本页累计。
        "today": note_queue_service.today_item_counts(
            db,
            user_id=user_id,
            item_task_type=note_queue_service.TASK_TYPE_SYNC_ITEM,
        ),
    }


def wait_sync_done(note_pk: int, timeout: float = 30.0) -> bool:
    """等某篇跑完（**测试用**；生产代码不调）。超时返 False。

    判据 = 库里该 `note_pk` 不再有待跑（`pending`）/ 在跑（`running`）的同步项。
    已终态 / 从未入队 ⇒ 立即 True（与旧内存版「查不到等待事件就当已完成」同语义）。
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            db = _session_factory()
            try:
                open_pks = note_queue_service.list_note_pks_by_status(
                    db,
                    item_task_type=note_queue_service.TASK_TYPE_SYNC_ITEM,
                    statuses=("pending", "running"),
                )
            finally:
                db.close()
            if int(note_pk) not in open_pks:
                return True
        except BaseException:  # noqa: BLE001 - 查询失败继续等，不炸测试
            logger.exception("wait_sync_done: 查询队列状态失败（继续等待）")
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _pending_hydration_count(db: Session, account: PlatformAccount) -> int:
    """该账号还有多少条笔记的详情没尝试补齐过。"""
    from backend.app.models import Note

    return int(
        db.scalar(
            select(func.count())
            .select_from(Note)
            .where(
                Note.platform_account_id == account.id,
                Note.hydrated_at.is_(None),
            )
        )
        or 0
    )


def _raw_has_detail_token(raw: Any) -> bool:
    """`raw_json` 里有没有**任何**可用的笔记级详情令牌（顶层 / `ark` 子对象）。

    ⚠️ 与 `_note_detail_url` 取令牌的**两条来源同一口径**（顶层 `xsec_token` 或
    `ark.xsec_token`）—— 「判要不要刷新」与「拼 URL 用哪个」不能有两套规则，
    否则会出现「判为有令牌所以不刷新、但拼 URL 时又取不到」的空转（DEF-044 形态）。
    """
    data = raw if isinstance(raw, dict) else {}
    if str(data.get("xsec_token") or "").strip():
        return True
    ark = data.get("ark") if isinstance(data.get("ark"), dict) else {}
    return bool(str(ark.get("xsec_token") or "").strip())


def _pending_hydration_without_token_count(db: Session, account: PlatformAccount) -> int:
    """待补齐笔记里**本地完全没有详情令牌**的条数（只用于如实报数，不触发任何刷新）。

    设计 13 §19.13.8 / §19.13.9（2026-09-28）：详情走 explore SSR 后不挑令牌来源，
    有令牌的篇本就不需要刷新；没令牌的篇**不再翻 `user_posted` 兜底**（用户口径：
    「移除兜底，不允许出现翻页到底」）⇒ 本函数只把「有多少篇卡在缺令牌上」报给用户，
    让他去补齐网页版登录令牌，而不是拿一次全表扫描去赌。

    只读 `(note_id, raw_json)` 两列、不加载整对象 —— 欠账上千条时也只是一次轻查询。
    """
    from backend.app.models import Note

    rows = db.execute(
        select(Note.note_id, Note.raw_json).where(
            Note.platform_account_id == account.id,
            Note.hydrated_at.is_(None),
        )
    ).all()
    return sum(
        1
        for note_id, raw in rows
        if str(note_id or "").strip() and not _raw_has_detail_token(raw)
    )


def _row_is_video(row: Any) -> bool:
    """这条已落库笔记是不是视频笔记（读 `raw_json.type`，平台原值）。"""
    raw = row.raw_json if isinstance(row.raw_json, dict) else {}
    return str(raw.get("type") or "").strip().lower() == "video"


def _has_video_asset(db: Session, note_id: int) -> bool:
    """这条笔记本地有没有视频资产。"""
    from backend.app.models import NoteAsset

    return db.scalars(
        select(NoteAsset.id)
        .where(NoteAsset.note_id == note_id, NoteAsset.asset_type == "video")
        .limit(1)
    ).first() is not None


def _needs_hydration(db: Session, note: Any) -> bool:
    """这条已落库笔记要不要打详情（设计 13 §19.14.2 挑选口径）。

    * ``hydrated_at IS NULL`` —— 从没取过详情（新建行 / 历史欠账 / 上次失败没写标记）；
    * **视频笔记但没有视频资产** —— 2026-09-22 视频流地址修复前的存量行（列表端点
      不带流地址，可播放地址只有详情有）。

    已补过的行返回 False ⇒ 全量同步重扫到它们时**不重复请求**（额度 5 次/分钟，
    重复请求 = 白烧）。内容库「同步」按钮不看这个判据（它本来就单条刷新）。
    """
    if note.hydrated_at is None:
        return True
    return _row_is_video(note) and not _has_video_asset(db, note.id)


def _new_hydration_result() -> dict[str, Any]:
    """一次补齐统计的**唯一**初值（两个候选集来源共用，形状不许各写一份）。

    ``attempted_pks`` = 本次真打过详情的**本地笔记主键**。它不是给人看的统计，
    而是给**同一个任务**里的下一段用的：页内联试过的篇，账号级收尾不许再试一次
    （www 详情额度 5 次/分钟，同一任务重复打同一篇既是白烧、也会让 `detail_failed`
    把同一篇数两遍）。

    ``page_total`` / ``pending_total``（2026-09-28 加）= 本批**候选条数**（含不需要补齐的）
    与其中**真需要补齐**的条数 —— 进度口径 `processed / work_total` 靠这两个数算
    「已处理」：`已处理 = 候选 - (候选 - 已尝试)`，即「无需补的 + 真尝试过的」。
    只试过一部分（限流等待超时 / 取消）时**没尝试的不计入** ⇒ 进度条到不了 100% 是
    **如实**的（`_finish` 会写真实终态）。账号级补齐不设 `page_total`（那里 `pending` 就是全部候选）。
    """
    return {
        "filled": 0,
        "failed": 0,
        "no_token": 0,
        "no_url": 0,
        "skipped_by_limit": 0,
        "attempted": 0,
        "attempted_pks": [],
        "warnings": [],
        "page_total": 0,
        "pending_total": 0,
    }


def _hydrate_note_rows(
    db: Session,
    account: PlatformAccount,
    pending: list[Any],
    *,
    cookies: str,
    adapter: Any,
    limiter: Any,
    task_id: Optional[int] = None,
    scope_label: str = "本页",
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """逐篇补齐给定的**待补行**（**唯一循环实现**，设计 13 §19.14 / §35.18）。

    两个候选集来源共用它（DEF-044：同一语义不留两份实现）：

    * :func:`_hydrate_page_notes` —— 本页刚落库的行（翻页循环**内联**，§19.14）；
    * :func:`_hydrate_account_pending` —— 该账号的**全部历史欠账**（同步任务收尾，§35.18）。

    纪律：

    * **单一实现**：逐条委托 ``hydrate_note_detail``（正文填空 / 图集与视频只增不删 /
      话题拆分只增不删 / ``hydrated_at`` 三态）—— 不另写一份合并逻辑（DEF-044/047/058）；
    * **限流等待不即停**（§21.3）：等不到 ⇒ 停剩余条目并计数报上去，
      **不逐条静默 continue**（静默正是旧实现「同步成功、正文永远为空」的坑）；
    * **失败不写标记**：列表数据已落库，``hydrated_at`` 留 NULL，下次同步重试。

    ``scope_label`` 只影响 warning 文案（「本页」/「账号」），不改任何判据。
    ``on_progress`` 是**观测**回调（**每篇一次**，参数 = 已处理 / 本批总数）：账号级补齐要跑
    几小时、页内补齐一页也要几分钟，任务行的进度数据必须**持续**动起来
    （`sts.progress_percent` 的首选口径 `processed / work_total` 就靠它累加）。
    回调里的异常**全吞**：进度是观测，不得影响执行本身（同 `_ProgressPump` 纪律）。

    返回 ``{"filled", "failed", "no_url", "skipped_by_limit", "attempted", "warnings"}``；
    ``filled`` = 详情**确有数据写入**的条数（正文 / 图 / 视频至少一项），
    ``attempted`` = 实际打过详情的条数。
    """
    result = _new_hydration_result()
    if not pending or adapter is None:
        # 没有 www 会话（适配器为 None）⇒ 一条请求都不发；任务行由编排器安静说明。
        return result
    # 2026-09-28：本批**候选总数** —— 调用方算「已处理」用它扣掉「没尝试的」：
    # `已处理 = 候选 - (候选 - 已尝试) = 已尝试 + (候选 - pending)`。
    result["pending_total"] = len(pending)

    for note in pending:
        # 协作式中断检查点（§24.8）：每条详情请求前看一次。补齐是最密的一段
        # （一页最多 50 条 / 账号级可能上千条），不检查的话取消要等整段补完才生效。
        if task_id is not None and task_cancel.is_cancelled(task_id):
            skipped = len(pending) - result["attempted"]
            if skipped > 0:
                result["warnings"].append(
                    f"已被用户取消，{scope_label}剩余 {skipped} 条未补正文（下次同步重试）"
                )
            break
        # 打的是 PC 详情接口，与列表翻页共享 www 通道的真实额度（§19.13.4 口径不变）。
        if not limiter.wait_allow(f"{account.id}:www"):
            skipped = len(pending) - result["attempted"]
            result["skipped_by_limit"] = skipped
            if skipped:
                result["warnings"].append(
                    f"{skipped} 条笔记详情未补齐（{scope_label}限流等待超时，列表数据已落库，下次同步重试）"
                )
            break
        try:
            note_pk = int(note.id)
        except Exception:  # noqa: BLE001 - 对象已失效（并发删除 / 会话过期）⇒ 跳过该篇
            result["failed"] += 1
            continue
        result["attempted"] += 1
        result["attempted_pks"].append(note_pk)
        # ⚠️ **篇级安全网**（2026-09-27 真机实测 `Could not refresh instance`）：
        # `hydrate_note_detail` 内部会 commit ⇒ 会话里其它已加载对象被 expire；
        # 某行若在这期间失效，后续访问它的属性会抛 `InvalidRequestError`。
        # 旧实现没有这一层 ⇒ **一篇失败炸掉整个补齐批次**（真实库 task 45：
        # stored=60 之后整任务 failed，剩余 1700+ 篇一篇都没补）。
        # 安全网不能条件性（DEF-048）：单篇任何异常都只记这一篇，继续下一篇。
        try:
            outcome = hydrate_note_detail(
                db, account, note, account_cookies=cookies, adapter=adapter
            )
        except Exception as exc:  # noqa: BLE001 - 单篇失败不停批
            logger.warning("note hydrate: 单篇补齐异常 (note_pk=%s): %s", note_pk, exc)
            result["failed"] += 1
            try:
                # 失败的 commit 会留下回滚态；不回滚则**下一篇也写不进**。
                db.rollback()
            except Exception:  # noqa: BLE001
                logger.exception("note hydrate: rollback failed (note_pk=%s)", note_pk)
            continue
        status = outcome.get("status")
        if status == "ok":
            if (
                outcome.get("content_filled")
                or outcome.get("images_added")
                or outcome.get("video_added")
            ):
                result["filled"] += 1
        elif status == "no_token":
            # 缺令牌**不是失败**：它是「用户补齐登录就能好」的前置缺失，所以
            # ① 不计入 `failed`（否则任务行会把它报成取数失败，归因错了）；
            # ② 不写 `hydrated_at`（`hydrate_note_detail` 里已保证）⇒ 下次同步重试。
            result["no_token"] += 1
        elif status == "no_url":
            result["no_url"] += 1
        else:
            result["failed"] += 1
        # ⚠️ **每篇都回调**（2026-09-28 用户口径「进度条慢慢增加」）：回调体只写内存态的
        # `processed`（**不 commit**），成本可忽略；而旧实现的「每 5 篇一次」在额度
        # 8 次/分钟下 ≈ 37 秒才动一格 —— 用户看到的仍是「不动」。刷盘仍由
        # `hydrate_note_detail` 自己的 commit 顺带完成（`_report_detail_progress` 的纪律不变）。
        if on_progress is not None:
            try:
                on_progress(result["attempted"], len(pending))
            except Exception:  # noqa: BLE001 - 观测失败不得影响执行
                logger.debug("note hydrate progress callback failed", exc_info=True)
    return result


def _hydrate_page_notes(
    db: Session,
    account: PlatformAccount,
    note_ids: list[str],
    *,
    cookies: str,
    adapter: Any,
    limiter: Any,
    task_id: Optional[int] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """把**本页刚落库**的笔记就地补齐：正文 / 完整图集 / 视频流地址 / 话题（§19.14）。

    与商品同步的 ``_fetch_page_skus`` 同构：翻页循环**内联**完成，一次同步拿全再落库。

    候选 = 本页 note_id 里 ``_needs_hydration`` 为真的行（带账号归属闸，防串号）。
    循环体在 :func:`_hydrate_note_rows`（与账号级补齐共用，不另写一份）。

    ``on_progress``（2026-09-28 加）：逐篇回调 —— 页内补齐是最长的一段（一页 50 条
    按 www 额度 8 次/分钟约 6 分钟），不接回调的话进度条在这 6 分钟里一动不动。
    """
    ids = [note_id for note_id in dict.fromkeys(note_ids) if note_id]
    result = _new_hydration_result()
    # 2026-09-28：本页**候选条数**（含不需要补齐的）—— 进度口径 `processed` 要把它算进去，
    # 否则「无需补齐的行」永远不算「已处理」，进度条到不了 100%。
    result["page_total"] = len(ids)
    if not ids or adapter is None:
        # 没有 www 会话 ⇒ 一条详情请求都不发；这些行**本任务已处理完**（没有可做的详情活）
        # ⇒ `page_total` 照给，页尾会把它们计入 `processed`（否则进度条永远差一截）。
        return result

    from backend.app.models import Note

    notes = db.scalars(
        select(Note).where(
            Note.user_id == account.user_id,
            Note.platform_account_id == account.id,
            Note.note_id.in_(ids),
        )
    ).all()
    pending = [note for note in notes if _needs_hydration(db, note)]
    result = _hydrate_note_rows(
        db,
        account,
        pending,
        cookies=cookies,
        adapter=adapter,
        limiter=limiter,
        task_id=task_id,
        on_progress=on_progress,
    )
    result["page_total"] = len(ids)
    return result


def _hydrate_account_pending(
    db: Session,
    account: PlatformAccount,
    *,
    cookies: str,
    adapter: Any,
    limiter: Any,
    task_id: Optional[int] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
    exclude_pks: Optional[set[int]] = None,
) -> dict[str, Any]:
    """账号级详情补齐：把该账号**所有还没补过详情**的笔记逐篇补齐（设计 13 §35.18）。

    2026-09-27 用户口径：「账号信息卡中移除补齐详情逻辑，点击同步笔记时，应获取到
    当前笔记的所有信息后再落库，**在一个任务中完成**，不需要再点击补齐详情」。

    为什么必须有这一步（而不是只靠页内联）：账号卡「同步笔记」默认 `mode=auto`，
    有游标的账号走**增量** ⇒ 第 1 页早停 ⇒ 页内联补齐的候选集（只取**本页** note_id）
    为 0 ⇒ **历史欠账永远补不到**（真实库账号 8：1867 条 `hydrated_at IS NULL`，
    其中没有一条是因为缺详情地址）。收尾扫一次账号全量候选，就把这条缺口封在
    同一个同步任务里 —— 用户点一次「同步笔记」，任务结束时该账号的笔记详情即为完整态。

    候选判据 = :func:`_needs_hydration`（`hydrated_at IS NULL`，或**视频笔记但本地没有
    视频资产**）—— 与页内联**同一把尺子**（DEF-044）。对整个账号的行跑一次该判据：
    `hydrated_at` 非空的行才会走到 `_has_video_asset` 那步（账号 8 实测 1970 行里只有
    103 行），代价可忽略，换来判据 100% 同源。

    ⚠️ **代价如实记账**（2026-09-27 设计 13 §38.1 修订）：额度按账号读运行时配置，
    默认 60 次/分钟 ⇒ 1867 篇欠账 ≈ **31 分钟**。改造前那个「≈ 6.2 小时」是
    **写死 5 次/分钟**算出来的（不是平台额度）；若把额度调回 5，估算回到 6.2 小时。
    这条路径**可中断、可续跑**（失败不写 `hydrated_at`，下次同步接着补；
    用户点取消在逐篇检查点生效），不要求用户一次等完。

    ⚠️ 没有 www 会话时返回**空统计**（一条请求都不发）：账号级、可恢复 ——
    补登网页版后下次同步自动补上（任务行由 `_sync_with_task` 安静说明一行）。

    ``exclude_pks`` = 本任务**页内联阶段已经试过**的笔记主键：跳过它们，别在同一个
    任务里对同一篇打两次详情（重复请求 = 白烧限流额度；也让 `detail_failed` 不会把同一篇数两遍）。
    注意「试过」不等于「补上了」—— 失败的行 `hydrated_at` 仍是 NULL，**下次同步**
    自然会重新进候选（`_needs_hydration` 的口径不变）。
    """
    if adapter is None:
        return _new_hydration_result()

    from backend.app.models import Note

    notes = db.scalars(
        select(Note)
        .where(
            Note.user_id == account.user_id,
            Note.platform_account_id == account.id,
        )
        .order_by(Note.id)
    ).all()
    skip = exclude_pks or set()
    pending = [
        note for note in notes if int(note.id) not in skip and _needs_hydration(db, note)
    ]
    return _hydrate_note_rows(
        db,
        account,
        pending,
        cookies=cookies,
        adapter=adapter,
        limiter=limiter,
        task_id=task_id,
        scope_label="账号",
        on_progress=on_progress,
    )


def _pc_total(raw_payload: Any) -> Optional[int]:
    """PC 链路的响应总数；取不到返回 ``None``（不造 0，设计 13 §5.2）。"""
    data = raw_payload.get("data") if isinstance(raw_payload, dict) else None
    if not isinstance(data, dict):
        return None
    for key in ("total", "total_count", "note_total"):
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _sleep_between_pages() -> None:
    """页间停顿 = 基础值 × 随机抖动（唯一实现 `sync_pacing.sleep_between_pages`）。

    读**模块级** `PAGE_GAP_SECONDS`（不是闭包常量），测试 monkeypatch 成 0
    仍然免等待。
    """
    from backend.app.services.sync_pacing import sleep_between_pages

    sleep_between_pages(PAGE_GAP_SECONDS, cap=MAX_PAGE_GAP_SECONDS)


def _creator_total_strict(raw_payload: Any) -> Optional[int]:
    """创作者响应的**真实**总数：只认 ``tags[checked].notes_count``。

    刻意**不**复用 `pc.py::_creator_total` —— 那个函数在字段缺失时退化成
    「本页条数」（对展示接口是可接受的「不精确」，对我们是**错的**：
    拿 2 当 1969 会把「有待同步」的分母写坏，同步永远判定为「已同步完」）。
    取不到就返回 ``None``，让调用方留空（设计 13 §5.2 / 硬约束⑤）。
    """
    data = raw_payload.get("data") if isinstance(raw_payload, dict) else None
    tags = data.get("tags") if isinstance(data, dict) else None
    if not isinstance(tags, list):
        return None
    for tag in tags:
        if isinstance(tag, dict) and tag.get("checked"):
            try:
                count = int(tag.get("notes_count"))
            except (TypeError, ValueError):
                continue
            if count > 0:
                return count
    return None


def _note_time_ms(item: dict[str, Any]) -> int:
    """笔记行的发布时间（**毫秒**，失败返回 0）。

    ⚠️ **读的是归一化契约的键 `timestamp`**，不是平台原始键（2026-09-22 修）：
    三个源归一化后**都**输出 `timestamp` —— creator（`pc.py::_normalize_creator_note`，
    形如 ``"2026-08-13 09:30"`` 的字符串）、PC（`_normalize_search_item`，数字或字符串）、
    ark（`_normalize_ark_note`，毫秒整数）。旧实现只认
    ``publish_time_ms / publish_time / time_ms / create_time_ms``，**一个都对不上**
    ⇒ 恒返回 0 ⇒ `filter_fresh_notes` 把每行都当「旧行」早停、`advance_note_cursor`
    永远推进不了 —— **增量同步在生产的三个源上全是空转**，而单测用的是自造字典所以全绿
    （DEF-049/051 的「夹具共谋」又一例：测的实现 ≠ 产的形状）。

    解析口径与 `qianfan.py::_parse_time` 对齐（同一语义不写第二份）：
    数字按毫秒、小于 10 位按秒；字符串按 ``YYYY-MM-DD HH:MM[:SS]`` / ISO / 纯日期。
    """
    for key in ("timestamp", "publish_time_ms", "publish_time", "time_ms", "create_time_ms"):
        value = item.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            stamp = int(value)
            # 秒级时间戳（10 位）自动升毫秒，否则 1970 附近的毫秒值会把区间筛成空集。
            return stamp * 1000 if stamp < 10_000_000_000 else stamp
        text = str(value).strip()
        if not text:
            continue
        if text.isdigit():
            stamp = int(text)
            return stamp * 1000 if stamp < 10_000_000_000 else stamp
        parsed = _parse_note_time_text(text)
        if parsed is not None:
            return parsed
    return 0


def _parse_note_time_text(text: str) -> Optional[int]:
    """把 ``YYYY-MM-DD[ HH:MM[:SS]]`` / ISO 字符串按**上海时区**解析成毫秒。"""
    from datetime import datetime, timedelta, timezone

    normalized = text.replace("T", " ").replace("Z", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        return int(parsed.replace(tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
    return None


def _note_id(item: dict[str, Any]) -> str:
    return str(item.get("note_id") or item.get("id") or "")


def filter_window_notes(
    items: list[dict[str, Any]], from_ms: int, to_ms: int = 0
) -> tuple[list[dict[str, Any]], bool]:
    """**时间窗**过滤（设计 13 §17.5.1 / §23）。返回 ``(in_window, should_stop)``：

    * ``in_window`` = 发布时间落在 ``(from_ms, to_ms]`` 的行；
    * ``should_stop`` = **整页都旧于下界** —— 列表按发布时间倒序，后续页必然更旧，
      可以早停省请求（风控）。

    ``to_ms = 0`` 表示没有上界 ⇒ 行为与旧的 `filter_fresh_notes(items, cursor)`
    **逐字节一致**（增量同步只用下界）。

    页内**部分**界外行**不**停：页内时间不保证严格单调（实测有并列），
    只要窗口里还有行就继续翻。
    """
    in_window: list[dict[str, Any]] = []
    oldest_seen: int | None = None
    for item in items:
        stamp = _note_time_ms(item)
        if oldest_seen is None or (stamp and stamp < oldest_seen):
            oldest_seen = stamp or oldest_seen
        if stamp <= from_ms:
            continue
        if to_ms and stamp > to_ms:
            continue  # 比上界还新（用户选的区间截止之前）—— 跳过但不停
        in_window.append(item)
    # 整页没有任何一行新于下界 ⇒ 后面只会更旧。
    should_stop = bool(items) and not any(_note_time_ms(it) > from_ms for it in items)
    return in_window, should_stop


def filter_fresh_notes(
    items: list[dict[str, Any]], last_cursor_ms: int
) -> tuple[list[dict[str, Any]], bool]:
    """增量同步的「页内过滤」（设计 13 §17.5.1）。

    ``filter_window_notes(items, last_cursor_ms)`` 的**薄包装**（只有下界），
    保留是为了不制造第二份实现：两边的语义必须完全一致（DEF-044/047/058）。
    """
    return filter_window_notes(items, last_cursor_ms, 0)


def advance_note_cursor(items: list[dict[str, Any]], last_cursor_ms: int) -> tuple[int, str, bool]:
    """算「本次成功落库后」的新游标（D13 / §17.9）。

    用 ``publish_time`` + ``note_id`` 双值取最大，防同毫秒并列漏行。
    返回 ``(new_cursor_ms, new_note_id, advanced)``；不比旧游标新时 ``advanced=False``。
    """
    if not items:
        return last_cursor_ms, "", False
    newest = max(items, key=lambda it: (_note_time_ms(it) or 0, _note_id(it)))
    new_ms = _note_time_ms(newest) or 0
    if new_ms > last_cursor_ms:
        return new_ms, _note_id(newest), True
    return last_cursor_ms, "", False


def _normalize_ark_note(entry: dict[str, Any]) -> dict[str, Any]:
    """千帆商品笔记条目 → 共享笔记契约（§19.1.1 实测字段，2026-09-21）。

    响应字段（`goods-note/list` 的 `note_infos[]`）：`note_id`/`note_title`/
    `note_image`/`note_type`/`publish_time`/`browse_num`/`like_num`/`collect_num`/
    `comment_num`/`share_num`/`item_id`/`item_title`/`item_image`/`variant_desc`/
    `goods_infos`/`xsec_token`/`note_flag`。

    两个**刻意的空缺**（不造假，硬约束⑤）：
    * ``content`` 恒为 "" —— 列表没有正文，靠 www 会话的详情回填补齐；
    * ``author_*`` 恒为 "" —— 响应没有作者块（买手笔记的作者不在店铺视角里）。
    """
    # 指标转换委托 pc.py 唯一实现（`_metric`，缺值 0 的计数口径）。
    from backend.app.api.platforms.xhs.pc import _metric

    note_id = str(entry.get("note_id") or "").strip()
    xsec_token = str(entry.get("xsec_token") or "").strip()
    image = str(entry.get("note_image") or "").strip()
    publish_time = entry.get("publish_time")
    return {
        "note_id": note_id,
        # 与 creator 源同一形态：explore URL + 笔记级 xsec_token（详情回填的探针要用）。
        "note_url": (
            f"https://www.xiaohongshu.com/explore/{note_id}"
            + (f"?xsec_token={xsec_token}" if xsec_token else "")
            if note_id
            else ""
        ),
        "title": str(entry.get("note_title") or "").strip(),
        "content": "",
        "author_id": "",
        "author_name": "",
        "author_avatar": "",
        "cover_url": image,
        "image_urls": [image] if image else [],
        "likes": _metric(entry.get("like_num")),
        "collects": _metric(entry.get("collect_num")),
        "comments": _metric(entry.get("comment_num")),
        "shares": _metric(entry.get("share_num")),
        "views": _metric(entry.get("browse_num")),
        "timestamp": publish_time if isinstance(publish_time, (int, float)) else 0,
        # Carried through because the detail backfill needs it; never rendered.
        "xsec_token": xsec_token,
        # `goods_infos` 在 pc.py::_GOODS_CONTAINER_KEYS 里 —— 商品↔笔记映射的
        # 唯一有真值的来源（2026-09-19 实测，见 pc.py 常量注释）。
        "goods": _extract_goods(entry),
        "raw": entry,
    }


def _extract_goods(entry: dict[str, Any]) -> Any:
    """商品挂载信息（委托 pc.py 唯一实现，这里只做薄包装避免循环导入）。"""
    from backend.app.api.platforms.xhs.pc import _extract_goods as _pc_extract_goods

    return _pc_extract_goods(entry)


def _creator_page_index(cursor: str) -> int:
    try:
        return int(cursor) if str(cursor).strip() else 0
    except (TypeError, ValueError):
        return 0


def _fetch_page(
    *,
    db: Session,
    account: PlatformAccount,
    cookies: str,
    kind: str,
    pc_adapter: Any,
    identity: Any,
    creator_factory: Any,
    ark_adapter: Any,
    page_size: int,
    cursor: str,
) -> tuple[list[dict[str, Any]], str, bool, Optional[int]]:
    """取一页笔记，统一成 ``(items, cursor_out, has_more, total)``。

    ``kind``：``pc`` / ``creator`` / ``ark``（§19.3 三源）。
    """
    if kind == "pc":
        from backend.app.api.platforms.xhs.pc import (
            _NOTE_LIST_KEYS,
            _NOTE_XSEC_SOURCE,
            _items_from_payload,
            _normalize_search_item,
            _pagination_from_payload,
        )

        success, message, raw_payload = pc_adapter.get_user_posted_page(
            identity.user_id, cursor, identity.xsec_token, identity.xsec_source
        )
        if not success:
            raise SyncFailure(message or "笔记列表获取失败")
        items = [
            _normalize_search_item(item, _NOTE_XSEC_SOURCE["notes"])
            for item in _items_from_payload(raw_payload, _NOTE_LIST_KEYS)
        ] or []
        cursor_out, has_more = _pagination_from_payload(raw_payload)
        return items, cursor_out, has_more, _pc_total(raw_payload)

    if kind == "ark":
        # 千帆商品笔记（§19.1.1 实测：GET goods-note/list，page_num 分页，
        # end_tag 结束信号，total 是过滤后条数）。SKU 级挂载商品随条目带给
        # `goods_infos`，不需要再打商品接口。
        from backend.app.api.platforms.xhs.pc import _items_from_payload

        success, message, raw_payload = ark_adapter.get_goods_note_page(
            page_num=_creator_page_index(cursor) or 1, page_size=page_size
        )
        if not success:
            raise SyncFailure(
                message or "千帆商品笔记列表获取失败", code="ARK_NOTE_LIST_FAILED"
            )
        items = [
            _normalize_ark_note(entry)
            for entry in _items_from_payload(raw_payload, ("note_infos",))
        ]
        data = raw_payload.get("data") if isinstance(raw_payload, dict) else None
        data = data if isinstance(data, dict) else {}
        end_tag = bool(data.get("end_tag"))
        cursor_out = str((_creator_page_index(cursor) or 0) + 1)
        # total 取不到保持 None（不造 0，与 `_pc_total` / `_creator_total_strict` 同口径）。
        try:
            ark_total: Optional[int] = int(data.get("total"))
        except (TypeError, ValueError):
            ark_total = None
        return items, cursor_out, not end_tag, ark_total

    # Creator 链路：自己取原始响应，好让「总数」走严格口径（见 _creator_total_strict）。
    from backend.app.api.platforms.xhs.pc import (
        _NOTE_LIST_KEYS,
        _creator_pagination,
        _items_from_payload,
        _normalize_creator_note,
    )

    adapter = creator_factory(cookies)
    success, message, raw_payload = adapter.get_posted_notes_page(
        page=_creator_page_index(cursor), tab=0
    )
    if not success:
        raise SyncFailure(message or "创作者平台笔记列表获取失败")
    items = [
        _normalize_creator_note(item)
        for item in _items_from_payload(raw_payload, _NOTE_LIST_KEYS)
    ]
    cursor_out, has_more = _creator_pagination(raw_payload)
    return items, cursor_out, has_more, _creator_total_strict(raw_payload)


def _merge_ark_notes(db: Session, account: PlatformAccount, items: list[dict[str, Any]]) -> int:
    """ark 源落库：**填空式合并**（§19.3.2）。返回处理条数（建 + 合并）。

    与 `crawl._save_normalized_notes`（整行覆盖）是**两种语义**，所以是两个函数：
    PC/creator 源先跑、条目带正文潜力；ark 源后跑若整行覆盖，会把已落库的正文
    抹成空。本函数的纪律：

    * 既有行：**只补空字段**（title/content/author_name），`raw_json` **补键不冲键**
      （PC 的顶层指标原样保留，ark 的指标与商品信息进 `ark` 子键，同源新数据覆盖旧数据）；
    * 资产：**只在完全没有资产时**下图（不删不改已有的）；
    * 新行：直接建（标题 / 封面 / 商品信息；正文留空，等 www 会话的详情回填补）。

    **发布状态**（设计 13 §19.12）：ark 源拉的同样是**账号自己已在平台上发布**的商品
    笔记，所以新建行 `published=True`、命中既有行也把该列**升**为 `True`（只升不降）。
    与「只补空字段」的填空语义并不冲突 —— 填空针对的是正文/作者/资产，而这一列是
    **链路事实**，不是缺值。

    **笔记类型**（设计 13 §25）：与 `published` 同一把尺子 —— ark 源是同步链路的一份子，
    新建行 `note_type="synced"`、命中既有行升为 `synced`（只升不降）。采集链路后跑
    命中同一行时**不碰**这一列 ⇒ 同步笔记不会变成本地笔记、详情卡的「同步」按钮不消失。
    """
    from backend.app.api.platforms.xhs.crawl import _download_asset, _image_urls, _raw_with_metrics
    from backend.app.models import Note, NoteAsset

    merged = 0
    for normalized in items:
        note_id = str(normalized.get("note_id") or "").strip()
        if not note_id:
            continue
        note = db.scalars(
            select(Note).where(Note.user_id == account.user_id, Note.note_id == note_id)
        ).first()
        ark_raw = _raw_with_metrics(normalized)
        if note is None:
            note = Note(
                user_id=account.user_id,
                platform_account_id=account.id,
                platform=account.platform,
                note_id=note_id,
                # 同步链路落库 ⇒ 三方同步笔记（§25）。
                note_type=NOTE_TYPE_SYNCED,
            )
            # 千帆商品笔记 = 账号已在平台上发布的作品（设计 13 §19.12）：发布状态经
            # **唯一入口**写（§32.3.2），`published` 投影由入口同步维护。
            set_note_publish_status(note, PUBLISH_STATUS_PUBLISHED)
            note.title = str(normalized.get("title") or "")
            note.content = str(normalized.get("content") or "")
            note.author_name = str(normalized.get("author_name") or "")
            note.raw_json = ark_raw
            db.add(note)
            db.flush()
            # §34：ark 条目 content 恒为空（列表无正文），声明话题也没有 ⇒ 这里是
            # 幂等 no-op；万一带了正文 / 话题，与 creator 源同一把尺子拆（唯一实现）。
            apply_sync_topics(db, note)
            # ⚠️ 持锁纪律（设计 13 §24.7）：**先落笔记行（短事务），再下载素材（零锁），
            # 再落资产行**。旧实现 flush 后逐图下载、页末才 commit ⇒ 写锁跨网络持有
            # 数十秒~数分钟，其他类型任务的写者全被顶到 `database is locked`。
            db.commit()
            for url in _image_urls(normalized):
                local_name = _download_asset(url, account.user_id, "image")
                db.add(
                    NoteAsset(
                        note_id=note.id, asset_type="image", url=url, local_path=local_name or ""
                    )
                )
            db.commit()
        else:
            # 与 _save_normalized_notes 同一口径：命中既有行重挂归属（P2）。
            note.platform_account_id = account.id
            # 只升不降（§19.12）：同步看到的这一条在平台上就是已发布状态。
            set_note_publish_status(note, PUBLISH_STATUS_PUBLISHED)
            # 只升不降（§25）：同步链路见过这条 ⇒ 三方同步笔记。
            note.note_type = NOTE_TYPE_SYNCED
            if not note.title:
                note.title = str(normalized.get("title") or "")
            if not note.content:
                note.content = str(normalized.get("content") or "")
            if not note.author_name:
                note.author_name = str(normalized.get("author_name") or "")
            # §34：与新建分支同一把尺子（ark 条目通常无正文无话题 ⇒ 幂等 no-op）。
            apply_sync_topics(db, note)
            # ⚠️ 必须赋**新 dict**：JSON 列没有挂 MutableDict，原地改
            # （`raw["ark"] = ...`）再赋回同一个对象，SQLAlchemy 的历史比对
            # 看到「还是那个对象」就不发 UPDATE —— 合并会静默丢失（本函数
            # 第一版就是这么死的，测试插桩才发现 raw 键没多出来）。
            raw = note.raw_json if isinstance(note.raw_json, dict) else {}
            ark_bucket = raw.get("ark") if isinstance(raw.get("ark"), dict) else {}
            ark_bucket.update(ark_raw)  # 同源新数据覆盖旧数据；PC 顶层键一个字节不动
            merged_raw = dict(raw)
            merged_raw["ark"] = ark_bucket
            note.raw_json = merged_raw
            # ⚠️ 持锁纪律（§24.7）：字段更新先落库（短事务）再按需下图 ——
            # 下载在网络侧进行，不持锁。
            db.commit()
            has_assets = db.scalars(
                select(NoteAsset).where(NoteAsset.note_id == note.id).limit(1)
            ).first()
            if not has_assets:
                for url in _image_urls(normalized):
                    local_name = _download_asset(url, account.user_id, "image")
                    db.add(
                        NoteAsset(
                            note_id=note.id,
                            asset_type="image",
                            url=url,
                            local_path=local_name or "",
                        )
                    )
                db.commit()
        merged += 1
    return merged


def _sync_source(
    db: Session,
    task: Task,
    account: PlatformAccount,
    source: dict[str, Any],
    *,
    limiter: Any,
    max_pages: Optional[int],
    page_size: int,
    profile_url: str,
    xsec_token: str,
    incremental: bool,
    last_cursor_ms: int,
    time_from_ms: int = 0,
    time_to_ms: int = 0,
    www_cookies: str = "",
    www_adapter: Any = None,
    backlog_work: int = 0,
) -> dict[str, Any]:
    """**单个源**的翻页同步（**全量** + 限流等待 + 逐页落库，设计 13 §21）。

    源内**首页失败抛 `SyncFailure`**（交给编排器记这个源失败、继续下一个源）；
    后续页失败保留已同步的（warning）。返回本源的计数与游标推进。

    ``time_from_ms`` / ``time_to_ms``（§23，用户自选时间区间）：用户选「某个时间段」
    时只落窗口内的行；``(0, 0)`` = 不设界（全量 / 增量的既有行为）。**窗口与增量
    不互斥** —— 增量是「从游标往后」，窗口是「夹在起止日之间」，两者是 and 关系。

    ``www_cookies`` / ``www_adapter``（§19.14）：页内联详情补齐用的 www 会话与
    适配器，由编排器**解析一次**传进来（适配器为 None = 账号没有 www 会话 ⇒ 本页
    不补、也不发任何请求）。

    ``backlog_work``（2026-09-28）：启动时该账号**本次任务能处理的**详情欠账条数
    （编排器算一次传进来：欠账总数 − 本地没令牌、处理不了的那些），
    用于算进度分母 `work_total`（见 `_update_progress` 的 `processed` / `work_total`）。
    """
    from backend.app.api.platforms.xhs.crawl import _save_normalized_notes

    kind = source["kind"]
    cookies = source["cookies"]
    pc_adapter = identity = creator_factory = ark_adapter = None

    if kind == "pc":
        from backend.app.api.platforms.xhs.pc import (
            _LIST_XSEC_SOURCE,
            _resolve_identity,
            get_xhs_pc_api_adapter_factory,
        )

        pc_adapter = get_xhs_pc_api_adapter_factory()(cookies)
        # 令牌只能从请求体流进来：`/v2/user/me` 给 user_id 但不给 URL 级
        # `xsec_token`，而 `user_posted` 恰恰要那个（设计 13 §4.1 的修订说明）。
        identity = _resolve_identity(
            pc_adapter, profile_url, xsec_token, "", _LIST_XSEC_SOURCE["notes"]
        )
        if not identity.user_id:
            raise SyncFailure(
                identity.self_message or "无法识别账号身份",
                code="IDENTITY_UNRESOLVED",
            )
        if not identity.xsec_token:
            # 不静默同步空集：令牌缺失会让列表请求被平台拒绝，
            # 「同步成功但 0 条」会把缺令牌伪装成「账号没笔记」。
            raise SyncFailure(
                "需要主页链接（xsec_token）才能读取笔记列表。",
                code="XSEC_TOKEN_REQUIRED",
            )
    elif kind == "creator":
        from backend.app.api.platforms.xhs.pc import get_xhs_creator_api_adapter_factory

        creator_factory = get_xhs_creator_api_adapter_factory()
    else:
        from backend.app.adapters.xhs.qianfan_api_adapter import (
            get_qianfan_api_adapter_factory,
        )

        ark_adapter = get_qianfan_api_adapter_factory()(cookies)

    stored = 0
    goods_stored = 0
    content_filled = 0
    # §19.14：本页内联补齐的失败条数（详情请求失败 / 平台报错）—— 列表数据已落库，
    # 由编排器聚合成**一行**任务说明（与商品侧 detail_failed 同口径），不每页一行。
    detail_failed = 0
    # §35.15（2026-09-27）：本页内联补齐里「本地没有详情地址」被跳过的条数。
    # 原来这个数字**只存在于返回值里**、从不上报 ⇒ 用户看不到「有 N 条被静默跳过」。
    # 现在由编排器聚合成一行安静说明（DEF-058 同型：静默跳过是「不知道发生了什么」的源头）。
    no_url = 0
    # §19.13.9（2026-09-28）：本页内联补齐里「缺详情令牌」被拦下的条数 —— 与
    # `no_url` **分开报**：缺令牌是**用户补齐登录就能好**的（可自救），`no_url` 是
    # 本地数据缺失（不会自愈）。混进一个数字里，用户不知道该做什么（DEF-058 口径）。
    no_token = 0
    # §35.18：本页内联**试过详情**的本地笔记主键（跨页累积）—— 交给编排器，
    # 让账号级收尾那段跳过它们（同一任务不对同一篇打两次详情）。
    attempted_note_pks: list[int] = []
    # 2026-09-28：**本任务累计已处理条数** + 预计总工作量（进度条看的那一对，
    # 见 `_update_progress`）。`work_total` 在首个页边界算一次，之后只增不减。
    processed = 0
    work_total: Optional[int] = None
    total: Optional[int] = None
    # §35.18.6：**有界同步**（增量 / 用户选的时间区间）会早停 ⇒ `stored` 永远追不上
    # 平台报告的账号总量（`total`）；拿它当进度分母 = 进度条恒 0%，用户以为卡死而
    # 反复取消（2026-09-27 报障）。有界时**不报分母**，让「比例未知」如实表达。
    bounded = incremental or bool(time_from_ms) or bool(time_to_ms)
    cursor = ""
    warnings: list[str] = []
    stopped = False
    cursor_ms = last_cursor_ms
    cursor_note_id = ""
    cursor_advanced = False  # 游标只在成功落库后推进（D13 / DEF-043）
    # §27 对账融合：本轮**枚举到、且落在同步范围内**的平台 note_id，以及「枚举
    # 是否完整」。完整 = 翻到平台没有下一页 / 游标 -1 / 已落库 ≥ total / **窗口内
    # 早停**（整页旧于下界 ⇒ 范围内翻完）；不完整 = 限流超时 / 页数兜底 / 显式
    # max_pages / 后续页失败 —— 那些情形下「平台没有它」的结论不成立（DEF-057）。
    seen_ids: list[str] = []
    enumerate_complete = True

    page_no = 0
    while True:
        # 协作式中断检查点（设计 13 §24.8）：每翻一页前看一次取消信号，
        # 命中即停 —— 不再打平台、不再落库，由编排器写 cancelled 终态。
        if task_cancel.is_cancelled(task.id):
            break
        page_no += 1
        # 绝对兜底（设计 13 §21.2）：平台异常导致 `has_more` 恒真时必须能停下来，
        # 否则「取消页数上限」= 死循环。
        if page_no > sts.ABSOLUTE_PAGE_CAP:
            warnings.append(f"已达绝对页数兜底（{sts.ABSOLUTE_PAGE_CAP} 页）")
            enumerate_complete = False
            break
        # 只有调用方**显式**要了小批量才停在这里（调试用）。不传时 `max_pages is None`，
        # 这条永不触发 —— 旧契约「默认 3 页上限」已被 §21 取代。
        if max_pages is not None and page_no > max_pages:
            warnings.append(f"已达指定页数上限（{max_pages} 页）")
            enumerate_complete = False
            break

        # 限流键带源（§19.11 真机教训）：平台风控按平台各自独立，三源共用一个
        # 账号键会让先跑的源把额度耗光、后跑的源第 1 页就被挡。
        #
        # ⚠️ **等待**而不是 break（设计 13 §21.3）：额度再高也有烧完的时候（页间只
        # sleep 5 秒 × 抖动 ⇒ 一分钟能打十几页），这里 break 等于把「全量」悄悄改回
        # 「有界」，问题原样存在。
        if not limiter.wait_allow(f"{account.id}:{kind}"):
            stopped = True
            warnings.append(f"第 {page_no} 页前限流等待超时")
            enumerate_complete = False
            break

        try:
            items, cursor_out, has_more, page_total = _fetch_page(
                db=db,
                account=account,
                cookies=cookies,
                kind=kind,
                pc_adapter=pc_adapter,
                identity=identity,
                creator_factory=creator_factory,
                ark_adapter=ark_adapter,
                page_size=page_size,
                cursor=cursor,
            )
        except SyncFailure:
            # 首页失败 = 一行都没落 → 抛给编排器；后续页失败保留已同步的。
            if page_no == 1:
                _update_progress(
                    db,
                    task,
                    stored=stored,
                    total=None if bounded else total,
                    goods_stored=goods_stored,
                )
                raise
            warnings.append(f"第 {page_no} 页获取失败")
            enumerate_complete = False
            break

        if page_total is not None:
            total = page_total

        # 时间界：增量的下界是游标，用户自选区间的下界是 start_date（§23）。
        # 两者是 **and**：用户选「9-01 ~ 9-10」且账号有同步历史时，只会取
        # 「比游标新 **且** 落在 9-01~9-10」的行 —— 任何一边不满足都不该落库。
        window_from = max(last_cursor_ms, time_from_ms) if incremental else time_from_ms
        if window_from or time_to_ms:
            in_window, should_stop = filter_window_notes(items, window_from, time_to_ms)
            items = in_window
            if should_stop:
                # 整页都旧于下界 ⇒ 后续页只会更旧（列表按发布时间倒序）。
                # 区分文案：用户能看出是「增量追平了」还是「翻过自己选的区间了」。
                # ⚠️ 这是**范围内翻完**（in-scope complete），不是「没翻完」——
                # §27 对账的枚举完整性判据认它（范围内该看到的都看到了）。
                if time_from_ms and time_from_ms >= last_cursor_ms:
                    warnings.append(f"第 {page_no} 页已早于所选起始日期，停止翻页")
                else:
                    warnings.append(f"第 {page_no} 页全部早于上次同步位置，增量早停")
                break

        # §27：收集本轮枚举到、且落在同步范围内的平台 note_id（对账用）。
        # 放在窗口过滤**之后** —— 窗口外的行不参与对账判定。
        seen_ids.extend(note_id for note_id in (_note_id(item) for item in items) if note_id)

        if kind == "ark":
            # 填空合并：不覆盖 PC/creator 已落库的正文/作者/资产（§19.3.2）。
            stored += _merge_ark_notes(db, account, items)
            # §26：ark 条目自带 goods_infos ⇒ 就地解析「本地笔记 ↔ 本地商品」关系
            # （与 creator 源的证据缓存路径汇到同一个解析器）。
            _link_ark_note_goods(db, account, items)
        else:
            # published=True：**同步链路看到的都是账号已在平台上发布的笔记**，
            # 与触发方式（手动 / 定时）无关（§19.12.1）——按 `source == "scheduled"`
            # 判会把「手动同步回来的已发布作品」错标成待发布。
            # note_type="synced"：同一把尺子的来源侧（§25）—— 同步链路落库的行
            # 是三方同步笔记，采集链路落库的才是本地笔记。
            saved = _save_normalized_notes(
                db, account, items, published=True, note_type=NOTE_TYPE_SYNCED
            )
            stored += len(saved)

        # ⚠️ **进度分母 = 本次任务「启动时可知」的工作量**（设计 13 §43.1，2026-09-28
        # 用户报障后收紧）：
        #   * 无界全量：平台报告的账号总量（拿不到 ⇒ 0，**不拿 `stored` 冒充**）；
        #   * 有界（增量 / 区间）：**0** —— 列表阶段要翻多少条**只有跑完才知道**
        #     （§35.18.6 禁的是报 `total` 这个键，本轮补上的是「也不许拿运行中的量充数」）；
        #   * 详情欠账数（启动时数得清）**永远**计入。
        # ⛔ **运行中才发现的量不进分母**：它与分子的「本页已处理」数的是**同一批笔记**
        # （两边每页同步 +N）⇒ 比值恒 100% ⇒ 进度条**第一页就打满** —— 这正是 2026-09-28
        # 用户报障「刚开跑就 100%」的真身（旧实现在有界分支拿「已落库条数」当分母）。
        # 唯一例外：**欠账 > 0** 时把「本任务已落库条数」也叠加进分母 —— 那一段工作量确实
        # 属于本次任务（欠账是它的延续），叠加后 `processed ≤ work_total` 恒成立（不虚高），
        # 且恰在欠账补完那一刻到 100%；**欠账 = 0 时叠加就是同源，禁止**。
        # **只在首个页边界算一次**，之后只允许变大（变小 = 进度倒退，禁止）。
        candidate = 0 if bounded else (int(total) if total is not None else 0)
        candidate += int(backlog_work)
        if backlog_work > 0:
            candidate += int(stored)
        if candidate > 0:
            work_total = candidate if work_total is None else max(work_total, candidate)

        # ⚠️ **落库即刷进度**（设计 13 §42.3，2026-09-28 用户口径「每执行 50 条应更新一次
        # 任务执行详情数据」）：本页的 `stored` 此刻已经确定，而**下面**的页内详情补齐
        # 要逐篇打 www 详情（额度 8 次/分钟 ⇒ 一页 50 条约 6 分钟）。
        # 旧实现把刷盘点放在补齐**之后**（页尾那次 `_update_progress`）⇒ 这 6 分钟里
        # 用户看到的是一个**不动的数字**，任务日志的进度条整页只跳一格且要等到最后 ——
        # 这正是「进度不更新」的真身。刷盘点前移到落库之后：数字与事实同刻。
        # ⚠️ 必须 `commit`：进度泵（`task_progress_probe`）是**独立 Session**，
        # 不提交它读不到（§35.17.3）。页尾那次保留 —— 它要补 `goods_stored`。
        _update_progress(
            db,
            task,
            stored=stored,
            total=None if bounded else total,
            goods_stored=goods_stored,
            processed=processed,
            work_total=work_total,
        )
        db.commit()

        # 正文 / 图集 / 视频流地址的补齐**在本页内联完成**（设计 13 §19.14）：
        # 落库后就地打一次 www 详情，一次同步拿全再落库。账号级补齐队列 /
        # 跨轮累积 / 「每轮 cap=30」那套契约已移除（§19.14.1）。
        page_note_ids = [note_id for note_id in (_note_id(item) for item in items) if note_id]
        # 页内补齐的**逐篇**回调（2026-09-28）：把「本页已补 M 篇」换算成**任务累计
        # 已处理**再写进任务行 —— 这是「进度条在页内也慢慢涨」的唯一来源。
        # ⚠️ 不传 `detail_*`：页内补齐的分母只有本页待补数，写进去就是一页内 0→100 反复跳。
        # ⚠️ 回调体只改内存态（不 commit），刷盘借 `hydrate_note_detail` 自己的 commit。
        processed_base = processed

        def _on_page_hydrate(attempted_in_page: int, _batch_total: int) -> None:
            _report_detail_progress(
                db,
                task,
                stored=stored,
                total=None if bounded else total,
                goods_stored=goods_stored,
                processed=processed_base + int(attempted_in_page),
                work_total=work_total,
            )

        hydration = _hydrate_page_notes(
            db,
            account,
            page_note_ids,
            cookies=www_cookies,
            adapter=www_adapter,
            limiter=limiter,
            task_id=task.id,
            on_progress=_on_page_hydrate,
        )
        # 「本页**已处理**」= 无需补齐的（`page_total - pending_total`）+ 真尝试过的
        # （回调已经逐篇加过 `attempted`）。⚠️ **没尝试的不计入** —— 限流等待超时 /
        # 取消导致中途停下时，进度条到不了 100% 是**如实**的（`_finish` 写真实终态）。
        processed += int(hydration.get("page_total") or 0) - int(
            hydration.get("pending_total") or 0
        )
        content_filled += hydration["filled"]
        detail_failed += hydration["failed"]
        no_url += hydration["no_url"]
        no_token += hydration.get("no_token", 0)
        attempted_note_pks.extend(hydration.get("attempted_pks") or [])
        warnings.extend(hydration["warnings"])

        if items:
            # D13：游标取「已成功落库行的最大值」，且用 publish_time + note_id 双值
            # 防并列时间（§17.9）。三个源的 publish_time 同口径，共用账号级游标。
            #
            # ⚠️ **全量模式也要算**（§22）：全量扫过了整份列表，本来就观测到了最新
            # 的发布时间。旧实现把这段写在 `if incremental and items:` 里 ⇒ 全量
            # 同步的游标恒为 0 ⇒ 自动模式永远选全量。过滤 / 早停才是增量专属，
            # **算游标不是**—— 这两件事必须分开，别再合回去。
            new_ms, new_note_id, advanced = advance_note_cursor(items, cursor_ms)
            if advanced:
                cursor_ms = new_ms
                cursor_note_id = new_note_id
                cursor_advanced = True

        if kind == "creator" and account.sub_type == SUB_TYPE_CREATOR:
            goods_stored += _sync_goods_for_notes(db, account, items)

        _store_notes_count(db, account, total)
        # 进度分母：有界同步不报 `total`（§35.18.6）—— `total` 本身仍照原样返回给编排器
        # （`_store_notes_count` / 多源聚合要的是「账号笔记数」，与进度分母不是一件事）。
        # `processed` / `work_total` 才是进度条看的那一对（单调，见 `_update_progress`）。
        _update_progress(
            db,
            task,
            stored=stored,
            total=None if bounded else total,
            goods_stored=goods_stored,
            processed=processed,
            work_total=work_total,
        )
        db.commit()

        if not has_more or str(cursor_out).strip() == "-1":
            break
        if total is not None and stored >= total:
            break
        cursor = cursor_out
        _sleep_between_pages()

    return {
        "stored": stored,
        "goods_stored": goods_stored,
        "content_filled": content_filled,
        "detail_failed": detail_failed,
        "no_url": no_url,
        "no_token": no_token,
        "total": total,
        # §35.18：本页内联试过详情的笔记主键（编排器拿去给账号级收尾做去重）。
        "attempted_note_pks": attempted_note_pks,
        "warnings": warnings,
        "stopped_by_limit": stopped,
        "cursor_ms": cursor_ms,
        "cursor_note_id": cursor_note_id,
        "cursor_advanced": cursor_advanced,
        # §27 对账融合：本轮枚举到、且落在同步范围内的平台 note_id + 枚举完整性。
        # 编排器据此判「能不能下『平台没有它』的结论」。
        "seen_ids": seen_ids,
        "enumerate_complete": enumerate_complete,
        # 2026-09-28：进度条口径的两个数（编排器接着往下累加，见 `_sync_with_task`）。
        "processed": processed,
        "work_total": work_total,
    }


def _sync_with_task(
    db: Session,
    task: Task,
    account: PlatformAccount,
    sources: list[dict[str, Any]],
    *,
    max_pages: Optional[int],
    page_size: int,
    profile_url: str = "",
    xsec_token: str = "",
    mode: str = sts.SYNC_MODE_AUTO,
    time_from_ms: int = 0,
    time_to_ms: int = 0,
) -> dict[str, Any]:
    """多源编排（§19.3）：每个源各同步各的，互不阻塞。

    ``mode``：``full`` = 全量重扫 + upsert；``incremental`` = 只取比游标新的笔记；
    ``auto``（默认）= 按账号有没有同步历史自动选。详见设计 13 §17 / §22。

    ``time_from_ms`` / ``time_to_ms``（§23）：用户在弹窗里选的时间区间，
    ``(0, 0)`` = 不设界。与 ``mode`` 是 **and** 关系，不是替代关系。

    **全部源都失败**（或一个源都没有）⇒ 抛 `SyncFailure`，`code` 统一
    ``NOTE_SOURCES_UNAVAILABLE``，message 是**分源多原因**（每个源一行），
    前端结果行原样展示；部分成功 ⇒ `done` + note 说明（设计 13 §9）。
    """
    limiter = get_rate_limiter()
    # §19.14：页内联详情补齐用的 www 会话**在这里解析一次**（适配器构造零平台请求）。
    # 账号没有 www 会话 ⇒ `www_adapter=None` ⇒ 每页不补、一条请求都不发，任务行
    # 安静说明一行（账号级、可恢复：补登网页版后下次同步自动补上）。
    www_cookies = _account_cookies(db, account.id, channel=CHANNEL_WWW)
    www_adapter = None
    from backend.app.api.platforms.xhs.pc import _has_web_session

    if _has_web_session(www_cookies):
        from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory

        www_adapter = get_xhs_pc_api_adapter_factory()(www_cookies)
    # §22：``auto``（默认）在这里落成 full / incremental。判据是游标，不是
    # 「表里有没有行」—— 见 `resolve_note_sync_mode` 的说明。
    mode = sts.resolve_note_sync_mode(db, account.id, mode)
    incremental = mode == sts.SYNC_MODE_INCREMENTAL
    # §35.18.6：有界同步 = 增量或用户选了时间区间 ⇒ 会早停，进度分母不可知。
    bounded = incremental or bool(time_from_ms) or bool(time_to_ms)
    # 增量从游标起；全量从 0 起（扫全部，但**仍然**把观测到的最新时间写回游标，
    # 否则自动模式永远选全量 —— 见下面 `if cursor_advanced` 的注释）。
    last_cursor_ms = sts.load_note_cursor(db, account.id) if incremental else 0
    # §27 对账范围 = 本次同步范围（在游标被推进**之前**取，推进后取到的是新游标）。
    scope_from_ms = max(last_cursor_ms, time_from_ms) if incremental else time_from_ms
    scope_to_ms = time_to_ms

    cursor_note_id = ""
    cursor_advanced = False

    stored = 0
    goods_stored = 0
    content_filled = 0
    # §19.14：跨源聚合的「详情请求失败」条数（列表数据已落库，下次全量同步重试）。
    detail_failed = 0
    # §19.13.9（2026-09-28）：跨源聚合的「缺详情令牌、被拦下」条数 —— 与
    # `detail_failed` 分开：那个说「下次重试」，这个说「去补网页版登录」。
    no_token = 0
    warnings: list[str] = []
    stopped_by_limit = False
    source_results: list[dict[str, Any]] = []
    # §27：本轮枚举到的平台 note_id（跨源并集）+ 「能枚举全量笔记的源」是否完整
    # 翻完。后者是「平台没有它」这个结论的判据前提（DEF-057：样本不完整则结论不成立）。
    platform_ids: set[str] = set()
    full_list_complete = False
    # 2026-09-28：进度条口径 —— `processed` 跨源累加、`work_total` 取各源的最大值
    # （源之间是**并行取数**的语义，取最大而不是相加：相加会让「creator + ark 两个源
    # 覆盖同一批笔记」把分母算成两倍，进度条永远到不了 100%）。
    processed_total = 0
    work_total_max = 0
    # 启动时该账号的**详情欠账条数**：算进度分母用（`work_total = 列表工作量 + 欠账`）。
    # 只在编排器这里算一次（每个源都算一遍是白花一次 COUNT）。
    # ⚠️ 其中「本地完全没有详情令牌」的那些**本次任务处理不了**（用户补齐网页版登录
    # 才能好，如实报在 warnings 里）⇒ **不计入工作量** —— 算进去会让进度条永远差最后一截，
    # 用户读成「卡住了」（2026-09-27 报障的同一形态）。
    backlog_total = _pending_hydration_count(db, account)
    backlog_work = max(
        0, backlog_total - _pending_hydration_without_token_count(db, account)
    )
    # §35.18：本轮**页内联阶段已经试过详情**的本地笔记主键 —— 账号级收尾要跳过它们
    # （同一任务不对同一篇打两次详情；也让 `detail_failed` 不会把同一篇数两遍）。
    attempted_note_pks: set[int] = set()

    for source in sources:
        try:
            result = _sync_source(
                db,
                task,
                account,
                source,
                limiter=limiter,
                max_pages=max_pages,
                page_size=page_size,
                profile_url=profile_url,
                xsec_token=xsec_token,
                incremental=incremental,
                last_cursor_ms=last_cursor_ms,
                time_from_ms=time_from_ms,
                time_to_ms=time_to_ms,
                www_cookies=www_cookies,
                www_adapter=www_adapter,
                backlog_work=backlog_work,
            )
        except SyncFailure as exc:
            reason = f"{exc.code}: {exc.message}" if exc.code else exc.message
            source_results.append(
                {"source": source["source"], "stored": 0, "total": None, "error": reason}
            )
            continue
        stored += result["stored"]
        goods_stored += result["goods_stored"]
        content_filled += result["content_filled"]
        detail_failed += result.get("detail_failed") or 0
        attempted_note_pks.update(result.get("attempted_note_pks") or [])
        warnings.extend(result["warnings"])
        stopped_by_limit = stopped_by_limit or result["stopped_by_limit"]
        platform_ids.update(result.get("seen_ids") or [])
        # 2026-09-28：进度条口径跨源累加（分母取最大 —— 多源覆盖同一批笔记，
        # 相加会把分母算成两倍，进度条永远到不了 100%）。
        processed_total += int(result.get("processed") or 0)
        work_total_max = max(work_total_max, int(result.get("work_total") or 0))
        # ark 源只看得到商品笔记（子集）⇒ 凭它不能下「平台没有普通笔记」的结论；
        # creator / pc 源读的是全量列表（§21.4：creator 更全），它完整才算完整。
        if result.get("enumerate_complete") and source["kind"] in ("creator", "pc"):
            full_list_complete = True
        if result["cursor_advanced"] and result["cursor_ms"] > last_cursor_ms:
            last_cursor_ms = result["cursor_ms"]
            cursor_note_id = result["cursor_note_id"]
            cursor_advanced = True
        source_results.append(
            {
                "source": source["source"],
                "stored": result["stored"],
                "total": result["total"],
                "error": "",
                # 被限流 ⇒ 0 条是「没跑完」而不是「没数据」：摘要行必须能分开说
                # （§19.11 真机教训：千帆显示 0 条，实际第 1 页前就被挡了）。
                "limited": result["stopped_by_limit"],
            }
        )

    # 多源 total 聚合（§19.3.3）：**取各源报告的正数最大值**（最广口径 ≈ 账号
    # 的笔记总量；千帆商品笔记数是子集，PC/creator 列表是全量）。**0 视为
    # 「没报告」**（该源没有笔记，不贡献分母）—— 全部源都没给正数 ⇒ None，
    # 不造 0（硬约束⑤；单源时行为与从前逐字节一致）。
    reported = [entry["total"] for entry in source_results if entry.get("total")]
    total = max(reported) if reported else None
    # §35.18.6：**进度分母**与「账号笔记数」（上面的 `total`）不是一回事 ——
    # 有界同步（增量 / 区间）会早停，`stored` 追不上账号总量 ⇒ 一律不报分母。
    progress_total = None if bounded else total

    # 2026-09-28：**进度口径跨源收口** —— 把「累计已处理 / 预计总工作量」写一次，
    # 让下面的账号级补齐从同一对数字继续。不写的话多源时 payload 里留下的是
    # **最后一个源**的数（分母可能更小 ⇒ 进度条虚高）。
    if work_total_max:
        _update_progress(
            db,
            task,
            stored=stored,
            total=progress_total,
            goods_stored=goods_stored,
            processed=processed_total,
            work_total=work_total_max,
        )

    # 协作式中断（设计 13 §24.8）：取消信号命中 ⇒ 不做对账、不推进游标、不走 done
    # 收尾，直接写 cancelled 终态（`_finish` 内的竞态守卫是第二道保险）。
    if task_cancel.is_cancelled(task.id):
        # §35.18.6：取消 ⇒ **收尾段（详情令牌刷新 + 账号级补齐）整段没跑**，
        # 正文 / 图集还欠着。如实报数并给下一步 —— 旧实现只说「已被用户取消」，
        # 用户以为同步已经完成、数据本该如此（2026-09-27 报障的直接来源）。
        pending_detail = _pending_hydration_count(db, account)
        cancel_note = (
            f"正文与图集待补 {pending_detail} 条，再次同步可继续补"
            if pending_detail
            else ""
        )
        _finish(
            task,
            status=sts.STATUS_CANCELLED,
            stored=stored,
            total=progress_total,
            goods_stored=goods_stored,
            # ⚠️ **不传**「已被用户取消」：`_finish`（:3210）自己会拼
            # `note = f"{note}；已被用户取消" if note else "已被用户取消"` ——
            # 这里再传一遍就会拼成「已被用户取消；已被用户取消」（2026-09-27
            # 真机数据 `task=11` 的 payload 实读发现）。传**业务说明**才是这个
            # 参数的本意（商品侧传「未翻完，保留既有行」⇒ 拼出
            # 「未翻完，保留既有行；已被用户取消」，那是对的）。
            note=cancel_note,
            sources=source_results,
        )
        db.commit()
        return {
            "stored": stored,
            "total": total,
            "goods_stored": goods_stored,
            "content_filled": content_filled,
            "detail_failed": detail_failed,
            "cancelled": True,
        }

    if not source_results:
        # 一个可用源都没有 ⇒ 「均不可用」（§19.3.1）。按账号类型说清缺什么。
        raise SyncFailure("没有可用的笔记同步令牌。", code="NOTE_SOURCES_UNAVAILABLE")
    if all(entry["error"] for entry in source_results):
        # 每个源都失败了（能进 source_results 的错误都是首页失败 ⇒ 一行都没落）。
        detail = "；".join(
            f"{entry['source']}：{entry['error']}" for entry in source_results
        )
        raise SyncFailure(detail, code="NOTE_SOURCES_UNAVAILABLE")

    # 正文 / 图集 / 视频流地址的补齐分两段（设计 13 §19.14 + §35.18）：
    #
    # ① **源内页循环**已就地补过**本轮枚举到**的行（§19.14，翻页内联，拿全再落库）；
    # ② 这里再对该账号**全部历史欠账**收口一次（§35.18，2026-09-27 用户口径：
    #    「点击同步笔记时，应获取到当前笔记的所有信息后再落库，在一个任务中完成，
    #    不需要再点击补齐详情」）。第 ② 段是**必需**的而不是锦上添花：增量同步
    #    （`mode=auto` 有游标时）第 1 页就早停 ⇒ 候选集为 0 ⇒ 只靠 ① 的话历史欠账
    #    永远补不到（真实库账号 8：1867 条 `hydrated_at IS NULL`）。
    #
    # ⚠️ 代价如实记账：额度按账号读运行时配置（默认 60 次/分钟）⇒ 上千篇欠账按
    # 额度换算（60 ⇒ ≈ 半小时量级；调回 5 ⇒ 几小时）。见设计 13 §38.1/§38.2。
    # 这条路径**可中断（逐篇检查点）、可续跑（失败不写 `hydrated_at`）**，
    # 不要求用户一次等完；进度写进任务行 `payload.progress.detail_done/detail_total`
    # （§35.17.3 的进度泵据此更新「任务日志」的进度条）。
    if www_adapter is None:
        if stored:
            warnings.append("该账号没有网页版（www）会话，本轮同步的笔记未补正文与图集")
    else:
        # ⚠️ **不再刷新详情令牌**（2026-09-28 用户口径：「移除兜底，不允许出现翻页
        # 到底；没有令牌强行拦截，让用户补齐登录令牌」）。
        #
        # 沿革：§19.13.5「先刷令牌再补齐」（每篇翻 `user_posted` 66–200 页）→
        # §19.13.8「只在完全没令牌时兜底且命中即停」→ **本轮整段删除**。详情走
        # explore SSR 后本就不挑令牌来源，有令牌的篇不需要刷新；没令牌的篇改为
        # **如实报数、让用户补齐**，而不是拿一次全表扫描去赌它恰好被翻到。
        missing_token_count = _pending_hydration_without_token_count(db, account)
        if missing_token_count:
            warnings.append(
                f"{missing_token_count} 条笔记缺详情令牌（xsec_token），未补正文；"
                "请在网页版（xiaohongshu.com）登录该账号后重新同步"
            )
        sweep = _hydrate_account_pending(
            db,
            account,
            cookies=www_cookies,
            adapter=www_adapter,
            limiter=limiter,
            task_id=task.id,
            exclude_pks=attempted_note_pks,
            on_progress=lambda done, todo: _report_detail_progress(
                db,
                task,
                stored=stored,
                total=progress_total,
                goods_stored=goods_stored,
                # 2026-09-28：进度条口径**跨阶段连续** —— 账号级补齐从列表阶段
                # 停下的地方接着数（`processed_total + done`），分母沿用同一个
                # `work_total`（它已经含启动时的欠账数）⇒ 进度条从 ~X% 平滑走到 100%，
                # 不会在阶段切换时归零重来（那正是用户禁止的「反复跳」）。
                processed=processed_total + int(done),
                work_total=work_total_max or None,
                # `detail_*` 只服务前端文案「详情补齐 d/t」，不参与百分比（见 progress_percent）。
                detail_done=done,
                detail_total=todo,
            ),
        )
        content_filled += sweep["filled"]
        detail_failed += sweep["failed"]
        no_token += sweep.get("no_token", 0)
        warnings.extend(sweep["warnings"])
    if detail_failed:
        warnings.append(
            f"{detail_failed} 条笔记详情获取失败（列表数据已落库，下次同步重试）"
        )
    if no_token:
        # §19.13.9（2026-09-28）：缺令牌**单独报**，且给的是可执行动作。
        # 不混进上面那条「获取失败」—— 那条说「下次重试」，这条说「去补登录」，
        # 用户按错的那句做只会白等（DEF-058 口径）。
        warnings.append(
            f"{no_token} 条笔记缺详情令牌（xsec_token），未补正文；"
            "请在网页版（xiaohongshu.com）登录该账号后重新同步"
        )
    remaining = _pending_hydration_count(db, account)
    if remaining:
        # 走到这里还有欠账 ⇒ 只可能是「被限流 / 被取消 / 没有 www 会话」三种
        # （正常跑完的账号候选集会被清空）。如实报数，别让用户以为已经全补完了。
        warnings.append(f"正文待补齐 {remaining} 条（下次同步继续补）")

    # §27：对账融合 —— 用**本轮已枚举**的平台集合标记「本地有、平台无」的行。
    # 零额外平台请求（枚举结果同步链路已经有了）。判据前提（枚举完整 + 有全量
    # 枚举源）不满足 ⇒ 本轮不对账，任务行明说，不把「没对账」伪装成「对过了」。
    if full_list_complete:
        from backend.app.services import reconcile_service

        outcome = reconcile_service.mark_notes_missing(
            db,
            account,
            platform_ids,
            time_from_ms=scope_from_ms,
            time_to_ms=scope_to_ms,
        )
        # 有话可说才写任务行（干净同步不产噪声）：有标记 / 有自愈 / 有时间未知的行。
        if outcome["marked"] or outcome["healed"] or outcome["unknown_time"]:
            line = f"对账：标记三方不存在 {outcome['marked']} 条"
            if outcome["healed"]:
                line += f"，自愈 {outcome['healed']} 条"
            if outcome["unknown_time"]:
                line += f"；{outcome['unknown_time']} 条取不到发布时间未纳入对账"
            warnings.append(line)
    else:
        warnings.append("对账未执行：平台列表未完整枚举（限流/兜底/失败），不下「平台没有」的结论")

    note = "；".join(warnings)
    # 「部分完成」也是 done：任务行有数据、原因写 note（设计 13 §9）。
    # failed 只留给「一行都没落」的情况，那由上面的多原因分支处理。
    _finish(
        task,
        status=sts.STATUS_DONE,
        stored=stored,
        total=progress_total,
        goods_stored=goods_stored,
        note=note,
        sources=source_results,
    )
    if cursor_advanced:
        # D13：游标只在「本次同步落库成功」之后推进。落库失败时游标保持不动，
        # 下一轮从头重取，不会漏数据（DEF-043 的守卫）。
        #
        # ⚠️ **全量同步也必须推进游标**（§22，2026-09-22）：全量扫过了整份列表，
        # 本来就观测到了最新的 `publish_time`，不写游标 = 游标恒为 0 = 自动模式
        # 永远选全量 = 「增量化」这件事永远不生效。旧实现写成
        # `if incremental and cursor_advanced`，正是这个缺口。
        sts.save_note_cursor(db, account.id, last_cursor_ms, cursor_note_id)
    if stopped_by_limit:
        logger.info("note sync stopped by rate limit (account=%s)", account.id)
    db.commit()
    return {
        "stored": stored,
        "total": total,
        "goods_stored": goods_stored,
        "content_filled": content_filled,
        "note": note,
        "sources": source_results,
    }


def _update_progress(
    db: Session,
    task: Task,
    *,
    stored: int,
    total: Optional[int],
    goods_stored: int,
    detail_done: Optional[int] = None,
    detail_total: Optional[int] = None,
    processed: Optional[int] = None,
    work_total: Optional[int] = None,
) -> None:
    """写任务行的进行中进度（**唯一实现**：列表落库 + 详情补齐两个阶段共用）。

    **刷盘点**（设计 13 §42.3，2026-09-28 用户口径「每执行 50 条应更新一次任务执行详情
    数据」）：本函数在列表阶段每页调用**两次** —— ① 本页落库之后、页内详情补齐之前
    （`stored` 刚确定就刷，去掉分钟级滞后）；② 页尾（补 `goods_stored`）。
    页内补齐阶段则由 `on_progress` 回调**逐篇**更新（借 `hydrate_note_detail` 自己的
    commit 刷盘，≈7.5 秒一格），页粒度 = `sts.PROGRESS_FLUSH_EVERY`（默认 50 条）。
    ⚠️ **没有时间上界**（`PROGRESS_FLUSH_SECONDS` 已删）：逐篇回调本来就比它更细。

    ``processed`` / ``work_total`` 是**进度条真正看的那一对**（2026-09-28 用户口径
    「进度条慢慢增加、不许 0→100 反复跳」）：`processed` = 本任务**累计已处理条数**
    （列表落库 + 详情补齐都算「处理」，每篇一次），`work_total` = 本任务**预计总工作量**
    （首个页边界算一次，中途不缩）。分子只增、分母固定 ⇒ 比值单调，
    这是 `sts.progress_percent` 的**首选**口径（见那里的三条优先级）。

    ``detail_done`` / ``detail_total`` 是**第二阶段**（账号级详情补齐，设计 13 §35.18）
    的分子分母：只在真的进入补齐阶段后才写（``detail_total is None`` ⇒ 不写这个键，
    与「缺值不造 0」同口径）。⚠️ 页内补齐**不再**写它（那是 0→100 反复跳的根源），
    它现在只服务前端文案「详情补齐 d/t」。
    """
    payload = sts.task_payload(task)
    # ⚠️ §35.18.6：`total` 缺失（有界同步早停 / 首页还没回来）⇒ **不写这个键**，
    # 而不是写 `null` —— 前端据此区分「比例未知」与「比例就是 0」（缺值口径）。
    progress: dict[str, Any] = {"stored": stored}
    if total is not None:
        progress["total"] = total
    if processed is not None:
        progress["processed"] = processed
    if work_total is not None:
        progress["work_total"] = work_total
    if detail_total is not None:
        progress["detail_done"] = detail_done
        progress["detail_total"] = detail_total
    payload["progress"] = progress
    payload["goods_stored"] = goods_stored
    task.payload = payload
    if total:
        task.progress = min(100, int(stored * 100 / total))
    task.status = sts.STATUS_RUNNING
    if task.started_at is None:
        task.started_at = shanghai_now()


def _report_detail_progress(
    db: Session,
    task: Task,
    *,
    stored: int,
    total: Optional[int],
    goods_stored: int,
    processed: Optional[int] = None,
    work_total: Optional[int] = None,
    detail_done: Optional[int] = None,
    detail_total: Optional[int] = None,
) -> None:
    """把**补齐阶段**的进度写进任务行（页内补齐 + 账号级补齐共用，设计 13 §35.18 / §42.4）。

    **为什么不做额外 commit**：``_hydrate_note_rows`` 每篇都会经
    ``hydrate_note_detail`` 落一次库（它自己 commit），本函数写下的 payload 变更
    会被**下一篇那次 commit 顺带刷盘** —— 进度最多滞后一篇（额度 8 次/分钟下 ≈ 7.5 秒），
    换来的是不把写库次数翻倍。整段跑完的终态由 ``_finish`` 统一写。

    ``processed`` / ``work_total`` = 进度条看的那一对（单调口径，见 `_update_progress`）；
    ``detail_done`` / ``detail_total`` = **仅账号级阶段**的分子分母（前端文案「详情补齐 d/t」用）。
    ⚠️ **页内补齐不传 `detail_*`** —— 它的分母只有本页待补数，写进去就是「一页内 0→100
    反复跳」（2026-09-28 用户口径明确禁止）。

    ⚠️ 观测纪律：任何异常都不许影响执行（同 `_ProgressPump`）。
    """
    try:
        _update_progress(
            db,
            task,
            stored=stored,
            total=total,
            goods_stored=goods_stored,
            processed=processed,
            work_total=work_total,
            detail_done=detail_done,
            detail_total=detail_total,
        )
    except Exception:  # noqa: BLE001 - 观测不得影响执行
        logger.debug("note sync: detail progress write failed", exc_info=True)


def _finish(
    task: Task,
    *,
    status: str,
    stored: int,
    total: Optional[int],
    goods_stored: int,
    note: str = "",
    error: str = "",
    sources: Optional[list[dict[str, Any]]] = None,
) -> None:
    # ⚠️ 取消竞态（设计 13 §24.8）：用户在本轮收尾前取消 ⇒ **不覆写 cancelled 终态**，
    # 只补 finished_at 与说明行。否则跑完的线程会把「已取消」悄悄改回 done/failed
    # （旧实现正是如此：DB 显示 done，用户以为取消没用）。
    if task_cancel.is_cancelled(task.id):
        status = sts.STATUS_CANCELLED
        note = f"{note}；已被用户取消" if note else "已被用户取消"
    task.status = status
    task.finished_at = shanghai_now()
    payload = sts.task_payload(task)
    # ⚠️ **合并而不是覆盖**（2026-09-28 修）：旧实现直接 `payload["progress"] = {stored, total}`
    # ⇒ 终态那一刻把 `processed` / `work_total` / `detail_*` 全抹掉，而观测线程正是拿
    # `sts.progress_percent(payload["progress"])` 算**日志终态百分比**的 ⇒ 终态会退回
    # 旧口径（`stored/total`）算出一个**更低**的数 ⇒ 进度条在收尾时**倒退**（用户口径
    # 明确禁止）。只覆盖本函数拥有的两个键，其余原样保留。
    # 同 `_update_progress`：`total` 缺失（有界同步）⇒ 不写这个键（§35.18.6）。
    progress: dict[str, Any] = dict(payload.get("progress") or {})
    progress["stored"] = stored
    if total is not None:
        progress["total"] = total
    else:
        progress.pop("total", None)
    payload["progress"] = progress
    payload["goods_stored"] = goods_stored
    payload["note"] = note
    payload["error"] = error
    # 分源明细（§19.5）：每个源 {source, stored, error}；终态才写，进行中不写
    # （避免半程快照被前端读成最终结论）。
    if sources is not None:
        payload["sources"] = sources
    task.payload = payload
    task_cancel.clear(task.id)


def _mark_failed(db: Session, task: Optional[Task], reason: str) -> None:
    if task is None:
        return
    # 取消竞态同 `_finish`：已取消的任务不覆写为 failed（§24.8）。
    if task_cancel.is_cancelled(task.id):
        task.status = sts.STATUS_CANCELLED
        task.finished_at = shanghai_now()
        payload = sts.task_payload(task)
        payload["error"] = payload.get("error", "")
        payload["note"] = payload.get("note", "") or "已被用户取消"
        task.payload = payload
        task_cancel.clear(task.id)
        try:
            db.commit()
        except Exception:  # noqa: BLE001 - 标终态本身不能再抛
            logger.warning("note sync: could not persist cancelled state")
        return
    try:
        task.status = sts.STATUS_FAILED
        task.finished_at = shanghai_now()
        payload = sts.task_payload(task)
        payload["error"] = reason
        task.payload = payload
        db.commit()
    except Exception:  # noqa: BLE001 - 标失败本身不能再抛
        logger.warning("note sync: could not mark task failed")


def run_note_sync(
    account_id: int,
    *,
    task_id: Optional[int] = None,
    user_id: Optional[int] = None,
    max_pages: Optional[int] = sts.DEFAULT_MAX_PAGES,
    page_size: int = sts.DEFAULT_PAGE_SIZE,
    source: str = "manual",
    profile_url: str = "",
    xsec_token: str = "",
    mode: str = sts.SYNC_MODE_AUTO,
    time_from_ms: int = 0,
    time_to_ms: int = 0,
) -> dict[str, Any]:
    """执行一次笔记同步（供后台线程 / 调度器调用）。**不向上抛异常。**

    ``mode`` 默认 ``auto``（§22）：账号没有同步历史 ⇒ 全量；有 ⇒ 增量。
    返回 ``{task_id, stored, total, goods_stored, note, error, status}``。
    """
    db = SessionLocal()
    task: Optional[Task] = None
    try:
        account = db.get(PlatformAccount, account_id)
        if account is None:
            return {"task_id": task_id, "stored": 0, "total": None, "goods_stored": 0,
                    "note": "", "error": "账号不存在", "status": sts.STATUS_FAILED}

        if task_id is not None:
            task = db.get(Task, task_id)
        if task is None:
            task = sts.create_task(
                db,
                user_id=user_id or account.user_id,
                task_type=sts.NOTE_SYNC_TASK_TYPE,
                account_id=account_id,
                source=source,
                max_pages=max_pages,
                page_size=page_size,
                status=sts.STATUS_RUNNING,
                extra={"sub_type": account.sub_type},
            )
            db.commit()
            db.refresh(task)

        # 取消竞态（设计 13 §24.8）：取消端点可能在线程启动前就把行置成 cancelled
        # （建行 → 起点 → 用户点取消 → 线程才跑）。此时**不翻转为 running**，
        # 直接终态返回 —— 否则「点了取消还是跑起来了」。
        if task.status == sts.STATUS_CANCELLED:
            if task.finished_at is None:
                task.finished_at = shanghai_now()
                db.commit()
            return {"task_id": task.id, "stored": 0, "total": None, "goods_stored": 0,
                    "note": "已被用户取消", "error": "", "sources": [],
                    "status": sts.STATUS_CANCELLED}
        task_cancel.register(task.id)

        task.status = sts.STATUS_RUNNING
        if task.started_at is None:
            task.started_at = shanghai_now()
        # ⚠️ 重跑时**清掉上一轮的结束时间**（与 `note_queue_service.recover_interrupted`
        # 的脏值修复同因）：`running` + 已有 `finished_at` 是**自相矛盾**的行 ——
        # 「这个任务结束了吗」会出现两种答案（账号卡按钮读 `status`、任务中心读
        # 时间戳），用户看到的就是「任务已失败，卡片还显示同步中」。
        task.finished_at = None
        db.commit()

        # §19.3：按源清单同步（www / creator / ark 各自独立、互不阻塞）。
        # 源清单是**零请求**的 L2 口径；一个源都没有 ⇒ 「均不可用」的结构化失败
        # （用户口径：提示登录 pc 和千帆后再次触发）。
        sources = _note_sources(db, account)
        if not sources:
            # 源清单为空 = **只有一种可能：创作者平台会话缺失**（§21.4 起 www 源
            # 已不参与，ark 不足以单独成行）。给**结构化**原因，让用户知道该补什么。
            reason = (
                "CREATOR_SESSION_REQUIRED: 缺少创作者平台登录会话，请补齐后再试。"
            )
            _finish(task, status=sts.STATUS_FAILED, stored=0, total=None, goods_stored=0,
                    error=reason, note=reason)
            db.commit()
            return {"task_id": task.id, "stored": 0, "total": None, "goods_stored": 0,
                    "note": reason, "error": reason,
                    "status": sts.STATUS_FAILED}

        result = _sync_with_task(
            db,
            task,
            account,
            sources,
            max_pages=max_pages,
            page_size=page_size,
            profile_url=profile_url,
            xsec_token=xsec_token,
            mode=mode,
            time_from_ms=time_from_ms,
            time_to_ms=time_to_ms,
        )
        return {"task_id": task.id, "status": task.status, "error": "", **result}
    except SyncFailure as exc:
        reason = f"{exc.code}: {exc.message}" if exc.code else exc.message
        _mark_failed(db, task, reason)
        return {"task_id": task.id if task else task_id, "stored": 0, "total": None,
                "goods_stored": 0, "note": "", "error": reason, "sources": [],
                "status": sts.STATUS_FAILED}
    except Exception as exc:  # noqa: BLE001 - 调度器/线程调用，必须吞异常
        logger.error("run_note_sync failed (account=%s): %s", account_id, exc)
        _mark_failed(db, task, str(exc))
        return {"task_id": task.id if task else task_id, "stored": 0, "total": None,
                "goods_stored": 0, "note": "", "error": str(exc), "sources": [],
                "status": sts.STATUS_FAILED}
    finally:
        db.close()


def start_note_sync(
    account_id: int,
    *,
    user_id: int,
    max_pages: Optional[int] = sts.DEFAULT_MAX_PAGES,
    page_size: int = sts.DEFAULT_PAGE_SIZE,
    source: str = "manual",
    profile_url: str = "",
    xsec_token: str = "",
    mode: str = sts.SYNC_MODE_AUTO,
    time_from_ms: int = 0,
    time_to_ms: int = 0,
) -> dict[str, Any]:
    """建任务行 + 唤醒账号任务 worker，**立即返回**任务摄要（HTTP 请求不阻塞）。

    §35.16（2026-09-27 用户口径「同步商品的任务日志没有新增」）：执行**不再起独立线程**
    （原 `note-sync-{account_id}`），改由 `account_task_service` 的账号任务 worker 认领 ——
    串行（不并发打平台）、进程退出后可续跑（行是 `pending`，重启自动接上）。
    防重口径与响应形状**逐字不变**（调用方零感知）。

    防重（需求 N3）：该账号已有未完成的笔记同步 → 返回**已有任务**
    （``reused=True``），不新建、不打平台。

    ``profile_url`` / ``xsec_token`` 是 **www 源**的 URL 级令牌，**不写进任务 payload**：
    任务行会被前端读到，没有理由留第二份。§21.4 起 www 源**整体移除** ⇒ 两者已是
    死参数（§35.16.3 实查无读取方），worker 从行重建时传空串**语义等价**；
    将来若恢复 www 源，必须同时把「令牌落库 **或** 放弃续跑」想清楚。
    ``mode`` 默认 ``auto``（§22）；``time_from_ms`` / ``time_to_ms`` 是用户在
    弹窗里选的时间区间（§23），``(0, 0)`` = 不设界 —— **三者都必须写进任务行**
    （§35.16：执行体从行重建参数，不落库就丢）。
    """
    db = SessionLocal()
    try:
        existing = sts.find_active_task(db, sts.NOTE_SYNC_TASK_TYPE, account_id)
        if existing is not None:
            return {"reused": True, **sts.task_summary(existing)}
        task = sts.create_task(
            db,
            user_id=user_id,
            task_type=sts.NOTE_SYNC_TASK_TYPE,
            account_id=account_id,
            source=source,
            max_pages=max_pages,
            page_size=page_size,
            # §35.16：执行改由账号任务 worker 从**任务行**重建参数 ⇒ 同步语义必须落库
            # （原线程版靠 args 传，行里没有）。漏写 = 用户选的「全量 / 时间区间」静默丢失。
            extra={"mode": mode, "time_from_ms": time_from_ms, "time_to_ms": time_to_ms},
        )
        db.commit()
        db.refresh(task)
        summary = sts.task_summary(task)
        task_id = task.id
        # 建行即登记取消事件（§24.8）：取消端点可能在线程启动前就到，
        # 先登记 ⇒ 信号不会落空；线程内的 status 检查是第二道保险。
        task_cancel.register(task_id)
    finally:
        db.close()

    # §35.16：不再起独立线程 —— 交给账号任务 worker 认领执行（串行 + 可续跑 + 可见）。
    # 执行参数已随任务行落库（见上面的 `extra`），worker 从行重建。
    from backend.app.services import account_task_service

    account_task_service.ensure_account_task_worker()
    return {"reused": False, **summary}
