/**
 * 任务列表的**列定义工厂**（设计 69 §4.3，2026-10-25 用户口径
 * 「前端将组件可以共用的提取成公共组件，不同参数和页面调用展示对应内容」）。
 *
 * ## 为什么做成工厂而不是组件
 *
 * 任务列表在不同页面要显示**不同的列**：
 *
 * | 页面 | 需要的列 |
 * |---|---|
 * | 系统管理 → 任务中心（系统任务页签） | ID / 任务 / 状态 / 触发 / Cron / 上次 / 下次 / 运行 / 失败 |
 * | 系统管理 → 任务中心（工作区任务页签） | 同上 + 工作区标识 |
 * | 系统管理 → 任务中心（执行日志页签） | 日志字段（另一份工厂） |
 * | （已下线）用户任务中心 | 原有 9 列：名称/属性/调用目标/类型/触发器/错过策略/并发/成功失败/状态/操作 |
 *
 * 若做成一个固定列的 `<TaskTable />`，就只能靠十几个 `showXxx` 布尔开关拼，
 * 那是同一份逻辑的 N 个分支。**工厂 + 参数**才是"不同参数展示对应内容"。
 *
 * ## 判据：什么该抽、什么不该抽
 *
 * - **该抽**：列的取数/渲染/宽度/对齐（`status` → 中文标签、`cron` → 等宽字体、
 *   时间 → 统一格式化）—— 这些一改就要改 N 处，且各页写一份必然漂移。
 * - **不抽**：页面特有的交互（某页有「立刻执行」按钮、另一页没有）——
 *   作为 `render` 回调由页面传入，而不是塞进工厂里判断"当前是哪一页"。
 */

import type { ColumnsType } from "antd/es/table";
import { Tag, Typography } from "antd";

import type { ScheduledJob } from "../../types";

const { Text } = Typography;

/** 任务状态 → 颜色（唯一实现：各页显示同一个状态不能是两种颜色）。 */
export function jobStatusColor(status: string): string {
  if (status === "active") return "green";
  if (status === "paused") return "orange";
  if (status === "completed") return "default";
  return "default";
}

/** 任务状态 → 中文（后端下发 `active` / `paused` / `completed`）。 */
export function jobStatusLabel(status: string): string {
  if (status === "active") return "运行中";
  if (status === "paused") return "已暂停";
  if (status === "completed") return "已完成";
  return status;
}

/** 工作区标识 → 中文（`system` 是公共管理区，其余是业务工作区）。 */
export function workspaceLabel(workspace?: string | null): string {
  if (!workspace) return "-";
  if (workspace === "system") return "系统";
  if (workspace === "xhs") return "小红书";
  return workspace;
}

/** 时间显示：`2026-10-25T19:39:04` → `2026-10-25 19:39:04`；缺值 `-`（不是空串）。 */
export function formatTime(value?: string | null): string {
  if (!value) return "-";
  return value.replace("T", " ").slice(0, 19);
}

/** Cron 显示：等宽字体 + 缺值 `-`（等宽是为了让 5 段对齐好扫读）。 */
export function CronCell({ value }: { value?: string | null }) {
  if (!value) return <Text type="secondary">-</Text>;
  return <Text style={{ fontFamily: "monospace", fontSize: 12 }}>{value}</Text>;
}

/** 状态 Tag（各页共用，保证同一个状态到处是同一个颜色 + 同一套文案）。 */
export function JobStatusTag({ value }: { value: string }) {
  return <Tag color={jobStatusColor(value)}>{jobStatusLabel(value)}</Tag>;
}

export interface TaskColumnOptions {
  /** 是否显示「工作区」列（系统管理总览要，单工作区页不要）。 */
  showWorkspace?: boolean;
  /** 是否显示「归属人」（设计 69 后 `user_id` = 最后修改人；不显示则省一列）。 */
  showOwner?: boolean;
  /** 是否显示触发器细节（错过策略 / 并发）。 */
  showTriggerDetail?: boolean;
  /** 操作列：由页面传入（不传则不出操作列）。 */
  renderActions?: (job: ScheduledJob) => React.ReactNode;
  /** 附加在末尾的列（页面特有时插在这里，工厂不做判断）。 */
  extraColumns?: ColumnsType<ScheduledJob>;
}

/**
 * 任务定义表的列（按参数裁剪）。
 *
 * ⚠️ 列的**宽度与顺序在此统一**，页面不要各自覆写 —— 覆写会让同一份列表在
 * 两个页面上列宽不同、扫读时对不齐（本仓已有先例：商品列表两处列宽漂移）。
 */
export function buildTaskColumns(options: TaskColumnOptions = {}): ColumnsType<ScheduledJob> {
  const { showWorkspace = false, showOwner = false, showTriggerDetail = false } = options;

  const columns: ColumnsType<ScheduledJob> = [{ title: "ID", dataIndex: "id", width: 70 }];

  if (showWorkspace) {
    columns.push({
      title: "工作区",
      dataIndex: "workspace",
      width: 90,
      render: (value?: string | null) => <Tag>{workspaceLabel(value)}</Tag>,
    });
  }

  columns.push(
    { title: "任务", dataIndex: "name", width: 180, ellipsis: true },
    { title: "任务键", dataIndex: "job_key", width: 220, ellipsis: true },
  );

  if (showOwner) {
    columns.push({
      title: "修改人",
      dataIndex: "user_id",
      width: 100,
      render: (value?: number | null) => (value ?? "-"),
    });
  }

  columns.push(
    {
      title: "状态",
      dataIndex: "status",
      width: 90,
      render: (value: string) => <JobStatusTag value={value} />,
    },
    { title: "触发", dataIndex: "trigger_type", width: 80 },
    {
      title: "Cron",
      dataIndex: "cron_expression",
      width: 140,
      render: (value?: string | null) => <CronCell value={value} />,
    },
  );

  if (showTriggerDetail) {
    columns.push(
      { title: "错过策略", dataIndex: "misfire_policy", width: 100 },
      { title: "并发", dataIndex: "concurrent", width: 90 },
    );
  }

  columns.push(
    {
      title: "上次运行",
      dataIndex: "last_run_at",
      width: 170,
      render: (value?: string | null) => formatTime(value),
    },
    {
      title: "下次运行",
      dataIndex: "next_run_at",
      width: 170,
      render: (value?: string | null) => formatTime(value),
    },
    {
      title: "运行",
      dataIndex: "run_count",
      width: 70,
      align: "right",
    },
    {
      title: "失败",
      dataIndex: "fail_count",
      width: 70,
      align: "right",
      render: (value?: number | null) => (
        <Text type={Number(value) > 0 ? "danger" : undefined}>{value ?? 0}</Text>
      ),
    },
  );

  if (options.renderActions) {
    const render = options.renderActions;
    columns.push({
      title: "操作",
      key: "actions",
      //: 2026-10-25 用户口径「所有操作列均不允许溢出页面」：
      //: 原 160px 放 4 个按钮必然裁切（截图里「日志」只剩半个）。
      //: 240px 放 3 个短按钮 + 「更多」，其余收敛进下拉。
      width: 240,
      fixed: "right",
      render: (_: unknown, job) => render(job),
    });
  }

  if (options.extraColumns?.length) {
    columns.push(...options.extraColumns);
  }

  return columns;
}
