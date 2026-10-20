/**
 * 店铺违规管理 · 纯逻辑（设计 43，2026-10-18）
 *
 * ## 为什么逻辑要单独成一个文件
 *
 * 页面是 `.tsx`，没有组件测试框架；而这个需求里有**几条必须被守住的规则**：
 *
 * 1. 「批量操作只提示、不发请求」—— 用户口径「开发过程中不要触发批量删除 / 批量操作」；
 * 2. 「申诉提交不伪造成功」—— 只读边界；
 * 3. 「缺值渲染 `-`，不渲染 `0`」—— 硬约束⑤；
 * 4. 「违规原因 tag 名不 trim」—— `违反法律法规` 与尾空格那组是两条 tag。
 *
 * 写在页面里，这四条都断言不了（守卫只能断言「页面没出现某个字符串」，很脆）。
 * 抽成纯函数后，`violation-manage.test.ts` 可以逐条钉死，且页面只需调用。
 */

import type { ViolationEnumOption, ViolationReasonTag } from "../types/violation";

// --------------------------------------------------------------------------- #
// 子 Tab
// --------------------------------------------------------------------------- #
export type ViolationTabKey =
  | "warnings"
  | "violations"
  | "compensations"
  | "arbitrations"
  | "spot-checks"
  | "exemptions";

/** 子 Tab（顺序与千帆后台逐字一致）。 */
export const VIOLATION_TABS: { key: ViolationTabKey; label: string }[] = [
  { key: "warnings", label: "预警列表" },
  { key: "violations", label: "违规列表" },
  { key: "compensations", label: "赔付列表" },
  { key: "arbitrations", label: "仲裁列表" },
  { key: "spot-checks", label: "抽检违规列表" },
  { key: "exemptions", label: "免罚机会" },
];

export const DEFAULT_VIOLATION_TAB: ViolationTabKey = "violations";

/** URL `?tab=` ⇒ 合法 tab（非法值回落默认，不让页面空白）。 */
export function normalizeTab(raw: string | null | undefined): ViolationTabKey {
  const hit = VIOLATION_TABS.find((tab) => tab.key === raw);
  return hit ? hit.key : DEFAULT_VIOLATION_TAB;
}

// --------------------------------------------------------------------------- #
// 快捷筛选（真机 bundle 原文的顺序与语义）
// --------------------------------------------------------------------------- #
export type QuickFilterKey =
  | "all"
  | "pending"
  | "unread"
  | "severe"
  | "recent7"
  | "correctable"
  | "examinable"
  | "appealable"
  | "high_risk";

export type QuickFilter = { key: QuickFilterKey; label: string };

/** 违规列表的快捷筛选（顺序逐字对齐千帆）。 */
export const VIOLATION_QUICK_FILTERS: QuickFilter[] = [
  { key: "all", label: "全部" },
  { key: "pending", label: "待处理" },
  { key: "unread", label: "未读" },
  { key: "severe", label: "严重违规" },
  { key: "recent7", label: "近7日违规" },
  { key: "correctable", label: "可整改" },
  { key: "examinable", label: "可考试" },
  { key: "appealable", label: "可申诉" },
];

/** 预警列表的快捷筛选是**另一套**（4 个）。 */
export const WARNING_QUICK_FILTERS: QuickFilter[] = [
  { key: "all", label: "全部" },
  { key: "high_risk", label: "高危预警" },
  { key: "correctable", label: "可整改" },
  { key: "appealable", label: "可申诉" },
];

/**
 * 快捷筛选胶囊文案。`unread` / `pending` 两个带累计数（千帆：`未读3`）。
 *
 * ⚠️ 计数为 `null`/`undefined` 时**只显示标签**（不显示 `未读0`）——
 * 那是「还没查回来」，不是「零条」。
 */
export function quickFilterLabel(
  filter: QuickFilter,
  counts: Record<string, number | null | undefined>
): string {
  const value = counts[filter.key];
  if (filter.key === "unread" || filter.key === "pending") {
    return typeof value === "number" ? `${filter.label}${value}` : filter.label;
  }
  return filter.label;
}

/** 需要独立查 `counts` 的快捷项（其余不显示数字）。 */
export const COUNTED_QUICK_KEYS: QuickFilterKey[] = ["unread", "pending"];

