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

#: LLM 网关预警规则初始化数据文件名（设计 17 §4.5，2026-10-02）
GATEWAY_ALERTS_FILENAME = "llm_gateway_alerts.json"

#: 内置提示词模板 / 模块初始化数据文件名（设计 17 §13.5.4，v2.24）
PROMPT_TEMPLATES_FILENAME = "prompt_templates.json"

#: LLM 网关服务商种子初始化数据文件名（2026-10-13 用户口径「国内常见 6 个服务商作为种子」）
GATEWAY_PROVIDERS_FILENAME = "llm_gateway_providers.json"


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


def _normalize_gateway_alert(raw: Any) -> dict[str, Any] | None:
    """单条预警规则初始化数据 → 规范 dict；字段非法 ⇒ `None`（丢弃该条 + warning）。

    白名单从 `api/llm_gateway_monitor.py` 的常量取 —— **不在这里重抄一份**
    （`metric`/`operator` 枚举漂移即缺陷，与 `_normalize_scheduled_job` 同纪律）。
    `gateway_id` **不在种子里**：它由播种方按「刚创建的网关 / 用户已有网关」填，
    种子只描述**规则本身**（一条规则可套用到任何网关）。
    """
    # 惰性 import：`core` 层不在模块级依赖 `api`（避免 core ↔ api 的导入环）。
    from backend.app.api.llm_gateway_monitor import ALERT_METRICS, ALERT_OPERATORS

    if not isinstance(raw, dict):
        logger.warning("initial_data: 预警规则条目不是对象（%r），已跳过", type(raw).__name__)
        return None

    metric = str(raw.get("metric") or "").strip()
    if metric not in ALERT_METRICS:
        logger.warning(
            "initial_data: 预警规则 metric=%r 不在白名单 %s，已跳过该条",
            metric,
            list(ALERT_METRICS),
        )
        return None

    operator = str(raw.get("operator") or "gt").strip()
    if operator not in ALERT_OPERATORS:
        logger.warning(
            "initial_data: 预警规则 operator=%r 不在白名单 %s，已跳过该条",
            operator,
            list(ALERT_OPERATORS),
        )
        return None

    threshold = raw.get("threshold")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        logger.warning("initial_data: 预警规则 threshold=%r 不是数字，已跳过该条", threshold)
        return None

    window_seconds = raw.get("window_seconds", 86400)
    if not isinstance(window_seconds, int) or isinstance(window_seconds, bool) or window_seconds <= 0:
        logger.warning(
            "initial_data: 预警规则 window_seconds=%r 非法，改用 86400", window_seconds
        )
        window_seconds = 86400

    level = str(raw.get("level") or "warning").strip()
    if level not in ("info", "warning", "error"):
        level = "warning"

    return {
        "platform": str(raw.get("platform") or "").strip(),
        "function": str(raw.get("function") or "").strip(),
        #: v2.3：具体模型作用域（`""` = 该服务商全模型）。种子默认不指定模型。
        "model_name": str(raw.get("model_name") or "").strip()[:128],
        "metric": metric,
        "operator": operator,
        "threshold": float(threshold),
        "window_seconds": window_seconds,
        "level": level,
        "enabled": bool(raw.get("enabled", True)),
    }


def load_initial_gateway_alerts() -> list[dict[str, Any]]:
    """LLM 网关预警规则初始化数据（规范化后的列表；任何问题 ⇒ warning + 可用部分）。

    与 `load_initial_scheduled_jobs` 同构（双路解析、坏数据不致命）。
    ⚠️ 与定时任务**播种时机不同**：预警规则**依附于网关**（`gateway_id` 非空），
    所以不在 lifespan 里盲目播种，而是在**创建/首次出现网关配置时**按用户补种
    （`ensure_default_alerts_for_gateway`）—— 没有网关时播下去就是一批悬空行。
    """
    payload = _load_payload(GATEWAY_ALERTS_FILENAME)
    if payload is None:
        return []
    if not isinstance(payload, dict):
        logger.warning("initial_data: %s 顶层不是对象，已忽略", GATEWAY_ALERTS_FILENAME)
        return []
    rows = payload.get("gateway_alerts")
    if not isinstance(rows, list):
        logger.warning(
            "initial_data: %s 缺少 gateway_alerts 数组，已忽略", GATEWAY_ALERTS_FILENAME
        )
        return []

    normalized: list[dict[str, Any]] = []
    for raw in rows:
        item = _normalize_gateway_alert(raw)
        if item is not None:
            normalized.append(item)
    logger.info("initial_data: 载入 %s 条预警规则初始化数据", len(normalized))
    return normalized


