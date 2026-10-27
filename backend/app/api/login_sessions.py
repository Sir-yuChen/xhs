from __future__ import annotations

import base64
import io
import json
import logging
from typing import Any, NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.adapters.xhs.creator_login_adapter import XhsCreatorLoginAdapter
from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter
from backend.app.adapters.xhs.qianfan_login_adapter import XhsQianfanLoginAdapter
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user, get_owned_active_account
from backend.app.core.security import decrypt_text, encrypt_text
from backend.app.models import LoginSession, PlatformAccount, User
from backend.app.services.account_service import (
    channel_for_cookies,
    cookie_header_from_text,
    enrich_user_info_with_xhs_self_profile,
    fetch_account_metrics,
    get_xhs_self_profile_adapter,
    serialize_account,
    store_channel_cookies,
    upsert_platform_account_from_login,
)

router = APIRouter(prefix="/xhs/login-sessions", tags=["xhs-login-sessions"])

logger = logging.getLogger(__name__)


class PcQrCodeRequest(BaseModel):
    sync_creator: bool = False
    #: **续期目标**（设计 13 §18）：把扫出来的新令牌写进**这一个**账号的对应通道，
    #: 而不是按身份去 upsert（后者在「扫成了另一个号」时会**新建一行账号**）。
    #: 留空 = 老的「添加账号」行为，完全不变。
    target_account_id: int | None = None


class CreatorQrCodeRequest(BaseModel):
    target_account_id: int | None = None


class QianfanQrCodeRequest(BaseModel):
    """千帆扫码（2026-09-21）：**只做续期**，必须带目标账号。

    为什么不支持「扫码添加新账号」：千帆侧没有小红书用户身份，扫码只能拿到
    店铺（`shop_user_id`）—— 建号需要的 `external_user_id` / 昵称无从得来。
    新账号请走「绑定账号 → 导入 Cookie」（一次可贴两份）。

    ⚠️ **2026-09-23 被设计 13 §18.15.3 取代**：`target_account_id` 改**可选** ——
    扫码/邮箱密码都能直接建号了（建号用店铺名兜底，不伪造 `external_user_id`）。
    原句保留备查：~~新账号请走「绑定账号 → 导入 Cookie」~~。
    """

    target_account_id: int | None = None


class QianfanEmailLoginRequest(BaseModel):
    """千帆邮箱密码登录（2026-09-23，设计 13 §18.15.3；真机抓包 + 服务端实证）。

    **邮箱与密码不保留**：它们只是本次请求换令牌的输入 —— 不写库、不进
    `LoginSession` 行、不回显、不进日志（硬约束，需求原文）。落库的只有
    Cookie 信封（`account_cookie_versions.encrypted_cookies`，按平台标识）。

    `target_account_id`：带 = 续期（店铺对账，§18.14.3）；不带 = 绑定新账号
    （`sub_type="qianfan"`，昵称取店铺名，`external_user_id` 留空走兜底匹配）。
    """

    model_config = {"extra": "forbid"}

    email: str = Field(min_length=3, max_length=128)
    pwd: str = Field(min_length=1, max_length=128)
    target_account_id: int | None = None


class PhoneSendCodeRequest(BaseModel):
    phone: str = Field(min_length=6, max_length=32)
    sync_creator: bool = False
    #: **续期目标**（设计 13 §18.16）：带 = 把验证码登录拿到的新令牌写进**这一个**
    #: 账号的对应通道；不带 = 老的「添加账号」行为（与 QR 端点同口径）。
    target_account_id: int | None = None


class PhoneConfirmRequest(BaseModel):
    session_id: int
    phone: str = Field(min_length=6, max_length=32)
    code: str = Field(min_length=4, max_length=12)
    sync_creator: bool | None = None
    #: 续期目标（§18.16）；缺省时以发码时写进 temp state 的值为兜底（同 sync_creator）。
    target_account_id: int | None = None


def _dump_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(value: str | None) -> dict:
    if not value:
        return {}
    return json.loads(value)


class TempState(NamedTuple):
    """一次登录会话的临时状态（加密存在 `LoginSession.encrypted_temp_cookies`）。

    **用具名元组而不是 5 元组**：字段一多，位置就变成了「顺序也是语义」那类坑
    （`{...detail, goods}` 与 `{goods, ...detail}` 行为相反，只差一行）。
    """

    cookies: dict
    sync_creator: bool
    device_state: dict
    host_cookie_state: dict
    #: 续期目标账号（见 `PcQrCodeRequest.target_account_id`）；`None` = 添加新账号。
    target_account_id: int | None


def _dump_temp_state(
    cookies: dict,
    *,
    sync_creator: bool = False,
    device_state: dict | None = None,
    host_cookie_state: dict | None = None,
    target_account_id: int | None = None,
) -> str:
    payload = {"cookies": cookies}
    if sync_creator:
        payload["sync_creator"] = True
    if device_state:
        payload["device_state"] = device_state
    if host_cookie_state:
        payload["host_cookie_state"] = host_cookie_state
    if target_account_id:
        payload["target_account_id"] = int(target_account_id)
    return _dump_json(payload)


