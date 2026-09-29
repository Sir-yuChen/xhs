"""运行时配置中心（设计 10 §7，2026-09-27）。

## 为什么需要它

`core/config.py::Settings` 是**启动期**配置（YAML / env + `lru_cache`），语义是「进程
生命周期内不变」。而有一类参数必须**运行期能改且立即生效**：平台限流额度、同步并行度。

改造前的真实缺陷（设计 13 §38.1）：`crawl_rate_limit_per_minute` 在
`config/default.yaml` 声明 60，但 `rate_limiter.get_rate_limiter()` 把 5 **写死**在
构造参数里（优先级「构造参数 > settings > 5」⇒ 构造参数赢）⇒ **配置声明了却读不到**，
1867 篇欠账因此要跑 6.2 小时。这正是设计 10 §1 定义的管理缺陷：
「只存在于配置文件、UI 无入口的开关」。

## 三层取值（优先级从高到低）

1. `system_settings` 行（本表，运行期、按用户、页面可改、**立即生效**）；
2. `get_settings()` 的字段（YAML / env，启动期基线）；
3. 内置兜底常量（`BUILTIN_DEFAULTS`）。

## 缓存（设计 10 §5.1 的 **P-3**，本次一并结清）

**写后立即失效**是主路径：页面改完，**同进程内下一次读就是新值**（不重启、不等 TTL）。
TTL（`CACHE_TTL_SECONDS`）只是兜底 —— 覆盖「多进程 / 手工改库」这类失效通知到不了的场景。
⚠️ 刻意**不复用** `get_settings()` 的 `lru_cache`：两者生命周期语义相反，混用就会造出
「改了不生效」的时间窗（P-3 警告的怪现象）。
"""

from __future__ import annotations

import json
import logging
import time
from threading import Lock
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.models.system_setting import (
    CATEGORY_FEATURE,
    VALUE_TYPE_BOOL,
    VALUE_TYPE_FLOAT,
    VALUE_TYPE_INT,
    VALUE_TYPE_JSON,
    VALUE_TYPE_STR,
    VALUE_TYPES,
    SystemSetting,
)

logger = logging.getLogger(__name__)

#: 缓存兜底 TTL（秒）。主路径是**写后立即失效**，这里只为「别的进程改了库」兜底。
CACHE_TTL_SECONDS = 3.0

#: key 长度上限（与模型列宽一致；超长直接拒，不让 DB 抛）
KEY_MAX_LENGTH = 128

#: **已接入运行时读点**的键（设计 10 §7.5）。
#:
#: ⚠️ 只有在这里的键，改了才**真的立即生效**；其余键（含用户自定义）只是**存档**，
#: UI 必须显式标注「仅存档」—— 否则就是让用户以为改了有用（硬约束⑤：不造假）。
#: ⚠️ **只增不减**：新增一个运行时读点就要加一行；漏加 = UI 把「已接线」说成「仅存档」，
#: 同样是负向失真。守卫：`test_app_settings.py::test_live_keys_are_real_read_points`。
LIVE_KEYS: dict[str, str] = {
    "crawl_rate_limit_per_minute": "rate_limiter.CrawlRateLimiter.max_per_minute（设计 13 §38.2）",
    "xhs_sync_account_parallelism": "note_sync_service 同步 worker 池大小（设计 13 §38.3）",
    "xhs_sync_account_gap_seconds": "调度轮内账号间停顿",
    "xhs_allow_duplicate_goods_publish": "note_publish_service 商品重复发布门禁（设计 13 §31.5.2）",
}

#: 内置兜底默认值（第 3 层）。**只放 `LIVE_KEYS` 里的键** —— 给没有读点的键编一个默认值
#: 毫无意义（没人读），只会让「改了没反应」更难排查。
#: 数值与 `config/default.yaml` 保持一致（改一处要改两处是有意的冗余：一个是产品基线，
#: 一个是「连配置文件都没有」时的最后防线）。
#: ⚠️ 额度基线 2026-09-27 由 60 下调为 **8**（用户口径，见设计 13 §38.7）—— 实测 60 次/分钟
#: 会让平台踢掉网页会话，随包种子（`config/initial_data/system_settings.json`）同值。
BUILTIN_DEFAULTS: dict[str, Any] = {
    "crawl_rate_limit_per_minute": 8,
    "xhs_sync_account_parallelism": 3,
    "xhs_sync_account_gap_seconds": 10,
    # 商品笔记重复发布开关（设计 13 §31.5.2，2026-09-28 用户口径）：**默认关** =
    # 同一商品已有笔记（已发布 / 正在发布）时不再发布新的，该笔记落「发布失败」。
    "xhs_allow_duplicate_goods_publish": False,
}

