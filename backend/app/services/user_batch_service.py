"""用户批量操作的**执行内核**（2026-10-25 用户口径「新增用户管理批量相关操作」）。

## 为什么抽成服务层而不是在端点里写循环

批量与单条必须是**同一套判据**：

* 不能删自己（`_assert_not_self`）
* 不能删最后一个超级管理员（`LAST_ADMIN_PROTECTED`）
* 软删要释放唯一键（`deleted_<id>_<原名>`）

如果在批量端点里另写一遍，迟早两边漂移 —— 单条能拦的情况批量放过去，
而「批量删了唯一管理员」是不可恢复的事故。

⇒ 批量只做三件事：**遍历 / 逐条调单条实现 / 汇总回执**。

## 回执四态（与设计 44「平台优先」同一套报账口径）

| 态 | 含义 |
|---|---|
| `ok` | 成功 |
| `skipped` | 按规则跳过（自己 / 唯一管理员 / 目标不存在） |
| `failed` | 执行出错（含权限相关的 400） |
| `not_found` | 目标不存在或已删 |

一个失败**不中断整批** —— 否则用户要反复试「先删哪几个」。
但回执必须逐条说清，不能合并成「已处理 N 个」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.app.models import User


@dataclass
class BatchOutcome:
    """一条目标的处理结果。"""

    user_id: int
    status: str  # ok / skipped / failed / not_found
    reason: str = ""


def run_batch(
    db: Session,
    actor: User,
    user_ids: Iterable[int],
    *,
    action: Callable[[Session, User, int], None],
    action_label: str,
) -> dict:
    """逐条执行并汇总。

    :param action: 单条实现 `(db, actor, user_id) -> None`；抛 `HTTPException`
        表示这条失败（`detail` 作为原因）。
    :param action_label: 用于拼原因文案（「删除」「重置密码」）。

    ⚠️ **每条独立提交**：一条失败不能把前面成功的也回滚掉。所以 action 内部
    自己 commit（与单条端点一致），这里只在异常时 rollback 这一条的脏事务。
    """
    results: list[BatchOutcome] = []
    #: 去重但保序（用户勾选的顺序即处理顺序，便于对照）
    seen: set[int] = set()
    ordered = [uid for uid in user_ids if not (uid in seen or seen.add(uid))]

    for uid in ordered:
        if uid == actor.id:
            results.append(BatchOutcome(uid, "skipped", f"不能{action_label}自己"))
            continue
        target = db.get(User, uid)
        if target is None or getattr(target, "deleted_at", None) is not None:
            results.append(BatchOutcome(uid, "not_found", "用户不存在或已删除"))
            continue
        try:
            action(db, actor, uid)
            results.append(BatchOutcome(uid, "ok"))
        except HTTPException as exc:
            db.rollback()
            detail = exc.detail
            reason = detail.get("message") if isinstance(detail, dict) else str(detail)
            #: 规则性拦截记 skipped（不是错误），其余记 failed
            status = "skipped" if exc.status_code in (400, 403) else "failed"
            results.append(BatchOutcome(uid, status, reason))
        except Exception as exc:  # noqa: BLE001 - 单条失败不中断整批
            db.rollback()
            results.append(BatchOutcome(uid, "failed", str(exc)[:200]))

    summary = {
        "requested": len(ordered),
        "ok": sum(1 for r in results if r.status == "ok"),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "failed": sum(1 for r in results if r.status == "failed"),
        "not_found": sum(1 for r in results if r.status == "not_found"),
        "results": [
            {"user_id": r.user_id, "status": r.status, "reason": r.reason} for r in results
        ],
    }
    return summary