def _load_temp_state(value: str | None) -> TempState:
    payload = _load_json(value)
    if isinstance(payload, dict) and isinstance(payload.get("cookies"), dict):
        return TempState(
            payload["cookies"],
            bool(payload.get("sync_creator")),
            payload.get("device_state") or {},
            payload.get("host_cookie_state") or {},
            int(payload["target_account_id"]) if payload.get("target_account_id") else None,
        )
    if isinstance(payload, dict):
        return TempState(payload, False, {}, {}, None)
    return TempState({}, False, {}, {}, None)


def _qr_data_url(qr_url: str) -> str:
    import qrcode

    image = qrcode.make(qr_url)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def get_pc_login_adapter() -> XhsPcLoginAdapter:
    return XhsPcLoginAdapter()


def get_creator_login_adapter() -> XhsCreatorLoginAdapter:
    return XhsCreatorLoginAdapter()


def get_qianfan_login_adapter() -> XhsQianfanLoginAdapter:
    return XhsQianfanLoginAdapter()


def _sync_creator_account_from_pc_login(
    *,
    db: Session,
    user_id: int,
    pc_cookies: dict,
    creator_adapter: XhsCreatorLoginAdapter,
) -> tuple[dict | None, str | None]:
    """Best-effort Creator-account sync after a PC login.

    Returns ``(account_payload, error_message)``. The PC account is already bound
    at this point, so a failure here must **not** fail the login — but it must
    also not vanish: the previous implementation returned ``None`` from a bare
    ``except Exception``, which left the UI unable to tell "synced" from
    "silently did nothing" (the checkbox appeared to have no effect at all).
    """
    try:
        creator_result = creator_adapter.exchange_from_user_cookies(pc_cookies)
        creator_user_info = creator_adapter.get_user_info(creator_result["cookies"])
        creator_account, creator_action = _create_account_from_login(
            db=db,
            user_id=user_id,
            sub_type="creator",
            user_info=creator_user_info,
            cookies=creator_result["cookies"],
        )
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not swallowed
        logger.warning("[login] creator sync from PC cookies failed: %s", exc)
        return None, str(exc) or exc.__class__.__name__
    return serialize_account(creator_account, creator_action), None


def _create_account_from_login(
    *,
    db: Session,
    user_id: int,
    sub_type: str,
    user_info: dict,
    cookies: dict,
) -> tuple[PlatformAccount, str]:
    cookies_text = _dump_json(cookies)
    if sub_type == "pc":
        # 身份字段来自 `/v2/user/me`，**互动计数来自 `/v1/user/otherinfo`**（两个接口）。
        # 绑定成功就顺手把三项计数落库，账号卡片不必再点一次「同步数据」（DEF-039）。
        # 注意顺序：`get_self_profile` 失败会抛异常，所以平台不可达时**不会**多打一次请求。
        try:
            header = cookie_header_from_text(cookies_text)
            profile_adapter = get_xhs_self_profile_adapter()
            self_profile = profile_adapter.get_self_profile(header)
            metrics, _reason = fetch_account_metrics(
                profile_adapter,
                cookies=cookies_text,
                sub_type=sub_type,
                external_user_id=str(user_info.get("external_user_id") or ""),
            )
            user_info = enrich_user_info_with_xhs_self_profile(user_info, self_profile, metrics=metrics)
        except Exception as exc:  # noqa: BLE001 - 可选步骤：不阻塞绑定，但必须留痕
            logger.warning("[login] self profile enrich skipped: %s", exc)
    return upsert_platform_account_from_login(
        db=db,
        user_id=user_id,
        platform="xhs",
        sub_type=sub_type,
        user_info=user_info,
        cookies_text=cookies_text,
    )


def _qianfan_shop_user_info(shop: dict) -> dict:
    """千帆店铺信息 → 建号用的 `user_info`（扫码 / 邮箱密码两条路共用）。

    千帆侧**没有小红书用户身份**，能建号的锚点只有店铺：
    * `nickname` = 店铺名（`external_user_id` 留空 ⇒ upsert 走
      `(user_id, platform, sub_type, nickname)` 兜底匹配，**不伪造 id、
      也不会无界新建**，§18.15.3）；
    * `shop_user_id` / `seller_name` 进 `profile`（卡片与后续对账用）。
    """
    shop_name = str(shop.get("shop_name") or "").strip()
    return {
        "nickname": shop_name or "千帆账号",
        "profile": {
            "shop_user_id": str(shop.get("shop_user_id") or ""),
            "seller_name": shop_name,
        },
    }