// --------------------------------------------------------------------------- #
// 批量操作（真的可用）
// --------------------------------------------------------------------------- #
/**
 * ⚠️ 2026-10-18 用户口径修正（推翻首版「一律拦截」）：
 *
 * > 「系统支持批量功能，只是在你开发过程中不要使用大量的数据去测试批量功能，
 * >  只使用一条数据测试批量按钮即可」
 *
 * 所以批量入口**必须真跑通**，只是**开发与验收阶段只用一条数据**。
 * 原来那条 `isBatchBlocked()` 恒返回 `true` 的写法是**把功能阉掉**，不是「安全」。
 *
 * ## 批量到什么程度（千帆实际能力为准）
 *
 * 实测千帆违规列表页**没有行勾选框、也没有批量写接口**（只有单条 `read_mark`）
 * ⇒ 平台侧不存在「批量删除 / 批量申诉」这类动作。故本系统提供的批量是：
 *
 * | 批量项 | 是否写平台 | 说明 |
 * |---|---|---|
 * | **标记已读 / 未读** | 否 | 与单条同源（本地标记），只是批量套用 |
 * | **导出选中记录** | 否 | 只读导出 CSV，运营可以直接拿去核对 |
 * | 批量删除 / 批量申诉 | **不提供** | 平台没有该能力；凭空造一个删数据的按钮更危险 |
 */
export type BatchActionKey = "mark_read" | "mark_unread" | "export" | "delete_related";

/** 批量项的中文名（菜单与确认框同一份文案）。 */
export const BATCH_ACTION_LABELS: Record<BatchActionKey, string> = {
  mark_read: "标记为已读",
  mark_unread: "标记为未读",
  export: "导出选中记录",
  // 删的是违规**关联的本地内容**，不是违规记录本身（平台违规删不掉）
  delete_related: "删除关联内容",
};

/** 需要二次确认的批量项（其余直接执行）。 */
export const BATCH_CONFIRM_ACTIONS: BatchActionKey[] = ["export", "delete_related"];

/** 批量执行结果的一句话（`done` = 实际生效条数）。 */
export function batchOutcomeText(action: BatchActionKey, done: number): string {
  if (done <= 0) return "没有可处理的行";
  return `${BATCH_ACTION_LABELS[action]}：已处理 ${done} 条`;
}

/**
 * 可选中行的「批量勾选」判据。
 *
 * 只有**当前页真实渲染出来的行**才能被选中 —— 跨页残留的 key 会让批量动作作用到
 * 用户看不见的数据上，这是最容易被忽略的一类误操作。
 */
export function keepVisibleKeys(selectedKeys: string[], visibleIds: string[]): string[] {
  const visible = new Set(visibleIds);
  return selectedKeys.filter((key) => visible.has(key));
}

/** 选中行 ⇒ 导出用的一行对象数组（字段名即中文表头，运营可直接看）。 */
export function rowsForExport(
  rows: Array<Record<string, unknown>>,
  selectedIds: string[]
): Array<Record<string, string>> {
  const picked = new Set(selectedIds);
  return rows
    .filter((row) => picked.has(String(row.violation_id ?? "")))
    .map((row) => ({
      违规原因: String(row.violation_reason ?? ""),
      违规对象: String(row.entity_name ?? row.display_entity_name ?? ""),
      处罚方式: Array.isArray(row.actions) ? row.actions.map(String).join("/") : String(row.actions ?? ""),
      违规单号: String(row.violation_id ?? ""),
      违规时间: String(row.violation_time ?? ""),
      处罚处理状态: String(row.appeal_status_name ?? ""),
      处罚状态: String(row.status_name ?? ""),
    }));
}

// --------------------------------------------------------------------------- #
// 删除关联内容（设计 44，2026-10-18 用户需求）
// --------------------------------------------------------------------------- #
/**
 * 这条违规**有没有可删的本地内容**。
 *
 * 只有「商品(1) / 笔记(3)」有本地实体可删；店铺(0)/账号(2)/直播间(4) 没有 ——
 * 账号是登录主体（删了等于删账号），直播间本系统不存实体。
 *
 * ⚠️ 判据在**一行**上，所以列表里不可删的行按钮直接禁用并给 tooltip，
 * 而不是让用户点了才看到失败回执。
 */
export function canDeleteRelated(entityType: number | null | undefined): boolean {
  return entityType === 1 || entityType === 3;
}

