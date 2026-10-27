/**
 * 商品列表的**导出前置校验**（纯逻辑，可单测）。
 *
 * ⚠️ 本文件原为 M4「商品编辑优化」的纯逻辑集（PATCH payload 拼装 / `updated[]`
 * 中文名 / 采纳备选），**编辑那四件已随「编辑商品功能逻辑移除」整条删除**
 * （2026-10-27 用户口径「编辑商品功能逻辑移除」）：
 * `EDITABLE_FIELDS` / `buildEditPayload` / `updatedSummary` / `adoptCandidate`。
 * 原因：商品已改平台实时数据，本地台账（含 `ai_title` / `ai_selling_point`）整表 DROP，
 * 「改本地标注」这件事的前提不存在了。
 *
 * 只留导出这条 —— 它与编辑无关，仍被商品列表工具栏使用。
 */

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