def _record_login_seller(
    *,
    db: Session,
    account: PlatformAccount,
    shop: dict,
) -> dict | None:
    """登录成功后**顺手把千帆卖家身份写进账号字段**（ADR-021，2026-09-30）。

    ## 从「建店关系」变成「回填账号字段」

    2026-09-26 时的做法是登录后调 `bind_shop_for_account` 建 `shop_id_fk`，
    因为那时「千帆可用」被实现成一条需要维护的店铺关系。ADR-021 之后：
    店铺不再是实体（`shops` 表退场），`seller_id` 下沉为 `platform_accounts`
    自己的字段 ⇒ 这一步退化成**一次纯字段回填**，不再是「建关系」。

    为什么仍然要做：`seller_id` 是**归属**信息（这个千帆卖家属于哪个账号），
    登录是唯一能免费、准确地拿到它的时刻（`get_shop_info()` 已经调用过）。
    不回填的话，商品列表 / 概览里的「卖家 ID」会一直是空的。

    ⚠️ 回填 `seller_id` **不影响能力判据** —— 能不能调千帆看的是 `ark` 令牌
    （`has_ark_session`），令牌在 `upsert_platform_account_from_login` /
    `store_channel_cookies` 那一步已经落库了，与本函数无关。

    返回值恒为 `None`（保留函数签名与调用点的 `bind_error` 形状，减少连带改动）：
    字段回填没有「被别的账号占用」这种冲突了，不存在需要用户处理的失败。
    """
    seller_id = str(shop.get("shop_user_id") or "").strip()
    if seller_id and (account.seller_id or "").strip() != seller_id:
        account.seller_id = seller_id
    return None


def _create_qianfan_account_from_login(
    *,
    db: Session,
    user_id: int,
    cookies: dict,
    qianfan_adapter: Any,
) -> tuple[PlatformAccount | None, str, dict, dict | None]:
    """千帆登录**建号**（扫码 / 邮箱密码共用，设计 13 §18.15.3）。

    返回 `(account, action, shop, bind_error)`；读店铺信息失败时 `account` 为 `None`
    且**什么都不写**（令牌都没落库 —— 建不了号的令牌对用户没有意义，
    落下去只会多一个「千帆已登录却不知道是谁」的半成品行）。

    `bind_error` 目前恒为 `None`（ADR-021 后「回填 `seller_id`」不再有冲突失败；
    保留该返回位是为了不改动调用方的解包形状，见 `_record_login_seller`）。
    """
    try:
        shop = qianfan_adapter.get_shop_info(cookies)
    except Exception as exc:  # noqa: BLE001 - 业务结果，不是崩溃
        logger.warning("[login] qianfan shop info failed on bind: %s", exc)
        return None, "", {
            "code": "QIANFAN_SHOP_UNVERIFIED",
            "message": f"登录成功，但读取千帆店铺信息失败（{exc}）—— 本次没有保存任何令牌。",
        }, None
    account, action = upsert_platform_account_from_login(
        db=db,
        user_id=user_id,
        platform="xhs",
        sub_type="qianfan",
        user_info=_qianfan_shop_user_info(shop),
        cookies_text=_dump_json(cookies),
    )
    bind_error = _record_login_seller(db=db, account=account, shop=shop)
    return account, action, shop, bind_error


#: 续期时**扫到的账号与目标账号不是同一个**（设计 13 §18.8 决定 ③A：报错让用户重扫）。
RENEW_IDENTITY_MISMATCH = "RENEW_IDENTITY_MISMATCH"

#: 续期目标账号不存在 / 不属于当前用户 / 已被软删（DEF-055 口径，§18.9.4）。
RENEW_TARGET_UNAVAILABLE = "RENEW_TARGET_UNAVAILABLE"


def _identity_mismatch_reason(target: PlatformAccount, user_info: dict) -> str:
    """扫到的身份与目标账号是否一致 —— **不一致（含「无法确认」）就返回原因**。

    判定顺序（§18.8 写死）：

    1. 两边都有 `external_user_id` → **必须相等**；
    2. 否则退到 `nickname`（两边都非空且不等 → 不一致）；
    3. **两边都拿不到可比字段 → 也算不一致**。

    ⚠️ 第 3 条是刻意的：**「没检查」不能当成「通过」**。这里照抄的是 DEF-057 的教训，
    但代价更重 —— 那次只是显示错一行，这次是**把令牌写到错误的账号上**。
    与其静默猜，不如让用户重扫一次（他的成本是一次扫码，代价是一次写错库）。
    """
    scanned_id = str(user_info.get("external_user_id") or "").strip()
    scanned_name = str(user_info.get("nickname") or "").strip()
    target_id = str(target.external_user_id or "").strip()
    target_name = str(target.nickname or "").strip()
    described = scanned_name or scanned_id or "未命名账号"

    if target_id and scanned_id:
        if target_id == scanned_id:
            return ""
        return (
            f"扫到的账号是「{described}」，与这张卡片对应的「{target_name or target_id}」不是同一个。"
            "本次**没有保存任何令牌** —— 续期只会改你点开的那个账号，请重新扫描正确账号的二维码。"
        )
    if target_name and scanned_name:
        if target_name == scanned_name:
            return ""
        return (
            f"扫到的账号是「{scanned_name}」，与这张卡片对应的「{target_name}」不是同一个。"
            "本次**没有保存任何令牌**，请重新扫描正确账号的二维码。"
        )
    return (
        f"扫到了「{described}」，但**无法确认**它就是这张卡片对应的账号"
        "（双方都缺少可比对的身份信息）。为避免把令牌写到错误的账号上，本次**没有保存任何令牌** —— "
        "请改用「导入 Cookie」为该账号补齐身份信息后再续期。"
    )