#: 宽松布尔文本（`coerce_value` 与 `live_bool` **共用同一份**）——
#: 两处各写一份就会出现「页面存得进去（`yes`），读出来却是默认值」的静默错值。
_TRUE_TEXTS = frozenset({"true", "1", "yes", "on"})
_FALSE_TEXTS = frozenset({"false", "0", "no", "off"})

#: 值类型 → 解析器。`bool` 刻意**不做**「非空字符串即真」的宽松判断：
#: `"false"` 这种文本一律按 `False` 解（否则页面存了 `false` 却是真值 —— 静默错值）。
_DECODERS = {
    VALUE_TYPE_INT: lambda text: int(float(text)),
    VALUE_TYPE_FLOAT: lambda text: float(text),
    VALUE_TYPE_BOOL: lambda text: bool(text),
    VALUE_TYPE_STR: lambda text: str(text),
    VALUE_TYPE_JSON: lambda text: text,
}

#: 数值类键的合理上界（防手滑把限流额度填成 100000 打爆平台）。
#: 键不在表里 ⇒ 不夹（用户自定义键由使用者负责）。
_KEY_BOUNDS: dict[str, tuple[int, int]] = {
    "crawl_rate_limit_per_minute": (1, 600),
    "xhs_sync_account_parallelism": (1, 8),
    "xhs_sync_account_gap_seconds": (0, 600),
}

#: 进程内缓存：`(user_id, key) -> (值, 过期时刻)`。
#:
#: ⚠️ 存的是**过期时刻**而不是写入时刻：不同条目的 TTL 不一样（配置 3 秒、账号归属 60 秒），
#: 存写入时刻就得在读取处按条目类型反推 TTL —— 那种「靠负年龄差绕过去」的写法
#: 读代码的人一定会理解错，而缓存失效的 bug 是最难查的那一类。
#: `user_id=None` 用于「账号归属」「进程级开关」这类与单个用户无关的派生缓存
#: （键名带前缀区分，见 `account_owner_id` / `live_int_process`）。
_cache: dict[tuple[Optional[int], str], tuple[Any, float]] = {}
_cache_lock = Lock()


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #
def invalidate(*, user_id: Optional[int] = None, key: Optional[str] = None) -> None:
    """清缓存。三种粒度：全清（都不传）/ 清某用户 / 清某用户的某个键。

    ⚠️ **写路径必须调用它**（`create_setting` / `update_setting` / `delete_setting`
    内部已调）—— 漏调 = 改了要等 TTL 才生效，正是 P-3 要避免的怪现象。
    """
    with _cache_lock:
        if user_id is None and key is None:
            _cache.clear()
            return
        for cache_key in [k for k in _cache if _match(k, user_id, key)]:
            _cache.pop(cache_key, None)


def _match(cache_key: tuple[Optional[int], str], user_id: Optional[int], key: Optional[str]) -> bool:
    cached_user, cached_key = cache_key
    if user_id is not None and cached_user != user_id:
        return False
    if key is not None and cached_key != key:
        return False
    return True


def _cache_get(cache_key: tuple[Optional[int], str]) -> Any:
    """取缓存（过期 ⇒ `UNSET`）。返回值可能是 `None`（合法值），所以用哨兵区分未命中。"""
    with _cache_lock:
        hit = _cache.get(cache_key)
    if hit is None:
        return UNSET
    value, expires_at = hit
    if time.monotonic() >= expires_at:
        with _cache_lock:
            _cache.pop(cache_key, None)
        return UNSET
    return value


def _cache_put(cache_key: tuple[Optional[int], str], value: Any, *, ttl: float = CACHE_TTL_SECONDS) -> None:
    """写缓存（`ttl` 按条目类型给：配置 3 秒、账号归属 60 秒）。"""
    with _cache_lock:
        _cache[cache_key] = (value, time.monotonic() + max(float(ttl), 0.0))


class _Miss:
    """未命中哨兵（`None` 是合法配置值，不能拿它当「没有」）。"""


UNSET = _Miss()


