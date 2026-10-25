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

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.roles import ROLE_ADMIN
from backend.app.models.user import User
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
    "xhs_image_upload_parallelism": "媒体上传并发数（商品主图/详情图/规格图共用；设计 24 §22）",
    "xhs_ocr_parallelism": "image_ocr_service 违规词检测（OCR）的**起始**并发数；实际并发按 CPU 核数与目标耗时自动上调（设计 24 §22.9）",
    "xhs_note_image_total_limit": "笔记图片总数上限（生成链路组合总闸；平台硬顶 18，设计 41 §17）",
    "xhs_sku_name_max_chars": "SKU 规格名称最大显示长度（用户口径：30 字 = 60 字符，汉字计 2、ASCII 计 1）",
    "register.enabled": "登录页「注册」入口 + POST /api/auth/register 开关（设计 46 P11；默认 false）",
    "log_retention_days": "record_cleanup 清理 api_logs / business_logs 的保留天数（设计 64 §2.4）",
    "job_log_retention_days": "record_cleanup 清理 scheduled_job_logs 的保留天数（设计 64 §2.4）",
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
    #: 媒体上传并发数（用户口径 2026-10-11：**默认 5**，可在 config 表调整）。
    #: ⚠️ 为什么敢并发：上传是**两段式** —— ① `upload/web/permit` 打小红书（低频，一次）；
    #:   ② PUT 字节打 **ROS/COS 对象存储**（`https://<uploadAddr>/<fileId>`，**不是
    #:   小红书域名**）⇒ 并发第二节**不产生平台请求**，与风控无关。单张 ~2.5s 的耗时
    #:   基本全在 ②，故 N 并发把 38 张从 ~94s 压到 ~19s。
    "xhs_image_upload_parallelism": 8,
    #: 违规词检测（OCR）起始并发数（用户口径 2026-10-13：默认 5）。
    #: ⚠️ 这是**起步值**：`image_ocr_service.resolve_parallelism()` 会按
    #:   待处理图数 / CPU 核数 / 目标总耗时（30s）**自动上调**，配置值是下限。
    #:   引擎 2026-10-13 由 Windows OCR 换为 RapidOCR（单图 0.95s → 3.3s）。
    "xhs_ocr_parallelism": 5,
    #: 笔记图片总数上限（用户口径 2026-10-16：默认 6，可在 config 表调整）。
    #: ⚠️ **平台硬顶 18**（官方 `xhs.postNote`：「图片可以多张（1-18）」）——
    #:    这里的 6 是我们刻意收窄的产品默认，**不是平台限制**；用户改大也不会违反契约，
    #:    但超过 18 会被 `resolve_note_image_limit` 夹住（平台契约优先，见设计 41 §17.3）。
    "xhs_note_image_total_limit": 6,
    #: SKU 规格名称最大**显示长度**（用户口径 2026-10-20：「30 字，60 个字符」）。
    #: 计量口径：**汉字计 2、ASCII 计 1** ⇒ 60 字符 == 30 个汉字。
    #: ⚠️ 口径是「**达到**即优化」：`display_len(name) >= 该值` 就交给大模型压到该值以内。
    "xhs_sku_name_max_chars": 60,
    #: 开放注册（v1.1.0 / 设计 46 §388 + 设计 50 §261，S1）。
    #: **默认 false**：出厂管理员是 `admin/admin123` + `must_change_password=True`，
    #: 若同时开放注册，「弱口令 + 任何人可注册」两条就同时成立。用户在设置页可打开。
    #: 读点：`POST /api/auth/register`（关闭时 403 `REGISTER_DISABLED`，S3 落地）。
    "register.enabled": False,
    #: 日志保留期（设计 64 §2.4）：API 审计 + 业务日志 90 天，定时任务日志 30 天。
    "log_retention_days": 90,
    "job_log_retention_days": 30,
}