def _renew_account_from_login(
    *,
    db: Session,
    current_user: User,
    target_account_id: int,
    user_info: dict,
    cookies: dict,
) -> tuple[PlatformAccount | None, str, dict | None]:
    """把扫码得到的新令牌写进**指定账号**的对应平台（设计 13 §18）。

    返回 `(account, channel, error)`；`error` 非空时 `account` 为 `None` 且**什么都没写**。

    ## 三条不变量

    * **只写这一个平台**：新 jar 属于哪个平台由 `channel_for_cookies()` 按形态判定
      （不靠调用方传），`store_channel_cookies()` 只追加一行信封 —— 同一账号的
      其它平台**一个字节都不动**（§11.5 / 2026-09-21 拆分）；
    * **不走 upsert**：`upsert_platform_account_from_login` 是按身份键找/建行的，
      在「扫成另一个号」时会**新建一行账号**。续期的语义是「修复这一个」，
      所以这里连调用它的机会都不给自己（§18.8 决定 ③A：宁可失败，不可静默改目标）；
    * **不刷新资料**：续期只换令牌，昵称 / 头像 / 计数仍由「同步数据」「检查」负责
      —— 资料刷新的写入点只有 `upsert_*` 一处，多一个地方写就会漂移。
    """
    try:
        account = get_owned_active_account(
            db, current_user, target_account_id
        )
    except HTTPException:
        return None, "", {
            "code": RENEW_TARGET_UNAVAILABLE,
            "message": "要续期的账号不存在、不属于你，或已被删除 —— 本次没有保存任何令牌。",
        }

    reason = _identity_mismatch_reason(account, user_info)
    if reason:
        return None, "", {"code": RENEW_IDENTITY_MISMATCH, "message": reason}

    cookies_text = _dump_json(cookies)
    channel = channel_for_cookies(cookies_text)
    store_channel_cookies(db=db, account=account, cookies_text=cookies_text)
    return account, channel, None


def _renew_qianfan_from_login(
    *,
    db: Session,
    current_user: User,
    target_account_id: int,
    cookies: dict,
    qianfan_adapter: Any,
) -> tuple[PlatformAccount | None, str, dict | None]:
    """千帆扫码续期：**店铺对账** → 写 ark 平台。返回 `(account, error, shop)`。

    与 PC / 创作者续期的差别只在**身份锚点**：千帆侧没有小红书用户身份，
    能对账的只有店铺（`shop_user_id`）。所以：

    * 账号**已有 `seller_id`**（说明这个账号此前就属某个千帆卖家）且双方都拿得到
      卖家 ID ⇒ 必须相等，否则拒绝写入（§18.8 ③A 同型：宁可失败，不可把别人店的
      令牌写进这个账号）；
    * 账号**没有 `seller_id`**（第一次补千帆令牌）⇒ 无法对账，放行 —— 用户点开的
      就是这张卡片，扫码本身就是意图表达；写入的店铺名随响应回去，界面照实显示。

    ⚠️ 这条不对称是**刻意**的：它对「老账号扫错店」零容忍，对「新账号第一次补」
    不添堵。哪天千帆能给出用户身份了，再把无 seller_id 的分支也收紧。

    ⚠️ 归属信息现在读 `platform_accounts.seller_id`（ADR-021：店铺不再是实体，
    `shop_id_fk` / `shops` 表已随 2026-09-30 迁移退场）。
    """
    try:
        account = get_owned_active_account(
            db, current_user, target_account_id
        )
    except HTTPException:
        return None, "", {
            "code": RENEW_TARGET_UNAVAILABLE,
            "message": "要续期的账号不存在、不属于你，或已被删除 —— 本次没有保存任何令牌。",
        }, None

    try:
        shop = qianfan_adapter.get_shop_info(cookies)
    except Exception as exc:  # noqa: BLE001 - 校验失败本身是业务结果
        logger.warning("[login] qianfan shop info failed: %s", exc)
        return None, "", {
            "code": "RENEW_QIANFAN_UNVERIFIED",
            "message": f"扫码成功，但用这份令牌读取千帆店铺信息失败（{exc}）—— 本次没有保存任何令牌。",
        }, None

    if (
        shop.get("shop_user_id")
        and (account.seller_id or "").strip()
        and account.seller_id != shop["shop_user_id"]
    ):
        return None, "", {
            "code": RENEW_IDENTITY_MISMATCH,
            "message": (
                f"扫到的是店铺「{shop.get('shop_name') or shop['shop_user_id']}」，"
                f"与这个账号已归属的「{account.seller_id}」不是同一个。"
                "本次**没有保存任何令牌** —— 请重新扫描正确店铺的二维码。"
            ),
        }, None

    cookies_text = _dump_json(cookies)
    channel = channel_for_cookies(cookies_text)
    store_channel_cookies(db=db, account=account, cookies_text=cookies_text)
    # ADR-021：不再「登录即建店」，改为**回填 seller_id**（见 `_record_login_seller`）。
    bind_error = _record_login_seller(db=db, account=account, shop=shop)
    return account, channel, shop, bind_error


