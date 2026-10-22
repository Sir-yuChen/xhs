/** 用**真实数据包**验证「商品 SKU 映射列去重 = 规格」与「价格按行落」的正确性。
 *
 *  数据包：`店透视_ID1073821618329_20261021/店透-SKU预览-表格-1073821618329-2026-10-21.csv`
 *  （456 行 × 13 列，实测：颜色分类 24 个 / 尺寸 19 个 / 组合 456 个 = 行数）。
 *
 *  ⚠️ 这里**不 import 页面**（`.tsx` 不被 vitest 覆盖），只验证 lib 层的判据；
 *  页面接线由 `tests/backend/test_page_optimizations.py` 的源码守卫盯。
 */
import { describe, expect, it } from "vitest";

import {
  buildSkuFillPlan,
  passedSkuRows,
  skuImageRelOf,
  skuSpecTextOf,
  type PrecheckTable,
} from "./precheck-publish";
import type { PlatformSpecDim } from "./precheck-publish";

//: ⚠️ 用 Vite 的 `?raw` 导入**仓库内的夹具副本**（tsconfig 没有 `node` 类型，
//: `import { readFileSync } from "node:fs"` 会 TS2307 且会让 `npm run build` 失败）。
//: 夹具 = 真实包 456 行的 6 列子集（见文件头说明），随仓库提交。
import CSV_RAW from "./fixtures/sku-import-456.csv?raw";

/** 极简 CSV 解析（本夹具没有引号内换行；真解析器在 `tabular_reader`）。 */
function loadTable(): PrecheckTable {
  const text = String(CSV_RAW).replace(/^\ufeff/, "");
  const lines: string[] = text.split(/\r?\n/).filter((l: string) => l.length > 0);
  const split = (line: string): string[] => line.split(",");
  const columns: string[] = split(lines[0]);
  const rows: string[][] = lines.slice(1).map((l: string) => split(l));
  return {
    title: "店透-SKU预览-表格-1073821618329-2026-10-21",
    sheet_name: "店透-SKU预览-表格-1073821618329-2026-10-21",
    columns,
    rows,
    column_kinds: columns.map(() => "text"),
    is_sku_table: true,
  } as unknown as PrecheckTable;
}

const SPEC_COLS = ["颜色分类", "尺寸"];

/** 价格库存映射（用户口径 2026-10-21：不再有 SKU 名称 / SKU 图 行）。 */
const PRICE_MAPPING = [
  { target: "price" as const, source: "价格" },
  { target: "stock" as const, source: "库存" },
];

/** 商品 SKU 映射关系（弹窗里用户配的）：列名 → 平台维度名。 */
const MAPPING: Record<string, string> = { 颜色分类: "颜色分类", 尺寸: "尺寸" };

const DIMS: PlatformSpecDim[] = [
  { name: "颜色分类", id: "1", customizable: true, values: [], options: [] },
  { name: "尺寸", id: "2", customizable: true, values: [], options: [] },
] as unknown as PlatformSpecDim[];

