"""队列记录页（设计 13 §35.11，2026-09-25 用户口径「做一个任务记录抽屉/页面」；
同日定名「**队列记录**」+「显示来源，使用 tip 展示说明」；§31.10 起新增商品生成队列；
2026-09-26 用户多轮口径：**批次+详情父子树 + 唯一批次号**（早间一度「不需要队列批次」
被当日晚间口径取代 ——「将队列执行详情表使用批次+详情父子树风格展示，批次逻辑不再
移除，生成唯一批次号」）、**列表支持按唯一批次号查询**、「来源要具体到按钮」、
「补回状态列」、「新增手动批量清理（不清理待重试和进行中）」；**再一轮**（同日覆盖
表格形态）：「不做兼容逻辑，数据已清空，批次号 / 记录列只展示批次行，批次创建时间，
注意使用父子树列表」+「支持按照批次清理记录……清理功能分为两个维度，批次维度和
批次内的操作，批次中存在正在执行或待执行得记录，则不支持该批次全部清理」）。
**第三轮**（同日）：**「类型」列展示执行队列中文名**（`QUEUE_REGISTRY.label`，
前端不写死队列清单）、**「来源」列的 tip 全部移除**（说明只留详情抽屉）。

两半，各管一件事：

1. **纯逻辑**（`tests/frontend/task_records.cjs`，esbuild 打包**真实**
   `frontend/src/lib/task-records.ts`）：类型中文名不许猜、可取消 / 可重试判据与后端
   `tasks.py` 的条件更新一致、**可清理判据**（仅 done/cancelled）、缺值渲染 `-`、
   失败摘要必须带真实原因、**来源按钮级九值**（空值/遗留 manual 归早期数据，不许
   「未知」）、**商品生成队列的主体口径**（商品名优先 / 缺值 `-` / 终态后
   「生成了哪篇笔记」从 `note_title` 取）、**执行详情多行文案**、**批次父子树**
   （批次行在页面白名单里 / 「是不是批次」走 `batch_no` 数据判据 / 队列项永不当父节点）、
   **队列归属反查**（批次行也要落回队列，否则筛选时父行消失）、**批次摘要取后端
   `summary`**（前端不重算）、**清理的两维度判据**（批次维度原子：批内有正在执行 /
   待执行或待重试的明细就不许整批清；批次内维度单条清）、**清理结果分层报账**
   （清了几条 / 多少被挡 / 多少状态不可清被跳过）。
2. **结构守卫**（读页面 / 路由 / 菜单源码）：这一页是**接线**不是新造 —— 必须复用既有
   `/tasks` 端点与 `lib/task-records.ts`，不许在页面里另写一份类型映射表（DEF-044），
   也不许把提示摆成 inline 消息条（设计 04 §3.6）。§31.10 追加：详情抽屉必须按队列项
   类型分主体（商品生成显示商品名）。2026-09-26 追加：状态列必须回来、进度列不许回来、
   手动清理三入口 + 复选框禁用判据必须接线；**再一轮追加**：「批次号」与
   「批次创建时间」两列只对批次行渲染（明细行留空）、批次维度清理必须是「清理整批」
   且不可清时给 Tooltip 原因（不许按钮消失让人猜）。

⚠️ 与「任务中心 · 任务日志」（`/tasks` 路由 = `scheduled_job_logs`）**不是一回事**：
那一页记「**什么时候跑**」（调度视角，1 次调度 1 行），本页记「**跑完了什么**」
（业务视角，1 次批量 = 1 父行 + N 子行）。定时任务触发时**两边都会落**（调度器一行 +
每个账号一行）⇒ 本页每行带「来源」列，否则用户会以为系统在偷偷跑
（**2026-09-26 第三轮**：来源**列**已按口径去掉 Tooltip，说明只留在详情抽屉）。
守卫里钉住两者都在、路径不撞、菜单名不撞（「队列记录」≠「任务中心」）。
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tests.backend.conftest import ROOT

RECORDS_PAGE = ROOT / "frontend/src/pages/tasks/task-records-page.tsx"
TASK_RECORDS_LIB = ROOT / "frontend/src/lib/task-records.ts"
ROUTER = ROOT / "frontend/src/app/router.tsx"
APP_SHELL = ROOT / "frontend/src/components/layout/app-shell.tsx"
API_TS = ROOT / "frontend/src/lib/api.ts"


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """剥掉 `//` 行注释与 `/* */` 块注释，**不动字符串字面量**（状态机，防 URL 被劈断）。

    为什么必须剥：守卫断言的是**代码里真的有**，而 docstring / 注释里几乎一定会提到
    被守护的符号名（实测：页面 docstring 写着「走 `lib/task-records.ts` 单一真相源」，
    于是「import 被改成别的文件」这种破坏仍然绿）—— 不剪注释就是被自己的注释命中。
    """
    out: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        nxt = source[index + 1] if index + 1 < length else ""
        if char == "/" and nxt == "/":
            while index < length and source[index] != "\n":
                index += 1
            continue
        if char == "/" and nxt == "*":
            index += 2
            while index < length and not (source[index] == "*" and source[index + 1 : index + 2] == "/"):
                index += 1
            index += 2
            continue
        if char in "\"'`":
            quote = char
            out.append(char)
            index += 1
            while index < length:
                out.append(source[index])
                if source[index] == "\\" and index + 1 < length:
                    out.append(source[index + 1])
                    index += 2
                    continue
                if source[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _run_task_records(env: dict | None = None):
    script = ROOT / "tests/frontend/task_records.cjs"
    assert script.exists(), "行为验证脚本缺失（tests/frontend/task_records.cjs）"
    try:
        return subprocess.run(
            ["node", str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError:  # pragma: no cover - 环境问题，不是逻辑分支
        pytest.fail("未找到 node：本机无法运行前端行为验证（AGENTS.md §2 已依赖 node 跑 tsc）")


# --------------------------------------------------------------------------- #
# 1) 纯逻辑：真实打包产物
# --------------------------------------------------------------------------- #
def test_task_record_logic_is_driven_for_real():
    """158 条 = A(10) + B(8) + C(6) + D(17) + E(7) + F(5) + G(10) + H(22) + I(7)
    + J(10) + K(9) + L(10) + M(5) + N(7) + O(7) + P(12) + Q(6)。

    覆盖：类型中文名（白名单内 / **表外原样** / 空串兜底）、分组、状态映射与颜色、
    可取消（仅 pending/running）与可重试（仅 failed/exhausted）判据、**可清理判据**
    （仅 done/cancelled；待重试与进行中不可清）、耗时缺值 `-` 与四档格式、结果摘要
    （成功 / 原因+code / 兜底）、结构判据（队列项 vs 批次父行）、
    **来源按钮级九值**（内容库-同步/批量同步/发布/批量发布、商品列表-生成笔记/
    批量生成笔记、重试、恢复、定时；遗留 manual 与空值归「页面按钮（早期数据）」，
    不许「未知」）与消歧说明、**商品生成队列主体**（§31.10：商品名优先 / 退 item_id /
    缺值 `-`；终态后「生成了哪篇笔记」从 note_title 取）、**执行详情多行文案**、
    **批次父子树**（J：白名单收批次父行 / `batch_no` 数据判据 / 队列项不当父节点）、
    **队列归属反查**（L：批次行落回队列）、**批次摘要**（M：总数取后端 summary、
    只列非零计数、空批次不硬凑）、**批次维度清理的阻塞判据**（N：批内有正在执行 /
    待执行或待重试的明细就不许整批清，原因要带条数 + 出路）、**清理结果分层报账**
    （O：清了几条 / 多少被挡 ⇒ warning 不是 success / 没有可清记录 ⇒ info）、
    **账号列**（P：`accountNameMap` 只放账号名 + 名字缺失退 `账号 #id` + 明细行取
    自己 `payload.account_id` + 批次行取派生 `accounts`（单账号显名 / 跨账号显
    「N 个账号」+ Tooltip 列清）+ 非法 id 过滤 + 不做「从 children 重算」的第二份判据）、
    **批次行筛选**（Q：队列 + 状态两个条件**只作用于顶级（批次）行** —— 命中批次
    整批明细原样，明细行从不被单独筛掉；「父子树不使用同一个表头」的落地）。
    """
    result = _run_task_records()
    assert result.returncode == 0, (result.stdout + result.stderr)[-900:]
    assert "160 PASS / 0 FAIL" in result.stdout


@pytest.mark.parametrize(
    "mutate",
    [
        ("typelabel"),  # 未知类型也造名 ⇒ 把未知说成已知
        ("group"),      # 店铺商品同步掉出 sync 组 ⇒ 筛选时它消失
        ("cancel"),     # done 也可取消 ⇒ 前端多一个无效按钮
        ("retry"),      # cancelled 也可重试 ⇒ 用户点了静默无操作
        ("duration"),   # 缺值渲染 0ms ⇒ 把「不知道」说成「0 毫秒」
        ("outcome"),    # 摘要吞掉真实原因 ⇒ 用户失去自救路径
        ("source"),     # 全部来源显示成同一个值 ⇒ 按钮级消歧失效
        ("genitem"),    # 商品名优先级被抹掉 ⇒ 生成队列详情只显示商品 ID
        ("subject"),    # 主体快照被抹掉 ⇒ 执行详情只剩「笔记 #123」
        ("clean"),      # 一律可清 ⇒ 待重试 / 进行中的记录也能被选中删除
        ("batchtypes"),  # 页面白名单只留队列项 ⇒ 批次父行被过滤，父子树只剩明细
        ("batchrow"),    # 批次判据恒 false ⇒ 批次行按普通行渲染，父子树塌成平铺
        ("queuekey"),    # 归属反查只认队列项 ⇒ 按队列筛选时批次父行消失
        ("queuelabel"),  # 类型列改显示队列 key（英文）⇒ 「类型展示为队列中文名」落空
        ("batchdetail"),  # 总数只算成功+失败 ⇒ 进行中/待处理/取消的明细被漏掉
        ("batchblock"),   # 批次维度永远「可清」⇒ 批内有活/待重试的明细也放行整批清理
        ("cleanreport"),  # 报账吞掉「有批次被挡」⇒ 用户把没清掉当成清干净了
        ("account"),      # 跨账号批次只报第一个账号 ⇒ 页面写 1 个账号、明细里 3 个（说错）
        ("accountname"),  # 账号名映射退化成只报 id ⇒ 「账号」列全是 `账号 #1`，认不出是谁
        ("statusdeep"),   # 明细行也被状态条件切一遍 ⇒ 「共 N 条」被筛成 1 条（父子共用筛选）
    ],
)
def test_task_record_guards_are_load_bearing(mutate):
    """变异验证：每个守卫被改坏时脚本必须变红（否则它没在守任何东西）。"""
    result = _run_task_records({"TASK_RECORDS_MUTATE": mutate})
    assert result.returncode != 0, f"{mutate} 变异必须让脚本失败"
    assert "FAIL  " in result.stdout, f"{mutate} 变异必须留下具体的 FAIL 行"


# --------------------------------------------------------------------------- #
# 2) 结构守卫：接线而非新造
# --------------------------------------------------------------------------- #
def test_task_records_page_is_wired_not_reinvented():
    """页面 / 路由 / 菜单 / API 四处的接线守卫（判据落在 `_code_only` 之后的代码上）。"""
    page = _code_only(_read(RECORDS_PAGE))
    router = _read(ROUTER)
    shell = _read(APP_SHELL)
    api = _read(API_TS)
    lib = _code_only(_read(TASK_RECORDS_LIB))

    # ① 路由与菜单都在（缺一就是「写好了没接线」）
    assert 'path="/task-records"' in router and "<TaskRecordsPage />" in router, (
        "路由缺少 /task-records（或没指向 TaskRecordsPage）"
    )
    # ⚠️ 菜单名是「**队列记录**」不是「任务记录」（2026-09-25 用户口径「菜单名称重新队列记录」）：
    # 与「任务中心 · 任务日志」只差一个字，用户根本分不清进哪页。
    assert 'key: "/task-records"' in shell and 'label: "队列记录"' in shell, (
        "侧边菜单入口必须叫「队列记录」（与「任务中心」消歧）"
    )
    assert 'label: "任务记录"' not in shell, "菜单名不该再叫「任务记录」（已按口径改「队列记录」）"
    # ② 与「任务中心」并存且不混淆（两条路由、两个页面文件）
    assert 'path="/tasks"' in router and "TaskCenterPage" in router, (
        "「任务中心」（定时任务）路由必须仍在 —— 任务记录不是它的替代品"
    )

    # ③ 复用既有 /tasks 四件，不新写请求函数
    assert "fetchTaskDetail(" in page and "cancelTask(" in page and "retryTask(" in page, (
        "页面必须复用既有 /tasks 端点（详情含 children / cancel / retry）"
    )
    assert "fetchTaskDetail" in api and "`/tasks/${taskId}`" in api, (
        "api.ts 应已有任务详情（含 children）入口"
    )
    assert "http" not in page and "fetch(" not in page, "页面不许自己发裸 HTTP 请求（一律走 lib/api）"

    # ③b **只收队列**（2026-09-25 用户纠正：「队列记录中，记录的是队列中每一条记录的
    #     详情以及执行情况数据」）：账号级同步 / 商品同步 / AI / 抓取这些**不是队列**的
    #     记录不许混进来 —— 混进来就又把本页变成另一个任务日志。
    assert "isQueueTaskType(" in page, "必须用 `isQueueTaskType` 过滤，只留队列项 + 批次行"
    assert "fetchQueues(" in page, "队列清单必须由后端下发（GET /tasks/queues），不许前端写死"

    # ③b' **批次父子树 + 批次号查询的接线**（2026-09-26 晚间 §35.13）：
    #     批次行判据必须走 lib 的 `isBatchRow`（**数据判据**：`batch_no` 有值，不是写死
    #     类型清单 —— 新队列上线后端自动发号，前端零改动）；批次摘要必须走 lib（计数取
    #     后端 `summary`，前端不重算 —— DEF-044）；按队列筛选必须走 lib 的归属反查
    #     （**批次行也要落回队列**，否则筛「同步」时父行消失、父子树只剩明细）；
    #     批次号查询必须**受控提交**（回车 / 点搜索，打字即查会把接口打穿）。
    assert "isBatchRow(" in page, "批次行判据必须走 lib（`batch_no` 数据判据，不是写死类型清单）"
    assert "batchExecutionDetailLines(" in page, (
        "批次行执行详情必须走 lib（计数取后端 summary，前端不重算 —— DEF-044）"
    )
    assert "filterBatchRows(" in page, (
        "批次行筛选必须走 lib 的 `filterBatchRows`（唯一实现 —— 状态筛选搬到表头后，"
        "页面不许自己再写一份过滤，DEF-044）"
    )
    assert "queueKeyForTask(row.task_type, queues) === queueKey" in lib, (
        "按队列筛选必须走 lib 的归属反查（`filterBatchRows` 里**真调用**它 —— 批次行要能"
        "落回队列，否则筛队列时父行消失、父子树只剩明细）"
    )
    assert "row.batch_no" in page, "列表第一列必须展示唯一批次号（批次行 = 父节点）"
    # ⚠️ 队列管理卡片**只显示队列类型**：`batch_task_type` 是注册表常量（每个队列恒定，
    #     如 `xhs_note_gen_batch`）⇒ 上屏会被读成「批次号」（2026-09-26 用户口径「直接
    #     使用队列类型不就可以了，唯一 key 肯定是唯一的，不需要多一个批次」）。
    #     字段**仍由后端下发**、由 lib 的 `queueKeyForTask` 用于批次行归队反查 ——
    #     不上屏 ≠ 能删（删了下发，筛「同步」时批次父行会消失、父子树只剩明细）。
    assert "batch_task_type" not in page, (
        "队列管理卡片不许渲染 batch_task_type（恒定值会被误读成批次号；真实批次号是 `batch_no`）"
    )
    # ⚠️ 状态筛选的**入口在「状态」列表头**（2026-09-26 用户口径「将队列执行记录表中的
    #     状态筛选放到批次内，父子树表不使用同一个表头，在表头状态列加筛选条件」）：
    #     ① 表头筛选（**受控** `filteredValue` + **单选**），工具栏**不再有**状态下拉；
    #     ② 只筛**批次（父）行**：真过滤是 lib 的 `filterBatchRows`（只 filter 顶级行，
    #        明细行整批跟随 —— 父子树不共用同一个筛选条件，否则「共 N 条」会对不上）；
    #     ③ 页面列上**不写 `onFilter`**（写了 = 同一判据两个实现，且 antd 会连带筛明细）。
    assert "filters: STATUS_FILTERS" in page, "状态筛选入口必须在「状态」列的**表头**"
    assert "filteredValue: status" in page, "表头筛选必须受控（否则选了不生效）"
    assert "filterMultiple: false" in page, "状态筛选是单选（与原来的工具栏下拉同语义）"
    assert "onChange={setStatus}" not in page, "状态下拉不许留在工具栏（入口只在表头）"
    assert "onFilter" not in page, (
        "表头列不许写 `onFilter`（真过滤只在 lib 的 `filterBatchRows`，防两份实现）"
    )
    assert "onSearch=" in page, "批次号查询必须受控提交（回车 / 点搜索），打字即查会打穿接口"
    assert 'placeholder="按批次号查询' in page, "批次号查询入口要说明「可只输尾段」"
    # ⚠️ 只拦**类型常量**（硬编码队列的真信号）；`QUEUE_REGISTRY` 允许出现在空态文案里
    # （提示「后端没注册队列」，那是给用户看的信息不是判断依据）。
    # §31.10 起六种（同步 / 发布 / 商品生成各自的队列项与批次父行）。
    for constant in (
        "xhs_note_sync_item",
        "xhs_note_publish_item",
        "xhs_note_gen_item",
        "xhs_note_sync_batch",
        "xhs_note_publish_batch",
        "xhs_note_gen_batch",
    ):
        assert constant not in page, (
            f"前端不许写死队列类型常量 {constant}（新队列上线 = 后端加一行注册，前端零改动）"
        )

    # ③c 两个 Tab（用户口径「分为两个 tab 队列管理和队列执行记录」）
    assert "Tabs" in page and 'key: "queues"' in page and 'key: "records"' in page, (
        "必须是两个 Tab：队列管理（queues）+ 队列执行记录（records）"
    )
    assert 'label: "队列管理"' in page and 'label: "队列执行记录"' in page, (
        "两个 Tab 的文案必须是「队列管理」/「队列执行记录」"
    )

    # ③d 队列内**详情记录**按队列项类型分主体（§31.10）：商品生成队列显示商品名
    #     （生成前笔记不存在，行上带 `item_id` / `item_name`），终态后附带「生成了
    #     哪篇笔记」；同步 / 发布仍走笔记标题。判据同样落 lib（单一真相源）。
    assert "isProductGenItem(" in page, "详情抽屉必须按队列项类型分主体（商品生成 ≠ 笔记）"
    assert "genItemProductText(" in page and "genItemNoteText(" in page, (
        "商品文案必须走 lib/task-records（item_name 优先、退 item_id、缺值 -；终态后 note_title）"
    )

    # ③e **账号列的接线**（2026-09-26 用户口径「批次记录执行的账号」）：明细行取
    #     自己 `payload.account_id`（入队快照）、批次行取后端**按子行派生**的
    #     `accounts` —— 判定与文案必须走 lib（DEF-044），账号清单由 `fetchAccounts()`
    #     拉 + `accountNameMap()` 查表。⚠️ 页面**不许自己解 `payload.account_id`**：
    #     那就是把「批次级多账号」的判据搬到页面上重写一份（第二份真相源）。
    assert 'title: "账号"' in page, "列表必须有「账号」列（口径「批次记录执行的账号」）"
    assert "taskAccountText(" in page and "taskAccountTip(" in page, (
        "账号列文案 / Tooltip 必须走 lib（批次级多账号判据不许在页面上重写）"
    )
    assert "fetchAccounts(" in page, "账号名映射必须来自 fetchAccounts()（页面不许写死账号名）"
    assert "accountNameMap(" in page, (
        "id → 账号名必须走 lib 的 accountNameMap（只放账号名，与账号下拉同口径）"
    )
    assert "account_id" not in page, (
        "页面不许自己解 `payload.account_id`（账号判据走 lib —— 自己解就是第二份真相源）"
    )

    # ④ 纯逻辑单一真相源：页面 import lib/task-records，不另写映射表
    assert 'from "../../lib/task-records"' in page, "页面必须从 lib/task-records 取判据与文案"
    for call in (
        "queueLabelForTask(",
        "taskStatusLabel(",
        "canCancelTask(",
        "canRetryTask(",
        "formatDuration(",
        "taskSourceLabel(",
        "taskSourceTip(",
    ):
        assert call in page, f"页面必须真的调用 {call}（绕开它就是在页面里另写一份判据）"
    # ⚠️ 2026-09-26 第三轮：类型展示改用**执行队列中文名**（`queueLabelForTask`
    # 内部回退 `taskTypeLabel`）⇒ 页面不再直接调 `taskTypeLabel`，本条随之移除。
    # 但内部类型代码仍不许在页面上「翻译一遍」：中文名映射表只有 lib 一份。
    assert "TASK_TYPE_LABELS" not in page and "Record<string, string>" not in page, (
        "页面里不许另写一份类型中文名映射表（唯一真相源是 lib/task-records.ts；"
        "复制映射表必然带这两个特征之一）"
    )

    # ④b 来源列 —— **2026-09-26 第三轮用户口径「来源列的 tip 全部移除」**：
    #     列表列只留一个 Tag（不挂 Tooltip、不写 cursor:help）；说明性文字
    #     （两页分工 / 定时触发 / 早期数据）只留在**详情抽屉**那一处。
    #     ⚠️ 判据必须**特指来源列那一段**：只断言 `"<Tooltip" in page` 会被别处
    #     （账号列 / 结果列）的 Tooltip 满足 ⇒ 列上偷偷加回 tip 仍然绿（实测过）。
    assert 'title: "来源"' in page, "列表必须有「来源」列（手动 / 定时 / 重试 / 恢复）"
    source_column = page.split('title: "来源"', 1)[1].split('title: "状态"', 1)[0]
    assert "<Tooltip" not in source_column, (
        "来源列不许再挂 Tooltip（2026-09-26 口径「来源列的 tip 全部移除」）"
    )
    assert page.count("title={taskSourceTip(") == 1, (
        "来源说明只允许出现在**详情抽屉**一处（列表列的 tip 已按口径移除）"
    )
    assert "taskSourceTip(" in page, "来源说明必须来自 lib/task-records（不写死文案）"

    # ④b-2 类型列 —— **2026-09-26 第三轮用户口径「类型展示为执行队列中文展示」**：
    #     走 lib 的 `queueLabelForTask`（队列名由后端 `QUEUE_REGISTRY.label` 下发，
    #     前端不写死队列清单）；列表**不再**铺内部类型代码（排障看详情抽屉）。
    assert "queueLabelForTask(" in page, (
        "类型列必须走 lib 的队列中文名（写死映射表 = 新队列上线界面不显示）"
    )
    type_column = page.split('title: "类型"', 1)[1].split('title: "来源"', 1)[0]
    assert "{row.task_type}" not in type_column, (
        "类型列不再显示内部类型代码（2026-09-26 口径：类型列展示队列中文名）"
    )

    # ④c **2026-09-26 用户口径**（两轮，后者覆盖前者的「状态列移除」）：执行进度列
    #    移除；**状态列必须回来**（用户当晚口径「队列执行记录列表缺少状态列」）；
    #    执行详情列用多行简单列出。
    assert 'title: "执行详情"' in page, "列表必须有「执行详情」列（多行简单列出执行内容）"
    assert "executionDetailLines(" in page, "执行详情文案必须走 lib/task-records（DEF-044）"
    assert 'title: "状态"' in page, "状态列必须回来（2026-09-26 用户口径「缺少状态列」）"
    assert "taskStatusLabel(" in page and "taskStatusColor(" in page, (
        "状态列必须走 lib/task-records 的中文名与颜色（不许在页面里另写一份）"
    )
    assert 'title: "进度"' not in page, "执行进度列已按 2026-09-26 用户口径移除，不许回来"
    assert "row.progress" not in page, "进度值不该再被渲染（列已移除）"
    # 时间 / 耗时两列保留（用户再一轮口径点名「批次创建时间」）
    assert 'title: "批次创建时间"' in page and 'title: "耗时"' in page, (
        "「批次创建时间」列与「耗时」列必须保留"
    )
    assert 'title: "创建时间"' not in page, (
        "列名已按用户口径改成「批次创建时间」（这一列只对批次行有意义）"
    )

    # ④d **手动清理**（2026-09-26 用户口径「新增手动批量清理……操作列单独清理记录
    #     和一键清除全部；清理逻辑不包含待重试和进行中状态的记录；前端不允许被
    #     选中」）：三个入口（操作列单清 / 清理选中 / 一键清除全部）+ 复选框禁用
    #     判据必须接线；判据走 lib 的 canCleanTask（与后端 CLEANABLE_STATUSES 同一）。
    assert "cleanTasks(" in page and "cleanAllTasks(" in page, (
        "页面必须接入手动清理两个端点（显式 ids + 一键全部）"
    )
    assert "canCleanTask(" in page, "可清理判据必须走 lib/task-records（前端体验，后端边界）"
    assert "rowSelection" in page and "getCheckboxProps" in page, (
        "行选择必须按状态禁用复选框（待重试 / 进行中不允许被选中）"
    )
    assert "清理选中" in page and "一键清除全部" in page and "清理" in page, (
        "操作列与工具条都要有清理入口"
    )
    # ④d' **清理的两个维度**（2026-09-26 用户再一轮口径「支持按照批次清理记录……清理
    #     功能分为两个维度，批次维度和批次内的操作，批次中存在正在执行或待执行得记录，
    #     则不支持该批次全部清理」）：批次维度（整批，原子）与批次内维度（单条）必须
    #     分开实现；批次维度不可清时**给原因**（Tooltip），不许让按钮凭空消失让人猜。
    assert '{batch ? "清理整批" : "清理"}' in page, (
        "操作列必须按维度给文案：批次行「清理整批」/ 明细行「清理」"
        "（用户要能分清清的是这一批还是这一条）"
    )
    assert "onCleanBatch(" in page and "onCleanItem(" in page, (
        "两个清理维度必须分开实现（批次维度整批 / 批次内维度单条）"
    )
    # ⚠️ 锚点必须**特指操作列那处调用**：页面里 `cleanableCount` 也算 `batchCleanBlockReason`
    # ⇒ 只断言「函数名出现过」时，把操作列的判据抹成恒 `null`（不可清时不给原因）仍然绿
    #（实测踩中：变异 `noreason` 没被抓住）。
    assert "const cleanBlockReason = batch ? batchCleanBlockReason(row) : null;" in page, (
        "操作列必须算出批次维度的阻塞原因（Tooltip 用），判据走 lib"
        "（与后端 batch_clean_blockers 同一）"
    )
    assert "cleanOutcomeText(" in page, (
        "清理结果必须分层报账（清了几条 / 多少被挡 / 多少被跳过）—— 判据走 lib"
    )

    # ④e **「批次号」与「批次创建时间」两列只展示批次行**（2026-09-26 用户口径
    #     「不做兼容逻辑，数据已清空，批次号 / 记录列只展示批次行，批次创建时间，
    #     注意使用父子树列表」）：两列都以 `isBatchRow` 为闸（明细行留空）—— 明细的
    #     主体在「执行详情」列里逐条列出，不在第一列重复；列表本身仍是**父子树**
    #     （明细挂 `children`，不是平铺）。
    assert 'title: "批次号"' in page, "第一列必须是「批次号」（只展示批次行）"
    assert "批次号 / 记录" not in page, (
        "旧列名「批次号 / 记录」是被取代的兼容形态（一列同时渲染两种行），不许回来"
    )
    assert "if (!isBatchRow(row)) return null;" in page, (
        "「批次号」列必须对明细行留空（`isBatchRow` 当闸，明细行不重复渲染批次号）"
    )
    assert "isBatchRow(row) ?" in page, (
        "「批次创建时间」列必须只对批次行渲染（明细行留空）"
    )
    assert "expandable" in page and "children" in page, (
        "明细必须按 antd 树形 `children` 挂在父行下（父子树列表，不是平铺）"
    )

    # ⑤ 提示走 antd message，不占版面（设计 04 §3.6）
    assert "App.useApp()" in page and "message.error(" in page and "message.success(" in page, (
        "结果提示必须走 antd message（App.useApp() 实例）"
    )
    assert "Alert" not in page, "队列记录页不该有 inline 消息条（连 import 都不许）"
    assert "notification" not in page, "提示统一 message，不用 notice 弹窗（更占空间）"

    # ⑥ 轮询纪律：有未终态才 5 秒一轮，全终态即停
    assert "REFRESH_INTERVAL_MS = 5000" in page, "轮询间隔与两张进度卡同节奏（5s）"
    assert "if (!hasOpen) return;" in page, (
        "只有存在未终态任务时才轮询（去掉这个出口 = 全终态也空转）"
    )

    # ⑦ 缺值口径：耗时走 formatDuration（缺值 `-`，不是 0）
    assert "formatDuration(" in page, "耗时必须走 lib/task-records 的 formatDuration（缺值 -）"