#: 每个配置键的**唯一归属级别**（设计 65 §配置三级拆分，2026-10-25 用户口径）：
#: 「系统级只展示系统管理中展示系统公共的，用户级别也是该用户下公共的配置，
#:   工作区域级别的是指定工作区域私有的」。
#:
#: ## 为什么需要这张表（而不是看数据行在哪一级）
#:
#: 三级配置的实现是「一张表三个归属」，所以**同一个键**在三层都可以有行。
#: 若页面靠「有行才显示」来分区，就会出现既混乱又错误的现象：
#: * 某个键系统级有行、用户级也有行 ⇒ 两个页面都列它，用户不知道改哪个；
#: * 某个键三层都没行（用内置默认）⇒ **哪个页面都不显示**，等于不可配置。
#:
#: ⇒ 归属由**代码**定死，页面按这份清单分区展示：
#: * `system`   —— 全站唯一事实（限流额度、并发数、日志保留…）。用户/工作区层
#:                 **不提供覆盖**（覆盖它会让「全站额度」失去意义）；
#: * `user`     —— 只有「每个用户各自成立」的才有意义（如订单同步游标）。
#: * `workspace`—— 某个平台工作区私有（如该工作区是否允许重复发布）。
#:
#: ⚠️ 新增键**必须登记到本表**，否则 `test_settings_scope_map_covers_every_key`
#: 会红 —— 未登记的键不知道该在哪个页面出现，等于又回到「靠数据行猜」。
SETTING_SCOPES: dict[str, str] = {
    # ── 系统级：全站共享、与任何平台无关 ────────────────────────────
    #: 日志保留期：管的是**本系统自己的**表，跟接哪个平台无关 ⇒ 系统级。
    "log_retention_days": "system",
    "job_log_retention_days": "system",
    #: 开放注册是**全站开关**（登录页有无「注册」入口）。
    #: ⚠️ 原实现放在用户级（26 个用户各一行、各改各的），且读取取「管理员那一行为准」
    #: —— 那是「一张表三个归属」早期没定归属时的权宜做法。现在归系统级。
    "register.enabled": "system",
    # ── 用户级：每个用户各自成立 ────────────────────────────────────
    "order_sync_cursor_ms": "user",            # 订单同步游标（每用户各自的店铺）
    # ── 工作区级：**平台口径**，只对某个工作区成立 ──────────────────
    #
    #: ⚠️ 判据（2026-10-25 用户口径「私有配置只在对应工作区域内展示」）：
    #: **凡取值取决于「接哪个平台」的，都是工作区级** —— 即使本系统目前只有
    #: 小红书一个工作区。判据不是"现在有几个平台"，而是"换个平台它是否照用"。
    #:
    #: 这 5 个键原先都放在系统级，理由是「全站唯一事实」。那个理由是错的：
    #: 它们限制的是**对某个平台的调用节奏**，接抖音后必然要各配一份。
    #:
    #: 平台请求限流额度：小红书 www 详情实测 5~8 次/分钟就触发风控
    #:（`crawl_rate_limit_per_minute` 库里值是 8，正是小红书的口径）。
    "crawl_rate_limit_per_minute": "workspace",
    #: 同步 worker 池大小：同时同步**小红书账号**几个。
    "xhs_sync_account_parallelism": "workspace",
    #: 账号间停顿：小红书风控下的账号切换节奏。
    "xhs_sync_account_gap_seconds": "workspace",
    #: 媒体上传并发：小红书图床上传并发。
    "xhs_image_upload_parallelism": "workspace",
    #: 违规词检测并发：小红书违规词 OCR 的并发。
    "xhs_ocr_parallelism": "workspace",
    #: 该工作区是否允许重复发布商品笔记。
    "xhs_allow_duplicate_goods_publish": "workspace",
    #: 笔记图片数上限：**内容线的产品口径**，按工作区（xhs / 未来抖音…）各自不同。
    "xhs_note_image_total_limit": "workspace",
    #: SKU 名称压缩字数：**店铺线**的商品打磨口径，按工作区私有。
    "xhs_sku_name_max_chars": "workspace",
}

#: 键 → 分组。用于「没有行时合成默认行」时的分组归属（按 category 筛选要用）。
#: 与 `config/initial_data/system_settings.json` 的 category 保持一致；
#: 守卫 `test_settings_scope_map_covers_every_key` 一并校验本表覆盖全部键。
_BUILTIN_CATEGORY: dict[str, str] = {
    "crawl_rate_limit_per_minute": "sync",
    "xhs_sync_account_parallelism": "sync",
    "xhs_sync_account_gap_seconds": "scheduler",
    "xhs_image_upload_parallelism": "sync",
    "xhs_ocr_parallelism": "publish",
    "log_retention_days": "feature",
    "job_log_retention_days": "scheduler",
    "register.enabled": "feature",
    "order_sync_cursor_ms": "feature",
    "xhs_allow_duplicate_goods_publish": "publish",
    "xhs_note_image_total_limit": "note",
    "xhs_sku_name_max_chars": "publish",
}

