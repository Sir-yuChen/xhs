/**
 * 任务中心（设计 15 §9.1，2026-09-24 验收修正）——**两个页签**：
 *
 * 1. **任务列表**：全部 `scheduled_jobs` 行（手动执行 / Cron 周期 / 定时一次
 *    合并为单一列表，列一致；操作列按任务类型与状态区分，completed 的不给暂停/恢复）；
 * 2. **任务日志**：每次调度执行一行（成功 / 失败 / 跳过留痕）。
 *
 * ⚠️ **2026-09-27：第三个页签「账号级任务」已移除**（用户口径「任务中心移除账号级任务 (1)
 * tab，点击同步笔记和同步商品属于手动触发的任务，日志应记录在任务日志 tab 下，且注意
 * 更新任务进度数据即可」）。账号卡「同步笔记 / 同步商品」（设计 13 §35.17）本来就各落
 * 一条 `trigger_type=manual` 的日志行 ⇒ 归属「任务日志」；账号级任务行本身仍可在队列
 * 记录页看到（`QUEUE_RECORD_TYPES` 含三类账号级类型），中断 / 重试也在那里。
 *
 * 验收口径（用户 2026-09-24，多轮）：
 * ① 任务页不放说明文案；调度补跑按钮与手动执行记录页签按口径移除
 *    （补跑由调度器自动进行；手动执行的明细在各业务页自行呈现）；
 * ② at 与周期不拆两个页签，合并为单一任务列表；
 * ③ 任务类型三档（R14）：手动执行 / Cron 周期 / 定时一次 ——「间隔周期」并入 Cron；
 * ④ 日志表只展示 6 列（R13）：任务名称 / 任务类型 / 状态 / 触发方式 /
 *    开始执行时间 / 任务进度（进度条 + 耗时或异常），其余进详情弹窗；
 * ⑤ 页面**不留 inline 消息条**：一切结果提示走 antd `message` 轻提示
 *    （2026-09-24 口径「所有的提示都改为弹窗提示」⇒ 2026-09-25 口径
 *    「所有消息提醒参考 antd 成熟组件，不要在页面暂用空间」，设计 04 §3.6；
 *    notice 弹窗比消息条更占空间，被 message 取代）；
 * ⑥ **任务分组整条移除**（2026-09-24 口径「任务分组先去掉」）：列表不再有「分组」列；
 * ⑦ 调用目标**一律两行**（中文名 + 代码键）：列表列与日志详情弹窗同口径
 *    （2026-09-25 用户口径「调用目标需要配置中文名称」—— 弹窗原只显代码键）；
 *    任务名下方不挂「内置」Tag。
 */
