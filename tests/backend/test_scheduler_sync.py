"""账号矩阵同步的**调度侧**（T-806）契约测试。

2026-09-24（设计 15）：调度装配从「硬编码 8 job」改为 **DB 驱动**（`scheduled_jobs`
行 ⇒ 启动/API 同步注册）。本文件盯的事：

1. **开关默认关** —— 两个同步的真实默认值是关 ⇒ 种子行 `paused`（不默认替用户打平台）；
2. **注册的 job 带 `max_instances=1` + `coalesce=True`** —— 两轮同步绝不相交（风控）；
3. **「有待同步」判定（D9）** —— 平台总数缺值**视为待同步**（宁可多跑一轮，不可漏）；
4. **轮内纪律** —— 同一账号有未完成任务就跳过；账号之间**真的停顿**（这一条顺带
   钉住 `scheduler_service` 里 `time` 的导入：`time` 没导入时 `time.sleep` 只在运行时炸，
   而「gap=0 时不调用」会让它躲过所有测试）。

持久化 store 守卫（§24.9）与 builtin 种子一致性的用例在 `test_scheduled_jobs.py`。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.core.config import get_settings
from backend.app.core.database import Base
from backend.app.core.security import encrypt_text
from backend.app.models import AccountCookieVersion, PlatformAccount, ScheduledJob, Task, User
from backend.app.models.note import Note
from backend.app.services import note_sync_service, scheduler_service as sched
from backend.app.services.account_service import CHANNEL_CREATOR, CHANNEL_WWW
from tests.backend.cookie_fixtures import bind_shop_account


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration

# 真实形态（2026-09-21 拆分）：带 creator 域令牌
CREATOR_COOKIES = (
    "galaxy_creator_session_id=abc123; "
    "access-token-creator.xiaohongshu.com=ctok123; "
    "customer-sso-sid=sso123; a1=xyz"
)
# 真实登录的 www jar 一定同时带 `web_session` 与 `id_token`（DEF-051）。
PC_COOKIES = "web_session=abc123; id_token=tok123; a1=xyz"
#: 千帆（ark）jar —— 形态必须带 `access-token-ark.*`，否则会被判到别的通道，
#: 而这轮「取不到票」的红会指错方向。
_ARK_COOKIES = "a1=x; access-token-ark.xiaohongshu.com=y"


def _setup(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sched-sync.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(sched, "SessionLocal", SessionLocal)

    db = SessionLocal()
    try:
        user = User(username="owner", password_hash="x")
        db.add(user)
        db.flush()
        # A：创作者链路（无 web_session）→ 可无人值守同步
        creator = PlatformAccount(
            user_id=user.id, platform="xhs", sub_type="creator", nickname="创作者"
        )
        # B：带 web_session → 需要用户粘贴令牌，定时轮不覆盖
        pc_ish = PlatformAccount(
            user_id=user.id, platform="xhs", sub_type="creator", nickname="网页会话"
        )
        # C：pc 账号 → 同样不在定时轮范围
        pc = PlatformAccount(user_id=user.id, platform="xhs", sub_type="pc", nickname="PC")
        db.add_all([creator, pc_ish, pc])
        db.flush()
        db.add_all(
            [
                _cookie(creator.id, CREATOR_COOKIES),
                _cookie(pc_ish.id, PC_COOKIES, platform=CHANNEL_WWW),
                _cookie(pc.id, PC_COOKIES, platform=CHANNEL_WWW),
            ]
        )
        #: 关系 + 状态 + 凭据各归其位（`shop_account_bindings` 整表移除，2026-09-22）。
        #: 定时轮给创作者链路同步商品笔记时取的就是这份 `ark` 令牌。
        bind_shop_account(db, creator, cookies_text=_ARK_COOKIES, shop_name="店")
        creator.profile_json = json.dumps({"notes_count": 100})
        db.commit()
        # DEF-066：user_id 一并带回（调度函数按行主收口账号范围，用例必须显式传）
        ids = {"creator": creator.id, "pc_ish": pc_ish.id, "pc": pc.id, "user": user.id}
    finally:
        db.close()
    return SessionLocal, ids


def _cookie(account_id: int, raw: str, platform: str = CHANNEL_CREATOR):
    """落一行信封（2026-09-21 起信封是唯一存储形态；平台由调用方按 jar 归属给）。"""
    from backend.app.services.account_service import dump_channel_envelope

    return AccountCookieVersion(
        platform_account_id=account_id,
        encrypted_cookies=encrypt_text(dump_channel_envelope({platform: raw})),
    )


# --------------------------------------------------------------------------- #
# DB 驱动装配（设计 15 §6.1，2026-09-24 重构后）
# --------------------------------------------------------------------------- #
def test_builtin_seeds_default_to_config_values():
    """内置 job 的**真实默认值**来自 config：两个同步默认关 ⇒ 种子行 paused。

    ⚠️ 刻意不 monkeypatch：把开关 patch 成 False 再断言「没注册」的守卫在默认值
    被改成 True 时依然全绿，等于没守（2026-09-22 用户口径：「先停止本地项目
    同步定时任务」——同步改为完全由用户手动触发）。
    """
    settings = get_settings()
    assert settings.xhs_note_sync_enabled is False, "笔记同步定时任务应默认关闭"
    assert settings.xhs_shop_goods_sync_enabled is False, "商品同步定时任务应默认关闭"

    from backend.app.services.scheduler_service import _builtin_seed_defaults

    seeds = _builtin_seed_defaults()
    assert seeds["system.note_sync"]["status"] == "paused"
    assert seeds["system.shop_goods_sync"]["status"] == "paused"
    # 巡检默认开（与随包种子一致）。
    # ⚠️ 2026-10-07：原先这里还断言 `seeds["system.no_traffic_cleanup"]["status"]
    # == "active"` —— 该任务已从注册表移除（用户口径「无流量笔记清理任务不应在
    # 任务列表展示」），**兜底字典里也必须没有它**：兜底是「初始化数据缺该键」时
    # 的播种来源，留一条就会把它重新播回任务列表（第三处漏删点）。
    assert seeds["system.cookie_health_check"]["status"] == "active"
    assert seeds["system.qianfan_health_check"]["status"] == "active"
    assert "system.no_traffic_cleanup" not in seeds


def test_enabled_switches_register_single_instance_jobs(monkeypatch):
    """同步开关打开 ⇒ 种子行 active ⇒ 启动同步注册，且带风控三件套。"""
    from backend.app.services.scheduler_service import build_scheduler, ensure_builtin_jobs, sync_jobs_from_db

    settings = get_settings()
    monkeypatch.setattr(settings, "xhs_note_sync_enabled", True, raising=False)
    monkeypatch.setattr(settings, "xhs_note_sync_interval_hours", 6, raising=False)
    monkeypatch.setattr(settings, "xhs_shop_goods_sync_enabled", True, raising=False)
    monkeypatch.setattr(settings, "xhs_shop_goods_sync_interval_hours", 6, raising=False)

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(sched, "SessionLocal", SessionLocal)

    db = SessionLocal()
    try:
        # DEF-066：内置行按用户播种 ⇒ 先有用户，ensure 才会铺出行
        db.add(User(username="switches", password_hash="x"))
        db.commit()
        ensure_builtin_jobs(db)
    finally:
        db.close()

    scheduler = build_scheduler()
    stats = sync_jobs_from_db(scheduler)
    assert stats["registered"] >= 2
    jobs = {job.id: job for job in scheduler.get_jobs()}
    sync_jobs = [
        job for job in jobs.values()
        if job.id.startswith("job_")
    ]
    assert sync_jobs, "至少注册了两个同步 job"
    for job in sync_jobs:
        # 两轮绝不相交：风控第三层（与旧 auto_tasks_runner 时代的断言同口径）
        assert job.max_instances == 1
        assert job.coalesce is True


def test_paused_rows_are_not_registered(monkeypatch):
    """paused 行不进调度器（同步侧只注册 active 行，设计 15 §6.1）。"""
    from backend.app.services.scheduler_service import build_scheduler, sync_jobs_from_db

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(sched, "SessionLocal", SessionLocal)

    db = SessionLocal()
    try:
        # R14：`interval` 档与 `interval_seconds` 列已删（换 cron 表达），
        # 夹具必须跟着换 —— 旧写法会在构造期抛未知 kwarg（DEF-064 记录）。
        paused = ScheduledJob(
            user_id=None, name="暂停的",
            job_key="system.monitoring_refresh", trigger_type="cron",
            cron_expression="0 0 * * * *", status="paused",
        )
        db.add(paused)
        db.commit()
        db.refresh(paused)
        paused_id = paused.id
    finally:
        db.close()

    scheduler = build_scheduler()
    sync_jobs_from_db(scheduler)
    ids = {job.id for job in scheduler.get_jobs()}
    # 只断言「暂停行没进调度器」：sync 会顺带 ensure_builtin_jobs（5 个活跃内置行），
    # 数总和没有意义。
    assert f"job_{paused_id}" not in ids


def test_scheduler_is_not_started_when_master_switch_is_off(monkeypatch):
    """总开关 `scheduler_enabled=False` 时**连调度器都不启动**（结构性闸门）。

    闸门在 `main.lifespan`（`if settings.scheduler_enabled:`）。⚠️ 这里把
    `init_db` / `start_scheduler` **以及队列恢复、三个 worker 唤醒、三个看门狗**
    全部打桩：否则进 lifespan 会拿**真实运行库**跑迁移 / 做队列恢复 / 起常驻
    worker 去认领真实数据 —— 测试就变成了对生产库的写操作。

    顺带守住「重启续跑 + 守活确实被接线」（比 AST 断言更强：走一遍真 lifespan
    看副作用）。顺序也是判据：**先复位再唤醒** —— 先唤醒会让 worker 去认领那些
    本该被复位成 `pending` 的 `running` 行（认领只认 `pending`，等于什么也没捞到）。
    """
    import asyncio

    from backend.app import main as main_module

    settings = get_settings()
    monkeypatch.setattr(settings, "scheduler_enabled", False, raising=False)
    monkeypatch.setattr(main_module, "init_db", lambda: None)

    # ⚠️ 以下七项**同样必须桩掉**（2026-09-28 补齐，此前一直漏着）：它们要么自开 Session
    #    写**真实运行库**、要么起常驻线程去认领真实数据 —— 与 `init_db` 同一条理由。
    #    漏桩的实际后果（本轮实测）：新增一个运行时配置键后，
    #    `seed_builtin_settings_from_initial_data()` 第一次真的写 ⇒ **全量回归把 6 行写进了
    #    `data/spider_xhs.db`**（2026-09-28 18:08:47，6 个用户各一行）。更早之前
    #    `recover_account_tasks()` 与 `ensure_account_task_worker()` 也一直在拿真库跑
    #    （前者当时恰好是 no-op、后者起了常驻 worker 线程）—— 只是因为「没造成可见破坏」
    #    而没被发现。守卫：`test_lifespan_steps_are_all_stubbed_by_this_module`。
    monkeypatch.setattr(main_module, "seed_builtin_jobs_from_initial_data", lambda: 0)
    monkeypatch.setattr(main_module, "seed_builtin_settings_from_initial_data", lambda: 0)
    # v2.24 内置提示词模板播种：**按用户各写一行**（自开 Session）⇒ 与上面两条同一条理由，
    # 不桩就会拿真实运行库写 `prompt_modules` / `prompt_templates`。
    monkeypatch.setattr(
        main_module, "seed_builtin_prompt_templates_for_all_users", lambda *a, **k: 0
    )
    #: ⚠️ 设计 40 数据包列规则的同型播种（2026-10-15 加）：**独立 Session + 逐用户 commit**
    #: ⇒ 不桩的话全量回归会拿**真实运行库**播种（写库）。
    #: 与上面几项同一语义：随安装附有 + 只补缺失不覆盖。
    monkeypatch.setattr(
        main_module, "seed_builtin_rules_for_all_users", lambda *a, **k: 0
    )
    # 设计 37 服务商种子播种：**按用户各写 6 家服务商 + N 个模型 + 11 个业务点映射**
    # （自开 Session）⇒ 与上面两条同一条理由，不桩就会拿真实运行库写
    # `llm_gateway_configs` / `llm_gateway_models` / `llm_business_providers`。
    # ⚠️ 守卫 `test_lifespan_steps_are_all_stubbed_by_this_module` 就是为这条存在的 ——
    #    它是本用例唯一被**自动发现**的漏桩（2026-10-14 实测：全量 1 failed）。
    monkeypatch.setattr(
        main_module, "seed_gateway_providers_for_all_users", lambda *a, **k: 0
    )
    monkeypatch.setattr(main_module, "recover_account_tasks", lambda *a, **k: 0)
    #: 违规词种子（2026-10-21 加进 lifespan）：**自开 Session 逐用户写库** ⇒ 同一条理由。
    #: ⚠️ 它此前一直漏桩（守卫在 2026-10-24 S1 轮才报出来）—— 漏桩的表现是**测试全绿**
    #: 而副作用落在真实库上，正是 `test_lifespan_steps_are_all_stubbed_by_this_module`
    #: 存在的理由。
    monkeypatch.setattr(
        main_module, "seed_builtin_words_for_all_users", lambda *a, **k: 0
    )
    #: v1.1.0 S1 系统管理域种子（角色 / 菜单 / 字典 / 出厂 admin / 授权）：
    #: **自开 Session 写库** ⇒ 与上面几条同一条理由，不桩就会拿真实运行库播种。
    monkeypatch.setattr(main_module, "seed_system_domain", lambda *a, **k: {})
    monkeypatch.setattr(main_module, "start_account_task_watchdog", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "stop_account_task_watchdog", lambda: None)
    monkeypatch.setattr(main_module, "start_scheduler_watchdog", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "stop_scheduler_watchdog", lambda: None)
    monkeypatch.setattr(main_module, "shutdown_scheduler", lambda *a, **k: None)
    # ⚠️ M9 新增两步**同样必须桩掉**：
    #    ① `log_startup_selfcheck()` —— 真跑 `node --version`（起子进程；不写库但也
    #       不该在单测里真的执行环境探测）；
    #    ② `acquire_scheduler_lease()` —— 会在**真实 data/ 目录**创建租约文件
    #       （比写库更隐蔽：它不碰 DB，但会留下一个锁文件，且抢到/抢不到会
    #       改变 `start_scheduler` 是否被调用 —— 正是本用例要断言的因果链）。
    #    `_data_dir_for_lease` 纯函数（只算路径）⇒ 无需桩，登记进白名单。
    monkeypatch.setattr(main_module, "log_startup_selfcheck", lambda *a, **k: {"node_ok": True})
    monkeypatch.setattr(
        main_module, "acquire_scheduler_lease", lambda *a, **k: (True, "SPY-LEASE")
    )
    monkeypatch.setattr(main_module, "release_scheduler_lease", lambda *a, **k: None)

    started: list[object] = []
    monkeypatch.setattr(
        main_module, "start_scheduler",
        lambda *a, **k: started.append(True) or "SPY-SCHEDULER",
    )

    recoveries: list[str] = []
    woken: list[str] = []

    class _StubRecovery:
        def recover_interrupted(self, _db):
            recoveries.append("interrupted")
            return {"requeued": 0, "failed": 0}

        def recover_publishing_notes(self, _db):
            recoveries.append("publishing")
            return 0

    class _StubSession:
        def close(self):
            pass

    monkeypatch.setattr(main_module, "note_queue_service", _StubRecovery())
    monkeypatch.setattr(main_module, "SessionLocal", _StubSession)
    monkeypatch.setattr(main_module, "ensure_sync_worker", lambda: woken.append("sync"))
    monkeypatch.setattr(main_module, "ensure_publish_worker", lambda: woken.append("publish"))
    monkeypatch.setattr(main_module, "ensure_generate_worker", lambda: woken.append("generate"))
    monkeypatch.setattr(main_module, "ensure_llm_generate_worker", lambda: woken.append("generate_llm"))
    # ⚠️ 这两条是**补的漏桩**（2026-10-11，A1b）：它们后来才加进 `lifespan`，
    #    桩清单没同步 ⇒ 用例真的对**生产库**起了两条常驻线程
    #    （`ensure_account_task_worker` / `ensure_goods_sync_worker` 都会认领真实队列项）。
    #    表现是**全绿**、副作用落在真库上 —— 正是 `test_lifespan_steps_are_all_stubbed_by_this_module`
    #    要防的那种事故（同 2026-09-28 写库那次的形态）。
    monkeypatch.setattr(main_module, "ensure_goods_sync_worker", lambda: woken.append("goods_sync"))
    monkeypatch.setattr(main_module, "ensure_account_task_worker", lambda: woken.append("account_task"))
    monkeypatch.setattr(main_module, "start_sync_worker_watchdog", lambda **k: None)
    monkeypatch.setattr(main_module, "stop_sync_worker_watchdog", lambda: None)
    monkeypatch.setattr(main_module, "start_publish_worker_watchdog", lambda **k: None)
    monkeypatch.setattr(main_module, "stop_publish_worker_watchdog", lambda: None)
    monkeypatch.setattr(main_module, "start_generate_worker_watchdog", lambda **k: None)
    monkeypatch.setattr(main_module, "stop_generate_worker_watchdog", lambda: None)
    monkeypatch.setattr(main_module, "start_llm_generate_worker_watchdog", lambda **k: None)
    monkeypatch.setattr(main_module, "stop_llm_generate_worker_watchdog", lambda: None)

    app = main_module.app

    async def _enter() -> object:
        await app.router.lifespan_context(app).__aenter__()
        return app.state.scheduler

    try:
        scheduler = asyncio.run(_enter())
        assert started == [], "总开关关着时不该启动调度器"
        assert scheduler is None
        assert recoveries == ["interrupted", "publishing"], (
            "启动必须先复位中断队列项、再复位卡在 publishing 的笔记"
        )
        assert woken == ["sync", "publish", "generate", "generate_llm", "goods_sync", "account_task"], (
            "复位完必须**立刻**唤醒**全部** worker（等看门狗 30 秒巡检 = 积压白等半分钟）。"
            "⚠️ 名单按 `lifespan` 里的真实调用顺序：漏一个就等于漏了一条**对真库起线程**的路径。"
        )
    finally:
        try:
            asyncio.run(app.router.lifespan_context(app).__aexit__(None, None, None))
        except Exception:  # noqa: BLE001 - 测试收尾不影响结论
            pass


# --------------------------------------------------------------------------- #
# 进真 lifespan 的用例：**每个**步骤都必须打桩（防「测试写生产库」）
# --------------------------------------------------------------------------- #
#: `lifespan` 里**允许不打桩**的裸名调用（纯读 / 内置，无副作用）。
LIFESPAN_STUB_FREE_CALLS = {"get_settings", "getattr", "_data_dir_for_lease"}

#: 上面那个「进真 lifespan」用例的函数名（桩清单从它体内取，见守卫）。
LIFESPAN_TEST_NAME = "test_scheduler_is_not_started_when_master_switch_is_off"


def _lifespan_test_stubs() -> set[str]:
    """从**那个用例体内**提取 `monkeypatch.setattr(main_module, "X", …)` 的 `X` 集合。

    ⚠️ 两个「不」：不从整个文件取（别处的桩会把它凑绿 —— 宽锚点 = 假绿）；
    不在守卫里再抄一份名单（两份真相迟早漂移：加桩的人只改一处，守卫拿旧名单判，
    红绿都可能失真）。判据的唯一真相源 = 那段源码本身。
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    owner = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == LIFESPAN_TEST_NAME
    )
    names: set[str] = set()
    for node in ast.walk(owner):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "setattr"):
            continue
        if len(node.args) < 2:
            continue
        target, attribute = node.args[0], node.args[1]
        if (
            isinstance(target, ast.Name)
            and target.id == "main_module"
            and isinstance(attribute, ast.Constant)
        ):
            names.add(str(attribute.value))
    return names