#: 键 → 中文名（合成默认行时用；实际有行时以库里的 label 为准）。
_BUILTIN_LABEL: dict[str, str] = {
    "crawl_rate_limit_per_minute": "平台请求限流额度",
    "xhs_sync_account_parallelism": "同步并行账号数",
    "xhs_sync_account_gap_seconds": "账号间停顿",
    "xhs_image_upload_parallelism": "媒体上传并发数",
    "xhs_ocr_parallelism": "违规词检测并发数",
    "log_retention_days": "API/业务日志保留天数",
    "job_log_retention_days": "定时任务日志保留天数",
    "register.enabled": "开放注册",
    "order_sync_cursor_ms": "订单同步游标",
    "xhs_allow_duplicate_goods_publish": "允许重复发布商品笔记",
    "xhs_note_image_total_limit": "笔记图片总数上限",
    "xhs_sku_name_max_chars": "SKU 名称字数上限",
}


#: 短说明（界面文案，≤20 字）。读 `config/initial_data/setting_descriptions.json`；
#: 读不到 ⇒ 空串（页面上不显示占位文案，比显示一长段技术说明好）。
_SHORT_DESCRIPTIONS: dict[str, str] | None = None


def short_description(key: str) -> str:
    """该键的**界面用**短说明（设计 65：界面只留核心一句话）。"""
    global _SHORT_DESCRIPTIONS
    if _SHORT_DESCRIPTIONS is None:
        from backend.app.core.initial_data import resolve_initial_data_file

        path = resolve_initial_data_file("setting_descriptions.json")
        try:
            _SHORT_DESCRIPTIONS = json.loads(path.read_text(encoding="utf-8")).get(
                "descriptions", {}
            ) if path else {}
        except Exception:  # noqa: BLE001 - 文案读不到不该阻断页面
            _SHORT_DESCRIPTIONS = {}
    return _SHORT_DESCRIPTIONS.get(key, "")


def _default_row(
    key: str, *, scope: str, user_id: Optional[int] = None, workspace: Optional[str] = None
) -> SystemSetting:
    """建一条**不落库**的虚拟行（用内置默认值），用于「该级还没有这个键的行」。

    ⚠️ 不 `db.add()`：它只服务 API 的展示与编辑入口 —— 用户点「保存」时才真正落行。
    让「没配过」的键在页面上可见，是三级拆分的前提（否则它等于不可配置）。

    `id` 用 -1：前端据「id 是否为正」判断该走 POST（新建）还是 PUT（更新）。
    """
    return SystemSetting(
        id=-1,
        user_id=user_id if scope == "user" else None,
        workspace=workspace if scope == "workspace" else None,
        key=key,
        value=str(BUILTIN_DEFAULTS.get(key, "")).lower()
        if isinstance(BUILTIN_DEFAULTS.get(key), bool)
        else str(BUILTIN_DEFAULTS.get(key, "")),
        value_type=(
            "bool" if isinstance(BUILTIN_DEFAULTS.get(key), bool)
            else "int" if isinstance(BUILTIN_DEFAULTS.get(key), int)
            else "string"
        ),
        category=_BUILTIN_CATEGORY.get(key, "general"),
        label=_BUILTIN_LABEL.get(key, key),
        description=short_description(key),
        requires_restart=False,
        is_dangerous=False,
    )


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
    "xhs_ocr_parallelism": (1, 32),
    #: 笔记图片总数（用户口径 2026-10-16）。**上界 = 平台硬顶 18** ——
    #: 官方 `xhs.postNote`：「图片可以多张（1-18）」；夹在这里 = 配置怎么填都不会
    #: 让生成出平台契约之外的图（下界 1：0 张图不成图文笔记）。
    "xhs_note_image_total_limit": (1, 18),
    "log_retention_days": (1, 3650),
    "job_log_retention_days": (1, 3650),
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


def invalidate_key(key: str) -> None:
    """按 key 清**所有用户**的缓存格（系统级/工作区级写路径专用）。

    系统级行改动影响每个用户的读点 —— 只清 `(None, key)` 一格会留下其它用户
    的陈旧缓存（TTL 3s 兜底会让「改了 3 秒后才生效」，违背「写后立即生效」判据）。
    """
    with _cache_lock:
        stale = [ck for ck in _cache if ck[1] == key]
        for ck in stale:
            _cache.pop(ck, None)


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


