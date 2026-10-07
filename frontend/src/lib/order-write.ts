/**
 * M7 订单**写操作**（发货 / 售后审核）的纯逻辑（设计 16 §7.4）。
 *
 * ⚠️ 这些是**真实平台写操作、不可撤回** —— 本模块只做「文案 / 判据 / 上限」，
 * 真正调接口在页面里，且必须**两层确认**（勾选 + 内容指纹）。
 *
 * 三条口径：
 * 1. **上限前后端同值**（`MAX_BATCH_SHIP` 与后端 `order_write_service.MAX_BATCH_SHIP` 相等）。
 * 2. **非目标状态不给按钮**（`canShipOrder` / `canHandleAfterSale`）—— 门禁在**点击之前**，
 *    不是「点了才知道不行」。
 * 3. **平台原文不美化**（失败原因原样展示，不换成「操作失败」）。
 */

/** 批量发货上限（⚠️ 与后端 `order_write_service.MAX_BATCH_SHIP` **必须相等**）。 */
export const MAX_BATCH_SHIP = 50;

/** 可发货的订单状态（与后端 `ORDER_STATUS_PENDING_SHIP` 同值）。 */
export const SHIPPABLE_STATUS = "待发货";

/** 可审核的售后状态（与后端 `AFTERSALE_STATUS_PENDING` 同值）。 */
export const HANDLEABLE_AFTERSALE_STATUS = "待处理";

/** 物流公司选项（首期给常见几家；平台清单接口在发货页，后续可下发）。 */
export const LOGISTICS_COMPANIES: Array<{ code: string; name: string }> = [
  { code: "SF", name: "顺丰速运" },
  { code: "YTO", name: "圆通速递" },
  { code: "ZTO", name: "中通快递" },
  { code: "STO", name: "申通快递" },
  { code: "YD", name: "韵达速递" },
  { code: "JD", name: "京东物流" },
  { code: "EMS", name: "邮政EMS" },
];

/**
 * 这笔订单能不能发货（门禁判据，**与后端同一条**）。
 *
 * ⚠️ 页面据此**不渲染发货按钮**（而不是「渲染了但点了报错」）——
 * 后端仍会再判一次（两处都判不是重复：前端是「不给假入口」，后端是「不许绕」）。
 */
export function canShipOrder(status: string | null | undefined): boolean {
  return (status ?? "") === SHIPPABLE_STATUS;
}

/** 这条售后能不能审核（门禁判据）。 */
export function canHandleAfterSale(status: string | null | undefined): boolean {
  return (status ?? "") === HANDLEABLE_AFTERSALE_STATUS;
}

/**
 * 批量发货的前置校验 —— 返回错误文案，`null` = 可以提交。
 *
 * ⚠️ 与后端同判据：空 ⇒ 拒；超 `MAX_BATCH_SHIP` ⇒ 拒（**不静默截断**）。
 */
export function batchShipBlockReason(
  items: ReadonlyArray<{ order_no: string; tracking_no?: string }>,
  maxShip: number = MAX_BATCH_SHIP,
): string | null {
  const valid = items.filter((item) => String(item.order_no ?? "").trim());
  if (valid.length === 0) return "没有选择要发货的订单。";
  if (valid.length > maxShip) {
    return `单次最多发货 ${maxShip} 笔，当前选了 ${valid.length} 笔。`;
  }
  const missing = valid.filter((item) => !String(item.tracking_no ?? "").trim());
  if (missing.length > 0) {
    return `有 ${missing.length} 笔还没填运单号。`;
  }
  return null;
}

/**
 * 二次确认弹窗的**影响面文案**（`N 笔订单 / 不可撤回`）。
 *
 * ⚠️ 文案必须说清「不可撤回」—— 用户以为能撤销而点了确认，是这里的最大风险。
 */
export function shipConfirmText(
  items: ReadonlyArray<{ order_no: string; tracking_no?: string }>,
): string {
  const count = items.filter((item) => String(item.order_no ?? "").trim()).length;
  if (count <= 1) {
    const one = items.find((item) => String(item.order_no ?? "").trim());
    return `将对订单 ${one?.order_no ?? "-"} 执行真实发货（运单号 ${one?.tracking_no || "未填"}）。此操作不可撤回。`;
  }
  return `将对 ${count} 笔订单执行真实发货。此操作不可撤回。`;
}

/** 售后审核的影响面文案（同意 / 拒绝）。 */
export function afterSaleConfirmText(
  action: "agree" | "reject",
  afterSaleId: string,
  amount?: number | null,
): string {
  const money = typeof amount === "number" ? `退款金额 ¥${amount.toFixed(2)}` : "退款金额未知";
  const verb = action === "reject" ? "拒绝" : "同意";
  return `将对售后单 ${afterSaleId} 执行真实${verb}（${money}）。此操作不可撤回。`;
}

/**
 * 写操作回执 → 用户提示（**区分部分成功**）。
 *
 * ⚠️ 批量结果**必须分层报**：「全部成功」「部分成功」「全部失败」三态，
 * 混成一句「操作完成」会让用户以为全发成功了。
 */
export function shipOutcomeText(result: {
  succeeded_count?: number;
  failed_count?: number;
  requested?: number;
}): { ok: boolean; text: string } {
  const ok = Number(result.succeeded_count ?? 0);
  const failed = Number(result.failed_count ?? 0);
  if (failed === 0) return { ok: true, text: `已发货 ${ok} 笔` };
  if (ok === 0) return { ok: false, text: `发货失败 ${failed} 笔（详见下方原因）` };
  return { ok: false, text: `部分成功：已发货 ${ok} 笔，失败 ${failed} 笔（详见下方原因）` };
}