def test_lifespan_steps_are_all_stubbed_by_this_module():
    """`lifespan` 里**每个**裸名调用都必须被「进真 lifespan」那个用例桩掉（或登记为无需桩）。

    ⚠️ 为什么要有这条（2026-09-28 实测踩到）：`lifespan` 里两个**播种**步骤
    （`seed_builtin_jobs_from_initial_data` / `seed_builtin_settings_from_initial_data`）
    会自开 Session 写库（`settings_service.SessionLocal`）。此前它们一直是 no-op
    （内置行都在），漏桩看不出问题；本轮新增一个运行时配置键后播种第一次真的写
    ⇒ **全量回归把 6 行写进了真实运行库 `data/spider_xhs.db`**。同批漏桩的还有
    `recover_account_tasks()`（写库）、`ensure_account_task_worker()` 与
    `start_account_task_watchdog()`（起常驻线程认领真实数据）。

    判据是 **AST 集合包含**，不是「测试没炸」—— 漏桩的表现恰恰是**测试全绿**，
    而副作用已经落在生产库上。
    """
    import ast
    from pathlib import Path

    from backend.app import main as main_module

    tree = ast.parse(Path(main_module.__file__).read_text(encoding="utf-8"))
    lifespan = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "lifespan"
    )
    called = {
        node.func.id
        for node in ast.walk(lifespan)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    missing = sorted(called - _lifespan_test_stubs() - LIFESPAN_STUB_FREE_CALLS)
    assert not missing, (
        f"lifespan 里有没被本文件桩掉的步骤：{missing} —— 它们会拿**真实运行库**执行"
        "（写库 / 起常驻线程）。请在本文件的桩清单里补上；确实无需桩的（纯读 / 内置）"
        "登记进 LIFESPAN_STUB_FREE_CALLS 并写明理由。"
    )


# --------------------------------------------------------------------------- #
# 「有待同步」判定（D9）
# --------------------------------------------------------------------------- #
def test_unknown_platform_total_counts_as_needing_sync(tmp_path, monkeypatch):
    SessionLocal, ids = _setup(tmp_path, monkeypatch)
    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, ids["creator"])
        account.profile_json = "{}"  # 平台总数缺值
        assert sched.account_needs_note_sync(db, account) is True
    finally:
        db.close()


