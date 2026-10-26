"""应用启动编排（设计 61 §三 A3b，2026-10-24）。

## 为什么是「薄壳拆分」

`main.lifespan` 原来 130 行内联 25+ 个步骤。本模块把实现按**步**拆成 6 个函数，
`main.lifespan` 保留**裸名调用薄壳** —— 这不是装饰：

守卫 `test_scheduler_sync::test_lifespan_steps_are_all_stubbed_by_this_module`
用 AST 断言「lifespan 的每个裸名调用都被测试桩掉（或登记免桩）」，桩经
`monkeypatch.setattr(main_module, "名字", …)` 打在 **main 模块属性**上。
把实现搬走、调用留在 main ⇒ 守卫**一行不改语义**（两级展开后照常工作），
桩目标按需在 main（整步）/ startup（步内细节）两级打。

⚠️ 陷阱（调研结论）：**不要**改成 `for step in STEPS: step(app)` ——
裸名 `step` 无法 `setattr` 打桩，与守卫的结构性前提不兼容。

## 为什么本模块在应用根而不是 core/

启动编排是**组合根**：它天然依赖全部服务层（播种 / 看门狗 / 恢复 / 唤醒）。
放 `core/` 会直接违反依赖方向合同 C1（core 不得 import services）——
组合根与 `main.py` 同级（应用根），不受三条合同管辖。

## 步骤与副作用（测试打桩的依据）

| 函数 | 副作用 |
|---|---|
| `seed_all_builtin_data` | 自开 Session **写库**（7 组种子，只补缺不覆盖） |
| `acquire_lease_and_start_scheduler` | 在 `data/` 建**租约文件** + 起调度器线程 + 守活 |
| `start_all_watchdogs` | 起 5 条常驻看门狗线程 |
| `recover_and_wake_workers` | 复位 `running`/`publishing` 行（写库）+ 起 6 条 worker |
| `shutdown_all` | 停看门狗 / 停调度器 / 释放租约（幂等，测试安全） |
"""
from __future__ import annotations

import logging
from pathlib import Path

from backend.app.core.config import get_settings
from backend.app.core.database import SessionLocal
from backend.app.core.selfcheck import log_startup_selfcheck  # noqa: F401 - 重导出给 main 薄壳
from backend.app.core.singleton import acquire_scheduler_lease, release_scheduler_lease
from backend.app.core.route_guard import assert_every_route_guarded  # noqa: F401
from backend.app.core.database import init_db  # noqa: F401
from backend.app.services import note_queue_service
from backend.app.services.account_task_service import (
    ensure_account_task_worker,
    recover_account_tasks,
    start_account_task_watchdog,
    stop_account_task_watchdog,
)
from backend.app.services.forbidden_word_service import seed_builtin_words_for_all_users
from backend.app.services.llm_gateway_seed_service import seed_gateway_providers_for_all_users
from backend.app.services.menu_seed_service import seed_system_domain
from backend.app.services.note_gen_llm_worker import (
    ensure_llm_generate_worker,
    start_llm_generate_worker_watchdog,
    stop_llm_generate_worker_watchdog,
)
from backend.app.services.note_publish_service import (
    ensure_publish_worker,
    start_publish_worker_watchdog,
    stop_publish_worker_watchdog,
)
#: ⚠️ 设计 75 S4/S5（2026-10-27）：`note_sync_service` 的同步 worker 已整条退场
#: （用户口径「同步笔记任务全链路移除」，笔记改实时查询）—— 不再 ensure / 不再起看门狗。
from backend.app.services.product_note_gen_service import (
    ensure_generate_worker,
    start_generate_worker_watchdog,
    stop_generate_worker_watchdog,
)
#: ⚠️ 设计 75 S5（2026-10-27）：`shop_goods_sync_service` 整模块删除（同步商品任务
#: 全链路退场，商品改实时查询）—— 不再 ensure 商品同步 worker。
from backend.app.services.scheduler_service import (
    seed_builtin_jobs_from_initial_data,
    shutdown_scheduler,
    start_scheduler,
    start_scheduler_watchdog,
    stop_scheduler_watchdog,
)
from backend.app.services.settings_service import seed_builtin_settings_from_initial_data
from backend.app.services.data_package_rule_service import seed_builtin_rules_for_all_users
from backend.app.api.llm_prompts import seed_builtin_prompt_templates_for_all_users

