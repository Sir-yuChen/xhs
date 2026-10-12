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


#: 内置任务键（**唯一真相源**）：API 层按 key 定位任务行（账号卡定向执行，
#: 设计 13 §35.17.2 的 `run_targeted_job`）时引用这里，**不要在别处再写一遍字面量** ——
#: 漂移后要么 `get_job_spec` 抛 `KeyError`，要么更糟：悄悄定位到另一个任务。
JOB_KEY_NOTE_SYNC = "system.note_sync"
JOB_KEY_GOODS_NOTE_SYNC = "system.goods_note_sync"
JOB_KEY_SHOP_GOODS_SYNC = "system.shop_goods_sync"
JOB_KEY_ORDER_SYNC = "system.order_sync"
#: 2026-10-05 用户口径：队列记录清理**升级为公共「清理记录表」任务**
#: （`system.queue_record_cleanup`）—— 定期清理项目中所有指定记录表。
#: 键**保持不变**（存量行与历史日志快照不迁移即可继续对上），中文名改「清理记录表」。
JOB_KEY_RECORD_CLEANUP = "system.queue_record_cleanup"
#: ⚠️ **`system.no_traffic_cleanup` 已于 2026-10-07 从注册表移除**（用户口径
#: 「无流量笔记清理任务表移除该配置」+「无流量笔记清理任务不应在任务列表展示」）：
#: 清理**只能从「我的笔记」页按钮发起**，定时任务中心里既不该有这一行、也不该
#: 出现在「调用目标」下拉里。原先那句「键保留在注册表里（任务中心可点立即执行、
#: 可看日志）」**被本次取代**；键名常量与 `run_scheduled_cleanups` 一并删除
#: （留着一个再也注册不上的执行器只会让人以为还有定时入口）。
#: 迁移 `j1k2l3m4n5o6` 负责把**存量行**从 `scheduled_jobs` 表清掉（光删注册表
#: 只会让这些行变成 `unknown job_key` 的僵尸 —— 列表照显、点击报错）。
#: ⚠️ 清理**本身**没有退场：`/users/no-traffic/cleanup` 端点、候选池、日志表
#: （`no_traffic_cleanup_logs` / `_items`）与「我的笔记」页入口全部保留。
#: 离线笔记数据同步（「我的笔记」商业/普通两类，2026-10-06 需求）。
JOB_KEY_NOTE_DATA_SYNC = "system.note_data_sync"

#: 「每日发布商业笔记」（设计 15 §19，2026-10-07 用户需求）：逐账号探活 → 取
#: 「挂链笔记数为 0」的商品 → 按指定方式生成 → 对生成成功的笔记逐篇发布。
JOB_KEY_BUSINESS_NOTE_AUTO_PUBLISH = "system.business_note_auto_publish"

#: M6 方向 6（设计 16 §7）订单同步。与 `JOB_KEY_SHOP_GOODS_SYNC` 分开：
#: 落不同的表、不同的任务类型，任务中心是两行独立的任务。