def test_needs_sync_compares_local_count_against_platform_total(tmp_path, monkeypatch):
    SessionLocal, ids = _setup(tmp_path, monkeypatch)
    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, ids["creator"])  # notes_count = 100
        assert sched.account_needs_note_sync(db, account) is True  # 本地 0 < 100

        for index in range(100):
            db.add(
                Note(
                    user_id=account.user_id,
                    platform_account_id=account.id,
                    platform="xhs",
                    note_id=f"N{index}",
                )
            )
        db.commit()
        assert sched.account_needs_note_sync(db, account) is False  # 已追平
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 轮内：只覆盖能无人值守的账号
# --------------------------------------------------------------------------- #
def test_only_creator_link_accounts_are_scheduled(tmp_path, monkeypatch):
    """定时轮只覆盖**有创作者/千帆令牌的创作者账号**（§24.9 修订理由）。

    ⚠️ 2026-09-23：判据从「有 creator 令牌**且没有 www 网页会话」收紧为
    「有 creator/ark 令牌」—— www 源已整体退场（§21.4），同步不再需要 URL 级
    `xsec_token`，「有 www 会话就要留给手动」的前提不存在了。下面
    `test_creator_account_with_www_session_is_now_schedulable` 钉新判据。
    """
    SessionLocal, ids = _setup(tmp_path, monkeypatch)
    db = SessionLocal()
    try:
        accounts = db.scalars(select(PlatformAccount)).all()
        picked = {account.id for account in sched._schedulable_accounts(db, list(accounts))}
    finally:
        db.close()

    assert picked == {ids["creator"]}
    # `pc_ish` 只有 www 通道（没有 creator/ark 令牌）⇒ 排除；
    # `pc` 是 pc 链路账号 ⇒ 排除。
    assert ids["pc_ish"] not in picked
    assert ids["pc"] not in picked