logger = logging.getLogger(__name__)


def _data_dir_for_lease(settings) -> Path:
    """租约文件所在目录（单副本调度锁，`core/singleton.py`）。

    `app_root()` 按**文件位置**推仓库根（不依赖 CWD，见 `core/paths.py`），
    systemd / Docker / 换目录起 uvicorn 都指向同一个 `data/`。
    """
    from backend.app.core import paths

    return paths.app_root() / "data"


def seed_all_builtin_data() -> None:
    """全部「随安装附有 + **只补缺失不覆盖**」的内置种子（7 组）。

    ⚠️ 每组都会**自开 Session 写库**；测试进真 lifespan 必须整组打桩
    （或在本模块逐个打桩），否则副作用落在真实运行库上
    （2026-09-28 实测：新增一个配置键后播种第一次真写，6 行进了生产库）。
    """
    # 应用初始化数据（设计 15 §4.5）：定时任务基线随安装自动附有。
    # 位置在调度器之前且**不受 `scheduler_enabled` 影响** —— 桌面版默认不启调度器，
    # 若把播种挂在调度器装配里，任务中心会是空的（「装完即有用」落空）。
    seed_builtin_jobs_from_initial_data()
    # 运行时配置基线（设计 10 §7.3）：必须早于任何运行期读点（限流器 / worker 池都读它）。
    seed_builtin_settings_from_initial_data()
    # 内置提示词模板 / 模块（设计 17 §13.5.4，v2.24）：没有种子时出图弹窗下拉是空的。
    seed_builtin_prompt_templates_for_all_users()
    # 设计 40：为存量用户补种数据包列规则的示例包（只补「一个都没有」的用户）。
    seed_builtin_rules_for_all_users()
    # LLM 网关服务商种子**补存量**（2026-10-13 用户报障「模型服务商依旧未配置」）。
    seed_gateway_providers_for_all_users()
    # 2026-10-21：违规词内置种子**补存量**（只往空分组追加，用户改过/删过的不重建）。
    seed_builtin_words_for_all_users()
    # 系统管理域（v1.1.0 / 设计 47 §6，S1）：角色 / 菜单 / 字典 / 出厂 admin / 授权。
    # 放在这里是**判据**：`create_all` 建库不经过迁移链，只有启动期会播种；
    # 「刚 upgrade 完的库」由迁移 3 播一次。
    try:
        _db = SessionLocal()
        try:
            seed_system_domain(_db)
            _db.commit()
        finally:
            _db.close()
    except Exception as exc:  # noqa: BLE001 - 播种失败不该让整个应用起不来
        logger.warning("startup: seed system domain failed: %s", exc)


def acquire_lease_and_start_scheduler(app) -> None:
    """多实例闸（T-108 / DEF-003）+ 调度器启动 + 守活接线。

    抢不到租约的进程**不启动调度器**（其余 API / worker 照常，那些链路各有
    自己的 DB 级幂等），并把原因如实记进日志 —— 静默跳过正是 DEF-048 那类
    「看着在跑其实没跑」。在 `data/` 创建**租约文件**（不碰 DB，但会留锁文件）。
    """
    settings = get_settings()
    scheduler_lease_acquired, scheduler_lease_reason = True, "未启用多实例闸（scheduler 关闭）"
    if settings.scheduler_enabled:
        scheduler_lease_acquired, scheduler_lease_reason = acquire_scheduler_lease(
            _data_dir_for_lease(settings)
        )
        if not scheduler_lease_acquired:
            logger.warning("调度器未启动：%s", scheduler_lease_reason)
    app.state.scheduler_lease_acquired = scheduler_lease_acquired
    app.state.scheduler_lease_reason = scheduler_lease_reason
    scheduler = (
        start_scheduler()
        if settings.scheduler_enabled and scheduler_lease_acquired
        else None
    )
    app.state.scheduler = scheduler
    if scheduler is not None:
        # 守活（DEF-067）：APScheduler 主循环一遇异常就整线程死掉且不自愈
        # （`running` 还骗人地报 True）⇒ 靠看门狗重建，否则作业静默停摆。
        start_scheduler_watchdog(app)


