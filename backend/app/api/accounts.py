from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.adapters.xhs.creator_login_adapter import XhsCreatorLoginAdapter
from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter
from backend.app.adapters.xhs.qianfan_api_adapter import (
    get_qianfan_api_adapter_factory,  # 登录态探针（L3）要能被打桩：走 FastAPI 依赖注入
)
from backend.app.core.account_types import (
    SUB_TYPE_CREATOR,
    SUB_TYPE_PC,
    SubTypeNotSupportedError,
    UnknownSubTypeError,
    normalize_sub_type,
)
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user, get_owned_active_account
from backend.app.core.time import shanghai_now
from backend.app.models import AccountCookieVersion, PlatformAccount, User
from backend.app.schemas.common import paginated
from backend.app.schemas.accounts import AccountListResponse
from backend.app.services.account_service import (
    CHANNEL_ARK,
    CHANNEL_CREATOR,
    CHANNEL_WWW,
    account_profile_from_user_info,
    channel_for_cookies,
    channel_jars,
    cookie_header_from_text,
    decode_cookie_text,
    enrich_user_info_with_xhs_self_profile,
    fetch_account_metrics,
    get_xhs_self_profile_adapter,
    has_web_session,
    latest_cookies_for_channel,
    METRICS_FETCH_ERROR_REASON,
    merge_metrics_into_profile,
    NO_COOKIE_METRICS_REASON,
    serialize_account,
    store_channel_cookies,
    upsert_platform_account_from_login,
    XhsSelfProfileAdapter,
)
from backend.app.services import login_state_service
from xhs_utils.cookie_util import trans_cookies

router = APIRouter(prefix="/accounts", tags=["accounts"])

logger = logging.getLogger(__name__)


class CookieImportRequest(BaseModel):
    platform: str = Field(pattern="^xhs$")
    sub_type: str = Field(pattern="^(pc|creator)$")
    cookie_string: str = Field(min_length=3)
    sync_creator: bool = False
    #: **第二条令牌**（§11.5 / v1.24）：可选的千帆/创作者平台 Cookie。
    #: www 与 ark 是两套互不通用的令牌（真机实测），一次导入两份 ⇒ 库里只留
    #: **一个账号**却同时能打网页版与千帆/创作者平台的接口。
    #: 留空时行为与从前完全一致（只落 `cookie_string` 那一条通道）。
    ark_cookie_string: str = ""


def get_pc_account_adapter() -> XhsPcLoginAdapter:
    return XhsPcLoginAdapter()


def get_creator_account_adapter() -> XhsCreatorLoginAdapter:
    return XhsCreatorLoginAdapter()


def get_creator_api_adapter_factory():
    from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter

    return XhsCreatorApiAdapter


# 账号资料取数口（`XhsSelfProfileAdapter` / `get_xhs_self_profile_adapter`）住在
# `services/account_service.py`，本模块从上面 import 后再导出——理由见那里的类注释：
# 原先本模块与 `login_sessions.py` 各自内联一份取数逻辑，形状还不一致，
# 结果身份增强在一处静默失效（DEF-039 同族）。


def _select_adapter(sub_type: Optional[str], pc_adapter: XhsPcLoginAdapter, creator_adapter: XhsCreatorLoginAdapter):
    """按协议链路（``sub_type``）选择登录适配器。

    故意写成**显式枚举 + raise**，而不是 ``... else pc_adapter``：
    ``sub_type`` 决定请求域名与 Cookie 域，用 ``else`` 兜底会让将来新增的
    协议链路静默走 pc 分支，错误要到 HTTP 层才暴露（T-102）。

    ⚠️ **千帆（ark）链路**由 :func:`normalize_sub_type` 提前抛
    :class:`SubTypeNotSupportedError`（已知但无适配器）—— 那个是**业务结果**，
    调用方该给 400 + 可读原因；本函数不把它当成「未知取值」。
    """
    normalized = normalize_sub_type(sub_type)
    if normalized == SUB_TYPE_CREATOR:
        return creator_adapter
    if normalized == SUB_TYPE_PC:
        return pc_adapter
    # 只有在 account_types.SUB_TYPES 新增取值却忘记在此补分支时才会到达。
    raise UnknownSubTypeError(
        f"sub_type={normalized!r} 未在 _select_adapter 中显式分支；"
        "请同步更新本函数（禁止 else 兜底）。"
    )