def test_creator_account_with_www_session_is_now_schedulable(tmp_path, monkeypatch):
    """§24.9：带 www 网页会话**不再是**排除理由（观隅好物 acct 8 同款形状）。

    旧实现的 `has_web_session(www jar)` 过滤会把三通道齐全的账号永久排除出
    定时轮 —— 那是 www 源时代「PC 链路要用户粘贴 xsec_token」的残留。§21.4
    起同步源只剩 creator + ark，不需要 xsec_token ⇒ 这类账号必须进定时轮。
    """
    SessionLocal, _ids = _setup(tmp_path, monkeypatch)
    db = SessionLocal()
    try:
        user = db.scalars(select(User)).first()
        account = PlatformAccount(
            user_id=user.id, platform="xhs", sub_type="creator", nickname="三通道"
        )
        db.add(account)
        db.flush()
        db.add_all(
            [
                _cookie(account.id, CREATOR_COOKIES),
                _cookie(account.id, PC_COOKIES, platform=CHANNEL_WWW),
            ]
        )
        db.commit()
        picked = {a.id for a in sched._schedulable_accounts(db, [account])}
    finally:
        db.close()

    assert picked == {account.id}


def test_scheduled_run_syncs_due_accounts_and_pauses_between_them(tmp_path, monkeypatch):
    SessionLocal, ids = _setup(tmp_path, monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        note_sync_service,
        "run_note_sync",
        lambda account_id, **kw: calls.append((account_id, kw.get("source"))),
    )
    monkeypatch.setattr(sched, "_sync_account_gap_seconds", lambda: 3)
    slept: list = []
    # 这一行同时钉住 `scheduler_service` 里 `time` 的导入（没导入就没有可打的属性）。
    monkeypatch.setattr(sched.time, "sleep", lambda seconds: slept.append(seconds))

    sched.run_scheduled_note_syncs(ids["user"])

    assert calls == [(ids["creator"], "scheduled")]
    assert slept == [3]