# --------------------------------------------------------------------------- #
# 值编解码
# --------------------------------------------------------------------------- #
def normalize_value_type(value_type: Any) -> str:
    """值类型归一化；非法 / 缺省 ⇒ `str`（不 500）。"""
    text = str(value_type or "").strip().lower()
    return text if text in VALUE_TYPES else VALUE_TYPE_STR


def infer_value_type(value: Any) -> str:
    """从 Python 值推断类型（新增行的便捷路径；显式给 `value_type` 时以显式为准）。

    ⚠️ `bool` 必须在 `int` **之前**判 —— Python 里 `isinstance(True, int)` 为真，
    顺序写反会把 `true` 存成 `int` 再读成 `1`。
    """
    if isinstance(value, bool):
        return VALUE_TYPE_BOOL
    if isinstance(value, int):
        return VALUE_TYPE_INT
    if isinstance(value, float):
        return VALUE_TYPE_FLOAT
    if isinstance(value, (dict, list)):
        return VALUE_TYPE_JSON
    return VALUE_TYPE_STR


def encode_value(value: Any, value_type: str) -> str:
    """Python 值 → 存储文本（JSON 文本；解码失败由 `decode_value` 兜底）。"""
    kind = normalize_value_type(value_type)
    if kind == VALUE_TYPE_BOOL:
        return json.dumps(bool(value))
    if kind == VALUE_TYPE_INT:
        return json.dumps(int(float(value)))
    if kind == VALUE_TYPE_FLOAT:
        return json.dumps(float(value))
    if kind == VALUE_TYPE_JSON:
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def decode_value(text: Any, value_type: Any) -> Any:
    """存储文本 → Python 值。**坏值不抛**，退回原始文本（缺值口径：不造数据）。

    为什么容忍坏值：一行坏配置不该让整个「平台请求」链路炸 —— 但也不能装作解析成功，
    所以坏值原样返回并在调用方（`live_int` 等）按类型转换时再失败一次。
    """
    kind = normalize_value_type(value_type)
    raw = "" if text is None else str(text)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    decoder = _DECODERS.get(kind)
    if decoder is None:
        return parsed
    try:
        return decoder(parsed)
    except (TypeError, ValueError):
        return parsed


def coerce_value(value: Any, value_type: Any, *, key: str = "") -> Any:
    """入参值 → 按声明类型转换（含边界夹取）。转不动 ⇒ `ValueError`（由 API 转 422）。

    `bool` 的宽松输入只认这几种字符串（`true/false/1/0/yes/no/on/off`）——
    其余一律 `ValueError`，**不猜**（猜错 = 静默把「关」存成「开」）。
    """
    kind = normalize_value_type(value_type)
    if kind == VALUE_TYPE_BOOL:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE_TEXTS:
            return True
        if text in _FALSE_TEXTS:
            return False
        raise ValueError(f"值 {value!r} 不是合法布尔")
    if kind == VALUE_TYPE_INT:
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError):
            raise ValueError(f"值 {value!r} 不是合法整数") from None
        return _clamp(key, number)
    if kind == VALUE_TYPE_FLOAT:
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(f"值 {value!r} 不是合法小数") from None
    if kind == VALUE_TYPE_JSON:
        if isinstance(value, str):
            try:
                json.loads(value)
            except ValueError:
                raise ValueError(f"值 {value!r} 不是合法 JSON") from None
            return value
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def _clamp(key: str, number: int) -> int:
    """按 `_KEY_BOUNDS` 夹取（键不在表里 ⇒ 原样返回）。"""
    bounds = _KEY_BOUNDS.get(key)
    if bounds is None:
        return number
    low, high = bounds
    if number < low:
        return low
    if number > high:
        return high
    return number


# --------------------------------------------------------------------------- #
# 读：三层取值
# --------------------------------------------------------------------------- #
def settings_default(key: str) -> Any:
    """第 2 层：`get_settings()` 的同名字段（YAML / env）。没有该字段 ⇒ `UNSET`。

    ⚠️ 用 `hasattr` 而不是 `getattr(..., None)`：字段存在但值为 `None` 与「没有这个
    字段」是两件事，前者应作为有效值参与优先级。
    """
    from backend.app.core.config import get_settings

    settings = get_settings()
    return getattr(settings, key) if hasattr(settings, key) else UNSET