def load_initial_prompt_templates() -> dict[str, list[dict[str, Any]]]:
    """内置提示词模板 / 模块初始化数据（v2.24，设计 17 §13.5.4）。

    返回 `{"modules": [...], "templates": [...]}`；任何问题 ⇒ warning + 可用部分
    （与 `load_initial_scheduled_jobs` 同构：坏种子不致命，但不能静默）。

    ## 为什么要往这里放「一致性约束」

    用户口径「之前你把一致性抽取成公共模板了，但是未使用，在生成图片时应选择公共
    提示词模板，保证图片商品的一致性」——

    v2.21 把防漂移三条抽进了 `services/image_prompt.py`，但那是**代码常量**：
    用户在模板页看不到、改不了，且弹窗里**选不到任何模板**（`prompt_templates`
    表是空的）⇒ 「公共模板」这件事在出图链路上等于没发生。

    本种子把同一组约束**放进模板模块**（`category="style"`），于是：
    ① 出图时下拉里**真的有模板可选**，默认就选中它；
    ② 用户能按自己的品类调措辞，改的是库里的模板行，不改代码。

    ⚠️ 与 `image_prompt.py` 的关系**不是二选一**：那份常量仍由
    `requirement_block()` 作为**兜底约束**追加（没选模板时也要有硬要求），
    两者互补 —— 模板管「风格与一致性」，常量管「成图底线」。
    """
    payload = _load_payload(PROMPT_TEMPLATES_FILENAME)
    if payload is None:
        return {"modules": [], "templates": []}
    if not isinstance(payload, dict):
        logger.warning("initial_data: %s 顶层不是对象，已忽略", PROMPT_TEMPLATES_FILENAME)
        return {"modules": [], "templates": []}

    # 惰性 import：`core` 层不在模块级依赖 `models`（避免 core ↔ models 的导入环，
    # 与 `_normalize_gateway_alert` 取 api 常量同一纪律）。
    from backend.app.models.prompt import PROMPT_TASK_TYPES

    modules: list[dict[str, Any]] = []
    for raw in payload.get("modules") or []:
        if not isinstance(raw, dict):
            logger.warning("initial_data: 提示词模块条目不是对象，已跳过")
            continue
        name = str(raw.get("name") or "").strip()
        content = str(raw.get("content") or "").strip()
        if not name or not content:
            logger.warning("initial_data: 提示词模块缺 name/content，已跳过：%r", name)
            continue
        modules.append(
            {
                "name": name,
                "category": str(raw.get("category") or "style").strip() or "style",
                "content": content,
                "sort_order": int(raw.get("sort_order") or 0),
            }
        )

    templates: list[dict[str, Any]] = []
    for raw in payload.get("templates") or []:
        if not isinstance(raw, dict):
            logger.warning("initial_data: 提示词模板条目不是对象，已跳过")
            continue
        name = str(raw.get("name") or "").strip()
        task_type = str(raw.get("task_type") or "").strip()
        skeleton = str(raw.get("prompt_skeleton") or "").strip()
        if not name or not skeleton:
            logger.warning("initial_data: 提示词模板缺 name/prompt_skeleton，已跳过：%r", name)
            continue
        # task_type 白名单**从模型常量取**，不在这里重抄一份（漂移即缺陷）
        if task_type not in PROMPT_TASK_TYPES:
            logger.warning(
                "initial_data: 提示词模板 task_type=%r 不在白名单 %s，已跳过该条",
                task_type,
                list(PROMPT_TASK_TYPES),
            )
            continue
        names = raw.get("module_names") or []
        templates.append(
            {
                "name": name,
                "task_type": task_type,
                "prompt_skeleton": skeleton,
                "is_default": bool(raw.get("is_default")),
                "module_names": [str(n).strip() for n in names if str(n).strip()],
            }
        )

    logger.info(
        "initial_data: 载入 %s 个提示词模块 / %s 张提示词模板初始化数据",
        len(modules),
        len(templates),
    )
    return {"modules": modules, "templates": templates}