def test_scheduled_run_skips_accounts_with_an_active_task(tmp_path, monkeypatch):
    SessionLocal, ids = _setup(tmp_path, monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        note_sync_service, "run_note_sync", lambda account_id, **kw: calls.append(account_id)
    )
    monkeypatch.setattr(sched, "_sync_account_gap_seconds", lambda: 0)

    db = SessionLocal()
    try:
        db.add(
            Task(
                user_id=1,
                platform="xhs",
                task_type="xhs_note_sync",
                status="running",
                payload={"account_id": ids["creator"]},
            )
        )
        db.commit()
    finally:
        db.close()

    sched.run_scheduled_note_syncs(ids["user"])

    assert calls == []  # 手动任务在跑 → 本轮跳过该账号（两道入口共用同一条判断）


def test_scheduled_run_swallows_per_account_failures(tmp_path, monkeypatch):
    SessionLocal, ids = _setup(tmp_path, monkeypatch)

    def boom(account_id, **kw):
        raise RuntimeError("平台炸了")

    monkeypatch.setattr(note_sync_service, "run_note_sync", boom)
    monkeypatch.setattr(sched, "_sync_account_gap_seconds", lambda: 0)

    sched.run_scheduled_note_syncs(ids["user"])  # 不抛 = 单个账号失败不拖垮整轮


