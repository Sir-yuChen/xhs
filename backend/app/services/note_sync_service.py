"""账号笔记同步：把平台的笔记拉进本地 ``notes`` 表（需求 R1 / R3 / §19）。

## 为什么需要它

一期起「我的笔记」就是**纯实时、完全不落库**（设计 12 §17）。这次要的是本地
沉淀：离线可查、可检索、供后续生成与统计取用。两条链路并存，不互相替代。

## 单源同步（§44，2026-09-30 用户拍板：拆成两个任务/入口）

**一次任务只跑一个源**，由 `_note_sources(db, account, *, only=…)` 的 `only` 决定：

| 任务 | task_type | `only` | 硬门禁 | 源 |
|---|---|---|---|---|
| 同步**普通笔记** | `xhs_note_sync` | `"creator"` | 创作者会话 | `creator` |
| 同步**商品笔记** | `xhs_goods_note_sync` | `"ark"` | 千帆 / ark 令牌 | `ark` |

~~「creator 跑完再追加 ark 的混合双源」~~ **已删除**（DEF-044：同一语义不留两份）：
两件事的前置条件、耗时、失败原因都不同，混在一个任务里用户只能得到「哪些源跑成了」
这种混合答案。各源自己的链路见下表（`pc` 分支保留但当前**无调用方**，§21.4 已移除 www 源）。

| 源 | 令牌 | 链路 |
|---|---|---|
| ``www`` | 账号 www 通道（`web_session`+`id_token`） | PC（``get_user_posted_page``）——**§21.4 起整体移除**，`_sync_source` 的 `pc` 分支保留备用 |
| ``creator`` | 账号 creator 通道 | 创作者平台（``get_posted_notes_page``）——**普通笔记任务**的源 |
| ``ark`` | 账号**自己的** ark 通道（`qianfan_cookies_for_account`，§20 起**无兜底、不借用**） | 千帆商品笔记（``get_goods_note_page``，§19.1.1 实测）——**商品笔记任务**的源 |

**`only="ark"` 时三条账号级副作用一律不写**（§44.2，它们是 creator 列表的位置/口径）：

1. **不推进** `note_sync_cursor_ms`：那是 creator 列表的增量位置，被 ark 推进会让
   普通笔记的增量同步**跳过**从未同步过的旧笔记；
2. **不写** `profile_json.notes_count`：ark 平台报的 `total` 是**商品笔记子集数**；
3. **不做**收尾的账号级详情补齐与对账（`mark_notes_missing` 的判据要求「能枚举全量
   笔记的源完整翻完」，ark 只看得到子集 ⇒ 结论不成立，做了会把正常笔记误标「平台不存在」）。

**PC 链路缺 ``xsec_token`` 时明确失败**（``XSEC_TOKEN_REQUIRED``），不静默同步
空集 —— 「同步成功但 0 条」会把「令牌缺失」伪装成「账号没笔记」。

**落库合并语义**（§19.3.2）：creator 源整行覆盖（有正文）；ark 源**填空式合并**
（只补空字段、raw_json 补键不冲键、无资产才下图）—— 直接覆盖会用空正文抹掉已落库的正文。

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
* **时长代价**（§21.5）：1961 条 creator ≈ 66 页 ≈ 13 分钟；千帆商品笔记 ≈ 40 页 ≈ 8 分钟。
  §44 拆分后**每个任务只跑一个源**（原来的「三源串行、单任务 30–45 分钟」已是历史口径），
  两个任务由**各自的 worker 并行认领**（任务类型级并行，§42）。

## Creator 增强（需求 R3）

Creator 账号每一页笔记落库后，把**本页 note_id** 批量交给
``shop_goods_service.sync_note_ids``（一次只读请求，≤50 个 id）落挂载商品 →
笔记与商品一个任务里一起完成，不拆成两个任务。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.time import shanghai_now
from backend.app.models.platform_account import PlatformAccount
from backend.app.services import sync_task_service as sts
from backend.app.services.account_service import (
    CHANNEL_CREATOR,
    CHANNEL_WWW,
    has_ark_token,
    has_creator_token,
)
from backend.app.services.note_topic_service import apply_sync_topics
from backend.app.services import task_cancel

logger = logging.getLogger(__name__)

#: 账号级并发度的默认值与上限（`sync_parallelism()` 用）。
#: ⚠️ 设计 75 S5：这组常量原来跟着 worker 池一起定义在被删的段落里，
#: 而 `sync_parallelism()` **仍在用**（登录态巡检读它决定探活并发）⇒ 提到这里。
SYNC_DEFAULT_PARALLELISM = 3
SYNC_MAX_PARALLELISM = 8

#: 页与页之间的停顿**基础值**（秒）。测试把它置 0 以避免真等待。
#: 2026-09-25 用户口径「后端同步时注意不要触发平台的限流，可以拉长间隔时间，
#: 随机化间隔时间」⇒ 3.0 → **5.0**；实际停顿还要乘抖动系数（见 `sync_pacing`），
#: 落在 [0.8, 1.6] × 基础值之间且不超过 `MAX_PAGE_GAP_SECONDS`。
PAGE_GAP_SECONDS = 5.0

#: 单页停顿的硬顶（设计 13 §8）。time.sleep 永远不会超过这个值。
MAX_PAGE_GAP_SECONDS = 60.0
SOURCE_CREATOR = "creator"
SOURCE_ARK = "ark"

#: 源取值与「源 → 任务类型」映射（住在 `sync_task_service`，§44）**必须一一对应**：
#: 任何一边漂移（改名 / 加源）都在 import 期报错，而不是运行时静默把商品笔记
#: 写进普通笔记的任务类型里。
assert set(sts.SOURCE_TASK_TYPES) == {SOURCE_CREATOR, SOURCE_ARK}, (
    "sync_task_service.SOURCE_TASK_TYPES 与 note_sync_service 的源常量不一致："
    "新增同步源要两个模块一起改"
)


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


def _note_sources(
    db: Session, account: PlatformAccount, *, only: str = SOURCE_CREATOR
) -> list[dict[str, Any]]:
    """本次同步的笔记源清单（**单源**，§44.2 / §19.3.1 / §21.4）。

    `only` 是**唯一的两把尺子**（缺省 `creator`）：

    * ``"creator"`` ⇒ **创作者平台会话硬门禁**（无会话 ⇒ 返回 `[]`，上游给
      `CREATOR_SESSION_REQUIRED`），返回 ``[creator]``；
    * ``"ark"`` ⇒ **千帆 ark 令牌硬门禁**（`has_ark_token` 不成立 ⇒ 返回 `[]`，
      上游给 `ARK_SESSION_REQUIRED`），返回 ``[ark]``；
    * 其它取值 ⇒ **抛 `ValueError`**：源清单是路由开关，未知取值静默退化成某一种
      会让「商品笔记同步」变成跑普通笔记（`account_types.normalize_sub_type` 同款纪律）。

    ⚠️ **混合路径已删**（§44，2026-09-30 用户拍板「删旧混合路径」）：~~creator 跑完
    再追加 ark 的填空合并~~不再存在 —— 一次任务只跑一个源，两条链路各自独立、各自
    硬门禁、互不阻塞（DEF-044：同一语义不留两份）。

    可用性判据是**零请求**的 L2 口径（jar 形态），与登录态服务同一把尺子；
    能不能真取到由翻页时的第一次请求回答（DEF-057：不预判）。

    ⚠️ `_sync_source` 的 pc 分支**保留代码但当前无调用方** —— www 源已整体移除
    （§21.4）；要恢复 www 源时在这里加回三行即可（不要改 `_sync_source`）。
    """
    if only == SOURCE_CREATOR:
        # **强制前置条件：必须有创作者平台登录会话**（2026-09-22 用户口径原文：
        # 「同步笔记按钮该账号必须存在有效的创作者登录会话，不存在有效创作者平台
        # 登录会话的提示补齐后再触发」）。
        #
        # www 源**整体不再参与**（同日用户口径：「www 源在这里是冗余的，则移除不要，
        # 使用 creator 源」）：creator 源读的是同一批笔记且**更全**（含仅自己可见的），
        # 而 www 源额外要一个 URL 级 `xsec_token` —— 那不是登录令牌，是网页版主页 URL
        # 上的查询参数，账号卡片触发时拿不到 ⇒ 每轮白打一次平台请求 + 结果行里一条
        # 看起来像故障的 `XSEC_TOKEN_REQUIRED`。
        creator_cookies = _account_cookies(db, account.id, channel=CHANNEL_CREATOR)
        if not has_creator_token(creator_cookies):
            # 返回空 ⇒ 上层给**结构化**原因（`CREATOR_SESSION_REQUIRED`）。
            # **刻意不退化成「用 ark 凑合」**：用户要的是「补齐后再触发」，
            # 而不是「哪个源能用就用哪个」—— 后者会让「没创作者会话」表现为
            # 「同步成功但只同步到商品笔记」。
            return []
        return [{"source": SOURCE_CREATOR, "kind": "creator", "cookies": creator_cookies}]

    if only == SOURCE_ARK:
        # ark：**只认账号自己的 ark 通道**（与 `_probe_ark` 同一口径）。2026-09-22 起
        # 「绑定表兜底」整条移除 —— 没有独立 ark 令牌的账号就是没有千帆源。
        # §44：商品笔记任务里它是**唯一硬门禁**（原来只是混合任务的可选附加）。
        from backend.app.services.shop_binding_service import qianfan_cookies_for_account

        ark_cookies = qianfan_cookies_for_account(db, account.id)
        if not has_ark_token(ark_cookies):
            return []
        # `ark_pc`：**ark 条目自带 explore 地址 + xsec_token**（2026-10-12 抓包实测：
        # `goods-note/list` 的 `note_infos[].xsec_token`）⇒ 详情补齐不必借 www 会话，
        # 用这条源的凭证自己走 explore SSR（`hydrate_note_detail` 的取数路由）。
        # 值为 `True` 而不是适配器对象：适配器在 `_sync_source` 里按 kind 构造。
        return [
            {
                "source": SOURCE_ARK,
                "kind": "ark",
                "cookies": ark_cookies,
                "detail_with_own_token": True,
            }
        ]

    raise ValueError(
        f"未知的同步源 only={only!r}；已知取值：{SOURCE_CREATOR!r}（普通笔记）、"
        f"{SOURCE_ARK!r}（商品笔记）。"
    )


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


#: ⚠️ **已退场**（设计 75 S5，2026-10-27）：`_sync_goods_for_notes` / `_link_ark_note_goods`
#: 两个函数整段删除 —— 它们把千帆挂链映射写进 `shop_note_goods` 并把关系写进
#: `note_goods`，两张表都已随迁移 `n8o9p0q1r2s3` 整表 DROP（挂链改实时查询）。
#: 笔记本身仍正常落库；「这篇挂什么商品」改由平台实时回答（`rt.list_goods_notes`）。


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


def backfill_note_detail_refs(
    db: Session, notes: list[Any], fetch: Any, *, force: bool = False
) -> int:
    """**批量**补详情地址：一次 `note_ids` 精确查询补全整批（§25.4，2026-09-25 提速）。

    为什么需要批量版：批量同步 N 篇本地生成笔记时，旧实现每篇各回查一次千帆
    ⇒ N 次请求（每次 1-2 秒）。而 `get_goods_note_page(note_ids=[...])` 本身
    支持一次查多个（与 `shop_goods_service.sync_note_ids` 同参数）⇒ 合并为
    **一次请求**。由 worker 在「本篇缺地址」时触发，把当时队列里同样缺地址的
    篇一起补上（一篇触发、全批受益）—— 单条按钮路径走单篇版，行为不变。

    只补**缺地址**的行（已有 `note_url` 的不碰）；返回补齐篇数（调用方据此
    决定要不要 commit）。hex 判据是 `_note_detail_url` 拼 explore 的关切；
    这里只要求有平台 id（真实平台恒为 hex，替身 / 历史行不保证）。

    ``force``（2026-10-12）：**连已有 `note_url` 的行也重取**。给「令牌过期刷新」
    用 —— 那种行的地址**在、但令牌已失效**，按「缺地址」过滤会整批跳过
    （`_refresh_note_detail_token` 的调用因此永远拿不到新令牌）。
    缺省 `False` 保持既有调用方的「只补缺」语义不变。
    """
    pending = []
    for note in notes:
        raw = note.raw_json if isinstance(note.raw_json, dict) else {}
        if not force and str(raw.get("note_url") or "").strip():
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


def _backfill_detail_ref_for_account(
    db: Session, account: PlatformAccount, note: Any, *, force: bool = False
) -> bool:
    """用**账号自己的 `ark` 令牌**回查商品笔记列表，补详情地址（§25.4 / §18.14）。

    千帆凭据只认本账号 `ark`（`qianfan_cookies_for_account`，无令牌返空串）——
    拿不到就静默跳过（调用方有 explore 兜底），**不借任何别的凭据**、不发探活。

    ``force``（2026-10-12）：连已有地址的行也重取（令牌过期刷新用，见
    ``_refresh_note_detail_token``）。
    """
    return _backfill_detail_ref_for_accounts(db, account, [note], force=force) > 0


def _refresh_note_detail_token(db: Session, account: PlatformAccount, note: Any) -> bool:
    """把这条笔记的 `xsec_token` **强制换成平台最新的一枚**（2026-10-12）。

    与 `_backfill_detail_ref_for_account` 是**同一件事**，差别只在**调用时机**：

    * 那个只在「本地完全没有令牌」时被调（`if not _note_detail_url(note)`）；
    * 本函数在「本地**有**令牌、但取数回 `DETAIL_EMPTY`」时被调 —— 那说明
      令牌**过期了**（平台对过期令牌回 `200 + noteDetailMap={} + errorCode=-510000`，
      不是 4xx，所以从响应本身分不出「没有正文」与「令牌失效」）。

    ⚠️ **令牌有时效**是这轮真机定论：抓包当天同一枚 token 能读到完整
    `noteDetailMap`，几小时后同一枚回空对象；而千帆列表**每次都发新鲜 token**。
    旧实现「有令牌就永不刷新」正是用户反复点同步都拿不到正文的直接原因。

    返回 True 表示 `raw_json` 被改写（**调用方负责提交**，本函数不 commit ——
    与 `_backfill_detail_ref_*` 同一纪律）。
    """
    raw = note.raw_json if isinstance(note.raw_json, dict) else {}
    before = str(raw.get("xsec_token") or "").strip()
    changed = _backfill_detail_ref_for_account(db, account, note, force=True)
    after_raw = note.raw_json if isinstance(note.raw_json, dict) else {}
    after = str(after_raw.get("xsec_token") or "").strip()
    if changed and after and after != before:
        db.commit()
        return True
    return False


def _backfill_detail_ref_for_accounts(
    db: Session, account: PlatformAccount, notes: list[Any], *, force: bool = False
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

    filled = backfill_note_detail_refs(db, notes, _fetch, force=force)
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
        # ⚠️ **不再一律拦下**（2026-10-12 真机抓包结论，见下）：先看这条笔记能不能
        # 用**它自带的** explore 地址 + `xsec_token` 取详情 —— 那条路只认令牌、
        # **不认 www 会话**（`get_note_info_html` 一次 GET 拿全，§19.13.8 已实测）。
        #
        # 判据从「账号有没有 www 会话」改成「**这条笔记带没带令牌**」：
        #
        # * 带 token（典型：千帆列表返回里就有 `xsec_token`）⇒ 照常打详情，
        #   **只登千帆的账号也能补齐商业笔记正文与图集** —— 这正是「两大业务线
        #   隔离」要的效果（ark 源自给自足，不向 www 借会话）；
        # * 不带 token 且没有 www 会话 ⇒ 仍给 `no_session`（一条请求都不发）。
        #
        # 旧实现按账号一刀切 ⇒ 只登千帆的账号每条都 `no_session`，正文永远补不到
        # （真实库账号 8 的报障原文：「该账号没有可用的网页版（www）会话，无法读取
        # 笔记详情」）—— 而令牌明明就在手上。
        if "xsec_token=" not in url:
            # 「暂不可用」不是「尝试过」：**不写** `hydrated_at`，用户补登 www 后仍会被补。
            result.update(
                status="no_session",
                reason="该账号没有可用的网页版（www）会话，无法读取笔记详情",
            )
            return result
        # 带令牌 ⇒ www 会话**不是必需**：匿名 / 千帆令牌都能读 SSR 详情。
        # `account_cookies` 为空串时 `_build_pc_adapter` 也建得出适配器（请求头少几个
        # cookie，SSR 页照样返回）—— 所以这里不提前返回，把判定权交给取数本身。
        account_cookies = account_cookies or ""

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


#: ⚠️ **已退场**（设计 75 S5，2026-10-27）：**账号笔记同步的整套执行引擎**整段删除
#: （`sync_note_from_platform` / 队列入队 `_enqueue_notes` / worker 循环与池管理
#: `_sync_worker_loop` / `_claim_one_*` / `_spawn_sync_worker` / `_reconcile_sync_worker_pool`
#: `/ `start_*` / `stop_sync_worker` / 看门狗 / 队列快照）。
#:
#: 用户口径（2026-10-26）：「同步商品和同步笔记任务**全链路移除**」+「笔记列表实时查
#: 线上」，两层一起删才能不留死链：端点已 410 墓碑（S4）、任务已从注册表移除，
#: 执行体再留着就是「没有入口但还在跑的代码」。
#:
#: **保留**的是与「同步」无关、别处仍在用的纯工具：`backfill_note_detail_ref`
#: （发布链路补正文）、`hydrate_note_detail`（数据中心补详情）、`_merge_note_images`、
#: `_needs_hydration`、`_note_detail_url`、`sync_parallelism`（登录态巡检读并行度）。


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


#: ⚠️ **已退场的同步引擎**（设计 75 S5，2026-10-27）—— 这一整段（原 `_enqueue_sync` /
#: `ensure_sync_worker` / `_enqueue_notes` / `start_batch_sync` / `start_sync_note` /
#: `_sync_worker_loop` / 认领与池管理 / `stop_sync_worker` / 看门狗 / `sync_queue_snapshot` /
#: `wait_sync_done`）**整段删除**：账号笔记同步的八个层（任务注册 / 调度 / 端点 / 按钮 /
#: 权限码 / 菜单 / 服务层 / 落库表）已按用户口径全部退场（端点 410 墓碑在 S4）。
#:
#: 保留的只有下面这些与「同步」无关的补详情工具（发布链路 / 数据中心仍在用）。


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
            Note.is_deleted.is_(False),
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
            Note.is_deleted.is_(False),
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

    **笔记分类**（2026-10-12 抓包实测）：条目自带 `note_flag` —— 平台原生判据：

    * ``"goods_note"`` ⇒ 商业笔记（挂链商品）；
    * 其它 / 缺省 ⇒ 普通笔记。

    这一个字段就是「商业笔记 vs 普通笔记」的**平台原生真相源** —— 不必另造判据，
    也不必反推（反推 = 造第二份真相源；同 `note_type` 的纪律）。

    ⚠️ 2026-10-12 设计 35：`note_category` 已退役。本函数**只保留 `note_flag`**
    （供落库到 `note_datacenter_stats` 统计表 + 同步限流分线），不再产出
    `note_category` 投影。
    """
    # 指标转换委托 pc.py 唯一实现（`_metric`，缺值 0 的计数口径）。
    from backend.app.api.platforms.xhs.pc import _metric

    note_id = str(entry.get("note_id") or "").strip()
    xsec_token = str(entry.get("xsec_token") or "").strip()
    image = str(entry.get("note_image") or "").strip()
    publish_time = entry.get("publish_time")
    #: `note_flag == "goods_note"` = 平台自报的商业笔记（2026-10-12 真机抓包）。
    note_flag = str(entry.get("note_flag") or "").strip()
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
        # 笔记分类（平台原生 `note_flag`）：2026-10-12 设计 35 起**不再投影成
        # `note_category`**，`note_flag` 本身供落库到统计表 + 限流分线。
        "note_flag": note_flag,
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

#: ⚠️ **已退场的同步执行体**（设计 75 S5，2026-10-27）：从这里到文件末尾的整段
#: （`_upsert_ark_stat` / `_merge_ark_notes` / `_sync_source` / `_sync_with_task` /
#: 进度回报 / `run_note_sync` / `start_note_sync` / `start_goods_note_sync`）全部删除 ——
#: 它们是「把平台笔记翻页写进本地 `notes`」的执行体，而用户口径是**笔记实时查线上**、
#: **同步任务全链路移除**（端点 410 已在 S4、任务注册已删）。
#:
#: 保留本文件是因为发布链路 / 数据中心仍要用补详情的四个纯工具（见文件头说明）。
