"""任务中心两级架构守卫（设计 69，2026-10-25 用户口径）。

用户口径：
  ① 「任务中心分为系统级别和工作区域级别，工作区域为每个区域中私有」
  ② 「暂时不允许用户创建任务」
  ③ 「不在保存用户级的任务中心，收为公共」

这些守卫钉的是**架构判据**，换实现但判据不成立时必须红。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend" / "app"


def test_job_definitions_are_unique_per_job_key():
    """**一种任务只有一行定义** —— 不按用户复制（设计 69 §2）。

    ⚠️ 这是本次重构的核心判据。DEF-066 曾按用户各持一份，结果 403 行
    （31 用户 × 13 任务）里同一个「清理记录表」重复 31 次，改一次 Cron 要改
    31 处，系统管理页看不出任务内容。

    判据落在**模型约束**上（`job_key` 唯一），不是「播种代码碰巧没多建」。
    """
    from sqlalchemy import inspect

    from backend.app.models.scheduled_job import ScheduledJob

    table = ScheduledJob.__table__
    unique_col_sets = set()
    for constraint in table.constraints:
        cols = tuple(sorted(c.name for c in getattr(constraint, "columns", [])))
        if cols and constraint.__class__.__name__ in {"UniqueConstraint"}:
            unique_col_sets.add(cols)
    for index in table.indexes:
        if index.unique:
            unique_col_sets.add(tuple(sorted(c.name for c in index.columns)))

    assert ("job_key",) in unique_col_sets, (
        "`job_key` 不是唯一约束 ⇒ 又可能按用户复制定义行（设计 69 禁止）"
    )
    #: `(job_key, user_id)` 是 DEF-066 的旧唯一键，必须已退场。
    assert ("job_key", "user_id") not in unique_col_sets, (
        "旧的 `(job_key, user_id)` 唯一键还在 ⇒ 同一个任务可以在不同用户下各存一份"
    )
    assert inspect(ScheduledJob).columns["user_id"].nullable, (
        "`user_id` 必须可空（它是最后修改人，内置行没人改过）"
    )


def test_every_job_declares_its_level():
    """每个注册表任务都必须归到 `system` 或业务工作区。"""
    from backend.app.services.job_registry import _build_registry, workspace_for_job

    registry = _build_registry()
    assert registry, "注册表为空（判据前提不成立）"
    for key in registry:
        assert workspace_for_job(key) in {"system", "xhs"}, (
            f"{key} 的工作区归属非法：{workspace_for_job(key)}"
        )


def test_system_level_jobs_are_platform_agnostic():
    """**系统级任务不得依赖平台适配器**（设计 69 §2.1 的判据）。

    判据：「换一个平台接入，这个任务还要不要存在？」——
    系统级任务是清本系统自己的资源（记录表 / 暂存目录 / 图片空间），
    接抖音后照样要跑；工作区级任务只操作某个平台的数据。

    ⚠️ 这条如果失守，意味着某个平台专属任务被错放成系统级 ——
    它的执行函数会 import 平台 SDK，在一个「与平台无关」的级别上跑平台调用。
    """
    import inspect

    from backend.app.services.job_registry import (
        _build_registry,
        workspace_for_job,
    )

    registry = _build_registry()
    offenders: list[str] = []
    for key, spec in registry.items():
        if workspace_for_job(key) != "system":
            continue
        func = spec.func
        try:
            source = inspect.getsource(func)
        except (OSError, TypeError):  # pragma: no cover - 动态函数
            continue
        #: 平台适配器/平台服务出现在源码里 ⇒ 这个任务不是平台无关的。
        if re.search(r"adapters\.xhs|qianfan_apis|creator_apis|platform_account", source):
            offenders.append(key)
    assert not offenders, (
        f"这些系统级任务引用了平台相关代码，应归工作区域级：{offenders}"
    )


def test_user_scoped_jobs_fan_out_instead_of_failing():
    """`user_scoped` 任务必须**遍历用户执行**，不能因 `user_id` 为空报失败。

    ⚠️ 实测踩过：改成公共定义后 `job.user_id` 变 NULL，旧代码据此判
    「任务行未归属用户，拒绝执行」⇒ 所有同步类任务全部失败且不跑任何账号。
    """
    source = (BACKEND / "services" / "scheduled_job_runner.py").read_text(encoding="utf-8")
    assert "_fan_out(" in source, "没有 fan-out 实现"
    assert "_platform_data_owners(" in source, "没有「遍历有账号的用户」的实现"
    #: 旧的「未归属就失败」判据必须已删除。
    assert "任务行未归属用户（user_id 为空），拒绝执行" not in source, (
        "旧的「user_id 为空即拒绝执行」判据还在 ⇒ 公共定义下所有同步任务都会失败"
    )


def test_each_fan_out_execution_records_its_target_user():
    """fan-out 的每条子日志必须记 `executed_for_user_id`。

    否则 12 个用户跑完只留 12 条一模一样的「成功」，
    用户排查「我这次为什么没同步成功」时无法定位到自己那条。
    """
    from sqlalchemy import inspect

    from backend.app.models.scheduled_job import ScheduledJobLog

    cols = {c.name for c in inspect(ScheduledJobLog).columns}
    assert "executed_for_user_id" in cols, "日志表缺 executed_for_user_id"
    #: 它必须与 `user_id` 分开（后者已改为"最后修改人"，语义不同）。
    assert "user_id" in cols, "`user_id` 被删了 —— 它是最后修改人快照，要保留"


def test_user_facing_task_center_is_gone():
    """用户侧任务中心已下线（用户口径「暂时不允许用户创建任务」）。"""
    router = (ROOT / "frontend" / "src" / "app" / "router.tsx").read_text(encoding="utf-8")
    assert 'path="/tasks" element={<TaskCenterPage' not in router, (
        "用户侧任务中心页面还在挂着（用户口径：暂不允许用户创建任务）"
    )
    #: 旧深链要被接住（重定向到系统任务中心），不是 404。
    assert 'path="/tasks"' in router and "Navigate" in router, (
        "/tasks 既没页面也没重定向 —— 旧书签会 404"
    )

    registry = (BACKEND / "core" / "perms_registry.py").read_text(encoding="utf-8")
    assert "xhs:job:" not in registry, "用户级任务权限码（xhs:job:*）仍在注册表里"


def test_job_endpoints_require_admin_codes():
    """任务读写端点必须挂 admin 权限码（`system:scheduler:*`）。"""
    perms = (BACKEND / "core" / "route_perms.py").read_text(encoding="utf-8")
    for route in (
        "POST /api/schedule/jobs",
        "PUT /api/schedule/jobs/{job_id}",
        "DELETE /api/schedule/jobs/{job_id}",
    ):
        assert route in perms, f"{route} 没登记权限码"
    assert "xhs:job:" not in perms, "写端点还挂在用户级任务码上"
    #: 2026-10-25 红线「一按钮一码」⇒ `system:scheduler:manage` 这个合并码已拆成
    #: create / edit / delete / run / toggle / cleanLogs / deleteLogs，
    #: 各自挂在对应端点上（见 `test_button_level_perms.py`）。
    for code in (
        "system:scheduler:create",
        "system:scheduler:edit",
        "system:scheduler:delete",
    ):
        assert code in perms, f"按钮码 {code} 没登记"


def test_job_list_no_longer_filters_by_user():
    """任务列表端点**不得**再按 `user_id` 过滤（定义已公共）。

    ⚠️ 实测踩过：`user_id` 置 NULL 后，`WHERE user_id == 当前用户` 让 admin
    一条也查不到 —— 页面空表，但接口 200、无任何报错（静默失效）。
    """
    source = (BACKEND / "api" / "schedule.py").read_text(encoding="utf-8")
    offenders = re.findall(r"Scheduled(?:Job|JobLog)\.user_id == current_user\.id", source)
    assert not offenders, (
        f"还有 {len(offenders)} 处按 user_id 过滤任务/日志 —— 公共定义下会查不到任何行"
    )


def test_system_level_seeding_does_not_recreate_per_user_rows():
    """播种必须按 `job_key` 判重（不是 `(job_key, user_id)`）。"""
    source = (BACKEND / "services" / "scheduler_service.py").read_text(encoding="utf-8")
    start = source.index("def seed_builtin_jobs(")
    end = source.index("\ndef ", start + 10)
    body = source[start:end]
    assert "select(ScheduledJob.job_key)" in body, (
        "播种没按 job_key 全表判重 —— 会重新按用户复制定义行"
    )
    assert "user_id=None" in body, "内置种子行的 user_id 应为 None（最后修改人为空）"