# --------------------------------------------------------------------------- #
# 商品同步的调度轮：只跑有绑定的账号
# --------------------------------------------------------------------------- #
def test_goods_run_only_touches_bound_accounts(tmp_path, monkeypatch):
    SessionLocal, ids = _setup(tmp_path, monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        sched, "_sync_account_gap_seconds", lambda: 0
    )
    import backend.app.services.shop_goods_sync_service as sgs

    monkeypatch.setattr(
        sgs, "run_shop_goods_sync", lambda account_id, **kw: calls.append(account_id)
    )

    sched.run_scheduled_shop_goods_syncs(ids["user"])

    assert calls == [ids["creator"]]  # 只有它绑了店


def test_goods_run_swallows_failures(tmp_path, monkeypatch):
    SessionLocal, ids = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sched, "_sync_account_gap_seconds", lambda: 0)
    import backend.app.services.shop_goods_sync_service as sgs

    def boom(account_id, **kw):
        raise RuntimeError("千帆炸了")

    monkeypatch.setattr(sgs, "run_shop_goods_sync", boom)

    sched.run_scheduled_shop_goods_syncs(ids["user"])


# --------------------------------------------------------------------------- #
# 配置口径
# --------------------------------------------------------------------------- #
def test_sync_switches_default_to_off_and_have_yaml_aliases():
    settings = get_settings()
    assert settings.xhs_note_sync_enabled is False
    assert settings.xhs_shop_goods_sync_enabled is False
    assert settings.xhs_note_sync_interval_hours == 6
    assert settings.xhs_shop_goods_sync_interval_hours == 6
    assert settings.xhs_sync_account_gap_seconds == 10

    from backend.app.core.config import Settings

    mapping = Settings.model_fields["xhs_note_sync_enabled"]
    assert mapping is not None  # 字段存在
    source = open(
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "backend/app/core/config.py",
        encoding="utf-8",
    ).read()
    for yaml_key in (
        "xhs.note_sync.enabled",
        "xhs.note_sync.interval_hours",
        "xhs.shop_goods_sync.enabled",
        "xhs.shop_goods_sync.interval_hours",
        "xhs.sync.account_gap_seconds",
    ):
        assert f'"{yaml_key}"' in source, f"YAML 键 {yaml_key} 未登记到 alias 表"
