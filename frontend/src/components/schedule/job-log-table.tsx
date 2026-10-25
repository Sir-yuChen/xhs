/**
 * 任务执行日志的**公共表格组件**（设计 69 §4.3）。
 *
 * ## 抽它的理由（用户口径「组件可以共用的提取成公共组件」）
 *
 * 「执行日志」出现在**三处**：系统管理任务中心、原本的用户任务中心、账号卡里的
 * 同步任务区块。三处此前各写一份列定义，结果是：
 * - 同一个 `status` 在一处显示「成功」、另一处显示 `success`；
 * - 时间格式一处 `toLocaleString()`、一处原样打印 ISO 串（带 `T`）；
 * - 进度列一处有、一处没有。
 *
 * ⇒ 列定义与格式化收进本组件，页面只传「数据 + 要显示哪些列 + 行点击做什么」。
 *
 * ## 为什么不干脆做成页面
 *
 * 三处的**外层**完全不同：系统管理页是整页表格（带分页 + 筛选栏），
 * 账号卡是卡片里的一个小区块（5 行、无分页、无筛选）。
 * 抽「表格 + 列」而不抽「页面」，才能两边都用。
 */

import { Empty, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";

import type { ScheduledJobLog } from "../../types";
import { formatTime } from "./task-columns";

const { Text } = Typography;

/** 日志状态 → 颜色（唯一实现）。 */
export function logStatusColor(status: string): string {
  if (status === "success") return "green";
  if (status === "failed") return "red";
  if (status === "running") return "blue";
  if (status === "skipped") return "default";
  return "default";
}

/** 日志状态 → 中文（后端下发 success/failed/running/skipped）。 */
export function logStatusLabel(status: string): string {
  if (status === "success") return "成功";
  if (status === "failed") return "失败";
  if (status === "running") return "执行中";
  if (status === "skipped") return "跳过";
  return status;
}

export interface JobLogTableOptions {
  /** 是否显示「目标用户」列（fan-out 后一次执行跑的是谁的数据）。 */
  showExecutedFor?: boolean;
  /** 是否显示「工作区」列。 */
  showWorkspace?: boolean;
  /** 是否显示「任务键」列（页面已按任务筛过就不用重复显示）。 */
  showJobKey?: boolean;
  /** 是否显示进度列。 */
  showProgress?: boolean;
}

/**
 * 日志表的列（按参数裁剪）。
 *
 * ⚠️ `executed_for_user_id` 与 `user_id` **不是一回事**（设计 69 §4.1）：
 * 前者 = 这次执行跑的是谁的数据；后者 = 任务最后由谁修改。
 * 页面上的「目标用户」列读前者，缺值时显示 `-`（系统级清理任务没有执行主体）。
 */
export function buildLogColumns(options: JobLogTableOptions = {}): ColumnsType<ScheduledJobLog> {
  const {
    showExecutedFor = true,
    showWorkspace = false,
    showJobKey = false,
    showProgress = false,
  } = options;

  const columns: ColumnsType<ScheduledJobLog> = [
    {
      title: "开始时间",
      dataIndex: "started_at",
      width: 170,
      render: (value?: string | null) => formatTime(value),
    },
  ];

  if (showJobKey) {
    columns.push({ title: "任务", dataIndex: "job_name", width: 180, ellipsis: true });
  }

  if (showWorkspace) {
    columns.push({
      title: "工作区",
      dataIndex: "workspace",
      width: 90,
      render: (value?: string | null) => <Tag>{value === "system" ? "系统" : value || "-"}</Tag>,
    });
  }

  columns.push(
    {
      title: "状态",
      dataIndex: "status",
      width: 90,
      render: (value: string) => <Tag color={logStatusColor(value)}>{logStatusLabel(value)}</Tag>,
    },
    { title: "触发", dataIndex: "trigger_type", width: 80 },
  );

  if (showExecutedFor) {
    columns.push({
      title: "目标用户",
      dataIndex: "executed_for_user_id",
      width: 100,
      render: (value?: number | null) => (value ?? "-"),
    });
  }

  if (showProgress) {
    columns.push({
      title: "进度",
      dataIndex: "progress",
      width: 80,
      align: "right",
      render: (value?: number | null) => (value == null ? "-" : `${value}%`),
    });
  }

  columns.push(
    {
      title: "耗时",
      dataIndex: "duration_ms",
      width: 90,
      align: "right",
      render: (value?: number | null) =>
        value == null ? "-" : value < 1000 ? `${value}ms` : `${(value / 1000).toFixed(1)}s`,
    },
    {
      title: "结果",
      dataIndex: "message",
      ellipsis: true,
      render: (value?: string | null) => <Text type="secondary">{value || "-"}</Text>,
    },
  );

  return columns;
}

export interface JobLogTableProps extends JobLogTableOptions {
  data: ScheduledJobLog[];
  loading?: boolean;
  /** 行点击（打开详情弹窗由页面决定；组件不 import 弹窗）。 */
  onRowClick?: (log: ScheduledJobLog) => void;
  /** 是否显示分页（整页表格要，卡片内小区块不要）。 */
  pagination?: false | import("antd/es/table").TablePaginationConfig;
  /** 表格高度（撑满布局用 `"100%"`；卡片内不传）。 */
  scrollY?: number | string;
  /** 空态文案（缺省「暂无执行记录」）。 */
  emptyText?: string;
  rowKey?: (row: ScheduledJobLog) => string;
}

/**
 * 执行日志表格（公共组件）。
 *
 * 页面侧只负责：取数、传 `data`、决定显示哪些列、行点击行为。
 */
export function JobLogTable({
  data,
  loading = false,
  onRowClick,
  pagination = false,
  scrollY,
  emptyText = "暂无执行记录",
  rowKey,
  ...columnOptions
}: JobLogTableProps) {
  return (
    <Table<ScheduledJobLog>
      size="small"
      rowKey={rowKey ?? ((row) => String(row.id))}
      loading={loading}
      columns={buildLogColumns(columnOptions)}
      dataSource={data}
      pagination={pagination}
      scroll={scrollY ? { x: 1000, y: scrollY } : { x: 1000 }}
      onRow={(row) => ({
        onClick: onRowClick ? () => onRowClick(row) : undefined,
        style: onRowClick ? { cursor: "pointer" } : undefined,
      })}
      locale={{
        emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyText} />,
      }}
    />
  );
}