def effective_value(
    db: Optional[Session], user_id: Optional[int], key: str, *, default: Any = UNSET
) -> Any:
    """三层取值（DB 行 → settings → 内置兜底 → `default`）。

    `db=None` 时**跳过第 1 层**（用于「没有会话也不想开一个」的调用点，如限流器的
    缓存未命中路径自己开会话）。`user_id=None` 同理跳过 DB 层（无归属可查）。
    """
    if db is not None and user_id is not None:
        row = db.scalar(
            select(SystemSetting).where(
                SystemSetting.user_id == user_id, SystemSetting.key == key
            )
        )
        if row is not None:
            return decode_value(row.value, row.value_type)
    from_settings = settings_default(key)
    if from_settings is not UNSET:
        return from_settings
    if key in BUILTIN_DEFAULTS:
        return BUILTIN_DEFAULTS[key]
    return default


def live_value(db: Optional[Session], user_id: Optional[int], key: str, *, default: Any = None) -> Any:
    """带缓存的 `effective_value`（**运行时读点唯一入口**）。

    读路径：缓存命中 → 直接返回；未命中 → 取值（必要时自开一个短会话）→ 写缓存。
    """
    cache_key = (user_id, key)
    cached = _cache_get(cache_key)
    if cached is not UNSET:
        return cached

    value = UNSET
    if db is not None:
        value = effective_value(db, user_id, key, default=default)
    elif user_id is None:
        # 没有归属用户 ⇒ DB 层**结构上不可能命中**（`effective_value` 会跳过它）⇒
        # 连会话都不必开。热路径（限流器）省一次「开会话 + 关会话」。
        value = effective_value(None, None, key, default=default)
    else:
        session = None
        try:
            session = SessionLocal()
            value = effective_value(session, user_id, key, default=default)
        except Exception:  # noqa: BLE001 - 读配置失败不该打断平台请求
            logger.warning("settings: 读取 %s 失败，本次用兜底值", key, exc_info=True)
        finally:
            if session is not None:
                session.close()
    if value is UNSET:
        value = default

    _cache_put(cache_key, value)
    return value


def live_int(db: Optional[Session], user_id: Optional[int], key: str, *, default: int) -> int:
    """`live_value` 的整数版：转不动 / 缺值 ⇒ `default`（不抛）。

    ⚠️ 调用方是**平台请求路径**（限流器），这里抛异常等于让一次同步炸掉 ——
    所以坏值退 `default` 并留一行 warning，绝不向上抛。
    """
    value = live_value(db, user_id, key, default=default)
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("settings: %s 的值 %r 不是整数，本次用 %s", key, value, default)
        return default