def _workspace_row_any(db: Session, key: str) -> Optional[SystemSetting]:
    """取该键在**任意工作区**的那一行（设计 66 归属工作区级的键专用）。

    ## 为什么是「任意工作区」而不是「当前工作区」

    运行时读取点（限流器 / 同步 worker / OCR 并发）**没有工作区上下文** ——
    它们只知道自己要为哪个账号干活，而账号 → 工作区的解析要多一次查询，
    放在每次平台请求的热路径上不划算。

    本系统目前只有**一个业务工作区**（`xhs`），所以「任意」与「xhs」等价。
    接第二个平台时，正确做法是这些读点自己带上工作区（`workspace` 参数），
    而不是在这里猜 —— 那时 `_workspace_row_any` 应改成必须传 workspace。

    优先级：先找 `workspace IS NOT NULL` 的行；没有则退到「早期的工作区级行」
    （`user_id IS NULL ∧ workspace IS NULL` 且键归属工作区级）—— 迁移前
    这 5 个键就存在那一层，不能因为口径改了就读不到值。
    """
    row = db.scalar(
        select(SystemSetting)
        .where(SystemSetting.key == key, SystemSetting.workspace.isnot(None))
        .order_by(SystemSetting.workspace)
    )
    if row is not None:
        return row
    return db.scalar(
        select(SystemSetting).where(
            SystemSetting.key == key,
            SystemSetting.user_id.is_(None),
            SystemSetting.workspace.is_(None),
        )
    )


def _system_row(db: Session, key: str) -> Optional[SystemSetting]:
    """系统级行（`user_id IS NULL ∧ workspace IS NULL`，设计 64 §1.2）。"""
    return db.scalar(
        select(SystemSetting).where(
            SystemSetting.user_id.is_(None),
            SystemSetting.workspace.is_(None),
            SystemSetting.key == key,
        )
    )


def effective_value(
    db: Optional[Session], user_id: Optional[int], key: str, *, default: Any = UNSET
) -> Any:
    """生效取值（设计 64 §1.3 两级形态）：**用户级行 → 系统级行 → settings → 内置兜底 → default**。

    `db=None` 时**跳过 DB 层**（用于「没有会话也不想开一个」的调用点，如限流器的
    缓存未命中路径自己开会话）。系统级行对所有用户生效 —— 管理员在系统管理改一次，
    全站读点即变（用户级行仍是个人覆盖，优先于系统级）。
    """
    if db is not None:
        #: ① **归属工作区级**的键（`SETTING_SCOPES[key] == "workspace"`）：
        #: 它们的行落在工作区级（`workspace` 列有值 / 早期是 `user_id` 与 `workspace`
        #: 都为 NULL 的那一行），**不在**用户级/系统级里。
        #: 若不在这里先处理，重新归类后这些配置会「改了不生效」——
        #: 页面能改、业务读的是另一层的旧值，且**零报错**（静默失效）。
        #: ⚠️ 业务热路径没有「当前工作区」上下文（限流器只认账号），所以读
        #: **任意工作区的那一行**：本系统目前只有一个业务工作区（xhs），
        #: 多平台时按 `platform_account.workspace` 解析（那时才需要改这里）。
        if SETTING_SCOPES.get(key) == "workspace":
            ws_row = _workspace_row_any(db, key)
            if ws_row is not None:
                return decode_value(ws_row.value, ws_row.value_type)
        if user_id is not None:
            row = db.scalar(
                select(SystemSetting).where(
                    SystemSetting.user_id == user_id, SystemSetting.key == key
                )
            )
            if row is not None:
                return decode_value(row.value, row.value_type)
        system = _system_row(db, key)
        if system is not None:
            return decode_value(system.value, system.value_type)
    from_settings = settings_default(key)
    if from_settings is not UNSET:
        return from_settings
    if key in BUILTIN_DEFAULTS:
        return BUILTIN_DEFAULTS[key]
    return default