/** 不可删时给用户的一句话（说清「为什么不能删」，不是「不支持」）。 */
export function deleteBlockedReason(entityType: number | null | undefined): string {
  if (entityType === 0) return "店铺没有可删除的本地内容";
  if (entityType === 2) return "账号是登录主体，不能在这里删除";
  if (entityType === 4) return "直播间没有本地内容可删";
  return "该对象没有可删除的本地内容";
}

/** 删除按钮/菜单项文案（按对象类型说人话）。 */
export function deleteActionLabel(entityType: number | null | undefined): string {
  if (entityType === 1) return "删除关联商品";
  if (entityType === 3) return "删除关联笔记";
  return "删除关联内容";
}

/**
 * 删除前给用户看的影响说明。
 *
 * ⚠️ 必须点明「**先删平台，再删本地**」和「违规记录本身删不掉」两件事：
 * 前者决定用户的预期（平台内容真会被删），后者防止误会（以为处罚被撤销）。
 */
export function deleteConfirmText(count: number): string {
  return (
    `将对选中的 ${count} 条违规执行删除：\n\n` +
    // ⚠️ 界面上不许出现 Markdown 的 `**`（本仓有守卫扫用户可见文案）
    "① 先删除平台上的商品 / 笔记（不可恢复）；\n" +
    "② 再检查本地是否有对应数据，有则一并删除，没有则跳过。\n\n" +
    "⚠️ 违规记录本身不会被删除 —— 违规单是平台处罚凭证，平台不提供删除接口。"
  );
}

/**
 * 删除结果的一句话汇总（四态分别计数）。
 *
 * 把 `platform_only` 说成「成功」是对的（平台已删），但必须**单独报**它有几次 ——
 * 否则用户以为本地也清了，回头发现本地还在会以为没生效。
 */
export function deleteOutcomeText(summary: {
  deleted: number;
  platform_only: number;
  failed: number;
  skipped: number;
  /** 平台已无此数据且本地也没有（已在别处删过）。 */
  absent?: number;
}): string {
  const parts: string[] = [];
  if (summary.deleted > 0) parts.push(`已删除 ${summary.deleted} 条（平台+本地）`);
  if (summary.platform_only > 0) parts.push(`${summary.platform_only} 条仅删平台（本地无数据，已跳过）`);
  if (summary.skipped > 0) parts.push(`${summary.skipped} 条无可删内容（已跳过）`);
  if ((summary.absent ?? 0) > 0) {
    // 用户口径 2026-10-19：平台与本地都没有 ⇒ 明确告知「已被删除，不要重复删除」
    parts.push(`${summary.absent} 条数据已经被删除，不要重复删除`);
  }
  if (summary.failed > 0) parts.push(`${summary.failed} 条平台删除失败`);
  return parts.length > 0 ? parts.join("；") : "没有可处理的行";
}

/** 导出 CSV 的列顺序（= 列表表头的中文名）。 */
export const EXPORT_HEADERS = [
  "违规原因",
  "违规对象",
  "处罚方式",
  "违规单号",
  "违规时间",
  "处罚处理状态",
  "处罚状态",
];