def live_bool(db: Optional[Session], user_id: Optional[int], key: str, *, default: bool) -> bool:
    """`live_value` 的布尔版：转不动 / 缺值 ⇒ `default`（不抛）。

    与 `live_int` 对称：调用方是**业务门禁**（商品重复发布开关，设计 13 §31.5.2），
    读配置失败不该让一次发布请求 500 —— 退 `default`（保守侧 = 默认关闭 ⇒ 仍然拦截）
    并留一行 warning，绝不向上抛。

    ⚠️ 宽松文本只认 `_TRUE_TEXTS` / `_FALSE_TEXTS`（与 `coerce_value` **同一份**）：
    `"false"` 必须解成 `False` —— 把「关」读成「开」等于让开关形同虚设（静默错值）。
    """
    value = live_value(db, user_id, key, default=default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_TEXTS:
        return True
    if text in _FALSE_TEXTS:
        return False
    logger.warning("settings: %s 的值 %r 不是布尔，本次用 %s", key, value, default)
    return default


def is_live(key: str) -> bool:
    """该键是否**接进了运行时读点**（UI 的「生效性」列据此渲染）。"""
    return str(key or "").strip() in LIVE_KEYS


def live_int_process(key: str, *, default: int) -> int:
    """**进程级**开关的整数读（如 worker 池大小）。

    与 `live_int` 的区别只在「取哪一行」：账号级开关按**账号归属用户**读（`live_int`），
    而进程级资源（worker 池）不属于任何单个账号 ⇒ 规则是
    **所有用户的该键行取最大值（最宽松者胜）**。

    ⚠️ 为什么取 max 而不是「第一个用户」/「管理员」：进程级资源是共享的，
    取某个特定用户的行会让**另一个用户在页面上改了却不生效** —— 那正是
    设计 10 §1 定义的管理缺陷的翻版。取 max 保证「任何用户在页面上调大，都真的生效」；
    代价是「调低到低于其他用户配置的值时以更宽松的那个为准」（单用户场景下不存在）。
    """
    cache_key = (None, f"process:{key}")
    cached = _cache_get(cache_key)
    if cached is not UNSET:
        return cached

    resolved: Optional[int] = None
    session = None
    try:
        session = SessionLocal()
        rows = session.scalars(
            select(SystemSetting).where(SystemSetting.key == key)
        ).all()
        values: list[int] = []
        for row in rows:
            decoded = decode_value(row.value, row.value_type)
            if isinstance(decoded, bool):
                continue
            try:
                values.append(int(decoded))
            except (TypeError, ValueError):
                continue
        if values:
            resolved = max(values)
    except Exception:  # noqa: BLE001 - 读配置失败退兜底，绝不打断调用方
        logger.warning("settings: 读取进程级配置 %s 失败，本次用兜底值", key, exc_info=True)
    finally:
        if session is not None:
            session.close()

    if resolved is None:
        resolved = live_int(None, None, key, default=default)
    _cache_put(cache_key, resolved)
    return resolved


# --------------------------------------------------------------------------- #
# 账号维度：限流额度按**账号归属用户**读
# --------------------------------------------------------------------------- #
#: 账号归属缓存 TTL（秒）。比配置缓存长：账号换主人是极罕见事件，
#: 而限流器每次请求都要用它（短 TTL 会把「每请求一次查库」变成常态）。
ACCOUNT_OWNER_TTL_SECONDS = 60.0


def account_owner_id(account_id: int) -> Optional[int]:
    """`account_id -> user_id`（带 TTL 缓存；查不到 ⇒ `None`）。

    ⚠️ 为什么限流额度要按**账号归属用户**读，而不是「当前登录用户」：
    额度描述的是「**这个账号**打平台的速率」，账号属于谁就该用谁的配置。若按登录用户读，
    A 用户改了额度会影响 B 用户的账号（跨用户越权生效），而 A 自己账号的额度反而可能
    读不到 —— 两个方向都错。
    """
    cache_key = (None, f"account_owner:{int(account_id)}")
    cached = _cache_get(cache_key)
    if cached is not UNSET:
        return cached

    owner: Optional[int] = None
    session = None
    try:
        from backend.app.models.platform_account import PlatformAccount

        session = SessionLocal()
        owner = session.scalar(
            select(PlatformAccount.user_id).where(PlatformAccount.id == int(account_id))
        )
    except Exception:  # noqa: BLE001 - 归属查不到就退回「不按用户读」
        logger.warning("settings: 账号 %s 归属查询失败", account_id, exc_info=True)
    finally:
        if session is not None:
            session.close()

    # 用独立的 TTL：账号归属不该跟着配置缓存的 3 秒一起过期
    _cache_put(cache_key, owner, ttl=ACCOUNT_OWNER_TTL_SECONDS)
    return owner


def rate_limit_per_minute_for_key(rate_key: str, *, fallback: int = 5) -> int:
    """限流额度（次/分钟）：按**限流键里的账号**找归属用户，读他的运行时配置。

    `rate_key` 形如 `"8:www"` / `"8"` / `"8:goods"`（`rate_limiter` 的既有键形态）。
    解析不出账号（键不是以数字开头）⇒ 不按用户读，直接走 settings / 内置兜底。

    ⚠️ 这是**热路径**（每次平台请求都调），所以：缓存优先、失败退 `fallback`、绝不抛。
    """
    user_id: Optional[int] = None
    prefix = str(rate_key or "").split(":", 1)[0].strip()
    if prefix.isdigit():
        user_id = account_owner_id(int(prefix))
    return live_int(None, user_id, "crawl_rate_limit_per_minute", default=fallback)


# --------------------------------------------------------------------------- #
# 序列化 / 列表
# --------------------------------------------------------------------------- #
def serialize_setting(row: SystemSetting, *, db: Optional[Session] = None) -> dict[str, Any]:
    """一行的 API 形态。`is_live` 是**派生**字段（不落库，见 `LIVE_KEYS`）。"""
    return {
        "id": row.id,
        "key": row.key,
        "value": decode_value(row.value, row.value_type),
        "raw_value": row.value,
        "value_type": row.value_type,
        "category": row.category,
        "label": row.label or row.key,
        "description": row.description or "",
        "requires_restart": bool(row.requires_restart),
        "is_dangerous": bool(row.is_dangerous),
        "is_live": is_live(row.key),
        "live_read_point": LIVE_KEYS.get(row.key, ""),
        "updated_by": row.updated_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_settings(
    db: Session, user_id: int, *, category: Optional[str] = None
) -> list[SystemSetting]:
    """本用户的配置行（按分组 + key 排序，UI 稳定序）。"""
    statement = select(SystemSetting).where(SystemSetting.user_id == user_id)
    text = str(category or "").strip()
    if text:
        statement = statement.where(SystemSetting.category == text)
    return list(
        db.scalars(
            statement.order_by(SystemSetting.category.asc(), SystemSetting.key.asc())
        ).all()
    )


def find_setting(db: Session, user_id: int, key: str) -> Optional[SystemSetting]:
    """按 `(user_id, key)` 取行（归属校验的唯一入口）。"""
    return db.scalar(
        select(SystemSetting).where(
            SystemSetting.user_id == user_id, SystemSetting.key == str(key or "").strip()
        )
    )


# --------------------------------------------------------------------------- #
# 写（增 / 改 / 删）—— 每次都要**立即失效缓存**
# --------------------------------------------------------------------------- #
def create_setting(
    db: Session,
    *,
    user_id: int,
    key: str,
    value: Any,
    value_type: Optional[str] = None,
    category: str = CATEGORY_FEATURE,
    label: str = "",
    description: str = "",
    requires_restart: bool = False,
    is_dangerous: bool = False,
) -> SystemSetting:
    """新增一行（**不 commit**：调用方提交）。key 已存在 ⇒ `ValueError`（API 转 409）。"""
    from backend.app.core.time import shanghai_now

    clean_key = str(key or "").strip()
    if not clean_key:
        raise ValueError("配置键不能为空")
    if len(clean_key) > KEY_MAX_LENGTH:
        raise ValueError(f"配置键最长 {KEY_MAX_LENGTH} 字符")
    if find_setting(db, user_id, clean_key) is not None:
        raise ValueError(f"配置键 {clean_key} 已存在")

    kind = normalize_value_type(value_type) if value_type else infer_value_type(value)
    coerced = coerce_value(value, kind, key=clean_key)
    now = shanghai_now()
    row = SystemSetting(
        user_id=user_id,
        key=clean_key,
        value=encode_value(coerced, kind),
        value_type=kind,
        category=str(category or CATEGORY_FEATURE).strip() or CATEGORY_FEATURE,
        label=str(label or "").strip(),
        description=str(description or "").strip(),
        requires_restart=bool(requires_restart),
        is_dangerous=bool(is_dangerous),
        updated_by=user_id,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    invalidate(user_id=user_id, key=clean_key)
    return row


def update_setting(
    db: Session,
    row: SystemSetting,
    *,
    value: Any = UNSET,
    label: Optional[str] = None,
    description: Optional[str] = None,
    category: Optional[str] = None,
    value_type: Optional[str] = None,
    requires_restart: Optional[bool] = None,
    is_dangerous: Optional[bool] = None,
    updated_by: Optional[int] = None,
) -> SystemSetting:
    """改一行（**不 commit**）。只改传进来的字段（`None` = 不改）。

    ⚠️ 改 `value_type` 时值要**按新类型重新编解码**，否则会留下「类型说 int、文本是
    `"abc"`」的行 —— 读的时候才发现，属于静默错值。
    """
    from backend.app.core.time import shanghai_now

    kind = normalize_value_type(value_type) if value_type else row.value_type
    if value is not UNSET:
        coerced = coerce_value(value, kind, key=row.key)
        row.value = encode_value(coerced, kind)
    elif kind != row.value_type:
        # 只改类型不改值：把现有值按新类型重编码（转不动 ⇒ ValueError，不静默留脏行）
        coerced = coerce_value(decode_value(row.value, row.value_type), kind, key=row.key)
        row.value = encode_value(coerced, kind)
    row.value_type = kind
    if label is not None:
        row.label = str(label).strip()
    if description is not None:
        row.description = str(description).strip()
    if category is not None:
        row.category = str(category).strip() or row.category
    if requires_restart is not None:
        row.requires_restart = bool(requires_restart)
    if is_dangerous is not None:
        row.is_dangerous = bool(is_dangerous)
    if updated_by is not None:
        row.updated_by = updated_by
    row.updated_at = shanghai_now()
    db.flush()
    invalidate(user_id=row.user_id, key=row.key)
    return row


def delete_setting(db: Session, row: SystemSetting) -> dict[str, Any]:
    """删一行（**不 commit**）⇒ 该键**回落到 settings / 内置默认**。

    返回 `{"key", "effective_value"}`：让前端立刻显示回落后的值，不用再发一次 GET
    （删除的语义是「恢复默认」，用户必须看得见恢复成了什么）。
    """
    key = row.key
    user_id = row.user_id
    db.delete(row)
    db.flush()
    invalidate(user_id=user_id, key=key)
    return {"key": key, "effective_value": effective_value(db, user_id, key, default=None)}


# --------------------------------------------------------------------------- #
# 种子（随安装包分发的基线，设计 10 §7.3）
# --------------------------------------------------------------------------- #
def seed_settings_for_user(db: Session, user_id: int) -> list[SystemSetting]:
    """给**一个用户**补种缺失的配置行（幂等：同 `(user_id, key)` 不插、**不覆盖**）。

    「只补缺失、不覆盖」是升级语义的关键：用户在设置页调过的值不会被新版本重置回基线。

    ⚠️ 只种**有运行时读点**的键（`LIVE_KEYS`）：种一批没有读点的行，就是在给
    「改了没反应」制造现场（设计 10 §1 的定义）。
    """
    from backend.app.core.initial_data import load_initial_system_settings

    payload = {row["key"]: row for row in load_initial_system_settings()}
    existing = set(
        db.scalars(select(SystemSetting.key).where(SystemSetting.user_id == user_id)).all()
    )
    created: list[SystemSetting] = []
    for key, fallback in BUILTIN_DEFAULTS.items():
        if key in existing:
            continue
        row = payload.get(key) or {}
        kind = normalize_value_type(row.get("value_type") or infer_value_type(fallback))
        value = row.get("value", fallback)
        try:
            coerced = coerce_value(value, kind, key=key)
        except ValueError:
            logger.warning("settings: 种子 %s 的值 %r 非法，改用内置默认 %r", key, value, fallback)
            kind = infer_value_type(fallback)
            coerced = coerce_value(fallback, kind, key=key)
        from backend.app.core.time import shanghai_now

        now = shanghai_now()
        created.append(
            SystemSetting(
                user_id=user_id,
                key=key,
                value=encode_value(coerced, kind),
                value_type=kind,
                category=str(row.get("category") or CATEGORY_FEATURE),
                label=str(row.get("label") or key),
                description=str(row.get("description") or ""),
                requires_restart=bool(row.get("requires_restart", False)),
                is_dangerous=bool(row.get("is_dangerous", False)),
                updated_by=None,
                created_at=now,
                updated_at=now,
            )
        )
    if created:
        db.add_all(created)
    return created


def ensure_settings(db: Session) -> list[SystemSetting]:
    """为**每个已存在用户**补种（幂等；零用户库 ⇒ 零行）。"""
    from backend.app.models.user import User

    users = db.scalars(select(User).order_by(User.id.asc())).all()
    created: list[SystemSetting] = []
    for user in users:
        created.extend(seed_settings_for_user(db, user.id))
    return created


def seed_builtin_settings_from_initial_data() -> int:
    """应用启动时播种配置行（设计 10 §7.3）。返回新建行数。

    **与任何开关解耦**：设置页打开就该有那批行 —— 播种挂启动，不挂某个子系统的装配
    （与 `scheduler_service.seed_builtin_jobs_from_initial_data` 同型）。

    失败只记 warning 不抛：种子坏掉是可容忍的（用户仍能在设置页手动新建），
    但不能静默。
    """
    db = SessionLocal()
    try:
        created = ensure_settings(db)
        if created:
            db.commit()
            logger.info("应用初始化数据：补种 %s 行运行时配置", len(created))
        return len(created)
    except Exception as exc:  # noqa: BLE001 - 起服务不能被子系统问题拖死
        db.rollback()
        logger.warning("运行时配置播种失败（已跳过，可在设置页手动新建）：%s", exc)
        return 0
    finally:
        db.close()


def reset_cache_for_tests() -> None:
    """清空缓存（测试用；生产不调用）。"""
    invalidate()


def live_keys() -> Iterable[str]:
    """已接入运行时读点的键（升序，供守卫与 UI 参考）。"""
    return sorted(LIVE_KEYS)