def _sync_creator_account_from_pc_cookie(
    *,
    db: Session,
    user_id: int,
    platform: str,
    cookie_string: str,
    creator_adapter: XhsCreatorLoginAdapter,
    account: Optional[PlatformAccount] = None,
) -> str | None:
    """Best-effort Creator-account sync from an imported PC cookie.

    Returns an error message (``None`` on success). Mirrors the QR-login path:
    a failure must not fail the import, but it must be observable — the old
    ``except Exception: return None`` made "synced" and "did nothing" identical
    from the outside (see the QR-login regression of the same date).

    **§11.5（v1.24）起不再新建第二行账号**：派生出来的创作者 Cookie 挂到**同一个
    账号**的 ``ark`` 通道上（创作者平台与千帆同属 customer 单点登录，实测可互换）。
    从前这里按 ``sub_type="creator"`` 再 upsert 一次，同一个身份在库里留下两行 ——
    那正是「一个账号两条令牌」要消灭的形态。
    """
    try:
        creator_result = creator_adapter.exchange_from_user_cookies(trans_cookies(cookie_string))
        creator_cookies_text = json.dumps(creator_result["cookies"], ensure_ascii=False, separators=(",", ":"))
        creator_user_info = creator_adapter.get_user_info(creator_result["cookies"])
        if account is not None:
            store_channel_cookies(
                db=db,
                account=account,
                cookies_text=creator_cookies_text,
            )
        else:
            upsert_platform_account_from_login(
                db=db,
                user_id=user_id,
                platform=platform,
                sub_type="creator",
                user_info=creator_user_info,
                cookies_text=creator_cookies_text,
            )
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not swallowed
        logger.warning("[accounts] creator sync from imported PC cookie failed: %s", exc)
        return str(exc) or exc.__class__.__name__
    return None


def _get_owned_active_account(db: Session, current_user: User, account_id: int) -> PlatformAccount:
    """（薄别名）**唯一实现已收敛到 `core/deps.py::get_owned_active_account`**（DEF-055）。

    本函数曾经是全项目**唯一**带软删闸的副本，另外三份（`qianfan._owned_account` /
    `notes._get_owned_account` / `pc` 取通道函数 / `crawl` 内联校验）都漏了 `deleted_at`
    —— 于是「账号删了但仍能发起同步 / 建绑定 / 借会话」在多数入口上层拦不住。
    现在只留一份实现，保留本名是为了不改动本文件已有的 11 处调用点。
    """
    return get_owned_active_account(db, current_user, account_id)


