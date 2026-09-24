"""发布幂等（T-103 / 缺陷 DEF-002）。

## 风险背景

发布是**不可逆写操作**。原设计有四道防线，但只有最后一道是可靠的：

| # | 防线 | 强度 |
|---|---|---|
| 1 | 状态机（`publishing` 时不再触发） | 中，无原子性保证 |
| 2 | 调度器 `max_instances=1` | **仅单进程内**，多副本失效 |
| 3 | 补跑幂等（只取 `scheduled` 态） | 中，状态回写前崩溃会重发 |
| 4 | **数据库唯一约束** | ❌ 此前缺失 → **本模块补上** |

## 键的设计

    idempotency_key = "{platform}:{platform_account_id}:{sha256(fingerprint)[:32]}"

`fingerprint` = 标题 + 正文 + **规范化后的** `publish_options`（JSON 键排序）。

**刻意不纳入**时间戳、素材路径、`scheduled_at`——它们会随重试或环境变化，
混进指纹会让幂等直接失效（换个时间就成了"新内容"）。

## 为什么生成时机是「发布前」而非「建任务时」

建任务时就写键，会让「建了 → 取消 → 再建同样内容」被唯一索引**直接拒绝**，
而那是完全合法的操作。放到发布认领时（真正要写平台的那一刻），
才能精确表达唯一的真实约束：

> **同一账号的同一内容，只允许被提交一次。**

## 核心不变式

    **键非空 ⇔ 该任务的内容已占用该账号的槽位**

由两条规则共同维持：

* :func:`claim_publish_idempotency` —— 提交前写入键；
* :func:`release_publish_idempotency` —— **仅当可证明「从未提交给平台」时**清空键。

⚠️ 释放条件**不能**简单按状态判断：``scheduled`` 代表平台已受理、只是按未来时间发，
此时本地取消（``POST /publish/jobs/{id}/cancel`` 允许从 ``scheduled`` 取消）
**并不能撤回平台侧的那篇笔记**，因此必须继续持有键。
目前唯一合法的释放点是「从 ``pending`` 取消」——那时确实什么都没提交。

同理，``failed`` 状态**不释放**：``post_note`` 抛异常不保证平台没收到请求
（可能是发出后才断连）。保守持有键，用户重试同一任务时走自身排除，不受影响。

## 应用层与数据库层的口径必须一致

:func:`find_duplicate_publish` **不按状态过滤**。理由是唯一索引也不看状态：
若一个过滤、一个不过滤，就会出现「应用层放行 → 数据库抛 IntegrityError」，
用户只看到一句「服务器错误」。两者判定口径严格统一为：**列里有值即为占用**。

## 已知副作用（必须知悉）

同一账号 + 完全相同的标题/正文/发布参数，**只能发布一次**。
若确有重复发布需求（例如删稿后重发），必须改动内容或更换账号。

这是有意为之：验收指标中「重复发布次数 = 0」是**硬指标**。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import PublishJob


class DuplicatePublishError(Exception):
    """同一账号的同一内容已被占用（已发布 / 正在发布 / 已提交待发）。"""

    def __init__(self, message: str, *, conflicting_job_id: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.conflicting_job_id = conflicting_job_id


def normalize_publish_options(publish_options: Any) -> str:
    """把 ``publish_options`` 归一为**稳定**字符串，用于参与指纹计算。

    ``{"b": 1, "a": 2}`` 与 ``{"a": 2, "b": 1}`` 必须得到同一个结果，
    否则键会随字典序抖动、幂等失效。
    """
    if publish_options is None:
        return "{}"
    if isinstance(publish_options, str):
        raw = publish_options.strip()
        if not raw:
            return "{}"
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            # 不是合法 JSON：原样使用（保持确定性即可）
            return raw
    else:
        parsed = publish_options

    try:
        return json.dumps(parsed, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(parsed)


def build_publish_idempotency_key(
    *,
    platform: str,
    platform_account_id: Optional[int],
    title: str,
    body: str,
    publish_options: Any = None,
) -> str:
    """构造发布幂等键。

    :param platform_account_id: 发布账号。为 ``None`` 时键仍可构造，
        但调用方应先通过发布闸门（无账号本就不允许发布）。
    """
    options = normalize_publish_options(publish_options)
    # 用 \x1f（单元分隔符）拼接：普通字符里不会出现，避免字段边界歧义
    fingerprint = "\x1f".join(
        [
            (platform or "").strip(),
            str(platform_account_id if platform_account_id is not None else ""),
            title or "",
            body or "",
            options,
        ]
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:32]
    return f"{(platform or '').strip()}:{platform_account_id}:{digest}"


def build_job_idempotency_key(job: PublishJob) -> str:
    """按 job 当前内容计算幂等键。"""
    return build_publish_idempotency_key(
        platform=job.platform,
        platform_account_id=job.platform_account_id,
        title=job.title or "",
        body=job.body or "",
        publish_options=job.publish_options,
    )


def find_duplicate_publish(
    db: Session,
    *,
    idempotency_key: str,
    exclude_job_id: Optional[int] = None,
) -> Optional[PublishJob]:
    """查找是否已有别的任务持有同一个幂等键。

    **刻意不按状态过滤**——唯一索引也不看状态。两者判定口径必须一致，
    否则会出现「应用层放行、数据库报 IntegrityError」的组合。
    """
    stmt = select(PublishJob).where(PublishJob.idempotency_key == idempotency_key)
    if exclude_job_id is not None:
        stmt = stmt.where(PublishJob.id != exclude_job_id)
    return db.scalars(stmt).first()


def claim_publish_idempotency(db: Session, *, job: PublishJob) -> str:
    """在**真正提交给平台前**认领幂等键；返回该键。

    做两件事：

    1. **可读的重复检查**——命中同键的其他任务时抛出
       :class:`DuplicatePublishError`，错误信息可直接展示给用户；
    2. 把键写入 ``job``，交由 **唯一索引**兜底。

    为什么两步都要：唯一索引能挡住并发竞争，但它抛出的是 ``IntegrityError``，
    用户看到的只会是一句「服务器错误」。第 1 步负责在无竞争（绝大多数情况）
    时给出**可操作**的提示，第 2 步负责正确性。

    本函数只**原地修改** ``job``，不 commit —— 与调用方的状态迁移放在同一事务，
    避免出现「键已占用但状态还没变」的中间态。

    :raises DuplicatePublishError: 同一账号的同一内容已被其他任务占用。
    """
    key = build_job_idempotency_key(job)
    conflict = find_duplicate_publish(db, idempotency_key=key, exclude_job_id=job.id)
    if conflict is not None:
        raise DuplicatePublishError(
            f"同一账号已存在相同内容的发布任务（任务 #{conflict.id}，当前状态 {conflict.status}），"
            "已阻止重复发布。如需重发请修改标题/正文，或更换发布账号。",
            conflicting_job_id=conflict.id,
        )
    job.idempotency_key = key
    return key


def release_publish_idempotency(job: PublishJob) -> None:
    """释放幂等键 —— **仅在可证明「从未提交给平台」时调用**。

    目前唯一合法的调用点是「从 ``pending`` 取消任务」：那时确实什么都没提交，
    释放后「重新建一个同内容任务」可以正常认领槽位（否则会被唯一索引无故挡住）。

    ⚠️ 以下情形**必须继续持有键**，不要调用本函数：

    * ``failed`` —— ``post_note`` 抛异常不保证平台没收到请求（可能是发出后断连），
      保守持有；用户重试**同一个**任务走自身排除，不受影响；
    * ``scheduled`` —— 平台已受理、按未来时间发。此时本地「取消」只改本地状态，
      **撤不回平台侧那篇笔记**，因此槽位仍被占用。

    注意是**原地修改** ``job``（不 commit），与调用方的同一次事务一起提交。
    """
    job.idempotency_key = None
