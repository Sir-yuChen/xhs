/**
 * 运行时配置中心的**纯展示/转换逻辑**（设计 10 §7.6，2026-09-27）。
 *
 * 为什么单独一层：设置页的这几件事都是「输入 → 输出」的确定映射，混在 `.tsx` 里
 * 就只能靠人眼看源码，测不了。抽出来之后 `tests/frontend/settings_view.cjs` 能直接
 * 调用它们做行为断言（含变异验证）—— 与 `lib/sync-gates.ts` 同一套做法。
 *
 * 三条口径（都是「不许想当然」的地方）：
 *
 * 1. **布尔渲染成「开 / 关」**，不是 `true` / `false`：这是给运营看的页面；
 * 2. **缺值渲染 `-` 不渲染 `0`**（硬约束⑤）：`null` 与 `0` 是两件事；
 * 3. **表单值 → 提交值按 `value_type` 分派**：`bool` 用开关、数值用 InputNumber、
 *    其余用文本框 —— 分派错了会把「关」提交成 `"false"` 字符串（存进库就是静默错值）。
 */

import type {
  SystemSetting,
  SystemSettingCreatePayload,
  SystemSettingUpdatePayload,
  SystemSettingValueType,
} from "../types";
import { dictLabel } from "./system-dicts-core";

/** 分组中文名（未知分组回落原名 —— 不猜、也不显示空白）。 */
export const CATEGORY_LABELS: Record<string, string> = {
  sync: "同步与限流",
  scheduler: "调度",
  publish: "发布与风控",
  ai: "AI 能力",
  feature: "实验特性",
};

export const VALUE_TYPE_OPTIONS: Array<{ value: SystemSettingValueType; label: string }> = [
  { value: "int", label: "整数" },
  { value: "float", label: "小数" },
  { value: "bool", label: "开关" },
  { value: "str", label: "文本" },
  { value: "json", label: "JSON" },
];

/** 分组中文名（未知分组原样返回，不显示空白）。 */
export function categoryLabel(category: string): string {
  // 设计 63：分组名走字典 `sys_setting_category`（未知分组仍回落原名 —— 不猜）。
  return dictLabel("sys_setting_category", category, CATEGORY_LABELS);
}

/** 值 → 表格单元格文本。`null` / `undefined` ⇒ `-`（缺值口径，不渲染 0）。 */
export function renderSettingValue(setting: SystemSetting): string {
  const value = setting.value;
  const type = String(setting.value_type || "");
  if (value === null || value === undefined) return "-";
  if (type === "bool") return value ? "开" : "关";
  if (type === "json") return typeof value === "string" ? value : JSON.stringify(value);
  return String(value);
}

/** 生效性标记（`is_live` 是后端按白名单派生的字段，前端不自己猜）。 */
export function liveBadge(setting: SystemSetting): { label: string; tip: string } {
  if (setting.is_live) {
    return {
      label: "实时生效",
      tip: setting.live_read_point ? `读点：${setting.live_read_point}` : "已接入运行时读点",
    };
  }
  return {
    label: "仅存档",
    tip: "该键没有运行时读点：改它只影响这里显示的值，不影响程序行为。",
  };
}

/** 弹窗表单里那一组值（三类控件各一个字段，按类型取用）。 */
export type SettingFormValues = {
  key: string;
  value_type: string;
  category: string;
  label: string;
  description: string;
  requires_restart: boolean;
  is_dangerous: boolean;
  text_value: string;
  number_value: number | null;
  bool_value: boolean;
};

/** 一行配置 → 表单初值（编辑时用）。 */
export function settingToFormValues(setting: SystemSetting): SettingFormValues {
  const type = String(setting.value_type || "str");
  const raw = setting.value;
  return {
    key: setting.key,
    value_type: type,
    category: setting.category,
    label: setting.label,
    description: setting.description,
    requires_restart: setting.requires_restart,
    is_dangerous: setting.is_dangerous,
    text_value:
      type === "json"
        ? typeof raw === "string"
          ? raw
          : JSON.stringify(raw ?? null)
        : raw === null || raw === undefined
          ? ""
          : String(raw),
    number_value: typeof raw === "number" ? raw : null,
    bool_value: Boolean(raw),
  };
}

/** 新建时的空表单初值。 */
export function emptyFormValues(): SettingFormValues {
  return {
    key: "",
    value_type: "int",
    category: "sync",
    label: "",
    description: "",
    requires_restart: false,
    is_dangerous: false,
    text_value: "",
    number_value: null,
    bool_value: false,
  };
}

/**
 * 表单值 → 提交值（按 `value_type` 分派）。
 *
 * ⚠️ `bool` 必须取 `bool_value`（布尔），**不许**回退到 `text_value` ——
 * 回退会把开关状态提交成字符串 `"true"` / `"false"`，后端按 `bool` 解时
 * `"false"` 恰好能解对、`""` 会 422，而**用户看到的是「保存成功但没生效」**。
 * 数值类型同理：`null` 是「没填」，交给后端 422 报出来，不在这里猜成 0。
 */
export function formValuesToSubmitValue(values: SettingFormValues): unknown {
  const type = String(values.value_type || "str");
  if (type === "bool") return values.bool_value;
  if (type === "int" || type === "float") return values.number_value;
  return values.text_value;
}

/** 新增提交体（带 `key`）。
 *
 * ⚠️ 逐字段写而不是 `{key, ...update}`：展开会让 `value` 的类型退化成可选
 * （`SystemSettingUpdatePayload.value` 是可选的），于是「忘了带值」在类型上不可见。
 */
export function formValuesToCreatePayload(values: SettingFormValues): SystemSettingCreatePayload {
  return {
    key: String(values.key || "").trim(),
    value: formValuesToSubmitValue(values),
    value_type: values.value_type as SystemSettingValueType,
    category: values.category,
    label: values.label,
    description: values.description,
    requires_restart: values.requires_restart,
    is_dangerous: values.is_dangerous,
  };
}

/** 编辑提交体（**不带 `key`** —— 改键要删旧建新，界面也不给改）。 */
export function formValuesToUpdatePayload(values: SettingFormValues): SystemSettingUpdatePayload {
  return {
    value: formValuesToSubmitValue(values),
    value_type: values.value_type as SystemSettingValueType,
    category: values.category,
    label: values.label,
    description: values.description,
    requires_restart: values.requires_restart,
    is_dangerous: values.is_dangerous,
  };
}

/** 分组筛选项（「全部」+ 实际出现过的分组，保序）。 */
export function categoryOptions(
  settings: SystemSetting[]
): Array<{ value: string; label: string }> {
  const seen = new Set(settings.map((item) => item.category));
  return [
    { value: "all", label: "全部" },
    ...[...seen].sort().map((value) => ({ value, label: categoryLabel(value) })),
  ];
}

/** 按分组筛选（`all` = 不过滤）。 */
export function filterByCategory(settings: SystemSetting[], category: string): SystemSetting[] {
  return category === "all" ? settings : settings.filter((item) => item.category === category);
}
