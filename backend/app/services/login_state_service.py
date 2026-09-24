"""账号维度的**登录态**（设计 13 §18.5；2026-09-21 拆分为三平台）。

## 为什么要有这一层

账号卡片此前只有 `status`（业务健康）与 `metrics_reason`（互动计数），**没有任何令牌
维度的展示** —— 于是「千帆登录态过期了」这件事只在用户点「同步商品」那一刻才暴露，
页面在那之前看起来一切正常（同 T-31 当年的静默失败）。

## 三个平台、三条令牌（2026-09-21 拆分，用户拍板）

| 平台 | 通道 | 判据（L2） | 探针（L3，只读） |
|---|---|---|---|
| 网页版 | `www` | `web_session` + `id_token` | `GET /v2/user/me` |
| 创作者平台 | `creator` | creator 域令牌 / customer 单点登录 | `GET /api/galaxy/user/info` |
| 千帆 | `ark` | ark 域令牌 / customer 单点登录 | `GET /api/edith/seller/info/v2` |

⚠️ **千帆与创作者不再是同一份令牌**（真机实测，观隅好物 acct 8）：创作者扫码的
jar 只带 `access-token-creator.*`，打千帆一律 `-13000 缺失登录凭证`；千帆那份是
**超集**（同时带两个平台的域令牌），打创作者平台正常。所以：

* 探针**按平台各问各的**（创作者问创作者平台，千帆问千帆）；
* 千帆的探针与取票只认**账号自己的 `ark` 通道**
  （`shop_binding_service.qianfan_cookies_for_account`）。2026-09-22 用户拍板
  「每个账号只使用自己账号对应的平台有效登录令牌」后，原来的
  `shop_account_bindings` 兜底整条移除 —— 所以「千帆 未导入」是**实话**：
  这个账号确实没登录过千帆，不是「令牌藏在别处」。

## 三层判据，**三层都要给**（不能只给一个绿灯）

| 层 | 判据 | 请求数 | 回答的问题 | 状态值 |
|---|---|---|---|---|
| **L1 存没存** | 该账号有没有这个平台的信封 | 0 | 「这个平台从没登录过」 | `missing` |
| **L2 是不是登录态** | www：`web_session`+`id_token`；creator/ark：平台域或单点登录键 | 0 | 「只拿到了游客态 / 空壳」 | `guest` |
| **L3 还能不能用** | 只读探针（见上表） | 1 | 「令牌过期了」 | `valid` / `expired` |

三层问的是**三个不同的问题**，压成一个布尔值必然说谎：
「存了但没探过」既不是「可用」也不是「不可用」。

## ⚠️ `unknown` 绝不能渲染成「已失效」

`unknown` = 「**未确认**」：存了、也是登录态，但**还没探过**，或者**探活本身没得到结论**
（网络异常 / 平台结构变了）。它是 DEF-057 的同型红线 —— 用「没检查」冒充「不可用」，
用户会去做一次**根本不需要的重登**。所以：

* 探针发生在**进入 / 刷新账号页时**（批量，`probe=True`，v1.30 起由用户要求；
  仍是「一次页面动作一次探针」，**不进轮询、不进定时任务**）。v1.31 起账号卡片的
  「检查」按钮已移除（用户口径），探活不再有第二个手工入口 —— 真探不出结论时
  刷新页面即重试；
* `成功=False` 且 **`payload is None`**（SDK 在传输异常时不抛异常、只把 `success` 置假）
  ⇒ 判 `unknown`，**不判 `expired`**。判据是**结构**（有没有拿到平台的 JSON），
  不是去匹配错误文案；
* 每一次结果都带上 `probed` 与 `reason`，让界面能说「上次检测未得到结论：<原因>」，
  而不是替平台下一个它没下过的结论。

## 只读边界

* 探针**只读**（三个 GET），**不落库**：L3 的结论只影响**这一次响应**里的展示 ——
  它**不得**删除 / 覆盖任何存量令牌，也**不得**动店铺关系（`shop_id_fk` / `shop_state`）（§18.9.3）；
  （原句为「也不得清空 `shop_account_bindings`」—— 该表已于 2026-09-22 整表移除。）
* **不进轮询、不进定时任务**（§18.9.5 风控）：一张卡片最多三次探活（www + creator + ark），
  且仅限页面动作触发。
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

from sqlalchemy.orm import Session

from backend.app.core.time import shanghai_now
from backend.app.models import PlatformAccount
from backend.app.services.account_service import (
    CHANNEL_ARK,
    CHANNEL_CREATOR,
    CHANNEL_WWW,
    CHANNELS,
    has_ark_token,
    has_creator_token,
    has_web_session,
    latest_cookies_for_channels,
)

logger = logging.getLogger(__name__)

#: 状态取值（**互斥且完整**，§18.5）。
STATE_MISSING = "missing"
STATE_GUEST = "guest"
STATE_VALID = "valid"
STATE_EXPIRED = "expired"
STATE_UNKNOWN = "unknown"
LOGIN_STATES: tuple[str, ...] = (
    STATE_MISSING,
    STATE_GUEST,
    STATE_VALID,
    STATE_EXPIRED,
    STATE_UNKNOWN,
)

#: 每个平台的展示名。**千帆与创作者分开**（2026-09-21 拆分）—— 合成一个名字
#: 会让用户以为一份令牌能同时打两边，正是本轮要消灭的误解。
#: ⚠️ 全名必须**严格长于**前端 tag 里的短名（`LOGIN_CHANNEL_SHORT_LABELS`）——
#: 相等就等于「tag 里塞的是全名」，昵称会被挤出卡片（`login_state.cjs` F2 钉住）。
CHANNEL_LABELS: dict[str, str] = {
    CHANNEL_WWW: "网页版（PC）",
    CHANNEL_CREATOR: "创作者平台",
    CHANNEL_ARK: "千帆商家后台",
}

#: 各状态的原因（**工具提示用全文**；短句由前端按状态映射，后端不重复给）。
_REASONS: dict[tuple[str, str], str] = {
    (CHANNEL_WWW, STATE_MISSING): (
        "该账号还没有网页版（www）令牌 —— 从未登录过，我赞过的 / 我收藏的 / 互动消息、"
        "笔记正文与评论都读不到。"
    ),
    (CHANNEL_WWW, STATE_GUEST): (
        "这条网页版令牌只拿到了游客态（有 web_session 但没有 id_token），读不到本人数据，"
        "需要重新登录网页版。"
    ),
    (CHANNEL_CREATOR, STATE_MISSING): (
        "该账号还没有创作者平台令牌 —— 从未在创作者平台登录过，"
        "创作者平台的笔记管理与发布用不了。"
    ),
    (CHANNEL_CREATOR, STATE_GUEST): (
        "这条令牌不含创作者平台的登录凭证（access-token-creator 等），"
        "需要重新登录创作者平台。"
    ),
    (CHANNEL_ARK, STATE_MISSING): (
        "该账号还没有千帆（ark）令牌 —— 商品列表、店铺商品同步、商品笔记反查都用不了。"
    ),
    (CHANNEL_ARK, STATE_GUEST): (
        "这条令牌不含千帆的登录凭证（access-token-ark 等），"
        "无法访问千帆商家后台，需要重新登录千帆。"
    ),
    (CHANNEL_WWW, STATE_VALID): "探活通过：这条网页版令牌现在可用。",
    (CHANNEL_CREATOR, STATE_VALID): "探活通过：这条创作者平台令牌现在可用。",
    (CHANNEL_ARK, STATE_VALID): "探活通过：千帆现在可用。",
    (CHANNEL_WWW, STATE_EXPIRED): (
        "令牌已失效，请在网页版（xiaohongshu.com）重新登录后扫码续期。"
    ),
    (CHANNEL_CREATOR, STATE_EXPIRED): (
        "令牌已失效，请在创作者平台（creator.xiaohongshu.com）重新登录后扫码续期。"
    ),
    (CHANNEL_ARK, STATE_EXPIRED): (
        "千帆登录态已失效，请在千帆商家后台（ark.xiaohongshu.com）重新登录后续期。"
    ),
    (CHANNEL_WWW, STATE_UNKNOWN): "已确认是登录态，但还没检测过它现在还能不能用（刷新页面会自动检测）。",
    (CHANNEL_CREATOR, STATE_UNKNOWN): "已确认是登录态，但还没检测过它现在还能不能用（刷新页面会自动检测）。",
    (CHANNEL_ARK, STATE_UNKNOWN): "已确认是登录态，但还没检测过它现在还能不能用（刷新页面会自动检测）。",
}


def empty_state() -> dict[str, Any]:
    """一个平台的「不存在」形态。

    `state` 与 `reason` 一起下发，前端**不需要**自己拼兜底文案（缺值不造假：
    没记录就是 `missing`，不是 `unknown`）。
    """
    return {"state": STATE_MISSING, "reason": "", "probed": False}


def _layer12_state(channel: str, cookies: str) -> str:
    """L1/L2（**零平台请求**）。过不了 L2 就是 `guest`，**过了也不是 `valid`**。

    ⚠️ 这里最容易写成 `return STATE_VALID if ok else STATE_GUEST` —— 那等于**替平台
    宣布「还能用」，而依据只是「jar 里有两把钥匙」**。L2 只回答「是不是登录态」；
    「还能不能用」只有 L3 的探针能答（§18.5）。所以 L2 过关的正确取值是
    `unknown`（**未确认**），随后由 `probe=True` 把它推进到 `valid` / `expired`。
    """
    if not cookies:
        return STATE_MISSING
    if channel == CHANNEL_WWW:
        return STATE_UNKNOWN if has_web_session(cookies) else STATE_GUEST
    if channel == CHANNEL_CREATOR:
        return STATE_UNKNOWN if has_creator_token(cookies) else STATE_GUEST
    return STATE_UNKNOWN if has_ark_token(cookies) else STATE_GUEST


def _probe_www(cookies: str, profile_adapter: Any) -> tuple[str, str]:
    """L3 探活网页版（`/v2/user/me`，只读）。返回 `(state, reason)`。

    **三种结局，不能压成两种**：

    * `success=True` 且 `data.nickname` 非空 → `valid`；
    * `success=True` 但没有昵称 → `guest`（真机实测：游客会话正是这个形状，
      `ok=True / nickname=None`，设计 12 §17.6.2 的三组对照）；
    * `success=False`：
      - **`payload is None`** ⇒ SDK 连平台的 JSON 都没拿到（网络/超时/解析失败）
        ⇒ **`unknown`**。**这是本函数最容易写错的地方**：把它判成 `expired`
        就是又一次「用『没问到』冒充『不可用』」；
      - 拿到了 JSON ⇒ 平台**答复并拒绝**（真机口径：`msg = 登录已过期`）⇒ `expired`。
    """
    try:
        success, message, payload = profile_adapter.probe_self_profile(cookies)
    except Exception as exc:  # noqa: BLE001 - 探活失败本身是业务结果，不是程序错误
        logger.warning("[login-state] www probe raised: %s", exc)
        return STATE_UNKNOWN, f"检测网页版令牌时出错，状态未确定：{exc}"

    if success:
        data = payload.get("data") if isinstance(payload, dict) else None
        nickname = str((data or {}).get("nickname") or "").strip() if isinstance(data, dict) else ""
        if nickname:
            return STATE_VALID, _REASONS[(CHANNEL_WWW, STATE_VALID)]
        return STATE_GUEST, _REASONS[(CHANNEL_WWW, STATE_GUEST)]
    if payload is None:
        return STATE_UNKNOWN, f"检测网页版令牌未得到结论（请求未完成）：{message}"
    return STATE_EXPIRED, f"{_REASONS[(CHANNEL_WWW, STATE_EXPIRED)]}（平台原话：{message}）"


def _probe_creator(cookies: str, adapter_factory: Any) -> tuple[str, str]:
    """L3 探活创作者平台（`GET /api/galaxy/user/info`，只读）。返回 `(state, reason)`。

    与 `_probe_www` 同构：三种结局（valid / 平台答复并拒绝 → expired /
    没拿到 JSON → unknown）。⚠️ 千帆那份**超集** jar 也能过这个探针（它带
    creator 域令牌）—— 那是平台事实，不是漏洞。
    """
    try:
        adapter = (adapter_factory or _default_creator_factory())(cookies)
        success, message, payload = adapter.get_creator_profile()
    except Exception as exc:  # noqa: BLE001 - 探活失败本身是业务结果
        logger.warning("[login-state] creator probe raised: %s", exc)
        return STATE_UNKNOWN, f"检测创作者平台令牌时出错，状态未确定：{exc}"

    if success:
        return STATE_VALID, _REASONS[(CHANNEL_CREATOR, STATE_VALID)]
    if payload is None:
        return STATE_UNKNOWN, f"检测创作者平台令牌未得到结论（请求未完成）：{message}"
    return STATE_EXPIRED, f"{_REASONS[(CHANNEL_CREATOR, STATE_EXPIRED)]}（平台原话：{message}）"


def _probe_ark(cookies: str, adapter_factory: Any) -> tuple[str, str]:
    """L3 探活千帆（`GET /api/edith/seller/info/v2`，只读）。返回 `(state, reason)`。

    SDK 已经把「凭据失效」（`code=-13000`）与「接口变了」分开（`CREDENTIAL_ERROR_CODE`），
    这里只在**明确是凭据问题**时才判 `expired`，其余失败一律 `unknown`。

    ⚠️ 2026-09-21 拆分后，**创作者平台的 jar 打这个探针只会得到 `-13000`**
    （它没有 ark 域令牌）—— 那是实话：那份令牌确实打不开千帆。所以取票必须
    按平台分清楚（`qianfan_cookies_for_account`），不能让千帆探针拿到创作者 jar。
    """
    from apis.xhs_qianfan_apis import XHS_Qianfan_Apis

    credential_error_code = int(getattr(XHS_Qianfan_Apis, "CREDENTIAL_ERROR_CODE", -13000))
    try:
        adapter = (adapter_factory or _default_qianfan_factory())(cookies)
        success, message, payload = adapter.get_seller_info()
    except Exception as exc:  # noqa: BLE001 - 探活失败本身是业务结果
        logger.warning("[login-state] ark probe raised: %s", exc)
        return STATE_UNKNOWN, f"检测千帆令牌时出错，状态未确定：{exc}"

    if success:
        return STATE_VALID, _REASONS[(CHANNEL_ARK, STATE_VALID)]
    code = payload.get("code") if isinstance(payload, dict) else None
    if code == credential_error_code:
        return STATE_EXPIRED, f"{_REASONS[(CHANNEL_ARK, STATE_EXPIRED)]}（平台原话：{message}）"
    if payload is None:
        return STATE_UNKNOWN, f"检测千帆令牌未得到结论（请求未完成）：{message}"
    return STATE_UNKNOWN, f"检测千帆令牌未得到结论（{message}）"


def _default_creator_factory():
    from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter

    return XhsCreatorApiAdapter


def _default_qianfan_factory():
    from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory

    return get_qianfan_api_adapter_factory()


def login_states_for_accounts(
    db: Session,
    accounts: Sequence[PlatformAccount],
    *,
    probe: bool = False,
    profile_adapter: Any = None,
    qianfan_adapter_factory: Any = None,
    creator_adapter_factory: Any = None,
) -> dict[int, dict[str, Any]]:
    """一批账号的登录态。`probe=False`（列表加载）**零平台请求**。

    返回 `{account_id: {"channels": {www: {...}, creator: {...}, ark: {...}}, "checked_at": iso|None}}`。
    `channels` 里三个平台**都在**（哪怕 `missing`）—— 缺项消失会让前端误以为
    「这个账号没有这个平台」和「后端没算」是两回事。
    """
    cookie_map = latest_cookies_for_channels(db, [account.id for account in accounts])
    checked_at = shanghai_now().isoformat() if probe else None

    result: dict[int, dict[str, Any]] = {}
    for account in accounts:
        channels: dict[str, dict[str, Any]] = {}
        for channel in CHANNELS:
            cookies = cookie_map.get((account.id, channel), "")
            state = _layer12_state(channel, cookies)
            entry = {
                "state": state,
                "reason": _REASONS.get((channel, state), ""),
                "probed": False,
                "label": CHANNEL_LABELS.get(channel, channel),
            }
            # L3：只在**显式**探活、且 L2 已经确认「是登录态」时打那一次只读请求。
            # `missing` / `guest` 探也没有意义 —— 没有可用令牌，探针只会告诉我们
            # 已知的事实，白花一次请求（风控：请求数越少越好）。
            if probe and state == STATE_UNKNOWN:
                if channel == CHANNEL_WWW:
                    probed_state, reason = _probe_www(cookies, profile_adapter)
                elif channel == CHANNEL_CREATOR:
                    probed_state, reason = _probe_creator(cookies, creator_adapter_factory)
                else:
                    probed_state, reason = _probe_ark(cookies, qianfan_adapter_factory)
                entry["state"] = probed_state
                entry["reason"] = reason
                entry["probed"] = True
            channels[channel] = entry
        result[account.id] = {"channels": channels, "checked_at": checked_at}
    return result


def summarize_state(state_payload: dict[str, Any]) -> str:
    """把一次登录态压成一句话（给日志与测试断言用；**不用于界面**）。

    优先级刻意是 `expired` > `missing` > `guest` > `unknown` > `valid`：
    有失效的令牌时，用户最需要知道的是那一条。
    """
    channels = state_payload.get("channels") or {}
    states = [entry.get("state") for entry in channels.values()]
    for candidate in (STATE_EXPIRED, STATE_MISSING, STATE_GUEST, STATE_UNKNOWN, STATE_VALID):
        if candidate in states:
            return candidate
    return ""


# ⚠️ 「哪些状态该给「重新登录」入口」**刻意不在这里**：那是**呈现**决定（改版就意味着改后端），
# 而状态本身才是业务判断。映射只有一份实现：`frontend/src/lib/login-state.ts::loginStateView()`。