describe("真实数据包：商品 SKU 映射列去重 = 规格", () => {
  it("① 列去重数与实测一致（24 色 / 19 尺寸 / 456 组合）", () => {
    const table = loadTable();
    const idx = (name: string) => table.columns.indexOf(name);
    const colors = new Set(table.rows.map((r) => r[idx("颜色分类")]));
    const sizes = new Set(table.rows.map((r) => r[idx("尺寸")]));
    const combos = new Set(table.rows.map((r) => `${r[idx("颜色分类")]}|${r[idx("尺寸")]}`));
    expect(colors.size).toBe(24);
    expect(sizes.size).toBe(19);
    expect(combos.size).toBe(456);
    expect(table.rows.length).toBe(456);
  });

  it("② `skuSpecTextOf` 按行拼接（与 SKU信息 列逐字一致）", () => {
    const table = loadTable();
    const iInfo = table.columns.indexOf("SKU信息");
    const iColor = table.columns.indexOf("颜色分类");
    const iSize = table.columns.indexOf("尺寸");
    for (const row of table.rows.slice(0, 30)) {
      const cells: Record<string, string> = {};
      table.columns.forEach((c, i) => (cells[c] = String(row[i] ?? "").trim()));
      //: ⚠️ `SKU信息` 用 `;` 拼接，我们统一用 ` / `（与 spec_combinations 同形），
      //: 逐段比对而不是整串比对。
      const parts = skuSpecTextOf(cells, SPEC_COLS).split(" / ");
      expect(parts).toEqual([
        String(row[iColor] ?? "").trim(),
        String(row[iSize] ?? "").trim(),
      ]);
      expect(String(row[iInfo] ?? "").split(";")).toEqual(parts);
    }
  });

  it("③ **价格按行落**：456 个规格行，不是 24 个（旧行为）", () => {
    const table = loadTable();
    const rows = passedSkuRows(table, [], [], [], PRICE_MAPPING, SPEC_COLS);
    //: ⚠️ 必须传**商品 SKU 映射**（列名 → 平台维度名）—— `buildSkuFillPlan`
    //: 的第一道门禁就是「有没有配映射」，空映射会直接返回空计划。
    const plan = buildSkuFillPlan(rows, DIMS, MAPPING, 1, SPEC_COLS);
    //: 旧实现（按维度值合并）→ 只剩 24 行；新实现 → 456 行。
    expect(plan.rows.length).toBe(456);
    const specs = new Set(plan.rows.map((r) => r.spec));
    expect(specs.size).toBe(456);
  });

  it("③b **规格图按规格值去重**：只上传第一个维度去重后的图，不是每行一张", () => {
    //: 用户口径 2026-10-21（真机报「上传规格图 25/455」后定论）：
    //: 「导入规格图应**只在第一个组**需要上传，且是**去重后的数据行**对应规格图」
    //: —— 规格图挂的是**规格值**（颜色分类 24 个），不是 SKU **行**（456 行）。
    const table = loadTable();
    const iColor = table.columns.indexOf("颜色分类");
    //: ⚠️ SKU 图**不走映射参数**，它是固定的「SKU 图」列（用户口径 2026-10-21）
    //: ⇒ 夹具要真的把这一列建出来，否则 `skuImageRelOf` 拿不到值、`plan.images` 恒空。
    const withImg: PrecheckTable = {
      ...table,
      columns: [...table.columns, "SKU 图"],
      rows: table.rows.map((row) => [
        ...row,
        //: 同一颜色 ⇒ 同一张图（这才是真实数据包的样子）。
        `${String(row[iColor] ?? "").trim()}.jpg`,
      ]),
    };
    const rows = passedSkuRows(withImg, [], [], [], PRICE_MAPPING, SPEC_COLS);
    const plan = buildSkuFillPlan(rows, DIMS, MAPPING, 1, SPEC_COLS);
    //: 456 行 → 只有 24 张（= 颜色分类去重数），不是 456。
    expect(plan.images.length).toBe(24);
    //: 全部挂在**第一个维度**上；第二个维度一张都不该有。
    expect(new Set(plan.images.map((im) => im.dimName))).toEqual(new Set(["颜色分类"]));
    //: 值不重复（同色只传一次）。
    expect(new Set(plan.images.map((im) => im.value)).size).toBe(24);
  });

  it("④ 同一规格不重复、价格取自**本行**（不是首行）", () => {
    const table = loadTable();
    const rows = passedSkuRows(table, [], [], [], PRICE_MAPPING, SPEC_COLS);
    const plan = buildSkuFillPlan(rows, DIMS, MAPPING, 1, SPEC_COLS);
    const iPrice = table.columns.indexOf("价格");
    //: 随便取 5 行，逐行核对 spec → price 的对应关系没有错位。
    for (const i of [0, 1, 100, 300, 455]) {
      const cells: Record<string, string> = {};
      table.columns.forEach((c, k) => (cells[c] = String(table.rows[i][k] ?? "").trim()));
      const spec = skuSpecTextOf(cells, SPEC_COLS);
      const hit = plan.rows.find((r) => r.spec === spec);
      expect(hit, `第 ${i} 行找不到规格「${spec}」`).toBeTruthy();
      expect(hit!.price).toBe(Number(String(table.rows[i][iPrice] ?? "").trim()));
    }
  });

  it("⑤ 不传 `specColumns` ⇒ 键退回 `维度名:值` 形态（不是行级拼接）", () => {
    //: ⚠️ 这条钉住「`specColumns` 到底改了什么」：不传时价格行**仍然逐行生成**
    //: （2026-10-21 之前是「按维度值合并成 24 行」，那才是真正的丢失），
    //: 但 `spec` 文本是 `${维度名}:${值}` 拼接 —— 与第二步 `spec_combinations`
    //: **不同形**，按 key 对齐时会一行都对不上（价格表空白）。
    const table = loadTable();
    const rows = passedSkuRows(table, [], [], [], PRICE_MAPPING, SPEC_COLS);
    const legacy = buildSkuFillPlan(rows, DIMS, MAPPING, 1);
    expect(legacy.rows.length).toBe(456);
    expect(legacy.rows[0].spec.startsWith("颜色分类:")).toBe(true);
    //: 传了 `specColumns` ⇒ 行级拼接形态（无维度前缀）。
    const withCols = buildSkuFillPlan(rows, DIMS, MAPPING, 1, SPEC_COLS);
    expect(withCols.rows[0].spec.startsWith("颜色分类:")).toBe(false);
    expect(withCols.rows[0].spec).toContain(" / ");
  });

  it("⑥ **规格分组顺序 == 用户映射配置顺序**（不是平台规格顺序）", () => {
    //: 用户报 2026-10-21：「商品规格映射分组应与商品 SKU 映射关系配置顺序一致，
    //: 现在是顺序是倒的」。原实现按 `dims`（平台规格类型）排序，与用户配的顺序无关。
    //: ⇒ 这里把映射配成「尺寸 → 颜色分类」（与 DIMS 的平台序**相反**），
    //:   断言 `plan.dims` 就是用户配的顺序。
    const table = loadTable();
    const rows = passedSkuRows(table, [], [], [], PRICE_MAPPING, SPEC_COLS);
    const reversedMapping: Record<string, string> = { 尺寸: "尺寸", 颜色分类: "颜色分类" };
    const plan = buildSkuFillPlan(rows, DIMS, reversedMapping, 1, SPEC_COLS);
    //: ⚠️ `MAPPING` 的对象键顺序决定 `usable` 顺序 ⇒ 「尺寸」在前。
    expect(plan.dims.map((d) => d.dimName)).toEqual(["尺寸", "颜色分类"]);
    //: 反过来配一次，顺序也跟着反过来（证明真的跟着用户走，而不是碰巧）。
    const plan2 = buildSkuFillPlan(rows, DIMS, MAPPING, 1, SPEC_COLS);
    expect(plan2.dims.map((d) => d.dimName)).toEqual(["颜色分类", "尺寸"]);
  });
});

describe("真实数据包：SKU 图取固定列本行值", () => {
  it("⑤ `skuImageRelOf` 只认固定的「SKU 图」列", () => {
    const table = loadTable();
    //: 加固定列（预检里由 `withDerivedColumns` 插入）+ 填一行值。
    const withImg = {
      ...table,
      columns: ["SKU 图", ...table.columns],
      rows: table.rows.map((r, i) =>
        i === 7 ? [`SKU图/SKU图_08_奇遇童话08.jpg`, ...r] : ["", ...r],
      ),
    } as unknown as PrecheckTable;
    expect(skuImageRelOf(withImg, 7)).toBe("SKU图/SKU图_08_奇遇童话08.jpg");
    expect(skuImageRelOf(withImg, 8)).toBe(null);
    //: 没这列 ⇒ null（不猜、不回落别的列）。
    expect(skuImageRelOf(table, 7)).toBe(null);
  });
});