/** 导出 CSV 文本（UTF-8 BOM 由调用方的 Blob 决定；这里只管转义）。 */
export function toCsv(rows: Array<Record<string, string>>, headers: string[]): string {
  const esc = (value: string) => (/[",\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value);
  const lines = [headers.map(esc).join(",")];
  for (const row of rows) {
    lines.push(headers.map((h) => esc(String(row[h] ?? ""))).join(","));
  }
  return lines.join("\r\n");
}

/** 已读标记仅本地时给用户看的一句（守卫按它断言，逐字固定）。 */
export const LOCAL_READ_MESSAGE = "已读标记仅本地生效，不同步平台";

// --------------------------------------------------------------------------- #
// 已读标记：只在前端本地切换（平台写接口不接）
// --------------------------------------------------------------------------- #
/** 已读标记的本地存储键（与千帆的「未读 N」角标无关，不参与任何过滤）。 */
export const LOCAL_READ_STORAGE_KEY = "xhs.violation-manage.read-marks";

/** 已读集合的类型：`<账号 ID>:<违规单号>` ⇒ `true`。 */
export type ReadMarkMap = Record<string, boolean>;

/** 一条记录在本地的已读键。账号维度隔离 —— 同一单号不同账号互不影响。 */
export function readMarkKey(accountId: number | null, violationId: string | null | undefined): string {
  return `${accountId ?? ""}:${(violationId || "").trim()}`;
}

/**
 * 本地切换一条记录的已读标记，**返回新表**（不改入参 —— 调用方是 React state）。
 *
 * 纯函数：不做 IO。读写 localStorage 留在页面（`loadReadMarks` / `saveReadMarks`），
 * 这样这条判据能在 vitest 里直接测。
 */
export function toggleReadMark(
  marks: ReadMarkMap,
  accountId: number | null,
  violationId: string | null | undefined
): ReadMarkMap {
  const key = readMarkKey(accountId, violationId);
  if (!(violationId || "").trim()) return { ...marks };
  const next = { ...marks };
  if (next[key]) delete next[key];
  else next[key] = true;
  return next;
}

/**
 * 批量设定若干条记录的已读标记（**幂等设定**，不是逐条切换）。
 *
 * ⚠️ 批量必须用「设定」而不是循环调 `toggleReadMark`：循环切换会让已经处于目标状态的
 * 行被翻回去（勾 5 条其中 3 条已读 ⇒ 结果 3 条变未读）。这是批量最容易出的错。
 */
export function setReadMarks(
  marks: ReadMarkMap,
  accountId: number | null,
  violationIds: string[],
  read: boolean
): ReadMarkMap {
  const next = { ...marks };
  for (const id of violationIds) {
    if (!(id || "").trim()) continue;
    const key = readMarkKey(accountId, id);
    if (read) next[key] = true;
    else delete next[key];
  }
  return next;
}

/** 这条记录在本地是不是已读。 */
export function isLocallyRead(
  marks: ReadMarkMap,
  accountId: number | null,
  violationId: string | null | undefined
): boolean {
  return Boolean(marks[readMarkKey(accountId, violationId)]);
}

/** 解析本地已读表（坏数据 ⇒ 空表，不抛 —— 一个 localStorage 值不该白屏）。 */
export function parseReadMarks(raw: string | null | undefined): ReadMarkMap {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const out: ReadMarkMap = {};
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (value) out[key] = true;
    }
    return out;
  } catch {
    return {};
  }
}

// --------------------------------------------------------------------------- #
// 申诉：本系统不代提交（只读边界）
// --------------------------------------------------------------------------- #
export type AppealSubmitVerdict = {
  /** 恒为 `true` —— 本系统没有可用的申诉提交路径。 */
  blocked: true;
  /** 给用户看的话（逐字，守卫会断言）。 */
  message: string;
  /** 指向千帆后台（用户实际要去做的地方）。 */
  dashboardHint: string;
};

/**
 * 申诉弹窗点「确认提交」后的判定。
 *
 * ⚠️ **没有成功分支**：平台申诉提交是写接口，本系统只读（设计 43 §1.3）。
 * 这里返回一个**恒 blocked** 的结果，页面据此弹 `Modal.warning`，
 * 绝不出现「提交成功」—— 那会是给用户的假回执。
 */
export function appealSubmitVerdict(): AppealSubmitVerdict {
  return {
    blocked: true,
    message: "本系统不代提交申诉，请在千帆后台「违规管理」中提交。",
    dashboardHint: "ark.xiaohongshu.com/app-violation/violation-manage/list",
  };
}

/** 申诉说明长度上限（与千帆一致：0/200）。 */
export const APPEAL_NOTE_MAX = 200;

/** 申诉凭证的允许格式与大小（千帆原文：10MB / jpg png pdf zip rar）。 */
export const APPEAL_FILE_LIMIT_MB = 10;
export const APPEAL_FILE_TYPES = ["jpg", "png", "pdf", "zip", "rar"];

/** 校验一个申诉凭证文件；返回空串表示通过。 */
export function validateAppealFile(file: {
  name: string;
  size: number;
}): string {
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  if (!APPEAL_FILE_TYPES.includes(ext)) {
    return `仅支持 ${APPEAL_FILE_TYPES.join("、")} 格式`;
  }
  if (file.size > APPEAL_FILE_LIMIT_MB * 1024 * 1024) {
    return `单个文件不能超过 ${APPEAL_FILE_LIMIT_MB} MB`;
  }
  return "";
}

