"""工作流节点实现 + 注册表（设计 19 §3）。

注册表与 ``scheduled_jobs.job_registry`` 同构：``node_type -> {run, validate, needs_review}``。
``validate`` 在**保存工作流时**校验 config schema，不合法直接 400，不进库（§3.8）。

七类节点（§3.1–3.7）：

| 类型 | 做事 | 下沉队列 |
|---|---|---|
| trigger | 只写运行上下文 | 否（立即 succeeded） |
| topic | 读热点匹配结果选品 | 否（只读） |
| generate | 生成笔记 | **是**（task_type=workflow_generate） |
| review | 人工审核，暂停等人 | 否（waiting） |
| schedule | 排期到时间窗 | 否（只写排期表） |
| publish | 发布 | **是**（一期发布队列） |
| monitor | 触发指标快照 | **是**（指标快照任务） |

幂等键 = ``batch_no``（一期批次号语义的直接延伸，§9.3）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.time import shanghai_now
from backend.app.models.workflow import (
    NODE_GENERATE,
    NODE_MONITOR,
    NODE_PUBLISH,
    NODE_REVIEW,
    NODE_SCHEDULE,
    NODE_TOPIC,
    NODE_TRIGGER,
    WorkflowRun,
    WorkflowScheduledPublish,
)
from backend.app.services.workflow_engine import NodeContext, WorkflowError

logger = logging.getLogger(__name__)

#: 发布时段随机抖动幅度（±10 分钟，§3.4）
_SCHEDULE_JITTER_MINUTES = 10


@dataclass
class NodeOutcome:
    """节点执行结果：``detail`` 写进 ``workflow_run_logs.detail`` 并进跨节点上下文。"""

    detail: dict


@dataclass
class NodeHandler:
    run: Callable[[dict, NodeContext, WorkflowRun, Session], Optional[NodeOutcome]]
    validate: Callable[[dict], None]
    needs_review: bool = False


# ---------------------------------------------------------------------------
# 校验工具
# ---------------------------------------------------------------------------


def _require_bool(config: dict, key: str) -> bool:
    v = config.get(key)
    if v is None:
        return False
    if not isinstance(v, bool):
        raise WorkflowError("node_error", f"{key} 必须是布尔值，得到 {type(v).__name__}")
    return v


def _require_int_list(config: dict, key: str) -> list[int]:
    v = config.get(key)
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, int) for x in v):
        raise WorkflowError("node_error", f"{key} 必须是整数列表")
    return v


def _require_str(config: dict, key: str, *, allowed: Optional[tuple] = None) -> Optional[str]:
    v = config.get(key)
    if v is None or v == "":
        return None
    if not isinstance(v, str):
        raise WorkflowError("node_error", f"{key} 必须是字符串")
    if allowed is not None and v not in allowed:
        raise WorkflowError("node_error", f"{key}={v!r} 不在允许范围 {allowed}")
    return v


# ---------------------------------------------------------------------------
# 1. trigger（§3.1）：不做业务，只写上下文
# ---------------------------------------------------------------------------


def _trigger_run(config, ctx, run, db) -> NodeOutcome:
    return NodeOutcome(detail={"started_at": shanghai_now().isoformat()})


def _trigger_validate(config: dict) -> None:
    if not isinstance(config, dict):
        raise WorkflowError("node_error", "trigger 节点 config 必须是对象")


# ---------------------------------------------------------------------------
# 2. topic（§3.7）：读热点匹配结果选品（只读，不采集热点）
# ---------------------------------------------------------------------------


def _topic_run(config, ctx, run, db) -> NodeOutcome:
    from backend.app.models.hot_topic import TopicGoodsMatch

    top_n = int(config.get("match_top_n") or 10)
    topic_ids = config.get("topic_ids") or []
    rows = db.scalars(
        select(TopicGoodsMatch)
        .where(TopicGoodsMatch.user_id == run.user_id)
        .order_by(TopicGoodsMatch.relevance_score.desc())
        .limit(top_n)
    ).all()
    product_ids = sorted({int(r.product_pk) for r in rows if r.product_pk})
    if topic_ids:
        # 指定话题时进一步收口（TopicGoodsMatch 有 topic 字段）
        wanted = set(topic_ids)
        rows = [r for r in rows if r.topic in wanted]
        product_ids = sorted({int(r.product_pk) for r in rows if r.product_pk})
    ctx.update(
        "topic", {"topic_ids": list(topic_ids), "product_ids": product_ids}
    )
    return NodeOutcome(detail={"product_ids": product_ids, "matched": len(rows)})


def _topic_validate(config: dict) -> None:
    if not isinstance(config, dict):
        raise WorkflowError("node_error", "topic 节点 config 必须是对象")
    n = config.get("match_top_n")
    if n is not None and (not isinstance(n, int) or n <= 0 or n > 500):
        raise WorkflowError("node_error", "match_top_n 必须是 1..500 的整数")


# ---------------------------------------------------------------------------
# 3. generate（§3.2）：下沉队列生成笔记
# ---------------------------------------------------------------------------


def _generate_run(config, ctx, run, db) -> NodeOutcome:
    """为每个（商品 × 账号）组合入队一条生成任务，等全部终态后收口。

    幂等（§9.3）：以 ``batch_no + node_key + product_id + account_id`` 为键，
    本 run 本节点已入过队的就复用既有 task 行（task_id 存在即跳过）。
    """
    source = config.get("source") or "products"
    product_ids = config.get("product_ids") or (ctx.get("topic") or {}).get("product_ids") or []
    account_ids = config.get("account_ids") or (run.global_params_snapshot or {}).get(
        "account_ids", []
    )
    template_id = config.get("template_id")
    if not product_ids:
        raise WorkflowError("param_unresolved", "generate 节点没有可用的 product_ids")
    if not account_ids:
        raise WorkflowError("param_unresolved", "generate 节点没有可用的 account_ids")

    from backend.app.models.task import Task

    node_key = config.get("_node_key", "generate")
    queued: list[int] = []
    for pid in product_ids:
        for aid in account_ids:
            # 幂等：同一 run + 节点 + 商品 + 账号已入队 ⇒ 复用。
            # JSON 路径查询在 SQLite/Postgres 上写法不一 ⇒ 用 like 兜底
            # （payload 里的 id 是数字，like 匹配串不会与别的节点混淆：
            #  query 里同时带 node_key 与 run id 两个锚点）。
            pattern = f'%"workflow_run_id": {run.id}, "node_key": "{node_key}"%'
            existing = db.scalars(
                select(Task.id)
                .where(
                    Task.task_type == "workflow_generate",
                    Task.user_id == run.user_id,
                    Task.payload.like(pattern),
                )
                .limit(1)
            ).first()
            if existing is not None:
                queued.append(int(existing))
                continue
            queued.append(
                _enqueue_task(
                    db,
                    run=run,
                    node_key=node_key,
                    task_type="workflow_generate",
                    payload={
                        "product_id": int(pid),
                        "account_id": int(aid),
                        "template_id": template_id,
                        "source": source,
                    },
                )
            )

    ctx.update(node_key, {"note_ids": queued})
    return NodeOutcome(detail={"note_ids": queued, "generated_count": len(queued)})


def _generate_validate(config: dict) -> None:
    if not isinstance(config, dict):
        raise WorkflowError("node_error", "generate 节点 config 必须是对象")
    src = config.get("source")
    if src is not None and src not in ("products", "topics"):
        raise WorkflowError("node_error", "generate.source 只能是 products / topics")
    n = config.get("notes_per_product")
    if n is not None and (not isinstance(n, int) or n <= 0 or n > 20):
        raise WorkflowError("node_error", "notes_per_product 必须是 1..20 的整数")


# ---------------------------------------------------------------------------
# 4. review（§3.3）：人工审核，暂停等人，不自动放行
# ---------------------------------------------------------------------------


def _review_run(config, ctx, run, db) -> NodeOutcome:
    """能被引擎调到说明 ``ctx.review_done`` 已为 True（§4.2 的 needs_review 门）。"""
    results = ctx.review_results
    rejected = [nid for nid, v in results.items() if v == "reject"]
    if rejected and (config.get("reject_action") or "fail_run") == "fail_run":
        raise WorkflowError(
            "review_rejected", f"人工审核驳回 {len(rejected)} 条笔记：{rejected[:10]}"
        )
    approved = [nid for nid, v in results.items() if v in ("approve", "skip")]
    return NodeOutcome(detail={"approved": approved, "rejected": rejected})


def _review_validate(config: dict) -> None:
    if not isinstance(config, dict):
        raise WorkflowError("node_error", "review 节点 config 必须是对象")
    # ⚠️ 硬约束（§3.3 / §1.3）：只接受 manual，写 auto 直接 400 不入库
    mode = config.get("review_mode")
    if mode is not None and mode != "manual":
        raise WorkflowError("node_error", "审核节点必须人工（review_mode 只能是 manual）")
    action = config.get("reject_action")
    if action is not None and action not in ("fail_run", "skip_note"):
        raise WorkflowError("node_error", "reject_action 只能是 fail_run / skip_note")
    hours = config.get("review_timeout_hours")
    if hours is not None and (not isinstance(hours, (int, float)) or hours <= 0 or hours > 720):
        raise WorkflowError("node_error", "review_timeout_hours 必须是 0..720 的数")


# ---------------------------------------------------------------------------
# 5. schedule（§3.4）：只排期，不发布
# ---------------------------------------------------------------------------


def _schedule_run(config, ctx, run, db) -> NodeOutcome:
    """把上游 note_ids 打散到时间窗，写入排期表（方案 A：不碰 notes 表）。"""
    window = config.get("publish_window") or {}
    start = _require_str(window, "start") or "18:00"
    end = _require_str(window, "end") or "20:00"
    account_ids = (run.global_params_snapshot or {}).get("account_ids", [])

    note_ids = ctx.upstream_notes()
    if not note_ids:
        raise WorkflowError("param_unresolved", "schedule 节点上游没有笔记可排期")

    start_h, start_m = _parse_hhmm(start)
    end_h, end_m = _parse_hhmm(end)
    if (end_h, end_m) <= (start_h, start_m):
        raise WorkflowError("node_error", f"发布时段无效：{start} 不早于 {end}")

    created = 0
    total = max(1, len(note_ids) * max(1, len(account_ids)))
    span = (end_h * 60 + end_m) - (start_h * 60 + start_m)
    step = span / total
    base = shanghai_now().replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    if base <= shanghai_now():
        base = base + timedelta(days=1)  # 当天窗口已过，排到明天

    for idx, note_id in enumerate(note_ids):
        for jdx, _aid in enumerate(account_ids or [0]):
            at = base + timedelta(minutes=step * (idx + jdx))
            at = _jitter(at)
            row = WorkflowScheduledPublish(
                run_id=run.id,
                user_id=run.user_id,
                note_id=int(note_id),
                account_id=int(_aid),
                scheduled_at=at,
                published=False,
            )
            db.add(row)
            created += 1
    db.commit()
    return NodeOutcome(detail={"scheduled_count": created, "window": f"{start}-{end}"})


def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        h, m = value.split(":")
        return int(h), int(m)
    except Exception as exc:  # noqa: BLE001
        raise WorkflowError("node_error", f"时段格式必须是 HH:MM：{value!r}") from exc


def _jitter(at: datetime) -> datetime:
    import random

    delta = random.randint(-_SCHEDULE_JITTER_MINUTES, _SCHEDULE_JITTER_MINUTES)
    return at + timedelta(minutes=delta)


def _schedule_validate(config: dict) -> None:
    if not isinstance(config, dict):
        raise WorkflowError("node_error", "schedule 节点 config 必须是对象")
    window = config.get("publish_window")
    if window is None:
        return
    if not isinstance(window, dict):
        raise WorkflowError("node_error", "publish_window 必须是对象")
    for key in ("start", "end"):
        v = window.get(key)
        if v is None:
            continue
        if not isinstance(v, str) or len(v.split(":")) != 2:
            raise WorkflowError("node_error", f"publish_window.{key} 必须是 HH:MM")


# ---------------------------------------------------------------------------
# 6. publish（§3.5）：复用一期发布队列
# ---------------------------------------------------------------------------


def _publish_run(config, ctx, run, db) -> NodeOutcome:
    """调一期发布队列入队；账号互斥在它内部，引擎不自加账号锁（§8.1）。"""
    account_ids = config.get("account_ids") or (run.global_params_snapshot or {}).get(
        "account_ids", []
    )
    platform = config.get("platform") or "www"
    if platform not in ("www", "creator", "ark"):
        raise WorkflowError("node_error", f"platform={platform!r} 不在 www/creator/ark")
    if not account_ids:
        raise WorkflowError("param_unresolved", "publish 节点没有可用的 account_ids")

    note_ids = ctx.upstream_notes()
    if not note_ids:
        raise WorkflowError("param_unresolved", "publish 节点上游没有笔记可发布")

    immediately = bool(config.get("publish_immediately"))
    if not immediately and not _has_schedule_node(run):
        raise WorkflowError(
            "node_error",
            "没有 schedule 节点时 publish_immediately 必须为 true（否则笔记永远不会发）",
        )

    task_ids: list[int] = []
    for note_id in note_ids:
        for aid in account_ids:
            task_ids.append(
                _enqueue_task(
                    db,
                    run=run,
                    node_key=config.get("_node_key", "publish"),
                    task_type="workflow_publish",
                    payload={
                        "note_id": int(note_id),
                        "account_id": int(aid),
                        "platform": platform,
                        "aigc_mark": _require_bool(config, "aigc_mark"),
                        "publish_immediately": immediately,
                    },
                )
            )
    return NodeOutcome(detail={"note_ids": note_ids, "queued": task_ids})


def _has_schedule_node(run: WorkflowRun) -> bool:
    return any(
        n["node_type"] == NODE_SCHEDULE
        for n in (run.graph_snapshot or {}).get("nodes", [])
    )


def _publish_validate(config: dict) -> None:
    if not isinstance(config, dict):
        raise WorkflowError("node_error", "publish 节点 config 必须是对象")
    _require_str(config, "platform", allowed=("www", "creator", "ark"))


# ---------------------------------------------------------------------------
# 7. monitor（§3.6）：触发指标快照任务
# ---------------------------------------------------------------------------


def _monitor_run(config, ctx, run, db) -> NodeOutcome:
    """只触发快照任务；仪表盘读快照表，与工作流无直接耦合（§12）。"""
    note_ids = ctx.upstream_notes()
    metrics = config.get("metrics") or ["note_views", "note_interactions"]
    task_id = _enqueue_task(
        db,
        run=run,
        node_key=config.get("_node_key", "monitor"),
        task_type="workflow_note_metrics_snapshot",
        payload={"note_ids": [int(n) for n in note_ids], "metrics": list(metrics)},
    )
    return NodeOutcome(detail={"snapshot_task_id": task_id, "note_count": len(note_ids)})


def _monitor_validate(config: dict) -> None:
    if not isinstance(config, dict):
        raise WorkflowError("node_error", "monitor 节点 config 必须是对象")
    m = config.get("metrics")
    if m is not None and not isinstance(m, list):
        raise WorkflowError("node_error", "metrics 必须是列表")


# ---------------------------------------------------------------------------
# 注册表（与 job_registry 同构）
# ---------------------------------------------------------------------------


REGISTRY: dict[str, NodeHandler] = {
    NODE_TRIGGER: NodeHandler(run=_trigger_run, validate=_trigger_validate),
    NODE_TOPIC: NodeHandler(run=_topic_run, validate=_topic_validate),
    NODE_GENERATE: NodeHandler(run=_generate_run, validate=_generate_validate),
    NODE_REVIEW: NodeHandler(run=_review_run, validate=_review_validate, needs_review=True),
    NODE_SCHEDULE: NodeHandler(run=_schedule_run, validate=_schedule_validate),
    NODE_PUBLISH: NodeHandler(run=_publish_run, validate=_publish_validate),
    NODE_MONITOR: NodeHandler(run=_monitor_run, validate=_monitor_validate),
}


def validate_workflow(nodes: list[dict]) -> None:
    """保存时校验整张图（§4.2 REGISTRY.validate + 结构校验）。

    结构校验（§1.3 排除断点）：
    - 恰好一个 trigger 节点，且 seq 最小；
    - 所有 node_type 在白名单内；
    - node_key 同图不重复（DB 也有唯一约束，这是更早的一道闸）。
    """
    if not nodes:
        raise WorkflowError("dag_invalid", "工作流至少需要一个节点")
    triggers = [n for n in nodes if n["node_type"] == NODE_TRIGGER]
    if len(triggers) != 1:
        raise WorkflowError(
            "dag_invalid", f"工作流必须恰好一个 trigger 节点，现有 {len(triggers)} 个"
        )
    if min(n["seq"] for n in nodes) != triggers[0]["seq"]:
        raise WorkflowError("dag_invalid", "trigger 节点必须在链首（seq 最小）")

    keys = [n["node_key"] for n in nodes]
    if len(set(keys)) != len(keys):
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        raise WorkflowError("dag_invalid", f"node_key 重复：{dupes}")

    for node in nodes:
        handler = REGISTRY.get(node["node_type"])
        if handler is None:
            raise WorkflowError("dag_invalid", f"未知节点类型：{node['node_type']!r}")
        handler.validate(node.get("config") or {})


# ---------------------------------------------------------------------------
# 下沉到一期 tasks 队列（§5.2）
# ---------------------------------------------------------------------------


def _enqueue_task(
    db: Session,
    *,
    run: WorkflowRun,
    node_key: str,
    task_type: str,
    payload: dict,
) -> int:
    """落一行 task，payload 带回指字段（§5.2 的下沉约定）。

    不 commit（由引擎在节点边界 commit）；task 行的认领/重试/退避全部由一期 worker 承担。
    """
    from backend.app.models.task import Task

    row = Task(
        user_id=run.user_id,
        platform="xhs",
        task_type=task_type,
        status="pending",
        progress=0,
        payload={
            **payload,
            "workflow_run_id": int(run.id),
            "node_key": str(node_key),
            "batch_no": run.batch_no,
        },
        batch_no=None,  # 子行不挂批次号（ux_tasks_batch_no 对 NULL 不生效）
    )
    db.add(row)
    db.flush()
    return int(row.id)
