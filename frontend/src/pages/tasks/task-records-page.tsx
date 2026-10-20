/**
 * 队列记录（设计 13 §35.11，2026-09-25 用户口径「做一个任务记录抽屉/页面」；
 * 同日再定名「**队列记录**」+「显示来源，使用 tip 展示说明」）。
 *
 * 为什么复用 `/tasks` 而不是新页面新接口：同步 / 发布队列的持久化底座（§35.9）**刻意
 * 复用既有 `tasks` 表**，而 `GET /tasks`、`GET /tasks/{id}`（含 `children`）、
 * `POST /tasks/{id}/cancel`（协作式中断）、`POST /tasks/{id}/retry`、`/tasks/clean*`
 * 都已存在 —— 这一页只是把它们接到界面上，**不新增资源**（DEF-044）。
 *
 * **本页只收「队列」**（2026-09-25 用户当场纠正：「队列记录中，记录的是队列中每一条
 * 记录的详情以及执行情况数据」）：账号级全量同步 / 商品同步 / AI / 抓取监控这些
 * **不是队列**的记录不在这里（那些在账号卡片与「任务中心 · 任务日志」看）—— 混进来
 * 就又把本页变成另一个任务日志。
 *
 * ⚠️ **批次形态（2026-09-26 两轮口径，后者覆盖前者）**：早间「不需要队列批次」把
 * 批次层删掉；当日晚间「**将队列执行详情表使用批次+详情父子树风格展示，批次逻辑
 * 不再移除，生成唯一批次号**」把它请回来 ——
 *
 * * 一次入队动作 = **一个唯一批次号**（`NB20260926153324-7K2M`），批次行是**父节点**，
 *   队列项（一篇 / 一个商品）是它的**子节点**；
 * * 列表用 antd Table 的**树形数据**渲染（`children` 由后端 `GET /tasks?tree=1` 下发，
 *   前端不自己拼树 —— 分页截断会把父子拆散）；
 * * 批次行的状态 / 计数 / 耗时由**后端按子行现算**（`batch_rollup`，唯一实现）：
 *   本页只渲染，不重算；
 * * 「这一行是不是批次」的判据是 `batch_no` 有值（**数据判据**，不是类型清单）——
 *   新队列上线后端自动生成批次号，本页零改动；
 * * 批次行**不是可执行任务**：不给取消 / 重试按钮（后端也会 400 拦），只给详情与清理；
 * * **顶级行只有批次行**（用户口径「不做兼容逻辑，数据已清空」）：批次行是队列项的
 *   唯一入口，不存在「无父的队列项行」这种形态；明细一律挂在父行的 `children` 里。
 *
 * ⚠️ **队列清单由后端下发**（`GET /tasks/queues` → `QUEUE_REGISTRY`）：用户口径
 * 「后端服务可能有很多不同的队列」⇒ 前端**不写死**队列类型，新队列上线只加一行注册，
 * 本页自动多出一张卡、筛选项自动多一项（前端零改动）。
 *
 * 与「任务中心 · 任务日志」（`scheduled_job_logs`）**不是一回事**：那一页记「**什么时候跑**」
 * （调度视角，1 次调度 1 行），本页记「**队列里每条跑成什么样**」（业务视角）。
 *
 * 呈现口径：
 * ① 类型中文名 / 来源 / 可取消可重试判据 / 执行详情 / 批次判据全部走
 *    `lib/task-records.ts`（单一真相源，行为脚本 `tests/frontend/task_records.cjs` 打包验证）；
 * ② 失败原因**只在行 `payload.reason` 里给一次**（同步失败不落库到笔记行）⇒
 *    执行详情列与详情抽屉必须显示，不显示就是把用户的自救路径拿走；
 * ③ 队列项的**主体按队列而不同**（§31.10）：同步 / 发布显示笔记标题（入队快照），
 *    **商品生成**显示商品名（生成前笔记不存在，行上带 `item_id` / `item_name`），
 *    终态成功后附带「生成了哪篇笔记」（worker 写进行体）；
 * ④ 缺值一律 `-`（没跑完的计时不是 0）；
 * ⑤ 有未终态任务时 5 秒一轮自动刷新（与内容库进度卡同一节奏），全终态即停；
 * ⑥ 页面**不留 inline 消息条**：结果提示走 antd `message`，说明性文字走 Tooltip
 *    （设计 04 §3.6，2026-09-25 用户口径「使用 tip 展示说明」）；
 * ⑦ **2026-09-26 用户口径**：执行进度列移除；**状态列保留**；来源**具体到按钮**；
 *    执行详情用**多行简单列出**；**批次号可查询**（同日口径「新增列表支持使用唯一
 *    批次号查询」）；
 * ⑧ **2026-09-26 再一轮口径**（同日覆盖 ⑦ 的表格形态）：「**不做兼容逻辑，数据已
 *    清空，批次号 / 记录列只展示批次行，批次创建时间，注意使用父子树列表**」——
 *    顶级行**只列批次行**（不再有「无父的队列项行」这种存量形态）；「批次号」与
 *    「批次创建时间」两列**只对批次行渲染**（明细行在这两列留空 —— 明细的主体在
 *    「执行详情」列里逐条列出，不在第一列重复；时间看明细自己的耗时列）；
 *    明细仍按 antd 树形 `children` 挂在父行下（父子树列表，不是平铺）。
 * ⑨ **同日晚间口径「批次记录执行的账号」**：新增**「账号」列**（同一列管两级）——
 *    明细行取自己 `payload.account_id`（入队时的账号快照，发布 / 商品生成 / 同步
 *    三个队列都写）；批次行取后端**按子行派生**的 `accounts`（`batch_account_ids`
 *    去重保序）⇒ 单账号显名、跨账号显「N 个账号」+ Tooltip 列出是哪几个（一次批量
 *    可以跨账号勾选，批次级硬报一个账号名就是报错的）。账号名映射 = `fetchAccounts()`
 *    + `accountNameMap`（**只放账号名**，与账号下拉同口径）；名字缺失退 `账号 #id`
 *    （与 `笔记 #pk` 同型的可辨认兜底，不造名字）。
 * ⑩ **2026-09-26 第三轮口径**：**「类型」列展示执行队列的中文名**（后端
 *    `QUEUE_REGISTRY.label`，如「笔记同步队列」；队列名与队列卡片 / 队列筛选器
 *    **同一份来源**，前端不写死队列清单）—— 内部类型代码不再铺在列表里，排障时
 *    在**详情抽屉**仍能看到；**「来源」列的 Tooltip 全部移除**（列表只留一个 Tag，
 *    说明性文字只留在详情抽屉那一处，不在列表每行挂浮层）。
 */
