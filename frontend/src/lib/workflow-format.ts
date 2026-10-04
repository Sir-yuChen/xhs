/**
 * 自动化工作流的**纯逻辑**（设计 19 / M5）。
 *
 * ## 为什么单独成文件
 *
 * 与 `lib/task-records.ts` / `lib/metrics-format.ts` 同规：页面只负责渲染，
 * 判据（状态映射、可操作性、时长、节点标签）下沉到这里，行为脚本可直接
 * 驱动真实函数断言，**不必渲染页面**。
 *
 * ## ⚠️ 词表的唯一真相源在后端
 *
 * 节点类型 / 状态取自 `backend/app/models/workflow.py`（`NODE_TYPES` /
 * `RUN_STATUSES` / `WF_STATUSES`），本文件**只做展示映射**。
 * **表外值原样显示、不造名**（DEF-057 同型）：用户看到 `foo_bar` 比看到
 * 「未知状态」有用 —— 后者会把「后端加了新值」伪装成「前端有 bug」。
 */

// ---------------------------------------------------------------------------
// 节点类型 → 中文标签（后端 `NODE_TYPES` 的展示层镜像）
// ---------------------------------------------------------------------------
const NODE_LABELS: Record<string, string> = {
  trigger: "触发器",
  topic: "选题",
  generate: "生成",
  review: "人工审核",
  schedule: "定时发布",
  publish: "发布",
  monitor: "监控回填",
};

/** 节点的中文标签；**表外值原样返回**（不造名，见文件头）。 */
export function nodeLabel(nodeType: string): string {
  return NODE_LABELS[nodeType] ?? nodeType;
}

/** 全部节点类型（给「添加节点」下拉用；与后端 `NODE_TYPES` 同序）。 */
export const NODE_TYPE_OPTIONS = [
  "trigger",
  "topic",
  "generate",
  "review",
  "schedule",
  "publish",
  "monitor",
] as const;

/** `trigger` 是链首、唯一、不可删（§3.1）—— 不允许再添加。 */
export function selectableNodeTypes(existing: string[]): string[] {
  return NODE_TYPE_OPTIONS.filter((t) => t !== "trigger" || !existing.includes("trigger"));
}

// ---------------------------------------------------------------------------
// 状态映射
// ---------------------------------------------------------------------------
const WF_STATUS_TEXT: Record<string, string> = {
  enabled: "已启用",
  disabled: "已停用",
};

const RUN_STATUS_TEXT: Record<string, string> = {
  pending: "待执行",
  running: "执行中",
  paused: "待审核",
  succeeded: "成功",
  failed: "失败",
  cancelled: "已取消",
};

export function workflowStatusText(status: string): string {
  return WF_STATUS_TEXT[status] ?? status;
}

export function runStatusText(status: string): string {
  return RUN_STATUS_TEXT[status] ?? status;
}

/** antd `Tag` 配色（纯展示，不影响判据）。 */
export function runStatusColor(status: string): string {
  switch (status) {
    case "succeeded":
      return "success";
    case "running":
      return "processing";
    case "failed":
      return "error";
    case "paused":
      return "warning";
    case "cancelled":
      return "default";
    default:
      return "default";
  }
}

// ---------------------------------------------------------------------------
// 判据
// ---------------------------------------------------------------------------
/** 只有**活跃**（`pending`/`running`/`paused`）的 run 可取消。 */
export function canCancelRun(status: string): boolean {
  return status === "pending" || status === "running" || status === "paused";
}

/**
 * `paused` = **卡在人工审核**，必须由人放行（§3.3 硬约束：默认人工、不自动放行）。
 * 这是「待我处理」告警的唯一判据 —— 页面用它决定是否高亮。
 */
export function needsHumanReview(status: string): boolean {
  return status === "paused";
}

/** 失败 / 已取消的 run 可重跑节点（成功的重跑没有意义，也不给入口）。 */
export function canRerunNode(runStatus: string): boolean {
  return runStatus === "failed" || runStatus === "cancelled";
}

/**
 * 触发器显示文案：`manual` / `cron` / `at` 三种（§7.2）。
 *
 * ⚠️ **`at`（一次性）执行后工作流仍是 `enabled`**（由调度侧保证只跑一次）
 * —— 所以不能靠 `status` 推断「已跑过」，这只是**展示**。
 */
export function triggerText(triggerType: string, cronExpression: string): string {
  if (triggerType === "cron") {
    return cronExpression ? `定时 ${cronExpression}` : "定时";
  }
  if (triggerType === "at") {
    return "一次性";
  }
  if (triggerType === "manual") {
    return "手动";
  }
  return triggerType;
}

// ---------------------------------------------------------------------------
// 时长 / 时间
// ---------------------------------------------------------------------------
/**
 * 耗时文本。
 *
 * ⚠️ **未结束 ⇒ 返回 `-`，不补 0**（缺值口径，项目硬约束⑤）：
 * 「还在跑」与「跑了 0 秒」是两件事。
 */
export function durationText(startedAt: string | null, finishedAt: string | null): string {
  if (!startedAt) {
    return "-";
  }
  if (!finishedAt) {
    return "-";
  }
  const start = new Date(startedAt).getTime();
  const end = new Date(finishedAt).getTime();
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) {
    return "-";
  }
  const seconds = Math.round((end - start) / 1000);
  if (seconds < 60) {
    return `${seconds} 秒`;
  }
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`;
}

// ---------------------------------------------------------------------------
// 节点链（只读展示）
// ---------------------------------------------------------------------------
export interface ChainNode {
  node_key: string;
  node_type: string;
  node_name: string;
  enabled: boolean;
}

/**
 * 把节点按 `seq` 排序成展示链。
 *
 * ⚠️ **同 `seq` 要有稳定次序**（`node_key` 兜底）：只按 `seq` 排时同序号项的
 * 顺序由后端返回顺序决定，刷新一次链就跳一次（与 `list_matches` 同一条纪律）。
 */
export function sortChain(
  nodes: { node_key: string; node_type: string; node_name: string; enabled: boolean; seq: number }[],
): ChainNode[] {
  return [...nodes]
    .sort((a, b) => (a.seq - b.seq) || a.node_key.localeCompare(b.node_key))
    .map((n) => ({
      node_key: n.node_key,
      node_type: n.node_type,
      node_name: n.node_name || nodeLabel(n.node_type),
      enabled: n.enabled,
    }));
}

/** 链的文本（`触发器 → 选题 → 生成`），停用节点标 `(停用)`。 */
export function chainText(nodes: ChainNode[]): string {
  if (nodes.length === 0) {
    return "（无节点）";
  }
  return nodes.map((n) => (n.enabled ? n.node_name : `${n.node_name}(停用)`)).join(" → ");
}