@router.post("/pc/qrcode")
def pc_qrcode(
    payload: PcQrCodeRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter: XhsPcLoginAdapter = Depends(get_pc_login_adapter),
):
    payload = payload or PcQrCodeRequest()
    try:
        qr_payload = adapter.create_qrcode()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"XHS PC QR code generation failed: {exc}",
        ) from exc
    session = LoginSession(
        user_id=current_user.id,
        platform="xhs",
        sub_type="pc",
        status="pending",
        qr_id=qr_payload["qr_id"],
        code=qr_payload["code"],
        qr_url=qr_payload["qr_url"],
        encrypted_temp_cookies=encrypt_text(
            _dump_temp_state(
                qr_payload["cookies"],
                sync_creator=payload.sync_creator,
                device_state=qr_payload.get("device_state"),
                host_cookie_state=qr_payload.get("host_cookie_state"),
                target_account_id=payload.target_account_id,
            )
        ),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return {
        "session_id": session.id,
        "status": session.status,
        "qr_url": session.qr_url,
        "qr_image_data_url": _qr_data_url(session.qr_url or ""),
    }


@router.post("/creator/qrcode")
def creator_qrcode(
    payload: CreatorQrCodeRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter: XhsCreatorLoginAdapter = Depends(get_creator_login_adapter),
):
    payload = payload or CreatorQrCodeRequest()
    try:
        qr_payload = adapter.create_qrcode()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"XHS Creator QR code generation failed: {exc}",
        ) from exc
    session = LoginSession(
        user_id=current_user.id,
        platform="xhs",
        sub_type="creator",
        status="pending",
        qr_id=qr_payload["qr_id"],
        qr_url=qr_payload["qr_url"],
        encrypted_temp_cookies=encrypt_text(
            _dump_temp_state(
                qr_payload["cookies"],
                target_account_id=payload.target_account_id,
            )
        ),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return {
        "session_id": session.id,
        "status": session.status,
        "qr_url": session.qr_url,
        "qr_image_data_url": _qr_data_url(session.qr_url or ""),
    }


@router.post("/qianfan/qrcode")
def qianfan_qrcode(
    payload: QianfanQrCodeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter: XhsQianfanLoginAdapter = Depends(get_qianfan_login_adapter),
):
    """千帆扫码续期（用户 2026-09-21 要求；真机抓包验证可行）。

    与创作者扫码**同一套 customer 单点登录流程**，service 指向
    `https://ark.xiaohongshu.com` —— 扫码成功后种下的就是千帆域令牌
    （`access-token-ark.xiaohongshu.com` 等）。落到哪个平台由 jar 形态判定
    （必然落在 `ark`），写进**目标账号**，不走 upsert。
    """
    # 目标账号先校验（归属 / 软删）：二维码还没生成就拦掉不合法请求，
    # 别让用户扫完码才被告知「账号不属于你」。
    # 2026-09-23（§18.15.3）：target 改可选 —— 不带 = 扫码建号（confirmed 分支建行）。
    if payload.target_account_id:
        get_owned_active_account(
            db, current_user, payload.target_account_id
        )
    try:
        qr_payload = adapter.create_qrcode()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"XHS Qianfan QR code generation failed: {exc}",
        ) from exc
    session = LoginSession(
        user_id=current_user.id,
        platform="xhs",
        sub_type="qianfan",
        status="pending",
        qr_id=qr_payload["qr_id"],
        qr_url=qr_payload["qr_url"],
        encrypted_temp_cookies=encrypt_text(
            _dump_temp_state(
                qr_payload["cookies"],
                target_account_id=payload.target_account_id,
            )
        ),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return {
        "session_id": session.id,
        "status": session.status,
        "qr_url": session.qr_url,
        "qr_image_data_url": _qr_data_url(session.qr_url or ""),
    }


@router.post("/qianfan/email-login")
def qianfan_email_login(
    payload: QianfanEmailLoginRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter: XhsQianfanLoginAdapter = Depends(get_qianfan_login_adapter),
):
    """千帆邮箱密码登录（2026-09-23，设计 13 §18.15.3；真机抓包 + 服务端实证）。

    **同步端点**：邮箱密码没有「等用户扫码」的中间态，一次请求跑完
    emailPwd → ticket/route → 导航换票 → ssologin → seller/login，
    所以**不建 `LoginSession` 行、不需要轮询**。

    **邮箱与密码不保留**（需求硬约束）：只经适配器换令牌，不写库、
    不进会话行、不回显、不进日志；落库只有 Cookie 信封。

    两种语义（与扫码建号/续期同口径）：
    * 不带 `target_account_id` ⇒ **绑定新账号**（`sub_type="qianfan"`）；
    * 带 ⇒ **续期**（`_renew_qianfan_from_login` 店铺对账，不符一个字节都不写）。
    """
    if payload.target_account_id:
        # 与扫码同一条闸：归属 / 软删先在发请求前拦掉。
        get_owned_active_account(
            db, current_user, payload.target_account_id
        )
    try:
        result = adapter.login_by_email_pwd(payload.email, payload.pwd)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"邮箱密码登录失败：{exc}",
        ) from exc

    cookies = result["cookies"]
    if payload.target_account_id:
        account, renewed_channel, renewed_shop, bind_error = _renew_qianfan_from_login(
            db=db,
            current_user=current_user,
            target_account_id=payload.target_account_id,
            cookies=cookies,
            qianfan_adapter=adapter,
        )
        if account is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=renewed_shop,
            )
        db.commit()
        return {
            "session_id": None,
            "status": "confirmed",
            "account": serialize_account(account, "renewed"),
            "renewed_channel": renewed_channel,
            "renewed_shop": renewed_shop,
            # 令牌已落库、账号已就位，只是店铺关系没绑上（已被别的账号占用等）。
            # 界面照实说明，而不是让「补登成功」看起来一切正常。
            "shop_bind_error": bind_error,
        }

    account, action, renewed_shop, bind_error = _create_qianfan_account_from_login(
        db=db,
        user_id=current_user.id,
        cookies=cookies,
        qianfan_adapter=adapter,
    )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=renewed_shop,
        )
    db.commit()
    return {
        "session_id": None,
        "status": "confirmed",
        "account": serialize_account(account, action),
        "renewed_shop": renewed_shop,
        "shop_bind_error": bind_error,
    }