import {
  ClockCircleOutlined,
  CloseCircleOutlined,
  DashboardOutlined,
  DeleteOutlined,
  EditOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import {
  App,
  Button,
  Card,
  Col,
  DatePicker,
  Empty,
  Popconfirm,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";

import { useThemePalette } from "../../app/providers";
import { JobFormModal } from "../../components/schedule/job-form-modal";
import { JobLogDetailModal } from "../../components/schedule/job-log-detail-modal";
import { JobLogProgress } from "../../components/schedule/job-log-progress";
import { RunJobModal } from "../../components/schedule/run-job-modal";
import {
  cancelTask,
  changeScheduleJobStatus,
  cleanScheduleLogs,
  deleteScheduleJob,
  deleteScheduleLogs,
  fetchAccounts,
  fetchScheduleJobs,
  fetchScheduleLogs,
  fetchScheduleStatus,
  fetchScheduleTargets,
  runScheduleJobOnce,
} from "../../lib/api";
import {
  concurrentLabel,
  jobStatusTag,
  logStatusTag,
  logTriggerLabel,
  misfirePolicyLabel,
  triggerSummary,
  triggerTypeFullLabel,
  triggerTypeLabel,
} from "../../lib/schedule";
import { accountNameMap } from "../../lib/task-records";
import type { AccountNameMap } from "../../lib/task-records";
import { formatDuration, formatShanghaiTime } from "../../lib/time";
import type {
  ScheduleStatus,
  ScheduledJob,
  ScheduledJobLog,
} from "../../types";

const { Text } = Typography;
const { RangePicker } = DatePicker;

export function TaskCenterPage() {
  const p = useThemePalette();
  const cardStyle = { background: p.cardBg, borderColor: p.border };
  const [activeTab, setActiveTab] = useState("jobs");
  // 全站消息提醒统一走 antd message（设计 04 §3.6，2026-09-25 用户口径：
  // 「所有消息提醒参考 antd 成熟组件，不要在页面暂用空间」）—— 取代 2026-09-24
  // 的 notice 结果弹窗（弹窗遮罩比 inline 消息条更占空间，设计 15 §9.1.1 R22
  // 原句已标注被取代）。`App.useApp()` 的实例继承主题与 locale。
  const { message } = App.useApp();

  // 任务列表（scheduled_jobs：at / cron / interval 全量）
  const [jobs, setJobs] = useState<ScheduledJob[]>([]);
  const [jobsLoading, setJobsLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState(false);

  // 任务日志
  //: `账号 id → 账号名`（详情弹窗「执行账号」逐条显示用）。与订单页同一实现。
  //: 拉一次缓存住 —— 弹窗每次打开都重拉既不必要，也会让回看的**历史快照**随账号改名
  //: 而漂移（那本就该是当时的名字，但快照没存名字，取「当前名」是次优解且要稳定）。
  const [accountNames, setAccountNames] = useState<AccountNameMap>({});
  const [logs, setLogs] = useState<ScheduledJobLog[]>([]);
  const [logsLoading, setLogsLoading] = useState(false);
  const [logJobKey, setLogJobKey] = useState<string | undefined>(undefined);
  const [logTriggerType, setLogTriggerType] = useState<string | undefined>(undefined);
  const [logStatus, setLogStatus] = useState<string | undefined>(undefined);
  const [logRange, setLogRange] = useState<[string, string] | null>(null);
  const [selectedLogIds, setSelectedLogIds] = useState<number[]>([]);

  // 调度器状态 + 弹窗
  const [scheduleStatus, setScheduleStatus] = useState<ScheduleStatus | null>(null);
  // 调用目标名称表（R18：job_key → 中文名；列表「调用目标」列两行展示用）
  const [targetNames, setTargetNames] = useState<Record<string, string>>({});
  const [jobFormOpen, setJobFormOpen] = useState(false);
  const [editingJob, setEditingJob] = useState<ScheduledJob | null>(null);
  // 设计 22 §5：「执行一次」有可编辑参数 ⇒ 弹参数弹窗（无参数任务走 `Popconfirm`）。
  const [runJobTarget, setRunJobTarget] = useState<ScheduledJob | null>(null);
  // 日志详情弹窗（执行时配置快照 + 日志明细）
  const [detailLog, setDetailLog] = useState<ScheduledJobLog | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      setScheduleStatus(await fetchScheduleStatus());
    } catch {
      setScheduleStatus(null);
    }
  }, []);

  const loadJobs = useCallback(async () => {
    setJobsLoading(true);
    try {
      // 一次拉全量（任务行是十位数量级），三种触发器同一张表
      const result = await fetchScheduleJobs({ page_size: 100 });
      setJobs(result.items);
    } catch {
      message.error("任务列表加载失败，请稍后重试。");
    } finally {
      setJobsLoading(false);
    }
  }, []);

  const loadTargets = useCallback(async () => {
    try {
      const items = await fetchScheduleTargets();
      setTargetNames(
        Object.fromEntries(items.map((item) => [item.job_key, item.name]))
      );
    } catch {
      setTargetNames({});
    }
  }, []);

  const loadAccounts = useCallback(async () => {
    try {
      // 只放账号名，与账号下拉同口径（`accountNameMap` 是 lib 的**唯一实现**）。
      setAccountNames(accountNameMap(await fetchAccounts("xhs")));
    } catch {
      // 拉不到就退 `账号 #id`（缺值不猜）；不阻断任务中心其余功能。
      setAccountNames({});
    }
  }, []);

  const loadLogs = useCallback(async () => {
    setLogsLoading(true);
    try {
      const result = await fetchScheduleLogs({
        job_key: logJobKey,
        trigger_type: logTriggerType,
        status: logStatus,
        date_from: logRange?.[0],
        date_to: logRange?.[1],
        page_size: 100,
      });
      setLogs(result.items);
      setSelectedLogIds([]);
    } catch {
      message.error("任务日志加载失败，请稍后重试。");
    } finally {
      setLogsLoading(false);
    }
  }, [logJobKey, logTriggerType, logStatus, logRange]);

  useEffect(() => {
    void loadStatus();
    void loadJobs();
    void loadTargets();
    void loadAccounts();
  }, [loadStatus, loadJobs, loadTargets, loadAccounts]);

  useEffect(() => {
    if (activeTab === "logs") {
      void loadLogs();
    }
  }, [activeTab, loadLogs]);

  function refreshAll() {
    void loadStatus();
    void loadJobs();
    void loadTargets();
    if (activeTab === "logs") {
      void loadLogs();
    }
  }

  /**
   * 取消一次执行（任务日志「操作」列的取消按钮）。
   *
   * 用户口径（2026-09-27）：「点击取消按钮**二次确认**后**真实取消**再执行的任务并
   * **修改对应任务日志记录相关状态和数据**」。前端只做三件事：
   * ① 按日志的 `task_id` 找到业务任务行（缺 `task_id` ⇒ 按钮根本不渲染）；
   * ② 调 `POST /tasks/{id}/cancel` —— **真实中断**由后端完成（行置 `cancelled` + 发
   *    协作式取消信号，任务体在翻页 / 补详情的检查点优雅退出，不再打平台）；
   * ③ 重新拉日志 / 任务列表 —— **日志状态的修改也在后端同一请求内完成**
   *    （`close_running_logs_for_task`），前端不自己改本地状态（那会造第二份真相源）。
   */
  async function cancelLogTask(record: ScheduledJobLog) {
    if (!record.task_id) {
      return;
    }
    try {
      await cancelTask(record.task_id);
      message.success("已取消该次执行");
      void loadLogs();
      void loadJobs();
    } catch {
      message.error("取消失败，请稍后重试。");
    }
  }

  // --------------------------------------------------------------- //
  // 任务操作（暂停/恢复 · 执行一次 · 编辑 · 删除）
  // --------------------------------------------------------------- //
  async function toggleJobStatus(job: ScheduledJob) {
    setActionLoading(true);
    try {
      const paused = job.status === "active";
      await changeScheduleJobStatus(job.id, paused ? "paused" : "active");
      // 结果提醒走 antd message（设计 04 §3.6，2026-09-25 用户口径）。
      message.success(
        paused
          ? `任务「${job.name}」已暂停：到达触发时间也不会自动执行，可随时点「恢复」。`
          : `任务「${job.name}」已恢复：将按触发器自动执行。`
      );
      await loadJobs();
      await loadStatus();
    } catch {
      message.error(`任务「${job.name}」状态切换失败。`);
    } finally {
      setActionLoading(false);
    }
  }

  async function runJobOnce(job: ScheduledJob) {
    setActionLoading(true);
    try {
      await runScheduleJobOnce(job.id);
      message.success(`任务「${job.name}」已入队执行，记录见「任务日志」页签。`);
    } catch (err) {
      // 后端回的结构化原因（如参数校验 / 归属失败）要带给用户，不吃成笼统「失败」。
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || `任务「${job.name}」执行失败。`);
    } finally {
      setActionLoading(false);
    }
  }

  /**
   * 「执行一次」入口（设计 22 §5，2026-10-06 用户口径）。
   *
   * * 任务**有可编辑参数** ⇒ 弹参数弹窗，允许改本次参数后执行；
   * * 任务**无参数** ⇒ `Popconfirm` 二次确认后直接执行。
   *
   * 判据是 `params_editable`（后端注册表下发，前端不硬编码）——
   * 与「编辑弹窗能改哪些参数」是**同一个字段**，不会分叉。
   */
  function requestRunJob(job: ScheduledJob) {
    if ((job.params_editable ?? []).length > 0) {
      setRunJobTarget(job);
    }
    // 无参数任务：点按钮不做事，交给外层 `Popconfirm` 的「确定」触发 `runJobOnce`。
  }

  async function submitRunJob(params: Record<string, unknown>) {
    const job = runJobTarget;
    if (!job) return;
    setActionLoading(true);
    try {
      await runScheduleJobOnce(job.id, params);
      message.success(`任务「${job.name}」已按本次参数入队执行，记录见「任务日志」页签。`);
      setRunJobTarget(null);
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || `任务「${job.name}」执行失败。`);
    } finally {
      setActionLoading(false);
    }
  }

  async function removeJob(job: ScheduledJob) {
    setActionLoading(true);
    try {
      await deleteScheduleJob(job.id);
      message.success(`任务「${job.name}」已删除（任务日志保留）。`);
      await loadJobs();
      await loadStatus();
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(String(detail ?? `任务「${job.name}」删除失败。`));
    } finally {
      setActionLoading(false);
    }
  }

  // --------------------------------------------------------------- //
  // 列定义
  // --------------------------------------------------------------- //
  const jobColumns: ColumnsType<ScheduledJob> = [
    {
      title: "任务名称",
      dataIndex: "name",
      key: "name",
      width: 180,
      render: (name: string) => <Text strong>{name}</Text>,
    },
    {
      //: 2026-10-07 用户口径「定时任务新增表字段是否为内置任务，列表新增展示，
      //: 内容任务不允许删除，前后端添加校验逻辑」—— 内置与否是**列表上要看的一眼**
      //: （它决定这行能不能删），所以单开一列而不是挂在任务名下的 Tag。
      title: "任务属性",
      dataIndex: "is_builtin",
      key: "is_builtin",
      width: 110,
      render: (isBuiltin: boolean) =>
        isBuiltin ? (
          <Tag color="blue">系统内置</Tag>
        ) : (
          <Tag>自定义</Tag>
        ),
    },
    // 用户口径（2026-09-24）：「任务分组先去掉」⇒「分组」列整条移除
    {
      title: "调用目标",
      dataIndex: "job_key",
      key: "job_key",
      width: 220,
      // R18 后续口径：**两行**展示 —— 第一行中文名、第二行代码键。
      // 中文名取自 `GET /schedule/targets`；取不到就只显示键（缺值不猜）。
      render: (key: string) => (
        <Space direction="vertical" size={0}>
          <Text>{targetNames[key] ?? "-"}</Text>
          <Text code type="secondary" style={{ fontSize: 12 }}>
            {key}
          </Text>
        </Space>
      ),
    },
    {
      title: "任务类型",
      dataIndex: "trigger_type",
      key: "trigger_type",
      width: 120,
      // R16 用户口径：类型**不用 Tag**，就一行纯文本（Tag 把三档窄信息做成了彩块，噪声大于信息）
      render: (type: string) => <Text>{triggerTypeLabel(type)}</Text>,
    },
    {
      title: "触发器",
      key: "trigger",
      width: 170,
      render: (_: unknown, record: ScheduledJob) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {triggerSummary(record)}
        </Text>
      ),
    },
    {
      title: "错过策略",
      dataIndex: "misfire_policy",
      key: "misfire_policy",
      width: 110,
      render: (policy: string) => (
        <Tooltip title={policy}>{misfirePolicyLabel(policy)}</Tooltip>
      ),
    },
    {
      title: "并发",
      dataIndex: "concurrent",
      key: "concurrent",
      width: 100,
      render: (mode: string) => (mode === "allow" ? "允许" : "禁止"),
    },
    {
      title: "成功 / 失败",
      key: "counts",
      width: 110,
      render: (_: unknown, record: ScheduledJob) => (
        <Text>
          <Text type="success">{record.run_count}</Text>
          {" / "}
          <Text type="danger">{record.fail_count}</Text>
        </Text>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (status: string) => {
        const tag = jobStatusTag(status);
        return <Tag color={tag.color}>{tag.label}</Tag>;
      },
    },
    {
      title: "操作",
      key: "actions",
      width: 210,
      render: (_: unknown, record: ScheduledJob) => (
        <Space>
          {/* 操作列按类型/状态区分：办结（completed，只有 at 会到）不给暂停/恢复 */}
          {record.status !== "completed" && (
            <Button
              size="small"
              icon={record.status === "active" ? <PauseCircleOutlined /> : <PlayCircleOutlined />}
              disabled={actionLoading}
              onClick={() => toggleJobStatus(record)}
            >
              {record.status === "active" ? "暂停" : "恢复"}
            </Button>
          )}
          {/* 设计 22 §5：「执行一次」——有可编辑参数 ⇒ 参数弹窗；无参数 ⇒ 二次确认。
              用 `Popconfirm` 包住整个按钮：有参数的实例点「确定」只是打开弹窗
              （`requestRunJob` 走弹窗分支），无参数的实例点「确定」直接跑。 */}
          <Popconfirm
            title={`确定立即执行任务「${record.name}」？`}
            okText="执行"
            cancelText="取消"
            disabled={actionLoading || (record.params_editable ?? []).length > 0}
            onConfirm={() => runJobOnce(record)}
          >
            <Button
              size="small"
              icon={<SyncOutlined />}
              disabled={actionLoading}
              onClick={() => requestRunJob(record)}
            >
              执行一次
            </Button>
          </Popconfirm>
          <Button
            size="small"
            icon={<EditOutlined />}
            disabled={actionLoading}
            onClick={() => {
              setEditingJob(record);
              setJobFormOpen(true);
            }}
          >
            编辑
          </Button>
          <Popconfirm
            title={`删除任务「${record.name}」？`}
            description="任务日志会保留，可在日志页签清理"
            onConfirm={() => removeJob(record)}
            okButtonProps={{ danger: true }}
            // 内置任务不给弹确认（点都点不动，弹了也没用）
            disabled={record.is_builtin}
          >
            {/*
             * 2026-10-07 用户口径「内容任务不允许删除，前后端添加校验逻辑」：
             * 内置任务（`is_builtin`）删除按钮**置灰**，Tooltip 说明出路（改调度 / 暂停）——
             * 后端 `DELETE /schedule/jobs/{id}` 同步拒 400 `JOB_BUILTIN_PROTECTED`，
             * 前端这道只是**别让用户白点**，真正的门禁在后端。
             */}
            <Tooltip
              title={record.is_builtin ? "系统内置任务不可删除（可暂停或改调度）" : ""}
            >
              <Button
                size="small"
                danger
                icon={<DeleteOutlined />}
                disabled={actionLoading || record.is_builtin}
              >
                删除
              </Button>
            </Tooltip>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const logColumns: ColumnsType<ScheduledJobLog> = [
    {
      // 日志 ID（2026-09-27 用户口径「任务日志添加 ID 列」）：它是**排障的锚点** ——
      // 用户报障时念一个数字即可精确定位到某一次执行（时间戳会撞、任务名会重复）。
      // 取 `ScheduledJobLog.id`（主键，单调递增 = 落库顺序），**不是**业务任务号
      // （那是 `task_id`，账号卡路径上为空，见设计 15 §15）。
      title: "ID",
      dataIndex: "id",
      key: "id",
      width: 90,
      render: (id: number) => <Text>{id}</Text>,
    },
    {
      title: "任务名称",
      dataIndex: "job_name",
      key: "job_name",
      width: 220,
      render: (name: string) => <Text strong>{name}</Text>,
    },
    {
      title: "任务类型",
      key: "job_trigger_type",
      width: 120,
      // R16：纯文本，不用 Tag（与任务列表同口径）
      render: (_: unknown, record: ScheduledJobLog) =>
        record.job_trigger_type ? (
          <Text>{triggerTypeLabel(record.job_trigger_type)}</Text>
        ) : (
          <Text type="secondary">-</Text>
        ),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 130,
      render: (status: string) => {
        const tag = logStatusTag(status);
        return <Tag color={tag.color}>{tag.label}</Tag>;
      },
    },
    {
      title: "触发方式",
      dataIndex: "trigger_type",
      key: "trigger_type",
      width: 110,
      render: (trigger: string) => logTriggerLabel(trigger),
    },
    {
      title: "开始执行时间",
      dataIndex: "started_at",
      key: "started_at",
      width: 180,
      render: (value: string) => formatShanghaiTime(value),
    },
    {
      // R13 用户口径：一列合并展示「任务进度 + 耗时（成功）/ 异常（失败）」；
      // 其余细节（触发器 / 错过策略 / 并发 / 消息 / 结果全文）全在详情弹窗。
      //
      // ⚠️ **进度条的渲染不在本文件**（DEF-044）：比例已知 / 未知两态、以及「未知」的
      // 呈现（**不定量滑动条**，不是满格条）统一由 `JobLogProgress` 承担，
      // **详情弹窗用的是同一个组件**。
      //
      // 为什么必须共用（2026-09-28 用户报障，设计 13 §43）：旧实现这里在「在跑且
      // `progress` 为 0」时渲染 `percent={100}` 的满格条（与「100% 完成」视觉同形），
      // 而详情弹窗读的是同一个 `log.progress`（0）⇒ 用户看到「列表 100% / 详情 0%」
      // 两个相反读数；取消后本列又回落成真实值 0%。「未知」与「0%」都不是 100%，
      // 唯一不会说谎的载体是**没有固定填充比例**的滑动条。
      title: "任务进度",
      key: "progress",
      width: 240,
      render: (_: unknown, record: ScheduledJobLog) => (
        <Space direction="vertical" size={2} style={{ width: "100%" }}>
          <JobLogProgress log={record} />
          {record.status === "failed" && record.exception_info ? (
            /* 2026-09-27 用户口径「任务日志的进度列展示异常信息太长了，固定一下长度，
               使用 tip 展示全部」：原实现只有外层 `Tooltip` + `Text ellipsis`，但
               `Table` 未设 `tableLayout`（默认 auto）⇒ 列宽 240 只是**建议值**，长异常
               文本会把整列撑开，`ellipsis` 的 `max-width: 100%` 随之被撑大 —— **实际
               从未截断**。故给**固定上限** `maxWidth`（不依赖 td 宽度），并改用 antd
               `ellipsis={{ tooltip }}`：只在**真被截断时**才出 tip（与账号卡同口径，
               没截断就不平白多一个悬停气泡）。 */
            <Text
              type="danger"
              style={{ fontSize: 12, maxWidth: 200, display: "inline-block" }}
              ellipsis={{ tooltip: record.exception_info }}
            >
              {record.exception_info}
            </Text>
          ) : (
            <Text type="secondary" style={{ fontSize: 12 }}>
              耗时 {formatDuration(record.duration_ms)}
            </Text>
          )}
        </Space>
      ),
    },
    {
      title: "操作",
      key: "actions",
      width: 170,
      render: (_: unknown, record: ScheduledJobLog) => (
        <Space size={4}>
          <Button size="small" onClick={() => setDetailLog(record)}>
            详情
          </Button>
          {/* 取消（2026-09-27 用户口径「任务日志列表操作列新增取消任务中断任务执行功能
              按钮和逻辑」）：**只在日志仍在 `running` 且带业务任务号时**渲染 ——
              终态行点取消是「假动作」（后端原样返回），缺 `task_id` 的行（cron 系统作业 /
              存量日志）没有可取消的业务任务行。二次确认后才真发请求。 */}
          {record.status === "running" && record.task_id ? (
            <Popconfirm
              title="取消这次执行？"
              description="已在执行的部分保留，剩余部分不再继续。"
              okText="取消任务"
              cancelText="再想想"
              onConfirm={() => cancelLogTask(record)}
            >
              <Button size="small" danger>
                取消
              </Button>
            </Popconfirm>
          ) : null}
        </Space>
      ),
    },
  ];

  const jobKeyOptions = [
    ...new Map(jobs.map((job) => [job.job_key, job])).values(),
  ].map((job) => ({ value: job.job_key, label: job.name }));

  return (
    <div>
      {/* ⚠️ 顶部标题区已**整块移除**（2026-09-27 用户口径：「任务中心的顶部：Automation /
          任务中心 / 调度任务的统一管理台：任务列表 + 每次执行的日志 —— **全部移除**，
          刷新按钮放后台调度卡右上角」）。进入页面即是「后台调度」卡：少一层标题占位，
          首屏直接是数据。刷新按钮随之挪到那张卡的右上角（`Card.extra`）。 */}

      {/* 用户口径（2026-09-24）：页面不放 inline 消息条，结果一律走底部弹窗 */}

      {scheduleStatus && (
        <Card
          title={
            <Space>
              <Text>后台调度</Text>
              <Tag color={scheduleStatus.enabled && scheduleStatus.running ? "success" : "default"}>
                {scheduleStatus.enabled
                  ? scheduleStatus.running
                    ? "运行中"
                    : "已启用"
                  : "未启用"}
              </Tag>
            </Space>
          }
          /* 刷新按钮（2026-09-27 用户口径：从顶部标题区挪到本卡右上角）。 */
          extra={
            <Button icon={<ReloadOutlined />} onClick={refreshAll} loading={jobsLoading}>
              刷新
            </Button>
          }
          style={{ ...cardStyle, marginBottom: 24 }}
        >
          <Row gutter={[16, 16]}>
            <Col xs={12} sm={6}>
              <Statistic
                title="运行状态"
                value={scheduleStatus.running ? "Running" : "Stopped"}
                prefix={<DashboardOutlined />}
              />
            </Col>
            <Col xs={12} sm={6}>
              <Statistic title="时区" value={scheduleStatus.timezone} prefix={<ClockCircleOutlined />} />
            </Col>
            <Col xs={12} sm={6}>
              <Statistic
                title="注册任务"
                value={scheduleStatus.jobs.length}
                suffix={`/ ${scheduleStatus.stats.active} 活跃`}
                prefix={<SyncOutlined />}
              />
            </Col>
            <Col xs={12} sm={6}>
              <Statistic
                title="任务行"
                value={scheduleStatus.stats.total}
                suffix={`暂停 ${scheduleStatus.stats.paused} · 办结 ${scheduleStatus.stats.completed}`}
                prefix={<CloseCircleOutlined />}
              />
            </Col>
          </Row>
          {/* 不逐条罗列调度器里的 job_N（这里只做总览，执行明细见任务日志）；
              未运行时给一句可操作说明。 */}
          {!scheduleStatus.running && (
            <div style={{ marginTop: 12 }}>
              <Text type="secondary">
                调度器未运行：本地默认关闭，开启需设置 SCHEDULER_ENABLED=true；未启用时任务配置仍可维护，只是不会自动执行。
              </Text>
            </div>
          )}
        </Card>
      )}

      <Card style={cardStyle}>
        <Tabs
          activeKey={activeTab}
          onChange={setActiveTab}
          items={[
            {
              key: "jobs",
              label: `任务列表${jobs.length ? ` (${jobs.length})` : ""}`,
              children: (
                <Space direction="vertical" size="middle" style={{ width: "100%" }}>
                  <Button
                    type="primary"
                    icon={<PlusOutlined />}
                    onClick={() => {
                      setEditingJob(null);
                      setJobFormOpen(true);
                    }}
                  >
                    新建任务
                  </Button>
                  {jobsLoading ? (
                    <div style={{ textAlign: "center", padding: 48 }}>
                      <Spin tip="正在加载任务..." />
                    </div>
                  ) : jobs.length === 0 ? (
                    <Empty description="还没有任务" />
                  ) : (
                    <Table<ScheduledJob>
                      columns={jobColumns}
                      dataSource={jobs}
                      rowKey="id"
                      size="small"
                      pagination={false}
                    />
                  )}
                </Space>
              ),
            },
            {
              key: "logs",
              label: "任务日志",
              children: (
                <Space direction="vertical" size="middle" style={{ width: "100%" }}>
                  <Space wrap>
                    <Segmented
                      value={logStatus ?? ""}
                      onChange={(value) => setLogStatus(value || undefined)}
                      options={[
                        { value: "", label: "全部" },
                        { value: "success", label: "成功" },
                        { value: "failed", label: "失败" },
                        { value: "skipped", label: "已跳过" },
                        { value: "running", label: "执行中" },
                      ]}
                    />
                    <Select
                      style={{ width: 160 }}
                      value={logTriggerType ?? ""}
                      onChange={(value) => setLogTriggerType(value || undefined)}
                      options={[
                        { value: "", label: "全部任务" },
                        { value: "cron", label: triggerTypeFullLabel("cron") },
                        { value: "at", label: triggerTypeFullLabel("at") },
                      ]}
                    />
                    <Select
                      style={{ width: 200 }}
                      value={logJobKey ?? ""}
                      onChange={(value) => setLogJobKey(value || undefined)}
                      options={[
                        { value: "", label: "全部任务" },
                        ...jobKeyOptions.map((option) => ({
                          value: option.value,
                          label: option.label,
                        })),
                      ]}
                    />
                    <RangePicker
                      showTime
                      onChange={(_: unknown, dateStrings: [string, string] | null) =>
                        setLogRange(dateStrings ? [dateStrings[0], dateStrings[1]] : null)
                      }
                    />
                    <Button
                      danger
                      icon={<DeleteOutlined />}
                      disabled={selectedLogIds.length === 0}
                      onClick={async () => {
                        const result = await deleteScheduleLogs(selectedLogIds);
                        message.success(`已删除 ${result.deleted} 条日志。`);
                        await loadLogs();
                      }}
                    >
                      批量删除{selectedLogIds.length ? ` (${selectedLogIds.length})` : ""}
                    </Button>
                    <Popconfirm
                      title="清空当前筛选范围内的任务日志？"
                      description="不可恢复。任务行不受影响。"
                      onConfirm={async () => {
                        const result = await cleanScheduleLogs();
                        message.success(`已清空 ${result.deleted} 条日志。`);
                        await loadLogs();
                      }}
                      okButtonProps={{ danger: true }}
                    >
                      <Button danger icon={<DeleteOutlined />}>
                        清空
                      </Button>
                    </Popconfirm>
                  </Space>
                  {logsLoading ? (
                    <div style={{ textAlign: "center", padding: 48 }}>
                      <Spin tip="正在加载日志..." />
                    </div>
                  ) : logs.length === 0 ? (
                    <Empty description="还没有执行记录，任务首次触发后这里会有" />
                  ) : (
                    <Table<ScheduledJobLog>
                      columns={logColumns}
                      dataSource={logs}
                      rowKey="id"
                      size="small"
                      pagination={{ pageSize: 20, size: "small" }}
                      rowSelection={{
                        selectedRowKeys: selectedLogIds,
                        onChange: (keys) => setSelectedLogIds(keys as number[]),
                      }}
                    />
                  )}
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <JobFormModal
        open={jobFormOpen}
        job={editingJob}
        onCancel={() => setJobFormOpen(false)}
        onSaved={(saved) => {
          setJobFormOpen(false);
          message.success(`任务「${saved.name}」已保存。`);
          void loadJobs();
          void loadStatus();
        }}
      />

      {/* 设计 22 §5：「执行一次」参数弹窗（只对有可编辑参数的任务弹）。
          弹窗里改的参数**只对这一次执行生效**，不写回任务行配置。 */}
      <RunJobModal
        open={runJobTarget !== null}
        job={runJobTarget}
        submitting={actionLoading}
        onCancel={() => setRunJobTarget(null)}
        onSubmit={(params) => void submitRunJob(params)}
      />

      {/* 日志详情：执行时的配置快照 + 日志明细 */}
      <JobLogDetailModal
        open={detailLog !== null}
        log={detailLog}
        // 调用目标中文名与列表列共用同一份 /schedule/targets（不另写映射表，DEF-044）
        targetNames={targetNames}
        // 账号名与任务记录页共用 lib 的 `accountNameMap`（不另写映射，DEF-044）
        accountNames={accountNames}
        onCancel={() => setDetailLog(null)}
      />
    </div>
  );
}