@router.get("", response_model=AccountListResponse)
def get_accounts(
    platform: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """账号列表。**每张卡片都带上 `login_channels`**（设计 13 §18.5 的 L1/L2）。

    带上它而不是让前端逐卡片再问一次，有两个理由：

    ① **零平台请求**：L1/L2 是纯本地判据（有没有那一行 + jar 里有没有那两把钥匙），
       列表本来就该一次给全 —— 前端逐卡片发请求会变成 N 次往返，
       而其中每一次的答案其实都躺在同一次查询里；
    ② **不新增「第二个真相源」**：卡片上的登录态与账号本身来自**同一次响应**，
       不会出现「列表已经刷新、登录态还是上一轮」的错位。

    L3（真实可用性）**不在这里**：它要发只读探针，只随**页面动作**发生 ——
    进入 / 刷新账号页时批量探活（`probe=True`，v1.30 起由用户要求；v1.31 起
    「检查」按钮已移除，探活不再有第二个手工入口）。
    """
    # 软删行不下发（I3）：它们只是「身份占位」，存在的意义是让唯一索引成立、让重新导入复活。
    statement = select(PlatformAccount).where(
        PlatformAccount.user_id == current_user.id,
        PlatformAccount.deleted_at.is_(None),
    )
    if platform:
        statement = statement.where(PlatformAccount.platform == platform)
    accounts = db.scalars(statement.order_by(PlatformAccount.created_at.desc())).all()
    # 一次查询算完所有卡片的 L1/L2（探针关闭 = 零平台请求）。
    login_states = login_state_service.login_states_for_accounts(db, accounts, probe=False)
    payload = []
    for account in accounts:
        item = serialize_account(account)
        item["login_channels"] = login_states.get(account.id, {}).get("channels", {})
        payload.append(item)
    return paginated(payload, page, page_size)


class LoginStateRequest(BaseModel):
    #: 是否**真的去打一次只读探针**（L3）。默认关闭 —— 「看一眼卡片」不该产生平台请求。
    probe: bool = False


@router.post("/{account_id}/login-state")
def account_login_state(
    account_id: int,
    payload: LoginStateRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    self_profile_adapter: XhsSelfProfileAdapter = Depends(get_xhs_self_profile_adapter),
    qianfan_adapter_factory=Depends(get_qianfan_api_adapter_factory),
    creator_adapter_factory=Depends(get_creator_api_adapter_factory),
):
    """**账号维度**的登录态（设计 13 §18.5 / 2026-09-21 三平台拆分）。

    `probe=false`：只给 L1/L2，**零平台请求**（等价于列表里那一份，但可单独取）。
    `probe=true`：对「已经是登录态」的平台各发 **1 次只读探针**，回答「**还能不能用**」。

    ## 为什么需要独立于 `POST /qianfan/health-check`

    那个端点是**绑定维度**的：账号没绑千帆店铺时直接 404。而 §11.6 之后
    「**有 ark 平台但没绑店**」是一种**合法且正在推广的形态**（一次导入两份 Cookie
    的账号根本不需要绑千帆）—— 此时千帆登录态**问不出来**，这是个真实的契约缺口，
    不是实现 bug。本端点按**账号 + 平台**回答，与有没有绑店无关（千帆还会退回
    绑定表的令牌，见 `shop_binding_service.qianfan_cookies_for_account`）。

    ## 边界（§18.9）

    * **只读**：`/v2/user/me`、`/api/galaxy/user/info`、`/api/edith/seller/info/v2`，
      不发任何写请求；
    * **不落库**：L3 的结论只影响本次响应，**不删改**任何存量令牌，
      也**不动**任何存量令牌（探活失败 ≠ 可以毁数据）。
      （原句为「也不动店铺关系与任何存量令牌」—— `shop_id_fk` 已随 ADR-021 退场；
      更早的原句「也不清空 `shop_account_bindings`」—— 该表已于 2026-09-22 整表移除。
      现在要保护的是账号的 `ark` 通道与 `shop_state`。）
    * **软删账号不给**：`get_owned_active_account` 拦（同 DEF-055 口径）。
    """
    account = _get_owned_active_account(db, current_user, account_id)
    payload = payload or LoginStateRequest()
    states = login_state_service.login_states_for_accounts(
        db,
        [account],
        probe=payload.probe,
        profile_adapter=self_profile_adapter,
        qianfan_adapter_factory=qianfan_adapter_factory,
        creator_adapter_factory=creator_adapter_factory,
    )
    result = states.get(account.id) or {"channels": {}, "checked_at": None}
    return {
        "platform_account_id": account.id,
        "probe": payload.probe,
        **result,
    }


class AccountDisabledRequest(BaseModel):
    """启用 / 停用账号（2026-10-27）。**只有这一个字段** —— 它是用户唯一可控的开关。"""

    disabled: bool


class QianfanCookieRequest(BaseModel):
    """千帆令牌续期（用户 2026-09-21 要求「千帆也能扫码登录」的现实落点）。

    ⚠️ **千帆没有扫码口**：千帆登录态的末步是 `POST /ark/api/edith/open/ssologin`，
    那个请求带 `X-s` / `X-t` 签名 —— 服务端复现它等于**登录逆向 + 长期维护签名**，
    违反只读边界 / C4（与「合并千帆绑定」当年判不可交付同因，§4.10.9）。
    所以这里做**一步粘贴**：在弹窗里粘贴、当场只读校验、通过即落库。
    """

    cookie_string: str = Field(min_length=3)


@router.post("/{account_id}/qianfan-cookie")
def renew_qianfan_cookie(
    account_id: int,
    payload: QianfanCookieRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    qianfan_adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """给**已有账号**续期千帆（ark 平台）令牌：粘贴 → 只读校验 → 落库。

    本端点是**账号维度** —— 只把令牌写进账号的 `ark` 通道，**不碰其他任何数据**
    （令牌是账号自己的凭据，续期不该顺手动别的东西，§18.4）。ADR-021 起店铺实体
    与关系一并退场，更不存在「顺手动店铺关系」这回事。
    （原句为「与 `POST /qianfan/shops/bind` 的分工：那个是**关系维度**（顺带写账号的
    `shop_id_fk` / `shop_state`）」与「不碰店铺关系」—— `shop_id_fk` / 绑店动作
    均已退场；更早的原句「顺带建/改 `shop_account_bindings` 行」「不碰绑定表」
    —— 该表已于 2026-09-22 整表移除。**「只写令牌、不顺手动别的」这条纪律不变**。）

    校验用**只读**的 `get_seller_info()`：通过才落库，不通过就把平台原话带回前端，
    绝不「先存了再说」。
    """
    account = _get_owned_active_account(db, current_user, account_id)
    cookies = payload.cookie_string.strip()
    adapter = qianfan_adapter_factory(cookies)
    success, message, _payload = adapter.get_seller_info()
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=message or "千帆登录态校验失败，请确认复制的是千帆后台（ark.xiaohongshu.com）的 Cookie",
        )
    store_channel_cookies(db=db, account=account, cookies_text=cookies)
    db.commit()
    db.refresh(account)
    return serialize_account(account)


@router.post("/login-states")
def accounts_login_states(
    payload: LoginStateRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    self_profile_adapter: XhsSelfProfileAdapter = Depends(get_xhs_self_profile_adapter),
    qianfan_adapter_factory=Depends(get_qianfan_api_adapter_factory),
    creator_adapter_factory=Depends(get_creator_api_adapter_factory),
):
    """**一批账号**的登录态（账号矩阵页「进入/刷新自动检查」用，v1.30）。

    为什么要有批量版：自动检查要对**每张卡片**的各平台探活。逐账号调
    `POST /{id}/login-state` 会发出 N 次请求，而它们的答案躺在同一次查询里
    （列表端点本来就要算 L1/L2）—— 一个入口一次问完，前端也只发一个请求。

    `probe=true` 时对每个「已经是登录态」的平台各打 1 次只读探针；请求数上限
    = 账号数 × 3（www / creator / ark），**不进轮询、不进定时任务**（§18.9.5 风控）。
    """
    payload = payload or LoginStateRequest()
    accounts = db.scalars(
        select(PlatformAccount).where(
            PlatformAccount.user_id == current_user.id,
            PlatformAccount.deleted_at.is_(None),
        )
    ).all()
    states = login_state_service.login_states_for_accounts(
        db,
        accounts,
        probe=payload.probe,
        profile_adapter=self_profile_adapter,
        qianfan_adapter_factory=qianfan_adapter_factory,
        creator_adapter_factory=creator_adapter_factory,
    )
    return {
        "probe": payload.probe,
        "accounts": [
            {"platform_account_id": account.id, **(states.get(account.id) or {"channels": {}, "checked_at": None})}
            for account in accounts
        ],
    }


@router.post("/import-cookie")
def import_cookie(
    payload: CookieImportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    pc_adapter: XhsPcLoginAdapter = Depends(get_pc_account_adapter),
    creator_adapter: XhsCreatorLoginAdapter = Depends(get_creator_account_adapter),
    self_profile_adapter: XhsSelfProfileAdapter = Depends(get_xhs_self_profile_adapter),
):
    adapter = _select_adapter(payload.sub_type, pc_adapter, creator_adapter)
    try:
        user_info = adapter.get_user_info(trans_cookies(payload.cookie_string))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cookie is invalid or expired") from exc

    if payload.sub_type == "pc":
        # 身份字段与互动计数来自**两个不同的平台接口**：`/v2/user/me` 有身份
        # 但**没有** interactions，`/v1/user/otherinfo` 才有计数（DEF-039）。
        try:
            header = cookie_header_from_text(payload.cookie_string)
            self_profile = self_profile_adapter.get_self_profile(header)
            metrics, _reason = fetch_account_metrics(
                self_profile_adapter,
                cookies=payload.cookie_string,
                sub_type=payload.sub_type,
                external_user_id=str(user_info.get("external_user_id") or ""),
            )
            user_info = enrich_user_info_with_xhs_self_profile(user_info, self_profile, metrics=metrics)
        except Exception as exc:  # noqa: BLE001 - 可选步骤：不阻塞建号，但必须留痕
            logger.warning("[accounts] self profile enrich skipped: %s", exc)

    account, action = upsert_platform_account_from_login(
        db=db,
        user_id=current_user.id,
        platform=payload.platform,
        sub_type=payload.sub_type,
        user_info=user_info,
        cookies_text=payload.cookie_string,
    )
    # §11.5：一次登录可以拿**两份** Cookie（www + 千帆/创作者）。第二份直接挂到同一个账号上，
    # 不新建行 —— 「一个账号、多平台令牌」就是这样落库的。落到哪个平台**按 jar 形态判定**
    # （2026-09-21 拆分）：粘贴千帆后台 Cookie → `ark`；粘贴创作者平台 Cookie → `creator`。
    ark_stored = False
    ark_channel = ""
    if payload.ark_cookie_string.strip():
        ark_channel = channel_for_cookies(payload.ark_cookie_string.strip())
        store_channel_cookies(
            db=db,
            account=account,
            cookies_text=payload.ark_cookie_string.strip(),
        )
        ark_stored = True
    if payload.sub_type == "pc" and payload.sync_creator:
        creator_sync_error = _sync_creator_account_from_pc_cookie(
            db=db,
            user_id=current_user.id,
            platform=payload.platform,
            cookie_string=payload.cookie_string,
            creator_adapter=creator_adapter,
            account=account,
        )
    else:
        creator_sync_error = None
    db.commit()
    db.refresh(account)
    return {
        **serialize_account(account, action),
        "creator_sync_error": creator_sync_error,
        "ark_channel_stored": ark_stored,
        #: 第二份 Cookie 实际落到哪个平台（按 jar 形态判定）：`ark`=千帆 / `creator`=创作者平台。
        #: 前端据此提示「这份 Cookie 已识别为创作者平台令牌」，避免用户以为粘错了地方。
        "ark_channel": ark_channel,
    }


def _account_cookies_by_sub_type(db: Session, account: PlatformAccount) -> str:
    """账号维度操作（检查 / 同步数据）取哪个平台的令牌。

    首选平台按 **sub_type**（协议链路）：pc → `www`，creator → `creator`。
    2026-09-21 拆分后一个账号可能只存了其中一两个平台的令牌，所以首选平台
    缺失时**退回其它平台**（能拿到一个可用的总比直接 400 好）；一个都没有才
    返回空串，由调用方给出可读原因。

    ⚠️ 刻意不按「最新一版」猜：拆分后最新一行信封可能只含本次登录碰过的那个
    平台，拿它去猜就会拿创作者 jar 打网页版接口（或反过来）。

    ⚠️ **互动数据（粉丝/关注/获赞）不要用本函数**（v1.31，设计 12 §21.4）：
    它按 sub_type 偏好 creator 通道，而计数**只存在于网页版** —— 带 www 会话的
    creator 账号会被它领到取不到计数的通道上。互动数据一律走
    :func:`_www_cookies_for_metrics`。

    ⚠️ **千帆（ark）链路**（2026-10-11）：`normalize_sub_type` 对它抛
    `SubTypeNotSupportedError` —— 但本函数的语义是「**随便给我一份能用的令牌**」，
    千帆账号的 `ark` 令牌正是那份令牌。所以这里**必须**单独接住，回落到
    `CHANNEL_ARK`；漏接就是 500（账号 9 的现场）。
    """
    from backend.app.core.account_types import (
        SUB_TYPE_CREATOR,
        SubTypeNotSupportedError,
        normalize_sub_type,
    )

    try:
        preferred = (
            CHANNEL_CREATOR
            if normalize_sub_type(account.sub_type) == SUB_TYPE_CREATOR
            else CHANNEL_WWW
        )
    except SubTypeNotSupportedError:
        # 千帆链路：唯一可能存在的通道就是 `ark`。
        preferred = CHANNEL_ARK
    cookies = latest_cookies_for_channel(db, account.id, preferred)
    if cookies:
        return cookies
    for jar in channel_jars(db, account.id).values():
        return jar
    return ""


def _www_cookies_for_metrics(db: Session, account: PlatformAccount) -> str:
    """互动数据（粉丝 / 关注 / 获赞）的取数令牌：**一律 www 通道**（v1.31）。

    用户 2026-09-21 口径：「粉丝关注获赞数均应来之网页端走网端接口」——
    计数只存在于网页版接口（`/v1/user/otherinfo`，edith），所以选令牌时
    **不看 sub_type**：creator 账号带上 www 会话同样取得到（真实库 acct 8
    实测两个通道并存），没有 www 会话就由 `fetch_account_metrics` 零平台请求
    地给出可读原因。

    **不做跨通道兜底**：拿 creator / ark 的 jar 打网页版接口只会被平台拒绝，
    兜底回来还是要报「不含网页会话」—— 那条兜底分支在任何真实输入下都不会
    产生不同结果（DEF-048 定论：答不上来「什么输入会走到它」就是死代码）。
    """
    return latest_cookies_for_channel(db, account.id, CHANNEL_WWW)


@router.post("/{account_id}/check")
def check_account(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    pc_adapter: XhsPcLoginAdapter = Depends(get_pc_account_adapter),
    creator_adapter: XhsCreatorLoginAdapter = Depends(get_creator_account_adapter),
    creator_api_adapter_factory=Depends(get_creator_api_adapter_factory),
    self_profile_adapter: XhsSelfProfileAdapter = Depends(get_xhs_self_profile_adapter),
):
    account = _get_owned_active_account(db, current_user, account_id)

    cookies_text = _account_cookies_by_sub_type(db, account)
    if not cookies_text:
        account.status = "expired"
        account.status_message = "No stored cookie version"
        db.commit()
        db.refresh(account)
        return {
            **serialize_account(account),
            "metrics_reason": NO_COOKIE_METRICS_REASON,
        }

    # ⚠️ **顺序即纪律**（2026-10-11）：取通道令牌**必须**排在
    # `normalize_sub_type` / `_select_adapter` **之前** —— 千帆（ark）链路账号会
    # 在 `normalize_sub_type` 处抛 `SubTypeNotSupportedError`，而它其实可能有可用的
    # `ark` 令牌，「拿不到令牌」才是它真实的、可执行的答案。
    # 原来就是这个顺序，但当时 `qianfan` 被当成「未知取值」抛未捕获异常 ⇒ 500，
    # 提示既不可读、也不指向下一步。
    try:
        normalized_sub_type = normalize_sub_type(account.sub_type)
        adapter = _select_adapter(normalized_sub_type, pc_adapter, creator_adapter)
    except SubTypeNotSupportedError as exc:
        # 已知链路，但**本次操作**它做不了（探活要打 www / creator 域名，千帆只有 `ark`）
        # ⇒ 业务结果，不是故障：把原因写进卡片并给下一步，**不 500**。
        account.status_message = str(exc)
        db.commit()
        db.refresh(account)
        return {
            **serialize_account(account),
            "metrics_reason": exc.code,
        }
    #: 互动数据不可得 / 未取到时的可读原因，随响应回传（前端卡片就地展示）。
    metrics_reason = ""
    try:
        user_info = adapter.get_user_info(decode_cookie_text(cookies_text))
        # 身份（/v2/user/me）与互动计数（/v1/user/otherinfo）是两个接口；
        # 健康检查顺带把三项计数刷新落库（DEF-039）。
        # v1.31：计数与身份增强**一律走 www 通道**（用户口径「来之网页端」）——
        # 不再跟着健康检查的 creator 链路走，否则带 www 会话的 creator 账号
        # 也会被领到取不到计数的通道上（设计 12 §21.4）。
        try:
            metrics_cookies = _www_cookies_for_metrics(db, account)
            header = cookie_header_from_text(metrics_cookies)
            if has_web_session(header):
                self_profile = self_profile_adapter.get_self_profile(header)
                metrics, metrics_reason = fetch_account_metrics(
                    self_profile_adapter,
                    cookies=metrics_cookies,
                    sub_type=normalized_sub_type,
                    external_user_id=str(user_info.get("external_user_id") or ""),
                )
                user_info = enrich_user_info_with_xhs_self_profile(user_info, self_profile, metrics=metrics)
            else:
                # 没有网页会话 ⇒ 零平台请求，原因由 fetch_account_metrics 给
                # （能力优先于类型，sub_type 不参与判定）。
                _metrics, metrics_reason = fetch_account_metrics(
                    self_profile_adapter,
                    cookies=metrics_cookies,
                    sub_type=normalized_sub_type,
                    external_user_id=str(user_info.get("external_user_id") or ""),
                )
        except Exception as exc:  # noqa: BLE001 - 可选步骤：不阻塞健康检查，但必须留痕
            # 卡片上只给固定短原因；异常文本进日志（defensive：服务层已经收口，这里是第二道）
            metrics_reason = METRICS_FETCH_ERROR_REASON
            logger.warning("[accounts] self profile enrich skipped during check: %s", exc)
        account.status = "active"
        account.status_message = ""
        account.nickname = user_info.get("nickname", account.nickname)
        account.avatar_url = user_info.get("avatar_url", account.avatar_url)
        account.external_user_id = user_info.get("external_user_id", account.external_user_id)
        account.profile_json = json.dumps(account_profile_from_user_info(user_info), ensure_ascii=False, separators=(",", ":"))
        account.updated_at = shanghai_now()

        if normalized_sub_type == SUB_TYPE_CREATOR:
            try:
                # Stored cookies may be a Cookie header or a JSON object
                # (QR/phone login stores JSON); XHSCreatorAuth parses the
                # header form, so normalize first -- same contract as the
                # creator publish routes' _cookies_to_string.
                creator_cookies_text = cookie_header_from_text(cookies_text)
                creator_api_adapter = creator_api_adapter_factory(creator_cookies_text)
                permit_ok, permit_msg = creator_api_adapter.check_upload_permit("image")
                if not permit_ok:
                    account.status = "expired"
                    account.status_message = f"上传凭证获取失败: {permit_msg}"
            except Exception as upload_exc:
                account.status = "expired"
                account.status_message = f"上传凭证验证异常: {upload_exc}"
    except Exception as exc:
        account.status = "expired"
        account.status_message = str(exc)
        account.updated_at = shanghai_now()

    db.commit()
    db.refresh(account)
    return {**serialize_account(account), "metrics_reason": metrics_reason}


# NOTE(T-105): 原 `PATCH /accounts/{account_id}` 已于 2026-09-18 下线。
#
# 原实现恒返回 {"id": ..., "status": "updated"}，**不查库、不落库、无鉴权**
# （缺陷 DEF-007）。它比 404 更危险：调用方会以为更新成功。
#
# 下线而非「实现真实更新」的理由：`PlatformAccount` 上没有**用户可编辑**的字段。
#   · nickname / avatar_url / external_user_id / profile_json —— 由平台侧同步，
#     每次健康检查都会用平台值覆写（见下方 check 路由），本地改名会被冲掉；
#   · status / status_message —— 系统推导（健康检查、Cookie 巡检），不应由用户写；
#   · sub_type —— 协议链路，由绑定方式决定（见 core/account_types.py）。
#
# 若后续确实需要「账号本地别名/停用标记」（矩阵场景下区分数十个账号确有价值），
# 应作为**独立需求**设计：新增专用列（如 local_alias / is_disabled），
# 与平台同步字段物理隔离，避免被健康检查覆写。不要复用本路由的旧契约。


@router.delete("/{account_id}")
def delete_account(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**软删**账号（台账 §4.9 / 不变量 I3）。

    为什么不是物理删：

    * 它名下的 `notes` 是用户的**内容库**（可批量转草稿 / 打标签）。物理删要么连这些数据一起丢，
      要么留下一批指向不存在 id 的孤儿行 —— 两种都违反「不静默删除」。软删让两者都不必发生。
    * 身份行**一直在位** ⇒ 部分唯一索引 `uq_platform_accounts_identity` **永久成立** ⇒
      重新导入同一账号时 `upsert_platform_account_from_login()` 命中它并**复活**
      （`action="restored"`），而不是新建一条 —— 这是「删了再导入又来一条」的根治办法。

    Cookie 版本**照旧物理删除**：它是账号凭据，账号被移除后没有保留理由；
    复活时本来就要重新导入 Cookie。

    幂等：对已软删的账号再调一次返回 200（`already_deleted: true`），不报错。
    """
    account = db.get(PlatformAccount, account_id)
    if account is None or account.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    for cookie_version in db.scalars(
        select(AccountCookieVersion).where(AccountCookieVersion.platform_account_id == account.id)
    ).all():
        db.delete(cookie_version)
    already_deleted = account.deleted_at is not None
    if not already_deleted:
        account.deleted_at = shanghai_now()
        account.updated_at = account.deleted_at
    db.commit()
    return {"id": account_id, "status": "deleted", "soft": True, "already_deleted": already_deleted}


@router.post("/{account_id}/disabled")
def set_account_disabled(
    account_id: int,
    payload: AccountDisabledRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """启用 / 停用账号。

    只改 `disabled` 列，**不碰 `status`**（后者由巡检自动写，混在一起会互相覆盖）。
    停用只影响任务派发，不删数据、不吊销令牌。幂等（设成当前值回 `changed: false`）。
    """
    account = _get_owned_active_account(db, current_user, account_id)
    before = bool(account.disabled)
    account.disabled = bool(payload.disabled)
    if before != account.disabled:
        account.updated_at = shanghai_now()
        db.commit()
        db.refresh(account)
        logger.info(
            "[accounts] %s account %s", "disabled" if account.disabled else "enabled", account.id
        )
    return {**serialize_account(account), "changed": before != account.disabled}


@router.post("/{account_id}/metrics")
def sync_account_metrics(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    self_profile_adapter: XhsSelfProfileAdapter = Depends(get_xhs_self_profile_adapter),
):
    """同步账号级互动数据（粉丝 / 关注 / 获赞）并落库到 `profile_json`。

    与 `/check` 的分工：`/check` 判**账号是否可用**（会改 `status`），本端点只刷**资料数字**，
    **不改状态**。取数口径与 `/check`、`/import-cookie` 共用
    `services.account_service.fetch_account_metrics`（唯一实现）。

    响应 = `serialize_account()` + `metrics_reason`：不可得时给出可读中文原因，
    可得时为 `""`。前端因此能像处理 `/check` 一样**整对象替换**卡片数据。

    **v1.31（2026-09-21 用户口径）**：取数通道一律 **www**（「均应来之网页端走网端
    接口，点击同步数据应走 pc 端获取对应数据」）—— 不再按 `sub_type` 选通道，
    带 www 会话的 creator 账号同样取得到；没有 www 会话时**零平台请求**给原因。
    实测口径（DEF-039）：计数来自 `/v1/user/otherinfo` 的 `data.interactions[]`。
    设计见设计文档 12 §21.4。
    """
    account = _get_owned_active_account(db, current_user, account_id)

    if not channel_jars(db, account.id):
        # 一个 Cookie 版本都没有 ⇒ 「没有已保存的 Cookie 版本」
        # （与「有版本但没有 www 通道」是两回事，后者由 fetch_account_metrics 给原因）
        return {
            **serialize_account(account),
            "metrics_reason": NO_COOKIE_METRICS_REASON,
        }

    metrics, reason = fetch_account_metrics(
        self_profile_adapter,
        cookies=_www_cookies_for_metrics(db, account),
        sub_type=account.sub_type,
        external_user_id=account.external_user_id or "",
    )
    # 只有真的拿到新值才写库：同步失败不得抹掉上一次的好数据（DEF-039）。
    if merge_metrics_into_profile(account, metrics):
        db.commit()
        db.refresh(account)
    return {**serialize_account(account), "metrics_reason": reason}


_NOTE_SYNC_GONE = {
    "code": "NOTE_SYNC_REMOVED",
    "message": (
        "「同步笔记」已移除：笔记改为**实时查询平台**，本地不再保存副本。"
        "列表 / 详情直接读平台即可，不需要也不再允许先同步。"
    ),
}


@router.post("/{account_id}/notes/sync", include_in_schema=False)
def start_account_note_sync(account_id: int, current_user: User = Depends(get_current_user)):
    """**410 墓碑** —— 「同步笔记」整条链路已退场（设计 75 S4，2026-10-27）。

    用户口径（2026-10-26）：「同步商品和同步笔记任务全链路移除」。

    ## 为什么保留端点而不是删掉

    删掉 ⇒ 客户端拿到 **404**，分不清「这个功能没有了」和「路径写错了 / 版本太旧」。
    410 是**明确答复**：功能已移除、不要重试。前端同批已删调用点，这条留给
    旧版页面 / 脚本一个可读的原因，而不是一个裸 404。

    ## 为什么不需要读库

    本端点现在**零参数校验、零数据库访问、零平台请求** —— 它只回答一件事：
    「同步」这个概念不存在了。连账号归属都不用判（不判反而不会泄露「这个 id 存不存在」）。
    """
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_NOTE_SYNC_GONE)


@router.post("/{account_id}/goods-notes/sync", include_in_schema=False)
def start_account_goods_note_sync(account_id: int, current_user: User = Depends(get_current_user)):
    """**410 墓碑** —— 「同步商品笔记」同批退场（设计 75 S4，2026-10-27）。

    商品笔记的**挂链关系**改为实时查（`rt.list_goods_notes`），不再落本地映射表
    （`shop_note_goods` 已随迁移 `n8o9p0q1r2s3` DROP）⇒ 这条「先同步进来」的入口
    失去了意义。理由与上一条完全相同。
    """
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_NOTE_SYNC_GONE)


@router.get("/{account_id}/notes/sync/status", include_in_schema=False)
def account_note_sync_status(account_id: int, current_user: User = Depends(get_current_user)):
    """**410 墓碑** —— 同步已退场，进度查询随之失去查询对象（设计 75 S4）。

    任务本身不再存在（`system.note_sync` 已从注册表摘除），前端也不再轮询；
    保留端点只为让旧版页面拿到「功能已移除」而不是裸 404。
    """
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_NOTE_SYNC_GONE)


@router.get("/{account_id}/goods-notes/sync/status", include_in_schema=False)
def account_goods_note_sync_status(account_id: int, current_user: User = Depends(get_current_user)):
    """**410 墓碑** —— 「同步商品笔记」同批退场（设计 75 S4）。

    商品笔记的挂链关系改为实时查（`rt.list_goods_notes`），本地映射表
    `shop_note_goods` 已随迁移 `n8o9p0q1r2s3` DROP ⇒ 没有「同步进度」可查。
    """
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_NOTE_SYNC_GONE)


# ⚠️ **`POST /accounts/{account_id}/notes/hydrate-pending` 已移除**（2026-09-27 用户口径：
# 「账号信息卡中移除补齐详情逻辑，点击同步笔记时，应获取到当前笔记的所有信息后再落库，
# 在一个任务中完成，不需要再点击补齐详情」）。
#
# 补齐能力并入 `POST /accounts/{account_id}/notes/sync` 自己（设计 13 §35.18：
# `note_sync_service._sync_with_task` 收尾对该账号**全部待补笔记**内联补齐）——
# 于是这条端点连同 `GET /notes/hydrate-queue`、`note_hydrate` 队列一并删除：
# 入口没了还留着链路就是死代码 + 与同步内补齐重复（DEF-044）。