@router.get("/{session_id}")
def login_session(
    session_id: int,
    sync_creator: bool | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    pc_adapter: XhsPcLoginAdapter = Depends(get_pc_login_adapter),
    creator_adapter: XhsCreatorLoginAdapter = Depends(get_creator_login_adapter),
    qianfan_adapter: XhsQianfanLoginAdapter = Depends(get_qianfan_login_adapter),
):
    """Poll one login session.

    ``sync_creator`` is an **optional live override** for the preference stored
    on the session. It exists because the preference must be changeable *without*
    recreating the session: the original implementation only read it at creation
    time, so the UI regenerated the whole QR code whenever the user ticked the
    box — silently invalidating a QR the user may already have scanned, after
    which the poll kept watching a session nobody had scanned and the page
    appeared completely unresponsive.

    The override is idempotent: the sync itself only runs on the poll that first
    observes ``confirmed``.
    """
    session = db.get(LoginSession, session_id)
    if session is None or session.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Login session not found")
    if session.status in {"confirmed", "expired"}:
        return {"session_id": session.id, "status": session.status, "qr_url": session.qr_url}
    if session.sub_type not in {"pc", "creator", "qianfan"} or not session.qr_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported login session")

    state = _load_temp_state(decrypt_text(session.encrypted_temp_cookies))
    cookies = state.cookies
    effective_sync_creator = (
        state.sync_creator if sync_creator is None else bool(sync_creator)
    )
    if session.sub_type == "pc":
        if not session.code:
            raise HTTPException(status_code=400, detail="Unsupported login session")
        result = pc_adapter.check_qrcode_status(
            session.qr_id,
            session.code,
            cookies,
            device_state=state.device_state,
            host_cookie_state=state.host_cookie_state,
        )
        account_sub_type = "pc"
        user_info = pc_adapter.get_user_info(result["cookies"]) if result["status"] == "confirmed" else None
    elif session.sub_type == "qianfan":
        # 千帆扫码（2026-09-21）：与创作者同一套 CAS 流程，service 指向 ark。
        result = qianfan_adapter.check_qrcode_status(session.qr_id, cookies)
        account_sub_type = "qianfan"
        # 千帆侧没有「小红书用户」，身份锚点是**店铺**（对账在下面续期分支里做）。
        user_info = None
    else:
        result = creator_adapter.check_qrcode_status(session.qr_id, cookies)
        account_sub_type = "creator"
        user_info = creator_adapter.get_user_info(result["cookies"]) if result["status"] == "confirmed" else None
    session.status = result["status"]
    session.encrypted_temp_cookies = encrypt_text(
        _dump_temp_state(
            result["cookies"],
            sync_creator=effective_sync_creator,
            device_state=result.get("device_state") or state.device_state or None,
            host_cookie_state=(
                result.get("host_cookie_state") or state.host_cookie_state or None
            ),
            # 续期目标**必须带过每一轮**：状态每轮重写一次，漏掉它就会在
            # 「扫了但还没确认」的那一轮把目标丢掉，确认轮变成添加新账号。
            target_account_id=state.target_account_id,
        )
    )

    account_payload = None
    creator_account_payload = None
    creator_sync_error = None
    renewed_channel = ""
    renew_error = None
    #: 千帆续期写入的店铺名（仅千帆分支会赋值；其余恒 None）。
    renewed_shop = None
    if session.status == "confirmed":
        if state.target_account_id:
            # **续期**（§18）：写进指定账号的对应通道；身份不符则**什么都不写**。
            # 这条分支**不碰** `_create_account_from_login` / `_sync_creator_account_from_pc_login`
            # —— 它们都会 upsert 出账号行，而续期的语义是「只改我点开的这一个」。
            if session.sub_type == "qianfan":
                # 千帆的身份锚点是**店铺**，对账逻辑不一样（见函数 docstring）。
                account, renewed_channel, renewed_shop, shop_bind_error = _renew_qianfan_from_login(
                    db=db,
                    current_user=current_user,
                    target_account_id=state.target_account_id,
                    cookies=result["cookies"],
                    qianfan_adapter=qianfan_adapter,
                )
                renew_error = renewed_shop if account is None else None
            else:
                renewed_shop = None
                account, renewed_channel, renew_error = _renew_account_from_login(
                    db=db,
                    current_user=current_user,
                    target_account_id=state.target_account_id,
                    user_info=user_info or {},
                    cookies=result["cookies"],
                )
            if account is not None:
                account_payload = serialize_account(account, "renewed")
        else:
            renewed_shop = None
            if session.sub_type == "qianfan":
                # 千帆扫码**建号**（2026-09-23，§18.15.3）：店铺信息 → upsert
                # `sub_type=qianfan`（external_user_id 留空，昵称兜底匹配）。
                account, action, renewed_shop, shop_bind_error = _create_qianfan_account_from_login(
                    db=db,
                    user_id=current_user.id,
                    cookies=result["cookies"],
                    qianfan_adapter=qianfan_adapter,
                )
                if account is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=renewed_shop,
                    )
            else:
                account, action = _create_account_from_login(
                    db=db,
                    user_id=current_user.id,
                    sub_type=account_sub_type,
                    user_info=user_info,
                    cookies=result["cookies"],
                )
            account_payload = serialize_account(account, action)
            if account_sub_type == "pc" and effective_sync_creator:
                creator_account_payload, creator_sync_error = _sync_creator_account_from_pc_login(
                    db=db,
                    user_id=current_user.id,
                    pc_cookies=result["cookies"],
                    creator_adapter=creator_adapter,
                )

    db.commit()
    return {
        "session_id": session.id,
        "status": session.status,
        "qr_url": session.qr_url,
        "account": account_payload,
        "creator_account": creator_account_payload,
        "creator_sync_error": creator_sync_error,
        # 续期结果（`target_account_id` 为空时两者恒为空 / None，行为与从前完全一致）。
        "renewed_channel": renewed_channel,
        # 千帆续期写进去的**店铺名**（其它平台恒为 None）—— 界面照实显示，
        # 用户能立刻看出「扫的是不是我家店」。
        "renewed_shop": renewed_shop,
        "renew_error": renew_error,
    }


