/**
 * 调度任务中心的文案与口径唯一来源（设计 15 §9.3）。
 *
 * 表格、弹窗、详情、Tooltip 全部从这里取文案，不各写一份
 * （DEF-061 同型纪律：同一字段两处各写一份呈现 = 漂移）。
 *
 * ⚠️ 本文件的字面值是**对用户的契约**：tests/frontend/schedule_center.cjs
 * 钉的是外部字面期望表，改了这里的文案而没改那里 = 契约变更，脚本应当变红。
 */

import { CRON_PRESETS } from "./cron-builder";

export type TriggerType = "manual" | "cron" | "at" | string;
export type MisfirePolicy = "fire_now" | "once" | "discard" | string;
export type ConcurrentMode = "forbid" | "allow" | string;
export type JobStatus = "active" | "paused" | "completed" | string;
export type LogStatus = "running" | "success" | "failed" | "skipped" | string;

/**
 * 任务类型三档（2026-09-24 用户口径 R14 + R15）：
 * 手动 / Cron / 定时（列表展示用**简写**，R15 用户口径「尽量简写」）。
 * ⚠️「间隔周期」已并入 Cron 周期（不再是一档）。
 */
export const TRIGGER_TYPE_LABELS: Record<string, string> = {
  manual: "手动",
  cron: "Cron",
  at: "定时",
};

/**
 * 任务类型**全称**（表单选项 / 下拉 Tooltip 用；列表列宽有限，那里用简写）。
 * 两份文案同源一表：谁都不许在自己那处拼字符串（DEF-044/047/058 纪律）。
 */
export const TRIGGER_TYPE_FULL_LABELS: Record<string, string> = {
  manual: "手动执行",
  cron: "Cron 周期",
  at: "定时一次",
};

export const MISFIRE_POLICY_LABELS: Record<string, string> = {
  fire_now: "立即执行",
  once: "执行一次",
  discard: "放弃执行",
};

/** 每档一句解释（Tooltip 用；语义见设计 15 §6.3 的 APScheduler 映射表） */
export const MISFIRE_POLICY_TIPS: Record<string, string> = {
  fire_now: "错过的执行全部补跑：数据完整性最高，积压时会瞬间扎堆",
  once: "错过的多次执行合并为一次补跑：统计 / 同步类任务推荐",
  discard: "错过的执行全部丢弃，只等下一个周期：巡检 / 清理类推荐",
};

export const CONCURRENT_LABELS: Record<string, string> = {
  forbid: "禁止并发",
  allow: "允许并发",
};

export const JOB_STATUS_LABELS: Record<string, { color: string; label: string }> = {
  active: { color: "green", label: "运行中" },
  paused: { color: "default", label: "已暂停" },
  completed: { color: "blue", label: "已完成" },
};

export const LOG_STATUS_LABELS: Record<string, { color: string; label: string }> = {
  running: { color: "processing", label: "执行中" },
  success: { color: "success", label: "成功" },
  failed: { color: "error", label: "失败" },
  skipped: { color: "warning", label: "已跳过（退避中）" },
};

export const LOG_TRIGGER_LABELS: Record<string, string> = {
  scheduled: "调度触发",
  manual: "手动执行",
};

/** 类型标签：列表用简写；需要说清时用 `TRIGGER_TYPE_FULL_LABELS`。 */
export function triggerTypeLabel(type: string): string {
  return TRIGGER_TYPE_LABELS[type] ?? type;
}

/** 类型全称：表单 / 下拉 / Tooltip 用。 */
export function triggerTypeFullLabel(type: string): string {
  return TRIGGER_TYPE_FULL_LABELS[type] ?? triggerTypeLabel(type);
}

export function misfirePolicyLabel(policy: string): string {
  return MISFIRE_POLICY_LABELS[policy] ?? policy;
}

export function misfirePolicyTip(policy: string): string {
  return MISFIRE_POLICY_TIPS[policy] ?? "";
}

export function concurrentLabel(mode: string): string {
  return CONCURRENT_LABELS[mode] ?? mode;
}

export function jobStatusTag(status: string): { color: string; label: string } {
  return JOB_STATUS_LABELS[status] ?? { color: "default", label: status };
}

export function logStatusTag(status: string): { color: string; label: string } {
  return LOG_STATUS_LABELS[status] ?? { color: "default", label: status };
}

export function logTriggerLabel(trigger: string): string {
  return LOG_TRIGGER_LABELS[trigger] ?? trigger;
}

/**
 * 触发器摘要（列表列用）。cron 先匹配预设（`CRON_PRESETS`），
 * 匹配不上回原文 —— 不在前端自造第二个 cron 解释器。
 *
 * ⚠️ R14：`manual` 档没有触发器，摘要固定为「手动触发」；`interval` 已退役，
 * 不再有 interval 分支（老行由迁移换算成 cron）。
 */
export function triggerSummary(job: {
  trigger_type: string;
  cron_expression?: string;
  run_at?: string | null;
}): string {
  if (job.trigger_type === "manual") {
    return "手动触发（不自动执行）";
  }
  if (job.trigger_type === "at") {
    return job.run_at ? `定时一次 · ${job.run_at.replace("T", " ").slice(0, 16)}` : "定时一次";
  }
  const cron = (job.cron_expression ?? "").trim();
  const preset = CRON_PRESETS.find((item) => item.cron === cron);
  return preset ? preset.label : cron || "-";
}
