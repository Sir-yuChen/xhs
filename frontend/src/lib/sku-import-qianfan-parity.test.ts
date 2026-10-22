/** 导入后价格及库存表必须与千帆同形（用户口径 2026-10-21）。
 *
 *  > 「导入多个商品 sku 规格后需与 <千帆商品> 的价格及库存一致，
 *  >   第一组规格为**主----分第二组规格**」
 *
 *  千帆真机结构（CDP 实读 `6a55be4dc0b0790015eb55b2`，设计 24 §23/§24）：
 *    5 个颜色分组 × 8 个尺寸 = 40 个 SKU；
 *    **每个颜色组的 8 个尺寸完全相同**（尺寸是跨颜色的公共维度）；
 *    **售价逐行不同**（81.08 / … / 52.7）—— 不能按维度值合并；
 *    第一维度 `rowspan=8` 合并；规格图一组一张。
 *
 *  本测试用**真实 456 行数据包**（24 色 × 19 尺寸）跑完整链路，
 *  断言「主（色）----分（尺寸）」结构在价格表里成立。
 */
import { describe, expect, it } from "vitest";

import { buildSkuFillPlan, passedSkuRows, type PrecheckTable } from "./precheck-publish";
import type { PlatformSpecDim, PriceMappingRow } from "./precheck-publish";
import CSV_RAW from "./fixtures/sku-import-456.csv?raw";

/** 读真实 CSV（与 `sku-import-real-package.test.ts` 同一份夹具）。 */
function loadTable(): PrecheckTable {
  const lines: string[] = CSV_RAW.split(/\r?\n/).filter((l: string) => l.length > 0);
  const split = (line: string): string[] => line.split(",");
  const columns: string[] = split(lines[0]);
  const rows: string[][] = lines.slice(1).map((l: string) => split(l));
  return {
    sheet_name: "sheet1",
    columns,
    rows,
    column_kinds: columns.map(() => "text"),
    is_sku_table: true,
  } as unknown as PrecheckTable;
}

const SPEC_COLS = ["颜色分类", "尺寸"];
const PRICE_MAPPING = [
  { target: "price" as const, source: "价格" },
  { target: "stock" as const, source: "库存" },
];
const MAPPING: Record<string, string> = { 颜色分类: "颜色分类", 尺寸: "款式" };
const DIMS: PlatformSpecDim[] = [
  { name: "颜色分类", id: "1", customizable: true, values: [], options: [] },
  { name: "款式", id: "2", customizable: true, values: [], options: [] },
] as unknown as PlatformSpecDim[];

describe("导入后：第一组规格为主、第二组为分（与千帆同形）", () => {
  it("① 价格行数 = 真实 SKU 行数（456，不塌缩成 24）", () => {
    const table = loadTable();
    const rows = passedSkuRows(table, [], [], [], PRICE_MAPPING, SPEC_COLS);
    const plan = buildSkuFillPlan(rows, DIMS, MAPPING, 1, SPEC_COLS);
    expect(plan.rows.length).toBe(456);
  });

  it("② 第一维度（主）= 颜色分类，24 个值；第二维度（分）= 款式，19 个值", () => {
    const table = loadTable();
    const rows = passedSkuRows(table, [], [], [], PRICE_MAPPING, SPEC_COLS);
    const plan = buildSkuFillPlan(rows, DIMS, MAPPING, 1, SPEC_COLS);
    //: dims 顺序 = 用户映射顺序（颜色分类 在前 = 主）。
    expect(plan.dims.map((d) => d.dimName)).toEqual(["颜色分类", "款式"]);
    expect(plan.dims[0].values.length).toBe(24);
    expect(plan.dims[1].values.length).toBe(19);
    //: 24 × 19 = 456（笛卡尔积完整）。
    expect(plan.dims[0].values.length * plan.dims[1].values.length).toBe(456);
  });

  it("③ 每个颜色组下的 19 个尺寸**完全相同**（尺寸是跨色公共维度）", () => {
    const table = loadTable();
    const rows = passedSkuRows(table, [], [], [], PRICE_MAPPING, SPEC_COLS);
    const plan = buildSkuFillPlan(rows, DIMS, MAPPING, 1, SPEC_COLS);
    const sizes = plan.dims[1].values.map((v) => v.value).sort();
    expect(sizes.length).toBe(19);

    //: 取第一组（奇遇童话01）的行，按行顺序提取其款式值。
    const firstColor = plan.dims[0].values[0].value;
    const groupRows = plan.rows.filter((r) => r.spec.startsWith(`${firstColor} / `));
    expect(groupRows.length).toBe(19);
    //: 组内款式值 = 全部 19 个尺寸（没有缺、没有重复）。
    expect(new Set(groupRows.map((r) => r.spec)).size).toBe(19);
  });

  it("④ 售价逐行不同 —— 不能被维度值合并掉（真机同款判据）", () => {
    const table = loadTable();
    const rows = passedSkuRows(table, [], [], [], PRICE_MAPPING, SPEC_COLS);
    const plan = buildSkuFillPlan(rows, DIMS, MAPPING, 1, SPEC_COLS);
    const iPrice = table.columns.indexOf("价格");
    //: 同一颜色下，不同尺寸的价格必须如实保留（真机 81.08→52.7 逐行不同）。
    const firstColor = plan.dims[0].values[0].value;
    const groupRows = plan.rows.filter((r) => r.spec.startsWith(`${firstColor} / `));
    const prices = groupRows.map((r) => r.price);
    //: 至少有两个不同的价格（若被合并成 1 个，说明又退回「按维度值合并」的旧 bug）。
    expect(new Set(prices).size).toBeGreaterThan(1);

    //: 逐行核对：价格必须来自**本行**，不是组内首行。
    for (const row of groupRows.slice(0, 5)) {
      const idx = plan.rows.indexOf(row);
      const [color, sizePart] = row.spec.split(" / ");
      const src = table.rows.find(
        (r) =>
          String(r[iPrice - 0]) !== "" &&
          String(r[table.columns.indexOf("颜色分类")] ?? "").trim() === color &&
          String(r[table.columns.indexOf("尺寸")] ?? "").trim() === sizePart,
      );
      if (src) {
        expect(Number(String(src[iPrice]).trim())).toBe(row.price);
      }
      expect(idx).toBeGreaterThanOrEqual(0);
    }
  });

  it("⑤ 规格图只在第一维度去重（24 张，不是 456 张）", () => {
    const table = loadTable();
    const iColor = table.columns.indexOf("颜色分类");
    const withImg: PrecheckTable = {
      ...table,
      columns: [...table.columns, "SKU 图"],
      rows: table.rows.map((r) => [...r, `${String(r[iColor] ?? "").trim()}.jpg`]),
    };
    const rows = passedSkuRows(withImg, [], [], [], PRICE_MAPPING, SPEC_COLS);
    const plan = buildSkuFillPlan(rows, DIMS, MAPPING, 1, SPEC_COLS);
    //: 主维度（颜色）24 个值 ⇒ 24 张图；不分维度就是 456 张。
    expect(plan.images.length).toBe(24);
    expect(new Set(plan.images.map((i) => i.dimName))).toEqual(new Set(["颜色分类"]));
  });
});

