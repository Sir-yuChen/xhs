/**
 * 展示层格式化助手（**唯一实现**）。
 *
 * 分 → 元的换算在本项目出现在多个页面（商品台账的价格列、内容库详情卡的挂载商品），
 * 各写一份必然漂移（DEF-044/047/058）—— 特别是「区间价」与「取不到」两种形态，
 * 漏一种就是界面上的 `NaN` 或假 `0`。
 */

/** 分 → 元字符串（`1999` → `"19.99"`）。取不到给 `-`（缺值口径，**不造 0**）。 */
export function yuan(fen: number | null | undefined): string {
  if (fen === null || fen === undefined || !Number.isFinite(fen)) return "-";
  return (fen / 100).toFixed(2);
}

/**
 * `shop_products.price` 的**分字符串** → 元展示串。
 *
 * 后端 `shop_goods_sync_service._price_fen_text` 的契约（设计 13 §19.4）：
 * 同价 `"1999"`、区间 `"1999~2999"`、取不到 `""`。区间价直接 `Number()` 会得
 * `NaN` ⇒ 界面显示「¥ NaN」（渲染了但读不到，缺值口径明令禁止）。
 *
 * 空 / 契约外形状 ⇒ `""`（调用方按缺值口径给文案；**不猜**、不造 0）。
 */
export function fenPriceText(text: string): string {
  const raw = (text || "").trim();
  if (!raw) return "";
  const parts = raw.split("~");
  const yuanTexts: string[] = [];
  for (const part of parts) {
    const value = Number(part.trim());
    if (!Number.isFinite(value)) return "";
    yuanTexts.push((value / 100).toFixed(2));
  }
  return yuanTexts.join("~");
}
