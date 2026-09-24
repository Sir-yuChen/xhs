/**
 * 任务中心（设计 15 §9.1，2026-09-24 验收修正）——两个页签：
 *
 * 1. **任务列表**：全部 `scheduled_jobs` 行（手动执行 / Cron 周期 / 定时一次
 *    合并为单一列表，列一致；操作列按任务类型与状态区分，completed 的不给暂停/恢复）；
 * 2. **任务日志**：每次调度执行一行（成功 / 失败 / 跳过留痕）。
 *
 * 验收口径（用户 2026-09-24，多轮）：
 * ① 任务页不放说明文案；调度补跑按钮与手动执行记录页签按口径移除
 *    （补跑由调度器自动进行；手动执行的明细在各业务页自行呈现）；
 * ② at 与周期不拆两个页签，合并为单一任务列表；
 * ③ 任务类型三档（R14）：手动执行 / Cron 周期 / 定时一次 ——「间隔周期」并入 Cron；
 * ④ 日志表只展示 6 列（R13）：任务名称 / 任务类型 / 状态 / 触发方式 /
 *    开始执行时间 / 任务进度（进度条 + 耗时或异常），其余进详情弹窗；
 * ⑤ 页面**不留 inline 消息条**（2026-09-24 口径「所有的提示都改为弹窗提示除非
 *    界面没有内容」）：一切结果提示走统一弹窗；
 * ⑥ **任务分组整条移除**（2026-09-24 口径「任务分组先去掉」）：列表不再有「分组」列；
 * ⑦ 调用目标列**两行**（中文名 + 代码键）、任务名下方不挂「内置」Tag。
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
  Button,
  Card,
  Col,
  DatePicker,
  Empty,
  Modal,
  Popconfirm,
  Progress,
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

import { PageHeader } from "../../components/layout/app-shell";
import { JobFormModal } from "../../components/schedule/job-form-modal";
import { JobLogDetailModal } from "../../components/schedule/job-log-detail-modal";
import {
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
import { formatDuration, formatShanghaiTime } from "../../lib/time";
import type {
  PlatformAccount,
  ScheduleStatus,
  ScheduledJob,
  ScheduledJobLog,
} from "../../types";

const { Text } = Typography;
const { RangePicker } = DatePicker;

const cardStyle = { background: "#1f1f1f", borderColor: "#303030" };

export function TaskCenterPage() {
  const [activeTab, setActiveTab] = useState("jobs");
  // 用户口径（2026-09-24）：页面**不放 inline 消息条**，一切结果提示走**弹窗**。
  // `notice` 取代原先的 `message`（Alert）+ `jobNotice` 两套，统一成一个弹窗。
  const [notice, setNotice] = useState<{
    kind: "success" | "error" | "info";
    title: string;
    content: string;
  } | null>(null);

  // 任务列表（scheduled_jobs：at / cron / interval 全量）
  const [jobs, setJobs] = useState<ScheduledJob[]>([]);
  const [jobsLoading, setJobsLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState(false);

  // 任务日志
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
  // 日志详情弹窗（执行时配置快照 + 日志明细）
  const [detailLog, setDetailLog] = useState<ScheduledJobLog | null>(null);

  /** 结果提示小工具：一次调用一个弹窗（替代原先散落的 setMessage） */
  const notify = useCallback(
    (kind: "success" | "error" | "info", title: string, content: string) =>
      setNotice({ kind, title, content }),
    []
  );

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
      notify("error", "加载失败", "任务列表加载失败，请稍后重试。");
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
      notify("error", "加载失败", "任务日志加载失败，请稍后重试。");
    } finally {
      setLogsLoading(false);
    }
  }, [logJobKey, logTriggerType, logStatus, logRange]);

  useEffect(() => {
    void loadStatus();
    void loadJobs();
    void loadTargets();
  }, [loadStatus, loadJobs, loadTargets]);

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

  // --------------------------------------------------------------- //
  // 任务操作（暂停/恢复 · 执行一次 · 编辑 · 删除）
  // --------------------------------------------------------------- //
  async function toggleJobStatus(job: ScheduledJob) {
    setActionLoading(true);
    try {
      const paused = job.status === "active";
      await changeScheduleJobStatus(job.id, paused ? "paused" : "active");
      // 验收口径（用户 2026-09-24）：启停结果用**弹窗**提醒，不用页顶 inline 提示
      setNotice({
        kind: "success",
        title: paused ? "任务已暂停" : "任务已恢复",
        content: paused
          ? `任务「${job.name}」已暂停：到达触发时间也不会自动执行，可随时点「恢复」。`
          : `任务「${job.name}」已恢复：将按触发器自动执行。`,
      });
      await loadJobs();
      await loadStatus();
    } catch {
      notify("error", "操作失败", `任务「${job.name}」状态切换失败。`);
    } finally {
      setActionLoading(false);
    }
  }

  async function runJobOnce(job: ScheduledJob) {
    setActionLoading(true);
    try {
      await runScheduleJobOnce(job.id);
      notify("success", "已入队执行", `任务「${job.name}」已入队执行，记录见「任务日志」页签。`);
    } catch {
      notify("error", "执行失败", `任务「${job.name}」执行失败。`);
    } finally {
      setActionLoading(false);
    }
  }

  async function removeJob(job: ScheduledJob) {
    setActionLoading(true);
    try {
      await deleteScheduleJob(job.id);
      notify("success", "任务已删除", `任务「${job.name}」已删除（任务日志保留）。`);
      await loadJobs();
      await loadStatus();
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      notify("error", "删除失败", String(detail ?? `任务「${job.name}」删除失败。`));
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
      // 用户口径：任务名下方不再挂「内置」Tag（任务一律可编辑可删除，标不标都一样）
      render: (name: string) => <Text strong>{name}</Text>,
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
          <Button
            size="small"
            icon={<SyncOutlined />}
            disabled={actionLoading}
            onClick={() => runJobOnce(record)}
          >
            执行一次
          </Button>
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
            description="任务日志会保留（任务名快照仍在），可随后在日志页签清理。"
            onConfirm={() => removeJob(record)}
            okButtonProps={{ danger: true }}
          >
            {/* 用户口径：删除按钮**放开**（所有任务一律可删，内置不再是特权） */}
            <Button
              size="small"
              danger
              icon={<DeleteOutlined />}
              disabled={actionLoading}
            >
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const logColumns: ColumnsType<ScheduledJobLog> = [
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
      title: "任务进度",
      key: "progress",
      width: 240,
      render: (_: unknown, record: ScheduledJobLog) => (
        <Space direction="vertical" size={2} style={{ width: "100%" }}>
          <Progress
            percent={Math.max(0, Math.min(100, record.progress ?? 0))}
            size="small"
            status={
              record.status === "failed"
                ? "exception"
                : record.status === "running"
                  ? "active"
                  : "normal"
            }
          />
          {record.status === "failed" && record.exception_info ? (
            <Tooltip title={record.exception_info}>
              <Text type="danger" style={{ fontSize: 12 }} ellipsis>
                {record.exception_info}
              </Text>
            </Tooltip>
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
      width: 90,
      render: (_: unknown, record: ScheduledJobLog) => (
        <Button size="small" onClick={() => setDetailLog(record)}>
          详情
        </Button>
      ),
    },
  ];

  const jobKeyOptions = [
    ...new Map(jobs.map((job) => [job.job_key, job])).values(),
  ].map((job) => ({ value: job.job_key, label: job.name }));

  return (
    <div>
      <PageHeader
        eyebrow="Automation"
        title="任务中心"
        description="调度任务的统一管理台：任务列表 + 每次执行的日志。"
        action={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={refreshAll} loading={jobsLoading}>
              刷新
            </Button>
          </Space>
        }
      />

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
                    <Empty description="还没有任务。点「新建任务」创建一个：定时一次、Cron 周期或间隔周期。" />
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
                        notify("success", "已删除", `已删除 ${result.deleted} 条日志。`);
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
                        notify("success", "已清空", `已清空 ${result.deleted} 条日志。`);
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
                    <Empty description="还没有执行记录。任务首次触发后，这里会有每次执行的成败与耗时。" />
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
          notify("success", "已保存", `任务「${saved.name}」已保存。`);
          void loadJobs();
          void loadStatus();
        }}
      />

      {/* 统一结果弹窗（用户口径 2026-09-24：一切提示走弹窗，页面不留消息条） */}
      <Modal
        open={notice !== null}
        title={notice?.title}
        onOk={() => setNotice(null)}
        onCancel={() => setNotice(null)}
        okText="知道了"
        width={460}
        footer={(_, { OkBtn }) => <OkBtn />}
      >
        <Text type={notice?.kind === "error" ? "danger" : undefined}>{notice?.content}</Text>
      </Modal>

      {/* 日志详情：执行时的配置快照 + 日志明细 */}
      <JobLogDetailModal
        open={detailLog !== null}
        log={detailLog}
        onCancel={() => setDetailLog(null)}
      />
    </div>
  );
}
