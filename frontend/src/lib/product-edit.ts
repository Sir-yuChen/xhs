/**
 * M4「商品编辑优化」的**纯逻辑**（设计 20）。
 *
 * 单独成文件的意义：这些判据（PATCH 语义 / `updated[]` 中文名 / 导出上限 /
 * 采纳提示）**不需要渲染就能验证** —— vitest 直接跑真断言，而不是靠页面守卫
 * 正则匹配源码。
 *
 * ⚠️ 一条硬口径：**PATCH 的「不传」与「传 null」是两件事**。前端拼请求体时
 * 绝不能把没编辑的字段塞成 `null`（那等于清空）—— 本模块的
 * `buildEditPayload` 就是这条的唯一实现。
 */

/** 商品编辑可改的三个字段（设计 20 §2.1 端点 5）。 */
export const EDITABLE_FIELDS = ["group_id", "ai_title", "ai_selling_point"] as const;
export type EditableField = (typeof EDITABLE_FIELDS)[number];

/** 编辑表单的当前值（`undefined` = 用户没碰过这一项）。 */
export type EditFormState = Partial<Record<EditableField, number | string | null>>;

/**
 * 拼 `PATCH /items/edit` 的请求体 —— **只带用户真的改过的键**。
 *
 * ⚠️ **绝不能把没改的字段填 `null`**：后端把「显式 null」读成**清空**，
 * 只想改分组的用户会把标题清掉。这里用「键不存在」表达「不动」。
 */
export function buildEditPayload(
  base: { platform_account_id: number; item_id: string },
  form: EditFormState,
): Record<string, unknown> {
  const payload: Record<string, unknown> = { ...base };
  for (const field of EDITABLE_FIELDS) {
    if (field in form) {
      payload[field] = form[field];
    }
  }
  return payload;
}

/** 后端 `updated[]` 的代码名 → 中文提示（后端只回代码，中文名在前端一处）。 */
const UPDATED_LABELS: Record<string, string> = {
  group_id: "分组",
  ai_title: "AI 标题",
  ai_selling_point: "AI 卖点",
};

/**
 * `updated[]` → 用户能读的一句话。
 *
 * ⚠️ **空数组 = 什么都没改**，返回 `null`（调用方据此**不弹提示**）——
 * 报「已更新商品」而其实一个字段没动，是比不提示更糟的误导。
 */
export function updatedSummary(updated: string[] | null | undefined): string | null {
  const names = (updated ?? []).map((key) => UPDATED_LABELS[key] ?? key);
  if (names.length === 0) return null;
  return `已更新${names.join("、")}`;
}

/** 导出上限（与后端 `MAX_EXPORT_ROWS` 同值；超限前端先拦，不发请求）。 */
export const EXPORT_MAX_ROWS = 5000;

/**
 * 导出前的前置校验 —— 返回错误文案，`null` = 可导出。
 *
 * ⚠️ **超限不发请求**：后端会整批拒（400），前端先拦能省一次往返；
 * 但**判据必须与后端同值**（两份不同的上限 = 用户看到的与实际的不是一回事）。
 */
export function exportBlockReason(total: number, maxRows: number = EXPORT_MAX_ROWS): string | null {
  if (!Number.isFinite(total) || total <= 0) return "当前筛选条件下没有可导出的商品。";
  if (total > maxRows) {
    return `当前筛选条件下有 ${total} 行，超过导出上限 ${maxRows} 行。请收窄筛选条件后再导出。`;
  }
  return null;
}

/** 采纳一枚备选（设计 20 §3.1）—— 润色只回草稿，采纳才落库。 */
export function adoptCandidate(
  form: EditFormState,
  field: EditableField,
  value: string,
): EditFormState {
  return { ...form, [field]: value };
}

/** 分组筛选下拉的「未分组」哨兵值（与后端 `group_id=0` 同值）。 */
export const UNGROUPED_SENTINEL = 0;

/** 分组下拉选项（含「未分组」与「全部分组」两个特殊项）。 */
export function groupFilterOptions(groups: Array<{ id: number; name: string }>): Array<{
  value: number;
  label: string;
}> {
  return [
    { value: -1, label: "全部分组" },
    { value: UNGROUPED_SENTINEL, label: "未分组" },
    ...groups.map((group) => ({ value: group.id, label: group.name })),
  ];
}

/** 「全部分组」哨兵（不发 `group_id` 参数）。 */
export const ALL_GROUPS_SENTINEL = -1;

/** 分组下拉值 → 请求参数（`undefined` = 不筛）。 */
export function groupFilterParam(value: number): number | undefined {
  if (value === ALL_GROUPS_SENTINEL) return undefined;
  return value;
}
