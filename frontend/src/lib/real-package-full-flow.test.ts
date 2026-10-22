/** 真实数据包全流程实测（用户口径 2026-10-22：「测试跑全流程 …数据包」）。
 *
 *  数据包：`C:/Users/admin/Desktop/店透视_ID1073821618329_20261021`
 *  （456 行 SKU / 24 张 SKU 图 / 19 张详情图 / 5 张主图）
 *
 * 本文件**只度量、不断言**（断言在 `sku-import-qianfan-parity.test.ts`）：
 * 打印每一段的产出与耗时，用来回答「规格图到底该传几张、慢在哪」。
 */

import { describe, expect, it } from "vitest";
import { buildSkuFillPlan, passedSkuRows } from "./precheck-publish";
import type { PlatformSpecDim, PriceMappingRow } from "./precheck-publish";
import CSV_RAW from "./fixtures/sku-import-456-full.csv?raw";

function readCsv(): { columns: string[]; rows: string[][] } {
  const lines = CSV_RAW.split(/\r?\n/).filter((l) => l.trim() !== "");
  const split = (line: string): string[] => {
    const out: string[] = [];
    let cur = "";
    let quoted = false;
    for (let i = 0; i < line.length; i += 1) {
      const ch = line[i];
      if (ch === '"') {
        if (quoted && line[i + 1] === '"') {
          cur += '"';
          i += 1;
        } else quoted = !quoted;
      } else if (ch === "," && !quoted) {
        out.push(cur);
        cur = "";
      } else cur += ch;
    }
    out.push(cur);
    return out;
  };
  const header = split(lines[0]).map((h) => h.trim());
  return { columns: header, rows: lines.slice(1).map(split) };
}

const table = { ...readCsv(), column_kinds: [] } as unknown as Parameters<typeof passedSkuRows>[0];

const PRICE_MAPPING: PriceMappingRow[] = [
  { target: "price", source: "券后价格" },
  { target: "current_stock", source: "库存" },
  { target: "stock", source: "库存" },
  { target: "market_price", source: "价格" },
  { target: "spec_id", source: "SKUID" },
  { target: "seller_code", source: "商品ID" },
];

const DIMS: PlatformSpecDim[] = [
  { name: "颜色分类", id: "1", customizable: true },
  { name: "规格/尺寸", id: "2", customizable: true },
];

const MAPPING = { 颜色分类: "颜色分类", 尺寸: "规格/尺寸" };
const SPEC_COLUMNS = ["颜色分类", "尺寸"];

describe("真实数据包全流程（456 行）", () => {
  it("从读表到规格图清单，逐段打印", () => {
    const t0 = performance.now();

    const passed = passedSkuRows(
      table as never,
      [],
      [],
      [],
      PRICE_MAPPING,
      SPEC_COLUMNS,
    );
    const t1 = performance.now();

    const plan = buildSkuFillPlan(
      passed,
      DIMS,
      MAPPING,
      1.7,
      SPEC_COLUMNS,
    );
    const t2 = performance.now();

    //: 数据包里有图的规格值（第一维度=颜色分类，24 个色）
    const colorValues = new Set(passed.map((r) => String(r.specValue ?? "").trim()).filter(Boolean));

    console.log("=== 真实数据包全流程 ===");
    console.log(`表：${table?.columns.length ?? 0} 列 / ${table?.rows.length ?? 0} 行`);
    console.log(`① passedSkuRows：${passed.length} 行，耗时 ${(t1 - t0).toFixed(1)}ms`);
    console.log(`② buildSkuFillPlan：${plan.rows.length} 行价格库存，耗时 ${(t2 - t1).toFixed(1)}ms`);
    console.log(`③ 规格图清单：${plan.images.length} 张`);
    console.log(`   第一维度去重后的值：${colorValues.size} 个`);
    console.log(`   去重键样例：${plan.images.slice(0, 3).map((i) => `${i.dimName}:${i.value}`).join(" | ")}`);
    console.log(`   skip：${plan.skipped.length} 条 ${JSON.stringify(plan.skipped.slice(0, 2))}`);
    console.log(`   价格非空行：${plan.rows.filter((r) => r.price !== null).length}`);
    console.log(`   库存非空行：${plan.rows.filter((r) => r.stock !== null).length}`);
    console.log(`   规格ID非空行：${plan.rows.filter((r) => r.specId !== "").length}`);
    console.log(`   当前库存非空行：${plan.rows.filter((r) => r.currentStock !== null).length}`);

    expect(passed.length).toBeGreaterThan(0);
    expect(plan.rows.length).toBeGreaterThan(0);
  });
});
