/**
 * cron 表达式构建器（设计 15 §9.2）——纯函数，可单测。
 *
 * 分工边界：
 * - 本文件负责**结构化编辑**（六个字段的点选）与预设；
 * - **合法性唯一来源是后端 APScheduler 实解析**（`POST /api/schedule/cron/validate`），
 *   前端不造第二个校验器（与 `lib/note-filters.ts` 的口径一致：文案/校验单一来源）。
 *
 * 6 位 cron（秒 分 时 日 月 周），APScheduler 口径：
 * - `day_of_week` 数字 **0=周一 … 6=周日**（也接受 mon/tue 名字）；
 * - 迁移自旧 auto_tasks 的 isoweekday（1=周一…7=周日）已由后端换算，前端只认 APScheduler 口径。
 */

export type FieldMode = "every" | "value" | "range" | "step" | "list";

export type CronFieldState = {
  mode: FieldMode;
  /** value / range / step / list 的取值（字符串原样，如 "9"、"1-5"、"10/2"、"1,15"） */
  value: string;
};

export type CronFieldKey = "second" | "minute" | "hour" | "day" | "month" | "week";

export type CronDraft = Record<CronFieldKey, CronFieldState>;

export const CRON_FIELD_LABELS: Record<CronFieldKey, string> = {
  second: "秒",
  minute: "分",
  hour: "时",
  day: "日",
  month: "月",
  week: "周",
};

export const CRON_FIELD_ORDER: CronFieldKey[] = [
  "second",
  "minute",
  "hour",
  "day",
  "month",
  "week",
];

/** 各字段的点选候选（周用 APScheduler 数字口径：0=周一） */
export const CRON_FIELD_OPTIONS: Record<CronFieldKey, Array<{ value: string; label: string }>> = {
  second: minutesSecondsOptions(0, 59),
  minute: minutesSecondsOptions(0, 59),
  hour: minutesSecondsOptions(0, 23),
  day: minutesSecondsOptions(1, 31),
  month: minutesSecondsOptions(1, 12),
  week: [
    { value: "0", label: "周一" },
    { value: "1", label: "周二" },
    { value: "2", label: "周三" },
    { value: "3", label: "周四" },
    { value: "4", label: "周五" },
    { value: "5", label: "周六" },
    { value: "6", label: "周日" },
  ],
};

function minutesSecondsOptions(from: number, to: number): Array<{ value: string; label: string }> {
  const options: Array<{ value: string; label: string }> = [];
  for (let i = from; i <= to; i += 1) {
    options.push({ value: String(i), label: String(i).padStart(2, "0") });
  }
  return options;
}

/** 常用预设（一键填充；旧 auto-tasks 四档的一键等价都在这里） */
export const CRON_PRESETS: Array<{ label: string; cron: string }> = [
  { label: "每分钟", cron: "0 * * * * *" },
  { label: "每小时整点", cron: "0 0 * * * *" },
  { label: "每天 9:00", cron: "0 0 9 * * *" },
  { label: "每天 9:00 与 18:00", cron: "0 0 9,18 * * *" },
  { label: "每周一、五 9:00", cron: "0 0 9 * * 0,4" },
  { label: "每月 1 号 9:00", cron: "0 0 9 1 * *" },
  { label: "每 6 小时", cron: "0 0 */6 * * *" },
];

export function emptyCronDraft(): CronDraft {
  return {
    second: { mode: "value", value: "0" },
    minute: { mode: "value", value: "0" },
    hour: { mode: "value", value: "9" },
    day: { mode: "every", value: "*" },
    month: { mode: "every", value: "*" },
    week: { mode: "every", value: "*" },
  };
}

function detectMode(part: string): CronFieldState {
  const text = part.trim();
  if (text === "*") {
    return { mode: "every", value: "*" };
  }
  if (/^\d+(,\d+)*$/.test(text)) {
    return { mode: text.includes(",") ? "list" : "value", value: text };
  }
  if (/^\d+-\d+$/.test(text)) {
    return { mode: "range", value: text };
  }
  if (/^(\d+-\d+|\*)\/\d+$/.test(text)) {
    return { mode: "step", value: text };
  }
  // 解析不了（混合形态等）⇒ 回退为「原样值 + value 模式不可信」，
  // 由调用方决定是否转原文模式（见 parseCron 的返回值约定）。
  return { mode: "value", value: text };
}

/**
 * 解析 cron 原文为结构化草稿。六个字段全部可识别才返回 draft；
 * 任一字段不可识别返回 null（调用方转原文模式，不猜）。
 */
export function parseCron(expression: string): CronDraft | null {
  const parts = (expression ?? "").trim().split(/\s+/);
  if (parts.length !== 6) {
    return null;
  }
  const draft = emptyCronDraft();
  const rawStates = parts.map(detectMode);
  const unrecognized = rawStates.filter(
    (state, index) => state.mode === "value" && !/^\d+(,\d+)*$/.test(parts[index].trim())
  );
  if (unrecognized.length > 0) {
    return null;
  }
  CRON_FIELD_ORDER.forEach((key, index) => {
    draft[key] = rawStates[index];
  });
  return draft;
}

function fieldExpression(state: CronFieldState): string {
  switch (state.mode) {
    case "every":
      return "*";
    case "value":
      return state.value.trim() || "*";
    case "range":
      return state.value.trim() || "*";
    case "step":
      return state.value.trim() || "*";
    case "list":
      return state.value.trim() || "*";
    default:
      return "*";
  }
}

/** 结构化草稿 → cron 原文（6 位）。 */
export function buildCron(draft: CronDraft): string {
  return CRON_FIELD_ORDER.map((key) => fieldExpression(draft[key])).join(" ");
}

/** 浅校验：六段非空、不含明显非法字符（深校验在后端，本函数只防手滑）。 */
export function cronLooksSane(expression: string): boolean {
  const parts = (expression ?? "").trim().split(/\s+/);
  if (parts.length !== 6) {
    return false;
  }
  return parts.every((part) => /^[\d*/,\-]+$/.test(part));
}