// --------------------------------------------------------------------------- #
// 渲染口径：缺值 `-`
// --------------------------------------------------------------------------- #
/** 缺值占位（所有「该有但平台没给」的字段都用它）。 */
export const EMPTY_TEXT = "-";

/**
 * 文本渲染：`null` / `undefined` / 空串 ⇒ `-`。
 *
 * ⚠️ **不要把 `0` 变成 `-`**：`0` 是真实值（例如「赔付金额 0」），
 * 把它当缺值渲染会掩盖真实数据。这是硬约束⑤的反向陷阱。
 */
export function renderText(value: unknown): string {
  if (value === null || value === undefined) return EMPTY_TEXT;
  const text = String(value).trim();
  return text === "" ? EMPTY_TEXT : text;
}

/** 毫秒时间戳 ⇒ `YYYY-MM-DD HH:mm:ss`；缺值/非法 ⇒ `-`。 */
export function renderTime(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    return EMPTY_TEXT;
  }
  // 平台用 4102416000000（9999-12-31 前后）表示「长期有效」，如实显示而不是 -。
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return EMPTY_TEXT;
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

/** 处罚方式：多条用 `/` 连接（千帆原文）；空 ⇒ `-`。 */
export function renderActions(actions: string[] | null | undefined): string {
  if (!Array.isArray(actions) || actions.length === 0) return EMPTY_TEXT;
  const cleaned = actions.filter((item) => String(item ?? "").trim() !== "");
  return cleaned.length ? cleaned.join("/") : EMPTY_TEXT;
}

/**
 * 违规对象展示：对象名 + 编号。
 *
 * 千帆原文形如 `商品编号: 6a36…` / `账号编号: 5bdc…` / `笔记编号: 6a66…`；
 * 名称可能缺（笔记类有时只给编号），缺哪半就只显示另一半。
 */
export function renderEntity(row: {
  entity_type_name?: string | null;
  entity_id?: string | null;
  entity_name?: string | null;
}): { name: string; idText: string } {
  const typeName = (row.entity_type_name || "").trim();
  const id = (row.entity_id || "").trim();
  return {
    name: renderText(row.entity_name),
    idText: typeName && id ? `${typeName}编号: ${id}` : id ? `编号: ${id}` : EMPTY_TEXT,
  };
}

// --------------------------------------------------------------------------- #
// 状态标签颜色（千帆：可申诉/已取消=中性，处罚中=红，已处罚=橙，待处罚=灰）
// --------------------------------------------------------------------------- #
export type ViolationTagColor = "default" | "success" | "processing" | "error" | "warning";

/** 处罚状态 → 标签色（按 `violationStatus` 数值，不按文案 —— 文案可能变）。 */
export function statusTagColor(status: unknown): ViolationTagColor {
  switch (Number(status)) {
    case 0:
      return "default"; // 待处罚
    case 1:
      return "warning"; // 已处罚
    case 2:
      return "error"; // 处罚中
    case -1:
      return "default"; // 已取消
    default:
      return "default";
  }
}

/** 申诉状态 → 标签色（`appealStatus`：0 可申诉 / 100 不可申诉）。 */
export function appealTagColor(appealStatus: unknown): ViolationTagColor {
  switch (Number(appealStatus)) {
    case 0:
      return "processing"; // 可申诉
    case 1:
      return "processing"; // 申诉中
    case 2:
      return "success"; // 申诉成功
    case 3:
      return "error"; // 申诉失败
    case 6:
      return "default"; // 超时未申诉
    case 100:
      return "default"; // 不可申诉
    default:
      return "default";
  }
}

/** 「至 2027/1/23 结束」后缀（处罚有明确结束时间时显示；长期有效不显示）。 */
export function renderPeriodEnd(periodEndTime: unknown, now = Date.now()): string {
  if (typeof periodEndTime !== "number" || !Number.isFinite(periodEndTime)) return "";
  // 4102416000000 ≈ 2100-01-01：平台用它表示「无结束时间 / 长期」，不显示后缀。
  if (periodEndTime >= 4102416000000) return "";
  if (periodEndTime <= now) return "已结束";
  const date = new Date(periodEndTime);
  return `至 ${date.getFullYear()}/${date.getMonth() + 1}/${date.getDate()} 结束`;
}