def _normalize_gateway_provider(raw: Any) -> dict[str, Any] | None:
    """单条服务商种子 → 规范 dict；字段非法 ⇒ `None`（丢弃该条 + warning）。

    ⚠️ **不含访问令牌**（KEY 由用户登录后自填）—— 种子里出现 token 字段也**不读**，
    这是刻意的：种子随包分发，塞 KEY 等于泄露（设计纪律同 §10 安全设计）。
    `model_type` 白名单从 `services.business_provider_keys.MODEL_TYPES` 取 ——
    **不在这里重抄一份**（枚举漂移即缺陷，与 `_normalize_gateway_alert` 同纪律）。
    未知类型的模型**丢弃该模型行**（不猜类型，同 §13.2.9.4）、服务商本身仍保留。
    """
    # 惰性 import：`core` 层不在模块级依赖 `services`（避免 core ↔ services 导入环）。
    from backend.app.services.business_provider_keys import MODEL_TYPES

    if not isinstance(raw, dict):
        logger.warning("initial_data: 服务商种子条目不是对象（%r），已跳过", type(raw).__name__)
        return None

    name = str(raw.get("name") or "").strip()
    base_url = str(raw.get("base_url") or "").strip()
    if not name or not base_url:
        logger.warning("initial_data: 服务商种子缺 name/base_url，已跳过：%r", name)
        return None

    models: list[dict[str, str]] = []
    for item in raw.get("models") or []:
        if not isinstance(item, dict):
            logger.warning("initial_data: 服务商 %s 的模型条目不是对象，已跳过", name)
            continue
        model_name = str(item.get("model_name") or "").strip()
        model_type = str(item.get("model_type") or "").strip()
        if not model_name:
            logger.warning("initial_data: 服务商 %s 有模型条目缺 model_name，已跳过", name)
            continue
        if model_type not in MODEL_TYPES:
            logger.warning(
                "initial_data: 服务商 %s 的模型 %s model_type=%r 不在白名单 %s，已跳过该模型",
                name,
                model_name,
                model_type,
                sorted(MODEL_TYPES),
            )
            continue
        models.append({"model_name": model_name, "model_type": model_type})

    return {
        "name": name[:128],
        "base_url": base_url[:512],
        "remark": str(raw.get("remark") or "").strip(),
        "models": models,
    }


def load_initial_gateway_providers() -> list[dict[str, Any]]:
    """LLM 网关服务商种子初始化数据（规范化后的列表；任何问题 ⇒ warning + 可用部分）。

    与 `load_initial_gateway_alerts` 同构（双路解析、坏数据不致命）。
    ⚠️ **播种时机**：服务商**不依附任何行**（不像预警规则依附 `gateway_id`），
    但也**不在 lifespan 全局播** —— 它属于「某用户的凭据容器」，在**用户注册时**
    播种（`api/auth.py::register` 与既有三个种子并列），存量用户不主动补种
    （避免替用户建一堆没配 KEY 的服务商，同 §4.5.1 纪律）。
    """
    payload = _load_payload(GATEWAY_PROVIDERS_FILENAME)
    if payload is None:
        return []
    if not isinstance(payload, dict):
        logger.warning("initial_data: %s 顶层不是对象，已忽略", GATEWAY_PROVIDERS_FILENAME)
        return []
    rows = payload.get("providers")
    if not isinstance(rows, list):
        logger.warning(
            "initial_data: %s 缺少 providers 数组，已忽略", GATEWAY_PROVIDERS_FILENAME
        )
        return []

    normalized: list[dict[str, Any]] = []
    for raw in rows:
        item = _normalize_gateway_provider(raw)
        if item is not None:
            normalized.append(item)
    logger.info("initial_data: 载入 %s 个服务商种子初始化数据", len(normalized))
    return normalized
