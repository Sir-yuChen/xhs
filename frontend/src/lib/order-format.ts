/**
 * 订单管理的**纯逻辑**（设计 16 §7，M6 · 一期只读）。
 *
 * 与 `lib/task-records.ts` / `lib/workflow-format.ts` 同规：页面只渲染，
 * 判据下沉到这里，行为脚本可直接驱动真实函数断言。
 *
 * ## 三条口径（各有守卫）
 *
 * 1. **词表来自后端**（`order.status` 的六个值）—— 表外值**原样显示、不造名**
 *    （DEF-057 同型：编一个近义词就是给用户看平台没说过的话）。
 * 2. **缺值显示 `-`，不补 0** —— 金额缺值 `-`（不是 `￥0.00`），
 *    因为「没这个数」和「这个数是 0」是两回事（退款率 / 平均发货时长同理）。
 * 3. **脱敏提示** —— 平台对某些订单**本身就只给脱敏值**（真机实测：
 *    `高*` / `189***2857`）。`includeFull` 只是「不做二次脱敏」，
 *    **不等于一定有原值**；页面不许承诺「点开就能看到完整信息」。
 */

/** 后端 `ORDER_STATUSES` 的镜像（**顺序即展示顺序**）。 */
export const ORDER_STATUSES = [
  "待付款",
  "待发货",
  "待收货",
  "已完成",
  "售后中",
  "已关闭",
] as const;

/** 状态 → 颜色（antd Tag 的 color 值；表外值走 `default`）。 */
export function orderStatusColor(status?: string | null): string {
  switch (status) {
    case "待付款":
      return "gold";
    case "待发货":
      return "orange";
    case "待收货":
      return "blue";
    case "已完成":
      return "green";
    case "售后中":
      return "red";
    case "已关闭":
      return "default";
    default:
      return "default";
  }
}

/**
 * 状态 → 展示文本。
 *
 * ⚠️ **表外值原样返回**：后端平台码映射表可能先于前端扩展（`ORDER_STATUS_MAP`
 * 加一个码就会多一个新状态）。此时若返回「未知」或 `-`，用户就**看不到平台
 * 实际给了什么**。空值才退 `-`（缺值口径）。
 */
export function orderStatusText(status?: string | null): string {
  const text = (status ?? "").trim();
  return text === "" ? "-" : text;
}

/** 金额 → `￥1,234.50`；缺值 → `-`（**不是** `￥0.00`）。 */
export function moneyText(value?: number | null, currency = "￥"): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  return `${currency}${value.toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** 百分比（0.1234 → `12.34%`）；缺值 → `-`。 */
export function percentText(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  return `${(value * 100).toFixed(2)}%`;
}

/** 时长（小时，1 位小数 + `h`）；缺值 → `-`。 */
export function hoursText(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  return `${value.toFixed(1)}h`;
}

/**
 * 时间戳 → `MM-DD HH:mm`；缺值 → `-`。
 *
 * ⚠️ 只做「有没有值」的判断 + 截断，**不做时区换算** —— 后端下发的已经是
 * 上海本地时间（`_iso()` 统一口径），前端再转一次会**整体偏移 8 小时**。
 */
export function timeText(value?: string | null): string {
  const text = (value ?? "").trim();
  if (!text) {
    return "-";
  }
  // 形如 2026-09-03T18:51:20 或 2026-09-03 18:51:20
  const m = text.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (!m) {
    return text;
  }
  return `${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
}

/** 商品行 → `商品A（红色） x2`；缺名不编（返回空串，由调用方过滤）。 */
export function orderItemText(item: {
  item_name?: string | null;
  sku?: string | null;
  quantity?: number | null;
}): string {
  const name = (item.item_name ?? "").trim();
  if (!name) {
    return "";
  }
  const sku = (item.sku ?? "").trim();
  const qty = item.quantity ?? 0;
  return `${name}${sku ? `（${sku}）` : ""} x${qty}`;
}

/** 一单的商品摘要（多件用 `|` 连；全缺 → `-`）。 */
export function orderItemsSummary(items?: Array<Record<string, unknown>> | null): string {
  const parts = (items ?? []).map((it) => orderItemText(it as never)).filter(Boolean);
  return parts.length === 0 ? "-" : parts.join(" | ");
}

/**
 * 收件人信息是否**可能被平台脱敏过**（含 `*`）。
 *
 * ⚠️ 这是给页面出提示用的：值为 `高*` / `189***2857` 时，`includeFull=true`
 * **也给不出更多信息**（库里存的就是平台给的脱敏值）。
 * ⇒ 页面这时要提示「平台未提供完整信息」，而不是让用户以为按钮坏了。
 */
export function looksMasked(value?: string | null): boolean {
  return (value ?? "").includes("*");
}

/** 筛选/展示共用的脱敏提示文案（后端只做二次脱敏，不会还原平台脱敏）。 */
export function maskingNotice(name?: string | null, phone?: string | null): string {
  if (looksMasked(name) || looksMasked(phone)) {
    return "平台未提供完整收件信息（当前即为平台所给原值）";
  }
  return "默认脱敏显示；发货 / 打单场景可开启完整信息";
}

/** 导出前的强制提示（导出**含完整值**，必须先告知）。 */
export const EXPORT_WARNING = "导出文件将包含完整收件人姓名、手机号与地址，请妥善保管。";

/** 状态计数：**每个状态都要有数**（缺键渲染 0，页面不用补样板）。 */
export function statusCounts(byStatus?: Record<string, number> | null): Record<string, number> {
  const source = byStatus ?? {};
  const out: Record<string, number> = {};
  for (const status of ORDER_STATUSES) {
    out[status] = source[status] ?? 0;
  }
  return out;
}
