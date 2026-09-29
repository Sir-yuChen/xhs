"""dedupe seeded scheduled_jobs rows (design 15 §4.5, DEF-077, 2026-09-27 用户口径)

Revision ID: i0j1k2l3m4n5
Revises: h9i0j1k2l3m4
Create Date: 2026-09-27

2026-09-27 用户口径（原文）：

> 队列记录清理 `system.queue_record_cleanup` 任务，在任务列表出现了两次，
> 应只有只需要一个

**现象**：任务列表里「队列记录清理」每个用户各出现两行 —— 实测 6 个用户
（`scheduled_jobs.id` 50–55 与 56–61），两批 `active` 行**都被 apscheduler 注册**
（`job_<行主键>`）⇒ 该任务实际**双跑**（`fail_count` 两批都在涨）。

**根因**：改名迁移 `c2d3e4f5a6b7`（`system.note_task_record_cleanup` →
`system.queue_record_cleanup`）**与「按新键播种」时序错开**：
① 库里先有「旧键行」（那时种子文件还是旧键 + 旧文案 remark）；
② 种子文件改成新键（09-26 00:31:14）后启动，播种按**新键**判重 ⇒ 查不到仍顶着旧键
   的那批行 ⇒ 又插一批（09-26 00:52:08，文案已是新的）；
③ 改名迁移落地后两批撞成同一个 `job_key` ⇒ 一任务两行。
`c2d3e4f5a6b7` 的 docstring 断言「旧键行由本迁移改名，**不会一任务两行**」——
**该断言被现实推翻**，已在该文件里就地标注（结论被推翻要留痕，不静默改掉）。

**为什么不能加 `(user_id, job_key)` 唯一索引**（最直觉的硬防线）：R18
（2026-09-24 用户口径「调用目标应支持所有的任务目标」）明确允许「**用户新建一条
同目标、不同调度**的任务行」，唯一索引会把这条**合法**用法一并禁掉。判重键
`(user_id, job_key)` 只约束**播种**（`seed_builtin_jobs_for_user`），不是表的完整性约束。

**本迁移做什么**：只收敛「**播种影子行**」，判据落在播种行的**文案特征**上 ——
`remark` 以 `系统内置任务（应用初始化数据` 开头（种子文件唯一文案前缀；用户新建行的
备注由用户自己在表单里填）。同一 `(user_id, job_key)` 组内保留 **`id` 最小**的一行
（= 最早的那条；实测重复组内除 `remark` 文案与时间戳外**其余字段完全同构**，
`status` / `cron` / `params` 全一致 ⇒ 保留哪行都不改变调度语义）。

⚠️ **不覆盖已有行**（§4.5 升级语义）：本迁移**只删不建、不改任何保留行的字段** ——
用户在任务中心调过的调度属性一个字都不动。

**幂等**：收敛后每组只剩一行，条件不再命中 ⇒ 重复执行零删除。
**降级**：不可逆（已删的重复行无从重建，且它本就是脏数据）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "i0j1k2l3m4n5"
down_revision: Union[str, None] = "h9i0j1k2l3m4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 播种行的 remark 前缀（`config/initial_data/scheduled_jobs.json` 里每条的固定开头）。
#: 判据必须落在**内容**上而不是 `is_builtin`：R18 起用户新建行同样写 `is_builtin=True`
#: （「所有任务都是内置的」），那一列已经不能用来区分来源。
_SEEDED_REMARK_PREFIX = "系统内置任务（应用初始化数据"


def upgrade() -> None:
    # 同一 (user_id, job_key) 组内，保留 id 最小的一行，删掉其余**播种行**。
    # ⚠️ 双重限定（remark 前缀 + 分组取 MIN）缺一不可：
    #   - 只按 (user_id, job_key) 去重 ⇒ 会误删用户自建的同目标任务行（R18 合法用法）；
    #   - 不带 MIN 分组 ⇒ 会把整组删空。
    op.execute(
        "DELETE FROM scheduled_jobs "
        f"WHERE remark LIKE '{_SEEDED_REMARK_PREFIX}%' "
        "  AND id NOT IN ("
        "    SELECT MIN(id) FROM scheduled_jobs "
        f"    WHERE remark LIKE '{_SEEDED_REMARK_PREFIX}%' "
        "    GROUP BY user_id, job_key"
        "  )"
    )


def downgrade() -> None:
    # 只删不建：重复行是脏数据，重建它没有任何意义（也不该重建）。
    pass