def start_all_watchdogs() -> None:
    """五条常驻看门狗（DEF-069 同型缺口逐一补齐，线程被带走后无人自愈）。"""
    # 守活（DEF-069）：批量同步 worker —— 真机实测 19 篇跑到 8 篇后线程无声消失。
    start_sync_worker_watchdog()
    # 守活（DEF-069 同型缺口）：发布 worker —— 线程被 SystemExit 带走就永久停摆。
    start_publish_worker_watchdog()
    # 守活（DEF-069 同型）：商品生成 worker（§31.10）—— 一篇三步 + 轮询 ~6–21s。
    start_generate_worker_watchdog()
    # 守活（DEF-069 同型）：LLM 生成队列 worker（设计 17 §7.4 / T6）—— 最长的一条链。
    start_llm_generate_worker_watchdog()
    # 守活（DEF-069 同型）：账号级任务 worker（设计 13 §35.16）。
    start_account_task_watchdog()


def recover_and_wake_workers() -> None:
    """重启恢复（设计 13 §35.9.2）+ 立刻唤醒全部 worker。

    ⚠️ 只在**启动时**做一次：正常运行时 `running` / `publishing` 是「正在跑」的
    合法状态，在跑的过程中复位就是把任务从 worker 脚下抽走。
    ⚠️ 顺序是判据：**先复位再唤醒** —— 先唤醒会让 worker 去认领那些本该被
    复位成 `pending` 的 `running` 行（认领只认 `pending`，等于什么也没捞到）。
    """
    # ① 上次进程留下的 `running` 队列项 ⇒ 复位成 `pending`（只自动重试「中断」；
    #    业务失败不自动重试，等用户在任务记录里手动点「重试」）；
    # ② 卡在 `publishing` 的笔记行 ⇒ 复位成 `pending`（进程在两步之间退出会让它
    #    永远停在那儿：既不会被重跑，也不进任何终态统计）。
    recovery_db = SessionLocal()
    try:
        note_queue_service.recover_interrupted(recovery_db)
        note_queue_service.recover_publishing_notes(recovery_db)
    finally:
        recovery_db.close()
    # §35.16：账号级任务的 `running` 行**不在** `QUEUE_ITEM_TASK_TYPES` 里 ⇒
    # 上面那次复位覆盖不到，此前永远卡在「同步中」（真缺陷，DEF-048 同型）。单独复位。
    recover_account_tasks()
    # 复位完**立刻**唤醒六个 worker：积压的 `pending` 项不该等看门狗 30 秒巡检
    # 才被发现（唤醒带兜底超时的长等待，无活可干时它们自动回去睡）。
    ensure_publish_worker()
    ensure_generate_worker()
    ensure_llm_generate_worker()
    ensure_account_task_worker()


def shutdown_all(app) -> None:
    """退出清理（幂等）：停看门狗 → 停调度器 → 释放租约。"""
    stop_publish_worker_watchdog()
    stop_sync_worker_watchdog()
    stop_generate_worker_watchdog()
    stop_llm_generate_worker_watchdog()
    stop_account_task_watchdog()
    stop_scheduler_watchdog()
    # 取 `app.state` 上的**当前**实例：看门狗可能已经换过一台。
    shutdown_scheduler(getattr(app.state, "scheduler", None))
    # 释放多实例租约（T-108）—— 只在**确实持有**时删（`atexit` 是兜底）。
    release_scheduler_lease()