#: 图片空间「清理未引用图片」（2026-10-11 用户需求）：每周一清一次
#: 未引用（平台 `productCount == 0`）的图片，可选「进回收站 / 彻底删除」。
JOB_KEY_PICTURE_SPACE_CLEANUP = "system.picture_space_cleanup"


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
    params_defaults: dict[str, Any] = field(default_factory=dict)
    """每个参数的**默认值**（2026-10-07 用户口径「调用参数不允许随意添加和删除，
    必须有默认值」）。

    ⚠️ 这是**契约声明，不是 UI 提示**，三处都从它出发：

    1. **兜底**：`scheduled_job_runner` 在执行前把缺的键按它补上 ⇒ 存量任务行
       （早于某个参数上线）拿到新参数时**不会 `TypeError`**，行为 = 默认值；
    2. **广播**：`GET /schedule/targets` / 任务序列化下发 `params_schema`
       （键 + 中文名 + 默认值 + 控件类型），前端据此**渲染固定行**；
    3. **校验**：注册表守卫断言「键集 == `params_editable ∪ params_required`」
       且每个键都有默认值 ⇒ 新增参数时**不可能漏默认值**。

    `account_ids` 的默认值恒为 `[]`（= 本人全部账号，既有口径）。

    ⚠️ **不另设 `params_schema`**：控件类型由**默认值的类型**推断
    （`list` ⇒ 多选 / `bool` ⇒ 开关 / `int` ⇒ 数字 / `str` ⇒ 文本），
    枚举取值表仍在前端 `PARAM_ENUM_OPTIONS`（按 key）。再声明一份 schema = 第二个
    真相源，两处迟早不一致（DEF-044）。"""
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
    from backend.app.services.business_note_publish_service import (
        run_business_note_auto_publish,
        validate_business_publish_params,
    )
    from backend.app.services.metric_scheduler_service import (
        run_note_metrics_collect_once,
        run_shop_metrics_collect_once,
    )
    from backend.app.services.picture_space_cleanup_service import (
        run_picture_space_cleanup,
        validate_cleanup_params,
    )
    from backend.app.services.record_cleanup_service import (
        run_record_cleanup,
        validate_retention_days,
    )
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
            params_defaults={"account_ids": []},
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
            params_defaults={"account_ids": []},
            default_trigger_type="cron",
            default_cron="0 0 */6 * * *",
        ),
        JobSpec(
            key=JOB_KEY_NOTE_SYNC,
            name="账号笔记同步",
            func=scheduler_service.run_scheduled_note_syncs,
            builtin=True,
            user_scoped=True,
            params_editable=("account_ids",),
            params_defaults={"account_ids": []},
            default_trigger_type="cron",
            default_cron="0 0 */6 * * *",
        ),
        JobSpec(
            key=JOB_KEY_GOODS_NOTE_SYNC,
            # §44（2026-09-30）：商品笔记同步（千帆 goods-note/list 的挂链笔记）——
            # 由原「账号笔记同步」的混合双源拆出的独立任务，与 JOB_KEY_NOTE_SYNC
            # （普通笔记，creator 源）各自成行：各自 cron、各自 account_ids、各自日志流。
            # ⚠️ 名字里必须带「商品笔记」四字 —— 它与 `JOB_KEY_SHOP_GOODS_SYNC`
            # （**店铺商品台账**，SKU / 价格）不是一回事。
            name="账号商品笔记同步",
            func=scheduler_service.run_scheduled_goods_note_syncs,
            builtin=True,
            user_scoped=True,
            params_editable=("account_ids",),
            params_defaults={"account_ids": []},
            default_trigger_type="cron",
            default_cron="0 0 */6 * * *",
        ),
        JobSpec(
            key=JOB_KEY_SHOP_GOODS_SYNC,
            name="店铺商品同步",
            func=scheduler_service.run_scheduled_shop_goods_syncs,
            builtin=True,
            user_scoped=True,
            params_editable=("account_ids",),
            params_defaults={"account_ids": []},
            default_trigger_type="cron",
            default_cron="0 0 */6 * * *",
        ),
        JobSpec(
            # 「我的笔记」离线数据同步（2026-10-06 需求）：千帆数据中心商业 + 普通
            # 两类笔记，走 butterfly/data 列表接口（无签名直连）落库。与上面的
            # 笔记同步（creator/ark 三方同步）是**两件事**：那个同步到 `note_type=synced`
            # 的线上笔记，这个落 `note_datacenter_stats` 统计表的离线数据中心笔记。
            key=JOB_KEY_NOTE_DATA_SYNC,
            name="离线笔记数据同步",
            func=scheduler_service.run_scheduled_note_data_syncs,
            builtin=True,
            user_scoped=True,
            # 2026-10-06：新增「时间区间」参数 `days`（同步最近 N 天，上限 90）。
            params_editable=("account_ids", "days"),
            params_defaults={"account_ids": [], "days": 90},
            default_trigger_type="cron",
            default_cron="0 0 */6 * * *",
        ),
        JobSpec(
            # M6 方向 6（设计 16 §7）：订单一期只读 —— 拉 `fulfillment/order/page`
            # 落 `orders` 等 5 张表，不触任何写操作。
            key=JOB_KEY_ORDER_SYNC,
            name="店铺订单同步",
            func=scheduler_service.run_scheduled_order_syncs,
            builtin=True,
            user_scoped=True,
            params_editable=("account_ids",),
            params_defaults={"account_ids": []},
            default_trigger_type="cron",
            default_cron="0 30 */6 * * *",
        ),
        JobSpec(
            # 2026-09-25 用户口径「笔记任务记录清理应扩展为队列详情记录清理……
            # 任务名称修改为队列记录清理」：任务键与中文名同步改（迁移
            # `c2d3e4f5a6b7` 把存量行与历史日志一起改名，不静默留旧键）。
            key=JOB_KEY_RECORD_CLEANUP,
            name="清理记录表",
            func=run_record_cleanup,
            builtin=True,
            user_scoped=True,
            # ⚠️ 2026-10-05 用户口径**取代**了 2026-09-27 的「保留期固定口径、不可调」：
            #    保留期改为任务参数 `retention_days`（默认 15 天），在任务中心的
            #    新增 / 编辑弹窗里配，每次执行时读取。原句与对照表见设计 15 §4.5。
            params_editable=("retention_days",),
            params_defaults={"retention_days": 15},
            params_validator=validate_retention_days,
            default_trigger_type="cron",
            default_cron="0 0 7 * * *",
        ),
        JobSpec(
            # 2026-10-03 M2 数据仪表盘切片 2（设计 17 §13.9）：笔记指标快照采集。
            # ⚠️ **纯本地零平台请求**（数据源 = 已落库的 `notes.raw_json`），
            # 故频率不受平台限流约束；但仍**不调高到每分钟** —— 写锁是共享资源，
            # 采集频率只要不低于源数据的更新频率即可（笔记同步默认 6 小时一次）。
            key="system.note_metrics_collect",
            name="笔记指标快照采集",
            func=run_note_metrics_collect_once,
            builtin=True,
            user_scoped=True,
            params_editable=(),
            default_trigger_type="cron",
            # 每小时第 5 分。**避开整点**：整点是同步/发布/巡检的集中时段，
            # 错峰减少 SQLite 写锁竞争（DEF-085 的直接教训）。
            default_cron="0 5 * * * *",
        ),
        JobSpec(
            # 2026-10-03 M2 数据仪表盘切片 2（设计 17 §13.9）：商品指标快照采集。
            # ⚠️ 与笔记采集**错开 30 分钟**（第 35 分），避免两个采集同时抢写锁。
            key="system.shop_metrics_collect",
            name="店铺商品指标快照采集",
            func=run_shop_metrics_collect_once,
            builtin=True,
            user_scoped=True,
            params_editable=(),
            default_trigger_type="cron",
            default_cron="0 35 * * * *",
        ),
        JobSpec(
            # 设计 15 §19（2026-10-07 用户需求「新增定时任务 每日发布商业笔记」）：
            # 平台写操作（生成 + 发布）⇒ 恒 forbid 并发；账号维度 ⇒ user_scoped。
            key=JOB_KEY_BUSINESS_NOTE_AUTO_PUBLISH,
            name="每日发布商业笔记",
            func=run_business_note_auto_publish,
            builtin=True,
            user_scoped=True,
            params_editable=(
                "account_ids",
                "limit",
                "interval_seconds",
                "generate_mode",
                # 2026-10-07 追加：发布失败重试三件（用户口径「是否重试、最多 3 次、间隔时间」）。
                "retry_on_failure",
                "max_retries",
                "retry_interval_seconds",
            ),
            # 默认值 = 服务层解析器的缺省（`_coerce_int` / `_coerce_bool` 的 default）——
            # **同一份数值只在这里写一次**：运行器兜底、前端预填、测试都读它。
            params_defaults={
                "account_ids": [],
                "limit": 1,
                "interval_seconds": 30,
                "generate_mode": "qianfan",
                "retry_on_failure": True,
                "max_retries": 3,
                "retry_interval_seconds": 30,
            },
            params_validator=validate_business_publish_params,
            default_trigger_type="cron",
            default_cron="0 0 9 * * *",
        ),
        JobSpec(
            key=JOB_KEY_PICTURE_SPACE_CLEANUP,
            # 2026-10-11 用户需求「图片空间新增清理未引用图片定时任务，每周一执行一次」。
            # 平台**写操作**（批量删除 / 彻底删除）⇒ 恒 forbid 并发；账号维度 ⇒ user_scoped。
            name="清理未引用图片",
            func=run_picture_space_cleanup,
            builtin=True,
            user_scoped=True,
            #: 用户口径：「执行账号默认全部；删除到回收站或彻底删除下拉单选二选一，
            #: 默认彻底删除」。两个键都登记到这里 ⇒ 前端自动渲染两行（`account_ids`
            #: 走账号下拉多选、`delete_mode` 走枚举下拉，见 `params-editor`）。
            params_editable=("account_ids", "delete_mode"),
            params_defaults={"account_ids": [], "delete_mode": "permanent"},
            params_validator=validate_cleanup_params,
            default_trigger_type="cron",
            #: 用户口径「每周一执行一次」= `0 分 0 时 * * MON`（6 位：秒 分 时 日 月 周）。
            #: ⚠️ **用 `MON` 不用数字 `1`** —— APScheduler 的 cron 周字段同样认 0-6（0=周一），
            #: 写数字会和「周日=0 还是周一=0」的习惯打架，英文缩写无歧义。
            default_cron="0 0 0 * * MON",
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
