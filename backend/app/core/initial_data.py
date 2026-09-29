"""应用初始化数据（设计 15 §4.5）—— **随安装包分发**的基线数据。

**为什么要单独一层**：定时任务的种子一度写在两处（迁移写死 + 运行期读 config），
结果是**新装库拿到的是过时形态**（间隔周期档已退役、启停口径也变了），而判重逻辑
又让它修不掉。用户口径（2026-09-24）：「将当前的定时任务配置数据定义为应用初始化
数据，随应用的安装自动附有」⇒ 种子**只能有一份真相源**，且它必须**随包走**
（写死在迁移里、写死在代码里，都不是「随安装附有」）。

**定位（双路，与 `config/default.yaml` 同构）**：

1. `CONFIG_FILE` 所在目录下的 `initial_data/<文件名>` —— 安装版即
   `<安装目录>/config/initial_data/…`，用户可见、可编辑（启动器注 `CONFIG_FILE`，
   见设计 14 §3）；
2. `<project_root>/config/initial_data/<文件名>` —— dev 仓库根 / frozen 下
   `_internal/config/…` 的兜底（与 `core/config.py::_load_yaml_config()` 同口径）。

**先命中者胜**。文件缺失 / JSON 坏 / 结构不符 ⇒ **显式 warning 一行**并返回空列表
（调用方退化为代码默认）—— 不静默、不抛异常：初始化数据是**增强**，它坏掉不该让
应用起不来，但也不能装作没事（`build.ps1` 与 `SpiderXHS.spec` 另有打包侧正向断言）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 数据目录名（与 `config/` 同级）
INITIAL_DATA_DIRNAME = "initial_data"

#: 定时任务初始化数据文件名
SCHEDULED_JOBS_FILENAME = "scheduled_jobs.json"

#: 运行时配置初始化数据文件名（设计 10 §7.3，2026-09-27）
SYSTEM_SETTINGS_FILENAME = "system_settings.json"


def _project_root() -> Path:
    """与 `core/config.py::_load_yaml_config()` 同一口径（frozen 下 = `_internal/`）。"""
    return Path(__file__).resolve().parent.parent.parent.parent


def initial_data_candidates(filename: str) -> list[Path]:
    """候选路径（有序，先命中者胜）。顺序与 `config/default.yaml` 的双路一致。"""
    candidates: list[Path] = []
    config_file = os.environ.get("CONFIG_FILE")
    if config_file:
        config_path = Path(config_file)
        if not config_path.is_absolute():
            config_path = _project_root() / config_path
        candidates.append(config_path.parent / INITIAL_DATA_DIRNAME / filename)
    candidates.append(_project_root() / "config" / INITIAL_DATA_DIRNAME / filename)
    return candidates


def resolve_initial_data_file(filename: str) -> Path | None:
    """命中的初始化数据文件路径（都找不到 ⇒ `None`）。诊断/测试用。"""
    for path in initial_data_candidates(filename):
        if path.is_file():
            return path
    return None


def _load_payload(filename: str) -> Any:
    """读并解析初始化数据；缺失 / 读不动 / JSON 坏 ⇒ `None` + warning。"""
    path = resolve_initial_data_file(filename)
    if path is None:
        logger.warning(
            "initial_data: %s 不存在（候选：%s）—— 本次按「无初始化数据」处理",
            filename,
            " / ".join(str(item) for item in initial_data_candidates(filename)),
        )
        return None
    try:
        # ⚠️ 用 `utf-8-sig` 而不是 `utf-8`：这两个文件**用户可见、可编辑**（安装目录下），
        # 一个被 Windows 编辑器加上 BOM 的副本会让 `json.load` 抛 `JSONDecodeError`
        # ⇒ 整份种子静默退化成代码默认（2026-09-27 实测踩到过一次）。
        # 随包出厂的形态由测试钉住「必须是**无 BOM** 的纯 UTF-8」，读取侧则一律宽容。
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:  # noqa: BLE001 - 坏数据不是致命错误
        logger.warning("initial_data: %s 解析失败（%s: %s）", path, type(exc).__name__, exc)
        return None


def _normalize_scheduled_job(raw: Any) -> dict[str, Any] | None:
    """单条定时任务初始化数据 → 规范 dict；字段非法 ⇒ `None`（丢弃该条 + warning）。

    白名单一律从 `models/scheduled_job.py` 的常量取 —— **不在这里重抄一份**
    （枚举漂移即缺陷）。坏值只丢该条，不牵连其余（部分可用胜过全丢）。
    """
    # 惰性 import：`core` 层不在模块级依赖 `models`（避免 core ↔ models 的导入环）。
    from backend.app.models.scheduled_job import (
        CONCURRENT_FORBID,
        CONCURRENT_MODES,
        JOB_STATUSES,
        MISFIRE_ONCE,
        MISFIRE_POLICIES,
        TRIGGER_CRON,
        TRIGGER_TYPES,
    )

    if not isinstance(raw, dict):
        logger.warning("initial_data: 任务条目不是对象（%r），已跳过", type(raw).__name__)
        return None

    job_key = raw.get("job_key")
    if not isinstance(job_key, str) or not job_key.strip():
        logger.warning("initial_data: 任务条目缺少 job_key，已跳过")
        return None
    job_key = job_key.strip()

    trigger_type = str(raw.get("trigger_type") or TRIGGER_CRON)
    if trigger_type not in TRIGGER_TYPES:
        logger.warning(
            "initial_data: %s 的 trigger_type=%r 不在白名单 %s，已跳过该条",
            job_key,
            trigger_type,
            list(TRIGGER_TYPES),
        )
        return None

    status = str(raw.get("status") or "")
    if status not in JOB_STATUSES:
        logger.warning(
            "initial_data: %s 的 status=%r 不在白名单 %s，已跳过该条",
            job_key,
            status,
            list(JOB_STATUSES),
        )
        return None

    cron_expression = str(raw.get("cron_expression") or "").strip()
    if trigger_type == TRIGGER_CRON and not cron_expression:
        logger.warning("initial_data: %s 是 cron 档但 cron_expression 为空，已跳过该条", job_key)
        return None

    misfire_policy = str(raw.get("misfire_policy") or MISFIRE_ONCE)
    if misfire_policy not in MISFIRE_POLICIES:
        logger.warning(
            "initial_data: %s 的 misfire_policy=%r 非法，改用默认 %s",
            job_key,
            misfire_policy,
            MISFIRE_ONCE,
        )
        misfire_policy = MISFIRE_ONCE

    concurrent = str(raw.get("concurrent") or CONCURRENT_FORBID)
    if concurrent not in CONCURRENT_MODES:
        logger.warning(
            "initial_data: %s 的 concurrent=%r 非法，改用默认 %s",
            job_key,
            concurrent,
            CONCURRENT_FORBID,
        )
        concurrent = CONCURRENT_FORBID

    params = raw.get("params")
    if params is not None and not isinstance(params, dict):
        logger.warning("initial_data: %s 的 params 不是对象，按空对象处理", job_key)
        params = None

    name = raw.get("name")
    remark = raw.get("remark")

    return {
        "job_key": job_key,
        "name": str(name).strip() if isinstance(name, str) and name.strip() else job_key,
        "trigger_type": trigger_type,
        "cron_expression": cron_expression,
        "misfire_policy": misfire_policy,
        "concurrent": concurrent,
        "params": dict(params) if params else {},
        "status": status,
        "remark": str(remark) if isinstance(remark, str) and remark.strip() else "",
    }


def load_initial_scheduled_jobs() -> list[dict[str, Any]]:
    """定时任务应用初始化数据（规范化后的列表；任何问题 ⇒ warning + 可用部分）。

    返回顺序 = 文件顺序（播种按 job_key 索引取值，顺序不影响结果）。
    **故意不在这里做「key 是否在注册表里」的校验** —— 那是 `job_registry` 的职责，
    由播种方过滤（不插没有 func 的死行）。
    """
    payload = _load_payload(SCHEDULED_JOBS_FILENAME)
    if payload is None:
        return []
    if not isinstance(payload, dict):
        logger.warning("initial_data: %s 顶层不是对象，已忽略", SCHEDULED_JOBS_FILENAME)
        return []
    rows = payload.get("scheduled_jobs")
    if not isinstance(rows, list):
        logger.warning("initial_data: %s 缺少 scheduled_jobs 数组，已忽略", SCHEDULED_JOBS_FILENAME)
        return []

    normalized: list[dict[str, Any]] = []
    for raw in rows:
        item = _normalize_scheduled_job(raw)
        if item is not None:
            normalized.append(item)
    logger.info("initial_data: 载入 %s 条定时任务初始化数据", len(normalized))
    return normalized


def _normalize_system_setting(raw: Any) -> dict[str, Any] | None:
    """单条运行时配置初始化数据 → 规范 dict；缺 key ⇒ `None`（丢弃该条 + warning）。

    ⚠️ **不在这一层校验 `value_type` 白名单**：值类型归一化与坏值兜底是
    `settings_service` 的职责（唯一实现，DEF-044），这里只做「结构是否像一行配置」。
    在两层各写一份类型判断，迟早一边改了另一边没改。
    """
    if not isinstance(raw, dict):
        logger.warning("initial_data: 配置条目不是对象（%r），已跳过", type(raw).__name__)
        return None
    key = raw.get("key")
    if not isinstance(key, str) or not key.strip():
        logger.warning("initial_data: 配置条目缺少 key，已跳过")
        return None
    return {
        "key": key.strip(),
        "value": raw.get("value"),
        "value_type": str(raw.get("value_type") or ""),
        "category": str(raw.get("category") or ""),
        "label": str(raw.get("label") or ""),
        "description": str(raw.get("description") or ""),
        "requires_restart": bool(raw.get("requires_restart", False)),
        "is_dangerous": bool(raw.get("is_dangerous", False)),
    }


def load_initial_system_settings() -> list[dict[str, Any]]:
    """运行时配置应用初始化数据（规范化后的列表；任何问题 ⇒ warning + 可用部分）。

    与 `load_initial_scheduled_jobs` 同构（双路解析、坏数据不致命）。
    """
    payload = _load_payload(SYSTEM_SETTINGS_FILENAME)
    if payload is None:
        return []
    if not isinstance(payload, dict):
        logger.warning("initial_data: %s 顶层不是对象，已忽略", SYSTEM_SETTINGS_FILENAME)
        return []
    rows = payload.get("system_settings")
    if not isinstance(rows, list):
        logger.warning(
            "initial_data: %s 缺少 system_settings 数组，已忽略", SYSTEM_SETTINGS_FILENAME
        )
        return []

    normalized: list[dict[str, Any]] = []
    for raw in rows:
        item = _normalize_system_setting(raw)
        if item is not None:
            normalized.append(item)
    logger.info("initial_data: 载入 %s 条运行时配置初始化数据", len(normalized))
    return normalized