@router.post("/pc/phone/send-code")
def pc_phone_send_code(
    payload: PhoneSendCodeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter: XhsPcLoginAdapter = Depends(get_pc_login_adapter),
):
    # 续期目标先校验（归属 / 软删，同 /qianfan/qrcode 的提前闸，§18.16）：
    # 不让用户等一条短信下来才被告知「账号不属于你」。
    if payload.target_account_id:
        get_owned_active_account(
            db, current_user, payload.target_account_id
        )
    try:
        result = adapter.create_phone_session(payload.phone)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to send phone code") from exc
    # ⚠️ **手机号不落库**（同 creator 分支，2026-09-23 用户口径）：连掩码一并停写，
    # 迁移 `x7y8z9a0b1c2` 移除 `login_sessions.phone_mask` 列。
    session = LoginSession(
        user_id=current_user.id,
        platform="xhs",
        sub_type="pc",
        login_method="phone",
        status="pending",
        encrypted_temp_cookies=encrypt_text(
            _dump_temp_state(
                result["cookies"],
                sync_creator=payload.sync_creator,
                # 续期目标带进临时态（§18.16）：confirm 缺省时从这里兜底。
                target_account_id=payload.target_account_id,
            )
        ),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return {"session_id": session.id, "status": session.status, "message": result.get("message", "sent")}


@router.post("/pc/phone/confirm")
def pc_phone_confirm(
    payload: PhoneConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter: XhsPcLoginAdapter = Depends(get_pc_login_adapter),
    creator_adapter: XhsCreatorLoginAdapter = Depends(get_creator_login_adapter),
):
    return _confirm_phone_login(payload, current_user, db, adapter, "pc", creator_adapter)


@router.post("/creator/phone/send-code")
def creator_phone_send_code(
    payload: PhoneSendCodeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter: XhsCreatorLoginAdapter = Depends(get_creator_login_adapter),
):
    # 续期目标先校验（归属 / 软删，同 pc 分支，§18.16）。
    if payload.target_account_id:
        get_owned_active_account(
            db, current_user, payload.target_account_id
        )
    try:
        result = adapter.create_phone_session(payload.phone)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to send phone code") from exc
    # ⚠️ **手机号不落库**（2026-09-23 用户口径：「登录时手机号和邮箱密码等均不落库，
    # 数据库只存登录成功后的账号信息和会话临牌」）：原 `phone_mask=_mask_phone(...)`
    # 连掩码一并停写（该字段只写不读、没有消费方，迁移 `x7y8z9a0b1c2` 整列移除）。
    # 短信验证码同理：只随 confirm 请求透传给平台，从不落任何一行。
    # 2026-09-23（§18.16）：`_dump_json` → `_dump_temp_state` —— 临时态要能携带
    # `target_account_id`（续期目标）；两种形态 `_load_temp_state` 都认，老会话不受影响。
    session = LoginSession(
        user_id=current_user.id,
        platform="xhs",
        sub_type="creator",
        login_method="phone",
        status="pending",
        encrypted_temp_cookies=encrypt_text(
            _dump_temp_state(
                result["cookies"],
                target_account_id=payload.target_account_id,
            )
        ),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return {"session_id": session.id, "status": session.status, "message": result.get("message", "sent")}


@router.post("/creator/phone/confirm")
def creator_phone_confirm(
    payload: PhoneConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter: XhsCreatorLoginAdapter = Depends(get_creator_login_adapter),
):
    return _confirm_phone_login(payload, current_user, db, adapter, "creator", None)


def _confirm_phone_login(
    payload: PhoneConfirmRequest,
    current_user: User,
    db: Session,
    adapter,
    sub_type: str,
    creator_adapter: XhsCreatorLoginAdapter | None,
):
    session = db.get(LoginSession, payload.session_id)
    if (
        session is None
        or session.user_id != current_user.id
        or session.sub_type != sub_type
        or session.login_method != "phone"
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Login session not found")
    try:
        state = _load_temp_state(decrypt_text(session.encrypted_temp_cookies))
        result = adapter.confirm_phone_login(payload.phone, payload.code, state.cookies)
        user_info = adapter.get_user_info(result["cookies"])
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Phone login failed") from exc

    # 续期目标：**payload 优先、发码时写进的 temp state 兜底**（与 sync_creator 同形，§18.16）。
    target_account_id = (
        payload.target_account_id
        if payload.target_account_id is not None
        else state.target_account_id
    )
    sync_creator = payload.sync_creator if payload.sync_creator is not None else state.sync_creator
    session.status = "confirmed"
    session.encrypted_temp_cookies = encrypt_text(
        _dump_temp_state(
            result["cookies"],
            sync_creator=sync_creator,
            target_account_id=target_account_id,
        )
    )

    if target_account_id:
        # **续期分支**（设计 13 §18.16，2026-09-23）：手机验证码也能补登了。
        # 只把这一个账号的对应通道换掉 —— 不走 upsert、不派生 Creator
        # （两者都会建/改别的账号行，§18.8 决定 ③A），与扫码续期同一条
        # `_renew_account_from_login`（身份核对 + 按 jar 形态判通道 + 只写一个信封）。
        #
        # 登录本身已成功（session 已 confirmed），但续期失败时**一个字节都没写** ——
        # 所以失败走 400 + 结构化 `{code, message}`，界面照实显示真原因。
        account, renewed_channel, renew_error = _renew_account_from_login(
            db=db,
            current_user=current_user,
            target_account_id=target_account_id,
            user_info=user_info or {},
            cookies=result["cookies"],
        )
        if account is None:
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=renew_error,
            )
        db.commit()
        return {
            "session_id": session.id,
            "status": session.status,
            "account": serialize_account(account, "renewed"),
            "creator_account": None,
            "creator_sync_error": None,
            "renewed_channel": renewed_channel,
            "renew_error": None,
        }

    account, action = _create_account_from_login(
        db=db,
        user_id=current_user.id,
        sub_type=sub_type,
        user_info=user_info,
        cookies=result["cookies"],
    )
    creator_account_payload = None
    creator_sync_error = None
    if sub_type == "pc" and creator_adapter is not None and sync_creator:
        creator_account_payload, creator_sync_error = _sync_creator_account_from_pc_login(
            db=db,
            user_id=current_user.id,
            pc_cookies=result["cookies"],
            creator_adapter=creator_adapter,
        )
    db.commit()
    return {
        "session_id": session.id,
        "status": session.status,
        "account": serialize_account(account, action),
        "creator_account": creator_account_payload,
        "creator_sync_error": creator_sync_error,
        "renewed_channel": "",
        "renew_error": None,
    }
