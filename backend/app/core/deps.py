from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.roles import is_admin, normalize_role
from backend.app.core.security import decode_token
from backend.app.models import PlatformAccount, User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

def get_owned_active_account(
    db: Session,
    current_user: User,
    account_id: int,
    *,
    platform: str | None = None,
    sub_type: str | None = None,
    detail: str = "Account not found",
) -> PlatformAccount:
    """取「属于当前用户**且未被软删**」的账号，否则 404。

    **这是全仓唯一实现**（DEF-055，2026-09-21 收敛）。此前这段判断被抄了 **4 份**
    （`qianfan._owned_account` —— 一家就有 11 个调用点、`notes._get_owned_account`、
    `pc` 的取通道函数、`crawl` 的内联校验），**只有 `accounts.py` 那份带软删闸**；
    于是「账号已删除，但仍能发起商品同步 / 建绑定 / 借会话」在四分之三的入口上都拦不住，
    而且全程静默。同一语义多处实现 = 一处对一处错，本项目已反复踩过（DEF-044 / DEF-047）。

    为什么必须逐个端点都过闸：账号删除是**软删**（台账 §4.9 / I3）—— 卡片会立刻从列表消失，
    但用户手里可能还留着**旧页面上的 id**（另一个标签页、刚点开的下拉）。
    **只过滤列表是不够的**，「看不见」不等于「不能用」。

    故意不区分「不存在」与「不属于你」：避免越权探测（与既有行为一致）。

    :param platform: 传了就额外要求平台相符（如 `"xhs"`）。
    :param sub_type: 传了就额外要求协议链路相符（如 `"pc"`）。
    :param detail: 404 的文案（历史调用点用词不一，如 `Platform account not found`；
        收敛实现**不改既有响应形状**，只补软删闸）。
    """
    not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    account = db.get(PlatformAccount, account_id)
    if account is None or account.user_id != current_user.id or account.deleted_at is not None:
        raise not_found
    if platform is not None and account.platform != platform:
        raise not_found
    if sub_type is not None and account.sub_type != sub_type:
        raise not_found
    return account


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    payload = decode_token(token)
    if payload.get("token_type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.get(User, payload["user_id"])
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # DEF-022：令牌版本不匹配 ⇒ 已被撤销（改密 / 登出 / 强制下线）。
    # 缺 `ver` 的老令牌按版本 0 处理（向后兼容），但版本一旦被抬到 >0 就全部失效。
    if int(payload.get("ver") or 0) != int(user.token_version or 0):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """要求管理员身份（T-104）。

    **使用场景很窄**：只用于会影响所有用户的「全局级」操作，例如
    ``scheduler.enabled`` 这类系统设置。

    普通业务接口**不要**加这个依赖——它们是按 ``user_id`` 做数据隔离的，
    加角色校验只会让普通用户无法使用自己的数据。
    """
    if not is_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"需要管理员权限（当前角色：{normalize_role(current_user.role)}）",
        )
    return current_user
