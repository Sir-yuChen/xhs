"""任务注册表（设计 15 §5）——定时任务中心的**代码侧白名单**。

为什么不抄若依的 `invoke_target` 自由字符串：若依靠白名单常量拦恶意字符串
（早期出过 RCE 类问题后才补的）；Python 生态里"按字符串 importlib 反射"比
Java 更危险（`os.system` 一行就能拼出来）。本表把"可被调度的函数"钉死在
代码里：新增任务类型 = 改本表 + 发版，**运行时不可注入**。

⚠️ 注册表**惰性构建**（`_build_registry` 在首次访问时才 import 各服务的
模块级函数）：`scheduler_service` 要在模块层 import 本表做启动同步，而本表
   引用的函数又住在 `scheduler_service` / `shop_health_service` /
   `auto_ops_service` 里 —— 模块层互相 import 会成环，惰性构建拆掉这个环。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class JobSpec:
    """一个可调度任务的代码侧声明。"""

    key: str
    """注册表键（`scheduled_jobs.job_key` 的唯一合法取值来源）。"""
    name: str
    """默认显示名（种子行 name 的初始值）。"""
    func: Callable[..., Any]
    """module 级函数（APScheduler 持久化 store 按 `module:name` 文本引用，lambda 不行）。"""
    builtin: bool = False
    """True = 种子行：禁删除、禁改 key/params；调度属性（触发器/策略/暂停）可调。"""
    default_trigger_type: str = "cron"
    """默认触发器（R14 后只剩 `cron` / `at` 可作默认；`manual` 是「只能手动触发」
    档位，种子默认不用它）。"""
    default_cron: str = "0 0 9 * * *"
    """默认 cron（6 位，秒 分 时 日 月 周）。⚠️ R14 后 interval 已退役，
    周期类任务一律用 cron 表达（如「每 6 小时」= `0 0 */6 * * *`）。"""
    default_misfire_policy: str = "once"
    allow_concurrent: bool = False
    """False ⇒ 行上的 `concurrent` 恒 `forbid`（max_instances 恒 1，平台写操作风控）。"""
    params_required: tuple[str, ...] = field(default_factory=tuple)
    """`params` 必填键（浅校验：键存在性；深校验由 func 自己负责）。"""
    params_editable: tuple[str, ...] = field(default_factory=tuple)
    """用户可写的自定义参数键（白名单）。其余键由代码拥有，API 写入即 400
    `JOB_PARAMS_NOT_EDITABLE`。首批：5 个账号维度系统任务 = `("account_ids",)`
    （多选；**空 = 行主本人的全部账号**，即 2026-09-24 隔离整改前的行为范围收口到
    行主）。后续新任务按自己的参数形态声明，互不影响。"""
    user_scoped: bool = False
    """True = 执行函数收 `user_id` 形参，runner 注入**行归属者**（DEF-066，
    设计 15 §4.1.1）：账号维度系统任务一律 True ⇒ 账号查询恒带
    `PlatformAccount.user_id == user_id`，「一行触发只跑行主数据」。
    auto_ops 管线不设（其隔离由 `auto_task_id` 归属校验承担）。"""
    params_validator: Optional[Callable[[dict, Any, Any], None]] = None
    """深校验钩子（归属 / 引用存在性等浅校验管不了的事）：`(params, db, user) -> None`，
    不合规抛 `ValueError`（API 映射 400）。创建与更新都会过它。"""


def _validate_auto_ops_params(params: dict, db: Any, user: Any) -> None:
    """auto_ops 管线参数深校验：`auto_task_id` 必须属于当前用户。

    浅校验（键存在性）管不了「拿别人的 auto_task_id 建调度行」—— 那条路会在
    调度触发时用**别人的账号与关键词**跑管线（跨租户）。API 边界挡掉。
    """
    from backend.app.models import AutoTask

    task_id = params.get("auto_task_id")
    task = db.get(AutoTask, task_id) if isinstance(task_id, int) else None
    if task is None or task.user_id != user.id:
        raise ValueError("auto_task_id 不存在或不属于当前用户")


@lru_cache(maxsize=1)
def _build_registry() -> dict[str, JobSpec]:
    # 延迟 import：拆掉 scheduler_service ↔ job_registry 的导入环（见模块 docstring）。
    from backend.app.services import scheduler_service
    from backend.app.services.auto_ops_service import run_keyword_pipeline
    from backend.app.services.shop_health_service import run_shop_health_checks_once

    specs = [
        JobSpec(
            key="system.due_publish",
            name="定时发布补跑",
            func=scheduler_service.run_due_publish_jobs_once,
            builtin=True,
            user_scoped=True,
            default_trigger_type="cron",
            default_cron="0 * * * * *",
        ),
        JobSpec(
            key="system.monitoring_refresh",
            name="监控目标刷新",
            func=scheduler_service.run_monitoring_refresh_once,
            builtin=True,
            user_scoped=True,
            default_trigger_type="cron",
            default_cron="0 * * * * *",
        ),
        JobSpec(
            key="system.cookie_health_check",
            name="账号 Cookie 巡检",
            func=scheduler_service.check_all_account_cookies_once,
            builtin=True,
            user_scoped=True,
            params_editable=("account_ids",),
            default_trigger_type="cron",
            default_cron="0 0 */2 * * *",
        ),
        JobSpec(
            key="system.qianfan_health_check",
            name="千帆登录态巡检",
            func=run_shop_health_checks_once,
            builtin=True,
            user_scoped=True,
            params_editable=("account_ids",),
            default_trigger_type="cron",
            default_cron="0 0 */6 * * *",
        ),
        JobSpec(
            key="system.no_traffic_cleanup",
            name="无流量笔记清理",
            func=scheduler_service.run_scheduled_cleanups,
            builtin=True,
            user_scoped=True,
            params_editable=("account_ids",),
            default_trigger_type="cron",
            default_cron="0 0 */6 * * *",
        ),
        JobSpec(
            key="system.note_sync",
            name="账号笔记同步",
            func=scheduler_service.run_scheduled_note_syncs,
            builtin=True,
            user_scoped=True,
            params_editable=("account_ids",),
            default_trigger_type="cron",
            default_cron="0 0 */6 * * *",
        ),
        JobSpec(
            key="system.shop_goods_sync",
            name="店铺商品同步",
            func=scheduler_service.run_scheduled_shop_goods_syncs,
            builtin=True,
            user_scoped=True,
            params_editable=("account_ids",),
            default_trigger_type="cron",
            default_cron="0 0 */6 * * *",
        ),
        JobSpec(
            key="auto_ops.keyword_pipeline",
            name="自动运营管线",
            func=run_keyword_pipeline,
            builtin=False,
            default_trigger_type="cron",
            default_cron="0 0 9 * * *",
            params_required=("auto_task_id",),
            params_validator=_validate_auto_ops_params,
        ),
    ]
    return {spec.key: spec for spec in specs}


def job_registry() -> dict[str, JobSpec]:
    """注册表全量（惰性构建 + 缓存）。"""
    return _build_registry()


def get_job_spec(key: str) -> JobSpec:
    """按键取声明；未知键抛 `KeyError`（API 层映射 400 `JOB_KEY_UNSUPPORTED`）。"""
    registry = _build_registry()
    if key not in registry:
        raise KeyError(key)
    return registry[key]


def validate_params(spec: JobSpec, params: dict[str, Any] | None) -> None:
    """`params` 浅校验：必填键存在且非空。非法抛 `ValueError`。"""
    payload = params or {}
    missing = [name for name in spec.params_required if not payload.get(name)]
    if missing:
        raise ValueError(f"缺少必填参数: {', '.join(missing)}")