// --------------------------------------------------------------------------- #
// 下拉数据
// --------------------------------------------------------------------------- #
/** 零宽空格（U+200B）。平台 `user_tag` 实测里有 `违反法律法规` 与 `违反法律法规\u200b` 两条。 */
export const ZERO_WIDTH_SPACE = "\u200b";

/**
 * tag 名里有没有不可见字符（`\u200b` 零宽空格 / 普通空格）。
 *
 * ⚠️ 为什么要标出来：`违反法律法规` 与 `违反法律法规\u200b` 在界面上**看起来完全一样**，
 * 用户会当成下拉重复项，或选错了却以为选的是同一条 —— 而两条的 `tagIds` 是
 * `5 个` vs `1 个`，过滤结果不同（真机实测）。
 */
export function reasonTagHint(tag: ViolationReasonTag): string {
  const hidden =
    tag.tag_name.includes(ZERO_WIDTH_SPACE) ||
    (tag.tag_name.length > 0 && tag.tag_name.trimEnd() !== tag.tag_name);
  return hidden ? "同名（含不可见字符）" : "";
}

/**
 * 违规原因下拉：tag 名**原样**（可能带不可见字符），值用 `tag_name` 精确匹配。
 *
 * 标签里带上 `tag_ids` 条数 + 同名提示 —— 这两样是用户唯一能**分辨同名不同 ID**
 * 的线索（设计 43 §1.6）。标签只是展示，`value` 永远是原始 tag_name，不 trim。
 */
export function reasonOptions(tags: ViolationReasonTag[] | undefined): {
  value: string;
  label: string;
}[] {
  return (tags || []).map((tag) => {
    const ids = Array.isArray(tag.tag_ids) ? tag.tag_ids.length : 0;
    const hint = reasonTagHint(tag);
    return {
      value: tag.tag_name,
      label: `${tag.tag_name}（${ids} 项）${hint ? ` · ${hint}` : ""}`,
    };
  });
}

/** 按名字查 tagIds（精确匹配，不 trim —— 与后端同一判据）。 */
export function tagIdsForName(
  tags: ViolationReasonTag[] | undefined,
  name: string
): string[] {
  const hit = (tags || []).find((tag) => tag.tag_name === name);
  return hit ? [...hit.tag_ids] : [];
}

/** 处罚状态下拉（后端下发；这里只补一个「全部」空值项）。 */
export function statusOptions(options: ViolationEnumOption[] | undefined): {
  value: number | undefined;
  label: string;
}[] {
  const blank: { value: number | undefined; label: string }[] = [
    { value: undefined, label: "全部" },
  ];
  return blank.concat(
    (options || []).map((item) => ({ value: item.value as number | undefined, label: item.label }))
  );
}

// --------------------------------------------------------------------------- #
// 分页
// --------------------------------------------------------------------------- #
/** 千帆的分页尺寸选项（逐字：10/20/30/40/50）。 */
export const PAGE_SIZE_OPTIONS = [10, 20, 30, 40, 50];

/** 「共 N 条，M 页」（千帆原文）。总数 0 时也是 M=1。 */
export function renderTotalText(total: number, pageSize: number): string {
  const safeSize = pageSize > 0 ? pageSize : 10;
  const pages = Math.max(1, Math.ceil((Number(total) || 0) / safeSize));
  return `共 ${Number(total) || 0} 条，${pages} 页`;
}

// --------------------------------------------------------------------------- #
// 店铺体检
// --------------------------------------------------------------------------- #
export type CheckupTabKey = "all" | "violation-risk" | "negative-feedback-risk" | "advice";

export const CHECKUP_TABS: { key: CheckupTabKey; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "violation-risk", label: "违规风险" },
  { key: "negative-feedback-risk", label: "负反馈风险" },
  { key: "advice", label: "经营建议" },
];

/** 抽检页的处罚状态是**另一套**枚举（真机下拉：待落罚 / 已落罚 / 已取消）。 */
export const SPOT_CHECK_PUNISH_STATUSES = ["待落罚", "已落罚", "已取消"];

/** 抽检页申诉状态（与违规列表同：可/不可）。 */
export const SPOT_CHECK_APPEAL_FILTERS = [
  { value: true, label: "可申诉" },
  { value: false, label: "不可申诉" },
];
