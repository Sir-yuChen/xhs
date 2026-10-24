from __future__ import annotations

import json
import logging
import re
from typing import Any, Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.app.core.security import decrypt_text, encrypt_text
from backend.app.core.time import shanghai_now
from backend.app.models import AccountCookieVersion, PlatformAccount
from xhs_utils.cookie_util import trans_cookies

logger = logging.getLogger(__name__)


def account_profile_from_user_info(user_info: dict[str, Any]) -> dict[str, Any]:
    profile = user_info.get("profile")
    if isinstance(profile, dict):
        return profile
    return {}


def decode_cookie_text(value: str) -> dict[str, Any]:
    stripped = value.strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    return trans_cookies(stripped)


def cookie_header_from_text(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return ""
    if not stripped.startswith("{"):
        return stripped
    cookies = decode_cookie_text(stripped)
    return "; ".join(f"{key}={cookie_value}" for key, cookie_value in cookies.items())


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


# ---------------------------------------------------------------------------
# 账号级互动数据（粉丝 / 关注 / 获赞）
#
# 唯一真相源：`/api/sns/web/v1/user/otherinfo` 的 `data.interactions[]`。
# 为什么不读 `/v2/user/me`：实测该响应的 `data` 只有 8 个字段
# （desc/gender/guest/imageb/images/nickname/red_id/user_id），**没有
# `interactions`** —— 原实现读它，于是三个指标恒为 None（缺陷 DEF-039，
# 表现是账号卡片「粉丝/关注/获赞」永远显示 `-`）。设计见
# `docs/02_设计文档/12_功能设计_我的笔记与互动数据.md` §21。
# ---------------------------------------------------------------------------

WEB_SESSION_COOKIE = "web_session"

#: 真登录的 jar 里一定有的第二把钥匙（DEF-051）。
#: 平台对**未登录访客**也会种 `web_session`（游客会话），所以只看 `web_session`
#: 会把游客判成「已登录」。真机三组对照实测（2026-09-20，真实 Chrome + 隔离 profile）：
#:
#: | 组 | web_session | id_token | `/v2/user/me` |
#: |---|---|---|---|
#: | 什么都不注入，只浏览 www（纯游客） | 有 | **无** | `ok=True, nickname=None`，id 每次不同 |
#: | 注入真账号凭据 | 有 | **有** | `nickname=本人` |
#: | 注入创作者凭据后访问 www | 有 | **无** | `ok=True, nickname=None`（游客） |
#:
#: ⇒ `id_token` 是「真的登录过」的本地判别式，**零平台请求**。
ID_TOKEN_COOKIE = "id_token"

#: 平台的 interaction 类型 → 我们落库/展示的字段名（唯一映射表，禁止在别处再写一套）
OTHERINFO_INTERACTION_FIELDS: dict[str, str] = {
    "fans": "followers",
    "follows": "following",
    "interaction": "likes",
}

#: 账号级互动数据的三个字段（顺序即 UI 顺序：粉丝 / 关注 / 获赞）
METRIC_FIELDS: tuple[str, ...] = ("followers", "following", "likes")

#: 原因文案的长度窗口（R10 / D16，用户 2026-09-18 拍板）。
#: 卡片上只留一行，70 字的解释贴在卡片上等于没写——固定文案必须写在窗口内，
#: 动态部分（平台 msg、异常文本）由 :func:`clamp_reason` 只压上限。
METRICS_REASON_MIN_CHARS = 25
METRICS_REASON_MAX_CHARS = 30

# ⚠️ **v1.31（2026-09-21 用户口径）移除 `CREATOR_METRICS_REASON`**
# （「创作者平台不返回互动数据，请改用网页版登录的账号查看」）：
# 该归因**已被真机抓包推翻** —— 带 www 会话的创作者账号走 www 通道能正常取到
# 三项计数（真实库 acct 8：creator + www 两个通道并存，旧实现却按 sub_type
# 优先取了 creator 通道 ⇒ 取不到还给出「平台没有这个能力」的错原因）。
# 错归因比没有原因更糟：用户再也不会去补登录（DEF-058 同族）。
# 现行口径见设计 12 §21.4：**一律 www 通道 + 能力判定**，取不到统一用
# `NO_WEB_SESSION_METRICS_REASON`（缺的是网页会话，不是平台能力）。
NO_WEB_SESSION_METRICS_REASON = "该账号 Cookie 不含网页会话，请重新登录网页版账号"
PLATFORM_EMPTY_METRICS_REASON = "平台本次未返回互动数据，请稍后重试或重新登录网页版账号"
METRICS_FETCH_ERROR_REASON = "读取互动数据失败，请稍后重试，或重新登录网页版账号"
IDENTITY_UNKNOWN_METRICS_REASON = "无法识别账号身份，Cookie 可能已失效，请重新登录账号"
NO_COOKIE_METRICS_REASON = "该账号没有已保存的 Cookie 版本，无法读取互动数据。"

# ---------------------------------------------------------------------------
# 凭据通道（设计 12 §11.5 / v1.24；台账 §4.10.11）
#
# 一个账号可以并存**三条互不通用**的令牌。选哪一条只看「要打哪个平台的接口」：
#   * www      —— 网页版（`web_session` + `id_token`）→ `www` / `edith` / `sns/web`
#   * creator  —— 创作者平台（`access-token-creator.*`）→ `creator.xiaohongshu.com`
#   * ark      —— 千帆（`access-token-ark.*`）→ `ark.xiaohongshu.com`
#
# ⚠️ **2026-09-21 拆分（用户拍板）**：创作者与千帆**不再是同一份令牌**。
# 真机实测（观隅好物 acct 8）：创作者扫码拿到的 jar 只带 `*-creator.xiaohongshu.com`
# 域的令牌，打千帆一律 `-13000 缺失登录凭证`；而千帆（绑定表）那份 jar 是**超集**
# （同时带 creator 域与 ark 域令牌），打创作者平台正常。原「共用 ark 通道」的假设
# 由此被推翻 —— 后果正是用户遇到的问题：千帆探针拿着创作者 jar 去问千帆，
# 平台答「缺失登录凭证」，界面显示「千帆已失效」，而令牌其实好好的。
#
# 因此两条口径定死：
#   ① **分类按 jar 形态**（平台域强键判定，不靠调用方传）；
#   ② **取票按目标平台**（打创作者平台取 creator 通道，打千帆取 ark 通道）。
# ---------------------------------------------------------------------------
CHANNEL_WWW = "www"
CHANNEL_CREATOR = "creator"
CHANNEL_ARK = "ark"
CHANNELS = (CHANNEL_WWW, CHANNEL_CREATOR, CHANNEL_ARK)

#: 通道判定的**强键**（平台域令牌）。真机实测（2026-09-21）：
#:   创作者 jar 带 `access-token-creator.xiaohongshu.com`；
#:   千帆 jar 带 `access-token-ark.xiaohongshu.com`（通常是整份浏览器 jar 的超集）。
_ARK_STRONG_KEYS = ("access-token-ark.xiaohongshu.com", "x-user-id-ark.xiaohongshu.com")
_CREATOR_STRONG_KEYS = (
    "access-token-creator.xiaohongshu.com",
    "x-user-id-creator.xiaohongshu.com",
    "galaxy.creator.beaker.session.id",
)
_WWW_MARKER_KEYS = ("web_session", "id_token")
#: customer 单点登录的**弱键**：创作者与千帆 jar 都带，只能说明「登录过」，
#: **不能**说明「能打哪个平台」—— 那个只有平台域令牌（强键）能回答。
_SSO_WEAK_KEYS = ("customer-sso-sid", "galaxy_creator_session_id")


def cookie_names(cookies: str) -> set[str]:
    """这个 jar 里**有值**的 Cookie 名集合（零请求、零副作用）。

    通道判定与「是不是登录态」两个问题都要问「有哪些键」，所以解析只留这一份：
    原先 `channel_for_cookies` 与 `has_web_session` 各写了一遍相同的切分逻辑，
    两份实现迟早会在某个边角（空值、多余分号、大小写）上分叉，而它们判的是**同一件事**。

    ## ⚠️ 必须同时认识 **JSON 形态**（DEF-059）

    库里存的 jar **有两种形态**，而且**不是可选的**：

    * `k=v; k=v` —— 用户粘贴的 Cookie 串；
    * `{"k":"v",...}` —— **所有登录路径**落的形态（`login_sessions._create_account_from_login`
      用 `_dump_json(cookies)` 把适配器给的字典序列化后入库）。

    原实现只按 `;` / `=` 切分：**JSON 串里既没有 `;` 也没有 `=`**，于是
    `cookie_names()` 恒为空集 ⇒ `channel_for_cookies()` 对**每一份登录来的 jar**
    都返回 `www`（含创作者 / 千帆的 ark jar），`has_web_session()` 同样恒为假。
    后果不是「报错」而是**静默归错通道**：ark 令牌被写进 www，随后
    `latest_cookies_for_channel(ark)` 取不到 → 创作者 / 千帆取数拿不到令牌
    （「一个账号两条令牌」在登录路径上从未真正生效）。
    既有用例全用 `k=v` 字符串写夹具，所以这条路径**一次都没被走到**。

    坏输入（畸形 JSON）返回空集而不是抛：本函数服务的是**判定**，判不出来的正确
    后果是「判不出来」，不是让导入 / 登录链路 500。
    """
    text = (cookies or "").strip()
    if not text:
        return set()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            return set()
        if not isinstance(parsed, dict):
            return set()
        return {
            str(name).strip()
            for name, value in parsed.items()
            if str(name).strip() and str(value).strip()
        }
    names: set[str] = set()
    for part in text.split(";"):
        name, _, value = part.strip().partition("=")
        if name and value.strip():
            names.add(name.strip())
    return names


def channel_for_cookies(cookies: str) -> str:
    """按 Cookie 形态判定这条 jar 服务于哪个平台（**不靠调用方传**）。

    判定顺序（2026-09-21 拆分后，**平台域强键**优先）：
    1. 带 ark 域令牌（`access-token-ark.*`）→ `ark`（千帆）；
    2. 否则带 creator 域令牌 → `creator`（创作者平台）；
    3. 否则只带 customer 单点登录弱键（`customer-sso-sid` 等）→ `creator`
       （它显然**不是** www jar —— 没有 `web_session`；单点登录产物离创作者平台更近）；
    4. 否则带网页会话（`web_session`）→ `www`；
    5. 都没有 → `www`（默认不变）。

    ⚠️ ark 必须优先于 creator：千帆那份 jar 通常是**超集**（同时带两个平台的
    域令牌），不优先就会把它误存进 creator 平台，千帆反而取不到自己的令牌。
    ⚠️ 单靠弱键**分不出**「能打哪个平台」（观隅好物的教训：创作者 jar 带齐弱键，
    打千帆照样 `-13000 缺失登录凭证`）—— 所以弱键只用来「排除 www」，平台归属
    最终由探针确认（L3），不由这里宣布。
    """
    names = cookie_names(cookies)
    if any(key in names for key in _ARK_STRONG_KEYS):
        return CHANNEL_ARK
    if any(key in names for key in _CREATOR_STRONG_KEYS):
        return CHANNEL_CREATOR
    if any(key in names for key in _SSO_WEAK_KEYS):
        return CHANNEL_CREATOR
    if any(key in names for key in _WWW_MARKER_KEYS):
        return CHANNEL_WWW
    return CHANNEL_WWW


# ---------------------------------------------------------------------------
# 令牌的存储形态：**一个字段存信封 JSON**（用户 2026-09-21 口径）
#
# 不拆多字段、不加列，**平台信息也存进 JSON 里**：
#   {"v": 1, "channels": {"www": "<jar 文本>", "creator": "<jar 文本>", "ark": "..."}}
# 一次写入涉及几个平台就包几个。读时**最新一版优先**跨行合并 —— 一次创作者
# 扫码只更新 creator 通道，同一个账号其它平台一个字节都不动（§11.5 语义不变，
# 只是从「一行一个通道」变成「一行一个信封」）。
# ---------------------------------------------------------------------------
ENVELOPE_VERSION = 1
ENVELOPE_CHANNELS_KEY = "channels"


def dump_channel_envelope(channels: dict[str, str]) -> str:
    """`{平台: jar 文本}` → 信封 JSON 文本（落库用）。空值 / 未知平台不写进去。"""
    payload = {
        "v": ENVELOPE_VERSION,
        ENVELOPE_CHANNELS_KEY: {
            channel: jar
            for channel, jar in (channels or {}).items()
            if channel in CHANNELS and str(jar or "").strip()
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def parse_channel_envelope(text: str) -> dict[str, str]:
    """信封 JSON 文本 → `{平台: jar 文本}`；**不是信封返回空字典**。

    判据刻意严格：`channels` 必须是 dict、键必须是已知平台、值必须是非空字符串。
    Cookie jar 的 JSON 形态也是 dict，但它的键是 Cookie 名（`a1` / `web_session`…），
    整棵子树不可能全是「已知平台名 → 字符串」—— 所以认不出来的（含迁移前的
    老数据）返回空字典，由调用方决定怎么办，**不猜**。
    """
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    channels = data.get(ENVELOPE_CHANNELS_KEY)
    if not isinstance(channels, dict):
        return {}
    return {
        str(channel): jar
        for channel, jar in channels.items()
        if channel in CHANNELS and isinstance(jar, str) and jar.strip()
    }


def latest_cookies_for_channels(
    db: Session, account_ids: Sequence[int]
) -> dict[tuple[int, str], str]:
    """一批账号的各平台**最新一版**令牌（`{(account_id, channel): 请求头串}`）。

    账号矩阵页要一次给出所有卡片的 L1/L2 判据；逐账号逐平台调
    `latest_cookies_for_channel` 会退化成 N×M 次查询（N+1 的变体，且 SQLite
    上每次都要解密一行）。这里一次查完、只在内存里按「最新一版优先」合并。

    ⚠️ **「最新一版」的排序口径只有一份**：单账号版本 `latest_cookies_for_channel`
    委托到本函数，避免两处各写一遍 `created_at desc, id desc` ——
    那种漂移的后果是「同一行令牌，页面说有效、调用点说没有」。

    ⚠️ 合并方向是 **desc + setdefault**（先出现者赢）：升序覆盖写会让「旧信封」
    反过来顶掉新令牌。一行信封提供它包住的那几个平台；跨行按平台各取最新。
    """
    from backend.app.api.platforms.xhs.pc import _cookies_to_string

    ids = [int(value) for value in account_ids]
    if not ids:
        return {}
    rows = db.scalars(
        select(AccountCookieVersion)
        .where(AccountCookieVersion.platform_account_id.in_(ids))
        .order_by(
            AccountCookieVersion.created_at.desc(),
            AccountCookieVersion.id.desc(),
        )
    ).all()
    latest: dict[tuple[int, str], str] = {}
    undecryptable = 0
    for row in rows:
        # ⚠️ 逐行兜底（2026-10-25 实测教训）：一行的密文解不开（密钥轮换残留 /
        #    损坏）不该让**整个账号列表** 500 —— 那一行按「无 Cookie」处理
        #    （缺值口径：解析不到就留空，不造假、不拖垮别人）。
        try:
            envelope = parse_channel_envelope(decrypt_text(row.encrypted_cookies))
        except Exception:  # noqa: BLE001 - InvalidToken 等解密失败 = 该行视为无 Cookie
            undecryptable += 1
            continue
        for channel, jar in envelope.items():
            latest.setdefault((row.platform_account_id, channel), _cookies_to_string(jar))
    if undecryptable:
        import logging

        logging.getLogger(__name__).warning(
            "latest_cookies_for_channels: %d 行 Cookie 解密失败，已按无 Cookie 处理"
            "（若非个别行，检查 security.secret_key / fernet_key 是否轮换过）",
            undecryptable,
        )
    return latest


def channel_jars(db: Session, account_id: int) -> dict[str, str]:
    """该账号各平台**最新一版**的 jar（`{channel: 请求头串}`；没有的平台不在字典里）。

    批量版的单账号形态，实现委托过去（同一份合并口径，不另写一遍）。
    """
    account_id = int(account_id)
    return {
        channel: jar
        for (row_account_id, channel), jar in latest_cookies_for_channels(db, [account_id]).items()
        if row_account_id == account_id
    }


def latest_cookies_for_channel(db: Session, account_id: int, channel: str) -> str:
    """某账号某个平台的**最新一版**令牌（解密后的请求头串；没有则空串）。

    平台语义（§11.5 / 2026-09-21 拆分）：同一账号下 `www` / `creator` / `ark`
    三条令牌**互不覆盖**。调用方必须先想清楚「这次要打哪个平台」，再选通道 ——
    不要再按「最新一版」猜（猜错的后果是拿创作者 jar 去打千帆，平台只会回
    一句「缺失登录凭证」，而错误信息完全指错方向）。
    """
    return latest_cookies_for_channels(db, [account_id]).get((int(account_id), channel), "")


def store_channel_cookies(*, db: Session, account: PlatformAccount, cookies_text: str) -> None:
    """给**已有账号**追加一版令牌（§11.5 / 2026-09-21 拆分）。

    与 `upsert_platform_account_from_login` 的分工：那个负责「按身份找/建账号并刷新
    资料」，这个只负责「再存一条令牌」。一次登录拿两份 Cookie（www + creator）时走这里，
    避免为了存第二份令牌把账号资料重复写一遍、或干脆新建第二行账号。

    落库形态是**信封 JSON**（一个字段，平台信息也在 JSON 里）。存到哪个平台
    **按 jar 形态判定，不靠调用方传** —— 调用方经常分不清自己手里是哪一份
    （观隅好物的教训：创作者扫码的 jar 被当成千帆令牌存，打千帆一律
    「缺失登录凭证」，而界面显示「千帆已失效」，令牌其实好好的）。
    """
    channel = channel_for_cookies(cookies_text)
    db.add(
        AccountCookieVersion(
            platform_account_id=account.id,
            encrypted_cookies=encrypt_text(dump_channel_envelope({channel: cookies_text})),
        )
    )


def clamp_reason(text: str, limit: int = METRICS_REASON_MAX_CHARS) -> str:
    """把原因压到 ``limit`` 字以内（R10 / D16 的硬约束）。

    固定文案自己就写在 25–30 字窗口内；**动态部分**长度不可控，必须在这里收口——
    否则一句 `HTTPSConnectionPool(host='www.xiaohongshu.com', port=443): Read timed out`
    会糊满整张卡片。
    """
    reason = (text or "").strip()
    if len(reason) <= limit:
        return reason
    return reason[: limit - 1] + "…"


def has_web_session(cookies: str) -> bool:
    """这个 Cookie 串能否**认证**网页版（www）接口。

    **能力判定只有这一份实现**；`api/platforms/xhs/pc.py::_has_web_session`
    只是本函数的别名。判定依据刻意不是 `sub_type`：后者描述协议链路，
    不描述这个 jar 实际能认证什么。

    **DEF-051（2026-09-20）**：判据从「有 `web_session`」收紧为
    「有 `web_session` **且**有 `id_token`」—— 平台对**未登录访客**也会发
    `web_session`（游客会话），只看前者会把游客判成「网页会话可用」，
    页签亮着却读不出任何本人数据。理由与真机对照数据见 :data:`ID_TOKEN_COOKIE`。

    收紧方向是**安全的**：宁可把「疑似有效」判成无效（→ 让用户重登），
    也不拿一个可能属于游客的 jar 去回答「你赞过什么」（缺值不造假 / 不显示错数据）。
    """
    names = cookie_names(cookies)
    return WEB_SESSION_COOKIE in names and ID_TOKEN_COOKIE in names


def has_ark_token(cookies: str) -> bool:
    """这个 jar 能否认证**千帆**（`ark` 平台的 L2 判据）。

    与 :func:`has_web_session` 对称：它也回答「是**登录态**，还是只拿到了空壳」，
    **零平台请求**。

    ⚠️ 2026-09-21 拆分后 ark 只服务千帆。判据 = **ark 域强键**或 customer
    单点登录弱键。弱键（`customer-sso-sid`）创作者 jar 也有 —— 所以「L2 过关」
    只说明「是登录态」，**能不能打千帆要探针说了算**：创作者 jar 打千帆，
    平台回 `-13000 缺失登录凭证`，判 `expired` 是实话（那不是千帆的令牌）。

    ⚠️ 不要拿它判 www（真机实测：只注入创作者凭据访问 www 会掉进游客态）。
    """
    names = cookie_names(cookies)
    return any(key in names for key in _ARK_STRONG_KEYS + _SSO_WEAK_KEYS)


def has_creator_token(cookies: str) -> bool:
    """这个 jar 能否认证**创作者平台**（`creator` 平台的 L2 判据）。

    判据 = **creator 域强键**或 customer 单点登录弱键。千帆那份完整 jar 是
    超集（同时带 creator 域令牌），所以它也能过这一判 —— 平台能力由探针确认。
    """
    names = cookie_names(cookies)
    return any(key in names for key in _CREATOR_STRONG_KEYS + _SSO_WEAK_KEYS)


def coerce_count(value: Any) -> int | None:
    """平台计数 → int；**拿不到就返回 None**（调用方决定显示 `-` 还是 0）。

    这是全项目唯一的计数转换实现：`pc.py::_metric` 委托到这里，
    避免「同一个平台字段在两条链路上被解析成不同结果」。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    multiplier = 1
    if text.endswith("万"):
        multiplier = 10000
        text = text[:-1]
    elif text.lower().endswith("w"):
        multiplier = 10000
        text = text[:-1]
    number_match = re.search(r"\d+(?:\.\d+)?", text)
    if not number_match:
        return None
    return int(float(number_match.group(0)) * multiplier)


def interaction_counts_from_otherinfo(response: Any) -> dict[str, int]:
    """`/v1/user/otherinfo` 的 `data.interactions[]` → ``{followers, following, likes}``。

    实测形状（真实账号）::

        [{"type": "follows",     "count": "258", "name": "关注"},
         {"type": "fans",        "count": "49",  "name": "粉丝"},
         {"type": "interaction", "count": "39",  "name": "获赞与收藏"}]

    取不到任何一项时返回**空 dict**（而不是填 0）：缺值必须能被上层区分出来。
    """
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        return {}
    items = data.get("interactions")
    if not isinstance(items, list):
        return {}
    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        field = OTHERINFO_INTERACTION_FIELDS.get(str(item.get("type") or "").strip())
        if field is None:
            continue
        count = coerce_count(_first_present(item.get("count"), item.get("i18n_count")))
        if count is not None:
            counts[field] = count
    return counts


def fetch_account_metrics(
    profile_adapter: Any,
    *,
    cookies: str,
    sub_type: str | None,
    external_user_id: str = "",
) -> tuple[dict[str, int], str]:
    """取账号级互动数据 —— 返回 ``(metrics, reason)``。

    `reason` 非空表示**不可得**（此时 `metrics` 为空 dict），文案可直接展示给运营。

    三条硬约定：
      1. **按能力路由**（`web_session` 是否存在），**完全不看 `sub_type`**（v1.31：
         取数通道由调用方选 www，见设计 12 §21.4；`sub_type` 只进日志）；
      2. 没有网页会话 → **零平台请求**（结构上不可能成功的事不做，
         与 `/users/capabilities` 同构）；
      3. 任何失败都**降级为可读原因**，不抛异常 —— 这是「可选步骤」，
         但它必须**留下结果**，不能静默返回空。

    ⚠️ **v1.31 起这里不再有「creator 专用原因」**：旧实现对 creator 账号直接返回
    「创作者平台不返回互动数据」—— 该归因已被真机抓包推翻（带 www 会话的
    creator 账号取得到计数，真实库 acct 8 实测），错归因会让用户永远不去补登录。
    现在两类账号走同一条能力判据。
    """
    # 调用点可能传「JSON 字符串」（扫码/手机登录落库的形态）或「Cookie 头」。
    # 统一规范化后再判能力：否则 `web_session` 会被埋在 JSON 里判不出来，
    # 结果把「有网页会话」误判成「没有」，三项又变回 `-`（DEF-039 的复发形态）。
    header = cookie_header_from_text(cookies)
    if not has_web_session(header):
        return {}, NO_WEB_SESSION_METRICS_REASON

    try:
        user_id = (external_user_id or "").strip()
        if not user_id:
            # 落库的 external_user_id 缺失/过期时，才多花一次身份请求。
            success, _message, payload = profile_adapter.get_self_profile(header)
            if success and isinstance(payload, dict):
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                user_id = str(_first_present(data.get("user_id"), "") or "")
            if not user_id:
                return {}, IDENTITY_UNKNOWN_METRICS_REASON

        success, message, payload = profile_adapter.get_other_info(header, user_id)
        counts = interaction_counts_from_otherinfo(payload) if success else {}
        if not counts:
            if message:
                # 平台自己给的原因（如「登录已过期」）比笼统文案有用，但同样受长度窗口约束。
                return {}, clamp_reason(f"平台未返回互动数据：{message}")
            return {}, PLATFORM_EMPTY_METRICS_REASON
        return counts, ""
    except Exception as exc:  # noqa: BLE001 - 可选步骤：失败必须留下原因
        # 异常文本进日志（给开发者），卡片上只给 25–30 字的固定原因（给运营）。
        # ⚠️ 日志里只放 sub_type / external_user_id，**绝不打印 cookies**。
        logger.warning(
            "读取账号级互动数据失败 sub_type=%s external_user_id=%s: %s",
            sub_type,
            external_user_id,
            exc,
        )
        return {}, METRICS_FETCH_ERROR_REASON


class XhsSelfProfileAdapter:
    """网页版身份与账号级资料的取数口（真实实现；测试可整体替换）。

    两个方法的**返回形状不同，是有意的**：

    * :meth:`get_self_profile` → **完整响应**（含 `data`）。因为
      `enrich_user_info_with_xhs_self_profile()` 读的就是响应里的 `data`。
      ⚠️ **不要改成返回内层 `data`**：`XhsPcApiAdapter.get_self_info()` 正是那样，
      而它让身份增强**静默什么都没做**（red_id / 昵称 / 头像永远不更新）——
      与 DEF-039 同族的「形状不匹配」缺陷。
    * :meth:`get_other_info` → `(success, message, payload)` 三元组，保留平台原文，
      让调用方能区分「Cookie 过期」与「响应结构变了」。
    """

    def get_self_profile(self, cookies_text: str):
        from backend.app.adapters.xhs.pc_api_adapter import XhsPcApiAdapter

        success, message, payload = XhsPcApiAdapter(cookies_text).get_self_profile()
        if not success or not isinstance(payload, dict):
            raise RuntimeError(message or "XHS self profile refresh failed")
        return payload

    def get_other_info(self, cookies_text: str, user_id: str):
        """`/v1/user/otherinfo`：账号级互动计数（粉丝/关注/获赞）的真实来源（DEF-039）。"""
        from backend.app.adapters.xhs.pc_api_adapter import XhsPcApiAdapter

        return XhsPcApiAdapter(cookies_text).get_user_other_info(user_id)

    def probe_self_profile(self, cookies_text: str):
        """**只读探活**：这条 www 令牌**现在还能不能用**（返回原始三元组，设计 13 §18.5 的 L3）。

        与 :meth:`get_self_profile` 的差别是**刻意的**，理由与它和 `get_self_info` 的差别同类：

        * `get_self_profile` 在 `success=False` 时 `raise` —— 它服务「刷新资料」，
          失败就该中断；但它**丢掉了原始 payload**。
        * 本方法**保留 payload**，因为「平台拒绝了」与「根本没问到」必须分开：
          SDK 的 `get_user_me` 在网络异常时不抛异常（`except` 里把 `success=False`），
          **此时 `payload is None`**；而登录态失效时平台**回了一个 JSON**（`msg=登录已过期`）。
          ⇒ 判据是 `payload is None`（结构），不是去匹配错误文案（真机实测口径见 §18.5）。
        """
        from backend.app.adapters.xhs.pc_api_adapter import XhsPcApiAdapter

        return XhsPcApiAdapter(cookies_text).get_self_profile()


def get_xhs_self_profile_adapter() -> XhsSelfProfileAdapter:
    return XhsSelfProfileAdapter()


def merge_metrics_into_profile(account: PlatformAccount, metrics: dict[str, Any]) -> bool:
    """把互动计数并入 `profile_json`；**metrics 为空时不动**（失败刷新不得擦数据）。

    返回是否发生了改动。刻意只写 `METRIC_FIELDS` 三个键：
    其余资料字段由身份/健康检查链路负责，两者互不覆写。
    """
    if not metrics:
        return False
    try:
        profile = json.loads(account.profile_json or "{}")
        if not isinstance(profile, dict):
            profile = {}
    except json.JSONDecodeError:
        profile = {}

    changed = False
    for field in METRIC_FIELDS:
        value = metrics.get(field)
        if value is None:
            continue
        if profile.get(field) != value:
            profile[field] = value
            changed = True
    if not changed:
        return False

    account.profile_json = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
    account.updated_at = shanghai_now()
    return True


def enrich_user_info_with_xhs_self_profile(
    user_info: dict[str, Any],
    response: dict[str, Any],
    *,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把网页版身份字段并入 `user_info`；互动计数由 `metrics` 显式传入。

    `response` 是 `/v2/user/me` 的响应（basic_info + 身份字段），
    **它没有 interactions** —— 计数必须由调用方用
    :func:`interaction_counts_from_otherinfo` 从 `/v1/user/otherinfo` 取好再传进来（DEF-039）。

    `metrics=None` 或某个键缺失时**保留已有值**：一次失败刷新不应该把好数据抹成空。
    """
    data = response.get("data") if isinstance(response, dict) else {}
    if not isinstance(data, dict):
        return user_info
    basic_info = data.get("basic_info")
    if not isinstance(basic_info, dict):
        basic_info = {}

    existing = account_profile_from_user_info(user_info)
    metrics = metrics or {}
    profile = {
        **existing,
        "red_id": _first_present(basic_info.get("red_id"), existing.get("red_id"), ""),
        "description": _first_present(basic_info.get("desc"), existing.get("description"), ""),
        "ip_location": _first_present(basic_info.get("ip_location"), existing.get("ip_location"), ""),
        "gender": _first_present(basic_info.get("gender"), existing.get("gender")),
        "followers": _first_present(metrics.get("followers"), existing.get("followers")),
        "following": _first_present(metrics.get("following"), existing.get("following")),
        "likes": _first_present(metrics.get("likes"), existing.get("likes")),
        "raw": response,
    }
    return {
        **user_info,
        "nickname": _first_present(basic_info.get("nickname"), user_info.get("nickname"), ""),
        "avatar_url": _first_present(basic_info.get("images"), basic_info.get("imageb"), user_info.get("avatar_url"), ""),
        "profile": profile,
    }


def serialize_account(account: PlatformAccount, action: str | None = None) -> dict[str, Any]:
    try:
        profile = json.loads(account.profile_json or "{}")
        if not isinstance(profile, dict):
            profile = {}
    except json.JSONDecodeError:
        profile = {}

    payload = {
        "id": account.id,
        "platform": account.platform,
        "sub_type": account.sub_type,
        "external_user_id": account.external_user_id,
        "nickname": account.nickname,
        "avatar_url": account.avatar_url,
        "status": account.status,
        "status_message": account.status_message,
        "profile": profile,
        "created_at": account.created_at.isoformat() if account.created_at else None,
        "updated_at": (account.updated_at or account.created_at).isoformat() if (account.updated_at or account.created_at) else None,
    }
    if action:
        payload["action"] = action
    return payload


def upsert_platform_account_from_login(
    *,
    db: Session,
    user_id: int,
    platform: str,
    sub_type: str,
    user_info: dict[str, Any],
    cookies_text: str,
) -> tuple[PlatformAccount, str]:
    """**所有建号路径的唯一写入点**（登录 / Cookie 导入 / PC→创作者派生）。

    身份键（I1，台账 §4.9）= `(user_id, platform, external_user_id)` —— **按小红书账号 ID**，
    与 DB 层的部分唯一索引 `uq_platform_accounts_xhs_identity` **同口径**。

    三个容易写错的地方：

    * **`sub_type` 不在键里**（2026-09-26 用户口径：同一个小红书 ID 不允许两行）。
      同一账号的 `pc` 与 `creator` 是两条**合法链路**，但它们是**同一行**的两条链路：
      令牌各在自己的通道（`www` / `creator` / `ark`），`sub_type` 这一列记录
      **更强**的那条（`creator` > `qianfan` > `pc`）。⚠️ 若取 `pc`，
      发布能力会被**静默丢掉**（发布闸门要求 `sub_type == "creator"`），
      所以合并方向由 `account_types.strongest_sub_type()` 决定，不靠写入顺序。
    * **必须能命中软删行**：账号删除是软删（I3）。这里**故意不过滤 `deleted_at`**，
      命中后把 `deleted_at` 清空即「复活」—— 这正是「删了再导入又来一条」的根治办法。
    * **`external_user_id` 为空时降级匹配**：空 id 不参与唯一索引（部分索引），
      若不加兜底，每次导入都会新建。用 `(user_id, platform, sub_type, nickname)` 兜底，
      **既不伪造 id，也不会无界新建**。
    * **Cookie 按平台落库**（§11.5 / 2026-09-21 拆分）：jar 属于哪个平台由
      `channel_for_cookies()` **按形态判定**（www / creator / ark 三态），
      不靠调用方传。同一账号的三个平台令牌**互不覆盖**，调用方按「要打哪个
      平台」选通道读（`latest_cookies_for_channel`）。落库形态是信封 JSON
      （一个字段，平台信息在 JSON 里）。
    """
    external_user_id = user_info.get("external_user_id", "") or ""
    nickname = user_info.get("nickname", "") or ""
    # 合并同一身份的两条链路时要挑「更强的那条」，判定只有一份实现
    from backend.app.core.account_types import strongest_sub_type

    account = None
    if external_user_id:
        # ⚠️ **身份键不含 `sub_type`**（2026-09-26 用户口径）：按**小红书账号 ID** 找行。
        # 含 sub_type 时，同一个账号先按「PC」扫、再按「创作者平台」扫就会各建一行，
        # 卡片上看起来是「两个相同的账号信息」（真实库实测：同 external_user_id、
        # 同昵称、同小红书号）。与 DB 部分唯一索引 `uq_platform_accounts_xhs_identity`
        # **逐字同口径** —— 两处不一致的后果是「本地检查放行、DB 约束拒绝」，
        # 用户看到 500 而不是一行结果。
        account = db.scalars(
            select(PlatformAccount)
            .where(
                PlatformAccount.user_id == user_id,
                PlatformAccount.platform == platform,
                PlatformAccount.external_user_id == external_user_id,
            )
            .order_by(PlatformAccount.id)
            .limit(1)
        ).first()
    elif nickname:
        account = db.scalar(
            select(PlatformAccount).where(
                PlatformAccount.user_id == user_id,
                PlatformAccount.platform == platform,
                PlatformAccount.sub_type == sub_type,
                PlatformAccount.nickname == nickname,
                or_(
                    PlatformAccount.external_user_id.is_(None),
                    PlatformAccount.external_user_id == "",
                ),
            )
        )

    action = "updated" if account is not None else "created"
    now = shanghai_now()
    if account is None:
        account = PlatformAccount(
            user_id=user_id,
            platform=platform,
            sub_type=sub_type,
            external_user_id=external_user_id,
            created_at=now,
        )
        db.add(account)
    else:
        # 第二条链路合到同一行：`sub_type` 取更强的那个。
        # ⚠️ 取 `pc` 会**静默丢掉发布能力**（发布闸门要求 `sub_type == "creator"`），
        # 所以这里不允许「后写的覆盖先写的」。
        merged = strongest_sub_type(account.sub_type, sub_type)
        if merged and account.sub_type != merged:
            account.sub_type = merged
        if account.deleted_at is not None:
            # 复活：身份行一直在位，唯一索引因此永久成立
            account.deleted_at = None
            action = "restored"

    account.nickname = nickname or account.nickname or ""
    account.avatar_url = user_info.get("avatar_url", "") or account.avatar_url or ""
    account.external_user_id = external_user_id or account.external_user_id
    account.status = "active"
    account.status_message = ""
    account.profile_json = json.dumps(account_profile_from_user_info(user_info), ensure_ascii=False, separators=(",", ":"))
    account.updated_at = now
    db.flush()
    db.add(
        AccountCookieVersion(
            platform_account_id=account.id,
            encrypted_cookies=encrypt_text(
                dump_channel_envelope({channel_for_cookies(cookies_text): cookies_text})
            ),
        )
    )
    return account, action


# ---------------------------------------------------------------------------
# 发布类操作的账号闸门（T-109 / 缺陷 DEF-005 / 技术债 T-12）
# ---------------------------------------------------------------------------


class AccountRejectedError(Exception):
    """账号不满足操作的前置条件。

    携带 ``status_code``，让 HTTP 层可以原样映射为响应码，
    避免每个调用点各写一套 ``if/raise`` 并逐渐走样。
    """

    status_code = 400

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class AccountNotFoundError(AccountRejectedError):
    """账号不存在，或不属于当前用户。"""

    status_code = 404


class AccountNotPublishableError(AccountRejectedError):
    """账号存在但不可用于发布（平台/链路不符，或状态非 active）。"""

    status_code = 400


def ensure_creator_account_for_publish(account: Any, *, user_id: int) -> PlatformAccount:
    """发布与素材上传类操作的**统一前置闸门**。

    ## 为什么需要它

    在此之前，发布链路只校验 ``sub_type == "creator"``，**不校验账号状态**：
    已被标记 ``expired`` 的账号仍会进入完整发布流程——先白跑一遍素材上传
    （耗时且消耗平台配额），最后才在平台侧失败，且错误信息具有误导性。

    ⚠️ **收益最大的调用点是定时发布**（``scheduler_service._run_one_due_publish_job``）：
    那是**无人值守**路径，过期账号在那里不会被任何人注意到，
    会持续产生无意义的失败与通知噪音。

    ## 为什么不会误伤正常流程

    全项目 ``PlatformAccount`` 只有一处构造点
    （:func:`upsert_platform_account_from_login`），且必定设置
    ``status = "active"``。因此「导入 Cookie」与「扫码/手机号登录」两条
    绑定路径产出的账号**天然是 active**，闸门不会拦住它们。
    被拦住的只有健康检查**确实探测失败**的账号——这正是设计意图。

    ## 校验顺序

    按「先便宜后昂贵」排列，保证错误信息可读：
      1. 存在性
      2. 归属（多租户隔离）
      3. **未被软删**（软删账号等同「不存在」，见台账 §4.9 / I3）
      4. 平台与协议链路
      5. **状态必须为 ``active``**

    :raises AccountNotFoundError: 账号不存在或不属于该用户。
    :raises AccountNotPublishableError: 平台/链路不符，或账号状态不可发布。
    """
    from backend.app.core.account_types import (
        SUB_TYPE_CREATOR,
        SubTypeNotSupportedError,
        normalize_sub_type,
    )

    if account is None:
        raise AccountNotFoundError("Account not found")
    if account.user_id != user_id:
        # 故意不区分「不存在」与「不属于你」，避免越权探测（与既有行为一致）
        raise AccountNotFoundError("Account not found")
    if getattr(account, "deleted_at", None) is not None:
        # 软删账号等同「不存在」（台账 §4.9 / I3）。**定时发布是无人值守路径**，
        # 不在这里拦住的话，一个已被用户删掉的账号仍会继续被派发任务。
        raise AccountNotFoundError("Account not found")
    if account.platform != "xhs":
        raise AccountNotPublishableError("Creator account required")
    # ⚠️ **千帆（ark）链路要单独判**（2026-10-11）：它没有 creator 适配器，
    # `normalize_sub_type` 会抛 SubTypeNotSupportedError —— 那个异常若漏出去就是
    # 500。但这里的语义是「这个账号不可发布」，所以翻成业务拒绝（与未知取值同形，
    # 但原因更准确：不是数据脏了，是**这条链路本来就不参与发布**）。
    try:
        normalized = normalize_sub_type(account.sub_type)
    except SubTypeNotSupportedError as exc:
        raise AccountNotPublishableError(f"Creator account required（{exc}）") from exc
    if normalized != SUB_TYPE_CREATOR:
        raise AccountNotPublishableError("Creator account required")

    account_status = (account.status or "").strip().lower()
    if account_status != "active":
        reason = (account.status_message or "").strip() or "账号未通过有效性检查"
        raise AccountNotPublishableError(
            f"账号当前状态为「{account.status or 'unknown'}」，不可发布；"
            f"请先执行账号检查（POST /accounts/{{id}}/check）。原因：{reason}"
        )
    return account