import { WaitMask } from "../../components/ui/wait-mask";
import { DeleteOutlined, QuestionCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  App,
  Button,
  Card,
  Col,
  Drawer,
  Empty,
  Input,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";

import type { QueueSummary } from "../../lib/api";
import {
  cancelTask,
  cleanAllTasks,
  cleanTasks,
  fetchAccounts,
  fetchQueueRecords,
  fetchQueues,
  fetchSavedNote,
  fetchTaskDetail,
  retryTask,
} from "../../lib/api";
import {
  TASK_RECORD_GROUP_LABELS,
  accountNameMap,
  batchCleanBlockReason,
  batchExecutionDetailLines,
  canCleanTask,
  canCancelTask,
  canRetryTask,
  cleanOutcomeText,
  executionDetailLines,
  filterBatchRows,
  formatDuration,
  genItemNoteText,
  genItemProductText,
  hasChildren,
  isBatchRow,
  isProductGenItem,
  isQueueTaskType,
  itemOutcomeText,
  queueLabelForTask,
  taskAccountText,
  taskAccountTip,
  taskSourceLabel,
  taskSourceTip,
  taskStatusColor,
  taskStatusLabel,
} from "../../lib/task-records";
import { formatShanghaiTime } from "../../lib/time";
import type { AccountNameMap, CleanResultShape } from "../../lib/task-records";
import type { SavedNote, TaskRecord } from "../../types";

const { Text } = Typography;

/**
 * 一次拉多少个**批次（顶级行）**；后端每行再带 ≤20 条明细挂在 `children` 里。
 * 后端 `limit` 上限 100（`QUEUE_RECORD_MAX_ROWS`），50 够翻一页历史又不至于卡。
 */
const TASKS_PAGE_SIZE = 50;

/** 轮询间隔：与内容库两张进度卡同一节奏（用户口径「数据 5 秒更新一次」）。 */
const REFRESH_INTERVAL_MS = 5000;

/**
 * 「状态」列的**表头筛选**选项（2026-09-26 用户口径「将队列执行记录表中的状态筛选放到
 * 批次内，父子树表不使用同一个表头，在表头状态列加筛选条件」）。
 *
 * 不含「全部」：antd 列筛选自带「重置」，`all` 只是本页的内部口径（`status` state 的
 * 初值），不该出现在下拉里当第三个选项。
 */
const STATUS_FILTERS = ["pending", "running", "done", "failed", "exhausted", "cancelled"].map(
  (value) => ({ text: taskStatusLabel(value), value })
);

export function TaskRecordsPage() {
  const { message, modal } = App.useApp();

  // 队列清单（后端下发，设计 13 §35.11）：新队列只加一行注册，本页零改动
  const [queues, setQueues] = useState<QueueSummary[]>([]);
  const [queueKey, setQueueKey] = useState<string>("all");
  // 两个 Tab（2026-09-25 用户口径）：队列管理 / 队列执行记录
  const [activeTab, setActiveTab] = useState<string>("records");
  const [rows, setRows] = useState<TaskRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<string>("all");
  // 批次号查询（2026-09-26 用户口径「新增列表支持使用唯一批次号查询」）：
  // `batchNoInput` = 输入框里的字，`batchNoQuery` = **已提交**的查询条件
  // （两者分开，否则打字时每按一个键就打一次接口）。
  const [batchNoInput, setBatchNoInput] = useState("");
  const [batchNoQuery, setBatchNoQuery] = useState("");
  const [actionLoading, setActionLoading] = useState<number | null>(null);
  // 手动清理（2026-09-26 用户口径）：选中集合 + 清理中 + 一键清除中。
  // 选中只收**可清**状态（done / cancelled）—— 待重试与进行中的行复选框禁用。
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [cleaning, setCleaning] = useState(false);
  const [cleaningAll, setCleaningAll] = useState(false);

  // 详情抽屉：批次行 = 父行 + 逐条明细；队列项行 = 单条（后端 `children` 空数组）
  const [detail, setDetail] = useState<TaskRecord | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [titles, setTitles] = useState<Record<number, string>>({});

  // 账号名映射（2026-09-26 用户口径「批次记录执行的账号」）：「账号」列要把
  // `payload.account_id`（明细行）/ 后端派生的 `accounts`（批次行）翻成**账号名**。
  // 账号清单**不随记录轮询变** ⇒ 独立拉一次（塞进 5 秒一轮的 `load()` 是白花请求）；
  // 拉失败**安静**：列退 `账号 #id`（可辨认的兜底），不把「列显示得不够好」报成错误。
  const [accountNames, setAccountNames] = useState<AccountNameMap>({});
  useEffect(() => {
    let alive = true;
    void fetchAccounts()
      .then((list) => {
        if (alive) setAccountNames(accountNameMap(list));
      })
      .catch(() => {
        /* 拿不到账号清单 ⇒ 「账号」列退 `账号 #id`（不是错误，不打扰用户） */
      });
    return () => {
      alive = false;
    };
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [queueList, recordList] = await Promise.all([
        fetchQueues(),
        fetchQueueRecords(TASKS_PAGE_SIZE, batchNoQuery),
      ]);
      setQueues(queueList);
      // ⚠️ **只留队列记录**（用户 2026-09-25 纠正：这一页记「队列中每一条记录的详情与
      // 执行情况」，不是任务日志）：账号级同步 / 商品同步 / AI / 抓取这些**不是队列**
      // 的记录一律过滤掉 —— 混进来就又把本页变成另一个任务日志。
      // 后端顶级行只给批次行（`GET /tasks?tree=1`），这里的白名单是**第二道闸**：
      // 万一后端形状变了，也不会把非队列记录带进这一页（不是存量兼容）。
      setRows((recordList.items ?? []).filter((row) => isQueueTaskType(row.task_type)));
    } catch {
      // 提示归全局拦截器（两个端点都非 `_silent`）；这里只负责不把旧数据当新的
    } finally {
      setLoading(false);
    }
  }, [batchNoQuery]);

  useEffect(() => {
    void load();
  }, [load]);

  // 有未终态任务才轮询（全终态即停 —— 与进度卡「待处理为 0 就停」同口径）。
  // ⚠️ 子行也要看：批次行状态是派生的，光看父行会在「父行还算 pending」时漏判。
  const hasOpen = useMemo(() => {
    const isOpen = (row: TaskRecord) => row.status === "pending" || row.status === "running";
    return rows.some((row) => isOpen(row) || (row.children ?? []).some(isOpen));
  }, [rows]);
  useEffect(() => {
    if (!hasOpen) return;
    const timer = window.setInterval(() => {
      void load();
    }, REFRESH_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [hasOpen, load]);

  // 筛选只管**批次行**（顶级行只有批次行；状态按子行**派生**，带 `summary` 的那个）。
  // 批次命中时它的明细**全部**展示 —— 用户在看「这一批」，把明细按状态再筛一遍会
  // 让「共 5 条」变成「只显示 1 条」，数就对不上了。
  //
  // 2026-09-26 用户口径「将状态筛选放到批次内，父子树表不使用同一个表头」：状态这个
  // 条件只作用在**批次（父）行**上，入口是「状态」列的**表头**（`STATUS_FILTERS` +
  // 受控 `filteredValue`），工具栏不再有状态下拉。真过滤走 lib 的 `filterBatchRows`
  // （**唯一实现**；表头列上不写 `onFilter`，否则同一判据两份实现 —— DEF-044）。
  const filtered = useMemo(
    () => filterBatchRows(rows, { queueKey, queues, status }),
    [rows, queueKey, queues, status]
  );

  const openDetail = useCallback(async (row: TaskRecord) => {
    setDetailLoading(true);
    setTitles({});
    try {
      const full = await fetchTaskDetail(row.id);
      setDetail(full);
      // 子行标题：**入队快照优先**（§35.9 起行上带 `note_title`，直接显示）；
      // 只有早期数据（没有快照）才按需拉（一次 Promise.all；子行有界 ≤ 批量
      // 上限 20，失败安静）—— 拉不到的标题不编，缺了就不显示。
      const next: Record<number, string> = {};
      const missing: number[] = [];
      for (const child of full.children ?? []) {
        const payload = (child.payload ?? {}) as { note_pk?: number; note_title?: string };
        const pk = Number(payload.note_pk) || 0;
        if (!pk) continue;
        const snapshot = String(payload.note_title ?? "").trim();
        if (snapshot) next[pk] = snapshot;
        else missing.push(pk);
      }
      if (missing.length) {
        const notes = await Promise.all(
          missing.map((pk) => fetchSavedNote(pk, true).catch(() => null))
        );
        notes.forEach((note: SavedNote | null, index: number) => {
          if (note) next[missing[index]] = note.title || "未命名";
        });
      }
      setTitles(next);
    } catch {
      message.error("任务详情加载失败。");
    } finally {
      setDetailLoading(false);
    }
  }, [message]);

  const onCancel = useCallback(
    async (row: TaskRecord) => {
      setActionLoading(row.id);
      try {
        await cancelTask(row.id);
        message.success(`已取消任务 #${row.id}。`);
        await load();
        if (detail?.id === row.id) setDetail(await fetchTaskDetail(row.id));
      } catch {
        message.error("取消失败。");
      } finally {
        setActionLoading(null);
      }
    },
    [detail?.id, load, message]
  );

  const onRetry = useCallback(
    async (row: TaskRecord) => {
      setActionLoading(row.id);
      try {
        await retryTask(row.id);
        message.success(`已重新入队任务 #${row.id}。`);
        await load();
        if (detail?.id === row.id) setDetail(await fetchTaskDetail(row.id));
      } catch {
        message.error("重试失败。");
      } finally {
        setActionLoading(null);
      }
    },
    [detail?.id, load, message]
  );

  // ----------------------------------------------------------------------- #
  // 手动清理（2026-09-26 用户口径「新增手动批量清理……清理逻辑不包含待重试和
  // 进行中状态的记录，前端不允许被选中」）
  // ----------------------------------------------------------------------- #

  /** 清理后收尾：刷新列表 + 清空选中 + **如实分层报账**（批次几个 / 明细几条 /
   *  多少因批次维度被挡 / 多少因状态不可清被跳过 —— 只说「已清理 N 条」会让用户
   *  以为剩下的也清掉了）。文案判据走 lib（单一真相源）。 */
  const afterClean = useCallback(
    (result: CleanResultShape) => {
      setSelectedIds([]);
      void load();
      const outcome = cleanOutcomeText(result);
      if (outcome.level === "success") message.success(outcome.text);
      else if (outcome.level === "warning") message.warning(outcome.text);
      else message.info(outcome.text);
    },
    [load, message]
  );

  /** **批次维度**清理（2026-09-26 用户口径「支持按照批次清理记录」）：整批 ——
   *  批次 + 它的全部明细一次删。**原子动作**：批内有正在执行 / 待执行（或待重试）
   *  的明细就**不支持**（按钮已禁用；后端 `batch_clean_blockers` 再拦一道）。
   *  不半清的理由：清一半会在父子树里留下一个残缺批次，用户看不懂「这批怎么只剩两条」。 */
  const onCleanBatch = useCallback(
    (row: TaskRecord) => {
      modal.confirm({
        centered: true,
        title: `清理批次 ${row.batch_no}？`,
        content: `将清理这个批次与它的全部 ${row.summary?.total ?? 0} 条明细记录（不影响笔记本身，也不影响平台数据）。`,
        okText: "清理整批",
        okButtonProps: { danger: true },
        cancelText: "取消",
        onOk: async () => {
          setCleaning(true);
          try {
            afterClean(await cleanTasks([row.id]));
          } catch {
            message.error("清理失败。");
          } finally {
            setCleaning(false);
          }
        },
      });
    },
    [afterClean, message, modal]
  );

  /** **批次内维度**清理（同一口径的后半句「批次内的操作」）：只删这一条明细，
   *  批次与其它明细留着 —— 同批还有要重试的篇时，也能清掉已成功 / 已取消的那条。 */
  const onCleanItem = useCallback(
    (row: TaskRecord) => {
      modal.confirm({
        centered: true,
        title: `清理记录 #${row.id}？`,
        content:
          "清理后这条执行记录不再显示（不影响笔记本身，也不影响平台数据；批次记录与同批其它明细保留）。",
        okText: "清理",
        okButtonProps: { danger: true },
        cancelText: "取消",
        onOk: async () => {
          setCleaning(true);
          try {
            afterClean(await cleanTasks([row.id]));
          } catch {
            message.error("清理失败。");
          } finally {
            setCleaning(false);
          }
        },
      });
    },
    [afterClean, message, modal]
  );

  /** 批量清理选中的记录（复选框只收可清状态，这里是第二道确认）。 */
  const onCleanSelected = useCallback(() => {
    if (!selectedIds.length) return;
    modal.confirm({
      centered: true,
      title: `清理选中的 ${selectedIds.length} 条记录？`,
      content:
        "只有已完成 / 已取消的记录会被清理；选中批次 = 整批清理（连它的全部明细一起）。批内有正在执行 / 待执行（或待重试）明细的批次不会被清理 —— 整批是原子动作，不做半清。",
      okText: "清理",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        setCleaning(true);
        try {
          afterClean(await cleanTasks(selectedIds));
        } catch {
          message.error("清理失败。");
        } finally {
          setCleaning(false);
        }
      },
    });
  }, [afterClean, message, modal, selectedIds]);

  /** 一键清除全部可清记录（done / cancelled；待重试 / 进行中后端不碰）。 */
  const onCleanAll = useCallback(() => {
    modal.confirm({
      centered: true,
      title: "一键清除全部执行记录？",
      content:
        "将清除全部已完成 / 已取消的队列记录：整批可清的批次连明细一起清除。批内有正在执行 / 待执行（或待重试）明细的批次，与它可清的明细都不会被清除 —— 不把还在使用的批次清成残的。此操作不可恢复。",
      okText: "全部清除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        setCleaningAll(true);
        try {
          afterClean(await cleanAllTasks());
        } catch {
          message.error("清除失败。");
        } finally {
          setCleaningAll(false);
        }
      },
    });
  }, [afterClean, message, modal]);

  /** 可**整批清理**的批次数（「一键清除全部」的禁用判据 + 按钮上的预期数）。
   *
   *  一键清除走的是**批次维度** ⇒ 只数「整批可清」的批次（`batchCleanBlockReason`
   *  为 `null`）。⚠️ 不能去数明细行：批内有活 / 待重试明细时，它那些可清的明细
   *  **也不会**被清，数进去会让按钮上的数字比实际清掉的多（数字必须对得上行为）。
   *  列表顶级行只有批次行（`filtered` 即批次行集合）。 */
  const cleanableCount = useMemo(
    () => filtered.filter((row) => batchCleanBlockReason(row) === null).length,
    [filtered]
  );

  // ----------------------------------------------------------------------- #
  // 列（**一套列同时渲染父行与子行**：树形表格的展开箭头在第一列；批次行专属的
  //     两列 —— 「批次号」与「批次创建时间」—— 对明细行**不渲染**，
  //     2026-09-26 用户口径「批次号 / 记录列只展示批次行」）
  // ----------------------------------------------------------------------- #
  const columns: ColumnsType<TaskRecord> = [
    {
      // 第一列（树形表格的展开箭头就落在这里）：**只展示批次行** —— 唯一批次号。
      // 明细行（子节点）在这一列**留空**：它们各是一条队列项的详情，主体已经在
      // 「执行详情」列里逐条列出（商品名 / 笔记标题），再在这列重复一遍是同一件事
      // 说两遍，也让「这一列 = 批次的身份证」的语义糊掉。
      title: "批次号",
      key: "batch_no",
      width: 220,
      render: (_: unknown, row) => {
        if (!isBatchRow(row)) return null;
        return (
          <div>
            <Text strong style={{ fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace" }}>
              {row.batch_no}
            </Text>
            <Text type="secondary" style={{ fontSize: 12, display: "block" }}>
              批次 #{row.id} · {row.summary?.total ?? 0} 条明细
            </Text>
          </div>
        );
      },
    },
    {
      // 账号（2026-09-26 用户口径「批次记录执行的账号」）：这一行记录是**哪个平台
      // 账号**跑的。两级判据不同、**同一列**：
      //  * 明细行 = 它自己 `payload.account_id`（入队快照；发布 / 商品生成 / 同步
      //    三个队列都写）⇒ 显示账号名；
      //  * 批次行 = 后端按子行**派生**的 `accounts`（`batch_account_ids` 去重保序）⇒
      //    单账号显名、跨账号显「N 个账号」+ Tooltip 列出是哪几个。一次批量可以
      //    跨账号勾选 ⇒ 硬报一个账号名是**报错的**，所以批次级不给单一账号。
      // 文案 / 兜底（`账号 #id`）/ Tooltip 全部走 lib（DEF-044），本列只排版。
      title: "账号",
      key: "account",
      width: 132,
      render: (_: unknown, row) => {
        const text = taskAccountText(row, accountNames);
        const tip = taskAccountTip(row, accountNames);
        // 单账号 / 明细行没有额外可说的 ⇒ 不套 Tooltip（不给用户一个点开只有
        // 一行同样文字的浮层）。
        if (!tip) return text;
        return <Tooltip title={tip}>{text}</Tooltip>;
      },
    },
    {
      // 类型（2026-09-26 第二轮用户口径「队列执行记录列表的类型展示为执行队列中文展示」）：
      // 展示这一行所属**执行队列**的中文名（后端 `QUEUE_REGISTRY.label`，如「笔记同步队列」），
      // 与队列卡片 / 队列筛选器同一份来源（`queueLabelForTask`，前端不写死队列清单）。
      //
      // ⚠️ 内部类型代码（`xhs_note_sync_item`）**不再铺在列表里**：它是排障信息，
      // 想看的人在**详情抽屉**那一行仍能看到（同一事实不在一行字里说两遍）。
      title: "类型",
      dataIndex: "task_type",
      width: 132,
      render: (_: unknown, row) => <div>{queueLabelForTask(row.task_type, queues)}</div>,
    },
    {
      // 来源（2026-09-25 用户口径「显示来源，使用 tip 展示说明」；2026-09-26
      // 「来源列不能是未知，排查所有来源后给每个来源固定值」；**2026-09-26 第二轮
      // 「来源列的 tip 全部移除」**）：定时任务触发时同一次执行在任务中心的
      // 「任务日志」里还有一条调度记录 —— 不显示来源，用户会以为系统在偷偷跑。
      // ⚠️ 本列现在**只留一个 Tag**（无 Tooltip、无 `cursor: help`）；说明性文字
      // （两页分工 / 定时触发 / 早期数据）只留在**详情抽屉**里那处 Tooltip。
      title: "来源",
      dataIndex: "payload",
      width: 108,
      render: (_: unknown, row) => {
        const source = String((row.payload ?? {}).source ?? "");
        return <Tag color={source === "scheduled" ? "blue" : "default"}>{taskSourceLabel(source)}</Tag>;
      },
    },
    {
      // 状态列（2026-09-26 用户口径「队列执行记录列表缺少状态列」—— 补回）。
      // 批次行的状态是后端按子行**派生**的（`batch_rollup`），所以这一个列定义
      // 同时管两级，且与「可清理判据」自洽（不可清的明细 ⇒ 批次也不可清）。
      // 表头筛选（2026-09-26 用户口径「状态筛选放到批次内，父子树表不使用同一个
      // 表头」）：**只筛批次行**（顶级行）—— 明细行不参与，命中批次整批显示，
      // 否则「共 N 条明细」会被筛成「只显示 1 条」，数对不上。
      title: "状态",
      dataIndex: "status",
      width: 96,
      filters: STATUS_FILTERS,
      // 单选：与原工具栏下拉同语义（状态是「看哪一类」，不是多选并集）。
      filterMultiple: false,
      // 受控 + **不写 `onFilter`**：真过滤始终由下面 `filtered` 那个 useMemo 一手
      // 完成（DEF-044 同一语义不留两份实现），表头只是入口。写了 `onFilter` antd
      // 会再按同一条件筛一遍，等于同一判据两个实现。
      filteredValue: status === "all" ? null : [status],
      render: (_: unknown, row) => <Tag color={taskStatusColor(row.status)}>{taskStatusLabel(row.status)}</Tag>,
    },
    {
      // 执行详情（2026-09-26 用户口径「执行详情，使用多行简单列出执行具体内容」）：
      // 队列项 = 主体 + 结果（失败带真实原因 —— 同步失败的原因只在行上给一次）；
      // 批次行 = 「共 N 条明细」+ 非零计数（计数取后端 summary，前端不重算）。
      // 判据与文案全部走 lib/task-records（DEF-044），本列只负责排版。
      // ⚠️ 字色必须走 antd 语义 `Text`（type="secondary"）—— 本应用默认**暗色
      // 主题**（Table 底 #1a1a1a），硬编码黑色 rgba 会黑字画在深底上直接看不见
      // （2026-09-26 用户报「黑色字体看不清」）。
      title: "执行详情",
      key: "detail",
      render: (_: unknown, row) => {
        const lines = isBatchRow(row)
          ? batchExecutionDetailLines(row)
          : executionDetailLines(row);
        if (!lines.length) return "-";
        return (
          <div>
            {lines.map((line, index) => (
              <div key={index}>
                <Text type={index === 0 ? undefined : "secondary"} style={{ fontSize: index === 0 ? 13 : 12 }}>
                  {line}
                </Text>
              </div>
            ))}
          </div>
        );
      },
    },
    {
      // 批次创建时间（2026-09-26 用户口径「批次创建时间」）：批次行落库的时刻
      // （懒创建 ⇒ 就是用户点下那个按钮、第一条真入队的时刻）。
      // 明细行**不渲染**：一条明细的「创建时间」逐条看没有意义（它落在批次之后
      // 一秒内），明细自己的时间看右边「耗时」列。
      title: "批次创建时间",
      dataIndex: "created_at",
      width: 176,
      render: (_: unknown, row) => (isBatchRow(row) ? formatShanghaiTime(row.created_at) : null),
    },
    {
      // 耗时：批次行 = 整批跨度（后端按子行现算）；明细行 = 这一条自己跑了多久。
      title: "耗时",
      dataIndex: "duration_ms",
      width: 96,
      render: (_: unknown, row) => formatDuration(row.duration_ms),
    },
    {
      title: "操作",
      key: "actions",
      width: 200,
      render: (_: unknown, row) => {
        // 批次行是**入队动作的记账行**，不是可执行任务：不给取消 / 重试
        // （后端同样 400 —— 只改父行会让父子树自相矛盾）。
        const batch = isBatchRow(row);
        // 清理**两个维度**（2026-09-26 用户口径「清理功能分为两个维度，批次维度和
        // 批次内的操作」）：
        //  * 批次行 → 「清理整批」：整批原子删除，批内有**正在执行 / 待执行**（或
        //    待重试）的明细就**禁用 + Tooltip 说明原因**（`batchCleanBlockReason`，
        //    与后端 `batch_clean_blockers` 同判据）—— 功能在位、条件不满足，
        //    比按钮凭空消失更说得清；
        //  * 明细行 → 「清理」：只删这一条，批次与同批其它明细留着。
        const cleanBlockReason = batch ? batchCleanBlockReason(row) : null;
        return (
          <Space size={4}>
            <Button type="link" size="small" style={{ padding: 0 }} onClick={() => void openDetail(row)}>
              详情
            </Button>
            {!batch && canCancelTask(row.status) && (
              <Button
                type="link"
                size="small"
                danger
                style={{ padding: 0 }}
                loading={actionLoading === row.id}
                onClick={() => void onCancel(row)}
              >
                取消
              </Button>
            )}
            {!batch && canRetryTask(row.status) && (
              <Button
                type="link"
                size="small"
                style={{ padding: 0 }}
                loading={actionLoading === row.id}
                onClick={() => void onRetry(row)}
              >
                重试
              </Button>
            )}
            <Tooltip
              title={
                cleanBlockReason ??
                (canCleanTask(row.status)
                  ? undefined
                  : "只有已完成 / 已取消的明细可以清理；待重试的先点重试，进行中的等它跑完。")
              }
            >
              {/* ⚠️ disabled 的 Button 不冒泡鼠标事件 ⇒ Tooltip 必须套一层 span
                  （否则「为什么不能清」的说明根本弹不出来）。 */}
              <span style={{ display: "inline-flex" }}>
                <Button
                  type="link"
                  size="small"
                  danger
                  style={{ padding: 0 }}
                  disabled={batch ? Boolean(cleanBlockReason) : !canCleanTask(row.status)}
                  loading={cleaning}
                  onClick={() => (batch ? onCleanBatch(row) : onCleanItem(row))}
                >
                  {batch ? "清理整批" : "清理"}
                </Button>
              </span>
            </Tooltip>
          </Space>
        );
      },
    },
  ];

  /** 抽屉里的逐条明细列（批次行才有；队列项行用它渲染单条）。 */
  const childColumns: ColumnsType<TaskRecord> = [
    { title: "ID", dataIndex: "id", width: 72 },
    {
      // 队列项的**主体**（§31.10 起有两种）：笔记绑定队列（同步 / 发布）显示笔记
      // 标题（入队快照 `note_title` 优先，早期数据退 `titles` 按需拉）；
      // **商品生成队列**显示商品名（生成前笔记不存在，行上带 `item_id` /
      // `item_name`），终态成功后附带「生成了哪篇笔记」。
      title: "笔记 / 商品",
      key: "subject",
      render: (_: unknown, row) => {
        const payload = (row.payload ?? {}) as {
          note_pk?: number;
          item_id?: string;
          item_name?: string;
          note_title?: string;
          note_id?: string;
        };
        if (isProductGenItem(row.task_type)) {
          const noteText = genItemNoteText(payload);
          return (
            <div>
              <div>{genItemProductText(payload)}</div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                item_id {payload.item_id || "-"}
                {noteText ? ` · 已生成《${noteText}》` : ""}
              </Text>
            </div>
          );
        }
        const pk = Number(payload.note_pk) || 0;
        if (!pk) return "-";
        const title = String(payload.note_title ?? "").trim() || titles[pk];
        return (
          <div>
            <div>{title ?? `#${pk}`}</div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              note_pk {pk}
            </Text>
          </div>
        );
      },
    },
    {
      title: "结果",
      key: "outcome",
      render: (_: unknown, row) => {
        if (row.status === "pending") return <Tag color="default">待处理</Tag>;
        if (row.status === "running") return <Tag color="processing">进行中</Tag>;
        if (row.status === "cancelled") return <Tag color="warning">已取消</Tag>;
        const payload = (row.payload ?? {}) as { ok?: boolean; reason?: string; code?: string };
        return row.status === "done" ? (
          <Tag color="success">{itemOutcomeText(payload)}</Tag>
        ) : (
          <Tooltip title={itemOutcomeText(payload)}>
            <Tag color="error">{itemOutcomeText(payload)}</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "耗时",
      dataIndex: "duration_ms",
      width: 96,
      render: (_: unknown, row) => formatDuration(row.duration_ms),
    },
  ];

  /** 抽屉里「账号」行要多账号明细（列表列里收敛成「N 个账号」，抽屉里列清名字）。 */
  const detailAccountTip = detail ? taskAccountTip(detail, accountNames) : "";

  return (
    <div>
      {/* 2026-10-05 用户口径「移除所有页面页面头」：原「队列记录 / 队列记录 / 说明行
          + Tooltip」整块（两行标题与侧栏菜单项逐字重复）。其中**说明正文按用户口径
          整段移除**（「…文本移除」）—— 本轮先收进「队列管理」页签顶部，随后被
          用户点掉；说明的承载面（队列卡自带计数与「队列类型」行、记录的「批次号」列）
          本身已在页面上，不再需要一段总说明。 */}

      {/* 2026-09-25 用户口径「把列表重构队列管理，分为两个 tab 队列管理和队列执行记录」：
          Tab 1 队列管理 = 队列清单与实时概览；Tab 2 队列执行记录 = 每一条的详情与执行情况。 */}
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: "queues",
            label: "队列管理",
            children: (
              <>
              <Row gutter={12}>
                {queues.map((queue) => (
                  <Col key={queue.key} xs={24} sm={12} lg={8} style={{ marginBottom: 12 }}>
                    <Card
                      size="small"
                      hoverable
                      title={queue.label}
                      extra={
                        <Button
                          type="link"
                          size="small"
                          style={{ padding: 0 }}
                          onClick={() => {
                            setQueueKey(queue.key);
                            setActiveTab("records");
                          }}
                        >
                          看执行记录
                        </Button>
                      }
                    >
                      <Space direction="vertical" size={8} style={{ width: "100%" }}>
                        <Statistic
                          title="待处理（排队 + 在跑）"
                          value={queue.open}
                          valueStyle={{ fontSize: 22, color: queue.open > 0 ? "#1677ff" : undefined }}
                        />
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          近 15 天：成功 {queue.done} · 失败 {queue.failed}
                          {queue.cancelled ? ` · 取消 ${queue.cancelled}` : ""}
                        </Text>
                        {/* 队列类型是这行的**唯一标识**（2026-09-26 用户口径
                            「直接使用队列类型不就可以了，唯一 key 肯定是唯一的，
                            不需要多一个批次」）：不再把 `batch_task_type` 上屏 ——
                            它是注册表常量（每个队列恒定，如 `xhs_note_gen_batch`），
                            标成「批次」会被读成「批次号」，而真实的批次号是每次入队
                            动作自动生成的 `NB…`（那是「队列执行记录」Tab 的「批次号」列）。
                            ⚠️ `batch_task_type` **仍由后端下发**：`queueKeyForTask` 靠它把
                            批次行归回本队列做筛选（不上屏 ≠ 能删）。 */}
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          队列类型 {queue.item_task_type}
                        </Text>
                      </Space>
                    </Card>
                  </Col>
                ))}
                {!queues.length && (
                  <Col span={24}>
                    <Empty description="后端还没有注册任何队列（QUEUE_REGISTRY 为空）" />
                  </Col>
                )}
              </Row>
              </>
            ),
          },
          {
            key: "records",
            label: "队列执行记录",
            children: (
              <>
                <Card
                  size="small"
                  style={{ marginBottom: 16 }}
                  extra={
                    <Space size={8}>
                      {/* 手动清理（2026-09-26 用户口径「新增手动批量清理」）：
                          批量清理选中的 + 一键清除全部可清的。两个都只对
                          done / cancelled 生效（待重试 / 进行中后端不碰）。 */}
                      <Button
                        danger
                        size="small"
                        icon={<DeleteOutlined />}
                        disabled={!selectedIds.length}
                        loading={cleaning}
                        onClick={onCleanSelected}
                      >
                        清理选中{selectedIds.length ? `（${selectedIds.length}）` : ""}
                      </Button>
                      <Button
                        danger
                        size="small"
                        type="primary"
                        icon={<DeleteOutlined />}
                        disabled={cleanableCount === 0}
                        loading={cleaningAll}
                        onClick={onCleanAll}
                      >
                        一键清除全部{cleanableCount ? `（${cleanableCount}）` : ""}
                      </Button>
                      <Button
                        icon={<ReloadOutlined />}
                        size="small"
                        loading={loading}
                        onClick={() => void load()}
                      >
                        刷新
                      </Button>
                    </Space>
                  }
                >
                  <Space wrap>
                    <Input.Search
                      // 批次号查询（2026-09-26 用户口径「新增列表支持使用唯一批次号查询」）：
                      // 受控输入 + 回车 / 点搜索才提交（打字即查会把接口打穿）。
                      // 支持只输入尾段（后端做「不区分大小写的包含」匹配）。
                      allowClear
                      value={batchNoInput}
                      placeholder="按批次号查询（可只输入尾段，如 7K2M）"
                      style={{ width: 280 }}
                      onChange={(event) => setBatchNoInput(event.target.value)}
                      onSearch={(value) => setBatchNoQuery(String(value ?? "").trim())}
                    />
                    {batchNoQuery && (
                      <Button
                        size="small"
                        onClick={() => {
                          setBatchNoInput("");
                          setBatchNoQuery("");
                        }}
                      >
                        清除查询
                      </Button>
                    )}
                    <Select
                      value={queueKey}
                      onChange={setQueueKey}
                      style={{ width: 180 }}
                      options={[
                        { value: "all", label: "全部队列" },
                        ...queues.map((queue) => ({ value: queue.key, label: queue.label })),
                      ]}
                    />
                    {/* 状态筛选已移到「状态」列的**表头**（2026-09-26 用户口径
                        「将状态筛选放到批次内，父子树表不使用同一个表头，在表头状态列
                        加筛选条件」）：工具栏只留队列筛选 —— 同一件事不留两个入口。 */}
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      共 {filtered.length} 个批次
                      {batchNoQuery ? `（批次号含「${batchNoQuery}」）` : ""}
                      {hasOpen ? "（有任务在执行，5 秒自动刷新）" : ""}
                    </Text>
                  </Space>
                </Card>

                {/* 2026-10-20 统一：表格等待由公共 WaitMask 表达（Table 自带 loading 会渲染

                    antd Spin 覆盖，与 WaitMask 叠成两层 ⇒ 已移除）。 */}

                <WaitMask open={loading} mask={false} title="正在加载队列记录" />

                <Table<TaskRecord>
                  // ⚠️ `key` 随查询条件变 ⇒ 换一次查询 **重挂载** 一次，让
                  // `defaultExpandAllRows` 生效（查批次号时用户就是要看这一批的明细，
                  // 否则得手点一次展开才知道结果对不对）。
                  key={batchNoQuery ? `batch-${batchNoQuery}` : "all"}
                  rowKey="id"
          //: ⚠️ 2026-10-20 统一：移除 Table 自带 loading（它渲染 antd Spin，会与 WaitMask 叠两层）。
                  columns={columns}
                  dataSource={filtered}
                  // 父子树：`children` 由后端下发（前端不自己拼树 —— 分页截断会把
                  // 父子拆散）。空的 `children` 数组 ⇒ 没有展开箭头（队列项行）。
                  expandable={{ defaultExpandAllRows: Boolean(batchNoQuery) }}
                  pagination={{ pageSize: 20, showSizeChanger: false }}
                  // 表头状态筛选的入口（2026-09-26 用户口径）：只把**选中的状态**写回
                  // state，真过滤在上面的 `filtered` 里（受控 `filteredValue`，
                  // 不写 `onFilter`）。`filterMultiple: false` ⇒ 数组最多一项；
                  // 点「重置」（清空）即回 `all`。
                  onChange={(_pagination, filters) => {
                    const picked = (filters.status ?? [])[0];
                    setStatus(picked ? String(picked) : "all");
                  }}
                  // 行选择（2026-09-26 用户口径「前端不允许被选中」）：待重试
                  // （failed / exhausted）与进行中（pending / running）的行
                  // **复选框禁用** —— 判据与后端 CLEANABLE_STATUSES 同一个
                  // （lib 的 canCleanTask），前端是体验、后端是边界。
                  // 批次行同样按**派生状态**判（子行全可清它才可清）。
                  rowSelection={{
                    selectedRowKeys: selectedIds,
                    onChange: (keys) => setSelectedIds(keys.map(Number)),
                    getCheckboxProps: (row) => ({
                      disabled: !canCleanTask(row.status),
                      title: canCleanTask(row.status)
                        ? undefined
                        : "只有已完成 / 已取消的记录可以清理（待重试与进行中的不行）",
                    }),
                  }}
                  locale={{
                    emptyText: (
                      <Empty
                        description={
                          batchNoQuery
                            ? `没有批次号包含「${batchNoQuery}」的记录（批次号可在列表第一列核对）`
                            : "还没有队列记录（同步 / 发布 / 生成商品笔记后这里会出现批次与逐条记录）"
                        }
                      />
                    ),
                  }}
                />
              </>
            ),
          },
        ]}
      />

      <Drawer
        title={
          detail
            ? isBatchRow(detail)
              ? `批次 ${detail.batch_no}`
              : `队列记录 #${detail.id}`
            : "队列记录"
        }
        open={detail !== null}
        onClose={() => setDetail(null)}
        width={760}
        loading={detailLoading}
      >
        {detail && (
          <Space direction="vertical" size={16} style={{ width: "100%" }}>
            <Card size="small" title={isBatchRow(detail) ? "这一批" : "这一条"}>
              <Space direction="vertical" size={4}>
                {isBatchRow(detail) && (
                  <Text>
                    批次号：
                    <Text strong copyable style={{ fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace" }}>
                      {detail.batch_no}
                    </Text>
                  </Text>
                )}
                <Text>
                  类型：{queueLabelForTask(detail.task_type, queues)}
                  <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                    {detail.task_type}
                  </Text>
                </Text>
                {/* 账号（2026-09-26 用户口径「批次记录执行的账号」）：抽屉里空间够，
                    多账号批次把名字**直接列清**（列表列里才收敛成「N 个账号」）。 */}
                <Text>
                  账号：{taskAccountText(detail, accountNames)}
                  {detailAccountTip ? `（${detailAccountTip}）` : ""}
                </Text>
                <Text>
                  状态：<Tag color={taskStatusColor(detail.status)}>{taskStatusLabel(detail.status)}</Tag>
                  {isBatchRow(detail) && detail.summary
                    ? `（${batchExecutionDetailLines(detail).join(" · ")}）`
                    : ""}
                </Text>
                {/* 来源也进详情：抽屉里能看到这条是哪个按钮点出来的（Tooltip 说明） */}
                <Text>
                  来源：
                  <Tooltip title={taskSourceTip(String((detail.payload ?? {}).source ?? ""))}>
                    <Tag
                      color={
                        String((detail.payload ?? {}).source ?? "") === "scheduled" ? "blue" : "default"
                      }
                      style={{ cursor: "help" }}
                    >
                      {taskSourceLabel(String((detail.payload ?? {}).source ?? ""))}
                    </Tag>
                  </Tooltip>
                </Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  创建 {formatShanghaiTime(detail.created_at)} · 耗时 {formatDuration(detail.duration_ms)}
                  {detail.retry_count ? ` · 已重试 ${detail.retry_count} 次` : ""}
                </Text>
              </Space>
            </Card>

            {/* 批次行 = 父行 + 逐条明细；队列项行 = 单条（后端给空 children）。
                `hasChildren` 是 lib 的判据（单一真相源）：真出现带 children 的
                队列项行（脏数据）也不静默按单项渲染。 */}
            {hasChildren(detail.task_type, detail.children) ? (
              <Card size="small" title={`逐条明细（${detail.children?.length ?? 0}）`}>
                <Table<TaskRecord>
                  rowKey="id"
                  size="small"
                  columns={childColumns}
                  dataSource={detail.children ?? []}
                  pagination={false}
                />
              </Card>
            ) : (
              <Card size="small" title="这一条">
                <Table<TaskRecord>
                  rowKey="id"
                  size="small"
                  columns={childColumns}
                  dataSource={[detail]}
                  pagination={false}
                />
              </Card>
            )}
          </Space>
        )}
      </Drawer>
    </div>
  );
}