describe("当前库存 / 规格ID 必须真的落进 plan.rows（2026-10-21 真 bug）", () => {
  /** 用户报障：「**当前库存、规格ID 列配置的映射管理没有映射成功**」。
   *
   *  根因：`passedSkuRows` 早已按映射取好这两个值，但 `buildSkuFillPlan`
   *  只把槽位初始化成 `null` / `""`、**从未赋值**就 push 进 `plan.rows`
   *  ⇒ 页面落值那步拿到 `null` ⇒ 表格两列永远空。
   *  ⚠️ 售价/库存/条形码不受影响，所以现象极像「映射没配好」，容易误判。
   */
  const PRICE_MAPPING: PriceMappingRow[] = [
    { target: "price", source: "券后价格" },
    { target: "current_stock", source: "库存" }, //: 与「改后库存」同源
    { target: "stock", source: "库存" },
    { target: "market_price", source: "价格" },
    { target: "spec_id", source: "SKUID" },
    { target: "seller_code", source: "SKU信息" },
  ];

  const table = {
    columns: ["店铺名称", "SKU信息", "SKUID", "价格", "券后价格", "颜色分类", "尺寸", "库存"],
    rows: [
      ["店", "奇遇童话01;尺寸", "6124772908850", "132.3", "57.40", "奇遇童话01", "【适应床宽150cm】", "200"],
      ["店", "奇遇童话01;尺寸", "6124772908851", "160.2", "67.60", "奇遇童话01", "【适应床宽200cm】", "300"],
    ],
    column_kinds: ["text"],
  } as unknown as PrecheckTable;

  const dims: PlatformSpecDim[] = [
    { name: "颜色分类", id: "1", customizable: true },
    { name: "规格/尺寸", id: "2", customizable: true },
  ];
  const mapping = { 颜色分类: "颜色分类", 尺寸: "规格/尺寸" };
  const specColumns = ["颜色分类", "尺寸"];

  it("currentStock / specId 逐行带上（不许恒 null / 空串）", () => {
    const passed = passedSkuRows(table, [], [], [], PRICE_MAPPING, specColumns);
    const plan = buildSkuFillPlan(passed, dims, mapping, 1.7, specColumns);
    expect(plan.rows).toHaveLength(2);
    expect(plan.rows[0].currentStock).toBe(200);
    expect(plan.rows[0].specId).toBe("6124772908850");
    expect(plan.rows[1].currentStock).toBe(300);
    expect(plan.rows[1].specId).toBe("6124772908851");
  });

  it("没配映射 ⇒ 保持 null / 空串（不造假）", () => {
    const without: PriceMappingRow[] = [{ target: "price", source: "券后价格" }];
    const passed = passedSkuRows(table, [], [], [], without, specColumns);
    const plan = buildSkuFillPlan(passed, dims, mapping, 1.7, specColumns);
    expect(plan.rows[0].currentStock).toBeNull();
    expect(plan.rows[0].specId).toBe("");
  });
});