def effective_scoped_value(
    db: Optional[Session],
    user_id: Optional[int],
    workspace: Optional[str],
    key: str,
    *,
    default: Any = UNSET,
) -> Any:
    """三级级联取值（设计 64 §1.3 完整形态）：

    **工作区级 > 用户级 > 系统级 > settings > 内置兜底**（最贴近人的赢）。
    带 workspace 上下文的读点用这个；业务热路径没 workspace 概念的继续用
    `effective_value`（两级形态）。
    """
    if db is not None and workspace:
        row = db.scalar(
            select(SystemSetting).where(
                SystemSetting.workspace == workspace, SystemSetting.key == key
            )
        )
        if row is not None:
            return decode_value(row.value, row.value_type)
    return effective_value(db, user_id, key, default=default)


def effective_settings_map(
    db: Session, user_id: int, workspace: Optional[str] = None
) -> dict[str, dict[str, Any]]:
    """三级配置的**合并视图**（`GET /settings/effective` 的数据源）。

    返回 `{key: {value, value_type, category, label, scope_hit, updated_at}}` ——
    `scope_hit` ∈ workspace/user/system/builtin，前端据此展示「这一格生效的是哪级」。
    键集合 = 三级全部行的 key ∪ LIVE_KEYS（内置默认也展示，没行就是 builtin 命中）。
    """
    merged: dict[str, dict[str, Any]] = {}
    # 1) 系统级（底座）
    for row in db.scalars(
        select(SystemSetting).where(
            SystemSetting.user_id.is_(None), SystemSetting.workspace.is_(None)
        )
    ).all():
        merged[row.key] = {
            "value": decode_value(row.value, row.value_type),
            "value_type": row.value_type,
            "category": row.category,
            "label": row.label or row.key,
            "scope_hit": "system",
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
    # 2) 用户级（个人覆盖）
    for row in db.scalars(
        select(SystemSetting).where(SystemSetting.user_id == user_id)
    ).all():
        merged[row.key] = {
            "value": decode_value(row.value, row.value_type),
            "value_type": row.value_type,
            "category": row.category,
            "label": row.label or row.key,
            "scope_hit": "user",
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
    # 3) 工作区级（最具体，最后盖）
    if workspace:
        for row in db.scalars(
            select(SystemSetting).where(SystemSetting.workspace == workspace)
        ).all():
            merged[row.key] = {
                "value": decode_value(row.value, row.value_type),
                "value_type": row.value_type,
                "category": row.category,
                "label": row.label or row.key,
                "scope_hit": "workspace",
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
    # 4) LIVE_KEYS 里没有行的键 ⇒ builtin 命中（UI 也要能看到这批基线）
    for key, fallback in BUILTIN_DEFAULTS.items():
        if key not in merged:
            from_settings = settings_default(key)
            merged[key] = {
                "value": fallback if from_settings is UNSET else from_settings,
                "value_type": infer_value_type(fallback),
                "category": CATEGORY_FEATURE,
                "label": key,
                "scope_hit": "builtin",
                "updated_at": None,
            }
    return merged


def _baseline_bool(key: str, default: bool) -> bool:
    """`settings` / `BUILTIN_DEFAULTS` 里的布尔基线（不含 DB 行）。

    只给 `live_global_bool` 的「没有管理员行」分支用 —— 与
    `effective_value(user_id=None)` 同口径，但不带 `default` 短路。
    """
    #: ⚠️ 顺序 = `effective_value` 的**同一顺序**（settings 优先于 BUILTIN）。
    #:
    #: `settings` 层是 `config/default.yaml` 的值 —— 用户可编辑的部署配置，
    #: 优先级必须高于代码里的兜底常量。这也解释了一个曾经困惑的现象：
    #: 只改 `BUILTIN_DEFAULTS` 打不开注册（yaml 里是 `false`）—— 那是**设计如此**，
    #: 不是 bug。测试要打开注册必须改设置行或改 yaml。
    from_settings = settings_default(key)
    value = from_settings if from_settings is not UNSET else BUILTIN_DEFAULTS.get(key, UNSET)
    if value is UNSET or value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def live_global_bool(db: Optional[Session], key: str, *, default: bool) -> bool:
    """**全局开关**读取（设计 64 §1.2 三级化后）：**系统级行为准**。

    ## 为什么不能直接用 `live_bool(db, None, ...)`

    `live_bool(db, None, key)` 在 `user_id=None` 时（三级化之前）会跳过 DB 层 ——
    于是「管理员把注册打开」这件事在未登录的注册请求里读不到。三级化之后
    `register.enabled` 落在**系统级行**（`user_id IS NULL`），`effective_value`
    对任何 `user_id`（含 None）都会读它 —— 本函数即「系统级行 → 旧管理员行 → 基线」：

    * 系统级行：三级化后的唯一权威写点（系统管理配置页）；
    * 旧管理员行：迁移 `b0c1d2e3f4a5` 只删「值==默认」的用户行，管理员真改过的
      行会**保留为用户级** —— 兼容读它，管理员重新在系统管理保存一次即收敛；
    * 都没有 ⇒ 配置基线（`settings` / `BUILTIN_DEFAULTS`），不直接给 `default`
      （实测教训：`return default` 会让测试库无行时开关永远关）。
    """
    if not key or key not in LIVE_KEYS or db is None:
        return default

    def _as_bool(raw: Any) -> Optional[bool]:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        if isinstance(raw, str):
            lowered = raw.strip().lower()
            if lowered in ("true", "1", "yes", "on"):
                return True
            if lowered in ("false", "0", "no", "off"):
                return False
        return None

    system = _system_row(db, key)
    if system is not None and system.value is not None:
        resolved = _as_bool(decode_value(system.value, system.value_type))
        if resolved is not None:
            return resolved

    legacy_admin = db.execute(
        select(SystemSetting.value, SystemSetting.value_type)
        .join(User, User.id == SystemSetting.user_id)
        .where(
            SystemSetting.key == key,
            User.role == ROLE_ADMIN,
            User.deleted_at.is_(None),
            SystemSetting.value.is_not(None),
        )
        .order_by(SystemSetting.id.desc())
        .limit(1)
    ).first()
    if legacy_admin is not None:
        resolved = _as_bool(decode_value(legacy_admin[0], legacy_admin[1]))
        if resolved is not None:
            return resolved
    return _baseline_bool(key, default)


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

    三级化后（设计 64 §1.3）语义收敛：进程级资源（worker 池）不属于任何单个账号
    ⇒ **系统级行优先**（管理员在系统管理改一次即全进程生效）；没有系统级行时
    保留旧口径**所有用户行取最大值**（兼容迁移后幸存的用户级覆盖），再退基线。
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
        #: 系统级行优先（三级化后的权威写点）；没有系统行才看用户行 max。
        system_values: list[int] = []
        user_values: list[int] = []
        for row in rows:
            if row.user_id is None and row.workspace is None:
                decoded = decode_value(row.value, row.value_type)
                if isinstance(decoded, bool):
                    continue
                try:
                    system_values.append(int(decoded))
                except (TypeError, ValueError):
                    continue
            elif row.user_id is not None:
                decoded = decode_value(row.value, row.value_type)
                if isinstance(decoded, bool):
                    continue
                try:
                    user_values.append(int(decoded))
                except (TypeError, ValueError):
                    continue
        if system_values:
            resolved = system_values[0]
        elif user_values:
            resolved = max(user_values)
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
    scope = "workspace" if row.workspace else ("system" if row.user_id is None else "user")
    return {
        "id": row.id,
        "scope": scope,
        "workspace": row.workspace,
        "user_id": row.user_id,
        "key": row.key,
        "value": decode_value(row.value, row.value_type),
        "raw_value": row.value,
        "value_type": row.value_type,
        "category": row.category,
        "label": row.label or row.key,
        #: ⚠️ 界面只看短说明（设计 65 / 2026-10-25 用户口径「说明简短」）。
        #: 库里的 `description` 是给开发看的（含设计依据 / 历史沿革），可能上百字 ⇒
        #: **不直接下发**；`short_description()` 读不到时才回落到库里的值。
        "description": short_description(row.key) or (row.description or ""),
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


def list_scoped_settings(
    db: Session,
    *,
    scope: str,
    user_id: Optional[int] = None,
    workspace: Optional[str] = None,
    category: Optional[str] = None,
) -> list[SystemSetting]:
    """按级取行（设计 64 §1.5 + 设计 65 三级拆分）。

    ## ⚠️ 关键判据（2026-10-25 用户口径「不要混乱展示」）

    行**归属哪一级**由 `SETTING_SCOPES` 决定，**不由「库里哪一级有行」决定**。

    旧实现只按行位置过滤（`system` = `user_id IS NULL` 等），后果是：
    * 同一个键在三层都有行 ⇒ **两个页面都列它**，用户不知道该改哪个；
    * 某个键三层都没行（走内置默认）⇒ **哪个页面都不显示**，等于不可配置。

    现在：先按 `SETTING_SCOPES[key] == scope` 定出本页该展示的键，再取该级的行。
    某个键在该级还没有行 ⇒ **合成一条「使用默认值」的虚拟行**下发（`to_default_row`），
    这样三级页面各自完整、互不重复。
    """
    text = str(category or "").strip()
    keys_here = [k for k, s in SETTING_SCOPES.items() if s == scope]
    if text:
        keys_here = [k for k in keys_here if _BUILTIN_CATEGORY.get(k) == text]

    #: ⚠️ **内置键按归属过滤，用户自建的键不过滤**（2026-10-25）。
    #:
    #: 原因：`SETTING_SCOPES` 管的是**内置键**该出现在哪一级 —— 它解决「同一个内置键
    #: 在三个页面都列一遍」的混乱。但用户**有权自建**任意键（如业务临时开关），
    #: 那些键没登记归属；若一律按清单过滤，用户刚建的键**立刻从列表里消失**
    #: （实测：`test_settings_crud_round_trip` 建完再查就找不到），比混乱更糟。
    #:
    #: 判据：`SETTING_SCOPES` 里有的 ⇒ 必须属于本级才显示；
    #:       不在里面的（自建）⇒ 按「它落在哪一级」显示。
    statement = select(SystemSetting)
    if scope == "system":
        statement = statement.where(
            SystemSetting.user_id.is_(None), SystemSetting.workspace.is_(None)
        )
    elif scope == "user":
        statement = statement.where(SystemSetting.user_id == user_id)
    elif scope == "workspace":
        statement = statement.where(SystemSetting.workspace == str(workspace or "").strip())
    else:
        raise ValueError(f"未知 scope：{scope}")
    if keys_here:
        #: 内置键（属于本级的）∪ 任何未登记的自建键。
        custom_key = ~SystemSetting.key.in_(list(SETTING_SCOPES))
        statement = statement.where(
            or_(SystemSetting.key.in_(keys_here), custom_key)
        )

    rows = list(
        db.scalars(
            statement.order_by(SystemSetting.category.asc(), SystemSetting.key.asc())
        ).all()
    )

    #: 缺行的键补虚拟默认行（不落库）—— 否则「没配过」的键在页面上不可见。
    present = {row.key for row in rows}
    for key in keys_here:
        if key not in present:
            rows.append(_default_row(key, scope=scope, user_id=user_id, workspace=workspace))
    return sorted(rows, key=lambda r: (r.category or "", r.key))


def find_setting(db: Session, user_id: int, key: str) -> Optional[SystemSetting]:
    """按 `(user_id, key)` 取行（归属校验的唯一入口）。"""
    return db.scalar(
        select(SystemSetting).where(
            SystemSetting.user_id == user_id, SystemSetting.key == str(key or "").strip()
        )
    )


def find_scoped(
    db: Session,
    *,
    scope: str,
    key: str,
    user_id: Optional[int] = None,
    workspace: Optional[str] = None,
) -> Optional[SystemSetting]:
    """按级取单行（归属校验的唯一入口，设计 64 §1.5）。"""
    clean = str(key or "").strip()
    if scope == "system":
        return _system_row(db, clean)
    if scope == "user":
        return find_setting(db, int(user_id or 0), clean)
    if scope == "workspace":
        return db.scalar(
            select(SystemSetting).where(
                SystemSetting.workspace == str(workspace or "").strip(),
                SystemSetting.key == clean,
            )
        )
    raise ValueError(f"未知 scope：{scope}")


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
    scope: str = "user",
    workspace: Optional[str] = None,
) -> SystemSetting:
    """新增一行（**不 commit**：调用方提交）。key 已存在 ⇒ `ValueError`（API 转 409）。

    `scope`（设计 64 §1.5）：`user`（缺省，本人覆盖）/ `system`（全局行，
    user_id=NULL）/ `workspace`（工作区行，需传 `workspace`）。权限面在 API 层收口。
    """
    from backend.app.core.time import shanghai_now

    clean_key = str(key or "").strip()
    if not clean_key:
        raise ValueError("配置键不能为空")
    if len(clean_key) > KEY_MAX_LENGTH:
        raise ValueError(f"配置键最长 {KEY_MAX_LENGTH} 字符")
    scope = str(scope or "user").strip()
    clean_workspace = str(workspace or "").strip() or None
    if scope == "workspace" and not clean_workspace:
        raise ValueError("工作区级配置必须指定 workspace")
    if clean_workspace and scope != "workspace":
        raise ValueError("只有工作区级配置带 workspace")
    if find_scoped(db, scope=scope, key=clean_key, user_id=user_id, workspace=clean_workspace) is not None:
        raise ValueError(f"配置键 {clean_key} 已存在（{scope} 级）")

    kind = normalize_value_type(value_type) if value_type else infer_value_type(value)
    coerced = coerce_value(value, kind, key=clean_key)
    now = shanghai_now()
    row = SystemSetting(
        user_id=None if scope in ("system", "workspace") else user_id,
        workspace=clean_workspace if scope == "workspace" else None,
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
    if scope == "user":
        invalidate(user_id=user_id, key=clean_key)
    else:
        invalidate_key(clean_key)
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
    if row.user_id is not None:
        invalidate(user_id=row.user_id, key=row.key)
    else:
        invalidate_key(row.key)
    return row


def delete_setting(db: Session, row: SystemSetting) -> dict[str, Any]:
    """删一行（**不 commit**）⇒ 该键**回落到 settings / 内置默认**。

    返回 `{"key", "effective_value"}`：让前端立刻显示回落后的值，不用再发一次 GET
    （删除的语义是「恢复默认」，用户必须看得见恢复成了什么）。
    """
    key = row.key
    user_id = row.user_id
    scope = "workspace" if row.workspace else ("system" if user_id is None else "user")
    db.delete(row)
    db.flush()
    if scope == "user":
        invalidate(user_id=user_id, key=key)
    else:
        invalidate_key(key)
    fallback = (
        effective_scoped_value(db, user_id, row.workspace, key, default=None)
        if scope == "workspace"
        else effective_value(db, user_id, key, default=None)
    )
    return {"key": key, "scope": scope, "effective_value": fallback}


# --------------------------------------------------------------------------- #
# 种子（随安装包分发的基线，设计 10 §7.3）
# --------------------------------------------------------------------------- #
def seed_system_settings(db: Session) -> list[SystemSetting]:
    """播种**系统级**配置行（设计 64 §1.4：三级化后每 key 一行全局行，不再每用户一份）。

    幂等：同 key 的系统级行已存在 ⇒ 跳过（**只补缺不覆盖** —— 管理员在系统管理
    调过的值不会被新版本重置回基线，与旧 `seed_settings_for_user` 同一升级语义）。
    只种有运行时读点的键（`LIVE_KEYS`）：种没有读点的行 = 给「改了没反应」造现场。
    """
    from backend.app.core.initial_data import load_initial_system_settings

    payload = {row["key"]: row for row in load_initial_system_settings()}
    existing = set(
        db.scalars(
            select(SystemSetting.key).where(
                SystemSetting.user_id.is_(None), SystemSetting.workspace.is_(None)
            )
        ).all()
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
                user_id=None,
                workspace=None,
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


def seed_builtin_settings_from_initial_data() -> int:
    """应用启动时播种**系统级**配置行（设计 64 §1.4）。返回新建行数。

    **与任何开关解耦**：设置页打开就该有那批行 —— 播种挂启动，不挂某个子系统的装配。
    失败只记 warning 不抛：种子坏掉是可容忍的，但不能静默。
    """
    db = SessionLocal()
    try:
        created = seed_system_settings(db)
        if created:
            db.commit()
            logger.info("应用初始化数据：补种 %s 行系统级运行时配置", len(created))
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


# ---------------------------------------------------------------------------
# 限流额度的真相源注册（设计 61 A7 依赖反转）
# ---------------------------------------------------------------------------
# `core/rate_limiter` 的第 2 层额度（运行时配置）由本模块提供 —— core 不许反向
# import services，所以由**拥有真相源的一方**在加载时注册读取函数（注册表式注入）。
# 本模块被应用启动链必然加载（`main.lifespan` 播种 / 各服务读配置），接线不会漏；
# 测试里单独 import `core.rate_limiter` 而未加载本模块时，退到 settings YAML 额度。
from backend.app.core import rate_limiter as _core_rate_limiter  # noqa: E402

_core_rate_limiter.QUOTA_PROVIDER = rate_limit_per_minute_for_key
