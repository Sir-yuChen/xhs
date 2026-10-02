"""测试夹具：往 `account_cookie_versions` 落**信封**（与生产写入同一条路径）。

2026-09-21 起令牌只存信封 JSON（一个字段，平台信息在 JSON 里，没有 channel 列）。
夹具如果直接写裸 jar 文本，读取方（`parse_channel_envelope`）会一个平台都解析不出来 ——
账号在测试里「看起来没有令牌」，于是一条条用例因为莫名其妙的原因变红。

所以夹具也必须走 `dump_channel_envelope`，形状与 `store_channel_cookies()` 的落库
完全一致（「测试 = 可执行口径」，夹具与生产形状不一致时，口径就是假的）。

用法::

    db.add(cookie_version(account.id, "a1=x; web_session=ws; id_token=it", CHANNEL_WWW))
"""
from __future__ import annotations

from backend.app.core.security import encrypt_text
from backend.app.models import AccountCookieVersion
from backend.app.services.account_service import dump_channel_envelope


def cookie_version(account_id: int, cookies_text: str, platform: str) -> AccountCookieVersion:
    """一行「某账号在某平台有一版令牌」的信封（未 commit，由调用方 add）。"""
    return AccountCookieVersion(
        platform_account_id=account_id,
        encrypted_cookies=encrypt_text(dump_channel_envelope({platform: cookies_text})),
    )


def cookie_version_multi(account_id: int, platforms: dict[str, str]) -> AccountCookieVersion:
    """一行信封里包**多个平台**（一次写入涉及几个平台就包几个）。"""
    return AccountCookieVersion(
        platform_account_id=account_id,
        encrypted_cookies=encrypt_text(dump_channel_envelope(platforms)),
    )


#: 一份**形态正确**的千帆 jar（带 `access-token-ark.*` 强键 ⇒ `channel_for_cookies`
#: 判成 `ark`）。夹具里别自己拼别的形状：形态错了会被判到 creator/www 通道，
#: 而「取不到千帆票」的报错会指着一个完全无关的方向。
ARK_COOKIES = "a1=x; access-token-ark.xiaohongshu.com=y"


def give_ark_cookies(db, account, cookies_text: str = ARK_COOKIES) -> None:
    """给账号一份 `ark`（千帆）令牌 —— 只写凭据、不建店铺关系（未 commit）。"""
    db.add(cookie_version(account.id, cookies_text, "ark"))


def bind_shop_account(
    db,
    account,
    *,
    cookies_text: str = ARK_COOKIES,
    shop_name: str = "观隅好物的店",
    seller_id: str = "SELLER-1",
    shop_status: str = "active",
    **state,
):
    """把账号与一个千帆店铺关联起来，并给它一份 `ark` 令牌（= 走完 `POST /bind`）。

    ⚠️ **ADR-021（2026-09-30）起 `shops` 表已退场** —— 本夹具随之收敛为「写两处」：

    | 写什么 | 写哪 |
    |---|---|
    | 千帆**店铺标识** | `platform_accounts.seller_id`（归属列，**不再是实体关系**） |
    | 千帆**凭据** | `account_cookie_versions` 的 `ark` 通道 |
    | **状态与游标** | `platform_accounts.shop_state`（只经 `set_shop_state`） |

    被取代的旧实现原句保留：*「2026-09-22 `shop_account_bindings` 整表移除后，
    夹具从『写一行』变成『写三处』……账号 ↔ 店铺**关系** 写 `platform_accounts.shop_id_fk`」*
    —— 该列已于 ADR-021 摘除，故不再写；`Shop` 行也不再建。

    ⚠️ **归属列 ≠ 能力判据**（ADR-021 核心不变量，账号 9 Bug 的根因）：
    `seller_id` 只回答「这个账号属于哪个千帆店铺」，**不回答「能不能调千帆」** ——
    后者唯一判据是 `account_cookie_versions` 里的 `ark` 令牌（`has_ark_session`）。
    所以 `cookies_text=""` 时依然写 `seller_id`，只是没有票。

    **夹具必须与生产形状一致**（「测试 = 可执行口径」）：不然「看起来绑了店、
    千帆却取不到票」这类红会指错方向。`**state` 透传给 `set_shop_state`
    （如 `goods_note_total=3`、`goods_cursor_ms=1`）。

    返回 `account`（旧实现返回 `Shop` 行；店铺实体退场后**没有别的东西可返**，
    调用点也不消费返回值 —— 实测只用于 `db.add` 之后的链路）。
    """
    from backend.app.services.shop_binding_service import set_shop_state

    account.seller_id = seller_id
    if shop_name:
        # 账号昵称 ≠ 店铺名的历史数据里两者同值居多；此处只在空时补，不覆盖真昵称。
        account.nickname = account.nickname or shop_name
    set_shop_state(account, status=shop_status, **state)
    if cookies_text:
        give_ark_cookies(db, account, cookies_text)
    db.flush()
    return account
