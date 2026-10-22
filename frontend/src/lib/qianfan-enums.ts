/** 千帆商品发布枚举 —— **与后端 `backend/app/core/qianfan_item_enums.py` 一一对应**。
 *
 * ## 为什么要单独一份
 *
 * 前端要用来**渲染选项文案**、拼提交体字段；后端要用来**组装提交体**。
 * 两边各写一份一定会漂移（本项目已经有过：`deliveryMode` 前端当发货模式、
 * 后端当物流类型 ⇒ 平台报「商品不支持设置无物流发货」）。
 *
 * ⇒ 规则：
 *   · **数字值只在本文件与后端枚举两处出现，且必须相同**（`qianfan-enums.test.ts` 守）；
 *   · 界面文案可以在前端，但**值**必须引用这里的常量，不许内联字符串；
 *   · 未坐实的值（例如「审核通过后立即上架开售」的编码）**不写**，缺值不造假。
 *
 * 取证：CDP 直读千帆商品编辑页 `input[type=radio]` 的 `value`（2026-10-21）。
 */

// ────────────────────────────────────────────────────────────────
// ① 物流类型（提交体 `itemData[].deliveryMode`）
// ────────────────────────────────────────────────────────────────

/** **物流类型** —— 提交体字段 `itemData[].deliveryMode`。
 *  ⚠️ 这**不是**「发货模式」（现货/预售）：那是 SKU 级 `skuData[].deliveryTime`。 */
export const LogisticsType = {
  /** 物流配送（需上传快递单号）—— **默认**，绝大多数类目只有这一档。 */
  LOGISTICS: 0,
  /** 无物流发货或物流配送（无需快递单号）—— 仅类目支持时可选。 */
  NOT_LOGISTICS: 1,
} as const;

/** 界面文案（与 `LogisticsType` 的值一一对应）。 */
export const LOGISTICS_TYPE_LABELS = {
  logistics: "物流配送",
  notLogistics: "无物流发货或物流配送",
} as const;

/** 「无物流发货或物流配送」的说明（真机 bundle 原文）。 */
export const NOT_LOGISTICS_HINT =
  "在订单发货页面支持选择无物流发货，无需上传快递单号即可发货成功；支持选择物流配送，需上传快递单号发货。请根据实际情况选择物流配送模式";

// ────────────────────────────────────────────────────────────────
// ② 发货时间（提交体 `skuData[].deliveryTime.type`）
// ────────────────────────────────────────────────────────────────

/** **发货模式的真身**：`skuData[].deliveryTime.type`。
 *
 *  | 界面 | 提交体 |
 *  |---|---|
 *  | 现货 · 当日发货 | `{type: 3}`（**不带 time**） |
 *  | 现货 · 24/48 小时 | `{type: 4, time: "24"/"48"}` |
 *  | 全款预售 · 时段 N 天 | `{type: 4, time: String(N*24)}` |
 *  | 全款预售 · 定时某日 | `{type: 5, time: "YYYY/MM/DD"}` |
 */
export const DeliveryTimeType = {
  /** 当日发货（不带 time） */
  SAME_DAY: 3,
  /** 按小时数（预售按 天×24 换算） */
  HOURS: 4,
  /** 按固定日期 */
  DATE: 5,
} as const;

/** 现货发货时效的**界面取值**（真机 radio `value`）。 */
export const SPOT_SHIP_TIME = {
  /** 当日发货 */
  SAME_DAY: "-1",
  /** 24 小时 */
  H24: "24",
  /** 48 小时（推荐，默认选中） */
  H48: "48",
} as const;

// ────────────────────────────────────────────────────────────────
// ③ 开售时间 / 回看讲解 / 库存类型 / 商品状态
// ────────────────────────────────────────────────────────────────

/** **开售时间**（真机 radio `value`）。
 *  ⚠️ 「审核通过后立即上架开售」的编码未坐实 ⇒ 不写（缺值不造假）。 */
export const SaleTimeMode = {
  /** 不设置，手工上架开售（默认） */
  MANUAL: 2,
  /** 定时开售 */
  SCHEDULED: 3,
} as const;

/** **回看讲解挂件**（真机 radio `value`）。 */
export const PlaybackWidget = {
  HIDDEN: 0,
  SHOWN: 1,
} as const;

/** 库存类型（真机骨架值）。 */
export const StockType = {
  SPOT: 1,
} as const;

/** 商品上架状态。 */
export const ItemStatus = {
  ON_SHELF: 1,
  OFF_SHELF: 2,
} as const;

/** 售后 · 七天无理由（真机骨架值）。 */
export const FreeReturn = {
  SUPPORTED: 1,
} as const;
